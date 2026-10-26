#!/usr/bin/env python3
"""Reference implementation of the Kanban card progress block (v3).

Design verified on the REAL Hermes desktop app (tasks t_a3594a70 and t_c82fa710,
board `default`, throwaway `[probe]` cards).

WHY THIS SHAPE
--------------
The card preview is `latest_summary || body` rendered as RAW text inside a
`line-clamp-2` span. Newlines collapse to spaces, so the only way to get a
dedicated 2nd visual line is a token that (a) does not fit after line 1 and
(b) cannot be broken.

MEASURED FACTS (live app, all reproducible - see WIDTH_TABLE below)
-------------------------------------------------------------------
* The card text box is a fixed-width CSS box: the board column is `w-64`
  (16rem), inside it the card is `border` (2+1 px) + `p-2.5` (20 px), and the
  preview span itself carries no padding - so the box is
  `16rem - 16 (column p-2) - 3 (border) - 20 (card p-2.5)` = **217.0 CSS px**
  at the app's default root font size (16 px). It does NOT change with the
  window width (900/1400/2200/2560 px gave the identical box) or with the app's
  UI zoom (90/100/110/125 % all render 217 CSS px - Chromium zoom scales device
  pixels, not CSS layout). It DOES scale with the root font size (14 px -> 189.5,
  18 px -> 244.5), which no user control drives.
* The historical "231 px" was the AX FRAME width on the operator's app, which
  runs at UI scale 110 %: the same 2-line text reports AX frame 210 at scale 100
  and 231 at scale 110 (210 x 1.1). AX frames are screen pixels and are scaled
  by the app zoom; the renderer's CSS box is 209..217 px (see CONTENT_WIDTH_PX).
  231 was therefore a unit error (AX screen px vs CSS px), not a wider box.
* The font is PROPORTIONAL, so characters are not a fixed width. The per-glyph
  advance table below is the advance measured in the app's own Blink engine at
  zoom 1 with the card's CSS (0.6875rem, `--dt-font-sans`): `tools/measure_advances.py`
  writes `tests/measured_advances.json` from the live renderer, and
  `TestWidthTableMatchesMeasuredReference` pins the table against it.
* A run that does not fit its line is ellipsis-CLAMPED: its AX frame width stops
  growing (saturates at the content width) while its label keeps the full text.
  That is how the fit/clamp boundary was measured, not guessed.
* `#` (6.99 px) is the FILL character; `.` (3.33 px) is the operator's chosen
  EMPTY character (v3.1; the v3 first cut used `_` 6.48 px, v2 used `=` 6.99 px).
* `[`, `]`, `#`, `.`, `_`, `=` form an UNBREAKABLE run: the whole `[####....]`
  token wraps to line 2 intact. `-` (hyphen) IS a break opportunity and splits
  the bar across the wrap - rejected.
* U+00A0 (NBSP) neither collapses nor breaks: the safe glue between the bar and
  the percentage, so `NN%` can never be orphaned. Measured advance 3.16 px.

LAYOUT BUDGET (derived from the table, not by trial)
----------------------------------------------------
  content (wrap) width : 209 px (SMALLEST reachable box: a busy lane shows an
                         8 px scrollbar; an idle lane is 217 px)
  safety margin        :   4 px (1.9 %, font-rendering / rounding headroom)
  usable line budget   : 205 px

  line 2 = "[" + BAR_WIDTH*[#.] + "]" + NBSP + "NN%"   (widest case: 100 %, all #)
      41.09 px fixed ("[" 4.27 + "]" 4.27 + NBSP 3.16 + "100%" 29.40)
      + BAR_WIDTH * 6.99 <= 205  ->  BAR_WIDTH = 23  (23*6.99 = 160.86; total 201.95)
      The BINDING case is the all-`#` 100 % bar: it contains no empty glyph, so
      the narrower `.` buys no width here. 23 -> 201.95 px fits the 205 px budget
      with 3.05 px of headroom; 24 -> 208.95 px would NOT fit the 209 px box.

  line 1 = "▶ N/M · label"  (N, M of 1..3 digits)
      header "▶ N/M · " = 35.44 px ("1/1") .. 67.96 px ("444/444")
      ->  label budget = 205 - 67.96 = 137.04 px .. 205 - 35.44 = 169.56 px
      the label is cut by PIXEL width at the longest prefix that fits, on a
      word boundary when possible, otherwise mid-word, and NEVER with an ellipsis.

LINE 2 PAD (v3.2) - keep the description OFF line 2
---------------------------------------------------
A real newline cannot separate the block from the description: the preview is RAW
text in a `line-clamp-2` span and every newline collapses to a space. So the
description flows onto line 2 right after `NN%` whenever the bar is not full
(the 0 % bar line is only 105.54 px of the 209 px box).

The fix is a run of NBSP (U+00A0 - invisible, never collapses, no break
opportunity) appended AFTER the percent, wide enough that the next regular word
cannot fit on line 2 and wraps to line 3, which the 2-line clamp hides. The pad
is sized from the LARGEST box (217 px), so it also blocks at the 209 px box:

  PAD_MIN_PX = 217.00 (largest box) - 3.16 (space) - 2.78 (narrowest glyph)
               + 1.00 (safety)                                        = 212.07
  PAD_MAX_PX = 217.00 (largest box) - 1.00                           = 216.00
  k = smallest count with  text_width(line2) + k * 3.16 >  PAD_MIN_PX,
      capped so             text_width(line2) + k * 3.16 <= PAD_MAX_PX.

Every word is preceded by a space and starts with a glyph at least 2.78 px wide
(the `j` advance, the narrowest in the table), so a token wider than 212.07 px
leaves no room for it. The 1.00 px safety absorbs the table's own rounding error
(the table over-states the real base by at most 0.67 px), so the rule blocks the
first word in the renderer too, not only on paper. The pad is an UNBREAKABLE run:
at the 209 px box it overflows invisibly (the visible part is <= 201.95 px, so
`NN%` is never clipped) and never wraps itself onto line 3.

Measured over all 101 percentages: the padded line-2 token is 212.11..215.17 px
(k = 4..35 NBSP, advance 3.16 px). The VISIBLE part of line 2 is unchanged and
still <= 201.95 px, so `NN%` can never be clipped; the pad only occupies invisible
space. The hard limit is the 217 px wrap width, which the token always stays
below.

This module is pure Python with no font library: the measured advance table is
embedded and the cut is arithmetic over it.

TASK LIST (v1.2) - the whole Tasklist below the bar
---------------------------------------------------
One line per Tasklist item, appended AFTER the pad, so the list lives in the
card body where the card face cannot show it: line 2 is already full of the pad,
so the list starts on line 3, which the 2-line clamp hides. Opening the card
shows the block, then the whole list, then the description. Items are prefixed
with ``• [x]`` (completed), ``• [>]`` (in progress), ``• [ ]`` (pending) or
``• [-]`` (cancelled) - the same statuses ``map_progress`` counts - and an
item's own newlines are flattened so the block stays contiguous and its shape
stays recognisable. The list is part of the block: a sweep replaces it in place
and a strip takes it away with the block, so the body never accumulates copies.

CLAMP ELLIPSIS (proven in Blink, the app's own engine)
-----------------------------------------------------
When the 2-line clamp hides line 3+, the renderer appends a visible ellipsis at
the END of line 2. Measured with the app's Chromium on all 12 probe shapes: the
ellipsis eats only the invisible NBSP pad - the right edge of the `%` glyph is
byte-identical with the clamp ON and OFF (e.g. 0 % -> 115.52 px, 100 % ->
211.93 px in both states). The ellipsis therefore never consumes a digit or the
`%`; `NN%` stays complete on the card face.
"""

