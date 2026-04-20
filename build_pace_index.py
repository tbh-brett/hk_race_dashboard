#!/usr/bin/env python3
"""
build_pace_index.py — One-shot (or incremental) backfill of the persistent
measured-pace index from hkjc_results_updated.xlsx.

Usage:
    python build_pace_index.py                 # everything in the DB
    python build_pace_index.py 2025-09-01      # only races on/after this date
    python build_pace_index.py --rebuild       # discard existing cache first

Writes:
    cache/race_pace_index.json
        keyed by "YYYY-MM-DD_R{n}"
        value = compact pace dict (see pace_utils.annotate_results_meeting)
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

import pandas as pd

from pace_utils import (
    compute_actual_race_pace,
    load_pace_index,
    save_pace_index,
    upsert_pace,
)

BASE = Path(__file__).parent


def _load_db() -> pd.DataFrame:
    src = BASE / "hkjc_results_updated.xlsx"
    if not src.exists():
        sys.exit(f"ERROR: {src} not found")
    try:
        df = pd.read_excel(src)
    except PermissionError:
        tmp = Path(tempfile.gettempdir()) / src.name
        shutil.copy2(src, tmp)
        df = pd.read_excel(tmp)
    df["race_date"] = pd.to_datetime(df["race_date"])
    return df


def _race_to_dict(grp: pd.DataFrame) -> dict:
    """Convert a per-(date, race_number) DB group into the shape
    expected by pace_utils.compute_actual_race_pace()."""
    first = grp.iloc[0]
    rc = str(first.get("race_course") or "").upper()
    rt = str(first.get("race_track") or "").upper()
    tt = str(first.get("track_type") or "")
    is_awt = "AWT" in rc or "AWT" in tt.upper() or "ALL WEATHER" in tt.upper()
    venue = "HV" if rt == "HV" else "ST"

    runners = []
    for _, row in grp.iterrows():
        try:
            ft = float(row.get("finish_time_seconds")) if pd.notna(row.get("finish_time_seconds")) else None
        except (TypeError, ValueError):
            ft = None
        runners.append({
            "place":                 row.get("place"),
            "finish_time_seconds":   ft,
            "sectiontimes":          row.get("sectiontimes"),
            "running_position":      row.get("running_positions"),
        })

    return {
        "race_number":    int(first["race_number"]) if pd.notna(first.get("race_number")) else None,
        "distance":       int(first["distance"]) if pd.notna(first.get("distance")) else 0,
        "race_class":     first.get("race_class"),
        "going":          first.get("going"),
        "race_course":    first.get("race_course"),
        "race_track":     first.get("race_track"),
        "track_type":     tt,
        "is_awt":         is_awt,
        "runners":        runners,
    }, venue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("since", nargs="?", default=None,
                    help="YYYY-MM-DD — only process races on/after this date")
    ap.add_argument("--rebuild", action="store_true",
                    help="discard existing cache and rebuild from scratch")
    args = ap.parse_args()

    df = _load_db()
    print(f"DB loaded: {len(df):,} rows ({df['race_date'].min().date()} → {df['race_date'].max().date()})")

    if args.since:
        since = pd.to_datetime(args.since)
        df = df[df["race_date"] >= since]
        print(f"Filtering to race_date >= {since.date()} → {len(df):,} rows")

    idx = {} if args.rebuild else load_pace_index()
    print(f"Existing index entries: {len(idx):,}  ({'REBUILD' if args.rebuild else 'INCREMENTAL UPSERT'})")

    stats = Counter()
    processed = 0
    for (rd, rn), grp in df.groupby(["race_date", "race_number"], sort=True):
        race_dict, venue = _race_to_dict(grp)
        pace = compute_actual_race_pace(race_dict, venue)
        stats[pace.get("actual_source") or "none"] += 1

        if pace.get("actual_dev") is None:
            continue

        date_iso = rd.strftime("%Y-%m-%d")
        upsert_pace(idx, date_iso, int(rn), {
            "date":           date_iso,
            "race_number":    int(rn),
            "venue":          venue,
            "distance":       race_dict["distance"],
            "race_class":     race_dict["race_class"],
            "is_awt":         race_dict["is_awt"],
            "going":          pace["actual_going"],
            "winner_sects":   pace["actual_winner_sects"],
            "winner_early_s": pace["actual_winner_early_s"],
            "winner_time_s":  pace["actual_winner_time"],
            "hkjc_std_early": pace["actual_std_early"],
            "hkjc_std_total": pace["actual_std_total"],
            "raw_dev_s":      pace["actual_raw_dev_s"],
            "going_adj_s":    pace["actual_going_adj_s"],
            "adj_dev_s":      pace["actual_dev"],
            "label":          pace["actual_pace_label"],
            "source":         pace["actual_source"],
            "winner_style":   pace["actual_winner_style"],
        })
        processed += 1
        stats[pace.get("actual_pace_label") or "N/A"] += 1

    save_pace_index(idx)
    print(f"\nSaved {len(idx):,} entries to cache/race_pace_index.json "
          f"(processed {processed:,} new/updated this run)")
    print("\nSource breakdown (this run):")
    for src in ("early_sectional", "total_time", "none"):
        print(f"  {src:>18s}: {stats[src]:>5d}")
    print("\nLabel distribution (this run):")
    label_order = ["Very Fast", "Fast", "Slightly Fast", "Normal",
                   "Slightly Slow", "Slow", "Very Slow"]
    for lbl in label_order:
        print(f"  {lbl:>14s}: {stats[lbl]:>5d}")


if __name__ == "__main__":
    main()
