"""Import-guarded security.sandbox wiring (shell.exec + worker process)."""

from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from stackflowos.agents import sandbox as sandbox_module
from stackflowos.agents.sandbox import (apply_process_sandbox, probe,
                                        reset_warning_cache,
                                        wrap_shell_command)
from stackflowos.config import SecurityCfg

TMP_ROOT = Path(__file__).resolve().parent.parent / ".stackflowos" / "tmp"


def _fake_sandbox(**overrides) -> ModuleType:
    module = ModuleType("stackflowos.security.sandbox")
    module.BACKEND = "fake-landlock"

    def wrap_shell_command(command, *, allow_network=False, tmp_mb=256,
                           cwd="."):
        return f"echo sandboxed-network={int(bool(allow_network))}\n{command}"

    def restrict_current_process(*, writable_paths, allow_network=False,
                                 backend=None):
        return {"backend": "fake-landlock",
                "writable_paths": list(writable_paths),
                "allow_network": bool(allow_network)}

    module.wrap_shell_command = wrap_shell_command
    module.restrict_current_process = restrict_current_process
    for name, value in overrides.items():
        setattr(module, name, value)
    return module


def _real_sandbox_module():
    try:
        return importlib.import_module("stackflowos.security.sandbox")
    except Exception:
        return None


def _has_landlock() -> bool:
    module = _real_sandbox_module()
    return bool(module is not None
                and callable(getattr(module, "restrict_current_process", None))
                and sys.platform.startswith("linux"))


class SandboxUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_warning_cache()

    def test_off_mode_is_a_no_op(self) -> None:
        cfg = SecurityCfg(sandbox="off")
        report = wrap_shell_command(cfg, "echo hi", cwd=".")
        self.assertEqual(report["status"], "disabled")
        self.assertEqual(report["command"], "echo hi")
        self.assertFalse(report["use_exec"])
        process = apply_process_sandbox(cfg, ["/tmp"])
        self.assertEqual(process["status"], "disabled")
        self.assertEqual(probe(cfg)["status"], "disabled")

    def test_missing_module_keeps_original_behaviour(self) -> None:
        cfg = SecurityCfg(sandbox="landlock")
        with mock.patch.object(sandbox_module, "fetch_module",
                               return_value=None):
            report = wrap_shell_command(cfg, "echo hi", cwd=".")
            self.assertEqual(report["status"], "unavailable")
            self.assertEqual(report["command"], "echo hi")
            process = apply_process_sandbox(cfg, ["/tmp/work"])
            self.assertEqual(process["status"], "unavailable")
            self.assertIn("not available", process["reason"])

    def test_process_sandbox_passes_writable_paths_and_network_flag(self) -> None:
        calls: list[dict] = []

        def restrict(*, writable_paths, allow_network=False):
            calls.append({"writable_paths": list(writable_paths),
                          "allow_network": allow_network})
            return {"backend": "fake-landlock"}

        module = _fake_sandbox(restrict_current_process=restrict)
        cfg = SecurityCfg(sandbox="auto", sandbox_allow_network=True)
        with mock.patch.object(sandbox_module, "fetch_module",
                               return_value=module):
            report = apply_process_sandbox(cfg, ["/tmp/work", "/tmp/state"])
        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["backend"], "fake-landlock")
        self.assertEqual(calls[0]["writable_paths"], ["/tmp/work", "/tmp/state"])
        self.assertTrue(calls[0]["allow_network"])

    def test_process_sandbox_failure_degrades(self) -> None:
        def restrict(**_kwargs):
            raise RuntimeError("landlock unavailable")

        module = _fake_sandbox(restrict_current_process=restrict)
        cfg = SecurityCfg(sandbox="landlock")
        with mock.patch.object(sandbox_module, "fetch_module",
                               return_value=module):
            report = apply_process_sandbox(cfg, ["/tmp/work"])
        self.assertEqual(report["status"], "failed")
        self.assertIn("landlock unavailable", report["reason"])


class ShellSandboxWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.ws = self.tmp / "workspace"
        self.ws.mkdir()
        reset_warning_cache()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _run_shell(self, command: str, cfg: SecurityCfg):
        from stackflowos.agents.models import Agent
        from stackflowos.agents.tools import ToolExecutor
        from tests.test_agent_tools import FakeSysAPI

        sys_api = FakeSysAPI()
        agent = Agent(id="agent-coder-1", role="coder", project_id="p-1",
                      task_id="t-1")
        executor = ToolExecutor(sys_api, agent=agent, workspace=self.ws,
                                security_config=cfg)
        return await executor.run("shell.exec", {"command": command})

    async def test_shell_wrapper_string_is_used(self) -> None:
        module = _fake_sandbox()
        cfg = SecurityCfg(sandbox="auto")
        with mock.patch.object(sandbox_module, "fetch_module",
                               return_value=module):
            result = await self._run_shell("echo payload", cfg)
        self.assertTrue(result.ok, result.error)
        self.assertIn("sandboxed-network=0", result.data["stdout"])
        self.assertIn("payload", result.data["stdout"])
        self.assertEqual(result.data["sandbox"]["status"], "wrapped")

    async def test_shell_wrapper_argv_uses_exec(self) -> None:
        def wrap(command, **_kwargs):
            return ["/bin/sh", "-c", "echo argv-mode"]

        module = _fake_sandbox(wrap_shell_command=wrap)
        cfg = SecurityCfg(sandbox="auto")
        with mock.patch.object(sandbox_module, "fetch_module",
                               return_value=module):
            result = await self._run_shell("echo ignored", cfg)
        self.assertTrue(result.ok, result.error)
        self.assertIn("argv-mode", result.data["stdout"])
        self.assertEqual(result.data["sandbox"]["status"], "wrapped")

    async def test_shell_wrapper_failure_runs_unsandboxed(self) -> None:
        def wrap(command, **_kwargs):
            raise RuntimeError("bwrap missing")

        module = _fake_sandbox(wrap_shell_command=wrap)
        cfg = SecurityCfg(sandbox="bwrap")
        with mock.patch.object(sandbox_module, "fetch_module",
                               return_value=module):
            result = await self._run_shell("echo fallback", cfg)
        self.assertTrue(result.ok, result.error)
        self.assertIn("fallback", result.data["stdout"])
        self.assertEqual(result.data["sandbox"]["status"], "failed")
        self.assertEqual(result.data["sandbox"]["mode"], "bwrap")

    async def test_off_mode_does_not_touch_the_command(self) -> None:
        cfg = SecurityCfg(sandbox="off")
        result = await self._run_shell("echo plain", cfg)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["stdout"].strip(), "plain")
        self.assertEqual(result.data["sandbox"]["status"], "disabled")


@unittest.skipUnless(_has_landlock(), "security.sandbox Landlock is unavailable")
class RealLandlockTests(unittest.TestCase):
    """Host check: the worker sandbox limits writes to the whitelist."""

    def test_restrict_current_process_limits_writes(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        inside = tmp / "inside"
        inside.mkdir()
        outside = Path(tempfile.mkdtemp(prefix="sfo-outside-"))
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from stackflowos.agents.sandbox import apply_process_sandbox\n"
            "from stackflowos.config import SecurityCfg\n"
            "report = apply_process_sandbox("
            "SecurityCfg(sandbox='landlock'), [sys.argv[1]])\n"
            "print(report['status'])\n"
            "if report['status'] != 'applied':\n"
            "    sys.exit(3)\n"
            "Path(sys.argv[1], 'ok.txt').write_text('ok')\n"
            "try:\n"
            "    Path(sys.argv[2], 'bad.txt').write_text('bad')\n"
            "except (PermissionError, OSError):\n"
            "    print('DENIED')\n"
            "else:\n"
            "    print('ALLOWED')\n"
            "    sys.exit(4)\n"
        )
        try:
            proc = __import__("subprocess").run(
                [sys.executable, "-c", script, str(inside), str(outside)],
                capture_output=True, text=True, timeout=30,
                cwd=str(Path(__file__).resolve().parent.parent))
            if proc.returncode == 3:
                self.skipTest("Landlock backend reported unavailable")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("applied", proc.stdout)
            self.assertIn("DENIED", proc.stdout)
            self.assertTrue((inside / "ok.txt").is_file())
            self.assertFalse((outside / "bad.txt").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
