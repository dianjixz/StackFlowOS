"""LLM provider implementations (stdlib only).

Wire formats
------------
* :class:`OpenAICompatibleProvider` -- ``POST /chat/completions`` with native
  ``tools``/``tool_calls`` and OpenAI SSE ``data:`` streaming.
* :class:`AnthropicProvider` -- ``POST /v1/messages`` with ``x-api-key`` +
  ``anthropic-version``, ``input_schema`` tools and ``tool_use`` content blocks.
* :class:`GeminiProvider` -- ``POST :generateContent`` with
  ``functionDeclarations``/``functionCall`` and SSE (``alt=sse``).
* :class:`MockProvider` -- deterministic offline provider, scriptable from
  tests.

All providers expose the same small interface:

``chat(messages, tools=None) -> str`` (legacy)
``chat_with_tools(messages, tools=None) -> LLMResult``
``stream(messages, tools=None) -> async iterator of delta dicts``

Deltas are ``{"type": "text"|"tool_call"|"usage"|"finish", ...}``; the stream
aggregation helper turns them back into an :class:`LLMResult` for callers that
only care about the final answer.

Network calls run in worker threads (``asyncio.to_thread`` for request/response,
a reader thread + ``asyncio.Queue`` for SSE) so the hub event loop never blocks.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from typing import Any, AsyncIterator, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..errors import BadRequest, SfoError, Unavailable
from ..util.logging import get_logger
from .context import estimate_tools_tokens, trim_messages
from .limits import CircuitBreaker, RateLimited, RateLimiter
from .telemetry import (
    BudgetExceeded,
    estimate_messages_tokens,
    estimate_tokens,
    get_runtime,
)
from .types import LLMResult, ToolCall, Usage, parse_tool_arguments

_log = get_logger("ai.llm")

_TRANSIENT_PREFIXES = (
    "LLM HTTP 429", "LLM HTTP 500", "LLM HTTP 502", "LLM HTTP 503",
    "LLM HTTP 504", "LLM connection failed", "LLM request failed",
)


def _is_transient(message: str) -> bool:
    return str(message or "").startswith(_TRANSIENT_PREFIXES)


def _content_text(content: Any) -> str:
    """Flatten OpenAI/Anthropic message content (string or parts) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "tool_result":
                    parts.append(_content_text(item.get("content")))
        return "\n".join(parts)
    return "" if content is None else str(content)


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "user":
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


def normalise_finish_reason(reason: Any, *, has_tools: bool = False) -> str:
    value = str(reason or "").strip().lower()
    mapping = {
        "end_turn": "stop", "stop_sequence": "stop", "stop": "stop",
        "tool_use": "tool_calls", "tool_calls": "tool_calls",
        "function_call": "tool_calls",
        "max_tokens": "length", "length": "length",
        "content_filter": "content_filter", "safety": "content_filter",
        "recitation": "content_filter",
    }
    normalised = mapping.get(value, value or ("tool_calls" if has_tools else "stop"))
    if normalised == "stop" and has_tools:
        return "tool_calls"
    return normalised


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


# ---------------------------------------------------------------------------
# tool schema conversion
# ---------------------------------------------------------------------------

_JSON_TYPES = {
    "str": "string", "string": "string",
    "int": "integer", "integer": "integer",
    "float": "number", "number": "number",
    "bool": "boolean", "boolean": "boolean",
    "dict": "object", "object": "object",
    "list": "array", "array": "array",
}


def _tool_parts(tool: Any) -> tuple[str, str, dict]:
    """Return ``(name, description, json_schema_parameters)`` for any spec."""
    if not isinstance(tool, dict):
        return "", "", {"type": "object", "properties": {}}
    if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
        function = tool["function"]
        name = str(function.get("name") or "")
        description = str(function.get("description") or "")
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        return name, description, parameters
    name = str(tool.get("name") or "")
    description = str(tool.get("description") or "")
    raw_args = tool.get("args")
    if isinstance(raw_args, dict):
        properties: dict[str, dict] = {}
        required: list[str] = []
        for arg_name, kind in raw_args.items():
            properties[str(arg_name)] = {
                "type": _JSON_TYPES.get(str(kind).lower(), "string")}
            required.append(str(arg_name))
        parameters = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        return name, description, parameters
    parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    return name, description, parameters


def openai_tool_schemas(tools: Any) -> list[dict]:
    """Convert internal tool specs to the OpenAI ``tools`` array."""
    out: list[dict] = []
    for tool in tools or []:
        name, description, parameters = _tool_parts(tool)
        if not name:
            continue
        out.append({"type": "function",
                    "function": {"name": name,
                                 "description": description,
                                 "parameters": parameters}})
    return out


def anthropic_tools(tools: Any) -> list[dict]:
    out: list[dict] = []
    for tool in tools or []:
        name, description, parameters = _tool_parts(tool)
        if not name:
            continue
        out.append({"name": name, "description": description,
                    "input_schema": parameters})
    return out


def gemini_tools(tools: Any) -> list[dict]:
    declarations: list[dict] = []
    for tool in tools or []:
        name, description, parameters = _tool_parts(tool)
        if not name:
            continue
        declarations.append({"name": name, "description": description,
                             "parameters": parameters})
    return [{"functionDeclarations": declarations}] if declarations else []


# ---------------------------------------------------------------------------
# base provider
# ---------------------------------------------------------------------------

