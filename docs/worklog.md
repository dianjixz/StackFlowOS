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
