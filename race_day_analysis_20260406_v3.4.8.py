"""
Race Day Analysis + PDF Report — Sha Tin, 29 March 2026  (v3.4.8)
===============================================================================================
v3.4.7 fixes (evidence from March 15 pre-race domain review):
  AB. Class-relative ability: replace flat class-transition penalty with a system
      that computes the horse's margin of competitiveness in its old class (residual
      vs old-class ET) and scales the adjustment by the ET gap between old and new
      class.  Horses that dominated a lower class get a smaller penalty; horses that
      barely competed get a larger one.  Also supports class DROP (bonus) for horses
      like Elegant Life/Star Elegance/Joker Orbit who finished well after dropping.
  AC. Trajectory reliability gate: when a horse has ≤4 runs AND the residual σ
      exceeds 1.0s, suppress trajectory classification to "Insufficient" — the data
      is too volatile for reliable trend detection.  Fixes Turquoise Velocity
      (n=3, σ=1.2s) being marked Declining despite winning last start.
  AD. Draw offset field-size scaling: outside draws are disproportionately
      penalised in large fields (14 runners) vs small fields (8 runners).
      When a horse's draw > 70% of field size and field ≥12, amplify the
      draw offset by a field-size factor.  Fixes Gate 14 at 1400m C+3 in a
      14-runner field where +0.19s underestimates the real positional disadvantage
      (avg position 8.4 vs 6.0 for gate 2; win rate 6.2% vs 7.1%).
  AE. Ability-anchored projection: the horse's projection is now driven by its
      OWN contextualised historical times, not by the class expected time with a
      heavily-shrunk residual offset.  Each historical run's finish time is
      normalised for that race's conditions (class/weight/going/course via ET,
      race pace via RPI, finishing credit).  The recency-weighted average of
      these normalised residuals IS the horse's innate ability signal.  Adding
      it to today's ET translates proven form into today's conditions.  Crucially,
      NO shrinkage toward the class average is applied — the horse's data drives
      the projection entirely.  Uncertainty for thin data is handled via the
      existing uncertainty penalty (+0.1s/√n for n<4), not by pulling the
      residual toward zero.  Impact: thin-data horses like Turquoise Velocity
      (n=3, SF=0.27) now project based on their actual performance (-0.218s)
      instead of a shrunk residual (-0.022s).

v3.4.6 systematic fixes (evidence from March 15 pre-race review):
  X. Leader congestion: when ≥3 leaders contest, discount leader pace multiplier
     proportionally.  With 5 leaders the leader mult is halved, preventing the model
     from rewarding leaders in suicidal-pace scenarios (R9 Devas Twelve issue).
  Y. Class-transition penalty: when a horse's best recent class is lower than today's
     race class and the horse has ≥2 runs in that lower class, apply +0.08s penalty.
     Prevents C5 winners being projected too fast in C4 (Come Fast Fay Fay issue).
  Z. Outlier trimming for thin data: for horses with ≤5 runs, residuals >2.5σ from
     the median are capped at the 2.5σ boundary.  Prevents one catastrophic run from
     destroying ability estimates (Osi Honour, Turquoise Velocity issues).
  AA. Surface penalty flexibility: if the horse's most recent same-surface run resulted
      in a top-3 finish, halve the surface penalty.  One strong representative run
      should reduce surface uncertainty (Happy Universe issue).

v3.4.2 recalibrations (evidence-based corrections from 8-horse investigation):
  N. Shrinkage floor for n=1 disasters: when SF < 0.20 and raw residual > +0.20s,
     enforce minimum effective residual = raw_resid × 0.50.  Prevents dead-last
     single-run horses from appearing average (IMPRESSIVE CHAMP, KWAI CHUNG TALENTS).
  O. Trajectory bonus gating: trajectory bonus only applies when the resulting
     effective residual would be < +0.10s.  Prevents rewarding "terrible→bad"
     trajectories that are still clearly slower than expected (SIR CHARGE).
  P. Distance proximity tightened: DISTANCE_PROXIMITY_M reduced from 200→100m.
     When 100% of runs are at a different distance (even within old threshold),
     apply forced distance penalty.  Fixes LOVE TOGETHER 1000→1200m (60% inflated).
  Q. Last-start winner boost: when most recent run is a win (place=1),
     its recency weight is doubled.  Prevents adjacent disasters from cancelling
     a just-won signal (SOVEREIGN FUND).

v3.4 changes from v3.3 (trajectory bias recalibration):
  E. Trajectory detection: classify each horse as Improving/Declining/Stable based on
     last-2 runs mean vs prior-3 baseline + OLS trend slope confirmation.
     Minimum 3 runs required for classification.
  F. Trajectory bonus/penalty: Improving horses receive time bonus (faster projection),
     Declining horses receive time penalty (slower projection).
     Bonus: min(|delta|×0.5, 0.20s). Penalty: min(|delta|×0.4, 0.15s).
  G. Shrinkage gate override for improvers: if trajectory=Improving AND n≥3,
     use 50% recency + 50% shrunk instead of 100% shrunk (partial raw escape).
  H. Reduced uncertainty for strong debuts: n=1, raw_resid < -0.50s →
     penalty = 0.06/√n instead of 0.10/√n.
  I. AWT→Turf surface discount: when today's race is Turf, AWT historical runs
     receive reduced recency weight (×0.50). AWT form is referential, not directly
     transferable. Trajectory bonus/penalty halved when last-2 include cross-surface runs.
  J. Trajectory consistency check: if last-2 runs disagree in direction relative to
     baseline (one better, one worse), classify as Stable regardless of mean delta.
     Prevents single outlier runs from corrupting trajectory signal.
  K. HV↔ST venue discount: when today's meeting is at SHA TIN, Happy Valley historical
     runs receive reduced recency weight (×0.50). HV is a tight, short circuit; ST is
     wide and long — form doesn't transfer 1:1 (same principle as AWT→Turf).
     Trajectory bonus/penalty halved when last-2 include cross-venue runs.

Retained from v3.3:
  A. Recency decay on residual: λ=0.85 exponential decay
  B. Uncertainty penalty: +0.10s / √n_runs for horses with < 4 runs (modified by H)
  C. Pace multiplier gating: n_sectional_runs ≥ 3
  D. Consistency flagging: residual_std > 0.50s → "INCONSISTENT"

Retained from v3.2:
  - Draw offset: raw with hard cap ±0.25s, distance-conditional, min-n=15
  - Bayesian shrinkage blend: 80% raw / 20% shrunk (SF≥0.30, n≥3)
  - Empirical pace-style multiplier system (Pearson r=0.91)
  - Confidence-weighted win probabilities (SF<0.50 deflated)
  - Risk metric framework
"""

import re, textwrap, warnings, math
from pathlib import Path
from datetime import datetime
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE = Path(r"c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards")

# ── Per-meeting configuration (UPDATE THESE FOR EACH MEETING) ─────────────────
RACE_CARD    = BASE / "racecards" / "racecard_20260406.xlsx"
SHEET_NAME   = "All Races"                                          # scraped output sheet
USE_SCRAPED  = True                                                  # scraped race card format
EXP_TIME_REF = BASE / "expected_time_references_v3.xlsx"
ABILITY_FILE = BASE / "horse_ability_analysis_v3.xlsx"

OUT_PDF  = BASE / "reports" / "race_day_report_20260406_v3.4.8.pdf"
OUT_TEXT = BASE / "reports" / "race_day_analysis_20260406_v3.4.8.txt"
DB_FILE  = BASE / "hkjc_results_updated.xlsx"

MEETING_TITLE = "SHA TIN — MONDAY, 6 APRIL 2026"
MEETING_VENUE = "ST"

# ── Scratchings (race_number: [horse_name, ...]) ──────────────────────────────
SCRATCHINGS = {
}

# ── Standby Promotions (injected after card parse) ────────────────────────────
STANDBY_PROMOTIONS = {
}
TURF_GOING_ASSUMED = "Good"    # auto-set by orchestrator
AWT_GOING_ASSUMED  = "Good"    # auto-set by orchestrator

# ── v3.2 Shrinkage Downplay Parameters (reduced from v3.1) ────────────────────
# v3.2: increased raw weight from 0.70 → 0.80 and lowered SF threshold from 0.40 → 0.30
# Justification: over-shrinkage dampened genuine ability signals (diagnostic finding).
# Shrinkage now only dominates for horses with very thin data (SF < 0.30 or n < 3).
BLEND_RAW_WT    = 0.80     # v3.2: was 0.70 — raw residual weight increased
BLEND_SF_THRESH = 0.30     # v3.2: was 0.40 — lower bar for blending activation
BLEND_N_THRESH  = 3        # v3.2: was 4 — 3 runs sufficient for blend

# Win probability confidence deflation for low-SF horses
PROB_CONF_DEFLATE = True   # enable confidence-weighted win probabilities

# ── v3.3 Audit-Driven Correction Parameters ───────────────────────────────────
# A. Recency decay: weight_i = RECENCY_LAMBDA^(n - 1 - i),  most recent run = weight 1.0
RECENCY_LAMBDA = 0.85      # each prior run discounted by 15%

# B. Uncertainty penalty: added to projection when n_runs < UNCERTAINTY_N_THRESH
#    penalty = UNCERTAINTY_BASE / sqrt(n_runs)  (e.g., +0.10s for n=1)
UNCERTAINTY_BASE   = 0.10  # seconds
UNCERTAINTY_N_THRESH = 4   # horses with >= 4 runs get no penalty

# C. Pace multiplier gating: require this many sectional runs for style-based pace adj
STYLE_CONF_MIN_SECTIONAL = 3

# D. Consistency flagging threshold
CONSISTENCY_STD_THRESH = 0.50  # residual_std above this → flagged as inconsistent

# ── v3.4 Trajectory Detection Parameters ──────────────────────────────────────
# E. Trajectory detection: last-2 vs prior-3 mean comparison
TRAJ_IMPROVE_THRESH = 0.10   # seconds improvement needed to classify as Improving
TRAJ_DECLINE_THRESH = 0.10   # seconds decline needed to classify as Declining
TRAJ_MIN_RUNS       = 3      # minimum total runs for trajectory classification

# F. Trajectory bonus/penalty applied to effective residual
TRAJ_BONUS_CAP      = 0.20   # maximum trajectory bonus in seconds (negative = faster)
TRAJ_DECLINE_CAP    = 0.25   # v3.4.1: raised from 0.15 — 0.15 was too lenient for steep declines (e.g. WISDOM STAR Δ=+0.46)

# G. Shrinkage gate override for improving horses
#    v3.3: SF<0.30 or n<3 → 100% shrunk.  v3.4: if Improving + n≥3, partial raw blend.
TRAJ_SF_OVERRIDE_WT = 0.50   # raw weight when trajectory overrides shrinkage gate

# H. Reduced uncertainty penalty for strong debuts
STRONG_DEBUT_THRESH    = -0.50  # raw residual threshold for "strong debut"
UNCERTAINTY_BASE_V34   = 0.06   # reduced penalty for strong debuts (was 0.10)

# L. v3.4.1: Surface-aware uncertainty
#    When projecting Turf, count only Turf runs for shrinkage/uncertainty thresholds.
#    A horse with 5 AWT runs and 0 Turf runs should not get same confidence as 5 Turf runs.
SURFACE_UNCERTAINTY_BASE = 0.12  # penalty per 1/sqrt(n_same_surface) when n_surface < 4
SURFACE_UNCERTAINTY_THRESH = 4   # need this many same-surface runs to avoid penalty

# M. v3.4.1: Distance-aware uncertainty
#    When horse has few runs at today's distance (±DISTANCE_PROXIMITY_M), add uncertainty.
DISTANCE_UNCERTAINTY_BASE  = 0.08  # penalty per 1/sqrt(n_dist)
DISTANCE_UNCERTAINTY_THRESH = 3    # need this many nearby-distance runs to avoid penalty
DISTANCE_PROXIMITY_M       = 100   # v3.4.2 (P): tightened from 200→100m

# N. v3.4.2: Shrinkage floor for n=1 disasters
#    When SF < 0.20 and raw residual > +0.20s (slower than expected),
#    enforce minimum effective residual = raw × SHRINKAGE_FLOOR_FACTOR.
#    Prevents dead-last single-run horses from being shrunk to near-zero.
SHRINKAGE_FLOOR_SF_THRESH  = 0.20  # SF below this triggers the floor
SHRINKAGE_FLOOR_RAW_THRESH = 0.20  # raw residual above this (slower) triggers floor
SHRINKAGE_FLOOR_FACTOR     = 0.50  # minimum effective = raw × this factor

# O. v3.4.2: Trajectory bonus gating
#    Trajectory bonus only applied when resulting effective residual < this threshold.
#    Prevents improving from "terrible" to "bad" being rewarded.
TRAJ_BONUS_RESID_GATE = 0.10  # effective_resid must be below this for bonus to apply

# P. v3.4.2: Forced distance penalty for 100% mismatch
#    When ALL runs are at a different distance (even within old 200m window),
#    apply a forced penalty.  Addresses untested distance projection inflation.
FULL_DIST_MISMATCH_PEN = 0.12  # seconds added when 0 runs at exact distance

# Q. v3.4.2: Last-start winner boost
#    When most recent run is a win, its recency weight is multiplied by this.
#    Prevents adjacent disasters from cancelling a just-won signal.
LAST_WIN_BOOST = 2.0  # weight multiplier for most recent run if place=1

# R. v3.4.3: Race-pace normalization
#    For each historical run, subtract the median field residual of that same race.
#    This removes race-pace effects (tactical/slow races vs fast races) so that
#    residuals measure horse ABILITY relative to the field, not absolute speed.
#    Critical for Group races where tactical pacing makes absolute times unreliable.
#    Cache is populated lazily during compute_recency_residual calls.
_RACE_PACE_CACHE = {}  # (race_date, race_number) → median field residual

# S. v3.4.4: ST venue-wide pace offset
#    March 8 post-race: 10/11 ST races ran faster than predicted.
#    Mean pace error = -0.95s (excl R7 outlier: -0.73s).  Conservative -0.45s applied.
#    Applied in predict_race_pace_v3() when venue == "ST".

# T. v3.4.4: Class-tier temperature scaling (Boltzmann)
#    March 8 post-race: C4-C5 avg ρ=0.19 vs C1-C3 avg ρ=0.59.
#    For C4 races, multiply Boltzmann T by 1.30; for C5, by 1.50.
#    Flattens win probabilities in lower classes where model has less predictive power.

# U. v3.4.4: Draw offset cap overhaul
#    Previous caps: 1650m ±0.15s, 1800m ±0.12s — killed 72-75% of real draw effect.
#    Data shows: 1650m C+3 draw 9 raw=+0.44s (n=28), 1800m draw 4 raw=-0.24s (n=24).
#    New data-driven caps: 1650m ±0.40s, 1800m ±0.30s, global ±0.50s.
#    DRAW_MIN_N lowered from 15 to 10 (matching builder n≥10 threshold).

# V. v3.4.4: Distance-aware recency weighting
#    Runs at different distances are discounted in recency calculation.
#    Fixes: Highland Rahy (great 1650m form inflating 1800m projection),
#    Macanese Master (terrible 1200m/1400m runs drowning 1000m wins),
#    Winning Data (1200-1400m runs artificially boosting 1650m projection).
DIST_RECENCY_EXACT  = 1.0    # same distance
DIST_RECENCY_NEAR   = 0.70   # within ±100m
DIST_RECENCY_MED    = 0.40   # within ±200m
DIST_RECENCY_FAR    = 0.15   # beyond ±200m

# W. v3.4.4: Finishing-position credit
#    The model is time-based and doesn't inherently value winning.  A horse can
#    win a slow C5 race with a positive residual (slower than ET) yet clearly
#    demonstrated competitive ability.  This credit adjusts each run's residual
#    based on finishing position, rewarding horses that beat the field.
#    Applied AFTER race-pace normalization, BEFORE recency weighting.
#    Addresses: Macanese Master (2 recent wins, model ranks near last),
#    China Win (won twice over 1800m, ranked 7th).
POSITION_CREDIT = {
    1: -0.15,   # winner credit: ~0.6 lengths
    2: -0.08,   # runner-up
    3: -0.04,   # 3rd place
}

# I. AWT→Turf surface discount
#    When projecting Turf, AWT historical runs get reduced weight.
#    Rationale: AWT outperformance doesn't transfer 1:1 to Turf (empirical:
#    Vulcanus AWT avg=3.7 vs Turf avg=4.5; Armour War Eagle AWT avg=3.0 vs Turf avg=5.5)
AWT_TURF_DISCOUNT      = 0.50   # AWT run weight multiplier when today is Turf
AWT_TRAJ_HAIRCUT       = 0.50   # trajectory bonus/penalty multiplied by this if cross-surface

# K. HV↔ST venue discount
#    When projecting at ST, HV historical runs get reduced weight (and vice versa).
#    Rationale: Happy Valley is a tight, short circuit with sharp turns;
#    Sha Tin is wide, long circuit — form doesn't transfer 1:1.
HV_ST_DISCOUNT         = 0.50   # cross-venue run weight multiplier
VENUE_TRAJ_HAIRCUT     = 0.50   # trajectory bonus/penalty multiplied by this if cross-venue

# ── v3.4.5/v3.4.8 Draw Offset Logic ───────────────────────────────────────────
# Bayesian shrinkage: adjusted = (n / (n + k)) × raw_offset
#   - Thin data (small n) is partially trusted, proportionally shrunk toward 0
#   - Well-sampled draws (n >> k) pass through nearly unchanged
#   - k=8 balances noise suppression vs preserving real draw effects
# Global safety cap ±1.50s (sanity check only — real offsets expected up to ~0.8s)
# v3.4.8: Draw offsets now computed at runtime from DB using race-median method
#   (residual = FT - race_median) instead of pre-computed FT - expected_time.
#   The old method produced statistically insignificant and physically implausible
#   offsets (e.g. Draw 14 at 1200m Course A showed -0.20s benefit when D10-D13
#   all showed penalties). Race-median normalises within each race, removing
#   confounders like horse quality and condition mix.
DRAW_SHRINKAGE_K = 8           # Bayesian shrinkage strength for draw offsets
DRAW_MAX_OFFSET_GLOBAL = 1.50  # v3.4.5: safety cap only, effectively uncapped

# X. v3.4.6: Leader congestion adjustment
#    When ≥3 leaders contest, the pace multiplier for Leader style is discounted.
#    Rationale: the PACE_STYLE_MULTIPLIERS table was calibrated from typical fields
#    (1-2 leaders). When 3+ leaders race, they burn each other out — the historical
#    Leader-benefits-from-fast-pace signal reverses. Discount linearly:
#      3 leaders: mult moves 25% toward 1.0
#      4 leaders: 50% toward 1.0
#      5+ leaders: 75% toward 1.0
LEADER_CONGESTION_MIN = 3       # minimum leaders to trigger congestion discount
LEADER_CONGESTION_SCALE = 0.25  # discount per additional leader above CONGESTION_MIN-1
LEADER_CONGESTION_MAX_DISC = 0.75  # maximum discount factor (75% toward neutral)

# Y → AB. v3.4.7: Class-relative ability (replaces flat CLASS_TRANSITION_PEN)
#    Instead of a flat +0.08s penalty for class step-ups, compute the horse's
#    competitive margin in its old class (mean residual of recent runs in that class),
#    then scale by the ET gap between old and new class.
#    Step-up: penalty  = max(0, ET_gap - abs(old_class_margin))  when margin<0 (better in old class)
#             penalty  = ET_gap + old_class_margin             when margin>0 (weak in old class)
#    Step-down: bonus  = min(0, -(ET_gap × frac of old dominance))
#    Elegant Life/Star Elegance/Joker Orbit: dominated old class → small/no penalty.
#    Come Fast Fay Fay: barely won C5 → larger penalty stepping to C4.
CLASS_REL_MIN_RUNS     = 2     # minimum runs in the old class to trigger
CLASS_REL_ET_GAP_FLOOR = 0.10  # minimum ET gap to apply any adjustment (seconds)
CLASS_REL_STEP_UP_CAP  = 0.30  # maximum step-up penalty (seconds)
CLASS_REL_STEP_DN_CAP  = -0.20 # maximum step-down bonus (negative, seconds)
CLASS_REL_DOMINANCE_W  = 0.60  # weight applied to old-class margin for step-down bonus

