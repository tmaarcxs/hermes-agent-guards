"""Tests for the task-retro plugin hook.

Runnable OUTSIDE the gateway: ``hermes_cli`` is stubbed via ``sys.modules``
(see the hermes-agent skill, references/plugin-operations.md — the CLI install
layout does not export it and a real import masks every downstream assertion).

The one exception is ``tests/test_chain_behaviour.py``, which needs the real
``hermes_cli.kanban_db`` API against a temporary database.

Run:  python3 -m pytest tests/ -q
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent

# ``sys.stdlib_module_names`` only exists on 3.10+; this suite must also run on
# the bare ``python3`` (3.9) that the card documents.
_STDLIB_FALLBACK = {
    "__future__", "json", "os", "pathlib", "time", "traceback", "typing",
}


# --- fakes ------------------------------------------------------------------


class FakeTask:
    def __init__(self, id, title, skills=None, assignee="worker", status="done",
                 idempotency_key=None, created_at=0):
        self.id = id
        self.title = title
        self.skills = list(skills or [])
        self.assignee = assignee
        self.status = status
        self.idempotency_key = idempotency_key
        self.created_at = created_at


class FakeRun:
    def __init__(self, id=1, task_id="t_done", outcome="completed",
                 started_at=0, ended_at=1000, metadata=None):
        self.id = id
        self.task_id = task_id
        self.outcome = outcome
        self.started_at = started_at
        self.ended_at = ended_at
        self.metadata = metadata


class _Row:
    """Minimal sqlite3.Row stand-in: ``row["id"]`` and ``row[0]``."""

    def __init__(self, values):
        self._values = values

    def __getitem__(self, key):
        return self._values[key]


class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    """Records create_task calls; enough surface for the plugin's reads.

    ``execute`` answers the one query the plugin issues itself — the
    idempotency lookup over ``tasks.idempotency_key`` — against the FakeKB's
    card list, so the archived-retro dedupe is exercised for real.
    """

    def __init__(self, task=None, run=None, kb=None):
        self.task = task
        self.run = run
        self.kb = kb
        self.created = []
        self.closed = False

    def execute(self, sql, params=()):
        if "idempotency_key" in sql:
            key = params[0] if params else None
            cards = list(getattr(self.kb, "retro_tasks", []))
            for card in reversed(cards):
                if getattr(card, "idempotency_key", None) == key:
                    return _Cursor(_Row({"id": card.id}))
            return _Cursor(None)
        return _Cursor(None)

    def close(self):
        self.closed = True


class FakeKB:
    """Stands in for ``hermes_cli.kanban_db``.

    ``list_tasks`` reads the seeded ``retro_tasks`` so the lanes are exercised
    for real: ``create_task`` appends the new card and records its parent edge,
    so a second hook call sees the first retro as an open lane tail.
    ``parent_ids``/``link_tasks``/``unlink_tasks``/``recompute_ready`` mirror the
    real API's gating rules so the lanes and the blocked hook can be driven
    without a database.
    """

    def __init__(self, task=None, run=None, conn=None, retro_tasks=None):
        self._task = task
        self._run = run
        self.runs = None
        self.retro_tasks = list(retro_tasks or [])
        self.conn = conn or FakeConn(task, run, kb=self)
        self.create_calls = []
        self.links = []          # (parent_id, child_id)
        self.comments = []       # (task_id, author, body)
        self._seq = 0

    def get_task(self, conn, task_id):
        return self._task

    def get_run(self, conn, run_id):
        return self._run

    def list_runs(self, conn, task_id, **kwargs):
        """Mirror ``kanban_db.list_runs`` for the parent-wall gate.

        ``runs`` (a list) wins when set; otherwise the single ``_run`` stands in
        for a one-attempt card.
        """
        if self.runs is not None:
            return list(self.runs)
        return [self._run] if self._run is not None else []

    def list_tasks(self, conn, **kwargs):
        """Mirror ``kanban_db.list_tasks``: newest-first for ``created-desc``
        and archived rows excluded unless requested."""
        tasks = list(self.retro_tasks)
        if not kwargs.get("include_archived"):
            tasks = [t for t in tasks if t.status != "archived"]
        if kwargs.get("order_by") == "created-desc":
            tasks.reverse()
        return tasks

    def child_ids(self, conn, task_id):
        return [child for parent, child in self.links if parent == task_id]

    def parent_ids(self, conn, task_id):
        return [parent for parent, child in self.links if child == task_id]

    def link_tasks(self, conn, parent_id, child_id):
        if (parent_id, child_id) not in self.links:
            self.links.append((parent_id, child_id))
        return False

    def unlink_tasks(self, conn, parent_id, child_id):
        edge = (parent_id, child_id)
        if edge in self.links:
            self.links.remove(edge)
            self.recompute_ready(conn)
            return True
        return False

    def recompute_ready(self, conn):
        """Mirror the real gate: a todo card whose parents are all terminal."""
        status = {t.id: t.status for t in self.retro_tasks}
        promoted = 0
        for card in self.retro_tasks:
            if card.status != "todo":
                continue
            parents = [p for p, c in self.links if c == card.id]
            if all(status.get(p) in ("done", "archived") for p in parents):
                card.status = "ready"
                promoted += 1
        return promoted

    def archive_task(self, conn, task_id, **kwargs):
        """Mirror ``kanban_db.archive_task``: terminal + release children."""
        for card in self.retro_tasks:
            if card.id == task_id and card.status != "archived":
                card.status = "archived"
                self.recompute_ready(conn)
                return True
        return False

    def add_comment(self, conn, task_id, author, body):
        self.comments.append((task_id, author, body))
        return len(self.comments)

    def create_task(self, conn, **kwargs):
        self.create_calls.append(kwargs)
        key = kwargs.get("idempotency_key")
        if key:
            for card in self.retro_tasks:
                if getattr(card, "idempotency_key", None) == key:
                    return card.id
        self._seq += 1
        new_id = f"t_retro{self._seq}"
        parents = list(kwargs.get("parents") or [])
        for parent in parents:
            self.links.append((parent, new_id))
        # Mirror kanban_db: a not-yet-done parent parks the child in ``todo``.
        status = "todo" if parents else "ready"
        self.retro_tasks.append(
            FakeTask(new_id, kwargs.get("title", ""), status=status,
                     idempotency_key=key, created_at=self._seq)
        )
        return new_id


class FakeTxn:
    """Stand-in for ``kanban_db_connect.write_txn`` (a context manager)."""

    def __init__(self, conn, **kwargs):
        self.conn = conn
        self.kwargs = kwargs
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self.conn

    def __exit__(self, *exc):
        return False


@pytest.fixture
def env(monkeypatch):
    """Install stubbed hermes_cli modules and a fresh plugin import.

    The age cap is disabled here (``queue_max_age_seconds: 0``) so the lane
    tests keep their seeded retros; the age-cap tests set their own cap.
    """
    config = {"plugins": {"entries": {"task-retro": {"queue_max_age_seconds": 0}}},
              "kanban": {"default_assignee": "worker"}}

    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.load_config_readonly = lambda: config

    fake_kb = types.ModuleType("hermes_cli.kanban_db")
    state = {}

    def get_task(conn, task_id):
        return state["kb"]._task

    def get_run(conn, run_id):
        return state["kb"]._run

    def list_runs(conn, task_id, **kwargs):
        return state["kb"].list_runs(conn, task_id, **kwargs)

    def list_tasks(conn, **kwargs):
        return state["kb"].list_tasks(conn, **kwargs)

    def child_ids(conn, task_id):
        return state["kb"].child_ids(conn, task_id)

    def parent_ids(conn, task_id):
        return state["kb"].parent_ids(conn, task_id)

    def link_tasks(conn, parent_id, child_id):
        return state["kb"].link_tasks(conn, parent_id, child_id)

    def unlink_tasks(conn, parent_id, child_id):
        return state["kb"].unlink_tasks(conn, parent_id, child_id)

    def recompute_ready(conn, failure_limit=None):
        return state["kb"].recompute_ready(conn)

    def create_task(conn, **kwargs):
        return state["kb"].create_task(conn, **kwargs)

    def archive_task(conn, task_id, **kwargs):
        return state["kb"].archive_task(conn, task_id, **kwargs)

    def add_comment(conn, task_id, author, body):
        return state["kb"].add_comment(conn, task_id, author, body)

    fake_kb.get_task = get_task
    fake_kb.get_run = get_run
    fake_kb.list_runs = list_runs
    fake_kb.list_tasks = list_tasks
    fake_kb.child_ids = child_ids
    fake_kb.parent_ids = parent_ids
    fake_kb.link_tasks = link_tasks
    fake_kb.unlink_tasks = unlink_tasks
    fake_kb.recompute_ready = recompute_ready
    fake_kb.create_task = create_task
    fake_kb.archive_task = archive_task
    fake_kb.add_comment = add_comment

    fake_kbc = types.ModuleType("hermes_cli.kanban_db_connect")
    fake_kbc.connect = lambda board=None, **kw: state["kb"].conn
    fake_kbc.write_txn = lambda conn, **kw: FakeTxn(conn, **kw)

    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []

    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake_kb)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db_connect", fake_kbc)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    sys.modules.pop("task_retro_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "task_retro_under_test", PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["task_retro_under_test"] = mod
    spec.loader.exec_module(mod)

    kb = FakeKB()
    kb.conn = FakeConn(kb=kb)
    state["kb"] = kb
    mod._config_cache = None  # fresh config read per test
    return types.SimpleNamespace(mod=mod, kb=kb, config=config, state=state)


def _hook(env, **overrides):
    payload = {
        "task_id": "t_done",
        "board": "default",
        "assignee": "worker",
        "run_id": 7,
        "profile_name": "worker",
        "summary": "did the thing",
    }
    payload.update(overrides)
    env.mod._on_kanban_task_completed(**payload)


def _parents(call):
    """``parents`` normalised to a list, so absent == []."""
    return list(call.get("parents") or [])


# --- registration -----------------------------------------------------------


def test_register_subscribes_the_completed_hook():
    captured = []
    skills = []

    class Ctx:
        def register_hook(self, name, cb):
            captured.append((name, cb))

        def register_skill(self, name, path, description=""):
            skills.append((name, path, description))

    spec = importlib.util.spec_from_file_location(
        "task_retro_reg", PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.register(Ctx())
    assert captured == [
        ("kanban_task_completed", mod._on_kanban_task_completed),
        ("kanban_task_blocked", mod._on_kanban_task_blocked),
    ]


def test_register_ships_the_skill_from_the_plugin_tree():
    """Self-contained: the skill must be registered from inside the plugin dir."""
    skills = []

    class Ctx:
        def register_hook(self, name, cb):
            pass

        def register_skill(self, name, path, description=""):
            skills.append((name, path, description))

    spec = importlib.util.spec_from_file_location(
        "task_retro_reg2", PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.register(Ctx())

    assert len(skills) == 1
    name, path, description = skills[0]
    assert name == "task-retrospective"
    assert description
    path = Path(path)
    assert path.name == "SKILL.md"
    assert path.is_file(), "the plugin must ship SKILL.md"
    assert path.parent.parent.parent == PLUGIN_DIR, (
        f"skill must live under the plugin dir, got {path}")
    assert path.parent.name == "task-retrospective"


def test_register_survives_a_missing_skill_tree(monkeypatch):
    """A missing tree must not stop the hook from registering."""
    spec = importlib.util.spec_from_file_location(
        "task_retro_reg3", PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_SKILL_DIR", Path("/nonexistent/skill/dir"))
    captured = []

    class Ctx:
        def register_hook(self, name, cb):
            captured.append(name)

        def register_skill(self, *a, **k):
            raise AssertionError("must not register a missing skill")

    mod.register(Ctx())
    assert captured == ["kanban_task_completed", "kanban_task_blocked"]


def test_shipped_skill_does_not_point_at_the_loose_skill_tree():
    """Self-contained: the skill must not reference ~/.hermes/skills/... paths."""
    base = PLUGIN_DIR / "skills" / "task-retrospective"
    text = (base / "SKILL.md").read_text()
    actions = (base / "references" / "actions.md").read_text()

    # No path may assume the retired loose-skill location; scripts are reached
    # through the skill directory the loader reports. (`~/.hermes/skills/` itself
    # legitimately appears as the write-gate-blocked target — only the retired
    # loose-skill path and the external write-gates reference must be gone.)
    assert "skills/autonomous-ai-agents/task-retrospective" not in text
    assert "autonomous-ai-agents/task-retrospective" not in actions
    # Every script invocation must be reached through the resolved skill dir, and
    # the library gate must scan the plugin's own copy rather than a home tree.
    assert "<skill_dir>/scripts/session_dump.py" in text
    assert "<skill_dir>/scripts/retro_lock.py" in text
    assert "python3 scripts/session_dump.py" not in text
    assert "python3 scripts/retro_lock.py" not in text
    assert "skill_tree_check.py --root <skill_dir>/.." in text
    assert "skill_tree_check.py --root ~/.hermes/skills" not in text

    # The write gates must be inlined: no external skill path may be required.
    # (Naming `nightly-self-improvement` as an out-of-scope target is fine; a
    # pointer that requires loading its reference file is not.)
    assert "nightly-silo-2-write-gates" not in text
    assert "nightly-silo-2-write-gates" not in actions
    assert "references/nightly" not in actions
    for gate in ("Already-covered check first", "Smallest justified correction", "Read back"):
        assert gate in actions, f"inlined write gate missing: {gate}"

    for ref in ("references/analysis.md", "references/actions.md",
                "scripts/session_dump.py", "scripts/retro_lock.py"):
        assert (base / ref).is_file(), ref


def test_register_skill_failure_is_swallowed(monkeypatch):
    """A broken register_skill must not abort plugin load."""
    spec = importlib.util.spec_from_file_location(
        "task_retro_reg4", PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    captured = []

    class Ctx:
        def register_hook(self, name, cb):
            captured.append(name)

        def register_skill(self, *a, **k):
            raise RuntimeError("registry exploded")

    mod.register(Ctx())  # must not raise
    assert captured == ["kanban_task_completed", "kanban_task_blocked"]


# --- happy path -------------------------------------------------------------


def test_creates_one_card_with_expected_fields(env):
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun(metadata={"worker_session_id": "20261021_120000_abc123"})
    _hook(env)

    assert len(env.kb.create_calls) == 1
    call = env.kb.create_calls[0]
    assert call["title"] == "retro: Build the widget"
    assert call["assignee"] == "worker"
    assert call["skills"] == ["task-retro:task-retrospective"]
    assert call["idempotency_key"] == "retro:t_done"
    assert call["workspace_kind"] == "scratch"
    assert "t_done" in call["body"]
    assert "did the thing" in call["body"]
    assert "20261021_120000_abc123" in call["body"]
    assert "task-retro:task-retrospective" in call["body"]
    assert 'skill_view(name="task-retro:task-retrospective")' in call["body"]
    assert env.kb.conn.closed is True


def test_no_active_retro_means_no_parent_link(env):
    """First retro (nothing open) runs immediately: no parent edge."""
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert _parents(env.kb.create_calls[0]) == []


def test_no_creator_task_id_ever(env):
    """It would inherit notify subs / auto-subscribe a chat."""
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert "creator_task_id" not in env.kb.create_calls[0]


def _seed_lanes(kb, count, status="running"):
    """Seed ``count`` independent open retros (each its own lane), oldest first."""
    kb.retro_tasks = [
        FakeTask(f"t_lane{i}", f"retro: lane {i}", status=status, created_at=i + 1)
        for i in range(count)
    ]
    return [f"t_lane{i}" for i in range(count)]


def test_free_lane_means_no_parent(env):
    """0/1/2 open retros -> a free lane, so the new retro starts immediately."""
    for count in (0, 1, 2):
        env.kb.retro_tasks = []
        env.kb.links = []
        env.kb.create_calls.clear()
        _seed_lanes(env.kb, count)
        env.kb._task = FakeTask("t_done", "Build the widget")
        env.kb._run = FakeRun()
        _hook(env)
        assert _parents(env.kb.create_calls[0]) == [], count
        assert env.kb.retro_tasks[-1].status == "ready", count


def test_full_board_chains_behind_the_oldest_lane_tail(env):
    """3 open tails -> the new retro becomes a child of the OLDEST tail."""
    lanes = _seed_lanes(env.kb, 3)
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert _parents(env.kb.create_calls[0]) == [lanes[0]]
    assert env.kb.retro_tasks[-1].status == "todo"


def test_round_robin_across_three_lanes(env):
    """7 retros A..G land in lanes A->D->G, B->E, C->F."""
    for i in range(7):
        env.kb._task = FakeTask(f"t_done{i}", f"Build the widget {i}")
        env.kb._run = FakeRun()
        _hook(env, task_id=f"t_done{i}")

    calls = env.kb.create_calls
    assert len(calls) == 7
    # first three free lanes
    assert [_parents(c) for c in calls[:3]] == [[], [], []]
    # D behind A, E behind B, F behind C, G behind D
    assert _parents(calls[3]) == ["t_retro1"]
    assert _parents(calls[4]) == ["t_retro2"]
    assert _parents(calls[5]) == ["t_retro3"]
    assert _parents(calls[6]) == ["t_retro4"]


def test_oldest_lane_tail_wins_same_second_ties(env):
    """Same-second retros tie on created_at, so the id tie-break must be stable.

    Three open lanes; whichever way ``list_tasks`` orders the tie, the oldest
    (lowest id) tail is chosen — never a newer one.
    """
    seeded = [
        FakeTask("t_a", "retro: a", status="todo", created_at=5),
        FakeTask("t_b", "retro: b", status="todo", created_at=5),
        FakeTask("t_c", "retro: c", status="todo", created_at=5),
    ]
    for ordering in (list(seeded), list(reversed(seeded))):
        env.kb.retro_tasks = list(ordering)
        env.kb.links = []
        env.kb.create_calls.clear()
        env.kb._task = FakeTask("t_done", "Build the widget")
        env.kb._run = FakeRun()
        _hook(env)
        assert _parents(env.kb.create_calls[0]) == ["t_a"], [t.id for t in ordering]


def test_blocked_parent_is_ignored(env):
    """A blocked retro is not an open lane tail, so it never gates the next."""
    env.kb.retro_tasks = [FakeTask("t_blocked", "retro: stuck", status="blocked")]
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert _parents(env.kb.create_calls[0]) == []


def test_done_parent_is_ignored(env):
    env.kb.retro_tasks = [FakeTask("t_done_retro", "retro: finished", status="done")]
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert _parents(env.kb.create_calls[0]) == []


def test_archived_parent_is_ignored(env):
    env.kb.retro_tasks = [FakeTask("t_arch", "retro: archived", status="archived")]
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert _parents(env.kb.create_calls[0]) == []


def test_blocked_retro_does_not_hide_an_open_one_behind_it(env):
    """Only open retros are lane tails, so a blocked one is skipped over."""
    lanes = _seed_lanes(env.kb, 3)
    env.kb.retro_tasks.append(FakeTask("t_blocked", "retro: stuck", status="blocked"))
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert _parents(env.kb.create_calls[0]) == [lanes[0]]


def test_non_retro_open_cards_are_not_lane_tails(env):
    """Only ``retro:`` cards count as lanes, even when open."""
    env.kb.retro_tasks = [FakeTask("t_other", "some other running card",
                                   status="running")]
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert _parents(env.kb.create_calls[0]) == []


def test_link_failure_still_creates_the_retro(env):
    """Fail open: a raising link call must not lose the retro card."""
    _seed_lanes(env.kb, 3)
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()

    real = env.kb.create_task
    attempts = []

    def flaky(conn, **kwargs):
        attempts.append(list(kwargs.get("parents") or []))
        if kwargs.get("parents"):
            raise RuntimeError("database is locked")
        return real(conn, **kwargs)

    sys.modules["hermes_cli.kanban_db"].create_task = flaky
    _hook(env)  # must not raise

    assert attempts == [["t_lane0"], []]  # linked attempt, then unparented retry
    assert len(env.kb.create_calls) == 1
    assert _parents(env.kb.create_calls[0]) == []
    assert env.kb.retro_tasks[-1].id == "t_retro1"


def test_lane_lookup_failure_still_creates_the_retro(env):
    """Fail open: an unreadable board must not lose the retro card."""
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()

    def boom(conn, **kwargs):
        raise RuntimeError("no such table: tasks")

    sys.modules["hermes_cli.kanban_db"].list_tasks = boom
    _hook(env)  # must not raise
    assert len(env.kb.create_calls) == 1
    assert _parents(env.kb.create_calls[0]) == []


def test_assignee_comes_from_config_not_hardcoded(env):
    env.config["kanban"]["default_assignee"] = "specialist"
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert env.kb.create_calls[0]["assignee"] == "specialist"


# --- session id resolution --------------------------------------------------


def test_session_id_falls_back_to_env(env, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "20261021_999999_env")
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun(metadata={})
    _hook(env)
    assert "20261021_999999_env" in env.kb.create_calls[0]["body"]


def test_session_id_absent_is_stated_in_body(env):
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun(metadata=None)
    _hook(env)
    body = env.kb.create_calls[0]["body"]
    assert "NOT AVAILABLE" in body
    assert "state.db" in body


# --- idempotency ------------------------------------------------------------


def test_idempotency_key_is_stable_across_repeated_hooks(env):
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    _hook(env)
    keys = {c["idempotency_key"] for c in env.kb.create_calls}
    assert keys == {"retro:t_done"}


def test_idempotency_key_survives_the_chain(env):
    """A chained retry keeps the same key, so it cannot duplicate."""
    _seed_lanes(env.kb, 3)
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    # A re-fired completion for the SAME task must not mint a second retro: the
    # key already exists, so the hook skips (this is what protects the archived
    # case too — ``create_task`` alone ignores archived rows).
    _hook(env)
    assert {c["idempotency_key"] for c in env.kb.create_calls} == {"retro:t_done"}
    assert len(env.kb.create_calls) == 1
    assert _parents(env.kb.create_calls[0]) == ["t_lane0"]
    assert [t.id for t in env.kb.retro_tasks][-1] == "t_retro1"


# --- loop guard -------------------------------------------------------------


def test_loop_guard_by_title_prefix(env):
    env.kb._task = FakeTask("t_done", "retro: Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert env.kb.create_calls == []


def test_loop_guard_by_skill(env):
    env.kb._task = FakeTask("t_done", "Build the widget", skills=["task-retrospective"])
    env.kb._run = FakeRun()
    _hook(env)
    assert env.kb.create_calls == []


def test_loop_guard_by_qualified_skill(env):
    """The plugin now writes the qualified name; it must still suppress a retro."""
    env.kb._task = FakeTask("t_done", "Build the widget",
                            skills=["task-retro:task-retrospective"])
    env.kb._run = FakeRun()
    _hook(env)
    assert env.kb.create_calls == []


def test_loop_guard_ignores_unrelated_skills(env):
    """A skill-name near-miss must not silently suppress a legitimate retro."""
    env.kb._task = FakeTask("t_done", "Build the widget", skills=["task-retrospective-x"])
    env.kb._run = FakeRun()
    _hook(env)
    assert len(env.kb.create_calls) == 1


# --- probe / throwaway filter -----------------------------------------------


@pytest.mark.parametrize("title", [
    "[probe] task-retro live end-to-end (throwaway)",
    "[PROBE] something",
    "probe: live end-to-end",
    "PROBE: live end-to-end",
    "Probe: Mixed Case",
    "a throwaway experiment",
    "THROWAWAY check",
    "  [probe] leading space  ",
])
def test_probe_titles_are_skipped(env, title):
    env.kb._task = FakeTask("t_probe", title)
    env.kb._run = FakeRun()
    _hook(env)
    assert env.kb.create_calls == []


@pytest.mark.parametrize("title", [
    "Build the widget",
    "probing the widget",          # substring but not the marker
    "reprobe the widget",
    "throw away the widget",       # spaced, not the marker
    "task: probe the widget",
])
def test_non_probe_titles_are_created(env, title):
    env.kb._task = FakeTask("t_done", title)
    env.kb._run = FakeRun()
    _hook(env)
    assert len(env.kb.create_calls) == 1


def test_probe_marker_list_is_a_small_constant(env):
    mod = env.mod
    assert isinstance(mod._PROBE_MARKERS, tuple)
    assert len(mod._PROBE_MARKERS) <= 4
    assert "[probe]" in mod._PROBE_MARKERS
    assert "throwaway" in mod._PROBE_MARKERS
    assert mod._PROBE_PREFIX == "probe:"


def test_probe_filter_runs_before_the_lane(env):
    """A probe completion must not consume a lane slot."""
    _seed_lanes(env.kb, 3)
    env.kb._task = FakeTask("t_probe", "[probe] live check")
    env.kb._run = FakeRun()
    _hook(env)
    assert env.kb.create_calls == []


# --- self-containment -------------------------------------------------------


def test_plugin_module_imports_nothing_outside_stdlib_at_module_level():
    """Self-contained: the plugin's module level must be stdlib-only.

    ``hermes_cli`` is imported lazily inside functions (it is absent outside the
    gateway), so any module-level import beyond the stdlib would break plugin
    load in a plain interpreter.
    """
    import ast

    tree = ast.parse((PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8"))
    allowed = set(getattr(sys, "stdlib_module_names", ()) or _STDLIB_FALLBACK)
    module_level = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module:
                module_level.add(node.module.split(".")[0])

    assert module_level <= allowed, sorted(module_level - allowed)
    assert "hermes_cli" not in module_level


def test_plugin_has_no_sibling_plugin_imports():
    """Self-contained: nothing may be imported from another plugin's tree."""
    text = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    for banned in ("agent_guards", "agent-guards", "hindsight", "langfuse",
                   "hermes_cli.plugins"):
        assert banned not in text, banned


