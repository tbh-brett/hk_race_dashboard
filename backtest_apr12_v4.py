"""
backtest_apr12_v4.py — Backtest the v4.4 model (3-lb weight bands, sectional decomp)
against April 12 actual results, then perform hierarchical factor importance analysis
and detailed failure diagnosis.

Outputs:
  - reports/backtest_20260412_v4.json   (per-race metrics for dashboard)
  - Console: full factor analysis + failure breakdown
"""

import sys, os, warnings, re, math, json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from scipy import stats as sp_stats
from collections import defaultdict

warnings.filterwarnings("ignore")

# ── Paths ──
BASE = Path(r"c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards")
REPORTS = BASE / "reports"

MEETING = {
    "date": "2026-04-12", "venue": "ST", "going": "Good",
    "predictions": REPORTS / "race_day_report_20260412_v4.4.json",
    "results": REPORTS / "results_20260412.json",
}

# ── Running style helpers (inline from model, to avoid slow module import) ──
def _parse_first_pos(rp_str):
    """Extract first running position number from position string."""
    if not rp_str:
        return None
    m = re.match(r"(\d+)", str(rp_str).strip())
    return int(m.group(1)) if m else None

def _classify_style(first_pos, field_size):
    """Classify running style — MUST match model's _classify_running_style labels.
    Leader ≤2, On-Pace ≤max(4, field×0.3), Closer ≥max(8, field×0.7), else Midfield."""
    if first_pos is None or field_size <= 0:
        return "Unknown"
    if first_pos <= 2:
        return "Leader"
    on_pace_cutoff = max(4, int(field_size * 0.3))
    if first_pos <= on_pace_cutoff:
        return "On-Pace"
    closer_cutoff = max(8, int(field_size * 0.7))
    if first_pos >= closer_cutoff:
        return "Closer"
    return "Midfield"

# ── Load predictions ──
print("Loading prediction and result files ...")
with open(MEETING["predictions"], "r", encoding="utf-8") as f:
    pred_data = json.load(f)

# ── Load actual results ──
with open(MEETING["results"], "r", encoding="utf-8") as f:
    actual_data = json.load(f)

# ── Build actuals lookup ──
actuals_by_race = {}
for race in actual_data.get("races", []):
    rnum = race["race_number"]
    actuals_by_race[rnum] = race

# ── Build predictions lookup (from model JSON) ──
preds_by_race = {}
for race in pred_data.get("races", []):
    rnum = race["race_number"]
    preds_by_race[rnum] = race

# ══════════════════════════════════════════════════════════════════════════════
# MATCH PREDICTIONS TO RESULTS
# ══════════════════════════════════════════════════════════════════════════════

all_rows = []
race_summaries = []

