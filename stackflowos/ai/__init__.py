"""AI layer: LLM providers, prompt text and the LLM-driven brain."""

from __future__ import annotations

from .llm import (LLMBrain, LLMProvider, MockProvider,
                  OpenAICompatibleProvider, make_provider)
from .prompts import SYSTEM_PROMPT_OS, role_prompt, tool_call_protocol

__all__ = [
    "LLMBrain",
    "LLMProvider",
    "MockProvider",
    "OpenAICompatibleProvider",
    "SYSTEM_PROMPT_OS",
    "make_provider",
    "role_prompt",
    "tool_call_protocol",
]
