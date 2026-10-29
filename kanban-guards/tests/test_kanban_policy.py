import sys

import pytest

guard = sys.modules["kanban_guards"]
policy = guard.kanban_policy


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setattr(policy, "configured_worker", lambda: "worker")


VALID = {"title": "Review X", "assignee": "worker", "body": "Goal: spec\n\n## Acceptance\n1. `pytest -q` exits 0\n2. PR open",
         "parents": ["t_1"]}


def blocked(tool, args):
    result = guard._pre_tool_call(tool_name=tool, args=args, task_id="", session_id="")
    return None if result and result.get("action") == "modify" else result


def test_valid_card_passes():
    assert blocked("kanban_create", VALID) is None


def test_default_assignee_blocked():
    result = blocked("kanban_create", {**VALID, "assignee": "default"})
    assert result["action"] == "block" and "'default'" in result["message"]


def test_missing_assignee_blocked():
    assert blocked("kanban_create", {"title": "x"})["action"] == "block"


def test_triage_blocked():
    assert "triage" in blocked("kanban_create", {**VALID, "triage": True})["message"]


def test_triage_false_passes():
    assert blocked("kanban_create", {**VALID, "triage": False}) is None


def test_each_execution_override_blocked():
    for key in ("model", "provider", "goal_mode", "goal_max_turns", "max_runtime_seconds"):
        assert key in blocked("kanban_create", {**VALID, key: None})["message"], key


def test_all_violations_reported_together():
    message = blocked("kanban_create", {**VALID, "assignee": "default", "model": "x", "triage": True})["message"]
    assert "'default'" in message and "triage" in message and "'model'" in message


def test_review_reviewer_not_gated_by_this_plugin():
    """The reviewer name is the native tool's and the dispatcher's business, not ours.

    History: this gate used to force ``reviewer`` to the configured worker because the
    setup had no reviewer profile. Once a real ``reviewer`` profile existed and was on
    ``kanban.dispatch_profiles``, the gate blocked a legitimate handoff. It is removed.

    Asserted at the policy layer only: the full ``_pre_tool_call`` chain also runs
    ``review_gate``, which legitimately blocks this stub for lacking pre-review checks,
    so routing through it would test that gate instead of this one.
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
    assert "'builder'" in blocked("kanban_create", VALID)["message"]


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
