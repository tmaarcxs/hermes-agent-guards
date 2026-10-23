"""CAS write path, race handling and safety gates."""

from __future__ import annotations

import sqlite3
import unittest

from harness import Env, append_todo, todo_payload

BLOCK = "▶ 1/2 · working\n[############============]\u00a050%"


class TestCasWrite(unittest.TestCase):
    def test_cas_matches_exact_old_body(self):
        with Env() as env:
            tid = env.running(body="orig")
            self.assertTrue(env.pkg.card_write.cas_write_body(env.conn, tid, "orig", "new"))
            self.assertEqual(env.body(tid), "new")

    def test_cas_miss_leaves_human_edit(self):
        with Env() as env:
            tid = env.running(body="orig")
            env.conn.execute("UPDATE tasks SET body = ? WHERE id = ?", ("human edit", tid))
            env.conn.commit()
            self.assertFalse(env.pkg.card_write.cas_write_body(env.conn, tid, "orig", "clobber"))
            self.assertEqual(env.body(tid), "human edit")

    def test_cas_null_body_guard(self):
        with Env() as env:
            tid = env.running(body=None)
            self.assertTrue(env.pkg.card_write.cas_write_body(env.conn, tid, None, "first"))
            self.assertEqual(env.body(tid), "first")

    def test_event_emitted_only_on_match(self):
        with Env() as env:
            tid = env.running(body="orig")
            before = env.conn.execute(
                "SELECT COUNT(*) c FROM task_events WHERE kind='progress'").fetchone()["c"]
            env.pkg.card_write.cas_write_body(env.conn, tid, "orig", "new")
            after = env.conn.execute(
                "SELECT COUNT(*) c FROM task_events WHERE kind='progress'").fetchone()["c"]
            self.assertEqual(after, before + 1)
            env.pkg.card_write.cas_write_body(env.conn, tid, "stale", "x")
            again = env.conn.execute(
                "SELECT COUNT(*) c FROM task_events WHERE kind='progress'").fetchone()["c"]
            self.assertEqual(again, after, "a CAS miss must emit no event")

    def test_no_edited_event_ever(self):
        with Env() as env:
            tid = env.running(body="orig")
            env.pkg.card_write.cas_write_body(env.conn, tid, "orig", "new")
            self.assertEqual(env.conn.execute(
                "SELECT COUNT(*) c FROM task_events WHERE kind='edited'").fetchone()["c"], 0)

    def test_write_block_retries_through_a_race(self):
        with Env() as env:
            tid = env.running(body="orig")
            cw = env.pkg.card_write
            real = cw.cas_write_body
            calls = {"n": 0}

            def flaky(conn, task_id, old_body, new_body, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    conn.execute("UPDATE tasks SET body = body || ' +human' WHERE id = ?",
                                 (task_id,))
                    conn.commit()
                    return False
                return real(conn, task_id, old_body, new_body, **kw)

            cw.cas_write_body = flaky
            try:
                self.assertTrue(cw.write_block(env.conn, tid, "NEW"))
            finally:
                cw.cas_write_body = real
            self.assertEqual(env.body(tid), "NEW")
            self.assertGreaterEqual(calls["n"], 2)

    def test_write_block_noop_when_body_identical(self):
        with Env() as env:
            tid = env.running(body="same")
            self.assertFalse(env.pkg.card_write.write_block(env.conn, tid, "same"))

    def test_write_block_false_for_missing_card(self):
        with Env() as env:
            self.assertFalse(env.pkg.card_write.write_block(env.conn, "t_nope", "x"))

    def test_write_denied_in_delegated_child(self):
        with Env() as env:
            tid = env.running(body="orig")
            from agent import delegation_context as dc
            try:
                with dc.delegated_child_context():
                    env.pkg.card_write.cas_write_body(env.conn, tid, "orig", "hacked")
            except PermissionError:
                return
            raise AssertionError("the delegated-child fence did not fire")

    def test_strip_block_removes_only_leading_block(self):
        with Env() as env:
            pb = env.pkg.progress_block
            body = pb.apply_block("Human description.", BLOCK)
            tid = env.task_with_id("t_aa1", body=body, status="blocked")
            self.assertTrue(env.pkg.card_write.strip_block(env.conn, tid))
            self.assertEqual(env.body(tid), "Human description.")

    def test_strip_block_noop_without_block(self):
        with Env() as env:
            tid = env.task_with_id("t_aa2", body="plain body", status="blocked")
            self.assertFalse(env.pkg.card_write.strip_block(env.conn, tid))
            self.assertEqual(env.body(tid), "plain body")


class TestStatusPredicate(unittest.TestCase):
    """F1: the CAS write carries a status predicate, not just the body CAS.

    The sweep reads the card's status before it formats the block; a completion
    between that read and the write must not strand a block on a terminal card.
    Each test drives the transition *between* the read and the write
    deterministically (no sleeps) by wrapping ``cas_write_body`` so the first
    call flips the status and then delegates to the real implementation.
    """

    def _race_once(self, env, tid, *, to_status, call, **kwargs):
        """Run ``call`` with a status transition injected before its first CAS."""
        cw = env.pkg.card_write
        real = cw.cas_write_body
        fired = {"n": 0}

        def racer(conn, task_id, old_body, new_body, **kw):
            if fired["n"] == 0:
                fired["n"] += 1
                conn.execute("UPDATE tasks SET status = ? WHERE id = ?",
                             (to_status, task_id))
                conn.commit()
            return real(conn, task_id, old_body, new_body, **kw)

        cw.cas_write_body = racer
        try:
            return call()
        finally:
            cw.cas_write_body = real

    def _progress_events(self, env):
        return env.conn.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind='progress'").fetchone()["c"]

    def test_cas_write_refuses_done_card(self):
        with Env() as env:
            tid = env.running(body="orig")
            env.set_status(tid, "done")
            self.assertFalse(
                env.pkg.card_write.cas_write_body(env.conn, tid, "orig", "block"))
            self.assertEqual(env.body(tid), "orig")

    def test_cas_write_refuses_archived_card(self):
        with Env() as env:
            tid = env.running(body="orig")
            env.set_status(tid, "archived")
            self.assertFalse(
                env.pkg.card_write.cas_write_body(env.conn, tid, "orig", "block"))
            self.assertEqual(env.body(tid), "orig")

    def test_cas_write_refuses_terminal_card_with_null_body(self):
        with Env() as env:
            tid = env.task(body=None, status="done")
            self.assertFalse(
                env.pkg.card_write.cas_write_body(env.conn, tid, None, "block"))
            self.assertIsNone(env.body(tid))

    def test_status_miss_emits_no_event(self):
        with Env() as env:
            tid = env.running(body="orig")
            env.set_status(tid, "done")
            before = self._progress_events(env)
            env.pkg.card_write.cas_write_body(env.conn, tid, "orig", "block")
            self.assertEqual(self._progress_events(env), before,
                             "a status-predicate miss must emit no event")

    def test_write_block_race_transition_to_done_is_a_noop(self):
        with Env() as env:
            tid = env.running(body="orig")
            wrote = self._race_once(
                env, tid, to_status="done",
                call=lambda: env.pkg.card_write.write_block(env.conn, tid, "NEW"))
            self.assertFalse(wrote, "a card that completed must not receive a block")
            self.assertEqual(env.body(tid), "orig")

    def test_refresh_card_race_strands_no_block(self):
        """The reviewer's sweep.py:78 -> card_write.py:64 scenario, in-suite."""
        with Env() as env:
            tid = env.running(body="Original body.")
            db = env.state_db("sess-1")
            append_todo(db, "sess-1", todo_payload(("in_progress", "x")))
            env.admit(tid, str(db), "sess-1")
            wrote = self._race_once(
                env, tid, to_status="done",
                call=lambda: env.pkg.sweep.refresh_card(env.conn, tid))
            self.assertFalse(wrote)
            self.assertEqual(env.body(tid), "Original body.",
                             "no block may be stranded on a done card")
            self.assertEqual(env.status(tid), "done")
            self.assertEqual(self._progress_events(env), 0)

    def test_strip_block_refuses_running_card(self):
        with Env() as env:
            pb = env.pkg.progress_block
            body = pb.apply_block("Human.", pb.format_block(1, 2, "x", 50))
            tid = env.running(body=body)
            self.assertFalse(env.pkg.card_write.strip_block(env.conn, tid),
                             "cleanup must never strip a live block")
            self.assertEqual(env.body(tid), body)

    def test_strip_block_refuses_terminal_transition(self):
        with Env() as env:
            pb = env.pkg.progress_block
            body = pb.apply_block("Human.", pb.format_block(1, 2, "x", 50))
            tid = env.task_with_id("t_zz1", body=body, status="blocked")
            stripped = self._race_once(
                env, tid, to_status="done",
                call=lambda: env.pkg.card_write.strip_block(env.conn, tid))
            self.assertFalse(stripped, "a card that completed must be left alone")
            self.assertEqual(env.body(tid), body)


class TestFrozenStatuses(unittest.TestCase):
    def test_done_and_archived_are_frozen(self):
        self.assertEqual(env_frozen(), {"done", "archived"})


def env_frozen():
    from harness import load_package

    return set(load_package().card_write.FROZEN_STATUSES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
