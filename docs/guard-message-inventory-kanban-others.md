# Guard-message inventory: the remaining plugins (card t_972ef7a1)

Every block/bounce/continue message in the in-scope plugins, with the
alternative it names, measured live in the worktree at base `fb7c961`
(origin/main after the sibling t_d295d26a merge; its shell-guards/workspace-guards
rewrites and gates are upstream and covered by that card's inventory). Messages
marked CHANGED were reworded by this card; messages marked OK already name a
concrete alternative and were left unchanged. The mechanical net
`tests/test_block_messages_name_alternative.py` pin all of it.

## kanban-guards

| Gate (source) | Trigger | Before text (short) | After text | Alternative named |
|---|---|---|---|---|
| `completion_gate.MESSAGE` | `kanban_complete` with Acceptance items missing from `metadata.acceptance` | OK | "the card's Acceptance list has items {missing} without evidence in metadata.acceptance. Map every item number to its concrete evidence, e.g. `metadata.acceptance = {"1": "...", "2": "..."}`. If an item cannot be met, do not complete: finish it, or `kanban_block` with the exact external constraint." | the exact `metadata.acceptance` shape + names the missing item numbers |
| `completion_gate.UNMET_MESSAGE` | evidence for items says "not met yet" | OK | "the evidence for Acceptance items {unmet} says they are not met yet. Finish those items (merge, deploy, verify, close), or `kanban_block` with the exact external constraint." | names the unmet numbers + the exact alternative calls |
| `completion_gate.REVIEWER_MESSAGE` | reviewer approval would close a card with post-review steps | OK | "Record the verdict as APPROVED in a card comment, then hand the card back to the implementer with `kanban_request_review` naming the implementer as reviewer... retry `kanban_complete` with the full `metadata.acceptance` map." | exact two-call alternative per outcome |
| `checkpoint_gate.MESSAGE` | run past FIRST_CHECKPOINT_CALLS with no checkpoint | OK | "this worker run is at {calls} API calls and past its budget-checkpoint duty. Durable, board-visible progress first, now, in ONE `kanban_comment` on your task: what is done and verified, what is mid-flight..." | exact `kanban_comment(body=...)` call with content contract |
| `checkpoint_gate.REPEAT_MESSAGE` | RECHECK_INTERVAL_CALLS since last checkpoint | OK | "Write the follow-up `kanban_comment` now (new verified facts, mid-flight state, next step) and continue." | exact `kanban_comment` call |
| `kanban_policy.check` | `kanban_create` with wrong assignee, triage, execution overrides, code card without `kanban_request_review` | OK | "assignee must be 'worker', got 'other'"; "triage is reserved for automatic escalation; create the card in todo"; "remove execution override 'model'"; "add `kanban_request_review` to its Acceptance list" | the corrected `kanban_create` per violation |
| `github_contact.check` | `gh pr review` / reviewer flags / @mentions from a worker terminal | OK | "Contacting people needs the owner's approval. If the card forbids merging or the diff needs independent review, `kanban_block` with kind=needs_input — do not just wait." + owner-approved merge hint when configured | the two authorized alternatives, one exact call |
| `pr_link_gate.check` | `gh pr merge` without a Development link, or `gh pr create/edit` body without exactly one closing keyword | OK | FIX_LINK: "edit the body to `Closes #N.`..., link it with GraphQL `gh api graphql -f query='mutation($p:ID!,$i:ID!){addCloseIssueReferences...}' -f p=<PR node id> -f i=<issue node id>`"; FIX_BODY: "edit the PR body so it has exactly one closing keyword (`Closes #N.`)" | exact commands/calls |
| `round_cap_gate.check` | `kanban_request_review` past the changes_requested ceiling | OK | "escalate to the lead in ONE `kanban_comment` on this card: what each round asked for, what the current candidate does, and the ONE decision needed... The lead can lift this ceiling... by posting a comment containing '{OVERRIDE_TOKEN}'" | exact `kanban_comment` + the override token |
| `handoff_gate.MESSAGE` | `kanban_block` whose reason says the work is done | OK | "That is a handoff, not a blocker: call `kanban_complete` (or `kanban_request_review`...) with the evidence and the exact next owner and action in the summary. If a real blocker, repeat the same `kanban_block` call and it will go through." | both corrected calls |
| `backup_copy_gate._MESSAGE` | handoff summary/metadata keeping a backup/rollback copy | OK | "Rollback is git: delete the copy and, if needed, recover the prior state from git history (or the originating external system, e.g. the GitHub API), then re-run the handoff without the retained copy." | exact recipe + re-run instruction |
| `schedule_gate.MESSAGE` | `kanban_schedule` from a worker | OK | "wait for CI, deployments or executions with a background process and notify=true, and continue... If genuinely blocked, call `kanban_block` with kind and the exact release condition" | exact call shapes |
| `scratch_artifact_gate._MESSAGE` | handoff names a deliverable only inside the scratch workspace, or an unpushed scratch git repo | OK | "pass the absolute paths in `metadata.artifacts` (...; for commits: `git bundle create <abs>.bundle <branch>`, or `git format-patch` for the commits) — the scratch workspace is deleted on completion." | exact fix per finding |
| `unused_code_gate._MESSAGE_HEAD` | diff adds unused private Python names/imports | OK | "Delete {entries} and rerun the tests, then request review again." (entries = each `file:line:name`) | exact delete list + retry |
| `stop_gate.MESSAGE` | run ends text-only after a blocked lifecycle call | OK | "Call the lifecycle tool for real: `kanban_complete(summary=..., artifacts=[...])` ... `kanban_request_review(summary=...)` ... `kanban_block(reason=...)`... If the tool is blocked again, read the error, fix the named cause, and retry." | the three exact calls |
| `completion_gate._SHOW_REMINDER` / `_REJECTION_SUFFIX` (transform, not a block) | published-PR duty visibility / "PR acceptance missing" rejection | OK | "kanban_complete must carry `metadata.published_pr` as {fix}"; "Retry `kanban_complete` in this same response with `metadata.published_pr` set to {fix} exactly." | exact argument value |

## recall-guards

| Gate | Trigger | Before | After | Alternative |
|---|---|---|---|---|
| `recall_guard.BLOCK_MESSAGE` (pre_tool_call on `clarify`) | `clarify` before the turn read memory | OK | "run `hindsight_recall` on the question (and `hindsight_reflect` if a synthesis is needed)... If memory answers it, act on the answer... If not, ask again in this same turn and say what memory returned." | exact `hindsight_recall` call + the ask-again path |

## jot-guards

No block messages: the plugin's only gate (a `pre_verify` continue directive
reminding the lead to update Jot) was removed — a reminder shapes behavior
instead of enforcing it. The plugin registers no hooks today.

## text-guards

No block messages: it strips the forbidden character (transform/pre_tool_call
rewrite), never blocks.

## provider-guards

| Gate | Trigger | Before | After | Alternative |
|---|---|---|---|---|
| `classify` (HTTP 424 / `byok_upstream_provider_failed`) | upstream provider failure verdict | OK (not a block against the worker: a routing verdict `retryable=True`/`should_fallback=True` that tells core exactly what to do — retry/fallback IS the alternative) | unchanged | verdict fields name the action |

## goal-waker

No block messages: it injects the goal manager's own continuation prompt on a
dispatch tick; there is no block/refusal path.

## Limit of the inventory

Wakes (`kanban-guards/wakes.py`: stale/loop/churn/waiting/stall/reconcile
texts) and `board_watch`/`active_pr_unblock`/`delivered_pr_park` are
dispatcher-tick injections into the card's subscribed chat — advice to the
lead, not blocks of a call the model just tried, and each already names its
exact recovery action (e.g. loop_text offers the four named decisions; the
unreconciled-notice names the completion evidence to reconcile). They were
audited and left unchanged.
