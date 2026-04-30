#!/usr/bin/env python3
"""
HKJC Race Card Scraper — scrape_hkjc_racecard.py
==================================================
Extracts race card data from https://racing.hkjc.com/en-us/local/information/racecard
and exports to structured Excel files.

Usage:
    python scrape_hkjc_racecard.py --date 2026-04-01
    python scrape_hkjc_racecard.py --date 2026-04-01 --no-cache --output racecard_20260401.xlsx

Data source: the HKJC "My Race Card" table (id=racecardlist) which is server-rendered
HTML containing 27 columns per horse row.  The companion "starter" table (class=starter)
holds the same data.  Stand-by starters are in table id=standbylist.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from bs4 import BeautifulSoup, Tag

from hkjc_client import (
    BASE_URL, RACECARD_URL, HEADERS,
    fetch_html as _fetch_html, safe_excel_write,
    safe_json_read, safe_json_write,
)

# Fixed column indices for the HKJC "My Race Card" / "starter" table (27 cols)
# Verified empirically against live HTML as of March 2026.
COL = {
    "horse_no": 0,
    "last_6": 1,
    "colour": 2,
    "horse_name": 3,
    "brand_no": 4,
    "weight": 5,
    "jockey": 6,
    "overweight": 7,
    "draw": 8,
    "trainer": 9,
    "intl_rating": 10,
    "rating": 11,
    "rating_change": 12,
    "horse_wt_decl": 13,
    "wt_change": 14,
    "best_time": 15,
    "age": 16,
    "wfa": 17,
    "sex": 18,
    "season_stakes": 19,
    "priority": 20,
    "days_since_last": 21,
    "gear": 22,
    "owner": 23,
    "sire": 24,
    "dam": 25,
    "import_cat": 26,
}

DEFAULT_CACHE_DIR = Path(__file__).parent / "cache"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("racecard")


# ══════════════════════════════════════════════════════════════════════════════
# 1. Network — fetch_page with retry
# ══════════════════════════════════════════════════════════════════════════════

def fetch_page(url: str, session: requests.Session, params: Optional[dict] = None,
               retries: int = 3, backoff: float = 2.0) -> Optional[str]:
    """Fetch a URL with retry logic and exponential backoff.

    Returns the HTML text, or None on failure.
    """
    html = _fetch_html(session, url, params=params, retries=retries, backoff=backoff, quiet=True)
    if not html:
        log.error("All %d attempts failed for %s", retries, url)
        return None
    return html


def _ensure_playwright_browser() -> bool:
    """Install Playwright Chromium if not already available. Returns True on success."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # Try to launch — will fail fast if browser isn't installed
            b = p.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:
        log.info("Installing Playwright Chromium browser …")
        try:
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=True, capture_output=True, timeout=120,
            )
            return True
        except Exception as exc:
            log.warning("Playwright browser install failed: %s", exc)
            return False


