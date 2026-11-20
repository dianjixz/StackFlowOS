# StackFlowOS 单元模型规划

> 外部评审：`claude-sonnet-5-5`（Open WebUI）
> 对话：http://192.168.20.38:18848/c/fd0fecec-a0b7-44bd-94c8-5cbf96701c95
> 本文是设计规划；文首附本仓库已落地的 Phase 1 子集。

## 本仓库已落地（Phase 1 子集）

当前 hub 启动后会登记 12 个单元（无 worker 时）：

| tier | 单元 |
| --- | --- |
| system | `sys.0`、`store.eventlog`、`store.project`、`store.memory`、`scheduler.0`、`gateway.0`、`context.0` |
| service | `approval.0`、`llm.0`、`tool.0`、`skill.0`、`plugin.0` |

- 服务单元是 **in-process logical unit**：`runtime.mode=in_process`，`pid/rpc_address` 为 `null`，
  不伪造地址；调用侧使用 `unit://<id>`。
- `sys.0` 保留真实 PID 与 NNG RPC/events 地址；worker 单元仍是一个 Agent 一个进程。
- `skill.0` 已登记但状态为 `disabled`（SkillRegistry 属于规划 Phase 2）；
  `plugin.0` 是插件宿主（当前 0 个插件）；`tool.0` 是工具目录（执行仍在 worker 内）。
- `/api/units` 支持 `tier/kind/state/project_id/status` 过滤；状态栏在线口径为
  `ready|degraded|online|suspect|draining`。

下面是 claude-sonnet-5-5 的完整规划原文。

---

# StackFlowOS「单元（Unit）」模型与落地方案

## 0. 结论摘要

1. **单元 = 有独立生命周期、可观测健康、对外有契约、有独立失败域、需要被单独管理的运行时实体。** 它不等于进程。进程只是单元的一种承载形态（`runtime.mode`）。
2. **Unit 和 Catalog 分开。** 单元是「提供者/运行体」，工具、skill、审批单、项目、会话是「目录条目/数据」。`TOOLS` 里的每个工具不是单元，提供工具的 `tool.*` 才是。每个 skill 也不是单元，`skill.0`（SkillRegistry）才是。每个外部插件有代码生命周期和失败域，所以每个插件是一个单元。
3. **先做 in-process 逻辑单元，不拆进程。** 用「描述符 + 健康探针 + 状态机」把现有服务登记进 UnitRegistry。全新 hub 无 worker 时约 11 个单元，既不空也不灌水。
4. **描述符用 `runtime` + `endpoints[]` + `lease`，不再用平铺的 `pid/rpc_address`。** 服务单元这些字段为 `null`，不伪造。旧字段保留为兼容投影。
5. **UI 分三层：系统单元 / 服务单元 / 工人单元。** 在线 = `ready|degraded`，按层统计。保留「只看独立进程」作为过滤器，而不是整页的定义。

---

## 1. 单元的定义与准入

### 1.1 定义

> **Unit**：在 StackFlowOS 中具备**可寻址身份**、**显式生命周期状态**、**健康信号**、**对外能力契约**、**监管者**的运行时实体。承载形态可以是子进程、hub 内常驻服务（线程/协程/对象）、远程端点，或纯声明式包。

### 1.2 准入五问

对任何候选对象逐条回答，**必须满足 ①③，且总共至少 4 条**，才登记为 Unit：

| # | 问题 | 例：ApprovalBroker | 例：单个 TOOL `fs.read` |
|---|---|---|---|
| ① | 有独立生命周期（能 start/stop/降级/重启），且状态可变？ | ✅ | ❌ 随注册表存在 |
| ② | 有真实健康信号（探针/心跳/错误率），不是常量？ | ✅ 队列、存储可写 | ❌ |
| ③ | 对外有契约（abilities / RPC / 事件）？ | ✅ | ⚠️ 是契约条目，不是提供者 |
| ④ | 有独立失败域，坏了可以单独降级/替换？ | ✅ 坏了 fail-closed | ❌ |
| ⑤ | 用户/运维需要单独查看、配置、启停？ | ✅ | ❌ 只需要在目录里看 |

### 1.3 应该是 / 不应该是 Unit

**应该是：**
- worker 进程里的 Agent。
- hub 内有独立失败域的常驻服务：EventLog、ProjectStore、MemoryStore、Scheduler、Web/SSE 网关、审批、LLM provider、ToolRegistry/Broker、SkillRegistry、Context 容器。
- 外部插件，每个 manifest 一个。

**不应该是（反假单元清单）：**
- 单个工具、单个 skill、单条审批 ticket、单次 LLM 调用。
- Project（它是**作用域/边界**，不是运行体）、Session、消息、策略规则（数据）。
- HTTP handler 线程、工具函数、纯函数库。
- ChatService：它是编排门面，没有独立生命周期。除非以后拆成会话路由服务，否则作为 gateway 的内部组件。
- Cordis 的子 Context（项目级作用域）：先作为 `context.0` 详情里的树节点，不单独成单元。

