import sys

import pytest

guard = sys.modules["agent_guards"]
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


def test_review_reviewer_rules():
    assert policy.check("kanban_request_review", {"summary": "s"}) is None
    assert policy.check("kanban_request_review", {"summary": "s", "reviewer": "worker"}) is None
    assert "reviewer must be 'worker'" in policy.check("kanban_request_review", {"summary": "s", "reviewer": "default"})
    message = blocked("kanban_request_review", {"summary": "s", "reviewer": "default"})["message"]
    assert "reviewer must be 'worker'" in message


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


@pytest.mark.parametrize("body", [
    "spec without acceptance",
    "Acceptance: everything works",
    "## Acceptance\n- tests pass\n- docs updated",
    "## Acceptance criteria\nTests pass and docs updated.",
])
def test_create_requires_numbered_acceptance_list(body):
    assert "Acceptance" in blocked("kanban_create", {**VALID, "body": body})["message"]


@pytest.mark.parametrize("body", [
    "Goal\n\nAcceptance:\n1. a\n2. b",
    "**Acceptance criteria**\n\n1) a",
    "## Acceptance\n  1. indented",
])
def test_create_accepts_numbered_acceptance_variants(body):
    assert blocked("kanban_create", {**VALID, "body": body}) is None


DES_BATCH = [
    "6. **Verify in DES.** Run fresh executions on at least 3 audits, including one with Employee Record findings.",
    "3. Run fresh executions in DES on at least 3 audits and report per-analyzer coverage.",
    "Measure coverage from at least 4 audits in DES after merge.",
]


@pytest.mark.parametrize("step", DES_BATCH)
def test_des_batch_as_analysis_measurement_blocked(step):
    body = f"Goal\n\n{step}\n\n## Acceptance\n1. Coverage table posted"
    result = blocked("kanban_create", {**VALID, "body": body})
    assert result and "replicated DES snapshots" in result["message"]


def test_des_batch_with_local_replay_passes():
    body = ("Measure locally on replicated snapshots of 4 audits with real LUMEN. Then in DES run one fresh execution "
            "for the screenshot, not at least 3 audits.\n\n## Acceptance\n1. Coverage table posted")
    assert blocked("kanban_create", {**VALID, "body": body}) is None


def test_single_des_readback_passes():
    body = "Verify the DES image carries the merge commit and render one execution.\n\n## Acceptance\n1. Image tag recorded"
    assert blocked("kanban_create", {**VALID, "body": body}) is None
