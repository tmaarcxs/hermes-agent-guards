"""A foreground wait the guard moves to the background tells the model it has not finished.

``pre_tool_call`` rewrites a foreground ``terminal`` wait (sleep, polling loop, ``read -t``,
``select``) to ``background=true, notify=true``. A rewrite carries no message, so the model
read the bare ``Background process started`` as a command that had run, re-ran the wait and
stacked silent jobs. ``record`` remembers the rewrite by ``tool_call_id`` and ``transform``
adds one ``rewritten_to_background`` field to that call's started-process result.
"""

import json
import sys

import pytest

guards = sys.modules["shell_guards"]
note = guards.background_wait_note

STARTED = {
    "output": "Background process started",
    "session_id": "proc_1",
    "pid": 41,
    "exit_code": 0,
    "error": None,
    "notify_on_complete": True,
}
REWRITE = {"action": "modify", "args": {"background": True, "notify": True}}


@pytest.fixture(autouse=True)
def lead_session(monkeypatch):
    monkeypatch.setattr(guards._policy, "configured_worker", lambda: "")
    note.reset()
    yield
    note.reset()


def pre(command, call_id="c1", **args):
    return guards._pre_tool_call(
        tool_name="terminal", args={"command": command, **args}, tool_call_id=call_id, session_id="s1"
    )


def post(call_id="c1", result=STARTED, tool_name="terminal"):
    raw = result if isinstance(result, str) else json.dumps(result)
    return guards._transform_tool_result(
        tool_name=tool_name, args={}, result=raw, tool_call_id=call_id, session_id="s1"
    )


def test_rewritten_foreground_wait_gets_a_note():
    assert pre("sleep 200; gh run view 1") == REWRITE
    data = json.loads(post())
    text = data.pop("rewritten_to_background")
    assert data == STARTED
    assert "NOT finished" in text and "Do NOT re-run it" in text
    assert 'process_manage(action="wait", session_id="proc_1")' in text
    assert "terminal(command=<the same command>, background=true, notify=true)" in text
    assert "completion message" in text


@pytest.mark.parametrize(
    "command",
    [
        "sleep 200; echo done",
        "cd repo && sleep 2m && gh pr checks 1",
        "for i in 1 2 3; do gh run view 1; sleep 20; done",
        "read -t 30 x",
        'python3 -c "import time; time.sleep(30)"',
    ],
)
def test_every_wait_shape_the_guard_rewrites_gets_the_note(command):
    assert pre(command)["args"]["background"] is True
    assert "rewritten_to_background" in json.loads(post())


def test_model_declared_background_gets_no_note():
    assert pre("sleep 200; echo done", background=True, notify=True) is None
    assert post() is None


def test_worker_notify_insert_gets_no_note(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setattr(guards._policy, "configured_worker", lambda: "worker")
    assert pre("make build", background=True) == {"action": "modify", "args": {"notify": True}}
    assert post() is None


def test_note_is_delivered_once():
    pre("sleep 200")
    assert post() is not None
    assert post() is None


def test_failed_spawn_gets_no_note_and_clears_the_record():
    pre("sleep 200")
    failed = {"output": "", "exit_code": -1, "error": "Failed to start background process: boom"}
    assert post(result=failed) is None
    assert post() is None


def test_foreground_result_after_a_denied_rewrite_gets_no_note():
    pre("sleep 200")
    assert post(result={"output": "done", "exit_code": 0, "error": None}) is None


def test_result_with_a_session_id_but_other_output_gets_no_note():
    pre("sleep 200")
    assert post(result={"output": "done", "session_id": "proc_1", "exit_code": 0}) is None


def test_other_calls_and_tools_pass_through():
    pre("sleep 200", call_id="c9")
    assert post(call_id="c1") is None
    assert post(call_id="c9", tool_name="read_file") is None
    assert "rewritten_to_background" in json.loads(post(call_id="c9"))


def test_no_tool_call_id_means_no_note():
    assert guards._pre_tool_call(tool_name="terminal", args={"command": "sleep 200"}) == REWRITE
    assert not note._PENDING
    assert post(call_id="") is None


def test_blocked_waits_are_not_recorded():
    assert pre("sleep 200", pty=True)["action"] == "block"
    assert pre("sleep 200; git push -f origin main", call_id="c2")["action"] == "block"
    assert not note._PENDING


def test_subagent_result_does_not_promise_a_completion_message():
    pre("sleep 200")
    quiet = {**STARTED, "notify_on_complete": False, "subagent_note": "You are a subagent."}
    text = json.loads(post(result=quiet))["rewritten_to_background"]
    assert "completion message" not in text.replace("No completion message will reach you", "")
    assert 'process_manage(action="wait", session_id="proc_1")' in text and "before you finish" in text


def test_non_json_and_non_string_results_pass_through():
    pre("sleep 200")
    assert post(result="plain text") is None
    pre("sleep 200", call_id="c2")
    assert guards._transform_tool_result(
        tool_name="terminal", args={}, result={"ok": 1}, tool_call_id="c2"
    ) is None


def test_same_tool_call_id_in_another_session_gets_no_note():
    pre("sleep 200", call_id="call_0")
    other = guards._transform_tool_result(
        tool_name="terminal", args={}, result=json.dumps(STARTED), tool_call_id="call_0", session_id="s2"
    )
    assert other is None
    assert "rewritten_to_background" in json.loads(post(call_id="call_0"))


def test_pending_records_are_bounded():
    for number in range(note._MAX_PENDING * 3):
        note.record(f"c{number}", "s1")
    assert len(note._PENDING) == note._MAX_PENDING


def test_a_failing_note_never_loses_the_rewrite_or_the_result(monkeypatch):
    monkeypatch.setattr(note, "_MAX_PENDING", None)
    assert pre("sleep 200") == REWRITE
    monkeypatch.setattr(note, "NOTE", "{unknown}")
    assert post() is None


def test_note_and_skill_size_flag_both_reach_the_model(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    (skills / "cat" / "demo").mkdir(parents=True)
    skill = skills / "cat" / "demo" / "SKILL.md"
    skill.write_text("---\nname: demo\ndescription: Use when testing.\n---\n" + "x" * 100)
    monkeypatch.setattr(guards.skill_size, "ROOTS", [skills])
    guards.skill_size._PENDING.clear()
    pre("sleep 200")
    skill.write_text(skill.read_text() + "y" * 7000)
    final = post()
    assert "rewritten_to_background" in final and "over the limit for SKILL.md" in final
    guards.skill_size._PENDING.clear()
