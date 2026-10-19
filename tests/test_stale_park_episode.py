"""The stale wake's suppression gate: park states a repeat wake cannot help.

`wakes` drops the stale wake exactly where the park's
own dispatcher notification has already carried the state to the chats and the
park is out of the lead-reachable set: a `capability` wall on a blocked card
and a triage escalation (block_loop_detected). The dispatcher notified 47 of
46 blocked events within 5 minutes in the 10-12..10-18 sample, so the +5-min
stale wake only restated those; the first transition still appears once, and
the unreconciled-notice sweep covers a lost delivery. Everything else keeps the
legacy behavior: plain parks (`block_kind` NULL or foreign — nobody was told),
`needs_input`/`transient` parks (lead-resolvable, the reason addresses the
lead), and every unparked card even when a block_kind lingers, because
`hermes_cli.kanban_db` deliberately survives a block_kind across an unblock.

The gate is stateless — it reads the live row each sweep — so it cannot leak
bookkeeping, and re-arm semantics stay exactly the pre-existing ones.
"""

import sqlite3

import sys

wake = sys.modules["agent_guards"].wakes

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
    conn.execute(
        "insert into tasks values (?, ?, ?, ?, ?)",
        (tid, status, f"title {tid}", block_kind, NOW - age_h * HOUR),
    )
    conn.execute(
        "insert into task_events (task_id, kind, created_at) values (?, ?, ?)",
        (tid, kind, NOW - age_h * HOUR),
    )
    conn.execute(
        "insert into kanban_notify_subs values (?, 'telegram', '555000100', '900332', 'dm', '555000100', null, 'default')",
        (tid,),
    )


def event(conn, tid, kind, age_h):
    conn.execute(
        "insert into task_events (task_id, kind, created_at) values (?, ?, ?)",
        (tid, kind, NOW - age_h * HOUR),
    )


def woken(rec):
    return sorted(text.split()[3] for text, _ in rec.calls)


class Recorder:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, text, origin):
        self.calls.append((text, origin))
        return self.ok


def test_capability_park_is_suppressed_wherever_the_park_stays():
    """The stop class: the park notification carried the state; repeats go."""
    conn = board()
    card(conn, "t_wall", "blocked", 2, block_kind="capability")
    event(conn, "t_wall", "blocked", 2)
    for hours in (0, 1, 2, 6, 24, 300):
        event(conn, "t_wall", "commented", 0)
        assert wake.stale_sweep(conn, {}, Recorder(), NOW + hours * HOUR) == {}


def test_triage_escalation_is_suppressed_while_it_waits():
    conn = board()
    card(conn, "t_tri", "triage", 2, block_kind="needs_input")
    event(conn, "t_tri", "block_loop_detected", 2)
    rec = Recorder()
    assert wake.stale_sweep(conn, {}, rec, NOW) == {}
    assert rec.calls == []


def test_needs_input_park_keeps_its_wake():
    """Keep class: the block reason addresses the lead; the card is actionable."""
    conn = board()
    card(conn, "t_ask", "blocked", 2, block_kind="needs_input")
    event(conn, "t_ask", "blocked", 2)
    rec = Recorder()
    state = wake.stale_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_ask"]
    assert state == {"t_ask": "parked"}
    event(conn, "t_ask", "commented", 0)
    assert wake.stale_sweep(conn, state, rec, NOW + HOUR) == state
    assert len(rec.calls) == 1  # once per parked stretch, as before


def test_transient_park_keeps_its_wake():
    conn = board()
    card(conn, "t_flaky", "blocked", 2, block_kind="transient")
    event(conn, "t_flaky", "blocked", 2)
    rec = Recorder()
    state = wake.stale_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_flaky"]


def test_foreign_block_kind_value_keeps_its_wake():
    conn = board()
    card(conn, "t_x", "blocked", 2, block_kind="dependency")
    event(conn, "t_x", "blocked", 2)
    rec = Recorder()
    state = wake.stale_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_x"]
    assert state == {"t_x": "parked"}


def test_unblocking_reopens_normal_wakes_even_with_a_lingering_block_kind():
    """block_kind survives an unblock by design (hermes_cli.kanban_db): a
    capability-residue todo card must wake on its own state event."""
    conn = board()
    card(conn, "t_u", "blocked", 2, block_kind="capability")
    event(conn, "t_u", "blocked", 2)
    assert wake.stale_sweep(conn, {}, Recorder(), NOW) == {}
    conn.execute("update tasks set status = 'todo' where id = 't_u'")
    event(conn, "t_u", "unblocked", 0.12)  # 7 min ago: past the 5-minute age
    rec = Recorder()
    state = wake.stale_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_u"]  # the wake re-arms: gate reads status, not residue
    assert state == {"t_u": 3}


def test_re_park_of_a_capability_card_stays_silent_after_an_unblock():
    """Unblock -> re-block same kind: the new park fires its own dispatcher
    notification again, so the stale wake stays silent for the new parked
    stretch too."""
    conn = board()
    card(conn, "t_r", "blocked", 2, block_kind="capability")
    event(conn, "t_r", "blocked", 2)
    assert wake.stale_sweep(conn, {}, Recorder(), NOW) == {}
    conn.execute("update tasks set status = 'todo' where id = 't_r'")
    event(conn, "t_r", "unblocked", 0.12)
    state = wake.stale_sweep(conn, {}, Recorder(), NOW)  # unparked wake spends the state key
    conn.execute("update tasks set status = 'blocked' where id = 't_r'")
    event(conn, "t_r", "blocked", 0.02)
    rec = Recorder()
    assert wake.stale_sweep(conn, state, rec, NOW + HOUR) == state
    assert rec.calls == []  # fresh park, fresh notification, no stale repeat


def test_an_unparked_card_with_a_park_record_wakes_its_state_event():
    """The t_23a8c303/t_58d55e20 record: blocked -> todo with the unblock's
    `needs_input` block_kind residue still wakes — the gate reads status."""
    conn = board()
    card(conn, "t_res", "todo", 2, block_kind="needs_input")
    event(conn, "t_res", "unblocked", 2)
    rec = Recorder()
    state = wake.stale_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_res"]
    assert state == {"t_res": 2}


def test_gate_is_stateless_across_a_lost_state_file():
    conn = board()
    card(conn, "t_k", "blocked", 2, block_kind="capability")
    event(conn, "t_k", "blocked", 2)
    for _ in range(3):
        assert wake.stale_sweep(conn, {}, Recorder(), NOW) == {}


def test_park_record_suppression_does_not_touch_next_due():
    """The due-time cursor stays honest while a suppressed card sits parked."""
    conn = board()
    card(conn, "t_sup", "blocked", 0.01, block_kind="capability")
    event(conn, "t_sup", "blocked", 0.01)
    card(conn, "t_open", "blocked", 0.01, block_kind="needs_input")
    event(conn, "t_open", "blocked", 0.01)
    due = wake.next_due(conn, NOW)
    assert due is not None
    assert wake.stale_sweep(conn, {}, Recorder(), due + 1) == {"t_open": "parked"}
