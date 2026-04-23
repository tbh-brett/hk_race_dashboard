"""Merge $env:TEMP\hkjc_results.xlsx (Apr 19 fresh full scrape) into hkjc_results_updated.xlsx."""
import os, shutil, tempfile
from pathlib import Path
import pandas as pd

BASE = Path(__file__).parent
DB = BASE / "hkjc_results_updated.xlsx"
FRESH = Path(tempfile.gettempdir()) / "hkjc_results.xlsx"
TARGET_DATE = "2026-04-19"

fresh = pd.read_excel(FRESH)
fresh["race_date"] = pd.to_datetime(fresh["race_date"], errors="coerce")
print(f"Fresh rows: {len(fresh)} | races: {sorted(fresh['race_number'].unique())}")

# Read existing DB via temp-copy (OneDrive lock workaround)
tmp_in = Path(tempfile.gettempdir()) / "hkjc_results_updated_in.xlsx"
shutil.copy2(DB, tmp_in)
existing = pd.read_excel(tmp_in)
existing["race_date"] = pd.to_datetime(existing["race_date"], errors="coerce")
print(f"DB rows before: {len(existing)}")

target = pd.Timestamp(TARGET_DATE)
before = len(existing)
existing = existing[existing["race_date"] != target]
print(f"Removed existing {TARGET_DATE} rows: {before - len(existing)}")

# Align columns both ways
for c in existing.columns:
    if c not in fresh.columns:
        fresh[c] = pd.NA
for c in fresh.columns:
    if c not in existing.columns:
        existing[c] = pd.NA
fresh = fresh[existing.columns]

combined = pd.concat([existing, fresh], ignore_index=True)
combined = combined.sort_values(["race_date", "race_number"]).reset_index(drop=True)
print(f"DB rows after: {len(combined)}")

tmp_out = Path(tempfile.gettempdir()) / "hkjc_results_updated_out.xlsx"
combined.to_excel(tmp_out, index=False)
try:
    shutil.copy2(tmp_out, DB)
    print(f"OK: wrote {DB}")
except PermissionError:
    print(f"OneDrive lock — saved to {tmp_out}; copy manually.")