# Z. v3.4.6: Outlier trimming for thin data
#    For horses with ≤ this many runs, cap outlier residuals at median ± Z_TRIM σ.
#    Prevents one catastrophic run from destroying projections
#    (e.g. Osi Honour: 1 run at 105.72s crushing 98.12s projection).
OUTLIER_TRIM_MAX_RUNS = 5      # only trim for horses with ≤ 5 runs
OUTLIER_TRIM_Z = 2.5           # z-score boundary for trimming

# AA. v3.4.6: Surface penalty flexibility
#     If the horse's most recent same-surface run was a top-3 finish,
#     halve the surface penalty. One strong representative run reduces
#     surface uncertainty (Happy Universe: P2 on AWT from bad draw).
SURFACE_PEN_STRONG_DISCOUNT = 0.50  # multiply surface penalty by this if recent top-3

# AC. v3.4.7: Trajectory reliability gate
#     When a horse has ≤ this many runs AND residual std > threshold,
#     the trajectory signal is too noisy to trust.  Suppress to "Insufficient".
#     Fixes Turquoise Velocity: n=3, σ=0.75s → "Declining" when last run was a WIN.
TRAJ_RELIABILITY_MAX_N  = 4    # for n ≤ this, apply the reliability check
TRAJ_RELIABILITY_STD    = 0.70 # residual std must exceed this to suppress

# AD. v3.4.7: Draw offset field-size scaling
#     Outer draws are disproportionately penalised in large fields.
#     At 1400m C+3 Gate 14: avg pos 8.4 vs Gate 2: avg pos 6.0, win rate 6.2% vs 7.1%.
#     When draw is in the outer 30% of the field and field ≥ DRAW_FIELD_MIN,
#     amplify the draw offset.
DRAW_FIELD_MIN          = 12   # minimum field size to trigger amplification
DRAW_FIELD_OUTER_FRAC   = 0.70 # draws above this fraction get amplified
DRAW_FIELD_SCALE        = 2.0  # amplification per unit of outer excess

# ── v3.3 Going-code mapping (DB abbreviations → expected-time reference labels) ─
GOING_CODE_MAP = {
    "GF": "Good-to-Firm", "G": "Good", "GY": "Good-to-Yielding",
    "Y": "Yielding", "WF": "AWT-Wet-Fast", "WS": "AWT-Wet-Slow",
    "SE": "AWT-Standard", "SEALED": "AWT-Standard",
    "GOOD TO FIRM FAST": "Good-to-Firm",
}

# Pace adjustment for projected final sectional (seconds added/subtracted)
# Fast pace → leaders slow, closers gain; overall final secs tend to be slower
# Slow pace → more sprint finish, final secs faster for closers
PACE_FINAL_SEC_ADJ = {
    "Fast":   {"Leader": +0.15, "Front": +0.08, "Midfield": -0.02, "Closer": -0.06, "Unknown": 0.0},
    "Neutral":{"Leader":  0.00, "Front":  0.00, "Midfield":  0.00, "Closer":  0.00, "Unknown": 0.0},
    "Slow":   {"Leader": -0.05, "Front": -0.03, "Midfield": +0.02, "Closer": +0.05, "Unknown": 0.0},
}


# ══════════════════════════════════════════════════════════════════════════════
# 1. Parse race card  (Sheet6 layout — ST A-course format)
# ══════════════════════════════════════════════════════════════════════════════

def parse_race_card(path):
    raw = pd.read_excel(path, sheet_name=SHEET_NAME, header=None)
    races = []
    current_race = None

    for idx, row in raw.iterrows():
        cell1 = str(row[1]) if pd.notna(row[1]) else ""

        # ── Race header ──
        if cell1.startswith("Race "):
            if current_race is not None:
                races.append(current_race)

            lines = cell1.split("\n")
            m = re.match(r"Race\s+(\d+)\s*-\s*(.+)", lines[0])
            race_num  = int(m.group(1)) if m else 0
            race_name = m.group(2).strip() if m else lines[0]

            track_line = lines[2].strip() if len(lines) > 2 else ""
            is_awt = "All Weather" in track_line

            dist_m = re.search(r"(\d{3,4})M", track_line)
            distance = int(dist_m.group(1)) if dist_m else 0

            if is_awt:
                race_course = "AWT"
                track_type  = "All Weather Track"
            else:
                course_m = re.search(r'"([A-C](?:\+\d)?)"', track_line)
                race_course = course_m.group(1) if course_m else "A"
                track_type  = "Turf"

            prize_line = lines[3].strip() if len(lines) > 3 else ""
            class_m = re.search(r"Class\s+(\d)", prize_line)
            race_class = int(class_m.group(1)) if class_m else 0
            group_m = re.search(r"Group\s+(One|Two|Three)", prize_line, re.I)
            if group_m:
                race_class = 0

            rating_m = re.search(r"Rating:\s*([\d]+)-([\d]+)", prize_line)
            rating_range = f"{rating_m.group(1)}-{rating_m.group(2)}" if rating_m else ""

            current_race = {
                "race_number": race_num,
                "race_name":   race_name,
                "distance":    distance,
                "track_type":  track_type,
                "race_course": race_course,
                "is_awt":      is_awt,
                "race_class":  race_class,
                "track_line":  track_line,
                "prize_line":  prize_line,
                "rating_range": rating_range,
                "horses":      [],
            }
            continue

        if cell1.strip() == "Horse No.":
            continue

        # ── Horse row ──
        horse_no = row[1]
        if current_race is not None and pd.notna(horse_no):
            try:
                horse_no_int = int(float(str(horse_no)))
            except (ValueError, TypeError):
                continue

            weight_raw = str(row[4]).strip() if pd.notna(row[4]) else "0"
            try: weight = int(weight_raw)
            except ValueError: weight = 0

            draw_raw = str(row[6]).strip() if pd.notna(row[6]) else ""
            try: draw = int(draw_raw)
            except ValueError: draw = None

            # Column mapping for 15 Mar form (standard ST layout):
            # Mar 18 HV layout: col[2]=Horse, col[3]=Brand, col[7]=Trainer, col[8]=Rtg
            horse_name_raw = str(row[2]).strip() if pd.notna(row[2]) else ""
            brand_no_raw   = str(row[3]).strip() if pd.notna(row[3]) else ""
            trainer_raw    = str(row[7]).strip() if pd.notna(row[7]) else ""
            rating_raw_col = str(row[8]).strip() if pd.notna(row[8]) else ""
            try: rating_parsed = int(float(rating_raw_col))
            except (ValueError, TypeError): rating_parsed = None

            current_race["horses"].append({
                "horse_no":    horse_no_int,
                "horse_name":  horse_name_raw,
                "brand_no":    brand_no_raw,
                "weight":      weight,
                "draw":        draw,
                "rating":      rating_parsed,
                "jockey":      str(row[5]).strip() if pd.notna(row[5]) else "",
                "trainer":     trainer_raw,
            })

    if current_race is not None:
        races.append(current_race)

    # Apply scratchings
    for race in races:
        scratched = SCRATCHINGS.get(race["race_number"], [])
        if scratched:
            before = len(race["horses"])
            race["horses"] = [h for h in race["horses"]
                              if h["horse_name"] not in scratched]
            after = len(race["horses"])
            if before != after:
                print(f"  ✂ R{race['race_number']}: scratched {scratched} "
                      f"({before}→{after} runners)")

    # Inject standby promotions
    for race in races:
        promo = STANDBY_PROMOTIONS.get(race["race_number"])
        if promo:
            # Check not already present
            existing = {h["horse_name"] for h in race["horses"]}
            if promo["horse_name"] not in existing:
                race["horses"].append(promo)
                print(f"  ✚ R{race['race_number']}: promoted standby {promo['horse_name']} "
                      f"(#{promo['horse_no']}, {promo['weight']}lbs, {promo['jockey']})")

    return races


def parse_scraped_race_card(path):
    """Parse Excel output from scrape_hkjc_racecard.py into the same structure
    as parse_race_card().

    Reads the 'All Races' sheet, filters out stand-by/reserve horses
    (is_standby==True or jockey is NaN), and builds per-race dicts.

    Weight handling:
      - Declared weight   = HKJC "Wt." column (handicap allocation)
      - Apprentice claim  = extracted from jockey name, e.g. "H Y Yuen (-10)" → -10
      - Jockey overweight = HKJC "Over Wt." column (rare positive addition)
      - Actual carrying   = declared + claim (negative) + overweight (positive)
      horse["weight"] is set to actual carrying weight for correct ET-band lookup.
    """
    df = pd.read_excel(path, sheet_name="All Races")

    # Filter out reserves: standby flag OR no jockey assigned
    runners = df[
        (df["is_standby"] != True) &
        (df["jockey"].notna()) &
        (df["jockey"].astype(str).str.strip() != "")
    ].copy()

    races = []
    claim_log = []

    for rn in sorted(runners["race_number"].dropna().unique()):
        rdf = runners[runners["race_number"] == rn]
        first = rdf.iloc[0]

        distance = int(first["distance"]) if pd.notna(first.get("distance")) else 0
        surface  = str(first.get("surface", ""))
        is_awt   = "All Weather" in surface
        race_course = str(first.get("race_course", "AWT" if is_awt else "A"))
        track_type  = "All Weather Track" if is_awt else "Turf"

        rc = first.get("race_class", 0)
        try:
            race_class = int(rc)
        except (ValueError, TypeError):
            race_class = 0

        rating_range = str(first.get("rating_range", "")) if pd.notna(first.get("rating_range")) else ""
        prize_line = str(first.get("prize", "")) if pd.notna(first.get("prize")) else ""
        race_name = str(first.get("race_name", "")) if pd.notna(first.get("race_name")) else ""

        horses = []
        for _, row in rdf.iterrows():
            try:
                hno = int(row["horse_no"])
            except (ValueError, TypeError):
                continue

            try:
                declared_wt = int(row["weight"]) if pd.notna(row.get("weight")) else 0
            except (ValueError, TypeError):
                declared_wt = 0

            draw_val = row.get("draw")
            try:
                draw = int(draw_val) if pd.notna(draw_val) else None
            except (ValueError, TypeError):
                draw = None

            rtg_val = row.get("rating")
            try:
                rating = int(rtg_val) if pd.notna(rtg_val) else None
            except (ValueError, TypeError):
                rating = None

            jockey_raw = str(row.get("jockey", "")).strip()

            # Apprentice claim: negative number in parens at end of jockey name
            claim = 0
            claim_m = re.search(r'\((-\d+)\)\s*$', jockey_raw)
            if claim_m:
                claim = int(claim_m.group(1))

            # Jockey overweight: HKJC "Over Wt." column (positive lbs)
            ow_raw = str(row.get("overweight", "")).strip() if pd.notna(row.get("overweight")) else ""
            try:
                overweight_val = int(ow_raw) if ow_raw and ow_raw not in ("-", "") else 0
            except (ValueError, TypeError):
                overweight_val = 0

            actual_wt = declared_wt + claim + overweight_val
            jockey_clean = re.sub(r"\s*\([^)]*\)\s*$", "", jockey_raw).strip()

            if claim != 0 or overweight_val != 0:
                claim_log.append((int(rn), str(row.get("horse_name", "")).strip(),
                                  declared_wt, claim, overweight_val, actual_wt,
                                  jockey_clean))

            horses.append({
                "horse_no":        hno,
                "horse_name":      str(row.get("horse_name", "")).strip(),
                "brand_no":        str(row.get("brand_no", "")).strip() if pd.notna(row.get("brand_no")) else "",
                "weight":          actual_wt,
                "declared_weight": declared_wt,
                "claim":           claim,
                "overweight":      overweight_val,
                "draw":            draw,
                "rating":          rating,
                "jockey":          jockey_clean,
                "trainer":         str(row.get("trainer", "")).strip() if pd.notna(row.get("trainer")) else "",
            })

        track_line = f"{race_course}, {distance}M" if race_course else f"{distance}M"

        races.append({
            "race_number": int(rn),
            "race_name":   race_name,
            "distance":    distance,
            "track_type":  track_type,
            "race_course": race_course,
            "is_awt":      is_awt,
            "race_class":  race_class,
            "track_line":  track_line,
            "prize_line":  prize_line,
            "rating_range": rating_range,
            "horses":      horses,
        })

    if claim_log:
        print("  ── Apprentice claims / overweights applied ──")
        for rn, name, decl, clm, ow, actual, jock in claim_log:
            parts = []
            if clm != 0:
                parts.append(f"claim {clm:+d}")
            if ow != 0:
                parts.append(f"overweight +{ow}")
            adj_str = ", ".join(parts)
            print(f"    R{rn}  {name:<22s}  {jock:<14s}  "
                  f"{decl}lbs declared  [{adj_str}]  → {actual}lbs actual")
    else:
        print("  No apprentice claims or overweights found.")

    # Apply scratchings
    for race in races:
        scratched = SCRATCHINGS.get(race["race_number"], [])
        if scratched:
            before = len(race["horses"])
            race["horses"] = [h for h in race["horses"]
                              if h["horse_name"] not in scratched]
            after = len(race["horses"])
            if before != after:
                print(f"  ✂ R{race['race_number']}: scratched {scratched} "
                      f"({before}→{after} runners)")

    # Inject standby promotions
    for race in races:
        promo = STANDBY_PROMOTIONS.get(race["race_number"])
        if promo:
            existing = {h["horse_name"] for h in race["horses"]}
            if promo["horse_name"] not in existing:
                race["horses"].append(promo)
                print(f"  ✚ R{race['race_number']}: promoted standby {promo['horse_name']} "
                      f"(#{promo['horse_no']}, {promo['weight']}lbs, {promo['jockey']})")

    return races


# ══════════════════════════════════════════════════════════════════════════════
# 2. Load v3 references
# ══════════════════════════════════════════════════════════════════════════════

def load_references_v3():
    class_fine = pd.read_excel(EXP_TIME_REF, sheet_name=0)
    fine       = pd.read_excel(EXP_TIME_REF, sheet_name=1)
    coarse     = pd.read_excel(EXP_TIME_REF, sheet_name=2)
    ultra      = pd.read_excel(EXP_TIME_REF, sheet_name=3)
    try:
        abilities  = pd.read_excel(ABILITY_FILE, sheet_name="Horse Ability v3")
    except PermissionError:
        import shutil, tempfile
        tmp = Path(tempfile.gettempdir()) / ABILITY_FILE.name
        if not tmp.exists():
            shutil.copy2(ABILITY_FILE, tmp)
        print(f"  ⚠ OneDrive lock on {ABILITY_FILE.name}, reading from {tmp}")
        abilities = pd.read_excel(tmp, sheet_name="Horse Ability v3")

    # v3.4.8: Compute ALL draw offsets from DB using race-median method
    # (replaces pre-computed FT - expected_time offsets which were flawed)
    draw_off = pd.DataFrame()
    db_path = DB_FILE
    if db_path.exists():
        db_raw = pd.read_excel(db_path)
        db_raw["draw_num"] = pd.to_numeric(db_raw["draw"], errors="coerce")
        db_raw = db_raw[db_raw["draw_num"].notna()
                        & db_raw["finish_time_seconds"].notna()
                        & (db_raw["finish_time_seconds"] > 0)].copy()
        # Unify course label: Turf uses race_course; AWT gets "AWT"
        is_awt = db_raw["track_type"].str.contains("All Weather", case=False, na=False)
        db_raw["draw_course"] = db_raw["race_course"]
        db_raw.loc[is_awt, "draw_course"] = "AWT"

        all_rows = []
        for (dist, course), subset in db_raw.groupby(["distance", "draw_course"]):
            # Per-race median finish time → residual = ft - race_median
            race_med = subset.groupby(["race_date", "race_number"])["finish_time_seconds"].median()
            subset = subset.merge(race_med.rename("race_median"),
                                  on=["race_date", "race_number"])
            subset["resid"] = subset["finish_time_seconds"] - subset["race_median"]
            overall_mean = subset["resid"].mean()
            for dr_num, grp in subset.groupby("draw_num"):
                n = len(grp)
                if n >= 5:  # minimum sample
                    raw_off = grp["resid"].mean() - overall_mean
                    all_rows.append({
                        "distance": int(dist),
                        "race_course": course,
                        "draw": int(dr_num),
                        "mean_resid": round(grp["resid"].mean(), 6),
                        "n": n,
                        "overall_mean": round(overall_mean, 6),
                        "draw_offset": round(raw_off, 6),
                    })
        if all_rows:
            draw_off = pd.DataFrame(all_rows)
            turf_n = len(draw_off[draw_off["race_course"] != "AWT"])
            awt_n = len(draw_off[draw_off["race_course"] == "AWT"])
            print(f"  Draw offsets (race-median): {turf_n} turf + {awt_n} AWT entries")

    return class_fine, fine, coarse, ultra, draw_off, abilities


def weight_band(w):
    if w <= 115: return "105-115"
    if w <= 120: return "116-120"
    if w <= 125: return "121-125"
    if w <= 130: return "126-130"
    return "131-135"


def class_band(c):
    if c <= 0: return "Group/Other"
    if c <= 2: return "C1-C2"
    if c == 3: return "C3"
    if c == 4: return "C4"
    return "C5"


# ══════════════════════════════════════════════════════════════════════════════
# 3. Expected-time lookup (multi-tier)
# ══════════════════════════════════════════════════════════════════════════════

def lookup_expected_time(class_fine, fine, coarse, ultra,
                         distance, going, wband, course, track_type, cband):
    MIN_N = 5
    for df_ref, cols, tier_name in [
        (class_fine, {"distance": distance, "going_group": going,
                      "weight_band": wband, "race_course": course,
                      "class_band": cband}, "class_fine"),
        (fine, {"distance": distance, "going_group": going,
                "weight_band": wband, "race_course": course}, "fine"),
        (coarse, {"distance": distance, "going_group": going,
                  "weight_band": wband, "track_type": track_type}, "coarse"),
        (ultra, {"distance": distance, "going_group": going,
                 "track_type": track_type}, "ultra"),
    ]:
        mask = pd.Series(True, index=df_ref.index)
        for col, val in cols.items():
            mask &= (df_ref[col] == val)
        matched = df_ref.loc[mask]
        if len(matched) and matched.iloc[0].get("sample_size", 0) >= MIN_N:
            r = matched.iloc[0]
            return r["expected_time"], int(r["sample_size"]), r.get("std_time", np.nan), tier_name
    return np.nan, 0, np.nan, "none"


