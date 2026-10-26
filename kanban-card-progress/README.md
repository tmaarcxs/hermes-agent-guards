# kanban-card-progress

Deterministic per-card progress block for the Hermes Kanban board.

Every ~10 s (operator-set) a gateway-side daemon thread sweeps the **running**
cards, reads the worker's native Tasklist (the persisted `todo_list` tool
result), and rewrites a two-line block at the head of the card body:

```
▶ 3/7 · probando tests
[#########..............] 43%
<the card's original description, untouched>
```

No LLM. No worker cooperation beyond keeping its Tasklist updated. The mapping is
pure arithmetic over the persisted Tasklist, so the same Tasklist always renders
the same block.

## Format (v3.2)

* **line 1** — `▶ N/M · label`, where the label is cut deterministically by the
  measured **pixel** budget: the longest prefix that fits, on a word boundary
  when one fits and otherwise mid-word, and **never** with an ellipsis. `N`/`M`
  may be 1–3 digits each (`▶ 12/34`, `▶ 123/123`).
* **line 2** — `[` + `BAR_WIDTH` of `#`/`.` + `]` + NBSP + `NN%` + a run of NBSP.
  `#` is the filled part, `.` the empty part (v3.1 alphabet; the v3 first cut used
  `_`, v2 used `=`), and the NBSP glues the percentage to the bar so it can never
  be orphaned. The trailing NBSP run is the **line-2 pad** (below): it keeps the
  description from following the percent on the card.
* **line 3+** — the whole Tasklist (v1.2), one `• [mark] text` line per item, then
  the untouched original description (clamped on the card, kept for the drawer).

### The line-2 pad (v3.2)

The card preview is RAW text in a `line-clamp-2` span, so a real newline collapses
to a space and the description flows onto line 2 right after `NN%` whenever the
bar is not full (the 0 % bar line is only 105.54 px of the 209 px box). The fix is
a run of NBSP (U+00A0 — invisible, never collapses, no break opportunity) appended
**after** the percent, wide enough that the next word cannot fit on line 2 and
wraps to line 3, where the clamp hides it:

```
PAD_MIN_PX = 217.00 (largest box) - 3.16 (space) - 2.78 (narrowest glyph)
             + 1.00 (safety)                                        = 212.07
PAD_MAX_PX = 217.00 (largest box) - 1.00                           = 216.00
k = smallest count with  text_width(line2) + k * 3.16 >  PAD_MIN_PX,
    capped so             text_width(line2) + k * 3.16 <= PAD_MAX_PX
```

Every word is preceded by a space and starts with a glyph at least 2.78 px wide
(the `j` advance, the narrowest in the table), so a token wider than
**212.07 px** leaves no room for it. The **1.00 px** safety absorbs the embedded
table's own rounding error (the table over-states the real rendered base by at
most 0.67 px), so the rule blocks the first word in the renderer too, not only on
paper. The token never reaches the **216.00 px** cap (217 px largest box − 1 px).
The pad is an **unbreakable** run: at the 209 px box it overflows invisibly (its
visible part is ≤ 201.95 px, so `NN%` is never clipped) and never wraps itself
onto line 3. It is sized from the **217 px** largest box, so it also blocks a word
at the 209 px box.

Measured over all 101 percentages, the padded line-2 token is **212.11 px** to
**215.17 px** (`k` = 4…35 NBSP, advance **3.16 px**). The **visible** part of
line 2 is unchanged and still ≤ 201.95 px, so `NN%` is never clipped; the pad is
purely invisible space. The hard limit is the 217 px wrap width, which the token
always stays below.

**What the operator loses:** the description is no longer visible on the card face
for any card that carries a block. That is the point of the fix — the card shows
the progress block only; the full description remains in the body and in the
drawer.

**The clamp ellipsis.** When the 2-line clamp hides line 3+, the renderer appends
a **visible** `…` at the end of line 2. Measured in the app's own Blink engine on
all 12 probe shapes: the ellipsis consumes only the invisible NBSP pad — the right
edge of the `%` glyph is **byte-identical** with the clamp on and off (0 % →
115.52 px, 100 % → 211.93 px in both states). So `NN%` is never clipped and no
digit is lost; the ellipsis is cosmetic, sitting in the pad's invisible space.

