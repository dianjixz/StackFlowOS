"""Structural guardrails for the Astryx console.

The console is a React 19 + Astryx single-page app with a Vite build:

* ``webui/src``        the sources (screens, store, api client, dictionaries)
* ``stackflowos/webassets``  the *built* output the Python hub serves

The product never needs Node at runtime, so the Python suite checks the served
bundle and the source contracts textually instead of booting a browser:

* ``index.html`` references only local, existing, hashed assets, with no inline
  script and no inline event handlers;
* the legacy hand-written console (``webassets/js``, ``css/os.css``) is gone;
* every ``/api/...`` path the API client calls exists in the real route table
  (``stackflowos/web/api.py``, the optional subsystems' ``routes()``, or the SSE
  handler in ``web/server.py``);
* screens are built from the design system (Astryx imports, no raw layout divs,
  no hard-coded colors).

Run with::

    python3 -m unittest tests.test_web_assets -v
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "stackflowos" / "webassets"
WEBUI = ROOT / "webui"
SRC = WEBUI / "src"
SCREENS = SRC / "screens"
INDEX = ASSETS / "index.html"
API_PY = ROOT / "stackflowos" / "web" / "api.py"
SERVER_PY = ROOT / "stackflowos" / "web" / "server.py"

#: ``<div>`` is not banned outright (the 401 gate needs a positioned overlay),
#: but a screen built from the design system should not need it for layout.
MAX_RAW_DIVS = 2


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def normalize_endpoint(path: str) -> str:
    """Turn both route regexes and client template strings into one shape."""
    path = path.split("?", 1)[0]
    path = re.sub(r"\$\{[^}]+\}", "{}", path)
    path = path.replace("([^/]+)", "{}")
    path = re.sub(r"\([^)]*\)", "{}", path)
    return path.rstrip("/") or "/"


class WebAssetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = read(INDEX)
        cls.built_js = sorted((ASSETS / "assets").glob("*.js"))
        cls.built_css = sorted((ASSETS / "assets").glob("*.css"))
        cls.client_text = read(SRC / "api" / "client.ts")
        cls.store_text = read(SRC / "state" / "store.ts")
        cls.api_text = read(API_PY)
        cls.server_text = read(SERVER_PY)
        cls.screen_files = sorted(SCREENS.rglob("*.tsx"))

    # -- index.html ------------------------------------------------------
    def test_index_referenced_assets_exist(self) -> None:
        refs = re.findall(r'(?:src|href)="([^"]+)"', self.html)
        self.assertTrue(refs, "index.html references no assets")
        for ref in refs:
            if ref.startswith(("#", "data:", "mailto:")):
                continue
            self.assertFalse(ref.startswith(("http://", "https://", "//")),
                             f"external asset reference: {ref}")
            target = ASSETS / ref.lstrip("/")
            self.assertTrue(target.is_file(), f"missing asset referenced by index.html: {ref}")

        scripts = re.findall(r'<script\b[^>]*src="([^"]+)"[^>]*>', self.html)
        self.assertEqual(len(scripts), 1, f"expected one module entry, got {scripts}")
        self.assertIn("/assets/", scripts[0], "the entry script must be a built asset")
        self.assertIn('type="module"', self.html)
        self.assertRegex(self.html, r'<link[^>]+rel="stylesheet"[^>]+href="/assets/')

    def test_no_inline_scripts_or_handlers(self) -> None:
        # A <script> without src is an inline script (blocked by the CSP anyway).
        inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                            self.html, flags=re.IGNORECASE | re.DOTALL)
        self.assertEqual(inline, [], "inline script blocks are not allowed")
        handlers = re.findall(r"\son[a-z]+\s*=", self.html, flags=re.IGNORECASE)
        self.assertEqual(handlers, [], "inline event handler attributes are not allowed")

    def test_no_external_http_assets(self) -> None:
        css_text = "\n".join(read(path) for path in self.built_css)
        haystack = "\n".join([self.html, css_text])
        urls = re.findall(r"https?://[^\s\"'<>)]+", haystack)
        # A data-URI favicon carries the SVG XML namespace, not a network load.
        urls = [url for url in urls if not url.startswith("http://www.w3.org/")]
        self.assertEqual(sorted(set(urls)), [], f"external network references: {urls}")
        self.assertNotIn("@import", css_text, "built CSS must not @import remote sheets")

    def test_built_bundle_is_present_and_hashed(self) -> None:
        self.assertTrue(self.built_js, "no built JavaScript under webassets/assets")
        self.assertTrue(self.built_css, "no built CSS under webassets/assets")
        total_js = sum(path.stat().st_size for path in self.built_js)
        total_css = sum(path.stat().st_size for path in self.built_css)
        self.assertGreater(total_js, 200_000, "built JS looks too small to be the console")
        self.assertGreater(total_css, 100_000, "Astryx production CSS is missing")
        for path in self.built_js + self.built_css:
            self.assertRegex(path.name, r"-[A-Za-z0-9_-]{6,}\.", f"unhashed asset: {path.name}")

    def test_legacy_console_is_gone(self) -> None:
        self.assertFalse((ASSETS / "js").exists(), "legacy webassets/js is still shipped")
        self.assertFalse((ASSETS / "css").exists(), "legacy webassets/css is still shipped")
        self.assertFalse((ASSETS / "js" / "apps.js").exists())

    # -- sources ---------------------------------------------------------
    def test_frontend_source_tree_is_present(self) -> None:
        expected = [
            WEBUI / "package.json",
            WEBUI / "vite.config.ts",
            WEBUI / "index.html",
            SRC / "main.tsx",
            SRC / "App.tsx",
            SRC / "api" / "client.ts",
            SRC / "state" / "store.ts",
            SRC / "shell" / "manifest.ts",
            SRC / "shell" / "router.ts",
            SRC / "i18n" / "index.ts",
            SRC / "i18n" / "dict.json",
        ]
        for path in expected:
            self.assertTrue(path.is_file(), f"missing console source: {path}")
        for key in ("chat", "projects", "agents", "units", "approvals",
                    "events", "autonomy", "settings"):
            self.assertTrue((SCREENS / key / "index.tsx").is_file(),
                            f"missing screen: {key}")

    def test_design_system_is_the_ui_layer(self) -> None:
        package = json.loads(read(WEBUI / "package.json"))
        deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
        self.assertIn("@astryxdesign/core", deps)
        self.assertIn("react", deps)
        self.assertGreaterEqual(int(deps["react"].split(".")[0].lstrip("^~")), 19,
                                "Astryx needs React 19+")
        importing = [path for path in self.screen_files
                     if "@astryxdesign/core" in read(path)]
        self.assertGreaterEqual(len(importing), 25,
                                f"only {len(importing)} screen files import Astryx")

    def test_screens_use_components_not_raw_markup(self) -> None:
        offenders = {}
        for path in self.screen_files:
            count = read(path).count("<div")
            if count:
                offenders[str(path.relative_to(SCREENS))] = count
        total = sum(offenders.values())
        self.assertLessEqual(total, MAX_RAW_DIVS,
                             f"screens should compose Astryx primitives, not divs: {offenders}")

    def test_no_hardcoded_colors_in_screens(self) -> None:
        hits = {}
        for path in self.screen_files:
            text = read(path)
            for match in re.findall(r"#[0-9a-fA-F]{6}\b", text):
                hits.setdefault(str(path.relative_to(SCREENS)), set()).add(match)
        self.assertEqual(hits, {}, f"hard-coded colors belong to theme tokens: {hits}")

    # -- API contract ----------------------------------------------------
    def test_api_endpoints_used_by_ui_exist_in_route_table(self) -> None:
        routes: set[str] = set()
        for method, pattern in re.findall(
            r'r\(\s*"(GET|POST|PATCH|DELETE)"\s*,\s*r?"([^"]+)"', self.api_text
        ):
            self.assertIn(method, {"GET", "POST", "PATCH", "DELETE"})
            routes.add(normalize_endpoint(pattern))
        # Optional subsystems register routes as ("GET", r"/api/...", handler).
        for source in (ROOT / "stackflowos").rglob("*.py"):
            if source == API_PY:
                continue
            for _method, pattern in re.findall(
                r'\(\s*"(GET|POST|PATCH|DELETE)"\s*,\s*r?"(/api/[^"]+)"', read(source)
            ):
                routes.add(normalize_endpoint(pattern))
        # SSE is served by server.py, not the JSON route table.
        for literal in re.findall(r'"(/api/[^"]+)"', self.server_text):
            routes.add(normalize_endpoint(literal))
        self.assertIn("/api/events/stream", routes)

        calls = re.findall(r"['\"`](/api/[^'\"`\s]+)['\"`]", self.client_text + self.store_text)
        used = {normalize_endpoint(path) for path in calls}
        self.assertGreaterEqual(len(used), 40, "the API client looks truncated")
        missing = sorted(path for path in used if path not in routes)
        self.assertEqual(missing, [], f"UI calls endpoints absent from the route table: {missing}")

    def test_api_client_keeps_the_hardening(self) -> None:
        self.assertIn("credentials: 'same-origin'", self.client_text)
        self.assertIn("'X-SFO-Request'", self.client_text)
        self.assertIn("sfo:unauthorized", self.client_text)
        self.assertIn("cache: 'no-store'", self.client_text)
        self.assertNotIn("localStorage.setItem('sfo-token", self.client_text)


if __name__ == "__main__":
    unittest.main()
