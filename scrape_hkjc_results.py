#!/usr/bin/env python3
"""
HKJC Post-Race Results Scraper — scrape_hkjc_results.py
=========================================================
Lightweight wrapper around scrape_hkjc.py logic to fetch actual race results
for a single meeting and output a per-meeting JSON for backtesting.

Usage:
    python scrape_hkjc_results.py --date 2026-04-01
    python scrape_hkjc_results.py --date 2026-04-01 --venue ST

Output:
    reports/results_YYYYMMDD.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
REPORTS_DIR = BASE_DIR / "reports"

BASE_URL = "https://racing.hkjc.com"
LOCALRESULTS_URL = f"{BASE_URL}/en-us/local/information/localresults"
RESULTSALL_URL = f"{BASE_URL}/en-us/local/information/resultsall"
SECTIONAL_URL = f"{BASE_URL}/en-us/local/information/displaysectionaltime"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

DATE_FMT_UI = "%d/%m/%Y"
DATE_FMT_QUERY = "%Y/%m/%d"


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


def parse_time_seconds(value: str) -> Optional[float]:
    value = value.strip()
    if not value:
        return None
    if ":" in value:
        mins, secs = value.split(":", 1)
        try:
            return int(mins) * 60 + float(secs)
        except ValueError:
            return None
    try:
        return float(value)
    except ValueError:
        return None


def iso_to_ui(date_str: str) -> str:
    """YYYY-MM-DD → DD/MM/YYYY"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.strftime(DATE_FMT_UI)


def iso_to_query(date_str: str) -> str:
    """YYYY-MM-DD → YYYY/MM/DD"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.strftime(DATE_FMT_QUERY)


# ── Race discovery ────────────────────────────────────────────────────────────

def discover_races(session: requests.Session,
                   date_str: str) -> Tuple[str, List[int]]:
    """Get racecourse code and list of race numbers for a meeting date."""
    html = fetch_html(session, RESULTSALL_URL,
                      {"racedate": iso_to_query(date_str)})
    if not html:
        return "", []

    soup = BeautifulSoup(html, "lxml")
    race_links = soup.select("a[href*='localresults?racedate=']")
    venue = ""
    nums = set()
    for link in race_links:
        href = link.get("href", "")
        m = re.search(r"Racecourse=([A-Z]{2})&RaceNo=(\d+)", href)
        if m and m.group(1) in ("ST", "HV"):
            venue = m.group(1)
            nums.add(int(m.group(2)))
    return venue, sorted(nums)


# ── Result parsing ────────────────────────────────────────────────────────────

GOING_ABBREV = {
    "GOOD": "G", "GOOD TO FIRM": "GF", "GOOD TO YIELDING": "GY",
    "FIRM": "FT", "YIELDING": "Y", "SOFT": "SE",
    "YIELDING TO SOFT": "YS", "WET FAST": "WF", "WET SLOW": "WS",
    "HEAVY": "HV",
}


def parse_race_header(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    text = " ".join(soup.stripped_strings)
    info: Dict[str, str] = {}

    cm = re.search(r"Class\s*(\d+)\s*-\s*(\d+)M", text)
    if cm:
        info["race_class"] = cm.group(1)
        info["distance"] = cm.group(2)
    else:
        # Fallback for non-Class races: Griffin Race, Group N, Listed Race, etc.
        alt = re.search(r"(Griffin\s+Race|Group\s+\d+|Listed\s+Race)\s*-\s*(\d+)M", text, re.IGNORECASE)
        if alt:
            info["race_class"] = alt.group(1).strip()
            info["distance"] = alt.group(2)
        else:
            dist_only = re.search(r"-\s*(\d{3,4})M", text)
            if dist_only:
                info["distance"] = dist_only.group(1)

    gm = re.search(
        r"Going\s*:\s*([A-Z]+(?:\s+(?:TO|AND)\s+[A-Z]+)?(?:\s+(?:FAST|SLOW))?)",
        text)
    if gm:
        raw = gm.group(1).strip()
        info["going"] = GOING_ABBREV.get(raw.upper(), raw)

    nm = re.search(
        r"Going\s*:\s*(?:[A-Z]+(?:\s+(?:TO|AND)\s+[A-Z]+)?(?:\s+(?:FAST|SLOW))?)"
        r"\s+(.+?)\s+Course\s*:", text)
    if nm:
        info["race_name"] = nm.group(1).strip()

    course_m = re.search(r'Course\s*:\s*(.+?)(?:\s+Class|\s+Race|\s*$)', text)
    if course_m:
        raw_c = course_m.group(1).strip()
        if "ALL WEATHER" in raw_c.upper() or "AWT" in raw_c.upper():
            info["race_course"] = "AWT"
            info["is_awt"] = True
        else:
            vm = re.search(r'["\']?([A-C](?:\+\d)?)["\']?', raw_c)
            info["race_course"] = vm.group(1) if vm else raw_c
            info["is_awt"] = False
    return info


def parse_results_table(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.f_tac.table_bd.draggable")
    if not table:
        return []
    rows = []
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        if len(cells) < 12:
            continue
        rows.append({
            "place": cells[0].get_text(" ", strip=True),
            "horse_no": cells[1].get_text(" ", strip=True),
            "horse_name": cells[2].get_text(" ", strip=True).split("(")[0].strip(),
            "jockey": cells[3].get_text(" ", strip=True),
            "trainer": cells[4].get_text(" ", strip=True),
            "actual_weight": cells[5].get_text(" ", strip=True),
            "declared_weight": cells[6].get_text(" ", strip=True),
            "draw": cells[7].get_text(" ", strip=True),
            "lbw": cells[8].get_text(" ", strip=True),
            "running_position": cells[9].get_text(" ", strip=True),
            "finish_time": cells[10].get_text(" ", strip=True),
            "win_odds": cells[11].get_text(" ", strip=True),
        })
    return rows


def parse_sectional_table(html: str) -> Dict[str, Dict]:
    """Returns {horse_no: {positions: [...], sectiontimes: [...]}}."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.table_bd.f_tac.race_table")
    if not table:
        return {}

    results = {}
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        horse_no = tds[1].get_text(strip=True)
        if not horse_no:
            continue

        positions, sectiontimes = [], []
        for td in tds[3:9]:
            if td.find("img"):
                positions.append("")
                sectiontimes.append("")
                continue
            pos = td.select_one("span.f_fl")
            positions.append(pos.get_text(strip=True) if pos else "")
            sec_time = ""
            ps = td.find_all("p")
            if len(ps) >= 2:
                time_p = ps[1]
                for span in time_p.find_all("span"):
                    span.decompose()
                sec_time = time_p.get_text(strip=True)
            sectiontimes.append(sec_time)

        results[horse_no] = {
            "positions": positions,
            "sectiontimes": sectiontimes,
        }
    return results


