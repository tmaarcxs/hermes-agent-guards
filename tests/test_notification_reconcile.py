import json
import sqlite3
import sys
import time

import pytest

guards = sys.modules["agent_guards"]
reconcile = guards.notification_reconcile


def board():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        create table tasks (id text, status text, title text);
        create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text, last_ping_event_id integer);
        """
    )
    return conn


NOW = 1_800_000_000.0
OLD = reconcile.GRACE_SECONDS + 5


def card(conn, tid, status="review", title=None):
    conn.execute("insert into tasks values (?, ?, ?)", (tid, status, title or f"title {tid}"))


def event(conn, tid, kind, payload=None, event_id=None, age=OLD):
    if event_id is None:
        event_id = conn.execute("select coalesce(max(id), 0) + 1 from task_events").fetchone()[0]
    conn.execute(
        "insert into task_events (id, task_id, kind, payload, created_at) values (?, ?, ?, ?, ?)",
        (event_id, tid, kind, json.dumps(payload) if isinstance(payload, dict) else payload, NOW - age),
    )
    return event_id


def sub(conn, tid, last_ping_event_id=0):
    conn.execute(
        "insert into kanban_notify_subs values (?, 'telegram', '407617100', '', 'dm', '407617100', null, 'default', ?)",
        (tid, last_ping_event_id),
    )


def drop_sub(conn, tid):
    conn.execute("delete from kanban_notify_subs where task_id = ?", (tid,))


class Recorder:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, text, origin):
        self.calls.append((text, origin))
        return self.ok


def woken(rec):
    return [text.split()[3] for text, _ in rec.calls]


def deliver(conn, tid, event_id):
    """The gateway's notifier delivered everything up to ``event_id``."""
    conn.execute(
        "update kanban_notify_subs set last_ping_event_id = max(last_ping_event_id, ?) where task_id = ?",
        (event_id, tid),
    )


# --- the reconciled hole: sub dropped with an undelivered state ---------------


def test_dropped_sub_with_undelivered_state_notifies_once():
    conn = board()
    card(conn, "t_h1")
    event(conn, "t_h1", "status", {"status": "review"})
    sub(conn, "t_h1")
    rec = Recorder()
    state = reconcile.sweep(conn, {}, rec, NOW)
    assert rec.calls == []
    event(conn, "t_h1", "completed", {"summary": "shipped the fix"})
    state = reconcile.sweep(conn, state, rec, NOW)
    assert rec.calls == []  # live sub: gateway's retry loop owns it
    drop_sub(conn, "t_h1")
    state2 = reconcile.sweep(conn, state, rec, NOW)
    assert woken(rec) == ["t_h1"]
    assert "completed" in rec.calls[0][0]
    assert "shipped the fix" in rec.calls[0][0]
    assert reconcile.sweep(conn, state2, rec, NOW) == state2
    assert len(rec.calls) == 1


def test_unchanged_state_after_the_notice_never_repeats():
    conn = board()
    card(conn, "t_h2", status="blocked")
    event(conn, "t_h2", "review_requested")
    sub(conn, "t_h2")
    rec = Recorder()
    state = reconcile.sweep(conn, {}, rec, NOW)
    assert rec.calls == []
    event(conn, "t_h2", "gave_up", {
        "failures": 2, "effective_limit": 2, "limit_source": "dispatcher",
        "error": "pid 27608 exited with exit code 1",
    })
    state = reconcile.sweep(conn, state, rec, NOW)
    drop_sub(conn, "t_h2")
    state = reconcile.sweep(conn, state, rec, NOW)
    assert len(rec.calls) == 1
    assert "gave_up" in rec.calls[0][0]
    assert "pid 27608 exited with exit code 1" in rec.calls[0][0]
    for _ in range(3):
        assert reconcile.sweep(conn, state, rec, NOW) == state
    assert len(rec.calls) == 1


def test_new_state_after_the_notice_notifies_once_more():
    conn = board()
    card(conn, "t_h3")
    event(conn, "t_h3", "completed")
    sub(conn, "t_h3")
    rec = Recorder()
    state = reconcile.sweep(conn, {}, rec, NOW)
    event(conn, "t_h3", "gave_up", {"error": "second failure"})
    state = reconcile.sweep(conn, state, rec, NOW)
    drop_sub(conn, "t_h3")
    state = reconcile.sweep(conn, state, rec, NOW)
    assert len(rec.calls) == 1
    event(conn, "t_h3", "blocked", {"reason": "needs credentials"})
    conn.execute("update tasks set status = 'blocked' where id = 't_h3'")
    state2 = reconcile.sweep(conn, state, rec, NOW)
    assert len(rec.calls) == 2
    assert "blocked" in rec.calls[1][0]
    assert reconcile.sweep(conn, state2, rec, NOW) == state2