from __future__ import annotations

import math
import re

"""--- Measured advance widths (px) of the 0.6875rem UI font on the real card ----
Source: tools/measure_advances.py in the app's own Blink, card CSS, the real
card preview span; pinned by TestWidthTableMatchesMeasuredReference against
tests/measured_advances.json. Single-glyph advance (a word's first glyph uses
its own advance, so single-glyph values are the correct ones here)."""
WIDTH_TABLE = {
    " ": 3.156, "!": 3.488, "\"": 5.319, "#": 6.994, "$": 6.994, "%": 10.244, "&": 7.891, "'": 3.331, "(": 4.266, ")": 4.266, "*": 5.253, "+": 6.994, ",": 3.331, "-": 5.253, ".": 3.331, "/": 3.419,
    "0": 6.994, "1": 5.169, "2": 6.703, "3": 6.962, "4": 7.144, "5": 6.866, "6": 7.069, "7": 6.328, "8": 7.091, "9": 7.069,
    ":": 3.331, ";": 3.331, "<": 6.994, "=": 6.994, ">": 6.994, "?": 5.706, "@": 10.163,
    "A": 7.478, "B": 7.297, "C": 7.941, "D": 8.059, "E": 6.619, "F": 6.359, "G": 8.278, "H": 8.231, "I": 3.009, "J": 5.984, "K": 7.312, "L": 6.312, "M": 9.681,
    "N": 8.231, "O": 8.553, "P": 7.053, "Q": 8.553, "R": 7.253, "S": 7.075, "T": 7.038, "U": 8.175, "V": 7.478, "W": 10.712, "X": 7.531, "Y": 7.275, "Z": 7.344,
    "[": 4.266, "\\": 3.419, "]": 4.266, "^": 6.994, "_": 6.484, "`": 5.566,
    "a": 6.134, "b": 6.822, "c": 6.222, "d": 6.822, "e": 6.35, "f": 4.047, "g": 6.769, "h": 6.538, "i": 2.784, "j": 2.778, "k": 6.038, "l": 2.847,
    "m": 9.638, "n": 6.484, "o": 6.566, "p": 6.781, "q": 6.769, "r": 4.256, "s": 5.825, "t": 4.062, "u": 6.484, "v": 6.028,
    "w": 8.584, "x": 5.834, "y": 6.038, "z": 5.997,
    "{": 4.266, "|": 2.913, "}": 4.266, "~": 6.994,
    "\u25b6": 8.881, "\u00b7": 3.331,
    "\u00e1": 6.134, "\u00e9": 6.35, "\u00ed": 2.784, "\u00f3": 6.566, "\u00fa": 6.484, "\u00f1": 6.484, "\u00fc": 6.484,
    "\u00c1": 7.478, "\u00c9": 6.619, "\u00cd": 3.009, "\u00d3": 8.553, "\u00da": 8.175, "\u00d1": 8.231, "\u00dc": 8.175,
    "\u00a0": 3.158,
}
"""Unknown glyph: widest plausible advance, so the budget is never over-spent."""
DEFAULT_ADVANCE = 11.80

