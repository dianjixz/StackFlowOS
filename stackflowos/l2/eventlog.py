"""Append-only, globally sequenced event log (docs/architecture.md §5.6).

The log is the durable memory of the system and the replay source for the UI
(``dmesg`` + SSE resume). Design points:

* one JSONL file ``<events_dir>/events.jsonl``; once it grows past the
  rotation limit it is flushed, fsynced and archived:
  - **compressed mode** (``rotation_bytes=`` / ``$SFO_EVENTLOG_ROTATE_BYTES``
    / the default): ``events-<seq>-<epoch_ms>.jsonl.gz``, newest ``rotate_keep``
    archives retained;
  - **legacy mode** (an explicit ``rotate_bytes=`` from old callers):
    ``events-<seq>.jsonl``, never pruned -- this keeps existing state dirs and
    callers byte-for-byte compatible;
* every record carries a globally monotonic ``seq``. Ephemeral record types
  (streaming deltas: ``chat.delta``, ``agent.chat.delta``, any ``*.delta`` and
  ``stream.chunk``) still get a ``seq`` and are broadcast live, but are not
  written to disk, so a restart resumes the sequence from the file maximum and
  can never replay a half-streamed message.  A transient type found in
  persistence (legacy rows or rows relayed by hand) is skipped by replay
  (``since``/``query``) and counted as ``replay_skipped_transient`` in
  :meth:`EventLog.health`;
* the last ``memory_limit`` records are kept in memory to serve ``tail`` and
  hot ``since`` reads; older replay streams the archives in rotation order.
  ``query`` scans newest->oldest and stops as soon as ``limit`` matches are
  collected; corrupt archives are logged and skipped, never fatal;
* an optional :class:`~stackflowos.storage.backend.StorageBackend` (e.g.
  SQLite) can replace the JSONL files.  ``SFO_STORAGE_BACKEND=sqlite`` selects
  it without code changes; the composition root can inject one explicitly.
  With a backend, rotation/pruning is unnecessary (the database owns retention);
  run ``python -m stackflowos.storage.migrate`` *before* switching to keep the
  existing history reachable;
* subscribers get an ``asyncio.Queue``; publishing never blocks: a full queue
  drops its oldest record.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import re
import shutil
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterator

from ..errors import BadRequest
from ..protocol import EventRecord, new_id, now_ms
from ..util.jsonio import append_jsonl, ensure_dir, iter_jsonl, read_json, write_json
from ..storage import StorageBackend, backend_from_env

#: Rotated files (new compressed format and the legacy uncompressed one).
_ROTATED_RE = re.compile(r"^events-(?P<seq>\d+)(?:-[^.]*)?\.jsonl(?P<gz>\.gz)?$")

#: Suffix that marks a streaming/delta event as transient (broadcast only).
_TRANSIENT_SUFFIX = ".delta"

#: Environment overrides read by the constructor (the composition root passes
#: ``config.storage.eventlog_rotate_*`` explicitly when it wires the log).
ENV_ROTATE_BYTES = "SFO_EVENTLOG_ROTATE_BYTES"
ENV_ROTATE_KEEP = "SFO_EVENTLOG_ROTATE_KEEP"

#: Default threshold for rotation when no argument/environment is supplied.
DEFAULT_ROTATE_BYTES = 8 * 1024 * 1024
#: Default number of compressed archives retained.
DEFAULT_ROTATE_KEEP = 5


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

    @classmethod
    def is_transient(cls, event_type: Any) -> bool:
        """Whether *event_type* is broadcast-only (never durable/replayable).

        Streaming deltas are matched by the ``*.delta`` suffix on top of
        :attr:`EPHEMERAL_TYPES`, so every producer's delta variant
        (``worker.chat.delta``, ``tool.delta``, ...) is classified without
        maintaining a global list.
        """
        name = str(event_type or "")
        return name in cls.EPHEMERAL_TYPES or name.endswith(_TRANSIENT_SUFFIX)

    #: Hard upper bound for :meth:`query` (mirrors the web API limit).
    QUERY_MAX_LIMIT = 1000
    #: Upper bounds for one filtered query's backward file replay.  When an
    #: old event is not found within this budget the query returns fewer rows
    #: (or none) rather than reading unbounded history into memory.
    query_max_scan_records = 100_000
    query_max_scan_bytes = 16 * 1024 * 1024

    def __init__(self, events_dir: Path, *, logger: logging.Logger | None = None,
                 memory_limit: int = 5000,
                 rotate_bytes: int | None = None,
                 rotation_bytes: int | None = None,
                 rotate_keep: int | None = None,
                 backend: StorageBackend | None = None) -> None:
        self.events_dir = ensure_dir(Path(events_dir))
        self.log = logger or logging.getLogger("sfo.l2.eventlog")
        self.memory_limit = max(1, int(memory_limit))
        #: Max backlog per subscriber queue; older records are dropped first.
        self.subscriber_queue_size = 2000
        self.current_path = self.events_dir / "events.jsonl"
        #: Durable high-water mark so a pruned/rotated-away tail cannot make a
        #: restart re-issue sequence numbers (relevant with ``rotate_keep=0``).
        self.watermark_path = self.events_dir / ".last_seq"

        self._configure_rotation(rotate_bytes=rotate_bytes,
                                 rotation_bytes=rotation_bytes,
                                 rotate_keep=rotate_keep)

        self._backend = backend if backend is not None else backend_from_env(
            self.events_dir.parent)
        self._lock = threading.RLock()
        self._closed = False
        self._memory: deque[EventRecord] = deque(maxlen=self.memory_limit)
        self._subscribers: list[asyncio.Queue] = []
        self._archive_errors: deque[dict] = deque(maxlen=20)
        #: Transient (``*.delta``/ephemeral) rows skipped while replaying
        #: persistence; reported by :meth:`health` so operators can see that a
        #: restart dropped streaming-only history (by design).
        self._replay_skipped_transient = 0
        if self._backend is not None and self._backend.count("events") == 0:
            file_max = self._file_last_seq()
            if file_max:
                self.log.warning(
                    "eventlog backend %r is empty but JSONL history (last_seq=%d) "
                    "exists in %s; run `python -m stackflowos.storage.migrate` "
                    "to make the old events reachable",
                    getattr(self._backend, "name", "?"), file_max,
                    self.events_dir)
        self._last_seq = self._recover_last_seq()
        self._load_memory()

    # -- public API -------------------------------------------------------
    @property
    def last_seq(self) -> int:
        """Highest sequence number handed out (0 for an empty log)."""
        return self._last_seq

    @property
    def backend(self) -> StorageBackend | None:
        """The injected/selected backend (``None`` means the JSONL files)."""
        return self._backend

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
        ephemeral = self.is_transient(record.type)
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
                if self._backend is not None:
                    self._backend.append("events", stored.to_dict())
                else:
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
        seen: set[int] = set()
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
        # Requested seq predates the in-memory window: replay persistence in
        # ascending order (files oldest->newest, or the backend's row order).
        for record in self._iter_persisted_ascending():
            if record.seq <= seq or record.seq in seen:
                continue
            seen.add(record.seq)
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
              source: str | None = None,
              agent_id: str | None = None,
              since_ts: int | None = None,
              until_ts: int | None = None,
              limit: int = 200) -> list[EventRecord]:
        """Return up to ``limit`` records matching the filters, oldest first.

        Unlike :meth:`tail`, this searches *all* retained history, not just the
        newest in-memory window: matching records are collected newest-first
        from the memory deque, then persistence is scanned backwards (newest
        archive/row first) until enough matches are found.  The result is
        reversed to oldest-first so callers keep the console's ordering.

        ``types`` matches exact event types or dotted prefixes: ``["rule"]``
        matches ``rule`` and ``rule.created`` but not ``rulebook``.
        ``source`` is an exact match; ``agent_id`` matches the hub-assigned
        ``agent:<id>`` source (and the bare id / ``data.agent_id`` for older
        records), which is what makes "show me this agent's actions" an
        ordinary replay query.  ``since_ts``/``until_ts`` are inclusive
        millisecond timestamps (``record.ts``).

        The scan is bounded by :attr:`query_max_scan_records` /
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
        source_text = str(source) if source else None
        target_agent = str(agent_id) if agent_id else None
        start_ts = _optional_ms(since_ts, "since_ts")
        end_ts = _optional_ms(until_ts, "until_ts")

        def keep(record: EventRecord) -> bool:
            if project is not None and record.project_id != project:
                return False
            if type_match is not None and not type_match(record.type):
                return False
            if source_text is not None and record.source != source_text:
                return False
            if target_agent is not None:
                owned = record.source in (target_agent, f"agent:{target_agent}")
                if not owned and isinstance(record.data, dict):
                    owned = str(record.data.get("agent_id") or "") == target_agent
                if not owned:
                    return False
            if start_ts is not None and int(record.ts or 0) < start_ts:
                return False
            if end_ts is not None and int(record.ts or 0) > end_ts:
                return False
            return True

        with self._lock:
            memory = list(self._memory)
        collected: list[EventRecord] = []
        seen: set[int] = set()
        for record in reversed(memory):
            if keep(record):
                collected.append(record)
                seen.add(record.seq)
                if len(collected) >= limit:
                    break
        if len(collected) < limit:
            floor = memory[0].seq if memory else None
            if self._backend is not None:
                self._query_backend(collected, keep, limit, seen,
                                    memory_floor=floor)
            else:
                self._query_files(collected, keep, limit, memory_floor=floor,
                                  seen=seen)
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

    def health(self) -> dict:
        """Rotation/archive status for ``/api/health`` (never raises)."""
        payload: dict[str, Any] = {
            "last_seq": self._last_seq,
            "memory": len(self._memory),
            "backend": getattr(self._backend, "name", "jsonl"),
            "rotations": 0,
            "archive_bytes": 0,
            "writable": False,
            "corrupt_archives": len(self._archive_errors),
            # Transient streaming rows found in persistence are never replayed
            # (see is_transient); the counter makes that visible instead of
            # silently dropping history.
            "replay_skipped_transient": int(self._replay_skipped_transient),
        }
        if self._backend is not None:
            try:
                backend_health = self._backend.health()
            except Exception:  # noqa: BLE001 - health must never raise
                backend_health = {"writable": False}
            payload.update({
                "rotations": 0,
                "archive_bytes": 0,
                "writable": bool(backend_health.get("writable")),
                "storage": backend_health,
            })
            return payload
        archives = self._archive_files()
        payload["rotations"] = len(archives)
        payload["archive_bytes"] = sum(_safe_size(path) for path in archives)
        payload["writable"] = os.access(self.events_dir, os.W_OK)
        payload["compressed"] = self.compress_archives
        payload["rotate_bytes"] = self.rotate_bytes
        payload["rotate_keep"] = self.rotate_keep
        if self._archive_errors:
            payload["archive_errors"] = list(self._archive_errors)
        return payload

    async def close(self) -> None:
        """Mark the log closed and drop live subscribers."""
        with self._lock:
            self._closed = True
            self._subscribers.clear()
        if self._backend is not None:
            try:
                self._backend.flush()
            except Exception:  # noqa: BLE001 - close must not raise
                self.log.debug("eventlog backend flush failed", exc_info=True)

    # -- rotation configuration ------------------------------------------
    def _configure_rotation(self, *, rotate_bytes: int | None,
                            rotation_bytes: int | None,
                            rotate_keep: int | None) -> None:
        """Resolve the rotation limit/mode from args and the environment.

        Precedence: ``rotation_bytes`` argument > ``$SFO_EVENTLOG_ROTATE_BYTES``
        > explicit legacy ``rotate_bytes`` argument > built-in default.  The
        explicit legacy argument keeps the old *uncompressed*, unpruned format
        (existing callers/tests), every other path uses compressed archives.
        """
        env_limit = _env_int(ENV_ROTATE_BYTES, None, self.log)
        env_keep = _env_int(ENV_ROTATE_KEEP, None, self.log)
        if rotation_bytes is not None:
            limit, compressed = int(rotation_bytes), True
        elif env_limit is not None:
            limit, compressed = env_limit, True
        elif rotate_bytes is not None:
            limit, compressed = int(rotate_bytes), False
        else:
            limit, compressed = DEFAULT_ROTATE_BYTES, True
        self.rotate_bytes = max(1, int(limit))
        self.compress_archives = compressed
        if not compressed:
            self.rotate_keep = None  # legacy behaviour: never prune
            return
        keep = rotate_keep if rotate_keep is not None else env_keep
        self.rotate_keep = max(0, int(
            DEFAULT_ROTATE_KEEP if keep is None else keep))

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

    # -- persistence helpers ---------------------------------------------
    def _skip_replay_transient(self, record: EventRecord) -> bool:
        """Drop a transient row from replay, counting it for ``health()``.

        Transient/delta events are broadcast-only (never written by this log),
        but legacy files or rows relayed by another tool may still contain one.
        Replaying them would hand the UI a partial message, so they are skipped
        and reported explicitly rather than silently.
        """
        if not self.is_transient(record.type):
            return False
        with self._lock:
            self._replay_skipped_transient += 1
        return True

    def _iter_persisted_ascending(self) -> Iterator[EventRecord]:
        """Yield durable records oldest first (backend rows or files)."""
        if self._backend is not None:
            for row in self._backend.iter_rows("events"):
                record = _record_from_dict(row)
                if record is not None and not self._skip_replay_transient(record):
                    yield record
            return
        for path in self._files_ascending():
            for record in _iter_file_records(path, self):
                if not self._skip_replay_transient(record):
                    yield record

    def _query_backend(self, collected: list[EventRecord],
                       keep: Callable[[EventRecord], bool], limit: int,
                       seen: set[int], *, memory_floor: int | None) -> None:
        """Append matches from the backend, newest row first."""
        budget = [self.query_max_scan_bytes]
        scanned = 0
        for row in self._backend.iter_rows("events", reverse=True):  # type: ignore[union-attr]
            if budget[0] <= 0 or scanned >= self.query_max_scan_records:
                return
            scanned += 1
            record = _record_from_dict(row)
            if record is None or record.seq in seen:
                continue
            if self._skip_replay_transient(record):
                continue
            seen.add(record.seq)
            if memory_floor is not None and record.seq >= memory_floor:
                continue
            if not keep(record):
                continue
            collected.append(record)
            if len(collected) >= limit:
                return
            budget[0] -= len(json.dumps(row, ensure_ascii=False, default=str))

    def _maybe_rotate(self) -> None:
        if self._backend is not None:
            return
        try:
            size = self.current_path.stat().st_size
        except FileNotFoundError:
            return
        if size <= self.rotate_bytes:
            return
        if self.compress_archives:
            self._rotate_compressed()
        else:
            self._rotate_legacy()

    def _rotate_compressed(self) -> None:
        """Flush+fsync the active file, gzip it, then prune old archives."""
        stamp = int(time.time() * 1000)
        target = self.events_dir / f"events-{self._last_seq}-{stamp}.jsonl.gz"
        attempt = 1
        while target.exists() and attempt < 100:
            target = self.events_dir / (
                f"events-{self._last_seq}-{stamp}-{attempt}.jsonl.gz")
            attempt += 1
        tmp = target.with_name(target.name + ".tmp")
        try:
            # Flush Python buffers and force the bytes to the storage device
            # *before* the rename/compress step: the archive must never be
            # newer than what a crash-durable reader considers durable.
            with open(self.current_path, "ab") as fh:
                fh.flush()
                os.fsync(fh.fileno())
            with open(self.current_path, "rb") as source, \
                    open(tmp, "wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                                   mtime=0) as archive:
                    shutil.copyfileobj(source, archive, 1024 * 1024)
                raw.flush()
                os.fsync(raw.fileno())
            os.replace(tmp, target)
            try:
                os.unlink(self.current_path)
            except FileNotFoundError:
                pass
            self._write_watermark()
        except OSError:
            self.log.exception("could not rotate/compress event log %s",
                               self.current_path)
            try:
                tmp.unlink()
            except OSError:
                pass
            return
        self._prune_archives()
        self.log.info("rotated event log -> %s", target.name)

    def _write_watermark(self) -> None:
        """Persist the highest sequenced record (best effort, never fatal)."""
        try:
            write_json(self.watermark_path, {"seq": int(self._last_seq)})
        except OSError:
            self.log.warning("could not persist event-log watermark %s",
                             self.watermark_path, exc_info=True)

    def _read_watermark(self) -> int:
        try:
            raw = read_json(self.watermark_path, default=None)
        except ValueError:
            self.log.warning("ignoring corrupt event-log watermark %s",
                             self.watermark_path)
            return 0
        if not isinstance(raw, dict):
            return 0
        try:
            return max(0, int(raw.get("seq") or 0))
        except (TypeError, ValueError):
            return 0

    def _rotate_legacy(self) -> None:
        target = self.events_dir / f"events-{self._last_seq}.jsonl"
        if target.exists():
            target = self.events_dir / f"events-{self._last_seq}-{new_id()}.jsonl"
        try:
            os.replace(self.current_path, target)
        except OSError:
            self.log.exception("could not rotate event log %s", self.current_path)
            return
        self.log.info("rotated event log -> %s", target.name)

    def _prune_archives(self) -> None:
        keep = self.rotate_keep
        if keep is None:
            return
        archives = self._archive_files()
        for path in archives[:max(0, len(archives) - keep)]:
            try:
                path.unlink()
                self.log.info("pruned event archive %s", path.name)
            except OSError as exc:  # pragma: no cover - unusual fs errors
                self.log.warning("could not prune event archive %s: %s",
                                 path, exc)

    def _archive_files(self) -> list[Path]:
        """Every archive file, oldest rotation first."""
        rotated: list[tuple[int, str, Path]] = []
        try:
            entries = list(self.events_dir.iterdir())
        except OSError:
            entries = []
        for path in entries:
            match = _ROTATED_RE.match(path.name)
            if match:
                rotated.append((int(match.group("seq")), path.name, path))
        rotated.sort(key=lambda item: (item[0], item[1]))
        return [path for _, _, path in rotated]

    def _files_ascending(self) -> list[Path]:
        """Rotated files ordered by rotation sequence, current file last."""
        files = self._archive_files()
        if self.current_path.exists():
            files.append(self.current_path)
        return files

    def _query_files(self, collected: list[EventRecord],
                     keep: Callable[[EventRecord], bool],
                     limit: int, *, memory_floor: int | None,
                     seen: set[int]) -> None:
        """Append matches found by scanning rotated files newest-first.

        Records already covered by the in-memory window (``seq >=
        memory_floor``) or already collected are skipped so callers never see
        duplicates -- this also covers the brief window in which a rotation has
        written the archive but not yet removed the active file.  The scan
        stops at ``limit`` matches or when the record/byte budget is spent.
        """
        budget = [self.query_max_scan_bytes]
        scanned = 0
        for path in reversed(self._files_ascending()):
            if budget[0] <= 0 or scanned >= self.query_max_scan_records:
                break
            for raw in _iter_lines_reversed(path, budget, self):
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
                if record is None or record.seq in seen:
                    continue
                if self._skip_replay_transient(record):
                    continue
                seen.add(record.seq)
                if memory_floor is not None and record.seq >= memory_floor:
                    continue
                if not keep(record):
                    continue
                collected.append(record)
                if len(collected) >= limit:
                    return

    def _load_memory(self) -> None:
        """Warm the in-memory window from the newest persistence (recent history)."""
        if self._backend is not None:
            rows: list[EventRecord] = []
            for row in self._backend.iter_rows("events", reverse=True,
                                               limit=self.memory_limit):
                record = _record_from_dict(row)
                # Transient rows are never part of the durable window; this
                # warm-up is not a user-visible replay, so it is not counted.
                if record is not None and not self.is_transient(record.type):
                    rows.append(record)
            rows.reverse()
            self._memory.extend(rows[-self.memory_limit:])
            return
        collected: list[EventRecord] = []
        for path in reversed(self._files_ascending()):
            rows = [record for record in _iter_file_records(path, self)
                    if not self.is_transient(record.type)]
            collected = rows + collected
            if len(collected) >= self.memory_limit:
                break
        self._memory.extend(collected[-self.memory_limit:])

    def _file_last_seq(self) -> int:
        best = self._read_watermark()
        for path in self._archive_files():
            match = _ROTATED_RE.match(path.name)
            if match:
                best = max(best, int(match.group("seq")))
        if self.current_path.exists():
            best = max(best, _last_seq_in_file(self.current_path))
        return best

    def _recover_last_seq(self) -> int:
        """Recover the highest persisted seq, tolerating a truncated last line."""
        if self._backend is not None:
            last = self._backend.last_row("events")
            best = 0
            if isinstance(last, dict):
                try:
                    best = max(best, int(last.get("seq") or 0))
                except (TypeError, ValueError):
                    best = 0
            try:
                best = max(best, int(self._backend.get("meta", "events.last_seq", 0) or 0))
            except Exception:  # noqa: BLE001 - a broken meta value is not fatal
                pass
            return best
        return self._file_last_seq()

    def _record_archive_error(self, path: Path, error: BaseException) -> None:
        report = {"path": str(path), "error": f"{type(error).__name__}: {error}"}
        self._archive_errors.append(report)
        self.log.warning("skipping corrupt event archive %s: %s", path, error)


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _env_int(name: str, default: int | None, logger: logging.Logger) -> int | None:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning("ignoring non-integer %s=%r", name, raw)
        return default
    if value < 0:
        logger.warning("ignoring negative %s=%r", name, raw)
        return default
    return value


def _record_from_dict(obj: dict) -> EventRecord | None:
    """Tolerant parser for on-disk/queued rows (bad rows are skipped)."""
    try:
        return EventRecord.from_dict(obj)
    except (KeyError, TypeError, ValueError):
        return None


#: Chunk size for backwards line reading; bounds memory to chunk + one line.
_REVERSE_CHUNK_BYTES = 64 * 1024


def _iter_file_records(path: Path, log: EventLog) -> Iterator[EventRecord]:
    """Yield records from one JSONL file, skipping corrupt rows/archives."""
    try:
        if path.suffix == ".gz":
            opener = gzip.open
            with opener(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    record = _parse_line(line)
                    if record is not None:
                        yield record
            return
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                record = _parse_line(line)
                if record is not None:
                    yield record
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        log._record_archive_error(path, exc)


def _parse_line(line: str) -> EventRecord | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    return _record_from_dict(obj)


def _iter_lines_reversed(path: Path, byte_budget: list[int],
                         log: EventLog | None = None):
    """Yield raw lines from *path*, newest line first.

    Plain JSONL is read backwards in fixed-size chunks, so neither the whole
    file nor all of its lines are materialised.  A gzip archive cannot be
    seeked backwards portably; it is decompressed once (archives are bounded by
    the rotation threshold) and its lines are yielded in reverse.  Corrupt gzip
    archives are recorded and skipped.

    ``byte_budget`` is a one-element mutable list holding the remaining byte
    allowance; it is decremented as data is read, and iteration stops when it
    reaches zero.
    """
    if byte_budget[0] <= 0:
        return
    if path.suffix == ".gz":
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                data = fh.read()
        except (OSError, EOFError, gzip.BadGzipFile, UnicodeDecodeError) as exc:
            if log is not None:
                log._record_archive_error(path, exc)
            return
        raw_bytes = data.encode("utf-8", "replace")
        byte_budget[0] -= len(raw_bytes)
        for line in reversed(data.splitlines()):
            yield line.encode("utf-8", "replace")
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


def _optional_ms(value: Any, name: str) -> int | None:
    """Coerce an optional millisecond timestamp; reject garbage with a 400."""
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be an integer millisecond timestamp") from None
    if number < 0:
        raise BadRequest(f"{name} must be non-negative")
    return number


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