def fetch_page_js(url: str, params: Optional[dict] = None,
                  timeout: int = 30000) -> Optional[str]:
    """Fetch a page using Playwright (headless Chromium) for JS-rendered content.

    Returns the fully-rendered HTML, or None on failure.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright not installed — cannot render JS pages")
        return None

    if not _ensure_playwright_browser():
        return None

    # Build full URL with query params
    if params:
        from urllib.parse import urlencode
        full_url = f"{url}?{urlencode(params)}"
    else:
        full_url = url

    log.info("  Fetching via Playwright: %s", full_url[:120])
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(full_url, wait_until="networkidle", timeout=timeout)
            # Wait for table content to appear (up to 10s)
            try:
                page.wait_for_selector(
                    "table.starter, table#racecardlist, table.table_bd",
                    timeout=10000,
                )
            except Exception:
                pass  # Table may not exist for this date
            html = page.content()
            browser.close()
        return html
    except Exception as exc:
        log.warning("Playwright fetch failed: %s", exc)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 2. Discovery — available dates & race numbers
# ══════════════════════════════════════════════════════════════════════════════

def discover_meeting(html: str) -> Tuple[str, str, List[int]]:
    """Parse the race card landing page to extract racecourse and race numbers.

    Returns (racecourse, display_date, [race_numbers]).
    """
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)

    racecourse = "ST"
    if "Happy Valley" in page_text:
        racecourse = "HV"

    display_date = ""
    date_match = re.search(r"(\d{1,2}\s+\w+\s*-\s*(?:Sha Tin|Happy Valley))", page_text)
    if date_match:
        display_date = date_match.group(1)

    race_nums = set()
    for img in soup.find_all("img", src=True):
        m = re.search(r"racecard_rt_(\d+)", str(img["src"]))
        if m:
            race_nums.add(int(m.group(1)))
    for a_tag in soup.find_all("a", href=True):
        m = re.search(r"RaceNo=(\d+)", str(a_tag["href"]), re.IGNORECASE)
        if m:
            race_nums.add(int(m.group(1)))

    race_list = sorted(race_nums) if race_nums else list(range(1, 12))
    return racecourse, display_date, race_list


# ══════════════════════════════════════════════════════════════════════════════
# 3. Parse — race header & horse table
# ══════════════════════════════════════════════════════════════════════════════

def _clean(text: str) -> str:
    """Normalize whitespace, strip hidden chars."""
    return re.sub(r"\s+", " ", text).strip()


def _safe_int(val: str) -> Optional[int]:
    """Parse integer safely, returning None on failure."""
    if not val or val == "-":
        return None
    m = re.search(r"-?\d+", val.replace(",", ""))
    return int(m.group()) if m else None


def parse_race_header(soup: BeautifulSoup, race_no: int) -> Dict[str, Any]:
    """Extract race-level metadata from the page.

    The race info typically appears in a text block like:
      "Race 1 - SHEK KIP MEI HANDICAP Wednesday, April 01, 2026, Sha Tin, 18:45
       All Weather Track, 1200M, Good  Prize Money: $875,000, Rating: 40-0, Class 5"
    """
    meta: Dict[str, Any] = {
        "race_number": race_no, "race_name": "", "race_class": "",
        "distance": 0, "surface": "", "race_course": "", "going": "",
        "prize": "", "race_time": "", "rating_range": "",
    }

    page_text = soup.get_text(" ", strip=True)

    # Race name — e.g. "Race 1 - SHEK KIP MEI HANDICAP"
    name_m = re.search(
        r"Race\s+\d+\s*[-–—]\s*(.+?)(?:Monday|Tuesday|Wednesday|Thursday|Friday|"
        r"Saturday|Sunday|,\s*\d{4})",
        page_text, re.IGNORECASE,
    )
    if name_m:
        meta["race_name"] = _clean(name_m.group(1).rstrip(" ,"))

    # Class
    cls_m = re.search(r"Class\s+(\d)", page_text)
    if cls_m:
        meta["race_class"] = int(cls_m.group(1))
    else:
        gm = re.search(r"Group\s+(One|Two|Three|1|2|3)", page_text, re.I)
        if gm:
            meta["race_class"] = f"Group {gm.group(1)}"

    # Distance
    dist_m = re.search(r"(\d{3,4})\s*M\b", page_text, re.I)
    if dist_m:
        meta["distance"] = int(dist_m.group(1))

    # Surface
    if "All Weather" in page_text:
        meta["surface"] = "All Weather Track"
        meta["race_course"] = "AWT"
    else:
        meta["surface"] = "Turf"
        course_m = re.search(r'"([A-C](?:\+\d)?)"', page_text)
        if course_m:
            meta["race_course"] = course_m.group(1)

    # Going — match the longest specific going description first
    for pattern in [
        "Good to Firm", "Good to Yielding", "Yielding to Soft",
        "Wet Fast", "Wet Slow", "Good", "Firm", "Yielding", "Soft", "Heavy",
    ]:
        if pattern in page_text:
            meta["going"] = pattern
            break

    # Rating range
    rtg_m = re.search(r"Rating:\s*(\d+)\s*-\s*(\d+)", page_text)
    if rtg_m:
        meta["rating_range"] = f"{rtg_m.group(1)}-{rtg_m.group(2)}"

    # Prize
    prize_m = re.search(r"(?:Prize\s+Money:\s*\$|HK\$)\s*([\d,]+)", page_text)
    if prize_m:
        meta["prize"] = f"HK${prize_m.group(1)}"

    # Race time — e.g. "18:45"
    time_m = re.search(r",\s*(\d{1,2}:\d{2})\s", page_text)
    if time_m:
        meta["race_time"] = time_m.group(1)

    return meta


def _find_starter_table(soup: BeautifulSoup) -> Optional[Tag]:
    """Locate the main horse data table — the 'starter' class table or 'racecardlist'."""
    # Primary: table with class "starter"
    tbl = soup.find("table", class_="starter")
    if tbl:
        return tbl

    # Secondary: table id="racecardlist"
    tbl = soup.find("table", id="racecardlist")
    if tbl:
        return tbl

    # Fallback: any table whose header row contains "Horse No." + "Jockey" + "Draw"
    for tbl in soup.find_all("table"):
        first_row = tbl.find("tr")
        if first_row:
            text = first_row.get_text(" ", strip=True)
            if "Horse No." in text and "Jockey" in text and "Draw" in text:
                return tbl

    return None


def _find_standby_table(soup: BeautifulSoup) -> Optional[Tag]:
    """Locate the stand-by starters table (id=standbylist)."""
    return soup.find("table", id="standbylist")


def _detect_column_indices(header_row: Tag) -> Dict[str, int]:
    """Dynamically detect column indices from the header row.

    Falls back to the hardcoded COL map if headers match the expected 27-column layout.
    """
    cells = header_row.find_all(["td", "th"])
    headers = [_clean(c.get_text()) for c in cells]

    if len(headers) >= 20:
        # Try matching known header names to indices
        detected: Dict[str, int] = {}
        patterns = [
            ("horse_no", r"^Horse No"),
            ("last_6", r"Last\s*6"),
            ("colour", r"Colour"),
            ("horse_name", r"^Horse$"),
            ("brand_no", r"Brand"),
            ("weight", r"^Wt\.$"),
            ("jockey", r"Jockey"),
            ("overweight", r"Over\s*Wt"),
            ("draw", r"^Draw$"),
            ("trainer", r"^Trainer$"),
            ("intl_rating", r"Int.*Rtg"),
            ("rating", r"^Rtg\.$"),
            ("rating_change", r"Rtg\.\s*\+"),
            ("horse_wt_decl", r"Horse Wt"),
            ("wt_change", r"Wt\.\s*\+"),
            ("best_time", r"Best\s*Time"),
            ("age", r"^Age$"),
            ("wfa", r"WFA|Weight.*Age"),
            ("sex", r"^Sex$"),
            ("season_stakes", r"Season\s*Stakes"),
            ("priority", r"^Priority$"),
            ("days_since_last", r"Days"),
            ("gear", r"^Gear$"),
            ("owner", r"Owner"),
            ("sire", r"^Sire$"),
            ("dam", r"^Dam$"),
            ("import_cat", r"Import"),
        ]
        for key, pat in patterns:
            for i, h in enumerate(headers):
                if key not in detected and re.search(pat, h, re.I):
                    detected[key] = i
                    break
        if len(detected) >= 10:
            return detected

    # Fallback to hardcoded
    return dict(COL)


def parse_racecard_table(soup: BeautifulSoup, race_meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse horse data from the HKJC 'starter' or 'racecardlist' table (27 cols).

    Returns list of horse dicts, one per runner.
    """
    horses: List[Dict[str, Any]] = []

    tbl = _find_starter_table(soup)
    if tbl is None:
        log.warning("  No starter table found for R%d", race_meta["race_number"])
        return horses

    rows = tbl.find_all("tr")
    if not rows:
        return horses

    # Detect column layout from header row
    col_idx = _detect_column_indices(rows[0])

    # Parse data rows (skip header)
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) < 10:
            continue

        texts = [_clean(c.get_text()) for c in cells]

        # Validate: first cell should be horse number (integer 1-30)
        no_idx = col_idx.get("horse_no", 0)
        horse_no = _safe_int(texts[no_idx] if no_idx < len(texts) else "")
        if horse_no is None or horse_no < 1 or horse_no > 30:
            continue

        def _get(key: str) -> str:
            idx = col_idx.get(key)
            if idx is not None and idx < len(texts):
                return texts[idx]
            return ""

        def _get_cell(key: str) -> Optional[Tag]:
            idx = col_idx.get(key)
            if idx is not None and idx < len(cells):
                return cells[idx]
            return None

        # Horse name & ID
        horse_name = _get("horse_name")
        horse_id = ""
        hcell = _get_cell("horse_name")
        if hcell:
            a_tag = hcell.find("a", href=True)
            if a_tag:
                horse_name = horse_name or _clean(a_tag.get_text())
                m = re.search(r"horseid=([^&]+)", str(a_tag["href"]), re.I)
                if m:
                    horse_id = m.group(1)

        if not horse_name:
            log.debug("  Skipping row with empty horse name (no=%s)", horse_no)
            continue

        # Jockey — might include overweight in parens e.g. "C L Chau (-2)"
        jockey_raw = _get("jockey")
        overweight = _get("overweight")

        horses.append({
            # Race-level
            "race_number": race_meta.get("race_number"),
            "race_name": race_meta.get("race_name", ""),
            "race_class": race_meta.get("race_class", ""),
            "distance": race_meta.get("distance", 0),
            "surface": race_meta.get("surface", ""),
            "race_course": race_meta.get("race_course", ""),
            "going": race_meta.get("going", ""),
            "prize": race_meta.get("prize", ""),
            "race_time": race_meta.get("race_time", ""),
            "rating_range": race_meta.get("rating_range", ""),
            # Horse-level
            "horse_no": horse_no,
            "horse_name": horse_name,
            "horse_id": horse_id,
            "brand_no": _get("brand_no"),
            "draw": _safe_int(_get("draw")),
            "jockey": jockey_raw,
            "overweight": overweight,
            "trainer": _get("trainer"),
            "weight": _safe_int(_get("weight")),
            "rating": _safe_int(_get("rating")),
            "rating_change": _get("rating_change"),
            "intl_rating": _get("intl_rating"),
            "age": _safe_int(_get("age")),
            "sex": _get("sex"),
            "colour": _get("colour"),
            "last_6_runs": _get("last_6"),
            "gear": _get("gear"),
            "priority": _get("priority"),
            "horse_wt_declaration": _safe_int(_get("horse_wt_decl")),
            "wt_change": _get("wt_change"),
            "best_time": _get("best_time"),
            "wfa": _get("wfa"),
            "season_stakes": _get("season_stakes"),
            "days_since_last": _safe_int(_get("days_since_last")),
            "owner": _get("owner"),
            "sire": _get("sire"),
            "dam": _get("dam"),
            "import_cat": _get("import_cat"),
            "is_standby": False,
        })

    # Also parse stand-by starters (simpler table with 10 cols)
    standby_tbl = _find_standby_table(soup)
    if standby_tbl:
        sb_rows = standby_tbl.find_all("tr")
        for row in sb_rows[1:]:  # skip header
            cells = row.find_all(["td", "th"])
            texts = [_clean(c.get_text()) for c in cells]
            if len(texts) < 5:
                continue
            sb_no = _safe_int(texts[0])
            if sb_no is None:
                continue
            horses.append({
                "race_number": race_meta.get("race_number"),
                "race_name": race_meta.get("race_name", ""),
                "race_class": race_meta.get("race_class", ""),
                "distance": race_meta.get("distance", 0),
                "surface": race_meta.get("surface", ""),
                "race_course": race_meta.get("race_course", ""),
                "going": race_meta.get("going", ""),
                "prize": race_meta.get("prize", ""),
                "race_time": race_meta.get("race_time", ""),
                "rating_range": race_meta.get("rating_range", ""),
                "horse_no": sb_no,
                "horse_name": texts[1] if len(texts) > 1 else "",
                "horse_id": "",
                "brand_no": "",
                "draw": None,
                "jockey": "",
                "overweight": "",
                "trainer": texts[7] if len(texts) > 7 else "",
                "weight": _safe_int(texts[3]) if len(texts) > 3 else None,
                "rating": _safe_int(texts[4]) if len(texts) > 4 else None,
                "rating_change": "",
                "intl_rating": "",
                "age": _safe_int(texts[5]) if len(texts) > 5 else None,
                "sex": "",
                "colour": "",
                "last_6_runs": texts[6] if len(texts) > 6 else "",
                "gear": texts[9] if len(texts) > 9 else "",
                "priority": texts[8] if len(texts) > 8 else "",
                "horse_wt_declaration": _safe_int(texts[2]) if len(texts) > 2 else None,
                "wt_change": "",
                "best_time": "",
                "wfa": "",
                "season_stakes": "",
                "days_since_last": None,
                "owner": "",
                "sire": "",
                "dam": "",
                "import_cat": "",
                "is_standby": True,
            })

    return horses


