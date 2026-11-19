"""Deterministic, offline planning for the lead agent and demo seeding.

The planner is intentionally dumb and reproducible: keyword matching picks one
of four recipes (software build, research, ops, fallback) and expands it into
4–7 actionable tasks with roles from docs/architecture.md §8.1. There is no
randomness, no network and no LLM here, so the same goal always produces the
same plan — tests and demos can rely on that.

The output is plain data (:class:`PlannedTask`) consumed by
``ProjectStore.add_task``; the store itself never calls the planner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Roles used by the recipes (architecture §8.1).
ROLE_ARCHITECT = "architect"
ROLE_CODER = "coder"
ROLE_TESTER = "tester"
ROLE_RESEARCHER = "researcher"
ROLE_REVIEWER = "reviewer"

_SOFTWARE_KEYWORDS = (
    "software", "build", "code", "coding", "program", "app", "application",
    "game", "tool", "website", "web", "implement", "develop", "development",
    "feature", "api", "service", "系统", "软件", "程序", "代码", "开发",
    "实现", "构建", "工具", "网站", "游戏", "应用", "功能", "平台",
)
_RESEARCH_KEYWORDS = (
    "research", "investigate", "analysis", "analyze", "analyse", "report",
    "study", "survey", "compare", "evaluation", "调研", "研究", "分析",
    "报告", "调查", "评估", "对比", "资料", "论证",
)
_OPS_KEYWORDS = (
    "ops", "operation", "operations", "deploy", "deployment", "operate",
    "fix", "repair", "maintain", "maintenance", "incident", "bug", "release",
    "rollout", "部署", "运维", "修复", "故障", "维护", "上线", "发布",
    "排障", "维修", "回归",
)

_MILESTONES = {
    "software": ["需求与设计", "实现", "验证与交付"],
    "research": ["调研", "结论与评审"],
    "ops": ["诊断", "修复", "回归验证"],
    "fallback": ["目标与方案", "执行", "验证"],
}


@dataclass
class PlannedTask:
    """One task proposed by the planner (not yet persisted)."""

    title: str
    description: str
    role: str
    priority: int = 0
    milestone: str | None = None

    def to_dict(self) -> dict:
        """Return a JSON-serialisable copy for seeding scripts."""
        return {
            "title": self.title,
            "description": self.description,
            "role": self.role,
            "priority": self.priority,
            "milestone": self.milestone,
        }


def _keyword_hit(haystack: str, keyword: str) -> bool:
    """Match English keywords on word boundaries, Chinese as substrings."""
    if keyword.isascii():
        pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?:s|es|ed|ing)?(?![a-z0-9])"
        return re.search(pattern, haystack) is not None
    return keyword in haystack


def _score(haystack: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for keyword in keywords if _keyword_hit(haystack, keyword))


def _family(goal: str, title: str = "") -> str:
    """Pick a recipe family; ties prefer software, then research, then ops."""
    haystack = f"{title} {goal}".strip().lower()
    if not haystack:
        return "fallback"
    scores = {
        "software": _score(haystack, _SOFTWARE_KEYWORDS),
        "research": _score(haystack, _RESEARCH_KEYWORDS),
        "ops": _score(haystack, _OPS_KEYWORDS),
    }
    best = "fallback"
    best_score = 0
    for family in ("software", "research", "ops"):
        if scores[family] > best_score:
            best = family
            best_score = scores[family]
    return best


def milestones_for(goal: str) -> list[str]:
    """Return the milestone names the recipe for ``goal`` will use."""
    return list(_MILESTONES[_family(goal)])


def _subject(goal: str, title: str) -> str:
    return (title or "").strip() or (goal or "").strip() or "未命名目标"


def _software_plan(subject: str, goal: str, milestones: list[str]) -> list[PlannedTask]:
    return [
        PlannedTask(
            "需求与边界",
            f"梳理「{subject}」的目标、用户场景与验收边界，产出需求说明。",
            ROLE_ARCHITECT, 3, milestones[0],
        ),
        PlannedTask(
            "架构设计",
            f"为「{subject}」设计模块划分、数据形状与关键接口，写明取舍。",
            ROLE_ARCHITECT, 2, milestones[0],
        ),
        PlannedTask(
            "核心实现",
            f"按设计实现「{subject}」的核心功能，保持小步提交与可回滚。",
            ROLE_CODER, 2, milestones[1],
        ),
        PlannedTask(
            "测试与自检",
            f"为「{subject}」补上可执行测试并跑通，记录结果与失败边界。",
            ROLE_TESTER, 1, milestones[2],
        ),
        PlannedTask(
            "文档",
            f"补齐「{subject}」的使用说明与实现要点，方便后续维护。",
            ROLE_CODER, 1, milestones[2],
        ),
        PlannedTask(
            "评审",
            f"对照需求评审「{subject}」的实现与文档，给出通过或打回结论。",
            ROLE_REVIEWER, 0, milestones[2],
        ),
    ]


def _research_plan(subject: str, goal: str, milestones: list[str]) -> list[PlannedTask]:
    return [
        PlannedTask(
            "资料收集",
            f"围绕「{subject}」收集一手资料与可信来源，记录链接和摘要。",
            ROLE_RESEARCHER, 3, milestones[0],
        ),
        PlannedTask(
            "关键问题梳理",
            f"把「{subject}」拆成关键问题与判断标准，标出未知项。",
            ROLE_RESEARCHER, 2, milestones[0],
        ),
        PlannedTask(
            "结论报告",
            f"基于证据写出「{subject}」的结论报告，区分事实、推断与建议。",
            ROLE_RESEARCHER, 2, milestones[1],
        ),
        PlannedTask(
            "评审",
            f"复核「{subject}」报告的证据链与结论，给出通过或补证意见。",
            ROLE_REVIEWER, 1, milestones[1],
        ),
    ]


def _ops_plan(subject: str, goal: str, milestones: list[str]) -> list[PlannedTask]:
    return [
        PlannedTask(
            "现状检查",
            f"检查「{subject}」当前运行状态、日志与复现步骤，固定证据。",
            ROLE_TESTER, 3, milestones[0],
        ),
        PlannedTask(
            "修复方案",
            f"针对「{subject}」给出最小修复方案与回滚路径，先评审再动手。",
            ROLE_CODER, 2, milestones[1],
        ),
        PlannedTask(
            "执行与验证",
            f"执行「{subject}」的修复并验证关键路径，记录前后对比。",
            ROLE_TESTER, 2, milestones[1],
        ),
        PlannedTask(
            "回归",
            f"对「{subject}」相关路径做回归检查，确认没有引入新问题。",
            ROLE_REVIEWER, 1, milestones[2],
        ),
    ]


def _fallback_plan(subject: str, goal: str, milestones: list[str]) -> list[PlannedTask]:
    return [
        PlannedTask(
            "目标澄清",
            f"澄清「{subject}」要解决的真正问题、边界与验收标准。",
            ROLE_ARCHITECT, 3, milestones[0],
        ),
        PlannedTask(
            "方案设计",
            f"为「{subject}」给出可执行方案与备选路径，说明取舍。",
            ROLE_ARCHITECT, 2, milestones[0],
        ),
        PlannedTask(
            "执行",
            f"按方案推进「{subject}」，小步交付并保留可回滚点。",
            ROLE_CODER, 2, milestones[1],
        ),
        PlannedTask(
            "验证",
            f"验证「{subject}」的交付结果，记录测试与检查证据。",
            ROLE_TESTER, 1, milestones[2],
        ),
        PlannedTask(
            "评审",
            f"对照目标评审「{subject}」的结果，给出通过或返工结论。",
            ROLE_REVIEWER, 0, milestones[2],
        ),
    ]


_BUILDERS = {
    "software": _software_plan,
    "research": _research_plan,
    "ops": _ops_plan,
    "fallback": _fallback_plan,
}


def plan_for(goal: str, *, title: str = "") -> list[PlannedTask]:
    """Expand ``goal`` into 4–7 deterministic :class:`PlannedTask` entries.

    ``title`` is extra context (for example the project name); it influences
    keyword selection and appears in descriptions, but the output is always a
    pure function of ``(goal, title)``.
    """
    family = _family(goal, title)
    milestones = list(_MILESTONES[family])
    subject = _subject(goal, title)
    return _BUILDERS[family](subject, goal or "", milestones)


def brief_for(project_name: str, goal: str, tasks: list[dict], *,
              done: int, total: int) -> str:
    """Return a short deterministic Chinese progress paragraph.

    ``tasks`` are task dicts (``title`` / ``status`` / ``role`` are read);
    ``done`` and ``total`` are the authoritative project counters. The text
    never includes a timestamp, so seeding and tests produce identical output.
    """
    name = (project_name or "未命名项目").strip() or "未命名项目"
    items = [task for task in tasks or [] if isinstance(task, dict)]
    statuses = [str(task.get("status", "todo")) for task in items]
    counts = {status: statuses.count(status)
              for status in ("todo", "doing", "review", "done", "blocked")}

    total_n = int(total) if total else len(items)
    if total_n <= 0:
        goal_text = (goal or "").strip() or "未填写"
        return f"《{name}》尚未拆解任务，目标：{goal_text}。"
    done_n = max(0, min(int(done), total_n))
    percent = int(done_n / total_n * 100 + 0.5)

    parts = [f"《{name}》共 {total_n} 项任务，已完成 {done_n} 项（进度 {percent}%）",
             f"待办 {counts['todo']} 项",
             f"进行中 {counts['doing']} 项"]
    if counts["review"]:
        parts.append(f"待评审 {counts['review']} 项")
    if counts["blocked"]:
        parts.append(f"受阻 {counts['blocked']} 项")
    text = "，".join(parts) + "。"

    focus = next(
        (task for task in items
         if task.get("status") in ("doing", "review")),
        None,
    )
    if focus is not None:
        role = focus.get("role") or "未指定角色"
        text += f"当前重点：{focus.get('title', '')}（{role}）。"
    elif done_n >= total_n:
        text += "当前任务已全部完成，等待收尾评审。"
    else:
        text += "当前没有进行中的任务，等待派工。"
    goal_text = (goal or "").strip()
    if goal_text:
        text += f"目标：{goal_text}。"
    return text


__all__ = [
    "PlannedTask",
    "ROLE_ARCHITECT",
    "ROLE_CODER",
    "ROLE_RESEARCHER",
    "ROLE_REVIEWER",
    "ROLE_TESTER",
    "brief_for",
    "milestones_for",
    "plan_for",
]
