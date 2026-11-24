"""Sandbox capability detection, command wrapping and real Landlock checks."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stackflowos.config import default_root
from stackflowos.security import sandbox

REPO = default_root()


class DetectionTests(unittest.TestCase):
    def test_detect_landlock_shape(self):
        status = sandbox.detect_landlock()
        self.assertIsInstance(status, sandbox.LandlockStatus)
        self.assertIsInstance(status.available, bool)
        if status.available:
            self.assertGreaterEqual(status.abi, 1)
            self.assertEqual(status.reason, "")
        else:
            self.assertTrue(status.reason)

    def test_detect_bwrap_is_functional_not_which(self):
        info = sandbox.detect_bwrap()
        self.assertEqual(info.backend, "bwrap")
        self.assertIsInstance(info.available, bool)
        self.assertTrue(info.detail)
        if not info.available:
            # A binary present but with unusable namespaces must still be
            # reported as unavailable with the reason.
            self.assertTrue(info.detail)

    def test_detect_backend_returns_known_names(self):
        self.assertIn(sandbox.detect_backend("auto"),
                      {"landlock", "bwrap", "unsandboxed"})
        self.assertEqual(sandbox.detect_backend("off"), "unsandboxed")
        self.assertEqual(sandbox.detect_backend("none"), "unsandboxed")
        with self.assertRaises(ValueError):
            sandbox.detect_backend("magic")

    def test_auto_prefers_landlock_when_available(self):
        if sandbox.detect_landlock().available:
            self.assertEqual(sandbox.detect_backend("auto"), "landlock")

    def test_backends_snapshot(self):
        infos = sandbox.detect_backends()
        self.assertEqual(set(infos), {"landlock", "bwrap"})
        self.assertEqual(infos["bwrap"].backend, "bwrap")


class BuildCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sandbox_off_is_explicitly_unsandboxed(self):
        argv, backend, notes = sandbox.build_command(
            ["echo", "hi"], backend="off")
        self.assertEqual(argv, ["echo", "hi"])
        self.assertEqual(backend, "unsandboxed")
        self.assertTrue(any("disabled" in note for note in notes))

    def test_unavailable_bwrap_degrades_with_reason(self):
        unavailable = sandbox.BackendInfo("bwrap", False, "namespaces denied")
        with mock.patch.object(sandbox, "detect_bwrap",
                               return_value=unavailable):
            argv, backend, notes = sandbox.build_command(
                ["echo", "hi"], backend="bwrap", writable_paths=[self.tmp])
        self.assertEqual(argv, ["echo", "hi"])
        self.assertEqual(backend, "unsandboxed")
        self.assertTrue(any("namespaces denied" in note for note in notes))

    def test_bwrap_command_shape(self):
        available = sandbox.BackendInfo(
            "bwrap", True, "ok", path="/usr/bin/bwrap",
            network_isolated=True)
        with mock.patch.object(sandbox, "detect_bwrap",
                               return_value=available):
            argv, backend, notes = sandbox.build_command(
                ["echo", "hi"], cwd=self.tmp,
                writable_paths=[self.tmp], allow_network=False,
                backend="bwrap")
        self.assertEqual(backend, "bwrap")
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        self.assertIn("--unshare-net", argv)
        self.assertIn("--die-with-parent", argv)
        self.assertIn("--ro-bind", argv)
        self.assertIn(str(self.tmp.resolve()), argv)
        self.assertEqual(argv[argv.index("--") + 1:], ["echo", "hi"])

    def test_bwrap_allow_network_omits_unshare_net(self):
        available = sandbox.BackendInfo(
            "bwrap", True, "ok", path="/usr/bin/bwrap",
            network_isolated=True)
        with mock.patch.object(sandbox, "detect_bwrap",
                               return_value=available):
            argv, backend, _ = sandbox.build_command(
                ["echo"], writable_paths=[self.tmp], allow_network=True,
                backend="bwrap")
        self.assertEqual(backend, "bwrap")
        self.assertNotIn("--unshare-net", argv)

    def test_bwrap_without_network_isolation_is_honest(self):
        no_net = sandbox.BackendInfo(
            "bwrap", True, "works but no netns",
            path="/usr/bin/bwrap", network_isolated=False)
        with mock.patch.object(sandbox, "detect_bwrap", return_value=no_net):
            argv, backend, notes = sandbox.build_command(
                ["echo"], writable_paths=[self.tmp], allow_network=False,
                backend="bwrap")
        self.assertEqual(backend, "bwrap")
        self.assertNotIn("--unshare-net", argv)
        self.assertTrue(any("NOT isolated" in note for note in notes))

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(sandbox.SandboxError):
            sandbox.build_command([])
        with self.assertRaises(sandbox.SandboxError):
            sandbox.build_command("echo hi")
        with self.assertRaises(sandbox.SandboxError):
            sandbox.build_command(["echo"], cwd=self.tmp / "does-not-exist")
        with self.assertRaises(ValueError):
            sandbox.build_command(["echo"], backend="magic")

    def test_auto_landlock_plan_carries_preexec(self):
        status = sandbox.detect_landlock()
        if not status.available:
            self.skipTest("Landlock is not available")
        plan = sandbox.wrap_command(
            ["echo", "hi"], cwd=self.tmp, writable_paths=[self.tmp],
            allow_network=True, backend="auto")
        self.assertEqual(plan.backend, "landlock")
        self.assertTrue(plan.sandboxed)
        self.assertIsNotNone(plan.preexec_fn)
        self.assertTrue(plan.notes)


@unittest.skipUnless(sys.platform.startswith("linux"), "Landlock is Linux-only")
class LandlockEnforcementTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.writable = Path(tempfile.mkdtemp(prefix="sfo-rw-", dir=self.tmp))
        self.readonly = Path(tempfile.mkdtemp(prefix="sfo-ro-", dir=self.tmp))

    def tearDown(self):
        self._tmp.cleanup()

    def _require_landlock(self) -> sandbox.LandlockStatus:
        status = sandbox.detect_landlock()
        if not status.available:
            self.skipTest(f"Landlock unavailable: {status.reason}")
        return status

    def _run_child(self, source: str, *, writable=True,
                   allow_network: bool = True) -> subprocess.CompletedProcess:
        preexec = sandbox.preexec_landlock(
            [str(self.writable)] if writable else [],
            allow_network=allow_network)
        return subprocess.run(
            [sys.executable, "-c", source], preexec_fn=preexec,
            capture_output=True, text=True, timeout=30, check=False)

    def test_writes_are_limited_to_writable_paths(self):
        self._require_landlock()
        source = f"""
