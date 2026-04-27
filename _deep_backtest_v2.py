"""
_deep_backtest_v2.py — Layer-2 analysis on top of deep_backtest.json
  - Per-class breakdown
  - ET bias correction simulation (subtract +0.66s)
  - When-only-X-is-right analysis (which model uniquely catches winners)
  - Pace accuracy by venue
  - Win odds context: how does model pick odds compare to market top-3?
"""
from __future__ import annotations
import json
from pathlib import Path
from statistics import mean, median, stdev
from collections import defaultdict
import math, re

BASE = Path(__file__).parent
data = json.loads((BASE/"cache"/"deep_backtest.json").read_text(encoding="utf-8"))
rows = data["rows"]
pace = data["pace"]
proj = data["proj_err"]

print(f"=== Deep Backtest v2 — {len(rows)} races ===\n")

# ───────────────────────────── PER-CLASS ─────────────────────────────
def class_bucket(rc):
    if not rc: return "?"
    rc=str(rc).upper()
    if "G1" in rc or "GROUP 1" in rc: return "G1"
    if "G2" in rc or "GROUP 2" in rc: return "G2"
    if "G3" in rc or "GROUP 3" in rc: return "G3"
    if "GRIFFIN" in rc: return "Griffin"
    if "GRADUATION" in rc or "RESTRICTED" in rc: return "Restricted"
    m=re.search(r"CLASS\s*(\d)",rc)
    if m: return f"C{m.group(1)}"
    if rc.startswith("4 YEAR") or rc.startswith("4-YEAR") or "4YO" in rc: return "4yo"
    return rc[:14]

print("━━━ BY RACE CLASS ━━━")
print(f"{'Class':<14} {'n':>3}  {'ET-W':>6} {'ET-P':>6}  {'SA-W':>6} {'SA-P':>6}  {'MK-W':>6} {'MK-P':>6}")
by_cls = defaultdict(list)
for r in rows: by_cls[class_bucket(r["race_class"])].append(r)
for cls in sorted(by_cls.keys()):
    rs=by_cls[cls]
    if not rs: continue
    e_w=mean([r["et"]["win"] for r in rs if r["et"]])*100
    e_p=mean([r["et"]["plc"] for r in rs if r["et"]])*100
    s_w=mean([r["sa"]["win"] for r in rs if r["sa"]])*100
    s_p=mean([r["sa"]["plc"] for r in rs if r["sa"]])*100
    m_w=mean([r["mk"]["win"] for r in rs if r["mk"]])*100
    m_p=mean([r["mk"]["plc"] for r in rs if r["mk"]])*100
    print(f"  {cls:<12} n={len(rs):>3}  {e_w:>5.1f}% {e_p:>5.1f}%  {s_w:>5.1f}% {s_p:>5.1f}%  {m_w:>5.1f}% {m_p:>5.1f}%")

# ───────────────────────────── ET BIAS CORRECTION ─────────────────────────────
print("\n━━━ ET PROJECTION RMSE: BIAS CORRECTION SIMULATION ━━━")
errs=[p["err"] for p in proj]
bias=mean(errs)
print(f"  Original: bias={bias:+.3f}s  RMSE={math.sqrt(mean(e*e for e in errs)):.3f}s")
# Try subtracting bias
errs_corr=[e-bias for e in errs]
rmse_corr=math.sqrt(mean(e*e for e in errs_corr))
print(f"  Bias-corrected: bias={mean(errs_corr):+.3f}s  RMSE={rmse_corr:.3f}s  ({(1-rmse_corr/math.sqrt(mean(e*e for e in errs)))*100:+.1f}% RMSE reduction)")
# Inside ±0.5s after correction
inside_05=sum(1 for e in errs_corr if abs(e)<0.5)
inside_05_orig=sum(1 for e in errs if abs(e)<0.5)
print(f"  |err|<0.5s: original={inside_05_orig}/{len(errs)} ({inside_05_orig/len(errs)*100:.0f}%) → corrected={inside_05}/{len(errs)} ({inside_05/len(errs)*100:.0f}%)")
# Was bias consistent across distance?
print("\n  Bias by distance (ET projection error for winners):")
def dbucket(d):
    try: d=int(d)
    except: return "?"
    if d<=1200: return "sprint(≤1200)"
    if d<=1650: return "mile(1300-1650)"
    return "route(>1650)"
# Need to merge with rows to get distance
proj_with_dist=[]
row_lookup={(r["date"],r["race"]):r for r in rows}
for p in proj:
    rk=(p["date"],p["race"])
    rw=row_lookup.get(rk)
    if rw:
        proj_with_dist.append({**p,"distance":rw["distance"],"dist_bucket":dbucket(rw["distance"])})
by_dist=defaultdict(list)
for p in proj_with_dist: by_dist[p["dist_bucket"]].append(p["err"])
for db in sorted(by_dist.keys()):
    es=by_dist[db]
    print(f"    {db:<18} n={len(es):>3}  bias={mean(es):+.3f}s  RMSE={math.sqrt(mean(e*e for e in es)):.3f}s")

