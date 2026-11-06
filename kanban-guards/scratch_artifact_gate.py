"""Block a scratch-workspace handoff whose deliverable exists only inside the workspace.

The kernel deletes a card's scratch workspace on completion (``_cleanup_workspace``); a
deliverable that lives only there is destroyed with it. Active on ``kanban_complete`` and
``kanban_request_review`` (worker setups only, see ``kanban_policy``) for cards whose
``workspace_kind`` is ``scratch``. Two conditions block:

(a) the handoff text (summary, result, metadata values, nested) names an absolute path
inside the card's scratch workspace that ``metadata.artifacts`` does not cover — covered
means the exact path, or an artifact that is that path or contains it (a directory or
archive entry in ``artifacts`` covers the paths under it);

(b) a git repository inside the scratch workspace has commits not reachable from any
remote-tracking ref while ``artifacts`` holds no ``.bundle``/``.patch`` file — the commits
die with ``rmtree`` unless they travel as a bundle or patch list.

The block message names every uncovered path (or the branch and head SHA of an unpushed
repo) and the exact fixing call, and states that the scratch workspace is deleted on
completion. Fails open on any error, and on tasks whose workspace is not scratch.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from . import kanban_policy
from .kanban_policy import RETRY_INSTRUCTION

HOOK_TOOLS = ("kanban_complete", "kanban_request_review")

_ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tar.xz", ".txz", ".zip", ".tar.zst")

_FIX = (
    "pass the absolute paths in metadata.artifacts"
    " (an artifact that is the path or a directory/archive containing it covers it;"
    " for commits: git bundle create <abs>.bundle <branch>, or git format-patch for the commits)"
    " — the scratch workspace is deleted on completion."
)

_MESSAGE = (
    "agent-guards blocked {tool}: card {task_id}'s scratch workspace is deleted on completion, and the "
    "handoff names a deliverable that would be lost with it: {detail}. " + _FIX + " " + RETRY_INSTRUCTION
)


def _task(task_id: str) -> Optional[dict]:
    """``{"workspace_kind", "workspace_path"}`` for the task, ``None`` if absent."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=None)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            return None
        return {"workspace_kind": task.workspace_kind, "workspace_path": task.workspace_path}
    finally:
        conn.close()


def _metadata(args: Any) -> dict:
    metadata = args.get("metadata") if isinstance(args, dict) else None
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = None
    return metadata if isinstance(metadata, dict) else {}


def _handoff_texts(args: Any) -> list[str]:
    texts = []
    for key in ("summary", "result"):
        value = (args or {}).get(key)
        if isinstance(value, str):
            texts.append(value)
    stack = [args.get("metadata") if isinstance(args, dict) else None]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return [text for text in texts if text]


def _artifact_paths(metadata: dict) -> list[Path]:
    raw = metadata.get("artifacts")
    if not isinstance(raw, (list, tuple)):
        return []
    paths = []
    for item in raw:
        if not str(item or "").strip():
            continue
        path = Path(str(item)).expanduser()
        try:
            paths.append(path.resolve())
        except OSError:
            paths.append(path)
    return paths


def _covered(path: Path, workspace: Path, artifacts: list[Path]) -> bool:
    """Whether an artifact is the path, or contains it (a directory artifact whose
    tree holds it, or an archive artifact whose members include the path relative
    to the workspace, with or without a leading ``./``)."""
    for artifact in artifacts:
        if artifact == path:
            return True
        try:
            if artifact.is_dir() and path.is_relative_to(artifact):
                return True
        except OSError:
            pass
        if _archive_suffix(artifact) and _archive_member(artifact, path, workspace):
            return True
    return False


def _archive_suffix(artifact: Path) -> Optional[str]:
    for suffix in _ARCHIVE_SUFFIXES:
        if str(artifact).lower().endswith(suffix):
            return suffix
    return None


def _archive_member(artifact: Path, path: Path, workspace: Path) -> bool:
    """Whether the archive lists ``path`` (relative to ``workspace``) as a member."""
    suffix = _archive_suffix(artifact)
    if not suffix:
        return False
    try:
        relative = str(path.relative_to(workspace))
        stem = str(artifact)[: -len(suffix)]
        if path.is_relative_to(Path(stem)):
            relative = str(path.relative_to(Path(stem)))
        import tarfile
        import zipfile
        if suffix in (".zip",):
            with zipfile.ZipFile(artifact) as archive:
                names = archive.namelist()
        elif suffix in (".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tar.xz", ".txz", ".tar", ".tar.zst"):
            with tarfile.open(artifact) as archive:
                names = archive.getnames()
        else:
            return False
        stripped = {name.lstrip("./") for name in names}
        return relative in stripped or any(name.endswith("/" + relative) for name in names)
    except Exception:
        return False