def get_draw_offset(draw_off, distance, course, draw, field_size=None):
    """v3.4.7: Bayesian-shrunk draw offset with field-size scaling.
    
    Base: adjusted = (n / (n + k)) × raw_offset  (Bayesian shrinkage)
    
    v3.4.7 (AD): When field_size ≥ DRAW_FIELD_MIN and draw is in the outer
    fraction (> field_size × DRAW_FIELD_OUTER_FRAC), amplify the offset:
      position_ratio = draw / field_size
      scale = 1.0 + (position_ratio - DRAW_FIELD_OUTER_FRAC) × DRAW_FIELD_SCALE
    
    Example: Gate 14 in 14-runner race at 1400m C+3:
      ratio = 14/14 = 1.0, excess = 1.0 - 0.70 = 0.30
      scale = 1.0 + 0.30 × 2.0 = 1.60
      offset: +0.193 × 1.60 = +0.309s (was +0.193s)
    """
    if draw is None or pd.isna(draw):
        return 0.0, False
    m = ((draw_off["distance"] == distance) &
         (draw_off["race_course"] == course) &
         (draw_off["draw"] == draw))
    rows = draw_off.loc[m]
    if len(rows) > 0:
        raw_offset = rows.iloc[0]["draw_offset"]
        n_obs = int(rows.iloc[0].get("n", 1))
        
        # Bayesian shrinkage toward zero
        shrinkage = n_obs / (n_obs + DRAW_SHRINKAGE_K)
        adjusted = shrinkage * raw_offset
        
        # v3.4.7 (AD): Field-size scaling for outer draws
        if field_size is not None and field_size >= DRAW_FIELD_MIN:
            draw_num = pd.to_numeric(draw, errors='coerce')
            if pd.notna(draw_num) and draw_num > 0:
                position_ratio = draw_num / field_size
                if position_ratio > DRAW_FIELD_OUTER_FRAC:
                    excess = position_ratio - DRAW_FIELD_OUTER_FRAC
                    scale = 1.0 + excess * DRAW_FIELD_SCALE
                    adjusted *= scale
        
        # Global safety cap
        adjusted = max(-DRAW_MAX_OFFSET_GLOBAL, min(DRAW_MAX_OFFSET_GLOBAL, adjusted))
        
        return round(adjusted, 3), True
    return 0.0, False


# ══════════════════════════════════════════════════════════════════════════════
# 4. v3.2 Pace prediction — empirical model
# ══════════════════════════════════════════════════════════════════════════════

# ── Empirical pace-style multiplier tables (from 16,096-record analysis) ──────
# Multiplier = IV(pace,style) / IV(marginal,style), shrunk toward 1.0
# Source: pace_model_revision.py, validated: Pearson r=0.91, cross-val confirmed
PACE_STYLE_MULTIPLIERS = {
    (1000, "Fast", "Leader"): 0.662, (1000, "Fast", "On-Pace"): 1.392,
    (1000, "Fast", "Midfield"): 1.769, (1000, "Fast", "Closer"): 0.194,
    (1000, "Slightly Fast", "Leader"): 0.731, (1000, "Slightly Fast", "On-Pace"): 0.890,
    (1000, "Slightly Fast", "Midfield"): 1.509, (1000, "Slightly Fast", "Closer"): 1.899,
    (1000, "Normal", "Leader"): 1.040, (1000, "Normal", "On-Pace"): 0.942,
    (1000, "Normal", "Midfield"): 1.309, (1000, "Normal", "Closer"): 0.601,
    (1000, "Slightly Slow", "Leader"): 1.404, (1000, "Slightly Slow", "On-Pace"): 0.619,
    (1000, "Slightly Slow", "Midfield"): 0.293, (1000, "Slightly Slow", "Closer"): 1.282,
    (1000, "Slow", "Leader"): 1.171, (1000, "Slow", "On-Pace"): 1.050,
    (1000, "Slow", "Midfield"): 0.162, (1000, "Slow", "Closer"): 1.188,
    (1000, "Very Slow", "Leader"): 1.443, (1000, "Very Slow", "On-Pace"): 0.761,
    (1000, "Very Slow", "Midfield"): 0.345, (1000, "Very Slow", "Closer"): 0.322,
    (1200, "Very Fast", "Leader"): 0.666, (1200, "Very Fast", "On-Pace"): 1.416,
    (1200, "Very Fast", "Midfield"): 0.724, (1200, "Very Fast", "Closer"): 1.675,
    (1200, "Fast", "Leader"): 0.991, (1200, "Fast", "On-Pace"): 0.959,
    (1200, "Fast", "Midfield"): 1.202, (1200, "Fast", "Closer"): 0.783,
    (1200, "Slightly Fast", "Leader"): 0.929, (1200, "Slightly Fast", "On-Pace"): 0.931,
    (1200, "Slightly Fast", "Midfield"): 1.100, (1200, "Slightly Fast", "Closer"): 1.235,
    (1200, "Normal", "Leader"): 0.942, (1200, "Normal", "On-Pace"): 0.806,
    (1200, "Normal", "Midfield"): 1.242, (1200, "Normal", "Closer"): 1.574,
    (1200, "Slightly Slow", "Leader"): 1.014, (1200, "Slightly Slow", "On-Pace"): 1.299,
    (1200, "Slightly Slow", "Midfield"): 0.559, (1200, "Slightly Slow", "Closer"): 0.709,
    (1200, "Slow", "Leader"): 1.191, (1200, "Slow", "On-Pace"): 0.952,
    (1200, "Slow", "Midfield"): 0.897, (1200, "Slow", "Closer"): 0.571,
    (1200, "Very Slow", "Leader"): 1.213, (1200, "Very Slow", "On-Pace"): 1.022,
    (1200, "Very Slow", "Midfield"): 0.912, (1200, "Very Slow", "Closer"): 0.140,
    (1400, "Very Fast", "Leader"): 0.629, (1400, "Very Fast", "On-Pace"): 0.521,
    (1400, "Very Fast", "Midfield"): 2.188, (1400, "Very Fast", "Closer"): 1.193,
    (1400, "Fast", "Leader"): 1.404, (1400, "Fast", "On-Pace"): 0.481,
    (1400, "Fast", "Midfield"): 1.455, (1400, "Fast", "Closer"): 0.979,
    (1400, "Slightly Fast", "Leader"): 0.378, (1400, "Slightly Fast", "On-Pace"): 1.294,
    (1400, "Slightly Fast", "Midfield"): 0.800, (1400, "Slightly Fast", "Closer"): 1.890,
    (1400, "Normal", "Leader"): 1.158, (1400, "Normal", "On-Pace"): 0.720,
    (1400, "Normal", "Midfield"): 1.264, (1400, "Normal", "Closer"): 0.916,
    (1400, "Slightly Slow", "Leader"): 0.743, (1400, "Slightly Slow", "On-Pace"): 1.489,
    (1400, "Slightly Slow", "Midfield"): 0.680, (1400, "Slightly Slow", "Closer"): 0.653,
    (1400, "Slow", "Leader"): 1.075, (1400, "Slow", "On-Pace"): 1.579,
    (1400, "Slow", "Midfield"): 0.500, (1400, "Slow", "Closer"): 0.160,
    (1400, "Very Slow", "Leader"): 1.703, (1400, "Very Slow", "On-Pace"): 0.995,
    (1400, "Very Slow", "Midfield"): 0.207, (1400, "Very Slow", "Closer"): 0.868,
    (1600, "Fast", "Leader"): 0.609, (1600, "Fast", "On-Pace"): 1.025,
    (1600, "Fast", "Midfield"): 1.378, (1600, "Fast", "Closer"): 1.112,
    (1600, "Slightly Fast", "Leader"): 0.524, (1600, "Slightly Fast", "On-Pace"): 1.082,
    (1600, "Slightly Fast", "Midfield"): 1.150, (1600, "Slightly Fast", "Closer"): 1.735,
    (1600, "Normal", "Leader"): 1.151, (1600, "Normal", "On-Pace"): 0.952,
    (1600, "Normal", "Midfield"): 0.989, (1600, "Normal", "Closer"): 0.816,
    (1600, "Slightly Slow", "Leader"): 1.284, (1600, "Slightly Slow", "On-Pace"): 1.110,
    (1600, "Slightly Slow", "Midfield"): 0.520, (1600, "Slightly Slow", "Closer"): 0.465,
    (1600, "Slow", "Leader"): 1.076, (1600, "Slow", "On-Pace"): 0.882,
    (1600, "Slow", "Midfield"): 1.090, (1600, "Slow", "Closer"): 1.026,
    (1600, "Very Slow", "Leader"): 1.754, (1600, "Very Slow", "On-Pace"): 1.049,
    (1600, "Very Slow", "Midfield"): 0.358, (1600, "Very Slow", "Closer"): 0.312,
    (1650, "Fast", "Leader"): 0.599, (1650, "Fast", "On-Pace"): 1.335,
    (1650, "Fast", "Midfield"): 0.769, (1650, "Fast", "Closer"): 1.049,
    (1650, "Slightly Fast", "Leader"): 0.940, (1650, "Slightly Fast", "On-Pace"): 0.866,
    (1650, "Slightly Fast", "Midfield"): 1.129, (1650, "Slightly Fast", "Closer"): 1.075,
    (1650, "Normal", "Leader"): 1.069, (1650, "Normal", "On-Pace"): 1.023,
    (1650, "Normal", "Midfield"): 1.021, (1650, "Normal", "Closer"): 1.053,
    (1650, "Slightly Slow", "Leader"): 0.610, (1650, "Slightly Slow", "On-Pace"): 0.898,
    (1650, "Slightly Slow", "Midfield"): 1.704, (1650, "Slightly Slow", "Closer"): 1.088,
    (1650, "Slow", "Leader"): 1.072, (1650, "Slow", "On-Pace"): 0.996,
    (1650, "Slow", "Midfield"): 0.825, (1650, "Slow", "Closer"): 1.102,
    (1650, "Very Slow", "Leader"): 1.640, (1650, "Very Slow", "On-Pace"): 0.999,
    (1650, "Very Slow", "Midfield"): 0.397, (1650, "Very Slow", "Closer"): 0.531,
    (1800, "Fast", "Leader"): 1.054, (1800, "Fast", "On-Pace"): 0.293,
    (1800, "Fast", "Midfield"): 1.535, (1800, "Fast", "Closer"): 0.293,
    (1800, "Slightly Fast", "Leader"): 0.942, (1800, "Slightly Fast", "On-Pace"): 1.656,
    (1800, "Slightly Fast", "Midfield"): 0.757, (1800, "Slightly Fast", "Closer"): 0.881,
    (1800, "Normal", "Leader"): 1.142, (1800, "Normal", "On-Pace"): 1.005,
    (1800, "Normal", "Midfield"): 0.748, (1800, "Normal", "Closer"): 1.238,
    (1800, "Slightly Slow", "Leader"): 0.894, (1800, "Slightly Slow", "On-Pace"): 0.216,
    (1800, "Slightly Slow", "Midfield"): 1.327, (1800, "Slightly Slow", "Closer"): 1.716,
    (1800, "Slow", "Leader"): 0.942, (1800, "Slow", "On-Pace"): 1.697,
    (1800, "Slow", "Midfield"): 0.797, (1800, "Slow", "Closer"): 0.881,
    (2000, "Fast", "Leader"): 0.907, (2000, "Fast", "On-Pace"): 0.970,
    (2000, "Fast", "Midfield"): 0.443, (2000, "Fast", "Closer"): 1.894,
    (2000, "Slightly Fast", "Leader"): 1.404, (2000, "Slightly Fast", "On-Pace"): 0.963,
    (2000, "Slightly Fast", "Midfield"): 0.835, (2000, "Slightly Fast", "Closer"): 0.261,
    (2000, "Normal", "Leader"): 0.465, (2000, "Normal", "On-Pace"): 1.081,
    (2000, "Normal", "Midfield"): 1.490, (2000, "Normal", "Closer"): 2.002,
    (2000, "Slow", "Leader"): 0.520, (2000, "Slow", "On-Pace"): 1.320,
    (2000, "Slow", "Midfield"): 1.747, (2000, "Slow", "Closer"): 0.465,
    (2200, "Slightly Fast", "Leader"): 0.553, (2200, "Slightly Fast", "On-Pace"): 1.400,
    (2200, "Slightly Fast", "Midfield"): 0.520, (2200, "Slightly Fast", "Closer"): 1.268,
    (2200, "Normal", "Leader"): 1.041, (2200, "Normal", "On-Pace"): 0.404,
    (2200, "Normal", "Midfield"): 1.346, (2200, "Normal", "Closer"): 1.346,
    (2200, "Slightly Slow", "Leader"): 1.417, (2200, "Slightly Slow", "On-Pace"): 1.536,
    (2200, "Slightly Slow", "Midfield"): 0.553, (2200, "Slightly Slow", "Closer"): 0.520,
    (2200, "Slow", "Leader"): 0.553, (2200, "Slow", "On-Pace"): 1.536,
    (2200, "Slow", "Midfield"): 0.553, (2200, "Slow", "Closer"): 1.268,
}

# Marginal style Impact Value by distance (overall style advantage, pace-independent)
MARGINAL_STYLE_IV = {
    (1000, "Leader"): 2.034, (1000, "On-Pace"): 1.162, (1000, "Midfield"): 0.573, (1000, "Closer"): 0.418,
    (1200, "Leader"): 1.708, (1200, "On-Pace"): 1.204, (1200, "Midfield"): 0.765, (1200, "Closer"): 0.428,
    (1400, "Leader"): 1.370, (1400, "On-Pace"): 1.395, (1400, "Midfield"): 0.954, (1400, "Closer"): 0.424,
    (1600, "Leader"): 1.455, (1600, "On-Pace"): 1.439, (1600, "Midfield"): 0.829, (1600, "Closer"): 0.448,
    (1650, "Leader"): 1.287, (1650, "On-Pace"): 1.398, (1650, "Midfield"): 0.861, (1650, "Closer"): 0.542,
    (1800, "Leader"): 1.951, (1800, "On-Pace"): 0.627, (1800, "Midfield"): 1.184, (1800, "Closer"): 0.451,
    (2000, "Leader"): 1.771, (2000, "On-Pace"): 1.165, (2000, "Midfield"): 0.866, (2000, "Closer"): 0.450,
    (2200, "Leader"): 1.142, (2200, "On-Pace"): 1.004, (2200, "Midfield"): 0.690, (2200, "Closer"): 1.380,
}

# Pace z-score parameters by distance (for categorising the predicted pace)
PACE_Z_PARAMS = {
    1000: {"mean": -0.0407, "std": 0.2662},
    1200: {"mean": -0.0045, "std": 0.4574},
    1400: {"mean":  0.0173, "std": 0.4771},
    1600: {"mean": -0.1173, "std": 0.6847},
    1650: {"mean": -0.2326, "std": 0.7487},
    1800: {"mean": -0.1367, "std": 0.9266},
    2000: {"mean":  0.1025, "std": 0.7944},
    2200: {"mean": -0.1679, "std": 1.0854},
}

# Legacy PACE_PCT kept as fallback for risk engine dispersion calculation only
PACE_PCT = {
    "Fast":          {"Leader": +0.0035, "On-Pace": +0.0005, "Midfield": -0.0015, "Closer": -0.0035},
    "Slightly Fast": {"Leader": +0.0020, "On-Pace": +0.0003, "Midfield": -0.0008, "Closer": -0.0020},
    "Normal":        {"Leader":  0.0000, "On-Pace":  0.0000, "Midfield":  0.0000, "Closer":  0.0000},
    "Slightly Slow": {"Leader": -0.0015, "On-Pace": -0.0003, "Midfield": +0.0008, "Closer": +0.0015},
    "Slow":          {"Leader": -0.0025, "On-Pace": -0.0005, "Midfield": +0.0015, "Closer": +0.0025},
}


def predict_race_pace_v3(horses, distance, going="Good", venue="ST"):
    """v3.2 Recalibrated: Heuristic pace prediction with empirically-anchored thresholds.

    v3.4.3 HV 1200m recalibration (March 4 post-race evidence):
      HV 1200m races R2 & R5 both showed fast actual pace (-0.63s, -0.72s) vs
      model prediction of ~+0.01s ("Normal").  Average pace error = -0.68s.
      Cause: HV Course C 1200m is a short straight with minimal turning, producing
      systematically faster early sections than the venue-agnostic model predicts.
      Correction: apply -0.35s offset to predicted_dev for HV 1200m races
      (conservative half-step: 0.35 ≈ 0.68/2, given n=2 sample size).

    Computes heuristic pace pressure from horse running styles, converts to
    predicted pace deviation (seconds) via empirical calibration (1,125 races),
    and classifies using thresholds anchored to 5 verified reference races:

      Race 399 (ST 1000m): +0.53s -> Very Slow Pace
      Race 400 (ST 1600m): -0.15s -> Normal Pace
      Race 402 (ST 1400m): -0.59s -> Fast Pace
      Race 406 (HV 1200m): -0.07s -> Normal Pace
      Race 407 (HV 1650m): -0.93s -> Fast Pace

    Pace deviation = (Actual early-sectional sum) - (Standard early-sectional sum)
    Positive = slower than standard.  Negative = faster than standard.

    Thresholds (asymmetric -- fast side has wider range empirically):
      Very Slow:     dev >= +0.50s
      Slow:          +0.35 <= dev < +0.50
      Slightly Slow: +0.20 <= dev < +0.35
      Normal:        -0.20 < dev < +0.20
      Slightly Fast: -0.40 <= dev <= -0.20
      Fast:          -1.00 < dev < -0.40
      Very Fast:     dev <= -1.00
    """
    field_size = len(horses)
    if field_size == 0:
        return "Normal", 0.0, ["Empty field"], []

    pace_pressure = 0.0
    leader_names = []

    for h in horses:
        lfrac   = h.get("leader_frac", 0)
        ffrac   = h.get("front_frac", 0)
        esz     = h.get("early_speed_z", 0)
        entropy = h.get("style_entropy", 1.0)

        certainty = max(0.3, 1.0 - entropy / 2.0)
        h_pressure = (lfrac * 2.0 + ffrac * 1.0) * certainty

        if esz < -0.5:
            h_pressure += min(0.5, abs(esz) * 0.3)

        pace_pressure += h_pressure

        if lfrac >= 0.3 or (esz < -0.8 and ffrac >= 0.3):
            leader_names.append(h["horse_name"])

    pace_index = pace_pressure / field_size

    # Distance factor
    if distance <= 1200:
        dist_factor = 0.8
    elif distance <= 1650:
        dist_factor = 1.0
    else:
        dist_factor = 1.2
    pace_index *= dist_factor

    # Going factor (embedded in pace_index)
    going_factor = 1.0
    if going in ("Good-to-Firm",):
        going_factor = 1.10
    elif going in ("Good-to-Yielding", "Yielding"):
        going_factor = 0.90
    elif going in ("Soft/Heavy",):
        going_factor = 0.80
    elif going.startswith("AWT"):
        going_factor = 1.0
    pace_index *= going_factor

    # -- Empirical calibration: pace_index -> predicted deviation (seconds) --
    # Linear mapping fitted from 1,125 historical races (Pearson r=-0.145)
    # Higher pace_index (more front-runner pressure) -> more negative dev (faster)
    predicted_dev = 0.1173 + (-0.6253) * pace_index

    # Going surface correction: empirical offset relative to Good going
    # (residual beyond what going_factor already captures in pace_index)
    GOING_DEV_ADJ = {
        "Good":              0.000,
        "Good-to-Firm":     -0.166,   # GF races avg 0.18s faster; PI captures ~0.01s
        "Good-to-Yielding": +0.199,   # GY races avg 0.20s slower
        "Yielding":         +0.30,
        "Soft/Heavy":       +0.45,
    }
    predicted_dev += GOING_DEV_ADJ.get(going, 0.0)

    # -- v3.4.3 HV 1200m venue-specific pace recalibration --
    # March 4 post-race: HV 1200m pace errors were -0.63s (R2, GY) and -0.73s (R5, Good).
    # The model systematically underpredicts pace speed at HV 1200m.
    # Conservative correction: -0.35s offset (half of ~0.68s average error, n=2).
    HV_1200_PACE_OFFSET = -0.35
    if venue == "HV" and distance == 1200:
        predicted_dev += HV_1200_PACE_OFFSET

    # -- v3.4.4 ST venue-wide pace recalibration --
    # March 8 post-race: 10 of 11 ST races ran faster than predicted.
    # Mean pace error = -0.95s (including R7 1800m C2 outlier at -2.96s).
    # Excluding R7: mean = -0.73s across 10 races.
    # Conservative correction: -0.45s offset for all ST races.
    ST_PACE_OFFSET = -0.45
    if venue == "ST":
        predicted_dev += ST_PACE_OFFSET

    # -- Classification: reference-anchored thresholds (seconds) --
    # Positive deviation = slower than standard; negative = faster
    if predicted_dev >= 0.50:
        label = "Very Slow"
    elif predicted_dev >= 0.35:
        label = "Slow"
    elif predicted_dev >= 0.20:
        label = "Slightly Slow"
    elif predicted_dev > -0.20:
        label = "Normal"
    elif predicted_dev >= -0.40:
        label = "Slightly Fast"
    elif predicted_dev > -1.00:
        label = "Fast"
    else:
        label = "Very Fast"

    reasons = []
    if len(leader_names) == 0:
        reasons.append("No established front-runners")
    elif len(leader_names) == 1:
        reasons.append(f"Sole front-runner ({leader_names[0]})")
    else:
        reasons.append(f"{len(leader_names)} front-runners ({', '.join(leader_names[:4])})")

    hv_note = f", HV1200 {HV_1200_PACE_OFFSET:+.2f}s" if (venue == "HV" and distance == 1200) else ""
    st_note = f", ST {ST_PACE_OFFSET:+.2f}s" if venue == "ST" else ""
    reasons.append(f"Pred dev={predicted_dev:+.2f}s "
                   f"(PI={pace_index:.3f}, dist x{dist_factor:.1f}, going x{going_factor:.2f}{hv_note}{st_note})")

    return label, predicted_dev, reasons, leader_names