**四条硬规矩（防止假单元）：**
1. `state` 必须由真实探针或租约推导，不能写死 `ready`。
2. 不得伪造 `pid/address`，没有就是 `null`，UI 显示「—」和运行形态。
3. 全新 hub 的单元数应在 10–12 个，不会因为工具或 skill 变多而膨胀。
4. 目录条目（工具/skill）走 `/api/capabilities`，不走 `/api/units`。

---

## 2. Unit 分类法（kind）

`tier` 的划分标准是「用户能对它做什么」：
- **system**：内核基础设施，不可停用，只能观察，必要时重启。
- **service**：能力服务，可配置、可启停。
- **worker**：工人，用户或调度器可创建和销毁。

| kind | tier | 对应实体 | 运行形态 | 租约 | 失败域 / 失败策略 | 监管者 |
|---|---|---|---|---|---|---|
| `sys` | system | hub 内核（注册表、监管、配置、HTTP/NNG 入口） | 独立进程（hub 自己，有真实 pid 与地址） | 无（根） | 全局。挂了即全系统停 | OS/systemd |
| `store` | system | eventlog / project / memory（`role` 区分） | in_process | 随 hub + 探针（可写、延迟） | eventlog 故障：**写入失败即 failed，hub 进入只读/拒绝新任务**；memory 故障：降级（Agent 无记忆继续工作） | sys.0 |
| `scheduler` | system | Scheduler | in_process | 随 hub + 探针（tick 滞后） | 故障则不派新任务，已有任务继续 | sys.0 |
| `gateway` | system | Web + SSE（ChatService 作为内部组件） | in_process（HTTP 线程池） | 随 hub + 探针（连接数、队列） | 故障只影响 UI/API，不影响 Agent | sys.0 |
| `context` | system | Cordis-style Context 根容器 | in_process | 随 hub | 容器 dispose 会级联 dispose 其下插件 | sys.0 |
| `approval` | service | PolicyEngine + ApprovalBroker | in_process → 长期独立进程 | 随 hub + 探针 | **fail-closed**：不可用时，需要审批的工具调用一律挂起或拒绝，绝不放行 | sys.0 |
| `llm` | service | 一个 provider endpoint + 凭证 = 一个单元（模型是 ability） | remote 端点，由 in_process 客户端承载 | **探针型租约**（被动熔断 + 低频 `/models`，不为探活消耗 token） | 429/超时 → degraded；401 → failed；多实例按路由 failover | sys.0 |
| `tool` | service | 工具提供者：`tool.0` = 内置工具目录 + 路由/审计（执行发生在 Agent worker 内）；以后 MCP/沙箱执行器各一个 | in_process（registry）/ subprocess（外部 provider） | 随 hub / 心跳 | 目录损坏 → 工具不可发现；外部 provider 挂 → 其工具标 unavailable | sys.0 |
| `skill` | service | `skill.0` = SkillRegistry（加载、校验、版本、启用范围）。**单个 skill 是目录条目** | declarative + in_process 加载器 | 随 hub | 某个 skill manifest 非法 → 该条目 `invalid`，registry 本身 degraded，不影响其他 | sys.0 |
| `plugin` | service | 一个已加载的外部插件 | in_process（受信）→ subprocess（不受信） | 随宿主；子进程用心跳 | in_process 插件异常由 context 捕获并 dispose；子进程插件崩溃只影响自身 | context.0 |
| `agent` | worker | 一个 Agent 进程，`role` = lead/architect/coder/tester/reviewer/researcher/aggregator/assistant | subprocess | **心跳租约**（TTL ≈ 3× 间隔） | 单 worker 崩溃：任务回收重派；不影响他人 | sys.0（调度由 scheduler.0 驱动） |

### 补充说明

**id 与旧字段兼容：**
- 当前 `kind` 对 worker 实际承担「角色」含义。新模型里 `kind` 变为分类（`agent`），旧值进入 `role`。
- `work_id` 保持不变（如 `coder.1`），同时新增同值别名 `unit_id`。
- 单例服务允许 slug：`store.eventlog`、`approval.0`。**上线前需 grep 现有代码是否对 `.n` 做 `int()`。**

**approval：**
- 它是一个单元，策略规则是数据，审批单（ticket）是该单元管理的记录。
- 抽出时 `abilities` 为 `["policy.evaluate","approval.request","approval.decide"]`。
- 长期应独立进程，因为它是安全边界，不应与可能被 prompt 注入影响的工具路径同进程。

**llm：**
- 无状态，可多实例（`llm.0`、`llm.1`）。
- 配额挂在单元和项目两级：并发、RPM/TPM、按项目预算。
- 凭证只出现 `secret_ref`，descriptor 永不含密钥。

