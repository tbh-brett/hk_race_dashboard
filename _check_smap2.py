import json

with open("reports/race_day_report_20260412_v3.4.8.json", "r", encoding="utf-8") as f:
    data = json.load(f)

row_map = {"RAIL": 1, "W2": 2, "WIDE": 3}

for race in data["races"]:
    rn = race["race_number"]
    sm = race.get("speed_map", {})
    grid = sm.get("grid", [])
    nc = sm.get("n_cols")
    nr = sm.get("n_rows")
    dist = race.get("distance", "?")
    fs = len(race.get("picks", []))
    print(f"=== R{rn}  {dist}m  field={fs}  ({nc} cols x {nr} rows) ===")
    for row_name in ["WIDE", "W2", "RAIL"]:
        row_num = row_map[row_name]
        cells = [(g["col"], g["horse_name"]) for g in grid if g["row"] == row_num]
        cells.sort()
        print(f"  {row_name}: {cells}")
    print()
