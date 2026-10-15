"""Every guard block of a Kanban lifecycle call must end with the retry instruction.

A blocked lifecycle call does not satisfy the terminal-call protocol: the core stop-guard
(`agent/kanban_stop.py session_called_kanban_terminal`) scans transcript TOOL NAMES, not
outcomes, so a run whose only terminal call was blocked and whose final reply is text-only
exits rc=0 as a protocol violation with no nudge. The block MESSAGE is the one lever the
guards control, so each path ends with the same demand: retry in this same response.
"""

import sys

guards = sys.modules["agent_guards"]

INSTRUCTION = "retry the lifecycle call in this same response"
VIOLATION_CLAUSE = "protocol violation"


def assert_retry_instruction(message: str) -> None:
    assert INSTRUCTION in message, message
    assert VIOLATION_CLAUSE in message, message


def test_kanban_policy_block_carries_retry_instruction(monkeypatch):
    policy = guards.kanban_policy
    monkeypatch.setattr(policy, "configured_worker", lambda: "worker")
    body = "Goal: spec\n\n## Acceptance\n1. check"
    result = guards._pre_tool_call(
        tool_name="kanban_request_review",
        args={"summary": "s", "reviewer": "someone-else"},
        task_id="", session_id="",
    )
    assert result["action"] == "block"
    assert_retry_instruction(result["message"])


def test_review_gate_block_carries_retry_instruction(monkeypatch):
    import json
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    gate = guards.review_gate
    todo_items = [{"id": "1", "content": "Contract coverage", "status": "pending"}]
    guards._post_tool_call(tool_name="todo_list", args={"todos": todo_items},
                           result=json.dumps({"todos": todo_items}), status="ok", session_id="s9")
    result = guards._pre_tool_call(tool_name="kanban_request_review", args={"summary": "s"}, session_id="s9")
    assert result["action"] == "block"
    assert_retry_instruction(result["message"])
    gate.reset()


def test_completion_gate_missing_and_unmet_blocks_carry_retry_instruction(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    gate = guards.completion_gate
    body = "Goal\n\n## Acceptance\n1. Suites pass.\n2. PR merged.\n3. Staging verified.\n"
    missing = gate.check("kanban_complete", {"summary": "s", "metadata": {"changed_files": []}},
                         load_task=lambda _: (body, False))
    assert missing and "Acceptance" in missing
    assert_retry_instruction(missing)
    unmet = gate.check("kanban_complete",
                       {"summary": "s", "metadata": {"acceptance": {"1": "ok", "2": "PR merged? NOT YET", "3": "staging app 200"}}},
                       load_task=lambda _: (body, False))
    assert unmet and "not met yet" in unmet
    assert_retry_instruction(unmet)


def test_handoff_gate_bounce_carries_retry_instruction(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    gate = guards.handoff_gate
    reason = ("Signed publication is complete (commit 31439b10, PR #934 open). Nothing left for this worker "
              "to execute: merge of PR #934 plus production acceptance are held by the lead.")
    result = guards._pre_tool_call(tool_name="kanban_block",
                                   args={"reason": reason, "kind": "needs_input"}, session_id="s-hg")
    assert result["action"] == "block"
    assert_retry_instruction(result["message"])
    gate.reset()


def test_schedule_gate_block_carries_retry_instruction(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    result = guards._pre_tool_call(tool_name="kanban_schedule",
                                   args={"reason": "SCHEDULED_UNTIL=2099-01-20"}, session_id="s-sg")
    assert result["action"] == "block"
    assert_retry_instruction(result["message"])


def test_repeat_guard_ledger_block_of_lifecycle_call_carries_retry_instruction():
    guard = guards.repeat_guard
    args = {"task_id": "t_x", "summary": "s"}
    for _ in range(2):
        guards._post_tool_call(tool_name="kanban_request_review", args=args,
                               result='{"error": "agent-guards blocked kanban_request_review: pre-review checks"}',
                               status="error",
                               error_message="agent-guards blocked kanban_request_review: pre-review checks",
                               session_id="s-rg")
    result = guards._pre_tool_call(tool_name="kanban_request_review", args=args, session_id="s-rg")
    assert result["action"] == "block"
    assert "already failed 2 times" in result["message"]
    assert_retry_instruction(result["message"])
    guard.reset()


def test_repeat_guard_non_lifecycle_block_stays_generic():
    guard = guards.repeat_guard
    for _ in range(2):
        guards._post_tool_call(tool_name="read_file", args={"path": "/missing.txt"},
                               result='{"error": "File not found: /missing.txt"}', status="error",
                               error_message="File not found: /missing.txt", session_id="s-plain")
    result = guards._pre_tool_call(tool_name="read_file", args={"path": "/missing.txt"}, session_id="s-plain")
    assert result["action"] == "block"
    assert INSTRUCTION not in result["message"]
    guard.reset()
