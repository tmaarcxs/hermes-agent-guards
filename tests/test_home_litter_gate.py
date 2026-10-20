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


@pytest.mark.parametrize("command", [
    "cd ~ && mkdir -p -m 755 git_repos/proj",
    "cd ~ && mkdir -p -m 700 git_repos/proj/.venv",
    "cd ~ && mkdir -p -m 755 Downloads/new",
    "cd ~ && mkdir -m 755 existing.txt",
    "cd ~ && touch -t 202001010000 existing.txt",
    "cd ~ && touch -d 2020-01-01T00:00:00 existing.txt",
    "cd ~ && touch --reference existing.txt existing.txt",
    "cd ~ && touch -r existing.txt existing.txt",
    "cd ~ && mkdir --mode 755 git_repos/proj",
])
def test_mkdir_touch_option_arguments_not_targets_allowed(monkeypatch, command):
    """A value-consuming option's argument is not a path: allowlisted or
    existing targets must pass, not be blocked as a fabricated ``~/755``."""
    assert worker_terminal(command) is None, command


@pytest.mark.parametrize("command", [
    "cd ~ && mkdir -m 755 brandnew",
    "cd ~ && mkdir --mode=755 brandnew",
    "cd ~ && mkdir -p -m 755 brandnew/deep",
    "cd ~ && touch -t 202001010000 newfile.txt",
])
def test_mkdir_touch_option_arguments_real_target_still_blocked(monkeypatch, command):
    """Skipping the option's argument must not hide the real create target."""
    assert "home_litter_gate" in worker_terminal(command), command


def test_mkdir_option_argument_block_names_real_path(monkeypatch):
    """On a genuine block the message must name the created path, not ``~/755``."""
    message = worker_terminal("cd ~ && mkdir -m 755 brandnew")
    assert "brandnew" in message and "~/755" not in message


@pytest.mark.parametrize("command", [
    "cd ~ && mkdir -pm 755 git_repos/proj",
    "cd ~ && mkdir -vm 755 git_repos/proj",
    "cd ~ && mkdir -pm 700 git_repos/proj/.venv",
    "cd ~ && mkdir -pm 755 Downloads/new",
    "cd ~ && touch -mt 202001010000 existing.txt",
    "cd ~ && touch -dt 2020-01-01 existing.txt",
])
def test_clustered_option_arguments_not_targets_allowed(monkeypatch, command):
    """A *clustered* short option (``-pm``, ``-vm``, ``-mt``, ``-dt``) is one
    token that is not in the option set, yet its value-consuming letter still
    takes the next token; that value must not be read as a create target. A
    contract-allowlisted ``~/git_repos`` (and a no-op ``touch`` on an existing
    file) must pass, not be blocked as a fabricated ``~/755``.
    """
    assert worker_terminal(command) is None, command


@pytest.mark.parametrize("command,entry", [
    ("cd ~ && mkdir -pm 755 brandnew", "~/brandnew"),
    ("cd ~ && mkdir -vm 755 brandnew/deep", "~/brandnew"),
    ("cd ~ && touch -mt 202001010000 newfile.txt", "~/newfile.txt"),
    ("cd ~ && touch -dt 2020-01-01 newfile.txt", "~/newfile.txt"),
])
def test_clustered_option_arguments_real_target_still_blocked(monkeypatch, command, entry):
    """Skipping a clustered option's argument must not hide the real create
    target, and the message must name that path — not the option's value.
    """
    message = worker_terminal(command)
    assert message and "home_litter_gate" in message, command
    assert entry in message, command
    assert "~/755" not in message and "~/202001010000" not in message, command


_MKDIR_MODE_SPELLINGS = [
    "-m 755",
    "-pm 755",
    "-vm 755",
    "--mode 755",
    "--mode=755",
    "-m755",
]

_TOUCH_STAMP_SPELLINGS = [
    "-t 202001010000",
    "-mt 202001010000",
    "-dt 2020-01-01",
    "--time 202001010000",
    "--time=202001010000",
    "-t202001010000",
]


@pytest.mark.parametrize("option", _MKDIR_MODE_SPELLINGS)
def test_mkdir_mode_spelling_does_not_change_the_decision(monkeypatch, option):
    """Generative guard for the recurring parser-shape hole (R4841-1): every
    spelling of a value-consuming option — spaced, clustered, long, attached —
    must leave the create/allow decision on the *operand* unchanged, so the next
    spelling fails a test instead of reaching review.
    """
    assert worker_terminal(f"cd ~ && mkdir {option} git_repos/proj") is None, option
    message = worker_terminal(f"cd ~ && mkdir {option} brandnew")
    assert message and "~/brandnew" in message, option
    assert "~/755" not in message, option


