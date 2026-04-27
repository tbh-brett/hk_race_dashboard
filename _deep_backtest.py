"""
_deep_backtest.py — Deep-dive ET vs SARR vs Market backtest.

Adds to compare_et_vs_sarr.py:
  - Market favourite baseline (lowest win_odds)
  - Pace prediction accuracy (predicted vs actual_pace_label)
  - Failure-mode analysis: which horses each model misses
  - ET projected-time RMSE vs actual finish_time
  - Combined model (intersection / union) hit rates
  - Per-class breakdown (G1-G3, C1-C5, Griffin/4yo)
  - Win odds bucket analysis (favourites vs longshots)
"""
from __future__ import annotations
import json, math
from pathlib import Path
from statistics import mean, median, stdev
from collections import defaultdict, Counter

BASE = Path(__file__).parent
REPORTS = BASE / "reports"

def _load(p): 
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return None

def _safe_float(v):
    try: return float(str(v).strip())
    except (TypeError, ValueError): return None

def _finish_pos(r):
    try: return int(str(r.get("place")).strip())
    except (TypeError, ValueError): return None

def _et_path(dc):
    for tag in ("v4.4", "v3.4.8"):
        p = REPORTS / f"race_day_report_{dc}_{tag}.json"
        if p.exists(): return p
    return None

def _gather():
    et = set()
    for tag in ("v4.4","v3.4.8"):
        for p in REPORTS.glob(f"race_day_report_????????_{tag}.json"):
            et.add(p.name[len("race_day_report_"):][:8])
    sarr = {p.name[len("race_day_report_"):][:8] for p in REPORTS.glob("race_day_report_????????_SARR.json")}
    res = {p.name[len("results_"):][:8] for p in REPORTS.glob("results_????????.json")}
    return sorted(et & sarr & res)

