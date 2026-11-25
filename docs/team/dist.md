# DIST 交接：事件日志轮转、租约 fencing、存储后端、mDNS、ZMQ、部署

> Owner: **dist**。本文件记录改动、接口、需要 Lead 接线的小段代码、测试结果与残余风险。
> 工作目录 `/home/nihao/work/workspace/StackFlowOS`；测试解释器 `/tmp/sfo-venv/bin/python`。

## 1. 交付清单与文件

| 交付 | 文件 |
|---|---|
| 事件日志轮转/压缩归档/跨归档查询/可注入后端/health | `stackflowos/l2/eventlog.py` |
| 租约 fencing token、时钟偏移、verify、资源作用域校验、health | `stackflowos/l2/leases.py` |
| 存储抽象 + JSON/SQLite 后端 + 迁移 CLI | `stackflowos/storage/{__init__,backend,json_backend,sqlite_backend,migrate}.py` |
| MemoryStore 可注入后端 | `stackflowos/memory.py` |
| ProjectStore counters 可注入后端（项目数据仍是 JSON） | `stackflowos/projects/store.py` |
| mDNS 发现扩展（编解码/广播/浏览/路由/discover_hubs） | `stackflowos/transport/discovery.py` |
| 可选 pyzmq 的 ZMQ 桥 + CLI | `stackflowos/transport/zmq_bridge.py` |
| 部署 | `deploy/Dockerfile`、`deploy/stackflowos.service`、`deploy/README.md` |
| 测试 | `tests/test_dist_*.py`（6 个文件） |
| 本文件 | `docs/team/dist.md` |

未改动：`app.py`、`config.py`、`web/api.py`、`web/server.py`、`extensions.py`、`l2/registry.py`、别人的文件。没有运行任何 git 写操作。

## 2. EventLog：轮转 / 归档 / 后端

### 2.1 构造参数（向后兼容）

```python
EventLog(events_dir,
         *, logger=None,
         memory_limit=5000,
         rotate_bytes=None,          # 老参数：显式传入 → 沿用旧的未压缩轮转，保持兼容
         rotation_bytes=None,        # 新参数：压缩归档 + 保留 N 个
         rotate_keep=None,           # 新参数：保留归档数（默认 5，0 = 不留）
         backend=None)               # 可选 StorageBackend（sqlite 等）
```

优先级（决定轮转阈值与格式）：

1. `rotation_bytes=` 参数 → 压缩模式；
2. 环境变量 `SFO_EVENTLOG_ROTATE_BYTES`（和 `SFO_EVENTLOG_ROTATE_KEEP`，由构造函数读取）→ 压缩模式；
3. 显式 `rotate_bytes=`（旧调用，例如现有测试 `rotate_bytes=300`）→ **旧格式** `events-<seq>.jsonl`，不压缩、不修剪；
4. 都没有 → 默认 8MB，压缩模式。

压缩模式归档名：`events-<last_seq>-<epoch_ms>.jsonl.gz`。
- 序号放在名字里是 `_recover_last_seq`/跨归档排序的依据；epoch 给人看。所以字面上不是 `events-<time>.jsonl.gz`，而是 `events-<seq>-<time>.jsonl.gz`。
- 轮转前对活动文件 `flush+fsync`；先写 `*.gz.tmp`、fsync、`os.replace`，再删除 `events.jsonl`；最后按 `rotate_keep` 删最旧的归档。
- 整个过程在 `EventLog._lock` 内完成，因此并发 append 不会看到半个文件；压缩期间 append 会短暂阻塞（见风险）。

### 2.2 语义

- `seq` 全局单调，跨轮转/重启连续（归档名、当前文件、backend 三者取最大）。
- `since(seq)`：内存窗口内直接返回；否则从最旧归档到当前文件升序回放，按 `seq` 去重（覆盖"归档已生成、活动文件尚未删除"的短暂重叠窗口）。
- `query(...)`：先内存（新→旧），再归档（新→旧）扫描，收集满 `limit` 即停；byte/record 扫描预算语义保持不变。损坏的 `.gz` 会被跳过、计入 `health().corrupt_archives`，不会让 `query/since/启动` 失败。
- 旧版测试 `test_rotation_and_recovery`、`test_query_replays_rotated_files_newest_first` 使用显式 `rotate_bytes=`，仍走未压缩旧格式，因此不改测试也能通过。
- Web UI `/api/events?since=` 走 `EventLog.since`，跨归档逻辑在日志内部，路由无需改动。

