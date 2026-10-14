# agent-guards

A Hermes Agent plugin that blocks recurring agent mistakes before a tool runs and keeps delegated Kanban work moving. Every check fails open: an internal error never blocks a tool call.

## What it does

Terminal blocks:
- foreground `sleep` of 60 s or more (run the wait in the background with a notification instead);
- `gh pr merge` without `--match-head-commit`;
- `osascript`;
- `git push --force`/`-f` without `--force-with-lease`.

Attachment block:
- `kanban_attach` with more than 256 characters of inline base64. Models transcribe those bytes and corrupt them; attach by path (`artifacts=[absolute path]` at handoff, or `hermes kanban attach <task_id> <path>`).

Argument normalization (the call proceeds with corrected arguments):
- empty optional Kanban arguments become absent;
- `skill_view` names of the form `x:x` become `x`;
- a Kanban `task_id` sent as the literal `$HERMES_KANBAN_TASK` becomes absent, so the worker's own task is used.

Other guards:
- `repeat_guard`: blocks an identical call that already failed twice in the session with nothing changed since.
- `skill_size`: blocks any write that grows a skill markdown file past its limit (`SKILL.md` 6000 characters, other `.md` files 12000). Shrinking is always allowed, so oversized skills can be split into an index plus a reference tree.

Worker setups (only when `kanban.default_assignee` is set in the profile config):
- `kanban_policy`: `kanban_create` must use that assignee, must not use triage, must not override execution settings and must carry an `Acceptance` heading followed by a numbered list of checkable criteria; `kanban_request_review` may only name that worker as reviewer.
- `lead_delegation`: in the lead process (no `HERMES_KANBAN_TASK`), blocks `write_file`/`patch` under repository roots (`~/git_repos`, override with `AGENT_GUARDS_REPO_ROOTS`) and `git commit`, `git push` and `gh pr create` aimed at them. The lead investigates, writes issues and cards, reviews and integrates; implementation and delivery go to a worker card.
- `completion_gate`: in a worker process, blocks `kanban_complete` on a card whose body has a numbered `Acceptance` list until `metadata.acceptance` maps every item number to its evidence. Reviewer completions (run claimed from review, or `metadata.review_verdict`) are exempt. An item that cannot be met calls for `kanban_block`, not completion. Evidence that itself admits an item is unmet ("not yet", "not met", "pending merge/review/DES/deploy", "remains open") also blocks.
- `review_gate`: `kanban_request_review` is blocked until the session's latest `todo_list` holds one closed item per pre-review check (Contract coverage, Failure lifecycle, Model evidence, Claims within evidence, Real boundary, Stale siblings, Self reproduction, Claims anchored): completed with a receipt anchor in the item text (a receipt path, artifact file, file:line reference, hash, exit code or test count), or cancelled with `N/A: <reason>`.
- `stop_gate`: after a blocked lifecycle call (review gate, policy, any tool error), a final text-only reply like "Building the todo list, then requesting review:" is answered with a `pre_verify` continue directive telling the worker to actually call `kanban_complete`/`kanban_request_review`/`kanban_block`. Without it the core stop-guard counts the blocked call as done and the run exits rc=0 as a protocol violation. `pre_verify` only fires in turns that edited files (`write_file`/`patch`), so pure-read runs get no nudge from this gate alone.
- Retry instruction: every guard block of a lifecycle call (`kanban_policy`, `completion_gate`, `review_gate`, `handoff_gate`, `schedule_gate`, and `repeat_guard` when the repeated call is a lifecycle call) ends with the same demand — retry the lifecycle call in this same response; a text-only reply ends the run as a protocol violation. This is the one lever a block message controls, and it covers read-only runs that `stop_gate` cannot reach.
- `handoff_gate`: the first `kanban_block` of a session whose reason says the worker's part is finished and only lead-owned steps remain (merge, deployment, DES, publication, approval) is bounced with advice to hand off through `kanban_complete` or `kanban_request_review`; repeating the call lets a genuine decision block through.
- `schedule_gate`: in a worker process, blocks `kanban_schedule`. Scheduling ends the run and sets no timer, so a self-scheduled card stops until someone notices. Workers wait for CI, deployments or executions with background processes and notifications; a real external dependency is a `kanban_block` with its release condition.
- `published_head_gate`: in a worker process, blocks `kanban_request_review` when it names a GitHub PR and a commit (`metadata.commit` or `metadata.head`) that is not the PR's live head, so a reviewer never freezes an unpushed candidate. Fails open if either value is missing or GitHub cannot be read.

Stale card wake (`stale_wake`): on the Kanban dispatcher tick (at most hourly) it wakes each chat subscribed to a triage/todo/ready/review/blocked/scheduled card whose last state change is at least 1 h old, once per card state. It needs gateway injection to be allowed:

Review loop wake (`review_loop`): on the dispatcher tick (at most every 5 min) it wakes each chat subscribed to an open card that has reached 3, 6, 9... review rounds with changes requested, once per threshold, with the latest requested change, so the lead narrows, splits, corrects or takes over the card. It uses the same gateway injection setting.

```yaml
plugins:
  entries:
    agent-guards:
      allow_gateway_injection: true
```

## Install

```bash
hermes plugins install tmaarcxs/hermes-agent-guards --enable
```

## Tests

```bash
python3 -m pytest -q tests
```

Updates: installs without `--ref` follow `main`; with `plugins.auto_apply: true` Hermes pulls new commits on its update-check cadence (`plugins.auto_update_check_hours`). A private fork needs read access for the Hermes host, for example a read-only deploy key.
