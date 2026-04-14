"""
Backtest v4.1 model against actual results for 3 race meetings.
Meetings: April 1 (ST/AWT), April 6 (ST/Turf), April 8 (HV/Turf)
March 29 excluded — no racecard file available.

CRITICAL: Excludes target-date results from the historical DB before running
the model, so predictions are genuinely blind.
"""

import sys, os, warnings, re, math
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from scipy import stats as sp_stats

warnings.filterwarnings("ignore")

# ── Paths ──
BASE = Path(r"c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards")
TEMP = Path(os.environ.get("TEMP", str(BASE)))
DB_PATH = TEMP / "hkjc_results_updated.xlsx"
ET_PATH = TEMP / "expected_time_references_v3.xlsx"

# ── Meeting configs ──
MEETINGS = [
    {
        "date": "2026-04-01", "venue": "ST", "going": "Good",
        "card": BASE / "racecards" / "racecard_20260401.xlsx",
    },
    {
        "date": "2026-04-06", "venue": "ST", "going": "Good",
        "card": BASE / "racecards" / "racecard_20260406.xlsx",
    },
    {
        "date": "2026-04-08", "venue": "HV", "going": "Good",
        "card": BASE / "racecards" / "racecard_20260408.xlsx",
    },
]

# ── Import model functions ──
sys.path.insert(0, str(BASE))
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "model", str(BASE / "race_day_analysis_20260329_v3.4.8.py"))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

# ── Load full DB + references (once) ──
print("Loading references and historical DB ...")
db_full = pd.read_excel(DB_PATH)
print(f"  Full DB: {len(db_full)} rows")

class_fine = pd.read_excel(ET_PATH, sheet_name=0)
fine       = pd.read_excel(ET_PATH, sheet_name=1)
coarse     = pd.read_excel(ET_PATH, sheet_name=2)
ultra      = pd.read_excel(ET_PATH, sheet_name=3)

# ── Collect all results ──
all_race_results = []  # list of dicts per horse

