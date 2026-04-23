"""Aggregate April 2026 trip features vs finish position.

Tests the hypothesis: does trip quality (rail vs wide) systematically
affect finish position, above and beyond running style?
"""
import json
import os
from pathlib import Path
import numpy as np
from scipy.stats import spearmanr

BASE = Path(__file__).parent
PHOTOS = BASE / "running_position_photos"
REPORTS = BASE / "reports"

rows = []  # (date, race, horse, place, avg_x, avg_y, n_frames, wide_all, rail_all)
for day_dir in sorted(PHOTOS.iterdir()):
    if not day_dir.is_dir():
        continue
    dc = day_dir.name
    res_path = REPORTS / f"results_{dc}.json"
    if not res_path.exists():
        continue
    res = json.load(open(res_path, "r", encoding="utf-8"))
    pos_by_race_name = {}
    for race in res.get("races", []):
        rn = race.get("race_number")
        for r in race.get("runners", []):
            try:
                place = int(r.get("place", 99))
            except Exception:
                place = 99
            pos_by_race_name[(rn, r.get("horse_name", "").upper())] = place

    for jpath in sorted(day_dir.glob("R*.json")):
        trip = json.load(open(jpath, "r", encoding="utf-8"))
        rn = trip["race_number"]
        for h in trip["horses"]:
            key = (rn, h["horse_name"].upper())
            if key not in pos_by_race_name:
                continue
            place = pos_by_race_name[key]
            if place >= 99:
                continue
            rows.append({
                "date":  dc,
                "race":  rn,
                "horse": h["horse_name"],
                "place": place,
                "avg_x": h["avg_x_frac"],
                "avg_y": h["avg_y_in_band"],
                "seen":  h["n_frames_seen"],
                "wide_all": h["wide_all_way"],
                "rail_all": h["rail_all_way"],
            })

print(f"Total horse-runs correlated: {len(rows)}")
if not rows:
    exit()

# --- Average finish by trip category ---
def summarise(filter_fn, label):
    subset = [r for r in rows if filter_fn(r)]
    if not subset:
        print(f"  {label}: n=0")
        return
    places = [r["place"] for r in subset]
    print(f"  {label}: n={len(subset):>3d}  avg finish={np.mean(places):>4.2f}  "
          f"median={np.median(places):>4.1f}  win%={100*sum(1 for p in places if p==1)/len(places):>4.1f}  "
          f"top3%={100*sum(1 for p in places if p<=3)/len(places):>4.1f}")

print("\nBY TRIP TYPE (all races):")
summarise(lambda r: r["rail_all"],  "RAIL all way  (y<0.35 each frame) ")
summarise(lambda r: r["wide_all"],  "WIDE all way  (y>0.55 each frame) ")
summarise(lambda r: (r["avg_y"] or 1) < 0.35, "avg_y < 0.35 (on rail overall)    ")
summarise(lambda r: 0.35 <= (r["avg_y"] or 0) < 0.55, "0.35 <= avg_y < 0.55 (1-2 out)    ")
summarise(lambda r: (r["avg_y"] or 0) >= 0.55, "avg_y >= 0.55 (wide/outside)      ")

print("\nCORRELATIONS (all runs with place info):")
ys = [r["avg_y"] for r in rows if r["avg_y"] is not None]
ps = [r["place"] for r in rows if r["avg_y"] is not None]
rho, p = spearmanr(ys, ps)
print(f"  avg_y  vs place:  Spearman ρ = {rho:+.3f}  (p={p:.3g})  n={len(ys)}")

xs = [r["avg_x"] for r in rows if r["avg_x"] is not None]
ps2 = [r["place"] for r in rows if r["avg_x"] is not None]
rho, p = spearmanr(xs, ps2)
print(f"  avg_x  vs place:  Spearman ρ = {rho:+.3f}  (p={p:.3g})  n={len(xs)}  "
      f"(avg_x ≈ running_position leader-rank)")

# Most striking examples: WIDE trips that still won / placed
print("\nHORSES THAT OVERCAME WIDE TRIP (wide_all=True, place <=3):")
wide_top = [r for r in rows if r["wide_all"] and r["place"] <= 3]
wide_top.sort(key=lambda r: (r["place"], r["date"]))
for r in wide_top[:15]:
    print(f"  {r['date']} R{r['race']:>2d}  P{r['place']}  {r['horse']:<22s}  avg_y={r['avg_y']:.3f}")

print("\nHORSES CAUGHT WIDE + LOST (wide_all=True, place >=10):")
wide_bad = [r for r in rows if r["wide_all"] and r["place"] >= 10]
wide_bad.sort(key=lambda r: -r["place"])
for r in wide_bad[:15]:
    print(f"  {r['date']} R{r['race']:>2d}  P{r['place']}  {r['horse']:<22s}  avg_y={r['avg_y']:.3f}")

print("\nSAVED GROUND, OUTPERFORMED (rail_all=True, place <=3):")
rail_top = [r for r in rows if r["rail_all"] and r["place"] <= 3]
rail_top.sort(key=lambda r: (r["place"], r["date"]))
for r in rail_top[:15]:
    print(f"  {r['date']} R{r['race']:>2d}  P{r['place']}  {r['horse']:<22s}  avg_y={r['avg_y']:.3f}")
