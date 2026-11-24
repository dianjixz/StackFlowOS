# 自治 HTTP API 交接（2026-11-24）

> 负责人：api。范围：`stackflowos/autonomy/api.py`（新建）、
> `tests/test_autonomy_api.py`（新建）、本文件。按
> `docs/team/autonomy-plan.md` §4 实现扩展路由；未改 `service.py`、
> `models.py`、`config.py`、`web/api.py`、`app.py` 或他人文件；未运行任何 git
> 写操作。

## 1. 改动文件

| 文件 | 状态 | 内容 |
|---|---|---|
| `stackflowos/autonomy/api.py` | 新增 | `build_routes(service)`：4 条扩展路由 + 认证/角色守卫 |
| `tests/test_autonomy_api.py` | 新增 | 16 个用例（fake service 直调 handler + 真实 App/WebAPI 端到端） |
| `docs/team/api.md` | 新增 | 本交接 |

未改：`stackflowos/autonomy/service.py`、`models.py`、`config.py`、
`stackflowos/web/api.py`、`stackflowos/app.py`、`stackflowos/extensions.py`。

## 2. 路由

`AutonomyService.routes()` 已调用 `from .api import build_routes` 并返回
`build_routes(self)`；`WebAPI._build_routes` 经 `extension_routes(...)` 注册，
因此 `app.start()` 后路由自然存在（真实 App 测试已验证，不是 404）。

| Method | 正则 | 行为 | 权限 |
|---|---|---|---|
| GET | `/api/autonomy` | `service.status()` | 已认证（viewer 可读） |
| GET | `/api/autonomy/status` | 同上（别名） | 已认证 |
| POST | `/api/autonomy/patrol` | 解析 `dry_run`；`await service.run_patrol(trigger="operator", dry_run=...)`；返回 report dict | operator |
| PATCH | `/api/autonomy/policy` | body 原样传给 `await service.configure(body)`，然后返回 `service.status()` | operator |

- Handler 签名严格为 `async (match, query, body) -> (status, payload)`；
  成功均为 `200, {"ok": True, "data": ...}`。
- 错误只 `raise stackflowos.errors`：`Denied`(403) / `BadRequest`(400)；
  service 抛出的 `Conflict`（巡检已在跑）由 `WebAPI` 映射为 409。
  handler 内的意外异常由 `WebAPI` 统一收敛为 500 + request_id，不泄露文本。

### `POST /api/autonomy/patrol` 的 body

- 空 body / `{}` 合法，`dry_run` 默认 `False`。
- `dry_run` 用 `stackflowos.web.api._as_bool` 严格解析（与其它端点一致）：
  JSON `true/false`、`0/1`、字符串
  `true/false/yes/no/on/off/1/0/""` 合法；`"maybe"`、`2`、list、object 等
  抛 `BadRequest`（400），**不会**被 `bool("false")` 静默转成真值。
- 返回 `service.run_patrol()` 的 report dict（`seq/started_at/finished_at/
  trigger/findings/summary/headline/dispatched/approvals/...`），不包
  `report` 字段，直接作为 `data`。

### `PATCH /api/autonomy/policy` 的 body

- body 是**扁平的 autonomy 局部配置对象**，如
  `{"dispatch_mode": "tasks", "interval_s": 300}`；不支持
  `{"patch": {...}}` 包装（包装会被 service 当作未知字段 → 400）。
- 空对象/非法 object 由 `service.configure` 抛 `BadRequest`；未知字段由
  service 抛 `BadRequest`（带 `detail.fields`）；枚举/范围非法由
  `Config.update` 抛 `CONFIG_INVALID`(400)。以上都不会变成 500，也不落盘。
- 成功返回 **configure 之后重新读取的** `service.status()`，因此 `data.policy`
  已是新策略；同时 `app.config.autonomy` 热生效（service 的 `on_config`）。

## 3. 认证语义

与 `stackflowos/security/sessions.py` 的 `_require_operator` 对齐：

