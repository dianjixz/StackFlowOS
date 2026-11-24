# SEC 工作流交接（2026-11-24）

> 负责人：sec。范围：TLS 监听、单次 launch token + operator control socket、
> 服务端会话表/多用户读取角色、OS 沙箱（Landlock/bwrap）、operator 审计脱敏、
> `net.fetch` DNS 固定 IP。只改了自己拥有的文件；未运行任何 git 写操作。

## 1. 改动文件

| 文件 | 状态 | 内容 |
|---|---|---|
| `stackflowos/web/auth.py` | 改 | 单次 URL token、多用户账号、cookie 绑定 user/role/session、viewer 只读（admission 层提前 403）、`Secure` cookie |
| `stackflowos/web/server.py` | 改 | `tls_*` 参数 + `ssl.SSLContext` 监听、HTTPS 自动 Secure、非 loopback 无 TLS 的诚实告警、viewer 写操作 403、会话/control 扩展 attach、`WebReply` 放行 `Set-Cookie` |
| `stackflowos/session/operator.py` | 改 | `_audit` 的 `result`/`error` 统一 redact + 按 `security.audit_max_field_chars` 截断（args 原有脱敏保留） |
| `stackflowos/util/net.py` | 改（追加） | `PinnedTarget`/DNS 固定 IP 连接类、逐跳重定向校验、`fetch_url(..., pin_dns=)` 逃生门 |
| `stackflowos/security/sessions.py` | 新增 | `SessionManager`（线程安全、上限、revoke/rotate/cleanup）+ 扩展 `build(app)` 与 `/api/sessions*` 路由 |
| `stackflowos/security/control.py` | 新增 | Unix control socket（`run/control.sock`、0600、SO_PEERCRED 同 uid）、扩展 `build(app)`、CLI 同步客户端 |
| `stackflowos/security/sandbox.py` | 新增 | Landlock ABI 探测 + 真实 `restrict_self`、bwrap 功能性探测、`detect_backend`/`build_command`/`wrap_command`/`preexec_landlock` |
| `tests/test_sec_support.py` | 新增 | 测试公用 FakeApp / raw HTTP/TLS 客户端 / 自签证书生成 |
| `tests/test_sec_tls.py` | 新增 | HTTPS、`Secure` cookie、`/healthz`、TLS 配置失败、远程无 TLS 告警 |
| `tests/test_sec_launch_token.py` | 新增 | 单次兑换、Bearer 逃生门、mint、多用户 operator/viewer |
| `tests/test_sec_sessions.py` | 新增 | 会话表单测（含并发）、列表/轮换/撤销/登出、session 上限、viewer |
| `tests/test_sec_control.py` | 新增 | 0600、同 uid、stale socket、非 socket 拒接、新 URL 单次可用 |
| `tests/test_sec_sandbox.py` | 新增 | 探测、bwrap 命令构造、**真实 Landlock 子进程写/网络限制** |
| `tests/test_sec_audit.py` | 新增 | result/error 脱敏与截断、事件不可逆 |
| `tests/test_sec_net.py` | 新增 | 单次解析、Host/SNI、重定向逐跳、私网拒绝、旧路径 |

未改：`app.py`、`config.py`、`extensions.py`、`cli.py`、`web/api.py`、`agents/**`、`webassets/**`。

## 2. TLS 监听

`WebServer.__init__` 新增（全部有默认值，旧调用不破）：

```python
WebServer(app, *, tls_cert=None, tls_key=None, tls_min_version="TLSv1.2",
          users=None, session_max=None, launch_token_single_use=None,
          session_manager=None)
```

- 只有 `tls_cert`/`tls_key` 同时给出才启用；半配置/未知版本在构造时 `ValueError`（fail loud）。
- `ssl.SSLContext(PROTOCOL_TLS_SERVER)`，`minimum_version` 支持 `TLSv1.2`（默认）/`TLSv1.3`，`load_cert_chain` 后作为 `asyncio.start_server(..., ssl=ctx)` 传入。
- HTTPS 时：`server.tls_enabled=True`、`server.url` 前缀 `https://`、cookie 自动加 `Secure`（`WebAuth(secure=True)`），`/healthz` 仍匿名可用。
- 非 loopback 且无 TLS：`server.insecure_remote=True`、`server.security_notes` 有一条 plaintext 警告，`start()` 用 `log.warning` 打出；不假装安全。
- 配置兜底：`tls_cert/tls_key/users/session_max/launch_token_single_use` 为 `None` 时从 `app.config.web` 读取；显式参数优先。

