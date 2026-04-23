#!/usr/bin/env python3
"""
HKJC Meeting Orchestrator — run_meeting.py
============================================
Automates the full pipeline: scrape race card -> generate analysis -> PDF + JSON.

Usage:
    python run_meeting.py --date 2026-04-05
    python run_meeting.py --date 2026-04-05 --no-cache
    python run_meeting.py --date 2026-04-05 --going-turf "Good to Firm"
    python run_meeting.py --date 2026-04-05 --post-race   # add results + backtest

Pre-race pipeline:
  [1/4] Scrape race card
  [2/4] Scrape vet records
  [3/4] Generate analysis script
  [4/4] Run analysis -> PDF + text + JSON

Post-race (--post-race flag):
  [5a] Scrape actual results
  [5b] Run backtest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
PYTHON = sys.executable
TEMPLATE = BASE / "race_day_analysis_v3.4.8_template.py"
TEMPLATE_V44 = BASE / "race_day_analysis_v4.4.py"
SCRAPER = BASE / "scrape_hkjc_racecard.py"
VET_SCRAPER = BASE / "scrape_hkjc_vet.py"
RESULTS_SCRAPER = BASE / "scrape_hkjc_results.py"
BACKTEST = BASE / "backtest_model.py"
INCIDENT_SCRAPER = BASE / "scrape_hkjc_incident_reports.py"
RP_PHOTO_SCRAPER = BASE / "scrape_hkjc_rp_photos.py"
COMMENTARY = BASE / "race_commentary.py"
FORM_GUIDE_BUILDER = BASE / "build_form_guide.py"

# Day-of-week names for meeting title
DAY_NAMES = {
    0: "MONDAY", 1: "TUESDAY", 2: "WEDNESDAY",
    3: "THURSDAY", 4: "FRIDAY", 5: "SATURDAY", 6: "SUNDAY",
}

VENUE_FULL = {
    "ST": "SHA TIN",
    "HV": "HAPPY VALLEY",
}

MONTH_NAMES = {
    1: "JANUARY", 2: "FEBRUARY", 3: "MARCH", 4: "APRIL",
    5: "MAY", 6: "JUNE", 7: "JULY", 8: "AUGUST",
    9: "SEPTEMBER", 10: "OCTOBER", 11: "NOVEMBER", 12: "DECEMBER",
}


def parse_args():
    p = argparse.ArgumentParser(
        description="Orchestrate HKJC race card scraping + analysis pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--date", required=True,
                   help="Race date YYYY-MM-DD (e.g. 2026-04-05)")
    p.add_argument("--no-cache", action="store_true",
                   help="Force re-scrape (ignore cached race card)")
    p.add_argument("--going-turf", default="Good",
                   help="Assumed turf going (default: Good)")
    p.add_argument("--going-awt", default="Good",
                   help="Assumed AWT going (default: Good)")
    p.add_argument("--skip-scrape", action="store_true",
                   help="Skip scraping (use existing race card Excel)")
    p.add_argument("--skip-analysis", action="store_true",
                   help="Skip analysis (only scrape)")
    p.add_argument("--skip-sarr", action="store_true",
                   help="Skip the SARR model stage (e.g. when dashboard runs it separately)")
    p.add_argument("--post-race", action="store_true",
                   help="Scrape actual results and run backtest (post-race mode)")
    p.add_argument("--model", default="v4.4", choices=["v3.4.8", "v4.4"],
                   help="Model version to use (default: v4.4)")
    return p.parse_args()


def run_scraper(date_str: str, no_cache: bool) -> Path:
    """Run scrape_hkjc_racecard.py and return the output Excel path."""
    date_compact = date_str.replace("-", "")
    output = BASE / "racecards" / f"racecard_{date_compact}.xlsx"

    cmd = [PYTHON, str(SCRAPER), "--date", date_str]
    if no_cache:
        cmd.append("--no-cache")

    print(f"\n{'='*60}")
    print(f"[1/4] SCRAPING RACE CARD — {date_str}")
    print(f"{'='*60}")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(cmd, env=env, capture_output=False)
    if result.returncode != 0:
        print(f"ERROR: Scraper failed with exit code {result.returncode}")
        sys.exit(1)

    if not output.exists():
        print(f"ERROR: Expected output not found: {output}")
        sys.exit(1)

    return output


def detect_venue_from_cache(date_str: str) -> str:
    """Read venue from the scraper's cache JSON."""
    cache_file = BASE / "cache" / f"racecard_{date_str}.json"
    if cache_file.exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("racecourse", "ST")

    # Fallback: try to infer from the Excel data
    import pandas as pd
    date_compact = date_str.replace("-", "")
    xl = BASE / "racecards" / f"racecard_{date_compact}.xlsx"
    if xl.exists():
        df = pd.read_excel(xl, sheet_name="All Races", nrows=1)
        if "racecourse" in df.columns:
            return str(df["racecourse"].iloc[0])
    return "ST"


