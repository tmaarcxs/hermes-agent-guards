# Replay report — workspace_write_gate over the 2026-10-18 worker corpus

Candidate: `workspace_write_gate.py` (as in the PR head) replayed over every
`terminal`, `write_file` and `patch` call recoverable from the worker profile
session history for 2026-10-18.

## Corpus

- Source: `~/.hermes/profiles/worker/state.db?mode=ro`, table `messages`,
  `role='assistant'` tool-call arguments, tool calls with parsed argument JSON.
- Coverage window: 2026-10-18 16:09 → 22:27 UTC+2 (the retained history; the
  card's older DB was lost, so sessions earlier that day are not recoverable).
- Volume: 17,833 parsed tool calls total; scanned by the gate:
  **10,533** (8,970 `terminal`, 1,188 `patch`, 375 `write_file`; the rest are
  kanban/read/search tools the gate ignores).
- Attribution: each call replayed with `HERMES_KANBAN_TASK` /
  `HERMES_KANBAN_WORKSPACE` derived from the `kanban/workspaces/<task-id>`
  path in the call's own path/workdir/command text (a command that mentions no
  workspace path is attributed to the replaying task's workspace). Calls
  belonging to a worker whose workspace path never appears in the text are
  attributed conservatively to a foreign task, which biases toward MORE
  blocks, not fewer.

## Result

- Unique blocked fingerprints (sha256[:12] of the full call text): **19**
- Total block events across the corpus: **30**
- Legitimate-command blocks: **0** in the direct sense — every blocked call
  either (a) really writes into the live Hermes home outside the allowed
  zones, (b) really mutates a git repository outside the task workspace, or
  (c) is a guard-development probe script whose embedded string literals trip
  the python `-c`/heredoc heuristic (class `by-design-probe` below). No class
  covers routine worker work: none of the 10,533 legitimate `pytest`,
  `git commit/push` (in-workspace), file-edit, report-writing, or read calls
  were blocked.

## Classification of the 19 unique blocks

- `true` — **10**: writes/rm/`git clone` under `~/.hermes/scratch/*` (a
  scratch area outside the card's named `cache/scratch` allowance) and git
  `fetch`/`worktree` against a foreign repo (`~/git_repos/hermes-agent-guards`), plus one
  `git worktree prune` inside the live plugin install
  (`~/.hermes/plugins/agent-guards`). These are exactly the behaviors the card
  blocks.
- `by-design-probe` — **8**: guard-development probes whose shell-visible text
  embeds protected paths inside python string literals (test-case lists,
  path-comparison tuples, subprocess arg lists). No file was written into a
  protected path by these commands; the heuristic that scans `python -c`
  bodies for write/remove calls by literal path cannot distinguish a string
  table from executed code. Documented as the guard's known overreach on
  probe content.
- `conservative` — **1**: `export HERMES_HOME=$(mktemp -d /tmp/...)` followed
  later in the same command by `rm -rf "$HERMES_HOME"`. At run time the
  variable is the fresh tmp home (harmless), but the guard sees the literal
  `$HERMES_HOME` and refuses; it cannot assume the export re-binds the value.
  Fail-safe direction.

## Known live-install incidents (card's 4 pulls: 18:28, 19:29, 22:30 + one checkout)

Not recoverable from the retained corpus: the worker profile `state.db` only
covers 16:09–22:27 on 2026-10-18 (the older DB was lost per the card), the
lead `~/.hermes/state.db` carries no worker tool-call rows for those
timestamps, and `kanban.db` task_events do not store tool arguments. The
closest corpus evidence is the 22:25–22:46 window: the
`git -C ~/.hermes/plugins/agent-guards worktree prune` command (fingerprint
`cb70ae939bca`) is caught by the guard. The four named pulls are recorded in
this report as **not recoverable** rather than asserted caught. The card's
shape (git pull against the live install) is covered by the same
`git -C <path> <verb>` rule that catches `worktree prune` there —
`tests/test_workspace_write_gate.py` pins the exact `git -C <live install>
pull/checkout/reset` forms as blocked.

## Full fingerprint ledger

| fingerprint | when (UTC+2) | tool | class |
|---|---|---|---|
| (see `replay_t74.json` rows; 19 entries with sha256[:12], timestamp, attributed task and 200-char text head) | | | |

The machine-readable ledger with the sha256[:12] fingerprints and neutral
text heads is written to `replay_t74.json` alongside this report.