for meeting in MEETINGS:
    mdate = meeting["date"]
    mdate_ts = pd.Timestamp(mdate)
    venue = meeting["venue"]
    going = meeting["going"]
    card_path = meeting["card"]

    print(f"\n{'='*80}")
    print(f"MEETING: {mdate}  Venue={venue}  Going={going}")
    print(f"{'='*80}")

    # ── 1. Filter DB: EXCLUDE this meeting's results + all future dates ──
    db = db_full[db_full["race_date"] < mdate_ts].copy()
    print(f"  DB after excluding {mdate} and later: {len(db)} rows "
          f"(removed {len(db_full) - len(db)})")

    # ── 2. Get actual results for this meeting (for comparison only) ──
    actuals = db_full[db_full["race_date"] == mdate_ts].copy()
    print(f"  Actual results for {mdate}: {len(actuals)} runners")

    # ── 3. Patch model globals for this meeting ──
    mod.MEETING_VENUE = venue
    mod.TURF_GOING_ASSUMED = going
    mod.AWT_GOING_ASSUMED = going

    # ── 4. Compute draw offsets from filtered DB ──
    mod.DB_FILE = DB_PATH  # needed by load_references_v3 internals
    # We need to compute draw offsets from filtered DB manually
    db_raw = db.copy()
    db_raw["draw_num"] = pd.to_numeric(db_raw["draw"], errors="coerce")
    db_raw = db_raw[db_raw["draw_num"].notna()
                    & db_raw["finish_time_seconds"].notna()
                    & (db_raw["finish_time_seconds"] > 0)].copy()
    is_awt = db_raw["track_type"].str.contains("All Weather", case=False, na=False)
    db_raw["draw_course"] = db_raw["race_course"]
    db_raw.loc[is_awt, "draw_course"] = "AWT"

    draw_rows = []
    for (dist, course), subset in db_raw.groupby(["distance", "draw_course"]):
        race_med = subset.groupby(["race_date", "race_number"])["finish_time_seconds"].median()
        subset = subset.merge(race_med.rename("race_median"),
                              on=["race_date", "race_number"])
        subset["resid"] = subset["finish_time_seconds"] - subset["race_median"]
        overall_mean = subset["resid"].mean()
        for dr_num, grp in subset.groupby("draw_num"):
            n = len(grp)
            if n >= 5:
                raw_off = grp["resid"].mean() - overall_mean
                draw_rows.append({
                    "distance": int(dist), "race_course": course,
                    "draw": int(dr_num), "mean_resid": round(grp["resid"].mean(), 6),
                    "n": n, "overall_mean": round(overall_mean, 6),
                    "draw_offset": round(raw_off, 6),
                })
    draw_off = pd.DataFrame(draw_rows) if draw_rows else pd.DataFrame()

    # ── 5. Clear caches (critical — stale from previous meeting) ──
    mod._RACE_QUALITY_CACHE.clear()
    mod._RACE_PACE_CACHE.clear()
    mod._FIELD_SIZE_CACHE.clear()
    mod._ET_CACHE.clear()
    if hasattr(mod, '_CLASS_BAND_OFFSETS'):
        mod._CLASS_BAND_OFFSETS.clear()

    # ── 6. Compute race quality scores on filtered DB ──
    mod.compute_race_quality_scores(db, class_fine, fine, coarse, ultra)

    # ── 7. Parse race card ──
    races = mod.parse_race_card_v2(card_path)
    print(f"  Parsed {len(races)} races from card")

    # ── 8. Project each race ──
    for race in races:
        rnum = race["race_number"]
        dist = race["distance"]
        rc = race["race_course"]
        rc_class = race["race_class"]
        is_awt = race["is_awt"]
        surface_label = "AWT" if is_awt else "Turf"

        df, pace_label, pace_score, pace_reasons, leader_names = \
            mod.project_race(race, class_fine, fine, coarse, ultra, draw_off, db=db)
        df = mod.enrich_with_risk(df, race)

        # ── Get actual results for this race ──
        race_actuals = actuals[actuals["race_number"] == rnum].copy()
        if len(race_actuals) == 0:
            print(f"  R{rnum}: NO ACTUAL RESULTS — skipping")
            continue

        # Compute actual race pace (median field residual)
        actual_field_resids = []
        for _, ar in race_actuals.iterrows():
            ft = ar.get("finish_time_seconds")
            d_raw = ar.get("distance", dist)
            if pd.isna(ft) or ft <= 0 or pd.isna(d_raw):
                continue
            d_i = int(d_raw)
            g_i = mod._map_going(ar.get("going", "G"))
            w_i = ar.get("actual_weight")
            wb_i = mod.weight_band(int(w_i)) if pd.notna(w_i) and w_i > 0 else "121-125"
            rc_i = ar.get("race_course", rc)
            tt_i = ar.get("track_type", "Turf")
            cb_i = mod.class_band(int(ar.get("race_class", 0))) if pd.notna(ar.get("race_class")) else "Group/Other"
            et_i, _, _, _ = mod.lookup_expected_time(
                class_fine, fine, coarse, ultra, d_i, g_i, wb_i, rc_i, tt_i, cb_i)
            if pd.notna(et_i) and et_i > 0:
                actual_field_resids.append(ft - et_i)

        actual_pace_dev = float(np.median(actual_field_resids)) if len(actual_field_resids) >= 3 else None

        # Classify actual pace
        def classify_pace(dev):
            if dev is None: return "Unknown"
            if dev >= 0.50: return "Very Slow"
            if dev >= 0.35: return "Slow"
            if dev >= 0.20: return "Slightly Slow"
            if dev > -0.20: return "Normal"
            if dev >= -0.40: return "Slightly Fast"
            if dev > -1.00: return "Fast"
            return "Very Fast"

        actual_pace_label = classify_pace(actual_pace_dev)

        # Compute actual running styles from results
        actual_styles = {}
        for _, ar in race_actuals.iterrows():
            hn = ar.get("horse_name")
            rp = str(ar.get("running_positions", ""))
            fs = len(race_actuals)
            fp = mod._parse_first_position(rp)
            if fp is not None and fs > 0:
                actual_styles[hn] = mod._classify_running_style(fp, fs)

        # ── Match predictions to actual results ──
        for _, pred in df.iterrows():
            hn = pred["horse_name"]
            actual_row = race_actuals[race_actuals["horse_name"] == hn]
            if len(actual_row) == 0:
                continue
            ar = actual_row.iloc[0]
            actual_place = mod._safe_place(ar.get("place", 99))
            actual_ft = ar.get("finish_time_seconds")
            actual_draw = ar.get("draw")
            actual_rp = str(ar.get("running_positions", ""))
            actual_fp = mod._parse_first_position(actual_rp)
            actual_style = actual_styles.get(hn, "Unknown")
            actual_odds = ar.get("win_odds")
            try:
                actual_odds = float(actual_odds)
            except (ValueError, TypeError):
                actual_odds = None

            pred_rank = int(pred.get("rank", 99)) if pd.notna(pred.get("rank")) else 99
            pred_time = pred.get("projected_time")
            pred_style = str(pred.get("dominant_style", "Unknown"))
            pred_esz = pred.get("early_speed_z", 0)
            pred_risk = pred.get("risk_score", 50)
            pred_risk_tier = str(pred.get("risk_tier", "?"))
            pred_win = pred.get("win_prob", 0)
            pred_eff = pred.get("effective_resid")
            pred_traj = str(pred.get("trajectory", ""))
            pred_draw_off = pred.get("draw_offset", 0)
            pred_unc = pred.get("uncertainty_penalty", 0)
            pred_surf_pen = pred.get("surface_penalty", 0)
            pred_dist_pen = pred.get("distance_penalty", 0)
            pred_class_pen = pred.get("class_trans_penalty", 0)
            pred_consistency = str(pred.get("consistency_flag", ""))
            pred_sf = pred.get("shrink_factor", 0)
            pred_n_runs = pred.get("n_runs", 0)
            pred_pace_mult = pred.get("pace_multiplier", 1.0)
            pred_leader_frac = pred.get("leader_frac", 0)
            pred_front_frac = pred.get("front_frac", 0)
            pred_ctx_adj = pred.get("context_adj", 0)
            pred_quality = pred.get("quality_effect", 1.0)

            all_race_results.append({
                "meeting": mdate,
                "venue": venue,
                "race": rnum,
                "distance": dist,
                "class": rc_class,
                "course": rc,
                "surface": surface_label,
                "horse": hn,
                # Prediction
                "pred_rank": pred_rank,
                "pred_time": pred_time,
                "pred_style": pred_style,
                "pred_esz": pred_esz,
                "pred_risk": pred_risk,
                "pred_risk_tier": pred_risk_tier,
                "pred_win%": pred_win,
                "pred_eff_resid": pred_eff,
                "pred_traj": pred_traj,
                "pred_draw_off": pred_draw_off,
                "pred_unc_pen": pred_unc,
                "pred_surf_pen": pred_surf_pen,
                "pred_dist_pen": pred_dist_pen,
                "pred_class_pen": pred_class_pen,
                "pred_consistency": pred_consistency,
                "pred_sf": pred_sf,
                "pred_n_runs": pred_n_runs,
                "pred_pace_mult": pred_pace_mult,
                "pred_leader_frac": pred_leader_frac,
                "pred_front_frac": pred_front_frac,
                "pred_ctx_adj": pred_ctx_adj,
                "pred_quality_eff": pred_quality,
                # Pace
                "pred_pace_label": pace_label,
                "pred_pace_score": pace_score,
                "actual_pace_dev": actual_pace_dev,
                "actual_pace_label": actual_pace_label,
                # Actual
                "actual_place": actual_place,
                "actual_ft": actual_ft,
                "actual_style": actual_style,
                "actual_odds": actual_odds,
            })

        # Quick summary
        top3_pred = df.head(3)["horse_name"].tolist()
        top3_actual = race_actuals.sort_values(
            "place", key=lambda s: s.apply(mod._safe_place)).head(3)["horse_name"].tolist()
        hit = len(set(top3_pred) & set(top3_actual))
        winner = top3_actual[0] if top3_actual else "?"
        winner_rank = "N/A"
        wr = df[df["horse_name"] == winner]
        if len(wr) > 0 and pd.notna(wr.iloc[0].get("rank")):
            winner_rank = int(wr.iloc[0]["rank"])
        cls_label = f"C{rc_class}" if rc_class and rc_class > 0 else "Grp"
        apd = f"{actual_pace_dev:+.2f}s" if actual_pace_dev is not None else "N/A"
        print(f"  R{rnum:>2d} {dist:>4d}m {surface_label:>4s} {cls_label:>4s}  "
              f"Pace: pred={pace_label:>15s} ({pace_score:+.2f}s)  actual={actual_pace_label:>15s} ({apd})  "
              f"Winner={winner} (pred_rank={winner_rank})  Top3 overlap={hit}/3")

