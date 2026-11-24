"""Logging setup for StackFlowOS.

Console logs are for humans watching a terminal; the file log is the durable
trace of the kernel. Event records live in the event log, not here.

The file handler can rotate (:class:`logging.handlers.RotatingFileHandler`).
Rotation is opt-in: :func:`setup_logging` keeps its historical signature and
behaviour unless ``rotation`` is passed, and the observability extension can
re-apply the ``observability.log_rotate_*`` settings to the live hub logger
without duplicating handlers.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

ROOT_LOGGER = "sfo"
_FORMAT = "%(asctime)s %(levelname)-5s %(name)s | %(message)s"
_DATEFMT = "%H:%M:%S"
_FILE_FORMAT = "%(asctime)s %(levelname)-5s %(name)s | %(message)s"

_configured = False

#: Marker put on the file handler this module owns.  The observability layer
#: uses it so reconfiguration cannot remove console handlers or handlers that
#: another subsystem installed.
MANAGED = "_sfo_file_handler"


def file_formatter() -> logging.Formatter:
    """The formatter used for durable log lines (console uses a short clock)."""
    return logging.Formatter(_FILE_FORMAT)


def normalise_rotation(rotation: Any) -> tuple[int, int] | None:
    """Return ``(max_bytes, backup_count)`` from the accepted rotation shapes.

    Accepted values:

    * ``None`` -- no rotation (plain :class:`logging.FileHandler`);
    * ``(max_bytes, backup_count)`` tuple/list;
    * ``{"bytes": n, "keep": k}`` (also ``max_bytes``/``backup_count`` keys);
    * a plain integer meaning ``max_bytes`` with the default keep count.

    A non-positive ``max_bytes`` disables rotation, which keeps
    ``observability.log_rotate_bytes = 0`` meaningful.
    """
    if rotation is None:
        return None
    if isinstance(rotation, bool):  # bool is an int; reject it explicitly
        raise TypeError("rotation must be a byte count, tuple or dict")
    if isinstance(rotation, int):
        return (int(rotation), 3)
    if isinstance(rotation, (tuple, list)):
        if len(rotation) != 2:
            raise TypeError("rotation tuple must be (max_bytes, backup_count)")
        return (int(rotation[0]), max(0, int(rotation[1])))
    if isinstance(rotation, dict):
        max_bytes = rotation.get("bytes", rotation.get("max_bytes", 0))
        keep = rotation.get("keep", rotation.get("backup_count", 3))
        return (int(max_bytes), max(0, int(keep)))
    raise TypeError(f"unsupported rotation config: {type(rotation)!r}")


def make_file_handler(log_file: Path, rotation: Any = None) -> logging.Handler:
    """Create the file handler for *log_file* (rotating when configured)."""
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalised = normalise_rotation(rotation)
    if normalised is not None and normalised[0] > 0:
        max_bytes, keep = normalised
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max(1, max_bytes), backupCount=max(0, keep),
            encoding="utf-8")
    else:
        handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(file_formatter())
    setattr(handler, MANAGED, True)
    return handler


def _detach_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - closing must not break setup
            pass


def setup_logging(level: str = "INFO", log_file: Path | None = None,
                  *, console: bool = True, quiet: bool = False,
                  rotation: Any = None,
                  force: bool = False) -> logging.Logger:
    """Configure the root ``sfo`` logger.

    ``rotation`` is optional and defaults to the historical plain file
    handler, so existing call sites (``setup_logging("INFO", log_file)``) keep
    working.  Pass ``force=True`` to rebuild handlers in place; tests use it,
    production reconfiguration goes through
    :func:`stackflowos.observability.logging.configure_from_app`.
    """
    global _configured
    root = logging.getLogger(ROOT_LOGGER)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.propagate = False
    if _configured and not force:
        return root
    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    if force:
        _detach_handlers(root)
    if console and not quiet:
        # A caller may have attached its own handlers before us (tests do);
        # leave them alone, but avoid stacking a second console handler.
        has_console = any(
            isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
            and getattr(handler, "stream", None) is sys.stderr
            for handler in root.handlers)
        if force or not has_console:
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setFormatter(fmt)
            root.addHandler(console_handler)
    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        root.addHandler(make_file_handler(path, rotation))
    _configured = True
    return root


def get_logger(name: str = "core") -> logging.Logger:
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")


__all__ = ["ROOT_LOGGER", "MANAGED", "file_formatter", "get_logger",
           "make_file_handler", "normalise_rotation", "setup_logging"]
