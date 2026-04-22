"""Merge 2026 JSON results into xlsx using proper schema mapping."""
import json, shutil, tempfile, time
from pathlib import Path
import pandas as pd

BASE = Path(__file__).parent
DB   = BASE / "hkjc_results_updated.xlsx"
TMP  = Path(tempfile.gettempdir()) / DB.name
RPTS = BASE / "reports"

# Read existing (try DB, fall back to TEMP copy)
try:
    existing = pd.read_excel(DB)
    print(f"read DB: {len(existing)} rows")
except PermissionError:
    print(f"DB locked; reading {TMP}")
    existing = pd.read_excel(TMP)
    print(f"read TEMP: {len(existing)} rows")

existing["race_date"] = pd.to_datetime(existing["race_date"], errors="coerce").dt.strftime("%Y-%m-%d")
print(f"  existing dates: {existing['race_date'].nunique()}")

# Rebuild rows for every 2026 JSON using same logic as db_utils.append_results_to_db
new_rows = []
for jf in sorted(RPTS.glob("results_2026*.json")):
    data = json.loads(jf.read_text(encoding="utf-8"))
    race_date = data.get("date", "")
    venue = (data.get("venue") or "").upper()
    race_track = "ST" if venue in ("ST", "SHA TIN") else ("HV" if venue in ("HV", "HAPPY VALLEY") else venue)
    for race in data.get("races", []):
        is_awt = bool(race.get("is_awt"))
        track_type = "All Weather Track" if is_awt else "Turf"
        for runner in race.get("runners", []):
            pos_list = runner.get("positions", []) or []
            running_positions = " ".join(p for p in pos_list if p)
            sec_list = runner.get("sectiontimes", []) or []
            sectiontimes_str = "; ".join(s for s in sec_list if s)
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

new_df = pd.DataFrame(new_rows)
new_df["race_date"] = pd.to_datetime(new_df["race_date"], errors="coerce").dt.strftime("%Y-%m-%d")
print(f"  new: {len(new_df)} rows across {new_df['race_date'].nunique()} dates")

refresh_dates = set(new_df["race_date"].unique())
before = len(existing)
keep = existing[~existing["race_date"].isin(refresh_dates)]
print(f"  dropping {before - len(keep)} stale rows for {len(refresh_dates)} dates")

all_cols = list(dict.fromkeys(list(keep.columns) + list(new_df.columns)))
combined = pd.concat([keep.reindex(columns=all_cols), new_df.reindex(columns=all_cols)], ignore_index=True)
combined = combined.sort_values(["race_date", "race_number"], kind="stable").reset_index(drop=True)
print(f"  combined: {len(combined)} rows, {combined['race_date'].nunique()} dates")

combined.to_excel(TMP, index=False)
print(f"  wrote TEMP {TMP} ({TMP.stat().st_size:,} bytes)")

try:
    shutil.copy2(DB, DB.with_suffix(".xlsx.bak"))
except Exception as e:
    print(f"  backup skipped: {e}")

for attempt in range(10):
    try:
        shutil.copy2(TMP, DB)
        print(f"[OK] wrote {DB}")
        break
    except PermissionError as e:
        print(f"  attempt {attempt+1}: lock; retrying 5s ({e})")
        time.sleep(5)
else:
    print(f"[WARN] could not write to {DB}; data available at {TMP}")
