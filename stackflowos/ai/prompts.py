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
    "5) 没有可推进的事项时立即 finish，并说明原因；\n"
    "6) 工具返回 ok=false 时先读 error 再修正参数重试；同一个失败动作不要重复三次，"
    "仍无法完成就 finish(status=blocked) 并说明原因。"
)

#: Text describing the JSON action protocol.
_ACTION_PROTOCOL = (
    "输出协议：你的回复必须是一个 JSON 对象，不要 Markdown 代码块，不要多余文字。\n"
    "调用工具：{\"tool\": \"fs.write\", \"args\": {\"path\": \"notes/x.md\", "
    "\"content\": \"...\"}, \"say\": \"（可选）一句话说明你在做什么\"}\n"
    "结束任务：{\"finish\": {\"summary\": \"一句话结果\", \"status\": \"done\"}}\n"
    "status 只能是 done、failed、blocked 之一。\n"
    "每次工具调用后，系统会把工具结果（ok/data/error）作为下一条消息发给你；"
    "据此决定下一步，不要重复已成功的动作。\n"
    "要求：每次只调用一个工具；args 必须是对象；路径相对项目 workspace；"
    "文件内容保持精炼，避免一次写入过长内容；"
    "summary 写真实完成的内容与证据（文件路径/命令结果）。"
)


def _signature(spec) -> str:
    args = ", ".join(f"{name}:{kind}" for name, kind in (spec.args or {}).items())
    return f"{spec.name}({args})"


def tool_call_protocol(role: str | None = None) -> str:
    """Return protocol text plus the tools available (all, or those of ``role``)."""
    try:
        from ..agents.tools import TOOLS, tools_for
    except Exception:  # pragma: no cover - import guard for standalone tests
        return _ACTION_PROTOCOL
    specs = tools_for(role) if role else []
    if not specs:
        specs = sorted(TOOLS.values(), key=lambda item: item.name)
    if not specs:
        return _ACTION_PROTOCOL
    lines = [f"- {_signature(spec)}：{spec.description}" for spec in specs]
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
