"""AI layer: LLM providers, routing, prompts and the LLM-driven brain."""

from __future__ import annotations

from .llm import (AnthropicProvider, BaseProvider, GeminiProvider, LLMBrain,
                  LLMProvider, LLMResult, MockProvider,
                  OpenAICompatibleProvider, Router, ToolCall, Usage, complete,
                  make_provider, supports_native_tools)
from .prompts import SYSTEM_PROMPT_OS, role_prompt, tool_call_protocol

__all__ = [
    "AnthropicProvider",
    "BaseProvider",
    "GeminiProvider",
    "LLMBrain",
    "LLMProvider",
    "LLMResult",
    "MockProvider",
    "OpenAICompatibleProvider",
    "Router",
    "SYSTEM_PROMPT_OS",
    "ToolCall",
    "Usage",
    "complete",
    "make_provider",
    "role_prompt",
    "supports_native_tools",
    "tool_call_protocol",
]
