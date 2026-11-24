"""Import-guarded bridge to the optional OS sandbox (``security.sandbox``).

The security workstream owns ``stackflowos/security/sandbox.py``.  Agents only
*use* it when it says it can isolate something; this module never imports it at
module import time and never lets a missing/broken sandbox stop the worker:

* ``security.sandbox == "off"`` (default) -> no-op, original behaviour;
* module missing -> ``{"status": "unavailable", ...}`` and the original command
  runs unsandboxed, with the reason logged once;
* detection/apply failure -> ``{"status": "failed", ...}``, same fallback.

Two integration points:

* :func:`wrap_shell_command` -- ``agents/tools.py`` wraps each ``shell.exec``
  command before spawning it (per-call sandbox);
* :func:`apply_process_sandbox` -- ``units/worker.py`` restricts the worker
  process itself (Landlock: write only the project workspace + state tmp/logs,
  reads everywhere, optional network) before tools start.

The exact helper names are resolved defensively against a small set of
candidates, so the security module can land with any of the obvious names
without another edit here.  The returned dict always includes ``status`` and
``reason`` so callers can audit the decision.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("sfo.agents.sandbox")

#: Shell command wrapper candidates (in preference order).
_SHELL_WRAPPERS = (
    "wrap_shell_command", "wrap_command", "sandbox_command",
    "build_shell_command", "shell_command", "wrap_shell",
    "sandboxed_command", "wrap_exec",
)
#: Process restriction candidates (in preference order).
_PROCESS_RESTRICTORS = (
    "restrict_current_process", "restrict_process", "apply_landlock",
    "restrict", "apply_process_sandbox", "sandbox_current_process",
)
#: Reasons already logged (avoid one warning per shell.exec).
_REPORTED: set[tuple[str, str]] = set()

_OFF_VALUES = frozenset({"", "off", "none", "disabled", "no"})


def reset_warning_cache() -> None:
    """Clear the once-per-reason log cache (tests)."""
    _REPORTED.clear()


def _warn_once(key: str, reason: str, *args: Any) -> None:
    marker = (key, reason)
    if marker in _REPORTED:
        return
    _REPORTED.add(marker)
    log.warning("sandbox %s: %s", key, reason % args if args else reason)


def fetch_module() -> Any:
    """Import ``stackflowos.security.sandbox`` (or return None).

    A missing module is normal during development; any import-time failure is
    treated as "unavailable" instead of propagating.
    """
    try:
        from ..security import sandbox as module  # noqa: F401 - optional
    except Exception as exc:  # ImportError, but also broken optional deps
        _warn_once("unavailable",
                   "stackflowos.security.sandbox is not importable: %s",
                   f"{type(exc).__name__}: {exc}")
        return None
    return module


def sandbox_mode(config: Any) -> str:
    """Normalised ``security.sandbox`` value (``off`` when missing/invalid)."""
    value = str(getattr(config, "sandbox", "off") or "off").strip().lower()
    return "off" if value in _OFF_VALUES else value


def allow_network(config: Any) -> bool:
    return bool(getattr(config, "sandbox_allow_network", False))


def _tmp_mb(config: Any) -> int:
    try:
        return max(16, min(65536, int(getattr(config, "sandbox_tmp_mb", 256))))
    except (TypeError, ValueError):
        return 256


def _first_callable(module: Any, names: Iterable[str]) -> tuple[str, Any]:
    for name in names:
        function = getattr(module, name, None)
        if callable(function):
            return name, function
    return "", None


def _normalise_command(result: Any) -> tuple[Any, bool]:
    """Return ``(command, use_exec)`` from a wrapper's return value."""
    if isinstance(result, str):
        return result, False
    if isinstance(result, (list, tuple)):
        parts = list(result)
        if parts and all(isinstance(item, str) for item in parts):
            return parts, True
        if len(parts) == 2 and isinstance(parts[0], (str, list, tuple)):
            command, _meta = parts
            if isinstance(command, str):
                return command, False
            if isinstance(command, (list, tuple)) and all(
                    isinstance(item, str) for item in command):
                return list(command), True
        return None, False
    if isinstance(result, dict):
        argv = result.get("argv") or result.get("args")
        if isinstance(argv, (list, tuple)) and then_all_str(argv):
            return list(argv), True
        command = result.get("command") or result.get("cmd")
        if isinstance(command, str):
            return command, False
        if isinstance(command, (list, tuple)) and then_all_str(command):
            return list(command), True
        return None, False
    argv = getattr(result, "argv", None)
    if isinstance(argv, (list, tuple)) and then_all_str(argv):
        return list(argv), True
    command = getattr(result, "command", None)
    if isinstance(command, str):
        return command, False
    return None, False


