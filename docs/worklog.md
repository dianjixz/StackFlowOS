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

### 10:00–10:20 工人反馈：项目入住设施、失败语义与读预授权
- 背景：一位工人在空项目 workspace 里的反馈——缺门牌/目录骨架/种子任务/自检入口，
  读动作频繁弹审批，失败只有 `failed` 且 `error` 为空，事件无法按 agent/时间回放。
- **策略边界修复**：`PolicyEngine.workspace_for` + `workspace_resolver`，`ApprovalBroker`
  的风险与 `path_prefix` 授权改用项目真实 workspace；接入项目（attached-ro/rw）里的
  `fs.read/fs.list` 回到 low 自动放行，写仍 medium 人审，未知/非法 id fail-closed。
  提交：`8abde18` `fix(policy): use attached project workspace for risk and approval scopes`。
- **失败语义**：Agent 新增终态 `blocked`（缺 scope/workspace/任务、max_steps、超时、
  工具上限、连续失败），`failed` 只留给执行错误；两者强制非空 `error` + `error_code`；
  runner 在 claim 前 preflight；事件 `agent.blocked` 独立；派工强制 project_id 且
  workspace 存在；UI 的活动/结束筛选与顺序同步。
- **项目入住包**：新增 `stackflowos/projects/onboarding.py`（brief.md、固定目录、种子
  任务、记忆初值、只读接入拒绝写入）与 `stackflowos/projects/health.py`（14 项检查 +
  `ready/not_ready_reasons/next_actions/fix`）。创建项目自动入住；接入项目只做只读
  巡检 + 状态侧种子；`POST /api/projects/{id}/onboard` 幂等补齐。
- **自检入口**：Agent 工具 `project.health`；RPC `project.health`（按 principal 带 agent
  binding 检查，单位只能看自己项目）；`GET /api/projects/{id}/healthcheck`；
  CLI `sfo project health`（离线只读）与 `sfo project scaffold`（只写 workspace）；
  项目详情新增「入住自检」卡（ok/warn/fail、fix、补齐/重检按钮）。
- **读预授权与记忆**：`security.auto_allow_low` 默认生效（读类自动放行）；`memory.list`
  RPC/工具/角色授权；Runner view、LLM compact state、Agent 对话上下文注入项目记忆。
- **事件回放**：`EventLog.query` 支持 `agent_id/source/types/since_ts/until_ts`；API
  `/api/events` `/api/console` 与工具 `event.query` 接线；新增 `sfo events` 命令行。
- **文档**：新增 `docs/onboarding.md`；更新 `README.md`、`docs/architecture.md`
  （§5.7 事件、§6.2 workspace 边界、§7 入住、§8.1 blocked/failed）。
- **测试**：新增 `test_projects_onboarding/health`、`test_project_onboarding_app`、
  `test_attached_workspace_policy`、`test_event_replay`；更新 runner/supervisor/limits/
  queue/attach/event-trust/rpc-scope 断言；全量 **1152 passed + 623 subtests**（2 skipped）。
- **提交**：`f6ae4c6` `feat(onboarding): project kit, health check, blocked/failed split,
  replay filters`；`6f99e86` 文档；`3768e0d` 统一 `agent.spawn` RPC 的项目 scope 守卫；
  `906351b` `fix(agents): use the LLM brain when routes or fallback are configured`
  （并行阶段遗留的 5 行修复）。

### 10:20–10:30 最终验收
- 全量测试：`/tmp/sfo-venv/bin/python -m pytest -q` → **1152 passed, 2 skipped, 623 subtests passed**（~96s；2 skipped 为真实组播/pyzmq 可选路径）。
- `python3 -m compileall -q stackflowos tests`、`git diff --check`、所有 `webassets/js/*.js` 的 `node --check` 通过。
- 端到端：`stackflowos demo --until-done --no-web` 在临时 state 下完成 7 个任务并产出 workspace 文件。
- 远程/TLS：本机自签证书启动 `hub --tls-cert/--tls-key --port 18999`，`stackflowos url` 经 control socket 铸造一次性 URL → HTTPS `?token=` 兑换 303 + HttpOnly cookie → `/api/health` 200；第二次兑换被拒绝。
- 清理：停止遗留的仓库 `.stackflowos` hub 进程，避免占用 17800；工作树只保留文档变更。
- 团队看板：task-1…task-7 全部 completed；7 个 teammate 已停止，无后台任务。
