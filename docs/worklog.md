# 工作记录

按项目 AGENTS.md：至少每小时记录一次；阶段完成即提交。

## 2026-11-24

### 09:20–09:35 基线、团队契约与扩展点
- 基线：`691 passed / 592 subtests`，先提交前一轮未提交的 per-agent chat 功能（`457eb98`）。
- 通读 `docs/units-plan.md`、`security-fix-plan.md`、`config.py`、`app.py`、`web/api.py`、`web/server.py`。
- 建立团队模式：`docs/team-plan.md` 定义 7 个工作流（sec / llm / agent / units / ops / dist / front）、文件所有权、API 契约、验收标准；team task `task-1`…`task-7` 已建，7 个 teammate 已并行启动。
- Lead 完成扩展点：`stackflowos/extensions.py`（隔离式 build/start/stop/units/routes/capabilities/health/on_config）、App 生命周期与单元合并、`WebReply` + `current_auth`、扩展路由注册。
- 扩展配置 schema：`llm.routes/fallback/rpm/tpm/预算/stream`、`agents.task_timeout_s/max_tool_calls/queue_max/config_reload_s`、`skills/plugins/mcp/observability/storage/cluster` 新 section、`web.tls_*/session/users`、`security.sandbox/workspace_git/audit_*`。
- 配置密钥脱敏：`/api/config` 屏蔽 api_key/token/密码；PATCH `***` 回填真实值；`sys.config.updated` 事件不再泄漏密钥。
- 新增 `tests/test_extensions.py`（9 用例）：扩展契约、失败隔离、App 合并/生命周期、扩展路由/WebReply、认证上下文、配置脱敏 round-trip。
- 提交：`feat(core): extension points, expanded config schema, redacted config patches`。
- 并行进度：7 个 teammate 已 claim `task-1`…`task-7`，分别负责安全、LLM、Agent、单元/能力、可观测性、分布式/存储、前端。

### 09:35–09:50 并行工作流交付与首批集成
- sec 完成：TLS 监听、单次 launch token + control socket、会话表/多用户角色、Landlock/bwrap 沙箱、审计脱敏、net.fetch 固定 IP（227 相关测试通过）。
- ops 完成：Prometheus 指标、webhook 通知、备份/恢复/导出、日志轮转。
- dist 完成：事件 gzip 轮转/跨归档、租约 fencing/时钟偏移、SQLite backend + 迁移、mDNS、ZMQ bridge、Dockerfile/systemd。
- agent 完成：优先级队列、任务超时/工具次数、热更新、git 分支隔离、reviewer fs.write、沙箱接线。
- llm 完成：原生 tool calling（OpenAI/Anthropic/Gemini）、流式、usage/cost/上下文/限流/熔断、角色路由/降级。
- units 完成：能力目录、skills/plugins/MCP、单元探针/迟滞/动作/依赖图、UnitClient/unit://；示例插件。
- front 完成：单元三层/详情/依赖图/动作、i18n（518 键）、会话 UI、LLM 用量、能力管理、git/依赖图、流式渲染。
- Lead 接线：`EXTENSION_MODULES` 全部 13 个扩展加载；EventLog 轮转参数、SQLite backend、租约 fencing/作用域；App 生命周期/健康检查/冷备锁；CLI `--tls-cert/--tls-key/--standby`、`sfo url`、`backup/restore/export-events/backup-status/storage-migrate`；`/api/units` summary；skill 注入（worker + agent chat）；`capability.call` approval_id；net.fetch pinned 路径；supervisor.close。
- 提交：`35f9039`（安全）、`f7fc029`（可观测性）、`10d3df5`（分布式）、`38cff17`（Agent）、`659d3dd`（LLM）、`1463893`（单元/能力）、`0d63a5e`/`5200d2b`（Lead 集成）、`56eef95`（前端）。
- 新增测试：`tests/test_feature_endpoints.py`（真实 App + WebServer 跑扩展端点/control socket 单次登录）、`tests/test_extensions.py`（契约/冷备/配置脱敏）、各子系统 `test_sec_*/test_ops_*/test_dist_*/test_agent_*/test_llm_*/test_units_*/test_front_*`。

