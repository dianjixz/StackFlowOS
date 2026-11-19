"""Structural guardrails for the hand-written operator UI.

These tests are deliberately text-level: the UI has no build step and no JS
runtime in the Python test suite, so we check the contracts that matter most:

* every module / stylesheet referenced by index.html exists (and the JS module
  graph is closed and local);
* all ``getElementById`` targets exist in index.html;
* no external HTTP(S) assets, no ``@import``, no inline event handlers;
* every ``/api/...`` endpoint the frontend calls is present in the real
  ``stackflowos/web/api.py`` route table (or the server's SSE route).

Run with::

    python3 -m unittest tests.test_web_assets -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "stackflowos" / "webassets"
INDEX = ASSETS / "index.html"
CSS_DIR = ASSETS / "css"
JS_DIR = ASSETS / "js"
API_PY = ROOT / "stackflowos" / "web" / "api.py"
SERVER_PY = ROOT / "stackflowos" / "web" / "server.py"

XML_NAMESPACE = "http://www.w3.org/2000/svg"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def normalize_endpoint(path: str) -> str:
    """Turn both route regexes and JS templates into a comparable shape."""
    path = path.split("?", 1)[0]
    path = re.sub(r"\$\{[^}]+\}", "{}", path)
    path = path.replace("([^/]+)", "{}")
    path = re.sub(r"\([^)]*\)", "{}", path)
    return path.rstrip("/") or "/"


class WebAssetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = read(INDEX)
        cls.css = "\n".join(read(path) for path in sorted(CSS_DIR.glob("*.css")))
        cls.js_files = sorted(JS_DIR.glob("*.js"))
        cls.js_text = "\n".join(read(path) for path in cls.js_files)
        cls.api_text = read(API_PY)
        cls.server_text = read(SERVER_PY)

    # -- index.html ------------------------------------------------------
    def test_index_referenced_assets_exist(self) -> None:
        refs = re.findall(r'(?:src|href)="([^"]+)"', self.html)
        self.assertTrue(any(ref.endswith("/js/main.js") for ref in refs),
                        "index.html must load /js/main.js as a module")
        self.assertTrue(any(ref.endswith("/css/os.css") for ref in refs),
                        "index.html must load /css/os.css")
        for ref in refs:
            if ref.startswith(("#", "data:", "mailto:")):
                continue
            self.assertFalse(ref.startswith(("http://", "https://", "//")),
                             f"external asset reference: {ref}")
            target = ASSETS / ref.lstrip("/")
            self.assertTrue(target.is_file(), f"missing asset referenced by index.html: {ref}")

        scripts = re.findall(r'<script\b[^>]*src="([^"]+)"[^>]*>', self.html)
        self.assertTrue(scripts, "index.html has no module script")
        for script in scripts:
            self.assertTrue((ASSETS / script.lstrip("/")).is_file(),
                            f"missing script: {script}")

    def test_js_module_graph_is_closed_and_local(self) -> None:
        self.assertGreaterEqual(len(self.js_files), 7)
        index_scripts = {
            (ASSETS / src.lstrip("/")).resolve()
            for src in re.findall(r'<script\b[^>]*src="([^"]+)"[^>]*>', self.html)
        }
        self.assertTrue(index_scripts, "index.html has no scripts")
        reachable = set(index_scripts)
        queue = list(index_scripts)
        while queue:
            current = queue.pop()
            source = read(current)
            specifiers = re.findall(r"""\bfrom\s+['"]([^'"]+)['"]""", source)
            specifiers += re.findall(r"""\bimport\s+['"]([^'"]+)['"]""", source)
            for specifier in specifiers:
                self.assertFalse(
                    specifier.startswith(("http://", "https://", "//")),
                    f"external module import in {current.name}: {specifier}",
                )
                if not specifier.startswith("."):
                    continue
                target = (current.parent / specifier).resolve()
                self.assertTrue(target.is_file(),
                                f"{current.name} imports missing module {specifier}")
                if target not in reachable:
                    reachable.add(target)
                    queue.append(target)
        on_disk = {path.resolve() for path in self.js_files}
        orphaned = sorted(path.name for path in on_disk - reachable)
        self.assertEqual(orphaned, [], f"JS modules not reachable from index.html: {orphaned}")

    def test_get_element_by_id_targets_exist(self) -> None:
        ids = set(re.findall(r'\bid="([^"]+)"', self.html))
        requested = set(re.findall(r"""getElementById\(\s*['"]([^'"]+)['"]\s*\)""", self.js_text))
        missing = sorted(requested - ids)
        self.assertEqual(missing, [], f"JS references missing element ids: {missing}")

    def test_no_inline_event_handlers(self) -> None:
        matches = re.findall(r"\son[a-z]+\s*=", self.html, flags=re.IGNORECASE)
        self.assertEqual(matches, [], "inline event handler attributes are not allowed")

    # -- offline / no build step ----------------------------------------
    def test_no_external_http_assets(self) -> None:
        haystack = "\n".join([self.html, self.css, self.js_text])
        urls = re.findall(r"https?://[^\s\"'<>)]+", haystack)
        unexpected = sorted({url for url in urls if not url.startswith(XML_NAMESPACE)})
        self.assertEqual(unexpected, [], f"external network references: {unexpected}")

    def test_css_has_no_import_and_has_design_tokens(self) -> None:
        self.assertNotIn("@import", self.css)
        for token in ("--bg:", "--accent:", "--ok:", "--warn:", "--danger:", "--mono:"):
            self.assertIn(token, self.css, f"missing CSS token {token}")

    # -- API contract ----------------------------------------------------
    def test_api_endpoints_used_by_ui_exist_in_route_table(self) -> None:
        routes: set[str] = set()
        for method, pattern in re.findall(
            r'r\(\s*"(GET|POST|PATCH|DELETE)"\s*,\s*r?"([^"]+)"', self.api_text
        ):
            self.assertIn(method, {"GET", "POST", "PATCH", "DELETE"})
            routes.add(normalize_endpoint(pattern))
        # SSE lives in server.py rather than the JSON route table.
        for literal in re.findall(r'"(/api/[^"]+)"', self.server_text):
            routes.add(normalize_endpoint(literal))
        self.assertIn("/api/events/stream", routes)

        api_calls = re.findall(r"['\"`](/api/[^'\"`\s]+)['\"`]", self.js_text)
        used = {normalize_endpoint(path) for path in api_calls}
        self.assertTrue(used, "could not find any API paths in the UI modules")
        missing = sorted(path for path in used if path not in routes)
        self.assertEqual(missing, [], f"UI calls endpoints absent from api.py: {missing}")


if __name__ == "__main__":
    unittest.main()
