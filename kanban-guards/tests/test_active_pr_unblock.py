"""The active_pr auto-lift: hop only for a human unblock newer than the PR comment.

Fixture board on a tmp path, no live board touched. The core guard is imported
read-only — these tests prove the plugin's hop makes core's own
``check_respawn_guard`` return None, they never modify core.

Every case carries a paired positive control (``control``) that hops in the same
tick, so disabling the hop fails every test, not only the first: the negative
cards assert "untouched" while the control asserts the lift still ran.
"""

import json
import sqlite3
import sys
import time

import pytest

guards = sys.modules["kanban_guards"]
unblock = guards.active_pr_unblock

NOW = int(time.time())
PR_URL = "https://github.com/tmaarcxs/hermes-agent-guards/pull/123"
OLD = NOW - 4000


@pytest.fixture
def board(tmp_path, monkeypatch):
    path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from hermes_cli import kanban_db_connect

    conn = kanban_db_connect.connect(board=None)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _profiles(monkeypatch):
    from hermes_cli import config_effective

    monkeypatch.setattr(
        config_effective, "load_user_config_effective",
        lambda *a, **k: {"kanban": {"dispatch_profiles": ["default", "reviewer"]}},
    )


def card(conn, tid, status="ready", assignee="worker", claim_lock=None, run_id=None):
    conn.execute(
        "insert into tasks (id, title, status, assignee, created_at, claim_lock, current_run_id) "
        "values (?, 'a card', ?, ?, ?, ?, ?)",
        (tid, status, assignee, OLD, claim_lock, run_id),
    )
    conn.commit()
    return tid


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
    """A ready card the hop must lift: PR comment then a newer unblock."""
    card(conn, tid)
    comment(conn, tid, f"opened {PR_URL}")
    event(conn, tid, "unblocked", at=NOW + 1)
    return tid


class Result:
    def __init__(self, *pairs):
        self.respawn_guarded = list(pairs)


def run(conn, *pairs):
    unblock.tick(result=Result(*pairs), board=None)


def assigned(conn, tid):
    return [
        json.loads(r[0])
        for r in conn.execute(
            "select payload from task_events where task_id = ? and kind = 'assigned' order by id",
            (tid,),
        )
    ]


def comments(conn, tid):
    return [r[0] for r in conn.execute("select body from task_comments where task_id = ?", (tid,))]


def backdate(conn, tid, seconds=60):
    """Make the PR comment older than the hop, so the hop's ``assigned`` events
    land strictly after it. Timestamps are second-granular: without this the hop
    can tie the comment's second and core's guard fails closed on the tie."""
    conn.execute(
        "UPDATE task_comments SET created_at = created_at - ? WHERE task_id = ?",
        (seconds, tid),
    )
    conn.commit()


def guard(conn, tid):
    from hermes_cli.kanban_db_dispatch import check_respawn_guard

    return check_respawn_guard(conn, tid, lane="ready")


def test_unblock_after_pr_comment_lifts_the_guard(board):
    tid = card(board, "t_card")
    comment(board, tid, f"opened {PR_URL}")
    backdate(board, tid)
    event(board, tid, "unblocked", at=NOW + 1)
    run(board, (tid, "active_pr"))
    hops = assigned(board, tid)
    assert len(hops) == 2
    assert hops[1]["from"] != hops[1]["assignee"]
    assert guard(board, tid) is None


def test_same_second_hop_stays_held_and_alerts_once(board, monkeypatch):
    """The one-second edge: a hop in the comment's own second is not a handoff,
    so core still holds the card. That must surface as one human alert, not a
    silent strand."""
    fixed = int(time.time())
    monkeypatch.setattr(time, "time", lambda: float(fixed))
    tid = card(board, "t_card")
    comment(board, tid, f"opened {PR_URL}", at=fixed)
    event(board, tid, "unblocked", at=fixed + 1)
    run(board, (tid, "active_pr"))
    assert len(assigned(board, tid)) == 2
    assert guard(board, tid) == "active_pr"
    run(board, (tid, "active_pr"))
    assert guard(board, tid) == "active_pr"
    assert len(comments(board, tid)) == 2
    alert = comments(board, tid)[-1]
    assert "human must decide" in alert
    assert "same clock second" in alert
    assert "automatic lifts" not in alert


def test_pr_comment_without_unblock_is_left_held(board):
    tid = card(board, "t_card")
    comment(board, tid, f"opened {PR_URL}")
    other = control(board)
    run(board, (tid, "active_pr"), (other, "active_pr"))
    assert assigned(board, tid) == []
    assert guard(board, tid) == "active_pr"
    assert len(assigned(board, other)) == 2


def test_unblock_older_than_pr_comment_is_not_a_lift(board):
    tid = card(board, "t_card")
    event(board, tid, "unblocked", at=NOW - 10)
    comment(board, tid, f"opened {PR_URL}", at=NOW)
    other = control(board)
    run(board, (tid, "active_pr"), (other, "active_pr"))
    assert assigned(board, tid) == []
    assert guard(board, tid) == "active_pr"
    assert len(assigned(board, other)) == 2


def test_cap_posts_one_comment_and_stops_hopping(board):
    tid = card(board, "t_card")
    comment(board, tid, f"opened {PR_URL}")
    event(board, tid, "unblocked", at=NOW + 1)
    path = unblock._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({tid: {"hops": [NOW - 700, NOW - 800, NOW - 900]}}))
    other = control(board)
    run(board, (tid, "active_pr"), (other, "active_pr"))
    assert assigned(board, tid) == []
    assert len(comments(board, tid)) == 2
    assert "human must decide" in comments(board, tid)[-1]
    assert len(assigned(board, other)) == 2
    run(board, (tid, "active_pr"), (other, "active_pr"))
    assert assigned(board, tid) == []
    assert len(comments(board, tid)) == 2


def test_running_or_review_card_is_untouched(board):
    for status in ("running", "review"):
        tid = card(board, f"t_{status}", status=status, claim_lock="lock", run_id=7)
        comment(board, tid, f"opened {PR_URL}")
        event(board, tid, "unblocked", at=NOW + 1)
        other = control(board, f"t_ctrl_{status}")
        run(board, (tid, "active_pr"), (other, "active_pr"))
        assert assigned(board, tid) == []
        assert len(assigned(board, other)) == 2


def test_other_guard_reasons_are_untouched(board):
    tid = card(board, "t_card")
    comment(board, tid, f"opened {PR_URL}")
    event(board, tid, "unblocked", at=NOW + 1)
    run(board, (tid, "recent_success"), (tid, "blocker_auth"))
    assert assigned(board, tid) == []
    assert len(comments(board, tid)) == 1
    other = control(board)
    run(board, (other, "active_pr"))
    assert len(assigned(board, other)) == 2
