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
from collections import defaultdict
import os
import json
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE = Path(r"c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards")

# ── Per-meeting configuration (UPDATE THESE FOR EACH MEETING) ─────────────────
RACE_CARD    = BASE / "racecards" / "racecard_20260412.xlsx"
SHEET_NAME   = "All Races"                                          # v4.0: new racecard format
EXP_TIME_REF = Path(os.environ.get("TEMP", str(BASE))) / "expected_time_references_v3.xlsx"
ABILITY_FILE = BASE / "horse_ability_analysis_v3.xlsx"

OUT_PDF  = BASE / "reports" / "race_day_report_20260412_v4.4_sec.pdf"
OUT_TEXT = BASE / "reports" / "race_day_analysis_20260412_v4.4_sec.txt"
DB_FILE  = Path(os.environ.get("TEMP", str(BASE))) / "hkjc_results_updated.xlsx"

MEETING_TITLE = "SHA TIN — SUNDAY, 12 APRIL 2026"
MEETING_VENUE = "ST"

# ── Scratchings (race_number: [horse_name, ...]) ──────────────────────────────
SCRATCHINGS = {
}

# ── Standby Promotions (injected after card parse) ────────────────────────────
STANDBY_PROMOTIONS = {
}
TURF_GOING_ASSUMED = "Good"    # UPDATE per meeting (check HKJC going report)
AWT_GOING_ASSUMED  = "Good"    # UPDATE per meeting (if AWT races scheduled)

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

# V. v3.4.4 → v4.0: Distance-aware recency weighting
#    v4.0 Change 3: replaced symmetric weights with asymmetric step-up/step-down.
#    These old symmetric constants kept only for reference — actual logic uses
#    DIST_STEP_UP_* / DIST_STEP_DOWN_* above.
DIST_RECENCY_EXACT  = 1.0    # same distance (unchanged)
DIST_RECENCY_NEAR   = 0.70   # (legacy — superseded by asymmetric)
DIST_RECENCY_MED    = 0.40   # (legacy — superseded by asymmetric)
DIST_RECENCY_FAR    = 0.15   # (legacy — superseded by asymmetric)

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

# ── v4.0 Change 1: Runtime horse profiles (replaces static ability file) ──────
# All horse profile data (style, shrinkage, stats) now computed live from DB.
# ABILITY_FILE is no longer loaded — eliminates stale-data risk.
PROFILE_SHRINKAGE_K = 5        # Bayesian shrinkage k for runtime SF calculation

# ── v4.0 Change 2: Contextualised run performance ────────────────────────────
# Each historical run's residual is adjusted for circumstantial factors:
#   - Draw context: remove the draw offset effect from the raw time
#   - Wide-running cost: estimate energy cost of wide running from draw + position
# This produces a "true ability" residual stripped of trip luck.
WIDE_COST_OUTER_FRAC    = 0.65  # draws beyond this fraction of field = "wide draw"
WIDE_COST_LEADER_PEN    = 0.08  # seconds: wide draw + front position = crossed over
WIDE_COST_ONPACE_PEN    = 0.05  # seconds: wide draw + on-pace = some wide running
WIDE_COST_MIDFIELD_PEN  = 0.03  # seconds: wide draw + midfield = minor wide cost

# ── v4.0 Change 3: Asymmetric distance penalties ─────────────────────────────
# Stepping down (running shorter) vs stepping up (running longer) are different.
# Sprinters stepping up lose stamina; stayers stepping down lack early speed.
# Empirically: stepping up has higher failure rate than stepping down.
DIST_STEP_DOWN_NEAR  = 0.75    # hist run was longer by ≤100m (horse going shorter)
DIST_STEP_DOWN_MED   = 0.45    # hist run was longer by ≤200m
DIST_STEP_DOWN_FAR   = 0.20    # hist run was longer by >200m
DIST_STEP_UP_NEAR    = 0.60    # hist run was shorter by ≤100m (horse going longer)
DIST_STEP_UP_MED     = 0.30    # hist run was shorter by ≤200m
DIST_STEP_UP_FAR     = 0.10    # hist run was shorter by >200m

# ── v4.0 Change 4: Race quality weighting (form franking) ────────────────────
# Weight each historical race by how well its runners performed subsequently.
# Strong-form races (winners/placers went on to perform) get higher weight.
# Weak-form races (beaten horses all flopped) get lower weight.
RACE_QUALITY_MIN_SUBSEQUENT = 3   # min runners with a subsequent start to score
RACE_QUALITY_WEIGHT_MIN     = 0.70  # minimum quality weight (weak races)
RACE_QUALITY_WEIGHT_MAX     = 1.30  # maximum quality weight (strong races)
RACE_QUALITY_LOOKBACK_DAYS  = 120   # only frank form from races within this window

# Caches for expensive computations (populated once at analysis time)
_FIELD_SIZE_CACHE = {}              # (race_date, race_number) → int
_RACE_QUALITY_CACHE = {}            # (race_date, race_number) → float (quality weight)

# ── v4.3 Change 1: Speed-map positional adjustment ───────────────────────────
# Feed smap_advantage (−0.4 to +0.6) into projected_time via a tunable coefficient.
# Positive advantage = saves time (negative adjustment); negative = loses time.
# The coefficient converts advantage units to seconds: adj = -advantage * COEFF.
# Calibrated by backtest — start with moderate default.
SMAP_TIME_COEFF = 0.06          # seconds per unit of smap_advantage (backtest-calibrated)
SMAP_TIME_CAP   = 0.15          # maximum positional adjustment ±seconds

# ── v4.3 Change 2: Pace × Position × Style interaction ───────────────────────
# 3-way interaction term replacing the disabled pace multipliers.
# Key insight: pace effect depends on WHERE the horse races, not just its style.
# Each (pace_bucket, row_position, style) → seconds adjustment.
# Negative = faster (benefits), Positive = slower (hurts).
PACE_POS_STYLE_ADJ = {
    # (pace_bucket, row_label, style) → seconds
    # Fast pace scenarios: front-runners on rail best + closers from behind benefit
    ("Fast", "RAIL", "Leader"):    -0.06,   # dictates on rail, efficient
    ("Fast", "RAIL", "On-Pace"):   -0.03,   # follows leader, protected
    ("Fast", "RAIL", "Midfield"):   0.00,   # boxed risk, but saves ground
    ("Fast", "RAIL", "Closer"):    -0.04,   # saves ground, pace meltdown ahead
    ("Fast", "W2",   "Leader"):    -0.02,   # pressing but not ideal
    ("Fast", "W2",   "On-Pace"):    0.00,   # neutral
    ("Fast", "W2",   "Midfield"):   0.00,   # neutral
    ("Fast", "W2",   "Closer"):    -0.03,   # clear run, pace meltdown
    ("Fast", "WIDE", "Leader"):    +0.05,   # burning fuel + losing ground
    ("Fast", "WIDE", "On-Pace"):   +0.03,   # wide pressing, energy cost
    ("Fast", "WIDE", "Midfield"):   0.00,   # neutral wide
    ("Fast", "WIDE", "Closer"):    -0.02,   # wide but pace collapses

    # Normal pace: mostly neutral, slight advantage for good positions
    ("Normal", "RAIL", "Leader"):  -0.03,   # controlling on rail
    ("Normal", "RAIL", "On-Pace"):  0.00,
    ("Normal", "RAIL", "Midfield"): 0.00,
    ("Normal", "RAIL", "Closer"):   0.00,
    ("Normal", "W2",   "Leader"):   0.00,
    ("Normal", "W2",   "On-Pace"):  0.00,
    ("Normal", "W2",   "Midfield"): 0.00,
    ("Normal", "W2",   "Closer"):   0.00,
    ("Normal", "WIDE", "Leader"):  +0.02,   # extra ground
    ("Normal", "WIDE", "On-Pace"): +0.01,
    ("Normal", "WIDE", "Midfield"): 0.00,
    ("Normal", "WIDE", "Closer"):   0.00,

    # Slow pace: front-runners benefit most (dictate), closers hurt (no pace to run into)
    ("Slow", "RAIL", "Leader"):    -0.06,   # dream run: controls rail, slow tempo
    ("Slow", "RAIL", "On-Pace"):   -0.03,   # stalking leader on rail
    ("Slow", "RAIL", "Midfield"):   0.00,
    ("Slow", "RAIL", "Closer"):    +0.04,   # no pace to close into
    ("Slow", "W2",   "Leader"):    -0.03,   # pressing from W2
    ("Slow", "W2",   "On-Pace"):   -0.01,
    ("Slow", "W2",   "Midfield"):   0.00,
    ("Slow", "W2",   "Closer"):    +0.03,   # no pace, stuck wide
    ("Slow", "WIDE", "Leader"):     0.00,   # controls but loses ground
    ("Slow", "WIDE", "On-Pace"):   +0.01,
    ("Slow", "WIDE", "Midfield"):  +0.01,
    ("Slow", "WIDE", "Closer"):    +0.05,   # worst: no pace + wide + behind
}

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

# ── v4.4: Sectional decomposition integration ────────────────────────────────
# Integrates horse-level sectional profiles (Early/Mid/Late zone deviations
# from race-median, per-400m normalised) as additive signals in projection.
# Based on standalone analysis: prior_late_dev ρ = 0.25–0.36 with actual place.
# SSI importance scales with distance: winner gap −0.224 sprints → −0.490 at 1800m+.
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

SEC_LATE_DEV_COEFF   = 0.12   # seconds per unit of avg_late_dev (additive signal)
SEC_SSI_DIST_COEFF   = 0.10   # SSI penalty coefficient for 1600m+ front-loaded fade risk
SEC_SSI_DIST_THRESH  = 1600   # minimum distance for SSI distance-suitability penalty
SEC_SSI_SPRINT_COEFF = 0.04   # SSI coefficient for sprints (≤1200m) — smaller effect
SEC_MIN_RUNS         = 2      # minimum profiled runs for sectional adjustment to apply
SEC_LATE_STD_PENALTY = 0.02   # per unit of late_std above threshold (reliability)
SEC_LATE_STD_THRESH  = 0.30   # late_std above this triggers reliability penalty


# ══════════════════════════════════════════════════════════════════════════════
# 1. Parse race card  (v4.0 structured format OR legacy Sheet6 layout)
# ══════════════════════════════════════════════════════════════════════════════

