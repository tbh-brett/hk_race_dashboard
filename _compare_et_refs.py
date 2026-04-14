"""Compare ET reference tables v3 vs v4."""
import pandas as pd
from pathlib import Path
import os

TEMP = Path(os.environ.get("TEMP", "."))

v3 = pd.ExcelFile(TEMP / "expected_time_references_v3.xlsx")
v4 = pd.ExcelFile(TEMP / "expected_time_references_v4.xlsx")

co3 = pd.read_excel(v3, sheet_name="Coarse (Dist×Going×Wt×Trk)")
co4 = pd.read_excel(v4, sheet_name="Coarse (Dist×Going×Wt×Trk)")

print("=== Coarse tier: 1200m Good Turf ===\n")
print("v3 (5-lb bands):")
sub = co3[(co3["distance"] == 1200) & (co3["going_group"] == "Good") & (co3["track_type"] == "Turf")]
for _, r in sub.sort_values("weight_band").iterrows():
    print(f"  {r['weight_band']:>10s}: ET={r['expected_time']:.2f}s  n={int(r['sample_size'])}")

print("\nv4 (3-lb bands):")
sub = co4[(co4["distance"] == 1200) & (co4["going_group"] == "Good") & (co4["track_type"] == "Turf")]
for _, r in sub.sort_values("weight_band").iterrows():
    print(f"  {r['weight_band']:>10s}: ET={r['expected_time']:.2f}s  n={int(r['sample_size'])}")

# Also check 1400m and 1600m
for dist in [1400, 1600]:
    print(f"\n=== Coarse tier: {dist}m Good Turf ===\n")
    print("v3:")
    sub = co3[(co3["distance"] == dist) & (co3["going_group"] == "Good") & (co3["track_type"] == "Turf")]
    for _, r in sub.sort_values("weight_band").iterrows():
        print(f"  {r['weight_band']:>10s}: ET={r['expected_time']:.2f}s  n={int(r['sample_size'])}")

    print("v4:")
    sub = co4[(co4["distance"] == dist) & (co4["going_group"] == "Good") & (co4["track_type"] == "Turf")]
    for _, r in sub.sort_values("weight_band").iterrows():
        print(f"  {r['weight_band']:>10s}: ET={r['expected_time']:.2f}s  n={int(r['sample_size'])}")

# Show weight coefficient comparison
print("\n=== Weight Coefficients ===")
wc3 = pd.read_excel(v3, sheet_name="Weight Coefficients")
wc4 = pd.read_excel(v4, sheet_name="Weight Coefficients")
print("\nv3 (population slopes):")
print(wc3.to_string(index=False))
print("\nv4 (within-horse slopes):")
print(wc4.to_string(index=False))