for rnum in sorted(actuals_by_race.keys()):
    actual_race = actuals_by_race[rnum]
    pred_race = preds_by_race.get(rnum)
    if not pred_race:
        continue

    dist = actual_race.get("distance", pred_race.get("distance", 0))
    going = actual_race.get("going", "G")
    course = actual_race.get("race_course", pred_race.get("race_course", ""))
    race_class = actual_race.get("race_class", pred_race.get("race_class", 0))
    try:
        race_class = int(race_class) if race_class else 0
    except (ValueError, TypeError):
        race_class = 0
    is_awt = actual_race.get("is_awt", False)

    # Build actual results lookup
    actual_runners = {}
    for r in actual_race.get("runners", []):
        hn = r.get("horse_name", "").upper().strip()
        actual_runners[hn] = r

    # Build prediction lookup
    pred_picks = pred_race.get("picks", [])
    if not pred_picks:
        continue

    # Actual placements
    place_map = {}
    for hn, r in actual_runners.items():
        p = r.get("place", "99")
        try:
            place_map[hn] = int(re.match(r"(\d+)", str(p)).group(1)) if re.match(r"(\d+)", str(p)) else 99
        except (ValueError, AttributeError):
            place_map[hn] = 99

    # Sort actuals by place
    actual_sorted = sorted(place_map.items(), key=lambda x: x[1])
    winner = actual_sorted[0][0] if actual_sorted else "?"
    top3_actual = set(hn for hn, p in actual_sorted[:3])

    # Match each predicted horse
    for pick in pred_picks:
        hn = pick.get("horse_name", "").upper().strip()
        actual = actual_runners.get(hn, {})
        actual_place = place_map.get(hn, 99)
        actual_ft = actual.get("finish_time_seconds")
        actual_odds = actual.get("win_odds")
        try:
            actual_odds = float(actual_odds)
        except (ValueError, TypeError):
            actual_odds = None

        # Running positions to detect actual style
        rp_str = actual.get("running_position", "")
        fp = _parse_first_pos(rp_str)
        fs = len(actual_runners)
        actual_style = _classify_style(fp, fs) if fp else "Unknown"

        row = {
            "race": rnum,
            "distance": dist,
            "class": race_class,
            "course": course,
            "horse": hn,
            # Predictions
            "pred_rank": pick.get("rank", 99),
            "pred_time": pick.get("projected_time"),
            "pred_win_pct": pick.get("win_prob", 0),
            "pred_style": pick.get("style", "Unknown"),
            "pred_esz": pick.get("early_speed_z", 0),
            "pred_eff_resid": pick.get("effective_resid"),
            "pred_draw_off": pick.get("draw"),  # draw number
            "pred_flags": pick.get("flags", []),
            "pred_sf": pick.get("smap_total_adj", 0),
            "pred_avg_late": pick.get("avg_late_dev", 0),
            "pred_ssi": pick.get("avg_ssi", 0),
            "pred_sec_adj": pick.get("sec_total_adj", 0),
            "pred_sec_type": pick.get("sec_type", ""),
            "pred_smap_adj": pick.get("smap_total_adj", 0),
            "pred_smap_pps": pick.get("smap_pps_adj", 0),
            "pred_weight": pick.get("weight"),
            "pred_jockey": pick.get("jockey", ""),
            "pred_draw": pick.get("draw"),
            "pred_vet": pick.get("vet_flag", ""),
            # Actuals
            "actual_place": actual_place,
            "actual_ft": actual_ft,
            "actual_odds": actual_odds,
            "actual_style": actual_style,
            "won": actual_place == 1,
            "placed": actual_place <= 3,
        }
        all_rows.append(row)

    # Race summary
    top3_pred = set(p["horse_name"].upper().strip() for p in pred_picks[:3])
    winner_rank = next((p["rank"] for p in pred_picks
                        if p["horse_name"].upper().strip() == winner), None)
    overlap = len(top3_pred & top3_actual)
    race_summaries.append({
        "race": rnum, "distance": dist, "class": race_class,
        "pace": pred_race.get("pace", "?"),
        "winner": winner, "winner_pred_rank": winner_rank,
        "top3_overlap": overlap,
        "n_runners": len(actual_runners),
        "n_predicted": len(pred_picks),
    })

df = pd.DataFrame(all_rows)
valid = df[df["pred_time"].notna() & df["actual_ft"].notna()].copy()
valid["time_error"] = valid["actual_ft"] - valid["pred_time"]
valid["abs_error"] = valid["time_error"].abs()

print(f"\nMatched: {len(valid)} horse-level predictions with actual results")
print(f"Races: {len(race_summaries)}")

# ══════════════════════════════════════════════════════════════════════════════
# METRIC 1: OVERALL ACCURACY
# ══════════════════════════════════════════════════════════════════════════════
def _sep(title):
    print(f"\n{'═'*80}")
    print(f"  {title}")
    print(f"{'═'*80}")

_sep("METRIC 1: OVERALL MODEL ACCURACY")

mae = valid["abs_error"].mean()
median_ae = valid["abs_error"].median()
bias = valid["time_error"].mean()
print(f"  MAE:          {mae:.3f}s")
print(f"  Median AE:    {median_ae:.3f}s")
print(f"  Bias:         {bias:+.3f}s  ({'predicted too fast' if bias > 0 else 'predicted too slow'})")

