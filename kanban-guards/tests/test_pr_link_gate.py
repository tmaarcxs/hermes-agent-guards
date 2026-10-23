import sys

import pytest

guards = sys.modules["kanban_guards"]
pl = guards.pr_link_gate


def run(command, workdir=None):
    return guards._pre_tool_call(tool_name="terminal", args={"command": command, "workdir": workdir})


@pytest.fixture(autouse=True)
def worker(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_dem0003")
    monkeypatch.setattr(pl, "_linked", lambda ref, repo, cwd: LINKED[0])


LINKED = [[{"number": 7}]]


def set_linked(value):
    LINKED[0] = value


@pytest.mark.parametrize("command", [
    "gh pr merge 5 -R o/r --squash",
    "gh pr merge https://github.com/o/r/pull/5",
    "gh api -X PUT repos/o/r/pulls/5/merge",
    "cd x && gh pr merge 5",
])
@pytest.mark.parametrize("linked", [[], None])
def test_merge_without_link_blocked(command, linked):
    set_linked(linked)
    result = run(command)
    assert result and "pr_link_gate" in result["message"] and "Closes #N" in result["message"]


@pytest.mark.parametrize("command", [
    "gh pr merge 5 --squash",
    "gh api -X PUT repos/o/r/pulls/5/merge",
    "gh pr view 5",
    "gh api repos/o/r/pulls/5/merge",  # GET: read-only
])
def test_merge_with_link_or_read_passes(command):
    set_linked([{"number": 7}])
    assert run(command) is None


@pytest.mark.parametrize("body", ["Related to #7.", "Refs #7", "no keyword", "Closes #7. Closes #8."])
def test_create_edit_bad_body_blocked(body):
    for cmd in ("create", "edit 5"):
        result = run(f"gh pr {cmd} --body '{body}'")
        assert result and "closing keyword" in result["message"]


def test_create_edit_good_or_unverifiable_passes(tmp_path):
    (tmp_path / "ok.md").write_text("## What\nx\n\nCloses #7.")
    (tmp_path / "bad.md").write_text("Related to #7.")
    assert run("gh pr create -t t --body 'Closes #7.'") is None
    assert run("gh pr edit 5 --body-file ok.md", str(tmp_path)) is None
    assert run("gh pr edit 5 --title new") is None  # no body flag: nothing to verify
    assert run("gh pr create --body-file -") is None  # stdin: fail open
    assert run("gh pr edit 5 --body-file bad.md", str(tmp_path))


def test_not_a_worker_passes(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    set_linked([])
    assert run("gh pr merge 5") is None
