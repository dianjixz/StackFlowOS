# AGENT 工作流交接（2026-11-24）

> 负责人：agent。范围：`stackflowos/agents/**`（新增 `queue.py`、`hotreload.py`、
> `workspace_git.py`、`git_api.py`、`extension.py`、`sandbox.py`）、`tests/test_agent_*.py`、
> `tests/test_supervisor.py`、`docs/team/agent.md`；按 Lead 追加授权只额外修改
> `stackflowos/units/worker.py`（热更新 + worker 自身沙箱接线）。未运行任何 git 写操作。
>
> 任务：`team_task task-3`（优先级队列、超时/工具次数、热更新、git 分支隔离、
> reviewer fs.write、沙箱接线、capability.call 工具桥）。

## 1. 改动文件

| 文件 | 状态 | 内容 |
|---|---|---|
| `stackflowos/agents/queue.py` | 新增 | `DispatchQueue` 有界优先级队列、`QueueEntry`、`compute_priority`（task priority/urgency + role + 显示顺序）、持久化快照 |
| `stackflowos/agents/supervisor.py` | 改 | max_workers 满时排队（`spawn(..., priority=None)`）；`drain_queue` 自动补位；`cancel_queued`；`queue_snapshot`；`on_config` 热推送；`reap_stalled` 超时回收/重派；超时 watchdog；task deps 门禁；`agent.queued/dequeued/configured/timeout` 事件；roster 持久化 queue/limits |
| `stackflowos/agents/models.py` | 改 | 新增 `queued` 状态（不占 worker 名额、无进程） |
| `stackflowos/agents/runner.py` | 改 | `agents.task_timeout_s` 墙钟超时（`asyncio.wait_for`，含 brain/tool 等待）；`max_tool_calls_per_task` 上限；`reload_limits` 热更新；attached-rw 任务开始时建 git 分支、结束时提交；超时/超限事件带 `timeout`/`limit` 原因 |
| `stackflowos/agents/hotreload.py` | 新增 | `public_runtime_config`（无密钥）、`apply_runtime_config`（就地更新 `agents`/`llm`，routes 字段级合并且保留已有 key）、`ConfigReloader`（注册 `unit.configure` + `config_reload_s` 轮询 `sys.config.get`） |
| `stackflowos/agents/workspace_git.py` | 新增 | `WorkspaceGit`：ensure_repo / task_branch / commit_task / diff_summary / rollback；ref/branch 校验、拒绝工作区嵌套在外层 repo、rollback 先建 `sfo-rollback/*` 备份分支、不 push/不删分支 |
| `stackflowos/agents/git_api.py` | 新增 | `GET /api/projects/{id}/git`、`POST .../git/rollback` 的 handler；rollback 仅 attached-rw + operator |
| `stackflowos/agents/extension.py` | 新增 | `build(app)` 扩展：queue 路由 + git 路由；给 supervisor 装 `task_lookup`/`task_requeue` |
| `stackflowos/agents/sandbox.py` | 新增 | `security.sandbox` import-guard 桥：`wrap_shell_command`（用 sec 的 `wrap_command`→`SandboxPlan`，含 Landlock `preexec_fn`）、`apply_process_sandbox`（worker 自身 Landlock）、`probe`、失败原因只记一次 |
| `stackflowos/agents/tools.py` | 改 | `shell.exec` 接入沙箱（返回 `sandbox` 元数据，失败回退原命令）；新增 `capability.call` 工具（hub RPC 桥） |
| `stackflowos/agents/roles.py` | 改 | reviewer 增加 `fs.write`（§14 缺陷）+ 允许写 `review/<id>-review.md` 提示；lead/architect/coder/tester/reviewer/researcher 增加 `capability.call` |
| `stackflowos/units/worker.py` | 改（Lead 授权） | 启动早期安装 `unit.configure` + 轮询；创建 runner/executor 后挂载；按 `security.sandbox` 对 worker 进程调 `restrict_current_process`（写白名单）并把报告写进 `unit.register` metadata / `unit.status`；退出时停 reloader |
| `tests/test_agent_queue.py` | 新增 | 队列顺序/上限/补位/取消/持久化/deps/优先级/reaper |
| `tests/test_agent_limits.py` | 新增 | 超时、工具次数、`reload_limits`、热更新 payload/apply/轮询、`supervisor.on_config` 推送 |
| `tests/test_agent_workspace_git.py` | 新增 | repo/branch/commit/diff/rollback、非法 ref、外层 repo 拒绝、runner 集成（真实临时 git repo） |
| `tests/test_agent_extension.py` | 新增 | `build(app)+routes()`、WebAPI 路由注册、queue/git handler、rollback 权限 |
| `tests/test_agent_reviewer.py` | 新增 | reviewer `fs.write` 可用且路径仍受限；`capability.call` 参数/错误映射/审批 |
| `tests/test_agent_sandbox.py` | 新增 | off/缺失模块/失败降级、argv+Landlock preexec、真实 Landlock 子进程写限制 |
| `tests/test_supervisor.py` | 改 | 旧「满员抛 Conflict」断言改为「满员排队 + queue_max 才 Conflict」 |
| `docs/team/agent.md` | 新增 | 本文件 |

