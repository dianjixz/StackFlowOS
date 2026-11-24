# 团队实施计划（2026-11-24）

> 目标：把外部评审列出的缺口从「规划」推进到「可运行、可验证、有测试的代码」。
> 本文是并行开发的契约：**文件所有权、扩展点、API 形状、验收与交接方式**。
> 先读本文，再读 `docs/units-plan.md`、`docs/security-fix-plan.md`、`docs/architecture.md` 中与你的工作流相关的小节。

## 0. 基线

- 仓库：`/home/nihao/work/workspace/StackFlowOS`，Python ≥3.11，唯一运行时依赖 `pynng`。
- 测试：`/tmp/sfo-venv/bin/python -m pytest -q`（系统 Python 受 PEP 668 保护，pytest 装在 `/tmp/sfo-venv`）。基线 **693 passed + 599 subtests**。
- 工作纪律：阶段提交由 Lead 执行；**teammate 不运行任何 `git` 写操作**（不 add/commit/checkout/stash），只改文件并报告。
- 不碰别人的文件（见 §2）；确实需要跨界时先 `send_message` 给 `lead` 说明原因，等 Lead 处理。
- 同一 repo 内并行跑测试会争抢端口/临时目录：**只跑与你改动相关的测试文件**，加 `--basetemp=/tmp/pytest-<你的名字>`；全量测试由 Lead 跑。
- 工作记录：Lead 会写 `docs/worklog.md`。teammate 完成后在 `docs/team/<name>.md` 留一份交接说明（改了哪些文件、接口、测试、残余风险、需要 Lead 接线的点）。

## 1. 已提供的扩展点（不要改 `app.py` / `extensions.py`）

`stackflowos/extensions.py` 会按顺序 import 下列模块，并调用 `build(app)`：

```
stackflowos.capabilities
stackflowos.skills
stackflowos.plugins
stackflowos.mcp
stackflowos.units.control
stackflowos.observability.metrics
stackflowos.observability.notifications
stackflowos.observability.backup
stackflowos.transport.discovery
stackflowos.cluster.standby
```

扩展对象可选实现：

```python
name: str
async start(); async stop(); async on_config(config)
units() -> list[dict]                    # 与 /api/units 同形状；同 unit_id 覆盖核心单元
routes() -> list[(method, regex, handler)]  # handler: async (match, query, body) -> (status, payload)
capabilities(kind: str|None) -> list[dict]  # {type,name,provider,risk,schema,description,available,enabled_in}
health() -> dict
```

- 路由由 `WebAPI` 在核心路由之后注册；handler 是**绑定方法**（自己读 `self.app`）。
- 非 JSON 响应用 `stackflowos.web.api.WebReply(body, content_type=..., status=...)`。
- 请求的认证决策用 `stackflowos.web.api.current_auth()` 读取（`role` 为 `operator|viewer`）。
- 扩展失败被隔离：`build/start` 抛异常只记日志，不会阻止 hub 启动。
- `app.logical_units()` 会合并 `units()`；**不要在自己的 `units()` 里调用 `app.logical_units()`**（有递归保护但没必要）。
- 需要的运行时对象：`app.ctx`、`app.eventlog`、`app.projects`、`app.memory`、`app.approvals`、`app.policy`、`app.registry`、`app.leases`、`app.supervisor`、`app.provider`、`app.chat`、`app.agent_chat`、`app.paths`、`app.config`、`app.rpc_auth`。

### 配置

`stackflowos/config.py` 已加入全部新 section（不可再改该文件，需要字段就告诉 Lead）：

