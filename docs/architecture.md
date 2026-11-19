# StackFlowOS 架构与实现契约

> 本文是实现的唯一契约。所有模块按这里的接口、数据形状和事件名实现；
> 发现契约不合理时先改本文并通知，不要各自发明。
> 背景判断见 `docs/vision.md`：缺口在 L2 编排层，L0 用 pynng，L1 继承 Cordis 语义。

---

## 0. 一句话

**StackFlowOS 是给 Agent 住的系统**：单位（unit）像进程，项目像用户空间，
Agent 像随项目生灭的工人，权限像手机 App 的授权弹窗，事件流像 `dmesg`。

它不是一个"更好的聊天软件"，而是把 Agent 的操作放进一个**有边界、可观测、
可回滚、可授权**的运行环境。

---

## 1. 分层

```
┌────────────────────────────────────────────────────────────────────┐
│ L3 表现层   聊天对话框 + 工作面板（项目卡片/进度/任务/审批/系统设置）   │
├────────────────────────────────────────────────────────────────────┤
│ L2 编排层   sys 单元：能力目录 · 租约 · 失败域 · 事件流 · 调度         │
│             Agent 生命周期：项目随生随灭 / 管理推进 / 数据汇总         │
│             统一权限：能力 → 风险 → 策略 → 审批 → 授予 → 审计         │
├────────────────────────────────────────────────────────────────────┤
│ L1 内核     Context 树 · Service 注入 · effect/ disposer（逆序）       │
│             emit/parallel/serial/bail/waterfall 事件语义（Cordis 4）   │
├────────────────────────────────────────────────────────────────────┤
│ L0 传输     pynng：REQ/REP（控制面）+ PUB/SUB（事件面）                │
│             地址 = ipc://… 或 tcp://host:port（同一种 wire 协议）      │
└────────────────────────────────────────────────────────────────────┘
```

L0 的契约窄到只有三样东西：`work_id + action + object/data`。
换掉 pynng 不改 L2，换掉 L2 不改内核。

---

## 2. 进程与拓扑

```
                 ┌──────────────────────────────────────────┐
                 │  hub 进程（= sys 单元，work_id: sys.0）    │
                 │  ┌─────────┐  ┌──────────┐  ┌─────────┐  │
  浏览器 ──HTTP──▶│  │ Web/SSE │  │  L2 服务  │  │ 事件日志 │  │
                 │  └─────────┘  └──────────┘  └─────────┘  │
                 │        REP : RPC 服务（单元侧拨入）        │
                 │        SUB : 聚合所有单元的事件             │
                 └───────┬───────────────────────┬──────────┘
                         │ REQ/REP               │ PUB/SUB
              ┌──────────┴─────────┐   ┌─────────┴──────────┐
              │ worker 单元进程      │   │ worker 单元进程      │
              │ work_id: coder.3   │   │ work_id: lead.2     │
              │ 一个 Agent 一个进程  │   │ 一个 Agent 一个进程   │
              └────────────────────┘   └────────────────────┘
```

* **hub 进程**承载 sys 单元、Web 服务、项目/Agent 注册表、审批中心。
* **worker 进程**承载一个 Agent，自己执行本地工具（读写文件、跑命令），
  动作前通过 RPC 向 sys 申请能力。这对应真实 OS 的"进程本地执行 + 内核授权"。
* worker 由 hub 的 `AgentSupervisor` 用 `subprocess` 拉起；进程退出 = Agent 死亡。
* IPC 与 TCP 走同一条 `Envelope`，只是 URL 不同；一把开关切换：
  `transport.mode = ipc | tcp`。

### 2.1 命名

| 对象 | 形状 | 例 |
|---|---|---|
| 单元 work_id | `<kind>.<n>` | `sys.0`, `coder.3`, `lead.2`, `agg.1` |
| 项目 | `p-<n>` | `p-1` |
| 任务 | `t-<n>` | `t-7` |
| 里程碑 | `m-<n>` | `m-1` |
| Agent | `agent.<role>.<n>` | `agent.coder.4` |
| 审批请求 | `apr-<random>` | `apr-3f1a` |
| 租约 | `lease-<random>` | `lease-9c2e` |
| 事件 | 全局 `seq` + `id` | `seq=42` |

work_id 里带 `kind`，是为了让"这是什么进程"从地址上就能读出来（`ps` 的直觉）。

### 2.2 地址

| 用途 | IPC | TCP |
|---|---|---|
| sys RPC | `ipc://<run>/sys.rpc.sock` | `tcp://<host>:<pool+0>` |
| sys 事件入口 | `ipc://<run>/sys.events.sock` | `tcp://<host>:<pool+1>` |
| 单元 RPC | `ipc://<run>/<work_id>.rpc.sock` | `tcp://<host>:<port>` |
| 单元事件出口 | `ipc://<run>/<work_id>.events.sock` | `tcp://<host>:<port>` |