# Per-race ranking accuracy
print(f"\n  {'Race':>4s}  {'Dist':>5s}  {'Cl':>3s}  {'Winner':>20s}  {'Win Rk':>6s}  {'Top3∩':>5s}  {'Spρ':>5s}")
spearman_rhos = []
for rs in race_summaries:
    rdf = valid[valid["race"] == rs["race"]].sort_values("pred_rank")
    if len(rdf) >= 3:
        rho, _ = sp_stats.spearmanr(rdf["pred_rank"], rdf["actual_place"])
    else:
        rho = float("nan")
    spearman_rhos.append(rho)
    wr = rs["winner_pred_rank"] if rs["winner_pred_rank"] else "N/P"
    cl = f"C{rs['class']}" if rs["class"] and rs["class"] > 0 else "Grp"
    # Truncate winner name for display
    wn = rs["winner"][:20]
    print(f"  R{rs['race']:>2d}  {rs['distance']:>5d}  {cl:>3s}  {wn:>20s}  {str(wr):>6s}  {rs['top3_overlap']:>4d}/3  {rho:>5.2f}")

# Summary stats
winner_ranks = [rs["winner_pred_rank"] for rs in race_summaries if rs["winner_pred_rank"] is not None]
top1_hits = sum(1 for r in winner_ranks if r == 1)
top3_hits = sum(1 for r in winner_ranks if r <= 3)
top5_hits = sum(1 for r in winner_ranks if r <= 5)
valid_rhos = [r for r in spearman_rhos if not np.isnan(r)]

print(f"\n  Winner predicted rank: median={np.median(winner_ranks):.0f}  mean={np.mean(winner_ranks):.1f}")
print(f"  Top-1 hit rate: {top1_hits}/{len(winner_ranks)} ({100*top1_hits/len(winner_ranks):.0f}%)")
print(f"  Top-3 hit rate: {top3_hits}/{len(winner_ranks)} ({100*top3_hits/len(winner_ranks):.0f}%)")
print(f"  Top-5 hit rate: {top5_hits}/{len(winner_ranks)} ({100*top5_hits/len(winner_ranks):.0f}%)")
print(f"  Spearman ρ (rank accuracy): mean={np.mean(valid_rhos):.3f}  median={np.median(valid_rhos):.3f}")

top3_overlaps = [rs["top3_overlap"] for rs in race_summaries]
print(f"  Top-3 overlap: mean={np.mean(top3_overlaps):.1f}/3  "
      f"perfect={sum(1 for x in top3_overlaps if x==3)}/{len(top3_overlaps)}")

# ══════════════════════════════════════════════════════════════════════════════
# METRIC 2: HIERARCHICAL FACTOR IMPORTANCE
# ══════════════════════════════════════════════════════════════════════════════
_sep("METRIC 2: HIERARCHICAL FACTOR IMPORTANCE")

print("\n  Which model components best predict actual finishing order?\n")

# For each factor, compute correlation with actual place
factors = {
    "Effective Residual":   "pred_eff_resid",
    "Projected Time":       "pred_time",
    "Win%":                 "pred_win_pct",
    "Sectional Adj":        "pred_sec_adj",
    "Late Dev (avg)":       "pred_avg_late",
    "SSI":                  "pred_ssi",
    "Speed Map Adj":        "pred_smap_adj",
    "Early Speed Z":        "pred_esz",
}

print(f"  {'Factor':>25s}  {'ρ(place)':>8s}  {'p-val':>8s}  {'ρ(FT)':>8s}  {'Direction':>12s}")
print(f"  {'─'*25}  {'─'*8}  {'─'*8}  {'─'*8}  {'─'*12}")
factor_importances = []

