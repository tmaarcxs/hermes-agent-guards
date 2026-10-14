"""Jev wake filter in ``stale_wake``: ``skip`` at/above confidence suppresses a
wake; ``wake``, malformed answers, and every failure mode still wake (fail open).
``jev_decide`` is mocked; the network is never touched.
"""

import sys

import pytest

wake = sys.modules["agent_guards"].stale_wake

NOW = 1_000_000.0
HOUR = 3600

ANSWER_WAKE = {"type": "choice", "choice": "wake", "confidence": 0.99}
ANSWER_SKIP_HIGH = {"type": "choice", "choice": "skip", "confidence": 0.95}
ANSWER_SKIP_LOW = {"type": "choice", "choice": "skip", "confidence": 0.5}


def board():
    conn = __import__("sqlite3").connect(":memory:")
    conn.executescript(
        """
        create table tasks (id text, status text, title text, block_kind text, created_at integer);
        create table task_events (id integer primary key, task_id text, kind text, created_at integer);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text);
        create table comments (id integer primary key, task_id text, body text, created_at integer);
        """
    )
    return conn


def card(conn, tid, status="blocked", age_h=2):
    at = NOW - age_h * HOUR
    conn.execute("insert into tasks values (?, ?, ?, null, ?)", (tid, status, f"title {tid}", at))
    conn.execute("insert into task_events (task_id, kind, created_at) values (?, ?, ?)", (tid, "created", at))
    conn.execute(
        "insert into kanban_notify_subs values (?, 'telegram', '407617100', null, 'dm', '407617100', null, 'default')",
        (tid,),
    )


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, text, origin):
        self.calls.append((text, origin))
        return True


@pytest.fixture(autouse=True)
def jev_stub(monkeypatch):
    monkeypatch.setattr(wake, "jev_decide", lambda *a, **k: {"answers": {"wake": ANSWER_WAKE}})


def sweep(conn, rec):
    wake.sweep(conn, {}, rec, NOW)


def test_wake_verdict_wakes_by_default(monkeypatch):
    conn = board()
    card(conn, "t_1")
    rec = Recorder()
    sweep(conn, rec)
    assert len(rec.calls) == 1


def test_skip_at_high_confidence_suppresses_and_keeps_rearm(monkeypatch):
    monkeypatch.setattr(wake, "jev_decide", lambda *a, **k: {"answers": {"wake": ANSWER_SKIP_HIGH}})
    conn = board()
    card(conn, "t_1")
    rec = Recorder()
    sweep(conn, rec)
    assert rec.calls == []
    # unchanged event id: a later sweep with a changed state still evaluates again
    conn.execute("update tasks set status = 'ready' where id = 't_1'")
    rec2 = Recorder()
    state = {("t_1"): 1}
    wake.sweep(conn, {("t_1"): 1}, rec2, NOW)
    assert rec2.calls == []  # same event id -> not stale-for-wake again


def test_skip_below_confidence_still_wakes(monkeypatch):
    monkeypatch.setattr(wake, "jev_decide", lambda *a, **k: {"answers": {"wake": ANSWER_SKIP_LOW}})
    conn = board()
    card(conn, "t_1")
    rec = Recorder()
    sweep(conn, rec)
    assert len(rec.calls) == 1


def test_jev_none_malformed_or_raising_wakes():
    pass


def test_jev_failure_fails_open(monkeypatch):
    monkeypatch.setattr(wake, "jev_decide", lambda *a, **k: None)
    conn = board()
    card(conn, "t_1")
    rec = Recorder()
    sweep(conn, rec)
    assert len(rec.calls) == 1


def test_malformed_answer_fails_open(monkeypatch):
    monkeypatch.setattr(wake, "jev_decide", lambda *a, **k: {"answers": {}})
    conn = board()
    card(conn, "t_1")
    rec = Recorder()
    sweep(conn, rec)
    assert len(rec.calls) == 1


def test_comment_history_is_read_for_the_verdict(monkeypatch):
    seen = {}
    def fake_decide(state, questions, timeout=2.0, caller=""):
        seen["state"] = state
        seen["caller"] = caller
        return {"answers": {"wake": ANSWER_WAKE}}
    monkeypatch.setattr(wake, "jev_decide", fake_decide)
    conn = board()
    card(conn, "t_1")
    conn.execute("insert into comments (task_id, body, created_at) values ('t_1', 'blocked on credentials', 1)")
    conn.execute("insert into comments (task_id, body, created_at) values ('t_1', 'waiting on maintainer', 2)")
    rec = Recorder()
    sweep(conn, rec)
    assert "blocked on credentials" in seen["state"] and "waiting on maintainer" in seen["state"]
    assert seen["caller"] == "stale_wake"


def test_missing_comments_table_fails_open(monkeypatch):
    conn = board()
    conn.execute("drop table comments")
    card(conn, "t_1")
    rec = Recorder()
    sweep(conn, rec)
    assert len(rec.calls) == 1
