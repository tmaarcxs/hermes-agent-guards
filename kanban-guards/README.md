# kanban-guards

Everything Kanban in one self-contained plugin. Worker gates (checkpoint, completion
acceptance, review checklist, handoff, stop, schedule, backup-copy, scratch-artifact,
GitHub contact, delegation policy), lead gates (work nudge, board output shaping) and
the dispatcher wake chassis (`wakes` + `board_watch`), plus `active_pr_unblock`: a
dispatcher-tick observer that auto-lifts the `active_pr` respawn guard when a human
unblocked a card after its PR comment.

Extracted from the monolithic `agent-guards` plugin; registration order inside this
plugin reproduces the Kanban part of the old root wiring verbatim (see the seam note in
`__init__.py`). Terminal hygiene (sleep/polling, PR-merge pin, osascript, force push,
home deletion), the inline-attach cap and Kanban argument normalization live in
`shell-guards`; write-location policy lives in `workspace-guards`; this plugin imports
nothing from them.

Run the tests:

```bash
PYTHONPATH="$HOME/.hermes/hermes-agent" ~/.hermes/hermes-agent/venv/bin/python -m pytest -q tests
```

## scratch_artifact_gate

Blocks `kanban_complete` and `kanban_request_review` (pre_tool_call, worker setups only)
on a card whose workspace kind is `scratch` — the workspace is deleted on completion, so
anything still only inside it is lost:

- the handoff text (summary, result, metadata values, nested) names an absolute path
  inside the card's scratch workspace that `metadata.artifacts` does not cover: covered
  means the exact path, or an artifact that is that path, a directory containing it, or
  an archive whose members include it (relative to the workspace or the archive stem);
- a git repository under the workspace has commits unreachable from any remote-tracking
  ref while `artifacts` holds no `.bundle`/`.patch` file.

The block message names every uncovered path (or the branch and head SHA of the unpushed
repo) and the exact fix: pass the absolute paths in `metadata.artifacts`
(`git bundle create <abs>.bundle <branch>`, or `git format-patch` for commits), and
states that the scratch workspace is deleted on completion. Fails open on any internal
error, on non-scratch workspaces and outside worker setups.
