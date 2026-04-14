"""
Sectional Decomposition Analysis for HKJC Racing
==================================================
Standalone analysis — NOT integrated into the model.

Concept:
  For each horse-run, decompose the finish time into normalised sectional
  zones (Early / Mid / Late) and measure each horse's energy distribution
  pattern.  By comparing against race-median sectionals, we strip out
  pace/class/going effects and isolate HOW the horse distributes effort.

Sectional structure (from HKJC):
  1000m: S1(200m), S2(400m), S3(400m)
  1200m: S1(400m), S2(400m), S3(400m)
  1400m: S1(200m), S2(400m), S3(400m), S4(400m)
  1600m: S1(400m), S2(400m), S3(400m), S4(400m)
  1650m: S1(450m), S2(400m), S3(400m), S4(400m)
  1800m: S1(200m), S2(400m), S3(400m), S4(400m), S5(400m)
  2000m: S1(400m), S2(400m), S3(400m), S4(400m), S5(400m)
  2200m: S1(200m), S2(400m), S3(400m), S4(400m), S5(400m), S6(400m)
  2400m: S1(400m), S2(400m), S3(400m), S4(400m), S5(400m), S6(400m)

Normalisation into 3 zones:
  EARLY = first ~400m of running (S1, or S1+S2 for short-S1 distances, normalised to per-400m)
  MID   = middle section(s) between early and late (per-400m average)
  LATE  = final 400m (always the last section)

For each horse-run:
  early_dev = horse_early_per400m - race_median_early_per400m
  mid_dev   = horse_mid_per400m   - race_median_mid_per400m
  late_dev  = horse_late_per400m  - race_median_late_per400m

  Negative dev = faster than field median in that zone.

Sectional Shape Index (SSI):
  SSI = late_dev - early_dev
  SSI < 0 → strong finisher (late dev better than early dev relative to field)
  SSI > 0 → front-loaded (burns energy early, fades late)
  SSI ≈ 0 → even-paced

Deceleration Index (DI):
  DI = (late_per400m - early_per400m) / early_per400m
  Measures how much the horse slows from start to finish.
  Lower DI = better stamina / energy management.
"""

import os
import sys
import re
import math
import shutil
import warnings
import numpy as np
import pandas as pd
from collections import defaultdict
from scipy import stats
warnings.filterwarnings("ignore")

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
os.chdir(r"c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards")

DB_SRC  = os.path.join(os.environ["TEMP"], "hkjc_results_updated.xlsx")
DB_TMP  = os.path.join(os.environ["TEMP"], "hkjc_sectional_analysis.xlsx")

# Section lengths per distance (metres per section index)
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


# ═══════════════════════════════════════════════════════════════════════════════
# CORE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def parse_sections(sectional_str):
    """Parse semicolon-separated section times into list of floats."""
    if pd.isna(sectional_str) or not str(sectional_str).strip():
        return []
    parts = str(sectional_str).split(";")
    result = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        try:
            val = float(p)
            if val > 0:
                result.append(val)
        except (ValueError, TypeError):
            continue
    return result


def compute_per400m(sections, distance):
    """Convert raw section times to per-400m pace for each section."""
    if pd.isna(distance):
        return None
    lengths = SECTION_LENGTHS.get(int(distance))
    if lengths is None or len(sections) != len(lengths):
        return None
    per400 = []
    for time, length in zip(sections, lengths):
        per400.append(time * 400.0 / length)
    return per400


def decompose_zones(per400m_list):
    """Decompose per-400m list into Early / Mid / Late zones.

    Early = first section (per-400m normalised)
    Late  = last section (always 400m, so per400m = raw)
    Mid   = average of everything in between
    """
    if per400m_list is None or len(per400m_list) < 2:
        return None
    early = per400m_list[0]
    late = per400m_list[-1]
    if len(per400m_list) >= 3:
        mid = np.mean(per400m_list[1:-1])
    else:
        mid = (early + late) / 2.0  # only 2 sections: mid = average
    return {"early": early, "mid": mid, "late": late}