### Lead 在 `cli.py` 的接线点

`_web_server(app, cfg)` 构造 `WebServer(...)` 时补：

```python
tls_cert=cfg.web.tls_cert or None,
tls_key=cfg.web.tls_key or None,
tls_min_version=cfg.web.tls_min_version,
users=cfg.web.users,
session_max=cfg.web.session_max,
launch_token_single_use=cfg.web.launch_token_single_use,
```

（即使不补，`WebServer` 也会从 `app.config.web` 兜底；显式传入便于后续 PATCH 配置时行为清晰。）
启动告警可改成：`if server.insecure_remote: print(server.security_notes[0], ...)`。

## 3. 单次 launch token + control socket

### token 语义（重要取舍）

- `WebAuth` 维护：base token（`launch_token`，可由 `SFO_WEB_TOKEN` 显式覆盖）+ `mint_launch_token()` 生成的 token 集合。
- `?token=` **cookie 交换路径**默认单次：`web.launch_token_single_use=True` 时兑换一次后该 token 不能再兑换（base 标记 consumed；minted 直接从集合删除）。
- **`Authorization: Bearer <内存 secret>` 永远接受**（含 base token 已兑换后）。这是 Lead 指定的 CLI/测试显式 API 路径，现有 `tests/test_web*.py`/`test_integration.py` 每请求同 token 的用法不受影响。危险面：SFO_WEB_TOKEN 是长期 secret，仅作为逃生门，文档建议 TTY/control socket 优先。
- `?token=` 也接受 `config.web.users` 的长期账号 token（不消耗，可重复登录）；角色随之绑定到 cookie。
- 新 API：
  ```python
  auth.mint_launch_token() -> str
  auth.login_url_for(token, *, scheme=None, host=None, port=None) -> str
  auth.check_launch_token(token) -> bool          # 仍可兑换
  auth.check_bearer_token(token) -> bool          # Bearer 逃生门
  auth.exchange_launch_token(token) -> bool       # 兑换并（单次时）失效
  server.mint_login_url() -> str                  # control socket 使用
  ```

### control socket

`stackflowos/security/control.py`：

- 路径 `<state>/run/control.sock`，父目录 0700，socket `0600`；启动时只清理“同 uid + 是 socket + 连接被拒”的 stale 文件；普通文件/别人的 socket 一律拒绝覆盖。
- 每个连接 `SO_PEERCRED` 校验：`uid != hub uid` → `permission denied`；内核不支持 peer cred → 拒绝（fail closed）。请求先读有界一行（防关闭时 RST 掩盖错误），再做鉴权。
- 动作（JSON line）：`{"action":"url"}` → `{"ok":true,"url":"https://.../?token=<新 token>"}`；`{"action":"status"}` → `{running,tls,url,sessions}`；`{"action":"ping"}`。无 shutdown/token dump。URL 不写日志。
- 扩展 `build(app)` 设置 `app.security_control`；`start()` 监听、`stop()` 关闭并 unlink。`WebServer.start()` 调 `app.security_control.attach_web(server)`；未 attach 时 `url` 返回 `web server is not ready`（不会给坏链接）。

### Lead 在 `cli.py` 的接线点

```python
from stackflowos.security.control import (
    control_socket_path, query_control_socket,
)

# sfo url
reply = query_control_socket(control_socket_path(cfg.paths.run_dir), "url")
if reply.get("ok"):
    print(reply["url"])
else:
    print(reply.get("error", "control socket unavailable"), file=sys.stderr)

# sfo status（摘要）
reply = query_control_socket(control_socket_path(cfg.paths.run_dir), "status")
```

客户端是同步函数、不会因 socket 不存在而抛异常。`request_login_url(run_dir)` 是只要 URL 的便捷封装。
非 Linux/`SO_PEERCRED` 不可用时 control 功能应当由 CLI 回退到 `SFO_WEB_TOKEN` 文档路径（提示用户显式设置）。

