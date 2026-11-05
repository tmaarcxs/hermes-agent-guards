import os
import sys

import pytest

guards = sys.modules["shell_guards"]
gate = guards.hermes_home_search_gate
HOME = os.path.expanduser("~")


def check(command):
    return gate.check("terminal", {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        'find ~/.hermes -name "*sonar*" 2>/dev/null | head',
        f"find {HOME}/.hermes -name '*probe*' -o -name '*col*'",
        "find $HOME/.hermes/ -type f -newermt 2026-11-01",
        'find "${HOME}/.hermes/installs" -name node_modules',
        "grep -rn eslint ~/.hermes | head -5",
        "grep -rl axinpasweb ~/.hermes/benchmarks",
        "cd /repo && grep -rn 'x' ~/.hermes/memories/ ~/.hermes; ls",
        "rg sonar ~/.hermes",
        "find ~/.hermes -maxdepth 3 -name x",
        "find / -name '*.log' 2>/dev/null | head",
        "find ~ -name '*sonar*'",
        "find $HOME/ -type f -newer x",
        "grep -rn eslint ~ | head -3",
        "grep -rl foo /",
    ],
)
def test_bulk_searches_are_blocked(command):
    assert check(command) and "search_files" in check(command)


@pytest.mark.parametrize(
    "command",
    [
        "find ~/.hermes/skills -name SKILL.md",
        "grep -rn eslint ~/.hermes/memories/",
        "grep -rn x ~/.hermes/profiles/worker/SOUL.md",
        "rg sonar ~/.hermes/hermes-agent/tools",
        "find ~/.hermes -maxdepth 1 -name '*.yaml'",
        "find ~/.hermes -maxdepth 2 -type d",
        "grep foo ~/.hermes/config.yaml",
        "ls ~/.hermes",
        "find ~/git_repos -name package.json",
        "find / -maxdepth 2 -name x",
        "find ~ -maxdepth 1 -name '*.md'",
        "ls /",
        "grep foo ~/notes.txt",
        'echo "find ~/.hermes -name x"',
        "cat <<'EOF'\nfind ~/.hermes -name x\nEOF",
        "git log --oneline -3",
    ],
)
def test_scoped_searches_and_mentions_pass(command):
    assert check(command) is None


def test_only_terminal():
    assert gate.check("read_file", {"command": "find ~/.hermes"}) is None
    assert gate.check("terminal", {}) is None


def test_hook_blocks_with_attribution():
    result = guards._pre_tool_call(tool_name="terminal", args={"command": "find ~/.hermes -name x"})
    assert result["action"] == "block" and "[hermes_home_search_gate]" in result["message"]
