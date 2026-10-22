# Split equivalence: old `agent-guards` vs the three folders

Measured by loading the old monolithic plugin at base `90456b2` and the three
new folders in one interpreter with unique package names
(`scratch/equiv.py`). Hook callbacks are listed per hook; the check order is the
intra-plugin `pre_tool_call` tuple.

## Registered callbacks per hook

| Hook | OLD (one plugin) | NEW kanban-guards | NEW shell-guards | NEW workspace-guards |
|---|---|---|---|---|
| `pre_tool_call` | 1 (`__init__._pre_tool_call`) | 1 | 1 | 1 |
| `post_tool_call` | 2 (`__init__._post_tool_call`, `wakes._wake_poke`) | 2 (same two) | 1 (no-op placeholder) | 0 |
| `pre_verify` | 1 (`stop_gate.check`) | 1 (`stop_gate.check`) | 0 | 0 |
| `transform_tool_result` | 1 (`__init__._transform_tool_result`) | 1 | 1 | 0 |
| `on_kanban_dispatch_tick` | 6 (the six `wakes` ticks) | 6 (same six) | 0 | 0 |

Total registered callbacks: OLD 11, NEW 3 + 4 + 1 = 8 (kanban 11, shell 3,
workspace 1). The old root registered 11 hooks; the new folders register 15
across the three plugins because the two placeholder/no-op registrations
(`shell-guards._post_tool_call`) exist per plugin. Hook totals per plugin match
each `plugin.yaml`.

## Intra-plugin `pre_tool_call` check order

OLD (root `__init__.py:274-291`):

```
checkpoint_gate, workspace_write_gate, home_litter_gate, kanban_policy,
workspace_convention, lead_delegation, github_contact, paas_promote_gate,
review_gate, handoff_gate, completion_gate, backup_copy_gate, schedule_gate,
skill_size, skill_size(record), background_notify_gate
```

then the terminal/attach/normalize tail.

NEW, by plugin:

```
kanban-guards:    checkpoint_gate, kanban_policy, lead_delegation, github_contact,
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
   `workspace-guards`. In the monolith the single root tuple decided. No known
   call is blocked by two plugins at once (their tool sets are disjoint: Kanban
   lifecycle vs shell commands vs write paths), so this is latent.

2. **`transform_tool_result` no longer chains across plugins.** OLD ran one
   callback that chained `completion_gate → workspace_scope_gate →
   kanban_show_slim_gate → skill_size → lead_work_nudge`. NEW splits that into
   two plugins: `kanban-guards` keeps
   `completion_gate → workspace_scope_gate → kanban_show_slim_gate →
   lead_work_nudge` (unchanged inside the plugin) and `shell-guards` owns
   `skill_size.transform`. The host keeps the **first non-`None` string** and
   drops the rest (`model_tools.py:849-866`). The Kanban and shell transforms are
   mutually exclusive in practice (Kanban transforms fire on `kanban_show` /
   completion results; the skill-size transform fires on
   `terminal`/`execute_code`/`patch` results). The one accepted corner: a lead
   turn that both crosses the nudge threshold and leaves an oversized skill file
   keeps only the **kanban-guards** result (it loads first). Pinned by
   `tests/test_transform_winner.py::test_first_non_none_string_wins_in_host_order`.

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

5. **`pre_verify` / `on_kanban_dispatch_tick` unchanged.** Both remain only in
   `kanban-guards` with the same callbacks as the monolith.

6. **`post_tool_call`.** OLD had `__init__._post_tool_call` (recording into
   `review_gate`, `stop_gate`, `checkpoint_gate`) plus `wakes._wake_poke`.
   `kanban-guards` keeps both verbatim; `shell-guards` registers a no-op
   placeholder so its declared hook surface is stable; `workspace-guards`
   registers none.
