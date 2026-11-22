# Hindsight reviewer

Reviews injected and tool-recalled memories after an eligible gateway turn. The proposal, independent confirmation, exact evidence, fresh primary-fact read, reversible invalidation and readback gates remain unchanged. A newer admitted message cancels unfinished writes.

## Noise recall calibration

Version 0.20.0 raises the share of real noise the reviewer removes, measured on 354 memories sampled from 3,318 reviewed over 34 hours of 0.19.0 turns. Ground truth covers 263 of the 354: 249 items where two blind labelers agree (the other 105 were disputed), plus 14 of the disputed items that the user ruled noise (the user ruled 15; one of them is also in the agreed set). Each variant ran three times per item on the cron model; intervals are bootstrap 90%.

| variant | noise removed | useful wrongly removed |
|---|---|---|
| 0.19.0 | 23% (18-28) | 1.9% (0.3-3.7) |
| 0.20.0 | 38% (32-45) | 0.3% (0-0.9) |

Four changes, each pinned by a test:

- **Prompt.** The noise pass is told that most noise is restatement and must search the other memories for one that already says everything, with a same-date rule and a keep-only-if-you-can-name-the-fact rule.
- **Full timestamps.** The reviewer sent dates cut to 10 characters, so a newer memory written the same day could never be proven newer and 37 of 41 correct superseded flags were rejected as `unproven_newer_target`. Dates now carry the time of day.
- **Narrower protection regex.** Bare `commit`, `identity`, `permission` and `consent` shielded 20% of reviewed memories, almost all git commits, system identities and OAuth consent. Real preferences, authorizations, commitments and identity facts still shield (`commitment`, `committed to`, `permission to`, `gave permission`, `consent to`, `identity fact`, `prefer`, `authorized`).
- **Duplicates go back to the second checker.** The 0.19.0 skip was calibrated against the old prompt. With the stronger first pass the checker is what stops a duplicate whose kept copy lacks a path, value or step: it cut wrongly removed useful memories from 2.8% to 0.3% on the same sample, measured by replaying the shipped plugin itself.

The audit no longer emits `confirmation_skipped`; `TRUSTED_NOISE_REASONS` stays as an empty set so a reason can be trusted again by adding it back.

## Noise confirmer calibration

Version 0.18.0 rewrites the second-pass noise confirmer after it was measured against two independent blind labelers (92% and 79% agreement) on 235 vetoed memories. 86% of the memories it vetoed (144 of 167) were real noise, while it had approved 0 false removals, so its default of keeping when unsure was costing recall, not protecting memories. Each reason now has its own test: a duplicate is noise unless a named fact, value or step is missing from the kept memory; a fragment is noise only when it names no subject and holds no specific value, path, identifier or decision; a superseded memory still needs a newer, contradicting kept memory. Preferences, authorizations, commitments and identity facts are always kept.

Replayed on 261 labelled cases with three repetitions each: noise recall 0.37 to 0.46, useful memories wrongly removed 4.3% to 1.4%, and none removed by majority vote. Expected removals rise about 14% (534 to roughly 610 over the same period). A candidate that also kept the unsure default only for fragments reached 0.52 recall but removed two useful memories, so it was rejected. The model and its labels are unchanged; this is a prompt-only change with no new retries.

## Result line and reconnecting Desktop (0.21.0)

Two reasons a finished review showed nothing in Desktop or on mobile.

- **No change meant no notice.** 59 of 99 reviews on the 0.20.1 Mac ended `not_needed` and posted nothing, so most turns never said the review had run. A review that checked memories and changed none now posts one line: `🧠 Memory review: 8 checked · no changes`. An incomplete review says `review incomplete · no changes` and never claims a count it did not check; a failed one says `review failed`. The line carries a count, never memory text. Machine-written turns (cron, tool, delegated) and turns with no sender stay silent as before.
- **A Desktop socket that dropped mid-turn lost the notice.** On the remote `hermes serve` route the resolver required a live socket at the instant the turn ended. 9 of 9 Claudia GUI turns whose socket closed mid-turn ended `suppressed_unbound_or_group`; 21 of 21 with a stable socket were handed off. The binding still requires the same session, route, profile and one running agent, but a detached viewer no longer unbinds the review. The notice waits up to 300 seconds for the viewer to reattach, which is longer than the 20 second grace the server gives an orphaned socket, and is dropped with a `notice_failed` audit line if nobody returns.