**tool：**
- 「工具」这个词有三层，要分清：
  - **目录条目**（name/schema/risk_level/provider）→ `/api/capabilities?type=tool`。
  - **提供者**（`tool.0`、`tool.mcp-github`）→ 单元。
  - **执行位置**（目前在 worker 内）→ Agent 单元的 `abilities` 里体现 `tool.exec`。
- `tool.0` 在 hub 侧做三件事：目录、参数 schema 校验、审批挂钩与审计。

**skill：**
- skill 是数据包（指令、资源、依赖的工具、触发条件），没有独立运行体，所以不为每个 skill 建单元。
- 如果某个 skill 带可执行脚本或常驻进程，那部分应作为 `tool` provider 或 `plugin` 登记，而不是让 skill 变成假进程。

**context：**
- 这里的 context 是 Cordis 式的 DI/生命周期容器，是插件的宿主。
- 「LLM 提示词窗口装配」是 Agent 使用的库函数。若需要服务化，作为 `context.0` 的 ability `context.assemble`，不另立 kind。

---

## 3. 描述符 Schema 与状态机

### 3.1 字段

| 组 | 字段 | 说明 |
|---|---|---|
| 身份 | `schema`, `unit_id`, `work_id`(兼容别名), `kind`, `role`, `label`, `tier`, `version`, `description` | `kind` 为分类，`role` 为子类型 |
| 作用域 | `scope: {type: global\|project, project_id}` | 项目边界 |
| 运行时 | `runtime: {mode, host_unit, host, pid, module, started_at}` | `mode ∈ in_process\|subprocess\|remote\|declarative`；`pid` 仅 `subprocess/sys` 有；in_process 用 `host_unit` 指向宿主（如 `sys.0`） |
| 端点 | `endpoints: [{name, transport, address}]` | `transport ∈ nng-req\|nng-pub\|http\|inproc`；服务单元可为空数组 |
| 逻辑地址 | `ref: "unit://approval.0"` | 调用方只用 `ref`，由解析器决定走 inproc 还是 NNG。以后拆进程调用方零改动 |
| 状态 | `state`, `reason`, `since`, `activity`, `health: {ok, checked_at, checks[]}` | `activity`（idle/busy）只用于 agent，与生命周期状态分离 |
| 租约 | `lease: {type, ttl_s, interval_s, renewed_at, expires_at}` | `type ∈ heartbeat\|probe\|host` |
| 监管 | `supervisor`, `restart_policy: {mode, backoff_s[], max_restarts, window_s}`, `restart_count`, `singleton`, `critical` | |
| 依赖 | `requires[]`（硬依赖）, `wants[]`（软依赖） | |
| 能力 | `abilities[]`, `provides: {tools: n, skills: n}` | 描述符只放计数，明细走 capabilities |
| 配额 | `limits{}`, `usage{}` | 按 kind 不同 |
| 操作 | `actions[]` | 后端声明允许的操作（`restart/disable/enable/drain/probe`），UI 不硬编码 |
| 兼容 | `transport, rpc_address, events_address, pid, host, status, last_seen, hwinfo, metadata` | 由 `runtime/endpoints/lease` **投影**得到，服务单元为 `null`；`last_seen` 对服务 = 最近一次探针成功时间 |

### 3.2 示例

**in-process 服务单元（approval）：**
```json
{
  "schema": "stackflow.unit/v1",
  "unit_id": "approval.0", "work_id": "approval.0",
  "kind": "approval", "role": null, "tier": "service",
  "label": "审批中心", "version": "0.1.0",
  "scope": {"type": "global", "project_id": null},
  "runtime": {"mode": "in_process", "host_unit": "sys.0", "host": "hub-1", "pid": null,
              "module": "stackflow.hub.approval:ApprovalBroker", "started_at": 1767000000.1},
  "endpoints": [{"name": "rpc", "transport": "inproc", "address": "approval.0"}],
  "ref": "unit://approval.0",
  "state": "ready", "reason": null, "since": 1767000000.5, "activity": null,
  "health": {"ok": true, "checked_at": 1767000120.0,
             "checks": [{"name": "store.eventlog", "ok": true, "latency_ms": 2},
                        {"name": "pending_queue", "ok": true, "detail": "3 pending"}]},
  "lease": {"type": "host", "host_unit": "sys.0", "interval_s": 5, "renewed_at": 1767000120.0},
  "supervisor": "sys.0",
  "restart_policy": {"mode": "on_failure", "backoff_s": [1, 2, 5], "max_restarts": 3, "window_s": 300},
  "restart_count": 0, "singleton": true, "critical": true,
  "requires": ["store.eventlog"], "wants": [],
  "abilities": ["policy.evaluate", "approval.request", "approval.decide"],
  "limits": {"max_pending": 200}, "usage": {"pending": 3},
  "actions": ["probe"],
  "transport": null, "rpc_address": null, "events_address": null, "pid": null,
  "host": "hub-1", "status": "ready", "last_seen": 1767000120.0,
  "hwinfo": null, "metadata": {"policy_version": "2025-01-01"}
}
```

