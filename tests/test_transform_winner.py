"""Pin which plugin wins when two plugins both transform the same tool result.

The host keeps the FIRST non-None string a ``transform_tool_result`` callback
returns and drops the rest (``model_tools.py`` ``_apply_transform_tool_result_hook``:
``next((r for r in hook_results if isinstance(r, str)), result)``). Callback
order is plugin load order, which is alphabetical by plugin key with
``requires_plugins`` edges first (``hermes_cli/plugins_manifest.py``
``resolve_plugin_load_order``).

With the three-plugin split that makes ``kanban-guards`` load before
``shell-guards``, so when a single call would be transformed by both (a lead
turn that crosses the nudge threshold AND leaves an oversized skill file), the
kanban-guards transform wins and the shell-guards one is dropped. The pre-split
monolith ran both inside one callback and chained them (skill_size first, then
the nudge on its output). This test pins the new, narrower behaviour so a future
reorder cannot silently flip the survivor.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

PLUGINS = {
    "kanban-guards": ROOT / "kanban-guards",
    "shell-guards": ROOT / "shell-guards",
    "workspace-guards": ROOT / "workspace-guards",
}


def _load(name: str, folder: Path):
    module_name = name.replace("-", "_")
    for mod in [m for m in list(sys.modules) if m == module_name or m.startswith(module_name + ".")]:
        del sys.modules[mod]
    spec = importlib.util.spec_from_file_location(
        module_name, folder / "__init__.py", submodule_search_locations=[str(folder)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        pass
    return module


def _host_load_order(names):
    """The host's order: requires_plugins edges first, alphabetical ties."""
    try:
        from hermes_cli.plugins_manifest import PluginManifest, resolve_plugin_load_order

        manifests = {
            name: PluginManifest(name=name, version="test", source="user", path=str(PLUGINS[name]))
            for name in names
        }
        return list(resolve_plugin_load_order(manifests))
    except Exception:
        return sorted(names)


def test_kanban_guards_loads_before_shell_guards():
    order = _host_load_order(list(PLUGINS))
    assert order.index("kanban-guards") < order.index("shell-guards"), (
        "kanban-guards must load before shell-guards so its transform is the "
        f"survivor; host order was {order}"
    )


def test_first_non_none_string_wins_in_host_order():
    """Simulate the host's transform resolution over both plugins' callbacks."""
    order = _host_load_order(list(PLUGINS))
    kanban = _load("kanban-guards", PLUGINS["kanban-guards"])
    shell = _load("shell-guards", PLUGINS["shell-guards"])

    sentinel_kanban = "KANBAN-TRANSFORM-WINS"
    sentinel_shell = "SHELL-TRANSFORM-WINS"

    original_nudge = kanban.lead_work_nudge.hook
    original_skill_transform = shell.skill_size.transform
    kanban.lead_work_nudge.hook = lambda **kwargs: sentinel_kanban
    shell.skill_size.transform = lambda *a, **k: sentinel_shell
    try:
        callbacks = {
            "kanban-guards": [kanban._transform_tool_result],
            "shell-guards": [shell._transform_tool_result],
        }
        results = []
        for name in order:
            for fn in callbacks.get(name, []):
                results.append(fn(tool_name="terminal", args={}, result="BASE", session_id="s", turn_id="t"))
        winner = next((r for r in results if isinstance(r, str)), "BASE")
    finally:
        kanban.lead_work_nudge.hook = original_nudge
        shell.skill_size.transform = original_skill_transform

    assert winner == sentinel_kanban, (
        "with the three folders merged the kanban-guards transform must win the "
        f"first-string race; got {winner!r} from order {order}"
    )
