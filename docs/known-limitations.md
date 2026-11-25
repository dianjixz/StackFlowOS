# 已知限制（代码证据版）

本文记录 StackFlowOS **当前实现**里确认保留的架构限制：每条都给出代码位置、影响、
缓解方式和是否在计划内。目的是让 README、self-hosting、operations 与各工作流交付
文档对"系统现在到底是什么样"只有一套说法。

阅读约定：

- 证据写成 `路径::符号`（类/函数/常量/模块文档），行号会随重构漂移，符号更稳定；
  需要精确行号时再 `grep -n` 定位。
- 只有能在代码里找到调用点或测试的能力，才写成"已接线/已实现"；找不到就说"未接线"。
- 标注"相关代码/测试路径"的条目，表示该处正在演进：以路径里的当前实现为准，不要
  从本条反推"没有"。
- 与本文冲突的旧表述一律以本文 + 代码为准；改代码时请在同一个改动里更新本文。

核实基线：仓库 `f788580` + 2026-11-26 撰写时的工作区（含并行工作流已落地的改动）。
总览：

| # | 限制 | 是否计划内 |
|---|---|---|
| 1 | 单 hub：单点 + 冷备等锁，无选主/复制/分片 | 否（"多 hub 强一致"仅长期项，无排期） |
| 2 | worker 同 OS 用户；沙箱是可选的 Landlock/bwrap，不是容器 | 部分（独立 uid/容器是 Phase 5，无排期） |
| 3 | 审批与插件在 hub 同进程；插件 manifest permissions 声明不强制 | 审批拆进程是长期项；插件权限强制不计划 |
| 4 | 多用户会话在内存、hub 重启需重新登录、长期 Bearer | 否（有意取舍） |
| 5 | projects/tasks 仍是 JSON 无事务；JSON 与 SQLite 不双写；fencing 尽力而为；租约只有 TTL+fencing | 部分（projects/tasks 事务化是长期项） |
| 6 | 备份不是一致性快照；config.json 默认脱敏、可选 include_secrets | 否 |
| 7 | chat/agent chat 的 delta 是临时事件，不可回放 | 否（设计取舍） |
| 8 | 无 OTel 导出、指标重启归零；通知尽力而为 | OTel 是长期项；指标/通知模型保留 |
| 9 | 沙箱/组播/pyzmq/真实 LLM/IPv6 在默认测试里 skip 或未测；沙箱仅 Linux | 部分（测试基础设施在补） |
| 10 | 前端 i18n 只覆盖主路径、流式单草稿、依赖图固定布局、移动端未真机测试 | 否（当前前端取舍） |

---

## 1. 单 hub：单点 + 冷备等锁，无选主/复制/分片

**限制**：一个状态目录同时只有一个 active hub。`cluster.standby=true` 只是让第二个
进程轮询状态目录里的 `run/hub.lock`（POSIX `flock`），活动 hub 退出后接管；没有
quorum、没有日志/状态复制、没有 leader 选举、没有分片。

**影响**：

- hub 进程或主机故障 = 全系统不可用，直到冷备完成启动（数秒级不可用窗口）；
- active 只是挂起（SIGSTOP、VM freeze、网络分区）时，冷备无法判断"死亡"，会一直等锁；
- 状态目录必须是两个进程都能访问的同一路径（同机/同卷），跨机复制只能靠外部存储；
- 没有跨 hub 多写一致性，所有写者都假设自己是唯一 writer。

**现状证据**：

- `stackflowos/app.py::App._acquire_hub_lock` —— 打开 `run/hub.lock`，`fcntl.flock`
  非阻塞尝试；`cluster.standby` 时轮询等待，否则直接报"另一个 hub 正在使用状态目录"；
  docstring 明说 "process-level failover, not consensus -- there is no quorum, no
  replication"。
- `stackflowos/app.py::App._release_hub_lock` —— 进程退出/关闭时释放锁（内核在进程
  死亡时也会释放，残留锁文件无害）。
- `stackflowos/config.py::ClusterCfg` —— `standby: bool = False`、
  `standby_poll_s: float = 2.0`，注释写明"no quorum, single shared state dir"。
