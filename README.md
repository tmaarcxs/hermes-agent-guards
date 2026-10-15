# agent-guards

A Hermes Agent plugin that blocks recurring agent mistakes before a tool runs and keeps delegated Kanban work moving. Every check fails open: an internal error never blocks a tool call.

## What it does

Terminal blocks:
- foreground `sleep` of 60 s or more (run the wait in the background with a notification instead);
- `gh pr merge` without `--match-head-commit`;
- `osascript`;
- `git push --force`/`-f` without `--force-with-lease`;
- in a worker process, `github_contact`: commands that contact people on GitHub — review requests (`gh pr edit --add-reviewer`, `gh pr create --reviewer`, `gh api` POST to `.../requested_reviewers`, every `gh pr review`), issue assignees and comments whose body `@mentions` a user or org/team (inline or via a readable body file). Mention-free comments stay allowed; `@me`/`@copilot` and removal flags are exempt; on assignee flags the authenticated login is also exempt (self-assignment contacts nobody — resolved once via `gh api user` and cached; unresolvable login fails closed). Message: contacting people needs the owner's approval — for a CODEOWNERS-only gap on a green exact head, merge it yourself via `pr_merge_verified --repo <owner/repo> <pr> <head-sha>`; if the card forbids merging or the diff needs independent review, `kanban_block` with `kind=needs_input` — do not just wait.

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
- `completion_gate`: in a worker process, blocks `kanban_complete` on a card whose body has a numbered `Acceptance` list until `metadata.acceptance` maps every item number to its evidence. An item that cannot be met calls for `kanban_block`, not completion. Evidence that itself admits an item is unmet ("not yet", "not met", "pending merge/review/DES/deploy", "remains open") also blocks. Reviewer completions (run claimed from review, or `metadata.review_verdict`) are blocked too when unevidenced Acceptance items name post-review steps (merge, deploy, DES, close): the approval must record its verdict and hand the card back to the implementer through `kanban_request_changes` (completion from review closes the whole card, and there is no approved-and-returned state). Reviews whose remaining items do not name post-review steps complete as before.
- `review_gate`: `kanban_request_review` is blocked until the session's latest `todo_list` holds one closed item per pre-review check (Contract coverage, Failure lifecycle, Model evidence, Claims within evidence, Real boundary, Stale siblings, Self reproduction, Claims anchored): completed with a receipt anchor in the item text (a receipt path, artifact file, file:line reference, hash, exit code or test count), or cancelled with `N/A: <reason>`.
- `stop_gate`: after a blocked lifecycle call (review gate, policy, any tool error), a final text-only reply like "Building the todo list, then requesting review:" is answered with a `pre_verify` continue directive telling the worker to actually call `kanban_complete`/`kanban_request_review`/`kanban_block`. Without it the core stop-guard counts the blocked call as done and the run exits rc=0 as a protocol violation. `pre_verify` only fires in turns that edited files (`write_file`/`patch`), so pure-read runs get no nudge from this gate alone.
- Retry instruction: every guard block of a lifecycle call (`kanban_policy`, `completion_gate`, `review_gate`, `handoff_gate`, `schedule_gate`, and `repeat_guard` when the repeated call is a lifecycle call) ends with the same demand — retry the lifecycle call in this same response; a text-only reply ends the run as a protocol violation. This is the one lever a block message controls, and it covers read-only runs that `stop_gate` cannot reach.
- `handoff_gate`: the first `kanban_block` of a session whose reason says the worker's part is finished and only lead-owned steps remain (merge, deployment, DES, publication, approval) is bounced with advice to hand off through `kanban_complete` or `kanban_request_review`; repeating the call lets a genuine decision block through.
- `backup_copy_gate`: in a worker process, blocks `kanban_complete`/`kanban_request_review` whose summary, result or metadata declares a kept/preserved backup, rollback, pristine or before-copy of prior state ("backup kept", "rollback copy preserved", "defective versions backed up"). Rollback is git: the worker deletes the copy and recovers prior state from git history instead. Statements that no copy was kept, or rollback done via git, pass.
- `schedule_gate`: in a worker process, blocks `kanban_schedule`. Scheduling ends the run and sets no timer, so a self-scheduled card stops until someone notices. Workers wait for CI, deployments or executions with background processes and notifications; a real external dependency is a `kanban_block` with its release condition.
- `published_head_gate`: in a worker process, blocks `kanban_request_review` when it names a GitHub PR and a commit (`metadata.commit` or `metadata.head`) that is not the PR's live head, so a reviewer never freezes an unpushed candidate. Fails open if either value is missing or GitHub cannot be read.
- `approval_wait_gate`: in a worker process, blocks the silent hours-long wait on an approval-only pull request (the run-3517 incident: PR was green on every automated check, the CODEOWNERS approval was the only open gate, and the worker polled it with background loops for ~17 h). When the exact head of a polled PR is green and review approval is the only open gate, readiness polls of that PR (`gh pr checks`, `gh pr view --json statusCheckRollup|mergeStateStatus|…`, `gh api …/pulls/<n>`, `pr_status`/`pr_wait_checks`) return the verdict line and the two authorized moves instead of running: merge the verified head via `pr_merge_verified` (standing authorization for a CODEOWNERS-only gap), or `kanban_block(kind="needs_input")` so the lead is notified. A repeating `kanban_heartbeat` note that admits the approval-only wait is blocked from the second one on (the first passes so the situation can be time-stamped). Pending or failing checks, drafts, conflicts and mergeable-read errors are not verdict names, so their polls flow through untouched, and the verdict is cached for 15 minutes so repeated polls cost at most one GraphQL round-trip per quarter hour.

Stale card wake (`stale_wake`): on the Kanban dispatcher tick (at most hourly) it wakes each chat subscribed to a triage/todo/ready/review/blocked/scheduled card whose last state change is at least 1 h old, once per card state. It needs gateway injection to be allowed:

Review loop wake (`review_loop`): on the dispatcher tick (at most every 5 min) it wakes each chat subscribed to an open card that has reached 3, 6, 9... review rounds with changes requested, once per threshold, with the latest requested change, so the lead narrows, splits, corrects or takes over the card. It uses the same gateway injection setting.

Churn warning (`churn_wake`): on the dispatcher tick (at most hourly) it warns each chat subscribed to a non-archived, non-done card whose worker runs have reached 10, 20, 40… or whose change-requests have reached 5, 10, 20…, once per threshold, with the last change-request reasons, so the lead decides to split the card, narrow the acceptance or confirm continue. It is a warning only: nothing is blocked. It uses the same gateway injection setting.

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
