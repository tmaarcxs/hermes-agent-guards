"""Behaviour of the recall gates: the per-turn clarify block and the periodic nudge.

The real store is never touched: every test points ``HERMES_HOME`` at a temporary
home whose ``state.db`` carries the one table the guard reads.
"""

import sqlite3
import time

import pytest

from pathlib import Path

from recall_guards import recall_guard


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    recall_guard.reset()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield
    recall_guard.reset()


def make_db(tmp_path: Path, rows) -> Path:
    """A minimal ``state.db`` with the ``messages`` columns the guard queries."""
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE messages (session_id TEXT, role TEXT, tool_name TEXT, timestamp REAL)"
    )
    connection.executemany(
        "INSERT INTO messages (session_id, role, tool_name, timestamp) VALUES (?, 'tool', ?, ?)", rows
    )
    connection.commit()
    connection.close()
    return path



def test_a_read_in_this_turn_allows_clarify(tmp_path):
    recall_guard.record(tool_name="hindsight_recall", session_id="s1", turn_id="t1")
    assert recall_guard.clarify_block("s1", "t1", "clarify") is None


def test_a_read_in_an_earlier_turn_does_not_allow_clarify(tmp_path):
    recall_guard.record(tool_name="hindsight_recall", session_id="s1", turn_id="t1")
    assert recall_guard.clarify_block("s1", "t2", "clarify") is not None


def test_a_read_in_another_session_does_not_allow_clarify(tmp_path):
    recall_guard.record(tool_name="hindsight_recall", session_id="other", turn_id="t1")
    assert recall_guard.clarify_block("s1", "t1", "clarify") is not None


def test_a_non_memory_tool_does_not_allow_clarify(tmp_path):
    recall_guard.record(tool_name="read_file", session_id="s1", turn_id="t1")
    assert recall_guard.clarify_block("s1", "t1", "clarify") is not None


def test_every_memory_tool_counts(tmp_path):
    for tool in ("hindsight_recall", "hindsight_reflect", "hindsight_retain"):
        recall_guard.reset()
        recall_guard.record(tool_name=tool, session_id="s1", turn_id="t1")
        assert recall_guard.clarify_block("s1", "t1", "clarify") is None, tool


def test_record_ignores_calls_without_identity(tmp_path):
    recall_guard.record(tool_name="hindsight_recall", session_id="", turn_id="t1")
    recall_guard.record(tool_name="hindsight_recall", session_id="s1", turn_id="")
    assert recall_guard.clarify_block("s1", "t1", "clarify") is not None


def test_a_non_clarify_tool_is_never_blocked(tmp_path):
    assert recall_guard.clarify_block("s1", "t1", "kanban_create") is None
    assert recall_guard.clarify_block("s1", "t1", "terminal") is None



def test_a_read_seconds_ago_counts_when_the_turn_record_was_lost(tmp_path):
    make_db(tmp_path, [("s1", "hindsight_recall", time.time() - 5)])
    assert recall_guard.clarify_block("s1", "t1", "clarify") is None


def test_a_read_a_minute_ago_does_not_count(tmp_path):
    make_db(tmp_path, [("s1", "hindsight_recall", time.time() - recall_guard.FALLBACK_WINDOW_SECONDS - 5)])
    assert recall_guard.clarify_block("s1", "t1", "clarify") is not None


def test_the_fallback_does_not_widen_to_the_session(tmp_path):
    """A read 20 minutes ago is stale: the gate must still fire."""
    make_db(tmp_path, [("s1", "hindsight_recall", time.time() - 1200)])
    assert recall_guard.clarify_block("s1", "t1", "clarify") is not None


def test_missing_database_fails_open(tmp_path):
    assert recall_guard.clarify_block("s1", "t1", "clarify") is not None  # blocks, never crashes
    assert recall_guard.recent_read("s1") is None


def test_unreadable_schema_fails_open(tmp_path):
    (tmp_path / "state.db").write_text("not a database")
    assert recall_guard.recent_read("s1") is None


def test_cache_avoids_a_second_query(monkeypatch, tmp_path):
    make_db(tmp_path, [])
    calls = []
    real = recall_guard._db_memory_read
    monkeypatch.setattr(
        recall_guard, "_db_memory_read", lambda sid: (calls.append(sid), real(sid))[1]
    )
    recall_guard.recent_read("s1")
    recall_guard.recent_read("s1")
    assert calls == ["s1"]



def test_check_ignores_worker_processes(monkeypatch, tmp_path):
    monkeypatch.setattr(recall_guard.policy, "is_lead", lambda: False)
    assert recall_guard.check("clarify", {}, session_id="s1", turn_id="t1") is None


def test_check_blocks_clarify_on_a_lead(monkeypatch, tmp_path):
    monkeypatch.setattr(recall_guard.policy, "is_lead", lambda: True)
    result = recall_guard.check("clarify", {}, session_id="s1", turn_id="t1")
    assert result and result["action"] == "block"
    assert "hindsight_recall" in result["message"]


def test_check_allows_clarify_when_this_turn_read_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(recall_guard.policy, "is_lead", lambda: True)
    recall_guard.record(tool_name="hindsight_recall", session_id="s1", turn_id="t1")
    assert recall_guard.check("clarify", {}, session_id="s1", turn_id="t1") is None



def test_nudge_fires_on_the_fifth_turn_only(monkeypatch):
    monkeypatch.setattr(recall_guard.policy, "is_lead", lambda: True)
    for turn in range(1, 6):
        text = recall_guard.transform(tool_name="read_file", result="ok", session_id="s", turn_id=f"t{turn}")
        if turn == 5:
            assert text and "hindsight_recall" in text
        else:
            assert text is None


def test_nudge_fires_once_per_qualifying_turn(monkeypatch):
    monkeypatch.setattr(recall_guard.policy, "is_lead", lambda: True)
    for turn in range(1, 6):
        recall_guard.transform(tool_name="read_file", result="ok", session_id="s", turn_id=f"t{turn}")
    assert recall_guard.transform(tool_name="read_file", result="ok", session_id="s", turn_id="t5") is None


def test_nudge_ignores_non_string_results(monkeypatch):
    monkeypatch.setattr(recall_guard.policy, "is_lead", lambda: True)
    for turn in range(1, 6):
        recall_guard.transform(tool_name="read_file", result=None, session_id="s", turn_id=f"t{turn}")
    assert recall_guard.transform(tool_name="read_file", result={"a": 1}, session_id="s", turn_id="t6") is None


def test_nudge_is_per_session(monkeypatch):
    monkeypatch.setattr(recall_guard.policy, "is_lead", lambda: True)
    for turn in range(1, 5):
        recall_guard.transform(tool_name="read_file", result="ok", session_id="a", turn_id=f"t{turn}")
    assert recall_guard.transform(tool_name="read_file", result="ok", session_id="b", turn_id="t1") is None
    for turn in range(2, 5):
        recall_guard.transform(tool_name="read_file", result="ok", session_id="b", turn_id=f"t{turn}")
    assert recall_guard.transform(tool_name="read_file", result="ok", session_id="b", turn_id="t5") is not None
