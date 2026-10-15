import json
import sqlite3
import sys

churn = sys.modules["agent_guards"].churn_wake


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
            "insert into kanban_notify_subs values (?, 'telegram', '407617100', '179332', 'dm', '407617100', null, 'default')",
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
        "insert into kanban_notify_subs values (?, 'telegram', '407617199', '179340', 'dm', '407617100', null, 'default')",
        ("t_hot",),
    )
    rec = Recorder()
    state = churn.sweep(conn, {}, rec)
    assert len(rec.calls) == 2 and {o["chat_id"] for _, o in rec.calls} == {"407617100", "407617199"}
    assert state == {"t_hot": 10}


def test_tick_is_rate_limited_and_fails_open(monkeypatch, tmp_path):
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "k.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    sqlite3.connect(tmp_path / "k.db").close()
    monkeypatch.setattr(churn.stale_wake, "_home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(churn, "sweep", lambda *a: calls.append(a) or {"t_x": 20})
    monkeypatch.setattr(churn, "_last_check", 0.0)
    churn.on_tick(board=None)
    churn.on_tick(board=None)
    assert len(calls) == 1
    assert json.loads((tmp_path / "state" / "kanban-churn-wake.json").read_text()) == {"t_x": 20}
    monkeypatch.setattr(churn, "_last_check", 0.0)
    monkeypatch.setattr(churn, "sweep", lambda *a: 1 / 0)
    churn.on_tick(board=None)
    churn.on_tick(board=None, dry_run=True)


def test_registered_on_dispatch_tick():
    hooks = []
    ctx = type("Ctx", (), {"register_hook": lambda self, name, fn: hooks.append((name, fn))})()
    sys.modules["agent_guards"].register(ctx)
    assert ("on_kanban_dispatch_tick", churn.on_tick) in hooks