- `docs/team/dist.md` §7 冷备语义 —— 与代码一致（同状态目录第二进程、非 Raft）。

**缓解**：

- 同状态目录冷备 + 外部进程监控（systemd `Restart=on-failure` 或等价）；
- 定期备份（见第 6 条），把恢复时间目标写进运维流程；
- 维护窗口执行升级/迁移，不把冷备当 HA（active 挂起场景需要人工介入）；
- 需要水平扩展时，先在外部做状态目录单写约束，再考虑多 hub 设计。

**是否计划内**：否。当前形态就是冷备；README 的长期项里列了"多 hub 强一致"
（[README.md](../README.md) "边界与已知限制" 第 8 条），没有排期。

---

## 2. worker 同 OS 用户；沙箱为可选 Landlock/bwrap，不是容器

**限制**：

- worker 由 hub 以**当前 OS 用户**身份启动，没有 uid/gid 切换；
- `security.sandbox` 默认 `off`；开启后 Landlock 只限制**文件写**（ABI≥4 时含 TCP
  bind/connect），不处理 UDP、信号、`/proc`、调试等；
- bwrap 需要可用的非特权 user namespace，探测失败会明确降级 `unsandboxed`；
- 目前只有 Linux 有这两个后端。

**影响**：恶意或幻觉的 worker 在同 uid 下仍能读状态目录、给 hub/其它 worker 发信号、
访问 `.stackflowos` 数据、使用 UDP 或读取进程信息；`sandbox=off` 时文件边界只靠
hub 侧路径策略与审批。接不可信设备/多租户时，这不是安全隔离边界。

**现状证据**：

- `stackflowos/agents/supervisor.py::AgentSupervisor._launch` —— 构造
  `subprocess` 的 `kwargs` 只有 `cwd/env/stdio/start_new_session`，没有
  `user`/`group`/`preexec` 切换身份。
- `stackflowos/units/worker.py::run_worker` —— 启动时调用
  `apply_process_sandbox(...)`；失败只告警，worker 继续以 `unsandboxed` 运行。
- `stackflowos/agents/sandbox.py::apply_process_sandbox` /
  `::wrap_shell_command` —— `security.sandbox=off` 时 no-op；模块缺失/应用失败都
  fail-open，并在返回值和日志里带 `status`/`reason`。
- `stackflowos/security/sandbox.py` —— 模块文档写明 Landlock 规则只处理写
  （read/execute 始终放行）、ABI≥4 才 deny TCP、UDP 不在覆盖范围；
  `detect_landlock()` 对非 Linux 直接返回不可用；`detect_bwrap()` 做功能性探测
  （`bwrap --ro-bind / / true`），`build_command()` 无法隔离时返回原 argv 并标
  `unsandboxed`；`audit_startup()` 在启动时输出能力矩阵，降级时写
  `sandbox.unsandboxed` 审计事件。
- `stackflowos/agents/tools.py`（`shell.exec` 执行路径）—— 每个命令走
  `wrap_shell_command`，采集返回的 `backend/notes` 进审计。

**缓解**：生产环境显式开 `security.sandbox=auto|landlock|bwrap`，并看
`sandbox.capabilities`/`sandbox.unsandboxed` 事件与 `/api/health`；不可信设备/多租户
用独立 OS 用户或容器（README 把独立 uid/容器列在 Phase 5）；不要依赖沙箱限制网络
（UDP 不在 Landlock 范围）。

**是否计划内**：部分。独立 uid/容器是 Phase 5、无排期；Landlock 的覆盖范围是内核
能力边界，属于保留限制；仅 Linux 沙箱保留。

---

## 3. 审批与插件在 hub 同进程；插件 permissions 声明不强制

**限制**：

- 审批是 hub 进程内的 `ApprovalBroker`：请求、策略判断、等待/超时、发放 grant 都在
  hub 里完成，没有独立审批进程；
- 插件是 **trusted in-process**：`runtime` 只接受 `in_process`，`importlib` 在 hub
  进程里加载并执行 `apply(ctx, provides)`；