def detect_surface_mix(date_str: str) -> bool:
    """Check if meeting has AWT races (for title suffix)."""
    cache_file = BASE / "cache" / f"racecard_{date_str}.json"
    if cache_file.exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        for race in data.get("races", []):
            meta = race.get("meta", {})
            if "All Weather" in str(meta.get("surface", "")):
                return True
    return False


def build_meeting_title(date_str: str, venue: str) -> str:
    """Build meeting title like 'SHA TIN (AWT) — WEDNESDAY, 1 APRIL 2026'."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    day_name = DAY_NAMES[dt.weekday()]
    venue_full = VENUE_FULL.get(venue, venue)
    month_name = MONTH_NAMES[dt.month]

    # Check if meeting has AWT
    has_awt = detect_surface_mix(date_str)
    venue_suffix = " (AWT)" if has_awt and venue == "ST" else ""

    return f"{venue_full}{venue_suffix} — {day_name}, {dt.day} {month_name} {dt.year}"


def run_vet_scraper(date_str: str, venue: str) -> Path:
    """Run scrape_hkjc_vet.py and return the output JSON path."""
    date_compact = date_str.replace("-", "")
    json_out = BASE / "reports" / f"vet_report_{date_compact}.json"

    print(f"\n{'='*60}")
    print(f"[2/4] SCRAPING VET RECORDS — {date_str}")
    print(f"{'='*60}")

    cmd = [PYTHON, str(VET_SCRAPER), "--date", date_str, "--venue", venue.upper()]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(cmd, env=env, cwd=str(BASE), capture_output=False)
    if result.returncode != 0:
        print(f"WARNING: Vet scraper failed (exit code {result.returncode}) — continuing without vet data")
    else:
        print(f"  ✓ Vet report: {json_out.name}")

    return json_out


def generate_analysis_script(date_str: str, venue: str,
                             racecard_path: Path,
                             going_turf: str, going_awt: str) -> Path:
    """Copy template and patch the per-meeting config section."""
    date_compact = date_str.replace("-", "")
    output_script = BASE / f"race_day_analysis_{date_compact}_v3.4.8.py"

    meeting_title = build_meeting_title(date_str, venue)

    print(f"\n{'='*60}")
    print(f"[3/4] GENERATING ANALYSIS SCRIPT")
    print(f"  Meeting: {meeting_title}")
    print(f"  Script:  {output_script.name}")
    print(f"{'='*60}")

    content = TEMPLATE.read_text(encoding="utf-8")

    # Patch config section — replace the placeholder values
    replacements = {
        # Race card path
        r'RACE_CARD\s*=\s*BASE\s*/\s*"racecards"\s*/\s*"[^"]*".*':
            f'RACE_CARD    = BASE / "racecards" / "{racecard_path.name}"',
        # Sheet name
        r'SHEET_NAME\s*=\s*"[^"]*".*':
            f'SHEET_NAME   = "All Races"                                          # scraped output sheet',
        # USE_SCRAPED
        r'USE_SCRAPED\s*=\s*\w+.*':
            f'USE_SCRAPED  = True                                                  # scraped race card format',
        # Output PDF
        r'OUT_PDF\s*=\s*BASE\s*/\s*"reports"\s*/\s*"[^"]*"':
            f'OUT_PDF  = BASE / "reports" / "race_day_report_{date_compact}_v3.4.8.pdf"',
        # Output TEXT
        r'OUT_TEXT\s*=\s*BASE\s*/\s*"reports"\s*/\s*"[^"]*"':
            f'OUT_TEXT = BASE / "reports" / "race_day_analysis_{date_compact}_v3.4.8.txt"',
        # Meeting title
        r'MEETING_TITLE\s*=\s*"[^"]*"':
            f'MEETING_TITLE = "{meeting_title}"',
        # Meeting venue
        r'MEETING_VENUE\s*=\s*"[^"]*"':
            f'MEETING_VENUE = "{venue}"',
        # Going assumptions
        r'TURF_GOING_ASSUMED\s*=\s*"[^"]*".*':
            f'TURF_GOING_ASSUMED = "{going_turf}"    # auto-set by orchestrator',
        r'AWT_GOING_ASSUMED\s*=\s*"[^"]*".*':
            f'AWT_GOING_ASSUMED  = "{going_awt}"    # auto-set by orchestrator',
    }

    for pattern, replacement in replacements.items():
        content = re.sub(pattern, replacement, content, count=1)

    output_script.write_text(content, encoding="utf-8")
    print(f"  ✓ Generated: {output_script.name}")
    return output_script


def generate_analysis_script_v44(date_str: str, venue: str,
                                 racecard_path: Path,
                                 going_turf: str, going_awt: str) -> Path:
    """Patch the v4.4 model's config section for a specific meeting date."""
    date_compact = date_str.replace("-", "")
    output_script = BASE / f"race_day_analysis_{date_compact}_v4.4.py"
    meeting_title = build_meeting_title(date_str, venue)

    print(f"\n{'='*60}")
    print(f"[3/4] GENERATING v4.4 ANALYSIS SCRIPT")
    print(f"  Meeting: {meeting_title}")
    print(f"  Script:  {output_script.name}")
    print(f"{'='*60}")

    content = TEMPLATE_V44.read_text(encoding="utf-8")

    replacements = {
        r'RACE_CARD\s*=\s*BASE\s*/\s*"racecards"\s*/\s*"[^"]*"':
            f'RACE_CARD    = BASE / "racecards" / "{racecard_path.name}"',
        r'OUT_PDF\s*=\s*BASE\s*/\s*"reports"\s*/\s*"[^"]*"':
            f'OUT_PDF  = BASE / "reports" / "race_day_report_{date_compact}_v4.4.pdf"',
        r'OUT_TEXT\s*=\s*BASE\s*/\s*"reports"\s*/\s*"[^"]*"':
            f'OUT_TEXT = BASE / "reports" / "race_day_analysis_{date_compact}_v4.4.txt"',
        r'MEETING_TITLE\s*=\s*"[^"]*"':
            f'MEETING_TITLE = "{meeting_title}"',
        r'MEETING_VENUE\s*=\s*"[^"]*"':
            f'MEETING_VENUE = "{venue}"',
        r'TURF_GOING_ASSUMED\s*=\s*"[^"]*".*':
            f'TURF_GOING_ASSUMED = "{going_turf}"    # auto-set by orchestrator',
        r'AWT_GOING_ASSUMED\s*=\s*"[^"]*".*':
            f'AWT_GOING_ASSUMED  = "{going_awt}"    # auto-set by orchestrator',
    }

    for pattern, replacement in replacements.items():
        content = re.sub(pattern, replacement, content, count=1)

    output_script.write_text(content, encoding="utf-8")
    print(f"  ✓ Generated: {output_script.name}")
    return output_script