for label, col in factors.items():
    vals = valid[[col, "actual_place", "actual_ft"]].dropna()
    if len(vals) < 10:
        continue
    rho_place, p_place = sp_stats.spearmanr(vals[col], vals["actual_place"])
    rho_ft, _ = sp_stats.spearmanr(vals[col], vals["actual_ft"])
    direction = "faster→better" if rho_ft > 0 else "slower→better" if rho_ft < 0 else "neutral"
    sig = "***" if p_place < 0.001 else "**" if p_place < 0.01 else "*" if p_place < 0.05 else ""
    print(f"  {label:>25s}  {rho_place:>+7.3f}{sig:1s}  {p_place:>8.4f}  {rho_ft:>+7.3f}  {direction:>12s}")
    factor_importances.append({"factor": label, "rho_place": rho_place, "p": p_place, "rho_ft": rho_ft})

# Within-race factor importance (partial correlations controlling for race)
print(f"\n  Within-race analysis (controlling for field strength):\n")
print(f"  {'Factor':>25s}  {'Avg ρ':>7s}  {'n_races':>7s}  {'% correct dir':>13s}")
print(f"  {'─'*25}  {'─'*7}  {'─'*7}  {'─'*13}")

for label, col in factors.items():
    race_rhos = []
    correct_dir = 0
    for rn in valid["race"].unique():
        rdf = valid[valid["race"] == rn][[col, "actual_place"]].dropna()
        if len(rdf) < 5:
            continue
        r, _ = sp_stats.spearmanr(rdf[col], rdf["actual_place"])
        if not np.isnan(r):
            race_rhos.append(r)
            # For most factors, positive rho means higher value → worse place (correct)
            if col in ["pred_time", "pred_eff_resid", "pred_sec_adj", "pred_avg_late", "pred_ssi", "pred_esz"]:
                correct_dir += 1 if r > 0 else 0
            elif col in ["pred_win_pct"]:
                correct_dir += 1 if r < 0 else 0
    if race_rhos:
        avg_rho = np.mean(race_rhos)
        pct_correct = 100 * correct_dir / len(race_rhos)
        print(f"  {label:>25s}  {avg_rho:>+6.3f}  {len(race_rhos):>7d}  {pct_correct:>12.0f}%")

# ══════════════════════════════════════════════════════════════════════════════
# METRIC 3: FAILURE ANALYSIS — WHY DID PICKS FAIL?
# ══════════════════════════════════════════════════════════════════════════════
_sep("METRIC 3: FAILURE ANALYSIS — WHY DID TOP PICKS FAIL?")

