"""Append results_20260412.json to the database, then run backtest for Apr 12."""
import json, os, shutil, tempfile
import pandas as pd
from pathlib import Path

BASE = Path(r"c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards")
TEMP = Path(os.environ.get("TEMP", str(BASE)))
RESULTS_JSON = BASE / "reports" / "results_20260412.json"
DB_ONEDRIVE = BASE / "hkjc_results_updated.xlsx"
DB_TEMP = TEMP / "hkjc_results_updated.xlsx"

# ── 1. Read results JSON ──────────────────────────────────────────────────────
with open(RESULTS_JSON, "r", encoding="utf-8") as f:
    data = json.load(f)

print(f"Results: {data.get('date')}  {data.get('venue')}  {len(data.get('races',[]))} races")

new_rows = []
race_date = data.get("date", "")
venue = data.get("venue", "")
race_track = "ST" if venue.upper() in ("ST", "SHA TIN") else (
    "HV" if venue.upper() in ("HV", "HAPPY VALLEY") else venue)

for race in data.get("races", []):
    is_awt = race.get("is_awt", False)
    track_type = "All Weather Track" if is_awt else "Turf"
    for runner in race.get("runners", []):
        positions_list = runner.get("positions", [])
        running_positions = " ".join(p for p in positions_list if p) if positions_list else ""
        sectiontimes_list = runner.get("sectiontimes", [])
        sectiontimes_str = "; ".join(s for s in sectiontimes_list if s) if sectiontimes_list else ""
        new_rows.append({
            "race_date": race_date,
            "race_number": race.get("race_number"),
            "horse_number": runner.get("horse_no"),
            "horse_name": runner.get("horse_name", ""),
            "place": runner.get("place", ""),
            "jockey": runner.get("jockey", ""),
            "trainer": runner.get("trainer", ""),
            "actual_weight": runner.get("actual_weight"),
            "declared_weight": runner.get("declared_weight"),
            "draw": runner.get("draw"),
            "lbw": runner.get("lbw", ""),
            "running_positions": running_positions,
            "finish_time_seconds": runner.get("finish_time_seconds"),
            "win_odds": runner.get("win_odds", ""),
            "going": race.get("going", ""),
            "race_class": race.get("race_class", ""),
            "race_course": race.get("race_course", ""),
            "race_track": race_track,
            "track_type": track_type,
            "distance": race.get("distance"),
            "sectiontimes": sectiontimes_str,
        })

print(f"  New rows: {len(new_rows)}")
new_df = pd.DataFrame(new_rows)

# ── 2. Append to TEMP DB ─────────────────────────────────────────────────────
db = pd.read_excel(DB_TEMP)
# Remove any existing Apr 12 rows (in case re-running)
db = db[db["race_date"].astype(str) != race_date].copy()
combined = pd.concat([db, new_df], ignore_index=True)
combined.to_excel(DB_TEMP, index=False)
print(f"  TEMP DB: {len(db)} → {len(combined)} rows (saved to {DB_TEMP})")

# ── 3. Also update OneDrive copy ─────────────────────────────────────────────
try:
    # Backup first
    if DB_ONEDRIVE.exists():
        shutil.copy2(DB_ONEDRIVE, DB_ONEDRIVE.with_suffix(".xlsx.bak"))
    shutil.copy2(DB_TEMP, DB_ONEDRIVE)
    print(f"  OneDrive DB updated: {DB_ONEDRIVE}")
except PermissionError:
    print(f"  ⚠ OneDrive lock — TEMP copy updated, OneDrive copy skipped")

print("\nDone — DB updated with Apr 12 results.")
