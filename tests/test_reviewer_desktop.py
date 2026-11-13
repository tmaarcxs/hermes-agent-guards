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


def test_rotated_transcript_keeps_the_stable_desktop_owner(desktop):
    reviewer, env, _runner, _live, sender, _worker, agent = desktop
    env["HERMES_SESSION_ID"] = "rotated-transcript"
    agent.session_id = "rotated-transcript"
    assert reviewer._origin()["platform"] == "local"
    assert reviewer._turn_review_callback() is sender


def test_unwinding_worker_with_the_same_cached_agent_is_not_ambiguous(desktop):
    reviewer, _env, runner, _live, sender, _worker, agent = desktop
    from gateway.session_runtime_workers import track_turn_worker
    track_turn_worker(runner, SimpleNamespace(worker_done=threading.Event()), [agent])
    assert reviewer._origin()["platform"] == "local"
    assert reviewer._turn_review_callback() is sender


def test_empty_agent_holder_stays_idle(desktop):
    reviewer, _env, runner, *_ = desktop
    for _worker, holder in runner.session_authority._turn_workers.values():
        holder[0] = None
    assert reviewer._origin() is None
    assert reviewer._turn_review_callback() is None


def test_local_job_captures_the_validated_sender_once(desktop, monkeypatch):
    reviewer, env, _runner, _live, sender, *_ = desktop
    resolver = importlib.import_module(reviewer.__package__ + ".desktop")
    lookups, jobs = [], []
    def resolve(_runner):
        lookups.append(1)
        return sender if len(lookups) == 1 else None
    monkeypatch.setattr(resolver, "turn_sender", resolve)
    monkeypatch.setattr(reviewer, "_run", jobs.append)
    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(reviewer.threading, "Thread", InlineThread)
    if hasattr(reviewer, "_jot_available"):
        monkeypatch.setattr(reviewer, "_jot_available", lambda: True)
    else:
        reviewer._tool_recalls[env["HERMES_SESSION_ID"]] = {"bank": "synthetic", "records": [
            {"id": "11111111-1111-1111-1111-111111111111", "text": "Synthetic port is 5432."}]}
    reviewer.on_post_llm_call(session_id=env["HERMES_SESSION_ID"], turn_id="synthetic-turn",
        user_message="Synthetic port is 6543.", assistant_response="Acknowledged.",
        conversation_history=[{"role": "user", "content": "Synthetic port is 6543."}])
    assert len(jobs) == 1
    assert jobs[0]["deliver"] is sender
    assert len(lookups) == 1
    assert "_desktop_sender" not in jobs[0].get("origin", {})
