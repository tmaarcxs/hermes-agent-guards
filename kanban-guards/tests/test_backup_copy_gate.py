import sys

import pytest

guards = sys.modules["kanban_guards"]
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
    "Review ready; pristine tree retained at /tmp/x.",
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
    "LANE A parity verified: render consumes the same snapshot, so parity holds by construction",
    "and zero backup/rollback/snapshot artifacts left anywhere after cleanup",
    "No `.bak`/backup/snapshot copies of live config exist; removes excluded.",
    "Deleted 53 backup/rollback/pristine/before-candidate artifacts from the board.",
    "The owner forbids backup/rollback/snapshot artifacts of prior state anywhere; honored.",
    "Memory review done: 'snapshot kept nowhere', stores unchanged.",
    "Verified against the deployed snapshot (no changes made).",
    "Removed the backup directory; nothing retained anywhere.",
])
def test_false_positive_regressions_pass(summary):
    assert check(summary) is None


@pytest.mark.parametrize("summary", [
    "rollback at .task/deployment-backup/installed-before-compact.",
    "with /tmp pre-install backups retained;",
    "keep /tmp pre-install backups;",
    "Snapshot kept at overview.py.bak for comparison.",
])
def test_path_and_location_keep_claims_still_block(summary):
    assert check(summary) and "Rollback is git" in check(summary)


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


@pytest.mark.parametrize("summary", [
    "Backup preserved for a comparison pass tonight.",
    "The auditor reviewed the tree but left the pristine copy in place at /tmp/x.",
    "Verified against the deployed snapshot and kept the original artifacts under /tmp/compare.",
    "Preview checks kept a backup of the index in case a release fails.",
])
def test_keep_claims_near_review_and_parity_vocabulary_block(summary):
    message = check(summary)
    assert message and "Rollback is git" in message


@pytest.mark.parametrize("summary", [
    "Snapshot saved to /tmp/snap; will delete after the sign-off arrives.",
    "Snapshot saved to /tmp/snap; deleted after the sign-off arrived.",
])
def test_later_or_planned_removal_does_not_erase_a_keep_claim(summary):
    assert check(summary) and "Rollback is git" in check(summary)


@pytest.mark.parametrize("summary", [
    "Kept 'a backup at /tmp/x.bak'",
    "'backup kept'",
    '"pre-task snapshot kept at /tmp/preview"',
    "summary mentions 'snapshot kept at /tmp/p' in prose",
])
def test_quoting_a_real_claim_still_blocks(summary):
    message = check(summary)
    assert message and "Rollback is git" in message


def test_quoted_fixture_sentence_keeps_blocking_as_accepted_trade_off():
    message = check("'pre-task snapshot kept at /tmp/preview'")
    assert message and "Rollback is git" in message


def test_quoted_reviewer_fixture_keeps_blocking_as_accepted_trade_off():
    message = check("'Backup kept for the reviewer.' still blocks")
    assert message and "Rollback is git" in message


@pytest.mark.parametrize("summary", [
    "Review ready; before-worker-manifest.py preserved for safety.",
    "Prior-state tree backed up before the handler rewrite.",
    "source DB was in WAL mode; .backup captures committed data as of backup time. For a 14-day frequency-premise test this is immaterial.",
    "'rollback at .task/deployment-backup/...' still blocks",
    "12/12 hold cases still block (incl. 'pre-task snapshot kept at /tmp/preview', 'rollback at .task/deployment-backup/...') and 5/5 pass cases pass (incl. 'purged 77 entries (backup kept) and then deleted the backup directory.')",
    "336 rows unblocked vs base: path/FS listings 96, keep-verb-wording rows 89 (dominated by repeated policy/doc lines like 'New execution/snapshot/report retain their existing IDs'), snapshot-consumption contexts 25, negated policy wording 41, tool-JSON rows 16, other 69; 17 rows match 'rollback baseline' advisory phrasing; all 28 prior recent-window drops remain clean (still_pass=28, now_block_again=0)",
    "Replay re-run this run (after delta, per lead round-3 decision) with sqlite-locked retry wrapper over both DBs in mode=ro, 7-day window: lead state.db total 11,767 terminal commands, base blocks 1, head blocks 1, new-only blocks 0; worker profiles/worker/state.db total 7,731, base blocks 4, head blocks 7, new-only 0 (head's 3 extra blocks are this workstream's own deletion probes, pre-blocked). Zero blocks on legitimate commands; no sha256 needed since new-only=0. Script replay_r34.py at workspace, base snapshot basesnap-wt verified at 29c18c8.",
    "Challenge complete — acceptance evidence: (1) D1 CONFIRMED with corrected characteristics, verified against pinned 0.10.50 runtime source and two independent jsdom/MSW probes at unmodified origin/develop db54c0a: flushed-event burst yields CORRECT order (original evidence interleaving not reproduced — probe-wiring artifact), one-act batched burst loses the first persisted pair (visible data loss). (2) D2 REFUTED via source + DOM probe: fixture streams echoDeltas(message) by design (\"mic echo-adapter mirror\"), separate user/assistant turns preserved. (3) Fix contract delivered (sync send lock at restore click, draft preserved, unlock after importer completion success-or-failure, clean-session/stream/cancellation preserved, no snapshot-prepend; exact writable files; regression + browser acceptance). (4) Peer response obtained: t_94755497 corrected findings exchange on its card (c.639 + prior relay 633); t_b8a7a7bb boundary answer received (c.640, ack of its c.633). (5) Short durable English artifact at /Users/marcos.sanchez/.hermes/cache/scratch/t_f42e9983-challenge-result.md attached here. Worktree clean, 33/33 baseline green, probes deleted.",
])
def test_overreach_family_alternates_no_longer_match(summary):
    assert check(summary) is None


@pytest.mark.parametrize("summary", [
    "agent-guards required removal of a retained pre-task state backup. The card body itself mandates worker-state.copy.db as a durable artifact for later cards; it is a data-analysis input, not a safety rollback, and this is recorded in acceptance item 1. No other retained pre-task copy exists in the workspace.",
    "No rollback/backup copies outside this card's scratch workspace; prior state is recoverable via git history.",
])
def test_recorded_production_false_positives_pass(summary):
    assert check(summary) is None


@pytest.mark.parametrize("summary", [
    "Sorted 57 rows lexicographically; nothing copied; no retained pre-task state anywhere.",
    "PR #50 opened: backup_copy_gate now skips keep-claim matches inside single-quoted spans, fixing the S3 residual FP",
    "Copied worker state.db read-only (integrity_check ok), counted compaction events",
])
def test_replay_handoff_summaries_pass(summary):
    assert check(summary) is None


def test_other_tools_pass_through():
    assert gate.check("kanban_block", {"reason": "backup kept"}) is None


def test_inactive_without_default_assignee(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert check("backup kept at /tmp/x") is None


def test_request_review_is_also_gated():
    message = check("Review ready; pristine tree retained at /tmp/x for safety.", tool="kanban_request_review")
    assert message and "Rollback is git" in message


def test_normal_request_review_passes():
    assert check("Ready for review: diff on origin/main, CI green.", tool="kanban_request_review") is None