## 2. 交付 1：优先级派工队列

### supervisor API（向后兼容）

```python
await supervisor.spawn(role, project_id, task_id=..., reason=...,
                       priority=None)        # 旧调用不变，新增 priority 关键字
supervisor.queue_snapshot(project_id=None)    # GET /api/agents/queue 数据
supervisor.cancel_queued(queue_id)            # 也接受 agent_id（便利）
await supervisor.drain_queue()                # 手动补位（测试/管理）
await supervisor.on_config(full_config)       # app.update_config 已调用
await supervisor.reap_stalled(now=None)       # 超时回收/重派
await supervisor.close()                      # 停 watchdog（建议 Lead 在 shutdown 调）
```

- `spawn` 在「运行中 worker（spawning/idle/working/waiting）>= agents.max_workers」时
  不再抛 `Conflict`，而是注册一个 `status="queued"`、`unit_id=None` 的 Agent 并入队，
  立即返回（API 返回 queued 状态）。`agents.queue_max` 满时仍抛 `Conflict`。
- `drain_queue` 在 worker 结束/失败/被 kill/project archive/`max_workers` 调大时被调度，
  按 `(-priority, seq)` 取队头启动；`priority` = 显式参数 > task priority(0..3)/urgency
  + role 权重 + 显示顺序，范围 0..100（`agents/queue.py:compute_priority`）。
- `task_lookup` 由扩展接线（`projects.task_get`）时，派工会检查 `task.deps`：
  未全部 done → `Conflict(detail={"task_id","blocked_by"})`，不再提前派下游任务。
- Agent 列表/项目卡片直接显示 `status="queued"`；`stats()` 增加 `queued/running/queue_max`。
- 事件：`agent.queued`、`agent.dequeued`（取消时 `canceled=True`）、`agent.timeout`、
  `agent.configured`；roster 快照持久化 queue/counters/limits。

