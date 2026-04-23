#!/usr/bin/env python3
"""
SARR — Sectional-Anchored Relative Rating
==========================================
Independent standalone prototype for HKJC race ranking prediction.

Core philosophy:
  Rank horses by a multi-factor composite strength score derived from
  FIELD-RELATIVE metrics — NOT from absolute time projections.

Key differences from ET-Residual:
  1. No Expected Time lookup tables — uses race-median-normalised performance
  2. Late sectional ability as a primary signal (proven rho=0.25-0.36)
  3. Direct ordinal scoring (no time → ranking conversion)
  4. HKJC rating for class calibration (not ET gap)
  5. Style x venue fit as first-class signal (not dependent on pace prediction)
  6. Market odds benchmark included for context

Factors:
  F1: FMRP  — Field-Median Relative Performance (how fast vs field)
  F2: LSA   — Late Sectional Ability (finishing strength)
  F3: ESZ   — Early Speed (tactical speed / position advantage)
  F4: TFS   — Tactical Fit Score (style x venue x distance)
  F5: DS    — Draw Score (draw x venue x course x field_size)
  F6: RE    — Rating Edge (horse rating vs field median)
  F7: FT    — Form Trajectory (recent FMRP trend)
  F8: WPR   — Win/Place Rate (proven consistency)
"""

import os, sys, re, math, warnings, shutil, time as _time
import numpy as np
import pandas as pd
from scipy import stats
from collections import defaultdict

warnings.filterwarnings("ignore")

# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════
WORKSPACE = r"c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards"
os.chdir(WORKSPACE)

DB_SRC = "hkjc_results_updated.xlsx"
DB_TMP = os.path.join(os.environ.get("TEMP", "/tmp"), "hkjc_sarr_analysis.xlsx")

# Recency weighting
RECENCY_LAMBDA = 0.85
MAX_PRIOR_RUNS = 15

# Distance proximity weights
DIST_WT = {0: 1.0, 100: 0.75, 200: 0.50, 400: 0.25}

# Cross-condition discounts
VENUE_CROSS = 0.60
SURFACE_CROSS = 0.50

# Sectional structure
SECTION_LENGTHS = {
    1000: [200, 400, 400],
    1200: [400, 400, 400],
    1400: [200, 400, 400, 400],
    1600: [400, 400, 400, 400],
    1650: [450, 400, 400, 400],
    1800: [200, 400, 400, 400, 400],
    2000: [400, 400, 400, 400, 400],
    2200: [200, 400, 400, 400, 400, 400],
    2400: [400, 400, 400, 400, 400, 400],
}

# Unconditional style advantage (top-3 rate multiplier, from model briefing §4.6)
STYLE_ADV = {
    1000: {"Leader": 2.034, "On-Pace": 1.162, "Midfield": 0.573, "Closer": 0.418},
    1200: {"Leader": 1.708, "On-Pace": 1.204, "Midfield": 0.765, "Closer": 0.428},
    1400: {"Leader": 1.370, "On-Pace": 1.395, "Midfield": 0.954, "Closer": 0.424},
    1600: {"Leader": 1.455, "On-Pace": 1.439, "Midfield": 0.829, "Closer": 0.448},
    1650: {"Leader": 1.287, "On-Pace": 1.398, "Midfield": 0.861, "Closer": 0.542},
    1800: {"Leader": 1.951, "On-Pace": 0.627, "Midfield": 1.184, "Closer": 0.451},
    2000: {"Leader": 1.771, "On-Pace": 1.165, "Midfield": 0.866, "Closer": 0.450},
    2200: {"Leader": 1.142, "On-Pace": 1.004, "Midfield": 0.690, "Closer": 1.380},
}

# HV tight-track amplification
HV_STYLE_MOD = {"Leader": 1.15, "On-Pace": 1.10, "Midfield": 0.95, "Closer": 0.80}

# Ideal SSI by distance (strong finisher profile benefits more at distance)
IDEAL_SSI = {
    1000: 0.0, 1200: -0.10, 1400: -0.20, 1600: -0.30,
    1650: -0.30, 1800: -0.35, 2000: -0.40, 2200: -0.45, 2400: -0.50,
}

# Training/validation split
TRAIN_CUTOFF = pd.Timestamp("2026-03-29")
VAL_DATES = ["2026-04-01", "2026-04-06", "2026-04-08", "2026-04-12", "2026-04-15"]

# ET-Residual baselines (from backtest JSONs)
ET_BASELINES = {
    "2026-04-01": {"rho": 0.374, "top1": 0.111, "top3": 0.259},
    "2026-04-06": {"rho": 0.340, "top1": 0.241, "top3": 0.333},  # 3-meeting avg
    "2026-04-08": {"rho": 0.340, "top1": 0.241, "top3": 0.333},  # 3-meeting avg
    "2026-04-12": {"rho": 0.480, "top1": 0.143, "top3": 0.571},  # v4.4
}


