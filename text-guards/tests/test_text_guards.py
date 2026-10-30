"""Tests for text-guards: the forbidden characters never reach the reply or a tool argument."""

import importlib.util
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"
FORBIDDEN = "\u00a7"
EM_DASH = "\u2014"


def _load():
    spec = importlib.util.spec_from_file_location("text_guards", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["text_guards"] = module
    spec.loader.exec_module(module)
    return module


def test_removes_the_character_and_keeps_words_apart():
    mod = _load()
    assert mod.strip_forbidden(f"voir le runbook {FORBIDDEN}2 pour le detail") == (
        "voir le runbook 2 pour le detail"
    )


def test_removes_the_em_dash():
    mod = _load()
    assert mod.strip_forbidden(f"two things block this {EM_DASH} both in config") == (
        "two things block this both in config"
    )
    assert mod.strip_forbidden(f"a {EM_DASH}b{EM_DASH} c") == "a b c"


def test_no_character_leaves_the_reply_alone():
    mod = _load()
    assert mod.strip_forbidden("seccion 4.3, sin caracteres raros") is None
    assert mod.strip_forbidden("a - b and c d") is None  # hyphen and en dash stay


def test_handles_repeated_and_adjacent_characters():
    mod = _load()
    out = mod.strip_forbidden(f"{FORBIDDEN}1 y {FORBIDDEN}2 y {FORBIDDEN}{FORBIDDEN}3")
    assert FORBIDDEN not in out


def test_non_string_and_empty_inputs_pass_through():
    mod = _load()
    assert mod.strip_forbidden(None) is None
    assert mod.strip_forbidden("") is None


def test_tool_arguments_are_rewritten_before_the_call():
    mod = _load()
    out = mod.rewrite_arguments(
        tool_name="write_file",
        args={"path": "doc.md", "content": f"guia {FORBIDDEN}4.3 y {FORBIDDEN}5"},
    )
    assert out["action"] == "modify"
    assert out["args"]["content"] == "guia 4.3 y 5"
    assert out["args"]["path"] == "doc.md"


def test_tool_arguments_with_an_em_dash_are_rewritten():
    mod = _load()
    out = mod.rewrite_arguments(
        tool_name="write_file",
        args={"path": "rev.md", "content": f"verdict {EM_DASH} reason here"},
    )
    assert out["action"] == "modify"
    assert out["args"]["content"] == "verdict reason here"


def test_nested_arguments_are_cleaned():
    mod = _load()
    out = mod.rewrite_arguments(
        args={"body": {"cells": [f"a{FORBIDDEN}b", 3, None, {"deep": f"{FORBIDDEN}c"}]}}
    )
    assert out["args"]["body"]["cells"] == ["a b", 3, None, {"deep": " c"}]


def test_clean_arguments_are_not_touched():
    mod = _load()
    assert mod.rewrite_arguments(args={"content": "texto normal", "n": 1}) is None
    assert mod.rewrite_arguments(args=None) is None
    assert mod.rewrite_arguments(args="not a dict") is None


def test_register_wires_both_hooks():
    mod = _load()
    seen = []

    class Ctx:
        def register_hook(self, name, callback):
            seen.append((name, callback))

    mod.register(Ctx())
    assert [name for name, _ in seen] == ["transform_llm_output", "pre_tool_call"]
    assert seen[0][1] is mod.strip_forbidden
    assert seen[1][1] is mod.rewrite_arguments
