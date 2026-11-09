"""jot-reviewer: background review of the weekly Jot board after each gateway turn (see reviewer.py)."""


def register(ctx) -> None:
    from . import reviewer

    reviewer.CTX = ctx
    ctx.register_hook("pre_gateway_dispatch", reviewer.on_pre_gateway_dispatch)
    ctx.register_hook("post_llm_call", reviewer.on_post_llm_call)
