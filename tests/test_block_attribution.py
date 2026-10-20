"""Pre_tool_call block messages name the producing guard, audits keep the common prefix.

The census scripts match the exact prefix `agent-guards blocked `, so each guard's name
is added after the tool token (`agent-guards blocked terminal [github_contact]: ...`),
never before it. Failing modules keep failing the whole chain open.
"""

import sys

import pytest

guards = sys.modules["agent_guards"]

REGISTERED_GUARDS = [
    "checkpoint_gate",
    "home_litter_gate",
    "kanban_policy",
    "lead_delegation",
    "github_contact",
    "paas_promote_gate",
    "review_gate",
    "handoff_gate",
    "completion_gate",
    "backup_copy_gate",
    "schedule_gate",
    "skill_size",
    "background_notify_gate",
]


def block(tool_name, args, **kw):
    result = guards._pre_tool_call(tool_name=tool_name, args=args, **kw)
    assert result is not None and result["action"] == "block", result
    return result["message"]


def test_terminal_block_names_builtin_and_keeps_audit_prefix():
    message = block("terminal", {"command": "sleep 60", "background": False})
    assert message.startswith("agent-guards blocked terminal")
    assert "[__init__]" in message


def test_attach_cap_message_is_one_line_with_cap_and_remedy():
    message = block(
        "kanban_attach",
        {"filename": "r.json", "content_base64": "QUJD" * 100},
    )
    assert "\n" not in message
    assert "256" in message and "attach by path" in message and "sha256" in message
    assert "[__init__]" in message
    assert len(message) <= 200


@pytest.mark.parametrize("worker_process", [False, True])
def test_kanban_policy_block_names_guard_in_both_contexts(monkeypatch, worker_process):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "kworker")
    if worker_process:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_attr0001")
    else:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    message = block(
        "kanban_request_review",
        {"summary": "s", "reviewer": "someone-else"},
        task_id="",
        session_id="",
    )
    assert "[kanban_policy]" in message
    assert "retry the lifecycle call in this same response" in message


@pytest.mark.parametrize("name", REGISTERED_GUARDS)
def test_block_from_each_registered_guard_carries_its_name(monkeypatch, name):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    module = getattr(guards, name)
    monkeypatch.setattr(module, "check", lambda *a, **k: "agent-guards blocked x: y")
    result = guards._pre_tool_call(tool_name="x", args={}, session_id=f"s-attr-{name}")
    assert result["action"] == "block"
    assert result["message"] == f"agent-guards blocked x [{name}]: y"


def test_raising_guard_stays_fail_open(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")

    def boom(*a, **k):
        raise RuntimeError("probe crash")

    monkeypatch.setattr(guards.checkpoint_gate, "check", boom)
    assert guards._pre_tool_call(tool_name="x", args={}, session_id="s-attr-raise") is None
