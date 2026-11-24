"""Static guardrails for the unit explorer (three tiers, drawer, graph, actions).

These tests pin the contract the reviewer asked for without running a browser:

* the units page renders three groups, an overview bar and the four filters;
* clicking a row opens a detail drawer backed by ``GET /api/units/{id}`` with
  descriptor / runtime / lease / dependency graph / recent events;
* action buttons are driven by ``descriptor.actions`` and call the probe /
  actions endpoints, with 403/409 friendly handling;
* "no units" and "broken" are distinct empty states;
* the dependency drawing is local SVG (no library) and all text goes through
  textContent.

Run with::

    python3 -m unittest tests.test_front_units -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "stackflowos" / "webassets"
JS = ASSETS / "js"
UNITS = JS / "units.js"
API = JS / "api.js"
UI = JS / "ui.js"
CSS = ASSETS / "css" / "os.css"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class UnitsPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.units = read(UNITS)
        cls.api = read(API)
        cls.ui = read(UI)
        cls.css = read(CSS)

    def test_three_tier_groups_are_rendered(self) -> None:
        self.assertIn("const TIERS = ['system', 'service', 'worker']", self.units)
        self.assertIn("t(`units.tier.${tier}`)", self.units)
        self.assertIn("t(`units.tier.${tier}.hint`)", self.units)
        self.assertIn("function renderTier(", self.units)
        self.assertIn("unit-group-", self.units)
        i18n = read(JS / "i18n.js")
        for tier in ("system", "service", "worker"):
            self.assertIn(f"'units.tier.{tier}'", i18n,
                          f"missing tier label for {tier}")

    def test_overview_bar_shows_health_counts_and_problems(self) -> None:
        self.assertIn("function renderOverview(", self.units)
        for key in ("units.overview.health", "units.problems", "units.count"):
            self.assertIn(key, self.units, f"overview loses {key}")
        self.assertIn("t(`units.health.${overall}`", self.units)
        i18n = read(JS / "i18n.js")
        for health in ("ok", "degraded", "down"):
            self.assertIn(f"'units.health.{health}'", i18n)
        self.assertIn("view.summary", self.units)
        self.assertIn("function countTier(", self.units)
        self.assertIn("function health(", self.units)

    def test_filters_cover_all_issues_processes_project_search(self) -> None:
        self.assertIn("units.filter.all", self.units)
        self.assertIn("units.filter.issues", self.units)
        self.assertIn("units.filter.processes", self.units)
        self.assertIn("unit-project-filter", self.units)
        self.assertIn("unit-search", self.units)
        self.assertIn("view.query", self.units)
        self.assertIn("view.projectId", self.units)
        self.assertIn("view.filter", self.units)

    def test_row_click_opens_drawer_with_detail_endpoint(self) -> None:
        self.assertIn("function openUnitDrawer(", self.units)
        self.assertIn("onclick: () => openUnitDrawer(", self.units)
        self.assertIn("api.getUnit(", self.units)
        self.assertIn("/api/units/${enc(id)}", self.api)
        for key in ("units.detail.runtime", "units.detail.lease",
                    "units.detail.deps", "units.detail.events",
                    "units.detail.descriptor"):
            self.assertIn(key, self.units, f"drawer loses {key}")

    def test_drawer_shows_runtime_lease_and_endpoints(self) -> None:
        self.assertIn("function runtimeText(", self.units)
        self.assertIn("runtime?.mode", self.units)
        self.assertIn("host_unit", self.units)
        self.assertIn("units.runtime.in_process", self.units)
        self.assertIn("function renderLease(", self.units)
        self.assertIn("lease.ttl_s", self.units)
        self.assertIn("function renderEndpoints(", self.units)

    def test_dependency_graph_is_local_svg_with_requires_and_wants(self) -> None:
        self.assertIn("function dependencyGraph(", self.units)
        self.assertIn("createElementNS", self.units)
        self.assertIn("'requires'", self.units)
        self.assertIn("'wants'", self.units)
        self.assertIn("marker-end", self.units)
        self.assertIn("units.deps.missing", self.units)
        # no chart library / external asset
        for forbidden in ("d3", "echarts", "chart.js", "cytoscape", "vis-network"):
            self.assertNotIn(forbidden, self.units.lower())
        self.assertIn(".unit-dep-graph", self.css)

    def test_actions_are_descriptor_driven(self) -> None:
        self.assertIn("function actionList(", self.units)
        self.assertIn("units.detail.actions", self.units)
        self.assertIn("drawer.actions", self.units)
        self.assertIn("unit?.actions", self.units)
        for action in ("probe", "restart", "disable", "enable", "drain"):
            self.assertIn(f"units.action.{action}", self.units)
        self.assertIn("ACTION_LABELS", self.units)
        self.assertIn("api.probeUnit(", self.units)
        self.assertIn("api.unitAction(", self.units)

    def test_action_errors_are_friendly_for_403_and_409(self) -> None:
        self.assertIn("isForbidden", self.units)
        self.assertIn("isConflict", self.units)
        self.assertIn("units.action.forbidden", self.units)
        self.assertIn("units.action.conflict", self.units)
        self.assertIn("canWrite()", self.units)
        self.assertIn("operator role", self.units)

    def test_empty_versus_broken_states_are_distinct(self) -> None:
        self.assertIn("units.empty.none.title", self.units)
        self.assertIn("units.empty.broken.title", self.units)
        self.assertIn("units.empty.none.text", self.units)
        self.assertIn("units.empty.broken.text", self.units)
        self.assertIn("view.broken", self.units)
        self.assertIn("view.endpointUnavailable", self.units)
        self.assertIn("isUnsupported", self.units)

    def test_detail_endpoint_degrades_to_snapshot(self) -> None:
        self.assertIn("units.detail.unavailable", self.units)
        self.assertIn("units.detail.forbidden", self.units)
        self.assertIn("localEvents(", self.units)

    def test_descriptor_json_is_collapsible_and_text_safe(self) -> None:
        self.assertIn("unit-descriptor", self.units)
        self.assertIn("JSON.stringify(unit, null, 2)", self.units)
        self.assertNotIn("innerHTML", self.units)
        self.assertNotIn("outerHTML", self.units)
        self.assertNotIn("insertAdjacentHTML", self.units)
        self.assertNotIn("document.write", self.units)

    def test_drawer_layout_loads_live_and_closes_cleanly(self) -> None:
        self.assertIn("function closeUnitDrawer(", self.units)
        self.assertIn("unit-drawer-layer", self.units)
        self.assertIn("Escape", self.units)
        self.assertIn("onUnitsEvent(", self.units)
        self.assertIn("initUnits(", self.units)
        self.assertIn(".unit-drawer", self.css)
        self.assertIn("unit-drawer-open", self.css)

    def test_new_api_methods_are_wired(self) -> None:
        for call in (
            "getUnitsSummary", "getUnit", "probeUnit", "unitAction",
        ):
            self.assertIn(f"{call}:", self.api, f"api.{call} missing")
        self.assertIn("/api/units", self.api)
        self.assertIn("/probe", self.api)
        self.assertIn("/actions", self.api)

    def test_no_hardcoded_action_set(self) -> None:
        """The list must come from the descriptor, not a fixed array."""
        self.assertNotRegex(self.units, r"ACTIONS\s*=\s*\[")
        self.assertIn("Array.isArray(unit?.actions)", self.units)

    def test_css_has_responsive_units_layout(self) -> None:
        self.assertIn(".unit-row", self.css)
        self.assertIn("@media (max-width: 760px)", self.css)
        section = self.css[self.css.index("23 ─ front additions"):]
        self.assertIn(".unit-overview", section)
        self.assertIn(".unit-drawer", section)


if __name__ == "__main__":
    unittest.main()
