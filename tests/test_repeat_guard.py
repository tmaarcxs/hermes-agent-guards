import sys

import pytest

guards = sys.modules["agent_guards"]
repeat = guards.repeat_guard

ARGS = {"path": "/missing.txt"}
NOT_FOUND = '{"error": "File not found: /missing.txt"}'


@pytest.fixture(autouse=True)
def clean():
    repeat.reset()
    yield
    repeat.reset()


def fail(tool="read_file", args=ARGS, session="s1", error="File not found: /missing.txt"):
    guards._post_tool_call(tool_name=tool, args=args, result=NOT_FOUND, status="error",
                           error_message=error, session_id=session)


def ok(tool="read_file", args=ARGS, session="s1"):
    guards._post_tool_call(tool_name=tool, args=args, result='{"content": "x"}', status="ok", session_id=session)


def pre(tool="read_file", args=ARGS, session="s1"):
    return guards._pre_tool_call(tool_name=tool, args=args, session_id=session)


def is_blocked(result):
    return result is not None and result["action"] == "block"


def test_one_failure_allows_a_retry():
    fail()
    assert pre() is None


def test_second_identical_failure_blocks_the_third_call():
    fail()
    fail()
    result = pre()
    assert is_blocked(result)
    assert "already failed 2 times" in result["message"]
    assert "File not found" in result["message"]


def test_different_arguments_are_not_blocked():
    fail()
    fail()
    assert pre(args={"path": "/other.txt"}) is None


def test_argument_order_does_not_matter():
    fail(args={"a": 1, "b": 2})
    fail(args={"b": 2, "a": 1})
    assert is_blocked(pre(args={"a": 1, "b": 2}))


def test_other_sessions_are_independent():
    fail()
    fail()
    assert pre(session="s2") is None


def test_success_of_the_same_call_rearms():
    fail()
    fail()
    ok()
    assert pre() is None


@pytest.mark.parametrize("tool", ["write_file", "patch", "terminal", "kanban_unblock", "skill_manage"])
def test_successful_state_change_rearms_everything(tool):
    fail()
    fail()
    ok(tool=tool, args={"x": 1})
    assert pre() is None


def test_successful_read_only_call_does_not_rearm():
    fail()
    fail()
    ok(tool="search_files", args={"pattern": "x"})
    assert is_blocked(pre())


@pytest.mark.parametrize(
    "error",
    ["Request timed out.", "[Errno 3] No such process", "Error code: 503", "rate limit exceeded", "HTTP 429"],
)
def test_transient_failures_never_count(error):
    fail(error=error)
    fail(error=error)
    fail(error=error)
    assert pre() is None


@pytest.mark.parametrize("tool", ["terminal", "execute_code", "process_manage"])
def test_untracked_tools_are_never_blocked(tool):
    args = {"command": "false"}
    fail(tool=tool, args=args)
    fail(tool=tool, args=args)
    assert pre(tool=tool, args=args) is None


def test_terminal_rules_still_apply_after_repeat_check():
    assert is_blocked(guards._pre_tool_call(tool_name="terminal", args={"command": "sleep 90"}, session_id="s1"))


def test_normalization_still_applies():
    result = guards._pre_tool_call(tool_name="skill_view", args={"name": "x:x"}, session_id="s1")
    assert result == {"action": "modify", "args": {"name": "x"}}


def test_calls_without_session_share_one_bucket():
    fail(session="")
    fail(session="")
    assert is_blocked(pre(session=""))
    assert pre(session="s1") is None


def test_session_memory_is_bounded():
    for index in range(repeat._MAX_SESSIONS + 5):
        fail(session=f"s{index}")
    assert len(repeat._failures) == repeat._MAX_SESSIONS


def test_hooks_fail_open():
    assert guards._pre_tool_call(tool_name="read_file", args=object(), session_id="s1") is None
    guards._post_tool_call(tool_name="read_file", args=ARGS, status="error", session_id="s1", error_message=object())
