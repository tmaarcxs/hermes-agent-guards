"""Recover narrowly identified upstream failures without changing Hermes core.

HTTP 424 BYOK failures keep their existing retry/backoff behavior. Copilot's exact
HTTP 400 upstream_request_rejected response goes straight to configured fallback:
its generic rejection does not establish that the request has an invalid shape.
Other HTTP 400 responses, including explicit content-policy failures, stay with
Hermes' classifier. The hook fails open on malformed input.
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
        body = error_body if isinstance(error_body, dict) else {}
        upstream = body.get("error", body)
        if (status_code == 400 and kwargs.get("provider") == "copilot"
                and isinstance(upstream, dict) and upstream.get("code") == "upstream_request_rejected"
                and upstream.get("message") == "The model provider rejected the request."):
            return {
                "reason": "server_error",
                "retryable": False,
                "should_fallback": True,
                "message": "Copilot upstream rejected the request (HTTP 400): trying configured fallback.",
            }
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
