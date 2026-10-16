import json
import sqlite3
import sys
import time

guards = sys.modules["agent_guards"]
wake = guards.waiting_wake
board_watch = guards.board_watch

NOW = 1_000_000.0
HOUR = 3600
MIN = 60
WAIT_AFTER = wake.WAIT_AFTER


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


def heart(conn, tid, note, age_s=0.0, event_id=None):
    cursor = conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values (?, 'heartbeat', ?, ?)",
        (tid, json.dumps({"note": note}) if note is not None else None, NOW - age_s),
    )
    return event_id or cursor.lastrowid


def spawn(conn, tid, age_s=1800.0):
    conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values (?, 'spawned', ?, ?)",
        (tid, '{"pid": 1}', NOW - age_s),
    )


def other(conn, tid, kind="commented"):
    """A non-heartbeat event that must not advance or reset the wait clock."""
    conn.execute("insert into task_events (task_id, kind, created_at) values (?, ?, ?)", (tid, kind, NOW))


INCIDENT = (
    "Still waiting on CDTool auto-merge of Deploy PR 1086 (open since 10:11Z, mergeStateStatus CLEAN, "
    "no checks). CDTool typically takes ~10-25min in observed runs; watcher continues."
)
COOLDOWN = "Rate cooldown from sre MCP after metric-name probe retries (~48s pause). Waiting it out; next: gateway."


class Recorder:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, text, origin):
        self.calls.append((text, origin))
        return self.ok


def woken(rec):
    return sorted(text.split()[2] for text, _ in rec.calls)


def waiting(conn, now=NOW):
    return [c["id"] for c in wake.waiting_cards(conn, now)]


def a_run_with_one_wait_note(conn, tid="t_w", first_age_s=1000.0):
    card(conn, tid)
    spawn(conn, tid)
    heart(conn, tid, INCIDENT, age_s=first_age_s)


def test_a_wait_note_sixteen_minutes_old_wakes_the_card_exactly_once_with_the_incident_text():
    conn = board()
    a_run_with_one_wait_note(conn, first_age_s=16 * MIN)
    rec = Recorder()
    state = wake.sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_w"]
    text, origin = rec.calls[0]
    assert "[Kanban waiting]" in text and "t_w" in text
    assert "16 min" in text and "CDTool auto-merge of Deploy PR 1086" in text
    assert "Do not just wait longer" in text
    assert origin == {
        "platform": "telegram", "chat_id": "555000100", "chat_type": "dm",
        "user_id": "555000100", "profile": "default",
    }
    assert state == {"t_w": 1}
    assert wake.sweep(conn, state, Recorder(), NOW) == state
    assert wake.sweep(conn, state, Recorder(), NOW + 5 * HOUR) == state


def test_a_wait_note_fourteen_minutes_old_does_not_wake_yet():
    conn = board()
    a_run_with_one_wait_note(conn, first_age_s=14 * MIN)
    rec = Recorder()
    assert wake.sweep(conn, {}, rec, NOW) == {}
    assert rec.calls == []
    state = wake.sweep(conn, {}, Recorder(), NOW + 2 * MIN)
    assert state == {"t_w": 1}


def test_wakes_again_only_for_a_new_run():
    conn = board()
    a_run_with_one_wait_note(conn)
    state = wake.sweep(conn, {}, Recorder(), NOW)
    spawn(conn, "t_w")
    rec = Recorder()
    state2 = wake.sweep(conn, state, rec, NOW)
    assert rec.calls == [] and state2 == {}
    heart(conn, "t_w", INCIDENT, age_s=WAIT_AFTER + 60)
    heart(conn, "t_w", INCIDENT, age_s=30)
    rec2 = Recorder()
    state3 = wake.sweep(conn, state2, rec2, NOW)
    assert woken(rec2) == ["t_w"] and state3["t_w"] > state["t_w"]


def test_boundary_is_the_newest_spawn_or_claim():
    conn = board()
    card(conn, "t_w")
    heart(conn, "t_w", INCIDENT, age_s=WAIT_AFTER + 900)
    spawn(conn, "t_w")
    heart(conn, "t_w", INCIDENT, age_s=10)
    assert waiting(conn) == []