**Rejected alternatives.** A real newline (collapsed to a space); ZWSP U+200B /
U+2063 (render as nothing and create no break); U+2028 / U+000B (no visible second
line); a visible filler such as `.`/`·`/`-` (shows as noise and `-` breaks
mid-bar); a wider bar or a longer label (the bar width is already at its 205 px
limit, and line 1 must not wrap).

### The Tasklist (v1.2)

The sweep appends one line per Tasklist item **after the pad**, so the list is
part of the block and lands on line 3+: the card face keeps its two lines, and
the whole list appears as soon as the card is opened, above the description.

```
▶ 3/7 · probando tests
[##########.............] 43%<NBSP pad>
• [x] leer el issue
• [>] medir el ancho del texto
• [ ] escribir los tests
• [-] descartado (cancelled)
Objetivo: la descripción original, intacta.
```

`• [x]` is completed, `• [>]` in progress, `• [ ]` pending and `• [-]` cancelled —
the same statuses `map_progress` counts, so `N/M` in line 1 and the list agree.
An item's own line breaks become spaces (one line per item, so the run stays
contiguous), and the run is recognised by its bullet + checkbox, so a sweep
replaces it in place and a strip takes it away with the block: no copies pile up.
`shortcut:` a description line that begins with exactly `• [x] ` right after the
block is read as part of the list; upgrade by bracketing the run in marker lines.


## Measured budgets

Measured on the REAL Hermes desktop app (board `default`, throwaway `[probe]`
cards created `--initial-status blocked`), from the AX tree: one `AXStaticText`
run per rendered line, `label` = the full untruncated text, `frame.w` = the
advance width in CSS px.

| quantity | value | how it was measured |
|---|---|---|
| card text-box **wrap width** | **209 px** (smallest) … **217 px** | the board column is `w-64` (16rem = 256 px); the card is `border` (2+1 px) + `p-2.5` (20 px) and the preview span has no padding, so the box is 256 − 16 − 3 − 20 = 217 CSS px, MINUS the lane's scrollbar. The app styles every scrollbar at `::-webkit-scrollbar { width: 0.5rem }` (8 px, not overlay — it reserves space), so a busy lane renders 209 px and an idle lane 217 px. Measured in the app's own renderer at 900/1400/2200/2560 px windows and at 90/100/110/125 % UI zoom. The historical 231 px was the AX **frame** at the operator's 110 % UI zoom (209 × 1.1 = 230 matches his live cards), i.e. screen pixels, not CSS px |
| safety margin | **4 px** | headroom for font-rendering / rounding differences |
| **usable line budget** | **205 px** | 209 − 4 (the smallest box, because line 1 and the bar are breakable text and must not wrap) |
| `▶` / `·` / space / NBSP | 8.88 / 3.33 / 3.16 / 3.16 px | Blink, card CSS, zoom 1 |
| `#` / `=` / `_` / `.` | 6.99 / 6.99 / 6.48 / **3.33** px | Blink, card CSS, zoom 1 |
| `[` / `]` / `%` | 4.27 / 4.27 / 10.24 px | Blink, card CSS, zoom 1 |
| `0`–`9` | 5.17–7.14 px | Blink, card CSS, zoom 1 |

`.` is much **narrower** than `_` and `=`, so an empty-heavy bar is far cheaper
in pixels — but the binding case (see below) contains no empty glyph.

### `BAR_WIDTH` derivation

```
"[" 4.27 + "]" 4.27 + NBSP 3.16 + "100%" 29.40  = 41.09 px fixed
41.09 + BAR_WIDTH * 6.99 <= 205   ->   BAR_WIDTH = 23   (201.95 px)
```

**`BAR_WIDTH = 23`.** The bar line is **30** chars wide and 201.95 px at the worst
case (`100%`, all `#`), leaving 3.05 px of headroom under the 205 px budget. At
24 the worst case would be 208.95 px — which would NOT fit the 209 px box — so 23
is the widest bar that fits the smallest reachable box.

