"""Guardian tests: envelope fences, modes, races, denials and quotas.

Every test uses a fake app (approval broker + project/policy/supervisor), so no
hub or worker process is started.  The expected behaviour is the hard contract
from ``docs/team/autonomy-plan.md`` §3.3 (see ``docs/team/guard.md``).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from stackflowos.autonomy.guardian import Guardian
from stackflowos.errors import Conflict, Denied, NotFound

NOW = 1_700_000_000_000


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
def make_config(**overrides):
    base = dict(
        approve_mode="enforce",
        approve_risk_max="medium",
        approve_capabilities=["fs.write", "agent.spawn"],
        approve_projects=[],
        approve_roles=["architect", "coder", "tester", "reviewer",
                       "researcher"],
        approve_max_per_hour=20,
        approve_min_age_s=0.0,
        approve_deny_cooldown_s=86400.0,
        approve_max_agents_per_project=4,
        approve_shell=False,
        approve_shell_patterns=[],
        approve_domains=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeProjects:
    def __init__(self):
        self.items: dict[str, SimpleNamespace] = {}

    def add(self, project):
        self.items[project.id] = project
        return project

    def get(self, project_id):
        try:
            return self.items[project_id]
        except KeyError:
            raise NotFound(f"project {project_id!r} not found") from None


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


class FakeApprovals:
    def __init__(self, requests=None):
        self.requests = [dict(row) for row in requests or []]
        self.list_calls: list[tuple] = []

    def list(self, *, status=None, limit=200):
        self.list_calls.append((status, limit))
        rows = [row for row in self.requests
                if status is None or row.get("status") == status]
        return [dict(row) for row in rows[:max(0, int(limit))]]


class FakeApp:
    def __init__(self, projects, approvals=None, supervisor=None):
        self.projects = projects
        self.policy = FakePolicy(projects)
        self.approvals = approvals or FakeApprovals()
        self.supervisor = supervisor or FakeSupervisor()
        self.decide_calls: list[dict] = []
        self.decide_error: Exception | None = None

    async def decide_approval(self, request_id, *, decision, mode="once",
                              note="", by="human:operator"):
        self.decide_calls.append({
            "request_id": request_id, "decision": decision, "mode": mode,
            "note": note, "by": by,
        })
        if self.decide_error is not None:
            raise self.decide_error
        for row in self.approvals.requests:
            if row.get("id") == request_id:
                row["status"] = "approved"
        return {"id": request_id, "status": "approved", "decision": decision}


def make_project(root, project_id="p1", status="active",
                 workspace_mode="managed", workspace=True):
    path = Path(root) / project_id / "workspace"
    if workspace:
        path.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(id=project_id, status=status,
                           workspace_mode=workspace_mode, workspace=path)


def make_request(request_id="apr-1", capability="fs.write",
                 principal="agent-coder-1", scope=None, risk="medium",
                 project_id="p1", status="pending", created_at=None,
                 decided_by=None, decided_at=None):
    row = {
        "id": request_id,
        "principal": principal,
        "capability": capability,
        "scope": dict(scope or {}),
        "risk": risk,
        "reason": "test request",
        "project_id": project_id,
        "status": status,
        "created_at": NOW - 60_000 if created_at is None else created_at,
        "expires_at": NOW + 300_000,
    }
    if decided_by is not None:
        row["decided_by"] = decided_by
    if decided_at is not None:
        row["decided_at"] = decided_at
    return row


class GuardianTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = FakeProjects()
        self.project = self.projects.add(make_project(self.root))
        self.workspace = Path(self.project.workspace)
        self.approvals = FakeApprovals()
        self.app = FakeApp(self.projects, self.approvals)
        self.clock = [NOW]
        self.state: dict = {}

    def now(self):
        return self.clock[0]

    def guardian(self, config=None, app=None, state=None):
        return Guardian(app or self.app, config or make_config(),
                        state if state is not None else self.state,
                        now=self.now)

    def pending(self, request):
        self.approvals.requests.append(dict(request))
        return request

    # -- fs.write ---------------------------------------------------------
    async def test_enforce_allows_fs_write_inside_workspace(self):
        target = self.workspace / "src" / "module.py"
        request = self.pending(make_request(scope={"path": str(target)}))
        guardian = self.guardian()

        verdict = guardian.evaluate(dict(request))
        self.assertEqual(verdict.decision, "allow")

        records = await guardian.apply()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["request_id"], "apr-1")
        self.assertEqual(record["decision"], "allow")
        self.assertEqual(record["capability"], "fs.write")
        self.assertEqual(record["project_id"], "p1")
        self.assertTrue(record["reason"])

        self.assertEqual(len(self.app.decide_calls), 1)
        call = self.app.decide_calls[0]
        self.assertEqual(call["request_id"], "apr-1")
        self.assertEqual(call["decision"], "allow")
        self.assertEqual(call["mode"], "once")
        self.assertEqual(call["by"], "autonomy:guardian")
        self.assertTrue(call["note"])

        self.assertEqual(len(self.state["rate"]["approvals"]), 1)
        self.assertEqual(len(self.state["approvals"]), 1)
        self.assertEqual(self.state["approvals"][0]["decision"], "allow")

    async def test_fs_write_outside_workspace_skipped(self):
        request = self.pending(make_request(
            scope={"path": str(self.root / "outside.py")}))
        guardian = self.guardian()
        self.assertEqual(guardian.evaluate(dict(request)).decision, "skip")
        records = await guardian.apply()
        self.assertEqual(records[0]["decision"], "skip")
        self.assertIn("outside", records[0]["reason"])
        self.assertEqual(self.app.decide_calls, [])

    async def test_fs_write_git_path_skipped(self):
        request = self.pending(make_request(
            scope={"path": str(self.workspace / ".git" / "config")}))
        records = await self.guardian().apply()
        self.assertEqual(records[0]["decision"], "skip")
        self.assertIn(".git", records[0]["reason"])
        self.assertEqual(self.app.decide_calls, [])

    async def test_fs_write_relative_path_uses_cwd(self):
        self.pending(make_request(
            scope={"path": "pkg/app.py", "cwd": str(self.workspace)}))
        records = await self.guardian().apply()
        self.assertEqual(records[0]["decision"], "allow")

    async def test_fs_write_attached_ro_skipped(self):
        self.projects.items["p1"] = make_project(
            self.root, workspace_mode="attached-ro")
        self.pending(make_request(
            scope={"path": str(self.workspace / "a.py")}))
        records = await self.guardian().apply()
        self.assertEqual(records[0]["decision"], "skip")
        self.assertIn("read-only", records[0]["reason"])
        self.assertEqual(self.app.decide_calls, [])

    async def test_fs_write_without_path_skipped(self):
        self.pending(make_request(scope={}))
        records = await self.guardian().apply()
        self.assertEqual(records[0]["decision"], "skip")
        self.assertIn("no path", records[0]["reason"])

    # -- agent.spawn ------------------------------------------------------
    async def test_agent_spawn_allowed_within_role_and_count(self):
        self.app.supervisor.agents = [
            {"id": "agent-coder-1", "status": "working", "project_id": "p1"},
        ]
        self.pending(make_request(
            capability="agent.spawn",
            scope={"project_id": "p1", "role": "coder"}))
        records = await self.guardian().apply()
        self.assertEqual(records[0]["decision"], "allow")
        self.assertEqual(len(self.app.decide_calls), 1)

    async def test_agent_spawn_role_not_allowed_skipped(self):
        self.pending(make_request(
            capability="agent.spawn",
            scope={"project_id": "p1", "role": "lead"}))
        records = await self.guardian().apply()
        self.assertEqual(records[0]["decision"], "skip")
        self.assertIn("approve_roles", records[0]["reason"])

    async def test_agent_spawn_project_agent_cap(self):
        self.app.supervisor.agents = [
            {"id": f"agent-coder-{index}", "status": "working",
             "project_id": "p1"}
            for index in range(4)
        ]
        self.pending(make_request(
            capability="agent.spawn",
            scope={"project_id": "p1", "role": "coder"}))
        records = await self.guardian().apply()
        self.assertEqual(records[0]["decision"], "skip")
        self.assertIn("active agents", records[0]["reason"])

    # -- shell.exec -------------------------------------------------------
    async def test_shell_exec_requires_opt_in(self):
        config = make_config(
            approve_capabilities=["fs.write", "agent.spawn", "shell.exec"],
            approve_risk_max="high", approve_shell=False,
            approve_shell_patterns=[r"pytest .*"])
        request = make_request(
            capability="shell.exec", risk="high",
            scope={"command": "pytest -q", "cwd": str(self.workspace),
                   "project_id": "p1"})
        verdict = self.guardian(config=config).evaluate(request)
        self.assertEqual(verdict.decision, "skip")
        self.assertIn("disabled", verdict.reason)

    async def test_shell_exec_allowed_when_every_fence_passes(self):
        config = make_config(
            approve_capabilities=["fs.write", "agent.spawn", "shell.exec"],
            approve_risk_max="high", approve_shell=True,
            approve_shell_patterns=[r"pytest .*"])
        self.pending(make_request(
            capability="shell.exec", risk="high",
            scope={"command": "pytest -q tests/", "cwd": str(self.workspace),
                   "project_id": "p1"}))
        records = await self.guardian(config=config).apply()
        self.assertEqual(records[0]["decision"], "allow")
        self.assertEqual(len(self.app.decide_calls), 1)

    async def test_shell_exec_fences(self):
        base = dict(
            approve_capabilities=["fs.write", "agent.spawn", "shell.exec"],
            approve_risk_max="high", approve_shell=True,
            approve_shell_patterns=[r"pytest .*"])
        cases = [
            ("control", "pytest -q; rm -rf /", self.workspace,
             "control operators"),
            ("chained-pipe", "pytest -q | tee out", self.workspace,
             "control operators"),
            ("mismatch", "python setup.py", self.workspace, "does not match"),
            ("outside-cwd", "pytest -q", self.root, "outside the project"),
            ("no-cwd", "pytest -q", None, "no cwd"),
        ]
        for name, command, cwd, part in cases:
            with self.subTest(name=name):
                scope = {"command": command, "project_id": "p1"}
                if cwd is not None:
                    scope["cwd"] = str(cwd)
                request = make_request(
                    capability="shell.exec", risk="high", scope=scope)
                verdict = self.guardian(config=make_config(**base)).evaluate(
                    request)
                self.assertEqual(verdict.decision, "skip")
                self.assertIn(part, verdict.reason)

    async def test_shell_exec_requires_high_risk_ceiling(self):
        config = make_config(
            approve_capabilities=["fs.write", "agent.spawn", "shell.exec"],
            approve_risk_max="medium", approve_shell=True,
            approve_shell_patterns=[r"pytest .*"])
        request = make_request(
            capability="shell.exec", risk="high",
            scope={"command": "pytest -q", "cwd": str(self.workspace),
                   "project_id": "p1"})
        verdict = self.guardian(config=config).evaluate(request)
        self.assertEqual(verdict.decision, "skip")
        self.assertIn("approve_risk_max", verdict.reason)

    # -- net.fetch --------------------------------------------------------
    async def test_net_fetch_domain_allowlist(self):
        config = make_config(
            approve_capabilities=["fs.write", "agent.spawn", "net.fetch"],
            approve_domains=["example.com"])
        allowed = make_request(
            capability="net.fetch", risk="medium",
            scope={"url": "https://api.example.com/v1", "project_id": "p1"})
        self.assertEqual(
            self.guardian(config=config).evaluate(allowed).decision, "allow")
        exact = make_request(
            capability="net.fetch", scope={"url": "https://example.com/x",
                                           "project_id": "p1"})
        self.assertEqual(
            self.guardian(config=config).evaluate(exact).decision, "allow")
        denied = make_request(
            capability="net.fetch",
            scope={"url": "https://evil.test/x", "project_id": "p1"})
        verdict = self.guardian(config=config).evaluate(denied)
        self.assertEqual(verdict.decision, "skip")
        self.assertIn("approve_domains", verdict.reason)

    async def test_net_fetch_without_domains_skipped(self):
        request = make_request(
            capability="net.fetch", scope={"url": "https://example.com/x",
                                           "project_id": "p1"})
        config = make_config(
            approve_capabilities=["fs.write", "agent.spawn", "net.fetch"])
        verdict = self.guardian(config=config).evaluate(request)
        self.assertEqual(verdict.decision, "skip")
        self.assertIn("approve_domains", verdict.reason)

    # -- hard deny / principals / risk / projects -------------------------
    async def test_hard_denied_capability_never_allowed(self):
        config = make_config(approve_capabilities=["fs.delete"],
                             approve_risk_max="high")
        request = make_request(capability="fs.delete", risk="high",
                               scope={"path": str(self.workspace / "a.py")})
        verdict = self.guardian(config=config).evaluate(request)
        self.assertEqual(verdict.decision, "skip")
        self.assertIn("hard-denied", verdict.reason)

    async def test_capability_not_allowlisted_skipped(self):
        request = make_request(capability="data.write",
                               scope={"project_id": "p1"})
        verdict = self.guardian().evaluate(request)
        self.assertEqual(verdict.decision, "skip")
        self.assertIn("approve_capabilities", verdict.reason)

    async def test_non_worker_principal_skipped(self):
        request = make_request(principal="human:alice",
                               scope={"path": str(self.workspace / "a.py")})
        verdict = self.guardian().evaluate(request)
        self.assertEqual(verdict.decision, "skip")
        self.assertIn("not a worker", verdict.reason)

    async def test_non_pending_request_skipped(self):
        request = make_request(status="approved",
                               scope={"path": str(self.workspace / "a.py")})
        verdict = self.guardian().evaluate(request)
        self.assertEqual(verdict.decision, "skip")
        self.assertIn("not pending", verdict.reason)

    async def test_risk_above_ceiling_skipped(self):
        request = make_request(risk="high",
                               scope={"path": str(self.workspace / "a.py")})
        verdict = self.guardian().evaluate(request)
        self.assertEqual(verdict.decision, "skip")
        self.assertIn("risk", verdict.reason)

    async def test_project_guards(self):
        cases = [
            ("archived", "archived"),
            ("done", "done"),
        ]
        for project_id, status in cases:
            with self.subTest(project_id=project_id):
                self.projects.items["p1"] = make_project(
                    self.root, status=status)
                request = make_request(
                    scope={"path": str(self.workspace / "a.py")})
                verdict = self.guardian().evaluate(request)
                self.assertEqual(verdict.decision, "skip")
                self.assertIn(status, verdict.reason)
        # unknown project
        request = make_request(project_id="ghost",
                               scope={"path": str(self.workspace / "a.py")})
        verdict = self.guardian().evaluate(request)
        self.assertEqual(verdict.decision, "skip")
        self.assertIn("does not exist", verdict.reason)

    async def test_project_allowlist(self):
        self.pending(make_request(
            scope={"path": str(self.workspace / "a.py")}))
        config = make_config(approve_projects=["p2"])
        verdict = self.guardian(config=config).evaluate(
            dict(self.approvals.requests[0]))
        self.assertEqual(verdict.decision, "skip")
        self.assertIn("approve_projects", verdict.reason)
        config = make_config(approve_projects=["p1"])
        verdict = self.guardian(config=config).evaluate(
            dict(self.approvals.requests[0]))
        self.assertEqual(verdict.decision, "allow")

    # -- human denial cooldown -------------------------------------------
    async def test_human_denial_cooldown(self):
        self.approvals.requests.append(make_request(
            request_id="apr-old", principal="agent-coder-7",
            capability="fs.write", status="denied",
            decided_by="human:alice", decided_at=NOW - 1000))
        request = make_request(scope={"path": str(self.workspace / "a.py")})
        verdict = self.guardian().evaluate(request)
        self.assertEqual(verdict.decision, "skip")
        self.assertIn("human denied", verdict.reason)

        # after the cool-down the request is eligible again
        self.clock[0] = NOW + 86401 * 1000
        request = make_request(
            created_at=self.clock[0] - 60_000,
            scope={"path": str(self.workspace / "a.py")})
        self.assertEqual(
            self.guardian().evaluate(request).decision, "allow")

    async def test_policy_denial_does_not_trigger_cooldown(self):
        self.approvals.requests.append(make_request(
            request_id="apr-auto", principal="agent-coder-7",
            capability="fs.write", status="denied",
            decided_by="policy", decided_at=NOW - 1000))
        request = make_request(scope={"path": str(self.workspace / "a.py")})
        self.assertEqual(
            self.guardian().evaluate(request).decision, "allow")

    async def test_denial_for_other_capability_does_not_block(self):
        self.approvals.requests.append(make_request(
            request_id="apr-old", principal="agent-coder-7",
            capability="agent.spawn", status="denied",
            decided_by="human:alice", decided_at=NOW - 1000))
        request = make_request(scope={"path": str(self.workspace / "a.py")})
        self.assertEqual(
            self.guardian().evaluate(request).decision, "allow")

    # -- rate / age / modes ----------------------------------------------
    async def test_hourly_rate_limit(self):
        self.pending(make_request(request_id="apr-1",
                                  scope={"path": str(self.workspace / "a")}))
        self.pending(make_request(request_id="apr-2",
                                  scope={"path": str(self.workspace / "b")}))
        config = make_config(approve_max_per_hour=1)
        records = await self.guardian(config=config).apply()
        self.assertEqual([row["decision"] for row in records],
                         ["allow", "skip"])
        self.assertEqual(len(self.app.decide_calls), 1)
        self.assertEqual(len(self.state["rate"]["approvals"]), 1)
        self.assertIn("quota", records[1]["reason"])

    async def test_rate_zero_forbids_approvals(self):
        self.pending(make_request(
            scope={"path": str(self.workspace / "a.py")}))
        records = await self.guardian(
            config=make_config(approve_max_per_hour=0)).apply()
        self.assertEqual(records[0]["decision"], "skip")
        self.assertIn("disabled", records[0]["reason"])
        self.assertEqual(self.app.decide_calls, [])

    async def test_min_age_lets_a_human_see_it_first(self):
        config = make_config(approve_min_age_s=10)
        fresh = make_request(created_at=NOW - 1000,
                             scope={"path": str(self.workspace / "a")})
        verdict = self.guardian(config=config).evaluate(fresh)
        self.assertEqual(verdict.decision, "skip")
        self.assertIn("approve_min_age_s", verdict.reason)
        older = make_request(created_at=NOW - 20_000,
                             scope={"path": str(self.workspace / "b")})
        self.assertEqual(
            self.guardian(config=config).evaluate(older).decision, "allow")

    async def test_observe_mode_records_without_deciding(self):
        config = make_config(approve_mode="observe")
        self.pending(make_request(request_id="apr-1",
                                  scope={"path": str(self.workspace / "a")}))
        self.pending(make_request(request_id="apr-2", principal="human:alice",
                                  scope={"path": str(self.workspace / "b")}))
        records = await self.guardian(config=config).apply()
        self.assertEqual([row["decision"] for row in records],
                         ["observe", "observe"])
        self.assertEqual(self.app.decide_calls, [])
        self.assertEqual(self.state["rate"].get("approvals", []), [])
        self.assertEqual(len(self.state["approvals"]), 2)

    async def test_off_mode_apply_is_empty_but_evaluate_works(self):
        config = make_config(approve_mode="off")
        self.pending(make_request(
            scope={"path": str(self.workspace / "a.py")}))
        guardian = self.guardian(config=config)
        self.assertEqual(await guardian.apply(), [])
        self.assertEqual(self.app.decide_calls, [])
        self.assertEqual(
            guardian.evaluate(dict(self.approvals.requests[0])).decision,
            "allow")

    # -- races / errors ---------------------------------------------------
    async def test_conflict_race_becomes_skip(self):
        self.pending(make_request(
            scope={"path": str(self.workspace / "a.py")}))
        self.app.decide_error = Conflict("approval already approved")
        records = await self.guardian().apply()
        self.assertEqual(records[0]["decision"], "skip")
        self.assertIn("already handled", records[0]["reason"])
        self.assertEqual(self.state["rate"].get("approvals", []), [])

    async def test_other_decide_error_becomes_skip(self):
        self.pending(make_request(
            scope={"path": str(self.workspace / "a.py")}))
        self.app.decide_error = Denied("runtime denied")
        records = await self.guardian().apply()
        self.assertEqual(records[0]["decision"], "skip")
        self.assertIn("decide_approval failed", records[0]["reason"])
        self.assertEqual(self.state["rate"].get("approvals", []), [])

    async def test_unexpected_evaluation_error_is_a_skip(self):
        guardian = self.guardian()
        verdict = guardian.evaluate(None)
        self.assertEqual(verdict.decision, "skip")

    # -- fetch / state bookkeeping ---------------------------------------
    async def test_apply_fetches_pending_50(self):
        self.pending(make_request(
            scope={"path": str(self.workspace / "a.py")}))
        await self.guardian().apply()
        self.assertIn(("pending", 50), self.approvals.list_calls)

    async def test_requests_argument_bypasses_fetch(self):
        self.approvals.requests = []
        records = await self.guardian().apply([
            make_request(scope={"path": str(self.workspace / "a.py")}),
        ])
        self.assertEqual(records[0]["decision"], "allow")
        self.assertNotIn(("pending", 50), self.approvals.list_calls)

    async def test_state_records_are_deduplicated_and_bounded(self):
        requests = [
            make_request(request_id=f"apr-{index}",
                         scope={"path": str(self.workspace / "a.py")})
            for index in range(105)
        ]
        config = make_config(approve_mode="observe")
        guardian = self.guardian(config=config)
        await guardian.apply(requests)
        self.assertEqual(len(self.state["approvals"]), 100)
        # same request + same decision overwrites instead of growing.
        one = make_request(scope={"path": str(self.workspace / "a.py")})
        guardian = self.guardian(config=config,
                                 state={"approvals": [], "rate": {}})
        await guardian.apply([one])
        await guardian.apply([one])
        self.assertEqual(len(guardian.state["approvals"]), 1)


if __name__ == "__main__":
    unittest.main()
