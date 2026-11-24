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

    project = sub.add_parser(
        "project", help="project onboarding / health (state reads only)")
    project_sub = project.add_subparsers(dest="project_command")
    project_health = project_sub.add_parser(
        "health", help="check whether a project is ready for an agent")
    project_health.add_argument("project_id")
    project_health.add_argument("--json", action="store_true",
                                help="print the raw report")
    _add_common(project_health)
    project_scaffold = project_sub.add_parser(
        "scaffold", help="create the fixed workspace layout + brief.md")
    project_scaffold.add_argument("project_id")
    project_scaffold.add_argument("--json", action="store_true")
    _add_common(project_scaffold)

    events = sub.add_parser(
        "events", help="replay the local event stream (agent/source/time filters)")
    events.add_argument("--project", default=None)
    events.add_argument("--agent", default=None, help="agent id (agent-...)")
    events.add_argument("--source", default=None, help="exact event source")
    events.add_argument("--types", default=None,
                        help="comma-separated types or prefixes, e.g. tool,agent")
    events.add_argument("--since-ts", type=int, default=None,
                        help="inclusive start timestamp (epoch ms)")
    events.add_argument("--until-ts", type=int, default=None,
                        help="inclusive end timestamp (epoch ms)")
    events.add_argument("--limit", type=int, default=200)
    events.add_argument("--json", action="store_true",
                        help="print raw JSONL records")
    _add_common(events)

    status = sub.add_parser("status", help="print a running hub's state")
    status.add_argument("--url", default=None, help="hub base URL (default: config web host/port)")
    status.add_argument("--token", default=None,
                        help="web launch token for Authorization: Bearer "
                             "(default: $SFO_WEB_TOKEN)")

    url = sub.add_parser("url", help="mint a fresh browser login URL via the "
                                     "hub control socket")
    _add_common(url)
    url.add_argument("--json", action="store_true", help="print the raw reply")
    url.add_argument("--status", action="store_true",
                     help="print control-socket status instead of a URL")

    backup = sub.add_parser("backup", help="create a state archive")
    _add_common(backup)
    backup.add_argument("--dest", default=None, help="target directory/file")
    backup.add_argument("--keep", type=int, default=None,
                        help="keep the newest N archives")

    restore = sub.add_parser("restore", help="restore a state archive")
    _add_common(restore)
    restore.add_argument("archive", help="sfo-backup-*.tar.gz")
    restore.add_argument("--force", action="store_true",
                         help="replace a non-empty state directory")

    export_events = sub.add_parser("export-events",
                                   help="export the event stream (jsonl/csv)")
    _add_common(export_events)
    export_events.add_argument("--format", choices=("jsonl", "csv"),
                               default="jsonl")
    export_events.add_argument("--since", type=int, default=None)
    export_events.add_argument("--until", type=int, default=None)
    export_events.add_argument("--types", default=None,
                               help="comma-separated event types")
    export_events.add_argument("--project", default=None)
    export_events.add_argument("--out", default=None)

    backup_status = sub.add_parser("backup-status",
                                   help="show backup size/count")
    _add_common(backup_status)
    backup_status.add_argument("--json", action="store_true")

    migrate = sub.add_parser("storage-migrate",
                             help="copy JSON state into the SQLite backend")
    _add_common(migrate)
    migrate.add_argument("--sqlite-path", default=None)
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--include-projects", action="store_true")

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
    parser.add_argument("--standby", action="store_true", default=None,
                        help="cold-standby hub: wait for the state-dir lock "
                             "instead of exiting when another hub is active")
    parser.add_argument("--tls-cert", default=None,
                        help="PEM certificate for the HTTPS listener")
    parser.add_argument("--tls-key", default=None,
                        help="PEM private key for the HTTPS listener")


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
    if args.command == "project":
        return _run_cli(lambda: _project_cmd(args))
    if args.command == "events":
        return _run_cli(lambda: _events_cmd(args))
    if args.command == "doctor":
        return _run_cli(lambda: _doctor(args))
    if args.command == "status":
        return _run_cli(lambda: _status(args))
    if args.command == "url":
        return _run_cli(lambda: _url(args))
    if args.command == "backup":
        return _run_cli(lambda: _backup(args))
    if args.command == "restore":
        return _run_cli(lambda: _restore(args))
    if args.command == "export-events":
        return _run_cli(lambda: _export_events(args))
    if args.command == "backup-status":
        return _run_cli(lambda: _backup_status(args))
    if args.command == "storage-migrate":
        return _run_cli(lambda: _storage_migrate(args))
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
    if getattr(args, "standby", None):
        patch.setdefault("cluster", {})["standby"] = True
    if getattr(args, "tls_cert", None):
        patch.setdefault("web", {})["tls_cert"] = args.tls_cert
    if getattr(args, "tls_key", None):
        patch.setdefault("web", {})["tls_key"] = args.tls_key
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

    TLS, multi-user accounts, the session bound and single-use launch tokens
    all come from ``config.web``; the explicit arguments keep a live config
    patch from being shadowed by a stale server-side default.
    """
    from .web.server import WebServer

    server = WebServer(
        app,
        host=cfg.web.host,
        port=cfg.web.port,
        trusted_hosts=tuple(cfg.web.trusted_hosts),
        cookie_ttl_days=cfg.web.cookie_ttl_days,
        sse_heartbeat_s=cfg.web.sse_heartbeat_s,
        sse_max_clients=cfg.web.sse_max_clients,
        launch_token=os.environ.get("SFO_WEB_TOKEN") or None,
        tls_cert=cfg.web.tls_cert or None,
        tls_key=cfg.web.tls_key or None,
        tls_min_version=cfg.web.tls_min_version,
        users=cfg.web.users,
        session_max=cfg.web.session_max,
        launch_token_single_use=cfg.web.launch_token_single_use,
    )
    # Honest startup: a non-loopback plaintext listener is a real risk, and a
    # stale certificate path must be visible before the first request.
    for note in getattr(server, "security_notes", ()):
        print(f"[warn] {note}", file=sys.stderr)
    if not is_loopback_host(cfg.web.host) and not server.tls_enabled:
        print("[warn] web console is bound to a non-loopback address without "
              "TLS; use --tls-cert/--tls-key (or web.tls_* in config), an SSH "
              "tunnel, or a TLS-terminating reverse proxy", file=sys.stderr)
    return server


def _print_web_url(server, label: str) -> None:
    """Print the login URL only on a TTY; never leak the token otherwise.

    systemd/journald capture stdout, so a token printed there would end up in
    the journal.  A non-TTY operator mints a one-shot URL from the same machine
    with ``stackflowos url`` (operator control socket, same-uid only), or sets
    ``SFO_WEB_TOKEN`` explicitly as an escape hatch.
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
    if any(a.status not in ("dead", "blocked", "failed") for a in existing):
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
        if current.status in ("done", "blocked", "failed", "dead"):
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


