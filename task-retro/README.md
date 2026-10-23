# task-retro

Auto-create a retrospective kanban card when a worker card completes.

Every qualifying completed worker run spawns exactly one
`retro: <original title>` card assigned to `kanban.default_assignee`, carrying
the `task-retro:task-retrospective` skill and the worker session id. Nothing is
routed to the lead chat and no chat is notified.

Retros are **serialized**: at most three are active at a time (see
[Serialization](#serialization-three-retros-at-a-time)).

The plugin is **self-contained**: it ships the retrospective skill inside its own
tree under `skills/task-retrospective/` and registers it with
`ctx.register_skill(...)`, so installing the plugin alone is enough. The skill is
served read-only as `task-retro:task-retrospective` and is **not** copied into
`~/.hermes/skills/`; the skill instructions derive their script paths from the
`[Skill directory: ...]` the loader reports.

## Install / enable

The plugin ships from this monorepo as the `task-retro/` folder:

```
hermes plugins install tmaarcxs/hermes-agent-guards#task-retro
```

It is a fourth self-contained folder alongside the three guards: its own
`plugin.yaml`, its own `register()`, its own tests, no imports into a sibling
folder. The hook fires **in the worker process** (the dispatcher-spawned worker
calls `kanban_complete`, which commits and then fires `kanban_task_completed`).
Enable it in the profile whose workers complete cards:

```
hermes plugins enable task-retro
```

The lead owns that step; the plugin never edits `config.yaml` itself.

## Behaviour

| Step | Rule |
|---|---|
| Hook | `kanban_task_completed` (kwargs: `task_id`, `board`, `assignee`, `run_id`, `profile_name`, `summary`) |
| Hook | `kanban_task_blocked` (kwargs: `task_id`, `board`, `assignee`, `run_id`, `profile_name`, `reason`) — archives a blocked retro |
| Card | title `retro: <original title>`, assignee from `kanban.default_assignee` (never hardcoded) |
| Skills | `["task-retro:task-retrospective"]` — the qualified name of the skill the plugin itself registers |
| Skill | registered from `<plugin>/skills/task-retrospective/SKILL.md`; a missing tree logs and does not stop the hook |
| Idempotency | `idempotency_key = "retro:<task_id>"` — retries never duplicate |
| Body | task_id, run_id, board, original title, handoff summary, worker session id, plus the qualified skill name and a `skill_view` fallback |
| Session id | `task_runs.metadata.worker_session_id` for the run, falling back to `HERMES_SESSION_ID`; if neither, the body says so and points at `state.db` |
| Loop guard | skip when the completed title starts with `retro:` **or** it carries the `task-retrospective` / `task-retro:task-retrospective` skill |
| Probe filter | skip when the original title contains `[probe]`, starts with `probe:`, or contains `throwaway` (case-insensitive; the marker list is the `_PROBE_MARKERS` / `_PROBE_PREFIX` constants) |
| Filters | only `outcome == "completed"`; skip cards whose **total run wall** (the sum of all the parent card's runs) is below `min_duration_seconds` (default 900) |
| Age cap | in the same sweep, an OPEN retro (`todo`/`ready`) queued longer than `queue_max_age_seconds` (default 21600 = 6 h) is archived with a comment naming its parent, releasing its queued children; a `running` retro is never aged out |
| Serialization | at most `_MAX_ACTIVE_RETROS` (3) open lane tails; a new retro with a free lane is unparented, otherwise it is linked as a **child** of the oldest lane tail (`todo`/`ready`/`running`), waits in `todo` and auto-promotes when that parent finishes; the tail lookup and the insert share ONE `write_txn` |
| Archive on block | the `kanban_task_blocked` hook archives a blocked retro (`kanban_db.archive_task` + `recompute_ready`), releasing its queued children; the dispatcher's circuit-breaker parks cards with no hook, so every completed-hook fire also sweeps retros left in `blocked`/`triage` |
| Notify | **none** — no `creator_task_id`, so neither notify-sub inheritance nor the `kanban_create` auto-subscribe path runs |
| Failure | any exception is logged and swallowed; neither hook ever raises or blocks a transition |

## Serialization: three retros at a time

Retros are assigned to the same `worker` profile as real work, so a per-profile
dispatcher cap (`kanban.max_in_progress_per_profile`) would throttle real
workers instead of retros. The limit therefore lives **in the plugin**: open
retros (status `todo`/`ready`/`running`) form at most `_MAX_ACTIVE_RETROS` = 3
lanes, where a *lane tail* is an open retro with no open retro child.

- **A free lane means no parent.** When fewer than 3 tails are open, the new
  retro card is created unparented and starts immediately.
- **A full board round-robins.** When all 3 lanes are taken, the new retro is
  linked with `parents=[<oldest lane tail>]` (`created_at` asc, id tie-break), so
  `kanban_db.create_task` parks it in `todo` and
  `kanban_db.recompute_ready` promotes it as soon as that parent reaches
  `done`/`archived`. Retros therefore spread across the lanes: A, B, C free;
  D behind A; E behind B; F behind C; G behind D. Nothing is ever dropped.
- **Tail selection and insertion are atomic.** Both run inside ONE
  `kanban_db_connect.write_txn` (`BEGIN IMMEDIATE`; `create_task` rides the
  nested savepoint via `allow_nested=True`), so two workers completing in the
  same instant cannot both read the same lane state and both insert a ready
  retro. The lock covers only that lookup+insert — never slow work — and
  SQLite's own busy timeout governs contention.
- **A blocked retro never stalls its lane.** `recompute_ready` releases a child
  only when **every** parent is `done`/`archived`, so a retro that gets blocked
  while it already has queued children would park its subtree in `todo` forever.
  Nobody ever unblocks a retro, so the `kanban_task_blocked` hook archives it
  (`kanban_db.archive_task`, which calls `recompute_ready` after commit) and its
  queued children are released. The dispatcher's circuit-breaker parks cards with
  a raw `UPDATE` and fires no hook, so every `kanban_task_completed` fire also
  sweeps any retro left in `blocked`/`triage` and archives it. `review` is never
  swept: a review handoff is a live lane.
- **Fail-open.** If the transaction, the tail lookup, the idempotency read, the
  create or the archive fails, the card is still created — unparented — rather
  than lost. A transaction failure retries the insert outside the transaction.
- **An archived retro still protects `retro:<task_id>`.** `create_task`'s own
  idempotency check filters `status != 'archived'`; the plugin checks the key
  itself first (inside the same transaction), so a re-fired completion cannot
  mint a second retro for a task whose retro was archived.

## Config (`plugins.entries.task-retro`)

```yaml
plugins:
  entries:
    task-retro:
      enabled: true
      min_duration_seconds: 900
      queue_max_age_seconds: 21600
      debug: false
```

`min_duration_seconds` is measured on the **parent card's total run wall** (the
sum of every run it had), not on the completing run: two 400 s attempts are an
800 s card and are skipped. `queue_max_age_seconds` bounds the backlog: an OPEN
retro (`todo`/`ready`) older than the cap is archived with a comment naming its
parent, which releases its queued children. A `running` retro is never aged out,
and `0` disables the age sweep.

## Why the board slug is normalized

The hook payload carries `board="default"`. Passing that literal back into
`kanban_db_connect.connect(board=...)` makes `kanban_db._board_path` treat it as
an explicit board intent, which **outranks the `HERMES_KANBAN_DB` pin** and
opens `kanban_home()/kanban.db` instead of the pinned database. The plugin maps
`""`/`"default"` to `None` so the process pin resolves; a named board is passed
through unchanged.

## Tests

```
cd ~/.hermes/plugins/task-retro && python3 -m pytest tests/ -q
```

Unit tests run anywhere (`hermes_cli` is stubbed via `sys.modules`). Coverage:
registration (hook + skill registration from the plugin tree, missing tree,
register_skill failure), card fields, the qualified `skills` value and body
hint, config-derived assignee, session-id resolution and its absence,
idempotency (including the archived-retro case), both loop guards (bare and
qualified names, plus a near-miss that must NOT suppress), the trivial-run filter
and its configurability, the outcome filter, the disabled switch, board-pin
resolution, the probe filter, the lanes (0/1/2 open → unparented; 3 open → child
of the oldest tail; 7 retros A..G → lanes A→D→G, B→E, C→F; blocked/archived
parent ignored), the atomic create path (one `write_txn` around tail lookup +
insert; a transaction failure retries unparented; an absent `write_txn` still
creates the card), the blocked hook (archives a retro, ignores non-retro cards,
fail-open), the parked-retro sweep (a `blocked`/`triage` retro is archived,
`review` is left alone), and the fail-open paths.

`tests/test_chain_behaviour.py` is the real-database proof: it drives the
plugin against a **temporary** kanban DB through the real
`hermes_cli.kanban_db` API (never the live `~/.hermes/kanban.db`) and asserts
that three retros fill the three lanes `ready`, a fourth parks in `todo` behind
the oldest lane tail, and it is promoted to `ready` by `recompute_ready` when
that parent completes. It also carries the concurrency and
block regressions: a real 4-thread concurrent-completion driver (the four
threads are released at the `write_txn` boundary, the real check-then-act
window, so the barrier genuinely fires for all four) proving exactly 3 retros
end `ready` and 1 `todo` as a child, and the blocked A → D scenario proving the
hook archives A and D becomes `ready`.
It needs the Hermes environment, so it skips (with a stated reason) when
`hermes_cli` cannot be imported; run it with an interpreter that can — either
the 3.14 uv run, or the Hermes venv 3.11 with `hermes-agent` on the path:

```
uv run --no-project --python ~/.hermes/hermes-agent/.venv/bin/python3 \
  --with pytest python -m pytest tests/ -q
PYTHONPATH=~/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -q
```

## Verified evidence

- `hermes plugins validate .` — passes (only advisory: private core imports of
  `hermes_cli.kanban_db` / `kanban_db_connect`, which the card mandates).
- `hermes plugins doctor task-retro` — OK in both homes: runtime discovery,
  manifest parsing, import and registration; 2 hooks.
- `skill_view('task-retro:task-retrospective')` resolves in a fresh process for
  both profiles; `references/analysis.md` and `references/actions.md` resolve
  through the plugin.
- Live end-to-end: throwaway card `t_750001cb` (130 s run) completed and the hook
  created exactly one retro card `t_49fb0a98` with
  `skills=["task-retro:task-retrospective"]`, the qualified name in its body, and
  zero notify subscriptions. (Under the new `min_duration_seconds: 900` measured
  on the card's total wall, that 130 s run is skipped, and `t_750001cb`'s
  `probe:` title is itself filtered.)

## Dispatcher concurrency caps (reported, not changed)

`skipped_per_profile_capped` is populated in `hermes_cli/kanban_db_dispatch.py`
only when `kanban.max_in_progress_per_profile` is a positive int
(`_dispatch_tick`, ~line 2364; gate at ~line 2065).

- Default: `None` → **no per-profile cap** (`hermes_cli/config_defaults.py`).
- Gateway path: `gateway/kanban_watchers_dispatcher.py` reads it via
  `_positive_int_setting(kanban_cfg, "max_in_progress_per_profile")`.
- CLI path: `hermes_cli/kanban_ops.py` `_cmd_dispatch`.
- On this Mac it is unset in both profiles, so the counter cannot fire.
- Related: `kanban.max_in_progress` (global, memory-derived; `None` on macOS
  because MemTotal is unreadable) and `kanban.max_spawn` (per-tick).

Nothing in the plugin or its config sets any of these. The three-retro-at-a-time
limit is enforced by the plugin's lane chain, not by any dispatcher cap.