# ════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ════════════════════════════════════════════════════════════════════════════
def safe_place(val):
    if pd.isna(val): return 99
    m = re.match(r"^(\d+)", str(val).strip())
    return int(m.group(1)) if m else 99

def safe_float(val, default=np.nan):
    try:
        return float(val)
    except (ValueError, TypeError):
        return default

def parse_sections(s):
    if pd.isna(s) or not str(s).strip(): return []
    out = []
    for p in str(s).split(";"):
        p = p.strip()
        if not p: continue
        try:
            v = float(p)
            if v > 0: out.append(v)
        except (ValueError, TypeError):
            continue
    return out

def classify_style(rp_str, field_size):
    if pd.isna(rp_str) or not str(rp_str).strip():
        return "Unknown"
    parts = str(rp_str).strip().split()
    try:
        fc = int(parts[0])
    except (ValueError, IndexError):
        return "Unknown"
    fs = max(field_size, 1)
    if fc <= 2:
        return "Leader"
    elif fc <= max(4, int(fs * 0.3)):
        return "On-Pace"
    elif fc >= max(8, int(fs * 0.7)):
        return "Closer"
    else:
        return "Midfield"

def dist_weight(delta_m):
    d = abs(delta_m)
    if d == 0: return 1.0
    elif d <= 100: return 0.75
    elif d <= 200: return 0.50
    elif d <= 400: return 0.25
    return 0.10

def get_style_fit(style, distance, venue):
    """Log-multiplier style advantage. Negative = advantage = better."""
    dists = sorted(STYLE_ADV.keys())
    closest = min(dists, key=lambda d: abs(d - distance))
    mult = STYLE_ADV[closest].get(style, 1.0)
    if venue == "HV":
        mult *= HV_STYLE_MOD.get(style, 1.0)
    return -math.log(max(mult, 0.05))