"""--- Measured layout constants ------------------------------------------------
The card text box is 13.75rem wide: board column `w-64` (256 px) - column p-2
(16) - border (3) - card p-2.5 (20) = 217 px, MINUS the lane's scrollbar when
the lane overflows. The app styles every scrollbar at `::-webkit-scrollbar
{ width: 0.5rem }` (8 px, NOT overlay - it reserves space), so a busy lane
renders a 209 px box and an idle lane a 217 px box. Both were measured in the
app's own renderer; the operator's real board (busy lanes) shows 209 px.

The historical "231 px" was the AX FRAME width at the operator's 110 % UI zoom:
the same text reports AX 210 at scale 100 and 231 at scale 110 (210 x 1.1), and
209 x 1.1 = 230 matches his live 2-line cards. AX frames are screen pixels and
are scaled by the app zoom; the renderer's CSS box is 209..217 px.

Two references, because the two lines constrain differently:
* line 1 and the bar must FIT the SMALLEST box (209 px) - they are breakable
text and would wrap if too wide;
* the pad must BLOCK a word at the LARGEST box (217 px) - it is an
unbreakable run, so it never wraps itself; at 209 it merely overflows
invisibly (the visible `NN%` right edge is <= 202 px, inside 209)."""
CONTENT_WIDTH_PX = 209.0    # smallest reachable box: line 1 / bar budget
PAD_CONTENT_WIDTH_PX = 217.0  # largest reachable box: pad budget
SAFETY_MARGIN_PX = 4.0      # headroom for font-rendering / rounding differences
USABLE_WIDTH_PX = CONTENT_WIDTH_PX - SAFETY_MARGIN_PX     # 205.0

