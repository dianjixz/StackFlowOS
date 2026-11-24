"""REST surface for the operator UI.

Every handler is a thin translation over ``App``. Errors are ``SfoError``
subclasses and map to stable codes/statuses, so the frontend can show exactly
why something was refused.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
import secrets
from typing import Any, Awaitable, Callable

from ..errors import BadRequest, SfoError
from ..extensions import extension_routes
from ..util.logging import get_logger

Handler = Callable[[re.Match, dict, dict], Awaitable[tuple[int, Any]]]

#: Admission decision of the request currently being routed (set per task by
#: :meth:`WebAPI.handle`).  Handlers that need the authenticated role/user read
#: it through :func:`current_auth` instead of trusting headers or the query
#: string.  A context variable keeps concurrent requests isolated.
_current_auth: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "sfo_web_auth", default=None)


def current_auth() -> Any:
    """The :class:`web.auth.AdmissionDecision` for this request, if any."""
    return _current_auth.get()


class WebReply:
    """A raw (non-JSON) HTTP response produced by an API handler.

    Used by ``/api/metrics`` (Prometheus text) and other endpoints whose
    payload is not JSON.  Security headers are still applied by the server.
    """

    __slots__ = ("body", "content_type", "status", "headers")

    def __init__(self, body: str | bytes, *,
                 content_type: str = "text/plain; charset=utf-8",
                 status: int = 200,
                 headers: dict[str, str] | None = None) -> None:
        self.body = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        self.content_type = str(content_type)
        self.status = int(status)
        self.headers = dict(headers or {})


_INTEGER = re.compile(r"^[+-]?[0-9]+$")
_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRINGS = frozenset({"", "0", "false", "no", "off"})


def _ok(data: Any, status: int = 200) -> tuple[int, dict]:
    return status, {"ok": True, "data": data}


def _as_bool(value: Any, default: bool = False) -> bool:
    """Strict boolean parsing for JSON bodies and query strings (L1).

    JSON ``true``/``false`` and the string spellings the UI may send are
    accepted; everything else is a 400 instead of silently becoming ``True``
    (``bool("false")``) or ``False``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
        raise BadRequest("expected a boolean value")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    raise BadRequest("expected a boolean value")


