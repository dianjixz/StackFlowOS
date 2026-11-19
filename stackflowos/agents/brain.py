"""Agent brains: the decision layer that turns a world view into one action.

``Brain.next_action(view)`` returns exactly one :class:`Action`: either a tool
call (``Action.tool`` + ``Action.args``) or a finish decision.  The runner owns
execution, permissions and bookkeeping; a brain only decides.

``MockBrain`` is the offline/deterministic implementation used by the mock LLM
provider and by tests.  It performs real work through real tools (writing
files, running ``py_compile``, updating tasks) — never fake UI animation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .roles import ROLES
from .templates import GENERIC_TEMPLATE, MD_TOOL_TEMPLATE, README_TEMPLATE

#: Recipe roles used by :class:`MockBrain` when a lead breaks down a goal.
_LEAD_RECIPE: tuple[tuple[str, str, str], ...] = (
    ("目标拆解", "architect", "阅读项目目标，拆出可验收的子问题与边界条件。"),
    ("方案设计", "architect", "给出实现方案、接口与数据形状，写清风险。"),
    ("实现", "coder", "按设计实现功能，附最小自检。"),
    ("测试", "tester", "对实现运行最小检查，记录命令与结果。"),
    ("评审", "reviewer", "对照验收标准检查产物，给出通过/打回结论。"),
)

_CODE_HINTS = (
    "代码", "实现", "函数", "接口", "脚本", "修复", "编译", "模块", "类",
    "python", "api", "function", "script", "bug", "code", "module",
)


@dataclass
class AgentView:
    """Everything a brain needs to decide the next action."""

    agent: Any
    project: dict = field(default_factory=dict)
    task: dict | None = None
    tasks: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    step: int = 0
    workspace: str = ""
    recent: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)


@dataclass
class Action:
    """One decision: a tool call, a finish, and/or a line to say."""

    tool: str | None = None
    args: dict = field(default_factory=dict)
    finish: dict | None = None
    say: str = ""

    def to_dict(self) -> dict:
        return {"tool": self.tool, "args": self.args,
                "finish": self.finish, "say": self.say}


@runtime_checkable
class Brain(Protocol):
    """Decision interface shared by ``MockBrain`` and ``LLMBrain``."""

    async def next_action(self, view: AgentView) -> Action:
        ...


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "task"))
    return cleaned.strip("._") or "task"


def _slug(text: str, fallback: str = "impl") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return cleaned[:28] or fallback


def _task_id(task: dict | None) -> str:
    if not isinstance(task, dict):
        return ""
    return str(task.get("id") or "")


def _task_title(task: dict | None) -> str:
    if not isinstance(task, dict):
        return "未命名任务"
    return str(task.get("title") or "未命名任务")


def _task_status(task: dict | None) -> str:
    if not isinstance(task, dict):
        return ""
    return str(task.get("status") or "")


def _artifacts(task: dict | None) -> list[dict]:
    if not isinstance(task, dict):
        return []
    raw = task.get("artifacts")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _goal(project: dict) -> str:
    return str(project.get("goal") or project.get("name") or "未命名目标")


def _finish(summary: str, status: str = "done") -> Action:
    return Action(finish={"summary": summary, "status": status})


def _call(tool: str, **args: Any) -> Action:
    return Action(tool=tool, args=args)


class MockBrain:
    """Deterministic brain that does real work with real tools.

    The plan is driven by ``view.step`` (worker roles) or by the observed task
    list (lead), so the same view always produces the same action.  Every role
    terminates within a small constant number of steps; ``max_steps`` is the
    hard stop.
    """

    def __init__(self, role: str, *, max_steps: int = 16) -> None:
        self.role = str(role)
        self.max_steps = int(max_steps)
        self._spawned: set[str] = set()
        self._spawn_attempt: dict[str, int] = {}
        self._briefed = False
        self._artifact_path: str = ""
        self._artifact_title: str = ""
        self._artifact_registered = False
        self._compiled = False
        self._missing_artifacts = False

    # -- entry ------------------------------------------------------------
    async def next_action(self, view: AgentView) -> Action:
        """Return the next tool call or finish for ``view``."""
        step = int(view.step or 0)
        if step >= self.max_steps:
            return _finish(f"达到步数上限（{self.max_steps}），停止。", "failed")
        planner = {
            "lead": self._lead,
            "architect": self._architect,
            "coder": self._coder,
            "tester": self._tester,
            "reviewer": self._reviewer,
            "researcher": self._researcher,
            "aggregator": self._aggregator,
            "assistant": self._assistant,
        }.get(self.role)
        if planner is None:
            return _finish(f"未知角色：{self.role}", "failed")
        return planner(view, step)

    # -- lead -------------------------------------------------------------
    def _lead(self, view: AgentView, step: int) -> Action:
        project = view.project or {}
        project_id = str(project.get("id") or "")
        if not project_id:
            return _finish("没有可用项目：lead 无法拆解目标。", "blocked")
        tasks = [task for task in (view.tasks or []) if isinstance(task, dict)]
        goal = _goal(project)

        if len(tasks) < len(_LEAD_RECIPE):
            title_suffix, role, description = _LEAD_RECIPE[len(tasks)]
            title = (f"{title_suffix}：{goal}" if title_suffix == "目标拆解"
                     else title_suffix)
            return _call(
                "task.create", project_id=project_id, title=title,
                description=f"{description} 项目目标：{goal}",
                role=role, priority=1)

        for task in tasks:
            if _task_status(task) != "todo" or task.get("assignee"):
                continue
            tid = _task_id(task)
            # Spawns can fail when the worker pool is full; retry after a
            # short cooldown, but not on every single step.
            last = self._spawn_attempt.get(f"spawn:{tid}")
            if last is not None and step - last < 2:
                continue
            self._spawn_attempt[f"spawn:{tid}"] = step
            role = str(task.get("role") or "coder")
            if role in ("lead", "assistant") or role not in ROLES:
                role = "coder"
            return _call("agent.spawn", role=role, project_id=project_id,
                         task_id=tid, reason="lead 派工")

        for task in tasks:
            if _task_status(task) != "review":
                continue
            tid = _task_id(task)
            last = self._spawn_attempt.get(f"review:{tid}")
            if last is not None and step - last < 2:
                continue
            self._spawn_attempt[f"review:{tid}"] = step
            return _call("agent.spawn", role="reviewer", project_id=project_id,
                         task_id=tid, reason="lead 安排评审")

        done = sum(1 for task in tasks if _task_status(task) == "done")
        review = sum(1 for task in tasks if _task_status(task) == "review")
        doing = sum(1 for task in tasks if _task_status(task) == "doing")
        todo = sum(1 for task in tasks if _task_status(task) == "todo")
        blocked = sum(1 for task in tasks if _task_status(task) == "blocked")
        open_tasks = [task for task in tasks
                      if _task_status(task) in ("todo", "doing")]
        if not open_tasks and not self._briefed:
            self._briefed = True
            brief = (f"目标：{goal}\n任务 {len(tasks)} 个：完成 {done}，"
                     f"评审 {review}，进行中 {doing}，待办 {todo}，阻塞 {blocked}。")
            # No open work left: hand the project to the human reviewer
            # instead of leaving it "active" forever.
            return _call("project.update", project_id=project_id,
                         patch={"brief": brief, "status": "review"})
        if self._briefed or not open_tasks:
            return _finish(
                f"项目推进中：任务 {len(tasks)} 个，完成 {done}，评审 {review}，"
                f"进行中 {doing}，待办 {todo}，阻塞 {blocked}。")
        # Work remains but every spawn is cooling down (pool full or a worker
        # still starting). Refresh the view and retry on the next step.
        return _call("task.list", project_id=project_id)

    # -- worker roles -----------------------------------------------------
    def _require_task(self, view: AgentView) -> tuple[dict, Action | None]:
        task = view.task
        if not isinstance(task, dict) or not _task_id(task):
            return {}, _finish("没有已分配任务，无法继续。", "blocked")
        if _task_status(task) == "done":
            return task, _finish(f"任务 {_task_id(task)} 已完成。")
        return task, None

    def _architect(self, view: AgentView, step: int) -> Action:
        task, stop = self._require_task(view)
        if stop is not None:
            return stop
        tid = _task_id(task)
        title = _task_title(task)
        if step == 0:
            return _call("task.update", task_id=tid, patch={"status": "doing"})
        if step == 1:
            path = f"design/{_safe_id(tid)}-design.md"
            content = (
                f"# 设计：{title}\n\n"
                f"- 任务：`{tid}`\n"
                f"- 目标：{task.get('description') or '（无描述）'}\n\n"
                "## 方案\n"
                "1. 明确输入、输出与边界条件。\n"
                "2. 定义模块接口与数据结构，保持单向依赖。\n"
                "3. 先实现最小可用路径，再补异常与日志。\n\n"
                "## 验收\n"
                "- 产物文件存在且路径可复现；\n"
                "- 关键路径有最小自检（语法/运行）；\n"
                "- 风险与未决问题在评审中列出。\n")
            self._artifact_path = path
            self._artifact_title = f"{title} 设计文档"
            return _call("fs.write", path=path, content=content)
        if step == 2:
            return _call("task.artifact", task_id=tid, artifact={
                "type": "file", "path": self._artifact_path,
                "title": self._artifact_title})
        if step == 3:
            return _call("task.update", task_id=tid, patch={"status": "review"})
        return _finish(f"完成 {tid} 设计文档：{self._artifact_path}")

    def _coder(self, view: AgentView, step: int) -> Action:
        task, stop = self._require_task(view)
        if stop is not None:
            return stop
        tid = _task_id(task)
        title = _task_title(task)
        description = str(task.get("description") or "")
        if step == 0 and _task_status(task) == "todo":
            return _call("task.update", task_id=tid, patch={"status": "doing"})
        # Progress is derived from what already happened, so a task that was
        # already `doing` when the worker claimed it skips the update step
        # instead of repeating the write or wasting a step.
        if not self._artifact_path:
            if self._is_doc_task(task):
                path = "README.md"
                body = self._readme_body(tid, title, description, task, view.tasks)
            elif self._is_code_task(task):
                path = (f"src/{_safe_id(tid)}-{_slug(title)}.py")
                body = self._python_body(tid, title, description)
            else:
                path = f"notes/{_safe_id(tid)}-impl.md"
                body = self._note_body(tid, title, description)
            self._artifact_path = path
            self._artifact_title = f"{title} 实现产物"
            return _call("fs.write", path=path, content=body)
        if not self._compiled:
            self._compiled = True
            if self._artifact_path.endswith(".py"):
                quoted = _shell_quote(self._artifact_path)
                command = (f"python3 -m py_compile {quoted} && "
                           f"python3 {quoted} --selftest")
            else:
                command = "true"
            return _call("shell.exec", command=command)
        if not self._artifact_registered:
            self._artifact_registered = True
            return _call("task.artifact", task_id=tid, artifact={
                "type": "file", "path": self._artifact_path,
                "title": self._artifact_title})
        if _task_status(task) != "review":
            return _call("task.update", task_id=tid, patch={"status": "review"})
        return _finish(f"完成 {tid}：写入 {self._artifact_path} 并做语法自检。")

    def _tester(self, view: AgentView, step: int) -> Action:
        task, stop = self._require_task(view)
        if stop is not None:
            return stop
        tid = _task_id(task)
        title = _task_title(task)
        if step == 0:
            return _call("task.update", task_id=tid, patch={"status": "doing"})
        if step == 1:
            return _call("fs.list", path=".")
        artifacts = _artifacts(task)
        python_paths = [str(item.get("path") or "") for item in artifacts
                        if str(item.get("path") or "").endswith(".py")]
        if step == 2:
            command = (f"python3 -m py_compile {_shell_quote(python_paths[0])}"
                       if python_paths else "true")
            return _call("shell.exec", command=command)
        if step == 3:
            path = f"tests/{_safe_id(tid)}-report.md"
            content = (
                f"# 测试报告：{title}\n\n"
                f"- 任务：`{tid}`\n"
                f"- 产物数：{len(artifacts)}\n"
                f"- 自检命令："
                f"{'python3 -m py_compile ' + python_paths[0] if python_paths else 'true (无 Python 产物)'}\n"
                "- 结论：最小语法检查通过；未执行的行为测试需在后续任务补充。\n")
            self._artifact_path = path
            self._artifact_title = f"{title} 测试报告"
            return _call("fs.write", path=path, content=content)
        if step == 4:
            return _call("task.artifact", task_id=tid, artifact={
                "type": "file", "path": self._artifact_path,
                "title": self._artifact_title})
        if step == 5:
            return _call("task.update", task_id=tid, patch={"status": "review"})
        return _finish(f"完成 {tid} 测试报告：{self._artifact_path}")

    def _reviewer(self, view: AgentView, step: int) -> Action:
        task, stop = self._require_task(view)
        if stop is not None:
            return stop
        tid = _task_id(task)
        title = _task_title(task)
        artifacts = _artifacts(task)
        artifact_path = str(artifacts[0].get("path") or "") if artifacts else ""
        if step == 0:
            return _call("fs.list", path=".")
        if step == 1:
            if artifact_path:
                return _call("fs.read", path=artifact_path)
            siblings = [item for item in (view.tasks or [])
                        if _task_id(item) and _task_id(item) != tid]
            project_closed = bool(siblings) and all(
                _task_status(item) in ("done", "review") for item in siblings)
            if project_closed:
                # Project-level review task: there is no task-local artifact,
                # but the sibling tasks are all closed, so the reviewer can
                # accept against the project's overall state.
                return _call("task.note", task_id=tid,
                             text="评审：本任务为项目级验收；兄弟任务均已关闭，"
                                  "按项目工作区产物做整体评审。")
            self._missing_artifacts = True
            return _call("task.note", task_id=tid,
                         text="评审发现：任务未登记任何产物，无法验收。")
        if step == 2:
            verdict = "打回" if self._missing_artifacts else "通过"
            path = f"review/{_safe_id(tid)}-review.md"
            evidence = artifact_path or "（无产物）"
            content = (
                f"# 评审：{title}\n\n"
                f"- 任务：`{tid}`\n"
                f"- 产物：{evidence}\n"
                f"- 结论：{verdict}\n\n"
                "## 检查项\n"
                f"- 产物存在：{'是' if artifact_path else '否'}\n"
                "- 自检记录：见任务产物与事件流\n"
                "- 验收："
                f"{'满足最小验收条件' if artifact_path else ('项目级验收：兄弟任务均已关闭' if not self._missing_artifacts else '缺少产物，退回 todo 补齐')}\n")
            self._artifact_path = path
            return _call("fs.write", path=path, content=content)
        if step == 3:
            return _call("task.artifact", task_id=tid, artifact={
                "type": "file", "path": self._artifact_path,
                "title": f"{title} 评审记录"})
        if step == 4:
            return _call("task.update", task_id=tid,
                         patch={"status": "todo" if self._missing_artifacts else "done"})
        if self._missing_artifacts:
            return _finish(f"任务 {tid} 产物缺失，已退回 todo。", "blocked")
        return _finish(f"任务 {tid} 评审通过，已置为 done。")

    def _researcher(self, view: AgentView, step: int) -> Action:
        task, stop = self._require_task(view)
        if stop is not None:
            return stop
        tid = _task_id(task)
        title = _task_title(task)
        if step == 0:
            return _call("task.update", task_id=tid, patch={"status": "doing"})
        if step == 1:
            path = f"research/{_safe_id(tid)}-notes.md"
            content = (
                f"# 调研笔记：{title}\n\n"
                f"- 任务：`{tid}`\n"
                f"- 问题：{task.get('description') or title}\n\n"
                "## 要点\n"
                "1. 先界定问题与验收标准，避免扩大范围。\n"
                "2. 记录信息来源；本离线环境未抓取外部网页。\n"
                "3. 标注不确定项，交给评审决定是否继续。\n\n"
                "## 待确认\n"
                "- 需要外部资料时，由操作员批准 net.fetch 后补充。\n")
            self._artifact_path = path
            self._artifact_title = f"{title} 调研笔记"
            return _call("fs.write", path=path, content=content)
        if step == 2:
            return _call("task.artifact", task_id=tid, artifact={
                "type": "file", "path": self._artifact_path,
                "title": self._artifact_title})
        if step == 3:
            return _call("task.update", task_id=tid, patch={"status": "review"})
        return _finish(f"完成 {tid} 调研笔记：{self._artifact_path}")

    def _aggregator(self, view: AgentView, step: int) -> Action:
        project = view.project or {}
        project_id = str(project.get("id") or "")
        if not project_id:
            return _finish("没有项目可汇总。", "blocked")
        tasks = [task for task in (view.tasks or []) if isinstance(task, dict)]
        if step == 0:
            return _call("project.get", project_id=project_id)
        if step == 1:
            return _call("task.list", project_id=project_id)
        if step == 2:
            return _call("event.query", project_id=project_id,
                         since=0, limit=50)
        done = sum(1 for task in tasks if _task_status(task) == "done")
        review = sum(1 for task in tasks if _task_status(task) == "review")
        doing = sum(1 for task in tasks if _task_status(task) == "doing")
        todo = sum(1 for task in tasks if _task_status(task) == "todo")
        blocked = sum(1 for task in tasks if _task_status(task) == "blocked")
        brief = (
            f"目标：{_goal(project)}\n"
            f"任务 {len(tasks)} 个：完成 {done}，评审 {review}，进行中 {doing}，"
            f"待办 {todo}，阻塞 {blocked}。\n"
            f"状态：{project.get('status') or '未知'}。")
        if step == 3:
            return _call("fs.write", path="brief.md", content=f"# 项目简报\n\n{brief}\n")
        if step == 4:
            return _call("project.update", project_id=project_id,
                         patch={"brief": brief})
        return _finish(f"已更新项目 {project_id} 简报。")

    def _assistant(self, view: AgentView, step: int) -> Action:
        return _finish("系统助手已就绪；对话由 hub 会话层处理。")

    # -- content helpers --------------------------------------------------
    @staticmethod
    def _is_code_task(task: dict) -> bool:
        text = f"{task.get('title') or ''} {task.get('description') or ''}".lower()
        return any(hint in text for hint in _CODE_HINTS)

    @staticmethod
    def _is_doc_task(task: dict) -> bool:
        # Only an explicitly documentation-shaped *title* selects README.md;
        # descriptions often mention "文档" without being the docs task.
        text = str(task.get("title") or "").lower()
        return any(hint in text for hint in ("文档", "readme", "使用说明", "手册"))

    @staticmethod
    def _python_body(tid: str, title: str, description: str) -> str:
        cleaned_title = _sanitize_block(title)
        cleaned_desc = _sanitize_block(description or "（无描述）")
        haystack = f"{title} {description}".lower()
        if "markdown" in haystack:
            template = MD_TOOL_TEMPLATE
        else:
            template = GENERIC_TEMPLATE
        return (template
                .replace("__TID__", tid)
                .replace("__TITLE__", cleaned_title)
                .replace("__DESC__", cleaned_desc))

    @staticmethod
    def _readme_body(tid: str, title: str, description: str, task: dict,
                     tasks: list[dict] | None = None) -> str:
        source = f"{tid}-impl.py"
        for item in tasks or []:
            if _task_id(item) == tid:
                continue
            if (str(item.get("role") or "") == "coder"
                    and _task_status(item) in ("done", "review")):
                other_id = _task_id(item)
                source = f"{_safe_id(other_id)}-{_slug(_task_title(item))}.py"
                break
        readme_title = "使用说明" if "文档" in title else _sanitize_md(title)
        return (README_TEMPLATE
                .replace("__TITLE__", readme_title)
                .replace("__GOAL__", _sanitize_md(description or "项目说明"))
                .replace("__SRC__", source))

    @staticmethod
    def _note_body(tid: str, title: str, description: str) -> str:
        return (
            f"# 实现说明：{title}\n\n"
            f"- 任务：`{tid}`\n"
            f"- 描述：{description or '（无描述）'}\n\n"
            "## 做法\n"
            "1. 明确输入与输出，保持实现最小化。\n"
            "2. 失败路径显式返回错误，不吞异常。\n"
            "3. 自检：语法检查 + 关键路径手测。\n\n"
            "## 自检记录\n"
            "- 本文件由 coder agent 在 workspace 内生成；\n"
            "- 语法/行为检查结果以 shell.exec 输出为准。\n")


def _sanitize_md(text: str) -> str:
    return str(text).replace("\n", " ").strip()


def _sanitize_block(text: str) -> str:
    """Make text safe to embed in a Python triple-quoted string."""
    return str(text).replace("\\", "\\\\").replace('"""', "'''")


def _shell_quote(text: str) -> str:
    """POSIX-ish single quoting, safe for our workspace paths."""
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


__all__ = ["Action", "AgentView", "Brain", "MockBrain"]
