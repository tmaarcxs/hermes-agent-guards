"""Block a review request whose claimed commit is not the head of its pull request.

Active only in worker setups (``kanban.default_assignee`` set; see ``kanban_policy``) and only
in a worker process (``HERMES_KANBAN_TASK`` set). When ``kanban_request_review`` names a GitHub
pull request URL (in the summary or metadata) and a commit (``metadata.commit`` or
``metadata.head``), the pull request's live head must match that commit. Otherwise the reviewer
freezes a head that does not contain the claimed work, and the round is wasted. Fails open when
either value is missing or GitHub cannot be read.
"""

import json
import os
import re
import subprocess
from typing import Any, Callable, Optional

from . import kanban_policy

_PR_URL = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
MESSAGE = (
    "agent-guards blocked kanban_request_review: you claim commit {claimed} but PR {pr} has head {head} on GitHub. "
    "Push the commit (git push, then confirm with gh pr view --json headRefOid) and wait for exact-head CI on it, "
    "or correct metadata.commit to the published head. "
)


def _live_head(owner: str, repo: str, number: str) -> Optional[str]:
    out = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/pulls/{number}", "-q", ".head.sha"],
        capture_output=True, text=True, timeout=15,
    )
    return out.stdout.strip() or None if out.returncode == 0 else None


def claimed(args: dict) -> tuple[Optional[tuple[str, ...]], Optional[str]]:
    metadata = args.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    text = " ".join([str(args.get("summary") or ""), json.dumps(metadata, default=str)])
    pr = _PR_URL.search(text)
    sha = str(metadata.get("commit") or metadata.get("head") or "").strip()
    return (pr.groups() if pr else None), (sha if _SHA.match(sha) else None)


def check(tool_name: str, args: Any, live_head: Callable[[str, str, str], Optional[str]] = _live_head) -> Optional[str]:
    if tool_name != "kanban_request_review" or not isinstance(args, dict) or not os.environ.get("HERMES_KANBAN_TASK"):
        return None
    if not kanban_policy.configured_worker():
        return None
    pr, sha = claimed(args)
    if not pr or not sha:
        return None
    head = live_head(*pr)
    if not head or head.lower().startswith(sha.lower()):
        return None
    return MESSAGE.format(claimed=sha, pr=f"{pr[0]}/{pr[1]}#{pr[2]}", head=head[:12]) + kanban_policy.RETRY_INSTRUCTION
