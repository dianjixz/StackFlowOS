"""Agent runtime: roster model, roles, tools, brains, runner and supervisor."""

from __future__ import annotations

from .api import (SysAPI, call_ok, emit_event, poll_approval,
                  request_permission)
from .brain import Action, AgentView, Brain, MockBrain
from .models import (AGENT_STATUSES, TERMINAL_AGENT_STATUSES, Agent,
                     display_name_for)
from .roles import ROLES, TASK_ROLES, RoleDef, get_role, role_names
from .runner import AgentRunner, RunResult
from .supervisor import AgentSupervisor
from .tools import (TOOLS, ToolError, ToolExecutor, ToolResult, ToolSpec,
                    get_tool, tools_for)

__all__ = [
    "AGENT_STATUSES",
    "ROLES",
    "TASK_ROLES",
    "TERMINAL_AGENT_STATUSES",
    "TOOLS",
    "Action",
    "Agent",
    "AgentRunner",
    "AgentSupervisor",
    "AgentView",
    "Brain",
    "MockBrain",
    "RoleDef",
    "RunResult",
    "SysAPI",
    "ToolError",
    "ToolExecutor",
    "ToolResult",
    "ToolSpec",
    "call_ok",
    "display_name_for",
    "emit_event",
    "get_role",
    "get_tool",
    "poll_approval",
    "request_permission",
    "role_names",
    "tools_for",
]
