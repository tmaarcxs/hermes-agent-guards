import sys

import pytest

guards = sys.modules["agent_guards"]
lead = guards.lead_delegation


@pytest.fixture(autouse=True)
def lead_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("AGENT_GUARDS_REPO_ROOTS", str(tmp_path / "git_repos"))
    return tmp_path


def call(tool, args):
    result = guards._pre_tool_call(tool_name=tool, args=args, task_id="", session_id="")
    return result if result and result.get("action") == "block" else None


def test_lead_write_into_repository_is_blocked(lead_setup):
    path = str(lead_setup / "git_repos" / "mic" / "code" / "x.py")
    assert "Kanban worker" in call("write_file", {"path": path, "content": "x"})["message"]
    assert call("patch", {"path": path, "old_string": "a", "new_string": "b"})


def test_lead_write_outside_repositories_passes(lead_setup):
    assert call("write_file", {"path": str(lead_setup / "scratch" / "issue.md"), "content": "x"}) is None


@pytest.mark.parametrize(
    "command",
    [
        "cd {root}/mic && git commit -S -m 'fix'",
        "git -C {root}/mic push -u origin feature/x",
        "cd {root}/spa; gh pr create --base develop --title t",
    ],
)
def test_lead_delivery_commands_in_repositories_are_blocked(lead_setup, command):
    root = lead_setup / "git_repos"
    assert call("terminal", {"command": command.format(root=root)})


def test_lead_delivery_via_workdir_is_blocked(lead_setup):
    workdir = str(lead_setup / "git_repos" / "mic")
    assert call("terminal", {"command": "git commit -m x", "workdir": workdir})


def test_lead_reads_and_outside_commits_pass(lead_setup):
    root = lead_setup / "git_repos"
    assert call("terminal", {"command": f"cd {root}/mic && git status && git log -3 && gh pr view 1"}) is None
    assert call("terminal", {"command": f"cd {lead_setup}/plugin && git commit -m x && git push"}) is None
    assert call("terminal", {"command": f"cd {root}/mic && grep -n 'git commit' notes.md"}) is None


def test_worker_process_is_not_restricted(lead_setup, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_123")
    root = lead_setup / "git_repos"
    assert call("write_file", {"path": str(root / "mic" / "x.py"), "content": "x"}) is None
    assert call("terminal", {"command": f"cd {root}/mic && git commit -m x"}) is None


def test_without_worker_setup_nothing_is_enforced(lead_setup, monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert call("write_file", {"path": str(lead_setup / "git_repos" / "mic" / "x.py"), "content": "x"}) is None
