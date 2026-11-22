"""Tests for text-guards: the reply never carries the em dash or the section sign."""

import importlib.util
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"
EM = "\u2014"
SEC = "\u00a7"


def _load():
    spec = importlib.util.spec_from_file_location("text_guards_probe", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["text_guards_probe"] = module
    spec.loader.exec_module(module)
    return module


def test_a_spaced_dash_becomes_a_comma():
    mod = _load()
    assert mod.strip_forbidden(f"Miro si puedo usarlas {EM} ser\u00eda sin pedir nada") == (
        "Miro si puedo usarlas, ser\u00eda sin pedir nada"
    )


def test_a_pair_of_dashes_becomes_a_parenthetical_between_commas():
    mod = _load()
    assert mod.strip_forbidden(f"the guard {EM} not the plugin {EM} failed") == (
        "the guard, not the plugin, failed"
    )


def test_an_unspaced_dash_between_words_becomes_a_comma():
    mod = _load()
    assert mod.strip_forbidden(f"one{EM}two") == "one, two"


def test_a_dash_between_digits_becomes_a_hyphen():
    mod = _load()
    assert mod.strip_forbidden(f"weeks 43{EM}47") == "weeks 43-47"


def test_a_line_leading_dash_becomes_a_hyphen_bullet_with_its_indent():
    mod = _load()
    assert mod.strip_forbidden(f"list:\n{EM} first\n  {EM} nested") == "list:\n- first\n  - nested"


def test_a_dash_before_closing_punctuation_or_a_line_end_is_dropped():
    mod = _load()
    assert mod.strip_forbidden(f"wait {EM}.") == "wait."
    assert mod.strip_forbidden(f"wait {EM}\nnext") == "wait\nnext"
    assert mod.strip_forbidden(f"wait {EM}") == "wait"


def test_the_section_sign_is_replaced_by_a_single_space():
    mod = _load()
    assert mod.strip_forbidden(f"voir le runbook {SEC}2 pour le detail") == (
        "voir le runbook 2 pour le detail"
    )
    assert mod.strip_forbidden(f"{SEC}4 first") == "4 first"
    assert mod.strip_forbidden(f"a {SEC}{SEC}3 b") == "a 3 b"


def test_inline_code_and_fenced_blocks_are_left_alone():
    mod = _load()
    inline = f"the file holds `a {EM} b` literally"
    assert mod.strip_forbidden(inline) is None
    fenced = f"before\n```\nx {EM} y {SEC}1\n```\nafter"
    assert mod.strip_forbidden(fenced) is None
    mixed = f"prose {EM} here and `code {EM} stays` and more {EM} prose"
    assert mod.strip_forbidden(mixed) == "prose, here and `code " + EM + " stays` and more, prose"


def test_a_dash_right_after_a_code_span_is_not_taken_for_a_line_start():
    mod = _load()
    assert mod.strip_forbidden(f"both tokens are `gho_` {EM} of session") == "both tokens are `gho_`, of session"
    assert mod.strip_forbidden(f"`a` {EM} b `c` {EM} d") == "`a`, b `c`, d"


def test_a_dash_in_a_line_that_starts_with_a_code_span_still_cleans():
    mod = _load()
    assert mod.strip_forbidden(f"`x`\n{EM} item") == "`x`\n- item"


def test_a_dash_next_to_a_comma_or_a_cjk_character_leaves_no_double_comma():
    mod = _load()
    assert mod.strip_forbidden(f"changes{EM}, two banks") == "changes, two banks"
    assert mod.strip_forbidden(f"\u5df2\u751f\u6548{EM}\u4e24\u4e2a bank") == "\u5df2\u751f\u6548\u4e24\u4e2a bank"


def test_a_reply_without_the_characters_is_returned_as_none():
    mod = _load()
    assert mod.strip_forbidden("seccion 4.3, a - b and c") is None


def test_non_string_and_empty_inputs_pass_through():
    mod = _load()
    assert mod.strip_forbidden(None) is None
    assert mod.strip_forbidden("") is None
    assert mod.strip_forbidden(response_text=123) is None


def test_cleaning_is_idempotent():
    mod = _load()
    once = mod.strip_forbidden(f"a {EM} b {SEC}1 {EM}.")
    assert once is not None
    assert mod.strip_forbidden(once) is None


def test_the_hook_ignores_the_other_payload_fields():
    mod = _load()
    out = mod.strip_forbidden(response_text=f"a {EM} b", session_id="s", model="m", platform="p", turn_id="t")
    assert out == "a, b"


def test_the_plugin_registers_only_the_reply_transform_under_its_core_name():
    mod = _load()
    registered = []

    class Ctx:
        def register_hook(self, name, callback, **kwargs):
            registered.append((name, callback, kwargs))

    mod.register(Ctx())
    assert [name for name, _, _ in registered] == ["transform_llm_output"]
    assert registered[0][1] is mod.strip_forbidden
    assert registered[0][2] == {}


def test_the_hook_name_is_a_valid_core_hook():
    hermes = Path.home() / ".hermes" / "hermes-agent"
    if str(hermes) not in sys.path and (hermes / "hermes_cli" / "plugins.py").is_file():
        sys.path.append(str(hermes))
    try:
        from hermes_cli.plugins import VALID_HOOKS
    except Exception:
        return
    assert "transform_llm_output" in VALID_HOOKS
