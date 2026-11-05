"""The goal waker resumes a parked goal exactly when its barrier lifted, on messaging chats only.

Self-contained on purpose: a ``tests/conftest.py`` here would be a second top-level ``conftest``
module and shadow the one ``kanban-guards/tests/test_register_contract.py`` imports by hand.
"""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[1]
if "goal_waker" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "goal_waker", _root / "__init__.py", submodule_search_locations=[str(_root)]
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["goal_waker"] = _module
    _spec.loader.exec_module(_module)
waker = importlib.import_module("goal_waker.waker")

TG = json.dumps({"platform": "telegram", "chat_id": "1"})
NOW = 1_000_000_000.0
LOCAL = json.dumps({"platform": "local", "chat_id": "x"})


class FakeManager:
    def __init__(self, barrier_holds=False, prompt="CONTINUE"):
        self.barrier_holds = barrier_holds
        self.prompt = prompt
        self.reparked = []

    def is_waiting(self):
        return self.barrier_holds

    def next_continuation_prompt(self):
        return self.prompt

    def wait_for_seconds(self, seconds, reason=""):
        self.reparked.append((seconds, reason))


def add(conn, sid, status="active", wait=None, origin=TG, key=None, idle=0.0):
    state = {"status": status, "waiting_on_pid": None, "waiting_on_session": None, "waiting_until": 0.0}
    state.update(wait or {"waiting_until": 123.0})
    conn.execute("insert into state_meta values (?, ?)", (f"goal:{sid}", json.dumps(state)))
    conn.execute(
        "insert into sessions values (?, ?, ?, ?)", (sid, key or f"agent:main:telegram:dm:1:{sid}", origin, NOW - idle)
    )


