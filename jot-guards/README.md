# jot-guards

One gate: keep the Jot weekly board current before a work turn ends.

`pre_verify` fires at the point the agent would accept its final answer after editing files.
When the turn changed a file inside a git work tree and called no mutating Jot tool
(`jot_create`, `jot_update`, `jot_set_task`, `jot_delete`), this plugin returns a `continue`
directive: the attempted answer becomes an interim row and the agent updates Jot in the same
turn. `jot_read` does not count — reading the board records nothing.

Scope, so it does not fire on every turn:

- a turn counts as work only when a changed path sits in a git work tree;
- paths under the skip roots (default `~/.hermes`) never count, so agent upkeep is silent;
- worker processes (`HERMES_KANBAN_TASK` set) are skipped: their handoff is the card, and the
  lead keeps the Jot line;
- one nudge per turn (`attempt`), bounded by `agent.max_verify_nudges` in the host;
- silent as soon as a mutating Jot call ran in that turn.

Settings live under `plugins.entries.jot-guards.settings`:

```yaml
plugins:
  entries:
    jot-guards:
      settings:
        jot_nudge: true                  # false switches the reminder off
        jot_nudge_skip_roots: ["~/.hermes"]   # never professional work
```

Known limit, Hermes-side: `pre_verify` only fires on a turn that mutated files through a file
tool. A turn that only runs commands or queries gets no reminder; covering those needs a core
stop gate, not a plugin hook.

Run the tests:

```bash
PYTHONPATH="$HOME/.hermes/hermes-agent" ~/.hermes/hermes-agent/venv/bin/python -m pytest -q tests
```
