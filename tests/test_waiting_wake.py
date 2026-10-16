import json
import sqlite3
import sys
import time

guards = sys.modules["agent_guards"]
wake = guards.waiting_wake
board_watch = guards.board_watch

NOW = 1_000_000.0
HOUR = 3600


def board():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        create table tasks (id text, status text, title text);
        create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text);
        """
    )
    return conn


def card(conn, tid, status="running", title=None, subscribed=True):
    conn.execute("insert into tasks values (?, ?, ?)", (tid, status, title or ("title " + tid)))
    if subscribed:
        conn.execute(
            "insert into kanban_notify_subs values (?, 'telegram', '555000100', null, 'dm', '555000100', null, 'default')",
            (tid,),
        )


def heart(conn, tid, note, age_h=1.0, event_id=None):
    cursor = conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values (?, 'heartbeat', ?, ?)",
        (tid, json.dumps({"note": note}) if note is not None else None, NOW - age_h * HOUR),
    )
    return event_id or cursor.lastrowid


def spawn(conn, tid, age_h=2.0):
    conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values (?, 'spawned', ?, ?)",
        (tid, '{"pid": 1}', NOW - age_h * HOUR),
    )


def other(conn, tid, kind="commented"):
    """A non-heartbeat event that must NOT count toward the wait tally."""
    conn.execute("insert into task_events (task_id, kind, created_at) values (?, ?, ?)", (tid, kind, NOW))


CDTOOL = "Still waiting on CDTool auto-merge of Deploy PR 1086. Watcher continues."
COOLDOWN = "Rate cooldown from sre MCP after metric-name probe retries (~48s pause). Waiting it out; next: gateway."


class Recorder:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, text, origin):
        self.calls.append((text, origin))
        return self.ok


def woken(rec):
    return sorted(text.split()[2] for text, _ in rec.calls)


def waiting(conn):
    return [c["id"] for c in wake.waiting_cards(conn, NOW)]


def test_two_wait_notes_in_a_run_wake_the_card():
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", CDTOOL)
    assert waiting(conn) == []
    heart(conn, "t_w", "Promotion run 12345 still awaiting rollout")
    rec = Recorder()
    state = wake.sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_w"]
    text, origin = rec.calls[0]
    assert "[Kanban waiting]" in text and "2 times" in text and "Promotion run 12345" in text
    assert origin["chat_id"] == "555000100"
    assert state == {"t_w": 1}


def test_wakes_once_per_run_and_rearms_on_a_new_run():
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", CDTOOL)
    heart(conn, "t_w", CDTOOL.replace("1086", "1087"))
    state = wake.sweep(conn, {}, Recorder(), NOW)
    assert wake.sweep(conn, state, Recorder(), NOW) == state
    spawn(conn, "t_w")
    heart(conn, "t_w", CDTOOL.replace("1086", "1088"))
    heart(conn, "t_w", CDTOOL.replace("1086", "1089"))
    rec = Recorder()
    state2 = wake.sweep(conn, state, rec, NOW)
    assert woken(rec) == ["t_w"] and state2["t_w"] > state["t_w"]


def test_boundary_is_the_newest_spawn_or_claim():
    conn = board()
    card(conn, "t_w")
    heart(conn, "t_w", CDTOOL)
    spawn(conn, "t_w")
    heart(conn, "t_w", CDTOOL)
    assert waiting(conn) == []


def test_progress_notes_and_cooldown_notes_never_count():
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    for note in (
        COOLDOWN,
        "Run 3872: 7/12 of batch 1 GLM runs done; matrix continues.",
        "Deploy PR merged; verifying image version on the pod.",
        "Analysis essentially complete: waiting buckets (P2 med 33min), report follows.",
        None,
    ):
        heart(conn, "t_w", note)
    assert waiting(conn) == []


def test_a_wait_on_checks_or_sonar_counts_as_an_external_gate():
    conn = board()
    card(conn, "t_w", title="t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", "PR #1047 open on 323c2193; waiting for Code/Integration + Code/Verify to finish")
    heart(conn, "t_w", "Refactor pushed; waiting for SonarCloud + Code/Verify on new head")
    assert waiting(conn) == ["t_w"]


def test_a_non_heartbeat_event_between_notes_does_not_reset_the_tally():
    conn = board()
    card(conn, "t_w", title="t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", CDTOOL)
    other(conn, "t_w")
    heart(conn, "t_w", "Promotion run 12345 still awaiting rollout")
    assert waiting(conn) == ["t_w"]


def test_only_running_cards_are_watched():
    conn = board()
    card(conn, "t_done", status="done")
    card(conn, "t_ready", status="ready")
    card(conn, "t_blocked", status="blocked")
    for tid in ("t_done", "t_ready", "t_blocked"):
        spawn(conn, tid)
        heart(conn, tid, CDTOOL)
        heart(conn, tid, CDTOOL.replace("1086", "1087"))
    assert waiting(conn) == []


def test_closed_or_unsubscribed_cards_drop_out_and_failed_injection_retries():
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", CDTOOL)
    heart(conn, "t_w", CDTOOL.replace("1086", "1087"))
    state = wake.sweep(conn, {}, Recorder(ok=False), NOW)
    assert state == {}
    rec = Recorder()
    assert wake.sweep(conn, state, rec, NOW) == {"t_w": 1}
    conn.execute("update tasks set status = 'done' where id = 't_w'")
    assert wake.sweep(conn, {"t_w": 1}, Recorder(), NOW) == {}


def test_wait_phrase_without_an_external_actor_nearby_does_not_count():
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", "Waiting for the local test suite to finish rebuilding the venv.")
    heart(conn, "t_w", "Waited out the rate cooldown; resuming the sweep now.")
    assert waiting(conn) == []


def test_heartbeat_without_a_note_is_never_admission():
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", None)
    heart(conn, "t_w", None)
    assert waiting(conn) == []


def test_on_tick_is_unthrottled_ignores_heartbeats_only_in_its_own_state_and_fails_open(monkeypatch, tmp_path):
    fake = type(sys)("hermes_cli")
    fake.kanban_db = type(sys)("kanban_db")
    fake.kanban_db.kanban_db_path = lambda board=None: tmp_path / "kanban.db"
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "kanban.db")
    conn.executescript(
        """
        create table tasks (id text, status text, title text);
        create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text);
        """
    )
    conn.execute("insert into tasks values ('t_w', 'running', 'waiting card')")
    conn.execute(
        "insert into kanban_notify_subs values ('t_w', 'telegram', '555000100', null, 'dm', '555000100', null, 'default')"
    )
    conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values ('t_w', 'spawned', '{\"pid\": 1}', 0)"
    )
    for i in range(2):
        conn.execute(
            "insert into task_events (task_id, kind, payload, created_at) values "
            "('t_w', 'heartbeat', ?, ?)",
            (json.dumps({"note": CDTOOL}), i),
        )
    conn.commit()
    conn.close()

    calls = []
    monkeypatch.setattr(wake.stale_wake, "_inject", lambda text, origin: calls.append((text, origin)) or True)
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    board_watch_state = tmp_path / "kanban-waiting-wake.json"

    wake.on_tick(board=None)
    assert len(calls) == 1 and "[Kanban waiting]" in calls[0][0]
    first = board_watch_state.read_text()
    wake.on_tick(board=None)
    wake.on_tick(board=None, dry_run=True)
    assert len(calls) == 1 and board_watch_state.read_text() == first

    monkeypatch.setattr(wake, "sweep", lambda *a: 1 / 0)
    wake.on_tick(board=None)
    wake.on_tick(board=None, dry_run=True)


def test_registered_on_dispatch_tick():
    hooks = []
    ctx = type("Ctx", (), {"register_hook": lambda self, name, fn: hooks.append((name, fn))})()
    sys.modules["agent_guards"].register(ctx)
    assert ("on_kanban_dispatch_tick", wake.on_tick) in hooks
