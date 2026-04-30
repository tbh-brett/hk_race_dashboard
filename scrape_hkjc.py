#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import duckdb
import pandas as pd
import requests
from bs4 import BeautifulSoup

from hkjc_client import (
    BASE_URL, LOCALRESULTS_URL, RESULTSALL_URL, SECTIONAL_URL,
    HORSE_URL, HORSE_URL_ZH, OTHERHORSE_URL, OTHERHORSE_URL_ZH,
    HEADERS, DATE_FMT_UI, DATE_FMT_QUERY, GOING_ABBREV,
    abbreviate_going, fetch_html, extract_horse_id,
    strip_html_to_text, split_slash_value, safe_excel_write,
)


@dataclass
class SectionalRow:
    positions: List[str]
    lbws: List[str]
    sectiontimes: List[str]


# --- Helper Functions ---

def convert_date_ui_to_query(date_ui: str) -> str:
    """Convert DD/MM/YYYY to YYYY/MM/DD for query params."""
    return dt.datetime.strptime(date_ui, DATE_FMT_UI).strftime(DATE_FMT_QUERY)


def convert_date_ui_to_iso(date_ui: str) -> str:
    """Convert DD/MM/YYYY to YYYY-MM-DD for storage."""
    return dt.datetime.strptime(date_ui, DATE_FMT_UI).strftime("%Y-%m-%d")


