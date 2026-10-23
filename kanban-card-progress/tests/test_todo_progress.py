"""Todo parsing and N/M/label/percent mapping (no DB)."""

from __future__ import annotations

import json
import unittest

from harness import load_package

pkg = load_package()
tp = pkg.todo_progress


class TestParseTodos(unittest.TestCase):
    def test_valid_payload(self):
        payload = json.dumps({"todos": [
            {"id": "a", "content": "one", "status": "completed"},
            {"id": "b", "content": "two", "status": "in_progress"},
        ], "revision": 3})
        self.assertEqual(tp.parse_todos(payload), [
            {"content": "one", "status": "completed", "id": "a"},
            {"content": "two", "status": "in_progress", "id": "b"},
        ])

    def test_none_and_empty(self):
        self.assertIsNone(tp.parse_todos(None))
        self.assertIsNone(tp.parse_todos(""))

    def test_malformed_json_never_raises(self):
        for bad in ("{", "not json", "[1,2,3]", '"a string"', "null", "42"):
            self.assertIsNone(tp.parse_todos(bad), bad)

    def test_missing_todos_key(self):
        self.assertIsNone(tp.parse_todos(json.dumps({"revision": 1})))

    def test_todos_not_a_list(self):
        self.assertIsNone(tp.parse_todos(json.dumps({"todos": {"a": 1}})))

    def test_malformed_items_dropped_not_fatal(self):
        payload = json.dumps({"todos": [
            "not a dict",
            {"content": "no status"},
            {"status": "bogus", "content": "bad status"},
            {"status": "pending"},
            {"status": "completed", "content": "good"},
        ]})
        self.assertEqual(tp.parse_todos(payload),
                         [{"content": "good", "status": "completed"}])

    def test_oversized_payload_rejected(self):
        self.assertIsNone(tp.parse_todos("x" * (tp.MAX_TODO_RESULT_CHARS + 1)))

    def test_alternative_text_keys(self):
        payload = json.dumps({"todos": [{"status": "pending", "title": "from title"}]})
        self.assertEqual(tp.parse_todos(payload),
                         [{"content": "from title", "status": "pending"}])


class TestMapProgress(unittest.TestCase):
    @staticmethod
    def _t(*pairs):
        return [{"content": text, "status": status} for status, text in pairs]

    def test_none_started(self):
        p = tp.map_progress(self._t(("pending", "first"), ("pending", "second")))
        self.assertEqual((p.done, p.total, p.label, p.percent), (0, 2, "first", 0))

    def test_all_done(self):
        p = tp.map_progress(self._t(("completed", "a"), ("completed", "b")))
        self.assertEqual((p.done, p.total, p.percent), (2, 2, 100))
        """no in_progress/pending -> last completed is the label"""
        self.assertEqual(p.label, "b")

    def test_in_progress_wins_over_pending(self):
        p = tp.map_progress(self._t(("pending", "queued"), ("in_progress", "running"),
                                    ("completed", "done")))
        self.assertEqual((p.done, p.total, p.label), (1, 3, "running"))
        self.assertEqual(p.percent, 33)

    def test_pending_fallback(self):
        p = tp.map_progress(self._t(("completed", "done"), ("pending", "next")))
        self.assertEqual(p.label, "next")

    def test_cancelled_excluded_from_total(self):
        p = tp.map_progress(self._t(("completed", "a"), ("cancelled", "skip"),
                                    ("pending", "b")))
        self.assertEqual((p.done, p.total, p.percent), (1, 2, 50))

    def test_only_cancelled_is_none(self):
        self.assertIsNone(tp.map_progress(self._t(("cancelled", "a"))))

    def test_empty_and_none(self):
        self.assertIsNone(tp.map_progress([]))
        self.assertIsNone(tp.map_progress(None))

    def test_percent_rounds(self):
        p = tp.map_progress(self._t(("completed", "a"), ("pending", "b"), ("pending", "c")))
        self.assertEqual(p.percent, 33)  # round(33.33)

    def test_progress_from_content_end_to_end(self):
        payload = json.dumps({"todos": [
            {"content": "clone", "status": "completed"},
            {"content": "test", "status": "completed"},
            {"content": "ship", "status": "in_progress"},
            {"content": "nope", "status": "cancelled"},
        ]})
        p = tp.progress_from_content(payload)
        self.assertEqual((p.done, p.total, p.label, p.percent), (2, 3, "ship", 67))

    def test_progress_from_content_malformed(self):
        self.assertIsNone(tp.progress_from_content("{"))
        self.assertIsNone(tp.progress_from_content(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
