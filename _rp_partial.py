"""Partial-correlation test: does trip width (avg_y) predict finish
position AFTER controlling for running-order position (avg_x)?

Also: does trip width within running-style cohorts matter?
"""
import json
from pathlib import Path
import numpy as np
from scipy.stats import spearmanr

BASE = Path(__file__).parent
PHOTOS = BASE / "running_position_photos"
REPORTS = BASE / "reports"

rows = []
for day_dir in sorted(PHOTOS.iterdir()):
    if not day_dir.is_dir(): continue
    dc = day_dir.name
    res_path = REPORTS / f"results_{dc}.json"
    if not res_path.exists(): continue
    res = json.load(open(res_path, "r", encoding="utf-8"))
    pos_by = {}
    field_by = {}
    for race in res.get("races", []):
        rn = race.get("race_number")
        field_by[rn] = len(race.get("runners", []))
        for r in race.get("runners", []):
            try: place = int(r.get("place", 99))
            except: place = 99
            pos_by[(rn, r.get("horse_name","").upper())] = place
    for jp in sorted(day_dir.glob("R*.json")):
        trip = json.load(open(jp, "r", encoding="utf-8"))
        rn = trip["race_number"]
        for h in trip["horses"]:
            key = (rn, h["horse_name"].upper())
            if key not in pos_by: continue
            place = pos_by[key]
            if place >= 99: continue
            # Normalize place by field size (percentile rank: 0=winner, 1=last)
            fs = field_by.get(rn, 14)
            place_pct = (place - 1) / max(1, fs - 1)
            rows.append({
                "x": h["avg_x_frac"], "y": h["avg_y_in_band"],
                "place": place, "place_pct": place_pct, "fs": fs,
                "wide": h["wide_all_way"], "rail": h["rail_all_way"],
            })

xs  = np.array([r["x"] for r in rows])
ys  = np.array([r["y"] for r in rows])
pp  = np.array([r["place_pct"] for r in rows])

# Residualise place_pct on x (linear regression), then corr residuals with y
def residuals(target, pred):
    A = np.vstack([pred, np.ones_like(pred)]).T
    b, _, _, _ = np.linalg.lstsq(A, target, rcond=None)
    fitted = A @ b
    return target - fitted, b

resid_place, _ = residuals(pp, xs)
rho_raw, p_raw = spearmanr(ys, pp)
rho_res, p_res = spearmanr(ys, resid_place)
print(f"n={len(rows)}")
print(f"RAW:      avg_y vs place_pct           rho={rho_raw:+.3f}  p={p_raw:.3g}")
print(f"PARTIAL:  avg_y vs place_pct | avg_x   rho={rho_res:+.3f}  p={p_res:.3g}")

# By running-style cohort (avg_x bins)
print("\nBY RUNNING-STYLE COHORT (bins of avg_x):")
bins = [(0.00, 0.25, "LEADER   "), (0.25, 0.50, "HANDY    "),
        (0.50, 0.75, "MID-PACK "), (0.75, 1.01, "BACK     ")]
for lo, hi, lbl in bins:
    sub = [r for r in rows if lo <= r["x"] < hi]
    if len(sub) < 20:
        print(f"  {lbl} n={len(sub):>3d}  (skip, too few)"); continue
    ys_s  = [r["y"] for r in sub]
    pps_s = [r["place_pct"] for r in sub]
    rho, p = spearmanr(ys_s, pps_s)
    avg_pp = np.mean(pps_s)
    # Split wide vs rail within cohort
    wide_sub = [r for r in sub if r["wide"]]
    rail_sub = [r for r in sub if r["rail"]]
    wp = np.mean([r["place_pct"] for r in wide_sub]) if wide_sub else float("nan")
    rp = np.mean([r["place_pct"] for r in rail_sub]) if rail_sub else float("nan")
    print(f"  {lbl} n={len(sub):>3d}  avg place%={avg_pp:.2f}  "
          f"rho(y,place%)={rho:+.3f} (p={p:.2g})  "
          f"wide_all avg={wp:.2f} (n={len(wide_sub)})  "
          f"rail_all avg={rp:.2f} (n={len(rail_sub)})")

# Unusual: leaders on the rail should dominate; leaders trapped wide should still win
print("\nLEADER COHORT (avg_x<0.25) BY LANE:")
leaders = [r for r in rows if r["x"] < 0.25]
for lo, hi, lbl in [(0,0.35,"rail lead"),(0.35,0.55,"1-2 out"),(0.55,1.01,"wide lead")]:
    sub = [r for r in leaders if lo <= r["y"] < hi]
    if not sub: continue
    places = [r["place"] for r in sub]
    win = sum(1 for p in places if p == 1)
    top3 = sum(1 for p in places if p <= 3)
    print(f"  {lbl:<10s} n={len(sub):>3d}  win%={100*win/len(sub):>4.1f}  top3%={100*top3/len(sub):>4.1f}  avg place%={np.mean([r['place_pct'] for r in sub]):.2f}")
