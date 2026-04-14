"""Check running_positions format across distances in the historical DB."""
import pandas as pd
import os, shutil

SRC = os.path.join(os.environ["TEMP"], "hkjc_results_updated.xlsx")
TMP = os.path.join(os.environ["TEMP"], "hkjc_db_check.xlsx")
shutil.copy2(SRC, TMP)
db = pd.read_excel(TMP)

print(f"Total rows: {len(db)}")
print(f"Columns: {list(db.columns)}\n")

# Check running_positions format by distance
distances = sorted(db["distance"].dropna().unique())
for dist in distances:
    subset = db[db["distance"] == dist]
    rp_valid = subset["running_positions"].dropna()
    if len(rp_valid) == 0:
        print(f"{int(dist)}m: no running_positions data")
        continue
    
    # Count number of calls (space-separated)
    n_calls = rp_valid.apply(lambda x: len(str(x).strip().split()))
    print(f"{int(dist)}m: {len(rp_valid)} rows with RP, "
          f"calls: min={n_calls.min()}, max={n_calls.max()}, "
          f"median={n_calls.median():.0f}, mode={n_calls.mode().iloc[0]}")
    # Show 3 examples
    for ex in rp_valid.head(3):
        print(f"  example: '{ex}'")
    print()

# Check sectiontimes format similarly
print("=== SECTION TIMES ===")
for dist in [1000, 1200, 1400, 1600, 1800, 2000, 2400]:
    subset = db[db["distance"] == dist]
    st_valid = subset["sectiontimes"].dropna()
    if len(st_valid) == 0:
        continue
    n_parts = st_valid.apply(lambda x: len(str(x).strip().split(";")))
    print(f"{dist}m: {len(st_valid)} rows, "
          f"parts: min={n_parts.min()}, max={n_parts.max()}, "
          f"median={n_parts.median():.0f}")
    for ex in st_valid.head(3):
        print(f"  example: '{ex}'")
    print()

# Check last 20 meetings for backtest
print("=== LAST 20 MEETINGS ===")
db["race_date"] = pd.to_datetime(db["race_date"])
meeting_dates = db.groupby("race_date")["race_number"].nunique().reset_index()
meeting_dates.columns = ["date", "n_races"]
meeting_dates = meeting_dates.sort_values("date", ascending=False)
last_20 = meeting_dates.head(20)
print(f"Date range: {last_20['date'].min().date()} to {last_20['date'].max().date()}")
print(f"Total races: {last_20['n_races'].sum()}")
total_rows = db[db["race_date"].isin(last_20["date"])].shape[0]
print(f"Total rows (horse-race): {total_rows}")
print(last_20.to_string(index=False))
