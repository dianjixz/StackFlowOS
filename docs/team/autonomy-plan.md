# 自治闭环实施契约（2026-11-24）

> 目标：让 StackFlowOS 像一个 OS 那样**自己巡一遍系统、自己派工、自己批审批**。
> 三者不是三个按钮，而是一个可审计、有预算、能急停的闭环：
>
> `巡检 findings → 派工 tasks/agents → 代理审批 approvals → 新事件/新状态 → 下一轮巡检`
>
> 本文是并行开发的硬契约。先读本文，再读 `docs/architecture.md` §6/§8、
> `stackflowos/autonomy/models.py` 与 `stackflowos/config.py` 的 `AutonomyCfg`。
> 与代码冲突时以代码/测试为准，并报告 Lead。

## 0. 设计原则（不可妥协）

1. **默认关闭**：`autonomy.enabled=false` 时没有循环、没有派工、没有代审批。
2. **人类先授权，系统后执行**：自审批只在 `approve_mode=enforce` 且落在
   `approve_capabilities / risk_max / projects / roles / shell_patterns` 围栏内才发生。
   永久性破坏动作（删除、杀进程、改配置、改规则、删项目）**永不**自动批准。
3. **一切可急停**：`enabled=false` 立即停止下一轮；单轮内的动作有每轮上限。
4. **幂等与冷却**：同一 fingerprint 不重复派工；失败有冷却；每小时的建任务/派工/
   审批次数有上限。
5. **可审计**：每次巡检、派工、代审批都写事件（§4），`approval.decided` 的
   `decided_by="autonomy:guardian"`，`autonomy.dispatched` 记录 role/task/project。
6. **失败隔离**：任何一步异常只记 `autonomy.error` 并继续；绝不让自治拖垮 hub。
7. **不绕过审批**：guardian 只调用 `app.decide_approval(...)`（正常审批口），
   不写规则、不伪造 grant；dispatcher 的 `app.spawn_agent` 是系统运营动作，写审计。

## 1. 文件所有权（硬边界）

| 所有者 | 独占文件 |
|---|---|
| **lead** | `stackflowos/config.py`、`stackflowos/extensions.py`、`stackflowos/autonomy/models.py`、`stackflowos/autonomy/service.py`、`stackflowos/session/operator.py`、`stackflowos/cli.py`、`docs/*`（除 `docs/team/*`）、`README.md` |
| **patrol** | `stackflowos/autonomy/patrol.py`、`tests/test_autonomy_patrol.py` |
| **guard** | `stackflowos/autonomy/dispatcher.py`、`stackflowos/autonomy/guardian.py`、`tests/test_autonomy_dispatch.py`、`tests/test_autonomy_guardian.py` |
| **api** | `stackflowos/autonomy/api.py`、`tests/test_autonomy_api.py` |
| **front** | `stackflowos/webassets/**`、`tests/test_front_autonomy.py` |

规则：只改自己的文件；需要跨界先 `send_message` 给 `lead`。**teammate 不跑 git 写操作**，
不改 `app.py`、`web/api.py`（路由走扩展 `routes()`）。只跑自己的测试文件，
加 `--basetemp=/tmp/pytest-<name>`；全量由 Lead 跑。

## 2. 配置（`config.autonomy`，Lead 已落地）

```python
enabled: bool = False
interval_s: float = 120.0          # 巡检周期
on_start: bool = True              # 启动后先巡一次
report_keep: int = 20              # 历史报告保留条数
task_idle_s: float = 120.0         # todo 任务无人认领多久算“闲置”
approval_age_s: float = 120.0      # 待审批超过多久算“等人”
failure_window_s: float = 3600.0   # 失败聚合窗口
failure_threshold: int = 3         # 同一类失败超过 N 次升级为 critical
dispatch_mode: str = "off"         # off | tasks | agents
dispatch_max_agents: int = 2       # 同时由自治派出的在岗 Agent 上限
dispatch_max_tasks_per_hour: int = 10
dispatch_max_spawns_per_hour: int = 4
dispatch_cooldown_s: float = 900.0
approve_mode: str = "off"          # off | observe | enforce
approve_risk_max: str = "medium"
approve_capabilities: list[str] = ["fs.write", "agent.spawn"]
approve_projects: list[str] = []   # 空 = 所有 active/review/degraded
approve_roles: list[str] = ["architect","coder","tester","reviewer","researcher"]
approve_max_per_hour: int = 20
approve_min_age_s: float = 0.0
approve_deny_cooldown_s: float = 86400.0
approve_max_agents_per_project: int = 4
approve_shell: bool = False
approve_shell_patterns: list[str] = []
approve_domains: list[str] = []
```

配置有严格校验（枚举、上下界、capability 白名单、shell 正则可编译）；UI 通过
`PATCH /api/autonomy/policy` 修改，服务端走 `app.update_config` 持久化并热生效。

## 3. 模块接口（按此实现）

### 3.1 `models.py`（Lead 已落地）