def get_pace_multiplier(distance, pace_label, style):
    """Look up the empirical pace-style multiplier.
    
    Returns a multiplier (centered on 1.0) that indicates how much better/worse
    this running style performs relative to its marginal average under this pace.
    Falls back to 1.0 (no adjustment) if lookup fails.
    """
    return PACE_STYLE_MULTIPLIERS.get((distance, pace_label, style), 1.0)


def pace_adj_from_multiplier(expected_time, multiplier, scale=0.010):
    """Convert empirical multiplier to a time adjustment (seconds).
    
    multiplier > 1.0 → style benefits → negative time adj (faster)
    multiplier < 1.0 → style penalised → positive time adj (slower)
    
    Scale controls conversion: 0.010 means a 1.0 multiplier unit ≈ 1.0% of ET.
    Example: ET=70s, mult=1.2 → adj = -(1.2-1.0)*70*0.010 = -0.140s
    Example: ET=70s, mult=0.6 → adj = -(0.6-1.0)*70*0.010 = +0.280s
    """
    if expected_time is None or pd.isna(expected_time):
        return 0.0
    return round(-(multiplier - 1.0) * expected_time * scale, 3)


# ══════════════════════════════════════════════════════════════════════════════
# 4½. Horse-Level Risk Metric & Win Probability  (interpretive overlay)
# ══════════════════════════════════════════════════════════════════════════════

def _logistic(x, centre, steepness):
    return 1.0 / (1.0 + math.exp(-steepness * (x - centre)))


# ---------- A. Pace Sensitivity (0-25) ----------
def _pace_sensitivity(et, style, max_pts=25):
    if et is None or pd.isna(et):
        return max_pts * 0.5, ["No expected time — pace sensitivity unknown"]
    adjs = [et * PACE_PCT[s].get(style, 0.0) for s in PACE_PCT]
    dispersion = max(adjs) - min(adjs)
    max_disp = 0.006 * et if et > 0 else 1.0
    norm = min(dispersion / max_disp, 1.0)
    score = round(norm * max_pts, 1)
    drivers = []
    if score > 15:
        drivers.append(f"High pace sensitivity ({dispersion:.3f}s swing across scenarios)")
    elif score > 8:
        drivers.append(f"Moderate pace sensitivity ({dispersion:.3f}s swing)")
    return score, drivers


# ---------- B. Draw Sensitivity (0-20) ----------
def _draw_sensitivity(draw_offset, draw_found, max_pts=20):
    if not draw_found:
        return 4.0, ["No draw data for this course/distance"]
    if draw_offset >= 0:
        return 0.0, []
    abs_off = abs(draw_offset)
    norm = _logistic(abs_off, 0.15, 20.0)
    score = round(norm * max_pts, 1)
    drivers = []
    if score > 10:
        drivers.append(f"Heavy draw reliance ({draw_offset:+.3f}s advantage at risk)")
    elif score > 5:
        drivers.append(f"Moderate draw advantage ({draw_offset:+.3f}s)")
    return score, drivers


# ---------- C. Performance Variance (0-25) ----------
def _performance_variance(resid_std, best_r, worst_r, max_pts=25):
    if pd.isna(resid_std):
        return max_pts * 0.6, ["No historical variance data — elevated uncertainty"]
    std_norm = _logistic(resid_std, 0.72, 3.0)
    raw_range = (worst_r - best_r) if (pd.notna(worst_r) and pd.notna(best_r)) else 2.0
    range_norm = _logistic(raw_range, 2.0, 1.5)
    combined = 0.60 * std_norm + 0.40 * range_norm
    score = round(combined * max_pts, 1)
    drivers = []
    if score > 15:
        drivers.append(f"High historical volatility (σ={resid_std:.2f}s, range={raw_range:.1f}s)")
    elif score > 8:
        drivers.append(f"Moderate volatility (σ={resid_std:.2f}s)")
    return score, drivers


# ---------- D. Shrinkage Dependence (0-15) → v3.1: reduced max to 10 ----------
def _shrinkage_dependence(shrunk_r, raw_r, sf, n_runs, max_pts=10):
    """v3.1: max reduced from 15→10 to downplay shrinkage in risk scoring."""
    if pd.isna(shrunk_r) or pd.isna(raw_r):
        return max_pts * 0.8, ["No ability data — full estimation risk"]
    delta = abs(shrunk_r - raw_r)
    shrink_intensity = 1.0 - sf
    delta_norm = _logistic(delta, 0.30, 5.0)
    sf_norm    = _logistic(shrink_intensity, 0.50, 5.0)
    combined = 0.50 * delta_norm + 0.50 * sf_norm
    score = round(combined * max_pts, 1)
    drivers = []
    if score > 7:
        drivers.append(f"Heavy shrinkage reliance (Δ={delta:.3f}s, SF={sf:.2f})")
    elif score > 4:
        drivers.append(f"Moderate shrinkage effect (SF={sf:.2f})")
    return score, drivers


# ---------- E. Structural Unknowns (0-15) ----------
def _structural_unknowns(n_runs, n_sect, style_entropy, confidence, max_pts=15):
    score = 0.0
    drivers = []
    if n_runs <= 2:
        score += 6.0;  drivers.append(f"Very lightly raced ({n_runs} runs)")
    elif n_runs <= 4:
        score += 4.0;  drivers.append(f"Lightly raced ({n_runs} runs)")
    elif n_runs <= 6:
        score += 2.0
    if n_runs > 0:
        sec_cov = n_sect / n_runs
        if sec_cov < 0.30:
            score += 3.0;  drivers.append(f"Sparse sectional data ({n_sect}/{n_runs} runs)")
    if style_entropy > 1.50:
        score += 4.0;  drivers.append(f"Highly unpredictable style (ent={style_entropy:.2f})")
    elif style_entropy > 1.20:
        score += 2.0;  drivers.append(f"Variable running style (ent={style_entropy:.2f})")
    if confidence == "no_data":
        score += 5.0;  drivers.append("No historical data in database")
    elif confidence == "low":
        score += 2.0
    return min(score, max_pts), drivers


# ---------- Composite Risk ----------
def compute_risk_metric(row):
    et    = row.get("expected_time")
    style = str(row.get("dominant_style", "Unknown"))
    a, da = _pace_sensitivity(et, style)
    b, db = _draw_sensitivity(row.get("draw_offset", 0), row.get("draw_found", False))
    c, dc = _performance_variance(
                row.get("residual_std", np.nan),
                row.get("best_residual", np.nan),
                row.get("worst_residual", np.nan))
    d, dd = _shrinkage_dependence(
                row.get("shrunk_resid", np.nan),
                row.get("raw_resid", np.nan),
                row.get("shrink_factor", 0),
                row.get("n_runs", 0))
    e, de = _structural_unknowns(
                row.get("n_runs", 0),
                row.get("n_sectional_runs", 0),
                row.get("style_entropy", 1.0),
                str(row.get("confidence", "no_data")))
    # v3.1: risk budget now sums to 95 max (pace 25 + draw 20 + var 25 + shr 10 + struct 15)
    total = min(100.0, max(0.0, a + b + c + d + e))
    tier  = "Low" if total <= 30 else ("Moderate" if total <= 55 else "High")
    comps = {"pace": a, "draw": b, "variance": c, "shrinkage": d, "structural": e}
    all_d = da + db + dc + dd + de or ["Minimal risk factors identified"]
    return round(total, 1), tier, comps, all_d


# ---------- Win Probability (Boltzmann + confidence weighting) ----------
def compute_win_probabilities(df, race_class=None):
    """
    v3.4.4: Boltzmann distribution with confidence deflation and class-tier
    temperature scaling.

    v3.4.4 class-tier calibration (March 8 post-race evidence):
      C4-C5 races had avg Spearman \u03c1=0.19 vs C1-C3 avg \u03c1=0.59.
      The model is overconfident in lower classes where fields are more
      unpredictable.  Widen the Boltzmann temperature for C4-C5 to flatten
      probability distributions, better reflecting genuine uncertainty.
      C4: T multiplied by 1.30 (30% wider spread)
      C5: T multiplied by 1.50 (50% wider spread)
    """
    valid = df["projected_time"].notna()
    probs = pd.Series(0.0, index=df.index)
    if valid.sum() < 2:
        if valid.sum() == 1:
            probs.loc[valid] = 100.0
        return probs

    times = df.loc[valid, "projected_time"].values
    T = max(0.25, np.std(times) * 0.80)

    # v3.4.4: class-tier temperature scaling
    CLASS_T_MULT = {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.30, 5: 1.50}
    if race_class is not None:
        T *= CLASS_T_MULT.get(int(race_class), 1.0)

    logits = -(times - times.min()) / T
    exp_l  = np.exp(logits - logits.max())
    raw_probs = exp_l / exp_l.sum()

    if PROB_CONF_DEFLATE:
        # Deflate probabilities for low-confidence horses
        sfs = df.loc[valid, "shrink_factor"].values
        conf_weights = np.array([
            (0.5 + 0.5 * sf) if sf < 0.50 else 1.0
            for sf in sfs
        ])
        adjusted = raw_probs * conf_weights
        adjusted = adjusted / adjusted.sum()  # renormalise
        probs.loc[valid] = np.round(adjusted * 100, 1)
    else:
        probs.loc[valid] = np.round(raw_probs * 100, 1)

    return probs


# ---------- Enrichment wrapper ----------
def enrich_with_risk(df, race):
    df["win_prob"] = compute_win_probabilities(df, race_class=race.get("race_class"))
    rscores, rtiers, rcomps, rdrivers = [], [], [], []
    for _, row in df.iterrows():
        s, t, c, d = compute_risk_metric(row)
        rscores.append(s);  rtiers.append(t)
        rcomps.append(c);   rdrivers.append(d)
    df["risk_score"]      = rscores
    df["risk_tier"]       = rtiers
    df["risk_components"] = rcomps
    df["risk_drivers"]    = rdrivers
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 5. Project one race  (v3.3: audit-driven corrections)
# ══════════════════════════════════════════════════════════════════════════════

def _map_going(code):
    """Map DB going abbreviation to expected-time reference label."""
    return GOING_CODE_MAP.get(str(code).strip(), "Good")


def _safe_place(val):
    """Parse place value safely — handles '3 DH', 'WV', NaN etc."""
    if pd.isna(val):
        return 99
    s = str(val).strip()
    # Extract leading digits (e.g., '3 DH' → 3)
    m = re.match(r'^(\d+)', s)
    if m:
        return int(m.group(1))
    return 99


def _get_race_pace_index(race_date, race_number, db, class_fine, fine, coarse, ultra):
    """
    v3.4.3 (R): Compute the median field residual for a specific historical race.
    Returns the median of (finish_time - expected_time) across all finishers.
    Cached in _RACE_PACE_CACHE for efficiency.
    Returns 0.0 if fewer than 3 finishers have valid residuals.
    """
    key = (race_date, race_number)
    if key in _RACE_PACE_CACHE:
        return _RACE_PACE_CACHE[key]

    race_runners = db[(db["race_date"] == race_date) & (db["race_number"] == race_number)]
    field_residuals = []
    for _, runner in race_runners.iterrows():
        ft = runner.get("finish_time_seconds")
        dist = runner.get("distance")
        if pd.isna(ft) or pd.isna(dist) or ft <= 0:
            continue
        dist_i = int(dist)
        going_i = _map_going(runner.get("going", "G"))
        wt_i = runner.get("actual_weight")
        wb_i = weight_band(int(wt_i)) if pd.notna(wt_i) and wt_i > 0 else "121-125"
        rc_i = runner.get("race_course", "A")
        tt_i = runner.get("track_type", "Turf")
        rc_class_i = runner.get("race_class")
        cb_i = class_band(int(rc_class_i)) if pd.notna(rc_class_i) else "Group/Other"

        et_i, _, _, _ = lookup_expected_time(
            class_fine, fine, coarse, ultra, dist_i, going_i, wb_i, rc_i, tt_i, cb_i)
        if pd.notna(et_i) and et_i > 0:
            field_residuals.append(ft - et_i)

    rpi = float(np.median(field_residuals)) if len(field_residuals) >= 3 else 0.0
    _RACE_PACE_CACHE[key] = rpi
    return rpi


def compute_recency_residual(horse_name, db, class_fine, fine, coarse, ultra,
                              today_track_type="Turf", today_venue="ST",
                              today_distance=None):
    """
    v3.3 Adjustment A: recency-weighted raw residual from individual runs.
    v3.4: also returns the raw residuals list + surfaces + venues for trajectory detection.
    v3.4 (I): AWT→Turf discount — when today is Turf, AWT historical runs get
              reduced weight (×AWT_TURF_DISCOUNT) in the recency calculation.
    v3.4 (K): HV↔ST venue discount — when today is ST, HV historical runs get
              reduced weight (×HV_ST_DISCOUNT), and vice versa.
    v3.4.1 (L+M): Also returns n_same_surface_runs and n_nearby_dist_runs for
                   surface-aware and distance-aware uncertainty.
    v3.4.3 (R): Race-pace normalization — each run's residual is adjusted by
                subtracting the median field residual of that race, removing
                tactical pace effects (critical for Group races).

    For each historical run, computes run_residual = (finish_time − expected_time) − race_pace_index.
    Weights runs with exponential decay: w_i = λ^(n-1-i), most recent = weight 1.0.
    AWT runs discounted when today is Turf. Cross-venue runs discounted.
    Returns (recency_resid, n_valid_runs, residuals_list, surfaces_list, venues_list,
             n_same_surface_runs, n_nearby_dist_runs).
    If no valid runs, returns (None, 0, [], [], [], 0, 0).
    """
    runs = db[db["horse_name"] == horse_name].copy()
    if len(runs) == 0:
        return None, 0, [], [], [], 0, 0

    runs = runs.sort_values("race_date", ascending=True).reset_index(drop=True)

    residuals = []
    surfaces = []   # v3.4 (I): parallel list of surface per valid run
    venues = []     # v3.4 (K): parallel list of venue (HV/ST) per valid run
    distances = []  # v3.4.1 (M): parallel list of distance per valid run
    places = []     # v3.4.2 (Q): parallel list of finishing place per valid run
    for _, run in runs.iterrows():
        ft = run.get("finish_time_seconds")
        dist = run.get("distance")
        if pd.isna(ft) or pd.isna(dist) or ft <= 0:
            continue
        dist = int(dist)
        going_label = _map_going(run.get("going", "G"))
        wt = run.get("actual_weight")
        wband = weight_band(int(wt)) if pd.notna(wt) and wt > 0 else "121-125"
        rc = run.get("race_course", "A")
        tt = run.get("track_type", "Turf")
        venue = run.get("race_track", "ST")  # v3.4 (K): HV or ST
        rc_class = run.get("race_class")
        # v3.4.3: Fixed class_band mapping — class=0 (Group) was incorrectly falling to "C4"
        cband = class_band(int(rc_class)) if pd.notna(rc_class) else "Group/Other"

        et, n_ref, _, _ = lookup_expected_time(
            class_fine, fine, coarse, ultra, dist, going_label, wband, rc, tt, cband)
        if pd.notna(et) and et > 0:
            raw_resid = ft - et
            # v3.4.3 (R): Race-pace normalization — subtract median field residual
            rpi = _get_race_pace_index(
                run.get("race_date"), run.get("race_number"),
                db, class_fine, fine, coarse, ultra)
            adj_resid = raw_resid - rpi
            # v3.4.4 (W): Finishing-position credit
            place_val = _safe_place(run.get("place", 99))
            adj_resid += POSITION_CREDIT.get(place_val, 0.0)
            residuals.append(adj_resid)
            surfaces.append(tt)
            venues.append(str(venue))
            distances.append(dist)
            places.append(place_val)

    n_valid = len(residuals)
    if n_valid == 0:
        return None, 0, [], [], [], 0, 0

    # ── v3.4.6 Adjustment Z: outlier trimming for thin data ──
    #    For horses with ≤ OUTLIER_TRIM_MAX_RUNS runs, cap extreme residuals
    #    at median ± OUTLIER_TRIM_Z × σ.  This prevents one catastrophic run
    #    from destroying the projection (e.g. Osi Honour 105.72s → resid +7s).
    if n_valid >= 2 and n_valid <= OUTLIER_TRIM_MAX_RUNS:
        resid_arr = np.array(residuals)
        med = np.median(resid_arr)
        resid_std = np.std(resid_arr)
        if resid_std > 0:
            upper = med + OUTLIER_TRIM_Z * resid_std
            lower = med - OUTLIER_TRIM_Z * resid_std
            residuals = [max(lower, min(upper, r)) for r in residuals]

    # v3.4.1 (L): count same-surface runs
    n_same_surface = sum(1 for s in surfaces
                         if (today_track_type == "Turf" and "All Weather" not in str(s))
                         or (today_track_type != "Turf" and "All Weather" in str(s)))

    # v3.4.1 (M): count nearby-distance runs
    n_nearby_dist = 0
    if today_distance is not None:
        n_nearby_dist = sum(1 for d in distances
                            if abs(d - today_distance) <= DISTANCE_PROXIMITY_M)

    # Apply exponential recency decay: most recent run gets weight 1.0
    weights = np.array([RECENCY_LAMBDA ** (n_valid - 1 - i) for i in range(n_valid)])

    # v3.4.4 (V): Distance-aware recency discount — runs at different distances
    # get reduced weight.  This prevents great 1650m form inflating 1800m projection
    # (Highland Rahy) or terrible 1200m runs drowning 1000m wins (Macanese Master).
    if today_distance is not None:
        for i, d in enumerate(distances):
            diff = abs(d - today_distance)
            if diff == 0:
                dist_mult = DIST_RECENCY_EXACT
            elif diff <= 100:
                dist_mult = DIST_RECENCY_NEAR
            elif diff <= 200:
                dist_mult = DIST_RECENCY_MED
            else:
                dist_mult = DIST_RECENCY_FAR
            weights[i] *= dist_mult

    # v3.4 (I): AWT→Turf discount — reduce weight of AWT runs when today is Turf
    today_is_turf = today_track_type == "Turf"
    n_awt_discounted = 0
    if today_is_turf:
        for i, surf in enumerate(surfaces):
            if "All Weather" in str(surf):
                weights[i] *= AWT_TURF_DISCOUNT
                n_awt_discounted += 1

    # v3.4 (K): HV↔ST venue discount — reduce weight of cross-venue runs
    n_venue_discounted = 0
    for i, v in enumerate(venues):
        if v != today_venue:
            weights[i] *= HV_ST_DISCOUNT
            n_venue_discounted += 1

    # v3.4.2 (Q): Last-start winner boost — if most recent run is a win, boost its weight
    if len(places) > 0 and places[-1] == 1:
        weights[-1] *= LAST_WIN_BOOST

    weights /= weights.sum()
    recency_resid = float(np.dot(residuals, weights))
    return recency_resid, n_valid, residuals, surfaces, venues, n_same_surface, n_nearby_dist