* 默认 `run` = `<state>/run`（不写 `/tmp`，遵守"只在工作区写"）。
* ipc 路径长度必须 < 100 字节；超长时 `AddressAllocator` 用哈希缩短。
* tcp 端口从 `transport.port_pool_start` 起顺序分配，类似 StackFlow 的端口池。
* 单元停止后释放端口与 socket 文件；启动前清理陈旧 ipc 文件。
* 同一 state 目录只允许一个 hub：`<state>/run/hub.lock` 用 `flock` 独占，
  第二个 hub 启动即报 `UNAVAILABLE`。否则旧 hub 关闭时的 `allocator.cleanup`
  会删掉新 hub 已经绑定的 socket。

---

## 3. 线协议

`stackflowos/protocol.py` 已实现，是唯一格式来源：

```json
{
  "v": 1,
  "kind": "rpc",
  "id": "msg-3f1a",
  "src": "coder.3",
  "dst": "sys",
  "action": "approval.request",
  "object": null,
  "data": {"capability": "fs.write", "scope": {"path": "/abs/x"}},
  "ts": 1763,
  "ok": true,
  "error": null,
  "seq": 0,
  "trace": null
}
```

* `kind`: `rpc` | `reply` | `event`；其他值解码即报错。
* `reply` 复用请求的 `id`；`ok=false` 时 `error={"code","message","detail"}`。
* `event` 的 `data` 内层固定为
  `{"type","source","ts","data","project_id"?}`，`seq` 由 hub 事件日志统一编号。
* 消息上限 2MB；超出拒收。
* pynng 保留消息边界，不需要额外分帧。
* 重试语义：RPC 超时由客户端重试一次（**复用同一 `id`**）；hub 按
  `(src, id)` 做 in-flight 合并与 5 分钟结果缓存，保证 `agent.spawn` 这类
  非幂等动作只会执行一次。事件是**至少一次**，消费者按 `seq` 去重；
  控制面操作本身仍应幂等（例如 `task.claim` 返回冲突而不是重复认领）。
* REP 服务端每一路 lane 设置 5s 发送超时；对端 REQ 进程消失导致 lane 卡死时
  自动回收重建，避免 8 条 lane 被慢泄漏耗尽。

### 3.1 RPC 动作清单（sys 侧）

| action | 请求 data | 返回 |
|---|---|---|
| `sys.ping` | `{}` | `{ok, ts}` |
| `sys.info` | `{}` | 版本、uptime、transport、seq |
| `sys.hwinfo` | `{}` | `{cpu, mem, load, disk}` |
| `sys.stats` | `{}` | 单元/Agent/项目/租约/事件计数 |
| `unit.allocate` | `{kind, label?, project_id?}` | `{work_id, rpc_address, events_address, sys_events_address, transport}` |
| `unit.addresses` | `{work_id}` | 同上（worker 启动时获取 supervisor 预先分配的地址） |
| `unit.register` | 单元完整信息（见 5.1） | `{work_id, registered_at}` |
| `unit.heartbeat` | `{work_id, stats?}` | `{server_ts}` |
| `unit.release` | `{work_id, reason?}` | `{released}` |
| `unit.list` | `{kind?, project_id?, status?}` | `[...]` |
| `unit.kill` | `{work_id, reason}` | `{accepted}` |
| `lease.acquire` | `{resource, holder, ttl_s, renewable?, preemptible?, priority?, metadata?}` | `Lease` |
| `lease.renew` | `{lease_id, holder, ttl_s?}` | `Lease` |
| `lease.release` | `{lease_id, holder?}` | `Lease` |
| `lease.list` | `{holder?, resource?}` | `[Lease]` |
| `approval.request` | `{principal, capability, scope, reason, preview, project_id?, task_id?, ttl_s?}` | `ApprovalRequest` |
| `approval.status` | `{request_id}` | `ApprovalRequest` |
| `approval.use` | `{grant_id}` | `{ok}` |
| `approval.list` | `{status?, project_id?}` | `[ApprovalRequest]` |
| `rule.list` | `{active_only?}` | `[Rule]` |
| `project.create` | `{name, goal, created_by, auto_lead?}` | `Project` |
| `project.list` | `{status?}` | `[Project]` |
| `project.get` | `{project_id}` | `Project` |
| `project.update` | `{project_id, patch}` | `Project` |
| `project.archive` | `{project_id}` | `{project, agents_stopped, approvals_canceled, leases_released}` |
| `project.delete` | `{project_id, confirm, purge_workspace?}` | `{deleted, name, tasks_removed, agents_stopped, workspace_purged}` |
| `task.create` | `{project_id, title, description?, role?, priority?, deps?, created_by?}` | `Task` |
| `task.list` | `{project_id?, status?, assignee?}` | `[Task]` |
| `task.get` | `{task_id}` | `Task` |
| `task.claim` | `{task_id, agent_id}` | `Task`（被占则 CONFLICT） |
| `task.update` | `{task_id, patch}` | `Task` |
| `task.artifact` | `{task_id, artifact}` | `Task` |
| `task.note` | `{task_id, text, author?}` | `Task` |
| `task.check` | `{task_id, item_id?, text?, done?}` | `Task` |
| `agent.register` | `{role, project_id, unit_id, task_id?, model?}` | `Agent` |
| `agent.update` | `{agent_id, patch}` | `Agent` |
| `agent.finish` | `{agent_id, summary}` | `Agent` |
| `agent.spawn` | `{role, project_id, task_id?, reason?}` | `Agent`（hub 拉起进程） |
| `agent.kill` | `{agent_id, reason?}` | `{ok}` |
| `agent.list` | `{project_id?, status?}` | `[Agent]` |
| `event.append` | 事件 `{type, data, project_id?, source?}` | `{seq}` |
| `event.since` | `{seq, limit?, project_id?}` | `[EventRecord]` |
| `memory.get` / `memory.set` | `{project_id, key, value?}` | `{value}` |
| `chat.send` | `{text}` | `{message_id}`（仅本地；worker 不需要） |

