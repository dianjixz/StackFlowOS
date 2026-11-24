# guard 交接：自治派工 dispatcher + 代理审批 guardian

日期：2026-11-24 / 分支：工作区（未做任何 git 写操作）

## 改动文件

| 文件 | 说明 |
|---|---|
| `stackflowos/autonomy/dispatcher.py` | 替换 stub，实现 `Dispatcher`：off/tasks/agents 三模式、fingerprint 去重+冷却、长期重试上限、小时配额、在岗 Agent 上限、项目/工作区守卫、建任务、spawn、失败隔离、每轮动作上限、state/rate 记账 |
| `stackflowos/autonomy/guardian.py` | 替换 stub，实现 `Guardian` / `Verdict`：pending/worker/capability/风险/项目/角色/路径/域名/人类否决/小时配额/最小年龄围栏，off/observe/enforce 三模式，只走 `app.decide_approval`，Conflict 记 skip |
| `tests/test_autonomy_dispatch.py` | 新建：Dispatcher 契约测试（fake app，不启动 hub/worker） |
| `tests/test_autonomy_guardian.py` | 新建：Guardian 契约测试（fake app） |
| `docs/team/guard.md` | 本交接文档 |

未改 `models.py` / `service.py` / `config.py` / `app.py` / `web/api.py` 或任何他人文件；
未执行 git add/commit/push；未占用或重启 17800 的 hub。

## 接口

```python
MAX_ACTIONS_PER_RUN = 5
MAX_ATTEMPTS_PER_FINDING = 5

class Dispatcher:
    def __init__(self, app, config, state: dict, *, now=now_ms)
    async def apply(self, findings: list[Finding]) -> list[dict]

class Verdict:  # dataclass: request_id, decision(allow|observe|skip), reason,
                # capability, principal, risk, project_id

class Guardian:
    def __init__(self, app, config, state: dict, *, now=now_ms)
    def evaluate(self, request: dict) -> Verdict
    async def apply(self, requests: list[dict] | None = None) -> list[dict]
```

`now` 可传函数或 int；缺省用 `stackflowos.protocol.now_ms`。
config/state 缺字段、finding 非 dispatchable、app 缺 supervisor/policy 时不抛异常。

### Dispatcher 行为要点

- `dispatch_mode="off"` → `[]`；`tasks` 只建任务（绝不 spawn）；`agents` 支持
  `kind="task"`（建任务后同轮 spawn）和 `kind="spawn"`（直接 spawn）。mode 不符返回 skipped。
- 去重：`state["dispatch"][fingerprint]` 非 error 记录且 `now-ts < dispatch_cooldown_s` → skip。
  error 记录按 plan 字面语义可在下轮重试（受下面的重试上限与配额约束）。
- **长期重试上限（Lead 追加要求）**：`prior["attempts"] >= MAX_ATTEMPTS_PER_FINDING(5)`
  → skip，reason 为 `retry budget exhausted (5 attempts); needs a human or a new finding`。
  成功、失败动作都计数（task 创建、spawn 各记一次 attempt）。
  旧 `state/autonomy.json` 里的记录没有 `attempts` 字段时按 0 处理（向后兼容）。
- 配额：`state["rate"]["tasks"/"spawns"]` 1 小时窗口；达到 `dispatch_max_*_per_hour`
  → skip；0 = 禁止。每次“尝试”都占配额（失败也算，避免反复撞上限）。
- 在岗 Agent 上限：从 `state["dispatch"]` 记录里的 `agent_id` + `supervisor.list()`
  状态（`ACTIVE_AGENT_STATUSES`）统计自治派出的在岗 Agent，`>= dispatch_max_agents` → skip；0 = 禁止。
- 项目守卫：项目存在、status ∈ {active, review, degraded}、workspace 是已存在目录、
  `workspace_mode != "attached-ro"`；archived/done 等 skip。
- 建任务：`app.projects.add_task(project_id, title, description, role=role or None,
  priority<=2, created_by="autonomy:governor")`；description 末尾追加
  `\n\n[autonomy:<fingerprint>] <finding.title>`；成功把 task_id 写入 state。
  失败（含 ValidationError）记 `status="error"`、reason 截断 300 字，继续处理后续 finding。
- 派工：`await app.spawn_agent(role or "coder", project_id, task_id)`；异常一律记
  `status="error"` 不抛出。agents 模式下 task 创建成功但 spawn 失败时任务保留，
  下一轮按同一 fingerprint 复用已有 task_id 重试，不会重复建任务。
- 返回 record 形状：`{"fingerprint","action","status","project_id","task_id",
  "agent_id","role","reason","ts"}`，按 ts 升序；skipped 也返回。
- 每轮最多 5 个实际动作（task 创建与 spawn 各算一个）。

### Guardian 行为要点

- `apply()` 无参时自己取 `app.approvals.list(status="pending", limit=50)` 并转
  `to_public_dict` 形状；`requests` 参数供测试注入。
- `approve_mode="off"` → `apply()` 返回 `[]`（`evaluate` 仍可用）；`observe` 只产
  `decision="observe"` 记录、不调 decide、不消耗速率；`enforce` 且 `allow` 才
  `await app.decide_approval(request_id, decision="allow", mode="once",
  by="autonomy:guardian", note=reason)`。
- Conflict（人已处理）→ `decision="skip"`、不重试、不消耗速率；其他 SfoError/异常同样
  skip + reason，绝不抛出。
