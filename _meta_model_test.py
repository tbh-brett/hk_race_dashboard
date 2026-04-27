"""
_meta_model_test.py — Test meta-model strategies that re-rank using ET+SARR jointly.

Strategies:
  M1: average rank (ET_rank + SARR_rank), top-1 = lowest sum
  M2: weighted blend favouring SARR for places, ET for wins
  M3: ET top-1 with mandatory SARR top-3 inclusion (sanity)
  M4: longshot filter — drop ET top-1 if >12 odds, fall to next pick
  M5: combined top-3 = (ET_top1, SARR_top1, market_fav) — coverage bet
"""
import json
from pathlib import Path
from statistics import mean
from collections import defaultdict

BASE = Path(__file__).parent
REPORTS = BASE / "reports"

def _load(p):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except: return None
def _et(dc):
    for tag in ("v4.4","v3.4.8"):
        p=REPORTS/f"race_day_report_{dc}_{tag}.json"
        if p.exists(): return p
    return None
def _sf(v):
    try: return float(str(v).strip())
    except: return None
def _fp(r):
    try: return int(str(r.get("place")).strip())
    except: return None

dates=sorted(set(p.name[len("race_day_report_"):][:8] for p in REPORTS.glob("race_day_report_????????_SARR.json")) &
             set(p.name[len("results_"):][:8] for p in REPORTS.glob("results_????????.json")))

records=[]
for dc in dates:
    et=_load(_et(dc)) if _et(dc) else None
    sa=_load(REPORTS/f"race_day_report_{dc}_SARR.json")
    rs=_load(REPORTS/f"results_{dc}.json")
    if not (et and sa and rs): continue
    et_by={r["race_number"]:r for r in et["races"]}
    sa_by={r["race_number"]:r for r in sa["races"]}
    for rr in rs["races"]:
        rn=rr["race_number"]; er=et_by.get(rn); sr=sa_by.get(rn)
        if not er or not sr: continue
        finish={}; odds={}
        for r in rr["runners"]:
            hn=r.get("horse_no"); pos=_fp(r)
            if hn is None: continue
            if pos is not None: finish[hn]=pos
            wo=_sf(r.get("win_odds"))
            if wo is not None: odds[hn]=wo
        if not finish: continue
        # Build per-horse records
        et_picks={int(p["horse_no"]):p["rank"] for p in er.get("picks",[]) if p.get("horse_no") is not None}
        sa_picks={int(p["horse_no"]):p["rank"] for p in sr.get("picks",[]) if p.get("horse_no") is not None}
        common = set(et_picks) & set(sa_picks)
        # Field universe = horses appearing in either model + finishers
        all_h=set(et_picks)|set(sa_picks)|set(finish)
        records.append({
            "date":dc,"race":rn,"finish":finish,"odds":odds,
            "et_picks":et_picks,"sa_picks":sa_picks,"all_h":all_h,
        })

n=len(records)
print(f"=== Meta-Model Test on {n} races ===\n")

def eval_strategy(name, picker):
    """picker(rec) -> (top1_horse, top3_list, cost_per_race=1)"""
    wins=plcs=t3w=staked=ret=skipped=0
    valid=0
    for r in records:
        sel=picker(r)
        if not sel:
            skipped+=1; continue
        t1,t3 = sel
        if t1 is None: skipped+=1; continue
        valid+=1
        fp=r["finish"].get(t1)
        wins+=1 if fp==1 else 0
        plcs+=1 if (fp and fp<=3) else 0
        if any(r["finish"].get(h)==1 for h in t3): t3w+=1
        # ROI: 1u flat
        staked+=1
        if fp==1:
            o=r["odds"].get(t1)
            if o: ret+=o
    return {"name":name,"n":valid,"skipped":skipped,
            "win":wins/valid*100 if valid else 0,
            "plc":plcs/valid*100 if valid else 0,
            "t3w":t3w/valid*100 if valid else 0,
            "roi":(ret-staked)/staked*100 if staked else 0,
            "profit":ret-staked}

def m1_avg_rank(r):
    common = list(set(r["et_picks"]) & set(r["sa_picks"]))
    if not common: return None
    common.sort(key=lambda h: r["et_picks"][h]+r["sa_picks"][h])
    top3=common[:3] if len(common)>=3 else common+[h for h in r["et_picks"] if h not in common][:3-len(common)]
    return common[0], top3[:3]

def m4_longshot_filter(r):
    # ET ranking, but skip top-1 if odds>12, fall to next; if all top-3 are longshots, skip race
    et_sorted=sorted(r["et_picks"].items(), key=lambda kv: kv[1])
    for hn, _ in et_sorted:
        o=r["odds"].get(hn)
        if o is not None and o<=12:
            top3=[h for h,_ in et_sorted[:3]]
            return hn, top3
    return None  # skip

