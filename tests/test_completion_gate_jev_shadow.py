"""Shadow-only Jev ask in the completion gate.

The gate's own verdict and message are unchanged whatever Jev answers — the ask
only appends an auditable decision row that names the card, the session, the
gate's own verdict and the action actually taken, so a later outcome (post-completion
state of the card) can be joined to the decision for calibration.
"""

import sys

import pytest

gate = sys.modules["agent_guards"].completion_gate

from test_completion_gate import BODY, check


@pytest.fixture(autouse=True)
def worker_process(monkeypatch):
    monkeypatch.setattr(gate.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")


@pytest.fixture()
def rows(monkeypatch):
    seen = []

    def fake_decide(state, questions, timeout=2.0, caller="", context=None, gate_verdict=None, action=None):
        seen.append({"state": state, "caller": caller, "context": context, "gate_verdict": gate_verdict, "action": action})
        return {"answers": {"completion": {"choice": "done"}}}

    monkeypatch.setattr(gate, "jev_decide", fake_decide)
    return seen


def test_passing_completion_asks_shadow_and_stays_allowed(rows):
    full = check({"acceptance": {"1": "3416 passed", "2": "PR #9 merged abc", "3": "staging 200"}})
    assert full is None
    assert len(rows) == 1
    assert rows[0]["caller"] == "completion_gate"
    assert rows[0]["action"] == "ask:pass-path"
    assert rows[0]["context"]["task_id"] == "t_1"
    assert "1. Suites pass." in rows[0]["state"]


def test_blocked_completion_still_asks_and_records_the_action(rows):
    message = check({})
    assert message and "1, 2, 3" in message
    assert rows[0]["action"] == "blocked:missing-evidence-1,2,3"
    assert rows[0]["context"]["task_id"] == "t_1"


def test_unmet_evidence_records_its_action(rows):
    evidence = "NOT YET - deploy runs later"
    message = check({"acceptance": {"1": "ok", "2": "ok", "3": evidence}})
    assert message and "3" in message
    assert rows[0]["action"] == "blocked:unmet-evidence-3"


def test_reviewer_approval_records_its_action(rows):
    message = check({"review_verdict": "APPROVE", "acceptance": {"1": "a", "2": "b", "3": "c"}})
    assert message is None
    assert rows[0]["action"] == "allowed:reviewer-approve"


def test_jev_failure_never_blocks(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("router down")

    monkeypatch.setattr(gate, "jev_decide", boom)
    assert check({"acceptance": {"1": "x", "2": "y", "3": "z"}}) is None
    assert check({}) and "1, 2, 3" in check({})