- `skills`: `dirs, enabled, inject_max_chars, max_skills`
- `plugins`: `dirs, enabled, max_plugins, allow_untrusted`
- `mcp`: `servers, call_timeout_s, max_servers, max_tools`
- `observability`: `metrics_enabled, webhooks, notify_events, notify_timeout_s, log_rotate_bytes, log_rotate_keep, backup_keep`
- `storage`: `backend, sqlite_path, eventlog_rotate_bytes, eventlog_rotate_keep`
- `cluster`: `discovery, service_type, advertise, standby, standby_poll_s, lease_fencing, lease_clock_skew_s`
- `web` 新增：`tls_min_version, launch_token_single_use, session_max, users[]`
- `security` 新增：`sandbox, sandbox_allow_network, sandbox_tmp_mb, workspace_git, workspace_git_commit, audit_redact, audit_max_field_chars, net_pin_dns`
- `llm` 新增：`stream, rpm, tpm, max_concurrency, max_context_tokens, project_token_budget, fallback[], routes[], retries, retry_backoff_s`
- `agents` 新增：`task_timeout_s, max_tool_calls_per_task, queue_max, config_reload_s`

密钥在 `/api/config` 中显示为 `***`；PATCH 回来的 `***` 由 `Config.apply` 还原为当前值。

## 2. 文件所有权（硬边界）

| 所有者 | 独占文件（可改） | 交付主题 |
|---|---|---|
| **lead** | `app.py`、`config.py`、`extensions.py`、`cli.py`、`web/api.py`、`web/server.py`（W-SEC 完成后移交）、`README.md`、`docs/*`（除 `docs/team/*`）、`tests/test_extensions.py` | 接线、集成、全量测试、提交 |
| **sec** | `web/auth.py`、`web/server.py`、`session/operator.py`、`util/net.py`、新增 `security/sandbox.py`、`security/sessions.py`、`security/control.py` | TLS、单次 launch token、会话/多用户、沙箱、审计脱敏、net.fetch 固定 IP |
| **llm** | `ai/**`、`session/chat.py`、`session/agent_chat.py` | 原生 tool calling、多 provider、流式、token/配额/路由/降级 |
| **agent** | `agents/**`、新增 `agents/workspace_git.py` | 优先级队列、超时/工具次数、热更新、git 分支隔离、reviewer 写权限、沙箱接线 |
| **units** | 新增 `capabilities.py`、`skills/**`、`plugins/**`、`mcp/**`、`units/control.py`、`l2/registry.py`（只在必要时小改） | 能力目录、SkillRegistry、PluginLoader、MCP、单元动作/探针/UnitClient |
| **ops** | 新增 `observability/**`、`util/logging.py` | Prometheus 指标、通知渠道、备份/恢复/导出、日志轮转 |
| **dist** | `l2/eventlog.py`、`l2/leases.py`、新增 `storage/**`、`transport/discovery.py`、`transport/zmq_bridge.py`、`deploy/**`、`projects/store.py`、`memory.py` | 事件日志轮转/归档、租约 fencing、存储后端、mDNS、ZMQ bridge、HA/打包 |
| **front** | `webassets/**` | 单元详情/依赖图/动作、i18n、会话管理 UI、配额/用量、任务依赖图、流式渲染 |

> `web/server.py` 已由 Lead 加了 `WebReply` 与 `api.handle(..., decision)` 钩子；**sec 接手后可以继续改**，但不要删除这两个能力。
> `web/api.py` 的扩展路由钩子已存在；**不要 edit api.py**，新路由一律走扩展 `routes()`。

## 3. API 契约（按此实现；前端按此调用）

已有：`/api/state`、`/api/units`、`/api/agents`、`/api/approvals`、`/api/events/stream`（SSE）、`/api/llm/test`、`/api/config`、`/api/leases`。

新增（POST body 默认 JSON；错误沿用 `{ok:false,error:{code,message}}`）：

