"""Guardrails for identity/session, LLM usage and the capability panes.

These are the "operator plumbing" surfaces: the 401 gate, viewer write gate,
LLM usage/models, skills/plugins/MCP and the project workspace git panel.

Run with::

    python3 -m unittest tests.test_front_session_llm -v
"""

from __future__ import annotations

import unittest

from tests._frontend import SRC, read, screen_text, split_pair


class FrontSessionLlmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = read(SRC / "api" / "client.ts")
        cls.session = read(SRC / "session" / "index.ts")
        cls.store = read(SRC / "state" / "store.ts")
        cls.app = read(SRC / "App.tsx")
        cls.settings = screen_text("settings")

    def test_auth_surface_exists(self) -> None:
        for call in ("getSessions", "rotateSessions", "revokeSession", "logout"):
            self.assertIn(call, self.client, f"api client lost {call}")
        for marker in ("useCanWrite", "canWrite", "sfo:unauthorized", "loadSessions"):
            self.assertIn(marker, self.session, f"session module lost {marker}")

    def test_expired_session_blocks_the_console(self) -> None:
        self.assertIn("initAuthGate", self.session)
        self.assertIn("AuthGate", self.app)
        self.assertIn("login.title", self.app)
        # The gate must offer a way out (reload with a fresh launch URL).
        self.assertIn("window.location.reload()", self.app)

    def test_viewer_role_hides_write_actions(self) -> None:
        self.assertIn("role !== 'viewer'", self.session)
        self.assertTrue("useCanWrite" in self.settings or "ViewerNote" in self.settings)

    def test_llm_usage_and_models_are_wired(self) -> None:
        for call in ("getLlmUsage", "getLlmModels", "testLlm"):
            self.assertIn(call, self.client)
        for marker in ("getLlmUsage", "getLlmModels", "routes", "fallback"):
            self.assertIn(marker, self.settings, f"AI pane lost '{marker}'")
        self.assertTrue("budget" in self.settings or "tokens" in self.settings)

    def test_capability_catalog_is_wired(self) -> None:
        for call in ("getSkills", "setSkillEnabled", "getPlugins", "loadPlugin",
                     "unloadPlugin", "getMcpServers", "connectMcp", "disconnectMcp",
                     "getMcpTools", "callMcp"):
            self.assertIn(call, self.client, f"api client lost {call}")
        for marker in ("skills", "plugins", "mcp", "capabilities"):
            self.assertIn(marker, self.settings.lower(), f"capabilities pane lost '{marker}'")

    def test_config_is_saved_as_a_patch(self) -> None:
        self.assertIn("patchConfig", self.client)
        self.assertIn("submitConfigPatch", self.settings)
        # Only changed fields are submitted — the panes diff before patching.
        self.assertTrue("changed" in self.settings.lower() or "diff" in self.settings.lower())

    def test_streaming_reducer_keeps_drafts_and_reconnect_semantics(self) -> None:
        self.assertIn("chat.delta", self.store)
        self.assertIn("stream:", self.store)
        self.assertIn("resetStream", self.store)
        self.assertIn("/api/events/stream?since=", self.store)
        # SSE frames are JSON parsed defensively.
        self.assertIn("JSON.parse", self.store)
        self.assertIn("scheduleReconnect", self.store)

    def test_settings_tabs_are_deep_linkable(self) -> None:
        self.assertIn("parseTab", self.settings)
        self.assertIn("query.get('tab')", self.settings)
        self.assertIn("TabList", self.settings)

    def test_git_panel_is_reachable_from_projects(self) -> None:
        projects = screen_text("projects")
        self.assertIn("getProjectGit", self.client)
        self.assertIn("rollbackProjectGit", self.client)
        self.assertIn("rollback", projects.lower())

    def test_dictionary_modules_used_here_are_non_empty(self) -> None:
        for name in ("settings", "agents"):
            path = SRC / "i18n" / "keys" / f"{name}.ts"
            zh_part, en_part = split_pair(read(path))
            self.assertTrue(zh_part and en_part, f"{name} dictionary is empty")


if __name__ == "__main__":
    unittest.main()
