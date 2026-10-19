"""Bare ``pytest`` at the repo root must stay green.

With no path argument pytest honors ``pytest.ini`` ``testpaths`` for collection
 BUT still forms the root as a package and imports ``__init__.py`` under the
top-level name ``__init__``, where the relative submodule import has no parent
package. The synthetic shape must import cleanly (package body skipped) without
weakening the real package: the host loader shape (``hermes_plugins.<slug>``
parent registered first) must still bind every submodule, and a genuine import
failure outside the synthetic shape must still raise.
"""

import importlib.util
import shutil
import sys
import tempfile
import textwrap
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "__init__.py"


def _load(name: str, search: bool):
    spec = importlib.util.spec_from_file_location(
        name, INIT, submodule_search_locations=[str(ROOT)] if search else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_the_synthetic_unit_import_survives_and_binds_nothing():
    module = _load("__init__", search=False)
    assert not hasattr(module, "stop_gate_unused_probe")


def test_the_host_loader_shape_still_binds_every_submodule():
    ns = "hermes_plugins"
    if ns not in sys.modules:
        parent = types.ModuleType(ns)
        parent.__path__ = []
        parent.__package__ = ns
        sys.modules[ns] = parent
    name = ns + ".agent_guards_regression"
    module = _load(name, search=True)
    assert callable(module.register)
    for sub in ("stop_gate", "wakes", "workspace_scope_gate"):
        assert getattr(module, sub) is not None


def test_a_genuine_import_failure_outside_the_synthetic_shape_still_raises():
    tmp = Path(tempfile.mkdtemp())
    try:
        pkg = tmp / "bgbroken"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(textwrap.dedent("""
            try:
                from . import nosuchmodule
            except ImportError:
                if __name__ != "__init__":
                    raise
            """).lstrip())
        sys.path.insert(0, str(tmp))
        try:
            import bgbroken  # noqa: F401
        except ImportError:
            pass
        else:
            raise AssertionError("broken package import outside the synthetic shape was swallowed")
    finally:
        sys.path.remove(str(tmp))
        shutil.rmtree(tmp, ignore_errors=True)
