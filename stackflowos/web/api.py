"""REST surface for the operator UI.

Every handler is a thin translation over ``App``. Errors are ``SfoError``
subclasses and map to stable codes/statuses, so the frontend can show exactly
why something was refused.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable

from ..errors import BadRequest, SfoError
from ..util.logging import get_logger

Handler = Callable[[re.Match, dict, dict], Awaitable[tuple[int, Any]]]


def _ok(data: Any, status: int = 200) -> tuple[int, dict]:
    return status, {"ok": True, "data": data}


class WebAPI:
    def __init__(self, app) -> None:
        self.app = app
        self.log = get_logger("web.api")
        self._routes: list[tuple[str, re.Pattern, Handler]] = []
        self._build_routes()

    async def handle(self, method: str, path: str, query: dict, body: bytes,
                     headers: dict | None = None) -> tuple[int, dict]:
        try:
            payload = _decode_body(body)
        except ValueError as exc:
            return 400, {"ok": False, "error": {"code": "BAD_REQUEST",
                                                "message": str(exc)}}
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
            except Exception as exc:
                self.log.exception("api handler failed: %s %s", method, path)
                return 500, {"ok": False, "error": {
                    "code": "INTERNAL", "message": str(exc)}}
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
        r("POST", r"/api/approvals/([^/]+)/decide", self.approval_decide)
        r("GET", r"/api/rules", self.rules_list)
        r("POST", r"/api/rules", self.rules_create)
        r("DELETE", r"/api/rules/([^/]+)", self.rules_delete)

        r("GET", r"/api/units", self.units_list)
        r("POST", r"/api/units/([^/]+)/kill", self.unit_kill)
        r("GET", r"/api/agents", self.agents_list)
        r("POST", r"/api/agents", self.agents_spawn)
        r("DELETE", r"/api/agents/([^/]+)", self.agent_kill)

        r("GET", r"/api/llm/test", self.llm_test)
        r("POST", r"/api/llm/test", self.llm_test)
        r("GET", r"/api/config", self.config_get)
        r("PATCH", r"/api/config", self.config_patch)
        r("GET", r"/api/leases", self.leases_list)
        r("POST", r"/api/system/complete/([^/]+)", self.project_complete)

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
        try:
            since = int(query.get("since") or 0)
        except ValueError:
            since = 0
        try:
            limit = min(int(query.get("limit") or 200), 1000)
        except ValueError:
            limit = 200
        records = self.app.eventlog.since(since, limit=limit,
                                          project_id=query.get("project_id") or None)
        return _ok({"events": [r.to_dict() for r in records],
                    "last_seq": self.app.eventlog.last_seq})

    async def console(self, match, query, body):
        types: list[str] | None = None
        if query.get("types"):
            types = [t.strip() for t in str(query["types"]).split(",") if t.strip()]
        try:
            limit = min(int(query.get("limit") or 200), 1000)
        except ValueError:
            limit = 200
        rows = self.app.console(types=types, project_id=query.get("project_id") or None,
                                limit=limit)
        return _ok({"events": rows})

    async def chat_history(self, match, query, body):
        try:
            limit = min(int(query.get("limit") or 100), 200)
        except ValueError:
            limit = 100
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
        card = await self.app.attach_project(
            str(body.get("name") or ""), path,
            goal=str(body.get("goal") or ""),
            allow_writes=bool(body.get("allow_writes", False)),
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
        allow = body.get("allow_writes", False)
        return _ok(await self.app.set_project_access(
            match.group(1), allow_writes=bool(allow), by="human:operator"))

    async def project_archive(self, match, query, body):
        return _ok(await self.app.archive_project(match.group(1),
                                                  by="human:operator"))

    async def project_delete(self, match, query, body):
        purge = body.get("purge_workspace", True)
        if isinstance(purge, str):
            purge = purge.lower() not in ("0", "false", "no")
        confirm = body.get("confirm") or query.get("confirm")
        return _ok(await self.app.delete_project(
            match.group(1), purge_workspace=bool(purge), confirm=confirm,
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
            role=body.get("role"), priority=int(body.get("priority") or 0),
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
            done=bool(body.get("done", True)), note=str(body.get("note") or ""))
        return _ok(task.to_dict())

    async def approvals_list(self, match, query, body):
        status = query.get("status") or None
        items = self.app.approvals.list(status=status,
                                        project_id=query.get("project_id") or None,
                                        limit=min(int(query.get("limit") or 200), 500))
        return _ok({"approvals": [a.to_dict() for a in items]})

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
        rules = self.app.policy.list_rules(active_only=bool(query.get("active_only")))
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
        return _ok(rule.to_dict(), 201)

    async def rules_delete(self, match, query, body):
        return _ok({"deleted": self.app.policy.delete_rule(match.group(1))})

    async def units_list(self, match, query, body):
        kind = query.get("kind") or None
        project_id = query.get("project_id") or None
        status = query.get("status") or None
        units = [
            unit for unit in self.app.unit_cards()
            if (kind is None or unit.get("kind") == kind)
            and (project_id is None or unit.get("project_id") == project_id)
            and (status is None or unit.get("status") == status)
        ]
        return _ok({"units": units})

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

    async def llm_test(self, match, query, body):
        """Round-trip a tiny completion against the *live* provider."""
        provider = getattr(self.app, "provider", None)
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
        leases = self.app.leases.list(holder=query.get("holder") or None,
                                      resource=query.get("resource") or None,
                                      active_only=query.get("all") not in ("1", "true"))
        return _ok({"leases": [l.to_dict() for l in leases]})


def _decode_body(body: bytes) -> dict:
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload
