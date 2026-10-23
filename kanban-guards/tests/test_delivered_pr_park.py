"""The delivered-PR park: a ready card whose PR is merged is parked, not re-spawned.

Fixture board on a tmp path and an injected ``gh`` runner, so nothing touches the
network and no live board is read. Every case that asserts "untouched" carries a
paired positive control (``control``) that must be parked in the same tick, so
disabling the park action fails the test instead of only the first one.
"""

import json
import sys
import time

import pytest

guards = sys.modules["kanban_guards"]
park = guards.delivered_pr_park

NOW = int(time.time())
PR_URL = "https://github.com/tmaarcxs/hermes-agent-guards/pull/123"
CTRL_URL = "https://github.com/tmaarcxs/hermes-agent-guards/pull/456"
SHA = "a" * 40
OLD = NOW - 4000


class Gh:
    """Injectable ``gh`` runner: records calls, serves PR JSON, never networks.

    ``overrides`` maps a PR URL to a state so a negative case (open PR) and its
    positive control can run in the same tick. ``fail``/``garbage`` are PR URLs
    the runner serves a non-zero exit or unparseable JSON for, which is how the
    gh-failure cases keep a working paired control on another card.
    """

    def __init__(self, state="MERGED", fail=(), garbage=(), overrides=None):
        self.state = state
        self.fail = set(fail)
        self.garbage = set(garbage)
        self.overrides = overrides or {}
        self.calls = []

    def __call__(self, args, env):
        self.calls.append(list(args))
        if args[:3] == ["gh", "pr", "view"]:
            url = args[3]
            if url in self.fail:
                return 1, ""
            if url in self.garbage:
                return 0, "not json"
            state = next((s for u, s in self.overrides.items() if u in url), self.state)
            merge = {"oid": SHA} if state == "MERGED" else None
            return 0, json.dumps({"state": state, "mergeCommit": merge, "number": 123})
        if args[:3] == ["gh", "auth", "token"]:
            return 0, "tok\n"
        return 1, ""

    @property
    def views(self):
        return [c for c in self.calls if c[:3] == ["gh", "pr", "view"]]


@pytest.fixture
def board(tmp_path, monkeypatch):
    path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(park.KILL_ENV, raising=False)
    from hermes_cli import kanban_db_connect

    conn = kanban_db_connect.connect(board=None)
    yield conn
    conn.close()


@pytest.fixture
def gh(monkeypatch):
    runner = Gh()
    monkeypatch.setattr(park, "_run_gh", runner)
    return runner


def card(conn, tid, status="ready", claim_lock=None, run_id=None):
    conn.execute(
        "insert into tasks (id, title, status, assignee, created_at, claim_lock, current_run_id) "
        "values (?, 'a card', ?, 'worker', ?, ?, ?)",
        (tid, status, OLD, claim_lock, run_id),
    )
    conn.commit()
    return tid


def crashed(conn, tid):
    conn.execute(
        "insert into task_runs (task_id, status, started_at, ended_at) values (?, 'crashed', ?, ?)",
        (tid, OLD, OLD + 1),
    )
    conn.commit()


def comment(conn, tid, body, at=NOW):
    conn.execute(
        "insert into task_comments (task_id, author, body, created_at) values (?, 'worker', ?, ?)",
        (tid, body, at),
    )
    conn.commit()


def event(conn, tid, kind, payload=None, at=NOW):
    conn.execute(
        "insert into task_events (task_id, kind, payload, created_at) values (?, ?, ?, ?)",
        (tid, kind, json.dumps(payload) if payload else None, at),
    )
    conn.commit()


def control(conn, tid="t_ctrl"):
    """A ready card the park must catch: crashed run, PR comment, no handoff."""
    card(conn, tid)
    crashed(conn, tid)
    comment(conn, tid, f"opened {CTRL_URL}")
    return tid


class Result:
    skipped_locked = False


def run(conn):
    park.tick(result=Result(), board=None)


def status(conn, tid):
    return conn.execute("select status from tasks where id = ?", (tid,)).fetchone()[0]


def comments(conn, tid):
    return [r[0] for r in conn.execute(
        "select body from task_comments where task_id = ? order by id", (tid,))]


def park_comments(conn, tid):
    return [b for b in comments(conn, tid) if park.MARKER in b]


def test_merged_pr_parks_once_with_one_comment(board, gh):
    tid = card(board, "t_card")
    crashed(board, tid)
    comment(board, tid, f"opened {PR_URL}")
    run(board)
    assert status(board, tid) == "blocked"
    bodies = park_comments(board, tid)
    assert len(bodies) == 1
    assert SHA in bodies[0]
    assert "already MERGED" in bodies[0]
    assert "parked instead of re-spawned" in bodies[0]


def test_next_tick_posts_no_second_comment_and_no_second_call(board, gh):
    tid = card(board, "t_card")
    crashed(board, tid)
    comment(board, tid, f"opened {PR_URL}")
    run(board)
    assert status(board, tid) == "blocked"
    assert len(gh.views) == 1
    run(board)
    assert len(gh.views) == 1
    assert len(park_comments(board, tid)) == 1


def test_cache_spares_a_repeated_gh_call_on_a_still_ready_card(board, monkeypatch):
    runner = Gh(overrides={PR_URL: "OPEN"})
    monkeypatch.setattr(park, "_run_gh", runner)
    tid = card(board, "t_card")
    crashed(board, tid)
    comment(board, tid, f"opened {PR_URL}")
    run(board)
    assert len(runner.views) == 1
    run(board)
    assert len(runner.views) == 1
    assert status(board, tid) == "ready"


def test_handoff_after_the_pr_comment_leaves_the_card_running(board, gh):
    tid = card(board, "t_card")
    crashed(board, tid)
    comment(board, tid, f"opened {PR_URL}")
    event(board, tid, "unblocked", at=NOW + 1)
    other = control(board)
    run(board)
    assert status(board, tid) == "ready"
    assert comments(board, tid) == [f"opened {PR_URL}"]
    assert len(gh.views) == 1
    assert status(board, other) == "blocked"


def test_assigned_handoff_after_the_pr_comment_leaves_the_card_running(board, gh):
    tid = card(board, "t_card")
    crashed(board, tid)
    comment(board, tid, f"opened {PR_URL}")
    event(board, tid, "assigned", {"from": "worker", "assignee": "reviewer"}, at=NOW + 1)
    other = control(board)
    run(board)
    assert status(board, tid) == "ready"
    assert status(board, other) == "blocked"


def test_open_pr_is_untouched(board, monkeypatch):
    runner = Gh(overrides={PR_URL: "OPEN"})
    monkeypatch.setattr(park, "_run_gh", runner)
    tid = card(board, "t_card")
    crashed(board, tid)
    comment(board, tid, f"opened {PR_URL}")
    other = control(board)
    run(board)
    assert status(board, tid) == "ready"
    assert comments(board, tid) == [f"opened {PR_URL}"]
    assert status(board, other) == "blocked"


def test_no_crashed_run_never_calls_gh(board, gh):
    tid = card(board, "t_card")
    comment(board, tid, f"opened {PR_URL}")
    run(board)
    assert gh.calls == []
    assert status(board, tid) == "ready"
    other = control(board)
    run(board)
    assert len(gh.views) == 1
    assert status(board, other) == "blocked"


def test_gh_failure_takes_no_action(board, monkeypatch):
    runner = Gh(fail={PR_URL})
    monkeypatch.setattr(park, "_run_gh", runner)
    tid = card(board, "t_card")
    crashed(board, tid)
    comment(board, tid, f"opened {PR_URL}")
    other = control(board)
    run(board)
    assert status(board, tid) == "ready"
    assert comments(board, tid) == [f"opened {PR_URL}"]
    assert status(board, other) == "blocked"


def test_gh_garbage_json_takes_no_action(board, monkeypatch):
    monkeypatch.setattr(park, "_run_gh", Gh(garbage={PR_URL}))
    tid = card(board, "t_card")
    crashed(board, tid)
    comment(board, tid, f"opened {PR_URL}")
    other = control(board)
    run(board)
    assert status(board, tid) == "ready"
    assert comments(board, tid) == [f"opened {PR_URL}"]
    assert status(board, other) == "blocked"


def test_cap_exceeded_skips_and_logs_once(board, gh, caplog):
    tid = card(board, "t_card")
    crashed(board, tid)
    comment(board, tid, f"opened {PR_URL}")
    path = park._state_path()
    path.write_text(json.dumps({park.CALLS_KEY: [NOW - 1] * park.CALL_CAP}))
    with caplog.at_level("INFO"):
        run(board)
        run(board)
    assert gh.views == []
    assert status(board, tid) == "ready"
    assert len([r for r in caplog.records if "skipping quietly" in r.getMessage()]) == 1


@pytest.mark.parametrize("status_name", ["running", "review", "blocked"])
def test_non_ready_cards_are_untouched(board, gh, status_name):
    tid = card(board, f"t_{status_name}", status=status_name, claim_lock="lock", run_id=7)
    crashed(board, tid)
    comment(board, tid, f"opened {PR_URL}")
    other = control(board)
    run(board)
    assert status(board, tid) == status_name
    assert comments(board, tid) == [f"opened {PR_URL}"]
    assert status(board, other) == "blocked"


def test_park_action_disabled_leaves_the_card_ready(board, gh, monkeypatch):
    monkeypatch.setenv(park.KILL_ENV, "off")
    tid = card(board, "t_card")
    crashed(board, tid)
    comment(board, tid, f"opened {PR_URL}")
    run(board)
    assert status(board, tid) == "ready"
    assert comments(board, tid) == [f"opened {PR_URL}"]
