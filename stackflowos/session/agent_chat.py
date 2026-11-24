"""Per-agent conversation surface.

The system assistant (``session/chat.py``) is the human's proxy for the whole
OS.  This module adds the missing second half: **a conversation with one
roster agent**, opened from the Agent app.  A thread belongs to an agent id, so
the human can keep talking to the same worker identity after its process has
finished (the roster record is durable; the process is not).

A turn runs in the hub.  That is deliberate:

* it works for ``done``/``failed``/``dead`` agents without inventing a fake
  worker process or reviving an old task;
* it reuses the *same* role prompt, role tool set, workspace confinement,
  capability broker and approval queue as a worker;
* every step is appended to the same append-only event stream, so the audit
  story does not change.

Tools execute through :class:`stackflowos.agents.tools.ToolExecutor` with a
small in-process :class:`_AgentSys` adapter.  The adapter presents a synthetic
unit principal (``agent_id``, project and task scope) to
:meth:`stackflowos.app.App.handle_rpc_action`, so task/project scoping and
approval ownership are the same checks a worker process gets.  High-risk work
still parks in the human approval queue; the chat only gets to say it was
denied.

Event contract (SSE, ``source = "agent:<id>"``)::

    agent.chat.message   one complete user/assistant/system message
    agent.chat.delta     ephemeral typing/stream chunk (never persisted)
    agent.chat.tool      tool call state: running | ok | failed | denied
    agent.chat.state     busy true/false for one agent
    agent.chat.thread    thread cleared (messages=[])

Threads persist to ``<state_dir>/state/agent-chat/<agent_id>.json`` and are
bounded, so a long-running conversation cannot grow the state directory
without limit.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agents.models import Agent, now_ms
from ..agents.tools import ToolExecutor, ToolResult, tools_for
from ..ai.prompts import SYSTEM_PROMPT_OS, role_prompt
from ..errors import BadRequest, Conflict, SfoError
from ..security import Principal
from ..util.jsonio import read_json, write_json
from ..util.logging import get_logger

#: How much conversation one agent keeps on disk / in memory.
MAX_MESSAGES = 400
#: Prompt window: newest messages, bounded by count and characters.
MAX_PROMPT_MESSAGES = 30
MAX_PROMPT_CHARS = 26_000
#: One turn may make at most this many tool calls before it summarises.
MAX_TOOL_STEPS = 8
#: A single message may not exceed this many characters.
MAX_MESSAGE_CHARS = 8_000
#: Tool preview kept inside the persisted assistant message.
MAX_TOOL_PREVIEW = 500
#: How often a tool call's persisted preview is clipped when sent to the LLM.
MAX_TOOL_RESULT_CHARS = 7_000
#: Streaming chunk size (characters) and pause between chunks.
_STREAM_CHUNK = 72
_STREAM_DELAY_S = 0.01

_AGENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_SAFE_ARG_SKIP = {"content", "value", "patch"}


def _mid() -> str:
    return "acm-" + uuid.uuid4().hex[:10]


@dataclass
class AgentChatMessage:
    """One persisted message in an agent thread."""

    id: str
    role: str
    text: str
    ts: int
    agent_id: str
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "id": self.id,
            "role": self.role,
            "text": self.text,
            "ts": self.ts,
            "agent_id": self.agent_id,
        }
        if self.meta:
            out["meta"] = self.meta
        return out


def _short_args(args: Any, limit: int = 120) -> dict:
    """Argument preview safe for events and persisted tool cards."""
    out: dict[str, Any] = {}
    if not isinstance(args, dict):
        return out
    for key, value in args.items():
        if key in _SAFE_ARG_SKIP:
            continue
        if isinstance(value, str):
            text = " ".join(value.split())
            out[str(key)] = text if len(text) <= limit else text[:limit - 1] + "…"
        elif isinstance(value, (int, float, bool)) or value is None:
            out[str(key)] = value
    return out


def _clip(value: Any, limit: int, *, depth: int = 0) -> Any:
    """Recursively shorten strings so one tool result cannot flood the prompt."""
    if depth > 5:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {str(k): _clip(v, limit, depth=depth + 1)
                for k, v in list(value.items())[:80]}
    if isinstance(value, list):
        return [_clip(item, limit, depth=depth + 1)
                for item in value[:80]]
    return value


def _compact(text: Any, limit: int = 160) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


_TOOL_TAG = re.compile(
    r"<tool\s+name=[\"']([^\"']+)[\"']\s*>(.*?)</tool>", re.S)
_TOOL_CALL_TAG = re.compile(r"<tool_call>(.*?)</tool_call>", re.S)


def _action_from_payload(payload: Any) -> dict | None:
    """Normalise the three tool-call shapes local models actually emit."""
    if not isinstance(payload, dict):
        return None
    tool = payload.get("tool")
    args = payload.get("args")
    if not isinstance(tool, str) or not tool.strip():
        # OpenAI function-call shape: {"name": "...", "arguments": "{...}"}.
        tool = payload.get("name")
        args = payload.get("arguments", args)
        if not isinstance(tool, str) or not tool.strip():
            # {"function": {"name": "...", "arguments": {...}}}
            function = payload.get("function")
            if isinstance(function, dict):
                tool = function.get("name")
                args = function.get("arguments", args)
    if not isinstance(tool, str) or not tool.strip():
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = None
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None
    action: dict[str, Any] = {"tool": tool.strip(), "args": args}
    say = payload.get("say")
    if isinstance(say, str) and say.strip():
        action["say"] = _compact(say, 300)
    return action


#: DeepSeek's native tool tags are occasionally emitted even when the prompt
#: asks for JSON.  The token has unicode vertical bars around "DSML", so match
#: the tag shape rather than the exact spelling.
_DSML_INVOKE = re.compile(
    r"<[^>]*?DSML[^>]*?\s+invoke\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)"
    r"</[^>]*?DSML[^>]*?\s+invoke\s*>", re.S)
_DSML_PARAM = re.compile(
    r"<[^>]*?DSML[^>]*?\s+parameter\s+name=[\"']([^\"']+)[\"']"
    r"(?:\s+string=[\"'](true|false)[\"'])?[^>]*>(.*?)"
    r"</[^>]*?DSML[^>]*?\s+parameter\s*>", re.S)
_GENERIC_INVOKE = re.compile(
    r"<invoke\s+name=[\"']([^\"']+)[\"']\s*>(.*?)</invoke>", re.S)
_GENERIC_PARAM = re.compile(
    r"<parameter\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)</parameter>", re.S)


def _params_from_tags(body: str, pattern: re.Pattern) -> dict:
    args: dict[str, Any] = {}
    for match in pattern.finditer(body):
        name = match.group(1)
        raw_value = match.group(3) if pattern is _DSML_PARAM else match.group(2)
        string_flag = match.group(2) if pattern is _DSML_PARAM else None
        value: Any = raw_value.strip()
        if string_flag == "false" or (string_flag is None and str(value).startswith(("{", "["))):
            try:
                value = json.loads(str(value))
            except ValueError:
                pass
        args[str(name)] = value
    return args


def _action_from_native_tags(text: str) -> dict | None:
    """Parse DSML / Anthropic-style <invoke>/<parameter> tool tags."""
    for pattern, param_pattern in ((_DSML_INVOKE, _DSML_PARAM),
                                   (_GENERIC_INVOKE, _GENERIC_PARAM)):
        match = pattern.search(text)
        if match is None:
            continue
        name = match.group(1).strip()
        args = _params_from_tags(match.group(2), param_pattern)
        if not name:
            continue
        if "args" in args and isinstance(args["args"], dict):
            args = dict(args["args"])
        return {"tool": name, "args": args}
    return None


def _parse_action(raw: str) -> dict | None:
    """Parse a chat action from the model reply.

    The documented protocol is one JSON object.  Local models also wrap the
    call in a fenced block, an XML ``<tool name="...">...</tool>`` tag or an
    OpenAI function-call shape; all of those are accepted.  A normal answer
    that merely *mentions* JSON must never be executed as a tool call, so the
    reply has to be essentially just the call.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    fenced = re.fullmatch(r"```[a-zA-Z0-9_+-]*\s*\n?(.*?)\n?```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        action = _action_from_payload(payload)
        if action is not None:
            return action
    # <tool name="shell.exec">{"command": "..."}</tool>
    tagged = _TOOL_TAG.search(text)
    if tagged is not None:
        name = tagged.group(1).strip()
        body = tagged.group(2).strip()
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            payload = None
        if payload is None:
            return None
        if isinstance(payload, dict) and "args" in payload:
            candidate = _action_from_payload(payload)
            if candidate is not None:
                candidate["tool"] = name or candidate["tool"]
                return candidate
        # The tag name is authoritative; the body is the args object.
        if isinstance(payload, dict):
            return {"tool": name, "args": payload}
        return None
    # <tool_call>{"tool": "...", "args": {...}}</tool_call>
    call = _TOOL_CALL_TAG.search(text)
    if call is not None:
        body = call.group(1).strip()
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        return _action_from_payload(payload)
    # DeepSeek DSML / Anthropic <invoke> tool tags.
    return _action_from_native_tags(text)


class _AgentSys:
    """In-process ``SysAPI`` for one agent-chat turn.

    It routes through ``App.handle_rpc_action`` as a synthetic unit principal,
    so:
      * task/project scoping (M5) is enforced exactly as for a worker unit;
      * approval requests are owned by ``agent_id`` and can be claimed/polled;
      * ``event.append`` is stamped by the hub, not by us.
    """

    def __init__(self, app: Any, agent: Agent) -> None:
        self.app = app
        self.agent = agent
        self.src = f"agent:{agent.id}"
        self.principal = Principal(
            kind="unit",
            id=f"chat:{agent.id}",
            project_id=agent.project_id,
            role=agent.role,
            agent_id=agent.id,
            task_id=agent.task_id,
        )

    async def call(self, action: str, data: dict | None = None, *,
                   timeout: float | None = None) -> dict:
        # ``timeout`` is accepted for SysAPI parity: ToolExecutor passes it but
        # the in-process path needs no transport deadline.
        _ = timeout
        return await self.app.handle_rpc_action(
            action, dict(data or {}), src=self.src, external=True,
            principal=self.principal)


class AgentChatService:
    """Durable, per-agent conversations with a real tool loop."""

    def __init__(self, ctx: Any, eventlog: Any, *, app: Any, provider: Any,
                 state_dir: Path, logger: Any = None) -> None:
        self.ctx = ctx
        self.eventlog = eventlog
        self.app = app
        self.provider = provider
        self.root = Path(state_dir)
        self.dir = self.root / "state" / "agent-chat"
        self.log = logger or get_logger("agent_chat")
        self._threads: dict[str, list[AgentChatMessage]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        #: Live (in-flight) turn snapshot so a chat opened mid-turn can show
        #: the tool call that is currently waiting for approval.
        self._drafts: dict[str, dict] = {}
        #: When the current turn started, used to cancel only the approvals
        #: that belong to that turn (not a live worker's older request).
        self._turn_started: dict[str, int] = {}
        self._closed = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - read-only state dir
            self.log.warning("cannot create agent chat dir %s", self.dir)

    async def stop(self) -> None:
        self._closed = True
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        for agent_id in list(self._threads):
            self._save(agent_id)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def busy(self, agent_id: str) -> bool:
        task = self._tasks.get(str(agent_id))
        return task is not None and not task.done()

    def draft(self, agent_id: str) -> dict | None:
        """Snapshot of the running turn (tool cards / typing), if any."""
        value = self._drafts.get(str(agent_id))
        if not value:
            return None
        return {
            "message_id": value.get("message_id"),
            "typing": bool(value.get("typing")),
            "text": value.get("text") or "",
            "tools": [dict(item) for item in value.get("tools") or []],
        }

    def history(self, agent_id: str, limit: int = 200) -> list[dict]:
        thread = self._thread(agent_id)
        try:
            count = max(1, min(int(limit), MAX_MESSAGES))
        except (TypeError, ValueError):
            count = 200
        return [message.to_dict() for message in thread[-count:]]

    def thread_size(self, agent_id: str) -> int:
        return len(self._thread(agent_id))

    # ------------------------------------------------------------------
    # mutations
    # ------------------------------------------------------------------
    async def send_user(self, agent_id: str, text: str, *,
                        from_: str = "human:operator") -> dict:
        """Append a user message and start one reply turn.

        Only one turn per agent may run at a time: the Agent app disables the
        composer, and a second tab gets a clear 409 instead of two tool loops
        racing over the same workspace.
        """
        agent_id = self._validate_id(agent_id)
        agent = self.app.supervisor.get(agent_id)  # NotFound for unknown ids
        value = str(text or "").strip()
        if not value:
            raise BadRequest("text required")
        if len(value) > MAX_MESSAGE_CHARS:
            value = value[:MAX_MESSAGE_CHARS]
        if self.busy(agent_id):
            raise Conflict("该 Agent 正在回复上一条消息；请等待或先停止本轮。")

        message = AgentChatMessage(
            id=_mid(), role="user", text=value, ts=now_ms(),
            agent_id=agent_id, meta={"from": str(from_ or "human:operator")})
        self._append(agent_id, message)
        self._emit(agent, "agent.chat.message", message.to_dict())
        self._emit(agent, "agent.chat.state",
                    {"agent_id": agent_id, "busy": True})

        self._turn_started[agent_id] = now_ms()
        task = asyncio.create_task(self._reply(agent, message),
                                   name=f"agent-chat-{agent_id}")
        self._tasks[agent_id] = task

        def _done(finished: asyncio.Task, aid: str = agent_id) -> None:
            if self._tasks.get(aid) is finished:
                self._tasks.pop(aid, None)
                self._turn_started.pop(aid, None)
            if not finished.cancelled():
                try:
                    finished.exception()
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                fresh = self.app.supervisor.get(aid)
            except Exception:
                fresh = agent
            self._emit(fresh, "agent.chat.state",
                       {"agent_id": aid, "busy": False})

        task.add_done_callback(_done)
        # Touch the roster so the Agent list shows this agent as recently used.
        try:
            self.app.supervisor.update(agent_id, {"last_seen": now_ms()})
        except Exception:
            self.log.debug("could not touch agent %s", agent_id, exc_info=True)
        return message.to_dict()

    def cancel(self, agent_id: str) -> bool:
        """Ask the running turn (if any) to stop.  Returns True when cancelled.

        Pending approvals created by this turn are denied too: leaving them in
        the queue would show an actionable approval whose executor is gone.
        """
        agent_id = self._validate_id(agent_id)
        task = self._tasks.get(agent_id)
        if task is None or task.done():
            return False
        started = self._turn_started.get(agent_id)
        task.cancel()
        self._deny_turn_approvals(agent_id, started,
                                  "agent chat turn stopped")
        return True

    async def clear(self, agent_id: str) -> bool:
        """Stop the current turn (if any) and forget the thread."""
        agent_id = self._validate_id(agent_id)
        task = self._tasks.get(agent_id)
        started = self._turn_started.get(agent_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._deny_turn_approvals(agent_id, started, "agent chat cleared")
        self._turn_started.pop(agent_id, None)
        self._threads[agent_id] = []
        path = self._thread_path(agent_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort cleanup
            self.log.debug("could not delete thread file %s", path, exc_info=True)
        try:
            agent = self.app.supervisor.get(agent_id)
        except Exception:
            agent = None
        if agent is not None:
            self._emit(agent, "agent.chat.thread",
                       {"agent_id": agent_id, "cleared": True})
            self._emit(agent, "agent.chat.state",
                       {"agent_id": agent_id, "busy": False})
        return True

    def _deny_turn_approvals(self, agent_id: str, started: int | None,
                             reason: str) -> None:
        try:
            requests = self.app.approvals.list(status="pending")
        except Exception:
            return
        for request in requests:
            if str(getattr(request, "principal", "")) != agent_id:
                continue
            created = int(getattr(request, "created_at", 0) or 0)
            if started is not None and created < int(started):
                continue
            try:
                self.app.approvals.decide(
                    request.id, decision="deny", by="human:operator",
                    note=reason)
            except Exception:
                self.log.debug("could not deny chat approval %s",
                               getattr(request, "id", "?"), exc_info=True)

    # ------------------------------------------------------------------
    # one reply turn
    # ------------------------------------------------------------------
    async def _reply(self, agent: Agent, user_msg: AgentChatMessage) -> None:
        reply_id = _mid()
        tools_used: list[dict] = []
        self._drafts[agent.id] = {
            "message_id": reply_id, "typing": True, "text": "", "tools": [],
        }
        try:
            project = self._project(agent)
            executor = self._executor(agent, project)
            tools = self._tools(agent, project)
            messages = self._prompt_messages(agent, project, tools,
                                             user_msg.text)
            await self._typing(agent, reply_id, True)
            answer, tools_used = await self._tool_loop(
                agent, project, messages, tools, executor, reply_id)
            answer = (answer or "").strip() or "（模型没有返回内容）"
            await self._typing(agent, reply_id, False)
            await self._stream(agent, reply_id, answer)
            message = AgentChatMessage(
                id=reply_id, role="assistant", text=answer, ts=now_ms(),
                agent_id=agent.id,
                meta={"tools": tools_used[:20]} if tools_used else {})
            self._append(agent.id, message)
            self._emit(agent, "agent.chat.message", message.to_dict())
        except asyncio.CancelledError:
            await self._typing(agent, reply_id, False)
            note = AgentChatMessage(
                id=_mid(), role="system", text="（本轮已停止）", ts=now_ms(),
                agent_id=agent.id, meta={"reason": "cancelled"})
            self._append(agent.id, note)
            self._emit(agent, "agent.chat.message", note.to_dict())
            raise
        except Exception as exc:  # noqa: BLE001 - a bad turn must not kill the hub
            self.log.exception("agent chat reply failed for %s", agent.id)
            await self._typing(agent, reply_id, False)
            text = (f"这轮对话失败了：{type(exc).__name__}: {exc}".strip()
                    + "\n\n可以重发一次，或查看事件流里的错误记录。")
            try:
                await self._stream(agent, reply_id, text)
            except Exception:  # pragma: no cover - event log unavailable
                pass
            message = AgentChatMessage(
                id=reply_id, role="assistant", text=text, ts=now_ms(),
                agent_id=agent.id, meta={"error": type(exc).__name__})
            try:
                self._append(agent.id, message)
                self._emit(agent, "agent.chat.message", message.to_dict())
            except Exception:  # pragma: no cover - never mask the original error
                self.log.exception("could not persist failed chat reply")
        finally:
            self._drafts.pop(agent.id, None)

    async def _tool_loop(self, agent: Agent, project: Any,
                         messages: list[dict], tools: list[Any],
                         executor: ToolExecutor | None,
                         reply_id: str) -> tuple[str, list[dict]]:
        """Run the LLM/tool loop until the model answers in natural language."""
        allowed = {spec.name for spec in tools}
        specs = [{"name": spec.name, "description": spec.description,
                  "args": spec.args, "capability": spec.capability}
                 for spec in tools]
        records: list[dict] = []
        bad_format = 0
        if executor is None:
            return await self._plain_reply(messages), records

        for _step in range(MAX_TOOL_STEPS):
            try:
                raw = await self.provider.chat(messages, tools=specs or None)
            except SfoError as exc:
                return f"模型调用失败：{exc.code}: {exc.message}", records
            except Exception as exc:  # noqa: BLE001 - report, do not crash
                self.log.exception("provider chat failed for %s", agent.id)
                return f"模型调用失败：{type(exc).__name__}: {exc}", records
            text = str(raw or "").strip()
            if not text:
                return "（模型没有返回内容）", records
            action = _parse_action(text)
            if action is not None and action["tool"] in allowed:
                await self._typing(agent, reply_id, False)
                record = self._run_tool_record(agent, action, reply_id)
                records.append(record)
                self._draft_upsert_tool(agent.id, record)
                await self._emit_tool(agent, reply_id, record)
                try:
                    result = await executor.run(action["tool"], action["args"])
                except Exception as exc:  # noqa: BLE001 - tool bug is data
                    result = ToolResult(ok=False,
                                        error=f"{type(exc).__name__}: {exc}")
                self._finish_tool_record(record, result)
                self._draft_upsert_tool(agent.id, record)
                await self._emit_tool(agent, reply_id, record)
                messages.append({"role": "assistant", "content": text[:4000]})
                messages.append({"role": "user", "content": (
                    "工具结果（" + action["tool"] + "）："
                    + json.dumps(self._result_payload(result),
                                 ensure_ascii=False, default=str))})
                await self._typing(agent, reply_id, True)
                bad_format = 0
                continue
            if action is not None:
                # Valid JSON but a tool outside this role's set.  Tell the model
                # instead of pretending it ran.
                messages.append({"role": "assistant", "content": text[:2000]})
                messages.append({"role": "user", "content": (
                    f"工具 {action['tool']!r} 不在你的角色工具集里。"
                    f"可用工具：{', '.join(sorted(allowed)) or '无'}。"
                    "请改用可用工具，或直接用自然语言回答。")})
                bad_format += 1
                if bad_format >= 3:
                    return ("我没能选到合适的工具，先停在这里。"
                            "可以换一种说法，或告诉我要检查/修改的具体文件。"), records
                continue
            if text.startswith("{"):
                # A malformed action attempt.  Nudge once or twice, then treat
                # it as prose rather than exposing raw JSON to the human.
                bad_format += 1
                messages.append({"role": "assistant", "content": text[:2000]})
                messages.append({"role": "user", "content": (
                    "上一条不是合法的动作 JSON。需要动手时只输出："
                    '{"tool": "工具名", "args": {...}}；'
                    "不需要动手就直接用中文回答。")})
                if bad_format >= 2:
                    return ("我这次没有组织好动作，先停在这里。"
                            "可以再说一次你想让我做的事。"), records
                continue
            return text, records
        return ("我已经连续执行了 "
                f"{MAX_TOOL_STEPS} 步工具调用，先停在这里。"
                "你可以让我继续，或指定下一步。"), records

    async def _plain_reply(self, messages: list[dict]) -> str:
        try:
            raw = await self.provider.chat(messages)
        except SfoError as exc:
            return f"模型调用失败：{exc.code}: {exc.message}"
        except Exception as exc:  # noqa: BLE001
            self.log.exception("provider chat failed")
            return f"模型调用失败：{type(exc).__name__}: {exc}"
        return str(raw or "").strip() or "（模型没有返回内容）"

    def _run_tool_record(self, agent: Agent, action: dict,
                         reply_id: str) -> dict:
        return {
            "id": "call-" + uuid.uuid4().hex[:8],
            "message_id": reply_id,
            "tool": action["tool"],
            "args": _short_args(action.get("args") or {}),
            "state": "running",
            "ts": now_ms(),
        }

    def _finish_tool_record(self, record: dict, result: ToolResult) -> None:
        error = str(result.error or "")
        if result.ok:
            state = "ok"
        elif error.startswith("DENIED"):
            state = "denied"
        else:
            state = "failed"
        record["state"] = state
        record["ok"] = bool(result.ok)
        if result.preview:
            record["preview"] = str(result.preview)[:MAX_TOOL_PREVIEW]
        if result.ok:
            output = self._tool_output(result)
            if output:
                record["output"] = output
        if error:
            record["error"] = _compact(error, 600)
        record["finished_at"] = now_ms()

    @staticmethod
    def _tool_output(result: ToolResult, limit: int = 1200) -> str:
        """A short, human-useful rendering of a tool result for the tool card."""
        data = result.data if isinstance(result.data, dict) else {}
        for key in ("stdout", "content"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                text = value if len(value) <= limit else value[:limit] + "…"
                return text
        entries = data.get("entries")
        if isinstance(entries, list):
            lines = []
            for item in entries[:60]:
                if not isinstance(item, dict):
                    continue
                suffix = "/" if item.get("type") == "dir" else ""
                size = item.get("size")
                lines.append(f"{item.get('name')}{suffix}"
                             + (f"  ({size} B)" if isinstance(size, int) else ""))
            if lines:
                return "\n".join(lines)[:limit]
        if data:
            try:
                return json.dumps(data, ensure_ascii=False, default=str)[:limit]
            except (TypeError, ValueError):
                return ""
        return ""

    @staticmethod
    def _result_payload(result: ToolResult) -> dict:
        payload: dict[str, Any] = {"ok": bool(result.ok)}
        if result.ok:
            payload["data"] = _clip(result.data, MAX_TOOL_RESULT_CHARS)
        else:
            payload["error"] = _compact(result.error, 1200)
        return payload

    # ------------------------------------------------------------------
    # prompt construction
    # ------------------------------------------------------------------
    def _tools(self, agent: Agent, project: Any) -> list[Any]:
        if project is None:
            # Without a project there is no workspace to confine fs/shell to.
            return []
        return list(tools_for(agent.role))

    def _prompt_messages(self, agent: Agent, project: Any, tools: list[Any],
                         user_text: str) -> list[dict]:
        system = self._system_prompt(agent, project, tools)
        thread = self._thread(agent.id)
        history: list[dict] = []
        for message in thread:
            if message.role not in ("user", "assistant"):
                continue
            text = message.text
            if message.role == "assistant" and message.meta.get("tools"):
                names = [str(item.get("tool") or "") for item in
                         message.meta["tools"] if isinstance(item, dict)]
                if names:
                    text = (text + "\n（上一轮已使用工具："
                            + ", ".join(names[:8]) + "）")
            history.append({"role": message.role, "content": text})
        # The current user message was appended by ``send_user``; keep exactly
        # one copy even if a retry re-enters this path.
        if (not history or history[-1].get("role") != "user"
                or history[-1].get("content") != user_text):
            history.append({"role": "user", "content": user_text})
        history = self._trim_history(history, user_text)
        return [{"role": "system", "content": system}, *history]

    @staticmethod
    def _trim_history(history: list[dict], user_text: str) -> list[dict]:
        """Drop oldest turns until the prompt fits the count/char budget."""
        selected: list[dict] = []
        used = 0
        for item in reversed(history):
            content = str(item.get("content") or "")
            if content:
                content = content[:6000]
            if selected and (len(selected) >= MAX_PROMPT_MESSAGES
                             or used + len(content) > MAX_PROMPT_CHARS):
                break
            selected.append({"role": item["role"], "content": content})
            used += len(content)
        selected.reverse()
        if not selected or selected[-1].get("role") != "user":
            selected.append({"role": "user", "content": user_text})
        return selected

    def _system_prompt(self, agent: Agent, project: Any,
                       tools: list[Any]) -> str:
        context = self._context(agent, project)
        if project is None:
            tool_text = ("你没有绑定项目，因此没有文件/命令工具；"
                         "只回答问题、给出建议，不要声称执行过工作。")
        else:
            lines = ["可用工具（只能从这里选，禁止编造）："]
            for spec in tools:
                args = ", ".join((spec.args or {}).keys()) or "无"
                lines.append(f"- {spec.name}({args})：{spec.description}"
                             f"（{spec.capability}）")
            tool_text = "\n".join(lines)
        read_only_note = ""
        if project is not None and (
                getattr(project, "status", "") == "archived"
                or getattr(project, "workspace_mode", "") == "attached-ro"):
            read_only_note = ("项目当前是只读/已归档：可以读取文件，但不能写文件、"
                              "删除文件或执行命令。\n")
        return "\n\n".join((
            SYSTEM_PROMPT_OS,
            role_prompt(agent.role),
            "你现在在「Agent 对话」界面里，直接和操作员（人类）协作，"
            "不是在执行一次性任务。\n"
            "对话规则：\n"
            "- 默认用中文、简洁地回答；回答前先核对下面的实时上下文，"
            "不要编造状态、文件或命令输出。\n"
            "- 当操作员要求检查、修改、运行、创建内容时，先调用工具，"
            "不要只描述步骤；工具结果会作为下一条消息发给你，你可以继续调用，"
            "最后用自然语言总结（引用真实路径与结果）。\n"
            "- 需要动手时，只输出一个 JSON 对象："
            '{"tool": "工具名", "args": {...}}；不要加 Markdown 代码块，'
            "不要一次输出多个 JSON。\n"
            "- 不要使用 DSML、XML、<tool>、<invoke> 或函数调用标签；"
            "系统只认 JSON 动作对象。\n"
            "- 每次只调用一个工具；文件路径只能位于项目 workspace 内。\n"
            "- 中高风险动作会进入审批，等待操作员放行；被拒绝时如实说明，"
            "不要换路径或命令绕过。\n"
            "- 不要使用 finish 协议；不需要动手时直接用自然语言回答。\n"
            "- 如果超出你的角色/工具范围，说明限制并给出最接近的可执行选项。\n"
            + read_only_note,
            tool_text,
            "实时上下文（JSON，只包含本次对话需要的摘要）：\n"
            + json.dumps(context, ensure_ascii=False, default=str),
        ))

    def _context(self, agent: Agent, project: Any) -> dict:
        tasks: list[dict] = []
        current_task: dict | None = None
        workspace = ""
        files: list[str] = []
        events: list[dict] = []
        project_info: dict[str, Any] = {}
        if project is not None:
            workspace = str(getattr(project, "workspace", "") or "")
            try:
                cards = self.app.projects.task_list(project_id=agent.project_id)
            except Exception:
                cards = []
            for task in cards:
                try:
                    row = task.to_dict()
                except Exception:
                    continue
                tasks.append({
                    "id": row.get("id"),
                    "title": _compact(row.get("title"), 100),
                    "status": row.get("status"),
                    "role": row.get("role"),
                    "assignee": row.get("assignee"),
                })
                if row.get("id") == agent.task_id:
                    current_task = {
                        "id": row.get("id"),
                        "title": _compact(row.get("title"), 160),
                        "description": _compact(row.get("description"), 800),
                        "status": row.get("status"),
                        "artifacts": [
                            {"path": item.get("path"), "type": item.get("type")}
                            for item in (row.get("artifacts") or [])[:8]
                            if isinstance(item, dict)
                        ],
                    }
            files = self._workspace_listing(workspace)
            try:
                records = self.app.eventlog.query(
                    project_id=agent.project_id, limit=8)
            except Exception:
                records = []
            for record in records:
                events.append({
                    "type": getattr(record, "type", ""),
                    "source": getattr(record, "source", ""),
                    "data": _compact(getattr(record, "data", {}), 180),
                })
            try:
                card = self.app.project_card(agent.project_id)
            except Exception:
                card = {}
            project_info = {
                "id": card.get("id"),
                "name": card.get("name"),
                "goal": _compact(card.get("goal"), 600),
                "brief": _compact(card.get("brief"), 800),
                "status": card.get("status"),
                "progress": card.get("progress"),
                "counts": card.get("counts") or {},
                "workspace": workspace,
                "workspace_mode": card.get("workspace_mode"),
            }
        return {
            "now": now_ms(),
            "agent": {
                "id": agent.id,
                "role": agent.role,
                "display_name": agent.display_name,
                "status": agent.status,
                "task_id": agent.task_id,
                "summary": _compact(agent.summary, 600),
                "error": _compact(agent.error, 300),
            },
            "project": project_info,
            "current_task": current_task,
            "tasks": tasks[:16],
            "workspace_files": files,
            "recent_events": events,
            "model": str(getattr(self.provider, "model", "") or ""),
        }

    @staticmethod
    def _workspace_listing(workspace: str, limit: int = 28) -> list[str]:
        if not workspace:
            return []
        root = Path(workspace)
        try:
            entries = sorted(root.iterdir(),
                             key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return []
        out: list[str] = []
        for entry in entries[:limit]:
            if entry.name.startswith("."):
                continue
            try:
                suffix = "/" if entry.is_dir() else ""
            except OSError:
                suffix = ""
            out.append(entry.name + suffix)
        return out

    # ------------------------------------------------------------------
    # project / executor
    # ------------------------------------------------------------------
    def _project(self, agent: Agent) -> Any:
        if not agent.project_id:
            return None
        try:
            return self.app.projects.get(agent.project_id)
        except Exception:
            return None

    def _executor(self, agent: Agent, project: Any) -> ToolExecutor | None:
        if project is None:
            return None
        workspace = Path(str(getattr(project, "workspace", "") or ""))
        # ToolExecutor resolves a relative path against the hub's cwd.  A
        # malformed/legacy project record must not turn into "run in the hub".
        try:
            if not workspace.is_absolute() or not workspace.is_dir():
                return None
        except OSError:
            return None
        read_only = (getattr(project, "workspace_mode", "") == "attached-ro"
                     or getattr(project, "status", "") == "archived")
        paths = self.app.paths
        deny = [paths.config_file, paths.state_dir / "state", paths.events_dir,
                paths.logs_dir, paths.run_dir, paths.tmp_dir]
        return ToolExecutor(
            _AgentSys(self.app, agent), agent=agent, workspace=workspace,
            security_config=self.app.config.security,
            logger=get_logger("agent_chat.tools"),
            event_source=f"agent:{agent.id}", read_only=read_only,
            deny_paths=deny)

    # ------------------------------------------------------------------
    # events / streaming
    # ------------------------------------------------------------------
    def _emit(self, agent: Agent, type: str, data: dict) -> None:
        payload = dict(data or {})
        payload.setdefault("agent_id", agent.id)
        try:
            self.eventlog.append(
                type, payload, source=f"agent:{agent.id}",
                project_id=agent.project_id)
        except Exception:  # pragma: no cover - telemetry is best effort
            self.log.debug("cannot emit %s", type, exc_info=True)

    def _draft_upsert_tool(self, agent_id: str, record: dict) -> None:
        draft = self._drafts.get(agent_id)
        if draft is None:
            return
        item = {key: value for key, value in record.items()
                if key != "message_id"}
        tools = draft.setdefault("tools", [])
        for index, existing in enumerate(tools):
            if existing.get("id") == item.get("id"):
                tools[index] = item
                return
        tools.append(item)

    async def _typing(self, agent: Agent, message_id: str, on: bool) -> None:
        draft = self._drafts.get(agent.id)
        if draft is not None:
            draft["typing"] = bool(on)
        self._emit(agent, "agent.chat.delta", {
            "message_id": message_id,
            "delta": "",
            "role": "assistant",
            "typing": bool(on),
        })
        await asyncio.sleep(0)

    async def _stream(self, agent: Agent, message_id: str, text: str) -> None:
        for index in range(0, len(text), _STREAM_CHUNK):
            if self._closed:
                return
            chunk = text[index:index + _STREAM_CHUNK]
            draft = self._drafts.get(agent.id)
            if draft is not None:
                draft["text"] = str(draft.get("text") or "") + chunk
                draft["typing"] = False
            self._emit(agent, "agent.chat.delta", {
                "message_id": message_id,
                "delta": chunk,
                "role": "assistant",
            })
            await asyncio.sleep(_STREAM_DELAY_S)

    async def _emit_tool(self, agent: Agent, message_id: str,
                         record: dict) -> None:
        payload = {key: value for key, value in record.items()
                   if key != "message_id"}
        payload["message_id"] = message_id
        self._emit(agent, "agent.chat.tool", payload)
        await asyncio.sleep(0)

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _validate_id(self, agent_id: str) -> str:
        value = str(agent_id or "").strip()
        if not _AGENT_ID.fullmatch(value):
            raise BadRequest("invalid agent id")
        return value

    def _thread_path(self, agent_id: str) -> Path:
        return self.dir / f"{agent_id}.json"

    def _thread(self, agent_id: str) -> list[AgentChatMessage]:
        agent_id = self._validate_id(agent_id)
        thread = self._threads.get(agent_id)
        if thread is None:
            thread = self._load(agent_id)
            self._threads[agent_id] = thread
        return thread

    def _load(self, agent_id: str) -> list[AgentChatMessage]:
        path = self._thread_path(agent_id)
        try:
            data = read_json(path, default=None)
        except ValueError:
            self.log.warning("ignoring corrupt agent chat thread %s", path,
                             exc_info=True)
            return []
        if not isinstance(data, dict):
            return []
        messages: list[AgentChatMessage] = []
        for raw in (data.get("messages") or [])[-MAX_MESSAGES:]:
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role") or "")
            if role not in ("user", "assistant", "system"):
                continue
            text = str(raw.get("text") or "")
            if not text:
                continue
            meta = raw.get("meta")
            messages.append(AgentChatMessage(
                id=str(raw.get("id") or _mid()),
                role=role,
                text=text[:MAX_MESSAGE_CHARS],
                ts=int(raw.get("ts") or now_ms()),
                agent_id=agent_id,
                meta=dict(meta) if isinstance(meta, dict) else {},
            ))
        return messages

    def _append(self, agent_id: str, message: AgentChatMessage) -> None:
        thread = self._thread(agent_id)
        for index, existing in enumerate(thread):
            if existing.id == message.id:
                thread[index] = message
                self._save(agent_id)
                return
        thread.append(message)
        if len(thread) > MAX_MESSAGES:
            del thread[: len(thread) - MAX_MESSAGES]
        self._save(agent_id)

    def _save(self, agent_id: str) -> None:
        payload = {
            "agent_id": agent_id,
            "updated_at": now_ms(),
            "messages": [message.to_dict() for message in
                         self._threads.get(agent_id, [])],
        }
        try:
            write_json(self._thread_path(agent_id), payload)
        except (OSError, ValueError):
            self.log.warning("cannot save agent chat thread %s", agent_id,
                             exc_info=True)


__all__ = ["AgentChatMessage", "AgentChatService"]
