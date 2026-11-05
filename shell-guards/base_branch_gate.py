"""Block a wrong-base PR or a branch started at a long-lived branch of a different name.

Workers repeatedly opened PRs or started branches against ``develop`` while the
repo's real default branch is ``main`` (the two largest product repos are
trunk-based; ``develop`` is frozen and never deploys) — and the audit-automation
frontend repo is the mirror case: its default branch IS ``develop``. The
default branch therefore comes from the repository itself, never from a
hard-coded name: ``git symbolic-ref --short refs/remotes/origin/HEAD``, which
``git fetch origin`` maintains locally. No network on the hot path; when the
symref is absent the gate fails open (it does not call ``ls-remote``).

Blocked shapes
- ``gh pr create`` with an explicit ``--base`` that is NOT the repo's default
  branch but IS a long-lived integration name (``main``/``master``/``develop``/
  ``trunk``/``dev``): the classic wrong-trunk PR. A feature or release base
  (stacked PR) passes; an omitted base passes.
- ``az repos pr create`` / ``az devops pr create`` with ``--target-branch``/``-t``
  under the same rule.
- a new branch started AT a long-lived branch of a different name
  (``git checkout -b x origin/develop``, ``git switch -c x develop``,
  ``git worktree add p -b x origin/develop``, ``git branch x develop``):
  the future PR would land on the wrong trunk. A start at the repo's own
  default branch, at a feature/release branch, or at no explicit start passes.

A promotion PR passes: when the head (``--head``/``-H``, the corresponding
Azure ``--source-branch``/``-s``, or, absent a flag, the branch checked out in
the call's repo) is the repo's default branch, the PR only syncs the default
into a long branch (develop into main for the Gitflow-to-trunk migration) and
is exactly what the rule wants to allow. A head that is not the default is
still blocked, even when it equals the proposed base: no base rewrite could
make that call pass, so the message then names the passing shape instead of a
command.

The repo is resolved from the call's real cwd: the ``workdir`` argument, then a
``cd`` inside the command (it moves the cwd for the statements after it), then
``git -C`` per git statement; for ``gh`` calls with ``--repo``/``-R`` the named
repo is matched against the cwd's own repo or a directory beside it, and a repo
with no local clone beside the cwd fails open. The correction in the block
message swaps only the flagged branch value, so quoted ``--title``/``--body``
text comes back byte-identical, and the printed command passes this gate when
run as printed. Every parse surprise, missing tool, subprocess timeout or
unexpected error fails open without blocking.
"""

import re
import subprocess
from pathlib import Path
from typing import Optional

from . import hermes_home_gate, terminal_policy

HOOK = "pre_tool_call"

INTEGRATION = frozenset({"main", "master", "develop", "trunk", "dev"})

_START = terminal_policy._START

_CANDIDATE = re.compile(r"pr\s+create\b|\bcheckout\b|\bswitch\b|\bworktree\b|\bbranch\b")
_STATEMENTS = re.compile(r"&&|[;&|\n(]")
_CD = re.compile(
    _START
    + r"cd\s+(?P<dir>\x01[^\x01]*\x01|\x02[^\x02]*\x02|[^\s;&|]+)"
)
_SENTINELS = ("\x01", "\x02")
_MASK = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|\$\([^()]*\)")
_GIT = re.compile(_START + r"git\s+(?P<rest>[^\n;&|]*)")
_GH_PR_CREATE = re.compile(r"gh\s+(?:\S+\s+){0,6}pr\s+create\b")
_AZ_PR_CREATE = re.compile(r"az\s+(?:\S+\s+){0,6}pr\s+create\b")
_GH_REPO = re.compile(r"(?:^|\s)(?:--repo|-R)(?:=|\s+)(?P<repo>[^\s;&|]+)")
_GH_BASE = re.compile(r"(?:^|\s)--base(?:=|\s+)(?P<branch>[^\s;&|]+)")
_GH_HEAD = re.compile(r"(?:^|\s)(?:--head|-H)(?:=|\s+)(?P<branch>[^\s;&|]+)")
_AZ_TARGET = re.compile(r"(?:^|\s)(?:--target-branch|-t)(?:=|\s+)(?P<branch>[^\s;&|]+)")
_AZ_SOURCE = re.compile(r"(?:^|\s)(?:--source-branch|-s)(?:=|\s+)(?P<branch>[^\s;&|]+)")

