"""backup_copy_gate must not match the family adjective alone.

Regression from the guards audit: session ``20261018_191002_c3465e`` msgs 13899-14498 (six
fires, ~24 calls) blocked a handoff whose summary said "unchanged vs **pristine**" — a
comparison, not a retained copy. The gate matched the bare family word. The samples below are
the summary/metadata strings the gate actually saw.
"""

import sys

import pytest

guards = sys.modules["kanban_guards"]
gate = guards.backup_copy_gate


@pytest.fixture(autouse=True)
def worker_process(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")


def check(summary, metadata=None):
    args = {"summary": summary}
    if metadata is not None:
        args["metadata"] = metadata
    return gate.check("kanban_request_review", args)


AUDIT_SUMMARY = (
    "R1 rework complete on PR #30: both blockers fixed by scoping the suppression layers; "
    "gate file 47/47, full suite 609 passed/1 skipped/14 pre-existing env errors "
    "(unchanged vs pristine), corpus replay 2804->2468 with zero gained blocks."
)
AUDIT_METADATA = {
    "full_suite": {
        "candidate_head": "609 passed, 1 skipped, 14 errors (14 are the pre-existing "
                          "test_workspace_scope_gate sqlite setup errors, unchanged vs pristine)",
    },
    "tests_fields": "same environment (HERMES_HOME temp home) used for both candidate and pristine/day counts",
}


def test_audited_handoff_passes():
    assert check(AUDIT_SUMMARY, AUDIT_METADATA) is None


@pytest.mark.parametrize("summary", [
    "unchanged vs pristine",
    "the result is unchanged vs pristine",
    "behaviour unchanged from the pristine baseline",
    "kanban.db, board backups, archive-backup 20261013",
    "0 backup files left.",
    "No .bak/~/.orig backups left.",
])
def test_comparison_and_absence_prose_passes(summary):
    assert check(summary) is None


@pytest.mark.parametrize("summary", [
    "Delivery complete. Pre-task rollback copy at /tmp/pre-work preserved for the lead.",
    "Implementation done; backup kept at ~/.hermes/state/nightly-old in case of regression.",
    "Review ready; pristine tree retained at /tmp/x.",
    "Snapshot kept at overview.py.bak for comparison.",
    "backup kept",
    "kept a backup",
    "Backup preserved for a comparison pass tonight.",
    "Comparison against the pristine state kept the numbers identical",
    "kept the pristine baseline",
    "the pristine version was kept for comparison",
])
def test_real_keep_claims_still_block(summary):
    message = check(summary)
    assert message and "Rollback is git" in message
