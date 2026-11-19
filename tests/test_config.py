import os
import tempfile
import unittest
from pathlib import Path

from stackflowos.config import Config, ConfigError, default_root, resolve_paths

REPO = default_root()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.root = Path(self._tmp.name)
        self._saved_env = {k: os.environ.get(k) for k in
                           ("SFO_ROOT", "SFO_STATE_DIR", "SFO_WEB_PORT",
                            "SFO_TRANSPORT_MODE", "SFO_LLM_PROVIDER")}
        for key in list(self._saved_env):
            os.environ.pop(key, None)

    def tearDown(self):
        self._tmp.cleanup()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_defaults(self):
        cfg = Config.load(self.root)
        self.assertEqual(cfg.transport.mode, "ipc")
        self.assertEqual(cfg.web.port, 17800)
        self.assertEqual(cfg.llm.provider, "mock")
        self.assertTrue(cfg.paths.state_dir.exists())
        self.assertEqual(cfg.paths.state_dir, self.root / ".stackflowos")

    def test_update_persists_and_reloads(self):
        cfg = Config.load(self.root)
        new = cfg.update({"transport": {"mode": "tcp"}, "web": {"port": 17999}})
        self.assertEqual(new.transport.mode, "tcp")
        self.assertEqual(new.web.port, 17999)
        reloaded = Config.load(self.root)
        self.assertEqual(reloaded.transport.mode, "tcp")
        self.assertEqual(reloaded.web.port, 17999)

    def test_invalid_patch_leaves_file_untouched(self):
        cfg = Config.load(self.root)
        with self.assertRaises(ConfigError):
            cfg.update({"transport": {"mode": "carrier-pigeon"}})
        self.assertFalse(cfg.paths.config_file.exists())
        with self.assertRaises(ConfigError):
            cfg.update({"nonsense": {"x": 1}})
        self.assertFalse(cfg.paths.config_file.exists())

    def test_public_masks_secret(self):
        cfg = Config.load(self.root)
        cfg = cfg.update({"llm": {"api_key": "sk-secret"}})
        public = cfg.public_dict()
        self.assertEqual(public["llm"]["api_key"], "***")
        self.assertEqual(cfg.llm.api_key, "sk-secret")

    def test_env_override(self):
        os.environ["SFO_WEB_PORT"] = "17777"
        os.environ["SFO_TRANSPORT_MODE"] = "tcp"
        cfg = Config.load(self.root)
        self.assertEqual(cfg.web.port, 17777)
        self.assertEqual(cfg.transport.mode, "tcp")

    def test_state_dir_env(self):
        state = self.root / "custom-state"
        os.environ["SFO_STATE_DIR"] = str(state)
        cfg = Config.load(self.root)
        self.assertEqual(cfg.paths.state_dir, state.resolve())


if __name__ == "__main__":
    unittest.main()
