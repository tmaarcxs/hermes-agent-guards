# Token efficiency

A standalone default-profile plugin. It changes only newly returned native tool-result strings. It never rewrites commands, environment isolation, past messages, tool schemas or the system prompt.

## Lossless JSON packing

The transformer validates a complete JSON object or array and removes only JSON whitespace outside string literals. Numeric spellings, string contents, escapes, key order and all fields remain unchanged. Duplicate keys, nonstandard numeric constants, malformed JSON, scalar/non-JSON text and inputs over 4 MiB pass through unchanged. The size bound disables packing; it never clips output. Internal errors fail open.

No recovery pointer, custom encoding or extra model call is added. Shorter bytes do not guarantee fewer tokens on every individual payload. No provider saving percentage is claimed.

Advisory dependency ordering places packing after shell-guards and workspace-guards. The host stops at the first replacement, so mandatory rewrites and repeat-block tracking must see the original result first. Missing guard plugins do not prevent standalone loading.

## Temporary, content-free observation

Observation is off by default. Optional plugin settings:

```yaml
plugins:
  entries:
    hermes-agent-guards/token-efficiency:
      settings:
        observe_requests: true
        request_budget: 2500
        observation_run: once
```

The observer admits requests for at most 12 hours or the request budget, whichever comes first. The profile-private SQLite ledger preserves both limits across reloads/processes; changing `observation_run` explicitly starts a new diagnostic window. Already-admitted requests may emit their final usage. The budget must be an integer from 1 through 2500. The optional `llm_execution` middleware fingerprints the complete structured SDK request without changing its input, response, exception or transport call count. It unregisters itself when admission ends. Transports that bypass this middleware are not observed. HMAC session/route references and previous-input prefix hashes allow append-only growth to be distinguished from changes to retained input. It never logs request content, credentials, raw errors or the HMAC key. A fingerprint epoch prevents comparison across module reloads. Usage values remain provider-reported; no fresh/cache arithmetic is inferred.

`sdk_request` means the complete structured SDK input, **not the provider's serialized/tokenized prefix**. Fingerprints do not prove server-side cache reuse. Post-compaction records retain only the public kind and integer core estimates; those estimates are not provider-confirmed token counts. Observation adds no prompt content or model calls.

## Deliberately unsupported here

The host's inline ToolSearch dispatcher bypasses `transform_tool_result`. Consequently this plugin does not replace `tool_describe` with an unchanged-schema receipt. A native-result transformer cannot safely implement that feature by remembering a previous response alone.

`ContextEngine.select_context` affects request copies, not persisted history. A persistent formatter would require additional state and rehydration guarantees. Selecting an external context engine also changes ownership of compression policy: the host does not forward its normal threshold/auto-raise policy. That is not a behavior-preserving replacement and is outside this plugin's activation proposal.

Completed prepared summaries are owned by the host agent. The public post-compaction hook is observational; it supplies no pre-discard veto or completed-candidate transfer/adoption operation. This plugin neither patches that lifecycle nor claims to fix the historical nonshrinking post-commit requests. No private-method override or runtime monkeypatch is used.

## Rollback

Disable `hermes-agent-guards/token-efficiency` through the supported plugin CLI. New results return to native serialization and telemetry stops. Past context and stored notes are not rewritten. Restoring the previous plugin configuration disables observation without changing models or thresholds.
