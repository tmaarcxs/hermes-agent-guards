"""Foreground waits that outlive the liveness clock are bounced to the background form.

The crash audit found 10 worker runs and 8.9 h lost to a foreground ``terminal``
call that stays silent past the tool deadline (``sleep 480; gh run list`` being
the anchor) and then dies on the 600 s liveness watchdog. The sleep and loop
ceilings already live in ``terminal_violations`` (those block from 30 s / 15 s
with their own message); ``silent_wait_gate`` covers the shapes they miss at the
card's 120 s threshold and names the exact corrected call in one short message.
"""

import sys

import pytest

guards = sys.modules["agent_guards"]

from test_retry_instruction import assert_retry_instruction

WORKER_ENV = {"HERMES_KANBAN_TASK": "t_wait1"}


def call(command, **extra):
    return guards._pre_tool_call(tool_name="terminal", args={"command": command, **extra})


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_wait1")
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    return


@pytest.mark.usefixtures("worker")
@pytest.mark.parametrize(
    "command",
    [
        "sleep 480; gh run list --repo o/r",
        "sleep 600",
        "sleep 2m; echo done",
        "sleep 120",
        "X=1 sleep 900",
        "timeout 300 sleep 3600",
        "sleep infinity",
        "tail -f log",
        "pnpm exec vite build 2>&1 | tail -f log",
        "tail -F access.log",
        "docker compose logs --follow app",
        "docker logs -f web",
        "journalctl -f -u app",
        "while true; do gh run list; sleep 300; break; done",
        "while :; do echo tick; sleep 5m; break; done",
        "for ((;;)); do sleep 10m; break; done",
    ],
)
def test_blocks(command):
    result = call(command)
    assert result["action"] == "block"
    assert "foreground wait" in result["message"]


@pytest.mark.usefixtures("worker")
@pytest.mark.parametrize(
    "args",
    [
        {"command": "sleep 480; gh run list --repo o/r", "background": True, "notify": True},
        {"command": "tail -f log", "background": True, "notify": True},
        {"command": "sleep 480; gh run list --repo o/r", "background": True, "notify": ["BUILD OK"]},
        {"command": "sleep 29; gh run list --repo o/r"},
        {"command": "sleep 29"},
        {"command": "echo 'sleep 480' > notes.md"},
        {"command": "echo tail -f log"},
        {"command": "echo docker logs -f web"},
        {"command": "grep -c 'tail -f' notes.md"},
        {"command": "cat <<'EOF'\nsleep infinity\nEOF"},
        {"command": "python3 -c \"print('sleep 600')\""},
        {"command": "docker compose logs app"},
        {"command": "tail log"},
        {"command": "tail -n 20 log"},
        {"command": "sleep 5m", "background": True, "notify": True},
        {"command": "tail -f log", "pty": True},
    ],
)
def test_allows(args):
    assert call(**args) is None


@pytest.mark.usefixtures("worker")
def test_foreground_heartbeat_is_no_outcome_channel():
    result = call("sleep 480; gh run list", heartbeat=60)
    assert result["action"] == "block"


@pytest.mark.usefixtures("worker")
def test_sleep_30_passes_the_new_rule():
    assert guards.silent_wait_gate.check("terminal", {"command": "sleep 30; gh run list"}) is None
    assert guards.silent_wait_gate.check("terminal", {"command": "sleep 30"}) is None


def test_lead_process_unaffected(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert guards.silent_wait_gate.check("terminal", {"command": "sleep 480; gh run list"}) is None
    assert guards.silent_wait_gate.check("terminal", {"command": "tail -f log"}) is None


@pytest.mark.usefixtures("worker")
def test_message_names_the_exact_corrected_call():
    message = call("sleep 480; gh run list --repo o/r")["message"]
    assert "background=true" in message and "notify=true" in message


@pytest.mark.usefixtures("worker")
def test_message_carries_retry_instruction():
    assert_retry_instruction(call("tail -f log")["message"])


@pytest.mark.usefixtures("worker")
def test_message_names_the_bounded_form_for_loops():
    message = call("while true; do gh run list; sleep 300; break; done")["message"]
    assert "bounded form" in message