未知 action → `BAD_REQUEST`；handler 抛 `SfoError` → 对应 code。

### 3.2 单元侧 RPC（hub → worker）

| action | data | 说明 |
|---|---|---|
| `unit.pause` | `{reason}` | 停止领取新任务，当前步骤做完 |
| `unit.resume` | `{}` | 恢复 |
| `unit.kill` | `{reason}` | 优雅退出（dispose 之后退出） |
| `unit.status` | `{}` | 当前 agent/task/step |

worker 必须实现这四个动作。

---

## 4. L1 内核（已实现，不要改语义）

`stackflowos/kernel/` 对照 Cordis 4：

| Cordis | 本实现 | 语义 |
|---|---|---|
| `ctx.provide(name, impl)` | `ctx.provide` | 服务注册，随 context 销毁 |
| `ctx.get(name)` | `ctx.get/require` | 沿父链查找；`isolate(name)` 截断 |
| `ctx.effect(fn)` | `ctx.effect` | fn 立即执行，返回 disposer；逆序执行；可重复 dispose |
| `ctx.on(name, cb)` | `ctx.on` | 监听器随 context 销毁 |
| `ctx.emit` | `ctx.emit` | 同步广播，异常隔离 |
| `ctx.parallel` | `ctx.parallel` | 全部并发，聚合异常 |
| `ctx.serial` / `ctx.bail` | `ctx.serial/bail` | 顺序执行，遇 bail 停止 |
| `ctx.waterfall` | `ctx.waterfall` | 环绕中间件，不调 `next()` 即否决 |
| `ctx.plugin(p, cfg)` | `ctx.plugin` | 子 context；服务发布到挂载点 |
| `Service` 基类 | `kernel.Service` | 构造即注册 |

附加：`ctx.on_any(cb)` 观察所有事件（事件日志/审计用），
`ctx.log` 是带层级名的 stdlib logger。

**纪律**：模块通过 `Service` 子类或 `ctx.provide` 暴露自己；
清理逻辑一律走 `ctx.effect`，不要手写 `try/finally` 藏在业务里。

---

## 5. L2 编排

### 5.1 单位（UnitRegistry）

```python
@dataclass
class UnitInfo:
    work_id: str; kind: str            # sys|lead|coder|reviewer|tester|researcher|reviewer|agg
    label: str; project_id: str | None
    pid: int | None; host: str; transport: str
    rpc_address: str; events_address: str
    status: str                        # online|suspect|offline|quarantined
    abilities: list[dict]              # [{"name":"fs.write","scope":"project"}]
    hwinfo: dict
    started_at: int; last_seen: int
    restart_count: int = 0
    metadata: dict = {}
```

`UnitRegistry`：`register / heartbeat / release / get / list / sweep`。
`sweep(now, offline_after)` 依据最后一次心跳把 online→suspect→offline；
每次状态迁移写事件并调用失败域。能力目录 = 在线单位的 `abilities` 并集，
回答"现在能承诺什么"，不做假：只列真实注册过的能力。

### 5.2 租约（LeaseManager）

```python
@dataclass
class Lease:
    id: str; resource: str; holder: str
    ttl_s: int; renewable: bool; preemptible: bool; priority: int
    created_at: int; expires_at: int
    status: str                        # active|released|expired|preempted
    metadata: dict
```

* `resource` 是字符串命名空间：`unit:<work_id>`、`project:<id>:lead`、
  `cap:<name>`、`file:<abs>`、`slot:<work_id>:<n>`。