_GH_MESSAGE = (
    "agent-guards blocked terminal: `gh pr create --base {base}` sends this pull request to "
    "integration branch '{base}' while the repo's real default branch is '{default}' (integration "
    "branches other than the default never deploy; a stacked PR on a feature or release branch "
    "stays allowed). {fix}"
)

_AZ_MESSAGE = (
    "agent-guards blocked terminal: `az repos pr create --target-branch {base}` sends this pull "
    "request to integration branch '{base}' while the repo's real default branch is '{default}' "
    "(integration branches other than the default never deploy). {fix}"
)

_BRANCH_MESSAGE = (
    "agent-guards blocked terminal: starting branch at integration branch '{base}' while "
    "the repo's real default branch is '{default}' points the future pull request at the wrong "
    "trunk. {fix}"
)


def default_branch(repo_dir: str) -> Optional[str]:
    """The repo's default branch from the local remote symref, or ``None`` (never the network)."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip().rstrip("/").split("/")[-1]
    return name or None


def _is_integration_ref(ref: str) -> bool:
    """``main``/``master``/... bare, or spelled ``origin/<integration>``."""
    if "/" in ref:
        remote, _, name = ref.partition("/")
        return remote == "origin" and name in INTEGRATION
    return ref in INTEGRATION


def _quoted_spans(raw: str) -> list:
    """Spans of the raw command that are data: quoted text and ``$(...)`` bodies."""
    return [m.span() for m in _MASK.finditer(raw)]


def _swap_flag(raw: str, pattern, default: str) -> Optional[str]:
    """``raw`` with the last flagged branch value replaced; quoted title/body text stays byte-identical.

    shortcut: the last match wins, so a command holding two wrong-base statements has only the
    second corrected; the first is blocked with its own message anyway.
    """
    spans = _quoted_spans(raw)
    target = None
    for match in pattern.finditer(raw):
        if not any(start <= match.start("branch") < end for start, end in spans):
            target = match
    if target is None:
        return None
    return raw[: target.start("branch")] + default + raw[target.end("branch") :]


def _swap_token(raw: str, token: str, default: str) -> Optional[str]:
    """``raw`` with the last occurrence of ``token`` outside quoted data replaced."""
    spans = _quoted_spans(raw)
    at = raw.rfind(token)
    while at >= 0 and any(start <= at < end for start, end in spans):
        at = raw.rfind(token, 0, at)
    return None if at < 0 else raw[:at] + default + raw[at + len(token) :]


def _current_head(repo_dir: str) -> Optional[str]:
    """The checked-out branch, the head ``gh``/``az`` use when no flag is given.

    Only when the branch exists on the remote (``refs/remotes/origin/<branch>``):
    a branch never pushed cannot be a pull-request head, and treating an
    unpushed local branch as one would pass a wrong-base PR.
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_dir, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()
        pushed = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    return branch if branch and pushed.returncode == 0 else None


def _message(template: str, base: str, default: str, corrected: Optional[str]) -> str:
    """The block text: the corrected command, or the passing call shape when none exists."""
    if corrected:
        fix = f"Run instead: {corrected}"
    else:
        fix = (
            "No single edit of this command passes; open the pull request from the branch that "
            f"holds the work: `gh pr create --base {default} --head <branch>` (Azure: "
            f"`az repos pr create --target-branch {default} --source-branch <branch>`)."
        )
    return template.format(base=_branch_name(base), default=default, fix=fix)


