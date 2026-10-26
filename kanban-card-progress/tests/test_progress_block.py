#!/usr/bin/env python3
"""Unit tests for progress_block v3.1 (stdlib unittest, no third-party deps).

The measured width table is embedded in the module; these tests assert the
layout budgets it derives, the `.` alphabet, and the three-alphabet migration
(v2 `#=`, v3-first-cut `#_`, v3.1 `#.`).
"""

import json
import re
import unittest
from pathlib import Path

import progress_block as pb

PLUGIN_DIR = Path(__file__).resolve().parent.parent


class TestWidthTable(unittest.TestCase):
    def test_table_covers_the_bar_alphabet(self):
        for ch in list("#_=.[ ]%") + list("0123456789") + ["\u25b6", "\u00b7"]:
            self.assertIn(ch, pb.WIDTH_TABLE, ch)

    def test_table_covers_accented_letters(self):
        for ch in "áéíóúñüÁÉÍÓÚÑÜ":
            self.assertIn(ch, pb.WIDTH_TABLE, ch)

    def test_period_is_narrower_than_underscore_and_equals(self):
        """The v3.1 empty char is the operator's choice. The measurement says `.`
(3.33 px) is much narrower than `_` (6.48) and `=` (6.99). It buys no
width for the binding all-`#` 100 % bar, which contains no empty glyph."""
        self.assertLess(pb.WIDTH_TABLE["."], pb.WIDTH_TABLE["_"])
        self.assertLess(pb.WIDTH_TABLE["."], pb.WIDTH_TABLE["="])
        self.assertAlmostEqual(pb.WIDTH_TABLE["."], 3.33, places=2)

    def test_measured_anchors(self):
        """Anchors from the Blink measurement at the card CSS, zoom 1
(tools/measure_advances.py); pinned in full by
TestWidthTableMatchesMeasuredReference."""
        self.assertAlmostEqual(pb.WIDTH_TABLE["i"], 2.78, places=2)
        self.assertAlmostEqual(pb.WIDTH_TABLE["x"], 5.83, places=2)
        self.assertAlmostEqual(pb.WIDTH_TABLE["W"], 10.71, places=2)
        self.assertAlmostEqual(pb.WIDTH_TABLE["#"], 6.99, places=2)
        self.assertAlmostEqual(pb.WIDTH_TABLE["%"], 10.24, places=2)
        self.assertAlmostEqual(pb.WIDTH_TABLE["\u25b6"], 8.88, places=2)

    def test_unknown_glyph_uses_the_widest_advance(self):
        self.assertEqual(pb.text_width("\u4e2d"), pb.DEFAULT_ADVANCE)
        self.assertGreaterEqual(pb.DEFAULT_ADVANCE, max(pb.WIDTH_TABLE.values()))


