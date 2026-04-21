"""
scrape_hkjc_live_odds.py
------------------------
Scrapes Win/Place + Quinella odds snapshots from https://bet.hkjc.com
for all races of a meeting. Uses Playwright because bet.hkjc.com is a
React SPA — the odds are not in the initial HTML.

Snapshots are stored as JSON per race:
    cache/live_odds/YYYYMMDD/{VENUE}_R{N}_{HHMMSS}.json

Each snapshot:
{
  "scraped_at":     ISO timestamp (local HK time)
  "last_update":    HKJC-reported "Last Update: DD/MM/YYYY HH:MM"
  "date":           "2026-04-22"
  "venue":          "HV" | "ST"
  "race_no":        1
  "race_info":      "Class 5, 1200m, TURF, ..."
  "odds": [
      {"no":"1", "horse":"HAPPY ALLIANCE", "win":"40", "place":"10"},
      ...
  ]
}

Usage:
    python scrape_hkjc_live_odds.py --date 2026-04-22 --venue HV
    python scrape_hkjc_live_odds.py --date 2026-04-22 --venue HV --races 1,2,3
    python scrape_hkjc_live_odds.py                      # today, auto-venue
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


def scrape_race(page, date_iso: str, venue: str, race_no: int) -> dict:
    url = f"https://bet.hkjc.com/en/racing/wpq/{date_iso}/{venue}/{race_no}"
    page.goto(url, wait_until="networkidle", timeout=30_000)
    # Wait for WPQ table to render (horse names appear)
    try:
        page.wait_for_selector("text=Horse Name", timeout=15_000)
    except Exception:
        pass
    page.wait_for_timeout(800)  # let odds settle

    last_update, race_info, odds = _extract_odds_table(page)

    return {
        "scraped_at": dt.datetime.now().isoformat(timespec="seconds"),
        "last_update": last_update,
        "url": url,
        "date": date_iso,
        "venue": venue,
        "race_no": race_no,
        "race_info": race_info,
        "n_runners": len(odds),
        "odds": odds,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--venue", default="HV", choices=["HV", "ST"])
    ap.add_argument("--races", default="1-11",
                    help="Race range e.g. 1-11 or 1,2,3 (default: 1-11)")
    ap.add_argument("--headless", action="store_true", default=True)
    args = ap.parse_args()

    date_iso = args.date or dt.date.today().isoformat()
    ymd = date_iso.replace("-", "")

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
                snap = scrape_race(page, date_iso, args.venue, rn)
                outf = out_dir / f"{args.venue}_R{rn:02d}_{ts}.json"
                outf.write_text(json.dumps(snap, indent=2, ensure_ascii=False),
                                encoding="utf-8")
                print(f"N={snap['n_runners']} → {outf.name}")
            except Exception as e:
                print(f"ERR {e}")

        browser.close()


if __name__ == "__main__":
    main()
