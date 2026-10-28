# text-guards

Answers: *does the agent's reply contain a character the operator forbids?*

The operator forbids the section sign (`§`) in anything the agent writes. This plugin cuts it out of
the final reply before delivery:

- Registers one hook: `transform_llm_output`.
- Replaces every occurrence — with the surrounding spaces — by a single space, so `§4.3` becomes
  ` 4.3` and the sentence stays readable.
- Returns `None` when the reply has no such character, so the slot stays free for another
  transform.

It does nothing else. It never blocks a tool call, rewrites an argument, or touches other
text, and any internal error lets the reply through unchanged.

## Scope note

This guard trims the reply only. Text written elsewhere — issue comments, files, Jot notes —
is not affected: put the rule where that text is produced instead of widening this hook.

## Tests

```
PYTHONPATH="$HOME/.hermes/hermes-agent" ~/.hermes/hermes-agent/venv/bin/python -m pytest text-guards
```

## Install

```
hermes plugins install tmaarcxs/hermes-agent-guards#text-guards
```