def parse_race_card_v2(path):
    """Parse v4.0 structured race card (column-header format from scraper)."""
    df = pd.read_excel(path, sheet_name=SHEET_NAME)

    races = []
    for rn, grp in df.groupby("race_number"):
        row0 = grp.iloc[0]
        rc_raw = row0.get("race_class", "")
        if isinstance(rc_raw, str) and "Group" in rc_raw:
            race_class = 0
        else:
            try:
                race_class = int(rc_raw)
            except (ValueError, TypeError):
                race_class = 0

        surface = str(row0.get("surface", "Turf"))
        is_awt = "All Weather" in surface or "AWT" in surface
        if is_awt:
            track_type = "All Weather Track"
            race_course_val = "AWT"
        else:
            track_type = "Turf"
            race_course_val = str(row0.get("race_course", "A"))

        distance = int(row0.get("distance", 0))
        rating_range = str(row0.get("rating_range", "")) if pd.notna(row0.get("rating_range")) else ""

        horses = []
        for _, hr in grp.iterrows():
            # Skip standby horses
            if hr.get("is_standby") == True:
                continue
            try:
                hno = int(hr["horse_no"])
            except (ValueError, TypeError):
                continue
            declared_wt = int(hr["weight"]) if pd.notna(hr.get("weight")) and hr["weight"] > 0 else 0
            jockey_str = str(hr.get("jockey", "")).strip() if pd.notna(hr.get("jockey")) else ""
            claim = _parse_apprentice_claim(jockey_str)
            actual_wt = max(declared_wt - claim, 0)
            draw_val = int(hr["draw"]) if pd.notna(hr.get("draw")) else None
            try:
                rating_val = int(float(hr["rating"])) if pd.notna(hr.get("rating")) else None
            except (ValueError, TypeError):
                rating_val = None
            horses.append({
                "horse_no":   hno,
                "horse_name": str(hr["horse_name"]).strip(),
                "brand_no":   str(hr.get("brand_no", "")).strip() if pd.notna(hr.get("brand_no")) else "",
                "weight":     actual_wt,
                "declared_weight": declared_wt,
                "draw":       draw_val,
                "rating":     rating_val,
                "jockey":     jockey_str,
                "trainer":    str(hr.get("trainer", "")).strip() if pd.notna(hr.get("trainer")) else "",
            })

        races.append({
            "race_number":  int(rn),
            "race_name":    str(row0.get("race_name", "")),
            "distance":     distance,
            "track_type":   track_type,
            "race_course":  race_course_val,
            "is_awt":       is_awt,
            "race_class":   race_class,
            "track_line":   f"{distance}M {surface} \"{race_course_val}\"",
            "prize_line":   str(row0.get("prize", "")),
            "rating_range": rating_range,
            "horses":       horses,
        })

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
            try: declared_wt = int(weight_raw)
            except ValueError: declared_wt = 0

            draw_raw = str(row[6]).strip() if pd.notna(row[6]) else ""
            try: draw = int(draw_raw)
            except ValueError: draw = None

            # Column mapping for 15 Mar form (standard ST layout):
            # Mar 18 HV layout: col[2]=Horse, col[3]=Brand, col[7]=Trainer, col[8]=Rtg
            horse_name_raw = str(row[2]).strip() if pd.notna(row[2]) else ""
            brand_no_raw   = str(row[3]).strip() if pd.notna(row[3]) else ""
            jockey_raw     = str(row[5]).strip() if pd.notna(row[5]) else ""
            trainer_raw    = str(row[7]).strip() if pd.notna(row[7]) else ""
            rating_raw_col = str(row[8]).strip() if pd.notna(row[8]) else ""
            try: rating_parsed = int(float(rating_raw_col))
            except (ValueError, TypeError): rating_parsed = None

            claim = _parse_apprentice_claim(jockey_raw)
            actual_wt = max(declared_wt - claim, 0)

            current_race["horses"].append({
                "horse_no":    horse_no_int,
                "horse_name":  horse_name_raw,
                "brand_no":    brand_no_raw,
                "weight":      actual_wt,
                "declared_weight": declared_wt,
                "draw":        draw,
                "rating":      rating_parsed,
                "jockey":      jockey_raw,
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


# ══════════════════════════════════════════════════════════════════════════════
# 2. Load v3 references
# ══════════════════════════════════════════════════════════════════════════════

def load_references_v3():
    class_fine = pd.read_excel(EXP_TIME_REF, sheet_name=0)
    fine       = pd.read_excel(EXP_TIME_REF, sheet_name=1)
    coarse     = pd.read_excel(EXP_TIME_REF, sheet_name=2)
    ultra      = pd.read_excel(EXP_TIME_REF, sheet_name=3)
    # v4.0 Change 1: Ability file removed — all profiles computed at runtime from DB

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

    return class_fine, fine, coarse, ultra, draw_off


def _parse_apprentice_claim(jockey_str):
    """Extract apprentice weight claim from jockey string.
    e.g. 'P N Wong (-7)' → 7, 'J Moreira' → 0."""
    if not jockey_str:
        return 0
    m = re.search(r'\(-(\d+)\)', str(jockey_str))
    return int(m.group(1)) if m else 0


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
# 2½. v4.0 Runtime Horse Profile + Race Quality + Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _parse_first_position(rp_str):
    """Extract the first-call position from running_positions string (space-separated)."""
    if pd.isna(rp_str) or not str(rp_str).strip():
        return None
    parts = str(rp_str).strip().split()
    try:
        return int(float(parts[0]))
    except (ValueError, TypeError):
        return None


def _classify_running_style(first_pos, field_size):
    """Classify running style from first-call position.
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


def _get_field_size(race_date, race_number, db):
    """Get field size for a race, cached."""
    key = (race_date, race_number)
    if key not in _FIELD_SIZE_CACHE:
        _FIELD_SIZE_CACHE[key] = int(((db["race_date"] == race_date)
                                       & (db["race_number"] == race_number)).sum())
    return _FIELD_SIZE_CACHE[key]


def compute_horse_profile_runtime(horse_name, db, class_fine, fine, coarse, ultra):
    """v4.0 Change 1: Compute all horse profile fields at runtime from DB.

    Replaces the static horse_ability_analysis_v3.xlsx lookup.
    Returns dict with: dominant_style, leader_frac, front_frac, early_speed_z,
    style_entropy, residual_std, best_residual, worst_residual,
    n_runs, n_sectional_runs, shrink_factor, confidence, ability_v3.
    """
    NO_DATA = {
        "dominant_style": "Unknown", "leader_frac": 0.0, "front_frac": 0.0,
        "early_speed_z": 0.0, "style_entropy": 1.0, "residual_std": np.nan,
        "best_residual": np.nan, "worst_residual": np.nan, "n_runs": 0,
        "n_sectional_runs": 0, "shrink_factor": 0.0, "confidence": "no_data",
        "ability_v3": np.nan,
    }
    runs = db[db["horse_name"] == horse_name].copy()
    if len(runs) == 0:
        return NO_DATA

    runs = runs.sort_values("race_date", ascending=True).reset_index(drop=True)

    # ── Compute raw residuals for stat fields ──
    residuals = []
    for _, run in runs.iterrows():
        ft = run.get("finish_time_seconds")
        dist = run.get("distance")
        if pd.isna(ft) or pd.isna(dist) or ft <= 0:
            continue
        dist_i = int(dist)
        going_i = _map_going(run.get("going", "G"))
        wt_i = run.get("actual_weight")
        wb_i = weight_band(int(wt_i)) if pd.notna(wt_i) and wt_i > 0 else "121-125"
        rc_i = run.get("race_course", "A")
        tt_i = run.get("track_type", "Turf")
        cb_i = class_band(int(run.get("race_class", 0))) if pd.notna(run.get("race_class")) else "Group/Other"
        et_i, _, _, _ = lookup_expected_time(
            class_fine, fine, coarse, ultra, dist_i, going_i, wb_i, rc_i, tt_i, cb_i)
        if pd.notna(et_i) and et_i > 0:
            residuals.append(ft - et_i)

    n_runs = len(residuals)
    if n_runs == 0:
        return NO_DATA

    # Residual statistics
    resid_arr = np.array(residuals)
    residual_std = float(np.std(resid_arr)) if n_runs > 1 else np.nan
    best_residual = float(resid_arr.min())
    worst_residual = float(resid_arr.max())
    ability_v3 = float(resid_arr.mean())

    # Shrinkage factor & confidence
    k = PROFILE_SHRINKAGE_K
    shrink_factor = n_runs / (n_runs + k)
    if shrink_factor < 0.30:
        confidence = "low"
    elif shrink_factor < 0.50:
        confidence = "medium"
    else:
        confidence = "high"

    # Sectional runs count
    n_sectional_runs = int(runs["sectiontimes"].notna().sum()) if "sectiontimes" in runs.columns else 0

    # ── Running style classification ──
    style_counts = {"Leader": 0, "On-Pace": 0, "Midfield": 0, "Closer": 0}
    style_top3 = {"Leader": 0, "On-Pace": 0, "Midfield": 0, "Closer": 0}
    first_positions = []

    for _, run in runs.iterrows():
        rp = run.get("running_positions", "")
        rd = run.get("race_date")
        rn = run.get("race_number")
        first_pos = _parse_first_position(rp)
        if first_pos is None:
            continue
        field_sz = _get_field_size(rd, rn, db)
        if field_sz <= 0:
            continue
        style = _classify_running_style(first_pos, field_sz)
        if style == "Unknown":
            continue
        style_counts[style] += 1
        place = _safe_place(run.get("place", 99))
        if place <= 3:
            style_top3[style] += 1
        first_positions.append(first_pos / field_sz)  # normalised position

    total_classified = sum(style_counts.values())
    if total_classified == 0:
        return {**NO_DATA, "n_runs": n_runs, "n_sectional_runs": n_sectional_runs,
                "shrink_factor": round(shrink_factor, 3), "confidence": confidence,
                "residual_std": round(residual_std, 4) if pd.notna(residual_std) else np.nan,
                "best_residual": round(best_residual, 4), "worst_residual": round(worst_residual, 4),
                "ability_v3": round(ability_v3, 4)}

    # Dominant style: OPTIMAL = best top-3 rate; tie-break by most runs
    best_rate = -1.0
    dom_style = "Unknown"
    for style in ("Leader", "On-Pace", "Midfield", "Closer"):
        if style_counts[style] > 0:
            rate = style_top3[style] / style_counts[style]
            if rate > best_rate or (rate == best_rate and style_counts[style] > style_counts.get(dom_style, 0)):
                best_rate = rate
                dom_style = style

    leader_frac = style_counts["Leader"] / total_classified
    front_frac = (style_counts["Leader"] + style_counts["On-Pace"]) / total_classified

    # Early speed z-score: how early does this horse position itself?
    # Lower normalised position = faster early speed
    if len(first_positions) >= 2:
        # Mean normalised first-call position, z-scored against neutral 0.50
        # Negative = front-runner (fast early speed), Positive = backmarker (slow)
        mean_pos = float(np.mean(first_positions))
        early_speed_z = (mean_pos - 0.50) / 0.20
    elif len(first_positions) == 1:
        early_speed_z = (first_positions[0] - 0.50) / 0.20
    else:
        early_speed_z = 0.0

    # Style entropy: Shannon entropy of style distribution
    probs = np.array([style_counts[s] / total_classified for s in style_counts])
    probs = probs[probs > 0]
    style_entropy = float(-np.sum(probs * np.log2(probs))) if len(probs) > 1 else 0.0

    return {
        "dominant_style": dom_style,
        "leader_frac": round(leader_frac, 3),
        "front_frac": round(front_frac, 3),
        "early_speed_z": round(early_speed_z, 3),
        "style_entropy": round(style_entropy, 3),
        "residual_std": round(residual_std, 4) if pd.notna(residual_std) else np.nan,
        "best_residual": round(best_residual, 4),
        "worst_residual": round(worst_residual, 4),
        "n_runs": n_runs,
        "n_sectional_runs": n_sectional_runs,
        "shrink_factor": round(shrink_factor, 3),
        "confidence": confidence,
        "ability_v3": round(ability_v3, 4),
    }


# ── v4.4: Sectional decomposition functions ──────────────────────────────────

def precompute_sectional_deviations(db):
    """Precompute race-median normalised sectional zone deviations for all runs.

    For each horse-run with valid sectional times:
      1. Convert raw sections to per-400m pace
      2. Decompose into Early / Mid / Late zones
      3. Compute race-median for each zone
      4. Deviation = horse_zone − race_median_zone

    Returns dict: horse_name → sorted list of
        {race_date, early_dev, mid_dev, late_dev, ssi}
    """
    # Step 1: Compute per-400m zones for every run with valid sectionals
    run_zones = {}
    for idx, row in db.iterrows():
        st = row.get("sectiontimes")
        dist = row.get("distance")
        if pd.isna(st) or pd.isna(dist):
            continue
        dist_i = int(dist)
        lengths = SECTION_LENGTHS.get(dist_i)
        if not lengths:
            continue
        parts = [p.strip() for p in str(st).split(";")]
        secs = []
        for p in parts:
            if not p:
                continue
            try:
                v = float(p)
                if v > 0:
                    secs.append(v)
            except (ValueError, TypeError):
                continue
        if len(secs) != len(lengths):
            continue
        per400 = [t * 400.0 / l for t, l in zip(secs, lengths)]
        if len(per400) < 2:
            continue
        early = per400[0]
        late = per400[-1]
        mid = float(np.mean(per400[1:-1])) if len(per400) >= 3 else (early + late) / 2.0
        run_zones[idx] = {"early": early, "mid": mid, "late": late}

    # Step 2: Compute race-level medians (need ≥3 runners per race)
    race_groups = defaultdict(list)
    for idx in run_zones:
        rd = db.at[idx, "race_date"]
        rn = db.at[idx, "race_number"]
        race_groups[(rd, rn)].append(idx)

    race_medians = {}
    for key, indices in race_groups.items():
        if len(indices) < 3:
            continue
        race_medians[key] = {
            "med_early": float(np.median([run_zones[i]["early"] for i in indices])),
            "med_mid":   float(np.median([run_zones[i]["mid"] for i in indices])),
            "med_late":  float(np.median([run_zones[i]["late"] for i in indices])),
        }

    # Step 3: Compute per-run deviations from race median
    result = defaultdict(list)
    for idx, zones in run_zones.items():
        rd = db.at[idx, "race_date"]
        rn = db.at[idx, "race_number"]
        med = race_medians.get((rd, rn))
        if med is None:
            continue
        ed = zones["early"] - med["med_early"]
        md = zones["mid"] - med["med_mid"]
        ld = zones["late"] - med["med_late"]
        horse = db.at[idx, "horse_name"]
        result[horse].append({
            "race_date": rd,
            "early_dev": ed, "mid_dev": md, "late_dev": ld,
            "ssi": ld - ed,
        })

    for horse in result:
        result[horse].sort(key=lambda x: x["race_date"])

    return dict(result)


def compute_sectional_profile(horse_name, sec_devs):
    """v4.4: Build horse sectional profile from precomputed deviations.

    Uses last 6 runs with recency weighting (λ = RECENCY_LAMBDA).
    Returns dict with avg_early_dev, avg_mid_dev, avg_late_dev, avg_ssi,
    late_std, best_late, sec_type, n_sec_profile.
    """
    NO_DATA = {
        "avg_early_dev": None, "avg_mid_dev": None, "avg_late_dev": None,
        "avg_ssi": None, "late_std": None, "best_late": None,
        "sec_type": "No Data", "n_sec_profile": 0,
    }
    if sec_devs is None:
        return NO_DATA
    devs = sec_devs.get(horse_name)
    if not devs or len(devs) < 1:
        return NO_DATA

    devs = devs[-6:]
    n = len(devs)
    lam = RECENCY_LAMBDA
    w = np.array([lam ** (n - 1 - i) for i in range(n)])
    w /= w.sum()

    avg_ed = float(np.dot([d["early_dev"] for d in devs], w))
    avg_md = float(np.dot([d["mid_dev"] for d in devs], w))
    avg_ld = float(np.dot([d["late_dev"] for d in devs], w))
    avg_ssi = float(np.dot([d["ssi"] for d in devs], w))
    late_std = float(np.std([d["late_dev"] for d in devs])) if n > 1 else None
    best_late = float(min(d["late_dev"] for d in devs))

    # Classify sectional type
    if avg_ssi < -0.15 and avg_ld < -0.05:
        sec_type = "Strong Fin"
    elif avg_ssi > 0.15 and avg_ed < -0.05:
        sec_type = "Front-Load"
    elif abs(avg_ssi) <= 0.15 and abs(avg_ed) < 0.15:
        sec_type = "Even-Paced"
    elif avg_ed < -0.15:
        sec_type = "Speed Merch"
    elif avg_ld > 0.15:
        sec_type = "Weak Fin"
    else:
        sec_type = "Mixed"

    return {
        "avg_early_dev": round(avg_ed, 3),
        "avg_mid_dev": round(avg_md, 3),
        "avg_late_dev": round(avg_ld, 3),
        "avg_ssi": round(avg_ssi, 3),
        "late_std": round(late_std, 3) if late_std is not None else None,
        "best_late": round(best_late, 3),
        "sec_type": sec_type,
        "n_sec_profile": n,
    }


def compute_race_quality_scores(db, class_fine, fine, coarse, ultra):
    """v4.0 Change 4: Precompute form-franking quality scores for all races.

    For each historical race, check how the BEATEN horses (non-winners)
    performed in their NEXT start:
      - "Validated": next-start residual improved or horse placed (top 3)
      - "Failed": next-start residual worsened significantly
    Quality = proportion of validated runners among those with subsequent data.
    Mapped to weight range [RACE_QUALITY_WEIGHT_MIN, RACE_QUALITY_WEIGHT_MAX].

    Stored in _RACE_QUALITY_CACHE for use in compute_recency_residual.
    """
    global _RACE_QUALITY_CACHE
    if _RACE_QUALITY_CACHE:
        return  # already computed

    # Build lookup: for each horse, ordered list of (race_date, race_number, place, resid)
    horse_runs = {}
    for _, row in db.iterrows():
        hn = row.get("horse_name")
        ft = row.get("finish_time_seconds")
        dist = row.get("distance")
        if pd.isna(hn) or pd.isna(ft) or pd.isna(dist) or ft <= 0:
            continue
        rd = row.get("race_date")
        rn = row.get("race_number")
        place = _safe_place(row.get("place", 99))
        dist_i = int(dist)
        going_i = _map_going(row.get("going", "G"))
        wt_i = row.get("actual_weight")
        wb_i = weight_band(int(wt_i)) if pd.notna(wt_i) and wt_i > 0 else "121-125"
        rc_i = row.get("race_course", "A")
        tt_i = row.get("track_type", "Turf")
        cb_i = class_band(int(row.get("race_class", 0))) if pd.notna(row.get("race_class")) else "Group/Other"
        et_i, _, _, _ = lookup_expected_time(
            class_fine, fine, coarse, ultra, dist_i, going_i, wb_i, rc_i, tt_i, cb_i)
        resid = (ft - et_i) if (pd.notna(et_i) and et_i > 0) else None
        if hn not in horse_runs:
            horse_runs[hn] = []
        horse_runs[hn].append({"date": rd, "rn": rn, "place": place, "resid": resid})

    # Sort each horse's runs by date
    for hn in horse_runs:
        horse_runs[hn].sort(key=lambda x: x["date"])

    # For each race, compute quality score
    race_keys = db.groupby(["race_date", "race_number"]).size().index.tolist()
    for rd, rn in race_keys:
        race_runners = db[(db["race_date"] == rd) & (db["race_number"] == rn)]
        validated = 0
        total_with_next = 0

        for _, runner in race_runners.iterrows():
            hn = runner.get("horse_name")
            if pd.isna(hn) or hn not in horse_runs:
                continue
            runs_list = horse_runs[hn]
            # Find this run's index
            run_idx = None
            for i, r in enumerate(runs_list):
                if r["date"] == rd and r["rn"] == rn:
                    run_idx = i
                    break
            if run_idx is None or run_idx + 1 >= len(runs_list):
                continue  # no subsequent run

            this_run = runs_list[run_idx]
            next_run = runs_list[run_idx + 1]
            if this_run["resid"] is None or next_run["resid"] is None:
                continue

            total_with_next += 1
            # "Validated": next start was competitive (placed, or residual improved)
            if next_run["place"] <= 3 or next_run["resid"] < this_run["resid"]:
                validated += 1

        if total_with_next >= RACE_QUALITY_MIN_SUBSEQUENT:
            ratio = validated / total_with_next  # 0.0 to 1.0
            # Map to weight range: 0.0 → min, 1.0 → max
            weight_range = RACE_QUALITY_WEIGHT_MAX - RACE_QUALITY_WEIGHT_MIN
            quality_w = RACE_QUALITY_WEIGHT_MIN + ratio * weight_range
        else:
            quality_w = 1.0  # neutral — insufficient data to judge

        _RACE_QUALITY_CACHE[(rd, rn)] = round(quality_w, 3)

    print(f"  Race quality scores: {len(_RACE_QUALITY_CACHE)} races scored "
          f"(avg={np.mean(list(_RACE_QUALITY_CACHE.values())):.3f})")


def get_race_quality_weight(race_date, race_number):
    """Look up precomputed race quality weight. Returns 1.0 if not scored."""
    return _RACE_QUALITY_CACHE.get((race_date, race_number), 1.0)


def _get_historical_draw_offset(draw_off, distance, course, draw_num):
    """Look up raw (unshrunk) draw offset for a historical run's conditions.
    Used in Change 2 to contextualise — we want the raw effect, not the
    Bayesian-shrunk version, because we're *removing* the draw effect."""
    if draw_num is None or pd.isna(draw_num):
        return 0.0
    m = ((draw_off["distance"] == distance) &
         (draw_off["race_course"] == course) &
         (draw_off["draw"] == int(draw_num)))
    rows = draw_off.loc[m]
    if len(rows) > 0:
        return float(rows.iloc[0]["draw_offset"])
    return 0.0


def _estimate_wide_cost(draw_num, field_size, first_call_pos, course_config):
    """v4.0 Change 2: Estimate the wide-running cost in seconds.

    Logic: a horse drawn wide (outer WIDE_COST_OUTER_FRAC of field) that also
    settles in a forward position must have crossed over or run wide on the first
    turn. The cost depends on how forward they positioned (Leaders pay most,
    closers pay nothing — they just sit back from a wide draw).

    Course narrowness amplifies the cost: C+3 > C > B+2 > B > A+3 > A.
    """
    if draw_num is None or field_size <= 0 or first_call_pos is None:
        return 0.0

    draw_num = int(draw_num)
    position_ratio = draw_num / field_size
    if position_ratio <= WIDE_COST_OUTER_FRAC:
        return 0.0  # not a wide draw

    # Course width factor: narrower courses amplify wide-running cost
    COURSE_WIDTH = {"A": 0.6, "A+3": 0.75, "B": 0.85, "B+2": 1.0,
                    "C": 1.15, "C+3": 1.30, "AWT": 0.7}
    course_factor = COURSE_WIDTH.get(str(course_config), 0.85)

    # Style determines how much of the wide draw is "spent"
    # first_call_pos relative to field: lower = more forward = more cost
    pos_frac = first_call_pos / field_size
    if pos_frac <= 0.20:  # front-runner from wide draw
        base_cost = WIDE_COST_LEADER_PEN
    elif pos_frac <= 0.40:
        base_cost = WIDE_COST_ONPACE_PEN
    elif pos_frac <= 0.60:
        base_cost = WIDE_COST_MIDFIELD_PEN
    else:
        base_cost = 0.0  # back-marker from wide draw — just sat there

    # Scale by how far outside the threshold the draw is
    excess = position_ratio - WIDE_COST_OUTER_FRAC
    draw_severity = min(1.0, excess / 0.35)  # 0 at threshold, 1.0 at 100%

    return round(base_cost * course_factor * draw_severity, 4)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Expected-time lookup (multi-tier)
# ══════════════════════════════════════════════════════════════════════════════

_ET_CACHE = {}

# Pre-computed class-band correction offsets for Fine tier fallthrough.
# When ClassFine has insufficient data and we fall to Fine (all-class pooled),
# better-class horses in the same weight band pull the Fine ET unrealistically
# fast for C5 horses.  This dict maps (distance, going, wband, course, cband)
# to the correction offset = ClassFine_median_ET − Fine_ET for that same
# distance/going/wband/course.  Populated lazily from the reference data.
_CLASS_BAND_OFFSETS = {}

def _compute_class_band_offset(class_fine, fine, distance, going, wband, course, cband):
    """Compute correction offset: median ClassFine ET (across weight bands for this
    distance/going/course/class) minus the Fine ET for this specific weight band.
    Returns the offset in seconds, or 0.0 if no reference data available."""
    key = (distance, going, wband, course, cband)
    if key in _CLASS_BAND_OFFSETS:
        return _CLASS_BAND_OFFSETS[key]

    # Get all ClassFine rows for this distance/going/course/class (any weight band)
    cf_mask = ((class_fine["distance"] == distance) &
               (class_fine["going_group"] == going) &
               (class_fine["race_course"] == course) &
               (class_fine["class_band"] == cband))
    cf_rows = class_fine.loc[cf_mask]

    # Get the Fine row for this exact weight band (all-class pooled)
    fn_mask = ((fine["distance"] == distance) &
               (fine["going_group"] == going) &
               (fine["weight_band"] == wband) &
               (fine["race_course"] == course))
    fn_rows = fine.loc[fn_mask]

    offset = 0.0
    if len(cf_rows) > 0 and len(fn_rows) > 0:
        # Weighted median of ClassFine ETs (weighted by sample_size)
        cf_valid = cf_rows[cf_rows["sample_size"] >= 2]
        if len(cf_valid) > 0:
            cf_et_avg = float(np.average(cf_valid["expected_time"],
                                          weights=cf_valid["sample_size"]))
            fn_et = float(fn_rows.iloc[0]["expected_time"])
            offset = cf_et_avg - fn_et  # positive = class is slower than pooled

    _CLASS_BAND_OFFSETS[key] = offset
    return offset


def lookup_expected_time(class_fine, fine, coarse, ultra,
                         distance, going, wband, course, track_type, cband):
    cache_key = (distance, going, wband, course, track_type, cband)
    if cache_key in _ET_CACHE:
        return _ET_CACHE[cache_key]
    # ClassFine: lowered to MIN_N=2 to use thin class-specific data when available
    CLASS_FINE_MIN_N = 2
    GENERAL_MIN_N = 5
    tiers = [
        (class_fine, {"distance": distance, "going_group": going,
                      "weight_band": wband, "race_course": course,
                      "class_band": cband}, "class_fine", CLASS_FINE_MIN_N),
        (fine, {"distance": distance, "going_group": going,
                "weight_band": wband, "race_course": course}, "fine", GENERAL_MIN_N),
        (coarse, {"distance": distance, "going_group": going,
                  "weight_band": wband, "track_type": track_type}, "coarse", GENERAL_MIN_N),
        (ultra, {"distance": distance, "going_group": going,
                 "track_type": track_type}, "ultra", GENERAL_MIN_N),
    ]
    for df_ref, cols, tier_name, min_n in tiers:
        mask = pd.Series(True, index=df_ref.index)
        for col, val in cols.items():
            mask &= (df_ref[col] == val)
        matched = df_ref.loc[mask]
        if len(matched) and matched.iloc[0].get("sample_size", 0) >= min_n:
            r = matched.iloc[0]
            et_val = r["expected_time"]
            # Apply class-band correction when falling to Fine tier
            if tier_name == "fine":
                correction = _compute_class_band_offset(
                    class_fine, fine, distance, going, wband, course, cband)
                et_val = et_val + correction
            result = et_val, int(r["sample_size"]), r.get("std_time", np.nan), tier_name
            _ET_CACHE[cache_key] = result
            return result
    result = np.nan, 0, np.nan, "none"
    _ET_CACHE[cache_key] = result
    return result


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

# ══════════════════════════════════════════════════════════════════════════════
# v4.2: HKJC Official Reference Standard Times (Course Standard Times page)
# Source: https://racing.hkjc.com/en-us/local/page/racing-course-time
# Last updated: 26-Aug-2025 (HKJC page timestamp)
#
# Key: (venue_surface, distance, class_num) → {"total": seconds, "last_400": seconds}
# venue_surface: "ST_Turf", "HV_Turf", "ST_AWT"
# class_num: 0=Group, 1..5=Class 1..5, 9=Griffin
# "early" = total - last_400 (computed at lookup time)
# ══════════════════════════════════════════════════════════════════════════════
HKJC_STANDARD_TIMES = {
    # ── Sha Tin Turf ──
    ("ST_Turf", 1000, 0): {"total": 55.90, "last_400": 22.25},
    ("ST_Turf", 1000, 2): {"total": 56.05, "last_400": 22.35},
    ("ST_Turf", 1000, 3): {"total": 56.45, "last_400": 22.75},
    ("ST_Turf", 1000, 4): {"total": 56.65, "last_400": 22.90},
    ("ST_Turf", 1000, 5): {"total": 57.00, "last_400": 22.90},
    ("ST_Turf", 1000, 9): {"total": 56.65, "last_400": 22.60},
    ("ST_Turf", 1200, 0): {"total": 68.15, "last_400": 22.40},
    ("ST_Turf", 1200, 1): {"total": 68.45, "last_400": 22.60},
    ("ST_Turf", 1200, 2): {"total": 68.65, "last_400": 22.65},
    ("ST_Turf", 1200, 3): {"total": 69.00, "last_400": 22.95},
    ("ST_Turf", 1200, 4): {"total": 69.35, "last_400": 23.15},
    ("ST_Turf", 1200, 5): {"total": 69.55, "last_400": 23.30},
    ("ST_Turf", 1200, 9): {"total": 69.90, "last_400": 23.00},
    ("ST_Turf", 1400, 0): {"total": 81.10, "last_400": 22.40},
    ("ST_Turf", 1400, 1): {"total": 81.25, "last_400": 22.70},
    ("ST_Turf", 1400, 2): {"total": 81.45, "last_400": 23.00},
    ("ST_Turf", 1400, 3): {"total": 81.65, "last_400": 23.25},
    ("ST_Turf", 1400, 4): {"total": 82.00, "last_400": 23.40},
    ("ST_Turf", 1400, 5): {"total": 82.30, "last_400": 23.65},
    ("ST_Turf", 1600, 0): {"total": 93.90, "last_400": 22.75},
    ("ST_Turf", 1600, 1): {"total": 94.05, "last_400": 23.00},
    ("ST_Turf", 1600, 2): {"total": 94.25, "last_400": 23.10},
    ("ST_Turf", 1600, 3): {"total": 94.70, "last_400": 23.50},
    ("ST_Turf", 1600, 4): {"total": 94.90, "last_400": 23.70},
    ("ST_Turf", 1600, 5): {"total": 95.45, "last_400": 23.90},
    ("ST_Turf", 1800, 0): {"total": 107.10, "last_400": 22.75},
    ("ST_Turf", 1800, 2): {"total": 107.30, "last_400": 23.40},
    ("ST_Turf", 1800, 3): {"total": 107.50, "last_400": 23.55},
    ("ST_Turf", 1800, 4): {"total": 107.85, "last_400": 23.75},
    ("ST_Turf", 1800, 5): {"total": 108.45, "last_400": 24.20},
    ("ST_Turf", 2000, 0): {"total": 120.50, "last_400": 23.20},
    ("ST_Turf", 2000, 1): {"total": 121.20, "last_400": 23.20},
    ("ST_Turf", 2000, 2): {"total": 121.70, "last_400": 23.40},
    ("ST_Turf", 2000, 3): {"total": 121.90, "last_400": 23.55},
    ("ST_Turf", 2000, 4): {"total": 122.35, "last_400": 23.75},
    ("ST_Turf", 2000, 5): {"total": 122.65, "last_400": 24.20},
    ("ST_Turf", 2400, 0): {"total": 147.00, "last_400": 23.95},
    # ── Happy Valley Turf ──
    ("HV_Turf", 1000, 2): {"total": 56.40, "last_400": 22.95},
    ("HV_Turf", 1000, 3): {"total": 56.65, "last_400": 23.15},
    ("HV_Turf", 1000, 4): {"total": 57.20, "last_400": 23.35},
    ("HV_Turf", 1000, 5): {"total": 57.35, "last_400": 23.35},
    ("HV_Turf", 1200, 1): {"total": 69.10, "last_400": 23.25},
    ("HV_Turf", 1200, 2): {"total": 69.30, "last_400": 23.50},
    ("HV_Turf", 1200, 3): {"total": 69.60, "last_400": 23.55},
    ("HV_Turf", 1200, 4): {"total": 69.90, "last_400": 23.55},
    ("HV_Turf", 1200, 5): {"total": 70.10, "last_400": 23.65},
    ("HV_Turf", 1650, 1): {"total": 99.10, "last_400": 23.40},
    ("HV_Turf", 1650, 2): {"total": 99.30, "last_400": 23.65},
    ("HV_Turf", 1650, 3): {"total": 99.90, "last_400": 23.85},
    ("HV_Turf", 1650, 4): {"total": 100.10, "last_400": 24.05},
    ("HV_Turf", 1650, 5): {"total": 100.30, "last_400": 24.20},
    ("HV_Turf", 1800, 0): {"total": 108.95, "last_400": 23.90},
    ("HV_Turf", 1800, 2): {"total": 109.15, "last_400": 23.95},
    ("HV_Turf", 1800, 3): {"total": 109.45, "last_400": 23.95},
    ("HV_Turf", 1800, 4): {"total": 109.65, "last_400": 24.30},
    ("HV_Turf", 1800, 5): {"total": 109.95, "last_400": 24.55},
    ("HV_Turf", 2200, 3): {"total": 136.60, "last_400": 24.30},
    ("HV_Turf", 2200, 4): {"total": 137.05, "last_400": 24.35},
    ("HV_Turf", 2200, 5): {"total": 137.35, "last_400": 24.60},
    # ── Sha Tin All Weather Track ──
    ("ST_AWT", 1200, 2): {"total": 68.35, "last_400": 23.05},
    ("ST_AWT", 1200, 3): {"total": 68.55, "last_400": 23.20},
    ("ST_AWT", 1200, 4): {"total": 68.95, "last_400": 23.55},
    ("ST_AWT", 1200, 5): {"total": 69.35, "last_400": 23.65},
    ("ST_AWT", 1650, 1): {"total": 97.80, "last_400": 23.90},
    ("ST_AWT", 1650, 2): {"total": 98.40, "last_400": 23.90},
    ("ST_AWT", 1650, 3): {"total": 98.60, "last_400": 23.95},
    ("ST_AWT", 1650, 4): {"total": 99.05, "last_400": 24.15},
    ("ST_AWT", 1650, 5): {"total": 99.45, "last_400": 24.30},
    ("ST_AWT", 1800, 3): {"total": 108.05, "last_400": 23.95},
    ("ST_AWT", 1800, 4): {"total": 108.55, "last_400": 24.15},
    ("ST_AWT", 1800, 5): {"total": 109.45, "last_400": 24.30},
}


def get_hkjc_standard(venue, distance, race_class, track_type="Turf"):
    """Look up HKJC reference standard time for venue/distance/class.
    Returns dict {"total": s, "last_400": s, "early": s} or None if not found.
    Falls back to nearest class if exact match unavailable."""
    # Build venue_surface key
    if track_type != "Turf" or (venue == "ST" and track_type != "Turf"):
        vs = "ST_AWT"
    elif venue == "HV":
        vs = "HV_Turf"
    else:
        vs = "ST_Turf"

    cls_num = int(race_class) if race_class and race_class > 0 else 0

    ref = HKJC_STANDARD_TIMES.get((vs, distance, cls_num))
    if ref is None:
        # Try nearest class (±1)
        for delta in [1, -1, 2, -2]:
            ref = HKJC_STANDARD_TIMES.get((vs, distance, cls_num + delta))
            if ref:
                break
    if ref is None:
        return None
    return {"total": ref["total"], "last_400": ref["last_400"],
            "early": round(ref["total"] - ref["last_400"], 2)}


# ── Empirical pace-style multiplier tables (v4.2: DISABLED — kept as reference) ──
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


def predict_race_pace_v3(horses, distance, going="Good", venue="ST",
                         race_class=None, track_type="Turf"):
    """v4.2: Pace prediction anchored to HKJC Official Reference Sectional Times.

    Pace deviation = predicted_early_sectional - HKJC_standard_early_sectional
    Positive = slower than HKJC standard.  Negative = faster.

    The heuristic pace pressure model (leader fractions, ESZ) predicts the
    OFFSET from HKJC standard pace for the specific venue/distance/class.
    Venue-specific ad-hoc offsets (ST -0.45s, HV -0.35s) are replaced by
    the inherent venue/class/distance differences in the HKJC table.

    Thresholds (unchanged — describe deviation from standard):
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

    # v4.2: HKJC-anchored approach — look up reference standard time for this
    # venue/distance/class.  The HKJC table already captures venue-specific pace
    # characteristics, so the old ad-hoc venue offsets (ST -0.45s, HV -0.35s)
    # are no longer needed.  Instead, we note the reference anchor.
    hkjc_ref = get_hkjc_standard(venue, distance, race_class, track_type)
    hkjc_note = ""
    if hkjc_ref:
        hkjc_note = f", HKJC std={hkjc_ref['total']:.2f}s (early={hkjc_ref['early']:.2f}s)"

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

    hv_note = ""
    st_note = ""
    reasons.append(f"Pred dev={predicted_dev:+.2f}s "
                   f"(PI={pace_index:.3f}, dist x{dist_factor:.1f}, going x{going_factor:.2f}{hkjc_note})")

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
    """v4.2: Risk metric removed (backtest ρ=-0.017, no predictive value).
    Only win probability is computed now."""
    df["win_prob"] = compute_win_probabilities(df, race_class=race.get("race_class"))
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
                              today_distance=None, draw_off=None):
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
             n_same_surface_runs, n_nearby_dist_runs, context_adj_total, quality_effect).
    If no valid runs, returns (None, 0, [], [], [], 0, 0, 0.0, 1.0).

    v4.0 Changes:
      - Change 2: Contextualise — subtracts historical draw offset and wide-running cost
        from each run's residual, revealing true ability.
      - Change 3: Asymmetric distance — stepping up/down weighted differently.
      - Change 4: Race quality — runs from better-franked races weighted higher.
    """
    runs = db[db["horse_name"] == horse_name].copy()
    if len(runs) == 0:
        return None, 0, [], [], [], 0, 0, 0.0, 1.0

    runs = runs.sort_values("race_date", ascending=True).reset_index(drop=True)

    residuals = []
    surfaces = []   # v3.4 (I): parallel list of surface per valid run
    venues = []     # v3.4 (K): parallel list of venue (HV/ST) per valid run
    distances = []  # v3.4.1 (M): parallel list of distance per valid run
    places = []     # v3.4.2 (Q): parallel list of finishing place per valid run
    race_dates_l = []      # v4.0 Change 4: for race quality lookup
    race_numbers_l = []    # v4.0 Change 4
    context_draw_adjs = [] # v4.0 Change 2: draw context adj per run
    context_wide_adjs = [] # v4.0 Change 2: wide-cost adj per run
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

            # v4.0 Change 2: Contextualise — remove historical draw effect
            draw_num_i = run.get("draw")
            _ctx_draw = 0.0
            if draw_off is not None and pd.notna(draw_num_i):
                _ctx_draw = _get_historical_draw_offset(
                    draw_off, dist, rc, int(draw_num_i))
                adj_resid -= _ctx_draw
            # v4.0 Change 2: Remove estimated wide-running positional cost
            _ctx_wide = 0.0
            _rp_str = run.get("running_positions", "")
            _first_call = _parse_first_position(_rp_str)
            _fs = _get_field_size(run.get("race_date"), run.get("race_number"), db)
            if _first_call is not None and _fs > 0:
                _ctx_wide = _estimate_wide_cost(
                    draw_num_i, _fs, _first_call, rc)
                adj_resid -= _ctx_wide

            residuals.append(adj_resid)
            surfaces.append(tt)
            venues.append(str(venue))
            distances.append(dist)
            places.append(place_val)
            race_dates_l.append(run.get("race_date"))
            race_numbers_l.append(run.get("race_number"))
            context_draw_adjs.append(_ctx_draw)
            context_wide_adjs.append(_ctx_wide)

    n_valid = len(residuals)
    if n_valid == 0:
        return None, 0, [], [], [], 0, 0, 0.0, 1.0

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

    # v4.0 Change 3: Asymmetric distance penalties — stepping down (to shorter)
    # vs stepping up (to longer) have different relevance weights.
    # Positive direction = horse ran longer before → stepping DOWN to shorter today.
    # Negative direction = horse ran shorter before → stepping UP to longer today.
    if today_distance is not None:
        for i, d in enumerate(distances):
            direction = d - today_distance
            abs_diff = abs(direction)
            if abs_diff == 0:
                dist_mult = 1.0  # exact match — full weight
            elif direction > 0:  # stepping DOWN (from longer to shorter)
                if abs_diff <= 100:
                    dist_mult = DIST_STEP_DOWN_NEAR
                elif abs_diff <= 200:
                    dist_mult = DIST_STEP_DOWN_MED
                else:
                    dist_mult = DIST_STEP_DOWN_FAR
            else:  # stepping UP (from shorter to longer)
                if abs_diff <= 100:
                    dist_mult = DIST_STEP_UP_NEAR
                elif abs_diff <= 200:
                    dist_mult = DIST_STEP_UP_MED
                else:
                    dist_mult = DIST_STEP_UP_FAR
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

    # v4.2: Form franking REMOVED (backtest: high-RQ-weight horses performed worse).
    # Race quality weights forced to neutral 1.0.
    # for i in range(n_valid):
    #     rq_w = get_race_quality_weight(race_dates_l[i], race_numbers_l[i])
    #     weights[i] *= rq_w

    weights /= weights.sum()
    recency_resid = float(np.dot(residuals, weights))

    # v4.0 diagnostics
    context_adj_total = round(sum(context_draw_adjs) + sum(context_wide_adjs), 4)
    quality_effect = 1.0  # v4.2: form franking removed

    return (recency_resid, n_valid, residuals, surfaces, venues,
            n_same_surface, n_nearby_dist, context_adj_total, quality_effect)


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


def project_race(race, class_fine, fine, coarse, ultra, draw_off, db=None, sec_devs=None):
    """v4.4: project_race with runtime profiles, contextualised residuals,
    and sectional decomposition integration."""
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

        # v4.0 Change 1: Compute horse profile at runtime from DB (replaces ability file)
        if db is not None:
            profile = compute_horse_profile_runtime(
                h["horse_name"], db, class_fine, fine, coarse, ultra)
        else:
            profile = {
                "dominant_style": "Unknown", "leader_frac": 0.0, "front_frac": 0.0,
                "early_speed_z": 0.0, "style_entropy": 1.0, "residual_std": np.nan,
                "best_residual": np.nan, "worst_residual": np.nan, "n_runs": 0,
                "n_sectional_runs": 0, "shrink_factor": 0.0, "confidence": "no_data",
                "ability_v3": np.nan,
            }
        ability_v3     = profile["ability_v3"]
        shrunk_resid   = np.nan  # v4.0: no longer pre-shrunk; fallback not used
        raw_resid      = profile["ability_v3"]  # mean raw residual for reporting
        shrink_factor  = profile["shrink_factor"]
        shrink_k       = PROFILE_SHRINKAGE_K
        n_runs         = profile["n_runs"]
        confidence     = profile["confidence"]
        dom_style      = profile["dominant_style"]
        leader_frac    = profile["leader_frac"]
        front_frac     = profile["front_frac"]
        early_speed_z  = profile["early_speed_z"]
        style_entropy  = profile["style_entropy"]
        residual_std   = profile["residual_std"]
        best_residual  = profile["best_residual"]
        worst_residual = profile["worst_residual"]
        n_sectional_runs = profile["n_sectional_runs"]

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
        context_adj = 0.0       # v4.0 Change 2 diagnostic
        quality_effect = 1.0    # v4.0 Change 4 diagnostic
        trajectory_info = {"trajectory": "Insufficient", "bonus": 0.0, "penalty": 0.0}
        if db is not None:
            recency_resid, n_valid_runs, residuals_list, surfaces_list, venues_list, \
                n_same_surface_runs, n_nearby_dist_runs, context_adj, quality_effect = \
                compute_recency_residual(
                    h["horse_name"], db, class_fine, fine, coarse, ultra,
                    today_track_type=track_type, today_venue=MEETING_VENUE,
                    today_distance=distance, draw_off=draw_off)
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
            # v4.1: Trajectory bonus/penalty REMOVED — the recency-weighted
            # residual (λ=0.85) already captures form trends. Adding trajectory
            # on top double-counts improvement/decline, and the fixed cap
            # (-0.20s/+0.25s) acts as a blunt binary flag rather than a gradient.
        elif pd.notna(shrunk_resid):
            # Fallback: no recency data — use pre-computed shrunk (rare)
            effective_resid = shrunk_resid
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

        # ── v4.1: Distance penalties REMOVED ──
        # v3.4.1 distance_pen and v3.4.2 forced_dist_pen are no longer applied.
        # The v4.0 asymmetric distance weighting in compute_recency_residual
        # already discounts off-distance runs (e.g., DIST_STEP_UP_MED=0.30 for
        # 200m mismatch). Adding flat penalties on top double-counts the uncertainty.
        # Example: DASH (9 runs at 1200m, 0 at 1400m) was getting +0.24s penalty
        # despite residuals already being down-weighted by 70%.
        distance_pen = 0.0
        forced_dist_pen = 0.0

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

        # ── v4.2: Consistency flag REMOVED (backtest: inverted signal) ──
        consistency_flag = ""

        # ── v4.4: Sectional decomposition adjustment ──────────────────────────
        sec_profile = compute_sectional_profile(h["horse_name"], sec_devs)
        sec_adj = 0.0
        sec_dist_adj = 0.0
        sec_std_adj = 0.0
        if (sec_profile["n_sec_profile"] >= SEC_MIN_RUNS
                and sec_profile["avg_late_dev"] is not None):
            # Component 1: Late finishing ability signal (all distances)
            # Negative avg_late_dev = strong finisher → saves time
            sec_adj = sec_profile["avg_late_dev"] * SEC_LATE_DEV_COEFF

            # Component 2: Distance suitability from SSI
            ssi = sec_profile["avg_ssi"]
            if ssi is not None:
                if distance >= SEC_SSI_DIST_THRESH and ssi > 0.15:
                    # Front-loaded at 1600m+: extra fade risk penalty
                    sec_dist_adj = max(0, ssi - 0.15) * SEC_SSI_DIST_COEFF
                elif distance <= 1200 and ssi > 0:
                    sec_dist_adj = ssi * SEC_SSI_SPRINT_COEFF

            # Component 3: Reliability uncertainty from late_std
            ls = sec_profile["late_std"]
            if ls is not None and ls > SEC_LATE_STD_THRESH:
                sec_std_adj = (ls - SEC_LATE_STD_THRESH) * SEC_LATE_STD_PENALTY

            total_sec_adj = sec_adj + sec_dist_adj + sec_std_adj
            if pd.notna(proj) and total_sec_adj != 0:
                proj += total_sec_adj

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
            "sec_late_adj": round(sec_adj, 4),
            "sec_dist_adj": round(sec_dist_adj, 4),
            "sec_std_adj": round(sec_std_adj, 4),
            "sec_total_adj": round(sec_adj + sec_dist_adj + sec_std_adj, 4),
            "avg_late_dev": sec_profile["avg_late_dev"],
            "avg_ssi": sec_profile["avg_ssi"],
            "late_std": sec_profile["late_std"],
            "sec_type": sec_profile["sec_type"],
            "n_sec_profile": sec_profile["n_sec_profile"],
            "n_same_surface": n_same_surface_runs,
            "n_nearby_dist": n_nearby_dist_runs,
            "context_adj": round(context_adj, 4),        # v4.0 Change 2 diagnostic
            "quality_effect": round(quality_effect, 3),   # v4.0 Change 4 diagnostic
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
        horse_data, distance, going, venue=MEETING_VENUE,
        race_class=race.get("race_class"), track_type=track_type)

    # v4.2: PACE_STYLE_MULTIPLIERS removed (backtest: inverted — penalised horses
    # outperformed beneficiaries). Pace adjustment set to 0 for all horses.
    # Style/sectional data preserved for informational display.
    for hd in horse_data:
        hd["pace_adj"] = 0.0
        hd["pace_multiplier"] = 1.0
        hd["style_gated"] = False
        hd["leader_congestion"] = False
        if hd["proj_pre_pace"] is not None:
            hd["projected_time"] = round(hd["proj_pre_pace"], 2)

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

    # ── v4.3: Speed-map positional + pace×position×style adjustments ──────────
    # Compute speed map from the initial projected_time ordering, then apply
    # positional adjustments BEFORE final ranking.
    df_pre = pd.DataFrame(horse_data)
    smap_data = compute_speed_map(df_pre, race, pace_label)
    smap_adj = compute_smap_time_adjustment(smap_data, pace_label)
    for hd in horse_data:
        adj_info = smap_adj.get(hd["horse_name"], {})
        hd["smap_pos_adj"]   = adj_info.get("smap_pos_adj", 0.0)
        hd["smap_pps_adj"]   = adj_info.get("smap_pps_adj", 0.0)
        hd["smap_total_adj"] = adj_info.get("smap_total_adj", 0.0)
        hd["smap_adj_notes"] = adj_info.get("smap_adj_notes", "")
        if hd["projected_time"] is not None and hd["smap_total_adj"] != 0:
            hd["projected_time"] = round(
                hd["projected_time"] + hd["smap_total_adj"], 2)

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

    # v4.1: Trajectory bonus/penalty removed from projections. 
    # Still detect and report as informational (no time adjustment applied).
    if "trajectory" in df.columns:
        improving = df[(df["trajectory"] == "Improving") & df["projected_time"].notna()]
        if len(improving):
            parts = [f"{r['horse_name']} (Δ={r['traj_delta']:+.2f}s)"
                     for _, r in improving.iterrows()]
            lines.append(f"↑ Improving form: {'; '.join(parts[:5])}.")

        declining = df[(df["trajectory"] == "Declining") & df["projected_time"].notna()]
        if len(declining):
            parts = [f"{r['horse_name']} (Δ={r['traj_delta']:+.2f}s)"
                     for _, r in declining.iterrows()]
            lines.append(f"↓ Declining form: {'; '.join(parts[:5])}.")

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

    # v4.4: Sectional decomposition commentary
    if "avg_ssi" in df.columns and "sec_total_adj" in df.columns:
        strong_fin = df[(df["avg_ssi"].notna()) & (df["avg_ssi"] < -0.15) & df["projected_time"].notna()]
        if len(strong_fin):
            parts = [f"{r['horse_name']} (SSI={r['avg_ssi']:+.2f}, adj={r['sec_total_adj']:+.3f}s)"
                     for _, r in strong_fin.sort_values("avg_ssi").head(3).iterrows()]
            dist_note = " — sectional shape strongly favours these at this distance" if race["distance"] >= 1600 else ""
            lines.append(f"⚡ Strong finishers: {'; '.join(parts)}{dist_note}.")

        front_load = df[(df["avg_ssi"].notna()) & (df["avg_ssi"] > 0.15) & df["projected_time"].notna()]
        if len(front_load) and race["distance"] >= 1600:
            parts = [f"{r['horse_name']} (SSI={r['avg_ssi']:+.2f}, adj={r['sec_total_adj']:+.3f}s)"
                     for _, r in front_load.sort_values("avg_ssi", ascending=False).head(3).iterrows()]
            lines.append(f"⚠ Front-loaded at {race['distance']}m: {'; '.join(parts)}. "
                         f"Fade risk — SSI data shows {race['distance']}m+ penalises this profile.")

        # High late_std (inconsistent finishing)
        high_var = df[(df["late_std"].notna()) & (df["late_std"] > 0.40) & df["projected_time"].notna()]
        if len(high_var):
            parts = [f"{r['horse_name']} (late_σ={r['late_std']:.2f})"
                     for _, r in high_var.sort_values("late_std", ascending=False).head(3).iterrows()]
            lines.append(f"⚠ Inconsistent finishers: {'; '.join(parts)}. "
                         f"High variance in late sectional — unreliable projection.")

    return lines


# ══════════════════════════════════════════════════════════════════════════════
# 6b. Speed Map
# ══════════════════════════════════════════════════════════════════════════════

SMAP_ROWS = 4   # max lateral lanes (1 = rail, 4 = widest)
SMAP_COLS = 6   # max longitudinal slots (1 = last, 6 = lead)


def compute_smap_time_adjustment(smap_data, pace_label):
    """v4.3: Compute projected_time adjustments from speed map position.

    Two components:
      1. Positional cost/benefit: smap_advantage × SMAP_TIME_COEFF
      2. Pace × Position × Style interaction: lookup from PACE_POS_STYLE_ADJ

    Returns dict: {horse_name → {"smap_pos_adj": float, "smap_pps_adj": float,
                                  "smap_total_adj": float, "smap_adj_notes": str}}
    """
    ROW_LABEL = {1: "RAIL", 2: "W2", 3: "WIDE"}
    # Bucket pace_label into Fast / Normal / Slow
    pl = str(pace_label).lower()
    if "fast" in pl:
        pace_bucket = "Fast"
    elif "slow" in pl:
        pace_bucket = "Slow"
    else:
        pace_bucket = "Normal"

    adjustments = {}
    for h in smap_data:
        name = h["horse_name"]
        advantage = h.get("smap_advantage", 0.0)
        row = h.get("smap_row", 2)
        style = h.get("dominant_style", "Unknown")
        row_label = ROW_LABEL.get(row, "W2")

        # Module 1: Positional cost
        pos_adj = -advantage * SMAP_TIME_COEFF
        pos_adj = max(-SMAP_TIME_CAP, min(SMAP_TIME_CAP, pos_adj))

        # Module 3: Pace × Position × Style
        if style in ("Leader", "On-Pace", "Midfield", "Closer"):
            pps_adj = PACE_POS_STYLE_ADJ.get((pace_bucket, row_label, style), 0.0)
        else:
            pps_adj = 0.0

        total = round(pos_adj + pps_adj, 4)

        notes_parts = []
        if pos_adj != 0:
            notes_parts.append(f"position {'saves' if pos_adj < 0 else 'costs'} {abs(pos_adj):.3f}s")
        if pps_adj != 0:
            notes_parts.append(f"pace×lane×style {'helps' if pps_adj < 0 else 'hurts'} {abs(pps_adj):.3f}s")
        notes = "; ".join(notes_parts) if notes_parts else "no positional effect"

        adjustments[name] = {
            "smap_pos_adj": round(pos_adj, 4),
            "smap_pps_adj": round(pps_adj, 4),
            "smap_total_adj": round(total, 4),
            "smap_adj_notes": notes,
        }
    return adjustments


def compute_speed_map(df, race, pace_label="Normal"):
    """Compute predicted early-race positions for all horses.

    Returns list of dicts with smap_col, smap_row, smap_advantage, etc.

    Grid rules (uniform for all races):
      - 3 rows: RAIL (1), middle (2), WIDE (3). Max 3-4 horses on WIDE row.
      - 5-6 columns (length): 5 for ≤10 runners, 6 for 11+.
      - Column (front/back) driven by ESZ: lowest ESZ → front, highest → back.
      - Row (rail/wide) driven by DRAW: low draw → rail, high draw → wide.
        BUT: wide-drawn speed horses (Leader/On-Pace with good ESZ) push forward
        and stay wide. Wide-drawn closers drop back to find a better lane toward
        rail/middle. All horses PREFER less ground (rail) but are forced wide
        by suboptimal draw gates.
      - ST 1000m straight: draw reversed (gate 1 = widest).
    """

    field_size = len(df)
    if field_size == 0:
        return []

    distance = race["distance"]
    venue = MEETING_VENUE
    is_straight = (venue == "ST" and distance == 1000)

    # ── Fixed grid: 3 rows, N cols ──
    # Choose n_cols so each column gets ~3 horses → all 3 rows fill naturally.
    # ceil(field/3) keeps columns ≈ 3 deep; cap 3-6 for visual size.
    n_rows = 3
    n_cols = max(3, min(6, math.ceil(field_size / 3)))
    wide_cap = 4  # max horses on WIDE row

    # ── 1. Build horse list ──
    horses = []
    for _, r in df.iterrows():
        esz = r.get("early_speed_z", 0)
        if pd.isna(esz):
            esz = 0.5
        horses.append({
            "horse_name": r["horse_name"],
            "horse_no": int(r["horse_no"]),
            "draw": int(r["draw"]) if pd.notna(r.get("draw")) else field_size // 2,
            "esz": esz,
            "dominant_style": r.get("dominant_style", "Unknown"),
            "leader_frac": r.get("leader_frac", 0),
            "front_frac": r.get("front_frac", 0),
            "projected_time": r.get("projected_time"),
            "rank": int(r.get("rank", 99)),
            "win_prob": r.get("win_prob", 0),
        })

    # ── 2. Assign column by ESZ (front-to-back) ──
    # Sort by ESZ ascending: most negative (fastest early speed) → front
    horses.sort(key=lambda h: h["esz"])
    horses_per_col = max(1, math.ceil(field_size / n_cols))

    for i, h in enumerate(horses):
        col = n_cols - (i // horses_per_col)
        h["smap_col"] = max(1, min(n_cols, col))

    # ── 3. Assign row by DRAW rank within each column ──
    # Within each column the lowest-draw horse gets RAIL, next gets W2,
    # highest-draw gets WIDE. This mirrors real jockey behavior — everyone
    # settles inside, and only the widest-drawn in each pace group stays out.
    grid = {}

    for col in range(n_cols, 0, -1):
        col_horses = [h for h in horses if h["smap_col"] == col]
        if not col_horses:
            continue

        # Sort within column by draw: low draw → rail priority
        if is_straight:
            col_horses.sort(key=lambda h: -h["draw"])  # ST 1000m reversed
        else:
            col_horses.sort(key=lambda h: h["draw"])

        # Assign rows sequentially: 1st → RAIL, 2nd → W2, 3rd → WIDE
        for idx, h in enumerate(col_horses):
            row = min(idx + 1, n_rows)  # 0-based idx → 1-based row, cap at n_rows
            # Enforce WIDE cap
            if row == n_rows:
                wt = sum(1 for (c, r) in grid if r == n_rows)
                if wt >= wide_cap:
                    row = max(1, n_rows - 1)
            # Collision: find nearest open slot in this column
            if (col, row) in grid:
                placed = False
                for try_row in range(1, n_rows + 1):
                    if (col, try_row) not in grid:
                        if try_row == n_rows:
                            wt2 = sum(1 for (c, r) in grid if r == n_rows)
                            if wt2 >= wide_cap:
                                continue
                        row = try_row
                        placed = True
                        break
                if not placed:
                    # Overflow: adjacent column
                    for alt_col in [col - 1, col + 1]:
                        if alt_col < 1 or alt_col > n_cols:
                            continue
                        for try_row in range(1, n_rows + 1):
                            if (alt_col, try_row) not in grid:
                                if try_row == n_rows:
                                    wt3 = sum(1 for (c, r) in grid if r == n_rows)
                                    if wt3 >= wide_cap:
                                        continue
                                h["smap_col"] = alt_col
                                row = try_row
                                placed = True
                                break
                        if placed:
                            break
            h["smap_row"] = row
            grid[(h["smap_col"], row)] = h

    # Store grid dimensions
    for h in horses:
        h["_n_rows"] = n_rows
        h["_n_cols"] = n_cols

    # ── 4. Compute positional advantage/disadvantage ──
    for h in horses:
        notes = []
        advantage = 0.0
        style = h["dominant_style"]
        col = h["smap_col"]
        row = h["smap_row"]

        # a. Rail at bends saves ground
        if not is_straight:
            if row == 1 and col >= n_cols - 1:
                if style in ("Leader", "On-Pace"):
                    notes.append("Inside + forward — saves ground at bend")
                    advantage += 0.5
            elif row == 3 and col >= n_cols - 1:
                notes.append("Wide + forward — loses ground at bend")
                advantage -= 0.4

        # b. Style-position alignment
        if style == "Leader":
            if col >= n_cols - 1 and row <= 2:
                notes.append("Leader on rail — controlling position")
                advantage += 0.3
        elif style == "Closer":
            if col <= 2 and row >= 2:
                notes.append("Closer behind — clear running room for late surge")
                advantage += 0.3
            elif col <= 2 and row == 1:
                notes.append("Closer on rail — saves ground, needs clear run")
                advantage += 0.1
        elif style in ("Midfield", "On-Pace"):
            if row == 1 and 2 <= col <= n_cols - 2:
                has_cover = (grid.get((col + 1, row)) is not None
                             or grid.get((col, row + 1)) is not None)
                if has_cover:
                    notes.append("Rail midfield — boxed-in risk")
                    advantage -= 0.3

        # c. Wide draw + forward = energy cost
        if not is_straight and h["draw"] > field_size * 0.7 and col >= n_cols - 1:
            notes.append(f"Wide draw ({h['draw']}) pushing forward — uses energy")
            advantage -= 0.3

        # d. Low draw + speed = ideal rail
        if not is_straight and h["draw"] <= max(2, field_size * 0.2) and h["esz"] < -0.3:
            notes.append("Inside draw + natural speed — ideal rail position")
            advantage += 0.3

        # e. HV tight turns penalty
        if venue == "HV" and not is_straight and row == 3:
            notes.append("Wide at HV — extra ground on tight turns")
            advantage -= 0.3

        # f. ST 1000m straight-specific
        if is_straight:
            if h["draw"] >= field_size * 0.8:
                notes.append("Near-rail draw (ST 1000m reversed)")
                advantage += 0.2
            elif h["draw"] <= max(2, field_size * 0.2):
                notes.append("Widest draw (ST 1000m reversed)")
                advantage -= 0.2

        h["smap_advantage"] = round(advantage, 2)
        h["smap_notes"] = "; ".join(notes) if notes else "Neutral position"

        # Position score for model
        if style in ("Leader", "On-Pace"):
            pos_quality = col / n_cols
        else:
            pos_quality = (n_cols - col + 1) / n_cols
        rail_bonus = (n_rows - row + 1) / n_rows * 0.3 if not is_straight else 0.1
        h["smap_position_score"] = round(pos_quality + rail_bonus + advantage * 0.1, 3)

    return horses


def speed_map_beneficiaries(smap_data, race, pace_label):
    """Select up to 4 horses that benefit most from their speed map position.

    Logic:
    1. Positional advantage (high smap_advantage)
    2. Style-position alignment (right style for where they are)
    3. Clear running lanes (not boxed in)
    4. Ground-saving potential at bends
    5. Cross-reference with projected rank (strong horse in good position > weak horse in good position)

    Returns list of dicts: {horse_name, reason, advantage, rank}
    """
    if not smap_data:
        return []

    distance = race["distance"]
    venue = MEETING_VENUE
    is_straight = (venue == "ST" and distance == 1000)
    n_cols = smap_data[0].get("_n_cols", SMAP_COLS) if smap_data else SMAP_COLS
    n_rows = smap_data[0].get("_n_rows", SMAP_ROWS) if smap_data else SMAP_ROWS

    candidates = []
    for h in smap_data:
        score = 0.0
        reasons = []
        style = h["dominant_style"]
        col = h["smap_col"]
        row = h["smap_row"]

        # Base: positional advantage
        score += h.get("smap_advantage", 0) * 2.0

        # Bonus: strong horse (top-6 projected) in a good position
        if h["rank"] <= 3 and h["smap_advantage"] >= 0:
            score += 0.5
            reasons.append(f"top-{h['rank']} ranked + favourable position")
        elif h["rank"] <= 6 and h["smap_advantage"] > 0:
            score += 0.3
            reasons.append(f"#{h['rank']} ranked with positional edge")

        # Bonus: speed type on rail at front saves the most ground
        if style in ("Leader", "On-Pace") and row <= 2 and col >= n_cols - 1 and not is_straight:
            score += 0.6
            reasons.append("saves most ground at bends from forward rail position")

        # Bonus: closer/midfielder with clear outside run
        if style in ("Closer", "Midfield") and row >= 3 and col <= n_cols // 2:
            score += 0.3
            reasons.append("wide + back — clear path to build momentum on straight")

        # Bonus: inside draw with matching speed (ground-saving run likely)
        if h["draw"] <= max(2, len(smap_data) * 0.2) and h["esz"] < 0 and not is_straight:
            score += 0.4
            reasons.append(f"draw {h['draw']} with natural speed — economical ground-saving trip")

        # Penalty: poor position for style
        if style == "Leader" and col < n_cols - 2:
            score -= 0.5
        if style == "Closer" and col >= n_cols - 1:
            score -= 0.3

        # ST 1000m: high draw (= near rail, reversed) + speed is ideal
        if is_straight:
            if h["draw"] >= len(smap_data) * 0.7 and h["esz"] < 0:
                score += 0.4
                reasons.append(f"ST 1000m: high gate={h['draw']} (near-rail reversed) + early speed")

        # Slow pace: leaders who can control from front
        if pace_label in ("Slow", "Very Slow", "Slightly Slow"):
            if style == "Leader" and col >= n_cols - 1:
                score += 0.4
                reasons.append("expected slow pace — can dictate from front and control")
            # Congested pack penalty for rail midfielders
            if style == "Midfield" and row <= 1 and 2 <= col <= n_cols - 2:
                score -= 0.3

        # Fast pace: closers who sit back benefit most
        if pace_label in ("Fast", "Very Fast", "Slightly Fast"):
            if style in ("Closer",) and col <= 2:
                score += 0.4
                reasons.append("fast expected pace — closers benefit from leaders tiring")

        if not reasons:
            if h["smap_advantage"] > 0:
                reasons.append(h.get("smap_notes", "favourable position"))
            else:
                continue  # skip horses with no clear reason

        candidates.append({
            "horse_name": h["horse_name"],
            "horse_no": h["horse_no"],
            "score": round(score, 2),
            "rank": h["rank"],
            "advantage": h["smap_advantage"],
            "reasons": reasons,
            "style": style,
            "draw": h["draw"],
            "esz": h["esz"],
            "col": col,
            "row": row,
        })

    # Sort by score, take top 4
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:4]


def speed_map_text(smap_data, race, pace_label):
    """Generate ASCII speed map for text report."""
    if not smap_data:
        return "  (No speed map data)\n"

    distance = race["distance"]
    venue = MEETING_VENUE
    is_straight = (venue == "ST" and distance == 1000)
    n_cols = smap_data[0].get("_n_cols", SMAP_COLS) if smap_data else SMAP_COLS
    n_rows = smap_data[0].get("_n_rows", SMAP_ROWS) if smap_data else SMAP_ROWS

    lines = []
    course_note = " (STRAIGHT)" if is_straight else ""
    lines.append(f"  Speed Map — {distance}m{course_note} | Pace: {pace_label} | "
                 f"Field: {len(smap_data)}")
    lines.append(f"  {'─' * 78}")

    grid = {}
    for h in smap_data:
        grid[(h["smap_col"], h["smap_row"])] = h

    # Column headers
    col_width = max(22, 110 // n_cols)  # wide enough for full horse names
    col_labels = ["← BACK"] + [""] * (n_cols - 2) + ["FRONT →"]
    if n_cols <= 2:
        col_labels = ["← BACK", "FRONT →"]

    header = "  " + "".join(f"{col_labels[c] if c < len(col_labels) else '':^{col_width}s}"
                            for c in range(n_cols))
    lines.append(header)
    lines.append(f"  {'─' * (col_width * n_cols)}")

    for row in range(n_rows, 0, -1):
        row_label = "RAIL" if row == 1 else f"W{row}" if row < n_rows else "WIDE"
        cells = []
        for col in range(1, n_cols + 1):
            h = grid.get((col, row))
            if h:
                name = f"{h['horse_no']}.{h['horse_name']}"
                if len(name) > col_width - 1:
                    name = name[:col_width - 2] + "…"
                cells.append(f"{name:<{col_width}s}")
            else:
                cells.append(" " * col_width)
        lines.append(f"  {''.join(cells)}  {row_label}")

    lines.append(f"  {'─' * (col_width * n_cols)}")

    # Positional notes
    noted = [h for h in smap_data if h["smap_advantage"] != 0]
    noted.sort(key=lambda h: h["smap_advantage"])
    if noted:
        lines.append("  Positional Factors:")
        for h in noted:
            sign = "+" if h["smap_advantage"] > 0 else ""
            lines.append(f"    {h['horse_name']:<22s}  [{sign}{h['smap_advantage']:.1f}]  {h['smap_notes']}")

    # Beneficiaries
    beneficiaries = speed_map_beneficiaries(smap_data, race, pace_label)
    if beneficiaries:
        lines.append("")
        lines.append("  ★ POSITIONAL BENEFICIARIES (max 4):")
        for i, b in enumerate(beneficiaries, 1):
            reason_str = "; ".join(b["reasons"])
            lines.append(f"    {i}. {b['horse_name']:<20s}  "
                         f"[Rk#{b['rank']}, Dr{b['draw']}, {b['style'][:6]}, "
                         f"ESZ={b['esz']:+.1f}]  → {reason_str}")

    return "\n".join(lines) + "\n"


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
            except Exception:
                pass

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
                           f"Model: v4.4 (sectional decomposition + race-median draws + ability-anchored + class-relative)",
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
                   "Draw\nOff", "SSI", "Fin\nSec", "ESZ", "Style", "Vet"]
    col_widths  = [10*mm, 9*mm, 45*mm, 10*mm, 9*mm, 10*mm,
                   14*mm, 14*mm, 13*mm, 14*mm, 13*mm, 10*mm,
                   12*mm, 12*mm, 15*mm, 11*mm, 17*mm, 14*mm]

    HDR_BG = colors.HexColor("#2C3E50")
    ROW_ALT = colors.HexColor("#F0F4F8")
    TOP1_BG = colors.HexColor("#D4EFDF")
    TOP2_BG = colors.HexColor("#EBF5FB")
    TOP3_BG = colors.HexColor("#FEF9E7")

    for (df, pace_label, pace_score, pace_reasons, leader_names, _smap), race in zip(all_results, races):

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
            ]

            # v4.4: SSI column (replaces Pace Adj which was always 0 since v4.2)
            ssi_val = r.get("avg_ssi")
            if ssi_val is not None and not (isinstance(ssi_val, float) and np.isnan(ssi_val)):
                if ssi_val < -0.15:
                    ssi_str = f'<font color="#27AE60"><b>{ssi_val:+.2f}</b></font>'
                elif ssi_val > 0.15:
                    ssi_str = f'<font color="#E74C3C">{ssi_val:+.2f}</font>'
                else:
                    ssi_str = f'{ssi_val:+.2f}'
            else:
                ssi_str = "-"
            row_cells.append(Paragraph(ssi_str, cell_center))

            row_cells += [
                Paragraph(fin_sec_str, cell_center),
                Paragraph(esz_str, cell_center),
                Paragraph(str(r.get("dominant_style", "?"))[:8], cell_center),
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

        # ── Speed Map (PDF) ──
        smap_data = compute_speed_map(df, race, pace_label)
        if smap_data:
            elements_for_race.append(Spacer(1, 3*mm))

            smap_title_style = ParagraphStyle("SmapTitle", parent=styles["Normal"],
                                               fontSize=9, leading=11, spaceBefore=1*mm,
                                               spaceAfter=1*mm,
                                               textColor=colors.HexColor("#2C3E50"))
            smap_cell = ParagraphStyle("SmapCell", parent=styles["Normal"],
                                        fontSize=6.5, leading=8, alignment=TA_CENTER)
            smap_label = ParagraphStyle("SmapLbl", parent=styles["Normal"],
                                         fontSize=6.5, leading=8, alignment=TA_CENTER,
                                         textColor=colors.HexColor("#777777"))
            smap_benef_style = ParagraphStyle("SmapBenef", parent=styles["Normal"],
                                               fontSize=7.5, leading=10, spaceBefore=1*mm,
                                               textColor=colors.HexColor("#1A5276"))

            # Get actual grid dimensions from smap_data
            s_n_cols = smap_data[0].get("_n_cols", SMAP_COLS) if smap_data else SMAP_COLS
            s_n_rows = smap_data[0].get("_n_rows", SMAP_ROWS) if smap_data else SMAP_ROWS
            smap_dist = race["distance"]
            is_straight = (MEETING_VENUE == "ST" and smap_dist == 1000)
            course_note = " (STRAIGHT)" if is_straight else ""

            elements_for_race.append(
                Paragraph(f"<b>Speed Map</b> — {smap_dist}m{course_note}  |  Pace: {pace_label}",
                          smap_title_style))

            # Build grid lookup
            smap_grid = {}
            for sh in smap_data:
                smap_grid[(sh["smap_col"], sh["smap_row"])] = sh

            # Table: s_n_cols data columns + 1 label column on right
            smap_table_data = []

            # Header row
            smap_hdr = []
            for ci in range(s_n_cols):
                if ci == 0:
                    lbl = "← BACK"
                elif ci == s_n_cols - 1:
                    lbl = "FRONT →"
                else:
                    lbl = ""
                smap_hdr.append(Paragraph(f"<b>{lbl}</b>", smap_label))
            smap_hdr.append(Paragraph("", smap_label))
            smap_table_data.append(smap_hdr)

            # Data rows: top (wide) to bottom (rail)
            for row in range(s_n_rows, 0, -1):
                row_label = "RAIL" if row == 1 else f"W{row}" if row < s_n_rows else "WIDE"
                smap_row_cells = []
                for col in range(1, s_n_cols + 1):
                    sh = smap_grid.get((col, row))
                    if sh:
                        name = f"{sh['horse_no']}.{sh['horse_name']}"
                        adv = sh.get("smap_advantage", 0)
                        if adv >= 0.3:
                            cell_text = f'<font color="#27AE60"><b>{name}</b></font>'
                        elif adv <= -0.3:
                            cell_text = f'<font color="#C0392B">{name}</font>'
                        else:
                            cell_text = name
                        smap_row_cells.append(Paragraph(cell_text, smap_cell))
                    else:
                        smap_row_cells.append(Paragraph("", smap_cell))
                smap_row_cells.append(Paragraph(row_label, smap_label))
                smap_table_data.append(smap_row_cells)

            # Dynamic column width: fit full names, fill available page width
            avail_w = landscape(A4)[0] - 40*mm  # page width minus margins
            smap_col_w = min(48*mm, max(30*mm, (avail_w - 12*mm) / s_n_cols))
            smap_tbl = Table(smap_table_data,
                             colWidths=[smap_col_w]*s_n_cols + [10*mm],
                             rowHeights=[4*mm] + [9*mm]*s_n_rows)

            # Clean styling — light borders, subtle row shading, no yellow
            smap_ts = [
                # Header bar
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#34495E")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                # Grid lines — subtle
                ("GRID", (0, 0), (s_n_cols - 1, -1), 0.4, colors.HexColor("#D5D8DC")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#2C3E50")),
                # Alignment
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                # Rail row (bottom data row) — very subtle green tint
                ("BACKGROUND", (0, s_n_rows), (s_n_cols - 1, s_n_rows),
                 colors.HexColor("#F0FAF0")),
                # Alternate row shading for readability
            ]
            for ri in range(1, s_n_rows + 1):
                if ri % 2 == 0 and ri != s_n_rows:
                    smap_ts.append(("BACKGROUND", (0, ri), (s_n_cols - 1, ri),
                                    colors.HexColor("#F8F9FA")))
            smap_tbl.setStyle(TableStyle(smap_ts))
            elements_for_race.append(smap_tbl)

            # Positional notes (compact)
            noted = [sh for sh in smap_data if sh["smap_advantage"] != 0]
            noted.sort(key=lambda sh: sh["smap_advantage"])
            if noted:
                note_parts = []
                for sh in noted[:6]:
                    sign = "+" if sh["smap_advantage"] > 0 else ""
                    note_parts.append(
                        f"{sh['horse_name']} [{sign}{sh['smap_advantage']:.1f}]: "
                        f"{sh['smap_notes']}")
                elements_for_race.append(
                    Paragraph("Positional: " + " | ".join(note_parts),
                              ParagraphStyle("SmapNotes", parent=styles["Normal"],
                                              fontSize=7, leading=9, spaceBefore=1*mm,
                                              textColor=colors.HexColor("#555555"))))

            # Beneficiaries
            beneficiaries = speed_map_beneficiaries(smap_data, race, pace_label)
            if beneficiaries:
                benef_parts = []
                for i, b in enumerate(beneficiaries, 1):
                    reason_str = "; ".join(b["reasons"])
                    benef_parts.append(
                        f"<b>{i}. {b['horse_name']}</b> "
                        f"[Rk#{b['rank']}, Dr{b['draw']}, {b['style'][:6]}] "
                        f"— {reason_str}")
                elements_for_race.append(
                    Paragraph("★ Positional Beneficiaries: " + " | ".join(benef_parts),
                              smap_benef_style))

        comments = race_commentary(df, race, pace_label, pace_reasons, leader_names)
        for c in comments:
            elements_for_race.append(Paragraph(f"• {c}", commentary_style))

        story.append(KeepTogether(elements_for_race))
        story.append(PageBreak())

    doc.build(story)
    print(f"  ✓ PDF saved: {path.name}")


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

        for (df, pace_label, pace_score, pace_reasons, leader_names, _smap), race in zip(all_results, races):
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
                   f"{'Style':<8s} {'Flags':<12s}\n")
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

                # Final sectional
                fs_val = r.get("proj_final_sec")
                fs_s = f"{fs_val:.2f}" if (fs_val is not None and not (isinstance(fs_val, float) and np.isnan(fs_val))) else "  -"

                # Flags: consistency + uncertainty + surface/distance penalties + style-gated
                flags = []
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
                    flags.append("↑IMP")
                elif traj == "Declining":
                    flags.append("↓DEC")
                flag_s = ",".join(flags) if flags else "-"

                f.write(f"{int(r['rank']):>3d}  {r['horse_name']:<22s} "
                        f"{r['weight']:>3d} {str(r['draw']) if pd.notna(r['draw']) else '-':>3s} "
                        f"{str(r['rating']) if pd.notna(r['rating']) else '-':>4s}  "
                        f"{et_s:>6s} {pt_s:>6s} {wp_s:>5s} {ab_s:>7s} {er_s:>7s} "
                        f"{sf_s:>4s} {do_s:>6s} {pa_s:>6s} {fs_s:>6s} {esz_s:>5s}  "
                        f"{st_s:<8s} {flag_s:<12s}\n")

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

            f.write("\n")

            # Speed map
            smap_data = compute_speed_map(df, race, pace_label)
            if smap_data:
                f.write(speed_map_text(smap_data, race, pace_label))
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
    print(f"RACE DAY ANALYSIS (v4.4) — {MEETING_TITLE}")
    print("=" * 80)
    print(f"\nv4.4: Sectional decomposition integration")
    print(f"  SEC. Late finishing signal: avg_late_dev × {SEC_LATE_DEV_COEFF} (additive to projection)")
    print(f"  SEC. Distance suitability: front-loaded SSI > 0.15 at ≥{SEC_SSI_DIST_THRESH}m penalised × {SEC_SSI_DIST_COEFF}")
    print(f"  SEC. Reliability: late_std > {SEC_LATE_STD_THRESH} adds {SEC_LATE_STD_PENALTY} per unit uncertainty")
    print(f"  SEC. Min profiled runs: {SEC_MIN_RUNS}")
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
    # Auto-detect format: new structured racecards have 'All Races' sheet
    try:
        _xl = pd.ExcelFile(RACE_CARD)
        if "All Races" in _xl.sheet_names:
            races = parse_race_card_v2(RACE_CARD)
            print("  (v4.0 structured racecard format detected)")
        else:
            races = parse_race_card(RACE_CARD)
            print("  (legacy racecard format detected)")
    except Exception:
        races = parse_race_card(RACE_CARD)
    for r in races:
        surface = "AWT" if r["is_awt"] else "Turf"
        print(f"  R{r['race_number']:>2d}  {r['distance']:>4d}m  {surface:>4s}  "
              f"Course={r['race_course']}  Class={r['race_class'] or 'Grp'}  "
              f"Runners={len(r['horses'])}")

    # 2. Load references
    print("\n[2] Loading v3 references + historical DB …")
    class_fine, fine, coarse, ultra, draw_off = load_references_v3()
    print(f"  ClassFine: {len(class_fine)}, Fine: {len(fine)}, "
          f"Coarse: {len(coarse)}, Ultra: {len(ultra)}")
    print(f"  Draw offsets: {len(draw_off)}")

    # v3.3: load historical DB for recency computation + final sectional
    db = pd.read_excel(DB_FILE)
    print(f"  Historical DB: {len(db)} records loaded from {DB_FILE.name}")

    # v4.2: Form franking removed — race quality precomputation skipped
    # compute_race_quality_scores(db, class_fine, fine, coarse, ultra)
    print("  Race quality scoring DISABLED (v4.2: form franking removed)")

    # v4.4: Precompute sectional deviations for all horse-runs
    print("  Precomputing sectional deviations …")
    sec_devs = precompute_sectional_deviations(db)
    n_horses_sec = len(sec_devs)
    n_runs_sec = sum(len(v) for v in sec_devs.values())
    print(f"  Sectional profiles: {n_horses_sec} horses, {n_runs_sec} profiled runs")

    # 3. Project
    print("\n[3] Projecting performances (v4.4 model) …")
    all_results = []
    for race in races:
        df, pace_label, pace_score, pace_reasons, leader_names = project_race(
            race, class_fine, fine, coarse, ultra, draw_off, db=db, sec_devs=sec_devs)
        df = enrich_with_risk(df, race)

        # v4.2: Enrich with speed map position data
        smap = compute_speed_map(df, race, pace_label)
        smap_lookup = {s["horse_name"]: s for s in smap}
        df["smap_col"] = df["horse_name"].map(lambda n: smap_lookup.get(n, {}).get("smap_col"))
        df["smap_row"] = df["horse_name"].map(lambda n: smap_lookup.get(n, {}).get("smap_row"))
        df["smap_position_score"] = df["horse_name"].map(lambda n: smap_lookup.get(n, {}).get("smap_position_score"))
        df["smap_advantage"] = df["horse_name"].map(lambda n: smap_lookup.get(n, {}).get("smap_advantage", 0))

        all_results.append((df, pace_label, pace_score, pace_reasons, leader_names, smap))
        matched = df["projected_time"].notna().sum()
        top_name = df.iloc[0]["horse_name"] if len(df) else "?"
        top_time = df.iloc[0]["projected_time"] if len(df) else "?"
        # v3.3: show flags summary
        n_unc = (df["uncertainty_penalty"] > 0).sum() if "uncertainty_penalty" in df.columns else 0
        n_sg = (df["style_gated"] == True).sum() if "style_gated" in df.columns else 0
        print(f"  R{race['race_number']:>2d}: {matched}/{len(df)} projected | "
              f"pace={pace_label} ({pace_score:+.2f}s) | "
              f"top: {top_name} ({top_time}s) | "
              f"flags: {n_unc}unc {n_sg}sg")

    # 4. Output
    print("\n[4] Generating outputs …")
    generate_text_report(all_results, races, OUT_TEXT)
    generate_pdf(all_results, races, OUT_PDF)

    # 4b. JSON output (for dashboard)
    import re as _re_date
    _m_d = _re_date.search(r'(\d{8})', OUT_PDF.name)
    _date_compact = _m_d.group(1) if _m_d else "00000000"
    out_json = OUT_PDF.parent / f"race_day_report_{_date_compact}_v4.4.json"

    # Load vet flags for JSON
    _vet_flags = {}
    _vet_json_path = OUT_PDF.parent / f"vet_report_{_date_compact}.json"
    if _vet_json_path.exists():
        try:
            with open(_vet_json_path, "r", encoding="utf-8") as _vf:
                _vd = json.load(_vf)
            for _rd in _vd.get("races", []):
                _vet_flags[_rd["race_number"]] = {
                    h["horse_no"]: h["max_flag"]
                    for h in _rd.get("horses", [])
                    if h.get("max_flag") in ("RED", "AMBER", "INFO")
                }
        except Exception:
            pass

    json_data = {
        "meeting_title": MEETING_TITLE,
        "meeting_venue": MEETING_VENUE,
        "model_version": "v4.4",
        "generated_at": datetime.now().isoformat(),
        "pdf_file": OUT_PDF.name,
        "text_file": OUT_TEXT.name,
        "races": [],
    }
    for (df, pace_label, pace_score, pace_reasons, leader_names, smap), race in zip(all_results, races):
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
                "proj_pre_pace": round(float(r["proj_pre_pace"]), 3) if pd.notna(r.get("proj_pre_pace")) else None,
                "win_prob": round(float(r.get("win_prob", 0)), 1),
                "risk_score": round(float(r.get("risk_score", 0)), 0),
                "risk_tier": str(r.get("risk_tier", "?")),
                "effective_resid": round(float(r.get("effective_resid", 0)), 3),
                "style": str(r.get("dominant_style", "?"))[:8],
                "early_speed_z": round(float(r.get("early_speed_z", 0)), 1),
                "proj_final_sec": round(float(r.get("proj_final_sec", 0)), 2) if pd.notna(r.get("proj_final_sec")) else None,
                "smap_pos_adj": round(float(r.get("smap_pos_adj", 0)), 4),
                "smap_pps_adj": round(float(r.get("smap_pps_adj", 0)), 4),
                "smap_total_adj": round(float(r.get("smap_total_adj", 0)), 4),
                "smap_adj_notes": str(r.get("smap_adj_notes", "")),
                "avg_late_dev": round(float(r["avg_late_dev"]), 3) if pd.notna(r.get("avg_late_dev")) else None,
                "avg_ssi": round(float(r["avg_ssi"]), 3) if pd.notna(r.get("avg_ssi")) else None,
                "late_std": round(float(r["late_std"]), 3) if pd.notna(r.get("late_std")) else None,
                "sec_type": str(r.get("sec_type", "No Data")),
                "sec_total_adj": round(float(r.get("sec_total_adj", 0)), 4),
                "n_sec_profile": int(r.get("n_sec_profile", 0)),
                "jockey": str(r.get("jockey", "")),
                "draw": int(r["draw"]) if pd.notna(r.get("draw")) else None,
                "weight": int(r["weight"]) if pd.notna(r.get("weight")) else None,
                "flags": flags,
                "vet_flag": _vet_flags.get(race["race_number"], {}).get(int(r["horse_no"]), ""),
            })

        # Speed map grid data for dashboard
        smap_grid = []
        n_cols_sm = smap[0]["_n_cols"] if smap else 6
        n_rows_sm = smap[0]["_n_rows"] if smap else 3
        beneficiaries = speed_map_beneficiaries(smap, race, pace_label)
        ben_names = [b["horse_name"] for b in beneficiaries]
        for sh in smap:
            smap_grid.append({
                "horse_name": sh["horse_name"],
                "horse_no": sh["horse_no"],
                "draw": sh["draw"],
                "col": sh["smap_col"],
                "row": sh["smap_row"],
                "advantage": sh["smap_advantage"],
                "notes": sh["smap_notes"],
                "style": sh["dominant_style"],
                "esz": round(sh["esz"], 1),
                "is_beneficiary": sh["horse_name"] in ben_names,
            })
        beneficiary_list = []
        for b in beneficiaries:
            beneficiary_list.append({
                "horse_name": b["horse_name"],
                "horse_no": b["horse_no"],
                "reason": b.get("reason", ""),
            })

        json_data["races"].append({
            "race_number": race["race_number"],
            "race_name": race.get("race_name", ""),
            "distance": race["distance"],
            "race_course": race["race_course"],
            "race_class": race["race_class"],
            "is_awt": race["is_awt"],
            "pace": pace_label,
            "pace_score": round(pace_score, 2),
            "pace_reasons": pace_reasons,
            "pace_leaders": leader_names,
            "runners": len(df),
            "projected": int(valid.shape[0]),
            "picks": picks,
            "speed_map": {
                "n_cols": n_cols_sm,
                "n_rows": n_rows_sm,
                "grid": smap_grid,
                "beneficiaries": beneficiary_list,
            },
        })
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)
    print(f"  ✓ JSON saved: {out_json.name}")

    # 5. Console summary
    print("\n" + "=" * 80)
    print("TOP 3 PER RACE (v4.4)")
    print("=" * 80)
    for (df, pace_label, _, _, _, _smap), race in zip(all_results, races):
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
            ssi_v = r.get("avg_ssi")
            ssi_s = f"SSI={ssi_v:+.2f}" if pd.notna(ssi_v) else "SSI=n/a"
            sec_adj_v = r.get("sec_total_adj", 0)
            sec_s = f"sec={sec_adj_v:+.3f}s" if sec_adj_v != 0 else ""
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
                  f"style={style}  {esz_s}  {ssi_s}  {sec_s}  {flag_s}")

    print(f"\nDone. PDF → {OUT_PDF.name}")


if __name__ == "__main__":
    main()
