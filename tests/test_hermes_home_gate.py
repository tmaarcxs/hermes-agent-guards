import re
import sys

import pytest

guards = sys.modules["agent_guards"]


def run(command, background=False):
    return guards._pre_tool_call(tool_name="terminal", args={"command": command, "background": background})


def blocked(command, background=False):
    result = run(command, background)
    return result is not None and result["action"] == "block"


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


def test_hermes_home_same_command_var_assignment_stays_blocked():
    assert blocked('HERMES_HOME="$(mktemp -d /tmp/gh-test-home.XXXXXX)" && rm -rf "$HERMES_HOME"')
    assert blocked("HERMES_HOME=$(mktemp -d /tmp/gh-test-home.XXXXXX); rm -rf $HERMES_HOME/")


def test_hermes_home_block_message_recipe_allowed_by_guard():
    from agent_guards.hermes_home_gate import _HERMES_HOME_DELETE_MESSAGE
    match = re.search(r'`env HERMES_HOME=.*?<test cmd>`', _HERMES_HOME_DELETE_MESSAGE)
    assert match, _HERMES_HOME_DELETE_MESSAGE
    recipe = match.group(0).strip("`")
    assert run(recipe) is None


def test_hermes_home_block_message_single_trailing_period():
    from agent_guards.hermes_home_gate import _HERMES_HOME_DELETE_MESSAGE
    assert _HERMES_HOME_DELETE_MESSAGE.endswith(".")
    assert not _HERMES_HOME_DELETE_MESSAGE.endswith("..")
