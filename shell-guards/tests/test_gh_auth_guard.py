"""The ``gh auth`` shared-account block: account-mutating verbs never pass.

``gh auth switch`` flips the active gh account for EVERY Hermes process (Hermes
reads its Copilot token from ``gh auth token``, source ``gh_cli``), and the
default account ``msanchez19`` is the one every Inditex repository access needs.
The rule is to pin the account per command instead:
``GH_TOKEN=$(gh auth token -u <user>) <command>``.

The guard matches the account-mutating verbs (``switch``, ``login``, ``logout``,
``refresh``) at command-word position and inside a quoted ``bash -c``/``sh -c``
argument, while ``gh auth status``/``gh auth token``, quoted prose and
``python -c``/heredoc bodies stay allowed — the same matcher style as the other
terminal blocks. It fails open on an internal error.
"""

import sys

import pytest

guards = sys.modules["shell_guards"]
terminal_policy = guards.terminal_policy


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
        "gh auth switch",
        "gh auth switch --user tmaarcxs",
        "gh auth switch -u tmaarcxs",
        "gh auth switch --hostname github.com --user tmaarcxs",
        "env FOO=1 gh auth switch --user tmaarcxs",
        "env -i gh auth switch",
        "X=1 gh auth switch",
        "sudo gh auth switch --user tmaarcxs",
        "command gh auth switch",
        "cd /tmp && gh auth switch --user tmaarcxs",
        "true && gh auth switch",
        "echo hi; gh auth switch",
        "echo hi | gh auth switch",
        "$(gh auth switch --user tmaarcxs)",
        "bash -c 'gh auth switch --user tmaarcxs'",
        'bash -c "gh auth switch"',
        "sh -c 'gh auth switch'",
        "bash -c 'cd /tmp && gh auth switch -u x'",
        "true && bash -c \"gh auth refresh\"",
        "gh auth login",
        "gh auth login --hostname github.com",
        "gh auth logout",
        "gh auth refresh",
        "gh auth refresh -h github.com",
    ],
)
def test_account_mutating_gh_auth_is_blocked(command):
    assert blocked(command)


def test_blocked_even_for_a_background_call():
    assert blocked("gh auth switch --user tmaarcxs", background=True)


@pytest.mark.parametrize(
    "command",
    [
        "gh auth status",
        "gh auth status --hostname github.com",
        "gh auth token",
        "gh auth token -u x",
        "GH_TOKEN=$(gh auth token -u x) gh pr view",
        "GH_TOKEN=$(gh auth token -u tmaarcxs) git ls-remote https://github.com/tmaarcxs/hermes-agent-guards.git",
        "echo \"gh auth switch\"",
        "echo 'gh auth switch --user tmaarcxs'",
        "echo gh auth login",
        "grep -n \"gh auth switch\" notes.md",
        "grep -rn 'gh auth refresh' ~/.hermes/plugins",
        "printf %s \"gh auth logout\"",
        "bash -c \"echo gh auth switch\"",
        "echo \"run bash -c 'gh auth switch' to flip\"",
        "python3 -c \"print('gh auth switch')\"",
        "cat > note.md <<EOF\nnever run gh auth switch\nEOF",
        "python3 - <<PYEOF\nprint(\"gh auth refresh\")\nPYEOF",
    ],
)
def test_allowed_spellings_pass(command):
    assert run(command) is None


def test_message_names_the_pinned_account_form_and_the_default():
    message = run("gh auth switch --user tmaarcxs")["message"]
    assert "GH_TOKEN=$(gh auth token -u <user>) <command>" in message
    assert "msanchez19" in message
    assert "shared active account" in message
    assert "\n" not in message


def test_message_leads_with_the_imperative():
    message = run("gh auth switch")["message"]
    lead = message.split("agent-guards blocked terminal [__init__]: ", 1)[1]
    assert lead.startswith("gh auth switch (also login/logout/refresh) is not allowed")


def test_guard_fails_open_on_an_internal_error(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("probe crash")

    monkeypatch.setattr(terminal_policy, "_gh_auth_blocked", boom)
    assert run("gh auth switch --user tmaarcxs") is None