**Which case limits the width.** The binding case is the **all-`#` 100 % bar**:
it contains no empty glyph, so the narrower `.` buys nothing there. The all-`.`
0 % bar is far narrower (31.94 px fixed + 23·3.33 = 105.54 px). Therefore
`BAR_WIDTH` stays 23 unless the 100 % representation itself changes.

### Label budget

The header `▶ N/M · ` costs 35.44 px (1-digit counts, `▶ 1/1`) to 67.96 px
(3-digit, `▶ 444/444`), so the label budget is **137.04 px … 169.56 px**, i.e.
roughly **23–29 characters** for ordinary lowercase prose. Example: `format_block(12, 34, "verificar la
latencia del endpoint")` keeps the phrase as `▶ 12/34 · verificar la latencia del
endpoint` = 218.91 px — wider than the 205 px line budget, so the cut shortens it
to `▶ 12/34 · verificar la latencia del` = 169.42 px (the label alone is
167.84 px, the `▶ 12/34 · ` header is 51.08 px). `W`-heavy text fits ~13–15 chars
(15 / 14 / 13 `W` for 1 / 2 / 3-digit headers). The old
3-word / 19-char rule is gone.

## How it works

1. **Timer** (`timer.py`) — one daemon thread per gateway process, started from
   `register(ctx)`. Interval from config (10 s here), interruptible stop, every
   cycle wrapped so a failure logs at WARNING and the loop continues. It runs on
   its own thread (not the event loop, not the shared `on_kanban_dispatch_tick`
   hook) because the sweep does blocking SQLite I/O; the dispatch hook is untimed
   and shared with `kanban-guards`, so a slow sweep there would stall every other
   subscriber.

2. **Sweep** (`sweep.py`) — one bounded pass:
   - selects `status = 'running'` cards (cap `max_cards_per_cycle`, wall-clock
     `cycle_budget_seconds`; remaining cards defer to the next cycle),
   - refreshes each card's block, then strips a stale block from any card that
     stopped running (non-terminal statuses only — see Safety),
   - per-card failures are isolated; one bad card never stops the sweep.

