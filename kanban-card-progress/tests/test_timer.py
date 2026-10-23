"""The timer: single instance, exception survival, clean stop, and register()."""

from __future__ import annotations

import threading
import time
import unittest

from harness import Env, append_todo, todo_payload


class TestTimerLoop(unittest.TestCase):
    def test_single_instance(self):
        pkg = _pkg()
        from kanban_card_progress_under_test.timer import ProgressTimer

        calls = []
        t = ProgressTimer(lambda: calls.append(1), interval_seconds=5.0)
        try:
            self.assertTrue(t.start())
            self.assertFalse(t.start(), "a second start() must be a no-op")
            deadline = time.monotonic() + 5.0
            while not calls and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(calls)
        finally:
            t.stop()

    def test_exception_survival(self):
        from kanban_card_progress_under_test.timer import ProgressTimer

        calls = {"n": 0}
        warns = []

        def boom():
            calls["n"] += 1
            raise RuntimeError("sweep exploded")

        t = ProgressTimer(boom, interval_seconds=5.0, warn=warns.append)
        try:
            self.assertTrue(t.start())
            deadline = time.monotonic() + 5.0
            while calls["n"] < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(t.running, "the thread must survive an exception")
            self.assertTrue(warns, "the failure must be logged at warning")
        finally:
            t.stop()

    def test_stop_is_clean_and_interrupts_the_wait(self):
        from kanban_card_progress_under_test.timer import ProgressTimer

        t = ProgressTimer(lambda: None, interval_seconds=300.0)
        t.start()
        self.assertTrue(t.running)
        started = time.monotonic()
        t.stop()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0, "stop() must not wait out the interval")
        self.assertFalse(t.running)
        self.assertFalse(_alive("kanban-card-progress"))

    def test_min_interval_floor(self):
        from kanban_card_progress_under_test.timer import ProgressTimer

        t = ProgressTimer(lambda: None, interval_seconds=0.1)
        self.assertGreaterEqual(t._interval, 5.0)

    def test_start_returns_immediately(self):
        from kanban_card_progress_under_test.timer import ProgressTimer

        gate = threading.Event()
        t = ProgressTimer(lambda: gate.wait(5.0), interval_seconds=5.0)
        try:
            started = time.monotonic()
            t.start()
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            gate.set()
            t.stop()


class TestRegister(unittest.TestCase):
    def test_disabled_by_default(self):
        """With no entry in config, `enabled` defaults True but the plugin is off
        because it is absent from `plugins.enabled` (the allow-list). The switch
        only turns it off once the plugin is loaded."""
        with Env() as env:
            pkg = env.pkg
            pkg._reset_config_cache()
            hooks = {}
            ctx = _FakeCtx(hooks)
            pkg.register(ctx)
            try:
                self.assertIn("unload", hooks, "on_unload must always be registered")
                self.assertIsNotNone(pkg._timer,
                                     "the allow-list, not this switch, gates loading")
            finally:
                hooks["unload"]()

    def test_explicit_enabled_false_starts_no_thread(self):
        with Env() as env:
            pkg = env.pkg
            pkg._config_cache = {"enabled": False, "debug": False}
            hooks = {}
            ctx = _FakeCtx(hooks)
            pkg.register(ctx)
            try:
                self.assertIn("unload", hooks)
                self.assertIsNone(pkg._timer,
                                  "enabled: false must not start a thread")
            finally:
                hooks["unload"]()

    def test_enabled_starts_and_unload_stops(self):
        with Env() as env:
            pkg = env.pkg
            pkg._config_cache = {"enabled": True, "interval_seconds": 30,
                                 "debug": False}
            hooks = {}
            ctx = _FakeCtx(hooks)
            pkg.register(ctx)
            try:
                self.assertIsNotNone(pkg._timer)
                self.assertTrue(pkg._timer.running)
                self.assertTrue(_alive("kanban-card-progress"))
                self.assertFalse(pkg.register(_FakeCtx({})),
                                 "a second register() must not start a second thread")
            finally:
                hooks["unload"]()
            self.assertFalse(_alive("kanban-card-progress"))

    def test_cycle_writes_a_real_block(self):
        with Env() as env:
            pkg = env.pkg
            tid = env.running(body="Original body.")
            db = env.state_db("sess-1")
            append_todo(db, "sess-1", todo_payload(
                ("completed", "clone"), ("in_progress", "probando tests")))
            env.admit(tid, str(db), "sess-1")
            pkg._config_cache = {"enabled": True, "debug": False}
            pkg._run_cycle()
            body = env.body(tid)
            self.assertTrue(body.startswith("▶ 1/2 · probando tests"))
            self.assertTrue(body.endswith("Original body."))

    def test_documented_settings_path_is_the_one_read(self):
        """F4: the docs' `plugins.entries.<id>.settings.<key>` is what `_config` reads.

        The old README/plugin.yaml documented the bare
        `plugins.entries.<id>.enabled: false`, which the code never reads (a
        silent no-op). This test writes the documented path into a real
        config.yaml and proves the plugin honours it — and that the bare key is
        *not* honoured, so the doc cannot drift back.
        """
        with Env() as env:
            pkg = env.pkg
            (env.home / "config.yaml").write_text(
                "plugins:\n"
                "  entries:\n"
                "    kanban-card-progress:\n"
                "      settings:\n"
                "        enabled: false\n"
                "        interval_seconds: 42\n",
                encoding="utf-8",
            )
            pkg._reset_config_cache()
            cfg = pkg._config()
            self.assertEqual(cfg.get("enabled"), False,
                             "the documented settings subtree must be read")
            self.assertEqual(cfg.get("interval_seconds"), 42)

            # The bare key (no `settings`) is not read — documents the real path.
            (env.home / "config.yaml").write_text(
                "plugins:\n"
                "  entries:\n"
                "    kanban-card-progress:\n"
                "      enabled: false\n",
                encoding="utf-8",
            )
            pkg._reset_config_cache()
            self.assertEqual(pkg._config(), {},
                             "the bare `entries.<id>.enabled` key is not the read path")


class _FakeCtx:
    def __init__(self, hooks):
        self._hooks = hooks

    def on_unload(self, callback):
        self._hooks["unload"] = callback

    def register_hook(self, name, callback):
        self._hooks[name] = callback


def _pkg():
    from harness import load_package

    return load_package()


def _alive(name):
    return any(t.name == name and t.is_alive() for t in threading.enumerate())


if __name__ == "__main__":
    unittest.main(verbosity=2)