class BaseProvider:
    """Shared HTTP, retry, usage, limiter, breaker and streaming machinery."""

    name = "base"
    default_base_url = ""

    def __init__(self, *, base_url: str = "", model: str = "",
                 api_key: str = "", temperature: float = 0.2,
                 max_tokens: int = 4096, timeout_s: float = 60.0,
                 retries: int = 2, retry_backoff_s: float = 0.5,
                 stream: bool = True, rpm: int = 0, tpm: int = 0,
                 max_concurrency: int = 8, max_context_tokens: int = 128_000,
                 project_token_budget: int = 0,
                 price_per_1k_input: float = 0.0,
                 price_per_1k_output: float = 0.0,
                 circuit_threshold: int = 3,
                 circuit_cooldown_s: float = 30.0) -> None:
        self.base_url = str(base_url or self.default_base_url).rstrip("/")
        self.model = str(model or "")
        self.api_key = str(api_key or "")
        self.temperature = float(temperature)
        self.max_tokens = max(1, int(max_tokens))
        self.timeout_s = max(0.1, float(timeout_s))
        self.retries = max(0, int(retries))
        self.retry_backoff_s = max(0.0, float(retry_backoff_s))
        self.stream_enabled = bool(stream)
        self.rpm = max(0, int(rpm or 0))
        self.tpm = max(0, int(tpm or 0))
        self.max_concurrency = max(1, int(max_concurrency or 1))
        self.max_context_tokens = max(1024, int(max_context_tokens or 128_000))
        self.project_token_budget = max(0, int(project_token_budget or 0))
        self.price_per_1k_input = max(0.0, float(price_per_1k_input or 0.0))
        self.price_per_1k_output = max(0.0, float(price_per_1k_output or 0.0))
        self.runtime = get_runtime()
        self.limiter = RateLimiter(rpm=self.rpm, tpm=self.tpm,
                                   max_concurrency=self.max_concurrency)
        self.breaker = CircuitBreaker(threshold=circuit_threshold,
                                      cooldown_s=circuit_cooldown_s)
        self._endpoint: str | None = None
        self._stats: dict[str, Any] = {
            "calls": 0, "failures": 0, "last_latency_ms": None,
            "last_error": "", "last_used_at": None,
        }

    # -- lifecycle/inspection --------------------------------------------
    def attach_runtime(self, runtime: Any) -> None:
        self.runtime = runtime

    def health(self) -> dict:
        return {
            "ok": self.breaker.state != "open",
            "provider": self.name,
            "model": self.model,
            "circuit": self.breaker.snapshot(),
            "limiter": self.limiter.snapshot(),
            **{key: self._stats.get(key) for key in
               ("calls", "failures", "last_latency_ms", "last_error",
                "last_used_at")},
        }

    def describe(self) -> dict:
        return {
            "name": self.name,
            "provider": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "stream": self.stream_enabled,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "limits": self.limiter.snapshot(),
            "health": self.health(),
        }

    def usage_snapshot(self, project_id: str | None = None) -> dict:
        return self.runtime.usage.snapshot(project_id)

    # -- public interface -------------------------------------------------
    async def chat(self, messages: list[dict], *,
                   tools: list[dict] | None = None,
                   on_delta: Callable[[dict], Any] | None = None,
                   role: str | None = None,
                   project_id: str | None = None) -> str:
        """Legacy text interface; internally uses native tool calling."""
        # ``tools`` is accepted for interface parity.  Legacy callers speak the
        # JSON action protocol, so native schemas are not sent here.
        _ = tools
        result = await self.chat_with_tools(
            messages, tools=None, role=role, project_id=project_id,
            on_delta=on_delta)
        if not result.text and not result.tool_calls:
            raise Unavailable("LLM returned an empty message")
        return result.text

    async def chat_with_tools(
            self, messages: list[dict], tools: list[dict] | None = None, *,
            tool_choice: Any = None,
            role: str | None = None,
            project_id: str | None = None,
            on_delta: Callable[[dict], Any] | None = None,
            stream: bool | None = None) -> LLMResult:
        self._validate_messages(messages)
        prepared, prompt_estimate = self._prepare(messages, tools)
        self._check_budget(project_id, prompt_estimate)
        use_stream = self.stream_enabled if stream is None else bool(stream)
        if use_stream and on_delta is not None:
            return await self._collect_stream(
                prepared, tools, tool_choice=tool_choice, role=role,
                project_id=project_id, on_delta=on_delta,
                prompt_estimate=prompt_estimate)
        return await self._request_nonstream(
            prepared, tools, tool_choice=tool_choice, role=role,
            project_id=project_id, prompt_estimate=prompt_estimate)

    async def stream(self, messages: list[dict],
                     tools: list[dict] | None = None, *,
                     tool_choice: Any = None,
                     role: str | None = None,
                     project_id: str | None = None
                     ) -> AsyncIterator[dict]:
        """Yield incremental deltas (text / tool_call / usage / finish)."""
        self._validate_messages(messages)
        prepared, prompt_estimate = self._prepare(messages, tools)
        self._check_budget(project_id, prompt_estimate)
        async for delta in self._stream_deltas(
                prepared, tools, tool_choice=tool_choice, role=role,
                project_id=project_id, prompt_estimate=prompt_estimate):
            yield delta

    async def ping(self) -> dict:
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

    # -- preparation ------------------------------------------------------
    @staticmethod
    def _validate_messages(messages: list[dict]) -> None:
        if not isinstance(messages, list) or not messages:
            raise BadRequest("messages must be a non-empty list")

    def _prepare(self, messages: list[dict],
                 tools: list[dict] | None) -> tuple[list[dict], int]:
        reserve = min(self.max_tokens, max(0, self.max_context_tokens // 2))
        trimmed = trim_messages(messages, self.max_context_tokens,
                                tools=tools, reserve_tokens=reserve)
        if trimmed.changed or trimmed.over_limit:
            data = trimmed.event_data()
            data.update({"model": self.model, "provider": self.name})
            self.runtime.emit("llm.context.trimmed", data)
            _log.info("context trimmed for %s: %d -> %d tokens (limit %d)",
                      self.model, trimmed.tokens_before,
                      trimmed.tokens_after, trimmed.limit)
        estimate = (estimate_messages_tokens(trimmed.messages)
                    + estimate_tools_tokens(tools))
        return trimmed.messages, max(1, estimate)

    def _check_budget(self, project_id: str | None,
                      prompt_estimate: int) -> None:
        self.runtime.usage.check_budget(
            project_id, prompt_estimate, self.project_token_budget)

    def _reserve(self) -> int:
        return min(self.max_tokens, max(0, self.max_context_tokens // 2))

    # -- usage / stats ----------------------------------------------------
    def _record_usage(self, usage: Usage, *, role: str | None,
                      project_id: str | None, model: str | None = None) -> None:
        self.runtime.record(
            project_id=project_id, role=role, provider=self.name,
            model=str(model or self.model),
            usage=usage,
            price_per_1k_input=self.price_per_1k_input,
            price_per_1k_output=self.price_per_1k_output)

    def _fill_usage(self, usage: Usage, prompt_estimate: int,
                    text: str) -> Usage:
        if usage.prompt == 0 and usage.completion == 0:
            return Usage(prompt=prompt_estimate,
                         completion=estimate_tokens(text))
        if usage.prompt == 0:
            usage.prompt = prompt_estimate
        if usage.total == 0:
            usage.total = usage.prompt + usage.completion
        return usage

    def _note_success(self, started: float) -> None:
        self._stats["calls"] = int(self._stats.get("calls") or 0) + 1
        self._stats["last_latency_ms"] = int((time.monotonic() - started) * 1000)
        self._stats["last_used_at"] = int(time.time() * 1000)
        self._stats["last_error"] = ""
        self.breaker.record_success()

    def _note_failure(self, error: BaseException, started: float) -> None:
        self._stats["failures"] = int(self._stats.get("failures") or 0) + 1
        self._stats["last_error"] = _compact(str(error), 300)
        self._stats["last_used_at"] = int(time.time() * 1000)
        if isinstance(error, Unavailable) and not isinstance(
                error, (BudgetExceeded, RateLimited)):
            if _is_transient(str(error)):
                self.breaker.record_failure(str(error))

    # -- non-streaming ----------------------------------------------------
    async def _request_nonstream(
            self, messages: list[dict], tools: list[dict] | None, *,
            tool_choice: Any, role: str | None, project_id: str | None,
            prompt_estimate: int) -> LLMResult:
        started = time.monotonic()
        ticket = await self.limiter.acquire(prompt_estimate)
        actual_tokens: int | None = None
        try:
            if not self.breaker.allow():
                raise Unavailable(
                    f"LLM circuit open for {self.name}/{self.model}")
            payload: dict | None = None
            last_error: Unavailable | None = None
            for attempt in range(self.retries + 1):
                try:
                    payload = await asyncio.to_thread(
                        self._post_json_sync, messages, tools, tool_choice)
                    break
                except Unavailable as exc:
                    last_error = exc
                    if attempt >= self.retries or not _is_transient(str(exc)):
                        raise
                    delay = self.retry_backoff_s * (2 ** attempt)
                    _log.info("LLM transient error (%s); retry %d/%d in %.1fs",
                              exc, attempt + 1, self.retries, delay)
                    await asyncio.sleep(delay)
            if payload is None:  # pragma: no cover - defensive
                raise last_error or Unavailable("LLM request failed")
            result = self._parse_response(payload)
            usage = self._fill_usage(result.usage, prompt_estimate, result.text)
            result.usage = usage
            actual_tokens = usage.total
            self._record_usage(usage, role=role, project_id=project_id,
                               model=result.model or self.model)
            self._note_success(started)
            return result
        except BudgetExceeded:
            raise
        except Exception as exc:
            self._note_failure(exc, started)
            raise
        finally:
            self.limiter.release(ticket, actual_tokens)

    # -- streaming --------------------------------------------------------
    async def _collect_stream(
            self, messages: list[dict], tools: list[dict] | None, *,
            tool_choice: Any, role: str | None, project_id: str | None,
            on_delta: Callable[[dict], Any],
            prompt_estimate: int) -> LLMResult:
        result = LLMResult(provider=self.name, model=self.model)
        usage = Usage()
        calls: dict[int, dict] = {}
        text_parts: list[str] = []
        async for delta in self._stream_deltas(
                messages, tools, tool_choice=tool_choice, role=role,
                project_id=project_id, prompt_estimate=prompt_estimate):
            kind = str(delta.get("type") or "")
            if kind == "text":
                chunk = str(delta.get("text") or "")
                if chunk:
                    text_parts.append(chunk)
            elif kind == "tool_call":
                index = int(delta.get("index") or 0)
                entry = calls.setdefault(
                    index, {"id": "", "name": "", "arguments": ""})
                if delta.get("id"):
                    entry["id"] = str(delta["id"])
                if delta.get("name"):
                    entry["name"] = str(delta["name"])
                fragment = delta.get("arguments")
                if isinstance(fragment, dict):
                    entry["arguments"] = json.dumps(fragment, ensure_ascii=False)
                elif fragment:
                    entry["arguments"] += str(fragment)
            elif kind == "usage":
                usage = usage.merged(Usage.from_dict(delta.get("usage")))
            elif kind == "finish":
                result.finish_reason = str(
                    delta.get("finish_reason") or "stop")
            await _maybe_await(on_delta(delta))
        result.text = "".join(text_parts)
        result.tool_calls = self._calls_from_fragments(calls)
        result.usage = self._fill_usage(usage, prompt_estimate, result.text)
        if result.tool_calls and result.finish_reason == "stop":
            result.finish_reason = "tool_calls"
        return result

    @staticmethod
    def _calls_from_fragments(calls: dict[int, dict]) -> list[ToolCall]:
        out: list[ToolCall] = []
        for index in sorted(calls):
            entry = calls[index]
            name = entry.get("name") or ""
            if not name:
                continue
            arguments = parse_tool_arguments(entry.get("arguments"))
            out.append(ToolCall(
                id=str(entry.get("id") or f"call-{index}"),
                name=name,
                arguments=arguments,
            ))
        return out

    async def _stream_deltas(
            self, messages: list[dict], tools: list[dict] | None, *,
            tool_choice: Any, role: str | None, project_id: str | None,
            prompt_estimate: int) -> AsyncIterator[dict]:
        started = time.monotonic()
        ticket = await self.limiter.acquire(prompt_estimate)
        usage = Usage()
        saw_usage = False
        text_parts: list[str] = []
        actual_tokens: int | None = None
        if not self.breaker.allow():
            self.limiter.release(ticket)
            raise Unavailable(f"LLM circuit open for {self.name}/{self.model}")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def worker() -> None:
            try:
                for delta in self._iter_sse_sync(messages, tools, tool_choice):
                    loop.call_soon_threadsafe(queue.put_nowait,
                                              ("delta", delta))
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
            except BaseException as exc:  # noqa: BLE001 - forwarded to caller
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))

        thread = threading.Thread(target=worker, name=f"llm-sse-{self.name}",
                                  daemon=True)
        thread.start()
        try:
            while True:
                kind, value = await queue.get()
                if kind == "done":
                    break
                if kind == "error":
                    raise value
                delta = value
                if delta.get("type") == "text":
                    text_parts.append(str(delta.get("text") or ""))
                elif delta.get("type") == "usage":
                    saw_usage = True
                    usage = usage.merged(Usage.from_dict(delta.get("usage")))
                yield delta
            usage = self._fill_usage(usage, prompt_estimate,
                                     "".join(text_parts))
            actual_tokens = usage.total
            self._record_usage(usage, role=role, project_id=project_id)
            if not saw_usage:
                yield {"type": "usage", "usage": usage.to_dict()}
            self._note_success(started)
        except BudgetExceeded:
            raise
        except Exception as exc:
            self._note_failure(exc, started)
            raise
        finally:
            self.limiter.release(ticket, actual_tokens)
            try:
                thread.join(timeout=0.2)
            except Exception:  # pragma: no cover
                pass

    # -- HTTP -------------------------------------------------------------
    def _headers(self, *, stream: bool) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _candidate_urls(self, *, stream: bool) -> list[str]:
        raise NotImplementedError

    def _build_body(self, messages: list[dict], tools: list[dict] | None,
                    tool_choice: Any, *, stream: bool) -> dict:
        raise NotImplementedError

    def _parse_response(self, payload: dict) -> LLMResult:
        raise NotImplementedError

    def _parse_stream_payload(self, payload: dict) -> list[dict]:
        raise NotImplementedError

    def _http_error(self, exc: HTTPError) -> Unavailable:
        detail = ""
        try:
            detail = exc.read()[:300].decode("utf-8", "replace")
        except Exception:  # pragma: no cover - body may be unreadable
            detail = ""
        detail = _redact(_compact(detail, 200), self.api_key)
        suffix = f": {detail}" if detail else ""
        return Unavailable(f"LLM HTTP {exc.code}{suffix}")

    def _post_json_sync(self, messages: list[dict], tools: list[dict] | None,
                        tool_choice: Any) -> dict:
        body = self._build_body(messages, tools, tool_choice, stream=False)
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_error: Unavailable | None = None
        for url in self._candidate_urls(stream=False):
            request = Request(url, data=raw_body,
                              headers=self._headers(stream=False),
                              method="POST")
            try:
                with urlopen(request, timeout=self.timeout_s) as response:
                    raw = response.read().decode("utf-8", "replace")
                self._endpoint = url
                try:
                    payload = json.loads(raw)
                except ValueError as exc:
                    raise Unavailable(
                        "LLM returned non-JSON response") from exc
                if not isinstance(payload, dict):
                    raise Unavailable("LLM returned a non-object response")
                return payload
            except HTTPError as exc:
                error = self._http_error(exc)
                if exc.code in (404, 405):
                    last_error = error
                    continue
                raise error from exc
            except URLError as exc:
                raise Unavailable(
                    f"LLM connection failed: {exc.reason}") from exc
            except Unavailable:
                raise
            except Exception as exc:  # socket timeout, malformed URL, ...
                raise Unavailable(
                    f"LLM request failed: {type(exc).__name__}") from exc
        raise last_error or Unavailable("LLM request failed: no endpoint candidate")

    def _iter_sse_sync(self, messages: list[dict], tools: list[dict] | None,
                       tool_choice: Any):
        body = self._build_body(messages, tools, tool_choice, stream=True)
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_error: Unavailable | None = None
        for url in self._candidate_urls(stream=True):
            request = Request(url, data=raw_body,
                              headers=self._headers(stream=True),
                              method="POST")
            try:
                with urlopen(request, timeout=self.timeout_s) as response:
                    self._endpoint = url
                    for raw_line in response:
                        line = raw_line.decode("utf-8", "replace").strip()
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if not data or data == "[DONE]":
                            if data == "[DONE]":
                                return
                            continue
                        try:
                            payload = json.loads(data)
                        except ValueError:
                            continue
                        if not isinstance(payload, dict):
                            continue
                        for delta in self._parse_stream_payload(payload):
                            yield delta
                return
            except HTTPError as exc:
                error = self._http_error(exc)
                if exc.code in (404, 405):
                    last_error = error
                    continue
                raise error from exc
            except URLError as exc:
                raise Unavailable(
                    f"LLM connection failed: {exc.reason}") from exc
            except Unavailable:
                raise
            except Exception as exc:  # pragma: no cover - stream decode errors
                raise Unavailable(
                    f"LLM request failed: {type(exc).__name__}") from exc
        raise last_error or Unavailable("LLM request failed: no endpoint candidate")


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------

class OpenAICompatibleProvider(BaseProvider):
    """POST ``{base_url}/chat/completions`` (OpenAI-compatible gateways)."""

    name = "openai"

    def __init__(self, *, base_url: str, model: str, api_key: str = "",
                 temperature: float = 0.2, max_tokens: int = 4096,
                 timeout_s: float = 60.0, retries: int = 2,
                 retry_backoff_s: float = 0.5, **kwargs: Any) -> None:
        if not base_url:
            raise BadRequest("llm.base_url is required for the openai provider")
        if not model:
            raise BadRequest("llm.model is required for the openai provider")
        super().__init__(
            base_url=base_url, model=model, api_key=api_key,
            temperature=temperature, max_tokens=max_tokens,
            timeout_s=timeout_s, retries=retries,
            retry_backoff_s=retry_backoff_s, **kwargs)

    def _candidate_urls(self, *, stream: bool) -> list[str]:
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

    def _build_body(self, messages: list[dict], tools: list[dict] | None,
                    tool_choice: Any, *, stream: bool) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max(1, int(self.max_tokens)),
        }
        native = openai_tool_schemas(tools)
        if native:
            body["tools"] = native
            body["tool_choice"] = tool_choice or "auto"
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body

    def _parse_response(self, payload: dict) -> LLMResult:
        try:
            choice = payload["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise Unavailable(
                "LLM response is missing choices[0]") from exc
        message = choice.get("message") or {}
        text = _content_text(message.get("content"))
        tool_calls = [call for call in (
            ToolCall.from_openai(raw) for raw in message.get("tool_calls") or [])
            if call is not None]
        usage = Usage.from_dict(payload.get("usage"))
        return LLMResult(
            text=text, tool_calls=tool_calls, usage=usage,
            finish_reason=normalise_finish_reason(
                choice.get("finish_reason"), has_tools=bool(tool_calls)),
            model=str(payload.get("model") or self.model),
            provider=self.name, raw=payload)

    def _parse_stream_payload(self, payload: dict) -> list[dict]:
        deltas: list[dict] = []
        usage = payload.get("usage")
        if isinstance(usage, dict) and usage:
            deltas.append({"type": "usage",
                           "usage": Usage.from_dict(usage).to_dict()})
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            text = _content_text(delta.get("content"))
            if text:
                deltas.append({"type": "text", "text": text})
            for raw in delta.get("tool_calls") or []:
                if not isinstance(raw, dict):
                    continue
                function = raw.get("function") or {}
                delta_call: dict[str, Any] = {
                    "type": "tool_call",
                    "index": int(raw.get("index") or 0),
                }
                if raw.get("id"):
                    delta_call["id"] = raw["id"]
                if function.get("name"):
                    delta_call["name"] = function["name"]
                if function.get("arguments") is not None:
                    delta_call["arguments"] = function["arguments"]
                deltas.append(delta_call)
            finish = choice.get("finish_reason")
            if finish:
                deltas.append({"type": "finish",
                               "finish_reason": normalise_finish_reason(finish)})
        return deltas


# ---------------------------------------------------------------------------
# Anthropic Messages
# ---------------------------------------------------------------------------

class AnthropicProvider(BaseProvider):
    """POST ``{base_url}/v1/messages`` (Anthropic Messages API)."""

    name = "anthropic"
    default_base_url = "https://api.anthropic.com"
    api_version = "2023-06-01"

    def __init__(self, *, base_url: str = "", model: str = "",
                 api_key: str = "", temperature: float = 0.2,
                 max_tokens: int = 4096, timeout_s: float = 60.0,
                 retries: int = 2, retry_backoff_s: float = 0.5,
                 **kwargs: Any) -> None:
        if not model:
            raise BadRequest("llm.model is required for the anthropic provider")
        super().__init__(
            base_url=base_url or self.default_base_url, model=model,
            api_key=api_key, temperature=temperature, max_tokens=max_tokens,
            timeout_s=timeout_s, retries=retries,
            retry_backoff_s=retry_backoff_s, **kwargs)

    def _headers(self, *, stream: bool) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "anthropic-version": self.api_version,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _candidate_urls(self, *, stream: bool) -> list[str]:
        base = self.base_url
        if base.endswith("/messages"):
            urls = [base]
        elif base.endswith("/v1"):
            urls = [base + "/messages"]
        else:
            urls = [base + "/v1/messages", base + "/messages"]
        if self._endpoint is not None and self._endpoint in urls:
            urls.remove(self._endpoint)
            urls.insert(0, self._endpoint)
        return urls

    @staticmethod
    def _convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        converted: list[dict] = []
        names_by_id: dict[str, str] = {}
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "system":
                text = _content_text(message.get("content"))
                if text:
                    system_parts.append(text)
                continue
            if role == "tool":
                text = _content_text(message.get("content"))
                tool_id = str(message.get("tool_call_id") or "")
                converted.append({
                    "role": "user",
                    "content": [{"type": "tool_result",
                                 "tool_use_id": tool_id,
                                 "content": text}],
                })
                continue
            if role == "assistant":
                blocks: list[dict] = []
                text = _content_text(message.get("content"))
                if text:
                    blocks.append({"type": "text", "text": text})
                for raw in message.get("tool_calls") or []:
                    if not isinstance(raw, dict):
                        continue
                    function = raw.get("function") or {}
                    name = str(function.get("name") or raw.get("name") or "")
                    if not name:
                        continue
                    call_id = str(raw.get("id") or f"call-{len(blocks)}")
                    names_by_id[call_id] = name
                    blocks.append({
                        "type": "tool_use", "id": call_id, "name": name,
                        "input": parse_tool_arguments(
                            function.get("arguments", raw.get("arguments", {}))),
                    })
                converted.append({"role": "assistant",
                                  "content": blocks or text})
                continue
            converted.append({"role": "user",
                              "content": _content_text(message.get("content"))})
        # Anthropic rejects consecutive identical roles; merge them.
        merged: list[dict] = []
        for message in converted:
            if merged and merged[-1]["role"] == message["role"]:
                previous = merged[-1]["content"]
                current = message["content"]
                if isinstance(previous, list) and isinstance(current, list):
                    previous.extend(current)
                elif isinstance(previous, list):
                    previous.append({"type": "text", "text": str(current)})
                elif isinstance(current, list):
                    merged[-1]["content"] = (
                        [{"type": "text", "text": str(previous)}] + current)
                else:
                    merged[-1]["content"] = (str(previous) + "\n\n"
                                             + str(current))
            else:
                merged.append(message)
        return "\n\n".join(system_parts), merged

    def _build_body(self, messages: list[dict], tools: list[dict] | None,
                    tool_choice: Any, *, stream: bool) -> dict:
        system, converted = self._convert_messages(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": max(1, int(self.max_tokens)),
            "temperature": self.temperature,
        }
        if system:
            body["system"] = system
        native = anthropic_tools(tools)
        if native:
            body["tools"] = native
            body["tool_choice"] = tool_choice or {"type": "auto"}
        if stream:
            body["stream"] = True
        return body

    def _parse_response(self, payload: dict) -> LLMResult:
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise Unavailable("LLM response is missing content blocks")
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                call = ToolCall.from_anthropic(block)
                if call is not None:
                    tool_calls.append(call)
        return LLMResult(
            text="".join(text_parts), tool_calls=tool_calls,
            usage=Usage.from_dict(payload.get("usage")),
            finish_reason=normalise_finish_reason(
                payload.get("stop_reason"), has_tools=bool(tool_calls)),
            model=str(payload.get("model") or self.model),
            provider=self.name, raw=payload)

    def _parse_stream_payload(self, payload: dict) -> list[dict]:
        kind = str(payload.get("type") or "")
        deltas: list[dict] = []
        if kind == "message_start":
            message = payload.get("message") or {}
            usage = message.get("usage")
            if isinstance(usage, dict) and usage:
                deltas.append({"type": "usage",
                           "usage": Usage.from_dict(usage).to_dict()})
        elif kind == "content_block_start":
            block = payload.get("content_block") or {}
            if block.get("type") == "tool_use":
                deltas.append({
                    "type": "tool_call",
                    "index": int(payload.get("index") or 0),
                    "id": block.get("id") or "",
                    "name": block.get("name") or "",
                })
        elif kind == "content_block_delta":
            delta = payload.get("delta") or {}
            if delta.get("type") == "text_delta":
                deltas.append({"type": "text",
                               "text": str(delta.get("text") or "")})
            elif delta.get("type") == "input_json_delta":
                deltas.append({
                    "type": "tool_call",
                    "index": int(payload.get("index") or 0),
                    "arguments": str(delta.get("partial_json") or ""),
                })
        elif kind == "message_delta":
            delta = payload.get("delta") or {}
            usage = payload.get("usage")
            if isinstance(usage, dict) and usage:
                deltas.append({"type": "usage",
                           "usage": Usage.from_dict(usage).to_dict()})
            if delta.get("stop_reason"):
                deltas.append({"type": "finish",
                               "finish_reason": normalise_finish_reason(
                                   delta.get("stop_reason"))})
        elif kind == "message_stop":
            deltas.append({"type": "finish", "finish_reason": "stop"})
        return deltas


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------

class GeminiProvider(BaseProvider):
    """POST ``{base_url}/v1beta/models/{model}:generateContent``."""

    name = "gemini"
    default_base_url = "https://generativelanguage.googleapis.com"

    def __init__(self, *, base_url: str = "", model: str = "",
                 api_key: str = "", temperature: float = 0.2,
                 max_tokens: int = 4096, timeout_s: float = 60.0,
                 retries: int = 2, retry_backoff_s: float = 0.5,
                 **kwargs: Any) -> None:
        if not model:
            raise BadRequest("llm.model is required for the gemini provider")
        super().__init__(
            base_url=base_url or self.default_base_url, model=model,
            api_key=api_key, temperature=temperature, max_tokens=max_tokens,
            timeout_s=timeout_s, retries=retries,
            retry_backoff_s=retry_backoff_s, **kwargs)

    def _headers(self, *, stream: bool) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        return headers

    @property
    def _model_path(self) -> str:
        model = self.model
        if model.startswith("models/"):
            model = model[len("models/"):]
        return quote(model, safe="._-")

    def _root(self) -> str:
        base = self.base_url
        if ":generateContent" in base:
            base = base.split(":generateContent", 1)[0]
        elif ":streamGenerateContent" in base:
            base = base.split(":streamGenerateContent", 1)[0]
        if "/v1beta" not in base and "/v1/" not in base:
            base = base.rstrip("/") + "/v1beta"
        return base

    def _candidate_urls(self, *, stream: bool) -> list[str]:
        action = (":streamGenerateContent?alt=sse" if stream
                  else ":generateContent")
        urls = [f"{self._root()}/models/{self._model_path}{action}"]
        if self._endpoint is not None and self._endpoint in urls:
            urls.remove(self._endpoint)
            urls.insert(0, self._endpoint)
        return urls

    @staticmethod
    def _convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        contents: list[dict] = []
        names_by_id: dict[str, str] = {}
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "system":
                text = _content_text(message.get("content"))
                if text:
                    system_parts.append(text)
                continue
            if role == "tool":
                name = names_by_id.get(str(message.get("tool_call_id") or ""), "")
                raw_content = _content_text(message.get("content"))
                try:
                    parsed: Any = json.loads(raw_content)
                except (TypeError, ValueError):
                    parsed = raw_content
                response: Any = parsed if isinstance(parsed, dict) else {
                    "result": parsed}
                contents.append({"role": "user", "parts": [{
                    "functionResponse": {"name": name or "tool",
                                         "response": response}}]})
                continue
            if role == "assistant":
                parts: list[dict] = []
                text = _content_text(message.get("content"))
                if text:
                    parts.append({"text": text})
                for raw in message.get("tool_calls") or []:
                    if not isinstance(raw, dict):
                        continue
                    function = raw.get("function") or {}
                    name = str(function.get("name") or raw.get("name") or "")
                    if not name:
                        continue
                    call_id = str(raw.get("id") or f"call-{len(parts)}")
                    names_by_id[call_id] = name
                    parts.append({"functionCall": {
                        "name": name,
                        "args": parse_tool_arguments(
                            function.get("arguments", raw.get("arguments", {}))),
                    }})
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
                continue
            contents.append({"role": "user",
                             "parts": [{"text": _content_text(
                                 message.get("content"))}]})
        return "\n\n".join(system_parts), contents

    def _build_body(self, messages: list[dict], tools: list[dict] | None,
                    tool_choice: Any, *, stream: bool) -> dict:
        system, contents = self._convert_messages(messages)
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": max(1, int(self.max_tokens)),
            },
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        native = gemini_tools(tools)
        if native:
            body["tools"] = native
        return body

    def _parse_response(self, payload: dict) -> LLMResult:
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            feedback = payload.get("promptFeedback") or {}
            reason = feedback.get("blockReason") if isinstance(feedback, dict) else ""
            raise Unavailable(
                f"LLM response has no candidates"
                + (f" (blocked: {reason})" if reason else ""))
        candidate = candidates[0] or {}
        content = candidate.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for index, part in enumerate(parts or []):
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            function_call = part.get("functionCall")
            if isinstance(function_call, dict):
                call = ToolCall.from_gemini(function_call, index=index)
                if call is not None:
                    tool_calls.append(call)
        usage = Usage.from_dict(payload.get("usageMetadata"))
        return LLMResult(
            text="".join(text_parts), tool_calls=tool_calls, usage=usage,
            finish_reason=normalise_finish_reason(
                candidate.get("finishReason"), has_tools=bool(tool_calls)),
            model=str(payload.get("modelVersion") or self.model),
            provider=self.name, raw=payload)

    def _parse_stream_payload(self, payload: dict) -> list[dict]:
        deltas: list[dict] = []
        usage = payload.get("usageMetadata")
        if isinstance(usage, dict) and usage:
            deltas.append({"type": "usage",
                           "usage": Usage.from_dict(usage).to_dict()})
        for candidate in payload.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content") or {}
            parts = content.get("parts") if isinstance(content, dict) else []
            for index, part in enumerate(parts or []):
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str) and part["text"]:
                    deltas.append({"type": "text", "text": part["text"]})
                function_call = part.get("functionCall")
                if isinstance(function_call, dict):
                    deltas.append({
                        "type": "tool_call",
                        "index": index,
                        "id": f"gemini-call-{index}",
                        "name": function_call.get("name") or "",
                        "arguments": function_call.get("args") or {},
                    })
            finish = candidate.get("finishReason")
            if finish:
                deltas.append({"type": "finish",
                               "finish_reason": normalise_finish_reason(finish)})
        return deltas


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------

