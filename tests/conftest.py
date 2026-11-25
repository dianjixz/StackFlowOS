"""Shared test infrastructure: scratch directories and UNIX-socket path checks.

Tests must not put scratch state under the checkout:

* ``sockaddr_un.sun_path`` is capped by the kernel (~108 bytes on Linux) and
  StackFlowOS itself caps IPC endpoints at :data:`MAX_IPC_PATH_BYTES` (100).
  A deep checkout path can therefore make ``bind()`` fail with
  ``OSError: AF_UNIX path too long``.  Scratch directories live under a short
  system temp root instead: ``SFO_TEST_TMPDIR`` or ``<system temp>/sfo-tests``.
* ``WorkspaceGit`` deliberately refuses a workspace that sits inside another
  git repository, so git tests need an isolated scratch root outside the
  checkout.

Import these helpers from tests as ``tests.conftest``::

    from tests.conftest import git_scratch_dir, require_ipc_dir

The module stays import-light (``pynng`` is optional) so pytest can load it
for any test subset, including runs that only exercise pure-Python units::

    SFO_TEST_TMPDIR=/dev/shm/sfo-tests   # relocate scratch state if /tmp is deep
    pytest --basetemp=/dev/shm/pytest    # pytest's own tmp_path root too
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

#: UNIX socket path cap.  Mirrors
#: :data:`stackflowos.transport.address.MAX_IPC_PATH_BYTES` as a literal so
#: this module imports without ``pynng`` (a test pins the two values equal);
#: keep them in sync if the transport limit ever changes.
IPC_PATH_LIMIT = 100

#: Repository root (the tests package lives directly under it).
REPO = Path(__file__).resolve().parent.parent

#: Short scratch root shared by the suite; never the checkout.
TMP_ROOT = Path(os.environ.get("SFO_TEST_TMPDIR")
                or (Path(tempfile.gettempdir()) / "sfo-tests"))

#: Worst case the allocator can emit after hashing a work id: one stem byte,
#: ``-``, a 12-char digest and the longest socket suffix (``.events.sock``).
_SHORTEST_HASHED_SOCKET_NAME = 1 + 1 + 12 + len(".events.sock")


def tmp_root() -> Path:
    """Return (and create) the shared short scratch root."""
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return TMP_ROOT


def make_temp_dir(prefix: str = "sfo-", root: Path | str | None = None) -> Path:
    """Create and return a fresh scratch directory on a short path.

    The caller owns cleanup (``shutil.rmtree(path, ignore_errors=True)`` or a
    ``tempfile.TemporaryDirectory``-style context).  ``root`` defaults to
    :func:`tmp_root`; pass an explicit root for nested fixtures.
    """
    base = Path(root) if root is not None else tmp_root()
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))


def git_scratch_dir(prefix: str = "sfo-git-") -> Path:
    """Create an isolated scratch root for throw-away git repositories.

    ``WorkspaceGit`` requires the workspace to *be* a repository root, so a
    repo created inside the (possibly git-managed) checkout is rejected by
    design.  This helper keeps that root short (for IPC sockets) and outside
    the checkout.
    """
    return make_temp_dir(prefix=prefix)


def ipc_path_bytes(path: Path | str) -> int:
    """Byte length of an IPC path as the kernel sees it (UTF-8 on disk)."""
    return len(os.fsencode(str(path)))


def ipc_path_skip_reason(path: Path | str, *,
                         label: str = "ipc socket path") -> str | None:
    """Return a skip reason when ``path`` cannot be bound as an IPC socket."""
    size = ipc_path_bytes(path)
    if size <= IPC_PATH_LIMIT:
        return None
    return (f"{label} is {size} bytes, over the {IPC_PATH_LIMIT}-byte UNIX "
            f"socket limit (sockaddr_un); set SFO_TEST_TMPDIR to a shorter "
            f"directory or pass --basetemp=<short dir>")


def ipc_dir_skip_reason(directory: Path | str, *,
                        label: str = "ipc run dir") -> str | None:
    """Return a skip reason when a run directory is too deep for IPC sockets.

    The allocator hashes long work ids but not the directory part, so even the
    shortest hashed socket name needs ``len(dir) + 1 + 26`` bytes.
    """
    size = ipc_path_bytes(directory)
    worst_case = size + 1 + _SHORTEST_HASHED_SOCKET_NAME
    if worst_case <= IPC_PATH_LIMIT:
        return None
    budget = IPC_PATH_LIMIT - 1 - _SHORTEST_HASHED_SOCKET_NAME
    return (f"{label} is {size} bytes and needs {worst_case} bytes for the "
            f"shortest hashed socket name, over the {IPC_PATH_LIMIT}-byte "
            f"UNIX socket limit (sockaddr_un); use a directory of at most "
            f"{budget} bytes (set SFO_TEST_TMPDIR or --basetemp to a shorter path)")


def require_ipc_path(testcase, path: Path | str, *,
                     label: str = "ipc socket path") -> None:
    """``testcase.skipTest`` when a concrete socket ``path`` is too long.

    Usage: ``require_ipc_path(self, socket_path)`` before ``bind()``.
    """
    reason = ipc_path_skip_reason(path, label=label)
    if reason is not None:
        testcase.skipTest(reason)


def require_ipc_dir(testcase, directory: Path | str, *,
                    label: str = "ipc run dir") -> None:
    """``testcase.skipTest`` when a run directory is too deep for IPC sockets.

    Usage: ``require_ipc_dir(self, run_dir)`` before creating an allocator.
    """
    reason = ipc_dir_skip_reason(directory, label=label)
    if reason is not None:
        testcase.skipTest(reason)
