"""Project onboarding: turn an empty workspace into a place an agent can inhabit.

The "empty house" failure mode is not missing features, it is missing signals:
an agent lands in ``<workspace>/`` and cannot tell which project it belongs to,
where it may write, what to work on, or how to check that its scope is ready.
This module defines the small, fixed kit that answers those questions:

* a **nameplate** — :func:`brief_markdown`, written to ``brief.md`` in the
  workspace, naming the project, goal, workspace path and boundaries;
* a **fixed layout** — ``tasks/``, ``artifacts/``, ``logs/``, ``tmp/`` plus
  ``brief.md`` (:data:`SCAFFOLD_DIRS`);
* a **seed task** — :func:`seed_task_spec`, one self-check task that walks the
  whole loop (read brief -> health check -> write artifact -> review);
* **memory seed** — :func:`memory_seed`, the goal, glossary placeholder,
  naming conventions and artifact-path habits every new agent would otherwise
  have to guess again.

The helpers are pure: they touch the workspace or build dictionaries, but they
never emit events or call the hub.  :class:`stackflowos.app.App` orchestrates
them on project create/attach, and the CLI reuses them for explicit onboarding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: Fixed workspace layout, with the one-line purpose an agent needs.
SCAFFOLD_DIRS: tuple[tuple[str, str], ...] = (
    ("tasks", "任务中间产物：按 <task-id>/ 分目录；这不是任务看板本身。"),
    ("artifacts", "交付物：任务登记 artifact 时指向这里，评审看这里。"),
    ("logs", "执行记录：命令输出、自检结果、排障线索。"),
    ("tmp", "草稿与临时文件：可以随时清理，不要放交付物。"),
)

BRIEF_FILENAME = "brief.md"
SEED_TASK_TITLE = "入职自检：复述目标并确认入住条件"
#: Memory keys seeded for every project (see ``memory_seed``).
MEMORY_KEYS: tuple[str, ...] = ("goal", "brief", "workspace", "layout",
                                "conventions", "glossary")
#: Bumped when the scaffold/memory contract changes semantically.
SCAFFOLD_VERSION = 1

_MODE_LABELS = {
    "managed": "系统托管",
    "attached-ro": "接入（只读）",
    "attached-rw": "接入（可写）",
}


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def writable_mode(project: Any) -> bool:
    """Whether the system owns writes in this project's workspace."""
    return str(_field(project, "workspace_mode", "managed")) in (
        "managed", "attached-rw")


def workspace_path(project: Any) -> Path | None:
    raw = str(_field(project, "workspace", "") or "").strip()
    return Path(raw).expanduser() if raw else None