# Identify cases where top-3 predicted did NOT place top-3
for rs in race_summaries:
    rdf = valid[valid["race"] == rs["race"]].copy()
    if len(rdf) == 0:
        continue

    top3_pred = rdf.sort_values("pred_rank").head(3)
    top3_actual_df = rdf.sort_values("actual_place").head(3)

    # Failures: predicted top-3 but finished outside top-3
    failures = top3_pred[top3_pred["actual_place"] > 3]
    # Surprises: finished top-3 but predicted outside top-3
    surprises = top3_actual_df[top3_actual_df["pred_rank"] > 3]

    if len(failures) == 0 and len(surprises) == 0:
        continue

    cl = f"C{rs['class']}" if rs['class'] and rs['class'] > 0 else "Grp"
    print(f"\n  R{rs['race']}  {rs['distance']}m  {cl}  Winner: {rs['winner']}")

    if len(failures) > 0:
        print(f"    FAILED PICKS (predicted top-3, finished worse):")
        for _, f in failures.iterrows():
            flags = ", ".join(f["pred_flags"]) if f["pred_flags"] else "none"
            style_match = "✓" if f["pred_style"] == f["actual_style"] else f"✗ ({f['pred_style']}→{f['actual_style']})"
            time_err = f["time_error"]
            err_str = f"{time_err:+.2f}s" if pd.notna(time_err) else "N/A"
            odds_str = f"${f['actual_odds']:.1f}" if pd.notna(f["actual_odds"]) else "?"
            print(f"      {f['horse']:20s}  Rk#{int(f['pred_rank'])} → P{int(f['actual_place'])}  "
                  f"err={err_str}  odds={odds_str}  style={style_match}  flags=[{flags}]")
            # Diagnose WHY
            reasons = []
            if pd.notna(time_err) and time_err > 1.0:
                reasons.append(f"ran {time_err:.1f}s slower than projected")
            if f["pred_style"] != f["actual_style"]:
                reasons.append(f"style mismatch: predicted {f['pred_style']}, ran as {f['actual_style']}")
            if "↓DEC" in (f["pred_flags"] or []):
                reasons.append("declining trajectory flagged")
            if pd.notna(f["pred_sec_adj"]) and f["pred_sec_adj"] > 0.03:
                reasons.append(f"weak sectional profile (adj={f['pred_sec_adj']:+.03f}s)")
            if pd.notna(f["actual_odds"]) and f["actual_odds"] > 15:
                reasons.append(f"market didn't fancy (${f['actual_odds']:.0f})")
            elif pd.notna(f["actual_odds"]) and f["actual_odds"] < 3:
                reasons.append(f"heavy favourite that underperformed")
            if reasons:
                print(f"        → Diagnosis: {'; '.join(reasons)}")

    if len(surprises) > 0:
        print(f"    SURPRISE PLACERS (finished top-3, predicted lower):")
        for _, s in surprises.iterrows():
            odds_str = f"${s['actual_odds']:.1f}" if pd.notna(s["actual_odds"]) else "?"
            time_err = s["time_error"]
            err_str = f"{time_err:+.2f}s" if pd.notna(time_err) else "N/A"
            print(f"      {s['horse']:20s}  Rk#{int(s['pred_rank'])} → P{int(s['actual_place'])}  "
                  f"err={err_str}  odds={odds_str}")
            reasons = []
            if pd.notna(time_err) and time_err < -0.5:
                reasons.append(f"ran {abs(time_err):.1f}s faster than projected")
            if pd.notna(s["actual_odds"]) and s["actual_odds"] < 5:
                reasons.append(f"market favourite at ${s['actual_odds']:.1f}")
            if s["pred_rank"] > 8:
                reasons.append(f"model had no signal (ranked #{int(s['pred_rank'])})")
            if reasons:
                print(f"        → Diagnosis: {'; '.join(reasons)}")

# ══════════════════════════════════════════════════════════════════════════════
# METRIC 4: SYSTEMATIC PATTERNS
# ══════════════════════════════════════════════════════════════════════════════
_sep("METRIC 4: SYSTEMATIC FAILURE PATTERNS")

# By class
print("\n  By Class:")
print(f"  {'Class':>6s}  {'n':>4s}  {'MAE':>6s}  {'Top3 rate':>9s}  {'Win rate':>8s}  {'Mean ρ':>7s}")
for cl in sorted(valid["class"].unique()):
    grp = valid[valid["class"] == cl]
    cl_label = f"C{cl}" if cl > 0 else "Grp"
    mae_cl = grp["abs_error"].mean()
    t3 = grp["placed"].mean() * 100
    w = grp["won"].mean() * 100
    rhos = []
    for rn in grp["race"].unique():
        rd = grp[grp["race"] == rn][["pred_rank", "actual_place"]].dropna()
        if len(rd) >= 3:
            r, _ = sp_stats.spearmanr(rd["pred_rank"], rd["actual_place"])
            rhos.append(r)
    avg_rho = np.mean(rhos) if rhos else float("nan")
    print(f"  {cl_label:>6s}  {len(grp):>4d}  {mae_cl:>5.2f}s  {t3:>8.1f}%  {w:>7.1f}%  {avg_rho:>+6.3f}")

# By distance
print("\n  By Distance:")
print(f"  {'Dist':>6s}  {'n':>4s}  {'MAE':>6s}  {'Bias':>7s}")
for d in sorted(valid["distance"].unique()):
    grp = valid[valid["distance"] == d]
    print(f"  {d:>6d}  {len(grp):>4d}  {grp['abs_error'].mean():>5.2f}s  {grp['time_error'].mean():>+6.2f}s")