**out-of-process worker（agent）：**
```json
{
  "schema": "stackflow.unit/v1",
  "unit_id": "coder.1", "work_id": "coder.1",
  "kind": "agent", "role": "coder", "tier": "worker", "label": "Coder #1",
  "scope": {"type": "project", "project_id": "p_42"},
  "runtime": {"mode": "subprocess", "host_unit": null, "host": "hub-1", "pid": 4123, "started_at": 1767000010.0},
  "endpoints": [
    {"name": "rpc",    "transport": "nng-req", "address": "ipc:///tmp/sf/coder.1.rpc"},
    {"name": "events", "transport": "nng-pub", "address": "ipc:///tmp/sf/coder.1.pub"}
  ],
  "ref": "unit://coder.1",
  "state": "ready", "activity": "busy", "since": 1767000012.0,
  "lease": {"type": "heartbeat", "ttl_s": 15, "interval_s": 5,
            "renewed_at": 1767000118.2, "expires_at": 1767000133.2},
  "supervisor": "sys.0",
  "restart_policy": {"mode": "on_failure", "backoff_s": [1, 2, 5, 15, 60], "max_restarts": 5, "window_s": 600},
  "restart_count": 1, "singleton": false, "critical": false,
  "requires": ["llm.*", "approval.0", "tool.0"],
  "abilities": ["chat", "tool.exec", "code.edit"],
  "limits": {"max_concurrency": 1, "max_tool_calls_per_task": 80, "task_timeout_s": 1800, "rss_mb": 1024},
  "actions": ["restart", "drain", "stop"],
  "transport": "nng", "rpc_address": "ipc:///tmp/sf/coder.1.rpc",
  "events_address": "ipc:///tmp/sf/coder.1.pub", "pid": 4123, "host": "hub-1",
  "status": "ready", "last_seen": 1767000118.2, "hwinfo": {"cpu": 8, "mem_mb": 16384}, "metadata": {}
}
```

**plugin（省略与上相同的字段）：**
```json
{
  "unit_id": "plugin.3", "kind": "plugin", "role": "tool-provider", "tier": "service",
  "label": "GitHub 集成", "version": "1.2.0",
  "scope": {"type": "global", "enabled_in": ["p_42"]},
  "runtime": {"mode": "subprocess", "pid": 5011},
  "state": "ready", "supervisor": "context.0",
  "requires": ["context.0", "tool.0"],
  "provides": {"tools": 6, "skills": 1},
  "manifest": {"path": "plugins/github/stackflow.plugin.json", "sha256": "…", "permissions": ["net:api.github.com", "secret:github_token"]}
}
```

### 3.3 插件 manifest（最小集）
```json
{
  "id": "github", "name": "GitHub 集成", "version": "1.2.0",
  "entry": {"type": "python", "module": "sf_github:apply"},
  "runtime": "in_process",
  "requires": ["context.0", "tool.0"],
  "contributes": {"tools": ["github.pr.create"], "skills": ["skills/pr-review"], "llm": []},
  "permissions": ["net:api.github.com"],
  "config_schema": "schema.json"
}
```
插件激活时通过 ctx 向 `tool.0`/`skill.0` 注册条目，目录里记录 `provider=plugin.3`。插件 dispose 时按 Cordis scope 机制自动注销，所以工具/skill 的生命周期天然绑定在插件单元上。

### 3.4 状态机

| state | 含义 | 计入在线 |
|---|---|---|
| `registered` | 已声明未启动（配置存在） | 否 |
| `waiting` | 硬依赖未就绪（`reason=dep:store.eventlog`） | 否（不算故障） |
| `starting` | 启动/握手中 | 否 |
| `ready` | 正常 | **是** |
| `degraded` | 可用但有损（探针部分失败、限流、软依赖缺失） | **是**（黄色） |
| `unreachable` | 租约过期，进程可能还活着（**仅 out-of-process**） | 否 |
| `failed` | 确认失败 / 熔断打开，需策略或人工介入 | 否 |
| `draining` | 不再接新活，等待在途完成 | 是（标记） |
| `stopped` | 正常停止 | 否 |
| `disabled` | 被配置禁用 | 否 |

