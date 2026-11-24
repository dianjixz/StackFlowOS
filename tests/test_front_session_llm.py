"""Static guardrails for sessions, LLM usage, capabilities, git and streaming.

These pin the remaining review gaps without a browser:

* the session panel calls ``/api/sessions*``, rotates/revokes/logs out, hides
  write controls for a known viewer and has a blocking 401 re-login screen;
* the LLM panel reads ``/api/llm/usage`` and ``/api/llm/models``, renders
  tokens/cost/RPM/TPM/budgets/circuit, degrades on 404 and edits
  routes/fallback through the existing ``PATCH /api/config`` form;
* skills/plugins/MCP panels call their APIs and show "未启用" when missing;
* project git calls ``/api/projects/{id}/git(/rollback)``; task dependencies
  use the real ``deps`` model and render an SVG graph;
* chat streaming folds ``chat.delta`` into a draft, replaces it with the final
  message exactly once and survives reconnects without duplicates.

Run with::

    python3 -m unittest tests.test_front_session_llm -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "stackflowos" / "webassets"
JS = ASSETS / "js"
API = JS / "api.js"
APPS = JS / "apps.js"
SESSION = JS / "session.js"
LLM = JS / "llm.js"
CAP = JS / "capabilities.js"
EXTRAS = JS / "projectextras.js"
CHAT = JS / "chat.js"
STORE = JS / "store.js"
AGENTCHAT = JS / "agentchat.js"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class SessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.api = read(API)
        cls.apps = read(APPS)
        cls.session = read(SESSION)
        cls.index = read(ASSETS / "index.html")

    def test_session_api_methods_exist(self) -> None:
        for call in ("getSessions", "rotateSessions", "revokeSession", "logout"):
            self.assertIn(f"{call}:", self.api, f"api.{call} missing")
        for endpoint in ("/api/sessions", "/api/sessions/rotate", "/api/logout"):
            self.assertIn(endpoint, self.api)

    def test_panel_shows_identity_and_session_table(self) -> None:
        self.assertIn("export function renderSessionPanel(", self.session)
        self.assertIn("view.auth", self.session)
        self.assertIn("session.user", self.session)
        self.assertIn("session.role", self.session)
        self.assertIn("session.last_seen", self.session)
        self.assertIn("session.created", self.session)
        self.assertIn("session.revoke", self.session)
        self.assertIn("session.rotate", self.session)
        self.assertIn("session.logout", self.session)
        self.assertIn("renderSessionPanel()", self.apps,
                      "settings should mount the session panel")

    def test_viewer_write_controls_are_hidden(self) -> None:
        self.assertIn("export function canWrite(", self.session)
        self.assertIn("role !== 'viewer'", self.session)
        self.assertIn("session.viewer_hint", self.session)
        self.assertIn("canWrite()", self.apps)

    def test_401_shows_blocking_relogin_gate(self) -> None:
        self.assertIn("sfo:unauthorized", self.api)
        self.assertIn("sfo:unauthorized", self.session)
        self.assertIn("export function showLoginGate(", self.session)
        self.assertIn("session.expired.title", self.session)
        self.assertIn("auth-gate", self.session)
        self.assertIn("initSession(", read(JS / "main.js"))

    def test_topbar_identity_chip_has_index_targets(self) -> None:
        for element_id in ("session-chip", "session-user", "session-role"):
            self.assertIn(f'id="{element_id}"', self.index)
            self.assertIn(element_id, self.session)

    def test_unknown_session_fields_are_tolerated(self) -> None:
        # backend uses created/last_seen and does not tag the current row
        self.assertIn("row.created ?? row.created_at", self.session)
        self.assertIn("last_seen", self.session)
        self.assertIn("rows.length === 1", self.session)


class LlmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.api = read(API)
        cls.llm = read(LLM)
        cls.apps = read(APPS)

    def test_usage_and_models_endpoints(self) -> None:
        self.assertIn("getLlmUsage:", self.api)
        self.assertIn("getLlmModels:", self.api)
        self.assertIn("/api/llm/usage", self.api)
        self.assertIn("/api/llm/models", self.api)

    def test_panel_covers_tokens_cost_rates_budget_circuit(self) -> None:
        for key in ("llm.tokens_in", "llm.tokens_out", "llm.cost",
                    "llm.rpm", "llm.tpm", "llm.circuit", "llm.budget.title",
                    "llm.models.title", "llm.fallback"):
            self.assertIn(key, self.llm, f"usage panel loses {key}")
        self.assertIn("rpm_limit", self.llm)
        self.assertIn("tpm_limit", self.llm)
        self.assertIn("usageBudgets(", self.llm)
        self.assertIn("bar(ratio", self.llm)

    def test_route_shapes_from_backend_are_read(self) -> None:
        # usage returns providers/models/roles as maps and limits/circuit/budget
        for token in ("data.providers", "data.models", "data.roles",
                      "payload.limits", "payload.circuit", "payload.budget"):
            self.assertIn(token, self.llm)

    def test_missing_endpoint_degrades(self) -> None:
        self.assertIn("llm.usage.unavailable", self.llm)
        self.assertIn("llm.models.unavailable", self.llm)
        self.assertIn("isUnsupported", self.llm)
        self.assertIn("emptyState", self.llm)

    def test_routes_editor_saves_through_config_patch(self) -> None:
        self.assertIn("export function renderRoutesEditor(", self.llm)
        self.assertIn("cfgPath: 'llm.routes'", self.llm)
        self.assertIn("cfgPath: 'llm.fallback'", self.llm)
        self.assertIn("cfgKind: 'json'", self.llm)
        self.assertIn("renderRoutesEditor()", self.apps)
        # collectConfigPatch gained a json branch that validates before PATCH
        self.assertIn("kind === 'json'", self.apps)
        self.assertIn("settings.ai.routes.invalid", self.apps)
        self.assertIn("JSON.parse(source)", self.apps)

    def test_usage_panel_is_mounted_in_ai_settings(self) -> None:
        self.assertIn("renderLlmUsagePanel()", self.apps)
        self.assertIn("settings.tab.ai", self.apps)


class CapabilitiesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cap = read(CAP)
        cls.api = read(API)
        cls.apps = read(APPS)

    def test_three_sections_call_their_apis(self) -> None:
        for call in ("getSkills", "setSkillEnabled", "getPlugins",
                     "loadPlugin", "unloadPlugin", "getMcpServers",
                     "connectMcp", "disconnectMcp", "getMcpTools", "callMcp"):
            self.assertIn(f"{call}:", self.api, f"api.{call} missing")
        for key in ("cap.skills.title", "cap.plugins.title", "cap.mcp.title"):
            self.assertIn(key, self.cap)
        self.assertIn("renderCapabilitiesPane()", self.apps)
        self.assertIn("settings.tab.capabilities", self.apps)

    def test_missing_endpoints_show_not_enabled(self) -> None:
        for key in ("cap.skills.unavailable", "cap.plugins.unavailable",
                    "cap.mcp.unavailable", "cap.mcp.tools.unavailable"):
            self.assertIn(key, self.cap)
        self.assertIn("isUnsupported", self.cap)
        # each section has its own retry path instead of a global failure
        self.assertIn("loadSkills(", self.cap)
        self.assertIn("loadPlugins(", self.cap)
        self.assertIn("loadMcp(", self.cap)

    def test_skill_state_fields_and_project_scope(self) -> None:
        self.assertIn("enabled_global", self.cap)
        self.assertIn("enabled_projects", self.cap)
        self.assertIn("setSkillEnabled(id, nextEnabled, scopeSelect.value)", self.cap)
        self.assertIn("cap.skills.invalid", self.cap)
        self.assertIn("skill.error", self.cap)

    def test_mcp_call_test_uses_json_arguments(self) -> None:
        self.assertIn("JSON.parse(args.value", self.cap)
        self.assertIn("cap.mcp.args.invalid", self.cap)
        self.assertIn("api.callMcp(", self.cap)
        self.assertIn("cap.mcp.call.need_approval", self.cap)


class ProjectGitDepsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.extras = read(EXTRAS)
        cls.apps = read(APPS)
        cls.api = read(API)

    def test_task_dependencies_use_real_model_fields(self) -> None:
        self.assertIn("task.deps", self.extras)
        self.assertIn("task.depends_on", self.extras)
        self.assertIn("task.blocked_by", self.extras)
        self.assertIn("export function taskDeps(", self.extras)
        self.assertIn("export function renderTaskDependencySection(", self.extras)
        self.assertIn("renderTaskDependencySection(view.projectTasks", self.apps)

    def test_task_graph_is_local_svg(self) -> None:
        self.assertIn("export function buildTaskGraph(", self.extras)
        self.assertIn("createElementNS", self.extras)
        self.assertIn("task-dep-graph", self.extras)
        self.assertNotIn("innerHTML", self.extras)

    def test_task_cards_show_dependency_badges(self) -> None:
        self.assertIn("depBadgeText", self.apps)
        self.assertIn("isDependencyBlocked", self.apps)
        self.assertIn("tc-deps", self.apps)
        self.assertIn("is-dep-blocked", self.apps)

    def test_git_panel_reads_and_rolls_back(self) -> None:
        self.assertIn("getProjectGit:", self.api)
        self.assertIn("rollbackProjectGit:", self.api)
        self.assertIn("/git/rollback", self.api)
        self.assertIn("export function mountGitSection(", self.extras)
        self.assertIn("git.unavailable", self.extras)
        self.assertIn("git.rollback", self.extras)
        self.assertIn("mountGitSection(project.id", self.apps)
        self.assertIn("git.rollback.confirm", self.extras)
        self.assertIn("invalidateGit", self.extras)


class StreamingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = read(STORE)
        cls.chat = read(CHAT)
        cls.agent = read(AGENTCHAT)

    def test_store_folds_chat_delta_and_tracks_message_id(self) -> None:
        self.assertIn("type === 'chat.delta'", self.store)
        self.assertIn("state.stream.text += String(data.delta)", self.store)
        self.assertIn("stream.messageId", self.store)
        self.assertIn("emit('chat', { kind: 'stream'", self.store)

    def test_agent_chat_delta_has_its_own_reducer(self) -> None:
        self.assertIn("agent.chat.delta", self.agent)
        self.assertIn("draft.text += String(data.delta)", self.agent)
        self.assertIn("agent.chat.message", self.agent)

    def test_final_message_replaces_the_draft(self) -> None:
        self.assertIn("case 'final'", self.chat.replace("detail.kind === 'final'", "case 'final'"))
        self.assertIn("clearStream()", self.chat)
        self.assertIn("appendMessage(detail.message", self.chat)
        # the dedupe path uses rendered ids, so a replayed final cannot double
        self.assertIn("rendered.has(message.id)", self.chat)
        self.assertIn("rendered.set(message.id", self.chat)

    def test_reconnect_drops_stale_drafts_and_syncs_messages(self) -> None:
        self.assertIn("stream-reset", self.store)
        self.assertIn("resetStream()", self.store)
        self.assertIn("syncMessages()", self.chat)
        self.assertIn("resetStream", self.chat)
        self.assertIn("scheduleRefresh(60)", self.store)

    def test_rich_text_helpers_are_still_used(self) -> None:
        self.assertIn("richText(", self.chat)
        ui = read(JS / "ui.js")
        self.assertIn("export function richText(", ui)


class NoBuildStepTest(unittest.TestCase):
    def test_no_npm_or_bundler_references(self) -> None:
        assets = ASSETS.rglob("*")
        forbidden = ("npm install", "npm run", "node_modules", "package.json",
                     "webpack", "vite", "rollup", "esbuild", "browserify")
        for path in assets:
            if not path.is_file() or path.suffix not in (".js", ".css", ".html", ".md"):
                continue
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text,
                                 f"{path.name} references the build toolchain: {token}")

    def test_no_markup_injection_in_new_modules(self) -> None:
        for path in (SESSION, LLM, CAP, EXTRAS, JS / "units.js", JS / "i18n.js"):
            text = read(path)
            for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                              "document.write"):
                self.assertNotIn(forbidden, text,
                                 f"{path.name} uses {forbidden}")


if __name__ == "__main__":
    unittest.main()