def test_plugin_tree_is_the_only_source_of_its_skill():
    """The skill path is derived from the plugin dir, never a loose skills tree."""
    text = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    assert "_SKILL_DIR = Path(__file__).resolve().parent" in text
    # the loose tree may only be named as the NOT-copied-into target
    assert "not copied under" in text


# --- filters ----------------------------------------------------------------


def test_skips_trivial_card(env):
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun(started_at=1000, ended_at=1060)  # 60s < 900s default
    _hook(env)
    assert env.kb.create_calls == []


def test_card_wall_899_is_skipped_and_900_is_created(env):
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun(started_at=1000, ended_at=1899)  # 899s
    _hook(env)
    assert env.kb.create_calls == []

    env.kb._run = FakeRun(started_at=1000, ended_at=1900)  # 900s
    _hook(env)
    assert len(env.kb.create_calls) == 1


def test_gate_measures_the_parent_card_total_not_one_run(env):
    """The unit is the card's TOTAL wall: the sum of every run it had."""
    env.kb._task = FakeTask("t_done", "Build the widget")
    # one 400 s run -> 400 s card -> skipped
    env.kb.runs = [FakeRun(started_at=0, ended_at=400)]
    env.kb._run = FakeRun(started_at=0, ended_at=400)
    _hook(env)
    assert env.kb.create_calls == []

    # two 400 s runs -> 800 s card -> still skipped
    env.kb.runs = [FakeRun(started_at=0, ended_at=400),
                   FakeRun(started_at=400, ended_at=800)]
    env.kb._run = FakeRun(started_at=400, ended_at=800)
    _hook(env)
    assert env.kb.create_calls == []

    # 1000 s total -> retroed
    env.kb.runs = [FakeRun(started_at=0, ended_at=600),
                   FakeRun(started_at=600, ended_at=1000)]
    env.kb._run = FakeRun(started_at=600, ended_at=1000)
    _hook(env)
    assert len(env.kb.create_calls) == 1