import os
rw = {str(self.writable)!r}
ro = {str(self.readonly)!r}
with open(os.path.join(rw, "ok.txt"), "w") as handle:
    handle.write("ok")
print("rw-ok")
try:
    with open(os.path.join(ro, "no.txt"), "w") as handle:
        handle.write("no")
except PermissionError:
    print("ro-denied")
else:
    print("ro-ALLOWED")
# reads and exec stay allowed
with open("/etc/hostname", "r") as handle:
    handle.read()
print("read-ok")
"""
        completed = self._run_child(source)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("rw-ok", completed.stdout)
        self.assertIn("ro-denied", completed.stdout)
        self.assertIn("read-ok", completed.stdout)
        self.assertNotIn("ro-ALLOWED", completed.stdout)

    def test_without_writable_paths_all_writes_are_denied(self):
        self._require_landlock()
        target = self.writable / "x.txt"
        source = f"""
try:
    with open({str(target)!r}, "w") as handle:
        handle.write("x")
except PermissionError:
    print("denied")
else:
    print("ALLOWED")
"""
        completed = self._run_child(source, writable=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("denied", completed.stdout)
        self.assertNotIn("ALLOWED", completed.stdout)

    def test_tcp_connect_denied_when_network_disabled(self):
        status = self._require_landlock()
        if status.abi < 4:
            self.skipTest("Landlock ABI < 4 has no network rules")
        source = """
import socket
try:
    socket.create_connection(("127.0.0.1", 9), timeout=1)
except PermissionError:
    print("net-denied")
except OSError as exc:
    print("net-other", exc.errno)
else:
    print("net-connected")
"""
        completed = self._run_child(source, allow_network=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("net-denied", completed.stdout)

    def test_network_allowed_when_requested(self):
        status = self._require_landlock()
        if status.abi < 4:
            self.skipTest("Landlock ABI < 4 has no network rules")
        source = """
import socket
try:
    socket.create_connection(("127.0.0.1", 9), timeout=1)
except PermissionError:
    print("net-denied")
except OSError:
    print("net-open")
else:
    print("net-open")
"""
        completed = self._run_child(source, allow_network=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("net-open", completed.stdout)

    def test_restrict_current_process_reports_abi(self):
        status = self._require_landlock()
        source = f"""
from stackflowos.security.sandbox import restrict_current_process
report = restrict_current_process([{str(self.writable)!r}],
                                  allow_network=False)
print("backend", report.backend)
print("abi", report.abi)
print("net", report.network_restricted)
"""
        completed = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True,
            timeout=30, check=False,
            env={**os.environ, "PYTHONPATH": str(REPO)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("backend landlock", completed.stdout)
        self.assertIn(f"abi {status.abi}", completed.stdout)
        self.assertIn("net True", completed.stdout)


if __name__ == "__main__":
    unittest.main()
