"""Every block message from shell-guards and workspace-guards names an alternative.

Marcos: a block that only says what is forbidden sends the worker back into the
runbook. Every block message must (1) say what was blocked in one clause and
(2) give the exact permitted alternative for the same intent: a concrete tool
call or shell command the model can copy, never a generic pointer ("see the
docs"). The gates already name it — this file pins the exact alternative text
per gate so a wording regression fails here, not on a real run.
"""

import sys

import pytest

guards = sys.modules["shell_guards"]


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setattr(guards._policy, "configured_worker", lambda: "worker")


def _run(command, background=False):
    """pty=True keeps the wait a block: a plain foreground wait is rewritten instead."""
    return guards._pre_tool_call(tool_name="terminal", args={"command": command, "background": background, "pty": True})


def _message(result):
    assert result is not None and result["action"] == "block"
    return result["message"]


def test_foreground_sleep_names_the_background_call_shape():
    message = _message(_run("sleep 300; echo done"))
    assert "terminal(command=<the same command>, background=true, notify=true)" in message
    assert "never sleep or poll" in message


def test_polling_loop_names_the_background_call_shape():
    message = _message(_run("for i in 1 2; do sleep 4; sleep 4; sleep 4; sleep 4; done"))
    assert "terminal(command=<the same loop>, background=true, notify=[<ready pattern>])" in message
    assert "never sleep or poll" in message


def test_poll_block_names_wait_log_and_notify_alternatives():
    message = guards.process_poll_gate.check("process_manage", {"action": "poll"})
    assert 'process_manage(action="wait", session_id=...)' in message
    assert 'process_manage(action="log", session_id=...)' in message
    assert "background=true, notify=true" in message


def test_gh_pr_merge_names_the_exact_flag_with_a_copyable_shape():
    message = _message(_run("gh pr merge 12 --repo o/r --merge --admin"))
    assert "--match-head-commit <head-sha-of-the-pull-request>" in message
    assert "gh pr merge" in message


def test_osascript_names_the_tool_alternative():
    message = _message(_run("osascript -e 'quit app \"Safari\"'"))
    assert "ego-browser" in message


def test_force_push_names_force_with_lease():
    message = _message(_run("git push origin main --force"))
    assert "--force-with-lease" in message


def test_gh_auth_names_its_own_exact_alternative():
    message = _message(_run("gh auth switch -u other"))
    assert "GH_TOKEN=$(gh auth token -u <user>)" in message


def test_stash_block_names_the_observation_alternatives():
    message = _message(_run("git stash pop"))
    assert "git show HEAD:<file>" in message
    assert "git diff" in message
    assert "git worktree add" in message


def test_attach_cap_names_the_artifacts_alternative():
    result = guards._pre_tool_call(
        tool_name="kanban_attach", args={"filename": "r.json", "content_base64": "QUJD" * 100}
    )
    message = _message(result)
    assert "artifacts=[absolute path]" in message
    assert "sha256" in message


def test_home_delete_names_the_throwaway_home_recipe():
    message = _message(_run('rm -rf "$HOME/.hermes"'))
    assert "mktemp -d" in message


def test_plugin_source_gates_name_the_next_step():
    dirty = guards.plugin_source_gate.activation_block("hermes plugins enable jot-guards")
    assert dirty is None


def test_bg_notify_message_names_the_call_shape():
    message = guards.background_notify_gate.check(
        "terminal", {"background": True, "command": "python3 replay.py > replay.log 2>&1"}
    )
    assert "terminal(command=<same command>, background=true, notify=true)" in message
