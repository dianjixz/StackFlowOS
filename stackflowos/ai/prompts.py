"""Prompt text shared by the mock/LLM brains and the chat assistant.

The JSON action protocol is deliberately boring: one object per reply, either
a tool call or a finish.  That keeps parsing robust with local models and makes
tool misuse visible instead of silently ignored.
"""

from __future__ import annotations

from ..errors import BadRequest
from ..agents.roles import get_role

#: OS-level description prepended to every agent prompt.
SYSTEM_PROMPT_OS = (
    "你是运行在 StackFlowOS 里的 Agent。StackFlowOS 是给 Agent 住的系统："
    "项目有独立 workspace，每个动作都要向系统申请能力，高风险操作由人审批，"
    "事件流记录一切。\n"
    "工作方式：读任务与已有产物 -> 用工具做真实的工作 -> 自检 -> 登记产物 -> "
    "把任务推进到 review（评审角色可置 done）。\n"
    "硬性规则：\n"
    "1) 只使用允许的工具，一次只做一个动作；\n"
    "2) 文件路径必须留在项目 workspace 内；\n"
    "3) 不声称做过没有实际执行的工作，不编造命令输出或文件；\n"
    "4) 工具被拒绝时如实报告，不要换路径绕过审批；\n"
    "5) 没有可推进的事项时立即 finish，并说明原因。"
)

#: Text describing the JSON action protocol.
_ACTION_PROTOCOL = (
    "输出协议：你的回复必须是一个 JSON 对象，不要 Markdown 代码块，不要多余文字。\n"
    "调用工具：{\"tool\": \"fs.write\", \"args\": {\"path\": \"notes/x.md\", "
    "\"content\": \"...\"}}\n"
    "结束任务：{\"finish\": {\"summary\": \"一句话结果\", \"status\": \"done\"}}\n"
    "status 只能是 done、failed、blocked 之一。\n"
    "要求：每次只调用一个工具；args 必须是对象；路径相对项目 workspace；"
    "summary 写真实完成的内容与证据（文件路径/命令结果）。"
)


def tool_call_protocol() -> str:
    """Return protocol text plus the concrete tool names available."""
    try:
        from ..agents.tools import TOOLS
    except Exception:  # pragma: no cover - import guard for standalone tests
        TOOLS = {}
    if not TOOLS:
        return _ACTION_PROTOCOL
    lines = [f"- {spec.name}（{spec.capability}）：{spec.description}"
             for spec in sorted(TOOLS.values(), key=lambda item: item.name)]
    return _ACTION_PROTOCOL + "\n可用工具：\n" + "\n".join(lines)


def role_prompt(role: str) -> str:
    """Return the role-specific system prompt (falls back to a generic one)."""
    try:
        definition = get_role(role)
    except BadRequest:
        return ("你是 StackFlowOS 的一个 Agent。"
                "只做任务要求的工作，使用工具留下证据，完成后 finish。")
    return (f"{definition.system_prompt}\n\n"
            f"你的角色：{definition.name}（{definition.display}）。"
            f"允许的工具：{', '.join(definition.tools)}。")


__all__ = ["SYSTEM_PROMPT_OS", "role_prompt", "tool_call_protocol"]