print(f"\n\nTotal horse-level predictions: {len(all_race_results)}")

# ══════════════════════════════════════════════════════════════════════════════
# ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

results = pd.DataFrame(all_race_results)
# Only analyse horses with valid predictions
valid = results[results["pred_time"].notna()].copy()
print(f"Valid predictions: {len(valid)} of {len(results)}")

# Add derived columns
valid["time_error"] = valid["actual_ft"] - valid["pred_time"]  # positive = slower than predicted
valid["abs_time_error"] = valid["time_error"].abs()
valid["won"] = valid["actual_place"] == 1
valid["placed"] = valid["actual_place"] <= 3
valid["top5"] = valid["actual_place"] <= 5
valid["pred_top3"] = valid["pred_rank"] <= 3
valid["pred_top5"] = valid["pred_rank"] <= 5

# Race-level grouping
race_keys = valid.groupby(["meeting", "race"]).first().reset_index()[["meeting", "race"]].values

def _sep():
    print("\n" + "─"*100)

# ══════════════════════════════════════════════════════════════════════════════
# METRIC 1: RANKING ACCURACY
# ══════════════════════════════════════════════════════════════════════════════
_sep()
print("METRIC 1: MODEL RANKING ACCURACY")
_sep()

winner_ranks = []
spearman_rhos = []
top3_overlaps = []
top1_hits = 0
total_races = 0

for mdate, rnum in race_keys:
    rdf = valid[(valid["meeting"] == mdate) & (valid["race"] == rnum)].copy()
    if len(rdf) < 3:
        continue
    total_races += 1

    # Winner's predicted rank
    winner = rdf[rdf["won"]]
    if len(winner) > 0:
        wr = int(winner.iloc[0]["pred_rank"])
        winner_ranks.append(wr)
        if wr == 1:
            top1_hits += 1
    else:
        winner_ranks.append(99)

    # Top-3 overlap
    pred_t3 = set(rdf[rdf["pred_top3"]]["horse"].tolist())
    actual_t3 = set(rdf[rdf["placed"]]["horse"].tolist())
    overlap = len(pred_t3 & actual_t3)
    top3_overlaps.append(overlap)

    # Spearman rank correlation (pred_rank vs actual_place)
    rdf_corr = rdf[rdf["actual_place"] < 90].copy()  # exclude DNF
    if len(rdf_corr) >= 5:
        rho, pval = sp_stats.spearmanr(rdf_corr["pred_rank"], rdf_corr["actual_place"])
        spearman_rhos.append({"meeting": mdate, "race": rnum, "rho": rho, "p": pval, "n": len(rdf_corr)})

print(f"\nTotal races analysed: {total_races}")
print(f"Winner predicted #1: {top1_hits}/{total_races} ({100*top1_hits/total_races:.1f}%)")
wr_arr = np.array(winner_ranks)
for k in [1, 2, 3, 5]:
    cnt = (wr_arr <= k).sum()
    print(f"Winner in top-{k}: {cnt}/{total_races} ({100*cnt/total_races:.1f}%)")
print(f"Median winner rank: {np.median(wr_arr):.0f}")
print(f"Mean winner rank: {np.mean(wr_arr):.1f}")
print(f"\nTop-3 overlap (pred top3 ∩ actual top3):")
for k in [0, 1, 2, 3]:
    cnt = sum(1 for x in top3_overlaps if x == k)
    print(f"  {k}/3 overlap: {cnt} races ({100*cnt/total_races:.1f}%)")
print(f"  Mean overlap: {np.mean(top3_overlaps):.2f}/3")

if spearman_rhos:
    rho_df = pd.DataFrame(spearman_rhos)
    print(f"\nSpearman ρ (pred rank vs actual place): mean={rho_df['rho'].mean():.3f}, "
          f"median={rho_df['rho'].median():.3f}")
    sig = rho_df[rho_df["p"] < 0.05]
    print(f"  Significant (p<0.05): {len(sig)}/{len(rho_df)} races")

    # Best and worst
    best = rho_df.loc[rho_df["rho"].idxmax()]
    worst = rho_df.loc[rho_df["rho"].idxmin()]
    print(f"\n  BEST:  {best['meeting']} R{int(best['race'])}  ρ={best['rho']:.3f} (p={best['p']:.4f})")
    print(f"  WORST: {worst['meeting']} R{int(worst['race'])}  ρ={worst['rho']:.3f} (p={worst['p']:.4f})")

# Show specific examples
print("\n  GOOD EXAMPLES (winner ranked #1):")
for mdate, rnum in race_keys:
    rdf = valid[(valid["meeting"] == mdate) & (valid["race"] == rnum)]
    winner = rdf[rdf["won"]]
    if len(winner) > 0 and int(winner.iloc[0]["pred_rank"]) == 1:
        w = winner.iloc[0]
        print(f"    {mdate} R{rnum}: {w['horse']} won (pred=#1, time={w['pred_time']:.2f}s, actual={w['actual_ft']:.2f}s, odds={w['actual_odds']})")

