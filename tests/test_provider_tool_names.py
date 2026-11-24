"""Wire-safe tool names: dotted internals must survive real providers.

OpenAI/Anthropic/Gemini reject ``fs.read`` (charset ``[A-Za-z0-9_-]``), so the
provider layer translates names out and consumers translate them back.  These
tests pin the mapping, all three schema builders and the two call parsers.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from stackflowos.agents.tools import TOOLS
from stackflowos.ai.llm import LLMBrain
from stackflowos.ai.providers import (
    anthropic_tools,
    gemini_tools,
    internal_tool_name,
    openai_tool_schemas,
    provider_tool_name,
)
from stackflowos.ai.types import ToolCall


class ProviderToolNameTests(unittest.TestCase):
    def test_dots_become_underscores(self) -> None:
        self.assertEqual(provider_tool_name("fs.read"), "fs_read")
        self.assertEqual(provider_tool_name("task.create"), "task_create")
        self.assertEqual(provider_tool_name("wait"), "wait")

    def test_invalid_characters_are_replaced(self) -> None:
        self.assertEqual(provider_tool_name("weird name!"), "weird_name_")
        self.assertEqual(provider_tool_name("a/b:c"), "a_b_c")

    def test_leading_digit_is_allowed_by_the_provider_charset(self) -> None:
        self.assertEqual(provider_tool_name("1tool"), "1tool")
        self.assertEqual(provider_tool_name(""), "tool")

    def test_long_names_are_truncated(self) -> None:
        self.assertLessEqual(len(provider_tool_name("x" * 200)), 64)

    def test_all_real_tool_names_stay_unique(self) -> None:
        safe = [provider_tool_name(name) for name in TOOLS]
        self.assertEqual(len(safe), len(set(safe)),
                         "provider tool names collide")

    def test_internal_resolution_prefers_exact_then_safe_alias(self) -> None:
        known = ["fs.read", "task.create", "wait"]
        self.assertEqual(internal_tool_name("fs.read", known), "fs.read")
        self.assertEqual(internal_tool_name("fs_read", known), "fs.read")
        self.assertEqual(internal_tool_name("wait", known), "wait")
        self.assertEqual(internal_tool_name("nope", known), "nope")
        self.assertEqual(internal_tool_name("fs_read", None), "fs_read")

    def test_openai_schema_uses_wire_safe_names(self) -> None:
        schemas = openai_tool_schemas([{
            "name": "fs.read", "description": "read",
            "args": {"path": "string"},
        }])
        self.assertEqual(schemas[0]["function"]["name"], "fs_read")
        self.assertEqual(schemas[0]["function"]["parameters"]["required"],
                         ["path"])

    def test_anthropic_and_gemini_schemas_use_wire_safe_names(self) -> None:
        tools = [{"name": "fs.write", "description": "write",
                  "args": {"path": "string", "content": "string"}}]
        anth = anthropic_tools(tools)
        self.assertEqual(anth[0]["name"], "fs_write")
        gem = gemini_tools(tools)
        self.assertEqual(
            gem[0]["functionDeclarations"][0]["name"], "fs_write")

    def test_llm_brain_accepts_a_wire_safe_call(self) -> None:
        action, error = LLMBrain._parse_tool_call(
            ToolCall(id="c1", name="fs_read", arguments={"path": "x"}),
            {"fs.read": object(), "wait": object()})
        self.assertEqual(error, "")
        self.assertEqual(action.tool, "fs.read")
        self.assertEqual(action.args, {"path": "x"})

    def test_llm_brain_accepts_the_internal_spelling_too(self) -> None:
        action, error = LLMBrain._parse_tool_call(
            ToolCall(id="c1", name="fs.read", arguments={"path": "x"}),
            {"fs.read": object()})
        self.assertEqual(error, "")
        self.assertEqual(action.tool, "fs.read")

    def test_native_history_echoes_the_wire_safe_name(self) -> None:
        brain = LLMBrain(SimpleNamespace(name="openai", model="m"), "coder")
        action = SimpleNamespace(tool="fs.read", args={"path": "x"}, say="")
        call = ToolCall(id="c1", name="fs_read", arguments={"path": "x"})
        brain._remember_native_action(action, call, "")
        recorded = brain._history[-1]["tool_calls"][0]["function"]["name"]
        self.assertEqual(recorded, "fs_read")

    def test_native_history_normalises_an_internal_echo(self) -> None:
        brain = LLMBrain(SimpleNamespace(name="openai", model="m"), "coder")
        action = SimpleNamespace(tool="fs.read", args={"path": "x"}, say="")
        call = ToolCall(id="c1", name="fs.read", arguments={"path": "x"})
        brain._remember_native_action(action, call, "")
        recorded = brain._history[-1]["tool_calls"][0]["function"]["name"]
        self.assertEqual(recorded, "fs_read")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
