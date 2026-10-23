#!/usr/bin/env python3
"""Read-only dump of one finished worker session, for a task retrospective.

Resolves the worker session for a kanban task (state.db ``source='kanban'``), prints a compact
timeline, and emits deterministic failure signals: failing tool results, duplicated tool calls
(same tool + same arguments), and the terminal kanban lifecycle call. Never writes anything.

Usage:
    python3 session_dump.py --task-id t_abc123
    python3 session_dump.py --session-id 20261021_124741_5f0460 --timeline-limit 200

Exit 0 on success, 2 when the session cannot be resolved (say so, do not guess).
"""
from __future__ import annotations

import argparse
import atexit
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_STATE_DB = "~/.hermes/profiles/worker/state.db"
DEFAULT_KANBAN_DB = "~/.hermes/kanban.db"
FAILURE_MARKERS = (
    '"exit_code": 1', '"exit_code": 2', '"exit_code": 75', '"exit_code": 126',
    '"exit_code": 127', '"exit_code": 130', '"exit_code": 137', '"exit_code": 143',
    '"success": false', '"ok": false', "Traceback (most recent call last)",
    "command not found", "not_found", "Permission denied", "No such file or directory",
    "unrecognized arguments", "Error:", "ERROR:", "error:", "failed", "FAILED",
)
LIFECYCLE = ("kanban_complete", "kanban_request_review", "kanban_request_changes", "kanban_block")
_WS = re.compile(r"\s+")


_CLEANUP: list[Path] = []


def _cleanup() -> None:
    for d in _CLEANUP:
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup)


def _probe(conn: sqlite3.Connection) -> None:
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()


def _snapshot(path: Path) -> Path:
    """Copy a live SQLite database with any readable -wal/-shm sidecars into a temp dir.

    An unreadable sidecar is skipped rather than fatal: the copy is then read
    through the immutable fallback below.
    """
    d = Path(tempfile.mkdtemp(prefix="session_dump-"))
    _CLEANUP.append(d)
    dest = d / path.name
    shutil.copyfile(path, dest)
    for ext in ("-wal", "-shm"):
        side = path.with_name(path.name + ext)
        if side.exists():
            try:
                shutil.copyfile(side, dest.with_name(dest.name + ext))
            except OSError:
                pass
    return dest


def _open(path: Path, immutable: bool = False) -> sqlite3.Connection:
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    return sqlite3.connect(f"file:{path}?{query}", uri=True)


def connect(path: str) -> sqlite3.Connection:
    """Open read-only, tolerating a live WAL database on a host that rejects mode=ro.

    A live WAL database whose -wal/-shm sidecars are missing (or cannot be read
    in place) fails with sqlite error 14 (``unable to open database file``). The
    fallback snapshots the database and its sidecars into a temp dir and opens
    that copy read-only; when the sidecars are absent from the snapshot the copy
    is opened with ``immutable=1`` instead, which reads a WAL-header database
    without them. ``immutable=1`` ignores a populated -wal, so it is applied only
    to the snapshot, never to the live file. The live database is never written to.
    """
    live = Path(path).expanduser()
    conn = _open(live)
    try:
        _probe(conn)
        return conn
    except sqlite3.OperationalError:
        conn.close()
    snap = _snapshot(live)
    conn = _open(snap)
    try:
        _probe(conn)
        return conn
    except sqlite3.OperationalError:
        conn.close()
    return _open(snap, immutable=True)


