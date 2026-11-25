"""Pagination contract for the extension-owned catalogue endpoints.

``GET /api/capabilities`` (``capabilities.py``) and ``GET /api/skills``
(``skills/``) register their own routes through the extension contract, so the
shared web layer applies ``limit``/``offset`` on top of their ``data``
envelope.  No parameter keeps the old payload exactly; when paging is requested
the response gains ``data.pagination`` and only that page of rows.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.web.api import WebAPI

REPO = default_root()

ALPHA = """---
name: Alpha
version: 1.0.0
description: Alpha skill
triggers: hello
---
# Alpha

Use this when the user greets you.
"""

BETA = """---
name: Beta
version: 1.0.0
description: Beta skill
triggers: beta
---
# Beta

Use this for beta work.
"""


class CataloguePaginationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(base))
        tmp = Path(self._tmp.name)
        state_dir = tmp / "state"
        for skill_id, text in (("alpha", ALPHA), ("beta", BETA)):
            folder = state_dir / "skills" / skill_id
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(text, "utf-8")
        cfg = Config.load(root=tmp, state_dir=state_dir)
        cfg = cfg.update({"skills": {"dirs": ["skills"], "enabled": []},
                          "plugins": {"dirs": [], "enabled": []},
                          "mcp": {"servers": []}})
        self.app = App(cfg)
        await self.app.start()
        self.api = WebAPI(self.app)

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def api_call(self, path, query=None):
        status, payload = await self.api.handle("GET", path, query or {}, b"")
        return status, payload

    # -- capabilities ----------------------------------------------------
    async def test_capabilities_without_paging_is_unchanged(self):
        status, payload = await self.api_call("/api/capabilities",
                                              {"type": "tool"})
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertNotIn("pagination", data)
        self.assertGreaterEqual(len(data["capabilities"]), 3)
        self.assertEqual(data["count"], len(data["capabilities"]))

    async def test_capabilities_limit_and_offset_slice_the_catalogue(self):
        status, base = await self.api_call("/api/capabilities",
                                           {"type": "tool"})
        self.assertEqual(status, 200)
        rows = base["data"]["capabilities"]

        status, payload = await self.api_call(
            "/api/capabilities",
            {"type": "tool", "limit": "2", "offset": "1"})
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertEqual(data["capabilities"], rows[1:3])
        self.assertEqual(data["count"], 2)
        page = data["pagination"]
        self.assertEqual(page["total"], len(rows))
        self.assertEqual(page["offset"], 1)
        self.assertEqual(page["limit"], 2)
        self.assertEqual(page["returned"], 2)
        self.assertEqual(page["has_more"], len(rows) > 3)

    async def test_capabilities_offset_only_returns_the_tail(self):
        status, base = await self.api_call("/api/capabilities",
                                           {"type": "tool"})
        rows = base["data"]["capabilities"]
        status, payload = await self.api_call(
            "/api/capabilities", {"type": "tool", "offset": "1"})
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertEqual(data["capabilities"], rows[1:])
        self.assertEqual(data["pagination"]["offset"], 1)
        self.assertFalse(data["pagination"]["has_more"])

    async def test_bad_paging_values_are_400(self):
        for query in ({"limit": "0"}, {"limit": "abc"}, {"limit": "-1"},
                      {"offset": "-1"}, {"offset": "x"}):
            status, payload = await self.api_call(
                "/api/capabilities", {"type": "tool", **query})
            self.assertEqual(status, 400, f"{query} -> {status} {payload}")
            self.assertEqual(payload["error"]["code"], "BAD_REQUEST")

    # -- skills ----------------------------------------------------------
    async def test_skills_without_paging_is_unchanged(self):
        status, payload = await self.api_call("/api/skills")
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertNotIn("pagination", data)
        self.assertEqual(len(data["skills"]), 2)
        self.assertEqual(data["summary"]["total"], 2)

    async def test_skills_limit_and_offset_slice_the_catalogue(self):
        status, base = await self.api_call("/api/skills")
        rows = base["data"]["skills"]
        self.assertEqual(len(rows), 2)

        status, payload = await self.api_call(
            "/api/skills", {"limit": "1", "offset": "1"})
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertEqual(len(data["skills"]), 1)
        self.assertEqual(data["skills"], rows[1:2])
        page = data["pagination"]
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["offset"], 1)
        self.assertEqual(page["limit"], 1)
        self.assertEqual(page["returned"], 1)
        self.assertFalse(page["has_more"])
        # The summary keeps describing the whole catalogue, not the page.
        self.assertEqual(data["summary"]["total"], 2)

    async def test_skills_beyond_the_end_is_an_empty_page(self):
        status, payload = await self.api_call(
            "/api/skills", {"offset": "10"})
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertEqual(data["skills"], [])
        self.assertEqual(data["pagination"]["total"], 2)
        self.assertFalse(data["pagination"]["has_more"])


if __name__ == "__main__":
    unittest.main()
