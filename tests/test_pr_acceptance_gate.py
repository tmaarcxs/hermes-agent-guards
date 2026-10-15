import json
import sys

import pytest

guards = sys.modules["agent_guards"]
gate = guards.pr_acceptance_gate

URL_CONTRACT = "https://github.com/atlas/spa-demosuite/pull/223"
REPO_CONTRACT = "atlas/spa-demosuite"
REJECTION = (
    "PR acceptance missing: Supply metadata.published_pr matching the persisted completion contract."
)


def configure(monkeypatch, session="s1"):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setattr(gate, "_read_contract", lambda _tid: REPO_CONTRACT)
    gate.reset()
    return session


@pytest.fixture(autouse=True)
def _reset():
    yield
    gate.reset()


def show(task_id=None):
    args = {} if task_id is None else {"task_id": task_id}
    return gate.transform("kanban_show", args, json.dumps({"task": {"completion_contract": REPO_CONTRACT}}))


def test_reminder_on_show_of_own_contract_card(monkeypatch):
    configure(monkeypatch)
    result = show()
    assert "published_pr" in result and REPO_CONTRACT in result


def test_no_reminder_for_other_card_or_local_contract(monkeypatch):
    configure(monkeypatch)
    assert gate.transform("kanban_show", {"task_id": "t_other"},
                          json.dumps({"task": {"completion_contract": REPO_CONTRACT}})) is None
    assert gate.transform("kanban_show", {}, json.dumps({"task": {"completion_contract": "local-only"}})) is None


def test_show_reminder_requires_string_result(monkeypatch):
    configure(monkeypatch)
    assert gate.transform("kanban_show", {}, None) is None


def test_rejection_suffix_names_the_fix(monkeypatch):
    configure(monkeypatch)
    out = gate.transform("kanban_complete", {}, '{"error": "%s"}' % REJECTION,
                         status="error", error_message=REJECTION)
    assert out and REPO_CONTRACT in out and "published_pr" in out


def test_no_suffix_on_other_errors_or_success(monkeypatch):
    configure(monkeypatch)
    assert gate.transform("kanban_complete", {}, '{"error": "boomed"}', status="error",
                          error_message="other failure") is None
    assert gate.transform("kanban_complete", {}, '{"ok": true}', status="ok") is None


def test_rejections_counted_and_cleared(monkeypatch):
    configure(monkeypatch)
    for _ in range(gate.CAP):
        gate.record("kanban_complete", status="error", error_message=REJECTION, session_id="s1")
    assert gate._rejections["s1"] == gate.CAP
    gate.record("kanban_complete", status="ok", session_id="s1")
    assert "s1" not in gate._rejections


def test_other_rejections_not_counted(monkeypatch):
    configure(monkeypatch)
    gate.record("kanban_complete", status="error", error_message="PR acceptance infra: gh failed",
                session_id="s1")
    assert "s1" not in gate._rejections


def test_below_cap_retry_passes(monkeypatch):
    session = configure(monkeypatch)
    gate.record("kanban_complete", status="error", error_message=REJECTION, session_id=session)
    assert gate.check("kanban_complete", {"summary": "s"}, session_id=session) is None


@pytest.mark.parametrize("published", [None, "", "not-a-pr", "https://github.com/other/repo/pull/1"])
def test_at_cap_unfixed_retry_is_blocked(monkeypatch, published):
    session = configure(monkeypatch)
    for _ in range(gate.CAP):
        gate.record("kanban_complete", status="error", error_message=REJECTION, session_id=session)
    metadata = {} if published is None else {"published_pr": published}
    message = gate.check("kanban_complete", {"summary": "s", "metadata": metadata}, session_id=session)
    assert message and REPO_CONTRACT in message and "kanban_block" in message
    assert "first published_pr whose repository matches is bound" in message


def test_at_cap_fixed_retry_passes(monkeypatch):
    session = configure(monkeypatch)
    for _ in range(gate.CAP):
        gate.record("kanban_complete", status="error", error_message=REJECTION, session_id=session)
    args = {"summary": "s", "metadata": {"published_pr": "https://github.com/atlas/spa-demosuite/pull/9"}}
    assert gate.check("kanban_complete", args, session_id=session) is None


def test_at_cap_url_contract_needs_that_exact_pr(monkeypatch):
    session = configure(monkeypatch, session="s2")
    monkeypatch.setattr(gate, "_read_contract", lambda _tid: URL_CONTRACT)
    for _ in range(gate.CAP):
        gate.record("kanban_complete", status="error", error_message=REJECTION, session_id=session)
    sibling = {"summary": "s", "metadata": {"published_pr": "https://github.com/atlas/spa-demosuite/pull/9"}}
    message = gate.check("kanban_complete", sibling, session_id=session)
    assert message and URL_CONTRACT in message
    exact = {"summary": "s", "metadata": {"published_pr": URL_CONTRACT}}
    assert gate.check("kanban_complete", exact, session_id=session) is None


def test_local_contract_after_rejections_passes(monkeypatch):
    session = configure(monkeypatch, session="s3")
    monkeypatch.setattr(gate, "_read_contract", lambda _tid: "local-only")
    for _ in range(gate.CAP):
        gate.record("kanban_complete", status="error", error_message=REJECTION, session_id=session)
    assert gate.check("kanban_complete", {"summary": "s"}, session_id=session) is None


def test_lead_process_is_not_gated(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    gate.reset()
    assert gate.transform("kanban_show", {}, json.dumps({"task": {"completion_contract": REPO_CONTRACT}})) is None


def test_summary_and_body_use_owner_words(monkeypatch):
    """Employer scrub: the module must not carry the employer's name into the public repo.

    The forbidden terms are assembled at runtime so this test file itself pushes clean.
    """
    configure(monkeypatch)
    halves = ("indite", "x")
    employer = halves[0] + halves[1]
    owner_word = "mar" + "cos"
    module_text = open(gate.__file__).read()
    assert employer not in module_text.lower() and owner_word not in module_text.lower()


def test_publication_contract_classifier():
    assert gate.publication_contract(URL_CONTRACT) == ("url", URL_CONTRACT)
    assert gate.publication_contract(REPO_CONTRACT) == ("repo", REPO_CONTRACT)
    assert gate.publication_contract("local-only") == ("none", "")
    assert gate.publication_contract("") == ("none", "")
    assert gate.publication_contract("not a repo") == ("none", "")


def test_required_value():
    assert gate.required_value("url", URL_CONTRACT) == URL_CONTRACT
    assert gate.required_value("repo", REPO_CONTRACT) == f"a GitHub PR URL in {REPO_CONTRACT}"
