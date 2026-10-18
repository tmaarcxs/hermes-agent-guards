"""GC-1 regression tests — malformed flag shapes near contact flags (t_efb9f7a4).

At the pre-fix HEAD (17b80b5) every command in ``CRASH`` raised IndexError inside
``github_contact._flags`` (the ``A and B or C`` precedence bug had no bounds check
on the trailing branch), which the host ``__init__.py`` hook harness swallows into
a silent pass — the contact command executed unblocked. Other shapes
(``--add-reviewer -r 4``, ``--add-reviewer '-p'``) false-consumed their next
option-like token as the reviewer value; under this fix that behavior is preserved
(same recorded value, still a block), only without the crash risk.

The fix records an empty value for the un-verifiable shapes; ``_contacts`` then
blocks (fail-closed). These tests pin the shapes from the t_ebe70bcb audit
(q_robustness.py GC-1 battery + robustness_transcript.txt) plus a compact
token-level battery over the ``_flags`` branches.
"""

import sys

import pytest

guards = sys.modules["agent_guards"]
gc = guards.github_contact


def run(command, workdir=None):
    return guards._pre_tool_call(
        tool_name="terminal", args={"command": command, "workdir": workdir}
    )


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_dem0002")
    monkeypatch.setattr(gc, "_login_cache", "nobody-here")
    monkeypatch.setattr(gc, "_login_resolved", True)


CRASH = [
    "gh pr edit 1 --add-reviewer",
    "gh pr create --reviewer",
    "gh issue create --assignee",
    "gh issue edit 3 --add-assignee",
    "GH_REPO=o/r gh pr edit 1 --add-reviewer",
    "env gh issue create --assignee",
    "cd x && gh issue create --assignee",
    "gh issue create --assignee x && gh pr edit 1 --add-reviewer",
]


@pytest.mark.parametrize("command", CRASH)
def test_trailing_flag_no_longer_crashes_or_silently_passes(command):
    """Shapes that raised IndexError pre-fix; fail-closed blocks afterwards."""
    result = run(command)  # would raise IndexError pre-fix
    assert result is not None and result["action"] == "block"
    assert "needs the owner's approval" in result["message"]


SWALLOW = [
    "gh pr edit 1 --add-reviewer -r 4",
    "gh pr edit 1 --add-reviewer '-p'",
]


@pytest.mark.parametrize("command", SWALLOW)
def test_swallowed_value_shape_still_blocks(command):
    """Option-like next tokens stay recorded verbatim and still block."""
    result = run(command)
    assert result is not None and result["action"] == "block"


@pytest.mark.parametrize(
    "tokens,flag,expected",
    [
        ([("--add-reviewer", False)], "--add-reviewer", [""]),
        ([("--add-reviewer", False), ("-r", False), ("4", False)], "--add-reviewer", ["-r"]),
        ([("--add-reviewer", False), ("alice", False)], "--add-reviewer", ["alice"]),
        ([("--add-reviewer", False), ("-persona", True)], "--add-reviewer", [""]),
        ([("--add-reviewer", False), ("", False)], "--add-reviewer", [""]),
        ([("-r", False), ("4", False)], "-r", ["4"]),
        ([("--add-reviewer=alice", False)], "--add-reviewer", ["alice"]),
        ([("--add-reviewer", False)], "-r", []),
    ],
)
def test_flags_values_consume_branches(tokens, flag, expected):
    """Token-level battery over every ``_flags`` branch (post-fix semantics)."""
    values = gc._flags(tokens, {"--add-reviewer": "reviewer", "-r": "reviewer"})
    assert values.get(flag, []) == expected