def safe_place(val):
    if pd.isna(val): return 99
    m = re.match(r"^(\d+)", str(val).strip())
    return int(m.group(1)) if m else 99


# ═══════════════════════════════════════════════════════════════════════════════
# LOAD DATA
# ═══════════════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTIONAL DECOMPOSITION ANALYSIS")
print("=" * 80)

shutil.copy2(DB_SRC, DB_TMP)
db = pd.read_excel(DB_TMP)
db["race_date"] = pd.to_datetime(db["race_date"])
print(f"\nLoaded {len(db)} rows")

# Parse sections for every row
db["_sections"] = db["sectiontimes"].apply(parse_sections)
db["_n_sections"] = db["_sections"].apply(len)
db["_place"] = db["place"].apply(safe_place)

# Compute per-400m pace for every row
db["_per400m"] = db.apply(lambda r: compute_per400m(r["_sections"], r["distance"])
                          if r["_n_sections"] > 0 else None, axis=1)

# Compute zone decomposition for every row
db["_zones"] = db["_per400m"].apply(lambda p: decompose_zones(p) if p else None)

valid = db[db["_zones"].notna()].copy()
valid["_early"] = valid["_zones"].apply(lambda z: z["early"])
valid["_mid"] = valid["_zones"].apply(lambda z: z["mid"])
valid["_late"] = valid["_zones"].apply(lambda z: z["late"])

print(f"Rows with valid zone decomposition: {len(valid)}")

# ═══════════════════════════════════════════════════════════════════════════════
# COMPUTE RACE-MEDIAN NORMALISED DEVIATIONS
# ═══════════════════════════════════════════════════════════════════════════════
print("\nComputing race-median normalised deviations...")

# For each (race_date, race_number), compute median early/mid/late
race_medians = valid.groupby(["race_date", "race_number"]).agg(
    med_early=("_early", "median"),
    med_mid=("_mid", "median"),
    med_late=("_late", "median"),
    field_size=("horse_name", "count"),
).reset_index()

valid = valid.merge(race_medians, on=["race_date", "race_number"], how="left")

# Deviations from race median (negative = faster than field)
valid["early_dev"] = valid["_early"] - valid["med_early"]
valid["mid_dev"] = valid["_mid"] - valid["med_mid"]
valid["late_dev"] = valid["_late"] - valid["med_late"]

# Sectional Shape Index: late_dev - early_dev
# Negative SSI = strong finisher; Positive SSI = front-loaded
valid["ssi"] = valid["late_dev"] - valid["early_dev"]

# Deceleration Index: (late - early) / early (absolute, not relative to field)
valid["di"] = (valid["_late"] - valid["_early"]) / valid["_early"]

# Residual from race median finish time (for correlation)
race_ft = valid.groupby(["race_date", "race_number"])["finish_time_seconds"].median().reset_index(
    name="med_ft")
valid = valid.merge(race_ft, on=["race_date", "race_number"], how="left")
valid["ft_resid"] = valid["finish_time_seconds"] - valid["med_ft"]

print(f"Analysis ready: {len(valid)} horse-runs\n")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 1: HOW SECTIONAL SHAPE CORRELATES WITH FINISHING POSITION
# ═══════════════════════════════════════════════════════════════════════════════
print("=" * 80)
print("PART 1: SECTIONAL SHAPE → FINISHING POSITION CORRELATION")
print("=" * 80)

