import sys

import pytest

guards = sys.modules["agent_guards"]
gate = guards.published_head_gate

PR = "https://github.com/org/sample-api/pull/1066"
PUBLISHED = "0b9f3062e6e4d2372f5f832e7afb97e12cc7c51e"


@pytest.fixture(autouse=True)
def worker_process(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_pub0003")


def review(metadata, summary=f"Corrections on {PR}", head=PUBLISHED):
    calls = []

    def live_head(owner, repo, number):
        calls.append((owner, repo, number))
        return head

    return gate.check("kanban_request_review", {"summary": summary, "metadata": metadata}, live_head=live_head), calls


def test_unpushed_claimed_commit_is_blocked():
    message, calls = review({"commit": "0e533ed2"})
    assert calls == [("org", "sample-api", "1066")]
    assert "0e533ed2" in message and "0b9f3062e6e4" in message


def test_published_commit_passes_full_or_short():
    assert review({"commit": PUBLISHED})[0] is None
    assert review({"head": "0b9f3062"})[0] is None


def test_pr_url_in_metadata_is_found():
    message, _ = review({"pr": PR, "commit": "0e533ed2"}, summary="done")
    assert message


def test_missing_values_or_unreadable_github_fail_open():
    assert review({"commit": "0e533ed2"}, summary="no pr here")[0] is None
    assert review({"pr": PR})[0] is None
    assert review({"commit": "0e533ed2"}, head=None)[0] is None


def test_lead_process_is_not_checked(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert review({"commit": "0e533ed2"})[0] is None
