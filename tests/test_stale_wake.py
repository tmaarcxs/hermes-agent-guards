import importlib.util
import sqlite3
from pathlib import Path

import sys

wake = sys.modules["agent_guards"].stale_wake

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


def test_tick_is_rate_limited_and_fails_open(monkeypatch, tmp_path):
    import sys
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "kanban.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    sqlite3.connect(tmp_path / "kanban.db").close()
    calls = []
    monkeypatch.setattr(wake, "sweep", lambda *a: calls.append(a) or {})
    monkeypatch.setattr(wake, "_state_path", lambda: tmp_path / "s.json")
    monkeypatch.setattr(wake, "_last_check", 0.0)
    wake.on_tick(board=None)
    wake.on_tick(board=None)
    assert len(calls) == 1
    monkeypatch.setattr(wake, "_last_check", 0.0)
    monkeypatch.setattr(wake, "sweep", lambda *a: 1 / 0)
    wake.on_tick(board=None)
    wake.on_tick(board=None, dry_run=True)