for dist in [1200, 1400, 1600, 1800, 2000]:
    dv = valid[valid["distance"] == dist]
    if len(dv) < 100:
        continue
    # Correlation: SSI vs place
    rho_ssi, p_ssi = stats.spearmanr(dv["ssi"], dv["_place"])
    # Correlation: late_dev vs place
    rho_late, p_late = stats.spearmanr(dv["late_dev"], dv["_place"])
    # Correlation: early_dev vs place
    rho_early, p_early = stats.spearmanr(dv["early_dev"], dv["_place"])
    print(f"\n  {dist}m (n={len(dv)}):")
    print(f"    early_dev vs place:  rho={rho_early:+.3f}  p={p_early:.4f}")
    print(f"    late_dev  vs place:  rho={rho_late:+.3f}  p={p_late:.4f}")
    print(f"    SSI       vs place:  rho={rho_ssi:+.3f}  p={p_ssi:.4f}")

    # Winners vs rest
    winners = dv[dv["_place"] == 1]
    rest = dv[dv["_place"] > 3]
    if len(winners) >= 10:
        print(f"    Winners  → early_dev={winners['early_dev'].mean():+.3f}  "
              f"mid_dev={winners['mid_dev'].mean():+.3f}  "
              f"late_dev={winners['late_dev'].mean():+.3f}  "
              f"SSI={winners['ssi'].mean():+.3f}")
        print(f"    Also-ran → early_dev={rest['early_dev'].mean():+.3f}  "
              f"mid_dev={rest['mid_dev'].mean():+.3f}  "
              f"late_dev={rest['late_dev'].mean():+.3f}  "
              f"SSI={rest['ssi'].mean():+.3f}")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 2: HOW DOES DISTANCE CHANGE WHAT SECTIONAL SHAPE WINS?
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n\n{'=' * 80}")
print("PART 2: WINNING SECTIONAL SHAPE BY DISTANCE")
print("=" * 80)

print(f"\n  {'Distance':>8}  {'Win early_dev':>14}  {'Win late_dev':>13}  {'Win SSI':>10}  "
      f"{'Win DI':>8}  {'Rest DI':>8}  n_win")

for dist in [1000, 1200, 1400, 1600, 1800, 2000]:
    dv = valid[valid["distance"] == dist]
    winners = dv[dv["_place"] == 1]
    rest = dv[dv["_place"] > 3]
    if len(winners) < 10:
        continue
    print(f"  {dist}m  "
          f"{winners['early_dev'].mean():>+14.3f}  "
          f"{winners['late_dev'].mean():>+13.3f}  "
          f"{winners['ssi'].mean():>+10.3f}  "
          f"{winners['di'].mean():>8.4f}  "
          f"{rest['di'].mean():>8.4f}  "
          f"{len(winners)}")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 3: WEIGHT IMPACT ON FINAL SECTIONAL (your Q2)
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n\n{'=' * 80}")
print("PART 3: WEIGHT vs FINAL SECTIONAL (late_dev) BY DISTANCE")
print("=" * 80)

print("\n  Does carrying more weight make the final sectional worse?")
print(f"  {'Distance':>8}  {'Corr(wt,late_dev)':>18}  {'p-value':>10}  {'Light late':>11}  {'Heavy late':>11}  {'Diff':>8}")

for dist in [1200, 1400, 1600, 1800, 2000]:
    dv = valid[(valid["distance"] == dist) & (valid["actual_weight"].notna())]
    if len(dv) < 100:
        continue
    rho, pval = stats.spearmanr(dv["actual_weight"], dv["late_dev"])
    # Split by weight
    med_wt = dv["actual_weight"].median()
    light = dv[dv["actual_weight"] < med_wt - 2]
    heavy = dv[dv["actual_weight"] > med_wt + 2]
    if len(light) >= 20 and len(heavy) >= 20:
        diff = heavy["late_dev"].mean() - light["late_dev"].mean()
        print(f"  {dist}m  {rho:>+18.3f}  {pval:>10.4f}  "
              f"{light['late_dev'].mean():>+11.3f}  {heavy['late_dev'].mean():>+11.3f}  "
              f"{diff:>+8.3f}s")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 4: COURSE-SPECIFIC PATTERNS (ST vs HV)
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n\n{'=' * 80}")
print("PART 4: COURSE-SPECIFIC SECTIONAL PATTERNS (ST vs HV)")
print("=" * 80)

