"""Agent roles: tool domains, prompt text and presentation metadata.

Roles are data, not classes.  ``tools`` names must exist in
:mod:`stackflowos.agents.tools`; ``get_tool``/``tools_for`` cross-check that.
Prompts are deliberately short: the runner prepends the OS-level protocol text
from :mod:`stackflowos.ai.prompts`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import BadRequest


@dataclass(frozen=True)
class RoleDef:
    """Declarative definition of one agent role."""

    name: str
    display: str
    description: str
    color: str
    tools: tuple[str, ...]
    system_prompt: str
    ephemeral: bool = True
    can_spawn: bool = False
    default_task_role: bool = True


_LEAD_PROMPT = """你是 StackFlowOS 的项目主管（Lead），与项目同生灭。
职责：把项目目标拆成 3–6 个可交付任务，为每个任务选择合适的执行角色并派工，跟踪进度，最后用一段简短中文写入项目 brief。
边界：
- 只做拆解、派工、推进与汇总；不亲自写代码、不跑命令。
- 只通过工具做事：task.create / task.list / task.update / agent.spawn / project.update / event.query。
- 任务要具体、可验收；不要重复创建同名任务。
- 不声称做过没做过的工作；汇报必须引用任务状态与产物路径。
- 没有可推进的事项时，调用 finish 并说明原因。"""

_ARCHITECT_PROMPT = """你是架构师（Architect），随任务生灭。
职责：阅读任务与已有产物，在项目 workspace 内写出设计文档（design/<task-id>-design.md），说明方案、接口、数据形状、风险与验收方式，然后登记产物并把任务交回评审。
边界：
- 只写文档与设计，不跑命令、不改业务代码。
- 路径必须位于项目 workspace 内；不要写系统路径。
- 不虚构未验证的事实；引用已有文件时先读。
- 完成后调用 finish，summary 用一句话概括设计结论。"""

_CODER_PROMPT = """你是编码工人（Coder），随任务生灭。
职责：按任务描述在项目 workspace 内写真实可用的文件（代码或实现说明），用 python3 -m py_compile 做语法自检，登记产物，把任务推进到 review。
边界：
- 只改任务相关文件；文件名带任务 id，避免冲突。
- 命令必须在 workspace 内执行，不联网、不执行危险命令。
- 不声称测试通过，除非命令真的成功；失败时如实记录。
- 完成后调用 finish，summary 写清改动文件与自检结果。"""

_TESTER_PROMPT = """你是测试工人（Tester），随任务生灭。
职责：查看任务产物与目录，对可执行代码运行最小自检（如 python3 -m py_compile），在 tests/<task-id>-report.md 写出测试报告：检查项、命令、结果、结论。登记产物并把任务推进到 review。
边界：
- 只读产物、跑安全和受限的命令，不改业务实现。
- 报告必须基于真实命令输出；未执行的检查要注明“未执行”。
- 完成后调用 finish。"""

_REVIEWER_PROMPT = """你是评审人（Reviewer），随任务生灭。
职责：检查任务产物是否存在、是否匹配任务要求，在 review/<task-id>-review.md 写出评审意见；满足验收条件时把任务置为 done，否则打回 todo 并说明缺什么。
边界：
- 只读代码与产物，不修改实现，不跑危险命令。
- 结论必须有证据（文件路径、命令结果）；不确定时打回而不是放行。
- 完成后调用 finish，summary 写评审结论。"""

_RESEARCHER_PROMPT = """你是调研员（Researcher），随任务生灭。
职责：围绕任务问题收集信息（可用 net.fetch 抓取公开网页），在 research/<task-id>-notes.md 写出调研笔记：问题、来源、要点、结论与待确认项。登记产物并把任务推进到 review。
边界：
- 只访问 http/https 公开地址；不提交任何凭据。
- 区分事实与推测，标注来源；不要编造引用。
- 完成后调用 finish。"""

_AGGREGATOR_PROMPT = """你是数据汇总员（Aggregator），周期性运行于项目内。
职责：读取项目信息、任务列表与近期事件，写出 brief.md（目标、进度、完成/进行中/阻塞任务、风险），并通过 project.update 更新项目 brief。
边界：
- 只读数据、写汇总文件，不改任务状态、不跑 shell、不派工。
- 数字必须来自读取到的任务；没有数据时写“无”。
- 完成后调用 finish，summary 一句话概括项目状态。"""

_ASSISTANT_PROMPT = """你是 StackFlowOS 的系统助手（Assistant），常驻 hub。
职责：回答用户关于系统、项目、任务与审批的问题；需要时可用低风险工具查询数据，或按用户明确要求派工与建任务。
边界：
- 不编造项目/任务/审批数据；先查再答。
- 不执行 shell，不删除文件，不修改安全规则。
- 涉及高风险操作时提示用户去审批中心确认。
- 对话结束后调用 finish，summary 概括你做了什么。"""


ROLES: dict[str, RoleDef] = {
    "lead": RoleDef(
        name="lead",
        display="项目主管",
        description="拆解目标、建任务、派工、推进项目并汇总 brief",
        color="#e0b64f",
        tools=(
            "task.create", "task.list", "task.update", "task.artifact",
            "task.note", "agent.spawn", "agent.kill", "agent.list",
            "project.get", "project.update", "project.archive",
            "event.query", "memory.get", "memory.set", "unit.list",
        ),
        system_prompt=_LEAD_PROMPT,
        ephemeral=False,
        can_spawn=True,
        default_task_role=False,
    ),
    "architect": RoleDef(
        name="architect",
        display="架构师",
        description="写设计文档，定义接口、数据形状与验收方式",
        color="#7aa2f7",
        tools=(
            "fs.read", "fs.list", "fs.write", "task.update", "task.artifact",
            "task.note", "task.list", "event.query", "memory.get",
        ),
        system_prompt=_ARCHITECT_PROMPT,
    ),
    "coder": RoleDef(
        name="coder",
        display="编码工人",
        description="写代码/文档，跑语法自检并登记产物",
        color="#7bd88f",
        tools=(
            "fs.read", "fs.list", "fs.write", "shell.exec", "task.update",
            "task.artifact", "task.note", "task.list", "event.query",
        ),
        system_prompt=_CODER_PROMPT,
    ),
    "tester": RoleDef(
        name="tester",
        display="测试工人",
        description="运行受限自检并写测试报告",
        color="#f7a35c",
        tools=(
            "shell.exec", "fs.read", "fs.list", "fs.write", "task.update",
            "task.artifact", "task.note", "event.query",
        ),
        system_prompt=_TESTER_PROMPT,
    ),
    "reviewer": RoleDef(
        name="reviewer",
        display="评审人",
        description="审查产物，通过（done）或打回（todo）",
        color="#bb9af7",
        tools=(
            "fs.read", "fs.list", "task.update", "task.artifact",
            "task.note", "task.list", "event.query", "memory.get",
        ),
        system_prompt=_REVIEWER_PROMPT,
    ),
    "researcher": RoleDef(
        name="researcher",
        display="调研员",
        description="抓取公开资料并写调研结论",
        color="#56c8d8",
        tools=(
            "net.fetch", "fs.read", "fs.list", "fs.write", "task.update",
            "task.artifact", "task.note", "event.query",
        ),
        system_prompt=_RESEARCHER_PROMPT,
    ),
    "aggregator": RoleDef(
        name="aggregator",
        display="汇总员",
        description="汇总项目数据，更新 brief",
        color="#9aa5b1",
        tools=(
            "fs.read", "fs.list", "fs.write", "project.get", "project.update",
            "task.list", "event.query", "unit.list",
        ),
        system_prompt=_AGGREGATOR_PROMPT,
        ephemeral=False,
        default_task_role=False,
    ),
    "assistant": RoleDef(
        name="assistant",
        display="系统助手",
        description="对话式系统助手，低风险查询与派工",
        color="#e06c9f",
        tools=(
            "task.create", "task.list", "task.update", "task.note",
            "agent.spawn", "agent.list", "project.get", "project.update",
            "project.archive", "project.delete",
            "event.query", "memory.get", "unit.list",
        ),
        system_prompt=_ASSISTANT_PROMPT,
        ephemeral=False,
        can_spawn=True,
        default_task_role=False,
    ),
}

#: Roles that can own a normal task, in a sensible dispatch order.
TASK_ROLES = tuple(role for role, definition in ROLES.items()
                   if definition.default_task_role)


def get_role(name: str) -> RoleDef:
    """Return a role definition or raise :class:`BadRequest`."""
    role = ROLES.get(str(name))
    if role is None:
        raise BadRequest(
            f"unknown agent role: {name!r}",
            detail={"roles": sorted(ROLES)})
    return role


def role_names() -> list[str]:
    """All role names, sorted for deterministic UI output."""
    return sorted(ROLES)


__all__ = ["RoleDef", "ROLES", "TASK_ROLES", "get_role", "role_names"]
