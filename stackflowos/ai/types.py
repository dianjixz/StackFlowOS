"""Structured LLM results shared by every provider.

The original provider interface returned just the assistant text.  Native tool
calling, usage accounting, cost and context management need more than that, so
providers now return :class:`LLMResult`.  ``chat()`` keeps returning a plain
string for backwards compatibility and is implemented on top of
``chat_with_tools()``.

The wire formats differ (OpenAI ``tool_calls``, Anthropic ``tool_use`` blocks,
Gemini ``functionCall``); everything is normalised to these small dataclasses
before it reaches the brain/chat layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Usage:
    """Token accounting for one request.

    ``prompt``/``completion``/``total`` mirror the OpenAI field names.  When a
    provider omits usage (some OpenAI-compatible gateways do) the caller fills
    in a conservative estimate so budgets still work.
    """

    prompt: int = 0
    completion: int = 0
    total: int = 0

    def __post_init__(self) -> None:
        self.prompt = max(0, int(self.prompt or 0))
        self.completion = max(0, int(self.completion or 0))
        self.total = max(0, int(self.total or 0))
        if self.total == 0 and (self.prompt or self.completion):
            self.total = self.prompt + self.completion

    @classmethod
    def from_dict(cls, data: Any) -> "Usage":
        if not isinstance(data, dict):
            return cls()
        prompt = data.get("prompt", data.get("prompt_tokens",
                                             data.get("input_tokens",
                                                      data.get(
                                                          "promptTokenCount", 0))))
        completion = data.get("completion",
                              data.get("completion_tokens",
                                       data.get("output_tokens",
                                                data.get(
                                                    "candidatesTokenCount", 0))))
        total = data.get("total", data.get("total_tokens",
                                           data.get("totalTokenCount", 0)))
        return cls(prompt=prompt, completion=completion, total=total)

    def merged(self, other: "Usage") -> "Usage":
        """Merge incremental usage chunks (streaming providers split them)."""
        return Usage(
            prompt=max(self.prompt, other.prompt),
            completion=max(self.completion, other.completion),
            total=max(self.total, other.total)
            or max(self.prompt, other.prompt) + max(self.completion, other.completion),
        )

    def add(self, other: "Usage") -> "Usage":
        return Usage(prompt=self.prompt + other.prompt,
                     completion=self.completion + other.completion,
                     total=self.total + other.total)

    def to_dict(self) -> dict:
        return {"prompt": self.prompt, "completion": self.completion,
                "total": self.total}

    def __getitem__(self, key: str) -> int:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __bool__(self) -> bool:
        return bool(self.total)


@dataclass
class ToolCall:
    """One normalised tool call requested by the model."""

    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    #: Raw arguments string as sent by the provider (useful for diagnostics).
    raw_arguments: str = ""

    @classmethod
    def from_openai(cls, raw: Any) -> "ToolCall | None":
        if not isinstance(raw, dict):
            return None
        function = raw.get("function") if isinstance(raw.get("function"), dict) else raw
        name = function.get("name") or raw.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        raw_args = function.get("arguments", raw.get("arguments", {}))
        arguments, raw_text = _normalise_arguments(raw_args)
        return cls(
            id=str(raw.get("id") or raw.get("tool_call_id") or ""),
            name=name.strip(),
            arguments=arguments,
            raw_arguments=raw_text,
        )

    @classmethod
    def from_anthropic(cls, raw: Any) -> "ToolCall | None":
        if not isinstance(raw, dict):
            return None
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        raw_args = raw.get("input", {})
        arguments, raw_text = _normalise_arguments(raw_args)
        return cls(id=str(raw.get("id") or ""), name=name.strip(),
                   arguments=arguments, raw_arguments=raw_text)

    @classmethod
    def from_gemini(cls, raw: Any, *, index: int = 0) -> "ToolCall | None":
        if not isinstance(raw, dict):
            return None
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        raw_args = raw.get("args", {})
        arguments, raw_text = _normalise_arguments(raw_args)
        return cls(id=f"gemini-call-{index}", name=name.strip(),
                   arguments=arguments, raw_arguments=raw_text)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    # Mapping-style access keeps the documented
    # ``tool_calls[{id, name, arguments}]`` shape usable directly.
    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __iter__(self):
        return iter(self.to_dict().items())

    def to_openai(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


def _normalise_arguments(value: Any) -> tuple[dict, str]:
    """Return ``(arguments_dict, raw_text)`` tolerating partial/garbled JSON."""
    if isinstance(value, dict):
        return dict(value), json.dumps(value, ensure_ascii=False)
    if value is None:
        return {}, ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}, ""
        try:
            parsed = json.loads(text)
        except ValueError:
            return {}, text
        if isinstance(parsed, dict):
            return parsed, text
        return {"value": parsed}, text
    return {}, str(value)


@dataclass
class LLMResult:
    """Normalised provider response used by brains and chat services."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    model: str = ""
    provider: str = ""
    #: Raw provider payload (never contains secrets; useful for diagnostics).
    raw: dict | None = None

    @property
    def content(self) -> str:
        """Alias for callers that think in OpenAI ``content`` terms."""
        return self.text

    @content.setter
    def content(self, value: str) -> None:
        self.text = str(value or "")

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "tool_calls": [
                (coerce_tool_call(call).to_dict()
                 if coerce_tool_call(call) is not None else call)
                for call in self.tool_calls
            ],
            "usage": self.usage.to_dict(),
            "finish_reason": self.finish_reason,
            "model": self.model,
            "provider": self.provider,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __bool__(self) -> bool:
        return bool(self.text or self.tool_calls)


def parse_tool_arguments(value: Any) -> dict:
    """Public helper for provider adapters and tests."""
    return _normalise_arguments(value)[0]


def coerce_tool_call(value: Any) -> ToolCall | None:
    """Accept a :class:`ToolCall` or the documented dict shape.

    Test doubles and third-party providers sometimes return plain dicts
    (``{"id", "name", "arguments"}`` or the OpenAI ``function`` shape); the
    brain and chat loops use this helper so both work.
    """
    if isinstance(value, ToolCall):
        return value
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("function"), dict):
        return ToolCall.from_openai(value)
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    arguments, raw = _normalise_arguments(
        value.get("arguments", value.get("args", {})))
    return ToolCall(id=str(value.get("id") or ""), name=name.strip(),
                    arguments=arguments, raw_arguments=raw)


def coerce_tool_calls(values: Any) -> list[ToolCall]:
    out: list[ToolCall] = []
    for value in values or []:
        call = coerce_tool_call(value)
        if call is not None:
            out.append(call)
    return out


__all__ = ["LLMResult", "ToolCall", "Usage", "coerce_tool_call",
           "coerce_tool_calls", "parse_tool_arguments"]
