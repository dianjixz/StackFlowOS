"""Sandbox capability detection, command wrapping and real Landlock checks."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stackflowos.config import default_root
from stackflowos.l2 import EventLog
from stackflowos.security import sandbox
from stackflowos.transport.address import AddressAllocator

REPO = default_root()


def _landlock_skip_reason(prefix: str = "Landlock is not available") -> str:
    """Skip reason that carries the full capability matrix (W3 item 2).

    A bare "unavailable" hides *why* the host cannot isolate and which other
    backend was probed; the matrix makes the skip self-explanatory in CI logs.
    """
    try:
        matrix = sandbox.capability_matrix()
        detail = sandbox.matrix_line(matrix)
    except Exception as exc:  # pragma: no cover - probing is best effort
        detail = f"capability probe failed: {type(exc).__name__}: {exc}"
    return f"{prefix} — capability matrix: {detail}"


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
            self.skipTest(_landlock_skip_reason())
        plan = sandbox.wrap_command(
            ["echo", "hi"], cwd=self.tmp, writable_paths=[self.tmp],
            allow_network=True, backend="auto")
        self.assertEqual(plan.backend, "landlock")
        self.assertTrue(plan.sandboxed)
        self.assertIsNotNone(plan.preexec_fn)
        self.assertTrue(plan.notes)


class LandlockEnforcementTests(unittest.TestCase):
    # No class-level skipUnless: every test calls ``_require_landlock()`` so the
    # skip reason can include the capability matrix (Linux-only is part of it).
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
            self.skipTest(_landlock_skip_reason(
                f"Landlock unavailable: {status.reason}"))
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
            self.skipTest(_landlock_skip_reason(
                "Landlock ABI < 4 has no network rules"))
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
            self.skipTest(_landlock_skip_reason(
                "Landlock ABI < 4 has no network rules"))
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

class CapabilityMatrixTests(unittest.TestCase):
    """Item 2: the host capability matrix is explicit, not a silent default."""

    def test_matrix_has_both_backends_and_selection(self):
        matrix = sandbox.capability_matrix()
        self.assertEqual(matrix["platform"], sys.platform)
        self.assertIn("landlock", matrix)
        self.assertIn("bwrap", matrix)
        self.assertIn(matrix["auto_backend"],
                      {"landlock", "bwrap", "unsandboxed"})
        self.assertIsInstance(matrix["landlock"]["available"], bool)
        self.assertIsInstance(matrix["bwrap"]["available"], bool)
        self.assertTrue(matrix["bwrap"]["probed"])
        line = sandbox.matrix_line(matrix)
        self.assertIn("landlock=", line)
        self.assertIn("bwrap=", line)
        self.assertIn("auto=", line)

    def test_matrix_can_skip_the_expensive_bwrap_probe(self):
        with mock.patch.object(sandbox, "detect_bwrap") as probe:
            matrix = sandbox.capability_matrix(probe_bwrap=False)
        probe.assert_not_called()
        self.assertFalse(matrix["bwrap"]["probed"])
        self.assertIsNone(matrix["auto_backend"])
        self.assertEqual(sandbox.selected_backend(matrix, "off"),
                         "unsandboxed")

    def test_selected_backend_reads_the_matrix_only(self):
        matrix = sandbox.capability_matrix(probe_bwrap=False)
        with mock.patch.object(sandbox, "detect_bwrap") as probe:
            self.assertIn(sandbox.selected_backend(matrix, "auto"),
                          {"landlock", "unsandboxed"})
        probe.assert_not_called()
        with self.assertRaises(ValueError):
            sandbox.selected_backend(matrix, "magic")

    def test_skip_reason_includes_the_matrix(self):
        reason = _landlock_skip_reason("Landlock unavailable: example")
        self.assertIn("Landlock unavailable: example", reason)
        self.assertIn("landlock=", reason)
        self.assertIn("bwrap=", reason)


class StartupAuditTests(unittest.TestCase):
    """Item 2: startup probe logs a WARNING and an audit event on degradation."""

    def _app(self, mode: str, allow_network: bool = False):
        from types import SimpleNamespace

        tmp = tempfile.mkdtemp(dir=str(REPO / ".stackflowos" / "tmp"))
        app = SimpleNamespace(
            config=SimpleNamespace(security=SimpleNamespace(
                sandbox=mode, sandbox_allow_network=allow_network)),
            eventlog=EventLog(Path(tmp) / "events"),
        )
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        return app

    def _close(self, app) -> None:
        asyncio.run(app.eventlog.close())

    def test_unsandboxed_degradation_warns_and_audits(self):
        app = self._app("landlock")
        unavailable = sandbox.LandlockStatus(
            False, reason="landlock unavailable: Operation not permitted")
        with mock.patch.object(sandbox, "detect_landlock",
                               return_value=unavailable), \
                mock.patch.object(
                    sandbox, "detect_bwrap",
                    return_value=sandbox.BackendInfo(
                        "bwrap", False,
                        "bwrap cannot create an unprivileged sandbox")):
            try:
                with self.assertLogs("sfo.security.sandbox",
                                     level="WARNING") as captured:
                    matrix = sandbox.audit_startup(app)
            finally:
                self._close(app)
        self.assertEqual(matrix["effective_backend"], "unsandboxed")
        self.assertTrue(matrix["unsandboxed"])
        self.assertTrue(any("UNSANDBOXED" in line for line in captured.output))
        records = app.eventlog.query(types=["sandbox"])
        types = [record.type for record in records]
        self.assertIn("sandbox.unsandboxed", types)
        payload = next(record for record in records
                       if record.type == "sandbox.unsandboxed")
        self.assertEqual(payload.data["effective_backend"], "unsandboxed")
        self.assertIn("landlock", payload.data["matrix"])

    def test_available_backend_records_capability_event(self):
        app = self._app("auto")
        with mock.patch.object(
                sandbox, "detect_landlock",
                return_value=sandbox.LandlockStatus(True, abi=8)), \
                mock.patch.object(
                    sandbox, "detect_bwrap",
                    return_value=sandbox.BackendInfo(
                        "bwrap", False, "namespaces denied")):
            try:
                with self.assertLogs("sfo.security.sandbox",
                                     level="INFO") as captured:
                    matrix = sandbox.audit_startup(app)
            finally:
                self._close(app)
        self.assertEqual(matrix["effective_backend"],
                         sandbox.BACKEND_LANDLOCK)
        self.assertNotIn("UNSANDBOXED", "\n".join(captured.output))
        types = [record.type for record in app.eventlog.query(types=["sandbox"])]
        self.assertIn("sandbox.capabilities", types)
        self.assertNotIn("sandbox.unsandboxed", types)

    def test_disabled_sandbox_is_logged_but_not_an_alert(self):
        app = self._app("off")
        with mock.patch.object(
                sandbox, "detect_landlock",
                return_value=sandbox.LandlockStatus(True, abi=8)), \
                mock.patch.object(sandbox, "detect_bwrap") as probe:
            try:
                with self.assertLogs("sfo.security.sandbox",
                                     level="INFO") as captured:
                    matrix = sandbox.audit_startup(app)
            finally:
                self._close(app)
        probe.assert_not_called()  # no subprocess at boot when disabled
        self.assertEqual(matrix["mode"], "off")
        self.assertFalse(matrix["bwrap"]["probed"])
        self.assertTrue(any("sandbox disabled" in line
                            for line in captured.output))
        self.assertEqual(app.eventlog.query(types=["sandbox"]), [])


class WorkerSocketCleanupTests(unittest.TestCase):
    """Item 1: the worker's Landlock whitelist cannot unlink run/ sockets.

    This is a verified residual risk, not papered over: the worker-side unlink
    is denied (and the transport logs a warning), while the hub allocator
    reclaims stale socket files on release/reallocation.  Adding ``run/`` to
    the worker whitelist would let a worker unlink/rebind hub sockets, so the
    fix is deliberately *not* applied here.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_worker_whitelist_denies_unlinking_run_dir_socket(self):
        status = sandbox.detect_landlock()
        if not status.available:
            self.skipTest(_landlock_skip_reason())
        writable = self.tmp / "workspace"
        writable.mkdir()
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        socket_file = run_dir / "coder.1.rpc.sock"
        socket_file.write_text("stale", "utf-8")
        # Run the real transport cleanup helper inside a Landlocked child with
        # exactly the worker whitelist: the unlink is denied and the helper
        # logs a WARNING (it is not silently swallowed).
        source = f"""
from stackflowos.transport.nng import cleanup_ipc_address
cleanup_ipc_address("ipc://{socket_file}")
import os
print("exists", os.path.exists({str(socket_file)!r}))
"""
        completed = subprocess.run(
            [sys.executable, "-c", source],
            preexec_fn=sandbox.preexec_landlock(
                [str(writable)], allow_network=True),
            capture_output=True, text=True, timeout=30, check=False,
            env={**os.environ, "PYTHONPATH": str(REPO)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("exists True", completed.stdout)
        self.assertIn("cannot remove ipc socket", completed.stderr)
        self.assertIn("Permission denied", completed.stderr)
        self.assertTrue(socket_file.exists())

    def test_cleanup_failure_is_logged_not_silent(self):
        from stackflowos.transport.nng import cleanup_ipc_address

        with mock.patch.object(Path, "unlink",
                               side_effect=PermissionError(13, "denied")):
            with self.assertLogs("sfo.transport", level="WARNING") as captured:
                cleanup_ipc_address(f"ipc://{self.tmp}/run/coder.1.rpc.sock")
        self.assertTrue(any("cannot remove ipc socket" in line
                            for line in captured.output))

    def test_hub_allocator_reclaims_stale_socket_files(self):
        run_dir = self.tmp / "run"
        allocator = AddressAllocator(mode="ipc", host="127.0.0.1",
                                     run_dir=run_dir)
        addresses = allocator.allocate("coder.1")
        paths = [Path(url[len("ipc://"):])
                 for url in (addresses.rpc, addresses.events)]
        for path in paths:
            path.write_text("stale", "utf-8")
        allocator.release("coder.1")
        for path in paths:
            self.assertFalse(path.exists())
        # A crashed predecessor leaves files behind; reallocation unlinks them
        # before the new listener binds, so a stale socket cannot wedge a unit.
        for path in paths:
            path.write_text("stale", "utf-8")
        allocator.allocate("coder.1")
        for path in paths:
            self.assertFalse(path.exists())



if __name__ == "__main__":
    unittest.main()
