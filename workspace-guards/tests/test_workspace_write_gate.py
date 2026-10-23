import os
import sys

import pytest

guard = sys.modules["workspace_guards"]
gate = guard.workspace_write_gate

WS = "/tmp/fake-task-workspace"

PROTECTED = [
    "~/.hermes/plugins/agent-guards/x.py",
    "~/.hermes/config.yaml",
    "~/.hermes/.env",
    "~/.hermes/profiles/lead/config.yaml",
    "~/.hermes/kanban/workspaces/t_other999/stuff",
    "~/.hermes/hermes-agent/hermes_cli/kanban.py",
]


@pytest.fixture(autouse=True)
def worker_env(monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_self")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_ATTACHMENTS_ROOT", raising=False)


def blocked(tool, args):
    result = gate.check(tool, args)
    return result


def worker_terminal(command, workdir=None):
    args = {"command": command}
    if workdir:
        args["workdir"] = workdir
    return blocked("terminal", args)


def terminal_matrix():
    return [
        ("redirect", "echo hi > {}"),
        ("append", "echo hi >> {}"),
        ("tee", "echo hi | tee {}"),
        ("cp", "cp note.md {}"),
        ("mv", "mv note.md {}"),
        ("rsync", "rsync -a src/ {}"),
        ("install", "install -m 644 note.py {}"),
        ("sed-i", "sed -i '' 's/a/b/' {}"),
        ("rm", "rm -rf {}"),
        ("git-pull", "git -C {} pull"),
        ("git-cd", "cd {} && git pull"),
        ("git-checkout", "git -C {} checkout main"),
        ("git-reset", "git -C {} reset --hard"),
        ("git-clone", "git clone https://example.com/r.git {}"),
        ("python-c", "python3 -c \"import shutil; shutil.rmtree('{}')\""),
        ("heredoc",
         "python3 - <<'EOF'\nimport os, shutil\nshutil.rmtree(os.path.expanduser('{}'))\nEOF"),
    ]


def lead_mode(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)


@pytest.mark.parametrize("mechanism,template", terminal_matrix())
@pytest.mark.parametrize("target", PROTECTED)
def test_worker_terminal_write_into_protected_target_blocked(mechanism, template, target):
    message = worker_terminal(template.format(target))
    assert message and "agent-guards blocked terminal" in message, (mechanism, target)
    assert "workspace" in message


def test_worker_write_file_and_patch_into_protected_paths_blocked():
    for target in PROTECTED:
        assert blocked("write_file", {"path": target, "content": "x"}), target
        assert blocked("patch", {"path": target, "old_string": "a", "new_string": "b"}), target


def test_worker_write_file_relative_path_resolves_inside_workspace(monkeypatch, tmp_path):
    assert blocked("write_file", {"path": "notes/draft.md", "content": "x"}) is None
    monkeypatch.chdir(tmp_path / "ws")
    assert blocked("write_file", {"path": "notes/draft.md", "content": "x"}) is None


def test_worker_terminal_writes_inside_own_workspace_allowed():
    for mechanism, template in terminal_matrix():
        target = "$HERMES_KANBAN_WORKSPACE/out.bin" if mechanism != "git-clone" else "$HERMES_KANBAN_WORKSPACE/clone"
        assert worker_terminal(template.format(target)) is None, mechanism


def test_worker_terminal_git_flow_inside_workspace_allowed():
    command = (
        "cd $HERMES_KANBAN_WORKSPACE && git add -A && git commit -m x && git push -u origin w 2>&1 | tail -2"
    )
    assert worker_terminal(command) is None


def test_worker_terminal_reads_everywhere_allowed():
    reads = [
        "cat ~/.hermes/config.yaml",
        "grep -rn guard ~/.hermes/plugins/agent-guards/*.py | head -5",
        "sed -n '20,60p' ~/.hermes/plugins/agent-guards/review_gate.py",
        "ls ~/.hermes/hermes-agent/venv/bin/python",
        "git -C ~/.hermes/plugins/agent-guards log --oneline -3",
        "git -C ~/.hermes/plugins/agent-guards status",
        "find ~/.hermes -maxdepth 2 -name '*.py'",
    ]
    for command in reads:
        assert worker_terminal(command) is None, command


@pytest.mark.parametrize("target", ["~/.hermes/cache/scratch/probe/x", "$TMPDIR/probe", "/tmp/probe"])
def test_worker_terminal_scratch_and_tmp_targets_allowed(target, monkeypatch):
    monkeypatch.setenv("TMPDIR", "/var/folders/f7/somewhere/T/")
    for mechanism, template in terminal_matrix()[:9]:
        assert worker_terminal(template.format(target)) is None, (mechanism, target)


