# ops 交接：可观测性（指标 / 通知 / 备份 / 日志轮转）

> 负责人：ops。文件所有权：`stackflowos/observability/**`、`stackflowos/util/logging.py`、
> `tests/test_ops_*.py`、本文件。**未改** `app.py`、`config.py`、`web/api.py`、
> `web/server.py`、`extensions.py` 或他人文件；未执行任何 git 写操作。

---

## 1. 改动文件

| 文件 | 内容 |
|---|---|
| `stackflowos/observability/__init__.py` | 共享工具：路径解析、递归密钥脱敏、数值容错、备份格式常量 |
| `stackflowos/observability/metrics.py` | Prometheus 指标扩展 + `GET /api/metrics` |
| `stackflowos/observability/notifications.py` | webhook 通知扩展 + `POST /api/notifications/test` |
| `stackflowos/observability/backup.py` | `create_backup` / `restore` / `export_events` + 备份扩展 |
| `stackflowos/observability/logging.py` | 按配置重配 hub 日志为 `RotatingFileHandler` |
| `stackflowos/util/logging.py` | `setup_logging(..., rotation=..., force=...)`，默认行为不变 |
| `tests/test_ops_metrics.py`、`test_ops_notifications.py`、`test_ops_backup.py`、`test_ops_logging.py` | 37 个测试 |
| `docs/team/ops.md` | 本交接 |

三个扩展由 `extensions.EXTENSION_MODULES` 自动按序加载，均实现了
`build(app)`，失败会被 `extensions.load_extensions` 隔离，不会阻止 hub 启动。

---

## 2. Prometheus 指标（`observability.metrics`）

### 路由

`GET /api/metrics`，和其它 `/api/*` 一样要经过 Web 认证。
`observability.metrics_enabled = false` 时扩展仍加载（`health()` 可见），但路由返回
`404 {"ok":false,"error":{"code":"NOT_FOUND",...}}`，不泄漏指标。

响应使用 `stackflowos.web.api.WebReply`：

```python
WebReply(text, content_type="text/plain; version=0.0.4; charset=utf-8")
```

实现要点：

* 抓取只读内存结构（`app.stats/unit_cards/supervisor/projects/approvals/leases` 等），
  不读磁盘、不发网络请求；每个 collector 单独 `try/except`，单个来源坏掉只丢该
  指标，不会让整个 scrape 500，也不会影响 hub。
* `# HELP` 中的 `\`、换行已转义；label 值中的 `\`、`"`、换行已转义；label 集合
  按名称稳定排序。
* 事件计数通过 `app.eventlog.subscribe()` 的独立协程消费；`stop()` 里
  `cancel + eventlog.unsubscribe()`，不泄漏订阅者。
* `sfo_events_total` 用 `eventlog.last_seq`（全局递增序列，重启后仍权威）；
  `sfo_approvals_decided_total` 启动时用 `eventlog.query(...)` 有界回填最近
  1000 条决定，之后由订阅实时累加（进程内计数器，重启归零）。

### 指标清单