def then_all_str(values: Any) -> bool:
    return isinstance(values, (list, tuple)) and bool(values) and all(
        isinstance(item, str) for item in values)


def wrap_shell_command(config: Any, command: str, *, cwd: Path | str = ".",
                       writable_paths: Iterable[Path | str] | None = None,
                       logger: Any = None) -> dict:
    """Sandbox one ``shell.exec`` command, or return it unchanged.

    Returns ``{"command", "use_exec", "preexec", "status", "mode", "backend",
    "reason", "notes"}``.  ``status`` is ``disabled``/``unavailable``/
    ``wrapped``/``failed``; callers keep the original command unless status is
    ``wrapped``.

    The security module's stable API is ``wrap_command(argv, ...) ->
    SandboxPlan`` (argv/backend/preexec_fn/notes).  Older/simpler modules are
    accepted through ``build_command``/string-wrapper fallbacks so this bridge
    survives a differently-shaped sandbox implementation.
    """
    mode = sandbox_mode(config)
    report: dict[str, Any] = {"command": command, "use_exec": False,
                              "preexec": None, "status": "disabled",
                              "mode": mode,
                              "backend": mode if mode != "off" else "off",
                              "reason": "", "notes": []}
    if mode == "off":
        return report
    module = fetch_module()
    if module is None:
        report.update(status="unavailable",
                      reason="stackflowos.security.sandbox is not available")
        return report
    network = allow_network(config)
    tmp_mb = _tmp_mb(config)
    writable = [str(Path(item)) for item in (
        writable_paths if writable_paths is not None else [cwd])]
    argv = ["/bin/sh", "-c", command]
    plan = _sandbox_plan(module, argv, cwd=str(cwd), writable=writable,
                         network=network, tmp_mb=tmp_mb, mode=mode,
                         report=report)
    if plan is None:
        return report
    plan_argv, backend, preexec, notes = plan

    def _unsandboxed() -> dict:
        report.update(
            status="unavailable" if backend == "unsandboxed" else "failed",
            backend=backend or "unsandboxed", notes=list(notes),
            reason=(notes[0] if notes
                    else "no usable sandbox backend for this host"))
        _warn_once("shell", "%s; running the command unsandboxed",
                   report["reason"])
        return report

    if backend in ("", "unsandboxed", None):
        return _unsandboxed()
    if not plan_argv or not then_all_str(plan_argv):
        report.update(status="failed", reason="sandbox plan has no argv")
        _warn_once("shell", "%s; running the command unsandboxed",
                   report["reason"])
        return report
    report.update(status="wrapped", command=list(plan_argv), use_exec=True,
                  preexec=preexec, backend=str(backend), notes=list(notes),
                  reason="")
    return report


