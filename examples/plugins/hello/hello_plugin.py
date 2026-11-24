"""Reference in-process StackFlowOS plugin (trusted example).

``apply(ctx, provides)`` runs inside the hub.  It contributes one executable
catalogue tool (``plugin.echo``) and one skill, and publishes a tiny service on
its Cordis child context.  The loader disposes everything below that context on
unload, so this file intentionally returns no extra disposer: the ``provides``
registrations already own their lifetime.
"""

from __future__ import annotations


def apply(ctx, provides):
    ctx.provide("plugin.hello", {"version": "0.1.0"})

    def echo(args, **kwargs):
        return {
            "echo": str(args.get("text", "")),
            "caller": kwargs.get("principal"),
            "project_id": kwargs.get("project_id"),
        }

    provides.tool(
        "plugin.echo",
        description="回显一段文本（示例插件贡献的工具）",
        capability="shell.exec",
        risk="medium",
        schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=echo,
    )
    provides.skill({
        "id": "hello-skill",
        "name": "Hello Skill",
        "version": "0.1.0",
        "description": "示例 skill：说明插件如何贡献只读提示词。",
        "instructions": "当用户向你打招呼时，用一句话回应并说明这是 hello 插件提供的技能。",
        "triggers": ["hello", "你好"],
        "allowed_roles": [],
    })
    return None
