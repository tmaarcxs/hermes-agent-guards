#!/usr/bin/env python3
"""attack_v112.py — independent real-renderer attack of the v1.1.2 candidate.

Re-runs the F3 attack shape (t_f713d7d6) against THIS candidate, in the app's own
renderer (CDP + Range.getClientRects), never on the operator's window:

  1. first-word leak: 101 pct x 3 digit-counts x starters -> leak must be 0
  2. line 1 on ONE visual line for every cut label and every N/M digit count
  3. `NN%` visible on the card face (pct_visible_on_card) for 101 pct x 3 counts
  4. the 6 label shapes of t_c82fa710 + the 3 widest realistic headers
  5. negative control: the same measurement fed the UNPADDED line 2 must leak

Writes attack_v112.json + cases CSV.
"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/marcos.sanchez/.hermes/artifacts/ui-probe-instance")
from cdp import CDP, page_target  # noqa: E402

PLUGIN = Path(__file__).resolve().parent.parent   # the plugin dir (tools/ is inside it)
sys.path.insert(0, str(PLUGIN))
import progress_block as pb  # noqa: E402

PORT = sys.argv[1]
OUTDIR = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".")

"""---- case construction -------------------------------------------------------"""
STARTERS = ["a", "I", "x", "it", "Go", "Fix", "Add", "Run", "the", "to", "in",
            "Ok", "Goal:", "Use", "Set", "tests", "repo", "i", "l", ".", "-", "1",
            "Clone", "Verify", "Write", "Ship"]
DESC = "realistic description of the task that continues after the block"

cases = []


def add(text, pct, meta, desc_marker=None):
    idx = text.index(f"{pct}%")
    if desc_marker is not None:
        di = text.index(desc_marker)
        """A marker that starts with the joining space must point at the first
non-space char: the space sits at the end of line 2 and would falsely
read as "on the same line as the percent"."""
        while di < len(text) and text[di] == " ":
            di += 1
    else:
        di = len(text)
    cases.append({"text": text, "pctIdx": idx, "pctLen": len(f"{pct}%"),
                  "descIdx": di, "padIdx": idx + len(f"{pct}%"), "padLen": 0,
                  "meta": meta})


"""1+3: every pct x digit counts, label 'x', description appended (leak attack)"""
for done, total in ((1, 3), (12, 34), (123, 123)):
    for pct in range(101):
        blk = pb.format_block(done, total, "x", pct)
        head, l2 = blk.split("\n", 1)
        text = head + " " + l2 + " " + DESC
        add(text, pct, {"kind": "leak", "done": done, "total": total, "pct": pct},
            desc_marker=DESC)

"""1b: first-word attack — every pct x digit count x starter word, after the pad.
The pad must leave no room for ANY word to start on line 2."""
for done, total in ((1, 3), (12, 34), (123, 123)):
    for pct in range(101):
        blk = pb.format_block(done, total, "x", pct)
        head, l2 = blk.split("\n", 1)
        for st in STARTERS:
            text = f"{head} {l2} {st} tail"
            add(text, pct, {"kind": "starter", "done": done, "total": total,
                            "pct": pct, "starter": st}, desc_marker=f" {st} tail")

"""2: line 1 on one visual line for every cut label (6 shapes) x digit counts"""
SHAPES = {
    "short": "clone repo",
    "long_single_word": "W" * 60,
    "many_short_words": "add the pad and the width table now",
    "accents": "verificar la latencia del endpoint desplegado",
    "digits_heavy": "check every percent from 0 to 100 and 999",
    "uppercase_heavy": "REVIEW THE MIGRATION AND THE WIDTH TABLE",
}
for name, label in SHAPES.items():
    for done, total in ((1, 1), (12, 34), (123, 123)):
        blk = pb.format_block(done, total, label, 50)
        head, l2 = blk.split("\n", 1)
        text = head + " " + l2 + " " + DESC
        add(text, 50, {"kind": "line1", "shape": name, "done": done, "total": total},
            desc_marker=DESC)

"""6 (widest realistic headers): longest 3-digit labels from the real board"""
for label in ("settle the real card text box width and fix the geometry",
              "Independent real-renderer check of kanban-card-progress",
              "Adversarial first-word leak test of padded progress block"):
    blk = pb.format_block(999, 999, label, 100)
    head, l2 = blk.split("\n", 1)
    text = head + " " + l2 + " " + DESC
    add(text, 100, {"kind": "widest_header", "label": label}, desc_marker=DESC)

"""negative control: UNPADDED line 2 (the pre-v3.2 leak) must leak"""
for pct in (0, 30, 50, 70, 90):
    base = pb.line2_base(pct)                      # no pad
    text = f"\u25b6 1/3 \u00b7 x {base} a"
    add(text, pct, {"kind": "negative_control", "pct": pct}, desc_marker=" a")

JS = r"""
window.__attack = function(cases) {
  const host = [...document.querySelectorAll('span')].find(
      s => s.className && s.className.includes('text-[0.6875rem]')
           && s.className.includes('line-clamp-2')
           && s.getBoundingClientRect().width > 100);
  if (!host) return JSON.stringify({error: 'host span not found'});
  const box = host.getBoundingClientRect();
  const cs = getComputedStyle(host);
  const clampN = parseInt(cs.webkitLineClamp, 10) || 0;
  const visTop = box.top + clampN * parseFloat(cs.lineHeight);
  const saved = host.textContent;
  const out = [];
  for (const c of cases) {
    host.textContent = c.text;
    const tn = host.firstChild;
    function cr(i, n) {
      const rr = document.createRange();
      rr.setStart(tn, i); rr.setEnd(tn, i + n);
      return rr.getBoundingClientRect();
    }
    // visual line index of a char offset (0-based, by distinct top)
    function lineOf(i) {
      const p = cr(i, 1);
      const rng = document.createRange();
      rng.setStart(tn, 0); rng.setEnd(tn, i);
      const tops = new Set();
      for (const q of rng.getClientRects()) if (q.width || q.height) tops.add(+q.top.toFixed(2));
      return tops.size;
    }
    const pct = cr(c.pctIdx, c.pctLen);
    const desc = c.descIdx < tn.length ? cr(c.descIdx, 1) : null;
    out.push(Object.assign({}, c.meta, {
      pctTop: +pct.top.toFixed(2), pctRight: +pct.right.toFixed(2),
      pctLine: lineOf(c.pctIdx),          // distinct visual lines before `%`; 2 = line 1 on one line
      descLine: desc ? lineOf(c.descIdx) : null,
      leak: desc ? (desc.top <= pct.top + 0.5) : false,
      pct_visible_on_card: pct.top < visTop - 0.5,
    }));
  }
  host.textContent = saved;
  return JSON.stringify({boxW: box.width, lineHeight: cs.lineHeight, clamp: cs.webkitLineClamp,
                         results: out});
};
"""

t = page_target(PORT)
c = CDP(t["webSocketDebuggerUrl"])
c.evaluate(JS)
res = json.loads(c.evaluate("window.__attack(%s)" % json.dumps(cases)))
c.close()

results = res["results"]
leaks = [r for r in results if r.get("leak")]
neg = [r for r in results if r["kind"] == "negative_control"]
line1 = [r for r in results if r["kind"] == "line1"]
pctvis = [r for r in results if r["kind"] in ("leak", "starter")]
"""A "leak" is only a defect outside the deliberate negative control."""
real_leaks = [r for r in leaks if r["kind"] != "negative_control"]

report = {
    "boxW": res["boxW"], "lineHeight": res["lineHeight"], "clamp": res["clamp"],
    "cases": len(results),
    "leaks": len(real_leaks),
    "negative_control_leaks": sum(1 for r in neg if r["leak"]),
    "negative_control_cases": len(neg),
    "line1_not_one_visual_line": [r for r in line1 if r["pctLine"] != 2],
    "pct_not_visible_on_card": [r for r in pctvis if not r["pct_visible_on_card"]],
    "pct_visible_true": sum(1 for r in pctvis if r["pct_visible_on_card"]),
    "pct_total": len(pctvis),
}
(OUTDIR / "attack_v112.json").write_text(json.dumps(report, indent=1) + "\n")
with open(OUTDIR / "cases_attack_v112.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["kind", "pct", "done", "total", "shape", "starter", "leak", "pctLine",
                "descLine", "pct_visible_on_card", "pctTop"])
    for r in results:
        w.writerow([r.get("kind"), r.get("pct"), r.get("done"), r.get("total"),
                    r.get("shape", ""), r.get("starter", ""), r.get("leak"),
                    r.get("pctLine"), r.get("descLine"), r.get("pct_visible_on_card"),
                    r.get("pctTop")])
print(json.dumps({k: v for k, v in report.items()
                  if k not in ("line1_not_one_visual_line", "pct_not_visible_on_card")}, indent=1))
print("line1 bad:", len(report["line1_not_one_visual_line"]),
      "pct not visible:", len(report["pct_not_visible_on_card"]),
      "real leaks:", len(real_leaks))
