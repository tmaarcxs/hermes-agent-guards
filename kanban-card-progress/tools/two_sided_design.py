#!/usr/bin/env python3
"""two_sided_design.py — validate the candidate design against BOTH reachable box
sizes before committing it:

  * line 1 and the bar must FIT the SMALLEST box (209 px);
  * the pad must BLOCK a word at the LARGEST box (217 px).

Builds the block with the candidate constants, renders it at 209 and 217 in the
isolated instance, and reports leaks / pct visibility / line-1 lines.
"""
import importlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/marcos.sanchez/.hermes/artifacts/ui-probe-instance")
from cdp import CDP, page_target  # noqa: E402

PORT = sys.argv[1]
PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
import progress_block as pb  # noqa: E402

LABELS = ["clone repo", "W" * 60, "add the pad and the width table now",
          "verificar la latencia del endpoint desplegado",
          "check every percent from 0 to 100 and 999",
          "REVIEW THE MIGRATION AND THE WIDTH TABLE",
          "settle the real card text box width and fix the geometry"]
STARTERS = ["a", "i", "l", "1", "x", "it", "Go", "the", "tests", "W"]

JS = r"""
window.__two = function(cases) {
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
    const lineOf = i => { const rng = document.createRange(); rng.setStart(tn,0); rng.setEnd(tn,i);
      const s = new Set(); for (const q of rng.getClientRects()) if (q.width||q.height) s.add(+q.top.toFixed(2));
      return s.size; };
    const rect = (i,n) => { const r=document.createRange(); r.setStart(tn,i); r.setEnd(tn,i+n); return r.getBoundingClientRect(); };
    const pct = rect(c.pctIdx, c.pctLen);
    const word = rect(c.wordIdx, c.wordLen);
    out.push(Object.assign({}, c.meta, {
      pctLine: lineOf(c.pctIdx),
      pct_visible: pct.top < visTop - 0.5,
      wordLine: lineOf(c.wordIdx),
      leak: word.top <= pct.top + 0.5,
    }));
  }
  host.textContent = saved;
  return JSON.stringify({boxW: +box.width.toFixed(3), results: out});
};
"""

t = page_target(PORT)
c = CDP(t["webSocketDebuggerUrl"])

cases = []
for label in LABELS:
    for done, total in ((1, 1), (12, 34), (123, 123)):
        blk = pb.format_block(done, total, label, 50)
        head, l2 = blk.split("\n", 1)
        text = f"{head} {l2} realistic description"
        cases.append({"text": text, "pctIdx": text.index("50%"), "pctLen": 3,
                      "wordIdx": text.index("realistic"), "wordLen": 9,
                      "meta": {"kind": "line1", "label": label[:24], "done": done, "total": total}})
for pct in range(0, 101, 2):
    for st in STARTERS:
        blk = pb.format_block(12, 34, "x", pct)
        head, l2 = blk.split("\n", 1)
        text = f"{head} {l2} {st} tail"
        wi = text.index(f" {st} tail") + 1
        cases.append({"text": text, "pctIdx": text.index(f"{pct}%"), "pctLen": len(f"{pct}%"),
                      "wordIdx": wi, "wordLen": len(st),
                      "meta": {"kind": "pad", "pct": pct, "starter": st}})

for label, setup in (("box217", "window.__lane.style.overflowY=''"),
                     ("box209", "window.__lane.style.overflowY='scroll'")):
    c.evaluate("window.__lane = [...document.querySelectorAll('span')].find(s=>s.className&&s.className.includes('text-[0.6875rem]')&&s.className.includes('line-clamp-2')&&s.getBoundingClientRect().width>100).closest('.overflow-y-auto')")
    c.evaluate(setup)
    time.sleep(0.7)
    c.evaluate(JS)
    res = json.loads(c.evaluate("window.__two(%s)" % json.dumps(cases)))
    r = res["results"]
    leaks = [x for x in r if x["leak"]]
    inv = [x for x in r if not x["pct_visible"]]
    l1bad = [x for x in r if x["kind"] == "line1" and x["pctLine"] != 2]
    print(f"{label}: boxW={res['boxW']} cases={len(r)} leaks={len(leaks)} pct_invisible={len(inv)} line1_bad={len(l1bad)}")
    for x in leaks[:4]:
        print("   leak", x)
    for x in l1bad[:4]:
        print("   l1bad", x)
    for x in inv[:4]:
        print("   inv", x)
c.evaluate("window.__lane.style.overflowY=''")
c.close()
