"""``repeat_block_gate`` tests: escalate the second identical guard block, nothing else.

production entry point: ``transform`` as registered on ``transform_tool_result``
(the same shape kanban-guards' ``workspace_scope_gate`` tests pin). The block
fixtures are the exact delivered shape in worker profile state.db: a single-line
``{"error": "agent-guards blocked <tool> [guard]: ...`` JSON object.
"""

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "workspace_guards_repeat_block_gate", ROOT / "repeat_block_gate.py")
gate = importlib.util.module_from_spec(spec)
sys.modules.setdefault("workspace_guards", type(sys)("workspace_guards"))
spec.loader.exec_module(gate)


def _block(tool="kanban_request_review", guard="review_gate", text="the pre-review checks are not closed."):
    return '{"error": "agent-guards blocked %s [%s]: %s Fix the call and retry."}' % (tool, guard, text)


@pytest.fixture(autouse=True)
def clean_slate():
    """Every test starts with empty repeat state and no kanban-task pin."""
    gate.reset()
    os.environ.pop("HERMES_KANBAN_TASK", None)
    yield
    gate.reset()
    os.environ.pop("HERMES_KANBAN_TASK", None)


ARGS = {"task_id": "t_2ecb70f0"}


def test_first_block_is_delivered_unchanged():
    result = _block()
    assert gate.transform("kanban_request_review", ARGS, result, "s1") is None


def test_second_identical_block_becomes_escalation():
    gate.transform("kanban_request_review", ARGS, _block(), "s1")
    out = gate.transform("kanban_request_review", ARGS, _block(), "s1")
    assert out is not None and out != _block()
    assert "rejected 2 times" in out and "review_gate" in out
    assert _block(guard="review_gate") in out


def test_a_passing_call_between_resets():
    gate.transform("kanban_request_review", ARGS, _block(), "s1")
    gate.transform("kanban_request_review", ARGS, '{"error": "no-args"}', "s1")
    assert gate.transform("kanban_request_review", ARGS, _block(), "s1") is None


def test_second_identical_attempt_that_passes_is_untouched():
    """Acceptance 2: no pre-emptive block exists anywhere — a call the guards now
    allow passes with its real result, byte for byte."""
    gate.transform("kanban_request_review", ARGS, _block(), "s1")
    passed = '{"task": {"id": "t_x", "status": "review_requested"}}'
    assert gate.transform("kanban_request_review", ARGS, passed, "s1") is None


def test_different_args_do_not_share_state():
    gate.transform("kanban_request_review", {"task_id": "t_a"}, _block(), "s1")
    assert gate.transform("kanban_request_review", {"task_id": "t_b"}, _block(), "s1") is None


def test_different_tool_and_different_guard_do_not_share_state():
    gate.transform("kanban_request_review", ARGS, _block("kanban_request_review", "review_gate"), "s1")
    assert gate.transform("kanban_complete", ARGS, _block("kanban_complete", "review_gate"), "s1") is None
    assert gate.transform(
        "kanban_request_review", ARGS, _block("kanban_request_review", "completion_gate"), "s1") is None


def test_different_sessions_do_not_share_state():
    gate.transform("kanban_request_review", ARGS, _block(), "s1")
    assert gate.transform("kanban_request_review", ARGS, _block(), "s2") is None


def test_worker_session_gets_kanban_block_instruction(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_2ecb70f0")
    gate.transform("kanban_request_review", ARGS, _block(), "s1")
    out = gate.transform("kanban_request_review", ARGS, _block(), "s1")
    assert 'kanban_block(reason="review_gate' in out and "review_gate" in out
    assert _block() in out


def test_non_worker_session_gets_change_the_call_instruction(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    gate.transform("kanban_request_review", ARGS, _block(), "s1")
    out = gate.transform("kanban_request_review", ARGS, _block(), "s1")
    assert "kanban_block" not in out and "make that one change" in out
    assert "review_gate" in out and _block() in out


def test_escalation_counts_grow_with_repeats():
    gate.transform("kanban_request_review", ARGS, _block(), "s1")
    out2 = gate.transform("kanban_request_review", ARGS, _block(), "s1")
    out3 = gate.transform("kanban_request_review", ARGS, _block(), "s1")
    assert "rejected 2 times" in out2 and "rejected 3 times" in out3


def test_handoff_bounce_is_an_escape_hatch_never_escalated():
    """The handoff gate bounces kanban_block: escalating a repeat of the escape hatch
    with its own name as the remedy would be absurd; it passes untouched forever."""
    bounce = '{"error": "agent-guards bounced kanban_block: the reason says your part is finished..."}'
    gate.transform("kanban_block", {"reason": "x"}, bounce, "s1")
    gate.transform("kanban_block", {"reason": "x"}, bounce, "s1")
    assert gate.transform("kanban_block", {"reason": "x"}, bounce, "s1") is None


def test_state_bounded_over_ten_thousand_distinct_blocks():
    """Acceptance 5: 10k distinct blocked calls never grow the tracked state."""
    start = time.process_time()
    for i in range(10_000):
        assert gate.transform("kanban_request_review", {"n": i}, _block(), "s1") is None
    assert len(gate._sessions) == 1
    gate.reset()
    assert time.process_time() - start < 10.0


def test_fail_open_on_internal_error():
    """Acceptance 5: an exception inside the transform returns the original result — via the
    registered wrapper, never raising out of the hook."""
    assert gate.transform("kanban_request_review", ARGS, _block(), "s1") is None
    gate._state_for("s1").key = 12345  # poison: attribute errors inside the path
    assert gate.transform("kanban_request_review", ARGS, _block(), "s1") is None


def test_non_block_results_untouched():
    assert gate.transform("kanban_request_review", ARGS, "plain text", "s1") is None
    assert gate.transform("kanban_request_review", ARGS, {"error": "x"}, "s1") is None
    assert gate.transform("kanban_request_review", ARGS, None, "s1") is None


def test_foreign_error_payload_with_guard_prefix_but_wrong_tool_untouched():
    """Only the call's own tool can match: a result whose embedded tool differs is not ours."""
    assert gate.transform("kanban_complete", ARGS, _block(tool="kanban_request_review"), "s1") is None


def test_overhead_under_one_millisecond_per_call():
    """Acceptance 6: warm-path cost of 100 non-block transforms, per call."""
    gate.transform("kanban_request_review", ARGS, "plain", "s1")
    other = '{"tasks": [], "count": 0}'
    start = time.perf_counter()
    for _ in range(100):
        gate.transform("kanban_request_review", ARGS, other, "s1")
    per_call_ms = (time.perf_counter() - start) / 100 * 1000
    assert per_call_ms < 1.0, per_call_ms


def test_registered_rides_transform_tool_result_and_tool_result_reaches_registered_hook():
    """The registered wiring carries the same acceptance behavior end to end."""
    import types

    package = types.ModuleType("workspace_guards")
    package.__path__ = [str(ROOT)]
    sys.modules["workspace_guards_real"] = package
    real = importlib.import_module("workspace_guards.__init__")
    sys.modules.pop("workspace_guards_real")
    seen = {}

    class Probe:
        def register_hook(self, name, fn):
            seen.setdefault(name, []).append(fn)

    real.register(Probe())
    assert "transform_tool_result" in seen
    hook = seen["transform_tool_result"][0]
    kwargs = {"tool_name": "kanban_request_review", "args": ARGS, "result": _block(),
              "session_id": "s1", "task_id": "", "status": "error"}
    assert hook(**kwargs) is None
    escalated = hook(**kwargs)
    assert escalated is not None and "rejected 2 times" in escalated
