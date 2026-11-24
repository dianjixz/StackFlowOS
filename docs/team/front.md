# front — 前端交付说明（task-7）

> 角色：前端 owner（`stackflowos/webassets/**`、`tests/test_front_*.py`、本文件）。
> 工作目录：`/home/nihao/work/workspace/StackFlowOS`。本轮未改任何 Python 文件，未做 git 写操作。

## 0. 结论

评审列出的 1–9 项全部落地，纯原生 JS/CSS/HTML、无构建步骤、无新增运行时依赖。
新增 6 个 JS 模块、3 个静态测试文件、约 400 行 CSS；`index.html` 只加 `data-i18n*`
属性、会话 chip 和 i18n 模块入口，没有内联脚本/事件处理器。

验证：

```bash
/tmp/sfo-venv/bin/python -m pytest -q \
  tests/test_web_assets.py tests/test_front_i18n.py \
  tests/test_front_units.py tests/test_front_session_llm.py \
  --basetemp=/tmp/pytest-front
# 59 passed
```

`node --check` 对 `stackflowos/webassets/js/*.js` 全部通过。
另外用 `scripts/ui_mock.py` + 临时 fixture harness（在 /tmp，不在仓库内）跑了真实 Chromium
截图冒烟：单元三层页、详情抽屉、依赖图、设置-AI/安全/能力、项目 Git、流式回答、
401 登录门、viewer 只读、420px 移动端，均正常；无 console 崩溃。

## 1. 改动文件

### 新增

| 文件 | 作用 |
|---|---|
| `stackflowos/webassets/js/i18n.js` | zh/en 字典、`t(key, params)`、`data-i18n*` 扫描、localStorage 持久化、`on('lang')` 总线事件、缺词回退中文 |
| `stackflowos/webassets/js/units.js` | 单元页重构：三层分组、概览条、过滤器、详情抽屉、requires/wants SVG 有向图、最近状态事件、descriptor 动作按钮、空态/坏态区分 |
| `stackflowos/webassets/js/session.js` | 当前身份 chip、会话列表/撤销/轮换/登出、viewer 写操作门禁、401 阻塞式重新登录门 |
| `stackflowos/webassets/js/llm.js` | `/api/llm/usage` + `/api/llm/models` 面板（tokens/成本/RPM/TPM/项目预算/熔断/路由/降级），routes/fallback JSON 编辑器 |
| `stackflowos/webassets/js/capabilities.js` | 设置-能力：Skills（列表/校验/项目级启停）、Plugins（load/unload）、MCP（servers connect/disconnect、tools、调用测试） |
| `stackflowos/webassets/js/projectextras.js` | 任务依赖（`deps`/`depends_on`/`blocked_by`）文字边 + SVG 依赖图；工作区 Git（分支/提交/diff/回滚） |
| `tests/test_front_i18n.py` | 字典完整性、键覆盖、回退/插值、data-i18n、加载顺序 |
| `tests/test_front_units.py` | 三层/概览/过滤/抽屉/依赖图/动作/空态/可访问性静态断言 |
| `tests/test_front_session_llm.py` | 会话/LLM/能力/Git/依赖/流式/无构建 静态断言 |

### 修改

