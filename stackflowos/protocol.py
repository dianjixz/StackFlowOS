"""Wire protocol of StackFlowOS.

One envelope shape carries RPC calls, RPC replies and events. That is the
entire L0/L2 contract (the "narrow contract" from docs/vision.md):

    work_id + action + object/data

Design notes
------------
* JSON on the wire. Readability and debuggability beat bytes on a control
  plane; the data plane is not in scope yet. pynng preserves message
  boundaries, so no extra framing is needed.
* ``kind`` decides the semantics: ``rpc`` (request), ``reply`` (response to an
  rpc), ``event`` (append-only stream record). Unknown kinds are rejected by
  the decoder, not silently ignored.
* Every envelope carries ``src``/``dst`` work ids. ``dst`` may be a work id
  (``worker.coder.3``) or the reserved ``sys`` for the hub. Seeing the address
  in the message makes a TCP fleet and an IPC fleet behave identically.
* ``seq`` is per-publisher for events. The hub's event log assigns the
  authoritative global sequence; replayed consumers compare it.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any

PROTOCOL_VERSION = 1

#: Hard cap for a single envelope. Agents exchange references (paths, ids),
#: not blobs; anything above this is a bug or an attack.
MAX_MESSAGE_BYTES = 2 * 1024 * 1024

KIND_RPC = "rpc"
KIND_REPLY = "reply"
KIND_EVENT = "event"
KINDS = (KIND_RPC, KIND_REPLY, KIND_EVENT)


class ErrorCode(str, Enum):
    BAD_REQUEST = "BAD_REQUEST"
    VALIDATION = "VALIDATION"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    DENIED = "DENIED"
    UNAUTHORIZED = "UNAUTHORIZED"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    INTERNAL = "INTERNAL"


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str = "") -> str:
    """Short, readable, collision-resistant id (e.g. ``req-7f3a1c2d``)."""
    token = uuid.uuid4().hex[:10]
    return f"{prefix}-{token}" if prefix else token


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"not JSON serialisable: {type(obj)!r}")


@dataclass(slots=True)
class Envelope:
    kind: str
    action: str
    src: str
    dst: str
    id: str = field(default_factory=lambda: new_id("msg"))
    v: int = PROTOCOL_VERSION
    data: dict = field(default_factory=dict)
    object: Any = None
    ts: int = field(default_factory=now_ms)
    ok: bool = True
    error: dict | None = None
    seq: int = 0
    trace: str | None = None

    # -- construction -----------------------------------------------------
    @classmethod
    def rpc(cls, action: str, data: dict | None = None, *, src: str,
            dst: str, object: Any = None, trace: str | None = None,
            request_id: str | None = None) -> "Envelope":
        return cls(kind=KIND_RPC, action=action, src=src, dst=dst,
                   data=data or {}, object=object, trace=trace,
                   **({"id": request_id} if request_id else {}))

    @classmethod
    def event(cls, type: str, data: dict, *, source: str, project_id: str | None = None,
              seq: int = 0, event_id: str | None = None) -> "Envelope":
        payload = {"type": type, "source": source, "ts": now_ms(), "data": data}
        if project_id is not None:
            payload["project_id"] = project_id
        return cls(kind=KIND_EVENT, action=type, src=source, dst="sys",
                   id=event_id or new_id("ev"), data=payload, seq=seq, ts=payload["ts"])

    def reply(self, data: Any = None) -> "Envelope":
        return Envelope(kind=KIND_REPLY, action=self.action, src=self.dst,
                        dst=self.src, id=self.id, data=data if isinstance(data, dict) else {"value": data},
                        ok=True, ts=now_ms())

    def reply_error(self, code: str, message: str, detail: Any = None) -> "Envelope":
        err = {"code": code, "message": message}
        if detail is not None:
            err["detail"] = detail
        return Envelope(kind=KIND_REPLY, action=self.action, src=self.dst,
                        dst=self.src, id=self.id, data={}, ok=False, error=err, ts=now_ms())

    # -- wire -------------------------------------------------------------
    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def to_bytes(self) -> bytes:
        raw = dumps(self.to_dict()).encode("utf-8")
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError(f"envelope too large: {len(raw)} bytes")
        return raw

    @classmethod
    def from_bytes(cls, raw: bytes | bytearray | memoryview) -> "Envelope":
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError(f"envelope too large: {len(raw)} bytes")
        try:
            obj = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"malformed envelope: {exc}") from exc
        return cls.from_dict(obj)

    @classmethod
    def from_dict(cls, obj: dict) -> "Envelope":
        if not isinstance(obj, dict):
            raise ValueError("envelope must be a JSON object")
        kind = obj.get("kind")
        if kind not in KINDS:
            raise ValueError(f"unknown envelope kind: {kind!r}")
        action = obj.get("action")
        src = obj.get("src")
        dst = obj.get("dst")
        if not isinstance(action, str) or not action:
            raise ValueError("envelope.action must be a non-empty string")
        if not isinstance(src, str) or not isinstance(dst, str):
            raise ValueError("envelope.src/dst must be strings")
        return cls(
            kind=kind,
            action=action,
            src=src,
            dst=dst,
            id=str(obj.get("id") or new_id("msg")),
            v=int(obj.get("v") or PROTOCOL_VERSION),
            data=obj.get("data") if isinstance(obj.get("data"), dict) else {},
            object=obj.get("object"),
            ts=int(obj.get("ts") or now_ms()),
            ok=bool(obj.get("ok", True)),
            error=obj.get("error") if isinstance(obj.get("error"), dict) else None,
            seq=int(obj.get("seq") or 0),
            trace=obj.get("trace"),
        )


@dataclass(slots=True)
class EventRecord:
    """An event as stored by the hub event log and streamed to the UI."""

    seq: int
    type: str
    source: str
    ts: int
    data: dict
    project_id: str | None = None
    id: str = field(default_factory=lambda: new_id("ev"))

    def to_dict(self) -> dict:
        out = {
            "seq": self.seq,
            "id": self.id,
            "type": self.type,
            "source": self.source,
            "ts": self.ts,
            "data": self.data,
        }
        if self.project_id is not None:
            out["project_id"] = self.project_id
        return out

    @classmethod
    def from_dict(cls, obj: dict) -> "EventRecord":
        return cls(
            seq=int(obj.get("seq") or 0),
            type=str(obj["type"]),
            source=str(obj.get("source") or "sys"),
            ts=int(obj.get("ts") or now_ms()),
            data=obj.get("data") if isinstance(obj.get("data"), dict) else {},
            project_id=obj.get("project_id"),
            id=str(obj.get("id") or new_id("ev")),
        )

    @classmethod
    def from_envelope(cls, env: Envelope, seq: int = 0) -> "EventRecord":
        payload = env.data if isinstance(env.data, dict) else {}
        return cls(
            seq=seq,
            type=str(payload.get("type") or env.action),
            source=str(payload.get("source") or env.src),
            ts=int(payload.get("ts") or env.ts),
            data=payload.get("data") if isinstance(payload.get("data"), dict) else {},
            project_id=payload.get("project_id"),
            id=env.id,
        )

    def to_envelope(self) -> Envelope:
        payload = {"type": self.type, "source": self.source, "ts": self.ts, "data": self.data}
        if self.project_id is not None:
            payload["project_id"] = self.project_id
        return Envelope(kind=KIND_EVENT, action=self.type, src=self.source,
                        dst="sys", id=self.id, data=payload, seq=self.seq, ts=self.ts)
