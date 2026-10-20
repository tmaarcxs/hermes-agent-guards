"""``kanban_create`` workspace paths must follow the shared placement convention.

The guard rejects a ``dir``/``worktree`` card whose explicit ``workspace_path``
is outside ``<main-checkout>/.worktrees/<task>/`` or ``~/git_repos/ad-hoc/``,
is a main checkout, or is the home directory or a direct child of it. The
allowed cases (a canonical linked worktree, an ad-hoc dir, the default scratch,
a card that lets the board derive its path) must stay untouched. Roots are
pinned to a tmp home so the suite never reads the operator's real layout.
"""

import os
import sys
from pathlib import Path

import pytest

guards = sys.modules["agent_guards"]
gate = guards.workspace_convention


@pytest.fixture(autouse=True)
def convention_home(monkeypatch, tmp_path):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_REAL_HOME", str(home))
    monkeypatch.setenv("AGENT_GUARDS_ADHOC_ROOT", str(home / "git_repos" / "ad-hoc"))
    return home


def blocked(args):
    result = guards._pre_tool_call(tool_name="kanban_create", args=args)
    assert result is not None and result["action"] == "block", result
    return result["message"]


def allowed(args):
    assert guards._pre_tool_call(tool_name="kanban_create", args=args) is None


BASE = {"title": "t", "assignee": "worker"}


def main_checkout(home, name="mic-auditqc"):
    """A repo whose ``.git`` is a real directory (a main checkout)."""
    repo = home / "git_repos" / name
    (repo / ".git").mkdir(parents=True)
    return repo


def canonical_worktree(home, task="t_1", name="mic-auditqc"):
    """``<main-checkout>/.worktrees/<task>/`` — the convention's repo path."""
    return main_checkout(home, name) / ".worktrees" / task


def test_allowed_canonical_worktree_dir_passes(convention_home):
    allowed({**BASE, "workspace_kind": "dir",
             "workspace_path": str(canonical_worktree(convention_home))})


def test_allowed_canonical_worktree_kind_passes(convention_home):
    allowed({**BASE, "workspace_kind": "worktree",
             "workspace_path": str(canonical_worktree(convention_home))})


def test_allowed_canonical_worktree_any_repo_passes(convention_home):
    allowed({**BASE, "workspace_kind": "dir",
             "workspace_path": str(canonical_worktree(convention_home, "t_2", "spa-auditqc"))})


def test_allowed_adhoc_path_passes(convention_home):
    allowed({**BASE, "workspace_kind": "dir",
             "workspace_path": str(convention_home / "git_repos" / "ad-hoc" / "spike-1")})


def test_scratch_default_without_path_passes(convention_home):
    allowed({**BASE, "workspace_kind": "scratch"})


def test_dir_card_without_path_is_left_to_the_board(convention_home):
    allowed({**BASE, "workspace_kind": "dir"})


def test_worktree_card_without_path_is_left_to_the_board(convention_home):
    allowed({**BASE, "workspace_kind": "worktree"})


def test_other_tools_and_kinds_untouched(convention_home):
    assert gate.check("terminal", {"workspace_kind": "dir", "workspace_path": str(convention_home)}) is None
    assert gate.check("kanban_comment", {"workspace_kind": "worktree", "workspace_path": "/"}) is None


def test_main_checkout_rejected(convention_home):
    message = blocked({**BASE, "workspace_kind": "dir",
                       "workspace_path": str(main_checkout(convention_home))})
    assert "main checkout" in message


def test_dir_at_main_checkout_root_rejected(convention_home):
    message = blocked({**BASE, "workspace_kind": "dir",
                       "workspace_path": str(main_checkout(convention_home))})
    assert "workspace_kind=dir must live at" in message


def test_worktree_at_main_checkout_root_rejected(convention_home):
    message = blocked({**BASE, "workspace_kind": "worktree",
                       "workspace_path": str(main_checkout(convention_home))})
    assert "workspace_kind=worktree must live at" in message


def test_home_directory_rejected(convention_home):
    message = blocked({**BASE, "workspace_kind": "dir",
                       "workspace_path": str(convention_home)})
    assert "home directory itself" in message


