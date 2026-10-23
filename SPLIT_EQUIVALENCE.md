# Split equivalence: old `agent-guards` vs the three folders

Measured by loading the old monolithic plugin at base `90456b2` and the three
new folders in one interpreter with unique package names
(`scratch/equiv.py`). Hook callbacks are listed per hook; the check order is the
intra-plugin `pre_tool_call` tuple.

This report establishes the split's *structural* equivalence: same hooks, same
callbacks, same relative check order. It is **not** a claim that the three
folders are behavior-identical to base — three deliberate behavior changes travel
with the split (§7 nudge rework, §9 skill-size flag + `pre_verify` enforcement,
§10 `gh auth` shared-account block).
Everything else in this document describes the split itself.

## Registered callbacks per hook

| Hook | OLD (one plugin) | NEW kanban-guards | NEW shell-guards | NEW workspace-guards |
|---|---|---|---|---|
| `pre_tool_call` | 1 (`__init__._pre_tool_call`) | 1 | 1 | 1 |
| `post_tool_call` | 2 (`__init__._post_tool_call`, `wakes._wake_poke`) | 2 (same two) | 1 (no-op placeholder) | 0 |
| `pre_verify` | 1 (`stop_gate.check`) | 1 (`stop_gate.check`) | 1 (`skill_size.verify_check`) | 0 |
| `transform_tool_result` | 1 (`__init__._transform_tool_result`) | 1 | 1 | 0 |
| `transform_terminal_output` | 0 | 0 | 1 | 0 |
| `on_kanban_dispatch_tick` | 6 (the six `wakes` ticks) | 6 (same six) | 0 | 0 |

Total registered callbacks: OLD 11, NEW 11 + 5 + 1 = 17 (kanban 11, shell 5,
workspace 1). The old root registered 11 callbacks on 5 hooks; the new folders
register 17 because the no-op `shell-guards._post_tool_call` placeholder,
`shell-guards`' own `transform_terminal_output` seam, and its `pre_verify`
enforcement exist per plugin. Hook totals per plugin match each `plugin.yaml`.

## Intra-plugin `pre_tool_call` check order

OLD (root `__init__.py:274-291`):

```
checkpoint_gate, workspace_write_gate, home_litter_gate, kanban_policy,
workspace_convention, (lead repository-write guard, removed), github_contact, paas_promote_gate,
review_gate, handoff_gate, completion_gate, backup_copy_gate, schedule_gate,
skill_size, skill_size(record), background_notify_gate
```

then the terminal/attach/normalize tail.

NEW, by plugin:

```
kanban-guards:    checkpoint_gate, kanban_policy, github_contact,
                  review_gate, handoff_gate, completion_gate, backup_copy_gate,
                  schedule_gate
shell-guards:     paas_promote_gate, skill_size, skill_size(record),
                  background_notify_gate, then terminal/attach/normalize tail
workspace-guards: workspace_write_gate, home_litter_gate, workspace_convention
```

Within each plugin the old relative order is preserved exactly (the Kanban
entries keep `checkpoint_gate` first; the shell entries keep their order; the
workspace entries keep theirs).

## Differences and why

1. **Cross-plugin `pre_tool_call` order is now host load order.** The host
   resolves the directive list in registration order and returns at the first
   `block` (`hermes_cli/plugins.py:1994-2015`), so when two plugins would block
   the same call the earlier-loaded plugin's message wins. Load order is
   alphabetical by plugin key with `requires_plugins` edges first
   (`resolve_plugin_load_order`): `kanban-guards` < `shell-guards` <
   `workspace-guards`. In the monolith the single root tuple decided.

   This is **not** latent: the same call can be blocked by two plugins. A
   recursive deletion aimed at the live Hermes home (`~/.hermes`) or at
   `$HERMES_HOME` is blocked by `shell-guards` (`hermes_home_gate` via
   `terminal_policy`) and by `workspace-guards` (`workspace_write_gate`). Under
   the load order above the `shell-guards` message wins and the
   `workspace-guards` one is never reached. Pinned by
   `tests/test_block_winner.py::test_shell_guards_block_message_wins_over_workspace_guards`.