### 2.3 可注入 backend

- 传入 `backend=`（例如 `SqliteBackend`）时，非 ephemeral 事件写 `backend.append("events", record.to_dict())`，`since/query/tail/重启恢复` 全部读 backend；不再写 JSONL、不做轮转。
- 未传 `backend` 但环境变量 `SFO_STORAGE_BACKEND=sqlite` 时，自动用 `SqliteBackend`：
  - 路径取 `SFO_STORAGE_SQLITE_PATH`，否则 `<state>/state/stackflowos.db`（`<state>` = `events_dir.parent`）。
- 空库 + 已有 JSONL 历史时启动会打 WARNING，提示先跑迁移（见 §4.3），否则旧历史在 UI 中不可见。

### 2.4 health

```python
app.eventlog.health()
# JSONL 模式:
{"last_seq": 42, "memory": 5000, "backend": "jsonl",
 "rotations": 3, "archive_bytes": 123456, "writable": true,
 "corrupt_archives": 0, "compressed": true,
 "rotate_bytes": 67108864, "rotate_keep": 5}
# backend 模式:
{"last_seq": 42, "backend": "sqlite", "rotations": 0,
 "archive_bytes": 0, "writable": true, "storage": {...SqliteBackend.health()}}
```

### 2.5 Lead 接线（可选，但推荐）

环境变量已能覆盖默认值；要让 `/api/config` 里的 `storage.eventlog_rotate_*` 生效需要改 `App.start` 的构造（约 `app.py:173`）：

```python
self.eventlog = EventLog(
    self.paths.events_dir,
    logger=get_logger("events"),
    rotation_bytes=cfg.storage.eventlog_rotate_bytes,
    rotate_keep=cfg.storage.eventlog_rotate_keep,
    # 仅当 storage.backend == "sqlite" 时传共享 backend；json 时保持 None
)
```

**注意**：`storage.backend == "json"` 时不要传 `JsonFileBackend`——那会让 EventLog 改走 KV/JSONL backend，丢失现有归档轮转语义。只有 sqlite 才传共享 backend。

## 3. 租约 fencing

### 3.1 新字段 / 新 API

```python
Lease(..., token: int = 0, last_renewed_at: int = 0)   # token 已进 to_dict/事件
manager.acquire(...) -> Lease          # token 单调递增（每个 resource 独立）
manager.renew(id, holder, ttl_s=None, *, token=None)   # 传 token 时校验，renew 不换 token
manager.release(id, *, holder=None, reason=..., token=None)
manager.verify(id, token, holder=None) -> bool         # 未知 id 抛 NotFound；过期/不匹配 False
manager.require(id, token, holder=None) -> Lease       # 不匹配抛 Denied
manager.token_for(id) -> int
manager.health() -> {"active": n, "fencing": {"enabled", "resources",
                       "last_token", "path", "persisted"}, "clock_skew_s", ...}
```

- 持久化：`<state>/state/lease_fencing.json`，结构 `{"version":1,"tokens":{"<resource>": n}}`，原子写 + fsync；重启不重置。
- 构造参数：

```python
LeaseManager(ctx, eventlog, *,
             fencing: bool | None = None,          # None → $SFO_LEASE_FENCING（默认 on）
             clock_skew_s: float | None = None,    # None → $SFO_LEASE_CLOCK_SKEW_S（默认 0）
             fencing_path: Path | None = None)     # None → <events_dir.parent>/state/lease_fencing.json
```

- 时钟偏移：`_is_active = now < expires_at + clock_skew_s*1000`；`sweep` 同样用宽容窗口，因此扫描不会"提前"过期。`Lease.active()` 保持精确语义（兼容旧测试）；`renew` 用 `max(now, last_renewed_at)` 作锚点。
- 持久化失败（只读盘等）：内存计数继续递增，`health().fencing.persisted=false`，重启可能重复发 token——需运维关注。

### 3.2 资源作用域辅助函数

