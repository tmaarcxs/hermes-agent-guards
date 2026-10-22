"""The review_gate block message is one message that names every open check and the closure format.

The per-request message already lists the open checks; what it must not do is escalate
per item or per attempt: the same open-check set produces the same single message on
every request, so a compliant worker closes everything it names on the second attempt.
The closure format is spelled out exactly once in the message, not once per check.
"""

import sys

guards = sys.modules["kanban_guards"]

EIGHT = (
    "Contract coverage", "Failure lifecycle", "Model evidence", "Claims within evidence", "Real boundary", "Stale siblings",
    "Self reproduction", "Claims anchored",
)
FORMAT_MARKS = ("delivery-verification", "N/A: <reason>")


def block_message(open_names):
    import json

    gate = guards.review_gate
    items = [
        {"id": str(i), "content": name if name in open_names else name + ": receipt", "status": "pending" if name in open_names else "completed"}
        for i, name in enumerate(EIGHT)
    ]
    guards._post_tool_call(tool_name="todo_list", args={"todos": items}, result=json.dumps({"todos": items}),
                           status="ok", session_id="msg")
    result = guards._pre_tool_call(tool_name="kanban_request_review", args={"summary": "s"}, session_id="msg")
    gate.reset()
    return result["message"]


def test_same_open_set_yields_one_unchanged_message():
    first = block_message(EIGHT[:3])
    second = block_message(EIGHT[:3])
    assert first == second


def test_message_names_all_open_checks():
    message = block_message(EIGHT[:2] + (EIGHT[5],))
    listed = message.split("closed in todo_list: ", 1)[1].split(". Add one", 1)[0]
    for name in (EIGHT[0], EIGHT[1], EIGHT[5]):
        assert name in listed
    for name in (EIGHT[2], EIGHT[3], EIGHT[4], EIGHT[6], EIGHT[7]):
        assert name not in listed


def test_message_states_the_closure_format_once():
    message = block_message(EIGHT[:4])
    assert all(mark in message for mark in FORMAT_MARKS)
    assert message.count("N/A: <reason>") == 1


def test_message_is_invariant_across_open_set_sizes():
    messages = [block_message(EIGHT[:size]) for size in (1, 4, 8)]
    opens = {}
    for size, message in zip((1, 4, 8), messages):
        prefix = "todo_list: "
        opens[size] = message.split(prefix, 1)[1].split(". Add one", 1)[0]
    for size, message in zip((1, 4, 8), messages):
        without_checks = message.replace(opens[size], "<checks>")
        assert without_checks == messages[0].replace(opens[1], "<checks>"), size
