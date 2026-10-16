import sys
import time

import pytest

guards = sys.modules["agent_guards"]
gate = guards.approval_wait_gate


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_del0001")
    gate.reset()


def check(command, tool="terminal"):
    return gate.check(tool, {"command": command})



PR_STATE = {
    "data": {"repository": {"pullRequest": {
        "state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED", "reviewDecision": "REVIEW_REQUIRED",
        "reviewThreads": {"totalCount": 0},
        "commits": {"nodes": [{"commit": {"oid": "0123456789abcdef0123456789abcdef01234567", "statusCheckRollup": {
            "contexts": {"nodes": [{"__typename": "CheckRun", "name": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"}]}
        }}}]}},
    }}
}


@pytest.fixture
def approval_only(monkeypatch):
    monkeypatch.setattr(gate, "_graphql", lambda *a: True)


REPO = "org/sample-web"

@pytest.mark.parametrize("command", [
    "gh pr checks 410 --repo org/sample-web",
    "gh pr checks 410 -R org/sample-web && echo done",
    "gh pr view 410 --repo org/sample-web --json statusCheckRollup --jq '.statusCheckRollup[] | .name'",
    "gh pr view 410 -R org/sample-web --json mergeStateStatus,reviewDecision",
    "gh api repos/org/sample-web/pulls/410/reviews --jq '[.[] | .state]'",
    "bash scripts/pr_status --repo org/sample-web --head 410",
    "bash scripts/pr_wait_checks --repo org/sample-web --timeout 420 410",
    "for i in $(seq 1 19); do gh pr checks 410 -R org/sample-web; sleep 40; done",
])
def test_approval_only_polls_are_blocked(command, approval_only):
    message = check(command)
    assert message and "REVIEW-ONLY" in message
    assert "kanban_block(kind=\"needs_input\")" in message


def test_verdict_is_cached_for_the_ttl(approval_only, monkeypatch):
    calls = []
    monkeypatch.setattr(gate, "_graphql", lambda *a: calls.append(1) or True)
    check("gh pr checks 410 --repo org/sample-web")
    check("gh pr view 410 -R org/sample-web --json statusCheckRollup")
    check("gh api repos/org/sample-web/pulls/410/reviews")
    assert len(calls) == 1
    real_time = time.time
    monkeypatch.setattr(gate.time, "time", lambda: real_time() + gate.TTL_SECONDS + 1)
    check("gh pr checks 410 --repo org/sample-web")
    assert len(calls) == 2


def test_blocked_poll_does_not_eat_other_guards_output(approval_only):
    message = check("GH_REPO=org/sample-web gh pr checks 410")
    assert message and "org/sample-web#410" in message



@pytest.mark.parametrize("verdict", [False, None])
def test_pending_failing_and_unknown_states_flow_through(verdict, monkeypatch):
    monkeypatch.setattr(gate, "_graphql", lambda *a: verdict)
    assert check("gh pr checks 410 --repo org/sample-web") is None
    assert check("gh api repos/org/sample-web/pulls/410/reviews") is None


def test_unresolvable_repo_fails_open():
    assert check("gh pr checks 410") is None



def test_run_watch_and_non_readiness_reads_pass(approval_only):
    assert check("gh run watch 98765432101 --repo org/sample-web --exit-status") is None
    assert check("gh run list --repo org/sample-web --workflow=ci.yml") is None
    assert check("gh pr view 410 --repo org/sample-web --json title,body") is None
    assert check("gh pr view 410 --repo org/sample-web --json headRefOid -q .headRefOid") is None


def test_merge_script_invocation_passes(approval_only):
    assert check(
        "bash scripts/merge_verified "
        "--repo org/sample-web 410 0123456789abcdef0123456789abcdef01234567"
    ) is None


def test_merge_hint_is_absent_without_env(approval_only, monkeypatch):
    monkeypatch.delenv("AGENT_GUARDS_MERGE_HINT", raising=False)
    message = check("gh pr checks 410 --repo org/sample-web")
    assert message and "documented merge command" not in message


def test_merge_hint_is_named_when_the_setup_documents_one(approval_only, monkeypatch):
    monkeypatch.setenv("AGENT_GUARDS_MERGE_HINT", "bash scripts/merge_verified.sh --repo o/r 410 HEAD")
    message = check("gh pr checks 410 --repo org/sample-web")
    assert message and "bash scripts/merge_verified.sh --repo o/r 410 HEAD" in message



def _hb(note, session="s1"):
    return gate.check("kanban_heartbeat", {"note": note}, session_id=session)


def test_first_wait_note_passes_second_is_blocked():
    note = "PR #410 still OPEN. CODEOWNERS approval is the sole remaining gate; long poller continues."
    assert _hb(note) is None
    message = _hb(note)
    assert message and "needs_input" in message


def test_other_sessions_are_counted_separately():
    note = "waiting on CODEOWNERS approval"
    assert _hb(note, session="a") is None
    assert _hb(note, session="b") is None
    assert _hb(note, session="a") is not None


def test_real_progress_notes_pass_repeatedly():
    for _ in range(3):
        assert _hb("Release run 98765432101 success; watching deploy run abcdef12") is None
        assert _hb("Deploy PR merged; verifying image version") is None


def test_variants_of_the_admission_are_caught():
    for note in (
        "review-required on #410, nothing I can do tonight",
        "The approval gate is what we are waiting for",
        "unreviewed PR; waiting until business hours",
    ):
        assert _hb(note, session=f"s-{abs(hash(note))}") is None  # first mention passes
        assert _hb(note, session=f"s-{abs(hash(note))}") is not None  # repetition blocked



def test_lead_process_not_guarded(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert check("gh pr checks 410 --repo org/sample-web") is None
    assert gate.check("kanban_heartbeat", {"note": "CODEOWNERS approval is the sole remaining gate"}, session_id="x") is None


def test_inactive_without_default_assignee(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert check("gh pr checks 410 --repo org/sample-web") is None


def test_worker_setup_without_task_env(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert check("gh pr checks 410 --repo org/sample-web") is None


def test_heartbeat_without_note_passes():
    assert gate.check("kanban_heartbeat", {}, session_id="s9") is None
