import sys

import pytest

guards = sys.modules["agent_guards"]

WASHOUT = {"reason": "WASHOUT_UNTIL=2099-01-01T10:00:00+02:00 backend fix needs release+deploy"}


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")


def schedule(args=WASHOUT):
    return guards._pre_tool_call(tool_name="kanban_schedule", args=args)


def test_worker_self_schedule_is_blocked(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_sch0004")
    result = schedule()
    assert result["action"] == "block"
    assert "kanban_block" in result["message"]


def test_lead_may_schedule(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert schedule({"task_id": "t_1", "reason": "SCHEDULED_UNTIL=2099-01-20"}) is None


def test_without_worker_setup_nothing_is_enforced(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert schedule() is None