* 同一 resource 同时只能有一个 active 租约；冲突 → `Conflict`，错误里带持有者。
* `preemptible` 且新请求 `priority` 更高时可抢占；被抢占者收到事件。
* `sweep(now)` 把过期租约置 `expired` 并发 `lease.expired`。
* 单位死亡时 `release_holder(work_id)` 释放其全部租约。
* **AI 敢接长任务的物理前提**：拿租约时要能回答"我占到什么时候"。
* **unit 身份租约**：每个 worker `unit.register` 时自动获取 `unit:<work_id>`
  的 renewable 租约（TTL = max(30s, 3×offline_after)），心跳续期；unit 退出、
  心跳超时或失败域回收时由 `release_holder` 释放。它让"我占着这个身份/地址到 T"
  成为可查询事实，也是 UI 状态栏"租约"计数的来源。

### 5.3 失败域（FailureManager）

```python
@dataclass
class FailurePolicy:
    kind: str                          # 单元 kind
    action: str                        # restart|fail|ignore
    max_restarts: int = 3
    window_s: float = 300.0
    quarantine_after: int = 5
```

* 每秒 tick：`registry.sweep` → 对新 offline 单位：
  释放租约 → 发 `failure.detected` → 按策略重启（budget 内）或隔离；
  超过 quarantine 阈值 → `unit.quarantined`。
* 重启由 `AgentSupervisor.restart(unit)` 回调完成；不允许无限重启。
* 项目承载者（lead）死亡：项目 `degraded=true`，发 `project.degraded`；
  lead 重新注册时清除 degraded。其他工人死亡只重置任务，不降级项目。

### 5.4 周期性汇总

`App` 内还有一个聚合循环：按 `agents.aggregator_interval_s`（默认 240s）扫描
active/review/degraded 项目，若项目有任务且当前没有在跑的 aggregator，就拉起一个
（受 max_workers 约束）。demo 在项目进入 review 后也会立刻跑一次，确保 brief 不为空。

### 5.5 调度（Scheduler）

`select(ability, project_id=None, exclude=())`：在在线单位里按
"能力匹配 → 同项目优先 → 租约负载最低"打分返回。
只做建议，不替 Agent 做决定；调用方仍要显式 `agent.spawn` 或 `lease.acquire`。

### 5.6 事件日志（EventLog）

* append-only JSONL：`<state>/events/events.jsonl`，超过 8MB 轮转。
* 全局单调 `seq`；`since(seq)` 支持重放（UI 刷新/断线重连）。
* 内存保留最近 5000 条；更早从文件回读。
* `subscribe()` 返回 `asyncio.Queue`；慢消费者丢弃最旧记录，不阻塞写。
* hub 每秒跑一次维护循环：`approvals.sweep()` 把过期 pending 置 `expired`（fail
  closed），`leases.sweep()` 回收过期租约。Agent 死亡/被杀/项目归档时，其名下
  pending 审批会被系统以 `deny` 关闭，避免“人走单还挂着”。
* `EPHEMERAL_TYPES = ("chat.delta", "stream.chunk")` 不落盘、不占 seq 持久位置
  （仍递增 seq，只是不写文件；重启后 seq 从文件最大值继续）。

### 5.7 事件目录（稳定契约）

```
sys.booted / sys.ready / sys.shutdown / sys.config.updated
unit.allocated / unit.registered / unit.online / unit.suspect / unit.offline
unit.released / unit.quarantined / unit.restarted / unit.paused / unit.resumed
lease.acquired / lease.renewed / lease.released / lease.expired / lease.preempted
project.created / project.updated / project.archived / project.progress / project.degraded / project.completed
project.deleted / project.delete.completed / project.archive.completed / project.purge.failed
task.created / task.updated / task.claimed / task.started / task.review
task.completed / task.blocked / task.artifact / task.note
agent.spawned / agent.registered / agent.working / agent.waiting / agent.finished
agent.failed / agent.terminated / agent.heartbeat / agent.step
approval.requested / approval.decided / approval.auto / approval.expired / approval.used / approval.ui_decided
operator.action
rule.created / rule.updated / rule.deleted
tool.invoked / tool.result / tool.denied / tool.failed
failure.detected / failure.action
chat.message / chat.delta / chat.typing
memory.updated / demo.seeded
```

事件 `data` 必须自包含：UI 不查库也能渲染一张卡片。
金额/路径/命令等敏感预览放在 `preview` 字段，UI 原样展示。

---

## 6. 统一权限（security/）

### 6.1 能力表

