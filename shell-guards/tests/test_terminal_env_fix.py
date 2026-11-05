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


def test_recursive_grep_skips_the_bulk_directories(tmp_path):
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules" / "pkg" / "a.txt").write_text("needle\n")
    (tmp_path / ".git" / "b.txt").write_text("needle\n")
    (tmp_path / "src" / "c.txt").write_text("needle\n")
    out = subprocess.run(["bash", "-c", fix("terminal", {"command": "grep -rn needle ."})["command"]], capture_output=True, text=True, cwd=tmp_path).stdout
    assert "src/c.txt" in out and "node_modules" not in out and ".git" not in out


def test_grep_rewrite_is_scoped_and_idempotent():
    rewritten = fix("terminal", {"command": "cd repo && grep -rln foo src | head -5"})["command"]
    assert "grep --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=.venv -rln foo src | head -5" in rewritten
    assert fix("terminal", {"command": rewritten}) is None
    for untouched in (
        "grep foo file.txt",
        "grep -n foo file.txt",
        "grep -rn foo . --exclude-dir=dist",
        "grep -rn react node_modules/react/package.json",
        "git grep -n foo",
        'echo "grep -rn foo ."',
        "cat <<'EOF'\ngrep -rn foo .\nEOF",
        "ls -r",
    ):
        assert fix("terminal", {"command": untouched}) is None, untouched
