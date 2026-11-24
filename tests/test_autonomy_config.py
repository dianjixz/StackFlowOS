"""Autonomy configuration: defaults, validation and live patch round-trips."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stackflowos.config import Config, ConfigError

TMP_ROOT = Path(__file__).resolve().parents[1] / ".stackflowos" / "tmp"


class AutonomyConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT))
        self.root = Path(self._tmp.name).resolve()
        self.config = Config.load(root=self.root,
                                  state_dir=self.root / "state")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _apply(self, patch: dict) -> Config:
        return self.config.with_overrides({"autonomy": patch})

    def test_defaults_are_fail_closed(self) -> None:
        cfg = self.config.autonomy
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.dispatch_mode, "off")
        self.assertEqual(cfg.approve_mode, "off")
        self.assertEqual(cfg.approve_risk_max, "medium")
        self.assertEqual(cfg.approve_capabilities, ["fs.write", "agent.spawn"])
        self.assertFalse(cfg.approve_shell)
        self.assertEqual(cfg.approve_shell_patterns, [])
        self.assertGreater(cfg.interval_s, 0)

    def test_public_config_includes_autonomy(self) -> None:
        public = self.config.public_dict()
        self.assertIn("autonomy", public)
        self.assertFalse(public["autonomy"]["enabled"])

    def test_valid_policy_round_trips(self) -> None:
        cfg = self._apply({
            "enabled": True,
            "dispatch_mode": "agents",
            "approve_mode": "enforce",
            "approve_capabilities": ["fs.write"],
            "approve_projects": [],
            "approve_max_per_hour": 5,
            "dispatch_max_agents": 3,
            "interval_s": 45.0,
        })
        self.assertTrue(cfg.autonomy.enabled)
        self.assertEqual(cfg.autonomy.dispatch_mode, "agents")
        self.assertEqual(cfg.autonomy.approve_mode, "enforce")
        self.assertEqual(cfg.autonomy.approve_max_per_hour, 5)
        self.assertEqual(cfg.autonomy.dispatch_max_agents, 3)
        self.assertEqual(cfg.autonomy.interval_s, 45.0)

    def test_unknown_field_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._apply({"enabled": True, "nope": 1})

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._apply({"dispatch_mode": "automatic"})
        with self.assertRaises(ConfigError):
            self._apply({"approve_mode": "allow-all"})

    def test_risk_ceiling_must_be_known(self) -> None:
        with self.assertRaises(ConfigError):
            self._apply({"approve_risk_max": "critical"})

    def test_hard_deny_capabilities_are_not_configurable(self) -> None:
        for capability in ("fs.delete", "unit.control", "sys.config",
                           "project.delete", "agent.kill", "approval.decide"):
            with self.subTest(capability=capability):
                with self.assertRaises(ConfigError):
                    self._apply({"approve_capabilities": ["fs.write",
                                                          capability]})

    def test_unknown_capability_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self._apply({"approve_capabilities": ["fs.write", "llm.hack"]})

    def test_shell_requires_explicit_opt_in(self) -> None:
        with self.assertRaises(ConfigError):
            self._apply({"approve_capabilities": ["shell.exec"]})
        with self.assertRaises(ConfigError):
            self._apply({"approve_shell": True,
                         "approve_capabilities": ["shell.exec"]})
        with self.assertRaises(ConfigError):
            self._apply({"approve_shell": True,
                         "approve_capabilities": ["shell.exec"],
                         "approve_shell_patterns": ["^python3 -m py_compile"]})
        cfg = self._apply({"approve_shell": True,
                           "approve_risk_max": "high",
                           "approve_capabilities": ["shell.exec"],
                           "approve_shell_patterns": ["^python3 -m py_compile"]})
        self.assertTrue(cfg.autonomy.approve_shell)
        self.assertEqual(cfg.autonomy.approve_shell_patterns,
                         ["^python3 -m py_compile"])

    def test_shell_patterns_must_compile(self) -> None:
        with self.assertRaises(ConfigError):
            self._apply({"approve_shell": True, "approve_risk_max": "high",
                         "approve_capabilities": ["shell.exec"],
                         "approve_shell_patterns": ["(["]})

    def test_net_fetch_requires_domains(self) -> None:
        with self.assertRaises(ConfigError):
            self._apply({"approve_capabilities": ["net.fetch"]})
        cfg = self._apply({"approve_capabilities": ["net.fetch"],
                           "approve_domains": ["example.com"]})
        self.assertEqual(cfg.autonomy.approve_domains, ["example.com"])

    def test_quotas_are_bounded(self) -> None:
        for patch in ({"dispatch_max_agents": -1},
                      {"dispatch_max_tasks_per_hour": -1},
                      {"approve_max_per_hour": -1},
                      {"interval_s": 1.0},
                      {"report_keep": 0},
                      {"failure_threshold": 0}):
            with self.subTest(patch=patch):
                with self.assertRaises(ConfigError):
                    self._apply(patch)

    def test_project_allowlist_is_validated(self) -> None:
        with self.assertRaises(ConfigError):
            self._apply({"approve_projects": ["../etc"]})
        with self.assertRaises(ConfigError):
            self._apply({"approve_projects": [123]})

    def test_roles_and_names_are_strings(self) -> None:
        with self.assertRaises(ConfigError):
            self._apply({"approve_roles": [1]})
        with self.assertRaises(ConfigError):
            self._apply({"approve_domains": [""]})

    def test_persisted_patch_is_reloaded(self) -> None:
        cfg = self.config.update({"autonomy": {"enabled": True,
                                               "approve_mode": "observe"}})
        self.assertTrue(cfg.autonomy.enabled)
        reloaded = Config.load(root=self.root, state_dir=self.root / "state")
        self.assertTrue(reloaded.autonomy.enabled)
        self.assertEqual(reloaded.autonomy.approve_mode, "observe")

    def test_disabled_can_be_restored_after_enable(self) -> None:
        cfg = self._apply({"enabled": True})
        cfg = cfg.with_overrides({"autonomy": {"enabled": False}})
        self.assertFalse(cfg.autonomy.enabled)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