### 扩展注册（Lead 在 `extensions.py`）

`EXTENSION_MODULES` 追加（顺序建议 sessions 在 control 前，均在 `WebServer` 创建前）：

```python
"stackflowos.security.sessions",   # sets app.security_sessions, /api/sessions*
"stackflowos.security.control",    # sets app.security_control, run/control.sock
```

未注册时功能安全降级：`WebServer` 自建 `SessionManager`（/api/sessions 路由不存在），control socket 不存在（`sfo url` 报不可用）。

## 4. 会话与多用户

`stackflowos/security/sessions.py`：

- `SessionManager(max_sessions=32, ttl_s=7d, now=...)`，`threading.RLock`；`create/get(touch)/revoke/revoke_user/rotate/cleanup/list_sessions/count`。`create` 达上限抛 `SessionLimitError(UNAVAILABLE)`（不驱逐活会话）。
- `Session` 字段：`id,user,role,created,last_seen,expires,authority,source`；`to_public_dict()` 不含任何 token/signature。
- cookie 载荷新增 `user/role/sid`；服务端有 session manager 时**必须有 sid 且会话有效、user/role 与载荷一致**（旧无 sid cookie 一律 401）。
- 多用户：`web.users[] = {name, token_sha256(hex), role: operator|viewer}`；token 比较用 `hashlib.sha256` + `hmac.compare_digest`。base launch token → `launch-token/operator`。
- viewer：admission 层在 Content-Type/CSRF 检查前直接 403（`viewer role is read-only`），server 层再做一次相同判断（路由忘不了）。唯一例外 `POST /api/logout`（允许 viewer 结束会话）。
- 路由（由 `SessionsExtension.routes()` 提供）：

| Method | Path | 约束 |
|---|---|---|
| GET | `/api/sessions` | operator 全部；viewer 只看自己会话；无 auth 401 |
| POST | `/api/sessions/rotate` | operator；无浏览器 session（纯 Bearer）400；返回 `Set-Cookie` |
| DELETE | `/api/sessions/{id}` | operator；未知 404 |
| POST | `/api/logout` | 任意已认证角色；revoke 当前 sid + `Set-Cookie: Max-Age=0` |

- 轮换/登出的 `Set-Cookie` 通过 `WebReply(headers={"Set-Cookie": ...})` 返回；`server._send_raw` 只允许 `set-cookie`/`content-disposition`/`cache-control`/`x-*`，并剥离 CR/LF。
- 扩展在 `build(app)` 时把 manager 放 `app.security_sessions`；`WebServer` 构造时优先读取它并回填 `app.security_web=server`（`start()` 时 attach），因此**扩展加载早于 WebServer** 的顺序成立。

## 5. OS 沙箱（`security/sandbox.py`）

稳定 API（agent 工作流按此接线）：

```python
detect_landlock() -> LandlockStatus(available, abi, reason)
detect_bwrap(executable=None) -> BackendInfo       # 真跑 bwrap --ro-bind / / -- true
detect_backends() -> {"landlock": BackendInfo, "bwrap": BackendInfo}
detect_backend(prefer="auto"|"off"|"bwrap"|"landlock", *, allow_network=False) -> str
restrict_current_process(writable_paths, allow_network=False, *, landlock=None) -> SandboxReport
preexec_landlock(writable_paths, allow_network=False) -> Callable[[], None]   # subprocess preexec_fn
build_command(argv, cwd=None, writable_paths=(), allow_network=False, *,
              backend=None, readonly_paths=(), tmp_mb=256) -> (argv, backend, notes)
wrap_command(...) -> SandboxPlan(argv, backend, notes, preexec_fn, cwd, writable_paths)
```

