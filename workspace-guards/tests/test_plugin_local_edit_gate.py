"""The plugin local-edit gate: installed plugin checkouts are never edited.

Marcos corrected the same mistake twice (2026-11-01 and 2026-11-05): a guard or
plugin change goes to the working clone under ~/git_repos, gets pushed, and is
then synced with ``hermes -p <profile> plugins update <name>`` — never written
into the installed copy under ``~/.hermes/plugins/<name>/`` or a profile's
``plugins`` directory. ``plugin_source_gate`` only refuses an *update* whose
tree is already dirty, so nothing stops the edit itself. This gate does.
"""

import os
import subprocess
import sys
import time

import pytest

guards = sys.modules["workspace_guards"]
gate = guards.plugin_local_edit_gate

ORIGIN = "https://example.com/tmaarcxs/plugA.git"
ORIGIN_B = "https://example.com/tmaarcxs/plugB.git"


@pytest.fixture(autouse=True)
def hermes_home(monkeypatch, tmp_path):
    """A synthetic home holding installed plugin checkouts and the matching clone."""
    home = tmp_path / "home"
    (home / "git_repos" / "plugA").mkdir(parents=True)
    (home / "git_repos" / "unrelated").mkdir()
    (home / ".hermes" / "plugins" / "plugA").mkdir(parents=True)
    (home / ".hermes" / "profiles" / "worker" / "plugins" / "plugA").mkdir(parents=True)
    (home / ".hermes" / "profiles" / "reviewer" / "plugins" / "plugA").mkdir(parents=True)
    (home / ".hermes" / "plugins" / "plain").mkdir(parents=True)
    make_checkout(home / ".hermes" / "plugins" / "plugA", ORIGIN)
    make_checkout(home / ".hermes" / "profiles" / "worker" / "plugins" / "plugA", ORIGIN)
    make_checkout(home / ".hermes" / "profiles" / "reviewer" / "plugins" / "plugA", ORIGIN)
    make_checkout(home / "git_repos" / "plugA", ORIGIN)
    make_checkout(home / "git_repos" / "unrelated", ORIGIN_B)
    worktree_root = home / "git_repos" / "hermes-agent-guards" / ".worktrees" / "t_x"
    worktree_root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_REAL_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def home(hermes_home):
    return hermes_home


def make_checkout(path, origin):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "remote.origin.url", origin], check=True)


def blocked(tool, args):
    return gate.check(tool, args)


def terminal(command, home):
    return blocked("terminal", {"command": command, "workdir": str(home)})


def test_gate_is_registered_under_the_canonical_hook_name():
    class Probe:
        def __init__(self):
            self.hooks = {}

        def register_hook(self, name, fn):
            self.hooks.setdefault(name, []).append(fn)

    probe = Probe()
    guards.register(probe)
    assert probe.hooks.get("pre_tool_call")


def test_write_file_into_global_installed_checkout_blocked():
    home = os.environ["HOME"]
    path = os.path.join(home, ".hermes", "plugins", "plugA", "gate.py")
    message = blocked("write_file", {"path": path, "content": "x"})
    assert message and "plugin_local_edit_gate" in message and "agent-guards blocked" in message


def test_patch_into_global_installed_checkout_blocked():
    home = os.environ["HOME"]
    path = os.path.join(home, ".hermes", "plugins", "plugA", "gate.py")
    assert blocked("patch", {"path": path, "old_string": "a", "new_string": "b"})


def test_write_file_into_profile_installed_checkout_blocked():
    home = os.environ["HOME"]
    path = os.path.join(home, ".hermes", "profiles", "worker", "plugins", "plugA", "x.py")
    assert blocked("write_file", {"path": path, "content": "x"})


def test_terminal_redirect_into_installed_checkout_blocked(home):
    target = home / ".hermes" / "plugins" / "plugA" / "x.py"
    assert terminal(f"echo x > {target}", home)


def test_terminal_sed_in_place_in_installed_checkout_blocked(home):
    target = home / ".hermes" / "plugins" / "plugA" / "x.py"
    assert terminal(f"sed -i '' 's/a/b/' {target}", home)


def test_terminal_tee_in_installed_checkout_blocked(home):
    target = home / ".hermes" / "plugins" / "plugA" / "x.log"
    assert terminal(f"echo hi | tee {target}", home)


def test_terminal_python_inline_write_into_installed_checkout_blocked(home):
    target = home / ".hermes" / "plugins" / "plugA" / "x.py"
    assert terminal(f"python -c \"open('{target}', 'w').write('x')\"", home)


def test_terminal_cp_into_installed_checkout_blocked(home):
    target = home / ".hermes" / "plugins" / "plugA"
    assert terminal("cp /tmp/a.py " + str(target), home)


def test_terminal_mv_into_installed_checkout_blocked(home):
    target = home / ".hermes" / "plugins" / "plugA"
    assert terminal("mv /tmp/a.py " + str(target), home)


def test_terminal_rm_in_installed_checkout_blocked(home):
    target = home / ".hermes" / "plugins" / "plugA" / "x.py"
    assert terminal(f"rm {target}", home)


