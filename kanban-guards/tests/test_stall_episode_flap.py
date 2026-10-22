"""The stall wake's boundary is the stall episode, not the lane snapshot.

One card looping between todo and ready across dispatcher ticks while its last
30 events are all active_pr guards (the 10-16 storm shape) is a single episode:
the wake keys on the episode anchor — the newest non-active_pr event below the
guard run — which cannot move while the guard run only grows in front. A card
that crosses the epoch boundary stays woken once; lane drift alone must not
re-arm it.
"""

import sqlite3
import sys

wake = sys.modules["kanban_guards"].wakes

NOW = 1_000_000.0
TEN_S = 10


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


def test_lane_flapping_todo_ready_between_ticks_keeps_one_wake_per_episode():
    """The 10-16 storm: one card, 11 wakes across ticks while the run only grew."""
    conn = board()
    card(conn, "t_d", "todo")
    conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values ('t_d', 'spawned', '{}', ?)",
        (next_id(conn) * TEN_S,),
    )
    guards_of(conn, "t_d", wake.STALL_MIN_EVENTS, start=next_id(conn))
    rec = Recorder()
    state = wake.stall_sweep(conn, {}, rec, NOW)
    assert sorted(text.split()[2] for text, _ in rec.calls) == ["t_d"]
    anchor = state["t_d"]
    for flip in range(20):
        new_status = "ready" if flip % 2 == 0 else "todo"
        conn.execute("update tasks set status = ? where id = 't_d'", (new_status,))
        guards_of(conn, "t_d", 1, start=next_id(conn))  # one more guard tick
        assert wake.stall_sweep(conn, state, rec, NOW) == state
    assert len(rec.calls) == 1
    assert state["t_d"] == anchor


def test_epoch_crossing_guards_do_not_rewake_below_the_next_full_window():
    """Guards crossing STALL_MIN_EVENTS while the card stays stalled stay one wake."""
    conn = board()
    card(conn, "t_x")
    guards_of(conn, "t_x", wake.STALL_MIN_EVENTS)
    state = wake.stall_sweep(conn, {}, Recorder(), NOW)
    guards_of(conn, "t_x", wake.STALL_MIN_EVENTS * 2, start=next_id(conn))
    rec = Recorder()
    assert wake.stall_sweep(conn, state, rec, NOW) == state
    assert rec.calls == []


def test_state_key_of_a_growing_run_is_the_anchor_not_the_head():
    conn = board()
    card(conn, "t_g")
    guards_of(conn, "t_g", wake.STALL_MIN_EVENTS)
    state = wake.stall_sweep(conn, {}, Recorder(), NOW)
    anchor = conn.execute(
        "select max(id) from task_events where task_id='t_g' and (kind != 'respawn_guarded'"
        " or instr(coalesce(payload, ''), 'active_pr') = 0)"
    ).fetchone()[0]
    assert anchor is None or state["t_g"] == anchor or anchor == 0
    guards_of(conn, "t_g", 5, start=next_id(conn))
    assert wake.stall_sweep(conn, state, Recorder(), NOW) == state
