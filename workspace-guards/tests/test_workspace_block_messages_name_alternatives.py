"""Every workspace-guards block message names an alternative.

Same contract as shell-guards (see its test file): a block names what happened
and the exact permitted alternative for the same intent. The home-litter gate
already lists where writes may live; the workspace-convention gate names the
three allowed placements and the plugin local-edit gate names the matching
working clone, its ``git worktree add`` command and the sync calls — all
static-pinned here verbatim.
"""

import os
import sys

import pytest

guards = sys.modules["workspace_guards"]
WS = "/tmp/fake-task-workspace"


@pytest.fixture(autouse=True)
def worker_env(monkeypatch, tmp_path):
    """A synthetic real home so the gate's create/allow decision is deterministic."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "git_repos").mkdir()
    (home / ".hermes").mkdir()
    (home / "existing.txt").write_text("x")
    monkeypatch.setenv("HERMES_REAL_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_self")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", WS)
    return home


def worker_terminal(command, home):
    args = {"command": command}
    if home:
        args["workdir"] = str(home)
    result = guards._pre_tool_call(tool_name="terminal", args=args, session_id="s-alt")
    return result["message"] if result else None


def test_home_litter_message_names_the_allowed_write_locations(tmp_path):
    message = worker_terminal("cd ~ && mkdir scratch", tmp_path / "home")
    assert message and "HERMES_KANBAN_WORKSPACE" in message
    assert "~/.hermes/cache/scratch" in message
    assert "<main-repo>/.worktrees/<task>/" in message
    assert "~/git_repos/ad-hoc/<task>/" in message
    assert "~/.hermes/kanban.db" in message


def test_write_file_message_names_the_task_workspace(tmp_path):
    message = guards.home_litter_gate.check(
        "write_file", {"path": str(tmp_path / "home" / "note.md")}
    )
    assert message and "top-level entry" in message
    assert "<main-repo>/.worktrees/<task>/" in message


def test_workspace_convention_message_names_the_three_allowed_roots():
    args = {"workspace_path": "/tmp/somewhere/else", "workspace_kind": "dir"}
    message = guards.workspace_convention.check("kanban_create", args)
    assert message and "<main-checkout>/.worktrees/<task>/" in message
    assert "~/git_repos/ad-hoc/<name>/" in message
    assert "workspace_kind=scratch, no workspace_path" in message


def test_plugin_local_edit_message_names_clone_worktree_and_sync(tmp_path, monkeypatch):
    """The runtime-computed alternative: matching clone + worktree add + syncs."""
    home = tmp_path / "home"
    checkout = home / ".hermes" / "plugins" / "plugA"
    clone = home / "git_repos" / "plugA"
    (home / ".hermes" / "profiles" / "worker" / "plugins" / "plugA").mkdir(parents=True)
    clone.mkdir(parents=True)
    for path in (checkout, clone):
        import subprocess

        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "config", "remote.origin.url",
                        "https://example.com/tmaarcxs/plugA.git"], check=True)
    message = guards.plugin_local_edit_gate.check(
        "write_file", {"path": str(checkout / "gate.py")}
    )
    assert message
    assert str(clone) in message
    assert "worktree add" in message
    assert "origin/main" in message
    assert "plugins update plugA" in message
    assert "hermes -p " in message


def test_plugin_local_edit_message_without_a_clone_names_the_clone_command(tmp_path):
    """The fixture home has a plugins checkout but no matching git_repos clone."""
    home = tmp_path / "home"
    checkout = home / ".hermes" / "profiles" / "worker" / "plugins" / "plugB"
    checkout.mkdir(parents=True)
    import subprocess

    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    origin = "https://example.com/tmaarcxs/plugB.git"
    subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", origin],
                   check=True)
    message = guards.plugin_local_edit_gate.check(
        "write_file", {"path": str(checkout / "gate.py")}
    )
    assert message
    assert "git clone" in message
    assert origin in message
    assert "git_repos" in message