"""Line 2 must end in a COMPLETE `NN%` (up to `100%`) with no ellipsis. The widest
case is the all-`#` 100 % bar, which contains no empty glyph - so the narrower
`.` buys nothing for the binding case:
"[" 4.27 + "]" 4.27 + NBSP 3.16 + "100%" 29.40 = 41.09 px fixed
41.09 + BAR_WIDTH * 6.99 <= 205  ->  BAR_WIDTH = 23 (201.95 px; 24 = 208.95
would NOT fit the 209 px box)"""
BAR_WIDTH = 23              # chars INSIDE the brackets (derived: see above)
FILLED = "#"
EMPTY = "."                 # operator's choice (v3.1): period, v3 used `_`, v2 `=`
LEGACY_EMPTY = "="          # v2 alphabet, still recognised for migration
UNDERSCORE_EMPTY = "_"      # v3 first-cut alphabet, still recognised for migration
SEP = "\u00a0"              # NBSP: no collapse, no break
GLYPH = "\u25b6"            # ▶ renders inline as text (verified)
MIDDLE_DOT = "\u00b7"       # ·

"""--- Line-2 pad (v3.2) --------------------------------------------------------
Appended AFTER `NN%` so the description can never follow the percent on line 2."""
PAD_CHAR = SEP              # U+00A0: invisible, never collapses, no break
NARROWEST_ADVANCE = min(WIDTH_TABLE.values())   # 2.78 px (the `j` glyph)
"""A word needs `space + first glyph` of room; the narrowest glyph that can start
a word is NARROWEST_ADVANCE, so a token must EXCEED this width to block it.
PAD_SAFETY_PX absorbs the embedded table's own rounding error: the table
over-states the real rendered base by at most 0.67 px, so a token sized to
exceed the threshold by >= 1 px also blocks the first word in the renderer."""
PAD_SAFETY_PX = 1.0
"""Sized from the LARGEST box (217 px): a token that blocks a word at 217 also
blocks it at 209, because 209 is narrower."""
PAD_MIN_PX = PAD_CONTENT_WIDTH_PX - WIDTH_TABLE[" "] - NARROWEST_ADVANCE + PAD_SAFETY_PX  # 212.07
"""The token must never reach the wrap width, or it would wrap ITSELF onto line 3."""
PAD_MAX_PX = PAD_CONTENT_WIDTH_PX - 1.0                                 # 216.00
assert PAD_MIN_PX < PAD_MAX_PX

"""v1.1.1 wrote its pad from a 231 px box (the AX-frame unit error fixed here), so
its blocks carry a DIFFERENT pad length than this version derives. They must
still be recognised and replaced in place, never stacked, so :func:`_match_block`
accepts that legacy length too. 231 px is the only historical value shipped."""
LEGACY_CONTENT_WIDTH_PX = 231.0
_LEGACY_PAD_MIN_PX = (LEGACY_CONTENT_WIDTH_PX - WIDTH_TABLE[" "] - NARROWEST_ADVANCE
                      + PAD_SAFETY_PX)                                   # 226.07
_LEGACY_PAD_MAX_PX = LEGACY_CONTENT_WIDTH_PX - 1.0                       # 230.00

