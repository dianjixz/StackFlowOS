# 运行与运维

本文回答"怎么把它跑起来、跑在哪、出了事看哪里"。架构与接口见
[architecture.md](architecture.md)。

---

## 1. 单机启动

```bash
python3 -m stackflowos doctor          # 先确认端口/目录/pynng
python3 -m stackflowos hub             # IPC 模式，Web 控制台 127.0.0.1:17800
python3 -m stackflowos hub --transport tcp --port 17800
```

前台运行时日志同时写 stderr 与 `.stackflowos/logs/stackflowos.log`。
Ctrl-C 会优雅关闭：先停 Agent 进程，再停失败域、聊天、传输，最后清理 IPC socket。

### 状态目录

```
.stackflowos/
├── config.json              # 运行时配置覆盖（UI 设置保存到这里）
├── run/                     # IPC socket（sys.rpc.sock / sys.events.sock / <work_id>.*.sock）
├── events/
│   ├── events.jsonl         # 当前事件流（append-only）
│   ├── events-<seq>-<ts>.jsonl.gz   # 压缩归档（storage.eventlog_rotate_*）
│   └── .last_seq            # 持久化序号水位（防重启重号）
├── logs/
│   ├── stackflowos.log
│   └── agents/<work_id>.log # 每个 worker 进程的 stdout/stderr
├── state/
│   ├── projects.json        # 项目/任务（原子替换）
│   ├── counters.json        # p-/t-/m- 计数器
│   ├── rules.json           # 放行/拒绝规则与 granted
│   ├── approvals.json       # 待审批 + 最近决策
│   ├── agents.json          # Agent 花名册快照
│   ├── chat.json            # 最近对话
│   ├── llm_usage.json       # token/成本聚合（按项目/角色/provider/model）
│   ├── skills.json          # 全局/项目级 skill 启用状态
│   ├── lease_fencing.json   # 租约 fencing token 水位
│   └── memory/              # 项目记忆键值（或 SQLite 后端）
├── backups/                 # sfo backup 归档（0600）
├── projects/<p-id>/workspace/   # Agent 的全部产物
└── tmp/                     # 测试/临时文件
```

可选 SQLite 后端（`storage.backend=sqlite`）把 eventlog/memory/counters 放进
`state/stackflowos.db`（WAL + `user_version` 迁移）；projects/tasks 域仍是 JSON。

**备份**：推荐 `sfo backup`（默认 `state/backups/`，manifest+sha256），不要直接拷运行中的目录。
**恢复**：`sfo restore <archive>`（目标非空需 `--force`，会先自动备份当前状态）。
**导出**：`sfo export-events --format jsonl|csv --since <seq> --types approval.*`。
**重置**：停 hub 后删除 `.stackflowos/`；注意这会删掉项目工作区。

---

## 2. 端口

| 端口 | 用途 |
|---|---|
| `17800` | Web 控制台（HTTP + SSE），默认只绑 127.0.0.1 |
| `17820`–`17899` | TCP 模式下 sys 与各单元的 RPC/事件端口池 |

IPC 模式不占端口。端口池可在设置 → 网络里改；改完需重启 hub。

---

## 3. 进程模型与观察

- hub：一个进程，`work_id = sys.0`；同一 state 目录只允许一个 hub
  （`run/hub.lock` 独占，第二个会直接报错退出）。
- worker：每个 Agent 一个进程，由 supervisor 用
  `python -m stackflowos.worker` 拉起；日志在 `.stackflowos/logs/agents/`。
- 查看 `ps -ef | grep stackflowos.worker` 能看到每个工人的真实进程。
- 单元表（UI「单元」或 `GET /api/units`）里能看到 work_id、pid、地址、能力、心跳。
- 某个单元 offline 后，失败域会释放租约、按策略重启或隔离；相关任务会回到
  `todo` 供主理人重新派工。

### 故障排查顺序

1. **面板无数据**：`python3 -m stackflowos status` 能否拿到 `/api/state`；
   不能则 hub 没起或端口被占（`ss -ltnp | grep 17800`）。
