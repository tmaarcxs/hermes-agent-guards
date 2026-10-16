"""Block a worker from silently waiting on an approval-only pull-request gate.

Active only in worker setups (``kanban.default_assignee`` set; see ``kanban_policy``)
and only in a worker process (``HERMES_KANBAN_TASK`` set). The failure: a pull
request green on every automated check with ``mss=BLOCKED review=REVIEW_REQUIRED``
— the CODEOWNERS approval was the sole open gate — and the worker, barred from requesting
reviews by ``github_contact``, polled it for hours with background shell loops
(``for i in $(seq N); do gh pr checks|gh pr view --json statusCheckRollup; sleep; done``)
and heartbeats saying "CODEOWNERS approval is the sole remaining gate; long poller
continues", instead of merging via the authorized guarded path or blocking with
``kind=needs_input`` so the lead could act. A running card is never stale-checked,
so nobody was woken.

What this gate blocks (worker terminal calls):
- Any PR-readiness poll of a pull request whose exact head is currently green with the
  review approval as the only open gate: ``gh pr checks <n> -R o/r``,
  ``gh pr view <n> --json statusCheckRollup/mergeStateStatus/...``,
  ``gh api repos/o/r/pulls/<n>...`` reads, and direct poller helper scripts (``pr_status`` / ``pr_wait_checks``)
  invocations. A poll cannot improve that state; the block message carries the verdict
  and the two authorized moves.
- A repeated ``kanban_heartbeat`` note whose text admits the approval-only wait
  (first such note passes so the worker can time-stamp the situation; every later one
  is blocked — real note heartbeats are spaced minutes apart, so the second is the
  TTL (the module default) bounds repeated polls.

How the verdict is reached: one read-only ``gh api graphql`` round-trip per
(owner, repo, pr), TTL-cached for 15 minutes, mirroring the poll helper's exact-head
classifier (commits(last:1) rollup, pending=0, failing=0, unresolvedThreads=0,
state OPEN, not draft, MERGEABLE, mss BLOCKED, reviewDecision REVIEW_REQUIRED).
Genuinely pending checks, failing checks and draft/dirty states are not verdict names,
so their polls flow through untouched; errors fail open.

Not blocked on purpose: a documented merge command's own invocations (their internal
calls run inside the script's own process, invisible here), workflow/run targets
(``gh run watch``, ``gh run list``), and non-readiness reads of a PR.
"""

import os
import re
import subprocess
import time
from collections import OrderedDict
from typing import Any, Optional

from . import kanban_policy

TTL_SECONDS = 900  # ~15 min: how long a resolved PR state stays trusted

MESSAGE = (
    "agent-guards blocked {tool}: PR {prs} is green on every automated check and the only open gate is "
    "review/CODEOWNERS approval (REVIEW-ONLY) — waiting cannot change that, and no amount of polling "
    "will. Authorized moves: {moves}"
)

_MOVES_BLOCK = '`kanban_block(kind="needs_input")` now so the lead is notified — do not keep waiting.'


def _moves() -> str:
    """The sanctioned next steps, naming the documented merge command when the
    operator set one (``AGENT_GUARDS_MERGE_HINT``); otherwise the block advice only."""
    hint = kanban_policy.merge_hint()
    if hint:
        return (
            "(1) merge the exact green head you already verified with the documented merge command "
            f"({hint}); or (2) {_MOVES_BLOCK}"
        )
    return _MOVES_BLOCK

_WAIT_NOTE = re.compile(
    r"(?is)"
    r"\bcodeowners?\b"
    r"|\b(?:sole|only)\s+(?:remaining\s+)?gate\b"
    r"|\breview[ -]required\b"
    r"|\b(?:waiting|wait|polling|poller|poll)\w*\b[^.\n]{0,60}\b(?:approval|review|codeowners)\b"
    r"|\b(?:approval|review|unreviewed|unapproved|codeowners)\b[^.\n]{0,60}\b(?:waiting|wait|pending)\b"
)

_REPO = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
_REPO_FLAG = re.compile(rf"(?:-R|--repo|\bGH_REPO=)[ \t]*[\"']?({_REPO})")
_GH_CHECKS = re.compile(r"\bgh\s+pr\s+checks\s+(\d+)\b")
_READY_FIELDS = r"statusCheckRollup|mergeStateStatus|mergeable|reviewDecision|reviewRequests"
_GH_VIEW = re.compile(rf"\bgh\s+pr\s+view\s+(\d+)\b[^;&|\n]*--json\b[^;&|\n]*(?:{_READY_FIELDS})")
_GH_API = re.compile(rf"\bgh\s+api\s+[\"']?repos/({_REPO})/pulls/(\d+)\b")
_SCRIPT_NAMES = ("pr_status", "pr_wait_checks")
_SCRIPT = re.compile(r"(?:^|[\s/])(" + "|".join(_SCRIPT_NAMES) + r")\b")
_NUM = re.compile(r"\b[1-9]\d*\b")
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")

_verdicts: dict[tuple[str, str, str], tuple[float, bool]] = OrderedDict()
_waits: "OrderedDict[str, str]" = OrderedDict()
_MAX_SESSIONS = 200


def reset() -> None:
    _verdicts.clear()
    _waits.clear()


