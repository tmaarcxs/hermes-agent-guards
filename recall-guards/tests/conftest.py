import importlib.util
import os
import sys

import pytest

from pathlib import Path

_root = Path(__file__).parents[1]
if "recall_guards" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "recall_guards", _root / "__init__.py", submodule_search_locations=[str(_root)]
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["recall_guards"] = _module
    _spec.loader.exec_module(_module)

collect_ignore = [str(_root / "__init__.py")]


def pytest_configure(config):
    if sys.version_info < (3, 10):
        raise pytest.UsageError(
            "recall-guards suite needs Python 3.10+ "
            f"(found {sys.version.split()[0]}): use the Hermes venv interpreter "
            "(~/.hermes/hermes-agent/venv/bin/python)."
        )
    repo = os.path.expanduser("~/.hermes/hermes-agent")
    if repo not in sys.path and (Path(repo) / "hermes_cli" / "config.py").is_file():
        sys.path.append(repo)


def pytest_unconfigure(config):
    sys.modules.pop("recall_guards", None)
