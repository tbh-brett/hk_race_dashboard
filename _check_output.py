import json
with open(r"c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards\reports\race_day_report_20260412_v4.4.json") as f:
    data = json.load(f)

# Discover structure
r0 = data["races"][0]
print("Race keys:", list(r0.keys()))
# Find the horse list key
for k, v in r0.items():
    if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
        print(f"\nHorse key: '{k}'  ({len(v)} entries)")
        print("Horse keys:", list(v[0].keys()))
        for h in v[:3]:
            print(f"  {h}")
        break
