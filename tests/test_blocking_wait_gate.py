"""Cap blocking ``process_manage`` waits in a Kanban worker session.

A worker session cannot receive a completion notification (Hermes strips
``notify_on_complete`` whenever ``HERMES_KANBAN_TASK`` is set), so an over-cap
blocking wait is idle time; the guard blocks it with the alternatives that exist
(short re-wait, non-blocking poll, or ``kanban_block``). The lead is untouched.
"""

import sys

import pytest

guards = sys.modules["agent_guards"]
gate = guards.blocking_wait_gate


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_wait01")


def call(args, tool="process_manage"):
    result = guards._pre_tool_call(tool_name=tool, args=args, session_id="s-wait")
    return None if result and result.get("action") == "modify" else result


def test_over_cap_wait_is_blocked():
    result = call({"action": "wait", "session_id": "proc_1", "timeout": 420})
    assert result["action"] == "block"
    assert "agent-guards blocked process_manage" in result["message"]


def test_block_message_names_the_alternatives():
    result = call({"action": "wait", "session_id": "proc_1", "timeout": 600})
    message = result["message"]
    assert "timeout<=60" in message
    assert "poll" in message
    assert "kanban_block" in message


def test_block_message_carries_the_retry_demand():
    result = call({"action": "wait", "session_id": "proc_1", "timeout": 300})
    assert "retry it in this same response" in result["message"]


def test_wait_at_the_cap_passes():
    assert call({"action": "wait", "session_id": "proc_1", "timeout": 60}) is None


def test_short_wait_passes():
    assert call({"action": "wait", "session_id": "proc_1", "timeout": 5}) is None


def test_bare_wait_is_blocked():
    result = call({"action": "wait", "session_id": "proc_1"})
    assert result["action"] == "block"


def test_boolean_timeout_is_not_a_short_wait():
    result = call({"action": "wait", "session_id": "proc_1", "timeout": True})
    assert result["action"] == "block"


@pytest.mark.parametrize("action", ["poll", "log", "kill", "write", "submit", "close", "handoff", "list"])
def test_other_actions_are_untouched(action):
    assert call({"action": action, "session_id": "proc_1", "timeout": 900}) is None


def test_lead_process_is_exempt(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert call({"action": "wait", "session_id": "proc_1", "timeout": 600}) is None


def test_without_configured_worker_nothing_is_enforced(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert call({"action": "wait", "session_id": "proc_1", "timeout": 600}) is None


def test_other_tools_are_untouched():
    assert call({"action": "wait", "timeout": 600}, tool="terminal") is None


def test_non_dict_args_fail_open():
    assert gate.check("process_manage", None) is None


def test_cap_is_sixty_seconds():
    assert gate.MAX_WAIT_SECONDS == 60
