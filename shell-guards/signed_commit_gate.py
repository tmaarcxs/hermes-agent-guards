"""Block an unsigned `git commit` in repos whose remote default branch history is signed.

GitHub repos that carry the `Require signed commits` ruleset reject an unsigned
push with ``GH013 push declined due to repository rule violations`` — the wasted
turn surfaces at push time, turns after the commit. That ruleset is not readable
by every token (the rules endpoints need admin scopes on some orgs and 403 on
personal repos), so the gate decides from LOCAL data only: a github.com origin
AND a >= 90 percent share of `gpgsig` commit headers among the last 20 commits
of `origin/<default>`. Validated 2026-11-05 against
`gh api repos/<o>/<r>/rules/branches/<default>` for every github repo under
~/git_repos: all 5 ruleset-signing repos classified correctly, one
false-positive pair that carries fully signed (bot-generated) history without an
active ruleset (cost: one spurious `-S` flag), zero false negatives; the Azure
DevOps checkouts short-circuit to not-required by remote host. The verdict is
cached per resolved real working directory for 10 minutes. The corrected command
carries `-S`, which honors the repo's `gpg.format` (git would default to GPG and
fail on an SSH-signing repo); the message also names the exact `git config`
lines that make signing permanent. Fails open on every error, missing git,
timeout or doubt; never touches the network, never runs gpg or ssh-keygen.
"""

import os
import re
import shutil
import subprocess
import urllib.parse
from typing import Any, Optional

from . import hermes_home_gate, terminal_policy

_CACHE_TTL = 600.0
_SIGNED_SHARE_THRESHOLD = 0.9
_HISTORY_DEPTH = 20
_TIMEOUT_S = 2.0
_GIT = shutil.which("git")
_GITHUB_HOSTS = {"github.com"}
_VERDICT_CACHE = {}
_CONFIG_KEYS = "remote\\.(origin)\\.url|commit\\.gpgsign|gpg\\.format|user\\.signingkey"

_COMMIT = re.compile(terminal_policy._START + r"(?:env\s+\S+\s+)*(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*git\s+(?:(?:-C|-c)\s+\S+\s+)*commit(?=\s|$)(?!-)")
_SIGN_ARG = re.compile(r"(?:^|\s)(?:-S|--gpg-sign)(?:(?=\s)|$|=\S)")
_UNSET_ARG = re.compile(r"(?:^|\s)--no-gpg-sign(?:\s|$)")
_CONFIG_TRUE_ARG = re.compile(r"(?:^|\s)(?:-c\s+)?commit\.gpgsign=(?:true|1|yes|on)\b")
_TRUE_WORDS = ("true", "1", "yes", "on")


def _now() -> float:
    """Wall clock, isolated for tests."""
    import time

    return time.time()


def _run(args, cwd, timeout=_TIMEOUT_S):
    """One git subprocess, fail-open: any error, non-zero exit or timeout returns None."""
    if not args or args[0] is None:
        return None
    try:
        result = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


def _resolve_repo(cwd):
    """The cache key for this call's cwd: its real path, no subprocess.

    Every later git call runs ``cwd=`` there, so a worktree or a subdirectory
    resolves its repo through the shared common config without a separate
    ``rev-parse --show-toplevel`` round trip. Unresolvable (no cwd) is None.
    """
    if not cwd:
        return None
    try:
        return os.path.realpath(cwd)
    except (OSError, ValueError):
        return None


def _configs(repo):
    """{lowercased key: value} for the origin url and the signing keys, or {} on failure."""
    out = _run([_GIT, "config", "--get-regexp", _CONFIG_KEYS], repo)
    found = {}
    if out:
        for line in out.splitlines():
            key, _, value = line.partition(" ")
            found[key.lower()] = value.strip()
    if "remote.origin.url" not in found:
        out = _run([_GIT, "config", "--get", "remote.origin.url"], repo)
        if out:
            found["remote.origin.url"] = out.strip()
    return found


