import inspect
import subprocess
import sys
from pathlib import Path

import pytest

guards = sys.modules["shell_guards"]
gate = guards.base_branch_gate


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo)] + list(args), check=True, capture_output=True, text=True
    )


def make_repo(root: Path, name: str, default: str, origin_head: bool = True) -> str:
    """A throwaway git repo whose refs/remotes/origin/HEAD names ``default`` (no network)."""
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", default)
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", "f.txt")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    if origin_head:
        _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{default}")
    return str(repo)


@pytest.fixture(scope="module")
def repos(tmp_path_factory):
    root = tmp_path_factory.mktemp("repos")
    return {
        "main": make_repo(root, "main-default", "main"),
        "master": make_repo(root, "master-default", "master"),
        "develop": make_repo(root, "develop-default", "develop"),
        "nohead": make_repo(root, "no-origin-head", "main", origin_head=False),
        "plain": str(root / "not-a-repo"),
    }


def check(command, repo=None, workdir=True):
    args = {"command": command}
    if workdir:
        args["workdir"] = repo
    return gate.check("terminal", args)


def blocked(command, repo, workdir=True):
    return check(command, repo, workdir) is not None


def message_of(command, repo, workdir=True):
    message = check(command, repo, workdir)
    assert message is not None
    return message




def test_gh_pr_create_wrong_long_lived_base_is_blocked(repos):
    message = message_of("gh pr create --title t --base develop", repos["main"])
    assert "default branch is 'main'" in message


def test_gh_pr_create_base_attribute_form_is_blocked(repos):
    assert blocked("gh pr create --base=develop", repos["main"])


def test_gh_pr_create_default_base_passes(repos):
    assert check("gh pr create --title t --base main", repos["main"]) is None


def test_gh_pr_create_omitted_base_passes(repos):
    assert check("gh pr create --title t --fill", repos["main"]) is None


def test_gh_pr_create_feature_base_passes_stacked(repos):
    assert check("gh pr create --base feature/y --head feature/x", repos["main"]) is None


def test_gh_pr_create_release_base_passes(repos):
    assert check("gh pr create --base release/25.50", repos["main"]) is None


def test_gh_pr_create_head_flag_only_passes(repos):
    assert check("gh pr create --head hotfix/x", repos["main"]) is None




def test_az_pr_create_wrong_target_branch_is_blocked(repos):
    message = message_of("az repos pr create --target-branch main --repository r", repos["master"])
    assert "default branch is 'master'" in message


def test_az_pr_create_default_target_branch_passes(repos):
    assert check("az repos pr create --target-branch master --repository r", repos["master"]) is None


def test_az_pr_create_short_t_flag(repos):
    assert blocked("az repos pr create -t main", repos["master"])
    assert check("az repos pr create -t master", repos["master"]) is None


def test_az_pr_create_feature_target_passes(repos):
    assert check("az repos pr create --target-branch feature/y", repos["master"]) is None


def test_develop_default_repo_gh_base_main_is_blocked(repos):
    message = message_of("gh pr create --base main", repos["develop"])
    assert "default branch is 'develop'" in message
    assert check("gh pr create --base develop", repos["develop"]) is None


def test_develop_default_repo_branch_start_passes(repos):
    assert check("git checkout -b x origin/develop", repos["develop"]) is None
    assert check("git switch -c x develop", repos["develop"]) is None
    assert check("git worktree add p -b x origin/develop", repos["develop"]) is None
    assert check("git branch x origin/develop", repos["develop"]) is None




@pytest.mark.parametrize(
    "command",
    [
        "git checkout -b x origin/develop",
        "git checkout -b x develop",
        "git checkout -q -b x origin/develop",
        "git switch -c x develop",
        "git switch -c x origin/develop",
        "git worktree add p -b x origin/develop",
        "git worktree add p -b x develop",
        "git worktree add -b x p origin/develop",
        "git worktree add -b x -track p origin/develop",
        "git branch x origin/develop",
        "git branch x develop",
        "git checkout --track -b x origin/develop",
        "develop=x git checkout -b x origin/develop",
    ],
)
def test_branch_start_points_at_long_lived_branch_are_blocked(repos, command):
    message = message_of(command, repos["main"])
    assert "default branch is 'main'" in message


