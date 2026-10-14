import sys

import pytest

guards = sys.modules["agent_guards"]
gate = guards.stop_gate

NUDGE_TEXT = "kanban_complete"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    gate.reset()
    yield
    gate.reset()


def continued(session="s1"):
    result = gate.check(session_id=session)
    return result is not None and result.get("action") == "continue"


def terminal_ok(session="s1", tool="kanban_request_review"):
    guards._post_tool_call(tool_name=tool, args={}, result="{}", status="ok", session_id=session)


def terminal_blocked(session="s1", tool="kanban_request_review"):
    guards._post_tool_call(
        tool_name=tool, args={}, result='{"error": "blocked"}', status="error",
        error_message="agent-guards blocked kanban_request_review: ...", session_id=session,
    )


def test_blocks_nothing_and_continues_when_no_lifecycle_call_ran():
    assert continued()


def test_blocked_terminal_call_does_not_stop_the_continue():
    terminal_blocked()
    assert continued()


def test_successful_completion_stops_the_continue():
    terminal_ok(tool="kanban_complete")
    assert not continued()


def test_successful_review_stops_the_continue():
    terminal_ok(tool="kanban_request_review")
    assert not continued()


def test_session_scoped():
    terminal_ok(session="done")
    assert continued("other")
    assert not continued("done")


def test_lead_process_and_non_worker_setups_are_untouched(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert gate.check(session_id="s1") is None
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert gate.check(session_id="s1") is None


def test_message_tells_the_model_to_call_the_tool():
    result = gate.check(session_id="s1")
    assert "kanban_complete" in result["message"]
    assert "kanban_request_review" in result["message"]
    assert "kanban_block" in result["message"]


@pytest.mark.parametrize("tool", ["kanban_block", "kanban_schedule", "kanban_request_changes"])
def test_every_terminal_tool_counts(tool):
    terminal_ok(tool=tool)
    assert not continued()


def test_error_statuses_do_not_count():
    guards._post_tool_call(tool_name="kanban_complete", args={}, result="{}", status="error", session_id="s1")
    assert continued()


def test_session_cache_is_bounded():
    for i in range(gate._MAX_SESSIONS + 5):
        terminal_ok(session=f"s{i}")
    assert len(gate._handed_off) == gate._MAX_SESSIONS
    assert continued("s0")  # evicted from the cache: back to continue-on-stop
    assert not continued("s204")  # newest marks stay
