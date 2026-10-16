import importlib
import os
import sys

import pytest

try:
    guards = sys.modules["agent_guards"]
except KeyError:
    guards = None

swarm_rule = importlib.import_module("swarm_rule")


ACCEPT_BODY = (
    "Plan: p\nLane: 3 of 12 (siblings run in the same batch)\n\n"
    "## Acceptance\n1. the work lands.\n"
)


@pytest.fixture
def lead_mode(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    return swarm_rule


@pytest.fixture
def worker_mode(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    return swarm_rule


def _create(args):
    return swarm_rule.check("kanban_create", args)


def test_missing_lane_line_blocks_lead_card(lead_mode):
    message = _create({"title": "Fix the bug", "body": "## Acceptance\n1. done.\n"})
    assert message is not None
    assert "`Lane: k of N`" in message
    assert "10" in message


def test_lane_three_of_twelve_passes(lead_mode):
    assert _create({"title": "Lane title", "body": ACCEPT_BODY}) is None


def test_lane_bounds_first_and_last_pass(lead_mode):
    assert _create({"title": "first", "body": "Lane: 1 of 10\n"}) is None
    assert _create({"title": "last", "body": "Lane: 10 of 10\n"}) is None


def test_lane_below_swarm_minimum_blocks(lead_mode):
    message = _create({"title": "small", "body": "Lane: 2 of 9\n"})
    assert message is not None


def test_lane_index_above_total_blocks(lead_mode):
    message = _create({"title": "over", "body": "Lane: 13 of 12\n"})
    assert message is not None


def test_zero_lane_index_blocks(lead_mode):
    message = _create({"title": "zero", "body": "Lane: 0 of 10\n"})
    assert message is not None


def test_non_numeric_lane_line_blocks(lead_mode):
    message = _create({"title": "words", "body": "Lane: x of y\n"})
    assert message is not None


def test_non_kanban_create_tools_pass_through(lead_mode):
    assert swarm_rule.check("terminal", {"command": "hermes kanban create --title 'x'"}) is None
    assert swarm_rule.check("kanban_request_review", {"reviewer": "worker"}) is None


def test_worker_context_passes_without_lane_line(worker_mode):
    assert _create({"title": "worker card", "body": "## Acceptance\n1. done.\n"}) is None


def test_non_dict_args_block_in_lead_mode(lead_mode):
    assert swarm_rule.check("kanban_create", None) is not None


def test_block_message_is_one_line(lead_mode):
    message = _create({"title": "m", "body": "no declaration\n"})
    assert "\n" not in message


def test_non_empty_parents_exempts_lead_card_without_lane_line(lead_mode):
    assert _create({"title": "Rework", "parents": ["t_x"], "body": "## Acceptance\n1. done.\n"}) is None


def test_non_empty_parents_exempts_without_validation(lead_mode):
    assert _create({"title": "Rework", "parents": ["t_x"], "body": "Lane: 99 of 9\n"}) is None


def test_empty_parents_does_not_exempt(lead_mode):
    assert _create({"title": "Lane card", "parents": [], "body": "no declaration\n"}) is not None


def test_missing_parents_does_not_exempt(lead_mode):
    assert _create({"title": "Lane card", "body": "no declaration\n"}) is not None


@pytest.mark.skipif(guards is None, reason="agent_guards not importable without conftest")
def test_chain_parents_exemption_and_blocked_lead_card(monkeypatch):
    """N4: the registered pre_tool_call chain carries the parents exemption."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    child = guards._pre_tool_call(
        tool_name="kanban_create",
        args={"title": "Rework", "parents": ["t_x"], "body": "## Acceptance\n1. done.\n"},
    )
    assert child is None
    blocked = guards._pre_tool_call(
        tool_name="kanban_create",
        args={"title": "Lane card", "body": "## Acceptance\n1. done.\n"},
    )
    assert blocked is not None and blocked.get("action") == "block"
    assert "`Lane: k of N`" in blocked["message"]