def _sandbox_plan(module: Any, argv: list[str], *, cwd: str,
                  writable: list[str], network: bool, tmp_mb: int,
                  mode: str, report: dict) -> tuple | None:
    """Resolve the module's command API into ``(argv, backend, preexec, notes)``.

    Returns ``None`` (after updating ``report``) when no callable API exists.
    """
    wrap = getattr(module, "wrap_command", None)
    if callable(wrap):
        calls = (
            lambda: wrap(argv, cwd=cwd, writable_paths=writable,
                         allow_network=network, backend=mode, tmp_mb=tmp_mb),
            lambda: wrap(argv, cwd=cwd, writable_paths=writable,
                         allow_network=network, backend=mode),
            lambda: wrap(argv, cwd=cwd, writable_paths=writable,
                         allow_network=network),
            lambda: wrap(argv, cwd=cwd, writable_paths=writable),
        )
        for call in calls:
            try:
                plan = call()
            except TypeError:
                continue
            except Exception as exc:
                report.update(status="failed",
                              reason=f"{type(exc).__name__}: {exc}")
                _warn_once("shell", "failed (%s); running unsandboxed",
                           report["reason"])
                return None
            returned = _unpack_plan(plan)
            if returned is not None:
                return returned
            report.update(status="failed",
                          reason="sandbox returned an unsupported plan")
            return None
    build = getattr(module, "build_command", None)
    if callable(build):
        try:
            built = build(argv, cwd=cwd, writable_paths=writable,
                          allow_network=network, backend=mode, tmp_mb=tmp_mb)
        except TypeError:
            built = build(argv, cwd=cwd, writable_paths=writable,
                          allow_network=network, backend=mode)
        except Exception as exc:
            report.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
            return None
        if isinstance(built, tuple) and len(built) == 3:
            built_argv, backend, notes = built
            preexec = None
            if backend == "landlock":
                factory = getattr(module, "preexec_landlock", None)
                if callable(factory):
                    preexec = factory(writable, network)
            return built_argv, backend, preexec, list(notes or ())
        report.update(status="failed", reason="build_command returned junk")
        return None
    # Last resort: a simple string/list wrapper (older sandbox modules).
    name, function = _first_callable(module, _SHELL_WRAPPERS)
    if function is None:
        report.update(status="unavailable",
                      reason="security.sandbox exposes no command API")
        _warn_once("shell", "%s", report["reason"])
        return None
    for call in (lambda: function(command_from_argv(argv),
                                  allow_network=network, tmp_mb=tmp_mb,
                                  cwd=cwd),
                 lambda: function(command_from_argv(argv),
                                  allow_network=network, cwd=cwd),
                 lambda: function(command_from_argv(argv))):
        try:
            result = call()
        except TypeError:
            continue
        except Exception as exc:
            report.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
            return None
        wrapped, use_exec = _normalise_command(result)
        if wrapped is None:
            continue
        report.update(status="wrapped", command=wrapped, use_exec=use_exec,
                      backend=mode)
        return None  # already recorded in report; caller sees status wrapped
    return None


def command_from_argv(argv: list[str]) -> str:
    """Recover the original shell command from ``["/bin/sh", "-c", cmd]``."""
    if len(argv) == 3 and argv[0].endswith("sh") and argv[1] == "-c":
        return argv[2]
    return " ".join(argv)


def _unpack_plan(plan: Any) -> tuple | None:
    """``(argv, backend, preexec_fn, notes)`` from a SandboxPlan-like object."""
    if plan is None:
        return None
    argv = getattr(plan, "argv", None)
    backend = getattr(plan, "backend", None)
    if argv is None and isinstance(plan, dict):
        argv = plan.get("argv") or plan.get("args")
        backend = plan.get("backend", backend)
    if not then_all_str(argv):
        return None
    preexec = getattr(plan, "preexec_fn", None)
    notes = getattr(plan, "notes", ())
    if isinstance(plan, dict):
        preexec = plan.get("preexec_fn", preexec)
        notes = plan.get("notes", notes)
    return list(argv), str(backend or ""), preexec, list(notes or ())


