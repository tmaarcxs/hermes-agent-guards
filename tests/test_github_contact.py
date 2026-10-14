import sys

import pytest

guards = sys.modules["agent_guards"]
gc = guards.github_contact


def run(command, workdir=None):
    return guards._pre_tool_call(
        tool_name="terminal", args={"command": command, "workdir": workdir}
    )


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_28cfac35")
    # Pin a login that appears in no test command, so guard checks never shell
    # out to `gh api user`; self-assignment tests override via the `login` fixture.
    monkeypatch.setattr(gc, "_login_cache", "nobody-here")
    monkeypatch.setattr(gc, "_login_resolved", True)


@pytest.fixture
def body_files(tmp_path):
    (tmp_path / "body.md").write_text("Thanks @celiafl97, please review")
    (tmp_path / "clean.md").write_text("Evidence attached, exit 0, 12 passed")
    return tmp_path


@pytest.mark.parametrize(
    "command",
    [
        "gh pr edit 1 -R o/r --add-reviewer celiafl97",
        "gh pr edit 1 --add-reviewer=celiafl97",
        "GH_REPO=o/r gh pr edit 1 --add-reviewer celiafl97",
        "gh pr create -t t -b b -r monalisa,myorg/team -B main",
        "gh pr create --reviewer monalisa",
        "gh issue create --assignee monalisa,hubot",
        "gh issue edit 12 --add-assignee monalisa",
        "gh api repos/o/r/pulls/7/requested_reviewers -F 'reviewer_ids[]=[1]'",
        "gh api -X POST repos/o/r/issues/1/requested_reviewers -f 'reviewers[]=monalisa'",
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
    assert "pr_merge_verified" in result["message"]


@pytest.mark.parametrize(
    "command",
    [
        "gh pr edit 1 --add-reviewer=@copilot",
        "gh issue edit 3 --remove-assignee monalisa",
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
    login("kexi")
    for command in (
        "gh issue create --assignee kexi -t t",
        "gh issue create -a kexi -t t",
        "gh pr create -t t -b b -a kexi",
        "gh issue edit 3 --add-assignee kexi",
    ):
        assert run(command) is None


def test_self_assignment_mixed_values(login):
    login("kexi")
    result = run("gh issue create --assignee kexi,monalisa -t t")
    assert result is not None and result["action"] == "block"
    assert run("gh issue create --assignee monalisa -t t") is not None


def test_self_assignment_case_and_at_sign_insensitive(login):
    login("kexi")
    assert run("gh issue create --assignee '@KEXI' -t t") is None
    assert run("gh issue edit 3 --add-assignee 'Kexi'") is None


def test_self_assignment_not_applied_to_reviewer_flags(login):
    login("kexi")
    assert run("gh pr edit 1 --add-reviewer kexi") is not None
    assert run("gh pr create -t t -b b -r kexi") is not None


@pytest.mark.parametrize(
    "command",
    [
        "gh issue create --assignee monalisa -t t",
        "gh issue create -a monalisa,hubot -t t",
        "gh pr create -t t -b b -a kexi",
        "gh issue edit 3 --add-assignee monalisa",
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


def test_login_resolved_once_per_process(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        return type("P", (), {"returncode": 0, "stdout": "kexi\n"})()

    monkeypatch.setattr(gc, "_login_cache", None)
    monkeypatch.setattr(gc, "_login_resolved", False)
    import unittest.mock as mock
    with mock.patch.object(gc.subprocess, "run", fake_run):
        assert gc.authenticated_login() == "kexi"
        assert gc.authenticated_login() == "kexi"
    assert calls == [1]


def test_comment_with_mention_blocked(body_files):
    result = run("gh pr comment 1 --body 'pinging @celiafl97 to review'")
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


@pytest.mark.parametrize("text", ["@celiafl97", "@org/team", "ping @user.", "(@someone)", '"@user"', "@missing"])
def test_real_mentions_found(text):
    assert gc._has_mention(text) is True


def test_api_comment_mentions_blocked(body_files):
    # only -F/--field has @file magic; -f passes the literal string
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
    assert "pr_merge_verified" in result["message"]
    assert "needs_input" in result["message"]
