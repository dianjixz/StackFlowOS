"""Command line entry points.

    stackflowos hub      boot the hub (sys unit + web console)
    stackflowos demo     boot + seed a self-contained project and watch it work
    stackflowos worker   internal: one worker unit (spawned by the supervisor)
    stackflowos doctor   environment / configuration checks
    stackflowos status   print a running hub's state as text

Everything is local-first: the hub binds 127.0.0.1 by default and writes only
under ``<repo>/.stackflowos``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, is_loopback_host
from .util.logging import get_logger, setup_logging


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stackflowos",
                                     description="StackFlowOS — an agent-native operating system")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    hub = sub.add_parser("hub", help="boot the hub and the web console")
    _add_common(hub)
    hub.add_argument("--no-web", action="store_true", help="do not start the HTTP console")

    demo = sub.add_parser("demo", help="boot, seed a demo project and watch agents work")
    _add_common(demo)
    demo.add_argument("--goal", default="构建一个把 Markdown 转成 HTML 的命令行工具，包含实现、测试和文档")
    demo.add_argument("--name", default="演示：Markdown → HTML 工具")
    demo.add_argument("--until-done", action="store_true",
                      help="exit once the demo project reaches review/done (for CI)")
    demo.add_argument("--timeout", type=int, default=240)
    demo.add_argument("--no-web", action="store_true")

    doctor = sub.add_parser("doctor", help="check environment and configuration")
    _add_common(doctor)

    status = sub.add_parser("status", help="print a running hub's state")
    status.add_argument("--url", default=None, help="hub base URL (default: config web host/port)")
    status.add_argument("--token", default=None,
                        help="web launch token for Authorization: Bearer "
                             "(default: $SFO_WEB_TOKEN)")

    attach = sub.add_parser("attach", help="attach one worker unit to a running hub")
    attach.add_argument("--sys", required=True, help="hub sys rpc address (ipc:// or tcp://)")
    attach.add_argument("--role", required=True,
                        choices=("lead", "architect", "coder", "tester", "reviewer",
                                 "researcher", "aggregator"))
    attach.add_argument("--project")
    attach.add_argument("--task")
    attach.add_argument("--label", default="")
    attach.add_argument("--root", default=None)
    attach.add_argument("--state-dir", default=None)
    attach.add_argument("--token", default=None,
                        help="operator RPC token for protocol v2 auth "
                             "(default: $SFO_RPC_TOKEN); the hub issues the "
                             "unit token returned to the worker via env")

    worker = sub.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("--sys", required=True)
    worker.add_argument("--work-id", required=True)
    worker.add_argument("--role", required=True)
    worker.add_argument("--project")
    worker.add_argument("--task")
    worker.add_argument("--state-dir", required=True)
    worker.add_argument("--root", required=True)
    worker.add_argument("--label", default="")
    return parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=None, help="repository root (default: auto)")
    parser.add_argument("--state-dir", default=None, help="runtime state directory")
    parser.add_argument("--transport", choices=("ipc", "tcp"), default=None)
    parser.add_argument("--port", type=int, default=None, help="web console port")
    parser.add_argument("--host", default=None,
                        help="web console bind host (loopback by default)")
    parser.add_argument("--allow-remote", dest="allow_remote", action="store_true",
                        default=None,
                        help="allow a non-loopback web console bind "
                             "(sets web.allow_remote; transport is configured "
                             "separately via transport.allow_remote)")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command is None:
        args.command = "hub"
        args.root = args.state_dir = args.transport = args.port = args.host = None
        if not hasattr(args, "no_web"):
            args.no_web = False
        args.open = False

    if args.command == "attach":
        return _run_cli(lambda: asyncio.run(_attach(args)))
    if args.command == "worker":
        from .units.worker import worker_parser, run_worker

        # Reuse the worker parser so both entry points share one contract.
        worker_args = worker_parser().parse_args(
            [a for a in (argv if argv is not None else sys.argv[1:])][1:]
        )
        return _worker_main(worker_args)
    if args.command == "doctor":
        return _run_cli(lambda: _doctor(args))
    if args.command == "status":
        return _run_cli(lambda: _status(args))
    if args.command == "demo":
        return _run_cli(lambda: asyncio.run(_demo(args)))
    return _run_cli(lambda: _hub(args))


async def _attach(args) -> int:
    """Allocate a unit on a running hub and run one worker process here.

    This is the manual/remote path: the supervisor uses it internally for
    local workers, but an operator can also point it at tcp://hub:port from
    another device (the unit binds the addresses the hub allocated).

    Protocol v2: the hub only accepts authenticated RPC, so ``--token`` (an
    operator token) is required. ``unit.allocate`` returns a unit token which
    is handed to the worker through ``SFO_UNIT_TOKEN`` (never argv).
    """
    import argparse as _argparse

    from .transport import RpcClient
    from .units.worker import run_worker

    token = args.token or os.environ.get("SFO_RPC_TOKEN") or ""
    if not token:
        print("attach: an operator token is required (protocol v2): "
              "pass --token or set SFO_RPC_TOKEN", file=sys.stderr)
        return 2
    cfg = Config.load(root=Path(args.root) if args.root else None,
                      state_dir=Path(args.state_dir) if args.state_dir else None)
    setup_logging("INFO", cfg.paths.log_file)
    auth = {"kind": "operator", "id": "human:operator", "token": token}
    client = RpcClient(args.sys, src="attach", dst="sys", timeout=15.0,
                       auth=auth)
    await client.start()
    try:
        allocation = await client.call("unit.allocate", {
            "kind": args.role,
            "project_id": args.project,
            "label": args.label or args.role,
        })
    finally:
        await client.stop()
    work_id = allocation["work_id"]
    unit_token = str(allocation.get("unit_token") or "")
    if not unit_token:
        print("attach: hub did not return a unit token; cannot authenticate "
              "the worker", file=sys.stderr)
        return 2
    os.environ["SFO_UNIT_TOKEN"] = unit_token
    print(f"attached as {work_id} to {args.sys}")
    ns = _argparse.Namespace(
        sys=args.sys, work_id=work_id, role=args.role,
        project=args.project, task=args.task,
        state_dir=str(cfg.paths.state_dir), root=str(cfg.paths.root),
        label=args.label or args.role,
    )
    return await run_worker(ns)


def _build_config(args, *, for_web: bool = True) -> Config:
    # ``status`` only exposes ``--url/--token`` (it needs the running hub's
    # address, not a repo path); every other subcommand gets ``--root`` etc.
    # from ``_add_common``.  Read them defensively so both shapes work.
    root = getattr(args, "root", None)
    state_dir = getattr(args, "state_dir", None)
    cfg = Config.load(root=Path(root) if root else None,
                      state_dir=Path(state_dir) if state_dir else None)
    patch: dict = {}
    if getattr(args, "transport", None):
        patch.setdefault("transport", {})["mode"] = args.transport
    if getattr(args, "port", None):
        patch.setdefault("web", {})["port"] = args.port
    if getattr(args, "host", None):
        patch.setdefault("web", {})["host"] = args.host
    if getattr(args, "allow_remote", None):
        # CLI flag is web-only: ``--host`` also targets the console.  Widening
        # the RPC listener requires ``transport.allow_remote`` in config.
        patch.setdefault("web", {})["allow_remote"] = True
    if patch:
        # CLI flags are per-process; only the Settings UI persists changes.
        cfg = cfg.with_overrides(patch)
    return cfg


def _run_cli(factory) -> int:
    try:
        return factory()
    except ConfigError as exc:
        print(f"config error: {exc.message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _web_server(app, cfg):
    """Build a :class:`WebServer` with the browser authentication settings.

    ``SFO_WEB_TOKEN`` is an explicit opt-in override for non-TTY deployments
    (systemd, containers); by default a fresh per-process token is generated
    and only printed on a TTY.  The token is never persisted by us.
    """
    from .web.server import WebServer

    if not is_loopback_host(cfg.web.host):
        # L8: remote binds are explicit, but the TLS listener is Phase 5.  Be
        # honest at startup instead of implying the console is protected.
        print("[warn] web console is bound to a non-loopback address "
              f"({cfg.web.host}); it speaks plaintext HTTP until the Phase 5 "
              "TLS listener, so use an SSH tunnel or a TLS-terminating "
              "reverse proxy", file=sys.stderr)

    return WebServer(
        app,
        host=cfg.web.host,
        port=cfg.web.port,
        trusted_hosts=tuple(cfg.web.trusted_hosts),
        cookie_ttl_days=cfg.web.cookie_ttl_days,
        sse_heartbeat_s=cfg.web.sse_heartbeat_s,
        sse_max_clients=cfg.web.sse_max_clients,
        launch_token=os.environ.get("SFO_WEB_TOKEN") or None,
    )


def _print_web_url(server, label: str) -> None:
    """Print the login URL only on a TTY; never leak the token otherwise.

    systemd/journald capture stdout, so a token printed there would end up in
    the journal.  Non-TTY operators set ``SFO_WEB_TOKEN`` explicitly and use
    ``stackflowos status --token``; ``server.login_url`` remains available to
    in-process callers (tests, future ``sfo url`` control socket).
    """
    if sys.stdout.isatty():
        print(f"\n  {label}  →  {server.login_url}")
        print("  登录链接只属于本次进程：token 只在这里显示，不写日志、不落盘。")
    else:
        print(f"\n  {label}  →  {server.url}")
        print("  非交互终端：不打印登录 token。请在 TTY 中运行，"
              "或在启动 hub 前设置 SFO_WEB_TOKEN，再用 `stackflowos status --token` 访问。")


# ---------------------------------------------------------------------------
# hub / demo
# ---------------------------------------------------------------------------

def _hub(args) -> int:
    return asyncio.run(_run_hub(args))


async def _run_hub(args, *, demo: bool = False) -> int:
    from .app import App

    cfg = _build_config(args)
    setup_logging("INFO", cfg.paths.log_file)
    app = App(cfg)
    await app.start()
    server = None
    if not getattr(args, "no_web", False):
        server = _web_server(app, cfg)
        await server.start()
        _print_web_url(server, "StackFlowOS 已启动")
        print(f"  transport: {cfg.transport.mode}   state: {cfg.paths.state_dir}")
        print("  Ctrl-C 退出\n")
        if getattr(args, "open", False):
            try:
                import webbrowser
                webbrowser.open(server.login_url)
            except Exception:
                pass
    else:
        print(f"hub started (transport={cfg.transport.mode}, web disabled)")
    stop_event = asyncio.Event()

    def _stop(*_args):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # pragma: no cover
            pass
    try:
        await stop_event.wait()
    finally:
        if server is not None:
            await server.stop()
        await app.stop()
    return 0


async def _demo(args) -> int:
    from .app import App

    cfg = _build_config(args)
    setup_logging("INFO", cfg.paths.log_file)
    app = App(cfg)
    await app.start()
    server = None
    if not getattr(args, "no_web", False):
        server = _web_server(app, cfg)
        await server.start()
        _print_web_url(server, "StackFlowOS demo")

    await seed_demo(app, name=args.name, goal=args.goal)
    if getattr(args, "until_done", False):
        return await _watch_demo(app, timeout=args.timeout, server=server)
    stop_event = asyncio.Event()

    def _stop(*_args):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # pragma: no cover
            pass
    try:
        await stop_event.wait()
    finally:
        if server is not None:
            await server.stop()
        await app.stop()
    return 0


async def seed_demo(app, *, name: str, goal: str) -> dict:
    """Create one project, seed safe project-scoped rules, and let the OS work."""
    existing = [p for p in app.projects.list() if p.status in ("active", "review")]
    if existing:
        return app.project_card(existing[0].id)

    projects_root = str(app.paths.projects_dir)
    app.policy.add_rule(
        effect="allow", capability="fs.read", principal="*",
        scope={"path_prefix": projects_root}, risk_max="high",
        note="demo: project workspaces are readable", created_by="demo",
    )
    app.policy.add_rule(
        effect="allow", capability="fs.list", principal="*",
        scope={"path_prefix": projects_root}, risk_max="high",
        note="demo: project workspaces are listable", created_by="demo",
    )
    app.policy.add_rule(
        effect="allow", capability="fs.write", principal="*",
        scope={"path_prefix": projects_root}, risk_max="high",
        note="demo: workers may write inside project workspaces", created_by="demo",
    )
    app.policy.add_rule(
        effect="allow", capability="shell.exec", principal="*",
        scope={"command_regex": r"^(python3?|pytest|ls|cat|echo|mkdir|cp|mv|true|git)(?:\s.*)?$"},
        risk_max="high",
        note="demo: harmless project-local commands", created_by="demo",
    )
    # The mock coder runs a syntax check and the program's own selftest as one
    # chain.  M3's chain guard only lets a pattern match a chained command when
    # the pattern explicitly names an operator, so grant exactly that two-step
    # shape instead of widening the plain command rule above.
    app.policy.add_rule(
        effect="allow", capability="shell.exec", principal="*",
        scope={"command_regex": (
            r"^python3 -m py_compile [^;&|<>`$()\n]+ && "
            r"python3 [^;&|<>`$()\n]+ --selftest$")},
        risk_max="high",
        note="demo: coder's explicit syntax-check + selftest chain",
        created_by="demo",
    )
    app.policy.add_rule(
        effect="allow", capability="agent.spawn", principal="*",
        scope={}, risk_max="high",
        note="demo: the lead may hire workers", created_by="demo",
    )
    app.policy.add_rule(
        effect="allow", capability="project.update", principal="*",
        scope={}, risk_max="high",
        note="demo: the lead may hand a project to review", created_by="demo",
    )
    app.policy.add_rule(
        effect="allow", capability="data.read", principal="*",
        scope={}, risk_max="high",
        note="demo: read-only project data", created_by="demo",
    )
    app.policy.add_rule(
        effect="allow", capability="data.write", principal="*",
        scope={}, risk_max="high",
        note="demo: project memory writes", created_by="demo",
    )
    app.eventlog.append("demo.seeded", {
        "name": name, "goal": goal,
        "rules": ["fs.read", "fs.list", "fs.write", "shell.exec",
                  "agent.spawn", "data.read", "data.write"],
    }, source="operator")
    return await app.create_project(name=name, goal=goal, created_by="demo")



async def _run_aggregator(app, project_id: str, timeout: float = 25.0) -> None:
    """Run one data-aggregation agent so the project brief is filled in."""
    from .errors import SfoError

    existing = [a for a in app.supervisor.list(project_id=project_id)
                if a.role == "aggregator"]
    if any(a.status not in ("dead", "failed") for a in existing):
        return
    try:
        agent = await app.spawn_agent("aggregator", project_id)
    except SfoError as exc:
        print(f"[demo] could not run aggregator: {exc.message}")
        return
    agent_id = agent["id"] if isinstance(agent, dict) else agent.id
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(0.5)
        current = app.supervisor.get(agent_id)
        if current.status in ("done", "failed", "dead"):
            break

async def _watch_demo(app, *, timeout: int, server=None) -> int:
    deadline = time.time() + timeout
    project_id = None
    cards = [p for p in app.projects.list() if p.status in ("active", "review", "degraded")]
    if cards:
        project_id = cards[0].id
    while time.time() < deadline:
        await asyncio.sleep(2.0)
        if project_id is None:
            break
        card = app.project_card(project_id)
        counts = card.get("counts") or {}
        print(f"[demo] {card.get('status'):8s} progress={card.get('progress', 0):3d}% "
              f"todo={counts.get('todo', 0)} doing={counts.get('doing', 0)} "
              f"review={counts.get('review', 0)} done={counts.get('done', 0)} "
              f"agents={len(card.get('agents') or [])} "
              f"approvals={card.get('pending_approvals', 0)}")
        if card.get("status") in ("review", "done"):
            break
    if project_id:
        await _run_aggregator(app, project_id)
    card = app.project_card(project_id) if project_id else {}
    print("\n=== demo finished ===")
    print(json.dumps({k: card.get(k) for k in
                      ("id", "name", "status", "progress", "counts")},
                     ensure_ascii=False, indent=2))
    tasks = app.projects.task_list(project_id=project_id) if project_id else []
    for task in tasks:
        print(f"  [{task.status:7s}] {task.title}  ({task.role or '-'})")
    print(f"\nartifacts: {app.paths.projects_dir / project_id / 'workspace' if project_id else '-'}")
    if server is not None:
        await server.stop()
    await app.stop()
    return 0


# ---------------------------------------------------------------------------
# worker / doctor / status
# ---------------------------------------------------------------------------

def _worker_main(args) -> int:
    from .units.worker import run_worker

    async def _run() -> int:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()

        def _signal(*_a):
            if task is not None:
                task.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal)
            except NotImplementedError:  # pragma: no cover
                pass
        try:
            return await run_worker(args)
        except asyncio.CancelledError:
            return 0

    return asyncio.run(_run())


def _doctor(args) -> int:
    cfg = _build_config(args, for_web=False)
    problems: list[str] = []
    warnings: list[str] = []

    try:
        import pynng
        print(f"[ok] pynng {pynng.__version__}")
    except Exception as exc:
        problems.append(f"pynng import failed: {exc}")

    print(f"[ok] python {sys.version.split()[0]} at {sys.executable}")
    print(f"[ok] root      {cfg.paths.root}")
    print(f"[ok] state dir  {cfg.paths.state_dir}")
    try:
        cfg.paths.ensure()
        probe = cfg.paths.tmp_dir / ".write-probe"
        probe.write_text("ok", "utf-8")
        probe.unlink()
        print("[ok] state dir writable")
    except Exception as exc:
        problems.append(f"state dir not writable: {exc}")

    # web port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((cfg.web.host, cfg.web.port))
        print(f"[ok] web port {cfg.web.port} is free ({cfg.web.host})")
    except OSError as exc:
        warnings.append(f"web port {cfg.web.port} unavailable: {exc}")
    finally:
        sock.close()

    # transport pool
    if cfg.transport.mode == "tcp":
        start, end = cfg.transport.port_pool_start, cfg.transport.port_pool_end
        print(f"[ok] tcp port pool {start}-{end}")
    else:
        ipc_dir = cfg.transport.ipc_dir or cfg.paths.run_dir
        print(f"[ok] ipc directory {ipc_dir}")
        if len(str(ipc_dir).encode("utf-8")) > 60:
            print("[warn] ipc directory path is long; sockets will fall back to a "
                  "short temp directory (or use SFO_STATE_DIR=/tmp/sfo)")

    print(f"[ok] llm provider {cfg.llm.provider}"
          + (f" model={cfg.llm.model}" if cfg.llm.model else ""))

    log_size = 0
    current = cfg.paths.events_dir / "events.jsonl"
    if current.exists():
        log_size = current.stat().st_size
    print(f"[ok] event log {current} ({log_size} bytes)")

    for warning in warnings:
        print(f"[warn] {warning}")
    for problem in problems:
        print(f"[fail] {problem}")
    if problems:
        return 1
    print("\nAll checks passed. Start with: python3 -m stackflowos hub")
    return 0


def _status(args) -> int:
    cfg = _build_config(args, for_web=False)
    url = args.url or f"http://{cfg.web.host}:{cfg.web.port}"
    token = getattr(args, "token", None) or os.environ.get("SFO_WEB_TOKEN")
    request = urllib.request.Request(f"{url}/api/state")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print(f"hub at {url} requires authentication: pass --token "
                  "(or set SFO_WEB_TOKEN) with the login token printed by "
                  "`stackflowos hub` on a TTY", file=sys.stderr)
        else:
            print(f"cannot read hub at {url}: HTTP {exc.code}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"cannot reach hub at {url}: {exc}", file=sys.stderr)
        return 1
    data = payload.get("data") or {}
    info = data.get("sys") or {}
    stats = data.get("stats") or {}
    print(f"{info.get('name')} {info.get('version')} @ {info.get('host')} "
          f"({info.get('transport')})  uptime={stats.get('uptime_s')}s")
    print(f"units={stats.get('units', {}).get('online', 0)} "
          f"agents={len(data.get('agents') or [])} "
          f"projects={len(data.get('projects') or [])} "
          f"leases={stats.get('leases', 0)} "
          f"approvals={stats.get('pending_approvals', 0)} "
          f"events={stats.get('events', 0)}")
    for card in data.get("projects") or []:
        counts = card.get("counts") or {}
        print(f"  {card.get('id')}  {card.get('name')}  {card.get('status')}  "
              f"{card.get('progress', 0)}%  "
              f"(todo {counts.get('todo', 0)}, doing {counts.get('doing', 0)}, "
              f"review {counts.get('review', 0)}, done {counts.get('done', 0)})")
    for ap in data.get("approvals") or []:
        print(f"  approval {ap.get('id')} {ap.get('capability')} "
              f"risk={ap.get('risk')} principal={ap.get('principal')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
