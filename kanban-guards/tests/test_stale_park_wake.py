import importlib.util
import json
import sqlite3
import time
from pathlib import Path

import sys

wake = sys.modules["agent_guards"].wakes

from agent_guards import board_watch

NOW = 1_000_000.0
HOUR = 3600


def board():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        create table tasks (id text, status text, title text, block_kind text, created_at integer);
        create table task_links (parent_id text, child_id text);
        create table task_events (id integer primary key, task_id text, kind text, created_at integer);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text);
        """
    )
    return conn


def card(conn, tid, status, age_h, subscribed=True, kind="created"):
    at = NOW - age_h * HOUR
    conn.execute("insert into tasks values (?, ?, ?, ?, ?)", (tid, status, f"title {tid}", "needs_input", at))
    conn.execute("insert into task_events (task_id, kind, created_at) values (?, ?, ?)", (tid, kind, at))
    if subscribed:
        conn.execute(
            "insert into kanban_notify_subs values (?, 'telegram', '555000100', '900332', 'dm', '555000100', null, 'default')",
            (tid,),
        )


def event(conn, tid, kind, age_h):
    conn.execute("insert into task_events (task_id, kind, created_at) values (?, ?, ?)", (tid, kind, NOW - age_h * HOUR))


class Recorder:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, text, origin):
        self.calls.append((text, origin))
        return self.ok


def woken(rec):
    return sorted(text.split()[3] for text, _ in rec.calls)


def test_wakes_each_stale_card_once_in_its_chat():
    conn = board()
    card(conn, "t_blocked", "blocked", 2)
    card(conn, "t_todo", "todo", 1.5)
    rec = Recorder()
    state = wake.stale_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_blocked", "t_todo"]
    assert rec.calls[0][1] == {
        "platform": "telegram", "chat_id": "555000100", "thread_id": "900332", "chat_type": "dm",
        "user_id": "555000100", "profile": "default",
    }
    rec2 = Recorder()
    assert wake.stale_sweep(conn, state, rec2, NOW + 5 * HOUR) == state
    assert rec2.calls == []


def test_todo_card_waiting_on_an_unfinished_parent_is_never_woken():
    conn = board()
    card(conn, "t_parent", "running", 3)
    card(conn, "t_child", "todo", 3)
    conn.execute("insert into task_links values ('t_parent', 't_child')")
    rec = Recorder()
    wake.stale_sweep(conn, {}, rec, NOW)
    assert rec.calls == []
    assert wake.next_due(conn, NOW) is None


def test_todo_card_with_all_parents_done_or_no_parents_still_wakes():
    conn = board()
    card(conn, "t_done_parent", "done", 3)
    card(conn, "t_child", "todo", 3)
    card(conn, "t_orphan", "todo", 3)
    conn.execute("insert into task_links values ('t_done_parent', 't_child')")
    rec = Recorder()
    wake.stale_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_child", "t_orphan"]


def test_blocked_card_with_an_unfinished_parent_still_wakes():
    conn = board()
    card(conn, "t_parent", "running", 3)
    card(conn, "t_child", "blocked", 3)
    conn.execute("insert into task_links values ('t_parent', 't_child')")
    rec = Recorder()
    wake.stale_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_child"]


def test_running_and_recent_cards_are_never_woken():
    conn = board()
    card(conn, "t_running", "running", 10)
    card(conn, "t_recent", "blocked", 0.05)
    card(conn, "t_done", "done", 10)
    rec = Recorder()
    wake.stale_sweep(conn, {}, rec, NOW)
    assert rec.calls == []


def test_parked_scheduled_card_wakes_its_chat():
    conn = board()
    card(conn, "t_scheduled", "scheduled", 2, kind="scheduled")
    rec = Recorder()
    wake.stale_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_scheduled"]


def test_heartbeats_do_not_count_as_movement_or_reset_the_state():
    conn = board()
    card(conn, "t_b", "blocked", 3)
    event(conn, "t_b", "heartbeat", 0.1)
    rec = Recorder()
    state = wake.stale_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_b"]
    event(conn, "t_b", "heartbeat", 0)
    rec2 = Recorder()
    wake.stale_sweep(conn, state, rec2, NOW)
    assert rec2.calls == []


def test_agent_comment_after_wake_does_not_rearm():
    conn = board()
    card(conn, "t_b", "blocked", 3)
    state = wake.stale_sweep(conn, {}, Recorder(), NOW)
    event(conn, "t_b", "commented", 0)
    event(conn, "t_b", "edited", 0)
    rec = Recorder()
    assert wake.stale_sweep(conn, state, rec, NOW + 2 * HOUR) == state
    assert rec.calls == []


def test_a_real_state_change_rearms_the_wake():
    conn = board()
    card(conn, "t_b", "todo", 3)
    state = wake.stale_sweep(conn, {}, Recorder(), NOW)
    event(conn, "t_b", "claimed", 2.5)
    event(conn, "t_b", "released", 2)
    rec = Recorder()
    state = wake.stale_sweep(conn, state, rec, NOW)
    assert woken(rec) == ["t_b"]


def test_unblock_and_reblock_unseen_by_a_sweep_stays_one_parked_episode():
    conn = board()
    card(conn, "t_b", "blocked", 3)
    state = wake.stale_sweep(conn, {}, Recorder(), NOW)
    event(conn, "t_b", "unblocked", 2.5)
    event(conn, "t_b", "blocked", 2)
    rec = Recorder()
    wake.stale_sweep(conn, state, rec, NOW)
    assert rec.calls == []


def test_card_without_chat_is_skipped_and_failed_injection_retries():
    conn = board()
    card(conn, "t_nochat", "blocked", 3, subscribed=False)
    card(conn, "t_b", "blocked", 3)
    failing = Recorder(ok=False)
    state = wake.stale_sweep(conn, {}, failing, NOW)
    assert woken(failing) == ["t_b"]
    assert state == {}
    rec = Recorder()
    wake.stale_sweep(conn, state, rec, NOW)
    assert woken(rec) == ["t_b"]


def test_cards_that_leave_the_stale_set_are_forgotten():
    conn = board()
    card(conn, "t_b", "blocked", 3)
    state = wake.stale_sweep(conn, {}, Recorder(), NOW)
    conn.execute("update tasks set status = 'done' where id = 't_b'")
    assert wake.stale_sweep(conn, state, Recorder(), NOW) == {}


def test_tick_sweeps_only_on_relevant_activity(monkeypatch, tmp_path):
    import sys
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "kanban.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "kanban.db")
    conn.execute("create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer)")
    conn.commit()
    calls = []
    monkeypatch.setattr(wake, "stale_sweep", lambda *a: calls.append(a) or {})
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    wake.stale_on_tick(board=None)  # no cursor yet: one initialization sweep, then quiet
    wake.stale_on_tick(board=None)
    assert len(calls) == 1
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'heartbeat')")
    conn.commit()
    wake.stale_on_tick(board=None)
    assert len(calls) == 1  # heartbeat is not state movement: skipped
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'blocked')")
    conn.commit()
    wake.stale_on_tick(board=None)
    assert len(calls) == 2
    wake.stale_on_tick(board=None)  # no new relevant activity: skipped
    assert len(calls) == 2

def test_a_poke_forces_the_sweep_despite_an_unchanged_cursor(monkeypatch, tmp_path):
    import sys
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "kanban.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "kanban.db")
    conn.executescript(
        """
        create table tasks (id text, status text, title text, block_kind text, created_at integer);
        create table task_links (parent_id text, child_id text);
        create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text);
        """
    )
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    calls = []
    real_sweep = wake.stale_sweep
    monkeypatch.setattr(wake, "stale_sweep", lambda conn, state, inject, now: calls.append(1) or real_sweep(conn, state, inject, now))
    wake.stale_on_tick(board=None)
    assert len(calls) == 1  # initialization sweep already spent the wake
    board_watch.write_state(board_watch.POKE_FILE, {"at": time.time() + 10})
    wake.stale_on_tick(board=None)
    assert len(calls) == 2


def _tick_board(tmp_path, monkeypatch):
    import sys
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "kanban.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "kanban.db")
    conn.executescript(
        """
        create table tasks (id text, status text, title text, block_kind text, created_at integer);
        create table task_links (parent_id text, child_id text);
        create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text);
        """
    )
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    return conn


def test_quiet_board_wakes_when_a_ready_card_crosses_five_minutes(monkeypatch, tmp_path):
    conn = _tick_board(tmp_path, monkeypatch)
    rec = Recorder()
    monkeypatch.setattr(wake, "inject", lambda text, origin: rec(text, origin) or True)
    t0 = 2_000_000.0
    conn.execute("insert into tasks values ('t_q', 'blocked', 'quiet', 'needs_input', ?)", (t0,))
    conn.execute("insert into task_events (task_id, kind, created_at) values ('t_q', 'created', ?)", (t0,))
    conn.execute(
        "insert into kanban_notify_subs values ('t_q', 'telegram', '555000100', '900332', 'dm', '555000100', null, 'default')"
    )
    conn.commit()

    wake.stale_on_tick(board=None, now=t0 + 1)  # installed-guard primer: init sweep wakes nothing, arms due_at
    assert rec.calls == []
    assert json.loads((tmp_path / "kanban-stale-wake-cursor.json").read_text())["due_at"] == t0 + 300
    wake.stale_on_tick(board=None, now=t0 + 299)
    assert rec.calls == []  # quiet, before due: no sweep, no wake
    assert json.loads((tmp_path / "kanban-stale-wake-cursor.json").read_text())["swept_at"] == t0 + 1
    wake.stale_on_tick(board=None, now=t0 + 299)
    assert rec.calls == []
    wake.stale_on_tick(board=None, now=t0 + 301)
    assert woken(rec) == ["t_q"]  # first tick past STALE_AFTER: swept and woken exactly once
    wake.stale_on_tick(board=None, now=t0 + 600)
    assert woken(rec) == ["t_q"]  # later quiet ticks re-wake nothing
    assert len(rec.calls) == 1
    assert json.loads((tmp_path / "kanban-stale-wake-cursor.json").read_text())["due_at"] is None


def test_a_new_state_event_rearms_the_stale_park_wake_on_a_quiet_board(monkeypatch, tmp_path):
    conn = _tick_board(tmp_path, monkeypatch)
    rec = Recorder()
    monkeypatch.setattr(wake, "inject", lambda text, origin: rec(text, origin) or True)
    t0 = 3_000_000.0
    conn.execute("insert into tasks values ('t_r', 'blocked', 'rearm', 'needs_input', ?)", (t0,))
    conn.execute("insert into task_events (task_id, kind, created_at) values ('t_r', 'created', ?)", (t0,))
    conn.execute(
        "insert into kanban_notify_subs values ('t_r', 'telegram', '555000100', '900332', 'dm', '555000100', null, 'default')"
    )
    conn.commit()

    wake.stale_on_tick(board=None, now=t0 + 301)
    wake.stale_on_tick(board=None, now=t0 + 601)
    assert len(rec.calls) == 1  # once per state: the second quiet tick does not re-wake
    at = t0 + 601
    conn.execute("insert into task_events (task_id, kind, created_at) values ('t_r', 'unblocked', ?)", (at,))
    conn.execute("update tasks set status = 'ready' where id = 't_r'")  # unparked, residue record: wakeable again
    conn.commit()
    wake.stale_on_tick(board=None, now=t0 + 602)
    assert len(rec.calls) == 1  # a state event re-arms: next crossing is 5 min after it
    wake.stale_on_tick(board=None, now=at + 301)
    assert len(rec.calls) == 2  # woken again exactly 5 min after the new state event

def test_dumping_sweep_or_dispatcher_crash_keeps_the_cursor_honest(monkeypatch, tmp_path):
    import sys
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "kanban.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "kanban.db")
    conn.execute("create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer)")
    conn.commit()
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'blocked')")
    conn.commit()

    monkeypatch.setattr(wake, "stale_sweep", lambda *a: 1 / 0)
    wake.stale_on_tick(board=None)  # sweep raised: driver still records the cursor
    cursor = json.loads((tmp_path / "kanban-stale-wake-cursor.json").read_text())
    assert cursor["event_id"] == 1

    monkeypatch.setattr(wake, "stale_sweep", lambda *a: {})
    wake.stale_on_tick(board=None)  # new activity? no — cursor already past it: skipped
    assert json.loads((tmp_path / "kanban-stale-wake-cursor.json").read_text()) == cursor

def test_dry_run_never_sweeps_and_never_writes(tmp_path, monkeypatch):
    import sys
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "kanban.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "kanban.db")
    conn.execute("create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer)")
    conn.commit()
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    wake.stale_on_tick(board=None, dry_run=True)
    assert not (tmp_path / "kanban-stale-wake.json").exists() and not (tmp_path / "kanban-stale-wake-cursor.json").exists()


def test_parked_card_wakes_once_per_episode_even_when_reblocked_or_triaged():
    conn = board()
    card(conn, "t_p", "blocked", 2)
    rec = Recorder()
    state = wake.stale_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_p"]
    event(conn, "t_p", "block_loop_detected", 1)
    conn.execute("update tasks set status = 'ready' where id = 't_p'")
    event(conn, "t_p", "promoted", 0.45)
    wake.stale_sweep(conn, state, rec, NOW - 0.44 * HOUR)
    conn.execute("update tasks set status = 'triage' where id = 't_p'")
    event(conn, "t_p", "block_loop_detected", 0.4)
    state = wake.stale_sweep(conn, state, rec, NOW)
    assert len(rec.calls) == 1
    state = wake.stale_sweep(conn, state, rec, NOW + HOUR)
    assert len(rec.calls) == 1
    conn.execute("update tasks set status = 'ready' where id = 't_p'")
    event(conn, "t_p", "unblocked", 0.2)
    state = wake.stale_sweep(conn, state, rec, NOW)
    assert len(rec.calls) == 2
    conn.execute("update tasks set status = 'blocked' where id = 't_p'")
    event(conn, "t_p", "blocked", 0.1)
    wake.stale_sweep(conn, state, rec, NOW)
    assert len(rec.calls) == 3


def test_state_written_before_parked_episodes_does_not_rewake_on_upgrade():
    conn = board()
    card(conn, "t_old", "triage", 2)
    event_id = conn.execute("select max(id) from task_events where task_id = 't_old'").fetchone()[0]
    rec = Recorder()
    state = wake.stale_sweep(conn, {"t_old": event_id}, rec, NOW)
    assert rec.calls == []
    assert state == {"t_old": event_id}  # plain cards no longer wake: legacy entries pass through
