# HKJC Horse Racing Prediction Model — v3.4.8 Technical Briefing

## 1. Environment & Setup

| Item | Detail |
|------|--------|
| Python | `C:\Users\tbhbr\miniconda3\python.exe` (NOT venv) |
| Encoding | `$env:PYTHONIOENCODING="utf-8"` before every Python call |
| Working dir | `20260208 ST Analysis (Updated)\` |
| OneDrive | Causes PermissionError on `.xlsx` — workaround: copy to `$env:TEMP` |

**Key Data Files:**
- `hkjc_results_updated.xlsx` — 17,366 historical race records
- `expected_time_references_v3.xlsx` — 4-tier expected-time lookup tables
- `horse_ability_analysis_v3.xlsx` — **DEPRECATED v4.0** (computed at runtime now)
- Race cards: `20262903 Race all form.xlsx` (YYYYDDMM format)
- Sheet names: Sheet16 (25 Mar), Sheet17 (29 Mar)

---

## 2. Model Architecture — Core Philosophy

**Time-based residual model.**  
Each horse's projected finish time = Expected Time + Ability Offset + Draw Offset + Pace-Style Adjustment + Penalties/Bonuses.

Win probabilities are derived via Boltzmann softmax over projected times, with class-tier temperature scaling.

### Pipeline (`race_day_analysis_YYYYMMDD_v3.4.8.py`)

1. **Parse race card** — Excel with multi-line cell headers
2. **Load references** — 4-tier ET cascade, draw offsets (runtime), historical DB
3. **Precompute race quality scores** — form franking (v4.0 Change 4)
4. **Project each race:**
   - Lookup expected time (ET): class_fine → fine → coarse → ultra cascade
   - Compute runtime horse profile (v4.0 Change 1 — replaces ability file)
   - Compute contextualised recency-weighted residual (v4.0 Changes 2, 3, 4)
   - Apply trajectory bonus/penalty (Improving / Declining / Stable)
   - Apply class-transition penalty (relative, not flat)
   - Predict race pace from field leader composition (see §4)
   - Apply pace-style multiplier per horse (see §4)
   - Apply draw offset (Bayesian shrunk, field-size scaled)
   - Apply uncertainty penalties (thin data, cross-surface, cross-distance)
   - Compute risk metric (5-component, 0–100 scale)
   - Compute win probabilities (Boltzmann + confidence deflation)
4. **Generate text report + PDF** (reportlab with MSJH CJK font)

---

## 3. Key Calibration Parameters

| Parameter | Value | Purpose |
|-----------|-------|---------|
| RECENCY_LAMBDA | 0.85 | Each prior run discounted by 15% |
| BLEND_RAW_WT | 0.80 | 80% raw / 20% shrunk for SF≥0.30, n≥3 |
| DRAW_SHRINKAGE_K | 8 | Bayesian shrinkage strength for draw offsets |
| TRAJ_BONUS_CAP | 0.20s | Max trajectory improvement bonus |
| TRAJ_DECLINE_CAP | 0.25s | Max trajectory decline penalty |
| LAST_WIN_BOOST | 2.0 | Weight multiplier for last-start winners |
| AWT_TURF_DISCOUNT | 0.50 | AWT runs half-weighted when projecting Turf |
| HV_ST_DISCOUNT | 0.50 | Cross-venue runs half-weighted |
| DIST_RECENCY_EXACT | 1.0 | Same distance: full weight |
| DIST_STEP_DOWN_NEAR/MED/FAR | 0.75/0.45/0.20 | **v4.0** Stepping to shorter distance |
| DIST_STEP_UP_NEAR/MED/FAR | 0.60/0.30/0.10 | **v4.0** Stepping to longer distance |
| FULL_DIST_MISMATCH_PEN | 0.12s | Added when 0 runs at today's exact distance |
| CLASS_T_MULT C4 | 1.30 | Boltzmann T widened 30% for C4 |
| CLASS_T_MULT C5 | 1.50 | Boltzmann T widened 50% for C5 |
| POSITION_CREDIT 1st | -0.15s | Winner credit (~0.6 lengths) |
| POSITION_CREDIT 2nd | -0.08s | Runner-up |
| POSITION_CREDIT 3rd | -0.04s | Third place |

---

## 4. Pace Prediction & Style Adjustment System

### 4.1 What Is Race Pace?

Race pace measures how fast or slow a race is run **before the final 400m**, relative to a track-, distance-, and class-specific standard:

```
Race Pace = (Actual sectional sum excl. final 400m) − (Standard sectional sum excl. final 400m)
```

- **Negative** = faster than standard
- **Positive** = slower than standard
- The final 400m is excluded to avoid finish-speed distortion

### 4.2 How the Model Predicts Pace Pre-Race

The model uses `predict_race_pace_v3()` to compute a **heuristic pace pressure index** from the field's running-style profiles:

1. **Per horse:** `h_pressure = (leader_frac × 2.0 + front_frac × 1.0) × certainty`
   - `certainty = max(0.3, 1.0 − entropy / 2.0)` — penalises inconsistent running styles
   - Early speed bonus: if early_speed_z < −0.5, add `min(0.5, |esz| × 0.3)`
2. **Average:** `pace_index = sum(h_pressure) / field_size`
3. **Distance factor:** ×0.8 for ≤1200m, ×1.0 for 1201–1650m, ×1.2 for ≥1651m
4. **Going factor:** GF ×1.10, GY ×0.90, Soft ×0.80
5. **Linear calibration (from 1,125 races):** `predicted_dev = 0.1173 + (−0.6253) × pace_index`
6. **Going residual adjustment:**

| Going | Adjustment |
|-------|-----------|
| Good | 0.000s |
| Good-to-Firm | −0.166s |
| Good-to-Yielding | +0.199s |
| Yielding | +0.300s |
| Soft/Heavy | +0.450s |

7. **Venue-specific offsets:**
   - **HV 1200m:** −0.35s (post-race R2/R5 evidence: HV Course C 1200m produces systematically faster early sections)
   - **ST all distances:** −0.45s (10/11 ST races ran faster than predicted on 8 Mar)

### 4.3 Pace Classification Thresholds

| Label | Deviation Range |
|-------|----------------|
| Very Fast | ≤ −1.00s |
| Fast | −1.00s to −0.40s |
| Slightly Fast | −0.40s to −0.20s |
| Normal | −0.20s to +0.20s |
| Slightly Slow | +0.20s to +0.35s |
| Slow | +0.35s to +0.50s |
| Very Slow | ≥ +0.50s |

### 4.4 Distance-Specific Pace Thresholds (Sectional-Based)

Used in historical `calculate_race_pace.py` analysis for post-race validation:

| Distance | Normal (±) | Sl. Fast/Slow | Fast/Slow | Very Fast/Slow |
|----------|-----------|---------------|-----------|----------------|
| 1000m | ±0.20s | 0.20–0.40s | 0.40–0.65s | >0.65s |
| 1200m | ±0.25s | 0.25–0.50s | 0.50–0.85s | >0.85s |
| 1400m | ±0.30s | 0.30–0.60s | 0.60–1.00s | >1.00s |
| 1600–1650m | ±0.32s | 0.32–0.65s | 0.65–1.10s | >1.10s |
| 1800m | ±0.40s | 0.40–0.80s | 0.80–1.20s | >1.20s |
| 2000m+ | ±0.47s | 0.47–0.95s | 0.95–1.50s | >1.50s |

Key observation: **tolerance widens with distance** — longer races have more natural sectional variance.

### 4.5 Pace-Style Multipliers (Empirical, from Historical Data)

The `PACE_STYLE_MULTIPLIERS` table gives the distance × pace × style interaction effect on Top-3 likelihood. Values > 1.0 = style benefits; < 1.0 = style penalised.

**Selected key patterns:**

| Dist | Fast Pace | Normal | Slow Pace |
|------|-----------|--------|-----------|
| **1000m** | Leader **0.66** / Midfield **1.77** | Leader 1.04 / Midfield 1.31 | Leader **1.44** / Midfield 0.35 |
| **1200m** | Leader 0.99 / Closer 0.78 | Leader 0.94 / Closer **1.57** | Leader **1.21** / Closer 0.57 |
| **1400m** | Leader **1.40** / Midfield **1.46** | Leader 1.16 / Closer 0.92 | Leader 1.08 / Closer 0.16 |
| **1650m** | Leader 0.60 / On-Pace **1.34** | all ~1.0 (balanced) | Leader **1.64** / Closer 0.53 |
| **2000m** | On-Pace 0.97 / Closer **1.89** | Midfield **1.49** / Closer **2.00** | On-Pace **1.32** / Midfield **1.75** |
| **2200m** | — | Leader 1.04 / Closer **1.35** | On-Pace **1.54** / Closer **1.27** |

**Multiplier → Time Conversion:**
```
time_adj = −(multiplier − 1.0) × expected_time × 0.010
```
Example: ET = 70s, mult = 1.20 → adj = −0.140s (faster); mult = 0.60 → adj = +0.280s (slower)

### 4.6 Marginal Style Impact by Distance (Pace-Independent)

Overall advantage of each style at each distance, regardless of pace:

| Distance | Leader | On-Pace | Midfield | Closer |
|----------|--------|---------|----------|--------|
| 1000m | **2.034** | 1.162 | 0.573 | 0.418 |
| 1200m | **1.708** | 1.204 | 0.765 | 0.428 |
| 1400m | 1.370 | **1.395** | 0.954 | 0.424 |
| 1600m | 1.455 | **1.439** | 0.829 | 0.448 |
| 1650m | 1.287 | **1.398** | 0.861 | 0.542 |
| 1800m | **1.951** | 0.627 | 1.184 | 0.451 |
| 2000m | **1.771** | 1.165 | 0.866 | 0.450 |
| 2200m | 1.142 | 1.004 | 0.690 | **1.380** |

Key: Leaders dominate sprints (1000–1200m); On-Pace dominates middle distances (1400–1650m); Leaders re-emerge at 1800–2000m; Closers only shine at 2200m.

### 4.7 Going Adjustments for Pace Calculation

| Going | Adj to Standard |
|-------|----------------|
| Firm | −0.15s (faster) |
| Good | 0s |
| Yielding | +0.15s (slower) |
| Soft | +0.30s |
| Sealed | +0.40s |

### 4.8 Pace Z-Score Parameters by Distance

| Distance | Mean | Std |
|----------|------|-----|
| 1000m | −0.041 | 0.266 |
| 1200m | −0.005 | 0.457 |
| 1400m | +0.017 | 0.477 |
| 1600m | −0.117 | 0.685 |
| 1650m | −0.233 | 0.749 |
| 1800m | −0.137 | 0.927 |
| 2000m | +0.103 | 0.794 |
| 2200m | −0.168 | 1.085 |

Shorter distances have tighter distributions — pace is more predictable.  
Longer distances (1800m+) have high variance — pace classification less decisive.

---

## 5. Venue & Course Characteristics

### 5.1 Sha Tin (ST) vs Happy Valley (HV)

| Attribute | Sha Tin | Happy Valley |
|-----------|---------|-------------|
| Circumference | ~1800m (wide oval) | ~1400m (tight triangle) |
| Home straight | ~430m | ~280m |
| Draw impact | Moderate (wide track dilutes) | Strong (tight turns amplify) |
| Congestion | Lower | Higher (narrow ~19–30m width) |
| Specialist penalty | — | HV↔ST specialists lose ~2.5–3 places switching |

### 5.2 Course Configurations (A → C+3)

| Configuration | Width | Draw Impact | Notes |
|---------------|-------|-------------|-------|
| A | ~30m (widest) | Lowest | Standard course |
| A+3 | ~27m | Low–moderate | Rail out 3m |
| B | ~24m | Moderate | Standard winter course at HV |
| B+2 | ~22m | Moderate–high | |
| C | ~21m | High | Sharp turns |
| C+3 | ~19m (tightest) | Highest | Maximum inside draw advantage |

Narrower course → more congestion → stronger inside draw bias → leaders from wide draws must work harder or get stuck wide, costing time.

---

## 6. Running Style Classification

- **Leader:** First-call position ≤ 2
- **On-Pace:** Position ≤ max(4, field × 0.3)
- **Closer:** Position ≥ max(8, field × 0.7)
- **Midfield:** Everything else
- **Dominant style (v3):** Based on OPTIMAL (best recency-weighted Top-3 rate), not modal (most frequent)

702/1,786 horses changed classification when switching from frequency-based to performance-based in v3.

---

## 7. Leader Congestion Discount

When ≥3 leaders in a race, the Leader pace multiplier is discounted toward 1.0:
- 3 leaders: 25% discount
- 4 leaders: 50% discount
- 5+ leaders: 75% discount

Rationale: PACE_STYLE_MULTIPLIERS calibrated from typical fields (1–2 leaders). When 3+ leaders contest, they burn each other out — the historical leader-benefits signal reverses.

---

## 8. HV B-Course Empirical Patterns (22 + 25 Mar 2026)

| Distance | Best Style | Weak Style | Draw Bias |
|----------|-----------|-----------|-----------|
| 1000m | Leader (52.5% T3) | Closer (11%) | Inside (1–4) dominant |
| 1200m | On-Pace (35.5%) | Closer (18%) | Wide (9+) penalised |
| 1650m | Midfield (32.1%) | Closer (20%) | Inside advantage |
| ALL | Improving (~50% T3) | Declining (~7%) | Universal inside bias |

---

## 9. Betting Utility Framework

### Scoring Rules

| Outcome | Points |
|---------|--------|
| Banker Top-4 | +3 |
| Banker Top-3 | +1 |
| Banker Win | +1 |
| Banker Miss (>4th) | −5 |
| Quinella (2 picks in top 2) | +6 |
| QP (2 picks in top 3) | +4 |
| 2+ picks in Top-4 | +3 |

### Cumulative Results (22 + 25 Mar)

| Strategy | 22 Mar | 25 Mar | Total |
|----------|--------|--------|-------|
| Human | +41 | +28 | +69 |
| Model | +48 | +67 | +115 |
| Combined | +55 | +77 | +132 |

---

## 10. Post-Race Rules (Derived from 25 Mar Analysis)

1. **Never banker risk ≥ 55** unless no credible alternative
2. **No Closer bankers at HV-B** unless draw ≤ 3
3. **Secondary elevation:** When human secondary has better model rank or pattern alignment, swap banker order
4. **Trust model pace prediction** — accurate 7+/9 races
5. **Continue combined approach** — consistently best strategy
6. **Auto-flag:** risk ≥ 55 + Closer style at HV = never banker

---

## 11. Version History

- **v3.2:** Bayesian shrinkage blend, empirical pace-style multipliers (r = 0.91), draw offset with hard caps
- **v3.3:** Recency decay λ = 0.85, uncertainty penalties, pace gating, consistency flagging
- **v3.4:** Trajectory detection (last-2 vs prior-3 + OLS), AWT/venue discounts, strong debut handling
- **v3.4.1:** Surface/distance-aware uncertainty, decline cap raised to 0.25s
- **v3.4.2:** Shrinkage floor for n=1, trajectory bonus gating, distance proximity tightened to 100m, last-start winner boost
- **v3.4.3:** Race-pace normalization, HV 1200m offset (−0.35s)
- **v3.4.4:** ST pace offset (−0.45s), class-tier Boltzmann scaling, draw cap overhaul, distance-aware recency, finishing-position credit
- **v3.4.5:** Bayesian draw shrinkage (k = 8), global safety cap ±1.50s
- **v3.4.6:** Leader congestion discount (≥3 leaders), outlier trimming, surface penalty flexibility
- **v3.4.7:** Class-relative ability, trajectory reliability gate, draw field-size scaling, ability-anchored projection
- **v3.4.8:** Draw offsets at runtime via race-median method (FT − race_median), replacing FT − expected_time

---

## 12. Script Naming & Adaptation

| Pattern | Purpose |
|---------|---------|
| `race_day_analysis_YYYYMMDD_v3.4.8.py` | Main model pipeline |
| `generate_combined_pdf_DDmon.py` | Combined human+model PDF |
| `post_race_analysis_YYYYMMDD.py` | Post-race comparison |
| `betting_utility_YYYYMMDD.py` | Betting evaluation |
| `scrape_hkjc.py` | Results scraper |

### Adapting for a New Meeting
1. Copy latest `race_day_analysis_*_v3.4.8.py`
2. Update: `RACE_CARD`, `SHEET_NAME`, `OUT_PDF`, `OUT_TEXT`, `MEETING_TITLE`, `MEETING_VENUE`
3. Update: `TURF_GOING_ASSUMED`, `AWT_GOING_ASSUMED`
4. Update: `SCRATCHINGS`, `STANDBY_PROMOTIONS`
5. Run with `C:\Users\tbhbr\miniconda3\python.exe`

---

## 13. Reference Documentation (in `General performance analysis/`)

| File | Contents |
|------|----------|
| `Pace intepretation.md` | Pace definition, formula, classification |
| `Composite Scoring Guideline.md` | Pace compatibility matrix, positional tactical scoring |
| `FACTOR_DETAILS_EXPLAINED.md` | Pace & running style factor explanations |
| `calculate_race_pace.py` | Distance-specific threshold definitions |
| `HK racing intepretation.md` | Course configurations, ST vs HV characteristics |
| `COURSE_SCORE_FIX.md` | Track specialist analysis |

---

## 14. v4.0 Structural Changes (Implemented April 2026)

Four foundational changes to the residual computation pipeline. All four modify how every horse's projection is computed.

### 14.1 Change 1: Kill the Ability File

**Before:** `horse_ability_analysis_v3.xlsx` pre-computed ability scores, running style, shrinkage factors. Required re-running a separate script before each meeting.

**After:** `compute_horse_profile_runtime()` computes ALL profile fields directly from DB at analysis time:
- `dominant_style` (OPTIMAL method — best top-3 rate by style)
- `leader_frac`, `front_frac`, `early_speed_z`, `style_entropy`
- `residual_std`, `best_residual`, `worst_residual`
- `n_runs`, `n_sectional_runs`, `shrink_factor`, `confidence`

**Impact:** Eliminates stale-data risk. Profile always reflects latest DB state. `PROFILE_SHRINKAGE_K = 5`.

### 14.2 Change 2: Contextualise Individual Run Performances

**Before:** Each historical run's residual = `(FT - ET) - race_pace_index + position_credit`. No awareness of draw or trip.

**After:** Two additional adjustments per run:
1. **Draw context:** Look up the draw offset for that run's conditions (distance, course, draw). Subtract it → removes draw advantage/disadvantage from the residual.
2. **Wide-running cost:** If horse was drawn wide (outer `WIDE_COST_OUTER_FRAC=0.65` of field) AND settled forward, estimate energy cost of crossing/running wide. Subtract from residual.

**Formula:** `adj_resid = (FT - ET) - RPI + position_credit - draw_offset - wide_cost`

**Effect:** Horses with consistently bad trips get uplift. Horses that benefited from inside draws see their "true" ability adjusted down. The `context_adj` diagnostic field shows the total adjustment per horse.

| Parameter | Value | Purpose |
|-----------|-------|---------|
| WIDE_COST_OUTER_FRAC | 0.65 | Draw ≥ 65th percentile = "wide" |
| WIDE_COST_LEADER_PEN | 0.08s | Cost of leading from wide draw |
| WIDE_COST_ONPACE_PEN | 0.05s | Cost of on-pace from wide draw |
| WIDE_COST_MIDFIELD_PEN | 0.03s | Cost of midfield from wide draw |
| Course width scaling | A=0.6, C+3=1.30 | Narrow courses amplify wide cost |

### 14.3 Change 3: Asymmetric Distance Penalties

**Before:** Symmetric `DIST_RECENCY_*` — a 200m step-up and 200m step-down had the same weight.

**After:** Direction matters:
- **Stepping DOWN** (from longer to shorter): Form transfers better — horse has stamina surplus.
- **Stepping UP** (from shorter to longer): Form transfers worse — untested endurance.

| Distance Diff | Step Down | Step Up |
|---------------|-----------|---------|
| ≤100m | 0.75 | 0.60 |
| ≤200m | 0.45 | 0.30 |
| >200m | 0.20 | 0.10 |

**Example:** Horse ran 1600m, today's race is 1400m (step down 200m) → weight 0.45. Same horse racing 1800m (step up 200m) → weight 0.30.

### 14.4 Change 4: Race Quality Weighting (Form Franking)

**Before:** All historical races from DB weighted equally (after recency/distance/surface discounts).

**After:** Each race gets a quality score based on how its runners performed subsequently:
1. For each race, check all runners' NEXT start
2. "Validated" = next-start residual improved OR horse placed (top 3)
3. Quality ratio = validated / total_with_subsequent_data
4. Mapped to weight range [`RACE_QUALITY_WEIGHT_MIN`, `RACE_QUALITY_WEIGHT_MAX`] = [0.70, 1.30]
5. Applied as a multiplier on each run's weight in recency computation

**Effect:** Winning from a strong race (where beaten horses later performed well) counts more. Winning from a weak race (beaten horses all flopped) counts less.

| Parameter | Value | Purpose |
|-----------|-------|---------|
| RACE_QUALITY_MIN_SUBSEQUENT | 3 | Minimum runners with next-start data |
| RACE_QUALITY_WEIGHT_MIN | 0.70 | Floor for weakest races |
| RACE_QUALITY_WEIGHT_MAX | 1.30 | Ceiling for strongest races |
| RACE_QUALITY_LOOKBACK_DAYS | 120 | Cache scope |

The `quality_effect` diagnostic field shows the mean race quality weight across a horse's runs.
