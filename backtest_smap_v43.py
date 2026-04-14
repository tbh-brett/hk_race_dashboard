"""
v4.3 Backtest: Speed-Map Positional + Pace×Position×Style Adjustments
=====================================================================
Tests whether injecting speed-map-based time adjustments into projected_time
improves rank prediction accuracy over the last 20 meetings.

Approach:
  For each historical race with results:
    1. Load all runners with their historical data (excluding current race)
    2. Run the full projection pipeline (project_race)
    3. Record projected ranks WITH and WITHOUT smap adjustments
    4. Compare to actual finishing order (place)
    5. Compute Spearman rank correlation for each version

Outputs:
  - Per-coefficient accuracy metrics (sweep SMAP_TIME_COEFF from 0 to 0.20)
  - Best coefficient selection
  - Summary statistics
"""
import os, sys, math, shutil, warnings
import numpy as np
import pandas as pd
from scipy import stats
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

# ── Paths ──
WORKSPACE = r"c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards"
os.chdir(WORKSPACE)
sys.path.insert(0, WORKSPACE)

DB_SRC = os.path.join(os.environ["TEMP"], "hkjc_results_updated.xlsx")
DB_TMP = os.path.join(os.environ["TEMP"], "hkjc_backtest_copy.xlsx")
shutil.copy2(DB_SRC, DB_TMP)

print("Loading DB...")
db_full = pd.read_excel(DB_TMP)
db_full["race_date"] = pd.to_datetime(db_full["race_date"])
print(f"  {len(db_full)} rows loaded")

# ── Last 20 meeting dates ──
meeting_dates = sorted(db_full["race_date"].unique())
last_20_dates = meeting_dates[-20:]
print(f"  Backtest period: {last_20_dates[0].date()} to {last_20_dates[-1].date()}")

# ── Import model functions ──
# We can't import the module directly (filename has spaces/dots), so exec it
# Instead, we'll extract only the functions we need by running a subset

# Load the model source and exec key functions
MODEL_FILE = os.path.join(WORKSPACE, "race_day_analysis_20260329_v3.4.8.py")
with open(MODEL_FILE, "r", encoding="utf-8") as f:
    source = f.read()

# We need these functions from the model:
# - lookup_expected_time, weight_band, class_band, _map_going
# - get_draw_offset, compute_horse_profile_runtime
# - compute_recency_residual, compute_projected_final_sectional
# - predict_race_pace_v3, compute_speed_map, compute_smap_time_adjustment
# - _parse_first_position, _classify_running_style, _estimate_wide_cost
# - _safe_place, _get_field_size, detect_trajectory

# Rather than exec the whole file (which would run main()), we'll use
# a simpler approach: reconstruct speed map from historical data directly.

# ═══════════════════════════════════════════════════════════════════════════
# SIMPLIFIED SPEED MAP RECONSTRUCTION
# ═══════════════════════════════════════════════════════════════════════════

