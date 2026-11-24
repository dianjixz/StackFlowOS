"""Tests for opt-in log rotation (util.logging + observability.logging)."""

from __future__ import annotations

import logging
import logging.handlers
import tempfile
import unittest
from pathlib import Path

from stackflowos.observability import logging as obs_logging
from stackflowos.util import logging as sfo_logging


class LogRotationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.log_file = self.tmp / "hub.log"
        self.root = logging.getLogger(sfo_logging.ROOT_LOGGER)
        self._cleanup()

    def tearDown(self):
        self._cleanup()
        self._tmp.cleanup()

    def _cleanup(self):
        for handler in list(self.root.handlers):
            self.root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        sfo_logging._configured = False

    def test_setup_logging_with_rotation_creates_backup(self):
        sfo_logging.setup_logging("INFO", self.log_file, rotation=(200, 2),
                                  force=True)
        logger = logging.getLogger("sfo.test.rotation")
        for index in range(40):
            logger.info("rotation test line %d %s", index, "x" * 20)
        for handler in self.root.handlers:
            handler.flush()
        files = sorted(path.name for path in self.tmp.iterdir())
        self.assertIn("hub.log.1", files)
        self.assertTrue((self.log_file).exists())
        self.assertTrue(any(isinstance(handler, logging.StreamHandler)
                            and not isinstance(handler, logging.FileHandler)
                            for handler in self.root.handlers))
        rotating = [handler for handler in self.root.handlers
                    if isinstance(handler, logging.handlers.RotatingFileHandler)]
        self.assertEqual(len(rotating), 1)
        self.assertEqual(rotating[0].backupCount, 2)

    def test_configure_file_logging_replaces_file_handler_only(self):
        console = logging.StreamHandler()
        self.root.addHandler(console)
        old = sfo_logging.make_file_handler(self.log_file)
        self.root.addHandler(old)
        self.assertEqual(len(self.root.handlers), 2)
        status = obs_logging.configure_file_logging(
            self.log_file, max_bytes=200, backup_count=1)
        self.assertTrue(status["configured"])
        self.assertTrue(status["rotating"])
        self.assertEqual(status["replaced"], 1)
        self.assertIn(console, self.root.handlers)
        file_handlers = [handler for handler in self.root.handlers
                         if isinstance(handler, logging.FileHandler)]
        self.assertEqual(len(file_handlers), 1)
        self.assertIsInstance(file_handlers[0],
                              logging.handlers.RotatingFileHandler)
        # Idempotent: applying again must not stack handlers.
        obs_logging.configure_file_logging(self.log_file, max_bytes=200,
                                           backup_count=1)
        file_handlers = [handler for handler in self.root.handlers
                         if isinstance(handler, logging.FileHandler)]
        self.assertEqual(len(file_handlers), 1)

    def test_zero_rotation_keeps_plain_file_handler(self):
        status = obs_logging.configure_file_logging(
            self.log_file, max_bytes=0, backup_count=0)
        self.assertTrue(status["configured"])
        self.assertFalse(status["rotating"])
        file_handlers = [handler for handler in self.root.handlers
                         if isinstance(handler, logging.FileHandler)]
        self.assertEqual(len(file_handlers), 1)
        self.assertNotIsInstance(file_handlers[0],
                                logging.handlers.RotatingFileHandler)

    def test_configure_from_app_reads_config(self):
        app = type("App", (), {})()
        app.paths = type("Paths", (), {"log_file": self.log_file})()
        app.config = type("Config", (), {})()
        app.config.observability = type("Obs", (), {
            "log_rotate_bytes": 512,
            "log_rotate_keep": 4,
        })()
        status = obs_logging.configure_from_app(app)
        self.assertTrue(status["configured"])
        self.assertEqual(status["max_bytes"], 512)
        self.assertEqual(status["backup_count"], 4)
        self.assertTrue(status["rotating"])

    def test_normalise_rotation_shapes(self):
        self.assertEqual(sfo_logging.normalise_rotation(None), None)
        self.assertEqual(sfo_logging.normalise_rotation(100), (100, 3))
        self.assertEqual(sfo_logging.normalise_rotation((100, 2)), (100, 2))
        self.assertEqual(sfo_logging.normalise_rotation({"bytes": 9, "keep": 1}),
                         (9, 1))
        with self.assertRaises(TypeError):
            sfo_logging.normalise_rotation(object())


if __name__ == "__main__":
    unittest.main()
