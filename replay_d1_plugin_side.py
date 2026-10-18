#!/usr/bin/env python3
"""Read-only replay of notification_reconcile.sweep against the live board.

Proves the claim-cursor coverage change (plugin-side variant of the D1 fix):
- none of the 11 states recorded in the reconcile state file — all of them
  wake-delivered in the gateway but never ping-checkpointed — produces a
  repair notice: the ten whose subscription rows still exist are now classified
  covered by the claim cursor, and the adopted-silent entry with no
  subscription row stays adopted without injecting;
- a synthetic deleted-sub case still reports repair (the hook's reason to
  exist stands).

No state file is written: the module's _state_path is redirected to a
temporary directory inside this workspace, and no full on_tick sweep is run.
"""
import importlib.util
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

BOARD = "file:/Users/marcos.sanchez/.hermes/kanban.db?mode=ro"
STATE_FILE = Path.home() / ".hermes" / "state" / "kanban-notify-reconcile.json"

PLUGIN_DIR = Path(sys.argv[1]).resolve()
STATE_DIR = Path(tempfile.mkdtemp(prefix="t5a6f-replay-", dir=str(PLUGIN_DIR)))
spec = importlib.util.spec_from_file_location(
    "agent_guards", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
)
mod = importlib.util.module_from_spec(spec)
sys.modules["agent_guards"] = mod
spec.loader.exec_module(mod)
reconcile = mod.notification_reconcile
STATE_DIR_parent = STATE_DIR
reconcile._home = lambda: STATE_DIR_parent
assert reconcile._state_path() == STATE_DIR / "state" / "kanban-notify-reconcile.json"
assert not (STATE_DIR / "state" / "kanban-notify-reconcile.json").exists()

conn = sqlite3.connect(BOARD, uri=True)
now = time.time()

record = json.loads(STATE_FILE.read_text())
print(f"reconcile record entries: {len(record)}")
"""Full sweep against the live board with the recorded state as input and an
inject that FAILS on purpose: nothing can be delivered or written, so the
board and every state file stay untouched while the classification runs."""
notices = []
new_state = reconcile.sweep(
    conn, record, lambda text, origin: notices.append((text, origin)) or False, now
)
repairs = [text for text, _ in notices]
print(f"repair notices the changed sweep would fire for the recorded states: {len(repairs)}")
assert not repairs, repairs

cards = {c["id"]: c for c in reconcile.uncovered_cards(conn, now)}
covered, uncovered = 0, 0
for tid, v in sorted(record.items(), key=lambda kv: kv[1].get("event_id", 0)):
    eid = v["event_id"]
    sub_rows = conn.execute(
        "select last_ping_event_id, last_event_id from kanban_notify_subs where task_id = ?", (tid,)
    ).fetchall()
    still_living = f"live sub, claim cursor covers it" if any(
        int(claim or 0) >= int(eid) or int(ping or 0) >= int(eid) for ping, claim in sub_rows
    ) else ("no sub row (adopted-silent)" if not sub_rows else "live sub, cursor does not cover it")
    if tid in cards and cards[tid]["event_id"] == eid and not sub_rows:
        pose = "uncovered-recorded (adopted-silent, no inject)"
    elif tid in cards:
        pose = "uncovered"
    else:
        pose = "covered"
    if pose == "uncovered":
        uncovered += 1
    else:
        covered += 1
    print(f"  {tid} event={eid} -> {pose} ({still_living})")
print(f"recorded states classified covered: {covered}/{len(record)}; still uncovered: {uncovered}")
kept = "t_b29220b7" in new_state
print(f"adopted-silent entry kept in the record without injecting: {kept}")
"""Synthetic deleted-sub repair case, mirrored from tests/test_notification_reconcile.py."""
mem = sqlite3.connect(":memory:")
mem.executescript(
    """
    create table tasks (id text, status text, title text);
    create table task_events (id integer primary key, task_id text, kind text, payload text, created_at integer);
    create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
        chat_type text, user_id text, user_id_alt text, notifier_profile text, last_ping_event_id integer,
        last_event_id integer);
    """
)
mem.execute("insert into tasks values ('t_synthetic', 'review', 'synthetic deleted-sub repair')")
mem.execute(
    "insert into task_events (id, task_id, kind, payload, created_at) values (1, 't_synthetic', 'completed', ?, ?)",
    (json.dumps({"summary": "synthetic repair probe"}), now - reconcile.GRACE_SECONDS - 5),
)
mem.execute(
    "insert into kanban_notify_subs values ('t_synthetic', 'telegram', '42', '', 'dm', '42', null, 'default', 0, 0)"
)
pending = reconcile.sweep(mem, {}, lambda *a: False, now)
assert pending["t_synthetic"]["pending"] is True, "live sub must stay pending"
mem.execute("delete from kanban_notify_subs where task_id = 't_synthetic'")
notices = []
repaired = mod.notification_reconcile.sweep(mem, pending, lambda text, origin: notices.append(text) or True, now + reconcile.GRACE_SECONDS + 5)
assert len(notices) == 1, f"exactly one repair notice expected, got {len(notices)}"
print(f"\nsynthetic deleted-sub case: repair notice produced = {len(notices) == 1}")
print(notices[0][:180] + "...")

shutil.rmtree(STATE_DIR)
conn.close()
print("\nstate file written: none (temp dir removed; live state file untouched)")
print("read-only board connection used:", BOARD)