- `auto`：**优先 Landlock**（无需特权）→ bwrap（需功能性探测通过）→ `unsandboxed`；任何失败都在 notes 写具体原因，绝不静默假装隔离。
- Landlock：syscall 444/445/446（x86_64/aarch64/riscv64），`prctl(PR_SET_NO_NEW_PRIVS)` + `restrict_self`；handled 仅写类权限（`WRITE_FILE/REMOVE_*/MAKE_*/REFER/TRUNCATE`），读/执行全域允许；写仅白名单 `writable_paths`（不存在则退到最近存在的父目录）。ABI≥4 且 `allow_network=False` 时 deny TCP bind/connect（UDP 不在 Landlock 范围）。
- bwrap：`--die-with-parent --new-session --unshare-pid/-ipc/-uts/-cgroup-try --ro-bind / / --dev /dev --proc /proc --size ... --tmpfs /tmp`，writable 额外 `--bind`，`allow_network=False` 时 `--unshare-net`（若探测不到则 notes 明确写 “network is NOT isolated”）。
- 本机实测：Landlock ABI=8 可用；bwrap 二进制存在但非特权 userns 不可用（探测 detail 为 `setting up uid map: Permission denied`），因此 agent 在本机应得到 `landlock`。
- agent 接线：
  - `shell.exec`：`plan = wrap_command(argv, cwd=cwd, writable_paths=[workspace, tmp_dir, logs_dir], allow_network=cfg.security.sandbox_allow_network, backend=cfg.security.sandbox)`；`Popen(plan.argv, preexec_fn=plan.preexec_fn, ...)`（`preexec_fn` 仅在 backend=landlock 时非 None）。
  - 独立 worker 进程：启动时调用一次 `restrict_current_process([...])`，之后再执行不受信命令。
  - 注意 `preexec_fn` 在多线程进程中有 fork 安全边界；当前 hub 进程不应调用 `restrict_current_process`。

## 6. operator 审计脱敏

`session/operator.py::_audit`：

- `result`：先结构化脱敏（secret key → `***`），`json.dumps` 后再次 `redact_text`（Bearer/JWT/URL userinfo/ghp_/sk- 等），最后截断到 `security.audit_max_field_chars`（默认 4000，最小 64）。
- `error`：`redact_text` 后同样截断。
- `security.audit_redact` 控制内联规则；事件仍 append-only（只改 append 之前的 payload，不重写历史）。

## 7. `net.fetch` DNS 固定 IP

`stackflowos/util/net.py` 新增（原有 `parse_fetch_url`/`validate_fetch_url`/`assert_public_target` 保持兼容）：

```python
resolve_target(url, *, allow_private_network) -> PinnedTarget   # 一次 getaddrinfo + 全部地址校验
build_pinned_opener(*, allow_private_network, redirect_handler=None, context=None)
GuardedRedirectHandler(validate)                                  # 每跳先 validate 再跟随
fetch_url(url, *, allow_private_network=False, pin_dns=True,
          timeout=15.0, max_bytes=2MiB, headers=None, context=None) -> FetchResult
PinnedHTTPConnection / PinnedHTTPSConnection / PinnedHTTPHandler / PinnedHTTPSHandler
```

- `pin_dns=True`：连接时**在该连接内解析一次**、校验全部地址后直接连返回的 IP；`http.client` 不再二次 DNS。HTTPS 用 `wrap_socket(..., server_hostname=URL host)`，SNI/证书校验/Host 头保持原 host:port 正确。
- 重定向：`GuardedRedirectHandler` 每跳 `validate_fetch_url`（含私网），最多 5 跳；pinned 连接自身每跳还会重新校验，两套独立。
- 私网策略保持 fail-closed：任何解析结果命中 loopback/RFC1918/link-local/metadata/保留地址即拒绝（除非显式 `allow_private_network=True`）。
- `pin_dns=False` = 旧 urllib 路径（仍逐跳校验），对应 `security.net_pin_dns=false` 的测试/运维逃生门。
- agent 接线（`agents/tools.py`，由 agent owner 做）：
  ```python
  result = net.fetch_url(url, allow_private_network=self._allow_private_network(),
                         pin_dns=cfg.security.net_pin_dns, max_bytes=max_bytes)
  ```
  或保留现有 `_http_get`，把 `build_opener` 换成 `build_pinned_opener(...)` + `GuardedRedirectHandler(self._validate_url)`。

## 8. 测试

本次实际运行：

