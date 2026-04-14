"""Analyze sample sizes for finer ET weight/draw granularity."""
import pandas as pd, numpy as np
from pathlib import Path
import os

db = pd.read_excel(Path(os.environ.get("TEMP", ".")) / "hkjc_results_updated.xlsx")
db = db[db["finish_time_seconds"].notna() & (db["finish_time_seconds"] > 0)].copy()
print(f"Total valid runs: {len(db)}")

# Map going to going_group
GOING_CODE_MAP = {
    "GF": "Good-to-Firm", "G": "Good", "GY": "Good-to-Yielding",
    "Y": "Yielding", "WF": "AWT-Wet-Fast", "WS": "AWT-Wet-Slow",
    "SE": "AWT-Standard", "SEALED": "AWT-Standard",
    "GOOD TO FIRM FAST": "Good-to-Firm",
}
db["going_group"] = db["going"].map(lambda x: GOING_CODE_MAP.get(str(x).strip(), "Good"))

w = db["actual_weight"].dropna()
print(f"\nWeight range: {w.min():.0f} – {w.max():.0f}")
print(f"Mean: {w.mean():.1f}, Median: {w.median():.0f}")

# Per-pound counts
print("\nPer-pound distribution:")
vc = w.value_counts().sort_index()
for wt in range(int(w.min()), int(w.max()) + 1):
    c = vc.get(wt, 0)
    if c > 0:
        print(f"  {wt:3d} lbs: {c:5d} runs")

# Current 5-lb bands
def wb_old(w):
    if w <= 115: return "105-115"
    if w <= 120: return "116-120"
    if w <= 125: return "121-125"
    if w <= 130: return "126-130"
    return "131-135"

# Proposed 3-lb bands
def wb_3(w):
    if w <= 112: return "≤112"
    if w <= 115: return "113-115"
    if w <= 118: return "116-118"
    if w <= 121: return "119-121"
    if w <= 124: return "122-124"
    if w <= 127: return "125-127"
    if w <= 130: return "128-130"
    return "131+"

db["wb_old"] = db["actual_weight"].apply(wb_old)
db["wb_3lb"] = db["actual_weight"].apply(wb_3)

print("\n=== Current 5-lb bands ===")
for b in ["105-115", "116-120", "121-125", "126-130", "131-135"]:
    g = db[db["wb_old"] == b]
    print(f"  {b}: {len(g):5d} runs")

print("\n=== Proposed 3-lb bands ===")
for b in ["≤112", "113-115", "116-118", "119-121", "122-124", "125-127", "128-130", "131+"]:
    g = db[db["wb_3lb"] == b]
    wmin = g["actual_weight"].min() if len(g) else 0
    wmax = g["actual_weight"].max() if len(g) else 0
    print(f"  {b:>8s}: {len(g):5d} runs  (actual {wmin:.0f}-{wmax:.0f})")

# Check how Fine tier (dist×going×wband×course) sample sizes change
print("\n=== Fine-tier cell sizes: current vs 3-lb ===")
is_turf = ~db["track_type"].str.contains("All Weather", case=False, na=False)
turf = db[is_turf].copy()

for label, col in [("Current 5-lb", "wb_old"), ("Proposed 3-lb", "wb_3lb")]:
    cells = turf.groupby(["distance", "going_group", col, "race_course"]).size()
    n5  = (cells >= 5).sum()
    n2  = (cells >= 2).sum()
    tot = len(cells)
    med = cells.median()
    p25 = cells.quantile(0.25)
    print(f"\n  {label}:")
    print(f"    Total cells: {tot}")
    print(f"    Cells with n≥5: {n5} ({100*n5/tot:.0f}%)")
    print(f"    Cells with n≥2: {n2} ({100*n2/tot:.0f}%)")
    print(f"    Median cell size: {med:.0f}, Q25: {p25:.0f}")

# Check Coarse tier (dist×going×wband×track_type)
print("\n=== Coarse-tier cell sizes: current vs 3-lb ===")
for label, col in [("Current 5-lb", "wb_old"), ("Proposed 3-lb", "wb_3lb")]:
    cells = db.groupby(["distance", "going_group", col, "track_type"]).size()
    n5  = (cells >= 5).sum()
    tot = len(cells)
    med = cells.median()
    print(f"  {label}: {tot} cells, {n5} with n≥5 ({100*n5/tot:.0f}%), median={med:.0f}")

# Draw analysis: draw distribution by distance
print("\n=== Draw distribution (top distances) ===")
db["draw_num"] = pd.to_numeric(db["draw"], errors="coerce")
for dist in [1000, 1200, 1400, 1600, 1650, 1800, 2000, 2200]:
    sub = db[(db["distance"] == dist) & db["draw_num"].notna()]
    if len(sub) == 0:
        continue
    dmax = int(sub["draw_num"].max())
    print(f"\n  {dist}m: {len(sub)} runs, draws 1-{dmax}")
    # Check draw × course cell sizes
    for crs, cg in sub.groupby("race_course"):
        cells = cg.groupby("draw_num").size()
        n5 = (cells >= 5).sum()
        print(f"    {crs}: {len(cg)} runs, {len(cells)} draws, {n5} with n≥5, median/draw={cells.median():.0f}")

# Draw zone approach: inner/middle/outer
print("\n=== Draw-zone approach (inner/middle/outer) ===")
def draw_zone(draw, field_size):
    if field_size <= 0 or pd.isna(field_size):
        return "Unknown"
    frac = draw / field_size
    if frac <= 0.33:
        return "Inner"
    if frac <= 0.67:
        return "Middle"
    return "Outer"

for (dist, crs), sub in db.groupby(["distance", "race_course"]):
    if len(sub) < 50:
        continue
    fs = sub.groupby(["race_date", "race_number"]).size()
    sub = sub.merge(fs.rename("field_size"), on=["race_date", "race_number"])
    sub["dzone"] = sub.apply(lambda r: draw_zone(r["draw_num"], r["field_size"]), axis=1)
    if sub["dzone"].nunique() < 3:
        continue
    zmed = sub.groupby("dzone")["finish_time_seconds"].agg(["median", "count"])
    inner = zmed.loc["Inner", "median"] if "Inner" in zmed.index else None
    outer = zmed.loc["Outer", "median"] if "Outer" in zmed.index else None
    if inner and outer:
        diff = outer - inner
        print(f"  {dist}m {crs}: Inner={inner:.2f} Mid={zmed.loc['Middle','median']:.2f} Outer={outer:.2f}  diff={diff:+.2f}s  (n={len(sub)})")