def readiness_polls(command: str) -> list[tuple[str, str, str]]:
    """(owner, repo, pr) targets named by readiness-poll parts of the command."""
    command = _QUOTED.sub(" '' ", command)
    repos = _REPO_FLAG.findall(command)
    found: list[tuple[str, str, str]] = []
    for number in _GH_CHECKS.findall(command):
        found.append(((repos[-1] if repos else ""), number, "checks"))
    for number in _GH_VIEW.findall(command):
        found.append(((repos[-1] if repos else ""), number, "view"))
    for repo, number in _GH_API.findall(command):
        found.append((repo, number, "api"))
    for _script in _SCRIPT.findall(command):
        if repos:
            numbers = _NUM.findall(command)
            if numbers:
                found.append((repos[-1], numbers[-1], "script"))
    out: list[tuple[str, str, str]] = []
    seen = set()
    for repo, number, _kind in found:
        if "/" not in repo:
            return []  # cannot resolve the repo: fail open on the whole command
        owner, name = repo.split("/", 1)
        key = (owner, name, number)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _pending_or_failing(contexts: list[dict]) -> tuple[list[str], list[str]]:
    pending, failing = [], []
    for ctx in contexts:
        name = str(ctx.get("name") or ctx.get("context") or "")
        if ctx.get("__typename") == "CheckRun":
            if ctx.get("status") != "COMPLETED":
                pending.append(name)
            elif ctx.get("conclusion") in ("FAILURE", "TIMED_OUT", "STARTUP_FAILURE"):
                failing.append(name)
        elif ctx.get("state") in ("PENDING", "EXPECTED"):
            pending.append(name)
        elif ctx.get("state") == "FAILURE":
            failing.append(name)
    return pending, failing


def approval_only_now(owner: str, repo: str, number: str, query=None) -> Optional[bool]:
    """True when the PR's exact head is green with review approval as the only open gate.

    False when the PR is pollable for a real reason (pending or failing checks, draft,
    merge conflicts, review threads). None = cannot tell (gh failing, network) — fail open.
    The result is cached for TTL_SECONDS, so repeated polls cost at most one round-trip
    per quarter hour and a state transition is re-read within ~15 minutes.
    """
    key = (owner, repo, number)
    hit = _verdicts.get(key)
    now = time.time()
    if hit and hit[0] > now:
        return hit[1]
    if query is None:
        query = _graphql
    try:
        verdict = query(owner, repo, number)
    except Exception:
        verdict = None
    if verdict is None:
        return None
    _verdicts[key] = (now + TTL_SECONDS, verdict)
    while len(_verdicts) > 200:
        _verdicts.popitem()
    return verdict


def _graphql(owner: str, repo: str, number: str) -> Optional[bool]:
    proc = subprocess.run(
        [
            "gh", "api", "graphql", "-f", "query=" + _QUERY,
            "-f", f"owner={owner}", "-f", f"name={repo}", "-F", f"pr={number}",
        ],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return None
    import json

    data = json.loads(proc.stdout or "{}")
    pr = ((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    if not pr:
        return None
    if pr.get("state") != "OPEN" or pr.get("isDraft") or pr.get("mergeable") != "MERGEABLE":
        return False
    if pr.get("mergeStateStatus") != "BLOCKED" or pr.get("reviewDecision") != "REVIEW_REQUIRED":
        return False
    if (pr.get("reviewThreads") or {}).get("totalCount"):
        return False
    commit = ((pr.get("commits") or {}).get("nodes") or [{}])[0].get("commit") or {}
    pending, failing = _pending_or_failing(((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or [])
    return not pending and not failing


_QUERY = """
query($owner:String!,$name:String!,$pr:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$pr){
      state isDraft mergeable mergeStateStatus reviewDecision
      reviewThreads(first:1){totalCount}
      commits(last:1){nodes{commit{oid statusCheckRollup{
        contexts(first:100){nodes{
          __typename
          ... on CheckRun {name status conclusion}
          ... on StatusContext {context state}
        }}
      }}}}
    }
  }
}
"""


def _heartbeat_check(args: Any, kwargs: dict) -> Optional[str]:
    note = str((args or {}).get("note") or "")
    if not _WAIT_NOTE.search(note):
        return None
    session = str(kwargs.get("session_id") or kwargs.get("task_id") or "")
    if not session:
        return None
    if session not in _waits:
        _waits[session] = note
        while len(_waits) > _MAX_SESSIONS:
            _waits.popitem(last=False)
        return None
    return MESSAGE.format(tool="kanban_heartbeat", prs="(see note)", moves=_moves())


def check(tool_name: str, args: Any, **kwargs: Any) -> Optional[str]:
    if not kanban_policy.configured_worker() or not os.environ.get("HERMES_KANBAN_TASK"):
        return None
    if tool_name == "kanban_heartbeat":
        return _heartbeat_check(args, kwargs)
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    command = str(args.get("command") or "")
    targets = readiness_polls(command)
    if not targets:
        return None
    blocked = [
        f"{owner}/{repo}#{number}"
        for owner, repo, number in targets
        if approval_only_now(owner, repo, number) is True
    ]
    if not blocked:
        return None
    return MESSAGE.format(tool="terminal", prs=", ".join(blocked), moves=_moves())