def reconstruct_speed_map_simple(runners_df, distance, venue, race_course):
    """Reconstruct speed map from historical data.
    
    For each runner: compute ESZ from their prior races, use actual draw.
    Then run the same column/row assignment logic.
    """
    field_size = len(runners_df)
    is_straight = (venue == "ST" and distance == 1000)
    
    n_rows = 3
    n_cols = max(3, min(6, math.ceil(field_size / 3)))
    wide_cap = 4
    
    horses = []
    for _, r in runners_df.iterrows():
        # Compute ESZ from running positions in prior races
        esz = compute_esz_from_history(r["horse_name"], db_full, r["race_date"])
        style = compute_style_from_history(r["horse_name"], db_full, r["race_date"])
        
        draw = int(r["draw"]) if pd.notna(r.get("draw")) else field_size // 2
        horses.append({
            "horse_name": r["horse_name"],
            "horse_no": int(r.get("horse_number", 0)),
            "draw": draw,
            "esz": esz,
            "dominant_style": style,
            "place": int(r["place"]) if pd.notna(r.get("place")) and str(r["place"]).strip().isdigit() else 99,
        })
    
    # Column assignment (same as model)
    horses.sort(key=lambda h: h["esz"])
    horses_per_col = max(1, math.ceil(field_size / n_cols))
    for i, h in enumerate(horses):
        col = n_cols - (i // horses_per_col)
        h["smap_col"] = max(1, min(n_cols, col))
    
    # Row assignment (same as model)
    grid = {}
    for col in range(n_cols, 0, -1):
        col_horses = [h for h in horses if h["smap_col"] == col]
        if not col_horses:
            continue
        if is_straight:
            col_horses.sort(key=lambda h: -h["draw"])
        else:
            col_horses.sort(key=lambda h: h["draw"])
        for idx, h in enumerate(col_horses):
            row = min(idx + 1, n_rows)
            if row == n_rows:
                wt = sum(1 for (c, r) in grid if r == n_rows)
                if wt >= wide_cap:
                    row = max(1, n_rows - 1)
            if (col, row) in grid:
                for try_row in range(1, n_rows + 1):
                    if (col, try_row) not in grid:
                        if try_row == n_rows:
                            wt2 = sum(1 for (c, r) in grid if r == n_rows)
                            if wt2 >= wide_cap:
                                continue
                        row = try_row
                        break
            h["smap_row"] = row
            grid[(h["smap_col"], row)] = h
    
    # Advantage scoring (simplified — key rules only)
    for h in horses:
        advantage = 0.0
        style = h["dominant_style"]
        col = h["smap_col"]
        row = h["smap_row"]
        
        if not is_straight:
            if row == 1 and col >= n_cols - 1:
                if style in ("Leader", "On-Pace"):
                    advantage += 0.5
            elif row == 3 and col >= n_cols - 1:
                advantage -= 0.4
            
            if style == "Leader":
                if col >= n_cols - 1 and row <= 2:
                    advantage += 0.3
            elif style == "Closer":
                if col <= 2 and row >= 2:
                    advantage += 0.3
                elif col <= 2 and row == 1:
                    advantage += 0.1
            elif style in ("Midfield", "On-Pace"):
                if row == 1 and 2 <= col <= n_cols - 2:
                    # Check cover
                    if (grid.get((col + 1, row)) is not None
                            or grid.get((col, row + 1)) is not None):
                        advantage -= 0.3
            
            if h["draw"] > field_size * 0.7 and col >= n_cols - 1:
                advantage -= 0.3
            
            if h["draw"] <= max(2, field_size * 0.2) and h["esz"] < -0.3:
                advantage += 0.3
            
            if venue == "HV" and row == 3:
                advantage -= 0.3
        else:
            if h["draw"] >= field_size * 0.8:
                advantage += 0.2
            elif h["draw"] <= max(2, field_size * 0.2):
                advantage -= 0.2
        
        h["smap_advantage"] = round(advantage, 2)
    
    return horses


def compute_esz_from_history(horse_name, db, cutoff_date):
    """Compute early_speed_z from races BEFORE cutoff_date."""
    runs = db[(db["horse_name"] == horse_name) & (db["race_date"] < cutoff_date)].copy()
    if len(runs) == 0:
        return 0.0
    
    first_positions = []
    for _, run in runs.iterrows():
        rp = run.get("running_positions", "")
        if pd.isna(rp) or not str(rp).strip():
            continue
        parts = str(rp).strip().split()
        try:
            fp = int(float(parts[0]))
        except (ValueError, TypeError):
            continue
        # Field size for that race
        rd, rn = run["race_date"], run["race_number"]
        fs_mask = (db["race_date"] == rd) & (db["race_number"] == rn)
        fs = int(fs_mask.sum())
        if fs <= 0:
            continue
        first_positions.append(fp / fs)
    
    if len(first_positions) >= 2:
        mean_pos = float(np.mean(first_positions))
        return (mean_pos - 0.50) / 0.20
    elif len(first_positions) == 1:
        return (first_positions[0] - 0.50) / 0.20
    return 0.0


def compute_style_from_history(horse_name, db, cutoff_date):
    """Compute dominant_style from races BEFORE cutoff_date."""
    runs = db[(db["horse_name"] == horse_name) & (db["race_date"] < cutoff_date)].copy()
    if len(runs) == 0:
        return "Unknown"
    
    style_counts = {"Leader": 0, "On-Pace": 0, "Midfield": 0, "Closer": 0}
    style_top3 = {"Leader": 0, "On-Pace": 0, "Midfield": 0, "Closer": 0}
    
    for _, run in runs.iterrows():
        rp = run.get("running_positions", "")
        if pd.isna(rp) or not str(rp).strip():
            continue
        parts = str(rp).strip().split()
        try:
            fp = int(float(parts[0]))
        except:
            continue
        rd, rn = run["race_date"], run["race_number"]
        fs = int(((db["race_date"] == rd) & (db["race_number"] == rn)).sum()) if pd.notna(rd) else 0
        if fs <= 0:
            continue
        # Classify
        if fp <= 2:
            style = "Leader"
        elif fp <= max(4, fs * 0.3):
            style = "On-Pace"
        elif fp >= max(8, fs * 0.7):
            style = "Closer"
        else:
            style = "Midfield"
        style_counts[style] += 1
        place = run.get("place", 99)
        if pd.notna(place):
            try:
                import re
                m = re.match(r'^(\d+)', str(place).strip())
                p = int(m.group(1)) if m else 99
            except:
                p = 99
            if p <= 3:
                style_top3[style] += 1
    
    total = sum(style_counts.values())
    if total == 0:
        return "Unknown"
    
    best_rate = -1.0
    dom = "Unknown"
    for s in ("Leader", "On-Pace", "Midfield", "Closer"):
        if style_counts[s] > 0:
            rate = style_top3[s] / style_counts[s]
            if rate > best_rate or (rate == best_rate and style_counts[s] > style_counts.get(dom, 0)):
                best_rate = rate
                dom = s
    return dom


def compute_pace_bucket(runners_df, db, distance, race_date):
    """Simplified pace prediction: count front-runners → bucket."""
    field_size = len(runners_df)
    if field_size == 0:
        return "Normal"
    
    pace_pressures = []
    for _, r in runners_df.iterrows():
        runs = db[(db["horse_name"] == r["horse_name"]) & (db["race_date"] < race_date)]
        if len(runs) == 0:
            pace_pressures.append(0.3)
            continue
        
        leader_count = 0
        front_count = 0
        total = 0
        for _, run in runs.iterrows():
            rp = run.get("running_positions", "")
            if pd.isna(rp):
                continue
            parts = str(rp).strip().split()
            try:
                fp = int(float(parts[0]))
            except:
                continue
            rd, rn = run["race_date"], run["race_number"]
            fs = int(((db["race_date"] == rd) & (db["race_number"] == rn)).sum())
            if fs <= 0:
                continue
            if fp <= 2:
                leader_count += 1
                front_count += 1
            elif fp <= max(4, fs * 0.3):
                front_count += 1
            total += 1
        
        if total > 0:
            lf = leader_count / total
            ff = front_count / total
            p = lf * 2.0 + ff * 1.0
            pace_pressures.append(p)
        else:
            pace_pressures.append(0.3)
    
    pace_index = sum(pace_pressures) / field_size
    dev = 0.1173 - 0.6253 * pace_index
    
    if dev <= -0.40:
        return "Fast"
    elif dev <= -0.20:
        return "Fast"  # "Slightly Fast" → bucket as Fast
    elif dev >= 0.35:
        return "Slow"
    elif dev >= 0.20:
        return "Slow"  # "Slightly Slow" → bucket as Slow
    return "Normal"


# ═══════════════════════════════════════════════════════════════════════════
# PACE × POSITION × STYLE ADJUSTMENT TABLE (same as model)
# ═══════════════════════════════════════════════════════════════════════════

PACE_POS_STYLE_ADJ = {
    ("Fast", "RAIL", "Leader"):    -0.06,
    ("Fast", "RAIL", "On-Pace"):   -0.03,
    ("Fast", "RAIL", "Midfield"):   0.00,
    ("Fast", "RAIL", "Closer"):    -0.04,
    ("Fast", "W2",   "Leader"):    -0.02,
    ("Fast", "W2",   "On-Pace"):    0.00,
    ("Fast", "W2",   "Midfield"):   0.00,
    ("Fast", "W2",   "Closer"):    -0.03,
    ("Fast", "WIDE", "Leader"):    +0.05,
    ("Fast", "WIDE", "On-Pace"):   +0.03,
    ("Fast", "WIDE", "Midfield"):   0.00,
    ("Fast", "WIDE", "Closer"):    -0.02,
    ("Normal", "RAIL", "Leader"):  -0.03,
    ("Normal", "RAIL", "On-Pace"):  0.00,
    ("Normal", "RAIL", "Midfield"): 0.00,
    ("Normal", "RAIL", "Closer"):   0.00,
    ("Normal", "W2",   "Leader"):   0.00,
    ("Normal", "W2",   "On-Pace"):  0.00,
    ("Normal", "W2",   "Midfield"): 0.00,
    ("Normal", "W2",   "Closer"):   0.00,
    ("Normal", "WIDE", "Leader"):  +0.02,
    ("Normal", "WIDE", "On-Pace"): +0.01,
    ("Normal", "WIDE", "Midfield"): 0.00,
    ("Normal", "WIDE", "Closer"):   0.00,
    ("Slow", "RAIL", "Leader"):    -0.06,
    ("Slow", "RAIL", "On-Pace"):   -0.03,
    ("Slow", "RAIL", "Midfield"):   0.00,
    ("Slow", "RAIL", "Closer"):    +0.04,
    ("Slow", "W2",   "Leader"):    -0.03,
    ("Slow", "W2",   "On-Pace"):   -0.01,
    ("Slow", "W2",   "Midfield"):   0.00,
    ("Slow", "W2",   "Closer"):    +0.03,
    ("Slow", "WIDE", "Leader"):     0.00,
    ("Slow", "WIDE", "On-Pace"):   +0.01,
    ("Slow", "WIDE", "Midfield"):  +0.01,
    ("Slow", "WIDE", "Closer"):    +0.05,
}

ROW_LABEL = {1: "RAIL", 2: "W2", 3: "WIDE"}


def compute_adjustments(smap_data, pace_bucket, coeff, cap=0.15):
    """Compute time adjustments for a given SMAP_TIME_COEFF."""
    adjustments = {}
    for h in smap_data:
        advantage = h.get("smap_advantage", 0.0)
        row = h.get("smap_row", 2)
        style = h.get("dominant_style", "Unknown")
        row_label = ROW_LABEL.get(row, "W2")
        
        # Module 1: positional
        pos_adj = -advantage * coeff
        pos_adj = max(-cap, min(cap, pos_adj))
        
        # Module 3: Pace × Position × Style
        if style in ("Leader", "On-Pace", "Midfield", "Closer"):
            pps_adj = PACE_POS_STYLE_ADJ.get((pace_bucket, row_label, style), 0.0)
        else:
            pps_adj = 0.0
        
        adjustments[h["horse_name"]] = pos_adj + pps_adj
    return adjustments


# ═══════════════════════════════════════════════════════════════════════════
# MAIN BACKTEST LOOP
# ═══════════════════════════════════════════════════════════════════════════

def safe_place(val):
    """Parse place safely."""
    import re
    if pd.isna(val):
        return 99
    m = re.match(r'^(\d+)', str(val).strip())
    return int(m.group(1)) if m else 99


# Coefficients to sweep
COEFFS = [0.00, 0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20]

print(f"\nSweeping SMAP_TIME_COEFF across {len(COEFFS)} values...")
print(f"{'Coeff':>6}  {'Races':>5}  {'Spearman_r':>10}  {'p':>8}  {'Top3_Hit%':>9}  {'Win_Hit%':>8}  {'Rank_MAE':>8}")
print("-" * 72)

# Pre-build race index
race_groups = db_full.groupby(["race_date", "race_number"])

# Collect all historical races from last 20 meetings
historical_races = []
for date in last_20_dates:
    date_races = db_full[db_full["race_date"] == date]
    for rn in sorted(date_races["race_number"].unique()):
        race_runners = date_races[date_races["race_number"] == rn].copy()
        # Need at least 4 runners with valid places
        race_runners["_place"] = race_runners["place"].apply(safe_place)
        valid_runners = race_runners[race_runners["_place"] <= 14]
        if len(valid_runners) >= 4:
            dist = valid_runners["distance"].iloc[0] if "distance" in valid_runners.columns else 1200
            if pd.isna(dist):
                continue
            venue = "HV" if "HV" in str(valid_runners.get("race_track", pd.Series(["ST"])).iloc[0]) else "ST"
            rc = str(valid_runners["race_course"].iloc[0]) if "race_course" in valid_runners.columns else "A"
            if pd.isna(rc) or rc == "nan":
                rc = "A"
            historical_races.append({
                "date": date,
                "race_number": rn,
                "runners": valid_runners,
                "distance": int(dist),
                "venue": venue,
                "race_course": rc,
            })

print(f"\n  {len(historical_races)} races with valid data in backtest period")

# For each race: reconstruct speed map, compute base residual ranks
# (We don't have the full projection pipeline here, so we use a proxy:
#  residual from finish_time vs race median as the "projection")
print("  Computing speed maps for all races...")

race_smap_data = []
n_errors = 0
n_skip_time = 0
for race_info in historical_races:
    runners = race_info["runners"]
    date = race_info["date"]
    dist = race_info["distance"]
    venue = race_info["venue"]
    rc = race_info["race_course"]
    
    # Actual places
    actual_places = {}
    for _, r in runners.iterrows():
        actual_places[r["horse_name"]] = safe_place(r["place"])
    
    # Compute "base projection" = pre-race estimate from prior form
    # For each horse: recency-weighted mean residual from their prior races
    # at similar distances. This simulates the model's projected_time.
    valid_times = runners[runners["finish_time_seconds"].notna() & (runners["finish_time_seconds"] > 0)]
    if len(valid_times) < 4:
        n_skip_time += 1
        continue
    
    race_median = valid_times["finish_time_seconds"].median()
    base_residuals = {}
    
    for _, r in valid_times.iterrows():
        horse = r["horse_name"]
        # Get PRIOR races only (before this race date)
        prior = db_full[(db_full["horse_name"] == horse) & 
                        (db_full["race_date"] < date) &
                        (db_full["finish_time_seconds"].notna()) &
                        (db_full["finish_time_seconds"] > 0)].copy()
        
        if len(prior) == 0:
            # No form: assign neutral residual (slight penalty for unknowns)
            base_residuals[horse] = 0.5
            continue
        
        prior = prior.sort_values("race_date", ascending=True)
        
        # Compute residual relative to each race's median
        projected_residuals = []
        weights = []
        lam = 0.85  # same decay as model
        n = len(prior)
        for idx, (_, pr) in enumerate(prior.iterrows()):
            # Get race median for that prior race
            pr_date = pr["race_date"]
            pr_rn = pr["race_number"]
            pr_dist = pr["distance"]
            
            # Distance proximity weight
            if pd.notna(pr_dist) and pd.notna(dist):
                ddiff = abs(int(pr_dist) - dist)
                if ddiff == 0:
                    dist_wt = 1.0
                elif ddiff <= 200:
                    dist_wt = 0.60
                elif ddiff <= 400:
                    dist_wt = 0.30
                else:
                    dist_wt = 0.10
            else:
                dist_wt = 0.5
            
            pr_mask = (db_full["race_date"] == pr_date) & (db_full["race_number"] == pr_rn)
            pr_times = db_full.loc[pr_mask & db_full["finish_time_seconds"].notna() & (db_full["finish_time_seconds"] > 0), "finish_time_seconds"]
            if len(pr_times) < 3:
                continue
            
            pr_median = pr_times.median()
            resid = pr["finish_time_seconds"] - pr_median
            
            # Recency weight
            w = lam ** (n - 1 - idx) * dist_wt
            projected_residuals.append(resid)
            weights.append(w)
        
        if len(projected_residuals) > 0:
            projected_residuals = np.array(projected_residuals)
            weights = np.array(weights)
            weights = weights / weights.sum()
            base_residuals[horse] = float(np.dot(projected_residuals, weights))
        else:
            base_residuals[horse] = 0.5
    
    # Reconstruct speed map
    try:
        smap = reconstruct_speed_map_simple(runners, dist, venue, rc)
    except Exception as e:
        if n_errors < 3:
            print(f"    ERROR in R{race_info['race_number']} on {date.date()}: {e}")
            n_errors += 1
        continue
    
    # Compute pace bucket
    pace_bucket = compute_pace_bucket(runners, db_full, dist, date)
    
    race_smap_data.append({
        "race_info": race_info,
        "actual_places": actual_places,
        "base_residuals": base_residuals,
        "smap": smap,
        "pace_bucket": pace_bucket,
    })

print(f"  {len(race_smap_data)} races with speed maps computed (skipped: {n_skip_time} no-time, {n_errors} errors)")

# Sweep coefficients
best_coeff = 0.0
best_spearman = -1.0

for coeff in COEFFS:
    all_projected_ranks = []
    all_actual_places = []
    top3_hits = 0
    top3_total = 0
    win_hits = 0
    win_total = 0
    
    for rd in race_smap_data:
        smap = rd["smap"]
        base = rd["base_residuals"]
        actual = rd["actual_places"]
        pace = rd["pace_bucket"]
        
        # Compute adjustments
        adj = compute_adjustments(smap, pace, coeff)
        
        # Build adjusted times
        adjusted_times = {}
        for name, resid in base.items():
            smap_adj = adj.get(name, 0.0)
            adjusted_times[name] = resid + smap_adj
        
        if len(adjusted_times) < 4:
            continue
        
        # Rank by adjusted time (lower = better)
        sorted_horses = sorted(adjusted_times.items(), key=lambda x: x[1])
        projected_ranks = {name: rank + 1 for rank, (name, _) in enumerate(sorted_horses)}
        
        # Collect for Spearman
        common = set(projected_ranks.keys()) & set(actual.keys())
        common = [n for n in common if actual[n] <= 14]
        if len(common) < 4:
            continue
        
        for name in common:
            all_projected_ranks.append(projected_ranks[name])
            all_actual_places.append(actual[name])
        
        # Top-3 hit rate: how many of our top-3 finished top-3
        our_top3 = set(list(projected_ranks.keys())[:3])
        actual_top3 = set(n for n, p in actual.items() if p <= 3)
        top3_hits += len(our_top3 & actual_top3)
        top3_total += min(3, len(actual_top3))
        
        # Win hit rate: did our #1 pick win?
        our_winner = sorted_horses[0][0]
        if actual.get(our_winner, 99) == 1:
            win_hits += 1
        win_total += 1
    
    if len(all_projected_ranks) < 10:
        continue
    
    # Spearman correlation
    rho, p_val = stats.spearmanr(all_projected_ranks, all_actual_places)
    
    # Rank MAE
    rank_mae = np.mean(np.abs(np.array(all_projected_ranks) - np.array(all_actual_places)))
    
    top3_pct = 100 * top3_hits / top3_total if top3_total > 0 else 0
    win_pct = 100 * win_hits / win_total if win_total > 0 else 0
    
    print(f"  {coeff:5.2f}  {win_total:5d}  {rho:10.4f}  {p_val:8.2e}  {top3_pct:8.1f}%  {win_pct:7.1f}%  {rank_mae:8.2f}")
    
    if rho > best_spearman:
        best_spearman = rho
        best_coeff = coeff

print(f"\n{'='*72}")
print(f"BEST COEFFICIENT: {best_coeff:.2f}  (Spearman r = {best_spearman:.4f})")
print(f"{'='*72}")

# ── Detailed analysis for best coefficient ──
print(f"\n--- Detailed analysis at SMAP_TIME_COEFF = {best_coeff:.2f} ---")

# Re-run with best coeff and collect per-race stats
rank_changes = []
benefit_to_actual = {"helped": 0, "hurt": 0, "neutral": 0}

for rd in race_smap_data:
    smap = rd["smap"]
    base = rd["base_residuals"]
    actual = rd["actual_places"]
    pace = rd["pace_bucket"]
    
    adj_0 = compute_adjustments(smap, pace, 0.0)
    adj_best = compute_adjustments(smap, pace, best_coeff)
    
    times_0 = {n: base[n] + adj_0.get(n, 0.0) for n in base}
    times_best = {n: base[n] + adj_best.get(n, 0.0) for n in base}
    
    if len(times_0) < 4:
        continue
    
    ranks_0 = {n: r + 1 for r, (n, _) in enumerate(sorted(times_0.items(), key=lambda x: x[1]))}
    ranks_best = {n: r + 1 for r, (n, _) in enumerate(sorted(times_best.items(), key=lambda x: x[1]))}
    
    for name in base:
        if name not in actual or actual[name] > 14:
            continue
        r0 = ranks_0.get(name, 99)
        rb = ranks_best.get(name, 99)
        ap = actual[name]
        
        if r0 != rb:
            # Rank changed
            improved = abs(rb - ap) < abs(r0 - ap)
            worsened = abs(rb - ap) > abs(r0 - ap)
            rank_changes.append({
                "horse": name,
                "actual": ap,
                "rank_before": r0,
                "rank_after": rb,
                "improved": improved,
            })
            if improved:
                benefit_to_actual["helped"] += 1
            elif worsened:
                benefit_to_actual["hurt"] += 1
            else:
                benefit_to_actual["neutral"] += 1

total_changed = len(rank_changes)
improved = sum(1 for r in rank_changes if r["improved"])
print(f"\nRank changes: {total_changed} horses changed rank")
if total_changed > 0:
    print(f"  Improved (closer to actual):  {improved} ({100*improved/total_changed:.1f}%)")
    print(f"  Worsened (further from actual): {total_changed - improved} ({100*(total_changed-improved)/total_changed:.1f}%)")
print(f"\nBenefit tracking: {benefit_to_actual}")

# Show some example rank changes
examples = sorted(rank_changes, key=lambda x: abs(x["rank_before"] - x["rank_after"]), reverse=True)[:10]
print(f"\nTop 10 largest rank changes:")
print(f"  {'Horse':25s}  {'Actual':>6}  {'Before':>6}  {'After':>5}  {'Better?':>7}")
for ex in examples:
    better = "YES" if ex["improved"] else "no"
    print(f"  {ex['horse']:25s}  {ex['actual']:6d}  {ex['rank_before']:6d}  {ex['rank_after']:5d}  {better:>7}")

print("\nDone.")