# Style accuracy
print("\n  Running Style Prediction Accuracy:")
pred_styles = valid["pred_style"].value_counts()
for style in pred_styles.index:
    grp = valid[valid["pred_style"] == style]
    match = (grp["pred_style"] == grp["actual_style"]).mean() * 100
    print(f"    {style:>12s}: {match:.0f}% exact match  (n={len(grp)})")

# Draw effect: did outer draws underperform?
print("\n  Draw Position Effect (today):")
valid["draw_zone"] = pd.cut(valid["pred_draw"],
                            bins=[0, 4, 9, 14],
                            labels=["Inner (1-4)", "Mid (5-9)", "Outer (10-14)"])
for zone, grp in valid.groupby("draw_zone", observed=True):
    avg_place = grp["actual_place"].mean()
    print(f"    {zone}: avg place={avg_place:.1f}  (n={len(grp)})")

# Vet flags
print("\n  Vet Flag Impact:")
for vf in ["", "INFO", "AMBER", "RED"]:
    grp = valid[valid["pred_vet"] == vf]
    if len(grp) >= 3:
        label = vf if vf else "None"
        avg_place = grp["actual_place"].mean()
        t3_rate = grp["placed"].mean() * 100
        print(f"    {label:>6s}: avg place={avg_place:.1f}  top-3 rate={t3_rate:.0f}%  (n={len(grp)})")

# ══════════════════════════════════════════════════════════════════════════════
# METRIC 5: ODDS COMPARISON (Expected Value)
# ══════════════════════════════════════════════════════════════════════════════
_sep("METRIC 5: MODEL VS MARKET")

odds_valid = valid[valid["actual_odds"].notna() & (valid["actual_odds"] > 0)].copy()
if len(odds_valid) > 0:
    # Model top-3 picks: what were their market odds?
    top3_model = odds_valid[odds_valid["pred_rank"] <= 3]
    print(f"\n  Model top-3 picks performance:")
    print(f"    n={len(top3_model)}: placed top-3 in {top3_model['placed'].sum()}/{len(top3_model)} "
          f"({100*top3_model['placed'].mean():.0f}%)")
    print(f"    Average odds of top-3 picks: ${top3_model['actual_odds'].mean():.1f}")
    print(f"    Average odds of winners in top-3: ", end="")
    t3w = top3_model[top3_model["won"]]
    if len(t3w):
        print(f"${t3w['actual_odds'].mean():.1f}  (n={len(t3w)})")
    else:
        print("no winners")

    # Flat bet ROI simulation
    print(f"\n  Flat $10 win bet on model top-1 pick each race:")
    top1 = odds_valid.groupby("race").first()  # already sorted by pred_rank
    stake = len(top1) * 10
    returns = sum(10 * r["actual_odds"] for _, r in top1.iterrows() if r["won"])
    print(f"    Staked: ${stake}  Returns: ${returns:.0f}  ROI: {100*(returns-stake)/stake:+.0f}%")

    print(f"\n  Flat $10 win bet on model top-3 picks:")
    top3_all = odds_valid[odds_valid["pred_rank"] <= 3]
    stake3 = len(top3_all) * 10
    returns3 = sum(10 * r["actual_odds"] for _, r in top3_all.iterrows() if r["won"])
    print(f"    Staked: ${stake3}  Returns: ${returns3:.0f}  ROI: {100*(returns3-stake3)/stake3:+.0f}%")

# ══════════════════════════════════════════════════════════════════════════════
# METRIC 6: EXCEPTIONAL PERFORMERS (Blackbook Candidates)
# ══════════════════════════════════════════════════════════════════════════════
_sep("METRIC 6: EXCEPTIONAL PERFORMERS — BLACKBOOK CANDIDATES")

