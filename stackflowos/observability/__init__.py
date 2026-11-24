"""Operational observability: metrics, notifications, backup/restore, logging.

The package is loaded as three optional extensions by
:mod:`stackflowos.extensions` (``metrics``, ``notifications``, ``backup``).
Each one only contributes optional hooks, so a broken subsystem cannot take the
hub down.  This module holds the shared, side-effect-free helpers:

* :func:`state_dir_of` / :func:`events_dir_of` -- accept an ``App``, ``Paths``
  object or a path so the CLI and the extensions agree on locations;
* :func:`redact_structure` -- recursive credential masking used before a
  notification payload is sent or recorded;
* small numeric helpers that never raise on malformed counters.

Nothing here imports the web server or the kernel, keeping observability
usable from a plain CLI process.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

from ..util.redact import MASK, redact_text

#: On-disk backup format/version handled by :mod:`stackflowos.observability.backup`.
BACKUP_FORMAT = "sfo-backup"
BACKUP_VERSION = 1

#: Event types this package emits about itself.  They are never forwarded to
#: notification sinks, which is what keeps a webhook from notifying about its
#: own notification (a feedback loop / "notification storm").
META_EVENTS = frozenset({
    "approval.notified",
    "observability.notified",
    "observability.notify_failed",
})


def state_dir_of(source: Any) -> Path:
    """Resolve a state directory from an App, ``Paths`` object or path."""
    if isinstance(source, (str, bytes, Path)):
        return Path(source).expanduser().resolve()
    if source is None:
        raise TypeError("a Paths object, App or state-dir path is required")
    state = getattr(source, "state_dir", None)
    if state is None:
        paths = getattr(source, "paths", None)
        state = getattr(paths, "state_dir", None)
    if state is None:
        raise TypeError(
            f"cannot find state_dir on {type(source).__name__}; pass a path")
    return Path(state).expanduser().resolve()


def config_of(source: Any) -> Any:
    """Return ``source.config`` when present, otherwise ``source`` itself."""
    return getattr(source, "config", source)


def backup_keep_of(source: Any, default: int = 7) -> int:
    """Read ``observability.backup_keep`` without requiring a Config object."""
    config = config_of(source)
    observability = getattr(config, "observability", None)
    value = getattr(observability, "backup_keep", None)
    try:
        return int(value) if value is not None else int(default)
    except (TypeError, ValueError):
        return int(default)


def app_version() -> str:
    from .. import __version__

    return str(__version__)


def now_ms() -> int:
    return int(time.time() * 1000)


def safe_int(value: Any, default: int = 0) -> int:
    """Best-effort integer for counters read from optional subsystems."""
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                return default
            return int(value)
        return int(str(value).strip(), 10)
    except (TypeError, ValueError, OverflowError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def redact_structure(value: Any, *, _depth: int = 0) -> Any:
    """Return a JSON-friendly copy with credentials masked.

    * mapping keys that look like secrets (``api_key``, ``token``,
      ``Authorization``, ...) have their whole value replaced with ``***``;
    * strings get :func:`stackflowos.util.redact.redact_text` applied
      (``Bearer <token>``, ``api_key=...``, URL userinfo, known token shapes);
    * lists/tuples are walked recursively.

    The function is pure, never mutates the input and never raises on odd
    values.  It is defence in depth for logs/events/outbound notifications,
    not a substitute for not collecting secrets in the first place.
    """
    if _depth > 32:  # pathological/self-referential input: stop, do not loop
        return "<too-deep>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if is_secret_name(name):
                out[name] = MASK
            else:
                out[name] = redact_structure(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_structure(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if hasattr(value, "to_dict"):
        try:
            return redact_structure(value.to_dict(), _depth=_depth + 1)
        except Exception:
            return f"<{type(value).__name__}>"
    return redact_text(str(value))


def is_secret_name(name: str) -> bool:
    """True for header/key names whose value is a credential."""
    text = str(name).strip().lower()
    if text in ("authorization", "proxy-authorization", "cookie", "set-cookie"):
        return True
    from ..util.redact import is_secret_key

    return is_secret_key(text)


def redact_url(url: str) -> str:
    """Mask userinfo and token-looking query values in a URL for logs/events."""
    return redact_text(str(url))

def events_dir_of(source: Any) -> Path:
    """Resolve the event-log directory from an App, ``Paths`` object or path."""
    if isinstance(source, (str, bytes, Path)):
        state = Path(source).expanduser().resolve()
        return state / "events"
    events = getattr(source, "events_dir", None)
    if events is not None:
        return Path(events).expanduser().resolve()
    paths = getattr(source, "paths", None)
    events = getattr(paths, "events_dir", None)
    if events is not None:
        return Path(events).expanduser().resolve()
    return state_dir_of(source) / "events"

__all__ = [
    "BACKUP_FORMAT", "BACKUP_VERSION", "META_EVENTS", "app_version",
    "backup_keep_of", "config_of", "events_dir_of", "is_secret_name", "now_ms",
    "redact_structure", "redact_url", "safe_float", "safe_int", "state_dir_of",
]

