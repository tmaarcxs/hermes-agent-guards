#!/usr/bin/env python3
"""width_sweep_v112.py — run the attack at several real conditions and record the
box + leaks: window viewport widths, UI zoom, and the smallest reachable box
(classic 8px lane scrollbar). Isolated renderer only.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/marcos.sanchez/.hermes/artifacts/ui-probe-instance")
from cdp import CDP, page_target  # noqa: E402

PORT = sys.argv[1]
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("cases_widths_v112.csv")
PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
import progress_block as pb  # noqa: E402

BOX = r"""
(() => {
  const h = [...document.querySelectorAll('span')].find(s => s.className
      && s.className.includes('text-[0.6875rem]') && s.className.includes('line-clamp-2')
      && s.getBoundingClientRect().width > 100);
  if (!h) return JSON.stringify({error:'no host'});
  const lane = h.closest('.overflow-y-auto');
  return JSON.stringify({boxW: +h.getBoundingClientRect().width.toFixed(3),
    laneScrollbar: lane ? lane.offsetWidth - lane.clientWidth : 0,
    innerW: window.innerWidth, rootFont: getComputedStyle(document.documentElement).fontSize});
})()
"""

t = page_target(PORT)
c = CDP(t["webSocketDebuggerUrl"])
rows = []


def run(label, extra=None):
    if extra:
        c.evaluate(extra)
        time.sleep(0.7)
    box = json.loads(c.evaluate(BOX))
    # build a small leak probe inline (cheaper than the full attack)
    blk = pb.format_block(12, 34, "verificar la latencia del endpoint", 50)
    head, l2 = blk.split("\n", 1)
    text = f"{head} {l2} realistic description"
    di = text.index("realistic")
    r = json.loads(c.evaluate(
        "window.__q(%s,%d,%d)" % (json.dumps(text), text.index("50%"), di)))
    rows.append({"condition": label, "boxW": box["boxW"],
                 "laneScrollbar": box["laneScrollbar"], "innerW": box["innerW"],
                 "rootFont": box["rootFont"], "line1_lines": r["line1_lines"],
                 "pct_visible_on_card": r["pct_visible"]})


c.evaluate(r"""
window.__q = function(text, pctIdx, descIdx) {
  const h = [...document.querySelectorAll('span')].find(s => s.className
      && s.className.includes('text-[0.6875rem]') && s.className.includes('line-clamp-2')
      && s.getBoundingClientRect().width > 100);
  const saved = h.textContent;
  const box = h.getBoundingClientRect();
  const cs = getComputedStyle(h);
  const clampN = parseInt(cs.webkitLineClamp, 10) || 0;
  const visTop = box.top + clampN * parseFloat(cs.lineHeight);
  h.textContent = text; const tn = h.firstChild;
  const rng = document.createRange(); rng.setStart(tn, 0); rng.setEnd(tn, pctIdx);
  const tops = new Set();
  for (const q of rng.getClientRects()) if (q.width || q.height) tops.add(+q.top.toFixed(2));
  const pr = document.createRange(); pr.setStart(tn, pctIdx); pr.setEnd(tn, pctIdx + 3);
  const pct = pr.getBoundingClientRect();
  h.textContent = saved;
  return JSON.stringify({line1_lines: tops.size, pct_visible: pct.top < visTop - 0.5});
};
""")

run("baseline_100")
for w in (900, 1400, 2200, 2560):
    run(f"viewport_{w}", "window.__lane=window.__lane||null;")
    c.cmd("Emulation.setDeviceMetricsOverride",
          {"width": w, "height": 900, "deviceScaleFactor": 0, "mobile": False})
    time.sleep(0.6)
    box = json.loads(c.evaluate(BOX))
    rows.append({"condition": f"viewport_{w}", "boxW": box["boxW"],
                 "laneScrollbar": box["laneScrollbar"], "innerW": box["innerW"],
                 "rootFont": box["rootFont"], "line1_lines": None,
                 "pct_visible_on_card": None})
c.cmd("Emulation.clearDeviceMetricsOverride")
time.sleep(0.5)

for pct in (90, 110, 125, 150, 175):
    c.evaluate(f"window.hermesDesktop.zoom.setPercent({pct})")
    time.sleep(0.7)
    box = json.loads(c.evaluate(BOX))
    rows.append({"condition": f"uizoom_{pct}", "boxW": box["boxW"],
                 "laneScrollbar": box["laneScrollbar"], "innerW": box["innerW"],
                 "rootFont": box["rootFont"], "line1_lines": None,
                 "pct_visible_on_card": None})
c.evaluate("window.hermesDesktop.zoom.setPercent(100)")
time.sleep(0.5)

# smallest reachable: force a classic 8px lane scrollbar
c.evaluate("window.__lane = [...document.querySelectorAll('span')].find(s=>s.className&&s.className.includes('text-[0.6875rem]')&&s.className.includes('line-clamp-2')&&s.getBoundingClientRect().width>100).closest('.overflow-y-auto'); window.__lane.style.overflowY='scroll'")
time.sleep(0.6)
box = json.loads(c.evaluate(BOX))
run("smallest_classic_scrollbar")
c.evaluate("window.__lane.style.overflowY=''")
time.sleep(0.4)

c.close()
import csv
with open(OUT, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
for r in rows:
    print(r)
print("wrote", OUT)
