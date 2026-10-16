"""Unit tests for the V4 inventory-first injection.

The payload shape mirrors the real Hermes call site:
``agent/turn_context.py`` ``_collect_pre_llm_call_context`` (line 798, fired at
the turn prologue, line 1177) invokes every ``pre_llm_call`` callback with:
session_id, task_id, turn_id, user_message, conversation_history,
is_first_turn, model, platform, parent_session_id, sender_id.
"""

import sys

import pytest

guard = sys.modules["agent_guards"]
inventory_rule = guard.inventory_rule

HOST_KWARGS = dict(
    platform="cli",
    parent_session_id="",
    sender_id="",
    task_id="",
    model="test-model",
)


def _fire(session_id, turn_id, user_message, **extra):
    kwargs = dict(
        session_id=session_id,
        turn_id=turn_id,
        user_message=user_message,
        conversation_history=[],
        is_first_turn=not bool(extra.get("conversation_history")),
        **HOST_KWARGS,
    )
    kwargs.update(extra)
    return inventory_rule.hook(**kwargs)


@pytest.fixture(autouse=True)
def lead_default(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)


@pytest.fixture(autouse=True)
def _clean():
    inventory_rule.reset()
    yield
    inventory_rule.reset()


def test_injects_on_lead_turn():
    result = _fire("sess-1", "turn-1", "Do the three repo tasks")
    assert result == {"context": inventory_rule.INSTRUCTION}


def test_injection_once_per_user_turn():
    first = _fire("sess-1", "turn-1", "Do the three repo tasks")
    again_same_turn = _fire("sess-1", "turn-1", "Do the three repo tasks")
    assert first is not None
    assert again_same_turn is None


def test_no_injection_on_tool_result_continuation():
    """A new pre_llm_call fire within the same turn is a no-op.

    The host only calls pre_llm_call once per user turn (turn prologue);
    tool-result iterations replay the prologue's api_content stamp. The dedup
    defends against a defensive double fire of the same turn_id.
    """
    assert _fire("sess-1", "turn-1", "Do the three repo tasks") is not None
    assert _fire("sess-1", "turn-1", "Do the three repo tasks") is None


def test_new_user_turn_injects_again():
    assert _fire("sess-1", "turn-1", "First request") is not None
    assert _fire("sess-1", "turn-1", "First request") is None
    assert _fire("sess-1", "turn-2", "Second request") is not None


def test_dedup_scoped_per_session():
    assert _fire("sess-1", "turn-1", "Same prompt") is not None
    assert _fire("sess-2", "turn-1", "Same prompt") is not None


def test_none_in_worker_context(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_somecard")
    assert _fire("sess-1", "turn-1", "Do the three repo tasks") is None


def test_empty_worker_task_env_is_lead(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "   ")
    assert _fire("sess-1", "turn-1", "Do the three repo tasks") is not None


def test_no_task_env_at_all_still_injects(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert _fire("sess-1", "turn-1", "Do the three repo tasks") is not None


def test_never_blocks_shape_is_context_only():
    result = _fire("sess-1", "turn-1", "Do the three repo tasks")
    assert isinstance(result, dict)
    assert set(result) == {"context"}
    assert "action" not in result


def test_instruction_mentions_inventory_purpose_and_no_number():
    text = inventory_rule.INSTRUCTION
    assert "inventory" in text.lower()
    assert "one kanban card per unit" in text.lower()
    assert "workers are slow" in text.lower()
    assert "parallel" in text.lower()
    assert "exempt" in text.lower()
    import re

    assert not re.search(r"\b(10|[2-9]\d)\b", text), "no fixed card floor/cap in the rule"


def test_1_card_turn_for_multi_part_request_still_fires():
    """SPEC acceptance: the mechanism fires on a 1-card turn for a multi-part request."""
    assert _fire("sess-1", "turn-1", "Add feature A, fix bug B and update docs C") is not None


def test_message_fallback_dedup_without_turn_id():
    assert _fire("sess-1", "", "Same body twice") is not None
    assert _fire("sess-1", "", "Same body twice") is None
    assert _fire("sess-1", "", "Different body") is not None


def test_empty_payload_injects_nothing():
    assert inventory_rule.hook(session_id="", turn_id="", user_message="", **HOST_KWARGS) is None


def test_dedup_evicts_oldest_beyond_cap():
    for i in range(inventory_rule._MAX_TRACKED_TURNS + 5):
        _fire("sess-1", f"turn-{i}", "body")
    assert inventory_rule._MAX_TRACKED_TURNS >= len(inventory_rule._injected_turns)
    evicted = inventory_rule.hook(
        session_id="sess-1", turn_id="turn-0", user_message="body", **HOST_KWARGS
    )
    assert evicted is not None, "oldest turn was evicted and may be re-injected"


def test_register_wires_the_pre_llm_call_hook():
    hooks = []
    ctx = type("Ctx", (), {"register_hook": lambda self, name, fn: hooks.append((name, fn))})()
    guard.register(ctx)
    assert ("pre_llm_call", inventory_rule.hook) in hooks


def test_lead_kanban_create_without_inventory_block_passes(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    result = guard._pre_tool_call(
        tool_name="kanban_create",
        args={"title": "x", "assignee": "worker", "body": "Goal: split\n\n## Acceptance\n1. pytest -q exits 0"},
        task_id="",
        session_id="",
    )
    assert result is None