def brief_markdown(project: Any) -> str:
    """Render the project nameplate written to ``brief.md``."""
    project_id = str(_field(project, "id", "") or "")
    name = str(_field(project, "name", "") or project_id or "未命名项目")
    goal = str(_field(project, "goal", "") or "").strip()
    goal_text = goal or "（未设置：请操作员补全项目 goal，否则 Agent 无法知道要做什么）"
    workspace = str(workspace_path(project) or "（未设置）")
    mode = str(_field(project, "workspace_mode", "managed") or "managed")
    mode_label = _MODE_LABELS.get(mode, mode)
    status = str(_field(project, "status", "") or "planning")
    created_by = str(_field(project, "created_by", "") or "operator")
    brief = str(_field(project, "brief", "") or "").strip()

    layout_lines = [
        f"- `{BRIEF_FILENAME}` — 本文件：门牌、目标、边界、自检入口。",
    ]
    layout_lines.extend(f"- `{name}/` — {purpose}" for name, purpose in SCAFFOLD_DIRS)
    memory_lines = "\n".join(f"- `{key}`" for key in MEMORY_KEYS)
    brief_section = (f"\n## 当前进展\n\n{brief}\n" if brief else "")
    return f"""# {name}

> 这是项目 workspace 的门牌。先读它，再决定下一步做什么。

## 门牌

- 项目 ID：`{project_id}`
- 项目目标：{goal_text}
- 工作区：`{workspace}`
- 工作区模式：{mode_label}（`{mode}`）
- 项目状态：`{status}`
- 创建者：`{created_by}`
{brief_section}
## 目录约定

{chr(10).join(layout_lines)}

## 工作方式

1. 先读本文件；再用 `project.health` 自检（命令行：`sfo project health {project_id}`）。
   有 `fail` 就先补齐前置条件，不要硬做。
2. 只做任务要求的事；接任务后按任务 id 把中间产物放 `tasks/<task-id>/`。
3. 最终交付物放 `artifacts/`，并在任务上登记 `task.artifact`。
4. 命令输出/自检结果放 `logs/`；不要把大段日志塞进事件或记忆。
5. 完成后把任务推进到 `review`；失败/缺条件时用 `finish(status=blocked)`
   并写清缺什么，**不要**把前置条件缺失标成 `failed`。

## 能力与审批边界

- 读类（`fs.read`、`fs.list`、`task.list`、`project.get`）是低风险，默认自动放行。
- 写类（`fs.write`）、执行类（`shell.exec`）、网络类（`net.fetch`）保持人审；
  工作区内的写是 medium，工作区外一律升为 high。
- 路径只能落在本项目 workspace 内；越界会被拒绝，换路径绕过审批不是解法。
- 需要更多权限时找操作员，不要在文件里猜口令或寻找凭据。

## 项目记忆（memory）

新项目会写入这些键；用 `memory.list` / `memory.get` 读取，约定变更要写回：

{memory_lines}

## 自检入口

- Agent 工具：`project.health`（返回逐项检查、`ready`/`not_ready_reasons` 与修复建议）。
- 命令行：`sfo project health {project_id}`。
- 失败时按检查项的 `fix` 处理，不要靠对话猜。

## 边界

- 不把密钥、令牌或个人信息写进文件、事件或记忆。
- 不修改工作区外的系统路径；不删除项目目录。
- 不确定时先 finish(blocked) 说明缺什么，交给操作员。
"""


def ensure_scaffold(project: Any, *, write: bool = True) -> dict:
    """Create/check the fixed layout and ``brief.md`` in the workspace.

    ``write=False`` is a pure inspection: it reports what is missing and never
    touches the filesystem (used for attached read-only projects).  Even with
    ``write=True`` an ``attached-ro`` project is refused, because attaching a
    directory must not silently mutate someone else's tree.
    """
    result: dict[str, Any] = {
        "project_id": str(_field(project, "id", "") or ""),
        "workspace": str(workspace_path(project) or ""),
        "created": [],
        "missing": [],
        "brief": "missing",
        "warnings": [],
        "refused": None,
        "version": SCAFFOLD_VERSION,
    }
    ws = workspace_path(project)
    if ws is None:
        result["warnings"].append("项目没有 workspace 路径")
        result["missing"] = [BRIEF_FILENAME] + [name for name, _ in SCAFFOLD_DIRS]
        return result
    if not write:
        pass
    elif not writable_mode(project):
        result["refused"] = (
            "workspace 是 attached-ro（只读接入）；不会向该目录写骨架。"
            "如确需写入，先由操作员切换为可写接入，或手工创建目录。")
        write = False

    result["workspace"] = str(ws)
    if not ws.is_dir():
        result["warnings"].append(f"工作区不存在或不是目录：{ws}")
        result["missing"] = [BRIEF_FILENAME] + [name for name, _ in SCAFFOLD_DIRS]
        return result

    for name, _purpose in SCAFFOLD_DIRS:
        target = ws / name
        if target.is_symlink():
            result["warnings"].append(f"{name} 是符号链接，跳过（不跟随到工作区外）")
            result["missing"].append(name)
            continue
        if target.is_dir():
            continue
        if target.exists():
            result["warnings"].append(f"{name} 已存在但不是目录")
            result["missing"].append(name)
            continue
        if not write:
            result["missing"].append(name)
            continue
        try:
            target.mkdir()
            result["created"].append(name)
        except OSError as exc:
            result["warnings"].append(f"创建 {name} 失败：{exc}")
            result["missing"].append(name)

    brief = ws / BRIEF_FILENAME
    if brief.is_symlink():
        result["warnings"].append("brief.md 是符号链接，跳过（不跟随到工作区外）")
        result["brief"] = "unsafe"
    elif brief.is_file():
        try:
            text = brief.read_text("utf-8")
        except OSError:
            text = ""
        if text.strip():
            result["brief"] = "kept"
        elif write:
            try:
                brief.write_text(brief_markdown(project), "utf-8")
                result["brief"] = "created"
            except OSError as exc:
                result["warnings"].append(f"写入 brief.md 失败：{exc}")
                result["brief"] = "missing"
        else:
            result["brief"] = "empty"
            result["missing"].append(BRIEF_FILENAME)
    elif brief.exists():
        result["warnings"].append("brief.md 已存在但不是普通文件")
        result["brief"] = "unsafe"
    elif write:
        try:
            brief.write_text(brief_markdown(project), "utf-8")
            result["brief"] = "created"
        except OSError as exc:
            result["warnings"].append(f"写入 brief.md 失败：{exc}")
            result["brief"] = "missing"
    else:
        result["missing"].append(BRIEF_FILENAME)
    return result


