import sys

import pytest

guards = sys.modules["kanban_guards"]
gc = guards.github_contact


def run(command, workdir=None):
    return guards._pre_tool_call(
        tool_name="terminal", args={"command": command, "workdir": workdir}
    )


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_dem0002")
    monkeypatch.setattr(gc, "_login_cache", "nobody-here")
    monkeypatch.setattr(gc, "_login_resolved", True)


@pytest.fixture
def body_files(tmp_path):
    (tmp_path / "body.md").write_text("Thanks @alice-dev, please review")
    (tmp_path / "clean.md").write_text("Evidence attached, exit 0, 12 passed")
    return tmp_path


@pytest.mark.parametrize(
    "command",
    [
        "gh pr edit 1 -R o/r --add-reviewer alice-dev",
        "gh pr edit 1 --add-reviewer=alice-dev",
        "GH_REPO=o/r gh pr edit 1 --add-reviewer alice-dev",
        "gh pr create -t t -b b -r reviewer-one,myorg/team -B main",
        "gh pr create --reviewer reviewer-one",
        "gh issue create --assignee reviewer-one,reviewer-two",
        "gh issue edit 12 --add-assignee reviewer-one",
        "gh api repos/o/r/pulls/7/requested_reviewers -F 'reviewer_ids[]=[1]'",
        "gh api -X POST repos/o/r/issues/1/requested_reviewers -f 'reviewers[]=reviewer-one'",
        "gh pr review 123 --approve",
        "gh pr review 123 --request-changes -b 'please address'",
        "gh pr review",
        "sudo gh pr review 1",
        "cd x && gh pr edit 1 --add-reviewer y",
        "gh pr edit 1 --add-reviewer y; gh issue comment 2 --body 'hi @you'",
    ],
)
def test_review_requests_blocked(command):
    result = run(command)
    assert result is not None and result["action"] == "block"
    assert "needs the owner's approval" in result["message"]
    assert "kanban_block" in result["message"] and "kind=needs_input" in result["message"]


@pytest.mark.parametrize(
    "command",
    [
        "gh pr edit 1 --add-reviewer=@copilot",
        "gh issue edit 3 --remove-assignee reviewer-one",
        "gh pr edit 1 --remove-reviewer somebody",
        "gh issue create --assignee '@me' -t t",
    ],
)
def test_copilot_and_me_not_people(command):
    assert run(command) is None


@pytest.fixture
def login(monkeypatch):
    """Force a cached authenticated login; any real `gh` call fails the test."""
    def _set(login):
        monkeypatch.setattr(gc, "_login_cache", login)
        monkeypatch.setattr(gc, "_login_resolved", True)
        monkeypatch.setattr(
            gc.subprocess, "run",
            lambda *a, **k: pytest.fail("network call leaked: gh ran during check"),
        )
    return _set


def test_self_assignment_allowed(login):
    login("owner-dev")
    for command in (
        "gh issue create --assignee owner-dev -t t",
        "gh issue create -a owner-dev -t t",
        "gh pr create -t t -b b -a owner-dev",
        "gh issue edit 3 --add-assignee owner-dev",
    ):
        assert run(command) is None


def test_self_assignment_mixed_values(login):
    login("owner-dev")
    result = run("gh issue create --assignee owner-dev,reviewer-one -t t")
    assert result is not None and result["action"] == "block"
    assert run("gh issue create --assignee reviewer-one -t t") is not None


def test_self_assignment_comma_list_pure_self_allowed(login):
    login("owner-dev")
    assert run("gh issue create -t x --assignee '@OWNER-DEV,owner-dev '") is None


def test_self_assignment_case_and_at_sign_insensitive(login):
    login("owner-dev")
    assert run("gh issue create --assignee '@OWNER-DEV' -t t") is None
    assert run("gh issue edit 3 --add-assignee 'Owner-Dev'") is None


def test_self_assignment_not_applied_to_reviewer_flags(login):
    login("owner-dev")
    assert run("gh pr edit 1 --add-reviewer owner-dev") is not None
    assert run("gh pr create -t t -b b -r owner-dev") is not None


@pytest.mark.parametrize(
    "command",
    [
        "gh issue create --assignee reviewer-one -t t",
        "gh issue create -a reviewer-one,reviewer-two -t t",
        "gh pr create -t t -b b -a owner-dev",
        "gh issue edit 3 --add-assignee reviewer-one",
    ],
)
def test_login_unresolvable_fails_closed(command, monkeypatch):
    monkeypatch.setattr(gc, "_login_cache", None)
    monkeypatch.setattr(gc, "_login_resolved", False)
    calls = []
    monkeypatch.setattr(
        gc.subprocess, "run",
        lambda *a, **k: calls.append(1) or type("P", (), {"returncode": 1, "stdout": ""})(),
    )
    result = run(command)
    assert result is not None and result["action"] == "block"