| 文件 | 改动 |
|---|---|
| `webassets/index.html` | `data-i18n*` 属性；topbar 会话 chip（`session-chip`/`session-user`/`session-role`）；i18n 模块先于 main 加载；无内联脚本 |
| `webassets/js/api.js` | 新端点方法、`isUnsupported/isForbidden/isConflict` 分类、401 时派发 `sfo:unauthorized` 事件 |
| `webassets/js/store.js` | `notify()` 总线导出；`chat.delta` 记录 `messageId`；重连时丢弃不可续的草稿并发 `stream-reset` |
| `webassets/js/chat.js` | 快照/重连 `syncMessages()`（补消息、更新文本、final 到达后清草稿、去重）；`stream-reset`；空态/副标题 i18n；`rerenderChat()` |
| `webassets/js/apps.js` | 单元页委托 `units.js`；设置新增「能力」tab、AI 用量面板 + routes/fallback JSON 编辑、安全区会话面板、常规区语言切换；项目详情接入依赖区与 Git 卡；任务卡依赖徽标；审批 viewer 隐藏写按钮；设置保存 viewer 隐藏；ESC/关闭 sheet 时关闭抽屉；语言切换重渲染 |
| `webassets/js/ui.js` | `statusLabel/roleLabel/appLabel` 走 i18n；相对时间/运行时长 zh/en；toast/空态/载入文案 i18n |
| `webassets/js/panel.js` | 工作面板关键文案 i18n；导出 `rerenderPanel()` |
| `webassets/js/main.js` | `initI18n/initSession/initUnits`；SSE 状态标签 i18n；`on('lang')` 统一重渲染 |
| `webassets/css/os.css` | 23 节：会话 chip、单元概览/三层/过滤器/行/抽屉/SVG 图、任务依赖图、Git、能力卡、LLM 指标/预算、语言切换、401 门、移动端适配 |

### 依赖的其他工作流（没有改他们的文件）

- 单元/技能/插件/MCP 的扩展路由（`units/control.py`、`skills/`、`plugins/`、`mcp/`）
- 会话：`security/sessions.py`
- LLM：`ai/llm_api.py`
- Git：`agents/extension.py` + `agents/git_api.py`
- 路由契约测试的扩展扫描：lead 已更新 `tests/test_web_assets.py`（本前端不再需要额外接线）。

## 2. 功能与 API 对应

| 需求 | 前端入口 | 后端接口 |
|---|---|---|
| 单元页三层/概览/过滤 | 单元 app（Dock） | `GET /api/units`、`GET /api/units/summary` |
| 单元详情抽屉 | 点击单元行 | `GET /api/units/{id}`（描述符 + `events` + `actions`） |
| 描述符动作 | 抽屉按钮，按 `descriptor.actions` 渲染 | `POST /api/units/{id}/probe`、`POST /api/units/{id}/actions {action}` |
| 依赖图 | 抽屉「依赖关系」SVG | 描述符 `requires[]`/`wants[]`（详情 `dependency` 也读取容器字段） |
| 最近状态事件 | 抽屉列表 | 详情 `events[]`（缺失时回退本地 `state.events` 中的 `unit.*`） |
| Skills | 设置-能力 | `GET /api/skills`、`POST /api/skills/{id}/enable|disable {project_id}` |
| Plugins | 设置-能力 | `GET /api/plugins`、`POST /api/plugins/{id}/load|unload` |
| MCP | 设置-能力 | `GET /api/mcp/servers`、`POST .../connect|disconnect`、`GET /api/mcp/tools?server=`、`POST /api/mcp/call` |
| LLM 用量/配额 | 设置-AI 顶部 | `GET /api/llm/usage`、`GET /api/llm/models` |
| routes/fallback 编辑 | 设置-AI「路由与降级」 | `PATCH /api/config {patch:{llm:{routes,fallback}}}`（复用现有表单收集器，JSON 先校验） |
| 会话管理 | 设置-安全 + topbar chip | `GET /api/sessions`、`POST /api/sessions/rotate`、`DELETE /api/sessions/{id}`、`POST /api/logout` |
| 任务依赖 | 项目详情 | 任务模型 `deps[]`（兼容 `depends_on`/`blocked_by`） |
| 工作区 Git | 项目详情 | `GET /api/projects/{id}/git`、`POST /api/projects/{id}/git/rollback {ref}` |
| 流式渲染 | 对话区 | SSE `chat.delta` / `agent.chat.delta`，最终 `chat.message`（含重连 `?since=` 去重） |

### 降级行为