### 09:50–10:05 文档、验收与残余项
- `docs/review-response.md`：六类评审逐条对照（✅ 已修复 / 🟡 部分 / 📝 设计说明），并列明长期项。
- `docs/operations.md`：TLS、`sfo url`、多用户、沙箱、通知、指标、备份恢复、SQLite 迁移、mDNS、冷备、单元/能力控制面。
- `README.md`：能力/skill/plugin/MCP、真实模型路由/流式/配额、安全模型、运维命令、诚实边界更新。
- `docs/architecture.md`：扩展契约、新事件目录、新配置 section、非目标/长期项、§15 子系统实现。
- `docs/security-fix-plan.md` §14：reviewer fs.write、lease 作用域、审计脱敏、launch token、DNS 固定 IP 标记完成；新增 §14.1 安全增补记录与残余风险。
- 全量测试目标：见最后一次 `pytest` 输出；所有新增模块 `python -m compileall` 通过。

### 10:00–10:20 工人反馈：项目入住设施、失败语义与读预授权
- 背景：一位工人在空项目 workspace 里的反馈——缺门牌/目录骨架/种子任务/自检入口，
  读动作频繁弹审批，失败只有 `failed` 且 `error` 为空，事件无法按 agent/时间回放。
- **策略边界修复**：`PolicyEngine.workspace_for` + `workspace_resolver`，`ApprovalBroker`
  的风险与 `path_prefix` 授权改用项目真实 workspace；接入项目（attached-ro/rw）里的
  `fs.read/fs.list` 回到 low 自动放行，写仍 medium 人审，未知/非法 id fail-closed。
  提交：`8abde18` `fix(policy): use attached project workspace for risk and approval scopes`。
- **失败语义**：Agent 新增终态 `blocked`（缺 scope/workspace/任务、max_steps、超时、
  工具上限、连续失败），`failed` 只留给执行错误；两者强制非空 `error` + `error_code`；
  runner 在 claim 前 preflight；事件 `agent.blocked` 独立；派工强制 project_id 且
  workspace 存在；UI 的活动/结束筛选与顺序同步。
- **项目入住包**：新增 `stackflowos/projects/onboarding.py`（brief.md、固定目录、种子
  任务、记忆初值、只读接入拒绝写入）与 `stackflowos/projects/health.py`（14 项检查 +
  `ready/not_ready_reasons/next_actions/fix`）。创建项目自动入住；接入项目只做只读
  巡检 + 状态侧种子；`POST /api/projects/{id}/onboard` 幂等补齐。
- **自检入口**：Agent 工具 `project.health`；RPC `project.health`（按 principal 带 agent
  binding 检查，单位只能看自己项目）；`GET /api/projects/{id}/healthcheck`；
  CLI `sfo project health`（离线只读）与 `sfo project scaffold`（只写 workspace）；
  项目详情新增「入住自检」卡（ok/warn/fail、fix、补齐/重检按钮）。
- **读预授权与记忆**：`security.auto_allow_low` 默认生效（读类自动放行）；`memory.list`
  RPC/工具/角色授权；Runner view、LLM compact state、Agent 对话上下文注入项目记忆。
- **事件回放**：`EventLog.query` 支持 `agent_id/source/types/since_ts/until_ts`；API
  `/api/events` `/api/console` 与工具 `event.query` 接线；新增 `sfo events` 命令行。
- **文档**：新增 `docs/onboarding.md`；更新 `README.md`、`docs/architecture.md`
  （§5.7 事件、§6.2 workspace 边界、§7 入住、§8.1 blocked/failed）。
- **测试**：新增 `test_projects_onboarding/health`、`test_project_onboarding_app`、
  `test_attached_workspace_policy`、`test_event_replay`；更新 runner/supervisor/limits/
  queue/attach/event-trust/rpc-scope 断言；全量 **1152 passed + 623 subtests**（2 skipped）。
- **提交**：`f6ae4c6` `feat(onboarding): project kit, health check, blocked/failed split,
  replay filters`；`6f99e86` 文档；`3768e0d` 统一 `agent.spawn` RPC 的项目 scope 守卫；
  `906351b` `fix(agents): use the LLM brain when routes or fallback are configured`
  （并行阶段遗留的 5 行修复）。

