"""End-to-end tick tests: every wake hook on the real dispatch-tick path.

The review-loop TypeError of 2026-10-19 (``wakes.py:370`` passing the result of
``_file_load()`` where ``loop_sweep`` expects a zero-arg callable) shipped
because every tick-level test in the suite monkeypatched the sweep it was
supposed to exercise, so the real runner never ran. These tests call each
registered ``on_kanban_dispatch_tick`` hook against a real board fixture and
assert the wake reaches the chat and the durable state file, so any signature
mismatch between a hook and its sweep surfaces as a missing injection instead
of a swallowed WARNING.

``stale``, ``waiting`` and ``stall`` already had real-tick tests
(``test_stale_park_wake.py::test_quiet_board_wakes_when_a_ready_card_crosses_five_minutes``,
``test_waiting_wake_run.py`` and ``test_stall_lane_wake.py``); ``loop``,
``churn`` and ``reconcile`` did not.
"""

import json
import sqlite3
import sys
import time
import types

import pytest

wake = sys.modules["agent_guards"].wakes
board_watch = sys.modules["agent_guards"].board_watch


def _board(tmp_path, monkeypatch):
    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "kanban.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "kanban.db")
    conn.executescript(
        """
        create table tasks (id text, status text, title text, block_kind text, created_at integer);
        create table task_links (parent_id text, child_id text);
        create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text,
            last_ping_event_id integer, last_event_id integer);
        """
    )
    conn.execute(
        "insert into kanban_notify_subs values ('t_loop', 'telegram', '555000100', '900332', 'dm',"
        " '555000100', null, 'default', 0, 0)"
    )
    conn.execute(
        "insert into kanban_notify_subs values ('t_churn', 'telegram', '555000100', '900332', 'dm',"
        " '555000100', null, 'default', 0, 0)"
    )
    conn.execute(
        "insert into kanban_notify_subs values ('t_rec', 'telegram', '407617100', '', 'dm',"
        " '407617100', null, 'default', 0, 0)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    return tmp_path


def _recorder(monkeypatch):
    calls = []
    monkeypatch.setattr(wake, "inject", lambda text, origin: calls.append((text, origin)) or True)
    return calls


def test_loop_wake_fires_end_to_end_through_the_tick_hook(tmp_path, monkeypatch):
    """The real runner (_loop_sweep_for) must reach loop_sweep and wake the card."""
    home = _board(tmp_path, monkeypatch)
    calls = _recorder(monkeypatch)
    (home / "kanban-review-loop.json").write_text(json.dumps({"t_old": 3}))
    conn = sqlite3.connect(home / "kanban.db")
    conn.execute("insert into tasks values ('t_loop', 'review', 'looping card', null, 1000)")
    for n in range(3):
        conn.execute(
            "insert into task_events (task_id, kind, payload, created_at) values (?, 'changes_requested', ?, ?)",
            ("t_loop", json.dumps({"reason": f"fix {n + 1}"}), 1000 + n),
        )
    conn.commit()
    conn.close()

    wake.loop_on_tick(board=None)

    assert len(calls) == 1
    assert "[Kanban review loop] t_loop" in calls[0][0]
    assert "3 review rounds" in calls[0][0]
    assert json.loads((home / "kanban-review-loop.json").read_text()) == {"t_loop": 3}

    wake.loop_on_tick(board=None)
    assert len(calls) == 1


def test_loop_wake_is_silent_on_a_quiet_board_but_marks_the_cursor(tmp_path, monkeypatch):
    """The tick still advances its cursor when the sweep has nothing to send."""
    home = _board(tmp_path, monkeypatch)
    calls = _recorder(monkeypatch)
    conn = sqlite3.connect(home / "kanban.db")
    conn.execute("insert into task_events (task_id, kind, payload, created_at) values ('t_x', 'changes_requested', null, 1)")
    conn.commit()
    conn.close()

    wake.loop_on_tick(board=None)

    assert calls == []
    assert json.loads((home / "kanban-review-loop-cursor.json").read_text())["event_id"] == 1


def test_churn_wake_fires_end_to_end_through_the_tick_hook(tmp_path, monkeypatch):
    """The real runner (_churn_sweep_for) must reach churn_sweep and warn."""
    home = _board(tmp_path, monkeypatch)
    calls = _recorder(monkeypatch)
    conn = sqlite3.connect(home / "kanban.db")
    conn.execute("insert into tasks values ('t_churn', 'review', 'grinding card', null, 1000)")
    for n in range(10):
        conn.execute(
            "insert into task_events (task_id, kind, payload, created_at) values (?, 'changes_requested', ?, ?)",
            ("t_churn", json.dumps({"reason": f"reason {n}"}), 1000 + n),
        )
    conn.commit()
    conn.close()

    wake.churn_on_tick(board=None)

    assert len(calls) == 1
    assert "[Kanban churn warning] t_churn" in calls[0][0]
    assert "10 change-requests" in calls[0][0]
    assert json.loads((home / "kanban-churn-wake.json").read_text()) == {"t_churn": 10}


def test_reconcile_wake_fires_end_to_end_through_the_tick_hook(tmp_path, monkeypatch):
    """The real reconcile_on_tick path must reach reconcile_sweep and notify the remembered origin."""
    home = _board(tmp_path, monkeypatch)
    calls = _recorder(monkeypatch)
    monkeypatch.setattr(wake, "_reconcile_state_path", lambda: home / "reconcile.json")
    now = time.time()
    conn = sqlite3.connect(home / "kanban.db")
    conn.execute("insert into tasks values ('t_rec', 'review', 'lost notice', null, 1)")
    event_id = conn.execute(
        "insert into task_events (id, task_id, kind, payload, created_at) values"
        " (7, 't_rec', 'completed', ?, ?)",
        (json.dumps({"summary": "shipped the fix"}), now - 120),
    ).lastrowid
    conn.execute("delete from kanban_notify_subs where task_id = 't_rec'")
    conn.commit()
    conn.close()
    (home / "reconcile.json").write_text(
        json.dumps(
            {
                "t_rec": {
                    "event_id": event_id,
                    "pending": True,
                    "subscribers": [{"platform": "telegram", "chat_id": "407617100", "chat_type": "dm"}],
                }
            }
        )
    )

    wake.reconcile_on_tick(board=None)

    assert len(calls) == 1
    assert "[Kanban unreconciled notice] t_rec" in calls[0][0]
    assert "shipped the fix" in calls[0][0]
    assert calls[0][1]["chat_id"] == "407617100"
    assert not json.loads((home / "reconcile.json").read_text())["t_rec"].get("pending")


@pytest.mark.parametrize("hook_name", ["stale_on_tick", "loop_on_tick", "churn_on_tick"])
def test_every_event_tick_hook_survives_a_real_board(hook_name, tmp_path, monkeypatch):
    """Each registered event tick runs against a real board without raising."""
    home = _board(tmp_path, monkeypatch)
    _recorder(monkeypatch)
    conn = sqlite3.connect(home / "kanban.db")
    conn.execute("insert into task_events (task_id, kind, payload, created_at) values ('t_x', 'changes_requested', null, 1)")
    conn.commit()
    conn.close()

    getattr(wake, hook_name)(board=None)
