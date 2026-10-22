"""GC-1 regression tests: the no-value contact-flag branch (t_efb9f7a4).

At the 17b80b5 baseline every command in ``NO_VALUE_LONG`` raised IndexError in
``github_contact._flags``, which the host ``__init__.py`` hook harness swallows
into a silent per-command pass. The decision in the task card: a contact flag
with no value contacts nobody and must PASS through a deliberate code branch,
with every other verdict unchanged.

Structure:
- ``test_passes_no_value_flag`` guards the red→green flip (RED on 17b80b5: the
  run raised IndexError; GREEN after this commit: run() returns None).
- ``test_verdicts_identical_for_person_and_placeholder_values`` pins the
  kept-verdict matrix named in the acceptance list.
- ``test_flags_values_consume_branches`` pins the token-level ``_flags``
  branches (None for a trailing flag, verbatim next token only when unquoted
  and non-empty).

Shapes come from the t_ebe70bcb audit evidence (q_robustness.py GC-1 battery
and its transcript).
"""

import sys

import pytest

guards = sys.modules["kanban_guards"]
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


NO_VALUE_LONG = [
    "gh pr edit 1 --add-reviewer",
    "gh pr create --reviewer",
    "gh issue create --assignee",
    "gh issue edit 3 --add-assignee",
    "GH_REPO=o/r gh pr edit 1 --add-reviewer",
    "env gh issue create --assignee",
    "cd x && gh issue create --assignee",
]

NO_VALUE_TOKENS = [
    [("--add-reviewer", False)],
    [("--reviewer", False)],
    [("-a", False)],
    [("--add-assignee", False)],
]


@pytest.mark.parametrize("tokens", NO_VALUE_TOKENS)
def test_flags_records_none_when_no_value_token_exists(tokens):
    """
    A trailing bare contact flag yields exactly one recorded None: name keyed,
    no crash, no false value in the token stream.
    """
    values = gc._flags(tokens, {"--add-reviewer": "reviewer", "--reviewer": "reviewer",
                                "-a": "assignee", "--add-assignee": "assignee"})
    assert list(values.keys()) == [tokens[0][0]]
    assert values[tokens[0][0]] == [None]


@pytest.mark.parametrize("command", NO_VALUE_LONG)
def test_passes_no_value_flag(command):
    """
    RED-then-GREEN anchor: on 17b80b5 run() raised IndexError instead of
    returning None; the no-value branch must PASS every one of these shapes.
    """
    assert run(command) is None


@pytest.mark.parametrize(
    "command,expectation",
    [
        ("gh pr review 5 --approve", "block"),
        ("gh pr edit 1 --add-reviewer alice-dev", "block"),
        ("gh pr edit 1 --add-reviewer -r 4", "block"),
        ("gh pr edit 1 --add-reviewer '-persona'", "block"),
        ("gh pr edit 1 --add-reviewer=alice-dev", "block"),
        ("gh pr create --reviewer reviewer-one", "block"),
        ("gh issue create --assignee reviewer-one -t t", "block"),
        ("gh pr create --reviewer @me", "pass"),
        ("gh pr edit 1 --add-reviewer=@me", "pass"),
        ("gh issue edit 3 --remove-assignee someone", "pass"),
        ("gh issue create -t t", "pass"),
    ],
)
def test_verdicts_identical_for_person_and_placeholder_values(command, expectation):
    """Every verdict outside the no-value branch must remain exactly as shipped."""
    result = run(command)
    if expectation == "block":
        assert result is not None and result["action"] == "block"
    else:
        assert result is None


@pytest.mark.parametrize(
    "tokens,flag,expected",
    [
        ([("--add-reviewer", False)], "--add-reviewer", [None]),
        ([("--add-reviewer", False), ("-r", False), ("4", False)], "--add-reviewer", ["-r"]),
        ([("--add-reviewer", False), ("alice", False)], "--add-reviewer", ["alice"]),
        ([("--add-reviewer", False), ("-persona", True)], "--add-reviewer", [None]),
        ([("--add-reviewer", False), ("", False)], "--add-reviewer", [None]),
        ([("-r", False), ("4", False)], "-r", ["4"]),
        ([("--add-reviewer=alice", False)], "--add-reviewer", ["alice"]),
    ],
)
def test_flags_values_consume_branches(tokens, flag, expected):
    """Token-level battery over the _flags branches (recorded values)."""
    values = gc._flags(tokens, {"--add-reviewer": "reviewer", "-r": "reviewer"})
    assert values.get(flag, []) == expected
