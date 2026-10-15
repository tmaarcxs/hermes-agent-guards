import sys
import time

import pytest

guards = sys.modules["agent_guards"]
gate = guards.approval_wait_gate


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_0e21b6ca")
    gate.reset()


def check(command, tool="terminal"):
    return gate.check(tool, {"command": command})


# --- The run-3517 poll shapes must be blocked when the PR is approval-only ----

PR_STATE = {
    "data": {"repository": {"pullRequest": {
        "state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED", "reviewDecision": "REVIEW_REQUIRED",
        "reviewThreads": {"totalCount": 0},
        "commits": {"nodes": [{"commit": {"oid": "3fe91b2fa1d8e5a394cba76112f78df92afe36b1", "statusCheckRollup": {
            "contexts": {"nodes": [{"__typename": "CheckRun", "name": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"}]}
        }}}]}},
    }}
}


@pytest.fixture
def approval_only(monkeypatch):
    monkeypatch.setattr(gate, "_graphql", lambda *a: True)


@pytest.mark.parametrize("command", [
    "gh pr checks 410 --repo atlas/spa-demosuite",
    "gh pr checks 410 -R atlas/spa-demosuite && echo done",
    "gh pr view 410 --repo atlas/spa-demosuite --json statusCheckRollup --jq '.statusCheckRollup[] | .name'",
    "gh pr view 410 -R atlas/spa-demosuite --json mergeStateStatus,reviewDecision",
    "gh api repos/atlas/spa-demosuite/pulls/410/reviews --jq '[.[] | .state]'",
    "bash ~/.hermes/skills/github/github-agent-ops/scripts/pr_status --repo atlas/spa-demosuite --head 410",
    "bash ~/.hermes/skills/github/github-agent-ops/scripts/pr_wait_checks --repo atlas/spa-demosuite --timeout 420 410",
    "for i in $(seq 1 19); do gh pr checks 410 -R atlas/spa-demosuite; sleep 40; done",
])
def test_approval_only_polls_are_blocked(command, approval_only):
    message = check(command)
    assert message and "REVIEW-ONLY" in message
    assert "pr_merge_verified" in message and "kanban_block(kind=\"needs_input\")" in message
    assert "sp-a" not in message  # no mangled repo names


def test_verdict_is_cached_for_the_ttl(approval_only, monkeypatch):
    calls = []
    monkeypatch.setattr(gate, "_graphql", lambda *a: calls.append(1) or True)
    check("gh pr checks 410 --repo atlas/spa-demosuite")
    check("gh pr view 410 -R atlas/spa-demosuite --json statusCheckRollup")
    check("gh api repos/atlas/spa-demosuite/pulls/410/reviews")
    assert len(calls) == 1
    # after the TTL expires the state is re-read within the next poll
    real_time = time.time
    monkeypatch.setattr(gate.time, "time", lambda: real_time() + gate.TTL_SECONDS + 1)
    check("gh pr checks 410 --repo atlas/spa-demosuite")
    assert len(calls) == 2


def test_blocked_poll_does_not_eat_other_guards_output(approval_only):
    message = check("GH_REPO=atlas/spa-demosuite gh pr checks 410")
    assert message and "atlas/spa-demosuite#410" in message


# --- Polls that still have something real to wait for must pass through -------

@pytest.mark.parametrize("verdict", [False, None])
def test_pending_failing_and_unknown_states_flow_through(verdict, monkeypatch):
    monkeypatch.setattr(gate, "_graphql", lambda *a: verdict)
    assert check("gh pr checks 410 --repo atlas/spa-demosuite") is None
    assert check("gh api repos/atlas/spa-demosuite/pulls/410/reviews") is None


def test_unresolvable_repo_fails_open():
    assert check("gh pr checks 410") is None


# --- Workflow targets, merges and non-readiness reads stay allowed ------------

def test_run_watch_and_non_readiness_reads_pass(approval_only):
    assert check("gh run watch 41284910567 --repo atlas/spa-demosuite --exit-status") is None
    assert check("gh run list --repo atlas/spa-demosuite --workflow=code-npm_ci-release.yml") is None
    assert check("gh pr view 410 --repo atlas/spa-demosuite --json title,body") is None
    assert check("gh pr view 410 --repo atlas/spa-demosuite --json headRefOid -q .headRefOid") is None


def test_merge_script_invocation_passes(approval_only):
    assert check(
        "bash ~/.hermes/skills/github/github-agent-ops/scripts/pr_merge_verified "
        "--repo atlas/spa-demosuite 410 3fe91b2fa1d8e5a394cba76112f78df92afe36b1"
    ) is None


# --- The repeating wait-admitting heartbeat ------------------------------------

def _hb(note, session="s1"):
    return gate.check("kanban_heartbeat", {"note": note}, session_id=session)


def test_first_wait_note_passes_second_is_blocked():
    note = "PR #410 still OPEN. CODEOWNERS approval is the sole remaining gate; long poller continues."
    assert _hb(note) is None
    message = _hb(note)
    assert message and "pr_merge_verified" in message and "needs_input" in message


def test_other_sessions_are_counted_separately():
    note = "waiting on CODEOWNERS approval"
    assert _hb(note, session="a") is None
    assert _hb(note, session="b") is None
    assert _hb(note, session="a") is not None


def test_real_progress_notes_pass_repeatedly():
    for _ in range(3):
        assert _hb("Release run 41284910567 success; watching deploy run f070ece4") is None
        assert _hb("DES deployment PR merged; verifying image version") is None


def test_variants_of_the_admission_are_caught():
    for note in (
        "review-required on #410, nothing I can do tonight",
        "The approval gate is what we are waiting for",
        "unreviewed PR; waiting until business hours",
    ):
        assert _hb(note, session=f"s-{abs(hash(note))}") is None  # first mention passes
        assert _hb(note, session=f"s-{abs(hash(note))}") is not None  # repetition blocked


# --- Activation scope -----------------------------------------------------------

def test_lead_process_not_guarded(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert check("gh pr checks 410 --repo atlas/spa-demosuite") is None
    assert gate.check("kanban_heartbeat", {"note": "CODEOWNERS approval is the sole remaining gate"}, session_id="x") is None


def test_inactive_without_default_assignee(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert check("gh pr checks 410 --repo atlas/spa-demosuite") is None


def test_worker_setup_without_task_env(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert check("gh pr checks 410 --repo atlas/spa-demosuite") is None


def test_heartbeat_without_note_passes():
    assert gate.check("kanban_heartbeat", {}, session_id="s9") is None
