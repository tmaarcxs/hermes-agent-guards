import json
import sqlite3
import time
import sys

churn = sys.modules["agent_guards"].churn_wake

from agent_guards import board_watch


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


def test_warns_open_card_at_10_runs_or_5_change_requests():
    conn = board()
    card(conn, "t_runs", "ready", runs=10)
    card(conn, "t_crs", "review", crs=5, reasons=[f"reason {n}" for n in range(5)])
    card(conn, "t_ok", "running", runs=6, crs=3)
    rec = Recorder()
    state = churn.sweep(conn, {}, rec)
    assert woken(rec) == ["t_crs", "t_runs"]
    by_id = {text.split()[3]: text for text, _ in rec.calls}
    assert "10 runs" in by_id["t_runs"] and "0 change-requests" in by_id["t_runs"]
    assert "5 change-requests" in by_id["t_crs"] and "reason 2" in by_id["t_crs"] and "reason 0" not in by_id["t_crs"]
    assert "split it into smaller cards" in by_id["t_runs"]
    assert state == {"t_crs": 105, "t_runs": 10}


def test_warns_once_per_threshold_and_again_at_double():
    conn = board()
    card(conn, "t_hot", "running", runs=12)
    rec = Recorder()
    state = churn.sweep(conn, {}, rec)
    assert len(rec.calls) == 1
    for _ in range(7):
        conn.execute("insert into task_runs (task_id) values (?)", ("t_hot",))
    rec2 = Recorder()
    state = churn.sweep(conn, state, rec2)
    assert rec2.calls == []
    for _ in range(1):
        conn.execute("insert into task_runs (task_id) values (?)", ("t_hot",))
    rec3 = Recorder()
    state = churn.sweep(conn, state, rec3)
    assert woken(rec3) == ["t_hot"] and state == {"t_hot": 20}
    text, _ = rec3.calls[0]
    assert "reaches 40" in text


def test_change_request_threshold_doubles_independently():
    conn = board()
    card(conn, "t_hot", "review", crs=5, reasons=["r"] * 5)
    state = churn.sweep(conn, {}, Recorder())
    for n in range(4):
        conn.execute(
            "insert into task_events (task_id, kind, payload) values (?, 'changes_requested', ?)",
            ("t_hot", json.dumps({"reason": f"more {n}"})),
        )
    rec = Recorder()
    state = churn.sweep(conn, state, rec)
    assert rec.calls == []
    conn.execute(
        "insert into task_events (task_id, kind, payload) values (?, 'changes_requested', ?)",
        ("t_hot", json.dumps({"reason": "the tenth"})),
    )
    rec2 = Recorder()
    state = churn.sweep(conn, state, rec2)
    assert woken(rec2) == ["t_hot"] and state == {"t_hot": 110}


def test_older_threshold_is_not_rewarned_when_a_higher_one_fires():
    conn = board()
    card(conn, "t_hot", "running", runs=21)
    state = churn.sweep(conn, {}, Recorder())
    assert state == {"t_hot": 20}
    for _ in range(3):
        conn.execute("delete from task_runs where id = (select max(id) from task_runs where task_id='t_hot')")
    rec = Recorder()
    state = churn.sweep(conn, state, rec)
    assert rec.calls == [] and state == {"t_hot": 20}


def test_done_archived_and_low_churn_cards_are_never_warned():
    conn = board()
    card(conn, "t_done", "done", runs=108)
    card(conn, "t_arch", "archived", crs=29)
    card(conn, "t_norm", "review", runs=3, crs=1)
    rec = Recorder()
    assert churn.sweep(conn, {}, rec) == {}
    assert rec.calls == []


def test_counts_drop_below_threshold_rearm_it():
    conn = board()
    card(conn, "t_hot", "running", runs=10)
    state = churn.sweep(conn, {}, Recorder())
    conn.execute("delete from task_runs where rowid in (select rowid from task_runs where task_id='t_hot' limit 11)")
    card(conn, "t_hot", "running", runs=0, subscribed=False)
    rec = Recorder()
    state = churn.sweep(conn, state, rec)
    assert rec.calls == []
    for _ in range(10):
        conn.execute("insert into task_runs (task_id) values (?)", ("t_hot",))
    rec2 = Recorder()
    state = churn.sweep(conn, state, rec2)
    assert woken(rec2) == ["t_hot"] and state == {"t_hot": 10}


def test_failed_injection_or_missing_chat_retries_later():
    conn = board()
    card(conn, "t_nochat", "ready", runs=10, subscribed=False)
    card(conn, "t_fail", "ready", runs=10)
    state = churn.sweep(conn, {}, Recorder(ok=False))
    assert state == {}
    rec = Recorder()
    assert churn.sweep(conn, state, rec) == {"t_fail": 10}


def test_multi_origin_cards_are_warned_in_every_subscribed_chat():
    conn = board()
    card(conn, "t_hot", "blocked", runs=10, subscribed=True)
    conn.execute(
        "update kanban_notify_subs set thread_id = null where task_id = 't_hot'"
    )
    conn.execute(
        "insert into kanban_notify_subs values (?, 'telegram', '555000199', '900340', 'dm', '555000100', null, 'default')",
        ("t_hot",),
    )
    rec = Recorder()
    state = churn.sweep(conn, {}, rec)
    assert len(rec.calls) == 2 and {o["chat_id"] for _, o in rec.calls} == {"555000100", "555000199"}
    assert state == {"t_hot": 10}


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
    monkeypatch.setattr(churn, "sweep", lambda *a: calls.append(a) or {"t_x": 20})
    churn.on_tick(board=None)
    assert len(calls) == 1  # no cursor yet: one initialization sweep
    assert json.loads((tmp_path / "kanban-churn-wake.json").read_text()) == {"t_x": 20}
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'heartbeat')")
    conn.commit()
    churn.on_tick(board=None)
    assert len(calls) == 1  # heartbeat does not change counts: skipped
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'changes_requested')")
    conn.commit()
    churn.on_tick(board=None)
    assert len(calls) == 2
    churn.on_tick(board=None)  # same relevant event again: skipped
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
    monkeypatch.setattr(churn, "sweep", lambda *a: calls.append(a) or {})
    churn.on_tick(board=None)
    assert len(calls) == 1  # no cursor yet: one initialization sweep
    board_watch.write_state(board_watch.POKE_FILE, {"at": time.time() + 10})
    churn.on_tick(board=None)
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
    monkeypatch.setattr(churn, "sweep", lambda *a: 1 / 0)
    churn.on_tick(board=None)
    assert json.loads((tmp_path / "kanban-churn-wake-cursor.json").read_text())["event_id"] == 1

def test_dry_run_never_sweeps_and_never_writes(monkeypatch, tmp_path):
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "k.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "k.db")
    conn.execute("create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer)")
    conn.commit()
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    churn.on_tick(board=None, dry_run=True)
    assert not (tmp_path / "kanban-churn-wake.json").exists() and not (tmp_path / "kanban-churn-wake-cursor.json").exists()


def test_registered_on_dispatch_tick():
    hooks = []
    ctx = type("Ctx", (), {"register_hook": lambda self, name, fn: hooks.append((name, fn))})()
    sys.modules["agent_guards"].register(ctx)
    assert ("on_kanban_dispatch_tick", churn.on_tick) in hooks
