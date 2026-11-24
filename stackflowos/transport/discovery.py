"""mDNS peer discovery (stdlib only, docs/team-plan.md §3 / architecture §2).

Workers normally dial the hub address they are given.  This module lets a hub
announce itself on the local link (``_stackflowos._tcp.local``) and lets a
worker discover hubs without a hand-written address::

    from stackflowos.transport.discovery import discover_hubs
    peers = discover_hubs(timeout_s=2.0)
    # [{"name": ..., "host": ..., "port": ..., "txt": {"transport_rpc": ...}}]

Design notes
------------
* **Wire format**: minimal DNS message codec (header, question, PTR/SRV/TXT/
  A/AAAA records) with name compression on decode.  No third-party mDNS
  library and no dependency on an avahi/bonjour daemon; the hub itself answers
  queries on ``224.0.0.251:5353``.
* **Advertise** sends an unsolicited response at start and every
  ``announce_interval_s``; it also answers PTR/SRV/TXT questions for our
  service type and instance name.  TXT carries the discovery contract:
  ``node_id``, ``transport_rpc``, ``http_port``, ``version``, ``projects``.
* **Browse** asks for PTR records regularly, parses every answer/announcement
  into :class:`DiscoveryState` (dedupe by instance name, expire by TTL) and
  exposes the peer list through ``/api/discovery/peers``.
* **Isolation**: the extension is a no-op unless ``cluster.discovery=mdns``.
  Socket failures (no multicast route, port taken by another daemon, sandbox)
  are logged and counted in ``health()`` -- they never stop the hub.
* **Tests** cover the codec and the :class:`DiscoveryState` state machine with
  synthetic packets; real multicast I/O is skipped by default because CI hosts
  frequently have no multicast route (flaky, environment dependent).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..util.logging import get_logger

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
DEFAULT_SERVICE_TYPE = "_stackflowos._tcp.local."
DEFAULT_PEER_TTL_S = 120.0
DEFAULT_ANNOUNCE_INTERVAL_S = 120.0
DEFAULT_QUERY_INTERVAL_S = 30.0
ENV_NODE_ID = "SFO_NODE_ID"
ENV_MDNS_ENABLE = "SFO_TEST_MDNS"

#: DNS record types we speak.
TYPE_A = 1
TYPE_PTR = 12
TYPE_TXT = 16
TYPE_AAAA = 28
TYPE_SRV = 33
_TYPE_NAMES = {TYPE_A: "A", TYPE_PTR: "PTR", TYPE_TXT: "TXT",
               TYPE_AAAA: "AAAA", TYPE_SRV: "SRV"}
_TYPE_BY_NAME = {value: key for key, value in _TYPE_NAMES.items()}
CLASS_IN = 1
CACHE_FLUSH = 0x8000
_FLAGS_QUERY = 0x0000
_FLAGS_RESPONSE = 0x8400  # QR=1, AA=1


class DnsFormatError(ValueError):
    """Raised when a packet is not a well-formed DNS/mDNS message."""


class MdnsError(RuntimeError):
    """Raised when multicast I/O cannot be set up."""


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """Canonical, comparable form: lowercase, no leading/trailing dot."""
    text = str(name or "").strip().lower()
    return text.strip(".")


def encode_name(name: str) -> bytes:
    """Encode a dotted name as DNS labels (uncompressed)."""
    clean = normalize_name(name)
    if not clean:
        return b"\x00"
    out = bytearray()
    for label in clean.split("."):
        raw = label.encode("utf-8")
        if not raw:
            raise DnsFormatError(f"empty label in DNS name {name!r}")
        if len(raw) > 63:
            raise DnsFormatError(f"DNS label too long in {name!r}")
        out.append(len(raw))
        out.extend(raw)
    out.append(0)
    if len(out) > 255:
        raise DnsFormatError(f"DNS name too long: {name!r}")
    return bytes(out)


def decode_name(packet: bytes, offset: int) -> tuple[str, int]:
    """Decode a (possibly compressed) name; return ``(name, next_offset)``."""
    labels: list[str] = []
    pos = offset
    end: int | None = None
    jumps = 0
    seen: set[int] = set()
    while True:
        if pos >= len(packet):
            raise DnsFormatError("name runs past end of packet")
        length = packet[pos]
        if length == 0:
            if end is None:
                end = pos + 1
            break
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(packet):
                raise DnsFormatError("truncated compression pointer")
            pointer = ((length & 0x3F) << 8) | packet[pos + 1]
            if end is None:
                end = pos + 2
            if pointer in seen or jumps >= 32:
                raise DnsFormatError("compression pointer loop")
            seen.add(pointer)
            jumps += 1
            pos = pointer
            continue
        if length & 0xC0:
            raise DnsFormatError(f"unsupported label type 0x{length:02x}")
        start = pos + 1
        stop = start + length
        if stop > len(packet):
            raise DnsFormatError("label runs past end of packet")
        labels.append(packet[start:stop].decode("utf-8", "replace"))
        pos = stop
    assert end is not None
    return normalize_name(".".join(labels)), end


# ---------------------------------------------------------------------------
# Records and messages
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DnsQuestion:
    name: str
    qtype: int
    qclass: int = CLASS_IN


@dataclass(frozen=True)
class DnsRecord:
    name: str
    rtype: int
    data: Any
    rclass: int = CLASS_IN
    ttl: int = 120

    @property
    def type_name(self) -> str:
        return _TYPE_NAMES.get(self.rtype, str(self.rtype))


@dataclass
class DnsMessage:
    qid: int = 0
    flags: int = _FLAGS_QUERY
    questions: list[DnsQuestion] = field(default_factory=list)
    answers: list[DnsRecord] = field(default_factory=list)
    authorities: list[DnsRecord] = field(default_factory=list)
    additionals: list[DnsRecord] = field(default_factory=list)

    @property
    def is_response(self) -> bool:
        return bool(self.flags & 0x8000)

    def all_records(self) -> list[DnsRecord]:
        return [*self.answers, *self.authorities, *self.additionals]


def ptr_record(name: str, target: str, *, ttl: int = 120) -> DnsRecord:
    return DnsRecord(name=name, rtype=TYPE_PTR, data=normalize_name(target),
                     ttl=max(0, int(ttl)))


def srv_record(name: str, target: str, port: int, *, priority: int = 0,
               weight: int = 0, ttl: int = 120) -> DnsRecord:
    return DnsRecord(
        name=name, rtype=TYPE_SRV, rclass=CLASS_IN | CACHE_FLUSH,
        data={"priority": int(priority), "weight": int(weight),
              "port": int(port), "target": normalize_name(target)},
        ttl=max(0, int(ttl)))


def txt_record(name: str, entries: Iterable[str] | dict[str, Any], *,
               ttl: int = 120) -> DnsRecord:
    if isinstance(entries, dict):
        strings = [f"{key}={value}" for key, value in entries.items()]
    else:
        strings = [str(item) for item in entries]
    return DnsRecord(name=name, rtype=TYPE_TXT,
                     rclass=CLASS_IN | CACHE_FLUSH, data=strings,
                     ttl=max(0, int(ttl)))


def a_record(name: str, address: str, *, ttl: int = 120) -> DnsRecord:
    packed = socket.inet_pton(socket.AF_INET, str(address))
    return DnsRecord(name=name, rtype=TYPE_A, rclass=CLASS_IN | CACHE_FLUSH,
                     data=socket.inet_ntop(socket.AF_INET, packed),
                     ttl=max(0, int(ttl)))


def aaaa_record(name: str, address: str, *, ttl: int = 120) -> DnsRecord:
    packed = socket.inet_pton(socket.AF_INET6, str(address))
    return DnsRecord(name=name, rtype=TYPE_AAAA,
                     rclass=CLASS_IN | CACHE_FLUSH,
                     data=socket.inet_ntop(socket.AF_INET6, packed),
                     ttl=max(0, int(ttl)))


def _encode_rdata(record: DnsRecord) -> bytes:
    if record.rtype == TYPE_PTR:
        return encode_name(str(record.data))
    if record.rtype == TYPE_SRV:
        data = record.data if isinstance(record.data, dict) else {}
        return (struct.pack("!HHH", int(data.get("priority", 0)),
                            int(data.get("weight", 0)),
                            int(data.get("port", 0)))
                + encode_name(str(data.get("target", ""))))
    if record.rtype == TYPE_TXT:
        items = record.data if isinstance(record.data, (list, tuple)) else []
        out = bytearray()
        for item in items:
            raw = str(item).encode("utf-8")[:255]
            out.append(len(raw))
            out.extend(raw)
        if not out:
            out.append(0)
        return bytes(out)
    if record.rtype == TYPE_A:
        return socket.inet_pton(socket.AF_INET, str(record.data))
    if record.rtype == TYPE_AAAA:
        return socket.inet_pton(socket.AF_INET6, str(record.data))
    if isinstance(record.data, (bytes, bytearray)):
        return bytes(record.data)
    raise DnsFormatError(f"cannot encode record type {record.rtype}")


def _encode_record(record: DnsRecord) -> bytes:
    rdata = _encode_rdata(record)
    return (encode_name(record.name)
            + struct.pack("!HHIH", int(record.rtype), int(record.rclass),
                          max(0, int(record.ttl)), len(rdata))
            + rdata)


def build_message(*, qid: int = 0, flags: int = _FLAGS_QUERY,
                  questions: Iterable[DnsQuestion] = (),
                  answers: Iterable[DnsRecord] = (),
                  authorities: Iterable[DnsRecord] = (),
                  additionals: Iterable[DnsRecord] = ()) -> bytes:
    """Encode a DNS message (no name compression: simpler and still valid)."""
    questions = list(questions)
    answers = list(answers)
    authorities = list(authorities)
    additionals = list(additionals)
    header = struct.pack("!HHHHHH", int(qid) & 0xFFFF, int(flags) & 0xFFFF,
                         len(questions), len(answers), len(authorities),
                         len(additionals))
    out = bytearray(header)
    for question in questions:
        out.extend(encode_name(question.name))
        out.extend(struct.pack("!HH", int(question.qtype), int(question.qclass)))
    for record in [*answers, *authorities, *additionals]:
        out.extend(_encode_record(record))
    return bytes(out)


def build_query(name: str, qtype: int = TYPE_PTR, *, qid: int = 0) -> bytes:
    return build_message(qid=qid, flags=_FLAGS_QUERY,
                         questions=[DnsQuestion(normalize_name(name), qtype)])


def build_response(*, answers: Iterable[DnsRecord] = (),
                   additionals: Iterable[DnsRecord] = (),
                   questions: Iterable[DnsQuestion] = ()) -> bytes:
    return build_message(qid=0, flags=_FLAGS_RESPONSE, questions=questions,
                         answers=answers, additionals=additionals)


def _parse_record(packet: bytes, offset: int) -> tuple[DnsRecord, int]:
    name, offset = decode_name(packet, offset)
    if offset + 10 > len(packet):
        raise DnsFormatError("truncated record header")
    rtype, rclass, ttl, rdlength = struct.unpack("!HHIH", packet[offset:offset + 10])
    offset += 10
    end = offset + rdlength
    if end > len(packet):
        raise DnsFormatError("record data runs past end of packet")
    if rtype == TYPE_PTR:
        target, _ = decode_name(packet, offset)
        data: Any = target
    elif rtype == TYPE_SRV:
        if rdlength < 7:
            raise DnsFormatError("truncated SRV record")
        priority, weight, port = struct.unpack("!HHH", packet[offset:offset + 6])
        target, _ = decode_name(packet, offset + 6)
        data = {"priority": priority, "weight": weight, "port": port,
                "target": target}
    elif rtype == TYPE_TXT:
        items: list[str] = []
        pos = offset
        while pos < end:
            length = packet[pos]
            pos += 1
            if pos + length > end:
                raise DnsFormatError("TXT string runs past record")
            items.append(packet[pos:pos + length].decode("utf-8", "replace"))
            pos += length
        data = items
    elif rtype == TYPE_A:
        if rdlength != 4:
            raise DnsFormatError("A record must be 4 bytes")
        data = socket.inet_ntop(socket.AF_INET, packet[offset:end])
    elif rtype == TYPE_AAAA:
        if rdlength != 16:
            raise DnsFormatError("AAAA record must be 16 bytes")
        data = socket.inet_ntop(socket.AF_INET6, packet[offset:end])
    else:
        data = bytes(packet[offset:end])
    return DnsRecord(name=name, rtype=rtype, rclass=rclass, ttl=ttl,
                     data=data), end


def parse_message(packet: bytes) -> DnsMessage:
    """Parse a DNS/mDNS packet; raises :class:`DnsFormatError` when malformed."""
    if len(packet) < 12:
        raise DnsFormatError("packet shorter than the DNS header")
    qid, flags, qdcount, ancount, nscount, arcount = struct.unpack(
        "!HHHHHH", packet[:12])
    offset = 12
    questions: list[DnsQuestion] = []
    for _ in range(qdcount):
        name, offset = decode_name(packet, offset)
        if offset + 4 > len(packet):
            raise DnsFormatError("truncated question")
        qtype, qclass = struct.unpack("!HH", packet[offset:offset + 4])
        offset += 4
        questions.append(DnsQuestion(name, qtype, qclass))
    answers: list[DnsRecord] = []
    for _ in range(ancount):
        record, offset = _parse_record(packet, offset)
        answers.append(record)
    authorities: list[DnsRecord] = []
    for _ in range(nscount):
        record, offset = _parse_record(packet, offset)
        authorities.append(record)
    additionals: list[DnsRecord] = []
    for _ in range(arcount):
        record, offset = _parse_record(packet, offset)
        additionals.append(record)
    return DnsMessage(qid=qid, flags=flags, questions=questions,
                      answers=answers, authorities=authorities,
                      additionals=additionals)


# ---------------------------------------------------------------------------
# Peer state machine (pure, unit-testable without sockets)
# ---------------------------------------------------------------------------

def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class DiscoveryState:
    """Collect mDNS answers into a peer table with TTL expiry.

    ``handle_message``/``handle_packet`` are pure with respect to time when a
    ``now`` callable is injected, which is what the tests use.  Records are
    merged across messages: PTR gives the instance name, SRV gives host/port,
    TXT gives metadata and A/AAAA give addresses for the SRV target host.
    A TTL of 0 is a multicast "goodbye" and removes the peer.
    """

    def __init__(self, service_type: str = DEFAULT_SERVICE_TYPE, *,
                 peer_ttl_s: float = DEFAULT_PEER_TTL_S,
                 now: Callable[[], float] | None = None,
                 logger: logging.Logger | None = None) -> None:
        self.service_type = normalize_name(service_type)
        self.peer_ttl_s = max(5.0, float(peer_ttl_s))
        self._now = now or time.monotonic
        self.log = logger or get_logger("transport.discovery")
        #: normalized instance name -> peer dict
        self.peers: dict[str, dict] = {}
        #: normalized host name -> {"A": [...], "AAAA": [...], "expires": ts}
        self._hosts: dict[str, dict] = {}
        self.errors = 0

    # -- ingestion --------------------------------------------------------
    def handle_packet(self, packet: bytes, addr: Any = None) -> list[dict]:
        try:
            message = parse_message(packet)
        except DnsFormatError as exc:
            self.errors += 1
            self.log.debug("ignoring malformed mDNS packet from %r: %s", addr, exc)
            return self.snapshot()
        return self.handle_message(message, addr=addr)

    def handle_message(self, message: DnsMessage, *, addr: Any = None,
                       now: float | None = None) -> list[dict]:
        current = self._now() if now is None else float(now)
        for record in message.all_records():
            try:
                self._apply(record, current, addr)
            except (KeyError, TypeError, ValueError) as exc:  # malformed record
                self.errors += 1
                self.log.debug("ignoring mDNS record %r: %s", record, exc)
        return self.snapshot(now=current)

    def _apply(self, record: DnsRecord, now: float, addr: Any) -> None:
        name = normalize_name(record.name)
        if record.rtype == TYPE_PTR:
            if name != self.service_type:
                return
            instance = normalize_name(record.data)
            if not instance:
                return
            if record.ttl <= 0:
                self.peers.pop(instance, None)
                return
            peer = self._peer(instance, now)
            peer["expires"] = now + self._bounded_ttl(record.ttl)
            return
        if record.rtype == TYPE_SRV:
            instance = name
            if not instance.endswith("." + self.service_type) and \
                    self.service_type not in instance:
                return
            data = record.data if isinstance(record.data, dict) else {}
            peer = self._peer(instance, now)
            peer["host"] = normalize_name(str(data.get("target", "")))
            peer["port"] = int(data.get("port", 0) or 0)
            peer["priority"] = int(data.get("priority", 0) or 0)
            peer["weight"] = int(data.get("weight", 0) or 0)
            peer["expires"] = now + self._bounded_ttl(record.ttl)
            self._link_addresses(peer)
            return
        if record.rtype == TYPE_TXT:
            instance = name
            if self.service_type not in instance:
                return
            entries = record.data if isinstance(record.data, list) else []
            txt: dict[str, str] = {}
            for item in entries:
                text = str(item)
                if not text:
                    continue
                key, _, value = text.partition("=")
                txt[key.strip()] = value
            peer = self._peer(instance, now)
            peer["txt"] = txt
            peer["node_id"] = txt.get("node_id") or peer.get("node_id")
            peer["transport_rpc"] = txt.get("transport_rpc", "")
            peer["version"] = txt.get("version", "")
            peer["http_port"] = _int_or_zero(txt.get("http_port"))
            peer["projects"] = _int_or_zero(txt.get("projects"))
            peer["expires"] = now + self._bounded_ttl(record.ttl)
            return
        if record.rtype in (TYPE_A, TYPE_AAAA):
            host = name
            entry = self._hosts.setdefault(
                host, {"A": [], "AAAA": [], "expires": now})
            kind = "A" if record.rtype == TYPE_A else "AAAA"
            address = str(record.data)
            if address not in entry[kind]:
                entry[kind].append(address)
            entry["expires"] = now + self._bounded_ttl(record.ttl)
            for peer in self.peers.values():
                if peer.get("host") == host:
                    self._link_addresses(peer)
            return

    def _peer(self, instance: str, now: float) -> dict:
        peer = self.peers.get(instance)
        if peer is None:
            peer = {
                "name": instance,
                "host": "",
                "port": 0,
                "addresses": [],
                "txt": {},
                "node_id": "",
                "transport_rpc": "",
                "version": "",
                "http_port": 0,
                "projects": 0,
                "priority": 0,
                "weight": 0,
                "first_seen": now,
                "expires": now + self.peer_ttl_s,
            }
            self.peers[instance] = peer
        else:
            if now - float(peer.get("first_seen", now)) > 3600:
                peer["first_seen"] = now
        return peer

    def _link_addresses(self, peer: dict) -> None:
        host = peer.get("host") or ""
        entry = self._hosts.get(host)
        addresses: list[str] = []
        if isinstance(entry, dict):
            addresses = [*entry.get("A", []), *entry.get("AAAA", [])]
        peer["addresses"] = sorted(dict.fromkeys(addresses))

    def _bounded_ttl(self, ttl: int) -> float:
        return max(5.0, min(float(ttl or self.peer_ttl_s), self.peer_ttl_s))

    # -- reading ----------------------------------------------------------
    def evict_expired(self, now: float | None = None) -> int:
        current = self._now() if now is None else float(now)
        gone = [name for name, peer in self.peers.items()
                if float(peer.get("expires", 0)) <= current]
        for name in gone:
            self.peers.pop(name, None)
        for host in [host for host, entry in self._hosts.items()
                     if float(entry.get("expires", 0)) <= current]:
            self._hosts.pop(host, None)
        return len(gone)

    def snapshot(self, now: float | None = None) -> list[dict]:
        current = self._now() if now is None else float(now)
        self.evict_expired(current)
        result: list[dict] = []
        for peer in sorted(self.peers.values(), key=lambda item: item["name"]):
            result.append({
                "name": peer["name"],
                "host": peer.get("host", ""),
                "port": int(peer.get("port", 0) or 0),
                "addresses": list(peer.get("addresses", [])),
                "txt": dict(peer.get("txt", {})),
                "node_id": peer.get("node_id", ""),
                "transport_rpc": peer.get("transport_rpc", ""),
                "version": peer.get("version", ""),
                "http_port": int(peer.get("http_port", 0) or 0),
                "projects": int(peer.get("projects", 0) or 0),
                "age_s": int(max(0.0, current - float(peer.get("first_seen", current)))),
                "expires_in_s": int(max(0.0, float(peer.get("expires", current)) - current)),
            })
        return result


# ---------------------------------------------------------------------------
# Sockets / threads
# ---------------------------------------------------------------------------

def _open_mdns_socket(bind_port: int = MDNS_PORT) -> socket.socket:
    """Bind a multicast UDP socket on the mDNS group (raise :class:`MdnsError`)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", int(bind_port)))
        group = socket.inet_aton(MDNS_GROUP)
        interface = socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        group + interface)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.settimeout(0.5)
        return sock
    except OSError as exc:
        raise MdnsError(f"cannot set up mDNS multicast socket: {exc}") from exc


