#!/usr/bin/env python3
"""
HKJC Barrier Trial Results Scraper — scrape_hkjc_trials.py
============================================================
Scrapes barrier trial results from the HKJC website and outputs JSON.

Usage:
    python scrape_hkjc_trials.py --date 2026-04-10
    python scrape_hkjc_trials.py --from 2026-03-01
    python scrape_hkjc_trials.py --from 2026-03-01 --to 2026-04-10
    python scrape_hkjc_trials.py --list-dates
    python scrape_hkjc_trials.py --all

Output:
    reports/trials_YYYYMMDD.json (one file per trial date)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup, Tag

BASE_DIR = Path(__file__).parent
REPORTS_DIR = BASE_DIR / "reports"

BASE_URL = "https://racing.hkjc.com"
BTRESULT_URL = f"{BASE_URL}/en-us/local/information/btresult"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_html(session: requests.Session, url: str,
               params: Optional[Dict] = None) -> str:
    for attempt in range(1, 4):
        try:
            resp = session.get(url, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            print(f"  Warning: attempt {attempt} failed — {e}")
            time.sleep(1.5 * attempt)
    return ""


def iso_to_query(date_str: str) -> str:
    """YYYY-MM-DD → YYYY/MM/DD"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.strftime("%Y/%m/%d")


def _clean_whitespace(text: str) -> str:
    """Collapse multiple whitespace, strip nbsps."""
    return re.sub(r"[\xa0\s]+", " ", text).strip()


def _extract_horse_id(cell: Tag) -> str:
    """Extract horse ID from link href like horseid=HK_2025_L097."""
    link = cell.find("a")
    if link and link.get("href"):
        m = re.search(r"horseid=([^&]+)", link["href"])
        if m:
            return m.group(1)
    return ""


def _parse_horse_name(raw: str) -> Tuple[str, str]:
    """Parse 'HORSE NAME(CODE)' into (name, code)."""
    m = re.match(r"^(.+?)\(([A-Z0-9]+)\)$", raw.strip())
    if m:
        return m.group(1).strip(), m.group(2)
    return raw.strip(), ""


def _parse_lbw(raw: str) -> str:
    """Normalise LBW value."""
    val = raw.strip()
    if not val or val == "-":
        return "-"
    return val


def _parse_positions(raw: str) -> List[int]:
    """Parse '3 3 3 1' into [3, 3, 3, 1]."""
    parts = raw.strip().split()
    result = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError:
            pass
    return result


# ── Batch parsing ─────────────────────────────────────────────────────────────

def _parse_batch_header(header_table: Tag) -> Dict:
    """Parse batch header table: 'Batch 1 - CONGHUA TURF - 1600m'."""
    text = _clean_whitespace(header_table.get_text())
    result = {"batch_number": 0, "course": "", "distance_m": 0}

    # Batch number
    m = re.search(r"Batch\s+(\d+)", text)
    if m:
        result["batch_number"] = int(m.group(1))

    # Course and distance: "- CONGHUA TURF - 1200m"
    m = re.search(r"-\s*(.+?)\s*-\s*(\d+)\s*m", text)
    if m:
        result["course"] = m.group(1).strip()
        result["distance_m"] = int(m.group(2))

    return result


def _parse_batch_meta(meta_table: Tag) -> Dict:
    """Parse going/time/sectional table."""
    rows = meta_table.find_all("tr")
    result = {"going": "", "overall_time": "", "sectional_times": []}

    if rows:
        row0_text = _clean_whitespace(rows[0].get_text())
        # Going: GOOD
        m = re.search(r"Going:\s*(\S+)", row0_text)
        if m:
            result["going"] = m.group(1)
        # Time: 1.40.60
        m = re.search(r"Time:\s*([\d.]+)", row0_text)
        if m:
            result["overall_time"] = m.group(1)

    if len(rows) > 1:
        sect_text = _clean_whitespace(rows[1].get_text())
        m = re.search(r"Sectional\s+Time:\s*(.+)", sect_text)
        if m:
            result["sectional_times"] = m.group(1).strip().split()

    return result