def test_direct_child_of_home_rejected(convention_home):
    child = convention_home / "scratchpad"
    child.mkdir()
    message = blocked({**BASE, "workspace_kind": "dir", "workspace_path": str(child)})
    assert "direct child of the home directory" in message


def test_central_sibling_worktrees_root_rejected(convention_home):
    """The old ``~/git_repos/worktrees/<repo>/<task>/`` is NOT the convention."""
    message = blocked({**BASE, "workspace_kind": "dir",
                       "workspace_path": str(convention_home / "git_repos" / "worktrees" / "mic-auditqc" / "t_1")})
    assert "workspace_kind=dir must live at" in message


def test_worktree_outside_the_worktrees_rule_rejected(convention_home):
    message = blocked({**BASE, "workspace_kind": "worktree",
                       "workspace_path": str(convention_home / "git_repos" / "ad-hoc" / "x")})
    assert "workspace_kind=worktree must live at" in message


def test_dot_worktrees_under_a_non_main_checkout_rejected(convention_home):
    """``.worktrees`` below a plain directory (no main checkout) is not canonical."""
    fake = convention_home / "git_repos" / "not-a-repo" / ".worktrees" / "t_3"
    fake.mkdir(parents=True)
    message = blocked({**BASE, "workspace_kind": "dir", "workspace_path": str(fake)})
    assert "workspace_kind=dir must live at" in message


def test_nested_dot_worktrees_rejected(convention_home):
    """A path nested inside another ``.worktrees`` never qualifies."""
    nested = canonical_worktree(convention_home) / ".worktrees" / "t_4"
    message = blocked({**BASE, "workspace_kind": "dir", "workspace_path": str(nested)})
    assert "workspace_kind=dir must live at" in message


def test_bare_dot_worktrees_root_rejected(convention_home):
    """``<repo>/.worktrees`` with no task segment is not a task workspace."""
    bare = main_checkout(convention_home) / ".worktrees"
    bare.mkdir()
    message = blocked({**BASE, "workspace_kind": "worktree", "workspace_path": str(bare)})
    assert "workspace_kind=worktree must live at" in message


def test_hermes_tmp_study_dir_rejected(convention_home):
    tmp_study = convention_home / ".hermes" / "tmp" / "jev-auditqc-study"
    message = blocked({**BASE, "workspace_kind": "dir", "workspace_path": str(tmp_study)})
    assert "workspace_kind=dir must live at" in message


def test_message_names_every_allowed_location_and_scratch_default(convention_home):
    message = blocked({**BASE, "workspace_kind": "dir",
                       "workspace_path": str(main_checkout(convention_home))})
    assert "<main-checkout>/.worktrees/<task>/" in message
    assert "~/git_repos/ad-hoc/<name>/" in message
    assert "~/.hermes/kanban/workspaces/<task>" in message
    assert "Use scratch by default" in message
    assert "~/git_repos/worktrees/<repo>/<task>/" not in message


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


def test_guard_roots_match_the_git_ops_single_source_of_truth(monkeypatch):
    """Pin the guard's rule to the placement policy the git-ops skill states.

    The convention lives in one place (``git-ops/references/skill-detail.md``,
    "Worktree placement (user-mandated)"). This test reads that document and
    fails if the guard's ``.worktrees`` segment or its ad-hoc root stops matching
    it, so a future convention change cannot silently desync the two.
    """
    detail = Path(os.environ.get("GIT_OPS_SKILL_DETAIL")
                  or "~/.hermes/skills/git-ops/references/skill-detail.md").expanduser()
    if not detail.is_file():
        pytest.skip(f"git-ops skill detail not present at {detail}")
    text = detail.read_text()
    assert f"<main-repo>/{gate.WORKTREE_SEGMENT}/<task-or-branch>/" in text, (
        "git-ops skill no longer states the canonical worktree root the guard enforces"
    )
    assert "~/git_repos/ad-hoc/<task>/" in text, (
        "git-ops skill no longer states the ad-hoc root the guard enforces"
    )
    monkeypatch.delenv("AGENT_GUARDS_ADHOC_ROOT", raising=False)
    monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
    assert gate.adhoc_root() == Path("~/git_repos/ad-hoc").expanduser()

