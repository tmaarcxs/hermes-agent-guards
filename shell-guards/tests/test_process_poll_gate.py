import sys

import pytest

guards = sys.modules["shell_guards"]
gate = guards.process_poll_gate


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setattr(guards._policy, "configured_worker", lambda: "worker")


def test_poll_is_blocked():
    message = gate.check("process_manage", {"action": "poll", "session_id": "proc_1"})
    assert message and "poll is not allowed" in message
    assert 'process_manage(action="wait", session_id=...)' in message


def test_other_actions_are_not_blocked():
    for action in ("wait", "log", "list", "kill", "write", "submit", "close"):
        assert gate.check("process_manage", {"action": action, "session_id": "proc_1"}) is None


def test_other_tools_are_not_blocked():
    assert gate.check("terminal", {"action": "poll"}) is None


def test_poll_is_not_blocked_without_a_configured_worker(monkeypatch):
    monkeypatch.setattr(guards._policy, "configured_worker", lambda: "")
    assert gate.check("process_manage", {"action": "poll"}) is None


def test_plugin_hook_blocks_poll_with_its_name():
    result = guards._pre_tool_call(tool_name="process_manage", args={"action": "poll", "session_id": "proc_1"})
    assert result["action"] == "block"
    assert "[process_poll_gate]" in result["message"]


def test_short_wait_runs_to_the_limit(monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    assert gate.fix("process_manage", {"action": "wait", "timeout": 5}) == {"timeout": 180}
    result = guards._pre_tool_call(tool_name="process_manage", args={"action": "wait", "timeout": 5})
    assert result == {"action": "modify", "args": {"timeout": 180}}


def test_wait_without_timeout_or_at_the_limit_is_untouched(monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    assert gate.fix("process_manage", {"action": "wait"}) is None
    assert gate.fix("process_manage", {"action": "wait", "timeout": 180}) is None
    assert gate.fix("process_manage", {"action": "wait", "timeout": 600}) is None


def test_wait_limit_follows_terminal_timeout(monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "60")
    assert gate.fix("process_manage", {"action": "wait", "timeout": 5}) == {"timeout": 60}
    monkeypatch.setenv("TERMINAL_TIMEOUT", "not-a-number")
    assert gate.fix("process_manage", {"action": "wait", "timeout": 5}) == {"timeout": 180}
