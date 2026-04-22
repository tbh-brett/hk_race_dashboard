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
# anonymous users see only the entry list (no matrix rendered as of 2026-04).
# We therefore require *strict* matrix shape before accepting the extraction:
#   - header row must be all-integers in 1..20 (no "Horse Name", "Draw", etc.)
#   - row-labels must be integers in 1..20 and distinct
#   - every odds cell must be a plausible pool odds value (<10000)
#   - at least 5 distinct horse labels to avoid false positives
_MATRIX_JS = r"""
() => {
  const HEADER_BAD = ['Horse', 'Jockey', 'Trainer', 'Draw', 'Wt', 'Gear',
                      'Last', 'Body', 'Rtg', 'Colour', 'No.', 'T/P'];
  const tables = Array.from(document.querySelectorAll('table'));
  for (const t of tables) {
    const rows = Array.from(t.querySelectorAll('tr'));
    if (rows.length < 6) continue;
    const txt = t.innerText;
    if (HEADER_BAD.some(w => txt.includes(w))) continue;  // entry-list table
    // Find header row: ALL cells must be integers 1..20 (allow empty corner cell)
    let headerRow = -1;
    let headers = [];
    for (let r = 0; r < Math.min(3, rows.length); r++) {
      const cells = Array.from(rows[r].querySelectorAll('th,td'))
        .map(c => c.innerText.trim());
      const nonEmpty = cells.filter(x => x !== '');
      const allInt = nonEmpty.every(x => /^\d+$/.test(x) && +x >= 1 && +x <= 20);
      if (nonEmpty.length >= 4 && allInt) {
        headerRow = r; headers = cells; break;
      }
    }
    if (headerRow < 0) continue;
    const out = [];
    const rowLabels = new Set();
    for (let r = headerRow + 1; r < rows.length; r++) {
      const cells = Array.from(rows[r].querySelectorAll('th,td'))
        .map(c => c.innerText.trim());
      if (!cells.length) continue;
      const rowLabel = cells[0];
      if (!/^\d+$/.test(rowLabel) || +rowLabel > 20) continue;
      rowLabels.add(rowLabel);
      for (let c = 1; c < cells.length && c < headers.length; c++) {
        const v = cells[c];
        if (!v) continue;
        if (!/^\d+(\.\d+)?$/.test(v)) continue;
        if (+v > 9999) continue;
        const colLabel = headers[c];
        if (!/^\d+$/.test(colLabel) || +colLabel > 20) continue;
        if (rowLabel === colLabel) continue;
        const a = +rowLabel, b = +colLabel;
        const lo = Math.min(a, b), hi = Math.max(a, b);
        out.push({a: String(lo), b: String(hi), odds: v});
      }
    }
    if (rowLabels.size < 5) continue;
    const seen = new Map();
    for (const row of out) {
      const k = row.a + '-' + row.b;
      if (!seen.has(k)) seen.set(k, row);
    }
    const uniq = Array.from(seen.values());
    if (uniq.length >= 10) return uniq;  // need reasonable matrix density
  }
  return [];
}
"""


def _extract_matrix_odds(page) -> tuple[str, list[dict]]:
    """Extract pairwise odds from the current QIN / QPL page.

    Returns (last_update, [{"a", "b", "odds"}, ...]).
    """
    try:
        pairs = page.evaluate(_MATRIX_JS) or []
    except Exception:
        pairs = []
    last_update = ""
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
        for ln in body_text.splitlines():
            s = ln.strip()
            if s.startswith("Last Update"):
                last_update = s
                break
    except Exception:
        pass
    return last_update, pairs


def scrape_race(page, date_iso: str, venue: str, race_no: int,
                pools: set[str]) -> dict:
    """Scrape requested pools for one race. `pools` is a subset of
    {"wp", "qin", "qpl"}."""
    snap = {
        "scraped_at": dt.datetime.now().isoformat(timespec="seconds"),
        "date": date_iso,
        "venue": venue,
        "race_no": race_no,
    }

    # 1) Win/Place (always provides the race_info / n_runners anchor)
    if "wp" in pools:
        url = f"https://bet.hkjc.com/en/racing/wpq/{date_iso}/{venue}/{race_no}"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            page.goto(url, timeout=30_000)
        try:
            page.wait_for_selector("text=Horse Name", timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(1200)
        last_update, race_info, odds = _extract_odds_table(page)
        snap.update({
            "url": url, "last_update": last_update,
            "race_info": race_info, "n_runners": len(odds), "odds": odds,
        })

    # 2) Quinella matrix
    if "qin" in pools:
        qurl = f"https://bet.hkjc.com/en/racing/qin/{date_iso}/{venue}/{race_no}"
        try:
            try:
                page.goto(qurl, wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                page.goto(qurl, timeout=30_000)
            page.wait_for_timeout(1500)
            qin_lu, qin_pairs = _extract_matrix_odds(page)
        except Exception as e:
            qin_lu, qin_pairs = "", []
            snap["qin_error"] = str(e)
        snap["qin_url"] = qurl
        snap["qin_last_update"] = qin_lu
        snap["qin_odds"] = qin_pairs

    # 3) Quinella Place matrix
    if "qpl" in pools:
        qpurl = f"https://bet.hkjc.com/en/racing/qpl/{date_iso}/{venue}/{race_no}"
        try:
            try:
                page.goto(qpurl, wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                page.goto(qpurl, timeout=30_000)
            page.wait_for_timeout(1500)
            qpl_lu, qpl_pairs = _extract_matrix_odds(page)
        except Exception as e:
            qpl_lu, qpl_pairs = "", []
            snap["qpl_error"] = str(e)
        snap["qpl_url"] = qpurl
        snap["qpl_last_update"] = qpl_lu
        snap["qpl_odds"] = qpl_pairs

    return snap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--venue", default="HV", choices=["HV", "ST"])
    ap.add_argument("--races", default="1-11",
                    help="Race range e.g. 1-11 or 1,2,3 (default: 1-11)")
    ap.add_argument("--pools", default="wp",
                    help=("Comma-sep subset of wp,qin,qpl (default: wp). "
                          "QIN/QPL are experimental: bet.hkjc.com currently "
                          "requires login to render the pair-odds matrix "
                          "for anonymous users, so QIN/QPL snapshots may "
                          "be empty. Post-race QIN/QPL dividends are "
                          "available via scrape_hkjc_dividends.py."))
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
