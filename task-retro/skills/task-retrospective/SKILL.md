---
name: task-retrospective
description: "Mine a finished worker session and fix its governing rule."
version: 0.2.0
author: Marcos Sanchez, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [retrospective, kanban, verification, skills, hindsight]
    related_skills: [nightly-self-improvement, hermes-agent-skill-authoring, subagent-orchestration]
---

# Task Retrospective

Post-card retrospective of one finished worker session: find concrete model failures with cited
evidence, then apply the smallest durable correction yourself. Nothing is routed to the lead and no
card is created. It is not a review lane, not a re-run, and not a second opinion on the deliverable.

This skill ships inside the `task-retro` plugin and resolves as `task-retro:task-retrospective`.
Its scripts and references live in the skill directory the loader reports (`[Skill directory: ...]`
in the preload banner, or `skill_dir` in the `skill_view` result). Use that absolute path; never
assume a `~/.hermes/skills/...` location — the plugin is not copied there.

## When to Use

- A `retro: <title>` card is dispatched to you and its body names a `task_id`, `run_id` or worker session id.
- You are asked to mine one finished worker session for model failures and fix the governing guidance.
- Don't use for: reviewing a delivery before merge (`sdlc-review`); the lead's own chats and corrections (`nightly-self-improvement`); a session that is still running.

## Prerequisites

- Read-only sqlite over `~/.hermes/profiles/worker/state.db` (worker sessions, `source='kanban'`) and `~/.hermes/kanban.db` (`task_runs`, `task_comments`, `task_events`).
- `python3` (stdlib only) for the two scripts below. No network, no credentials.
- Write access to the skill tree and Hindsight only for the action step; nothing else.

## How to Run

Resolve `<skill_dir>` from the preload banner / `skill_view` result, then:

```
terminal(command="python3 <skill_dir>/scripts/session_dump.py --task-id <task_id>")
```

Then follow `references/analysis.md` (findings) and `references/actions.md` (write gates and the
completion contract), and finish with `kanban_complete`.

## Quick Reference

- Resolve session + timeline: `python3 <skill_dir>/scripts/session_dump.py --task-id t_abc` (or `--session-id <sid>`); add `--timeline-limit 400`.
- Failure signals, duplicated calls, lifecycle calls: same script, same run.
- Original-card comments: `sqlite3 "file:$HOME/.hermes/kanban.db?mode=ro" "SELECT author,body FROM task_comments WHERE task_id='<id>' ORDER BY created_at"`.
- Write lock: `python3 <skill_dir>/scripts/retro_lock.py acquire --owner <task_id> --wait 60`, then `release --owner <task_id>`.
- Library gate: `python3 ~/.hermes/skills/skill-library-audit/scripts/skill_tree_check.py --root <skill_dir>/.. --skill task-retrospective`.
- Write discipline (`references/actions.md`): one finding one home; consolidate before appending;
  route always-on rules to topic references; verify live after the last write; never patch the plugin
  copy (record `deferred`); keep every size gate passing.

## Procedure

1. Read the card body and the original card's comments; note the named `task_id`/`run_id`/session id.
   Done when you can state which worker session this retro covers.
2. Run `session_dump.py` for that task. Done when you have the resolved session id, the failure
   signals, the duplicate-call list and the timeline.
3. Walk the timeline and classify each candidate against the failure taxonomy in
   `references/analysis.md`; cite the message id for every finding. Done when every candidate is
   either a cited finding or explicitly dismissed with a reason.
4. For each finding decide the action level (procedure, fact, or check) with `references/actions.md`.
   Done when every finding has a level and an already-covered verdict.
5. Acquire the write lock, apply the action, read back the result, release the lock. First apply the
   consolidate-first gates in `references/actions.md`: one finding one home, consolidate before
   adding.
   Done when each patch is read back byte-identical and each retain is confirmed by a recall.
6. Run the library gate over the plugin's skill copy. Done when it reports 0 failing.
7. `kanban_complete` with the summary fields in `references/actions.md`. Done when the summary
   carries the findings count, changes applied, `already_covered` and `deferred`.

## Pitfalls

- `session_dump.py` failure markers are substring hits, not verdicts: a task body quoting "failed" or
  a `not_found` in prose trips them. Confirm each one in the timeline before calling it a failure.
- One session id can map to several cards (retries, re-dispatches). Resolve through
  `task_runs.metadata.worker_session_id` first; the first-user-message fallback is a last resort.
- `flock` cannot span separate agent tool calls. The write step uses `retro_lock.py`, which serializes
  only read-target to patch to readback and never the analysis.
- The nightly skill owns lead chats and corrections; the retro owns worker cards. Do not edit it.
- A session with no findings is a valid result. Never manufacture a correction to have something to write.
- Never patch the plugin's own `skills/` copy: the plugin serves the file read-only and
  `plugins install` overwrites it. Corrections go to the governing skill under `~/.hermes/skills/`;
  a lesson with no writable home is recorded as `deferred` for the lead.

## Verification

- Every finding cites a message id and a tool call or quote.
- The library gate reports `0 failing` for the plugin's `task-retrospective` copy.
- Each patched skill is read back after its last mutation and the exact lesson is re-grepped in the
  live file; each retained fact is confirmed with a recall.
- `kanban_complete` summary names findings count, changes (file + hash or recall id), `already_covered`, `deferred`.
