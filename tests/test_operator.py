"""The chat assistant is an operator proxy: direct tools, full audit."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.errors import NotFound

REPO = default_root()


class ScriptedProvider:
    name = "scripted"
    model = "scripted"

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def chat(self, messages, *, tools=None) -> str:
        return self.reply

    async def ping(self) -> dict:
        return {"ok": True, "provider": self.name, "model": self.model}


class OperatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = App(Config.load(root=self.tmp, state_dir=self.tmp))
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    def _pending(self, project_id=None):
        return self.app.approvals.request(
            "agent-coder-1", "fs.write", {"path": "/etc/outside"},
            reason="operator test", preview="outside",
            project_id=project_id, ttl_s=300)

    def _external_dir(self) -> Path:
        holder = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.addCleanup(holder.cleanup)
        external = Path(holder.name)
        (external / "README.md").write_text("# external", "utf-8")
        return external

    async def test_approval_deny_and_allow_aliases(self):
        deny_me = self._pending()
        result = await self.app.operator_tool(
            "approval.deny", {"request_id": deny_me.id, "confirm": deny_me.id,
                              "note": "no"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.app.approvals.get(deny_me.id).status, "denied")

        allow_me = self._pending()
        result = await self.app.operator_tool(
            "approval.allow", {"id": allow_me.id, "confirm": allow_me.id,
                               "mode": "session"})
        self.assertTrue(result["ok"], result)
        approved = self.app.approvals.get(allow_me.id)
        self.assertEqual(approved.status, "approved")
        self.assertEqual(approved.mode, "session")

    async def test_project_attach_requires_confirmation(self):
        external = self._external_dir()
        missing = await self.app.operator_tool(
            "project.attach", {"path": str(external)})
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["code"], "BAD_REQUEST")
        self.assertIn("confirm", missing["error"])
        self.assertEqual(self.app.projects.list(), [])

        wrong = await self.app.operator_tool(
            "project.attach", {"path": str(external),
                               "confirm": str(self.tmp / "elsewhere")})
        self.assertFalse(wrong["ok"])
        self.assertEqual(wrong["code"], "BAD_REQUEST")
        self.assertEqual(self.app.projects.list(), [])

        confirmed = await self.app.operator_tool(
            "project.attach", {"path": str(external), "confirm": str(external)})
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertTrue(confirmed["data"]["external"])

    async def test_project_access_write_requires_confirmation(self):
        external = self._external_dir()
        card = await self.app.attach_project("确认访问", str(external))

        missing = await self.app.operator_tool(
            "project.access", {"project_id": card["id"], "allow_writes": True})
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["code"], "BAD_REQUEST")
        self.assertIn("confirm", missing["error"])
        self.assertEqual(self.app.projects.get(card["id"]).workspace_mode,
                         "attached-ro")

        confirmed = await self.app.operator_tool(
            "project.access", {"project_id": card["id"], "allow_writes": True,
                               "confirm": True})
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(self.app.projects.get(card["id"]).workspace_mode,
                         "attached-rw")

        # Read-only is the safe direction and needs no second confirmation.
        read_only = await self.app.operator_tool(
            "project.access", {"project_id": card["id"], "allow_writes": False})
        self.assertTrue(read_only["ok"], read_only)
        self.assertEqual(self.app.projects.get(card["id"]).workspace_mode,
                         "attached-ro")

    async def test_approval_decide_requires_confirmation(self):
        pending = self._pending()
        missing = await self.app.operator_tool(
            "approval.decide", {"request_id": pending.id, "decision": "allow"})
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["code"], "BAD_REQUEST")
        self.assertIn("confirm", missing["error"])
        self.assertEqual(self.app.approvals.get(pending.id).status, "pending")

        confirmed = await self.app.operator_tool(
            "approval.decide", {"request_id": pending.id, "decision": "allow",
                                "confirm": pending.id})
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(self.app.approvals.get(pending.id).status, "approved")

    async def test_operator_audit_records_subject_without_secrets(self):
        result = await self.app.operator_tool(
            "sys.status", {"token": "super-secret-token"}, subject="chat")
        self.assertTrue(result["ok"], result)
        actions = [record for record in self.app.eventlog.tail(30)
                   if record.type == "operator.action"]
        self.assertTrue(actions)
        payload = actions[-1].data
        self.assertEqual(payload["subject"], "chat")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("super-secret-token", serialized)
        self.assertIn("***", serialized)

    async def test_project_delete_is_direct_and_audited(self):
        card = await self.app.create_project("操作员删除", "operator delete",
                                             spawn_lead=False)
        workspace = Path(card["workspace"])
        (workspace / "x.txt").write_text("x", "utf-8")
        result = await self.app.operator_tool(
            "project.delete", {"project_id": card["id"],
                               "confirm": card["name"]})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["data"]["workspace_purged"])
        self.assertFalse(workspace.parent.exists())
        types = [record.type for record in self.app.eventlog.tail(50)]
        self.assertIn("operator.action", types)
        self.assertIn("project.deleted", types)

    async def test_project_delete_requires_explicit_confirmation(self):
        card = await self.app.create_project("操作员删除确认", "confirm required",
                                             spawn_lead=False)

        missing = await self.app.operator_tool(
            "project.delete", {"project_id": card["id"]})
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["code"], "BAD_REQUEST")
        self.assertIn("confirm", missing["error"])

        wrong = await self.app.operator_tool(
            "project.delete", {"project_id": card["id"],
                               "confirm": "别的项目名"})
        self.assertFalse(wrong["ok"])
        self.assertEqual(wrong["code"], "BAD_REQUEST")
        self.assertEqual(self.app.projects.get(card["id"]).id, card["id"])

        confirmed = await self.app.operator_tool(
            "project.delete", {"project_id": card["id"],
                               "confirm": card["name"]})
        self.assertTrue(confirmed["ok"], confirmed)
        with self.assertRaises(NotFound):
            self.app.projects.get(card["id"])

    async def test_rule_and_config_management(self):
        rule = await self.app.operator_tool("rule.create", {
            "effect": "allow", "capability": "fs.read", "principal": "*",
            "scope": {}, "note": "assistant rule",
        })
        self.assertTrue(rule["ok"], rule)
        rule_id = rule["data"]["id"]
        listed = await self.app.operator_tool("rule.list", {})
        self.assertTrue(any(r["id"] == rule_id for r in listed["data"]["rules"]))
        deleted = await self.app.operator_tool("rule.delete", {"rule_id": rule_id})
        self.assertTrue(deleted["data"]["deleted"])

        configured = await self.app.operator_tool("config.set", {
            "patch": {"security": {"approval_ttl_s": 120}},
        })
        self.assertTrue(configured["ok"], configured)
        self.assertEqual(self.app.config.security.approval_ttl_s, 120)

    async def test_project_files_and_memory_tools(self):
        card = await self.app.create_project("文件读取", "read artifacts",
                                             spawn_lead=False)
        workspace = Path(card["workspace"])
        (workspace / "notes").mkdir()
        (workspace / "notes" / "a.md").write_text("# hello", "utf-8")

        listing = await self.app.operator_tool(
            "project.files", {"project_id": card["id"], "path": "notes"})
        self.assertTrue(listing["ok"], listing)
        self.assertEqual(listing["data"]["entries"][0]["name"], "a.md")

        content = await self.app.operator_tool(
            "project.read", {"project_id": card["id"], "path": "notes/a.md"})
        self.assertTrue(content["ok"], content)
        self.assertIn("# hello", content["data"]["content"])

        escaped = await self.app.operator_tool(
            "project.read", {"project_id": card["id"], "path": "../../etc/passwd"})
        self.assertFalse(escaped["ok"])

        stored = await self.app.operator_tool("memory.set", {
            "project_id": card["id"], "key": "k", "value": {"v": 1}})
        self.assertTrue(stored["ok"], stored)
        recalled = await self.app.operator_tool(
            "memory.get", {"project_id": card["id"], "key": "k"})
        self.assertEqual(recalled["data"]["value"], {"v": 1})

    async def test_unknown_tool_is_reported_not_fatal(self):
        result = await self.app.operator_tool("project.explode", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "UNKNOWN_TOOL")
        self.assertIn("project.delete", result["supported"])

    async def test_chat_approval_action_intent(self):
        pending = self._pending()
        await self.app.chat.send_user(f"拒绝审批 {pending.id}")
        reply = await self._wait_reply()
        self.assertIn("已拒绝", reply)
        self.assertEqual(self.app.approvals.get(pending.id).status, "denied")

    async def test_chat_llm_can_call_operator_tool(self):
        pending = self._pending()
        self.app.chat.provider = ScriptedProvider(
            '{"tool":"approval.deny","args":{"request_id":"%s","confirm":"%s"}}'
            % (pending.id, pending.id))
        await self.app.chat.send_user("执行一下")
        reply = await self._wait_reply()
        self.assertIn("已处理", reply)
        self.assertEqual(self.app.approvals.get(pending.id).status, "denied")

    async def test_chat_llm_missing_confirm_is_refused(self):
        pending = self._pending()
        self.app.chat.provider = ScriptedProvider(
            '{"tool":"approval.deny","args":{"request_id":"%s"}}' % pending.id)
        await self.app.chat.send_user("直接拒绝")
        reply = await self._wait_reply()
        self.assertIn("confirm", reply)
        self.assertEqual(self.app.approvals.get(pending.id).status, "pending")

    async def _wait_reply(self, timeout: float = 15.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            history = self.app.chat.history(10)
            if history and history[-1]["role"] == "assistant":
                return history[-1]["text"]
            await asyncio.sleep(0.2)
        raise AssertionError("assistant did not reply")

    async def test_operator_does_not_create_approval_requests(self):
        pending_before = self.app.approvals.pending_count()
        card = await self.app.create_project("直接执行", "no approval",
                                             spawn_lead=False)
        await self.app.operator_tool("project.archive", {"project_id": card["id"]})
        self.assertEqual(self.app.approvals.pending_count(), pending_before)


if __name__ == "__main__":
    unittest.main()