def test_terminal_touch_in_installed_checkout_blocked(home):
    target = home / ".hermes" / "plugins" / "plugA" / "x.py"
    assert terminal(f"touch {target}", home)


def test_terminal_mkdir_in_installed_checkout_blocked(home):
    target = home / ".hermes" / "plugins" / "plugA" / "newdir"
    assert terminal(f"mkdir -p {target}", home)


def test_terminal_git_checkout_in_installed_checkout_blocked(home):
    target = home / ".hermes" / "plugins" / "plugA" / "x.py"
    assert terminal(f"git -C {home / '.hermes/plugins/plugA'} checkout -- {target}", home)


def test_terminal_git_restore_in_installed_checkout_blocked(home):
    top = home / ".hermes" / "plugins" / "plugA"
    assert terminal(f"git -C {top} restore x.py", home)


def test_terminal_git_apply_in_installed_checkout_blocked(home):
    top = home / ".hermes" / "plugins" / "plugA"
    assert terminal(f"git -C {top} apply fix.patch", home)


def test_read_file_of_installed_plugin_allowed(home):
    target = home / ".hermes" / "plugins" / "plugA" / "x.py"
    assert blocked("read_file", {"path": str(target)}) is None


def test_terminal_cat_of_installed_plugin_allowed(home):
    target = home / ".hermes" / "plugins" / "plugA" / "x.py"
    assert terminal(f"cat {target}", home) is None


def test_plugins_update_locked_to_allowed():
    assert terminal("hermes -p worker plugins update hermes-agent-guards", "") is None


def test_git_fetch_pull_status_rebase_in_installed_checkout_allowed(home):
    top = home / ".hermes" / "plugins" / "plugA"
    assert terminal(f"git -C {top} fetch", home) is None
    assert terminal(f"git -C {top} pull --ff-only", home) is None
    assert terminal(f"git -C {top} status", home) is None
    assert terminal(f"git -C {top} rebase origin/main", home) is None


def test_write_inside_worktree_of_working_clone_allowed(home):
    path = home / "git_repos" / "hermes-agent-guards" / ".worktrees" / "t_x" / "gate.py"
    assert blocked("write_file", {"path": str(path), "content": "x"}) is None


def test_write_into_non_git_plugin_dir_allowed(home):
    path = home / ".hermes" / "plugins" / "plain" / "x.py"
    assert blocked("write_file", {"path": str(path), "content": "x"}) is None


def test_message_names_clone_path_worktree_command_and_sync_command():
    home = os.environ["HOME"]
    path = os.path.join(home, ".hermes", "plugins", "plugA", "gate.py")
    message = blocked("write_file", {"path": path, "content": "x"})
    assert message
    assert os.path.join(home, "git_repos", "plugA") in message
    assert "worktree add" in message
    assert "worktree add" in message and "origin/main" in message
    assert "plugins update plugA" in message
    assert "-p worker" in message
    assert "-p reviewer" in message


def test_message_without_matching_clone_tells_to_clone_first(tmp_path, monkeypatch):
    home = tmp_path / "home-only"
    home.mkdir()
    (home / "git_repos").mkdir(parents=True)
    (home / ".hermes" / "plugins" / "plugA").mkdir(parents=True)
    make_checkout(home / ".hermes" / "plugins" / "plugA", ORIGIN)
    monkeypatch.setenv("HERMES_REAL_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    path = home / ".hermes" / "plugins" / "plugA" / "gate.py"
    message = blocked("write_file", {"path": str(path), "content": "x"})
    assert message
    assert "git clone" in message
    assert ORIGIN in message
    assert "git_repos" in message


def test_fail_open_on_path_resolution_error():
    assert blocked("write_file", {"path": "/tmp/a\x00b", "content": "x"}) is None


def test_fail_open_on_missing_git(monkeypatch, tmp_path):
    home = tmp_path / "home"
    target = home / ".hermes" / "plugins" / "plugA" / "x.py"
    monkeypatch.setattr(gate, "_git", lambda *args, **kwargs: None)
    assert blocked("write_file", {"path": str(target), "content": "x"}) is None


def test_fail_open_on_git_timeout(monkeypatch, tmp_path):
    home = tmp_path / "home"
    target = home / ".hermes" / "plugins" / "plugA" / "x.py"

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=0.01)

    monkeypatch.setattr(gate.subprocess, "run", boom)
    assert blocked("write_file", {"path": str(target), "content": "x"}) is None


def test_under_50ms_per_non_plugin_write_call():
    home = os.environ["HOME"]
    path = os.path.join(home, "git_repos", "notes.md")
    start = time.monotonic()
    for _ in range(100):
        assert blocked("write_file", {"path": path, "content": "x"}) is None
    elapsed_ms = (time.monotonic() - start) * 1000
    per_call = elapsed_ms / 100
    print(f"\nmeasured: {elapsed_ms:.1f} ms total, {per_call:.3f} ms per non-plugin write call")
    assert per_call < 50
