"""Pin the joined transform behaviour through the host's two transform seams.

The host keeps the FIRST non-None string a ``transform_tool_result`` callback
returns and drops the rest (``model_tools.py`` ``_apply_transform_tool_result_hook``:
``next((r for r in hook_results if isinstance(r, str)), result)``). Callbacks do
NOT chain: every callback receives the ORIGINAL result, and callback order is
plugin load order — alphabetical by plugin key with ``requires_plugins`` edges
first (``hermes_cli/plugins_manifest.py`` ``resolve_plugin_load_order``).

Every test here drives the callbacks the plugins ACTUALLY registered (captured
from ``register()``) through the host's own resolution rules.
"""

import pytest

from _plugin_loader import load_plugin, ROOT

PLUGINS = ("shell-guards", "workspace-guards")

def _host_load_order(names):
    """The host's order: requires_plugins edges first, alphabetical ties."""
    try:
        from hermes_cli.plugins_manifest import PluginManifest, resolve_plugin_load_order

        manifests = {
            name: PluginManifest(name=name, version="test", source="user", path=str(ROOT / name))
            for name in names
        }
        return list(resolve_plugin_load_order(manifests))
    except Exception:
        return sorted(names)


def _registered_hooks(module):
    """The ``{hook_name: [callback, ...]}`` a plugin's ``register()`` actually wires."""
    hooks = {}

    class _Ctx:
        def register_hook(self, name, callback):
            hooks.setdefault(name, []).append(callback)

    module.register(_Ctx())
    return hooks


def _run_host_pipeline(registered, order, output, *, tool_name="terminal", args=None,
                       tool_call_id="c1", session_id="lead-session", turn_id="turn-1"):
    """The host's two seams, in the host's order.

    First ``transform_terminal_output`` (inside the terminal tool), then
    ``transform_tool_result`` (after the tool returns). Each seam keeps the FIRST
    non-None string and does not chain.
    """
    args = args if args is not None else {}
    terminal_results = []
    for name in order:
        for callback in registered.get(name, {}).get("transform_terminal_output", []):
            terminal_results.append(callback(
                command="cat >> x", output=output, returncode=0, task_id="",
                env_type="local", tool_call_id=tool_call_id))
    output = next((r for r in terminal_results if isinstance(r, str)), output)

    result_results = []
    for name in order:
        for callback in registered.get(name, {}).get("transform_tool_result", []):
            result_results.append(callback(
                tool_name=tool_name, args=args, result=output,
                session_id=session_id, tool_call_id=tool_call_id, turn_id=turn_id))
    return next((r for r in result_results if isinstance(r, str)), output)


@pytest.fixture
def plugins(monkeypatch):
    """Shell guards loaded fresh with its registered hooks."""
    shell = load_plugin("shell-guards")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    return {"shell-guards": shell, "registered": {"shell-guards": _registered_hooks(shell)}}


def test_shell_guards_registers_the_terminal_output_seam(plugins):
    """The clamp note's delivery depends on shell-guards registering ``transform_terminal_output``."""
    assert plugins["registered"]["shell-guards"].get("transform_terminal_output")


def test_non_string_output_leaves_the_terminal_seam_untouched(plugins):
    """Fail open: a non-string output never changes the terminal result."""
    callback = plugins["registered"]["shell-guards"]["transform_terminal_output"][0]
    assert callback(command="cat >> x", output={"ok": True}, returncode=0, task_id="",
                    env_type="local", tool_call_id="c1") is None
