"""Static guardrails for the "自治" (autonomy) console app (#autonomy).

The console has no build step and no JS runtime in the Python suite, so — like
``tests/test_front_i18n.py`` — these checks parse the hand-written modules:

* ``js/autonomy.js`` exists, exports the lifecycle/render hooks and never
  builds markup from dynamic strings;
* ``api.js`` wraps exactly the three autonomy endpoints from the plan;
* ``apps.js`` registers the APP_META entry, the renderSheet case, the first
  load on open and the event-driven refresh lifecycle;
* ``index.html`` has the Dock button (between 审批 and 事件) and the
  ``i-autonomy`` SVG symbol;
* both i18n dictionaries carry every static autonomy key used by the UI;
* the stylesheet has the autonomy layout plus mobile single-column rules;
* the policy form only sends changed autonomy fields (never the whole config).

Run with::

    python3 -m unittest tests.test_front_autonomy -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "stackflowos" / "webassets"
JS_DIR = ASSETS / "js"
INDEX = ASSETS / "index.html"
CSS = ASSETS / "css" / "os.css"
AUTONOMY = JS_DIR / "autonomy.js"
APPS = JS_DIR / "apps.js"
API = JS_DIR / "api.js"
I18N = JS_DIR / "i18n.js"

API_ENDPOINTS = (
    "/api/autonomy/status",
    "/api/autonomy/patrol",
    "/api/autonomy/policy",
)

#: Keys every autonomy build must ship in both dictionaries.  The full key
#: inventory is checked separately by scanning `t(...)` calls.
REQUIRED_I18N_KEYS = (
    "app.autonomy",
    "app.sub.autonomy",
    "dock.autonomy",
    "dock.aria.autonomy",
    "autonomy.title",
    "autonomy.enabled",
    "autonomy.disabled",
    "autonomy.running",
    "autonomy.last_run",
    "autonomy.next_run",
    "autonomy.findings.title",
    "autonomy.findings.empty.title",
    "autonomy.dispatch.title",
    "autonomy.approvals.title",
    "autonomy.policy.title",
    "autonomy.policy.dispatch_mode",
    "autonomy.policy.approve_mode",
    "autonomy.policy.risk_max",
    "autonomy.policy.capabilities",
    "autonomy.policy.save",
    "autonomy.patrol_now",
    "autonomy.viewer_hint",
)

#: Capabilities the plan requires the policy form to expose.
REQUIRED_CAPABILITIES = ("fs.write", "agent.spawn", "net.fetch", "shell.exec")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def dictionary_keys(source: str, name: str) -> set[str]:
    """Keys of one ``const <name> = { ... };`` object literal."""
    match = re.search(rf"const {name} = \{{(.*?)\n\}};", source, flags=re.S)
    if not match:
        raise AssertionError(f"dictionary {name} not found")
    return set(re.findall(r"'([A-Za-z0-9_.-]+)':", match.group(1)))


def function_block(source: str, signature: str) -> str:
    """Best-effort slice from ``signature`` to the next top-level ``\n}``."""
    start = source.find(signature)
    if start < 0:
        raise AssertionError(f"missing {signature!r}")
    end = source.find("\n}", start)
    return source[start:end if end >= 0 else len(source)]


class FrontAutonomyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.autonomy = read(AUTONOMY)
        cls.apps = read(APPS)
        cls.api = read(API)
        cls.i18n = read(I18N)
        cls.html = read(INDEX)
        cls.css = read(CSS)
        cls.zh = dictionary_keys(cls.i18n, "zh")
        cls.en = dictionary_keys(cls.i18n, "en")

    # -- module + API contract -------------------------------------------
    def test_module_exports_lifecycle_and_render(self) -> None:
        self.assertTrue(AUTONOMY.is_file(), "js/autonomy.js is missing")
        for signature in (
            "export function mount(",
            "export function unmount(",
            "export function render(",
            "export function tick(",
        ):
            self.assertIn(signature, self.autonomy, f"autonomy.js lost {signature}")
        # No build step: only local ES modules may be imported.
        for specifier in re.findall(r"""\bfrom\s+['"]([^'"]+)['"]""", self.autonomy):
            self.assertTrue(specifier.startswith("./"),
                            f"external import in autonomy.js: {specifier}")

    def test_api_helpers_use_the_three_contract_endpoints(self) -> None:
        for endpoint in API_ENDPOINTS:
            self.assertIn(endpoint, self.api, f"api.js lost {endpoint}")
        for helper in ("getAutonomy:", "runAutonomyPatrol:", "patchAutonomyPolicy:"):
            self.assertIn(helper, self.api, f"api.js lost {helper}")
        self.assertRegex(self.api,
                         r"getAutonomy:\s*\(\)\s*=>\s*requestJson\('/api/autonomy/status'\)")
        patrol = function_block(self.api, "runAutonomyPatrol:")
        self.assertIn("/api/autonomy/patrol", patrol)
        self.assertIn("dry_run", patrol)
        policy = function_block(self.api, "patchAutonomyPolicy:")
        self.assertIn("/api/autonomy/policy", policy)
        self.assertIn("method: 'PATCH'", policy)

    # -- apps.js registration / routing ----------------------------------
    def test_apps_registers_meta_case_and_first_load(self) -> None:
        self.assertRegex(
            self.apps,
            r"autonomy:\s*\{\s*titleKey:\s*'app\.autonomy',\s*"
            r"subKey:\s*'app\.sub\.autonomy',\s*icon:\s*'autonomy'\s*\}",
            "APP_META has no autonomy entry (titleKey/subKey/icon)",
        )
        self.assertIn("case 'autonomy':", self.apps,
                      "renderSheet has no autonomy case")
        self.assertIn("renderAutonomyApp", self.apps,
                      "renderSheet does not route to the autonomy renderer")
        show_block = function_block(self.apps, "function showApp(")
        self.assertIn("name === 'autonomy'", show_block,
                      "showApp never handles the autonomy app")
        self.assertIn("loadAutonomyStatus", show_block,
                      "showApp does not trigger the first status load")

    def test_apps_refreshes_on_autonomy_events_and_stops_when_closed(self) -> None:
        handler = function_block(self.apps, "on('event', (event) => {")
        self.assertIn("startsWith('autonomy.')", handler,
                      "autonomy events do not schedule a refresh")
        hide = function_block(self.apps, "function hideSheet(")
        self.assertIn("clearTimeout(autonomyTimer)", hide,
                      "hideSheet does not clear the autonomy reload timer")
        self.assertIn("autonomy.unmount()", hide,
                      "hideSheet does not unmount the autonomy module")
        self.assertNotIn("setInterval", self.autonomy,
                         "autonomy.js must not own a timer (apps.js drives the countdown)")

    # -- index.html -------------------------------------------------------
    def test_index_dock_button_sits_between_approvals_and_events(self) -> None:
        dock = re.search(r'<nav class="dock".*?</nav>', self.html, flags=re.S)
        self.assertIsNotNone(dock, "index.html has no Dock nav")
        dock_html = dock.group(0)
        for needle in ('data-open-app="approvals"',
                       'data-open-app="autonomy"',
                       'data-open-app="events"'):
            self.assertIn(needle, dock_html)
        order = [
            dock_html.index('data-open-app="approvals"'),
            dock_html.index('data-open-app="autonomy"'),
            dock_html.index('data-open-app="events"'),
        ]
        self.assertEqual(order, sorted(order),
                         "Dock order must be 审批 → 自治 → 事件")

        button = re.search(
            r'<button[^>]*data-open-app="autonomy".*?</button>', dock_html, flags=re.S)
        self.assertIsNotNone(button, "no Dock button for autonomy")
        text = button.group(0)
        self.assertIn('data-i18n-aria-label="dock.aria.autonomy"', text)
        self.assertIn('data-i18n="dock.autonomy"', text)
        self.assertIn("aria-label=", text)
        self.assertIn('href="#i-autonomy"', text)

    def test_index_defines_the_autonomy_symbol(self) -> None:
        symbol = re.search(r'<symbol id="i-autonomy".*?</symbol>', self.html, flags=re.S)
        self.assertIsNotNone(symbol, "index.html lost the i-autonomy symbol")
        text = symbol.group(0)
        self.assertIn('viewBox="0 0 24 24"', text)
        self.assertIn("stroke=\"currentColor\"", text)

    # -- i18n -------------------------------------------------------------
    def test_i18n_covers_autonomy_keys_in_both_languages(self) -> None:
        for key in REQUIRED_I18N_KEYS:
            self.assertIn(key, self.zh, f"zh is missing {key}")
            self.assertIn(key, self.en, f"en is missing {key}")
        zh_autonomy = {key for key in self.zh if key.startswith("autonomy.")}
        en_autonomy = {key for key in self.en if key.startswith("autonomy.")}
        self.assertGreaterEqual(len(zh_autonomy), 60, "zh autonomy copy looks incomplete")
        self.assertGreaterEqual(len(en_autonomy), 60, "en autonomy copy looks incomplete")

    def test_every_static_t_call_has_a_dictionary_entry(self) -> None:
        sources = "\n".join((self.autonomy, self.apps))
        keys = set(re.findall(
            r"""\bt\(\s*['"](autonomy\.[A-Za-z0-9_.]+)['"]""", sources))
        self.assertTrue(keys, "no literal autonomy t() keys found")
        missing = sorted(key for key in keys if key not in self.zh)
        self.assertEqual(missing, [], f"t() keys absent from zh dictionary: {missing}")

    # -- safety / viewer gate --------------------------------------------
    def test_dynamic_data_is_never_written_as_markup(self) -> None:
        for source, name in ((self.autonomy, "autonomy.js"), (self.apps, "apps.js")):
            for sink in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write"):
                self.assertNotIn(sink, source, f"{name} uses {sink}")

    def test_viewer_controls_are_read_only(self) -> None:
        self.assertIn("canWrite()", self.autonomy,
                      "autonomy.js never consults the viewer gate")
        self.assertIn("disabled: !writable", self.autonomy,
                      "policy controls are not disabled for viewers")
        self.assertIn("t('autonomy.viewer_hint')", self.autonomy)
        self.assertIn("t('autonomy.policy.viewer')", self.autonomy)

    def test_policy_form_patches_only_changed_autonomy_fields(self) -> None:
        self.assertIn("patchAutonomyPolicy", self.apps,
                      "apps.js must save through the autonomy policy endpoint")
        save = function_block(self.apps, "async function saveAutonomyPolicy(")
        self.assertNotIn("patchConfig", save,
                         "the autonomy policy must not be saved via /api/config")
        self.assertIn("data-autonomy-policy", self.autonomy,
                      "policy fields are not marked for the partial collector")
        self.assertIn("function collectPolicyPatch(", self.autonomy)
        for capability in REQUIRED_CAPABILITIES:
            self.assertIn(f"'{capability}'", self.autonomy,
                          f"policy form does not offer {capability}")

    # -- CSS ---------------------------------------------------------------
    def test_css_has_autonomy_layout_and_mobile_rules(self) -> None:
        for selector in (".autonomy-app", ".autonomy-hero", ".autonomy-metrics",
                         ".autonomy-counts", ".autonomy-list",
                         ".autonomy-capability-grid"):
            self.assertIn(selector, self.css, f"os.css lost {selector}")
        media_blocks = re.findall(
            r"@media \(max-width: (?:760|430)px\) \{(.*?)\n\}", self.css, flags=re.S)
        responsive = "\n".join(media_blocks)
        self.assertIn(".autonomy-hero", responsive,
                      "the autonomy hero never collapses on mobile")
        self.assertIn("grid-template-columns", responsive,
                      "mobile autonomy rules do not restack the grid")


if __name__ == "__main__":
    unittest.main()