### 扩展路由（`agents/extension.py`）

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/agents/queue?project_id=` | `{ok,data:{queued[],count,running,max_workers,queue_max,available}}` |
| POST | `/api/agents/queue/{id}/cancel` | body `{"reason":...}`（operator）；`{ok,data:{queue_id,canceled,agent}}` |

## 3. 交付 2：任务超时与工具次数

- runner 起跑时按 `agents.task_timeout_s` 建 deadline；每个 `brain.next_action` /
  `executor.run` 用剩余时间 `asyncio.wait_for` 包住；超时返回
  **blocked**，summary 含 `agents.task_timeout_s`，`agent.failed` 事件 data 带
  `{"timeout": true, "task_timeout_s": ..., "tool_calls": ...}`，任务置 `blocked`。
- 工具调用数达到 `agents.max_tool_calls_per_task` 时在**执行下一次之前**停止，
  返回 blocked，事件带 `{"limit": "max_tool_calls_per_task", ...}`。
- hub 安全网：`reap_stalled()` 扫描 `last_seen` 超过 `task_timeout_s` 的运行中任务
  agent（worker 卡死、无法自行汇报），kill/release 其进程，任务仍 `doing` 时通过
  `task_requeue`（扩展接线 = `projects.task_update(status=todo)`）释放并重新入队；
  `blocked/done/review` 不自动复活（人工决定）。watchdog 在第一次成功 spawn 后启动，
  间隔 `min(5s, timeout/4)`；`App` 关停时建议 `await supervisor.close()`。
- `reload_limits()` 让热更新在任务中途生效（deadline 基于起跑时间重算）。

## 4. 交付 3：配置热更新

hub 侧（`App.update_config` 已有 `for target in (supervisor, ...): on_config(cfg)` 调用，
**无需改 app.py**）：

```python
# supervisor.on_config(full_config)
payload = {
  "agents": {heartbeat_s, offline_after_s, max_workers, max_steps, task_poll_s,
             task_timeout_s, max_tool_calls_per_task, queue_max,
             config_reload_s, aggregator_interval_s},
  "llm": {provider, model, base_url, temperature, max_tokens, timeout_s, stream,
          rpm, tpm, max_concurrency, max_context_tokens, project_token_budget,
          retries, retry_backoff_s, routes[], fallback[]},   # 无任何 api_key
}
# 对每个在线 unit: control(unit_id, "unit.configure", payload)
```

worker 侧（`units/worker.py` 已接线）：

- `install_config_reload(unit, cfg, sys=sys_api, ...)` 注册 **`unit.configure`** RPC；
  处理时 `apply_runtime_config` 就地更新 `cfg.agents`/`cfg.llm`（routes 字段级合并，
  `api_key` 永不覆盖），调用 `runner.reload_limits()`，provider/model/base_url 变化时
  用 `_make_brain` 重建 brain；返回 `{ok, applied[], llm_changed, provider, model, reload_s}`。
- `agents.config_reload_s > 0` 时同一 reloader 周期性调用既有 RPC
  `sys.config.get`（`public_dict()` 密钥已打码）做对账，避免漏推送。
- 旧 worker 不认 `unit.configure` 时 hub 只记 warning/`agent.configured` 失败，
  持久化配置仍是唯一真相。

## 5. 交付 4：git 分支隔离（attached-rw）

`WorkspaceGit` 安全约束：

- 仅当 `security.workspace_git` 开启且项目 `workspace_mode == "attached-rw"` 时
  runner 才动作（managed/attached-ro 不碰）；`security.workspace_git_commit` 控制结束时是否提交。
- 工作区必须**本身**是 git repo 根；`rev-parse --show-toplevel != workspace` 直接拒绝
  （防止 reset/commit 外层用户仓库）。
- 全部 git 调用 `git -C <workspace> -c user.name=... -c core.hooksPath=/dev/null ...`
  的 argv 列表，禁用 hooks/签名/交互提示，60s 超时；不 push、不配 remote、不删分支。
- 分支名 `task/<project>-<task>`（非法字符归一、长度上限、拒绝 `..`/- 开头）；
  rollback 前把旧 HEAD 备份为 `sfo-rollback/<timestamp>[-n]` 再 `reset --hard <ref>`；
  ref 校验：`^[A-Za-z0-9][A-Za-z0-9._/@~^+-]{0,199}$`、无 `..`/前导 `-`、`rev-parse --verify`。
- `.artifacts/`（shell 日志）在 status/commit 中排除，避免把工具日志提交进仓库。

扩展路由：

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/projects/{id}/git` | `{repo, branch, head, dirty, changes[], stat, commits[], enabled, git_available, workspace_mode,...}`；未初始化返回 `repo:false` 不报错 |
| POST | `/api/projects/{id}/git/rollback` | body `{"ref"}`；仅 attached-rw + operator；返回 `{ref,commit,previous_head,backup_branch,...}` 并写 `project.git_rollback` 审计事件 |

## 6. 交付 5：reviewer 写权限（security-fix-plan §14）

- `ROLES["reviewer"].tools` 增加 `fs.write`；prompt 明确唯一写入是
  `review/<task-id>-review.md`。路径仍走 `ToolExecutor.check_path`（workspace 内、
  symlink/`..` 解析后判定、deny_paths 仍生效）；审批仍按现有策略（fs.write medium），
  **未**为 reviewer 放宽策略。
- 测试证明：reviewer 写 review 文件成功；`../outside.md` 仍被拒绝且无副作用。
- **请 Lead 更新 `docs/security-fix-plan.md` §14 第 1 条**（该文件属 Lead）：标注已补
  `fs.write`，剩余建议是对 review 路径做更细的策略（可选）。

## 7. 交付 6：沙箱 / net 接线

### shell.exec

`tools.py` 在 spawn 前调用 `agents.sandbox.wrap_shell_command(config, command,
cwd=..., writable_paths=[self.workspace])`：

- `security.sandbox == "off"` → 原命令、原行为；
- 模块可用 → 调 sec 的 `wrap_command(["/bin/sh","-c",cmd], writable_paths=...,
  allow_network=..., backend=security.sandbox, tmp_mb=...)`：
  Landlock 后端把 `plan.preexec_fn` 传给子进程，bwrap 后端用 `plan.argv`
  （`create_subprocess_exec`）；结果里加 `sandbox: {status,mode,backend,notes}`；
- 模块缺失/检测失败/包装抛错 → **保持原命令运行**，`agents.sandbox` 只记录一次原因；
  返回 `sandbox.status = unavailable|failed` 便于审计（不影响 exit_code/stdout）。