def _read_project_record(paths, project_id: str) -> tuple[dict, list[dict]] | None:
    """Read one project record directly from state (no store writes)."""
    from .util.jsonio import read_json

    try:
        raw = read_json(paths.state_dir / "state" / "projects.json",
                        default=None)
    except (OSError, ValueError) as exc:
        print(f"cannot read project state: {exc}", file=sys.stderr)
        return None
    if not isinstance(raw, dict):
        return None
    for item in raw.get("projects") or []:
        if isinstance(item, dict) and str(item.get("id")) == project_id:
            tasks = [task for task in (item.get("tasks") or [])
                     if isinstance(task, dict)]
            return item, tasks
    print(f"project not found: {project_id}", file=sys.stderr)
    return None


def _offline_project_health(cfg, project: dict, tasks: list[dict]) -> dict:
    """Build a health report from state files only (safe while the hub runs)."""
    from .kernel import Context
    from .projects.health import build_report
    from .security import PolicyEngine
    from .util.jsonio import read_json

    project_id = str(project.get("id") or "")
    memory_path = cfg.paths.state_dir / "state" / "memory" / f"{project_id}.json"
    try:
        memory = read_json(memory_path, default={})
    except (OSError, ValueError):
        memory = {}
    if not isinstance(memory, dict):
        memory = {}
    ctx = Context(name="cli-project-health")
    # ``eventlog=None`` is deliberate: an offline check must not write a
    # second sequence into the live log.  PolicyEngine only needs the log for
    # rule.* events; evaluate() is pure.
    policy = PolicyEngine(
        ctx, None,
        rules_path=cfg.paths.state_dir / "state" / "rules.json",
        config=cfg.security,
        projects_root=cfg.paths.projects_dir,
        workspace_resolver=lambda pid: (
            project.get("workspace") if str(pid) == project_id else None),
    )
    return build_report(
        project, tasks, memory=memory, policy=policy, agents=None,
        eventlog=None, paths=cfg.paths)