- manifest 的 `permissions` 是声明式元数据，不参与能力裁剪或权限强制；当前工作区
  已加入加载时审计：高危声明会打 WARNING 并记 `security.plugin_permissions` 事件
  （`enforced: false`），但这只是让缺口可见，不是授权边界；
- 审批超时按拒绝处理（fail closed）；当前工作区把超时升级做成可观测性（
  `approval.timeout` 事件 + 有界操作员队列 + 可选同步 hook），但没有自动改派给
  其它审批人或升级审批层级的机制。

**影响**：审批逻辑或插件代码崩溃/被攻破会直接影响 hub；插件以 hub 进程权限运行
（文件、网络、状态目录、其它服务 API 全可见）；把 `permissions` 当成安全边界是
危险的；操作员长时间不在线时高风险任务停在 pending，超时后失败。

**现状证据**：

- `stackflowos/app.py::App.__init__` —— `self.approvals = ApprovalBroker(...)`，与
  eventlog/policy 同进程构造；`stackflowos/security/approvals.py::ApprovalBroker`
  的模块文档描述 request/decide/claim/use 流程与"过期为拒绝"。
- `stackflowos/app.py::App._approval_request` / `::App._approval_decide` —— hub 的
  RPC 处理函数直接调用 `ApprovalBroker.request()`/`decide()`，审批与 hub 同进程，
  不存在独立审批守护进程。
- `stackflowos/security/approvals.py::ApprovalBroker._record_timeout` /
  `::ApprovalBroker.timeout_queue` —— 超时 = 拒绝；`approval.timeout` 事件与有界
  队列只是升级线索，hook 不能批准请求。
- `stackflowos/plugins/__init__.py` —— 模块文档 "Only trusted, in-process plugins are
  supported"；`validate_manifest()` 对 `runtime != "in_process"` 直接报错；
  `PluginLoader._load_module()` 用 `importlib.util.spec_from_file_location` /
  `exec_module` 在 hub 内执行。
- `stackflowos/plugins/__init__.py::validate_manifest` 与
  `::PluginRecord.to_dict`、`stackflowos/plugins/__init__.py`（`unit_cards` 的
  `plugin.N` metadata）—— `permissions` 只做 `list[str]` 类型校验并原样回显；
  `stackflowos/plugins/__init__.py::PluginLoader` 的加载路径没有读取该字段做授权。
- `stackflowos/security/plugin_audit.py`（`permission_risk`/`high_risk_permissions`）
  与 `stackflowos/security/control.py`（`_start_plugin_audit`）—— 高危声明只记
  WARNING + `security.plugin_permissions` 审计事件（`enforced: false`）；测试见
  `tests/test_units_plugins.py`。
- `stackflowos/app.py::App.unit_cards`（`plugin.0` 逻辑服务）—— 插件宿主作为
  hub 内单元存在。
- `docs/architecture.md` / [README.md](../README.md) 的"边界与已知限制" —— 把
  "审批拆进程、不受信插件子进程"列为长期项，与本条一致。

**缓解**：只加载可审计来源的插件；把"贡献工具"继续纳入审批与事件审计；高危插件
单独评估或放到 hub 之外；审批依赖人时在运维上保证值守；对外部工具调用保持默认
审批（不要用 `always` 规则换便利）。

**是否计划内**：是（长期）。README 长期项列了"审批拆进程、不受信插件子进程"；
无排期。插件 `permissions` 强制不在计划内——当前信任模型就是 trusted in-process。

---

## 4. 多用户会话在内存、hub 重启需重新登录、长期 Bearer

**限制**：浏览器会话表 `SessionManager` 是进程内字典，不落盘；launch token 与
cookie 签名密钥每个进程随机生成、只在内存；`config.web.users` 的账号是长期
`token_sha256`；`Authorization: Bearer`（含 base token）在 launch token 兑换后仍被
接受，供 CLI/测试使用。

**影响**：hub 重启后所有 cookie 失效，用户要重新打开带 `?token=` 的登录 URL；
无法保留"已登录设备"；`SFO_WEB_TOKEN`/users token 一旦泄露就长期有效，撤销只能改
配置并重启；多用户共享同一 hub 的会话上限（`web.session_max`，满时新登录 503，
不驱逐活会话）。

