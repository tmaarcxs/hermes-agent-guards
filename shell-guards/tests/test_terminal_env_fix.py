import os
import subprocess
import sys

guards = sys.modules["shell_guards"]
fix = guards.terminal_env_fix.fix
POLLUTED = "/Users/x/.hermes/installs/abc/environments/old/venv/lib/python3.14/site-packages"


def run(command, **env):
    changed = fix("terminal", {"command": command})
    rewritten = changed["command"] if changed else command
    return subprocess.run(["bash", "-c", rewritten], capture_output=True, text=True, env={**os.environ, **env})


def test_hermes_install_pythonpath_is_unset():
    assert run('python3 -c "import os;print(\'[%s]\' % os.environ.get(\'PYTHONPATH\', \'\'), end=\'\')"', PYTHONPATH=POLLUTED).stdout == "[]"


def test_user_pythonpath_is_kept():
    assert run('python3 -c "import os;print(\'[%s]\' % os.environ.get(\'PYTHONPATH\', \'\'), end=\'\')"', PYTHONPATH="/opt/mine").stdout == "[/opt/mine]"


def test_pipefail_added_only_for_runner_piped_to_viewer():
    assert "pipefail" in fix("terminal", {"command": "pytest -q 2>&1 | tail -5"})["command"]
    assert "pipefail" not in fix("terminal", {"command": "python3 x.py | head -5"})["command"]
    assert "pipefail" not in fix("terminal", {"command": "pytest -q"})["command"]
    assert fix("terminal", {"command": "git log | head -5"}) is None


def test_failing_runner_through_tail_exits_non_zero():
    assert run("false | tail -1 # pytest").returncode == 1
    assert run("false | tail -1").returncode == 0  # no runner: status unchanged


def test_non_python_commands_are_untouched_and_rewrite_is_idempotent():
    assert fix("terminal", {"command": "ls -la"}) is None
    once = fix("terminal", {"command": "python3 -V"})["command"]
    assert fix("terminal", {"command": once}) is None
    assert fix("read_file", {"command": "python3 -V"}) is None
    assert fix("terminal", {"command": "  "}) is None