def _parse_int(raw: Any, name: str, default: int, minimum: int,
               maximum: int | None = None) -> int:
    """Strict integer parsing for one query/body field (L1)."""
    if raw is None:
        return default
    if isinstance(raw, bool):
        raise BadRequest(f"{name} must be an integer")
    if isinstance(raw, int):
        value = raw
    else:
        text = str(raw).strip()
        if not _INTEGER.fullmatch(text):
            raise BadRequest(f"{name} must be an integer")
        try:
            value = int(text, 10)
        except ValueError:  # pragma: no cover - guarded by the regex
            raise BadRequest(f"{name} must be an integer") from None
    if value < minimum:
        raise BadRequest(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise BadRequest(f"{name} must be <= {maximum}")
    return value


def _int_query(query: dict, key: str, default: int, minimum: int,
               maximum: int | None = None) -> int:
    """Parse ``query[key]`` as an integer, rejecting bad values with a 400."""
    return _parse_int(query.get(key), key, default, minimum, maximum)


def _int_body(body: dict, key: str, default: int, minimum: int,
              maximum: int | None = None) -> int:
    """Parse ``body[key]`` as an integer, rejecting bad values with a 400."""
    return _parse_int(body.get(key), key, default, minimum, maximum)


class WebAPI:
    def __init__(self, app) -> None:
        self.app = app
        self.log = get_logger("web.api")
        self._routes: list[tuple[str, re.Pattern, Handler]] = []
        self._build_routes()

    async def handle(self, method: str, path: str, query: dict, body: bytes,
                     headers: dict | None = None,
                     auth: Any = None) -> tuple[int, Any]:
        try:
            payload = _decode_body(body)
        except ValueError as exc:
            return 400, {"ok": False, "error": {"code": "BAD_REQUEST",
                                                "message": str(exc)}}
        auth_token = _current_auth.set(auth)
        try:
            for route_method, pattern, handler in self._routes:
                if route_method != method:
                    continue
                match = pattern.fullmatch(path)
                if not match:
                    continue
                try:
                    status, result = await handler(match, query, payload)
                    return status, result
                except SfoError as exc:
                    return exc.http_status, {"ok": False, "error": exc.to_dict()}
                except Exception:
                    # Never leak exception text (paths, internals) to the client;
                    # the traceback goes to the log under a correlation id.
                    request_id = secrets.token_hex(8)
                    self.log.exception(
                        "api handler failed: %s %s (request_id=%s)",
                        method, path, request_id)
                    return 500, {"ok": False, "error": {
                        "code": "INTERNAL", "message": "internal server error",
                        "request_id": request_id}}
        finally:
            _current_auth.reset(auth_token)
        return 404, {"ok": False, "error": {"code": "NOT_FOUND",
                                            "message": f"no route {method} {path}"}}

    def _route(self, method: str, pattern: str, handler: Handler) -> None:
        self._routes.append((method, re.compile(pattern), handler))

    # ------------------------------------------------------------------
    def _build_routes(self) -> None:
        r = self._route
        r("GET", r"/api/health", self.health)
        r("GET", r"/api/state", self.state)
        r("GET", r"/api/events", self.events)
        r("GET", r"/api/console", self.console)

        r("GET", r"/api/chat", self.chat_history)
        r("POST", r"/api/chat", self.chat_send)

        r("GET", r"/api/projects", self.projects_list)
        r("POST", r"/api/projects", self.projects_create)
        r("POST", r"/api/projects/attach", self.projects_attach)
        r("POST", r"/api/projects/open", self.projects_attach)
        r("GET", r"/api/projects/([^/]+)", self.project_get)
        r("PATCH", r"/api/projects/([^/]+)", self.project_patch)
        r("POST", r"/api/projects/([^/]+)/archive", self.project_archive)
        r("POST", r"/api/projects/([^/]+)/access", self.project_access)
        r("DELETE", r"/api/projects/([^/]+)", self.project_delete)
        r("POST", r"/api/projects/([^/]+)/delete", self.project_delete)
        r("POST", r"/api/projects/([^/]+)/complete", self.project_complete)
        r("GET", r"/api/projects/([^/]+)/tasks", self.tasks_list)
        r("POST", r"/api/projects/([^/]+)/tasks", self.tasks_create)
        r("PATCH", r"/api/tasks/([^/]+)", self.task_patch)
        r("POST", r"/api/tasks/([^/]+)/note", self.task_note)
        r("POST", r"/api/tasks/([^/]+)/check", self.task_check)

        r("GET", r"/api/approvals", self.approvals_list)
        r("GET", r"/api/approvals/([^/]+)", self.approval_get)
        r("POST", r"/api/approvals/([^/]+)/decide", self.approval_decide)
        r("GET", r"/api/rules", self.rules_list)
        r("POST", r"/api/rules", self.rules_create)
        r("DELETE", r"/api/rules/([^/]+)", self.rules_delete)

        r("GET", r"/api/units", self.units_list)
        r("POST", r"/api/units/([^/]+)/kill", self.unit_kill)
        r("GET", r"/api/agents", self.agents_list)
        r("POST", r"/api/agents", self.agents_spawn)
        r("GET", r"/api/agents/([^/]+)/chat", self.agent_chat_history)
        r("POST", r"/api/agents/([^/]+)/chat", self.agent_chat_send)
        r("POST", r"/api/agents/([^/]+)/chat/cancel", self.agent_chat_cancel)
        r("DELETE", r"/api/agents/([^/]+)/chat", self.agent_chat_clear)
        r("DELETE", r"/api/agents/([^/]+)", self.agent_kill)

        r("GET", r"/api/llm/test", self.llm_test)
        r("POST", r"/api/llm/test", self.llm_test)
        r("GET", r"/api/config", self.config_get)
        r("PATCH", r"/api/config", self.config_patch)
        r("GET", r"/api/leases", self.leases_list)
        r("POST", r"/api/system/complete/([^/]+)", self.project_complete)

        # Optional subsystems expose their own routes through the extension
        # contract, so app.py/web/api.py stay a composition root rather than a
        # switchboard that every feature has to edit.
        for method, pattern, handler in extension_routes(
                getattr(self.app, "extensions", [])):
            try:
                self._route(method, pattern, handler)
            except Exception:
                self.log.exception("invalid extension route %s %s",
                                   method, pattern)

    # ------------------------------------------------------------------
    async def health(self, match, query, body):
        health = self.app.health()
        status = 200 if health.get("status") == "ok" else 503
        code, payload = _ok(health, status)
        if status != 200:
            payload["ok"] = False
        return code, payload

    async def state(self, match, query, body):
        return _ok(self.app.snapshot())

    async def events(self, match, query, body):
        since = _int_query(query, "since", 0, 0)
        limit = _int_query(query, "limit", 200, 1, 1000)
        records = self.app.eventlog.since(since, limit=limit,
                                          project_id=query.get("project_id") or None)
        return _ok({"events": [r.to_dict() for r in records],
                    "last_seq": self.app.eventlog.last_seq})

    async def console(self, match, query, body):
        types: list[str] | None = None
        if query.get("types"):
            types = [t.strip() for t in str(query["types"]).split(",") if t.strip()]
        limit = _int_query(query, "limit", 200, 1, 1000)
        rows = self.app.console(types=types, project_id=query.get("project_id") or None,
                                limit=limit)
        return _ok({"events": rows})

    async def chat_history(self, match, query, body):
        limit = _int_query(query, "limit", 100, 1, 200)
        return _ok({"messages": self.app.chat.history(limit)})

    async def chat_send(self, match, query, body):
        text = str(body.get("text") or "").strip()
        if not text:
            raise BadRequest("text required")
        message = await self.app.chat.send_user(text)
        return _ok({"message_id": message["id"], "message": message}, 202)

    async def projects_list(self, match, query, body):
        return _ok({"projects": [self.app.project_card(p.id)
                                 for p in self.app.projects.list()]})

    async def projects_create(self, match, query, body):
        name = str(body.get("name") or "").strip()
        goal = str(body.get("goal") or "").strip()
        if not name:
            name = (goal or "未命名项目")[:40]
        card = await self.app.create_project(name, goal or name,
                                             created_by="human:operator")
        return _ok(card, 201)

    async def projects_attach(self, match, query, body):
        path = str(body.get("path") or "").strip()
        if not path:
            raise BadRequest("path required")
        name = str(body.get("name") or "").strip()
        confirm = str(body.get("confirm") or "").strip()
        # Clicking "attach" is the user's confirmation, but the API still
        # requires the echoed path/name so a script cannot attach silently.
        if not confirm or confirm not in (path, name):
            raise BadRequest(
                "project.attach requires confirm=<path or name>",
                detail={"path": path, "name": name or None})
        card = await self.app.attach_project(
            name, path,
            goal=str(body.get("goal") or ""),
            allow_writes=_as_bool(body.get("allow_writes"), default=False),
            created_by="human:operator")
        return _ok(card, 201 if card.get("attached_now") else 200)

    async def project_get(self, match, query, body):
        return _ok(self.app.project_card(match.group(1)))

    async def project_patch(self, match, query, body):
        project_id = match.group(1)
        patch = body.get("patch") if isinstance(body.get("patch"), dict) else body
        if not isinstance(patch, dict):
            raise BadRequest("patch must be an object")
        if patch.get("status") == "done":
            return _ok(await self.app.complete_project(project_id))
        self.app.projects.update(project_id, patch)
        return _ok(self.app.project_card(project_id))

    async def project_access(self, match, query, body):
        allow = _as_bool(body.get("allow_writes"), default=False)
        confirm = body.get("confirm")
        if allow and not (confirm is True or
                          (isinstance(confirm, str) and
                           confirm.strip().lower() == "true")):
            raise BadRequest("project.access requires confirm=true to enable writes")
        return _ok(await self.app.set_project_access(
            match.group(1), allow_writes=allow, by="human:operator"))

    async def project_archive(self, match, query, body):
        return _ok(await self.app.archive_project(match.group(1),
                                                  by="human:operator"))

    async def project_delete(self, match, query, body):
        purge = _as_bool(body.get("purge_workspace"), default=True)
        confirm = body.get("confirm") or query.get("confirm")
        return _ok(await self.app.delete_project(
            match.group(1), purge_workspace=purge, confirm=confirm,
            by="human:operator"))

    async def project_complete(self, match, query, body):
        return _ok(await self.app.complete_project(match.group(1)))

    async def tasks_list(self, match, query, body):
        tasks = self.app.projects.task_list(project_id=match.group(1),
                                            status=query.get("status") or None)
        return _ok({"tasks": [t.to_dict() for t in tasks]})

    async def tasks_create(self, match, query, body):
        project_id = match.group(1)
        title = str(body.get("title") or "").strip()
        if not title:
            raise BadRequest("title required")
        task = self.app.projects.add_task(
            project_id, title, str(body.get("description") or ""),
            role=body.get("role"), priority=_int_body(body, "priority", 0, 0, 3),
            deps=list(body.get("deps") or []), created_by="human:operator",
        )
        return _ok(task.to_dict(), 201)

    async def task_patch(self, match, query, body):
        patch = body.get("patch") if isinstance(body.get("patch"), dict) else body
        if not isinstance(patch, dict):
            raise BadRequest("patch must be an object")
        task = self.app.projects.task_update(match.group(1), patch)
        return _ok(task.to_dict())

    async def task_note(self, match, query, body):
        task = self.app.projects.task_add_note(
            match.group(1), str(body.get("text") or ""), author="human:operator")
        return _ok(task.to_dict())

    async def task_check(self, match, query, body):
        task = self.app.projects.task_check(
            match.group(1), item_id=body.get("item_id"), text=body.get("text"),
            done=_as_bool(body.get("done"), default=True),
            note=str(body.get("note") or ""))
        return _ok(task.to_dict())

    async def approvals_list(self, match, query, body):
        status = query.get("status") or None
        items = self.app.approvals.list(
            status=status,
            project_id=query.get("project_id") or None,
            limit=_int_query(query, "limit", 200, 1, 500))
        # Lists must stay small: only the truncated preview plus its digest.
        return _ok({"approvals": [a.to_public_dict() for a in items]})

    async def approval_get(self, match, query, body):
        # Authenticated detail view: includes the bounded full preview (M7).
        request = self.app.approvals.get(match.group(1))
        return _ok(request.to_public_dict(include_full=True))

    async def approval_decide(self, match, query, body):
        decision = str(body.get("decision") or "").strip()
        if decision not in ("allow", "deny"):
            raise BadRequest("decision must be allow|deny")
        mode = str(body.get("mode") or "once")
        if mode not in ("once", "session", "always"):
            raise BadRequest("mode must be once|session|always")
        request = await self.app.decide_approval(
            match.group(1), decision=decision, mode=mode,
            note=str(body.get("note") or ""), by="human:operator")
        return _ok(request)

    async def rules_list(self, match, query, body):
        rules = self.app.policy.list_rules(
            active_only=_as_bool(query.get("active_only"), default=False))
        return _ok({"rules": [r.to_dict() for r in rules]})

    async def rules_create(self, match, query, body):
        rule = self.app.policy.add_rule(
            effect=str(body.get("effect") or "allow"),
            capability=str(body.get("capability") or ""),
            principal=str(body.get("principal") or "*"),
            scope=dict(body.get("scope") or {}),
            risk_max=str(body.get("risk_max") or "high"),
            expires_at=body.get("expires_at"),
            uses_left=body.get("uses_left"),
            note=str(body.get("note") or ""),
            created_by="human:operator",
        )
        self.app.approvals.resolve_pending()
        return _ok(rule.to_dict(), 201)

    async def rules_delete(self, match, query, body):
        return _ok({"deleted": self.app.policy.delete_rule(match.group(1))})

    async def units_list(self, match, query, body):
        kind = query.get("kind") or None
        tier = query.get("tier") or None
        state = query.get("state") or None
        project_id = query.get("project_id") or None
        status = query.get("status") or None
        units = [
            unit for unit in self.app.unit_cards()
            if (kind is None or unit.get("kind") == kind)
            and (tier is None or unit.get("tier") == tier)
            and (state is None or unit.get("state") == state)
            and (project_id is None or unit.get("project_id") == project_id)
            and (status is None or unit.get("status") == status)
        ]
        payload: dict[str, Any] = {"units": units}
        # The unit-control extension keeps hysteresis/dependency state; expose
        # its summary here so `/api/units` matches the documented contract
        # while remaining backwards compatible when the extension is absent.
        controller = getattr(self.app, "unit_control", None)
        summary = getattr(controller, "summary", None)
        if callable(summary):
            try:
                payload["summary"] = summary()
            except Exception:  # noqa: BLE001 - summary is advisory
                self.log.debug("unit summary failed", exc_info=True)
        return _ok(payload)

    async def unit_kill(self, match, query, body):
        return _ok(await self.app.kill_unit(match.group(1)))

    async def agents_list(self, match, query, body):
        agents = self.app.supervisor.list(project_id=query.get("project_id") or None,
                                          status=query.get("status") or None)
        return _ok({"agents": [a.to_dict() for a in agents]})

    async def agents_spawn(self, match, query, body):
        role = str(body.get("role") or "").strip()
        if not role:
            raise BadRequest("role required")
        agent = await self.app.spawn_agent(role, body.get("project_id"),
                                           body.get("task_id"))
        return _ok(agent, 201)

    async def agent_kill(self, match, query, body):
        return _ok({"ok": await self.app.kill_agent(match.group(1))})

    async def agent_chat_history(self, match, query, body):
        """One agent's thread + the live roster/project view for the chat UI."""
        agent_id = match.group(1)
        limit = _int_query(query, "limit", 200, 1, 400)
        agent = self.app.supervisor.get(agent_id).to_dict()
        project = None
        project_id = agent.get("project_id")
        if project_id:
            try:
                project = self.app.project_card(str(project_id))
            except SfoError:
                project = None
        provider = getattr(getattr(self.app, "agent_chat", None),
                           "provider", None) or getattr(self.app, "provider", None)
        return _ok({
            "agent": agent,
            "project": project,
            "messages": self.app.agent_chat.history(agent_id, limit=limit),
            "busy": self.app.agent_chat.busy(agent_id),
            "draft": self.app.agent_chat.draft(agent_id),
            "provider": getattr(provider, "name", ""),
            "model": getattr(provider, "model", ""),
        })

    async def agent_chat_send(self, match, query, body):
        text = str(body.get("text") or "").strip()
        if not text:
            raise BadRequest("text required")
        message = await self.app.agent_chat.send_user(match.group(1), text)
        return _ok({"message": message, "busy": True}, 202)

    async def agent_chat_cancel(self, match, query, body):
        return _ok({"canceled": self.app.agent_chat.cancel(match.group(1))})

    async def agent_chat_clear(self, match, query, body):
        return _ok({"cleared": await self.app.agent_chat.clear(
            match.group(1))})

    async def llm_test(self, match, query, body):
        """Round-trip a tiny completion against the *live* provider."""
        provider = getattr(getattr(self.app, "agent_chat", None),
                           "provider", None) or getattr(self.app, "provider", None)
        if provider is None:
            raise BadRequest("no llm provider configured")
        result = await provider.ping()
        return _ok({
            "provider": getattr(provider, "name", ""),
            "model": getattr(provider, "model", ""),
            "ok": bool(result.get("ok")),
            "latency_ms": result.get("latency_ms"),
            "error": result.get("error"),
        })

    async def config_get(self, match, query, body):
        return _ok(self.app.config.public_dict())

    async def config_patch(self, match, query, body):
        patch = body.get("patch") if isinstance(body.get("patch"), dict) else body
        if not isinstance(patch, dict) or not patch:
            raise BadRequest("config patch required")
        return _ok(await self.app.update_config(patch))

    async def leases_list(self, match, query, body):
        leases = self.app.leases.list(
            holder=query.get("holder") or None,
            resource=query.get("resource") or None,
            active_only=not _as_bool(query.get("all"), default=False))
        return _ok({"leases": [l.to_dict() for l in leases]})


def _decode_body(body: bytes) -> dict:
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Keep the client message stable: no parser internals, no body echo.
        raise ValueError("invalid JSON body") from None
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload
