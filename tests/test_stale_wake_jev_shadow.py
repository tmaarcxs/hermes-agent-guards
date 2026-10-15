"""Shadow-only Jev ask in the stale wake: it logs a decision row with the card id
and the action, and never changes whether a wake happens (including on Jev failure).
"""

import sys

import pytest

from agent_guards import jev

wake = sys.modules["agent_guards"].stale_wake

from test_stale_wake import NOW, Recorder, board, card


@pytest.fixture()
def rows(monkeypatch):
    seen = []

    def fake_decide(state, questions, timeout=2.0, caller="", context=None, gate_verdict=None, action=None):
        seen.append({"caller": caller, "state": state, "context": context, "gate_verdict": gate_verdict, "action": action})
        return {"answers": {"wake": {"choice": "wake"}}}

    monkeypatch.setattr(wake, "jev_decide", fake_decide)
    return seen


def test_stale_wake_logs_a_shadow_row_with_context(rows):
    conn = board()
    card(conn, "t_shadow", "todo", 1.5)
    rec = Recorder()
    wake.sweep(conn, {}, rec, NOW)
    assert any("t_shadow" in text for text, _ in rec.calls)
    assert len(rows) == 1
    row = rows[0]
    assert row["caller"] == "stale_wake"
    assert row["context"]["task_id"] == "t_shadow"
    assert row["action"] == "wake:injected"
    assert row["gate_verdict"] == "wake"


def test_failed_injection_logs_the_failed_action(rows):
    conn = board()
    card(conn, "t_fail", "todo", 2)
    rec = Recorder(ok=False)
    wake.sweep(conn, {}, rec, NOW)
    assert rows[0]["action"] == "wake:inject-failed"


def test_card_without_subscribers_logs_no_subscribers(rows):
    conn = board()
    card(conn, "t_alone", "todo", 2, subscribed=False)
    rec = Recorder()
    wake.sweep(conn, {}, rec, NOW)
    assert rec.calls == []
    assert rows[0]["action"] == "wake:no-subscribers"


def test_jev_failure_does_not_suppress_the_wake(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("router down")

    monkeypatch.setattr(wake, "jev_decide", boom)
    conn = board()
    card(conn, "t_down", "todo", 1.5)
    rec = Recorder()
    wake.sweep(conn, {}, rec, NOW)
    assert any("t_down" in text for text, _ in rec.calls)
