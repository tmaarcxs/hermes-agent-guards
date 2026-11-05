"""Tests for provider-guards: a 424 from an upstream BYOK provider is a retryable server error."""

import importlib.util
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"
CODE = "byok_upstream_provider_failed"
MESSAGE = "Upstream provider returned an error."


def _load():
    spec = importlib.util.spec_from_file_location("provider_guards", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["provider_guards"] = module
    spec.loader.exec_module(module)
    return module


def classify(**kwargs):
    return _load().classify_upstream_error(**kwargs)


def test_424_with_the_byok_code_is_a_retryable_server_error():
    verdict = classify(status_code=424, error_code=CODE, error_message=MESSAGE)
    assert verdict["reason"] == "server_error"
    assert verdict["retryable"] is True
    assert verdict["should_fallback"] is True


def test_424_with_the_code_only_in_the_body_is_retryable():
    verdict = classify(status_code=424, error_code="", error_message="", error_body={"error": {"code": CODE}})
    assert verdict and verdict["retryable"] is True


def test_424_with_only_the_upstream_message_is_retryable():
    verdict = classify(status_code=424, error_code="", error_message="HTTP 424: Upstream provider returned an error.")
    assert verdict and verdict["reason"] == "server_error"


def test_a_424_about_something_else_is_left_to_the_core():
    assert classify(status_code=424, error_code="context_length_exceeded", error_message="too long") is None


def test_other_statuses_are_left_to_the_core():
    for status in (400, 401, 403, 429, 500, 502, 503, None):
        assert classify(status_code=status, error_code=CODE, error_message=MESSAGE) is None


def test_garbage_input_never_raises():
    assert classify(status_code="424", error_code=None, error_message=None, error_body="x") is None
    assert classify() is None
    assert classify(status_code=424, error_code=7, error_message=object(), error_body=[1]) is None


def test_the_hook_is_registered_under_its_core_name():
    mod = _load()
    seen = {}

    class Ctx:
        def register_hook(self, name, fn):
            seen[name] = fn

    mod.register(Ctx())
    assert list(seen) == ["transform_api_error_classification"]
    assert seen["transform_api_error_classification"](status_code=424, error_code=CODE)["retryable"] is True