**转移规则：**
```
registered → waiting|starting          (依赖检查)
waiting    → starting                  (依赖全部 ready)
starting   → ready | failed            (握手/首次探针成功 | 超时)
ready      ⇄ degraded                  (探针迟滞：连续 2 次失败→degraded，连续 3 次→failed；连续 2 次成功恢复)
ready|degraded → unreachable           (心跳 TTL 过期；仅 subprocess/remote)
unreachable → ready                    (心跳恢复) | failed (超过宽限期 grace=2×TTL，或进程已退出)
failed     → starting                  (restart_policy 允许，按 backoff)
failed     → failed(sticky)            (超过 max_restarts/window，熔断，等人工 restart)
ready|degraded → draining → stopped    (用户/升级)
any        → disabled                  (配置)
```

**不变量：**
- in-process 单元不会进入 `unreachable`，只会 `degraded/failed`，因为它的存活和 hub 同生共死。
- 状态变化统一经 `registry.set_state(id, to, reason)` 产出事件 `unit.state_changed`，禁止各模块直接改字段。
- 迟滞和防抖防止探针抖动导致 UI 闪烁。

### 3.5 如何不让 `pid/rpc_address` 变成无意义字段
- **事实**放在 `runtime` 和 `endpoints[]`。服务单元写 `host_unit`，端点是 `inproc` 或空，不是假地址。
- 旧字段是只读投影，服务单元为 `null`，旧客户端按空值处理。
- UI「位置」列用 `runtime.mode` 渲染：「hub 进程内（寄居 sys.0）」「独立进程 PID 4123」「远程 api.openai.com」「声明式」。
- `rpc_address` 在详情抽屉中只对 subprocess/remote 展示。
- 调用侧只认 `ref`（`unit://…`）。

---

## 4. 现有组件 → Unit 映射

| 当前组件 | 目标单元 | 阶段 1 | 长期是否拆进程 |
|---|---|---|---|
| hub 主进程 | `sys.0`（真实 pid、HTTP/NNG 地址，不再是纯合成） | 保留并补全字段 | — |
| EventLog | `store.eventlog`（critical） | in-process | 否（可能换底层存储） |
| ProjectStore | `store.project` | in-process | 否 |
| MemoryStore | `store.memory` | in-process | 可能（向量库独立） |
| Scheduler | `scheduler.0` | in-process | 否 |
| Web/SSE（+ ChatService 内部） | `gateway.0` | in-process | 可拆，用于水平扩展 |
| Cordis Context 根 | `context.0` | in-process | 否 |
| PolicyEngine + ApprovalBroker | `approval.0`（critical，fail-closed） | in-process | **是**（安全边界） |
| LLM provider 配置 | `llm.<n>`（每个 endpoint 一个） | in-process 客户端 + remote 端点 | 可选：llm-gateway 进程做集中限流 |
| TOOLS 静态表 + dispatch/审计 | `tool.0`（目录 + 路由）；执行仍在 worker | in-process | 外部 provider（MCP/沙箱）**是** |
| （未实现）skill | `skill.0` | 阶段 2 实现 | 否 |
| （kernel plugin() 概念） | `plugin.<n>`（阶段 2 引入 manifest） | 阶段 1 仅内置模块伪插件可选 | 不受信插件**是** |
| worker 进程 | `agent` 单元（role = 8 种） | 现状，补字段 | 已是 |

### 注册与发现怎么挂到单元

**单元注册（谁登记谁）：**
- **worker**：沿用现有流程（启动→握手→心跳续租）。
- **服务单元**：hub 启动时在 `register_core_units()` 里，按依赖顺序用 `registry.register_service(spec, provider)` 登记。组件只需实现 `health()`，必要时实现 `start/stop`。
- **插件单元**：`PluginLoader` 读取 manifest → 在 `context.0` 下 apply → 登记 `plugin.<n>`。

**能力发现（目录，不是单元列表）：**
```
GET /api/capabilities?type=tool|skill|llm-model&project_id=p_42
→ [{"type":"tool","name":"fs.read","schema":{...},"risk":"low",
    "provider":"tool.0","available":true,"enabled_in":["*"]}]
```
- 目录条目带 `provider`（单元 id）和 `available`。`available` 由 provider 单元的状态推导：provider 非 ready，条目不可用。
- Agent 启动时拉取本项目可见的目录；`unit.state_changed` 事件触发增量刷新。
- 单元的 `provides` 计数由目录聚合得出。

---

## 5. 资源与治理

