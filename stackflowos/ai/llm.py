"""LLM providers and the optional LLM-driven brain.

Providers use the OpenAI-compatible ``/chat/completions`` shape over stdlib
``urllib`` (no third-party dependencies).  ``MockProvider`` is deterministic and
offline so the whole system runs and tests without network.

``make_provider`` is the single construction point used by sys/hub; it accepts
any object with the :class:`stackflowos.config.LLMCfg` attributes.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..errors import BadRequest, SfoError, Unavailable
from ..util.logging import get_logger
from .prompts import SYSTEM_PROMPT_OS, role_prompt, tool_call_protocol

_log = get_logger("ai.llm")


_TRANSIENT_HTTP = ("LLM HTTP 429", "LLM HTTP 500", "LLM HTTP 502",
                   "LLM HTTP 503", "LLM HTTP 504")


def _is_transient(message: str) -> bool:
    return (message.startswith(_TRANSIENT_HTTP)
            or message.startswith("LLM connection failed")
            or message.startswith("LLM request failed"))


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal text-completion interface used by the agents."""

    async def chat(self, messages: list[dict], *,
                   tools: list[dict] | None = None) -> str:
        ...

    async def ping(self) -> dict:
        ...


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _content_text(content: Any) -> str:
    """Flatten OpenAI message content (string or multimodal parts) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return "" if content is None else str(content)


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user":
            return _content_text(message.get("content"))
    for message in reversed(messages or []):
        if isinstance(message, dict):
            text = _content_text(message.get("content"))
            if text:
                return text
    return ""


def _compact(text: str, limit: int = 120) -> str:
    cleaned = " ".join(str(text).split())
    return cleaned if len(cleaned) <= limit else cleaned[: max(0, limit - 1)] + "…"


def _redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


# ---------------------------------------------------------------------------
# mock provider
# ---------------------------------------------------------------------------

class MockProvider:
    """Deterministic, offline provider used by default (llm.provider=mock)."""

    name = "mock"

    async def chat(self, messages: list[dict], *,
                   tools: list[dict] | None = None) -> str:
        """Return a deterministic Chinese reply summarising the last user turn."""
        text = _last_user_text(messages)
        short = _compact(text, 140)
        lowered = text.lower()
        if any(keyword in lowered for keyword in
               ("状态", "进度", "status", "progress")):
            return (
                "当前系统运行正常：hub 在线，项目任务由各 Agent 在后台推进。"
                f"你问的是「{short}」。更细的进度可以在工作面板查看任务状态与事件流；"
                "我是本地 mock 助手，不会调用外部模型。")
        if any(keyword in lowered for keyword in ("审批", "approval", "权限")):
            return (
                f"收到：「{short}」。权限请求会出现在审批中心，"
                "低风险动作按策略自动放行，中高风险需要你确认（允许一次/一小时/总是允许/拒绝）。")
        return (
            f"收到：「{short}」。我是 StackFlowOS 的本地模拟助手"
            "（provider=mock），已记录你的输入；需要真实模型分析时，"
            "请在系统设置的 AI 一栏切换 provider 并填写 API 地址。")

    async def ping(self) -> dict:
        """Report provider health without any network call."""
        return {"ok": True, "provider": "mock"}


# ---------------------------------------------------------------------------
# OpenAI-compatible provider
# ---------------------------------------------------------------------------

class OpenAICompatibleProvider:
    """POST ``{base_url}/chat/completions`` via urllib in a worker thread."""

    name = "openai"

    def __init__(self, *, base_url: str, model: str, api_key: str = "",
                 temperature: float = 0.2, max_tokens: int = 4096,
                 timeout_s: float = 60.0, retries: int = 2,
                 retry_backoff_s: float = 0.5) -> None:
        if not base_url:
            raise BadRequest("llm.base_url is required for the openai provider")
        if not model:
            raise BadRequest("llm.model is required for the openai provider")
        self.base_url = str(base_url).rstrip("/")
        self.model = str(model)
        self.api_key = str(api_key or "")
        self.temperature = float(temperature)
        # Sent verbatim: the upstream endpoint/model is the authority on its
        # own limit. Do not silently rewrite an operator's setting.
        self.max_tokens = max(1, int(max_tokens))
        self.timeout_s = float(timeout_s)
        self.retries = max(0, int(retries))
        self.retry_backoff_s = max(0.0, float(retry_backoff_s))
        self._endpoint: str | None = None

    async def chat(self, messages: list[dict], *,
                   tools: list[dict] | None = None) -> str:
        """Send one chat completion request and return the assistant text."""
        if not isinstance(messages, list) or not messages:
            raise BadRequest("messages must be a non-empty list")
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max(1, int(self.max_tokens)),
        }
        # `tools` is accepted for interface parity; the JSON action protocol
        # keeps provider-native tool calling out of the critical path.
        _ = tools
        raw = await self._post_with_retry(body)
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise Unavailable("LLM returned non-JSON response") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise Unavailable("LLM response is missing choices[0].message.content") from exc
        text = _content_text(content)
        if not text:
            raise Unavailable("LLM returned an empty message")
        return text

    async def _post_with_retry(self, body: dict) -> str:
        """POST, retrying transient failures (429/5xx/timeouts/connection)."""
        for attempt in range(self.retries + 1):
            try:
                return await asyncio.to_thread(self._post, body)
            except Unavailable as exc:
                if attempt >= self.retries or not _is_transient(str(exc)):
                    raise
                delay = self.retry_backoff_s * (2 ** attempt)
                _log.info("LLM transient error (%s); retry %d/%d in %.1fs",
                          exc, attempt + 1, self.retries, delay)
                await asyncio.sleep(delay)
        raise Unavailable("LLM request failed")  # pragma: no cover

    def _candidate_urls(self) -> list[str]:
        """Plausible chat-completion URLs for the configured base URL.

        Operators paste ``http://host:port`` as often as ``.../v1``. Trying
        the conventional suffixes avoids a confusing 404 for a correct
        endpoint. The first working URL is cached for later calls.
        """
        base = self.base_url
        if base.endswith("/chat/completions"):
            urls = [base]
        elif base.endswith("/v1") or "/v1/" in base:
            urls = [base + "/chat/completions"]
        else:
            urls = [
                base + "/v1/chat/completions",
                base + "/chat/completions",
                base + "/api/chat/completions",
            ]
        if self._endpoint is not None and self._endpoint in urls:
            urls.remove(self._endpoint)
            urls.insert(0, self._endpoint)
        return urls

    def _post(self, body: dict) -> str:
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last_error: Unavailable | None = None
        for url in self._candidate_urls():
            request = Request(url, data=raw_body, headers=headers, method="POST")
            try:
                with urlopen(request, timeout=self.timeout_s) as response:
                    raw = response.read().decode("utf-8", "replace")
                self._endpoint = url
                return raw
            except HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read()[:300].decode("utf-8", "replace")
                except Exception:  # pragma: no cover - body may be unreadable
                    detail = ""
                detail = _redact(_compact(detail, 200), self.api_key)
                suffix = f": {detail}" if detail else ""
                error = Unavailable(f"LLM HTTP {exc.code}{suffix}")
                if exc.code in (404, 405) or (exc.code == 400 and "model" in detail.lower()):
                    # Wrong path (or a proxy that only accepts another one);
                    # try the next candidate. 400/model errors are not path
                    # problems, so surface them immediately.
                    if exc.code in (404, 405):
                        last_error = error
                        continue
                raise error from exc
            except URLError as exc:
                raise Unavailable(f"LLM connection failed: {exc.reason}") from exc
            except Exception as exc:  # socket timeout, malformed URL, ...
                raise Unavailable(
                    f"LLM request failed: {type(exc).__name__}") from exc
        if last_error is not None:
            raise last_error
        raise Unavailable("LLM request failed: no endpoint candidate")

    async def ping(self) -> dict:
        """Round-trip a tiny chat request and report latency."""
        started = time.monotonic()
        try:
            await self.chat([{"role": "user", "content": "ping"}])
        except SfoError as exc:
            return {"ok": False, "provider": self.name, "model": self.model,
                    "error": f"{exc.code}: {exc.message}"}
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "provider": self.name, "model": self.model,
                    "error": type(exc).__name__}
        return {
            "ok": True,
            "provider": self.name,
            "model": self.model,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------

def make_provider(cfg: Any) -> LLMProvider:
    """Build a provider from an ``LLMCfg``-like object."""
    provider = str(getattr(cfg, "provider", "mock") or "mock").strip().lower()
    if provider in ("", "mock"):
        return MockProvider()
    if provider in ("openai", "openai-compatible", "openai_compatible"):
        return OpenAICompatibleProvider(
            base_url=str(getattr(cfg, "base_url", "") or ""),
            model=str(getattr(cfg, "model", "") or ""),
            api_key=str(getattr(cfg, "api_key", "") or ""),
            temperature=float(getattr(cfg, "temperature", 0.2)),
            max_tokens=int(getattr(cfg, "max_tokens", 4096)),
            timeout_s=float(getattr(cfg, "timeout_s", 60.0)),
        )
    raise BadRequest(f"unsupported llm provider: {provider!r}")


# ---------------------------------------------------------------------------
# LLM-driven brain
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
        "step": getattr(view, "step", 0),
        "last_tool_results": [],
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
MAX_HISTORY_CHARS = 60_000
RESULT_STR_LIMIT = 6000
ACTION_STR_LIMIT = 1500


class LLMBrain:
    """Brain that asks an :class:`LLMProvider` for one JSON action per step.

    It keeps one conversation per agent run: the opening state, every action
    the model chose and every tool result (``observe``), so the model can read
    files, react to errors/denials and build on earlier steps like any
    ordinary tool-using agent.
    """

    def __init__(self, provider: LLMProvider, role: str, *,
                 max_steps: int = 16) -> None:
        self.provider = provider
        self.role = str(role)
        self.max_steps = int(max_steps)
        self._history: list[dict] = []
        self._pending: Any = None
        self._board_sig = ""
        self._dropped = 0

    async def chat(self, messages: list[dict]) -> str:
        """Delegate a plain chat turn to the provider."""
        return await self.provider.chat(messages)

    # -- conversation memory ---------------------------------------------
    def _system(self) -> str:
        return "\n\n".join((SYSTEM_PROMPT_OS, role_prompt(self.role),
                             tool_call_protocol(self.role)))

    def observe(self, action: Any, result: Any) -> None:
        """Record the tool result for the action returned by ``next_action``."""
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
        if len(text) > 12_000:
            text = text[:12_000] + "…[结果已截断]"
        self._history.append({"role": "user", "content": f"工具结果：{text}"})
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

    def _trim(self) -> None:
        """Drop the oldest assistant/user pairs (index 0 holds the opening)."""
        def size() -> int:
            return sum(len(item["content"]) for item in self._history)
        while len(self._history) > 3 and (
                len(self._history) > MAX_HISTORY_MESSAGES
                or size() > MAX_HISTORY_CHARS):
            del self._history[1:3]
            self._dropped += 1

    def _step_hint(self, view: Any) -> str:
        step = getattr(view, "step", 0)
        hint = f"步数：{step}/{self.max_steps}。"
        if self.max_steps and step >= self.max_steps - 2:
            hint += "步数即将用尽，请尽快 finish 并如实汇报进度。"
        if self._dropped:
            hint += f"（较早的 {self._dropped} 轮对话已省略。）"
        return hint

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

        tools = [{"name": spec.name, "description": spec.description,
                  "capability": spec.capability, "args": spec.args}
                 for spec in specs]
        messages: list[dict] = ([{"role": "system", "content": self._system()}]
                                + [dict(item) for item in self._history])
        last_error = ""
        for _attempt in range(3):
            try:
                reply = await self.provider.chat(messages, tools=tools)
            except SfoError as exc:
                return Action(finish={
                    "summary": f"LLM 调用失败：{exc.code}: {exc.message}",
                    "status": "failed"})
            except Exception as exc:  # noqa: BLE001 - brain must not crash runner
                _log.warning("LLM chat failed: %s", exc)
                return Action(finish={
                    "summary": f"LLM 调用失败：{type(exc).__name__}",
                    "status": "failed"})
            payload = _first_json_object(reply)
            action, error = self._parse(payload, allowed)
            if action is not None:
                self._remember_action(action, reply)
                self._pending = action
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
    "LLMBrain",
    "LLMProvider",
    "MockProvider",
    "OpenAICompatibleProvider",
    "make_provider",
]