- 404/405/501 → 面板内「未启用」提示，不弹红错、不阻塞其它区块（单元/详情/技能/插件/MCP/LLM/Git 各自独立）；
- 403 / 后端 `operator role required` → 中文友好提示（viewer 直接隐藏或禁用写操作）；
- 409 → 「单元状态已变化，已刷新详情」；
- 401 → 全局阻塞式「会话已过期」门，提示用启动 URL 重新登录，不自动重试；
- `/api/units/summary` 缺失时用本地计数推 health；`/api/units` 缺 `summary` 包装时回退 `/api/state` 快照；
- Git `repo=false` 显示「该工作区还不是 git 仓库」而不是「干净」。

## 3. i18n 覆盖范围

字典规模：zh / en 各 518 键，键集合完全一致（有测试）。

已覆盖（主路径）：

- 外壳：`<title>`、跳过链接、topbar 工具提示、SSE 状态、`noscript`；
- 导航/Dock/移动端 tab：全部 6 个应用 + 各 aria-label；
- 对话：标题、副标题、输入框 placeholder、发送、提示、快捷建议区 aria、空态、思考中、上下文条；
- 工作面板：标题/计数、项目空态/状态文案、在岗 Agent 空态、实时事件空态；
- 单元：三层标题/提示、概览/健康/问题单元、全部/仅有问题/仅独立进程/项目/搜索、表格列、行状态、详情抽屉所有分区（运行位置/租约/依赖/能力/端点/事件/JSON）、动作按钮与确认/403/409/失败提示、全部空态；
- 审批：标题/副标题、空态、四个决定按钮与 tooltip、规则/最近决定区、完整预览、viewer 提示；
- 设置：7 个 tab、各 formCard 标题/hint、语言切换、保存/重新载入、保存提示、关于页字段、viewer 提示；
- 会话：面板标题、身份、表头、当前标记、撤销/轮换/登出/刷新、确认与结果、401 门；
- LLM：用量指标、熔断、模型/路由/降级、项目预算、空态/未启用；
- 能力：Skills/Plugins/MCP 三块、校验状态、加载/卸载/连接/断开/调用测试、未启用提示；
- 项目依赖与 Git：依赖区、依赖图、Git 分支/状态/提交/diff/回滚/确认；
- 通用：刷新/关闭/保存/重试/载入中/空/未知、状态词（30+）、角色词（9+）、toast 关闭。

明确**未翻译**（保留中文/服务端原文，属可接受范围）：

- `ui.eventSummary()` 里的事件摘要文案（事件流/最近决定的时间线；数据本身多为 ID/路径）；
- 设置页大量 `cfgField` 字段名与长 hint（主路径 tab/card/按钮已翻译）；
- 项目新建/接入表单、规则表单、Agent 派工表单的字段级标签；
- Agent 花名册/项目详情中服务端提供的 `label`（例如单元 `label` 本身是中文）；
- 快捷建议的 prompt 文本（发送给系统的指令，故意保持中文）；
- 后端错误 message 原样展示（先经 `toastError`，未做机器翻译）。
- 兼容性：缺词回退中文，`en` 不会出现键缺失（测试保证）。

切换行为：设置-常规「界面语言」两个按钮 → `setLang()` → localStorage → `applyI18n(document)` →
`notify('lang')` → main 重渲染 chat/panel/当前 sheet/单元抽屉，**不刷新整页**。
（设置里原有 `system.language` 配置项保留，用于后端/Agent 侧语言，不是界面语言。）

## 4. 测试覆盖（tests/test_front_*.py）

