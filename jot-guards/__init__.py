"""jot-guards: keep the Jot weekly board current before a work turn ends.

One gate only: ``pre_verify`` (the ``stop_gate`` sibling in ``kanban-guards`` rides the same
hook) asks the agent to record its outcome on Jot when the turn changed files in a work
repository and called no mutating Jot tool. ``post_tool_call`` feeds it the turn's Jot usage.
Imports are lazy so a bare ``pytest`` collecting this ``__init__.py`` as a top-level package
still imports cleanly. Every callback fails open: a broken check can never wedge the turn.
"""


def register(ctx) -> None:
    from . import jot_nudge

    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("pre_verify", jot_nudge.check)


def _post_tool_call(**kwargs) -> None:
    try:
        from . import jot_nudge

        jot_nudge.record(**kwargs)
    except Exception:
        pass


__all__ = ["register"]