| kind | 实例 | 配额 / 限制 | 重启策略 | 健康检查 | 项目边界 |
|---|---|---|---|---|---|
| sys | 单例 | — | 不自重启，交 systemd | 自检：线程数、fd、注册表一致性 | 全局 |
| store.* | 单例 | 磁盘水位、写队列长度 | eventlog 不自动重启（`never`，失败即告警）；project/memory `on_failure` 重开连接 | 探针：可写、延迟、磁盘剩余 | 数据按 `project_id` 分区 |
| scheduler | 单例 | 全局并发上限、每项目并发 | `on_failure`（重建 tick 线程） | tick 滞后 < 2×周期 | 每项目队列隔离 |
| gateway | 单例 | 最大 SSE 连接、请求体大小 | `on_failure`（重启 server 线程） | `/healthz` 自探 + 连接数 | 请求带 project 鉴权 |
| context | 单例 | 最大插件数 | `never`，需整体重载 | 容器树一致性 | 项目作用域是子 scope |
| approval | 单例（关键） | pending 上限、审批超时（超时默认拒绝） | `on_failure`，快速重试；**重启期间 fail-closed** | 队列 + 存储可写 | 全局单元，**策略按项目命名空间** |
| llm | 多实例 | 并发、RPM/TPM、**项目级 token 预算** | 不重启进程，**熔断器**：5xx/超时连续 N 次 open，半开探测 | 被动（真实调用）为主，主动 `/models` 每 60s | 预算和可用性按项目配置 |
| tool | 每个 provider 单例 | 每工具并发、超时、输出大小上限 | 内置不重启；外部 provider `on_failure` 带 backoff | schema 校验 + provider 探针 | 目录按项目 allowlist 过滤 |
| skill | 单例 registry | 单 skill 注入 token 上限 | 重新扫描目录即「重载」 | manifest 校验结果 | 全局安装，项目级启用 |
| plugin | 每插件一个 | 权限声明即配额（网络、密钥、文件）；内存/CPU（子进程时） | in_process：dispose + reload；subprocess：`on_failure` + 熔断 | 心跳或 `health()` | 全局安装，`enabled_in[]` 控制项目 |
| agent | 多实例；lead 每项目 1 个 | 单任务并发 1、任务超时、工具调用次数、RSS 上限、token 预算 | `on_failure`，backoff `[1,2,5,15,60]`，10 分钟内最多 5 次，超过 `failed` 熔断；任务回收重派 | 心跳 5s、TTL 15s；`unreachable` 宽限 30s 后 kill + 重启 | **绑定一个 project**，跨项目调用被 dispatcher 拒绝 |

### 依赖顺序（启动正序，停止倒序）
```
L0 sys.0
L1 store.eventlog → store.project, store.memory
L2 context.0, approval.0 (requires eventlog)
L3 tool.0, skill.0, llm.*         (目录/注册表先于贡献者)
L4 plugin.*                       (requires context, 可向 tool/skill 贡献)
L5 scheduler.0
L6 gateway.0                      (最后对外开放；停止时最先关闭)
L7 agent.*                        (requires llm / approval / tool；按需拉起)
```
- `requires` 失败 → 下游 `waiting`，不算故障，也不重启。
- `wants` 缺失 → 下游 `degraded`。例：memory 挂了，agent 继续工作。
- hub 就绪判定：所有 `critical=true` 的单元 `ready`，此时 `gateway.0` 才放行写请求。

### 单例 / 多实例
- 单例：sys、store.*、scheduler、gateway、context、approval、skill.0、tool.0。
- 多实例：llm、plugin、agent、外部 tool provider。
- lead 角色按「每项目一个」约束，由 scheduler 保证。

### 租约规则
- `host`：in-process 单元，租约继承自宿主，随 hub 存亡，只靠探针判定好坏。
- `heartbeat`：子进程，过期 → `unreachable` → 宽限 → 重启。
- `probe`：远程端点，由主动或被动探测维持，无心跳。

---

## 6. API / UI

### 6.1 API

```
GET  /api/units?tier=&kind=&state=&project_id=&include=descriptor|brief
GET  /api/units/{id}                    # 完整描述符 + 最近 20 条状态事件 + 依赖图
POST /api/units/{id}/probe              # 立即探测
POST /api/units/{id}/actions            # {"action":"restart|drain|disable|enable"}，只允许 descriptor.actions 中声明的
GET  /api/capabilities?type=tool|skill
SSE  unit.state_changed  {unit_id, from, to, reason, at}
```

**列表响应：**
```json
{
  "schema": "stackflow.units/v1",
  "generated_at": 1767000120.5,
  "summary": {
    "total": 13, "online": 12, "health": "degraded",
    "by_tier": {
      "system":  {"total": 6, "online": 6},
      "service": {"total": 5, "online": 4, "degraded": 1, "failed": 0},
      "worker":  {"total": 2, "online": 2, "busy": 1}
    },
    "problems": [{"unit_id": "llm.1", "state": "degraded", "reason": "rate_limited"}]
  },
  "units": [ /* 描述符，include=brief 时仅含身份/状态/runtime/lease/abilities */ ]
}
```
- `summary.health`：存在 critical 单元非 ready → `down`；存在任何 degraded/failed → `degraded`；否则 `ok`。
- 向后兼容：`units[]` 里保留旧平铺字段，旧前端不改也能工作，只是会多出新条目。