for venue_label, venue_filter in [("ST", "Sha Tin"), ("HV", "Happy Valley")]:
    v_data = valid[valid["race_track"].str.contains(venue_filter, na=False)]
    if len(v_data) == 0:
        continue
    print(f"\n  === {venue_label} ===")
    for dist in sorted(v_data["distance"].unique()):
        dist = int(dist)
        dv = v_data[v_data["distance"] == dist]
        winners = dv[dv["_place"] == 1]
        if len(winners) < 10:
            continue
        print(f"    {dist}m (n={len(dv)}, wins={len(winners)}): "
              f"Win SSI={winners['ssi'].mean():+.3f}  "
              f"Win early={winners['early_dev'].mean():+.3f}  "
              f"Win late={winners['late_dev'].mean():+.3f}")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 5: TRACK CONFIG (A, B, C, etc.) EFFECT ON SECTIONAL WINNERS
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n\n{'=' * 80}")
print("PART 5: TRACK CONFIG EFFECT ON WINNING SECTIONAL SHAPE (ST Turf)")
print("=" * 80)

st_turf = valid[(valid["race_track"].str.contains("Sha Tin", na=False)) &
                (valid["track_type"].str.contains("Turf", na=False, case=False))]

for dist in [1200, 1400, 1600]:
    dv = st_turf[st_turf["distance"] == dist]
    if len(dv) < 50:
        continue
    print(f"\n  {dist}m:")
    for config in sorted(dv["race_course"].dropna().unique()):
        cv = dv[dv["race_course"] == config]
        winners = cv[cv["_place"] == 1]
        if len(winners) < 5:
            continue
        print(f"    Config {config} (n={len(cv)}, wins={len(winners)}): "
              f"Win SSI={winners['ssi'].mean():+.3f}  "
              f"Win early={winners['early_dev'].mean():+.3f}  "
              f"Win late={winners['late_dev'].mean():+.3f}")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 6: REAL RACE EXAMPLES — recent races where sectional profile predicted outcome
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n\n{'=' * 80}")
print("PART 6: REAL RACE EXAMPLES FROM RECENT MEETINGS")
print("=" * 80)

recent = valid[valid["race_date"] >= "2026-03-01"].copy()
print(f"\n  Scanning {len(recent)} horse-runs from March 2026 onwards...\n")

# Find races where a strong-finisher (negative SSI) won from behind
print("  --- STRONG FINISHERS WHO WON (negative SSI, came from behind) ---")
n_ex = 0
for (dt, rn), grp in recent.groupby(["race_date", "race_number"]):
    if len(grp) < 8:
        continue
    winner = grp[grp["_place"] == 1]
    if len(winner) == 0:
        continue
    w = winner.iloc[0]
    if w["ssi"] >= 0:
        continue
    if w["late_dev"] >= -0.1:
        continue
    # Check there was a front-runner who faded
    faded = grp[(grp["early_dev"] < -0.1) & (grp["_place"] >= 6)]
    if len(faded) == 0:
        continue
    f = faded.iloc[0]
    dist = int(grp["distance"].iloc[0])
    rc = grp["race_course"].iloc[0]
    track = grp["race_track"].iloc[0]

    print(f"\n  {dt.date()} R{rn} — {dist}m {rc} ({track})")
    print(f"    WINNER: {w['horse_name']}")
    print(f"      early_dev={w['early_dev']:+.3f}  mid_dev={w['mid_dev']:+.3f}  "
          f"late_dev={w['late_dev']:+.3f}  SSI={w['ssi']:+.3f}")
    print(f"      → Started slower than field, finished faster → strong late surge")
    print(f"    FADED: {f['horse_name']} (finished {int(f['_place'])}th)")
    print(f"      early_dev={f['early_dev']:+.3f}  mid_dev={f['mid_dev']:+.3f}  "
          f"late_dev={f['late_dev']:+.3f}  SSI={f['ssi']:+.3f}")
    print(f"      → Started fast, couldn't sustain → front-loaded profile")

    n_ex += 1
    if n_ex >= 6:
        break

