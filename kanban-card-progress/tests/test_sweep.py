"""The sweep: mapping to the body, idempotence, cleanup, bounds and isolation."""

from __future__ import annotations

import time
import unittest

from harness import Env, append_todo, todo_payload


def _seed(env, items, *, session="sess-1"):
    db = env.state_db(session)
    append_todo(db, session, todo_payload(*items))
    return db


class TestRefreshCard(unittest.TestCase):
    def test_writes_block_for_a_running_card(self):
        with Env() as env:
            tid = env.running(body="Original body.")
            db = _seed(env, [("completed", "clone repo"), ("completed", "run tests"),
                             ("in_progress", "probando tests")])
            env.admit(tid, str(db), "sess-1")
            self.assertTrue(env.pkg.sweep.refresh_card(env.conn, tid))
            body = env.body(tid)
            self.assertTrue(body.startswith("▶ 2/3 · probando tests"))
            line2 = body.split("\n")[1]
            self.assertTrue(line2.startswith("["), line2)
            inner = line2[1:line2.index("]")]
            self.assertEqual(len(inner), env.pkg.progress_block.BAR_WIDTH)
            self.assertTrue(set(inner) <= {"#", "."})
            self.assertTrue(body.endswith("Original body."))
            self.assertIn("\u00a067%", body)

    def test_no_tasklist_writes_nothing(self):
        with Env() as env:
            tid = env.running(body="Original body.")
            db = env.state_db("sess-1")
            env.admit(tid, str(db), "sess-1")
            self.assertFalse(env.pkg.sweep.refresh_card(env.conn, tid))
            self.assertEqual(env.body(tid), "Original body.")

    def test_no_owner_event_writes_nothing(self):
        with Env() as env:
            tid = env.running(body="Original body.")
            self.assertFalse(env.pkg.sweep.refresh_card(env.conn, tid))
            self.assertEqual(env.body(tid), "Original body.")

    def test_m_zero_leaves_existing_block_untouched(self):
        with Env() as env:
            pb = env.pkg.progress_block
            existing = pb.apply_block("Original body.", pb.format_block(1, 2, "old", 50))
            tid = env.running(body=existing)
            db = _seed(env, [("cancelled", "only cancelled")])
            env.admit(tid, str(db), "sess-1")
            self.assertFalse(env.pkg.sweep.refresh_card(env.conn, tid))
            self.assertEqual(env.body(tid), existing)

    def test_idempotent_second_refresh(self):
        with Env() as env:
            tid = env.running(body="Original body.")
            db = _seed(env, [("in_progress", "step one")])
            env.admit(tid, str(db), "sess-1")
            self.assertTrue(env.pkg.sweep.refresh_card(env.conn, tid))
            first = env.body(tid)
            self.assertFalse(env.pkg.sweep.refresh_card(env.conn, tid),
                             "an unchanged body must not be rewritten")
            self.assertEqual(env.body(tid), first)

    def test_block_is_updated_when_progress_changes(self):
        with Env() as env:
            tid = env.running(body="Original body.")
            db = _seed(env, [("in_progress", "step one")])
            env.admit(tid, str(db), "sess-1")
            env.pkg.sweep.refresh_card(env.conn, tid)
            append_todo(db, "sess-1", todo_payload(
                ("completed", "step one"), ("in_progress", "step two")))
            self.assertTrue(env.pkg.sweep.refresh_card(env.conn, tid))
            body = env.body(tid)
            self.assertIn("step two", body)
            self.assertNotIn("step one", body)
            self.assertTrue(body.endswith("Original body."))

    def test_human_text_after_block_survives(self):
        with Env() as env:
            tid = env.running(body="Original body.")
            db = _seed(env, [("in_progress", "step one")])
            env.admit(tid, str(db), "sess-1")
            env.pkg.sweep.refresh_card(env.conn, tid)
            env.conn.execute("UPDATE tasks SET body = body || char(10) || char(10) || "
                             "'Nota humana: revisar.' WHERE id = ?", (tid,))
            env.conn.commit()
            append_todo(db, "sess-1", todo_payload(
                ("completed", "step one"), ("pending", "step two")))
            env.pkg.sweep.refresh_card(env.conn, tid)
            self.assertTrue(env.body(tid).endswith("Nota humana: revisar."))

    def test_not_running_card_is_skipped(self):
        with Env() as env:
            tid = env.task(status="todo", body="Original body.")
            db = _seed(env, [("in_progress", "step one")])
            env.admit(tid, str(db), "sess-1")
            self.assertFalse(env.pkg.sweep.refresh_card(env.conn, tid))
            self.assertEqual(env.body(tid), "Original body.")

    def test_oversized_body_skipped(self):
        with Env() as env:
            tid = env.running(body="x" * 500)
            db = _seed(env, [("in_progress", "step one")])
            env.admit(tid, str(db), "sess-1")
            self.assertFalse(env.pkg.sweep.refresh_card(env.conn, tid, max_body_bytes=100))
            self.assertEqual(env.body(tid), "x" * 500)

    def test_malformed_todo_row_never_raises(self):
        with Env() as env:
            tid = env.running(body="Original body.")
            db = env.state_db("sess-1")
            append_todo(db, "sess-1", "{not json")
            env.admit(tid, str(db), "sess-1")
            self.assertFalse(env.pkg.sweep.refresh_card(env.conn, tid))
            self.assertEqual(env.body(tid), "Original body.")

    def test_old_v2_block_replaced_in_place_by_the_sweep(self):
        """Migration: a live card carries the v2 `#=` 24-wide block."""
        with Env() as env:
            pb = env.pkg.progress_block
            old = ("\u25b6 1/8 \u00b7 Trace scan call\n"
                   "[" + "#" * 3 + "=" * 21 + "]\u00a012%\nOriginal body.")
            tid = env.running(body=old)
            db = _seed(env, [("completed", "a"), ("completed", "b"),
                             ("in_progress", "probando tests")])
            env.admit(tid, str(db), "sess-1")
            self.assertTrue(env.pkg.sweep.refresh_card(env.conn, tid))
            body = env.body(tid)
            self.assertEqual(body.count("\u25b6"), 1, "no second block stacked")
            self.assertNotIn("=", body.split("\n")[1])
            self.assertNotIn("Trace scan call", body)
            self.assertTrue(body.endswith("Original body."))

    def test_v3_underscore_block_replaced_in_place_by_the_sweep(self):
        """Migration: a card carries the v3-first-cut `#_` 23-wide block."""
        with Env() as env:
            old = ("\u25b6 1/8 \u00b7 Trace scan call\n"
                   "[" + "#" * 3 + "_" * 20 + "]\u00a012%\nOriginal body.")
            tid = env.running(body=old)
            db = _seed(env, [("completed", "a"), ("completed", "b"),
                             ("in_progress", "probando tests")])
            env.admit(tid, str(db), "sess-1")
            self.assertTrue(env.pkg.sweep.refresh_card(env.conn, tid))
            body = env.body(tid)
            self.assertEqual(body.count("\u25b6"), 1, "no second block stacked")
            self.assertNotIn("_", body.split("\n")[1])
            self.assertNotIn("Trace scan call", body)
            self.assertTrue(body.endswith("Original body."))

    def test_v31_unpadded_block_replaced_in_place_by_the_sweep(self):
        """Migration: a card carries the v3.1 `#.` block with NO trailing pad."""
        with Env() as env:
            old = ("\u25b6 1/8 \u00b7 Trace scan call\n"
                   "[" + "#" * 3 + "." * 20 + "]\u00a012%\nOriginal body.")
            tid = env.running(body=old)
            db = _seed(env, [("completed", "a"), ("completed", "b"),
                             ("in_progress", "probando tests")])
            env.admit(tid, str(db), "sess-1")
            self.assertTrue(env.pkg.sweep.refresh_card(env.conn, tid))
            body = env.body(tid)
            self.assertEqual(body.count("\u25b6"), 1, "no second block stacked")
            self.assertNotIn("Trace scan call", body)
            self.assertTrue(body.endswith("Original body."))
            """the new block carries the v3.2 pad"""
            self.assertEqual(set(body.split("\n")[1][len("[" + "#" * 3 + "." * 20 + "]\u00a012%"):]),
                             {env.pkg.progress_block.PAD_CHAR})

    def test_second_sweep_after_migration_is_a_noop(self):
        with Env() as env:
            old = ("\u25b6 1/8 \u00b7 Trace scan call\n"
                   "[" + "#" * 3 + "=" * 21 + "]\u00a012%\nOriginal body.")
            tid = env.running(body=old)
            db = _seed(env, [("in_progress", "probando tests")])
            env.admit(tid, str(db), "sess-1")
            self.assertTrue(env.pkg.sweep.refresh_card(env.conn, tid))
            first = env.body(tid)
            self.assertFalse(env.pkg.sweep.refresh_card(env.conn, tid),
                             "an unchanged body must not be rewritten")
            self.assertEqual(env.body(tid), first)

    def test_lookalike_bar_of_wrong_alphabet_left_untouched(self):
        with Env() as env:
            bad = ("\u25b6 1/8 \u00b7 leftover\n"
                   "[" + "#" * 10 + "-" * 16 + "]\u00a040%\nOriginal body.")
            tid = env.running(body=bad)
            db = _seed(env, [("in_progress", "probando tests")])
            env.admit(tid, str(db), "sess-1")
            env.pkg.sweep.refresh_card(env.conn, tid)
            body = env.body(tid)
            """the wrong-alphabet lookalike is not stripped, so it survives"""
            self.assertIn("-" * 16, body)
            self.assertTrue(body.endswith("Original body."))


