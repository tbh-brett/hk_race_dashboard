"""Temporary script: within-horse weight sensitivity analysis."""
import os, shutil, pandas as pd, numpy as np
from scipy import stats

DB_SRC = os.path.join(os.environ["TEMP"], "hkjc_results_updated.xlsx")
DB_TMP = os.path.join(os.environ["TEMP"], "hkjc_wt_panel.xlsx")
shutil.copy2(DB_SRC, DB_TMP)
db = pd.read_excel(DB_TMP)
db["race_date"] = pd.to_datetime(db["race_date"])

results = []
for horse, grp in db.groupby("horse_name"):
    dc = grp["distance"].value_counts()
    if len(dc) == 0:
        continue
    main_dist = dc.index[0]
    runs = grp[
        (grp["distance"] == main_dist)
        & grp["finish_time_seconds"].notna()
        & grp["actual_weight"].notna()
    ]
    if len(runs) < 6:
        continue
    sl, ic, r, p, se = stats.linregress(
        runs["actual_weight"], runs["finish_time_seconds"]
    )
    results.append({
        "horse": horse, "dist": main_dist, "n": len(runs),
        "wt_range": runs["actual_weight"].max() - runs["actual_weight"].min(),
        "slope": sl, "r": r, "p": p, "se": se,
        "mean_wt": runs["actual_weight"].mean(),
    })

res = pd.DataFrame(results)
print(f"Horses with 6+ runs at single distance: {len(res)}")
print(f"\nOVERALL within-horse weight effect:")
print(f"  Median slope: {res['slope'].median():.4f}s per lb")
print(f"  Mean slope:   {res['slope'].mean():.4f}s per lb")
print(f"  25th/75th:    {res['slope'].quantile(0.25):.4f} / {res['slope'].quantile(0.75):.4f}")
print(f"  % negative (heavier=faster): {(res['slope'] < 0).mean()*100:.1f}%")
print(f"  % significant p<0.10: {(res['p'] < 0.10).mean()*100:.1f}%")

print(f"\nBY DISTANCE:")
for d in sorted(res["dist"].unique()):
    s = res[res["dist"] == d]
    if len(s) < 10:
        continue
    print(f"  {int(d)}m: n={len(s)}, median slope={s['slope'].median():.4f}, "
          f"mean={s['slope'].mean():.4f}, sig%={(s['p'] < 0.10).mean()*100:.0f}%")

print(f"\nMOST WEIGHT-SENSITIVE (positive slope = slower when heavier):")
top = res[res["p"] < 0.15].sort_values("slope", ascending=False).head(8)
for _, r in top.iterrows():
    print(f"  {r['horse']:<25s} {int(r['dist'])}m  slope={r['slope']:+.3f}s/lb  "
          f"r={r['r']:.3f}  p={r['p']:.3f}  n={r['n']}  range={r['wt_range']:.0f}lbs")

print(f"\nIMPROVES WITH WEIGHT (negative slope — selection/improvement effect):")
neg = res[res["p"] < 0.15].sort_values("slope").head(8)
for _, r in neg.iterrows():
    print(f"  {r['horse']:<25s} {int(r['dist'])}m  slope={r['slope']:+.3f}s/lb  "
          f"r={r['r']:.3f}  p={r['p']:.3f}  n={r['n']}  range={r['wt_range']:.0f}lbs")

# How does the current ET system handle weight?
# Population-level: median finish time by weight band per distance
print(f"\nPOPULATION-LEVEL weight band medians (what ET tables approximate):")
for dist in [1200, 1400, 1600]:
    d = db[(db["distance"] == dist) & db["finish_time_seconds"].notna() & db["actual_weight"].notna()]
    bands = [(105, 115), (116, 120), (121, 125), (126, 130), (131, 135)]
    print(f"  {dist}m:")
    prev = None
    for lo, hi in bands:
        sub = d[(d["actual_weight"] >= lo) & (d["actual_weight"] <= hi)]
        if len(sub) < 20:
            continue
        med = sub["finish_time_seconds"].median()
        delta = f"  Δ={med - prev:+.2f}s" if prev else ""
        prev = med
        print(f"    {lo}-{hi}lb: median={med:.2f}s  n={len(sub)}{delta}")

# KEY QUESTION: does the within-horse slope differ from the population ET gap?
print(f"\n{'='*80}")
print("KEY COMPARISON: within-horse vs population weight slope")
print(f"{'='*80}")
# Population: regress finish_time on weight per distance, controlling for class
for dist in [1200, 1400, 1600]:
    d = db[(db["distance"] == dist) & db["finish_time_seconds"].notna()
           & db["actual_weight"].notna() & db["race_class"].notna()]
    # Simple population regression
    sl_pop, _, _, p_pop, _ = stats.linregress(d["actual_weight"], d["finish_time_seconds"])
    
    # Within-horse mean
    wh = res[res["dist"] == dist]
    if len(wh) < 10:
        continue
    sl_wh = wh["slope"].median()
    
    print(f"  {dist}m:")
    print(f"    Population slope (cross-sectional): {sl_pop:+.4f}s/lb  (CONFOUNDED by ability)")
    print(f"    Within-horse slope (panel, median):  {sl_wh:+.4f}s/lb  (TRUE causal estimate)")
    print(f"    Difference: {sl_wh - sl_pop:+.4f}s/lb")

# Interaction with running position: do heavy horses change position?
print(f"\n{'='*80}")
print("WEIGHT × RUNNING POSITION interaction (within-horse)")
print(f"{'='*80}")
import re
def parse_first_pos(rp):
    if pd.isna(rp): return None
    m = re.match(r"(\d+)", str(rp).strip())
    return int(m.group(1)) if m else None

db["_first_pos"] = db["running_positions"].apply(parse_first_pos)
# For horses with 8+ runs at a distance
for horse, grp in db.groupby("horse_name"):
    dc = grp["distance"].value_counts()
    if len(dc) == 0: continue
    md = dc.index[0]
    runs = grp[(grp["distance"] == md) & grp["finish_time_seconds"].notna()
               & grp["actual_weight"].notna() & grp["_first_pos"].notna()]
    if len(runs) < 8: continue
    # Does weight correlate with running position for this horse?
    r_wp, p_wp = stats.pearsonr(runs["actual_weight"], runs["_first_pos"])
    if abs(r_wp) > 0.4 and p_wp < 0.10:
        wt_lo = runs[runs["actual_weight"] <= runs["actual_weight"].median()]
        wt_hi = runs[runs["actual_weight"] > runs["actual_weight"].median()]
        print(f"  {horse}: weight-position r={r_wp:.3f} p={p_wp:.3f} "
              f"light_pos={wt_lo['_first_pos'].mean():.1f} heavy_pos={wt_hi['_first_pos'].mean():.1f} "
              f"n={len(runs)}")
