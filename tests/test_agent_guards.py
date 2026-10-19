import sys

import pytest
import re

guards = sys.modules["agent_guards"]


@pytest.fixture(autouse=True)
def _no_configured_worker(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")


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
        "sleep 45; gh pr checks 1054 2>&1",
        "sleep 30 && gh pr view 400 --repo o/r",
        "sleep 2m; echo done",
        "cd x && sleep 2m",
        "for i in 1 2; do sleep 90; done",
        "for i in $(seq 1 12); do st=$(gh run view 1 --json status); sleep 20; done",
        "for i in $(seq 1 30); do ST=$(status); sleep 30; done",
        "while kill -0 $p 2>/dev/null && [ $w -lt 520 ]; do sleep 20; done",
        "for i in 1 2; do sleep 15; done",
        "X=1 sleep 120",
        "gh pr merge 12 --repo o/r --merge --admin",
        "set -e; gh pr merge 12 --merge && gh pr view 12",
        "m(){ gh pr merge $1 --merge; }",
        "osascript -e 'tell app \"Safari\" to activate'",
        "x=$(osascript -e 1)",
        "git push origin HEAD --force",
        "git push -f origin main",
        "git push --force origin b 2>&1 | tail -1",
        "rm -rf \"$HERMES_HOME\"",
        "rm -rf ${HERMES_HOME}/",
        "cd /tmp && rm -rf $HERMES_HOME",
        "rm -r ~/.hermes/profiles/worker",
        "rm -fr /home/u/.hermes",
        "rm -fr \"$HOME/.hermes\"",
        "rm -rf /Users/u/.hermes",
        "rm -rf /Users/u/.hermes/profiles",
        "rm -rf /Users/u/.hermes/profiles/lead",
        "find $HERMES_HOME -delete",
        "find /home/u/.hermes/profiles -name '*.db' -delete",
        "rsync -a --delete /tmp/seed/ /home/u/.hermes/",
        "rsync -a --delete /tmp/seed/ /home/u/.hermes/profiles/worker",
        "python3 -c \"import shutil; shutil.rmtree(os.environ['HERMES_HOME'])\"",
        "python3 -c \"import shutil; shutil.rmtree('/home/u/.hermes/profiles/lead')\"",
        "python3 - <<'PYEOF'\nimport shutil, os\nshutil.rmtree(os.environ['HERMES_HOME'])\nPYEOF",
        "rm -fr ${HERMES_HOME} ; echo done",
        "rm -rf $HERMES_HOME/ /home/u/.hermes/profiles",
    ],
)
def test_blocks(command):
    assert blocked(command)


@pytest.mark.parametrize(
    "command",
    [
        "sleep 5; ls",
        "sleep 29",
        "for i in $(seq 1 30); do pg_isready -U appdb && break; sleep 1; done",
        "for i in $(seq 1 45); do code=$(curl -s -m 2 http://127.0.0.1:55518/readiness); sleep 2; done",
        "for i in 1 2 3 4 5 6 7 8; do curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:55518; sleep 2; done",
        "until [ -f ready ]; do sleep 10; done",
        "for i in 1 2; do sleep 14; done",
        "gh pr merge 12 --merge --admin --match-head-commit abc123",
        "m(){ gh pr merge $1 --merge --admin --match-head-commit $h; }",
        "grep -n 'gh pr merge' notes.md",
        "echo osascript is banned",
        "grep -c sleep log.txt",
        "git push --force-with-lease origin b",
        "git push origin b",
        "git push origin feature/x-f",
        "python3 -c \"print('sleep 600')\"",
        "cat > poll.sh <<'EOF'\nfor i in $(seq 1 30); do sleep 60; done\nEOF\nbash poll.sh",
        "python3 - <<'PYEOF'\nimport time\ntime.sleep(600)\nPYEOF",
        "grep -rn \"osascript\\\\|--admin\\\\|gh pr merge\" ~/.hermes/plugins",
        "python3 -c 'p=\"backups/worker-osascript-prohibition\"; print(p)'",
        "gh pr merge 1 --merge --match-head-commit \"$h\"",
        "echo 'sleep 45' > instructions.md",
        "rm -rf ~/.hermes/cache/scratch/x",
        "rm -rf \"$PWD/.hermes-test-home.ab12\"",
        "rm -rf /home/u/.hermes/kanban/workspaces/t_abc/tmp",
        "rm -rf /home/u/.hermes/profiles/worker/cache/scratch/x",
        "rm -f ~/.hermes/profiles/worker/logs/agent.log",
        "grep 'rm -rf $HERMES_HOME' notes.md",
        "cat > fragment.md <<'EOF'\nAs configured, a rm -rf $HERMES_HOME here would delete notes.\nEOF\ncat fragment.md",
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


def test_empty_kanban_args_become_absent(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    result = guards._pre_tool_call(tool_name="kanban_list", args={"assignee": "", "status": "ready", "limit": 5})
    assert result == {"action": "modify", "args": {"assignee": None}}
    result = guards._pre_tool_call(tool_name="kanban_create", args={"title": "t", "completion_contract": ""})
    assert result == {"action": "modify", "args": {"completion_contract": None}}


def test_valid_kanban_args_untouched(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
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
    assert guards._pre_tool_call(tool_name="kanban_show", args={"task_id": "t_show0005"}) is None
    assert guards._pre_tool_call(tool_name="kanban_show", args={}) is None
    assert guards._pre_tool_call(tool_name="read_file", args={"task_id": "$HERMES_KANBAN_TASK"}) is None
    assert guards._pre_tool_call(tool_name="kanban_show", args={"task_id": "$HERMES_KANBAN_TASK_X"}) is None


def test_register_wires_every_hook():
    import types

    hooks = []
    ctx = types.SimpleNamespace(register_hook=lambda name, fn: hooks.append(name))
    guards.register(ctx)
    assert sorted(hooks) == ["on_kanban_dispatch_tick"] * 6 + ["post_tool_call"] * 2 + ["pre_tool_call", "pre_verify", "transform_tool_result"]


def test_large_inline_attach_blocked():
    result = guards._pre_tool_call(tool_name="kanban_attach", args={"filename": "r.json", "content_base64": "QUJD" * 65})
    assert result["action"] == "block" and "attach by path" in result["message"]


def test_small_inline_attach_allowed():
    assert guards._pre_tool_call(tool_name="kanban_attach", args={"filename": "r.txt", "content_base64": "QUJD" * 64}) is None
