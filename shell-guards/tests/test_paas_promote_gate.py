import sys

import pytest

guards = sys.modules["shell_guards"]
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
    "gh workflow run paas-promote -F labels.json",
    "gh workflow run paas-promote --field-file labels.json -f SUPRAENVS=des",
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
    assert incident["action"] == "block" and check(INCIDENT).split(": ", 1)[1] in incident["message"]
    assert "[paas_promote_gate]" in incident["message"]
    assert guards._pre_tool_call("terminal", {"command": CORRECTED}) is None


"""Round-2: the JSON `dispatches` endpoint shape and LABELS whitespace.

The audit's §6 probe dispatched with raw
`curl -XPOST .../workflows/dispatches -d '{"ref":..., "inputs":{"DEPLOY_TARGET":"des"}}'`
and passed; a `-f LABELS='auto-approval '` (trailing space) validated as
labelled. Both are the same deployment risk as the gh form.
"""

_CURL_INCIDENT = (
    "curl -XPOST https://api.github.com/repos/x/y/actions/workflows/paas-promote/dispatches "
    "-d '{\"ref\":\"main\",\"inputs\":{\"DEPLOY_TARGET\":\"des\"}}'"
)
_CURL_LABELLED = _CURL_INCIDENT.replace(
    '{"DEPLOY_TARGET":"des"}',
    '{"DEPLOY_TARGET":"des","LABELS":"auto-approval"}')


@pytest.mark.parametrize("command", [
    _CURL_INCIDENT,
    _CURL_INCIDENT.replace("-d ", "--data "),
    _CURL_INCIDENT.replace("{'ref':'main'", '{"ref":"develop"'),
    "curl -X POST https://api.github.com/repos/x/y/actions/workflows/paas-promote.yml/dispatches "
    "-d '{\"ref\":\"main\",\"inputs\":{\"SUPRAENVS\":\"des\"}}'",
])
def test_curl_dispatch_without_label_blocked(command):
    assert blocked(command), command


def test_curl_dispatch_block_names_the_json_label_field():
    message = check(_CURL_INCIDENT)
    assert message and "auto-approval" in message
    assert '"LABELS":"auto-approval"' in message, message


@pytest.mark.parametrize("command", [
    _CURL_LABELLED,
    _CURL_INCIDENT.replace("paas-promote", "other-workflow"),
    "curl -s https://api.github.com/repos/x/y/actions/workflows/paas-promote",
    "curl -XPOST https://api.github.com/repos/x/y/actions/workflows/deploy/dispatches "
    "-d '{\"ref\":\"main\"}'",
    'curl -d \'{"ref":"main","inputs":{"DEPLOY_TARGET":"$TARGET"}}\' '
    "https://api.github.com/repos/x/y/actions/workflows/paas-promote/dispatches",
])
def test_curl_dispatch_allowed_shapes(command):
    assert not blocked(command), command


def test_labels_surrounding_whitespace_still_counts_as_the_label():
    assert not blocked("gh workflow run paas-promote -f DEPLOY_TARGET=des -f LABELS='auto-approval '")
    assert not blocked("gh workflow run paas-promote -f DEPLOY_TARGET=des -f LABELS=' auto-approval , x '")
    assert blocked("gh workflow run paas-promote -f DEPLOY_TARGET=des -f LABELS='auto approval '")