### 6.2 「在线单元」统计口径
- **online = state ∈ {ready, degraded}**，`draining` 也计入（仍在服务）。
- 状态栏文案：**「单元 12/13 在线」+ 工人徽标「工人 2」**。颜色取自 `summary.health`（绿/黄/红）。悬停展示 system/service/worker 三行分项。
- 不再用一个混合数字表达「工人数」。用户关心有几个能干活的 Agent，应单独显示。

### 6.3 单元页
```
[顶部概览卡]  系统健康 ● 降级(1)   |  系统 6/6  |  服务 4/5  |  工人 2（1 忙碌）
[过滤]  全部 | 仅有问题 | 仅独立进程 | 项目: [全部 ▾]   搜索…

▾ 系统单元（内核基础设施，不可停用）                6/6 在线
   sys.0           hub 内核          ● 就绪   独立进程 PID 811        心跳 —   
   store.eventlog  事件日志          ● 就绪   hub 进程内               探针 3s 前
   store.project / store.memory / scheduler.0 / gateway.0 / context.0 …
▾ 服务单元（可配置的能力服务）                      4/5 在线
   approval.0      审批中心          ● 就绪   hub 进程内   能力: 策略评估 · 审批
   llm.0           OpenAI 兼容       ● 就绪   远程 api.xxx.com
   llm.1           备用 provider     ◐ 降级   限流 429（2 分钟前）
   tool.0          内置工具          ● 就绪   42 个工具
   skill.0         技能库            ● 就绪   已加载 0 个技能
   plugin.*        （无插件）        — 空态: 「尚未安装插件」
▾ 工人单元（Agent 进程）                            2/2 在线
   coder.1         Coder #1          ● 就绪·忙碌  独立进程 PID 4123  心跳 2s 前  项目 p_42
   （无工人时）空态: 「暂无工人。系统服务已就绪，启动一个 Agent 开始协作。 [启动 Agent]」
```
- **表格列：** 名称/ID、类型、状态（色点 + 中文）、运行位置、租约（心跳/探针/随 hub）、能力 chips、项目、操作。
- **详情抽屉：** 描述符 JSON、依赖（requires/wants）、监管者、重启次数、最近状态事件、端点（仅有时显示）。
- **状态文案：** 就绪 / 降级 / 启动中 / 等待依赖 / 失联 / 故障 / 排空中 / 已停止 / 已停用。
- **运行位置文案：** 「hub 进程内」「独立进程 PID n」「远程 host」「声明式（无进程）」。
- **空态必须区分「没有」和「坏了」**：没有 worker 显示引导，服务故障显示红色并带原因。

---

## 7. 分阶段实施路线

### 阶段 1：最小可用（不拆进程，约 1–2 周）
**目标：** 单元页反映系统真实构成，且状态来自真实探针。

**改动模块：**
- 新增 `units/spec.py`：`UnitSpec`/`Descriptor` dataclass 和序列化，含旧字段投影函数。
- 新增 `units/provider.py`：`UnitProvider` 协议（`describe()` / `health()` / `start()` / `stop()`）。`health()` 要求 ≤1s、无副作用、不得持有请求路径的锁。
- 扩展 `UnitRegistry`：`register_service()`、`set_state()`、`snapshot()`（合并 worker 与 service）；worker 与 service 用同一状态机。
- `hub.register_core_units()`：按依赖顺序登记 sys/store×3/scheduler/gateway/context/approval/llm/tool/skill（共约 11 个）。
- 单线程 `HealthTicker`（5s）：逐单元带超时执行探针，迟滞判定后调用 `set_state`，状态变化写 EventLog 并通过 PUB/SSE 发 `unit.state_changed`。EventLog 自身故障时降级为 stderr 日志。
- `/api/units` 返回新 schema，保留旧字段。
- UI：三层分组、概览卡、状态栏口径、运行位置列、空态。
- 各组件补 `health()`，每个 10–30 行：eventlog 可写、provider 配置完整、scheduler tick 滞后等。

**验收标准：**
1. 全新 hub 零 worker 时，`/api/units` 返回 10–12 个单元，`summary` 正确，UI 显示三层。
2. 把 eventlog 目录改只读 → 2–3 个探针周期内 `store.eventlog` 变 `failed`，`summary.health=down`，UI 红色并给出原因。
3. 删除 LLM key → `llm.0` `failed`，其余不受影响。
4. 杀掉 worker（`kill -9`）→ ≤ TTL 后 `unreachable`，宽限后按策略重启，`restart_count` 增加。
5. 旧前端（不更新）仍能渲染单元列表，不报错。
6. `grep` 确认无代码对 id 后缀做 `int()` 假设（或已修复）。

**风险与对策：**
- 探针阻塞拖慢 hub → 超时 + 独立线程，探针中不拿业务锁。
- 假单元回潮 → PR 检查清单要求每个新单元回答准入五问。
- 状态抖动 → 迟滞与最小间隔。
- `kind` 语义变更影响旧调用方 → `role` 承载旧值，灰度期同时输出两者。

