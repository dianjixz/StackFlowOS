"""Guardrails for the 审批（approvals）and 自治（autonomy）screens.

The console is the human's half of the approval system, so the guardrails here
are about capability, not decoration: the pending queue and detail preview must
stay reachable, decisions must go through the decide endpoint, standing rules
must be editable, and the self-driving loop must expose its findings, dispatches,
guardian approvals, envelope counters and an emergency stop.

Run with::

    python3 -m unittest tests.test_front_autonomy -v
"""

from __future__ import annotations

import unittest

from tests._frontend import SRC, i18n_keys, read, screen_text


class FrontAutonomyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = read(SRC / "api" / "client.ts")
        cls.manifest = read(SRC / "shell" / "manifest.ts")
        cls.approvals = screen_text("approvals")
        cls.autonomy = screen_text("autonomy")

    def test_approval_queue_and_inspector_exist(self) -> None:
        for call in ("getApprovals", "getApproval", "decideApproval"):
            self.assertIn(call, self.client, f"api client lost {call}")
        self.assertIn("ApprovalDetailDialog", self.approvals)
        for marker in ("preview", "risk", "expires_at", "mode", "note"):
            self.assertIn(marker, self.approvals, f"approvals screen lost '{marker}'")

    def test_standing_rules_are_editable(self) -> None:
        for call in ("getRules", "createRule", "deleteRule"):
            self.assertIn(call, self.client, f"api client lost {call}")
        for marker in ("RuleDialog", "effect", "capability", "risk_max"):
            self.assertIn(marker, self.approvals, f"approvals screen lost '{marker}'")

    def test_decisions_are_operator_only(self) -> None:
        self.assertIn("useCanWrite", self.approvals)
        self.assertIn("canWrite", self.approvals)

    def test_autonomy_status_surface(self) -> None:
        for call in ("getAutonomy", "runAutonomyPatrol", "patchAutonomyPolicy"):
            self.assertIn(call, self.client, f"api client lost {call}")
        for marker in ("findings", "dispatches", "approvals", "guardrail", "next_run"):
            self.assertIn(marker, self.autonomy, f"autonomy screen lost '{marker}'")

    def test_autonomy_policy_only_patches_changed_fields(self) -> None:
        self.assertIn("PolicyForm", self.autonomy)
        self.assertIn("policyPatch", self.autonomy)
        self.assertIn("patchAutonomyPolicy", self.autonomy)
        # An unchanged form must not round-trip the whole policy.
        self.assertTrue("nothing" in self.autonomy.lower() or "unchanged" in self.autonomy.lower())

    def test_autonomy_has_an_emergency_stop(self) -> None:
        self.assertIn("estop", self.autonomy.lower())
        self.assertIn("Dialog", self.autonomy)
        self.assertIn("enabled", self.autonomy)

    def test_navigation_registers_both_screens(self) -> None:
        self.assertIn("approvals:", self.manifest)
        self.assertIn("autonomy:", self.manifest)
        self.assertIn("screens/approvals", self.manifest)
        self.assertIn("screens/autonomy", self.manifest)

    def test_autonomy_dictionary_is_present_in_both_languages(self) -> None:
        keys = i18n_keys("autonomy")
        self.assertGreaterEqual(len(keys), 20)
        self.assertIn("autonomy.title", keys)
        self.assertTrue(any(key.startswith("autonomy.policy") for key in keys))

    def test_approvals_dictionary_is_present_in_both_languages(self) -> None:
        keys = i18n_keys("approvals")
        self.assertGreaterEqual(len(keys), 20)
        self.assertIn("app.approvals", read(SRC / "i18n" / "dict.json"))


if __name__ == "__main__":
    unittest.main()
