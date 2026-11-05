"""The call-deadline clamp: a requested timeout above the host deadline is rewritten below it.

The host kills a sequential tool at ``timeouts.tools.sequential_call`` (default 420 s) and
returns ``Error executing tool '<name>': timed out after 420.0s`` — zero content for the
whole slot. A ``terminal`` foreground call whose ``timeout`` exceeds the deadline always
dies there (the tool's own 600 s promotion never runs), and a ``process_manage`` wait above
the deadline races it. Both are rewritten, before dispatch, to deadline minus a 15 s margin
(405 by default) so the tool's own timeout fires first and returns partial output and status.
Background calls, timeouts at or below the deadline and unparseable values are untouched:
the guard fails open.
"""

import sys

import pytest

guards = sys.modules["shell_guards"]

TERMINAL_COMMAND = "pytest -q -x"


def _rewrite_via(tool_name, base, **args):
    result = guards._pre_tool_call(tool_name=tool_name, args={**base, **args})
    if isinstance(result, dict) and result.get("action") == "modify":
        return {"changed": sorted(result["args"]), "timeout": result["args"].get("timeout")}
    return {"changed": [], "timeout": None}


def _terminal(**args):
    return _rewrite_via("terminal", {"command": TERMINAL_COMMAND}, **args)


def _wait(**args):
    return _rewrite_via("process_manage", {"action": "wait", "session_id": "p1"}, **args)


@pytest.fixture
def deadline_420(monkeypatch):
    monkeypatch.setattr(guards.terminal_policy, "_tool_deadline", lambda: 420.0)


def test_terminal_foreground_timeout_600_clamped_to_deadline_minus_15(deadline_420):
    assert _terminal(timeout=600) == {"changed": ["command", "timeout"], "timeout": 405}


def test_terminal_timeout_untouched_at_or_below_the_deadline(deadline_420):
    assert "timeout" not in _terminal(timeout=300)["changed"]
    assert _terminal(timeout=405)["timeout"] is None
    assert _terminal(timeout=420) == {"changed": ["command", "timeout"], "timeout": 405}


def test_terminal_background_call_untouched(deadline_420):
    assert "timeout" not in _terminal(timeout=600, background=True, notify=True)["changed"]


def test_terminal_call_without_a_timeout_key_untouched(deadline_420):
    assert "timeout" not in _terminal()["changed"]


@pytest.mark.parametrize("bad", ["600", None, True, 12.5, -30, 0])
def test_terminal_non_integer_or_non_positive_timeout_fails_open(deadline_420, bad):
    assert "timeout" not in _terminal(timeout=bad)["changed"]