def apply_process_sandbox(config: Any, writable_paths: Iterable[Path | str], *,
                          logger: Any = None) -> dict:
    """Restrict the current process via ``security.sandbox`` (Landlock).

    Writes are limited to ``writable_paths``; reads stay unrestricted (Landlock
    semantics).  Any failure is reported, never raised: the worker keeps running
    unsandboxed and logs the reason, matching the fail-open-for-availability /
    explicit-reporting policy for the optional sandbox.
    """
    mode = sandbox_mode(config)
    paths = [str(Path(item)) for item in writable_paths]
    report: dict[str, Any] = {"status": "disabled", "mode": mode,
                              "backend": mode if mode != "off" else "off",
                              "writable_paths": paths, "reason": ""}
    if mode == "off":
        return report
    module = fetch_module()
    if module is None:
        report.update(status="unavailable",
                      reason="stackflowos.security.sandbox is not available")
        return report
    if mode == "bwrap":
        # bwrap wraps one child command; it cannot restrict an already-running
        # process.  The worker itself only has the Landlock in-process path.
        report.update(
            status="unavailable",
            reason="bwrap cannot restrict the current process; "
                   "use landlock/auto for the worker (shell.exec still wraps)")
        _warn_once("process", "%s", report["reason"])
        return report
    detect = getattr(module, "detect_landlock", None)
    if callable(detect):
        try:
            status = detect()
        except Exception:  # pragma: no cover - detection is best effort
            status = None
        if status is not None and not bool(getattr(status, "available", True)):
            report.update(
                status="unavailable",
                reason=str(getattr(status, "reason", "") or
                           "landlock is unavailable on this host"))
            _warn_once("process", "%s", report["reason"])
            return report
    name, function = _first_callable(module, _PROCESS_RESTRICTORS)
    if function is None:
        report.update(status="unavailable",
                      reason="security.sandbox exposes no process restrictor")
        _warn_once("process", "%s", report["reason"])
        return report
    network = allow_network(config)
    calls = (
        lambda: function(writable_paths=paths, allow_network=network),
        lambda: function(writable_paths=paths, allow_network=network,
                         backend=mode),
        lambda: function(paths, allow_network),
        lambda: function(writable_paths=paths),
        lambda: function(paths),
    )
    last_error = ""
    for call in calls:
        try:
            result = call()
        except TypeError as exc:
            last_error = f"TypeError: {exc}"
            continue
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            break
        detail = result if isinstance(result, dict) else {}
        backend = (detail.get("backend")
                   or getattr(result, "backend", None)
                   or getattr(module, "BACKEND", "")
                   or mode)
        report.update(status="applied", backend=str(backend), reason="")
        notes = getattr(result, "notes", None)
        if notes:
            report["notes"] = list(notes)
            # ``network_restricted`` is the security module's honest summary.
            report["network_restricted"] = bool(
                getattr(result, "network_restricted", False))
        if detail:
            report["detail"] = detail
        return report
    report.update(status="failed",
                  reason=last_error or "process sandbox failed")
    _warn_once("process", "failed (%s); worker keeps running unsandboxed",
               report["reason"])
    return report


def probe(config: Any) -> dict:
    """Detection summary without touching the current process (health/UI)."""
    mode = sandbox_mode(config)
    if mode == "off":
        return {"mode": "off", "available": False, "status": "disabled"}
    module = fetch_module()
    if module is None:
        return {"mode": mode, "available": False, "status": "unavailable"}
    shell = _first_callable(module, _SHELL_WRAPPERS)[0]
    process = _first_callable(module, _PROCESS_RESTRICTORS)[0]
    return {"mode": mode, "available": bool(shell or process),
            "shell_wrapper": shell, "process_restrictor": process,
            "status": "available" if (shell or process) else "unavailable"}


__all__ = [
    "allow_network",
    "apply_process_sandbox",
    "fetch_module",
    "probe",
    "reset_warning_cache",
    "sandbox_mode",
    "wrap_shell_command",
]