def run_analysis(script_path: Path) -> int:
    """Execute the per-meeting analysis script."""
    print(f"\n{'='*60}")
    print(f"[4/4] RUNNING ANALYSIS — {script_path.name}")
    print(f"{'='*60}\n")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(
        [PYTHON, str(script_path)],
        env=env,
        cwd=str(BASE),
        capture_output=False,
    )
    return result.returncode


def run_sarr(date_str: str) -> int:
    """Run the independent SARR race-day model. Non-fatal on failure."""
    sarr_script = BASE / "sarr_raceday.py"
    if not sarr_script.exists():
        print("  (SARR script not found — skipping)")
        return 0
    print(f"\n{'='*60}")
    print(f"[4c] RUNNING SARR MODEL — {date_str}")
    print(f"{'='*60}\n")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [PYTHON, str(sarr_script), "--date", date_str],
        env=env, cwd=str(BASE), capture_output=False,
    )
    if r.returncode != 0:
        print(f"  (SARR exited {r.returncode} — non-fatal)")
    return r.returncode


def run_results_scraper(date_str: str) -> Path:
    """Run scrape_hkjc_results.py and return the output JSON path."""
    date_compact = date_str.replace("-", "")
    json_out = BASE / "reports" / f"results_{date_compact}.json"

    print(f"\n{'='*60}")
    print(f"[5a] SCRAPING POST-RACE RESULTS — {date_str}")
    print(f"{'='*60}")

    cmd = [PYTHON, str(RESULTS_SCRAPER), "--date", date_str]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(cmd, env=env, cwd=str(BASE), capture_output=False)
    if result.returncode != 0:
        print(f"WARNING: Results scraper failed (exit code {result.returncode})")
    else:
        print(f"  ✓ Results: {json_out.name}")

    return json_out


