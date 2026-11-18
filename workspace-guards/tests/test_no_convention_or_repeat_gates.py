"""workspace-guards carries neither the kanban workspace convention nor the repeat-block escalation."""

import sys
import types
from pathlib import Path

import yaml

guards = sys.modules["workspace_guards"]

PLUGIN_DIR = Path(__file__).resolve().parents[1]
BAD_WORKSPACE = {"workspace_path": "/tmp/somewhere/else", "workspace_kind": "dir"}


def test_a_kanban_create_outside_the_convention_is_not_blocked(monkeypatch):
    monkeypatch.setattr(guards._policy, "configured_worker", lambda: "worker")
    assert guards._pre_tool_call(tool_name="kanban_create", args=dict(BAD_WORKSPACE)) is None


def test_the_removed_modules_are_gone():
    for name in ("workspace_convention", "repeat_block_gate"):
        assert not (PLUGIN_DIR / f"{name}.py").exists(), name
        assert not hasattr(guards, name), name


def test_no_transform_tool_result_hook_is_registered():
    hooks = []
    guards.register(types.SimpleNamespace(register_hook=lambda name, fn: hooks.append(name)))
    assert hooks == ["pre_tool_call"]
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert manifest["hooks"] == ["pre_tool_call"]
    assert manifest["provides_hooks"] == ["pre_tool_call"]


def test_a_repeated_block_result_is_not_rewritten():
    assert not hasattr(guards, "_transform_tool_result")


def test_the_two_kept_gates_still_block():
    assert hasattr(guards.home_litter_gate, "check")
    assert hasattr(guards.plugin_local_edit_gate, "check")