def resolve_session(state: sqlite3.Connection, kanban: sqlite3.Connection, task_id: str) -> tuple[str | None, str]:
    rows = kanban.execute(
        "SELECT json_extract(metadata,'$.worker_session_id') FROM task_runs "
        "WHERE task_id=? AND metadata LIKE '%worker_session_id%' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchall()
    if rows and rows[0][0]:
        return rows[0][0], "kanban.task_runs.metadata.worker_session_id"
    needle = f"work kanban task {task_id}"
    rows = state.execute(
        "SELECT session_id FROM messages WHERE role='user' AND content LIKE ? ORDER BY id DESC LIMIT 1",
        (f"%{needle}%",),
    ).fetchall()
    if rows:
        return rows[0][0], "first user message"
    return None, "unresolved"


def call_args(raw: str | None) -> list[tuple[str, str]]:
    if not raw:
        return []
    try:
        calls = json.loads(raw)
    except (ValueError, TypeError):
        return [("<unparsed>", _WS.sub(" ", raw)[:200])]
    out = []
    for call in calls:
        fn = call.get("function", {}) if isinstance(call, dict) else {}
        name = fn.get("name", "<unknown>")
        try:
            args = json.loads(fn.get("arguments") or "{}")
            key = json.dumps(args, sort_keys=True)
        except (ValueError, TypeError):
            key = str(fn.get("arguments"))
        out.append((name, key))
    return out


def markers(content: str | None) -> list[str]:
    if not content:
        return []
    return [m for m in FAILURE_MARKERS if m in content]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task-id")
    ap.add_argument("--session-id")
    ap.add_argument("--state-db", default=DEFAULT_STATE_DB)
    ap.add_argument("--kanban-db", default=DEFAULT_KANBAN_DB)
    ap.add_argument("--timeline-limit", type=int, default=400, help="max timeline rows printed")
    ap.add_argument("--width", type=int, default=240, help="chars per timeline row")
    args = ap.parse_args()
    if not (args.task_id or args.session_id):
        ap.error("pass --task-id or --session-id")

    state, kanban = connect(args.state_db), connect(args.kanban_db)
    sid, how = (args.session_id, "explicit") if args.session_id else resolve_session(state, kanban, args.task_id)
    if not sid:
        print(json.dumps({"error": "session not resolved", "task_id": args.task_id,
                          "state_db": args.state_db}, indent=1))
        return 2

    meta = state.execute(
        "SELECT title, model, message_count, tool_call_count, started_at, ended_at FROM sessions WHERE id=?",
        (sid,),
    ).fetchone()
    rows = state.execute(
        "SELECT id, role, tool_name, content, tool_calls FROM messages WHERE session_id=? ORDER BY id",
        (sid,),
    ).fetchall()

    roles = Counter(r[1] for r in rows)
    calls: Counter = Counter()
    failures: list[dict] = []
    lifecycle: list[dict] = []
    for mid, role, tool_name, content, tool_calls in rows:
        for name, key in call_args(tool_calls):
            calls[name] += 1
            if name in LIFECYCLE:
                lifecycle.append({"message_id": mid, "call": name, "args": key[:400]})
        if role == "tool":
            found = markers(content)
            if found:
                failures.append({"message_id": mid, "tool": tool_name,
                                 "markers": found, "excerpt": _WS.sub(" ", content or "")[:300]})

    print("== session ==")
    print(json.dumps({"session_id": sid, "resolved_via": how, "task_id": args.task_id,
                      "title": meta[0] if meta else None, "model": meta[1] if meta else None,
                      "messages": len(rows), "roles": dict(roles), "calls": dict(calls)}, indent=1))
    print("\n== failure signals ==")
    for f in failures:
        print(json.dumps(f, ensure_ascii=False))
    print("\n== lifecycle calls ==")
    for c in lifecycle:
        print(json.dumps(c, ensure_ascii=False))
    print("\n== duplicated calls (same tool + same arguments) ==")
    seen: dict[tuple[str, str], list[int]] = defaultdict(list)
    for mid, role, tool_name, content, tool_calls in rows:
        for name, key in call_args(tool_calls):
            seen[(name, key)].append(mid)
    dups = {f"{n} {k[:160]}": ids for (n, k), ids in seen.items() if len(ids) > 1}
    for key, ids in sorted(dups.items(), key=lambda kv: -len(kv[1])):
        print(json.dumps({"call": key, "count": len(ids), "message_ids": ids}, ensure_ascii=False))
    if not dups:
        print("(none)")
    print("\n== timeline ==")
    for mid, role, tool_name, content, tool_calls in rows[: args.timeline_limit]:
        names = ",".join(n for n, _ in call_args(tool_calls)) or (tool_name or "")
        body = _WS.sub(" ", content or "")[: args.width]
        print(f"{mid} {role:9} {names:28} {body}")
    if len(rows) > args.timeline_limit:
        print(f"... {len(rows) - args.timeline_limit} more rows (raise --timeline-limit)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
