"""Address allocation for the L0 transport layer.

Every unit owns two sockets: an RPC endpoint (``REP`` on the unit, ``REQ`` on
the hub) and an events endpoint reserved for peer-to-peer subscriptions.  The
allocator hands out concrete URLs for both:

* ``ipc`` mode -> ``ipc://<run_dir>/<work_id>.rpc.sock`` (and ``.events.sock``)
* ``tcp`` mode -> ``tcp://<host>:<port>`` with two distinct pooled ports

The rest of StackFlowOS only ever sees the resulting :class:`UnitAddresses`;
switching ``transport.mode`` changes the URLs, not the code.

Design notes
------------
* work ids are ``<kind>.<n>`` with one global counter.  ``sys.0`` is reserved
  for the hub and consumed first, so the first worker is ``<kind>.1``.
* IPC socket paths live in the Unix ``sockaddr_un`` limit (~100 bytes).  Long
  run dirs are handled by shortening the stem and appending a SHA-1 suffix;
  collisions between sanitised ids are broken the same way.
* TCP ports are probed with a throwaway bind before being handed out, and
  released ports go back to the pool.  A port that is already in use is
  skipped, so a busy machine degrades to later ports instead of failing.
* Runtime files stay under the caller-provided ``run_dir``.  The only
  exception is :func:`socket_dir_for`, which the hub uses to move ipc sockets
  to a short temp dir when the state dir is too deep for ``sockaddr_un``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..errors import Unavailable, ValidationError
from ..util.logging import get_logger

#: Unix socket paths are stored in a fixed-size ``sockaddr_un``; stay well
#: below the usual 108-byte kernel limit so the URL prefix cannot overflow it.
MAX_IPC_PATH_BYTES = 100

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")
_KIND_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_SUFFIXES = {"rpc": ".rpc.sock", "events": ".events.sock"}


@dataclass(frozen=True)
class UnitAddresses:
    """The pair of endpoints owned by one unit.

    ``events`` is reserved for future peer-to-peer subscriptions; in this
    version a unit's own events endpoint is not bound: units publish by
    dialing the hub's events listener instead.
    """

    work_id: str
    rpc: str
    events: str


class AddressAllocator:
    """Allocate and recycle transport addresses for units of one hub.

    One allocator instance belongs to one hub process and one ``run_dir``.
    It is not thread safe; it is meant to be used from the asyncio loop.
    """

    def __init__(
        self,
        *,
        mode: str,
        host: str,
        run_dir: Path,
        port_pool: tuple[int, int] = (17820, 17899),
        advertise_host: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if mode not in ("ipc", "tcp"):
            raise ValidationError(f"transport mode must be 'ipc' or 'tcp', got {mode!r}")
        if not isinstance(host, str) or not host:
            raise ValidationError("transport host must be a non-empty string")
        start, end = int(port_pool[0]), int(port_pool[1])
        if not (1 <= start <= end <= 65535):
            raise ValidationError(f"invalid transport port pool: {port_pool!r}")

        self._mode = mode
        self._host = host
        self._run_dir = Path(run_dir).expanduser().resolve()
        self._port_pool: tuple[int, int] = (start, end)
        self._advertise_host = advertise_host
        self._log = logger or get_logger("transport.address")

        self._addresses: dict[str, UnitAddresses] = {}
        self._used_ports: dict[int, str] = {}
        self._next_port = start
        self._counter = 0  # 0 is reserved for sys.0
        self._issued: set[str] = set()

        if self._mode == "ipc":
            # The allocator owns the run directory: a listener cannot bind a
            # socket inside a directory that does not exist yet.
            self._run_dir.mkdir(parents=True, exist_ok=True)

    # -- properties -------------------------------------------------------
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def work_ids(self) -> tuple[str, ...]:
        """Work ids with a currently allocated address, in allocation order."""
        return tuple(self._addresses)

    @property
    def port_pool(self) -> tuple[int, int]:
        return self._port_pool

    # -- work ids ---------------------------------------------------------
    def ensure_sys(self) -> UnitAddresses:
        """Allocate (or return) the reserved ``sys.0`` addresses.

        Call this before allocating workers: the hub's addresses are reserved
        first, both for the id counter and for the first ports of the pool.
        Idempotent.
        """
        existing = self._addresses.get("sys.0")
        if existing is not None:
            return existing
        return self.allocate("sys.0")

    def new_work_id(self, kind: str) -> str:
        """Return a fresh ``<kind>.<n>`` id from the single global counter."""
        if not isinstance(kind, str) or not _KIND_RE.match(kind):
            raise ValidationError(
                f"invalid unit kind {kind!r}: expected [A-Za-z0-9_-]+")
        while True:
            self._counter += 1
            work_id = f"{kind}.{self._counter}"
            if work_id not in self._addresses and work_id not in self._issued:
                self._issued.add(work_id)
                return work_id

    # -- allocation -------------------------------------------------------
    def allocate(self, work_id: str) -> UnitAddresses:
        """Allocate addresses for ``work_id``.

        Idempotent: allocating the same id twice returns the existing address
        pair instead of leaking a second socket/port pair.  A released id can
        be allocated again and gets fresh addresses.
        """
        if not isinstance(work_id, str) or not work_id:
            raise ValidationError("work_id must be a non-empty string")
        existing = self._addresses.get(work_id)
        if existing is not None:
            return existing

        if self._mode == "ipc":
            addresses = self._allocate_ipc(work_id)
        else:
            addresses = self._allocate_tcp(work_id)
        self._addresses[work_id] = addresses
        self._log.debug("allocated %s -> rpc=%s events=%s",
                        work_id, addresses.rpc, addresses.events)
        return addresses

    def addresses(self, work_id: str) -> UnitAddresses | None:
        return self._addresses.get(work_id)

    def release(self, work_id: str) -> None:
        """Free a unit's sockets and ports.  Unknown ids are ignored."""
        addresses = self._addresses.pop(work_id, None)
        if addresses is None:
            return
        if self._mode == "ipc":
            for url in (addresses.rpc, addresses.events):
                self._unlink(self._path_of(url))
        else:
            for port, owner in list(self._used_ports.items()):
                if owner == work_id:
                    del self._used_ports[port]
        self._log.debug("released %s", work_id)

    def cleanup(self) -> None:
        """Drop every allocation and remove stale IPC socket files."""
        for work_id, addresses in list(self._addresses.items()):
            if self._mode == "ipc":
                for url in (addresses.rpc, addresses.events):
                    self._unlink(self._path_of(url))
            self._log.debug("cleanup: dropping %s", work_id)
        self._addresses.clear()
        self._used_ports.clear()
        self._next_port = self._port_pool[0]

    # -- ipc --------------------------------------------------------------
    def _allocate_ipc(self, work_id: str) -> UnitAddresses:
        rpc_path = self._ipc_path(work_id, "rpc")
        events_path = self._ipc_path(work_id, "events")
        # A crashed predecessor may have left socket files behind; the
        # listener cannot bind over them reliably, so remove them first.
        self._unlink(rpc_path)
        self._unlink(events_path)
        return UnitAddresses(
            work_id=work_id,
            rpc=f"ipc://{rpc_path}",
            events=f"ipc://{events_path}",
        )

    def _ipc_path(self, work_id: str, kind: str) -> Path:
        suffix = _SUFFIXES[kind]
        stem = _safe_stem(work_id)
        if stem != work_id:
            # Sanitising can collide two different ids ("a/b" vs "a_b");
            # keep the mapping unique by mixing the original id in.
            stem = f"{stem}-{_digest(work_id, 8)}"
        path = self._run_dir / f"{stem}{suffix}"
        if len(str(path).encode("utf-8")) <= MAX_IPC_PATH_BYTES:
            return path
        # Too long: shorten the stem, keep a hash that identifies the id.
        # Budget against the longest suffix so rpc and events paths both fit.
        longest_suffix = max(len(value) for value in _SUFFIXES.values())
        digest = _digest(work_id, 12)
        budget = (MAX_IPC_PATH_BYTES - len(str(self._run_dir).encode("utf-8"))
                  - 1 - len(digest) - 1 - longest_suffix)
        if budget < 1:
            raise Unavailable(
                f"run_dir is too long for an ipc socket path: {self._run_dir}")
        short = f"{stem[:budget]}-{digest}"
        return self._run_dir / f"{short}{suffix}"

    # -- tcp --------------------------------------------------------------
    def _allocate_tcp(self, work_id: str) -> UnitAddresses:
        rpc_port = self._pick_port()
        self._used_ports[rpc_port] = work_id
        try:
            events_port = self._pick_port()
        except Exception:
            del self._used_ports[rpc_port]
            raise
        self._used_ports[events_port] = work_id
        host = _url_host(self._advertise_host or self._host)
        return UnitAddresses(
            work_id=work_id,
            rpc=f"tcp://{host}:{rpc_port}",
            events=f"tcp://{host}:{events_port}",
        )

    def _pick_port(self) -> int:
        start, end = self._port_pool
        for _ in range(end - start + 1):
            port = self._next_port
            self._next_port = start if port >= end else port + 1
            if port in self._used_ports:
                continue
            if not self._port_free(port):
                self._log.debug("tcp port %d is already in use, skipping", port)
                continue
            return port
        raise Unavailable(
            f"transport tcp port pool {start}-{end} exhausted")

    def _port_free(self, port: int) -> bool:
        """Probe ``port`` by binding (then closing) a temporary socket."""
        family = socket.AF_INET6 if ":" in self._host else socket.AF_INET
        probe = socket.socket(family, socket.SOCK_STREAM)
        try:
            if family == socket.AF_INET6:
                probe.bind((self._host, port, 0, 0))
            else:
                probe.bind((self._host, port))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _path_of(url: str) -> Path:
        if url.startswith("ipc://"):
            return Path(url[len("ipc://"):])
        return Path(url)

    def _unlink(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:  # pragma: no cover - unusual fs errors
            self._log.warning("cannot remove socket file %s: %s", path, exc)


def _safe_stem(work_id: str) -> str:
    """Turn a work id into a single path component (dots kept)."""
    safe = _SAFE_ID.sub("_", work_id).strip()
    if not safe:
        safe = "unit"
    if safe.startswith("."):
        safe = f"_{safe}"
    return safe


def socket_dir_for(run_dir: Path, mode: str, log: logging.Logger | None = None) -> Path:
    """Directory for ipc sockets: ``run_dir``, or a short temp dir if it is too deep.

    ``sockaddr_un`` holds ~100 bytes, so a state dir under a deeply nested
    checkout would make every unit fail to bind.
    """
    run_dir = Path(run_dir)
    if mode != "ipc":
        return run_dir
    reserve = 40  # "coder.123.events.sock" or a hashed id
    if len(str(run_dir).encode("utf-8")) + reserve <= MAX_IPC_PATH_BYTES:
        return run_dir
    digest = _digest(str(run_dir.resolve()), 10)
    for base in (Path(tempfile.gettempdir()), Path("/tmp")):
        candidate = base / f"sfo-{digest}"
        if len(str(candidate).encode("utf-8")) + reserve <= MAX_IPC_PATH_BYTES:
            (log or _LOG).warning(
                "run dir %s is too long for unix sockets; using %s", run_dir, candidate)
            return candidate
    return run_dir


_LOG = logging.getLogger("sfo.transport.address")


def _digest(work_id: str, length: int) -> str:
    return hashlib.sha1(work_id.encode("utf-8")).hexdigest()[:length]


def _url_host(host: str) -> str:
    """Bracket bare IPv6 literals for use in a URL authority."""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host
