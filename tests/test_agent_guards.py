import sys

import pytest

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
    assert sorted(hooks) == ["on_kanban_dispatch_tick"] * 6 + ["post_tool_call"] * 2 + ["pre_llm_call", "pre_tool_call", "pre_verify", "transform_tool_result"]


def test_large_inline_attach_blocked():
    result = guards._pre_tool_call(tool_name="kanban_attach", args={"filename": "r.json", "content_base64": "QUJD" * 65})
    assert result["action"] == "block" and "attach by path" in result["message"]


def test_small_inline_attach_allowed():
    assert guards._pre_tool_call(tool_name="kanban_attach", args={"filename": "r.txt", "content_base64": "QUJD" * 64}) is None


RECURSIVE_RMS = ["-r", "-R", "-rf", "-fr", "-Rf", "--recursive"]
DEPTH_TARGETS = [
    ("protected", "/home/u/.hermes", True),
    ("ancestor", "/Users/v", True),
    ("below", "~/projects/x", False),
    ("unrelated", "/tmp/build", False),
]


@pytest.mark.parametrize("flag", RECURSIVE_RMS)
@pytest.mark.parametrize("target", DEPTH_TARGETS, ids=lambda target: target[0])
def test_recursive_rm_matrix(flag, target):
    name, path, expected = target
    assert blocked("rm %s %s" % (flag, path)) is expected


@pytest.mark.parametrize("target", DEPTH_TARGETS, ids=lambda target: target[0])
def test_plain_rm_matrix(target):
    name, path, expected = target
    assert not blocked("rm -f %s" % path)


ANCESTOR_BLOCKED_FORMS = [
    "rm --recursive /Users/u/.hermes",
    "rm --recursive $HERMES_HOME",
    "rm --recursive /home/v/.hermes",
    "rm -R ~/.hermes",
    "rm -rf /Users/v/.hermes",
    "rm -rf /home/v/.hermes/profiles/lead",
    "rm -rf /root/.hermes",
    "rm -rf ~",
    "rm -rf ~/",
    'rm -rf "$HOME"',
    "rm -rf ${HOME}/",
    'rm -rf "/Users/v/"',
    "rm -rf /home/v/",
    "rm -rf /root/",
    "rm -rf /",
    "rm -rf '/Users/v/'",
    "find ~ -delete",
    "find / -delete",
    "find /Users/v -delete",
    "find ~ -maxdepth 3 -delete",
    "rsync -a --delete /tmp/seed/ /Users/v/",
    "rsync -a --delete /tmp/seed/ /",
    "rsync -a --delete /tmp/seed/ /home/v/.hermes",
    'python3 -c "import shutil; shutil.rmtree(\'/Users/v\')"',
    'python3 -c "import shutil; shutil.rmtree(\'/\')"',
    'python3 -c "import shutil; shutil.rmtree(\'/root\')"',
    "python3 - <<'PY'\nimport shutil\nshutil.rmtree(os.environ['HERMES_HOME'])\nPY",
]

BELOW_AND_PROSE_ALLOWED_FORMS = [
    "rm -rf ~/projects/x",
    "rm -rf /Users/v/projects/x",
    "rm -rf /root/projects/x",
    "rm -rf ~someone/projects/x",
    "rm -rf /Users",
    "rm -rf /home",
    "rm -rf /tmp/build",
    'rm -rf "/Users/v/.hermesA"',
    "rm -rf /Users/v/.hermes-backup",
    "find ~ -name '*.db' -delete",
    "find /Users/v -name '*.pyc' -delete",
    "find ~/projects/x -name '*.tmp' -delete",
    "rsync -a --delete /Users/v/.hermes/ /tmp/backup/",
    "rsync -a /tmp/seed/ /Users/v/.hermes/",
    "rsync -a --delete /tmp/seed/ ~/projects/x/",
    'python3 -c "import shutil; shutil.rmtree(\'~/projects/x\')"',
    'python3 -c "import shutil; shutil.rmtree(\'/tmp/build\')"',
    'python3 -c "import shutil; shutil.rmtree(\'/Users/v/projects/x\')"',
    "cat > fragment.md <<'EOF'\nA shutil.rmtree('/Users/v/.hermes') here would delete notes.\nEOF\ncat fragment.md",
    "grep 'shutil.rmtree(\"/Users/v/.hermes\")' notes.md",
    "echo \"a shutil.rmtree('/Users/v/.hermes') b\"",
    "grep 'rm -rf /Users/v' notes.md",
]

