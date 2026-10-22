import importlib.util
import os
import sys

import pytest

from pathlib import Path

_root = Path(__file__).parents[1]
if "kanban_guards" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "kanban_guards", _root / "__init__.py", submodule_search_locations=[str(_root)]
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["kanban_guards"] = _module
    _spec.loader.exec_module(_module)
sys.modules.setdefault("agent_guards", sys.modules["kanban_guards"])

collect_ignore = [str(_root / "__init__.py")]


def pytest_configure(config):
    import sys

    if sys.version_info < (3, 10):
        raise pytest.UsageError(
            "kanban-guards suite needs Python 3.10+ (found "
            f"{sys.version.split()[0]}): on older interpreters the Hermes host's "
            "PEP-604 annotations fail at import, the worker gates silently turn "
            "inert and 100+ tests fail misleadingly. Use the Hermes venv interpreter "
            "(~/.hermes/hermes-agent/venv/bin/python) or the tools python3.14."
        )

    repo = os.path.expanduser("~/.hermes/hermes-agent")
    if repo not in sys.path and any((Path(repo) / "hermes_cli" / n).is_file() for n in ("config.py", "kanban_db.py")):
        sys.path.append(repo)


def pytest_unconfigure(config):
    import sys

    repo = os.path.expanduser("~/.hermes/hermes-agent")
    if repo in sys.path:
        sys.path.remove(repo)


@pytest.fixture(autouse=True)
def _clear_kanban_worker_env(monkeypatch):
    """Neutralize the worker/delegate Kanban env this process may carry.

    Tests were written assuming a host-shaped process: no dispatcher task pin
    and no delegation fence. A live worker carries a path-valued
    ``HERMES_DELEGATED_CHILD_CONTEXT`` marker, and the scope-gate fixtures pin
    ``HERMES_KANBAN_DB`` to their tmp board — with both present, Hermes core
    fences that exact path and read-opens the missing file, erroring 14
    fixtures. Tests that need these vars set them via monkeypatch after this
    fixture runs.
    """
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)


@pytest.fixture(autouse=True)
def _isolated_kanban_board(monkeypatch, tmp_path):
    """Point every board-path resolution at a throwaway tmp home.

    ``workspace_scope_gate`` re-queries the board at ``kanban_db_path()`` whenever
    a lead process carries a messaging session origin (``HERMES_SESSION_PLATFORM``
    + ``HERMES_SESSION_CHAT_ID``) and a configured worker, and rebuilds the
    ``kanban_list`` result from that query. With the operator's real
    ``~/.hermes/kanban.db`` present that read returns live tasks, so a test that
    asserts ``kanban_list`` passes through unchanged fails on a machine that has a
    board but passes on one that does not. Hermes core resolves the board through
    ``HERMES_KANBAN_DB`` (pins the default board file) and ``HERMES_KANBAN_HOME``
    (the root every named board lives under); pinning BOTH to a tmp home that holds
    no ``kanban.db`` makes every resolution fail open (``None``) regardless of what
    the operator's disk holds. This is the strongest level: no test can read the
    live board even by passing an explicit ``board=`` slug, which bypasses the
    ``HERMES_KANBAN_DB`` pin. Tests that exercise a real board set
    ``HERMES_KANBAN_DB``/``HERMES_KANBAN_ATTACHMENTS_ROOT`` themselves after this
    fixture runs, which overrides the pins.
    """
    home = tmp_path / "kanban-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_ATTACHMENTS_ROOT", raising=False)
