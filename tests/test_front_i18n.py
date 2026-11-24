"""Static guardrails for the zh/en i18n layer.

The UI has no build step, so these tests parse the hand-written modules the
same way ``tests/test_web_assets.py`` does:

* ``js/i18n.js`` ships both dictionaries and the required key families;
* lookup falls back to Chinese, interpolation is supported and the choice is
  persisted in localStorage;
* ``index.html`` loads i18n before the application module and uses
  ``data-i18n*`` attributes instead of inline scripts;
* the language switch lives in settings and does not reload the page.

Run with::

    python3 -m unittest tests.test_front_i18n -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "stackflowos" / "webassets"
INDEX = ASSETS / "index.html"
I18N = ASSETS / "js" / "i18n.js"
APPS = ASSETS / "js" / "apps.js"
MAIN = ASSETS / "js" / "main.js"

#: Key families the reviewer asked for: navigation/dock, settings, units,
#: approvals, sessions, empty states and buttons.
REQUIRED_PREFIXES = (
    "app.",
    "dock.",
    "chat.",
    "units.",
    "approvals.",
    "session.",
    "llm.",
    "cap.",
    "settings.",
    "projects.",
    "agents.",
    "events.",
    "common.",
    "status.",
    "deps.",
    "git.",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def dictionary_keys(source: str, name: str) -> set[str]:
    """Keys of one ``const <name> = { ... };`` object literal."""
    match = re.search(
        rf"const {name} = \{{(.*?)\n\}};", source, flags=re.S)
    if not match:
        raise AssertionError(f"dictionary {name} not found")
    return set(re.findall(r"'([A-Za-z0-9_.-]+)':", match.group(1)))


class I18nTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = read(I18N)
        cls.zh = dictionary_keys(cls.source, "zh")
        cls.en = dictionary_keys(cls.source, "en")
        cls.html = read(INDEX)
        cls.apps = read(APPS)
        cls.main = read(MAIN)

    def test_both_dictionaries_exist_and_are_non_trivial(self) -> None:
        self.assertGreaterEqual(len(self.zh), 150, "zh dictionary looks incomplete")
        self.assertGreaterEqual(len(self.en), 100, "en dictionary looks incomplete")

    def test_required_key_families_are_covered_in_both_languages(self) -> None:
        for prefix in REQUIRED_PREFIXES:
            zh = sorted(key for key in self.zh if key.startswith(prefix))
            en = sorted(key for key in self.en if key.startswith(prefix))
            self.assertTrue(zh, f"zh dictionary has no keys for {prefix}")
            self.assertTrue(en, f"en dictionary has no keys for {prefix}")

    def test_english_never_invents_keys_missing_from_chinese(self) -> None:
        missing = sorted(self.en - self.zh)
        self.assertEqual(missing, [], f"en keys without a zh fallback: {missing}")

    def test_key_navigation_copy_exists(self) -> None:
        for key in (
            "dock.projects", "dock.agents", "dock.units", "dock.approvals",
            "dock.events", "dock.settings",
            "chat.heading", "chat.input.placeholder", "chat.empty.title",
            "units.title", "units.tier.system", "units.tier.service",
            "units.tier.worker", "units.filter.issues",
            "units.detail.descriptor", "units.detail.deps", "units.detail.actions",
            "units.action.probe", "units.action.restart", "units.action.disable",
            "units.action.enable", "units.action.drain",
            "units.empty.none.title", "units.empty.broken.title",
            "approvals.title", "approvals.empty.title", "approvals.allow_once",
            "approvals.deny",
            "session.title", "session.rotate", "session.logout",
            "session.expired.title",
            "settings.tab.general", "settings.tab.ai", "settings.tab.capabilities",
            "settings.tab.security", "settings.language.title",
            "llm.usage.title", "llm.models.title", "llm.circuit",
            "cap.skills.title", "cap.plugins.title", "cap.mcp.title",
            "common.refresh", "common.save", "common.close",
        ):
            self.assertIn(key, self.zh, f"zh is missing {key}")
            self.assertIn(key, self.en, f"en is missing {key}")

    def test_lookup_falls_back_to_chinese_and_interpolates(self) -> None:
        self.assertIn("DICTS.zh[key]", self.source,
                      "missing zh fallback in t()")
        self.assertIn("params", self.source)
        self.assertIn(r"/\{(\w+)\}/g", self.source,
                      "t() should interpolate {name} parameters")
        self.assertIn("export function t(", self.source)
        self.assertIn("export function setLang(", self.source)
        self.assertIn("export function applyI18n(", self.source)

    def test_language_persists_and_switches_without_reload(self) -> None:
        self.assertIn("localStorage.setItem", self.source)
        self.assertIn("localStorage.getItem", self.source)
        self.assertIn("sfo-lang", self.source)
        self.assertIn("document.documentElement.lang", self.source)
        # Settings exposes a two-button language picker that calls setLang and
        # re-renders through the store bus; no page reload may be required.
        self.assertIn("function languageCard()", self.apps)
        self.assertIn("setLang(option)", self.apps)
        self.assertIn("on('lang'", self.main)
        self.assertIn("rerenderAfterLanguageChange", self.main)

    def test_index_uses_data_i18n_and_load_order(self) -> None:
        scripts = re.findall(r'<script\b[^>]*src="([^"]+)"[^>]*>', self.html)
        self.assertIn("/js/i18n.js", scripts)
        self.assertIn("/js/main.js", scripts)
        self.assertLess(scripts.index("/js/i18n.js"), scripts.index("/js/main.js"),
                        "i18n.js must be loaded before main.js")
        for tag in re.findall(r"<script\b[^>]*>", self.html):
            self.assertIn('type="module"', tag,
                          f"non-module script tag: {tag}")

        for key in (
            "dock.projects", "dock.agents", "dock.units", "dock.approvals",
            "dock.events", "dock.settings", "chat.heading", "shell.skip",
            "work.heading",
        ):
            self.assertIn(f'data-i18n="{key}"', self.html,
                          f"index.html lost data-i18n for {key}")
        self.assertIn('data-i18n-placeholder="chat.input.placeholder"', self.html)
        self.assertIn("data-i18n-aria-label", self.html)
        self.assertNotRegex(self.html, r"\son[a-z]+\s*=",
                            "inline event handlers are forbidden")

    def test_static_scan_covers_text_and_attributes(self) -> None:
        for attribute in ("data-i18n", "data-i18n-title",
                          "data-i18n-aria-label", "data-i18n-placeholder"):
            self.assertIn(attribute, self.source,
                          f"applyI18n does not scan {attribute}")


if __name__ == "__main__":
    unittest.main()
