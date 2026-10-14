import sys

import pytest

guards = sys.modules["agent_guards"]


def run(command, background=False):
    return guards._pre_tool_call(tool_name="terminal", args={"command": command, "background": background})


def blocked(command, background=False):
    result = run(command, background)
    return result is not None and result["action"] == "block"


@pytest.mark.parametrize(
    "command",
    [
        "sleep 60",
        "sleep 420; gh pr checks 1",
        "cd x && sleep 2m",
        "for i in 1 2; do sleep 90; done",
        "X=1 sleep 120",
        "gh pr merge 12 --repo o/r --merge --admin",
        "set -e; gh pr merge 12 --merge && gh pr view 12",
        "m(){ gh pr merge $1 --merge; }",
        "osascript -e 'tell app \"Safari\" to activate'",
        "x=$(osascript -e 1)",
        "git push origin HEAD --force",
        "git push -f origin main",
        "git push --force origin b 2>&1 | tail -1",
    ],
)
def test_blocks(command):
    assert blocked(command)


@pytest.mark.parametrize(
    "command",
    [
        "sleep 59",
        "sleep 5; ls",
        "gh pr merge 12 --merge --admin --match-head-commit abc123",
        "m(){ gh pr merge $1 --merge --admin --match-head-commit $h; }",
        "grep -n 'gh pr merge' notes.md",
        "echo osascript is banned",
        "grep -c sleep log.txt",
        "git push --force-with-lease origin b",
        "git push origin b",
        "git push origin feature/x-f",
        "python3 -c \"print('sleep 600')\"",
        "grep -rn \"osascript\\\\|--admin\\\\|gh pr merge\" ~/.hermes/plugins",
        "python3 -c 'p=\"backups/worker-osascript-prohibition\"; print(p)'",
        "gh pr merge 1 --merge --match-head-commit \"$h\"",
    ],
)
def test_allows(command):
    assert run(command) is None


def test_background_sleep_allowed():
    assert run("sleep 600", background=True) is None


def test_background_still_checks_merge():
    assert blocked("gh pr merge 1 --merge", background=True)


def test_all_violations_in_one_message():
    message = run("sleep 300; gh pr merge 1 --merge; git push -f")["message"]
    assert "sleep" in message and "match-head-commit" in message and "force-with-lease" in message


def test_empty_kanban_args_become_absent():
    result = guards._pre_tool_call(tool_name="kanban_list", args={"assignee": "", "status": "ready", "limit": 5})
    assert result == {"action": "modify", "args": {"assignee": None}}
    result = guards._pre_tool_call(tool_name="kanban_create", args={"title": "t", "completion_contract": ""})
    assert result == {"action": "modify", "args": {"completion_contract": None}}


def test_valid_kanban_args_untouched():
    assert guards._pre_tool_call(tool_name="kanban_list", args={"assignee": "worker"}) is None
    assert guards._pre_tool_call(tool_name="kanban_create", args={"completion_contract": "o/r"}) is None


def test_doubled_skill_name_normalized():
    result = guards._pre_tool_call(tool_name="skill_view", args={"name": "ego-browser:ego-browser"})
    assert result == {"action": "modify", "args": {"name": "ego-browser"}}


def test_plugin_qualified_skill_untouched():
    assert guards._pre_tool_call(tool_name="skill_view", args={"name": "superpowers:writing-plans"}) is None
    assert guards._pre_tool_call(tool_name="skill_view", args={"name": "github"}) is None


def test_other_tools_and_bad_args_fail_open():
    assert guards._pre_tool_call(tool_name="read_file", args={"path": "x"}) is None
    assert guards._pre_tool_call(tool_name="terminal", args="not-a-dict") is None


@pytest.mark.parametrize("literal", ["$HERMES_KANBAN_TASK", "HERMES_KANBAN_TASK", "${HERMES_KANBAN_TASK}", " $HERMES_KANBAN_TASK "])
@pytest.mark.parametrize("tool", ["kanban_show", "kanban_complete", "kanban_comment", "kanban_block"])
def test_env_task_literal_becomes_absent(tool, literal):
    result = guards._pre_tool_call(tool_name=tool, args={"task_id": literal, "summary": "s"})
    assert result == {"action": "modify", "args": {"task_id": None}}


def test_real_task_ids_and_other_tools_untouched():
    assert guards._pre_tool_call(tool_name="kanban_show", args={"task_id": "t_a32b8765"}) is None
    assert guards._pre_tool_call(tool_name="kanban_show", args={}) is None
    assert guards._pre_tool_call(tool_name="read_file", args={"task_id": "$HERMES_KANBAN_TASK"}) is None
    assert guards._pre_tool_call(tool_name="kanban_show", args={"task_id": "$HERMES_KANBAN_TASK_X"}) is None


def test_register_wires_every_hook():
    import types

    hooks = []
    ctx = types.SimpleNamespace(register_hook=lambda name, fn: hooks.append(name))
    guards.register(ctx)
    assert sorted(hooks) == ["on_kanban_dispatch_tick", "on_kanban_dispatch_tick", "post_tool_call", "pre_tool_call", "pre_verify"]


def test_large_inline_attach_blocked():
    result = guards._pre_tool_call(tool_name="kanban_attach", args={"filename": "r.json", "content_base64": "QUJD" * 65})
    assert result["action"] == "block" and "hermes kanban attach" in result["message"]


def test_small_inline_attach_allowed():
    assert guards._pre_tool_call(tool_name="kanban_attach", args={"filename": "r.txt", "content_base64": "QUJD" * 64}) is None
