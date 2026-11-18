"""Skill markdown files carry no size cap: no block, no flag, no end-of-turn refusal."""

import sys
import types
from pathlib import Path

import yaml

guards = sys.modules["shell_guards"]

PLUGIN_DIR = Path(__file__).resolve().parents[1]
OVERSIZED = "---\nname: demo\ndescription: Use when testing.\n---\n" + "x" * 20000


def test_an_oversized_skill_write_is_not_blocked():
    for tool_name, args in (
        ("skill_manage", {"action": "create", "name": "demo", "content": OVERSIZED}),
        ("write_file", {"path": "/Users/x/.hermes/skills/demo/SKILL.md", "content": OVERSIZED}),
    ):
        assert guards._pre_tool_call(tool_name=tool_name, args=args, tool_call_id="c1") is None


def test_the_skill_size_module_is_gone():
    assert not (PLUGIN_DIR / "skill_size.py").exists()
    assert not hasattr(guards, "skill_size")


def test_pre_verify_is_not_registered():
    hooks = []
    guards.register(types.SimpleNamespace(register_hook=lambda name, fn: hooks.append(name)))
    assert "pre_verify" not in hooks
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert "pre_verify" not in manifest["hooks"]
    assert "pre_verify" not in manifest["provides_hooks"]


def test_the_manifest_description_names_no_skill_size_rule():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert "skill markdown" not in manifest["description"]
