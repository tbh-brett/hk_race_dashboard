#!/usr/bin/env python3
"""
HKJC Vet Record Scraper + PDF — scrape_hkjc_vet.py
====================================================
Scrapes HKJC veterinary records for all races in a meeting,
classifies injury types by severity, and generates:
  - JSON:  reports/vet_report_YYYYMMDD.json
  - PDF:   reports/vet_report_YYYYMMDD.pdf

Usage:
    python scrape_hkjc_vet.py --date 2026-04-01
    python scrape_hkjc_vet.py --date 2026-04-01 --venue HV
    python scrape_hkjc_vet.py --date 2026-04-01 --no-pdf

Concern flags (relative to race date):
    RED   — Recent (<90 days) physical / respiratory / cardiac injury
    AMBER — Cleared-recent or semi-recent (90-180 days) concern
    INFO  — Historical physical injury (180-400 days)
    (none)— Procedural entry or old performance note — not shown
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

BASE_DIR    = Path(__file__).parent
CACHE_DIR   = BASE_DIR / "cache"
REPORTS_DIR = BASE_DIR / "reports"

BASE_URL = "https://racing.hkjc.com"
VET_URL  = f"{BASE_URL}/en-us/local/information/veterinaryrecord"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": BASE_URL + "/",
    "Connection": "keep-alive",
}

# ─────────────────────────────────────────────────────────────────────────────
# Injury classification
# ─────────────────────────────────────────────────────────────────────────────

INJURY_CATEGORIES: Dict[str, List[str]] = {
    # Evaluated in order — first match wins
    "PHYSICAL": [
        r"lame(?:\s+\w+)*",
        r"fracture",
        r"bone\s+(?:injury|chip|spur|plate|bruise)",
        r"splint(?:\s+bone)?",
        r"tendon",
        r"ligament",
        r"fetlock",
        r"pastern",
        r"coffin\s+bone",
        r"sesamoid",
        r"navicular",
        r"surgery|surgical",
        r"withdrawn\s+from\s+racing",
        r"muscle\s+(?:injury|tear|strain)",
        r"shin\s+soreness",
        r"joint\s+(?:injury|effusion|inflammation)",
        r"knee",
        r"swelling",
        r"wound",
        r"abscess",
    ],
    "RESPIRATORY": [
        r"blood\s+in\s+trachea",
        r"substantial\s+blood",
        r"post[\s\-]?race\s+scop",
        r"bleed(?:er|ing)",
        r"roarer",
        r"epiglottic",
        r"throat\s+surgery",
        r"laryngeal",
        r"exercise[\s\-]induced",
        r"respiratory",
    ],
    "CARDIAC": [
        r"heart\s+(?:irregularity|condition|murmur)",
        r"atrial\s+fibrillation",
        r"cardiac",
    ],
    "PERFORMANCE": [
        r"unacceptable\s+performance",
        r"disappointing\s+performance",
        r"racing\s+manners",
        r"rider\s+concerned",
        r"erratic",
    ],
    "PROCEDURAL": [
        r"castration",
        r"inadvertent\s+treatment",
        r"inappetence",
        r"fever",
        r"vaccin",
        r"dental",
        r"infection",
    ],
}

CAT_SEVERITY = {
    "PHYSICAL": 1, "RESPIRATORY": 2, "CARDIAC": 3,
    "PERFORMANCE": 4, "PROCEDURAL": 5, "UNKNOWN": 6,
}

RECENT_DAYS       = 90
SEMI_RECENT_DAYS  = 180
MAX_SHOW_PHYSICAL = 400
MAX_SHOW_PERF     = 90


def classify_injury(details: str) -> str:
    dl = details.lower()
    for cat, patterns in INJURY_CATEGORIES.items():
        for pat in patterns:
            if re.search(pat, dl):
                return cat
    return "UNKNOWN"


def parse_hkjc_date(date_str: str) -> Optional[date]:
    s = (date_str or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except ValueError:
        return None


def compute_flag(category: str, d_ago: Optional[int],
                 passed_date: Optional[date], race_date: date) -> str:
    if d_ago is None:
        return ""
    if category in ("PHYSICAL", "RESPIRATORY", "CARDIAC"):
        if d_ago <= RECENT_DAYS:
            return "RED"
        elif d_ago <= SEMI_RECENT_DAYS:
            return "AMBER"
        elif d_ago <= MAX_SHOW_PHYSICAL:
            return "INFO"
        return ""
    elif category == "PERFORMANCE":
        return "AMBER" if d_ago <= RECENT_DAYS else ""
    elif category == "PROCEDURAL":
        return ""
    else:
        if d_ago <= RECENT_DAYS and passed_date is None:
            return "INFO"
        return ""


def concern_score(flag: str, category: str) -> int:
    sev = CAT_SEVERITY.get(category, 6)
    if flag == "RED":
        return 10 - sev
    if flag == "AMBER":
        return 5 - min(sev, 4)
    if flag == "INFO":
        return 1
    return 0


FLAG_LABEL = {"RED": "HIGH", "AMBER": "MODERATE", "INFO": "INFO", "": ""}


# ─────────────────────────────────────────────────────────────────────────────
# Web scraping
# ─────────────────────────────────────────────────────────────────────────────

_session: Optional[requests.Session] = None


def get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(HEADERS)
    return _session


def fetch_vet_page(date_str: str, racecourse: str, race_no: int,
                   sleep_s: float = 1.5) -> Optional[BeautifulSoup]:
    date_param = date_str.replace("-", "/")
    params = {
        "racedate":   date_param,
        "Racecourse": racecourse.upper(),
        "RaceNo":     str(race_no),
    }
    try:
        resp = get_session().get(VET_URL, params=params, timeout=25)
        resp.raise_for_status()
        time.sleep(sleep_s)
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        print(f"    x Request failed: {exc}")
        return None


def find_vet_table(soup: BeautifulSoup):
    # Primary: look for table id="OVR1" (main runners vet table)
    tbl = soup.find("table", id="OVR1")
    if tbl:
        return tbl
    # Fallback: find a table whose first row contains "Horse" + "Detail" in <td>
    for tbl in soup.find_all("table"):
        first_row = tbl.find("tr")
        if not first_row:
            continue
        cells = first_row.find_all(["th", "td"])
        texts = [c.get_text(strip=True).lower() for c in cells]
        if any("horse" in t for t in texts) and any("detail" in t for t in texts):
            return tbl
    return None


def parse_vet_table(tbl, race_date: date) -> List[Dict]:
    rows = tbl.find_all("tr")
    current: Optional[Dict] = None
    horses: List[Dict] = []

    for row in rows:
        cells = row.find_all("td")
        if not cells or len(cells) < 5:
            continue

        hn_raw    = cells[0].get_text(strip=True)
        hname_raw = cells[1].get_text(strip=True)
        date_raw  = cells[2].get_text(strip=True)
        det_raw   = cells[3].get_text(strip=True)
        pass_raw  = cells[4].get_text(strip=True)

        details = re.sub(r"\s+", " ", det_raw).strip()

        # Detect standby section — stop parsing
        combined = (hn_raw + hname_raw + date_raw).lower()
        if "stand" in combined and "starter" in combined:
            break

        # New horse (Horse No. cell is numeric)
        if hn_raw.strip():
            try:
                horse_no = int(hn_raw.strip())
            except ValueError:
                continue
            current = {
                "horse_no":   horse_no,
                "horse_name": hname_raw.strip(),
                "records":    [],
                "max_flag":   "",
                "max_score":  0,
            }
            horses.append(current)

        if current is None or not details:
            continue

        rec_date  = parse_hkjc_date(date_raw)
        pass_date = parse_hkjc_date(pass_raw)
        d_ago     = (race_date - rec_date).days if rec_date else None

        cat  = classify_injury(details)
        flag = compute_flag(cat, d_ago, pass_date, race_date)

        # Show/hide filter
        if d_ago is not None:
            if cat in ("PHYSICAL", "RESPIRATORY", "CARDIAC"):
                if d_ago > MAX_SHOW_PHYSICAL:
                    continue
            elif cat == "PERFORMANCE":
                if d_ago > MAX_SHOW_PERF:
                    continue
            elif cat == "PROCEDURAL":
                continue
            else:
                if d_ago > RECENT_DAYS:
                    continue

        score = concern_score(flag, cat)
        rec = {
            "date":              rec_date.strftime("%Y-%m-%d") if rec_date else None,
            "date_display":      date_raw.strip(),
            "details":           details,
            "passed_on":         pass_date.strftime("%Y-%m-%d") if pass_date else None,
            "passed_on_display": pass_raw.strip() or None,
            "category":          cat,
            "days_ago":          d_ago,
            "flag":              flag,
            "concern_score":     score,
        }
        current["records"].append(rec)
        if score > current["max_score"]:
            current["max_score"] = score
            current["max_flag"]  = flag

    return [h for h in horses if h["records"]]


# ─────────────────────────────────────────────────────────────────────────────
# Race-number discovery
# ─────────────────────────────────────────────────────────────────────────────

def get_race_numbers(date_str: str) -> List[int]:
    cache_file = CACHE_DIR / f"racecard_{date_str}.json"
    if cache_file.exists():
        try:
            with open(cache_file, encoding="utf-8") as f:
                data = json.load(f)
            nums = []
            for r in data.get("races", []):
                meta = r.get("meta", r)
                rn = meta.get("race_number")
                if rn:
                    nums.append(int(rn))
            return sorted(set(nums)) or list(range(1, 12))
        except Exception:
            pass
    return list(range(1, 12))


# ─────────────────────────────────────────────────────────────────────────────
# Main scrape loop
# ─────────────────────────────────────────────────────────────────────────────

def scrape_meeting(date_str: str, racecourse: str, race_nums: List[int],
                   sleep_s: float = 1.5) -> Dict[int, List[Dict]]:
    race_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    results: Dict[int, List[Dict]] = {}

    for rn in race_nums:
        print(f"  R{rn:>2d} ... ", end="", flush=True)
        soup = fetch_vet_page(date_str, racecourse, rn, sleep_s)
        if soup is None:
            print("FETCH ERROR")
            continue

        tbl = find_vet_table(soup)
        if tbl is None:
            page_text = soup.get_text()
            if any(kw in page_text.lower() for kw in ("no record", "no data", "not found")):
                print("no records on page")
            else:
                print("table not found (race may not exist)")
            results[rn] = []
            continue

        horses = parse_vet_table(tbl, race_date)
        results[rn] = horses

        if not horses:
            print("clean (no concerning records)")
        else:
            n_red   = sum(1 for h in horses if h["max_flag"] == "RED")
            n_amber = sum(1 for h in horses if h["max_flag"] == "AMBER")
            n_info  = sum(1 for h in horses if h["max_flag"] == "INFO")
            print(f"{len(horses)} flagged  "
                  f"(RED:{n_red}  AMBER:{n_amber}  INFO:{n_info})")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# JSON output
# ─────────────────────────────────────────────────────────────────────────────

def build_output(date_str: str, racecourse: str, race_results: Dict[int, List[Dict]],
                 meeting_title: str) -> Dict:
    races = []
    for rn in sorted(race_results):
        races.append({"race_number": rn, "horses": race_results[rn]})
    return {
        "race_date":     date_str,
        "racecourse":    racecourse,
        "meeting_title": meeting_title,
        "scraped_at":    datetime.now().strftime("%Y-%m-%d %H:%M"),
        "races":         races,
    }


def save_json(data: Dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  JSON saved: {path.name}")


# ─────────────────────────────────────────────────────────────────────────────
# PDF generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_vet_pdf(data: Dict, out_path: Path) -> None:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer, KeepTogether)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_CENTER

    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=landscape(A4),
        leftMargin=12*mm, rightMargin=12*mm,
        topMargin=14*mm, bottomMargin=14*mm,
    )
    styles = getSampleStyleSheet()

    HDR_BG    = colors.HexColor("#2C3E50")
    RED_BG    = colors.HexColor("#FDEDEC")
    RED_TXT   = colors.HexColor("#C0392B")
    AMBER_BG  = colors.HexColor("#FEF9E7")
    AMBER_TXT = colors.HexColor("#D35400")
    INFO_BG   = colors.HexColor("#EBF5FB")
    INFO_TXT  = colors.HexColor("#1A5276")
    ALT_ROW   = colors.HexColor("#F5F6FA")

    def ps(name, **kw):
        base = kw.pop("parent", styles["Normal"])
        return ParagraphStyle(name, parent=base, **kw)

    page_title = ps("PT",  parent=styles["Title"],    fontSize=15, spaceAfter=3*mm)
    subtitle   = ps("Sub", fontSize=8.5, leading=11,  spaceAfter=1*mm,
                     textColor=colors.HexColor("#444444"))
    race_hdr   = ps("RH",  parent=styles["Heading2"], fontSize=11,
                     spaceBefore=5*mm, spaceAfter=1.5*mm)
    horse_hdr  = ps("HH",  fontSize=10, fontName="Helvetica-Bold",
                     spaceBefore=3*mm, spaceAfter=1*mm)
    legend     = ps("Leg", fontSize=7.5, textColor=colors.HexColor("#666666"),
                     spaceBefore=3*mm, spaceAfter=4*mm)
    cell       = ps("C",   fontSize=8,  leading=10)
    cell_bold  = ps("CB",  fontSize=8,  leading=10, fontName="Helvetica-Bold")
    hdr_white  = ps("HW",  fontSize=7.5, leading=9,
                     fontName="Helvetica-Bold", textColor=colors.white)

    def hdr_p(text):
        return Paragraph(f"<b>{text}</b>", hdr_white)

    story = []

    meeting_title = data.get("meeting_title", "VET RECORDS REPORT")
    story.append(Paragraph(f"VET RECORD ALERTS  -  {meeting_title}", page_title))
    story.append(Paragraph(
        f"Race Date: {data['race_date']}  |  Racecourse: {data['racecourse']}  |  "
        f"Scraped: {data['scraped_at']}",
        subtitle))
    story.append(Paragraph(
        "<b>HIGH (RED)</b> - Recent (&lt;90 days) physical / respiratory / cardiac injury    "
        "<b>MODERATE (AMBER)</b> - Cleared-recently or semi-recent (90-180 days)    "
        "<b>INFO</b> - Historical physical injury (180-400 days)",
        legend))

    # ── Summary table ────────────────────────────────────────────────────────
    flagged_all = []
    for rdata in data["races"]:
        rn = rdata["race_number"]
        for h in rdata["horses"]:
            if h["max_flag"] in ("RED", "AMBER", "INFO"):
                flagged_all.append((rn, h["horse_no"], h["horse_name"],
                                    h["max_flag"], h["max_score"]))
    flagged_all.sort(key=lambda x: (-x[4], x[0], x[1]))

    if flagged_all:
        story.append(Paragraph("<b>FLAGGED HORSES  -  SUMMARY</b>", race_hdr))
        sum_rows = [[hdr_p("Race"), hdr_p("No."), hdr_p("Horse Name"),
                     hdr_p("Alert"), hdr_p("Category")]]
        for rn, hno, hname, flg, _ in flagged_all:
            rdata_match = next((r for r in data["races"] if r["race_number"] == rn), None)
            worst_cat = ""
            if rdata_match:
                hmatch = next((h for h in rdata_match["horses"]
                               if h["horse_no"] == hno), None)
                if hmatch and hmatch["records"]:
                    top = sorted(hmatch["records"], key=lambda r: -r["concern_score"])
                    worst_cat = top[0]["category"]

            fc = (RED_TXT.hexval() if flg == "RED" else
                  "#D35400"        if flg == "AMBER" else
                  INFO_TXT.hexval())

            sum_rows.append([
                Paragraph(f"R{rn}", cell),
                Paragraph(str(hno), cell),
                Paragraph(hname, cell_bold),
                Paragraph(f'<font color="{fc}"><b>{FLAG_LABEL[flg]}</b></font>', cell),
                Paragraph(worst_cat, cell),
            ])

        sum_tbl = Table(sum_rows, colWidths=[16*mm, 12*mm, 80*mm, 38*mm, 30*mm])
        sum_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), HDR_BG),
            ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#AAAAAA")),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, ALT_ROW]),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(sum_tbl)
        story.append(Spacer(1, 6*mm))
    else:
        story.append(Paragraph(
            "No horses flagged with recent veterinary concerns for this meeting.", subtitle))
        story.append(Spacer(1, 4*mm))

    # ── Per-race detail ──────────────────────────────────────────────────────
    COL_W = [29*mm, 95*mm, 30*mm, 34*mm]

    for rdata in data["races"]:
        rn     = rdata["race_number"]
        horses = rdata["horses"]
        if not horses:
            continue

        race_elements = [Paragraph(f"Race {rn}  -  Veterinary Record Detail", race_hdr)]

        for h in sorted(horses, key=lambda x: -x["max_score"]):
            hno   = h["horse_no"]
            hname = h["horse_name"]
            flg   = h["max_flag"]

            hc = (RED_TXT.hexval()  if flg == "RED" else
                  "#D35400"         if flg == "AMBER" else
                  INFO_TXT.hexval() if flg == "INFO" else "#333333")

            race_elements.append(Paragraph(
                f'<font color="{hc}"><b>#{hno} {hname}</b></font>', horse_hdr))

            rec_rows = [[hdr_p("Date"), hdr_p("Details"),
                         hdr_p("Cleared On"), hdr_p("Alert")]]
            style_cmds = [
                ("BACKGROUND",    (0, 0), (-1, 0), HDR_BG),
                ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
                ("VALIGN",        (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING",    (0, 0), (-1, -1), 2.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ]

            for i, rec in enumerate(h["records"], 1):
                rf  = rec["flag"]
                cat = rec["category"]
                d_disp    = rec.get("date_display") or rec.get("date") or ""
                d_ago     = rec.get("days_ago")
                days_note = f" ({d_ago}d ago)" if d_ago is not None else ""
                clr_disp  = rec.get("passed_on_display") or rec.get("passed_on") or "Not cleared"
                cat_badge = f" [{cat}]" if cat not in ("UNKNOWN", "PROCEDURAL") else ""
                detail_txt = f"{rec['details']}{cat_badge}"

                if rf == "RED":
                    style_cmds.append(("BACKGROUND", (0, i), (-1, i), RED_BG))
                    fc = RED_TXT.hexval()
                    det_para = Paragraph(f"<b>{detail_txt}</b>", cell_bold)
                elif rf == "AMBER":
                    style_cmds.append(("BACKGROUND", (0, i), (-1, i), AMBER_BG))
                    fc = "#D35400"
                    det_para = Paragraph(f"<b>{detail_txt}</b>", cell_bold)
                elif rf == "INFO":
                    style_cmds.append(("BACKGROUND", (0, i), (-1, i), INFO_BG))
                    fc = INFO_TXT.hexval()
                    det_para = Paragraph(detail_txt, cell)
                else:
                    fc = "#888888"
                    det_para = Paragraph(detail_txt, cell)

                fl_label = FLAG_LABEL.get(rf, "")
                fl_para  = (Paragraph(
                    f'<font color="{fc}"><b>{fl_label}</b></font>', cell)
                    if fl_label else Paragraph("", cell))

                rec_rows.append([
                    Paragraph(f"{d_disp}{days_note}", cell),
                    det_para,
                    Paragraph(clr_disp, cell),
                    fl_para,
                ])

            rec_tbl = Table(rec_rows, colWidths=COL_W)
            rec_tbl.setStyle(TableStyle(style_cmds))
            race_elements.append(rec_tbl)

        story.append(KeepTogether(race_elements[:4]))
        for elem in race_elements[4:]:
            story.append(elem)

    doc.build(story)
    print(f"  PDF saved: {out_path.name}")


# ─────────────────────────────────────────────────────────────────────────────
# Venue detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_venue_from_cache(date_str: str) -> str:
    cache_file = CACHE_DIR / f"racecard_{date_str}.json"
    if cache_file.exists():
        try:
            with open(cache_file, encoding="utf-8") as f:
                data = json.load(f)
            for race in data.get("races", []):
                meta = race.get("meta", race)
                rc = meta.get("racecourse") or meta.get("venue") or ""
                if rc.upper() in ("ST", "HV"):
                    return rc.upper()
        except Exception:
            pass
    return "ST"


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="HKJC Vet Record Scraper")
    p.add_argument("--date",   required=True, help="Meeting date YYYY-MM-DD")
    p.add_argument("--venue",  default=None,  help="ST or HV (auto-detect if omitted)")
    p.add_argument("--sleep",  type=float, default=1.5,
                   help="Seconds between requests (default 1.5)")
    p.add_argument("--no-pdf", action="store_true", help="Skip PDF generation")
    p.add_argument("--title",  default=None, help="Meeting title for PDF header")
    return p.parse_args()


def main():
    args = parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(f"ERROR: Invalid date '{args.date}' — use YYYY-MM-DD")
        sys.exit(1)

    date_compact = args.date.replace("-", "")
    racecourse   = (args.venue or detect_venue_from_cache(args.date)).upper()
    race_nums    = get_race_numbers(args.date)

    if args.title:
        meeting_title = args.title
    else:
        dt = datetime.strptime(args.date, "%Y-%m-%d")
        venue_full = {"ST": "SHA TIN", "HV": "HAPPY VALLEY"}.get(racecourse, racecourse)
        day_name   = dt.strftime("%A").upper()
        month_name = dt.strftime("%B").upper()
        meeting_title = f"{venue_full} — {day_name}, {dt.day} {month_name} {dt.year}"

    print(f"\n{'='*60}")
    print(f"HKJC VET RECORD SCRAPER")
    print(f"  Date:   {args.date}  |  Venue: {racecourse}")
    print(f"  Races:  {race_nums}")
    print(f"{'='*60}\n")

    race_results = scrape_meeting(args.date, racecourse, race_nums, args.sleep)

    out_data = build_output(args.date, racecourse, race_results, meeting_title)

    json_path = REPORTS_DIR / f"vet_report_{date_compact}.json"
    save_json(out_data, json_path)

    if not args.no_pdf:
        pdf_path = REPORTS_DIR / f"vet_report_{date_compact}.pdf"
        try:
            generate_vet_pdf(out_data, pdf_path)
        except ImportError:
            print("  reportlab not installed — PDF skipped. pip install reportlab")

    all_red   = sum(1 for r in out_data["races"]
                    for h in r["horses"] if h["max_flag"] == "RED")
    all_amber = sum(1 for r in out_data["races"]
                    for h in r["horses"] if h["max_flag"] == "AMBER")
    print(f"\n{'='*60}")
    print(f"VET SCRAPE COMPLETE — {all_red} RED, {all_amber} AMBER alerts")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
