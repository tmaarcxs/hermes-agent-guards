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


CALL_TEMPLATES = [
    "import shutil; shutil.rmtree({a})",
    "import os; os.remove({a})",
    "import os; os.unlink({a})",
    "import os; os.rmdir({a})",
    "import pathlib; pathlib.Path({a}).rmdir()",
    "import pathlib; pathlib.Path({a}).unlink()",
]

TARGETS = ["~/.hermes", "$HOME/.hermes", "/Users/u/.hermes", "/Users/u/.hermes/profiles/x"]


def shell_arg(target, style):
    if style == "outer_single":
        return '"%s"' % target
    if style == "outer_double":
        return "'%s'" % target
    return '"%s"' % target


def py_command(interpreter, style, code):
    if style == "outer_single":
        return "%s -c '%s'" % (interpreter, code)
    return '%s -c "%s"' % (interpreter, code)


def python_matrix():
    for interpreter in ("python", "python3"):
        for style in ("outer_single", "outer_double", "escaped"):
            for template in CALL_TEMPLATES:
                for target in TARGETS:
                    arg = shell_arg(target, style)
                    code = template.format(a=arg)
                    if style == "escaped":
                        code = code.replace('"', '\\"')
                    yield py_command(interpreter, style, code)


@pytest.mark.parametrize("command", list(python_matrix()))
def test_python_delete_matrix_blocked(command):
    assert blocked(command)


BELOW_NEST_ALLOWED = [
    "python3 -c 'import shutil; shutil.rmtree(\"/Users/u/.hermes/cache/scratch/x\")'",
    "python3 -c \"import shutil; shutil.rmtree('/Users/u/.hermes/cache/scratch/x')\"",
    "python3 -c \"import shutil; shutil.rmtree(\\\"/Users/u/.hermes/cache/scratch/x\\\")\"",
    "python -c 'import pathlib; pathlib.Path(\"/Users/u/.hermes/cache/scratch/x\").rmdir()'",
    "python3 -c \"import os; os.remove('/Users/u/.hermes/cache/scratch/x')\"",
]

PROSE_ALLOWED = [
    "python -c \"print('rmtree')\"",
    "python3 -c \"print('rmtree')\"",
    "python -c \"import sys; print('rm -rf /Users/u/.hermes')\"",
    "grep 'shutil.rmtree(\"/Users/u/.hermes\")' notes.md",
    "echo \"shutil.rmtree('/Users/u/.hermes')\"",
    "echo \"pathlib.Path('/Users/u/.hermes').rmdir()\"",
    "echo 'os.remove(\"/Users/u/.hermes\")'",
    "python -c \"print('os.remove')\"",
]


@pytest.mark.parametrize("command", BELOW_NEST_ALLOWED + PROSE_ALLOWED)
def test_python_delete_allowances(command):
    assert not blocked(command)