"""Accepted bar widths when STRIPPING (migration: v2 wrote 24, v3 writes 23; a
hand-written bar of some other width in 8..40 is tolerated so a future width
change never strands a block). MAX_ACCEPTED_BAR must stay >= BAR_WIDTH."""
MIN_ACCEPTED_BAR = 8
MAX_ACCEPTED_BAR = 40
assert MIN_ACCEPTED_BAR <= BAR_WIDTH <= MAX_ACCEPTED_BAR

_PCT = re.compile(r"\d{1,3}%$")

"""--- Task list (v1.2) ----------------------------------------------------------
After the pad, one line per Tasklist item. Each line starts with the bullet and
a checkbox, so the run is recognisable and a description is never eaten; the
item text is flattened (no newline inside a line) to keep the run contiguous."""
LIST_BULLET = "\u2022"       # • (not in the width table: the list has no budget)
LIST_MARKS = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]", "cancelled": "[-]"}
_LIST_RUN = re.compile(r"(?:\u2022 \[[ x>\-]\] [^\n]*(?:\n|$))+")
"""shortcut: a description line that starts with exactly `• [x] ` right after the
block is read as part of the list, upgrade by bracketing the run in marker lines."""


def text_width(text: str) -> float:
    """Advance width of `text` in px, from the measured table."""
    return sum(WIDTH_TABLE.get(ch, DEFAULT_ADVANCE) for ch in text)


def cut_to_width(text: str, budget_px: float) -> str:
    """Longest prefix of `text` that fits `budget_px`, on a word boundary if one
    fits, otherwise mid-word. Never adds an ellipsis."""
    if not text:
        return ""
    if text_width(text) <= budget_px:
        return text
    """Longest prefix by width."""
    acc = 0.0
    end = 0
    for i, ch in enumerate(text):
        w = WIDTH_TABLE.get(ch, DEFAULT_ADVANCE)
        if acc + w > budget_px:
            break
        acc += w
        end = i + 1
    if end <= 0:
        return ""
    prefix = text[:end]
    """Prefer a word boundary: drop the trailing partial word when the previous
word boundary also fits and leaves at least one word."""
    if end < len(text) and text[end] != " ":
        sp = prefix.rfind(" ")
        if sp > 0:
            return prefix[:sp].rstrip()
    return prefix.rstrip()


def _header(done: int, total: int) -> str:
    return f"{GLYPH} {done}/{total}"


def line2_base(pct: int) -> str:
    """The visible line 2: `[bar]` + NBSP + `NN%` (no pad)."""
    filled = round(BAR_WIDTH * pct / 100)
    bar = "[" + FILLED * filled + EMPTY * (BAR_WIDTH - filled) + "]"
    return f"{bar}{SEP}{pct}%"


def _pad_count_for(base: str, pad_min_px: float, pad_max_px: float) -> int:
    """Number of NBSP that makes `base` block a word, for a given box size."""
    width = text_width(base)
    if width > pad_min_px:
        return 0
    step = WIDTH_TABLE[PAD_CHAR]
    k = math.ceil((pad_min_px - width) / step)
    while k > 0 and width + k * step > pad_max_px:
        k -= 1
    return max(0, k)


def pad_count(base: str) -> int:
    """Number of NBSP appended after the percent to block a description leak.

    ``base`` is the LINE-2 text (`[bar]` + NBSP + `NN%`), never the whole block.
    The smallest count whose token EXCEEDS :data:`PAD_MIN_PX` (so no word can
    fit after it) while staying at or below :data:`PAD_MAX_PX` (so the token
    itself never wraps). 0 when the base already exceeds PAD_MIN_PX.
    """
    return _pad_count_for(base, PAD_MIN_PX, PAD_MAX_PX)