| capability | risk | scope 键 | 说明 |
|---|---|---|---|
| `fs.read` | low | path | 读文件 |
| `fs.list` | low | path | 列目录 |
| `fs.write` | medium | path, bytes | 写文件（workspace 外升 high） |
| `fs.delete` | high | path | 删文件（默认不可用） |
| `shell.exec` | high | cwd, command | 执行命令（危险模式升 high） |
| `net.fetch` | medium | url | 出站 HTTP |
| `llm.chat` | low | model | 调模型 |
| `project.create` | low | — | 建项目 |
| `project.update` | medium | project_id | 改项目 |
| `project.archive` | medium | project_id | 归档项目、停止其 Agent |
| `project.delete` | high | project_id, purge_workspace | 永久删除项目记录与工作区 |
| `task.manage` | low | project_id | 建/改任务 |
| `agent.spawn` | medium | project_id, role | 拉起工人 |
| `agent.kill` | high | agent_id | 杀掉工人 |
| `unit.control` | high | work_id | 暂停/杀死单元 |
| `sys.config` | high | section | 改系统配置 |
| `approval.decide` | high | request_id | 放行/拒绝审批 |
| `data.read` | low | project_id | 读项目数据/事件 |
| `data.write` | medium | project_id | 写项目记忆 |

风险升级规则（`risk_of`）：
* 目标路径不在 `<state>/projects/<project>/workspace` 内 → 至少 high；
* shell 命令命中 `DANGEROUS_PATTERNS`（`rm -rf /`、`sudo`、`mkfs`、`dd if=`、
  `curl|sh`、`chmod 777 /` 等）→ high；
* URL 非 http/https → high；私网地址 → medium 起。

### 6.2 策略（PolicyEngine）

```python
@dataclass
class Rule:
    id: str; effect: str               # allow|deny
    capability: str                    # 精确或 "*"
    principal: str                     # fnmatch，如 "agent.*" / "coder.3" / "human:operator"
    scope: dict                        # 模式，见下
    risk_max: str = "high"             # allow 只允许到该风险
    expires_at: int | None = None
    uses_left: int | None = None
    note: str = ""
    source: str = "rule"               # rule|grant
    created_by: str = "operator"
    created_at: int
```

`scope` 匹配模式：
* `path_prefix`：规范化绝对路径前缀（`..` 解析后再比）；
* `path_glob`：fnmatch；
* `command_regex`：`re.search`；
* `domain`：URL host 等于或以 `.domain` 结尾；
* `url_prefix`：字符串前缀；
* `project_id` / `role` / `agent_id`：精确；
* `equals`：`{key: value}` 逐项精确。

判定顺序：**deny 优先**（任意匹配 deny → deny）→ allow（risk ≤ risk_max）→
默认策略（low 按 `auto_allow_low`，medium/high 按 `medium_default/high_default`）。
每次判定都写审计（`approval.auto` 或 `approval.requested`）。

### 6.3 审批（ApprovalBroker）

```python
@dataclass
class ApprovalRequest:
    id: str; principal: str; capability: str; scope: dict
    risk: str; reason: str; preview: str
    project_id: str | None; task_id: str | None
    status: str        # pending|approved|denied|expired|canceled
    mode: str | None   # rule|grant|auto|once|session|always
    grant_id: str | None
    created_at: int; expires_at: int; decided_at: int | None
    decided_by: str | None; note: str = ""
```

* `request()`：先查策略；allow → 直接 `approved`（`mode=rule/grant/auto`）；
  deny → `denied`；ask → 入队 `pending`，发 `approval.requested`，等待人工。
* `decide(id, decision, mode)`：`mode` ∈ `once`（一次）、`session`（1h）、
  `always`（转成永久规则）。批准即生成/复用 grant，写 `approval.decided`。
* `poll()`：过期 pending → `expired`，发事件；Agent 看到 expired 必须放弃动作。
* `use(grant_id)`：`once` 用一次即失效；记录 `approval.used`。
* 没有人在线时，pending 到期自动拒绝（**fail closed**）。
* 所有 `request/decide/use` 都进事件日志，审计页只读事件流。

### 6.4 系统助手 = 操作员代理（特权但全审计）

系统助手（对话框里的 AI）不是普通工人，而是**操作员的代理**：人通过对话下指令，
所以它的工具调用直接执行，不进入审批队列。`session/operator.py` 维护一个封闭的
管理工具目录（项目/任务/Agent/单元/审批/规则/配置/租约/项目文件/记忆），
每一次调用追加 `operator.action` 事件，参数递归脱敏（key/token/password/… → `***`），
结果截断后入流。它可以 `approval.decide`（含 `approval.allow` / `approval.deny`
别名），也可以删除项目、改配置、写规则。

边界：
* 工人 Agent 仍然走 `approval.request`，助手身份不能替它们自动放行工作动作；
* 助手没有 `shell.exec`，文件读取被限制在项目 workspace 内，不能碰宿主任意路径；
* 删除项目在人工 UI 仍要求输入项目名；助手路径由对话指令触发，但保留完整审计；
* 这是有意的信任选择：把"人机交互入口"等同于"操作员"。若未来接不可信输入，
  应把 operator 工具降级为 approval-gated，或在会话上加签名确认。

### 6.5 执行模型

