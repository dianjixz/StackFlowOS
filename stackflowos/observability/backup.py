"""Backup, restore and event export for a StackFlowOS state directory.

Layout of a backup (``sfo-backup-YYYYmmdd-HHMMSS-<rand>.tar.gz``)::

    manifest.json            # format/version, time, per-file sha256/size
    config.json              # runtime config overrides (when present)
    state/...                # projects, counters, rules, approvals, memory...
    projects/<id>/workspace/ # agent artifacts
    events/...               # append-only event log + rotations

Deliberately **excluded** (documented in docs/team/ops.md):

* ``run/``   -- sockets/locks are process identities, not data;
* ``tmp/``   -- scratch files;
* ``logs/``  -- can be huge and are not authoritative (the event log is);
* ``backups/`` -- otherwise every backup would contain all previous backups.

Symlinks and special files are skipped (never followed), so a backup cannot
escape the state directory.  Restore validates the manifest, refuses archives
whose members escape the target, requires ``force=True`` when the target is
not empty, and backs up the current state before replacing it.

All functions are synchronous and side-effect free until they write their
output file; they are safe to call from ``cli.py`` as well as from the
``backup`` extension.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import stat
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from ..errors import Conflict, ValidationError
from ..util.jsonio import iter_jsonl
from ..util.logging import get_logger
from . import (BACKUP_FORMAT, BACKUP_VERSION, app_version, backup_keep_of,
               events_dir_of, now_ms, safe_int, state_dir_of)

_ARCHIVE_PREFIX = "sfo-backup-"
_ARCHIVE_SUFFIX = ".tar.gz"
_ROTATED_RE = re.compile(r"^events-(\d+)(?:-[^.]*)?\.jsonl$")
_INCLUDE_ENTRIES = ("config.json", "state", "projects", "events")
_EXCLUDED = ("run", "tmp", "logs", "backups")
_MANIFEST = "manifest.json"
_CHUNK = 1024 * 1024
#: Defensive caps so a hostile archive cannot exhaust memory/disk quickly.
_MAX_MEMBERS = 200_000
_MAX_TOTAL_BYTES = 50 * 1024 ** 3
_MAX_FILE_BYTES = 10 * 1024 ** 3
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BackupError(ValidationError, ValueError):
    """Invalid archive/export request (also a plain ``ValueError`` for CLI callers)."""


class BackupConflict(Conflict, ValueError):
    """Non-empty restore target without ``force`` (Conflict + ValueError)."""


_LOG = get_logger("observability.backup")


# -- helpers -------------------------------------------------------------
def _iter_backup_files(state: Path,
                       skipped: list[str]) -> Iterator[tuple[Path, str]]:
    """Yield ``(absolute_path, arcname)`` for regular files.

    Symlinks/special files are appended to *skipped* (for the manifest) and
    never followed, so a backup cannot escape the state directory.
    """
    for top in _INCLUDE_ENTRIES:
        base = state / top
        try:
            if base.is_symlink():
                skipped.append(f"{top} (symlink)")
                continue
            if base.is_file():
                if stat.S_ISREG(base.stat().st_mode):
                    yield base, top
                continue
            if not base.is_dir():
                continue
        except OSError:
            skipped.append(f"{top} (unreadable)")
            continue
        for root, dirnames, filenames in os.walk(base, followlinks=False):
            root_path = Path(root)
            keep: list[str] = []
            for dirname in dirnames:
                child = root_path / dirname
                if child.is_symlink():
                    skipped.append(f"{child.relative_to(state).as_posix()} (symlink)")
                else:
                    keep.append(dirname)
            dirnames[:] = sorted(keep)
            for filename in sorted(filenames):
                path = root_path / filename
                try:
                    info = path.lstat()
                except OSError:
                    skipped.append(f"{path.relative_to(state).as_posix()} (unreadable)")
                    continue
                if stat.S_ISLNK(info.st_mode):
                    skipped.append(f"{path.relative_to(state).as_posix()} (symlink)")
                    continue
                if not stat.S_ISREG(info.st_mode):
                    skipped.append(f"{path.relative_to(state).as_posix()} (special)")
                    continue
                yield path, path.relative_to(state).as_posix()


class _HashingReader:
    """File wrapper that hashes exactly the bytes tarfile stores."""

    def __init__(self, handle: Any) -> None:
        self._handle = handle
        self.digest = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        if chunk:
            self.digest.update(chunk)
            self.size += len(chunk)
        return chunk


def _prune_backups(dest: Path, keep: int) -> list[Path]:
    """Delete all but the newest *keep* backups (``keep <= 0`` keeps all)."""
    if keep <= 0:
        return []
    try:
        candidates = sorted(
            (path for path in dest.glob(_ARCHIVE_PREFIX + "*" + _ARCHIVE_SUFFIX)
             if path.is_file()),
            key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    except OSError:
        return []
    removed: list[Path] = []
    for path in candidates[int(keep):]:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            _LOG.warning("could not prune old backup %s", path)
    return removed


def _archive_name() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"{_ARCHIVE_PREFIX}{stamp}-{os.urandom(4).hex()}{_ARCHIVE_SUFFIX}"


# -- backup --------------------------------------------------------------
def create_backup(paths: Any, dest: str | Path | None = None,
                  keep: int | None = None) -> Path:
    """Create a ``tar.gz`` backup and return its path.

    ``paths`` accepts an ``App``, a ``config.Paths`` object or a state-dir
    path.  ``dest`` defaults to ``<state>/backups``.  When ``keep`` is omitted
    it comes from ``observability.backup_keep`` (default 7); ``keep <= 0``
    disables pruning.
    """
    state = state_dir_of(paths)
    if not state.exists() or not state.is_dir():
        raise BackupError(f"state directory does not exist: {state}")
    if keep is None:
        keep = backup_keep_of(paths)
    keep = max(0, safe_int(keep, 0))
    destination = Path(dest).expanduser() if dest is not None else state / "backups"
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)

    target = destination / _archive_name()
    # Archives include config.json/state (which may carry credentials): never
    # create them world-readable, not even while they are being written.
    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(destination))
    os.close(temp_fd)
    temp_path = Path(temp_name)
    manifest: dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "app_version": app_version(),
        "created_at": now_ms(),
        "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_state_dir": state.name,
        "includes": list(_INCLUDE_ENTRIES),
        "excluded": list(_EXCLUDED),
        "files": {},
        "skipped": [],
        "counts": {"files": 0, "bytes": 0},
    }
    total = 0
    skipped: list[str] = []
    try:
        with tarfile.open(temp_path, "w:gz") as archive:
            for path, arcname in _iter_backup_files(state, skipped):
                try:
                    info = archive.gettarinfo(str(path), arcname=arcname)
                except (OSError, ValueError):
                    _LOG.warning("skipping unreadable file during backup: %s", path)
                    continue
                if not info.isreg():  # defensive: never archive links/devices
                    continue
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = (info.mode & 0o777) or 0o600
                try:
                    handle = open(path, "rb")
                except OSError:
                    _LOG.warning("skipping unreadable file during backup: %s", path)
                    continue
                with handle:
                    reader = _HashingReader(handle)
                    # Do not swallow read errors mid-member: a partially
                    # written tar member would make the archive inconsistent.
                    archive.addfile(info, reader)
                manifest["files"][arcname] = {
                    "size": reader.size,
                    "sha256": reader.digest.hexdigest(),
                    "mtime": int(info.mtime),
                }
                total += reader.size
            for item in skipped:
                if item not in manifest["skipped"] and len(manifest["skipped"]) < 200:
                    manifest["skipped"].append(item)
            manifest["counts"] = {
                "files": len(manifest["files"]),
                "bytes": total,
            }
            payload = json.dumps(manifest, ensure_ascii=False,
                                 sort_keys=True).encode("utf-8")
            info = tarfile.TarInfo(_MANIFEST)
            info.size = len(payload)
            info.mode = 0o600
            info.mtime = int(time.time())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(payload))
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
    removed = _prune_backups(destination, keep)
    _LOG.info("backup created: %s (%d files, %d bytes, pruned=%d)",
              target, manifest["counts"]["files"], manifest["counts"]["bytes"],
              len(removed))
    return target


# -- restore -------------------------------------------------------------
def _safe_member_name(raw: str) -> str:
    """Validate one tar member name; return a normalised POSIX relative path."""
    name = str(raw or "")
    if not name or "\x00" in name:
        raise BackupError("archive contains an empty/invalid member name")
    if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        raise BackupError(f"archive member is absolute: {name!r}")
    normalised = name.replace("\\", "/")
    parts = [part for part in normalised.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise BackupError(f"archive member escapes the target: {name!r}")
    cleaned = posixpath.normpath("/".join(parts))
    if cleaned in ("", ".", "..") or cleaned.startswith("../"):
        raise BackupError(f"archive member escapes the target: {name!r}")
    return cleaned


def _member_path(staging: Path, name: str) -> Path:
    target = staging / name
    resolved = target.resolve()
    try:
        resolved.relative_to(staging.resolve())
    except ValueError:
        raise BackupError(
            f"archive member escapes the target: {name!r}") from None
    return target


def _open_archive(path: Path) -> tarfile.TarFile:
    """Open a backup archive, turning tar/OS errors into ValidationError."""
    try:
        return tarfile.open(path, "r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError(
            f"archive is not a valid .tar.gz backup: {type(exc).__name__}") from None


_READ_ERRORS = (tarfile.TarError, OSError, EOFError)


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    try:
        return archive.getmembers()
    except _READ_ERRORS as exc:
        raise BackupError(
            f"cannot read archive members: {type(exc).__name__}") from None


def _read_manifest(archive: tarfile.TarFile,
                   members: list[tarfile.TarInfo]) -> tuple[dict, tarfile.TarInfo]:
    manifest_member = None
    for member in members:
        if _safe_member_name(member.name) == _MANIFEST:
            if manifest_member is not None:
                raise BackupError("archive contains more than one manifest")
            manifest_member = member
    if manifest_member is None:
        raise BackupError("archive has no manifest.json")
    if not manifest_member.isreg():
        raise BackupError("archive manifest is not a regular file")
    handle = archive.extractfile(manifest_member)
    if handle is None:
        raise BackupError("cannot read archive manifest")
    try:
        raw = handle.read()
    except _READ_ERRORS as exc:
        raise BackupError(
            f"cannot read archive manifest: {type(exc).__name__}") from None
    if len(raw) > 16 * 1024 * 1024:
        raise BackupError("archive manifest is unreasonably large")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BackupError("archive manifest is not valid JSON") from None
    if not isinstance(manifest, dict):
        raise BackupError("archive manifest must be an object")
    if manifest.get("format") != BACKUP_FORMAT:
        raise BackupError(f"unexpected backup format: {manifest.get('format')!r}")
    if safe_int(manifest.get("version")) != BACKUP_VERSION:
        raise BackupError(
            f"unsupported backup version: {manifest.get('version')!r}")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise BackupError("archive manifest has no file table")
    return manifest, manifest_member


def restore(archive: str | Path, state_dir: str | Path, force: bool = False, *,
            backup_current: bool = True) -> dict:
    """Validate *archive* and restore it into *state_dir*.

    Refuses path traversal, absolute members, links/device files and manifest
    mismatches.  A non-empty target requires ``force=True``; in that case the
    current state is backed up under ``<state>/backups`` before being replaced.
    """
    archive_path = Path(archive).expanduser()
    state = Path(state_dir).expanduser().resolve()
    if not archive_path.exists() or not archive_path.is_file():
        raise BackupError(f"backup archive does not exist: {archive_path}")
    if state.exists() and state.is_symlink():
        raise BackupError(f"state directory must not be a symlink: {state}")
    state.parent.mkdir(parents=True, exist_ok=True)

    backup_path: Path | None = None
    auto_backup_dir: Path | None = None

    staging = Path(tempfile.mkdtemp(prefix=f".{state.name}.restore-",
                                    dir=str(state.parent)))
    try:
        with _open_archive(archive_path) as archive:
            members = _safe_members(archive)
            if len(members) > _MAX_MEMBERS:
                raise BackupError(
                    f"archive has too many members ({len(members)})")
            manifest, _manifest_member = _read_manifest(archive, members)
            declared = manifest["files"]
            declared_names: dict[str, dict] = {}
            declared_total = 0
            for name, meta in declared.items():
                normalised = _safe_member_name(name)
                if normalised == _MANIFEST:
                    raise BackupError("manifest cannot list itself")
                if not isinstance(meta, dict):
                    raise BackupError(f"bad manifest entry for {normalised!r}")
                size = safe_int(meta.get("size"), -1)
                digest = str(meta.get("sha256") or "")
                if size < 0 or size > _MAX_FILE_BYTES:
                    raise BackupError(f"bad size for {normalised!r}")
                if not _SHA256_RE.fullmatch(digest):
                    raise BackupError(f"bad sha256 for {normalised!r}")
                declared_total += size
                if declared_total > _MAX_TOTAL_BYTES:
                    raise BackupError("backup exceeds the size limit")
                declared_names[normalised] = {"size": size, "sha256": digest}

            regular: dict[str, tarfile.TarInfo] = {}
            for member in members:
                name = _safe_member_name(member.name)
                if member.issym() or member.islnk():
                    raise BackupError(
                        f"archive contains a link member: {member.name!r}")
                if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                    raise BackupError(
                        f"archive contains a special member: {member.name!r}")
                if member.isdir():
                    _member_path(staging, name)
                    continue
                if not member.isreg():
                    raise BackupError(
                        f"archive contains unsupported member: {member.name!r}")
                if name == _MANIFEST:
                    continue
                if name in regular:
                    raise BackupError(
                        f"archive contains duplicate member: {member.name!r}")
                regular[name] = member
            if set(regular) != set(declared_names):
                extra = sorted(set(regular) - set(declared_names))[:5]
                missing = sorted(set(declared_names) - set(regular))[:5]
                raise BackupError(
                    "archive contents do not match its manifest",
                    detail={"extra": extra, "missing": missing})

            # Only after the archive itself validated do we touch the target:
            # a corrupt archive must not create a backup or modify anything.
            if state.exists():
                try:
                    existing = list(state.iterdir())
                except OSError as exc:
                    raise BackupError(
                        f"cannot read state directory: {exc}") from None
                if existing:
                    if not force:
                        raise BackupConflict(
                            f"state directory is not empty: {state}; "
                            "pass force=True")
                    if backup_current:
                        # The state tree is about to be replaced, so the safety
                        # copy of the *current* state must live beside it until
                        # the swap is done; it is moved into the restored
                        # state's backups/ later.
                        auto_backup_dir = (state.parent /
                                           f".{state.name}.restore-backups")
                        backup_path = create_backup(state, dest=auto_backup_dir)

            restored_files = 0
            restored_bytes = 0
            for name in sorted(regular):
                member = regular[name]
                expected = declared_names[name]
                target = _member_path(staging, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise BackupError(f"cannot extract {name!r}")
                digest = hashlib.sha256()
                size = 0
                try:
                    with open(target, "wb") as output:
                        while True:
                            try:
                                chunk = source.read(_CHUNK)
                            except _READ_ERRORS as exc:
                                raise BackupError(
                                    f"cannot read member {name!r}: "
                                    f"{type(exc).__name__}") from None
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > expected["size"] or size > _MAX_FILE_BYTES:
                                raise BackupError(
                                    f"member exceeds declared size: {name!r}")
                            digest.update(chunk)
                            output.write(chunk)
                except OSError as exc:
                    raise BackupError(f"cannot write {name!r}: {exc}") from None
                if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
                    raise BackupError(
                        f"checksum mismatch for {name!r}; archive is corrupt")
                try:
                    os.chmod(target, (member.mode & 0o777) or 0o600)
                    os.utime(target, (member.mtime, member.mtime))
                except OSError:
                    pass
                restored_files += 1
                restored_bytes += size

        # Swap the fully validated tree into place.  Keep the old tree until
        # the rename succeeds so a failure cannot leave the operator with
        # neither the old nor the new state.
        # If the archive itself lives under the state directory (the default
        # ``backups/`` location), keep it across the swap with a hardlink into
        # the staged tree; otherwise restoring would delete the only copy.
        try:
            inside_state = archive_path.resolve().is_relative_to(state)
        except (OSError, ValueError):
            inside_state = False
        if inside_state:
            try:
                keep_dir = staging / "backups"
                keep_dir.mkdir(parents=True, exist_ok=True)
                keep_path = keep_dir / archive_path.name
                if not keep_path.exists():
                    try:
                        os.link(archive_path, keep_path)
                    except OSError:
                        shutil.copy2(archive_path, keep_path)
            except OSError:
                _LOG.warning("could not preserve archive inside restored state: %s",
                             archive_path)
        previous: Path | None = None
        if state.exists():
            previous = state.with_name(
                f".{state.name}.pre-restore-{now_ms()}-{os.getpid()}")
            os.replace(state, previous)
        try:
            os.replace(staging, state)
        except Exception:
            if previous is not None and not state.exists():
                os.replace(previous, state)
            raise
        if previous is not None:
            shutil.rmtree(previous, ignore_errors=True)
        if backup_path is not None and backup_path.exists():
            backups_dir = state / "backups"
            try:
                backups_dir.mkdir(parents=True, exist_ok=True)
                moved = backups_dir / backup_path.name
                shutil.move(str(backup_path), str(moved))
            except OSError:
                _LOG.warning("pre-restore backup kept outside the state dir: %s",
                             backup_path)
            else:
                backup_path = moved
                if auto_backup_dir is not None:
                    try:
                        auto_backup_dir.rmdir()
                    except OSError:
                        pass
        result = {
            "state_dir": str(state),
            "archive": str(archive_path),
            "files": restored_files,
            "bytes": restored_bytes,
            "replaced": previous is not None,
            "backup": str(backup_path) if backup_path is not None else None,
            "manifest": {
                "version": BACKUP_VERSION,
                "created_at": manifest.get("created_at"),
                "app_version": manifest.get("app_version"),
            },
        }
        _LOG.info("restored %s into %s (%d files)", archive_path, state,
                  restored_files)
        return result
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


# -- event export --------------------------------------------------------
def _event_files(events_dir: Path) -> list[Path]:
    rotated: list[tuple[int, Path]] = []
    try:
        entries = list(events_dir.iterdir())
    except OSError:
        return []
    for path in entries:
        match = _ROTATED_RE.match(path.name)
        if match and path.is_file():
            rotated.append((int(match.group(1)), path))
    rotated.sort(key=lambda item: (item[0], item[1].name))
    files = [path for _, path in rotated]
    current = events_dir / "events.jsonl"
    if current.exists() and current.is_file():
        files.append(current)
    return files


def _event_types(types: Any) -> tuple[str, ...]:
    if types is None:
        return ()
    if isinstance(types, str):
        types = [item.strip() for item in types.split(",")]
    if not isinstance(types, (list, tuple, set, frozenset)):
        return ()
    return tuple(str(item).strip() for item in types if str(item).strip())


def _type_matches(event_type: str, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return True
    return any(event_type == pattern or event_type.startswith(pattern + ".")
               for pattern in patterns)


def export_events(paths: Any, format: str = "jsonl", since: int | None = None,
                  until: int | None = None, types: Any = None, *,
                  project_id: str | None = None,
                  dest: str | Path | None = None) -> Path:
    """Export the event log to JSONL or CSV and return the output path.

    ``since``/``until`` are sequence numbers (``since`` exclusive, ``until``
    inclusive).  ``types`` accepts a list or comma-separated string and uses
    the same dotted-prefix matching as ``EventLog.query``.  Output defaults to
    ``<state>/exports/events-<UTC>.<ext>``.
    """
    fmt = str(format or "jsonl").strip().lower()
    if fmt not in ("jsonl", "csv"):
        raise BackupError("format must be 'jsonl' or 'csv'")
    state = state_dir_of(paths)
    events_dir = events_dir_of(paths)
    if not events_dir.exists():
        raise BackupError(f"event log directory does not exist: {events_dir}")
    since_seq = None if since is None else safe_int(since, -1)
    until_seq = None if until is None else safe_int(until, -1)
    if since_seq is not None and since_seq < 0:
        raise BackupError("since must be a non-negative sequence number")
    if until_seq is not None and until_seq < 0:
        raise BackupError("until must be a non-negative sequence number")
    patterns = _event_types(types)
    project = str(project_id) if project_id else None

    rows: list[dict] = []
    for path in _event_files(events_dir):
        for row in iter_jsonl(path):
            seq = safe_int(row.get("seq"), -1)
            if seq < 0:
                continue
            if since_seq is not None and seq <= since_seq:
                continue
            if until_seq is not None and seq > until_seq:
                continue
            if project is not None and row.get("project_id") != project:
                continue
            if not _type_matches(str(row.get("type") or ""), patterns):
                continue
            rows.append(row)
    rows.sort(key=lambda row: safe_int(row.get("seq"), 0))

    destination = (Path(dest).expanduser() if dest is not None
                   else state / "exports" /
                   f"events-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.{fmt}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".",
                                     suffix=".tmp", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            if fmt == "jsonl":
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            else:
                writer = csv.writer(handle)
                writer.writerow(["seq", "ts", "type", "source",
                                 "project_id", "data"])
                for row in rows:
                    writer.writerow([
                        safe_int(row.get("seq")),
                        safe_int(row.get("ts")),
                        str(row.get("type") or ""),
                        str(row.get("source") or ""),
                        str(row.get("project_id") or ""),
                        json.dumps(row.get("data") or {}, ensure_ascii=False,
                                   sort_keys=True),
                    ])
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    _LOG.info("exported %d events to %s", len(rows), destination)
    return destination


# -- status / extension --------------------------------------------------
def list_backups(paths: Any) -> list[Path]:
    """Return backup archives in the default directory, newest first."""
    state = state_dir_of(paths)
    directory = state / "backups"
    try:
        rows = [path for path in directory.glob(_ARCHIVE_PREFIX + "*" + _ARCHIVE_SUFFIX)
                if path.is_file()]
    except OSError:
        return []
    rows.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    return rows


def backup_status(paths: Any) -> dict:
    """Summary used by ``health()`` and the CLI."""
    state = state_dir_of(paths)
    rows = list_backups(paths)
    total = 0
    for path in rows:
        try:
            total += path.stat().st_size
        except OSError:
            pass
    last = rows[0] if rows else None
    return {
        "ok": True,
        "dir": str(state / "backups"),
        "backups": len(rows),
        "bytes": total,
        "last_backup": str(last) if last is not None else None,
        "keep": backup_keep_of(paths),
    }


class BackupExtension:
    """Extension object: ``health()`` plus the pure functions for the CLI."""

    name = "backup"

    def __init__(self, app: Any) -> None:
        self.app = app
        self.log = get_logger("observability.backup")

    def create(self, dest: str | Path | None = None, keep: int | None = None) -> Path:
        return create_backup(self.app, dest=dest, keep=keep)

    def restore(self, archive: str | Path, force: bool = False) -> dict:
        return restore(archive, self.app.paths.state_dir, force=force)

    def export(self, format: str = "jsonl", **kwargs: Any) -> Path:
        return export_events(self.app, format=format, **kwargs)

    def health(self) -> dict:
        try:
            return backup_status(self.app)
        except Exception as exc:  # health must never raise
            return {"ok": False, "error": type(exc).__name__}


def build(app: Any) -> BackupExtension:
    """Extension entry point used by :mod:`stackflowos.extensions`."""
    return BackupExtension(app)


__all__ = ["BackupConflict", "BackupError", "BackupExtension", "backup_status", "build", "create_backup",
           "export_events", "list_backups", "restore"]