def _project_cmd(args) -> int:
    command = getattr(args, "project_command", None)
    if command == "health":
        return _project_health_cmd(args)
    if command == "scaffold":
        return _project_scaffold_cmd(args)
    print("usage: sfo project {health|scaffold} <project_id>", file=sys.stderr)
    return 2


def _project_health_cmd(args) -> int:
    cfg = _build_config(args, for_web=False)
    loaded = _read_project_record(cfg.paths, args.project_id)
    if loaded is None:
        return 1
    project, tasks = loaded
    report = _offline_project_health(cfg, project, tasks)
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["summary"]
        print(f"{report['project_id']}  {report['status']}  "
              f"ok={summary['ok']} warn={summary['warn']} fail={summary['fail']}"
              f" unchecked={summary['unchecked']}")
        for check in report["checks"]:
            marker = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[fail]"}.get(
                check["status"], "[????]")
            print(f"{marker} {check['label']}: {check['message']}")
            fix = check.get("fix")
            if fix and check["status"] != "ok":
                print(f"        fix: {fix}")
        if report.get("next_actions"):
            print("\n下一步：")
            for action in report["next_actions"]:
                print(f"- {action}")
    return 0 if report.get("ready") else 1


def _project_scaffold_cmd(args) -> int:
    from .projects import onboarding as project_onboarding

    cfg = _build_config(args, for_web=False)
    loaded = _read_project_record(cfg.paths, args.project_id)
    if loaded is None:
        return 1
    project, _tasks = loaded
    result = project_onboarding.ensure_scaffold(project, write=True)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"workspace: {result['workspace']}")
        print(f"created: {', '.join(result['created']) or '-'}")
        print(f"missing: {', '.join(result['missing']) or '-'}")
        print(f"brief:   {result['brief']}")
        if result.get("refused"):
            print(f"refused: {result['refused']}")
        for warning in result.get("warnings") or []:
            print(f"[warn] {warning}")
        print("\n提示：scaffold 只写 workspace；看板种子任务与项目记忆由 hub 的 "
              "POST /api/projects/<id>/onboard 补齐。")
    return 0 if not result.get("missing") and not result.get("refused") else 1


def _events_cmd(args) -> int:
    """Replay the local event stream with agent/source/time filters."""
    from .l2.eventlog import EventLog

    cfg = _build_config(args, for_web=False)
    log = EventLog(cfg.paths.events_dir)
    types = [item.strip() for item in (args.types or "").split(",")
             if item.strip()] or None
    records = log.query(
        project_id=args.project or None,
        types=types,
        source=args.source or None,
        agent_id=args.agent or None,
        since_ts=args.since_ts,
        until_ts=args.until_ts,
        limit=max(1, min(int(args.limit or 200), log.QUERY_MAX_LIMIT)),
    )
    for record in records:
        row = record.to_dict()
        if getattr(args, "json", False):
            print(json.dumps(row, ensure_ascii=False))
            continue
        data = json.dumps(row.get("data") or {}, ensure_ascii=False,
                          default=str)
        if len(data) > 240:
            data = data[:239] + "…"
        print(f"{row.get('seq'):>7}  {row.get('ts')}  {row.get('type'):<24}  "
              f"{row.get('source'):<22}  {row.get('project_id') or '-':<6}  {data}")
    if not records:
        print("no matching events", file=sys.stderr)
    return 0


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


