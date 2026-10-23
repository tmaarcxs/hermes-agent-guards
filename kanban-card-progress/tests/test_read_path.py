"""The read path: owner_admitted -> (state.db, session_id) -> latest todo row.

The WAL test is the one that matters: ``mode=ro`` must see a row committed by a
live writer in the same instant that ``immutable=1`` does not.
"""

from __future__ import annotations

import sqlite3
import unittest

from harness import Env, append_todo, todo_payload


class TestOwnerRef(unittest.TestCase):
    def test_resolves_latest_owner_admitted(self):
        with Env() as env:
            tid = env.running()
            env.admit(tid, "/tmp/one.db", "sess-one", run_id=1)
            env.admit(tid, "/tmp/two.db", "sess-two", run_id=2)
            self.assertEqual(env.pkg.todo_reader.owner_ref(env.conn, tid),
                             ("/tmp/two.db", "sess-two"))

    def test_none_without_owner_admitted(self):
        with Env() as env:
            tid = env.task()
            self.assertIsNone(env.pkg.todo_reader.owner_ref(env.conn, tid))

    def test_malformed_payload_is_none(self):
        with Env() as env:
            tid = env.running()
            env.conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'owner_admitted', ?, 0)", (tid, "{not json"))
            env.conn.commit()
            self.assertIsNone(env.pkg.todo_reader.owner_ref(env.conn, tid))

    def test_payload_missing_fields_is_none(self):
        with Env() as env:
            tid = env.running()
            env.conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'owner_admitted', ?, 0)", (tid, '{"db": "/x.db"}'))
            env.conn.commit()
            self.assertIsNone(env.pkg.todo_reader.owner_ref(env.conn, tid))


class TestReadTodoContent(unittest.TestCase):
    def test_returns_newest_row(self):
        with Env() as env:
            db = env.state_db("sess-1")
            append_todo(db, "sess-1", todo_payload(("pending", "old")))
            append_todo(db, "sess-1", todo_payload(("completed", "new")))
            content = env.pkg.todo_reader.read_todo_content(str(db), "sess-1")
            self.assertIn("new", content)
            self.assertNotIn("old", content)

    def test_filters_other_sessions(self):
        with Env() as env:
            db = env.state_db("sess-1")
            append_todo(db, "other", todo_payload(("pending", "foreign")))
            self.assertIsNone(env.pkg.todo_reader.read_todo_content(str(db), "sess-1"))

    def test_accepts_legacy_todo_tool_name(self):
        with Env() as env:
            db = env.state_db("sess-1")
            append_todo(db, "sess-1", todo_payload(("pending", "legacy")), tool_name="todo")
            self.assertIn("legacy", env.pkg.todo_reader.read_todo_content(str(db), "sess-1"))

    def test_ignores_compacted_only_when_inactive_and_uncompacted(self):
        with Env() as env:
            db = env.state_db("sess-1")
            append_todo(db, "sess-1", todo_payload(("pending", "hidden")),
                        active=0, compacted=0)
            self.assertIsNone(env.pkg.todo_reader.read_todo_content(str(db), "sess-1"))
            append_todo(db, "sess-1", todo_payload(("pending", "kept")),
                        active=0, compacted=1)
            self.assertIn("kept", env.pkg.todo_reader.read_todo_content(str(db), "sess-1"))

    def test_missing_db_is_none_not_raise(self):
        with Env() as env:
            self.assertIsNone(env.pkg.todo_reader.read_todo_content("/nope/missing.db", "s"))
            self.assertIsNone(env.pkg.todo_reader.read_todo_content("", "s"))
            self.assertIsNone(env.pkg.todo_reader.read_todo_content("/x.db", ""))

    def test_uninitialized_db_is_none_not_raise(self):
        with Env() as env:
            bogus = env.home / "bogus.db"
            bogus.write_bytes(b"not a database")
            self.assertIsNone(env.pkg.todo_reader.read_todo_content(str(bogus), "s"))

    def test_mode_ro_sees_fresh_row_where_immutable_would_not(self):
        """A live writer's committed row is visible to ``mode=ro``, invisible to ``immutable=1``."""
        with Env() as env:
            db = env.state_db("sess-wal")
            """A long-lived writer holding the WAL open."""
            writer = sqlite3.connect(db)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute(
                    "INSERT INTO messages (session_id, role, content, tool_name, timestamp) "
                    "VALUES ('sess-wal', 'tool', ?, 'todo_list', 0)",
                    (todo_payload(("pending", "from-wal")),),
                )
                writer.commit()

                ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    ro_count = ro.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                finally:
                    ro.close()

                imm = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
                try:
                    imm_count = imm.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                finally:
                    imm.close()

                self.assertEqual(ro_count, 1, "mode=ro must see the committed WAL row")
                self.assertLess(imm_count, ro_count,
                                "immutable=1 is expected to miss the WAL row")

                """And the production reader agrees with mode=ro."""
                content = env.pkg.todo_reader.read_todo_content(str(db), "sess-wal")
                self.assertIn("from-wal", content)
            finally:
                writer.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
