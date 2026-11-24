# 自治巡检 patrol 交接（2026-11-24）

> 负责人：patrol。范围：`stackflowos/autonomy/patrol.py`（替换 stub）、
> `tests/test_autonomy_patrol.py`（新增）、本文件。未运行任何 git 写操作；
> 未改 `models.py` / `service.py` / `config.py` / `app.py` / `web/api.py`。
>
> 任务：`team_task task-1`（按 `docs/team/autonomy-plan.md` §3.2 实现只读巡检）。

## 1. 改动文件

| 文件 | 状态 | 内容 |
|---|---|---|
| `stackflowos/autonomy/patrol.py` | 替换 stub | `Snapshot`（字段未改）、`collect_snapshot(app, *, now=None)`、`run_checks(snapshot, config)`、12 个检查函数 |
| `tests/test_autonomy_patrol.py` | 新增 | 20 个 pytest 用例：每类检查正/反例、空系统、排序、fingerprint 稳定、FakeApp 采集/异常吞掉/50 上限 |
| `docs/team/patrol.md` | 新增 | 本交接 |

## 2. 接口（硬契约）

```python
@dataclass
class Snapshot:            # 字段与 plan §3.2 完全一致，未新增
    now, health, stats, units, agents, projects, tasks, approvals,
    leases, events, project_health, task_timeout_s, max_workers

collect_snapshot(app, *, now=None) -> Snapshot   # 只读，绝不抛异常
run_checks(snapshot, config) -> list[Finding]    # 纯函数，绝不写状态
```

`collect_snapshot` 读取来源与边界：

- `health=app.health()`、`stats=app.stats()`（异常 → `{}`）；
- `units=app.unit_cards()`、`agents=[a.to_dict() for a in app.supervisor.list()]`；
- `projects=[p.to_dict() for p in app.projects.list()]`；
- `tasks`：**只调用一次** `app.projects.task_list()`，按 `project_id` 分组为
  `to_dict()`（空 key 也会保留，便于诊断无项目任务）；
- `approvals=app.approvals.list(limit=200)` → `to_public_dict()`（不含 preview_full）；
- `leases=app.leases.list(active_only=True)` → `to_dict()`；
- `events=app.eventlog.query(limit=300)` → `to_dict()`；
- `project_health`：仅对 `status in {active, review, degraded}` 且最多 50 个项目调
  `app.project_health(pid)`，单个项目异常跳过，失败的不会拖垮整轮；
- `task_timeout_s` / `max_workers` 取 `app.config.agents.*`，缺失时用默认
  `1800.0` / `6`；`now` 缺省用 `protocol.now_ms()`。

`run_checks` 只用 `config` 的 4 个字段：`task_idle_s`、`approval_age_s`、
`failure_window_s`、`failure_threshold`；其余配置归 dispatcher/guardian。
在岗 Agent 集合固定为 `models.ACTIVE_AGENT_STATUSES`
（`queued/spawning/idle/working/waiting`）。

## 3. 检查项（12 项，全部实现）

| check | 条件 | severity | finding 关键字段 / dispatch |
|---|---|---|---|
| `hub.degraded` | `health.status != "ok"`（空 health 视为未知，不报） | critical | fingerprint(`hub.degraded`) |
| `unit.problem` | 非 worker 单元 `state in failed/unreachable` 或 `status == failed` | critical | `unit_id` |
| `project.degraded` | `project.degraded` 或 `status == "degraded"` | critical | `project_id` |
| `project.no_plan` | `active` 且无任何任务、无在岗 lead | warn | `DispatchHint(spawn, role="lead")`，`project_id` 必填 |
| `project.health` | `project_health[pid].ready` 为 false（缺 ready 跳过） | warn | evidence 带前 3 个 fail/warn check + `next_actions`；`fix` = 第一条 next_action |
| `task.idle` | `todo`、无在岗 agent、deps 全 done 或无 deps、`now-max(updated_at,created_at) > task_idle_s` | warn | `DispatchHint(spawn, role=task.role or "coder", task_id)` + `project_id/task_id` |
| `task.stalled` | `doing`、无在岗 agent、`now-started_at > task_timeout_s`（缺 started_at 退 updated/created） | warn | 无 dispatch（失败域负责重排） |
| `agent.failure` | 窗口内 `blocked/failed` agent 按 `(role, error_code or "UNKNOWN")` 聚合；>= threshold critical，否则 warn（单条也报） | warn/critical | evidence 带 count/role/error_code/window |
| `approval.pending` | `pending` 且 `now-created_at > approval_age_s`，逐条，按等待时间降序最多 20 条 | >10min critical，否则 warn | `project_id/task_id/evidence.request_id` |
| `approval.denial` | 窗口内同类 `capability + principal 前缀(agent-role)` 被拒 >=3；兼容事件 `approval.decided(decision=deny)` 与 `approval.denied/approval.deny` | warn | evidence capability/principal_prefix/count |
| `lease.orphan` | 活跃租约 holder 不在 {units.work_id/unit_id} ∪ {在岗 agents.unit_id/id} | warn | evidence holder/resource/lease_id |
| `events.failure_burst` | 窗口内 `unit.failed/agent.failed/tool.error` 数量 >= threshold | critical | evidence total/by_type |