def test_default_threshold_is_900(env):
    assert env.mod._DEFAULT_MIN_DURATION_SECONDS == 900
    schema = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    assert "default: 900" in schema


def test_trivial_threshold_is_configurable(env):
    env.config["plugins"]["entries"]["task-retro"] = {"min_duration_seconds": 30}
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun(started_at=1000, ended_at=1060)  # 60s >= 30s
    _hook(env)
    assert len(env.kb.create_calls) == 1


def test_unreadable_run_history_falls_back_to_the_completing_run(env):
    """Fail-safe: an unreadable history keeps the old single-run measure."""
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun(started_at=1000, ended_at=1060)  # 60s

    def boom(conn, task_id, **kwargs):
        raise RuntimeError("no such table: task_runs")

    sys.modules["hermes_cli.kanban_db"].list_runs = boom
    _hook(env)  # must not raise
    assert env.kb.create_calls == []


def test_skips_non_completed_outcome(env):
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun(outcome="review_requested")
    _hook(env)
    assert env.kb.create_calls == []


def test_skips_when_plugin_disabled(env):
    env.config["plugins"]["entries"]["task-retro"] = {"enabled": False}
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert env.kb.create_calls == []


def test_skips_when_run_missing(env):
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = None
    _hook(env)
    assert env.kb.create_calls == []