@pytest.mark.parametrize("option", _TOUCH_STAMP_SPELLINGS)
def test_touch_stamp_spelling_does_not_change_the_decision(monkeypatch, option):
    """The touch twin of the mkdir spelling-invariance guard: a no-op stamp on an
    existing file allows, and the same stamp must not mask a new top-level file.
    """
    assert worker_terminal(f"cd ~ && touch {option} existing.txt") is None, option
    message = worker_terminal(f"cd ~ && touch {option} newfile.txt")
    assert message and "~/newfile.txt" in message, option
    assert "~/202001010000" not in message, option


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


"""Round-5 regression tests: resolved command cwd, sed -i operands, python receivers.

R4784-1: the gate read only the in-string ``cd``, so a bare relative
``sqlite3 kanban.db`` (the PR's own stated root cause) ran with the cwd at the
real home and was allowed. The terminal tool resolves the cwd from the explicit
``workdir`` argument first, then the session's recorded cwd. Both routes must
reach the same create/allow decision.

R4784-2: ``sed -i`` read ``tokens[index + 2:]``, which is the sed *script*, so
an in-place edit of an existing file was reported as creating ``~/s``.

R4784-3: ``copy``/``copyfile`` were missing from the python creation set and the
path literal lives in the receiver of ``pathlib.Path(p).write_text(...)``.
"""


def session_cwd_route(monkeypatch, session_id, cwd):
    """Record *cwd* as the session's cwd, as ``finalize_foreground_result`` does."""
    from tools.terminal_tool import clear_session_cwd, record_session_cwd

    record_session_cwd(session_id, cwd)
    return lambda: clear_session_cwd(session_id)


_WORKDIR_LITTER = (
    ("sqlite3 kanban.db 'select 1'", "kanban.db"),
    ("mkdir scratch", "scratch"),
    ("touch litter.md", "litter.md"),
    ("echo x > note.txt", "note.txt"),
    ("sed -i '' 's/a/b/' brandnew.txt", "brandnew.txt"),
)
"""Relative creating writes that litter ``~`` when the command cwd is the home."""


@pytest.mark.parametrize("command,entry", _WORKDIR_LITTER, ids=lambda value: str(value))
def test_explicit_workdir_at_home_blocks(monkeypatch, command, entry):
    """An explicit ``workdir`` at the real home resolves the relative target there."""
    home = home_of(monkeypatch)
    message = worker_terminal(command, workdir=home)
    assert message and "home_litter_gate" in message, command
    assert "~/" + entry in message, (command, message)


@pytest.mark.parametrize("command,entry", _WORKDIR_LITTER, ids=lambda value: str(value))
def test_recorded_session_cwd_at_home_blocks(monkeypatch, command, entry):
    """A prior ``cd ~`` sets the session cwd; a later bare command must still block."""
    home = home_of(monkeypatch)
    session_id = "s-hlg-session-cwd"
    cleanup = session_cwd_route(monkeypatch, session_id, home)
    try:
        result = guard._pre_tool_call(tool_name="terminal", args={"command": command}, session_id=session_id)
        message = result["message"] if result else None
        assert message and "home_litter_gate" in message, command
        assert "~/" + entry in message, (command, message)
    finally:
        cleanup()


_SAFE_CWD_LITTER = (
    ("sqlite3 kanban.db 'select 1'", "workspace"),
    ("mkdir scratch", "workspace"),
    ("touch litter.md", "workspace"),
    ("echo x > note.txt", "scratch"),
)
"""The same commands with the cwd inside an allowed zone create nothing under ``~``."""


@pytest.mark.parametrize("command,zone", _SAFE_CWD_LITTER, ids=lambda value: str(value))
def test_safe_workdir_does_not_fire(monkeypatch, tmp_path, command, zone):
    """A relative creating write resolves against the given cwd, not the home."""
    target = tmp_path / ("ws" if zone == "workspace" else "scratch")
    target.mkdir(exist_ok=True)
    assert worker_terminal(command, workdir=str(target)) is None, command


def test_safe_recorded_session_cwd_does_not_fire(monkeypatch, tmp_path):
    session_id = "s-hlg-session-cwd-ok"
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    cleanup = session_cwd_route(monkeypatch, session_id, workspace)
    try:
        result = guard._pre_tool_call(
            tool_name="terminal", args={"command": "mkdir scratch"}, session_id=session_id)
        assert result is None, result
    finally:
        cleanup()