# ───────────────────────────── UNIQUELY-CORRECT MODEL ─────────────────────────────
print("\n━━━ UNIQUELY-CORRECT TOP-1 (model catches winner; other doesn't) ━━━")
et_only_wins=0; sa_only_wins=0; both_wins=0; neither_wins=0
et_only_plc=0; sa_only_plc=0; both_plc=0; neither_plc=0
for r in rows:
    if not (r["et"] and r["sa"]): continue
    ew=r["et"]["win"]; sw=r["sa"]["win"]
    ep=r["et"]["plc"]; sp=r["sa"]["plc"]
    if ew and sw: both_wins+=1
    elif ew and not sw: et_only_wins+=1
    elif sw and not ew: sa_only_wins+=1
    else: neither_wins+=1
    if ep and sp: both_plc+=1
    elif ep and not sp: et_only_plc+=1
    elif sp and not ep: sa_only_plc+=1
    else: neither_plc+=1
n=len(rows)
print(f"  Top-1 WIN:")
print(f"    Both correct:    {both_wins}/{n} ({both_wins/n*100:.1f}%)")
print(f"    ET only:         {et_only_wins}/{n} ({et_only_wins/n*100:.1f}%)")
print(f"    SARR only:       {sa_only_wins}/{n} ({sa_only_wins/n*100:.1f}%)")
print(f"    Neither:         {neither_wins}/{n} ({neither_wins/n*100:.1f}%)")
print(f"  Top-1 PLACE:")
print(f"    Both correct:    {both_plc}/{n} ({both_plc/n*100:.1f}%)")
print(f"    ET only:         {et_only_plc}/{n} ({et_only_plc/n*100:.1f}%)")
print(f"    SARR only:       {sa_only_plc}/{n} ({sa_only_plc/n*100:.1f}%)")
print(f"    Neither:         {neither_plc}/{n} ({neither_plc/n*100:.1f}%)")

# ───────────────────────────── PACE BY VENUE ─────────────────────────────
print("\n━━━ PACE PREDICTION BY VENUE ━━━")
pace_by_venue=defaultdict(lambda: {"correct":0,"total":0,"close":0})
for r in rows:
    pp,ap=r.get("pace_predicted"),r.get("pace_actual")
    if not pp or not ap: continue
    v=r["venue"]
    pace_by_venue[v]["total"]+=1
    if pp==ap: pace_by_venue[v]["correct"]+=1
    fast={"Fast","Slightly Fast"}; slow={"Slow","Slightly Slow"}; norm={"Normal"}
    pside=lambda x: "F" if x in fast else ("S" if x in slow else "N")
    if pside(pp)==pside(ap): pace_by_venue[v]["close"]+=1
for v,st in sorted(pace_by_venue.items()):
    print(f"  {v}  n={st['total']:>3}  exact={st['correct']/st['total']*100:>5.1f}%  same-side={st['close']/st['total']*100:>5.1f}%")

# ───────────────────────────── ODDS POSITION OF MODEL PICKS ─────────────────────────────
print("\n━━━ MARKET RANK OF MODEL TOP-1 ━━━")
# We have et_top1_odds, sa_top1_odds, fav_odds. Find odds rank by sorting odds.
# Without per-runner odds we can't easily compute exact rank — approximation: how often is model top-1 == fav?
fav_pick_et=sum(1 for r in rows if r["et_top1"]==r["mkt_top1"])
fav_pick_sa=sum(1 for r in rows if r["sa_top1"]==r["mkt_top1"])
print(f"  ET top-1 = market fav:   {fav_pick_et}/{n} ({fav_pick_et/n*100:.1f}%)")
print(f"  SARR top-1 = market fav: {fav_pick_sa}/{n} ({fav_pick_sa/n*100:.1f}%)")
# Mean odds of top-1
et_odds=[r["et_top1_odds"] for r in rows if r["et_top1_odds"] is not None]
sa_odds=[r["sa_top1_odds"] for r in rows if r["sa_top1_odds"] is not None]
print(f"  ET top-1 median odds:    {median(et_odds):.1f}  (mean {mean(et_odds):.1f})")
print(f"  SARR top-1 median odds:  {median(sa_odds):.1f}  (mean {mean(sa_odds):.1f})")

# ───────────────────────────── COMBINED-MODEL VARIANTS ─────────────────────────────
print("\n━━━ COMBINED STRATEGIES ━━━")
# Strategy A: Bet only when ET and SARR agree on top-1
agreed=[r for r in rows if r["agree"]]
if agreed:
    aw=sum(r["et"]["win"] for r in agreed)/len(agreed)*100
    ap=sum(r["et"]["plc"] for r in agreed)/len(agreed)*100
    print(f"  A. Both agree top-1: n={len(agreed)} ({len(agreed)/n*100:.0f}%)  WIN {aw:.1f}%  PLC {ap:.1f}%")