def test_claim_boundary_counts_like_spawn():
    conn = board()
    card(conn, "t_w")
    heart(conn, "t_w", INCIDENT, age_s=WAIT_AFTER + 900)
    conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values ('t_w', 'claimed', '{}', ?)",
        (NOW - 10,),
    )
    heart(conn, "t_w", INCIDENT, age_s=5)
    assert waiting(conn) == []


def test_plain_progress_heartbeats_never_wake_no_matter_how_old():
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    for note in (
        "running tests 12/40",
        "pushed branch feature/x",
        COOLDOWN,
        "Deploy PR merged; verifying image version on the pod.",
        "Analysis essentially complete: waiting buckets (P2 med 33min), report follows.",
        None,
    ):
        heart(conn, "t_w", note, age_s=WAIT_AFTER + 600)
    assert waiting(conn) == []


def test_a_wait_on_checks_or_sonar_counts_as_an_external_gate():
    conn = board()
    card(conn, "t_w", title="t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", "PR #1047 open on 323c2193; waiting for Code/Integration + Code/Verify to finish",
          age_s=WAIT_AFTER)
    assert waiting(conn) == ["t_w"]


def test_a_non_heartbeat_event_does_not_advance_or_reset_the_wait_clock():
    conn = board()
    card(conn, "t_w", title="t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", INCIDENT, age_s=WAIT_AFTER + 60)
    other(conn, "t_w")
    other(conn, "t_w", "status")
    assert waiting(conn) == ["t_w"]


def test_only_running_cards_are_watched():
    conn = board()
    card(conn, "t_done", status="done")
    card(conn, "t_ready", status="ready")
    card(conn, "t_blocked", status="blocked")
    for tid in ("t_done", "t_ready", "t_blocked"):
        spawn(conn, tid)
        heart(conn, tid, INCIDENT, age_s=WAIT_AFTER + 600)
    assert waiting(conn) == []


def test_no_subscriber_or_failed_injection_retries_and_a_closed_card_drops_out():
    conn = board()
    a_run_with_one_wait_note(conn)
    state = wake.sweep(conn, {}, Recorder(ok=False), NOW)
    assert state == {}
    rec = Recorder()
    assert wake.sweep(conn, state, rec, NOW) == {"t_w": 1}
    conn.execute("update tasks set status = 'done' where id = 't_w'")
    assert wake.sweep(conn, {"t_w": 1}, Recorder(), NOW) == {}


def test_a_wait_phrase_without_an_external_actor_after_it_never_counts():
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", "Waiting for the local test suite to finish rebuilding the venv.", age_s=WAIT_AFTER + 600)
    heart(conn, "t_w", "Waited out the rate cooldown; resuming the sweep now.", age_s=WAIT_AFTER + 300)
    assert waiting(conn) == []


def test_a_heartbeat_without_a_note_is_never_admission():
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", None, age_s=WAIT_AFTER + 600)
    heart(conn, "t_w", None, age_s=WAIT_AFTER + 300)
    assert waiting(conn) == []


def test_on_tick_is_unthrottled_state_is_its_own_file_and_it_fails_open(monkeypatch, tmp_path):
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
    conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values ('t_w', 'heartbeat', ?, ?)",
        (json.dumps({"note": INCIDENT}), -2 * WAIT_AFTER),
    )
    conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values ('t_w', 'heartbeat', ?, ?)",
        (json.dumps({"note": INCIDENT}), -15 * 60),
    )
    conn.commit()
    conn.close()

    calls = []
    monkeypatch.setattr(wake.stale_wake, "_inject", lambda text, origin: calls.append((text, origin)) or True)
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    state_file = tmp_path / "kanban-waiting-wake.json"

    wake.on_tick(board=None)
    assert len(calls) == 1 and "[Kanban waiting]" in calls[0][0]
    first = state_file.read_text()
    wake.on_tick(board=None)
    wake.on_tick(board=None, dry_run=True)
    assert len(calls) == 1 and state_file.read_text() == first

    monkeypatch.setattr(wake, "sweep", lambda *a: 1 / 0)
    wake.on_tick(board=None)
    wake.on_tick(board=None, dry_run=True)


def test_registered_on_dispatch_tick():
    hooks = []
    ctx = type("Ctx", (), {"register_hook": lambda self, name, fn: hooks.append((name, fn))})()
    sys.modules["agent_guards"].register(ctx)
    assert ("on_kanban_dispatch_tick", wake.on_tick) in hooks
