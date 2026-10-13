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
- `kanban_policy`: `kanban_create` must use that assignee, must not use triage and must not override execution settings; `kanban_request_review` may only name that worker as reviewer.
- `review_gate`: `kanban_request_review` is blocked until the session's latest `todo_list` holds one closed item per pre-review check (Failure lifecycle, Model evidence, Claims within evidence, Real boundary, Stale siblings): completed, or cancelled with `N/A: <reason>`.

Stale card wake (`stale_wake`): on the Kanban dispatcher tick (at most hourly) it wakes each chat subscribed to a triage/todo/ready/review/blocked card whose last state change is at least 1 h old, once per card state. It needs gateway injection to be allowed:

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