def test_worker_terminal_kanban_attachments_copy_allowed(monkeypatch, tmp_path):
    """The board's kanban attachments directory is a writable target.

    Everything is synthetic so the allowed root comes from the pinned board home
    and not from any ambient root: ``tmp_path`` lives under the runner's real
    ``TMPDIR``, and ``allowed_roots()`` always allows ``TMPDIR``/``/tmp``, so a
    target merely inside ``tmp_path`` would pass on the TMPDIR allowance alone
    and the kanban-attachments branch would go untested. Pin a synthetic real
    home and a ``TMPDIR`` that does not contain the target, then assert the
    negative control: the same path is blocked once the board home points
    elsewhere, proving the attachment allowance — not an ambient root — is what
    permits the write.
    """
    real_home = tmp_path / "home"
    (real_home / ".hermes").mkdir(parents=True)
    fake_tmp = tmp_path / "fake-tmp"
    fake_tmp.mkdir()
    monkeypatch.setenv("HERMES_REAL_HOME", str(real_home))
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("TMPDIR", str(fake_tmp))
    root = real_home / ".hermes"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_ATTACHMENTS_ROOT", raising=False)
    target = root / "kanban" / "attachments" / "t_self" / "report.md"
    assert worker_terminal("cp report.md %s" % target) is None

    other = tmp_path / "other-kanban-home"
    other.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(other))
    assert "agent-guards blocked terminal" in worker_terminal(
        "cp report.md %s" % target)


def test_worker_terminal_repo_outside_home_hermes_git_mutation_blocked_plain_write_allowed():
    assert "agent-guards blocked terminal" in worker_terminal(
        "git -C ~/git_repos/sample-repo pull")
    assert worker_terminal("cp note.md ~/git_repos/sample-repo/notes.md") is None


def test_worker_terminal_worktree_sibling_workspace_blocked():
    assert "agent-guards blocked terminal" in worker_terminal(
        "cd ~/git_repos/repo && git checkout main")


def test_lead_unaffected(monkeypatch):
    lead_mode(monkeypatch)
    for target in PROTECTED:
        assert blocked("write_file", {"path": target, "content": "x"}) is None
    assert worker_terminal("rm -rf ~/.hermes/plugins/agent-guards") is None


def test_worker_terminal_testhome_suite_command_allowed():
    command = (
        'env HERMES_HOME="$(mktemp -d "$PWD/.hermes-test-home.XXXXXX")" '
        "PYTHONPATH=$HOME/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python -m pytest -q tests"
    )
    assert worker_terminal(command) is None


def test_worker_terminal_python_env_style_write_blocked():
    command = "env FOO=1 python3 -c \"open('/Users/marcos.sanchez/.hermes/plugins/agent-guards/x.py','w').write('x')\""
    assert "agent-guards blocked terminal" in worker_terminal(command)


def test_rewrite_env_and_absolute_target(monkeypatch):
    target = os.path.expanduser("~/.hermes/plugins/agent-guards/x.py")
    for template, _ in [("echo x > {}", None), ("tee {}", None)]:
        assert "agent-guards blocked terminal" in worker_terminal(template.format(target))


def test_multi_segment_command_mixed_cwd():
    command = ("cd $HERMES_KANBAN_WORKSPACE && ls && cd ~/.hermes/plugins/agent-guards && git pull && ls")
    assert "agent-guards blocked terminal" in worker_terminal(command)
    command = ("echo a > ~/.hermes/plugins/agent-guards/x.py; echo b > $HERMES_KANBAN_WORKSPACE/ok.txt")
    assert "agent-guards blocked terminal" in worker_terminal(command)


def test_non_dict_and_missing_args_fail_open():
    assert gate.check("terminal", None) is None
    assert gate.check("terminal", "not-a-dict") is None
    assert gate.check("write_file", {"path": "", "content": "x"}) is None
    assert gate.check("terminal", {"command": ""}) is None


