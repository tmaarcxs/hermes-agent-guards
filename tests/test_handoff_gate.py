import json
import sys
from pathlib import Path

import pytest

guards = sys.modules["agent_guards"]
gate = guards.handoff_gate

FINISHED = [
    "Signed publication is complete and exact-head gates are green (commit 31439b10, PR #934 open). Nothing left for "
    "this worker to execute: merge of PR #934 plus DES live acceptance are held by the lead per operator note.",
    "Delivery-prep phase complete and durably checkpointed: exact-head 3603ffb3 verified signed + CI-green. Merge of "
    "PR #923 and any DES build/deploy/execution remain with the lead.",
    "F1/F2 corrections implemented and verified on signed commit 180859f0 (2150 unit green). Lead approval needed "
    "before publication.",
]
REAL = [
    "Design decision needed from lead: corrective-instruction coverage remedy. The rejected candidate presupposed "
    "original-output knowledge a fresh run lacks.",
    "Operator (the owner) paused this delivery mid-run with an immediate STOP: no coding, commit, push, or PR until "
    "the pause is lifted.",
    "Session hit its context limit during orientation before any implementation edit.",
    "Acceptance is not met: this run produced no candidate directory, no receipt validator and no E2E results.",
]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    gate.reset()
    yield
    gate.reset()


def block(reason, session="s1"):
    return guards._pre_tool_call(tool_name="kanban_block", args={"reason": reason, "kind": "needs_input"},
                                 session_id=session)


@pytest.mark.parametrize("reason", FINISHED)
def test_bounces_finished_work_waiting_on_lead(reason):
    result = block(reason)
    assert result["action"] == "block"
    assert "kanban_complete" in result["message"]


@pytest.mark.parametrize("reason", REAL)
def test_real_blockers_pass(reason):
    assert block(reason) is None


def test_repeat_in_same_session_passes():
    assert block(FINISHED[0])["action"] == "block"
    assert block(FINISHED[0]) is None


def test_other_session_is_bounced_again():
    block(FINISHED[0], "s1")
    assert block(FINISHED[0], "s2")["action"] == "block"


def test_inactive_without_worker_setup(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert block(FINISHED[0]) is None


def test_matches_recorded_board_corpus():
    corpus = Path.home() / ".hermes/cache/scratch/block_reasons.json"
    if not corpus.exists():
        pytest.skip("local board corpus not present")
    hits = [t for _, t, r in json.loads(corpus.read_text()) if gate.finished_with_lead_remainder(r)]
    assert 8 <= len(hits) <= 20
