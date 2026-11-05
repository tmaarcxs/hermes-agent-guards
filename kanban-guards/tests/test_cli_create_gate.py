"""The lead terminal cannot create cards with the CLI; the tool alternative must fit.

Failing-first suite for ``cli_create_gate`` (t_e0ffa375). The blocked command's
own flags must reappear in the block message as a copyable ``kanban_create``
tool call — wrong title/assignee/workspace/idempotency/parents in the message is
a defect, not a wording nit.
"""

import sys

import pytest

guards = sys.modules["kanban_guards"]
gate = guards.cli_create_gate


def run(command, tool="terminal"):
    return gate.check(tool, {"command": command})


@pytest.fixture(autouse=True)
def lead_setup(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)


SIMPLE = 'hermes kanban create "My card" --assignee worker'

BLOCKED_SHAPES = [
    SIMPLE,
    "hermes -p default kanban create 'T' --assignee worker",
    "hermes kanban --board work create t --assignee worker",
    "hermes --board work kanban create t --assignee worker",
    "python -m hermes_cli kanban create t --assignee worker",
    "python3 -m hermes_cli.main kanban create t --assignee worker",
    "cd ~ && hermes kanban create t --assignee worker",
    "echo x; hermes kanban create t --assignee worker",
    "git status | hermes kanban create t --assignee worker",
    "(hermes kanban create t --assignee worker)",
    "bash -c \"hermes kanban create t --assignee worker\"",
    "sh -c 'hermes kanban create t --assignee worker'",
    "HERMES_KANBAN_BOARD=x hermes kanban create t --assignee worker",
    "sudo hermes kanban create t --assignee worker",
    'hermes kanban create "Fan-out" --assignee worker '
    "--workspace worktree:/Users/x/repo/.worktrees/w --branch wt/w "
    "--idempotency-key key-1 --parent t_1 --parent t_2 "
    "--project proj --skill s1 --skill s2 --goal",
    'hermes kanban create "File body" --assignee worker --body-file /tmp/body.md',
    'hermes kanban create "Scratch" --assignee worker --workspace scratch',
    "hermes kanban create t --assignee worker --assignee reviewer",
]


@pytest.mark.parametrize("command", BLOCKED_SHAPES)
def test_lead_cli_create_is_blocked(command):
    result = run(command)
    assert result is not None and result.startswith("agent-guards blocked terminal")


def test_message_names_the_exact_tool_call():
    message = run(SIMPLE)
    assert 'kanban_create(title="My card", assignee="worker"' in message
    assert "kanban_create tool" in message
    assert "no chat subscribed" in message


def test_branch_flag_is_read_and_honestly_mapped():
    message = run("hermes kanban create t --assignee worker --branch wt/x --workspace worktree:/p/w")
    assert "--branch wt/x" in message or "worktree branch is derived" in message
    assert 'workspace_kind="worktree"' in message


def test_message_flags_are_built_from_the_command():
    message = run(
        'hermes kanban create "Fan-out" --assignee worker '
        "--workspace worktree:/Users/x/repo/.worktrees/w --branch wt/w "
        "--idempotency-key key-1 --parent t_1 --parent t_2 --project proj"
    )
    assert 'workspace_kind="worktree"' in message
    assert 'workspace_path="/Users/x/repo/.worktrees/w"' in message
    assert 'idempotency_key="key-1"' in message
    assert 'parents=["t_1", "t_2"]' in message
    assert 'project="proj"' in message
    assert 'assignee="worker"' in message
    assert 'title="Fan-out"' in message


def test_message_covers_skills_goal_and_repeated_last_flag_wins():
    message = run(
        "hermes kanban create t --assignee worker --skill s1 --skill s2 --goal "
        "--idempotency-key first --idempotency-key second"
    )
    assert 'skills=["s1", "s2"]' in message
    assert "goal_mode=True" in message
    assert 'idempotency_key="second"' in message


def test_workspace_value_forms_map_onto_kind_and_path():
    message = run('hermes kanban create t --assignee worker --workspace scratch')
    assert 'workspace_kind="scratch"' in message and "workspace_path" not in message
    message = run('hermes kanban create t --assignee worker --workspace worktree')
    assert 'workspace_kind="worktree"' in message and "workspace_path" not in message
    message = run('hermes kanban create t --assignee worker --workspace dir:/abs/path')
    assert 'workspace_kind="dir"' in message and 'workspace_path="/abs/path"' in message


def test_body_file_message_points_at_the_body_argument():
    message = run('hermes kanban create "File body" --assignee worker --body-file /tmp/body.md')
    assert "--body-file" in message and "body argument" in message


def test_repeated_assignee_last_flag_wins():
    message = run("hermes kanban create t --assignee worker --assignee reviewer")
    assert 'assignee="reviewer"' in message


WORKER_SHAPES = [
    'hermes kanban create t --assignee worker',
    'cd $HERMES_KANBAN_WORKSPACE && hermes kanban create t --assignee worker',
]


@pytest.mark.parametrize("command", WORKER_SHAPES)
def test_worker_run_keeps_the_cli(command, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    assert run(command) is None


@pytest.mark.parametrize("profile", ["reviewer", "worker"])
def test_profile_pinned_profile_without_a_task_keeps_the_decision_per_identity(profile, monkeypatch):
    """The card's identity formula: block iff the task is unpinned AND the profile is
    not the configured worker. A pinned worker profile is a worker run (pass); a
    pinned reviewer profile is no card run either way the lead formula reads — it
    enforces, so only the task-pinned reviewer run is exempt."""
    monkeypatch.setenv("HERMES_PROFILE", profile)
    if profile == "worker":
        assert run(SIMPLE) is None
    else:
        assert run(SIMPLE) is not None


def test_reviewer_run_on_a_card_keeps_the_cli(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "reviewer")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review")
    assert run(SIMPLE) is None


def test_unpinned_or_lead_profile_enforces(monkeypatch):
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    assert run(SIMPLE) is not None
    monkeypatch.setenv("HERMES_PROFILE", "default")
    assert run(SIMPLE) is not None


ALLOWED = [
    "hermes kanban show t_x",
    "hermes kanban comment t_x --body 'hi'",
    "hermes kanban list",
    "hermes kanban notify-subscribe t_x --platform telegram --chat-id 1",
    "hermes kanban create --help",
    "hermes kanban --help",
    "hermes kanban list --help; hermes kanban create --help",
    "echo 'hermes kanban create t'",
    "echo he said kanban create is nice",
    "grep -rn 'kanban create' ~/.hermes/skills",
    "hermes kanban boards create scratchpad",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_other_subcommands_and_help_pass(command):
    assert run(command) is None


def test_worker_setup_enforcement_is_off_without_config(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert run(SIMPLE) is None


def test_garbage_fails_open():
    assert gate.check("terminal", {"command": 42}) is None
    assert gate.check("terminal", None) is None
    assert gate.check("kanban_create", {"command": SIMPLE}) is None


def test_unbalanced_command_shape_fails_open():
    assert run('hermes kanban create "unterminated --assignee worker') is None


def test_entry_point_stamps_the_guard_name():
    result = guards._pre_tool_call("terminal", {"command": SIMPLE})
    assert result is not None and result["action"] == "block"
    assert "[cli_create_gate]" in result["message"]
    assert run(SIMPLE).split(": ", 1)[1] in result["message"]