def _extract_final_sectional(sectional_str, distance):
    """Extract the final ~400m sectional time from semicolon-separated string."""
    if pd.isna(sectional_str) or not str(sectional_str).strip():
        return None
    parts = [p.strip() for p in str(sectional_str).split(";")]
    # Last non-empty numeric value = final sectional
    for p in reversed(parts):
        try:
            val = float(p)
            if 15.0 < val < 40.0:  # sanity check for ~400m time
                return val
        except (ValueError, TypeError):
            continue
    return None


def compute_projected_final_sectional(horse_name, db, distance, race_class, pace_label, dom_style):
    """
    v3.3: project a horse's final ~400m sectional time.

    1. Fetch the horse's historical final sectionals from DB
    2. Apply recency weighting
    3. Adjust for predicted race pace and running style
    4. If insufficient data (< 3 runs with sectionals), use class average

    Returns dict with: proj_sec, horse_mean, horse_std, n_sec, class_mean, pace_adj, source
    """
    result = {"proj_sec": None, "horse_mean": None, "horse_std": None,
              "n_sec": 0, "class_mean": None, "pace_adj": 0.0, "source": "none"}

    # Class average final sectional for this distance
    class_runs = db[(db["distance"] == distance) & (db["race_class"] == race_class)]
    class_secs = []
    for _, cr in class_runs.iterrows():
        fs = _extract_final_sectional(cr.get("sectiontimes"), distance)
        if fs is not None:
            class_secs.append(fs)
    if class_secs:
        result["class_mean"] = round(np.mean(class_secs), 2)

    # Horse-specific historical final sectionals
    horse_runs = db[db["horse_name"] == horse_name].sort_values("race_date", ascending=True)
    horse_secs = []
    for _, hr in horse_runs.iterrows():
        fs = _extract_final_sectional(hr.get("sectiontimes"), hr.get("distance"))
        if fs is not None:
            horse_secs.append(fs)

    result["n_sec"] = len(horse_secs)

    if len(horse_secs) >= 3:
        # Enough data — use recency-weighted horse-specific mean
        n = len(horse_secs)
        weights = np.array([RECENCY_LAMBDA ** (n - 1 - i) for i in range(n)])
        weights /= weights.sum()
        h_mean = float(np.dot(horse_secs, weights))
        result["horse_mean"] = round(np.mean(horse_secs), 2)
        result["horse_std"]  = round(np.std(horse_secs), 2) if n > 1 else None
        base_sec = h_mean
        result["source"] = "horse"
    elif len(horse_secs) > 0:
        # Some data but < 3: blend horse mean with class average
        h_mean = float(np.mean(horse_secs))
        result["horse_mean"] = round(h_mean, 2)
        if result["class_mean"] is not None:
            blend_wt = len(horse_secs) / 3.0  # partial confidence
            base_sec = blend_wt * h_mean + (1 - blend_wt) * result["class_mean"]
            result["source"] = "blend"
        else:
            base_sec = h_mean
            result["source"] = "horse_thin"
    elif result["class_mean"] is not None:
        base_sec = result["class_mean"]
        result["source"] = "class"
    else:
        return result  # no data at all

    # Pace adjustment based on predicted pace and running style
    style_key = dom_style if dom_style in ("Leader", "Front", "Midfield", "Closer") else "Unknown"
    pace_key = pace_label if pace_label in PACE_FINAL_SEC_ADJ else "Neutral"
    pace_adj = PACE_FINAL_SEC_ADJ[pace_key][style_key]
    result["pace_adj"] = pace_adj
    result["proj_sec"] = round(base_sec + pace_adj, 2)

    return result


def compute_blended_residual(shrunk_resid, raw_resid, sf, n_runs,
                              trajectory_info=None):
    """
    v3.4.2: trajectory-aware blend of raw and shrunk residual, with:
      N. Shrinkage floor for n=1 disasters
      O. Trajectory bonus gating

    Standard v3.2/3.3 logic:
      SF >= 0.30 and n_runs >= 3 → 0.80 * raw + 0.20 * shrunk
      Otherwise → 100% shrunk

    v3.4 changes:
      G. If trajectory=Improving AND n_runs >= TRAJ_MIN_RUNS AND SF < BLEND_SF_THRESH:
         Use TRAJ_SF_OVERRIDE_WT * raw + (1 - TRAJ_SF_OVERRIDE_WT) * shrunk
      F. Add trajectory bonus (negative = faster) or penalty (positive = slower)

    v3.4.2 changes:
      N. When SF < 0.20 and raw_resid > +0.20s, enforce floor = raw × 0.50
      O. Trajectory bonus only applied when resulting resid < +0.10s
    """
    if pd.isna(shrunk_resid) or pd.isna(raw_resid):
        return shrunk_resid  # fallback to whatever we have

    traj = trajectory_info.get("trajectory", "Insufficient") if trajectory_info else "Insufficient"

    if sf >= BLEND_SF_THRESH and n_runs >= BLEND_N_THRESH:
        blended = BLEND_RAW_WT * raw_resid + (1.0 - BLEND_RAW_WT) * shrunk_resid
    elif traj == "Improving" and n_runs >= TRAJ_MIN_RUNS:
        # v3.4 (G): trajectory override — partial raw blend for improvers
        blended = TRAJ_SF_OVERRIDE_WT * raw_resid + (1.0 - TRAJ_SF_OVERRIDE_WT) * shrunk_resid
    else:
        # Thinly raced: shrinkage is protective, keep it
        blended = shrunk_resid

    # v3.4.2 (N): Shrinkage floor for n=1 disasters
    #   When SF is very low (extreme shrinkage) and the raw residual shows the
    #   horse is genuinely slow, enforce a minimum effective residual.
    #   This prevents dead-last horses from being shrunk to near-zero.
    if sf < SHRINKAGE_FLOOR_SF_THRESH and raw_resid > SHRINKAGE_FLOOR_RAW_THRESH:
        floor_resid = raw_resid * SHRINKAGE_FLOOR_FACTOR
        if blended < floor_resid:
            blended = floor_resid

    # v3.4 (F): apply trajectory bonus/penalty
    if trajectory_info:
        bonus = trajectory_info.get("bonus", 0.0)
        penalty = trajectory_info.get("penalty", 0.0)

        # v3.4.2 (O): Trajectory bonus gating — only apply bonus when
        # the pre-bonus residual is already close to or better than expected.
        # Prevents rewarding "terrible → bad" trajectories.
        if bonus < 0 and blended > TRAJ_BONUS_RESID_GATE:
            bonus = 0.0  # suppress bonus — horse is still too slow

        blended += bonus
        blended += penalty

    return blended


def detect_trajectory(residuals_list, surfaces_list=None, today_surface="Turf",
                      venues_list=None, today_venue="ST"):
    """
    v3.4 (E): Classify horse trajectory from chronological run residuals.
    v3.4 (I): Surface-aware confidence — if last-2 runs include cross-surface,
              halve the trajectory bonus/penalty.
    v3.4 (J): Consistency check — if last-2 runs disagree in direction relative
              to baseline, classify as Stable.
    v3.4 (K): Venue-aware confidence — if last-2 runs include cross-venue (HV↔ST),
              halve the trajectory bonus/penalty.

    Returns dict:
      trajectory: "Improving" | "Declining" | "Stable" | "Insufficient"
      last2_mean: mean residual of last 2 runs
      prior_baseline: mean residual of runs before last 2 (up to prior 3)
      delta: last2_mean - prior_baseline (negative = improving)
      bonus: time adjustment for improving horses (negative = faster)
      penalty: time adjustment for declining horses (positive = slower)
      trend_slope: OLS slope of all residuals over run index
      cross_surface: True if last-2 runs include different surface than today
      cross_venue: True if last-2 runs include different venue than today
    """
    result = {
        "trajectory": "Insufficient", "last2_mean": None, "prior_baseline": None,
        "delta": None, "bonus": 0.0, "penalty": 0.0, "trend_slope": None,
        "n_traj_runs": len(residuals_list), "cross_surface": False,
        "cross_venue": False,
    }

    if len(residuals_list) < TRAJ_MIN_RUNS:
        return result

    n = len(residuals_list)

    # ── v3.4.7 (AC): Trajectory reliability gate ──
    #    When n ≤ TRAJ_RELIABILITY_MAX_N and the residual spread is very high,
    #    the trajectory signal is noise, not trend.  Suppress to Insufficient.
    #    Fixes Turquoise Velocity (n=3, σ=1.2s): P1, blowout P11, then WIN P1
    #    was classified "Declining" because OLS slope on 3 volatile points > 0.
    if n <= TRAJ_RELIABILITY_MAX_N:
        resid_std = float(np.std(residuals_list))
        if resid_std > TRAJ_RELIABILITY_STD:
            result["trajectory"] = "Insufficient"
            result["n_traj_runs"] = n
            return result

    # Last 2 runs vs prior baseline (up to 3 runs before last 2)
    last2 = residuals_list[-2:]
    prior_start = max(0, n - 5)
    prior = residuals_list[prior_start:-2]
    if len(prior) == 0:
        prior = residuals_list[:1]

    last2_mean = float(np.mean(last2))
    prior_mean = float(np.mean(prior))
    delta = last2_mean - prior_mean  # negative = improving (faster)

    result["last2_mean"] = round(last2_mean, 4)
    result["prior_baseline"] = round(prior_mean, 4)
    result["delta"] = round(delta, 4)

    # OLS trend slope
    x = np.arange(n)
    slope, _ = np.polyfit(x, residuals_list, 1)
    result["trend_slope"] = round(slope, 4)

    # v3.4 (I): check if last-2 runs are on a different surface than today
    cross_surface = False
    if surfaces_list and len(surfaces_list) >= n:
        last2_surfaces = surfaces_list[-2:]
        today_is_turf = today_surface == "Turf"
        for surf in last2_surfaces:
            if today_is_turf and "All Weather" in str(surf):
                cross_surface = True
            elif not today_is_turf and "All Weather" not in str(surf):
                cross_surface = True
    result["cross_surface"] = cross_surface

    # v3.4 (K): check if last-2 runs are at a different venue than today
    cross_venue = False
    if venues_list and len(venues_list) >= n:
        last2_venues = venues_list[-2:]
        for v in last2_venues:
            if str(v) != today_venue:
                cross_venue = True
    result["cross_venue"] = cross_venue

    # v3.4 (J): Consistency check — last-2 runs must agree in direction
    # If one of last-2 is better than baseline and the other is worse,
    # the trajectory signal is ambiguous → classify as Stable
    run_minus1 = last2[-1]  # most recent
    run_minus2 = last2[-2]  # second most recent
    better_count = sum(1 for r in last2 if r < prior_mean)
    worse_count = sum(1 for r in last2 if r > prior_mean)
    last2_disagree = (better_count >= 1 and worse_count >= 1)

    # Classification: delta + slope must agree in direction + last-2 consistency
    if last2_disagree:
        # v3.4 (J): last-2 runs disagree → Stable, no bonus/penalty
        result["trajectory"] = "Stable"
    elif delta <= -TRAJ_IMPROVE_THRESH and slope < 0:
        result["trajectory"] = "Improving"
        raw_bonus = min(abs(delta) * 0.5, TRAJ_BONUS_CAP)
        # Full bonus if both last-2 runs are better than baseline, else 60%
        both_better = all(r < prior_mean for r in last2)
        bonus = -raw_bonus if both_better else -raw_bonus * 0.6
        # v3.4 (I): halve bonus if cross-surface
        if cross_surface:
            bonus *= AWT_TRAJ_HAIRCUT
        # v3.4 (K): halve bonus if cross-venue
        if cross_venue:
            bonus *= VENUE_TRAJ_HAIRCUT
        result["bonus"] = round(bonus, 4)
    elif delta >= TRAJ_DECLINE_THRESH and slope > 0:
        result["trajectory"] = "Declining"
        raw_pen = min(abs(delta) * 0.4, TRAJ_DECLINE_CAP)
        penalty = raw_pen
        # v3.4 (I): halve penalty if cross-surface
        if cross_surface:
            penalty *= AWT_TRAJ_HAIRCUT
        # v3.4 (K): halve penalty if cross-venue
        if cross_venue:
            penalty *= VENUE_TRAJ_HAIRCUT
        result["penalty"] = round(penalty, 4)
    else:
        result["trajectory"] = "Stable"

    return result