- `SEVERITY_ORDER = {"info":0,"warn":1,"critical":2}`
- `DISPATCH_MODES`、`APPROVE_MODES`
- `fingerprint(*parts) -> str`：稳定 16 位 sha256 前缀
- `DispatchHint(kind: "spawn"|"task", role, task_id, title, description, priority)`，`to_dict()`
- `Finding(check, severity, title, detail, fingerprint, project_id, task_id, agent_id, unit_id, fix, evidence, dispatch, detected_at)`，`to_dict()` 增加 `dispatchable`
- `PatrolReport(...)`，`to_dict()` / `from_dict()`

### 3.2 `patrol.py`（patrol 所有）

```python
@dataclass
class Snapshot:
    now: int
    health: dict
    stats: dict
    units: list[dict]              # app.unit_cards()
    agents: list[dict]             # supervisor.list() -> to_dict()
    projects: list[dict]           # Project.to_dict()
    tasks: dict[str, list[dict]]   # project_id -> Task.to_dict()
    approvals: list[dict]          # approvals.list(limit=200) -> to_public_dict()
    leases: list[dict]
    events: list[dict]             # 最近 300 条，含 type/ts/source/data/project_id
    project_health: dict[str, dict]# project_id -> app.project_health()
    task_timeout_s: float
    max_workers: int

def collect_snapshot(app, *, now: int | None = None) -> Snapshot: ...
def run_checks(snapshot: Snapshot, config) -> list[Finding]: ...
```

只读：绝不写项目/任务/审批/事件。`collect_snapshot` 用 `app.projects.list()`、
`app.projects.task_list()` 一次分组；`project_health` 只对 `active/review/degraded`
且最多 50 个调用，异常吞掉。`run_checks` 必须对空数据/缺字段稳健。

检查项（最小值，可加）：

| check | 条件 | severity | dispatch |
|---|---|---|---|
| `hub.degraded` | `health.status != "ok"` | critical | 无 |
| `unit.problem` | 非 worker 单元 state in failed/unreachable，或 status failed | critical | 无 |
| `project.degraded` | project.degraded 或 status=="degraded" | critical | 无 |
| `project.no_plan` | active 且无任务、无在岗 lead | warn | spawn lead |
| `project.health` | `project_health.ready` 为 false；finding 带前 3 个失败项/fix | warn | 无（仅报告） |
| `task.idle` | todo、无在岗 agent、deps 全 done、`now - max(updated_at,created_at) > task_idle_s` | warn | spawn task.role or coder |
| `task.stalled` | doing、无在岗 agent、`now-started_at>task_timeout_s` | warn | 无（失败域负责重排） |
| `agent.failure` | 窗口内 blocked/failed agent 按 (role,error_code) 聚合超阈值 | warn/critical | 无 |
| `approval.pending` | pending 且 `now-created_at > approval_age_s`（逐条，最多 20） | warn（>10min 为 critical） | 无 |
| `approval.denial` | 窗口内同类 capability/principal 被拒 >=3 | warn | 无（提示补规则） |
| `lease.orphan` | 活跃租约 holder 不在在岗单元里 | warn | 无 |
| `events.failure_burst` | 窗口内 `unit.failed/agent.failed/tool.error` >= failure_threshold | critical | 无 |

在岗 agent 状态集合：`{"queued","spawning","idle","working","waiting"}`。
finding 的 `fingerprint` 必须稳定（用 `fingerprint(check, 关键 id...)`，不要拼 message/时间）。
`title` 面向人（中文），`detail` 说明证据数字，`fix` 一句话给下一步。
dispatchable 的 finding 必须给出 `DispatchHint`。

### 3.3 `dispatcher.py` + `guardian.py`（guard 所有）

```python
class Dispatcher:
    def __init__(self, app, config, state: dict, *, now=now_ms): ...
    async def apply(self, findings: list[Finding]) -> list[dict]: ...
```

- 只处理 `finding.dispatch` 非空且 `config.dispatch_mode != "off"`。
- `tasks` 模式：只允许 `kind=="task"`（创建任务），绝不 spawn。
- `agents` 模式：`kind=="task"` 创建任务后（同轮）派工；`kind=="spawn"` 直接派工。
- 去重：`state["dispatch"][fingerprint]`；已有非 error 记录且未过冷却 → skip；
  同一 task_id 已有在岗/排队 agent → skip。
- 配额：`state["rate"]["tasks"]` / `["spawns"]` 时间戳列表，1 小时窗口；
  `dispatch_max_agents` 统计“自治派出且在岗”的 agent（记录里带 agent_id，查 supervisor）。
- 建任务：`app.projects.add_task(project_id, title, description, role=..., priority=..., created_by="autonomy:governor")`；
  description 末尾加 `[autonomy:<fingerprint>]`，便于人核对来源。
- 派工：`await app.spawn_agent(role, project_id, task_id)`；失败捕获 `SfoError/Conflict`，
  记录 `status="error"`+`error`，不抛出。
- 每次成功动作写一条 record 并 `await emit`？不额外依赖 emit：service 会从返回 records
  写事件。返回 record 形状：
  `{"fingerprint","action":"task"|"spawn","status":"ok"|"skipped"|"error","project_id","task_id","agent_id","role","reason","ts"}`。