# Find races where an even-paced leader won
print(f"\n\n  --- LEADERS WHO WON WITH LOW DECELERATION (efficient energy use) ---")
n_ex2 = 0
for (dt, rn), grp in recent.groupby(["race_date", "race_number"]):
    if len(grp) < 8:
        continue
    winner = grp[grp["_place"] == 1]
    if len(winner) == 0:
        continue
    w = winner.iloc[0]
    if w["early_dev"] >= 0:
        continue  # wasn't leading
    if w["di"] >= 0.04:
        continue  # decelerated too much
    if w["late_dev"] >= 0.1:
        continue
    dist = int(grp["distance"].iloc[0])
    rc = grp["race_course"].iloc[0]
    track = grp["race_track"].iloc[0]

    print(f"\n  {dt.date()} R{rn} — {dist}m {rc} ({track})")
    print(f"    WINNER: {w['horse_name']}")
    print(f"      early_dev={w['early_dev']:+.3f}  mid_dev={w['mid_dev']:+.3f}  "
          f"late_dev={w['late_dev']:+.3f}  SSI={w['ssi']:+.3f}  DI={w['di']:+.4f}")
    print(f"      → Led early AND still finished strong → even-paced / efficient")
    # Show a closer who lost
    closers_lost = grp[(grp["early_dev"] > 0.2) & (grp["_place"] >= 4)]
    if len(closers_lost) > 0:
        cl = closers_lost.iloc[0]
        print(f"    STUCK: {cl['horse_name']} (finished {int(cl['_place'])}th)")
        print(f"      early_dev={cl['early_dev']:+.3f}  late_dev={cl['late_dev']:+.3f}  "
              f"SSI={cl['ssi']:+.3f}")
        print(f"      → Started slow, never got to the front despite finishing OK")

    n_ex2 += 1
    if n_ex2 >= 6:
        break


# ═══════════════════════════════════════════════════════════════════════════════
# PART 7: HORSE-LEVEL SECTIONAL PROFILES (aggregated across runs)
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n\n{'=' * 80}")
print("PART 7: BUILDING HORSE-LEVEL SECTIONAL PROFILES")
print("=" * 80)

# For each horse, compute average zone deviations across recent runs
horse_profiles = {}
for horse_name, grp in valid.groupby("horse_name"):
    grp_sorted = grp.sort_values("race_date", ascending=True)
    # Use up to last 6 runs
    recent_runs = grp_sorted.tail(6)
    if len(recent_runs) < 2:
        continue

    # Recency-weighted averages (lambda=0.85)
    lam = 0.85
    n = len(recent_runs)
    weights = np.array([lam ** (n - 1 - i) for i in range(n)])
    weights /= weights.sum()

    avg_early = float(np.dot(recent_runs["early_dev"].values, weights))
    avg_mid = float(np.dot(recent_runs["mid_dev"].values, weights))
    avg_late = float(np.dot(recent_runs["late_dev"].values, weights))
    avg_ssi = float(np.dot(recent_runs["ssi"].values, weights))
    avg_di = float(np.dot(recent_runs["di"].values, weights))

    # Consistency of late sectional
    late_std = float(recent_runs["late_dev"].std()) if len(recent_runs) > 1 else np.nan

    horse_profiles[horse_name] = {
        "n_runs": len(recent_runs),
        "avg_early_dev": round(avg_early, 4),
        "avg_mid_dev": round(avg_mid, 4),
        "avg_late_dev": round(avg_late, 4),
        "avg_ssi": round(avg_ssi, 4),
        "avg_di": round(avg_di, 4),
        "late_std": round(late_std, 4) if pd.notna(late_std) else None,
        "distances": list(recent_runs["distance"].unique()),
    }