工具执行在 worker 本地，顺序固定：

```
resolve scope → risk_of → sys.approval.request → poll 直到终态
→ 校验 grant → 执行（路径/命令/cwd 再校验一次）→ approval.use → 发 tool.result
```

sys 不代替 worker 执行文件操作，因为真正干活的是设备侧的进程；
sys 负责授权与审计。这是能力委托（capability delegation），
和"内核给进程发 fd"同一类语义。**已知限制**：worker 是可信的；
若未来接外部不可信设备，需要把工具执行也搬到 sys 侧沙箱。

---

## 7. 项目与任务

```python
@dataclass
class Task:
    id: str; project_id: str; title: str; description: str
    status: str            # todo|doing|review|done|blocked
    priority: int          # 0..3
    role: str | None       # 期望的执行角色
    assignee: str | None   # agent id
    created_by: str
    deps: list[str]        # task ids
    artifacts: list[dict]  # {"type","path","title","ts"}
    checks: list[dict]     # {"id","text","done","note"}
    notes: list[dict]      # {"ts","author","text"}
    created_at/updated_at: int
    started_at/finished_at: int | None
    error: str | None

@dataclass
class Project:
    id: str; name: str; goal: str
    status: str            # planning|active|review|done|archived|degraded
    created_by: str; created_at/updated_at: int
    tags: list[str]; color: str
    milestones: list[dict]
    brief: str             # 汇总 Agent 写的进展简述
    workspace: str         # 绝对路径
    degraded: bool
```

* 进度：`done=1.0, review=0.85, doing=0.45, todo=0, blocked=0`，按任务平均，
  输出 0–100 整数；没有任务时为 0。
* 任务状态机：`todo → doing → review → done`；`doing/review → blocked`；
  `blocked → todo`；`review → todo`（打回）。非法迁移报 `ValidationError`。
* `claim` 是原子操作：只有 `todo` 且无 assignee 才能被认领，否则 `CONFLICT`。
* 归档：先停止该项目全部 Agent、取消其 pending 审批、释放项目租约，
  再把状态置 `archived`；工作区保留。
* 删除：`project.delete` 需要 `confirm` 等于项目名（或 id）；先停 Agent、取消审批、
  释放租约，再从 store 删除项目及其任务；`purge_workspace=true` 时校验路径后
  `rmtree(<projects_dir>/<id>)`。事件流保留 `project.deleted` 记录。
* 持久化：`<state>/state/projects.json`（全量、原子替换）；
  ID 计数器在 `<state>/state/counters.json`。
* 每次变更发事件；卡片投影 `project.card()` 由 app 拼上 agents 统计后返回。
  `card.milestones` 是**数量**（不是数组），`card.total_tasks` 由 app 补上；
  UI 对两种形状都兼容。

---

## 8. Agent 模型

### 8.1 角色

| role | 生命周期 | 工具域 | 职责 |
|---|---|---|---|
| `lead` | 与项目同生灭 | 全项目（除高风险） | 拆解目标、建任务、派工、推进、汇总 |
| `coder` | 随任务生灭 | fs.*, shell（项目内） | 写代码/文档，跑自检 |
| `architect` | 随任务生灭 | fs.write, data.read | 设计，写设计文档 |
| `researcher` | 随任务生灭 | net.fetch, fs.write | 调研，写调研结论 |
| `tester` | 随任务生灭 | shell.exec, fs.read | 跑测试，写测试报告 |
| `reviewer` | 随任务生灭 | fs.read, data.read | 审产物，通过/打回 |
| `aggregator` | 周期性，项目内 | data.read, project.update | 数据汇总，更新 brief |
| `assistant` | hub 内常驻 | 低风险 + 派工 | 对话框里的系统助手 |

* "随项目生灭"由 `AgentSupervisor.archive(project)` 保证：归档即杀光 subprocess。
* lead 随项目创建自动拉起（`agents.auto_spawn_project_lead`）。
* 每个 Agent 一个进程（= 一个 unit），`unit.kind` 等于 role。
* Agent 状态：`spawning → idle → working → waiting → done|failed|dead`。

### 8.2 Agent 运行循环（worker 进程）

```
connect(sys.rpc) → unit.allocate → bind own rpc/events → unit.register
→ agent.register → 领取任务（lead/worker 不同）
→ loop: brain.step() → tools.run()（带审批） → task.update → agent.update
→ agent.finish → unit.release → dispose → exit
```

* 心跳：每 `agents.heartbeat_s` 一次 `unit.heartbeat`。
* 循环上限 `agents.max_steps`（默认 32）。lead 在容量满/工人尚在启动时不会直接
  结束，而是冷却 → 重新 `task.list` → 重试派工，直到没有 todo/doing 才交回人工评审。
* pause/kill 通过单元侧 RPC 实现；kill 走优雅 dispose。
* 崩溃由 hub 发现（心跳超时）并按失败域处理。

