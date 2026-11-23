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
│   ├── events.jsonl         # 当前事件流（append-only，8MB 轮转）
│   └── events-<seq>.jsonl
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
│   └── memory.json          # 项目记忆键值
├── projects/<p-id>/workspace/   # Agent 的全部产物
└── tmp/                     # 测试/临时文件
```

**备份**：停 hub 后拷贝整个 `.stackflowos/` 即可（事件流是权威审计）。
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
   `web.allow_remote = true`），并置于 SSH 隧道或 TLS 终止反代之后（TLS 监听见 Phase 5）。
2. 放行防火墙上的 sys RPC 端口与单元端口池。
3. 远端设备安装同一份代码（需要 `pynng`），在本地有自己的 `.stackflowos/`；
   用 attach 接入：
   ```bash
   stackflowos attach --sys tcp://<hub-ip>:17820 --role coder --project p-1
   ```
   它会向 hub 申请 work_id 与地址，在本机起一个 worker 进程；任务结束自动退出。
   也可以手动指定 `--task t-3`。

当前版本**没有设备发现**：地址要显式配置；sys 仍是单点；远端设备上的
worker 会绑定 hub 分配端口池中的端口，需要保证该端口在该设备上可用。
跨设备 PUB/SUB 的实际抖动还没有长时间实测（愿景文档已把这条列为未验证）。

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
# 非 TTY 部署要显式给一个登录 token（见下面“登录 token”）。文件缺失时
# systemd 忽略该行（前导 "-"），hub 仍会启动，只是没有可用的网页登录链接。
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

**登录 token（非 TTY 部署）**：H1 起 Web 控制台要求认证，`stackflowos hub` 只在
TTY 上打印本次进程的登录 URL；systemd 下没有 TTY，需要显式给一个 token：

```bash
mkdir -p ~/.config/stackflowos && chmod 700 ~/.config/stackflowos
python3 -c 'import secrets; print("SFO_WEB_TOKEN="+secrets.token_urlsafe(32))' \
  > ~/.config/stackflowos/web.env
chmod 600 ~/.config/stackflowos/web.env
```

之后用 `http://127.0.0.1:17800/?token=<token>` 登录一次（换成 HttpOnly Cookie），
或 `stackflowos status --token <token>` / `Authorization: Bearer <token>`。
注意：token 落盘为 0600，同用户进程可读；这是还没有 `sfo url` 控制套接字之前的
过渡方案（见 security-fix-plan §5.1.6），威胁模型里归为“同用户残余风险”。

```bash
systemctl --user daemon-reload
systemctl --user enable --now stackflowos-hub.service
loginctl enable-linger "$USER"   # 注销/重启后用户服务仍启动
systemctl --user status stackflowos-hub.service
journalctl --user -u stackflowos-hub -f
```

建议再加一个 health timer（每分钟探活，卡死也能重启），或至少监控
`/healthz` 中的 `transport` 字段：HTTP 线程活着不代表 pynng IPC 活着。
`/healthz` 不需要 token（`/api/health` 需要），适合 systemd/监控脚本直接调用：

```bash
# ~/.local/bin/stackflowos-health.sh
curl -fsS -m 3 --retry 2 --retry-delay 2 --retry-connrefused \
     -o /dev/null http://127.0.0.1:17800/healthz \
  || systemctl --user restart stackflowos-hub.service
```

```bash
systemctl --user stop stackflowos-hub   # 手动 stop 不会被 always 拉起
systemctl --user restart stackflowos-hub
```

若需要 Web 暴露到局域网，建议前面加一层带鉴权的反代（Caddy/Nginx），并且
**不要**直接暴露 sys RPC 端口给不可信网络。

---

## 6. 安全加固清单

- [ ] Web 只绑 `127.0.0.1`；对外必须加反代鉴权。
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
- 迁移机器：拷贝仓库 + `.stackflowos/`；IPC 地址会按新路径重新分配
  （地址不是持久身份，`work_id` 才是）。
- 如果改了 `transport.mode` 或 `web.port`，设置页会提示 `restart_required`。
