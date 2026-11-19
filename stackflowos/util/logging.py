"""Logging setup for StackFlowOS.

Console logs are for humans watching a terminal; the file log is the durable
trace of the kernel. Event records live in the event log, not here.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT_LOGGER = "sfo"
_FORMAT = "%(asctime)s %(levelname)-5s %(name)s | %(message)s"
_DATEFMT = "%H:%M:%S"

_configured = False


def setup_logging(level: str = "INFO", log_file: Path | None = None,
                  *, console: bool = True, quiet: bool = False) -> logging.Logger:
    global _configured
    root = logging.getLogger(ROOT_LOGGER)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.propagate = False
    if _configured:
        return root
    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    if console and not quiet:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(fmt)
        root.addHandler(handler)
    if log_file is not None:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(Path(log_file), encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s | %(message)s"))
        root.addHandler(file_handler)
    _configured = True
    return root


def get_logger(name: str = "core") -> logging.Logger:
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")
