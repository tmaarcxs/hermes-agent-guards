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
    assert state == {"t_crs": [0, 105], "t_runs": [10, 0]}


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
    assert woken(rec3) == ["t_hot"] and state == {"t_hot": [20, 0]}
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
    assert woken(rec2) == ["t_hot"] and state == {"t_hot": [0, 110]}


def test_older_threshold_is_not_rewarned_when_a_higher_one_fires():
    conn = board()
    card(conn, "t_hot", "running", runs=21)
    state = churn.sweep(conn, {}, Recorder())
    assert state == {"t_hot": [20, 0]}
    for _ in range(3):
        conn.execute("delete from task_runs where id = (select max(id) from task_runs where task_id='t_hot')")
    rec = Recorder()
    state = churn.sweep(conn, state, rec)
    assert rec.calls == [] and state == {"t_hot": [20, 0]}


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
    assert woken(rec2) == ["t_hot"] and state == {"t_hot": [10, 0]}


def test_failed_injection_or_missing_chat_retries_later():
    conn = board()
    card(conn, "t_nochat", "ready", runs=10, subscribed=False)
    card(conn, "t_fail", "ready", runs=10)
    state = churn.sweep(conn, {}, Recorder(ok=False))
    assert state == {}
    rec = Recorder()
    assert churn.sweep(conn, state, rec) == {"t_fail": [10, 0]}


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
    assert state == {"t_hot": [10, 0]}


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
    monkeypatch.setattr(churn, "sweep", lambda *a: calls.append(a) or {"t_x": [10, 0]})
    churn.on_tick(board=None)
    assert len(calls) == 1  # no cursor yet: one initialization sweep
    assert json.loads((tmp_path / "kanban-churn-wake.json").read_text()) == {"t_x": [10, 0]}
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


def test_hint_names_next_unfired_threshold_after_change_request_fire():
    conn = board()
    card(conn, "t_case", "review", runs=12, crs=6, reasons=["r"] * 6)
    rec = Recorder()
    state = churn.sweep(conn, {}, rec)
    assert state == {"t_case": [10, 105]}
    text, _ = rec.calls[0]
    assert "12 runs" in text
    assert "reaches 20 runs" in text
    for _ in range(8):
        conn.execute("insert into task_runs (task_id) values (?)", ("t_case",))
    rec2 = Recorder()
    state = churn.sweep(conn, state, rec2)
    assert woken(rec2) == ["t_case"] and state == {"t_case": [20, 105]}
    text2, _ = rec2.calls[0]
    assert "reaches 40 runs" in text2


def test_lower_family_warning_still_fires_after_a_change_request_fire():
    conn = board()
    card(conn, "t_case", "review", runs=12, crs=6, reasons=["r"] * 6)
    rec = Recorder()
    state = churn.sweep(conn, {}, rec)
    assert state == {"t_case": [10, 105]}
    for _ in range(8):
        conn.execute("insert into task_runs (task_id) values (?)", ("t_case",))
    rec2 = Recorder()
    state = churn.sweep(conn, state, rec2)
    assert woken(rec2) == ["t_case"] and state == {"t_case": [20, 105]}
    text2, _ = rec2.calls[0]
    assert "20 runs" in text2 and "reaches 40 runs" in text2


def test_family_slots_fire_exactly_once_per_threshold_in_both_crossing_orders():
    """Acceptance 1 over the full input range: run counts 0..45 x change-request counts
    0..25 via stepwise ramps from several start points, both orders. Each threshold
    fires exactly once (no duplicate, no permanently suppressed level): every wake must
    raise one family slot, no wake may fire on a sweep whose slots did not change, and
    the final slots must equal the top crossed level of each family."""
    tops = {"run": [10, 20, 40], "cr": [105, 110, 120]}
    for start_runs, start_crs, order in [
        (0, 0, "runs_first"), (0, 0, "crs_first"),
        (9, 4, "runs_first"), (9, 4, "crs_first"),
        (0, 25, "runs_first"), (45, 0, "crs_first"),
    ]:
        conn = board()
        card(conn, "t_case", "review", runs=start_runs, crs=start_crs)
        rec = Recorder()
        state = churn.sweep(conn, {}, rec)
        run_top = max((s for s in tops["run"] if start_runs >= s), default=0)
        cr_top = max((s for s in (5, 10, 20) if start_crs >= s), default=0)
        if not run_top and not cr_top:
            assert state == {}
        else:
            assert state == {"t_case": [run_top, 100 + cr_top if cr_top else 0]}, (start_runs, start_crs, order, state)
        texts = set()
        r, c = start_runs, start_crs
        expected_run_top = run_top
        expected_cr_top = cr_top
        fires = 0
        for kind, count in (
            ([("r", 45 - start_runs), ("c", 25 - start_crs)] if order == "runs_first" else [("c", 25 - start_crs), ("r", 45 - start_runs)])
        ):
            for _ in range(count):
                if kind == "r":
                    r += 1
                    conn.execute("insert into task_runs (task_id) values (?)", ("t_case",))
                else:
                    c += 1
                    conn.execute(
                        "insert into task_events (task_id, kind, payload) values (?, 'changes_requested', ?)",
                        ("t_case", json.dumps({"reason": "bulk"})),
                    )
                crossed_run_top = max((s for s in tops["run"] if r >= s), default=0)
                crossed_cr_top = max((s for s in (5, 10, 20) if c >= s), default=0)
                expect_awake = (r >= 10 and crossed_run_top > expected_run_top) or (c >= 5 and crossed_cr_top > expected_cr_top)
                before = len(rec.calls)
                state = churn.sweep(conn, state, rec)
                fired_here = len(rec.calls) > before
                assert fired_here == expect_awake, (start_runs, start_crs, order, r, c, fired_here, expect_awake, state)
                if fired_here:
                    fires += 1
                    texts.update(text for text, _ in rec.calls[before:])
                    expected_run_top = max(expected_run_top, crossed_run_top)
                    expected_cr_top = max(expected_cr_top, crossed_cr_top)
                    assert sorted(state.get("t_case", [])) == sorted([expected_run_top, 100 + expected_cr_top if expected_cr_top else 0]), (state, expected_run_top, expected_cr_top)
        assert fires >= 3
        assert len(texts) == len(set(texts))