| 指标 | 类型 | labels | 说明 / 来源 |
|---|---|---|---|
| `sfo_up` | gauge | – | hub 正在服务 scrape，恒 1 |
| `sfo_uptime_seconds` | gauge | – | `time.time() - app.started_at/1000` |
| `sfo_events_total` | counter | – | `eventlog.last_seq` |
| `sfo_event_seq` | gauge | – | 同上（当前最后一个 seq） |
| `sfo_units` | gauge | `state,tier` | `app.unit_cards()` 按状态/层级计数（含 sys.0） |
| `sfo_unit_restarts_total` | counter | – | 所有单元 `restart_count` 之和 |
| `sfo_agents` | gauge | `status,role` | `app.supervisor.list()`；不可用时退回 `stats()["by_status"]` + `role="unknown"` |
| `sfo_agent_queue_depth` | gauge | – | 依次探测 `queue_depth/queue_size/pending_dispatches/pending_count/queued`（可调用或属性/字典）；没有则为 0 |
| `sfo_projects` | gauge | `status` | `app.projects.list()` |
| `sfo_tasks` | gauge | `status` | `app.projects.task_list()`（全部项目） |
| `sfo_approvals_pending` | gauge | – | `app.approvals.pending_count()` |
| `sfo_approvals_decided_total` | counter | `decision` | 订阅 `approval.decided` / `approval.auto`；始终输出 allow/deny 两个 0 样本 |
| `sfo_leases_active` | gauge | – | `app.leases.list(active_only=True)` 长度 |
| `sfo_llm_tokens_total` | counter | `provider,model,kind` | 可选：探测 `app.llm_usage()/usage_snapshot()/usage`、`provider.usage`、`agent_chat.provider.usage` 等，见下 |
| `sfo_llm_cost_usd_total` | counter | – | 同上，`cost_usd/total_cost_usd/cost`；无 usage 时 0 |
| `sfo_sse_clients` | gauge | – | 通过包装 `WebServer._serve_sse` 统计当前 SSE 连接数；无来源时 0 |
| `sfo_http_requests_total` | counter | `method,status` | 通过包装 `WebAPI.handle` 统计 `/api/*` 请求；首次请求前只有 HELP/TYPE，无样本 |

**LLM usage 契约（给 llm 工作流）**：不需要 import 任何 llm 模块，指标扩展用
`getattr` 安全探测；已兼容：

```python
# 形式 A（推荐）
app.llm_usage() -> {"tokens": [{"provider":"openai","model":"gpt","kind":"prompt","count":12}],
                    "cost_usd": 0.03}
# 形式 B
{"tokens": {"openai": {"gpt": {"prompt": 12, "completion": 3}}}, "total_cost_usd": 0.03}
# 形式 C
{"prompt_tokens": 12, "completion_tokens": 3, "cost_usd": 0.03}
```

另外会直接探测所有 provider 共享的 `provider.runtime.usage.snapshot()`
（当前 `UsageTracker` 的 `totals/providers/models` 形状已适配，`cost/calls` 不会
被误当成 token）。没有 usage 时不输出 token/cost 样本（HELP/TYPE 仍在），符合
“llm 扩展若提供 usage”。

**HTTP/SSE 说明**：`web/api.py`、`web/server.py` 没有持久请求/连接计数器，而这两个
文件不归 ops 修改，所以指标扩展在**进程内一次性**包装
`WebAPI.handle` / `WebServer._serve_sse`（保留原返回值，带异常保护；已包装标记
避免重复）。只统计 `/api/*`，不含静态文件、`/healthz`、SSE 连接本身。若 sec 后续
给 server 增加显式计数器，可把包装换成读取属性，指标名不变。

---

## 3. Webhook 通知（`observability.notifications`）

### 配置

```json
{
  "observability": {
    "notify_events": ["approval.created", "approval.expired", "unit.failed"],
    "notify_timeout_s": 5.0,
    "webhooks": [
      {
        "type": "webhook",
        "name": "ops-hook",
        "url": "https://hooks.example/sfo",
        "events": ["approval", "unit.failed"],
        "headers": {"Authorization": "Bearer <secret>"},
        "timeout_s": 3,
        "retries": 1,
        "retry_backoff_s": 0.5,
        "rate_limit_per_s": 5,
        "burst": 5,
        "enabled": true
      }
    ]
  }
}
```

* 每个 sink 的 `events` 缺省时用 `notify_events`；显式 `[]` 表示不接收任何事件；
  事件类型支持点号前缀匹配（`"approval"` 匹配 `approval.created` 但不匹配
  `approvalbook`）。
* 只允许 `http://` / `https://`；其它 scheme / 缺 url 的 sink 会跳过并在 health
  `last_error` 中记录（不记录 URL/headers 原文）。
* 同名 sink 自动加 `-2` 后缀；`enabled=false` 跳过。
* 超时默认 `notify_timeout_s`，可被 sink `timeout_s` 覆盖，clamp 到 0.05–60s。
* `observability.webhooks` 结构在 `config.py` 已校验；上述可选键是 sink 内的扩展，
  由本模块容错读取。

