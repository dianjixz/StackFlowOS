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
        try:
            answer = await self._compose(user_msg.text)
        except SfoError as exc:
            answer = f"这个操作没做成：{exc.message}"
        except Exception as exc:  # never let the chat die
            self.log.exception("assistant reply failed")
            answer = f"我这边出了点问题：{exc}"
        if not answer:
            answer = "我在。"
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
    async def _compose(self, text: str) -> str:
        intent = self._detect_intent(text)
        if intent == "create_project":
            return await self._do_create_project(text)
        if intent == "status":
            return self._status_text()
        if intent == "projects":
            return self._projects_text()
        if intent == "approvals":
            return self._approvals_text()
        if intent == "help":
            return self._help_text()
        if self.provider is not None and getattr(self.provider, "name", "") != "mock":
            return await self._llm_reply(text)
        return self._fallback_text(text)

    def _detect_intent(self, text: str) -> str:
        low = text.lower()
        if re.search(r"(创建|新建|开一个|开个|建一个|立项|做一?个|build|create)\s*"
                     r"(一个|个|新的)?\s*(项目)?", low) and re.search(
                         r"(项目|游戏|工具|网站|系统|应用|服务|脚本|报告|调研)", low):
            return "create_project"
        if re.search(r"(状态|进度|在跑|在做什么|怎么样|如何了|summary|status|progress)", low):
            return "status"
        if re.search(r"(项目列表|有哪些项目|projects)", low):
            return "projects"
        if re.search(r"(审批|权限|pending|approval)", low):
            return "approvals"
        if re.search(r"(帮助|能做什么|怎么用|help|介绍一下)", low):
            return "help"
        return "chat"

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

    _ASSISTANT_PROMPT = (
        "你是 StackFlowOS 的系统助手，一个以操作系统口吻说话的执行型助手。"
        "你能看到下方的实时系统状态。需要操作时只输出一个 JSON 对象：\n"
        '{"tool":"project.create","args":{"name":"...","goal":"..."}}\n'
        "可用工具：project.create(name,goal)、project.list()、project.get(project_id)、"
        "task.list(project_id)、unit.list()、event.query(project_id,since,limit)、"
        "agent.spawn(role,project_id,task_id?)、agent.kill(agent_id)、"
        "project.update(project_id,patch)、project.archive(project_id)、"
        "project.delete(project_id,purge_workspace)。\n"
        "工具结果会作为下一条消息返回给你；据此继续调用工具，或直接用中文给出最终回答。"
        "高风险工具会进入人工审批，你会等到结果再继续。"
        "不需要工具时直接用中文回答，简洁、具体，不要编造状态里没有的事实。"
    )
    _MAX_TOOL_ROUNDS = 6

    def _brief_state(self) -> str:
        try:
            snap = self.app.snapshot()
        except Exception:
            return "{}"
        brief = {
            "projects": [
                {"id": p.get("id"), "name": p.get("name"), "status": p.get("status"),
                 "progress": p.get("progress"), "counts": p.get("counts")}
                for p in (snap.get("projects") or [])[:20]],
            "agents": [
                {"id": a.get("id"), "role": a.get("role"), "status": a.get("status"),
                 "project_id": a.get("project_id"), "task_id": a.get("task_id")}
                for a in (snap.get("agents") or [])[-20:]],
            "pending_approvals": [
                {"id": a.get("id"), "capability": a.get("capability"),
                 "risk": a.get("risk"), "principal": a.get("principal")}
                for a in (snap.get("approvals") or [])[:10]],
            "units_online": len([u for u in (snap.get("units") or [])
                                 if u.get("status") == "online"]),
        }
        return json.dumps(brief, ensure_ascii=False)

    def _history_messages(self, text: str) -> list[dict]:
        turns = [{"role": m.role, "content": m.text}
                 for m in self._messages[-13:]
                 if m.role in ("user", "assistant") and m.text]
        if not turns or turns[-1] != {"role": "user", "content": text}:
            turns.append({"role": "user", "content": text})
        return turns

    async def _announce_pending(self, request: Any) -> None:
        await self._append(ChatMessage(
            id=_mid(), role="assistant", ts=_now_ms(), agent_id="agent-assistant",
            text=(f"这一步需要你批准：`{request.capability}`（风险 {request.risk}，"
                  f"审批 `{request.id}`）。请打开「审批」放行或拒绝，我会等结果再继续。"),
        ))

    async def _llm_reply(self, text: str) -> str:
        messages = [
            {"role": "system", "content": self._ASSISTANT_PROMPT},
            {"role": "system", "content": f"实时状态（JSON）：{self._brief_state()}"},
            *self._history_messages(text),
        ]
        raw = ""
        last_note = ""
        for _round in range(self._MAX_TOOL_ROUNDS):
            raw = await self.provider.chat(messages)
            action = _parse_action(raw)
            if not action or not hasattr(self.app, "assistant_tool"):
                return raw.strip() or self._fallback_text(text)
            result = await self.app.assistant_tool(
                action["tool"], action.get("args") or {}, wait=True,
                on_pending=self._announce_pending)
            if result.get("ok"):
                last_note = self._explain_tool_result(action["tool"],
                                                      result.get("data") or {})
                payload = {"ok": True, "data": result.get("data")}
            else:
                last_note = f"这个操作没做成：{result.get('error')}"
                payload = {"ok": False, "error": result.get("error")}
            observation = json.dumps(payload, ensure_ascii=False, default=str)
            if len(observation) > 6000:
                observation = observation[:6000] + "…[已截断]"
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"工具结果：{observation}"})
        return last_note or raw.strip() or self._fallback_text(text)

    def _explain_tool_result(self, tool: str, data: dict) -> str:
        if tool == "project.create":
            return (f"项目 **{data.get('name')}** 已创建（`{data.get('id')}`），"
                    "主理人会开始拆解和派工。")
        if tool == "agent.spawn":
            return f"已派工：`{data.get('id')}`（{data.get('role')}）。"
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