```python
from stackflowos.l2.leases import validate_resource_scope
resource = validate_resource_scope(str(data["resource"]),
                                   principal.project_id if principal else None,
                                   principal.is_operator if principal else False)
```

- `project:<id>` / `project:<id>:<suffix>` 必须等于调用者 `project_id`；operator 例外；无 project 的调用者被拒（`Denied`）。
- `project:` 空 id → `BadRequest`。
- `unit:` / `cap:` / `file:` / `slot:` / 自定义字符串原样返回（向后兼容）。
- `is_operator` 也接受带 `is_operator` 属性的 Principal 对象。

### 3.3 Lead 接线

1) `App.start`（约 `app.py:195`）：

```python
self.leases = LeaseManager(
    self.ctx, self.eventlog,
    fencing=cfg.cluster.lease_fencing,
    clock_skew_s=cfg.cluster.lease_clock_skew_s,
    fencing_path=self.paths.state_dir / "state" / "lease_fencing.json")
```

2) `_lease_acquire`（约 `app.py:1146`）：在 `self.leases.acquire(...)` 前调用
`resource = validate_resource_scope(...)`，并用返回的 resource。
3) `_lease_renew` / `_lease_release`：把请求里的 `token`（可选）传给
`renew(..., token=...)` / `release(..., token=...)`；acquire 的返回 dict 已带 `token`。
4) `/api/health` 顶层（`App.health`，约 `app.py:2044`）：

```python
payload["eventlog"] = self.eventlog.health()
payload["leases"] = self.leases.health()
```

（discovery 已通过扩展 `extensions.discovery` 出现在 health 里。）

## 4. 存储后端

### 4.1 接口

`stackflowos/storage/backend.py` 定义 `StorageBackend` Protocol：

- KV：`get/set/delete/keys/items/has_prefix(namespace, key...)`
- append：`append/iter_rows(reverse, limit)/last_row/count`
- 生命周期：`transaction()`（可嵌套）、`flush`、`close`、`health`

`JsonFileBackend`：`<root>/<ns>.json`（原子写）+ `<root>/<ns>.jsonl`（每次 append flush+fsync）。
`SqliteBackend`：单文件、WAL、`BEGIN IMMEDIATE` + 嵌套 savepoint、`PRAGMA user_version` 迁移（当前 v1：`kv` / `rows` 两张表）。未来版本 > 本构建时拒绝打开。

### 4.2 已接入 / 未接入边界

| 数据 | 默认 | `SFO_STORAGE_BACKEND=sqlite` 时 |
|---|---|---|
| 事件日志 | `events/*.jsonl(.gz)` | SQLite `events` 表（EventLog 自动选） |
| 项目 memory | `state/memory/<p>.json` | SQLite `memory` namespace（MemoryStore 自动选） |
| ID counters | `state/counters.json` | SQLite `state/counters`（ProjectStore 自动选） |
| projects/tasks 域 | `state/projects.json` | **仍是 JSON**（不做迁移，见下） |
| approvals/rules/supervisor/registry 等 | 各自 JSON | 不变 |

- **ProjectStore 只把 counters 接到 backend**；`state/projects.json` 的读写逻辑未重写，避免为了抽象把 950 行稳定代码改坏。`migrate --include-projects` 会把 projects.json 作为 KV 值复制一份，但运行中的 hub 不会从那里读。
- EventLog / MemoryStore / ProjectStore 各自按环境变量建**独立 SQLite 连接**（同一文件，WAL 允许）。跨实例事务不原子；如果 Lead 想更强一致，可建一个共享 backend 实例并通过 `backend=` 传给三者（EventLog/MemoryStore/ProjectStore 都支持）。
- 默认（json）路径与旧版完全一致，现有状态目录可直接启动。

### 4.3 迁移 CLI

```bash
# 预览
python -m stackflowos.storage.migrate --state-dir /data --dry-run
# 执行（默认写 <state>/state/stackflowos.db，可用 --sqlite-path 覆盖）
python -m stackflowos.storage.migrate --state-dir /data
# 同时把 projects.json 复制成 KV 值（运行期仍不读，供未来迁移）
python -m stackflowos.storage.migrate --state-dir /data --include-projects
```

