"""Direct test: check if smap adjustments are in the output dataframe."""
import json

with open("reports/race_day_report_20260412_v3.4.8.json", "r", encoding="utf-8") as f:
    data = json.load(f)

# Check race level: "pace" field
r5 = [r for r in data["races"] if r["race_number"] == 5][0]
print(f"R5 pace field: '{r5.get('pace', 'MISSING')}'")
print(f"R5 pace_label: '{r5.get('pace_label', 'MISSING')}'")
print()

# Check what fields are in a pick
p0 = r5["picks"][0]
print(f"Pick fields: {sorted(p0.keys())}")
print()

# Now manually compute what the adjustments SHOULD be
# Check smap_advantage in the speed_map grid
grid = r5["speed_map"]["grid"]
for g in sorted(grid, key=lambda x: x["col"], reverse=True)[:6]:
    print(f"  {g['horse_name']:22s}  col={g['col']} row={g['row']} adv={g['advantage']:+.2f} style={g['style']}")