# Strategy B: Bet ET when fav, SARR otherwise
bw=0; bp=0; bn=0
for r in rows:
    if not (r["et"] and r["sa"]): continue
    use_sa = (r["sa_top1_odds"] is not None and r["sa_top1_odds"]<=3.0)
    pick_m = r["sa"] if use_sa else r["et"]
    bw+=pick_m["win"]; bp+=pick_m["plc"]; bn+=1
print(f"  B. SARR-if-fav, else ET: n={bn}  WIN {bw/bn*100:.1f}%  PLC {bp/bn*100:.1f}%")
# Strategy C: Always bet SARR top-1 if it's market fav (≤3); skip otherwise
cw=cp=cn=0
for r in rows:
    if r["sa"] and r["sa_top1_odds"] is not None and r["sa_top1_odds"]<=3.0:
        cw+=r["sa"]["win"]; cp+=r["sa"]["plc"]; cn+=1
print(f"  C. SARR top-1 only when ≤3.0 odds: n={cn} ({cn/n*100:.0f}%)  WIN {cw/cn*100:.1f}%  PLC {cp/cn*100:.1f}%")
# Strategy D: ET top-1 when in 3-6 odds range
dw=dp=dn=0
for r in rows:
    if r["et"] and r["et_top1_odds"] is not None and 3.0<r["et_top1_odds"]<=6.0:
        dw+=r["et"]["win"]; dp+=r["et"]["plc"]; dn+=1
print(f"  D. ET top-1 only when 3-6 odds: n={dn} ({dn/n*100:.0f}%)  WIN {dw/dn*100:.1f}%  PLC {dp/dn*100:.1f}%")
# Strategy E: agree AND fav
ew=ep=en=0
for r in rows:
    if r["agree"] and r["et_top1_odds"] is not None and r["et_top1_odds"]<=4.0:
        if r["et"]:
            ew+=r["et"]["win"]; ep+=r["et"]["plc"]; en+=1
print(f"  E. Agree AND ≤4.0 odds: n={en} ({en/n*100:.0f}%)  WIN {ew/en*100 if en else 0:.1f}%  PLC {ep/en*100 if en else 0:.1f}%")

# ───────────────────────────── ROI SIMULATION ─────────────────────────────
print("\n━━━ FLAT-STAKE ROI (1u win bet) ━━━")
def roi_win(rs, picker, oddser):
    spent=staked=ret=0
    n_b=0
    for r in rs:
        m=picker(r); o=oddser(r)
        if m is None or o is None: continue
        n_b+=1; staked+=1
        if m["win"]: ret+=o
    if not staked: return None
    return {"n":n_b,"staked":staked,"return":ret,"profit":ret-staked,"roi_pct":(ret-staked)/staked*100}

strategies=[
    ("ET top-1 (all)",       lambda r: r["et"], lambda r: r["et_top1_odds"]),
    ("SARR top-1 (all)",     lambda r: r["sa"], lambda r: r["sa_top1_odds"]),
    ("Market fav",           lambda r: r["mk"], lambda r: r["fav_odds"]),
    ("Agreement (both top1)",lambda r: r["et"] if r["agree"] else None, lambda r: r["et_top1_odds"] if r["agree"] else None),
    ("ET top1 odds 3-6",     lambda r: r["et"] if r["et_top1_odds"] and 3<r["et_top1_odds"]<=6 else None,
                              lambda r: r["et_top1_odds"] if r["et_top1_odds"] and 3<r["et_top1_odds"]<=6 else None),
    ("SARR top1 odds ≤3",    lambda r: r["sa"] if r["sa_top1_odds"] and r["sa_top1_odds"]<=3 else None,
                              lambda r: r["sa_top1_odds"] if r["sa_top1_odds"] and r["sa_top1_odds"]<=3 else None),
    ("ET top1 NOT longshot (≤12)", lambda r: r["et"] if r["et_top1_odds"] and r["et_top1_odds"]<=12 else None,
                              lambda r: r["et_top1_odds"] if r["et_top1_odds"] and r["et_top1_odds"]<=12 else None),
]
print(f"  {'Strategy':<32} {'n':>4} {'win%':>6} {'avg_o':>6} {'ROI%':>7} {'profit':>8}")
for name, pf, of in strategies:
    res=roi_win(rows, pf, of)
    if not res: continue
    avg_o = sum(of(r) for r in rows if of(r) is not None)/res["n"] if res["n"] else 0
    win_p = sum((pf(r)["win"] if pf(r) else 0) for r in rows if pf(r) is not None)/res["n"]*100 if res["n"] else 0
    print(f"  {name:<32} {res['n']:>4} {win_p:>5.1f}% {avg_o:>6.2f} {res['roi_pct']:>+6.1f}% {res['profit']:>+7.1f}u")