def test_skips_when_task_missing(env):
    env.kb._task = None
    env.kb._run = FakeRun()
    _hook(env)
    assert env.kb.create_calls == []


# --- serialization replaces the old no-throttle property --------------------


def test_every_completion_still_creates_its_card(env):
    """Serialization never drops a retro: 25 completions -> 25 cards."""
    for i in range(25):
        env.kb._task = FakeTask(f"t_done{i}", f"Build the widget {i}")
        env.kb._run = FakeRun()
        _hook(env, task_id=f"t_done{i}")
    assert len(env.kb.create_calls) == 25


def test_plugin_config_has_no_parallel_keys(env):
    """The cap is the lane chain, not a dispatcher/parallel knob.

    ``queue_max_age_seconds`` is the age cap on OPEN retros (a bounded-backlog
    rule), not a parallel/queue-size knob, so it is not in the banned list.
    """
    schema = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    for banned in ("max_parallel", "throttle", "concurrency", "max_active"):
        assert banned not in schema


def test_max_active_retros_is_three(env):
    assert env.mod._MAX_ACTIVE_RETROS == 3


# --- fail open --------------------------------------------------------------


def test_create_task_failure_is_swallowed(env):
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()

    def boom(conn, **kwargs):
        raise RuntimeError("sqlite is locked")

    sys.modules["hermes_cli.kanban_db"].create_task = boom
    _hook(env)  # must not raise


