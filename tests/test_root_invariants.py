"""Bare ``pytest`` at the repo root must stay green, and the injection must stay gone.

Two pre-split root-level invariants, re-pointed at the three plugin folders after
the split (the root ``__init__.py`` no longer exists):

- the synthetic-unit import shape: with no path argument pytest still forms the
  repo root as a package and imports a folder's ``__init__.py`` under the
  top-level name ``__init__``, where the relative submodule import has no parent
  package. That shape must import cleanly (package body skipped) without
  weakening the real package: the host loader shape (a ``hermes_plugins.<slug>``
  parent registered first) must still bind every submodule, and a genuine import
  failure outside the synthetic shape must still raise.
- the deleted swarm-split / inventory injection: no folder may register a
  ``pre_llm_call`` hook or expose an ``inventory_rule`` symbol.
"""

import importlib.util
import shutil
import sys
import tempfile
import textwrap
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

FOLDERS = {
    "shell-guards": ("shell_guards", ("terminal_policy",)),
    "workspace-guards": ("workspace_guards", ("workspace_write_gate", "home_litter_gate")),
    "kanban-guards": ("kanban_guards", ("stop_gate", "wakes", "workspace_scope_gate")),
}

SWARM_SPLIT_TEXT = "swarm-split reminder"
INVENTORY_TEXT = "inventory"


def _load(folder: Path, name: str, search: bool):
    init = folder / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        name, init, submodule_search_locations=[str(folder)] if search else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.mark.parametrize("folder_name", sorted(FOLDERS))
def test_the_synthetic_unit_import_survives(folder_name):
    folder = ROOT / folder_name
    module = _load(folder, "__init__", search=False)
    assert not hasattr(module, "stop_gate_unused_probe")


@pytest.mark.parametrize("folder_name", sorted(FOLDERS))
def test_the_host_loader_shape_still_binds_every_submodule(folder_name):
    package, subs = FOLDERS[folder_name]
    folder = ROOT / folder_name
    ns = "hermes_plugins"
    if ns not in sys.modules:
        parent = types.ModuleType(ns)
        parent.__path__ = []
        parent.__package__ = ns
        sys.modules[ns] = parent
    name = f"{ns}.{package}_regression"
    module = _load(folder, name, search=True)
    assert callable(module.register)
    for sub in subs:
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


@pytest.mark.parametrize("folder_name", sorted(FOLDERS))
def test_register_leaves_no_pre_llm_call_hook(folder_name):
    folder = ROOT / folder_name
    package = FOLDERS[folder_name][0]
    module = _load(folder, f"{package}_inject_probe", search=True)
    hooks = []
    ctx = types.SimpleNamespace(register_hook=lambda name, fn: hooks.append((name, fn)))
    module.register(ctx)
    pre_llm = [fn for name, fn in hooks if name == "pre_llm_call"]
    assert pre_llm == []
    assert "inventory_rule" not in vars(module)


@pytest.mark.parametrize("folder_name", sorted(FOLDERS))
def test_lead_turn_context_via_core_plugin_manager_has_no_injection(folder_name, monkeypatch, tmp_path):
    pytest.importorskip("hermes_cli.plugins")
    try:
        from hermes_cli.plugins_manifest import PluginManifest
    except Exception:
        pytest.skip("hermes_cli.plugins_manifest is unavailable")

    import hermes_cli.plugins as host_plugins

    folder = ROOT / folder_name
    package = FOLDERS[folder_name][0]
    module = _load(folder, f"{package}_inject_manager_probe", search=True)
    manifest = PluginManifest(name=folder_name, version="test", source="user", path=str(folder))
    manager = host_plugins.PluginManager(scope_key=str(tmp_path))
    ctx = host_plugins.PluginContext(manifest, manager)
    module.register(ctx)

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    payload = dict(
        session_id="lead-session", task_id="", turn_id="t-lead-1", user_message="delegate this",
        conversation_history=[], is_first_turn=True, model="m", platform="cli",
        parent_session_id="", sender_id="",
    )
    results = manager.invoke_hook("pre_llm_call", **payload)
    text = " ".join(str(r.get("context", "")) for r in results if isinstance(r, dict))
    assert SWARM_SPLIT_TEXT not in text
    assert INVENTORY_TEXT not in text
    assert "One narrow card per" not in text and "one Kanban card per unit" not in text
    assert results == []
