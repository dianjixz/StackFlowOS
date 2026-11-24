"""Context-window budgeting and trimming.

Providers call :func:`trim_messages` before every request.  The estimator is the
same conservative ``chars/3`` heuristic used for budgets (no tokenizer
dependency); trimming keeps the system prompt and the newest message, drops the
oldest history first, clips oversized tool results, and reports what happened so
the caller can emit ``llm.context.trimmed``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .telemetry import estimate_messages_tokens, estimate_tokens

#: Never trim below this even if the configured window is tiny.
MIN_CONTEXT_TOKENS = 256
#: Individual content clips applied before dropping whole messages.
TOOL_RESULT_CHARS = 8_000
MESSAGE_CHARS = 16_000


@dataclass
class TrimResult:
    messages: list[dict]
    dropped: int = 0
    clipped: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    limit: int = 0
    #: True when even after trimming the prompt might exceed the window.
    over_limit: bool = False
    details: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.dropped or self.clipped)

    def event_data(self) -> dict:
        return {
            "dropped": self.dropped,
            "clipped": self.clipped,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "limit": self.limit,
            "over_limit": self.over_limit,
        }


def estimate_tools_tokens(tools: Any) -> int:
    """Estimate the prompt cost of a native tool schema list."""
    if not tools:
        return 0
    try:
        text = json.dumps(tools, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(tools)
    return estimate_tokens(text)


def _estimate(messages: list[dict], tools_tokens: int = 0) -> int:
    return estimate_messages_tokens(messages) + max(0, int(tools_tokens or 0))


def _clip_content(message: dict) -> bool:
    content = message.get("content")
    limit = TOOL_RESULT_CHARS if message.get("role") == "tool" else MESSAGE_CHARS
    if not isinstance(content, str) or len(content) <= limit:
        return False
    suffix = f"\n…[已截断，原长 {len(content)} 字符]"
    message["content"] = content[:limit] + suffix
    return True


def trim_messages(messages: list[dict], limit: int, *,
                  tools: Any = None,
                  reserve_tokens: int = 0) -> TrimResult:
    """Return a copy of ``messages`` that fits the context token budget.

    ``limit`` is ``llm.max_context_tokens``; ``reserve_tokens`` leaves room for
    the completion.  A system message at index 0 and the newest message are
    always retained.  Tool-result messages lose their oldest siblings first.
    """
    original = [dict(item) if isinstance(item, dict) else item
                for item in (messages or [])]
    tools_tokens = estimate_tools_tokens(tools)
    tokens_before = _estimate(original, tools_tokens)
    window = max(MIN_CONTEXT_TOKENS, int(limit or 0))
    budget = max(MIN_CONTEXT_TOKENS, window - max(0, int(reserve_tokens or 0)))
    result = TrimResult(messages=original, tokens_before=tokens_before,
                        limit=window)
    if limit <= 0:
        result.tokens_after = tokens_before
        return result

    # 1) Clip individual oversized content (tool results are the usual culprit).
    for message in original:
        if isinstance(message, dict) and _clip_content(message):
            result.clipped += 1

    def size() -> int:
        return _estimate(original, tools_tokens)

    # 2) Drop the oldest non-system messages.  Keep at least system + newest.
    while size() > budget and len(original) > 2:
        index = 1
        removed = 1
        message = original[index]
        if (isinstance(message, dict)
                and message.get("role") == "assistant"
                and message.get("tool_calls")
                and index + 1 < len(original)
                and isinstance(original[index + 1], dict)
                and original[index + 1].get("role") == "tool"):
            removed = 2  # never orphan a tool result
        elif isinstance(message, dict) and message.get("role") == "tool":
            # A stray tool message: drop it alone.
            removed = 1
        del original[index:index + removed]
        result.dropped += removed
    result.over_limit = size() > budget

    # 3) Last resort: shrink the system prompt / newest message.
    if result.over_limit and original:
        for index in (0, len(original) - 1):
            content = original[index].get("content")
            if isinstance(content, str) and len(content) > 600:
                original[index]["content"] = content[:600] + "…[上下文已压缩]"
                result.clipped += 1
        result.over_limit = size() > budget

    result.tokens_after = size()
    return result


__all__ = [
    "MESSAGE_CHARS",
    "MIN_CONTEXT_TOKENS",
    "TOOL_RESULT_CHARS",
    "TrimResult",
    "estimate_tools_tokens",
    "trim_messages",
]
