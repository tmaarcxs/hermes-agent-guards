"""goal-waker: wake a parked goal when its barrier lifts (see waker.py)."""


def register(ctx) -> None:
    from . import waker

    waker.CTX = ctx
    ctx.register_hook("on_kanban_dispatch_tick", waker.on_tick)
