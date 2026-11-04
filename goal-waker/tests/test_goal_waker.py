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


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("create table state_meta (key text, value text)")
    c.execute("create table sessions (id text, session_key text, origin_json text)")
    yield c
    c.close()


def add(conn, sid, status="active", wait=None, origin=TG, key=None):
    state = {"status": status, "waiting_on_pid": None, "waiting_on_session": None, "waiting_until": 0.0}
    state.update(wait or {"waiting_until": 123.0})
    conn.execute("insert into state_meta values (?, ?)", (f"goal:{sid}", json.dumps(state)))
    conn.execute("insert into sessions values (?, ?, ?)", (sid, key or f"agent:main:telegram:dm:1:{sid}", origin))


def run(conn, mgr, accept=True):
    sent = []

    def inject(text, key):
        sent.append((text, key))
        return accept

    return waker.sweep(conn, inject, lambda sid: mgr), sent


def test_a_lifted_barrier_on_a_messaging_chat_injects_the_continuation_into_that_chat(conn):
    add(conn, "s1")
    woken, sent = run(conn, FakeManager(barrier_holds=False))
    assert woken == ["s1"]
    assert sent == [("CONTINUE", "agent:main:telegram:dm:1:s1")]


def test_a_barrier_that_still_holds_injects_nothing(conn):
    add(conn, "s1")
    woken, sent = run(conn, FakeManager(barrier_holds=True))
    assert woken == [] and sent == []


def test_a_local_session_is_left_to_its_own_idle_waker(conn):
    add(conn, "s1", origin=LOCAL)
    _, sent = run(conn, FakeManager())
    assert sent == []


def test_a_session_with_no_recorded_route_is_skipped(conn):
    add(conn, "s1", origin=None, key="")
    _, sent = run(conn, FakeManager())
    assert sent == []


def test_a_goal_that_is_not_active_is_never_woken(conn):
    add(conn, "s1", status="paused")
    _, sent = run(conn, FakeManager())
    assert sent == []


def test_an_active_goal_with_no_barrier_is_never_woken(conn):
    add(conn, "s1", wait={"waiting_until": 0.0})
    _, sent = run(conn, FakeManager())
    assert sent == []


def test_a_pid_or_process_session_barrier_counts_as_parked(conn):
    add(conn, "p", wait={"waiting_on_pid": 77})
    add(conn, "q", wait={"waiting_on_session": "proc_1"})
    woken, _ = run(conn, FakeManager())
    assert sorted(woken) == ["p", "q"]


def test_a_refused_injection_reparks_the_goal_instead_of_leaving_it_active_with_no_turn(conn):
    add(conn, "s1")
    mgr = FakeManager()
    woken, sent = run(conn, mgr, accept=False)
    assert woken == [] and len(sent) == 1
    assert mgr.reparked and mgr.reparked[0][0] == waker.RETRY_SECONDS


def test_an_empty_continuation_prompt_injects_nothing(conn):
    add(conn, "s1")
    _, sent = run(conn, FakeManager(prompt=None))
    assert sent == []


def test_a_malformed_goal_row_does_not_stop_the_sweep(conn):
    conn.execute("insert into state_meta values ('goal:bad', 'not json')")
    add(conn, "s1")
    woken, _ = run(conn, FakeManager())
    assert woken == ["s1"]


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