FIND_DELETE_MATRIX = [
    ("find $HERMES_HOME -delete", True),
    ("find /Users/v -delete", True),
    ("find /home/u/.hermes -delete", True),
    ("find / -delete", True),
    ("find ~ -delete", True),
    ("find ~ -name '*.db' -delete", False),
    ("find /Users/v -name '*.pyc' -delete", False),
    ("find / -maxdepth 2 -name '*.db'", False),
    ("find ~/.hermes -maxdepth 2 -name '*.db'", False),
    ("find $HERMES_HOME -name '*.db'", False),
    ("find /home/u/.hermes -name '*.db' -exec rm -rf {} +", True),
    ("find ~/projects -name '*.pyc' -exec rm -rf {} +", False),
]

RSYNC_MATRIX = [
    ("rsync -a --delete /tmp/seed/ /home/u/.hermes/", True),
    ("rsync -a --delete /Users/v/.hermes/ /tmp/backup/", False),
    ("rsync -a /tmp/seed/ /home/u/.hermes/", False),
    ("rsync -a --delete /tmp/seed/ ~/projects/x/", False),
    ("rsync -a --delete /home/u/.hermes /Users/v", True),
]

PYTHON_MATRIX = [
    ('python3 -c "import shutil; shutil.rmtree(os.environ[\'HERMES_HOME\'])"', True),
    ('python3 -c "import shutil; shutil.rmtree(\'/home/u/.hermes/profiles/lead\')"', True),
    ('python3 -c "import shutil; shutil.rmtree(\'/Users/v\')"', True),
    ('python3 -c "import shutil; shutil.rmtree(\'~/projects/x\')"', False),
    ('python3 -c "import shutil; shutil.rmtree(\'/Users/v/projects/x\')"', False),
]


@pytest.mark.parametrize("command", ANCESTOR_BLOCKED_FORMS)
def test_ancestor_and_long_form_blocked(command):
    assert blocked(command)


@pytest.mark.parametrize("command", BELOW_AND_PROSE_ALLOWED_FORMS)
def test_below_and_prose_allowed(command):
    assert not blocked(command)


@pytest.mark.parametrize("command,expected", FIND_DELETE_MATRIX + RSYNC_MATRIX + PYTHON_MATRIX)
def test_qualifying_token_and_direction_matrix(command, expected):
    assert blocked(command) is expected


XARGAS_RUNNERS = ["xargs", "env xargs", "sudo xargs"]

XARGS_BLOCKED = [
    "echo /Users/u/.hermes | xargs rm -rf",
    'printf "%s" /home/u/.hermes | xargs rm -rf',
    "xargs rm -rf /Users/u/.hermes < list",
    "find /home/u/.hermes | xargs rm -rf",
    'find /home/u/.hermes -name "*.db" -print0 | xargs -0 rm -rf',
    "find /Users/v -maxdepth 1 -print0 | xargs -0 rm -rf",
    "ls $(find /home/u/.hermes) | xargs rm -rf",
]
XARGS_ALLOWED = [
    "echo /Users/u/.hermes | xargs printf %s\\n",
    "find /Users/v/projects -name '*.pyc' | xargs rm -f",
    "find /Users/v/projects -name '*.pyc' -delete",
    "cat list | xargs rm /Users/v/.hermesA",
]


@pytest.mark.parametrize("runner", XARGAS_RUNNERS)
@pytest.mark.parametrize("command,expected", [(c, True) for c in XARGS_BLOCKED] + [(c, False) for c in XARGS_ALLOWED])
def test_xargs_home_guard_matrix(command, runner, expected):
    command = command.replace("xargs ", runner + " ")
    assert blocked(command) is expected