def run_backtest(date_str: str) -> Path:
    """Run backtest_model.py for a single meeting."""
    date_compact = date_str.replace("-", "")
    json_out = BASE / "reports" / f"backtest_{date_compact}.json"

    print(f"\n{'='*60}")
    print(f"[5b] RUNNING BACKTEST — {date_str}")
    print(f"{'='*60}")

    cmd = [PYTHON, str(BACKTEST), "--date", date_str]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(cmd, env=env, cwd=str(BASE), capture_output=False)
    if result.returncode != 0:
        print(f"WARNING: Backtest failed (exit code {result.returncode})")
    else:
        print(f"  ✓ Backtest: {json_out.name}")

    return json_out


def run_form_guide_builder(date_str: str) -> Path:
    """Pre-build form guide JSON cache so dashboard can render per-run video + commentary."""
    out_path = BASE / "cache" / f"form_guide_{date_str}.json"
    print(f"\n{'='*60}")
    print(f"[3.5] BUILDING FORM GUIDE CACHE — {date_str}")
    print(f"{'='*60}")
    cmd = [PYTHON, str(FORM_GUIDE_BUILDER), date_str]
    env = os.environ.copy(); env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(cmd, env=env, cwd=str(BASE), capture_output=False)
    if result.returncode != 0:
        print(f"WARNING: Form guide builder failed (exit code {result.returncode})")
    elif out_path.exists():
        print(f"  ✓ Form guide cache: {out_path.name}")
    return out_path


def run_incident_scraper(date_str: str) -> Path:
    """Scrape HKJC Racing Incident Report + Comments on Running."""
    date_compact = date_str.replace("-", "")
    out_path = BASE / "reports" / f"incidents_{date_compact}.json"
    print(f"\n{'='*60}")
    print(f"[5c] SCRAPING INCIDENT REPORTS — {date_str}")
    print(f"{'='*60}")
    cmd = [PYTHON, str(INCIDENT_SCRAPER), "--date", date_str]
    env = os.environ.copy(); env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(cmd, env=env, cwd=str(BASE), capture_output=False)
    if result.returncode != 0:
        print(f"WARNING: Incident scraper failed (exit code {result.returncode})")
    elif out_path.exists():
        print(f"  ✓ Incidents: {out_path.name}")
    return out_path


def run_rp_photo_scraper(date_str: str) -> Path:
    """Scrape running position photos."""
    date_compact = date_str.replace("-", "")
    out_dir = BASE / "running_position_photos" / date_compact
    print(f"\n{'='*60}")
    print(f"[5d] SCRAPING RUNNING POSITION PHOTOS — {date_str}")
    print(f"{'='*60}")
    cmd = [PYTHON, str(RP_PHOTO_SCRAPER), "--date", date_str]
    env = os.environ.copy(); env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(cmd, env=env, cwd=str(BASE), capture_output=False)
    if result.returncode != 0:
        print(f"WARNING: RP photo scraper failed (exit code {result.returncode})")
    elif out_dir.exists():
        print(f"  ✓ RP photos dir: {out_dir.relative_to(BASE)}")
    return out_dir


def run_commentary_generator(date_str: str) -> Path:
    """Generate AI race commentary from results + incidents + RP photos."""
    date_compact = date_str.replace("-", "")
    out_path = BASE / "reports" / f"commentary_{date_compact}.json"
    print(f"\n{'='*60}")
    print(f"[5e] GENERATING RACE COMMENTARY — {date_str}")
    print(f"{'='*60}")
    cmd = [PYTHON, str(COMMENTARY), "--date", date_str]
    env = os.environ.copy(); env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(cmd, env=env, cwd=str(BASE), capture_output=False)
    if result.returncode != 0:
        print(f"WARNING: Commentary generator failed (exit code {result.returncode})")
    elif out_path.exists():
        print(f"  ✓ Commentary: {out_path.name}")
    return out_path