# ══════════════════════════════════════════════════════════════════════════════
# 4. Normalize — clean & structure data
# ══════════════════════════════════════════════════════════════════════════════

def normalize_data(race_date: str, racecourse: str,
                   all_race_data: List[Tuple[Dict, List[Dict]]]) -> pd.DataFrame:
    """Combine all race data into a single normalised DataFrame.

    One row per horse per race, with race-level metadata as columns.
    """
    rows: List[Dict[str, Any]] = []

    for race_meta, horse_list in all_race_data:
        for horse in horse_list:
            row = {
                "race_date": race_date,
                "racecourse": racecourse,
            }
            row.update(horse)
            rows.append(row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Column ordering
    leading_cols = [
        "race_date", "racecourse", "race_number", "race_name", "race_class",
        "distance", "surface", "race_course", "going", "rating_range",
        "horse_no", "horse_name", "horse_id", "brand_no", "draw",
        "jockey", "overweight", "trainer",
        "weight", "rating", "rating_change", "intl_rating",
        "age", "sex", "colour",
        "last_6_runs", "gear", "priority",
        "horse_wt_declaration", "wt_change", "best_time", "wfa",
        "season_stakes", "days_since_last",
        "owner", "sire", "dam", "import_cat",
        "is_standby", "prize", "race_time",
    ]
    present = [c for c in leading_cols if c in df.columns]
    remaining = [c for c in df.columns if c not in present]
    df = df[present + remaining]

    return df


# ══════════════════════════════════════════════════════════════════════════════
# 5. Export — Excel with one sheet per race
# ══════════════════════════════════════════════════════════════════════════════

def export_to_excel(df: pd.DataFrame, output_path: Path) -> None:
    """Export DataFrame to Excel with separate sheet per race.

    Also creates a 'Summary' sheet with all races combined.
    Uses safe_excel_write to avoid OneDrive PermissionError.
    """
    def _write(tmp_path: Path) -> None:
        with pd.ExcelWriter(str(tmp_path), engine="openpyxl") as writer:
            # Summary sheet — all horses
            df.to_excel(writer, sheet_name="All Races", index=False)

            # Per-race sheets
            if "race_number" in df.columns:
                for rn in sorted(df["race_number"].dropna().unique()):
                    race_df = df[df["race_number"] == rn].copy()
                    sheet_name = f"Race {int(rn)}"
                    if len(sheet_name) > 31:
                        sheet_name = sheet_name[:31]
                    race_df.to_excel(writer, sheet_name=sheet_name, index=False)

    safe_excel_write(output_path, _write)

    log.info("Excel saved: %s (%d horses across %d races)",
             output_path.name, len(df),
             df["race_number"].nunique() if "race_number" in df.columns else 0)


# ══════════════════════════════════════════════════════════════════════════════
# 6. Cache — JSON cache keyed by race date
# ══════════════════════════════════════════════════════════════════════════════

def _cache_path(cache_dir: Path, race_date: str) -> Path:
    """Return the cache file path for a given race date."""
    safe_date = race_date.replace("/", "-").replace("\\", "-")
    return cache_dir / f"racecard_{safe_date}.json"


def load_cache(cache_dir: Path, race_date: str) -> Optional[Dict]:
    """Load cached race card data for a date.

    Returns None if not cached, OR if the cached file is incomplete (e.g. a
    previous scrape crashed mid-meeting). This prevents Bug A — partial-cache
    poisoning — where re-running would otherwise silently return a 3-of-11
    cache as if the meeting had only 3 races.
    """
    fp = _cache_path(cache_dir, race_date)
    data = safe_json_read(fp)
    if data is None:
        return None
    # Reject incomplete caches written by older versions or crashed runs.
    if not data.get("complete", False):
        log.warning("Cache for %s is incomplete (no completion flag) — ignoring",
                    race_date)
        return None
    races = data.get("races") or []
    expected = data.get("expected_races")
    if expected is not None and len(races) != expected:
        log.warning("Cache for %s has %d/%d races — ignoring partial cache",
                    race_date, len(races), expected)
        return None
    return data


def save_cache(cache_dir: Path, race_date: str, data: Dict) -> None:
    """Save race card data to cache atomically (.tmp → rename)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    fp = _cache_path(cache_dir, race_date)
    safe_json_write(fp, data)
    log.info("  Cache saved: %s", fp.name)


# ══════════════════════════════════════════════════════════════════════════════
# 7. Scrape orchestrator — one race day
# ══════════════════════════════════════════════════════════════════════════════

def scrape_race_day(race_date: str, use_cache: bool = True,
                    cache_dir: Path = DEFAULT_CACHE_DIR,
                    sleep: float = 0.5) -> Optional[pd.DataFrame]:
    """Scrape all races for a given date.

    Args:
        race_date: YYYY-MM-DD format.
        use_cache: If True, return cached data when available.
        cache_dir: Path to cache directory.
        sleep: Delay between requests in seconds.

    Returns:
        DataFrame with all horse data, or None on failure.
    """
    # Convert date formats
    date_query = datetime.strptime(race_date, "%Y-%m-%d").strftime("%Y/%m/%d")
    date_display = datetime.strptime(race_date, "%Y-%m-%d").strftime("%d %b %Y")

    # Check cache
    if use_cache:
        cached = load_cache(cache_dir, race_date)
        if cached:
            log.info("Using cached data for %s (%d races)", race_date,
                     len(cached.get("races", [])))
            all_data = []
            for race_entry in cached["races"]:
                all_data.append((race_entry["meta"], race_entry["horses"]))
            df = normalize_data(race_date, cached.get("racecourse", ""),
                                all_data)
            return df

    session = requests.Session()
    session.headers.update(HEADERS)

    # Step 1: Fetch landing page to discover racecourse and race numbers
    log.info("Fetching race card for %s …", date_display)
    landing_html = fetch_page(RACECARD_URL, session,
                              params={"racedate": date_query})
    if not landing_html:
        log.error("Failed to fetch landing page")
        return None

    racecourse, display, race_numbers = discover_meeting(landing_html)
    log.info("  Meeting: %s (%s), races: %s", racecourse, display,
             race_numbers if race_numbers else "discovering…")

    if not race_numbers:
        log.warning("  No race numbers found — trying 1–11")
        race_numbers = list(range(1, 12))

    # Step 2: Fetch each race card page
    # Try requests first; if no tables found on first race, switch to Playwright
    use_playwright = False
    all_race_data: List[Tuple[Dict, List[Dict]]] = []

    for rn in race_numbers:
        log.info("  Scraping Race %d/%d …", rn, len(race_numbers))
        params = {
            "racedate": date_query,
            "Racecourse": racecourse,
            "RaceNo": str(rn),
        }

        html = None
        if not use_playwright:
            html = fetch_page(RACECARD_URL, session, params=params)

        soup = BeautifulSoup(html, "html.parser") if html else None
        horses = []
        race_meta = {"race_number": rn}

        if soup:
            race_meta = parse_race_header(soup, rn)
            horses = parse_racecard_table(soup, race_meta)

        # If requests returned no horses on the first race, try Playwright
        if not horses and not use_playwright and rn == race_numbers[0]:
            log.info("  No tables via requests — trying Playwright (JS render) …")
            js_html = fetch_page_js(RACECARD_URL, params=params)
            if js_html:
                soup = BeautifulSoup(js_html, "html.parser")
                race_meta = parse_race_header(soup, rn)
                horses = parse_racecard_table(soup, race_meta)
                if horses:
                    log.info("  Playwright succeeded — switching to JS rendering")
                    use_playwright = True

        # For subsequent races, use Playwright if we switched
        if not horses and use_playwright and rn != race_numbers[0]:
            js_html = fetch_page_js(RACECARD_URL, params=params)
            if js_html:
                soup = BeautifulSoup(js_html, "html.parser")
                race_meta = parse_race_header(soup, rn)
                horses = parse_racecard_table(soup, race_meta)

        if not horses:
            # Race might not exist (e.g., only 9 races on the card)
            log.info("    R%d — no horses found (race may not exist)", rn)
            # Stop if consecutive empty after initially finding data
            if all_race_data and rn > race_numbers[0] + 1:
                consecutive_empty = True
                for prev_meta, prev_horses in all_race_data[-2:]:
                    if prev_horses:
                        consecutive_empty = False
                if consecutive_empty:
                    log.info("    Two consecutive empty races — stopping")
                    break
        else:
            log.info("    R%d: %d horses, %dm %s %s",
                     rn, len(horses), race_meta.get("distance", 0),
                     race_meta.get("surface", "?"),
                     f"Class {race_meta.get('race_class', '?')}")

        all_race_data.append((race_meta, horses))

        if rn < race_numbers[-1]:
            time.sleep(sleep)

    if not all_race_data:
        log.error("No race data collected for %s", race_date)
        return None

    # Save to cache with completeness metadata so partial scrapes can't poison
    # subsequent runs (Bug A fix).
    races_with_horses = [(m, h) for m, h in all_race_data if h]
    is_complete = (
        len(races_with_horses) == len(race_numbers)
        and len(races_with_horses) > 0
    )
    cache_data = {
        "race_date": race_date,
        "racecourse": racecourse,
        "scraped_at": datetime.now().isoformat(),
        "expected_races": len(race_numbers),
        "complete": is_complete,
        "races": [
            {"meta": meta, "horses": horses}
            for meta, horses in all_race_data
            if horses  # only persist non-empty races
        ],
    }
    save_cache(cache_dir, race_date, cache_data)
    if not is_complete:
        log.warning(
            "  Cache for %s saved as INCOMPLETE (%d/%d races) — next run will refetch",
            race_date, len(races_with_horses), len(race_numbers),
        )

    # Normalize
    df = normalize_data(race_date, racecourse, all_race_data)
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 8. CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Scrape HKJC race card data and export to Excel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scrape_hkjc_racecard.py --date 2026-04-01
  python scrape_hkjc_racecard.py --date 2026-04-01 --no-cache
  python scrape_hkjc_racecard.py --date 2026-04-01 --output racecards/card_20260401.xlsx
        """,
    )
    parser.add_argument(
        "--date", required=True,
        help="Race date in YYYY-MM-DD format (e.g. 2026-04-01).",
    )
    parser.add_argument(
        "--output", default=None,
        help="Output Excel file path. Default: racecards/racecard_YYYYMMDD.xlsx",
    )
    parser.add_argument(
        "--cache", dest="use_cache", action="store_true", default=True,
        help="Use cached data if available (default).",
    )
    parser.add_argument(
        "--no-cache", dest="use_cache", action="store_false",
        help="Ignore cache and re-scrape.",
    )
    parser.add_argument(
        "--force", dest="use_cache", action="store_false",
        help="Alias for --no-cache (force re-scrape).",
    )
    parser.add_argument(
        "--cache-dir", default=str(DEFAULT_CACHE_DIR),
        help=f"Cache directory (default: {DEFAULT_CACHE_DIR}).",
    )
    parser.add_argument(
        "--sleep", type=float, default=0.5,
        help="Delay between requests in seconds (default: 0.5).",
    )

    args = parser.parse_args()

    # Validate date
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        log.error("Invalid date format: %s (expected YYYY-MM-DD)", args.date)
        sys.exit(1)

    # Default output path
    if args.output is None:
        date_compact = args.date.replace("-", "")
        args.output = str(
            Path(__file__).parent / "racecards" / f"racecard_{date_compact}.xlsx"
        )

    cache_dir = Path(args.cache_dir)
    output_path = Path(args.output)

    log.info("=" * 60)
    log.info("HKJC Race Card Scraper")
    log.info("  Date:   %s", args.date)
    log.info("  Output: %s", output_path)
    log.info("  Cache:  %s (dir: %s)", "ON" if args.use_cache else "OFF", cache_dir)
    log.info("=" * 60)

    df = scrape_race_day(
        race_date=args.date,
        use_cache=args.use_cache,
        cache_dir=cache_dir,
        sleep=args.sleep,
    )

    if df is None or df.empty:
        log.error("No data collected — exiting.")
        sys.exit(1)

    export_to_excel(df, output_path)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    n_races = df["race_number"].nunique() if "race_number" in df.columns else 0
    n_horses = len(df)
    print(f"  Date:    {args.date}")
    print(f"  Races:   {n_races}")
    print(f"  Horses:  {n_horses}")
    print(f"  Output:  {output_path}")

    if "race_number" in df.columns:
        print(f"\n  {'Race':>6s}  {'Horses':>6s}  {'Dist':>6s}  {'Surface':<12s}  {'Class':<8s}")
        print(f"  {'─'*6}  {'─'*6}  {'─'*6}  {'─'*12}  {'─'*8}")
        for rn in sorted(df["race_number"].dropna().unique()):
            rdf = df[df["race_number"] == rn]
            dist = rdf["distance"].iloc[0] if "distance" in rdf.columns else "?"
            surf = rdf["surface"].iloc[0] if "surface" in rdf.columns else "?"
            cls = rdf["race_class"].iloc[0] if "race_class" in rdf.columns else "?"
            print(f"  {int(rn):>6d}  {len(rdf):>6d}  {dist:>6}  {str(surf):<12s}  {str(cls):<8s}")

    print(f"\nDone.")


if __name__ == "__main__":
    main()
