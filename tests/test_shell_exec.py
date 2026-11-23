"""H4/L4: shell.exec process-group, timeout, bounded-output and log hardening."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from stackflowos.agents.models import Agent
from stackflowos.agents.tools import (OUTPUT_CAP_BYTES, ToolError,
                                      ToolExecutor)
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

    @unittest.skipUnless(IS_LINUX, "requires POSIX process groups")
    async def test_non_positive_timeout_falls_back_to_default(self) -> None:
        executor = self._executor(shell_timeout_s=0.5)
        for bad in (-1.0, 0.0):
            started = time.monotonic()
            result = await asyncio.wait_for(
                executor.run("shell.exec",
                             {"command": "sleep 30", "timeout_s": bad}),
                timeout=3.0)
            self.assertTrue(result.ok, result.error)
            self.assertTrue(result.data["timed_out"], f"timeout_s={bad!r}")
            self.assertLess(time.monotonic() - started, 3.0)

    # -- L5: non-finite timeouts and the upper bound ----------------------
    async def test_non_finite_timeout_rejected_before_spawn(self) -> None:
        bad_values = (float("nan"), float("inf"), float("-inf"),
                      float("1e309"), "5", True, None)
        for index, bad in enumerate(bad_values):
            result = await self.executor.run(
                "shell.exec",
                {"command": f"touch spawned-{index}", "timeout_s": bad})
            self.assertFalse(result.ok, f"timeout_s={bad!r} must be rejected")
            self.assertIn("timeout_s", result.error or "")
            self.assertIn("number", result.error or "")
        # No process was spawned and no approval was even requested.
        self.assertFalse(list(self.ws.glob("spawned-*")),
                         "invalid timeout_s must not execute the command")
        self.assertEqual(self.executor.sys.calls, [],
                         "invalid timeout_s must fail before any RPC")

    async def test_huge_timeout_clamped_to_config_max(self) -> None:
        executor = self._executor(shell_max_timeout_s=1)
        started = time.monotonic()
        result = await asyncio.wait_for(
            executor.run("shell.exec",
                         {"command": "sleep 30", "timeout_s": 1e9}),
            timeout=5.0)
        elapsed = time.monotonic() - started
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.data["timed_out"])
        self.assertLess(elapsed, 3.0,
                        "timeout_s=1e9 must be clamped to shell_max_timeout_s")

    def test_resolved_timeout_is_bounded_and_finite(self) -> None:
        executor = self._executor(shell_max_timeout_s=600)
        self.assertEqual(executor._resolve_shell_timeout({"timeout_s": 1e9}),
                         600.0)
        self.assertEqual(executor._resolve_shell_timeout({}), 60.0)
        self.assertEqual(executor._resolve_shell_timeout({"timeout_s": 0}),
                         60.0)
        self.assertEqual(executor._resolve_shell_timeout({"timeout_s": -5}),
                         60.0)
        self.assertEqual(executor._resolve_shell_timeout({"timeout_s": 2.5}),
                         2.5)
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad), self.assertRaises(ToolError):
                executor._resolve_shell_timeout({"timeout_s": bad})

    def test_unusable_or_oversized_config_cap_is_sanitized(self) -> None:
        # Directly-built SecurityCfg bypasses Config validation; the executor
        # must still produce a finite, sane cap.
        for cap in (0, -10, 10 ** 6):
            executor = self._executor(shell_max_timeout_s=cap)
            cap_value = executor._shell_max_timeout()
            self.assertTrue(0 < cap_value <= 3600.0, cap)
            self.assertEqual(executor._resolve_shell_timeout({"timeout_s": 1e9}),
                             cap_value)

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

    # -- L4: log file permissions, redaction and size cap -----------------
    async def test_log_file_is_0600_and_missing_dir_is_created(self) -> None:
        artifacts = self.ws / ".artifacts"
        self.assertFalse(artifacts.exists(), "test workspace must start clean")
        result = await self.executor.run("shell.exec", {"command": "echo hi"})
        self.assertTrue(result.ok, result.error)
        path = Path(result.data["log"])
        self.assertTrue(path.exists())
        self.assertTrue(path.parent == artifacts)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        # The parent is created 0700; an existing one is never chmodded.
        self.assertTrue(artifacts.is_dir())
        self.assertEqual(stat.S_IMODE(artifacts.stat().st_mode) & 0o077, 0)

    async def test_log_redacts_header_stdout_and_stderr(self) -> None:
        # Tokens are assembled inside the shell so they are absent from the
        # (redacted) header: only the stream redaction can hide them.
        command = (
            "printf 'Authorization: Bearer %s\\n' \"$(printf abc)defgh\"; "
            "printf 'api_key=sk-%s\\n' 123456; "
            "printf 'Authorization: Bearer %s\\n' \"$(printf xyz)secret\" >&2"
        )
        result = await self.executor.run("shell.exec", {"command": command})
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.data["log"])
        text = Path(result.data["log"]).read_text(encoding="utf-8")
        for secret in ("abcdefgh", "sk-123456", "xyzsecret"):
            self.assertNotIn(secret, text, f"{secret} leaked into the log")
        self.assertIn("***", text)
        self.assertIn("Authorization: ***", text)

    async def test_log_header_redacts_command_secrets(self) -> None:
        result = await self.executor.run("shell.exec", {
            "command": "true # Authorization: Bearer abcdefghijkl",
        })
        self.assertTrue(result.ok, result.error)
        text = Path(result.data["log"]).read_text(encoding="utf-8")
        self.assertNotIn("abcdefghijkl", text)
        self.assertIn("$ true # Authorization: ***", text)

    async def test_log_is_truncated_at_configured_max_bytes(self) -> None:
        cap = 1024 * 1024
        executor = self._executor(shell_log_max_bytes=cap)
        script = "import sys; sys.stdout.write('x' * (3 * 1024 * 1024))"
        result = await executor.run("shell.exec", {
            "command": f'{sys.executable} -c "{script}"', "timeout_s": 30,
        })
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["exit_code"], 0)
        self.assertFalse(result.data["timed_out"])
        data = Path(result.data["log"]).read_bytes()
        marker = f"...[log truncated at {cap} bytes]...".encode("utf-8")
        self.assertIn(marker, data)
        # Exactly the cap plus the single marker: no unbounded disk growth.
        self.assertEqual(len(data), cap + len(b"\n" + marker + b"\n"))
        # The result's own cap/truncated flag is independent of the log cap.
        self.assertTrue(result.data["truncated"])

    def test_shell_log_cap_is_sanitized(self) -> None:
        default = 64 * 1024 * 1024
        self.assertEqual(self._executor()._shell_log_max_bytes(), default)
        for bad in (0, -1, "many", None, True):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self._executor(shell_log_max_bytes=bad)._shell_log_max_bytes(),
                    default)
        self.assertEqual(
            self._executor(shell_log_max_bytes=10)._shell_log_max_bytes(),
            1024 * 1024)
        self.assertEqual(
            self._executor(shell_log_max_bytes=10 ** 12)._shell_log_max_bytes(),
            1024 ** 3)

    async def test_unwritable_artifacts_dir_yields_empty_log(self) -> None:
        directory = self.ws / ".artifacts"
        directory.mkdir()
        if os.name != "posix" or os.geteuid() == 0:
            self.skipTest("directory permissions are not enforced here")
        os.chmod(directory, 0o500)
        try:
            result = await self.executor.run("shell.exec",
                                             {"command": "echo hi"})
        finally:
            os.chmod(directory, 0o700)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["stdout"], "hi\n")
        self.assertEqual(result.data["log"], "")

    async def test_log_write_failure_degrades_to_empty_log(self) -> None:
        original = ToolExecutor._open_shell_log

        class _BrokenHandle:
            def __init__(self, real: object) -> None:
                self.real = real

            def write(self, _data: bytes) -> None:
                raise OSError("disk full")

            def close(self) -> None:
                self.real.close()

        def open_with_broken_handle(self_: ToolExecutor, command: str,
                                    cwd: Path):
            log = original(self_, command, cwd)
            if log is not None:
                log._handle = _BrokenHandle(log._handle)
            return log

        with mock.patch.object(ToolExecutor, "_open_shell_log",
                               open_with_broken_handle):
            result = await self.executor.run("shell.exec",
                                             {"command": "echo hi"})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["stdout"], "hi\n")
        self.assertEqual(result.data["log"], "")

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
