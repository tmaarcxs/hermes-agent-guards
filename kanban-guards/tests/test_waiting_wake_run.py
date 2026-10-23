import json
import sqlite3
import sys
import time

guards = sys.modules["kanban_guards"]
wake = guards.wakes
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
PROGRESS = (
    "auto-merged within 12s of creation; CDTool deployment-success posted 11:56:19Z. "
    "Now verifying serving pod + image via Prometheus"
)
PIPELINE_WAIT = (
    "Release pipeline with queued workflows still running; our build is behind two queued jobs, "
    "waiting on the pipeline to reach the deploy stage."
)


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


CROSS_CLAUSE_FP = (
    "Waiting for the local sweep to finish before writing the summary: fires counted, dedup applied "
    "across the mirrored sessions, timestamps pinned to the merge of the stall-episode fix."
)
NEXT_CLAUSE_FP = (
    "While waiting, drafting the reconciliation sections that are patch-independent: the shared-register "
    "pass (evidence complete), the decision kernels (title-equality, ordering, wiring), and the merge-order notes."
)
DISCUSSION_FP = (
    "Premise checks done Tuesday: producer clean, state persisted, deliveries recoverable in full. "
    "Awaiting the changelog reconciliation, appendix numbering reads, and the paperwork round-trip "
    "for the merge-window choice."
)


def test_an_actor_in_a_later_clause_is_not_the_external_gate():
    """The observed live false-positive shapes on real heartbeat notes: the
    waiter word sits in a label, a tail mention or an independent sentence about
    separate work, and the only actor match inside the old 200-char window
    belongs to a different clause than a wait admission, so none of the notes
    admits waiting on an external gate."""
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    for note in (CROSS_CLAUSE_FP, NEXT_CLAUSE_FP, DISCUSSION_FP):
        assert wake._WAIT.search(note)
        window = note[wake._WAIT.search(note).end(): wake._WAIT.search(note).end() + 200]
        assert wake._ACTOR.search(window)  # the OLD window matched all three
        heart(conn, "t_w", note, age_s=WAIT_AFTER + 600)
    assert waiting(conn) == []
    rec = Recorder()
    assert wake.waiting_sweep(conn, {}, rec, NOW) == {}
    assert rec.calls == []


def test_a_wait_note_sixteen_minutes_old_wakes_the_card_exactly_once_with_the_incident_text():
    conn = board()
    a_run_with_one_wait_note(conn, first_age_s=16 * MIN)
    rec = Recorder()
    state = wake.waiting_sweep(conn, {}, rec, NOW)
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
    assert wake.waiting_sweep(conn, state, Recorder(), NOW) == state
    assert wake.waiting_sweep(conn, state, Recorder(), NOW + 5 * HOUR) == state


def test_a_wait_note_fourteen_minutes_old_does_not_wake_yet():
    conn = board()
    a_run_with_one_wait_note(conn, first_age_s=14 * MIN)
    rec = Recorder()
    assert wake.waiting_sweep(conn, {}, rec, NOW) == {}
    assert rec.calls == []
    state = wake.waiting_sweep(conn, {}, Recorder(), NOW + 2 * MIN)
    assert state == {"t_w": 1}


def test_wakes_again_only_for_a_new_run():
    conn = board()
    a_run_with_one_wait_note(conn)
    state = wake.waiting_sweep(conn, {}, Recorder(), NOW)
    spawn(conn, "t_w")
    rec = Recorder()
    state2 = wake.waiting_sweep(conn, state, rec, NOW)
    assert rec.calls == [] and state2 == {}
    heart(conn, "t_w", INCIDENT, age_s=WAIT_AFTER + 60)
    heart(conn, "t_w", INCIDENT, age_s=30)
    rec2 = Recorder()
    state3 = wake.waiting_sweep(conn, state2, rec2, NOW)
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
    state = wake.waiting_sweep(conn, {}, Recorder(ok=False), NOW)
    assert state == {}
    rec = Recorder()
    assert wake.waiting_sweep(conn, state, rec, NOW) == {"t_w": 1}
    conn.execute("update tasks set status = 'done' where id = 't_w'")
    assert wake.waiting_sweep(conn, {"t_w": 1}, Recorder(), NOW) == {}


def test_a_wait_phrase_without_an_external_actor_after_it_never_counts():
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", "Waiting for the local test suite to finish rebuilding the venv.", age_s=WAIT_AFTER + 600)
    heart(conn, "t_w", "Waited out the rate cooldown; resuming the sweep now.", age_s=WAIT_AFTER + 300)
    assert waiting(conn) == []


