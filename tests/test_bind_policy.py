"""L8 bind policy: wildcard/remote listeners require an explicit opt-in.

``web.host`` / ``transport.host`` default to loopback.  A wildcard or
non-loopback address is a configuration error unless the matching
``allow_remote`` flag is True; the two sections never widen each other.
"""

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path

from stackflowos.cli import _build_config
from stackflowos.config import Config, ConfigError

WILDCARD_HOSTS = ("", "0.0.0.0", "::", "*", "0:0:0:0:0:0:0:0")
REMOTE_HOSTS = ("192.168.1.10", "example.com")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class BindPolicyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
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

    def _cfg(self) -> Config:
        return Config.load(self.root)

    def _assert_policy(self, section: str, hosts) -> None:
        cfg = self._cfg()
        for host in hosts:
            with self.subTest(section=section, host=host, allow_remote=False):
                with self.assertRaises(ConfigError) as ctx:
                    cfg.with_overrides({section: {"host": host}})
                message = str(ctx.exception)
                self.assertIn(f"{section}.allow_remote", message)
                other = "transport" if section == "web" else "web"
                self.assertNotIn(f"{other}.allow_remote", message)
            with self.subTest(section=section, host=host, allow_remote=True):
                allowed = cfg.with_overrides(
                    {section: {"host": host, "allow_remote": True}})
                self.assertEqual(getattr(allowed, section).host,
                                 host.strip().strip("[]"))
                self.assertTrue(getattr(allowed, section).allow_remote)

    # -- defaults / loopback ------------------------------------------------
    def test_defaults_are_loopback(self):
        cfg = self._cfg()
        self.assertEqual(cfg.web.host, "127.0.0.1")
        self.assertEqual(cfg.transport.host, "127.0.0.1")
        self.assertFalse(cfg.web.allow_remote)
        self.assertFalse(cfg.transport.allow_remote)
        self.assertEqual(cfg.web.tls_cert, "")
        self.assertEqual(cfg.web.tls_key, "")

    def test_loopback_hosts_allowed_without_allow_remote(self):
        cfg = self._cfg()
        for section in ("web", "transport"):
            for host in LOOPBACK_HOSTS:
                with self.subTest(section=section, host=host):
                    changed = cfg.with_overrides({section: {"host": host}})
                    self.assertEqual(getattr(changed, section).host, host)
                    self.assertFalse(getattr(changed, section).allow_remote)

    def test_host_is_normalized_before_policy_check(self):
        cfg = self._cfg()
        spaced = cfg.with_overrides({"web": {"host": " 127.0.0.1 "}})
        self.assertEqual(spaced.web.host, "127.0.0.1")
        bracketed = cfg.with_overrides({"web": {"host": "[::1]"}})
        self.assertEqual(bracketed.web.host, "::1")
        # Bracket/space spelling of the wildcard is still a wildcard.
        with self.assertRaises(ConfigError):
            cfg.with_overrides({"web": {"host": " [::] "}})
        allowed = cfg.with_overrides(
            {"web": {"host": " [::] ", "allow_remote": True}})
        self.assertEqual(allowed.web.host, "::")

    # -- wildcard / remote require opt-in -----------------------------------
    def test_wildcard_hosts_require_allow_remote(self):
        for section in ("web", "transport"):
            self._assert_policy(section, WILDCARD_HOSTS)

    def test_non_loopback_hosts_require_allow_remote(self):
        for section in ("web", "transport"):
            self._assert_policy(section, REMOTE_HOSTS)

    def test_allow_remote_must_be_boolean(self):
        cfg = self._cfg()
        for section in ("web", "transport"):
            for bad in ("yes", 1, 0, None, [], {}):
                with self.subTest(section=section, bad=bad):
                    with self.assertRaises(ConfigError):
                        cfg.with_overrides({section: {"allow_remote": bad}})

    # -- the two sections are independent -----------------------------------
    def test_web_and_transport_allow_remote_are_independent(self):
        cfg = self._cfg()
        # web opt-in does not permit a wildcard transport listener.
        with self.assertRaises(ConfigError):
            cfg.with_overrides({"web": {"allow_remote": True},
                                "transport": {"host": "0.0.0.0"}})
        with self.assertRaises(ConfigError):
            cfg.with_overrides({"web": {"allow_remote": True},
                                "transport": {"host": "example.com"}})
        # transport opt-in does not permit a wildcard web listener.
        with self.assertRaises(ConfigError):
            cfg.with_overrides({"transport": {"allow_remote": True},
                                "web": {"host": "0.0.0.0"}})
        with self.assertRaises(ConfigError):
            cfg.with_overrides({"transport": {"allow_remote": True},
                                "web": {"host": "example.com"}})

    # -- CLI overrides ------------------------------------------------------
    def _args(self, **overrides):
        values = {"root": str(self.root), "state_dir": None, "transport": None,
                  "port": None, "host": None, "allow_remote": None}
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_cli_host_override_is_validated(self):
        with self.assertRaises(ConfigError) as ctx:
            _build_config(self._args(host="0.0.0.0"))
        self.assertIn("--allow-remote", str(ctx.exception))

    def test_cli_allow_remote_flag_widens_web_only(self):
        cfg = _build_config(self._args(host="0.0.0.0", allow_remote=True))
        self.assertEqual(cfg.web.host, "0.0.0.0")
        self.assertTrue(cfg.web.allow_remote)
        self.assertFalse(cfg.transport.allow_remote)
        # CLI flags stay per-process.
        self.assertFalse(cfg.paths.config_file.exists())

    # -- persisted config ---------------------------------------------------
    def test_persisted_wildcard_without_optin_fails_to_load(self):
        state = self.root / ".stackflowos"
        state.mkdir(parents=True, exist_ok=True)
        config_file = state / "config.json"
        config_file.write_text(json.dumps({"web": {"host": "0.0.0.0"}}),
                               encoding="utf-8")
        with self.assertRaises(ConfigError):
            Config.load(self.root)
        config_file.write_text(
            json.dumps({"web": {"host": "0.0.0.0", "allow_remote": True}}),
            encoding="utf-8")
        cfg = Config.load(self.root)
        self.assertTrue(cfg.web.allow_remote)
        self.assertEqual(cfg.web.host, "0.0.0.0")

    # -- reserved TLS pair --------------------------------------------------
    def test_tls_pair_is_validated_but_not_required(self):
        cfg = self._cfg()
        # Remote opt-in works without TLS in L8 (TLS is Phase 5, behind proxy).
        changed = cfg.with_overrides(
            {"web": {"host": "example.com", "allow_remote": True}})
        self.assertEqual(changed.web.tls_cert, "")
        with self.assertRaises(ConfigError) as ctx:
            cfg.with_overrides({"web": {"tls_cert": "/etc/sfo/cert.pem"}})
        self.assertIn("tls_key", str(ctx.exception))
        with self.assertRaises(ConfigError):
            cfg.with_overrides({"web": {"tls_key": "/etc/sfo/key.pem"}})
        paired = cfg.with_overrides(
            {"web": {"tls_cert": "/etc/sfo/cert.pem",
                     "tls_key": "/etc/sfo/key.pem"}})
        self.assertEqual(paired.web.tls_cert, "/etc/sfo/cert.pem")
        for bad in (1, True, []):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    cfg.with_overrides({"web": {"tls_cert": bad}})


if __name__ == "__main__":
    unittest.main()
