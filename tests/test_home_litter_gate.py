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
    """Drive the real plugin entry point so the guard-name stamp is exercised
    (the module returns the bare message; ``_pre_tool_call`` adds ``[name]``)."""
    result = guard._pre_tool_call(tool_name=tool, args=args, session_id="s-hlg")
    return result["message"] if result else None


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


def test_cp_file_onto_home_itself_blocked(monkeypatch):
    """`cp a.md ~/` lands a brand-new top-level entry even though the
    destination names no child."""
    message = worker_terminal("cd /tmp && cp a.md ~/")
    assert message and "home_litter_gate" in message
    assert "~/a.md" in message


def test_mv_file_onto_home_itself_blocked(monkeypatch):
    assert "home_litter_gate" in worker_terminal("cd /tmp && mv a.md ~/")


def test_rsync_dir_onto_home_itself_blocked(monkeypatch):
    assert "home_litter_gate" in worker_terminal("cd /tmp && rsync -a src/ ~/")


def test_install_onto_home_itself_blocked(monkeypatch):
    assert "home_litter_gate" in worker_terminal("cd /tmp && install -m644 a ~/")


def test_cp_onto_home_via_home_variable_blocked(monkeypatch):
    assert "home_litter_gate" in worker_terminal("cd /tmp && cp a.md $HOME/")


def test_cp_onto_existing_top_level_allowed(monkeypatch):
    """A destination that already exists stays allowed — no new entry appears."""
    assert worker_terminal("cd /tmp && cp a.md ~/existing.txt") is None
    assert worker_terminal("cd /tmp && cp a.md ~/git_repos/") is None
    assert worker_terminal("cd /tmp && cp a.md ~/Downloads/") is None


def test_cp_onto_home_with_glob_fails_open(monkeypatch):
    """The resulting basename is unknowable, so the shape fails open."""
    assert worker_terminal("cd /tmp && cp *.md ~/") is None


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
    assert worker_terminal("mkdir -p ~/git_repos/ad-hoc/t_x") is None


def test_nested_creation_of_new_top_level_entry_blocked(monkeypatch):
    """`mkdir -p <newtop>/<child>` from cwd ~ creates ~/<newtop>: litter."""
    for command in (
        "cd ~ && mkdir -p scratch/f3",
        "cd ~ && mkdir -p wt/t_x",
        "cd ~ && mkdir -p hermes-scratch/a",
        "cd ~ && mkdir -p kanban_tmp/x",
        "cd ~ && mkdir -p cache-scratch/x",
        "cd ~ && mkdir -p profiles/x",
    ):
        assert "home_litter_gate" in worker_terminal(command), command


def test_nested_creation_under_existing_top_level_allowed(monkeypatch):
    """An existing first component keeps deep creates working."""
    assert worker_terminal("cd ~ && mkdir -p git_repos/ad-hoc/t_x") is None
    assert worker_terminal("cd ~ && mkdir -p .cache/probe/deep") is None
    assert worker_terminal("cd ~ && mkdir -p Downloads/a/b") is None


def test_multi_operand_mkdir_one_new_top_level_blocked(monkeypatch):
    assert "home_litter_gate" in worker_terminal("cd ~ && mkdir -p a/b c/d")


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
    """A worktree dir that is a new top-level entry under ~ is litter, nested too."""
    assert "home_litter_gate" in worker_terminal("mkdir ~/wt")
    assert "home_litter_gate" in worker_terminal("mkdir -p ~/wt/t_x")


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
        args={"command": "mkdir -p ~/git_repos/ad-hoc/t_x"},
        session_id="s-home-litter-e2e-ok",
    )
    assert result is None


"""Structural matrix: every write form x every destination shape.

R4732-1 was a predicate-coverage hole: `cp <src> ~/` (and mv/rsync/install)
creates a brand-new top-level entry while the destination names no child, so
the per-target check saw nothing to name. The individual fire/do-not-fire
tests above cover the named cases; this matrix makes the NEXT missed form fail
a test instead of surviving to review.
"""