def test_get_task_failure_is_swallowed(env):
    env.kb._run = FakeRun()

    def boom(conn, task_id):
        raise RuntimeError("no such table")

    sys.modules["hermes_cli.kanban_db"].get_task = boom
    _hook(env)  # must not raise


def test_connect_failure_is_swallowed(env):
    def boom(board=None, **kw):
        raise RuntimeError("database is locked")

    sys.modules["hermes_cli.kanban_db_connect"].connect = boom
    _hook(env)  # must not raise


def test_connect_uses_pinned_board_not_literal_default(env):
    """The hook payload says board="default"; passing that back in would make
    kanban_db resolve ``kanban_home()/kanban.db`` and ignore HERMES_KANBAN_DB."""
    seen = {}

    def spy_connect(board=None, **kw):
        seen["board"] = board
        return env.kb.conn

    sys.modules["hermes_cli.kanban_db_connect"].connect = spy_connect
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert seen["board"] is None, f"expected the pin (None), got {seen['board']!r}"


def test_connect_passes_through_a_named_board(env):
    seen = {}

    def spy_connect(board=None, **kw):
        seen["board"] = board
        return env.kb.conn

    sys.modules["hermes_cli.kanban_db_connect"].connect = spy_connect
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env, board="release")
    assert seen["board"] == "release"