| Method | Path | 说明 | 所有者 |
|---|---|---|---|
| GET | `/api/capabilities?type=tool\|skill\|plugin\|mcp_tool&project_id=` | 统一能力目录，条目带 `provider/available` | units |
| GET | `/api/units/{id}` | 描述符 + 最近状态事件 + 依赖（requires/wants）+ 允许动作 | units |
| POST | `/api/units/{id}/probe` | 立即探针 | units |
| POST | `/api/units/{id}/actions` | `{"action":"restart\|disable\|enable\|drain\|undrain"}` | units |
| GET | `/api/skills?project_id=` | skill 目录 + 校验状态 | units |
| POST | `/api/skills/{id}/enable`、`/disable` | body `{"project_id":或空=全局}` | units |
| GET | `/api/plugins` | 插件目录 + 单元 id + 贡献 | units |
| POST | `/api/plugins/{id}/load`、`/unload` | 受信 in-process 插件 | units |
| GET | `/api/mcp/servers` | 配置/连接状态 | units |
| POST | `/api/mcp/servers/{name}/connect`、`/disconnect` | 生命周期 | units |
| GET | `/api/mcp/tools?server=` | MCP 工具目录 | units |
| POST | `/api/mcp/call` | `{"server","tool","args"}`，走审批/审计 | units |
| GET | `/api/metrics` | Prometheus text（`WebReply`） | ops |
| POST | `/api/notifications/test` | 发一条测试通知 | ops |
| GET | `/api/llm/usage?project_id=` | tokens/cost/rpm/tpm/预算/熔断 | llm |
| GET | `/api/llm/models` | 已配置 provider/route/健康 | llm |
| GET | `/api/agents/queue` | 排队中的派工 | agent |
| GET | `/api/projects/{id}/git` | 分支/最近提交/diff 摘要 | agent |
| POST | `/api/projects/{id}/git/rollback` | `{"ref"}` 恢复工作区 | agent |
| GET | `/api/sessions` | 当前会话/最近会话列表 | sec |
| POST | `/api/sessions/rotate` | 轮换 cookie + launch token（operator） | sec |
| DELETE | `/api/sessions/{id}` | 撤销一个会话 | sec |
| POST | `/api/logout` | 清 cookie | sec |
| GET | `/api/discovery/peers` | mDNS 发现的 hub/worker | dist |
| GET | `/api/cluster/status` | 本节点、standby、租约 fencing | dist |

SSE 事件新增：`chat.delta`（已有 ephemeral 类型）、`unit.state_changed`、`skill.changed`、`plugin.loaded/unloaded`、`mcp.connected/disconnected`、`llm.usage`、`approval.notified`。

## 4. 验收（每个工作流）

1. 代码可导入、无语法错误；相关测试通过（新增测试文件放 `tests/test_<domain>_*.py`）。
2. 不破坏基线：只改自己文件；新增依赖必须是 stdlib（可选依赖要 import guard）。
3. 安全默认值：关闭/失败时 fail-closed；不把密钥写日志/事件。
4. 在 `docs/team/<name>.md` 写交接：改动文件、接口、测试命令与结果、需要 Lead 接线的代码位置、残余风险。
5. **不要停在一半**：实现、测试、文档三件套都完成再报告；确实做不完的，明确写出已完成/未完成边界。

## 5. 团队任务板

每个工作流有一个 team task（`team_task_*`）。开始前 `team_task_get` → `team_task_update(action="claim", expected_revision=...)`；结束时 `team_task_update(action="complete")`。遇到跨域阻塞用 `send_message` 找 `lead`，不要自己越界改文件。

## 6. 已知取舍

- 多 hub 强一致/选主不在本轮：`cluster.standby` 是**同状态目录冷备**（进程级 failover），文档必须写清不是 Raft。
- 不受信插件子进程、容器级沙箱、系统级多租户是长期项；本轮做「可用的最小实现 + 诚实的能力检测/降级」。
- 前端无构建步骤是硬约束：不要引入 npm/打包器。

---
*本计划由 Lead 维护；与代码冲突时以 `docs/architecture.md` 的契约和现有测试为准，并在 `docs/team/lead-notes.md` 记录偏差。*
