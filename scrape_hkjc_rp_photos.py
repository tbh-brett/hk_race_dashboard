#!/usr/bin/env python3
"""
HKJC Running Position Photos Scraper — scrape_hkjc_rp_photos.py
================================================================
Downloads the composite "Race Running Position Photos" JPG that HKJC
publishes on the localresults page after each race. Each image shows
horses labelled with their saddle numbers at multiple meter marks
(e.g. 800m, 400m, 200m, finish) stacked vertically.

URL pattern (stable, no auth):
    https://racing.hkjc.com/general/-/media/Sites/JCRW/RaceResult/
        {season}/{YYYYMMDD}/{YYYYMMDD}R{N}_L.jpg

Conventions (from HKJC):
    - Left-most = front of pack, right-most = back
    - Inside rail is at the top of each frame (except Sha Tin 1000m,
      which runs on the back-straight and is reversed)

Usage:
    # Single date
    python scrape_hkjc_rp_photos.py --date 2026-04-12

    # Multiple explicit dates
    python scrape_hkjc_rp_photos.py --dates 2026-04-01,2026-04-06,2026-04-12

    # Date range (inclusive), only actual meeting dates that return photos
    python scrape_hkjc_rp_photos.py --from 2026-04-01 --to 2026-04-15

    # Force re-download even if file exists
    python scrape_hkjc_rp_photos.py --date 2026-04-12 --force

Output:
    running_position_photos/YYYYMMDD/R{N}.jpg
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import requests

from hkjc_client import (
    BASE_URL, RP_PHOTO_URL_TMPL as PHOTO_URL_TMPL, HEADERS as _BASE_HEADERS,
    fetch_bytes,
)

BASE_DIR = Path(__file__).parent
PHOTOS_DIR = BASE_DIR / "running_position_photos"

# Static-media requests need a Referer pointing to localresults so the CDN
# returns the JPG instead of a redirect.
HEADERS = {**_BASE_HEADERS, "Referer": f"{BASE_URL}/en-us/local/information/localresults"}

MAX_RACES_PROBE = 12  # HKJC meetings have up to 11 races; probe one extra
CONSECUTIVE_MISS_LIMIT = 3  # stop probing a date after N consecutive 404s


def season_for_date(d: date) -> str:
    """HK racing season runs Sep 1 → Aug 31 next year.
    Returns 'YY_YY' (e.g. 25_26 for Sep 2025–Aug 2026)."""
    if d.month >= 9:
        start_yy = d.year % 100
        end_yy = (d.year + 1) % 100
    else:
        start_yy = (d.year - 1) % 100
        end_yy = d.year % 100
    return f"{start_yy:02d}_{end_yy:02d}"


def build_photo_url(d: date, race_no: int) -> str:
    ymd = d.strftime("%Y%m%d")
    return PHOTO_URL_TMPL.format(season=season_for_date(d), ymd=ymd, race=race_no)


def download_photo(
    session: requests.Session,
    d: date,
    race_no: int,
    out_path: Path,
    force: bool = False,
) -> str:
    """Return status: 'ok' | 'skipped' | 'missing' | 'error'."""
    if out_path.exists() and not force:
        return "skipped"
    url = build_photo_url(d, race_no)
    resp = fetch_bytes(session, url, extra_headers={"Referer": HEADERS["Referer"]})
    if resp is None:
        print(f"    R{race_no}: network error")
        return "error"
    if resp.status_code == 404:
        return "missing"
    if resp.status_code != 200:
        print(f"    R{race_no}: HTTP {resp.status_code}")
        return "error"
    # Guard against HTML error pages returned with 200
    ct = resp.headers.get("Content-Type", "").lower()
    if "image" not in ct or len(resp.content) < 1024:
        return "missing"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(resp.content)
    return "ok"


def scrape_date(
    session: requests.Session,
    d: date,
    force: bool = False,
    max_races: int = MAX_RACES_PROBE,
) -> dict:
    ymd = d.strftime("%Y%m%d")
    day_dir = PHOTOS_DIR / ymd
    counts = {"ok": 0, "skipped": 0, "missing": 0, "error": 0}
    consecutive_missing = 0
    print(f"[{d.isoformat()}] season={season_for_date(d)} → {day_dir}")
    for race_no in range(1, max_races + 1):
        out_path = day_dir / f"R{race_no}.jpg"
        status = download_photo(session, d, race_no, out_path, force=force)
        counts[status] += 1
        if status == "ok":
            print(f"    R{race_no}: downloaded ({out_path.stat().st_size // 1024} KB)")
            consecutive_missing = 0
        elif status == "skipped":
            consecutive_missing = 0
        elif status == "missing":
            consecutive_missing += 1
            if consecutive_missing >= CONSECUTIVE_MISS_LIMIT:
                # No more races on this date
                break
        else:
            consecutive_missing = 0
        time.sleep(0.25)  # be polite
    print(
        f"    → ok={counts['ok']} skipped={counts['skipped']} "
        f"missing={counts['missing']} error={counts['error']}"
    )
    return counts


def parse_date_arg(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def date_range(d_from: date, d_to: date) -> List[date]:
    if d_to < d_from:
        raise ValueError("--to must be on or after --from")
    out = []
    cur = d_from
    while cur <= d_to:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="Single date YYYY-MM-DD")
    g.add_argument("--dates", help="Comma-separated list of YYYY-MM-DD")
    g.add_argument("--from", dest="d_from", help="Range start YYYY-MM-DD (use with --to)")
    p.add_argument("--to", dest="d_to", help="Range end YYYY-MM-DD (inclusive)")
    p.add_argument("--force", action="store_true", help="Re-download even if file exists")
    p.add_argument(
        "--max-races",
        type=int,
        default=MAX_RACES_PROBE,
        help=f"Max race number to probe per date (default {MAX_RACES_PROBE})",
    )
    args = p.parse_args()

    dates: List[date]
    if args.date:
        dates = [parse_date_arg(args.date)]
    elif args.dates:
        dates = [parse_date_arg(s.strip()) for s in args.dates.split(",") if s.strip()]
    else:
        if not args.d_to:
            p.error("--to is required when using --from")
        dates = date_range(parse_date_arg(args.d_from), parse_date_arg(args.d_to))

    PHOTOS_DIR.mkdir(exist_ok=True)
    session = requests.Session()

    total = {"ok": 0, "skipped": 0, "missing": 0, "error": 0}
    meeting_days = 0
    dates_with_photos: List[date] = []
    for d in dates:
        counts = scrape_date(session, d, force=args.force, max_races=args.max_races)
        for k, v in counts.items():
            total[k] += v
        if counts["ok"] or counts["skipped"]:
            meeting_days += 1
        # v4.6: also trigger OCR when photos already existed on disk but no
        # corresponding .json parse output — previously only NEW downloads
        # triggered OCR, so re-runs of an already-scraped meeting (or any
        # interrupted OCR) never re-parsed. Union `ok` with "has-photos-but-
        # missing-json" set.
        ymd = d.strftime("%Y%m%d")
        dir_d = PHOTOS_DIR / ymd
        jpgs = sorted(dir_d.glob("R*.jpg")) if dir_d.exists() else []
        needs_ocr = any(not j.with_suffix(".json").exists() for j in jpgs)
        if counts["ok"] or needs_ocr:
            if d not in dates_with_photos:
                dates_with_photos.append(d)

    print()
    print(f"Done. Processed {len(dates)} date(s), {meeting_days} meeting day(s).")
    print(
        f"Total: ok={total['ok']} skipped={total['skipped']} "
        f"missing={total['missing']} error={total['error']}"
    )
    print(f"Photos saved under: {PHOTOS_DIR}")

    # Auto-OCR newly downloaded photos (v4.5+).
    if dates_with_photos:
        print()
        print(f"Auto-OCR: running parse_rp_photos.py on {len(dates_with_photos)} date(s)...")
        try:
            import subprocess
            for d in dates_with_photos:
                ds = d.isoformat()
                r = subprocess.run(
                    [sys.executable, str(BASE_DIR / "parse_rp_photos.py"), "--date", ds],
                    cwd=str(BASE_DIR),
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                if r.returncode == 0:
                    print(f"  OCR {ds}: OK")
                else:
                    print(f"  OCR {ds}: FAILED rc={r.returncode}")
                    if r.stderr:
                        print(f"    {r.stderr.strip().splitlines()[-1]}")
        except Exception as e:
            print(f"  (Auto-OCR skipped: {e})")

    return 0 if total["error"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
