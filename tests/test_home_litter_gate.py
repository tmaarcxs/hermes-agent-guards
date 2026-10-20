import os
import sys

import pytest

guard = sys.modules["agent_guards"]
gate = guard.home_litter_gate

WS = "/tmp/fake-task-workspace"


@pytest.fixture(autouse=True)
def worker_env(monkeypatch, tmp_path):
    """A synthetic real home so the gate's create/allow decision is deterministic
    and independent of the developer's own ``~``."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "git_repos").mkdir()
    (home / ".hermes").mkdir()
    (home / ".cache").mkdir()
    (home / "existing.txt").write_text("x")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_REAL_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_self")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_ATTACHMENTS_ROOT", raising=False)
    return home


def home_of(monkeypatch):
    return os.path.expanduser(os.environ["HERMES_REAL_HOME"])


def blocked(tool, args):
    return gate.check(tool, args)


def worker_terminal(command, workdir=None):
    args = {"command": command}
    if workdir:
        args["workdir"] = workdir
    return blocked("terminal", args)


def test_sqlite3_bare_relative_kanban_db_from_home_blocked(monkeypatch):
    """The exact litter shape: cwd at ~ and a bare relative sqlite3 path."""
    command = "cd ~ && sqlite3 kanban.db 'select 1'"
    message = worker_terminal(command)
    assert message and "home_litter_gate" in message
    assert "~/kanban.db" in message


def test_mkdir_scratch_from_home_blocked(monkeypatch):
    assert "home_litter_gate" in worker_terminal("cd ~ && mkdir scratch")


def test_touch_new_home_file_blocked(monkeypatch):
    assert "home_litter_gate" in worker_terminal("cd ~ && touch notes.md")


def test_redirect_new_home_file_blocked(monkeypatch):
    assert "home_litter_gate" in worker_terminal("echo hi > ~/report.txt")


def test_tee_new_home_file_blocked(monkeypatch):
    assert "home_litter_gate" in worker_terminal("echo hi | tee ~/out.log")


def test_cp_into_new_home_dir_blocked(monkeypatch):
    assert "home_litter_gate" in worker_terminal("cp a.md ~/kanban_tmp")


def test_write_file_new_home_path_blocked(monkeypatch):
    assert blocked("write_file", {"path": "~/litter.md", "content": "x"})


def test_patch_new_home_path_blocked(monkeypatch):
    assert blocked("patch", {"path": "~/litter.md", "old_string": "a", "new_string": "b"})


def test_python_c_makedirs_in_home_blocked(monkeypatch):
    command = "python3 -c \"import os; os.makedirs(os.path.expanduser('~/scratch_review'))\""
    assert "home_litter_gate" in worker_terminal(command)


def test_python_heredoc_write_in_home_blocked(monkeypatch):
    command = "python3 - <<'EOF'\nopen('~/kanban.db','w').write('')\nEOF"
    assert "home_litter_gate" in worker_terminal(command)


def test_curl_output_into_new_home_file_blocked(monkeypatch):
    assert "home_litter_gate" in worker_terminal("curl -sL https://example.com/x -o ~/x.tar")


def test_wget_output_into_new_home_file_blocked(monkeypatch):
    assert "home_litter_gate" in worker_terminal("wget -O ~/x.tar https://example.com/x")



def test_existing_top_level_entry_allowed(monkeypatch):
    assert worker_terminal("cd ~ && echo x > existing.txt") is None


def test_existing_top_level_dir_allowed(monkeypatch):
    assert worker_terminal("cd ~ && mkdir -p git_repos/thing") is None


def test_allowlisted_new_top_level_allowed(monkeypatch):
    for name in ("git_repos", "Downloads", "Documents", "Desktop"):
        assert worker_terminal("cd ~ && mkdir %s" % name) is None, name


def test_dotted_new_top_level_allowed(monkeypatch):
    for name in (".cache2", ".config", ".anything"):
        assert worker_terminal("cd ~ && mkdir %s" % name) is None, name


def test_new_entry_below_home_allowed(monkeypatch):
    assert worker_terminal("cd ~/git_repos && mkdir newrepo") is None
    assert worker_terminal("mkdir -p ~/git_repos/worktrees/hermes-agent-guards/t_x") is None


def test_hermes_tree_untouched(monkeypatch):
    assert worker_terminal("mkdir -p ~/.hermes/cache/scratch/probe") is None
    assert worker_terminal("echo x > ~/.hermes/kanban/attachments/t_self/r.md") is None


def test_rm_of_absent_home_entry_not_litter(monkeypatch):
    assert worker_terminal("cd ~ && rm -rf ad-hoc") is None
    assert worker_terminal("rmdir ~/wt") is None


def test_workspace_and_tmp_targets_allowed(monkeypatch):
    assert worker_terminal("echo x > $HERMES_KANBAN_WORKSPACE/out.txt") is None
    assert worker_terminal("mkdir -p /tmp/probe") is None


def test_reads_at_home_allowed(monkeypatch):
    assert worker_terminal("ls ~") is None
    assert worker_terminal("cd ~ && cat existing.txt") is None


def test_lead_unaffected(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert blocked("write_file", {"path": "~/litter.md", "content": "x"}) is None
    assert worker_terminal("cd ~ && mkdir scratch") is None


def test_non_dict_and_missing_args_fail_open(monkeypatch):
    assert gate.check("terminal", None) is None
    assert gate.check("terminal", "not-a-dict") is None
    assert gate.check("write_file", {"path": "", "content": "x"}) is None
    assert gate.check("terminal", {"command": ""}) is None


def test_message_names_safe_targets(monkeypatch):
    message = worker_terminal("cd ~ && mkdir scratch")
    assert "HERMES_KANBAN_WORKSPACE" in message and "scratch" in message
    assert "kanban.db" in message


def test_message_names_canonical_repo_work_paths(monkeypatch):
    """The advice must not teach the superseded ~/git_repos/worktrees/ path."""
    message = worker_terminal("cd ~ && mkdir scratch")
    assert "<main-repo>/.worktrees/<task>/" in message
    assert "~/git_repos/ad-hoc/<task>/" in message
    assert "~/git_repos/worktrees/" not in message


def test_git_worktree_add_under_main_repo_allowed(monkeypatch, tmp_path):
    """`git worktree add <main-repo>/.worktrees/<task>` is not a direct child of ~."""
    main = tmp_path / "main-repo"
    main.mkdir()
    assert worker_terminal(
        "git worktree add %s/.worktrees/t_x -b t_x origin/main" % main) is None


def test_git_worktree_add_path_under_home_direct_child_blocked(monkeypatch):
    """A worktree dir that IS a direct child of ~ is still litter (nested is not)."""
    assert "home_litter_gate" in worker_terminal("mkdir ~/wt")
    assert worker_terminal("mkdir -p ~/wt/t_x") is None


def test_production_entry_point_pre_tool_call_blocks(monkeypatch):
    """Drive the real plugin entry point, not the module check."""
    result = guard._pre_tool_call(
        tool_name="terminal",
        args={"command": "cd ~ && sqlite3 kanban.db 'select 1'"},
        session_id="s-home-litter-e2e",
    )
    assert result is not None and result["action"] == "block", result
    assert "[home_litter_gate]" in result["message"]
    assert result["message"].startswith("agent-guards blocked terminal")


def test_production_entry_point_allows_safe_call(monkeypatch):
    result = guard._pre_tool_call(
        tool_name="terminal",
        args={"command": "mkdir -p ~/git_repos/worktrees/hermes-agent-guards/t_x"},
        session_id="s-home-litter-e2e-ok",
    )
    assert result is None
