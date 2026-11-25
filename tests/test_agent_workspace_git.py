"""Per-task git branch isolation for attached-rw project workspaces."""

from __future__ import annotations

import shutil
import subprocess
import unittest

from stackflowos.agents.brain import MockBrain
from stackflowos.agents.models import Agent
from stackflowos.agents.runner import AgentRunner
from stackflowos.agents.tools import ToolExecutor
from stackflowos.agents.workspace_git import (DirtyWorkspaceError, GitError,
                                              WorkspaceGit, git_available,
                                              task_branch_name, validate_ref)
from stackflowos.config import AgentsCfg, SecurityCfg
from tests.conftest import git_scratch_dir
from tests.test_agent_runner import FakeSysAPI

GIT = git_available()


@unittest.skipUnless(GIT, "git binary is not available")
class WorkspaceGitTests(unittest.TestCase):
    def setUp(self) -> None:
        # Outside the checkout: git must see an isolated repo root (a
        # workspace nested in another repository is refused by design).
        self.tmp = git_scratch_dir()
        self.ws = self.tmp / "workspace"
        self.ws.mkdir()
        (self.ws / "README.md").write_text("hello\n", encoding="utf-8")
        self.git = WorkspaceGit(self.ws)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _commits(self) -> int:
        return int(subprocess.check_output(
            ["git", "-C", str(self.ws), "rev-list", "--count", "HEAD"],
            text=True).strip())

    def test_ensure_repo_and_task_branch(self) -> None:
        info = self.git.ensure_repo()
        self.assertTrue(info["repo"])
        self.assertTrue(info["head"])
        self.assertEqual(self._commits(), 1)
        branch = self.git.task_branch("p-1", "t-1")
        self.assertEqual(branch["branch"], "task/p-1-t-1")
        self.assertTrue(branch["created"])
        again = self.git.task_branch("p-1", "t-1")
        self.assertFalse(again["created"])
        other = self.git.task_branch("p-1", "t-2")
        self.assertEqual(other["branch"], "task/p-1-t-2")

    def test_commit_task_and_diff_summary(self) -> None:
        self.git.ensure_repo()
        self.git.task_branch("p-1", "t-1")
        (self.ws / "src.txt").write_text("code\n", encoding="utf-8")
        summary = self.git.diff_summary()
        self.assertTrue(summary["dirty"])
        self.assertTrue(any(item["path"] == "src.txt"
                            for item in summary["changes"]))
        commit = self.git.commit_task("t-1", status="done")
        self.assertTrue(commit["committed"])
        # README.md was still untracked plus the new src.txt.
        self.assertGreaterEqual(commit["files"], 1)
        self.assertTrue(commit["commit"])
        clean = self.git.diff_summary()
        self.assertFalse(clean["dirty"])
        self.assertGreaterEqual(len(clean["commits"]), 1)
        # Nothing to commit is a no-op, not an error.
        again = self.git.commit_task("t-1", status="done")
        self.assertFalse(again["committed"])

    def test_rollback_restores_ref_and_keeps_backup_branch(self) -> None:
        self.git.ensure_repo()
        self.git.task_branch("p-1", "t-1")
        first = self.git.commit_task("t-1")
        self.assertTrue(first["committed"])
        (self.ws / "later.txt").write_text("later\n", encoding="utf-8")
        second = self.git.commit_task("t-1")
        self.assertTrue(second["committed"])
        result = self.git.rollback(first["commit"])
        self.assertEqual(result["previous_head"], second["commit"])
        self.assertFalse((self.ws / "later.txt").exists())
        self.assertTrue(result["backup_branch"].startswith("sfo-rollback/"))
        branches = subprocess.check_output(
            ["git", "-C", str(self.ws), "branch", "--list"], text=True)
        self.assertIn(result["backup_branch"], branches)
        self.assertIn("task/p-1-t-1", branches)

    def test_invalid_refs_are_rejected_before_any_git_write(self) -> None:
        self.git.ensure_repo()
        for bad in ("", "-x", "--hard", "a b", "a;b", "../main", "x" * 300):
            with self.assertRaises(GitError):
                validate_ref(bad)
        with self.assertRaises(GitError):
            self.git.rollback("does-not-exist")
        with self.assertRaises(GitError):
            self.git.rollback("-x")

    def test_rollback_refuses_dirty_workspace(self) -> None:
        self.git.ensure_repo()
        self.git.task_branch("p-1", "t-1")
        head = self.git.commit_task("t-1")["commit"]
        (self.ws / "README.md").write_text("local edit\n", encoding="utf-8")

        with self.assertRaises(DirtyWorkspaceError) as caught:
            self.git.rollback(head)
        error = caught.exception
        self.assertEqual(error.code, "GIT_WORKSPACE_DIRTY")
        self.assertIn("force=True", str(error))
        self.assertIn("refs/sfo-rollback-stash/", str(error))
        self.assertIn("force=True", error.hint)
        self.assertEqual([item["path"] for item in error.changes],
                         ["README.md"])
        # Refused before any write: the edit and HEAD are untouched, and no
        # backup branch was fabricated for a rollback that never happened.
        self.assertEqual((self.ws / "README.md").read_text(encoding="utf-8"),
                         "local edit\n")
        self.assertEqual(self.git.info()["head"], head)
        branches = subprocess.check_output(
            ["git", "-C", str(self.ws), "branch", "--list", "sfo-rollback/*"],
            text=True)
        self.assertEqual(branches.strip(), "")

    def test_forced_rollback_stashes_dirty_work_in_a_ref(self) -> None:
        self.git.ensure_repo()
        self.git.task_branch("p-1", "t-1")
        head = self.git.commit_task("t-1")["commit"]
        (self.ws / "README.md").write_text("local edit\n", encoding="utf-8")
        (self.ws / "scratch.txt").write_text("untracked\n", encoding="utf-8")

        result = self.git.rollback(head, force=True)
        self.assertTrue(result["force"])
        self.assertTrue(result["dirty_before"])
        self.assertIn("README.md",
                      [item["path"] for item in result["changes"]])
        self.assertTrue(
            result["stash_ref"].startswith("refs/sfo-rollback-stash/"),
            result["stash_ref"])
        self.assertEqual(result["restore_hint"],
                         f"git stash apply {result['stash_ref']}")
        # The tracked edit was reset away...
        self.assertEqual((self.ws / "README.md").read_text(encoding="utf-8"),
                         "hello\n")
        # ... but it survives in the stash ref and can be restored.
        apply = subprocess.run(
            ["git", "-C", str(self.ws), "stash", "apply",
             result["stash_ref"]], capture_output=True, text=True)
        self.assertEqual(apply.returncode, 0, apply.stderr)
        self.assertEqual((self.ws / "README.md").read_text(encoding="utf-8"),
                         "local edit\n")
        # reset --hard never deletes untracked files, so they are still there.
        self.assertEqual((self.ws / "scratch.txt").read_text(encoding="utf-8"),
                         "untracked\n")
        self.assertTrue(result["backup_branch"].startswith("sfo-rollback/"))
        self.assertEqual(self.git.info()["head"], head)

    def test_forced_rollback_with_only_untracked_changes_needs_no_stash(
            self) -> None:
        self.git.ensure_repo()
        self.git.task_branch("p-1", "t-1")
        head = self.git.commit_task("t-1")["commit"]
        (self.ws / "scratch.txt").write_text("untracked\n", encoding="utf-8")
        result = self.git.rollback(head, force=True)
        # Nothing tracked was dirty, so there is nothing to preserve; the
        # untracked file is left in place by git itself.
        self.assertEqual(result["stash_ref"], "")
        self.assertNotIn("restore_hint", result)
        self.assertTrue(result["dirty_before"])
        self.assertEqual((self.ws / "scratch.txt").read_text(encoding="utf-8"),
                         "untracked\n")

    def test_refuses_nested_workspace_of_outer_repo(self) -> None:
        outer = self.tmp / "outer"
        outer.mkdir()
        subprocess.check_call(["git", "init", "-q", str(outer)])
        nested = outer / "sub"
        nested.mkdir()
        git = WorkspaceGit(nested)
        with self.assertRaises(GitError) as caught:
            git.ensure_repo()
        self.assertIn("subdirectory", str(caught.exception))

    def test_allowed_root_is_enforced(self) -> None:
        other = self.tmp / "elsewhere"
        other.mkdir()
        with self.assertRaises(GitError):
            WorkspaceGit(self.ws, allowed_root=other)

    def test_branch_name_is_sanitised(self) -> None:
        name = task_branch_name("../../etc", "t 1; rm -rf /")
        self.assertTrue(name.startswith("task/"))
        self.assertNotIn("..", name)
        self.assertNotIn(" ", name)
        self.assertNotIn(";", name)
        self.assertNotIn("../", name)


