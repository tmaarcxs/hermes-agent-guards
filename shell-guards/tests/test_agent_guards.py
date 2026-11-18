import sys

import pytest
import re

guards = sys.modules["shell_guards"]


@pytest.fixture(autouse=True)
def _no_configured_worker(monkeypatch):
    monkeypatch.setattr(guards._policy, "configured_worker", lambda: "")


def run(command, background=False):
    return guards._pre_tool_call(tool_name="terminal", args={"command": command, "background": background})


def blocked(command, background=False):
    result = run(command, background)
    return result is not None and result["action"] == "block"


@pytest.mark.parametrize(
    "command",
    [

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
    result = run(command)
    assert result is None or result["action"] == "modify"


@pytest.mark.parametrize(
    "command",
    [
        'while pgrep -f "migrate.py" >/dev/null; do sleep 10; done; echo done',
        'while pgrep -f "migrate.py" >/dev/null; do sleep 15; done; echo done',
        'while pgrep -fl "migrate.py"; do sleep 2; done',
        'until pgrep -f migrate; do sleep 1; done',
        'while pgrep --full migrate.py; do sleep 1; done',
        'while pkill -f stale_worker; do sleep 1; done',
        'for i in 1 2; do pgrep -f target && break; done',
    ],
)
def test_self_matching_pgrep_loop_is_blocked(command):
    result = run(command)
    assert result and result["action"] == "block"


def test_self_matching_pgrep_message_names_the_fix():
    result = run('while pgrep -f "migrate.py" >/dev/null; do sleep 1; done')
    assert result and result["action"] == "block"
    assert "pgrep" in result["message"] and "self-matches" in result["message"]


@pytest.mark.parametrize(
    "command",
    [
        'while pgrep -f "migrate.py" >/dev/null; do sleep 10; done; echo done',
        'while pgrep -f "migrate.py" >/dev/null; do sleep 15; done; echo done',
    ],
)
def test_self_matching_pgrep_loop_blocked_in_background_too(command):
    """Background does not exempt it: the loop can never exit, it is a hang."""
    result = run(command, background=True)
    assert result and result["action"] == "block"
    assert "self-matches" in result["message"]


@pytest.mark.parametrize(
    "command",
    [
        "pgrep -f migrate.py",
        "pgrep -f migrate.py; echo done",
        "while [ -f ready ]; do sleep 1; done",
        "while kill -0 $p; do sleep 1; done",
        'echo "while pgrep -f x; do sleep 1; done"',
        "for i in 1 2 3; do pgrep migrate; done",
    ],
)
def test_pgrep_loop_allows(command):
    assert run(command) is None


def test_grep_for_a_pgrep_pattern_is_allowed():
    assert not blocked("grep -rn 'pgrep -f' notes.md")


def test_background_still_checks_merge():
    assert blocked("gh pr merge 1 --merge", background=True)


def test_all_violations_in_one_message():
    message = run("osascript -e 1; gh pr merge 1 --merge; git push -f")["message"]
    assert "osascript" in message and "match-head-commit" in message and "force-with-lease" in message


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


def test_doubled_name_of_registered_plugin_skill_untouched(monkeypatch):
    import hermes_cli.plugins as plugins

    class _PM:
        def find_plugin_skill(self, name):
            return object() if name == "ponytail:ponytail" else None

    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: _PM())
    assert guards._pre_tool_call(tool_name="skill_view", args={"name": "ponytail:ponytail"}) is None
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
    assert sorted(hooks) == [
        "post_tool_call", "pre_tool_call", "transform_terminal_output",
        "transform_tool_result",
    ]


def test_register_registers_exactly_the_plugin_yaml_hooks():
    import types
    from pathlib import Path

    import yaml

    declared = yaml.safe_load((Path(__file__).parents[1] / "plugin.yaml").read_text())["hooks"]
    hooks = []
    ctx = types.SimpleNamespace(register_hook=lambda name, fn: hooks.append(name))
    guards.register(ctx)
    assert sorted(hooks) == sorted(declared)
    assert len(hooks) == len(declared)


def test_large_inline_attach_blocked():
    result = guards._pre_tool_call(tool_name="kanban_attach", args={"filename": "r.json", "content_base64": "QUJD" * 65})
    assert result["action"] == "block" and "attach by path" in result["message"]


def test_small_inline_attach_allowed():
    assert guards._pre_tool_call(tool_name="kanban_attach", args={"filename": "r.txt", "content_base64": "QUJD" * 64}) is None


def test_process_wait_timeout_is_clamped_below_the_call_deadline(monkeypatch):
    monkeypatch.setattr(guards.terminal_policy, "_tool_deadline", lambda: 420.0)
    call = lambda **a: guards._pre_tool_call(tool_name="process_manage", args={"action": "wait", "session_id": "p1", **a})
    assert call(timeout=900) == {"action": "modify", "args": {"timeout": 405}}
    assert call(timeout=420) == {"action": "modify", "args": {"timeout": 405}}
    assert call(timeout=415) == {"action": "modify", "args": {"timeout": 405}}
    assert call(timeout=300) is None
    assert guards._pre_tool_call(tool_name="process_manage", args={"action": "poll", "session_id": "p1", "timeout": 900}) is None


def test_process_wait_cap_follows_the_configured_deadline(monkeypatch):
    monkeypatch.setenv("TERMINAL_TIMEOUT", "600")
    monkeypatch.setattr(guards.terminal_policy, "_tool_deadline", lambda: 900.0)
    result = guards._pre_tool_call(tool_name="process_manage", args={"action": "wait", "session_id": "p1", "timeout": 900})
    assert result == {"action": "modify", "args": {"timeout": 885}}


def test_process_wait_cap_absent_when_deadline_is_unbounded(monkeypatch):
    monkeypatch.setattr(guards.terminal_policy, "_tool_deadline", lambda: None)
    assert guards._pre_tool_call(tool_name="process_manage", args={"action": "wait", "session_id": "p1", "timeout": 900}) is None


def test_default_deadline_resolves_to_420():
    from agent.deadline import resolve_timeout
    deadline = resolve_timeout("tools.sequential_call", default=None) or resolve_timeout(
        "tools.concurrent_batch", default=420.0, env_var="HERMES_CONCURRENT_TOOL_TIMEOUT_S")
    assert deadline and guards.terminal_policy._wait_cap() == int(deadline) - 5


def test_missing_timeout_is_shimmed_and_keeps_exit_codes(monkeypatch):
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda name: None)
    expired = run("timeout 1 sleep 3")["args"]["command"]
    ok = run("timeout 5 true")["args"]["command"]
    assert subprocess.run(["bash", "-c", expired]).returncode == 124
    assert subprocess.run(["bash", "-c", ok]).returncode == 0
    assert "timeout" not in run("echo 'timeout 5 x'") if run("echo 'timeout 5 x'") else True


def test_real_timeout_binary_is_left_alone(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/timeout")
    assert run("timeout 5 true") is None