- 主机实测（Landlock ABI 8）：`echo ... > workspace/file` 成功，
  `echo ... > /tmp/outside` 报 `Permission denied`；bwrap 非特权不可用 → 自动选 Landlock。

### worker 自身（Lead 追加）

`units/worker.py` 在 `unit.start()` 之后、创建 executor/runner 之前调用
`apply_process_sandbox(cfg.security, [项目 workspace, state tmp, state logs/agents])`：

- → `security.sandbox.restrict_current_process(writable_paths=..., allow_network=...)`；
  读全域不限制，写只限白名单；报告写入 `unit.register.metadata.sandbox` 与
  `unit.status.sandbox`；
- `sandbox="bwrap"` 时 worker 自身不注入（bwrap 只能包子命令），报告 unavailable 并回退；
- 模块缺失/`landlock` 不可用/调用失败 → 记录原因、worker 继续以**未沙箱**模式运行
  （不阻断 hub）；`sandbox="off"` 完全 no-op。
- 风险：worker 在沙箱内不再能写 `run/`（socket unlink）；退出时 socket 清理可能失败
  （已有 try/except 兜底，hub allocator 会跳过被占路径）；tcp transport 下如果
  `sandbox_allow_network=False`，worker 重连 hub 会被 Landlock 拒绝，远程/tcp 部署建议
  开 `security.sandbox_allow_network`。`__pycache__` 写入失败由 Python 静默降级。

### net.fetch

sec 已改 `util/net.py`（`PinnedTarget`/`fetch_url(..., pin_dns=)`），但 agents 工具层
当前仍只走既有 `validate_fetch_url(..., allow_private_network=...)` + urllib opener
（双重逐跳校验）。**没有破坏性改动**：若后续要把 `security.net_pin_dns` 接到工具层，
需要 Lead/sec 提供等价 opener/`fetch_url` 适配（工具层可平滑替换 `_http_get`）；
当前行为与 sec 文档的“工具层传 pin 参数”尚未接线，记为残余项。

## 8. 交付 7：capability.call 工具桥（Lead 追加需求）

- TOOLS 新增 `capability.call`，args `{name:str, args:object={}}`，capability
  `capability.call`，risk medium；已加入 lead/architect/coder/tester/reviewer/researcher
  的 tools 表（aggregator 保持只读不进；assistant 未加）。
- 实现：`ToolExecutor._capability_call` → `sys.call("capability.call",
  {name, args, project_id, task_id})`；args JSON 序列化上限 256 KiB；
  - `Denied`/DENIED/UNAUTHORIZED/FORBIDDEN → `ToolError("DENIED: ...")`
    （复用 runner 的拒绝识别）；
  - NOT_FOUND/NOT_IMPLEMENTED/BAD_REQUEST 或 RPC 不可用 → 明确报
    “capability.call is unavailable on this hub”，worker 不会崩；
  - 仍走既有 `approval.request`（hub 策略/审计），最终授权由 hub 侧执行。
- hub 侧 `capability.call` action 已由 Lead/units 落地（`app._capability_call` →
  `capabilities.call_capability`，unit principal 被钉在 own project）；工具层实现与之
  对齐（payload `name/args/project_id/task_id`，返回 `{tool,provider,status,executed,result}`）。
- **已知集成缺口（需 Lead/units 决策）**：hub 对 capability 调用有强制审批门
  （`ensure_approved` 返回 `executed=false, status=pending, approval_id=...` 而不抛错），
  但 `app._capability_call` 目前不把 worker 传来的 `approval_id` 转发给
  `call_capability`，worker 也无法在本任务内完成第二轮人工审批 → 需要审批的 provider
  工具会得到 `pending`。工具层按 fail-closed 映射为 `DENIED: capability ... waiting
  for operator`（不会假装成功）。可选修法：hub 接受/透传 `approval_id`，或对
  「已被 worker 侧审批过的 unit 调用」复用 grant；在修好前，自动放行的 capability
  可正常执行。

## 9. 需要 Lead 接线/确认的点

1. **`stackflowos/extensions.py`**（Lead 所有，未改）：`"stackflowos.agents.extension"`
   已由 Lead 加入 `EXTENSION_MODULES`（已复核）；实机 boot 验证扩展加载为 `agents`、
   `GET /api/agents/queue` 返回 200、`GET /api/projects/<id>/git` 路由已注册、
   `task_lookup/task_requeue` 已挂上。
2. `app.py` 已把 `on_config(cfg)` 分发给 `supervisor`，**无需改动**。
3. 建议在 hub 关停路径调用 `await app.supervisor.close()`（取消 watchdog/drain task，
   避免关机时残留 task 警告）。