- `auth is None` 或 `auth.authenticated is not True`
  → `Denied("authentication required")`（403）。
- 写操作且 `auth.role != "operator"`
  → `Denied("operator role required")`（403），viewer 只读。
- 读操作只要求已认证；viewer 可读。
- 身份来自 `stackflowos.web.api.current_auth()`（`WebAPI.handle` 每请求设置
  的 contextvar），不信任 header/query。

## 4. 测试

命令（只跑本文件，temp 目录，不碰在跑的 hub/端口/仓库 state）：

```
cd /home/nihao/work/workspace/StackFlowOS && \
  /tmp/sfo-venv/bin/python -m pytest -q tests/test_autonomy_api.py \
  --basetemp=/tmp/pytest-api
```

结果：`16 passed, 33 subtests passed in 0.35s`（2026-11-24）。

覆盖：

- fake service（记录调用、固定返回）**直接调用 handler**：两条 GET 的
  envelope、patrol 默认 `trigger="operator"`/`dry_run=False`、`dry_run`
  各种布尔拼写、非法 `dry_run` 400、PATCH 传参与返回新 status、非 object
  body 400、未认证/匿名 403、viewer 读通/写 403。
- fake service 经 `WebAPI` + 扩展注册路径：Denied→403、BadRequest→400
  （错误 code 正确、无 500 泄露）。
- 真实 `App(Config.load(root=temp, state_dir=temp/"state"))` +
  `WebAPI`（每用例独立 temp，`await app.start()/stop()`，auth 用
  `AdmissionDecision.allow(authenticated=True, role=...)`）：
  - `GET /api/autonomy`、`/api/autonomy/status`：200、status 形状完整、
    确认 `app.autonomy` 扩展已加载；
  - `PATCH /policy`：200、`data.policy` 新值、`app.config.autonomy` 热生效、
    `state/config.json` 落盘且 `Config.load` 重读仍在；
  - `POST /patrol {"dry_run": true}`：200、report 形状/`trigger=="operator"`、
    spy 确认 kwargs 为 `dry_run=True`、`dispatched/approvals` 为空、
    `last_run` 更新；
  - 空对象/未知字段/非法 `dispatch_mode` 均 400 且配置不变；
  - 非法 `dry_run` 400 且巡检未执行（`last_run==0`）；
  - 匿名 403、viewer 读 200 / 写 403（配置与 `last_run` 均未变）。

注：`patrol.collect_snapshot/run_checks` 当前仍是 patrol 负责的 stub，
真实 App 的 patrol 用例只断言 report 形状与 dry_run 语义、不要求
`report.error == ""`；patrol 落地后该用例无需改动。

## 5. 残余风险 / 需 lead 注意

1. **PATCH body 契约**：前端要发扁平对象（`{"dispatch_mode": ...}`），
   不是 `{"patch": {...}}`。若 front 计划用统一 `patch` 包装，需要先改
   api.py 或 front 侧，建议在联调前对齐。
2. **操作者身份未进审计**：`service.run_patrol(trigger=..., dry_run=...)` 与
   `configure(patch)` 都没有 actor 参数，事件里只能看到
   `trigger="operator"`，没有具体 user/session。要更细的审计需 Lead 扩展
   service 签名（本次按契约未改 service）。
3. **`_as_bool` 是 `web.api` 的私有函数**：按任务约定复用，行为和其它端点
   一致；若 Lead 以后把它公开化，api.py 改为公有 import 即可。
4. **无 PATCH 结果版本号 / 并发保护**：两个 operator 并发 PATCH 以
   `Config.apply` 的读-改-写为准（最后写入者赢），没有乐观锁；当前需求未要求。
5. **未暴露 `recent_events`**：service 有该 helper，但 §4 未列路由，本实现未加；
   如需事件流可走现有 `/api/events?source=autonomy`。
6. **未跑全量测试**：按分工只跑 `tests/test_autonomy_api.py`；全量回归由 Lead
   执行（其它 teammate 的 patrol/dispatcher/guardian/front 仍在并行开发）。