def _branch_name(ref: str) -> str:
    return ref.split("/")[-1]


def _mask(command: str) -> str:
    """Quoted spans and ``$(...)`` bodies wrapped in sentinels, content kept.

    A substitution body such as ``GH_TOKEN=$(gh auth token -u x) gh pr create``
    must not split into "statements" at the parentheses; the wrapped body also
    keeps its ``pr create``/-looking text out of the statement parsers.
    """
    def opener(match):
        token = match.group(0)
        if token.startswith("$("):
            return "$" + _SENTINELS[1] + _SENTINELS[0] + token[2:-1] + _SENTINELS[0] + _SENTINELS[1]
        return (_SENTINELS[0] if token[0] == "'" else _SENTINELS[1]) + token[1:-1] + (_SENTINELS[0] if token[0] == "'" else _SENTINELS[1])
    return _MASK.sub(opener, command)


def _token(masked: str) -> str:
    """The literal token: sentinel pairs stripped."""
    return masked.strip("".join(_SENTINELS))


def _strip_masked(masked: str) -> str:
    """Quoted strings and ``$(...)`` bodies removed (they are data or env setup),
    sentinel characters dropped: what remains parses as command words."""
    return masked.replace(_SENTINELS[0], "").replace(_SENTINELS[1], "")


def _cd_target(cwd: Optional[str], raw: str) -> Optional[str]:
    """The cwd the statements after a ``cd`` run in, or ``None`` when it cannot be resolved."""
    raw = _token(raw.strip())
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        if cwd is None:
            return None
        path = Path(cwd) / path
    return str(path) if path.is_dir() else None


def _walk_git(tokens: list) -> Optional[dict]:
    """One ``git`` statement's shape: verb, new-branch flag, cwd override, start-point candidate.

    ``positions`` collects the non-flag tokens after the verb, skipping the value of a
    consuming flag, so a creation statement's start point is the position the man page
    puts it at: ``checkout -b <name> <start>``, ``switch -c <name> <start>``,
    ``branch <name> <start>``, ``worktree add <path> -b <name> <start>``.
    """
    verb = None
    positions = []
    consumed = set()
    git_c = None
    has_new = False
    dashdash = 0
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--":
            dashdash += 1
            i += 1
            continue
        if dashdash:
            i += 1
            continue
        if token.startswith("-") and token != "-" and "=" not in token:
            i += 1
            if token in ("-C", "-c") and verb is None:
                if i < len(tokens):
                    git_c = git_c or tokens[i]
                    consumed.add(i)
                    i += 1
            elif token == "-b" and verb in ("checkout", "worktree"):
                has_new = True
                if i < len(tokens):
                    consumed.add(i)
                    i += 1
            elif token == "-c" and verb == "switch":
                has_new = True
            continue
        if verb is None:
            verb = token
            i += 1
            continue
        if i not in consumed:
            positions.append(token)
        i += 1
    return {"verb": verb, "positions": positions, "has_new": has_new, "git_c": git_c}


def _branch_start(statement_shape: dict) -> Optional[str]:
    """The explicit start-point ref of a branch-creation statement, or ``None``."""
    verb = statement_shape["verb"]
    positions = statement_shape["positions"]
    has_new = statement_shape["has_new"]
    if verb == "checkout":
        return positions[0] if has_new and positions else None
    if verb == "switch":
        return positions[1] if has_new and len(positions) >= 2 else None
    if verb == "branch":
        return positions[1] if len(positions) >= 2 else None
    if verb == "worktree":
        if not positions or positions[0] != "add":
            return None
        return positions[2] if has_new and len(positions) >= 3 else None
    return None