_SED_IN_PLACE_ALLOWED = (
    "cd ~ && sed -i '' 's/a/b/' existing.txt",
    "cd ~ && sed -i 's/a/b/' existing.txt",
    "cd ~ && sed -i -e 's/a/b/' existing.txt",
    "cd ~ && sed --in-place 's/a/b/' existing.txt",
    "cd ~ && sed -i 's/foo/bar/' git_repos/x.txt",
    "cd ~ && sed -i '' 's/a/b/' .cache/probe",
)
"""In-place edits of an existing entry create nothing: the script and the ``-i``
suffix must never be read as destinations."""


@pytest.mark.parametrize("command", _SED_IN_PLACE_ALLOWED, ids=lambda value: str(value))
def test_sed_in_place_on_existing_file_allowed(monkeypatch, command):
    assert worker_terminal(command) is None, command


_SED_IN_PLACE_BLOCKED = (
    ("cd ~ && sed -i '' 's/a/b/' brandnew.txt", "brandnew.txt"),
    ("cd ~ && sed -i 's/a/b/' brandnew.txt", "brandnew.txt"),
    ("cd ~ && sed --in-place 's/a/b/' brandnew.txt", "brandnew.txt"),
)


@pytest.mark.parametrize("command,entry", _SED_IN_PLACE_BLOCKED, ids=lambda value: str(value))
def test_sed_in_place_on_new_top_level_file_blocks(monkeypatch, command, entry):
    message = worker_terminal(command)
    assert message and "home_litter_gate" in message, command
    assert "~/" + entry in message, (command, message)


def test_sed_without_in_place_reads_only(monkeypatch):
    assert worker_terminal("cd ~ && sed 's/a/b/' existing.txt") is None


_PY_HOME_CREATES = (
    "pathlib.Path('{home}/newfile').write_text('x')",
    "pathlib.Path('{home}/newdir').mkdir()",
    "shutil.copy('a', '{home}/newfile')",
    "shutil.copyfile('a', '{home}/newfile')",
)
"""Python creation shapes whose path lives in the receiver, not the call body."""


@pytest.mark.parametrize("template", _PY_HOME_CREATES, ids=lambda value: str(value))
def test_python_receiver_creation_in_home_blocks(monkeypatch, template):
    home = home_of(monkeypatch)
    module = "pathlib" if "pathlib" in template else "shutil"
    command = 'python3 -c "import %s; %s"' % (module, template.format(home=home))
    assert "home_litter_gate" in worker_terminal(command), command


@pytest.mark.parametrize("template", _PY_HOME_CREATES, ids=lambda value: str(value))
def test_python_receiver_creation_outside_home_allowed(monkeypatch, template):
    workspace = os.environ["HERMES_KANBAN_WORKSPACE"]
    module = "pathlib" if "pathlib" in template else "shutil"
    command = 'python3 -c "import %s; %s"' % (module, template.format(home=workspace))
    assert worker_terminal(command) is None, command


def test_config_cwd_at_home_blocks_on_the_first_command(monkeypatch, tmp_path):
    """A worker whose *process* cwd is the home (``terminal.cwd: .``) has no
    session record yet on its first command; the gate must still resolve that
    cwd from the tool's own default rather than falling back to the workspace.
    """
    home = home_of(monkeypatch)
    monkeypatch.setattr(os, "getcwd", lambda: home)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    for command, entry in (("sqlite3 kanban.db 'select 1'", "kanban.db"),
                           ("mkdir scratch", "scratch"),
                           ("touch litter.md", "litter.md")):
        result = guard._pre_tool_call(
            tool_name="terminal", args={"command": command}, session_id="s-hlg-config-cwd")
        message = result["message"] if result else None
        assert message and "home_litter_gate" in message, command
        assert "~/" + entry in message, (command, message)


def test_config_cwd_elsewhere_does_not_fire(monkeypatch, tmp_path):
    """The same first-command route with a safe process cwd stays allowed."""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    monkeypatch.setattr(os, "getcwd", lambda: str(workspace))
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    result = guard._pre_tool_call(
        tool_name="terminal", args={"command": "mkdir scratch"}, session_id="s-hlg-config-cwd-ok")
    assert result is None, result