def _parse_data_table(data_table: Tag) -> List[Dict]:
    """Parse the horse data table (class='bigborder')."""
    rows = data_table.find_all("tr")
    if len(rows) < 2:
        return []

    horses = []
    for row in rows[1:]:  # skip header row
        cells = row.find_all("td")
        if len(cells) < 10:
            continue

        raw_name = _clean_whitespace(cells[0].get_text())
        horse_name, horse_code = _parse_horse_name(raw_name)
        horse_id = _extract_horse_id(cells[0])

        horses.append({
            "horse_name": horse_name,
            "horse_code": horse_code,
            "horse_id": horse_id,
            "jockey": _clean_whitespace(cells[1].get_text()),
            "trainer": _clean_whitespace(cells[2].get_text()),
            "draw": int(cells[3].get_text(strip=True)) if cells[3].get_text(strip=True).isdigit() else 0,
            "gear": _clean_whitespace(cells[4].get_text()),
            "lbw": _parse_lbw(cells[5].get_text(strip=True)),
            "running_positions": _parse_positions(cells[6].get_text()),
            "time": _clean_whitespace(cells[7].get_text()),
            "result": _clean_whitespace(cells[8].get_text()),
            "comment": _clean_whitespace(cells[9].get_text()),
        })

    return horses


# ── Main scraper ──────────────────────────────────────────────────────────────

def scrape_trial_day(date_str: str) -> Optional[Dict]:
    """Scrape all barrier trial batches for a given date.
    
    Args:
        date_str: Date in YYYY-MM-DD format.
    
    Returns:
        Dict with trial data, or None if no data found.
    """
    session = requests.Session()
    query_date = iso_to_query(date_str)
    print(f"Fetching barrier trial results for {date_str} ...")

    html = fetch_html(session, BTRESULT_URL, {"Date": query_date})
    if not html:
        print("  ERROR: failed to fetch page")
        return None

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        print("  No tables found on page")
        return None

    # Identify data tables (class='bigborder') — these are the horse tables
    data_indices = [i for i, t in enumerate(tables)
                    if "bigborder" in (t.get("class") or [])]

    if not data_indices:
        print("  No barrier trial data tables found")
        return None

    batches = []
    for di in data_indices:
        # Header table is at di-2, meta table at di-1
        if di < 2:
            continue

        header_info = _parse_batch_header(tables[di - 2])
        meta_info = _parse_batch_meta(tables[di - 1])
        horses = _parse_data_table(tables[di])

        if not horses:
            continue

        batch = {
            **header_info,
            **meta_info,
            "n_horses": len(horses),
            "horses": horses,
        }
        batches.append(batch)
        print(f"  Batch {batch['batch_number']}: {batch['course']} "
              f"{batch['distance_m']}m — {len(horses)} horses")

    if not batches:
        print("  No valid batches parsed")
        return None

    total_horses = sum(b["n_horses"] for b in batches)
    print(f"  Total: {len(batches)} batches, {total_horses} horses")

    return {
        "date": date_str,
        "scraped_at": datetime.now().isoformat(),
        "n_batches": len(batches),
        "n_horses": total_horses,
        "batches": batches,
    }


# ── Date discovery ────────────────────────────────────────────────────────────

def discover_trial_dates(session: Optional[requests.Session] = None) -> List[str]:
    """Fetch available trial dates from the HKJC dropdown.

    Returns list of dates in YYYY-MM-DD format, newest first.
    """
    if session is None:
        session = requests.Session()
    html = fetch_html(session, BTRESULT_URL)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", id="selectId")
    if not select:
        return []

    dates = []
    for opt in select.find_all("option"):
        val = opt.get("value", "").strip()
        if not val:
            continue
        try:
            dt = datetime.strptime(val, "%d/%m/%Y")
            dates.append(dt.strftime("%Y-%m-%d"))
        except ValueError:
            continue
    return dates


# ── CLI ───────────────────────────────────────────────────────────────────────