print(f"\n  Built profiles for {len(horse_profiles)} horses")

# Classify into sectional types
def classify_sectional_type(profile):
    ssi = profile["avg_ssi"]
    late = profile["avg_late_dev"]
    early = profile["avg_early_dev"]
    if ssi < -0.15 and late < -0.05:
        return "Strong Finisher"
    elif ssi > 0.15 and early < -0.05:
        return "Front-Loaded"
    elif abs(ssi) <= 0.15 and abs(early) < 0.15:
        return "Even-Paced"
    elif early < -0.15:
        return "Speed Merchant"
    elif late > 0.15:
        return "Weak Finisher"
    else:
        return "Mixed"

type_counts = defaultdict(int)
for hp in horse_profiles.values():
    hp["sec_type"] = classify_sectional_type(hp)
    type_counts[hp["sec_type"]] += 1

print("\n  Sectional Type Distribution:")
for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
    print(f"    {t:20s}: {c}")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 8: DO SECTIONAL PROFILES PREDICT FUTURE SUCCESS? (the key question)
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n\n{'=' * 80}")
print("PART 8: PREDICTIVE VALUE — DOES HISTORICAL SECTIONAL PROFILE PREDICT OUTCOME?")
print("=" * 80)

# For each horse-run, compute their PRIOR sectional profile (from earlier runs only)
# Then correlate with THIS run's outcome
print("\n  Building prior-run sectional profiles for each horse-run...")

# Sort all valid runs
valid_sorted = valid.sort_values(["horse_name", "race_date"]).reset_index(drop=True)

# For each run, compute the horse's profile from all PRIOR runs
prior_early = []
prior_late = []
prior_ssi = []
has_prior = []

for i, row in valid_sorted.iterrows():
    horse = row["horse_name"]
    this_date = row["race_date"]
    # Get all prior runs for this horse
    prior_runs = valid_sorted[(valid_sorted["horse_name"] == horse) &
                               (valid_sorted["race_date"] < this_date)]
    if len(prior_runs) < 2:
        has_prior.append(False)
        prior_early.append(np.nan)
        prior_late.append(np.nan)
        prior_ssi.append(np.nan)
        continue
    has_prior.append(True)
    # Recency-weighted
    pr = prior_runs.tail(6)
    n = len(pr)
    wts = np.array([0.85 ** (n - 1 - j) for j in range(n)])
    wts /= wts.sum()
    prior_early.append(float(np.dot(pr["early_dev"].values, wts)))
    prior_late.append(float(np.dot(pr["late_dev"].values, wts)))
    prior_ssi.append(float(np.dot(pr["ssi"].values, wts)))

valid_sorted["prior_early"] = prior_early
valid_sorted["prior_late"] = prior_late
valid_sorted["prior_ssi"] = prior_ssi
valid_sorted["has_prior"] = has_prior

predictive = valid_sorted[valid_sorted["has_prior"]].copy()
print(f"  {len(predictive)} horse-runs with prior sectional profiles")

# Correlate prior_ssi with actual outcome
print("\n  Prior SSI (sectional shape from past runs) vs actual place:")
for dist in [1200, 1400, 1600, 1800, 2000]:
    dv = predictive[predictive["distance"] == dist]
    if len(dv) < 100:
        continue
    # Prior SSI vs actual place
    rho, pval = stats.spearmanr(dv["prior_ssi"], dv["_place"])
    # Prior late_dev vs actual place
    rho_l, pval_l = stats.spearmanr(dv["prior_late"], dv["_place"])
    print(f"    {dist}m (n={len(dv)}): prior_SSI vs place: rho={rho:+.3f} p={pval:.4f}  |  "
          f"prior_late vs place: rho={rho_l:+.3f} p={pval_l:.4f}")

