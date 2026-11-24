"""DIST: ZMQ bridge parameter validation and optional-dependency fallback."""

from __future__ import annotations

import asyncio
import io
import unittest
from contextlib import redirect_stderr

from stackflowos.transport import zmq_bridge as z


class AvailabilityTests(unittest.TestCase):
    def test_available_matches_import_guard(self):
        self.assertEqual(z.available(), z._zmq is not None)

    @unittest.skipUnless(z.available(), "pyzmq not installed")
    def test_require_zmq_returns_module_when_installed(self):  # pragma: no cover
        self.assertIs(z.require_zmq(), z._zmq)

    @unittest.skipIf(z.available(), "pyzmq is installed")
    def test_require_zmq_raises_clear_error(self):
        with self.assertRaises(z.ZmqUnavailable) as caught:
            z.require_zmq()
        self.assertIn("pyzmq", str(caught.exception))
        self.assertIn("pip install pyzmq", str(caught.exception))


class ParsingTests(unittest.TestCase):
    def test_rpc_config(self):
        config = z.parse_args(["--listen", "tcp://0.0.0.0:5555",
                               "--connect", "tcp://127.0.0.1:17820",
                               "--timeout", "3", "--contexts", "2"])
        self.assertEqual(config.mode, "rpc")
        self.assertEqual(config.listen, "tcp://0.0.0.0:5555")
        self.assertEqual(config.connect, "tcp://127.0.0.1:17820")
        self.assertEqual(config.timeout_s, 3.0)
        self.assertEqual(config.contexts, 2)

    def test_ingest_and_export_configs(self):
        ingest = z.parse_args(["--sub-connect", "tcp://pub:5556",
                               "--events-connect", "ipc:///tmp/events.sock",
                               "--topic", "sfo"])
        self.assertEqual(ingest.mode, "ingest")
        self.assertEqual(ingest.topic, "sfo")
        export = z.parse_args(["--events-listen", "tcp://127.0.0.1:17901",
                               "--pub-connect", "tcp://sink:5557"])
        self.assertEqual(export.mode, "export")
        self.assertEqual(export.pub_connect, "tcp://sink:5557")

    def test_invalid_combinations(self):
        cases = [
            [],
            ["--listen", "tcp://0.0.0.0:5555"],          # --connect missing
            ["--connect", "tcp://127.0.0.1:17820"],
            ["--sub-connect", "tcp://pub:5556"],         # events-connect missing
            ["--events-listen", "tcp://127.0.0.1:17901"],  # no pub target
            ["--listen", "http://nope", "--connect", "tcp://127.0.0.1:1"],
            ["--listen", "tcp://0.0.0.0:5555",
             "--connect", "http://127.0.0.1:1"],
            ["--listen", "tcp://0.0.0.0:5555",
             "--connect", "tcp://127.0.0.1:1", "--timeout", "0"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                with self.assertRaises(ValueError):
                    z.parse_args(argv)

    def test_endpoint_validation_helpers(self):
        self.assertEqual(z.validate_zmq_endpoint("tcp://x:1", "ep"), "tcp://x:1")
        with self.assertRaises(ValueError):
            z.validate_zmq_endpoint("", "ep")
        self.assertEqual(z._validate_pynng_address("ipc:///tmp/x.sock", "ep"),
                         "ipc:///tmp/x.sock")
        with self.assertRaises(ValueError):
            z._validate_pynng_address("http://x", "ep")


class BuilderTests(unittest.TestCase):
    def test_builds_each_mode(self):
        rpc = z.build_bridge(z.parse_args([
            "--listen", "tcp://127.0.0.1:5555",
            "--connect", "tcp://127.0.0.1:17820"]))
        self.assertIsInstance(rpc, z.ZmqRpcBridge)
        ingest = z.build_bridge(z.parse_args([
            "--sub-connect", "tcp://p:5556",
            "--events-connect", "tcp://127.0.0.1:17821"]))
        self.assertIsInstance(ingest, z.ZmqEventIngestBridge)
        export = z.build_bridge(z.parse_args([
            "--events-listen", "tcp://127.0.0.1:17901",
            "--pub-listen", "tcp://127.0.0.1:5557"]))
        self.assertIsInstance(export, z.NngEventExportBridge)

    @unittest.skipIf(z.available(), "pyzmq is installed")
    def test_start_degrades_without_pyzmq(self):
        bridge = z.build_bridge(z.parse_args([
            "--listen", "tcp://127.0.0.1:5555",
            "--connect", "tcp://127.0.0.1:17820"]))
        with self.assertRaises(z.ZmqUnavailable):
            asyncio.run(bridge.start())
        self.assertFalse(bridge.running)

    def test_invalid_parameters_raise_before_import(self):
        with self.assertRaises(ValueError):
            z.ZmqRpcBridge(z.BridgeConfig(mode="rpc", listen="",
                                          connect="tcp://x:1"))


class CliTests(unittest.TestCase):
    @unittest.skipIf(z.available(), "pyzmq is installed")
    def test_cli_reports_missing_dependency(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = z.main(["--listen", "tcp://0.0.0.0:5555",
                           "--connect", "tcp://127.0.0.1:17820"])
        self.assertEqual(code, 2)
        self.assertIn("pyzmq", stderr.getvalue())

    def test_cli_usage_error(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = z.main([])
        self.assertEqual(code, 2)
        self.assertIn("zmq_bridge:", stderr.getvalue())

    def test_cli_help_exits_zero(self):
        self.assertEqual(z.main(["--help"]), 0)


if __name__ == "__main__":
    unittest.main()
