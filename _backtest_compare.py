"""
Quick test: compare 3 scenarios
A) No adjustments at all (coeff=0, PPS disabled)
B) PPS only (coeff=0, PPS enabled)
C) Module 1 + PPS (coeff=0.08, PPS enabled)
"""
import os, sys, math, shutil, warnings, re
import numpy as np
import pandas as pd
from scipy import stats
warnings.filterwarnings("ignore")

os.chdir(r"c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards")
DB_SRC = os.path.join(os.environ["TEMP"], "hkjc_results_updated.xlsx")
DB_TMP = os.path.join(os.environ["TEMP"], "hkjc_backtest_ab.xlsx")
shutil.copy2(DB_SRC, DB_TMP)

print("Loading DB...")
db = pd.read_excel(DB_TMP)
db["race_date"] = pd.to_datetime(db["race_date"])

from backtest_smap_v43 import (
    reconstruct_speed_map_simple, compute_pace_bucket,
    PACE_POS_STYLE_ADJ, ROW_LABEL, safe_place,
    compute_esz_from_history, compute_style_from_history
)

meeting_dates = sorted(db["race_date"].unique())[-20:]

# Build races
races = []
for date in meeting_dates:
    dr = db[db["race_date"] == date]
    for rn in sorted(dr["race_number"].unique()):
        runners = dr[dr["race_number"] == rn].copy()
        runners["_place"] = runners["place"].apply(safe_place)
        valid = runners[runners["_place"] <= 14]
        if len(valid) < 4:
            continue
        dist = valid["distance"].iloc[0]
        if pd.isna(dist):
            continue
        venue = "HV" if "HV" in str(valid.get("race_track", pd.Series(["ST"])).iloc[0]) else "ST"
        rc = str(valid["race_course"].iloc[0])
        if pd.isna(rc) or rc == "nan":
            rc = "A"
        
        # Actual
        actual = {r["horse_name"]: safe_place(r["place"]) for _, r in valid.iterrows()}
        
        # Base residuals from prior form
        vt = valid[valid["finish_time_seconds"].notna() & (valid["finish_time_seconds"] > 0)]
        if len(vt) < 4:
            continue
        
        base = {}
        for _, r in vt.iterrows():
            horse = r["horse_name"]
            prior = db[(db["horse_name"] == horse) & (db["race_date"] < date) &
                       (db["finish_time_seconds"].notna()) & (db["finish_time_seconds"] > 0)].copy()
            if len(prior) == 0:
                base[horse] = 0.5
                continue
            prior = prior.sort_values("race_date")
            resids, wts = [], []
            lam, n = 0.85, len(prior)
            for idx, (_, pr) in enumerate(prior.iterrows()):
                pr_d = pr["distance"]
                ddiff = abs(int(pr_d) - int(dist)) if pd.notna(pr_d) else 999
                dw = {0: 1.0}.get(ddiff, 0.60 if ddiff <= 200 else (0.30 if ddiff <= 400 else 0.10))
                pr_mask = (db["race_date"] == pr["race_date"]) & (db["race_number"] == pr["race_number"])
                pt = db.loc[pr_mask & db["finish_time_seconds"].notna() & (db["finish_time_seconds"] > 0), "finish_time_seconds"]
                if len(pt) < 3:
                    continue
                resids.append(pr["finish_time_seconds"] - pt.median())
                wts.append(lam ** (n - 1 - idx) * dw)
            if resids:
                w = np.array(wts); w /= w.sum()
                base[horse] = float(np.dot(resids, w))
            else:
                base[horse] = 0.5
        
        # Speed map
        try:
            smap = reconstruct_speed_map_simple(valid, int(dist), venue, rc)
        except:
            continue
        
        pace = compute_pace_bucket(valid, db, int(dist), date)
        
        races.append({"actual": actual, "base": base, "smap": smap, "pace": pace, "dist": int(dist)})

print(f"{len(races)} races ready\n")

scenarios = {
    "A) No adjustments":       (0.00, False),
    "B) PPS only":             (0.00, True),
    "C) Mod1+PPS (coeff=0.06)":(0.06, True),
    "D) Mod1+PPS (coeff=0.08)":(0.08, True),
    "E) Mod1+PPS (coeff=0.10)":(0.10, True),
}

print(f"{'Scenario':30s}  {'Spearman':>8}  {'Top3%':>6}  {'Win%':>5}  {'MAE':>5}")
print("-" * 62)

for label, (coeff, use_pps) in scenarios.items():
    proj_ranks, act_places = [], []
    top3h, top3t, winh, wint = 0, 0, 0, 0
    
    for rd in races:
        smap = rd["smap"]
        base = rd["base"]
        actual = rd["actual"]
        pace = rd["pace"]
        
        adj = {}
        for h in smap:
            advantage = h.get("smap_advantage", 0.0)
            row = h.get("smap_row", 2)
            style = h.get("dominant_style", "Unknown")
            rl = ROW_LABEL.get(row, "W2")
            pa = -advantage * coeff
            pa = max(-0.15, min(0.15, pa))
            pps = PACE_POS_STYLE_ADJ.get((pace, rl, style), 0.0) if use_pps and style in ("Leader","On-Pace","Midfield","Closer") else 0.0
            adj[h["horse_name"]] = pa + pps
        
        times = {n: base[n] + adj.get(n,0.0) for n in base}
        if len(times) < 4:
            continue
        
        ranked = sorted(times.items(), key=lambda x: x[1])
        pranks = {n: r+1 for r, (n,_) in enumerate(ranked)}
        
        common = [n for n in pranks if n in actual and actual[n] <= 14]
        if len(common) < 4:
            continue
        
        for n in common:
            proj_ranks.append(pranks[n])
            act_places.append(actual[n])
        
        our_top3 = set(list(pranks.keys())[:3])
        act_top3 = set(n for n, p in actual.items() if p <= 3)
        top3h += len(our_top3 & act_top3)
        top3t += min(3, len(act_top3))
        
        if actual.get(ranked[0][0], 99) == 1:
            winh += 1
        wint += 1
    
    rho, _ = stats.spearmanr(proj_ranks, act_places)
    mae = np.mean(np.abs(np.array(proj_ranks) - np.array(act_places)))
    t3p = 100*top3h/top3t if top3t else 0
    wp = 100*winh/wint if wint else 0
    print(f"  {label:28s}  {rho:8.4f}  {t3p:5.1f}%  {wp:4.1f}%  {mae:5.2f}")
