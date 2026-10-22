"""PR-O red: the plain stale classes (no park record) no longer wake; park and
residue classes keep their wake. Census 10-05..10-19: the 17 plain wakes acted
0/17 while the park wakes acted 5/15, so every stale wake without a block_kind
record is dropped and everything with one is kept (minus the capability/triage
suppresses already gated)."""

import sqlite3
import sys

wakes = sys.modules["kanban_guards"].wakes

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


def card(conn, tid, status, age_h, block_kind=None, kind="created"):
    at = NOW - age_h * HOUR
    conn.execute("insert into tasks values (?, ?, ?, ?, ?)", (tid, status, f"title {tid}", block_kind, at))
    conn.execute("insert into task_events (task_id, kind, created_at) values (?, ?, ?)", (tid, kind, at))
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


def test_plain_todo_ready_and_recordless_blocked_cards_stay_silent():
    """The 0/17 census class: every stale card without a block_kind record."""
    conn = board()
    card(conn, "t_plain", "todo", 3)
    card(conn, "t_ready", "ready", 3)
    card(conn, "t_norec", "blocked", 3)
    card(conn, "t_sched", "scheduled", 3)
    card(conn, "t_rev", "review", 3)
    rec = Recorder()
    assert wakes.stale_sweep(conn, {}, rec, NOW) == {}
    assert rec.calls == []
    assert wakes.next_due(conn, NOW) is None


def test_a_park_with_a_record_still_wakes_once_per_stretch():
    conn = board()
    card(conn, "t_ask", "blocked", 2, block_kind="needs_input")
    event(conn, "t_ask", "blocked", 2)
    rec = Recorder()
    state = wakes.stale_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_ask"]
    event(conn, "t_ask", "commented", 0)
    rec2 = Recorder()
    assert wakes.stale_sweep(conn, state, rec2, NOW + HOUR) == state
    assert rec2.calls == []

    conn.execute("insert into tasks values ('t_flaky', 'blocked', 'x', 'transient', ?)", (NOW - 2 * HOUR,))
    conn.execute("insert into task_events (task_id, kind, created_at) values ('t_flaky', 'blocked', ?)", (NOW - 2 * HOUR,))
    conn.execute(
        "insert into kanban_notify_subs values ('t_flaky', 'telegram', '555000100', '900332', 'dm', '555000100', null, 'default')"
    )
    rec3 = Recorder()
    wakes.stale_sweep(conn, state, rec3, NOW + HOUR)
    assert woken(rec3) == ["t_flaky"]


def test_an_unparked_card_with_a_lingering_record_wakes_its_state_event():
    conn = board()
    card(conn, "t_res", "todo", 2, block_kind="needs_input")
    event(conn, "t_res", "unblocked", 2)
    rec = Recorder()
    state = wakes.stale_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_res"]
    assert state == {"t_res": 2}


def test_a_foreign_record_on_a_parked_card_counts_as_a_park():
    conn = board()
    card(conn, "t_x", "blocked", 2, block_kind="dependency")
    event(conn, "t_x", "blocked", 2)
    rec = Recorder()
    state = wakes.stale_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_x"]


def test_capability_park_and_triage_escalation_stay_suppressed_wherever_they_sit():
    conn = board()
    card(conn, "t_wall", "blocked", 2, block_kind="capability")
    event(conn, "t_wall", "blocked", 2)
    card(conn, "t_esc", "triage", 2, block_kind="capability")
    event(conn, "t_esc", "block_loop_detected", 2)
    rec = Recorder()
    for _ in range(3):
        assert wakes.stale_sweep(conn, {}, rec, NOW) == {}
    assert rec.calls == []


def test_todo_card_waiting_on_an_unfinished_parent_is_never_woken_even_parked():
    conn = board()
    card(conn, "t_parent", "running", 3)
    card(conn, "t_child", "todo", 3, block_kind="needs_input")
    conn.execute("insert into task_links values ('t_parent', 't_child')")
    rec = Recorder()
    wakes.stale_sweep(conn, {}, rec, NOW)
    assert rec.calls == []