函数入口：`migrate_json_to_sqlite(paths, *, sqlite_path=None, backend=None, dry_run=False, include_projects=False)`，
`paths` 可为 `config.Paths`、含 `state_dir/events_dir` 的 dict、或 state 目录 Path。

- 事件：读取 `events/events-*.jsonl`、`*.jsonl.gz` 与 `events.jsonl`，按 `seq` 升序写入；水位线记录在 `meta/events.last_seq`，**幂等**，重复运行只补新事件。
- memory：`state/memory/<p>.json` + legacy `state/memory.json` → `memory` KV（整值覆盖，幂等）。
- counters：`state/counters.json` → `state/counters` KV。
- 切换顺序：**先迁移，再设 `SFO_STORAGE_BACKEND=sqlite`**（空库不会自动回读 JSONL，启动会 WARNING）。

## 5. mDNS 发现（`transport/discovery.py`）

### 5.1 记录格式

- service type：`_stackflowos._tcp.local.`（`cluster.service_type`，比较时忽略大小写/尾点）。
- instance：`<node_id>._stackflowos._tcp.local.`；`node_id` = `$SFO_NODE_ID`，否则 `<host_label>-<pid>`。
- 记录：
  - `PTR` service → instance（TTL 120s；TTL=0 表示 goodbye）
  - `SRV` instance → `<host>.local.`，port = sys RPC 的 TCP 端口；transport=ipc 时退化用 web 端口
  - `TXT` instance → 字符串列表：
    - `node_id=<id>`
    - `transport_rpc=<ipc://... 或 tcp://host:port>`（完整 sys RPC 地址）
    - `http_port=<web port>`
    - `version=<app.version>`
    - `projects=<project count>`
  - `A` / `AAAA` host → 地址（`transport.advertise_host` 或本机地址；仅收录可解析的 IP）
- 行为：启动立即 announce，之后每 `announce_interval_s=120s` 重播；browser 启动发 PTR 查询，每 30s 再查；收到 TTL=0 删除 peer。
- `_open_mdns_socket` 用 `SO_REUSEADDR/SO_REUSEPORT` 绑定 `0.0.0.0:5353`、加入 `224.0.0.251`；失败抛 `MdnsError`，扩展捕获后继续跑 hub。

### 5.2 对外接口

```python
GET /api/discovery/peers
# 200 {"ok": true, "data": {
#   "service_type": "_stackflowos._tcp.local", "discovery": "mdns|off",
#   "advertising": bool, "browsing": bool,
#   "peers": [{"name","host","port","addresses":[..],"txt":{..},
#              "node_id","transport_rpc","version","http_port","projects",
#              "age_s","expires_in_s"}]}}

from stackflowos.transport.discovery import discover_hubs, discover_hubs_async
peers = discover_hubs(timeout_s=2.0)          # 同步；socket 失败返回 []
peers = await discover_hubs_async(timeout_s=2.0)
```

- `cluster.discovery=off`（默认）不绑定任何 socket、不起线程；路由仍存在并返回空 peers。
- `cluster.discovery=mdns` 时扩展自动 advertise+browse；`cluster.advertise=false` 只浏览。
- `on_config` 在 discovery/service_type 变化时重建 worker，失败被隔离。
- 扩展 `health()`：`{"peers": n, "mode", "service_type", "advertising", "browsing", "errors"}`。
- 测试只覆盖 DNS 编解码与纯状态机；真实组播测试用 `SFO_TEST_MDNS=1` opt-in，默认 skip（避免 CI 无组播路由时 flaky）。

## 6. ZMQ bridge（可选依赖）

```bash
# RPC: ZMQ REP <- bridge -> hub pynng sys RPC
python -m stackflowos.transport.zmq_bridge \
  --listen tcp://0.0.0.0:5555 --connect tcp://127.0.0.1:17820
# 事件: 外部 ZMQ PUB -> pynng events
python -m stackflowos.transport.zmq_bridge \
  --sub-connect tcp://publisher:5556 --events-connect tcp://127.0.0.1:17821
# 事件: pynng SUB bind -> ZMQ PUB
python -m stackflowos.transport.zmq_bridge \
  --events-listen tcp://127.0.0.1:17901 --pub-listen tcp://127.0.0.1:5557
```