# Does prior SSI help differently at different distances?
print("\n  CROSS-DISTANCE: Does a horse's sectional shape affect WHERE it should race?")
print("    (Do strong finishers do better when stepping up in distance?)")

# Look at horses who ran at 1200-1400m with known SSI, then ran at 1600+
steppers = predictive[(predictive["distance"] >= 1600) &
                       (predictive["prior_ssi"].notna())].copy()
steppers["prior_ssi_bin"] = pd.cut(steppers["prior_ssi"],
                                    bins=[-10, -0.2, 0.2, 10],
                                    labels=["StrongFinisher", "Even", "FrontLoaded"])
if len(steppers) >= 50:
    print(f"\n    Horses racing 1600m+ (n={len(steppers)}):")
    for stype in ["StrongFinisher", "Even", "FrontLoaded"]:
        subset = steppers[steppers["prior_ssi_bin"] == stype]
        if len(subset) >= 10:
            print(f"      {stype:16s}: mean_place={subset['_place'].mean():.2f}  "
                  f"win%={100*(subset['_place']==1).mean():.1f}%  "
                  f"top3%={100*(subset['_place']<=3).mean():.1f}%  "
                  f"n={len(subset)}")

steppers_short = predictive[(predictive["distance"] <= 1200) &
                             (predictive["prior_ssi"].notna())].copy()
steppers_short["prior_ssi_bin"] = pd.cut(steppers_short["prior_ssi"],
                                          bins=[-10, -0.2, 0.2, 10],
                                          labels=["StrongFinisher", "Even", "FrontLoaded"])
if len(steppers_short) >= 50:
    print(f"\n    Horses racing ≤1200m (n={len(steppers_short)}):")
    for stype in ["StrongFinisher", "Even", "FrontLoaded"]:
        subset = steppers_short[steppers_short["prior_ssi_bin"] == stype]
        if len(subset) >= 10:
            print(f"      {stype:16s}: mean_place={subset['_place'].mean():.2f}  "
                  f"win%={100*(subset['_place']==1).mean():.1f}%  "
                  f"top3%={100*(subset['_place']<=3).mean():.1f}%  "
                  f"n={len(subset)}")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 9: APPLY TO APRIL 12, 2026 RACE CARD
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n\n{'=' * 80}")
print("PART 9: APPLICATION TO 12 APRIL 2026 RACE CARD")
print("=" * 80)

# Load the race card
CARD_PATH = None
for f in os.listdir("."):
    if f.endswith(".xlsx") and "2026" in f and "race" in f.lower():
        CARD_PATH = f
        break

# Try to find the card
import glob
cards = glob.glob("*.xlsx")
race_cards = [c for c in cards if "1204" in c or "20261204" in c or "20260412" in c or "0412" in c]
if not race_cards:
    race_cards = [c for c in cards if "race" in c.lower() and "card" in c.lower()]
if not race_cards:
    race_cards = [c for c in cards if "2026" in c and c != "hkjc_results_updated.xlsx"]

print(f"\n  Available xlsx files: {cards}")
if race_cards:
    print(f"  Potential race cards: {race_cards}")

# Parse horse names from the race card if found
# If no card, use horses from the most recent meeting in DB
# and demonstrate with known horses
print("\n  Using horses likely racing on 12 April (from recent form)...")
print("  Showing sectional profiles for a sample of active horses:\n")

# Get horses from the last few meetings
last_meetings = valid[valid["race_date"] >= "2026-03-15"]
active_horses = last_meetings["horse_name"].unique()
print(f"  Active horses (raced since 15 March): {len(active_horses)}")

# Show top 20 most interesting profiles
interesting = []
for h in active_horses:
    if h in horse_profiles:
        p = horse_profiles[h]
        if p["n_runs"] >= 3:
            interesting.append((h, p))

# Sort by absolute SSI value (most extreme sectional shapes)
interesting.sort(key=lambda x: abs(x[1]["avg_ssi"]), reverse=True)