### Payload

POST `Content-Type: application/json`（`Authorization` 等配置的 header 原样发送，
这是 sink 认证所需；绝不写日志）：

```json
{
  "type": "approval.created",
  "seq": 42,
  "ts": 1730000000000,
  "project_id": "p-1",
  "source": "sys",
  "data": {"request_id": "apr-1", "api_key": "***"},
  "hub": {"version": "0.1.0"}
}
```

`data` 在**发送前**递归脱敏：`api_key/token/secret/password/...` 整值替换为 `***`，
`Authorization: Bearer ...`、URL userinfo、常见 token 形状走
`util.redact.redact_text`；错误文本也脱敏后记录。通知事件本身
（`approval.notified`、`observability.notified`、`observability.notify_failed`）永不
转发，避免“通知自己的通知”造成风暴。

### 可靠性 / 非阻塞

* `start()` 订阅 `eventlog.subscribe()`，独立协程消费；事件 append 不会等待 webhook。
* 每个 sink 的 HTTP 调用在 `asyncio.to_thread` 里执行（stdlib `urllib.request`），
  不阻塞事件循环。
* 每 sink 一个异步 token bucket（`rate_limit_per_s` / `burst`）；同一 sink 并发受
  全局 `Semaphore(32)` 与待发送任务上限 256 约束，超限事件计入 `dropped` 并告警。
* 失败重试 1 次（`retries` 可 0–3），退避 `retry_backoff_s`；2xx 视为成功。
* 同一 `(sink, seq)` 只发一次（10k 有界去重窗口），避免重复事件源造成重复推送。
* 每个 sink 的**最终结果**写一条 `approval.notified` 事件：

```json
{"sink":"ops-hook","event_type":"approval.created","event_seq":42,
 "ok":true,"status":200,"attempts":1,"error":null}
```

  失败时 `ok=false`，`status/error/attempts` 供排查；事件里只有 sink 名，没有 URL、
  headers 或 payload 内容。
* `stop()` 取消消费协程/在途任务、`eventlog.unsubscribe()`；`to_thread` 中已发出的
  请求可能仍会完成（线程不可取消），进程退出前给短宽限即可。

### `POST /api/notifications/test`（operator）

```bash
curl -sS -X POST http://127.0.0.1:17800/api/notifications/test \
  -H "Authorization: Bearer $SFO_WEB_TOKEN" \
  -H "X-SFO-Request: 1" -H "Content-Type: application/json" \
  -d '{"message":"hello","sink":"ops-hook"}'
```

返回：

```json
{"ok": true, "data": {"sinks": 2, "sent_to": 1, "tested_at": 1730000000000,
  "results": [{"sink": "ops-hook", "ok": true, "status": 200,
               "attempts": 1, "latency_ms": 12, "error": null}]}}
```

`role=viewer` 会被拒绝（`Denied`/403）；当前 `AdmissionDecision` 没有 role 时，视为
进程 launch token 的 operator 身份。测试消息绕过 sink 的 `events` 过滤（目的就是
验证连通性），并同样写 `approval.notified`（`event_type="notifications.test"`）。

### `health()`

`app.health()["extensions"]["notifications"]`：

```json
{"ok": true, "sinks": 2, "running": true, "sent": 10, "failed": 0,
 "dropped": 0, "queue": 0, "last_error": null, "last_error_sink": null}
```

`last_error` 只含脱敏错误文本/配置问题描述；不暴露 URL 与 header。

---

## 4. 备份 / 恢复 / 导出（`observability.backup`）

### CLI 纯函数（Lead 在 `cli.py` 接线）

所有函数同步、无第三方依赖；`paths` 参数接受 `App`、`config.Paths` 或 state 目录路径。