print("\n  BAD EXAMPLES (winner ranked ≥8):")
for mdate, rnum in race_keys:
    rdf = valid[(valid["meeting"] == mdate) & (valid["race"] == rnum)]
    winner = rdf[rdf["won"]]
    if len(winner) > 0 and int(winner.iloc[0]["pred_rank"]) >= 8:
        w = winner.iloc[0]
        # Who was predicted #1?
        pred1 = rdf[rdf["pred_rank"] == 1]
        p1_name = pred1.iloc[0]["horse"] if len(pred1) > 0 else "?"
        p1_place = int(pred1.iloc[0]["actual_place"]) if len(pred1) > 0 else "?"
        print(f"    {mdate} R{rnum}: {w['horse']} won (pred=#{int(w['pred_rank'])}, odds={w['actual_odds']})  "
              f"Model #1 was {p1_name} (finished #{p1_place})")

# ══════════════════════════════════════════════════════════════════════════════
# METRIC 2: PACE PREDICTION ACCURACY
# ══════════════════════════════════════════════════════════════════════════════
_sep()
print("METRIC 2: PACE PREDICTION ACCURACY")
_sep()

pace_data = valid.groupby(["meeting", "race"]).first().reset_index()
pace_valid = pace_data[pace_data["actual_pace_dev"].notna()].copy()

pace_valid["pace_error"] = pace_valid["actual_pace_dev"] - pace_valid["pred_pace_score"]
pace_valid["pace_label_match"] = pace_valid["pred_pace_label"] == pace_valid["actual_pace_label"]

# Within 1 band?
PACE_BANDS = ["Very Slow", "Slow", "Slightly Slow", "Normal", "Slightly Fast", "Fast", "Very Fast"]
def band_dist(a, b):
    if a not in PACE_BANDS or b not in PACE_BANDS: return 99
    return abs(PACE_BANDS.index(a) - PACE_BANDS.index(b))

pace_valid["pace_band_dist"] = pace_valid.apply(
    lambda r: band_dist(r["pred_pace_label"], r["actual_pace_label"]), axis=1)

print(f"\nTotal races with pace data: {len(pace_valid)}")
print(f"Exact label match: {pace_valid['pace_label_match'].sum()}/{len(pace_valid)} "
      f"({100*pace_valid['pace_label_match'].mean():.1f}%)")
within1 = (pace_valid["pace_band_dist"] <= 1).sum()
print(f"Within 1 band: {within1}/{len(pace_valid)} ({100*within1/len(pace_valid):.1f}%)")
print(f"\nPace deviation error (actual - predicted):")
print(f"  Mean: {pace_valid['pace_error'].mean():+.3f}s")
print(f"  Median: {pace_valid['pace_error'].median():+.3f}s")
print(f"  Std: {pace_valid['pace_error'].std():.3f}s")
print(f"  MAE: {pace_valid['pace_error'].abs().mean():.3f}s")

bias = pace_valid["pace_error"].mean()
if bias < -0.15:
    print(f"  ⚠ Model OVER-predicts pace speed by {abs(bias):.2f}s (predicts too fast)")
elif bias > 0.15:
    print(f"  ⚠ Model UNDER-predicts pace speed by {bias:.2f}s (predicts too slow)")
else:
    print(f"  ✓ Pace prediction relatively unbiased")

print(f"\nPer-race pace breakdown:")
for _, row in pace_valid.sort_values(["meeting", "race"]).iterrows():
    match = "✓" if row["pace_label_match"] else "✗"
    print(f"  {match} {row['meeting']} R{int(row['race'])}  pred={row['pred_pace_label']:>15s} ({row['pred_pace_score']:+.2f}s)  "
          f"actual={row['actual_pace_label']:>15s} ({row['actual_pace_dev']:+.2f}s)  "
          f"error={row['pace_error']:+.2f}s")

# ══════════════════════════════════════════════════════════════════════════════
# METRIC 3: PACE BENEFICIARY ACCURACY
# ══════════════════════════════════════════════════════════════════════════════
_sep()
print("METRIC 3: PACE BENEFICIARY ACCURACY")
_sep()

# For each race, identify predicted pace beneficiaries (pace_mult > 1.0)
# and pace-disadvantaged (pace_mult < 1.0) — compare with actual results
print("\nLogic: horses with pace_multiplier > 1.0 should outperform, < 1.0 should underperform")
print("Comparing predicted pace multiplier vs actual finishing position\n")

beneficiary_data = []
for mdate, rnum in race_keys:
    rdf = valid[(valid["meeting"] == mdate) & (valid["race"] == rnum)].copy()
    if len(rdf) < 5:
        continue
    pace_label = rdf.iloc[0]["pred_pace_label"]
    actual_pace = rdf.iloc[0]["actual_pace_label"]

    for _, r in rdf.iterrows():
        mult = r.get("pred_pace_mult", 1.0) if pd.notna(r.get("pred_pace_mult")) else 1.0
        beneficiary_data.append({
            "meeting": mdate, "race": rnum, "horse": r["horse"],
            "pred_style": r["pred_style"], "actual_style": r["actual_style"],
            "pace_mult": mult,
            "pred_rank": r["pred_rank"], "actual_place": r["actual_place"],
            "pred_pace": pace_label, "actual_pace": actual_pace,
            "won": r["won"], "placed": r["placed"],
        })

