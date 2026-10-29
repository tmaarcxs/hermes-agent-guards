"""round_cap_gate: a card cannot be reopened forever.

Measured on the real board (30 days): 244 cards took at least one changes_requested
round, 93 took 2+, 16 took 4+, and one card burned 108 runs / 27.8 h across 29 rounds.
The rounds converge (17 deep cards ask for less each round) — what was missing was a
ceiling, so this gate adds one and hands the decision back to the lead.

These tests drive the gate against a throwaway board, so the real board is never read.
"""

import os
import sqlite3
import sys
import time

import pytest

guards = sys.modules["kanban_guards"]
gate = guards.round_cap_gate


def _board(tmp_path, task_id="t_probe", assignee="worker", rounds=0, comments=()):
    """Create a minimal board holding one card and its runs/comments."""
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, status TEXT);
        CREATE TABLE task_runs (task_id TEXT, outcome TEXT, started_at INTEGER, ended_at INTEGER);
        CREATE TABLE task_comments (task_id TEXT, author TEXT, body TEXT, created_at INTEGER);
        """
    )
    conn.execute("INSERT INTO tasks VALUES (?,?,?)", (task_id, assignee, "review"))
    now = int(time.time())
    for i in range(rounds):
        conn.execute("INSERT INTO task_runs VALUES (?,?,?,?)", (task_id, "changes_requested", now - 100 + i, now - 90 + i))
    for author, body, age in comments:
        conn.execute("INSERT INTO task_comments VALUES (?,?,?,?)", (task_id, author, body, now - age))
    conn.commit()
    conn.close()
    return db


@pytest.fixture(autouse=True)
def _gate_active(monkeypatch):
    """The gate only runs for a worker on a card, in a configured-worker setup."""
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_probe")
    monkeypatch.delenv("AGENT_GUARDS_MAX_REVIEW_ROUNDS", raising=False)
    gate.reset()
    yield
    gate.reset()


def _patch_board(monkeypatch, db):
    monkeypatch.setattr(gate, "_board_path", lambda: db)


def test_under_ceiling_passes(monkeypatch, tmp_path):
    _patch_board(monkeypatch, _board(tmp_path, rounds=3))
    assert gate.check("kanban_request_review", {"summary": "s"}) is None


def test_at_ceiling_blocks(monkeypatch, tmp_path):
    _patch_board(monkeypatch, _board(tmp_path, rounds=4))
    message = gate.check("kanban_request_review", {"summary": "s"})
    assert message is not None
    assert "4 rounds" in message
    assert "escalate to the lead" in message
    assert gate.OVERRIDE_TOKEN in message


def test_over_ceiling_blocks(monkeypatch, tmp_path):
    _patch_board(monkeypatch, _board(tmp_path, rounds=29))
    assert gate.check("kanban_request_review", {"summary": "s"}) is not None


def test_other_tools_are_untouched(monkeypatch, tmp_path):
    _patch_board(monkeypatch, _board(tmp_path, rounds=29))
    for tool in ("kanban_complete", "kanban_comment", "kanban_block", "terminal", "kanban_request_changes"):
        assert gate.check(tool, {}) is None, tool


def test_lead_override_lifts_the_ceiling(monkeypatch, tmp_path):
    """A non-assignee comment with the token, after the last round, allows one more pass."""
    db = _board(tmp_path, rounds=6, comments=[("default", "please do one more pass", 5)])
    _patch_board(monkeypatch, db)
    assert gate.check("kanban_request_review", {"summary": "s"}) is not None  # no token yet

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO task_comments VALUES (?,?,?,?)",
        ("t_probe", "default", f"accepted, {gate.OVERRIDE_TOKEN}: this one really needs it", int(time.time())),
    )
    conn.commit()
    conn.close()
    assert gate.check("kanban_request_review", {"summary": "s"}) is None


def test_implementer_cannot_lift_its_own_ceiling(monkeypatch, tmp_path):
    db = _board(tmp_path, rounds=6, comments=[("worker", f"self {gate.OVERRIDE_TOKEN}", 5)])
    _patch_board(monkeypatch, db)
    assert gate.check("kanban_request_review", {"summary": "s"}) is not None


def test_stale_override_before_the_last_round_does_not_count(monkeypatch, tmp_path):
    """An old override is consumed by the round that followed it."""
    db = _board(tmp_path, rounds=6, comments=[("default", f"old {gate.OVERRIDE_TOKEN}", 10_000)])
    _patch_board(monkeypatch, db)
    assert gate.check("kanban_request_review", {"summary": "s"}) is not None


def test_threshold_is_configurable(monkeypatch, tmp_path):
    _patch_board(monkeypatch, _board(tmp_path, rounds=2))
    assert gate.check("kanban_request_review", {"summary": "s"}) is None
    monkeypatch.setenv("AGENT_GUARDS_MAX_REVIEW_ROUNDS", "2")
    assert gate.check("kanban_request_review", {"summary": "s"}) is not None


def test_zero_disables_the_gate(monkeypatch, tmp_path):
    _patch_board(monkeypatch, _board(tmp_path, rounds=29))
    monkeypatch.setenv("AGENT_GUARDS_MAX_REVIEW_ROUNDS", "0")
    assert gate.check("kanban_request_review", {"summary": "s"}) is None


def test_garbage_threshold_falls_back_to_default(monkeypatch, tmp_path):
    _patch_board(monkeypatch, _board(tmp_path, rounds=4))
    monkeypatch.setenv("AGENT_GUARDS_MAX_REVIEW_ROUNDS", "not-a-number")
    assert gate.check("kanban_request_review", {"summary": "s"}) is not None


def test_lead_chat_is_not_gated(monkeypatch, tmp_path):
    _patch_board(monkeypatch, _board(tmp_path, rounds=29))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert gate.check("kanban_request_review", {"summary": "s"}) is None


def test_inactive_without_a_configured_worker(monkeypatch, tmp_path):
    _patch_board(monkeypatch, _board(tmp_path, rounds=29))
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert gate.check("kanban_request_review", {"summary": "s"}) is None


def test_missing_board_fails_open(monkeypatch, tmp_path):
    _patch_board(monkeypatch, tmp_path / "nope.db")
    assert gate.check("kanban_request_review", {"summary": "s"}) is None


def test_board_read_error_fails_open(monkeypatch, tmp_path):
    _patch_board(monkeypatch, _board(tmp_path, rounds=4))
    monkeypatch.setattr(gate, "_rounds_and_escape", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert gate.check("kanban_request_review", {"summary": "s"}) is None


def test_ceiling_counts_only_changes_requested(monkeypatch, tmp_path):
    """Crashes, timeouts and review handoffs are not rounds."""
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, status TEXT);
        CREATE TABLE task_runs (task_id TEXT, outcome TEXT, started_at INTEGER, ended_at INTEGER);
        CREATE TABLE task_comments (task_id TEXT, author TEXT, body TEXT, created_at INTEGER);
        """
    )
    conn.execute("INSERT INTO tasks VALUES ('t_probe','worker','review')")
    now = int(time.time())
    for outcome in ("crashed", "timed_out", "review_requested", "completed", "blocked", "reclaimed"):
        for i in range(10):
            conn.execute("INSERT INTO task_runs VALUES (?,?,?,?)", ("t_probe", outcome, now - i, now - i + 1))
    conn.commit()
    conn.close()
    _patch_board(monkeypatch, db)
    assert gate.check("kanban_request_review", {"summary": "s"}) is None
