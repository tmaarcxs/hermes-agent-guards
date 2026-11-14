# Hindsight reviewer

Reviews injected and tool-recalled memories after an eligible gateway turn. The proposal, independent confirmation, exact evidence, fresh primary-fact read, reversible invalidation and readback gates remain unchanged. A newer admitted message cancels unfinished writes.

## Remote Desktop delivery (serve/WebSocket)

Version 0.14.0 also recognizes the legacy `hermes serve` Desktop route. It binds the
loaded server record by task-local UI id, route key, transcript id and profile home;
requires one running agent and a live transport; and captures only that agent’s
`review.summary` callback. A label alone never admits a review. Unattended, detached,
ambiguous, TUI and foreign-profile contexts stay idle.

The originating transcript’s latest persisted user-row id is checked before work
and writes. New/queued input, cancellation, owner replacement or disconnection
cancels unfinished work. Notices wait for the originating response to settle.
Read-only SQLite access uses the owning profile’s existing `state.db`; unavailable
transcript evidence fails closed. Native Desktop and messaging routes are unchanged.

## Desktop delivery

Version 0.13.1 admits native Desktop turns only when the current local policy says Desktop and the active profile authority retains exactly one unfinished agent for the bound session, route and chat. Cron, Kanban, delegated, one-shot and non-Desktop local contexts stay idle. The captured agent background-review callback uses the native `review.summary` contract after reply completion. It never opens another turn, uses a local adapter send or redirects the notice to Telegram. Telegram private chats retain their existing sender; group notices remain suppressed.

The small Desktop resolver is copied verbatim into Jot reviewer so both plugins remain independently installable. Tests enforce copy equality and use the real turn sender, session events, registered hooks and isolated persistence. No scenario mutates a real memory bank.

The Desktop owner is looked up by its stable chat id, so transcript-id rotation does not disable review. Repeated unfinished worker holders for the same cached agent are deduplicated; distinct matching agents remain ambiguous and fail closed. The validated sender is captured once when the local turn is admitted, then removed from the origin metadata before job construction.

Native Desktop does not need a prior messaging dispatch to bind the gateway: when the ingress cache is empty, the resolver reads the existing gateway runners weak reference from the already-loaded core module. The same active-profile, Desktop-policy, stable-owner, current-transcript and unattended gates still apply. No module import, gateway creation or core mutation is performed.
