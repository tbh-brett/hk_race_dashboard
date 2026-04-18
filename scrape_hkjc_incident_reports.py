#!/usr/bin/env python3
"""
HKJC Racing Incident Report Scraper
====================================
Scrapes the "Racing Incident Report" + "Comments on Running" sections from the
HKJC localresults page for a given meeting.

Usage:
    python scrape_hkjc_incident_reports.py --date 2026-04-15
    python scrape_hkjc_incident_reports.py --dates 2026-04-01,2026-04-06,...
    python scrape_hkjc_incident_reports.py --from 2026-04-01 --to 2026-04-17

Output:
    reports/incidents_YYYYMMDD.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

BASE_URL = "https://racing.hkjc.com"
LOCALRESULTS_URL = f"{BASE_URL}/en-us/local/information/localresults"
CORUNNING_URL = f"{BASE_URL}/en-us/local/information/corunning"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

MAX_RACES_PROBE = 12
CONSECUTIVE_MISS_LIMIT = 3


def fetch_html(session: requests.Session, url: str, params: Dict) -> str:
    for attempt in range(1, 4):
        try:
            resp = session.get(url, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            print(f"  Warning: attempt {attempt} failed — {e}")
            time.sleep(1.5 * attempt)
    return ""


def parse_incident_report(html: str) -> List[Dict]:
    """Extract per-horse Racing Incident Report entries.

    The incident report table follows the heading 'Racing Incident Report'.
    Each row: Place | HorseNo | HorseName (Code) | incident text
    """
    soup = BeautifulSoup(html, "html.parser")
    # The real heading is <p class="bg_blue ...">Racing Incident Report</p> followed by <table>
    anchor = None
    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        classes = " ".join(p.get("class") or [])
        if txt.strip() == "Racing Incident Report" and "bg_blue" in classes:
            anchor = p
            break
    if anchor is None:
        # Fallback: any <p> whose text equals the phrase exactly
        for p in soup.find_all("p"):
            if p.get_text(" ", strip=True) == "Racing Incident Report":
                anchor = p
                break
    if anchor is None:
        return []
    table = anchor.find_next("table")
    if table is None:
        return []

    rows: List[Dict] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        place_raw = tds[0].get_text(strip=True)
        horse_no_raw = tds[1].get_text(strip=True)
        horse_cell = tds[2].get_text(" ", strip=True)
        incident = tds[3].get_text(" ", strip=True)
        # Horse name may be "ALL ROUND WINNER (K382)" — split off the code
        m = re.match(r"^(.*?)\s*\(([A-Z]\d+)\)\s*$", horse_cell)
        if m:
            name = m.group(1).strip().upper()
            code = m.group(2)
        else:
            name = horse_cell.strip().upper()
            code = ""
        try:
            place = int(place_raw)
        except ValueError:
            place = None
        try:
            horse_no = int(horse_no_raw)
        except ValueError:
            horse_no = None
        if not name:
            continue
        # Skip sentinel rows that are pure headers
        if name in {"RACING INCIDENT REPORT", "HORSE", "HORSE NAME"}:
            continue
        rows.append({
            "place": place,
            "horse_no": horse_no,
            "horse_name": name,
            "horse_code": code,
            "incident": incident,
        })
    return rows


def parse_corunning(html: str) -> List[Dict]:
    """Comments-on-running table from the corunning endpoint."""
    soup = BeautifulSoup(html, "html.parser")
    rows: List[Dict] = []
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if not trs:
            continue
        # Quick heuristic: first row has header with "Horse" and "Comments"
        head_txt = " ".join(th.get_text(" ", strip=True)
                            for th in trs[0].find_all(["th", "td"])).lower()
        if "horse" in head_txt and ("comment" in head_txt or "running" in head_txt):
            for tr in trs[1:]:
                tds = tr.find_all("td")
                if len(tds) < 3:
                    continue
                horse_no = tds[0].get_text(strip=True)
                horse_cell = tds[1].get_text(" ", strip=True)
                comment = tds[2].get_text(" ", strip=True)
                m = re.match(r"^(.*?)\s*\(([A-Z]\d+)\)\s*$", horse_cell)
                name = (m.group(1) if m else horse_cell).strip().upper()
                rows.append({
                    "horse_no": int(horse_no) if horse_no.isdigit() else None,
                    "horse_name": name,
                    "comment": comment,
                })
            break
    return rows


def detect_venue(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    txt = soup.get_text(" ", strip=True)
    if "Happy Valley" in txt:
        return "HV"
    if "Sha Tin" in txt:
        return "ST"
    return ""


def scrape_race(session: requests.Session, date_iso: str,
                venue: str, race_no: int) -> Optional[Dict]:
    date_q = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%Y/%m/%d")
    params = {"racedate": date_q, "Racecourse": venue, "RaceNo": str(race_no)}
    html = fetch_html(session, LOCALRESULTS_URL, params)
    if not html:
        return None
    heading_pat = re.compile(
        r'<p class="bg_blue[^"]*"[^>]*>\s*Racing Incident Report\s*</p>', re.I)
    if not heading_pat.search(html):
        return None
    incidents = parse_incident_report(html)
    # Also fetch corunning (comments-on-running) for richer per-segment notes
    corun_params = {"Date": date_iso.replace("-", ""), "RaceNo": str(race_no)}
    corun_html = fetch_html(session, CORUNNING_URL, corun_params)
    comments = parse_corunning(corun_html) if corun_html else []
    return {
        "race_number": race_no,
        "incident_report": incidents,
        "comments_on_running": comments,
    }


def discover_and_scrape(date_iso: str) -> Optional[Dict]:
    sess = requests.Session()
    # Probe venue via R1 — look for the *actual* incident-report heading
    # (<p class="bg_blue">Racing Incident Report</p>), not the nav-menu link.
    date_q = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%Y/%m/%d")
    venue = ""
    heading_pat = re.compile(
        r'<p class="bg_blue[^"]*"[^>]*>\s*Racing Incident Report\s*</p>', re.I)
    for try_venue in ("HV", "ST"):
        html = fetch_html(sess, LOCALRESULTS_URL,
                          {"racedate": date_q, "Racecourse": try_venue, "RaceNo": "1"})
        if html and heading_pat.search(html):
            venue = try_venue
            break
    if not venue:
        print(f"[{date_iso}] no meeting detected")
        return None

    races: List[Dict] = []
    misses = 0
    for rn in range(1, MAX_RACES_PROBE + 1):
        race = scrape_race(sess, date_iso, venue, rn)
        if race is None:
            misses += 1
            if misses >= CONSECUTIVE_MISS_LIMIT:
                break
            continue
        misses = 0
        races.append(race)
        n_inc = len(race["incident_report"])
        n_cmt = len(race["comments_on_running"])
        print(f"  R{rn:>2d}: {n_inc} incident rows · {n_cmt} running comments")
        time.sleep(0.25)

    return {
        "date": date_iso,
        "venue": venue,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "n_races": len(races),
        "races": races,
    }


def save(meeting: Dict) -> Path:
    dc = meeting["date"].replace("-", "")
    path = REPORTS_DIR / f"incidents_{dc}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meeting, f, ensure_ascii=False, indent=2)
    print(f"  Saved → {path.name}")
    return path


def date_range(d_from: str, d_to: str) -> List[str]:
    a = datetime.strptime(d_from, "%Y-%m-%d")
    b = datetime.strptime(d_to, "%Y-%m-%d")
    return [(a + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range((b - a).days + 1)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date")
    p.add_argument("--dates")
    p.add_argument("--from", dest="d_from")
    p.add_argument("--to", dest="d_to")
    args = p.parse_args()

    dates: List[str] = []
    if args.date:
        dates.append(args.date)
    if args.dates:
        dates.extend([s.strip() for s in args.dates.split(",") if s.strip()])
    if args.d_from and args.d_to:
        dates.extend(date_range(args.d_from, args.d_to))
    if not dates:
        p.error("provide --date / --dates / --from+--to")

    for d in dates:
        print(f"[{d}]")
        meeting = discover_and_scrape(d)
        if meeting and meeting["races"]:
            save(meeting)


if __name__ == "__main__":
    main()
