# provider-guards

Answers: *does a provider failure that the core calls permanent get retried?*

- `transform_api_error_classification` classifies HTTP 424 `byok_upstream_provider_failed`
  ("Upstream provider returned an error") as a retryable `server_error`. The core then retries with
  its own backoff in the same session, and consults the fallback chain once the retries are spent.
- Without it the core reads an unlisted 4xx as a non-retryable format error: the turn ends on the
  first attempt and a Kanban worker run dies with its work.
- Any other status, error code or message is left to the core. Fails open on any error.

Raise `agent.api_max_retries` (the default is 3) to give the retries more time: the backoff is 2 s
doubling to a 60 s cap, so 6 retries wait about two minutes in total.
