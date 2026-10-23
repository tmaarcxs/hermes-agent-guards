"""kanban_policy must name the profile that exists in the reviewer rejection.

Regression from the guards audit: 14 worker fires, 10 re-blocked, 9.5 calls median recovery.
The message said "reviewer must be 'worker' or omitted" without saying that no other profile
exists, so the agent retried 'default'/'reviewer'/'implementer'/'sdlc-review' until it landed
on the configured value. The literal configured name must be in the message.
"""

import sys
import types

import pytest

guards = sys.modules["kanban_guards"]
policy = guards.kanban_policy


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setattr(policy, "configured_worker", lambda: "worker")


@pytest.mark.parametrize("reviewer", ["default", "reviewer", "implementer", "sdlc-review"])
def test_message_names_the_configured_profile(reviewer):
    message = policy.check("kanban_request_review", {"summary": "s", "reviewer": reviewer})
    assert "'worker'" in message
    assert "kanban.default_assignee" in message
    assert "is not a profile that exists" in message


def test_message_uses_whatever_profile_is_configured(monkeypatch):
    monkeypatch.setattr(policy, "configured_worker", lambda: "builder")
    message = policy.check("kanban_request_review", {"summary": "s", "reviewer": "reviewer"})
    assert "'builder'" in message and "'worker'" not in message


def test_blocking_behaviour_is_unchanged():
    assert policy.check("kanban_request_review", {"summary": "s"}) is None
    assert policy.check("kanban_request_review", {"summary": "s", "reviewer": "worker"}) is None
    assert policy.check("kanban_request_review", {"summary": "s", "reviewer": "reviewer"}) is not None