def test_config_cwd_honours_terminal_cwd_env(monkeypatch, tmp_path):
    """An absolute ``TERMINAL_CWD`` wins over the process cwd, as the tool's own
    ``_resolve_config_cwd`` does."""
    home = home_of(monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    monkeypatch.setattr(os, "getcwd", lambda: home)
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    result = guard._pre_tool_call(
        tool_name="terminal", args={"command": "mkdir scratch"}, session_id="s-hlg-config-cwd-env")
    assert result is None, result


"""Round-6 regression tests: create heads are recognised only in command position.

R4786-1: `_creation_targets` treated *any* token equal to a create head as the
command word, so a read-only or non-creating command that merely *mentions*
`mkdir`/`cp`/`install`/`sqlite3`/`tee` was blocked with a factually wrong
message ("would create ~/src"). At the cwd state this guard exists for (worker
process cwd = `~`) that turned `grep -rn mkdir src`, `pip install requests` and
`echo sqlite3 kanban.db` into hard failures. The head is now the first token in
command position (skipping leading wrappers and `VAR=value` words); everything
after it is an operand, never a head.
"""

_HEAD_POSITION_ALLOWED = (
    "grep -rn mkdir src",
    "grep -rn install src",
    "rg mkdir docs",
    "echo mkdir foo",
    "echo cp foo",
    "pip install requests",
    "pip3 install -q -r req.txt",
    "npm install",
    "make install",
    "brew install jq",
    "gem install bundler",
    "man install",
    "which install",
    "echo sqlite3 kanban.db",
    "grep -rn tee src",
    "git log --oneline -- install",
    "cat install",
    "awk '{print} ' install",
)
"""Read-only / non-creating commands that merely name a create head as an
argument. None of them creates anything under ``~``, so all must be allowed
with the cwd at the real home."""


@pytest.mark.parametrize("command", _HEAD_POSITION_ALLOWED, ids=lambda value: str(value))
def test_mentioning_a_create_head_is_not_a_creation(monkeypatch, command):
    home = home_of(monkeypatch)
    assert worker_terminal(command, workdir=home) is None, command


_HEAD_POSITION_BLOCKED = (
    ("mkdir brandnew", "brandnew"),
    ("touch brandnew.md", "brandnew.md"),
    ("echo x > brandnewfile", "brandnewfile"),
    ("sqlite3 kanban.db 'select 1'", "kanban.db"),
    ("tee brandnew.log", "brandnew.log"),
    ("cp a.md brandnewdir", "brandnewdir"),
)
"""True positives that must keep blocking at cwd ``~`` after the head fix."""


@pytest.mark.parametrize("command,entry", _HEAD_POSITION_BLOCKED, ids=lambda value: str(value))
def test_create_head_in_command_position_still_blocks(monkeypatch, command, entry):
    home = home_of(monkeypatch)
    message = worker_terminal(command, workdir=home)
    assert message and "home_litter_gate" in message, command
    assert "~/" + entry in message, (command, message)


def test_create_head_behind_a_wrapper_still_blocks(monkeypatch):
    """`sudo mkdir x` / `env FOO=1 mkdir x` create `~/x`; the wrapper is skipped
    when locating the command word."""
    home = home_of(monkeypatch)
    for command in ("sudo mkdir brandnew", "env FOO=1 mkdir brandnew"):
        message = worker_terminal(command, workdir=home)
        assert message and "home_litter_gate" in message, command
        assert "~/brandnew" in message, (command, message)


def test_wrapper_alone_does_not_fire(monkeypatch):
    home = home_of(monkeypatch)
    assert worker_terminal("sudo grep -rn mkdir src", workdir=home) is None


_DOWNLOAD_HEAD_ALLOWED = (
    "echo curl -o foo",
    "echo wget -O bar",
    "grep -rn 'curl -o' src",
    "grep -rn wget src",
    "curl --help",
    "wget --version",
)
"""Commands that name ``curl``/``wget`` without downloading: the old unanchored
``\\bcurl\\b`` regex blocked ``echo curl -o foo`` (which writes nothing)."""


@pytest.mark.parametrize("command", _DOWNLOAD_HEAD_ALLOWED, ids=lambda value: str(value))
def test_download_head_only_counts_in_command_position(monkeypatch, command):
    home = home_of(monkeypatch)
    assert worker_terminal(command, workdir=home) is None, command


_DOWNLOAD_HEAD_BLOCKED = (
    ("curl -o brandnew http://x", "brandnew"),
    ("wget -O brandnew http://x", "brandnew"),
    ("curl --output brandnew http://x", "brandnew"),
    ("curl --output=brandnew http://x", "brandnew"),
    ("curl -obrandnew http://x", "brandnew"),
    ("sudo curl -o brandnew http://x", "brandnew"),
)
"""``curl -o``/``wget -O`` still create the output file and must keep blocking."""


@pytest.mark.parametrize("command,entry", _DOWNLOAD_HEAD_BLOCKED, ids=lambda value: str(value))
def test_download_head_still_blocks(monkeypatch, command, entry):
    home = home_of(monkeypatch)
    message = worker_terminal(command, workdir=home)
    assert message and "home_litter_gate" in message, command
    assert "~/" + entry in message, (command, message)


def test_download_into_an_existing_target_is_allowed(monkeypatch):
    home = home_of(monkeypatch)
    assert worker_terminal("curl -o existing.txt http://x", workdir=home) is None


_DOWNLOAD_FLAG_CASES = (
    ("curl -o f.tar http://x", True, "f.tar"),
    ("curl --output f.tar http://x", True, "f.tar"),
    ("curl --output=f.tar http://x", True, "f.tar"),
    ("curl -of.tar http://x", True, "f.tar"),
    ("curl -O http://x/f.tar", True, "f.tar"),
    ("curl -sL -O http://x/f.tar", True, "f.tar"),
    ("sudo curl -O http://x/f.tar", True, "f.tar"),
    ("wget -O f.tar http://x", True, "f.tar"),
    ("wget --output-document f.tar http://x", True, "f.tar"),
    ("wget -q -O f.tar http://x", True, "f.tar"),
    ("curl -o /tmp/f http://x", False, None),
    ("wget -O /tmp/f http://x", False, None),
)
"""The two tools' output flags, which differ: curl's ``-O`` takes the URL
basename (not ``-o``'s meaning), wget's ``-O`` names the file (wget has no
``-o``). Reading curl's ``-O`` as a filename reported the bogus ``~/http:``."""


@pytest.mark.parametrize(
    "command,expected_block,entry", _DOWNLOAD_FLAG_CASES, ids=lambda value: str(value)
)
def test_download_flags_per_tool(monkeypatch, command, expected_block, entry):
    home = home_of(monkeypatch)
    message = worker_terminal(command, workdir=home)
    if expected_block:
        assert message and "home_litter_gate" in message, command
        assert "~/" + entry in message, (command, message)
    else:
        assert message is None, (command, message)


"""Round-7 regression tests: wrapper options, and python targets vs sources.

R4818-1: `_head_index` skipped a wrapper *word* but not the wrapper's own
options, so every form where the wrapper carries an option — `sudo -u root
mkdir x`, `nice -n 5 mkdir x`, `env -i mkdir x`, `timeout 5 mkdir x` — was
allowed while `sudo mkdir x` blocked. The option (and any argument it consumes)
is now skipped, and `timeout` also skips its positional duration.

R4818-2: the python branch read *every* literal in a creating call as a created
path, so a copy SOURCE was reported as litter; it also resolved relative
literals against the guard process's cwd instead of the command's resolved cwd,
so `python3 -c "os.makedirs('brandnew')"` with `workdir=~` slipped through.
Only the call's write target is read now, resolved against the command cwd.
"""

_WRAPPER_OPTION_CREATES = (
    ("sudo -u root mkdir brandnew", "brandnew"),
    ("sudo -n mkdir brandnew", "brandnew"),
    ("sudo -E mkdir brandnew", "brandnew"),
    ("sudo --user=root mkdir brandnew", "brandnew"),
    ("sudo -u root touch brandnew.md", "brandnew.md"),
    ("env -i mkdir brandnew", "brandnew"),
    ("env -u FOO mkdir brandnew", "brandnew"),
    ("env -C /tmp mkdir brandnew", "brandnew"),
    ("nice -n 5 mkdir brandnew", "brandnew"),
    ("ionice -c2 mkdir brandnew", "brandnew"),
    ("stdbuf -o0 mkdir brandnew", "brandnew"),
    ("time -p mkdir brandnew", "brandnew"),
    ("command -p mkdir brandnew", "brandnew"),
    ("timeout 5 mkdir brandnew", "brandnew"),
    ("sudo -u root tee brandnew.log", "brandnew.log"),
    ("sudo -u root sqlite3 kanban.db 'select 1'", "kanban.db"),
    ("sudo -u root curl -o brandnew http://x", "brandnew"),
    ("nice -n 5 tee brandnew.log", "brandnew.log"),
    ("timeout 5 sqlite3 kanban.db 'select 1'", "kanban.db"),
    ("echo hi | sudo -u root tee brandnew.log", "brandnew.log"),
    ("cat f | timeout 5 sqlite3 kanban.db", "kanban.db"),
    ("cd ~ && sudo -u root mkdir brandnew", "brandnew"),
    ("cd ~ && timeout 5 mkdir brandnew", "brandnew"),
)
"""Creating commands whose wrapper carries its own option(s); all must block."""


@pytest.mark.parametrize("command,entry", _WRAPPER_OPTION_CREATES, ids=lambda value: str(value))
def test_create_head_behind_wrapper_options_still_blocks(monkeypatch, command, entry):
    home = home_of(monkeypatch)
    message = worker_terminal(command, workdir=home)
    assert message and "home_litter_gate" in message, command
    assert "~/" + entry in message, (command, message)


_WRAPPER_OPTION_READS = (
    "sudo -u root grep -rn mkdir nosuchdir",
    "nice -n 5 grep -rn mkdir nosuchdir",
    "env -i grep -rn mkdir nosuchdir",
    "timeout 5 grep -rn mkdir nosuchdir",
    "sudo -u root cat install",
)
"""The same wrappers around a read-only command: nothing is created."""


@pytest.mark.parametrize("command", _WRAPPER_OPTION_READS, ids=lambda value: str(value))
def test_wrapper_options_on_a_read_do_not_fire(monkeypatch, command):
    home = home_of(monkeypatch)
    assert worker_terminal(command, workdir=home) is None, command


_PY_TARGET_CASES = (
    ("import os; os.makedirs('brandnew')", True, "brandnew"),
    ("import os; os.mkdir('brandnew')", True, "brandnew"),
    ("import pathlib; pathlib.Path('brandnew').write_text('x')", True, "brandnew"),
    ("import shutil; shutil.copy('a', 'brandnew')", True, "brandnew"),
    ("import shutil; shutil.copyfile('a', 'brandnew')", True, "brandnew"),
    ("import shutil; shutil.copy('a', 'existing.txt')", False, None),
    ("import os; os.makedirs('existing.txt')", False, None),
    ("import shutil; shutil.copy('existing.txt', 'existing.txt')", False, None),
)
"""Python creating calls: the *target* literal decides, never the source. A
relative target resolves against the command's cwd (``workdir=~`` here)."""


@pytest.mark.parametrize(
    "body,expected_block,entry", _PY_TARGET_CASES, ids=lambda value: str(value)
)
def test_python_target_resolves_against_the_command_cwd(monkeypatch, body, expected_block, entry):
    home = home_of(monkeypatch)
    command = 'python3 -c "%s"' % body
    message = worker_terminal(command, workdir=home)
    if expected_block:
        assert message and "home_litter_gate" in message, command
        assert "~/" + entry in message, (command, message)
    else:
        assert message is None, (command, message)


def test_python_relative_target_with_a_safe_workdir_does_not_fire(monkeypatch, tmp_path):
    """The same relative literal with the cwd inside the workspace creates
    nothing under ``~`` and stays allowed."""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    for body in ("import os; os.makedirs('brandnew')",
                 "import shutil; shutil.copy('a', 'brandnew')"):
        assert worker_terminal('python3 -c "%s"' % body, workdir=str(workspace)) is None, body


_SQLITE3_ARG_CASES = (
    ("sqlite3 -cmd '.timer on' brandnew.db 'select 1'", True, "brandnew.db"),
    ("sqlite3 -separator , brandnew.db 'select 1'", True, "brandnew.db"),
    ("sqlite3 -newline '\\n' brandnew.db 'select 1'", True, "brandnew.db"),
    ("sqlite3 -init init.sql brandnew.db 'select 1'", True, "brandnew.db"),
    ("sqlite3 -cmd '.timer on' existing.txt 'select 1'", False, None),
)
"""sqlite3 options that consume a separate argument: the DB operand is the one
after them, so the option's value is not mistaken for the path."""


@pytest.mark.parametrize(
    "command,expected_block,entry", _SQLITE3_ARG_CASES, ids=lambda value: str(value)
)
def test_sqlite3_option_arguments_are_not_the_database(monkeypatch, command, expected_block, entry):
    home = home_of(monkeypatch)
    message = worker_terminal(command, workdir=home)
    if expected_block:
        assert message and "home_litter_gate" in message, command
        assert "~/" + entry in message, (command, message)
    else:
        assert message is None, (command, message)
