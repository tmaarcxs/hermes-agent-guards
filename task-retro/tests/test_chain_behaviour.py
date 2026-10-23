"""Behaviour test for the retro lanes against a REAL kanban DB.

This is the acceptance proof that the plugin's lane chain actually caps retros
at ``_MAX_ACTIVE_RETROS``: it drives the real ``hermes_cli.kanban_db`` API
against a **temporary** database — never the live ``~/.hermes/kanban.db`` — and
asserts that

- the first three retros are created ``ready`` (free lanes, run immediately),
- the fourth is created as a CHILD of the oldest lane tail and parks in ``todo``,
- completing a lane tail promotes its queued child to ``ready``
  (``kanban_db.recompute_ready``), so at most three retros are active at a time,
- a blocked retro is archived by the blocked hook and releases its child.

The database path is pinned through ``HERMES_KANBAN_DB`` (the same pin the
dispatcher injects into workers), so the plugin resolves to the temp DB. The
temp dir lives under ``/tmp``, outside the fenced root, so the connection is a
normal writable one.

Needs the Hermes environment (``hermes_cli`` on ``sys.path``); skips with a
stated reason otherwise. Run it with an interpreter that can import it:

    uv run --no-project --python ~/.hermes/hermes-agent/.venv/bin/python3 \
      --with pytest python -m pytest tests/test_chain_behaviour.py -q
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent

try:  # the real API — this test exists to exercise it, not a stub
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
except Exception as exc:  # pragma: no cover - environment dependent
    pytest.skip(
        f"hermes_cli not importable in this interpreter ({exc!r}); run with "
        "~/.hermes/hermes-agent/.venv/bin/python3",
        allow_module_level=True,
    )


def _load_plugin():
    sys.modules.pop("task_retro_behaviour", None)
    spec = importlib.util.spec_from_file_location(
        "task_retro_behaviour", PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["task_retro_behaviour"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def temp_board(monkeypatch):
    """A throwaway kanban DB the plugin is wired to. Never the live one."""
    # /tmp, NOT TMPDIR: TMPDIR lives under ~/.hermes, which the delegation
    # fence treats as the owner's board root.
    root = Path(tempfile.mkdtemp(prefix="task-retro-chain-", dir="/tmp"))
    db_path = root / "kanban.db"
    live = kb.kanban_db_path()
    assert str(db_path) != str(live), "refusing to touch the live board"
    conn = kbc.connect(db_path=db_path)
    # Point the plugin at this temp board through the REAL connect function with
    # an explicit db_path. Setting HERMES_KANBAN_DB instead would make
    # ``kanban_path_is_fenced`` treat the pin as the owner's board and open it
    # read-only, and the plugin itself always calls ``connect(board=None)``.
    real_connect = kbc.connect
    monkeypatch.setattr(
        kbc, "connect",
        lambda board=None, **kw: real_connect(db_path=db_path, board=board),
    )
    try:
        yield db_path, conn
    finally:
        conn.close()
        monkeypatch.undo()
        assert str(db_path) != str(kb.kanban_db_path())
        shutil.rmtree(root, ignore_errors=True)


def _retro_cards(conn):
    """Every retro card on the temp board, newest first."""
    return [t for t in kb.list_tasks(conn, order_by="created-desc")
            if (t.title or "").startswith("retro:")]


def _complete_a_source_card(conn, title="Build the widget"):
    """Create + claim + complete a real source card; return (task_id, run_id)."""
    source_id = kb.create_task(conn, title=title, assignee="worker")
    kb.claim_task(conn, source_id)
    run_id = kb.get_task(conn, source_id).current_run_id
    assert run_id is not None
    assert kb.complete_task(
        conn, source_id, result="did the thing", expected_run_id=run_id,
        fire_lifecycle_hook=False,
    )
    return source_id, run_id


def _park_blocked(conn, task_id):
    """Park a card ``blocked`` exactly like the dispatcher's circuit-breaker: a
    raw UPDATE plus a sticky ``gave_up`` event, and NO ``kanban_task_blocked``
    hook. Deterministic whether or not a live plugin instance is registered."""
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,))
    kb._append_event(conn, task_id, "gave_up", {"sticky": True})
    assert kb.get_task(conn, task_id).status == "blocked"


def test_free_lanes_then_chain_on_a_real_board(temp_board):
    db_path, conn = temp_board
    mod = _load_plugin()
    # Threshold 0 so the freshly-claimed run is not filtered as trivial; the
    # duration gate itself is covered by the unit tests.
    mod._config_cache = {"plugin": {"min_duration_seconds": 0},
                         "default_assignee": "worker"}

    # 1. first three completions -> three free lanes, all ready
    first = None
    for index in range(3):
        source_id, run_id = _complete_a_source_card(conn, f"Build widget {index}")
        mod._on_kanban_task_completed(
            task_id=source_id, board="default", run_id=run_id, summary="did it")
        retros = _retro_cards(conn)
        if first is None:
            first = next(t for t in retros if t.id)
        assert len(retros) == index + 1, [t.id for t in retros]

    retros = _retro_cards(conn)
    assert len(retros) == 3
    for retro in retros:
        assert kb.get_task(conn, retro.id).status == "ready", retro.id
        assert kb.parent_ids(conn, retro.id) == []

    # 2. fourth completion: all lanes taken -> child of the OLDEST lane tail
    source_4, run_4 = _complete_a_source_card(conn, "Build widget 3")
    mod._on_kanban_task_completed(
        task_id=source_4, board="default", run_id=run_4, summary="did it")
    retros = _retro_cards(conn)
    assert len(retros) == 4
    statuses = {t.id: kb.get_task(conn, t.id).status for t in retros}
    ready = [tid for tid, st in statuses.items() if st == "ready"]
    todo = [tid for tid, st in statuses.items() if st == "todo"]
    assert len(ready) == 3, statuses
    assert len(todo) == 1, statuses
    fourth = todo[0]
    # the parent is the OLDEST lane tail among the three heads
    heads = [t for t in retros if t.id != fourth]
    oldest = min(heads, key=lambda t: (t.created_at or 0, t.id))
    assert kb.parent_ids(conn, fourth) == [oldest.id]

    # 3. the oldest tail finishing promotes its queued child
    assert kb.complete_task(conn, oldest.id, result="retro done",
                            fire_lifecycle_hook=False)
    assert kb.get_task(conn, fourth).status == "ready"
    assert db_path.exists()
    assert str(db_path) != str(kb.kanban_db_path())


def test_seven_retros_round_robin_on_a_real_board(temp_board):
    """A..G land in lanes A->D->G, B->E, C->F: 3 ready, 4 parked."""
    db_path, conn = temp_board
    mod = _load_plugin()
    mod._config_cache = {"plugin": {"min_duration_seconds": 0},
                         "default_assignee": "worker"}

    for index in range(7):
        source_id, run_id = _complete_a_source_card(conn, f"Build widget {index}")
        mod._on_kanban_task_completed(
            task_id=source_id, board="default", run_id=run_id, summary="did it")

    retros = _retro_cards(conn)
    assert len(retros) == 7
    statuses = {t.id: kb.get_task(conn, t.id).status for t in retros}
    ready = [tid for tid, st in statuses.items() if st == "ready"]
    todo = [tid for tid, st in statuses.items() if st == "todo"]
    assert len(ready) == 3, statuses
    assert len(todo) == 4, statuses


def test_blocked_retro_does_not_stall_the_lane(temp_board):
    """A blocked retro is never a lane tail: the next retro starts immediately."""
    db_path, conn = temp_board
    mod = _load_plugin()
    mod._config_cache = {"plugin": {"min_duration_seconds": 0},
                         "default_assignee": "worker"}

    source_1, run_1 = _complete_a_source_card(conn, "Build the widget")
    mod._on_kanban_task_completed(
        task_id=source_1, board="default", run_id=run_1, summary="did the thing")
    blocked = _retro_cards(conn)[0]
    _park_blocked(conn, blocked.id)

    source_2, run_2 = _complete_a_source_card(conn, "Build the other widget")
    mod._on_kanban_task_completed(
        task_id=source_2, board="default", run_id=run_2, summary="did the thing")

    # The blocked retro was swept (archived) by the next completion, so the
    # active board shows only the fresh retro, unparented and ready.
    retros = _retro_cards(conn)
    assert len(retros) == 1
    fresh = retros[0]
    assert fresh.id != blocked.id
    assert kb.parent_ids(conn, fresh.id) == []
    assert kb.get_task(conn, fresh.id).status == "ready"
    assert kb.get_task(conn, blocked.id).status == "archived"


def test_probe_source_card_gets_no_retro_on_a_real_board(temp_board):
    db_path, conn = temp_board
    mod = _load_plugin()
    mod._config_cache = {"plugin": {"min_duration_seconds": 0},
                         "default_assignee": "worker"}

    source_id, run_id = _complete_a_source_card(
        conn, "probe: task-retro live end-to-end (throwaway)")
    mod._on_kanban_task_completed(
        task_id=source_id, board="default", run_id=run_id, summary="did the thing")
    assert _retro_cards(conn) == []


def test_retro_never_subscribes_a_chat_on_a_real_board(temp_board):
    """No creator_task_id / parents inheritance: zero notify subscriptions."""
    db_path, conn = temp_board
    mod = _load_plugin()
    mod._config_cache = {"plugin": {"min_duration_seconds": 0},
                         "default_assignee": "worker"}

    source_id, run_id = _complete_a_source_card(conn, "Build the widget")
    mod._on_kanban_task_completed(
        task_id=source_id, board="default", run_id=run_id, summary="did the thing")

    retro = _retro_cards(conn)[0]
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM kanban_notify_subs WHERE task_id = ?", (retro.id,)
    ).fetchone()
    assert rows["n"] == 0


# --- concurrency on a REAL board --------------------------------------------


def test_four_concurrent_completions_cap_at_three_on_a_real_board(temp_board):
    """4 threads completing at once -> exactly 3 ready retros and 1 todo child.

    The four source cards are built FIRST, then ``write_txn`` is wrapped so each
    of the four threads blocks on a barrier the instant it reaches the
    transaction boundary — the real check-then-act window (tail lookup + insert
    live inside that one ``BEGIN IMMEDIATE``). All four arrive together
    (``barrier.broken`` stays False and every wait returns ``ok``); the lock then
    serializes them, so three find a free lane and the fourth chains behind the
    oldest tail. Wrapping ``create_task`` instead would NOT force the race: the
    outer txn is already held, so the later threads wait on the write lock and
    the barrier times out.
    """
    import threading

    db_path, conn = temp_board
    mod = _load_plugin()
    mod._config_cache = {"plugin": {"min_duration_seconds": 0},
                         "default_assignee": "worker"}

    real_connect = kbc.connect
    kbc.connect = lambda board=None, **kw: real_connect(db_path=db_path, board=board)

    pairs = []
    for index in range(4):
        source_id, run_id = _complete_a_source_card(conn, f"Build widget {index}")
        pairs.append((source_id, run_id))

    barrier = threading.Barrier(4, timeout=5.0)
    waits = []
    real_txn = kbc.write_txn

    def txn_with_window(connection, **kwargs):
        started = time.monotonic()
        try:
            barrier.wait()  # all four threads meet at the txn boundary
            waits.append(("ok", round(time.monotonic() - started, 3)))
        except threading.BrokenBarrierError:
            waits.append(("broken", round(time.monotonic() - started, 3)))
        return real_txn(connection, **kwargs)

    kbc.write_txn = txn_with_window

    errors = []

    def worker(source_id, run_id):
        try:
            mod._on_kanban_task_completed(
                task_id=source_id, board="default", run_id=run_id,
                summary="did the thing")
        except Exception as exc:  # the hook must never raise
            errors.append(exc)

    try:
        threads = [threading.Thread(target=worker, args=pair) for pair in pairs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
    finally:
        kbc.write_txn = real_txn

    assert not any(thread.is_alive() for thread in threads), "threads did not finish"
    assert errors == []
    # The race window was actually forced: all four met at the boundary.
    assert not barrier.broken, "barrier timed out — the window was not forced"
    assert [w[0] for w in waits] == ["ok"] * 4, waits

    retros = _retro_cards(conn)
    assert len(retros) == 4, [t.id for t in retros]
    statuses = {t.id: kb.get_task(conn, t.id).status for t in retros}
    ready = [tid for tid, status in statuses.items() if status == "ready"]
    todo = [tid for tid, status in statuses.items() if status == "todo"]
    assert len(ready) == 3, statuses
    assert len(todo) == 1, statuses
    child = todo[0]
    assert len(kb.parent_ids(conn, child)) == 1
    assert kb.parent_ids(conn, child)[0] in ready
    assert str(db_path) != str(kb.kanban_db_path())


# --- blocked retro archived by the hook on a REAL board ----------------------


def test_blocked_hook_archives_a_retro_and_releases_its_child(temp_board):
    """A blocked retro with a queued child is archived; the child becomes ready.

    Pre-fix this stalls: ``recompute_ready`` gates children on a blocked parent,
    so D stays ``todo`` forever. The blocked hook archives A, which releases D.
    """
    db_path, conn = temp_board
    mod = _load_plugin()
    mod._config_cache = {"plugin": {"min_duration_seconds": 0},
                         "default_assignee": "worker"}

    # Three completions fill the three lanes.
    for label in ("A", "B", "C"):
        source_id, run_id = _complete_a_source_card(conn, f"Build widget {label}")
        mod._on_kanban_task_completed(
            task_id=source_id, board="default", run_id=run_id, summary=f"did {label}")

    # A fourth retro chains behind the oldest lane tail (created_at tie-break).
    source_d, run_d = _complete_a_source_card(conn, "Build widget D")
    mod._on_kanban_task_completed(
        task_id=source_d, board="default", run_id=run_d, summary="did D")
    retros = _retro_cards(conn)
    parked = [t for t in retros if kb.get_task(conn, t.id).status == "todo"]
    assert len(parked) == 1, {t.id: kb.get_task(conn, t.id).status for t in retros}
    retro_d = parked[0]
    parent = kb.parent_ids(conn, retro_d.id)
    assert len(parent) == 1
    tail = parent[0]

    # The parent blocks (no hook fires). Without the hook D stays parked behind
    # it forever.
    _park_blocked(conn, tail)

    # The blocked hook archives the tail and releases D.
    mod._on_kanban_task_blocked(task_id=tail, board="default",
                                reason="needs input")
    assert kb.get_task(conn, tail).status == "archived"
    assert kb.get_task(conn, retro_d.id).status == "ready"
    assert str(db_path) != str(kb.kanban_db_path())


def test_completed_hook_sweeps_a_parked_retro_on_a_real_board(temp_board):
    """The completed-hook safety net archives a retro left blocked with no hook."""
    db_path, conn = temp_board
    mod = _load_plugin()
    mod._config_cache = {"plugin": {"min_duration_seconds": 0},
                         "default_assignee": "worker"}

    source_a, run_a = _complete_a_source_card(conn, "Build widget A")
    mod._on_kanban_task_completed(
        task_id=source_a, board="default", run_id=run_a, summary="did A")
    retro_a = _retro_cards(conn)[0]
    _park_blocked(conn, retro_a.id)

    source_b, run_b = _complete_a_source_card(conn, "Build widget B")
    mod._on_kanban_task_completed(
        task_id=source_b, board="default", run_id=run_b, summary="did B")
    assert kb.get_task(conn, retro_a.id).status == "archived"
    assert str(db_path) != str(kb.kanban_db_path())
