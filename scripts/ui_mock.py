#!/usr/bin/env python3
"""Fixture server for iterating on the StackFlowOS operator UI.

This is *not* part of the product and must never be imported by it. It serves
``stackflowos/webassets`` together with a realistic in-memory API and SSE
stream so the frontend can be developed and screenshotted without booting the
real hub.

Run (defaults to port 17800, or the next free port in 17800..17899)::

    python3 scripts/ui_mock.py
    python3 scripts/ui_mock.py --port 17800
    python3 scripts/ui_mock.py --no-ticker

Clicking around mutates the fixture (chat, approvals, projects, tasks, rules,
agents, units, config) so interactive smoke checks work. The ambient ticker
pushes a chat.delta → chat.message → task.updated → approval.requested
sequence every ~4 seconds.
"""

from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import queue
import re
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "stackflowos" / "webassets"
DEFAULT_PORT = 17800
DEFAULT_HOST = "127.0.0.1"
HEARTBEAT_S = 10.0

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def make_message(role: str, text: str, ts: int, agent_id: str | None = None) -> dict:
    message = {"id": new_id("msg"), "role": role, "text": text, "ts": ts}
    if agent_id:
        message["agent_id"] = agent_id
    return message


def make_task(
    task_id: str,
    project_id: str,
    title: str,
    *,
    description: str = "",
    status: str = "todo",
    priority: int = 0,
    role: str | None = None,
    assignee: str | None = None,
    age_s: int = 3600,
    checks: list[dict] | None = None,
    artifacts: list[dict] | None = None,
    notes: list[dict] | None = None,
    milestone_id: str | None = None,
) -> dict:
    created = now_ms() - age_s * 1000
    started = created + 30_000 if status in ("doing", "review", "done") else None
    finished = created + 300_000 if status == "done" else None
    return {
        "id": task_id,
        "project_id": project_id,
        "title": title,
        "description": description,
        "status": status,
        "priority": priority,
        "role": role,
        "assignee": assignee,
        "created_by": "agent-lead-1",
        "deps": [],
        "artifacts": artifacts or [],
        "checks": checks or [],
        "notes": notes or [],
        "created_at": created,
        "updated_at": created + 60_000,
        "started_at": started,
        "finished_at": finished,
        "error": None,
        "milestone_id": milestone_id,
    }


def checks_for(*items: tuple[str, bool]) -> list[dict]:
    return [
        {"id": f"chk-{uuid.uuid4().hex[:6]}", "text": text, "done": done, "note": ""}
        for text, done in items
    ]


