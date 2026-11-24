"""Per-task git branch isolation for attached-rw project workspaces.

An attached read-write workspace is somebody's real directory.  Letting several
agents write into it directly makes review and rollback hard, so with
``security.workspace_git`` enabled every task runs on its own branch::

    task/<project>-<task>   (created from the current branch, or reused)

and the runner commits the result when the task ends
(``security.workspace_git_commit``).  ``/api/projects/{id}/git`` exposes the
branch/commit/diff summary and ``.../git/rollback`` restores a ref.

Safety rules enforced here (all git calls are argv lists, never a shell):

* the workspace must *be* a git repository root -- a workspace nested inside a
  larger user repository is refused, so we never reset/commit an outer repo;
* only the project workspace is ever touched (no ``-C`` outside it);
* branch names and refs are validated (no leading ``-``, no whitespace/control
  characters, bounded length) and resolved with ``rev-parse --verify``;
* nothing is pushed, no remote is configured, no branch is deleted; a rollback
  first creates a ``sfo-rollback/*`` backup branch pointing at the previous
  HEAD so the reset itself is recoverable;
* git hooks are disabled (``core.hooksPath=/dev/null``) and prompts/failures are
  bounded by a timeout, so an attached repo cannot block the agent.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..util.logging import get_logger

#: A ref/branch fragment we accept from callers: no shell metacharacters that
#: matter (`;`, `|`, `&`, spaces, quotes, newlines) and no leading dash.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@~^+-]{0,199}$")
_SAFE_FRAGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
#: Branch prefix; ``sfo-rollback/`` is reserved for the safety backup branch.
TASK_BRANCH_PREFIX = "task/"
ROLLBACK_BRANCH_PREFIX = "sfo-rollback/"
#: Hard cap for one git invocation (seconds).
GIT_TIMEOUT_S = 60.0
#: Git configuration forced for every call (no hooks, no prompts, no signing).
_GIT_CONFIG = (
    "-c", "user.name=StackFlowOS",
    "-c", "user.email=agent@stackflowos.local",
    "-c", "commit.gpgsign=false",
    "-c", "tag.gpgsign=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "advice.detachedHead=false",
    "-c", "color.ui=false",
)


class GitError(Exception):
    """A safe, user-facing git failure (never contains secrets)."""


def git_available() -> bool:
    """True when a ``git`` binary is on PATH."""
    return shutil.which("git") is not None


def safe_fragment(value: str, *, limit: int = 64) -> str:
    """Turn a project/task id into a branch-name fragment (no traversal)."""
    text = _SAFE_FRAGMENT_RE.sub("-", str(value or "")).strip("-._")
    text = text[:limit].strip("-._")
    return text or "x"


def task_branch_name(project_id: str, task_id: str | None) -> str:
    """Deterministic branch name for one task (stable across retries)."""
    project = safe_fragment(project_id, limit=40)
    task = safe_fragment(task_id or "unassigned", limit=80)
    return f"{TASK_BRANCH_PREFIX}{project}-{task}"


def validate_ref(ref: str) -> str:
    """Validate a caller-supplied git ref or raise :class:`GitError`."""
    text = str(ref or "").strip()
    if not text:
        raise GitError("git ref is required")
    if len(text) > 200:
        raise GitError("git ref is too long")
    if not _REF_RE.match(text):
        raise GitError(
            "invalid git ref: only letters, digits and ._/@~^+- are allowed")
    if ".." in text or text.startswith("-"):
        raise GitError("invalid git ref")
    return text


def _git_env() -> dict[str, str]:
    """Minimal environment: no prompts, no user/system git config surprises."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "true",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }
    home = os.environ.get("HOME")
    if home:
        env["HOME"] = home
    return env