- `test_front_i18n.py`：zh/en 字典存在；16 个键族两边都有；en 不出现 zh 缺失键；导航/单元/审批/会话/设置/LLM/能力/按钮关键键存在；回退与 `{param}` 插值；localStorage 持久化；`data-i18n*` 扫描；index 中 i18n 先于 main 且均为 module；无内联事件。
- `test_front_units.py`：三层分组、概览（health/count/problems）、四类过滤、行点击 → `GET /api/units/{id}`、运行位置/租约/端点、requires/wants SVG（无图表库）、动作由 descriptor 驱动、403/409 友好、空 vs 坏、详情回退、JSON 折叠、抽屉关闭/Esc/实时事件、无 `innerHTML`、新 API 方法。
- `test_front_session_llm.py`：会话 API/面板/viewer 门禁/401 门/chip id；LLM usage/models 指标与预算/熔断/容量字段、缺失降级、routes JSON 校验保存；Skills/Plugins/MCP API 与「未启用」；任务依赖 `deps` 模型 + SVG；Git 读取与回滚；`chat.delta` 草稿/最终去重/重连 `stream-reset/syncMessages`；无 npm/打包器引用；新模块无 markup 注入。

## 5. 残余风险 / 已知取舍

1. **当前会话身份是推断的**：`GET /api/sessions` 不返回“哪一行是当前会话”。前端按「显式 current 标记 → 单行 → 最近 `last_seen`」推断。单用户/单会话准确；多会话 operator 下 topbar 可能显示最近活跃用户而非严格当前用户。要彻底解决需后端在响应里加 `current_session_id` 或 `/api/whoami`。
2. **没有后端登录页**：401 门是客户端全屏提示 + 重新加载，依赖用户拿到启动 URL（或 systemd 的 `SFO_WEB_TOKEN` 对应 URL）。若 sec 后续提供 `/login`，可在 `session.js::showLoginGate` 增加跳转。
3. **操作员拒绝是 400**：当前扩展用 `BadRequest("operator role required")`。前端已按 message 关键字映射成 403 式提示并做 viewer 隐藏；如果后端统一改成 `Denied` 更干净。
4. **i18n 是主路径覆盖**，不是全站逐字翻译（见第 3 节未翻译清单）。事件摘要、表单字段级文案仍是中文。
5. **流式渲染是单草稿模型**：同一端同一时刻只有一条全局流；`agent.chat.*` 有独立 reducer。重连会丢弃不可续的草稿（最终消息由快照补齐），不会重复渲染，但不会“续写”断线前的半句话。
6. **单元依赖图是确定性的两列布局**：依赖很多时靠 SVG 高度 + 抽屉滚动；不是通用图形布局引擎。通配依赖（如 `llm.*`）按前缀判断“已解析”。
7. **Git 卡按需拉取**：项目详情每次渲染复用 12s 缓存，回滚后主动失效；没有 SSE 级实时刷新。
8. **`/api/units?include=descriptor` 的 include 参数被核心路由忽略**：页面依赖 unit cards 本身已携带完整描述符（当前实现满足）；如果后端未来只返回 brief，需更新 `units.js::normalizeList`。
9. **契约形状宽容解析**：LLM/sessions/git 的形状按当前实现 + 常见别名读取；后端若重命名核心桶（如 `prompt/completion` → 其他），只影响对应面板显示，不会崩页面。
10. **移动端**：420px 下单元行三列/两行布局、概览改 grid、LLM 指标两列；已截图确认。未做真机触摸测试。

## 6. 交接 / Lead 接线

- 不需要 Lead 改 `extensions.py` 或 `app.py` 来启用本前端；所有新端点按既有 `routes()`/核心路由调用。
- 本前端没有新增任何后端依赖；`tests/test_web_assets.py` 的扩展路由扫描已由 Lead 更新，当前 59 个前端相关测试全绿。
- 本轮无 Python 文件改动、无 git 写操作；工作树中的 `stackflowos/webassets/js/agentchat.js` 早期改动属于 agent-chat/llm 工作流，不在本说明的交付范围内。
- 建议后续：后端补 `/api/whoami` 或 sessions 的 `current_session_id`；把扩展的 operator 拒绝统一为 403；若需要全站英文，再按第 3 节清单续翻。


---

## task-4 — 控制台「自治」应用交接（2026-11-24）