exceptional = []
for rnum in sorted(actuals_by_race.keys()):
    actual_race = actuals_by_race[rnum]
    pred_race = preds_by_race.get(rnum)
    if not pred_race:
        continue

    dist = actual_race.get("distance", pred_race.get("distance", 0))
    race_class = actual_race.get("race_class", pred_race.get("race_class", 0))
    try:
        race_class = int(race_class) if race_class else 0
    except (ValueError, TypeError):
        race_class = 0

    actual_runners = actual_race.get("runners", [])
    picks = pred_race.get("picks", [])

    # Compute race median finish time for context
    race_fts = [r.get("finish_time_seconds") for r in actual_runners
                if r.get("finish_time_seconds")]
    race_median_ft = float(np.median(race_fts)) if race_fts else None
    winner_ft = None
    for r in actual_runners:
        try:
            if int(re.match(r"(\d+)", str(r.get("place", "99"))).group(1)) == 1:
                winner_ft = r.get("finish_time_seconds")
                break
        except (ValueError, AttributeError):
            pass

    for r in actual_runners:
        try:
            place = int(re.match(r"(\d+)", str(r.get("place", "99"))).group(1))
        except (ValueError, AttributeError):
            continue
        hn = r.get("horse_name", "?")
        ft = r.get("finish_time_seconds")
        odds = r.get("win_odds")
        try:
            odds = float(odds)
        except (TypeError, ValueError):
            odds = None
        rp = r.get("running_position", "")
        draw = r.get("draw", "?")

        # Context: find model prediction
        pred_pick = next((p for p in picks if p.get("horse_name", "").upper().strip() == hn.upper().strip()), None)
        pred_rank = pred_pick.get("rank") if pred_pick else None
        pred_time = pred_pick.get("projected_time") if pred_pick else None

        reasons = []
        score = 0.0

        # 1. Won or placed by a significant margin
        if place == 1 and winner_ft and race_median_ft:
            margin_vs_median = race_median_ft - winner_ft
            if margin_vs_median > 1.0:
                reasons.append(f"won by {margin_vs_median:.1f}s vs median — dominant")
                score += margin_vs_median

        # 2. Great late sectionals (came from behind and powered home)
        secs = r.get("sectiontimes", [])
        if secs and isinstance(secs, list):
            valid_secs = [float(s) for s in secs if s and str(s).strip()]
            if len(valid_secs) >= 2:
                last_sec = valid_secs[-1]
                first_sec = valid_secs[0]
                # Strong late speed: last section notably faster than early sections
                avg_other = sum(valid_secs[:-1]) / max(len(valid_secs) - 1, 1)
                if last_sec < avg_other - 0.5 and place <= 4:
                    reasons.append(f"exceptional late speed ({last_sec:.2f}s last section vs {avg_other:.2f}s avg early)")
                    score += (avg_other - last_sec)

        # 3. Ran much faster than model projected
        if pred_time and ft:
            beat_proj = pred_time - ft
            if beat_proj > 0.5 and place <= 4:
                reasons.append(f"ran {beat_proj:.2f}s faster than projected")
                score += beat_proj * 0.8

        # 4. Overcame bad draw (outer) and still placed
        if place <= 3 and draw and str(draw).isdigit() and int(draw) >= 10:
            reasons.append(f"placed from wide draw ({draw})")
            score += 0.5

        # 5. Won at big odds → hidden ability
        if place == 1 and odds and odds >= 10:
            reasons.append(f"won at ${odds:.1f} — market underrated")
            score += min(odds / 10, 2.0)

        # 6. Model ranked poorly but outperformed massively
        if pred_rank and pred_rank >= 8 and place <= 3:
            reasons.append(f"model Rk#{pred_rank} → P{place} — unanticipated improvement")
            score += 1.0

        # 7. Strong closing position (moved up significantly)
        fp = _parse_first_pos(rp)
        if fp and fp >= 8 and place <= 3:
            reasons.append(f"closed from position {fp} to finish P{place}")
            score += 0.5

        # 8. Sustained speed from front (led throughout)
        if fp and fp <= 2 and place <= 2:
            # Check if still near front at finish — all-the-way
            reasons.append(f"led/sat 2nd from start — all-the-way or gate-to-wire effort")
            score += 0.3

        if score >= 1.0 and reasons:
            cls_str = f"C{race_class}" if race_class > 0 else "Grp"
            exceptional.append({
                "race": rnum,
                "distance": dist,
                "class": cls_str,
                "horse": hn,
                "place": place,
                "ft": ft,
                "odds": odds,
                "draw": draw,
                "running_position": rp,
                "pred_rank": pred_rank,
                "score": round(score, 2),
                "reasons": reasons,
            })

