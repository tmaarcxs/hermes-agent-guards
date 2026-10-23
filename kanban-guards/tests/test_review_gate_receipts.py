"""review_gate must accept the receipt forms workers actually write.

Regression from the guards audit: session ``20261018_181146_417a71`` msg 3644 had all eight
checks closed, but ``RECEIPT`` rejected three of them — "full suite 608+1" and "verified
read-only paths" carry real evidence with no ``\\d+ passed`` token. ``AUDIT_ITEMS`` below is
that list, verbatim.
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


def test_audit_msg_3644_named_false_negatives_pass_receipt():
    """The two items the card names as false negatives: "full suite 608+1" and "verified
    read-only paths"."""
    assert gate.RECEIPT.search(AUDIT_ITEMS[0])
    assert gate.RECEIPT.search(AUDIT_ITEMS[4])


def test_audit_msg_3644_review_passes_once_every_item_carries_a_receipt():
    """Item 2's evidence is prose with no receipt token, so it still blocks — the gate keeps
    requiring a real receipt; adding one to that item lets the request through."""
    items = list(AUDIT_ITEMS)
    items[1] = items[1] + " (rc=0, exit 0)"
    todo([{"id": str(i), "content": text, "status": "completed"} for i, text in enumerate(items)])
    assert review() is None


def test_audit_msg_3644_item_without_receipt_still_blocks():
    todo([{"id": str(i), "content": text, "status": "completed"} for i, text in enumerate(AUDIT_ITEMS)])
    result = review()
    assert result is not None and "Failure lifecycle" in result["message"]


@pytest.mark.parametrize("receipt", [
    "full suite 608+1",
    "PR #22 open",
    "commit 6ba7695",
    "RED->GREEN",
    "rc=0",
    "exit 0",
])
def test_widened_receipt_forms(receipt):
    assert gate.RECEIPT.search(receipt)


@pytest.mark.parametrize("text", ["Contract coverage", "Claims anchored", "done", "verified"])
def test_bare_items_still_have_no_receipt(text):
    """The intent is unchanged: a real receipt is still required."""
    assert not gate.RECEIPT.search(text)
