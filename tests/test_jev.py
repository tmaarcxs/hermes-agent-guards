"""Fail-open and audit tests for the shared Jev client (``jev.decide``).

The network is never touched: ``urllib.request.urlopen`` is monkeypatched to
raise or return canned bodies, so each failure mode is deterministic. The
decision log goes to a temp file, and its content is asserted to carry no API
key material.
"""

import importlib
import json
import sys
import types
import urllib.error
import urllib.request

import pytest

guards = sys.modules["agent_guards"]
jev = importlib.import_module("agent_guards.jev", package="agent_guards")

QUESTIONS = {
    "q": {
        "type": "choice",
        "instructions": "done?",
        "criteria": {"done": "all finished", "partial": "some pending", "failed": "broken"},
    }
}

RESPONSE = json.dumps(
    {
        "model": "typesafe/jev-1.13",
        "answers": {"q": {"type": "choice", "choice": "done", "probabilities": {"done": 1}, "confidence": 1}},
    }
).encode()


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def fake_hermes_cli(monkeypatch, config):
    """Make ``hermes_cli`` importable with the given resolved config, or unimportable with ``config=None``."""
    if config is None:
        # A process that cannot import hermes at all (standalone scripts, cron
        # --script jobs, probe interpreters): the import itself must fail.
        import builtins

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "hermes_cli" or name.startswith("hermes_cli."):
                raise ModuleNotFoundError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        return None
    fake = types.ModuleType("hermes_cli")
    fake.__path__ = []  # allow `from hermes_cli.config import ...` to resolve via sys.modules
    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.load_config_readonly = lambda: config
    monkeypatch.setitem(sys.modules, "hermes_cli", fake)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)
    return fake_config


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(jev, "_LOG_PATH", str(tmp_path / "jev-decisions.jsonl"))
    fake_hermes_cli(monkeypatch, {"providers": {"9router": {"api": "http://router.example", "api_key": "sk-secret"}}})


def logged(tmp_path):
    path = tmp_path / "jev-decisions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def write_profile_config(monkeypatch, tmp_path, text: str) -> str:
    """Create a temp HERMES_HOME with the given config.yaml and point the env at it."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return str(home)


def test_success_parses_answers_and_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: FakeResponse(RESPONSE))
    decision = jev.decide("state text", QUESTIONS, caller="test")
    assert decision["answers"]["q"]["choice"] == "done"
    records = logged(tmp_path)
    assert len(records) == 1
    assert records[0]["caller"] == "test"
    assert records[0]["answers"]["q"]["choice"] == "done"
    text = json.dumps(records)
    assert "sk-secret" not in text and "Authorization" not in text


def test_timeout_fails_open_and_logs(monkeypatch, tmp_path):
    def boom(req, timeout):
        raise TimeoutError("too slow")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert jev.decide("state", QUESTIONS) is None
    assert logged(tmp_path)[0]["error"] == "TimeoutError"


def test_http_error_fails_open_and_logs(monkeypatch, tmp_path):
    def boom(req, timeout):
        raise urllib.error.HTTPError("url", 503, "gone", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert jev.decide("state", QUESTIONS) is None
    assert logged(tmp_path)[0]["error"] == "HTTPError"


def test_malformed_body_fails_open_and_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: FakeResponse(b'{"answers": 3}'))
    assert jev.decide("state", QUESTIONS) is None
    assert logged(tmp_path)[0]["error"] == "malformed_body"


def test_unparsable_body_fails_open_and_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: FakeResponse(b"not json"))
    assert jev.decide("state", QUESTIONS) is None
    assert logged(tmp_path)[0]["error"] in ("JSONDecodeError", "ValueError")


def test_missing_config_fails_open_and_logs(monkeypatch, tmp_path):
    fake_hermes_cli(monkeypatch, None)  # hermes unimportable, AND no fallback config below
    write_profile_config(monkeypatch, tmp_path, "providers: {}\n")
    assert jev.decide("state", QUESTIONS) is None
    assert logged(tmp_path)[0]["error"] == "missing_config"


def test_fallback_reads_the_profile_config_when_hermes_is_unimportable(monkeypatch, tmp_path):
    """Regression: guard processes that cannot import hermes_cli resolved no config at
    all (263 of the first 456 decision rows failed missing_config). With the import
    blocked and the profile config file present, the direct-file fallback must supply
    providers.9router."""
    fake_hermes_cli(monkeypatch, None)  # hermes unimportable: the moat that killed the old path
    write_profile_config(
        monkeypatch,
        tmp_path,
        "providers:\n  9router:\n    api: http://router.example/v1\n    api_key: sk-something-secret\n",
    )
    url_key = jev._config()
    assert url_key == ("http://router.example/v1", "sk-something-secret")


def test_fallback_prefers_hermes_loader_when_importable(monkeypatch, tmp_path):
    """The hermes loader stays authoritative: when importable it wins over the file."""
    fake_hermes_cli(monkeypatch, {"providers": {"9router": {"api": "http://loader.example/v1", "api_key": "sk-loader"}}})
    write_profile_config(
        monkeypatch,
        tmp_path,
        "providers:\n  9router:\n    api: http://file.example/v1\n    api_key: sk-file\n",
    )
    assert jev._config() == ("http://loader.example/v1", "sk-loader")


def test_fallback_skips_incomplete_provider(monkeypatch, tmp_path):
    fake_hermes_cli(monkeypatch, None)
    write_profile_config(
        monkeypatch,
        tmp_path,
        "providers:\n  9router:\n    api: http://router.example/v1\n",  # api without key
    )
    assert jev._config() is None


def test_log_failure_never_breaks_the_call(monkeypatch, tmp_path):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: FakeResponse(RESPONSE))
    monkeypatch.setattr(jev, "_LOG_PATH", "/proc/definitely/not/writable/jev.jsonl")
    assert jev.decide("state", QUESTIONS)["answers"]["q"]["choice"] == "done"


def test_state_is_truncated_in_the_log(monkeypatch, tmp_path):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: FakeResponse(RESPONSE))
    jev.decide("x" * 5000, QUESTIONS)
    assert len(logged(tmp_path)[0]["state"]) == 400
