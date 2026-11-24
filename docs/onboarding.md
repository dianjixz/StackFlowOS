# 项目入住（Agent Onboarding）

> 来源：一位工人在空项目 workspace 里的真实体验——「不是缺东西，是缺信号」。
> 本文记录 StackFlowOS 现在为「Agent 进来时知道自己在哪、该做什么、能做什么、
> 做完往哪放」提供了什么，以及哪些边界是有意保留的。

## 0. 结论

一个空目录不会让 Agent 变强，只会让它反复试错。StackFlowOS 现在在项目
创建/接入时写入一组固定信号，并提供一条一次性自检入口：

| 信号 | 实现 | 入口 |
|---|---|---|
| 门牌：项目绑定 + workspace 路径 | 项目记录 + `brief.md` | `project.get` / `sfo project health` |
| 固定目录骨架 | `brief.md` + `tasks/` `artifacts/` `logs/` `tmp/` | `ensure_scaffold` / `sfo project scaffold` |
| 种子任务 | 「入职自检：复述目标并确认入住条件」 | 看板 / `project.health` |
| 入职自检 | scope/workspace/骨架/看板/记忆/读预授权/审计 | `project.health` / `GET /api/projects/{id}/healthcheck` |
| 能力白名单 | low 自动放行；写/执行/网络人审 | `PolicyEngine` + `ApprovalBroker` |
| 事件回放 | append-only 事件流，按 agent/source/type/时间过滤 | `sfo events` / `/api/events` / `event.query` |
| 记忆初值 | goal/workspace/layout/conventions/glossary | `memory.list` / `memory.get` |
| 失败语义 | `blocked`（缺前置条件）≠ `failed`（执行出错） | Agent 状态 + 事件 + UI |

## 1. 门牌：项目与 workspace

创建托管项目时，workspace 由系统分配：

```
<state>/projects/<project-id>/workspace/
├── brief.md      # 门牌：目标、路径、目录约定、审批边界、自检入口
├── tasks/        # 任务中间产物（按 task-id/）
├── artifacts/    # 交付物（评审看这里）
├── logs/         # 命令输出/自检结果
└── tmp/          # 草稿，可随时清理
```

`brief.md` 同时给出：项目 id、名称、goal、workspace 绝对路径、workspace 模式、
项目状态、创建者；目录用途；工作方式；读写执行网络的审批边界；记忆键；自检命令。
Agent 的 prompt/LLM 状态里也带 `project.goal`、`project.brief`、`workspace` 和
项目记忆，所以即使不读文件也知道门牌。

接入已有目录（`project.attach`）**不会**写入骨架，也不会创建/复制/删除目录：
- `attached-ro`：只读；写/执行在 worker 侧直接拒绝。
- `attached-rw`：可写；仍要经过能力审批。
- 自检会把「目录骨架缺失」标成 `warn`（不是 `fail`），并给出
  `sfo project scaffold <id>` 或改为可写接入的修复建议。
- 显式执行 `sfo project scaffold` / `POST /api/projects/{id}/onboard` 才会写外部
  目录；`attached-ro` 会拒绝。

## 2. 种子任务

项目创建/接入时，如果看板里还没有「入职自检」任务，会写入一条（role=`researcher`）：

1. 调 `project.health`；`ready=false` 就把 `not_ready_reasons` 写进
   `artifacts/onboarding-<pid>.md`，然后 `finish(blocked)`；
2. 读 `brief.md`，确认目标、路径、目录约定、审批边界；
3. `fs.list .` 确认四个目录存在；
4. 写 `artifacts/onboarding-<pid>.md`：复述目标、workspace、工具/审批边界、
   健康检查摘要、产物/日志路径；
5. `task.artifact` 登记，推进 `review`，`finish(done)`。

这样即使项目没有真实业务任务，Agent 也能完成一次「读 → 检查 → 写 → 登记 → 评审」
的完整闭环，而不是干等。

## 3. 自检入口

### Agent 工具

```json
{"tool": "project.health", "args": {}}
```

返回结构（节选）：

```json
{
  "project_id": "p-1",
  "ready": false,
  "status": "not_ready",
  "summary": {"ok": 8, "warn": 3, "fail": 1, "unchecked": 0},
  "checks": [
    {"id": "workspace.layout", "status": "fail",
     "message": "缺少：brief.md", "fix": "运行 `sfo project scaffold p-1` 补齐"},
    {"id": "board.tasks", "status": "ok", "message": "任务 6 个，可推进 5 个"},
    {"id": "policy.reads", "status": "ok",
     "message": "fs.read / fs.list / data.read 自动放行"}
  ],
  "not_ready_reasons": ["缺少：brief.md"],
  "next_actions": ["运行 `sfo project scaffold p-1` 补齐"]
}
```

检查项：`project.scope`、`agent.binding`（Agent 调用时）、`workspace.path`、
`workspace.write`、`workspace.layout`、`project.goal`、`board.tasks`、
`board.onboarding_task`、`memory.seed`、`policy.reads`、`policy.writes`、
`policy.shell`、`eventlog.audit`、`agents.active`、`agent.state`。
`fail` = 未就绪（Agent 会被前置条件卡住）；`warn` = 值得修但不阻塞；
`checked=false` = 离线调用缺少实时服务，不会误报失败。

### 命令行（离线，只读状态）

```bash
sfo project health p-1          # 人类可读
sfo project health p-1 --json   # 机器可读
sfo project scaffold p-1        # 只写 workspace 骨架，不碰 state
```

离线模式会跳过 Supervisor/实时事件日志检查（标 `unchecked`），并直接读
`state/projects.json`、`state/memory/<pid>.json`、`state/rules.json`，不会写第二份
事件序列。

