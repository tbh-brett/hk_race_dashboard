#!/usr/bin/env python3
"""
scrape_hkjc_dividends.py
=========================
Scrape HKJC post-race dividend table (WIN/PLACE/QIN/QPL/FCT/TCE/TRIO/F4/QTT)
from the same localresults page that scrape_hkjc_results.py uses.

Output: reports/dividends_YYYYMMDD.json

Dividends on the HKJC page are published per HK$10 bet — we store the raw
figure; downstream code divides by 10 to get the multiplier on a $1 stake.

Usage:
    python scrape_hkjc_dividends.py --date 2026-04-19
    python scrape_hkjc_dividends.py --month 2026-04        # all meetings

"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
REPORTS_DIR = BASE_DIR / "reports"

BASE_URL = "https://racing.hkjc.com"
LOCALRESULTS_URL = f"{BASE_URL}/en-us/local/information/localresults"
RESULTSALL_URL = f"{BASE_URL}/en-us/local/information/resultsall"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

POOLS = [
    "WIN", "PLACE", "QUINELLA", "QUINELLA PLACE", "FORECAST",
    "TIERCE", "TRIO", "FIRST 4", "QUARTET", "DOUBLE", "TREBLE",
    "ALL UP", "JOCKEY CHALLENGE",
]
POOL_ALIASES = {
    "WIN": "WIN",
    "PLACE": "PLACE",
    "QUINELLA": "QIN",
    "QUINELLA PLACE": "QPL",
    "FORECAST": "FCT",
    "TIERCE": "TCE",
    "TRIO": "TRIO",
    "FIRST 4": "F4",
    "QUARTET": "QTT",
    "DOUBLE": "DBL",
    "TREBLE": "TBL",
}


def fetch_html(session: requests.Session, url: str,
               params: Optional[Dict] = None, retries: int = 3) -> str:
    for i in range(1, retries + 1):
        try:
            r = session.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"  warn attempt {i}: {e}")
            time.sleep(1.3 * i)
    return ""


def discover_races(session: requests.Session, date_iso: str):
    """Returns (venue, [race_nums])."""
    date_q = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%Y/%m/%d")
    html = fetch_html(session, RESULTSALL_URL, {"racedate": date_q})
    if not html:
        return "", []
    soup = BeautifulSoup(html, "html.parser")
    venue, nums = "", set()
    for link in soup.select("a[href*='localresults?racedate=']"):
        m = re.search(r"Racecourse=([A-Z]{2})&RaceNo=(\d+)",
                      link.get("href", ""))
        if m and m.group(1) in ("ST", "HV"):
            venue = m.group(1)
            nums.add(int(m.group(2)))
    return venue, sorted(nums)


def _clean_num(s: str) -> Optional[float]:
    s = s.strip().replace(",", "")
    if not s or s.upper() in ("", "N/A", "---", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_dividends(html: str) -> List[Dict]:
    """
    Parse the dividend table from a localresults page.

    Returns list of dicts: {"pool": "QIN", "combination": "1,4",
    "dividend_per_10": 96.50}.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Find table whose text contains "WIN" and "QUINELLA"
    target = None
    for tbl in soup.find_all("table"):
        txt = tbl.get_text(" ", strip=True).upper()
        if "WIN" in txt and "QUINELLA" in txt and "TRIO" in txt:
            target = tbl
            break
    if target is None:
        return []

    out: List[Dict] = []
    current_pool = None
    for tr in target.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if not cells:
            continue
        # Skip header rows
        first = cells[0].upper().strip()
        # Row formats on HKJC:
        # [POOL_LABEL, COMBINATION, DIVIDEND]   e.g. ["WIN", "1", "52.50"]
        # [COMBINATION, DIVIDEND]                e.g. ["4", "15.00"] (continuation row for PLACE)
        # Sometimes a row like ["QUINELLA PLACE", "1,4", "43.50"] opens new pool.
        label_match = None
        # Sort by length desc so "QUINELLA PLACE" beats "QUINELLA"
        for name in sorted(POOLS, key=len, reverse=True):
            if first.startswith(name):
                label_match = name
                break
        if label_match:
            current_pool = POOL_ALIASES.get(label_match, label_match)
            if len(cells) >= 3:
                combo = cells[1]
                div = _clean_num(cells[2])
                if combo and div is not None:
                    out.append({"pool": current_pool, "combination": combo,
                                "dividend_per_10": div})
        else:
            # Continuation row — reuse current_pool
            if current_pool and len(cells) >= 2:
                combo = cells[0]
                div = _clean_num(cells[1])
                if combo and div is not None and combo.upper() != "COMBINATION":
                    out.append({"pool": current_pool, "combination": combo,
                                "dividend_per_10": div})
    return out


def scrape_race_dividends(session: requests.Session, date_iso: str,
                          venue: str, race_no: int) -> Dict:
    date_q = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%Y/%m/%d")
    params = {"racedate": date_q, "Racecourse": venue, "RaceNo": str(race_no)}
    html = fetch_html(session, LOCALRESULTS_URL, params)
    if not html:
        return {"race_number": race_no, "dividends": []}
    divs = parse_dividends(html)
    return {"race_number": race_no, "dividends": divs}


def scrape_meeting(date_iso: str, venue_override: Optional[str] = None) -> Dict:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    v, nums = discover_races(session, date_iso)
    venue = (venue_override or v or "ST").upper()
    if not nums:
        print(f"No races for {date_iso}")
        return {}
    print(f"Meeting: {date_iso} | {venue} | races: {nums}")
    races = []
    for rn in nums:
        print(f"  R{rn:>2d} dividends ... ", end="", flush=True)
        rec = scrape_race_dividends(session, date_iso, venue, rn)
        n = len(rec["dividends"])
        print(f"{n} rows")
        races.append(rec)
        time.sleep(0.3)
    out = {
        "date": date_iso, "venue": venue,
        "scraped_at": datetime.now().isoformat(),
        "n_races": len(races), "races": races,
    }
    fn = REPORTS_DIR / f"dividends_{date_iso.replace('-', '')}.json"
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"-> {fn.name}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--dates", help="comma-separated YYYY-MM-DD list")
    ap.add_argument("--venue")
    args = ap.parse_args()

    if args.dates:
        for d in args.dates.split(","):
            scrape_meeting(d.strip(), args.venue)
    elif args.date:
        scrape_meeting(args.date, args.venue)
    else:
        ap.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
