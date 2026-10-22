"""Pin which plugin's message wins when two plugins block the same call.

The host walks the registered ``pre_tool_call`` callbacks in plugin load order
and returns at the FIRST ``block`` directive (``hermes_cli/plugins.py``
``_get_pre_tool_call_directive_details``: ``if action == "block": return ...``).
Callback order is plugin load order, which is alphabetical by plugin key with
``requires_plugins`` edges first (``hermes_cli/plugins_manifest.py``
``resolve_plugin_load_order``).

Two plugins can block the same call: ``rm -rf ~/.hermes`` /
``rm -rf $HERMES_HOME`` is blocked by shell-guards (``hermes_home_gate`` via
``terminal_policy``) and by workspace-guards (``workspace_write_gate``). With
``kanban-guards`` < ``shell-guards`` < ``workspace-guards`` the shell-guards
message wins and the workspace-guards one is never reached. This pins that
winner so a future reorder cannot silently flip which message an operator sees.
"""

from _plugin_loader import load_plugin, ROOT

PLUGINS = ("kanban-guards", "shell-guards", "workspace-guards")

COMMANDS = ("rm -rf ~/.hermes", "rm -rf $HERMES_HOME")


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


def _host_first_block(order, plugins, command):
    """Simulate the host: walk plugins in load order, return the first block."""
    for name in order:
        result = plugins[name]._pre_tool_call(
            tool_name="terminal", args={"command": command, "background": False}
        )
        if isinstance(result, dict) and result.get("action") == "block":
            return name, result["message"]
    return None, None


def test_shell_guards_loads_before_workspace_guards():
    order = _host_load_order(list(PLUGINS))
    assert order.index("shell-guards") < order.index("workspace-guards"), (
        "shell-guards must load before workspace-guards so its hermes_home_gate "
        f"message is the survivor; host order was {order}"
    )


def test_shell_guards_block_message_wins_over_workspace_guards(monkeypatch):
    """The first plugin to block supplies the message the operator reads."""
    order = _host_load_order(list(PLUGINS))
    kanban = load_plugin("kanban-guards")
    shell = load_plugin("shell-guards")
    ws = load_plugin("workspace-guards")

    monkeypatch.setattr(ws.workspace_write_gate, "is_worker", lambda: True)

    plugins = {"kanban-guards": kanban, "shell-guards": shell, "workspace-guards": ws}

    for command in COMMANDS:
        shell_msg = shell._pre_tool_call(
            tool_name="terminal", args={"command": command, "background": False}
        )["message"]
        ws_msg = ws._pre_tool_call(
            tool_name="terminal", args={"command": command, "background": False}
        )["message"]
        assert "hermes_home_guard" in shell_msg and "[__init__]" in shell_msg, shell_msg
        assert "[workspace_write_gate]" in ws_msg and "protected path" in ws_msg, ws_msg
        assert shell_msg != ws_msg

        winner_name, winner_msg = _host_first_block(order, plugins, command)
        assert winner_name == "shell-guards", (
            f"host order {order} must let shell-guards block first for {command!r}; "
            f"got {winner_name!r}"
        )
        assert winner_msg == shell_msg, (
            f"the survivor for {command!r} must be the shell-guards message; got {winner_msg!r}"
        )
        assert winner_msg != ws_msg
