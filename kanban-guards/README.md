# kanban-guards

Everything Kanban in one self-contained plugin. Worker gates (checkpoint, completion
acceptance, review checklist, handoff, stop, schedule, backup-copy, GitHub contact,
delegation policy), lead gates (lead delegation, work nudge, board output shaping) and
the dispatcher wake chassis (`wakes` + `board_watch`).

Extracted from the monolithic `agent-guards` plugin; registration order inside this
plugin reproduces the Kanban part of the old root wiring verbatim (see the seam note in
`__init__.py`). Terminal hygiene (sleep/polling, PR-merge pin, osascript, force push,
home deletion), the inline-attach cap and Kanban argument normalization live in
`shell-guards`; write-location policy lives in `workspace-guards`; this plugin imports
nothing from them.

Run the tests:

```bash
PYTHONPATH="$HOME/.hermes/hermes-agent" ~/.hermes/hermes-agent/.venv/bin/python -m pytest -q tests
```
