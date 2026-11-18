"""Foreground waits run as written: no block, no background rewrite, no note."""

import sys
from pathlib import Path

import pytest

guards = sys.modules["shell_guards"]

PLUGIN_DIR = Path(__file__).resolve().parents[1]

WAITS = [
    "sleep 5; ls",
    "sleep 60; gh pr checks 1",
    "sleep 200; echo done",
    "sleep 2m && gh pr checks 1",
    "for i in 1 2 3; do gh run view 1; sleep 20; done",
    "until [ -f ready ]; do sleep 10; done",
    "read -t 45; echo done",
    'python3 -c "import time; time.sleep(120)"',
    "perl -e 'select(undef,undef,undef,300)'",
    "python3 - <<'PYEOF'\nimport time\ntime.sleep(600)\nPYEOF",
]


def run(command, **extra):
    return guards._pre_tool_call(
        tool_name="terminal", args={"command": command, "background": False, **extra},
        session_id="s", tool_call_id="c1")


def untouched(result):
    if result is None:
        return True
    args = result.get("args") or {}
    return result["action"] == "modify" and "background" not in args and "notify" not in args


@pytest.mark.parametrize("command", WAITS)
def test_a_foreground_wait_is_left_alone(command):
    assert untouched(run(command))


@pytest.mark.parametrize("command", WAITS[:3])
def test_a_foreground_wait_in_a_pty_session_is_left_alone(command):
    assert untouched(run(command, pty=True))


def test_a_wait_beside_another_violation_blocks_only_on_the_violation():
    result = run("sleep 60; git push -f origin main")
    assert result["action"] == "block"
    assert "force" in result["message"].lower() and "sleep" not in result["message"].lower()


def test_a_pgrep_f_loop_is_still_blocked():
    result = run('while pgrep -f "migrate.py" >/dev/null; do sleep 10; done; echo done')
    assert result["action"] == "block" and "pgrep -x" in result["message"]


def test_the_wait_note_module_is_gone():
    assert not (PLUGIN_DIR / "background_wait_note.py").exists()
    assert not hasattr(guards, "background_wait_note")


def test_no_wait_rewrite_helpers_remain():
    policy = guards.terminal_policy
    for name in ("only_waits", "_matches_wait", "_WAIT_FOREGROUND_MESSAGE", "_WAIT_LOOP_MESSAGE",
                 "_MAX_FOREGROUND_SLEEP", "_MAX_LOOP_SLEEP"):
        assert not hasattr(policy, name), name
