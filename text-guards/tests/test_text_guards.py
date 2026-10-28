"""Tests for text-guards: the section sign never reaches the reply."""

import importlib.util
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"
FORBIDDEN = "\u00a7"


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


def test_no_character_leaves_the_reply_alone():
    mod = _load()
    assert mod.strip_forbidden("seccion 4.3, sin caracteres raros") is None


def test_handles_repeated_and_adjacent_characters():
    mod = _load()
    out = mod.strip_forbidden(f"{FORBIDDEN}1 y {FORBIDDEN}2 y {FORBIDDEN}{FORBIDDEN}3")
    assert FORBIDDEN not in out


def test_non_string_and_empty_inputs_pass_through():
    mod = _load()
    assert mod.strip_forbidden(None) is None
    assert mod.strip_forbidden("") is None


def test_register_wires_the_output_hook():
    mod = _load()
    seen = []

    class Ctx:
        def register_hook(self, name, callback):
            seen.append((name, callback))

    mod.register(Ctx())
    assert seen[0][0] == "transform_llm_output"
    assert seen[0][1] is mod.strip_forbidden