**现状证据**：

- `stackflowos/security/sessions.py::SessionManager` —— 模块文档与类文档写明
  "in-memory session table"；`self._sessions: dict[str, Session] = {}`，无任何文件
  写入路径。
- `stackflowos/security/sessions.py::SessionsExtension` —— 暴露
  `/api/sessions`、`/api/sessions/rotate`、`DELETE /api/sessions/{id}`、
  `/api/logout`；响应只有会话元数据。
- `stackflowos/web/auth.py` 模块文档 —— "launch token and the cookie signing key are
  generated per process ... only ever live in memory"；`WebAuth.__init__` 的
  `launch_token or secrets.token_urlsafe(32)`。
- `stackflowos/web/auth.py::WebAuth.authenticate_bearer` /
  `::WebAuth._user_token_claims` / `stackflowos/config.py::WebCfg.users` —— Bearer
  与长期 `token_sha256` 账号的解析路径。
- `docs/team/sec.md` §9.2/§9.3、`docs/security-fix-plan.md`（"Cookie 签名密钥只在
  内存：hub 重启后 Cookie 失效"）。

**缓解**：日常用 `sfo url` 生成一次性登录 URL；把 `SFO_WEB_TOKEN` 只当 systemd/容器
的逃生门并定期轮换；远程访问必须 TLS（`web.tls_*`）；重启后重新走登录 URL；需要
持久会话/SSO 时引入外部 IdP 或持久 session 存储（当前代码没有）。

**是否计划内**：否（有意取舍，安全文档已记录）。持久会话是新产品能力，不在这轮
计划内。

---

## 5. projects/tasks 仍是 JSON 无事务；JSON 与 SQLite 不双写；fencing 尽力而为；租约只有 TTL+fencing

**限制**：

- projects/tasks 域保存在 `<state>/state/projects.json`：整域读写、写临时文件后
  `os.replace` 原子替换，没有事务日志/回滚；`storage.backend=sqlite` 只接管
  eventlog/memory/`state/counters`，`ProjectStore` 的 backend 只用于计数器；
- JSON 与 SQLite **不双写**：切换要显式 `python -m stackflowos.storage.migrate`，
  迁移后两边会随写入继续分叉；backend 写失败会回退 JSON（WARNING）；
- fencing token 是"尽力持久化"：磁盘只读/写满时 token 继续递增但不落盘，重启后
  可能重复；health 暴露 `fencing.persisted`/`persist_failures`；
- 租约本体是 hub 进程内表，只有 TTL + 时钟偏移容忍 + fencing token + 可抢占，
  没有分布式共识。

**影响**：单 hub 单 loop 下整域替换是安全的，但多个 hub 同时写会互相覆盖；跨
project/task/事件的"事务"不存在：`ProjectStore` 先落盘再发事件，崩溃可能留下"已
落盘但事件未写"的窗口；迁移后读哪份数据取决于配置，JSON/SQLite 可能给出不同历史；
fencing 落盘失败后重启可能发放重复 token；hub 重启会丢掉所有 lease，持有者需要
重新 acquire。

**现状证据**：

- `stackflowos/projects/store.py` —— 模块文档写明 `projects.json` 布局、每次
  mutation 先持久化再发事件、copy-on-write update、**"store is not thread-safe by
  design; one asyncio loop"**；`ProjectStore.save()` 用 `write_json` 写整个域。
- `stackflowos/projects/store.py::ProjectStore`（backend 注释与
  `_read_counters`/`_write_counters`）—— 注释写明 backend **只用于 id counters**；
  backend 失败回退 `counter_path`。
- `stackflowos/storage/migrate.py` 模块文档 —— `projects.json` **默认不迁移**；
  `--include-projects` 只是把文件复制成 `state/projects` 的 KV 值"for a future
  migration"；运行期不读 SQLite 的 projects 副本。
