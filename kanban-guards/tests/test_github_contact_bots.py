"""github_contact must not read a bot's API login as a person.

Regression from the guards audit: session ``20261019_184603_b6ae0d`` (5 fires in one session)
blocked ``gh api -X POST .../requested_reviewers -f 'reviewers[]=copilot-pull-request-reviewer[bot]'
-f 'reviewers[]=Copilot'``. ``_NOT_PEOPLE`` exempts ``@copilot`` but the API form carries no
``@``. Real human reviewer requests must still block.
"""

import sys

import pytest

guards = sys.modules["kanban_guards"]
gc = guards.github_contact


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_dem0002")
    monkeypatch.setattr(gc, "_login_cache", "nobody-here")
    monkeypatch.setattr(gc, "_login_resolved", True)


def run(command):
    return guards._pre_tool_call(tool_name="terminal", args={"command": command, "workdir": None})


AUDIT_COMMAND = (
    "gh api -X POST repos/acme/widget/pulls/1107/requested_reviewers "
    "-f 'reviewers[]=copilot-pull-request-reviewer[bot]' -f 'reviewers[]=Copilot'"
)


def test_audited_bot_review_request_passes():
    assert run(AUDIT_COMMAND) is None


@pytest.mark.parametrize("command", [
    AUDIT_COMMAND,
    "gh api -X POST repos/o/r/pulls/7/requested_reviewers -f 'reviewers[]=some-tool[bot]'",
    "gh api -X POST repos/o/r/pulls/7/requested_reviewers -f 'reviewers[]=Copilot'",
    "gh pr create -t t -b 'Closes #1.' -r 'copilot-pull-request-reviewer[bot]'",
])
def test_bot_logins_are_not_people(command):
    assert run(command) is None


@pytest.mark.parametrize("command", [
    "gh api -X POST repos/o/r/pulls/7/requested_reviewers -f 'reviewers[]=alice-dev'",
    "gh api -X POST repos/o/r/pulls/7/requested_reviewers -f 'reviewers[]=copilot-pull-request-reviewer[bot]' -f 'reviewers[]=alice-dev'",
    "gh pr create -t t -b 'Closes #1.' -r alice-dev",
])
def test_human_review_requests_still_block(command):
    result = run(command)
    assert result is not None and result["action"] == "block"
