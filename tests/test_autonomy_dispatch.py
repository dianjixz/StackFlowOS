"""Dispatcher tests: modes, dedupe/cooldown, quotas, guards, failure isolation.

The dispatcher is exercised with a fake app: no hub, no worker processes and no
real project store.  Every test asserts the hard contract from
``docs/team/autonomy-plan.md`` §3.3 (see ``docs/team/guard.md``).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from stackflowos.autonomy.dispatcher import MAX_ACTIONS_PER_RUN, Dispatcher
from stackflowos.autonomy.models import DispatchHint, Finding, fingerprint
from stackflowos.errors import NotFound, ValidationError

NOW = 1_700_000_000_000


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
def make_config(**overrides):
    base = dict(
        dispatch_mode="off",
        dispatch_max_agents=2,
        dispatch_max_tasks_per_hour=10,
        dispatch_max_spawns_per_hour=4,
        dispatch_cooldown_s=900.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeProjects:
    """Minimal project store: get + add_task + task_list."""

    def __init__(self):
        self.items: dict[str, SimpleNamespace] = {}
        self.tasks: dict[str, SimpleNamespace] = {}
        self.add_calls: list[dict] = []
        self.fail_adds = 0
        self._seq = 0

    def add_project(self, project):
        self.items[project.id] = project
        return project

    def get(self, project_id):
        try:
            return self.items[project_id]
        except KeyError:
            raise NotFound(f"project {project_id!r} not found") from None

    def add_task(self, project_id, title, description="", *, role=None,
                 priority=0, deps=None, created_by="operator",
                 milestone_id=None):
        if self.fail_adds > 0:
            self.fail_adds -= 1
            raise ValidationError("task title is required")
        self._seq += 1
        task = SimpleNamespace(id=f"t-{self._seq}", project_id=project_id,
                               title=title, description=description,
                               role=role, priority=priority,
                               created_by=created_by)
        self.tasks[task.id] = task
        self.add_calls.append({
            "project_id": project_id, "title": title,
            "description": description, "role": role,
            "priority": priority, "created_by": created_by,
        })
        return task

    def task_list(self, *, project_id=None, status=None, assignee=None):
        return [task for task in self.tasks.values()
                if project_id is None or task.project_id == project_id]


class FakePolicy:
    def __init__(self, projects):
        self.projects = projects

    def workspace_for(self, project_id):
        return self.projects.get(project_id).workspace


class FakeSupervisor:
    def __init__(self, agents=None):
        self.agents = [dict(agent) for agent in agents or []]

    def list(self, *, project_id=None, status=None):
        return [
            dict(agent) for agent in self.agents
            if (project_id is None or agent.get("project_id") == project_id)
            and (status is None or agent.get("status") == status)
        ]


class FakeApp:
    def __init__(self, projects, supervisor=None):
        self.projects = projects
        self.policy = FakePolicy(projects)
        self.supervisor = supervisor or FakeSupervisor()
        self.spawn_calls: list[dict] = []
        self.spawn_error: Exception | None = None

    async def spawn_agent(self, role, project_id=None, task_id=None):
        self.spawn_calls.append({
            "role": role, "project_id": project_id, "task_id": task_id})
        if self.spawn_error is not None:
            raise self.spawn_error
        return {"id": f"agent-{role}-{len(self.spawn_calls)}"}


def make_project(root, project_id="p1", status="active",
                 workspace_mode="managed", workspace=True):
    path = Path(root) / project_id / "workspace"
    if workspace:
        path.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(id=project_id, status=status,
                           workspace_mode=workspace_mode,
                           workspace=path)


def make_finding(project_id="p1", kind="task", fp=None, title="补项目计划",
                 role="coder", task_id=None, description="", priority=1):
    return Finding(
        check="task.idle", severity="warn", title=title,
        project_id=project_id,
        fingerprint=fp or fingerprint("task.idle", project_id),
        dispatch=DispatchHint(kind=kind, role=role, task_id=task_id,
                              title=title, description=description,
                              priority=priority),
    )


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = FakeProjects()
        self.projects.add_project(make_project(self.root))
        self.app = FakeApp(self.projects)
        self.clock = [NOW]

    def now(self):
        return self.clock[0]

    async def apply(self, findings, config, state=None, app=None):
        dispatcher = Dispatcher(
            app or self.app, config, state if state is not None else {},
            now=self.now)
        return await dispatcher.apply(findings)

    # -- modes ------------------------------------------------------------
    async def test_off_mode_returns_empty_and_never_acts(self):
        records = await self.apply([make_finding()],
                                   make_config(dispatch_mode="off"))
        self.assertEqual(records, [])
        self.assertEqual(self.projects.add_calls, [])
        self.assertEqual(self.app.spawn_calls, [])

    async def test_tasks_mode_creates_task_and_never_spawns(self):
        state = {}
        finding = make_finding(kind="task", description="给项目一个可执行计划",
                               role="architect", priority=3)
        records = await self.apply(
            [finding], make_config(dispatch_mode="tasks"), state=state)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["action"], "task")
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["project_id"], "p1")
        self.assertTrue(record["task_id"])
        self.assertEqual(record["role"], "architect")
        self.assertEqual(self.app.spawn_calls, [])
        self.assertEqual(len(self.projects.add_calls), 1)
        call = self.projects.add_calls[0]
        self.assertEqual(call["created_by"], "autonomy:governor")
        self.assertEqual(call["role"], "architect")
        self.assertEqual(call["priority"], 2)  # clamped to <= 2
        self.assertTrue(call["description"].endswith(
            f"[autonomy:{finding.fingerprint}] {finding.title}"))
        # state / rate bookkeeping
        stored = state["dispatch"][finding.fingerprint]
        self.assertEqual(stored["status"], "ok")
        self.assertEqual(stored["task_id"], record["task_id"])
        self.assertEqual(len(state["rate"]["tasks"]), 1)
        self.assertEqual(state["rate"].get("spawns", []), [])

    async def test_tasks_mode_skips_spawn_hints(self):
        state = {}
        records = await self.apply(
            [make_finding(kind="spawn", task_id="t-9")],
            make_config(dispatch_mode="tasks"), state=state)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["action"], "spawn")
        self.assertEqual(records[0]["status"], "skipped")
        self.assertIn("does not spawn", records[0]["reason"])
        self.assertEqual(self.app.spawn_calls, [])
        self.assertEqual(state.get("dispatch"), None)

    async def test_agents_mode_creates_task_then_spawns(self):
        state = {}
        finding = make_finding(kind="task", role="coder")
        records = await self.apply(
            [finding], make_config(dispatch_mode="agents"), state=state)

        self.assertEqual([row["action"] for row in records],
                         ["task", "spawn"])
        self.assertEqual([row["status"] for row in records], ["ok", "ok"])
        self.assertEqual(len(self.projects.add_calls), 1)
        self.assertEqual(len(self.app.spawn_calls), 1)
        spawn = self.app.spawn_calls[0]
        task_id = records[0]["task_id"]
        self.assertEqual(spawn["role"], "coder")
        self.assertEqual(spawn["project_id"], "p1")
        self.assertEqual(spawn["task_id"], task_id)
        self.assertEqual(records[1]["agent_id"], "agent-coder-1")
        stored = state["dispatch"][finding.fingerprint]
        self.assertEqual(stored["status"], "ok")
        self.assertEqual(stored["task_id"], task_id)
        self.assertEqual(stored["agent_id"], "agent-coder-1")
        self.assertEqual(stored["attempts"], 2)

    async def test_agents_mode_spawn_hint_spawns_existing_task(self):
        state = {}
        records = await self.apply(
            [make_finding(kind="spawn", task_id="t-42")],
            make_config(dispatch_mode="agents"), state=state)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "ok")
        self.assertEqual(records[0]["action"], "spawn")
        self.assertEqual(self.projects.add_calls, [])
        self.assertEqual(self.app.spawn_calls[0]["task_id"], "t-42")
        self.assertEqual(self.app.spawn_calls[0]["role"], "coder")

    async def test_spawn_without_role_defaults_to_coder(self):
        records = await self.apply(
            [make_finding(kind="spawn", task_id="t-1", role="")],
            make_config(dispatch_mode="agents"))
        self.assertEqual(records[0]["status"], "ok")
        self.assertEqual(self.app.spawn_calls[0]["role"], "coder")

    # -- dedupe / cooldown ------------------------------------------------
    async def test_dedupe_within_cooldown(self):
        state = {}
        config = make_config(dispatch_mode="tasks")
        finding = make_finding()
        first = await self.apply([finding], config, state=state)
        second = await self.apply([finding], config, state=state)
        self.assertEqual(first[0]["status"], "ok")
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["status"], "skipped")
        self.assertIn("cooldown", second[0]["reason"])
        self.assertEqual(len(self.projects.add_calls), 1)

    async def test_error_record_retries_after_cooldown(self):
        state = {}
        config = make_config(dispatch_mode="agents")
        self.app.spawn_error = ValidationError("spawn rejected")
        finding = make_finding(kind="spawn", task_id="t-1")

        first = await self.apply([finding], config, state=state)
        self.assertEqual(first[0]["status"], "error")
        self.assertIn("spawn rejected", first[0]["reason"])
        stored = state["dispatch"][finding.fingerprint]
        self.assertEqual(stored["status"], "error")
        self.assertEqual(stored["attempts"], 1)
        self.assertIn("spawn rejected", stored["error"])

        self.clock[0] += 901_000
        second = await self.apply([finding], config, state=state)
        self.assertEqual(second[0]["status"], "error")
        self.assertEqual(state["dispatch"][finding.fingerprint]["attempts"], 2)
        self.assertEqual(len(self.app.spawn_calls), 2)

    async def test_failed_spawn_keeps_task_for_retry(self):
        state = {}
        config = make_config(dispatch_mode="agents")
        self.app.spawn_error = ValidationError("model unavailable")
        finding = make_finding(kind="task")

        first = await self.apply([finding], config, state=state)
        self.assertEqual([row["status"] for row in first], ["ok", "error"])
        self.assertEqual(len(self.projects.add_calls), 1)
        task_id = first[0]["task_id"]
        stored = state["dispatch"][finding.fingerprint]
        self.assertEqual(stored["status"], "error")
        self.assertEqual(stored["task_id"], task_id)

        second = await self.apply([finding], config, state=state)
        self.assertEqual([row["status"] for row in second], ["error"])
        self.assertEqual(len(self.projects.add_calls), 1)  # task reused
        self.assertEqual(len(self.app.spawn_calls), 2)
        self.assertEqual(self.app.spawn_calls[1]["task_id"], task_id)

    async def test_attempt_budget_per_finding(self):
        state = {}
        config = make_config(dispatch_mode="agents", dispatch_cooldown_s=0,
                             dispatch_max_spawns_per_hour=100)
        finding = make_finding(kind="spawn", task_id="t-1")
        statuses = []
        last_records = []
        for _ in range(6):
            last_records = await self.apply([finding], config, state=state)
            statuses.append(last_records[0]["status"])
        self.assertEqual(statuses[:5], ["ok"] * 5)
        self.assertEqual(statuses[5], "skipped")
        self.assertIn("retry budget exhausted", last_records[0]["reason"])
        self.assertEqual(len(self.app.spawn_calls), 5)
        stored = state["dispatch"][finding.fingerprint]
        self.assertEqual(stored["attempts"], 5)
        self.assertEqual(stored["status"], "ok")

    async def test_attempt_budget_counts_errors(self):
        state = {}
        config = make_config(dispatch_mode="agents", dispatch_cooldown_s=0,
                             dispatch_max_spawns_per_hour=100)
        self.app.spawn_error = ValidationError("always fails")
        finding = make_finding(kind="spawn", task_id="t-1")
        for _ in range(5):
            await self.apply([finding], config, state=state)
        records = await self.apply([finding], config, state=state)
        self.assertEqual(records[0]["status"], "skipped")
        self.assertIn("retry budget exhausted", records[0]["reason"])
        self.assertEqual(len(self.app.spawn_calls), 5)

    async def test_old_state_without_attempts_is_compatible(self):
        state = {"dispatch": {fingerprint("task.idle", "p1"): {
            "status": "error", "ts": NOW - 10_000, "task_id": "t-1",
        }}}
        finding = make_finding(kind="spawn", task_id="t-1")
        records = await self.apply(
            [finding], make_config(dispatch_mode="agents"), state=state)
        self.assertEqual(records[0]["status"], "ok")
        self.assertEqual(
            state["dispatch"][finding.fingerprint]["attempts"], 1)

    async def test_hourly_rate_window_prunes_old_stamps(self):
        state = {"rate": {"tasks": [NOW - 2 * 3600 * 1000],
                          "spawns": []}}
        records = await self.apply(
            [make_finding()],
            make_config(dispatch_mode="tasks", dispatch_max_tasks_per_hour=1),
            state=state)
        self.assertEqual(records[0]["status"], "ok")
        self.assertEqual(len(state["rate"]["tasks"]), 1)

    # -- quotas -----------------------------------------------------------
    async def test_task_quota_zero_forbids_tasks(self):
        records = await self.apply(
            [make_finding()],
            make_config(dispatch_mode="tasks",
                        dispatch_max_tasks_per_hour=0))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "skipped")
        self.assertIn("task quota", records[0]["reason"])
        self.assertEqual(self.projects.add_calls, [])

    async def test_task_quota_blocks_over_limit(self):
        state = {"rate": {"tasks": [NOW - 1000], "spawns": []}}
        records = await self.apply(
            [make_finding()],
            make_config(dispatch_mode="tasks",
                        dispatch_max_tasks_per_hour=1),
            state=state)
        self.assertEqual(records[0]["status"], "skipped")
        self.assertIn("task quota", records[0]["reason"])
        self.assertEqual(self.projects.add_calls, [])

    async def test_spawn_quota_zero_forbids_spawns(self):
        records = await self.apply(
            [make_finding(kind="spawn", task_id="t-1")],
            make_config(dispatch_mode="agents",
                        dispatch_max_spawns_per_hour=0))
        self.assertEqual(records[0]["status"], "skipped")
        self.assertIn("spawn quota", records[0]["reason"])
        self.assertEqual(self.app.spawn_calls, [])

    async def test_governor_agent_cap_blocks_spawn(self):
        self.app.supervisor.agents = [
            {"id": "agent-coder-1", "status": "working", "project_id": "p1"},
            {"id": "agent-coder-2", "status": "queued", "project_id": "p1"},
        ]
        state = {"dispatch": {
            "old-1": {"status": "ok", "agent_id": "agent-coder-1",
                      "ts": NOW - 1000},
            "old-2": {"status": "ok", "agent_id": "agent-coder-2",
                      "ts": NOW - 1000},
        }}
        records = await self.apply(
            [make_finding(kind="spawn", task_id="t-1")],
            make_config(dispatch_mode="agents", dispatch_max_agents=2),
            state=state)
        self.assertEqual(records[0]["status"], "skipped")
        self.assertIn("agent cap", records[0]["reason"])
        self.assertEqual(self.app.spawn_calls, [])

    async def test_existing_agent_on_task_blocks_spawn(self):
        self.app.supervisor.agents = [
            {"id": "agent-coder-1", "status": "working", "project_id": "p1",
             "task_id": "t-7"},
        ]
        records = await self.apply(
            [make_finding(kind="spawn", task_id="t-7")],
            make_config(dispatch_mode="agents"))
        self.assertEqual(records[0]["status"], "skipped")
        self.assertIn("active agent", records[0]["reason"])
        self.assertEqual(self.app.spawn_calls, [])

    # -- project guards ---------------------------------------------------
    async def test_project_guards_skip(self):
        cases = [
            ("archived", "archived", "managed", True, "is archived"),
            ("done", "done", "managed", True, "is done"),
            ("planning", "planning", "managed", True, "not dispatchable"),
            ("readonly", "active", "attached-ro", True, "read-only"),
            ("noworkspace", "active", "managed", False, "workspace"),
        ]
        for project_id, status, mode, has_ws, reason_part in cases:
            with self.subTest(project_id=project_id):
                self.projects.add_project(make_project(
                    self.root, project_id=project_id, status=status,
                    workspace_mode=mode, workspace=has_ws))
                records = await self.apply(
                    [make_finding(project_id=project_id,
                                  fp=fingerprint("guard", project_id))],
                    make_config(dispatch_mode="tasks"))
                self.assertEqual(records[0]["status"], "skipped")
                self.assertIn(reason_part, records[0]["reason"])
        self.assertEqual(self.projects.add_calls, [])

    async def test_missing_project_is_skipped(self):
        records = await self.apply(
            [make_finding(project_id="ghost", fp="ghost-fp")],
            make_config(dispatch_mode="tasks"))
        self.assertEqual(records[0]["status"], "skipped")
        self.assertIn("does not exist", records[0]["reason"])

    # -- isolation / robustness ------------------------------------------
    async def test_task_creation_failure_is_isolated(self):
        self.projects.fail_adds = 1
        good = make_finding(fp="fp-good", title="第二个")
        bad = make_finding(fp="fp-bad", title="第一个")
        records = await self.apply(
            [bad, good], make_config(dispatch_mode="tasks"))
        self.assertEqual([row["status"] for row in records], ["error", "ok"])
        self.assertIn("task title is required", records[0]["reason"])

    async def test_spawn_exception_is_isolated(self):
        self.app.spawn_error = RuntimeError("worker launch exploded")
        records = await self.apply(
            [make_finding(kind="spawn", task_id="t-1")],
            make_config(dispatch_mode="agents"))
        self.assertEqual(records[0]["status"], "error")
        self.assertIn("worker launch exploded", records[0]["reason"])
        self.assertLessEqual(len(records[0]["reason"]), 300)

    async def test_non_dispatchable_findings_are_ignored(self):
        plain = Finding(check="hub.degraded", severity="critical",
                        title="hub 不健康", project_id="p1")
        records = await self.apply([plain],
                                   make_config(dispatch_mode="agents"))
        self.assertEqual(records, [])

    async def test_missing_config_fields_are_tolerated(self):
        records = await self.apply([make_finding()], SimpleNamespace())
        self.assertEqual(records, [])

    async def test_max_actions_per_run(self):
        findings = [
            make_finding(kind="spawn", task_id=f"t-{index}",
                         fp=f"fp-{index}")
            for index in range(7)
        ]
        config = make_config(dispatch_mode="agents",
                             dispatch_max_spawns_per_hour=100,
                             dispatch_max_agents=100)
        records = await self.apply(findings, config)
        self.assertEqual(MAX_ACTIONS_PER_RUN, 5)
        self.assertEqual(len(records), 5)
        self.assertEqual(len(self.app.spawn_calls), 5)
        self.assertTrue(all(row["status"] == "ok" for row in records))

    async def test_missing_supervisor_is_tolerated(self):
        app = FakeApp(self.projects)
        app.supervisor = None
        records = await self.apply(
            [make_finding(kind="spawn", task_id="t-1")],
            make_config(dispatch_mode="agents"), app=app)
        self.assertEqual(records[0]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
