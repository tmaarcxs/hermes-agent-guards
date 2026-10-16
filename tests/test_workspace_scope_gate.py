import json
import os
import sys

import pytest

"""
The gate imports hermes_cli lazily inside ``transform``; tests that exercise a real
board need this repo's hermes importable, plus the local ``hermes_yaml`` shim its
utils expect. The imports happen inside the ``hermes_board`` fixture with a
``sys.modules`` restore afterwards, so the rest of the suite keeps the
hermes-import-failure baseline its guards rely on
(``kanban_policy.configured_worker`` returning "" when the import fails).
"""
_HERMES_REPO = os.path.expanduser("~/.hermes/hermes-agent")


@pytest.fixture
def hermes_modules():
    """Load this repo's hermes modules for one test, then undo the import side effects.

    Records the ``sys.modules`` names the load added and the hermes-cli config caches it
    filled, removes the added modules and clears the caches on exit: other test modules
    were written assuming ``from hermes_cli import ...`` fails in this suite.
    """
    added_path = _HERMES_REPO not in sys.path
    before: set[str] = set()
    if added_path:
        sys.path.append(_HERMES_REPO)
    try:
        import yaml as _yaml

        sys.modules.setdefault("hermes_yaml", _yaml)
        before = set(sys.modules)
        import hermes_yaml  # noqa: F401 shim must resolve before hermes_cli imports
        import hermes_cli.kanban_db as kb  # noqa: F401
        import hermes_cli.kanban_db_connect as kbc  # noqa: F401 full chain incl. agent.delegation_context
        yield kb, kbc
    finally:
        if added_path:
            sys.path.remove(_HERMES_REPO)
        from hermes_cli import config as hermes_config

        caches = (getattr(hermes_config, "_LOAD_CONFIG_CACHE", None),
                  getattr(hermes_config, "_RAW_CONFIG_CACHE", None),
                  getattr(hermes_config, "_EXPANDED_CONFIG_CACHE", None))
        for name in set(sys.modules) - before:
            del sys.modules[name]
        for cache in caches:
            if isinstance(cache, dict):
                cache.clear()

guards = sys.modules["agent_guards"]
gate = guards.workspace_scope_gate

LIST_BASE = {"tasks": [], "count": 0, "limit": 30, "truncated": False, "next_limit": None,
             "promoted": 0}


def list_result(*tasks):
    payload = dict(LIST_BASE)
    payload["tasks"] = list(tasks)
    payload["count"] = len(tasks)
    return json.dumps(payload)


def task(tid):
    return {"id": tid, "title": "card " + tid, "status": "running"}


@pytest.fixture
def board(monkeypatch, tmp_path, hermes_modules):
    """A real temp kanban board pinned via ``HERMES_KANBAN_DB`` (the same path
    resolution a ``board=None`` call would use in production) with three
    subscription rows: our thread's card, a same-chat/other-thread card and a
    foreign-chat card."""
    kb, kbc = hermes_modules

    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    conn = kbc.connect(db_path=db_path)
    ids = {}
    for name in ("own", "other_thread", "other_chat"):
        ids[name] = kb.create_task(conn, title=name, body="b", assignee="worker", created_by="t")
    subs = [
        (ids["own"], "telegram", "100", "200"),
        (ids["other_thread"], "telegram", "100", "999"),
        (ids["other_chat"], "telegram", "777", "200"),
    ]
    for task_id, platform, chat, thread in subs:
        conn.execute(
            "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, created_at)"
            " VALUES (?, ?, ?, ?, 0)", (task_id, platform, chat, thread))
    conn.commit()
    conn.close()
    return {"ids": ids, "db_path": db_path}


def transform_with(board, monkeypatch, tasks, *, origin=("telegram", "100", "200"),
                   configure_worker=True):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(guards.kanban_policy, "configured_worker",
                        (lambda: "worker") if configure_worker else (lambda: ""))
    monkeypatch.setattr(gate, "_origin", (lambda: origin) if origin else (lambda: None))
    result = list_result(*tasks)
    return gate.transform("kanban_list", {}, result)


