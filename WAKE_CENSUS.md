# Stale/stall wake fire census and suppression design (t_2f526e1c)

Source data: the dispatcher's injected wake messages in the worker `state.db`
(`display_kind='internal_notification'`, texts `[Kanban stale card] …` /
`[Kanban stall] …`) cross-referenced against `kanban.db` `task_events`,
2026-10-12 .. 2026-10-18 (CEST, UTC+2; the week's clock span in unix seconds
is `[1791756000, 1792360800)`). 48 notification rows dedup by `message_uid`
to **43 unique wakes on 28 cards**: 32 stale, 11 stall.

## Fire-condition table (module:line = stale_wake.py at the feature base 29c18c8)

| # | Observed wake class | Count / cards | Fire condition (code anchor) | Reproduce |
|---|---|---|---|---|
| 1 | `todo`/`ready` open-card stale | 15 / 13 cards | `stale_cards` line 105: card in `STATUSES` minus parent-wait (`_WAITS_ON_PARENT`, line 64) whose last non-ignored event (line 119, `age >= STALE_AFTER=300`) is ≥5 min old; `sweep` fires when the state key differs from the record | `tests/test_stale_wake.py::test_wakes_each_stale_card_once_in_its_chat` |
| 2 | parked stale, no live park record | 2 (`blocked`, `block_kind` NULL) | same query as (1); a park without a record keeps the legacy once-per-park wake | `tests/test_parked_episode_wake.py::test_plain_park_without_a_record_keeps_its_wake` |
| 3 | parked stale **with** park record (`needs_input`/`capability`) | 12 / 9 cards — 10 fully redundant, 2 first-carries-information | same query as (1) with `block_kind` set by `kanban_db.block_task`; the park's own `blocked` dispatcher notification already reached the chats (44/47 within 2 min, 47/47 within 5 min — measured over 46 `blocked` events in the window) | `tests/test_parked_episode_wake.py::test_capability_park_is_suppressed_wherever_the_park_stays` |
| 4 | unparked status with lingering `block_kind` | 6 / 5 cards | `block_kind` deliberately survives `unblock` (`hermes_cli/kanban_db.py:3602`), so a todo/ready card can carry a park residue; the legacy once-per-park key made these repeat | `tests/test_parked_episode_wake.py::test_an_unparked_card_with_a_park_record_wakes_its_state_event` |
| 5 | stall (`respawn_guarded reason=active_pr` storm) | 11 / 1 card, 10-16 14:36..14:51 | `stalled_cards`: last 30 events all active_pr guards on a todo/ready card — this ran the pre-episode keying (fixed upstream 10-16 16:03, commit 936ad5c) and re-woke every tick the card sat parked-guarded | `tests/test_stall_lane_flap.py` (pins the fixed anchor semantics) |

Classes (3) and (4) are one defect family: the wake keyed the parked
*episode* on a field that outlives the episode. The two 10-13/10-15
capability wakes on `t_d580574f` (2 h and 309 h ages) predate the parked-
episode feature (commit 4ffa883, 10-15 11:15) and ran plain event-id
keying; the modern gate covers them too (a live `capability` record on a
parked card is never wakened, however old the park is).

## The suppression design

One stateless predicate on the live row, `stale_wake._suppressed_park` —
suppress exactly the stop-class parks, nothing else:

- **Suppressed**: `status == 'blocked' and block_kind == 'capability'`
  (the lead cannot unblock a capability wall), and `status == 'triage'`
  whatever the record (a block_loop escalation waits on a human decision the
  board event already surfaced).
- **Kept**: `needs_input` and `transient` parks (the reason addresses the
  lead; the card is reachable), plain parks (`block_kind` NULL or a foreign
  value — the dispatcher's park notification does not exist for them), and
  every unparked card even with a lingering record, because `block_kind`
  deliberately survives an unblock.

The first transition into a suppressed state still appears exactly once —
through the park's own automatic task-status message (47 of the 46 `blocked`
events in the window were notified within 5 minutes, 44 within 2); a lost
delivery is covered by the `notification_reconcile` sweep, so nothing is
silently dropped. The gate is stateless: it re-reads the live row each sweep,
so no bookkeeping can leak and unblocking re-opens the normal wake.

## Replay result and honesty about it

An event replay of the week's `task_events` through both semantics
(scratch script, removed from the repo after use; the logic is captured in
the fire table and its numbers below) reproduces the *shape*: baseline
78 simulated fires (66 open-stale, 10 park-stale, 2 stall), the class-3 rows
suppressed and the class-4 rows no longer re-arming on the parked key. It
does **not** reproduce the recorded fire list one-for-one — about two thirds
of the recorded rows match a simulated fire on card+class within ±15 min —
for three reasons:

- the replay's status track is event-derived; the dispatcher also flips
  statuses without an event row (crash paths, give_up), so some simulated
  cards park on paper that never parked in reality;
- the recorded wake state file survives gateway restarts and outages that
  the replay cannot see (the note-gap outliers sit inside the known 10-18
  17:15 profile-loss window);
- `kanban.db` is a live database: rows moved on after the window.

The census counts (43 recorded wakes; 10-of-28 cards park-record redundant;
11 same-episode stall repeats; 47-of-46-blocked-events notified ≤5 min) are
read directly from `state.db` + `kanban.db` and are exact. The per-class
row split inside class (3)/(4) is from the banner text itself
(`(todo/capability)`, `(blocked/needs_input)` …), not from the replay.

## What was NOT changed and why

- `next_due` still counts suppressed cards. Their crossing keeps waking the
  sweep on the quiet board; the sweep just declines to inject. Waking the
  sweep is nearly free (one read-only query round) and keeps the due-time
  trigger honest for every other card
  (`test_park_record_suppression_does_not_touch_next_due`).
- The stall wake: the 10-16 storm ran pre-fix code (936ad5c, merged 10-16
  16:03). The existing episode-anchor keying is the fix; `test_stall_lane_flap.py`
  pins it against the exact operational shape (one card flapping todo/ready
  across 20 guard ticks, one wake) so a future refactor cannot regress to
  per-event waking.
- `review_loop`, `churn_wake`, `waiting_wake`: the census gives them 6, 1
  and 0 fires respectively; per the study report §3.4 no change is
  warranted at those counts.

## Test receipts

Mandated command (from the board card):
`env HERMES_HOME="$(mktemp -d "$PWD/.hermes-test-home.XXXXXX")" PYTHONPATH=~/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python -m pytest -q tests`

- baseline (29c18c8) + new tests, before the change: 6 failed / 2 passed in
  `test_parked_episode_wake.py` (TDD red);
- candidate (this branch): **873 passed / 1 skipped** full suite — the +32
  over the 841 pre-existing are the 11 gate tests and 3 stall-boundary pins
  plus the parity pins of existing suites;
- baseline code with the new tests placed back over it reproduces the red
  run; the two baseline-green tests are explicit pins of behavior the old
  code already had (re-block inside a parked stretch, stateless gate across
  a lost state file).
