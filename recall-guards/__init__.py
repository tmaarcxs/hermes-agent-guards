"""recall-guards: stop the agent asking for what its own memory already answers.

Three hooks over two gates:

- ``post_tool_call`` records the ``(session_id, turn_id)`` of every memory call,
  so the turn in which the agent actually consulted memory is known.
- ``pre_tool_call`` blocks a ``clarify`` call when the current turn has read no
  memory. The block is not a refusal to ask — it is an order to read memory first
  and then ask in the same turn if memory does not answer.
- ``transform_tool_result`` appends an advisory reminder on the first tool call of
  every 5th user turn of a session, telling the agent to consult Hindsight.

Lead surfaces only: a worker answers to its card. Every gate fails open.
"""

from typing import Optional


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("transform_tool_result", _transform_tool_result)


def _post_tool_call(**kwargs) -> None:
    try:
        from . import recall_guard

        recall_guard.record(**kwargs)
    except Exception:
        pass


def _pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **kwargs) -> Optional[dict]:
    try:
        from . import recall_guard

        return recall_guard.check(tool_name, args, **kwargs)
    except Exception:
        return None


def _transform_tool_result(**kwargs) -> Optional[str]:
    try:
        from . import recall_guard

        return recall_guard.transform(**kwargs)
    except Exception:
        return None


__all__ = ["register"]
