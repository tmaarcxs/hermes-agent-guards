"""The Jot reminder fires once per work turn, and only for edits inside a git work tree.

Self-contained on purpose: a ``tests/conftest.py`` here would be a second top-level
``conftest`` module, and ``kanban-guards/tests/test_register_contract.py`` imports that name
by hand — my copy would shadow theirs and break it. Same reasoning as ``tests/_plugin_loader.py``.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest
import yaml

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
if "jot_guards" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "jot_guards", _root / "__init__.py", submodule_search_locations=[str(_root)]
    )
    if _spec is not None and _spec.loader is not None:
        _module = importlib.util.module_from_spec(_spec)
        sys.modules["jot_guards"] = _module
        _spec.loader.exec_module(_module)
if not hasattr(sys.modules["jot_guards"], "jot_nudge"):
    importlib.import_module("jot_guards.jot_nudge")

_hermes_repo = os.path.expanduser("~/.hermes/hermes-agent")
if _hermes_repo not in sys.path and (Path(_hermes_repo) / "hermes_cli" / "config.py").is_file():
    sys.path.append(_hermes_repo)

guards = sys.modules["jot_guards"]
gate = guards.jot_nudge


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    gate.reset()
    yield
    gate.reset()


@pytest.fixture
def work_repo(tmp_path):
    repo = tmp_path / "demo-repo"
    (repo / ".git").mkdir(parents=True)
    return repo


@pytest.fixture
def plain_dir(tmp_path):
    """A directory that is no repository: the shape of agent upkeep (~/.hermes)."""
    return tmp_path


def turned(tool="write_file", session="s1", turn="t1"):
    """A tool call in this turn (write_file keeps the turn free of Jot activity)."""
    guards._post_tool_call(
        tool_name=tool, args={}, result="{}", status="ok", session_id=session, turn_id=turn
    )


def check(paths, session="s1", attempt=0):
    return gate.check(session_id=session, attempt=attempt, changed_paths=paths)


def test_reminds_after_a_work_repo_edit_without_jot(work_repo):
    turned()
    result = check([str(work_repo / "src" / "app.py")])
    assert result is not None and result["action"] == "continue"
    assert "jot_update" in result["message"]


def test_silent_outside_a_repository(plain_dir):
    """Agent upkeep edits no repository, so no reminder."""
    turned()
    assert check([str(plain_dir / "skill.md")]) is None


def test_silent_without_changed_paths(work_repo):
    turned()
    assert check([]) is None


def test_silent_after_a_mutating_jot_call(work_repo):
    for tool in ("jot_create", "jot_update", "jot_set_task", "jot_delete"):
        gate.reset()
        turned(tool=tool)
        assert check([str(work_repo / "a.py")]) is None, tool


def test_reading_the_board_is_not_updating_it(work_repo):
    turned(tool="jot_read")
    assert check([str(work_repo / "a.py")]) is not None


def test_one_nudge_per_turn(work_repo):
    turned()
    assert check([str(work_repo / "a.py")]) is not None
    assert check([str(work_repo / "a.py")], attempt=1) is None


def test_next_turn_gets_its_own_reminder(work_repo):
    turned(tool="jot_update", turn="t1")
    assert check([str(work_repo / "a.py")]) is None
    turned(turn="t2")
    assert check([str(work_repo / "a.py")]) is not None


def test_state_is_session_scoped(work_repo):
    turned(tool="jot_update", session="a", turn="t1")
    assert check([str(work_repo / "a.py")], session="b") is not None


def test_skip_roots_win_over_the_git_tree(work_repo, monkeypatch):
    monkeypatch.setattr(gate, "skip_roots", lambda: [work_repo])
    turned()
    assert check([str(work_repo / "a.py")]) is None


def test_skip_roots_are_configurable(work_repo, monkeypatch):
    monkeypatch.setattr(gate, "settings", lambda: {"jot_nudge_skip_roots": [str(work_repo)]})
    assert gate.skip_roots() == [work_repo]
    turned()
    assert check([str(work_repo / "a.py")]) is None


def test_reminder_can_be_switched_off(work_repo, monkeypatch):
    monkeypatch.setattr(gate, "settings", lambda: {"jot_nudge": False})
    turned()
    assert check([str(work_repo / "a.py")]) is None


def test_worker_process_is_skipped(work_repo, monkeypatch):
    """A worker hands off to its card; the lead keeps the Jot line."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    turned()
    assert check([str(work_repo / "a.py")]) is None


def test_settings_come_from_the_plugin_entry(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"plugins": {"entries": {"jot-guards": {"settings": {"jot_nudge": False}}}}},
    )
    gate.reset()
    assert gate.settings() == {"jot_nudge": False}


def test_register_matches_declared_hooks():
    """The plugin registers exactly what plugin.yaml declares."""
    declared = tuple(yaml.safe_load((_root / "plugin.yaml").read_text())["hooks"])
    registered = []

    class Ctx:
        def register_hook(self, hook, fn):
            registered.append(hook)

    guards.register(Ctx())
    assert set(registered) == set(declared), (registered, declared)
