"""H4: shell.exec process-group, timeout and bounded-output hardening."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from stackflowos.agents.models import Agent
from stackflowos.agents.tools import OUTPUT_CAP_BYTES, ToolExecutor
from stackflowos.config import SecurityCfg

TMP_ROOT = Path(__file__).resolve().parent.parent / ".stackflowos" / "tmp"
IS_LINUX = sys.platform.startswith("linux")


class FakeSysAPI:
    """Minimal sys stand-in: records calls and auto-approves every request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.events: list[dict] = []

    async def call(self, action: str, data: dict | None = None, *,
                   timeout: float | None = None) -> dict:
        self.calls.append((action, dict(data or {})))
        if action == "event.append":
            self.events.append(dict(data or {}))
            return {"seq": len(self.events)}
        if action in ("approval.request", "approval.status"):
            return {"id": "apr-h4", "status": "approved", "mode": "rule",
                    "risk": "low", "grant_id": None}
        if action == "approval.use":
            return {"ok": True}
        return {}


class ShellExecHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.ws = self.tmp / "workspace"
        self.ws.mkdir()
        self.executor = self._executor()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _executor(self, **config: object) -> ToolExecutor:
        return ToolExecutor(
            FakeSysAPI(),
            agent=Agent(id="agent-h4", role="coder",
                        project_id="p-1", task_id="t-1"),
            workspace=self.ws,
            security_config=SecurityCfg(**config),
        )

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        return Path(f"/proc/{pid}").exists()

    @staticmethod
    def _capture_tasks() -> list[asyncio.Task]:
        current = asyncio.current_task()
        return [task for task in asyncio.all_tasks()
                if task is not current
                and task.get_name() in ("shell-stdout", "shell-stderr")]

    # -- timeouts and process groups --------------------------------------
    @unittest.skipUnless(IS_LINUX, "requires /proc and POSIX process groups")
    async def test_timeout_kills_pipe_holding_grandchild(self) -> None:
        # The background sleep inherits stdout, so the pipe never reaches EOF
        # even though the shell exits; the old code hung here forever.
        command = "sh -c 'sleep 30 & echo $! > sleep.pid; echo hi'"
        started = time.monotonic()
        result = await asyncio.wait_for(
            self.executor.run("shell.exec",
                              {"command": command, "timeout_s": 0.5}),
            timeout=3.0)
        elapsed = time.monotonic() - started
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.data["timed_out"])
        self.assertLess(elapsed, 3.0, "timed-out shell must return promptly")
        pid = int((self.ws / "sleep.pid").read_text().strip())
        self.assertFalse(self._pid_alive(pid),
                         f"grandchild sleep {pid} survived the timeout")
        self.assertEqual(self._capture_tasks(), [])

    @unittest.skipUnless(IS_LINUX, "requires /proc and POSIX process groups")
    async def test_timeout_kills_detached_descendant(self) -> None:
        pid_file = self.ws / "child.pid"
        script = (
            "import subprocess; "
            "child = subprocess.Popen(['sleep', '30']); "
            f"open({str(pid_file)!r}, 'w').write(str(child.pid)); "
            "print('x')"
        )
        started = time.monotonic()
        result = await asyncio.wait_for(
            self.executor.run("shell.exec", {
                "command": f'{sys.executable} -c "{script}"',
                "timeout_s": 0.5,
            }),
            timeout=3.0)
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.data["timed_out"])
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertIn("x", result.data["stdout"])
        pid = int(pid_file.read_text().strip())
        self.assertFalse(self._pid_alive(pid),
                         f"detached sleep {pid} survived the timeout")
        self.assertEqual(self._capture_tasks(), [])

    @unittest.skipUnless(IS_LINUX, "requires /proc and POSIX process groups")
    async def test_invalid_timeout_falls_back_to_default(self) -> None:
        executor = self._executor(shell_timeout_s=0.5)
        for bad in (float("nan"), float("inf"), float("-inf"), -1.0, 0.0):
            started = time.monotonic()
            result = await asyncio.wait_for(
                executor.run("shell.exec",
                             {"command": "sleep 30", "timeout_s": bad}),
                timeout=3.0)
            self.assertTrue(result.ok, result.error)
            self.assertTrue(result.data["timed_out"], f"timeout_s={bad!r}")
            self.assertLess(time.monotonic() - started, 3.0)

    # -- bounded output ---------------------------------------------------
    async def test_large_output_is_capped_and_truncated(self) -> None:
        script = "print('x' * 10000000)"
        result = await self.executor.run("shell.exec", {
            "command": f'{sys.executable} -c "{script}"',
            "timeout_s": 30,
        })
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["exit_code"], 0)
        self.assertFalse(result.data["timed_out"])
        self.assertTrue(result.data["truncated"])
        self.assertLessEqual(len(result.data["stdout"]), OUTPUT_CAP_BYTES)
        log_path = Path(result.data["log"])
        self.assertTrue(log_path.exists())
        # The full output must still be streamed to disk, not kept in memory.
        self.assertGreaterEqual(log_path.stat().st_size, 10_000_000)

    async def test_unwritable_log_dir_does_not_fail_command(self) -> None:
        (self.ws / ".artifacts").write_text("not a directory", encoding="utf-8")
        result = await self.executor.run("shell.exec", {"command": "echo hi"})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["stdout"], "hi\n")
        self.assertEqual(result.data["log"], "")

    # -- normal behaviour and cancellation --------------------------------
    async def test_normal_command_returns_streams_and_exit_code(self) -> None:
        result = await self.executor.run("shell.exec", {
            "command": "printf out; printf err >&2; exit 3",
        })
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["exit_code"], 3)
        self.assertEqual(result.data["stdout"], "out")
        self.assertEqual(result.data["stderr"], "err")
        self.assertFalse(result.data["truncated"])
        self.assertFalse(result.data["timed_out"])
        self.assertEqual(result.data["cwd"], str(self.ws))
        log_path = Path(result.data["log"])
        self.assertTrue(log_path.exists())
        self.assertTrue(str(log_path).startswith(str(self.ws / ".artifacts")))

    async def test_concurrent_commands_get_distinct_logs(self) -> None:
        results = await asyncio.gather(*[
            self.executor.run("shell.exec", {"command": "echo hi"})
            for _ in range(2)
        ])
        logs = {result.data["log"] for result in results}
        self.assertEqual(len(logs), 2)
        for log in logs:
            self.assertTrue(Path(log).exists())

    @unittest.skipUnless(IS_LINUX, "requires /proc and POSIX process groups")
    async def test_cancellation_kills_group_and_leaves_no_tasks(self) -> None:
        task = asyncio.create_task(self.executor.run("shell.exec", {
            "command": "sh -c 'echo $$ > shell.pid; sleep 30'",
            "timeout_s": 30,
        }))
        pid_file = self.ws / "shell.pid"
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        self.assertTrue(pid_file.exists(), "command never started")
        pid = int(pid_file.read_text().strip())

        started = time.monotonic()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertFalse(self._pid_alive(pid),
                         f"shell {pid} survived cancellation")
        self.assertEqual(self._capture_tasks(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