class TestCleanupStopped(unittest.TestCase):
    def test_strips_block_from_a_stopped_card(self):
        with Env() as env:
            pb = env.pkg.progress_block
            body = pb.apply_block("Original body.", pb.format_block(1, 2, "old", 50))
            tid = env.running(body=body)
            env.set_status(tid, "blocked")
            self.assertEqual(env.pkg.sweep.cleanup_stopped(env.conn), 1)
            self.assertEqual(env.body(tid), "Original body.")

    def test_leaves_done_and_archived_alone(self):
        with Env() as env:
            pb = env.pkg.progress_block
            body = pb.apply_block("Original body.", pb.format_block(1, 2, "old", 50))
            done = env.running(body=body)
            env.set_status(done, "done")
            arch = env.running(body=body)
            env.set_status(arch, "archived")
            self.assertEqual(env.pkg.sweep.cleanup_stopped(env.conn), 0)
            self.assertEqual(env.body(done), body)
            self.assertEqual(env.body(arch), body)

    def test_ignores_running_cards(self):
        with Env() as env:
            pb = env.pkg.progress_block
            body = pb.apply_block("Original body.", pb.format_block(1, 2, "old", 50))
            tid = env.running(body=body)
            self.assertEqual(env.pkg.sweep.cleanup_stopped(env.conn), 0)
            self.assertEqual(env.body(tid), body)

    def test_terminal_cards_do_not_starve_cleanup(self):
        """Archived/done cards must not consume the cleanup LIMIT.

        Regression: ``STOPPED_SQL`` used to select every non-running card, so on
        a live board (95 archived cards) all 20 slots were terminal rows that the
        loop then skipped, and a genuinely stopped card was never reached.

        Deterministic by construction: terminal cards are seeded with fixed ids
        that sort *before* the stopped card (``t_00...`` < ``t_zz...``), so under
        the unguarded query the first ``limit`` rows are all terminal and the
        stopped card is never reached — the test fails every run. Random hex ids
        (the old version) made the stopped card land in the first 20 rows only
        ~1/4 of the time.
        """
        with Env() as env:
            pb = env.pkg.progress_block
            body = pb.apply_block("Original body.", pb.format_block(1, 2, "old", 50))
            for i, status in enumerate(("archived", "done") * 30):
                env.task_with_id(f"t_00{i:06d}", body=body, status=status)
            """Sorts last, so an unguarded LIMIT 20 never reaches it."""
            stopped = env.task_with_id("t_zzzzzzzz", body=body, status="blocked")
            self.assertEqual(env.pkg.sweep.cleanup_stopped(env.conn, limit=20), 1)
            self.assertEqual(env.body(stopped), "Original body.")