`notices` now takes `true` (default, result line and change notices), `"changes"` (the previous behavior, a notice only when a memory changed) or `false` (silent). The audit `started` event also records `bound` and `origin` so a suppressed notice can be traced to its route without reading code.

## Incomplete-review warning (0.20.1)

On a day of live turns, 7 of 36 reviews ended `review incomplete`, and the notice reached the user on 5 of them. Two causes, both harmless to the memory bank:

- One missing verdict out of 20 (5 of the 7 turns). The model sometimes skips a single row. The memory is simply left alone, so this is now an abstention, counted in `abstained` and the per-lane `missing_rows` counter, not lost coverage. A batch that returns no usable row at all still raises `Incomplete model output` and still counts as lost.
- One out-of-vocabulary value rejected a whole batch of 20 (2 of the 7 turns). The model sometimes put `fragment` or `transient` in `decision` (the enum is `keep` or `noise`), or returned `null` for a field. Both output schemas now accept any string or `null` in `decision`, `reason`, `category`, `duplicate_of` and `evidence_quote`, and the existing per-row checks (valid decision, valid reason, string target, the target and newer-date rules) reject only that row and keep the memory. Nothing can be removed on a value the checks do not recognize.

An invalid review row used to count as both rejected and missing; it now counts once, as rejected.

## Coverage status

Version 0.17.0 splits what 0.16.0 called `partial`. `status=partial` now means coverage was lost: a batch or confirmation failed, the model dropped or garbled rows, a write could not be verified, or a newer message cancelled the work. Deliberate abstentions no longer downgrade the status: rejected noise rows (invalid target, unproven newer memory), unquotable evidence, deferred noise, stale or changed recalls and ineligible sources.

The `finished` audit line carries `lost` and `abstained` counts, and `failure_reasons`, a map of `lane:reason` to count. A failed batch also writes one `batch_failed` line with `lane`, `size` and `reason`. Reasons are a fixed vocabulary, never exception text: `http_<status>`, `truncated`, `incomplete_output`, `duplicate_case_ids`, `schema_mismatch`, `value_error`, `timeout`, `connection`, `other`. `truncated` means the reply did not parse and the model used at least 90% of its `max_tokens`.

Output budgets are 120 tokens per memory for the review lane and 100 for the noise lane (was 60), so a full batch of 20 UUID-keyed duplicate rows fits. There are still no same-route retries and no batch splitting.

## Every turn

Version 0.16.0 reviews every turn that injected or recalled memories, not only Desktop and Telegram. The turn's surface is read from the `post_llm_call` platform, then the session source. A surface without a live viewer (CLI, TUI without a bound Desktop owner, cron, subagents, the curator) gets an origin with no message id and no sender: it is never superseded and never notified, and the audit line records `surface` and `attended`.

Turns whose user rows are machine-written (cron, subagent, curator, Kanban, one-shot, tool, API server, webhook, bot room, or an unattended flag) run only the noise lane: duplicates, fragments and superseded memories. They never ask the model for a contradiction, because a prompt written by a scheduler is not Marcos correcting a memory. At most two such reviews run at once, so a cron burst cannot flood the model or the bank. Attended turns keep both lanes and every existing gate. The legacy `hermes serve` binder also admits the `tui` source, so a TUI session served to Desktop gets its notice.

## Silent mode

Version 0.15.0 adds `plugins.entries.hermes-agent-guards/hindsight-reviewer.notices`. Set it to `false` to keep every review and reversible invalidation running in the background without posting the chat notice; the audit log records `notice_state=suppressed_by_config`. Absent, unreadable or any other value keeps notices on; `"changes"` limits them to reviews that changed a memory (see 0.21.0).

## Remote Desktop delivery (serve/WebSocket)

Version 0.14.1 moves two inline notes in `legacy_desktop.py` into docstrings so the repository-wide no-comment-lines invariant passes; behavior is unchanged.

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
