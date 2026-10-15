import sqlite3
import sys

import pytest

guards = sys.modules["agent_guards"]
gate = guards.pr_comment_gate

# Synthetic stand-in for the incident body: a lead decision comment on a stallable card
# that quotes a THIRD-PARTY PR URL, which the dispatcher's respawn guard indistinguishably
# reads as "a worker already opened a PR". No real card id, author, or comment text.
INCIDENT_BODY = (
    "Lead decision (authority: the product owner approved the upstream contribution in "
    "this chat; the card body already directs \"comment our data on the existing "
    "PR/issue instead of duplicating\" — precedent: same flow used for earlier upstream "
    "duplicates). Execute now:\n"
    "1. Post ONE comment on https://github.com/UPSTREAM-ORG/example-upstream-repo/pull/1 as "
    "the contributor account, English, sanitized: the 7-day measurement that backs the change..."
)


@pytest.fixture(autouse=True)
def lead_setup(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")


def board(status="ready"):
    conn = sqlite3.connect(":memory:")
    conn.execute("create table tasks (id text, status text, title text, block_kind text, created_at integer)")
    conn.execute("create table task_events (id integer primary key, task_id text, kind text, created_at integer)")
    conn.execute("insert into tasks values ('t_example', ?, 'stalled card', null, 1)", (status,))
    return conn


def install_db(monkeypatch, conn):
    import types

    fake = types.ModuleType("hermes_cli")
    fake.kanban_db = types.SimpleNamespace(kanban_db_path=lambda board=None: "/dev/null")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    monkeypatch.setattr(gate, "_board_conn", lambda: conn)


def comment(args=None):
    args = {"task_id": "t_example", "body": INCIDENT_BODY} if args is None else args
    return guards._pre_tool_call(tool_name="kanban_comment", args=args)


def test_lead_pr_url_comment_on_ready_card_is_blocked(monkeypatch):
    install_db(monkeypatch, board("ready"))
    result = comment()
    assert result["action"] == "block"
    assert "respawn_guarded reason=active_pr" in result["message"]
    assert "UPSTREAM-ORG/example-upstream-repo#1" in result["message"]


@pytest.mark.parametrize("status", ["todo", "ready", "blocked"])
def test_stallable_statuses_are_blocked(monkeypatch, status):
    install_db(monkeypatch, board(status))
    assert comment()["action"] == "block"


@pytest.mark.parametrize("status", ["running", "review", "done", "archived"])
def test_running_and_closed_cards_are_not_blocked(monkeypatch, status):
    install_db(monkeypatch, board(status))
    assert comment() is None


def test_worker_pr_url_comment_is_allowed(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker1")
    install_db(monkeypatch, board("ready"))
    assert comment() is None


def test_without_default_assignee_nothing_is_enforced(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    install_db(monkeypatch, board("ready"))
    assert comment() is None


def test_worker_authored_body_url_is_allowed_in_lead_chat(monkeypatch):
    # A worker's OWN PR URL on a running card is the guard's legitimate input; even in the
    # lead chat the status check (not the author) gates this module.
    install_db(monkeypatch, board("done"))
    body = "Opened my PR: https://github.com/example-org/example-internal-repo/pull/774 with exact-head CI green."
    assert comment({"task_id": "t_example", "body": body}) is None


def test_shorthand_citation_is_not_a_url_and_passes(monkeypatch):
    install_db(monkeypatch, board("ready"))
    body = "Duplicate of UPSTREAM-ORG/example-upstream-repo#1 — comment our data there instead."
    assert comment({"task_id": "t_example", "body": body}) is None


def test_lookup_failure_fails_open(monkeypatch):
    monkeypatch.setattr(gate, "_board_conn", lambda: None)
    assert comment() is None


def test_unknown_task_id_fails_open(monkeypatch):
    install_db(monkeypatch, board())
    assert comment({"task_id": "t_unknown", "body": INCIDENT_BODY}) is None
