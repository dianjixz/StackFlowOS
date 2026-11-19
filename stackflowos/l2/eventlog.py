"""Append-only, globally sequenced event log (docs/architecture.md §5.5).

The log is the durable memory of the system and the replay source for the UI
(``dmesg`` + SSE resume). Design points:

* one JSONL file ``<events_dir>/events.jsonl``; rotated to
  ``events-<last_seq>.jsonl`` once it grows past ``rotate_bytes``;
* every record carries a globally monotonic ``seq``. Ephemeral record types
  (streaming deltas) still get a ``seq`` and are broadcast live, but are not
  written to disk, so a restart resumes the sequence from the file maximum;
* the last ``memory_limit`` records are kept in memory to serve ``tail`` and
  hot ``since`` reads; older replay streams the files in rotation order;
* subscribers get an ``asyncio.Queue``; publishing never blocks: a full queue
  drops its oldest record.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from collections import deque
from pathlib import Path
from typing import Any

from ..errors import BadRequest
from ..protocol import EventRecord, new_id, now_ms
from ..util.jsonio import append_jsonl, ensure_dir, iter_jsonl

_ROTATED_RE = re.compile(r"^events-(\d+)(?:-[^.]*)?\.jsonl$")


class EventLog:
    """Durable, sequenced event store with live broadcast.

    The class is deliberately independent of the kernel :class:`Context`: the
    log is infrastructure that services *use*, not a service itself. Callers
    that want kernel-level fan-out pair it with
    :func:`emit_both`, which writes here and emits on the context.
    """

    #: Event types that are broadcast but never persisted.
    EPHEMERAL_TYPES = {"chat.delta", "stream.chunk"}

    def __init__(self, events_dir: Path, *, logger: logging.Logger | None = None,
                 memory_limit: int = 5000,
                 rotate_bytes: int = 8 * 1024 * 1024) -> None:
        self.events_dir = ensure_dir(Path(events_dir))
        self.log = logger or logging.getLogger("sfo.l2.eventlog")
        self.memory_limit = max(1, int(memory_limit))
        self.rotate_bytes = max(1, int(rotate_bytes))
        #: Max backlog per subscriber queue; older records are dropped first.
        self.subscriber_queue_size = 2000
        self.current_path = self.events_dir / "events.jsonl"
        self._lock = threading.RLock()
        self._closed = False
        self._memory: deque[EventRecord] = deque(maxlen=self.memory_limit)
        self._subscribers: list[asyncio.Queue] = []
        self._last_seq = self._recover_last_seq()
        self._load_memory()

    # -- public API -------------------------------------------------------
    @property
    def last_seq(self) -> int:
        """Highest sequence number handed out (0 for an empty log)."""
        return self._last_seq

    def append(self, type: str, data: dict, *, source: str = "sys",
               project_id: str | None = None) -> EventRecord:
        """Append a new event and broadcast it to live subscribers."""
        record = EventRecord(
            seq=0,
            type=str(type),
            source=str(source),
            ts=now_ms(),
            data=dict(data or {}),
            project_id=project_id,
            id=new_id("ev"),
        )
        return self.append_record(record)

    def append_record(self, record: EventRecord) -> EventRecord:
        """Append an externally built record, re-assigning the global ``seq``.

        ``id``/``ts`` of the incoming record are kept: they identify the event
        before the hub sequenced it (e.g. relayed from a worker).
        """
        if not isinstance(record, EventRecord):
            raise BadRequest("append_record expects an EventRecord")
        ephemeral = record.type in self.EPHEMERAL_TYPES
        with self._lock:
            self._maybe_rotate()
            seq = self._last_seq + 1
            self._last_seq = seq
            stored = EventRecord(
                seq=seq,
                type=record.type,
                source=record.source,
                ts=record.ts,
                data=dict(record.data or {}),
                project_id=record.project_id,
                id=record.id,
            )
            if not ephemeral:
                append_jsonl(self.current_path, stored.to_dict())
            self._memory.append(stored)
        self._broadcast(stored)
        return stored

    def since(self, seq: int, *, limit: int = 500,
              project_id: str | None = None) -> list[EventRecord]:
        """Return records with ``seq`` greater than the given one, oldest first."""
        seq = int(seq)
        limit = max(0, int(limit))
        if limit == 0:
            return []
        out: list[EventRecord] = []
        start = 0
        if self._memory:
            start = self._memory[0].seq
        if self._memory and seq >= start - 1:
            for record in self._memory:
                if record.seq <= seq:
                    continue
                if project_id is not None and record.project_id != project_id:
                    continue
                out.append(record)
                if len(out) >= limit:
                    break
            return out
        # Requested seq predates the in-memory window: replay files in order.
        for path in self._files_ascending():
            for obj in iter_jsonl(path):
                record = _record_from_dict(obj)
                if record is None or record.seq <= seq:
                    continue
                if project_id is not None and record.project_id != project_id:
                    continue
                out.append(record)
                if len(out) >= limit:
                    return out
        return out

    def tail(self, limit: int = 100) -> list[EventRecord]:
        """Return the most recent in-memory records, oldest first."""
        if limit <= 0:
            return []
        return list(self._memory)[-int(limit):]

    def subscribe(self) -> asyncio.Queue:
        """Create a live queue receiving future :class:`EventRecord` objects."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.subscriber_queue_size)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a queue created by :meth:`subscribe` (idempotent)."""
        try:
            self._subscribers.remove(queue)
        except ValueError:
            return

    async def close(self) -> None:
        """Mark the log closed and drop live subscribers."""
        self._closed = True
        self._subscribers.clear()

    # -- internals --------------------------------------------------------
    def _broadcast(self, record: EventRecord) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(record)
            except asyncio.QueueFull:
                # Slow consumer: drop its oldest record, then retry once. The
                # writer must never block or fail because of a subscriber.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(record)
                except asyncio.QueueFull:
                    self.log.debug("subscriber queue full; dropping %s", record.type)

    def _maybe_rotate(self) -> None:
        try:
            size = self.current_path.stat().st_size
        except FileNotFoundError:
            return
        if size <= self.rotate_bytes:
            return
        target = self.events_dir / f"events-{self._last_seq}.jsonl"
        if target.exists():
            target = self.events_dir / f"events-{self._last_seq}-{new_id()}.jsonl"
        try:
            os.replace(self.current_path, target)
        except OSError:
            self.log.exception("could not rotate event log %s", self.current_path)

    def _files_ascending(self) -> list[Path]:
        """Rotated files ordered by rotation sequence, current file last."""
        rotated: list[tuple[int, Path]] = []
        try:
            entries = list(self.events_dir.iterdir())
        except OSError:
            entries = []
        for path in entries:
            match = _ROTATED_RE.match(path.name)
            if match:
                rotated.append((int(match.group(1)), path))
        rotated.sort(key=lambda item: (item[0], item[1].name))
        files = [path for _, path in rotated]
        if self.current_path.exists():
            files.append(self.current_path)
        return files

    def _load_memory(self) -> None:
        """Warm the in-memory window from the newest files (recent history)."""
        collected: list[EventRecord] = []
        for path in reversed(self._files_ascending()):
            rows: list[EventRecord] = []
            for obj in iter_jsonl(path):
                record = _record_from_dict(obj)
                if record is not None:
                    rows.append(record)
            collected = rows + collected
            if len(collected) >= self.memory_limit:
                break
        self._memory.extend(collected[-self.memory_limit:])

    def _recover_last_seq(self) -> int:
        """Recover the highest persisted seq, tolerating a truncated last line."""
        best = 0
        try:
            entries = list(self.events_dir.iterdir())
        except OSError:
            entries = []
        for path in entries:
            match = _ROTATED_RE.match(path.name)
            if match:
                best = max(best, int(match.group(1)))
        if self.current_path.exists():
            best = max(best, _last_seq_in_file(self.current_path))
        return best


def _record_from_dict(obj: dict) -> EventRecord | None:
    """Tolerant parser for on-disk/queued rows (bad rows are skipped)."""
    try:
        return EventRecord.from_dict(obj)
    except (KeyError, TypeError, ValueError):
        return None


def _last_seq_in_file(path: Path) -> int:
    """Return the seq of the last well-formed line of a JSONL file."""
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return 0
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            try:
                return int(obj.get("seq") or 0)
            except (TypeError, ValueError):
                continue
    return 0


def emit_both(ctx: Any, eventlog: EventLog, type: str, data: dict,
              project_id: str | None = None, *, source: str = "sys") -> EventRecord:
    """Append to the event log *and* emit the record on the kernel bus.

    Every L2/security service publishes events this way so that the durable
    log and the live UI stream never diverge.
    """
    record = eventlog.append(type, data, source=source, project_id=project_id)
    ctx.emit(record.type, record.to_dict())
    return record


__all__ = ["EventLog", "emit_both"]