print(f"\n  {'Horse Name':<25s}  {'Type':>18s}  {'early_dev':>10}  {'mid_dev':>9}  "
      f"{'late_dev':>9}  {'SSI':>7}  {'DI':>7}  {'late_σ':>6}  n")
print("  " + "-" * 115)

for name, p in interesting[:30]:
    print(f"  {name:<25s}  {p['sec_type']:>18s}  "
          f"{p['avg_early_dev']:>+10.3f}  {p['avg_mid_dev']:>+9.3f}  "
          f"{p['avg_late_dev']:>+9.3f}  {p['avg_ssi']:>+7.3f}  "
          f"{p['avg_di']:>+7.4f}  "
          f"{p['late_std']:>6.3f}  {p['n_runs']}")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 10: DEMONSTRATE WITH SPECIFIC HORSE HISTORIES
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n\n{'=' * 80}")
print("PART 10: DETAILED SECTIONAL HISTORY FOR EXEMPLAR HORSES")
print("=" * 80)

# Pick a strong finisher, a front-loaded, and an even-paced horse from active set
exemplars = {"Strong Finisher": None, "Front-Loaded": None, "Even-Paced": None}
for name, p in interesting:
    t = p["sec_type"]
    if t in exemplars and exemplars[t] is None:
        exemplars[t] = name
    if all(v is not None for v in exemplars.values()):
        break

for sec_type, horse_name in exemplars.items():
    if horse_name is None:
        continue
    print(f"\n  === {horse_name} ({sec_type}) ===")
    h_runs = valid[valid["horse_name"] == horse_name].sort_values("race_date")
    for _, r in h_runs.iterrows():
        dist = int(r["distance"])
        rc = r["race_course"]
        place = int(r["_place"])
        secs = r["_sections"]
        per400 = r["_per400m"]
        print(f"    {r['race_date'].date()} {dist}m {rc}  "
              f"Place={place:>2d}  Wt={int(r['actual_weight'])}  "
              f"Sections: {[f'{s:.2f}' for s in secs]}")
        print(f"      per400m: {[f'{s:.2f}' for s in per400]}  "
              f"early_dev={r['early_dev']:+.3f}  mid_dev={r['mid_dev']:+.3f}  "
              f"late_dev={r['late_dev']:+.3f}  SSI={r['ssi']:+.3f}")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 11: SUMMARY STATISTICS
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n\n{'=' * 80}")
print("SUMMARY STATISTICS")
print("=" * 80)

print(f"\n  Total horse-runs analysed: {len(valid)}")
print(f"  Horse profiles built: {len(horse_profiles)}")
print(f"  Active horses (since Mar 15): {len(active_horses)}")

print("\n  Overall zone statistics (per-400m seconds):")
print(f"    Early: mean={valid['_early'].mean():.2f}  std={valid['_early'].std():.2f}")
print(f"    Mid:   mean={valid['_mid'].mean():.2f}  std={valid['_mid'].std():.2f}")
print(f"    Late:  mean={valid['_late'].mean():.2f}  std={valid['_late'].std():.2f}")

print("\n  Winner vs Field average SSI by distance bucket:")
for label, lo, hi in [("Sprint (1000-1200)", 1000, 1200),
                       ("Mile (1400-1600)", 1400, 1600),
                       ("Middle (1800-2000)", 1800, 2000)]:
    dv = valid[(valid["distance"] >= lo) & (valid["distance"] <= hi)]
    winners = dv[dv["_place"] == 1]
    if len(winners) >= 10:
        print(f"    {label}: Winner SSI={winners['ssi'].mean():+.3f}  "
              f"Field SSI={dv['ssi'].mean():+.3f}  "
              f"Diff={winners['ssi'].mean() - dv['ssi'].mean():+.3f}")

print("\n\nDone. See sectional_decomposition_findings.md for interpretation.")