def project_race(race, class_fine, fine, coarse, ultra, draw_off, abilities, db=None):
    """v3.4.7: project_race with trajectory-aware scoring + systematic fixes."""
    distance   = race["distance"]
    track_type = race["track_type"]
    course     = race["race_course"]
    going = AWT_GOING_ASSUMED if race["is_awt"] else TURF_GOING_ASSUMED
    cband = class_band(race["race_class"])
    field_size = len(race["horses"])  # v3.4.7 (AD): needed for draw scaling

    horse_data = []
    for h in race["horses"]:
        wband = weight_band(h["weight"])
        et, n_ref, std_ref, tier = lookup_expected_time(
            class_fine, fine, coarse, ultra,
            distance, going, wband, course, track_type, cband)

        d_off, d_found = get_draw_offset(draw_off, distance, course, h["draw"],
                                          field_size=field_size)

        ab_row = abilities.loc[abilities["horse_name"] == h["horse_name"]]
        if len(ab_row):
            ab = ab_row.iloc[0]
            ability_v3    = ab.get("ability_score_v3", np.nan)
            shrunk_resid  = ab.get("shrunk_weighted_resid", np.nan)
            raw_resid     = ab.get("raw_weighted_resid", np.nan)
            shrink_factor = ab.get("shrinkage_factor", 1.0)
            shrink_k      = ab.get("shrinkage_k_used", 5)
            n_runs        = int(ab.get("n_runs", 0))
            confidence    = str(ab.get("confidence", "no_data"))
            dom_style     = str(ab.get("dominant_style", "Unknown"))
            leader_frac   = float(ab.get("leader_frac", 0))
            front_frac    = float(ab.get("front_frac", 0))
            early_speed_z = float(ab.get("early_speed_z", 0)) if pd.notna(ab.get("early_speed_z")) else 0.0
            style_entropy = float(ab.get("style_entropy", 1.0)) if pd.notna(ab.get("style_entropy")) else 1.0
            residual_std  = float(ab.get("residual_std", np.nan)) if pd.notna(ab.get("residual_std")) else np.nan
            best_residual = float(ab.get("best_residual", np.nan)) if pd.notna(ab.get("best_residual")) else np.nan
            worst_residual = float(ab.get("worst_residual", np.nan)) if pd.notna(ab.get("worst_residual")) else np.nan
            n_sectional_runs = int(ab.get("n_sectional_runs", 0)) if pd.notna(ab.get("n_sectional_runs")) else 0
        else:
            ability_v3 = shrunk_resid = raw_resid = np.nan
            shrink_factor = 0; shrink_k = 5; n_runs = 0
            confidence = "no_data"; dom_style = "Unknown"
            leader_frac = front_frac = 0.0
            early_speed_z = 0.0; style_entropy = 1.0
            residual_std = best_residual = worst_residual = np.nan
            n_sectional_runs = 0

        # ── v3.3 Adjustment A: recency-weighted residual ──
        # ── v3.4 Adjustment E: trajectory detection ──
        # ── v3.4 Adjustment K: venue-aware discount ──
        # ── v3.4.1 Adjustment L+M: surface/distance-aware uncertainty ──
        recency_resid = None
        n_valid_runs = 0
        residuals_list = []
        surfaces_list = []
        venues_list = []
        n_same_surface_runs = 0
        n_nearby_dist_runs = 0
        trajectory_info = {"trajectory": "Insufficient", "bonus": 0.0, "penalty": 0.0}
        if db is not None:
            recency_resid, n_valid_runs, residuals_list, surfaces_list, venues_list, \
                n_same_surface_runs, n_nearby_dist_runs = compute_recency_residual(
                h["horse_name"], db, class_fine, fine, coarse, ultra,
                today_track_type=track_type, today_venue=MEETING_VENUE,
                today_distance=distance)
            trajectory_info = detect_trajectory(residuals_list, surfaces_list, track_type,
                                                venues_list=venues_list, today_venue=MEETING_VENUE)

        # ── v3.4.7 Adjustment AE: Ability-anchored projection ──
        #    Use the horse's own contextualised performance (recency_resid)
        #    directly — NO shrinkage toward class average.
        #    recency_resid already captures: finish_time - ET(run_conditions)
        #    - race_pace_index + position_credit, weighted by recency decay,
        #    distance/surface/venue discounts.  Adding to today's ET gives
        #    what the horse would run under today's conditions based on
        #    its proven form.
        if recency_resid is not None:
            effective_resid = recency_resid
            # Apply trajectory bonus/penalty (trend detection still valuable)
            if trajectory_info:
                bonus = trajectory_info.get("bonus", 0.0)
                penalty = trajectory_info.get("penalty", 0.0)
                # Gating: only give improving bonus if horse is already decent
                if bonus < 0 and effective_resid > TRAJ_BONUS_RESID_GATE:
                    bonus = 0.0
                effective_resid += bonus + penalty
        elif pd.notna(shrunk_resid):
            # Fallback: no recency data — use pre-computed shrunk (rare)
            effective_resid = shrunk_resid
            if trajectory_info:
                bonus = trajectory_info.get("bonus", 0.0)
                penalty = trajectory_info.get("penalty", 0.0)
                if bonus < 0 and effective_resid > TRAJ_BONUS_RESID_GATE:
                    bonus = 0.0
                effective_resid += bonus + penalty
        else:
            effective_resid = np.nan

        proj = (et + effective_resid + d_off) if (pd.notna(et) and pd.notna(effective_resid)) else np.nan

        # ── v3.3 Adjustment B + v3.4 Adjustment H: uncertainty penalty ──
        uncertainty_pen = 0.0
        if n_runs > 0 and n_runs < UNCERTAINTY_N_THRESH:
            # v3.4 (H): reduced penalty for strong debuts
            raw_for_test = raw_resid if pd.notna(raw_resid) else (recency_resid or 0)
            if n_runs == 1 and raw_for_test < STRONG_DEBUT_THRESH:
                uncertainty_pen = UNCERTAINTY_BASE_V34 / math.sqrt(n_runs)
            else:
                uncertainty_pen = UNCERTAINTY_BASE / math.sqrt(n_runs)
            if pd.notna(proj):
                proj += uncertainty_pen

        # ── v3.4.1 Adjustment L: surface-aware uncertainty ──
        #    When projecting Turf but horse has few/no Turf runs, add penalty.
        #    This prevents pure-AWT horses (e.g. HAPPYDEARHAPPYDEER with 0 Turf runs)
        #    from being projected with false confidence on Turf.
        #    CRITICAL: only apply when there ARE cross-surface runs (n_same < n_total).
        #    If 100% of runs are on today's surface, small-sample uncertainty is
        #    already handled by the regular U+ penalty — no surface mismatch exists.
        surface_pen = 0.0
        has_cross_surface = (n_valid_runs > 0 and n_same_surface_runs < n_valid_runs)
        if has_cross_surface and n_same_surface_runs < SURFACE_UNCERTAINTY_THRESH:
            if n_same_surface_runs == 0:
                # No runs on today's surface — maximum uncertainty
                surface_pen = SURFACE_UNCERTAINTY_BASE * 1.5
            else:
                surface_pen = SURFACE_UNCERTAINTY_BASE / math.sqrt(n_same_surface_runs)

            # ── v3.4.6 Adjustment AA: surface penalty flexibility ──
            #    If the most recent same-surface run was a top-3 finish, halve the
            #    surface penalty.  One strong representative run reduces uncertainty.
            if db is not None and n_same_surface_runs > 0:
                horse_runs_surf = db[db["horse_name"] == h["horse_name"]].sort_values(
                    "race_date", ascending=False)
                for _, sr in horse_runs_surf.iterrows():
                    sr_tt = sr.get("track_type", "")
                    same = ((track_type == "Turf" and "All Weather" not in str(sr_tt))
                            or (track_type != "Turf" and "All Weather" in str(sr_tt)))
                    if same:
                        sr_place = _safe_place(sr.get("place", 99))
                        if sr_place <= 3:
                            surface_pen *= SURFACE_PEN_STRONG_DISCOUNT
                        break  # only check the most recent same-surface run

            if pd.notna(proj):
                proj += surface_pen

        # ── v3.4.1 Adjustment M: distance-aware uncertainty ──
        #    When horse has few runs at today's distance (±DISTANCE_PROXIMITY_M), add uncertainty.
        #    Prevents heavy extrapolation (e.g. MIGHTY STRENGTH 2000m→1400m).
        #    CRITICAL: only apply when there ARE runs at non-nearby distances.
        #    If 100% of runs are within ±DISTANCE_PROXIMITY_M, no distance mismatch exists.
        distance_pen = 0.0
        has_dist_mismatch = (n_valid_runs > 0 and n_nearby_dist_runs < n_valid_runs)
        if has_dist_mismatch and n_nearby_dist_runs < DISTANCE_UNCERTAINTY_THRESH:
            if n_nearby_dist_runs == 0:
                # No runs near today's distance — significant uncertainty
                distance_pen = DISTANCE_UNCERTAINTY_BASE * 1.5
            else:
                distance_pen = DISTANCE_UNCERTAINTY_BASE / math.sqrt(n_nearby_dist_runs)
            if pd.notna(proj):
                proj += distance_pen

        # ── v3.4.2 Adjustment P: forced distance penalty for 100% mismatch ──
        #    If horse has ZERO runs at today's exact distance, apply a forced penalty.
        #    Catches cases where all runs are at a different distance but close enough
        #    to pass the ±100m proximity check (safety net).
        forced_dist_pen = 0.0
        if db is not None and n_valid_runs > 0:
            horse_runs_for_dist = db[db["horse_name"] == h["horse_name"]].copy()
            exact_dist_runs = sum(1 for _, rr in horse_runs_for_dist.iterrows()
                                  if pd.notna(rr.get("distance")) and int(rr["distance"]) == distance
                                  and pd.notna(rr.get("finish_time_seconds")) and rr["finish_time_seconds"] > 0)
            if exact_dist_runs == 0:
                forced_dist_pen = FULL_DIST_MISMATCH_PEN
                if pd.notna(proj):
                    proj += forced_dist_pen

        # ── v3.4.7 Adjustment AB: class-relative ability ──
        #    Replace the flat class-transition penalty with a contextual system:
        #    1. Check if horse has ANY run in today's class — if so, skip (not transitioning)
        #    2. Identify the horse's 'old' class (modal class from recent 5 runs)
        #    3. Compute horse's mean residual in old-class runs (competitiveness margin)
        #    4. Get ET gap between old class and today's class
        #    5. Step-up: penalty scales with ET gap, reduced by old-class margin
        #    6. Step-down: bonus scales with ET gap × dominance weight
        class_trans_pen = 0.0
        today_class = race.get("race_class")
        if db is not None and today_class and today_class > 0 and n_valid_runs > 0:
            horse_class_runs = db[db["horse_name"] == h["horse_name"]].copy()
            horse_class_runs = horse_class_runs[horse_class_runs["race_class"].notna()]
            if len(horse_class_runs) > 0:
                recent_classes = horse_class_runs.sort_values("race_date", ascending=False).head(5)

                # GATE: if horse has any run in today's class, it's NOT transitioning.
                # The residual already reflects its ability at this level — no adjustment needed.
                # Fixes Turquoise Velocity: won C3 but modal class was C4 → false step-up.
                has_today_class_run = (recent_classes["race_class"] == today_class).any()

                if not has_today_class_run:
                    # Determine old class: modal class from recent runs
                    class_counts = recent_classes["race_class"].value_counts()
                    old_class = int(class_counts.index[0])

                    if old_class != today_class:
                        # Gather runs in old class to compute competitiveness margin
                        old_class_runs = horse_class_runs[horse_class_runs["race_class"] == old_class]
                        old_class_runs = old_class_runs.sort_values("race_date", ascending=False).head(5)

                        if len(old_class_runs) >= CLASS_REL_MIN_RUNS:
                            # Compute mean residual for runs in old class
                            old_resids = []
                            for _, ocr in old_class_runs.iterrows():
                                ocr_ft = ocr.get("finish_time_seconds")
                                ocr_dist = ocr.get("distance")
                                if pd.isna(ocr_ft) or pd.isna(ocr_dist) or ocr_ft <= 0:
                                    continue
                                ocr_dist = int(ocr_dist)
                                ocr_going = _map_going(ocr.get("going", "G"))
                                ocr_wt = ocr.get("actual_weight")
                                ocr_wband = weight_band(int(ocr_wt)) if pd.notna(ocr_wt) and ocr_wt > 0 else "121-125"
                                ocr_rc = ocr.get("race_course", "A")
                                ocr_tt = ocr.get("track_type", "Turf")
                                ocr_cband = class_band(int(old_class))
                                ocr_et, _, _, _ = lookup_expected_time(
                                    class_fine, fine, coarse, ultra,
                                    ocr_dist, ocr_going, ocr_wband, ocr_rc, ocr_tt, ocr_cband)
                                if pd.notna(ocr_et) and ocr_et > 0:
                                    old_resids.append(ocr_ft - ocr_et)

                            if len(old_resids) >= CLASS_REL_MIN_RUNS:
                                old_margin = float(np.mean(old_resids))
                                # old_margin < 0 means horse was BETTER than class average (dominant)
                                # old_margin > 0 means horse was WORSE than class average (weak)

                                # Get ET gap between old class and today's class at today's distance
                                old_et, _, _, _ = lookup_expected_time(
                                    class_fine, fine, coarse, ultra,
                                    distance, going, wband, course, track_type, class_band(int(old_class)))
                                new_et, _, _, _ = lookup_expected_time(
                                    class_fine, fine, coarse, ultra,
                                    distance, going, wband, course, track_type, cband)

                                if pd.notna(old_et) and pd.notna(new_et):
                                    et_gap = old_et - new_et  # positive when stepping UP (old class is slower)

                                    if abs(et_gap) >= CLASS_REL_ET_GAP_FLOOR:
                                        if old_class > today_class:
                                            # STEP UP: higher class number = lower class quality
                                            # Penalty = ET gap adjusted by old-class margin
                                            # Good in old class (margin < 0) → penalty reduced
                                            # Weak in old class (margin > 0) → penalty increased
                                            raw_pen = et_gap + old_margin * 0.5
                                            class_trans_pen = max(0.0, min(raw_pen, CLASS_REL_STEP_UP_CAP))
                                        else:
                                            # STEP DOWN: dropping to easier class
                                            # Bonus = fraction of ET gap, scaled by dominance
                                            # Dominant in old class (margin < 0) → bigger bonus
                                            raw_bonus = -abs(et_gap) * CLASS_REL_DOMINANCE_W
                                            if old_margin < 0:
                                                raw_bonus -= abs(old_margin) * 0.3
                                            class_trans_pen = max(CLASS_REL_STEP_DN_CAP, min(0.0, raw_bonus))

                                        if pd.notna(proj) and class_trans_pen != 0.0:
                                            proj += class_trans_pen

        # ── v3.3 Adjustment D: consistency flag ──
        consistency_flag = ""
        if pd.notna(residual_std) and residual_std > CONSISTENCY_STD_THRESH:
            consistency_flag = "INCONSISTENT"

        horse_data.append({
            "horse_no": h["horse_no"], "horse_name": h["horse_name"],
            "weight": h["weight"], "draw": h["draw"],
            "rating": h["rating"], "jockey": h.get("jockey", ""),
            "trainer": h.get("trainer", ""),
            "expected_time": round(et, 2) if pd.notna(et) else None,
            "ref_tier": tier, "ref_n": n_ref, "ref_std": round(std_ref, 3) if pd.notna(std_ref) else None,
            "ability_v3": round(ability_v3, 3) if pd.notna(ability_v3) else None,
            "shrunk_resid": round(shrunk_resid, 3) if pd.notna(shrunk_resid) else None,
            "raw_resid": round(raw_resid, 3) if pd.notna(raw_resid) else None,
            "effective_resid": round(effective_resid, 3) if pd.notna(effective_resid) else None,
            "shrink_factor": round(shrink_factor, 2),
            "shrink_k": int(shrink_k) if pd.notna(shrink_k) else 5,
            "n_runs": n_runs, "confidence": confidence,
            "draw_offset": d_off, "draw_found": d_found,
            "dominant_style": dom_style,
            "leader_frac": leader_frac, "front_frac": front_frac,
            "early_speed_z": early_speed_z,
            "style_entropy": style_entropy,
            "residual_std": residual_std,
            "best_residual": best_residual,
            "worst_residual": worst_residual,
            "n_sectional_runs": n_sectional_runs,
            "uncertainty_penalty": round(uncertainty_pen, 3),
            "surface_penalty": round(surface_pen, 3),
            "distance_penalty": round(distance_pen, 3),
            "forced_dist_penalty": round(forced_dist_pen, 3),
            "class_trans_penalty": round(class_trans_pen, 3),
            "n_same_surface": n_same_surface_runs,
            "n_nearby_dist": n_nearby_dist_runs,
            "consistency_flag": consistency_flag,
            "trajectory": trajectory_info.get("trajectory", "Insufficient"),
            "traj_delta": trajectory_info.get("delta"),
            "traj_bonus": trajectory_info.get("bonus", 0.0),
            "traj_penalty": trajectory_info.get("penalty", 0.0),
            "traj_slope": trajectory_info.get("trend_slope"),
            "proj_pre_pace": round(proj, 3) if pd.notna(proj) else None,
            "pace_adj": 0.0,
            "projected_time": None,
            "proj_final_sec": None,
            "sec_source": "none",
            "sec_horse_mean": None,
            "sec_horse_std": None,
            "sec_n": 0,
            "sec_class_mean": None,
            "sec_pace_adj": 0.0,
        })

    pace_label, pace_score, pace_reasons, leader_names = predict_race_pace_v3(
        horse_data, distance, going, venue=MEETING_VENUE)

    # v3.4.6 (X): count leaders for congestion adjustment
    n_leaders = len(leader_names)

    # v3.3: empirical pace-style multiplier + Adjustment C (gate on style confidence)
    for hd in horse_data:
        mult = get_pace_multiplier(distance, pace_label, hd["dominant_style"])

        # ── v3.3 Adjustment C: gate pace multiplier for thin sectional data ──
        style_gated = False
        if hd["n_sectional_runs"] < STYLE_CONF_MIN_SECTIONAL:
            mult = 1.0  # neutral — insufficient data to trust style classification
            style_gated = True

        # ── v3.4.6 Adjustment X: leader congestion discount ──
        # When ≥3 leaders contest, the Leader multiplier is discounted toward 1.0.
        # With 5 leaders the mult moves 75% toward neutral, reflecting suicidal pace.
        leader_congestion_applied = False
        if (not style_gated and n_leaders >= LEADER_CONGESTION_MIN
                and hd["dominant_style"] == "Leader" and mult > 1.0):
            excess = n_leaders - (LEADER_CONGESTION_MIN - 1)  # 3→1, 4→2, 5→3
            discount = min(excess * LEADER_CONGESTION_SCALE, LEADER_CONGESTION_MAX_DISC)
            mult = mult + (1.0 - mult) * discount  # move toward 1.0
            leader_congestion_applied = True
        hd["leader_congestion"] = leader_congestion_applied

        pa = pace_adj_from_multiplier(hd["expected_time"], mult)
        hd["pace_adj"] = pa
        hd["pace_multiplier"] = mult
        hd["style_gated"] = style_gated
        if hd["proj_pre_pace"] is not None:
            hd["projected_time"] = round(hd["proj_pre_pace"] + pa, 2)

        # ── v3.3: projected final sectional ──
        if db is not None:
            sec_info = compute_projected_final_sectional(
                hd["horse_name"], db, distance, race["race_class"],
                pace_label, hd["dominant_style"])
            hd["proj_final_sec"] = sec_info["proj_sec"]
            hd["sec_source"]     = sec_info["source"]
            hd["sec_horse_mean"] = sec_info["horse_mean"]
            hd["sec_horse_std"]  = sec_info["horse_std"]
            hd["sec_n"]          = sec_info["n_sec"]
            hd["sec_class_mean"] = sec_info["class_mean"]
            hd["sec_pace_adj"]   = sec_info["pace_adj"]

    df = pd.DataFrame(horse_data)
    if df["projected_time"].notna().sum() >= 2:
        mu = df["projected_time"].mean(); sd = df["projected_time"].std()
        df["perf_z"] = round(-1 * (df["projected_time"] - mu) / sd, 3) if sd > 0 else 0.0
    else:
        df["perf_z"] = np.nan

    df = df.sort_values("projected_time", ascending=True, na_position="last")
    df["rank"] = range(1, len(df) + 1)

    return df, pace_label, pace_score, pace_reasons, leader_names


# ══════════════════════════════════════════════════════════════════════════════
# 6. Commentary
# ══════════════════════════════════════════════════════════════════════════════

def race_commentary(df, race, pace_label, pace_reasons, leader_names):
    lines = []
    valid = df[df["projected_time"].notna()]
    if len(valid) < 2:
        lines.append("Insufficient data — fewer than 2 horses have projections.")
        return lines

    std_s = valid["projected_time"].std()
    top = valid.iloc[0]; sec = valid.iloc[1]
    gap = sec["projected_time"] - top["projected_time"]

    if std_s < 0.3:
        lines.append(f"Field separation: LOW (σ={std_s:.2f}s). Very competitive; small edges only.")
    elif std_s < 0.6:
        lines.append(f"Field separation: MODERATE (σ={std_s:.2f}s). Some differentiation.")
    else:
        lines.append(f"Field separation: CLEAR (σ={std_s:.2f}s). Clear top tier emerges.")

    lines.append(f"Top-ranked: {top['horse_name']} (proj {top['projected_time']:.2f}s, "
                 f"ability {top['ability_v3']:+.3f}, SF={top['shrink_factor']:.2f}, "
                 f"{top['confidence']}). Gap to 2nd ({sec['horse_name']}): {gap:.2f}s.")

    lines.append(f"Predicted pace: {pace_label}. " + " ".join(pace_reasons))

    # Draw
    big_draw = df[(df["draw_found"] == True) & (df["draw_offset"].abs() > 0.1)]
    if len(big_draw):
        parts = [f"{r['horse_name']} (dr{r['draw']}, {r['draw_offset']:+.2f}s)"
                 for _, r in big_draw.iterrows()]
        lines.append(f"Significant draw impacts: {'; '.join(parts)}.")

    # v3.1: Shrinkage caution still flagged but threshold raised
    shrunk = df[(df["shrink_factor"] < 0.35) & df["projected_time"].notna()]
    if len(shrunk):
        parts = []
        for _, r in shrunk.iterrows():
            blend_note = " [blended]" if (r.get("shrink_factor", 0) >= BLEND_SF_THRESH
                                          and r.get("n_runs", 0) >= BLEND_N_THRESH) else ""
            k_note = f" k={r.get('shrink_k', 5)}" if r.get("shrink_k", 5) == 8 else ""
            parts.append(f"{r['horse_name']} (SF={r['shrink_factor']:.2f}{k_note}{blend_note})")
        lines.append(f"Caution — very thin data: {', '.join(parts)}. Projections uncertain.")

    # Hidden speed alerts
    fast_starters = df[(df["early_speed_z"] < -0.8) & (df["dominant_style"] != "Leader")]
    if len(fast_starters):
        parts = [f"{r['horse_name']} (esz={r['early_speed_z']:+.1f}, style={r['dominant_style']})"
                 for _, r in fast_starters.iterrows()]
        lines.append(f"Hidden speed: {'; '.join(parts)} — fast first-sectional but non-Leader style.")

    # No data
    nodata = df[df["projected_time"].isna()]
    if len(nodata):
        names = ", ".join(nodata["horse_name"].tolist())
        lines.append(f"No projection available: {names}.")

    # Risk analysis
    if "risk_score" in df.columns:
        high_risk = df[(df["risk_score"] > 55) & df["projected_time"].notna()]
        if len(high_risk):
            parts = []
            for _, r in high_risk.iterrows():
                drv = r.get("risk_drivers", [])
                drv_str = "; ".join(drv[:2]) if drv else "multiple factors"
                parts.append(f"{r['horse_name']} (risk={r['risk_score']:.0f}): {drv_str}")
            lines.append(f"High-risk profiles: {'; '.join(parts[:4])}.")

        divergent = df[(df["risk_score"] > 50) & (df["win_prob"] > 12)]
        if len(divergent):
            parts = [f"{r['horse_name']} (win={r['win_prob']:.0f}%, risk={r['risk_score']:.0f})"
                     for _, r in divergent.iterrows()]
            lines.append(f"Risk-reward divergence: {'; '.join(parts[:3])} "
                         f"— strong ability but fragile projection.")

        bankable = df[(df["risk_score"] < 30) & (df["win_prob"] > 10)]
        if len(bankable):
            parts = [f"{r['horse_name']} (win={r['win_prob']:.0f}%, risk={r['risk_score']:.0f})"
                     for _, r in bankable.iterrows()]
            lines.append(f"Bankable profiles: {'; '.join(parts[:3])} "
                         f"— solid projection with low fragility.")

    # v3.3 Adjustment D: inconsistency warnings
    if "consistency_flag" in df.columns:
        inconsistent = df[(df["consistency_flag"] == "INCONSISTENT") & df["projected_time"].notna()]
        if len(inconsistent):
            parts = [f"{r['horse_name']} (std={r['residual_std']:.2f}s, n={r['n_runs']})"
                     for _, r in inconsistent.iterrows()]
            lines.append(f"⚠ Inconsistent performers (residual σ>{CONSISTENCY_STD_THRESH:.2f}s): "
                         f"{'; '.join(parts[:5])}. Projections more volatile.")

    # v3.3 Adjustment B: uncertainty penalty flag
    if "uncertainty_penalty" in df.columns:
        penalised = df[(df["uncertainty_penalty"] > 0) & df["projected_time"].notna()]
        if len(penalised):
            parts = [f"{r['horse_name']} (+{r['uncertainty_penalty']:.3f}s, n={r['n_runs']})"
                     for _, r in penalised.iterrows()]
            lines.append(f"Uncertainty penalty applied (n<{UNCERTAINTY_N_THRESH}): {'; '.join(parts[:5])}.")

    # v3.3 Adjustment C: style gating flag
    if "style_gated" in df.columns:
        gated = df[(df["style_gated"] == True) & df["projected_time"].notna()]
        if len(gated):
            parts = [f"{r['horse_name']} (n_sec={r['n_sectional_runs']})"
                     for _, r in gated.iterrows()]
            lines.append(f"Pace multiplier gated (n_sec<{STYLE_CONF_MIN_SECTIONAL}): "
                         f"{'; '.join(parts[:5])}. Style-based pace adj set to neutral.")

    # v3.4.1 Adjustment L: surface mismatch flag
    if "surface_penalty" in df.columns:
        surf_penalised = df[(df["surface_penalty"] > 0) & df["projected_time"].notna()]
        if len(surf_penalised):
            parts = [f"{r['horse_name']} (+{r['surface_penalty']:.3f}s, {r.get('n_same_surface',0)} same-surface runs of {r['n_runs']})"
                     for _, r in surf_penalised.iterrows()]
            lines.append(f"⚠ Surface mismatch penalty: {'; '.join(parts[:5])}. "
                         f"Few/no runs on today's surface.")

    # v3.4.1 Adjustment M: distance mismatch flag
    if "distance_penalty" in df.columns:
        dist_penalised = df[(df["distance_penalty"] > 0) & df["projected_time"].notna()]
        if len(dist_penalised):
            parts = [f"{r['horse_name']} (+{r['distance_penalty']:.3f}s, {r.get('n_nearby_dist',0)} runs within ±{DISTANCE_PROXIMITY_M}m)"
                     for _, r in dist_penalised.iterrows()]
            lines.append(f"⚠ Distance mismatch penalty: {'; '.join(parts[:5])}. "
                         f"Few/no runs near today's distance.")

    # v3.4.2 Adjustment P: forced distance penalty flag
    if "forced_dist_penalty" in df.columns:
        fdp = df[(df["forced_dist_penalty"] > 0) & df["projected_time"].notna()]
        if len(fdp):
            parts = [f"{r['horse_name']} (+{r['forced_dist_penalty']:.3f}s)"
                     for _, r in fdp.iterrows()]
            lines.append(f"⚠ First time at today's distance: {'; '.join(parts[:5])}. "
                         f"No runs at exact race distance — untested.")

    # v3.4 Adjustment E/F: trajectory flags
    if "trajectory" in df.columns:
        improving = df[(df["trajectory"] == "Improving") & df["projected_time"].notna()]
        if len(improving):
            parts = [f"{r['horse_name']} (Δ={r['traj_delta']:+.2f}s, bonus={r['traj_bonus']:+.3f}s)"
                     for _, r in improving.iterrows()]
            lines.append(f"↑ Improving trajectory: {'; '.join(parts[:5])}.")

        declining = df[(df["trajectory"] == "Declining") & df["projected_time"].notna()]
        if len(declining):
            parts = [f"{r['horse_name']} (Δ={r['traj_delta']:+.2f}s, pen={r['traj_penalty']:+.3f}s)"
                     for _, r in declining.iterrows()]
            lines.append(f"↓ Declining trajectory: {'; '.join(parts[:5])}.")

    # v3.4.6 Adjustment X: leader congestion flag
    if "leader_congestion" in df.columns:
        congested = df[(df["leader_congestion"] == True) & df["projected_time"].notna()]
        if len(congested):
            parts = [f"{r['horse_name']} (mult discounted toward neutral)"
                     for _, r in congested.iterrows()]
            n_ldrs = df[df["dominant_style"] == "Leader"]["projected_time"].notna().sum()
            lines.append(f"⚠ Leader congestion ({n_ldrs} leaders): {'; '.join(parts[:5])}. "
                         f"Pace multiplier reduced — multi-leader burn-out expected.")

    # v3.4.7 Adjustment AB: class-relative ability flag
    if "class_trans_penalty" in df.columns:
        ct_penalised = df[(df["class_trans_penalty"] > 0.005) & df["projected_time"].notna()]
        if len(ct_penalised):
            parts = [f"{r['horse_name']} (+{r['class_trans_penalty']:.3f}s)"
                     for _, r in ct_penalised.iterrows()]
            lines.append(f"⚠ Class step-up adjustment: {'; '.join(parts[:5])}. "
                         f"Performance in old class suggests difficulty at new level.")

        ct_bonus = df[(df["class_trans_penalty"] < -0.005) & df["projected_time"].notna()]
        if len(ct_bonus):
            parts = [f"{r['horse_name']} ({r['class_trans_penalty']:+.3f}s)"
                     for _, r in ct_bonus.iterrows()]
            lines.append(f"↓ Class step-down bonus: {'; '.join(parts[:5])}. "
                         f"Dropping in class — old-class form projects competitive.")

    return lines