def test_git_dash_c_points_at_the_real_repo(repos):
    command = "git -C %s checkout -b x origin/develop" % repos["main"]
    assert "default branch is 'main'" in message_of(command, None, workdir=False)


@pytest.mark.parametrize(
    "command",
    [
        "git checkout -b x origin/feature/y",
        "git checkout -b x",
        "git switch -c x",
        "git branch",
        "git branch --list",
        "git branch -D x",
        "git branch x main",
        "git worktree add p HEAD",
        "git worktree list",
        "git rebase -i develop",
    ],
)
def test_branch_start_point_exceptions_pass(repos, command):
    assert check(command, repos["main"]) is None




def test_cd_in_command_resolves_the_repo(repos):
    command = "cd '%s' && git checkout -b x origin/develop" % repos["main"]
    assert message_of(command, None, workdir=False)


def test_cd_updates_the_cwd_for_later_statements(repos):
    command = "cd '%s'; gh pr create --base develop" % repos["main"]
    assert message_of(command, None, workdir=False)


def test_git_dash_c_resolves_the_repo(repos):
    command = "git -C %s checkout -b x origin/develop" % repos["main"]
    assert message_of(command, None, workdir=False)


def test_cd_to_missing_directory_fails_open(repos):
    assert check("cd /definitely/missing/dir && git checkout -b x origin/develop", None, workdir=False) is None


def test_gh_repo_flag_without_local_clone_fails_open(repos):
    assert check("gh pr create -R org/app-wflow --base develop", repos["plain"]) is None
    assert check("gh pr create --repo org/app-wflow --base develop", None, workdir=False) is None


def test_gh_repo_local_name_resolves(repos):
    name = Path(repos["main"]).name
    assert message_of("cd '%s' && gh pr create -R %s --base develop" % (repos["main"], name), None, workdir=False)




def test_no_origin_head_fails_open(repos):
    assert check("gh pr create --base develop", repos["nohead"]) is None


def test_not_a_git_repo_fails_open(repos):
    assert check("gh pr create --base develop", repos["plain"]) is None


def test_internal_error_fails_open(repos, monkeypatch):
    def boom(repo):
        raise RuntimeError("boom")

    monkeypatch.setattr(gate, "default_branch", boom)
    assert check("gh pr create --base develop", repos["main"]) is None


def test_not_terminal_and_bad_args_fail_open():
    assert gate.check("read_file", {"command": "gh pr create --base develop"}) is None
    assert gate.check("terminal", None) is None
    assert gate.check("terminal", {}) is None


def test_other_commands_pass(repos):
    assert check("git stash pop; git push origin b", repos["main"]) is None




@pytest.mark.parametrize(
    "command,repo_key",
    [
        ("gh pr create --title t --base develop", "main"),
        ("az repos pr create -t main", "master"),
        ("git checkout -b x origin/develop", "main"),
    ],
)
def test_message_names_the_corrected_command_that_passes(repos, command, repo_key):
    message = message_of(command, repos[repo_key])
    corrected = message.rsplit("Run instead: ", 1)[1].splitlines()[0]
    assert check(corrected, repos[repo_key]) is None, corrected


def test_hook_blocks_with_attribution(repos):
    result = guards._pre_tool_call(
        tool_name="terminal", args={"command": "gh pr create --base develop", "workdir": repos["main"]}
    )
    assert result["action"] == "block" and "[base_branch_gate]" in result["message"]


def test_gate_is_registered_on_pre_tool_call():
    assert gate.HOOK == "pre_tool_call"
    assert "base_branch_gate" in inspect.getsource(guards._pre_tool_call)