# ════════════════════════════════════════════════════════════════════════════
# MAIN ANALYSIS
# ════════════════════════════════════════════════════════════════════════════
def main():
    t0 = _time.time()

    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  SARR — Sectional-Anchored Relative Rating                  ║")
    print("║  Alternative Race Projection Model for HKJC                 ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    # ── Load data ──────────────────────────────────────────────────────
    print("\n▸ Loading data...")
    shutil.copy2(DB_SRC, DB_TMP)
    db = pd.read_excel(DB_TMP)
    db["race_date"] = pd.to_datetime(db["race_date"])
    db["_place"]    = db["place"].apply(safe_place)
    db["_ft"]       = db["finish_time_seconds"].apply(safe_float)
    db["_draw"]     = db["draw"].apply(safe_float)
    db["_rating"]   = db["rating"].apply(safe_float)
    db["_odds"]     = db["win_odds"].apply(safe_float)
    db["_distance"] = db["distance"].apply(safe_float)

    valid = db[(db["_place"] < 90) & db["_ft"].notna() & (db["_ft"] > 0)].copy()
    n_meetings = valid.groupby("race_date").ngroups
    print(f"  {len(valid):,} valid finishers | {n_meetings} meetings "
          f"| {valid['race_date'].min().date()} → {valid['race_date'].max().date()}")

    # ── Race-level aggregates ──────────────────────────────────────────
    race_agg = valid.groupby(["race_date", "race_number"]).agg(
        med_ft=("_ft", "median"),
        med_rating=("_rating", "median"),
        field_size=("horse_name", "count"),
        r_distance=("_distance", "first"),
        r_venue=("race_track", "first"),
        r_course=("race_course", "first"),
        r_surface=("track_type", "first"),
    ).reset_index()
    valid = valid.merge(race_agg, on=["race_date", "race_number"], how="left")

    # ── F1: FMRP (Field-Median Relative Performance) ──────────────────
    valid["fmrp"] = (valid["_ft"] - valid["med_ft"]) / valid["med_ft"] * 100

    # ── F6: Rating edge ───────────────────────────────────────────────
    valid["rating_edge"] = valid["_rating"] - valid["med_rating"]

    # ── Sectional decomposition → F2 (LSA) + F3 (ESZ) ────────────────
    def compute_zones(row):
        secs = parse_sections(row["sectiontimes"])
        if not secs: return None
        d = int(row["_distance"]) if not pd.isna(row["_distance"]) else 0
        lengths = SECTION_LENGTHS.get(d)
        if lengths is None or len(secs) != len(lengths): return None
        p4 = [t * 400.0 / l for t, l in zip(secs, lengths)]
        return (p4[0], np.mean(p4[1:-1]) if len(p4) >= 3 else (p4[0]+p4[-1])/2, p4[-1])

    valid["_zones"] = valid.apply(compute_zones, axis=1)
    has_z = valid["_zones"].notna()

    # Zone medians per race
    zdf = valid[has_z].copy()
    zdf["_early"] = zdf["_zones"].apply(lambda z: z[0])
    zdf["_late"]  = zdf["_zones"].apply(lambda z: z[2])

    race_z = zdf.groupby(["race_date", "race_number"]).agg(
        med_early=("_early", "median"), med_late=("_late", "median")
    ).reset_index()
    zdf = zdf.merge(race_z, on=["race_date", "race_number"], how="left")
    zdf["early_dev"] = zdf["_early"] - zdf["med_early"]
    zdf["late_dev"]  = zdf["_late"]  - zdf["med_late"]
    zdf["ssi"]       = zdf["late_dev"] - zdf["early_dev"]

    valid = valid.merge(
        zdf[["race_date", "race_number", "horse_name", "early_dev", "late_dev", "ssi"]],
        on=["race_date", "race_number", "horse_name"], how="left"
    )

    # ── Running style ─────────────────────────────────────────────────
    valid["_style"] = valid.apply(
        lambda r: classify_style(r["running_positions"], r["field_size"]), axis=1
    )

    print(f"  Features computed | zones: {has_z.sum():,} | styles: {(valid['_style']!='Unknown').sum():,}")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 1 — Concurrent factor correlations (within-race, same race)
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "━" * 64)
    print("  PHASE 1: Concurrent Factor Power (upper bound, within-race)")
    print("━" * 64)
    print(f"  {'Factor':<42} {'ρ vs Place':>10} {'p':>11} {'n':>7}")
    print(f"  {'─'*42} {'─'*10} {'─'*11} {'─'*7}")

    for col, label, flip in [
        ("fmrp",        "FMRP (field-relative speed)",     False),
        ("late_dev",    "Late dev (finishing power)",       False),
        ("early_dev",   "Early dev (starting speed)",      False),
        ("ssi",         "SSI (sectional shape)",           False),
        ("rating_edge", "Rating edge (rating − median)",   True),
        ("_odds",       "Market odds (benchmark)",         False),
    ]:
        mask = valid[col].notna() & (valid["_place"] < 90)
        if col == "_odds":
            mask &= valid[col] > 0
        n = mask.sum()
        if n < 100: continue
        rho, p = stats.spearmanr(valid.loc[mask, col], valid.loc[mask, "_place"])
        if flip: rho = -rho
        print(f"  {label:<42} {rho:>+10.4f} {p:>11.2e} {n:>7,}")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 2 — Chronological profile building (no look-ahead)
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "━" * 64)
    print("  PHASE 2: Building Predictive Profiles (chronological, no look-ahead)")
    print("━" * 64)

    # Sort everything by date+race
    valid_s = valid.sort_values(["race_date", "race_number", "_place"]).reset_index(drop=True)

    # Pre-build per-run feature dicts for fast lookup
    horse_hist = defaultdict(list)   # horse → list of run-dicts (newest first after sort)
    pred_rows = []                   # store (date, race, horse, factors, actual_place)

    n_races_done = 0
    for (date, rnum), race_g in valid_s.groupby(["race_date", "race_number"], sort=True):
        distance  = race_g["r_distance"].iloc[0]
        venue     = race_g["r_venue"].iloc[0]
        surface   = race_g["r_surface"].iloc[0]
        course    = race_g["r_course"].iloc[0]
        field_sz  = len(race_g)

        if pd.isna(distance) or distance <= 0 or field_sz < 4:
            # Still add runs to history, then skip scoring
            for _, r in race_g.iterrows():
                _append_to_history(horse_hist, r)
            continue

        # Collect ratings for field-median
        ratings_in_field = []
        for _, r in race_g.iterrows():
            hn = r["horse_name"]
            hist = horse_hist.get(hn, [])
            rat = _latest_rating(hist, r["_rating"])
            if not np.isnan(rat):
                ratings_in_field.append(rat)
        med_rat = np.median(ratings_in_field) if ratings_in_field else 60.0

        # Score each runner
        for _, r in race_g.iterrows():
            hn = r["horse_name"]
            hist = horse_hist.get(hn, [])
            if len(hist) < 1:
                # Debut — append run, skip prediction row
                continue

            prof = _build_profile(hist, distance, venue, surface)
            if prof is None:
                continue

            # Context-dependent factors
            rat = prof["rating"]
            if np.isnan(rat): rat = med_rat
            f_rating = -(rat - med_rat) / 10.0  # negative = better (higher-rated)

            f_style = get_style_fit(prof["style"], distance, venue)
            f_dist  = _dist_affinity(prof["avg_ssi"], distance)

            pred_rows.append({
                "date":     date,
                "race":     rnum,
                "horse":    hn,
                "place":    r["_place"],
                "odds":     r["_odds"],
                "distance": distance,
                "venue":    venue,
                "course":   course,
                "field_sz": field_sz,
                "draw":     r["_draw"],
                # Factors (all: positive = worse / higher predicted place)
                "f_fmrp":    prof["fmrp"],
                "f_lsa":     prof["lsa"],
                "f_esz":     prof["esz"],
                "f_style":   f_style,
                "f_rating":  f_rating,
                "f_traj":    prof["traj"],
                "f_wpr":     -prof["place_rate"] * 5,  # negative = better
                "f_dist":    f_dist,
                "f_late_std": prof["late_std"],
                "n_runs":    prof["n_runs"],
            })

        # After scoring, add all runners to history
        for _, r in race_g.iterrows():
            _append_to_history(horse_hist, r)
        n_races_done += 1

    pdf_all = pd.DataFrame(pred_rows)
    print(f"  Processed {n_races_done:,} races | {len(pdf_all):,} scored runners")
    print(f"  Skipped debuts: {valid_s.groupby(['race_date','race_number']).ngroups * 12 - len(pdf_all):,} (approx)")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 3 — Predictive factor correlations (training data)
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "━" * 64)
    print("  PHASE 3: Predictive Factor Power (prior profile → actual place)")
    print("━" * 64)

    train_pdf = pdf_all[pdf_all["date"] <= TRAIN_CUTOFF]
    factor_names = ["f_fmrp", "f_lsa", "f_esz", "f_style", "f_rating", "f_traj", "f_wpr", "f_dist"]
    factor_labels = [
        "FMRP (hist. field-relative speed)",
        "LSA  (hist. late sectional)",
        "ESZ  (hist. early speed)",
        "TFS  (style × venue × distance)",
        "RE   (rating edge vs field)",
        "Traj (form trajectory)",
        "WPR  (win/place rate)",
        "DA   (distance affinity)",
    ]

    print(f"\n  {'Factor':<40} {'ρ vs Place':>10} {'p':>11} {'n':>7}")
    print(f"  {'─'*40} {'─'*10} {'─'*11} {'─'*7}")

    factor_rhos = {}
    for fn, fl in zip(factor_names, factor_labels):
        mask = train_pdf[fn].notna() & (train_pdf["place"] < 90)
        n = mask.sum()
        if n < 100:
            factor_rhos[fn] = 0.0
            continue
        rho, p = stats.spearmanr(train_pdf.loc[mask, fn], train_pdf.loc[mask, "place"])
        factor_rhos[fn] = rho
        print(f"  {fl:<40} {rho:>+10.4f} {p:>11.2e} {n:>7,}")

    # Market odds benchmark on training data
    mask = train_pdf["odds"].notna() & (train_pdf["odds"] > 0) & (train_pdf["place"] < 90)
    if mask.sum() > 100:
        rho_odds, _ = stats.spearmanr(train_pdf.loc[mask, "odds"], train_pdf.loc[mask, "place"])
        print(f"  {'Market odds (benchmark)':<40} {rho_odds:>+10.4f} {'—':>11} {mask.sum():>7,}")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 4 — Weight optimisation (on training data)
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "━" * 64)
    print("  PHASE 4: Weight Optimisation")
    print("━" * 64)

    # Method: use per-race z-scored factors, OLS regression → coefficients = weights
    train_z = train_pdf.copy()
    for fn in factor_names:
        # Z-score within each race
        train_z[fn + "_z"] = train_z.groupby(["date", "race"])[fn].transform(
            lambda x: (x - x.mean()) / x.std() if x.std() > 1e-6 else 0.0
        )
    # Also z-score place within each race
    train_z["place_z"] = train_z.groupby(["date", "race"])["place"].transform(
        lambda x: (x - x.mean()) / x.std() if x.std() > 1e-6 else 0.0
    )

    # OLS: place_z ~ factor_z's
    z_cols = [fn + "_z" for fn in factor_names]
    X = train_z[z_cols].fillna(0).values
    y = train_z["place_z"].fillna(0).values
    # Solve with pseudo-inverse (Ridge-like with tiny regularisation)
    XtX = X.T @ X + np.eye(X.shape[1]) * 0.01
    Xty = X.T @ y
    betas = np.linalg.solve(XtX, Xty)

    # Normalise to positive weights (some betas should be positive since higher factor = worse place)
    # Keep raw betas — they represent the direction and magnitude
    weights = {}
    for i, fn in enumerate(factor_names):
        weights[fn] = betas[i]

    print(f"\n  OLS coefficients (positive = factor predicts worse place):")
    print(f"  {'Factor':<40} {'β':>8} {'direction':>12}")
    print(f"  {'─'*40} {'─'*8} {'─'*12}")
    for fn, fl in zip(factor_names, factor_labels):
        b = weights[fn]
        direction = "→ worse" if b > 0 else "→ better"
        print(f"  {fl:<40} {b:>+8.4f} {direction:>12}")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 5 — Draw advantage table (from training data)
    # ══════════════════════════════════════════════════════════════════
    draw_stats = _compute_draw_stats(valid_s[valid_s["race_date"] <= TRAIN_CUTOFF])
    print(f"\n  Draw stats computed: {len(draw_stats)} (venue, draw) cells")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 6 — Validation on April 2026 meetings
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "━" * 64)
    print("  PHASE 5: Validation — April 2026 Meetings")
    print("━" * 64)

    val_dates = pd.to_datetime(VAL_DATES)
    val_pdf = pdf_all[pdf_all["date"].isin(val_dates)].copy()

    if len(val_pdf) == 0:
        print("  ⚠ No validation data found for April dates!")
        return

    # Compute SARR composite score
    val_pdf["sarr"] = sum(weights[fn] * val_pdf[fn].fillna(0) for fn in factor_names)

    # Add draw bonus (small, Bayesian-shrunk)
    val_pdf["draw_adj"] = val_pdf.apply(
        lambda r: _get_draw_score(r["draw"], r["venue"], draw_stats), axis=1
    )
    val_pdf["sarr"] += val_pdf["draw_adj"] * 0.3  # conservative draw weight

    # Per-race metrics
    race_metrics = []
    for (date, rnum), rg in val_pdf.groupby(["date", "race"]):
        if len(rg) < 4: continue
        rho, p = stats.spearmanr(rg["sarr"], rg["place"])
        top1 = 1 if rg.sort_values("sarr").iloc[0]["place"] == 1 else 0
        top3_preds = set(rg.sort_values("sarr").head(3)["horse"])
        top3_actual = set(rg.sort_values("place").head(3)["horse"])
        top3_overlap = len(top3_preds & top3_actual)

        # Odds benchmark
        rho_odds = np.nan
        odds_top1 = 0
        odds_top3 = 0
        if rg["odds"].notna().all() and (rg["odds"] > 0).all():
            rho_odds, _ = stats.spearmanr(rg["odds"], rg["place"])
            odds_top1 = 1 if rg.sort_values("odds").iloc[0]["place"] == 1 else 0
            odds_top3_set = set(rg.sort_values("odds").head(3)["horse"])
            odds_top3 = len(odds_top3_set & top3_actual)

        race_metrics.append({
            "date": date, "race": rnum, "n": len(rg),
            "sarr_rho": rho, "sarr_top1": top1,
            "sarr_top3_overlap": top3_overlap,
            "odds_rho": rho_odds, "odds_top1": odds_top1,
            "odds_top3_overlap": odds_top3,
        })

    rm = pd.DataFrame(race_metrics)

    # Per-meeting summary
    print(f"\n  {'Meeting':<12} │ {'Races':>5} │ {'SARR ρ':>7} │ {'Odds ρ':>7} │ "
          f"{'SARR T1':>7} │ {'Odds T1':>7} │ {'SARR T3/3':>9} │ {'Odds T3/3':>9}")
    print(f"  {'─'*12}─┼─{'─'*5}─┼─{'─'*7}─┼─{'─'*7}─┼─"
          f"{'─'*7}─┼─{'─'*7}─┼─{'─'*9}─┼─{'─'*9}")

    for date in sorted(rm["date"].unique()):
        dg = rm[rm["date"] == date]
        n_r = len(dg)
        s_rho = dg["sarr_rho"].mean()
        o_rho = dg["odds_rho"].mean()
        s_t1  = dg["sarr_top1"].mean() * 100
        o_t1  = dg["odds_top1"].mean() * 100
        s_t3  = dg["sarr_top3_overlap"].mean()
        o_t3  = dg["odds_top3_overlap"].mean()

        ds = date.strftime("%b %d")
        print(f"  {ds:<12} │ {n_r:>5} │ {s_rho:>+7.3f} │ {o_rho:>+7.3f} │ "
              f"{s_t1:>6.1f}% │ {o_t1:>6.1f}% │ {s_t3:>9.2f} │ {o_t3:>9.2f}")

    # Aggregate
    print(f"  {'─'*12}─┼─{'─'*5}─┼─{'─'*7}─┼─{'─'*7}─┼─"
          f"{'─'*7}─┼─{'─'*7}─┼─{'─'*9}─┼─{'─'*9}")
    n_total = len(rm)
    print(f"  {'AGGREGATE':<12} │ {n_total:>5} │ {rm['sarr_rho'].mean():>+7.3f} │ "
          f"{rm['odds_rho'].mean():>+7.3f} │ "
          f"{rm['sarr_top1'].mean()*100:>6.1f}% │ {rm['odds_top1'].mean()*100:>6.1f}% │ "
          f"{rm['sarr_top3_overlap'].mean():>9.2f} │ {rm['odds_top3_overlap'].mean():>9.2f}")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 6 — Comparison vs ET-Residual
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "━" * 64)
    print("  PHASE 6: Comparison vs ET-Residual Baseline")
    print("━" * 64)

    # Compare on meetings where we have ET baselines
    print(f"\n  {'Meeting':<12} │ {'SARR ρ':>7} │ {'ET ρ':>7} │ {'Δρ':>7} │ "
          f"{'SARR T1%':>8} │ {'ET T1%':>8} │ {'SARR T3ol':>9} │ {'ET T3%':>9}")
    print(f"  {'─'*12}─┼─{'─'*7}─┼─{'─'*7}─┼─{'─'*7}─┼─"
          f"{'─'*8}─┼─{'─'*8}─┼─{'─'*9}─┼─{'─'*9}")

    sarr_rhos, et_rhos = [], []
    sarr_t1s, et_t1s = [], []
    sarr_t3s, et_t3s = [], []

    for date in sorted(rm["date"].unique()):
        ds_key = date.strftime("%Y-%m-%d")
        dg = rm[rm["date"] == date]
        s_rho = dg["sarr_rho"].mean()
        s_t1  = dg["sarr_top1"].mean() * 100
        s_t3  = dg["sarr_top3_overlap"].mean()

        sarr_rhos.append(s_rho)
        sarr_t1s.append(s_t1)
        sarr_t3s.append(s_t3)

        et = ET_BASELINES.get(ds_key)
        if et:
            delta = s_rho - et["rho"]
            et_rhos.append(et["rho"])
            et_t1s.append(et["top1"] * 100)
            et_t3s.append(et["top3"] * 100)
            print(f"  {date.strftime('%b %d'):<12} │ {s_rho:>+7.3f} │ {et['rho']:>+7.3f} │ "
                  f"{delta:>+7.3f} │ {s_t1:>7.1f}% │ {et['top1']*100:>7.1f}% │ "
                  f"{s_t3:>9.2f} │ {et['top3']*100:>8.1f}%")
        else:
            print(f"  {date.strftime('%b %d'):<12} │ {s_rho:>+7.3f} │ {'N/A':>7} │ "
                  f"{'—':>7} │ {s_t1:>7.1f}% │ {'N/A':>8} │ "
                  f"{s_t3:>9.2f} │ {'N/A':>9}")

    if et_rhos:
        print(f"\n  Comparable meetings (with ET baseline):")
        print(f"    SARR mean ρ:  {np.mean(sarr_rhos[:len(et_rhos)]):+.3f}  "
              f"vs  ET mean ρ:  {np.mean(et_rhos):+.3f}  "
              f"(Δ = {np.mean(sarr_rhos[:len(et_rhos)]) - np.mean(et_rhos):+.3f})")
        print(f"    SARR Top-1:   {np.mean(sarr_t1s[:len(et_t1s)]):.1f}%  "
              f"vs  ET Top-1:   {np.mean(et_t1s):.1f}%")
        print(f"    SARR Top-3 overlap: {np.mean(sarr_t3s[:len(et_t3s)]):.2f}/3  "
              f"vs  ET Top-3%:  {np.mean(et_t3s):.1f}%")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 7 — Per-race breakdown (detailed)
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "━" * 64)
    print("  PHASE 7: Per-Race Detail (all validation races)")
    print("━" * 64)

    for (date, rnum), rg in val_pdf.groupby(["date", "race"]):
        if len(rg) < 4: continue
        rg_sorted = rg.sort_values("sarr")
        rho, _ = stats.spearmanr(rg_sorted["sarr"], rg_sorted["place"])

        ds = date.strftime("%b %d")
        dist = rg["distance"].iloc[0]
        venue = rg["venue"].iloc[0]

        # Winner info
        winner = rg.sort_values("place").iloc[0]
        winner_sarr_rank = (rg_sorted["horse"] == winner["horse"]).values.tolist()
        w_rank = winner_sarr_rank.index(True) + 1 if True in winner_sarr_rank else "?"

        print(f"\n  {ds} R{rnum} | {int(dist)}m {venue} | ρ={rho:+.3f} | "
              f"Winner: {winner['horse']} (SARR rank {w_rank}/{len(rg)})")

        # Top 4 SARR picks
        top4 = rg_sorted.head(4)
        for i, (_, h) in enumerate(top4.iterrows()):
            marker = " ✓" if h["place"] <= 3 else ""
            print(f"    {i+1}. {h['horse']:<22} SARR={h['sarr']:>+6.3f}  "
                  f"Actual={int(h['place']):>2}{marker}  "
                  f"FMRP={h['f_fmrp']:>+5.2f} LSA={h['f_lsa']:>+5.2f} "
                  f"RE={h['f_rating']:>+5.2f}")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 8 — Factor contribution analysis
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "━" * 64)
    print("  PHASE 8: Factor Contribution Analysis (validation data)")
    print("━" * 64)

    print(f"\n  Individual factor ρ vs actual place (April validation races):")
    print(f"  {'Factor':<40} {'ρ':>8} {'p':>11}")
    print(f"  {'─'*40} {'─'*8} {'─'*11}")
    for fn, fl in zip(factor_names, factor_labels):
        mask = val_pdf[fn].notna()
        if mask.sum() < 30: continue
        rho, p = stats.spearmanr(val_pdf.loc[mask, fn], val_pdf.loc[mask, "place"])
        print(f"  {fl:<40} {rho:>+8.4f} {p:>11.3e}")

    # Composite
    mask = val_pdf["sarr"].notna()
    if mask.sum() > 30:
        rho, p = stats.spearmanr(val_pdf.loc[mask, "sarr"], val_pdf.loc[mask, "place"])
        print(f"  {'SARR Composite':<40} {rho:>+8.4f} {p:>11.3e}")
    mask2 = val_pdf["odds"].notna() & (val_pdf["odds"] > 0)
    if mask2.sum() > 30:
        rho, p = stats.spearmanr(val_pdf.loc[mask2, "odds"], val_pdf.loc[mask2, "place"])
        print(f"  {'Market odds (benchmark)':<40} {rho:>+8.4f} {p:>11.3e}")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 9 — Distance-stratified analysis
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "━" * 64)
    print("  PHASE 9: Performance by Distance Bucket")
    print("━" * 64)
    for lo, hi, label in [(1000, 1200, "Sprint"), (1400, 1650, "Mile"), (1800, 2400, "Middle+")]:
        sub = val_pdf[(val_pdf["distance"] >= lo) & (val_pdf["distance"] <= hi)]
        if len(sub) < 20: continue
        # Per-race ρ
        rhos = []
        for (d, r), rg in sub.groupby(["date", "race"]):
            if len(rg) < 4: continue
            rho, _ = stats.spearmanr(rg["sarr"], rg["place"])
            rhos.append(rho)
        if rhos:
            print(f"  {label:<10} ({lo}–{hi}m): mean ρ = {np.mean(rhos):+.3f} | "
                  f"n_races = {len(rhos)}")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 10 — Venue-stratified analysis
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "━" * 64)
    print("  PHASE 10: Performance by Venue")
    print("━" * 64)
    for v in ["ST", "HV"]:
        sub = val_pdf[val_pdf["venue"] == v]
        if len(sub) < 20: continue
        rhos = []
        for (d, r), rg in sub.groupby(["date", "race"]):
            if len(rg) < 4: continue
            rho, _ = stats.spearmanr(rg["sarr"], rg["place"])
            rhos.append(rho)
        if rhos:
            print(f"  {v}: mean ρ = {np.mean(rhos):+.3f} | n_races = {len(rhos)}")

    # ══════════════════════════════════════════════════════════════════
    # SUMMARY
    # ══════════════════════════════════════════════════════════════════
    elapsed = _time.time() - t0
    print("\n" + "═" * 64)
    print("  SUMMARY")
    print("═" * 64)
    print(f"  Model: SARR (Sectional-Anchored Relative Rating)")
    print(f"  Factors: {len(factor_names)} core + draw adjustment")
    print(f"  Training: {len(train_pdf):,} runners from {train_pdf['date'].nunique()} meetings")
    print(f"  Validation: {len(val_pdf):,} runners from {val_pdf['date'].nunique()} meetings")
    agg_rho = rm["sarr_rho"].mean()
    agg_t1  = rm["sarr_top1"].mean() * 100
    agg_t3  = rm["sarr_top3_overlap"].mean()
    print(f"  SARR aggregate ρ:    {agg_rho:+.3f}")
    print(f"  SARR Top-1 rate:     {agg_t1:.1f}%")
    print(f"  SARR Top-3 overlap:  {agg_t3:.2f} / 3")
    if et_rhos:
        print(f"  ET-Resid aggregate ρ (comparable): {np.mean(et_rhos):+.3f}")
    print(f"  Runtime: {elapsed:.1f}s")
    print("═" * 64)