4. 更新 `docs/security-fix-plan.md` §14：reviewer fs.write 已修复（第 1 条）。
5. `capability.call` hub RPC action（units 完成后注册）。
6. `net.fetch` pin DNS：sec 的 `util/net.py` 已交付但工具层仍是旧 opener；如需启用
   `security.net_pin_dns`，请 Lead/sec 给出 `fetch_url` 适配方式，我再接（向后兼容）。
7. 若想让 watchdog 在 `App` 维护循环里统一驱动，可把 `supervisor.reap_stalled()`
   挂到 `_maintenance_loop`（当前是 supervisor 内部自启动，已可用）。

## 10. 测试

命令（本工作流）：

```
/tmp/sfo-venv/bin/python -m pytest -q \
  tests/test_supervisor.py tests/test_agent_chat.py tests/test_agent_runner.py \
  tests/test_agent_tools.py tests/test_agent_queue.py tests/test_agent_limits.py \
  tests/test_agent_workspace_git.py tests/test_agent_extension.py \
  tests/test_agent_reviewer.py tests/test_agent_sandbox.py tests/test_task_deps.py \
  --basetemp=/tmp/pytest-agent
```

结果：**142 passed + 51 subtests passed**（2026-11-24 09:4x，Linux，Python 3.12，
`/tmp/sfo-venv`）。另跑 `test_integration/test_web_security/test_self_hosting_contract/
test_shell_exec/test_net_fetch/test_operator/test_planner/test_config/test_units_visibility/
test_extensions/test_feature_endpoints` 共 **146 passed + 116 subtests**，无回归；`python -c "import stackflowos.app, stackflowos.units.worker"`
通过；`compileall` 通过。

实机 boot 冒烟（真实 `App.start()`，临时 state dir）：

```
extensions: [... 'agents' ...]
queue route: 200 0 6
git route: 404 project 'nope' not found   # 路由存在，项目不存在
task_lookup wired: True requeue: True
```

说明：

- git 测试在 `/tmp` 建**临时独立 repo**（不写入仓库）；rollback 断言会建备份分支；
  真实 Landlock 测试在子进程里验证「白名单内可写、外部不可写」（本机实测通过），
  bwrap/无 Landlock 环境自动 skip。
- `tests/test_agent_chat.py` 是 llm/lead 也在改的文件，本次只跑不修改。

## 11. 残余风险与取舍

1. **沙箱 API 形状**：按 sec 已交付的 `wrap_command/build_command/restrict_current_process/
   preexec_landlock` 实现，同时保留旧字符串 wrapper 回退；若 sec 再改签名，
   `agents/sandbox.py::_sandbox_plan` 的单点适配即可。
2. **worker Landlock 的 socket 清理**：`run/` 不在写白名单，`unit.stop()` unlink 会失败
   并被忽略；stale socket 由 hub allocator 的 ensure/跳号处理。若希望干净 unlink，
   需要把 `state/run` 加入白名单（会让 worker 可写自己的 socket 目录）。
3. **git 只支持「工作区本身是 repo 根」**：attached-rw 指向 monorepo 子目录时拒绝
   （否则会 reset 用户外层仓库）。需要这种用法时应改为 worktree/bare 隔离，
   属于后续项。
4. **rollback 是 `reset --hard`**：会丢弃该分支未提交改动（备份分支只保 HEAD，
   不保工作区未提交内容）；仅 operator + attached-rw 可用，且前端需二次确认。
5. **队列优先级是启发式**：task priority/urgency + role + 显示顺序；显式 `priority`
   才能表达更细的紧急度。排队任务持久化在 `agents.json`，hub 重启后恢复但不会自动
   drain（等下一次 spawn/配置/完成事件或 `drain_queue`）。
6. **reviewer 写文件仍需审批**（fs.write medium，按现有策略）；若希望评审记录免审批，
   需要 Lead 在 policy 层为 review 路径加低风险规则（不在本工作流所有权内）。
7. **超时重派只针对 `doing`**：runner 自己汇报超时会把任务置 blocked，交给人工；
   hub reaper 只回收无心跳的卡死 worker，避免自动复活已阻塞任务。
8. **capability.call 依赖 hub RPC**：未落地前工具返回清晰错误，不会崩溃，但功能不可用。
9. **测试临时目录**：git 测试用 `/tmp/sfo-git-*`（独立 repo 根），其余沿用仓库内
   `.stackflowos/tmp`；若 CI 需要全部隔离，可后续统一迁移。
