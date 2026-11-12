# provider-guards

Recover narrowly identified upstream failures without changing Hermes core.

- HTTP 424 `byok_upstream_provider_failed` (or its existing upstream-error message)
  remains a retryable `server_error`. Hermes owns retries, backoff and fallback.
- Copilot HTTP 400 with **both** code `upstream_request_rejected` and the exact message
  `The model provider rejected the request.` becomes a non-retryable `server_error`
  with fallback enabled. This lets auxiliary recovery consult the configured chain
  immediately instead of suppressing it as a request-format failure.
- Other providers and other HTTP 400 errors, including explicit content-policy
  failures and parameter errors, stay with Hermes' classifier. The hook fails open.

The generic rejection does not reveal the underlying provider cause. This rule
permits recovery; it does not claim the primary request was valid or fix that route.
No extra retries, fallback routes or model configuration are introduced.

## E2E verification

Run with the installed Hermes Python, core and GitHub Copilot provider plugin:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /path/to/hermes/venv/bin/python -m pytest -q tests/test_provider_rejection_e2e.py
```

Override `HERMES_PROVIDER_E2E_CORE` and `HERMES_PROVIDER_E2E_COPILOT` when installed
elsewhere. The scenarios enter native title generation, load the real plugins,
use synthetic credentials in an isolated home, and inject responses only through
loopback HTTP. They verify persisted titles after reopening SessionDB, title
callbacks, primary/fallback request counts, exhausted fallback, healthy primary,
other provider, explicit policy rejection and unrelated parameter errors. External
network connections are denied. No real credentials or production state are used.