def main():
    dates = _gather()
    print(f"=== Deep Backtest — {len(dates)} meetings ===")
    print(f"Dates: {', '.join(dates)}\n")

    rows = []
    pace_stats = {"correct":0,"close":0,"wrong":0,"total":0,"by_label":defaultdict(lambda:{"n":0,"correct":0})}
    proj_errors = []  # (et_proj_winner - actual_winner_time)
    horse_picks = []  # per-horse pick records for distribution analysis

    for dc in dates:
        et = _load(_et_path(dc))
        sarr = _load(REPORTS / f"race_day_report_{dc}_SARR.json")
        res = _load(REPORTS / f"results_{dc}.json")
        if not (et and sarr and res): continue
        venue = et.get("meeting_venue","?")

        et_by = {r["race_number"]:r for r in et["races"]}
        sa_by = {r["race_number"]:r for r in sarr["races"]}

        for rr in res["races"]:
            rn = rr.get("race_number")
            er = et_by.get(rn); sr = sa_by.get(rn)
            if not er or not sr: continue
            
            # Build runner data
            finishers = {}     # horse_no -> finish_pos
            odds_by = {}       # horse_no -> win_odds
            time_by = {}       # horse_no -> finish_time_seconds
            for r in rr.get("runners",[]):
                hn = r.get("horse_no"); pos = _finish_pos(r)
                if hn is None: continue
                if pos is not None: finishers[hn] = pos
                wo = _safe_float(r.get("win_odds"))
                if wo is not None: odds_by[hn] = wo
                ft = _safe_float(r.get("finish_time_seconds"))
                if ft is not None: time_by[hn] = ft
            if not finishers: continue
            field_size = len(finishers)
            
            # Picks
            et_picks = [int(p["horse_no"]) for p in er.get("picks",[])[:5] if p.get("horse_no") is not None]
            sa_picks = [int(p["horse_no"]) for p in sr.get("picks",[])[:5] if p.get("horse_no") is not None]
            # Market favourite (rank by ascending odds; ties broken by position 1 for top-1 selection)
            mkt_rank = sorted(odds_by.items(), key=lambda kv: kv[1])
            mkt_picks = [hn for hn,_ in mkt_rank[:5]]

            actual_winner = next((hn for hn,p in finishers.items() if p==1), None)
            actual_top3 = sorted([hn for hn,p in finishers.items() if p<=3], key=lambda h: finishers[h])

            def metrics(picks):
                if not picks: return None
                t1=picks[0]; top3=picks[:3]
                t1p = finishers.get(t1)
                t3p = [finishers.get(h) for h in top3]
                valid=[p for p in t3p if p is not None]
                return {
                    "win": 1 if t1p==1 else 0,
                    "plc": 1 if (t1p and t1p<=3) else 0,
                    "top3_has_w": 1 if any(p==1 for p in valid) else 0,
                    "trifecta": 1 if sorted(valid)==[1,2,3] else 0,
                    "in_top4": sum(1 for p in valid if p<=4),
                }

            et_m = metrics(et_picks); sa_m = metrics(sa_picks); mk_m = metrics(mkt_picks)

            # Combined: BOTH agree on top-1 (intersection); UNION top-3 contains winner
            combined_t1 = et_picks[0] if (et_picks and sa_picks and et_picks[0]==sa_picks[0]) else None
            comb_t1_win = 1 if combined_t1 and finishers.get(combined_t1)==1 else 0
            comb_t1_plc = 1 if combined_t1 and finishers.get(combined_t1) and finishers[combined_t1]<=3 else 0
            
            # Union top-3: any of either model's top-3 picks finishing 1st
            union_t3 = list(dict.fromkeys((et_picks[:3]+sa_picks[:3])))
            union_has_winner = 1 if actual_winner in union_t3 else 0
            
            # Pace accuracy (only if ET predicted)
            pp = er.get("pace"); ap = rr.get("actual_pace_label")
            if pp and ap:
                pace_stats["total"]+=1
                pace_stats["by_label"][pp]["n"]+=1
                if pp==ap:
                    pace_stats["correct"]+=1; pace_stats["by_label"][pp]["correct"]+=1
                # close = both fast/slow side
                fast={"Fast","Slightly Fast"}; slow={"Slow","Slightly Slow"}; norm={"Normal"}
                pside=lambda x: "F" if x in fast else ("S" if x in slow else "N")
                if pside(pp)==pside(ap): pace_stats["close"]+=1
                else: pace_stats["wrong"]+=1
            
            # ET projection RMSE (winner only — easiest validation)
            if actual_winner is not None and actual_winner in time_by:
                et_pick_proj = next((p.get("projected_time") for p in er.get("picks",[]) if p.get("horse_no")==actual_winner), None)
                if et_pick_proj is not None and time_by[actual_winner] is not None:
                    proj_errors.append({
                        "date":dc,"race":rn,"horse":actual_winner,
                        "proj":et_pick_proj,"actual":time_by[actual_winner],
                        "err":et_pick_proj-time_by[actual_winner]
                    })
            
            # Fav odds (info)
            fav_odds = mkt_rank[0][1] if mkt_rank else None
            top1_odds_et = odds_by.get(et_picks[0]) if et_picks else None
            top1_odds_sa = odds_by.get(sa_picks[0]) if sa_picks else None

            row = {
                "date":dc,"venue":venue,"race":rn,
                "distance":er.get("distance"),
                "race_class":er.get("race_class"),
                "field_size":field_size,
                "pace_predicted":pp,"pace_actual":ap,
                "winner":actual_winner,"actual_top3":actual_top3,
                "fav_odds":fav_odds,
                "et_top1":et_picks[0] if et_picks else None,
                "et_top1_odds":top1_odds_et,
                "sa_top1":sa_picks[0] if sa_picks else None,
                "sa_top1_odds":top1_odds_sa,
                "mkt_top1":mkt_picks[0] if mkt_picks else None,
                "agree":1 if (et_picks and sa_picks and et_picks[0]==sa_picks[0]) else 0,
                "et":et_m,"sa":sa_m,"mk":mk_m,
                "combined_top1":combined_t1,"comb_t1_win":comb_t1_win,"comb_t1_plc":comb_t1_plc,
                "union_t3_has_winner":union_has_winner,
            }
            rows.append(row)

    # ───── AGGREGATE ─────
    def avg(rs, key, sub):
        vs=[r[key][sub] for r in rs if r[key]]
        return mean(vs) if vs else None
    
    n = len(rows)
    print(f"━━━ OVERALL ({n} races) ━━━")
    print(f"  Top-1 agreement: {sum(r['agree'] for r in rows)/n*100:.1f}%")
    print()
    print(f"{'Metric':<22} {'ET':>8} {'SARR':>8} {'Market':>8} {'Combined':>10}")
    for k,name in [("win","Top-1 wins"),("plc","Top-1 places"),
                   ("top3_has_w","Top-3 has winner"),("trifecta","Top-3 = trifecta")]:
        e=avg(rows,"et",k); s=avg(rows,"sa",k); m=avg(rows,"mk",k)
        cv=mean([r["comb_t1_win"] for r in rows if r["combined_top1"]]) if k=="win" else (
           mean([r["comb_t1_plc"] for r in rows if r["combined_top1"]]) if k=="plc" else (
           mean([r["union_t3_has_winner"] for r in rows]) if k=="top3_has_w" else None))
        f = lambda v: f"{v*100:>6.1f}%" if v is not None else "    —  "
        print(f"  {name:<20} {f(e)} {f(s)} {f(m)}  {f(cv):>10}")
    print()
    n_combined = sum(1 for r in rows if r["combined_top1"])
    print(f"  Combined top-1 (when models agree): n={n_combined} / {n} ({n_combined/n*100:.0f}%)")
    print(f"  Union top-3 covers winner: {sum(r['union_t3_has_winner'] for r in rows)}/{n} ({sum(r['union_t3_has_winner'] for r in rows)/n*100:.1f}%)")
    
    # ───── PACE ACCURACY ─────
    print(f"\n━━━ PACE PREDICTION ({pace_stats['total']} races) ━━━")
    if pace_stats["total"]:
        print(f"  Exact match: {pace_stats['correct']}/{pace_stats['total']} ({pace_stats['correct']/pace_stats['total']*100:.1f}%)")
        print(f"  Same-side (F/N/S): {pace_stats['close']}/{pace_stats['total']} ({pace_stats['close']/pace_stats['total']*100:.1f}%)")
        print(f"  By predicted label:")
        for lbl, st in sorted(pace_stats["by_label"].items()):
            if st["n"]:
                print(f"    {lbl:<16} n={st['n']:>3}  exact={st['correct']/st['n']*100:>5.1f}%")
    
    # ───── PROJECTION ERROR (ET) ─────
    print(f"\n━━━ ET PROJECTION RMSE (winners only) ━━━")
    if proj_errors:
        errs = [p["err"] for p in proj_errors]
        rmse = math.sqrt(mean([e*e for e in errs]))
        bias = mean(errs)
        print(f"  n={len(errs)}  bias={bias:+.2f}s  RMSE={rmse:.2f}s  median_err={median(errs):+.2f}s")
        print(f"  |err|<0.5s: {sum(1 for e in errs if abs(e)<0.5)}/{len(errs)} ({sum(1 for e in errs if abs(e)<0.5)/len(errs)*100:.0f}%)")
        print(f"  |err|<1.0s: {sum(1 for e in errs if abs(e)<1.0)}/{len(errs)} ({sum(1 for e in errs if abs(e)<1.0)/len(errs)*100:.0f}%)")
        print(f"  |err|>2.0s: {sum(1 for e in errs if abs(e)>2.0)}/{len(errs)} ({sum(1 for e in errs if abs(e)>2.0)/len(errs)*100:.0f}%)")
        # worst 5
        worst = sorted(proj_errors, key=lambda p: -abs(p["err"]))[:5]
        print(f"  Worst 5: " + ", ".join(f"{p['date']} R{p['race']}: {p['err']:+.2f}s" for p in worst))
    
    # ───── ODDS BUCKET ANALYSIS ─────
    print(f"\n━━━ TOP-1 PICK BY ODDS BUCKET ━━━")
    def bucket(o):
        if o is None: return "?"
        if o<=3: return "1-fav (≤3.0)"
        if o<=6: return "2-medium (3-6)"
        if o<=12: return "3-mid-long (6-12)"
        return "4-longshot (>12)"
    for model_key, name in [("et","ET"),("sa","SARR"),("mk","Market")]:
        odds_key = "et_top1_odds" if model_key=="et" else ("sa_top1_odds" if model_key=="sa" else "fav_odds")
        b = defaultdict(lambda: {"n":0,"win":0,"plc":0})
        for r in rows:
            o = r[odds_key]; m = r[model_key]
            if m is None: continue
            bk = bucket(o)
            b[bk]["n"]+=1; b[bk]["win"]+=m["win"]; b[bk]["plc"]+=m["plc"]
        print(f"  {name}:")
        for bk in sorted(b.keys()):
            st=b[bk]
            if st["n"]:
                print(f"    {bk:<22} n={st['n']:>3}  win={st['win']/st['n']*100:>5.1f}%  plc={st['plc']/st['n']*100:>5.1f}%")
    
    # ───── DIVERGENCE: who's right when models disagree? ─────
    print(f"\n━━━ WHEN MODELS DISAGREE (n={sum(1 for r in rows if not r['agree'])}) ━━━")
    disag = [r for r in rows if not r["agree"]]
    et_w = sum(r["et"]["win"] for r in disag if r["et"])
    sa_w = sum(r["sa"]["win"] for r in disag if r["sa"])
    et_p = sum(r["et"]["plc"] for r in disag if r["et"])
    sa_p = sum(r["sa"]["plc"] for r in disag if r["sa"])
    n_d = len(disag)
    print(f"  ET top-1 wins:   {et_w}/{n_d} ({et_w/n_d*100:.1f}%)")
    print(f"  SARR top-1 wins: {sa_w}/{n_d} ({sa_w/n_d*100:.1f}%)")
    print(f"  ET top-1 plc:    {et_p}/{n_d} ({et_p/n_d*100:.1f}%)")
    print(f"  SARR top-1 plc:  {sa_p}/{n_d} ({sa_p/n_d*100:.1f}%)")
    
    # ───── WHEN MODELS AGREE ─────
    print(f"\n━━━ WHEN MODELS AGREE ━━━")
    agr = [r for r in rows if r["agree"]]
    if agr:
        n_a=len(agr)
        agr_w=sum(r["et"]["win"] for r in agr if r["et"])
        agr_p=sum(r["et"]["plc"] for r in agr if r["et"])
        print(f"  n={n_a}  win={agr_w/n_a*100:.1f}%  plc={agr_p/n_a*100:.1f}%")
        # What's odds of agreed pick?
        agr_odds=[r["et_top1_odds"] for r in agr if r["et_top1_odds"] is not None]
        if agr_odds:
            print(f"  Median odds of agreed pick: {median(agr_odds):.1f}")
    
    # ───── FIELD SIZE ─────
    print(f"\n━━━ BY FIELD SIZE ━━━")
    by_fs = defaultdict(list)
    for r in rows:
        bk = "small (≤10)" if r["field_size"]<=10 else ("medium (11-12)" if r["field_size"]<=12 else "large (13-14)")
        by_fs[bk].append(r)
    for bk in sorted(by_fs.keys()):
        rs=by_fs[bk]
        e_w=mean([r["et"]["win"] for r in rs if r["et"]])
        s_w=mean([r["sa"]["win"] for r in rs if r["sa"]])
        e_p=mean([r["et"]["plc"] for r in rs if r["et"]])
        s_p=mean([r["sa"]["plc"] for r in rs if r["sa"]])
        print(f"  {bk:<18} n={len(rs):>3}  ET win={e_w*100:>5.1f}% plc={e_p*100:>5.1f}%  SARR win={s_w*100:>5.1f}% plc={s_p*100:>5.1f}%")
    
    # ───── SAVE ─────
    out = BASE/"cache"/"deep_backtest.json"
    out.write_text(json.dumps({"rows":rows,"pace":dict(pace_stats),"proj_err":proj_errors},
                              default=lambda o:dict(o) if isinstance(o,defaultdict) else o,
                              indent=2,ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved → {out}")

if __name__=="__main__":
    main()