### 阶段 2：中期（约 3–5 周）
**目标：** 能力可发现、可治理；插件和 skill 落地。

**改动：**
- **Capability Catalog**：`/api/capabilities`；`TOOLS` 改为向 `tool.0` 注册（带 `provider/risk/schema`）。
- **SkillRegistry（`skill.0`）**：目录扫描、manifest 校验、版本、项目级启用；Agent 按触发条件注入。
- **PluginLoader + manifest**：`plugin.<n>` 作为 `context.0` 的子单元；Cordis scope 绑定注册与注销；仅先支持受信、in-process 插件。
- **Supervisor**：统一 restart_policy（backoff、熔断、window），把 worker 和服务单元的重启从零散逻辑收敛到一处。
- **UnitClient / `ref` 解析器**：`call("unit://approval.0", ...)`，inproc 与 NNG 同一接口；hub 内调用逐步迁移。
- **配额**：llm 的 RPM/TPM/项目预算；agent 的任务超时、工具次数上限。
- **动作 API**：probe/restart/disable/enable/drain。
- 单元详情抽屉与依赖图。

**验收：**
- 安装一个示例插件 → `plugin.N` 出现，贡献的工具出现在目录；停用插件 → 工具自动消失，Agent 下一轮不可见。
- `approval.0` 经 `UnitClient` 调用，把实现替换成 stub 进程不需要改调用方（接口验证）。
- 破坏性 skill manifest 只让该条目 `invalid`。
- 熔断：连续崩溃的 worker 在窗口内停止重启并给出 `failed` 原因。

**风险：**
- 插件 in-process 可能拖垮 hub → 阶段 2 只允许内置和签名插件，不开放第三方。
- 目录一致性（provider 掉线时的 `available` 抖动）→ 用单元状态单向推导，不另存。
- 配额实现复杂 → 先做并发与预算两项，不做精细令牌桶。

### 阶段 3：长期
- `approval` 拆为独立进程，工人与它只经 NNG 通信（安全边界、fail-closed 保持）。
- 外部 `tool` provider：MCP server、沙箱执行器，各一个 subprocess 单元，带资源限制（cgroup/容器）。
- 不受信插件默认子进程 + 权限声明强制。
- 可选 `llm-gateway` 进程，集中限流、缓存、成本核算。
- 远程 worker/多 hub：`runtime.mode=remote`、单元间认证令牌、`host` 字段真正多值。
- 状态事件溯源（unit state 可回放）、热重载、灰度升级（`draining` 编排）。

**风险：** 拆进程带来延迟和部署复杂度。依靠 `ref` 抽象和契约测试（同一套用例同时跑 inproc 和 NNG 实现）控制回归。

---

## 8. 为什么现在只有 sys.0 让人觉得「空」，以及推荐

**原因：**
1. **模型与心智不一致。** UnitRegistry 建模的是「有心跳租约的进程」，而用户心里的「单元」是「StackFlow 的组成部件」。审批、LLM、工具、上下文都在运行，但在这个页面上不可见。
2. **sys.0 是合成的，不传递信息。** 它总是在线，永远不会告诉你哪里坏了。
3. **零 worker 时，「正常」与「坏了」长得一样。** 页面看不出 LLM 是否配置、审批是否可用、事件日志是否可写，只剩一个「1 个在线」，几乎没有诊断价值。
4. **状态栏口径误导。** 「在线单元 1」同时被理解为工人数和系统健康，两者都不准确。

**推荐：扩展服务单元，不只显示进程。**
- 不拆进程也能让页面有意义，因为单元页的价值是**回答「系统由什么组成、哪个部件现在是什么状态」**，不是回答「有哪些进程」。
- 但必须按本方案的准入五问和四条硬规矩约束，避免走向把所有对象都包装成假单元。边界很清楚：有生命周期、有真实健康信号、有契约、有失败域的才是单元；工具、skill、ticket、项目、会话留在目录或数据里。
- 「只看进程」的需求用过滤器「仅独立进程」（`runtime.mode ∈ {subprocess, sys}`）满足，而不是整页的定义。这样两种视角都保留：运维看进程，产品看组成。
- 取舍是：多约 10 个 in-process 单元和一个探针线程，换来可诊断性、统一状态机、以及以后拆进程时调用方零改动。代价是前期要补每个组件的 `health()`，以及要持续维护「不造假单元」的纪律。

### 需要你确认的几点（会影响具体实现）
1. 旧 `kind` 字段是否被前端或其他代码当作「角色」使用？这决定 `role` 迁移是否需要灰度双写。
2. `status` 旧枚举取值是什么？需要映射到新 `state`。
3. 「上下文管理」是否仅指 Cordis 容器？若同时要把提示词窗口装配服务化，则在 `context.0` 增加 `context.assemble` ability 即可，不需要新 kind。