# ── Scrape one race ──────────────────────────────────────────────────────────

def scrape_race(session: requests.Session, date_str: str,
                venue: str, race_no: int) -> Optional[Dict]:
    date_ui = iso_to_ui(date_str)
    date_q = iso_to_query(date_str)

    params = {"racedate": date_q, "Racecourse": venue, "RaceNo": str(race_no)}
    local_html = fetch_html(session, LOCALRESULTS_URL, params)
    if not local_html:
        return None

    header = parse_race_header(local_html)
    runners = parse_results_table(local_html)
    if not runners:
        return None

    sect_html = fetch_html(session, SECTIONAL_URL,
                           {"racedate": date_ui, "RaceNo": str(race_no)})
    sectionals = parse_sectional_table(sect_html) if sect_html else {}

    for r in runners:
        r["finish_time_seconds"] = parse_time_seconds(r["finish_time"])
        sect = sectionals.get(r["horse_no"], {})
        r["positions"] = sect.get("positions", [])
        r["sectiontimes"] = sect.get("sectiontimes", [])
        # Parse numeric fields
        try:
            r["horse_no"] = int(r["horse_no"])
        except (ValueError, TypeError):
            pass
        try:
            r["draw"] = int(r["draw"])
        except (ValueError, TypeError):
            r["draw"] = None
        try:
            r["actual_weight"] = int(r["actual_weight"])
        except (ValueError, TypeError):
            r["actual_weight"] = None

    return {
        "race_number": race_no,
        "race_name": header.get("race_name", ""),
        "distance": int(header.get("distance", 0) or 0) or None,
        "race_class": header.get("race_class", ""),
        "race_course": header.get("race_course", ""),
        "is_awt": header.get("is_awt", False),
        "going": header.get("going", ""),
        "runners": runners,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def scrape_meeting_results(date_str: str,
                           venue_override: Optional[str] = None) -> Dict:
    """Scrape full meeting results. Returns structured dict for JSON output."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    venue_discovered, race_nums = discover_races(session, date_str)
    venue = (venue_override or venue_discovered or "ST").upper()

    if not race_nums:
        print(f"No races found for {date_str} at {venue}")
        return {}

    print(f"Meeting: {date_str} | Venue: {venue} | Races: {race_nums}")

    races = []
    for rn in race_nums:
        print(f"  R{rn:>2d} ... ", end="", flush=True)
        race = scrape_race(session, date_str, venue, rn)
        if race:
            print(f"{len(race['runners'])} runners")
            races.append(race)
        else:
            print("no data")
        time.sleep(0.4)

    output = {
        "date": date_str,
        "venue": venue,
        "scraped_at": datetime.now().isoformat(),
        "n_races": len(races),
        "races": races,
    }

    date_compact = date_str.replace("-", "")
    out_path = REPORTS_DIR / f"results_{date_compact}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n✓ Results saved: {out_path.name}")
    print(f"  {len(races)} races, "
          f"{sum(len(r['runners']) for r in races)} total runners")

    return output


def main():
    parser = argparse.ArgumentParser(
        description="Scrape HKJC post-race results for backtesting.")
    parser.add_argument("--date", required=True,
                        help="Race date YYYY-MM-DD")
    parser.add_argument("--venue", default=None,
                        help="Racecourse override (ST or HV)")
    args = parser.parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(f"ERROR: Invalid date '{args.date}' — use YYYY-MM-DD")
        sys.exit(1)

    result = scrape_meeting_results(args.date, args.venue)
    if not result:
        sys.exit(1)


if __name__ == "__main__":
    main()