- `stackflowos/storage/consistency.py`（新增只读分叉检查）与
  `tests/test_storage_consistency.py` —— 文档明确 "nothing dual-writes"，并检查
  迁移后/回退后的 JSON 与 SQLite 差异（`python -m stackflowos.storage.consistency
  --state-dir .stackflowos`，退出码 0/1/2）。
- `stackflowos/l2/leases.py` 模块文档 —— fencing token 的持久化是 best effort；
  `LeaseManager.health()` 返回 `fencing.enabled/persisted/persist_failures`；
  `LeaseManager` 用 `self._leases: dict[str, Lease] = {}` 在内存维护租约。
- `stackflowos/app.py::App.__init__` —— `LeaseManager(..., fencing=cfg.cluster.
  lease_fencing, clock_skew_s=..., fencing_path=...)`，配置已接线（不是环境变量
  兜底）。
- `docs/team/dist.md` §9.5/§9.6/§9.10 —— 与本条一致。

**缓解**：保持单 writer（冷备语义）；切换 SQLite 前后跑 `storage-migrate` 和
`storage.consistency`；把 `fencing.persisted=false` / `persist_failures>0` 接告警；
关键项目状态用备份/外部快照兜底；不要在多 hub 下共享同一个状态目录。

**是否计划内**：部分。README 长期项列了"projects/tasks 全量事务化"（无排期）；
"不双写 + 显式迁移"是设计选择，不做自动双写；TTL+fencing 的租约模型保留。

---

## 6. 备份不是一致性快照；config.json 默认脱敏、可选 include_secrets

**限制**：`sfo backup` 在 hub 运行中逐文件遍历打包，没有全局停写/快照点，不同文件
可能来自不同瞬间；备份包含 `config.json`、`state/`（规则/审批/记忆）和项目工作区。
默认对 `config.json` 的敏感字段做 `***` 脱敏（`include_secrets=False`）；
`include_secrets=True` 才原样写入。恢复时，`***` 会用目标机器现有配置回填，目标
没有的 secret 保持 `***`。

**影响**：运行中备份可能得到"事件 seq 与 projects 状态不在同一瞬间"的组合；需要
严格一致时只能停 hub 或接受跨瞬间窗口。脱敏备份恢复到全新机器时，缺失的 secret
需要手工补；归档整体仍含业务数据/审批记录，属敏感文件。

**现状证据**：

- `stackflowos/observability/backup.py::create_backup` —— 参数
  `include_secrets: bool = False`；docstring 写明默认把 secret-looking 字段打成
  `***`，`True` 才 verbatim；打包循环逐文件读取（无全局锁/停写）。
- `stackflowos/observability/backup.py::_redacted_config_payload` /
  `::_redact_secret_tree` —— 只对 `is_secret_key` 命中的字段整体打码；无法解析/
  超大的 config 直接跳过而不是原样写入。
- `stackflowos/observability/backup.py::_restore_masked_secrets` —— restore 时把
  `***` 与目标现有值合并，并统计 restored/missing。
- `stackflowos/observability/backup.py` 模块文档（备份布局、排除 `run/tmp/logs/
  backups`）与 `_INCLUDE_ENTRIES`。
- `tests/test_ops_backup.py`（默认 `include_secrets=False` 脱敏、显式 `True` 才
  明文、manifest 记录）—— 脱敏行为的回归路径。
- `docs/team/ops.md` §4（一致性 + `config.json` 脱敏说明）、§9.8（脱敏/0600/
  非快照）—— 与本条一致。

**缓解**：严格一致场景先停 hub（或维护窗口）再备份；定期做 restore 演练；备份文件
保持 0600/异地加密；需要明文 secret 时用 `create_backup(..., include_secrets=True)`
（`sfo backup` 是否暴露该开关以 CLI 当前实现为准）并单独保管；恢复后检查 missing
secret 并补齐。

**是否计划内**：否。运行中一致性快照需要存储层配合，当前取舍是"停 hub 或接受跨
瞬间"；脱敏已作为默认行为（以 `create_backup` 为准）。

---

## 7. chat/agent chat 的 delta 是临时事件，不可回放