- `pyzmq` 未安装时：`available()==False`，`require_zmq()` 抛 `ZmqUnavailable`（消息含 `pip install pyzmq`）；
  构造函数仍先做参数校验（`ValueError`）；`start()` 抛明确错误；CLI 返回 2 并打印原因。模块 import 本身不依赖 pyzmq。
- wire payload 就是 `Envelope` JSON；RPC 桥保留 `id`，hub `SfoError` 原样转成 `ok=false` 回包。
- 限制：hub 的审计事件**不信任**原始 PUB 帧（X2），所以 ingest 方向适合边缘/对等事件分发，不适合伪造 hub 审计记录。`NngEventExportBridge` 需要有一个 pynng PUB 主动连到它的 SUB bind 地址（当前 hub 没有内置该 fan-out）。

## 7. 部署

- `deploy/Dockerfile`：`python:3.12-slim` 两阶段；builder 装 `build-essential/cmake` 编译 pynng，runtime 只拷 venv；UID/GID 10001；`/data` volume；`SFO_STATE_DIR=/data`；`EXPOSE 17800 tcp` + transport pool；healthcheck 打匿名 `/healthz`；entrypoint `stackflowos hub --host 0.0.0.0 --allow-remote`。**不装 pyzmq**（可选依赖）。
- `deploy/stackflowos.service`：systemd **user** unit；`StateDirectory=stackflowos` + `SFO_STATE_DIR=%S/stackflowos`；`EnvironmentFile=-%h/.config/stackflowos/hub.env` 放 `SFO_WEB_TOKEN`；`Restart=on-failure`；加固项（`NoNewPrivileges`、`PrivateTmp`、`ProtectSystem=strict`）；ExecStart 假设 `~/.local/bin/stackflowos`，venv 需改。
- `deploy/README.md`：容器/systemd 安装、TLS/远程绑定/Host 白名单、备份恢复（SQLite `.backup` / 停机 tar）、升级步骤、故障排查。
- **冷备语义（文档已写清）**：`cluster.standby` 是**同状态目录第二进程轮询 `run/hub.lock` 接管**；不是 Raft/选主，无日志复制；共享状态目录是硬要求；active 只是挂起（SIGSTOP/VM freeze/网络分区）时无法判定死亡；切换期间数秒不可用；lease fencing 只能防止旧 worker 用过期 token 动作，不提供多写一致性。具体接管实现由 Lead/cluster.standby 扩展接线。

## 8. 测试

指定命令：

```bash
/tmp/sfo-venv/bin/python -m pytest -q \
  tests/test_eventlog*.py tests/test_leases.py tests/test_dist_*.py \
  --basetemp=/tmp/pytest-dist
```

结果（本轮）：**104 passed, 2 skipped, 18 subtests passed**。skip 分别是：
- `test_dist_discovery.py::real multicast smoke`（`SFO_TEST_MDNS=1` 才跑）；
- `test_dist_zmq.py` 中"pyzmq 已安装"分支（本机未装 pyzmq）。

额外回归（相邻模块）：

```bash
/tmp/sfo-venv/bin/python -m pytest -q \
  tests/test_transport.py tests/test_project_card_leases.py \
  tests/test_memory_store.py tests/test_console_query.py \
  tests/test_projects.py tests/test_store_cow.py tests/test_task_deps.py
```
结果：全部通过（50 + 57 两个批次）。

新增测试覆盖：

| 文件 | 覆盖 |
|---|---|
| `test_dist_eventlog_rotation.py` | 小阈值压缩轮转、保留 N、跨归档 `since/query/tail`、损坏 gz 跳过、health、环境变量、旧 `rotate_bytes` 兼容、SQLite 注入/环境选择、4 写 2 读并发压力 |
| `test_dist_leases.py` | token 单调/跨重启、renew/release 校验 token、verify/require、时钟偏移窗口、health、`validate_resource_scope` 全部形态 |
| `test_dist_storage.py` | JSON/SQLite round-trip、WAL、user_version、事务提交/回滚/嵌套、迁移幂等/dry-run/CLI、迁移后 EventLog 可读、ProjectStore counters |
| `test_dist_discovery.py` | 名称编码/压缩解码/指针环、PTR/SRV/TXT/A/AAAA round-trip、状态机合并/TTL/goodbye、失败隔离、`discover_hubs` 注入 socket |
| `test_dist_zmq.py` | 参数校验、三模式构建、无 pyzmq 降级、CLI 退出码 |
| `test_dist_health.py` | eventlog/leases/discovery health 形状、App `/api/discovery/peers` 路由 |

