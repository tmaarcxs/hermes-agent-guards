import json
import os

import pytest

guards = pytest.importorskip("agent_guards")

gate = guards.kanban_show_slim_gate

DROP_LIST = {"heartbeat", "claimed", "spawned", "attached", "respawn_guarded",
             "promoted", "linked", "unlinked", "assigned", "archived"}


def _task(**over):
    task = {"id": "t1", "title": "Card", "body": "Body text",
            "result": "handoff summary text", "status": "done"}
    task.update(over)
    return task


def _show(task=None, events=None, runs=None, extra=None):
    payload = {"task": task or _task(),
               "parents": [{"id": "p1", "summary": "parent handoff text"}],
               "comments": [{"author": "m", "body": "comment body"}],
               "runs": runs if runs is not None else [
                   {"id": 1, "summary": "run summary text", "error": "boom",
                    "metadata": {"acceptance": {"1": "receipt anchor"}}}],
               "events": events if events is not None else [
                   {"kind": "heartbeat", "created_at": 1},
                   {"kind": "claimed", "payload": {"lock": "L"}},
                   {"kind": "review_requested", "payload": {"summary": "verdict text"}},
                   {"kind": "completed", "payload": {"summary": "done text"}},
                   {"kind": "commented", "payload": {"author": "x", "len": 3}}],
               "worker_context": "# Card\n\nAssignee: worker\nStatus: run"}
    if extra:
        payload.update(extra)
    return json.dumps(payload)


@pytest.fixture
def lead(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)


def test_drop_list_matches_the_fixed_lead_decision():
    assert gate.EVENT_KINDS_DROPPED == DROP_LIST


def test_slim_drops_worker_context_and_bookkeeping_events(lead):
    out = json.loads(gate.transform("kanban_show", {}, _show()))
    assert "worker_context" not in out
    kinds = [row["kind"] for row in out["events"]]
    assert kinds == ["review_requested", "completed", "commented"]
    assert out[gate._REMOVED_KEY] == "worker_context"
    assert out[gate._DROPPED_KEY] == 2


def test_kept_text_fields_byte_identical(lead):
    payload = json.loads(_show())
    out = json.loads(gate.transform("kanban_show", {}, _show()))
    for field in ("task", "parents", "comments", "runs"):
        assert json.dumps(out[field]) == json.dumps(payload[field])
    kept_events = [e for e in payload["events"] if e["kind"] not in DROP_LIST]
    assert json.dumps(out["events"]) == json.dumps(kept_events)
    verdict = kept_events[0]["payload"]["summary"]
    assert verdict in json.dumps(out)


def test_dumped_payload_shrinks_marker_only(lead):
    before = len(_show())
    after = len(gate.transform("kanban_show", {}, _show()))
    assert 0 < before - after <= 100


def test_worker_output_unchanged(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t9")
    assert gate.transform("kanban_show", {}, _show()) is None


def test_other_tools_untouched(lead):
    assert gate.transform("kanban_list", {}, _show()) is None
    assert gate.transform("", {}, _show()) is None


def test_unknown_fields_kept(lead):
    out = json.loads(gate.transform("kanban_show", {}, _show(extra={"novel": {"a": 1}})))
    assert out["novel"] == {"a": 1}


def test_malformed_and_non_show_results_pass_through(lead):
    assert gate.transform("kanban_show", {}, "not json") is None
    assert gate.transform("kanban_show", {}, "[1,2]") is None
    assert gate.transform("kanban_show", {}, json.dumps({"plain": 1})) is None
    assert gate.transform("kanban_show", {}, json.dumps({"task": "no dict"})) is None
    assert gate.transform("kanban_show", {}, json.dumps({"task": {}, "other": 2})) is None
    assert gate.transform("kanban_show", {}, 42) is None
    assert gate.transform("kanban_show", {}, None) is None


def test_no_worker_context_no_change(lead):
    payload = json.loads(_show())
    del payload["worker_context"]
    raw = json.dumps(payload)
    assert gate.transform("kanban_show", {}, raw) is None


def test_determinism_same_bytes_twice(lead):
    raw = _show()
    first = gate.transform("kanban_show", {}, raw)
    second = gate.transform("kanban_show", {}, raw)
    assert first == second and first is not None


def test_idempotence_slimmed_result_untouched(lead):
    raw = _show()
    slim = gate.transform("kanban_show", {}, raw)
    assert json.loads(slim)[gate._REMOVED_KEY] == "worker_context"
    assert gate.transform("kanban_show", {}, slim) is None


def test_events_without_list_or_missing_kind_fail_open(lead):
    events = [None, 42, "x", {"payload": {"kind": "heartbeat"}},
              {"kind": 9, "payload": {"a": 1}},
              {"kind": "mystery_kind", "payload": {"note": "keep unknown"}},
              {"kind": "unverified", "payload": {"text": "kept"}}]
    out = json.loads(gate.transform("kanban_show", {}, _show(events=events)))
    assert out[gate._DROPPED_KEY] == 0
    assert len(out["events"]) == len(events)


def test_events_field_absent(lead):
    payload = json.loads(_show())
    del payload["events"]
    out = json.loads(gate.transform("kanban_show", {}, json.dumps(payload)))
    assert out[gate._DROPPED_KEY] == 0
    assert "events" not in out


def test_run_summary_and_error_survive_byte_identical(lead):
    out = json.loads(gate.transform("kanban_show", {}, _show()))
    text = json.dumps(out, ensure_ascii=False)
    assert "run summary text" in text
    assert "boom" in text
    assert out["runs"][0]["metadata"] == {"acceptance": {"1": "receipt anchor"}}


def test_parent_handoff_via_own_show(lead):
    parent_payload = {"task": _task(id="p1"), "runs": [
        {"id": 7, "status": "done",
         "summary": "Parent handoff summary line"}], "events": [],
        "worker_context": "rendered parent card"}
    slim = json.loads(gate.transform("kanban_show", {}, json.dumps(parent_payload)))
    assert slim["runs"][0]["summary"] == "Parent handoff summary line"


def test_chain_kanban_show_slimmed_and_list_untouched(lead):
    show = guards._transform_tool_result(tool_name="kanban_show", args={}, result=_show())
    payload = json.loads(show)
    assert "worker_context" not in payload
    assert payload["runs"][0]["summary"] == "run summary text"
    list_result = json.dumps({"tasks": [{"id": "a"}], "count": 1})
    assert guards._transform_tool_result(
        tool_name="kanban_list", args={}, result=list_result) is None