def test_missing_run_is_skipped(env):
    """No run row at all -> no outcome -> not a completed run -> skip.

    ``min_duration_seconds=0`` so the trivial-run gate cannot mask this: the
    outcome gate must be what stops it.
    """
    env.config["plugins"]["entries"]["task-retro"] = {"min_duration_seconds": 0}
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = None
    _hook(env)
    assert env.kb.create_calls == []


def test_missing_task_id_is_swallowed(env):
    _hook(env, task_id=None)  # must not raise


def test_unreadable_config_is_swallowed(env):
    def boom():
        raise RuntimeError("config.yaml corrupt")

    sys.modules["hermes_cli.config"].load_config_readonly = boom
    env.mod._config_cache = None
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)  # must not raise


def test_hook_returns_none(env):
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    assert _hook(env) is None


# --- F1: atomic tail-selection + insertion ----------------------------------


def test_tail_lookup_and_insert_run_inside_one_write_txn(env):
    """F1: the tail read and the insert must share ONE write transaction.

    Without the outer ``write_txn`` two workers completing in the same instant
    both read "no open retro" and both insert a ready retro.
    """
    entered = []
    sys.modules["hermes_cli.kanban_db_connect"].write_txn = (
        lambda conn, **kw: entered.append(True) or FakeTxn(conn, **kw))

    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert entered == [True]
    assert len(env.kb.create_calls) == 1
    assert _parents(env.kb.create_calls[0]) == []


