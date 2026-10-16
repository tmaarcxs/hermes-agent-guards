import json
import sqlite3

import sys

from agent_guards import board_watch

CLOCK = 1_000_000.0


def board():
    conn = sqlite3.connect(":memory:")
    conn.execute("create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer)")
    return conn


def add_event(conn, kind, task_id="t_x", n=1):
    last = 0
    for _ in range(n):
        last = conn.execute(
            "insert into task_events (task_id, kind, payload) values (?, ?, null)", (task_id, kind)
        ).lastrowid
    conn.commit()
    return last


def test_last_seen_event_id_scopes_to_kinds():
    conn = board()
    assert board_watch.last_seen_event_id(conn) == 0
    add_event(conn, "heartbeat")
    last_comment = add_event(conn, "changes_requested")
    assert board_watch.last_seen_event_id(conn) == last_comment
    assert board_watch.last_seen_event_id(conn, kinds=("changes_requested",)) == last_comment
    assert board_watch.last_seen_event_id(conn, kinds=("status",)) == 0
    assert board_watch.last_seen_event_id(conn, exclude=("heartbeat",)) == last_comment


def test_state_round_trip(tmp_path):
    (tmp_path / "sample.json").write_text("{not json")  # corrupt file reads as empty
    assert board_watch.read_json(tmp_path / "sample.json") == {}
    monkey = json.dumps({"t_a": 10})
    target = tmp_path / "state.json"
    target.write_text(monkey)
    assert board_watch.read_json(target) == {"t_a": 10}
    import pathlib, pytest
    expected = pathlib.Path
    assert monkey == target.read_text()


def test_poke_and_poked_since(tmp_path, monkeypatch):
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    assert not board_watch.poked_since(0, CLOCK)
    board_watch.poke()
    at = json.loads((tmp_path / board_watch.POKE_FILE).read_text())["at"]
    assert board_watch.poked_since(at - 1, at)
    assert board_watch.poked_since(at + 1, at + 5) is False
    assert board_watch.poked_since(0, at) is False  # moment 0 never counts


def test_poke_post_tool_call_filters_reads_and_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    poke_path = tmp_path / board_watch.POKE_FILE
    assert not poke_path.exists()
    board_watch.poke_post_tool_call(tool_name="kanban_show")
    board_watch.poke_post_tool_call(tool_name="web_search", args={})
    board_watch.poke_post_tool_call(tool_name="kanban_comment", status="error")
    board_watch.poke_post_tool_call(tool_name="kanban_comment", status="failed")
    assert not poke_path.exists()
    board_watch.poke_post_tool_call(tool_name="kanban_comment")
    board_watch.poke_post_tool_call(tool_name="kanban_create", args={})
    board_watch.poke_post_tool_call(tool_name="kanban_block", status=None)
    assert poke_path.exists()


def test_tick_skips_a_quiet_board_and_sweeps_on_activity(tmp_path, monkeypatch):
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn = board()
    runs = []
    board_watch.tick(conn, "wake-a.json", lambda state: runs.append(state) or {"t": 1}, kinds=("changes_requested",), now=CLOCK)
    board_watch.tick(conn, "wake-a.json", lambda state: runs.append(state) or {"t": 1}, kinds=("changes_requested",), now=CLOCK)
    assert runs == [{}]  # no cursor yet: the first tick sweeps once, then the cursor quiets it
    assert json.loads((tmp_path / "wake-a.json").read_text()) == {"t": 1}
    add_event(conn, "heartbeat")
    board_watch.tick(conn, "wake-a.json", lambda state: runs.append(state) or {"t": 1}, kinds=("changes_requested",), now=CLOCK)
    assert len(runs) == 1  # irrelevant kind advances nothing
    add_event(conn, "changes_requested")
    board_watch.tick(conn, "wake-a.json", lambda state: runs.append(state) or {"t": 1}, kinds=("changes_requested",), now=CLOCK)
    assert len(runs) == 2  # one sweep with the empty state
    assert json.loads((tmp_path / "wake-a.json").read_text()) == {"t": 1}
    assert json.loads((tmp_path / "wake-a-cursor.json").read_text())["event_id"] == 2
    board_watch.tick(conn, "wake-a.json", lambda state: runs.append(state) or {"t": 1}, kinds=("changes_requested",), now=CLOCK)
    assert len(runs) == 2  # cursor current: no second sweep


def test_tick_sweeps_when_a_poke_lands_after_the_last_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn = board()
    runs = []
    sweep = lambda state: runs.append(state) or {}
    add_event(conn, "changes_requested")
    board_watch.tick(conn, "wake-b.json", sweep, kinds=("changes_requested",), now=CLOCK - 100)
    assert len(runs) == 1
    board_watch.tick(conn, "wake-b.json", sweep, kinds=("changes_requested",), now=CLOCK - 100)
    assert len(runs) == 1
    board_watch.write_state(board_watch.POKE_FILE, {"at": CLOCK - 50})  # a mutation landed after the sweep
    board_watch.tick(conn, "wake-b.json", sweep, kinds=("changes_requested",), now=CLOCK)
    assert len(runs) == 2
    board_watch.tick(conn, "wake-b.json", sweep, kinds=("changes_requested",), now=CLOCK)
    assert len(runs) == 2  # cursor now records swept_at CLOCK, past the poke: quiet again


