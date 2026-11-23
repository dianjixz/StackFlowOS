"""L3: Rule/Policy data robustness.

Corrupt persisted rules must not crash ``PolicyEngine.load`` (they are
skipped, counted and announced), bad ``add_rule``/``update_rule`` input must
be a 400 instead of a 500, and ``Rule.active`` must fail closed on broken
lifetime data.  Good rules keep working across a save/load round trip.
"""

import json
import tempfile
import unittest
from pathlib import Path

from stackflowos.errors import ValidationError
from stackflowos.kernel import Context
from stackflowos.l2.eventlog import EventLog
from stackflowos.security import PolicyEngine, Rule


class RuleRobustnessTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ctx = Context(name="test")
        self.eventlog = EventLog(self.root / "events")
        self.rules_path = self.root / "security" / "rules.json"
        self.policy = self._engine()

    async def asyncTearDown(self):
        await self.ctx.dispose()
        await self.eventlog.close()
        self._tmp.cleanup()

    def _engine(self):
        return PolicyEngine(self.ctx, self.eventlog,
                            rules_path=self.rules_path,
                            projects_root=self.root / "projects")

    def _write_rules(self, rows):
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        self.rules_path.write_text(
            json.dumps({"version": 1, "rules": rows}), encoding="utf-8")

    def _file_bytes(self):
        return self.rules_path.read_bytes()

    def _memory(self):
        return {rule.id: rule.to_dict() for rule in self.policy.list_rules()}


class LoadRobustnessTests(RuleRobustnessTestBase):
    def test_corrupt_rows_are_skipped_and_good_rules_are_kept(self):
        good = {
            "id": "good-allow", "effect": "allow", "capability": "fs.read",
            "principal": "agent.*", "scope": {"project_id": "p-1"},
            "risk_max": "low",
        }
        bad = [
            {"id": "bad-uses", "effect": "allow", "capability": "fs.read",
             "principal": "a", "scope": {}, "uses_left": "x"},
            {"id": "bad-expiry", "effect": "allow", "capability": "fs.read",
             "principal": "a", "scope": {}, "expires_at": "x"},
            {"id": "bad-scope", "effect": "allow", "capability": "fs.read",
             "principal": "a", "scope": []},
            {"id": "bad-effect", "effect": "maybe", "capability": "fs.read",
             "principal": "a", "scope": {}, "risk_max": "high"},
            {"id": "bad-capability", "effect": "allow", "capability": 123,
             "principal": "a", "scope": {}, "risk_max": "high"},
            {"id": "bad-risk", "effect": "allow", "capability": "fs.read",
             "principal": "a", "scope": {}, "risk_max": "extreme"},
            {"id": "bad-regex", "effect": "allow", "capability": "shell.exec",
             "principal": "a", "scope": {"command_regex": "("},
             "risk_max": "high"},
        ]
        self._write_rules([good, *bad])

        with self.assertLogs("sfo.test", level="WARNING") as captured:
            policy = self._engine()

        self.assertEqual([rule.id for rule in policy.list_rules()], ["good-allow"])
        self.assertEqual(policy.rejected, len(bad))
        self.assertEqual(
            [entry["rule_id"] for entry in policy.rejected_rules],
            [row["id"] for row in bad])
        self.assertIn("rejecting invalid rule", "\n".join(captured.output))
        # The quarantine is announced on the event stream too.
        rejected = [record for record in self.eventlog.tail(50)
                    if record.type == "rule.rejected"]
        self.assertEqual(len(rejected), len(bad))
        self.assertTrue(all("reason" in record.data for record in rejected))
        # The surviving rule still evaluates normally.
        decision = policy.evaluate("agent.coder.1", "fs.read",
                                   {"project_id": "p-1"}, risk="low")
        self.assertEqual(decision.outcome, "allow")

    def test_corrupt_json_file_is_ignored_without_raising(self):
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        self.rules_path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("sfo.test", level="WARNING"):
            policy = self._engine()
        self.assertFalse(policy.list_rules())

    def test_rule_from_dict_is_strict_but_keeps_good_data(self):
        good = Rule(id="rule-1", effect="allow", capability="fs.read",
                    principal="agent.*", scope={"project_id": "p-1"},
                    created_at=1)
        self.assertEqual(Rule.from_dict(good.to_dict()), good)
        self.assertEqual(
            Rule.from_dict({"id": "r", "effect": "deny",
                            "capability": "*", "principal": "a",
                            "scope": None}).scope, {})
        for bad in (
            {"uses_left": "x"},
            {"uses_left": True},
            {"uses_left": 1.0},
            {"expires_at": "x"},
            {"expires_at": True},
            {"expires_at": 1.5},
            {"scope": []},
            {"effect": "maybe"},
            {"capability": 123},
            {"risk_max": "extreme"},
            {"risk_max": []},
            {"principal": ""},
        ):
            with self.subTest(bad=bad):
                payload = {"id": "r", "effect": "allow",
                           "capability": "fs.read", "principal": "a",
                           "scope": {}, **bad}
                with self.assertRaises(ValueError):
                    Rule.from_dict(payload)