def test_tail_lookup_sees_the_parent_inside_the_transaction(env):
    """The parent chosen inside the txn is the lane tail, not a pre-read value."""
    _seed_lanes(env.kb, 3)
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert _parents(env.kb.create_calls[0]) == ["t_lane0"]


def test_write_txn_failure_still_creates_the_retro_unparented(env):
    """Fail-open: a transaction/lock failure must never drop the retro."""
    import contextlib

    @contextlib.contextmanager
    def boom(conn, **kw):
        raise RuntimeError("database is locked")
        yield conn  # pragma: no cover

    sys.modules["hermes_cli.kanban_db_connect"].write_txn = boom
    _seed_lanes(env.kb, 3)
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)  # must not raise
    assert len(env.kb.create_calls) == 1
    assert _parents(env.kb.create_calls[0]) == []          # unparented retry
    assert env.kb.retro_tasks[-1].status == "ready"


def test_missing_write_txn_falls_back_and_still_creates_the_retro(env):
    """An older/absent ``write_txn`` must not lose the retro."""
    del sys.modules["hermes_cli.kanban_db_connect"].write_txn
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)  # must not raise
    assert len(env.kb.create_calls) == 1


def test_create_task_failure_inside_the_txn_retries_unparented(env):
    """Fail-open: a link/create failure retries outside the transaction."""
    _seed_lanes(env.kb, 3)
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()

    real = env.kb.create_task
    attempts = []

    def flaky(conn, **kwargs):
        attempts.append(list(kwargs.get("parents") or []))
        if kwargs.get("parents"):
            raise RuntimeError("database is locked")
        return real(conn, **kwargs)

    sys.modules["hermes_cli.kanban_db"].create_task = flaky
    _hook(env)  # must not raise
    assert attempts == [["t_lane0"], []]
    assert len(env.kb.create_calls) == 1
    assert _parents(env.kb.create_calls[0]) == []


def test_archived_retro_still_protects_the_idempotency_key(env):
    """N2: ``create_task`` ignores archived rows, so the plugin must not."""
    env.kb.retro_tasks = [FakeTask("t_arch", "retro: earlier", status="archived",
                                   idempotency_key="retro:t_done")]
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert env.kb.create_calls == []
    assert [t.id for t in env.kb.retro_tasks] == ["t_arch"]


def test_existing_retro_key_skips_even_when_not_archived(env):
    env.kb.retro_tasks = [FakeTask("t_live", "retro: earlier", status="running",
                                   idempotency_key="retro:t_done")]
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert env.kb.create_calls == []


def test_idempotency_read_failure_still_creates_the_retro(env):
    """Fail-open: an unreadable key lookup must not drop the retro."""
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()

    def boom(sql, params=()):
        raise RuntimeError("no such table: tasks")

    env.kb.conn.execute = boom
    _hook(env)  # must not raise
    assert len(env.kb.create_calls) == 1


# --- blocked hook + parked-retro sweep --------------------------------------


def test_blocked_hook_archives_a_retro(env):
    """A blocked retro is archived, releasing its queued children."""
    env.kb.retro_tasks = [
        FakeTask("t_A", "retro: A", status="blocked", created_at=1),
        FakeTask("t_D", "retro: D", status="todo", created_at=2),
    ]
    env.kb.links = [("t_A", "t_D")]
    env.kb._task = FakeTask("t_A", "retro: A", status="blocked")
    env.mod._on_kanban_task_blocked(
        task_id="t_A", board="default", reason="needs input")
    status = {t.id: t.status for t in env.kb.retro_tasks}
    assert status["t_A"] == "archived"
    assert status["t_D"] == "ready"           # released by recompute_ready


