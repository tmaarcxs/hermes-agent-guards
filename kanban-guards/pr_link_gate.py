"""Block worker terminal calls that publish or merge a PR without its issue link.

Rule: every PR carries a GitHub Development link to its issue (``closingIssuesReferences``
non-empty) and exactly one closing keyword (``Closes #N``) in the body. ``Related to`` /
``Refs`` do not create the link.

Active only in a worker process (``HERMES_KANBAN_TASK`` set), like ``github_contact``:

- ``gh pr merge`` and ``gh api -X PUT .../pulls/N/merge``: blocked when the PR's
  ``closingIssuesReferences`` is empty or cannot be read (fail closed);
- ``gh pr create`` / ``gh pr edit`` with an inline or file body (``-b/--body``,
  ``-F/--body-file``): blocked unless the body has exactly one closing keyword.
  No body flag (editor, ``--fill``) or a stdin/unreadable file cannot be verified: passes.

Read-only commands never block. Fix text names the exact repair.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from . import github_contact as gc

_CLOSING = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:[\w.-]+/[\w.-]+)?#\d+\b")
_PULL_MERGE = re.compile(r"(?:^|/)repos/([^/]+/[^/]+)/pulls/(\d+)/merge/?$")
_BODY_FLAGS = {"-b": "text", "--body": "text", "-F": "file", "--body-file": "file"}
_REPO_FLAGS = {"-R": "repo", "--repo": "repo"}

FIX_BODY = "edit the PR body so it has exactly one closing keyword for its issue (`Closes #N.`)"
FIX_LINK = (
    "the PR has no Development link to its issue: edit the body to `Closes #N.` and re-read "
    "`closingIssuesReferences`; if still empty, link it with GraphQL "
    "`gh api graphql -f query='mutation($p:ID!,$i:ID!){addCloseIssueReferences(input:{pullRequestId:$p,issueIds:[$i]}){clientMutationId}}' "
    "-f p=<PR node id> -f i=<issue node id>` (never `Related to`/`Refs`; a partial delivery is "
    "reopened after merge, not unlinked)"
)


def _gh_json(args: list[str], cwd: str) -> Optional[Any]:
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30, cwd=cwd or None)
        return json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _linked(ref: str, repo: str, cwd: str) -> Optional[list]:
    """closingIssuesReferences of the PR, or None when it cannot be read."""
    args = ["pr", "view"] + ([ref] if ref else []) + (["-R", repo] if repo else []) + ["--json", "closingIssuesReferences"]
    data = _gh_json(args, cwd)
    if not isinstance(data, dict) or not isinstance(data.get("closingIssuesReferences"), list):
        return None
    return data["closingIssuesReferences"]


def _body(parts: list, cwd: str) -> Optional[str]:
    """The inline/file body of a create/edit command; None when absent or unverifiable."""
    for flag, values in gc._flags(parts, _BODY_FLAGS).items():
        value = values[-1]
        if value is None:
            continue
        if _BODY_FLAGS[flag] == "text":
            return value
        return gc._read(value, cwd)
    return None


def _positional(parts: list) -> str:
    """First non-flag token after ``gh pr merge`` (number, URL or branch); '' when absent."""
    value_flags = {"-R", "--repo", "-b", "--body", "-F", "--body-file", "-t", "--subject", "-A", "--author-email", "--match-head-commit"}
    i = 0
    while i < len(parts):
        text, quoted = parts[i]
        if not quoted and text.startswith("-") and len(text) > 1:
            i += 2 if text in value_flags else 1
            continue
        return text
    return ""


def _repo(parts: list) -> str:
    values = gc._flags(parts, _REPO_FLAGS)
    return next((v[-1] for v in values.values() if v and v[-1]), "")


def violations(command: str, workdir: str = "") -> list[str]:
    found: list[str] = []
    for tokens in gc._scan(command):
        parts = gc._unprefixed(tokens)
        if not parts or parts[0][1] or parts[0][0] != "gh":
            continue
        sub = parts[1][0] if len(parts) > 1 and not parts[1][1] else ""
        kind = parts[2][0] if len(parts) > 2 and not parts[2][1] else ""
        rest = parts[3:]
        if sub == "pr" and kind == "merge":
            linked = _linked(_positional(rest), _repo(rest), workdir)
            if not linked:
                found.append("gh pr merge on a PR with no readable Development link: " + FIX_LINK)
        elif sub == "pr" and kind in ("create", "edit"):
            body = _body(rest, workdir)
            if body is not None and len(_CLOSING.findall(body)) != 1:
                found.append(f"gh pr {kind} body without exactly one closing keyword: " + FIX_BODY)
        elif sub == "api":
            endpoint, skip = "", False
            for t, q in parts[2:]:
                if skip:
                    skip = False
                elif not q and t.startswith("-"):
                    skip = t in gc._API_VALUE_FLAGS
                else:
                    endpoint = t.lower()
                    break
            match = _PULL_MERGE.search(endpoint)
            method = "PUT" if any(
                parts[i][0] in ("-X", "--method") and parts[i + 1][0].upper() == "PUT" for i in range(2, len(parts) - 1)
            ) else ""
            if match and method:
                if not _linked(match.group(2), match.group(1), workdir):
                    found.append("merge through the API on a PR with no readable Development link: " + FIX_LINK)
    return found


def check(tool_name: str, args: Any) -> Optional[str]:
    if tool_name != "terminal" or not isinstance(args, dict) or not os.environ.get("HERMES_KANBAN_TASK"):
        return None
    found = violations(str(args.get("command") or ""), str(args.get("workdir") or ""))
    if not found:
        return None
    return "agent-guards blocked terminal: " + "; ".join(found) + ". " + gc.RETRY
