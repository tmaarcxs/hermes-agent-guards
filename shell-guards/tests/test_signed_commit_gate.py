"""Unit tests for the signed-commit gate (TDD: written first, red before the module exists).

The gate decides `requires_signing` per repo from LOCAL data only: a github.com
origin and at least 90 percent of the last 20 commits on `origin/<default>` carrying
a raw `gpgsig` header. When a plain `git commit` runs in such a repo without
`-S`/`--gpg-sign`, without `commit.gpgsign=true` and without `-c commit.gpgsign=true`,
the gate blocks and names the exact corrected command. Everything fails open.
"""

import socket
import subprocess
import sys

import pytest

guards = sys.modules["shell_guards"]
gate = guards.signed_commit_gate


def run_git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _commit(repo, message, signed=False):
    if signed:
        run_git(repo, "commit", "--allow-empty", "-S", "-m", message)
    else:
        run_git(repo, "commit", "--allow-empty", "-m", message)


@pytest.fixture()
def stub_signing(tmp_path):
    real_gpg = subprocess.run(["which", "gpg"], capture_output=True, text=True).stdout.strip()
    if not real_gpg:
        pytest.skip("gpg not installed")
    key = tmp_path / "signkey"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                   capture_output=True, check=True)
    return {"format": "ssh", "key": str(key) + ".pub", "program": real_gpg}


@pytest.fixture()
def signed_repo(tmp_path, stub_signing):
    repo = tmp_path / "signed"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    run_git(repo, "config", "gpg.format", "ssh")
    run_git(repo, "config", "user.signingkey", stub_signing["key"])
    run_git(repo, "config", "gpg.program", stub_signing["program"])
    run_git(repo, "commit", "--allow-empty", "--no-gpg-sign", "-m", "seed")
    for i in range(1, 21):
        _commit(repo, f"c{i}", signed=True)
    bare = tmp_path / "origin.git"
    run_git(repo, "init", "-q", "--bare", str(bare))
    run_git(repo, "push", "-q", str(bare), "main")
    run_git(repo, "fetch", str(bare), "+main:refs/remotes/origin/main")
    (repo / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "refs" / "remotes" / "origin" / "HEAD").write_text("ref: refs/remotes/origin/main\n")
    run_git(repo, "remote", "add", "origin", str(bare))
    run_git(repo, "config", "--unset", "user.signingkey")
    run_git(repo, "config", "--unset", "gpg.program")
    return repo


@pytest.fixture()
def unsigned_repo(tmp_path):
    repo = tmp_path / "unsigned"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    run_git(repo, "commit", "--allow-empty", "--no-gpg-sign", "-m", "seed")
    for i in range(20):
        _commit(repo, f"u{i}")
    bare = tmp_path / "origin2.git"
    run_git(repo, "init", "-q", "--bare", str(bare))
    run_git(repo, "push", "-q", str(bare), "main")
    run_git(repo, "fetch", str(bare), "+main:refs/remotes/origin/main")
    (repo / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "refs" / "remotes" / "origin" / "HEAD").write_text("ref: refs/remotes/origin/main\n")
    run_git(repo, "remote", "add", "origin", str(bare))
    return repo


@pytest.fixture(autouse=True)
def fresh_cache():
    gate.reset_cache()
    yield
    gate.reset_cache()


@pytest.fixture()
def as_github(signed_repo):
    rewrite = run_git(signed_repo, "remote", "set-url", "origin", "https://github.com/o/r.git")
    assert rewrite.returncode == 0
    return signed_repo


def commit_cmd(repo, command, **extra):
    args = {"command": command, "workdir": str(repo)}
    args.update(extra)
    return gate.check("terminal", args)


def test_requires_signing_true_for_signed_history(signed_repo, as_github):
    assert gate.requires_signing(str(signed_repo)) is True


def test_requires_signing_false_for_unsigned_history(unsigned_repo):
    run_git(unsigned_repo, "remote", "set-url", "origin", "https://github.com/o/r.git")
    assert gate.requires_signing(str(unsigned_repo)) is False


def test_requires_signing_false_for_non_github_remote(signed_repo):
    assert gate.requires_signing(str(signed_repo)) is False


def test_verdict_is_cached_per_repo(signed_repo, as_github, monkeypatch):
    calls = []
    real = gate._signed_share

    def counted(repo):
        calls.append(repo)
        return real(repo)

    monkeypatch.setattr(gate, "_signed_share", counted)
    assert gate.requires_signing(str(signed_repo)) is True
    assert gate.requires_signing(str(signed_repo)) is True
    assert len(calls) == 1


def test_cache_expires_after_ten_minutes(signed_repo, as_github, monkeypatch):
    assert gate.requires_signing(str(signed_repo)) is True
    now = gate._now() + gate._CACHE_TTL + 1
    monkeypatch.setattr(gate, "_now", lambda: now)
    assert gate.requires_signing(str(signed_repo)) is True
    assert gate._CACHE_TTL == 600.0


