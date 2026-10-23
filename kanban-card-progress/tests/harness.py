"""Isolated test harness: temp board + temp worker state.db, plus package loading.

Every test that touches a board or a worker DB runs inside :class:`Env`, which

* points ``HERMES_KANBAN_HOME`` at a fresh temp root and **pops
  ``HERMES_KANBAN_DB``** (the worker env pins it, which is how an ad-hoc run
  silently hits the real board — see the prototype report),
* asserts the resolved board DB is under that temp root,
* points ``HERMES_HOME`` at the same temp root so the plugin's config read
  cannot see the real profile,
* restores the environment on exit.

``Env.state_db`` builds a WAL-mode worker ``state.db`` with the real ``messages``
schema and an active writer, so the read path can be exercised end to end.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
SRC = os.environ.get("HERMES_AGENT_SRC") or str(Path.home() / ".hermes" / "hermes-agent")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

_MESSAGES_DDL = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_messages_session_id ON messages(session_id, id);
"""


def load_package():
    """Load this plugin directory as a package, so its relative imports work."""
    name = "kanban_card_progress_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_progress_block():
    """Load ``progress_block.py`` as a top-level module (the format tests)."""
    name = "progress_block"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "progress_block.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Env:
    """A temp ``HERMES_HOME`` / ``HERMES_KANBAN_HOME`` with an initialized board."""

    _SAVED_KEYS = (
        "HERMES_HOME",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    )

    def __enter__(self) -> "Env":
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "fakehermes"
        (self.home / "kanban").mkdir(parents=True)
        self._saved = {k: os.environ.get(k) for k in self._SAVED_KEYS}
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ["HERMES_KANBAN_HOME"] = str(self.home)
        os.environ["HERMES_KANBAN_BOARD"] = "default"
        for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_TASK",
                    "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT"):
            os.environ.pop(key, None)

        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        self.kb, self.kbc = kb, kbc
        self.path = kbc.init_db()
        resolved = Path(kb.kanban_db_path()).resolve()
        """Isolation proof: never the real board."""
        assert str(resolved).startswith(str(self.home.resolve())), f"NOT ISOLATED: {resolved}"
        assert resolved == Path(self.path).resolve()
        self.conn = kbc.connect()
        self.pkg = load_package()
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self.pkg._shutdown()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    """--- board helpers ----------------------------------------------------"""

    def task(self, *, title: str = "probe", body: str = "Original body.",
             assignee: str = "worker", status: str = "todo") -> str:
        tid = self.kb.create_task(self.conn, title=title, body=body, assignee=assignee)
        if status != "todo":
            self.conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
            self.conn.commit()
        return tid

    def task_with_id(self, task_id: str, *, title: str = "probe",
                     body: str = "Original body.", assignee: str = "worker",
                     status: str = "todo") -> str:
        """Insert a task row with a *fixed* id (deterministic sort order).

        ``create_task`` mints a random hex id, which makes a ``LIMIT``-ordered
        query nondeterministic. Tests that assert on ordering seed explicit ids.
        """
        self.conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, created_at, "
            "workspace_kind) VALUES (?, ?, ?, ?, ?, ?, 'scratch')",
            (task_id, title, body, assignee, status, int(time.time())),
        )
        self.conn.commit()
        return task_id

    def running(self, **kw) -> str:
        tid = self.task(**kw)
        self.kb.claim_task(self.conn, tid, claimer="test")
        return tid

    def body(self, tid: str):
        row = self.conn.execute("SELECT body FROM tasks WHERE id = ?", (tid,)).fetchone()
        return row["body"] if row else None

    def status(self, tid: str):
        row = self.conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        return row["status"] if row else None

    def set_status(self, tid: str, status: str) -> None:
        self.conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        self.conn.commit()

    """--- worker state.db --------------------------------------------------"""

    def state_db(self, session_id: str, *, owner_for: str | None = None) -> Path:
        """Create a WAL-mode worker ``state.db`` and (optionally) admit ``owner_for``.

        Distinct session ids get distinct files, so several synthetic workers can
        coexist in one test.
        """
        path = self.home / f"worker-state-{session_id}.db"
        if not path.exists():
            conn = sqlite3.connect(path)
            conn.executescript(_MESSAGES_DDL)
            assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
            conn.commit()
            conn.close()
        if owner_for is not None:
            self.admit(owner_for, str(path), session_id)
        return path

    def admit(self, task_id: str, db_path: str, session_id: str, run_id: int | None = None) -> None:
        """Append an ``owner_admitted`` event, exactly as the gateway owner does."""
        import json

        self.conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'owner_admitted', ?, 0)",
            (task_id, run_id, json.dumps({"db": db_path, "session_id": session_id,
                                          "request_id": f"kanban:[\"default\",\"{task_id}\",1]",
                                          "pid": os.getpid(), "birth": 0})),
        )
        self.conn.commit()


def append_todo(db_path: Path, session_id: str, payload: str, *, tool_name: str = "todo_list",
                active: int = 1, compacted: int = 0, tool_call_id: str = "call-1") -> int:
    """Append a tool-result row to a synthetic worker ``state.db``; returns its id."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, "
            "timestamp, active, compacted) VALUES (?, 'tool', ?, ?, ?, 0, ?, ?)",
            (session_id, payload, tool_call_id, tool_name, active, compacted),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def todo_payload(*items) -> str:
    """Build a real ``todo_list`` result JSON from ``(status, content)`` pairs."""
    import json

    todos = [
        {"id": f"item-{i}", "content": text, "status": status}
        for i, (status, text) in enumerate(items)
    ]
    return json.dumps({"todos": todos, "revision": 1,
                       "summary": {"total": len(todos)}}, ensure_ascii=False)