### 8.3 工具

`agents/tools.py` 注册工具：名称、capability、参数 schema、执行函数。
工具一律经过 `ToolExecutor`：

* 路径限制在项目 workspace（`resolve()` 后校验）；`..` 逃逸直接拒绝；
* shell 用 `asyncio.create_subprocess_shell`，cwd 限制、超时、
  环境变量白名单（不传 `SFO_LLM_API_KEY`）；
* 输出截断到 20KB，完整输出写 `artifacts/<tool>-<ts>.log`；
* 每次调用发 `tool.invoked` / `tool.result`。

工具清单：`fs.read/list/write/delete`、`shell.exec`、`net.fetch`、
`task.update/artifact/note`、`project.update`、`agent.spawn/kill`、
`event.query`、`memory.get/set`、`unit.list`。

### 8.4 大脑（ai/）

```python
class Brain(Protocol):
    async def next_action(self, view: AgentView) -> Action  # tool call or finish
    async def chat(self, messages: list[dict]) -> str
```

* `MockBrain`：确定性、离线、可测试。按 role 生成真实产物
  （写文件、跑命令、写评审），**不是假的 UI 动画**。
* `LLMBrain`：OpenAI 兼容 `/chat/completions`，工具调用用 JSON 协议：
  `{"tool":"fs.write","args":{...}}` / `{"finish":{"summary":"..."}}`。
  用 stdlib `urllib`，不引入第三方依赖。
* 助手对话：provider=mock 时用规则意图（查状态/建项目/派工/审批提示）；
  provider=openai 时走模型，但工具仍受审批约束。

---

## 9. Web 与 UI

### 9.1 HTTP API（同源，无鉴权，只绑定 127.0.0.1）

统一响应：`{"ok": true, "data": ...}` / `{"ok": false, "error": {...}}`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 存活 |
| GET | `/api/state` | 快照：sys/units/projects/agents/approvals/rules/events/chat/stats/config |
| GET | `/api/events/stream?since=<seq>` | SSE，事件流 + 心跳 |
| POST | `/api/chat` | `{text}` → `{message_id}`，回复走 SSE |
| GET | `/api/chat` | 历史消息 |
| GET/POST | `/api/projects` | 列表 / 创建 `{name, goal}` |
| GET/PATCH | `/api/projects/{id}` | 详情 / 更新（status, brief） |
| POST | `/api/projects/{id}/archive` | 归档（杀 Agent） |
| GET/POST | `/api/projects/{id}/tasks` | 任务列表 / 创建 |
| PATCH | `/api/tasks/{id}` | 更新状态/说明/优先级 |
| GET | `/api/approvals` | 待审批 + 历史 |
| POST | `/api/approvals/{id}/decide` | `{decision: allow|deny, mode: once|session|always, note}` |
| GET/POST | `/api/rules` | 规则列表 / 新建 |
| DELETE | `/api/rules/{id}` | 删规则 |
| GET | `/api/units` | 单元列表 |
| POST | `/api/units/{id}/kill` | 杀单元 |
| GET | `/api/agents` | Agent 列表 |
| POST | `/api/agents` | `{role, project_id, task_id?}` 手动派工 |
| DELETE | `/api/agents/{id}` | 杀 Agent |
| GET/PATCH | `/api/config` | 系统设置 |
| GET | `/api/console` | 审计/事件（按类型过滤） |
| POST | `/api/system/complete/{project_id}` | 人工确认项目完成 |

SSE 帧：`data: <EventRecord JSON>\n\n`；每 15s 一行 `: ping\n\n`。
前端断线用 `since=lastSeq` 重连补齐。

### 9.2 UI 结构（手机 OS 隐喻）

* **状态栏**：系统名、host、在线单元数、租约数、待审批徽标、时钟。
* **主界面**：左侧/主区 **对话框**（首屏就是能说话），
  右侧/下区 **工作面板**（项目卡片、Agent 花名册、实时事件）。
* **Dock**：项目、Agent、单元、审批、事件、设置。
* **App 窗口**：从底部升起的面板；可关闭、可切换。
* **项目卡片**：名称、状态、进度环、当前任务、在岗工人、最近活动、待审批数。
  点击进入详情：任务看板（todo/doing/review/done）、产物、Agent、事件、里程碑。
* **审批中心**：待审批卡片（风险色、capability、scope、preview、理由、TTL 倒计时），
  一键 `允许一次 / 允许 1 小时 / 总是允许 / 拒绝`；规则编辑器；审计时间线。
* **设置**：常规、网络与传输（ipc/tcp、单元地址）、AI（provider/model）、
  安全（默认策略、审批 TTL、规则）、存储（路径、事件日志大小）、关于。
* **视觉**：深色、圆角、细边框、低饱和渐变、等宽字体显示地址/id；
  动效只用于状态变化，不做装饰性动画。

