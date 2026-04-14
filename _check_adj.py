"""Check v4.3 speed map adjustments in the JSON output."""
import json

with open("reports/race_day_report_20260412_v3.4.8.json", "r", encoding="utf-8") as f:
    data = json.load(f)

for race in data["races"]:
    rn = race["race_number"]
    dist = race.get("distance", "?")
    pl = race.get("pace", "?")
    picks = race.get("picks", [])

    print(f"=== R{rn}  {dist}m  pace={pl} ===")
    for p in sorted(picks, key=lambda x: x.get("rank", 99))[:6]:
        name = p.get("horse_name", "?")
        rank = p.get("rank", "?")
        pt = p.get("projected_time", "?")
        ppp = p.get("proj_pre_pace", "?")
        sadj = p.get("smap_total_adj", 0)
        spadj = p.get("smap_pos_adj", 0)
        spps = p.get("smap_pps_adj", 0)
        snotes = p.get("smap_adj_notes", "")
        print(f"  #{rank} {name:22s}  pre={ppp}  proj={pt}  pos={spadj:+.4f} pps={spps:+.4f} total={sadj:+.4f}  [{snotes}]")
    print()
