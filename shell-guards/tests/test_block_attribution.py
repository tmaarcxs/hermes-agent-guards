"""Pre_tool_call block messages name the producing guard, audits keep the common prefix.

The census scripts match the exact prefix `agent-guards blocked `, so each guard's name
is added after the tool token (`agent-guards blocked terminal [github_contact]: ...`),
never before it. Failing modules keep failing the whole chain open.
"""

import sys

import pytest

guards = sys.modules["shell_guards"]

REGISTERED_GUARDS = [
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


@pytest.mark.parametrize("name", REGISTERED_GUARDS)
def test_block_from_each_registered_guard_carries_its_name(monkeypatch, name):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(guards._policy, "configured_worker", lambda: "")
    module = getattr(guards, name)
    monkeypatch.setattr(module, "check", lambda *a, **k: "agent-guards blocked x: y")
    result = guards._pre_tool_call(tool_name="x", args={}, session_id=f"s-attr-{name}")
    assert result["action"] == "block"
    assert result["message"] == f"agent-guards blocked x [{name}]: y"


def test_raising_guard_stays_fail_open(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(guards._policy, "configured_worker", lambda: "")

    def boom(*a, **k):
        raise RuntimeError("probe crash")

    monkeypatch.setattr(guards.paas_promote_gate, "check", boom)
    assert guards._pre_tool_call(tool_name="x", args={}, session_id="s-attr-raise") is None