**限制**：`chat.delta`、`agent.chat.delta`、任意 `*.delta`、`stream.chunk` 是
transient 事件：分配 `seq` 并实时广播，但**不写盘**；replay（`since`/`query`）会
跳过它们，`health()` 用 `replay_skipped_transient` 计数。

**影响**：SSE 断线期间丢失的增量无法从事件历史补回；重连后只能等最终消息或快照；
不能从事件日志重建"半截流式文本"；前端在重连时会丢弃草稿（见第 10 条），改由最终
消息渲染一次。

**现状证据**：

- `stackflowos/l2/eventlog.py::EventLog.EPHEMERAL_TYPES`（含 `chat.delta`、
  `agent.chat.delta`、`stream.chunk`）与 `EventLog.is_transient()`（额外匹配
  `*.delta` 后缀）。
- `stackflowos/l2/eventlog.py::EventLog.append` —— transient 事件只广播/进内存，
  持久化分支用 `if not ephemeral` 跳过。
- `stackflowos/l2/eventlog.py::EventLog._skip_replay_transient` 与
  `::EventLog.health` —— replay 时跳过 transient 并计数。
- `stackflowos/storage/consistency.py` —— JSON/SQLite 一致性检查同样忽略 transient
  行（避免把设计内的临时事件误报为分叉）。
- `webui/src/state/store.ts`（SSE `open` 处理）—— 注释写明 "Ephemeral deltas are
  not replayed"，重连后清掉旧草稿、等快照/最终消息。

**缓解**：以最终 `chat.message` / `agent.chat.message`（或其快照）为准；UI 重连后
刷新状态；需要完整流式历史时由调用方/网关自行记录（当前代码没有该能力）。

**是否计划内**：否（设计取舍：避免把半截消息写进审计/回放）。

---

## 8. 无 OTel 导出、指标重启归零；通知尽力而为

**限制**：

- 没有 OpenTelemetry exporter（无 otel 依赖、无 OTLP 上报），只有 Prometheus 文本
  `GET /api/metrics`；
- 指标是进程内计数器（HTTP/SSE/队列等用 `WeakKeyDictionary`/内存结构），hub 重启
  归零；`sfo_events_total`/`event_seq` 这类基于持久 seq 的指标除外；
- 指标口径：`sfo_http_requests_total` 只统计经过 API 路由的 `/api/*`，`/healthz`、
  静态资源和 SSE 建连不计（在线 SSE 看 `sfo_sse_clients`）；队列深度无来源时输出
  `-1`，另有 `sfo_agent_queue_known` 0/1；
- webhook 通知尽力而为：默认重试 1 次（可配，代码上限 3 次）、有界待发/在途队列、
  失败只记 `approval.notified` 事件，不落盘重投、hub 停时无补发。

**影响**：无法直接接入 OTel 后端/分布式追踪；重启后计数断档，跨重启趋势要用事件
日志或外部存储；通知可能丢/重复（接收方需要按事件 seq 去重）；hub 停机窗口的通知
不会补发。

**现状证据**：

- `stackflowos/observability/metrics.py` 模块文档（Prometheus 文本、内存计数、
  fail-open）与 `_http_counts`/`_sse_clients`（`weakref.WeakKeyDictionary`）；
  `::build` 注册 `GET /api/metrics`；`sfo_agent_queue_depth`/`sfo_agent_queue_known`
  的新口径见该文件当前实现。
- `stackflowos/observability/notifications.py` 模块文档 + `_MAX_INFLIGHT = 32` /
  `_MAX_PENDING_TASKS = 256` / `Sink.retries`（默认 1，`min(3, ...)`）；
  `NotificationExtension` 的失败落 `approval.notified`。
- `pyproject.toml::[project].dependencies` 只有 `pynng`，没有 OTel SDK。
- `docs/team/ops.md` §9.2（进程内计数器、重启归零）、§9.6（通知尽力而为）、
  §9.8；`docs/architecture.md`（OpenTelemetry 导出未做）。
- `docs/operations.md` §6 监控条目（Prometheus 抓取 `/api/metrics`）已补口径说明。

