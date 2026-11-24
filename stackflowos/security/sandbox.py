"""OS-level sandbox capability detection and command wrapping (Phase 5).

The approval layer asks a human before a shell command runs; this module is
the *kernel* boundary behind that.  It is deliberately honest:

* :func:`detect_landlock` actually probes the Landlock ABI with the
  ``landlock_create_ruleset(..., LANDLOCK_CREATE_RULESET_VERSION)`` syscall.
* :func:`detect_bwrap` requires a *functional* bubblewrap probe
  (``bwrap --ro-bind / / true``); a binary on ``$PATH`` whose user namespaces
  are disabled is reported unavailable.
* :func:`detect_backend` defaults to ``auto`` and prefers Landlock (works
  unprivileged), then bubblewrap, and otherwise returns ``"unsandboxed"``.
* :func:`build_command` / :func:`wrap_command` never silently claim isolation:
  when a backend cannot be applied they return the original argv with backend
  ``"unsandboxed"`` and a concrete reason in ``notes``.

Stable API for the agent workflow (``shell.exec`` / worker startup)::

    from stackflowos.security.sandbox import (
        detect_backend, detect_landlock, detect_bwrap,
        restrict_current_process, preexec_landlock,
        build_command, wrap_command,
    )

    plan = wrap_command(argv, cwd=workspace,
                        writable_paths=[workspace, tmp_dir, logs_dir],
                        allow_network=False,
                        backend=cfg.security.sandbox)  # off|auto|bwrap|landlock
    subprocess.Popen(plan.argv, cwd=..., preexec_fn=plan.preexec_fn)

Landlock rules: read/execute stay allowed everywhere; only filesystem
*writes* (create/remove/rename/write/truncate) are handled, and allowed only
beneath the caller's ``writable_paths``.  On ABI >= 4 an allow_network=False
sandbox also denies TCP bind/connect (Landlock does not cover UDP; bubblewrap's
``--unshare-net`` is the stronger option).  Landlock must be applied in the
child before ``exec`` - :func:`preexec_landlock` returns a callable for
``subprocess``'s ``preexec_fn``, and :func:`restrict_current_process` does it
in-process (a dedicated worker process can call that once at startup).
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

# ----------------------------------------------------------------------
# constants
# ----------------------------------------------------------------------
BACKEND_BWRAP = "bwrap"
BACKEND_LANDLOCK = "landlock"
BACKEND_UNSANDBOXED = "unsandboxed"
_SANDBOX_OFF = frozenset({"", "off", "none", "false", "0"})
_VALID_PREFERENCE = frozenset({"auto", BACKEND_BWRAP, BACKEND_LANDLOCK,
                               *_SANDBOX_OFF})

#: Landlock syscall numbers (generic tables: x86_64, aarch64, riscv64).
_LANDLOCK_SYSCALLS = {
    "x86_64": (444, 445, 446),
    "amd64": (444, 445, 446),
    "aarch64": (444, 445, 446),
    "arm64": (444, 445, 446),
    "riscv64": (444, 445, 446),
}
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

#: ``include/uapi/linux/landlock.h`` access bits.
LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
LANDLOCK_ACCESS_FS_REFER = 1 << 13        # ABI 2
LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14     # ABI 3

#: Everything that can mutate the filesystem.  Read/execute are intentionally
#: absent: they remain allowed everywhere.
_FS_WRITE_BASE = (
    LANDLOCK_ACCESS_FS_WRITE_FILE
    | LANDLOCK_ACCESS_FS_REMOVE_DIR
    | LANDLOCK_ACCESS_FS_REMOVE_FILE
    | LANDLOCK_ACCESS_FS_MAKE_CHAR
    | LANDLOCK_ACCESS_FS_MAKE_DIR
    | LANDLOCK_ACCESS_FS_MAKE_REG
    | LANDLOCK_ACCESS_FS_MAKE_SOCK
    | LANDLOCK_ACCESS_FS_MAKE_FIFO
    | LANDLOCK_ACCESS_FS_MAKE_BLOCK
    | LANDLOCK_ACCESS_FS_MAKE_SYM
)

LANDLOCK_ACCESS_NET_BIND_TCP = 1 << 0     # ABI 4
LANDLOCK_ACCESS_NET_CONNECT_TCP = 1 << 1  # ABI 4

_BWRAP_PROBE_TIMEOUT_S = 5.0


class SandboxError(RuntimeError):
    """A sandbox backend could not be applied."""


class LandlockUnavailable(SandboxError):
    """The running kernel does not expose Landlock."""


# ----------------------------------------------------------------------
# detection
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class LandlockStatus:
    available: bool
    abi: int = 0
    reason: str = ""

    @property
    def supports_network(self) -> bool:
        return self.available and self.abi >= 4


@dataclass(frozen=True)
class BackendInfo:
    backend: str
    available: bool
    detail: str = ""
    path: str = ""
    landlock_abi: int = 0
    network_isolated: bool = False


def _libc():
    try:
        return ctypes.CDLL(None, use_errno=True)
    except OSError as exc:  # pragma: no cover - libc always present on Linux
        raise LandlockUnavailable(f"cannot load libc: {exc}") from exc


def _syscall_numbers() -> tuple[int, int, int]:
    machine = platform.machine().lower()
    try:
        return _LANDLOCK_SYSCALLS[machine]
    except KeyError:
        # Unknown architecture: try the generic numbers; the syscall itself
        # reports ENOSYS if this is wrong, which detect_landlock() classifies
        # as unavailable instead of crashing.
        return (444, 445, 446)


def detect_landlock() -> LandlockStatus:
    """Probe the Landlock ABI; never raises."""
    if not sys.platform.startswith("linux"):
        return LandlockStatus(False, reason="Landlock is Linux-only")
    create_nr, _add_nr, _restrict_nr = _syscall_numbers()
    try:
        libc = _libc()
        libc.syscall.restype = ctypes.c_long
        ctypes.set_errno(0)
        result = libc.syscall(ctypes.c_long(create_nr), ctypes.c_void_p(0),
                              ctypes.c_size_t(0),
                              ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION))
    except Exception as exc:  # pragma: no cover - defensive
        return LandlockStatus(False, reason=f"landlock syscall failed: {exc}")
    if result < 0:
        errno = ctypes.get_errno()
        try:
            detail = os.strerror(errno)
        except (ValueError, OverflowError):
            detail = f"errno {errno}"
        return LandlockStatus(False, reason=f"landlock unavailable: {detail}")
    return LandlockStatus(True, abi=int(result))


def _run_probe(argv: Sequence[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            list(argv), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=_BWRAP_PROBE_TIMEOUT_S, check=False)
    except FileNotFoundError:
        return False, "not found"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"probe failed: {type(exc).__name__}"
    if completed.returncode == 0:
        return True, "ok"
    stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
    if len(stderr) > 300:
        stderr = stderr[:299] + "…"
    return False, stderr or f"exit {completed.returncode}"


def detect_bwrap(executable: str | None = None) -> BackendInfo:
    """Functionally probe bubblewrap (a ``which`` hit is not enough)."""
    path = executable or shutil.which("bwrap")
    if not path:
        return BackendInfo(BACKEND_BWRAP, False, "bwrap executable not found")
    ok, detail = _run_probe([path, "--ro-bind", "/", "/", "--", "true"])
    if not ok:
        return BackendInfo(
            BACKEND_BWRAP, False,
            f"bwrap cannot create an unprivileged sandbox: {detail}", path=path)
    net_ok, net_detail = _run_probe(
        [path, "--ro-bind", "/", "/", "--unshare-net", "--", "true"])
    if net_ok:
        return BackendInfo(BACKEND_BWRAP, True, "ok", path=path,
                           network_isolated=True)
    return BackendInfo(
        BACKEND_BWRAP, True,
        f"bwrap works but --unshare-net is unavailable: {net_detail}",
        path=path, network_isolated=False)


def detect_backends() -> dict[str, BackendInfo]:
    """Probe both backends (no caching; callers may want live results)."""
    landlock = detect_landlock()
    return {
        BACKEND_LANDLOCK: BackendInfo(
            BACKEND_LANDLOCK, landlock.available, landlock.reason or "ok",
            landlock_abi=landlock.abi,
            network_isolated=landlock.supports_network),
        BACKEND_BWRAP: detect_bwrap(),
    }


def detect_backend(prefer: str = "auto", *,
                   allow_network: bool = False) -> str:
    """Return the strongest usable backend name for the preference.

    ``auto`` prefers Landlock (unprivileged), then bubblewrap.  A preference
    for an unavailable backend degrades to ``"unsandboxed"`` rather than
    pretending.  When ``allow_network`` is False, a backend that cannot isolate
    the network is only selected if nothing better exists; callers should pass
    the returned name to :func:`build_command`, which records the trade-off in
    its notes.
    """
    preference = str(prefer or "auto").strip().lower()
    if preference in _SANDBOX_OFF:
        return BACKEND_UNSANDBOXED
    if preference not in _VALID_PREFERENCE:
        raise ValueError(f"unknown sandbox preference: {prefer!r}")
    infos = detect_backends()
    if preference == BACKEND_LANDLOCK:
        return (BACKEND_LANDLOCK if infos[BACKEND_LANDLOCK].available
                else BACKEND_UNSANDBOXED)
    if preference == BACKEND_BWRAP:
        info = infos[BACKEND_BWRAP]
        if not info.available:
            return BACKEND_UNSANDBOXED
        if not allow_network and not info.network_isolated:
            # A bwrap sandbox without ``--unshare-net`` still confines the
            # filesystem; report it as bwrap but build_command adds a note.
            return BACKEND_BWRAP
        return BACKEND_BWRAP
    # auto: Landlock first, then bwrap.  When network isolation is required
    # and Landlock is older than ABI 4, bubblewrap is the better choice.
    land = infos[BACKEND_LANDLOCK]
    bwrap = infos[BACKEND_BWRAP]
    if land.available and (allow_network or land.network_isolated):
        return BACKEND_LANDLOCK
    if bwrap.available:
        return BACKEND_BWRAP
    if land.available:
        return BACKEND_LANDLOCK
    return BACKEND_UNSANDBOXED


# ----------------------------------------------------------------------
# Landlock restriction (in-process / child pre-exec)
# ----------------------------------------------------------------------
class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


@dataclass(frozen=True)
class SandboxReport:
    backend: str
    abi: int = 0
    writable_paths: tuple[str, ...] = ()
    writable_fds: tuple[str, ...] = ()
    network_restricted: bool = False
    notes: tuple[str, ...] = ()


def _handled_write_rights(abi: int) -> int:
    rights = _FS_WRITE_BASE
    if abi >= 2:
        rights |= LANDLOCK_ACCESS_FS_REFER
    if abi >= 3:
        rights |= LANDLOCK_ACCESS_FS_TRUNCATE
    return rights


def _open_writable(path: Path) -> tuple[int, Path]:
    """Open a writable path (or its nearest existing parent) with O_PATH."""
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        candidate = candidate.resolve()
    except OSError:
        candidate = Path(os.path.abspath(str(candidate)))
    current = candidate
    while True:
        try:
            fd = os.open(str(current), os.O_PATH | os.O_CLOEXEC)
            return fd, current
        except FileNotFoundError:
            if current.parent == current:
                raise SandboxError(f"no existing parent for writable path {path}")
            current = current.parent
        except OSError as exc:
            raise SandboxError(
                f"cannot open writable path {path}: {exc.strerror or exc}") from exc


def restrict_current_process(writable_paths: Iterable[str | os.PathLike],
                             allow_network: bool = False, *,
                             landlock: LandlockStatus | None = None) -> SandboxReport:
    """Apply Landlock to the **current process** (and future children).

    Call this in a dedicated worker process before executing untrusted
    commands, or from a child ``preexec_fn`` - never in the hub process.
    Raises :class:`LandlockUnavailable` / :class:`SandboxError` on failure so
    the caller can refuse to run instead of pretending isolation.
    """
    status = landlock or detect_landlock()
    if not status.available:
        raise LandlockUnavailable(status.reason or "Landlock unavailable")
    abi = int(status.abi)
    create_nr, add_nr, restrict_nr = _syscall_numbers()
    handled_fs = _handled_write_rights(abi)
    handled_net = 0
    network_restricted = False
    notes: list[str] = []
    if not allow_network:
        if abi >= 4:
            handled_net = (LANDLOCK_ACCESS_NET_BIND_TCP
                           | LANDLOCK_ACCESS_NET_CONNECT_TCP)
            network_restricted = True
        else:
            notes.append(
                "landlock ABI < 4 cannot restrict network; TCP remains open")

    attr = _RulesetAttr(handled_access_fs=handled_fs,
                        handled_access_net=handled_net)
    size = ctypes.sizeof(_RulesetAttr) if abi >= 4 else ctypes.sizeof(
        ctypes.c_uint64)
    try:
        libc = _libc()
        libc.syscall.restype = ctypes.c_long
        ctypes.set_errno(0)
        ruleset_fd = int(libc.syscall(ctypes.c_long(create_nr),
                                      ctypes.byref(attr),
                                      ctypes.c_size_t(size),
                                      ctypes.c_uint32(0)))
        if ruleset_fd < 0:
            errno = ctypes.get_errno()
            raise SandboxError(
                f"landlock_create_ruleset failed: {os.strerror(errno)}")

        fds: list[int] = []
        opened: list[str] = []
        try:
            for raw in writable_paths or ():
                fd, opened_path = _open_writable(Path(raw))
                fds.append(fd)
                opened.append(str(opened_path))
                rule = _PathBeneathAttr(
                    allowed_access=ctypes.c_uint64(handled_fs),
                    parent_fd=ctypes.c_int32(fd))
                result = int(libc.syscall(ctypes.c_long(add_nr),
                                          ctypes.c_int(ruleset_fd),
                                          ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH),
                                          ctypes.byref(rule),
                                          ctypes.c_uint32(0)))
                if result < 0:
                    errno = ctypes.get_errno()
                    raise SandboxError(
                        f"landlock_add_rule failed for {raw}: "
                        f"{os.strerror(errno)}")
            # PR_SET_NO_NEW_PRIVS is a hard prerequisite for restrict_self.
            if libc.prctl(ctypes.c_int(PR_SET_NO_NEW_PRIVS),
                          ctypes.c_ulong(1), ctypes.c_ulong(0),
                          ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
                errno = ctypes.get_errno()
                raise SandboxError(
                    f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(errno)}")
            if int(libc.syscall(ctypes.c_long(restrict_nr),
                                ctypes.c_int(ruleset_fd),
                                ctypes.c_uint32(0))) != 0:
                errno = ctypes.get_errno()
                raise SandboxError(
                    f"landlock_restrict_self failed: {os.strerror(errno)}")
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.close(ruleset_fd)
            except OSError:
                pass
    except (SandboxError, LandlockUnavailable):
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise SandboxError(f"landlock setup failed: {exc}") from exc
    return SandboxReport(
        backend=BACKEND_LANDLOCK, abi=abi,
        writable_paths=tuple(str(item) for item in writable_paths or ()),
        writable_fds=tuple(opened), network_restricted=network_restricted,
        notes=tuple(notes))


def preexec_landlock(writable_paths: Iterable[str | os.PathLike],
                     allow_network: bool = False, *,
                     landlock: LandlockStatus | None = None) -> Callable[[], None]:
    """Return a ``subprocess.Popen(preexec_fn=...)`` callable.

    Everything needed is captured *before* the fork; the returned callable only
    issues syscalls and opens paths already validated by the caller.  A failure
    raises, aborting the child before ``exec`` (fail closed).
    """
    paths = tuple(str(item) for item in writable_paths or ())
    status = landlock or detect_landlock()

    def _apply() -> None:
        restrict_current_process(paths, allow_network, landlock=status)

    return _apply


# ----------------------------------------------------------------------
# command wrapping
# ----------------------------------------------------------------------
@dataclass
class SandboxPlan:
    """Result of :func:`wrap_command`.

    ``argv`` is what to execute, ``backend`` is the *applied* backend (never a
    backend that could not be used), ``notes`` explains every degradation and
    ``preexec_fn`` must be passed to ``subprocess`` for Landlock.
    """

    argv: list[str]
    backend: str
    notes: list[str] = field(default_factory=list)
    preexec_fn: Callable[[], None] | None = None
    cwd: str | None = None
    writable_paths: tuple[str, ...] = ()

    @property
    def sandboxed(self) -> bool:
        return self.backend != BACKEND_UNSANDBOXED


def _normalise_argv(argv: Sequence[str]) -> list[str]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise SandboxError("argv must be a non-empty sequence of strings")
    result: list[str] = []
    for item in argv:
        if not isinstance(item, str) or not item:
            raise SandboxError("argv entries must be non-empty strings")
        if "\x00" in item:
            raise SandboxError("argv entries must not contain NUL")
        result.append(item)
    return result


def _normalise_cwd(cwd: str | os.PathLike | None) -> str | None:
    if cwd is None:
        return None
    path = Path(cwd).expanduser()
    if not path.is_dir():
        raise SandboxError(f"cwd is not an existing directory: {cwd}")
    return str(path.resolve())


def _bwrap_argv(info: BackendInfo, argv: list[str], *, cwd: str | None,
                writable_paths: Sequence[str], readonly_paths: Sequence[str],
                allow_network: bool, tmp_mb: int,
                notes: list[str]) -> list[str]:
    command = [
        info.path,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--size", str(max(16, int(tmp_mb)) * 1024 * 1024),
        "--tmpfs", "/tmp",
    ]
    if cwd:
        command += ["--chdir", cwd]
    for raw in writable_paths:
        path = str(Path(raw).expanduser().resolve())
        if path == "/":
            continue
        if path == "/tmp" or path.startswith("/tmp/"):
            # ``/tmp`` is already a private tmpfs inside the namespace.
            continue
        command += ["--bind", path, path]
    for raw in readonly_paths:
        path = str(Path(raw).expanduser().resolve())
        command += ["--ro-bind", path, path]
    if not allow_network:
        if info.network_isolated:
            command.append("--unshare-net")
        else:
            notes.append(
                "bwrap --unshare-net is unavailable; network is NOT isolated")
    command += ["--"] + argv
    return command


def build_command(argv: Sequence[str], cwd: str | os.PathLike | None = None,
                  writable_paths: Iterable[str | os.PathLike] = (),
                  allow_network: bool = False, *,
                  backend: str | None = None,
                  readonly_paths: Iterable[str | os.PathLike] = (),
                  tmp_mb: int = 256) -> tuple[list[str], str, list[str]]:
    """Return ``(argv, backend, notes)`` for one command.

    ``backend`` is ``"auto"`` (default), ``"bwrap"``, ``"landlock"`` or
    ``"off"``.  The returned backend is what will actually be applied; when no
    backend is usable the argv is unchanged and the notes say why.
    """
    command = _normalise_argv(argv)
    workdir = _normalise_cwd(cwd)
    writable = [str(Path(item).expanduser()) for item in writable_paths or ()]
    readonly = [str(Path(item).expanduser()) for item in readonly_paths or ()]
    preference = str(backend or "auto").strip().lower()
    notes: list[str] = []
    if preference in _SANDBOX_OFF:
        return command, BACKEND_UNSANDBOXED, [
            "sandbox is disabled (security.sandbox=off)"]
    if preference not in _VALID_PREFERENCE:
        raise ValueError(f"unknown sandbox preference: {backend!r}")

    chosen = detect_backend(preference, allow_network=allow_network)
    if chosen == BACKEND_LANDLOCK:
        status = detect_landlock()
        if not status.available:
            return command, BACKEND_UNSANDBOXED, [
                f"landlock requested but unavailable: {status.reason}"]
        if not allow_network and status.abi < 4:
            # Prefer a real network boundary over a filesystem-only one.
            bwrap = detect_bwrap()
            if bwrap.available and bwrap.network_isolated:
                chosen = BACKEND_BWRAP
            else:
                notes.append(
                    "landlock ABI < 4 cannot restrict network; only "
                    "filesystem writes are confined")
        if chosen == BACKEND_LANDLOCK:
            notes.append(
                "landlock applies in the child; pass preexec_landlock(...) "
                "as preexec_fn (wrap_command does this)")
            notes.append(
                "read/execute remain unrestricted; writes are limited to "
                f"{len(writable)} path(s)")
            if not allow_network and status.abi >= 4:
                notes.append("TCP bind/connect denied by landlock; UDP is not covered")
            return command, BACKEND_LANDLOCK, notes

    if chosen == BACKEND_BWRAP:
        info = detect_bwrap()
        if not info.available:
            return command, BACKEND_UNSANDBOXED, [
                f"bwrap requested but unavailable: {info.detail}"]
        if not workdir and cwd is not None:  # pragma: no cover - guarded above
            workdir = _normalise_cwd(cwd)
        wrapped = _bwrap_argv(
            info, command, cwd=workdir, writable_paths=writable,
            readonly_paths=readonly, allow_network=allow_network,
            tmp_mb=tmp_mb, notes=notes)
        return wrapped, BACKEND_BWRAP, notes

    # Everything unavailable / preference off.
    infos = detect_backends()
    reasons = [f"{name}: {info.detail or 'unavailable'}"
               for name, info in infos.items() if not info.available]
    if preference != "auto":
        reasons.insert(0, f"requested backend {preference!r} is unavailable")
    notes.extend(reasons or ["no sandbox backend is available"])
    return command, BACKEND_UNSANDBOXED, notes


def wrap_command(argv: Sequence[str], cwd: str | os.PathLike | None = None,
                 writable_paths: Iterable[str | os.PathLike] = (),
                 allow_network: bool = False, *,
                 backend: str | None = None,
                 readonly_paths: Iterable[str | os.PathLike] = (),
                 tmp_mb: int = 256) -> SandboxPlan:
    """Like :func:`build_command` but returns a :class:`SandboxPlan`.

    For Landlock the plan carries the ``preexec_fn`` that must be handed to
    ``subprocess.Popen``; for bwrap the sandbox is expressed in ``argv``.
    """
    paths = tuple(str(Path(item).expanduser()) for item in writable_paths or ())
    command, applied, notes = build_command(
        argv, cwd=cwd, writable_paths=paths, allow_network=allow_network,
        backend=backend, readonly_paths=readonly_paths, tmp_mb=tmp_mb)
    preexec: Callable[[], None] | None = None
    if applied == BACKEND_LANDLOCK:
        preexec = preexec_landlock(paths, allow_network)
    return SandboxPlan(argv=command, backend=applied, notes=notes,
                       preexec_fn=preexec,
                       cwd=str(Path(cwd).expanduser().resolve()) if cwd else None,
                       writable_paths=paths)


__all__ = [
    "BACKEND_BWRAP",
    "BACKEND_LANDLOCK",
    "BACKEND_UNSANDBOXED",
    "BackendInfo",
    "LandlockStatus",
    "LandlockUnavailable",
    "SandboxError",
    "SandboxPlan",
    "SandboxReport",
    "build_command",
    "detect_backend",
    "detect_backends",
    "detect_bwrap",
    "detect_landlock",
    "preexec_landlock",
    "restrict_current_process",
    "wrap_command",
]