- 围栏顺序：pending → principal 以 `agent-` 开头 → capability 在
  `approve_capabilities` 且不在 `HARD_DENY_CAPABILITIES` → risk ≤ `approve_risk_max`
  （risk 取 `request["risk"]`，缺省按 high fail-closed）→ project_id/project 状态
  （非 archived/done）/`approve_projects` → capability 专项 → 人类否决冷却 → 小时配额
  → `approve_min_age_s`。
- capability 专项：
  - `fs.write`：`normalize_path(path, cwd)` 必须 `relative_to(project.policy.workspace_for)`
    或项目 workspace；不在 `.git` 下；attached-ro skip。
  - `agent.spawn`：`scope.role` ∈ `approve_roles`；同项目在岗 Agent <
    `approve_max_agents_per_project`（0 = 禁止）。
  - `shell.exec`：需 `approve_shell`、`command` 匹配任一 `approve_shell_patterns`
    （`re.fullmatch`）、cwd 在 workspace 内、无 `; | & > < \` $(`/换行控制符、
    `approve_risk_max=="high"`。
  - `net.fetch`：URL host 等于 `approve_domains` 项或为其子域。
  - `fs.delete`/`sys.config`/`unit.control`/`project.*`/`agent.kill`/`approval.decide`
    即使被配置也永远 skip（HARD_DENY）；其他未列入白名单的 capability skip。
- 人类否决冷却：`approvals.list(status="denied", limit=200)` 中 `decided_by` 以
  `human` 开头、principal 去掉尾部 `-数字` 后相同、capability 相同且
  `decided_at` 在 `approve_deny_cooldown_s` 内 → skip。
- 速率：`state["rate"]["approvals"]` 1 小时窗口 < `approve_max_per_hour`，仅 enforce
  allow 成功后 append。
- 状态：`state["approvals"]` 最多 100 条，同 `request_id+decision` 覆盖更新。
- 返回 record：`{"request_id","decision","reason","capability","principal","risk",
  "project_id","ts"}`，按 ts 升序。

## 测试

命令：

```bash
cd /home/nihao/work/workspace/StackFlowOS
/tmp/sfo-venv/bin/python -m pytest -q tests/test_autonomy_dispatch.py \
  tests/test_autonomy_guardian.py --basetemp=/tmp/pytest-guard
```

结果：`62 passed, 12 subtests passed`（2026-11-24，python 3.11+，`/tmp/sfo-venv`）。

覆盖：dispatcher 三模式、建任务字段/来源标记、agents 建任务同轮 spawn、直接 spawn、
去重/冷却、error 重试、**6 次 apply 只 spawn 5 次的重试预算**、旧 state 无 attempts 兼容、
task/spawn 配额、Agent 上限、同 task 在岗去重、项目守卫（archived/done/planning/
attached-ro/无 workspace/不存在）、建任务与 spawn 异常隔离、非 dispatchable、坏 config、
每轮 5 动作上限、缺 supervisor。
guardian：fs.write 允许/越界/`.git`/相对 cwd/attached-ro/缺 path、agent.spawn 允许/
角色/项目人数上限、shell.exec 未授权/全通过/控制符/正则不匹配/cwd 越界/缺 cwd/risk
上限、net.fetch 精确与子域/空域名、HARD_DENY、principal/status/risk/project/
approve_projects 围栏、人类否决冷却与过期/其他 capability/policy 否决、小时配额与 0、
min_age、observe/off、Conflict/其他异常、pending limit=50、requests 注入、state 去重上限。

## 残余风险 / 需 Lead 注意

1. **error 重试与冷却的语义**：按 plan §3.3 与任务书字面（“非 error 记录且未过冷却 →
   skip”），error 记录在冷却内也会在下一轮重试。现已用 `MAX_ATTEMPTS_PER_FINDING=5`
   兜底，不会无限循环；若 Lead 希望 error 也严格走 `dispatch_cooldown_s`，需在契约层
   拍板（少量改动即可）。
2. **attempts 是“动作次数”而非“轮次”**：agents 模式一个 task finding 一轮会产生
   task+spawn 两条动作，attempts 一次 +2。因此 5 次预算约等于 4 次 spawn 重试。
   若 Lead 希望按“轮次”计数，需要改 `_remember` 调用粒度（会动到现有测试断言）。
3. **observe 模式事件噪声**：guardian 每轮对仍 pending 的请求都返回 `observe` record，
   service 会对非 skip record 写 `autonomy.approval` 事件，因此 observe 模式下同一请求
   每轮都会产生一条事件；state 已去重，只是事件流会重复。若不可接受，建议 service
   对 observe 去重或调低 observe 事件。
4. **guardian 的项目状态围栏只排除 archived/done**（按任务书），因此 planning 项目上的
   fs.write/agent.spawn 仍可能被放行；dispatcher 的派工守卫更严（仅 active/review/
   degraded）。attach-ro 只在 guardian 的 `fs.write` 分支拦截；attached-ro 项目上的
   agent.spawn 由 dispatcher 侧拦截。
5. **fs.write 缺 cwd 的相对路径**会按进程 cwd 解析，通常落在 workspace 外 → skip
   （fail-closed）；测试覆盖了带 cwd 的相对路径。
6. 本项目测试全部使用 fake app，未起真实 hub/worker；真实集成（`app.spawn_agent`、
   `app.decide_approval`、policy workspace resolver）由 Lead 在集成/全量测试中验证。
7. 未触碰 `state/autonomy.json` 的持久化/裁剪逻辑（service 所有）；dispatcher/guardian
   只按约定写 `state["dispatch"]`、`state["rate"]`、`state["approvals"]`，并做好缺键
   setdefault，旧 state 可平滑加载。
8. 未跑全量测试、未做 git 提交，等待 Lead 统一验收。