```python
from stackflowos.observability.backup import (
    create_backup, restore, export_events, backup_status, list_backups,
)

create_backup(paths, dest=None, keep=None) -> Path
# dest 缺省 <state>/backups；keep 缺省 config.observability.backup_keep(7)，<=0 不清理

restore(archive, state_dir, force=False) -> dict
# 返回 {"state_dir","archive","files","bytes","replaced","backup","manifest":{...}}

export_events(paths, format="jsonl", *, since=None, until=None,
              types=None, project_id=None, dest=None) -> Path
# since 为 seq（不含），until 为 seq（含）；types 列表或逗号字符串，点号前缀匹配

backup_status(paths) -> dict
list_backups(paths) -> list[Path]   # 新的在前
```

建议 CLI（Lead 接线）：

```
sfo backup  [--dest DIR] [--keep N]                  -> create_backup(cfg.paths, ...)
sfo restore ARCHIVE [--force]                        -> restore(ARCHIVE, cfg.paths.state_dir, force=...)
sfo export-events [--format jsonl|csv] [--since N] [--until N]
                   [--types a,b] [--project P] [--out FILE] -> export_events(...)
sfo backup-status                                    -> backup_status(cfg.paths)
```

### 归档布局与策略

`<state>/backups/sfo-backup-YYYYmmdd-HHMMSS-<rand>.tar.gz`，权限 **0600**（state/
config 可能含密钥）；目录 0700。内容：

```
manifest.json                  # format=sfo-backup, version=1, app_version,
                               # created_at/ISO, includes/excluded, skipped,
                               # counts, files{arcname:{size,sha256,mtime}}
config.json                    # 存在才打包
state/...                      # projects/counters/rules/approvals/agents/chat/memory...
projects/<id>/workspace/...    # Agent 产物
events/...                     # events.jsonl + 轮转文件
```

**明确排除**：`run/`（socket/lock 是进程身份）、`tmp/`、`logs/`（体积大且事件日志才是
权威审计）、`backups/`（否则备份套备份）。符号链接/设备/FIFO **不跟随、不打包**，
跳过项写入 manifest `skipped`（最多 200 条）。

`create_backup` 在写 tar 的同时对实际写入的字节计算 sha256/size，manifest 与归档
内容天然一致；写临时文件（0600，mkstemp）后 `os.replace` 原子发布；`keep` 保留最新
N 个，按 mtime 修剪。

**一致性**：hub 运行中备份不做全局快照，文件可能在两次读之间变化。写入的字节与
manifest 一致，但不同文件可能来自不同瞬间；需要严格一致时先停 hub 或低频写入。
（manifest 不记录“正在写入”状态，`.json` 原子写 + 事件 append 单行通常可接受。）

### restore 安全

1. 只接受存在的普通文件，`tarfile.open(..., "r:gz")`；损坏/截断 → `ValidationError`。
2. 拒绝绝对路径（`/`、`\`、`C:`）、`..` 组件、NUL；每个成员再 `resolve()` 校验仍在
   暂存目录内。
3. 拒绝 symlink/hardlink/字符设备/块设备/FIFO/未知成员；拒绝重复成员。
4. 先读 `manifest.json`：`format`、`version` 必须匹配；每个文件要有 `size`+64 位
   hex `sha256`；归档成员集合必须与 manifest 完全一致（多/少都拒绝）。
5. 解包到 state 目录同级的临时 staging 目录，边写边校验 size + sha256；不通过就
   删除 staging，原 state 不动。
6. 目标非空且 `force=False` → `Conflict`（409）；`force=True` 时**先自动备份当前
   state**：备份先放到 `<state>.restore-backups`（因为 state 树马上被替换），成功
   换入后移到新 state 的 `backups/`，返回路径在 `result["backup"]`。
7. 通过 `os.replace` 原子换入；旧树先改名 `.pre-restore-<ts>-<pid>`，失败回滚，
   成功才删除。
8. 若被恢复的归档本身位于 state 目录下（默认 `state/backups`），通过 hardlink（失败
   则 copy）保留到新 state 的 `backups/`，不会“恢复完把唯一备份删了”。

### export

`<state>/exports/events-YYYYmmdd-HHMMSS.<jsonl|csv>`（0600，mkstemp + 原子替换）。
JSONL 每行一条原始 event 字典；CSV 表头 `seq,ts,type,source,project_id,data`
（`data` 为 compact JSON）。按 seq 排序输出；轮转文件按 seq 排序后接当前
`events.jsonl`。`since/until/types/project_id` 均可在内存中过滤。

### `health()`

`app.health()["extensions"]["backup"]`：

```json
{"ok": true, "dir": ".../state/backups", "backups": 3, "bytes": 12345,
 "last_backup": ".../sfo-backup-....tar.gz", "keep": 7}
