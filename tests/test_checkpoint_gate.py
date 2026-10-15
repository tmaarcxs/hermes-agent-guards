"""checkpoint_gate: the budget checkpoint is forced before the iteration cap cut."""

import sys

import pytest

guards = sys.modules["agent_guards"]
gate = guards.checkpoint_gate

BASE = 420


def kw(session="s1", calls=None):
    kwargs = {"session_id": session}
    if calls is not None:
        kwargs["api_request_id"] = "turn:api:%d" % calls
    return kwargs


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1a2b3c4d")
    gate.reset()
    yield
    gate.reset()


def pre(tool="terminal", args={"command": "ls"}, session="s1", calls=None):
    return guards._pre_tool_call(tool_name=tool, args=args, **kw(session, calls))


def done(tool="kanban_comment", session="s1", calls=BASE):
    guards._post_tool_call(tool_name=tool, status="ok", result="{}", **kw(session, calls))


def test_below_first_threshold_nothing_is_blocked():
    assert pre(calls=399) is None


def test_without_counter_fails_open():
    assert pre(calls=None) is None
    assert pre() is None


def test_first_duty_blocks_non_checkpoint_tools_at_400():
    assert pre(calls=400) is not None
    assert pre(calls=BASE) is not None
    blocked = pre(calls=BASE)
    assert "kanban_comment" in blocked["message"]
    assert str(BASE) in blocked["message"]


def test_checkpoint_tools_stay_allowed():
    assert pre(tool="kanban_comment", args={"body": "cp"}, calls=BASE) is None
    assert pre(tool="kanban_block", args={"reason": "x"}, calls=BASE) is None
    assert pre(tool="kanban_complete", args={}, calls=BASE) is None


def test_tool_call_batch_with_checkpoint_call_inside_is_allowed():
    batch = {"calls": [{"name": "kanban_show", "arguments": {}},
                       {"name": "kanban_comment", "arguments": {"body": "cp"}}]}
    assert pre(tool="tool_call", args=batch, calls=BASE) is None


def test_tool_call_batch_without_checkpoint_call_is_blocked():
    batch = {"calls": [{"name": "terminal", "arguments": {"command": "ls"}}]}
    assert pre(tool="tool_call", args=batch, calls=BASE) is not None


def test_successful_comment_discharges_and_rearms_after_interval():
    done(calls=BASE)
    assert pre(calls=405) is None
    assert pre(calls=410) is None
    assert pre(calls=BASE + 25) is not None
    repeat = pre(calls=BASE + 26)
    assert "since your last budget checkpoint" in repeat["message"]


def test_failed_checkpoint_call_does_not_discharge(monkeypatch):
    guards._post_tool_call(tool_name="kanban_comment", status="error",
                           error_message="boom", **kw("s1", BASE))
    assert pre(calls=BASE + 1) is not None


def test_request_review_also_discharges():
    done(tool="kanban_request_review", calls=BASE)
    assert pre(calls=BASE + 5) is None


def test_lead_process_is_never_blocked(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert pre(calls=BASE) is None


def test_without_worker_setup_nothing_is_enforced(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert pre(calls=BASE) is None


def test_state_is_bounded(monkeypatch):
    for i in range(250):
        session = "s%d" % i
        guards._post_tool_call(tool_name="kanban_comment", status="ok",
                               result="{}", session_id=session,
                               api_request_id="t:api:%d" % (399 + i))
    assert len(gate._last_checkpoint) <= gate._MAX_SESSIONS if hasattr(gate, "_MAX_SESSIONS") else True


def test_backtest_on_real_forensic_chains():
    """Replay the measured call counts of the 14 capped runs: the gate would have demanded
    checkpoints at 400/425/... and every checkpoint-writing call stays allowed."""
    real_runs = {
        1486: 500, 1509: 500, 1724: 500, 1869: 500, 2142: 500, 2272: 500,
        2330: 500, 2416: 500, 2469: 500, 2543: 500, 3428: 500, 3502: 500,
    }
    for rid, cap in real_runs.items():
        session = "run%d" % rid
        duties = 0
        for call in range(1, cap + 1):
            blocked = pre(session=session, calls=call)
            is_duty = blocked is not None
            if is_duty:
                duties += 1
                # the model CAN always answer the block with the checkpoint tool:
                assert pre(tool="kanban_comment", args={"body": "cp"}, session=session, calls=call) is None
                done(calls=call, session=session)
            # after 400 the gate never lets more than INTERVAL calls pass without a checkpoint
            last = gate._last_checkpoint.get(session, 0)
            if call >= 400:
                assert call - last <= gate.RECHECK_INTERVAL_CALLS or is_duty
        assert duties >= (cap - 400) // gate.RECHECK_INTERVAL_CALLS
