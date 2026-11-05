"""Treat an upstream BYOK provider failure (HTTP 424) as a retryable server error.

GitHub Copilot answers ``HTTP 424`` with ``byok_upstream_provider_failed`` and the message
"Upstream provider returned an error" when the model behind it fails. The core classifier knows
only a few 4xx codes: any other 4xx becomes a non-retryable ``format_error``, so a 424 ends the
turn on the first attempt (``attempt 1/3, not retryable``) and a Kanban worker run dies with all
its work. Measured on one host over six weeks: 90 runs and 34.4 h lost, and most of the cards
completed when re-queued seconds later. This hook runs through ``transform_api_error_classification``
before the core classifier and returns ``server_error`` with ``retryable``, so the core retries with
its own backoff inside the same session and, once the retries are spent, still consults the fallback
chain. Any other status, code or message is left to the core. Fails open on any error.
"""

from typing import Any, Optional

_CODE = "byok_upstream_provider_failed"
_MESSAGE = "upstream provider returned an error"
_VERDICT = {
    "reason": "server_error",
    "retryable": True,
    "should_fallback": True,
    "message": "Upstream provider failed (HTTP 424): retrying.",
}


def _body_code(body: Any) -> str:
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code.lower() if isinstance(code, str) else ""


def classify_upstream_error(
    status_code: Any = None, error_code: Any = None, error_message: Any = None, error_body: Any = None, **kwargs: Any
) -> Optional[dict]:
    try:
        if status_code != 424:
            return None
        code = error_code.lower() if isinstance(error_code, str) else ""
        message = error_message.lower() if isinstance(error_message, str) else ""
        if _CODE in (code, _body_code(error_body)) or _MESSAGE in message:
            return dict(_VERDICT)
    except Exception:
        return None
    return None


def register(ctx) -> None:
    ctx.register_hook("transform_api_error_classification", classify_upstream_error)


__all__ = ["register", "classify_upstream_error"]