def fresh_conn(*rows):
    """A sweep-local in-memory db with sessions ``rows`` already inserted.

    Each row is a ``dict`` of ``add()`` arguments.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("create table state_meta (key text, value text)")
    conn.execute("create table sessions (id text, session_key text, origin_json text, last_activity_at real)")
    for row in rows:
        add(conn, **row)
    return conn


def run(conn, mgrs, accept=True, routes=None):
    """One sweep call with one ``FakeManager`` per session (default: fresh ones).

    ``routes`` pins the sweep-local routes map when a test simulates a session
    already woken once; a fresh sweep starts from an empty map.
    """
    sent = []

    def inject(text, key):
        sent.append((text, key))
        return accept

    if routes is None:
        routes = {}
    managers = dict(mgrs)
    woken = list(
        waker.sweep(conn, inject, lambda sid: managers.setdefault(sid, FakeManager()), now=NOW, routes=routes)
    )
    return woken, sent, routes


def test_a_lifted_barrier_on_a_messaging_chat_injects_the_continuation_into_that_chat():
    conn = fresh_conn({"sid": "s1"})
    woken, sent, _ = run(conn, {"s1": FakeManager(barrier_holds=False)})
    assert woken == ["s1"]
    assert sent == [("CONTINUE", "agent:main:telegram:dm:1:s1")]


def test_a_barrier_that_still_holds_injects_nothing():
    conn = fresh_conn({"sid": "s1"})
    woken, sent = run(conn, {"s1": FakeManager(barrier_holds=True)})[:2]
    assert woken == [] and sent == []


def test_a_local_session_is_left_to_its_own_idle_waker():
    conn = fresh_conn({"sid": "s1o", "origin": LOCAL})
    _, sent = run(conn, {})[:2]
    assert sent == []


def test_a_session_with_no_recorded_route_is_skipped():
    conn = fresh_conn({"sid": "s1n", "origin": None, "key": ""})
    _, sent = run(conn, {})[:2]
    assert sent == []


def test_a_session_idle_for_days_is_not_woken_because_its_route_is_gone():
    conn = fresh_conn()
    add(conn, "s1", idle=waker.MAX_IDLE + 1)
    _, sent = run(conn, {})[:2]
    assert sent == []


def test_a_goal_that_is_not_active_is_never_woken():
    conn = fresh_conn({"sid": "s1", "status": "paused"})
    _, sent = run(conn, {})[:2]
    assert sent == []


def test_an_active_goal_with_no_barrier_is_never_woken():
    conn = fresh_conn({"sid": "s1", "wait": {"waiting_until": 0.0}})
    _, sent = run(conn, {})[:2]
    assert sent == []


def test_a_pid_or_process_session_barrier_counts_as_parked():
    conn = fresh_conn({"sid": "p", "wait": {"waiting_on_pid": 77}}, {"sid": "q", "wait": {"waiting_on_session": "proc_1"}})
    woken, _sent, _routes = run(conn, {})
    assert sorted(woken) == ["p", "q"]


def test_a_refused_injection_reparks_the_goal_instead_of_leaving_it_active_with_no_turn():
    conn = fresh_conn({"sid": "s1"})
    mgr = FakeManager()
    refused = run(conn, {"s1": mgr}, accept=False)[1]
    assert refused and len(refused) == 1
    assert mgr.reparked and mgr.reparked[0][0] == waker.RETRY_SECONDS


def test_an_empty_continuation_prompt_injects_nothing():
    conn = fresh_conn({"sid": "s1"})
    _, sent = run(conn, {"s1": FakeManager(prompt=None)})[:2]
    assert sent == []


def test_a_malformed_goal_row_does_not_stop_the_sweep():
    conn = fresh_conn()
    conn.execute("insert into state_meta values ('goal:bad', 'not json')")
    add(conn, "s1")
    woken, _sent, _routes = run(conn, {})
    assert woken == ["s1"]


def test_a_session_woken_once_is_skipped_while_its_goal_row_has_not_advanced():
    conn = fresh_conn({"sid": "s1"})
    routes = {"s1": NOW - 60.0}
    conn.execute("update sessions set last_activity_at = ? where id = 's1'", (NOW - 60.0,))
    _woken, sent, routes = run(conn, {}, routes=routes)
    assert sent == []
    assert routes["s1"] == NOW - 60.0


def test_a_session_woken_once_wakes_again_once_its_goal_row_has_advanced():
    conn = fresh_conn({"sid": "s1"})
    routes = {"s1": NOW - 60.0}
    conn.execute("update sessions set last_activity_at = ? where id = 's1'", (NOW - 30.0,))
    woken, sent, routes = run(conn, {}, routes=routes)
    assert sent and routes["s1"] == NOW - 30.0
    assert routes["s1"] > NOW - 60.0
    conn2 = fresh_conn({"sid": "s1", "idle": 30.0})
    _woken2, sent2, routes2 = run(conn2, {}, routes=routes)
    assert sent2 == []
    assert routes2["s1"] == NOW - 30.0


def test_a_refused_injection_leaves_no_debounce_entry_and_reparks_the_goal():
    conn = fresh_conn({"sid": "s1"})
    mgr = FakeManager()
    attempts, routes = run(conn, {"s1": mgr}, accept=False)[1:]
    assert len(attempts) == 1
    assert routes == {}
    assert mgr.reparked and mgr.reparked[0][0] == waker.RETRY_SECONDS


def test_an_internal_error_is_swallowed_and_logged_the_fail_open_pin(monkeypatch):
    """A sweep failure must never raise out of the tick, and never stops later ticks."""
    failures = []
    monkeypatch.setattr(waker, "CTX", type("Ctx", (), {})())
    monkeypatch.setattr(waker, "_home", lambda: (_ for _ in ()).throw(RuntimeError("stop")))
    monkeypatch.setattr(
        waker.logger, "warning", lambda *a, **k: failures.append(1)
    )
    waker._state["last"] = 0.0
    waker.on_tick()
    assert failures == [1]
    waker._state["last"] = 0.0
    waker.on_tick()
    assert failures == [1, 1]


def test_register_wires_the_dispatcher_tick_hook():
    import goal_waker

    hooks = []

    class Ctx:
        def register_hook(self, name, fn):
            hooks.append((name, fn))

    goal_waker.register(Ctx())
    assert hooks == [("on_kanban_dispatch_tick", waker.on_tick)]


def test_the_tick_is_throttled_and_skips_dry_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(waker, "_home", lambda: (_ for _ in ()).throw(RuntimeError("stop")))
    monkeypatch.setattr(waker.logger, "warning", lambda *a, **k: calls.append(a))
    waker._state["last"] = 0.0
    waker.on_tick(dry_run=True)
    assert calls == []
    waker.on_tick()
    waker.on_tick()
    assert len(calls) == 1
