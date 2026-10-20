"""``kanban_create`` workspace paths must follow the shared placement convention.

The guard rejects a ``dir``/``worktree`` card whose explicit ``workspace_path``
is outside ``~/git_repos/worktrees/<repo>/<task>/`` or ``~/git_repos/ad-hoc/``,
is a main checkout, or is the home directory or a direct child of it. The
allowed cases (a linked worktree, an ad-hoc spike dir, the default scratch, a
card that lets the board derive its path) must stay untouched. Roots are pinned
to a tmp home so the suite never reads the operator's real layout.
"""

import sys

import pytest

guards = sys.modules["agent_guards"]
gate = guards.workspace_convention


@pytest.fixture(autouse=True)
def convention_home(monkeypatch, tmp_path):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_REAL_HOME", str(home))
    monkeypatch.setenv("AGENT_GUARDS_WORKTREE_ROOT", str(home / "git_repos" / "worktrees"))
    monkeypatch.setenv("AGENT_GUARDS_ADHOC_ROOT", str(home / "git_repos" / "ad-hoc"))
    return home


def blocked(args):
    result = guards._pre_tool_call(tool_name="kanban_create", args=args)
    assert result is not None and result["action"] == "block", result
    return result["message"]


def allowed(args):
    assert guards._pre_tool_call(tool_name="kanban_create", args=args) is None


BASE = {"title": "t", "assignee": "worker"}


def main_checkout(home):
    """A repo whose ``.git`` is a real directory (a main checkout)."""
    repo = home / "git_repos" / "mic-auditqc"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_allowed_linked_worktree_path_passes(convention_home):
    allowed({**BASE, "workspace_kind": "worktree",
             "workspace_path": str(convention_home / "git_repos" / "worktrees" / "mic-auditqc" / "t_1")})


def test_allowed_adhoc_path_passes(convention_home):
    allowed({**BASE, "workspace_kind": "dir",
             "workspace_path": str(convention_home / "git_repos" / "ad-hoc" / "spike-1")})


def test_scratch_default_without_path_passes(convention_home):
    allowed({**BASE, "workspace_kind": "scratch"})


def test_dir_card_without_path_is_left_to_the_board(convention_home):
    allowed({**BASE, "workspace_kind": "dir"})


def test_dir_under_worktrees_root_passes(convention_home):
    allowed({**BASE, "workspace_kind": "dir",
             "workspace_path": str(convention_home / "git_repos" / "worktrees" / "spa-auditqc" / "t_2")})


def test_other_tools_and_kinds_untouched(convention_home):
    assert gate.check("terminal", {"workspace_kind": "dir", "workspace_path": str(convention_home)}) is None
    assert gate.check("kanban_comment", {"workspace_kind": "worktree", "workspace_path": "/"}) is None


def test_main_checkout_rejected(convention_home):
    message = blocked({**BASE, "workspace_kind": "dir",
                       "workspace_path": str(main_checkout(convention_home))})
    assert "main checkout" in message


def test_home_directory_rejected(convention_home):
    message = blocked({**BASE, "workspace_kind": "dir",
                       "workspace_path": str(convention_home)})
    assert "home directory itself" in message


def test_direct_child_of_home_rejected(convention_home):
    child = convention_home / "scratchpad"
    child.mkdir()
    message = blocked({**BASE, "workspace_kind": "dir", "workspace_path": str(child)})
    assert "direct child of the home directory" in message


def test_worktree_outside_worktrees_root_rejected(convention_home):
    message = blocked({**BASE, "workspace_kind": "worktree",
                       "workspace_path": str(convention_home / "git_repos" / "ad-hoc" / "x")})
    assert "workspace_kind=worktree must live under" in message


def test_repo_dot_worktrees_dir_rejected(convention_home):
    message = blocked({**BASE, "workspace_kind": "worktree",
                       "workspace_path": str(convention_home / "git_repos" / "mic-auditqc" / ".worktrees" / "t_3")})
    assert "workspace_kind=worktree must live under" in message


def test_bare_repo_root_rejected(convention_home):
    repo = main_checkout(convention_home)
    message = blocked({**BASE, "workspace_kind": "dir", "workspace_path": str(repo)})
    assert "main checkout" in message


def test_message_names_every_allowed_location_and_scratch_default(convention_home):
    message = blocked({**BASE, "workspace_kind": "dir",
                       "workspace_path": str(main_checkout(convention_home))})
    assert "~/git_repos/worktrees/<repo>/<task>/" in message
    assert "~/git_repos/ad-hoc/<name>/" in message
    assert "~/.hermes/kanban/workspaces/<task>" in message
    assert "Use scratch by default" in message


def test_message_carries_the_retry_instruction(convention_home):
    from test_retry_instruction import assert_retry_instruction

    message = blocked({**BASE, "workspace_kind": "dir",
                       "workspace_path": str(convention_home)})
    assert_retry_instruction(message)


def test_block_is_attributed_to_the_guard(convention_home):
    message = blocked({**BASE, "workspace_kind": "dir",
                       "workspace_path": str(convention_home)})
    assert message.startswith("agent-guards blocked kanban_create [workspace_convention]:")


def test_inactive_without_configured_worker(monkeypatch, convention_home):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert gate.check("kanban_create", {**BASE, "workspace_kind": "dir",
                                        "workspace_path": str(convention_home)}) is None


def test_non_absolute_path_fails_open(convention_home):
    assert gate.check("kanban_create", {**BASE, "workspace_kind": "dir", "workspace_path": "relative/x"}) is None


def test_internal_error_fails_open(monkeypatch, convention_home):
    def boom(*a, **k):
        raise RuntimeError("probe crash")

    monkeypatch.setattr(gate, "violations", boom)
    assert guards._pre_tool_call(
        tool_name="kanban_create",
        args={**BASE, "workspace_kind": "dir", "workspace_path": str(convention_home)}) is None
