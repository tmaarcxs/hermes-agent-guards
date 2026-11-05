import os
import subprocess
import sys

guards = sys.modules["shell_guards"]
fix = guards.terminal_env_fix.fix
POLLUTED = "/Users/x/.hermes/installs/abc/environments/old/venv/lib/python3.14/site-packages"


def run(command, **env):
    os.environ.pop("PYTHONPATH", None)
    os.environ.update(env)
    changed = fix("terminal", {"command": command})
    rewritten = changed["command"] if changed else command
    return subprocess.run(["bash", "-c", rewritten], capture_output=True, text=True, env=dict(os.environ))


def test_hermes_install_pythonpath_is_unset():
    assert run('printf "[%s]" "$PYTHONPATH"', PYTHONPATH=POLLUTED).stdout == "[]"


def test_user_pythonpath_is_kept():
    assert run('printf "[%s]" "$PYTHONPATH"', PYTHONPATH="/opt/mine").stdout == "[/opt/mine]"


def test_pipefail_added_only_for_runner_piped_to_viewer():
    os.environ.pop("PYTHONPATH", None)
    assert "pipefail" in fix("terminal", {"command": "pytest -q 2>&1 | tail -5"})["command"]
    assert fix("terminal", {"command": "git log | head -5"}) is None
    assert fix("terminal", {"command": "pytest -q"}) is None


def test_failing_runner_through_tail_exits_non_zero():
    assert run("false | tail -1 # pytest").returncode == 1
    assert run("false | tail -1").returncode == 0  # no runner: status unchanged


def test_no_change_when_nothing_to_fix_and_idempotent():
    os.environ.pop("PYTHONPATH", None)
    assert fix("terminal", {"command": "ls"}) is None
    os.environ["PYTHONPATH"] = POLLUTED
    once = fix("terminal", {"command": "ls"})["command"]
    assert fix("terminal", {"command": once}) is None
    os.environ.pop("PYTHONPATH", None)
    assert fix("read_file", {"command": "ls"}) is None
    assert fix("terminal", {"command": "  "}) is None
