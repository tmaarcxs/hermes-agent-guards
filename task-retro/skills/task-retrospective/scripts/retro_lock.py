#!/usr/bin/env python3
"""Advisory lockfile that serializes only the short skill/memory write step of a retro.

A retro analyses sessions freely in parallel; only the read-target -> patch -> readback window must
be exclusive across simultaneous retros. A POSIX ``flock`` cannot span several agent tool calls, so
this holds an exclusive *lockfile* with an owner and a TTL: acquired atomically (O_CREAT|O_EXCL),
taken over when the owner died or the TTL expired, and released explicitly.

Usage:
    python3 retro_lock.py acquire --owner t_4e88187e --wait 60
    python3 retro_lock.py release --owner t_4e88187e
    python3 retro_lock.py status

Exit codes: 0 acquired/released/free; 75 still held after the bounded wait -> the caller retries the
write, it never skips the finding; 2 bad usage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_LOCK = "~/.hermes/cache/retro-writes.lock"
DEFAULT_TTL = 300.0


def read_meta(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def try_acquire(path: Path, owner: str, ttl: float) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"owner": owner, "pid": os.getpid(), "expires_at": time.time() + ttl})
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        meta = read_meta(path)
        expires = float(meta.get("expires_at") or 0)
        if expires and expires > time.time():
            return False
        # stale: owner died or TTL elapsed -> take over atomically
        try:
            path.unlink()
        except OSError:
            return False
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("acquire", "release", "status"))
    ap.add_argument("--owner", default="")
    ap.add_argument("--lock", default=DEFAULT_LOCK)
    ap.add_argument("--wait", type=float, default=60.0, help="bounded wait in seconds")
    ap.add_argument("--ttl", type=float, default=DEFAULT_TTL, help="lock lifetime; a later acquire may take it over")
    args = ap.parse_args()
    path = Path(args.lock).expanduser()

    if args.action == "release":
        meta = read_meta(path)
        if args.owner and meta.get("owner") not in (None, args.owner):
            print(json.dumps({"released": False, "reason": "not owner", "holder": meta}, indent=1))
            return 75
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        print(json.dumps({"released": True, "lock": str(path)}, indent=1))
        return 0

    if args.action == "status":
        meta = read_meta(path)
        held = bool(meta.get("expires_at", 0) > time.time())
        print(json.dumps({"held": held, "holder": meta, "lock": str(path)}, indent=1))
        return 0

    if not args.owner:
        ap.error("acquire needs --owner")
    deadline = time.time() + args.wait
    delay = 0.25
    while True:
        if try_acquire(path, args.owner, args.ttl):
            print(json.dumps({"acquired": True, "owner": args.owner, "lock": str(path)}, indent=1))
            return 0
        if time.time() >= deadline:
            print(json.dumps({"acquired": False, "owner": args.owner, "lock": str(path),
                              "holder": read_meta(path), "retry": True}, indent=1))
            return 75
        time.sleep(delay)
        delay = min(delay * 2, 2.0)


if __name__ == "__main__":
    sys.exit(main())