2. **Agent 一直 spawning**：看 `.stackflowos/logs/agents/<work_id>.log`；
   常见原因是 sys RPC 地址不可达、端口池耗尽、项目不存在。
3. **任务卡在 waiting/审批**：UI「审批」里看 pending；规则没覆盖到当前
   scope。批准后 Agent 下一次轮询就会继续（默认 0.5s）。
   **对话仍提示离线模型**：确认设置页 provider=openai 且已保存；新版保存后会
   立即重建 provider，无需重启。用「测试连接」看具体 HTTP/连接错误。
4. **IPC 地址冲突/陈旧 socket**：停掉旧进程；allocator 启动时会清理自己的
   run 目录；必要时删除 `.stackflowos/run/*.sock`。
5. **事件流太大**：轮转文件可归档；`events.jsonl` 是权威审计，别随手删。
6. **端口池耗尽**：确认没有泄漏的 worker 进程；重启 hub 会回收。
7. **接入已有目录**：项目页「接入已有目录」或 `POST /api/projects/attach`
   `{path, name?, allow_writes?}`。默认只读；外部目录不会被删除。
   读写模式可用 `POST /api/projects/{id}/access` `{allow_writes}` 切换，
   或在对话里说“把 <项目名> 改成可写/只读”。
8. **清理项目**：项目详情右下角「归档」（保留工作区）或「删除项目」
   （输入项目名确认，永久删除记录/任务/工作区）。Agent 通过 `project.delete`
   工具发起时风险为 high，必须走审批；确认名不匹配会被拒绝。
9. **过期审批一直挂着**：hub 每秒 sweep 过期项；Agent 死亡后其 pending 审批
   也会被自动 deny。重启 hub 会清理孤儿审批。

---

## 4. 跨设备（TCP）

两种传输是同一套 NNG 协议。跨设备时：

1. hub 配置 `transport.mode = "tcp"`，`transport.host` 设为设备可达地址，
   `transport.advertise_host` 设为**远端能拨到的 hub 地址**（例如局域网 IP）。
   非 loopback 的 `transport.host` 必须同时显式设置
   `transport.allow_remote = true`；默认只允许 loopback，通配/网卡地址会被配置校验拒绝。
   Web 控制台若也要远程访问，用 `--host <具体IP> --allow-remote`（即
   `web.allow_remote = true`）。**远程必须启用 TLS**：设置 `web.tls_cert` /
   `web.tls_key`（或启动参数 `--tls-cert/--tls-key`），控制台会直接用 HTTPS 监听并给
   cookie 加 `Secure`；否则只应走 SSH 隧道/本机反代，启动时会打印明文告警。
2. 放行防火墙上的 sys RPC 端口与单元端口池。
3. 远端设备安装同一份代码（需要 `pynng`），在本地有自己的 `.stackflowos/`；
   用 attach 接入：
   ```bash
   stackflowos attach --sys tcp://<hub-ip>:17820 --role coder --project p-1
   ```
   它会向 hub 申请 work_id 与地址，在本机起一个 worker 进程；任务结束自动退出。
   也可以手动指定 `--task t-3`。

设备发现（可选）：设置 `cluster.discovery = "mdns"` 后，hub 通过标准库组播 DNS
广播 `_stackflowos._tcp.local`（TXT 带 node_id/transport/http_port/version），
`GET /api/discovery/peers` 列出发现的节点；`stackflowos.transport.discovery.discover_hubs()`
可供 worker/CLI 自动找 hub。默认 `off`，不引入额外依赖。

sys 仍是单点：`cluster.standby = true`（或 `hub --standby`）会启动一个**同状态目录冷备**
进程，它在 hub.lock 上等待，活动 hub 退出后接管。没有 quorum/复制，切换期间短暂不可用；
这是进程级 failover，不是 Raft。

---

## 5. 作为服务运行（systemd --user，推荐）

桌面用户不需要 root。`nohup` 只挡 SIGHUP，挡不住注销/会话清理的 SIGTERM；
而 hub 被 SIGTERM 时会**优雅退出（exit 0）**，所以 `Restart=on-failure` 不会拉起它。
必须用 `Restart=always`，并启用 linger（注销后仍存活）：

