# Actions: applying the correction and closing the retro

The retro worker applies its own findings. Nothing is routed to the lead, no card is created, and
`kanban_request_review`/`kanban_block` are never used. Choose the **strongest feasible level** for
each finding, then apply exactly one of the routes below.

## 1. Write gates (self-contained)

These gates bind every write this retro makes. They are the whole authority: no external skill is
required to interpret them.

1. Name the exact demonstrated lesson and its source message id. Never invent history.
2. **Already-covered check first.** Read the governing skill; if its canonical guidance already
   states the rule, record `already_covered` and stop that write.
3. **Smallest justified correction.** Consolidate into the existing rule; never append an incident
   log, a date, or a card id.
4. **Read back** the changed file after its last mutation, and confirm a retain with a recall.
5. After a mutation failure, stop that route after at most one corrected retry. Even if a later
   retry succeeds, disclose the original failure; do not claim an untouched run.
6. Never write a placeholder or a test entry. A tool result is authoritative; a model statement is not.

### Consolidate-first (bind every write)

1. **One finding, one home.** Before writing, grep the whole skill library for the rule. If it
   exists, replace or merge it in place; never add a near-duplicate.
2. **Consolidate first.** If the target file is within ~500 chars of its gate (12000 for a
   reference, 6000 for `SKILL.md`) or one bullet/paragraph dominates it, refactor before adding:
   split by topic into a reference file, merge duplicates, tighten wording, preserving every rule.
   Record a before/after rule inventory in the completion metadata; keep no backup file.
3. **Route always-on additions to a topic reference file.** Never grow a `SKILL.md` hub past its gate.
4. **Verify live.** After the last write, re-grep the exact lesson in the live file and record that
   grep as evidence. A change not found live is reported as NOT applied.
5. **Never patch the plugin's own skill copy.** It is read-only and overwritten by `plugins install`;
   record the exact proposed patch as `deferred` for the lead.
6. **Keep the size gates passing** and update the skill's own `references/analysis.md` and
   `references/actions.md` to match; each stays under its gate.

## 2. Pick the level

- **procedure / pitfall** → `skill_manage(action='patch')` on the *governing* skill with an exact
  `old_string` and `new_string`. Use ONLY the `skill_manage` tool: the file `patch`/`write_file` tools and
  shell edits on `~/.hermes/skills/` are blocked for workers by `workspace_write_gate`, and that block is a
  wrong-tool signal, not a missing authorization. Never `kanban_block` over it; retry with `skill_manage`. Read the file first; the patch must land on existing text. Respect
  the size gate (`SKILL.md` ≤ 6000 chars, other `.md` ≤ 12000). If the target is at the limit,
  tighten neighbouring prose rather than relocating an always-on rule.
- **stable cross-session fact** → ONE `hindsight_retain`, deduped against existing memories and
  verified with a `hindsight_recall`. Recent incidents, dated config, model routes and temporary
  progress are not durable facts.
- **checkable rule** → propose a deterministic check only when it is cheap (a script in the owning
  skill's `scripts/`); otherwise a skill line.
- **no safe home** → record it under `deferred` in the completion metadata with the proposed home.
  Do not create a skill to hold it; skill creation is out of scope.

Out of scope, always recorded under `deferred` and never acted on: `SOUL.md`, `config.yaml`, cron,
credentials, permissions, other profiles, project repositories, external systems, and edits to the
`nightly-self-improvement` skill. A refused tool or a guardrail is not authorization for another route.

## 3. Serialize the write step only

Analysis runs freely in parallel. Only the short **read-target → patch → readback** window is
serialized, so the lock is never a parallelism limit.

```
terminal(command="python3 <skill_dir>/scripts/retro_lock.py acquire --owner $HERMES_KANBAN_TASK --wait 60")
```

Exit `0` means acquired; do the write; then release with `--owner $HERMES_KANBAN_TASK`. Exit `75`
means the lock is still held after the bounded wait: **retry the acquire**, never skip the finding.
The lock file carries an owner and a TTL, so a dead owner or an expired lock is taken over
atomically instead of deadlocking the board. Re-read the target skill *after* acquiring, because a
peer retro may have patched it while you analysed.

## 4. Close the card

Finish with `kanban_complete`; the summary carries:

- `findings`: count of cited findings (zero is valid);
- `changes`: each applied change as `<skill path> <sha256>` or the Hindsight recall id;
- `already_covered`: findings whose rule already existed, with the skill that states it;
- `deferred`: out-of-scope findings with their proposed home;
- `session`: the worker session id and the route used to resolve it.

Add the library-gate output for `task-retrospective` to `metadata`. No other lifecycle call is made.

## 5. Division of labour with the nightly

The retro covers **worker cards**; `nightly-self-improvement` still covers the lead's chats and
corrections. Do not edit the nightly skill and do not audit its scope.
