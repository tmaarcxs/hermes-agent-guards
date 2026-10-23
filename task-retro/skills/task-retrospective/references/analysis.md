# Analysis: reading one worker session and citing findings

Goal: turn a raw worker session into a short list of **concrete model failures**, each anchored to a
message id. Read the whole session once; do not re-derive it in a second pass.

## 1. Locate the session

The card body may carry the session id directly. When it does not, `scripts/session_dump.py` resolves
it from `~/.hermes/kanban.db`:

```
sqlite3 "file:$HOME/.hermes/kanban.db?mode=ro" \
  "SELECT task_id, profile, json_extract(metadata,'$.worker_session_id') \
   FROM task_runs WHERE task_id='<task_id>' ORDER BY id DESC"
```

On a live WAL database this `mode=ro` read can fail with sqlite error 14
(`unable to open database file`) when the `-wal`/`-shm` sidecars are absent or unreadable in place.
`session_dump.py` handles that itself: it opens in place first, and on that error snapshots the
database plus any readable sidecars into a temp dir and reads the copy — with `immutable=1` when the
sidecars are absent, which reads a WAL-header database without them. Do not hand-roll a workaround;
let the script open the database. The live database is never written to.

Prefer the newest run whose `metadata.worker_session_id` is set. A card retried several times has
several runs, and only the successful one carries the handoff metadata. If no run carries a session
id, fall back to the session whose first user message is `work kanban task <task_id>` — and say in the
summary which route you used.

Cross-check the session against the card's own record before trusting it: the card's `result`,
`summary` and `metadata` in `tasks`/`task_runs`, plus `task_comments` and `task_events`, tell you what
the worker claimed and what reviewers said.

## 2. Read the whole session

`session_dump.py` prints, in order: a session summary (message/role/tool counts, the model), the
failure signals, the lifecycle calls, the duplicated calls, then the timeline.

The timeline is the primary artifact. Each row is `message_id role tool_names first-240-chars`. Read
it end to end. Use `--timeline-limit`/`--width` to widen when a row is cut; the assistant rows that
carry only tool calls show the call names, and the `tool` rows show what came back.

For anything the timeline truncates, read the raw row:

```
sqlite3 "file:$HOME/.hermes/profiles/worker/state.db?mode=ro" \
  "SELECT id, role, tool_name, content, tool_calls FROM messages WHERE session_id='<sid>' AND id=<mid>"
```

Assistant `tool_calls` is a JSON array of `{function:{name,arguments}}`; `arguments` is itself a JSON
string. Parse both, do not eyeball a truncated copy.

## 3. Failure taxonomy

Classify every candidate as exactly one of these; if none fits, it is not a finding.

- **wrong or repeated tool use** — a tool that cannot do the job, a call retried with identical
  arguments, an avoidable retry after a schema/argument error that a single `--help` would have
  prevented. Evidence: the duplicated-call list, or two adjacent `tool` rows with the same error.
- **skipped verification** — the acceptance criteria name a check and the timeline never runs it; or
  a claim of success with no readback, no test, no live query behind it.
- **violated rule** — a SOUL, card-body or governing-skill rule contradicted by the observed call
  (wrote outside the workspace, edited a forbidden target, shelled out where the card said to use a
  tool, changed expected results to fit output).
- **avoidable retries / wasted effort** — the same information fetched twice, a long poll loop for a
  result already available, a rebuilt artifact that existed.
- **false completion claim** — the handoff asserts more than the evidence supports: "verified" with
  no receipt, a hash or count that does not match, a test claimed run that never appears.
- **reviewer correction** — a `request_changes` round, a blocking comment, or a re-dispatch whose
  cause is visible in the session. This is the strongest evidence class: prefer it over inference.

Also record **near-misses**: a correct call made only after a wrong one, a rule obeyed by luck. They
justify a sharper rule even when the run succeeded.

## 4. Evidence rules

- One finding = one message id (plus a second when the pair matters, e.g. call and its retry).
- Quote the smallest decisive fragment, not the whole tool result.
- Distinguish observed behaviour from inference; mark inference as such and say what would confirm it.
- A failing tool result is not by itself a model failure: a probe that fails and is then corrected is
  good practice. The failure is repeating it, ignoring it, or hiding it.
- Nonzero truncation, an unresolvable session id, or a collector failure is a blocker to record, not
  a lesson to invent.

## 5. Output shape

Produce a findings list before touching any skill or memory:

```
finding N: <one-line title>
  class:   <taxonomy class>
  evidence: msg <id> (<tool>) — "<quote>"
  rule:    <the rule that was broken or missing>
  level:   <procedure | fact | check | none>
  action:  <skill file + hash | hindsight retain | already_covered | deferred>
```

A run with zero findings ends here with `findings: 0` and `already_covered` filled in. That is a
valid, complete retro.