def test_review_request_and_changes_requested_reconcile_too():
    conn = board()
    for tid, kind, payload in (
        ("t_r4", "review_requested", {"summary": "PR up"}),
        ("t_r5", "changes_requested", {"reason": "tests missing"}),
    ):
        card(conn, tid)
        event(conn, tid, "status")
        sub(conn, tid)
    first = reconcile.sweep(conn, {}, Recorder(), NOW)
    event(conn, "t_r4", "review_requested", {"summary": "PR up"})
    event(conn, "t_r5", "changes_requested", {"reason": "tests missing"})
    rec = Recorder()
    state = reconcile.sweep(conn, first, rec, NOW)
    assert rec.calls == []
    drop_sub(conn, "t_r4")
    drop_sub(conn, "t_r5")
    state2 = reconcile.sweep(conn, state, rec, NOW)
    assert sorted(woken(rec)) == ["t_r4", "t_r5"]
    assert sorted(state2) == ["t_r4", "t_r5"]


def test_remembered_origin_receives_the_notice_after_the_drop():
    conn = board()
    card(conn, "t_h4")
    event(conn, "t_h4", "review_requested")
    sub(conn, "t_h4")
    rec = Recorder()
    state = reconcile.sweep(conn, {}, rec, NOW)
    event(conn, "t_h4", "gave_up", {"error": "after the drop"})
    state = reconcile.sweep(conn, state, rec, NOW)
    drop_sub(conn, "t_h4")
    state2 = reconcile.sweep(conn, state, rec, NOW)
    assert len(rec.calls) == 1
    assert state2["t_h4"]["subscribers"][0]["chat_id"] == "407617100"
    assert conn.execute("select count(*) from kanban_notify_subs").fetchone()[0] == 0


# --- delivered, silent, out-of-scope ------------------------------------------


def test_delivered_state_never_notifies_nor_records():
    conn = board()
    card(conn, "t_ok")
    event(conn, "t_ok", "completed", {"summary": "done"})
    sub(conn, "t_ok", last_ping_event_id=999)
    rec = Recorder()
    assert reconcile.sweep(conn, {}, rec, NOW) == {}
    assert rec.calls == []


def test_any_subscriber_past_the_event_covers_the_card():
    conn = board()
    card(conn, "t_ok2")
    event(conn, "t_ok2", "completed")
    sub(conn, "t_ok2", last_ping_event_id=10 ** 6)
    rec = Recorder()
    assert reconcile.sweep(conn, {}, rec, NOW) == {}
    assert rec.calls == []


def test_silent_kinds_never_reconcile():
    conn = board()
    for tid, kind in (("t_s1", "archived"), ("t_s2", "unblocked"), ("t_s3", "heartbeat"), ("t_s4", "attached")):
        card(conn, tid)
        event(conn, tid, kind)
        sub(conn, tid)
    rec = Recorder()
    assert reconcile.sweep(conn, {}, rec, NOW) == {}
    assert rec.calls == []


def test_card_with_no_notifiable_event_is_ignored():
    conn = board()
    card(conn, "t_none", status="ready")
    event(conn, "t_none", "spawned", {"pid": 9})
    sub(conn, "t_none")
    rec = Recorder()
    assert reconcile.sweep(conn, {}, rec, NOW) == {}
    assert rec.calls == []


def test_running_cards_are_ignored():
    conn = board()
    card(conn, "t_run", status="running")
    event(conn, "t_run", "completed")
    sub(conn, "t_run")
    rec = Recorder()
    assert reconcile.sweep(conn, {}, rec, NOW) == {}
    assert rec.calls == []


def test_card_never_subscribed_stays_out_of_the_record():
    conn = board()
    card(conn, "t_ns")
    event(conn, "t_ns", "completed")
    rec = Recorder()
    state = reconcile.sweep(conn, {}, rec, NOW)
    assert rec.calls == []
    assert state["t_ns"]["event_id"] >= 1
    assert rec.calls == []
    assert reconcile.sweep(conn, state, rec, NOW) == state
    assert rec.calls == []


