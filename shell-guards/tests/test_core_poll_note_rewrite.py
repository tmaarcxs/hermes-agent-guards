"""The core `poll it with process(action="poll")` instruction is rewritten plugin-side.

Hermes core promotes an over-cap foreground `terminal` call to a tracked background
process and attaches a note telling the model to poll it ("This session cannot receive
completion notifications, so poll it with process(action=\\\"poll\\\", session_id=...)
until it exits."). `process_poll_gate` blocks `poll`, so a worker following that note
loops between core's text and the guard. A `transform_tool_result` runs on every tool
result AFTER the tool returns — including this one — so shell-guards rewrites the
poll-only note to the instruction consistent with the guards: `process_manage wait`
returns at the process exit, `log` reads the output, a job already launched with
notify sends its completion as a message. Wait-blocked sessions are told to wait
in a later turn — never poll, never sleep.
"""

import sys

import pytest

guards = sys.modules["shell_guards"]


POLL_ONLY_RESULT = (
    '{"session_id": "proc_1", "command": "python3 big.py", "status": "running", '
    '"promoted_from_foreground": "Requested foreground timeout 900s exceeds the 600s cap, '
    "so this command was started as a tracked background process instead of being refused. "
    'Do NOT re-run it. This session cannot receive completion notifications, so poll it with '
    'process(action=\\"poll\\", session_id=...) until it exits."}'
)


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setattr(guards._policy, "configured_worker", lambda: "worker")


def test_poll_only_note_is_rewritten():
    rewritten = guards.terminal_policy.rewrite_poll_instructions(POLL_ONLY_RESULT)
    assert rewritten is not None and rewritten != POLL_ONLY_RESULT
    assert 'poll it with process(action=\\"poll\\"' not in rewritten


def test_rewritten_note_names_wait_and_log():
    rewritten = guards.terminal_policy.rewrite_poll_instructions(POLL_ONLY_RESULT)
    note = __import__("json").loads(rewritten)["promoted_from_foreground"]
    assert 'process_manage(action="wait", session_id=<session_id>)' in note
    assert 'process_manage(action="log", session_id=<session_id>)' in note
    assert "END the turn and re-issue the wait" in note


def test_notify_note_keeps_its_poll_hint():
    note = (
        "Requested foreground timeout 900s exceeds the 600s cap, so this command was started as a "
        "tracked background process with notify_on_complete=true instead of being refused. Do NOT "
        "re-run it. Its completion (exit code + output tail) arrives as a notification; poll with "
        'process(action=\\"poll\\", session_id=...) if you need it sooner.'
    )
    result = '{"session_id": "proc_1", "promoted_from_foreground": "%s"}' % note
    assert guards.terminal_policy.rewrite_poll_instructions(result) is None


def test_non_promoted_results_pass_through():
    assert guards.terminal_policy.rewrite_poll_instructions('{"status": "ok"}') is None
    assert guards.terminal_policy.rewrite_poll_instructions("plain text output") is None


def test_transform_hook_rewrites_the_poll_only_result():
    rewritten = guards._transform_tool_result(
        tool_name="terminal", args={"command": "python3 big.py"}, result=POLL_ONLY_RESULT
    )
    assert rewritten and "END the turn" in rewritten


def test_transform_hook_leaves_everything_else_alone():
    assert guards._transform_tool_result(tool_name="terminal", args={}, result="hello") is None
    assert guards._transform_tool_result(
        tool_name="read_file", args={}, result='{"promoted_from_foreground": "unrelated"}'
    ) is None


def test_rewrite_fails_open_on_broken_json():
    assert guards.terminal_policy.rewrite_poll_instructions('{"promoted": open') is None