```

---

## 5. 日志轮转

### `util/logging.py`（向后兼容）

```python
setup_logging(level="INFO", log_file=None, *, console=True, quiet=False,
              rotation=None, force=False) -> logging.Logger
```

* 不传 `rotation`：行为完全同旧版（普通 `FileHandler`）。
* `rotation`：`(max_bytes, keep)`、`{"bytes": n, "keep": k}` 或整数 bytes；
  `bytes <= 0` 仍是普通 `FileHandler`。
* `force=True` 重建 handler（测试 / 显式重配用）；幂等标记 `_sfo_file_handler`
  放在文件 handler 上。
* `app.start()` 现有调用 `setup_logging("INFO", self.paths.log_file)` 不变。

### `observability/logging.py`

扩展 `metrics.start()`（以及配置热更新时 `on_config`）调用：

```python
configure_from_app(app)  # 读 config.observability.log_rotate_bytes / log_rotate_keep
configure_file_logging(log_file, max_bytes=..., backup_count=...)
```

只移除**本 hub 日志文件**的 `FileHandler`（按 baseFilename 或 managed 标记），保留
console 和其它 handler；加入 `RotatingFileHandler` 并打日志
`observability.logging | hub log rotation: ...`。重复调用不会产生重复 handler。
`log_rotate_bytes=0` 时退化为普通 `FileHandler`（仍写文件，不轮转），`keep` 作为
`backupCount`。

默认配置 `32 MiB × 5`，实际启动验证：

```
log handlers [('StreamHandler', None), ('RotatingFileHandler', 33554432)]
```

---

## 6. 健康聚合

`App.health()` 通过 `extension_health()` 自动合并三个扩展：

```json
"extensions": {
  "metrics":       {"ok": true, "enabled": true, "subscribed": true, ...},
  "notifications": {"ok": true, "sinks": 2, "last_error": null, ...},
  "backup":        {"ok": true, "last_backup": "...", "backups": 3, ...}
}
```

`metrics_enabled=false` 时 metrics `ok=true, enabled=false`（主动关闭不算故障）。
`notifications.ok` 在最近一次投递成功后为 true，失败/flush 后带 `last_error`。
备份 health 只读目录，不创建文件。

---

## 7. 测试与验证

指定命令：

```bash
/tmp/sfo-venv/bin/python -m pytest -q \
  tests/test_ops*.py tests/test_web.py --basetemp=/tmp/pytest-ops