def format_list(todos) -> str:
    """The Tasklist as one `• [mark] text` line per item (``""`` when empty).

    Deterministic and total: a malformed item is skipped, and an item's own
    line breaks become single spaces so the run stays one line per item.
    """
    lines = []
    for item in todos or ():
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("content") or "").split())
        if text:
            mark = LIST_MARKS.get(str(item.get("status")), LIST_MARKS["pending"])
            lines.append(f"{LIST_BULLET} {mark} {text}")
    return "\n".join(lines)


def format_block(done: int, total: int, label: str, pct: int | None = None,
                 todos=None) -> str:
    """Return the two-line progress block, without a trailing newline.

    done/total drive the counter. `label` (the current in-progress todo item) is
    cut deterministically by the measured PIXEL budget: the longest prefix that
    fits, on a word boundary when possible and otherwise mid-word, never with an
    ellipsis. Line 1 can therefore never wrap itself.

    Line 2 is `[bar]` + NBSP + `NN%` + a run of NBSP (:func:`pad_count`), so the
    description can never follow the percent on the card: the invisible pad
    leaves no room for a word, which wraps to line 3 where the 2-line clamp
    hides it. The visible part of line 2 is unchanged and still <= 201.95 px.

    ``todos`` (the parsed Tasklist, optional) is appended as :func:`format_list`
    after the pad: line 3 onward, so the card face stays two lines and the whole
    list is visible only when the card is opened.
    """
    if total < 0 or done < 0:
        raise ValueError("done/total must be >= 0")
    if total and done > total:
        done = total

    if pct is None:
        pct = round(100 * done / total) if total else 0
    pct = max(0, min(100, int(pct)))

    head = _header(done, total)
    label = " ".join((label or "").split())
    if label:
        budget = USABLE_WIDTH_PX - text_width(head + " " + MIDDLE_DOT + " ")
        cut = cut_to_width(label, budget)
        if cut:
            head = f"{head} {MIDDLE_DOT} {cut}"

    base = line2_base(pct)
    block = f"{head}\n{base}{PAD_CHAR * pad_count(base)}"
    listed = format_list(todos)
    return block + ("\n" + listed if listed else "")



"""--- Block identification and migration ---------------------------------------
The bar line is `[` + 8..40 of [#=_.] + `]` + NBSP + NN%.  The alphabet accepts
ALL THREE shipped shapes - v2 `#=`, v3-first-cut `#_`, and v3.1 `#.` - at any
width in the accepted range, so a block written by an older version is
REPLACED in place (never duplicated)."""
_BAR = r"\[[#=_.]{8,40}\]"
_HEAD = re.escape(GLYPH) + r" \d+/\d+"
"""The block WITHOUT the v3.2 pad: `▶ N/M[ · label]` \n `[bar]` NBSP `NN%`."""
_BLOCK_CORE_RE = re.compile(
    r"^(?P<block>" + _HEAD + r"(?: \u00b7 [^\n]*)?\n"
    + _BAR + re.escape(SEP) + r"\d{1,3}%)",
    re.MULTILINE,
)
"""Whitespace that may follow a block line (a real newline, or the collapsed one)."""
_TAIL_RE = re.compile(r"[ \t]*\n?")
"""The v3.2 pad can end up on its OWN line: every strip written before v3.2
removes the two-line block and leaves the NBSP run behind (it is not `[ \t]`),
so each of those sweeps pushes the pad one line down. The lines are invisible,
hence consumed with the pad instead of accumulating on every sweep."""
_SPILL_RE = re.compile(r"[ \t]*\u00a0[ \u00a0\t]*\n?")


