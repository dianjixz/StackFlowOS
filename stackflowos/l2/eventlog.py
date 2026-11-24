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
from typing import Any, Callable

from ..errors import BadRequest
from ..protocol import EventRecord, new_id, now_ms
from ..util.jsonio import append_jsonl, ensure_dir, iter_jsonl

_ROTATED_RE = re.compile(r"^events-(\d+)(?:-[^.]*)?\.jsonl$")


class SubscriberQueue(asyncio.Queue):
    """Queue handed out by :meth:`EventLog.subscribe`.

    ``closed`` is ``True`` when the log was already closed at subscribe time.
    Such a queue is deliberately *not* registered with the log and will never
    receive records, so callers can end their stream instead of waiting for an
    event that cannot arrive.  Queues created while the log is open leave the
    flag ``False``.
    """

    closed: bool = False


class EventLog:
    """Durable, sequenced event store with live broadcast.

    The class is deliberately independent of the kernel :class:`Context`: the
    log is infrastructure that services *use*, not a service itself. Callers
    that want kernel-level fan-out pair it with
    :func:`emit_both`, which writes here and emits on the context.
    """

    #: Event types that are broadcast but never persisted.
    EPHEMERAL_TYPES = {"chat.delta", "agent.chat.delta", "stream.chunk"}

    #: Hard upper bound for :meth:`query` (mirrors the web API limit).
    QUERY_MAX_LIMIT = 1000
    #: Upper bounds for one filtered query's backward file replay.  When an
    #: old event is not found within this budget the query returns fewer rows
    #: (or none) rather than reading unbounded history into memory.
    query_max_scan_records = 100_000
    query_max_scan_bytes = 16 * 1024 * 1024

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

    def query(self, *, project_id: str | None = None,
              types: list[str] | None = None,
              limit: int = 200) -> list[EventRecord]:
        """Return up to ``limit`` records matching the filters, oldest first.

        Unlike :meth:`tail`, this searches *all* retained history, not just the
        newest in-memory window: matching records are collected newest-first
        from the memory deque, then the event files are scanned backwards
        (newest file/line first) until enough matches are found.  The result is
        reversed to oldest-first so callers keep the console's ordering.

        ``types`` matches exact event types or dotted prefixes: ``["rule"]``
        matches ``rule`` and ``rule.created`` but not ``rulebook``.  The scan is
        bounded by :attr:`query_max_scan_records` /
        :attr:`query_max_scan_bytes`; history beyond that is not searched.
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise BadRequest("limit must be an integer") from None
        if limit < 1 or limit > self.QUERY_MAX_LIMIT:
            raise BadRequest(
                f"limit must be between 1 and {self.QUERY_MAX_LIMIT}")
        project = str(project_id) if project_id else None
        type_match = _type_matcher(types)

        def keep(record: EventRecord) -> bool:
            if project is not None and record.project_id != project:
                return False
            if type_match is not None and not type_match(record.type):
                return False
            return True

        with self._lock:
            memory = list(self._memory)
        collected: list[EventRecord] = []
        for record in reversed(memory):
            if keep(record):
                collected.append(record)
                if len(collected) >= limit:
                    break
        if len(collected) < limit:
            floor = memory[0].seq if memory else None
            self._query_files(collected, keep, limit, memory_floor=floor)
        collected.reverse()
        return collected

    def subscribe(self) -> asyncio.Queue:
        """Create a live queue receiving future :class:`EventRecord` objects.

        Safe to call from any thread.  When the log has been closed the queue
        is returned with ``closed`` set and is not registered: callers can
        detect that and stop consuming instead of waiting forever.
        """
        queue: SubscriberQueue = SubscriberQueue(
            maxsize=self.subscriber_queue_size)
        with self._lock:
            if self._closed:
                queue.closed = True
                return queue
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a queue created by :meth:`subscribe` (idempotent)."""
        with self._lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                return

    async def close(self) -> None:
        """Mark the log closed and drop live subscribers."""
        with self._lock:
            self._closed = True
            self._subscribers.clear()

    # -- internals --------------------------------------------------------
    def _broadcast(self, record: EventRecord) -> None:
        # Snapshot under the lock so concurrent subscribe/unsubscribe cannot
        # mutate the list mid-iteration; put_nowait runs outside the lock so a
        # slow consumer (or a queue callback re-entering the log) never blocks
        # writers or deadlocks on the RLock.
        with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
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

    def _query_files(self, collected: list[EventRecord],
                     keep: Callable[[EventRecord], bool],
                     limit: int, *, memory_floor: int | None) -> None:
        """Append matches found by scanning rotated files newest-first.

        Records already covered by the in-memory window (``seq >=
        memory_floor``) are skipped so callers never see duplicates.  The scan
        stops at ``limit`` matches or when the record/byte budget is spent.
        """
        budget = [self.query_max_scan_bytes]
        scanned = 0
        for path in reversed(self._files_ascending()):
            if budget[0] <= 0 or scanned >= self.query_max_scan_records:
                break
            for raw in _iter_lines_reversed(path, budget):
                line = raw.strip()
                if not line:
                    continue
                scanned += 1
                if scanned > self.query_max_scan_records:
                    return
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                record = _record_from_dict(obj)
                if record is None:
                    continue
                if memory_floor is not None and record.seq >= memory_floor:
                    continue
                if not keep(record):
                    continue
                collected.append(record)
                if len(collected) >= limit:
                    return

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


#: Chunk size for backwards line reading; bounds memory to chunk + one line.
_REVERSE_CHUNK_BYTES = 64 * 1024


def _iter_lines_reversed(path: Path, byte_budget: list[int]):
    """Yield raw lines from *path*, newest line first.

    The file is read backwards in fixed-size chunks, so neither the whole file
    nor all of its lines are materialised.  ``byte_budget`` is a one-element
    mutable list holding the remaining byte allowance; it is decremented as
    data is read, and iteration stops when it reaches zero.
    """
    if byte_budget[0] <= 0:
        return
    try:
        fh = open(path, "rb")
    except OSError:
        return
    with fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        buffered = b""
        while pos > 0 and byte_budget[0] > 0:
            read_size = min(_REVERSE_CHUNK_BYTES, pos, byte_budget[0])
            pos -= read_size
            fh.seek(pos)
            chunk = fh.read(read_size)
            byte_budget[0] -= len(chunk)
            buffered = chunk + buffered
            lines = buffered.split(b"\n")
            # lines[0] may be a partial line unless we reached the file start.
            buffered = lines[0]
            for line in reversed(lines[1:]):
                yield line
        if pos == 0 and buffered:
            # A partial first line (budget exhausted mid-file) is not a record.
            yield buffered


def _type_matcher(types: list[str] | None) -> Callable[[str], bool] | None:
    """Build a predicate for exact event types or dotted prefixes."""
    if isinstance(types, str):
        types = [types]
    wanted = [str(item).strip() for item in (types or []) if str(item).strip()]
    if not wanted:
        return None

    def matches(event_type: str) -> bool:
        return any(event_type == item or event_type.startswith(item + ".")
                   for item in wanted)

    return matches


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


__all__ = ["EventLog", "SubscriberQueue", "emit_both"]