### 10:20–10:30 最终验收
- 全量测试：`/tmp/sfo-venv/bin/python -m pytest -q` → **1152 passed, 2 skipped, 623 subtests passed**（~96s；2 skipped 为真实组播/pyzmq 可选路径）。
- `python3 -m compileall -q stackflowos tests`、`git diff --check`、所有 `webassets/js/*.js` 的 `node --check` 通过。
- 端到端：`stackflowos demo --until-done --no-web` 在临时 state 下完成 7 个任务并产出 workspace 文件。
- 远程/TLS：本机自签证书启动 `hub --tls-cert/--tls-key --port 18999`，`stackflowos url` 经 control socket 铸造一次性 URL → HTTPS `?token=` 兑换 303 + HttpOnly cookie → `/api/health` 200；第二次兑换被拒绝。
- 清理：停止遗留的仓库 `.stackflowos` hub 进程，避免占用 17800；工作树只保留文档变更。
- 团队看板：task-1…task-7 全部 completed；7 个 teammate 已停止，无后台任务。

## 2026-11-24（下午）自治闭环：巡检 / 派工 / 代理审批

### 11:30–12:00 设计与契约
- 目标：让 OS 自己巡系统、自己派工、自己批审批，但不是"系统给自己无限放权"——
  由人预授权围栏 + 配额 + 审计 + 急停。
- 新增 `docs/team/autonomy-plan.md` 作为并行契约（文件所有权、模块接口、事件/API、
  验收）；4 个 teammate 并行：patrol / guard（dispatcher+guardian）/ api / front。
- 新增配置段 `autonomy`（24 个字段，默认全关；枚举/上下界/capability 白名单/
  shell 正则可编译校验；删除/系统/项目管理类能力禁止进入自动审批白名单）。
- 新增 `stackflowos/autonomy/`：`models.py`（Finding/DispatchHint/PatrolReport/
  fingerprint）、`service.py`（loop/状态/`run_patrol`/`status`/`configure`）、
  `patrol.py`、`dispatcher.py`、`guardian.py`、`api.py`；`extensions.py` 注册为第
  15 个扩展，挂 `app.autonomy`，贡献 `autonomy.0` 单元。

### 12:00–13:30 四个工作流交付
- patrol：12 项只读检查（hub/unit/project/health/task.idle/stalled/agent.failure/
  approval.pending/denial/lease.orphan/failure burst），Snapshot+纯函数，
  fingerprint 稳定，20 个测试。
- guard：Dispatcher（off/tasks/agents、fingerprint 去重+冷却、小时配额、自治在岗
  Agent 上限、每轮 5 动作、建任务/派工失败隔离、每 fingerprint 5 次尝试上限）；
  Guardian（pending/agent-*、capability 白名单+硬拒绝、风险上限、项目/角色/在岗数、
  workspace 路径且非 `.git`、shell 正则+cwd+控制符、域名、人类否决冷却、小时配额、
  min_age；enforce 走 `decide_approval(mode=once, by=autonomy:guardian)`）；
  62 个测试。
- api：`GET /api/autonomy[/status]`、`POST /api/autonomy/patrol`、
  `PATCH /api/autonomy/policy`，读需认证、写需 operator；16 个测试。
- front：Dock「自治」应用（状态卡/发现/派工/审批/策略表单/倒计时/移动端），
  `#autonomy` 深链，zh/en 632 键，13 个静态测试；截图 `docs/img/autonomy.png`。
- Lead：操作员工具 `autonomy.status/patrol/configure`、CLI `sfo autonomy
  status|patrol|set`、服务测试、配置测试、真实 App 闭环集成测试。

### 13:30–14:05 集成、真实闭环与验收
- 新增 `task.review`（review 无人处理 → 派 reviewer）与 `task.blocked`（报告，
  不派工）检查，补 7 个测试；observe 建议事件按 request_id 去重，避免每轮刷屏。