_COPY_HEADS = (
    ("cp a.md {d}", "a.md"),
    ("mv a.md {d}", "a.md"),
    ("rsync -a src/ {d}", "src"),
    ("install -m644 a {d}", "a"),
)
"""Copy-style heads and the basename each lands when the destination is `~`."""

_COPY_DESTS = (
    ("~", True),
    ("~/", True),
    ("$HOME", True),
    ("$HOME/", True),
    ("~/new_entry", True),
    ("~/new_entry/child", True),
    ("~/existing.txt", False),
    ("~/.cache/probe", False),
    ("~/.brandnewdot", False),
    ("~/git_repos/newrepo", False),
    ("~/git_repos/a/b/c", False),
    ("~/Downloads/a/b", False),
    ("~/Documents/x", False),
    ("~/Desktop/x", False),
)
"""Destination shapes x whether a copy into them creates a top-level entry.

The `True` group: `~`, `~/`, `$HOME`, `$HOME/` name no child and resolve to the
home itself, so the SOURCE basename becomes the new entry; `~/new_entry` and
`~/new_entry/child` name a not-yet-existing child (or a nested path under one).
The `False` group: an existing entry, a dotted name, or an allowlisted dir.
"""

_COPY_CASES = []
for _template, _src_base in _COPY_HEADS:
    for _dest, _blocks in _COPY_DESTS:
        if not _blocks:
            _COPY_CASES.append((_template, _src_base, _dest, False, None))
        elif _dest in ("~", "~/", "$HOME", "$HOME/"):
            _COPY_CASES.append((_template, _src_base, _dest, True, _src_base))
        else:
            _COPY_CASES.append((_template, _src_base, _dest, True, "new_entry"))


@pytest.mark.parametrize(
    "template,source_basename,dest,expected_block,entry",
    _COPY_CASES,
    ids=lambda value: str(value),
)
def test_copy_head_destination_matrix(monkeypatch, template, source_basename, dest, expected_block, entry):
    """A copy-style head must block exactly when the destination would create a
    new top-level entry under `~`, and name that entry in the message."""
    command = "cd /tmp && " + template.format(d=dest)
    message = worker_terminal(command)
    if expected_block:
        assert message and "home_litter_gate" in message, command
        assert "~/" + entry in message, (command, message)
    else:
        assert message is None, (command, message)


_CREATE_DESTS = (
    ("mkdir {d}", "~", False, None),
    ("mkdir {d}", "~/", False, None),
    ("touch {d}", "~", False, None),
    ("echo x > {d}", "~/", False, None),
    ("mkdir {d}", "~/new_entry", True, "new_entry"),
    ("mkdir -p {d}", "~/new_entry/child", True, "new_entry"),
    ("touch {d}", "~/new_entry", True, "new_entry"),
    ("echo x > {d}", "~/new_entry", True, "new_entry"),
    ("echo x > {d}", "~/new_entry/child", True, "new_entry"),
    ("touch {d}", "~/existing.txt", False, None),
    ("echo x > {d}", "~/.cache/probe", False, None),
    ("mkdir {d}", "~/git_repos/x", False, None),
    ("mkdir {d}", "~/Downloads/x", False, None),
)
"""Non-copy creating heads x destination shapes.

A head that names the home itself (`mkdir ~`) creates nothing new; one naming a
new child blocks and names it; existing, dotted and allowlisted destinations
stay allowed.
"""


@pytest.mark.parametrize(
    "template,dest,expected_block,entry",
    _CREATE_DESTS,
    ids=lambda value: str(value),
)
def test_create_head_destination_matrix(monkeypatch, template, dest, expected_block, entry):
    command = "cd ~ && " + template.format(d=dest)
    message = worker_terminal(command)
    if expected_block:
        assert message and "home_litter_gate" in message, command
        assert "~/" + entry in message, (command, message)
    else:
        assert message is None, (command, message)