# 45 passed（38 个 ops + 7 个既有 web）
```

覆盖：

* metrics：全部指标 family/标签转义/禁用 404/WebReply content-type/订阅 stop 清理/
  HTTP+SSE 包装/两种 LLM usage 形状/`UsageTracker.snapshot()` 形状/无 usage/health。
* notifications：过滤、默认事件、重试成功、永久失败、payload 与审计脱敏、
  meta 事件不递归、test 路由、viewer 拒绝、stop 退订、非法 sink 跳过。
* backup：布局/排除项/manifest 哈希/0600/保留数/round-trip/自动安全备份/强制覆盖/
  路径穿越/绝对路径/symlink/损坏归档/校验和不匹配/额外成员/导出 JSONL+CSV/
  project 过滤/状态。
* logging：小阈值产生 `.1`、重配保留 console 且不重复、0 关闭轮转、
  `configure_from_app` 读取配置、rotation 参数形状。

另外手工做了真实 `App.start()` 冒烟：扩展按序加载
`metrics/notifications/backup`，`/api/health` 聚合正常，日志 handler 为
`RotatingFileHandler(33554432)`，停止后订阅者清零；并用真实 `WebServer` 发 HTTP
请求验证了 `/api/metrics`（`text/plain; version=0.0.4; charset=utf-8`）与
`/api/notifications/test`（JSON）；另外在真实 hub 中写入 `UsageTracker` 后确认
`sfo_llm_tokens_total`（prompt/completion/total + provider/model）和
`sfo_llm_cost_usd_total` 正确出现，`sfo_http_requests_total{method="GET",status="200"}`
随 `/api/metrics` 请求递增。

---

## 8. 给 Lead 的接线点

1. **CLI**（仅 `cli.py`，Lead 所有）：按 §4 的签名加 `backup/restore/export-events/
   backup-status` 子命令；`export_events` 的第一个位置参数用 `cfg.paths` 即可。
2. **无需改 app.py/web**：路由全部走扩展 `routes()`；SSE/HTTP 计数由指标扩展包装，
   不需要 server/cli 赋值 `app.web_server`。
3. 若 llm 工作流新增 usage 接口，优先提供 `app.llm_usage()` 返回 §2 形式 A；不需要
   改指标代码。若 agent 工作流新增派工队列，暴露 `app.supervisor.queue_depth()`
   或 `app.agent_queue.depth` 即可被 `sfo_agent_queue_depth` 拾取。
4. `/api/notifications/test` 的 operator 校验依赖 sec 在 `AdmissionDecision` 上加
   `role`；没有该字段时按 operator 处理，显式 `viewer` 会被拒绝。

---

## 9. 残余风险 / 已知取舍

1. **HTTP/SSE 指标靠运行时包装** `WebAPI.handle` / `WebServer._serve_sse`（因为
   server 不暴露计数器且不归 ops 改）。若这两个方法未来被重写/替换，指标会丢或
   需要同步调整包装；包装保留原语义、只在成功返回后计数，`stop()` 不撤销（进程
   生命周期内一次性安装，避免测试/多 App 竞争）。
2. metrics 是**进程内计数器**：重启归零；`sfo_events_total/event_seq` 用持久 seq
   仍单调。`sfo_approvals_decided_total` 启动回填只查最近 1000 条（有界，避免启动
   扫全部历史）。
3. `sfo_llm_tokens_total`/`cost` 仅在 LLM 子系统提供 usage 时出现样本；形状是
   getattr 兼容层，不保证覆盖未来所有实现。
4. `sfo_agent_queue_depth` 依赖 agent 子系统暴露队列；当前没有来源时为 0，不能
   据此判断“没有排队”。
5. `sfo_http_requests_total` 只统计 `/api/*`，且第一次 scrape 时可能尚无样本
   （HELP/TYPE 已输出）；静态资源、`/healthz`、SSE 建连不计。
6. 通知可靠性为**尽力而为**：队列上限 2000（eventlog 侧满队列丢最旧）、待发送
   任务上限 256、每 sink 限流；重试在“请求已到达但响应超时”时会重复投递，接收方
   应用 `seq` 去重。`to_thread` 线程不可取消，`stop()` 后可能有在途请求。
7. 通知 payload **发送前就脱敏**（不只是日志/事件）：webhook 收不到 secret 字段；
   如果某个接收方确实需要原始值，应改为显式配置白名单（当前不支持，安全默认）。
8. 备份包含 `config.json`（可能有 API key）与 `state`（规则/审批/记忆），因此归档
   0600；请不要把 `backups/` 放到共享目录或提交到 git。备份不是一致性快照（hub
   运行中可能跨瞬间），严格场景先停 hub。
9. 恢复的 manifest 校验保证归档自洽，但不等于“可信来源”：恶意但自洽的归档仍会
   被恢复；只从自己产生的备份恢复。
10. `restore` 会替换整个 state 目录（包括项目工作区）；`force=True` 时已自动备份
    当前 state，但如果磁盘空间不足，备份会先失败并放弃恢复（安全侧）。