class WorkspaceGit:
    """Safe git operations confined to one project workspace."""

    def __init__(self, workspace: Path, *, logger: Any = None,
                 timeout_s: float = GIT_TIMEOUT_S,
                 allowed_root: Path | None = None) -> None:
        self.workspace = Path(workspace).expanduser()
        self.log = logger or get_logger("agents.workspace_git")
        self.timeout_s = max(1.0, float(timeout_s))
        self.allowed_root = (Path(allowed_root).resolve()
                             if allowed_root is not None else None)
        if not self.workspace.exists() or not self.workspace.is_dir():
            raise GitError(f"workspace does not exist: {self.workspace}")
        try:
            self.workspace = self.workspace.resolve()
        except OSError as exc:  # pragma: no cover - broken symlink
            raise GitError(f"cannot resolve workspace: {exc}") from exc
        if self.workspace == self.workspace.parent:
            raise GitError("refusing to use the filesystem root as a workspace")
        if self.allowed_root is not None:
            try:
                self.workspace.relative_to(self.allowed_root)
            except ValueError:
                raise GitError("workspace is outside the allowed root") from None

    # -- primitives -------------------------------------------------------
    def _run(self, *args: str, check: bool = True) -> str:
        if not git_available():
            raise GitError("git is not available on this host")
        command = ["git", "-C", str(self.workspace), *_GIT_CONFIG, *args]
        try:
            proc = subprocess.run(
                command, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, timeout=self.timeout_s, env=_git_env(),
                check=False)
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out") from exc
        except OSError as exc:
            raise GitError(f"cannot run git: {exc}") from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            message = detail[-1] if detail else f"exit {proc.returncode}"
            raise GitError(f"git {args[0]} failed: {message[:300]}")
        return proc.stdout if proc.returncode == 0 else ""

    def _ok(self, *args: str) -> bool:
        try:
            self._run(*args, check=True)
        except GitError:
            return False
        return True

    def _repo_root(self) -> str:
        """``rev-parse --show-toplevel`` or '' when this is not a repo."""
        try:
            return self._run("rev-parse", "--show-toplevel").strip()
        except GitError:
            return ""

    def _require_repo_root(self) -> str:
        root = self._repo_root()
        if not root:
            raise GitError("workspace is not a git repository")
        try:
            resolved = Path(root).resolve()
        except OSError:  # pragma: no cover - broken symlink
            resolved = Path(root)
        if resolved != self.workspace:
            raise GitError(
                "workspace is a subdirectory of another git repository; "
                "refusing to operate on the outer repository")
        return root

    def _head(self) -> str:
        try:
            return self._run("rev-parse", "--verify", "HEAD").strip()
        except GitError:
            return ""

    def _branch(self) -> str:
        try:
            name = self._run("rev-parse", "--abbrev-ref", "HEAD").strip()
        except GitError:
            return ""
        return "" if name == "HEAD" else name

    def _recent_commits(self, *, limit: int = 5) -> list[dict]:
        try:
            raw = self._run("log", f"-{max(1, int(limit))}",
                            "--pretty=format:%H%x09%s")
        except GitError:
            return []
        commits: list[dict] = []
        for line in raw.splitlines():
            sha, _, subject = line.partition("\t")
            if sha:
                commits.append({"commit": sha, "subject": subject[:200]})
        return commits

    def _status_entries(self, *, limit: int = 50) -> list[dict]:
        try:
            raw = self._run("status", "--porcelain", "--untracked-files=normal")
        except GitError:
            return []
        entries: list[dict] = []
        for line in raw.splitlines():
            if not line:
                continue
            path = line[3:].strip()
            # Shell/tool logs are diagnostics, not deliverable work.
            if path == ".artifacts" or path.startswith(".artifacts/"):
                continue
            if len(entries) >= max(1, int(limit)):
                break
            code = line[:2].strip() or "??"
            entries.append({"status": code, "path": path[:300]})
        return entries

    # -- public API -------------------------------------------------------
    def info(self) -> dict:
        """Branch/HEAD/repo state without mutating anything."""
        root = self._repo_root()
        if not root:
            return {"workspace": str(self.workspace), "repo": False,
                    "git": git_available()}
        return {
            "workspace": str(self.workspace),
            "repo": True,
            "git": True,
            "root": root,
            "branch": self._branch(),
            "head": self._head(),
            "dirty": bool(self._status_entries()),
        }

    def ensure_repo(self) -> dict:
        """Initialise the workspace as a repo (and an initial commit)."""
        if not git_available():
            raise GitError("git is not available on this host")
        initialised = False
        if not self._repo_root():
            self._run("init", "-q", "--initial-branch=main", check=False)
            if not self._repo_root():
                # Older git (<2.28) has no --initial-branch: retry plain init.
                self._run("init", "-q")
            initialised = bool(self._repo_root())
            if not initialised:
                raise GitError("cannot initialise a git repository here")
        self._require_repo_root()
        if initialised and not self._head():
            self._run("commit", "-q", "--allow-empty", "-m",
                      "chore: initialise project workspace")
        return {**self.info(), "initialised": initialised}

    def task_branch(self, project_id: str, task_id: str | None, *,
                    create: bool = True) -> dict:
        """Check out (or create) the per-task branch and return its state."""
        self._require_repo_root()
        name = task_branch_name(project_id, task_id)
        before = self._branch()
        created = False
        if not self._ok("rev-parse", "--verify", f"refs/heads/{name}"):
            if not create:
                raise GitError(f"task branch does not exist: {name}")
            # ``checkout -b`` needs a commit; an empty repo gets one first.
            if not self._head():
                self._run("commit", "-q", "--allow-empty", "-m",
                          "chore: initialise project workspace")
            self._run("checkout", "-q", "-b", name)
            created = True
        else:
            self._run("checkout", "-q", name)
        return {"branch": name, "created": created, "previous_branch": before,
                "head": self._head(), "workspace": str(self.workspace)}

    def commit_task(self, task_id: str | None, *, status: str = "done",
                    message: str = "") -> dict:
        """Commit the workspace on the current branch (no-op when clean)."""
        self._require_repo_root()
        branch = self._branch()
        entries = self._status_entries_limit(limit=1000)
        if not entries:
            return {"committed": False, "branch": branch, "head": self._head(),
                    "files": 0, "reason": "no changes"}
        self._run("add", "-A", "--", ".", ":(exclude).artifacts")
        text = str(message or "").strip() or (
            f"sfo: {task_id or 'workspace'} ({status})")
        # One line only: a newline would let a caller inject extra paragraphs.
        text = " ".join(text.split())[:200]
        try:
            self._run("commit", "-q", "-m", text)
        except GitError as exc:
            raise GitError(f"commit failed: {exc}") from exc
        return {"committed": True, "branch": branch, "head": self._head(),
                "commit": self._head(), "files": len(entries),
                "message": text}

    def _status_entries_limit(self, *, limit: int) -> list[dict]:
        try:
            raw = self._run("status", "--porcelain", "--untracked-files=normal")
        except GitError:
            return []
        entries: list[dict] = []
        for line in raw.splitlines():
            if not line:
                continue
            path = line[3:].strip()
            if path == ".artifacts" or path.startswith(".artifacts/"):
                continue
            entries.append({"line": line})
            if len(entries) >= max(1, int(limit)):
                break
        return entries

    def diff_summary(self, *, ref: str | None = None,
                     max_commits: int = 5, max_files: int = 50) -> dict:
        """Branch, HEAD, changed files and a bounded diff stat."""
        self._require_repo_root()
        base = ""
        if ref:
            base = self._resolve(ref)
        entries = self._status_entries(limit=max_files)
        diff_args = ["diff", "--stat"]
        diff_args.append(base or "HEAD")
        try:
            stat = self._run(*diff_args).strip()
        except GitError as exc:
            stat = f"(diff unavailable: {exc})"
        return {
            "workspace": str(self.workspace),
            "repo": True,
            "branch": self._branch(),
            "head": self._head(),
            "ref": ref or "",
            "dirty": bool(entries),
            "changes": entries,
            "stat": stat[:4000],
            "commits": self._recent_commits(limit=max_commits),
        }

    def _resolve(self, ref: str) -> str:
        cleaned = validate_ref(ref)
        try:
            commit = self._run("rev-parse", "--verify", "--quiet",
                               f"{cleaned}^{{commit}}").strip()
        except GitError:
            commit = ""
        if not commit:
            raise GitError(f"unknown git ref: {cleaned}")
        return commit

    def rollback(self, ref: str) -> dict:
        """Restore the workspace to ``ref`` (with a backup branch first).

        Refuses to delete branches or touch remotes; the previous HEAD is
        preserved as ``sfo-rollback/<timestamp>`` so the reset can be undone.
        """
        self._require_repo_root()
        commit = self._resolve(ref)
        previous_head = self._head()
        previous_branch = self._branch()
        backup = ""
        if previous_head:
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            backup = f"{ROLLBACK_BRANCH_PREFIX}{stamp}"
            suffix = 1
            while self._ok("rev-parse", "--verify", f"refs/heads/{backup}"):
                suffix += 1
                backup = f"{ROLLBACK_BRANCH_PREFIX}{stamp}-{suffix}"
            self._run("branch", backup, previous_head)
        self._run("reset", "--hard", commit)
        return {
            "ref": str(ref),
            "commit": commit,
            "previous_head": previous_head,
            "previous_branch": previous_branch,
            "backup_branch": backup,
            "branch": self._branch(),
            "head": self._head(),
            "workspace": str(self.workspace),
        }


__all__ = [
    "GIT_TIMEOUT_S",
    "GitError",
    "ROLLBACK_BRANCH_PREFIX",
    "TASK_BRANCH_PREFIX",
    "WorkspaceGit",
    "git_available",
    "safe_fragment",
    "task_branch_name",
    "validate_ref",
]