def test_plain_commit_blocked_and_message_names_the_corrected_command(as_github):
    result = commit_cmd(as_github, "git commit -m x")
    assert result and "git commit -S -m x" in result
    assert "git config commit.gpgsign true" in result
    assert "git config gpg.format ssh" in result


def test_commit_with_dash_capital_s_passes(as_github):
    assert commit_cmd(as_github, "git commit -S -m x") is None


def test_commit_with_long_gpg_sign_passes(as_github):
    assert commit_cmd(as_github, "git commit --gpg-sign -m fix") is None


def test_commit_with_commit_gpgsign_true_configured_passes(as_github):
    run_git(as_github, "config", "commit.gpgsign", "true")
    assert commit_cmd(as_github, "git commit -m x") is None


def test_commit_gpgsign_argument_explicitly_true_passes(as_github):
    assert commit_cmd(as_github, "git -c commit.gpgsign=true commit -m x") is None


def test_commit_gpgsign_config_explicit_false_blocks(as_github):
    run_git(as_github, "config", "commit.gpgsign", "false")
    assert commit_cmd(as_github, "git commit -m x")


def test_amend_blocked_too(as_github):
    result = commit_cmd(as_github, "git commit --amend --no-edit")
    assert result and "git commit -S --amend --no-edit" in result


def test_amend_with_sign_flag_passes(as_github):
    assert commit_cmd(as_github, "git commit --amend --no-edit -S") is None


def test_unsigned_history_repo_passes(unsigned_repo):
    run_git(unsigned_repo, "remote", "set-url", "origin", "https://github.com/o/r.git")
    assert commit_cmd(unsigned_repo, "git commit -m x") is None


def test_azure_style_remote_passes(signed_repo):
    assert commit_cmd(signed_repo, "git commit -m x") is None


def test_non_commit_git_commands_pass(as_github):
    for command in (
        "git status", "git push origin main", "git log -5 --oneline",
        "git rebase origin/main", "git notes add",
    ):
        assert commit_cmd(as_github, command) is None


def test_non_git_commands_pass(as_github):
    for command in ("ls -la", "python3 -m pytest -q", "echo git commit"):
        assert commit_cmd(as_github, command) is None


def test_a_commit_mention_inside_a_quoted_string_passes(as_github):
    assert commit_cmd(as_github, "grep 'git commit' docs/x.txt") is None


def test_no_origin_fails_open(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    assert commit_cmd(repo, "git commit -m x") is None


def test_no_remote_tracking_branch_fails_open(tmp_path):
    repo = tmp_path / "notracking"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    run_git(repo, "remote", "add", "origin", "https://github.com/o/r.git")
    run_git(repo, "commit", "--allow-empty", "--no-gpg-sign", "-m", "seed")
    assert commit_cmd(repo, "git commit -m x") is None


def test_missing_git_binary_fails_open(as_github, monkeypatch):
    monkeypatch.setattr(gate, "_GIT", None)
    assert commit_cmd(as_github, "git commit -m x") is None


def test_unparsable_origin_url_fails_open(signed_repo):
    run_git(signed_repo, "remote", "set-url", "origin", "::not-a-url::")
    assert commit_cmd(signed_repo, "git commit -m x") is None


def test_no_socket_is_opened_during_the_check(as_github, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert commit_cmd(as_github, "git commit -m x")


def test_only_a_commit_command_triggers_the_repo_check(as_github, monkeypatch):
    runs = []
    real = gate._resolve_repo

    def counted(cwd):
        runs.append(cwd)
        return real(cwd)

    monkeypatch.setattr(gate, "_resolve_repo", counted)
    commit_cmd(as_github, "git status")
    assert runs == [], "a non-commit command must not pay the repo resolution"


def test_workdir_resolves_the_repo_not_the_process_cwd(as_github, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert commit_cmd(as_github, "git commit -m x") is not None


def test_hook_blocks_with_attribution(as_github):
    result = guards._pre_tool_call(tool_name="terminal", args={"command": "git commit -m x", "workdir": str(as_github)})
    assert result["action"] == "block" and "[signed_commit_gate]" in result["message"]
    assert "git commit -S -m x" in result["message"]


def test_registered_on_the_canonical_hook_name():
    import types

    hooks = []
    ctx = types.SimpleNamespace(register_hook=lambda name, fn: hooks.append(name))
    guards.register(ctx)
    assert "pre_tool_call" in hooks


def test_registered_name_is_not_a_rename():
    import inspect
    import types

    hooks = {}
    ctx = types.SimpleNamespace(register_hook=lambda name, fn: hooks.setdefault(name, []).append(fn))
    guards.register(ctx)
    source = inspect.getsource(guards.register)
    assert 'ctx.register_hook("pre_tool_call"' in source


def test_message_says_what_was_blocked_and_the_exact_alternative(as_github, monkeypatch):
    command = "git commit -m 'x: fix thing'"
    result = commit_cmd(as_github, command)
    assert result.startswith("agent-guards blocked terminal")
    assert "git commit -S -m 'x: fix thing'" in result