def m4b_longshot_skip(r):
    # Same as ET top-1 but skip races where top-1 odds >12
    et_sorted=sorted(r["et_picks"].items(), key=lambda kv: kv[1])
    if not et_sorted: return None
    t1=et_sorted[0][0]
    o=r["odds"].get(t1)
    if o is not None and o>12: return None
    top3=[h for h,_ in et_sorted[:3]]
    return t1, top3

def m1b_consensus_only(r):
    # Bet only when ET and SARR both rank a horse in their top-3 — pick the one with lowest sum
    et_top3={h for h,rk in r["et_picks"].items() if rk<=3}
    sa_top3={h for h,rk in r["sa_picks"].items() if rk<=3}
    common=et_top3 & sa_top3
    if not common: return None
    sel=sorted(common, key=lambda h: r["et_picks"][h]+r["sa_picks"][h])
    return sel[0], sel[:3]

def m5_unionish(r):
    # Top-3 = ET top1 + SARR top1 + ET top2 (or SARR top2 if duplicate)
    et_sorted=sorted(r["et_picks"].items(), key=lambda kv: kv[1])
    sa_sorted=sorted(r["sa_picks"].items(), key=lambda kv: kv[1])
    if not et_sorted or not sa_sorted: return None
    et1=et_sorted[0][0]; sa1=sa_sorted[0][0]
    out=[et1]
    if sa1!=et1: out.append(sa1)
    for hn,_ in et_sorted[1:]:
        if hn not in out: out.append(hn); break
    if len(out)<3:
        for hn,_ in sa_sorted[1:]:
            if hn not in out: out.append(hn); break
    return et1, out[:3]  # use ET top-1 as the win pick

def m_et_only(r):
    et_sorted=sorted(r["et_picks"].items(), key=lambda kv: kv[1])
    if not et_sorted: return None
    return et_sorted[0][0], [h for h,_ in et_sorted[:3]]

def m_sa_only(r):
    sa_sorted=sorted(r["sa_picks"].items(), key=lambda kv: kv[1])
    if not sa_sorted: return None
    return sa_sorted[0][0], [h for h,_ in sa_sorted[:3]]

def m_agree_only(r):
    et_sorted=sorted(r["et_picks"].items(), key=lambda kv: kv[1])
    sa_sorted=sorted(r["sa_picks"].items(), key=lambda kv: kv[1])
    if not et_sorted or not sa_sorted: return None
    if et_sorted[0][0]!=sa_sorted[0][0]: return None
    t1=et_sorted[0][0]
    top3=[h for h,_ in et_sorted[:3]]
    return t1, top3

def m_et_3to6(r):
    et_sorted=sorted(r["et_picks"].items(), key=lambda kv: kv[1])
    if not et_sorted: return None
    t1=et_sorted[0][0]; o=r["odds"].get(t1)
    if o is None or not (3<o<=6): return None
    return t1, [h for h,_ in et_sorted[:3]]

def m_combined_filtered(r):
    """ET top-1 if 3-6 odds, else agreement, else skip"""
    et_sorted=sorted(r["et_picks"].items(), key=lambda kv: kv[1])
    sa_sorted=sorted(r["sa_picks"].items(), key=lambda kv: kv[1])
    if not et_sorted or not sa_sorted: return None
    et1=et_sorted[0][0]; o=r["odds"].get(et1)
    if o is not None and 3<o<=6:
        return et1, [h for h,_ in et_sorted[:3]]
    if et1==sa_sorted[0][0]:
        return et1, [h for h,_ in et_sorted[:3]]
    return None

strategies=[
    ("ET only", m_et_only),
    ("SARR only", m_sa_only),
    ("Agreement only", m_agree_only),
    ("ET odds 3-6 only", m_et_3to6),
    ("M1: avg rank (intersection)", m1_avg_rank),
    ("M1b: consensus top-3 only", m1b_consensus_only),
    ("M4: ET skip longshot top1", m4_longshot_filter),
    ("M4b: skip race if ET top1>12", m4b_longshot_skip),
    ("M5: ET top1, union top-3", m5_unionish),
    ("Combined (3-6 OR agree)", m_combined_filtered),
]
print(f"  {'Strategy':<32} {'bet':>4} {'skip':>4} {'win%':>6} {'plc%':>6} {'t3w%':>6} {'ROI%':>7} {'profit':>8}")
for name,pf in strategies:
    s=eval_strategy(name,pf)
    print(f"  {s['name']:<32} {s['n']:>4} {s['skipped']:>4} {s['win']:>5.1f}% {s['plc']:>5.1f}% {s['t3w']:>5.1f}% {s['roi']:>+6.1f}% {s['profit']:>+7.1f}u")
