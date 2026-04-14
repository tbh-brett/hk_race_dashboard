import json

with open(r"c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards\reports\race_day_report_20260412_v4.4.json", encoding="utf-8") as f:
    d = json.load(f)

for r in d["races"]:
    rn = r["race_number"]
    dist = r["distance"]
    runners = r["runners"]
    proj = r["projected"]
    picks = len(r["picks"])
    print(f"Race {rn}: {dist}m  runners={runners}  projected={proj}  picks={picks}")

# Show picks for first race with projections
for r in d["races"]:
    if r["picks"]:
        print(f"\n=== Race {r['race_number']} picks ===")
        for p in r["picks"][:5]:
            print(f"  {p}")
        break
