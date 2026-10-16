import sys

import pytest

guards = sys.modules["agent_guards"]
gate = guards.backup_copy_gate


@pytest.fixture(autouse=True)
def worker_process(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")


def check(summary, metadata=None, result=None, tool="kanban_complete"):
    args = {"summary": summary}
    if metadata is not None:
        args["metadata"] = metadata
    if result is not None:
        args["result"] = result
    return gate.check(tool, args)



@pytest.mark.parametrize("summary", [
    "Delivery complete. Pre-task rollback copy at /tmp/pre-work preserved for the lead.",
    "Implementation done; backup kept at ~/.hermes/state/nightly-old in case of regression.",
    "All gates green; defective versions backed up in worker scratch before the rewrite.",
])
def test_original_phrasings_are_blocked(summary):
    message = check(summary)
    assert message and "backup/rollback" in message and "Rollback is git" in message


@pytest.mark.parametrize("summary", [
    "Review ready; before-worker-manifest.py preserved for safety.",
    "Wiring checked; pristine tree retained at /tmp/x.",
    "Done; rollback source left in place under ~/.hermes/state/old.",
    "Accepted; old version stored under state/nightly-x.",
    "Complete. Snapshot saved to /tmp/snap for recovery.",
])
def test_other_keep_claims_are_blocked(summary):
    assert check(summary) and "Rollback is git" in check(summary)


def test_result_field_is_checked():
    assert check("Done", result="rollback copy preserved under ~/.hermes/state/old") is not None


def test_nested_metadata_is_checked():
    message = check("Done", metadata={"acceptance": {"1": "tests pass"}, "notes": "pristine tree retained at /tmp/x"})
    assert message and "Rollback is git" in message



@pytest.mark.parametrize("summary", [
    "purged 77 entries (backup kept)",
    "all 77 fake-card entries purged (backup kept)",
    "backup kept",
    "kept a backup",
    "backup retained",
    "Cleanup done (test files removed, backup kept)",
])
def test_paren_scoped_and_bare_keep_claims_are_blocked(summary):
    message = check(summary)
    assert message and "Rollback is git" in message



def test_normal_handoff_passes():
    assert check("Implemented X; 12 tests pass. Next: lead merges PR #9.") is None


@pytest.mark.parametrize("summary", [
    "No backup copies were kept; prior state remains recoverable via git history.",
    "Fixed the regression; rolled back only the failed patch via git checkout.",
    "Rollback is git: no rollback copy remains.",
    "Deleted the rollback copy; nothing retained.",
    "Defective versions backed up, then deleted during cleanup.",
    "Verified: no backup or candidate copies remain on disk.",
    "Rollback for this change is git history, commit abc123.",
])
def test_negated_and_git_rollback_wording_passes(summary):
    assert check(summary) is None


def test_mentioning_a_backup_without_a_keep_claim_passes():
    assert check("Restored from the backup made earlier by the lead; no copy retained.") is None



def test_other_tools_pass_through():
    assert gate.check("kanban_block", {"reason": "backup kept"}) is None


def test_inactive_without_default_assignee(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert check("backup kept at /tmp/x") is None


def test_request_review_is_also_gated():
    message = check("Review ready; before-worker-manifest.py preserved for safety.", tool="kanban_request_review")
    assert message and "Rollback is git" in message


def test_normal_request_review_passes():
    assert check("Ready for review: diff on origin/main, CI green.", tool="kanban_request_review") is None
