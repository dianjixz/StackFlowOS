"""Guardrails for the 单元（units）and 事件（events）screens.

The old console rendered these as hand-written DOM; the Astryx rewrite keeps the
same capability surface (filters, detail inspector, dependency graph, live event
log) composed from design-system components.

Run with::

    python3 -m unittest tests.test_front_units -v
"""

from __future__ import annotations

import unittest

from tests._frontend import SCREENS, read, screen_text


class FrontUnitsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.units = screen_text("units")
        cls.events = screen_text("events")

    def test_units_screen_is_split_into_readable_parts(self) -> None:
        folder = SCREENS / "units"
        for name in ("index.tsx", "UnitDetailDrawer.tsx", "DependencyGraph.tsx", "model.ts"):
            self.assertTrue((folder / name).is_file(), f"missing units part: {name}")

    def test_units_uses_the_real_control_plane_endpoints(self) -> None:
        for call in ("getUnitsSummary", "getUnit", "probeUnit", "unitAction", "killUnit"):
            self.assertIn(f"api.{call}", self.units, f"units screen lost {call}")
        self.assertIn("/api/units", (SCREENS.parent / "api" / "client.ts").read_text())

    def test_units_shows_tiers_requirement_graph_and_actions(self) -> None:
        for marker in ("tier", "requires", "wants", "DependencyGraph", "probe", "kill"):
            self.assertIn(marker, self.units, f"units screen lost '{marker}'")
        # Detail opens as an overlay, not a second navigation level.
        self.assertTrue(
            "Drawer" in self.units or "BottomSheet" in self.units or "Dialog" in self.units,
            "unit details should open in a drawer/dialog",
        )

    def test_units_has_empty_and_failure_states(self) -> None:
        self.assertIn("EmptyState", self.units)
        self.assertTrue("isUnsupported" in self.units or "error" in self.units)

    def test_events_filters_the_live_store_snapshot(self) -> None:
        self.assertIn("useStore", self.events)
        self.assertIn("events", self.events)
        for control in ("paused", "filter", "eventSummary"):
            self.assertIn(control, self.events, f"events screen lost '{control}'")
        self.assertIn("CodeBlock", self.events, "event payloads should render as code")

    def test_navigation_registers_both_screens(self) -> None:
        manifest = read(SCREENS.parent / "shell" / "manifest.ts")
        self.assertIn("units:", manifest)
        self.assertIn("events:", manifest)
        self.assertIn("screens/units", manifest)
        self.assertIn("screens/events", manifest)


if __name__ == "__main__":
    unittest.main()
