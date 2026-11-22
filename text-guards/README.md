# text-guards

Answers: *does the reply contain a character the operator forbids?*

The operator forbids the em dash and the section sign in anything the agent says. This plugin
rewrites the final reply on `transform_llm_output`, before delivery and before the assistant row is
persisted, so the user sees the cleaned text and the next turn replays it.

| Found | Becomes |
|---|---|
| a spaced em dash between words | a comma and a space |
| an em dash that opens a line | a hyphen bullet, indentation kept |
| an em dash between two digits, no spaces | a hyphen |
| an em dash before closing punctuation or at a line end | dropped |
| the section sign | a single space, dropped at the start of a line |

Fenced blocks and inline code spans are data and are never rewritten. When nothing needs cleaning
the hook returns `None`, so the reply slot stays free for another transform.

## What it does not do

- It never rewrites a tool argument. The 0.3.0 plugin did, and a `patch` or `write_file` whose
  content legitimately contained the character stopped matching or lost it. The 2026-11-05
  report "the em dash guard does not work" was that damage.
- It does not see commentary the agent sends while a turn is still running: that text never
  reaches `transform_llm_output`. In the last ten days of sessions about 90 percent of replies that
  carried the dash were final replies. Covering the rest means registering the hook with
  `hold_stream=True`, which withholds streaming and mid-turn commentary for the whole profile.
- It cannot stop a third party from seeing the character in text a tool posts on its own.

## Tests

```
PYTHONPATH="$HOME/.hermes/hermes-agent" ~/.hermes/hermes-agent/venv/bin/python -m pytest text-guards
```

## Install

```
hermes plugins install tmaarcxs/hermes-agent-guards#text-guards
```