def test_blocked_hook_ignores_non_retro_cards(env):
    """A real work card blocked is never archived."""
    env.kb.retro_tasks = [FakeTask("t_real", "Build the widget", status="blocked")]
    env.kb._task = FakeTask("t_real", "Build the widget", status="blocked")
    env.mod._on_kanban_task_blocked(task_id="t_real", board="default", reason="x")
    assert env.kb.retro_tasks[0].status == "blocked"


def test_blocked_hook_fail_open(env):
    """A missing task row / broken connect must not raise."""
    env.kb._task = None
    env.mod._on_kanban_task_blocked(task_id="t_missing", board="default")

    def boom(board=None, **kw):
        raise RuntimeError("database is locked")

    sys.modules["hermes_cli.kanban_db_connect"].connect = boom
    env.mod._on_kanban_task_blocked(task_id="t_A", board="default")  # must not raise


def test_blocked_hook_skipped_when_disabled(env):
    env.config["plugins"]["entries"]["task-retro"] = {"enabled": False}
    env.kb.retro_tasks = [FakeTask("t_A", "retro: A", status="blocked")]
    env.kb._task = FakeTask("t_A", "retro: A", status="blocked")
    env.mod._on_kanban_task_blocked(task_id="t_A", board="default")
    assert env.kb.retro_tasks[0].status == "blocked"


def test_completed_hook_sweeps_parked_retros(env):
    """Safety net: a blocked/triage retro is archived on the next completion."""
    env.kb.retro_tasks = [
        FakeTask("t_blocked", "retro: stuck", status="blocked", created_at=1),
        FakeTask("t_triage", "retro: triaged", status="triage", created_at=2),
        FakeTask("t_review", "retro: in review", status="review", created_at=3),
    ]
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    status = {t.id: t.status for t in env.kb.retro_tasks}
    assert status["t_blocked"] == "archived"
    assert status["t_triage"] == "archived"
    assert status["t_review"] == "review"     # review is never swept


def test_completed_hook_sweep_is_fail_open(env):
    """An unreadable board in the sweep must not stop the retro creation."""
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()

    def boom(conn, **kwargs):
        raise RuntimeError("no such table: tasks")

    sys.modules["hermes_cli.kanban_db"].list_tasks = boom
    _hook(env)  # must not raise
    assert len(env.kb.create_calls) == 1


# --- queue age cap -----------------------------------------------------------


def _set_cap(env, seconds):
    env.config["plugins"]["entries"].setdefault("task-retro", {})[
        "queue_max_age_seconds"] = seconds


def test_old_queued_retro_is_archived_and_child_promotes(env):
    """An OPEN retro older than the cap is archived, naming its parent."""
    _set_cap(env, 6 * 3600)
    now = int(time.time())
    env.kb.retro_tasks = [
        FakeTask("t_old", "retro: old", status="todo", created_at=now - 7 * 3600,
                 idempotency_key="retro:t_parent_old"),
        FakeTask("t_child", "retro: child", status="todo", created_at=now - 60),
    ]
    env.kb.links = [("t_old", "t_child")]
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)

    status = {t.id: t.status for t in env.kb.retro_tasks}
    assert status["t_old"] == "archived"
    assert status["t_child"] == "ready", "the queued child must be released"
    comment = next(c for c in env.kb.comments if c[0] == "t_old")
    assert "skipped for age" in comment[2]
    assert "t_parent_old" in comment[2], "the reason must name the parent"


def test_fresh_queued_retro_is_untouched(env):
    _set_cap(env, 6 * 3600)
    now = int(time.time())
    env.kb.retro_tasks = [
        FakeTask("t_fresh", "retro: fresh", status="todo", created_at=now - 60),
    ]
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert {t.id: t.status for t in env.kb.retro_tasks}["t_fresh"] == "todo"
    assert env.kb.comments == []


def test_running_retro_is_never_aged_out(env):
    _set_cap(env, 6 * 3600)
    now = int(time.time())
    env.kb.retro_tasks = [
        FakeTask("t_run", "retro: running", status="running",
                 created_at=now - 48 * 3600),
    ]
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert {t.id: t.status for t in env.kb.retro_tasks}["t_run"] == "running"


def test_age_cap_zero_disables_the_sweep(env):
    _set_cap(env, 0)
    env.kb.retro_tasks = [
        FakeTask("t_ancient", "retro: ancient", status="todo", created_at=1),
    ]
    env.kb._task = FakeTask("t_done", "Build the widget")
    env.kb._run = FakeRun()
    _hook(env)
    assert {t.id: t.status for t in env.kb.retro_tasks}["t_ancient"] == "todo"


def test_default_queue_max_age_is_six_hours(env):
    assert env.mod._DEFAULT_QUEUE_MAX_AGE_SECONDS == 6 * 3600
    assert env.mod._queue_max_age_seconds({}) == 6 * 3600
    assert env.mod._queue_max_age_seconds({"queue_max_age_seconds": "junk"}) == 6 * 3600
    assert env.mod._queue_max_age_seconds({"queue_max_age_seconds": 60}) == 60
    schema = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    assert "queue_max_age_seconds" in schema
    assert "default: 21600" in schema


def test_open_queue_statuses_exclude_running(env):
    assert env.mod._OPEN_QUEUE_STATUSES == ("todo", "ready")

