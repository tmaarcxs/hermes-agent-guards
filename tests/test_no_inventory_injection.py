"""The swarm-split injection must stay gone: no ``pre_llm_call`` hook and no context.

The deleted guard injected a 927-byte swarm-split instruction into every lead
user turn while SOUL and the task protocol already carry the same rule stronger
(measured ~13% follow-through at 927 bytes per fresh lead turn over 14 days).
These tests pin the deletion from the state the plugin leaves behind, including
the live host registry a real load would build.

The registry test needs the Hermes host's ``hermes_cli`` importable; it is
skipped, never failed, when the host is missing (see conftest.py).
"""

import os
import sys

import pytest

guards = sys.modules["agent_guards"]

SWARM_SPLIT_TEXT = "swarm-split reminder"
INVENTORY_TEXT = "inventory"


def test_register_leaves_no_pre_llm_call_hook():
    import types

    hooks = []
    ctx = types.SimpleNamespace(register_hook=lambda name, fn: hooks.append((name, fn)))
    guards.register(ctx)
    pre_llm = [fn for name, fn in hooks if name == "pre_llm_call"]
    assert pre_llm == []
    assert "inventory_rule" not in vars(guards)


def test_lead_turn_context_via_core_plugin_manager_has_no_injection(monkeypatch, tmp_path):
    hermes_cli = pytest.importorskip("hermes_cli.plugins")
    try:
        from hermes_cli.plugins_manifest import PluginManifest
    except Exception:
        pytest.skip("hermes_cli.plugins_manifest is unavailable")

    import hermes_cli.plugins as host_plugins

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    manifest = PluginManifest(name="agent-guards", version="test", source="user", path=repo_root)
    manager = host_plugins.PluginManager(scope_key=str(tmp_path))
    ctx = host_plugins.PluginContext(manifest, manager)
    guards.register(ctx)

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
