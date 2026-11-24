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

### 09:35–09:50 并行工作流交付与首批集成
- sec 完成：TLS 监听、单次 launch token + control socket、会话表/多用户角色、Landlock/bwrap 沙箱、审计脱敏、net.fetch 固定 IP（227 相关测试通过）。
- ops 完成：Prometheus 指标、webhook 通知、备份/恢复/导出、日志轮转。
- dist 完成：事件 gzip 轮转/跨归档、租约 fencing/时钟偏移、SQLite backend + 迁移、mDNS、ZMQ bridge、Dockerfile/systemd。
- agent 完成：优先级队列、任务超时/工具次数、热更新、git 分支隔离、reviewer fs.write、沙箱接线。
- llm 完成：原生 tool calling（OpenAI/Anthropic/Gemini）、流式、usage/cost/上下文/限流/熔断、角色路由/降级。
- units 完成：能力目录、skills/plugins/MCP、单元探针/迟滞/动作/依赖图、UnitClient/unit://；示例插件。
- front 完成：单元三层/详情/依赖图/动作、i18n（518 键）、会话 UI、LLM 用量、能力管理、git/依赖图、流式渲染。
- Lead 接线：`EXTENSION_MODULES` 全部 13 个扩展加载；EventLog 轮转参数、SQLite backend、租约 fencing/作用域；App 生命周期/健康检查/冷备锁；CLI `--tls-cert/--tls-key/--standby`、`sfo url`、`backup/restore/export-events/backup-status/storage-migrate`；`/api/units` summary；skill 注入（worker + agent chat）；`capability.call` approval_id；net.fetch pinned 路径；supervisor.close。
- 提交：`35f9039`（安全）、`f7fc029`（可观测性）、`10d3df5`（分布式）、`38cff17`（Agent）、`659d3dd`（LLM）、`1463893`（单元/能力）、`0d63a5e`/`5200d2b`（Lead 集成）、`56eef95`（前端）。
- 新增测试：`tests/test_feature_endpoints.py`（真实 App + WebServer 跑扩展端点/control socket 单次登录）、`tests/test_extensions.py`（契约/冷备/配置脱敏）、各子系统 `test_sec_*/test_ops_*/test_dist_*/test_agent_*/test_llm_*/test_units_*/test_front_*`。

### 09:50–10:05 文档、验收与残余项
- `docs/review-response.md`：六类评审逐条对照（✅ 已修复 / 🟡 部分 / 📝 设计说明），并列明长期项。
- `docs/operations.md`：TLS、`sfo url`、多用户、沙箱、通知、指标、备份恢复、SQLite 迁移、mDNS、冷备、单元/能力控制面。
- `README.md`：能力/skill/plugin/MCP、真实模型路由/流式/配额、安全模型、运维命令、诚实边界更新。
- `docs/architecture.md`：扩展契约、新事件目录、新配置 section、非目标/长期项、§15 子系统实现。
- `docs/security-fix-plan.md` §14：reviewer fs.write、lease 作用域、审计脱敏、launch token、DNS 固定 IP 标记完成；新增 §14.1 安全增补记录与残余风险。
- 全量测试目标：见最后一次 `pytest` 输出；所有新增模块 `python -m compileall` 通过。
