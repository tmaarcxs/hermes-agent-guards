import sys

import pytest

guard = sys.modules["kanban_guards"]
policy = guard.kanban_policy


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setattr(policy, "configured_worker", lambda: "worker")


VALID = {"title": "Review X", "assignee": "worker", "body": "Goal: spec\n\n## Acceptance\n1. `pytest -q` exits 0\n2. PR open",
         "parents": ["t_1"]}


def rewritten(tool, args):
    return guard._pre_tool_call(tool_name=tool, args=args, task_id="", session_id="")


def blocked(tool, args):
    result = guard._pre_tool_call(tool_name=tool, args=args, task_id="", session_id="")
    return None if result and result.get("action") == "modify" else result


def test_valid_card_passes():
    assert blocked("kanban_create", VALID) is None


def test_default_assignee_rewritten_to_worker():
    assert rewritten("kanban_create", {**VALID, "assignee": "default"}) == {"action": "modify", "args": {"assignee": "worker"}}


def test_missing_assignee_rewritten_to_worker():
    assert rewritten("kanban_create", {"title": "x", "body": VALID["body"]}) == {"action": "modify", "args": {"assignee": "worker"}}


def test_triage_blocked():
    assert "triage" in blocked("kanban_create", {**VALID, "triage": True})["message"]


def test_triage_false_passes():
    assert blocked("kanban_create", {**VALID, "triage": False}) is None


def test_each_execution_override_removed():
    for key in ("model", "provider", "goal_mode", "goal_max_turns", "max_runtime_seconds"):
        assert rewritten("kanban_create", {**VALID, key: "x"}) == {"action": "modify", "args": {key: None}}, key


def test_unfixable_violation_still_blocks_after_rewrite():
    result = rewritten("kanban_create", {**VALID, "assignee": "default", "model": "x", "triage": True})
    assert result["action"] == "block" and "triage" in result["message"]


def test_review_reviewer_not_gated_by_this_plugin():
    """The reviewer name is the native tool's and the dispatcher's business, not ours.

    History: this gate used to force ``reviewer`` to the configured worker because the
    setup had no reviewer profile. Once a real ``reviewer`` profile existed and was on
    ``kanban.dispatch_profiles``, the gate blocked a legitimate handoff. It is removed.

    Asserted at the policy layer only: the policy layer is tested on its own.
    """
    assert policy.check("kanban_request_review", {"summary": "s"}) is None
    assert policy.check("kanban_request_review", {"summary": "s", "reviewer": "worker"}) is None
    assert policy.check("kanban_request_review", {"summary": "s", "reviewer": "reviewer"}) is None
    assert policy.check("kanban_request_review", {"summary": "s", "reviewer": "default"}) is None
    assert policy.violations("kanban_request_review", {"summary": "s", "reviewer": "reviewer"}, "worker") == []


def test_other_tools_ignored():
    assert blocked("terminal", {"command": "ls", "assignee": "default"}) is None


def test_internal_error_fails_open():
    assert blocked("kanban_create", "not-a-dict") is None


def test_named_worker_comes_from_config(monkeypatch):
    monkeypatch.setattr(policy, "configured_worker", lambda: "builder")
    assert blocked("kanban_create", {**VALID, "assignee": "builder"}) is None
    assert rewritten("kanban_create", VALID) == {"action": "modify", "args": {"assignee": "builder"}}


def test_inactive_without_default_assignee(monkeypatch):
    monkeypatch.setattr(policy, "configured_worker", lambda: "")
    assert blocked("kanban_create", {**VALID, "assignee": "default", "triage": True, "model": "x"}) is None
    assert blocked("kanban_request_review", {"summary": "s", "reviewer": "default"}) is None
    assert blocked("kanban_request_review", {"summary": "s", "reviewer": "reviewer"}) is None


def test_configured_worker_reads_kanban_default_assignee(monkeypatch):
    import types

    monkeypatch.undo()
    fake = types.ModuleType("hermes_cli.config")
    fake.load_config_readonly = lambda: {"kanban": {"default_assignee": " worker "}}
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake)
    assert policy.configured_worker() == "worker"
    fake.load_config_readonly = lambda: {}
    assert policy.configured_worker() == ""


def test_create_numbered_acceptance_still_passes():
    for body in ("Goal\n\nAcceptance:\n1. a\n2. b",
                 "**Acceptance criteria**\n\n1) a",
                 "## Acceptance\n  1. indented"):
        assert blocked("kanban_create", {**VALID, "body": body}) is None


def test_prose_acceptance_body_passes():
    body = (
        "Tighten the gateway timeout\n\n"
        "## Acceptance\n"
        "The client drops stalled connections after five seconds and the "
        "integration suite demonstrates the drop against a stub server."
    )
    assert blocked("kanban_create", {**VALID, "body": body}) is None
    numbered = "## Acceptance\n1. `pytest -q` exits 0\n2. PR open"
    assert blocked("kanban_create", {**VALID, "body": numbered}) is None


def test_single_readback_passes():
    body = "Verify the image carries the merge commit and run one readback check.\n\n## Acceptance\n1. Image tag recorded"
    assert blocked("kanban_create", {**VALID, "body": body}) is None


CODE_BODY = "Implement issue 1.\n\n## Acceptance\n1. Commit one signed commit\n2. Lead owns review and merge"


def test_code_card_without_request_review_blocked():
    result = blocked("kanban_create", {**VALID, "body": CODE_BODY})
    assert result["action"] == "block" and "kanban_request_review" in result["message"]


def test_code_card_with_request_review_passes():
    body = CODE_BODY + "\n3. Hand off with kanban_request_review"
    assert blocked("kanban_create", {**VALID, "body": body}) is None


def test_read_only_review_card_exempt():
    body = "Independent read-only review. You did NOT implement it.\n\n## Acceptance\n1. Verdict report"
    assert blocked("kanban_create", {**VALID, "body": body}) is None


@pytest.mark.parametrize("contract", ["https://github.com/o/r/pull/1", "o/r"])
def test_pr_completion_contract_is_rewritten_to_local_only(contract):
    out = rewritten("kanban_create", {**VALID, "completion_contract": contract})
    assert out == {"action": "modify", "args": {"completion_contract": "local-only"}}


def test_local_only_or_absent_contract_is_left_alone():
    assert rewritten("kanban_create", {**VALID, "completion_contract": "local-only"}) is None
    assert rewritten("kanban_create", VALID) is None