2. **`transform_tool_result` no longer chains across plugins, so the two appending
   transforms are joined on two hooks.** OLD ran one callback that chained
   `completion_gate → workspace_scope_gate → kanban_show_slim_gate → skill_size →
   lead_work_nudge`. NEW splits that into two plugins: `kanban-guards` keeps
   `completion_gate → workspace_scope_gate → kanban_show_slim_gate →
   lead_work_nudge` (unchanged inside the plugin) and `shell-guards` owns
   `skill_size`. The host keeps the **first non-`None` string** and drops the rest
   (`model_tools.py:849-866`); callbacks do **not** chain — every callback receives
   the ORIGINAL result, so a plugin cannot build on the previous plugin's output.
   The Kanban and shell transforms are otherwise mutually exclusive in practice
   (Kanban transforms fire on `kanban_show` / completion results; the skill-size
   transform fires on `terminal`/`execute_code`/`patch` results).

   The one call both want is a Kanban lead's threshold-crossing `terminal` call
   that also left an oversized skill file: `kanban-guards` loads first, so its
   nudge would win the first-string race and the size flag would be silently
   dropped. That corner is **closed** by delivering the size flag on
   `transform_terminal_output` instead of `transform_tool_result` for `terminal`
   calls. The terminal tool runs that hook INSIDE the tool, before
   `transform_tool_result` (`tools/terminal_tool_result.py`
   `finalize_foreground_result` applies the output transform; the result hook runs
   after the tool returns), so the flag is already part of the result the nudge
   appends to and one `transform_tool_result` string carries both messages —
   exactly the OLD composition (size flag first, nudge after). `execute_code` and
   unified-diff `patch` keep the `transform_tool_result` seam, and a `terminal`
   call that yields to the background returns before the terminal hook runs, so
   the result seam still covers it. Both seams pop the same `tool_call_id`-keyed
   `_PENDING` snapshot, so a call is never flagged twice. Pinned by
   `tests/test_transform_winner.py` (nudge only, flag only, both with the flag
   first).

3. **Checkpoint message precedence.** In the monolith `checkpoint_gate.check`
   ran before `kanban_policy.check`. In the split both are still in
   `kanban-guards` in the same relative order, so the checkpoint message still
   wins inside the plugin — no change here. (The plan's expected
   "checkpoint precedence swap" was an artifact of the abandoned six-plugin
   design where `kanban_policy` moved to a separate shared plugin; the final
   three-folder design keeps both in `kanban-guards`, so the swap does not
   occur.)

4. **Fail-open per check restored.** The first `kanban-guards` extraction called
   each check without the root's `try/except`, so a raising guard propagated and
   took the whole chain down. The integration restores the root's per-check
   fail-open wrapper (`kanban-guards/__init__.py::_pre_tool_call_checks`); the
   regression is pinned by
   `tests/test_block_attribution_all_plugins.py::test_raising_guard_stays_fail_open`.

5. **`pre_verify` / `on_kanban_dispatch_tick`.** `on_kanban_dispatch_tick` remains
   only in `kanban-guards` with the same callbacks as the monolith.
   `pre_verify` stays in `kanban-guards` (`stop_gate.check`, unchanged) and is
   **added** to `shell-guards` — a deliberate behavior change, see §9.

6. **`post_tool_call`.** OLD had `__init__._post_tool_call` (recording into
   `review_gate`, `stop_gate`, `checkpoint_gate`) plus `wakes._wake_poke`.
   `kanban-guards` keeps both verbatim; `shell-guards` registers a no-op
   placeholder so its declared hook surface is stable; `workspace-guards`
   registers none.