# ══════════════════════════════════════════════════════════════════════════════
# 7. PDF generation
# ══════════════════════════════════════════════════════════════════════════════

def generate_pdf(all_results, races, path):
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                     Paragraph, Spacer, PageBreak, KeepTogether)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4),
                            leftMargin=12*mm, rightMargin=12*mm,
                            topMargin=14*mm, bottomMargin=14*mm)
    styles = getSampleStyleSheet()

    # ── Load vet flags for inline display ────────────────────────────────
    import re as _re_vet
    _m_date = _re_vet.search(r'(\d{8})', path.name)
    _vet_lookup = {}  # {race_number: {horse_no: flag}}
    if _m_date:
        _vet_json = path.parent / f"vet_report_{_m_date.group(1)}.json"
        print(f"  Vet JSON path: {_vet_json}  exists={_vet_json.exists()}")
        if _vet_json.exists():
            try:
                with open(_vet_json, "r", encoding="utf-8") as _vf:
                    _vet_data = json.load(_vf)
                for _rd in _vet_data.get("races", []):
                    _rn = _rd["race_number"]
                    _vet_lookup[_rn] = {}
                    for _h in _rd.get("horses", []):
                        if _h.get("max_flag") in ("RED", "AMBER", "INFO"):
                            _vet_lookup[_rn][_h["horse_no"]] = _h["max_flag"]
                _total_flags = sum(len(v) for v in _vet_lookup.values())
                print(f"  Vet loaded: {_total_flags} flags across {len(_vet_lookup)} races")
            except Exception as e:
                print(f"  WARNING: Vet load failed: {e}")

    title_style = ParagraphStyle("RaceTitle", parent=styles["Heading1"],
                                  fontSize=13, spaceAfter=2*mm, leading=16)
    subtitle_style = ParagraphStyle("RaceSub", parent=styles["Normal"],
                                     fontSize=9, spaceAfter=1*mm, leading=11,
                                     textColor=colors.HexColor("#444444"))
    commentary_style = ParagraphStyle("Commentary", parent=styles["Normal"],
                                       fontSize=8.5, leading=11, spaceBefore=2*mm,
                                       spaceAfter=1*mm, leftIndent=3*mm)
    header_style = ParagraphStyle("TblHdr", parent=styles["Normal"],
                                   fontSize=7.5, leading=9, alignment=TA_CENTER,
                                   textColor=colors.white)
    cell_style = ParagraphStyle("TblCell", parent=styles["Normal"],
                                 fontSize=7.5, leading=9, alignment=TA_RIGHT)
    cell_left = ParagraphStyle("TblCellL", parent=styles["Normal"],
                                fontSize=7.5, leading=9, alignment=TA_LEFT)
    cell_center = ParagraphStyle("TblCellC", parent=styles["Normal"],
                                  fontSize=7.5, leading=9, alignment=TA_CENTER)

    page_title = ParagraphStyle("PageTitle", parent=styles["Title"],
                                 fontSize=16, spaceAfter=4*mm)

    story = []

    story.append(Paragraph(MEETING_TITLE, page_title))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  "
                           f"Model: v3.4.8 (race-median draw offsets, ability-anchored, class-relative, traj reliability, draw field-size scaling)",
                           subtitle_style))
    story.append(Paragraph(f"Going: Turf={TURF_GOING_ASSUMED}, AWT={AWT_GOING_ASSUMED}  |  "
                           f"Recency λ={RECENCY_LAMBDA}  |  "
                           f"Uncertainty: +{UNCERTAINTY_BASE}s/√n (n<{UNCERTAINTY_N_THRESH})  |  "
                           f"Style gate: n_sec≥{STYLE_CONF_MIN_SECTIONAL}  |  "
                           f"Draw: shrinkage k={DRAW_SHRINKAGE_K}",
                           subtitle_style))
    story.append(Spacer(1, 4*mm))

    col_headers = ["Rk", "No", "Horse", "Wt", "Dr", "Rtg",
                   "Exp T", "Proj T", "Win%", "Ability", "Eff\nResid", "SF",
                   "Draw\nOff", "Pace\nAdj", "Fin\nSec", "ESZ", "Style", "Risk", "Vet"]
    col_widths  = [10*mm, 9*mm, 42*mm, 10*mm, 9*mm, 10*mm,
                   14*mm, 14*mm, 13*mm, 14*mm, 13*mm, 10*mm,
                   12*mm, 12*mm, 15*mm, 11*mm, 17*mm, 13*mm, 14*mm]

    HDR_BG = colors.HexColor("#2C3E50")
    ROW_ALT = colors.HexColor("#F0F4F8")
    TOP1_BG = colors.HexColor("#D4EFDF")
    TOP2_BG = colors.HexColor("#EBF5FB")
    TOP3_BG = colors.HexColor("#FEF9E7")

    for (df, pace_label, pace_score, pace_reasons, leader_names), race in zip(all_results, races):

        elements_for_race = []

        tag = "AWT" if race["is_awt"] else "Turf"
        cls_str = f"Class {race['race_class']}" if race["race_class"] else "Group"
        going_str = AWT_GOING_ASSUMED if race["is_awt"] else TURF_GOING_ASSUMED
        header_text = f"Race {race['race_number']} — {race['race_name']}"
        sub_text = (f"{race['distance']}m {tag}, Course {race['race_course']}, "
                    f"{cls_str} ({race.get('rating_range','')})  |  "
                    f"Going: {going_str}  |  Pace: {pace_label} ({pace_score:+.2f}s)")

        elements_for_race.append(Paragraph(header_text, title_style))
        elements_for_race.append(Paragraph(sub_text, subtitle_style))

        table_data = []
        hdr_row = [Paragraph(f"<b>{h}</b>", header_style) for h in col_headers]
        table_data.append(hdr_row)

        for _, r in df.iterrows():
            def fv(val, fmt="{:.2f}", na="-"):
                if val is None or (isinstance(val, float) and (np.isnan(val) or pd.isna(val))):
                    return na
                return fmt.format(val)

            def fvp(val, fmt="{:+.3f}", na="-"):
                return fv(val, fmt, na)

            wp = r.get("win_prob", 0)
            if wp >= 20:
                wp_display = f'<font color="#27AE60"><b>{wp:.0f}%</b></font>'
            elif wp >= 10:
                wp_display = f'{wp:.0f}%'
            else:
                wp_display = f'{wp:.0f}%' if wp > 0 else '-'

            risk_val = r.get("risk_score", 0)
            risk_tier = str(r.get("risk_tier", ""))
            if risk_tier == "High":
                risk_display = f'<font color="#E74C3C"><b>{risk_val:.0f} H</b></font>'
            elif risk_tier == "Moderate":
                risk_display = f'<font color="#E67E22">{risk_val:.0f} M</font>'
            else:
                risk_display = f'<font color="#27AE60">{risk_val:.0f} L</font>'

            esz_val = r.get("early_speed_z", 0)
            if pd.notna(esz_val) and esz_val != 0:
                esz_str = f"{esz_val:+.1f}"
                if esz_val < -0.8:
                    esz_str = f'<font color="#27AE60"><b>{esz_str}</b></font>'
            else:
                esz_str = "-"

            # v3.3: projected final sectional display with contextual color
            fin_sec_val = r.get("proj_final_sec")
            sec_src = str(r.get("sec_source", "none"))
            if fin_sec_val is not None and not (isinstance(fin_sec_val, float) and np.isnan(fin_sec_val)):
                sec_n = int(r.get("sec_n", 0))
                # Color: green=fast (< class_mean-0.3), red=slow (> class_mean+0.3), else neutral
                cls_mean = r.get("sec_class_mean")
                if cls_mean is not None and not (isinstance(cls_mean, float) and np.isnan(cls_mean)):
                    if fin_sec_val < cls_mean - 0.3:
                        fin_sec_str = f'<font color="#27AE60"><b>{fin_sec_val:.2f}</b></font>'
                    elif fin_sec_val > cls_mean + 0.3:
                        fin_sec_str = f'<font color="#E74C3C">{fin_sec_val:.2f}</font>'
                    else:
                        fin_sec_str = f'{fin_sec_val:.2f}'
                else:
                    fin_sec_str = f'{fin_sec_val:.2f}'
                # Add source indicator: * = horse, ~ = blend, c = class
                src_ind = {"horse": "", "blend": "~", "class": "c", "horse_thin": "~"}.get(sec_src, "?")
                fin_sec_str += f'<font size="5">{src_ind}</font>' if src_ind else ''
            else:
                fin_sec_str = "-"

            row_cells = [
                Paragraph(str(int(r["rank"])), cell_center),
                Paragraph(str(r["horse_no"]), cell_center),
                Paragraph(str(r["horse_name"]), cell_left),
                Paragraph(str(r["weight"]), cell_style),
                Paragraph(str(r["draw"]) if pd.notna(r["draw"]) else "-", cell_center),
                Paragraph(str(r["rating"]) if pd.notna(r["rating"]) else "-", cell_style),
                Paragraph(fv(r["expected_time"]), cell_style),
                Paragraph(f"<b>{fv(r['projected_time'])}</b>" if pd.notna(r.get("projected_time")) else "-", cell_style),
                Paragraph(wp_display, cell_center),
                Paragraph(fvp(r.get("ability_v3")), cell_style),
                Paragraph(fvp(r.get("effective_resid")), cell_style),
                Paragraph(fv(r.get("shrink_factor"), "{:.2f}"), cell_center),
                Paragraph(fvp(r.get("draw_offset")) if r.get("draw_found") else "-", cell_center),
                Paragraph(fvp(r.get("pace_adj")) if r.get("pace_adj", 0) != 0 else "0", cell_center),
                Paragraph(fin_sec_str, cell_center),
                Paragraph(esz_str, cell_center),
                Paragraph(str(r.get("dominant_style", "?"))[:8], cell_center),
                Paragraph(risk_display, cell_center),
            ]

            # ── Vet flag column ──
            _race_vet = _vet_lookup.get(race["race_number"], {})
            _vflag = _race_vet.get(int(r["horse_no"]), "")
            if _vflag == "RED":
                _vet_disp = '<font color="#C0392B"><b>RED</b></font>'
            elif _vflag == "AMBER":
                _vet_disp = '<font color="#D35400">AMB</font>'
            elif _vflag == "INFO":
                _vet_disp = '<font color="#1A5276">INF</font>'
            else:
                _vet_disp = '-'
            row_cells.append(Paragraph(_vet_disp, cell_center))

            table_data.append(row_cells)

        tbl = Table(table_data, colWidths=col_widths, repeatRows=1)

        ts_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), HDR_BG),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ]

        n_rows = len(df)
        if n_rows >= 1: ts_cmds.append(("BACKGROUND", (0, 1), (-1, 1), TOP1_BG))
        if n_rows >= 2: ts_cmds.append(("BACKGROUND", (0, 2), (-1, 2), TOP2_BG))
        if n_rows >= 3: ts_cmds.append(("BACKGROUND", (0, 3), (-1, 3), TOP3_BG))

        for i in range(4, n_rows + 1):
            if i % 2 == 0:
                ts_cmds.append(("BACKGROUND", (0, i), (-1, i), ROW_ALT))

        tbl.setStyle(TableStyle(ts_cmds))
        elements_for_race.append(tbl)

        comments = race_commentary(df, race, pace_label, pace_reasons, leader_names)
        for c in comments:
            elements_for_race.append(Paragraph(f"• {c}", commentary_style))

        story.append(KeepTogether(elements_for_race))
        story.append(PageBreak())

    # ── Vet record appendix ──────────────────────────────────────────────────
    _append_vet_appendix(story, path, subtitle_style, header_style,
                         cell_style, cell_left, cell_center,
                         HDR_BG, ROW_ALT, TOP1_BG)

    doc.build(story)
    print(f"  ✓ PDF saved: {path.name}")


def _append_vet_appendix(story, pdf_path, subtitle_style, header_style,
                         cell_style, cell_left, cell_center,
                         HDR_BG, ROW_ALT, TOP1_BG):
    """Append a vet-record summary page if vet_report JSON exists."""
    import re as _re
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Table, TableStyle, Paragraph, Spacer, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_CENTER

    # Derive date_compact from PDF filename (race_day_report_YYYYMMDD_v3.4.8.pdf)
    m = _re.search(r'(\d{8})', pdf_path.name)
    if not m:
        return
    date_compact = m.group(1)
    vet_json = pdf_path.parent / f"vet_report_{date_compact}.json"
    if not vet_json.exists():
        return

    try:
        with open(vet_json, "r", encoding="utf-8") as f:
            vet_data = json.load(f)
    except Exception:
        return

    # Collect flagged horses
    flagged = []
    for rdata in vet_data.get("races", []):
        rn = rdata["race_number"]
        for h in rdata.get("horses", []):
            if h.get("max_flag") in ("RED", "AMBER", "INFO"):
                top_rec = max(h.get("records", [{}]),
                              key=lambda r: r.get("concern_score", 0), default={})
                flagged.append((rn, h["horse_no"], h["horse_name"],
                                h["max_flag"], top_rec.get("category", ""),
                                top_rec.get("details", "")[:70],
                                top_rec.get("days_ago")))

    if not flagged:
        return

    flagged.sort(key=lambda x: ({"RED": 0, "AMBER": 1, "INFO": 2}.get(x[3], 3), x[0], x[1]))

    RED_BG   = colors.HexColor("#FDEDEC")
    RED_TXT  = colors.HexColor("#C0392B")
    AMBER_BG = colors.HexColor("#FEF9E7")
    AMBER_TXT= colors.HexColor("#D35400")
    INFO_BG  = colors.HexColor("#EBF5FB")
    INFO_TXT = colors.HexColor("#1A5276")

    styles = getSampleStyleSheet()
    vet_title = ParagraphStyle("VetTitle", parent=styles["Title"],
                                fontSize=14, spaceAfter=3*mm)
    vet_note  = ParagraphStyle("VetNote", parent=styles["Normal"],
                                fontSize=8, leading=10,
                                textColor=colors.HexColor("#666666"),
                                spaceAfter=3*mm)
    vet_cell  = ParagraphStyle("VetCell", parent=styles["Normal"],
                                fontSize=7.5, leading=9)
    vet_bold  = ParagraphStyle("VetBold", parent=styles["Normal"],
                                fontSize=7.5, leading=9,
                                fontName="Helvetica-Bold")
    hdr_white = ParagraphStyle("VetHdr", parent=styles["Normal"],
                                fontSize=7.5, leading=9,
                                fontName="Helvetica-Bold",
                                textColor=colors.white)

    story.append(Paragraph("APPENDIX — VET RECORD ALERTS", vet_title))
    story.append(Paragraph(
        f"Source: HKJC veterinary records | Race date: {vet_data.get('race_date', '')} | "
        f"Scraped: {vet_data.get('scraped_at', '')}",
        vet_note))
    story.append(Paragraph(
        "<b>RED</b> = Recent physical/respiratory/cardiac injury (&lt;90 days)   "
        "<b>AMBER</b> = Cleared-recently or semi-recent (90-180 days)   "
        "<b>INFO</b> = Historical (180-400 days)",
        vet_note))

    def hp(text):
        return Paragraph(f"<b>{text}</b>", hdr_white)

    rows = [[hp("Race"), hp("No"), hp("Horse"), hp("Alert"),
             hp("Category"), hp("Top Concern"), hp("Days Ago")]]

    flag_labels = {"RED": "HIGH", "AMBER": "MODERATE", "INFO": "INFO"}
    style_cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0), HDR_BG),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]

    for i, (rn, hno, hname, flg, cat, details, d_ago) in enumerate(flagged, 1):
        fc = (RED_TXT.hexval() if flg == "RED" else
              AMBER_TXT.hexval() if flg == "AMBER" else INFO_TXT.hexval())
        bg = (RED_BG if flg == "RED" else AMBER_BG if flg == "AMBER" else INFO_BG)
        style_cmds.append(("BACKGROUND", (0, i), (-1, i), bg))

        rows.append([
            Paragraph(f"R{rn}", vet_cell),
            Paragraph(str(hno), vet_cell),
            Paragraph(hname, vet_bold),
            Paragraph(f'<font color="{fc}"><b>{flag_labels.get(flg, flg)}</b></font>', vet_cell),
            Paragraph(cat, vet_cell),
            Paragraph(details, vet_cell),
            Paragraph(f"{d_ago}d" if d_ago is not None else "—", vet_cell),
        ])

    tbl = Table(rows, colWidths=[14*mm, 10*mm, 44*mm, 26*mm, 26*mm, 120*mm, 18*mm])
    tbl.setStyle(TableStyle(style_cmds))
    story.append(tbl)
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(
        f"Total: {sum(1 for f in flagged if f[3]=='RED')} RED, "
        f"{sum(1 for f in flagged if f[3]=='AMBER')} AMBER, "
        f"{sum(1 for f in flagged if f[3]=='INFO')} INFO alerts across "
        f"{len(set(f[0] for f in flagged))} races",
        vet_note))