class TestWidthTableMatchesMeasuredReference(unittest.TestCase):
    """The embedded table must equal the checked-in measured reference.

    The pad is sized from this table, so an inflated table silently re-opens the
    description leak while every self-consistent test stays green (the F1 review
    round). The reference is written by tools/measure_advances.py from the app's
    own Blink via CDP Range.getClientRects on the real card preview span; a table
    edit that is not re-measured fails here.
    """

    def test_every_reference_glyph_matches_within_tolerance(self):
        ref = json.loads((PLUGIN_DIR / "tests" / "measured_advances.json").read_text("utf-8"))
        tol = ref["TOL_PX"]
        self.assertEqual(ref["content_box_px"], pb.CONTENT_WIDTH_PX)
        missing = []
        for ch, want in ref["advances"].items():
            if ch not in pb.WIDTH_TABLE:
                missing.append(ch)
                continue
            self.assertAlmostEqual(pb.WIDTH_TABLE[ch], want, delta=tol,
                                   msg=f"{ch!r}: table {pb.WIDTH_TABLE[ch]} vs measured {want}")
        self.assertEqual(missing, [], f"table lacks measured glyphs: {missing}")

    def test_reference_covers_the_bar_and_percent_alphabet(self):
        ref = json.loads((PLUGIN_DIR / "tests" / "measured_advances.json").read_text("utf-8"))
        for ch in list("#=_.%[] \u00a0\u25b6\u00b7") + list("0123456789"):
            self.assertIn(ch, ref["advances"], ch)

    def test_reference_is_a_real_measurement_not_a_copy_of_the_table(self):
        """The earlier card shipped a reference byte-identical to WIDTH_TABLE, so it
pinned the table against itself and could not catch an inflated table.
A real per-glyph measurement is rounded from Blink layout and is NOT the
table verbatim; require a real minority of exact coincidences."""
        ref = json.loads((PLUGIN_DIR / "tests" / "measured_advances.json").read_text("utf-8"))
        exact = sum(1 for ch, v in ref["advances"].items()
                    if ch in pb.WIDTH_TABLE and pb.WIDTH_TABLE[ch] == v)
        self.assertLess(exact, len(ref["advances"]) // 2,
                        "the reference looks like a copy of WIDTH_TABLE, not a measurement")
        prov = ref["provenance"]
        for key in ("method", "surface", "measured_at", "box_w_css_px"):
            self.assertIn(key, prov, key)

    def test_content_box_constant_matches_the_measured_box_with_provenance(self):
        """CONTENT_WIDTH_PX is the number the whole layout derives from; it must be
the box the reference measured, with its provenance on file."""
        ref = json.loads((PLUGIN_DIR / "tests" / "measured_advances.json").read_text("utf-8"))
        self.assertEqual(pb.CONTENT_WIDTH_PX, ref["content_box_px"])
        self.assertEqual(pb.USABLE_WIDTH_PX, ref["content_box_px"] - pb.SAFETY_MARGIN_PX)
        prov = ref["provenance"]
        self.assertIn("Range.getClientRects", prov["method"])
        self.assertIn("card preview span", prov["surface"])
        self.assertRegex(prov["measured_at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_the_231_box_defect_would_fail_this_suite(self):
        """Restoring the old 231 px box must break the reference pin, so the exact
defect this card fixes cannot ship green again."""
        ref = json.loads((PLUGIN_DIR / "tests" / "measured_advances.json").read_text("utf-8"))
        self.assertNotEqual(ref["content_box_px"], 231.0)
        self.assertEqual(ref["content_box_px"], 209.0)
        self.assertEqual(pb.CONTENT_WIDTH_PX, ref["content_box_px"])
        self.assertEqual(ref["busy_lane_box_px"], 209.0)
        self.assertEqual(ref["idle_lane_box_px"], 217.0)

    def test_an_inflated_glyph_would_fail_this_suite(self):
        """Restoring the old inflated table must break the reference pin."""
        ref = json.loads((PLUGIN_DIR / "tests" / "measured_advances.json").read_text("utf-8"))
        inflated = dict(pb.WIDTH_TABLE)
        inflated["#"] = pb.WIDTH_TABLE["#"] + 0.8      # a real past failure mode
        mismatch = [ch for ch, v in ref["advances"].items()
                    if ch in inflated and abs(inflated[ch] - v) > ref["TOL_PX"]]
        self.assertIn("#", mismatch)


class TestBudget(unittest.TestCase):
    """The budgets are derived from the table, so they must stay consistent."""

    def test_bar_width_derived_from_the_budget(self):
        worst = "[" + pb.FILLED * pb.BAR_WIDTH + "]" + pb.SEP + "100%"
        self.assertLessEqual(pb.text_width(worst), pb.USABLE_WIDTH_PX)
        """BAR_WIDTH must leave real headroom under the smallest box's budget, so
the visible bar never reaches the wrap/clamp zone (>= 1 px)."""
        self.assertGreaterEqual(pb.USABLE_WIDTH_PX - pb.text_width(worst), 1.0)
        """The largest width that would still fit the SMALLEST box's budget is
exactly BAR_WIDTH: one more char would overflow the 209 px box."""
        w = pb.BAR_WIDTH
        while pb.text_width("[" + pb.FILLED * (w + 1) + "]" + pb.SEP + "100%") <= pb.USABLE_WIDTH_PX:
            w += 1
        self.assertEqual(w, pb.BAR_WIDTH)

    def test_limiting_case_is_the_all_hash_100_bar(self):
        """The all-`.` 0 % bar is far narrower than the all-`#` 100 % bar, so the
empty glyph's width does not set BAR_WIDTH."""
        all_dot = "[" + pb.EMPTY * pb.BAR_WIDTH + "]" + pb.SEP + "0%"
        all_hash = "[" + pb.FILLED * pb.BAR_WIDTH + "]" + pb.SEP + "100%"
        self.assertLess(pb.text_width(all_dot), pb.text_width(all_hash))
        self.assertLessEqual(pb.text_width(all_hash), pb.USABLE_WIDTH_PX)

    def test_usable_width_leaves_the_margin(self):
        self.assertEqual(pb.USABLE_WIDTH_PX,
                         pb.CONTENT_WIDTH_PX - pb.SAFETY_MARGIN_PX)
        self.assertGreater(pb.SAFETY_MARGIN_PX, 0)

    def test_line1_fits_for_every_digit_count(self):
        for done, total in ((0, 0), (1, 2), (12, 34), (123, 123), (999, 1000)):
            for label in ("x", "a b c d e f g h i j k l m n o p q r s t u v w",
                          "verificar la latencia del endpoint desplegado"):
                l1 = pb.format_block(done, total, label, 50).split("\n")[0]
                self.assertLessEqual(pb.text_width(l1), pb.USABLE_WIDTH_PX, l1)


class TestDocsMatchConstants(unittest.TestCase):
    """The prose budgets must equal the module constants (enforcement ladder).

    A stale docstring or README silently re-derives the wrong BAR_WIDTH - which
    is exactly the v2 defect. These tests fail the moment the two drift apart.
    """

    def _read(self, rel):
        return (PLUGIN_DIR / rel).read_text(encoding="utf-8")

    def test_docstring_states_the_implemented_constants(self):
        src = self._read("progress_block.py")
        doc = src.split('"""', 2)[1]
        self.assertIn(f"{pb.CONTENT_WIDTH_PX:.0f} px", doc)
        self.assertIn(f"{pb.USABLE_WIDTH_PX:.0f} px", doc)
        self.assertIn(f"BAR_WIDTH = {pb.BAR_WIDTH}", doc)
        """The LAYOUT BUDGET block is the number source a reader recomputes from;
the old wrong budget must not survive anywhere in it."""
        budget = doc.split("LAYOUT BUDGET")[1]
        for stale in ("247 px", "BAR_WIDTH = 26", "45.42 px", "26*7.69"):
            self.assertNotIn(stale, budget, stale)

    def test_docstring_states_the_implemented_pad_constants(self):
        """The v3.2 pad rule is the number source for the leak fix; its docstring
figures must be recomputed from the module, not typed (same drift class)."""
        doc = self._read("progress_block.py").split('"""', 2)[1]
        pad = doc.split("LINE 2 PAD")[1]
        self.assertIn(f"PAD_MIN_PX = {pb.PAD_CONTENT_WIDTH_PX:.2f}", pad)
        self.assertIn(f"{pb.PAD_MIN_PX:.2f}", pad)
        self.assertIn(f"PAD_MAX_PX = {pb.PAD_CONTENT_WIDTH_PX:.2f}", pad)
        self.assertIn(f"{pb.PAD_MAX_PX:.2f}", pad)
        self.assertIn(f"{pb.WIDTH_TABLE[pb.PAD_CHAR]:.2f}", pad)
        self.assertIn(f"{pb.NARROWEST_ADVANCE:.2f}", pad)
        lo, hi = self._pad_token_range()
        self.assertIn(f"{lo:.2f}..{hi:.2f} px", pad)

    def _pad_token_range(self):
        lo, hi = None, None
        for pct in range(101):
            tok = pb.text_width(pb.format_block(1, 3, "x", pct).split("\n")[1])
            lo = tok if lo is None else min(lo, tok)
            hi = tok if hi is None else max(hi, tok)
        return lo, hi

    def test_readme_states_the_implemented_constants(self):
        readme = self._read("README.md")
        self.assertIn(f"**{pb.CONTENT_WIDTH_PX:.0f} px**", readme)
        self.assertIn(f"**{pb.USABLE_WIDTH_PX:.0f} px**", readme)
        self.assertIn(f"`BAR_WIDTH = {pb.BAR_WIDTH}`", readme)
        self.assertIn(self._char_range(), readme)
        for stale in ("24\u201338", "BAR_WIDTH = 26", "45.42 px fixed",
                      "45.42 / 32.42", "24\u201328", "43.02 px", "183.98 px",
                      "157.44 px", "2.73 px"):
            self.assertNotIn(stale, readme, stale)

    def _header_budget_range(self):
        """(min, max) label budget px over every plausible `▶ N/M · ` header."""
        lo = hi = None
        for d in range(1000):
            for t in range(1, 1000):
                if d > t:
                    continue
                w = pb.text_width(f"\u25b6 {d}/{t} \u00b7 ")
                budget = pb.USABLE_WIDTH_PX - w
                lo = budget if lo is None else min(lo, budget)
                hi = budget if hi is None else max(hi, budget)
        return lo, hi

    def _char_range(self):
        """The README's rough lowercase-prose range, recomputed from the table."""
        avg = sum(pb.WIDTH_TABLE[c] for c in "abcdefghijklmnopqrstuvwxyz") / 26
        lo, hi = self._header_budget_range()
        return f"{int(lo / avg)}\u2013{-(-int(hi / avg * 100) // 100)}"

    def test_readme_states_the_recomputed_label_budget(self):
        """The header budget and its character range must be the ones the module
derives, not copied forward (the R3 review round failed on exactly this)."""
        readme = self._read("README.md")
        lo, hi = self._header_budget_range()
        self.assertIn(f"{lo:.2f} px", readme)
        self.assertIn(f"{hi:.2f} px", readme)
        for stale in ("69.56 px (3-digit)", "43.02 px"):
            self.assertNotIn(stale, readme, stale)


    def test_readme_states_the_pad_rule(self):
        """README must carry the recomputed pad figures, so a changed pad rule
cannot ship with stale prose."""
        readme = self._read("README.md")
        self.assertIn(f"**{pb.PAD_MIN_PX:.2f} px**", readme)
        self.assertIn(f"**{pb.PAD_MAX_PX:.2f} px**", readme)
        self.assertIn(f"{pb.WIDTH_TABLE[pb.PAD_CHAR]:.2f} px", readme)
        lo, hi = self._pad_token_range()
        self.assertIn(f"{lo:.2f} px", readme)
        self.assertIn(f"{hi:.2f} px", readme)
        self.assertIn("U+00A0", readme)

    def test_readme_bar_line_length_is_recomputed(self):
        readme = self._read("README.md")
        line = "[" + "#" * pb.BAR_WIDTH + "]" + pb.SEP + "100%"
        m = re.search(r"bar line is \*\*(\d+)\*\* chars", readme)
        self.assertIsNotNone(m, "README must state the bar line char count")
        self.assertEqual(int(m.group(1)), len(line))
        """The all-`.` 0 % bar px must be recomputed, not typed (same drift class)."""
        zero = "[" + "." * pb.BAR_WIDTH + "]" + pb.SEP + "0%"
        self.assertIn(f"{pb.text_width(zero):.2f} px", readme)

    def test_readme_label_example_px_is_recomputed(self):
        """The README's worked example must quote the px its own rule produces."""
        readme = self._read("README.md")
        cut = pb.format_block(12, 34, "verificar la latencia del endpoint").split("\n")[0]
        """The full phrase is wider than the 217 px box, so the cut drops the last
word; the README states both the full width and the cut's width."""
        self.assertEqual(cut, "\u25b6 12/34 \u00b7 verificar la latencia del")
        self.assertIn(f"= {pb.text_width(cut):.2f} px", readme)
        self.assertLessEqual(pb.text_width(cut), pb.USABLE_WIDTH_PX)
        full = "\u25b6 12/34 \u00b7 verificar la latencia del endpoint"
        self.assertIn(f"{pb.text_width(full):.2f} px", readme)

    def _max_w_run(self, done, total):
        n = 0
        while pb.text_width(f"\u25b6 {done}/{total} \u00b7 " + "W" * (n + 1)) <= pb.USABLE_WIDTH_PX:
            n += 1
        return n

    def _findings_path(self):
        """FINDINGS-v3.md ships next to the workspace root, not inside the
tarball; search a few ancestors so the guard also runs in dev."""
        for base in (PLUGIN_DIR.parent, PLUGIN_DIR.parent.parent):
            p = base / "FINDINGS-v3.md"
            if p.exists():
                return p
        return None

    def test_w_heavy_range_is_recomputed(self):
        """The `W`-heavy figure must be derived from the module, not typed."""
        runs = (self._max_w_run(0, 7), self._max_w_run(12, 34), self._max_w_run(123, 123))
        rng = f"~{min(runs)}\u2013{max(runs)} chars"
        targets = [("README.md", PLUGIN_DIR / "README.md")]
        findings = self._findings_path()
        if findings:
            targets.append(("FINDINGS-v3.md", findings))
        for rel, path in targets:
            text = path.read_text(encoding="utf-8")
            self.assertIn(rng, text, f"{rel} must state the recomputed {rng}")
            for stale in ("13\u201316", "13-16"):
                self.assertNotIn(stale, text, f"{rel}: stale {stale}")

    def test_plugin_yaml_names_the_current_empty_char(self):
        """A prose alphabet that names a superseded empty char re-derives the
wrong block shape (same class as R1)."""
        yaml = self._read("plugin.yaml")
        self.assertIn(f"the current #{pb.EMPTY} alphabet", yaml)
        for stale in ("the v3 #_ and the legacy #=",):
            self.assertNotIn(stale, yaml, stale)

    def test_plugin_yaml_states_the_measured_box_numbers(self):
        """The manifest description is the user-facing prose number source; it must
quote the two reachable boxes the module uses, recomputed not typed."""
        yaml = self._read("plugin.yaml")
        self.assertIn(f"{pb.CONTENT_WIDTH_PX:.0f} px smallest card box", yaml)
        self.assertIn(f"{pb.PAD_CONTENT_WIDTH_PX:.0f} px largest box", yaml)
        self.assertIn(f"{pb.SAFETY_MARGIN_PX:.0f} px margin", yaml)

    def test_findings_differential_is_current(self):
        """The v2-module differential cannot be recomputed offline (the v2 module
is not in the frozen tarball); forbid the stale count and require the
current one, so a copied-forward number fails the suite."""
        findings = self._findings_path()
        if findings is None:
            self.skipTest("FINDINGS-v3.md is not present next to the plugin")
        text = findings.read_text(encoding="utf-8")
        self.assertIn("33 fail/error", text)
        self.assertNotIn("22 fail/error", text)
        self.assertNotIn("31 fail/error", text)


class TestLine2Pad(unittest.TestCase):
    """v3.2: the pad after `NN%` must block every word from line 2."""

    def _line2(self, pct):
        return pb.format_block(1, 3, "x", pct).split("\n")[1]

    def test_every_percent_gets_a_pad(self):
        for pct in range(101):
            l2 = self._line2(pct)
            self.assertTrue(l2.endswith(pb.PAD_CHAR), pct)
            self.assertGreater(pb.pad_count(l2.rstrip(pb.PAD_CHAR)), 0, pct)

    def test_pad_token_exceeds_the_leak_threshold(self):
        """A word is `space + glyph`; the narrowest glyph is NARROWEST_ADVANCE.
The threshold carries a safety so the table's rounding error cannot
leave room for a real first word (see PAD_SAFETY_PX). The pad is sized
from the LARGEST box, so it also blocks at the 209 px smallest box."""
        need = (pb.PAD_CONTENT_WIDTH_PX - pb.WIDTH_TABLE[" "] - pb.NARROWEST_ADVANCE
                + pb.PAD_SAFETY_PX)
        self.assertEqual(pb.PAD_MIN_PX, need)
        for pct in range(101):
            tok = pb.text_width(self._line2(pct))
            self.assertGreater(tok, pb.PAD_MIN_PX, pct)

    def test_pad_token_never_reaches_the_wrap_width(self):
        """The pad is sized from the largest box; it must stay under that wrap
width (an unbreakable run would wrap itself onto line 3 otherwise)."""
        for pct in range(101):
            tok = pb.text_width(self._line2(pct))
            self.assertLess(tok, pb.PAD_CONTENT_WIDTH_PX, pct)

    def test_no_narrower_regular_glyph_can_follow_the_percent(self):
        """Exhaustive: for every percent and every glyph that can start a word,
`space + glyph` must NOT fit after the padded token, even at the LARGEST
box (the pad is sized from it, so the smaller box is stricter)."""
        starters = [c for c in pb.WIDTH_TABLE if c.isalnum()]
        self.assertTrue(starters)
        for pct in range(101):
            tok = pb.text_width(self._line2(pct))
            for c in starters:
                room = pb.PAD_CONTENT_WIDTH_PX - tok - pb.WIDTH_TABLE[" "]
                self.assertLess(room, pb.WIDTH_TABLE[c], (pct, c))

    def test_visible_line2_is_unchanged_and_never_clipped(self):
        for pct in range(101):
            base = pb.line2_base(pct)
            self.assertLessEqual(pb.text_width(base), pb.USABLE_WIDTH_PX, pct)
            self.assertTrue(base.endswith(f"{pct}%"), pct)
            self.assertEqual(self._line2(pct)[:len(base)], base, pct)

    def test_pad_uses_only_nbsp_and_is_invisible(self):
        for pct in range(101):
            l2 = self._line2(pct)
            self.assertEqual(set(l2[len(pb.line2_base(pct)):]), {pb.PAD_CHAR})
            self.assertEqual(pb.PAD_CHAR, "\u00a0")

    def test_pad_is_minimal(self):
        """Removing one pad char must break the block (otherwise it is not minimal)."""
        for pct in range(101):
            l2 = self._line2(pct)
            base = pb.line2_base(pct)
            self.assertEqual(len(l2) - len(base), pb.pad_count(base), pct)
            shorter = base + pb.PAD_CHAR * (pb.pad_count(base) - 1)
            self.assertLessEqual(pb.text_width(shorter), pb.PAD_MIN_PX, pct)

    def test_pad_count_zero_when_base_already_blocks(self):
        """A hypothetical wider bar leaves no room; pad_count must then be 0."""
        long_base = "[" + "#" * pb.BAR_WIDTH + "]" + pb.SEP + "100%" + pb.PAD_CHAR * 12
        self.assertGreater(pb.text_width(long_base), pb.PAD_MIN_PX)
        self.assertEqual(pb.pad_count(long_base), 0)

    def test_pad_never_exceeds_the_hard_cap(self):
        for pct in range(101):
            base = pb.line2_base(pct)
            k = pb.pad_count(base)
            self.assertLessEqual(pb.text_width(base) + k * pb.WIDTH_TABLE[pb.PAD_CHAR],
                                 pb.PAD_MAX_PX, pct)

class TestLine2PadMigration(unittest.TestCase):
    """The pad is part of the block shape, and old shapes must still migrate."""

    DESC = "Entrega el informe de auditoria con las cifras medidas."

    def test_v31_block_without_pad_is_recognised(self):
        old = pb.line2_base(50)
        body = "\u25b6 1/3 \u00b7 x\n" + old + "\n" + self.DESC
        self.assertEqual(pb.strip_block(body), self.DESC)
        self.assertEqual(pb.extract_block(body), "\u25b6 1/3 \u00b7 x\n" + old)

    def test_v31_block_is_replaced_by_the_padded_shape(self):
        old = pb.line2_base(50)
        new = pb.format_block(1, 3, "x", 50)
        out = pb.apply_block("\u25b6 1/3 \u00b7 x\n" + old + "\n" + self.DESC, new)
        self.assertEqual(out, new + "\n" + self.DESC)
        self.assertEqual(pb.apply_block(out, new), out)   # second sweep: 0 writes

    def test_wrong_length_nbsp_run_is_not_a_block(self):
        """A human line ending in NBSPs of any other count stays untouched."""
        for extra in (1, 2, 7, 40):
            body = ("\u25b6 1/3 \u00b7 x\n" + pb.line2_base(50)
                    + pb.PAD_CHAR * extra + "\n" + self.DESC)
            self.assertEqual(pb.strip_block(body), body, extra)

    def test_human_nbsp_line_after_a_real_block_survives(self):
        new = pb.format_block(1, 3, "x", 50)
        tail = "\u00a0\u00a0\u00a0 nota humana"
        out = new + "\n" + self.DESC + tail
        self.assertEqual(pb.strip_block(out), self.DESC + tail)

    def test_every_old_shape_migrates_to_the_padded_shape(self):
        desc = self.DESC
        new = pb.format_block(3, 7, "probando tests", 40)
        olds = [
            "\u25b6 1/8 \u00b7 Trace scan call\n[" + "#" * 3 + "=" * 21 + "]\u00a012%",
            "\u25b6 1/8 \u00b7 Trace scan call\n[" + "#" * 3 + "_" * 20 + "]\u00a012%",
            "\u25b6 1/8 \u00b7 Trace scan call\n[" + "#" * 3 + "." * 20 + "]\u00a012%",
            pb.format_block(1, 8, "Trace scan call", 12).rstrip(pb.PAD_CHAR),
        ]
        for old in olds:
            body = old + "\n" + desc
            out = pb.apply_block(body, new)
            self.assertEqual(out, new + "\n" + desc, old)
            self.assertEqual(out.count("\u25b6"), 1, old)
            self.assertEqual(pb.apply_block(out, new), out, old)

    def test_v111_padded_block_at_the_231_box_count_migrates_in_place(self):
        """v1.1.1 padded from a 231 px box, so its pad length differs from this
version's. Such a block must be recognised and replaced in place, never
duplicated, and the second sweep must write nothing."""
        desc = self.DESC
        for pct in range(0, 101, 10):
            base = pb.line2_base(pct)
            k_old = pb._pad_count_for(base, pb._LEGACY_PAD_MIN_PX, pb._LEGACY_PAD_MAX_PX)
            self.assertGreater(k_old, 0, pct)
            self.assertNotEqual(k_old, pb.pad_count(base), pct)
            old = f"\u25b6 1/8 \u00b7 Trace scan call\n{base}{pb.PAD_CHAR * k_old}"
            new = pb.format_block(3, 7, "probando tests", 40)
            out = pb.apply_block(old + "\n" + desc, new)
            self.assertEqual(out, new + "\n" + desc, pct)
            self.assertEqual(out.count("\u25b6"), 1, pct)
            self.assertEqual(pb.strip_block(out), desc, pct)
            self.assertEqual(pb.apply_block(out, new), out, pct)   # 0 writes

    def test_old_shape_at_every_width_8_to_40_migrates(self):
        desc = self.DESC
        new = pb.format_block(1, 2, "x", 50)
        for alphabet in ("=", "_", "."):
            for w in (8, 12, 20, 24, 30, 40):
                bar = "[" + "#" * (w // 2) + alphabet * (w - w // 2) + "]\u00a050%"
                body = "\u25b6 1/2\n" + bar + "\n" + desc
                out = pb.apply_block(body, new)
                self.assertEqual(out, new + "\n" + desc, (alphabet, w))
                self.assertEqual(pb.strip_block(out), desc, (alphabet, w))

    def test_text_after_the_block_is_preserved_byte_exact(self):
        desc = "line one\nline two\n\nNota: acentos áéíóú ñ — 100 % intacto."
        new = pb.format_block(2, 5, "x", 40)
        old = "\u25b6 1/8 \u00b7 Trace scan call\n[" + "#" * 3 + "=" * 21 + "]\u00a012%"
        out = pb.apply_block(old + "\n" + desc, new)
        self.assertEqual(out, new + "\n" + desc)
        self.assertEqual(pb.strip_block(out), desc)


class TestFormatBlock(unittest.TestCase):
    def test_basic_shape(self):
        b = pb.format_block(3, 7, "probando tests", 40)
        l1, l2 = b.split("\n")
        self.assertEqual(l1, "\u25b6 3/7 \u00b7 probando tests")
        """v3.2 appends an invisible NBSP pad after the percent (see TestLine2Pad)."""
        self.assertEqual(l2.rstrip(pb.PAD_CHAR),
                         "[" + "#" * 9 + "." * (pb.BAR_WIDTH - 9) + "]" + "\u00a040%")

    def test_bar_uses_hash_and_period(self):
        for pct in (0, 7, 50):
            l2 = pb.format_block(1, 2, "x", pct).split("\n")[1]
            inner = l2[1:l2.index("]")]
            self.assertEqual(len(inner), pb.BAR_WIDTH)
            self.assertTrue(set(inner) <= {"#", "."}, inner)
            self.assertNotIn("=", l2)
            self.assertNotIn("_", l2)
        inner50 = pb.format_block(1, 2, "x", 50).split("\n")[1][1:1 + pb.BAR_WIDTH]
        self.assertEqual(set(inner50), {"#", "."})

    def test_empty_bar_is_all_period(self):
        l2 = pb.format_block(0, 0, "", 0).split("\n")[1]
        self.assertEqual(l2.rstrip(pb.PAD_CHAR), "[" + "." * pb.BAR_WIDTH + "]\u00a00%")

    def test_full_bar_is_all_hash(self):
        l2 = pb.format_block(5, 5, "x", 100).split("\n")[1]
        self.assertEqual(l2.rstrip(pb.PAD_CHAR), "[" + "#" * pb.BAR_WIDTH + "]\u00a0100%")

    def test_percent_joined_with_nbsp(self):
        b = pb.format_block(3, 7, "x", 40)
        self.assertIn(pb.SEP + "40%", b)
        self.assertNotIn(" 40%", b)

    def test_line1_never_wraps(self):
        for done, total in ((0, 0), (1, 2), (12, 34), (999, 1000)):
            for label in ("x", "a b c d e f g h", "verificar latencia del endpoint desplegado"):
                l1 = pb.format_block(done, total, label, 50).split("\n")[0]
                self.assertLessEqual(pb.text_width(l1), pb.USABLE_WIDTH_PX, l1)

    def test_label_cut_is_deterministic(self):
        a = pb.format_block(1, 2, "uno dos tres cuatro cinco seis siete ocho", 50)
        b = pb.format_block(1, 2, "uno dos tres cuatro cinco seis siete ocho", 50)
        self.assertEqual(a, b)
        self.assertIn("uno dos tres cuatro", a)
        self.assertNotIn("ocho", a)

    def test_label_no_ellipsis_artifacts(self):
        out = pb.format_block(1, 2, "abcdefghijklmnopqrstuvwxyz" * 3, 50)
        label_line = out.split("\n")[0]
        self.assertNotIn("\u2026", label_line)
        self.assertNotIn("...", label_line)

    def test_short_label_kept_complete(self):
        out = pb.format_block(1, 2, "clone repo", 50).split("\n")[0]
        self.assertEqual(out, "\u25b6 1/2 \u00b7 clone repo")

    def test_long_label_cut_on_a_word_boundary(self):
        out = pb.format_block(1, 2, "verificar la latencia del endpoint desplegado", 50)
        l1 = out.split("\n")[0]
        self.assertTrue(l1.endswith("endpoint"), l1)
        self.assertLessEqual(pb.text_width(l1), pb.USABLE_WIDTH_PX)

    def test_long_single_word_cut_mid_word(self):
        out = pb.format_block(1, 2, "W" * 60, 50).split("\n")[0]
        self.assertTrue(out.endswith("W"), out)
        self.assertNotIn(" ", out.split("\u00b7 ")[1])

    def test_pct_bounds_and_default(self):
        """The percent is followed by the v3.2 pad, so compare the visible tail."""
        for done, total, pct, want in ((1, 3, None, "\u00a033%"), (0, 0, None, "\u00a00%"),
                                       (5, 5, None, "\u00a0100%"), (9, 9, 250, "\u00a0100%"),
                                       (9, 9, -5, "\u00a00%")):
            tail = pb.format_block(done, total, "x", pct).split("\n")[1].rstrip(pb.PAD_CHAR)
            self.assertTrue(tail.endswith(want), (done, total, pct, tail))

    def test_done_clamped_to_total(self):
        self.assertIn("7/7", pb.format_block(99, 7, "x", 100))

    def test_no_literal_noise(self):
        out = pb.format_block(3, 7, "probando tests", 40)
        for noise in ("<!--", "-->", "**", "`", "- ["):
            self.assertNotIn(noise, out)

    def test_negative_raises(self):
        with self.assertRaises(ValueError):
            pb.format_block(-1, 2, "x")

    def test_empty_label_omits_the_separator(self):
        l1 = pb.format_block(0, 0, "", 0).split("\n")[0]
        self.assertEqual(l1, "\u25b6 0/0")

    def test_label_with_newlines_collapses(self):
        l1 = pb.format_block(1, 2, "line one\nline two", 50).split("\n")[0]
        self.assertNotIn("\n", l1)
        self.assertIn("line one", l1)


class TestCutToWidth(unittest.TestCase):
    def test_fits_unchanged(self):
        self.assertEqual(pb.cut_to_width("abc", 1000), "abc")

    def test_empty_stays_empty(self):
        self.assertEqual(pb.cut_to_width("", 10), "")

    def test_zero_budget(self):
        self.assertEqual(pb.cut_to_width("abc", 0), "")

    def test_mid_word_when_no_space_fits(self):
        out = pb.cut_to_width("W" * 40, 100)
        self.assertTrue(out)
        self.assertEqual(set(out), {"W"})
        self.assertLessEqual(pb.text_width(out), 100)


class TestApplyStrip(unittest.TestCase):
    DESC = "Entrega el informe de auditoria con las cifras medidas."
    BLOCK = None

    @classmethod
    def setUpClass(cls):
        cls.BLOCK = pb.format_block(3, 7, "probando tests", 40)

    def test_apply_preserves_description(self):
        out = pb.apply_block(self.DESC, self.BLOCK)
        self.assertTrue(out.startswith(self.BLOCK))
        self.assertTrue(out.endswith(self.DESC))
        self.assertEqual(pb.strip_block(out), self.DESC)

    def test_apply_idempotent(self):
        once = pb.apply_block(self.DESC, self.BLOCK)
        twice = pb.apply_block(once, self.BLOCK)
        self.assertEqual(once, twice)

    def test_replace_updates_block_keeps_desc(self):
        out = pb.apply_block(pb.apply_block(self.DESC, self.BLOCK),
                             pb.format_block(7, 7, "cerrando", 100))
        self.assertIn("100%", out)
        self.assertNotIn("40%", out)
        self.assertTrue(out.endswith(self.DESC))

    """--- migration: three shipped alphabets -> v3.1 (`#.`, 23-wide) ---------"""
    V2_BLOCK = "\u25b6 1/8 \u00b7 Trace scan call\n" + \
               "[" + "#" * 3 + "=" * 21 + "]\u00a012%"
    V3_UNDERSCORE_BLOCK = "\u25b6 1/8 \u00b7 Trace scan call\n" + \
                          "[" + "#" * 3 + "_" * 20 + "]\u00a012%"

    def test_old_v2_block_is_replaced_in_place(self):
        body = self.V2_BLOCK + "\n" + self.DESC
        out = pb.apply_block(body, self.BLOCK)
        self.assertTrue(out.startswith(self.BLOCK))
        self.assertTrue(out.endswith(self.DESC))
        self.assertNotIn("Trace scan call", out)
        self.assertNotIn("=", out)
        """exactly one block, no stacking"""
        self.assertEqual(out.count("\u25b6"), 1)

    def test_old_v2_block_extracted_and_stripped(self):
        body = self.V2_BLOCK + "\n" + self.DESC
        self.assertEqual(pb.extract_block(body), self.V2_BLOCK)
        self.assertEqual(pb.strip_block(body), self.DESC)

    def test_old_v2_block_without_label(self):
        old = "\u25b6 0/7\n" + "[" + "=" * 24 + "]\u00a00%"
        self.assertEqual(pb.strip_block(old + "\n" + self.DESC), self.DESC)

    def test_v3_underscore_block_is_replaced_in_place(self):
        body = self.V3_UNDERSCORE_BLOCK + "\n" + self.DESC
        out = pb.apply_block(body, self.BLOCK)
        self.assertTrue(out.startswith(self.BLOCK))
        self.assertTrue(out.endswith(self.DESC))
        self.assertNotIn("Trace scan call", out)
        self.assertNotIn("_", out)
        self.assertEqual(out.count("\u25b6"), 1)

    def test_v3_underscore_block_extracted_and_stripped(self):
        body = self.V3_UNDERSCORE_BLOCK + "\n" + self.DESC
        self.assertEqual(pb.extract_block(body), self.V3_UNDERSCORE_BLOCK)
        self.assertEqual(pb.strip_block(body), self.DESC)

    def test_old_widths_8_to_40_recognised_for_every_alphabet(self):
        for alphabet in ("=", "_", "."):
            for w in (8, 12, 20, 24, 30, 40):
                bar = "[" + "#" * (w // 2) + alphabet * (w - w // 2) + "]\u00a050%"
                body = "\u25b6 1/2\n" + bar + "\n" + self.DESC
                self.assertEqual(pb.strip_block(body), self.DESC, (alphabet, w))

    def test_width_41_is_not_a_block(self):
        bar = "[" + "#" * 41 + "]\u00a050%"
        body = "\u25b6 1/2\n" + bar + "\n" + self.DESC
        self.assertEqual(pb.strip_block(body), body)

    def test_lookalike_wrong_alphabet_untouched(self):
        """`-` is not an accepted bar char (it would also break across the wrap)."""
        bar = "[" + "#" * 10 + "-" * 16 + "]\u00a040%"
        body = "\u25b6 1/2\n" + bar + "\n" + self.DESC
        self.assertEqual(pb.strip_block(body), body)

    def test_lookalike_without_nbsp_percent_untouched(self):
        body = "\u25b6 1/2 \u00b7 leftover\n[" + "#" * 10 + "." * 16 + "] 40%\nreal description"
        self.assertEqual(pb.strip_block(body), body)

    def test_new_block_second_sweep_is_a_noop(self):
        once = pb.apply_block(self.V2_BLOCK + "\n" + self.DESC, self.BLOCK)
        twice = pb.apply_block(once, self.BLOCK)
        self.assertEqual(once, twice)
        self.assertEqual(pb.strip_block(once), self.DESC)

    def test_migration_from_each_old_alphabet_writes_the_new_shape(self):
        new = pb.format_block(3, 7, "probando tests", 40)
        for old in (self.V2_BLOCK, self.V3_UNDERSCORE_BLOCK):
            out = pb.apply_block(old + "\n" + self.DESC, new)
            self.assertTrue(out.startswith(new), old)
            self.assertEqual(out.count("\u25b6"), 1)
            self.assertEqual(pb.strip_block(out), self.DESC)
            """idempotent second sweep: 0 writes"""
            self.assertEqual(pb.apply_block(out, new), out)
            """the new block uses only `#` and `.`"""
            inner = out.split("\n")[1]
            self.assertEqual(set(inner[1:inner.index("]")]) <= {"#", "."}, True)

    """--- edge cases -------------------------------------------------------"""
    def test_empty_body(self):
        self.assertEqual(pb.strip_block(""), "")
        out = pb.apply_block("", self.BLOCK)
        self.assertEqual(out, self.BLOCK)

    def test_none_body(self):
        self.assertEqual(pb.strip_block(None), "")

    def test_block_only_body_strips_to_empty(self):
        self.assertEqual(pb.strip_block(self.BLOCK), "")

    def test_multi_line_description(self):
        desc = "line one\nline two\nline three"
        out = pb.apply_block(desc, self.BLOCK)
        self.assertEqual(pb.strip_block(out), desc)

    def test_very_long_description(self):
        desc = "x" * 20000
        out = pb.apply_block(desc, self.BLOCK)
        self.assertEqual(pb.strip_block(out), desc)

    def test_human_edit_after_block_survives(self):
        out = pb.apply_block(self.DESC, self.BLOCK)
        out = out + "\n\nNota humana: revisar el anexo."
        self.assertTrue(pb.strip_block(out).endswith("revisar el anexo."))

    def test_leading_blank_lines_after_strip(self):
        out = pb.apply_block("\n\n" + self.DESC, self.BLOCK)
        self.assertEqual(pb.strip_block(out), self.DESC)

    def test_extract_block(self):
        out = pb.apply_block(self.DESC, self.BLOCK)
        self.assertEqual(pb.extract_block(out), self.BLOCK)
        self.assertIsNone(pb.extract_block(self.DESC))


class TestSpilledPad(unittest.TestCase):
    """A pre-v3.2 strip removed the block but not its pad, so every such sweep
    pushed the NBSP run one line down and the description drifted away from the
    card face. The block must own those lines, not accumulate them.
    """

    BLOCK = pb.format_block(1, 3, "find scope", 40)
    DESC = "Objetivo: medir el acceso.\n\nContexto: sin cambios."

    def _spilled(self, n=3):
        line2 = self.BLOCK.rsplit("\n", 1)[1]
        pad = line2[len(line2.rstrip(pb.PAD_CHAR)):]
        return self.BLOCK + "\n" + "\n".join([pad] * n) + "\n" + self.DESC

    def test_strip_removes_spilled_pad_lines(self):
        self.assertEqual(pb.strip_block(self._spilled()), self.DESC)

    def test_heal_keeps_one_block_and_the_description(self):
        healed = pb.heal_body(self._spilled(7))
        self.assertEqual(healed, self.BLOCK + "\n" + self.DESC)
        self.assertEqual(pb.heal_body(healed), healed)          # idempotent
        self.assertEqual(pb.heal_body(self.DESC), self.DESC)    # no block, untouched

    def test_apply_block_after_a_spill_does_not_grow_the_body(self):
        body = pb.apply_block(self._spilled(5), pb.format_block(2, 3, "write report", 66))
        self.assertEqual(body.count(pb.GLYPH), 1)
        self.assertEqual(
            [ln for ln in body.split("\n") if ln and set(ln) == {pb.PAD_CHAR}], []
        )


class TestTaskList(unittest.TestCase):
    """v1.2: the whole Tasklist goes below the bar, one deterministic line per
    item, and is owned by the block (replaced in place, stripped with it).
    """

    TODOS = [
        {"content": "leer el issue", "status": "completed"},
        {"content": "medir el ancho", "status": "in_progress"},
        {"content": "escribir   tests", "status": "pending"},
        {"content": "descartado", "status": "cancelled"},
    ]
    DESC = "Objetivo: medir el acceso.\n\nContexto: sin cambios."

    def test_one_line_per_item_right_after_the_pad(self):
        block = pb.format_block(1, 4, "medir el ancho", 25, todos=self.TODOS)
        self.assertEqual(block.split("\n")[2:], [
            "\u2022 [x] leer el issue",
            "\u2022 [>] medir el ancho",
            "\u2022 [ ] escribir tests",
            "\u2022 [-] descartado",
        ])

    def test_without_todos_the_block_is_unchanged(self):
        self.assertEqual(pb.format_block(1, 4, "x", 25),
                         pb.format_block(1, 4, "x", 25, todos=[]))
        self.assertNotIn(pb.LIST_BULLET, pb.format_block(1, 4, "x", 25))

    def test_apply_replaces_the_list_and_strip_takes_it_away(self):
        first = pb.apply_block(self.DESC, pb.format_block(1, 4, "a", 25, todos=self.TODOS[:1]))
        grown = pb.apply_block(first, pb.format_block(2, 4, "b", 50, todos=self.TODOS))
        self.assertEqual(grown.count(pb.LIST_BULLET), 4)
        self.assertTrue(grown.endswith(self.DESC))
        self.assertEqual(pb.apply_block(grown, pb.format_block(2, 4, "b", 50, todos=self.TODOS)),
                         grown)                                   # idempotent
        self.assertEqual(pb.strip_block(grown), self.DESC)

    def test_item_line_breaks_are_flattened(self):
        block = pb.format_block(1, 1, "a", 100,
                                todos=[{"content": "uno\ndos   tres ", "status": "pending"}])
        self.assertEqual(block.split("\n")[2], "\u2022 [ ] uno dos tres")

    def test_spilled_pad_line_between_pad_and_list_is_healed(self):
        """An older strip left the pad on its own line; it must not hide the list."""
        block = pb.format_block(1, 4, "a", 25, todos=self.TODOS)
        line1, line2 = block.split("\n")[:2]
        pad = line2[len(line2.rstrip(pb.PAD_CHAR)):]
        spilled = "\n".join([line1, line2.rstrip(pb.PAD_CHAR), pad,
                             *block.split("\n")[2:], self.DESC])
        self.assertEqual(pb.strip_block(spilled), self.DESC)
        healed = pb.apply_block(spilled, block)
        self.assertEqual(healed.count(pb.LIST_BULLET), len(self.TODOS))
        self.assertTrue(healed.endswith(self.DESC))

    def test_line_two_is_still_the_padded_bar(self):
        plain = pb.format_block(1, 4, "medir el ancho", 25)
        with_list = pb.format_block(1, 4, "medir el ancho", 25, todos=self.TODOS)
        self.assertEqual(plain.split("\n")[1], with_list.split("\n")[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