7. **`lead_work_nudge` rework — a deliberate, non-split behavior change.**
   Commit `20c1eeb` ("count only exploration; patch/write_file silences the
   turn"), re-pathed onto `kanban-guards` by `b0c5f49`, changes when the lead
   nudge fires. At base `90456b2` the nudge counted every tool outside the
   coordination/bookkeeping list; at head it counts only the seven exploration
   tools (`read_file`, `search_files`, `web_search`, `web_extract`,
   `session_search`, `tool_call`, `terminal`), and a `patch`/`write_file` sets a
   per-turn "wrote" flag that silences the nudge for the whole turn. This is
   planned (split plan §5) and tested (`test_writes_do_not_count`,
   `test_a_turn_with_a_write_never_nudges`), but it is a **behavior change
   carried by this branch**, not a mechanical move — the nudge fires strictly
   less often than at base. `_EXCLUDED_TOOLS`/`_EXCLUDED_PREFIXES` are gone for
   this reason, not because they were dead.

8. **Superseded analysis artifacts now live under `docs/`.** `WAKE_CENSUS.md`,
   `replay_workspace_write_gate.md` and `replay_workspace_write_gate.json` were
   dropped from the repo root by the split. Their content still has value as
   historical evidence (the wake fire-condition census and the gate's replay
   over the 2026-10-18 worker corpus), so they were **kept** — moved verbatim to
   `docs/` rather than deleted. Nothing in the code, tests or docs references
   them; they are reference material, not live documentation.

9. **Skill-size flag rewritten and enforced on `pre_verify` — a deliberate,
   non-split behavior change.** At base `90456b2` the over-limit flag was a
   message appended to the tool result and nothing more: a model could read it
   as someone else's task and end the turn. Two problems made that easy. The
   snapshot in `record` attributes ANY growth of a skill markdown file to the
   current call, so when another session or worker writes the file concurrently
   the flag lands on an unrelated call, and the model correctly says "I did not
   cause this". And the message itself gave no reason to act anyway.

   This branch changes both, on top of the §2 seam move:
   - the message now leads with the required action in one imperative sentence,
     states the file is over its limit NOW regardless of what caused the growth,
     says who caused it does not matter, says shrinking an over-limit file is
     always allowed, forbids replying that the file is unrelated, and keeps the
     tree-split instruction (`shell-guards/skill_size.py` `_post_message`);
   - `shell-guards` now registers `pre_verify` (`skill_size.verify_check`): while
     a file flagged in THIS session is still over its limit the turn cannot end —
     a continue directive names the file and the action. It re-measures at check
     time (a file already back under its limit clears its flag silently) and caps
     at two continuations per file per session (`_MAX_CONTINUATIONS`), so a model
     that cannot fix the file cannot loop forever.

   The flag itself is still raised on the same condition as before (a growth past
   the limit caused by a watched call) — the false attribution is not "fixed",
   it is made irrelevant by the message and by enforcement. Pinned by the
   `pre_verify` / message tests in `shell-guards/tests/test_skill_size.py`
   (including the concurrent-writer case) and by the host probe showing a
   non-worker chat session gets the continue directive.

10. **`gh auth` shared-account block — a deliberate, non-split behavior change.**
    `shell-guards/terminal_policy.py` adds `_GH_AUTH` (plus `_gh_auth_blocked`,
    `_quoted_spans`, `_shell_c_argument`), and `terminal_violations` appends its
    message after the force-push check. At base `90456b2` a `gh auth switch` was
    not matched by any guard, so a worker could flip the active gh account for
    every Hermes process (Hermes reads its Copilot token from `gh auth token`,
    source `gh_cli`) and leave it flipped — the hazard this branch closes. The
    block covers `switch`, `login`, `logout` and `refresh` at command-word
    position (bare, with `--user`/`-u`/`--hostname`, under `env`, after
    `&&`/`;`/`|`, in `$(...)`) and inside a quoted `bash -c`/`sh -c` argument;
    `gh auth status` and `gh auth token` stay allowed, as do quoted
    `echo`/`grep` prose, `python -c` bodies and heredoc bodies — the same
    matching style and the same exemptions as the other terminal blocks. The
    message names the required per-command form
    `GH_TOKEN=$(gh auth token -u <user>) <command>` and the default active
    account `msanchez19`. Pinned by
    `shell-guards/tests/test_gh_auth_guard.py`.