```ini
# ~/.config/systemd/user/stackflowos-hub.service
[Unit]
Description=StackFlowOS Hub
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
WorkingDirectory=/path/to/StackFlowOS
Environment=PYTHONUNBUFFERED=1
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
# 可选逃生门：无 TTY 且无法用 `sfo url`（control socket 不可用）时才需要。
EnvironmentFile=-%h/.config/stackflowos/web.env
ExecStart=/usr/bin/python3 -m stackflowos hub --port 17800
# 存活探针用匿名的 /healthz：它不需要登录 token，只报告 status/transport，
# 所以重启不会因为鉴权而失败（H1 之后 /api/health 需要 token）。
ExecStartPost=/usr/bin/curl -fsS -o /dev/null --connect-timeout 2 -m 3   --retry 30 --retry-delay 1 --retry-connrefused   http://127.0.0.1:17800/healthz
Restart=always
RestartSec=3
TimeoutStartSec=60
TimeoutStopSec=15
KillMode=control-group

[Install]
WantedBy=default.target
```

**登录 URL（非 TTY 部署，推荐）**：hub 在 `state/run/control.sock`（0600，
`SO_PEERCRED` 同 uid）提供 operator 控制面。同一台机器的终端里执行：

```bash
stackflowos url                 # 铸造一次性登录 URL
stackflowos url --status        # 查看 hub URL/TLS/会话数（不含数据）
```

URL 中的 launch token 只能兑换一次；兑换后浏览器拿到 HttpOnly/SameSite cookie。
这个设计不需要把 `SFO_WEB_TOKEN` 写盘，也适合 systemd/容器。

**逃生门**：若 control socket 不可用（非 Linux、无 `SO_PEERCRED`、跨容器挂载），
再显式设置 `SFO_WEB_TOKEN`（0600 env file）。它是长期 secret，同用户进程可读，
只作为最后手段；`Authorization: Bearer <token>` 仍可用于 CLI/自动化。

```bash
systemctl --user daemon-reload
systemctl --user enable --now stackflowos-hub.service
loginctl enable-linger "$USER"   # 注销/重启后用户服务仍启动
systemctl --user status stackflowos-hub.service
journalctl --user -u stackflowos-hub -f
```

建议再加一个 health timer（每分钟探活，卡死也能重启），或至少监控
`/healthz` 中的 `transport` 字段：HTTP 线程活着不代表 pynng IPC 活着。
`/healthz` 不需要 token（`/api/health` 需要），适合 systemd/监控脚本直接调用。
注意只重启“活着但不响应”的 hub：单元已经 `inactive`/`failed` 时交给 systemd 的
`Restart=always` + `StartLimitBurst` 处理，否则会跟手动 `stop` 打架，而且每分钟
`reset-failed` 会清掉启动限流，把一次启动失败变成整夜重试：

```bash
# ~/.local/bin/stackflowos-health.sh
unit=stackflowos-hub.service
state=$(systemctl --user is-active "$unit" 2>/dev/null || true)
[ "$state" = "active" ] || { echo "hub is ${state:-unknown}; leaving to systemd" >&2; exit 0; }
curl -fsS -m 3 --retry 2 --retry-delay 2 --retry-connrefused \
     -o /dev/null http://127.0.0.1:17800/healthz \
  || systemctl --user restart "$unit"
```

```bash
systemctl --user stop stackflowos-hub   # 手动 stop 不会被 always 拉起
systemctl --user restart stackflowos-hub
```

若需要 Web 暴露到局域网，建议前面加一层带鉴权的反代（Caddy/Nginx），并且
**不要**直接暴露 sys RPC 端口给不可信网络。

---

## 6. 安全加固清单

- [ ] Web 默认只绑 `127.0.0.1`；远程访问必须 TLS（`web.tls_*`）或 SSH 隧道。
- [ ] 多用户部署在 `web.users` 配置 `token_sha256` + `operator|viewer` 角色；
      viewer 只读，写操作在 admission/server 两层拒绝。
