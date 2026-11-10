"""Unattended runs cannot write the weekly Jot board; Marcos's own turns can."""

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[1]
if "jot_reviewer" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("jot_reviewer", _root / "__init__.py", submodule_search_locations=[str(_root)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["jot_reviewer"] = _module
    _spec.loader.exec_module(_module)
gate = importlib.import_module("jot_reviewer.writer_gate")
plugin = sys.modules["jot_reviewer"]

SCRIPT = "cd /x/skills/productivity/jot-weekly-board/scripts && npx tsx append-row.mts id Martes row"
ENV = ("HERMES_CRON_SESSION", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK", "HERMES_SINGLE_QUERY_SESSION",
       "HERMES_SESSION_PLATFORM", "HERMES_SESSION_SOURCE")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gate, "_env", lambda name: __import__("os").environ.get(name, ""))


@pytest.mark.parametrize("name,value", [("HERMES_CRON_SESSION", "1"), ("HERMES_DELEGATED_CHILD_CONTEXT", "1"),
                                        ("HERMES_KANBAN_TASK", "t_1"), ("HERMES_SINGLE_QUERY_SESSION", "1"),
                                        ("HERMES_SESSION_SOURCE", "tool"), ("HERMES_SESSION_PLATFORM", "cron")])
def test_unattended_runs_cannot_write_the_board(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    for tool, args in (("jot_update", {}), ("jot_create", {}), ("jot_set_task", {}), ("jot_delete", {}), ("terminal", {"command": SCRIPT})):
        assert "blocked" in gate.check(tool, args), (tool, name)


@pytest.mark.parametrize("platform", ["telegram", "cli", "local", "desktop", ""])
def test_marcos_own_turns_keep_direct_access(monkeypatch, platform):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", platform)
    assert gate.check("jot_update", {}) is None and gate.check("terminal", {"command": SCRIPT}) is None


def test_reads_and_unrelated_calls_are_never_blocked(monkeypatch):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    for tool, args in (("jot_read", {}), ("jot_list", {}), ("terminal", {"command": "ls"}), ("terminal", {"command": "tsx append-row.mts"})):
        assert gate.check(tool, args) is None


def test_the_hook_blocks_and_never_raises(monkeypatch):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    assert plugin._pre_tool_call(tool_name="jot_update", args={})["action"] == "block"
    monkeypatch.setattr(gate, "check", lambda *a, **k: 1 / 0)
    assert plugin._pre_tool_call(tool_name="jot_update", args={}) is None
