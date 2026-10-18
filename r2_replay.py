"""Before/after replay of the C2 finding R2 (t_ef9ddb7b): a card at runs=12, crs=6.

BEFORE (base, scalar `kept >= card["step"]` dedup): the first sweep fires a CR-family
wake storing {"t_case": 105}; every later runs-driven escalation (20, 40) stays silent
while wake_text promises "You will be warned again only if it reaches 10." — the hint
lies because its bare arithmetic (s > step - 100) does not match the dedup arithmetic,
and the stored 105 masks all future run-family fires.

AFTER (per-family slots): the first sweep fires ONE joint wake (the text already
reports both counts) storing [10, 105]; the escalations to 20 and 40 runs fire their
own wakes, and the hint names the next threshold of the family that will fire next.

Run with BOTH module versions to reproduce the evidence table:
    python3 r2_replay.py <path-to-agent-guards-dir>
"""
import json
import sqlite3
import sys
import types

Guards = types.ModuleType("agent_guards")
Guards.__path__ = [sys.argv[1] if len(sys.argv) > 1 else "."]
sys.modules["agent_guards"] = Guards
from agent_guards import churn_wake as churn


def board():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        create table tasks (id text, status text, title text);
        create table task_runs (id integer primary key, task_id text);
        create table task_events (id integer primary key, task_id text, kind text, payload text);
        create table kanban_notify_subs (task_id text, platform text, chat_id text, thread_id text,
            chat_type text, user_id text, user_id_alt text, notifier_profile text);
        """
    )
    return conn


def card(conn, tid, status, runs=0, crs=0, subscribed=True, reasons=()):
    conn.execute("insert into tasks values (?, ?, ?)", (tid, status, f"title {tid}"))
    for _ in range(runs):
        conn.execute("insert into task_runs (task_id) values (?)", (tid,))
    for n in range(crs):
        conn.execute(
            "insert into task_events (task_id, kind, payload) values (?, 'changes_requested', ?)",
            (tid, json.dumps({"reason": reasons[n] if n < len(reasons) else f"fix {n + 1}"})),
        )
    if subscribed:
        conn.execute(
            "insert into kanban_notify_subs values (?, 'telegram', '555000100', '900332', 'dm', '555000100', null, 'default')",
            (tid,),
        )


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, text, origin):
        self.calls.append((text, origin))
        return True


def hint(text):
    tail = text.splitlines()[-1]
    return tail.split("Record the decision as a comment on the card.")[-1].strip()


conn = board()
card(conn, "t_case", "review", runs=12, crs=6, reasons=["r"] * 6)
rec = Rec()
state = churn.sweep(conn, {}, rec)
print(f"FIRE 1 (runs=12, crs=6): wakes={len(rec.calls)} state={state}")
print(f"  hint: {hint(rec.calls[0][0])}")

for _ in range(8):
    conn.execute("insert into task_runs (task_id) values (?)", ("t_case",))
rec2 = Rec()
state = churn.sweep(conn, state, rec2)
print(f"AT runs=20: wakes={len(rec2.calls)} state={state}")
if rec2.calls:
    print(f"  hint: {hint(rec2.calls[0][0])}")

for _ in range(20):
    conn.execute("insert into task_runs (task_id) values (?)", ("t_case",))
rec3 = Rec()
state = churn.sweep(conn, state, rec3)
print(f"AT runs=40: wakes={len(rec3.calls)} state={state}")
if rec3.calls:
    print(f"  hint: {hint(rec3.calls[0][0])}")

for n in range(4):
    conn.execute(
        "insert into task_events (task_id, kind, payload) values (?, 'changes_requested', ?)",
        ("t_case", json.dumps({"reason": f"more {n}"})),
    )
rec4 = Rec()
state = churn.sweep(conn, state, rec4)
print(f"AT crs=10: wakes={len(rec4.calls)} state={state}")
if rec4.calls:
    print(f"  hint: {hint(rec4.calls[0][0])}")
