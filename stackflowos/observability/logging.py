"""Hub log rotation reconfiguration.

``App.start`` calls :func:`stackflowos.util.logging.setup_logging` with a plain
``FileHandler``.  The ``observability.log_rotate_bytes`` / ``log_rotate_keep``
settings arrive later (and may change at runtime), so this module swaps that
file handler for a :class:`logging.handlers.RotatingFileHandler` while leaving
console handlers untouched.

It is deliberately idempotent: applying the same settings twice still leaves
exactly one file handler for the hub log.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..util.logging import (MANAGED, ROOT_LOGGER, file_formatter,
                            make_file_handler)
from . import config_of
from . import safe_int

_LOG = logging.getLogger("sfo.observability.logging")


def _is_hub_file_handler(handler: logging.Handler, log_file: Path) -> bool:
    if not isinstance(handler, logging.FileHandler):
        return False
    if getattr(handler, MANAGED, False):
        return True
    try:
        return Path(handler.baseFilename).resolve() == log_file.resolve()
    except (OSError, ValueError):
        return False


def configure_file_logging(log_file: Path | str | None, *,
                           max_bytes: int = 0,
                           backup_count: int = 0,
                           level: str | int | None = None) -> dict:
    """Replace the hub log's file handler with the requested rotation policy.

    The console handler is preserved.  Only file handlers belonging to this
    process' hub log are removed, so unrelated handlers installed by an
    embedding application survive.  Returns a small status dict instead of
    raising: log reconfiguration must never break the hub.
    """
    root = logging.getLogger(ROOT_LOGGER)
    if level is not None:
        root.setLevel(getattr(logging, str(level).upper(), root.level))
    if log_file is None:
        return {"configured": False, "reason": "no log file"}
    path = Path(log_file).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        removed = 0
        for handler in list(root.handlers):
            if _is_hub_file_handler(handler, path):
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:  # pragma: no cover - close is best effort
                    pass
                removed += 1
        handler = make_file_handler(
            path, rotation=(int(max_bytes), int(backup_count))
            if int(max_bytes) > 0 else None)
        handler.setFormatter(file_formatter())
        root.addHandler(handler)
    except Exception as exc:  # pragma: no cover - defensive, logged below
        _LOG.exception("could not configure log rotation for %s", path)
        return {"configured": False, "path": str(path),
                "error": type(exc).__name__}
    result = {
        "configured": True,
        "path": str(path),
        "max_bytes": max(0, int(max_bytes)),
        "backup_count": max(0, int(backup_count)),
        "rotating": int(max_bytes) > 0,
        "replaced": removed,
    }
    _LOG.info("hub log rotation: path=%s max_bytes=%s keep=%s",
              result["path"], result["max_bytes"], result["backup_count"])
    return result


def configure_from_app(app: Any) -> dict:
    """Apply ``app.config.observability.log_rotate_*`` to the hub logger."""
    config = config_of(app)
    observability = getattr(config, "observability", None)
    paths = getattr(app, "paths", None)
    log_file = getattr(paths, "log_file", None)
    if log_file is None:
        return {"configured": False, "reason": "app has no paths.log_file"}
    return configure_file_logging(
        log_file,
        max_bytes=safe_int(getattr(observability, "log_rotate_bytes", 0)),
        backup_count=safe_int(getattr(observability, "log_rotate_keep", 0)),
    )


__all__ = ["configure_file_logging", "configure_from_app"]
