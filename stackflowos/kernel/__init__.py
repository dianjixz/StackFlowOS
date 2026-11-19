"""StackFlowOS L1 kernel: dependency container + typed events + lifecycle."""

from .context import Context, PluginHandle
from .events import EventBus, Hook, is_bailed
from .service import Service

__all__ = ["Context", "PluginHandle", "EventBus", "Hook", "is_bailed", "Service"]