def seed_task_spec(project: Any) -> dict:
    """Build the one task that gives a new project a complete work signal."""
    project_id = str(_field(project, "id", "") or "")
    name = str(_field(project, "name", "") or project_id)
    goal = str(_field(project, "goal", "") or "").strip() or "（项目目标待补全）"
    workspace = str(workspace_path(project) or "（未设置）")
    mode = str(_field(project, "workspace_mode", "managed") or "managed")
    artifact = f"artifacts/onboarding-{project_id or 'project'}.md"
    description = f"""这是项目 {project_id}（{name}）的入住自检任务，用来验证“门牌 / 目录 / 看板 / 权限”是否就绪。

步骤（只做这些，不要扩大范围）：
1. 调用 `project.health` 自检；如果返回 `ready=false`，把 `not_ready_reasons`
   原样写进 {artifact}，然后 `finish(status=blocked, summary=缺什么)`，不要硬做。
2. 用 `fs.read` 读 `brief.md`，确认：项目目标、workspace 路径、目录约定、审批边界。
3. 用 `fs.list .` 确认 `tasks/`、`artifacts/`、`logs/`、`tmp/` 都在。
4. 用 `fs.write` 写 {artifact}，内容至少包含：
   - 复述项目目标（一句话）；
   - workspace 路径与模式（{workspace} / {mode}）；
   - 你的角色可用工具，以及哪些动作需要人审；
   - 健康检查结果摘要（ok/warn/fail 数量）；
   - 后续应该把中间产物/交付物/日志分别放在哪里。
5. 用 `task.artifact` 登记该文件，把任务推进到 `review`，然后 `finish(done)`。

验收：artifact 文件存在且内容包含以上五项；任务状态为 review。
原项目目标：{goal}
"""
    return {
        "title": SEED_TASK_TITLE,
        "description": description,
        "role": "researcher",
        "priority": 0,
        "created_by": "sys:onboarding",
    }


def memory_seed(project: Any) -> dict[str, Any]:
    """Initial project memory: goal, workspace, layout and conventions."""
    project_id = str(_field(project, "id", "") or "")
    ws = workspace_path(project)
    return {
        "goal": str(_field(project, "goal", "") or ""),
        "brief": str(_field(project, "brief", "") or ""),
        "workspace": {
            "path": str(ws) if ws is not None else "",
            "mode": str(_field(project, "workspace_mode", "managed") or "managed"),
            "external": bool(_field(project, "external", False)),
            "project_id": project_id,
        },
        "layout": {name: purpose for name, purpose in SCAFFOLD_DIRS},
        "conventions": {
            "artifact_path": "artifacts/<task-id>-<slug>.<ext>",
            "task_scratch_path": "tasks/<task-id>/",
            "log_path": "logs/<task-id>.log",
            "tmp_path": "tmp/",
            "naming": "文件名带 task id；小写短横线分隔；内容用中文也可以。",
            "onboarding": f"brief.md 的 doorplate；自检用 project.health。",
        },
        "glossary": [],
    }


def seed_task_present(tasks: list[Any]) -> bool:
    """Whether the project already has an onboarding self-check task."""
    return any(
        str(_field(task, "title", "") or "").startswith("入职自检")
        for task in tasks or []
    )


__all__ = [
    "BRIEF_FILENAME",
    "MEMORY_KEYS",
    "SCAFFOLD_DIRS",
    "SCAFFOLD_VERSION",
    "SEED_TASK_TITLE",
    "brief_markdown",
    "ensure_scaffold",
    "memory_seed",
    "seed_task_present",
    "seed_task_spec",
    "workspace_path",
    "writable_mode",
]
