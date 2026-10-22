import json
import sqlite3
import time
import sys

churn = sys.modules["kanban_guards"].wakes

from kanban_guards import board_watch


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


def test_warns_only_from_10_change_requests_with_last_reasons():
    conn = board()
    card(conn, "t_crs", "review", crs=10, reasons=[f"reason {n}" for n in range(10)])
    card(conn, "t_nine", "review", crs=9, reasons=["r"] * 9)
    card(conn, "t_runs", "ready", runs=45)
    rec = Recorder()
    state = churn.churn_sweep(conn, {}, rec)
    assert woken(rec) == ["t_crs"]
    text, _ = rec.calls[0]
    assert "10 change-requests" in text and "reason 8" in text and "reason 0" not in text
    assert state == {"t_crs": 10}


def test_higher_count_after_a_fire_stays_quiet_and_a_fall_back_rearms():
    conn = board()
    card(conn, "t_crs", "review", crs=10, reasons=["r"] * 10)
    state = churn.churn_sweep(conn, {}, Recorder())
    for n in range(10):
        cr(conn, "t_crs", f"more {n}")
    rec = Recorder()
    assert churn.churn_sweep(conn, state, rec) == state
    assert rec.calls == []
    rows = conn.execute(
        "select rowid from task_events where task_id = 't_crs' and kind = 'changes_requested'"
    ).fetchall()
    for (rowid,) in rows:
        conn.execute("delete from task_events where rowid = ?", (rowid,))
    state = churn.churn_sweep(conn, state, Recorder())
    assert state == {}
    for n in range(10):
        cr(conn, "t_crs", f"again {n}")
    rec2 = Recorder()
    state2 = churn.churn_sweep(conn, state, rec2)
    assert woken(rec2) == ["t_crs"] and state2 == {"t_crs": 10}


def test_done_archived_and_low_churn_cards_are_never_warned():
    conn = board()
    card(conn, "t_done", "done", crs=29)
    card(conn, "t_arch", "archived", crs=29)
    card(conn, "t_norm", "review", crs=3)
    rec = Recorder()
    assert churn.churn_sweep(conn, {}, rec) == {}
    assert rec.calls == []


def test_failed_injection_or_missing_chat_retries_later():
    conn = board()
    card(conn, "t_nochat", "ready", crs=10, subscribed=False)
    card(conn, "t_fail", "ready", crs=10)
    state = churn.churn_sweep(conn, {}, Recorder(ok=False))
    assert state == {}
    rec = Recorder()
    assert churn.churn_sweep(conn, state, rec) == {"t_fail": 10}


def test_multi_origin_cards_are_warned_in_every_subscribed_chat():
    conn = board()
    card(conn, "t_hot", "blocked", crs=10, subscribed=True)
    conn.execute(
        "update kanban_notify_subs set thread_id = null where task_id = 't_hot'"
    )
    conn.execute(
        "insert into kanban_notify_subs values (?, 'telegram', '555000199', '900340', 'dm', '555000100', null, 'default')",
        ("t_hot",),
    )
    rec = Recorder()
    state = churn.churn_sweep(conn, {}, rec)
    assert len(rec.calls) == 2 and {o["chat_id"] for _, o in rec.calls} == {"555000100", "555000199"}
    assert state == {"t_hot": 10}


def test_wake_text_mentions_runs_shape_only_crs():
    card_text = churn.churn_text(
        {"id": "t_t", "status": "review", "title": "t" * 300, "crs": 12},
        ["r1", "r2"],
    )
    assert "12 change-requests" in card_text and "runs" not in card_text.replace("t_t", "")
    assert "split it into smaller cards" in card_text


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
    monkeypatch.setattr(churn, "churn_sweep", lambda *a, **k: calls.append(a) or {"t_x": 10})
    churn.churn_on_tick(board=None)
    assert len(calls) == 1  # no cursor yet: one initialization sweep
    assert json.loads((tmp_path / "kanban-churn-wake.json").read_text()) == {"t_x": 10}
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'heartbeat')")
    conn.commit()
    churn.churn_on_tick(board=None)
    assert len(calls) == 1  # heartbeat is not churn movement: skipped
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'changes_requested')")
    conn.commit()
    churn.churn_on_tick(board=None)
    assert len(calls) == 2
    churn.churn_on_tick(board=None)  # same relevant event again: skipped
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
    monkeypatch.setattr(churn, "churn_sweep", lambda *a, **k: calls.append(a) or {})
    churn.churn_on_tick(board=None)
    assert len(calls) == 1  # no cursor yet: one initialization sweep
    board_watch.write_state(board_watch.POKE_FILE, {"at": time.time() + 10})
    churn.churn_on_tick(board=None)
    assert len(calls) == 2

def test_dumping_sweep_still_advances_the_cursor(monkeypatch, tmp_path):
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "k.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "k.db")
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn.execute("create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer)")
    conn.commit()
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'changes_requested')")
    conn.commit()
    monkeypatch.setattr(churn, "churn_sweep", lambda *a: 1 / 0)
    churn.churn_on_tick(board=None)
    assert json.loads((tmp_path / "kanban-churn-wake-cursor.json").read_text())["event_id"] == 1

def test_dry_run_never_sweeps_and_never_writes(tmp_path, monkeypatch):
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "k.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "k.db")
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn.execute("create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer)")
    conn.commit()
    churn.churn_on_tick(board=None, dry_run=True)
    assert not (tmp_path / "kanban-churn-wake.json").exists() and not (tmp_path / "kanban-churn-wake-cursor.json").exists()

def test_registered_on_dispatch_tick():
    hooks = []
    ctx = type("Ctx", (), {"register_hook": lambda self, name, fn: hooks.append((name, fn))})()
    sys.modules["kanban_guards"].register(ctx)
    assert any(name == "on_kanban_dispatch_tick" and hook.__module__.endswith("wakes") for name, hook in hooks)