---

## 10. 配置（`stackflowos/config.py` 已实现）

优先级：内置默认 < `<state>/config.json` < 环境变量。
环境变量：`SFO_ROOT`、`SFO_STATE_DIR`、`SFO_WEB_PORT`、`SFO_TRANSPORT_MODE`、
`SFO_LLM_PROVIDER`、`SFO_LLM_BASE_URL`、`SFO_LLM_MODEL`、`SFO_LLM_API_KEY`。

默认：ipc、web 17800、mock LLM、心跳 2s、离线 8s、审批 TTL 300s。
所有运行期文件都在 `<repo>/.stackflowos/` 下。

---

## 11. 目录与所有权

```
stackflowos/
  protocol.py errors.py config.py cli.py app.py
  util/            jsonio/logging（完成）
  kernel/          context/events/service（完成）
  transport/       address.py nng.py           [A]
  l2/              registry.py leases.py failure.py scheduler.py eventlog.py   [B]
  security/        capabilities.py policy.py approvals.py   [B]
  projects/        models.py store.py          [C]
  agents/          models.py roles.py tools.py brain.py runner.py supervisor.py api.py [C]
  ai/              llm.py prompts.py           [C]
  session/         chat.py                     [C/lead]
  app.py           sys 单元装配（RPC 动作表 + 服务接线）           [lead]
  units/           worker.py（每 Agent 一进程）                    [lead]
  session/         chat.py（对话、流式、助手）                       [lead]
  web/             server.py api.py            [D/lead]
  webassets/       index.html css/ js/         [D]
tests/
```

---

## 12. 测试

* 全部用 stdlib `unittest`（不装 pytest）：`python3 -m unittest discover -s tests -v`。
* 每个模块自带单测；异步用 `unittest.IsolatedAsyncioTestCase`。
* 集成测试：真实起 hub + worker 子进程（ipc 与 tcp 各一遍），
  验证：注册/心跳/租约/审批/任务完成/事件落盘/Web API/SSE；
  以及 SIGKILL 一个工人后：offline 检测 → 租约释放 → 失败域重启替代。
* 当前 `python3 -m unittest discover -s tests -t .` 为 256 个测试。
* 测试临时目录必须放在 `<repo>/.stackflowos/tmp/`，不写系统临时目录。

## 13. 非目标（这一版不做）

* 真正的多设备发现（mDNS/组网）：预留 TCP 地址即可，不实现发现协议。
* 强沙箱（容器/命名空间）：用路径白名单 + 命令策略，文档写清限制。
* 多用户与鉴权：默认单机单操作员，绑定 127.0.0.1。
* 大规模调度：单 hub，租约语义优先于吞吐。


---

## 14. 实现补充与已知偏差（落地时）

1. **Worker 自注册**：supervisor 先 `unit.allocate` 再拉起进程；worker 启动后用
   `unit.addresses` 取回地址并绑定，随后 `unit.register`。因此 unit 的地址由
   hub 分配，进程只负责监听，跨设备时地址里的 host 由 `advertise_host` 决定。
2. **快速退出**：worker 可能在 supervisor 的 200ms 轮询前就 register→finish→exit。
   hub 在 `unit.register` 时 `note_online`，spawn 不再误判超时。
3. **非幂等动作去重**：RPC 重试复用 `Envelope.id`，hub 合并 in-flight 请求并缓存
   结果，避免 `agent.spawn` 被超时重试执行两次。
4. **Replacement 绑定**：`supervisor.restart` 在拉起新进程前把 agent 的 `unit_id`
   改成新 work_id，使 worker 的 `agent.register` 复用同一 roster 记录。
5. **项目级评审**：planner 的最后一个 "评审" 任务没有任务内产物；reviewer 会检查
   兄弟任务是否都已关闭，若是则按项目工作区做整体验收，而不是误判 blocked。
6. **演示规则**：`demo` 只是替操作员预置一组项目范围内的 allow 规则
   （fs.read/list/write、无害 shell、agent.spawn、project.update、data.*），
   不是绕过审批；项目外写入仍是 pending，等待人工。
7. **MockBrain 是开环的**：它按角色/步数决定动作，不解析工具返回值（避免把
   演示写成假智能）。真实模型用 `LLMBrain`，动作协议是 JSON tool call。
8. **Chat 助手**：`mock` provider 下是确定性意图（状态/建项目/审批/帮助）；
   `openai` provider 下把实时状态放进 system prompt，工具调用仍走审批口。
9. **Web**：SSE 断线按 `since` 续传；静态页支持 `?no-sse=1` 快照模式（截图/测试用）。
10. **测试目录**：IPC socket 受 ~100 字节路径限制，集成测试把 state_dir 放在
    浅层临时目录；正常仓库路径 `<repo>/.stackflowos/run` 足够短。