def test_process_manage_wait_timeout_600_clamped_to_deadline_minus_15(deadline_420, monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    assert _wait(timeout=600) == {"changed": ["timeout"], "timeout": 405}


def test_process_manage_poll_is_never_clamped(deadline_420, monkeypatch):
    monkeypatch.setattr(guards._policy, "configured_worker", lambda: "")
    assert guards._pre_tool_call(
        tool_name="process_manage", args={"action": "poll", "session_id": "p1", "timeout": 600}
    ) is None


def test_deadline_comes_from_the_configured_sequential_call_value(monkeypatch):
    monkeypatch.setattr(guards.terminal_policy, "_tool_deadline", lambda: 900.0)
    untouched = _terminal(timeout=600)
    assert "timeout" not in untouched["changed"]
    assert _terminal(timeout=1000) == {"changed": ["command", "timeout"], "timeout": 885}
    assert _wait(timeout=1000) == {"changed": ["timeout"], "timeout": 885}


def test_deadline_falls_back_to_420_without_a_configured_sequential_call(monkeypatch):
    deadline_holder = {}
    monkeypatch.setattr(guards.terminal_policy, "_tool_deadline", lambda: deadline_holder.get("v"))
    assert "timeout" not in _terminal(timeout=600)["changed"]
    assert _wait(timeout=600)["timeout"] is None
    assert _wait(timeout=801)["timeout"] is None


def test_rewritten_call_is_idempotent(deadline_420, monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    first = _terminal(timeout=600)
    assert first == {"changed": ["command", "timeout"], "timeout": 405}
    renewed = guards._pre_tool_call(tool_name="terminal", args={"command": TERMINAL_COMMAND, "timeout": 405})
    assert not (isinstance(renewed, dict) and renewed.get("action") == "modify" and "timeout" in renewed["args"])
    renewed_wait = guards._pre_tool_call(
        tool_name="process_manage", args={"action": "wait", "session_id": "p1", "timeout": 405})
    assert not (isinstance(renewed_wait, dict) and renewed_wait.get("action") == "modify"
                and "timeout" in renewed_wait["args"])
    assert _wait(timeout=600) == {"changed": ["timeout"], "timeout": 405}


def test_calls_far_above_the_600s_promotion_cap_stay_whole_and_just_get_clamped(deadline_420):
    """Hermes core promotes a foreground timeout above 600 s into a tracked background
    process; the clamp rewinds the call to 405 s instead of letting it reach that
    promotion, and never zeroes, blocks or drops the call."""
    assert _terminal(timeout=1200) == {"changed": ["command", "timeout"], "timeout": 405}


def test_tool_note_zeroes_in_on_the_exact_rewrite_cap_marker(deadline_420, monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    assert guards.terminal_policy.tool_note("done", TERMINAL_COMMAND, 124) is None
    assert guards.terminal_policy.tool_note("[Command timed out after 7s]", TERMINAL_COMMAND, 124) is None
    assert guards.terminal_policy.tool_note(None, TERMINAL_COMMAND, 124) is None
    assert guards.terminal_policy.tool_note(
        "Error executing tool 'terminal': timed out after 420.0s", TERMINAL_COMMAND, 124) is None
    assert guards.terminal_policy.tool_note(
        "arbitrary exit 124 \n[Command timed out after 405s]", TERMINAL_COMMAND, 124) is not None


def test_no_tool_note_on_a_tool_output_without_the_own_timeout_marker(deadline_420, monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    assert guards.terminal_policy.tool_note(
        "plain stdout, rc 0", TERMINAL_COMMAND, 0) is None


def test_no_tool_note_without_the_marker_at_another_exit_code(deadline_420):
    assert guards.terminal_policy.tool_note("all good", TERMINAL_COMMAND, 0) is None
    assert guards.terminal_policy.tool_note("rc 1 crash", TERMINAL_COMMAND, 1) is None


def test_tool_note_on_the_own_405s_timeout_death_of_a_rewritten_call(deadline_420, monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    output = "started... \n[Command timed out after 405s]"
    note = guards.terminal_policy.tool_note(output, TERMINAL_COMMAND, 124)
    assert note is not None and note.startswith(output)
    assert "405s" in note
    assert "terminal(command=<the same command>, background=true, notify=true)" in note


def test_output_transform_note_accepts_the_host_hook_shape(deadline_420, monkeypatch):
    """``transform_terminal_output`` is invoked with command/output/returncode/task_id/
    env_type/tool_call_id (``tools/terminal_tool_result.py``); the note path must accept
    exactly that shape and survive an unknown extra kwarg."""
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")
    note = guards._transform_terminal_output(
        command=TERMINAL_COMMAND, output="[Command timed out after 405s]", returncode=124,
        task_id="t1", env_type="local", tool_call_id="c1", unexpected_kwarg=1)
    assert note is not None and note.startswith("[Command timed out after 405s]")
    assert "terminal(command=<the same command>, background=true, notify=true)" in note
    assert guards._transform_terminal_output(
        command=TERMINAL_COMMAND, output="rc 1", returncode=1, task_id="t1",
        env_type="local", tool_call_id="c1") is None


def test_clamp_transformer_fails_open_on_an_unresolvable_deadline(monkeypatch):
    monkeypatch.setattr(guards.terminal_policy, "_tool_deadline", lambda: None)
    assert guards.terminal_policy.tool_note(
        "[Command timed out after 405s]", TERMINAL_COMMAND, 124) is None


def test_overhead_of_the_clamp_over_100_terminal_calls(deadline_420):
    import time

    calls = 100
    start = time.perf_counter()
    for _ in range(calls):
        guards._pre_tool_call(tool_name="terminal", args={"command": TERMINAL_COMMAND, "timeout": 600})
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert elapsed_ms / calls < 1.0