def _multicast_address() -> tuple[str, int]:
    return MDNS_GROUP, MDNS_PORT


class _MdnsWorker:
    """Shared thread/socket plumbing for advertiser and browser."""

    def __init__(self, *, log: logging.Logger | None = None,
                 bind_port: int = MDNS_PORT) -> None:
        self.log = log or get_logger("transport.discovery")
        self.bind_port = int(bind_port)
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.errors = 0
        self.started = False

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self.started:
            return
        self._sock = _open_mdns_socket(self.bind_port)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"mdns-{type(self).__name__}")
        self._thread.start()
        self.started = True

    def stop(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self.started = False

    # -- hooks ------------------------------------------------------------
    def _run(self) -> None:  # pragma: no cover - real sockets
        raise NotImplementedError

    def _send(self, payload: bytes) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            sock.sendto(payload, _multicast_address())
        except OSError as exc:
            self.errors += 1
            self.log.debug("mDNS send failed: %s", exc)

    def _recv(self) -> tuple[bytes, Any] | None:
        sock = self._sock
        if sock is None:
            return None
        try:
            return sock.recvfrom(9000)
        except socket.timeout:
            return None
        except OSError as exc:
            if not self._stop.is_set():
                self.errors += 1
                self.log.debug("mDNS receive failed: %s", exc)
            return None


class MdnsAdvertiser(_MdnsWorker):
    """Answer PTR/SRV/TXT/A/AAAA queries for one hub instance."""

    def __init__(self, *, service_type: str, instance: str, host: str,
                 port: int, txt: dict[str, Any] | None = None,
                 addresses: Iterable[str] = (), ttl: int = 120,
                 announce_interval_s: float = DEFAULT_ANNOUNCE_INTERVAL_S,
                 log: logging.Logger | None = None) -> None:
        super().__init__(log=log)
        self.service_type = normalize_name(service_type)
        self.instance = normalize_name(instance)
        self.host_name = _mdns_host_name(host)
        self.port = int(port)
        self.txt = {str(key): str(value) for key, value in (txt or {}).items()}
        self.addresses = list(addresses)
        self.ttl = max(5, int(ttl))
        self.announce_interval_s = max(5.0, float(announce_interval_s))

    def update_txt(self, txt: dict[str, Any]) -> None:
        self.txt = {str(key): str(value) for key, value in (txt or {}).items()}
        if self.started:
            self.announce()

    def records(self) -> list[DnsRecord]:
        records = [
            ptr_record(self.service_type, self.instance, ttl=self.ttl),
            srv_record(self.instance, self.host_name, self.port, ttl=self.ttl),
            txt_record(self.instance, self.txt, ttl=self.ttl),
        ]
        for address in self.addresses:
            try:
                parsed = ipaddress.ip_address(str(address))
            except ValueError:
                continue
            if parsed.version == 4:
                records.append(a_record(self.host_name, str(parsed), ttl=self.ttl))
            else:
                records.append(aaaa_record(self.host_name, str(parsed), ttl=self.ttl))
        return records

    def announce(self) -> None:
        """Send an unsolicited response with all our records."""
        self._send(build_response(answers=self.records()))

    def _matches(self, message: DnsMessage) -> bool:
        for question in message.questions:
            name = normalize_name(question.name)
            if name == self.service_type:
                return True
            if name == self.instance:
                return True
            if name == self.host_name and question.qtype in (TYPE_A, TYPE_AAAA):
                return True
        return False

    def _run(self) -> None:  # pragma: no cover - real multicast
        if not self._stop.is_set():
            self.announce()
        last_announce = time.monotonic()
        while not self._stop.is_set():
            received = self._recv()
            if received is not None:
                payload, addr = received
                try:
                    message = parse_message(payload)
                except DnsFormatError:
                    self.errors += 1
                    continue
                if not message.is_response and self._matches(message):
                    # RFC 6762 recommends a short random delay; a query flood
                    # from a worker shouldn't turn into a response flood.
                    if random.random() < 0.05:
                        time.sleep(random.uniform(0.02, 0.12))
                    self._send(build_response(
                        questions=message.questions, answers=self.records()))
            now = time.monotonic()
            if now - last_announce >= self.announce_interval_s:
                self.announce()
                last_announce = now


class MdnsBrowser(_MdnsWorker):
    """Query for the service type and collect peers into :class:`DiscoveryState`."""

    def __init__(self, *, service_type: str,
                 peer_ttl_s: float = DEFAULT_PEER_TTL_S,
                 query_interval_s: float = DEFAULT_QUERY_INTERVAL_S,
                 state: DiscoveryState | None = None,
                 log: logging.Logger | None = None) -> None:
        super().__init__(log=log)
        self.service_type = normalize_name(service_type)
        self.query_interval_s = max(2.0, float(query_interval_s))
        self.state = state or DiscoveryState(self.service_type,
                                             peer_ttl_s=peer_ttl_s,
                                             logger=self.log)

    def start(self) -> None:
        super().start()
        self.query()

    def query(self) -> None:
        self._send(build_query(self.service_type, TYPE_PTR))

    def _run(self) -> None:  # pragma: no cover - real multicast
        last_query = time.monotonic()
        if not self._stop.is_set():
            self.query()
        while not self._stop.is_set():
            received = self._recv()
            if received is not None:
                payload, addr = received
                self.state.handle_packet(payload, addr)
            now = time.monotonic()
            if now - last_query >= self.query_interval_s:
                self.query()
                last_query = now

    def peers(self) -> list[dict]:
        return self.state.snapshot()


# ---------------------------------------------------------------------------
# One-shot discovery for workers / CLI
# ---------------------------------------------------------------------------

def discover_hubs(timeout_s: float = 2.0,
                  service_type: str = DEFAULT_SERVICE_TYPE, *,
                  socket_factory: Callable[[], socket.socket] | None = None,
                  peer_ttl_s: float = DEFAULT_PEER_TTL_S,
                  logger: logging.Logger | None = None) -> list[dict]:
    """Browse the local link and return hub peers found within ``timeout_s``.

    Never raises for environment problems: no multicast route, a sandbox that
    blocks UDP or any socket error returns an empty list (with a debug log) so
    a worker can fall back to an explicitly configured address.  Injectable
    ``socket_factory`` is used by tests.
    """
    log = logger or get_logger("transport.discovery")
    state = DiscoveryState(service_type, peer_ttl_s=peer_ttl_s, logger=log)
    try:
        sock = socket_factory() if socket_factory is not None else _open_mdns_socket()
    except (OSError, MdnsError) as exc:
        log.warning("hub discovery unavailable: %s", exc)
        return []
    deadline = time.monotonic() + max(0.1, float(timeout_s))
    try:
        try:
            sock.sendto(build_query(service_type, TYPE_PTR), _multicast_address())
        except OSError as exc:
            log.debug("mDNS query send failed: %s", exc)
        while time.monotonic() < deadline:
            remaining = max(0.05, min(0.5, deadline - time.monotonic()))
            try:
                sock.settimeout(remaining)
                payload, addr = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError as exc:
                log.debug("mDNS receive failed: %s", exc)
                break
            state.handle_packet(payload, addr)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return state.snapshot()


async def discover_hubs_async(timeout_s: float = 2.0,
                              service_type: str = DEFAULT_SERVICE_TYPE,
                              **kwargs: Any) -> list[dict]:
    """Async wrapper around :func:`discover_hubs` (worker startup path)."""
    return await asyncio.to_thread(discover_hubs, timeout_s, service_type, **kwargs)


# ---------------------------------------------------------------------------
# Hub extension (/api/discovery/peers + health)
# ---------------------------------------------------------------------------

def _service_port(address: str) -> int:
    """Extract the port from ``tcp://host:port``; 0 for ipc/unknown."""
    text = str(address or "")
    if not text.startswith("tcp://"):
        return 0
    try:
        return int(text.rsplit(":", 1)[1].split("/")[0])
    except (IndexError, ValueError):
        return 0


def _mdns_host_name(host: str) -> str:
    """Turn a configured host into a ``*.local`` mDNS target host name."""
    clean = normalize_name(host)
    if clean.endswith(".local"):
        return clean
    try:
        ipaddress.ip_address(clean)
    except ValueError:
        if clean:
            return f"{clean}.local"
    label = "".join(ch if ch.isalnum() or ch in "-" else "-"
                    for ch in socket.gethostname()).strip("-") or "stackflowos"
    return f"{label}.local"


def _local_addresses(host: str | None) -> list[str]:
    """Best-effort address list for the A/AAAA records we advertise."""
    addresses: list[str] = []
    if host:
        try:
            addresses.append(str(ipaddress.ip_address(host)))
        except ValueError:
            try:
                infos = socket.getaddrinfo(host, None, family=socket.AF_INET)
            except OSError:
                infos = []
            addresses.extend(sorted({info[4][0] for info in infos}))
    if not addresses:
        try:
            infos = socket.getaddrinfo(socket.gethostname(), None)
        except OSError:
            infos = []
        addresses.extend(sorted({info[4][0] for info in infos
                                 if info[0] in (socket.AF_INET, socket.AF_INET6)}))
    return [address for address in dict.fromkeys(addresses)]


def _node_id(app: Any) -> str:
    explicit = os.environ.get(ENV_NODE_ID, "").strip()
    if explicit:
        return explicit
    system = getattr(getattr(app, "config", None), "system", None)
    label = str(getattr(system, "host_label", "") or socket.gethostname())
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in label)
    return f"{safe}-{os.getpid()}"