- [ ] 启用的 shell/worker 沙箱：`security.sandbox = "auto" | "landlock" | "bwrap"`。
      Landlock 限制写路径（workspace/tmp/logs）并在 ABI≥4 时 deny TCP；
      bwrap 需要非特权 user namespace，失败会明确记录 “unsandboxed”。
- [ ] 事件/审批通知：`observability.webhooks` 配置 IM/告警 webhook；
      `observability.notify_events` 控制转发类型。
- [ ] 监控：Prometheus 抓取 `GET /api/metrics`（需 Bearer token；文本格式）。
- [ ] 定时 `sfo backup`，并把 `state/backups/` 复制到异地；备份含 config（0600）。
- [ ] sys RPC/端口池只对受信网段开放。
- [ ] 项目工作区所在磁盘单独做配额/快照；Agent 的产物都在那里。
- [ ] 定期审阅「审批 → 规则」：`always` 规则等于长期授权，应设过期时间。
- [ ] 高风险能力（`shell.exec`、`fs.delete`、`unit.control`、`sys.config`）
      默认不写自动放行规则。
- [ ] 不要把 API key 写进仓库；用环境变量或运行时配置（UI 里显示为 `***`）。
- [ ] 定期备份 `.stackflowos/state` 与 `events`。

---

## 7. 升级 / 迁移

- 代码升级后重启 hub；`.stackflowos/state` 的数据格式在 `version: 1` 内兼容。
- 迁移机器：优先 `sfo backup` → 新机器 `sfo restore`；或停 hub 后拷整个目录。
  IPC 地址会按新路径重新分配（地址不是持久身份，`work_id` 才是）。
- 如果改了 `transport.mode` 或 `web.port`，设置页会提示 `restart_required`。
- 切换到 SQLite：先 `sfo storage-migrate --dry-run` 看差异，再
  `sfo storage-migrate`；设置 `storage.backend = "sqlite"` 后重启 hub。
  JSON 文件仍保留（可回退），但事件序号以数据库水位为准。
- 事件轮转由 `storage.eventlog_rotate_bytes / eventlog_rotate_keep` 控制；
  跨归档的 `since`/查询/SSE 续传会自动回放，不需要手工拼接。

## 8. 单元、能力与 Agent 控制面

- 单元页/API：`GET /api/units`（含 summary）、`GET /api/units/{id}`（描述符 + 依赖 +
  20 条状态事件）、`POST /api/units/{id}/probe`、`POST /api/units/{id}/actions`
  （`probe|restart|disable|enable|drain|undrain`，只接受描述符声明的动作）。
  服务单元是 in-process 逻辑单元，不做假 pid/地址；进程化失败域见 units-plan Phase 3。
- 能力目录：`GET /api/capabilities?type=tool|skill|plugin|mcp_tool&project_id=`。
  工具由 `tool.0` 提供，skill 由 `skill.0`，插件每个 `plugin.<id>`，MCP 每个
  `mcp.<name>`；`available` 由 provider 单元状态实时推导。
- 技能：`config.skills.dirs` 下放 `SKILL.md` 或 `skill.json`；`GET /api/skills`，
  `POST /api/skills/{id}/enable|disable`（可选 `project_id`）；启用的技能按 trigger/
  角色/项目注入 Agent system prompt。
- 插件：`config.plugins.dirs` 下放 `stackflow.plugin.json`；受信 in-process 插件
  通过 Cordis effect 注册/注销，`POST /api/plugins/{id}/load|unload`。
- MCP：`config.mcp.servers[]` 配置 stdio server；`GET /api/mcp/servers`，
  `connect/disconnect`，`GET /api/mcp/tools`，`POST /api/mcp/call`（走审批/审计）。
- Agent：`GET /api/agents/queue` 查看按优先级排队的派工；`agents.max_workers` 满时
  自动排队、worker 退出补位；`agents.task_timeout_s` / `max_tool_calls_per_task`
  超限转为 blocked；运行中 Agent 通过 `unit.configure` 热更新限制/模型。
  attached-rw 项目可开 `security.workspace_git`：每任务一个分支、任务结束提交，
  `GET /api/projects/{id}/git` 查看、`POST .../git/rollback` 回滚。
