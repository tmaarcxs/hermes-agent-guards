# Jot reviewer

The default-profile background reviewer maintains the professional weekly board. It keeps the existing proposal, deterministic gate, independent confirmation, revision check, two-cell isolation and readback workflow.

## Granularity

Version 0.11.0 stops the board from filling with steps. The proposal prompt defaults to no add: a new row is one outcome Marcos would name as a task, and work that belongs to an outcome already on the board only updates that row. Every proposed change carries a required `parent` field, the exact Tarea of the closest existing row; an add that names a parent is discarded. The deterministic gate also drops an add whose Tarea names an artifact file (png, pdf, csv, script and similar), allows at most two new outcomes per review, and refuses any new row once today's table holds eight rows. A move of an open row to today is exempt from all three. The second checker skips an add that is a step, deploy, diagram, credential retrieval or follow-up of work a row already covers.

Measured on the Wednesday 18 Nov board: 31 adds and 42 sets in two days, 27 rows in one day, 12 of them with a diagram, deploy, secret or sub-PR as the whole outcome.

Calibrated on the proposal stage with the cron model, 5 runs per case over 11 scripted turns: unwanted adds after the gate fell from 13 to 1 (22 to 10 adds in total; the 9 kept are the genuinely new outcomes). A deploy, diagram, secret retrieval or sub-PR of a tracked outcome went from added in 5 of 5 runs to 0 of 5.

## Silent mode

Version 0.10.0 adds `plugins.entries.hermes-agent-guards/jot-reviewer.notices`. Set it to `false` to keep reviews applying board changes without sending the chat notice. Absent, unreadable or any other value keeps notices on.

## Writer capability

Version 0.8.0 owns its TypeScript runner instead of assuming that Hermes core has a `node_modules/.bin/tsx`. Before any model call, the production worker imports the actual installed Jot store and rich-document validator through that runner. A missing/broken runner, missing helper or failed import skips the review with one explicit diagnostic. It does not disable Jot collaboration or change model routing.

From this plugin directory:

```sh
python3 install-runtime.py
```

The installer uses the lockfile, disables package lifecycle scripts and installs under the plugin's private `.venv` directory. It links `node_modules` to that directory. It never writes Hermes core or shared global packages. Use `--dev` for TypeScript checking. An unmanaged existing `node_modules` is refused rather than deleted.

A working external `tsx` remains a compatibility fallback. The owned runtime uses Node `--import` with tsx's public loader, not its CLI IPC server; this preserves long scratch paths on macOS/Node 24. The runtime import probe applies to either route. Dependency installation is not gateway activation; update and hot-reload the published plugin, then verify the real registered reviewer event.

## Verification

The repository's registered-event E2E uses the real PluginLlm, loopback model transport and isolated real Jot backend. It covers writes/readback, provider failover, unavailable runtime and collaboration-disabled rejection. Negative cases assert zero requests at the actual model HTTP transport, not an unused model double. No scenario writes the user's board. Model replies are controlled external fixtures, not provider savings measurements.

## Desktop delivery

Version 0.9.1 admits native Desktop turns only with a matching active local policy, profile authority, session, route, chat and exactly one unfinished agent. It captures that agents existing background-review callback, which publishes `review.summary` after the reply without opening another turn or sending to Telegram. Other local surfaces and unattended runs remain idle. Telegram private notices and group suppression are unchanged. All company-only proposal, independent confirmation, revision, serialization, isolation and readback gates remain intact. The Desktop resolver is byte-identical to Hindsight reviewers copy.

The Desktop owner is looked up by its stable chat id, so transcript-id rotation does not disable review. Repeated unfinished worker holders for the same cached agent are deduplicated; distinct matching agents remain ambiguous and fail closed. The validated sender is captured once when the local turn is admitted, then removed from the origin metadata before job construction.

Native Desktop does not need a prior messaging dispatch to bind the gateway: when the ingress cache is empty, the resolver reads the existing gateway runners weak reference from the already-loaded core module. The same active-profile, Desktop-policy, stable-owner, current-transcript and unattended gates still apply. No module import, gateway creation or core mutation is performed.
