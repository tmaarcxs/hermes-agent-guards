"""hindsight-reviewer: background review of injected Hindsight memories (see reviewer.py)."""


def register(ctx) -> None:
    from . import reviewer

    reviewer.CTX = ctx
    try:
        reviewer.patch_provider()
    except Exception:
        reviewer.logger.warning('Hindsight provider not available; the reviewer stays idle', exc_info=True)
    ctx.register_hook("pre_gateway_dispatch", reviewer.on_pre_gateway_dispatch)
    ctx.register_hook("post_llm_call", reviewer.on_post_llm_call)
