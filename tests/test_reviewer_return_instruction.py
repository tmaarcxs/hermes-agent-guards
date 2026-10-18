"""The reviewer instruction when post-review steps remain must not emit a rejection event.

A reviewer approval on a card whose Acceptance still names post-review steps (merge,
deploy, close) must hand the card back through ``kanban_request_review`` with the
implementer as reviewer — the gateway renders that as a ready-for-review ping (👀) and
re-spawns the implementer. The old instruction routed the approval through
``kanban_request_changes``, which core records as a ``changes_requested`` event and the
gateway delivers as "review requested changes/BLOCK; implementation is not approved" —
an approval the lead reads as a rejection (study B §3.1, 17 wakes/week).

The safety property is unchanged: the approval still never completes the card, and the
post-review steps remain gated on a later evidenced completion from the implementer's
own run.
"""

import sys

import pytest

guards = sys.modules["agent_guards"]
gate = guards.completion_gate

POST_REVIEW_BODY = (
    "Goal\n\n## Acceptance\n1. Suites pass.\n2. PR #1066 merged.\n3. Staging deployed and verified.\n4. Tracker issue #1069 closed.\n"
)
PURE_REVIEW_BODY = (
    "Goal: independent review.\n\n## Acceptance\n1. Review verdict recorded with findings.\n2. Findings converge with prior rounds.\n"
)


@pytest.fixture(autouse=True)
def worker_process(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")


def reviewer_check(metadata, body=POST_REVIEW_BODY, review=True):
    return gate.check("kanban_complete", {"summary": "s", "metadata": metadata}, load_task=lambda _: (body, review))


def test_reviewer_approval_instruction_names_request_review_not_request_changes():
    message = reviewer_check({"review_verdict": "APPROVE"})
    assert "kanban_request_review" in message
    assert "kanban_request_changes" not in message


def test_reviewer_approval_instruction_names_the_implementer_provenance():
    message = reviewer_check({})
    assert "implementer" in message


def test_reviewer_approval_instruction_keeps_the_verdict_recorded():
    message = reviewer_check({})
    assert "comment" in message


def test_reviewer_approval_instruction_still_forbids_completion():
    message = reviewer_check({})
    assert "kanban_complete" in message


@pytest.mark.parametrize("body,expected", [
    ("## Acceptance\n1. Verdict recorded.\n2. PR needs merge before release.", "2"),
    ("## Acceptance\n1. Verdict recorded.\n2. Deploy to production after approval.", "2"),
    ("## Acceptance\n1. Verdict recorded.\n2. Close tracker issue #1069.", "2"),
])
def test_each_post_review_keyword_still_blocks_with_the_new_instruction(body, expected):
    message = reviewer_check({}, body=body)
    assert message and expected in message
    assert "kanban_request_review" in message


def test_lead_visible_event_of_an_approval_is_not_a_changes_requested_rejection():
    """The instruction's authorized path emits a review_requested event, core-verified."""
    message = reviewer_check({"review_verdict": "APPROVE"})
    authorized = "kanban_request_review" in message and "kanban_request_changes" not in message
    assert authorized


def test_card_cannot_reach_done_before_the_steps_are_done():
    message = reviewer_check({})
    assert message, "a reviewer completion with pending post-review steps must stay blocked"


def test_real_rejection_instruction_is_untouched():
    from test_retry_instruction import assert_retry_instruction

    message = reviewer_check({})
    assert_retry_instruction(message)


def test_reviewer_approval_on_pure_review_card_still_passes():
    assert reviewer_check({"review_verdict": "APPROVE"}, body=PURE_REVIEW_BODY) is None
    assert reviewer_check({}, body=PURE_REVIEW_BODY, review=True) is None