# ══════════════════════════════════════════════════════════════════════════════
# 8. Text report
# ══════════════════════════════════════════════════════════════════════════════

def generate_text_report(all_results, races, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("=" * 125 + "\n")
        f.write(f"RACE DAY ANALYSIS — {MEETING_TITLE}\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"Model: v3.4.8 (race-median draw offsets, ability-anchored, class-relative, traj reliability, draw field-size scaling, "
                f"recency λ={RECENCY_LAMBDA}, "
                f"uncertainty +{UNCERTAINTY_BASE}s/√n for n<{UNCERTAINTY_N_THRESH} "
                f"({UNCERTAINTY_BASE_V34}s for strong debuts), "
                f"style gate n_sec≥{STYLE_CONF_MIN_SECTIONAL}, "
                f"consistency σ>{CONSISTENCY_STD_THRESH}s, "
                f"AWT→Turf ×{AWT_TURF_DISCOUNT}, "
                f"HV↔ST ×{HV_ST_DISCOUNT}, "
                f"traj reliability gate n≤{TRAJ_RELIABILITY_MAX_N} σ>{TRAJ_RELIABILITY_STD}s)\n")
        f.write(f"Going assumed: Turf = {TURF_GOING_ASSUMED}, AWT = {AWT_GOING_ASSUMED}\n")
        f.write(f"Draw: Bayesian shrinkage k={DRAW_SHRINKAGE_K}, safety cap ±{DRAW_MAX_OFFSET_GLOBAL}s\n")
        f.write("=" * 125 + "\n\n")

        for (df, pace_label, pace_score, pace_reasons, leader_names), race in zip(all_results, races):
            cls_str = f"Class {race['race_class']}" if race['race_class'] else "Group"
            surface = "AWT" if race["is_awt"] else "Turf"
            going_str = AWT_GOING_ASSUMED if race["is_awt"] else TURF_GOING_ASSUMED
            f.write("=" * 125 + "\n")
            f.write(f"RACE {race['race_number']} — {race['race_name']}\n")
            f.write(f"{race['distance']}m {surface}, Course {race['race_course']}, "
                    f"{cls_str}  |  Going: {going_str}\n")
            f.write(f"Pace: {pace_label} ({pace_score:+.2f}s)\n")
            f.write("-" * 125 + "\n")

            hdr = (f"{'Rk':>3s}  {'Horse':<22s} {'Wt':>3s} {'Dr':>3s} {'Rtg':>4s}  "
                   f"{'ExpTm':>6s} {'ProjT':>6s} {'Win%':>5s} {'AbilV3':>7s} {'EffRes':>7s} "
                   f"{'SF':>4s} {'DrOff':>6s} {'PcAdj':>6s} {'FinSec':>6s} {'ESZ':>5s}  "
                   f"{'Style':<8s} {'Risk':<8s} {'Flags':<12s}\n")
            f.write(hdr)
            f.write("-" * 125 + "\n")

            for _, r in df.iterrows():
                et_s = f"{r['expected_time']:.2f}" if pd.notna(r["expected_time"]) else "  -"
                pt_s = f"{r['projected_time']:.2f}" if pd.notna(r["projected_time"]) else "  -"
                wp_s = f"{r.get('win_prob', 0):.0f}%" if r.get('win_prob', 0) > 0 else "  -"
                ab_s = f"{r['ability_v3']:+.3f}" if pd.notna(r.get("ability_v3")) else "   -"
                er_s = f"{r['effective_resid']:+.3f}" if pd.notna(r.get("effective_resid")) else "   -"
                sf_s = f"{r['shrink_factor']:.2f}" if pd.notna(r.get("shrink_factor")) else " -"
                do_s = f"{r['draw_offset']:+.3f}" if r.get("draw_found") else "   -"
                pa_s = f"{r['pace_adj']:+.3f}" if r.get("pace_adj", 0) != 0 else "   0"
                esz_s = f"{r.get('early_speed_z', 0):+.1f}" if r.get("early_speed_z", 0) != 0 else "  -"
                st_s = str(r.get("dominant_style", "?"))[:8]
                rt = str(r.get('risk_tier', '?'))
                risk_s = f"{r.get('risk_score', 0):.0f} {rt[0]}" if pd.notna(r.get('risk_score')) else "  -"

                # Final sectional
                fs_val = r.get("proj_final_sec")
                fs_s = f"{fs_val:.2f}" if (fs_val is not None and not (isinstance(fs_val, float) and np.isnan(fs_val))) else "  -"

                # Flags: consistency + uncertainty + surface/distance penalties + style-gated
                flags = []
                if r.get("consistency_flag") == "INCONSISTENT":
                    flags.append("INC")
                if r.get("uncertainty_penalty", 0) > 0:
                    flags.append(f"U+{r['uncertainty_penalty']:.2f}")
                if r.get("surface_penalty", 0) > 0:
                    flags.append(f"SP+{r['surface_penalty']:.2f}")
                if r.get("distance_penalty", 0) > 0:
                    flags.append(f"DP+{r['distance_penalty']:.2f}")
                if r.get("forced_dist_penalty", 0) > 0:
                    flags.append(f"FDP+{r['forced_dist_penalty']:.2f}")
                if r.get("class_trans_penalty", 0) > 0:
                    flags.append(f"CTP+{r['class_trans_penalty']:.2f}")
                if r.get("leader_congestion"):
                    flags.append("LC")
                if r.get("style_gated"):
                    flags.append("SG")
                traj = r.get("trajectory", "")
                if traj == "Improving":
                    flags.append(f"↑{r.get('traj_bonus', 0):+.2f}")
                elif traj == "Declining":
                    flags.append(f"↓{r.get('traj_penalty', 0):+.2f}")
                flag_s = ",".join(flags) if flags else "-"

                f.write(f"{int(r['rank']):>3d}  {r['horse_name']:<22s} "
                        f"{r['weight']:>3d} {str(r['draw']) if pd.notna(r['draw']) else '-':>3s} "
                        f"{str(r['rating']) if pd.notna(r['rating']) else '-':>4s}  "
                        f"{et_s:>6s} {pt_s:>6s} {wp_s:>5s} {ab_s:>7s} {er_s:>7s} "
                        f"{sf_s:>4s} {do_s:>6s} {pa_s:>6s} {fs_s:>6s} {esz_s:>5s}  "
                        f"{st_s:<8s} {risk_s:<8s} {flag_s:<12s}\n")

            # Final sectional detail
            f.write("\n  FINAL SECTIONAL ANALYSIS:\n")
            for _, r in df.iterrows():
                if pd.notna(r.get("projected_time")):
                    fs_val = r.get("proj_final_sec")
                    fs_str = f"{fs_val:.2f}s" if (fs_val is not None and not (isinstance(fs_val, float) and np.isnan(fs_val))) else "n/a"
                    h_mean = r.get("sec_horse_mean")
                    h_std = r.get("sec_horse_std")
                    h_mean_s = f"hist={h_mean:.2f}s" if (h_mean is not None and not (isinstance(h_mean, float) and np.isnan(h_mean))) else "hist=n/a"
                    h_std_s = f"±{h_std:.2f}" if (h_std is not None and not (isinstance(h_std, float) and np.isnan(h_std))) else ""
                    cls_mean = r.get("sec_class_mean")
                    cls_s = f"class={cls_mean:.2f}s" if (cls_mean is not None and not (isinstance(cls_mean, float) and np.isnan(cls_mean))) else ""
                    src = r.get("sec_source", "none")
                    pace_adj = r.get("sec_pace_adj", 0)
                    pa_note = f"pace_adj={pace_adj:+.2f}s" if pace_adj != 0 else ""
                    f.write(f"    {r['horse_name']:<22s}  proj={fs_str}  "
                            f"{h_mean_s}{h_std_s}  {cls_s}  "
                            f"n={r.get('sec_n', 0)}  src={src}  {pa_note}\n")

            # Risk analysis detail
            f.write("\n  RISK ANALYSIS (top contenders + flagged horses):\n")
            for _, r in df.iterrows():
                if pd.notna(r.get("projected_time")) and (
                    int(r["rank"]) <= 3 or r.get("risk_score", 0) > 55):
                    drivers = r.get("risk_drivers", [])
                    drv_str = "; ".join(drivers[:3]) if drivers else "Minimal risk"
                    f.write(f"    {r['horse_name']:<22s} "
                            f"Win={r.get('win_prob', 0):>4.0f}%  "
                            f"Risk={r.get('risk_score', 0):>4.0f} ({r.get('risk_tier', '?'):<8s})  "
                            f"Drivers: {drv_str}\n")

            f.write("\n")
            for c in race_commentary(df, race, pace_label, pace_reasons, leader_names):
                f.write(f"  • {c}\n")
            f.write("\n")

    print(f"  ✓ Text saved: {path.name}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 80)
    print(f"RACE DAY ANALYSIS (v3.4.8) — {MEETING_TITLE}")
    print("=" * 80)
    print(f"\nv3.4.8 fix:")
    print(f"  AE. Draw offsets: race-median method (FT - race_median) replaces FT - expected_time")
    print(f"       Old method gave statistically insignificant, physically implausible gate benefits")
    print(f"\nv3.4.7 fixes:")
    print(f"  AB. Class-relative ability: old-class margin → ET gap → step-up pen / step-down bonus")
    print(f"  AC. Trajectory reliability gate: n≤{TRAJ_RELIABILITY_MAX_N} + σ>{TRAJ_RELIABILITY_STD}s → Insufficient")
    print(f"  AD. Draw field-size scaling: outer draws amplified when field≥{DRAW_FIELD_MIN}")
    print(f"  AE. Ability-anchored: projection driven by horse's own contextualised times, no shrinkage")
    print(f"\nv3.4.6 systematic fixes:")
    print(f"  A. Recency decay: λ={RECENCY_LAMBDA} (most recent run = weight 1.0)")
    print(f"  B. Uncertainty penalty: +{UNCERTAINTY_BASE}s/√n for n<{UNCERTAINTY_N_THRESH} (strong debuts: {UNCERTAINTY_BASE_V34}s)")
    print(f"  C. Pace multiplier gated: n_sectional_runs ≥ {STYLE_CONF_MIN_SECTIONAL}")
    print(f"  D. Consistency flag: residual σ > {CONSISTENCY_STD_THRESH}s")
    print(f"  E. Trajectory detection: last-2 vs prior-3 baseline + OLS slope")
    print(f"  F. Trajectory bonus/penalty: improve cap={TRAJ_BONUS_CAP}s, decline cap={TRAJ_DECLINE_CAP}s")
    print(f"  G. Shrinkage override for improvers: {TRAJ_SF_OVERRIDE_WT:.0%} raw + {1-TRAJ_SF_OVERRIDE_WT:.0%} shrunk")
    print(f"  H. Reduced uncertainty for strong debuts: threshold={STRONG_DEBUT_THRESH}s")
    print(f"  I. AWT→Turf discount: ×{AWT_TURF_DISCOUNT} weight, traj haircut ×{AWT_TRAJ_HAIRCUT}")
    print(f"  J. Trajectory consistency: last-2 must agree in direction vs baseline")
    print(f"  K. HV↔ST venue discount: ×{HV_ST_DISCOUNT} weight, traj haircut ×{VENUE_TRAJ_HAIRCUT}")
    print(f"  Going: Turf = {TURF_GOING_ASSUMED}")

    # 1. Parse
    print("\n[1] Parsing race card …")
    if USE_SCRAPED:
        races = parse_scraped_race_card(RACE_CARD)
    else:
        races = parse_race_card(RACE_CARD)
    for r in races:
        surface = "AWT" if r["is_awt"] else "Turf"
        print(f"  R{r['race_number']:>2d}  {r['distance']:>4d}m  {surface:>4s}  "
              f"Course={r['race_course']}  Class={r['race_class'] or 'Grp'}  "
              f"Runners={len(r['horses'])}")

    # 2. Load references
    print("\n[2] Loading v3 references + historical DB …")
    class_fine, fine, coarse, ultra, draw_off, abilities = load_references_v3()
    print(f"  ClassFine: {len(class_fine)}, Fine: {len(fine)}, "
          f"Coarse: {len(coarse)}, Ultra: {len(ultra)}")
    print(f"  Draw offsets: {len(draw_off)}, Abilities: {len(abilities)} horses")

    # v3.3: load historical DB for recency computation + final sectional
    db = pd.read_excel(DB_FILE)
    print(f"  Historical DB: {len(db)} records loaded from {DB_FILE.name}")

    # 3. Project
    print("\n[3] Projecting performances (v3.4 model) …")
    all_results = []
    for race in races:
        df, pace_label, pace_score, pace_reasons, leader_names = project_race(
            race, class_fine, fine, coarse, ultra, draw_off, abilities, db=db)
        df = enrich_with_risk(df, race)
        all_results.append((df, pace_label, pace_score, pace_reasons, leader_names))
        matched = df["projected_time"].notna().sum()
        top_name = df.iloc[0]["horse_name"] if len(df) else "?"
        top_time = df.iloc[0]["projected_time"] if len(df) else "?"
        # v3.3: show flags summary
        n_unc = (df["uncertainty_penalty"] > 0).sum() if "uncertainty_penalty" in df.columns else 0
        n_inc = (df["consistency_flag"] == "INCONSISTENT").sum() if "consistency_flag" in df.columns else 0
        n_sg = (df["style_gated"] == True).sum() if "style_gated" in df.columns else 0
        print(f"  R{race['race_number']:>2d}: {matched}/{len(df)} projected | "
              f"pace={pace_label} ({pace_score:+.2f}s) | "
              f"top: {top_name} ({top_time}s) | "
              f"flags: {n_unc}unc {n_inc}inc {n_sg}sg")

    # 4. Output
    print("\n[4] Generating outputs …")
    generate_text_report(all_results, races, OUT_TEXT)
    generate_pdf(all_results, races, OUT_PDF)

    # 5. Console summary
    print("\n" + "=" * 80)
    print("TOP 3 PER RACE (v3.4.7)")
    print("=" * 80)
    for (df, pace_label, _, _, _), race in zip(all_results, races):
        valid = df[df["projected_time"].notna()]
        cls = f"C{race['race_class']}" if race["race_class"] else "Grp"
        surface = "AWT" if race["is_awt"] else "Turf"
        print(f"\nR{race['race_number']}  {race['distance']}m {surface} ({race['race_course']})  "
              f"{cls}  Pace={pace_label}")
        for _, r in valid.head(3).iterrows():
            style = str(r.get("dominant_style", "?"))[:6]
            esz = r.get("early_speed_z", 0)
            esz_s = f"esz={esz:+.1f}" if esz != 0 else "esz=n/a"
            rt = str(r.get('risk_tier', '?'))
            ef_r = r.get("effective_resid", 0)
            ef_s = f"eff={ef_r:+.3f}" if pd.notna(ef_r) else "eff=n/a"
            fs = r.get("proj_final_sec")
            fs_s = f"fin={fs:.2f}s" if (fs is not None and not (isinstance(fs, float) and np.isnan(fs))) else "fin=n/a"
            flags = []
            if r.get("consistency_flag") == "INCONSISTENT": flags.append("INC")
            if r.get("uncertainty_penalty", 0) > 0: flags.append("U")
            if r.get("style_gated"): flags.append("SG")
            traj = r.get("trajectory", "")
            if traj == "Improving": flags.append("↑IMP")
            elif traj == "Declining": flags.append("↓DEC")
            flag_s = f"[{','.join(flags)}]" if flags else ""
            print(f"  {int(r['rank']):>2d}. {r['horse_name']:<22s}  "
                  f"proj={r['projected_time']:.2f}s  "
                  f"win={r.get('win_prob', 0):.0f}%  "
                  f"risk={r.get('risk_score', 0):.0f}({rt[0]})  "
                  f"{ef_s}  "
                  f"{fs_s}  "
                  f"style={style}  {esz_s}  {flag_s}")

    print(f"\nDone. PDF → {OUT_PDF.name}")

    # 6. Save results JSON for dashboard
    save_results_json(all_results, races)


def save_results_json(all_results, races):
    """Save structured results JSON for the dashboard to read."""
    import json as _json
    import re as _re_json
    out_json = OUT_PDF.parent / OUT_PDF.name.replace(".pdf", ".json")

    # Load vet flags if vet report exists
    _vet_flags = {}  # {race_number: {horse_no: flag}}
    _m_d = _re_json.search(r'(\d{8})', OUT_PDF.name)
    if _m_d:
        _vj = OUT_PDF.parent / f"vet_report_{_m_d.group(1)}.json"
        if _vj.exists():
            try:
                with open(_vj, "r", encoding="utf-8") as _vf:
                    _vd = _json.load(_vf)
                for _rd in _vd.get("races", []):
                    _vet_flags[_rd["race_number"]] = {
                        h["horse_no"]: h["max_flag"]
                        for h in _rd.get("horses", [])
                        if h.get("max_flag") in ("RED", "AMBER", "INFO")
                    }
            except Exception:
                pass

    data = {
        "meeting_title": MEETING_TITLE,
        "meeting_venue": MEETING_VENUE,
        "model_version": "v3.4.8",
        "generated_at": datetime.now().isoformat(),
        "pdf_file": OUT_PDF.name,
        "text_file": OUT_TEXT.name,
        "races": [],
    }
    for (df, pace_label, pace_score, pace_reasons, leader_names), race in zip(all_results, races):
        valid = df[df["projected_time"].notna()]
        picks = []
        for _, r in valid.iterrows():
            flags = []
            if r.get("consistency_flag") == "INCONSISTENT": flags.append("INC")
            if r.get("uncertainty_penalty", 0) > 0: flags.append("U")
            if r.get("style_gated"): flags.append("SG")
            traj = r.get("trajectory", "")
            if traj == "Improving": flags.append("↑IMP")
            elif traj == "Declining": flags.append("↓DEC")
            picks.append({
                "rank": int(r["rank"]),
                "horse_no": int(r["horse_no"]),
                "horse_name": r["horse_name"],
                "projected_time": round(float(r["projected_time"]), 2),
                "win_prob": round(float(r.get("win_prob", 0)), 1),
                "risk_score": round(float(r.get("risk_score", 0)), 0),
                "risk_tier": str(r.get("risk_tier", "?")),
                "effective_resid": round(float(r.get("effective_resid", 0)), 3),
                "style": str(r.get("dominant_style", "?"))[:8],
                "early_speed_z": round(float(r.get("early_speed_z", 0)), 1),
                "proj_final_sec": round(float(r.get("proj_final_sec", 0)), 2) if pd.notna(r.get("proj_final_sec")) else None,
                "jockey": str(r.get("jockey", "")),
                "draw": int(r["draw"]) if pd.notna(r.get("draw")) else None,
                "weight": int(r["weight"]) if pd.notna(r.get("weight")) else None,
                "flags": flags,
                "vet_flag": _vet_flags.get(race["race_number"], {}).get(int(r["horse_no"]), ""),
            })
        data["races"].append({
            "race_number": race["race_number"],
            "race_name": race.get("race_name", ""),
            "distance": race["distance"],
            "race_course": race["race_course"],
            "race_class": race["race_class"],
            "is_awt": race["is_awt"],
            "pace": pace_label,
            "pace_score": round(pace_score, 2),
            "runners": len(df),
            "projected": int(valid.shape[0]),
            "picks": picks,
        })
    with open(out_json, "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  ✓ JSON saved: {out_json.name}")


if __name__ == "__main__":
    main()
