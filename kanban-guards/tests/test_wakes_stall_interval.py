"""PR-O red: the 10-minute minimum interval between stall wakes (body appended
before running; this stub header survives the lint pass)."""

import sqlite3
import sys

wakes = sys.modules["kanban_guards"].wakes

NOW = 1_000_000.0
TEN_S = 10
MIN = 60


def board():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        create table tasks (id text, status text, title text, block_kind text, created_at integer);
        create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text);
        """
    )
    return conn


def card(conn, tid, status="ready"):
    conn.execute("insert into tasks values (?, ?, 'stalled card', null, 1)", (tid, status))
    conn.execute(
        "insert into kanban_notify_subs values (?, 'telegram', '555000100', null, 'dm', '555000100', null, 'default')",
        (tid,),
    )


def guards_of(conn, tid, n, reason="active_pr", start=1):
    last = start
    for i in range(n):
        conn.execute(
            "insert into task_events (task_id, kind, payload, created_at) values (?, 'respawn_guarded', ?, ?)",
            (tid, f'{{"reason":"{reason}"}}', start + i * TEN_S),
        )
        last += 1
    return last - 1


def next_id(conn):
    return 1 + (conn.execute("select coalesce(max(id), 0) from task_events").fetchone()[0])


class Recorder:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, text, origin):
        self.calls.append((text, origin))
        return self.ok


def test_a_renewed_episode_inside_ten_minutes_stays_silent():
    """The 10-16 storm shape: a fresh full guard run inside 10 min must not wake again."""
    conn = board()
    card(conn, "t_s")
    guards_of(conn, "t_s", wakes.STALL_MIN_EVENTS)
    state = wakes.stall_sweep(conn, {}, Recorder(), NOW)
    assert state, "first episode wakes"
    conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values ('t_s', 'commented', '{}', ?)",
        (next_id(conn),),
    )
    guards_of(conn, "t_s", wakes.STALL_MIN_EVENTS, start=next_id(conn))
    rec = Recorder()
    state2 = wakes.stall_sweep(conn, state, rec, NOW + 5 * MIN)
    assert rec.calls == [], "9.9 min after the previous wake: silent"
    assert state2 == state


def test_a_renewed_episode_at_or_after_ten_minutes_wakes_again():
    conn = board()
    card(conn, "t_s")
    guards_of(conn, "t_s", wakes.STALL_MIN_EVENTS)
    first_at = NOW - 3 * TEN_S
    state = wakes.stall_sweep(conn, {}, Recorder(), first_at)
    conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values ('t_s', 'commented', '{}', ?)",
        (next_id(conn),),
    )
    guards_of(conn, "t_s", wakes.STALL_MIN_EVENTS, start=next_id(conn))
    rec = Recorder()
    state2 = wakes.stall_sweep(conn, state, rec, first_at + 10 * MIN + TEN_S)
    assert sorted(text.split()[2] for text, _ in rec.calls) == ["t_s"]
    assert state2["t_s"] != state["t_s"]


def test_the_interval_is_per_card_not_global():
    conn = board()
    card(conn, "t_a")
    card(conn, "t_b")
    guards_of(conn, "t_b", wakes.STALL_MIN_EVENTS)
    wake_b = wakes.stall_sweep(conn, {}, Recorder(), NOW)
    conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values ('t_a', 'spawned', '{}', ?)",
        (next_id(conn),),
    )
    guards_of(conn, "t_a", wakes.STALL_MIN_EVENTS, start=next_id(conn))
    rec = Recorder()
    wakes.stall_sweep(conn, wake_b, rec, NOW)
    assert sorted(text.split()[2] for text, _ in rec.calls) == ["t_a"]


def test_legacy_state_keys_still_suppress_after_an_interval_wake():
    """Pre-interval state (anchor id only) reads as woken; the interval adds no re-arm."""
    conn = board()
    card(conn, "t_s")
    guards_of(conn, "t_s", wakes.STALL_MIN_EVENTS)
    legacy = {"t_s": int(conn.execute("select max(id) from task_events where task_id='t_s'").fetchone()[0])}
    rec = Recorder()
    assert wakes.stall_sweep(conn, legacy, rec, NOW) == legacy
    assert rec.calls == []
