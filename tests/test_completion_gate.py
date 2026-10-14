import sys

import pytest

guards = sys.modules["agent_guards"]
gate = guards.completion_gate

BODY = "Goal\n\n## Acceptance\n1. Suites pass.\n2. PR merged.\n3. DES verified.\n\nSibling: other card."


@pytest.fixture(autouse=True)
def worker_process(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")


def check(metadata, body=BODY, review=False):
    return gate.check("kanban_complete", {"summary": "s", "metadata": metadata}, load_task=lambda _: (body, review))


def test_partial_completion_is_blocked():
    message = check({"acceptance": {"2": "integration 325 passed"}})
    assert message and "1, 3" in message


def test_completion_without_acceptance_map_is_blocked():
    assert "1, 2, 3" in check({"changed_files": []})


def test_blank_evidence_does_not_count():
    assert "3" in check({"acceptance": {"1": "ok", "2": "ok", "3": " "}})


def test_full_evidence_passes():
    assert check({"acceptance": {"1": "3416 passed", 2: "PR #9 merged abc", "3": "DES 200"}}) is None


def test_card_without_acceptance_list_passes():
    assert check({}, body="Just do it") is None


def test_review_runs_and_verdicts_are_exempt():
    assert check({}, review=True) is None
    assert check({"review_verdict": "APPROVE"}) is None


def test_lead_process_is_not_gated(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert check({}) is None


def test_items_are_parsed_from_the_acceptance_section_only():
    body = "Plan\n1. read\n2. code\n\n## Acceptance\n1. a\n2. b\n\n## Notes\n1. ignore"
    assert gate.acceptance_items(body) == ["1", "2"]
