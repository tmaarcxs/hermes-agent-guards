"""jot-reviewer: background review of the weekly Jot board after each gateway turn (see reviewer.py)."""


def register(ctx) -> None:
    from . import reviewer

    reviewer.CTX = ctx
    ctx.register_hook("pre_gateway_dispatch", reviewer.on_pre_gateway_dispatch)
    ctx.register_hook("post_llm_call", reviewer.on_post_llm_call)
    ctx.register_hook("pre_tool_call", _pre_tool_call)


def _pre_tool_call(tool_name="", args=None, **kwargs):
    from . import writer_gate
    try:
        blocked = writer_gate.check(tool_name, args)
    except Exception:
        return None
    return {"action": "block", "message": blocked} if blocked else None