### HTTP / 操作员

```
GET  /api/projects/{id}/healthcheck
POST /api/projects/{id}/onboard      # 幂等：骨架 + 种子任务 + 记忆
```

## 4. 能力白名单：读预授权，写/执行/网络人审

策略默认值（`security.auto_allow_low=true`）：

| 能力 | 风险 | 默认 |
|---|---|---|
| `fs.read` / `fs.list`（workspace 内） | low | 自动放行 |
| `task.list` / `project.get` / `event.query` / `memory.get|list` / `project.health` | low | 自动放行 |
| `fs.write`（workspace 内） | medium | 人审 |
| `fs.write`（workspace 外） | high | 人审 |
| `shell.exec` | high | 人审 |
| `net.fetch` | medium | 人审（私网/非 http(s) 升 high） |

关键修复：接入项目的 workspace 在 `projects_root` 之外，旧实现把所有路径都按
`projects_root/<id>/workspace` 计算边界，导致接入项目里**每次读都升为 high**。
现在 `PolicyEngine.workspace_for` / `ApprovalBroker` 使用项目记录的真实
workspace，读回到 low（自动放行），写仍是 medium（人审）。未知/非法项目 id
仍回退到不存在的托管路径，保持 fail-closed。

## 5. 事件流可回放

所有动作（tool、approval、task、agent、chat）都 append 到
`<state>/events/events.jsonl`（可轮转 gzip）。查询支持：

- `agent_id`：匹配 hub 绑定的 `agent:<id>` source（兼容旧 `data.agent_id`）；
- `source`：精确 source；
- `types`：类型或点号前缀（`agent` 匹配 `agent.step`，`agent.` 不匹配）；
- `since_ts` / `until_ts`：毫秒时间戳，闭区间；
- `project_id`、`limit`。

```bash
sfo events --project p-1 --agent agent-coder-1 --since-ts 1795480000000
sfo events --types tool,approval --limit 50 --json
```

Agent 侧用 `event.query`（同一 `event.since` RPC，带过滤参数）；API 用
`GET /api/events` / `GET /api/console`。

## 6. 记忆初值

每个项目写入（只补缺失键，不覆盖人工修改）：

```json
{
  "goal": "项目目标",
  "brief": "项目 brief（初始为空）",
  "workspace": {"path": "...", "mode": "managed", "external": false, "project_id": "p-1"},
  "layout": {"tasks": "...", "artifacts": "...", "logs": "...", "tmp": "..."},
  "conventions": {
    "artifact_path": "artifacts/<task-id>-<slug>.<ext>",
    "task_scratch_path": "tasks/<task-id>/",
    "log_path": "logs/<task-id>.log",
    "tmp_path": "tmp/",
    "naming": "...",
    "onboarding": "brief.md 的 doorplate；自检用 project.health。"
  },
  "glossary": []
}
```

Agent 的 view/LLM state/agent chat context 都带这些键；新增术语应写回 memory，
而不是让每个新 Agent 重新猜。

## 7. 失败语义：blocked ≠ failed

| 状态 | 含义 | 例子 | 人应该做什么 |
|---|---|---|---|
| `blocked` | 缺前置条件/到预算上限 | 没绑定项目、workspace 不存在、任务不存在/已被抢、max_steps、任务超时、工具次数上限、连续失败 | 补 scope/目录/任务/调高预算 |
| `failed` | 执行期真的出错 | 异常崩溃、LLM 调用失败、工具实现错误 | 查 bug/日志 |
| `dead` | 进程被终止 | kill / 下线 | 视情况重派 |

两者都带非空 `error`；`blocked`/`failed` 还带机器可读 `error_code`
（`PROJECT_UNBOUND`、`WORKSPACE_MISSING`、`TASK_MISSING`、`CLAIM_CONFLICT`、
`MAX_STEPS`、`TASK_TIMEOUT`、`TOOL_LIMIT`、`TOOL_DENIED`、`AGENT_CRASH` 等）。
Runner 在 claim 前做 preflight；事件分别发 `agent.blocked` / `agent.failed`；
UI 分别显示，不会再出现「failed 但 error 为空」。

派工时现在强制要求 `project_id`，且项目 workspace 必须存在；没有 scope 的
Agent 会被直接拒绝（400/409），而不是启动后跑成谜之失败。

## 8. 已知边界

- 接入目录的骨架不由系统隐式创建（尊重「接入不修改既有目录」契约）；
  需要时由操作员显式 `sfo project scaffold` 或 `POST .../onboard`。
- 离线 `sfo project health` 不读实时 Supervisor/事件内存，相关项标 `unchecked`。
- 事件回放受 `EventLog.query` 的扫描预算约束（默认最多 5000 条/2MB 扫描），
  更早的归档需先缩小过滤条件。
- `blocked` 是 Agent 终态（进程结束），不是自动重试；由 lead/操作员决定补条件后
  重新派工。
- 记忆是 key/value 小块，不适合放长文档；长内容放 `artifacts/` 或 `logs/`。

## 9. 验收

```bash
# 项目入住自检（离线）
sfo project health p-1

# 事件回放
sfo events --project p-1 --agent agent-coder-1 --limit 20

# 全量测试
/tmp/sfo-venv/bin/python -m pytest -q
```

对应测试：
`tests/test_projects_onboarding.py`、`tests/test_projects_health.py`、
`tests/test_project_onboarding_app.py`、`tests/test_attached_workspace_policy.py`、
`tests/test_event_replay.py`、`tests/test_agent_runner.py`（preflight/blocked）、
`tests/test_supervisor.py`（finish 语义）、`tests/test_rpc_scope.py`（health/memory
scope）。