统一约定：

- 所有 finding 文案为中文、面向人；`detail` 写证据数字；`fix` 一句话下一步。
- `fingerprint` 全部显式 `models.fingerprint(check, 项目/任务/租约等关键 id)`，
  不含 message、时间或计数 → 同一条件跨轮稳定，dispatcher 去重/冷却可直接用。
- 排序：`severity` 从高到低 → `check` → `project_id` → `task_id` → 其余 id
  → fingerprint（`_sorted`），同输入顺序确定。
- 失败隔离：单个检查抛异常只跳过该检查；整份 snapshot 畸形时返回 `[]`；
  `collect_snapshot` 各探针独立 try/except，绝不向 service 抛异常。

## 4. 测试

```bash
cd /home/nihao/work/workspace/StackFlowOS
/tmp/sfo-venv/bin/python -m pytest -q tests/test_autonomy_patrol.py --basetemp=/tmp/pytest-patrol
```

结果：`20 passed`（2026-11-24）。覆盖：

- 12 类 check 各自正/反例（含 idle 的 deps 缺失/未完成、approval 10min 升级与 20 条上限、
  agent 失败聚合与窗口外忽略、lease 三种 holder、failure burst 阈值）；
- 空系统无 finding、畸形字段/缺 config 属性不抛异常；
- 排序（critical → check 字母序）与 fingerprint 跨时间稳定、不同 task 不同 fingerprint；
- FakeApp 上 `collect_snapshot`：一次 `task_list` 分组、`approvals.limit=200`、
  `leases active_only=True`、`eventlog.query limit=300`、health/project_health 异常吞掉、
  只采集 active/review/degraded 且最多 50 个项目、缺服务时用默认值。

未跑全量测试（Lead 统一跑）；未启动真实 worker/hub，未占用 17800 端口。

## 5. 残余风险

- `collect_snapshot` 按契约调用 `app.project_health()`，其内部会做工作区写入探针
  （写后立即删除）。这是既有 health 检查的行为，不是 patrol 写入；被测/受限工作区
  可能因此产生一次 warn/fail，但异常与不可写都只影响该项目的 `project.health` finding。
- 时间戳缺失时的取舍：`agent.failure` / `approval.denial` / `events.failure_burst`
  对“无 ts/无 last_seen”的记录按“窗口内”处理（真实数据总有时间戳），避免缺字段时
  漏报；`task.idle/stalled`、`approval.pending` 无时间戳则跳过（无法判断年龄）。
- `lease.orphan` 的 holder 集合采用“单元卡 + 在岗 agent”并集；若将来有非单元 holder
  （例如人工 capability 租约）长期存在，会产生一次 warn，需要时在 holder 语义上再收敛。
- `project.no_plan` 的 lead 派工依赖 `finding.project_id`；`task.idle` 的 spawn
  依赖 `finding.task_id`。dispatcher 会自行校验项目可写、workspace 存在等前置条件，
  patrol 不重复做这些守卫（避免与 guard 的判定分叉）。
- 每轮最多 20 条 `approval.pending`、最多 50 次 project health 探针，是有意的上界；
  数据量更大时由 UI/事件流承接，不在巡检里无限放大。

## 6. 需要 Lead 接线 / 注意

1. `service.py` 已经在 `_run_patrol_locked` 里调用
   `collect_snapshot(self.app, now=started)` + `run_checks(snapshot, self.cfg)`，
   patrol 侧无需再改；`Snapshot` 字段未动，无兼容问题。
2. 事件日志里现有拒绝事件是 `approval.decided` + `data.decision == "deny"`，
   patrol 已按此实现，并兼容 `approval.denied/approval.deny` 别名。
3. `agent.failure` / `events.failure_burst` / `approval.denial` 共用
   `config.autonomy.failure_window_s` 与 `failure_threshold`；若 Lead 后续新增独立阈值
   配置，只需在 `_build_context` 换读取字段。
4. `ApprovalBroker.list(limit=200)` 返回 pending 优先、再按时间；patrol 在
   `approval.pending` 内部再按等待时长降序截 20 条，避免“最新 20 条”掩盖最老请求。
5. `docs/autonomy.md`、API/UI 侧展示 finding 时直接用 `Finding.to_dict()`
   （`dispatchable` 由模型给出）；`dispatch` 提示里 `project.no_plan` 无 `task_id`，
   `task.idle` 有 `task_id`，前端链接时按需判空。