def _save_result(result: Dict, output: Optional[str] = None) -> Path:
    """Save a single trial day result to JSON."""
    if output:
        out_path = Path(output)
    else:
        REPORTS_DIR.mkdir(exist_ok=True)
        date_compact = result["date"].replace("-", "")
        out_path = REPORTS_DIR / f"trials_{date_compact}.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="Scrape HKJC barrier trial results")
    parser.add_argument("--date",
                        help="Single trial date (YYYY-MM-DD)")
    parser.add_argument("--from", dest="from_date",
                        help="Scrape all trials from this date onwards (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date",
                        help="Scrape trials up to this date (YYYY-MM-DD, default: today)")
    parser.add_argument("--all", action="store_true",
                        help="Scrape ALL available trial dates")
    parser.add_argument("--list-dates", action="store_true",
                        help="List available trial dates and exit")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip dates that already have a JSON file in reports/")
    parser.add_argument("--output", default=None,
                        help="Output path (only for --date single mode)")
    parser.add_argument("--sleep", type=float, default=1.5,
                        help="Sleep between requests in bulk mode (default: 1.5s)")
    args = parser.parse_args()

    # ── List dates mode ───────────────────────────────────────────────────
    if args.list_dates:
        print("Discovering available trial dates ...")
        dates = discover_trial_dates()
        print(f"Found {len(dates)} trial dates:")
        for d in dates:
            print(f"  {d}")
        return

    # ── Single date mode ──────────────────────────────────────────────────
    if args.date and not args.from_date and not args.all:
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print(f"ERROR: Invalid date format '{args.date}'. Use YYYY-MM-DD.")
            sys.exit(1)

        result = scrape_trial_day(args.date)
        if result is None:
            print("No trial data found.")
            sys.exit(1)

        out_path = _save_result(result, args.output)
        print(f"Saved to {out_path}")
        return

    # ── Bulk mode (--from / --all) ────────────────────────────────────────
    print("Discovering available trial dates ...")
    all_dates = discover_trial_dates()
    if not all_dates:
        print("ERROR: Could not discover any trial dates.")
        sys.exit(1)
    print(f"  Found {len(all_dates)} total trial dates on HKJC")

    # Filter by date range
    if args.all:
        target_dates = all_dates
    elif args.from_date:
        try:
            from_dt = datetime.strptime(args.from_date, "%Y-%m-%d")
        except ValueError:
            print(f"ERROR: Invalid --from date '{args.from_date}'. Use YYYY-MM-DD.")
            sys.exit(1)

        to_dt = datetime.now()
        if args.to_date:
            try:
                to_dt = datetime.strptime(args.to_date, "%Y-%m-%d")
            except ValueError:
                print(f"ERROR: Invalid --to date '{args.to_date}'. Use YYYY-MM-DD.")
                sys.exit(1)

        target_dates = [
            d for d in all_dates
            if from_dt <= datetime.strptime(d, "%Y-%m-%d") <= to_dt
        ]
    else:
        parser.print_help()
        sys.exit(1)

    # Skip existing
    if args.skip_existing:
        existing = set()
        if REPORTS_DIR.exists():
            for f in REPORTS_DIR.glob("trials_*.json"):
                m = re.search(r"trials_(\d{8})\.json$", f.name)
                if m:
                    ds = m.group(1)
                    existing.add(f"{ds[:4]}-{ds[4:6]}-{ds[6:]}")
        before = len(target_dates)
        target_dates = [d for d in target_dates if d not in existing]
        if before != len(target_dates):
            print(f"  Skipping {before - len(target_dates)} already-scraped dates")

    # Sort oldest first for scraping
    target_dates.sort()

    print(f"\nScraping {len(target_dates)} trial dates ...")
    print(f"{'='*60}")

    success = 0
    failed = 0
    for i, d in enumerate(target_dates, 1):
        print(f"\n[{i}/{len(target_dates)}] {d}")
        result = scrape_trial_day(d)
        if result:
            out_path = _save_result(result)
            print(f"  → {out_path.name}")
            success += 1
        else:
            print(f"  → SKIPPED (no data)")
            failed += 1

        if i < len(target_dates):
            time.sleep(args.sleep)

    print(f"\n{'='*60}")
    print(f"Done: {success} scraped, {failed} failed/empty")


if __name__ == "__main__":
    main()
