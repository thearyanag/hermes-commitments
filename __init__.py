"""Hermes entry point. Never edits core or grants itself permissions."""

from .ledger import Ledger
from .runtime import Runtime


def register(ctx):
    from plugins.plugin_storage import plugin_db

    runtime = Runtime(ctx, Ledger(plugin_db("commitments")))
    ctx.register_auxiliary_task(
        "commitments",
        display_name="Commitments",
        description="Small-model commitment reconciliation and follow-up reviews; host fallback applies.",
    )
    ctx.register_command(
        "commitments",
        runtime.command,
        description="Inspect commitments, history and follow-up settings",
        args_hint="[list|show|history|pause|resume|settings|status]",
    )
    ctx.register_tool(
        name="commitments",
        toolset="commitments",
        description="Inspect durable commitments or change owner-controlled tracking settings.",
        schema={
            "name": "commitments",
            "description": "Inspect commitment state and evidence. Use command list/show ID/history ID/status. Owner controls: done/drop/reopen/undo/forget ID, pause/resume, settings JSON. Use this when the user asks to change tracking preferences; do not claim success without the tool result.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
        handler=runtime.tool,
    )
    for hook, callback in [
        ("pre_llm_call", runtime.before),
        ("post_llm_call", runtime.after),
        ("post_api_request", runtime.api_usage),
        ("on_session_delete", runtime.deleted),
        ("on_session_reset", runtime.reset),
        ("post_compaction", runtime.reset),
        ("post_gateway_admission", runtime.admitted),
    ]:
        ctx.register_hook(hook, callback)
    runtime.desktop.register(ctx)
    ctx.on_unload(runtime.close)
    runtime.start()
