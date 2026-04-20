"""Compare projected speed-map style to actual early-running position.

Walks every v4.4 JSON in reports/ that has a matching results row in
hkjc_results_updated.xlsx, translates each horse's ``speed_map.style``
into an expected early bucket, and checks whether the horse's first
running-position call falls in that bucket.

Run:
    python _speedmap_accuracy_audit.py

Prints a confusion matrix + overall match rate + per-style recall.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
RESULTS_XLSX = BASE / "hkjc_results_updated.xlsx"

STYLE_TO_BUCKET = {
    "Leader":    "Lead",
    "On-Pace":   "Prom",
    "Prominent": "Prom",
    "Midfield":  "Mid",
    "Closer":    "Held",
    "Held-up":   "Held",
    "Held up":   "Held",
    "Rear":      "Rear",
}
BUCKET_ORDER = ["Lead", "Prom", "Mid", "Held", "Rear"]


def position_to_bucket(pos: int, field_size: int) -> str:
    """Translate actual running position → 5 early-position buckets.

    Split the field roughly into quintiles. Small fields collapse the
    middle bucket.
    """
    if field_size <= 0 or pos <= 0:
        return "?"
    frac = pos / field_size  # 0 < frac <= 1
    if frac <= 0.20:
        return "Lead"
    if frac <= 0.40:
        return "Prom"
    if frac <= 0.60:
        return "Mid"
    if frac <= 0.80:
        return "Held"
    return "Rear"


def main():
    if not RESULTS_XLSX.exists():
        print(f"Missing {RESULTS_XLSX}", file=sys.stderr)
        sys.exit(1)
    print(f"Loading {RESULTS_XLSX.name}…")
    df = pd.read_excel(RESULTS_XLSX)

    # Normalise columns
    df["horse_name_upper"] = df["horse_name"].astype(str).str.strip().str.upper()
    df["race_date"] = pd.to_datetime(df["race_date"]).dt.strftime("%Y-%m-%d")

    # We want the FIRST running position (earliest call) from the
    # space-separated ``running_positions`` string column.
    def _first_rp(val):
        try:
            parts = str(val).strip().split()
            return int(parts[0]) if parts else 0
        except (ValueError, TypeError):
            return 0

    df["_first_rp"] = df["running_positions"].apply(_first_rp)
    first_rp_col = "_first_rp"
    print(f"  parsed first running position from 'running_positions' column")

    # Field size per race_date + race_number
    fs = df.groupby(["race_date", "race_number"])["horse_name_upper"].count().to_dict()

    total = 0
    matched = 0
    confusion: dict[tuple[str, str], int] = defaultdict(int)
    bucket_support = Counter()
    misses: list[dict] = []

    for jpath in sorted(REPORTS.glob("race_day_report_*_v4.4.json")):
        # Extract date
        name = jpath.stem  # e.g. race_day_report_20260419_v4.4
        parts = name.split("_")
        date_str = parts[3]
        date_iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        try:
            data = json.loads(jpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        for race in data.get("races", []):
            rn = race.get("race_number")
            sm = race.get("speed_map", {})
            grid = sm.get("grid", [])
            if not grid:
                continue
            field_size = fs.get((date_iso, rn))
            if not field_size:
                continue
            for h in grid:
                style = h.get("style") or ""
                bucket_proj = STYLE_TO_BUCKET.get(style.strip())
                if not bucket_proj:
                    continue
                name_u = (h.get("horse_name") or "").strip().upper()
                row = df[(df["race_date"] == date_iso) &
                         (df["race_number"] == rn) &
                         (df["horse_name_upper"] == name_u)]
                if row.empty:
                    continue
                actual_pos = row.iloc[0][first_rp_col]
                try:
                    actual_pos = int(actual_pos)
                except (ValueError, TypeError):
                    continue
                if actual_pos <= 0:
                    continue
                bucket_act = position_to_bucket(actual_pos, field_size)
                if bucket_act == "?":
                    continue
                total += 1
                confusion[(bucket_proj, bucket_act)] += 1
                bucket_support[bucket_proj] += 1
                if bucket_proj == bucket_act:
                    matched += 1
                else:
                    misses.append({
                        "date": date_iso, "race": rn, "horse": h.get("horse_name"),
                        "proj": bucket_proj, "actual": bucket_act,
                        "pos": actual_pos, "field": field_size,
                    })

    if total == 0:
        print("No overlapping horses between JSONs and results DB.")
        return

    print(f"\nEvaluated {total} horse-race observations")
    print(f"Exact match: {matched}/{total} = {100*matched/total:.1f}%")

    # Within-1-bucket: count distance between projected and actual
    def _dist(a, b):
        return abs(BUCKET_ORDER.index(a) - BUCKET_ORDER.index(b))
    within_1 = sum(v for (p, a), v in confusion.items() if _dist(p, a) <= 1)
    print(f"Within-1 bucket: {within_1}/{total} = {100*within_1/total:.1f}%")

    # Confusion matrix
    print("\nConfusion matrix (rows = projected, cols = actual):")
    header = "proj \\ act    " + "  ".join(f"{b:>5}" for b in BUCKET_ORDER) + "   Support"
    print(header)
    print("-" * len(header))
    for p in BUCKET_ORDER:
        supp = bucket_support.get(p, 0)
        cells = []
        for a in BUCKET_ORDER:
            c = confusion.get((p, a), 0)
            pct = (100 * c / supp) if supp else 0
            cells.append(f"{pct:>4.0f}%")
        print(f"{p:<13}  " + "  ".join(cells) + f"    {supp}")

    # Per-style recall (exact)
    print("\nPer-projected-style exact-match rate:")
    for p in BUCKET_ORDER:
        supp = bucket_support.get(p, 0)
        correct = confusion.get((p, p), 0)
        if supp:
            print(f"  {p:<5}  {correct:>3}/{supp:<3} = {100*correct/supp:>5.1f}%")

    # Top bottleneck
    print("\nTop confusion pairs:")
    worst = sorted(
        (((p, a), v) for (p, a), v in confusion.items() if p != a),
        key=lambda kv: -kv[1],
    )[:8]
    for (p, a), v in worst:
        print(f"  {p} → {a}: {v}")


if __name__ == "__main__":
    main()
