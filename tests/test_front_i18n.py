"""i18n guardrails for the Astryx console.

zh is the source of truth; every key added to `zh` must exist in `en` (screens
keep their own dictionary file), and every literal `t('key')` in the sources
must resolve to a key some dictionary actually defines.

Run with::

    python3 -m unittest tests.test_front_i18n -v
"""

from __future__ import annotations

import json
import unittest

import re

from tests._frontend import (
    KEYS_DIR,
    SRC,
    dictionary_module_keys,
    i18n_keys,
    literal_t_keys,
    merged_keys,
    read,
    screen_text,
)

_T_LITERAL = re.compile(r"t\('([^']+)'")
_T_TEMPLATE = re.compile(r"t\(\s*`([^`]+)`")


def _template_patterns(text: str) -> list[re.Pattern[str]]:
    r"""Compile ``t(`prefix.${x}.suffix`)`` calls into key patterns."""
    patterns = []
    for template in _T_TEMPLATE.findall(text):
        segments = re.split(r"\$\{[^}]*\}", template)
        body = ".*".join(re.escape(segment) for segment in segments)
        patterns.append(re.compile(rf"^{body}$"))
    return patterns

INDEX_TS = SRC / "i18n" / "index.ts"
DICT_JSON = SRC / "i18n" / "dict.json"
EXTRA_TS = SRC / "i18n" / "extra.ts"


class FrontI18nTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dicts = json.loads(read(DICT_JSON))
        cls.implementation = read(INDEX_TS)

    def test_legacy_dictionary_has_matching_languages(self) -> None:
        zh = set(self.dicts["zh"])
        en = set(self.dicts["en"])
        self.assertGreaterEqual(len(zh), 600, "the legacy dictionary lost keys")
        self.assertEqual(sorted(zh ^ en), [], "zh/en dictionaries disagree")
        self.assertEqual(sorted(k for k, v in self.dicts["zh"].items() if not str(v).strip()), [],
                         "zh dictionary has empty strings")

    def test_every_screen_dictionary_is_complete(self) -> None:
        modules = sorted(KEYS_DIR.glob("*.ts"))
        self.assertGreaterEqual(len(modules), 8, "expected one dictionary per screen")
        problems = []
        for path in modules:
            zh, en = dictionary_module_keys(path)
            if not zh:
                problems.append(f"{path.name}: no zh keys")
            if len(zh) < 3:
                problems.append(f"{path.name}: only {len(zh)} keys")
            missing = sorted(zh - en)
            if missing:
                problems.append(f"{path.name}: missing en keys {missing[:5]}")
        self.assertEqual(problems, [], "; ".join(problems))

    def test_every_literal_key_used_by_the_ui_resolves(self) -> None:
        known = merged_keys()
        used = literal_t_keys()
        self.assertGreaterEqual(len(used), 120, "too few t() literals found — parser drift?")
        missing = sorted(key for key in used if key not in known)
        self.assertEqual(missing, [], f"t() keys with no dictionary entry: {missing}")

    def test_screen_dictionaries_are_actually_used(self) -> None:
        """A screen's dictionary must describe that screen (no copy-paste drift)."""
        problems = []
        for path in sorted(KEYS_DIR.glob("*.ts")):
            screen = path.stem
            keys = i18n_keys(screen)
            text = screen_text(screen)
            patterns = _template_patterns(text)
            unused = sorted(
                key for key in keys
                if f"'{key}'" not in text and not any(p.match(key) for p in patterns)
            )
            budget = max(4, len(keys) // 4)
            if len(unused) > budget:
                problems.append(f"{screen}: {len(unused)}/{len(keys)} keys unused: {unused[:5]}")
        self.assertEqual(problems, [], "; ".join(problems))

    def test_lookup_falls_back_to_zh_and_then_the_key(self) -> None:
        self.assertIn("DICTS.zh[key]", self.implementation)
        self.assertIn("fallback", self.implementation)
        self.assertIn(".replace(/\\{(\\w+)\\}/g", self.implementation)

    def test_language_choice_persists_and_reaches_the_document(self) -> None:
        self.assertIn("sfo-lang", self.implementation)
        self.assertIn("document.documentElement.lang", self.implementation)
        self.assertIn("localStorage", self.implementation)

    def test_shell_dictionary_and_extra_stay_apart(self) -> None:
        """Shell strings live in extra.ts; screens own keys/<screen>.ts."""
        self.assertTrue(EXTRA_TS.is_file())
        extra = read(EXTRA_TS)
        self.assertIn("export const zh", extra)
        self.assertIn("export const en", extra)
        self.assertIn("nav.workspace", extra)


if __name__ == "__main__":
    unittest.main()