class TestSweepOnce(unittest.TestCase):
    def test_sweeps_every_running_card(self):
        with Env() as env:
            a = env.running(body="card a")
            b = env.running(body="card b")
            idle = env.task(status="blocked", body="card c")
            db_a = env.state_db("sess-a")
            db_b = env.state_db("sess-b")
            append_todo(db_a, "sess-a", todo_payload(("in_progress", "alpha")))
            append_todo(db_b, "sess-b", todo_payload(("in_progress", "beta")))
            env.admit(a, str(db_a), "sess-a")
            env.admit(b, str(db_b), "sess-b")
            env.admit(idle, str(db_a), "sess-a")

            stats = env.pkg.sweep.sweep_once(env.conn)
            self.assertEqual(stats.running, 2)
            self.assertEqual(stats.written, 2)
            self.assertIn("alpha", env.body(a))
            self.assertIn("beta", env.body(b))
            self.assertEqual(env.body(idle), "card c")

    def test_one_bad_card_does_not_stop_the_sweep(self):
        with Env() as env:
            good = env.running(body="card good")
            bad = env.running(body="card bad")
            db = env.state_db("sess-1")
            append_todo(db, "sess-1", todo_payload(("in_progress", "alpha")))
            env.admit(good, str(db), "sess-1")
            """bad: owner_admitted points at a state.db that does not exist"""
            env.admit(bad, "/nonexistent/state.db", "sess-1")
            stats = env.pkg.sweep.sweep_once(env.conn)
            self.assertIn("alpha", env.body(good))
            self.assertEqual(stats.errors, 0)
            self.assertEqual(env.body(bad), "card bad")

    def test_card_cap(self):
        with Env() as env:
            db = env.state_db("sess-1")
            append_todo(db, "sess-1", todo_payload(("in_progress", "alpha")))
            ids = [env.running(body=f"card {i}") for i in range(5)]
            for tid in ids:
                env.admit(tid, str(db), "sess-1")
            stats = env.pkg.sweep.sweep_once(env.conn, max_cards=2)
            self.assertEqual(stats.running, 5)
            self.assertEqual(stats.written, 2)

    def test_time_budget_defers_remaining(self):
        with Env() as env:
            db = env.state_db("sess-1")
            append_todo(db, "sess-1", todo_payload(("in_progress", "alpha")))
            for i in range(4):
                tid = env.running(body=f"card {i}")
                env.admit(tid, str(db), "sess-1")
            clock = {"t": 0.0}

            def now():
                clock["t"] += 10.0
                return clock["t"]

            stats = env.pkg.sweep.sweep_once(env.conn, budget_seconds=25.0, now=now)
            self.assertLess(stats.written, 4, "the budget must stop the sweep early")

    def test_running_query_failure_is_contained(self):
        with Env() as env:
            env.conn.execute("DROP TABLE tasks")
            env.conn.commit()
            stats = env.pkg.sweep.sweep_once(env.conn)
            self.assertEqual(stats.running, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
