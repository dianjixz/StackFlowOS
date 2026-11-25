"""LLM providers, routing and the optional LLM-driven brain.

The public construction point is :func:`make_provider`, used by the hub and by
worker processes.  Providers implement native tool calling, usage accounting
and streaming; :class:`stackflowos.ai.router.Router` adds role routes and a
fallback chain.  ``chat()`` keeps the historical "return a string" contract.

``LLMBrain`` sends native tool schemas when the provider supports them and
falls back to the JSON action protocol otherwise, so local OpenAI-compatible
gateways with weak function-calling support keep working.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from ..errors import BadRequest, SfoError, Unavailable
from ..util.logging import get_logger
from .factory import create_provider, spec_from_config
from .prompts import SYSTEM_PROMPT_OS, role_prompt, tool_call_protocol
from .providers import (
    AnthropicProvider,
    BaseProvider,
    GeminiProvider,
    MockProvider,
    OpenAICompatibleProvider,
    StreamInterrupted,
    _compact,
    _content_text,
    _is_transient,
    _last_user_text,
    _redact,
    anthropic_tools,
    gemini_tools,
    internal_tool_name,
    normalise_finish_reason,
    openai_tool_schemas,
    provider_tool_name,
)
from .router import Router
from .telemetry import (CHARS_PER_TOKEN, attach_provider_runtime,
                        clip_text_to_tokens, estimate_messages_tokens,
                        estimate_tokens)
from .types import (LLMResult, ToolCall, Usage, coerce_tool_calls,
                    parse_tool_arguments)

_log = get_logger("ai.llm")

#: Kept for backwards compatibility with older callers/tests.
_TRANSIENT_HTTP = ("LLM HTTP 429", "LLM HTTP 500", "LLM HTTP 502",
                   "LLM HTTP 503", "LLM HTTP 504")


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------

def make_provider(cfg: Any) -> Any:
    """Build the configured provider (or a router when routes/fallback are set).

    ``app.update_config`` calls this for every successful config patch and
    swaps ``chat.provider`` / ``agent_chat.provider``; the returned object is
    stateless apart from usage/circuit counters, which live in the shared
    runtime so a rebuild does not lose or duplicate accounting.
    """
    routes = getattr(cfg, "routes", None) or []
    fallback = getattr(cfg, "fallback", None) or []
    if routes or fallback:
        return Router(cfg)
    return create_provider(spec_from_config(cfg))


def attach_runtime(provider: Any, app: Any) -> None:
    """Backwards-friendly alias for ``attach_provider_runtime``."""
    attach_provider_runtime(provider, app)


def supports_native_tools(provider: Any) -> bool:
    """True when the provider implements the structured interface."""
    return callable(getattr(provider, "chat_with_tools", None))


async def complete(provider: Any, messages: list[dict], *,
                   tools: list[dict] | None = None,
                   role: str | None = None,
                   project_id: str | None = None,
                   on_delta: Any = None) -> LLMResult:
    """Call the structured interface when available, else the legacy one.

    ``tools`` is a native tool schema list (use :func:`openai_tool_schemas`).
    Legacy providers receive no tools, which preserves their old prompt
    contract (the JSON protocol is described in the system prompt).
    """
    method = getattr(provider, "chat_with_tools", None)
    if callable(method):
        return await method(messages, tools=tools, role=role,
                            project_id=project_id, on_delta=on_delta)
    raw = await provider.chat(messages)
    text = raw if isinstance(raw, str) else str(raw or "")
    usage = Usage(prompt=estimate_messages_tokens(messages),
                  completion=estimate_tokens(text))
    return LLMResult(text=text, usage=usage,
                     model=str(getattr(provider, "model", "") or ""),
                     provider=str(getattr(provider, "name", "") or ""))


# ---------------------------------------------------------------------------
# brain helpers
# ---------------------------------------------------------------------------

def _first_json_object(text: str) -> dict | None:
    """Return the first complete JSON object embedded in ``text``."""
    # strict=False tolerates raw newlines/tabs inside strings, which models
    # emit constantly when writing file content.
    decoder = json.JSONDecoder(strict=False)
    if not isinstance(text, str):
        return None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _clip_value(value: Any, limit: int) -> Any:
    """Recursively shorten long strings so one result cannot flood the context."""
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return value[:limit] + f"…[已截断，共 {len(value)} 字符]"
    if isinstance(value, dict):
        return {key: _clip_value(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_clip_value(item, limit) for item in value[:60]]
    return value


def _board_line(view: Any) -> str:
    tasks = getattr(view, "tasks", []) or []
    rows = [f"{item.get('id')}|{_compact(str(item.get('title') or ''), 40)}|"
            f"{item.get('status')}|{item.get('role') or '-'}|"
            f"{item.get('assignee') or '-'}"
            for item in tasks[:20] if isinstance(item, dict)]
    return "\n".join(rows)


def _compact_state(view: Any) -> str:
    """Serialise just enough state for an LLM to choose an action."""
    agent = getattr(view, "agent", None)
    project = getattr(view, "project", {}) or {}
    task = getattr(view, "task", None)
    tasks = getattr(view, "tasks", []) or []
    events = getattr(view, "recent", None) or getattr(view, "events", []) or []

    def short(value: Any, limit: int = 240) -> str:
        return _compact(str(value or ""), limit)

    state: dict[str, Any] = {
        "agent": {
            "id": getattr(agent, "id", ""),
            "role": getattr(agent, "role", ""),
            "task_id": getattr(agent, "task_id", None),
        },
        "project": {
            "id": project.get("id"),
            "name": project.get("name"),
            "goal": short(project.get("goal"), 400),
            "status": project.get("status"),
            "brief": short(project.get("brief"), 400),
        },
        "task": None,
        "tasks": [
            {"id": item.get("id"), "title": short(item.get("title"), 80),
             "status": item.get("status"), "role": item.get("role"),
             "assignee": item.get("assignee")}
            for item in tasks[:12] if isinstance(item, dict)
        ],
        "recent_events": [
            {"seq": event.get("seq"), "type": event.get("type"),
             "data": short(event.get("data"), 160)}
            for event in events[-6:] if isinstance(event, dict)
        ],
        "workspace": getattr(view, "workspace", ""),
        "memory": {},
        "step": getattr(view, "step", 0),
        "last_tool_results": [],
    }
    memory = getattr(view, "memory", None)
    if isinstance(memory, dict):
        state["memory"] = {
            str(key): _clip_value(value, 400)
            for key, value in list(memory.items())[:20]
        }
    for result in (getattr(view, "tool_results", None) or [])[-3:]:
        if not isinstance(result, dict):
            continue
        data = result.get("data") or {}
        rendered = json.dumps(data, ensure_ascii=False, default=str)
        state["last_tool_results"].append({
            "tool": result.get("tool"),
            "ok": result.get("ok"),
            "error": short(result.get("error"), 300),
            "data": rendered[:4000],
        })
    if isinstance(task, dict):
        state["task"] = {
            "id": task.get("id"),
            "title": short(task.get("title"), 120),
            "description": short(task.get("description"), 600),
            "status": task.get("status"),
            "role": task.get("role"),
            "artifacts": [
                {"type": item.get("type"), "path": item.get("path")}
                for item in (task.get("artifacts") or [])[:6]
                if isinstance(item, dict)
            ],
        }
    return json.dumps(state, ensure_ascii=False, indent=2)


#: Conversation budget: keep the opening context plus the newest turns.
MAX_HISTORY_MESSAGES = 40
#: Token budget for the rolling conversation (the historical char budget is
#: kept as a derived constant for import compatibility).  ``_trim`` measures
#: history with the same :func:`estimate_messages_tokens` used for provider
#: usage, so trimming and accounting cannot disagree.
MAX_HISTORY_TOKENS = 20_000
MAX_HISTORY_CHARS = MAX_HISTORY_TOKENS * CHARS_PER_TOKEN
#: Tool-result JSON cap for ``observe`` (token budget; ≈12k chars).
OBSERVE_RESULT_TOKENS = 4_000
RESULT_STR_LIMIT = 6000
ACTION_STR_LIMIT = 1500


class LLMBrain:
    """Brain that asks an :class:`LLMProvider` for one action per step.

    It keeps one conversation per agent run: the opening state, every action
    the model chose and every tool result (``observe``), so the model can read
    files, react to errors/denials and build on earlier steps like any
    ordinary tool-using agent.

    When the provider supports native tool calling, the tool schemas are sent
    as ``tools`` and the reply is parsed from ``tool_calls``.  Otherwise (and
    whenever the model answers in text) the documented JSON action protocol is
    still parsed, so existing local-model setups keep working.
    """

    def __init__(self, provider: Any, role: str, *,
                 max_steps: int = 16,
                 skill_renderer: Any = None,
                 skill_max_tokens: int = 0) -> None:
        self.provider = provider
        self.role = str(role)
        self.max_steps = int(max_steps)
        #: Optional ``(project_id, role, prompt) -> str`` hook.  Workers pass a
        #: filesystem-backed renderer so enabled skills reach the system prompt
        #: without the worker needing the hub's SkillRegistry object.
        self.skill_renderer = skill_renderer
        #: Token budget applied to rendered skills (0 = renderer's own limit).
        #: Callers configure it from ``skills.inject_max_tokens`` (or the
        #: legacy ``inject_max_chars`` through the shared estimator) so skill
        #: truncation uses the same token accounting as budgets.
        self.skill_max_tokens = max(0, int(skill_max_tokens or 0))
        self._skills_cache: tuple[tuple, str] | None = None
        self._history: list[dict] = []
        self._pending: Any = None
        self._pending_call_id: str | None = None
        self._board_sig = ""
        self._dropped = 0

    async def chat(self, messages: list[dict]) -> str:
        """Delegate a plain chat turn to the provider."""
        return await self.provider.chat(messages)

    # -- conversation memory ---------------------------------------------
    def _skills(self, view: Any) -> str:
        if self.skill_renderer is None:
            return ""
        task = getattr(view, "task", None) or {}
        if not isinstance(task, dict):
            task = {}
        project = getattr(view, "project", None) or {}
        if not isinstance(project, dict):
            project = {}
        prompt = " ".join(
            str(part) for part in (task.get("title"), task.get("description"),
                                   getattr(view, "prompt", ""))
            if part)
        project_id = project.get("id") or getattr(
            getattr(view, "agent", None), "project_id", None)
        signature = (project_id, self.role, prompt)
        if self._skills_cache is not None and self._skills_cache[0] == signature:
            return self._skills_cache[1]
        try:
            text = str(self.skill_renderer(project_id, self.role, prompt) or "")
        except Exception:  # noqa: BLE001 - skills must never break a run
            _log.debug("skill injection failed", exc_info=True)
            text = ""
        if text and self.skill_max_tokens:
            # Enforce the configured budget with the shared estimator instead
            # of trusting the renderer's character cut (which would silently
            # mean a different token count for CJK/code-heavy skills).
            text = clip_text_to_tokens(
                text, self.skill_max_tokens,
                suffix="\n...[skills truncated to token budget]...")
        self._skills_cache = (signature, text)
        return text

    def _system(self, view: Any = None) -> str:
        text = "\n\n".join((SYSTEM_PROMPT_OS, role_prompt(self.role),
                             tool_call_protocol(self.role)))
        if supports_native_tools(self.provider):
            text += ("\n\n本模型支持原生工具调用：优先直接调用提供的工具；"
                     "如果无法使用原生调用，再按上面的 JSON 协议输出。")
        skills = self._skills(view) if view is not None else ""
        if skills:
            text += "\n\n" + skills
        return text

    def observe(self, action: Any, result: Any) -> None:
        """Record the tool result for the action returned by ``next_action``.

        Native tool calls are answered with a ``tool`` message carrying the
        matching ``tool_call_id``; the legacy JSON path keeps the old
        ``user``-role "工具结果" message because those providers have no
        tool-message slot in their conversation format.
        """
        payload: dict[str, Any] = {"tool": getattr(action, "tool", None),
                                   "ok": bool(getattr(result, "ok", False))}
        error = getattr(result, "error", None)
        if error:
            payload["error"] = _compact(str(error), 800)
            if str(error).startswith("DENIED"):
                payload["hint"] = "该操作被人工/策略拒绝，请换一种方式或 finish(blocked)。"
        data = getattr(result, "data", None)
        if data:
            payload["data"] = _clip_value(data, RESULT_STR_LIMIT)
        text = json.dumps(payload, ensure_ascii=False)
        if estimate_tokens(text) > OBSERVE_RESULT_TOKENS:
            text = clip_text_to_tokens(text, OBSERVE_RESULT_TOKENS,
                                       suffix="…[结果已截断]")
        call_id = self._pending_call_id
        if call_id:
            self._history.append({"role": "tool", "tool_call_id": call_id,
                                  "content": text})
        else:
            self._history.append({"role": "user", "content": f"工具结果：{text}"})
        self._pending_call_id = None
        self._pending = None

    def _remember_action(self, action: Any, reply: str) -> None:
        if getattr(action, "tool", None):
            record = {"tool": action.tool,
                      "args": _clip_value(dict(action.args or {}),
                                          ACTION_STR_LIMIT)}
            if getattr(action, "say", ""):
                record["say"] = action.say
            content = json.dumps(record, ensure_ascii=False)
        else:
            content = _compact(reply, 600)
        self._history.append({"role": "assistant", "content": content})

    def _remember_native_action(self, action: Any, call: ToolCall,
                                reply: str) -> None:
        record = {
            "role": "assistant",
            "content": _compact(reply, 600) if reply else "",
            "tool_calls": [{
                "id": call.id or "call-0",
                "type": "function",
                "function": {
                    # History must echo the wire-safe spelling: OpenAI/
                    # Anthropic/Gemini reject dotted names in a later request
                    # even when the call itself parsed fine.
                    "name": provider_tool_name(call.name),
                    "arguments": json.dumps(dict(action.args or {}),
                                            ensure_ascii=False),
                },
            }],
        }
        self._history.append(record)

    def _trim(self) -> None:
        """Drop the oldest assistant/user pairs (index 0 holds the opening)."""
        def size() -> int:
            # Same estimator the provider uses for prompt/usage accounting.
            return estimate_messages_tokens(self._history)

        while len(self._history) > 3 and (
                len(self._history) > MAX_HISTORY_MESSAGES
                or size() > MAX_HISTORY_TOKENS):
            remove = 2
            first = self._history[1]
            if (isinstance(first, dict) and first.get("role") == "assistant"
                    and first.get("tool_calls") and len(self._history) > 2
                    and isinstance(self._history[2], dict)
                    and self._history[2].get("role") == "tool"):
                remove = 2
            del self._history[1:1 + remove]
            self._dropped += 1

    def _step_hint(self, view: Any) -> str:
        step = getattr(view, "step", 0)
        hint = f"步数：{step}/{self.max_steps}。"
        if self.max_steps and step >= self.max_steps - 2:
            hint += "步数即将用尽，请尽快 finish 并如实汇报进度。"
        if self._dropped:
            hint += f"（较早的 {self._dropped} 轮对话已省略。）"
        return hint

    @staticmethod
    def _project_id(view: Any) -> str | None:
        project = getattr(view, "project", None)
        if isinstance(project, dict):
            value = project.get("id")
            return str(value) if value else None
        return None

    async def next_action(self, view: Any) -> Any:
        """Ask the model for one action, retrying malformed replies twice."""
        from ..agents.brain import Action
        from ..agents.tools import TOOLS, tools_for

        specs = tools_for(self.role)
        allowed = {spec.name for spec in specs} or set(TOOLS)
        if not self._history:
            self._history.append({"role": "user", "content": (
                f"当前状态（JSON）：\n{_compact_state(view)}\n\n"
                f"{self._step_hint(view)}请给出下一步：调用一个工具，或 finish。"
                "只输出一个 JSON 对象。")})
        else:
            board = _board_line(view)
            tail = [self._step_hint(view)]
            if board and board != self._board_sig:
                tail.append("当前任务看板（id|标题|状态|角色|负责人）：\n" + board)
            self._board_sig = board
            tail.append("请给出下一步（只输出一个 JSON 对象）。")
            if self._history[-1]["role"] == "user":
                self._history[-1]["content"] += "\n\n" + "\n".join(tail)
            else:
                self._history.append({"role": "user",
                                      "content": "\n".join(tail)})
        self._trim()

        legacy_tools = [{"name": spec.name, "description": spec.description,
                         "capability": spec.capability, "args": spec.args}
                        for spec in specs]
        native_tools = openai_tool_schemas(legacy_tools)
        native = supports_native_tools(self.provider)
        project_id = self._project_id(view)
        last_error = ""
        for _attempt in range(3):
            messages: list[dict] = ([{"role": "system", "content": self._system()}]
                                    + [dict(item) for item in self._history])
            result: LLMResult | None = None
            reply = ""
            try:
                if native:
                    result = await self.provider.chat_with_tools(
                        messages, tools=native_tools, role=self.role,
                        project_id=project_id)
                    reply = result.text or ""
                else:
                    reply = await self.provider.chat(messages,
                                                     tools=legacy_tools)
            except SfoError as exc:
                return Action(finish={
                    "summary": f"LLM 调用失败：{exc.code}: {exc.message}",
                    "status": "failed"})
            except Exception as exc:  # noqa: BLE001 - brain must not crash runner
                _log.warning("LLM chat failed: %s", exc)
                return Action(finish={
                    "summary": f"LLM 调用失败：{type(exc).__name__}",
                    "status": "failed"})

            calls = coerce_tool_calls(
                result.tool_calls if result is not None else None)
            if calls:
                call = calls[0]
                action, error = self._parse_tool_call(call, allowed)
                if action is not None:
                    self._remember_native_action(action, call, reply)
                    self._pending = action
                    self._pending_call_id = call.id or "call-0"
                    return action
                last_error = error
                messages.append({"role": "assistant",
                                 "content": _compact(reply, 500)})
                messages.append({"role": "user", "content": (
                    f"上一条工具调用无法使用：{error}。"
                    "请只调用可用工具，或输出一个合法 JSON 对象（tool 或 finish）。")})
                continue

            payload = _first_json_object(reply)
            action, error = self._parse(payload, allowed)
            if action is not None:
                self._remember_action(action, reply)
                self._pending = action
                self._pending_call_id = None
                return action
            last_error = error
            messages.append({"role": "assistant", "content": _compact(reply, 500)})
            messages.append({"role": "user", "content": (
                f"上一条回复无法解析：{error}。"
                "请只输出一个合法 JSON 对象（tool 或 finish）。")})
        return Action(finish={
            "summary": f"LLM 连续 3 次返回无法解析的内容：{last_error}",
            "status": "failed"})

    @staticmethod
    def _parse_tool_call(call: ToolCall, tools: Any) -> tuple[Any, str]:
        from ..agents.brain import Action

        name = internal_tool_name(call.name, tools)
        if not isinstance(name, str) or name not in tools:
            return None, f"未知工具 {call.name!r}（可用：{sorted(tools)}）"
        if not isinstance(call.arguments, dict):
            return None, "arguments 必须是对象"
        return Action(tool=name, args=dict(call.arguments)), ""

    @staticmethod
    def _parse(payload: dict | None, tools: Any) -> tuple[Any, str]:
        from ..agents.brain import Action

        if payload is None:
            return None, "没有找到 JSON 对象"
        if "tool" in payload:
            tool = payload.get("tool")
            if not isinstance(tool, str) or tool not in tools:
                return None, f"未知工具 {tool!r}（可用：{sorted(tools)}）"
            args = payload.get("args", {})
            if args is None:
                args = {}
            if not isinstance(args, dict):
                return None, "args 必须是对象"
            say = payload.get("say")
            return Action(tool=tool, args=args,
                          say=str(say) if isinstance(say, str) else ""), ""
        if "finish" in payload:
            finish = payload.get("finish")
            if not isinstance(finish, dict):
                return None, "finish 必须是对象"
            summary = finish.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                return None, "finish.summary 必须是非空字符串"
            status = str(finish.get("status") or "done")
            if status not in ("done", "failed", "blocked"):
                status = "done"
            return Action(finish={"summary": summary, "status": status}), ""
        return None, "缺少 tool 或 finish 字段"


__all__ = [
    "AnthropicProvider",
    "BaseProvider",
    "GeminiProvider",
    "LLMBrain",
    "LLMResult",
    "LLMProvider",
    "MockProvider",
    "OpenAICompatibleProvider",
    "Router",
    "StreamInterrupted",
    "ToolCall",
    "Usage",
    "anthropic_tools",
    "attach_runtime",
    "complete",
    "gemini_tools",
    "make_provider",
    "normalise_finish_reason",
    "openai_tool_schemas",
    "parse_tool_arguments",
    "supports_native_tools",
]


@runtime_checkable
class LLMProvider(Protocol):
    """Structural provider protocol kept for backwards-compatible imports."""

    async def chat(self, messages: list[dict], *,
                   tools: list[dict] | None = None) -> str:
        ...

    async def ping(self) -> dict:
        ...
