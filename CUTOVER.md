# Cutover: replace `agent-guards` with the three plugin folders

Ordered activation steps for the real install. **Do not run these as part of the
integration task** — this document is the plan the operator executes.

Precondition: `split/integrate` is merged to the default branch, and the three
folders are reachable at `tmaarcxs/hermes-agent-guards#shell-guards`,
`#workspace-guards`, `#kanban-guards`.

## Verified current state (read from the live configs, 2026-10-22)

- `~/.hermes/config.yaml` (default profile, a real file):
  - `plugins.enabled` = `['agent-guards', 'disk-cleanup', 'hermes-auto-titler', 'hindsight', 'progress-squash', 'task-retro', 'telegram-mirror', 'time-gap', 'web/firecrawl']`
  - `plugins.entries.agent-guards.allow_gateway_injection: true`
- `~/.hermes/profiles/worker/config.yaml` (worker profile, a real file — **not**
  a symlink to the agent-guards tree):
  - `plugins.enabled` = `['agent-guards', 'disk-cleanup', 'hindsight', 'task-retro', 'web/firecrawl']`
  - `plugins.entries` = `{'hindsight': {'allow_tool_override': False}}` — no
    `agent-guards` entry, so the worker carries no gateway-injection grant
    today (wakes never load there).
- Installed tree: `~/.hermes/plugins/agent-guards` (plus the
  `~/.hermes/plugins/agent_guards -> agent-guards` symlink).

## Steps

### 1. Install the three plugins

```
hermes plugins install tmaarcxs/hermes-agent-guards#shell-guards
hermes plugins install tmaarcxs/hermes-agent-guards#workspace-guards
hermes plugins install tmaarcxs/hermes-agent-guards#kanban-guards
```

Each install lands `~/.hermes/plugins/<name>/`. The `#<subdir>` fragment is the
CLI's subdirectory syntax (`_resolve_git_url` in `hermes_cli/plugins_cmd.py`).
For the worker profile add `-p worker` to each command.

### 2. Edit `plugins.enabled` in both configs

`~/.hermes/config.yaml` — replace `agent-guards` with the three names:

```yaml
plugins:
  enabled:
    - shell-guards
    - workspace-guards
    - kanban-guards
    - disk-cleanup
    - hermes-auto-titler
    - hindsight
    - progress-squash
    - task-retro
    - telegram-mirror
    - time-gap
    - web/firecrawl
```

`~/.hermes/profiles/worker/config.yaml` — same replacement:

```yaml
plugins:
  enabled:
    - shell-guards
    - workspace-guards
    - kanban-guards
    - disk-cleanup
    - hindsight
    - task-retro
    - web/firecrawl
```

### 3. Move the gateway-injection grant

The wakes need gateway injection. In `~/.hermes/config.yaml`, move the flag from
`agent-guards` to `kanban-guards` (the plugin that now owns `wakes`):

```yaml
plugins:
  entries:
    kanban-guards:
      allow_gateway_injection: true
```

Remove the `plugins.entries.agent-guards` block. The worker profile needs no
grant (wakes never load there) — leave its `entries` as-is.

### 4. One gateway restart

```
hermes gateway restart
```

Exactly one restart: the six/three-plugin load order is resolved at startup, and
`plugins.enabled` changes do not hot-reload.

### 5. Post-restart probes

Run these once, in order:

1. **One `sleep 45` block.** Ask a worker to run `sleep 45` in the foreground:
   `shell-guards` must block it with `agent-guards blocked terminal [__init__]:
   foreground sleep >= 30s: …`.
2. **One lead `kanban_list` scope.** From a lead chat with a messaging origin,
   call `kanban_list`: the result must be scoped to that chat's subscribed cards
   and carry `hidden_foreign` (`workspace_scope_gate` inside `kanban-guards`).
3. **One real wake injection.** Park a card with `kanban_block` and confirm the
   injected `[Kanban stale card]` message reaches the subscribed chat within one
   dispatcher tick. `doctor` cannot see injection denial, so this must be
   observed in the gateway log / chat.

Then watch one dispatcher tick: `~/.hermes/state/kanban-*-wake.json` mtimes
advance only for the wakes that actually fired.

## Revert (git only)

Do **not** run `hermes plugins remove` — it wipes `plugins.entries`.

1. `git checkout <pre-split-sha> -- .` in the plugin checkout (or reinstall the
   old tree from the pre-split revision).
2. Flip `plugins.enabled` back to `agent-guards` in both configs and drop the
   three new names.
3. Move `allow_gateway_injection` back to `plugins.entries.agent-guards`.
4. One gateway restart.

## Notes

- The worker profile's `plugins.entries` has no `agent-guards` key today, so the
  "move the grant" step only applies to the default profile.
- `~/.hermes/plugins/agent_guards -> agent-guards` is a compatibility symlink
  created by the old install; remove it after a successful cutover if nothing
  else references it.