# ════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ════════════════════════════════════════════════════════════════════════════
def _append_to_history(horse_hist, row):
    """Append a run to the horse's history dict."""
    horse_hist[row["horse_name"]].append({
        "date":     row["race_date"],
        "fmrp":     row.get("fmrp", np.nan),
        "late_dev": row.get("late_dev", np.nan),
        "early_dev": row.get("early_dev", np.nan),
        "ssi":      row.get("ssi", np.nan),
        "style":    row.get("_style", "Unknown"),
        "rating":   row.get("_rating", np.nan),
        "place":    row.get("_place", 99),
        "distance": row.get("_distance", np.nan),
        "venue":    row.get("race_track", ""),
        "surface":  row.get("track_type", ""),
    })


def _latest_rating(hist, current_rating):
    """Get most recent non-null rating."""
    if not np.isnan(current_rating if not pd.isna(current_rating) else np.nan):
        return safe_float(current_rating)
    for run in reversed(hist):
        r = run.get("rating", np.nan)
        if not np.isnan(r):
            return r
    return np.nan


def _build_profile(hist, today_dist, today_venue, today_surface):
    """
    Build recency-weighted profile from horse history.
    hist: list of run dicts (chronological, oldest first)
    Returns: dict of factor values, or None if insufficient.
    """
    if not hist:
        return None

    # Reverse to newest-first for recency weighting
    runs = list(reversed(hist[-MAX_PRIOR_RUNS:]))

    weights = []
    for i, run in enumerate(runs):
        w = RECENCY_LAMBDA ** i
        # Distance proximity
        rd = run.get("distance", np.nan)
        if not np.isnan(rd) and not np.isnan(today_dist):
            w *= dist_weight(rd - today_dist)
        # Venue cross-discount
        rv = run.get("venue", "")
        if rv and today_venue and rv != today_venue:
            w *= VENUE_CROSS
        # Surface cross-discount
        rs = run.get("surface", "")
        if rs and today_surface and rs != today_surface:
            w *= SURFACE_CROSS
        weights.append(max(w, 0.01))

    weights = np.array(weights)

    def wmean(key):
        vals = np.array([r.get(key, np.nan) for r in runs], dtype=float)
        m = ~np.isnan(vals)
        if not m.any(): return np.nan
        return np.average(vals[m], weights=weights[m])

    fmrp_val = wmean("fmrp")
    lsa_val  = wmean("late_dev")
    esz_val  = wmean("early_dev")
    ssi_val  = wmean("ssi")

    # Late consistency
    late_vals = np.array([r.get("late_dev", np.nan) for r in runs], dtype=float)
    late_valid = late_vals[~np.isnan(late_vals)]
    late_std = np.std(late_valid) if len(late_valid) >= 2 else 0.5

    # Dominant style
    styles = [r["style"] for r in runs if r.get("style", "Unknown") != "Unknown"]
    if styles:
        style_counts = defaultdict(int)
        for s in styles:
            style_counts[s] += 1
        style = max(style_counts, key=style_counts.get)
    else:
        style = "Midfield"

    # Latest rating
    rating = np.nan
    for run in runs:
        r = run.get("rating", np.nan)
        if not np.isnan(r):
            rating = r
            break

    # Trajectory (FMRP trend: last 5 runs)
    fmrp_recent = [r.get("fmrp", np.nan) for r in runs[:5]]
    fmrp_recent = [v for v in fmrp_recent if not np.isnan(v)]
    if len(fmrp_recent) >= 3:
        x = np.arange(len(fmrp_recent))
        slope = stats.linregress(x, fmrp_recent).slope
    else:
        slope = 0.0

    # Win/place rate
    places = [r.get("place", 99) for r in runs]
    n_runs = len(places)
    place_rate = sum(1 for p in places if p <= 3) / max(n_runs, 1)

    return {
        "fmrp":      fmrp_val if not np.isnan(fmrp_val) else 0.0,
        "lsa":       lsa_val if not np.isnan(lsa_val) else 0.0,
        "esz":       esz_val if not np.isnan(esz_val) else 0.0,
        "avg_ssi":   ssi_val if not np.isnan(ssi_val) else 0.0,
        "late_std":  late_std,
        "style":     style,
        "rating":    rating,
        "traj":      slope,
        "place_rate": place_rate,
        "n_runs":    n_runs,
    }


