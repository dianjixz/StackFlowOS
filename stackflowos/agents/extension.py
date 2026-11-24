"""Agent-system extension: queue API + project git routes.

Loaded through :mod:`stackflowos.extensions` (``build(app)`` contract) once the
core services exist.  It contributes:

* ``GET  /api/agents/queue``                     -- priority dispatch queue
* ``POST /api/agents/queue/{id}/cancel``         -- cancel a queued spawn
* ``GET  /api/projects/{id}/git``                -- branch/commits/diff
* ``POST /api/projects/{id}/git/rollback``       -- restore a ref

and wires the supervisor's optional project-domain hooks (``task_lookup`` for
dependency gating/priority, ``task_requeue`` for the timeout reaper).  The
extension never edits ``web/api.py``; the WebAPI registers these routes after
the core ones.

Lead wiring: add ``"stackflowos.agents.extension"`` to ``EXTENSION_MODULES`` in
``stackflowos/extensions.py`` (one line, no other change needed).
"""

from __future__ import annotations

from typing import Any

from ..errors import BadRequest, Denied, NotFound
from .git_api import ProjectGitAPI, require_auth

#: Same route list the extension contract consumes.
ROUTES = (
    ("GET", r"/api/agents/queue"),
    ("POST", r"/api/agents/queue/([^/]+)/cancel"),
    ("GET", r"/api/projects/([^/]+)/git"),
    ("POST", r"/api/projects/([^/]+)/git/rollback"),
)


def build(app: Any) -> "AgentExtension | None":
    """Extension entry point (called by ``extensions.load_extensions``)."""
    return AgentExtension(app)


class AgentExtension:
    """Queue control-plane routes and supervisor domain wiring."""

    name = "agents"

    def __init__(self, app: Any) -> None:
        self.app = app
        self.git = ProjectGitAPI(app)
        self._wired = False
        self.wire()

    # -- supervisor plumbing ---------------------------------------------
    def wire(self) -> bool:
        """Install ``task_lookup``/``task_requeue`` on the supervisor.

        Idempotent: ``build(app)`` runs before ``start()`` and both may run
        after a config reload.  ``task_lookup`` makes the supervisor enforce
        task ``deps`` (no dispatching downstream work early) and derive the
        queue priority from the task's priority; ``task_requeue`` lets the
        timeout reaper free a stuck ``doing`` task before re-dispatching it.
        """
        supervisor = getattr(self.app, "supervisor", None)
        projects = getattr(self.app, "projects", None)
        if supervisor is None or projects is None:
            return False
        task_get = getattr(projects, "task_get", None)
        task_update = getattr(projects, "task_update", None)
        if not callable(task_get):
            return False
        supervisor.task_lookup = task_get
        if callable(task_update):
            def requeue(task_id: str):
                task = task_get(task_id)
                if str(getattr(task, "status", "")) == "doing":
                    return task_update(task_id, {"status": "todo"})
                return task

            supervisor.task_requeue = requeue
        self._wired = True
        return True

    def start(self) -> None:
        self.wire()

    # -- extension contract ----------------------------------------------
    def health(self) -> dict:
        supervisor = getattr(self.app, "supervisor", None)
        stats = supervisor.stats() if supervisor is not None else {}
        return {"queue": {"count": stats.get("queued", 0),
                          "max": stats.get("queue_max", 0)},
                "running": stats.get("running", 0),
                "wired": self._wired}

    def routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("GET", r"/api/agents/queue", self.queue_list),
            ("POST", r"/api/agents/queue/([^/]+)/cancel", self.queue_cancel),
            ("GET", r"/api/projects/([^/]+)/git", self.git.project_git),
            ("POST", r"/api/projects/([^/]+)/git/rollback",
             self.git.project_git_rollback),
        ]

    # -- handlers ---------------------------------------------------------
    def _supervisor(self):
        supervisor = getattr(self.app, "supervisor", None)
        if supervisor is None:
            raise NotFound("agent supervisor is unavailable")
        return supervisor

    async def queue_list(self, match: Any, query: dict,
                         body: dict) -> tuple[int, dict]:
        """``GET /api/agents/queue`` -- queued spawns + capacity."""
        require_auth()
        project_id = (query or {}).get("project_id")
        snapshot = self._supervisor().queue_snapshot(
            project_id=str(project_id) if project_id else None)
        return 200, {"ok": True, "data": snapshot}

    async def queue_cancel(self, match: Any, query: dict,
                           body: dict) -> tuple[int, dict]:
        """``POST /api/agents/queue/{id}/cancel`` -- drop a queued spawn."""
        require_auth(operator=True)
        payload = body if isinstance(body, dict) else {}
        reason = str(payload.get("reason") or "operator_requested")
        queue_id = match.group(1)
        try:
            result = self._supervisor().cancel_queued(queue_id, reason=reason)
        except NotFound as exc:
            raise NotFound(f"queued spawn not found: {queue_id}") from exc
        except BadRequest:
            raise
        return 200, {"ok": True, "data": result}


__all__ = ["AgentExtension", "ROUTES", "build"]