日期：2026-11-24 · 所有者：front · 任务：`task-4`（revision 2 → complete）
范围：只改 `stackflowos/webassets/**` 与 `tests/test_front_autonomy.py`；未改后端 Python / config / 他人测试；未执行任何 git 写操作。

## 1. 改动文件

| 文件 | 改动 |
|---|---|
| `stackflowos/webassets/js/autonomy.js` | **新增**（约 700 行）。导出 `mount()/unmount()/isMounted()/render(options)/tick(root)`；渲染顶部状态卡、巡检发现、自主派工、代理审批、策略表单，并实现局部 policy patch 收集与前端围栏提示。 |
| `stackflowos/webassets/js/apps.js` | `APP_META` 增加 `autonomy`；`renderSheet` 增加 `case 'autonomy'`；`showApp` 在 `name === 'autonomy'` 时 `autonomy.mount()` 且首次 `loadAutonomyStatus()`；`on('event')` 对 `autonomy.*` 做 500ms debounce 刷新；`hideSheet`/离开应用清 `autonomyTimer` 并 `autonomy.unmount()`；新增 `renderAutonomyApp/loadAutonomyStatus/runAutonomyPatrol/saveAutonomyPolicy/scheduleAutonomyReload`；`tickCountdowns` 每秒驱动 `autonomy.tick`；findings 的 task 链接通过 `view.pendingTaskId` 打开项目看板并选中任务。 |
| `stackflowos/webassets/js/api.js` | 新增 `getAutonomy()`（GET `/api/autonomy/status`）、`runAutonomyPatrol(dryRun=false)`（POST `/api/autonomy/patrol`）、`patchAutonomyPolicy(patch)`（PATCH `/api/autonomy/policy`，body 即 patch）。 |
| `stackflowos/webassets/index.html` | Dock 在「审批」「事件」之间新增自治按钮（`data-open-app="autonomy"`、`aria-label`、`data-i18n-aria-label="dock.aria.autonomy"`、`data-i18n="dock.autonomy"`）；新增 SVG symbol `#i-autonomy`（24×24、脉冲线 + 圆环、`stroke="currentColor"`）。 |
| `stackflowos/webassets/js/i18n.js` | zh/en 双字典新增 `app.autonomy`、`app.sub.autonomy`、`dock.autonomy`、`dock.aria.autonomy` 及约 80 个 `autonomy.*` 键（模式、风险、能力、空态、错误、viewer、配额、校验提示、trigger）。 |
| `stackflowos/webassets/css/os.css` | 追加第 16 节自治样式：hero/指标/计数/列表/能力网格/policy 块/焦点样式；`.pill[data-severity]`、`.pill[data-status=disabled]`；760px/430px 响应式（hero 单列、能力单列、metrics 两列、counts 两列）。 |
| `tests/test_front_autonomy.py` | **新增**，13 个 unittest 静态用例（模块/端点/注册/首载/事件刷新/i18n 键完整性/无 innerHTML/CSS 响应式/局部 patch/viewer 只读/Dock 顺序与 symbol）。 |
| `docs/img/autonomy.png` | **新增**截图（临时 hub，见 §4）。 |
| `docs/team/front.md` | 本文件。 |

## 2. UI 结构（`#autonomy`）

1. **工具栏**：`N 轮巡检 · M 个错误`、刷新按钮（`onRefresh` → GET status）。
2. **顶部状态卡**（`.autonomy-hero`）
   - 标题「自治中枢」+ enabled 徽标（`已开启`/`已停用`）+ running 徽标（`巡检中`）；
   - headline（report.headline，空则「还没有巡检记录」）；
   - 指标：上次巡检（相对时间 + trigger）、下轮巡检（实时倒计时，`data-autonomy-next`，由 apps 的 1s interval 驱动；disabled 显示「未开启」）、累计巡检；
   - 计数：critical / warn / info / total（`.autonomy-counts`）；
   - 右侧：启用开关（PATCH `{enabled}`）、「立即巡一遍」按钮（POST；请求中按钮变「巡检中…」并 disabled；viewer/running 时 disabled 且 title/hint 说明）、只读提示。
3. **巡检发现**：severity pill（`.pill[data-severity]`）、check、title、dispatchable tag、project/task 链接、agent/unit、时间、detail、`建议：fix`；空态「系统健康 / 本轮巡检没有发现需要处理的问题」。
4. **自主派工**：action tag（建任务 / 派 Agent）、status pill（成功/跳过/失败）、fingerprint、role/project/task/agent/时间、reason；空态说明开启派工模式后会发生什么；hint 显示当前 mode 与条数。
5. **代理审批**：decision pill（允许/观察/跳过，`observe` 额外显示「仅建议」tag）、capability、risk pill、principal/request/project/时间、reason；空态说明 observe/enforce 与人类优先。
6. **自治策略**（`form-card`）：enabled、dispatch_mode（off/tasks/agents）、approve_mode（off/observe/enforce）、approve_risk_max（low/medium/high）；能力多选 `fs.read/fs.list/fs.write/shell.exec/net.fetch/llm.chat/task.manage/agent.spawn/data.read/data.write`；shell/net 额外条件文字提示 + `approve_domains`（逗号分隔）、`approve_shell`、`approve_shell_patterns`（每行一条）；配额 `dispatch_max_agents / dispatch_max_tasks_per_hour / dispatch_max_spawns_per_hour / approve_max_per_hour`；保存按钮。viewer：所有控件 + 保存 disabled，底部提示只读，提交前仍二次检查 `canWrite()`。

复用现有 `app-toolbar/card/form-grid/form-card/field/section-head/app-note/pill/risk-pill/tag/btn/empty-state`；未引入任何 npm 或外部资源。

## 3. 行为 / 安全 / 刷新生命周期

- **首载**：`showApp('autonomy')` → `autonomy.mount()` + `loadAutonomyStatus()`（token 防竞态；失败保留错误并渲染“接口未启用/加载失败”说明，不刷错误 toast 循环，404/405/501 视为未启用）。
- **刷新**：`on('event')` 中 `current.name === 'autonomy'` 且 `type.startsWith('autonomy.')` → `scheduleAutonomyReload()`（500ms debounce）。表单聚焦时跳过自动刷新，避免吞掉半编辑的策略；手动刷新/保存会重新同步。
- **关闭**：`hideSheet` 清 `autonomyTimer` + `autonomy.unmount()`；切到其它 app 在 `showApp` 中同样清理。`autonomy.js` 内部不创建任何 timer（`setInterval` 仅由 apps 既有全局 interval 通过 `tick()` 使用），关闭后不再发请求。
- **安全**：所有动态文本经 `el()`/`textContent`/属性设置，模块内无 `innerHTML/insertAdjacentHTML/outerHTML/document.write`（测试断言）。
- **局部 patch**：`collectPolicyPatch` 只比较 `[data-autonomy-policy]` 控件与 `status.policy` 对应字段，并单独收集能力复选框；提交体仅含变化字段（数组按内容比较），绝不回传整个 config。前端会先拦截 shell.exec/net.fetch 的非法组合，服务端仍是最终权威。
- **viewer**：`canWrite()` 来自 session；开关、保存、巡检均不可用；服务端 403 仍会以 toast 呈现（服务端权威）。
- **task 链接**：findings/派工记录中的 task 号 → 打开所属项目看板并选中/滚动到该任务（`view.pendingTaskId` 一次性消费）；任务不存在时 `loadTasks` 会清空选中。

## 4. 测试与自验

命令与结果：

```
cd /home/nihao/work/workspace/StackFlowOS
/tmp/sfo-venv/bin/python -m pytest -q tests/test_front_autonomy.py tests/test_front_i18n.py tests/test_web_assets.py --basetemp=/tmp/pytest-front
# 29 passed in 0.08s
```

顺手跑全量前端静态（含既有 units/session_llm）：

```
/tmp/sfo-venv/bin/python -m pytest -q tests/test_front_*.py tests/test_web_assets.py --basetemp=/tmp/pytest-front
# 71 passed in 0.10s
```

其中 `tests/test_front_autonomy.py`：13 passed（模块导出、三端点、APP_META/case/showApp 首载、事件刷新与关闭清 timer、Dock 顺序与 i18n 属性、`i-autonomy` symbol、双字典键（含 t() 静态键全覆盖）、无 HTML 注入、viewer 只读、局部 patch、CSS 响应式）。

运行时自验（未触碰 17800 真实 hub）：

- 临时 hub：`/tmp/sfo-front-state`，端口 `18765`（已停止；`17800` 的状态未被修改）。
- 用 9230 上 headless Chrome 新建独立 tab，经 `sfo url` 新 token 登录后打开 `#autonomy`：
  - `document.querySelector('.autonomy-app')` 存在，sheet 标题「自治」，`errors: []`；
  - 通过 UI 同源 fetch 执行 PATCH `{enabled:true,dispatch_mode:'tasks',approve_mode:'observe'}` + POST patrol → 均 `ok`，页面刷新后 headline「系统正常」、enabled=true、倒计时递减；
  - 用合成 status 直接 `render()`：4 条记录（2 findings + 1 dispatch + 1 approval）、severity/decision/observe「仅建议」/project+task 链接点击、`collectPolicyPatch` 提交 → 仅 `{"dispatch_mode":"agents"}`；
  - 390×844 移动端：hero 单列（324px）、能力网格单列、metrics 两列、`scrollWidth === 390` 无横向溢出。
- 截图：`docs/img/autonomy.png`（1440×1100，临时 hub，enabled + 2 轮巡检数据）。

## 5. 残余风险 / 需 lead 注意

1. **API 契约依赖**：写作时 `stackflowos/autonomy/api.py` 已落地并与本前端一致——GET `/api/autonomy/status` 返回 `status()`；POST `/api/autonomy/patrol` 返回 report；PATCH `/api/autonomy/policy` 的 body 是**局部 patch 本身**，响应 `data` 是完整 status（含 `policy`）。若路径/返回形状变动，需要同步改 `api.js` 与 `apps.js` 的 `saveAutonomyPolicy`（现在以 `'policy' in data` 判断，否则回退重新 GET）。
2. **`autonomy` 未加入 `main.js` 的 `APP_SHORTCUTS`**：Alt+1..6 仍是原有六个应用；自治通过 Dock 按钮或 `#autonomy` 直达。若希望 Alt+7，需要在 `main.js` 增加一项（本次未改，避免超范围）。
3. **策略表单额外字段**：为满足后端校验，UI 除契约字段外还提供 `approve_domains/approve_shell/approve_shell_patterns`（都属 autonomy 局部配置，仍只 PATCH 变化字段）。前端校验与后端一致，但以后端为准。
4. **自动刷新暂停条件**：策略表单或任意 input/select/textarea 聚焦时，`autonomy.*` 事件触发的刷新被跳过；用户手动刷新/保存即可同步。避免编辑中重渲染丢输入，属于有意取舍。
5. **viewer 身份到达前**：`canWrite()` 在 `/api/sessions` 返回前默认允许（既有全局行为）；此时写请求由服务端 403 拒绝并显示 toast，不会越权。
6. **倒计时精度**：下轮倒计时由 `next_run_in_s` 在渲染时换算为绝对时间，秒级递减；实际定时以服务端为准，状态事件/手动刷新会校正。
7. **截图数据**：`docs/img/autonomy.png` 来自临时 state（enabled、1 轮巡检、空发现），不是真实运行数据；`docs/img/` 为新增目录，不影响静态测试。
8. 未提供 dry-run UI；`api.runAutonomyPatrol(dryRun)` 已支持，如需“演练”按钮可后续加。
