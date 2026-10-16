import sys

import pytest

guards = sys.modules["agent_guards"]
gate = guards.paas_promote_gate

INCIDENT = (
    "gh workflow run paas-promote --repo acme/demo-web --ref main "
    "-f SUPRAENVS=des -f ENV=des -f LABELS=env/tenant/platform/slot/release"
)
CORRECTED = INCIDENT.replace(
    "-f LABELS=env/tenant/platform/slot/release", "-f LABELS=env/tenant/platform/slot/release,auto-approval"
)


def check(command, tool="terminal"):
    return gate.check(tool, {"command": command})


def blocked(command):
    return check(command) is not None


@pytest.mark.parametrize("command", [
    INCIDENT,
    INCIDENT.replace("-f SUPRAENVS=des ", ""),
    "gh workflow run paas-promote.yml -f SUPRAENVS=des -f LABELS=x",
    "gh workflow run paas-promote --repo o/r --ref main --field SUPRAENVS=des --field LABELS=x",
    "gh workflow run paas-promote -f SUPRAENVS='des' -f LABELS='env/x'",
    "gh workflow run paas-promote -f SUPRAENVS=des,pre,pro -f LABELS=env/x",
    "gh workflow run paas-promote -f SUPRAENVS=des,PRE,pro -f LABELS=y",
    "gh workflow run paas-promote -f SUPRAENVS=des -f LABELS=not-auto-approval",
    "SUPRAENVS=des gh workflow run paas-promote -f LABELS=x",
    "clean && gh workflow run paas-promote -f SUPRAENVS=des -f LABELS=x",
    "echo building; gh workflow run paas-promote -f SUPRAENVS=des",
    "gh workflow run paas-promote -f SUPRAENVS=des -f LABELS=env/x -f LABELS=auto-approval-later",
    "gh workflow run paas-promote -f SUPRAENVS=DES -f LABELS=x",
])
def test_incident_shapes_are_blocked(command):
    assert blocked(command)


def test_block_message_names_the_label_and_the_exact_flag():
    message = check(INCIDENT)
    assert message and "auto-approval" in message
    assert "LABELS=env/tenant/platform/slot/release,auto-approval" in message


def test_block_message_gives_a_bare_flag_when_labels_carry_no_value():
    message = check("gh workflow run paas-promote -f SUPRAENVS=des")
    assert message and "-f LABELS=auto-approval" in message


@pytest.mark.parametrize("command", [
    "gh workflow run paas-promote --repo acme/demo-web --ref main -f SUPRAENVS=des -f LABELS=auto-approval",
    "gh workflow run paas-promote -f SUPRAENVS=des -f LABELS=AUTO-APPROVAL",
    "gh workflow run paas-promote -f SUPRAENVS='des' -f LABELS='auto-approval,extra'",
    "gh workflow run paas-promote -f SUPRAENVS=des -f 'LABELS=auto-approval'",
    "gh workflow run paas-promote -f SUPRAENVS=des -f LABELS='env/tenant auto-approval'",
    "gh workflow run paas-promote -f SUPRAENVS=des -f LABELS=env/x -f LABELS=auto-approval",
    "gh workflow run paas-promote -f SUPRAENVS=\"des\" -f LABELS=x -f LABELS=auto-approval",
])
def test_labelled_dispatches_pass(command):
    assert not blocked(command)


@pytest.mark.parametrize("command", [
    "gh workflow run paas-promote -f SUPRAENVS=pre -f LABELS=x",
    "gh workflow run paas-promote -f SUPRAENVS=pro -f LABELS=env/x",
    "gh workflow run paas-promote -f SUPRAENVS=pre,pro -f LABELS=",
    "SUPRAENVS=pre gh workflow run paas-promote -f LABELS=x",
    "gh workflow run paas-promote -f SUPRAENVS=predawn -f LABELS=x",
])
def test_pre_and_pro_only_dispatches_pass(command):
    assert not blocked(command)


@pytest.mark.parametrize("command", [
    "gh run list --workflow paas-promote",
    "gh run watch 12345 --exit-status",
    "gh workflow list --repo o/r",
    "gh run view 987 --log",
    "gh workflow run other-workflow -f SUPRAENVS=des",
    "gh workflow set-default paas-promote",
    "cat paas-promote.yml",
])
def test_other_gh_commands_pass(command):
    assert not blocked(command)


@pytest.mark.parametrize("command", [
    'echo "paas-promote"',
    'echo "dispatch paas-promote with LABELS=x"',
    "grep paas-promote Makefile",
    'printf "gh workflow run paas-promote"',
    "echo -f SUPRAENVS=des",
])
def test_mentions_inside_echo_or_quoted_text_pass(command):
    assert not blocked(command)


@pytest.mark.parametrize("command", [
    "gh workflow run paas-promote -f SUPRAENVS=$ENVS -f LABELS=x",
    'gh workflow run paas-promote -f SUPRAENVS="$ENVS" -f LABELS=x',
    "gh workflow run paas-promote -f SUPRAENVS=`echo des` -f LABELS=x",
    "gh workflow run paas-promote -f SUPRAENVS=des -f LABELS=$(get_labels)",
    'gh workflow run paas-promote -f SUPRAENVS=des -f LABELS="unbalanced',
    "gh workflow run paas-promote -f SUPRAENVS='des -f LABELS=x",
    "env gh workflow run paas-promote -f SUPRAENVS=des -f LABELS=x",
])
def test_unanalyzable_shapes_fail_open(command):
    assert not blocked(command)


def test_non_terminal_tools_pass():
    assert gate.check("kanban_comment", {"body": INCIDENT}) is None
    assert gate.check("terminal", {}) is None
    assert gate.check("terminal", None) is None


def test_any_garbage_fails_open():
    assert gate.check("terminal", {"command": 42}) is None


def test_dispatch_entry_point_blocks_the_incident_and_passes_the_fix():
    incident = guards._pre_tool_call("terminal", {"command": INCIDENT})
    assert incident == {"action": "block", "message": check(INCIDENT)}
    assert guards._pre_tool_call("terminal", {"command": CORRECTED}) is None
