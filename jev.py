"""Jev (TypeSafe decision-model) client for agent-guards.

One small, stable function: ``decide(state, questions, timeout)``. Reads the
9router base URL and Bearer key from the profile config
(``providers.9router``); the key is never logged or persisted. Any failure —
missing config, bad request, timeout, non-200, malformed body — returns
``None`` (fail open): a decision tool that cannot answer must never block a
worker on its own. Every call — success or failure, with the state truncated
and no key material — is appended to ``~/.hermes/state/jev-decisions.jsonl``
so shadow-mode decisions and calibration runs stay auditable.

Config resolution (``_config``) must work in every process that loads the
plugin, not only in ones that can import ``hermes_cli``:

1. ``hermes_cli.config.load_config_readonly`` is tried first when importable —
   it is the authoritative resolver (profile scoping, env expansion, managed
   overlay).
2. When the import fails (standalone scripts, cron ``--script`` jobs, probe
   interpreters whose ``sys.path`` has no hermes tree — the calibration runs
   that produced 263/456 ``missing_config`` rows), the profile config file is
   read directly: ``$HERMES_HOME/config.yaml`` when set, else
   ``~/.hermes/config.yaml``, parsed with PyYAML (a Nox-free, always-present
   dependency in these contexts) — same precedence Hermes's own fallback.

Shared module: other guards import ``decide``; keep the interface exactly
this small.
"""

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

MODEL = "openrouter/typesafe/jev-1.13"
ENDPOINT = "/v1/systemone"
_LOG_PATH = os.path.expanduser("~/.hermes/state/jev-decisions.jsonl")
_STATE_TRUNCATE = 400


def _config() -> Optional[tuple[str, str]]:
    provider: dict[str, Any] = {}
    # Authoritative path first: hermes's own loader (profile-scoped, env-expanded).
    try:
        from hermes_cli.config import load_config_readonly

        provider = ((load_config_readonly() or {}).get("providers") or {}).get("9router") or {}
    except Exception:
        provider = {}
    if not (provider.get("api") and provider.get("api_key")):
        # Direct-file fallback: same precedence the loader itself uses when a
        # process cannot import the hermes package (HERMES_HOME -> ~/.hermes).
        home = os.environ.get("HERMES_HOME", "").strip() or os.path.expanduser("~/.hermes")
        try:
            import yaml

            with open(os.path.join(home, "config.yaml"), encoding="utf-8-sig") as handle:
                data = yaml.safe_load(handle) or {}
            if isinstance(data, dict):
                provider = ((data.get("providers") or {}).get("9router") or {})
        except Exception:
            provider = {}
    url = str(provider.get("api") or "").rstrip("/")
    key = str(provider.get("api_key") or "")
    if url and key:
        return url, key
    return None


def _log(entry: dict) -> None:
    """Append one decision record. Never raises: logging must not break the caller."""
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def decide(
    state: str,
    questions: dict,
    timeout: float = 2.0,
    caller: str = "",
    context: Optional[dict] = None,
    gate_verdict: Optional[str] = None,
    action: Optional[str] = None,
) -> Optional[dict]:
    """Ask Jev one decision. Returns the full response dict (``answers`` inside)
    or ``None`` on any error, timeout, non-200 or malformed body (fail open).

    ``context`` carries card/session ids the caller wants joinable to later
    outcomes; ``gate_verdict`` and ``action`` record what the caller did with
    the answer, so shadow rows can be labelled without guessing. Neither ever
    contains secret material — they are strings the caller constructs.
    """
    entry: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "caller": caller,
        "state": (state or "")[:_STATE_TRUNCATE],
        "questions": questions,
    }
    if context:
        entry["context"] = {str(k): str(v)[:200] for k, v in context.items() if v}
    if gate_verdict is not None:
        entry["gate_verdict"] = gate_verdict
    if action is not None:
        entry["action"] = action
    result = None
    latency = None
    started = time.time()
    config = _config()
    if config is None:
        entry["error"] = "missing_config"
    else:
        url, key = config
        body = json.dumps({"model": MODEL, "state": state or "", "questions": questions}).encode("utf-8")
        request = urllib.request.Request(
            url + ENDPOINT,
            data=body,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            answers = payload.get("answers")
            if isinstance(answers, dict):
                result = payload
                entry["answer_verdict"] = _answers_verdict(payload)
            else:
                entry["error"] = "malformed_body"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            entry["error"] = type(exc).__name__
        latency = round(time.time() - started, 3)
    if latency is not None:
        entry["latency_s"] = latency
    if result is not None:
        answer_view = {
            question: dict(answer) if isinstance(answer, dict) else answer
            for question, answer in result.get("answers", {}).items()
        }
        entry["answers"] = answer_view
        entry["confidence"] = {
            question: answer.get("confidence")
            for question, answer in result.get("answers", {}).items()
            if isinstance(answer, dict)
        }
    _log(entry)
    return result


def _answers_verdict(payload: dict) -> str:
    """Flatten the first answer into a short verdict string for log readability."""
    answers = payload.get("answers") or {}
    for answer in answers.values():
        if isinstance(answer, dict):
            choice = answer.get("choice") or answer.get("value") or answer.get("answer")
            if choice is not None:
                return str(choice)
            if answer.get("type") == "scale":
                value = answer.get("value")
                return str(value) if value is not None else "scale"
    return ""