def _gh_repo_dir(statement: str, cwd: Optional[str]) -> Optional[str]:
    """The local clone a ``gh`` statement targets: the cwd repo, or a matching dir beside it."""
    named = _GH_REPO.search(statement)
    if named is None:
        return cwd
    leaf = named.group("repo").rstrip("/").split("/")[-1]
    if not cwd:
        return None
    if Path(cwd).name == leaf:
        return cwd
    beside = Path(cwd) / leaf
    return str(beside) if beside.is_dir() else None


def _git_statement_block(statement: str, cwd: Optional[str], raw: str) -> Optional[str]:
    match = _GIT.match(statement)
    if match is None:
        return None
    shape = _walk_git(match.group("rest").split())
    start = _branch_start(shape)
    if start is None or not _is_integration_ref(start):
        return None
    repo_dir = shape["git_c"] or cwd
    default = default_branch(repo_dir) if repo_dir else None
    if default is None or _branch_name(start) == default:
        return None
    return _message(_BRANCH_MESSAGE, start, default, _swap_token(raw, start, default))


def _gh_statement_block(statement: str, cwd: Optional[str], raw: str) -> Optional[str]:
    if _GH_PR_CREATE.search(statement) is None:
        return None
    repo_dir = _gh_repo_dir(statement, cwd)
    default = default_branch(repo_dir) if repo_dir else None
    if default is None:
        return None
    bases = _GH_BASE.findall(statement)
    if not bases:
        return None
    base = bases[-1]
    if _branch_name(base) not in INTEGRATION or base == default:
        return None
    heads = _GH_HEAD.findall(statement)
    head = _branch_name(heads[-1]) if heads else (_current_head(repo_dir) if repo_dir else None)
    if head == default:
        return None
    corrected = None if head == _branch_name(base) else _swap_flag(raw, _GH_BASE, default)
    return _message(_GH_MESSAGE, base, default, corrected)


def _az_statement_block(statement: str, cwd: Optional[str], raw: str) -> Optional[str]:
    if _AZ_PR_CREATE.search(statement) is None:
        return None
    default = default_branch(cwd) if cwd else None
    if default is None:
        return None
    targets = _AZ_TARGET.findall(statement)
    if not targets:
        return None
    target = targets[-1]
    if _branch_name(target) not in INTEGRATION or target == default:
        return None
    sources = _AZ_SOURCE.findall(statement)
    source = _branch_name(sources[-1]) if sources else None
    if source == default:
        return None
    corrected = None if source == _branch_name(target) else _swap_flag(raw, _AZ_TARGET, default)
    return _message(_AZ_MESSAGE, target, default, corrected)


def _statement_block(statement: str, cwd: Optional[str], raw: str) -> Optional[str]:
    """``statement`` is the stripped statement; ``raw`` the whole command, whose quoted text is data."""
    statement = statement.strip()
    if not statement or _CD.match(statement):
        return None
    if _GH_PR_CREATE.search(statement) is not None:
        return _gh_statement_block(statement, cwd, raw)
    if _AZ_PR_CREATE.search(statement) is not None:
        return _az_statement_block(statement, cwd, raw)
    return _git_statement_block(statement, cwd, raw)


def _check(command: str, workdir) -> Optional[str]:
    if not command.strip():
        return None
    raw = hermes_home_gate._strip_heredocs(command)
    masked = _mask(raw)
    if not _CANDIDATE.search(_strip_masked(masked)):
        return None
    cwd = str(workdir) if workdir else None
    for statement in _STATEMENTS.split(masked):
        lead = statement.strip()
        if lead.startswith("cd"):
            cd = _CD.match(lead)
            if cd:
                cwd = _cd_target(cwd, cd.group("dir"))
                continue
        blocked = _statement_block(_strip_masked(statement), cwd, raw)
        if blocked:
            return blocked
    return None


def check(tool_name: str, args) -> Optional[str]:
    """``pre_tool_call``: the block message for a wrong-base PR or branch start, else ``None``."""
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    try:
        return _check(str(args.get("command") or ""), args.get("workdir"))
    except Exception:
        return None
