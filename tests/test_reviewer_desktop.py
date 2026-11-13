"""Desktop turns use the real owner-bound background review sender, never a chat adapter."""

import importlib
import importlib.util
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["hindsight-reviewer", "jot-reviewer"])
def desktop(request, monkeypatch):
    name = "desktop_test_" + request.param.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, ROOT / request.param / "__init__.py", submodule_search_locations=[str(ROOT / request.param)])
    package = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, package)
    spec.loader.exec_module(package)
    reviewer = importlib.import_module(name + ".reviewer")
    import gateway.session_context as sc
    import hermes_cli.plugin_session as ps
    from gateway.config import Platform
    from gateway.session_authority import LiveSession
    from gateway.session_runtime_workers import track_turn_worker
    from gateway.run_turn_runner import TurnRunner
    sid, key = "local-desktop-fixture", "local:local-desktop-fixture"
    source = SimpleNamespace(platform=Platform.LOCAL, chat_id=sid)
    live = LiveSession(source=source, route=key)
    authority = SimpleNamespace(sessions={sid: live})
    runner = SimpleNamespace(_running_agents={}, session_authority=authority)
    turn = object.__new__(TurnRunner)
    turn._ctx = SimpleNamespace(source=source)
    turn._approval_owner = (authority, sid, 7)
    sender, _release = turn._make_bg_review_callbacks()
    agent = SimpleNamespace(session_id=sid, background_review_callback=sender)
    worker = SimpleNamespace(worker_done=threading.Event())
    track_turn_worker(runner, worker, [agent])
    env = {"HERMES_SESSION_PLATFORM": "local", "HERMES_SESSION_SOURCE": "local", "HERMES_SESSION_ID": sid,
           "HERMES_SESSION_KEY": key, "HERMES_SESSION_CHAT_ID": sid, "HERMES_SESSION_CHAT_TYPE": "dm"}
    monkeypatch.setattr(sc, "get_session_env", lambda name, default="": env.get(name, default))
    monkeypatch.setattr(sc, "bound_session_key", lambda: env["HERMES_SESSION_KEY"])
    monkeypatch.setattr(ps, "session_surface", lambda: "desktop")
    monkeypatch.setitem(reviewer._gateway, "runner", runner)
    return reviewer, env, runner, live, sender, worker, agent


def test_native_desktop_origin_is_admitted(desktop):
    reviewer, env, *_ = desktop
    assert reviewer._origin()["platform"] == "local"


def test_native_desktop_sender_is_the_owner_bound_callback(desktop):
    reviewer, _env, runner, _live, sender, *_ = desktop
    assert not runner._running_agents
    assert reviewer._turn_review_callback() is sender


@pytest.mark.parametrize("flag,value", [("HERMES_CRON_SESSION", "1"), ("HERMES_KANBAN_TASK", "task-42"),
                                        ("HERMES_DELEGATED_CHILD_CONTEXT", "true"), ("HERMES_SINGLE_QUERY_SESSION", "1")])
def test_desktop_unattended_context_cannot_admit_or_deliver(desktop, flag, value):
    reviewer, env, *_ = desktop
    env[flag] = value
    assert reviewer._origin() is None
    assert reviewer._turn_review_callback() is None


@pytest.mark.parametrize("surface", ["cli", "tui", "acp", "api_server", "tool", ""])
def test_non_desktop_local_surface_keeps_existing_denial(desktop, monkeypatch, surface):
    import hermes_cli.plugin_session as ps
    monkeypatch.setattr(ps, "session_surface", lambda: surface)
    reviewer, *_ = desktop
    assert reviewer._origin() is None
    assert reviewer._turn_review_callback() is None


@pytest.mark.parametrize("mismatch", ["session", "route", "chat", "finished", "callback", "authority"])
def test_unbound_or_wrong_desktop_owner_fails_closed(desktop, mismatch):
    reviewer, env, runner, live, _sender, worker, agent = desktop
    if mismatch == "session":
        agent.session_id = "another-session"
    elif mismatch == "route":
        live.route = "another-route"
    elif mismatch == "chat":
        live.source.chat_id = "another-chat"
    elif mismatch == "finished":
        worker.worker_done.set()
    elif mismatch == "callback":
        agent.background_review_callback = None
    else:
        runner.session_authority = None
    assert reviewer._origin() is None
    assert reviewer._turn_review_callback() is None


def test_desktop_report_reaches_real_session_events_without_another_turn(desktop):
    reviewer, _env, _runner, live, *_ = desktop
    sent = []
    original = live.event_stream.publish
    def capture(session_id, payload, **kwargs):
        sent.append((session_id, payload, kwargs))
        return original(session_id, payload, **kwargs)
    live.event_stream.publish = capture
    sender = reviewer._turn_review_callback()
    assert callable(sender)
    sender("Desktop reviewer fixture: 1 verified synthetic change")
    assert len(sent) == 1
    assert sent[0][2]["event_type"] == "review.summary"
    assert sent[0][1]["text"] == "Desktop reviewer fixture: 1 verified synthetic change"


def test_desktop_helper_copies_stay_identical():
    assert (ROOT / "hindsight-reviewer/desktop.py").read_bytes() == (ROOT / "jot-reviewer/desktop.py").read_bytes()


def test_ambiguous_desktop_agent_does_not_admit(desktop):
    reviewer, _env, runner, _live, sender, *_ = desktop
    from gateway.session_runtime_workers import track_turn_worker
    other = SimpleNamespace(session_id="local-desktop-fixture", background_review_callback=sender)
    track_turn_worker(runner, SimpleNamespace(worker_done=threading.Event()), [other])
    assert reviewer._origin() is None
    assert reviewer._turn_review_callback() is None


@pytest.mark.parametrize("source", ["tool", "oneshot", "api_server", "cron", "kanban", "subagent", "webhook", "bot_room"])
def test_desktop_label_does_not_grant_unattended_source_access(desktop, source):
    reviewer, env, *_ = desktop
    env["HERMES_SESSION_SOURCE"] = source
    assert reviewer._origin() is None
    assert reviewer._turn_review_callback() is None
