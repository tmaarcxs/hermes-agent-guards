import importlib.util
import json
import sqlite3
import time
from pathlib import Path

import sys

wake = sys.modules["agent_guards"].stale_wake

from agent_guards import board_watch

NOW = 1_000_000.0
HOUR = 3600


def board():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        create table tasks (id text, status text, title text, block_kind text, created_at integer);
        create table task_events (id integer primary key, task_id text, kind text, created_at integer);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text);
        """
    )
    return conn


def card(conn, tid, status, age_h, subscribed=True, kind="created"):
    at = NOW - age_h * HOUR
    conn.execute("insert into tasks values (?, ?, ?, null, ?)", (tid, status, f"title {tid}", at))
    conn.execute("insert into task_events (task_id, kind, created_at) values (?, ?, ?)", (tid, kind, at))
    if subscribed:
        conn.execute(
            "insert into kanban_notify_subs values (?, 'telegram', '407617100', '179332', 'dm', '407617100', null, 'default')",
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
    state = wake.sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_blocked", "t_todo"]
    assert rec.calls[0][1] == {
        "platform": "telegram", "chat_id": "407617100", "thread_id": "179332", "chat_type": "dm",
        "user_id": "407617100", "profile": "default",
    }
    rec2 = Recorder()
    assert wake.sweep(conn, state, rec2, NOW + 5 * HOUR) == state
    assert rec2.calls == []


def test_running_and_recent_cards_are_never_woken():
    conn = board()
    card(conn, "t_running", "running", 10)
    card(conn, "t_recent", "blocked", 0.5)
    card(conn, "t_done", "done", 10)
    rec = Recorder()
    wake.sweep(conn, {}, rec, NOW)
    assert rec.calls == []


def test_parked_scheduled_card_wakes_its_chat():
    conn = board()
    card(conn, "t_scheduled", "scheduled", 2, kind="scheduled")
    rec = Recorder()
    wake.sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_scheduled"]


def test_heartbeats_do_not_count_as_movement_or_reset_the_state():
    conn = board()
    card(conn, "t_b", "blocked", 3)
    event(conn, "t_b", "heartbeat", 0.1)
    rec = Recorder()
    state = wake.sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_b"]
    event(conn, "t_b", "heartbeat", 0)
    rec2 = Recorder()
    wake.sweep(conn, state, rec2, NOW)
    assert rec2.calls == []


def test_agent_comment_after_wake_does_not_rearm():
    conn = board()
    card(conn, "t_b", "blocked", 3)
    state = wake.sweep(conn, {}, Recorder(), NOW)
    event(conn, "t_b", "commented", 0)
    event(conn, "t_b", "edited", 0)
    rec = Recorder()
    assert wake.sweep(conn, state, rec, NOW + 2 * HOUR) == state
    assert rec.calls == []


def test_a_real_state_change_rearms_the_wake():
    conn = board()
    card(conn, "t_b", "blocked", 3)
    state = wake.sweep(conn, {}, Recorder(), NOW)
    event(conn, "t_b", "unblocked", 2.5)
    event(conn, "t_b", "blocked", 2)
    rec = Recorder()
    state = wake.sweep(conn, state, rec, NOW)
    assert woken(rec) == ["t_b"]


def test_card_without_chat_is_skipped_and_failed_injection_retries():
    conn = board()
    card(conn, "t_nochat", "blocked", 3, subscribed=False)
    card(conn, "t_b", "blocked", 3)
    failing = Recorder(ok=False)
    state = wake.sweep(conn, {}, failing, NOW)
    assert woken(failing) == ["t_b"]
    assert state == {}
    rec = Recorder()
    wake.sweep(conn, state, rec, NOW)
    assert woken(rec) == ["t_b"]


def test_cards_that_leave_the_stale_set_are_forgotten():
    conn = board()
    card(conn, "t_b", "blocked", 3)
    state = wake.sweep(conn, {}, Recorder(), NOW)
    conn.execute("update tasks set status = 'done' where id = 't_b'")
    assert wake.sweep(conn, state, Recorder(), NOW) == {}


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
    monkeypatch.setattr(wake, "sweep", lambda *a: calls.append(a) or {})
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    wake.on_tick(board=None)  # no cursor yet: one initialization sweep, then quiet
    wake.on_tick(board=None)
    assert len(calls) == 1
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'heartbeat')")
    conn.commit()
    wake.on_tick(board=None)
    assert len(calls) == 1  # heartbeat is not state movement: skipped
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'blocked')")
    conn.commit()
    wake.on_tick(board=None)
    assert len(calls) == 2
    wake.on_tick(board=None)  # no new relevant activity: skipped
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
        create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text);
        """
    )
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    calls = []
    real_sweep = wake.sweep
    monkeypatch.setattr(wake, "sweep", lambda conn, state, inject, now: calls.append(1) or real_sweep(conn, state, inject, now))
    wake.on_tick(board=None)
    assert len(calls) == 1  # initialization sweep already spent the wake
    board_watch.write_state(board_watch.POKE_FILE, {"at": time.time() + 10})
    wake.on_tick(board=None)
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
        create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text);
        """
    )
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    return conn


def test_quiet_board_wakes_when_a_ready_card_crosses_one_hour(monkeypatch, tmp_path):
    conn = _tick_board(tmp_path, monkeypatch)
    rec = Recorder()
    monkeypatch.setattr(wake, "_inject", lambda text, origin: rec(text, origin) or True)
    t0 = 2_000_000.0
    conn.execute("insert into tasks values ('t_q', 'ready', 'quiet', null, ?)", (t0,))
    conn.execute("insert into task_events (task_id, kind, created_at) values ('t_q', 'created', ?)", (t0,))
    conn.execute(
        "insert into kanban_notify_subs values ('t_q', 'telegram', '407617100', '179332', 'dm', '407617100', null, 'default')"
    )
    conn.commit()

    wake.on_tick(board=None, now=t0 + 1)  # installed-guard primer: init sweep wakes nothing, arms due_at
    assert rec.calls == []
    assert json.loads((tmp_path / "kanban-stale-wake-cursor.json").read_text())["due_at"] == t0 + 3600
    wake.on_tick(board=None, now=t0 + 3599)
    assert rec.calls == []  # quiet, before due: no sweep, no wake
    assert json.loads((tmp_path / "kanban-stale-wake-cursor.json").read_text())["swept_at"] == t0 + 1
    wake.on_tick(board=None, now=t0 + 3599)
    assert rec.calls == []
    wake.on_tick(board=None, now=t0 + 3601)
    assert woken(rec) == ["t_q"]  # first tick past STALE_AFTER: swept and woken exactly once
    wake.on_tick(board=None, now=t0 + 7200)
    assert woken(rec) == ["t_q"]  # later quiet ticks re-wake nothing
    assert len(rec.calls) == 1
    assert json.loads((tmp_path / "kanban-stale-wake-cursor.json").read_text())["due_at"] is None


def test_a_new_state_event_rearms_the_stale_wake_on_a_quiet_board(monkeypatch, tmp_path):
    conn = _tick_board(tmp_path, monkeypatch)
    rec = Recorder()
    monkeypatch.setattr(wake, "_inject", lambda text, origin: rec(text, origin) or True)
    t0 = 3_000_000.0
    conn.execute("insert into tasks values ('t_r', 'ready', 'rearm', null, ?)", (t0,))
    conn.execute("insert into task_events (task_id, kind, created_at) values ('t_r', 'created', ?)", (t0,))
    conn.execute(
        "insert into kanban_notify_subs values ('t_r', 'telegram', '407617100', '179332', 'dm', '407617100', null, 'default')"
    )
    conn.commit()

    wake.on_tick(board=None, now=t0 + 3601)
    wake.on_tick(board=None, now=t0 + 7201)
    assert len(rec.calls) == 1  # once per state: the second quiet tick does not re-wake
    at = t0 + 7201
    conn.execute("insert into task_events (task_id, kind, created_at) values ('t_r', 'unblocked', ?)", (at,))
    conn.commit()
    wake.on_tick(board=None, now=t0 + 7202)
    assert len(rec.calls) == 1  # a state event re-arms: next crossing is 1 h after it
    wake.on_tick(board=None, now=at + 3601)
    assert len(rec.calls) == 2  # woken again exactly one hour after the new state event

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

    monkeypatch.setattr(wake, "sweep", lambda *a: 1 / 0)
    wake.on_tick(board=None)  # sweep raised: driver still records the cursor
    cursor = json.loads((tmp_path / "kanban-stale-wake-cursor.json").read_text())
    assert cursor["event_id"] == 1

    monkeypatch.setattr(wake, "sweep", lambda *a: {})
    wake.on_tick(board=None)  # new activity? no — cursor already past it: skipped
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
    wake.on_tick(board=None, dry_run=True)
    assert not (tmp_path / "kanban-stale-wake.json").exists() and not (tmp_path / "kanban-stale-wake-cursor.json").exists()
