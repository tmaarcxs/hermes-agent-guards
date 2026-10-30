# text-guards

Answers: *does anything the agent emits contain a character the operator forbids?*

The operator forbids the section sign and the em dash in anything the agent writes. This plugin
cuts them out on both paths where text leaves the agent:

- `transform_llm_output` — the final reply, before delivery and before the assistant row is
  persisted.
- `pre_tool_call` — the tool arguments, so a comment body, a file written by `write_file` or
  `patch`, and a shell command are cleaned before the tool runs.

Both replace every occurrence, with its surrounding spaces, by a single space, so `4.3` stays
readable. A hyphen (`-`) and an en dash are left alone: only the section sign and the em dash are
forbidden. When nothing carries a forbidden character the hooks return `None`, leaving the call
untouched and the reply slot free for another transform.

It never blocks a call, rejects an argument or changes any other text, and any internal error
lets the original value through.

## Limits

Arguments are cleaned by value, so a command that deliberately searches for the character must
spell it another way (for example `chr(167)` in Python). Text the operator writes is not
affected: only what the agent emits passes through these hooks.

## Tests

```
PYTHONPATH="$HOME/.hermes/hermes-agent" ~/.hermes/hermes-agent/venv/bin/python -m pytest text-guards
```

## Install

```
hermes plugins install tmaarcxs/hermes-agent-guards#text-guards
```
