#!/usr/bin/env python3
"""measure_advances.py — measure per-glyph advance widths in the app's OWN
renderer and write tests/measured_advances.json.

Why this exists: the earlier card shipped a `measured_advances.json` that was a
copy of the module's WIDTH_TABLE (111/112 entries byte-identical), so its
"independent reference" pinned the table against itself and could not catch an
inflated table. This script measures every glyph from the live Blink layout via
CDP Range.getClientRects, from a glyph-in-context difference (so the space and
other collapsing glyphs are measured too), and records its provenance.

Usage (the isolated probe instance must be running):
    ~/.hermes/hermes-agent/.venv/bin/python3 tools/measure_advances.py <cdp_port> [out.json]

The card preview span (the `line-clamp-2 text-[0.6875rem]` element the board
renders the body into) is the measurement surface: it is the exact element the
progress block is laid out in.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".hermes" / "artifacts" / "ui-probe-instance"))
from cdp import CDP, page_target  # noqa: E402

TOL_PX = 0.05

"""Every glyph the block can contain, plus the ASCII/alnum set the tests cover."""
GLYPHS = (
    list(" !\"#$%&'()*+,-./")
    + list("0123456789")
    + list(":;<=>?@")
    + list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + list("[\\]^_`")
    + list("abcdefghijklmnopqrstuvwxyz")
    + list("{|}~")
    + list("\u25b6\u00b7\u00a0")            # ▶ · NBSP
    + list("\u00e1\u00e9\u00ed\u00f3\u00fa\u00f1\u00fc")
    + list("\u00c1\u00c9\u00cd\u00d3\u00da\u00d1\u00dc")
)

MEASURE_JS = r"""
window.__measure_advances = function(glyphs) {
  const host = [...document.querySelectorAll('span')].find(
      s => s.className && s.className.includes('text-[0.6875rem]')
           && s.className.includes('line-clamp-2')
           && s.getBoundingClientRect().width > 100);
  if (!host) return JSON.stringify({error: 'preview span not found'});
  const saved = host.textContent;
  const box = host.getBoundingClientRect();
  // width of a string, measured as a Range over the span's single text node
  function width(t) {
    host.textContent = t;
    const tn = host.firstChild;
    if (!tn) return 0;
    const rng = document.createRange();
    rng.setStart(tn, 0);
    rng.setEnd(tn, tn.length);
    return +rng.getBoundingClientRect().width.toFixed(4);
  }
  // Single-glyph advance, the quantity WIDTH_TABLE documents: W(g) measured
  // alone. A collapsing glyph (space) measures 0 alone, so it uses the
  // in-context difference W('H'+g+'H') - W('HH') instead.
  const base = width('HH');
  const adv = {};
  const adv20 = {};
  for (const g of glyphs) {
    const alone = width(g);
    if (alone > 0) {
      adv[g] = +alone.toFixed(4);
      adv20[g] = +(width(g.repeat(20)) / 20).toFixed(4);
    } else {
      adv[g] = +(width('H' + g + 'H') - base).toFixed(4);
      adv20[g] = +((width('H' + g.repeat(20) + 'H') - base) / 20).toFixed(4);
    }
  }
  // one-line wrap boundary for the current box (space-separated words)
  let maxWords = 0, maxPx = 0;
  for (let n = 1; n <= 40; n++) {
    const t = Array(n).fill('W').join(' ');
    host.textContent = t;
    const rng = document.createRange(); rng.selectNodeContents(host);
    const tops = new Set();
    for (const r of rng.getClientRects()) if (r.width || r.height) tops.add(+r.top.toFixed(1));
    if (tops.size === 1) { maxWords = n; maxPx = +rng.getBoundingClientRect().width.toFixed(4); }
    else break;
  }
  // the two reachable box sizes: idle lane (no scrollbar) and busy lane (8 px
  // `::-webkit-scrollbar`). Force the lane to scroll to measure the small box.
  const lane = host.closest('.overflow-y-auto');
  let busyBoxW = null;
  if (lane) {
    const saved = lane.getAttribute('style') || '';
    lane.style.overflowY = 'scroll';
    busyBoxW = +host.getBoundingClientRect().width.toFixed(4);
    if (saved) lane.setAttribute('style', saved); else lane.removeAttribute('style');
  }
  host.textContent = saved;
  return JSON.stringify({
    boxW: +box.width.toFixed(4), boxH: +box.height.toFixed(4),
    busyBoxW: busyBoxW,
    clientW: host.clientWidth, scrollW: host.scrollWidth,
    fontSize: getComputedStyle(host).fontSize,
    lineHeight: getComputedStyle(host).lineHeight,
    rootFont: getComputedStyle(document.documentElement).fontSize,
    dpr: window.devicePixelRatio, innerW: window.innerWidth,
    maxWordsOneLine: maxWords, maxWordsOneLinePx: maxPx,
    advances: adv, advances20: adv20,
  });
};
"""


def main() -> int:
    port = sys.argv[1]
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("tests/measured_advances.json")

    t = page_target(port)
    if not t:
        print("no page target on port", port)
        return 1
    c = CDP(t["webSocketDebuggerUrl"])
    c.evaluate(MEASURE_JS)
    raw = c.evaluate("window.__measure_advances(%s)" % json.dumps(GLYPHS))
    c.close()
    m = json.loads(raw)
    if "error" in m:
        print("measurement failed:", raw)
        return 1

    """Use the run-of-20 average (rounding-free) when it agrees with the single
glyph; else the single-glyph value (the quantity WIDTH_TABLE documents)."""
    advances = {}
    for g in GLYPHS:
        single, run20 = m["advances"][g], m["advances20"][g]
        advances[g] = run20 if abs(run20 - single) <= 0.02 else single

    """The layout reference is the SMALLEST reachable box (a busy lane shows the
8 px scrollbar); record the idle box too for provenance."""
    idle = m["boxW"]
    busy = m.get("busyBoxW")
    smallest = min(x for x in (idle, busy) if x is not None)
    payload = {
        "_comment": (
            "Measured per-glyph advance widths (CSS px) of the card preview font, "
            "measured in the app's own Blink by tools/measure_advances.py via CDP "
            "Range.getClientRects on the real card preview span. NOT copied from "
            "progress_block.WIDTH_TABLE. WIDTH_TABLE must equal this within TOL_PX; "
            "TestWidthTableMatchesMeasuredReference enforces it."
        ),
        "provenance": {
            "method": "adv(g) = W(g) measured alone (single-glyph advance), via "
                      "Range.getClientRects on the card preview span; collapsing glyphs "
                      "(space) use W('H'+g+'H') - W('HH')",
            "surface": "the `line-clamp-2 text-[0.6875rem]` card preview span, isolated Hermes desktop instance",
            "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "box_w_css_px": m["boxW"],
            "busy_lane_box_w_css_px": m.get("busyBoxW"),
            "root_font_size": m["rootFont"],
            "device_pixel_ratio": m["dpr"],
            "viewport_inner_w": m["innerW"],
        },
        "TOL_PX": TOL_PX,
        "content_box_px": smallest,
        "idle_lane_box_px": idle,
        "busy_lane_box_px": busy,
        "max_words_one_line": m["maxWordsOneLine"],
        "max_words_one_line_px": m["maxWordsOneLinePx"],
        "advances": advances,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print("wrote", out, "boxW", m["boxW"], "maxWords", m["maxWordsOneLine"],
          "maxPx", m["maxWordsOneLinePx"], "glyphs", len(advances))
    return 0


if __name__ == "__main__":
    sys.exit(main())
