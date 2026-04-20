#!/usr/bin/env python3
"""
_analyse_pace_benefit.py — Empirically measure how race pace affects each
running style, by distance band and venue.

Reads:
    cache/race_pace_index.json   (1,465+ historical races)
    hkjc_results_updated.xlsx    (horse-level finishing positions)

Writes:
    cache/pace_style_benefit.json

Schema:
    {
      "_default":                {"Slow": {"Leader": -0.15, ...}, ...},
      "sprint":                  {"Slow": {"Leader": -0.20, ...}, ...},
      "mile":                    {...},
      "route":                   {...},
      "ST_sprint", "HV_sprint", ...: {...}
    }

Value = seconds adjustment SUBTRACTED from projected time (negative =
beneficiary / lower = faster).
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import pandas as pd

from pace_utils import pace_group, running_style_from_positions

BASE = Path(__file__).parent
CACHE_DIR = BASE / "cache"
PACE_INDEX = CACHE_DIR / "race_pace_index.json"
OUT_PATH = CACHE_DIR / "pace_style_benefit.json"

# Convert a top-3-rate lift (vs baseline) into seconds adjustment
# Rule of thumb: 1% top-3-rate lift ≈ 0.015s faster projected time.
# Rationale: the SMAP_TIME_COEFF = 0.10 scaling in v4.5 treats a 0.5s
# smap_advantage as a meaningful but not dominant edge; pace-style should
# be comparable but slightly smaller.
LIFT_TO_SECONDS = 0.015

STYLES = ["Leader", "On-Pace", "Midfield", "Closer"]
GROUPS = ["Slow", "Avg", "Fast"]


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


def _distance_band(d) -> str:
    try:
        d = int(d)
    except (TypeError, ValueError):
        return "other"
    if d <= 1200:
        return "sprint"
    if d <= 1600:
        return "mile"
    return "route"


def _parse_style(rp) -> str:
    if rp is None or (isinstance(rp, float) and pd.isna(rp)):
        return "N/A"
    positions = [int(x) for x in re.findall(r"\d+", str(rp))]
    if not positions:
        return "N/A"
    return running_style_from_positions(positions)


def _parse_place(p) -> int:
    if p is None or (isinstance(p, float) and pd.isna(p)):
        return 0
    m = re.search(r"\d+", str(p))
    return int(m.group()) if m else 0


def _compute_rates(rows_by_group_style: dict) -> dict:
    """Given {(group, style): [1_if_top3, 0_else, ...]}, return
    {group: {style: seconds_adjustment}} normalised vs baseline.
    """
    rates: dict = {}
    for (g, s), vals in rows_by_group_style.items():
        if len(vals) < 20:  # too few samples
            continue
        rates.setdefault(g, {})[s] = sum(vals) / len(vals)

    # Baseline = top-3 rate per style under Avg pace
    baseline = rates.get("Avg", {})
    out: dict = {}
    for g in GROUPS:
        if g not in rates:
            continue
        out[g] = {}
        for s in STYLES:
            rate = rates[g].get(s)
            base = baseline.get(s)
            if rate is None or base is None:
                out[g][s] = 0.0
                continue
            lift_pct = (rate - base) * 100.0  # percentage points
            # Beneficiary = lift>0 → faster → negative seconds
            out[g][s] = round(-lift_pct * LIFT_TO_SECONDS, 3)
    return out


def main():
    if not PACE_INDEX.exists():
        sys.exit(f"ERROR: {PACE_INDEX} not found — run build_pace_index.py first")

    with open(PACE_INDEX, encoding="utf-8") as f:
        pace_idx = json.load(f)
    print(f"Pace index: {len(pace_idx):,} races")

    df = _load_db()
    print(f"DB: {len(df):,} rows")

    # Per-row: look up pace group, style, top-3 flag
    buckets = {
        "_all": defaultdict(list),
        "sprint": defaultdict(list),
        "mile":   defaultdict(list),
        "route":  defaultdict(list),
        "ST_sprint": defaultdict(list), "ST_mile": defaultdict(list), "ST_route": defaultdict(list),
        "HV_sprint": defaultdict(list), "HV_mile": defaultdict(list), "HV_route": defaultdict(list),
    }

    matched = 0
    for _, row in df.iterrows():
        rd = row["race_date"]
        rn = row.get("race_number")
        if pd.isna(rd) or pd.isna(rn):
            continue
        key = f"{rd.strftime('%Y-%m-%d')}_R{int(rn)}"
        entry = pace_idx.get(key)
        if not entry:
            continue

        label = entry.get("label", "")
        g = pace_group(label)
        if g not in GROUPS:
            continue

        style = _parse_style(row.get("running_positions"))
        if style not in STYLES:
            continue

        place = _parse_place(row.get("place"))
        if place <= 0:
            continue
        top3 = 1 if place <= 3 else 0

        band = _distance_band(entry.get("distance") or row.get("distance"))
        venue = str(entry.get("venue") or "").upper()

        buckets["_all"][(g, style)].append(top3)
        if band in ("sprint", "mile", "route"):
            buckets[band][(g, style)].append(top3)
            if venue in ("ST", "HV"):
                buckets[f"{venue}_{band}"][(g, style)].append(top3)
        matched += 1

    print(f"Matched {matched:,} runner-rows against pace index")

    result: dict = {"_default": _compute_rates(buckets["_all"])}
    for band in ("sprint", "mile", "route"):
        rates = _compute_rates(buckets[band])
        if rates:
            result[band] = rates
    for venue in ("ST", "HV"):
        for band in ("sprint", "mile", "route"):
            rates = _compute_rates(buckets[f"{venue}_{band}"])
            if rates:
                result[f"{venue}_{band}"] = rates

    # Also write sample-sizes for transparency
    def _sample_counts(src):
        return {g: {s: len(src.get((g, s), [])) for s in STYLES} for g in GROUPS}

    result["_samples"] = {
        "_all":  _sample_counts(buckets["_all"]),
        "sprint": _sample_counts(buckets["sprint"]),
        "mile":   _sample_counts(buckets["mile"]),
        "route":  _sample_counts(buckets["route"]),
    }

    CACHE_DIR.mkdir(exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {OUT_PATH}")

    # Human-readable summary
    print("\n═══ Pace × Style top-3 rate (seconds adjustment) ═══")
    for scope in ("_default", "sprint", "mile", "route"):
        tbl = result.get(scope)
        if not tbl:
            continue
        print(f"\n[{scope}]")
        print(f"  {'':>8s}  " + "  ".join(f"{s:>8s}" for s in STYLES))
        for g in GROUPS:
            row = tbl.get(g, {})
            print(f"  {g:>8s}  " + "  ".join(f"{row.get(s, 0.0):>+8.3f}" for s in STYLES))


if __name__ == "__main__":
    main()