def parse_available_dates(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    options = soup.select("#selectId option")
    dates = [opt.get("value", "").strip() for opt in options]
    return [d for d in dates if d]


def parse_racecourse_and_numbers(html: str) -> Tuple[str, List[int]]:
    soup = BeautifulSoup(html, "html.parser")
    race_links = soup.select("a[href*='localresults?racedate=']")
    race_numbers = set()
    racecourse = ""
    for link in race_links:
        href = link.get("href", "")
        match = re.search(r"Racecourse=([A-Z0-9]+)&RaceNo=(\d+)", href)
        if match:
            course_code = match.group(1)
            # Only accept local HK racecourses (ST=Sha Tin, HV=Happy Valley)
            # Ignore overseas simulcast codes like S2, S3, etc.
            if course_code in ("ST", "HV"):
                racecourse = course_code
                race_numbers.add(int(match.group(2)))
    return racecourse, sorted(race_numbers)


def parse_race_header_info(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(soup.stripped_strings)

    race_title = ""
    race_index = ""
    match = re.search(r"RACE\s+(\d+)\s*\((\d+)\)", text, re.IGNORECASE)
    if match:
        race_title = match.group(1)
        # Zero-pad race_index to 3 digits to match form table format (e.g., 95 -> 095)
        race_index = match.group(2).zfill(3)

    meeting_date = ""
    meeting_track = ""
    meeting_match = re.search(r"Race Meeting:\s*(\d{2}/\d{2}/\d{4})\s+([A-Za-z ]+)", text)
    if meeting_match:
        meeting_date = meeting_match.group(1)
        meeting_track = meeting_match.group(2).strip()

    race_class = ""
    distance = ""
    race_track = ""
    race_name = ""

    class_distance_match = re.search(r"Class\s*(\d+)\s*-\s*(\d+)M", text)
    if class_distance_match:
        race_class = class_distance_match.group(1)
        distance = class_distance_match.group(2)
    else:
        # Fallback for non-Class races: Griffin Race, Group N, Listed Race, etc.
        alt_match = re.search(r"(Griffin\s+Race|Group\s+\d+|Listed\s+Race)\s*-\s*(\d+)M", text, re.IGNORECASE)
        if alt_match:
            race_class = alt_match.group(1).strip()
            distance = alt_match.group(2)
        else:
            # Last resort: grab any standalone NNNM between dashes
            dist_only = re.search(r"-\s*(\d{3,4})M", text)
            if dist_only:
                distance = dist_only.group(1)

    # Extract going - it's between "Going :" and the race name
    # Going can be: GOOD, GOOD TO FIRM, GOOD TO YIELDING, YIELDING, SOFT, HEAVY, FAST, WET FAST, WET SLOW
    going_match = re.search(r"Going\s*:\s*([A-Z]+(?:\s+(?:TO|AND)\s+[A-Z]+)?(?:\s+(?:FAST|SLOW))?)", text)
    going = going_match.group(1).strip() if going_match else ""
    
    # Extract race name - it's between going value and "Course :"
    race_name = ""
    race_name_match = re.search(r"Going\s*:\s*(?:[A-Z]+(?:\s+(?:TO|AND)\s+[A-Z]+)?(?:\s+(?:FAST|SLOW))?)\s+(.+?)\s+Course\s*:", text)
    if race_name_match:
        race_name = race_name_match.group(1).strip()
    
    # Extract course variant (e.g., "C+3", "A", "B") from course description
    # Format can be: 'ALL WEATHER TRACK' or 'TURF - "C+3" Course' or similar
    course_match = re.search(r'Course\s*:\s*(.+?)(?:\s+Class|\s+Race|\s*$)', text, re.IGNORECASE)
    course_raw = course_match.group(1).strip() if course_match else ""
    
    # Extract the course variant like "C+3", "A+3", "B" etc.
    # Check for ALL WEATHER FIRST, before the variant regex
    # (otherwise [A-C] matches the "A" in "ALL WEATHER TRACK")
    if "ALL WEATHER" in course_raw.upper() or "AWT" in course_raw.upper():
        course = "AWT"
    else:
        variant_match = re.search(r'["\']?([A-C](?:\+\d)?)["\']?', course_raw)
        if variant_match:
            course = variant_match.group(1)
        else:
            course = course_raw
    if course:
        race_track = course

    return {
        "race_number": race_title,
        "race_index": race_index,
        "meeting_date": meeting_date,
        "meeting_track": meeting_track,
        "race_class": race_class,
        "distance": distance,
        "race_track": race_track,
        "race_name": race_name,
        "going": going,
        "course": course,
    }


def parse_localresults_table(html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.f_tac.table_bd.draggable")
    if not table:
        return []

    rows = []
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        if len(cells) < 12:
            continue
        place = cells[0].get_text(" ", strip=True)
        horse_no = cells[1].get_text(" ", strip=True)

        horse_cell = cells[2]
        horse_link = horse_cell.find("a")
        horse_text = horse_cell.get_text(" ", strip=True)
        horse_name = horse_text.split("(")[0].strip()
        horse_id = extract_horse_id(horse_link.get("href", "")) if horse_link else None

        jockey = cells[3].get_text(" ", strip=True)
        trainer = cells[4].get_text(" ", strip=True)
        actual_weight = cells[5].get_text(" ", strip=True)
        declared_weight = cells[6].get_text(" ", strip=True)
        draw = cells[7].get_text(" ", strip=True)
        lbw = cells[8].get_text(" ", strip=True)
        # cells[9] is running positions (from local results table)
        finish_time = cells[10].get_text(" ", strip=True)
        win_odds = cells[11].get_text(" ", strip=True)

        rows.append(
            {
                "place": place,
                "horse_number": horse_no,
                "horse_name": horse_name,
                "horse_id": horse_id,
                "jockey": jockey,
                "trainer": trainer,
                "actual_weight": actual_weight,
                "declared_weight": declared_weight,
                "draw": draw,
                "lbw": lbw,
                "finish_time": finish_time,
                "win_odds": win_odds,
            }
        )
    return rows


def parse_sectional_table(html: str) -> Dict[str, SectionalRow]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.table_bd.f_tac.race_table")
    if not table:
        return {}

    results = {}
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue

        horse_link = tds[2].find("a")
        horse_id = extract_horse_id(horse_link.get("href", "")) if horse_link else None

        positions: List[str] = []
        lbws: List[str] = []
        sectiontimes: List[str] = []

        for td in tds[3:9]:
            if td.find("img"):
                positions.append("")
                lbws.append("")
                sectiontimes.append("")
                continue
            pos = td.select_one("span.f_fl")
            lbw = td.select_one("i")
            # Get section time from the second <p> tag
            # The time is the first number, before any nested spans with split times
            sec_time = ""
            ps = td.find_all("p")
            if len(ps) >= 2:
                time_p = ps[1]
                # Remove nested spans (split times) before getting text
                for span in time_p.find_all("span"):
                    span.decompose()
                sec_time = time_p.get_text(strip=True)
            positions.append(pos.get_text(strip=True) if pos else "")
            lbws.append(lbw.get_text(strip=True) if lbw else "")
            sectiontimes.append(sec_time)

        if horse_id:
            results[horse_id] = SectionalRow(positions, lbws, sectiontimes)

    return results


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


def extract_labeled_values(text: str, labels: List[str]) -> Dict[str, str]:
    """Extract values between labels. Properly terminates at any known label or noise patterns."""
    values = {}
    # Build set of all labels for proper termination
    all_labels = set(labels)
    # Common noise patterns that should terminate a value
    noise_patterns = [" PP ", " Racing ", " Audio ", " Video ", " Tips ", " Useful ", " General "]
    
    for label in labels:
        start = text.find(label)
        if start == -1:
            continue
        start = start + len(label)
        if text[start:start + 1] == ":":
            start += 1
        # Find the earliest next label (any label, not just ones after current)
        end = len(text)
        for other_label in all_labels:
            if other_label == label:
                continue
            idx = text.find(other_label, start)
            if idx != -1 and idx < end:
                end = idx
        # Also terminate at noise patterns
        for pattern in noise_patterns:
            idx = text.find(pattern, start)
            if idx != -1 and idx < end:
                end = idx
        values[label] = text[start:end].strip(" :")
    return values


def parse_horse_profile(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for candidate in soup.select("table"):
        if "Country of Origin / Age" in candidate.get_text(" ", strip=True):
            table = candidate
            break
    if not table:
        return {}

    text = " ".join(table.stripped_strings)
    labels = [
        "Country of Origin / Age", "Colour / Sex", "Import Type", "Season Stakes*",
        "Total Stakes*", "No. of 1-2-3-Starts*", "No. of starts in past 10 race meetings",
        "Current Stable Location (Arrival Date)", "Import Date", "Trainer", "Owner",
        "Current Rating", "Start of Season Rating", "Sire", "Dam", "Dam's Sire", "Last Rating",
    ]
    raw = extract_labeled_values(text, labels)

    origin_age = raw.get("Country of Origin / Age", "")
    colour_sex = raw.get("Colour / Sex", "")

    return {
        "country": split_slash_value(origin_age, 0),
        "age": split_slash_value(origin_age, 1),
        "colour": split_slash_value(colour_sex, 0),
        "sex": split_slash_value(colour_sex, 1),
        "import_type": raw.get("Import Type", ""),
        "season_stakes": raw.get("Season Stakes*", ""),
        "total_stakes": raw.get("Total Stakes*", ""),
        "no_of_123_starts": raw.get("No. of 1-2-3-Starts*", ""),
        "no_of_starts_in_past_10_race_meetings": raw.get("No. of starts in past 10 race meetings", ""),
        "current_stable_location_arrival_date": raw.get("Current Stable Location (Arrival Date)", ""),
        "import_date": raw.get("Import Date", ""),
        "owner": raw.get("Owner", ""),
        "current_rating": raw.get("Current Rating", ""),
        "start_of_season_rating": raw.get("Start of Season Rating", ""),
        "sire": raw.get("Sire", ""),
        "dam": raw.get("Dam", ""),
        "dam_sire": raw.get("Dam's Sire", ""),
        "last_rating": raw.get("Last Rating", ""),
        "trainer": raw.get("Trainer", ""),
    }


def parse_otherhorse_profile(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for candidate in soup.select("table"):
        if "Country of Origin" in candidate.get_text(" ", strip=True):
            table = candidate
            break
    if not table:
        return {}

    text = " ".join(table.stripped_strings)
    labels = [
        "Country of Origin", "Colour / Sex", "Import Type", "Total Stakes*",
        "No. of 1-2-3-Starts*", "Owner", "Last Rating", "Sire", "Dam", "Dam's Sire",
    ]
    raw = extract_labeled_values(text, labels)
    colour_sex = raw.get("Colour / Sex", "")

    return {
        "country": raw.get("Country of Origin", ""),
        "colour": split_slash_value(colour_sex, 0),
        "sex": split_slash_value(colour_sex, 1),
        "import_type": raw.get("Import Type", ""),
        "total_stakes": raw.get("Total Stakes*", ""),
        "no_of_123_starts": raw.get("No. of 1-2-3-Starts*", ""),
        "owner": raw.get("Owner", ""),
        "last_rating": raw.get("Last Rating", ""),
        "sire": raw.get("Sire", ""),
        "dam": raw.get("Dam", ""),
        "dam_sire": raw.get("Dam's Sire", ""),
    }


def parse_horse_profile_zh(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    horsename_zh = title.split(" - ")[0].strip() if " - " in title else ""

    table = None
    for candidate in soup.select("table"):
        if "出生地 / 馬齡" in candidate.get_text(" ", strip=True):
            table = candidate
            break
    if not table:
        return {"horsename_zh": horsename_zh}

    text = " ".join(table.stripped_strings)
    # Include ALL possible labels that can appear in the table to properly terminate values
    labels = [
        "出生地 / 馬齡", "毛色 / 性別", "進口類別", "現在位置 (到達日期)", "進口日期", "練馬師", "馬主",
        "今季獎金*", "總獎金*", "冠-亞-季-總出賽次數*", "最近十個賽馬日", "現時評分", "季初評分",
        "父系", "母系", "外祖父", "同父系馬", "*包括本地及海外賽績及獎金"
    ]
    raw = extract_labeled_values(text, labels)

    origin_age = raw.get("出生地 / 馬齡", "")
    colour_sex = raw.get("毛色 / 性別", "")

    return {
        "horsename_zh": horsename_zh,
        "毛色": split_slash_value(colour_sex, 0),
        "性別": split_slash_value(colour_sex, 1),
        "出生地": split_slash_value(origin_age, 0),
        "進口類別": raw.get("進口類別", ""),
        "現在位置 (到達日期)": raw.get("現在位置 (到達日期)", ""),
        "練馬師": raw.get("練馬師", ""),
        "馬主": raw.get("馬主", ""),
    }


def parse_otherhorse_profile_zh(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    horsename_zh = title.split(" - ")[0].strip() if " - " in title else ""

    table = None
    for candidate in soup.select("table"):
        if "出生地" in candidate.get_text(" ", strip=True) and "毛色" in candidate.get_text(" ", strip=True):
            table = candidate
            break
    if not table:
        return {"horsename_zh": horsename_zh}

    text = " ".join(table.stripped_strings)
    # Include ALL possible labels that can appear in the table to properly terminate values
    labels = [
        "出生地", "毛色 / 性別", "進口類別", "總獎金*", "冠-亞-季-總出賽次數*", "馬主", "最後評分",
        "父系", "母系", "外祖父", "同父系馬", "*包括本地及海外賽績及獎金"
    ]
    raw = extract_labeled_values(text, labels)
    colour_sex = raw.get("毛色 / 性別", "")

    return {
        "horsename_zh": horsename_zh,
        "毛色": split_slash_value(colour_sex, 0),
        "性別": split_slash_value(colour_sex, 1),
        "出生地": raw.get("出生地", ""),
        "進口類別": raw.get("進口類別", ""),
        "馬主": raw.get("馬主", ""),
        "last_rating": raw.get("最後評分", ""),
        "sire": raw.get("父系", ""),
        "dam": raw.get("母系", ""),
        "dam_sire": raw.get("外祖父", ""),
        "total_stakes": raw.get("總獎金*", ""),
        "no_of_123_starts": raw.get("冠-亞-季-總出賽次數*", ""),
    }


def parse_all_horse_form_rows(html: str) -> Dict[str, Dict[str, str]]:
    """Parse horse form table and return ALL race rows as a dict keyed by 'race_{date}_{index}'.
    
    Args:
        html: The horse page HTML
    
    Returns:
        Dict mapping race keys to {rating, gear, start_of_season_rating}
    """
    
    def normalize_date(date_str: str) -> str:
        """Normalize date to DD/MM/YYYY format."""
        if not date_str:
            return ""
        parts = date_str.split("/")
        if len(parts) == 3:
            day, month, year = parts
            if len(year) == 2:
                year_int = int(year)
                year = f"20{year}" if year_int < 50 else f"19{year}"
            return f"{day}/{month}/{year}"
        return date_str
    
    soup = BeautifulSoup(html, "html.parser")
    all_rows = {}
    start_of_season_rating = ""
    
    for table in soup.select("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_cells = rows[0].find_all(["th", "td"])
        headers = [cell.get_text(" ", strip=True) for cell in header_cells]
        if not headers or ("Race Index" not in headers and "RaceIndex" not in headers):
            continue
        
        idx_race_index = headers.index("RaceIndex") if "RaceIndex" in headers else (headers.index("Race Index") if "Race Index" in headers else 0)
        idx_rating = headers.index("Rtg.") if "Rtg." in headers else -1
        idx_gear = headers.index("Gear") if "Gear" in headers else -1
        idx_date = headers.index("Date") if "Date" in headers else 2
        
        in_current_season = False
        last_rating_in_season = ""
        
        for tr in rows[1:]:
            season_cell = tr.find("td", colspan=True)
            if season_cell and "Season" in season_cell.get_text():
                if not in_current_season:
                    in_current_season = True
                continue
            
            tds = tr.find_all("td")
            if not tds or len(tds) <= max(idx_rating, idx_gear, idx_race_index, idx_date):
                continue
            
            row_race_index = tds[idx_race_index].get_text(" ", strip=True) if idx_race_index >= 0 else ""
            row_date = tds[idx_date].get_text(" ", strip=True) if idx_date >= 0 and idx_date < len(tds) else ""
            row_rating = tds[idx_rating].get_text(" ", strip=True) if idx_rating >= 0 and idx_rating < len(tds) else ""
            row_gear = tds[idx_gear].get_text(" ", strip=True) if idx_gear >= 0 and idx_gear < len(tds) else ""
            
            if in_current_season and row_rating:
                last_rating_in_season = row_rating
            
            normalized_date = normalize_date(row_date)
            if row_race_index and normalized_date:
                race_key = f"race_{normalized_date}_{row_race_index}"
                all_rows[race_key] = {
                    "rating": row_rating,
                    "gear": row_gear,
                    "start_of_season_rating": "",  # Will be filled later
                }
        
        start_of_season_rating = last_rating_in_season
        break
    
    # Add start_of_season_rating to all entries
    for key in all_rows:
        all_rows[key]["start_of_season_rating"] = start_of_season_rating
    
    return all_rows


def parse_horse_form_row(html: str, race_index: str, race_date: str = "") -> Dict[str, str]:
    """Parse horse form table to get rating, gear, and start of season rating.
    
    Args:
        html: The horse page HTML
        race_index: The race index to match (e.g., "036")
        race_date: The race date in DD/MM/YYYY format (e.g., "18/09/2024")
                   Used to disambiguate when race indices repeat across seasons.
    """
    
    def normalize_date(date_str: str) -> str:
        """Normalize date to DD/MM/YYYY format for comparison.
        Handles both DD/MM/YY and DD/MM/YYYY formats."""
        if not date_str:
            return ""
        parts = date_str.split("/")
        if len(parts) == 3:
            day, month, year = parts
            # Convert 2-digit year to 4-digit
            if len(year) == 2:
                # Assume 20xx for years < 50, 19xx for >= 50
                year_int = int(year)
                year = f"20{year}" if year_int < 50 else f"19{year}"
            return f"{day}/{month}/{year}"
        return date_str
    
    soup = BeautifulSoup(html, "html.parser")
    result = {"rating": "", "gear": "", "start_of_season_rating": ""}
    
    # Normalize input date for comparison
    normalized_race_date = normalize_date(race_date)
    
    for table in soup.select("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_cells = rows[0].find_all(["th", "td"])
        headers = [cell.get_text(" ", strip=True) for cell in header_cells]
        # Handle both "Race Index" and "RaceIndex" header formats
        if not headers or ("Race Index" not in headers and "RaceIndex" not in headers):
            continue
        
        # Find column indices
        idx_race_index = headers.index("RaceIndex") if "RaceIndex" in headers else (headers.index("Race Index") if "Race Index" in headers else 0)
        idx_rating = headers.index("Rtg.") if "Rtg." in headers else -1
        idx_gear = headers.index("Gear") if "Gear" in headers else -1
        # Date column is typically at index 2
        idx_date = headers.index("Date") if "Date" in headers else 2
        
        in_current_season = False
        last_rating_in_season = ""
        
        for tr in rows[1:]:
            # Check for season header row (e.g., "25/26 Season")
            season_cell = tr.find("td", colspan=True)
            if season_cell and "Season" in season_cell.get_text():
                if not in_current_season:
                    in_current_season = True
                continue
            
            tds = tr.find_all("td")
            if not tds or len(tds) <= max(idx_rating, idx_gear, idx_race_index, idx_date):
                continue
            
            row_race_index = tds[idx_race_index].get_text(" ", strip=True) if idx_race_index >= 0 else ""
            row_date = tds[idx_date].get_text(" ", strip=True) if idx_date >= 0 and idx_date < len(tds) else ""
            row_rating = tds[idx_rating].get_text(" ", strip=True) if idx_rating >= 0 and idx_rating < len(tds) else ""
            row_gear = tds[idx_gear].get_text(" ", strip=True) if idx_gear >= 0 and idx_gear < len(tds) else ""
            
            # Track last rating in current season (oldest race = start of season rating)
            if in_current_season and row_rating:
                last_rating_in_season = row_rating
            
            # Match the specific race by race_index AND date
            # Race indices reset each season, so we need date to disambiguate
            try:
                race_index_match = int(row_race_index) == int(race_index)
            except (ValueError, TypeError):
                race_index_match = row_race_index == race_index
            
            # Date match - normalize both dates to handle DD/MM/YY vs DD/MM/YYYY
            normalized_row_date = normalize_date(row_date)
            date_match = (not normalized_race_date) or (normalized_row_date == normalized_race_date)
            
            if race_index_match and date_match:
                result["rating"] = row_rating
                result["gear"] = row_gear
                # Found exact match, no need to continue
                if race_date:
                    break
        
        result["start_of_season_rating"] = last_rating_in_season
        break
    
    return result


def fetch_horse_data(
    session: requests.Session,
    horse_id: str,
    race_index: str,
    race_date: str,
    horse_cache: Dict[str, Dict[str, str]],
    horse_cache_path: str,
    force_fresh: bool = False,
    fetched_this_run: Optional[set] = None,
) -> Dict[str, str]:
    """Fetch horse profile data, using cache when available.
    
    Args:
        session: The requests session
        horse_id: The horse ID (e.g., "HK_2021_G115")
        race_index: The race index (e.g., "036")
        race_date: The race date in DD/MM/YYYY format (e.g., "18/09/2024")
        horse_cache: Cache of horse profile data
        horse_cache_path: Path to save cache
        force_fresh: If True, bypass persistent cache (but still use session cache)
        fetched_this_run: Set of horse_ids already fetched fresh in this session
    """
    if fetched_this_run is None:
        fetched_this_run = set()
    
    cached = horse_cache.get(horse_id, {})
    race_key = f"race_{race_date}_{race_index}"
    
    # If we've already fetched this horse fresh in this run, use the in-memory cache
    already_fetched_fresh = horse_id in fetched_this_run
    
    # Check if we have the rating/gear for this specific race in our cache
    if race_key in cached and cached[race_key].get("rating"):
        # Use cache if: not forcing fresh, OR already fetched fresh this run
        if not force_fresh or already_fetched_fresh:
            print(f"    Using cached profile & form info: {horse_id}")
            return {**cached, **cached[race_key]}

    needs_zh = not cached.get("horsename_zh") or not cached.get("毛色")
    # Only force fetch if we haven't already fetched this horse fresh this run
    needs_fetch_profile = (force_fresh and not already_fetched_fresh) or horse_id not in horse_cache or needs_zh
    
    horse_html = None
    
    if needs_fetch_profile:
        print(f"    Fetching horse profile: {horse_id}")
        
        # Fetch English profile
        horse_html = fetch_html(session, HORSE_URL, params={"horseid": horse_id})
        horse_profile = parse_horse_profile(horse_html)
        
        # Fallback to otherhorse page if main page has no data
        if not horse_profile:
            other_html = fetch_html(session, OTHERHORSE_URL, params={"horseid": horse_id})
            horse_profile = parse_otherhorse_profile(other_html)
            if other_html and not horse_html:
                horse_html = other_html
        
        # Fetch Chinese profile
        horse_profile_zh = {}
        horse_zh_html = fetch_html(session, HORSE_URL_ZH, params={"horseid": horse_id})
        horse_profile_zh = parse_horse_profile_zh(horse_zh_html)
        
        # Fallback to otherhorse Chinese page
        if not horse_profile_zh.get("毛色"):
            other_zh_html = fetch_html(session, OTHERHORSE_URL_ZH, params={"horseid": horse_id})
            horse_profile_zh = {**horse_profile_zh, **parse_otherhorse_profile_zh(other_zh_html)}
        
        # Merge profile data
        cached = {**cached, **horse_profile, **horse_profile_zh}
        horse_cache[horse_id] = cached
        save_horse_cache(horse_cache_path, horse_cache)
        fetched_this_run.add(horse_id)
        time.sleep(0.3)
    else:
        print(f"    Using cached profile: {horse_id}")
    
    # Always fetch race-specific data (rating, gear) if not in cache
    if not horse_html:
        print(f"    Fetching horse form (rating/gear) + refreshing profile: {horse_id}")
        horse_html = fetch_html(session, HORSE_URL, params={"horseid": horse_id})
        
        # Since we're fetching the page anyway, update the profile too
        # (age, current location, trainer, season stakes, etc. can change)
        horse_profile = parse_horse_profile(horse_html)
        if horse_profile:
            cached = {**cached, **horse_profile}
        
        # Also refresh Chinese profile for dynamic fields
        horse_zh_html = fetch_html(session, HORSE_URL_ZH, params={"horseid": horse_id})
        horse_profile_zh = parse_horse_profile_zh(horse_zh_html)
        if horse_profile_zh:
            cached = {**cached, **horse_profile_zh}
        
        horse_cache[horse_id] = cached
        fetched_this_run.add(horse_id)
        time.sleep(0.2)
    
    # Parse ALL form rows at once and cache them
    all_form_rows = parse_all_horse_form_rows(horse_html)
    
    # Fallback to otherhorse page if no form data found
    if not all_form_rows:
        print(f"    Checking otherhorse form: {horse_id}")
        other_html = fetch_html(session, OTHERHORSE_URL, params={"horseid": horse_id})
        all_form_rows = parse_all_horse_form_rows(other_html)
    
    # Merge all form rows into cache
    for form_race_key, form_data in all_form_rows.items():
        cached[form_race_key] = form_data
    
    horse_cache[horse_id] = cached
    save_horse_cache(horse_cache_path, horse_cache)
    
    # Get the specific race we need
    horse_form = cached.get(race_key, {"rating": "", "gear": "", "start_of_season_rating": ""})
    
    # Combine profile with the specific race-specific data
    return {**cached, **horse_form}


def build_rows_for_race(
    session: requests.Session,
    race_date_ui: str,
    racecourse: str,
    race_no: int,
    horse_cache: Dict[str, Dict[str, str]],
    horse_cache_path: str,
    force_fresh: bool = False,
    fetched_this_run: Optional[set] = None,
) -> List[Dict[str, object]]:
    params = {"racedate": convert_date_ui_to_query(race_date_ui), "Racecourse": racecourse, "RaceNo": str(race_no)}
    local_html = fetch_html(session, LOCALRESULTS_URL, params=params)
    header = parse_race_header_info(local_html)
    race_index = header.get("race_index", "")
    meeting_track = header.get("meeting_track", "")

    section_html = fetch_html(session, SECTIONAL_URL, params={"racedate": race_date_ui, "RaceNo": str(race_no)})
    sectional = parse_sectional_table(section_html)

    rows = []
    for row in parse_localresults_table(local_html):
        horse_id = row.get("horse_id")
        section = sectional.get(horse_id) if horse_id else None
        positions = section.positions if section else [""] * 6
        lbws = section.lbws if section else [""] * 6
        sectiontimes = section.sectiontimes if section else [""] * 6

        # Fetch horse data (profile + form + Chinese)
        horse_data = fetch_horse_data(
            session, horse_id, race_index, race_date_ui, horse_cache, horse_cache_path,
            force_fresh=force_fresh, fetched_this_run=fetched_this_run
        ) if horse_id else {}

        # Running positions from sectional table (positions only)
        running_positions = " ".join([p for p in positions if p])

        meeting_track_code = "ST" if "Sha Tin" in meeting_track else "HV" if "Happy Valley" in meeting_track else ""

        track_type = "All Weather Track" if "AWT" in header.get("race_track", "").upper() else "Turf"

        rows.append(
            {
                "horse_id": horse_id,
                "race_date": convert_date_ui_to_iso(race_date_ui),
                "race_number": race_no,
                "race_index": race_index,
                "horse_name": row.get("horse_name"),
                "horsename_zh": horse_data.get("horsename_zh", ""),
                "horse_number": row.get("horse_number"),
                "place": row.get("place"),
                "finish_time_seconds": parse_time_seconds(row.get("finish_time", "")),
                "lbw": row.get("lbw"),
                "draw": row.get("draw"),
                "going": abbreviate_going(header.get("going", "")),
                "running_positions": running_positions,
                "lbws": "; ".join(lbws),
                "sectiontimes": "; ".join(sectiontimes),
                "positions": "; ".join(positions),
                "gear": horse_data.get("gear", ""),
                "rating": horse_data.get("rating", ""),
                "distance": int(header.get("distance") or 0) or None,
                "jockey": row.get("jockey"),
                "trainer": row.get("trainer"),
                "actual_weight": int(row.get("actual_weight") or 0) or None,
                "win_odds": row.get("win_odds"),
                "declared_weight": row.get("declared_weight"),
                "race_course": header.get("course") or header.get("race_track"),
                "race_class": header.get("race_class"),
                "race_track": meeting_track_code,
                "track_type": track_type,
                "colour": horse_data.get("colour", ""),
                "sex": horse_data.get("sex", ""),
                "country": horse_data.get("country", ""),
                "age": horse_data.get("age", ""),
                "import_type": horse_data.get("import_type", ""),
                "season_stakes": horse_data.get("season_stakes", ""),
                "total_stakes": horse_data.get("total_stakes", ""),
                "no_of_123_starts": horse_data.get("no_of_123_starts", ""),
                "no_of_starts_in_past_10_race_meetings": horse_data.get("no_of_starts_in_past_10_race_meetings", ""),
                "current_stable_location_arrival_date": horse_data.get("current_stable_location_arrival_date", ""),
                "import_date": horse_data.get("import_date", ""),
                "owner": horse_data.get("owner", ""),
                "current_rating": horse_data.get("current_rating", ""),
                "start_of_season_rating": horse_data.get("start_of_season_rating") or horse_data.get("current_rating", ""),
                "sire": horse_data.get("sire", ""),
                "dam": horse_data.get("dam", ""),
                "dam_sire": horse_data.get("dam_sire", ""),
                "last_rating": horse_data.get("last_rating", ""),
                "毛色": horse_data.get("毛色", ""),
                "性別": horse_data.get("性別", ""),
                "出生地": horse_data.get("出生地", ""),
                "進口類別": horse_data.get("進口類別", ""),
                "現在位置 (到達日期)": horse_data.get("現在位置 (到達日期)", ""),
                "練馬師": horse_data.get("練馬師", ""),
                "馬主": horse_data.get("馬主", ""),
                "race_url": f"{SECTIONAL_URL}?racedate={race_date_ui}&RaceNo={race_no}",
                "horse_url": f"{HORSE_URL}?horseid={horse_id}" if horse_id else None,
                "horse_url_chinese": f"{HORSE_URL_ZH}?horseid={horse_id}" if horse_id else None,
                "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    print(f"    Parsed {len(rows)} horses")
    return rows


def parse_dates_arg(value: str) -> List[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


class RaceCache:
    """Cache scraped race data using CSV + DuckDB."""

    def __init__(self, csv_path: str):
        self.csv_path = Path(csv_path)
        self.conn = duckdb.connect(":memory:")
        self._table_created = False
        self._load()

    def _load(self) -> None:
        """Load existing CSV into DuckDB."""
        if self.csv_path.exists():
            try:
                self.conn.execute(
                    f"CREATE TABLE races AS SELECT * FROM read_csv_auto('{self.csv_path}')"
                )
                count = self.conn.execute("SELECT COUNT(*) FROM races").fetchone()[0]
                print(f"Loaded race cache: {count} rows from {self.csv_path}")
                self._table_created = True
            except Exception as e:
                print(f"Warning: Could not load race cache: {e}")
                self._table_created = False
        else:
            self._table_created = False

    def is_race_scraped(self, race_date: str, race_number: int) -> bool:
        """Check if a race has already been scraped."""
        if not self._table_created:
            return False
        try:
            result = self.conn.execute(
                "SELECT 1 FROM races WHERE race_date = ? AND race_number = ? LIMIT 1",
                [race_date, race_number]
            ).fetchone()
            return result is not None
        except Exception:
            return False

    def add_rows(self, rows: List[Dict[str, object]]) -> None:
        """Add new rows to the cache."""
        if not rows:
            return
        df = pd.DataFrame(rows)
        # Ensure proper types for CSV output
        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].astype(str).replace('None', '')
        try:
            if not self._table_created:
                # Save to CSV first, then load into DuckDB for proper schema detection
                df.to_csv(self.csv_path, index=False)
                self.conn.execute(
                    f"CREATE TABLE races AS SELECT * FROM read_csv_auto('{self.csv_path}')"
                )
                self._table_created = True
            else:
                # Append to existing CSV and reload
                df.to_csv(self.csv_path, mode='a', header=False, index=False)
                # Reload the table from CSV
                self.conn.execute("DROP TABLE races")
                self.conn.execute(
                    f"CREATE TABLE races AS SELECT * FROM read_csv_auto('{self.csv_path}')"
                )
        except Exception as e:
            print(f"Warning: Could not add rows to cache: {e}")

    def save(self) -> None:
        """Save the cache to CSV (already saved incrementally)."""
        if self._table_created:
            try:
                count = self.conn.execute("SELECT COUNT(*) FROM races").fetchone()[0]
                print(f"Race cache: {count} rows in {self.csv_path}")
            except Exception as e:
                print(f"Warning: Could not check race cache: {e}")

    def get_all_rows(self) -> pd.DataFrame:
        """Get all cached rows as a DataFrame."""
        try:
            return self.conn.execute("SELECT * FROM races").df()
        except Exception:
            return pd.DataFrame()


def load_horse_cache(path: str) -> Dict[str, Dict[str, str]]:
    cache_path = Path(path)
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_horse_cache(path: str, cache: Dict[str, Dict[str, str]]) -> None:
    cache_path = Path(path)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape HKJC local results and sectional times.")

    date_group = parser.add_mutually_exclusive_group()
    date_group.add_argument("--dates", help="Comma-separated dates in DD/MM/YYYY format.")
    date_group.add_argument(
        "--all-dates",
        action="store_true",
        help="Scrape all available dates from dropdown.",
    )
    date_group.add_argument(
        "--latest-dates",
        "--latest",
        dest="latest_dates",
        type=int,
        default=0,
        help="Scrape only the latest N available meeting dates (e.g., --latest-dates 10).",
    )
    parser.add_argument("--output", default="hkjc_results.xlsx", help="Output Excel file.")
    parser.add_argument("--max-races", type=int, default=0, help="Limit total number of races to scrape (0 = all).")
    parser.add_argument("--sleep", type=float, default=0.3, help="Delay between requests.")
    parser.add_argument("--horse-cache", default=".horse_cache.json", help="Path to horse cache JSON.")
    parser.add_argument("--race-cache", default=".race_cache.csv", help="Path to race cache CSV.")
    parser.add_argument("--no-cache", action="store_true", help="Ignore race cache and rescrape all.")
    parser.add_argument("--force", dest="no_cache", action="store_true",
                        help="Alias for --no-cache (force re-scrape, bypassing race + horse profile cache).")
    args = parser.parse_args()

    session = requests.Session()

    # Initialize race cache
    race_cache = RaceCache(args.race_cache) if not args.no_cache else None

    base_html = fetch_html(session, LOCALRESULTS_URL)
    available_dates = parse_available_dates(base_html)
    print(f"Available dates: {len(available_dates)}")

    if getattr(args, "latest_dates", 0):
        latest_n = max(0, int(args.latest_dates))
        dates = available_dates[:latest_n]
    elif args.all_dates:
        dates = available_dates
    elif args.dates:
        dates = parse_dates_arg(args.dates)
    else:
        dates = available_dates[:1]

    print(f"Selected dates: {', '.join(dates)}")

    all_rows: List[Dict[str, object]] = []
    horse_cache: Dict[str, Dict[str, str]] = load_horse_cache(args.horse_cache)
    if horse_cache:
        print(f"Loaded horse cache: {len(horse_cache)} entries")
    
    # Track horses fetched fresh in this run (to avoid re-fetching same horse multiple times)
    horses_fetched_this_run: set = set()

    # Track progress
    races_processed = 0  # Total races (scraped + cached)
    races_scraped = 0
    races_skipped = 0
    max_races_limit = args.max_races if args.max_races > 0 else float('inf')
    limit_reached = False

    for race_date_ui in dates:
        if limit_reached:
            break
        print(f"\nProcessing meeting date: {race_date_ui}")
        race_date_iso = convert_date_ui_to_iso(race_date_ui)
        
        # Check if we should scrape this date (might be completely cached)
        # We still need the race list to know if individual races are cached
        print(f"  Fetching race list for {race_date_ui}...")
        resultsall_html = fetch_html(session, RESULTSALL_URL, params={"racedate": convert_date_ui_to_query(race_date_ui)})
        racecourse, race_numbers = parse_racecourse_and_numbers(resultsall_html)
        print(f"Racecourse: {racecourse or 'Unknown'} | Races: {race_numbers}")
        if not race_numbers:
            continue
        for race_no in race_numbers:
            # Check if we've hit the limit (count both scraped and cached towards limit)
            if races_processed >= max_races_limit:
                limit_reached = True
                print(f"\nReached max-races limit ({args.max_races})")
                break
            races_processed += 1
            progress = f"[{races_processed}/{args.max_races}]" if args.max_races else f"[{races_processed}]"
            # Check if race already scraped
            if race_cache and race_cache.is_race_scraped(race_date_iso, race_no):
                races_skipped += 1
                print(f"  {progress} Race #{race_no} on {race_date_ui} [CACHED - skipping]")
                continue
            races_scraped += 1
            print(f"  {progress} Race #{race_no} on {race_date_ui} [SCRAPING]")
            rows = build_rows_for_race(
                session, race_date_ui, racecourse, race_no, horse_cache, args.horse_cache,
                force_fresh=args.no_cache, fetched_this_run=horses_fetched_this_run
            )
            all_rows.extend(rows)
            if race_cache:
                race_cache.add_rows(rows)
            time.sleep(args.sleep)

    print(f"\n--- Summary ---")
    print(f"Races processed: {races_processed}")
    print(f"Races scraped: {races_scraped}")
    print(f"Races skipped (cached): {races_skipped}")
    print(f"Horses in cache: {len(horse_cache)}")

    # Combine newly scraped rows with cached rows for output
    if race_cache:
        cached_df = race_cache.get_all_rows()
        if not cached_df.empty:
            new_df = pd.DataFrame(all_rows)
            # Only use new rows for Excel if no new data, use cached
            if new_df.empty:
                df = cached_df
            else:
                df = cached_df  # All data is already in cache including new rows
        else:
            df = pd.DataFrame(all_rows)
    else:
        df = pd.DataFrame(all_rows)

    print(f"Total rows collected: {len(df)}")
    if not df.empty:
        # Sort by race_date descending (latest first), then race_number ascending
        # Sort by race_date descending (latest first), then race_number ascending
        if "race_date" in df.columns and "race_number" in df.columns:
            df["race_date"] = pd.to_datetime(df["race_date"], errors="coerce")
            df = df.sort_values(["race_date", "race_number"], ascending=[False, True]).reset_index(drop=True)
        # OneDrive-safe write: build to TEMP first, then move into place.
        safe_excel_write(
            Path(args.output),
            lambda tmp: df.to_excel(tmp, index=False, sheet_name="Sample Data"),
        )
        print(f"Saved Excel: {args.output}")
    else:
        print("No new data to save.")

    if race_cache:
        race_cache.save()
    save_horse_cache(args.horse_cache, horse_cache)
    print(f"Saved horse cache: {args.horse_cache}")


if __name__ == "__main__":
    main()