@pytest.mark.parametrize(
    "exc",
    [
        __import__("subprocess").TimeoutExpired(cmd="gh", timeout=10),
        __import__("subprocess").CalledProcessError(returncode=1, cmd="gh"),
        OSError("boom"),
    ],
    ids=["timeout", "called-process-error", "oserror"],
)
def test_resolver_crash_fails_closed_not_fail_open(exc, monkeypatch):
    """A crashing resolver must not crash the guard chain — the chain swallows
    exceptions into pass-through, so swallowed == fail-open. Every failure path
    must instead resolve to cache= None and keep blocking non-@me values."""
    monkeypatch.setattr(gc, "_login_cache", None)
    monkeypatch.setattr(gc, "_login_resolved", False)

    def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(gc.subprocess, "run", boom)
    assert gc.authenticated_login() is None
    result = run("gh issue create -t x --assignee reviewer-one")
    assert result is not None and result["action"] == "block"


def test_login_resolved_once_per_process(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        return type("P", (), {"returncode": 0, "stdout": "owner-dev\n"})()

    monkeypatch.setattr(gc, "_login_cache", None)
    monkeypatch.setattr(gc, "_login_resolved", False)
    import unittest.mock as mock
    with mock.patch.object(gc.subprocess, "run", fake_run):
        assert gc.authenticated_login() == "owner-dev"
        assert gc.authenticated_login() == "owner-dev"
    assert calls == [1]


def test_comment_with_mention_blocked(body_files):
    result = run("gh pr comment 1 --body 'pinging @alice-dev to review'")
    assert result is not None and result["action"] == "block"
    result = run("gh issue comment 5 -b 'cc @org/team'")
    assert result is not None and result["action"] == "block"
    result = run(str.format("gh pr comment 1 -F {0}/body.md", body_files), workdir=str(body_files))
    assert result is not None and result["action"] == "block"


def test_comment_without_mention_allowed(body_files):
    assert run("gh pr comment 1 --body 'looks good, evidence attached'") is None
    assert run(str.format("gh pr comment 1 -F {0}/clean.md", body_files), workdir=str(body_files)) is None


@pytest.mark.parametrize(
    "text",
    [
        "fix applied, contact myemail@example.com",
        "see https://x.com/@user/status/1 and `@codeme`",
        "a@b.com and @me and @copilot did this",
        "@123 and @-bad and @. are not logins",
    ],
)
def test_at_lookalikes_not_mentions(text):
    assert gc._has_mention(text) is False


@pytest.mark.parametrize("text", ["@alice-dev", "@org/team", "ping @user.", "(@someone)", '"@user"', "@missing"])
def test_real_mentions_found(text):
    assert gc._has_mention(text) is True


def test_api_comment_mentions_blocked(body_files):
    command = "gh api repos/o/r/issues/1/comments -F body=@{0}/body.md --method POST".format(body_files)
    result = run(command)
    assert result is not None and result["action"] == "block"
    result = run("gh api repos/o/r/issues/1/comments -f body='@someone check this'")
    assert result is not None and result["action"] == "block"


def test_api_inline_mention_body_blocked():
    result = run("gh api repos/o/r/issues/1/comments -f body='@someone check this'")
    assert result is not None and result["action"] == "block"


def test_api_get_and_read_paths_allowed():
    assert run("gh api repos/o/r/issues/1/comments") is None
    assert run("gh api repos/o/r/pulls/7/requested_reviewers") is None
    assert run("gh api repos/o/r/issues/1/comments -f body='all clear'") is None


def test_stdin_and_unreadable_bodies_fail_open(body_files):
    assert run("gh pr comment 1 --body-file -") is None
    assert run("gh pr comment 1 -F no-such-file.md") is None
    assert run("gh api repos/o/r/issues/1/comments --input -") is None


def test_lead_process_not_guarded(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert run("gh pr edit 1 --add-reviewer x") is None
    assert run("gh pr review 1") is None


def test_message_names_authorized_alternative():
    result = run("gh pr edit 1 --add-reviewer x")
    assert "needs the owner's approval" in result["message"]
    assert "kanban_block" in result["message"] and "kind=needs_input" in result["message"]
    assert "needs_input" in result["message"]