class DiscoveryExtension:
    """Optional mDNS advertise/browse subsystem (``cluster.discovery=mdns``)."""

    name = "discovery"

    def __init__(self, app: Any) -> None:
        self.app = app
        self.log = get_logger("transport.discovery")
        cluster = getattr(getattr(app, "config", None), "cluster", None)
        self.mode = str(getattr(cluster, "discovery", "off") or "off")
        self.service_type = normalize_name(
            getattr(cluster, "service_type", DEFAULT_SERVICE_TYPE))
        self.advertise_enabled = bool(getattr(cluster, "advertise", True))
        self.peer_ttl_s = DEFAULT_PEER_TTL_S
        self._state = DiscoveryState(self.service_type,
                                     peer_ttl_s=self.peer_ttl_s,
                                     logger=self.log)
        self._instance = ""
        self._advertiser: MdnsAdvertiser | None = None
        self._browser: MdnsBrowser | None = None
        self._refresh_task: asyncio.Task | None = None
        self._errors = 0

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        self._start_sync()
        if self._advertiser is not None and self.mode == "mdns":
            self._refresh_task = asyncio.create_task(
                self._refresh_loop(), name="discovery-refresh")

    async def stop(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except (asyncio.CancelledError, Exception):
                pass
            self._refresh_task = None
        self._stop_sync()

    async def on_config(self, config: Any) -> None:
        cluster = getattr(config, "cluster", None)
        mode = str(getattr(cluster, "discovery", "off") or "off")
        service = normalize_name(getattr(cluster, "service_type",
                                         DEFAULT_SERVICE_TYPE))
        if mode == self.mode and service == self.service_type:
            return
        self.log.info("discovery config changed (%s/%s -> %s/%s); restarting",
                      self.mode, self.service_type, mode, service)
        await self.stop()
        self.mode = mode
        self.service_type = service
        self.advertise_enabled = bool(getattr(cluster, "advertise", True))
        self._instance = ""
        self._state = DiscoveryState(self.service_type,
                                     peer_ttl_s=self.peer_ttl_s,
                                     logger=self.log)
        await self.start()

    def _start_sync(self) -> None:
        if self.mode != "mdns":
            return
        cfg = getattr(self.app, "config", None)
        if cfg is not None:
            self.advertise_enabled = bool(getattr(cfg.cluster, "advertise", True))
        node = _node_id(self.app)
        sys_addresses = getattr(self.app, "sys_addresses", None)
        rpc_address = str(getattr(sys_addresses, "rpc", "") or "")
        rpc_port = _service_port(rpc_address)
        web_port = int(getattr(getattr(cfg, "web", None), "port", 0) or 0)
        host = str(getattr(getattr(cfg, "transport", None), "advertise_host", "")
                   or getattr(getattr(cfg, "system", None), "host_label", "")
                   or socket.gethostname())
        if self.advertise_enabled:
            txt = self._txt_payload(node, rpc_address, web_port)
            advertiser = MdnsAdvertiser(
                service_type=self.service_type,
                instance=f"{node}.{self.service_type}",
                host=host,
                port=rpc_port or web_port,
                txt=txt,
                addresses=_local_addresses(host),
                log=self.log,
            )
            self._instance = advertiser.instance
            try:
                advertiser.start()
                self._advertiser = advertiser
                self.log.info("mDNS advertising %s on port %s",
                              advertiser.instance, advertiser.port)
            except (OSError, MdnsError) as exc:
                self._errors += 1
                self.log.warning("mDNS advertise disabled: %s", exc)
        browser = MdnsBrowser(service_type=self.service_type, state=self._state,
                              log=self.log)
        try:
            browser.start()
            self._browser = browser
            self.log.info("mDNS browsing for %s", self.service_type)
        except (OSError, MdnsError) as exc:
            self._errors += 1
            self.log.warning("mDNS browse disabled: %s", exc)

    def _stop_sync(self) -> None:
        for worker in (self._advertiser, self._browser):
            if worker is not None:
                try:
                    worker.stop()
                except Exception:  # noqa: BLE001 - stop must never raise
                    self.log.debug("mDNS worker stop failed", exc_info=True)
        self._advertiser = None
        self._browser = None

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(60.0)
            try:
                self._refresh_once()
            except Exception:  # noqa: BLE001 - refresh must never kill the hub
                self.log.exception("discovery refresh failed")

    def _refresh_once(self) -> None:
        advertiser = self._advertiser
        if advertiser is None:
            return
        cfg = getattr(self.app, "config", None)
        sys_addresses = getattr(self.app, "sys_addresses", None)
        rpc_address = str(getattr(sys_addresses, "rpc", "") or "")
        web_port = int(getattr(getattr(cfg, "web", None), "port", 0) or 0)
        advertiser.update_txt(self._txt_payload(self._node_id(), rpc_address,
                                                web_port))

    def _node_id(self) -> str:
        return _node_id(self.app)

    def _txt_payload(self, node: str, rpc_address: str, web_port: int) -> dict:
        projects = 0
        try:
            projects = len(self.app.projects.list())
        except Exception:  # noqa: BLE001 - metadata is best effort
            projects = 0
        return {
            "node_id": node,
            "transport_rpc": rpc_address,
            "http_port": int(web_port or 0),
            "version": str(getattr(self.app, "version", "")),
            "projects": int(projects),
        }

    # -- extension surface -----------------------------------------------
    def routes(self) -> list[tuple[str, str, Any]]:
        return [("GET", r"/api/discovery/peers", self.peers_route)]

    async def peers_route(self, match: Any, query: dict, body: Any):
        peers = self.peers()
        return 200, {"ok": True, "data": {
            "service_type": self.service_type,
            "discovery": self.mode,
            "advertising": self._advertiser is not None,
            "browsing": self._browser is not None,
            "peers": peers,
        }}

    def peers(self) -> list[dict]:
        if self._browser is not None:
            peers = self._browser.peers()
        else:
            peers = self._state.snapshot()
        if self._instance:
            peers = [peer for peer in peers if peer["name"] != self._instance]
        return peers

    def health(self) -> dict:
        return {
            "peers": len(self.peers()),
            "mode": self.mode,
            "service_type": self.service_type,
            "advertising": self._advertiser is not None,
            "browsing": self._browser is not None,
            "errors": self._errors,
        }


def build(app: Any) -> DiscoveryExtension:
    """Extension entry point used by ``stackflowos.extensions``."""
    return DiscoveryExtension(app)


__all__ = [
    "CLASS_IN", "CACHE_FLUSH", "DEFAULT_PEER_TTL_S", "DEFAULT_QUERY_INTERVAL_S",
    "DEFAULT_SERVICE_TYPE", "DiscoveryExtension", "DiscoveryState",
    "DnsFormatError", "DnsMessage", "DnsQuestion", "DnsRecord", "MDNS_GROUP",
    "MDNS_PORT", "MdnsAdvertiser", "MdnsBrowser", "MdnsError",
    "TYPE_A", "TYPE_AAAA", "TYPE_PTR", "TYPE_SRV", "TYPE_TXT",
    "a_record", "aaaa_record", "build", "build_message", "build_query",
    "build_response", "decode_name", "discover_hubs", "discover_hubs_async",
    "encode_name", "normalize_name", "parse_message", "ptr_record",
    "srv_record", "txt_record",
]
