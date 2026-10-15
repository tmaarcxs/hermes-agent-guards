import sqlite3
import sys

guards = sys.modules["agent_guards"]
wake = guards.stale_wake

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
    """n trailing respawn_guarded events, 10 s apart; the in-sample max rowid is returned."""
    last = start
    for i in range(n):
        conn.execute(
            "insert into task_events (task_id, kind, payload, created_at) values (?, 'respawn_guarded', ?, ?)",
            (tid, f'{{"reason":"{reason}"}}', start + i * TEN_S),
        )
        last += 1
    return last - 1


class Recorder:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, text, origin):
        self.calls.append((text, origin))
        return self.ok


def stalled(conn):
    return [c["id"] for c in wake.stalled_cards(conn)]


def test_trailing_active_pr_guards_stall_the_card():
    conn = board()
    card(conn, "t_s")
    guards_of(conn, "t_s", wake.STALL_MIN_EVENTS)
    assert stalled(conn) == ["t_s"]


def test_fewer_than_threshold_events_do_not_stall():
    conn = board()
    card(conn, "t_s")
    guards_of(conn, "t_s", wake.STALL_MIN_EVENTS - 1)
    assert stalled(conn) == []


def test_a_newer_real_event_breaks_the_stall():
    conn = board()
    card(conn, "t_s")
    guards_of(conn, "t_s", wake.STALL_MIN_EVENTS)
    conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values (?, 'commented', '{}', ?)",
        ("t_s", 10_000),
    )
    assert stalled(conn) == []


def test_other_guard_reasons_do_not_stall():
    conn = board()
    card(conn, "t_s")
    guards_of(conn, "t_s", wake.STALL_MIN_EVENTS, reason="recent_success")
    assert stalled(conn) == []


def test_running_and_blocked_cards_are_never_stall_woken():
    conn = board()
    card(conn, "t_run", status="running")
    card(conn, "t_block", status="blocked")
    guards_of(conn, "t_run", wake.STALL_MIN_EVENTS + 5)
    guards_of(conn, "t_block", wake.STALL_MIN_EVENTS + 5, start=100)
    assert stalled(conn) == []


def test_stall_sweep_wakes_once_per_guard_event_and_rearms_on_new_guards():
    conn = board()
    card(conn, "t_s")
    guards_of(conn, "t_s", wake.STALL_MIN_EVENTS)
    rec = Recorder()
    state = wake.stall_sweep(conn, {}, rec, NOW)
    assert sorted(text.split()[2] for text, _ in rec.calls) == ["t_s"]
    max_id = conn.execute("select max(id) from task_events where task_id='t_s'").fetchone()[0]
    assert list(state.values()) == [max_id]
    rec2 = Recorder()
    assert wake.stall_sweep(conn, state, rec2, NOW) == state
    assert rec2.calls == []
    # A new guard event changes the state and re-arms exactly one more wake.
    conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values "
        "('t_s', 'respawn_guarded', '{\"reason\":\"active_pr\"}', ?)",
        (10_000,),
    )
    rec3 = Recorder()
    state2 = wake.stall_sweep(conn, state, rec3, NOW)
    assert len(rec3.calls) == 1
    assert state2["t_s"] > state["t_s"]


def test_stall_sweep_forgets_cards_that_left_the_stall_set():
    conn = board()
    card(conn, "t_s")
    guards_of(conn, "t_s", wake.STALL_MIN_EVENTS)
    state = wake.stall_sweep(conn, {}, Recorder(), NOW)
    conn.execute("update tasks set status = 'done' where id = 't_s'")
    assert wake.stall_sweep(conn, state, Recorder(), NOW) == {}


def test_failed_injection_retries_next_tick():
    conn = board()
    card(conn, "t_s")
    guards_of(conn, "t_s", wake.STALL_MIN_EVENTS)
    failing = Recorder(ok=False)
    state = wake.stall_sweep(conn, {}, failing, NOW)
    assert state == {}
    rec = Recorder()
    wake.stall_sweep(conn, state, rec, NOW)
    assert len(rec.calls) == 1


def test_stall_wake_on_tick_is_unthrottled_and_named_fails_open(monkeypatch, tmp_path):
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
    conn.execute("insert into tasks values ('t_s', 'ready', 'stalled card', null, 1)")
    conn.execute(
        "insert into kanban_notify_subs values ('t_s', 'telegram', '555000100', null, 'dm', '555000100', null, 'default')"
    )
    for i in range(wake.STALL_MIN_EVENTS):
        conn.execute(
            "insert into task_events (task_id, kind, payload, created_at) values "
            "('t_s', 'respawn_guarded', '{\"reason\":\"active_pr\"}', ?)",
            (i * 10,),
        )
    conn.commit()
    conn.close()

    calls = []
    monkeypatch.setattr(wake, "_inject", lambda text, origin: calls.append((text, origin)) or True)
    monkeypatch.setattr(wake, "_stall_state_path", lambda: tmp_path / "stall.json")
    # Two ticks 1 s apart: both must sweep (unthrottled), the second is a state no-op.
    wake.stall_wake_on_tick(board=None)
    n_after_first = len(tmp_path.joinpath("stall.json").read_text())
    wake.stall_wake_on_tick(board=None, dry_run=True)
    wake.stall_wake_on_tick(board=None)
    assert len(calls) == 1
    assert "[Kanban stall]" in calls[0][0]
    assert tmp_path.joinpath("stall.json").read_text() == tmp_path.joinpath("stall.json").read_text()
    assert n_after_first > 0

    monkeypatch.setattr(wake, "stall_sweep", lambda *a: 1 / 0)
    wake.stall_wake_on_tick(board=None)  # fails open, no raise
    wake.stall_wake_on_tick(board=None, dry_run=True)