def test_own_thread_card_kept_foreign_hidden(board, monkeypatch):
    ids = board["ids"]
    out = transform_with(board, monkeypatch,
                         [task(ids["own"]), task(ids["other_thread"]), task(ids["other_chat"])])
    payload = json.loads(out)
    assert [t["id"] for t in payload["tasks"]] == [ids["own"]]
    assert payload["count"] == 1
    assert payload["hidden_foreign"] == 2
    for key, value in LIST_BASE.items():
        if key not in ("tasks", "count"):
            assert payload[key] == value  # remaining keys preserved


def test_other_thread_card_on_same_chat_is_hidden(board, monkeypatch):
    ids = board["ids"]
    out = transform_with(board, monkeypatch, [task(ids["other_thread"]), task(ids["other_chat"])])
    payload = json.loads(out)
    assert payload["tasks"] == []
    assert payload["count"] == 0
    assert payload["hidden_foreign"] == 2


def test_board_arg_reaches_the_same_board(board, monkeypatch):
    ids = board["ids"]
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setattr(gate, "_origin", lambda: ("telegram", "100", "200"))
    assert gate.transform("kanban_list", {"board": "NOT A SLUG"},
                          list_result(task(ids["own"]))) is None  # malformed slug fails open


def test_worker_env_untouched(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    result = list_result(task(board["ids"]["other_chat"]))
    assert gate.transform("kanban_list", {}, result) is None


def test_worker_process_short_circuits_before_origin(board, monkeypatch):
    """`_origin` itself: a worker process returns None even when real gateway
    imports are available — the env var, not the import, is the gate."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    assert gate._origin() is None


def test_no_origin_unchanged(board, monkeypatch):
    ids = board["ids"]
    result = list_result(task(ids["own"]), task(ids["other_chat"]))
    assert transform_with(board, monkeypatch, [task(ids["own"]), task(ids["other_chat"])],
                          origin=None) is None
    assert json.loads(result)["count"] == 2


def test_unconfigured_worker_setup_unchanged(board, monkeypatch):
    result = list_result(task(board["ids"]["other_chat"]))
    assert transform_with(board, monkeypatch, [task(board["ids"]["other_chat"])],
                          configure_worker=False) is None
    assert json.loads(result)["count"] == 1


def test_all_cards_owned_result_unchanged(board, monkeypatch):
    result = list_result(task(board["ids"]["own"]))
    assert transform_with(board, monkeypatch, [task(board["ids"]["own"])]) is None


def test_non_list_tools_and_bad_payloads_pass(board, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setattr(gate, "_origin", lambda: ("telegram", "100", "200"))
    assert gate.transform("kanban_show", {}, "{}") is None
    assert gate.transform("kanban_list", {}, "not json") is None
    assert gate.transform("kanban_list", {}, '{"tasks": null}') is None
    assert gate.transform("kanban_list", {}, None) is None


def test_missing_board_db_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "nonexistent" / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setattr(gate, "_origin", lambda: ("telegram", "100", "200"))
    result = list_result(task("t_any"))
    assert gate.transform("kanban_list", {}, result) is None


def test_missing_subs_table_fails_open(tmp_path, monkeypatch):
    import sqlite3

    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setattr(gate, "_origin", lambda: ("telegram", "100", "200"))
    assert gate.transform("kanban_list", {}, list_result(task("t_any"))) is None


@pytest.mark.skipif(guards is None, reason="agent_guards not importable without conftest")
def test_chain_kanban_list_transformed(board, monkeypatch):
    """The registered transform_tool_result chain carries the scoping."""
    ids = board["ids"]
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setattr(gate, "_origin", lambda: ("telegram", "100", "200"))
    result = list_result(task(ids["own"]), task(ids["other_chat"]))
    out = guards._transform_tool_result(tool_name="kanban_list", args={}, result=result)
    assert out is not None and json.loads(out)["hidden_foreign"] == 1
    assert guards._transform_tool_result(  # a foreign kanban_show is not this gate's business
        tool_name="kanban_show", args={}, result='{"task": {"id": "%s"}}' % ids["other_chat"]) is None