3. **Read path** (`todo_reader.py`) — the card's newest `owner_admitted`
   `task_events` row carries `{db, session_id}`. The newest visible `todo_list`
   result is read from that worker `state.db` with `mode=ro` (never
   `immutable=1`: it ignores the WAL and misses a live writer's newest rows).
   Re-resolved every cycle, because a retry/reclaim mints a new `session_id`.

4. **Mapping** (`todo_progress.py`) — `N` = `completed` items, `M` = items
   excluding `cancelled`, `percent = round(100*N/M)`. Label = first
   `in_progress` item, else first `pending`, else the last `completed`. Parsing
   is strict and total: malformed JSON or malformed rows are dropped, never
   raised.

5. **Format** (`progress_block.py`) — the block is identified by its exact shape
   (`▶ N/M · label` then `[` + 8–40 of `#`/`=`/`_`/`.` + `]` + NBSP + `NN%`), so
   it needs no sentinel and cannot be confused with prose. `apply_block`/
   `strip_block` are idempotent and lossless: human text after the block survives
   verbatim.

6. **Write path** (`card_write.py`) — `UPDATE tasks SET body=? WHERE id=? AND
   body=?` inside one `BEGIN IMMEDIATE`, retried on a CAS miss, so a concurrent
   human edit is never clobbered. The same transaction appends a `progress`
   `task_events` row; the desktop board and dashboard drawer invalidate the board
   for **any** event kind carrying a `task_id`, which is what makes the card
   update live. Nothing is written when the new body equals the current one, so
   there is no event spam.

## Migration (v2 / v3 / v3.1 → v3.2)

Blocks already written on live cards use an **older shape**: a 24-wide bar of
`#`/`=` and a label ≤ 19 chars (v2), a 23-wide bar of `#`/`_` (the v3 first
cut), or a 23-wide `#.` bar with **no** trailing pad (v3.1). The v3.2
`strip_block`/`extract_block` accept **all three alphabets** (`#` + `=`, `#` +
`_`, `#` + `.`) and any bar width in **8–40**, and treat the trailing NBSP pad
as **optional but exact**: the legacy shape with no pad is recognised, and the
v3.2 shape is recognised only at the exact pad length the rule derives, so at the
first sweep an old block is **replaced in place** — never stacked. A hand-written
lookalike whose bar uses another character (e.g. `-`, which would also break
across the wrap), whose line lacks the NBSP+`%` terminator, or whose line merely
ends in a *different* number of NBSPs is deliberately left untouched. Second and
later sweeps are no-ops (0 writes) once the block matches.

## Configuration

Settings live under `plugins.entries.kanban-card-progress.settings` in
`config.yaml` — that `settings` subtree is the exact path the code reads
(`__init__._config`, and the canonical writer `save_plugin_setting`):

| key | default | meaning |
|---|---|---|
| `enabled` | `true` | Write the progress block. The plugin is off until it is added to `plugins.enabled`; this switch then turns the sweep off again without unloading it. |
| `interval_seconds` | `30` | Seconds between sweeps (floor 5). |
| `max_cards_per_cycle` | `40` | Upper bound on cards touched per sweep. |
| `sqlite_timeout_seconds` | `5` | Busy timeout for each SQLite read/write. |
| `cycle_budget_seconds` | `20` | Wall-clock budget for one sweep. |
| `max_body_bytes` | `262144` | Cards with a larger body are skipped, never truncated. |
| `debug` | `false` | Append sweep decisions to `debug.log` next to the plugin. |

Example: `plugins.entries.kanban-card-progress.settings.enabled: false` stops the
sweep without unloading the plugin.

## How to enable / disable

Disabled by default: a user plugin loads only when its key is in the
`plugins.enabled` allow-list, so the manifest is absent from that list. (This is
the convention `task-retro` also follows; `kanban-guards` has no `config_schema`
at all, so it is not part of that convention.) To activate:

```
hermes plugins enable kanban-card-progress
```

(or add `kanban-card-progress` to `plugins.enabled` in `config.yaml`) and restart
Hermes. To turn it off again: `hermes plugins disable kanban-card-progress` (or
remove it from the list) and restart. Once the plugin is loaded, setting
`plugins.entries.kanban-card-progress.settings.enabled: false` stops the sweep
without unloading the plugin.

## Safety

- **Never writes to a `done` or `archived` card.** The CAS `UPDATE` itself
  carries `AND status = 'running'`, so a card that completes between the status
  read and the write is a no-op (no body change, no event). Cleanup likewise
  refuses terminal cards and never strips a live block.
- **Only running cards are refreshed.** `todo`/`ready`/`blocked`/`review` cards
  are left alone.
- **Human text is never clobbered.** The CAS write and the shape-anchored strip
  only ever replace the leading block; the description after it is preserved
  verbatim.
- **Body size is capped** (`max_body_bytes`); an oversized card is skipped, never
  truncated.
- **Malformed Tasklists never raise** — the sweep writes nothing and leaves any
  existing block untouched. A card with no Tasklist yet, or with `M == 0`, is
  also left untouched.
- **The timer can never crash the gateway** — the loop body is fully wrapped and
  logs at WARNING.

## Known caveat

The desktop card preview is `latest_summary || body`. Once an earlier run has
written a `summary` (the block/review/retry cases), the card preview shows the
summary and the body block is **shadowed on the card** — it remains in the body
and is still visible in the drawer. This is stated, not fixed: refreshing the
summary is a product decision outside this plugin's scope.

## Tests

`tests/` holds the stdlib-unittest suite, run with the Hermes venv python:

```
~/.hermes/hermes-agent/.venv/bin/python3 tests/run_tests.py -v
```

They cover the format module (width table, budget derivation, the `.` alphabet,
the label cut, migration from all three old alphabets), the README/docstring
budget consistency (`TestDocsMatchConstants` — a constant change can never ship
with stale prose), todo parsing, the mapping edge cases, the read path against a
synthetic WAL-mode `state.db` with a live writer (proving `mode=ro` sees the fresh
row where `immutable=1` does not), the CAS race, the timer loop (single instance,
exception survival, clean stop), idempotence and non-running cleanup. Every
board/worker-DB test runs in an isolated `HERMES_HOME` / `HERMES_KANBAN_HOME` with
an assertion that the resolved board DB is under the temp root — never the real
board.