bdf = pd.DataFrame(beneficiary_data)
if len(bdf) > 0:
    bdf["benefited"] = bdf["pace_mult"] > 1.05
    bdf["penalised"] = bdf["pace_mult"] < 0.95

    ben = bdf[bdf["benefited"]]
    pen = bdf[bdf["penalised"]]
    neutral = bdf[(~bdf["benefited"]) & (~bdf["penalised"])]

    print(f"Pace beneficiaries (mult>1.05): {len(ben)} horses")
    if len(ben) > 0:
        print(f"  Win rate: {100*ben['won'].mean():.1f}%  Place rate: {100*ben['placed'].mean():.1f}%  "
              f"Avg finish: {ben['actual_place'].mean():.1f}")
    print(f"Pace penalised (mult<0.95): {len(pen)} horses")
    if len(pen) > 0:
        print(f"  Win rate: {100*pen['won'].mean():.1f}%  Place rate: {100*pen['placed'].mean():.1f}%  "
              f"Avg finish: {pen['actual_place'].mean():.1f}")
    print(f"Neutral (0.95≤mult≤1.05): {len(neutral)} horses")
    if len(neutral) > 0:
        print(f"  Win rate: {100*neutral['won'].mean():.1f}%  Place rate: {100*neutral['placed'].mean():.1f}%  "
              f"Avg finish: {neutral['actual_place'].mean():.1f}")

    # By predicted style × actual pace match
    print(f"\nLeaders in predicted Fast/Very Fast races:")
    fast_leaders = bdf[(bdf["pred_style"] == "Leader") & (bdf["actual_pace"].isin(["Fast", "Very Fast"]))]
    if len(fast_leaders) > 0:
        print(f"  N={len(fast_leaders)}  Win rate: {100*fast_leaders['won'].mean():.1f}%  "
              f"Place rate: {100*fast_leaders['placed'].mean():.1f}%  "
              f"Avg finish: {fast_leaders['actual_place'].mean():.1f}")
        for _, fl in fast_leaders.iterrows():
            print(f"    {fl['meeting']} R{fl['race']}: {fl['horse']} (mult={fl['pace_mult']:.3f}) "
                  f"finished #{int(fl['actual_place'])}")

    print(f"\nClosers in predicted Slow/Very Slow races:")
    slow_closers = bdf[(bdf["pred_style"] == "Closer") & (bdf["actual_pace"].isin(["Slow", "Very Slow", "Slightly Slow"]))]
    if len(slow_closers) > 0:
        print(f"  N={len(slow_closers)}  Win rate: {100*slow_closers['won'].mean():.1f}%  "
              f"Place rate: {100*slow_closers['placed'].mean():.1f}%")
        for _, sc in slow_closers.iterrows():
            print(f"    {sc['meeting']} R{sc['race']}: {sc['horse']} (mult={sc['pace_mult']:.3f}) "
                  f"finished #{int(sc['actual_place'])}")

# ══════════════════════════════════════════════════════════════════════════════
# METRIC 4: PROJECTED TIME ACCURACY
# ══════════════════════════════════════════════════════════════════════════════
_sep()
print("METRIC 4: PROJECTED TIME ACCURACY")
_sep()

time_valid = valid[valid["actual_ft"].notna() & (valid["actual_ft"] > 0)].copy()
print(f"\nHorses with valid projected + actual times: {len(time_valid)}")
print(f"\nTime error (actual - projected): positive = horse ran SLOWER than predicted")
print(f"  Mean error: {time_valid['time_error'].mean():+.3f}s")
print(f"  Median error: {time_valid['time_error'].median():+.3f}s")
print(f"  Std: {time_valid['time_error'].std():.3f}s")
print(f"  MAE: {time_valid['abs_time_error'].mean():.3f}s")
print(f"  RMSE: {np.sqrt((time_valid['time_error']**2).mean()):.3f}s")

# By rank band
print(f"\nBy predicted rank band:")
for lo, hi, label in [(1,3,"#1-3"),(4,6,"#4-6"),(7,9,"#7-9"),(10,99,"#10+")]:
    band = time_valid[(time_valid["pred_rank"]>=lo) & (time_valid["pred_rank"]<=hi)]
    if len(band) > 0:
        print(f"  {label:>5s}: n={len(band):>3d}  mean_err={band['time_error'].mean():+.3f}s  "
              f"MAE={band['abs_time_error'].mean():.3f}s  win%={100*band['won'].mean():.1f}%  "
              f"place%={100*band['placed'].mean():.1f}%")

# By distance
print(f"\nBy distance:")
for d in sorted(time_valid["distance"].unique()):
    dband = time_valid[time_valid["distance"] == d]
    print(f"  {d:>5d}m: n={len(dband):>3d}  mean_err={dband['time_error'].mean():+.3f}s  "
          f"MAE={dband['abs_time_error'].mean():.3f}s  RMSE={np.sqrt((dband['time_error']**2).mean()):.3f}s")

# By class
print(f"\nBy class:")
for c in sorted(time_valid["class"].dropna().unique()):
    cband = time_valid[time_valid["class"] == c]
    c_lbl = f"C{int(c)}" if pd.notna(c) and c > 0 else "Grp"
    print(f"  {c_lbl:>4s}: n={len(cband):>3d}  mean_err={cband['time_error'].mean():+.3f}s  "
          f"MAE={cband['abs_time_error'].mean():.3f}s")

# Best/worst individual predictions
print(f"\nBest 5 predictions (smallest |error|):")
best5 = time_valid.nsmallest(5, "abs_time_error")
for _, r in best5.iterrows():
    print(f"  {r['meeting']} R{r['race']}: {r['horse']:>22s}  pred={r['pred_time']:.2f}s  "
          f"actual={r['actual_ft']:.2f}s  err={r['time_error']:+.2f}s  place=#{int(r['actual_place'])}")

print(f"\nWorst 5 predictions (largest |error|):")
worst5 = time_valid.nlargest(5, "abs_time_error")
for _, r in worst5.iterrows():
    print(f"  {r['meeting']} R{r['race']}: {r['horse']:>22s}  pred={r['pred_time']:.2f}s  "
          f"actual={r['actual_ft']:.2f}s  err={r['time_error']:+.2f}s  place=#{int(r['actual_place'])}  "
          f"n_runs={int(r['pred_n_runs'])}  sf={r['pred_sf']:.2f}")

