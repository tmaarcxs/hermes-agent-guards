import sys

import pytest

guards = sys.modules["agent_guards"]
gate = guards.completion_gate

BODY = "Goal\n\n## Acceptance\n1. Suites pass.\n2. PR merged.\n3. Staging verified.\n\nSibling: other card."


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
    assert check({"acceptance": {"1": "3416 passed", 2: "PR #9 merged abc", "3": "staging app 200"}}) is None


@pytest.mark.parametrize("evidence", [
    "PR MERGED? NOT YET - PR #1059 is OPEN with all gates green",
    "ISSUE CLOSURE PENDING MERGE: #1057 remains OPEN by design",
    "Acceptance not met: no staging run",
])
def test_evidence_admitting_unmet_item_is_blocked(evidence):
    message = check({"acceptance": {"1": "3416 passed", "2": evidence, "3": "staging app 200"}})
    assert message and "items 2 " in message


def test_evidence_mentioning_done_states_passes():
    assert check({"acceptance": {"1": "flake fixed, was pending before", "2": "PR #9 merged abc", "3": "staging app 200 open"}}) is None


def test_card_without_acceptance_list_passes():
    assert check({}, body="Just do it") is None


def test_review_runs_and_verdicts_are_exempt():  # legacy name kept: proves pure-review cards complete
    assert check({}, review=True, body=PURE_REVIEW_BODY) is None
    assert check({"review_verdict": "APPROVE"}, body=PURE_REVIEW_BODY) is None


POST_REVIEW_BODY = (
    "Goal\n\n## Acceptance\n1. Suites pass.\n2. PR #1066 merged.\n3. Staging deployed and verified.\n4. Tracker issue #1069 closed.\n"
)
PURE_REVIEW_BODY = (
    "Goal: independent review.\n\n## Acceptance\n1. Review verdict recorded with findings.\n2. Findings converge with prior rounds.\n"
)


def reviewer_check(metadata, body=POST_REVIEW_BODY, review=True):
    return gate.check("kanban_complete", {"summary": "s", "metadata": metadata}, load_task=lambda _: (body, review))


def test_reviewer_approval_blocked_on_pending_post_review_items():
    message = reviewer_check({"review_verdict": "APPROVE"})
    assert message and "2, 3, 4" in message


def test_reviewer_run_claimed_from_review_blocked_on_pending_post_review_items():
    message = reviewer_check({})
    assert message and "2, 3, 4" in message


@pytest.mark.parametrize("body,expected", [
    ("## Acceptance\n1. Verdict recorded.\n2. PR needs merge before release.", "2"),
    ("## Acceptance\n1. Verdict recorded.\n2. Deploy to production after approval.", "2"),
    ("## Acceptance\n1. Verdict recorded.\n2. Smoke verification on staging after deploy.", "2"),
    ("## Acceptance\n1. Verdict recorded.\n2. Close tracker issue #1069.", "2"),
])
def test_each_post_review_keyword_triggers_reviewer_block(body, expected):
    message = reviewer_check({}, body=body)
    assert message and expected in message and "kanban_request_changes" in message


def test_reviewer_approval_with_post_review_evidence_passes():
    assert reviewer_check({
        "review_verdict": "APPROVE",
        "acceptance": {"1": "118 passed", "2": "PR #1066 merged abc1234", "3": "staging app 200", "4": "#1069 closed"},
    }) is None


def test_reviewer_approval_on_pure_review_card_passes():
    assert reviewer_check({"review_verdict": "APPROVE"}, body=PURE_REVIEW_BODY) is None
    assert reviewer_check({}, body=PURE_REVIEW_BODY, review=True) is None


def test_reviewer_block_directs_to_request_changes_with_retry_instruction():
    message = reviewer_check({})
    assert "APPROVED" in message and "implementer" in message
    from test_retry_instruction import assert_retry_instruction
    assert_retry_instruction(message)


def test_reviewer_unmet_evidence_for_post_review_item_is_blocked():
    message = reviewer_check({
        "review_verdict": "APPROVE",
        "acceptance": {"2": "merge pending review", "3": "staging app 200", "4": "#1069 closed"},
    })
    assert message and "items 2 " in message and "not met yet" in message
    assert "kanban_request_changes" not in message


def test_lead_process_is_not_gated(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert check({}) is None


def test_items_are_parsed_from_the_acceptance_section_only():
    body = "Plan\n1. read\n2. code\n\n## Acceptance\n1. a\n2. b\n\n## Notes\n1. ignore"
    assert gate.acceptance_items(body) == ["1", "2"]


def test_string_metadata_json_is_parsed():
    import json
    message = check(json.dumps({"acceptance": {}}.get("acceptance") and {} or {"acceptance": {}}))
    assert message and "1, 2, 3" in message


def test_string_metadata_with_full_map_passes_and_reviewer_block_fires():
    import json
    empty_json = json.dumps({"acceptance": {}})
    assert "1, 2, 3" in check(empty_json)
    reviewer_json = json.dumps({"review_verdict": "APPROVE"})
    message = gate.check("kanban_complete", {"summary": "s", "metadata": reviewer_json},
                         load_task=lambda _: (POST_REVIEW_BODY, False))
    assert message and "2, 3, 4" in message and "kanban_request_changes" in message


def test_string_metadata_unparsable_treated_as_empty():
    message = check("{not json")
    assert message and "1, 2, 3" in message