def _backup(args) -> int:
    cfg = _build_config(args, for_web=False)
    from .observability.backup import backup_status, create_backup

    path = create_backup(cfg.paths, dest=getattr(args, "dest", None),
                         keep=getattr(args, "keep", None))
    print(path)
    status = backup_status(cfg.paths)
    print(f"backups={status.get('backups', 0)} bytes={status.get('bytes', 0)}")
    return 0


def _restore(args) -> int:
    # Do not call Config.load here: resolving a config ensures the state tree,
    # which would make a fresh restore look like a non-empty target.
    from .config import resolve_paths
    from .observability.backup import restore

    paths = resolve_paths(
        root=Path(args.root) if getattr(args, "root", None) else None,
        state_dir=Path(args.state_dir) if getattr(args, "state_dir", None) else None)
    result = restore(args.archive, paths.state_dir,
                     force=bool(getattr(args, "force", False)))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("backup"):
        print(f"pre-restore backup: {result['backup']}", file=sys.stderr)
    return 0


def _export_events(args) -> int:
    cfg = _build_config(args, for_web=False)
    from .observability.backup import export_events

    path = export_events(
        cfg.paths, format=args.format, since=args.since, until=args.until,
        types=[item.strip() for item in args.types.split(",") if item.strip()]
        if args.types else None,
        project_id=args.project, dest=args.out)
    print(path)
    return 0


def _backup_status(args) -> int:
    cfg = _build_config(args, for_web=False)
    from .observability.backup import backup_status

    status = backup_status(cfg.paths)
    if getattr(args, "json", False):
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(f"backups={status.get('backups', 0)} bytes={status.get('bytes', 0)} "
              f"dir={status.get('dir')} last={status.get('last_backup')}")
    return 0


def _storage_migrate(args) -> int:
    cfg = _build_config(args, for_web=False)
    from .storage import migrate_json_to_sqlite

    result = migrate_json_to_sqlite(
        cfg.paths, sqlite_path=args.sqlite_path, dry_run=bool(args.dry_run),
        include_projects=bool(args.include_projects))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _url(args) -> int:
    """Mint a fresh login URL (or read status) via the operator control socket.

    The socket lives under the same state directory, is mode 0600 and checks
    ``SO_PEERCRED``: only the hub's own uid can ask.  Tokens are never written
    to disk, which is what makes non-TTY deployments usable without pinning a
    long-lived ``SFO_WEB_TOKEN``.
    """
    cfg = _build_config(args, for_web=False)
    from .security.control import control_socket_path, query_control_socket

    action = "status" if getattr(args, "status", False) else "url"
    reply = query_control_socket(control_socket_path(cfg.paths.run_dir), action)
    if getattr(args, "json", False):
        print(json.dumps(reply, ensure_ascii=False))
        return 0 if reply.get("ok") else 1
    if not reply.get("ok"):
        print(f"control socket unavailable: {reply.get('error', 'unknown error')}",
              file=sys.stderr)
        print("the hub must be running in the same state directory "
              f"({cfg.paths.state_dir}); SFO_WEB_TOKEN remains the non-TTY "
              "escape hatch", file=sys.stderr)
        return 1
    if action == "status":
        print(f"hub={reply.get('url')}  tls={reply.get('tls')}  "
              f"running={reply.get('running')}  sessions={reply.get('sessions')}")
        return 0
    print(reply.get("url", ""))
    return 0


def _status(args) -> int:
    cfg = _build_config(args, for_web=False)
    scheme = "https" if cfg.web.tls_cert else "http"
    url = args.url or f"{scheme}://{cfg.web.host}:{cfg.web.port}"
    token = getattr(args, "token", None) or os.environ.get("SFO_WEB_TOKEN")
    if not token:
        # No long-lived secret at hand: the same-uid control socket exposes a
        # data-free liveness summary, which is enough for a text status.
        from .security.control import control_socket_path, query_control_socket

        reply = query_control_socket(control_socket_path(cfg.paths.run_dir),
                                     "status")
        if reply.get("ok"):
            print(f"hub running at {reply.get('url')}  "
                  f"tls={reply.get('tls')}  sessions={reply.get('sessions')}")
            print("run `stackflowos status --token <token>` (or open the hub "
                  "UI) for the full state")
            return 0
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