class MockProvider:
    """Deterministic, offline provider used by default (llm.provider=mock).

    Tests can inject replies (strings, ``LLMResult`` objects or dicts) with
    :meth:`push` / the ``replies`` constructor argument.
    """

    name = "mock"

    def __init__(self, *, model: str = "mock", replies: list[Any] | None = None,
                 stream: bool = True, **kwargs: Any) -> None:
        self.model = str(model or "mock")
        self.stream_enabled = bool(stream)
        self.replies: list[Any] = list(replies or [])
        self.calls: list[tuple[list[dict], Any]] = []
        self.runtime = get_runtime()
        self.max_context_tokens = max(1024, int(
            kwargs.get("max_context_tokens", 128_000) or 128_000))
        self.max_tokens = max(1, int(kwargs.get("max_tokens", 4096) or 4096))
        self.project_token_budget = max(0, int(
            kwargs.get("project_token_budget", 0) or 0))
        self.price_per_1k_input = 0.0
        self.price_per_1k_output = 0.0
        self.rpm = int(kwargs.get("rpm", 0) or 0)
        self.tpm = int(kwargs.get("tpm", 0) or 0)
        self.max_concurrency = int(kwargs.get("max_concurrency", 8) or 8)
        self.breaker = CircuitBreaker()
        self.limiter = RateLimiter(rpm=self.rpm, tpm=self.tpm,
                                   max_concurrency=self.max_concurrency)
        self._stats = {"calls": 0, "failures": 0, "last_latency_ms": None,
                       "last_error": "", "last_used_at": None}

    # -- test driver ------------------------------------------------------
    def push(self, reply: Any) -> "MockProvider":
        self.replies.append(reply)
        return self

    def attach_runtime(self, runtime: Any) -> None:
        self.runtime = runtime

    def health(self) -> dict:
        return {"ok": True, "provider": "mock", "model": self.model,
                "circuit": self.breaker.snapshot(),
                "limiter": self.limiter.snapshot(), **self._stats}

    def describe(self) -> dict:
        return {"name": "mock", "provider": "mock", "model": self.model,
                "base_url": "", "stream": self.stream_enabled,
                "limits": self.limiter.snapshot(), "health": self.health()}

    # -- deterministic text ----------------------------------------------
    @staticmethod
    def _text(messages: list[dict]) -> str:
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

    def _next_result(self, messages: list[dict]) -> LLMResult:
        reply: Any = self.replies.pop(0) if self.replies else None
        if isinstance(reply, LLMResult):
            if not reply.provider:
                reply.provider = "mock"
            if not reply.model:
                reply.model = self.model
            return reply
        if isinstance(reply, dict):
            return LLMResult(
                text=str(reply.get("text") or reply.get("content") or ""),
                tool_calls=[call for call in (
                    ToolCall.from_openai(raw)
                    for raw in reply.get("tool_calls") or [])
                    if call is not None],
                usage=Usage.from_dict(reply.get("usage")),
                finish_reason=str(reply.get("finish_reason") or "stop"),
                model=self.model, provider="mock")
        text = str(reply) if reply is not None else self._text(messages)
        return LLMResult(text=text, model=self.model, provider="mock")

    def _usage(self, result: LLMResult, messages: list[dict]) -> Usage:
        estimate = estimate_messages_tokens(messages)
        if result.usage.total:
            return result.usage
        return Usage(prompt=estimate,
                     completion=estimate_tokens(result.text))

    def _record(self, result: LLMResult, messages: list[dict], *,
                role: str | None, project_id: str | None) -> None:
        usage = self._usage(result, messages)
        result.usage = usage
        self.runtime.usage.check_budget(
            project_id, usage.prompt, self.project_token_budget)
        self.runtime.record(project_id=project_id, role=role,
                            provider="mock", model=self.model, usage=usage)
        self._stats["calls"] = int(self._stats.get("calls") or 0) + 1
        self._stats["last_used_at"] = int(time.time() * 1000)

    async def chat(self, messages: list[dict], *,
                   tools: list[dict] | None = None,
                   on_delta: Callable[[dict], Any] | None = None,
                   role: str | None = None,
                   project_id: str | None = None) -> str:
        _ = tools
        result = await self.chat_with_tools(messages, role=role,
                                            project_id=project_id,
                                            on_delta=on_delta)
        return result.text

    async def chat_with_tools(
            self, messages: list[dict], tools: list[dict] | None = None, *,
            tool_choice: Any = None,
            role: str | None = None,
            project_id: str | None = None,
            on_delta: Callable[[dict], Any] | None = None,
            stream: bool | None = None) -> LLMResult:
        _ = tools, tool_choice
        started = time.monotonic()
        self.calls.append(([dict(item) for item in messages],
                           tools if tools is not None else None))
        result = self._next_result(messages)
        self._record(result, messages, role=role, project_id=project_id)
        self._stats["last_latency_ms"] = int(
            (time.monotonic() - started) * 1000)
        use_stream = self.stream_enabled if stream is None else bool(stream)
        if use_stream and on_delta is not None:
            for delta in self._deltas_for(result):
                await _maybe_await(on_delta(delta))
        return result

    @staticmethod
    def _deltas_for(result: LLMResult) -> list[dict]:
        deltas: list[dict] = []
        text = result.text or ""
        for index in range(0, len(text), 24):
            deltas.append({"type": "text", "text": text[index:index + 24]})
        for index, call in enumerate(result.tool_calls):
            deltas.append({"type": "tool_call", "index": index,
                           "id": call.id, "name": call.name,
                           "arguments": json.dumps(call.arguments,
                                                   ensure_ascii=False)})
        deltas.append({"type": "usage", "usage": result.usage.to_dict()})
        deltas.append({"type": "finish",
                       "finish_reason": result.finish_reason})
        return deltas

    async def stream(self, messages: list[dict],
                     tools: list[dict] | None = None, *,
                     tool_choice: Any = None,
                     role: str | None = None,
                     project_id: str | None = None):
        _ = tools, tool_choice
        result = await self.chat_with_tools(messages, role=role,
                                            project_id=project_id,
                                            stream=False)
        for delta in self._deltas_for(result):
            yield delta

    async def ping(self) -> dict:
        return {"ok": True, "provider": "mock"}


__all__ = [
    "AnthropicProvider",
    "BaseProvider",
    "GeminiProvider",
    "MockProvider",
    "OpenAICompatibleProvider",
    "anthropic_tools",
    "gemini_tools",
    "normalise_finish_reason",
    "openai_tool_schemas",
]