**缓解**：用 Prometheus 抓取 + 告警规则；关键审计读 eventlog，不依赖指标；通知
接收方按 seq 幂等；需要 OTel 时在 hub 外做桥接/自建 exporter（当前代码没有）。

**是否计划内**：部分。README 长期项列了"OpenTelemetry 导出"（无排期）；
指标进程内/重启归零与通知尽力而为是当前取舍，不计划改成持久计数/可靠队列。

---

## 9. 测试覆盖缺口：沙箱/组播/pyzmq/真实 LLM/IPv6；仅 Linux 沙箱

**限制**：

- **沙箱**：`tests/test_sec_sandbox.py` / `tests/test_agent_sandbox.py` 在 Landlock
  不可用（或非 Linux）时 skip；本机是否真跑取决于内核。沙箱只有 Linux 后端。
- **真实组播**：`tests/test_dist_discovery.py` 的真实 mDNS 冒烟要
  `SFO_TEST_MDNS=1` 才跑；默认只覆盖编解码/状态机。
- **pyzmq**：`tests/test_dist_zmq.py` 在未安装 pyzmq 时 skip；桥接的真实 socket
  组合未在本机验证（`docs/team/dist.md` §9.9 也记录）。
- **真实 LLM**：`tests/test_llm.py` 用本地 stub HTTP server，`tests/test_llm_api.py`
  用 `mock` provider；仓库内没有对真实网关的联网测试，真实兼容性靠手工 smoke。
- **IPv6**：`tests/test_sec_net.py::IPv6PinnedFetchTests` 在 `socket.has_ipv6` 为
  false 或绑定 `::1` 失败时 skip。
- **测试基础设施**：本机可用解释器是 `/tmp/sfo-venv/bin/python`（pytest 9.2 + pynng，
  未装 pytest-subtests）；`uv` 隔离环境看不到系统 pynng，不要承诺 `uv run` 开箱即用。

**影响**：上述路径的 bug 不会在默认测试里暴露；发布前需要目标环境手工验证，否则
"测试全绿"不等于这些路径可用。

**现状证据**：

- `tests/test_sec_sandbox.py`（`skipUnless(sys.platform.startswith("linux"))`、
  Landlock 不可用 `skipTest`）、`tests/test_agent_sandbox.py`（`_has_landlock()`）。
- `tests/test_dist_discovery.py`（`skipUnless(os.environ.get("SFO_TEST_MDNS") ==
  "1", ...)`）。
- `tests/test_dist_zmq.py`（`skipUnless(z.available(), "pyzmq not installed")`）。
- `tests/test_llm.py`（本地 `_Server`/`base_url=http://127.0.0.1:...`）、
  `tests/test_llm_api.py`（`provider="mock"`）。
- `tests/test_sec_net.py::IPv6PinnedFetchTests`（`socket.has_ipv6` / `::1` 失败
  skip）。
- `stackflowos/security/sandbox.py::detect_landlock`（非 Linux 直接不可用）、
  `::audit_startup`（降级写 `sandbox.unsandboxed`，让"没生效"可见）。
- `scripts/test-report.sh`（JUnit 汇总 passed/failed/skipped 并按 skip reason 分组）、
  `scripts/README.md`、`pyproject.toml::[tool.pytest.ini_options]`（`addopts="-rs"`，
  `testpaths=["tests"]`）、`tests/conftest.py`（tmp/socket 路径 helper，IPC 路径
  >100 字节时 skip 并给原因）。

**缓解**：发布前用 `scripts/test-report.sh` 看 skip 原因；在目标环境手工 smoke：
真实网关、真实组播（`SFO_TEST_MDNS=1`）、装 pyzmq 后的桥接、IPv6 环境；CI 若存在，
应保留 skip 报告而不是把 skip 当通过。

**是否计划内**：部分。测试基础设施（dev extras、conftest、report 脚本）已补；真实
环境/真实 provider 验证按发布流程做，不计划全部塞进默认测试；仅 Linux 沙箱保留。

---

## 10. 前端：i18n 主路径、流式单草稿、依赖图固定布局、移动端未真机测试

**限制**：

