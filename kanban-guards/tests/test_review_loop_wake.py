import json
import pytest
import sqlite3
import time
import sys

loop = sys.modules["kanban_guards"].wakes

from kanban_guards import board_watch


def board():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        create table tasks (id text, status text, title text);
        create table task_events (id integer primary key, task_id text, kind text, payload text);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text);
        """
    )
    return conn


def card(conn, tid, status, rounds, subscribed=True):
    conn.execute("insert into tasks values (?, ?, ?)", (tid, status, f"title {tid}"))
    for n in range(rounds):
        changes(conn, tid, f"fix {n + 1}")
    if subscribed:
        conn.execute(
            "insert into kanban_notify_subs values (?, 'telegram', '555000100', '900332', 'dm', '555000100', null, 'default')",
            (tid,),
        )


def changes(conn, tid, reason):
    conn.execute(
        "insert into task_events (task_id, kind, payload) values (?, 'changes_requested', ?)",
        (tid, json.dumps({"reason": reason})),
    )


class Recorder:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, text, origin):
        self.calls.append((text, origin))
        return self.ok


class FakeFile:
    """The durable state file, in memory.

    ``sweep`` writes through the ``persist`` hook, so the tests model the
    restart timeline — what the file holds at each crash point — without
    touching disk. ``fail_first`` drops the first write, the crash before
    the marker lands (board_watch's atomic replace never leaves a partial
    file behind).
    """

    def __init__(self, fail_first=False):
        self.data, self.fail_first = {}, fail_first

    def __call__(self, state):
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("gateway restarted mid-write")
        self.data = dict(state)

    def load(self):
        return dict(self.data)


def woken(rec):
    return sorted(text.split()[3] for text, _ in rec.calls)


def test_wakes_open_card_at_third_round_with_latest_reason():
    conn = board()
    card(conn, "t_loop", "ready", 3)
    card(conn, "t_two", "ready", 2)
    card(conn, "t_done", "done", 9)
    rec = Recorder()
    state = loop.loop_sweep(conn, {}, rec)
    assert woken(rec) == ["t_loop"]
    text, origin = rec.calls[0]
    assert "3 review rounds" in text and "fix 3" in text and "every 3 review rounds" in text
    assert "(1) narrow the contract" in text
    assert "every numeric sentence names the script and output that print it, or is removed" in text
    assert "the reviewer re-executes the scripts, checks that mapping and the named lines" in text
    assert "opens no new analyses (a new analysis goes to a new card)" in text
    assert "(2) continue over the real defects" in text and "new or repeats of earlier ones" in text
    assert "successor card" in text
    assert "(3) split the card" in text and "(4) take it over" in text
    assert origin["chat_id"] == "555000100" and origin["thread_id"] == "900332"
    assert state == {"t_loop": 3}


def test_wakes_once_per_threshold_and_again_at_next():
    conn = board()
    card(conn, "t_loop", "running", 4)
    rec = Recorder()
    state = loop.loop_sweep(conn, {}, rec)
    assert len(rec.calls) == 1
    changes(conn, "t_loop", "fix 5")
    rec2 = Recorder()
    state = loop.loop_sweep(conn, state, rec2)
    assert rec2.calls == []
    changes(conn, "t_loop", "fix 6")
    rec3 = Recorder()
    state = loop.loop_sweep(conn, state, rec3)
    assert woken(rec3) == ["t_loop"] and state == {"t_loop": 6}


def test_failed_injection_or_missing_chat_retries_later():
    conn = board()
    card(conn, "t_nochat", "review", 3, subscribed=False)
    card(conn, "t_fail", "review", 3)
    state = loop.loop_sweep(conn, {}, Recorder(ok=False))
    assert state == {}
    rec = Recorder()
    assert loop.loop_sweep(conn, state, rec) == {"t_fail": 3}


def test_one_wake_per_threshold_across_repeated_sweeps_and_restarts(monkeypatch, tmp_path):
    """A card at 3 rounds swept N>=5 times wakes exactly once.

    Production shape, not the in-memory dict the older tests pass: every sweep
    loads whatever the durable file holds when it starts (the tick reloads it
    each dispatch), including the first sweep after a simulated gateway
    restart. Current main re-injected the same threshold when a sweep's
    snapshot state arrived without the marker the file already held (the
    t_bd3f3ce6 triple wake on 10-19): the flush decision trusted the snapshot
    alone. Call shape is main-compatible — the fresh durable read only exists
    with the fix, so on main the sweep trusts the snapshot alone, which is
    exactly the defect under test.
    """
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn = board()
    card(conn, "t_loop", "running", 3)
    rec = Recorder()
    total = []

    def sweep_once(state):
        load = getattr(loop, "_file_load", None)
        kwargs = {"load": load} if load else {}
        state = loop.loop_sweep(conn, dict(state), rec, **kwargs)
        total.extend(rec.calls)
        rec.calls.clear()
        return state

    state = sweep_once({})
    assert len(total) == 1
    for _ in range(4):
        state = sweep_once({})
        state = sweep_once(state)
    assert len(total) == 1  # still exactly the one wake
    assert json.loads((tmp_path / "kanban-review-loop.json").read_text()) == {"t_loop": 3}

    for n in range(4, 7):  # 6 rounds: exactly one more wake
        changes(conn, "t_loop", f"fix {n}")
    state = sweep_once({})
    assert len(total) == 2

    card(conn, "t_other", "running", 3)  # a different card at 3 rounds still wakes
    state = sweep_once({})
    assert len(total) == 3


def test_failed_injection_does_not_erase_a_sibling_marker(monkeypatch, tmp_path):
    """A failed inject must not erase a sibling's flushed marker.

    Two cards reach a threshold in one sweep; the first card's inject fails
    (no chat delivered), the second one delivers right after. The durable
    file, sampled from inside the second card's inject, must already hold
    the second card's threshold — not the pre-flush snapshot the failure
    used to restore, which erased the delivered card's marker and replayed
    its wake into the lead's chat if the gateway died before
    board_watch.tick persisted the sweep's return value (the duplicate
    t_da9201d3 reported after the 10-18 restart).
    """
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn = board()
    card(conn, "t_a", "review", 3)
    card(conn, "t_zb", "review", 3)
    file_at_t_zb_inject = []

    class Inject:
        """Fails the first card's delivery, samples the file on the second's."""

        def __call__(self, text, origin):
            if text.split()[3] == "t_a":
                return False
            file_at_t_zb_inject.append(
                json.loads((tmp_path / "kanban-review-loop.json").read_text())
            )
            return True

    state = loop.loop_sweep(conn, {}, Inject())
    assert state == {"t_zb": 3}
    assert file_at_t_zb_inject == [{"t_zb": 3}]

    rec = Recorder()
    replay = loop.loop_sweep(conn, json.loads((tmp_path / "kanban-review-loop.json").read_text()), rec)
    assert woken(rec) == ["t_a"] and replay == {"t_a": 3, "t_zb": 3}


def test_closed_cards_are_forgotten():
    conn = board()
    card(conn, "t_loop", "ready", 3)
    state = loop.loop_sweep(conn, {}, Recorder())
    conn.execute("update tasks set status = 'done' where id = 't_loop'")
    assert loop.loop_sweep(conn, state, Recorder()) == {}


def test_wake_text_is_byte_identical():
    """The merged 3-round decision menu must not drift with this change."""
    conn = board()
    card(conn, "t_abc123", "review", 6)
    rec = Recorder()
    loop.loop_sweep(conn, {}, rec)
    assert rec.calls[0][0] == (
        "[Kanban review loop] t_abc123 has had 6 review rounds with changes requested (review):"
        " title t_abc123\n"
        "Latest requested change: fix 6\n"
        "Implementer and reviewer are not converging. Decide it here with one of:\n"
        "(1) narrow the contract and send the card back — if the recent findings are sentence or number "
        "integrity only, record this convergence contract on the card: every numeric sentence names the "
        "script and output that print it, or is removed; the reviewer re-executes the scripts, checks "
        "that mapping and the named lines, and opens no new analyses (a new analysis goes to a new card);\n"
        "(2) continue over the real defects — check whether they are new or repeats of earlier ones and "
        "whether a successor card is needed once they are fixed;\n"
        "(3) split the card — close it and hand the remaining, still-tractable parts to fresh cards;\n"
        "(4) take it over — finish the remaining work yourself, or archive the card with a comment on "
        "where the outcome lives.\n"
        "The wake repeats at the next threshold (every 3 review rounds)."
    )


@pytest.mark.parametrize("rounds", [3, 6, 9])
def test_exactly_one_wake_per_threshold_across_restarts(rounds):
    """No crash point can turn one threshold into two lead wakes.

    Crash points, each followed by a fresh sweep on the same board state —
    the replay board_watch.tick runs after the gateway reappears (it
    swallows the sweep's exception; the state file survives the restart):

    - before the marker: the file stays empty, the sweep is owed and wakes
      exactly once on the next tick;
    - between the marker and the inject: the file already holds the
      threshold, so the replay sweep is silent and the wake is dropped
      (at-most-once), re-arming at the next threshold;
    - after the inject: identical to the previous point — the marker covers
      the delivered wake, so the replay is silent.

    The next threshold still wakes. The injected text is byte-identical to
    the pre-change menu (test_wake_text_is_byte_identical).
    """
    conn = board()
    card(conn, "t_loop", "review", rounds)
    threshold = rounds // 3 * 3
    next_threshold = threshold + 3

    file = FakeFile(fail_first=False)

    class Restart:
        def __call__(self, text, origin):
            raise RuntimeError("gateway restarted mid-sweep")

    with pytest.raises(RuntimeError):
        loop.loop_sweep(conn, {}, Restart(), file)
    assert file.load() == {"t_loop": threshold}

    rec = Recorder()
    assert loop.loop_sweep(conn, file.load(), rec, file) == {"t_loop": threshold}
    assert rec.calls == []

    file = FakeFile(fail_first=True)
    with pytest.raises(RuntimeError):
        loop.loop_sweep(conn, {}, Restart(), file)
    assert file.load() == {}

    rec = Recorder()
    assert loop.loop_sweep(conn, file.load(), rec, file) == {"t_loop": threshold}
    assert woken(rec) == ["t_loop"]

    for n in range(rounds, next_threshold + 1):
        changes(conn, "t_loop", f"fix {n + 1}")
    rec = Recorder()
    state = loop.loop_sweep(conn, file.load(), rec, file)
    assert woken(rec) == ["t_loop"] and state["t_loop"] == next_threshold
    assert len(rec.calls) == 1


def test_threshold_is_flushed_before_the_first_injection():
    """The marker is durable before the wake is sent, not after it returns."""
    conn = board()
    card(conn, "t_loop", "ready", 3)
    order = []
    file = FakeFile()

    class Inject:
        def __call__(self, text, origin):
            order.append(("inject", dict(file.data)))
            return True

    loop.loop_sweep(conn, {}, Inject(), file)
    assert order == [("inject", {"t_loop": 3})]
    assert file.load() == {"t_loop": 3}


def test_production_write_path_flushes_the_marker_before_the_inject(monkeypatch, tmp_path):
    """The production entry point (board_watch.write_state on disk) has the
    marker down before the first injection runs, not after it returns."""
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    conn = board()
    card(conn, "t_loop", "review", 3)
    on_disk_at_inject = []

    class Inject:
        def __call__(self, text, origin):
            on_disk_at_inject.append((tmp_path / "kanban-review-loop.json").exists())
            return True

    state = loop.loop_sweep(conn, {}, Inject())
    assert on_disk_at_inject == [True]
    assert state == {"t_loop": 3}


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
    monkeypatch.setattr(loop, "loop_sweep", lambda *a, **k: calls.append(a) or {"t_x": 3})
    loop.loop_on_tick(board=None)
    assert len(calls) == 1  # no cursor yet: one initialization sweep
    assert json.loads((tmp_path / "kanban-review-loop.json").read_text()) == {"t_x": 3}
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'heartbeat')")
    conn.commit()
    loop.loop_on_tick(board=None)
    assert len(calls) == 1  # heartbeat is not a review-round change: skipped
    conn.execute("insert into task_events (task_id, kind) values ('t_x', 'changes_requested')")
    conn.commit()
    loop.loop_on_tick(board=None)
    assert len(calls) == 2
    loop.loop_on_tick(board=None)  # same relevant event again: skipped
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
    monkeypatch.setattr(loop, "loop_sweep", lambda *a, **k: calls.append(a) or {})
    loop.loop_on_tick(board=None)
    assert len(calls) == 1  # no cursor yet: one initialization sweep
    board_watch.write_state(board_watch.POKE_FILE, {"at": time.time() + 10})
    loop.loop_on_tick(board=None)
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
    monkeypatch.setattr(loop, "loop_sweep", lambda *a: 1 / 0)
    loop.loop_on_tick(board=None)
    assert json.loads((tmp_path / "kanban-review-loop-cursor.json").read_text())["event_id"] == 1

def test_dry_run_never_sweeps_and_never_writes(monkeypatch, tmp_path):
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: tmp_path / "k.db")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    conn = sqlite3.connect(tmp_path / "k.db")
    conn.execute("create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer)")
    conn.commit()
    monkeypatch.setattr(board_watch, "state_path", lambda name: tmp_path / name)
    loop.stale_on_tick(board=None, dry_run=True)
    assert not (tmp_path / "kanban-review-loop.json").exists() and not (tmp_path / "kanban-review-loop-cursor.json").exists()


def test_registered_on_dispatch_tick():
    hooks = []
    ctx = type("Ctx", (), {"register_hook": lambda self, name, fn: hooks.append((name, fn))})()
    sys.modules["kanban_guards"].register(ctx)
    assert any(name == "on_kanban_dispatch_tick" and hook.__module__.endswith("wakes") for name, hook in hooks)