def test_archived_card_drops_out_of_the_record():
    conn = board()
    card(conn, "t_d1", status="done")
    event(conn, "t_d1", "completed")
    sub(conn, "t_d1")
    rec = Recorder()
    state = reconcile.sweep(conn, {"t_zz": {"event_id": 5, "subscribers": []}}, rec, NOW)
    assert "t_zz" not in state
    drop_sub(conn, "t_d1")
    state2 = reconcile.sweep(conn, state, rec, NOW)
    assert woken(rec) == ["t_d1"]
    conn.execute("update tasks set status = 'archived' where id = 't_d1'")
    state3 = reconcile.sweep(conn, state2, rec, NOW)
    assert len(rec.calls) == 1
    assert state3 == {}


# --- grace period: never race the notifier's own delivery ---------------------


def test_state_inside_the_grace_period_stays_pending_and_undelivered_delivery_clears_it():
    conn = board()
    card(conn, "t_g1")
    event(conn, "t_g1", "status")
    sub(conn, "t_g1")
    rec = Recorder()
    first = reconcile.sweep(conn, {}, rec, NOW)
    assert rec.calls == []
    event(conn, "t_g1", "completed", age=5)
    pending = reconcile.sweep(conn, first, rec, NOW)
    assert rec.calls == []
    assert pending["t_g1"]["pending"] is True
    deliver(conn, "t_g1", 2)
    assert reconcile.sweep(conn, pending, rec, NOW + reconcile.GRACE_SECONDS + 5) == {}
    assert rec.calls == []


def test_pending_state_not_delivered_notifies_once_the_sub_is_dropped():
    conn = board()
    card(conn, "t_g2")
    event(conn, "t_g2", "status")
    sub(conn, "t_g2")
    first = reconcile.sweep(conn, {}, Recorder(), NOW)
    event(conn, "t_g2", "completed", age=5)
    pending = reconcile.sweep(conn, first, Recorder(), NOW)
    assert pending["t_g2"]["pending"] is True
    drop_sub(conn, "t_g2")
    rec = Recorder()
    settled = reconcile.sweep(conn, pending, rec, NOW + reconcile.GRACE_SECONDS + 5)
    assert woken(rec) == ["t_g2"]
    assert "pending" not in settled["t_g2"]
    assert reconcile.sweep(conn, settled, rec, NOW + reconcile.GRACE_SECONDS + 5) == settled


def test_live_sub_lagging_is_recorded_never_notified():
    conn = board()
    card(conn, "t_l1")
    event(conn, "t_l1", "review_requested")
    sub(conn, "t_l1")
    rec = Recorder()
    state = reconcile.sweep(conn, {}, rec, NOW)
    assert rec.calls == []
    event(conn, "t_l1", "completed")
    state2 = reconcile.sweep(conn, state, rec, NOW)
    assert rec.calls == []
    assert state2["t_l1"]["pending"] is True


# --- failure handling ----------------------------------------------------------


def test_failed_injection_does_not_advance_the_record():
    conn = board()
    card(conn, "t_f1")
    event(conn, "t_f1", "review_requested")
    sub(conn, "t_f1")
    rec = Recorder()
    state = reconcile.sweep(conn, {}, rec, NOW)
    event(conn, "t_f1", "completed")
    state = reconcile.sweep(conn, state, rec, NOW)
    drop_sub(conn, "t_f1")
    rec_fail = Recorder(ok=False)
    assert reconcile.sweep(conn, state, rec_fail, NOW) == {}
    assert woken(rec_fail) == ["t_f1"]
    reconcile.sweep(conn, state, rec_fail, NOW)
    assert woken(rec_fail) == ["t_f1", "t_f1"]


def test_cause_extraction_picks_first_present_field():
    assert reconcile._cause(json.dumps({"error": "boom"})) == "boom"
    assert reconcile._cause(json.dumps({"summary": "wrapped package"})) == "wrapped package"
    assert reconcile._cause(json.dumps({"reason": "needs x", "error": "boom"})) == "needs x"
    assert reconcile._cause("not json") == ""
    assert reconcile._cause(None) == ""
    assert reconcile._cause(json.dumps({"error": "a" * 500})) == "a" * 200


def test_wake_text_mentions_state_and_is_terse():
    card_text = reconcile.wake_text({
        "id": "t_t1", "kind": "gave_up", "status": "blocked",
        "title": "t" * 300, "cause": "x" * 500,
    })
    assert "t_t1" in card_text and "gave_up" in card_text
    assert len(card_text) < 1000
    assert "never confirmed delivery" in card_text