class Problem(Exception):
    def __init__(self, status: int, code: str, message: str, detail=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------------------
# in-memory fixture
# ---------------------------------------------------------------------------
class Fixture:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.clients: list[queue.Queue] = []
        self.stop = threading.Event()
        self.ticker_thread: threading.Thread | None = None
        self.started_at = now_ms() - 3 * 3600 * 1000
        self.last_seq = 1200
        self.events: list[dict] = []
        self.ambient_index = 0
        self.spawn_count = 5
        self.tick_count = 0
        self.conversation = [
            make_message(
                "assistant",
                "我是 StackFlowOS 的系统助手。可以让我创建项目、查看进度、处理审批，"
                "或者直接问「现在在跑什么」。",
                now_ms() - 9 * 60 * 1000,
                "agent-assistant",
            ),
            make_message("user", "做一个命令行贪吃蛇游戏", now_ms() - 8 * 60 * 1000 + 12_000),
            make_message(
                "assistant",
                "项目 **贪吃蛇终端版** 已创建（`p-1`）。\n"
                "我先把目标拆成了 12 个任务；主理人 **Lead · 工人 1** 已上岗，"
                "会按任务派工人推进。\n"
                "工作面板里能看到实时进度；有风险的动工会弹审批给你。",
                now_ms() - 8 * 60 * 1000 + 20_000,
                "agent-assistant",
            ),
            make_message(
                "assistant",
                "现在的进度怎么样？当前有 **2** 个项目在推进：`p-1` 进度 56%，"
                "`p-2` 已进入待验收。还有 **2** 件审批在等你。",
                now_ms() - 3 * 60 * 1000,
                "agent-assistant",
            ),
        ]
        self.units = self._seed_units()
        self.agents = self._seed_agents()
        self.tasks = self._seed_tasks()
        self.projects = self._seed_projects()
        self.approvals = self._seed_approvals()
        self.rules = self._seed_rules()
        self.leases = self._seed_leases()
        self.config = self._seed_config()
        self._refresh_all_cards()
        self._seed_events()

    # -- seed helpers -----------------------------------------------------
    def _seed_units(self) -> list[dict]:
        t = now_ms()
        hwinfo = {
            "platform": "Linux-6.8.0-x86_64",
            "python": "3.12.3",
            "cpu_count": 16,
            "hostname": "orion-7",
        }
        workdir = REPO_ROOT / ".stackflowos" / "run"
        return [
            {
                "work_id": "lead.1",
                "kind": "agent",
                "label": "Lead · 工人 1",
                "project_id": "p-1",
                "pid": 18421,
                "host": "orion-7",
                "transport": "ipc",
                "rpc_address": f"ipc://{workdir}/lead.1-rpc.ipc",
                "events_address": f"ipc://{workdir}/lead.1-events.ipc",
                "status": "online",
                "abilities": [
                    {"name": "fs.write", "tool": "fs", "scope": "project"},
                    {"name": "shell.run", "tool": "shell", "scope": "project"},
                    {"name": "data.read", "tool": "data", "scope": "global"},
                    {"name": "agent.spawn", "tool": "agent", "scope": "project"},
                ],
                "hwinfo": hwinfo,
                "started_at": t - 52 * 60 * 1000,
                "last_seen": t - 2_000,
                "restart_count": 0,
                "metadata": {"role": "lead", "project": "p-1"},
            },
            {
                "work_id": "coder.2",
                "kind": "agent",
                "label": "Coder · 工人 2",
                "project_id": "p-1",
                "pid": 18455,
                "host": "orion-7",
                "transport": "ipc",
                "rpc_address": f"ipc://{workdir}/coder.2-rpc.ipc",
                "events_address": f"ipc://{workdir}/coder.2-events.ipc",
                "status": "online",
                "abilities": [
                    {"name": "fs.write", "tool": "fs", "scope": "project"},
                    {"name": "shell.run", "tool": "shell", "scope": "project"},
                ],
                "hwinfo": hwinfo,
                "started_at": t - 18 * 60 * 1000,
                "last_seen": t - 4_000,
                "restart_count": 1,
                "metadata": {"role": "coder", "project": "p-1", "task": "t-102"},
            },
            {
                "work_id": "tester.4",
                "kind": "agent",
                "label": "Tester · 工人 4",
                "project_id": "p-2",
                "pid": 18502,
                "host": "orion-7",
                "transport": "tcp",
                "rpc_address": "tcp://127.0.0.1:17834",
                "events_address": "tcp://127.0.0.1:17835",
                "status": "suspect",
                "abilities": [
                    {"name": "shell.run", "tool": "shell", "scope": "project"},
                    {"name": "fs.write", "tool": "fs", "scope": "project"},
                ],
                "hwinfo": hwinfo,
                "started_at": t - 9 * 60 * 1000,
                "last_seen": t - 11_000,
                "restart_count": 0,
                "metadata": {"role": "tester", "project": "p-2"},
            },
        ]

    def _seed_agents(self) -> list[dict]:
        t = now_ms()
        return [
            {
                "id": "agent-lead-1",
                "role": "lead",
                "project_id": "p-1",
                "task_id": None,
                "unit_id": "lead.1",
                "status": "working",
                "model": "mock",
                "display_name": "Lead · 工人 1",
                "created_at": t - 52 * 60 * 1000,
                "last_seen": t - 2_000,
                "stats": {"steps": 34, "tasks_assigned": 7},
                "summary": "正在协调 2 个工人，等待 t-104 的评审结果。",
                "error": None,
            },
            {
                "id": "agent-coder-2",
                "role": "coder",
                "project_id": "p-1",
                "task_id": "t-102",
                "unit_id": "coder.2",
                "status": "working",
                "model": "mock",
                "display_name": "Coder · 工人 2",
                "created_at": t - 18 * 60 * 1000,
                "last_seen": t - 4_000,
                "stats": {"steps": 22, "edits": 9},
                "summary": "正在编辑 src/snake.py：重写 tick 循环与输入缓冲。",
                "error": None,
            },
            {
                "id": "agent-reviewer-3",
                "role": "reviewer",
                "project_id": "p-2",
                "task_id": "t-205",
                "unit_id": "reviewer.3",
                "status": "waiting",
                "model": "mock",
                "display_name": "Reviewer · 工人 3",
                "created_at": t - 27 * 60 * 1000,
                "last_seen": t - 30_000,
                "stats": {"reviews": 3},
                "summary": "等待报告草稿完成，准备评审措辞和阈值。",
                "error": None,
            },
            {
                "id": "agent-tester-4",
                "role": "tester",
                "project_id": "p-2",
                "task_id": "t-206",
                "unit_id": "tester.4",
                "status": "idle",
                "model": "mock",
                "display_name": "Tester · 工人 4",
                "created_at": t - 9 * 60 * 1000,
                "last_seen": t - 11_000,
                "stats": {"tests": 18, "passed": 18},
                "summary": "上一轮 18 个用例全部通过，等待新任务。",
                "error": None,
            },
        ]

    def _seed_tasks(self) -> dict[str, list[dict]]:
        p1 = [
            make_task("t-101", "p-1", "搭建项目骨架与入口", status="done", priority=1,
                      role="architect", assignee="agent-lead-1", age_s=8_400,
                      checks=checks_for(("目录结构建立", True), ("入口可运行", True)),
                      artifacts=[
                          {"type": "file", "path": ".stackflowos/projects/p-1/workspace/src/main.py",
                           "title": "main.py", "ts": now_ms() - 8_000_000},
                      ]),
            make_task("t-102", "p-1", "实现游戏主循环与渲染", status="doing", priority=2,
                      role="coder", assignee="agent-coder-2", age_s=6_000,
                      description="用 curses 做双缓冲渲染；每 tick 处理输入、移动蛇身、重绘。",
                      checks=checks_for(("固定 60ms tick", True), ("输入不阻塞", False),
                                        ("退出时恢复终端", False)),
                      artifacts=[
                          {"type": "file",
                           "path": ".stackflowos/projects/p-1/workspace/src/snake.py",
                           "title": "snake.py · 输入缓冲", "ts": now_ms() - 90_000},
                      ],
                      notes=[{"ts": now_ms() - 120_000, "author": "agent-coder-2",
                              "text": "已经能跑通移动，正在补输入缓冲。"}]),
            make_task("t-103", "p-1", "实现食物、计分与加速", status="done", priority=1,
                      role="coder", assignee="agent-coder-2", age_s=7_500,
                      checks=checks_for(("食物不刷在蛇身上", True), ("每 5 分加速", True))),
            make_task("t-104", "p-1", "加入障碍物与碰撞判定", status="review", priority=2,
                      role="coder", assignee="agent-coder-2", age_s=5_000,
                      checks=checks_for(("墙体碰撞", True), ("自撞判定", True), ("障碍物生成", True)),
                      artifacts=[
                          {"type": "diff", "path": ".stackflowos/projects/p-1/workspace/src/snake.py",
                           "title": "snake.py 核心循环", "ts": now_ms() - 400_000},
                      ]),
            make_task("t-105", "p-1", "补单元测试（移动/碰撞/计分）", status="todo", priority=2,
                      role="tester"),
            make_task("t-106", "p-1", "写 README 与运行说明", status="done", priority=0,
                      role="researcher", assignee="agent-lead-1", age_s=9_000,
                      checks=checks_for(("快速开始", True), ("按键说明", True)),
                      artifacts=[{"type": "file", "path": ".stackflowos/projects/p-1/workspace/README.md",
                                  "title": "README.md", "ts": now_ms() - 9_000_000}]),
            make_task("t-107", "p-1", "性能剖析：长蛇场景", status="todo", priority=1, role="architect"),
            make_task("t-108", "p-1", "处理终端尺寸变化的边界", status="blocked", priority=1,
                      role="coder", assignee="agent-coder-2", age_s=4_000,
                      notes=[{"ts": now_ms() - 180_000, "author": "agent-coder-2",
                              "text": "需要确认 ncurses 在 resize 时的信号时序。"}]),
            make_task("t-109", "p-1", "暂停/继续与退出流程", status="doing", priority=1,
                      role="coder", assignee="agent-coder-2", age_s=3_000),
            make_task("t-110", "p-1", "录制演示 GIF 并截图", status="done", priority=0,
                      role="researcher", assignee="agent-lead-1", age_s=10_000),
            make_task("t-111", "p-1", "最终代码评审与清理", status="todo", priority=3, role="reviewer"),
            make_task("t-112", "p-1", "发布 v0.1.0 标签", status="done", priority=1,
                      role="lead", assignee="agent-lead-1", age_s=11_000),
        ]
        p2 = [
            make_task("t-201", "p-2", "采集主机 CPU/内存/磁盘指标", status="done", priority=1,
                      role="coder", assignee="agent-tester-4", age_s=7_200),
            make_task("t-202", "p-2", "写异常阈值判定", status="done", priority=2,
                      role="architect", assignee="agent-reviewer-3", age_s=6_400),
            make_task("t-203", "p-2", "生成 Markdown 巡检报告", status="done", priority=1,
                      role="coder", assignee="agent-tester-4", age_s=5_600,
                      checks=checks_for(("Markdown 可渲染", True), ("异常项高亮", True))),
            make_task("t-204", "p-2", "接入 cron 每日巡检", status="done", priority=2,
                      role="architect", assignee="agent-reviewer-3", age_s=4_800),
            make_task("t-205", "p-2", "评审报告模板与措辞", status="review", priority=2,
                      role="reviewer", assignee="agent-reviewer-3", age_s=3_600,
                      checks=checks_for(("模板结构", True), ("措辞检查", False))),
            make_task("t-206", "p-2", "补充 CPU 温度采集", status="review", priority=1,
                      role="tester", assignee="agent-tester-4", age_s=2_800,
                      checks=checks_for(("读取 hwmon", True), ("缺失时降级", False))),
            make_task("t-207", "p-2", "异常项自动创建工单", status="todo", priority=2, role="architect"),
            make_task("t-208", "p-2", "归档 2026-11 巡检记录", status="done", priority=0,
                      role="lead", assignee="agent-lead-1", age_s=1_800),
        ]
        return {"p-1": p1, "p-2": p2}

    def _seed_projects(self) -> list[dict]:
        t = now_ms()
        return [
            {
                "id": "p-1",
                "name": "贪吃蛇终端版",
                "goal": "用 Python 写一个可玩的命令行贪吃蛇：计分、障碍物、可暂停，附运行说明。",
                "status": "active",
                "progress": 0,
                "counts": {},
                "current_task": None,
                "total_tasks": 0,
                "updated_at": t - 95_000,
                "brief": "先跑通核心循环，再补测试和 README。",
                "workspace": str(REPO_ROOT / ".stackflowos/projects/p-1/workspace"),
                "tags": ["python", "cli", "game"],
                "color": "#7c5cff",
                "degraded": False,
                "archived_at": None,
                "agents": [],
                "pending_approvals": 0,
                "leases": [],
                "milestones": 3,
            },
            {
                "id": "p-2",
                "name": "运维巡检报告",
                "goal": "把每日主机巡检自动化：采集指标、按阈值标红、生成可分享的 Markdown 报告。",
                "status": "review",
                "progress": 0,
                "counts": {},
                "current_task": None,
                "total_tasks": 0,
                "updated_at": t - 6 * 60 * 1000,
                "brief": "报告已经进评审，确认措辞后归档。",
                "workspace": str(REPO_ROOT / ".stackflowos/projects/p-2/workspace"),
                "tags": ["ops", "report"],
                "color": "#3ddc97",
                "degraded": False,
                "archived_at": None,
                "agents": [],
                "pending_approvals": 0,
                "leases": [],
                "milestones": 2,
            },
        ]

    def _seed_approvals(self) -> list[dict]:
        t = now_ms()
        return [
            {
                "id": "apr-7f2c91a3",
                "principal": "agent-coder-2",
                "capability": "shell.run",
                "scope": {
                    "project_id": "p-1",
                    "cwd": str(REPO_ROOT / ".stackflowos/projects/p-1/workspace"),
                    "command": "python -m pytest -q",
                },
                "risk": "high",
                "reason": "运行测试套件，需要在项目工作区执行 pytest。",
                "preview": (
                    "$ cd .stackflowos/projects/p-1/workspace\n"
                    "$ python -m pytest -q\n"
                    "............\n"
                    "18 passed in 0.42s\n"
                    "$ python -m unittest discover -s tests -v"
                ),
                "project_id": "p-1",
                "task_id": "t-104",
                "status": "pending",
                "mode": None,
                "grant_id": None,
                "created_at": t - 70_000,
                "expires_at": t + 247_000,
                "decided_at": None,
                "decided_by": None,
                "note": "",
            },
            {
                "id": "apr-9b3d2e14",
                "principal": "agent-lead-1",
                "capability": "fs.write",
                "scope": {
                    "project_id": "p-1",
                    "path_prefix": str(REPO_ROOT / ".stackflowos/projects/p-1/workspace/src"),
                },
                "risk": "medium",
                "reason": "更新核心循环文件 snake.py 中的移动与碰撞逻辑。",
                "preview": (
                    "diff --git a/src/snake.py b/src/snake.py\n"
                    "--- a/src/snake.py\n"
                    "+++ b/src/snake.py\n"
                    "@@ -48,7 +48,9 @@ class Game:\n"
                    "-        self.snake.move(direction)\n"
                    "+        self.snake.move(direction)\n"
                    "+        self.score += self.food.eaten\n"
                    "+        self.speed = max(0.03, 0.06 - self.score * 0.002)\n"
                ),
                "project_id": "p-1",
                "task_id": "t-104",
                "status": "pending",
                "mode": None,
                "grant_id": None,
                "created_at": t - 35_000,
                "expires_at": t + 265_000,
                "decided_at": None,
                "decided_by": None,
                "note": "",
            },
        ]

    def _seed_rules(self) -> list[dict]:
        t = now_ms()
        return [
            {
                "id": "rule-6c1a2f00",
                "effect": "allow",
                "capability": "data.read",
                "principal": "*",
                "scope": {},
                "risk_max": "low",
                "expires_at": None,
                "uses_left": None,
                "note": "读取系统状态无需审批",
                "source": "rule",
                "created_by": "human:operator",
                "created_at": t - 3 * 3600 * 1000,
            },
            {
                "id": "rule-11d8c4b2",
                "effect": "allow",
                "capability": "shell.run",
                "principal": "agent-coder.*",
                "scope": {"project_id": "p-1"},
                "risk_max": "medium",
                "expires_at": t + 3600 * 1000,
                "uses_left": None,
                "note": "项目 p-1 的测试命令",
                "source": "rule",
                "created_by": "human:operator",
                "created_at": t - 40 * 60 * 1000,
            },
            {
                "id": "rule-2f7b9d11",
                "effect": "deny",
                "capability": "shell.run",
                "principal": "*",
                "scope": {"command_regex": "rm -rf|shutdown|reboot"},
                "risk_max": "high",
                "expires_at": None,
                "uses_left": None,
                "note": "危险命令永不自动放行",
                "source": "rule",
                "created_by": "human:operator",
                "created_at": t - 2 * 3600 * 1000,
            },
        ]

    def _seed_leases(self) -> list[dict]:
        t = now_ms()
        return [
            {
                "id": "lease-4a91",
                "resource": str(REPO_ROOT / ".stackflowos/projects/p-1/workspace/src/snake.py"),
                "holder": "agent-coder-2",
                "ttl_s": 600,
                "renewable": True,
                "preemptible": False,
                "priority": 1,
                "created_at": t - 180_000,
                "expires_at": t + 420_000,
                "status": "active",
                "metadata": {"mode": "write", "project_id": "p-1"},
            },
            {
                "id": "lease-7c22",
                "resource": str(REPO_ROOT / ".stackflowos/projects/p-2/workspace/report.md"),
                "holder": "agent-reviewer-3",
                "ttl_s": 300,
                "renewable": True,
                "preemptible": True,
                "priority": 2,
                "created_at": t - 60_000,
                "expires_at": t + 240_000,
                "status": "active",
                "metadata": {"mode": "review", "project_id": "p-2"},
            },
        ]

    def _seed_config(self) -> dict:
        state = REPO_ROOT / ".stackflowos"
        return {
            "system": {"name": "StackFlowOS", "host_label": "orion-7", "language": "zh-CN"},
            "transport": {
                "mode": "ipc",
                "host": "127.0.0.1",
                "advertise_host": None,
                "port_pool_start": 17820,
                "port_pool_end": 17899,
                "ipc_dir": None,
                "rpc_timeout_s": 8.0,
                "sys_rpc_address": f"ipc://{state}/run/sys-rpc.ipc",
            },
            "web": {"host": "127.0.0.1", "port": 17800, "open_browser": False,
                    "sse_heartbeat_s": 15.0},
            "security": {
                "auto_allow_low": True,
                "medium_default": "ask",
                "high_default": "ask",
                "approval_ttl_s": 300,
                "grant_ttl_s": 3600,
                "shell_timeout_s": 60,
                "allow_shell": True,
                "max_preview_chars": 4000,
            },
            "llm": {
                "provider": "mock",
                "base_url": "",
                "model": "",
                "api_key": "",
                "temperature": 0.2,
                "max_tokens": 1200,
                "timeout_s": 60.0,
            },
            "agents": {
                "heartbeat_s": 2.0,
                "offline_after_s": 8.0,
                "max_workers": 6,
                "max_steps": 16,
                "task_poll_s": 1.5,
                "auto_spawn_project_lead": True,
                "aggregator_interval_s": 240.0,
                "restart_limit": 3,
                "restart_window_s": 300.0,
            },
            "paths": {
                "root": str(REPO_ROOT),
                "state_dir": str(state),
                "run_dir": str(state / "run"),
                "events_dir": str(state / "events"),
                "logs_dir": str(state / "logs"),
                "projects_dir": str(state / "projects"),
                "tmp_dir": str(state / "tmp"),
            },
        }

    # -- cards ------------------------------------------------------------
    def _refresh_all_cards(self) -> None:
        for project in self.projects:
            self._refresh_card(project["id"])

    def _refresh_card(self, project_id: str) -> None:
        project = next((item for item in self.projects if item["id"] == project_id), None)
        if project is None:
            return
        tasks = self.tasks.get(project_id, [])
        counts = {"todo": 0, "doing": 0, "review": 0, "done": 0, "blocked": 0}
        for task in tasks:
            counts[task["status"]] = counts.get(task["status"], 0) + 1
        weights = {"done": 1.0, "review": 0.85, "doing": 0.45, "todo": 0.0, "blocked": 0.0}
        progress = 0
        if tasks:
            progress = max(0, min(100, int(sum(weights.get(t["status"], 0) for t in tasks)
                                        / len(tasks) * 100 + 0.5)))
        current = next((task for task in tasks if task["status"] == "doing"), None)
        project["counts"] = counts
        project["progress"] = progress
        project["total_tasks"] = len(tasks)
        project["current_task"] = copy.deepcopy(current) if current else None
        project["agents"] = [
            copy.deepcopy(agent) for agent in self.agents
            if agent.get("project_id") == project_id
            and agent.get("status") not in ("dead", "failed")
        ]
        project["pending_approvals"] = sum(
            1 for approval in self.approvals
            if approval.get("project_id") == project_id and approval.get("status") == "pending"
        )
        project["leases"] = [
            copy.deepcopy(lease) for lease in self.leases
            if project_id in json.dumps(lease.get("metadata", {})) or project_id in lease["resource"]
        ]

    # -- events -----------------------------------------------------------
    def _emit(self, event_type: str, data: dict, *, source: str = "sys",
              project_id: str | None = None, ts: int | None = None) -> dict:
        with self.lock:
            self.last_seq += 1
            event = {
                "seq": self.last_seq,
                "id": new_id("ev"),
                "type": event_type,
                "source": source,
                "ts": ts if ts is not None else now_ms(),
                "data": copy.deepcopy(data),
            }
            if project_id:
                event["project_id"] = project_id
            self.events.append(event)
            if len(self.events) > 800:
                self.events = self.events[-800:]
            clients = list(self.clients)
        for client in clients:
            try:
                client.put_nowait(event)
            except queue.Full:
                pass
        return event

    def _seed_events(self) -> None:
        t = now_ms()

        def add(event_type, data, source="sys", project_id=None, age_s=0):
            self._emit(event_type, data, source=source, project_id=project_id,
                       ts=t - age_s * 1000)

        add("sys.boot", {"version": "0.1.0", "transport": "ipc", "host": "orion-7",
                         "web_port": 17800}, age_s=3 * 3600)
        add("sys.config.loaded", {"source": "config.json", "sections": 6}, age_s=3 * 3600 - 2)
        add("unit.registered", {"work_id": "lead.1", "kind": "agent", "status": "online"},
            source="lead.1", project_id="p-1", age_s=3 * 3600 - 5)
        add("project.created", {"project_id": "p-1", "name": "贪吃蛇终端版",
                                "status": "planning", "by": "human:operator"},
            source="operator", project_id="p-1", age_s=2 * 3600 + 1800)
        add("agent.started", {"agent_id": "agent-lead-1", "role": "lead", "status": "spawning"},
            source="sys", project_id="p-1", age_s=2 * 3600 + 1770)
        add("task.created", {"task_id": "t-101", "title": "搭建项目骨架与入口", "status": "todo"},
            source="agent-lead-1", project_id="p-1", age_s=2 * 3600 + 1700)
        add("task.started", {"task_id": "t-101", "title": "搭建项目骨架与入口", "status": "doing"},
            source="agent-lead-1", project_id="p-1", age_s=2 * 3600 + 1600)
        add("tool.call", {"tool": "fs.write", "agent_id": "agent-lead-1",
                          "path": ".stackflowos/projects/p-1/workspace/src/main.py",
                          "status": "ok"}, source="agent-lead-1", project_id="p-1",
            age_s=2 * 3600 + 1500)
        add("tool.call", {"tool": "shell.run", "agent_id": "agent-lead-1",
                          "command": "python -m py_compile src/main.py", "status": "ok"},
            source="agent-lead-1", project_id="p-1", age_s=2 * 3600 + 1400)
        add("task.completed", {"task_id": "t-101", "title": "搭建项目骨架与入口", "status": "done"},
            source="agent-lead-1", project_id="p-1", age_s=2 * 3600 + 1300)
        add("unit.registered", {"work_id": "coder.2", "kind": "agent", "status": "online"},
            source="coder.2", project_id="p-1", age_s=2 * 3600 + 1200)
        add("agent.started", {"agent_id": "agent-coder-2", "role": "coder", "status": "spawning"},
            source="sys", project_id="p-1", age_s=2 * 3600 + 1190)
        add("lease.acquired", {"lease_id": "lease-4a91",
                               "resource": ".stackflowos/projects/p-1/workspace/src/snake.py",
                               "holder": "agent-coder-2"},
            source="agent-coder-2", project_id="p-1", age_s=2 * 3600 + 1100)
        add("task.started", {"task_id": "t-103", "title": "实现食物、计分与加速", "status": "doing"},
            source="agent-coder-2", project_id="p-1", age_s=2 * 3600 + 1000)
        add("project.status", {"project_id": "p-1", "status": "active"},
            source="sys", project_id="p-1", age_s=2 * 3600 + 900)
        add("chat.message", {"id": "msg-seed-1", "role": "assistant",
                             "text": "项目 **贪吃蛇终端版** 已创建（`p-1`）。"},
            source="assistant", project_id="p-1", age_s=2 * 3600 + 870)
        add("task.completed", {"task_id": "t-103", "title": "实现食物、计分与加速", "status": "done"},
            source="agent-coder-2", project_id="p-1", age_s=2 * 3600 + 700)
        add("project.created", {"project_id": "p-2", "name": "运维巡检报告",
                                "status": "planning", "by": "human:operator"},
            source="operator", project_id="p-2", age_s=2 * 3600 + 600)
        add("agent.started", {"agent_id": "agent-reviewer-3", "role": "reviewer",
                              "status": "spawning"}, source="sys", project_id="p-2",
            age_s=2 * 3600 + 550)
        add("unit.registered", {"work_id": "reviewer.3", "kind": "agent", "status": "online"},
            source="reviewer.3", project_id="p-2", age_s=2 * 3600 + 540)
        add("task.started", {"task_id": "t-202", "title": "写异常阈值判定", "status": "doing"},
            source="agent-reviewer-3", project_id="p-2", age_s=2 * 3600 + 500)
        add("agent.started", {"agent_id": "agent-tester-4", "role": "tester", "status": "spawning"},
            source="sys", project_id="p-2", age_s=2 * 3600 + 400)
        add("unit.registered", {"work_id": "tester.4", "kind": "agent", "status": "online",
                                "transport": "tcp"}, source="tester.4", project_id="p-2",
            age_s=2 * 3600 + 390)
        add("task.review", {"task_id": "t-205", "title": "评审报告模板与措辞", "status": "review"},
            source="agent-reviewer-3", project_id="p-2", age_s=2 * 3600 + 300)
        add("approval.requested", {"request_id": "apr-seed-1", "status": "pending",
                                   "principal": "agent-coder-2", "capability": "shell.run",
                                   "risk": "medium", "project_id": "p-1"},
            source="agent-coder-2", project_id="p-1", age_s=2 * 3600 + 250)
        add("approval.ui_decided", {"request_id": "apr-seed-1", "decision": "allow",
                                    "mode": "once", "by": "human:operator"},
            source="operator", project_id="p-1", age_s=2 * 3600 + 200)
        add("approval.ui_decided", {"request_id": "apr-seed-2", "decision": "deny",
                                    "mode": None, "by": "human:operator"},
            source="operator", project_id="p-2", age_s=1 * 3600 + 800)
        add("approval.ui_decided", {"request_id": "apr-seed-3", "decision": "allow",
                                    "mode": "session", "by": "human:operator"},
            source="operator", project_id="p-1", age_s=1 * 3600 + 200)
        add("rule.created", {"rule_id": "rule-6c1a2f00", "effect": "allow",
                             "capability": "data.read", "principal": "*"},
            source="operator", age_s=2 * 3600 + 150)
        add("project.progress", {"project_id": "p-1", "progress": 40,
                                 "counts": {"todo": 6, "doing": 2, "review": 0, "done": 4,
                                            "blocked": 0}},
            source="sys", project_id="p-1", age_s=2 * 3600 + 50)
        add("tool.call", {"tool": "fs.write", "agent_id": "agent-coder-2",
                          "path": ".stackflowos/projects/p-1/workspace/src/snake.py",
                          "status": "ok"}, source="agent-coder-2", project_id="p-1",
            age_s=2 * 3600 - 100)
        add("task.started", {"task_id": "t-104", "title": "加入障碍物与碰撞判定",
                             "status": "doing"}, source="agent-coder-2", project_id="p-1",
            age_s=2 * 3600 - 200)
        add("failure.retry", {"message": "worker heartbeat missed; retrying",
                              "work_id": "tester.4", "attempt": 1},
            source="tester.4", project_id="p-2", age_s=2 * 3600 - 300)
        add("unit.suspect", {"work_id": "tester.4", "status": "suspect"},
            source="sys", project_id="p-2", age_s=2 * 3600 - 340)
        add("task.review", {"task_id": "t-104", "title": "加入障碍物与碰撞判定",
                            "status": "review"}, source="agent-coder-2", project_id="p-1",
            age_s=2 * 3600 - 500)
        add("task.blocked", {"task_id": "t-108", "title": "处理终端尺寸变化的边界",
                             "status": "blocked"}, source="agent-coder-2", project_id="p-1",
            age_s=2 * 3600 - 600)
        add("rule.created", {"rule_id": "rule-11d8c4b2", "effect": "allow",
                             "capability": "shell.run", "principal": "agent-coder.*"},
            source="operator", age_s=2 * 3600 - 700)
        add("project.progress", {"project_id": "p-1", "progress": 56,
                                 "counts": {"todo": 3, "doing": 2, "review": 1, "done": 5,
                                            "blocked": 1}},
            source="sys", project_id="p-1", age_s=2 * 3600 - 800)
        add("approval.requested", {"request_id": "apr-7f2c91a3", "status": "pending",
                                   "principal": "agent-coder-2", "capability": "shell.run",
                                   "risk": "high", "project_id": "p-1"},
            source="agent-coder-2", project_id="p-1", age_s=70)
        add("approval.requested", {"request_id": "apr-9b3d2e14", "status": "pending",
                                   "principal": "agent-lead-1", "capability": "fs.write",
                                   "risk": "medium", "project_id": "p-1"},
            source="agent-lead-1", project_id="p-1", age_s=35)
        add("project.status", {"project_id": "p-2", "status": "review"},
            source="agent-lead-1", project_id="p-2", age_s=20)
        add("sys.config.updated", {"patch": {"security": {"approval_ttl_s": 300}},
                                   "restart_required": []},
            source="operator", age_s=10)

    # -- snapshot ---------------------------------------------------------
    def _active_lease_count(self) -> int:
        return sum(1 for lease in self.leases if lease.get("status") == "active")

    def _stats(self) -> dict:
        by_unit_status: dict[str, int] = {}
        for unit in self.units:
            by_unit_status[unit["status"]] = by_unit_status.get(unit["status"], 0) + 1
        by_agent_status: dict[str, int] = {}
        for agent in self.agents:
            by_agent_status[agent["status"]] = by_agent_status.get(agent["status"], 0) + 1
        active_projects = sum(1 for project in self.projects
                              if project["status"] in ("active", "review", "degraded"))
        pending = sum(1 for approval in self.approvals if approval["status"] == "pending")
        return {
            "units": {"total": len(self.units),
                      "online": by_unit_status.get("online", 0),
                      "by_status": by_unit_status,
                      "by_kind": {"agent": len(self.units)}},
            "agents": {"total": len(self.agents),
                       "active": sum(1 for agent in self.agents
                                     if agent["status"] not in ("done", "failed", "dead")),
                       "by_status": by_agent_status,
                       "by_role": {},
                       "max_workers": 6},
            "projects": len(self.projects),
            "active_projects": active_projects,
            "leases": self._active_lease_count(),
            "pending_approvals": pending,
            "events": self.last_seq,
            "uptime_s": int((now_ms() - self.started_at) / 1000),
        }

    def snapshot(self) -> dict:
        with self.lock:
            self._refresh_all_cards()
            stats = self._stats()
            info = {
                "name": "StackFlowOS",
                "version": "0.1.0",
                "host": "orion-7",
                "pid": 18210,
                "started_at": self.started_at,
                "transport": self.config["transport"]["mode"],
                "sys_rpc": self.config["transport"]["sys_rpc_address"],
                "sys_events": str(REPO_ROOT / ".stackflowos" / "run" / "sys-events.ipc"),
                "web_port": self.config["web"]["port"],
                "last_seq": self.last_seq,
                "stats": copy.deepcopy(stats),
            }
            return {
                "sys": info,
                "units": copy.deepcopy(self.units),
                "projects": copy.deepcopy(self.projects),
                "agents": copy.deepcopy(self.agents),
                "leases": copy.deepcopy([lease for lease in self.leases
                                         if lease.get("status") == "active"]),
                "approvals": copy.deepcopy([approval for approval in self.approvals
                                            if approval["status"] == "pending"]),
                "rules": copy.deepcopy([rule for rule in self.rules
                                        if rule.get("effect") in ("allow", "deny")]),
                "events": copy.deepcopy(self.events[-80:]),
                "chat": copy.deepcopy(self.conversation[-80:]),
                "config": copy.deepcopy(self.public_config()),
                "stats": copy.deepcopy(stats),
            }

    def public_config(self) -> dict:
        config = copy.deepcopy(self.config)
        api_key = config["llm"].get("api_key") or ""
        config["llm"]["api_key"] = "***" if api_key else ""
        return config

    # -- SSE --------------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        client: queue.Queue = queue.Queue(maxsize=4000)
        with self.lock:
            self.clients.append(client)
        return client

    def unsubscribe(self, client: queue.Queue) -> None:
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)

    def events_since(self, since: int, limit: int = 500) -> list[dict]:
        with self.lock:
            if since <= 0:
                return copy.deepcopy(self.events[-30:])
            return copy.deepcopy([event for event in self.events if event["seq"] > since][:limit])

    # -- chat -------------------------------------------------------------
    def chat_history(self, limit: int = 100) -> list[dict]:
        with self.lock:
            return copy.deepcopy(self.conversation[-limit:])

    def post_chat(self, text: str) -> dict:
        text = text.strip()
        if not text:
            raise Problem(400, "BAD_REQUEST", "text required")
        message = make_message("user", text, now_ms())
        with self.lock:
            self.conversation.append(message)
            self.conversation = self.conversation[-200:]
        self._emit("chat.message", message, source="user")
        threading.Thread(target=self._reply_later, args=(text,), daemon=True).start()
        return {"message_id": message["id"], "message": message}

    def _reply_later(self, text: str) -> None:
        time.sleep(0.4)
        reply = self._compose_reply(text)
        self._emit("chat.delta", {"message_id": "", "delta": "", "role": "assistant",
                                  "typing": True}, source="sys")
        time.sleep(0.25)
        chunks = [reply[i:i + 22] for i in range(0, len(reply), 22)]
        for chunk in chunks:
            if self.stop.is_set():
                return
            self._emit("chat.delta", {"message_id": "", "delta": chunk,
                                      "role": "assistant", "typing": False}, source="sys")
            time.sleep(0.045)
        message = make_message("assistant", reply, now_ms(), "agent-assistant")
        with self.lock:
            self.conversation.append(message)
            self.conversation = self.conversation[-200:]
        self._emit("chat.message", message, source="assistant")

    def _compose_reply(self, text: str) -> str:
        if any(word in text for word in ("审批", "approval", "pending")):
            pending = [approval for approval in self.approvals
                       if approval["status"] == "pending"]
            return (f"现在有 **{len(pending)}** 件操作在等审批。"
                    "打开「审批」可以允许一次、允许 1 小时或总是允许。")
        if any(word in text for word in ("进度", "状态", "在跑", "怎么样")):
            active = [project for project in self.projects
                      if project["status"] in ("active", "review", "degraded")]
            lines = [f"当前有 **{len(active)}** 个项目在推进："]
            for project in active:
                lines.append(
                    f"- **{project['name']}** `{project['id']}` · "
                    f"进度 {project['progress']}% · 进行中 "
                    f"{(project.get('counts') or {}).get('doing', 0)} / 待审 "
                    f"{(project.get('counts') or {}).get('review', 0)}")
            lines.append("")
            lines.append(f"在线单元 **{sum(1 for u in self.units if u['status'] == 'online')}** 个，"
                         f"待审批 **{sum(1 for a in self.approvals if a['status'] == 'pending')}** 件。")
            return "\n".join(lines)
        if any(word in text for word in ("创建", "新建", "做个", "做一个")):
            return ("收到。当前是演示夹具，不会真的创建新项目；"
                    "在真实后端里我会调用 `project.create` 并拆解任务。\n"
                    "你可以先在「项目」应用里体验创建流程。")
        return ("我在。这是 UI 夹具的助手回复，用来看流式气泡和排版效果。\n"
                "试试「现在的进度怎么样？」或「有哪些待审批？」。")

    # -- ambient ticker ---------------------------------------------------
    def start_ticker(self) -> None:
        self.ticker_thread = threading.Thread(target=self._tick_loop, daemon=True,
                                              name="ui-mock-ticker")
        self.ticker_thread.start()

    def _tick_loop(self) -> None:
        while not self.stop.wait(4.0):
            try:
                self.tick()
            except Exception as exc:  # keep the fixture alive
                print(f"[ui-mock] tick failed: {exc}", file=sys.stderr, flush=True)

    def tick(self) -> None:
        self.tick_count += 1
        replies = [
            "已核对任务依赖：`t-104` 可以开始，`t-106` 等接口稳定后再动。",
            "测试报告已回传，覆盖率 83%。需要我把它标记完成吗？",
            "工人 **Coder · 工人 2** 已完成 `snake.py` 的核心循环，正在补边界用例。",
            "有一条新的高风险操作在等审批：在本机运行 `pytest`。",
            "本轮巡检没有发现新的异常项，报告已写入工作区。",
        ]
        text = replies[self.tick_count % len(replies)]
        self._emit("chat.delta", {"message_id": "", "delta": "", "role": "assistant",
                                  "typing": True}, source="sys")
        time.sleep(0.18)
        for chunk in [text[i:i + 20] for i in range(0, len(text), 20)]:
            self._emit("chat.delta", {"message_id": "", "delta": chunk,
                                      "role": "assistant", "typing": False}, source="sys")
            time.sleep(0.05)
        message = make_message("assistant", text, now_ms(), "agent-assistant")
        with self.lock:
            self.conversation.append(message)
            self.conversation = self.conversation[-200:]
        self._emit("chat.message", message, source="assistant")
        self._advance_a_task()
        self._request_ambient_approval()

    def _advance_a_task(self) -> None:
        transitions = {"todo": "doing", "doing": "review", "review": "done"}
        with self.lock:
            for project_id in ("p-1", "p-2"):
                for task in self.tasks.get(project_id, []):
                    next_status = transitions.get(task["status"])
                    if not next_status:
                        continue
                    task["status"] = next_status
                    task["updated_at"] = now_ms()
                    if next_status == "done":
                        task["finished_at"] = now_ms()
                    if next_status == "doing" and not task.get("started_at"):
                        task["started_at"] = now_ms()
                    self._refresh_card(project_id)
                    project = next((item for item in self.projects
                                    if item["id"] == project_id), None)
                    if project:
                        project["updated_at"] = now_ms()
                    data = copy.deepcopy(task)
                    self._emit("task.updated", data, source="agent-coder-2",
                               project_id=project_id)
                    return

    def _request_ambient_approval(self) -> None:
        options = [
            ("shell.run", "high", {"command": "python -m pytest -q"},
             "运行测试套件，需要在本机执行 pytest。",
             "$ python -m pytest -q\n18 passed in 0.42s"),
            ("fs.write", "medium", {"path_prefix": str(REPO_ROOT / ".stackflowos/projects/p-1/workspace/src")},
             "更新 src/snake.py 的移动与碰撞逻辑。",
             "diff --git a/src/snake.py b/src/snake.py\n@@ -48,7 +48,9 @@\n+        self.score += 1"),
            ("network.connect", "medium", {"host": "127.0.0.1", "port": 8081},
             "向本地报告服务提交巡检结果。",
             "POST http://127.0.0.1:8081/report\n{\"status\":\"ok\",\"items\":18}"),
        ]
        capability, risk, scope, reason, preview = options[self.tick_count % len(options)]
        with self.lock:
            pending = [approval for approval in self.approvals
                       if approval["status"] == "pending"]
            if len(pending) >= 4:
                oldest = pending[0]
                oldest["status"] = "expired"
                self._emit("approval.expired", {"request_id": oldest["id"],
                                                "status": "expired"},
                           source="sys", project_id=oldest.get("project_id"))
            approval = {
                "id": new_id("apr"),
                "principal": "agent-coder-2",
                "capability": capability,
                "scope": {**scope, "project_id": "p-1"},
                "risk": risk,
                "reason": reason,
                "preview": preview,
                "project_id": "p-1",
                "task_id": "t-105",
                "status": "pending",
                "mode": None,
                "grant_id": None,
                "created_at": now_ms(),
                "expires_at": now_ms() + 300_000,
                "decided_at": None,
                "decided_by": None,
                "note": "",
            }
            self.approvals.append(approval)
            self._refresh_card("p-1")
        self._emit("approval.requested",
                   {"request_id": approval["id"], "status": "pending",
                    "principal": approval["principal"], "capability": capability,
                    "risk": risk, "scope": approval["scope"],
                    "request": copy.deepcopy(approval)},
                   source="agent-coder-2", project_id="p-1")

    # -- mutations --------------------------------------------------------
    def decide_approval(self, request_id: str, decision: str, mode: str, note: str) -> dict:
        if decision not in ("allow", "deny"):
            raise Problem(400, "BAD_REQUEST", "decision must be allow|deny")
        if mode not in ("once", "session", "always"):
            raise Problem(400, "BAD_REQUEST", "mode must be once|session|always")
        with self.lock:
            approval = next((item for item in self.approvals
                             if item["id"] == request_id), None)
            if approval is None:
                raise Problem(404, "NOT_FOUND", f"unknown approval request: {request_id!r}")
            if approval["status"] != "pending":
                raise Problem(409, "CONFLICT",
                              f"approval {request_id!r} is already {approval['status']!r}")
            approval["status"] = "approved" if decision == "allow" else "denied"
            approval["mode"] = mode if decision == "allow" else None
            approval["decided_at"] = now_ms()
            approval["decided_by"] = "human:operator"
            approval["note"] = note
            result = copy.deepcopy(approval)
            if decision == "allow":
                grant = {
                    "id": new_id("rule"),
                    "effect": "allow",
                    "capability": approval["capability"],
                    "principal": approval["principal"],
                    "scope": copy.deepcopy(approval["scope"]),
                    "risk_max": approval["risk"],
                    "expires_at": (now_ms() + (3600 if mode == "session" else 300) * 1000)
                    if mode in ("once", "session") else None,
                    "uses_left": 1 if mode == "once" else None,
                    "note": f"approval {request_id} ({mode})",
                    "source": "grant" if mode in ("once", "session") else "rule",
                    "created_by": "human:operator",
                    "created_at": now_ms(),
                }
                self.rules.append(grant)
                self._emit("rule.created", {"rule_id": grant["id"], "effect": "allow",
                                            "capability": grant["capability"],
                                            "principal": grant["principal"],
                                            "source": grant["source"]},
                           source="operator", project_id=approval.get("project_id"))
            self._refresh_card(approval.get("project_id") or "")
        self._emit("approval.decided",
                   {"request_id": request_id, "status": approval["status"],
                    "decision": decision, "mode": request_mode(mode, decision),
                    "by": "human:operator", "request": result},
                   source="operator", project_id=approval.get("project_id"))
        self._emit("approval.ui_decided",
                   {"request_id": request_id, "decision": decision,
                    "mode": request_mode(mode, decision), "by": "human:operator"},
                   source="operator", project_id=approval.get("project_id"))
        return result

    def create_project(self, name: str, goal: str) -> dict:
        with self.lock:
            index = len(self.projects) + 1
            project_id = f"p-{index}"
            while any(project["id"] == project_id for project in self.projects):
                index += 1
                project_id = f"p-{index}"
            project = {
                "id": project_id,
                "name": name or goal[:24] or "未命名项目",
                "goal": goal or name,
                "status": "active",
                "progress": 0,
                "counts": {},
                "current_task": None,
                "total_tasks": 0,
                "updated_at": now_ms(),
                "brief": "",
                "workspace": str(REPO_ROOT / ".stackflowos/projects" / project_id / "workspace"),
                "tags": [],
                "color": "#7c5cff",
                "degraded": False,
                "archived_at": None,
                "agents": [],
                "pending_approvals": 0,
                "leases": [],
                "milestones": 2,
            }
            self.projects.append(project)
            self.tasks[project_id] = [
                make_task(f"t-{project_id[2:]}01", project_id, "拆解目标并确认验收标准",
                          status="doing", priority=2, role="lead", age_s=5),
                make_task(f"t-{project_id[2:]}02", project_id, "搭建项目骨架",
                          priority=1, role="architect", age_s=4),
                make_task(f"t-{project_id[2:]}03", project_id, "实现核心功能",
                          priority=2, role="coder", age_s=3),
                make_task(f"t-{project_id[2:]}04", project_id, "补测试与运行说明",
                          priority=0, role="tester", age_s=2),
            ]
            self._refresh_card(project_id)
            card = copy.deepcopy(project)
        self._emit("project.created", {"project_id": project_id, "name": card["name"],
                                       "status": "active", "by": "human:operator"},
                   source="operator", project_id=project_id)
        return card

    def patch_project(self, project_id: str, patch: dict) -> dict:
        with self.lock:
            project = self._project(project_id)
            if patch.get("status") == "done":
                return self.complete_project(project_id)
            for key in ("name", "goal", "status", "brief", "tags", "color"):
                if key in patch:
                    project[key] = copy.deepcopy(patch[key])
            project["updated_at"] = now_ms()
            self._refresh_card(project_id)
            card = copy.deepcopy(project)
        self._emit("project.updated", {"project_id": project_id,
                                       "status": card["status"],
                                       "name": card["name"]},
                   source="operator", project_id=project_id)
        return card

    def archive_project(self, project_id: str) -> dict:
        stopped = 0
        with self.lock:
            project = self._project(project_id)
            project["status"] = "archived"
            project["archived_at"] = now_ms()
            project["updated_at"] = now_ms()
            for agent in self.agents:
                if agent.get("project_id") == project_id and agent["status"] not in ("dead", "failed"):
                    agent["status"] = "dead"
                    stopped += 1
            for unit in self.units:
                if unit.get("project_id") == project_id and unit["status"] == "online":
                    unit["status"] = "offline"
            self._refresh_card(project_id)
            card = copy.deepcopy(project)
        self._emit("project.archived", {"project_id": project_id, "agents_stopped": stopped},
                   source="operator", project_id=project_id)
        return {"project": card, "agents_stopped": stopped}

    def complete_project(self, project_id: str) -> dict:
        with self.lock:
            project = self._project(project_id)
            if project["status"] not in ("review", "active"):
                raise Problem(409, "CONFLICT",
                              f"project {project_id} is {project['status']}")
            for task in self.tasks.get(project_id, []):
                if task["status"] == "review":
                    task["status"] = "done"
                    task["finished_at"] = now_ms()
                    task["updated_at"] = now_ms()
            project["status"] = "done"
            project["updated_at"] = now_ms()
            for agent in self.agents:
                if agent.get("project_id") == project_id and agent["status"] not in ("dead", "failed"):
                    agent["status"] = "dead"
            self._refresh_card(project_id)
            card = copy.deepcopy(project)
        self._emit("project.completed", {"project_id": project_id, "by": "human:operator"},
                   source="operator", project_id=project_id)
        return card

    def create_task(self, project_id: str, body: dict) -> dict:
        title = str(body.get("title") or "").strip()
        if not title:
            raise Problem(400, "BAD_REQUEST", "title required")
        try:
            priority = int(body.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        with self.lock:
            self._project(project_id)
            task = make_task(new_id("t"), project_id, title,
                             description=str(body.get("description") or ""),
                             priority=priority, role=body.get("role"), age_s=0)
            self.tasks.setdefault(project_id, []).append(task)
            self._refresh_card(project_id)
            result = copy.deepcopy(task)
        self._emit("task.created", result, source="operator", project_id=project_id)
        return result

    def patch_task(self, task_id: str, patch: dict) -> dict:
        transitions = {
            "todo": {"doing", "blocked"},
            "doing": {"review", "blocked", "todo"},
            "review": {"done", "todo", "blocked"},
            "blocked": {"todo", "doing"},
            "done": set(),
        }
        with self.lock:
            task, project_id = self._task(task_id)
            if "status" in patch:
                new_status = str(patch["status"])
                if new_status not in transitions.get(task["status"], set()):
                    raise Problem(400, "VALIDATION",
                                  f"illegal task transition: {task['status']} -> {new_status}")
                task["status"] = new_status
                if new_status == "doing" and not task.get("started_at"):
                    task["started_at"] = now_ms()
                if new_status == "done":
                    task["finished_at"] = now_ms()
            for key in ("title", "description", "priority", "assignee", "role", "error"):
                if key in patch:
                    task[key] = patch[key]
            task["updated_at"] = now_ms()
            project = self._project(project_id)
            project["updated_at"] = now_ms()
            self._refresh_card(project_id)
            result = copy.deepcopy(task)
        self._emit("task.updated", result, source="operator", project_id=project_id)
        return result

    def add_task_note(self, task_id: str, text: str) -> dict:
        with self.lock:
            task, project_id = self._task(task_id)
            task["notes"].append({"ts": now_ms(), "author": "human:operator",
                                  "text": str(text or "")})
            task["updated_at"] = now_ms()
            self._refresh_card(project_id)
            result = copy.deepcopy(task)
        self._emit("task.updated", result, source="operator", project_id=project_id)
        return result

    def check_task(self, task_id: str, item_id: str | None, text: str | None,
                   done: bool, note: str) -> dict:
        with self.lock:
            task, project_id = self._task(task_id)
            checks = task.setdefault("checks", [])
            target = None
            if item_id:
                target = next((item for item in checks if item.get("id") == item_id), None)
            elif text:
                target = next((item for item in checks if item.get("text") == text), None)
            if target is None:
                target = {"id": new_id("chk"), "text": text or "验收项",
                          "done": False, "note": ""}
                checks.append(target)
            target["done"] = bool(done)
            if note:
                target["note"] = str(note)
            task["updated_at"] = now_ms()
            self._refresh_card(project_id)
            result = copy.deepcopy(task)
        self._emit("task.updated", result, source="operator", project_id=project_id)
        return result

    def create_rule(self, body: dict) -> dict:
        capability = str(body.get("capability") or "").strip()
        if not capability:
            raise Problem(400, "BAD_REQUEST", "capability required")
        with self.lock:
            rule = {
                "id": new_id("rule"),
                "effect": str(body.get("effect") or "allow"),
                "capability": capability,
                "principal": str(body.get("principal") or "*"),
                "scope": copy.deepcopy(body.get("scope") or {}),
                "risk_max": str(body.get("risk_max") or "high"),
                "expires_at": body.get("expires_at"),
                "uses_left": body.get("uses_left"),
                "note": str(body.get("note") or ""),
                "source": "rule",
                "created_by": "human:operator",
                "created_at": now_ms(),
            }
            self.rules.append(rule)
            result = copy.deepcopy(rule)
        self._emit("rule.created", {"rule_id": result["id"], "effect": result["effect"],
                                    "capability": capability,
                                    "principal": result["principal"]},
                   source="operator")
        return result

    def delete_rule(self, rule_id: str) -> dict:
        with self.lock:
            before = len(self.rules)
            self.rules = [rule for rule in self.rules if rule["id"] != rule_id]
            deleted = len(self.rules) != before
        if deleted:
            self._emit("rule.deleted", {"rule_id": rule_id}, source="operator")
        return {"deleted": deleted}

    def kill_unit(self, work_id: str) -> dict:
        with self.lock:
            unit = next((item for item in self.units if item["work_id"] == work_id), None)
            if unit is None:
                raise Problem(404, "NOT_FOUND", f"unknown unit: {work_id!r}")
            unit["status"] = "offline"
            unit["last_seen"] = now_ms()
            for agent in self.agents:
                if agent.get("unit_id") == work_id and agent["status"] not in ("dead", "failed"):
                    agent["status"] = "dead"
            self._refresh_all_cards()
        self._emit("unit.killed", {"work_id": work_id, "reason": "operator_requested"},
                   source="operator", project_id=unit.get("project_id"))
        return {}

    def spawn_agent(self, role: str, project_id: str | None, task_id: str | None) -> dict:
        role = str(role or "").strip()
        if not role:
            raise Problem(400, "BAD_REQUEST", "role required")
        with self.lock:
            self.spawn_count += 1
            number = self.spawn_count
            agent_id = new_id("agent")
            unit_id = f"{role}.{number}"
            agent = {
                "id": agent_id,
                "role": role,
                "project_id": project_id or None,
                "task_id": task_id,
                "unit_id": unit_id,
                "status": "idle",
                "model": self.config["llm"]["provider"],
                "display_name": f"{role.title()} · 工人 {number}",
                "created_at": now_ms(),
                "last_seen": now_ms(),
                "stats": {},
                "summary": "刚上岗，等待第一个任务。",
                "error": None,
            }
            self.agents.append(agent)
            unit = {
                "work_id": unit_id,
                "kind": "agent",
                "label": agent["display_name"],
                "project_id": project_id or None,
                "pid": 18500 + number,
                "host": "orion-7",
                "transport": self.config["transport"]["mode"],
                "rpc_address": f"ipc://{REPO_ROOT / '.stackflowos/run'}/{unit_id}-rpc.ipc",
                "events_address": f"ipc://{REPO_ROOT / '.stackflowos/run'}/{unit_id}-events.ipc",
                "status": "online",
                "abilities": [{"name": "fs.write", "tool": "fs", "scope": "project"}],
                "hwinfo": {"platform": "Linux-6.8.0-x86_64", "python": "3.12.3",
                           "cpu_count": 16, "hostname": "orion-7"},
                "started_at": now_ms(),
                "last_seen": now_ms(),
                "restart_count": 0,
                "metadata": {"role": role, "project": project_id or ""},
            }
            self.units.append(unit)
            self._refresh_all_cards()
            result = copy.deepcopy(agent)
        self._emit("unit.registered", {"work_id": unit_id, "kind": "agent", "status": "online"},
                   source=unit_id, project_id=project_id)
        self._emit("agent.started", {"agent_id": agent_id, "role": role, "status": "idle",
                                     "project_id": project_id},
                   source="sys", project_id=project_id)
        return result

    def kill_agent(self, agent_id: str) -> dict:
        with self.lock:
            agent = next((item for item in self.agents if item["id"] == agent_id), None)
            if agent is None:
                raise Problem(404, "NOT_FOUND", f"unknown agent: {agent_id!r}")
            agent["status"] = "dead"
            self._refresh_all_cards()
            project_id = agent.get("project_id")
        self._emit("agent.exited", {"agent_id": agent_id, "reason": "operator_requested"},
                   source="operator", project_id=project_id)
        return {"ok": True}

    def update_config(self, patch: dict) -> dict:
        if not isinstance(patch, dict) or not patch:
            raise Problem(400, "BAD_REQUEST", "config patch required")
        with self.lock:
            previous = copy.deepcopy(self.config)
            for section, values in patch.items():
                if section not in self.config:
                    raise Problem(400, "CONFIG", f"unknown config section: {section}")
                if not isinstance(values, dict):
                    raise Problem(400, "CONFIG", f"{section} must be an object")
                self.config[section].update(copy.deepcopy(values))
            if self.config["llm"].get("api_key"):
                self.config["llm"]["api_key"] = "***"
            restart_required = []
            if self.config["transport"]["mode"] != previous["transport"]["mode"]:
                restart_required.append("transport.mode")
            if self.config["web"]["port"] != previous["web"]["port"]:
                restart_required.append("web.port")
            config = copy.deepcopy(self.public_config())
        self._emit("sys.config.updated", {"patch": patch,
                                          "restart_required": restart_required},
                   source="operator")
        return {"config": config, "restart_required": restart_required}

    def console(self, types: list[str], project_id: str | None, limit: int) -> list[dict]:
        with self.lock:
            events = list(self.events)
            if project_id:
                events = [event for event in events if event.get("project_id") == project_id]
            if types:
                events = [
                    event for event in events
                    if event["type"] in types
                    or any(event["type"].startswith(f"{prefix}.") for prefix in types)
                ]
            return copy.deepcopy(events[-limit:])

    # -- small helpers ----------------------------------------------------
    def _project(self, project_id: str) -> dict:
        project = next((item for item in self.projects if item["id"] == project_id), None)
        if project is None:
            raise Problem(404, "NOT_FOUND", f"unknown project: {project_id!r}")
        return project

    def _task(self, task_id: str) -> tuple[dict, str]:
        for project_id, tasks in self.tasks.items():
            for task in tasks:
                if task["id"] == task_id:
                    return task, project_id
        raise Problem(404, "NOT_FOUND", f"unknown task: {task_id!r}")

    def stop_all(self) -> None:
        self.stop.set()


def request_mode(mode: str, decision: str) -> str | None:
    return mode if decision == "allow" else None


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    fixture: Fixture
    protocol_version = "HTTP/1.0"
    server_version = "StackFlowOS-UI-Mock/0.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib API
        # Keep the console clean; only log real problems.
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            print(f"[ui-mock] {self.address_string()} {fmt % args}", file=sys.stderr, flush=True)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def _handle(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = {key: values[-1] if values else ""
                 for key, values in parse_qs(parsed.query).items()}
        if path == "/api/events/stream":
            self._serve_sse(query)
            return
        if path.startswith("/api/"):
            try:
                body = self._read_json_body()
                status, payload = self._dispatch_api(method, path, query, body)
            except Problem as problem:
                status, payload = problem.status, {
                    "ok": False,
                    "error": {"code": problem.code, "message": problem.message,
                              "detail": problem.detail},
                }
            except Exception as exc:  # pragma: no cover - fixture safety net
                status, payload = 500, {"ok": False, "error": {
                    "code": "INTERNAL", "message": str(exc)}}
            self._send_json(status, payload)
            return
        if method in ("GET", "HEAD"):
            self._serve_static(path, head=method == "HEAD")
        else:
            self._send_json(405, {"ok": False, "error": {
                "code": "METHOD_NOT_ALLOWED", "message": "method not allowed"}})

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Problem(400, "BAD_REQUEST", f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise Problem(400, "BAD_REQUEST", "JSON body must be an object")
        return payload

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _serve_static(self, path: str, *, head: bool = False) -> None:
        if path in ("", "/"):
            path = "/index.html"
        relative = path.lstrip("/")
        candidate = (ASSETS_DIR / relative).resolve()
        try:
            candidate.relative_to(ASSETS_DIR.resolve())
        except ValueError:
            self._send_json(403, {"ok": False, "error": {
                "code": "DENIED", "message": "forbidden"}})
            return
        if not candidate.is_file():
            if "." not in Path(path).name:
                candidate = ASSETS_DIR / "index.html"
            if not candidate.is_file():
                self._send_json(404, {"ok": False, "error": {
                    "code": "NOT_FOUND", "message": "not found"}})
                return
        try:
            data = candidate.read_bytes()
        except OSError:
            self._send_json(404, {"ok": False, "error": {
                "code": "NOT_FOUND", "message": "not found"}})
            return
        content_type = CONTENT_TYPES.get(candidate.suffix.lower())
        if content_type is None:
            content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _serve_sse(self, query: dict) -> None:
        try:
            since = int(query.get("since") or 0)
        except ValueError:
            since = 0
        client = self.fixture.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 2000\n\n")
            for event in self.fixture.events_since(since):
                self.wfile.write(b"data: " + json.dumps(event, ensure_ascii=False).encode()
                                 + b"\n\n")
            self.wfile.flush()
            while not self.fixture.stop.is_set():
                try:
                    event = client.get(timeout=HEARTBEAT_S)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: " + json.dumps(event, ensure_ascii=False).encode()
                                 + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.fixture.unsubscribe(client)

    # -- API --------------------------------------------------------------
    def _dispatch_api(self, method: str, path: str, query: dict, body: dict):
        fixture = self.fixture
        routes = [
            ("GET", r"/api/health"),
            ("GET", r"/api/state"),
            ("GET", r"/api/events"),
            ("GET", r"/api/console"),
            ("GET", r"/api/chat"),
            ("POST", r"/api/chat"),
            ("GET", r"/api/projects"),
            ("POST", r"/api/projects"),
            ("GET", r"/api/projects/([^/]+)"),
            ("PATCH", r"/api/projects/([^/]+)"),
            ("POST", r"/api/projects/([^/]+)/archive"),
            ("POST", r"/api/projects/([^/]+)/complete"),
            ("GET", r"/api/projects/([^/]+)/tasks"),
            ("POST", r"/api/projects/([^/]+)/tasks"),
            ("PATCH", r"/api/tasks/([^/]+)"),
            ("POST", r"/api/tasks/([^/]+)/note"),
            ("POST", r"/api/tasks/([^/]+)/check"),
            ("GET", r"/api/approvals"),
            ("POST", r"/api/approvals/([^/]+)/decide"),
            ("GET", r"/api/rules"),
            ("POST", r"/api/rules"),
            ("DELETE", r"/api/rules/([^/]+)"),
            ("GET", r"/api/units"),
            ("POST", r"/api/units/([^/]+)/kill"),
            ("GET", r"/api/agents"),
            ("POST", r"/api/agents"),
            ("DELETE", r"/api/agents/([^/]+)"),
            ("GET", r"/api/config"),
            ("PATCH", r"/api/config"),
            ("GET", r"/api/leases"),
        ]
        match = None
        for route_method, pattern in routes:
            if route_method != method:
                continue
            found = re.fullmatch(pattern, path)
            if found:
                match = found
                break
        if match is None:
            return 404, {"ok": False, "error": {
                "code": "NOT_FOUND", "message": f"no route {method} {path}"}}

        # health
        if path == "/api/health":
            return 200, {"ok": True, "data": {"status": "ok", "version": "0.1.0",
                                              "uptime_s": int((now_ms() - fixture.started_at) / 1000)}}
        if path == "/api/state":
            return 200, {"ok": True, "data": fixture.snapshot()}
        if path == "/api/events":
            try:
                since = int(query.get("since") or 0)
            except ValueError:
                since = 0
            events = fixture.events_since(since, limit=500)
            return 200, {"ok": True, "data": {"events": events, "last_seq": fixture.last_seq}}
        if path == "/api/console":
            types = [item.strip() for item in str(query.get("types") or "").split(",")
                     if item.strip()]
            try:
                limit = min(int(query.get("limit") or 200), 1000)
            except ValueError:
                limit = 200
            rows = fixture.console(types, query.get("project_id") or None, limit)
            return 200, {"ok": True, "data": {"events": rows}}
        if path == "/api/chat" and method == "GET":
            try:
                limit = min(int(query.get("limit") or 100), 200)
            except ValueError:
                limit = 100
            return 200, {"ok": True, "data": {"messages": fixture.chat_history(limit)}}
        if path == "/api/chat" and method == "POST":
            return 202, {"ok": True, "data": fixture.post_chat(str(body.get("text") or ""))}
        if path == "/api/projects" and method == "GET":
            with fixture.lock:
                fixture._refresh_all_cards()
                projects = copy.deepcopy(fixture.projects)
            return 200, {"ok": True, "data": {"projects": projects}}
        if path == "/api/projects" and method == "POST":
            name = str(body.get("name") or "").strip()
            goal = str(body.get("goal") or "").strip()
            card = fixture.create_project(name or goal[:40] or "未命名项目", goal or name)
            return 201, {"ok": True, "data": card}
        if match.group(0).startswith("/api/projects/") and path.endswith("/tasks") and method == "GET":
            project_id = match.group(1)
            with fixture.lock:
                fixture._project(project_id)
                tasks = copy.deepcopy(fixture.tasks.get(project_id, []))
            return 200, {"ok": True, "data": {"tasks": tasks}}
        if path.endswith("/tasks") and method == "POST":
            project_id = match.group(1)
            return 201, {"ok": True, "data": fixture.create_task(project_id, body)}
        if path.endswith("/archive"):
            return 200, {"ok": True, "data": fixture.archive_project(match.group(1))}
        if path.endswith("/complete"):
            return 200, {"ok": True, "data": fixture.complete_project(match.group(1))}
        if path.startswith("/api/projects/") and method == "GET":
            with fixture.lock:
                fixture._refresh_all_cards()
                project = copy.deepcopy(fixture._project(match.group(1)))
            return 200, {"ok": True, "data": project}
        if path.startswith("/api/projects/") and method == "PATCH":
            patch = body.get("patch") if isinstance(body.get("patch"), dict) else body
            return 200, {"ok": True, "data": fixture.patch_project(match.group(1), patch)}
        if path.startswith("/api/tasks/") and path.endswith("/note"):
            return 200, {"ok": True, "data": fixture.add_task_note(
                match.group(1), str(body.get("text") or ""))}
        if path.startswith("/api/tasks/") and path.endswith("/check"):
            return 200, {"ok": True, "data": fixture.check_task(
                match.group(1), body.get("item_id"), body.get("text"),
                bool(body.get("done", True)), str(body.get("note") or ""))}
        if path.startswith("/api/tasks/") and method == "PATCH":
            patch = body.get("patch") if isinstance(body.get("patch"), dict) else body
            return 200, {"ok": True, "data": fixture.patch_task(match.group(1), patch)}
        if path == "/api/approvals" and method == "GET":
            status = query.get("status") or None
            with fixture.lock:
                items = [copy.deepcopy(item) for item in fixture.approvals
                         if status is None or item["status"] == status]
            return 200, {"ok": True, "data": {"approvals": items}}
        if path.startswith("/api/approvals/") and path.endswith("/decide"):
            return 200, {"ok": True, "data": fixture.decide_approval(
                match.group(1), str(body.get("decision") or ""),
                str(body.get("mode") or "once"), str(body.get("note") or ""))}
        if path == "/api/rules" and method == "GET":
            with fixture.lock:
                rules = copy.deepcopy(fixture.rules)
            return 200, {"ok": True, "data": {"rules": rules}}
        if path == "/api/rules" and method == "POST":
            return 201, {"ok": True, "data": fixture.create_rule(body)}
        if path.startswith("/api/rules/") and method == "DELETE":
            return 200, {"ok": True, "data": fixture.delete_rule(match.group(1))}
        if path == "/api/units" and method == "GET":
            with fixture.lock:
                units = copy.deepcopy(fixture.units)
            return 200, {"ok": True, "data": {"units": units}}
        if path.startswith("/api/units/") and path.endswith("/kill"):
            return 200, {"ok": True, "data": fixture.kill_unit(match.group(1))}
        if path == "/api/agents" and method == "GET":
            project_id = query.get("project_id") or None
            status = query.get("status") or None
            with fixture.lock:
                agents = [copy.deepcopy(agent) for agent in fixture.agents
                          if (project_id is None or agent.get("project_id") == project_id)
                          and (status is None or agent["status"] == status)]
            return 200, {"ok": True, "data": {"agents": agents}}
        if path == "/api/agents" and method == "POST":
            return 201, {"ok": True, "data": fixture.spawn_agent(
                str(body.get("role") or ""), body.get("project_id"), body.get("task_id"))}
        if path.startswith("/api/agents/") and method == "DELETE":
            return 200, {"ok": True, "data": fixture.kill_agent(match.group(1))}
        if path == "/api/config" and method == "GET":
            return 200, {"ok": True, "data": fixture.public_config()}
        if path == "/api/config" and method == "PATCH":
            patch = body.get("patch") if isinstance(body.get("patch"), dict) else body
            return 200, {"ok": True, "data": fixture.update_config(patch)}
        if path == "/api/leases" and method == "GET":
            with fixture.lock:
                leases = copy.deepcopy([lease for lease in fixture.leases
                                        if lease.get("status") == "active"])
            return 200, {"ok": True, "data": {"leases": leases}}
        return 404, {"ok": False, "error": {
            "code": "NOT_FOUND", "message": f"no route {method} {path}"}}


def pick_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    raise SystemExit(f"no free port in {preferred}..{preferred + 99}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="StackFlowOS UI fixture server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="preferred port (falls back to the next free one)")
    parser.add_argument("--no-ticker", action="store_true",
                        help="disable the ambient 4s SSE sequence")
    args = parser.parse_args(argv)

    fixture = Fixture()
    Handler.fixture = fixture
    port = pick_port(args.host, args.port)
    server = ThreadingHTTPServer((args.host, port), Handler)
    server.daemon_threads = True
    if not args.no_ticker:
        fixture.start_ticker()

    print(f"[ui-mock] StackFlowOS UI fixture on http://{args.host}:{port}/", flush=True)
    print(f"[ui-mock] assets: {ASSETS_DIR}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        fixture.stop_all()
        server.shutdown()
        server.server_close()
        print("[ui-mock] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