def _dist_affinity(avg_ssi, distance):
    """Distance affinity: how well SSI matches distance demands. Higher = worse."""
    if np.isnan(avg_ssi) or np.isnan(distance):
        return 0.2
    target = IDEAL_SSI.get(int(distance), -0.20)
    return abs(avg_ssi - target)


def _compute_draw_stats(train_df):
    """Compute empirical draw advantage from training data."""
    draw_stats = {}
    for (venue, draw), grp in train_df.groupby(["race_track", "_draw"]):
        if pd.isna(draw) or draw < 1: continue
        # Place residual: (place - (field_size+1)/2)
        resids = []
        for _, r in grp.iterrows():
            if r["_place"] >= 90: continue
            med_place = (r["field_size"] + 1) / 2.0
            resids.append(r["_place"] - med_place)
        if len(resids) >= 10:
            draw_stats[(venue, int(draw))] = (len(resids), np.mean(resids))
    return draw_stats


def _get_draw_score(draw, venue, draw_stats):
    """Bayesian-shrunk draw disadvantage. Positive = worse draw."""
    if pd.isna(draw) or draw < 1:
        return 0.0
    key = (venue, int(draw))
    if key not in draw_stats:
        return 0.0
    n, mean_resid = draw_stats[key]
    k = 10  # shrinkage strength
    return (n / (n + k)) * mean_resid


if __name__ == "__main__":
    main()