- **真实两小时无人值守闭环**（临时 state，mock LLM，17888）：753 轮巡检、0 错误、
  4 次自主派工（architect/researcher/coder/tester），10 次代理审批（fs.write、
  agent.spawn），项目 p-1 到 76%（2 done / 3 review / 1 blocked）。
  重启加载新检查后再派 3 个 reviewer，t-1/t-3 评审完成；剩余阻塞项被
  `task.blocked` 正确报告。截图 `docs/img/autonomy-live.png`（767 轮巡检、3 项待处理）。
- 全量测试：**1307 passed, 2 skipped, 680 subtests passed**（94.5s）。
- `compileall`、`git diff --check`、`node --check` 全过。
- **真实 provider 缺陷**：live 网关对工具名 `fs.read` 返回 HTTP 400
  （`^[a-zA-Z0-9_-]+$`）。新增 `provider_tool_name()`：schema 发 `fs_read`，
  消费端映射回 `fs.read`，native 历史也回写安全拼写；直连网关验证返回
  `fs_read` 工具调用；补 11 个测试并更新 3 处旧断言。全量 **1319 passed,
  2 skipped, 680 subtests**。
- 提交：`303c3cf` 配置/数据形状/服务骨架；`dbe1b46` 操作员工具/CLI/测试；
  `03443cb` patrol+dispatcher+guardian+API；`bbb59f9` 前端；
  `3a263c1` review/blocked 检查与文档；`8e3ea87` provider 工具名修复。
- 上线：live hub（17800，systemd）备份后重启，`autonomy.enabled=true,
  dispatch_mode=agents, approve_mode=enforce`（fs.write/agent.spawn，risk≤medium；
  shell/删除/系统/项目管理仍人审）。首个真实 reviewer 在工具名修复前失败，
  修复后网关直连验证通过；该失败作为 `agent.failure` 留在巡检报告里。
- 残余：权限不足的 agent 被拒后若一直重试会消耗派工尝试（5 次后停）；
  guardian 只按 (角色, capability) 尊重人类否决，未细化到具体路径/命令；
  自治不自动完成项目（done 仍是人的动作）；单 hub 假设、配额重启可能轻微超限。

### 14:10–15:05 Web 控制台改用 Astryx 重写（第一波）
- 决策：旧控制台（原生 JS/CSS，无构建）停止演进，新建 `webui/`——React 19 +
  Astryx 设计系统 + Vite；构建产物仍写入 `stackflowos/webassets`，运行时零 Node
  依赖。核心屏（对话/项目/Agent/单元/审批/事件/自治/设置）并行重写中。
- 地基（lead，已提交 `05cc640`）：Astryx neutral 主题（明/暗/跟随系统）、AppShell +
  TopNav + SideNav、hash 路由（兼容旧 `#agent/<id>` 深链）、api 客户端、
  不可变 store + SSE reducer + `useStore` selector、i18n（导出旧字典 632 键 +
  每屏独立 keys 模块，`import.meta.glob` 自动合并）、会话/401 门、toast 桥、
  移动端 TopNav 收敛、`webui/README.md` 架构与规则。
- fixture：`scripts/ui_mock.py` 补齐会话/LLM/能力/skills/plugins/MCP/autonomy/
  单元详情探针动作/审批详情/项目 health·onboard·access·git/单 Agent 对话等
  端点与种子数据；ticker 重复文案改为覆盖，截图不再是一墙重复。
- CSP：Astryx 运行时注入一段 position-try 样式表并用内联 CSS 变量传尺寸，
  `style-src 'self'` 会把两者都拦掉（overlay 定位静默失效）。改为
  `style-src 'self' 'unsafe-inline'`，`script-src` 仍为 `'self'`；注释写明
  理由（CSS 不能执行脚本、UI 全部 text 渲染）。
- 对话屏（lead）：Astryx `ChatLayout/ChatMessage*/ChatComposer` + `Markdown`，
  流式草稿/思考态、乐观发送、快捷建议空态；右侧「现场」面板（项目卡/在岗
  Agent/事件 ticker）均深链到完整屏。明暗与 420px 移动端截图通过。
- 团队：5 个 teammate 认领 task-1…task-5 并行重写其余屏幕（写作用域互斥：
  `webui/src/screens/<x>/**` + `webui/src/i18n/keys/<x>.ts`），约定不 commit、
  共享层改动先报 lead。
