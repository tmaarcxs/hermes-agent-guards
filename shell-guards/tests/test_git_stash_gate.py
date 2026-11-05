import sys

import pytest

guards = sys.modules["shell_guards"]
gate = guards.git_stash_gate


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setattr(guards._policy, "configured_worker", lambda: "worker")


def check(command):
    return gate.check("terminal", {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "git stash",
        "git stash -q",
        "git stash push -m wip",
        "git stash -u",
        "git stash pop",
        "git stash pop -q",
        "git stash drop",
        "git stash apply",
        "git stash clear",
        "git -C /repo stash pop",
        "cd /repo/code && git stash && pnpm exec tsc; git stash pop",
        "git stash -q; oxlint src 2>&1 | tail -2; git stash pop -q; echo restored",
    ],
)
def test_stash_writes_are_blocked(command):
    message = check(command)
    assert message and "git worktree add" in message and "git show HEAD:<file>" in message


@pytest.mark.parametrize(
    "command",
    [
        "git stash list",
        "git stash list | head -3",
        "git stash show -p stash@{0}",
        "git status",
        "git stashed",
        'echo "git stash pop"',
        "grep -rn 'git stash' docs",
        "cat <<'EOF'\ngit stash pop\nEOF",
        "git log --oneline -3",
    ],
)
def test_reads_and_mentions_pass(command):
    assert check(command) is None


def test_only_workers_and_only_terminal(monkeypatch):
    monkeypatch.setattr(guards._policy, "configured_worker", lambda: None)
    assert check("git stash pop") is None
    monkeypatch.setattr(guards._policy, "configured_worker", lambda: "worker")
    assert gate.check("read_file", {"command": "git stash pop"}) is None
    assert gate.check("terminal", {}) is None


def test_hook_blocks_with_attribution():
    result = guards._pre_tool_call(tool_name="terminal", args={"command": "git stash pop"})
    assert result["action"] == "block" and "[git_stash_gate]" in result["message"]