def test_worker_without_workspace_env_still_blocks_outside_writes(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    assert "agent-guards blocked terminal" in worker_terminal("echo x > ~/.hermes/plugins/agent-guards/x.py")


def test_background_terminal_flag_irrelevant():
    assert "agent-guards blocked terminal" in blocked(
        "terminal", {"command": "echo x > ~/.hermes/config.yaml", "background": True})


def test_message_names_allowed_targets():
    message = worker_terminal("echo x > ~/.hermes/config.yaml")
    assert "HERMES_KANBAN_WORKSPACE" in message and "scratch" in message


def test_quoted_target_still_blocked():
    assert "agent-guards blocked terminal" in worker_terminal(
        'cp note.md "~/.hermes/plugins/agent-guards/x.py"')
    assert "agent-guards blocked terminal" in worker_terminal(
        "cp note.md '$HOME/.hermes/plugins/agent-guards/x.py'")


def test_piped_deletion_target_blocked():
    assert "agent-guards blocked terminal" in worker_terminal(
        "echo ~/.hermes/plugins/agent-guards | xargs rm -rf")


READ_ONLY_GIT_SHAPES = [
    "git merge-base main origin/main",
    "git -C ~/git_repos/sample-repo merge-base main origin/main",
    "git -C ~/git_repos/sample-repo fetch origin -q",
    "git fetch origin",
    "git -C ~/git_repos/sample-repo log --oneline -3",
    "git -C ~/git_repos/sample-repo rev-parse HEAD",
    "git -C ~/git_repos/sample-repo ls-remote origin",
    "git -C ~/git_repos/sample-repo cat-file -p HEAD",
    "git -C ~/git_repos/sample-repo worktree list",
    "git worktree list",
    "git -C ~/git_repos/sample-repo worktree show",
    "cd ~/git_repos/sample-repo && git fetch origin",
    "cd ~/git_repos/sample-repo && git merge-base a b",
]


@pytest.mark.parametrize("command", READ_ONLY_GIT_SHAPES)
def test_read_only_git_outside_workspace_allowed(command):
    assert worker_terminal(command) is None, command


GIT_MUTATION_SHAPES = [
    "git -C ~/git_repos/sample-repo push",
    "git -C ~/git_repos/sample-repo commit -m x",
    "git -C ~/git_repos/sample-repo reset --hard",
    "git -C ~/git_repos/sample-repo rebase main",
    "git -C ~/git_repos/sample-repo checkout main",
    "git -C ~/git_repos/sample-repo merge other",
    "git -C ~/git_repos/sample-repo rm a",
    "git -C ~/git_repos/sample-repo mv a b",
    "git -C ~/git_repos/sample-repo clean -fd",
    "git -C ~/git_repos/sample-repo stash",
    "git -C ~/git_repos/sample-repo pull",
    "cd ~/git_repos/sample-repo && git push",
]


@pytest.mark.parametrize("command", GIT_MUTATION_SHAPES)
def test_real_git_mutation_outside_workspace_still_blocked(command):
    assert "agent-guards blocked terminal" in worker_terminal(command), command


def test_worktree_add_into_allowed_zone_allowed():
    assert worker_terminal(
        "git worktree add $HERMES_KANBAN_WORKSPACE/wt -b t_x origin/main") is None
    assert worker_terminal("git worktree add ~/.hermes/cache/scratch/wt -b x origin/main") is None


def test_worktree_add_outside_allowed_zone_blocked():
    assert "agent-guards blocked terminal" in worker_terminal(
        "git worktree add ~/git_repos/sample-repo/wt -b y origin/main")


def test_clone_into_allowed_zone_allowed_outside_blocked():
    assert worker_terminal(
        "git clone https://github.com/a/b.git $HERMES_KANBAN_WORKSPACE/b") is None
    assert "agent-guards blocked terminal" in worker_terminal(
        "git clone https://github.com/a/b.git ~/git_repos/b")


def test_worker_own_profile_cache_write_allowed(monkeypatch, tmp_path):
    """The profile's own ``cache/`` (terminal-output logs) is a legitimate write.

    ``HERMES_HOME`` points at ``<real home>/.hermes/profiles/<own>`` in a worker;
    the old allowlist covered only ``cache/scratch`` and blocked the sibling
    ``cache/terminal-output/`` logs.
    """
    home = tmp_path / "home"
    profile = home / ".hermes" / "profiles" / "worker"
    (profile / "cache" / "terminal-output").mkdir(parents=True)
    fake_tmp = tmp_path / "fake-tmp"
    fake_tmp.mkdir()
    monkeypatch.setenv("HERMES_REAL_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("TMPDIR", str(fake_tmp))
    monkeypatch.setattr(gate._policy, "configured_worker", lambda: "worker")
    target = profile / "cache" / "terminal-output" / "out.log"
    assert worker_terminal("echo x > %s" % target) is None
    assert blocked("write_file", {"path": str(target), "content": "x"}) is None


def test_reports_dir_write_allowed(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".hermes" / "reports").mkdir(parents=True)
    fake_tmp = tmp_path / "fake-tmp"
    fake_tmp.mkdir()
    monkeypatch.setenv("HERMES_REAL_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(fake_tmp))
    assert worker_terminal("echo x > %s/.hermes/reports/r.md" % home) is None
    assert blocked("write_file", {"path": str(home / ".hermes" / "reports" / "r.md"),
                                  "content": "x"}) is None


def test_protected_hermes_paths_still_blocked(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    fake_tmp = tmp_path / "fake-tmp"
    fake_tmp.mkdir()
    monkeypatch.setenv("HERMES_REAL_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(fake_tmp))
    for rel in ("config.yaml", ".env", "profiles/lead/config.yaml",
                "plugins/workspace-guards/x.py"):
        assert "agent-guards blocked terminal" in worker_terminal(
            "echo x > %s/.hermes/%s" % (home, rel)), rel