# ══════════════════════════════════════════════════════════════════════════════
# METRIC 5: SYSTEM BENEFICIARY/PENALTY ACCURACY
# ══════════════════════════════════════════════════════════════════════════════
_sep()
print("METRIC 5: SYSTEM BENEFICIARY & PENALTY SELECTION ACCURACY")
_sep()

# Context adjustment (draw/wide removal)
print("\n5a. Draw Context Adjustment (v4.0 Change 2):")
ctx_has = valid[valid["pred_ctx_adj"].notna() & (valid["pred_ctx_adj"].abs() > 0.001)]
if len(ctx_has) > 0:
    ctx_benefited = ctx_has[ctx_has["pred_ctx_adj"] > 0]  # positive = draw was removed (helped horse)
    ctx_penalised = ctx_has[ctx_has["pred_ctx_adj"] < 0]
    print(f"  Horses with draw context adjustments: {len(ctx_has)}")
    if len(ctx_benefited) > 0:
        print(f"  Context-boosted (draw luck removed → revealed better ability): {len(ctx_benefited)}")
        print(f"    Avg finish: {ctx_benefited['actual_place'].mean():.1f}  "
              f"Place rate: {100*ctx_benefited['placed'].mean():.1f}%")
    if len(ctx_penalised) > 0:
        print(f"  Context-penalised (draw penalty removed → worse true ability): {len(ctx_penalised)}")
        print(f"    Avg finish: {ctx_penalised['actual_place'].mean():.1f}  "
              f"Place rate: {100*ctx_penalised['placed'].mean():.1f}%")

# Uncertainty penalty
print(f"\n5b. Uncertainty Penalty:")
unc_horses = valid[valid["pred_unc_pen"] > 0]
no_unc = valid[valid["pred_unc_pen"] == 0]
print(f"  With penalty (thin data): {len(unc_horses)}  Avg finish: {unc_horses['actual_place'].mean():.1f}  "
      f"Place rate: {100*unc_horses['placed'].mean():.1f}%")
print(f"  Without penalty: {len(no_unc)}  Avg finish: {no_unc['actual_place'].mean():.1f}  "
      f"Place rate: {100*no_unc['placed'].mean():.1f}%")

# Class transition penalty
print(f"\n5c. Class Transition Penalty:")
cls_pen = valid[valid["pred_class_pen"] > 0.005]
no_cls = valid[valid["pred_class_pen"] <= 0.005]
if len(cls_pen) > 0:
    print(f"  Step-up penalised: {len(cls_pen)}  Avg pen: +{cls_pen['pred_class_pen'].mean():.3f}s  "
          f"Avg finish: {cls_pen['actual_place'].mean():.1f}  "
          f"Place rate: {100*cls_pen['placed'].mean():.1f}%")
    print(f"  No class penalty: {len(no_cls)}  Avg finish: {no_cls['actual_place'].mean():.1f}  "
          f"Place rate: {100*no_cls['placed'].mean():.1f}%")
    # Did step-up horses actually perform worse?
    if cls_pen['actual_place'].mean() > no_cls['actual_place'].mean():
        print(f"  ✓ Step-up horses finish worse on average — penalty directionally correct")
    else:
        print(f"  ✗ Step-up horses actually finish BETTER — penalty may be counterproductive")

# Trajectory
print(f"\n5d. Trajectory Detection:")
improving = valid[valid["pred_traj"] == "Improving"]
declining = valid[valid["pred_traj"] == "Declining"]
stable = valid[valid["pred_traj"].isin(["Stable", "Insufficient", ""])]
print(f"  Improving: {len(improving)} horses  Avg finish: {improving['actual_place'].mean():.1f}  "
      f"Place rate: {100*improving['placed'].mean():.1f}%  Win rate: {100*improving['won'].mean():.1f}%")
print(f"  Declining: {len(declining)} horses  Avg finish: {declining['actual_place'].mean():.1f}  "
      f"Place rate: {100*declining['placed'].mean():.1f}%  Win rate: {100*declining['won'].mean():.1f}%")
print(f"  Stable/Insuff: {len(stable)} horses  Avg finish: {stable['actual_place'].mean():.1f}  "
      f"Place rate: {100*stable['placed'].mean():.1f}%  Win rate: {100*stable['won'].mean():.1f}%")

# Form franking quality effect
print(f"\n5e. Form Franking (Race Quality Weight):")
high_q = valid[valid["pred_quality_eff"] > 1.05]
low_q = valid[valid["pred_quality_eff"] < 0.95]
mid_q = valid[(valid["pred_quality_eff"] >= 0.95) & (valid["pred_quality_eff"] <= 1.05)]
print(f"  High-quality form (wt>1.05): {len(high_q)} horses  "
      f"Avg finish: {high_q['actual_place'].mean():.1f}  "
      f"Place rate: {100*high_q['placed'].mean():.1f}%") if len(high_q) > 0 else None
print(f"  Low-quality form (wt<0.95): {len(low_q)} horses  "
      f"Avg finish: {low_q['actual_place'].mean():.1f}  "
      f"Place rate: {100*low_q['placed'].mean():.1f}%") if len(low_q) > 0 else None
print(f"  Neutral form: {len(mid_q)} horses  "
      f"Avg finish: {mid_q['actual_place'].mean():.1f}  "
      f"Place rate: {100*mid_q['placed'].mean():.1f}%") if len(mid_q) > 0 else None

# Consistency flag
print(f"\n5f. Consistency Flag:")
inconsistent = valid[valid["pred_consistency"] == "INCONSISTENT"]
consistent = valid[valid["pred_consistency"] != "INCONSISTENT"]
print(f"  INCONSISTENT: {len(inconsistent)} horses  Avg finish: {inconsistent['actual_place'].mean():.1f}  "
      f"Place rate: {100*inconsistent['placed'].mean():.1f}%")