## 9. 残余风险 / 后续

> 更完整的架构限制清单（影响/缓解/是否计划内）见
> [docs/known-limitations.md](../known-limitations.md)。本节保留 DIST 交付时的细节。

1. **轮转时延**：压缩整个活动文件期间持有 `EventLog._lock`，append 会短暂阻塞；默认阈值 64MB（config）时可能几十~几百 ms。本轮接受（轮转低频），如需零停顿可改为 rename + 后台压缩，但会引入"归档未就绪"的读窗口（已用 seq 去重兜底）。
2. **gz 查询需整包解压**：单个归档大小有上限（轮转阈值），内存可控；但极端阈值下 query 会一次性读入该归档。
3. **旧格式保留**：显式 `rotate_bytes=` 的调用（含两个旧测试）继续产生未压缩 `events-*.jsonl` 且不修剪。若 Lead 希望统一压缩格式，需要同步改旧测试；本轮选择不破坏基线。
4. **`rotate_keep=0`**：轮转后立即删除归档，等于只保留内存窗口与活动文件；`since(0)` 无法回放已删历史，属预期配置风险。
5. **SQLite 切换不是自动双写**：必须显式迁移；JSON/SQLite 两份历史可能分叉。fencing/counters 在 backend 写入失败时回退 JSON 文件（会打 WARNING），此时两种存储可能不一致。
6. **fencing 持久化是 best-effort**：磁盘只读/满时 token 仍递增但不落盘，重启可能重复；`health().fencing.persisted=false` 可监控。
7. **租约 RPC 已接线（原文“尚未接线”已过时）**：`App._lease_acquire` 在调用
   `LeaseManager.acquire` 之前执行 `validate_resource_scope`
   （`stackflowos/app.py::App._lease_acquire`），普通 unit principal 只能租自己项目的
   `project:<id>[:suffix]`，跨项目返回 `Denied`；operator 与进程内路径保留全局命名
   空间。测试见 `tests/test_dist_leases.py`（`validate_resource_scope` 各形态）与 RPC
   层回归。§9.6 的 fencing 持久化 best-effort 仍然成立。
8. **mDNS 未做真实组播 CI**：编码/状态机有单测，真实环境（容器无组播、5353 被 avahi 占用、防火墙）只做了失败隔离；`discover_hubs` 失败静默返回 `[]`，调用方必须有显式地址回退。TXT 无签名/无认证，仅用于链路本地发现，不要放敏感信息。
9. **ZMQ 桥未在装有 pyzmq 的环境实测**（本机无 pyzmq）：协议转换逻辑复用了 `RpcClient/EventPublisher/EventSubscriber` 的既有语义，但真实 socket 组合、关闭顺序需在装有 pyzmq 的机器上做一次 smoke test。
10. **ProjectStore 续用 JSON**：projects/tasks 域未事务化；高并发多 hub 场景仍会损坏状态。冷备必须保证同一时刻只有一个 writer。
11. **health 顶层聚合已接线（原文“未接线”已过时）**：`App.health()` 逐个调用
    `eventlog.health()` / `leases.health()`，合并 `extensions.health()` 后返回；
    `/api/health` 路由在 `stackflowos/web/api.py` 注册。任一服务没有 `health` 方法
    时该键跳过，抛错时降级为 `{"ok": false}`，不影响整体探针；discovery 作为
    extension 自动出现在 `extensions.discovery`。测试见
    `tests/test_dist_health.py::AppHealthTests`。
12. **`storage.sqlite_path` 为空**时默认 `<state>/state/stackflowos.db`；若状态目录在只读介质，EventLog/MemoryStore 会自动建库失败 → SQLite 初始化异常会向上抛，部署时要保证目录可写（默认 `/data`）。