- 每轮最多 5 个动作（硬编码 `MAX_ACTIONS_PER_RUN = 5`）。

```python
@dataclass
class Verdict:
    request_id: str
    decision: str          # allow | observe | skip
    reason: str
    capability: str
    principal: str
    risk: str
    project_id: str | None

class Guardian:
    def __init__(self, app, config, state: dict, *, now=now_ms): ...
    def evaluate(self, request: dict) -> Verdict: ...
    async def apply(self, requests: list[dict] | None = None) -> list[dict]: ...
```

- `apply()` 自己取 `app.approvals.list(status="pending", limit=50)`；`evaluate` 对单条纯判断。
- `off` → 不评估；`observe` → 只记录 `decision="observe"`，不动审批；
  `enforce` 且 allow → `await app.decide_approval(req_id, decision="allow", mode="once",
  by="autonomy:guardian", note=reason)`；`Conflict` 说明被人抢先处理，记 skip 不重试。
- 硬拒绝 capability：`fs.delete`、`shell.exec`（除非 `approve_shell` + 正则 fullmatch +
  cwd 在 workspace）、`net.fetch`（除非命中 `approve_domains`）、`unit.control`、
  `sys.config`、`project.*`、`agent.kill`、`approval.decide`。
- principal 必须是 worker（以 `agent-` 开头）；`project_id` 必须存在于 app.projects，
  状态不能 archived/done；`fs.write` 的 path 必须 `Path.resolve().relative_to(workspace)`
  且不在 `.git/` 下；attached-ro 项目跳过。
- `agent.spawn`：role 在 `approve_roles`，同一项目在岗 agent < `approve_max_agents_per_project`。
- 人类否决冷却：最近 `approve_deny_cooldown_s` 内同一 (principal 泛化 role, capability,
  scope 关键字段) 被人类 deny 过 → skip（尊重人的决定）。
- 速率：`state["rate"]["approvals"]`，1 小时窗口 ≤ `approve_max_per_hour`。
- 记录进 `state["approvals"]`（最多 100 条，按 request_id+decision 去重）+ rate 时间戳。
- 返回 record：`{"request_id","decision","reason","capability","principal","risk","project_id","ts"}`。

### 3.4 `service.py`（Lead 所有）

扩展对象 `AutonomyService`：loop、状态持久化 `state/autonomy.json`、`run_patrol()`、
`status()`、`configure(patch)`、`units()`、`health()`、`routes()`；
`build(app)` 返回单例并挂到 `app.autonomy`。单轮顺序：
collect → run_checks → 事件/持久化 → dispatcher.apply → guardian.apply → 更新计数/报告。

## 4. 事件与 API

事件（`source="autonomy"`，finding 事件带 project_id）：
`autonomy.patrol.started`、`autonomy.patrol.completed`、`autonomy.finding`（仅新 finding）、
`autonomy.dispatched`、`autonomy.approval`、`autonomy.error`、`autonomy.policy.changed`。

API（扩展路由，写操作要求 operator）：

| Method | Path | 返回 |
|---|---|---|
| GET | `/api/autonomy`、`/api/autonomy/status` | `{enabled, policy, last_run, next_run_in_s, running, counts, findings, report, history, dispatches, approvals, guardrail}` |
| POST | `/api/autonomy/patrol` | body `{"dry_run":bool?}`；跑一轮并返回 report |
| PATCH | `/api/autonomy/policy` | body 为 autonomy 局部配置；校验+持久化+热生效，返回 status |

响应统一 `{ok:true,data:...}`；错误用 `stackflowos.errors`（400/403/409）。

## 5. UI（front 所有）

新增 Dock 应用「自治」（`#autonomy`）：
- 顶部：开关（enabled）、上次巡检/下轮倒计时、critical/warn/info 计数、运行中状态、
  「立即巡一遍」按钮。
- 三个分区：巡检发现（severity pill、check、project/task 链接、fix）、自主派工
  （action/role/project/task/status）、代理审批（decision/capability/principal/risk/reason）。
- 策略卡：dispatch_mode、approve_mode、approve_risk_max、capabilities 多选、
  配额数字、保存（PATCH policy）。viewer 只读。
- 语言：zh 主、en 回退；i18n 加键；`tests/test_front_i18n.py` 必须仍过。
- 无构建步骤（原生 ES module），移动端可用；图标复用或新增 `i-autonomy` symbol。

## 6. 验收

1. 新增相关测试全过：`python3 -m pytest -q tests/test_autonomy_*.py tests/test_front_autonomy.py`。
2. 不破坏基线：不改别人文件；新增依赖只用 stdlib。
3. 失败/关闭 fail-closed；任何危险动作拒绝并写 reason。
4. 交接：在 `docs/team/<name>.md` 写改动文件、接口、测试命令/结果、残余风险。
5. **不要停在一半**：实现+测试+自验三件套齐了再报告；做不完要写清边界。
