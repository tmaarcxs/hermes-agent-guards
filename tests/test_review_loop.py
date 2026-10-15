import json
import sqlite3
import time
import sys

loop = sys.modules["agent_guards"].review_loop

from agent_guards import board_watch


def board():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        create table tasks (id text, status text, title text);
        create table task_events (id integer primary key, task_id text, kind text, payload text);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text);
        """
    )
    return conn


def card(conn, tid, status, rounds, subscribed=True):
    conn.execute("insert into tasks values (?, ?, ?)", (tid, status, f"title {tid}"))
    for n in range(rounds):
        changes(conn, tid, f"fix {n + 1}")
    if subscribed:
        conn.execute(
            "insert into kanban_notify_subs values (?, 'telegram', '407617100', '179332', 'dm', '407617100', null, 'default')",
            (tid,),
        )


def changes(conn, tid, reason):
    conn.execute(
        "insert into task_events (task_id, kind, payload) values (?, 'changes_requested', ?)",
        (tid, json.dumps({"reason": reason})),
    )


class Recorder:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, text, origin):
        self.calls.append((text, origin))
        return self.ok


def woken(rec):
    return sorted(text.split()[3] for text, _ in rec.calls)


def test_wakes_open_card_at_third_round_with_latest_reason():
    conn = board()
    card(conn, "t_loop", "ready", 3)
    card(conn, "t_two", "ready", 2)
    card(conn, "t_done", "done", 9)
    rec = Recorder()
    state = loop.sweep(conn, {}, rec)
    assert woken(rec) == ["t_loop"]
    text, origin = rec.calls[0]
    assert "3 review rounds" in text and "fix 3" in text and "reaches 6 rounds" in text
    assert origin["chat_id"] == "407617100" and origin["thread_id"] == "179332"
    assert state == {"t_loop": 3}


def test_wakes_once_per_threshold_and_again_at_next():
    conn = board()
    card(conn, "t_loop", "running", 4)
    rec = Recorder()
    state = loop.sweep(conn, {}, rec)
    assert len(rec.calls) == 1
    changes(conn, "t_loop", "fix 5")
    rec2 = Recorder()
    state = loop.sweep(conn, state, rec2)
    assert rec2.calls == []
    changes(conn, "t_loop", "fix 6")
    rec3 = Recorder()
    state = loop.sweep(conn, state, rec3)
    assert woken(rec3) == ["t_loop"] and state == {"t_loop": 6}


def test_failed_injection_or_missing_chat_retries_later():
    conn = board()
    card(conn, "t_nochat", "review", 3, subscribed=False)
    card(conn, "t_fail", "review", 3)
    state = loop.sweep(conn, {}, Recorder(ok=False))
    assert state == {}
    rec = Recorder()
    assert loop.sweep(conn, state, rec) == {"t_fail": 3}


def test_closed_cards_are_forgotten():
    conn = board()
    card(conn, "t_loop", "ready", 3)
    state = loop.sweep(conn, {}, Recorder())
    conn.execute("update tasks set status = 'done' where id = 't_loop'")
    assert loop.sweep(conn, state, Recorder()) == {}


def test_board_needs_relevant_events(monkeypatch, tmp_path):
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "k.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "k.db")
    conn.execute("create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer)")
    conn.commit()
    calls = []
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    monkeypatch.setattr(loop, "sweep", lambda *a: calls.append(a) or {"t_x": 3})
    loop.on_tick(board=None)
    assert len(calls) == 1  # no cursor yet: one initialization sweep
    assert json.loads((tmp_path / "kanban-review-loop.json").read_text()) == {"t_x": 3}
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'heartbeat')")
    conn.commit()
    loop.on_tick(board=None)
    assert len(calls) == 1  # heartbeat is not a review-round change: skipped
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'changes_requested')")
    conn.commit()
    loop.on_tick(board=None)
    assert len(calls) == 2
    loop.on_tick(board=None)  # same relevant event again: skipped
    assert len(calls) == 2

def test_poke_forces_the_sweep_despite_an_unchanged_cursor(monkeypatch, tmp_path):
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "k.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "k.db")
    conn.execute("create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer)")
    conn.commit()
    calls = []
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    monkeypatch.setattr(loop, "sweep", lambda *a: calls.append(a) or {})
    loop.on_tick(board=None)
    assert len(calls) == 1  # no cursor yet: one initialization sweep
    board_watch.write_state(board_watch.POKE_FILE, {"at": time.time() + 10})
    loop.on_tick(board=None)
    assert len(calls) == 2

def test_dumping_sweep_still_advances_the_cursor(monkeypatch, tmp_path):
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "k.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "k.db")
    conn.execute("create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer)")
    conn.commit()
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'changes_requested')")
    conn.commit()
    monkeypatch.setattr(loop, "sweep", lambda *a: 1 / 0)
    loop.on_tick(board=None)
    assert json.loads((tmp_path / "kanban-review-loop-cursor.json").read_text())["event_id"] == 1

def test_dry_run_never_sweeps_and_never_writes(monkeypatch, tmp_path):
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "k.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "k.db")
    conn.execute("create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer)")
    conn.commit()
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    loop.on_tick(board=None, dry_run=True)
    assert not (tmp_path / "kanban-review-loop.json").exists() and not (tmp_path / "kanban-review-loop-cursor.json").exists()


def test_registered_on_dispatch_tick():
    hooks = []
    ctx = type("Ctx", (), {"register_hook": lambda self, name, fn: hooks.append((name, fn))})()
    sys.modules["agent_guards"].register(ctx)
    assert ("on_kanban_dispatch_tick", loop.on_tick) in hooks
