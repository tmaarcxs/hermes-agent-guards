import json
import sys

import pytest

guards = sys.modules["agent_guards"]
gate = guards.review_gate

EIGHT = (
    "Contract coverage", "Failure lifecycle", "Model evidence", "Claims within evidence", "Real boundary", "Stale siblings",
    "Self reproduction", "Claims anchored",
)
ALL_DONE = [{"id": str(i), "content": f"{name}: receipt", "status": "completed"} for i, name in enumerate(EIGHT)]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    gate.reset()
    yield
    gate.reset()


def todo(items, session="s1"):
    guards._post_tool_call(tool_name="todo_list", args={"todos": items}, result=json.dumps({"todos": items}),
                           status="ok", session_id=session)


def review(session="s1"):
    return guards._pre_tool_call(tool_name="kanban_request_review", args={"summary": "s"}, session_id=session)


def teardown_with_evidence():
    """Completed stale-siblings item backed by a sweep receipt path."""
    return [item for item in ALL_DONE if item["content"].split(":")[0] != "Stale siblings"] + [
        {"id": "99", "content": "Stale siblings: grep found 0 old references (receipt: artifacts/stale.log)", "status": "completed"},
    ]


def test_blocks_review_without_any_todo_list():
    result = review()
    assert result["action"] == "block"
    for name in gate.CHECKS:
        assert name in result["message"]


def test_allows_review_when_all_checks_completed():
    todo(ALL_DONE)
    assert review() is None


def test_completed_check_without_receipt_evidence_blocks():
    items = [dict(item) for item in ALL_DONE]
    items[1] = {"id": "1", "content": "Failure lifecycle", "status": "completed"}
    todo(items)
    assert "Failure lifecycle" in review()["message"]


def test_completed_stale_siblings_without_receipt_blocks():
    items = [dict(item) for item in ALL_DONE]
    items[5] = {"id": "5", "content": "Stale siblings", "status": "completed"}
    todo(items)
    assert "Stale siblings" in review()["message"]


def test_cancelled_check_needs_na_reason():
    items = [dict(item) for item in ALL_DONE]
    items[2] = {"id": "2", "content": "Model evidence", "status": "cancelled"}
    todo(items)
    assert "Model evidence" in review()["message"]
    items[2]["content"] = "Model evidence N/A: no model behavior changed"
    todo(items)
    assert review() is None


@pytest.mark.parametrize("status", ["pending", "in_progress"])
def test_open_check_blocks(status):
    items = [dict(item) for item in ALL_DONE]
    items[-1]["status"] = status
    todo(items)
    message = review()["message"]
    assert "Claims anchored" in message and "Self reproduction" not in message


def test_matching_is_case_insensitive_and_session_scoped():
    todo([{**item, "content": item["content"].upper()} for item in ALL_DONE], session="s1")
    assert review("s1") is None
    assert review("s2")["action"] == "block"


def test_latest_list_wins():
    todo(ALL_DONE)
    todo(ALL_DONE[:-1])
    assert "Claims anchored" in review()["message"]


def test_other_tools_and_unparseable_results_pass():
    assert guards._pre_tool_call(tool_name="kanban_complete", args={"summary": "s"}, session_id="s1") is None
    guards._post_tool_call(tool_name="todo_list", args={}, result="not json", status="ok", session_id="s1")
    todo(ALL_DONE)
    guards._post_tool_call(tool_name="todo_list", args={}, result="not json", status="ok", session_id="s1")
    assert review() is None


def test_session_cache_is_bounded():
    for i in range(gate._MAX_SESSIONS + 5):
        todo(ALL_DONE, session=f"s{i}")
    assert len(gate._todos) == gate._MAX_SESSIONS
    assert review("s0")["action"] == "block"
    assert review(f"s{gate._MAX_SESSIONS + 4}") is None


def test_recently_updated_session_is_kept():
    todo(ALL_DONE, session="active")
    for i in range(gate._MAX_SESSIONS - 1):
        todo(ALL_DONE, session=f"s{i}")
    todo(ALL_DONE, session="active")
    todo(ALL_DONE, session="newest")
    assert review("active") is None


def test_inactive_without_worker_setup(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert review() is None
