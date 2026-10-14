"""Shadow / enforce semantics of the Jev layer in ``completion_gate``.

``jev_decide`` is mocked; the network is never touched.
"""

import sys

import pytest

guards = sys.modules["agent_guards"]
gate = guards.completion_gate

BODY = "Goal\n\n## Acceptance\n1. Suites pass.\n2. PR merged.\n3. DES verified.\n"

DECISION_DONE = {
    "answers": {"completion": {"type": "choice", "choice": "done", "probabilities": {"done": 1}, "confidence": 1}}
}
DECISION_PARTIAL = {
    "answers": {"completion": {"type": "choice", "choice": "partial", "probabilities": {"partial": 1}, "confidence": 1}}
}
DECISION_FAILED = {
    "answers": {"completion": {"type": "choice", "choice": "failed", "probabilities": {"failed": 0.9}, "confidence": 1}}
}
DECISION_LOW = {
    "answers": {"completion": {"type": "choice", "choice": "partial", "probabilities": {"partial": 0.5}, "confidence": 1}}
}


@pytest.fixture(autouse=True)
def worker_process(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")


# Structural layer satisfied: every item evidenced, so Jev is the layer under test.
FULL = {"acceptance": {"1": "all suites pass", "2": "PR #9 merged", "3": "DES 200 verified"}}


def check(monkeypatch, metadata=None, body=BODY, decision=DECISION_DONE, settings=("shadow", 0.85)):
    monkeypatch.setattr(gate, "jev_settings", lambda: settings)
    monkeypatch.setattr(gate, "jev_decide", lambda *a, **k: decision)
    return gate.check(
        "kanban_complete", {"summary": "s", "metadata": FULL if metadata is None else metadata},
        load_task=lambda _: (body, False),
    )


def test_shadow_never_blocks_even_when_jev_says_partial(monkeypatch):
    assert check(monkeypatch, decision=DECISION_PARTIAL, settings=("shadow", 0.85)) is None


def test_enforce_blocks_partial_above_threshold(monkeypatch):
    message = check(monkeypatch, decision=DECISION_PARTIAL, settings=("enforce", 0.85))
    assert message and "Jev" in message and "partial" in message
    assert "retry the lifecycle call in this same response" in message


def test_enforce_blocks_failed(monkeypatch):
    message = check(monkeypatch, decision=DECISION_FAILED, settings=("enforce", 0.85))
    assert message and "failed" in message


def test_enforce_passes_done(monkeypatch):
    assert check(monkeypatch, decision=DECISION_DONE, settings=("enforce", 0.85)) is None


def test_enforce_passes_partial_below_threshold(monkeypatch):
    assert check(monkeypatch, decision=DECISION_LOW, settings=("enforce", 0.85)) is None


def test_enforce_blocks_partial_at_custom_lower_threshold(monkeypatch):
    message = check(monkeypatch, decision=DECISION_LOW, settings=("enforce", 0.45))
    assert message and "Jev" in message


def test_enforce_fails_open_when_jev_returns_none(monkeypatch):
    assert check(monkeypatch, decision=None, settings=("enforce", 0.85)) is None


def test_enforce_fails_open_on_malformed_decision(monkeypatch):
    assert check(monkeypatch, decision={"answers": {}}, settings=("enforce", 0.85)) is None


def test_jev_layer_runs_after_the_structural_layers(monkeypatch):
    # empty evidence map blocks with the structural message, not Jev
    message = check(monkeypatch, metadata={}, decision=DECISION_PARTIAL, settings=("enforce", 0.85))
    assert "Acceptance list" in message


def test_structure_met_plus_jev_partial_blocks_in_enforce(monkeypatch):
    # acceptance map fully evidenced, yet the handoff text reads as pending work
    message = check(monkeypatch, decision=DECISION_PARTIAL, settings=("enforce", 0.85))
    assert message and "Jev" in message


def test_reviewer_completions_never_reach_jev(monkeypatch):
    calls = []
    monkeypatch.setattr(gate, "jev_decide", lambda *a, **k: calls.append(a) or DECISION_PARTIAL)
    monkeypatch.setattr(gate, "jev_settings", lambda: ("enforce", 0.85))
    body = "Goal\n\n## Acceptance\n1. Review verdict recorded.\n"
    message = gate.check(
        "kanban_complete", {"summary": "s", "metadata": {"review_verdict": "APPROVE"}},
        load_task=lambda _: (body, False),
    )
    assert message is None and not calls