print(f"  Consistent: {len(consistent)} horses  Avg finish: {consistent['actual_place'].mean():.1f}  "
      f"Place rate: {100*consistent['placed'].mean():.1f}%")

# ══════════════════════════════════════════════════════════════════════════════
# METRIC 6: ESZ & HORSE PROFILE ACCURACY
# ══════════════════════════════════════════════════════════════════════════════
_sep()
print("METRIC 6: ESZ & HORSE PROFILE ACCURACY")
_sep()

# Style match
style_valid = valid[(valid["pred_style"] != "Unknown") & (valid["actual_style"] != "Unknown")].copy()
print(f"\n6a. Running Style Prediction:")
print(f"  Horses with both predicted + actual style: {len(style_valid)}")
style_match = (style_valid["pred_style"] == style_valid["actual_style"]).sum()
print(f"  Exact match: {style_match}/{len(style_valid)} ({100*style_match/len(style_valid):.1f}%)")

# Adjacent match (Leader↔On-Pace, On-Pace↔Midfield, Midfield↔Closer)
STYLE_ORDER = {"Leader": 0, "On-Pace": 1, "Midfield": 2, "Closer": 3}
style_valid["pred_ord"] = style_valid["pred_style"].map(STYLE_ORDER)
style_valid["actual_ord"] = style_valid["actual_style"].map(STYLE_ORDER)
style_valid["style_dist"] = (style_valid["pred_ord"] - style_valid["actual_ord"]).abs()
adj_match = (style_valid["style_dist"] <= 1).sum()
print(f"  Within 1 band: {adj_match}/{len(style_valid)} ({100*adj_match/len(style_valid):.1f}%)")

# Confusion matrix
print(f"\n  Style Confusion Matrix (Predicted × Actual):")
cm = pd.crosstab(style_valid["pred_style"], style_valid["actual_style"],
                 margins=True, margins_name="Total")
print(cm.to_string())

# ESZ correlation with actual first-call position
print(f"\n6b. ESZ (Early Speed Z-score) Accuracy:")
esz_df = valid[valid["pred_esz"].notna() & (valid["pred_esz"] != 0)].copy()
# Get actual normalised first-call position
esz_actuals = []
for _, r in esz_df.iterrows():
    mdate = r["meeting"]
    rnum = r["race"]
    hn = r["horse"]
    race_actuals_r = db_full[(db_full["race_date"].astype(str).str.startswith(mdate))
                             & (db_full["race_number"] == rnum)]
    horse_actual = race_actuals_r[race_actuals_r["horse_name"] == hn]
    if len(horse_actual) == 0:
        esz_actuals.append(None)
        continue
    rp = str(horse_actual.iloc[0].get("running_positions", ""))
    fp = mod._parse_first_position(rp)
    fs = len(race_actuals_r)
    if fp and fs > 0:
        esz_actuals.append(fp / fs)
    else:
        esz_actuals.append(None)

esz_df["actual_norm_pos"] = esz_actuals
esz_valid = esz_df[esz_df["actual_norm_pos"].notna()].copy()
# ESZ: negative = front-runner (fast early), positive = closer
# actual_norm_pos: low = front, high = back
# Predicted ESZ should correlate POSITIVELY with actual_norm_pos
if len(esz_valid) >= 10:
    rho, p = sp_stats.spearmanr(esz_valid["pred_esz"], esz_valid["actual_norm_pos"])
    print(f"  ESZ vs actual normalised position: ρ={rho:.3f} (p={p:.4f}, n={len(esz_valid)})")
    # Note: ESZ sign was fixed — negative = front-runner, positive = back-marker
    # So ESZ should positively correlate with normalised position (low pos = front = negative ESZ)
    # Actually: ESZ = -(mean_pos - 0.50) / 0.20, so negative ESZ = back marker
    # Wait, let me re-check the code...
    # ESZ = (mean_pos - 0.50) / 0.20 — POSITIVE ESZ = further back than median
    # No wait, the v3.4.8 code says:
    # early_speed_z = (mean_pos - 0.50) / 0.20 ... but the ESZ sign was fixed
    # Actually from the code line ~869: early_speed_z = (mean_pos - 0.50) / 0.20
    # mean_pos is normalised first-call (low = front runner)
    # So ESZ > 0 means typically behind midfield = SLOWER early speed
    # ESZ < 0 means typically ahead of midfield = FASTER early speed (front-runner)
    # We expect ESZ to correlate POSITIVELY with actual_norm_pos
    if rho > 0.3:
        print(f"  ✓ Good correlation — ESZ predicts running position well")
    elif rho > 0.15:
        print(f"  ~ Moderate correlation — ESZ has some predictive value")
    else:
        print(f"  ✗ Weak/negative correlation — ESZ not predicting early speed accurately")

# Profile confidence vs actual performance
print(f"\n6c. Profile Confidence vs Performance:")
for conf in ["high", "medium", "low", "no_data"]:
    grp = valid[valid.apply(lambda r: True if conf == "no_data" and r["pred_n_runs"] == 0
                            else (conf == "low" and 0 < r["pred_n_runs"] <= 2)
                            if conf == "low" else (conf == "medium" and 2 < r["pred_n_runs"] <= 5)
                            if conf == "medium" else (conf == "high" and r["pred_n_runs"] > 5)
                            if conf == "high" else False, axis=1)]
    if len(grp) == 0:
        continue
    mae = grp["abs_time_error"].mean() if grp["abs_time_error"].notna().sum() > 0 else float('nan')
    print(f"  {conf:>8s} (n_runs {'=0' if conf=='no_data' else '1-2' if conf=='low' else '3-5' if conf=='medium' else '>5'}): "
          f"{len(grp)} horses  MAE={mae:.3f}s  Place rate={100*grp['placed'].mean():.1f}%")