def main():
    args = parse_args()

    # Validate date
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(f"ERROR: Invalid date format '{args.date}' — use YYYY-MM-DD")
        sys.exit(1)

    date_compact = args.date.replace("-", "")
    racecard_path = BASE / "racecards" / f"racecard_{date_compact}.xlsx"

    # Step 1: Scrape
    if not args.skip_scrape:
        racecard_path = run_scraper(args.date, args.no_cache)
    else:
        if not racecard_path.exists():
            print(f"ERROR: Race card not found: {racecard_path}")
            sys.exit(1)
        print(f"\n[1/4] SCRAPING SKIPPED — using {racecard_path.name}")

    if args.skip_analysis:
        print("\n[2/4] VET SCRAPING SKIPPED")
        print("\n[3/4] ANALYSIS SKIPPED")
        print(f"\nDone. Race card: {racecard_path.name}")
        if args.post_race:
            run_results_scraper(args.date)
            run_backtest(args.date)
            run_incident_scraper(args.date)
            run_rp_photo_scraper(args.date)
            run_commentary_generator(args.date)
        return

    # Step 2: Detect venue and scrape vet records
    venue = detect_venue_from_cache(args.date)
    vet_json = run_vet_scraper(args.date, venue)

    # Step 3: Generate analysis script
    model_ver = getattr(args, 'model', 'v4.4')
    if model_ver == "v4.4":
        script = generate_analysis_script_v44(
            args.date, venue, racecard_path,
            going_turf=args.going_turf,
            going_awt=args.going_awt,
        )
    else:
        script = generate_analysis_script(
            args.date, venue, racecard_path,
            going_turf=args.going_turf,
            going_awt=args.going_awt,
        )

    # Step 4: Run analysis
    rc = run_analysis(script)

    # Step 4b: Build form guide cache so dashboard can render per-run video + commentary
    try:
        run_form_guide_builder(args.date)
    except Exception as e:
        print(f"WARNING: Form guide cache build failed: {e}")

    # Step 4c: Run SARR (independent, always runs after ET)
    if args.skip_sarr:
        print("\n[4c] SARR SKIPPED (--skip-sarr)")
    else:
        try:
            run_sarr(args.date)
        except Exception as e:
            print(f"WARNING: SARR run failed: {e}")

    v_tag = model_ver.replace('v', 'v') if model_ver else 'v3.4.8'
    if rc == 0:
        json_path = BASE / "reports" / f"race_day_report_{date_compact}_{v_tag}.json"
        print(f"\n{'='*60}")
        print("PIPELINE COMPLETE")
        print(f"  PDF:  reports/race_day_report_{date_compact}_{v_tag}.pdf")
        print(f"  Text: reports/race_day_analysis_{date_compact}_{v_tag}.txt")
        if json_path.exists():
            print(f"  JSON: reports/race_day_report_{date_compact}_{v_tag}.json")
        if vet_json.exists():
            print(f"  Vet:  reports/vet_report_{date_compact}.json")
            vet_pdf = BASE / "reports" / f"vet_report_{date_compact}.pdf"
            if vet_pdf.exists():
                print(f"  VPdf: reports/vet_report_{date_compact}.pdf")
        print(f"{'='*60}")

        # Post-race: scrape results + backtest + incidents + RP + commentary
        if args.post_race:
            results_json = run_results_scraper(args.date)
            if results_json.exists():
                bt_json = run_backtest(args.date)
                # Incident reports, RP photos, then commentary (order matters —
                # commentary needs all three inputs)
                inc_json = run_incident_scraper(args.date)
                rp_dir = run_rp_photo_scraper(args.date)
                comm_json = run_commentary_generator(args.date)
                print(f"\n{'='*60}")
                print("POST-RACE COMPLETE")
                print(f"  Results:    reports/results_{date_compact}.json")
                if bt_json.exists():
                    print(f"  Backtest:   reports/backtest_{date_compact}.json")
                if inc_json.exists():
                    print(f"  Incidents:  reports/incidents_{date_compact}.json")
                if rp_dir.exists():
                    print(f"  RP photos:  running_position_photos/{date_compact}/")
                if comm_json.exists():
                    print(f"  Commentary: reports/commentary_{date_compact}.json")
                print(f"{'='*60}")
            else:
                print("\nSkipping backtest — no results scraped.")
    else:
        print(f"\nERROR: Analysis failed with exit code {rc}")
        sys.exit(rc)


if __name__ == "__main__":
    main()