def test_tick_persists_sweep_output_and_retries_after_crash_between_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn = board()
    add_event(conn, "changes_requested")
    boom = lambda state: 1 / 0
    board_watch.tick(conn, "wake-c.json", boom, kinds=("changes_requested",), now=CLOCK)
    assert json.loads((tmp_path / "wake-c-cursor.json").read_text())["event_id"] == 1
    assert not (tmp_path / "wake-c.json").exists()
    (tmp_path / "wake-c.json").write_text(json.dumps({"t": 7}))
    (tmp_path / "wake-c-cursor.json").write_text(json.dumps({"event_id": 0, "swept_at": CLOCK}))
    runs = []
    board_watch.write_state(board_watch.POKE_FILE, {"at": CLOCK - 5})
    board_watch.tick(conn, "wake-c.json", lambda state: runs.append(state) or {"t": 8}, now=CLOCK)
    assert runs == [{"t": 7}]
    assert json.loads((tmp_path / "wake-c.json").read_text()) == {"t": 8}


def test_sweep_receiving_none_leaves_the_state_file_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn = board()
    add_event(conn, "changes_requested")
    (tmp_path / "wake-d.json").write_text(json.dumps({"keep": 1}))
    board_watch.tick(conn, "wake-d.json", lambda state: None, now=CLOCK)
    assert json.loads((tmp_path / "wake-d.json").read_text()) == {"keep": 1}
    assert json.loads((tmp_path / "wake-d-cursor.json").read_text())["event_id"] == 1


def test_empty_board_never_regresses_a_nonzero_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    (tmp_path / "wake-e-cursor.json").write_text(json.dumps({"event_id": 41, "swept_at": CLOCK}))
    conn = board()
    board_watch.tick(conn, "wake-e.json", lambda state: {}, now=CLOCK)
    assert json.loads((tmp_path / "wake-e-cursor.json").read_text())["event_id"] == 41


def test_due_tracked_tick_sweeps_when_the_stored_due_time_arrives(tmp_path, monkeypatch):
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn = board()
    runs = []
    DUE = CLOCK + 100.0

    def sweep(state):  # a wake aging toward DUE
        runs.append(state)
        return {"t": 1}, DUE

    board_watch.tick(conn, "wake-f.json", sweep, now=CLOCK, due_tracked=True)
    assert runs == [{}]
    cursor = json.loads((tmp_path / "wake-f-cursor.json").read_text())
    assert cursor["due_at"] == DUE
    board_watch.tick(conn, "wake-f.json", sweep, now=CLOCK + 99)  # quiet, before due: skipped
    assert len(runs) == 1
    board_watch.tick(conn, "wake-f.json", lambda state: runs.append(state) or (state, None), now=CLOCK + 100, due_tracked=True)
    assert len(runs) == 2  # exactly at due: swept; nothing ripens afterwards
    assert json.loads((tmp_path / "wake-f-cursor.json").read_text())["due_at"] is None
    board_watch.tick(conn, "wake-f.json", lambda state: runs.append(state) or (state, None), now=CLOCK + 10_000, due_tracked=True)
    assert len(runs) == 2  # a stored None never triggers again


def test_a_failed_due_sweep_keeps_its_due_time_for_the_next_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn = board()
    runs, DUE = [], CLOCK + 100.0

    def flaky(state):
        runs.append(state)
        if len(runs) == 1:
            return {"t": 1}, DUE
        raise RuntimeError("dispatcher blip")

    board_watch.tick(conn, "wake-j.json", flaky, now=CLOCK, due_tracked=True)
    board_watch.tick(conn, "wake-j.json", flaky, now=CLOCK + 100, due_tracked=True)  # raises
    assert len(runs) == 2
    assert json.loads((tmp_path / "wake-j-cursor.json").read_text())["due_at"] == DUE  # kept for retry
    board_watch.tick(conn, "wake-j.json", flaky, now=CLOCK + 101, due_tracked=True)  # retries...
    assert len(runs) == 3  # ...itself also fails, cursor honest again, retry stays armed
    assert json.loads((tmp_path / "wake-j-cursor.json").read_text())["due_at"] == DUE
    board_watch.tick(conn, "wake-j.json", flaky, now=CLOCK + 102, due_tracked=True)
    assert len(runs) == 4  # until a sweep finally completes


def test_due_tracked_cursor_without_the_key_sweeps_once_to_learn_it(tmp_path, monkeypatch):
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn = board()
    (tmp_path / "wake-g-cursor.json").write_text(json.dumps({"event_id": 0, "swept_at": CLOCK}))
    runs = []
    board_watch.tick(conn, "wake-g.json", lambda state: runs.append(state) or (state, None), now=CLOCK + 1, due_tracked=True)
    assert len(runs) == 1  # one upgrade sweep, then the cursor knows due_at
    board_watch.tick(conn, "wake-g.json", lambda state: runs.append(state) or (state, None), now=CLOCK + 2, due_tracked=True)
    assert len(runs) == 1


def test_plain_tick_stores_no_due_at_and_never_sweeps_on_time_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn = board()
    runs = []
    board_watch.tick(conn, "wake-h.json", lambda state: runs.append(state) or ({"t": 1}, None), now=CLOCK)
    assert len(runs) == 1
    assert "due_at" not in json.loads((tmp_path / "wake-h-cursor.json").read_text())
    board_watch.tick(conn, "wake-h.json", lambda state: runs.append(state) or {"t": 2}, now=CLOCK + 999_999)
    assert len(runs) == 1  # a count-based wake ignores the clock entirely


def test_wake_state_writes_apply_to_the_tuple_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn = board()
    add_event(conn, "changes_requested")
    board_watch.tick(conn, "wake-i.json", lambda state: ({"t": 5}, None), now=CLOCK, due_tracked=True)
    assert json.loads((tmp_path / "wake-i.json").read_text()) == {"t": 5}
    assert json.loads((tmp_path / "wake-i-cursor.json").read_text())["due_at"] is None