def test_the_actor_must_sit_within_the_wait_notes_own_clause():
    """A distant 'PR' beyond the clause boundary at 80 chars is not a gate;
    the same actor within 80 chars still counts as the external gate."""
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    tail = " on the downstream gate. " + "x" * 70 + " pr"
    heart(conn, "t_w", "still waiting" + tail, age_s=WAIT_AFTER + 600)
    assert waiting(conn) == []
    conn2 = board()
    card(conn2, "t_w")
    spawn(conn2, "t_w")
    heart(conn2, "t_w", "still waiting on the " + "a" * 40 + " pr", age_s=WAIT_AFTER + 600)
    assert waiting(conn2) == ["t_w"]


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
    monkeypatch.setattr(wake, "inject", lambda text, origin: calls.append((text, origin)) or True)
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    state_file = tmp_path / "kanban-waiting-wake.json"

    wake.waiting_on_tick(board=None)
    assert len(calls) == 1 and "[Kanban waiting]" in calls[0][0]
    first = state_file.read_text()
    wake.waiting_on_tick(board=None)
    wake.stale_on_tick(board=None, dry_run=True)
    assert len(calls) == 1 and state_file.read_text() == first

    monkeypatch.setattr(wake, "waiting_sweep", lambda *a: 1 / 0)
    wake.waiting_on_tick(board=None)
    wake.stale_on_tick(board=None, dry_run=True)


def test_registered_on_dispatch_tick():
    hooks = []
    ctx = type("Ctx", (), {"register_hook": lambda self, name, fn: hooks.append((name, fn))})()
    sys.modules["kanban_guards"].register(ctx)
    registered = [hook for name, hook in hooks if name == "on_kanban_dispatch_tick"]
    for hook in (wake.stale_on_tick, wake.loop_on_tick, wake.churn_on_tick, wake.waiting_on_tick, wake.stall_on_tick, wake.reconcile_on_tick, guards.active_pr_unblock.tick, guards.delivered_pr_park.tick):
        assert hook in registered
    assert len(registered) == 8


def test_streak_wake_keys_on_the_newest_nonempty_note_not_the_run_first():
    """The real incident sequence: wait note 16 minutes old, then empty heartbeats
    each minute — empty notes neither start nor break a streak, so the newest
    non-empty note is still the aged wait note and the wake fires exactly once."""
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", INCIDENT, age_s=16 * MIN)
    for age in (5 * MIN, 4 * MIN, 3 * MIN, 2 * MIN, 1 * MIN):
        heart(conn, "t_w", None, age_s=age)
    rec = Recorder()
    state = wake.waiting_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_w"]
    assert "16 min" in rec.calls[0][0]
    assert state == {"t_w": 1}


def test_a_progress_note_after_a_wait_note_ends_the_streak():
    """The lead's counter-case: a wait note 19 minutes old that the run already
    moved past (progress 1 minute old) must NOT wake — the worker is not waiting."""
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", PIPELINE_WAIT, age_s=19 * MIN)
    for age in (16 * MIN, 12 * MIN, 8 * MIN):
        heart(conn, "t_w", PROGRESS, age_s=age)
    heart(conn, "t_w", None, age_s=3 * MIN)
    heart(conn, "t_w", PROGRESS, age_s=1 * MIN)
    rec = Recorder()
    assert wake.waiting_sweep(conn, {}, rec, NOW) == {}
    assert rec.calls == []


def test_a_new_wait_note_restarts_the_streak_and_the_clock():
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", INCIDENT, age_s=25 * MIN)
    heart(conn, "t_w", PROGRESS, age_s=10 * MIN)
    heart(conn, "t_w", "Still waiting on the code review verdict on PR 1047.", age_s=2 * MIN)
    rec = Recorder()
    assert wake.waiting_sweep(conn, {}, rec, NOW) == {}
    assert rec.calls == []
    heart(conn, "t_w", PROGRESS, age_s=1 * MIN)
    assert wake.waiting_sweep(conn, {}, Recorder(), NOW) == {}

    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", INCIDENT, age_s=25 * MIN)
    heart(conn, "t_w", PROGRESS, age_s=10 * MIN)
    heart(conn, "t_w", "Still waiting on the code review verdict on PR 1047.", age_s=16 * MIN)
    rec2 = Recorder()
    state = wake.waiting_sweep(conn, {}, rec2, NOW)
    assert woken(rec2) == ["t_w"] and state == {"t_w": 1}


def test_empty_heartbeats_in_the_middle_of_a_streak_do_not_break_it():
    conn = board()
    card(conn, "t_w")
    spawn(conn, "t_w")
    heart(conn, "t_w", INCIDENT, age_s=20 * MIN)
    heart(conn, "t_w", "Still waiting on CDTool auto-merge of Deploy PR 1086.", age_s=10 * MIN)
    for age in (9 * MIN, 8 * MIN, 7 * MIN, 6 * MIN, 5 * MIN):
        heart(conn, "t_w", None, age_s=age)
    rec = Recorder()
    state = wake.waiting_sweep(conn, {}, rec, NOW)
    assert woken(rec) == ["t_w"]
    text, _ = rec.calls[0]
    assert "20 min" in text
    assert state == {"t_w": 1}

