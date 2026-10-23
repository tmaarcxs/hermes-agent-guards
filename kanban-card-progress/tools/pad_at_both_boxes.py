#!/usr/bin/env python3
"""pad_at_both_boxes.py — prove the pad rule at BOTH reachable box sizes.

For a candidate pad sized from a reference box, render the block + a starter word
at box 209 and 217 and report: NN% visible, first-word leak, and the token's
visual line. Isolated renderer only.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/marcos.sanchez/.hermes/artifacts/ui-probe-instance")
from cdp import CDP, page_target  # noqa: E402

PORT = sys.argv[1]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import progress_block as pb  # noqa: E402

STARTERS = ["a", "i", "l", "1", "x", "it", "Go", "the", "tests", "W"]

JS = r"""
window.__padtest = function(cases) {
  const host = [...document.querySelectorAll('span')].find(s => s.className
      && s.className.includes('text-[0.6875rem]') && s.className.includes('line-clamp-2')
      && s.getBoundingClientRect().width > 100);
  const box = host.getBoundingClientRect();
  const cs = getComputedStyle(host);
  const clampN = parseInt(cs.webkitLineClamp, 10) || 0;
  const visTop = box.top + clampN * parseFloat(cs.lineHeight);
  const saved = host.textContent;
  const out = [];
  for (const c of cases) {
    host.textContent = c.text;
    const tn = host.firstChild;
    function lineOf(i) {
      const rng = document.createRange(); rng.setStart(tn, 0); rng.setEnd(tn, i);
      const tops = new Set();
      for (const q of rng.getClientRects()) if (q.width || q.height) tops.add(+q.top.toFixed(2));
      return tops.size;
    }
    function rect(i, n) { const r = document.createRange(); r.setStart(tn, i); r.setEnd(tn, i + n); return r.getBoundingClientRect(); }
    const pct = rect(c.pctIdx, c.pctLen);
    const word = rect(c.wordIdx, c.wordLen);
    out.push(Object.assign({}, c.meta, {
      pctRight: +pct.right.toFixed(2), pctTop: +pct.top.toFixed(2),
      pct_visible: pct.top < visTop - 0.5,
      wordTop: +word.top.toFixed(2), wordLine: lineOf(c.wordIdx),
      leak: word.top <= pct.top + 0.5,
    }));
  }
  host.textContent = saved;
  return JSON.stringify({boxW: +box.width.toFixed(3), results: out});
};
"""

t = page_target(PORT)
c = CDP(t["webSocketDebuggerUrl"])


def build_cases():
    cs = []
    for pct in range(0, 101, 5):
        for st in STARTERS:
            blk = pb.format_block(12, 34, "x", pct)
            head, l2 = blk.split("\n", 1)
            text = f"{head} {l2} {st} tail"
            wi = text.index(f" {st} tail") + 1
            cs.append({"text": text, "pctIdx": text.index(f"{pct}%"),
                       "pctLen": len(f"{pct}%"), "wordIdx": wi, "wordLen": len(st),
                       "meta": {"pct": pct, "starter": st}})
    return cs


cases = build_cases()
for label, setup in (("box217", "window.__lane.style.overflowY=''"),
                     ("box209", "window.__lane.style.overflowY='scroll'")):
    c.evaluate("window.__lane = [...document.querySelectorAll('span')].find(s=>s.className&&s.className.includes('text-[0.6875rem]')&&s.className.includes('line-clamp-2')&&s.getBoundingClientRect().width>100).closest('.overflow-y-auto')")
    c.evaluate(setup)
    time.sleep(0.7)
    c.evaluate(JS)
    res = json.loads(c.evaluate("window.__padtest(%s)" % json.dumps(cases)))
    leaks = [r for r in res["results"] if r["leak"]]
    invisible = [r for r in res["results"] if not r["pct_visible"]]
    print(f"{label}: boxW={res['boxW']} cases={len(res['results'])} leaks={len(leaks)} "
          f"pct_invisible={len(invisible)}")
    for r in leaks[:6]:
        print("   leak", r)
    for r in invisible[:6]:
        print("   pct invisible", r)
c.evaluate("window.__lane.style.overflowY=''")
c.close()