class RuleActiveRobustnessTests(unittest.TestCase):
    def test_corrupt_lifetime_data_fails_closed(self):
        cases = (
            ("expires_at", "x"),
            ("expires_at", True),
            ("expires_at", float("nan")),
            ("uses_left", "x"),
            ("uses_left", True),
            ("uses_left", 1.0),
            ("uses_left", float("inf")),
        )
        for field, value in cases:
            rule = Rule(id="rule-x", effect="allow", capability="fs.read")
            setattr(rule, field, value)
            with self.subTest(field=field, value=repr(value)):
                with self.assertLogs("sfo.security.policy", level="WARNING"):
                    self.assertFalse(rule.active())

        # An exhausted (even nonsensically negative) counter is simply inactive.
        rule = Rule(id="rule-x", effect="allow", capability="fs.read",
                    uses_left=-1)
        self.assertFalse(rule.active())

    def test_valid_lifetime_still_works(self):
        rule = Rule(id="rule-x", effect="allow", capability="fs.read",
                    expires_at=100, uses_left=2)
        self.assertTrue(rule.active(now_ms=99))
        self.assertFalse(rule.active(now_ms=100))
        rule.expires_at = None
        rule.uses_left = 0
        self.assertFalse(rule.active(now_ms=1))


class RuleWriteValidationTests(RuleRobustnessTestBase):
    def test_add_rule_rejects_invalid_matchers_without_writing(self):
        bad_scopes = (
            {"command_regex": "("},
            {"command_regex": 123},
            {"path_glob": "relative/*"},
            {"path_glob": "/ws/../etc/*"},
            {"path_glob": "/ws/proj/["},
            {"url_prefix": "not-a-url"},
            {"url_prefix": "https://user:pass@example.com/"},
        )
        for scope in bad_scopes:
            with self.subTest(scope=scope):
                with self.assertRaises(ValidationError):
                    self.policy.add_rule(effect="allow", capability="fs.read",
                                         scope=scope)
                self.assertFalse(self.rules_path.exists(),
                                 "a rejected rule must not be persisted")
        self.assertFalse(self.policy.list_rules())

    def test_bad_update_patch_is_400_and_changes_nothing(self):
        rule = self.policy.add_rule(
            effect="allow", capability="fs.read", principal="agent.*",
            scope={"path_glob": str(self.root / "projects" / "*")},
            note="original")
        before_memory = self._memory()
        before_file = self._file_bytes()

        patches = (
            {"uses_left": "x"},
            {"uses_left": True},
            {"uses_left": 1.0},
            {"expires_at": "x"},
            {"expires_at": True},
            {"expires_at": 1.5},
            {"scope": []},
            {"effect": "maybe"},
            {"capability": 123},
            {"risk_max": "extreme"},
            {"risk_max": []},
            {"principal": ""},
            {"scope": {"command_regex": "("}},
            {"scope": {"path_glob": "relative/*"}},
            {"scope": {"url_prefix": "not-a-url"}},
        )
        for patch in patches:
            with self.subTest(patch=patch):
                with self.assertRaises(ValidationError):
                    self.policy.update_rule(rule.id, patch)
                self.assertEqual(self._memory(), before_memory)
                self.assertEqual(self._file_bytes(), before_file)
        self.assertEqual(self.policy.get_rule(rule.id).note, "original")

    def test_good_rules_round_trip_add_update_delete(self):
        rule = self.policy.add_rule(
            effect="deny", capability="shell.exec", principal="agent.*",
            scope={"command_regex": r"^rm(?:\s.*)?$",
                   "path_glob": str(self.root / "projects" / "*")},
            risk_max="high", note="no rm")
        self.assertTrue(self.rules_path.exists())

        reloaded = self._engine()
        self.assertEqual(reloaded.rejected, 0)
        self.assertEqual([r.to_dict() for r in reloaded.list_rules()],
                         [rule.to_dict()])

        updated = reloaded.update_rule(rule.id, {"note": "still no rm",
                                                 "risk_max": "medium",
                                                 "expires_at": 2 ** 62})
        self.assertEqual(updated.note, "still no rm")
        self.assertEqual(self._engine().get_rule(rule.id).risk_max, "medium")

        self.assertTrue(reloaded.delete_rule(rule.id))
        self.assertFalse(self._engine().list_rules())
        self.assertFalse(reloaded.delete_rule(rule.id))
        names = [record.type for record in self.eventlog.tail(50)]
        for expected in ("rule.created", "rule.updated", "rule.deleted"):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
