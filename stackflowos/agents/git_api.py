"""HTTP handlers for project-workspace git (extension routes).

Registered by :mod:`stackflowos.agents.extension` through the extension
``routes()`` contract: ``GET /api/projects/{id}/git`` returns branch/HEAD/recent
commits/diff summary and ``POST /api/projects/{id}/git/rollback`` restores a
ref.  Neither route edits ``web/api.py``.

Rollback is a destructive operator action: it re-checks the request role
(fail-closed when a role is present), refuses read-only workspaces and lets
``WorkspaceGit`` validate the ref and keep a backup branch.
"""

from __future__ import annotations

from typing import Any

from ..errors import BadRequest, Denied, NotFound, SfoError
from ..web.api import current_auth
from .workspace_git import GitError, WorkspaceGit, git_available


def _ok(data: Any, status: int = 200) -> tuple[int, dict]:
    return status, {"ok": True, "data": data}


def require_auth(*, operator: bool = False) -> None:
    """Refuse unauthenticated callers; fail closed on a non-operator role.

    ``current_auth()`` is the admission decision set by ``WebAPI.handle``.  It
    is ``None`` for in-process/test calls (no request context), which keeps the
    handlers unit-testable; a real request always has a decision.
    """
    decision = current_auth()
    if decision is None:
        return
    if not bool(getattr(decision, "allowed", False)):
        raise Denied("request was not admitted")
    if not bool(getattr(decision, "authenticated", False)):
        raise Denied("authentication is required")
    role = getattr(decision, "role", None)
    if operator and role is not None and str(role) != "operator":
        raise Denied("operator role is required for this action")


class ProjectGitAPI:
    """Handlers bound to the hub app (one instance per extension)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    # -- helpers ----------------------------------------------------------
    def _security(self) -> Any:
        return getattr(getattr(self.app, "config", None), "security", None)

    def _project(self, project_id: str) -> Any:
        projects = getattr(self.app, "projects", None)
        if projects is None:
            raise NotFound("project store is unavailable")
        return projects.get(str(project_id))

    def _workspace(self, project: Any) -> Any:
        from pathlib import Path

        workspace = str(getattr(project, "workspace", "") or "")
        if not workspace:
            raise BadRequest("project has no workspace")
        path = Path(workspace)
        if not path.exists() or not path.is_dir():
            raise BadRequest(f"project workspace does not exist: {workspace}")
        return path

    def _git(self, project: Any) -> WorkspaceGit:
        return WorkspaceGit(self._workspace(project), logger=self._log())

    def _log(self) -> Any:
        return getattr(self.app, "log", None)

    # -- routes -----------------------------------------------------------
    async def project_git(self, match: Any, query: dict,
                          body: dict) -> tuple[int, dict]:
        """``GET /api/projects/{id}/git`` -- read-only workspace git state."""
        require_auth()
        project = self._project(match.group(1))
        security = self._security()
        enabled = bool(getattr(security, "workspace_git", False))
        data: dict = {
            "project_id": str(getattr(project, "id", "")),
            "workspace": str(getattr(project, "workspace", "")),
            "workspace_mode": str(getattr(project, "workspace_mode", "")),
            "enabled": enabled,
            "git_available": git_available(),
        }
        if not git_available():
            data.update({"repo": False, "reason": "git is not installed"})
            return _ok(data)
        try:
            git = self._git(project)
            info = git.info()
            if not info.get("repo"):
                data.update({"repo": False, "reason": "workspace is not a git repository",
                             "branch": "", "head": "", "dirty": False,
                             "commits": [], "changes": [], "stat": ""})
                return _ok(data)
            summary = git.diff_summary()
            data.update(summary)
            data["repo"] = True
        except GitError as exc:
            data.update({"repo": False, "reason": str(exc)})
        return _ok(data)

    async def project_git_rollback(self, match: Any, query: dict,
                                   body: dict) -> tuple[int, dict]:
        """``POST /api/projects/{id}/git/rollback`` -- restore a ref."""
        require_auth(operator=True)
        project = self._project(match.group(1))
        mode = str(getattr(project, "workspace_mode", "") or "")
        if mode != "attached-rw":
            raise Denied(
                "workspace rollback is only allowed for attached-rw projects")
        payload = body if isinstance(body, dict) else {}
        ref = str(payload.get("ref") or "").strip()
        if not ref:
            raise BadRequest("git ref is required")
        try:
            result = self._git(project).rollback(ref)
        except GitError as exc:
            raise BadRequest(str(exc)) from exc
        result["project_id"] = str(getattr(project, "id", ""))
        try:
            eventlog = getattr(self.app, "eventlog", None)
            if eventlog is not None:
                eventlog.append("project.git_rollback", {
                    "project_id": result["project_id"],
                    "ref": result["ref"],
                    "commit": result["commit"],
                    "previous_head": result["previous_head"],
                    "backup_branch": result["backup_branch"],
                }, source="operator", project_id=result["project_id"])
        except Exception:  # pragma: no cover - audit is best effort
            log = self._log()
            if log is not None:
                log.debug("git rollback audit failed", exc_info=True)
        return _ok(result)


__all__ = ["ProjectGitAPI", "require_auth"]
