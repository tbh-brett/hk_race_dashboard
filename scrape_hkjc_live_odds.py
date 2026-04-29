"""
scrape_hkjc_live_odds.py
------------------------
Scrapes Win/Place + Quinella + Quinella-Place odds snapshots from
https://bet.hkjc.com for all races of a meeting. Uses Playwright because
bet.hkjc.com is a React SPA — the odds are not in the initial HTML.

Snapshots are stored as JSON per race:
    cache/live_odds/YYYYMMDD/{VENUE}_R{N}_{HHMMSS}.json

Each snapshot:
{
  "scraped_at":     ISO timestamp (local HK time)
  "last_update":    HKJC-reported "Last Update: DD/MM/YYYY HH:MM" (win/place)
  "date":           "2026-04-22"
  "venue":          "HV" | "ST"
  "race_no":        1
  "race_info":      "Class 5, 1200m, TURF, ..."
  "odds": [                              # win/place (kept for back-compat)
      {"no":"1", "horse":"HAPPY ALLIANCE", "win":"40", "place":"10"},
      ...
  ],
  "qin_odds": [                          # quinella pair odds
      {"a":"1", "b":"2", "odds":"123.4"},
      ...
  ],
  "qpl_odds": [                          # quinella place pair odds
      {"a":"1", "b":"2", "odds":"45.6"},
      ...
  ],
  "qin_last_update": "Last Update: ..."  # (may be empty)
  "qpl_last_update": "Last Update: ..."
}

Usage:
    python scrape_hkjc_live_odds.py --date 2026-04-22 --venue HV
    python scrape_hkjc_live_odds.py --date 2026-04-22 --venue HV --races 1,2,3
    python scrape_hkjc_live_odds.py                      # today, auto-venue
    python scrape_hkjc_live_odds.py --pools wp,qin,qpl   # default: all three
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
OUT_ROOT = BASE / "cache" / "live_odds"


def _parse_race_info(txt: str) -> str:
    # e.g. "Race 1  22/04, WED, 18:40, Class 5, 1200m, TURF, FLAMINGO FLOWER HANDICAP, "B" Course, GOOD"
    m = re.search(r"Race\s+\d+\s+(.*)", txt)
    return m.group(1).strip() if m else txt.strip()


_NUM_RE = re.compile(r"^\d+(\.\d+)?$")


def _extract_from_text(body_text: str) -> tuple[str, str, list[dict]]:
    """Parse the Win/Place table out of the rendered body text.

    The HKJC WPQ panel text looks like:
        No. Banker Sel. Horse Name Win Place
        1
                HAPPY ALLIANCE
        40
        10
        2
                GIDDY UP
        9.4
        4.4
        ...
        F
        Field
    Returns (last_update, race_info, rows)."""
    lines = [l.strip() for l in body_text.splitlines()]

    last_update = ""
    race_info = ""
    for ln in lines:
        if not last_update and ln.startswith("Last Update"):
            last_update = ln
        if not race_info:
            m = re.match(r"(Race\s+\d+)", ln)
            # race_info line actually follows the "Race N" header; combine if found
        # race_info proper: line starting with DD/MM, WED, etc.
        if not race_info and re.match(r"\d{2}/\d{2},\s+\w+,\s+\d{2}:\d{2}", ln):
            race_info = ln

    # Find the WPQ header and scan forward
    try:
        start = next(i for i, ln in enumerate(lines)
                     if ln.startswith("No.") and "Horse Name" in ln and "Win" in ln)
    except StopIteration:
        return last_update, race_info, []

    rows: list[dict] = []
    i = start + 1
    while i < len(lines):
        ln = lines[i]
        # Stop at end-of-table markers
        if ln in ("Add", "Investment Calculator") or ln.startswith("Total "):
            break
        if ln == "F" or ln.startswith("Field"):
            break
        if ln.isdigit() and 1 <= int(ln) <= 20:
            no = ln
            # Walk forward collecting horse name + 2 numeric odds
            horse = ""
            win = ""
            place = ""
            j = i + 1
            nums: list[str] = []
            while j < len(lines) and len(nums) < 2:
                tok = lines[j]
                if not tok:
                    j += 1
                    continue
                # Next horse-row number -> stop (shouldn't happen before 2 nums, but guard)
                if tok.isdigit() and 1 <= int(tok) <= 20 and len(nums) == 0 and not horse:
                    # Might be a spurious numeric; but normal flow has horse name first.
                    break
                if _NUM_RE.match(tok):
                    nums.append(tok)
                elif re.search(r"[A-Za-z]", tok) and tok not in ("Banker", "Sel."):
                    if not horse:
                        horse = tok
                j += 1
            if len(nums) >= 2:
                win, place = nums[0], nums[1]
            rows.append({"no": no, "horse": horse, "win": win, "place": place})
            i = j
            continue
        i += 1
    return last_update, race_info, rows


def _extract_odds_table(page) -> tuple[str, str, list[dict]]:
    """Returns (last_update, race_info, rows)."""
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        body_text = ""
    return _extract_from_text(body_text)


# ─── Quinella / Quinella-Place matrix extraction ─────────────────────────────
# HKJC renders QIN/QPL as a triangular matrix when the user is logged in;
# HKJC's /wpq/ page renders QIN and QPL pair-odds in an L-shaped layout that
# packs a triangular matrix into a roughly-square HTML table. Each leaf table
# (one for QIN, one for QPL) has:
#   row 0  = column labels for the upper-right rectangle (horses 2..14)
#   col 0  = row labels for the lower-left triangle (horses 9..14)
#   diag   = column labels for the lower-left triangle (horses 8..14)
#   col -1 = row labels for the upper-right rectangle (horses 1..7)
# We rebuild the grid from cell bounding-box positions (cx, cy) clustered into
# row/column buckets, then read odds out of every non-label cell.
_MATRIX_JS = r"""
() => {
  const all = Array.from(document.querySelectorAll('table'));
  const leaves = all.filter(t => t.querySelectorAll('table').length === 0);
  const matrices = [];
  for (const t of leaves) {
    const txt = (t.innerText || '');
    if (!/Quinella/i.test(txt)) continue;
    const head = txt.trim().split(/\n+/)[0].trim();
    const label = /^Quinella Place/i.test(head) ? 'qpl' :
                  (/^Quinella$/i.test(head) ? 'qin' : null);
    if (!label) continue;

    const cells = [];
    for (const c of t.querySelectorAll('th,td')) {
      const v = (c.innerText || '').trim();
      const r = c.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) continue;
      cells.push({
        cx: Math.round(r.left + r.width / 2),
        cy: Math.round(r.top + r.height / 2),
        v: v,
      });
    }
    if (!cells.length) continue;

    const rowYs = [];
    for (const c of cells) {
      if (!rowYs.some(y => Math.abs(y - c.cy) < 8)) rowYs.push(c.cy);
    }
    rowYs.sort((a, b) => a - b);
    const colXs = [];
    for (const c of cells) {
      if (!colXs.some(x => Math.abs(x - c.cx) < 8)) colXs.push(c.cx);
    }
    colXs.sort((a, b) => a - b);
    const nR = rowYs.length, nC = colXs.length;
    const rowOf = cy => {
      for (let i = 0; i < nR; i++) if (Math.abs(rowYs[i] - cy) < 8) return i;
      return -1;
    };
    const colOf = cx => {
      for (let i = 0; i < nC; i++) if (Math.abs(colXs[i] - cx) < 8) return i;
      return -1;
    };
    const grid = Array.from({length: nR}, () => Array(nC).fill(''));
    for (const c of cells) {
      const r = rowOf(c.cy), col = colOf(c.cx);
      if (r >= 0 && col >= 0 && grid[r][col] === '') grid[r][col] = c.v;
    }

    const headerMap = {};
    for (let col = 0; col < nC; col++) {
      const v = grid[0][col];
      if (/^\d+$/.test(v) && +v >= 1 && +v <= 20) headerMap[col] = +v;
    }
    const upperRow = {};
    for (let r = 1; r < nR; r++) {
      const v = grid[r][nC - 1];
      if (/^\d+$/.test(v) && +v >= 1 && +v <= 20) upperRow[r] = +v;
    }
    const lowerRow = {};
    for (let r = 1; r < nR; r++) {
      const v = grid[r][0];
      if (/^\d+$/.test(v) && +v >= 1 && +v <= 20) lowerRow[r] = +v;
    }
    const lowerCol = {};
    for (let i = 1; i < Math.min(nR, nC); i++) {
      const v = grid[i][i];
      if (/^\d+$/.test(v) && +v >= 1 && +v <= 20) lowerCol[i] = +v;
    }

    const labelCells = new Set();
    for (const col in headerMap)  labelCells.add(`0,${col}`);
    for (const r   in upperRow)   labelCells.add(`${r},${nC - 1}`);
    for (const r   in lowerRow)   labelCells.add(`${r},0`);
    for (const i   in lowerCol)   labelCells.add(`${i},${i}`);
    const upperDiagColOf = {};
    for (let r = 1; r < nR; r++) {
      if (!(r in upperRow)) continue;
      for (let cc = 0; cc < nC - 1; cc++) {
        if (grid[r][cc] === String(upperRow[r]) && !labelCells.has(`${r},${cc}`)) {
          upperDiagColOf[r] = cc;
          labelCells.add(`${r},${cc}`);
          break;
        }
      }
    }

    const pairs = {};
    for (let r = 1; r < nR; r++) {
      const upperDiagCol = upperDiagColOf[r] !== undefined ? upperDiagColOf[r] : -1;
      for (let col = 0; col < nC; col++) {
        if (labelCells.has(`${r},${col}`)) continue;
        const v = grid[r][col];
        if (!/^\d+(\.\d+)?$/.test(v)) continue;
        const n = +v;
        if (n <= 0 || n > 9999) continue;

        let rowH = null, colH = null;
        if (upperDiagCol >= 0 && col > upperDiagCol && (col in headerMap)) {
          rowH = upperRow[r];
          colH = headerMap[col];
        } else if ((r in lowerRow) && (col in lowerCol) && col < r) {
          rowH = lowerRow[r];
          colH = lowerCol[col];
        }
        if (rowH === null || colH === null || rowH === colH) continue;
        const lo = Math.min(rowH, colH), hi = Math.max(rowH, colH);
        const k = lo + '-' + hi;
        if (!(k in pairs)) pairs[k] = {a: String(lo), b: String(hi), odds: v};
      }
    }
    if (Object.keys(pairs).length >= 5) {
      if (!matrices.some(m => m.label === label)) {
        matrices.push({label, pairs: Object.values(pairs)});
      }
    }
  }
  return matrices;
}
"""


def _extract_all_matrices(page) -> dict:
    """Extract QIN + QPL pair-odds matrices from the current /wpq/ DOM.

    Returns {"qin": [...pairs], "qpl": [...pairs]}. Either may be empty
    if the matrix didn't render in time.
    """
    try:
        matrices = page.evaluate(_MATRIX_JS) or []
    except Exception:
        matrices = []
    out = {"qin": [], "qpl": []}
    for m in matrices:
        lab = m.get("label")
        if lab in out and not out[lab]:
            out[lab] = m.get("pairs") or []
    return out


def scrape_race(page, date_iso: str, venue: str, race_no: int,
                pools: set[str]) -> dict:
    """Scrape requested pools for one race from the public /wpq/ page.
    `pools` is a subset of {"wp", "qin", "qpl"}. All three are extracted
    from the SAME page render — the matrix tables are visible to anonymous
    users on /wpq/ even though /qin/ and /qpl/ require login.
    """
    snap = {
        "scraped_at": dt.datetime.now().isoformat(timespec="seconds"),
        "date": date_iso,
        "venue": venue,
        "race_no": race_no,
    }
    url = f"https://bet.hkjc.com/en/racing/wpq/{date_iso}/{venue}/{race_no}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception:
        page.goto(url, timeout=30_000)
    try:
        page.wait_for_selector("text=Horse Name", timeout=15_000)
    except Exception:
        pass
    # The QIN/QPL pair-odds matrices render slightly after the WP table.
    # Wait for at least one cell containing 'Quinella Place' header text.
    try:
        page.wait_for_selector("text=Quinella Place", timeout=10_000)
    except Exception:
        pass
    page.wait_for_timeout(2500)

    # 1) Win/Place + race info
    if "wp" in pools:
        last_update, race_info, odds = _extract_odds_table(page)
        snap.update({
            "url": url, "last_update": last_update,
            "race_info": race_info, "n_runners": len(odds), "odds": odds,
        })

    # 2) QIN + QPL matrices (extracted from the same /wpq/ DOM)
    if "qin" in pools or "qpl" in pools:
        try:
            mats = _extract_all_matrices(page)
        except Exception as e:
            mats = {"qin": [], "qpl": []}
            snap["matrix_error"] = str(e)
        if "qin" in pools:
            snap["qin_odds"] = mats.get("qin", [])
        if "qpl" in pools:
            snap["qpl_odds"] = mats.get("qpl", [])

    return snap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--venue", default="HV", choices=["HV", "ST"])
    ap.add_argument("--races", default="1-11",
                    help="Race range e.g. 1-11 or 1,2,3 (default: 1-11)")
    ap.add_argument("--pools", default="wp,qin,qpl",
                    help=("Comma-sep subset of wp,qin,qpl "
                          "(default: wp,qin,qpl — all extracted from the "
                          "single /wpq/ page render)."))
    ap.add_argument("--headless", action="store_true", default=True)
    args = ap.parse_args()

    date_iso = args.date or dt.date.today().isoformat()
    ymd = date_iso.replace("-", "")
    pools = {p.strip().lower() for p in args.pools.split(",") if p.strip()}
    valid = {"wp", "qin", "qpl"}
    unknown = pools - valid
    if unknown:
        print(f"Unknown pools: {unknown}. Valid: {valid}", file=sys.stderr)
        sys.exit(2)
    if not pools:
        pools = valid

    # Parse race list
    races: list[int] = []
    for part in args.races.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            races.extend(range(int(a), int(b) + 1))
        else:
            races.append(int(part))

    out_dir = OUT_ROOT / ymd
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%H%M%S")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"),
        )
        page = context.new_page()

        for rn in races:
            print(f"[{args.venue}] R{rn} ...", end=" ", flush=True)
            try:
                snap = scrape_race(page, date_iso, args.venue, rn, pools)
                outf = out_dir / f"{args.venue}_R{rn:02d}_{ts}.json"
                outf.write_text(json.dumps(snap, indent=2, ensure_ascii=False),
                                encoding="utf-8")
                parts = []
                if "odds" in snap:
                    parts.append(f"WP={snap.get('n_runners', 0)}")
                if "qin_odds" in snap:
                    parts.append(f"QIN={len(snap['qin_odds'])}")
                if "qpl_odds" in snap:
                    parts.append(f"QPL={len(snap['qpl_odds'])}")
                print(f"{' '.join(parts)} → {outf.name}")
            except Exception as e:
                print(f"ERR {e}")

        browser.close()


if __name__ == "__main__":
    main()