@unittest.skipUnless(GIT, "git binary is not available")
class RunnerGitIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = git_scratch_dir(prefix="sfo-git-run-")
        self.ws = self.tmp / "attached"
        self.ws.mkdir()
        (self.ws / "README.md").write_text("base\n", encoding="utf-8")
        self.task = {"id": "t-1", "project_id": "p-1", "title": "写模块",
                     "description": "实现 hello 函数并自检", "status": "todo",
                     "role": "coder", "artifacts": [], "notes": []}
        self.sys = FakeSysAPI(task=self.task)
        self.sys.project = {
            "id": "p-1", "name": "Demo", "goal": "x", "status": "active",
            "workspace": str(self.ws), "workspace_mode": "attached-rw",
        }
        self.agent = Agent(id="agent-coder-1", role="coder",
                           project_id="p-1", task_id="t-1")
        self.security = SecurityCfg(workspace_git=True,
                                    workspace_git_commit=True)
        self.executor = ToolExecutor(
            self.sys, agent=self.agent, workspace=self.ws,
            security_config=self.security)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_runner_creates_task_branch_and_commits_at_finish(self) -> None:
        runner = AgentRunner(self.sys, self.agent,
                             MockBrain("coder", max_steps=8), self.executor,
                             config=AgentsCfg(max_steps=8), workspace=self.ws)
        result = await runner.run()
        self.assertEqual(result.status, "done", result.summary)
        git = WorkspaceGit(self.ws)
        info = git.info()
        self.assertTrue(info["repo"])
        self.assertEqual(info["branch"], "task/p-1-t-1")
        self.assertFalse(info["dirty"], "task files must be committed")
        log = subprocess.check_output(
            ["git", "-C", str(self.ws), "log", "--pretty=%s"], text=True)
        self.assertIn("sfo: t-1 (done)", log)
        sources = list((self.ws / "src").glob("*.py"))
        self.assertEqual(len(sources), 1)

    async def test_runner_with_git_disabled_touches_nothing(self) -> None:
        self.security.workspace_git = False
        runner = AgentRunner(self.sys, self.agent,
                             MockBrain("coder", max_steps=8), self.executor,
                             config=AgentsCfg(max_steps=8), workspace=self.ws)
        result = await runner.run()
        self.assertEqual(result.status, "done", result.summary)
        self.assertFalse((self.ws / ".git").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
