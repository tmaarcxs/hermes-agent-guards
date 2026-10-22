"""Shared helpers for the cross-plugin tests.

Each plugin folder has its own ``tests/conftest.py`` that registers the folder
under its canonical module name (``shell_guards``, ``workspace_guards``,
``kanban_guards``). The cross-plugin tests here need to load a folder *again* to
inspect it; they must not evict the canonical entry, because the folder suites
hold module-level references (``guards = sys.modules["kanban_guards"]``) that a
reload would desynchronize. So every loader below uses a unique probe package
name and removes only the entries it added.

This file is deliberately NOT named ``conftest.py``: the folder suites do
``import conftest``, and a second top-level ``conftest`` module on the same
``sys.path`` would shadow theirs.
"""

import importlib.util
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_counter = itertools.count()


def load_plugin(folder_name: str, probe: str = ""):
    """Load ``<ROOT>/<folder_name>/__init__.py`` under a unique package name.

    ``folder_name`` is the on-disk folder (``kanban-guards``); the probe package
    name is derived from it plus a counter so two loads never collide and the
    canonical ``sys.modules`` entry is never touched.
    """
    folder = ROOT / folder_name
    name = f"_probe_{folder_name.replace('-', '_')}_{probe or next(_counter)}"
    spec = importlib.util.spec_from_file_location(
        name, folder / "__init__.py", submodule_search_locations=[str(folder)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        unload(name)
        raise
    return module


def unload(name: str) -> None:
    for mod in [m for m in list(sys.modules) if m == name or m.startswith(name + ".")]:
        del sys.modules[mod]