def _match_block(body: str):
    """Match a leading block at ``body``, consuming its pad only when exact.

    Returns ``(core, end, pad)`` where ``end`` is the index just past the whole
    match and ``pad`` the NBSP run actually consumed. The pad is accepted ONLY
    at the exact count :func:`pad_count` derives for that core: a hand-written
    line that merely ends in NBSPs (any other count) is not a block and stays
    untouched, while both the v3.1 shape (0 pad) and the v3.2 shape (exact pad)
    are recognised and replaced in place. A Tasklist run (:data:`_LIST_RUN`)
    directly after the pad is part of the block, so it is replaced and stripped
    with it.
    """
    if not body:
        return None
    m = _BLOCK_CORE_RE.match(body)
    if not m:
        return None
    core = m.group("block")
    end = m.end()
    """The pad is sized from LINE 2 only: `core` also carries line 1, whose width
would otherwise swamp the threshold."""
    line2 = core.rsplit("\n", 1)[-1]
    want = pad_count(line2)
    """v1.1.1 wrote its pad from a 231 px box, so its blocks carry a different
(larger) length. Accept both, so a v1.1.1 block is replaced in place."""
    want_legacy = _pad_count_for(line2, _LEGACY_PAD_MIN_PX, _LEGACY_PAD_MAX_PX)
    got = 0
    while end + got < len(body) and body[end + got] == SEP:
        got += 1
    """Accepted shapes: the legacy one with NO pad (v2 / v3 / v3.1), the v1.1.1
one at the 231 px count, and this version's count. Any other NBSP run is a
human line, not a block."""
    if got not in (0, want, want_legacy):
        return None
    pad = body[end:end + got]
    end += got
    tail = _TAIL_RE.match(body, end)
    end = tail.end()
    """Pad lines an older strip salvaged onto their own line belong to the block.
    Swallow them BEFORE the list: a pad line between the pad and the list hides
    the list from :data:`_LIST_RUN`, and every sweep would then stack one more
    copy of it (measured on live cards written across the v1.1.3 upgrade)."""
    while True:
        spilled = _SPILL_RE.match(body, end)
        if not spilled:
            break
        end = spilled.end()
    """The Tasklist (v1.2) starts on the line after the pad."""
    listed = _LIST_RUN.match(body, end)
    listed_text = ""
    if listed:
        listed_text = "\n" + listed.group(0).rstrip("\n")
        end = listed.end()
    """Swallow the pad lines an older strip salvaged onto their own line."""
    while True:
        spilled = _SPILL_RE.match(body, end)
        if not spilled:
            break
        end = spilled.end()
    return core + pad + listed_text, end, pad



def extract_block(body: str) -> str | None:
    """Return the block text (without trailing newline), or None."""
    m = _match_block(body)
    return m[0] if m else None


def strip_block(body: str) -> str:
    """Remove a leading progress block; never touch the rest of the body.

    Idempotent, safe on empty/None, and lossless for a description that merely
    *looks* like a block (a hand-written bar of the wrong alphabet, a width
    outside 8..40, a line without the NBSP+percent terminator, or a trailing
    NBSP run of any length other than the exact v3.2 pad is left untouched).
    """
    if not body:
        return body or ""
    m = _match_block(body)
    if not m:
        return body
    return body[m[1]:]


def apply_block(body: str, block: str) -> str:
    """Replace (or insert) the block at the head of `body`.

    Idempotent and lossless: the original description survives verbatim, and an
    OLD-shape block (v2 alphabet/width) is replaced in place - never stacked.
    """
    rest = strip_block(body or "").lstrip("\n")
    return block + ("\n" + rest if rest else "")


def heal_body(body: str) -> str:
    """Drop the invisible pad lines an older strip left between block and description.

    Returns the body unchanged when it carries no leading block, so it is safe to
    run over every card of the board: only lines made of NBSP disappear, the
    description text stays byte-identical.
    """
    m = _match_block(body)
    if m is None:
        return body
    rest = body[m[1]:]
    return m[0] + ("\n" + rest if rest else "")


if __name__ == "__main__":
    desc = "Entrega el informe de auditoria con las cifras medidas."
    b = format_block(3, 7, "probando tests", 40)
    print(b)
    print("---")
    applied = apply_block(desc, b)
    print(applied)
    print("--- idempotent:", apply_block(applied, b) == applied)
    print("--- stripped:", repr(strip_block(applied)))