```bash
/tmp/sfo-venv/bin/python -m pytest -q tests/test_web*.py tests/test_sec_*.py \
  --basetemp=/tmp/pytest-sec
# 160 passed, 48 subtests passed
```

（中途 `test_web_assets.py::test_api_endpoints_used_by_ui_exist_in_route_table`
曾因 front 已改 webassets、units 的 `/api/units/{id}` 扩展路由尚未落地而失败；
units 工作流补齐后该用例转绿，与 SEC 改动无关，`web/api.py` 未被修改。）

补充回归（均通过）：

```bash
/tmp/sfo-venv/bin/python -m pytest -q tests/test_integration.py \
  tests/test_attach_project.py tests/test_project_admin.py \
  tests/test_approval_preview.py --basetemp=/tmp/pytest-sec   # 39 passed
/tmp/sfo-venv/bin/python -m pytest -q tests/test_net_fetch.py \
  tests/test_operator.py tests/test_redact.py tests/test_config.py \
  --basetemp=/tmp/pytest-sec                                  # 63 passed
```

新增覆盖命令：`tests/test_sec_tls.py`（8）、`test_sec_launch_token.py`（13）、
`test_sec_sessions.py`（18）、`test_sec_control.py`（11）、
`test_sec_sandbox.py`（17，含真实 Landlock 子进程写/网络测试）、
`test_sec_audit.py`（5）、`test_sec_net.py`（13）。

## 9. 残余风险 / 取舍

1. **同 uid 进程**：control socket 只校验 uid；同一用户的 worker 子进程理论上也能 `sfo url`（或直读进程内存），这是 §5.1.6 已记录的残余。彻底方案是独立 worker uid（Phase 5）。
2. **Bearer 长期有效**：为了现有 CLI/测试契约，base token 兑换后仍可 Bearer；`SFO_WEB_TOKEN` 应只作为 systemd/容器的显式逃生门，日常用 `sfo url`。Cookie 本身受 TTL/撤销约束。
3. **会话表在内存**：hub 重启后全部 cookie 失效（用户重新打开 URL）；`session_max` 满时新登录 503（不驱逐活会话，fail closed）。
4. **Landlock**：只限制文件系统写 + (ABI≥4) TCP；UDP、`/proc`、同 uid 信号/调试不在范围内。`preexec_fn` 在 threaded 进程中有 fork 安全限制，建议 worker 侧一次性 `restrict_current_process`。
5. **bwrap**：需要非特权 user namespace；本机不可用，`--unshare-net` 失败时 notes 会明确说明没有网络隔离。
6. **`net_pin_dns=false`** 仍是旧路径（名字解析与连接之间有 TOCTOU）；默认 `true`。
7. **TLS**：只做服务端证书/最低版本；不提供客户端证书、ACME、HSTS；非 loopback 无 TLS 只告警不阻断（由 `web.allow_remote` 控制是否允许绑定）。
8. **`Set-Cookie` 放行**：`WebReply` 现在可带 `set-cookie`（已剥离 CR/LF）；扩展 handler 仍是受信代码。
9. **未注册扩展时**：sessions 路由不存在、control socket 不存在；WebServer 仍安全（自建 manager、Bearer/单次 token 工作）。

## 10. Lead 接线清单（勾选）

- [ ] `extensions.py::EXTENSION_MODULES` 加 `stackflowos.security.sessions`、`stackflowos.security.control`（在 WebServer 创建前）。
- [ ] `cli.py::_web_server` 传 `tls_cert/tls_key/tls_min_version/users/session_max/launch_token_single_use`。
- [ ] `cli.py` 新增/接线 `sfo url`（`query_control_socket(..., "url")`）与可选 `sfo status`（`"status"`）。
- [ ] 启动时若 `server.insecure_remote`，把 `server.security_notes` 打到 stderr。
- [ ] 文档 `docs/operations.md` 更新：systemd 场景用 `sfo url`，`SFO_WEB_TOKEN` 仅逃生门；远程模式必须 TLS/SSH 隧道。
- [ ] agent owner：`shell.exec` 接 `wrap_command`/`preexec_fn`；`net.fetch` 接 `fetch_url(pin_dns=cfg.security.net_pin_dns)`（供参考的签名见 §5/§7）。
