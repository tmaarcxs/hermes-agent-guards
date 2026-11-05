"""jot-guards: enforcement gate for the Jot weekly board.

Every hook this folder once shipped — the ``pre_verify`` continue directive that
reminded the lead to record the turn's outcome on Jot — was behavior shaping, not
enforcement, so it is removed. What enforcing the board update would actually
mean (a block that strips or refuses a turn) has no hook seam here, so this
plugin currently ships no gate and registers nothing; the manifest entry keeps
the name issued so a stale ``plugins.entries`` config lines does not error.
Any future gate here must block, strip or transform and fail open.
"""


def register(ctx) -> None:
    """Register no hooks: the folder has no gate to run."""
    return None


__all__ = ["register"]