def _origin_host_is_github(url):
    """github.com over any transport (https, git@, ssh://); Azure and others out."""
    if not url:
        return False
    if url.startswith("git@github.com:") or url.startswith("ssh://git@github.com/"):
        return True
    if "github.com" not in url:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    return host in _GITHUB_HOSTS


def _default_branch(repo):
    """The remote default branch: origin/HEAD's symref target, else None (fail open)."""
    out = _run([_GIT, "symbolic-ref", "refs/remotes/origin/HEAD"], repo)
    if out and out.strip().startswith("refs/remotes/origin/"):
        return out.strip()[len("refs/remotes/origin/"):]
    out = _run([_GIT, "for-each-ref", "--format=%(symref:short)", "refs/remotes/origin/HEAD"], repo)
    if out:
        line = out.strip()
        if line.startswith("origin/") and not line.endswith("/HEAD"):
            return line[len("origin/"):]
    return None


def _signed_share(repo):
    """[0..1] share of the last _HISTORY_DEPTH commits on origin/<default> with a gpgsig header, or None.

    ``--pretty=raw`` prints each commit's headers verbatim: exactly one
    ``gpgsig `` line per signed commit at column 0 (message lines carry a
    four-space indent), so the column-0 count matches the card's validated grep
    recipe and cannot be inflated by message text.
    """
    branch = _default_branch(repo)
    if not branch:
        return None
    raw = _run([_GIT, "log", "-n", str(_HISTORY_DEPTH), "--pretty=raw", "origin/" + branch], repo)
    if raw is None:
        return None
    total = 0
    signed = 0
    for line in raw.splitlines():
        if line.startswith("commit "):
            total += 1
        elif line.startswith("gpgsig "):
            signed += 1
    if total <= 0:
        return None
    return signed / total


def requires_signing(repo):
    """Per-repo verdict with a 10-minute cache: github origin AND >=90% signed history."""
    now = _now()
    cached = _VERDICT_CACHE.get(repo)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    verdict = False
    try:
        url = _configs(repo).get("remote.origin.url")
        if url and _origin_host_is_github(url):
            share = _signed_share(repo)
            verdict = share is not None and share >= _SIGNED_SHARE_THRESHOLD
    except Exception:
        verdict = False
    _VERDICT_CACHE[repo] = (now, verdict)
    return verdict


def reset_cache() -> None:
    """Test seam: forget every cached verdict."""
    _VERDICT_CACHE.clear()


def _corrected(command):
    """The same command with `-S` inserted right after the `git commit` words."""
    match = _COMMIT.search(command)
    if not match:
        return None
    return command[: match.end()] + " -S" + command[match.end():]


def check(tool_name: str, args: Any) -> Optional[str]:
    """The block message for an unsigned `git commit` in a signing-required repo, or `None`."""
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    command = args.get("command")
    if not isinstance(command, str) or "commit" not in command:
        return None
    blanked = terminal_policy._QUOTED.sub("''", hermes_home_gate._strip_heredocs(command))
    if not _COMMIT.search(blanked):
        return None
    try:
        workdir = str(args.get("workdir") or "").strip()
        repo = _resolve_repo(workdir or os.getcwd())
        if not repo or not requires_signing(repo):
            return None
        if _SIGN_ARG.search(blanked) or _UNSET_ARG.search(blanked) or _CONFIG_TRUE_ARG.search(blanked):
            return None
        configs = _configs(repo)
        if configs.get("commit.gpgsign", "").lower() in _TRUE_WORDS:
            return None
        corrected = _corrected(command)
        if corrected is None:
            return None
        fmt = configs.get("gpg.format", "") or "ssh"
        key = configs.get("user.signingkey", "") or "<your-signing-key>"
        return (
            "agent-guards blocked terminal: an unsigned `git commit` in a repo that requires "
            "signed commits (the recent history on origin is fully signed, a push would be "
            "rejected). Run the exact command signed: " + corrected + ". To make every future "
            "commit signed without the flag, run inside the repo: git config gpg.format " + fmt
            + " and git config user.signingkey " + key + " and git config commit.gpgsign true."
        )
    except Exception:
        return None
