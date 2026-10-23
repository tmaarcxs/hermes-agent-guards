"""review_gate must accept the receipt forms workers actually write.

Regression from the guards audit: session ``20261018_181146_417a71`` msg 3644 had all eight
checks closed, but ``RECEIPT`` rejected them — the count/reference forms below ("full suite
608+1") carry real evidence with no ``\\d+ passed`` token. ``AUDIT_ITEMS`` below is that list,
verbatim.

Card item 1 is scoped to the count/reference forms only: ``N+M`` counts, ``PR #N``,
``commit <sha>``/``<sha>``, ``RED->GREEN``, ``rc=0``/``exit 0``. The audit's other named
false negative, the prose item "verified read-only paths", is deliberately NOT accepted: it
names no path, count or hash, so treating it as evidence would let any worker pass by typing
the phrase. ``test_verified_read_only_paths_is_not_a_receipt`` pins that decision.
"""

import json
import sys

import pytest

guards = sys.modules["kanban_guards"]
gate = guards.review_gate

AUDIT_ITEMS = [
    "Contract coverage: card t_5a6f7592 acceptance 1-4 all met (TDD tests RED->GREEN, full suite 608+1, replay attached, PR #22 states gateway variant deliberately not done).",
    "Failure lifecycle: transient failures all retried and resolved (sqlite 'unable to open' retries, replay AttributeErrors fixed, comment-only-line lint fixed); no unresolved failure carried.",
    "Model evidence: model = vendor/model/flash for this run (session header; receipt in session log).",
    "Claims within evidence: all numbers cited from tool output (pytest tails, replay output file replay_d1_output.txt, git log, gh api).",
    "Real boundary: deliverable is the pushed PR #22 on a real branch; live install, board DB, state file untouched (verified read-only paths + tempfile cleanup).",
    "Stale siblings: no other open cards touch notification_reconcile.py on the board; SYN proposed-changes list has no duplicate of this change in flight.",
    "Self reproduction: replay script + attached output reproduce the live-board claim; pytest command in README recipe reproduces the suite receipt.",
    "Claims anchored: PR text claims map to artifacts (replay script+output in-repo, test names, commit 6ba7695).",
]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    gate.reset()
    yield
    gate.reset()


def todo(items, session="audit"):
    guards._post_tool_call(tool_name="todo_list", args={"todos": items}, result=json.dumps({"todos": items}),
                           status="ok", session_id=session)


def review(session="audit"):
    return guards._pre_tool_call(tool_name="kanban_request_review", args={"summary": "s"}, session_id=session)


@pytest.mark.parametrize("receipt", [
    "full suite 608+1",   # N+M test count
    "PR #22",             # pull-request reference
    "commit 6ba7695",     # commit sha with prefix
    "6ba7695",            # bare sha
    "RED->GREEN",         # red-to-green proof
    "rc=0",               # rc value
    "exit 0",             # exit code
])
def test_audit_msg_3644_fixed_forms_pass_receipt(receipt):
    """The fixed forms, each a single token so no other token can hide an unmatched one."""
    assert gate.RECEIPT.search(receipt)


def test_audit_msg_3644_review_passes_once_every_item_carries_a_receipt():
    """Item 2's evidence is prose with no receipt token, so it still blocks — the gate keeps
    requiring a real receipt; adding one to that item lets the request through."""
    items = list(AUDIT_ITEMS)
    items[1] = items[1] + " (rc=0, exit 0)"
    todo([{"id": str(i), "content": text, "status": "completed"} for i, text in enumerate(items)])
    assert review() is None


def test_prose_only_item_still_blocks_for_lack_of_receipt():
    """Preservation test — green on base and on the candidate. Two identical item lists, both
    with a real receipt on every check, differ in exactly one thing: item 1's receipt is
    removed. The first passes; the second blocks naming that check. So the block is caused by
    the missing receipt alone, not by a missing check name or any other check."""
    with_receipts = [
        text + (" (rc=0)" if not gate.RECEIPT.search(text) else "") for text in AUDIT_ITEMS
    ]
    items = [{"id": str(i), "content": text, "status": "completed"} for i, text in enumerate(with_receipts)]
    todo(items)
    assert review() is None

    stripped = list(with_receipts)
    stripped[1] = "Failure lifecycle: transient failures all retried and resolved"
    assert not gate.RECEIPT.search(stripped[1])
    todo([{"id": str(i), "content": text, "status": "completed"} for i, text in enumerate(stripped)])
    result = review()
    assert result is not None and "Failure lifecycle" in result["message"]


@pytest.mark.parametrize("receipt", ["rc=0", "rc 0", "rc:1", "exit 0", "exit=0", "exit: 0"])
def test_exit_and_rc_need_a_value(receipt):
    """The word must carry its measurement: bare 'exit'/'rc' is prose, not a receipt."""
    assert gate.RECEIPT.search(receipt)


@pytest.mark.parametrize("text", ["no early exit, all good", "exits cleanly", "rc", "exit"])
def test_bare_exit_or_rc_is_not_a_receipt(text):
    """Each is a single token so nothing else in the string can match."""
    assert not gate.RECEIPT.search(text)


def test_prose_exit_blocks_and_rc_value_passes():
    """Differential: two all-green lists differ only in item 4's text — the bare-word item
    blocks naming it, the rc=0 item passes. So the block comes from the receipt alone."""
    all_green = [text + (" (rc=0)" if not gate.RECEIPT.search(text) else "") for text in AUDIT_ITEMS]

    prose = list(all_green)
    prose[4] = "Real boundary: no early exit, all good"
    todo([{"id": str(i), "content": text, "status": "completed"} for i, text in enumerate(prose)])
    result = review()
    assert result is not None and "Real boundary" in result["message"]

    valued = list(all_green)
    valued[4] = "Real boundary: rc=0"
    todo([{"id": str(i), "content": text, "status": "completed"} for i, text in enumerate(valued)])
    assert review() is None


def test_verified_read_only_paths_is_not_a_receipt():
    """Deliberate decision (card item 1, lead review R1): "verified read-only paths" is prose
    about the work, not a measurement of it — it names no path, count or hash. An item reading
    exactly that, with every other check genuinely closed, still BLOCKS; accepting the phrase
    would make the gate a password any worker can type and pass silently."""
    items = list(AUDIT_ITEMS)
    items[4] = "Real boundary: verified read-only paths"
    items[1] = items[1] + " (rc=0, exit 0)"
    todo([{"id": str(i), "content": text, "status": "completed"} for i, text in enumerate(items)])
    result = review()
    assert result is not None and "Real boundary" in result["message"]


@pytest.mark.parametrize("text", ["Contract coverage", "Claims anchored", "done", "verified"])
def test_bare_items_still_have_no_receipt(text):
    """The intent is unchanged: a real receipt is still required."""
    assert not gate.RECEIPT.search(text)