# ══════════════════════════════════════════════════════════════════════════════
# METRIC 7: RISK METRIC ACCURACY
# ══════════════════════════════════════════════════════════════════════════════
_sep()
print("METRIC 7: RISK METRIC ACCURACY (Diagnostic)")
_sep()

print("\nDoes higher risk = worse performance? (It should, if risk is meaningful)")
risk_valid = valid[valid["pred_risk"].notna()].copy()

for tier, label in [("Low", "Low (≤30)"), ("Moderate", "Moderate (31-55)"), ("High", "High (>55)")]:
    grp = risk_valid[risk_valid["pred_risk_tier"] == tier]
    if len(grp) == 0:
        continue
    print(f"\n  {label}: {len(grp)} horses")
    print(f"    Win rate: {100*grp['won'].mean():.1f}%  Place rate: {100*grp['placed'].mean():.1f}%  "
          f"Avg finish: {grp['actual_place'].mean():.1f}")
    print(f"    MAE: {grp['abs_time_error'].mean():.3f}s  "
          f"Avg risk score: {grp['pred_risk'].mean():.1f}")

# Correlation: risk score vs actual place
if len(risk_valid) >= 20:
    rho_risk, p_risk = sp_stats.spearmanr(risk_valid["pred_risk"], risk_valid["actual_place"])
    print(f"\n  Risk score vs actual place: ρ={rho_risk:.3f} (p={p_risk:.4f})")
    if rho_risk > 0.1:
        print(f"  ✓ Higher risk → worse finishes (directionally correct)")
    elif rho_risk < -0.05:
        print(f"  ✗ INVERTED: Higher risk → BETTER finishes — risk metric counterproductive")
    else:
        print(f"  ~ Near zero — risk metric has no predictive value for finishing position")

# Risk vs actual time error
rho_err, p_err = sp_stats.spearmanr(risk_valid["pred_risk"], risk_valid["abs_time_error"])
print(f"  Risk score vs |time error|: ρ={rho_err:.3f} (p={p_err:.4f})")
if rho_err > 0.1:
    print(f"  ✓ Higher risk → more prediction error (risk captures uncertainty)")
else:
    print(f"  ✗ Risk score does NOT predict larger errors")

# Risk tiers — winners breakdown
print(f"\n  Winners by risk tier:")
winners = risk_valid[risk_valid["won"]]
for tier in ["Low", "Moderate", "High"]:
    n_tier = len(risk_valid[risk_valid["pred_risk_tier"] == tier])
    n_wins = len(winners[winners["pred_risk_tier"] == tier])
    pct = 100*n_wins/n_tier if n_tier > 0 else 0
    print(f"    {tier:>8s}: {n_wins} winners / {n_tier} horses ({pct:.1f}% win rate)")

# Specific examples of high-risk horses winning
print(f"\n  HIGH-RISK WINNERS (risk>50, won):")
hr_winners = winners[winners["pred_risk"] > 50].sort_values("pred_risk", ascending=False)
for _, r in hr_winners.head(10).iterrows():
    print(f"    {r['meeting']} R{r['race']}: {r['horse']:>22s}  risk={r['pred_risk']:.0f} ({r['pred_risk_tier']})  "
          f"pred_rank=#{int(r['pred_rank'])}  odds={r['actual_odds']}")

print(f"\n  LOW-RISK LOSERS (risk<30, finished ≥6th):")
lr_losers = risk_valid[(risk_valid["pred_risk"] < 30) & (risk_valid["actual_place"] >= 6)]
for _, r in lr_losers.head(10).iterrows():
    print(f"    {r['meeting']} R{r['race']}: {r['horse']:>22s}  risk={r['pred_risk']:.0f} ({r['pred_risk_tier']})  "
          f"pred_rank=#{int(r['pred_rank'])}  finished=#{int(r['actual_place'])}  odds={r['actual_odds']}")

# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY & CALIBRATION RECOMMENDATIONS
# ══════════════════════════════════════════════════════════════════════════════
_sep()
print("SUMMARY & CALIBRATION RECOMMENDATIONS")
_sep()

print(f"""
SAMPLE: {total_races} races across 3 meetings ({len(valid)} horse-level predictions)
  April 1 (ST/AWT): 9 races    April 6 (ST/Turf): 11 races    April 8 (HV/Turf): 9 races

KEY FINDINGS:
""")

# Auto-generate findings based on data
# 1. Ranking
wr_med = np.median(winner_ranks)
print(f"1. RANKING: Median winner rank = {wr_med:.0f}")
print(f"   Winners in top-3: {(wr_arr<=3).sum()}/{total_races} ({100*(wr_arr<=3).sum()/total_races:.0f}%)")
print(f"   Mean rho: {rho_df['rho'].mean():.3f}" if len(spearman_rhos) > 0 else "")

# 2. Pace
pcorr = 0
if len(pace_valid) > 0:
    pcorr = sp_stats.pearsonr(pace_valid["pred_pace_score"], pace_valid["actual_pace_dev"])
    print(f"\n2. PACE: Pearson r(pred, actual) = {pcorr[0]:.3f} (p={pcorr[1]:.4f})")
    print(f"   Mean absolute error: {pace_valid['pace_error'].abs().mean():.3f}s")
    print(f"   Bias: {pace_valid['pace_error'].mean():+.3f}s")

# 3. Time accuracy
print(f"\n3. TIME: MAE = {time_valid['abs_time_error'].mean():.3f}s, "
      f"RMSE = {np.sqrt((time_valid['time_error']**2).mean()):.3f}s")
print(f"   Bias: {time_valid['time_error'].mean():+.3f}s (positive = horses run slower than predicted)")

# 4. Risk
print(f"\n4. RISK: ρ(risk, place) = {rho_risk:.3f}")
if rho_risk < 0:
    print(f"   ⚠ Risk metric INVERTED — recommend replacing or removing")

print(f"\n{'='*100}")
print("END OF BACKTEST REPORT")
print(f"{'='*100}")