- **i18n**：字典 zh/en 各 632 键 + 8 个屏各自的 `keys/<screen>.ts`；`zh` 是源，
  缺失的 `en` 回退 zh。`webui/src/api/client.ts` 的网络/401/HTTP 错误文案仍是中文；
  后端事件/动态文本不翻译。门禁 `tests/test_front_i18n.py` 只保证键存在、
  `t('...')` 字面键可解析，不保证所有 UI 文案都走字典。
- **流式单草稿**：核心 store 只有一个 `StreamState`（`stream: {active,text,
  startedAt,messageId}`）；Agent 对话 `useAgentChat` 也只有一个 `draft`。同一
  Agent 并发一轮返回 409，重连会丢草稿、等最终消息。
- **依赖图固定布局**：`DependencyGraph.tsx` 明确"不是 layout engine"——固定宽高/
  列位，一个单元在左、依赖在右，多依赖按行高堆叠，不做力导向/自动缩放。
- **移动端**：窄屏只在桌面 Chrome 的 420×900 视口截图检查
  （`docs/team/front.md` §4、`webui/scripts/shot.mjs`），没有真机/iOS Safari/
  Android 测试；响应式主要靠 Astryx 组件和少量 `useMediaQuery`。

**影响**：英文界面仍会看到中文错误/动态文本；多 Agent 同时回复时不能并行渲染草稿
（产品上就是"一次一轮"）；大依赖图可读性有限，需要靠下方 token/表格兜底；触屏和
移动浏览器行为未经验证。

**现状证据**：

- `webui/src/i18n/index.ts`（zh 为源、en 缺失回退 zh 的规则）。
- `webui/src/i18n/keys/`（8 个屏字典）、`webui/src/i18n/dict.json`（zh/en 各
  632 键）、`webui/src/api/client.ts`（中文错误文案）。
- `tests/test_front_i18n.py`（键完整性/`t()` 可解析门禁）。
- `webui/src/state/store.ts::StreamState` 与 `SfoState.stream`；
  `webui/src/screens/agents/useAgentChat.ts::ThreadState.draft`。
- `webui/src/screens/units/DependencyGraph.tsx`（文件头注释与 `WIDTH/
  ROW_HEIGHT/DEP_X` 等常量）。
- `docs/team/front.md` §4（桌面 1440×900、窄屏 420×900 的 Playwright/Chrome
  截图流程）；`webui/scripts/shot.mjs`。

**缓解**：中文作为主控台语言、英文覆盖主路径；大依赖图用列表/详情兜底；移动端用
桌面窄视口验收，真机问题按 bug 处理；并发流保持"一次一轮"的产品约束。

**是否计划内**：否（当前前端取舍）。i18n 全量、并行流、自动图布局、真机矩阵都
属于未排期改进。

---

## 附：如何复核与更新本文

```bash
# 例：确认本条证据仍在（符号搜索比行号稳定）
grep -n "def _acquire_hub_lock" stackflowos/app.py
grep -n "EPHEMERAL_TYPES\|def is_transient" stackflowos/l2/eventlog.py
grep -n "def create_backup\|include_secrets" stackflowos/observability/backup.py

# 存储分叉只读检查
python3 -m stackflowos.storage.consistency --state-dir .stackflowos

# 测试全量（本机用带 pynng 的解释器；skip 原因会汇总）
/tmp/sfo-venv/bin/python scripts/test-report.sh
```

更新规则：

1. 修改本文引用的代码时，在同一个改动里更新对应条目；删掉代码就删掉条目，不要
   留下"曾经没有"的断言。
2. 新限制必须能指出代码位置（`路径::符号`）和影响；没有代码证据的猜测不要写进来。
3. 如果某条已修复（例如排队 drain、rollback 保护、备份脱敏），从限制清单移除或
   改成"已修复 + 回归测试路径"，不要继续写成"没有"。

相关文档：[README.md](../README.md)、[self-hosting.md](self-hosting.md)、
[operations.md](operations.md)、[architecture.md](architecture.md)、
[team/dist.md](team/dist.md)、[team/sec.md](team/sec.md)、[team/ops.md](team/ops.md)、
[team/front.md](team/front.md)。