exceptional.sort(key=lambda x: x["score"], reverse=True)

if exceptional:
    print(f"\n  Found {len(exceptional)} exceptional performer(s):\n")
    for e in exceptional:
        odds_str = f"${e['odds']:.1f}" if e["odds"] else "?"
        rk_str = f"Rk#{e['pred_rank']}" if e["pred_rank"] else "N/P"
        print(f"  R{e['race']} {e['distance']}m {e['class']} — "
              f"{e['horse']}  P{e['place']}  {odds_str}  Dr{e['draw']}  {rk_str}  "
              f"score={e['score']}")
        for r in e["reasons"]:
            print(f"    ★ {r}")
        print()
else:
    print("\n  No exceptional performers detected.")

# ══════════════════════════════════════════════════════════════════════════════
# SAVE BACKTEST JSON FOR DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
_sep("SAVING BACKTEST JSON")

backtest_out = {
    "date": MEETING["date"],
    "venue": MEETING["venue"],
    "model_version": "v4.4",
    "n_races": len(race_summaries),
    "n_predictions": len(valid),
    "metrics": {
        "mae": round(mae, 3),
        "median_ae": round(median_ae, 3),
        "bias": round(bias, 3),
        "mean_spearman_rho": round(np.mean(valid_rhos), 3) if valid_rhos else None,
        "top1_rate": round(top1_hits / len(winner_ranks), 3) if winner_ranks else 0,
        "top3_rate": round(top3_hits / len(winner_ranks), 3) if winner_ranks else 0,
        "top5_rate": round(top5_hits / len(winner_ranks), 3) if winner_ranks else 0,
        "mean_top3_overlap": round(np.mean(top3_overlaps), 2),
    },
    "factor_importance": factor_importances,
    "exceptional_performers": [
        {
            "race": e["race"], "distance": e["distance"], "class": e["class"],
            "horse": e["horse"], "place": e["place"], "odds": e["odds"],
            "draw": e["draw"], "pred_rank": e["pred_rank"],
            "score": e["score"], "reasons": e["reasons"],
        } for e in exceptional
    ],
    "races": [],
}

for rs in race_summaries:
    rdf = valid[valid["race"] == rs["race"]]
    rho = None
    if len(rdf) >= 3:
        r, _ = sp_stats.spearmanr(rdf["pred_rank"], rdf["actual_place"])
        rho = round(r, 3)

    race_bt = {
        "race_number": rs["race"],
        "distance": rs["distance"],
        "class": rs["class"],
        "winner": rs["winner"],
        "winner_pred_rank": rs["winner_pred_rank"],
        "top3_overlap": rs["top3_overlap"],
        "spearman_rho": rho,
        "mae": round(rdf["abs_error"].mean(), 3) if len(rdf) else None,
        "horses": [],
    }
    for _, h in rdf.iterrows():
        race_bt["horses"].append({
            "horse": h["horse"],
            "pred_rank": int(h["pred_rank"]),
            "actual_place": int(h["actual_place"]),
            "pred_time": h["pred_time"],
            "actual_ft": h["actual_ft"],
            "time_error": round(h["time_error"], 3) if pd.notna(h["time_error"]) else None,
            "actual_odds": h["actual_odds"],
            "pred_style": h["pred_style"],
            "actual_style": h["actual_style"],
        })
    backtest_out["races"].append(race_bt)

out_path = REPORTS / "backtest_20260412_v4.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(backtest_out, f, indent=2, ensure_ascii=False, default=str)
print(f"\n  Saved: {out_path}")
print("\nDone.")