_PATH_RUN = r"[^\s`\"'<>|;*?\[\]!$&(),:]+"
_PATH_TOKEN = re.compile(r"(?<![\w.-])(/(?:%s/)*(?:%s/)*%s)" % (_PATH_RUN, _PATH_RUN, _PATH_RUN))


def _candidate_paths(text: str) -> list[str]:
    """Absolute-path-looking tokens from a handoff text, separators normalized."""
    found = []
    for match in _PATH_TOKEN.finditer(text):
        raw = match.group(1)
        raw = raw.rstrip(".,;:!?)]}\"'")
        if raw.endswith("."):
            raw = raw[:-1]
        found.append(raw)
    for match in re.finditer(r"(?<![\w.])~/(?:%s/)*%s" % (_PATH_RUN, _PATH_RUN), text):
        found.append(os.path.expanduser(match.group(0).rstrip(".,;:!?)]}\"'")))
    return found


def _git(repo: Path, *args: str, timeout: int = 30) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _unpushed_heads(workspace: Path) -> list[tuple[str, str]]:
    """``(branch, head_sha)`` for repos under ``workspace`` whose commits are not
    reachable from any remote-tracking ref.

    Two git processes per repo: ``rev-list --branches --not --remotes`` names every
    commit no remote ref reaches, and heads whose sha is in that set are exactly the
    branches the old per-branch ``merge-base --is-ancestor`` loop flagged — with 186
    local branches × 31 remote refs that loop spawned ~1550 subprocesses per repo
    (~42s), blowing the 30s ``pre_tool_call`` hook cap and making the host skip the
    hook for every tool call for a minute (#timeout suppression)."""
    heads = []
    try:
        candidates = sorted(set(p.parent for p in workspace.rglob(".git")))
    except OSError:
        return []
    seen = set()
    for candidate in candidates:
        if candidate in seen or not candidate.is_dir():
            continue
        seen.add(candidate)
        unpushed = set((_git(candidate, "rev-list", "--branches", "--not", "--remotes") or "").split())
        if not unpushed:
            continue
        branches = _git(candidate, "for-each-ref", "--format=%(refname:short) %(objectname)",
                        "refs/heads", "--count=50")
        for line in (branches or "").splitlines():
            branch, _, sha = line.partition(" ")
            if sha and sha in unpushed:
                heads.append((branch, sha))
    return heads


def _remote_refs(repo: Path) -> list[str]:
    out = _git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes")
    return [line for line in (out or "").splitlines() if line]


def _scan_scratch_repos(workspace: Path) -> list[tuple[str, str]]:
    return _unpushed_heads(workspace)


def check(tool_name: str, args: Any, load_task: Optional[Callable] = None) -> Optional[str]:
    """``check`` for the ``pre_tool_call`` chain; block message or ``None``."""
    if tool_name not in HOOK_TOOLS or not isinstance(args, dict) or not kanban_policy.configured_worker():
        return None
    task_id = str(args.get("task_id") or os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id:
        return None
    try:
        task = (load_task or _task)(task_id)
        if task is None or task.get("workspace_kind") != "scratch" or not task.get("workspace_path"):
            return None
        workspace = Path(task["workspace_path"]).expanduser()
        if not workspace.is_dir():
            return None
        return _check_task(task_id, tool_name, workspace, _metadata(args), _handoff_texts(args))
    except Exception:
        return None


def _check_task(task_id: str, tool_name: str, workspace: Path, metadata: dict, texts: list[str]) -> Optional[str]:
    artifacts = _artifact_paths(metadata)
    uncovered = _uncovered_paths(texts, workspace, artifacts)
    if uncovered:
        return _message(tool_name, task_id, "missing from metadata.artifacts: " + ", ".join(uncovered))
    if not any(str(path).lower().endswith((".bundle", ".patch")) for path in artifacts):
        heads = _scan_scratch_repos(workspace)
        if heads:
            detail = "; ".join(f"branch {branch} at {sha}" for branch, sha in heads[:5])
            return _message(tool_name, task_id, "unpushed commits " + detail)
    return None


def _message(tool_name: str, task_id: str, detail: str) -> str:
    return _MESSAGE.format(tool=tool_name, task_id=task_id, detail=detail)


def _uncovered_paths(texts: list[str], workspace: Path, artifacts: list[Path]) -> list[str]:
    prefix = str(workspace.resolve())
    uncovered = []
    for text in texts:
        for raw in _candidate_paths(text):
            try:
                normalized = str(Path(raw).resolve())
            except OSError:
                normalized = raw
            if not normalized.startswith(prefix + os.sep):
                continue
            path = Path(normalized)
            if not _covered(path, workspace, artifacts) and normalized not in uncovered:
                uncovered.append(normalized)
    return uncovered
