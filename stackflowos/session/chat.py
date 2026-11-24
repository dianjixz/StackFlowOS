"""The conversation surface of StackFlowOS.

The chat is the primary interface ("首先有一个对话框") but it is not a separate
product: every message is an event in the same append-only log, every action
the assistant takes goes through the same permission broker as a worker, and
the reply stream is SSE-friendly.

Two modes coexist:

* ``mock`` — deterministic intent handling. Useful offline, in tests, and for
  the demo. It still performs real operations (creating a project, spawning a
  lead agent) rather than pretending.
* ``openai`` — an OpenAI-compatible model answers with the live system state
  in its prompt. Tools are only executed when the reply is a well-formed JSON
  action; unknown actions are refused.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import SfoError
from ..kernel import Context
from ..util.jsonio import read_json, write_json
from ..util.logging import get_logger
from ..ai.llm import attach_runtime, complete
from .operator import catalog_text

MAX_HISTORY = 200


def _now_ms() -> int:
    return int(time.time() * 1000)


def _mid() -> str:
    import uuid
    return "msg-" + uuid.uuid4().hex[:10]


@dataclass
class ChatMessage:
    id: str
    role: str          # user | assistant | system
    text: str
    ts: int
    agent_id: str | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"id": self.id, "role": self.role, "text": self.text, "ts": self.ts}
        if self.agent_id:
            out["agent_id"] = self.agent_id
        if self.meta:
            out["meta"] = self.meta
        return out


class ChatService:
    def __init__(self, ctx: Context, eventlog, *, app, provider,
                 state_path: Path, logger=None) -> None:
        self.ctx = ctx
        self.eventlog = eventlog
        self.app = app
        self.provider = provider
        self.state_path = Path(state_path)
        self.log = logger or get_logger("chat")
        self._messages: list[ChatMessage] = []
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        # Share usage/cost/context accounting with every provider instance;
        # app.update_config swaps providers but keeps the same runtime.
        try:
            attach_runtime(provider, app)
        except Exception:  # telemetry must never block chat startup
            self.log.debug("could not attach llm runtime", exc_info=True)

    async def on_config(self, config: Any) -> None:
        """Re-attach after a hot config reload swapped the provider."""
        try:
            attach_runtime(self.provider, self.app)
        except Exception:
            self.log.debug("could not re-attach llm runtime", exc_info=True)

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        data = read_json(self.state_path, default={"messages": []}) or {}
        for raw in (data.get("messages") or [])[-MAX_HISTORY:]:
            try:
                self._messages.append(ChatMessage(
                    id=str(raw.get("id") or _mid()),
                    role=str(raw.get("role") or "assistant"),
                    text=str(raw.get("text") or ""),
                    ts=int(raw.get("ts") or _now_ms()),
                    agent_id=raw.get("agent_id"),
                    meta=raw.get("meta") or {},
                ))
            except (TypeError, ValueError):
                continue
        if not self._messages:
            await self._append(ChatMessage(
                id=_mid(), role="assistant", ts=_now_ms(),
                text="我是 StackFlowOS 的系统助手。可以让我创建项目、查看进度、"
                     "处理审批，或者直接问「现在在跑什么」。",
                agent_id="agent-assistant",
            ))
        self.log.info("chat ready with %d messages", len(self._messages))

    async def stop(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._save()

    # -- public API -------------------------------------------------------
    def history(self, limit: int = 100) -> list[dict]:
        return [m.to_dict() for m in self._messages[-limit:]]

    async def send_user(self, text: str, *, from_: str = "human:operator") -> dict:
        text = (text or "").strip()
        if not text:
            raise SfoError("empty message", code="BAD_REQUEST")
        if len(text) > 8000:
            text = text[:8000]
        msg = ChatMessage(id=_mid(), role="user", text=text, ts=_now_ms(),
                          meta={"from": from_})
        await self._append(msg)
        task = asyncio.create_task(self._reply(msg))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return msg.to_dict()

    # -- reply pipeline ---------------------------------------------------
    async def _reply(self, user_msg: ChatMessage) -> None:
        if self._closed:
            return
        reply_id = _mid()
        await self._emit_delta(reply_id, "", role="assistant", typing=True)
        streamed: list[str] = []

        async def on_delta(delta: dict) -> None:
            if self._closed or delta.get("type") != "text":
                return
            chunk = str(delta.get("text") or "")
            if not chunk:
                return
            streamed.append(chunk)
            await self._emit_delta(reply_id, chunk, role="assistant")

        try:
            answer = await self._compose(user_msg.text, on_delta=on_delta)
        except SfoError as exc:
            answer = f"这个操作没做成：{exc.message}"
        except Exception as exc:  # never let the chat die
            self.log.exception("assistant reply failed")
            answer = f"我这边出了点问题：{exc}"
        if not answer:
            answer = "我在。"
        streamed_text = "".join(streamed)
        if streamed_text and answer.startswith(streamed_text):
            remaining = answer[len(streamed_text):]
            if remaining:
                await self._stream(remaining, reply_id)
        else:
            await self._stream(answer, reply_id)
        await self._append(ChatMessage(
            id=reply_id, role="assistant", text=answer, ts=_now_ms(),
            agent_id="agent-assistant",
        ))

    async def _stream(self, text: str, message_id: str) -> None:
        # Chunked deltas give the UI a streaming feel without requiring the
        # provider to support token streaming. Chunks are ephemeral events.
        step = 48
        for index in range(0, len(text), step):
            if self._closed:
                return
            await self._emit_delta(message_id, text[index:index + step], role="assistant")
            await asyncio.sleep(0.012)

    async def _emit_delta(self, message_id: str, delta: str, *, role: str,
                          typing: bool = False) -> None:
        try:
            self.eventlog.append(
                "chat.delta",
                {"message_id": message_id, "delta": delta, "role": role,
                 "typing": typing},
                source="sys",
            )
        except Exception:
            self.log.debug("delta emit failed", exc_info=True)

    async def _append(self, msg: ChatMessage) -> None:
        self._messages.append(msg)
        if len(self._messages) > MAX_HISTORY:
            self._messages = self._messages[-MAX_HISTORY:]
        self._save()
        try:
            self.eventlog.append("chat.message", msg.to_dict(), source=msg.role)
        except Exception:
            self.log.debug("chat event append failed", exc_info=True)

    def _save(self) -> None:
        try:
            write_json(self.state_path, {"messages": [m.to_dict() for m in self._messages]})
        except Exception:
            self.log.exception("could not persist chat history")

    # -- composition ------------------------------------------------------
    async def _compose(self, text: str, *, on_delta: Any = None) -> str:
        intent = self._detect_intent(text)
        if intent == "project_access":
            return await self._do_project_access(text)
        if intent == "attach_project":
            return await self._do_attach_project(text)
        if intent == "create_project":
            return await self._do_create_project(text)
        if intent == "status":
            return self._status_text()
        if intent == "projects":
            return self._projects_text()
        if intent == "approval_action":
            return await self._do_approval_action(text)
        if intent == "approvals":
            return self._approvals_text()
        if intent == "help":
            return self._help_text()
        if self.provider is not None and getattr(self.provider, "name", "") != "mock":
            return await self._llm_reply(text, on_delta=on_delta)
        return self._fallback_text(text)

    def _detect_intent(self, text: str) -> str:
        low = text.lower()
        if re.search(r"(打开|接入|导入|挂载|open|attach|import)", low) and re.search(
                r"(?:~|/)[A-Za-z0-9._~+/@%-]{2,}", text):
            return "attach_project"
        if re.search(r"(允许写|可写|读写|只读|read[- ]?only|read[- ]?write|"
                     r"write access)", low) and re.search(
                r"(项目|project|p-[0-9]+)", low):
            return "project_access"
        if re.search(r"(允许写|可写|读写|只读|read[- ]?only|read[- ]?write|"
                     r"write access)", low):
            for project in self.app.projects.list():
                if project.name and project.name.lower() in low:
                    return "project_access"
        if re.search(r"(创建|新建|开一个|开个|建一个|立项|做一?个|build|create)\s*"
                     r"(一个|个|新的)?\s*(项目)?", low) and re.search(
                         r"(项目|游戏|工具|网站|系统|应用|服务|脚本|报告|调研)", low):
            return "create_project"
        if re.search(r"(状态|进度|在跑|在做什么|怎么样|如何了|summary|status|progress)", low):
            return "status"
        if re.search(r"(项目列表|有哪些项目|projects)", low):
            return "projects"
        if re.search(r"(apr-[0-9a-f]+)", low) and re.search(
                r"(拒绝|驳回|放行|批准|允许|deny|reject|approve|allow)", low):
            return "approval_action"
        if re.search(r"(审批|权限|pending|approval)", low):
            return "approvals"
        if re.search(r"(帮助|能做什么|怎么用|help|介绍一下)", low):
            return "help"
        return "chat"

    async def _do_project_access(self, text: str) -> str:
        low = text.lower()
        target = None
        match = re.search(r"(p-[0-9]+)", text)
        if match:
            project_id = match.group(1)
            try:
                target = self.app.projects.get(project_id)
            except SfoError:
                target = None
        if target is None:
            for project in self.app.projects.list():
                if project.name and project.name.lower() in low:
                    target = project
                    break
        if target is None:
            return "请指明要修改读写模式的项目（项目 id 或名称）。"
        if not target.external:
            return f"项目 {target.id} 不是接入的外部目录，读写模式固定。"
        allow = bool(re.search(r"(允许写|可写|读写|read[- ]?write|write access)",
                               low)) and not re.search(r"(只读|read[- ]?only)", low)
        result = await self.app.operator_tool("project.access", {
            "project_id": target.id, "allow_writes": allow,
            # The human said "可写/读写" in this very message: that is the
            # second confirmation the operator layer asks for.
            "confirm": allow,
        }, subject="chat")
        if not result.get("ok"):
            return f"切换失败：{result.get('error') or '未知错误'}"
        return (f"项目 **{target.name}** 已切换为"
                f"{'可写' if allow else '只读'}。")

    async def _do_attach_project(self, text: str) -> str:
        match = re.search(r"((?:~|/)[A-Za-z0-9._~+/@%-]+)", text)
        if not match:
            return "请给出要接入的本地目录绝对路径（例如 /home/you/project）。"
        path = match.group(1).rstrip("，。；;、)")
        allow_writes = bool(re.search(
            r"(可写|读写|允许写|read[- ]?write|\brw\b|\bwrite\b)", text, re.I))
        result = await self.app.operator_tool("project.attach", {
            "path": path,
            "allow_writes": allow_writes,
            # The user named this path in the sentence; echo it as the
            # explicit confirmation instead of trusting the parser alone.
            "confirm": path,
        }, subject="chat")
        if not result.get("ok"):
            return f"接入失败：{result.get('error') or '未知错误'}"
        data = result.get("data") or {}
        mode = ("可写" if data.get("workspace_mode") == "attached-rw" else "只读")
        verb = "已经接入" if data.get("existing") else "已接入"
        return (f"{verb}目录 **{data.get('workspace')}** 作为项目"
                f" **{data.get('name')}**（`{data.get('id')}`，{mode}）。\n"
                "该目录不会被复制或删除；可以让我列出/读取其中的文件，"
                "或为它创建任务、派 Agent。")

    async def _do_create_project(self, text: str) -> str:
        goal = self._extract_goal(text)
        name = goal[:24] + ("…" if len(goal) > 24 else "")
        try:
            card = await self.app.create_project(name=name, goal=goal,
                                                 created_by="human:operator")
        except SfoError as exc:
            return f"项目没建成：{exc.message}（{exc.code}）"
        tasks = card.get("counts") or {}
        total = card.get("total_tasks", sum(tasks.values()) if tasks else 0)
        lead = None
        for agent in card.get("agents") or []:
            if agent.get("role") == "lead":
                lead = agent
                break
        lines = [f"项目 **{card.get('name')}** 已创建（`{card.get('id')}`）。"]
        if total:
            lines.append(f"我先把目标拆成了 {total} 个任务；")
        if lead:
            lines.append(f"主理人 **{lead.get('display_name') or lead.get('id')}** 已上岗，"
                         "会按任务派工人推进。")
        else:
            lines.append("主理人还没上岗，你可以在「Agent」里手动派一个。")
        lines.append("工作面板里能看到实时进度；有风险的动工会弹审批给你。")
        return "\n".join(lines)

    def _extract_goal(self, text: str) -> str:
        cleaned = re.sub(r"^[\s，,。:：-]+", "", text)
        patterns = [
            r"(?:帮我|请|麻烦|给我)?\s*(?:创建|新建|开一个|开个|建一个|立项做|做一?个)\s*"
            r"(?:一个|个|新的)?\s*(?:项目)?\s*[：:，,]?\s*(.+)$",
            r"(?:build|create|make)\s+(?:a|an)?\s*(?:project)?\s*[:\-]?\s*(.+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, cleaned, re.I)
            if match and match.group(1).strip():
                return match.group(1).strip()
        return cleaned.strip()

    def _status_text(self) -> str:
        try:
            snap = self.app.snapshot()
        except Exception:
            return "我现在读不到系统状态。"
        projects = snap.get("projects") or []
        agents = snap.get("agents") or []
        units = snap.get("units") or []
        approvals = snap.get("approvals") or []
        active = [p for p in projects if p.get("status") in ("active", "review", "degraded")]
        lines = []
        if active:
            lines.append(f"当前有 **{len(active)}** 个项目在推进：")
            for card in active[:5]:
                counts = card.get("counts") or {}
                doing = counts.get("doing", 0)
                review = counts.get("review", 0)
                done = counts.get("done", 0)
                lines.append(
                    f"- **{card.get('name')}** `{card.get('id')}` · 进度 {card.get('progress', 0)}%"
                    f" · 进行中 {doing} / 待审 {review} / 完成 {done}")
        else:
            lines.append("现在没有正在推进的项目。跟我说「创建一个项目：…」就能开工。")
        online_units = [u for u in units if u.get("status") == "online"]
        working = [a for a in agents if a.get("status") in ("working", "waiting")]
        lines.append("")
        lines.append(f"在线单元 **{len(online_units)}** 个，在岗 Agent **{len(working)}** 个，"
                     f"待审批 **{len(approvals)}** 件。")
        if approvals:
            lines.append("有操作卡在审批口，切到「审批」就能放行或拒绝。")
        return "\n".join(lines)

    def _projects_text(self) -> str:
        snap = self.app.snapshot()
        projects = snap.get("projects") or []
        if not projects:
            return "还没有项目。"
        lines = ["项目一览："]
        for card in projects[:12]:
            lines.append(f"- `{card.get('id')}` **{card.get('name')}** · "
                         f"{card.get('status')} · {card.get('progress', 0)}%")
        return "\n".join(lines)

    async def _do_approval_action(self, text: str) -> str:
        match = re.search(r"(apr-[0-9a-z]+)", text, re.IGNORECASE)
        if not match:
            return "请给出要处理的审批 id（形如 `apr-xxxxxxxx`）。"
        request_id = match.group(1)
        low = text.lower()
        decision = "deny" if re.search(r"(拒绝|驳回|deny|reject)", low) else "allow"
        result = await self.app.operator_tool("approval.decide", {
            "request_id": request_id,
            "decision": decision,
            "mode": "once",
            "note": "由系统助手根据对话指令处理",
            # The user quoted this approval id in the sentence.
            "confirm": request_id,
        }, subject="chat")
        if result.get("ok"):
            verb = "拒绝" if decision == "deny" else "放行"
            return f"已{verb}审批 `{request_id}`。"
        return f"处理失败：{result.get('error') or '未知错误'}"

    def _approvals_text(self) -> str:
        snap = self.app.snapshot()
        approvals = snap.get("approvals") or []
        if not approvals:
            return "现在没有待审批的操作。规则可以在「设置 → 安全」里调整。"
        lines = [f"有 **{len(approvals)}** 件操作在等你放行："]
        for ap in approvals[:6]:
            lines.append(f"- `{ap.get('id')}` {ap.get('capability')} · "
                         f"风险 {ap.get('risk')} · {ap.get('principal')}"
                         + (f" · {ap.get('reason')}" if ap.get("reason") else ""))
        lines.append("打开「审批」可以允许一次、允许 1 小时或总是允许。")
        return "\n".join(lines)

    def _help_text(self) -> str:
        return (
            "我能做的事情大致分四类：\n"
            "- **项目**：创建项目、看项目卡片、完成后归档；主理人会拆任务、派工人。\n"
            "- **工人**：按角色（architect/coder/tester/reviewer/researcher）派工；"
            "每个工人是独立进程，任务结束就退出。\n"
            "- **权限**：工人的高风险操作会变成审批请求；你可以放行一次/一小时/永久，"
            "也能在设置里写自动放行规则。\n"
            "- **系统**：查看单元、租约、事件流；切换 ipc/tcp 传输；改模型配置。\n\n"
            "试着说：「创建一个项目：做一个命令行贪吃蛇游戏」或「现在的进度怎么样」。"
        )

    def _fallback_text(self, text: str) -> str:
        return ("我在。可以让我「创建一个项目：…」，或者问「现在的进度怎么样」。\n"
                "（当前模型为离线模式；在设置里配置 OpenAI 兼容端点后，我会用模型来理解和规划。）")

    def _prompt_history(self, *, limit: int = 16,
                        char_budget: int = 9000) -> list[dict]:
        """Recent turns, newest-first budgeted, returned oldest-first.

        The previous implementation sent only the current user sentence to
        the model, so the dialog had no conversational memory at all.
        """
        selected: list[dict] = []
        used = 0
        for message in reversed(self._messages[-limit:]):
            if message.role not in ("user", "assistant"):
                continue
            content = (message.text or "").strip()
            if not content:
                continue
            content = content[:1800]
            if used + len(content) > char_budget and selected:
                break
            selected.append({"role": message.role, "content": content})
            used += len(content)
        selected.reverse()
        return selected

    def _chat_context(self, snap: dict) -> dict:
        """A compact, chat-sized view of the live system.

        The full snapshot is dominated by unit/agent metadata; truncating it
        used to cut projects/tasks out of the prompt entirely.
        """
        projects = []
        for card in (snap.get("projects") or [])[:8]:
            project_id = str(card.get("id") or "")
            try:
                tasks = self.app.projects.task_list(project_id=project_id)
            except Exception:
                tasks = []
            projects.append({
                "id": project_id,
                "name": card.get("name"),
                "status": card.get("status"),
                "goal": str(card.get("goal") or "")[:400],
                "brief": str(card.get("brief") or "")[:600],
                "progress": card.get("progress"),
                "counts": card.get("counts") or {},
                "current_task": card.get("current_task"),
                "tasks": [
                    {"id": getattr(task, "id", ""),
                     "title": str(getattr(task, "title", ""))[:100],
                     "status": getattr(task, "status", ""),
                     "role": getattr(task, "role", ""),
                     "assignee": getattr(task, "assignee", None)}
                    for task in tasks[:12]
                ],
                "agents": [
                    {"role": agent.get("role"), "status": agent.get("status"),
                     "task_id": agent.get("task_id")}
                    for agent in (card.get("agents") or [])[:8]
                ],
            })
        units = snap.get("units") or []
        return {
            "now": _now_ms(),
            "projects": projects,
            "units": {
                "total": len(units),
                "online": sum(
                    1 for unit in units
                    if (unit.get("state") or unit.get("status")) in
                    ("ready", "degraded", "online", "suspect", "draining")),
                "by_tier": {
                    tier: sum(1 for unit in units
                              if (unit.get("tier") or "worker") == tier)
                    for tier in ("system", "service", "worker")
                },
            },
            "agents": [
                {"id": agent.get("id"), "role": agent.get("role"),
                 "status": agent.get("status"),
                 "task_id": agent.get("task_id")}
                for agent in (snap.get("agents") or [])
                if agent.get("status") not in ("done", "failed", "dead")
            ][:12],
            "approvals": [
                {"id": approval.get("id"),
                 "capability": approval.get("capability"),
                 "principal": approval.get("principal"),
                 "risk": approval.get("risk")}
                for approval in (snap.get("approvals") or [])[:10]
            ],
            "config": {
                "provider": (snap.get("config") or {}).get("llm", {}).get("provider"),
                "model": (snap.get("config") or {}).get("llm", {}).get("model"),
            },
        }

    async def _llm_reply(self, text: str, *, on_delta: Any = None) -> str:
        try:
            snap = self.app.snapshot()
        except Exception:
            snap = {}
        system = (
            "你是 StackFlowOS 的系统助手，也是操作员的代理，拥有系统管理权限。"
            "你可以看到最近对话历史和实时系统状态；回答要延续上下文，"
            "引用之前说过的项目/任务时先核对实时状态。"
            "用户通过对话下达指令；需要操作时只输出一个 JSON 对象："
            '{"tool":"工具名","args":{...}}。不要请求审批、不要输出多个 JSON、'
            "不要编造工具名。不需要操作时直接用中文回答，简洁、具体，"
            "不要编造状态里没有的事实。若用户要求做某件事而你没有对应工具，"
            "直接说明做不到，并给出最接近的可执行选项。\n\n"
            + catalog_text()
            + "\n\n实时状态（JSON，只包含聊天需要的摘要）："
            + json.dumps(self._chat_context(snap), ensure_ascii=False)
        )
        messages = [{"role": "system", "content": system}]
        history = self._prompt_history()
        if not history or history[-1].get("role") != "user" or \
                history[-1].get("content") != text:
            history.append({"role": "user", "content": text})
        messages.extend(history)
        result = await complete(self.provider, messages, role="assistant",
                                on_delta=on_delta)
        raw = result.text or ""
        action = _parse_action(raw)
        if action:
            runner = getattr(self.app, "operator_tool", None) or \
                getattr(self.app, "assistant_tool", None)
            if runner is not None:
                # No auto-filled confirm here: the model must supply it
                # explicitly, otherwise the operator layer refuses loudly.
                result = await runner(action["tool"], action.get("args") or {},
                                      subject="chat")
                if result.get("ok"):
                    return self._explain_tool_result(
                        action["tool"], result.get("data") or {})
                if result.get("code") == "UNKNOWN_TOOL":
                    return ("我这边没有这个操作工具。可以让我管理项目、任务、"
                            "Agent、审批、规则和系统配置。")
                return f"操作失败：{result.get('error') or '未知错误'}"
        return raw.strip() or self._fallback_text(text)

    def _explain_tool_result(self, tool: str, data: dict) -> str:
        if tool in ("project.delete", "project.remove", "project.purge"):
            return (f"项目 **{data.get('name') or data.get('deleted')}** 已永久删除"
                    + ("，工作区已清空。" if data.get("workspace_purged") else "。"))
        if tool == "project.archive":
            project = data.get("project") or {}
            return (f"项目 **{project.get('name') or project.get('id') or ''}** 已归档，"
                    f"停止 {int(data.get('agents_stopped') or 0)} 个 Agent。")
        if tool == "project.complete":
            return f"项目 **{data.get('name') or data.get('id') or ''}** 已确认完成。"
        if tool in ("project.access", "project.set_mode", "project.set_access",
                    "project.allow_writes"):
            mode = "可写" if data.get("workspace_mode") == "attached-rw" else "只读"
            return f"项目 **{data.get('name')}** 已切换为 {mode}。"
        if tool in ("project.attach", "project.open", "project.import"):
            mode = "只读" if data.get("workspace_mode") == "attached-ro" else "可写"
            return (f"已接入项目 **{data.get('name')}**（`{data.get('id')}`，{mode}），"
                    f"目录：{data.get('workspace')}")
        if tool == "project.create":
            return (f"项目 **{data.get('name')}** 已创建（`{data.get('id')}`），"
                    "主理人会开始拆解和派工。")
        if tool in ("approval.decide", "approval.allow", "approval.approve",
                    "approval.deny", "approval.reject"):
            status = data.get("status") or "完成"
            return f"审批 `{data.get('id') or ''}` 已处理（{status}）。"
        if tool == "agent.spawn":
            return f"已派工：`{data.get('id')}`（{data.get('role')}）。"
        if tool in ("agent.kill", "unit.kill"):
            return "已终止。"
        if tool == "sys.status":
            return data.get("summary") or "已刷新系统状态。"
        if tool in ("config.set", "rule.create", "rule.delete", "rule.update"):
            return "配置已更新（已热加载）。"
        if tool in ("task.update", "task.note", "task.create"):
            return f"任务 `{data.get('id') or ''}` 已更新。"
        if tool == "project.files":
            entries = data.get("entries") or []
            names = [f"{item.get('name')}{'/' if item.get('type') == 'dir' else ''}"
                     for item in entries[:40]]
            more = f" 等 {len(entries)} 项" if len(entries) > 40 else ""
            return (f"`{data.get('path') or '.'}` 下的内容："
                    + ("、".join(names) + more if names else "（空）"))
        if tool == "project.read":
            content = str(data.get("content") or "")
            suffix = "（已截断）" if data.get("truncated") else ""
            return (f"`{data.get('path')}` 的内容{suffix}：\n```\n"
                    + content[:1200] + "\n```")
        if tool == "project.list":
            projects = data.get("projects") or []
            if not projects:
                return "现在没有项目。"
            return "项目：" + "、".join(
                f"{p.get('name')}(`{p.get('id')}`)" for p in projects[:20])
        if tool == "task.list":
            tasks = data.get("tasks") or []
            return f"共 {len(tasks)} 个任务：" + "、".join(
                f"{t.get('title')}[{t.get('status')}]" for t in tasks[:20])
        return "操作完成。"


def _parse_action(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    try:
        obj = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        try:
            obj = json.loads(match.group(0))
        except ValueError:
            return None
    if isinstance(obj, dict) and isinstance(obj.get("tool"), str):
        args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
        return {"tool": obj["tool"], "args": args}
    return None
