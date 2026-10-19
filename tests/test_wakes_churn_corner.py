"""PR-O red: churn keeps only the change-request corner case.

The census recorded one churn fire in 14 days and the plan row pins the kept
condition to ``changes_requested >= 10``: the runs family and the 10/20 ...
re-arm ladder are gone, and a card crossing the change-request corner case
warns exactly once per ungated crossing (re-arming when the count falls back
below the point and crosses again)."""

import json
import sqlite3
import sys

wakes = sys.modules["agent_guards"].wakes


def board():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        create table tasks (id text, status text, title text);
        create table task_runs (id integer primary key, task_id text);
        create table task_events (id integer primary key, task_id text, kind text, payload text);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text);
        """
    )
    return conn


def card(conn, tid, status, runs=0, crs=0, subscribed=True, reasons=()):
    conn.execute("insert into tasks values (?, ?, ?)", (tid, status, f"title {tid}"))
    for _ in range(runs):
        conn.execute("insert into task_runs (task_id) values (?)", (tid,))
    for n in range(crs):
        conn.execute(
            "insert into task_events (task_id, kind, payload) values (?, 'changes_requested', ?)",
            (tid, json.dumps({"reason": reasons[n] if n < len(reasons) else f"fix {n + 1}"})),
        )
    if subscribed:
        conn.execute(
            "insert into kanban_notify_subs values (?, 'telegram', '555000100', '900332', 'dm', '555000100', null, 'default')",
            (tid,),
        )


class Recorder:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, text, origin):
        self.calls.append((text, origin))
        return self.ok


def woken(rec):
    return sorted(text.split()[3] for text, _ in rec.calls)


def cr(conn, tid, reason):
    conn.execute(
        "insert into task_events (task_id, kind, payload) values (?, 'changes_requested', ?)",
        (tid, json.dumps({"reason": reason})),
    )


def test_ten_change_requests_warn_with_the_last_reasons():
    conn = board()
    card(conn, "t_crs", "review", crs=10, reasons=[f"reason {n}" for n in range(10)])
    card(conn, "t_nine", "review", crs=9, reasons=["r"] * 9)
    rec = Recorder()
    state = wakes.churn_sweep(conn, {}, rec)
    assert woken(rec) == ["t_crs"]
    text, _ = rec.calls[0]
    assert "10 change-requests" in text and "reason 8" in text and "reason 0" not in text
    assert sorted(state) == ["t_crs"]


def test_twenty_change_requests_never_wake_again_without_falling_back_below_ten():
    conn = board()
    card(conn, "t_crs", "review", crs=10, reasons=["r"] * 10)
    state = wakes.churn_sweep(conn, {}, Recorder())
    for n in range(10):
        cr(conn, "t_crs", f"more {n}")
    rec = Recorder()
    state2 = wakes.churn_sweep(conn, state, rec)
    assert rec.calls == [], "no re-arm ladder at 20: one wake per crossing"
    assert state2 == state


def test_falling_back_below_ten_and_crossing_again_rearms():
    conn = board()
    card(conn, "t_crs", "blocked", crs=10, reasons=["r"] * 10)
    state = wakes.churn_sweep(conn, {}, Recorder())
    rows = conn.execute(
        "select rowid from task_events where task_id = 't_crs' and kind = 'changes_requested' order by id desc limit 8"
    ).fetchall()
    for (rowid,) in rows:
        conn.execute("delete from task_events where rowid = ?", (rowid,))
    conn.execute("delete from tasks where id = 't_crs'")
    card(conn, "t_crs", "blocked", crs=2, subscribed=False)
    rec = Recorder()
    state = wakes.churn_sweep(conn, state, rec)  # the card left the corner case: its entry drops
    assert rec.calls == [] and state == {}
    for n in range(8):
        cr(conn, "t_crs", f"again {n}")
    rec2 = Recorder()
    state2 = wakes.churn_sweep(conn, state, rec2)
    assert woken(rec2) == ["t_crs"]


def test_high_run_counts_alone_never_warn():
    conn = board()
    card(conn, "t_runs", "ready", runs=45)
    rec = Recorder()
    assert wakes.churn_sweep(conn, {}, rec) == {}
    assert rec.calls == []


def test_done_archived_cards_and_cards_without_subscribers_never_warn():
    conn = board()
    card(conn, "t_done", "done", crs=12)
    card(conn, "t_arch", "archived", crs=12)
    card(conn, "t_nochat", "review", crs=12, subscribed=False)
    rec = Recorder()
    assert wakes.churn_sweep(conn, {}, rec) == {}
    assert rec.calls == []


def test_failed_injection_retries_later():
    conn = board()
    card(conn, "t_fail", "review", crs=10, reasons=["r"] * 10)
    state = wakes.churn_sweep(conn, {}, Recorder(ok=False))
    assert state == {}
    rec = Recorder()
    assert wakes.churn_sweep(conn, state, rec)
    assert woken(rec) == ["t_fail"]
