import importlib.util
import os
import sys

import pytest

from pathlib import Path

_root = Path(__file__).parents[1]
if "agent_guards" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "agent_guards", _root / "__init__.py", submodule_search_locations=[str(_root)]
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["agent_guards"] = _module
    _spec.loader.exec_module(_module)

collect_ignore = [str(_root / "__init__.py")]


def pytest_configure(config):
    import sys

    if sys.version_info < (3, 10):
        raise pytest.UsageError(
            "agent-guards suite needs Python 3.10+ (found "
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