RUNNERS = ["xargs", "xargs -0", "xargs -I{}", "parallel", "env", "sudo", "nice", "nohup", "command", "exec", "time"]
RUNNER_VERBS = ["rm -r", "rm -rf", "rm -R", "find -maxdepth 0 -delete", "rsync --delete -a /tmp/seed/"]
RUNNER_PROTECTED = ["/Users/u/.hermes", "/home/u/.hermes/profiles/w", "$HERMES_HOME", "~/.hermes", "$HOME/.hermes", "~", "$HOME", "/Users/u", "/"]
RUNNER_BELOW = ["~/.hermes/cache/scratch/x", "~/projects/x"]


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("verb", RUNNER_VERBS)
@pytest.mark.parametrize("target", RUNNER_PROTECTED)
def test_runner_pipe_home_guard_matrix(runner, verb, target):
    assert blocked("echo %s | %s %s" % (target, runner, verb))


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("verb", RUNNER_VERBS)
@pytest.mark.parametrize("target", RUNNER_BELOW)
def test_runner_pipe_below_guard_matrix(runner, verb, target):
    assert not blocked("echo %s | %s %s" % (target, runner, verb))


@pytest.mark.parametrize("runner", RUNNERS)
def test_runner_pipe_filtered_find_matrix(runner):
    assert not blocked("echo /Users/u | %s find -name '*.db' -delete" % runner)
    assert not blocked("echo / | %s find -name '*.db' -delete" % runner)
    assert blocked("echo /Users/u | %s find -delete" % runner)
    assert blocked("echo /Users/u/.hermes | %s find -name '*.db' -delete" % runner)
    assert blocked("echo /Users/u/.hermes | %s find -exec rm -rf {} +" % runner)


QUOTED_PROSE_PIPE_MATRIX = [
    ("echo 'rm -rf /Users/u/.hermes' | xargs rm -rf /tmp/x", False),
    ("echo '/Users/u/.hermes live' | grep -c hermes", False),
    ("grep 'xargs rm -rf' logs | tail -1", False),
    ('echo "$HOME/.hermes" | xargs rm -rf', True),
    ('printf "%s" /home/u/.hermes | xargs rm -rf', True),
]


@pytest.mark.parametrize("command,expected", QUOTED_PROSE_PIPE_MATRIX)
def test_quoted_prose_pipe_matrix(command, expected):
    assert blocked(command) is expected


RUNNER_DASHDASH_MATRIX = [
    ("env -- rm -rf /Users/u/.hermes", True),
    ("sudo -- rm -rf /Users/u/.hermes", True),
    ("echo /Users/u/.hermes | xargs -- rm -rf", True),
    ("echo /Users/u/.hermes | env -- rm -rf", True),
    ("echo /Users/u/.hermes | xargs -- rm -rf /tmp/x", True),
    ("echo /Users/u/.hermes | xargs -0 -- rm -rf", True),
    ("echo '/Users/u/.hermes live' | xargs -- grep -c hermes", False),
    ("echo /Users/u | xargs -- find -name '*.db' -delete", False),
    ("echo /Users/u/.hermes | env -- xargs rm -rf", True),
]

DASHDASH_PASS_THROUGH_RUNNERS = ["env --", "sudo --", "xargs --", "xargs -0 --"]


@pytest.mark.parametrize("runner", DASHDASH_PASS_THROUGH_RUNNERS)
@pytest.mark.parametrize("verb", RUNNER_VERBS)
@pytest.mark.parametrize("target", RUNNER_PROTECTED)
def test_runner_dashdash_pipe_home_guard_matrix(runner, verb, target):
    assert blocked("echo %s | %s %s" % (target, runner, verb))


@pytest.mark.parametrize("runner", DASHDASH_PASS_THROUGH_RUNNERS)
@pytest.mark.parametrize("verb", RUNNER_VERBS)
@pytest.mark.parametrize("target", RUNNER_BELOW)
def test_runner_dashdash_pipe_below_guard_matrix(runner, verb, target):
    assert not blocked("echo %s | %s %s" % (target, runner, verb))


CONTINUATION_MATRIX = [
    ("echo /Users/u/.hermes | xargs rm -rf", True),
    ('echo "$HOME"/.hermes | xargs rm -rf', True),
    ("echo /Users/u/.hermes | xargs printf %s\n", False),
]


@pytest.mark.parametrize("command,expected", [(c.replace(" | ", " \\\n| "), e) for c, e in CONTINUATION_MATRIX])
def test_backslash_newline_continuation_pipe(command, expected):
    assert blocked(command) is expected
