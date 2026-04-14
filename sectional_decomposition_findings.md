# Sectional Decomposition Analysis — Findings & Methodology

**Date:** 11 April 2026  
**Data:** 17,649 horse-runs from HKJC historical database  
**Status:** Standalone analysis — NOT integrated into model  

---

## 1. What It Does

Breaks every horse-run's finish time into three normalised **zones**:

| Zone | What it measures | Per-400m normalised |
|------|-----------------|---------------------|
| **Early** | First section of the race (first ~200-400m) | Yes — short sections (200m) scaled to per-400m for comparability |
| **Mid** | Middle sections (everything between early and late) | Yes — averaged across mid sections |
| **Late** | Final 400m (always the last section) | Raw = per-400m (always 400m) |

Each zone is then expressed as a **deviation from the race-median** for that zone:

```
early_dev = horse_early_per400m − race_median_early_per400m
mid_dev   = horse_mid_per400m   − race_median_mid_per400m
late_dev  = horse_late_per400m  − race_median_late_per400m
```

**Negative dev = faster than the field median in that zone.**

### Key Metrics

| Metric | Formula | Interpretation |
|--------|---------|----------------|
| **SSI** (Sectional Shape Index) | `late_dev − early_dev` | Negative = strong finisher (improves relative to field from start to end). Positive = front-loaded (fades). |
| **DI** (Deceleration Index) | `(late − early) / early` | How much the horse slows from start to finish in absolute terms. Lower = better stamina. |
| **late_std** | Std dev of late_dev across recent runs | Consistency of finishing effort. Low = reliable closer. High = inconsistent. |

### Why Race-Median Normalisation Matters

Raw sectional times are contaminated by:
- **Race pace** (fast-run races = fast early sectionals for everyone)
- **Class** (C2 horses run faster sectionals than C5)
- **Going** (Good-to-Firm produces faster times than Yielding)
- **Distance** (1200m per-400m pace ≠ 2000m per-400m pace)

Subtracting the race median strips all of these out, isolating **how this horse distributed effort relative to its immediate competitors in that specific race**.

---

## 2. HKJC Sectional Structure

| Distance | # Sections | S1 | S2 | S3 | S4 | S5 | S6 |
|----------|-----------|-----|-----|-----|-----|-----|-----|
| 1000m | 3 | 200m | 400m | 400m | — | — | — |
| 1200m | 3 | 400m | 400m | 400m | — | — | — |
| 1400m | 4 | 200m | 400m | 400m | 400m | — | — |
| 1600m | 4 | 400m | 400m | 400m | 400m | — | — |
| 1650m (AWT) | 4 | 450m | 400m | 400m | 400m | — | — |
| 1800m | 5 | 200m | 400m | 400m | 400m | 400m | — |
| 2000m | 5 | 400m | 400m | 400m | 400m | 400m | — |
| 2200m | 6 | 200m | 400m | 400m | 400m | 400m | 400m |
| 2400m | 6 | 400m | 400m | 400m | 400m | 400m | 400m |

Sections sum exactly to finish time (verified: diff = 0.00s across all 17,649 runs).

---

## 3. Core Findings

### Finding 1: Late sectional is THE dominant predictor of finishing position

| Distance | early_dev vs place (ρ) | **late_dev vs place (ρ)** | SSI vs place (ρ) |
|----------|----------------------|--------------------------|-------------------|
| 1200m | +0.207 | **+0.610** | +0.271 |
| 1400m | +0.156 | **+0.692** | +0.299 |
| 1600m | +0.020 (ns) | **+0.729** | +0.462 |
| 1800m | +0.059 | **+0.665** | +0.330 |
| 2000m | +0.120 | **+0.716** | +0.398 |

All correlations p < 0.0001 (except 1600m early_dev which is non-significant).

**Interpretation:** The horse that finishes the final 400m faster than the field almost always finishes closer to first. This is obvious in hindsight — finishing fast = finishing well — but the question is whether a horse's **historical** late_dev predicts **future** outcomes (see Finding 4).

### Finding 2: As distance increases, SSI becomes more important

| Distance bucket | Winner SSI | Field SSI | Diff |
|----------------|-----------|-----------|------|
| Sprint (1000-1200m) | −0.146 | +0.077 | **−0.224** |
| Mile (1400-1600m) | −0.278 | +0.105 | **−0.383** |
| Middle (1800-2000m) | −0.398 | +0.092 | **−0.490** |

Winners at 1800-2000m have an SSI gap of −0.490 vs the field — nearly double the sprint gap (−0.224). This means:

- **At sprints:** You can win by leading and sustaining (small SSI gap — early speed matters more)
- **At middle distances:** You almost certainly need to finish stronger than where you started relative to the field (large negative SSI required)

### Finding 3: Weight does NOT make the final sectional worse (surprise)

| Distance | Corr(weight, late_dev) | p-value | Light late_dev | Heavy late_dev | Diff |
|----------|----------------------|---------|---------------|----------------|------|
| 1200m | −0.031 | 0.010 | +0.121 | +0.076 | **−0.045** |
| 1400m | −0.098 | 0.000 | +0.215 | +0.048 | **−0.167** |
| 1600m | −0.052 | 0.056 | +0.207 | +0.099 | **−0.108** |
| 1800m | −0.055 | 0.060 | +0.211 | +0.136 | **−0.075** |

The correlations are **negative** — heavier horses finish with **better** (lower) late_dev than lighter horses. This is a **selection effect**, not a weight benefit: horses carrying top weight are typically the best horses in the race. Their superior ability more than compensates for the weight.

**Key insight:** Weight impact should NOT be measured by comparing heavy vs light horses. The correct approach is to compare the **same horse** at different weights. This requires horse-level panel analysis, not cross-sectional correlation.

### Finding 4: Historical sectional profiles DO predict future outcomes (but modestly)

Prior SSI (from past runs) vs actual finishing place in the current race:

| Distance | prior_SSI vs place (ρ) | prior_late vs place (ρ) |
|----------|----------------------|------------------------|
| 1200m | +0.142 (p<0.0001) | +0.265 (p<0.0001) |
| 1400m | +0.107 (p<0.0001) | +0.308 (p<0.0001) |
| 1600m | +0.216 (p<0.0001) | +0.357 (p<0.0001) |
| 1800m | +0.152 (p<0.0001) | +0.255 (p<0.0001) |
| 2000m | +0.122 (p=0.022) | +0.247 (p<0.0001) |

**This is the predictive signal.** A horse's historical `prior_late` (how it has finished in the past relative to its competition) has ρ = 0.25–0.36 with actual place. That's a meaningful signal — weaker than the model's overall projection (ρ ≈ 0.45) but additive information.

**The strongest predictive signal is at 1600m** (ρ = 0.357 for prior_late, 0.216 for prior_SSI). This makes sense — 1600m is where pure speed runs out and finishing ability separates horses.

### Finding 5: Sectional shape affects which distance a horse should race

Horses racing 1600m+, grouped by their sectional profile from prior runs:

| Profile | Mean place | Win% | Top-3% | n |
|---------|-----------|------|--------|---|
| Strong Finisher (SSI < −0.2) | 6.17 | 9.2% | 27.4% | 2,332 |
| Even-Paced | 6.59 | 8.9% | 24.8% | 1,109 |
| Front-Loaded (SSI > +0.2) | 7.43 | 6.6% | 21.4% | 1,752 |

Horses racing ≤1200m:

| Profile | Mean place | Win% | Top-3% | n |
|---------|-----------|------|--------|---|
| Strong Finisher | 5.96 | 9.4% | 29.0% | 1,883 |
| Even-Paced | 6.24 | 8.5% | 26.3% | 1,564 |
| Front-Loaded | 6.76 | 8.4% | 24.6% | 2,973 |

**Strong Finishers outperform at ALL distances**, but the gap is larger at 1600m+ (6.0 percentage points in Top-3% vs 4.4 points at ≤1200m). Front-Loaded horses are most penalised at distance.

---

## 4. Real Race Examples

### Example A: Strong finisher wins from behind (1 March 2026)

**R6, 1200m B+2 (Sha Tin)**
- **WINNER: GALACTIC VOYAGE** — early_dev=+0.480, late_dev=−0.830, SSI=−1.310
  - Started 0.48s/400m slower than the field median off the gate
  - Finished 0.83s/400m faster than the field in the final section
  - Classic strong-finisher profile: conserves energy, finishes over the top
- **FADED: WONDERSTAR** — early_dev=−0.560, late_dev=+0.610, SSI=+1.170
  - Started 0.56s/400m faster than the field but died 0.61s/400m slower at the finish
  - Classic front-loaded profile: all speed, no sustain

### Example B: Efficient leader (1 March 2026)

**R1, 1800m B+2 (Sha Tin)**
- **WINNER: STORM RUNNER** — early_dev=−0.360, late_dev=−0.035, SSI=+0.325, DI=−0.173
  - Led early (0.36s faster than field) AND still ran near field-median late
  - Very low deceleration — maintained effort throughout
  - This is the ideal leader profile: just good enough to lead AND sustain

### Example C: Strong finisher in a stayer's race (4 March 2026)

**R3, 2200m C (Happy Valley)**
- **WINNER: ACE WAR** — early_dev=+0.840, late_dev=−1.380, SSI=−2.220
  - Settled almost a full second per 400m slower than the field early
  - Then ran 1.38s per 400m faster than the field in the final section
  - Extreme SSI of −2.22 — one of the most back-loaded wins in the dataset

---

## 5. How to Apply This (April 12 Example)

### R8 — 1600m C3: Where sectional shape is decisive
This is a 1600m race — the distance where SSI predictive power is strongest (ρ = 0.357).

| Horse | late_dev | SSI | Type | Interpretation |
|-------|---------|-----|------|----------------|
| AMAZING PARTNERS | −0.417 | −0.526 | Strong Finisher | Consistently finishes faster than field (σ=0.087 — very reliable) |
| ENDUED | −0.392 | −0.396 | Strong Finisher | Similar profile, slightly less extreme |
| FLYING LUCK | −0.349 | −0.733 | Strong Finisher | Most negative SSI — biggest gap between early and late |
| MISTER DAPPER | +0.159 | +0.674 | Front-Loaded | Fast early, fades — risk at 1600m |
| WITHALLMYFAITH | +0.339 | +0.838 | Front-Loaded | Same pattern, even more pronounced |

At 1600m C3, the data says:
- Horses with SSI < −0.3 hit top-3 at 27.4% vs 21.4% for those > +0.2
- **AMAZING PARTNERS** has the best late_dev AND the lowest late_std (0.087) — the most reliable strong finisher in this race
- **MISTER DAPPER** and **WITHALLMYFAITH** have the pace to lead but their profiles show consistent fading — at 1600m this pattern produces worse results

### R11 — 1200m C2: Sprint where it matters less
| Horse | late_dev | SSI | Type |
|-------|---------|-----|------|
| GALACTIC VOYAGE | −0.534 | −1.013 | Strong Finisher |
| COLOURFUL KING | −0.501 | −0.871 | Strong Finisher |
| RISING FORCE | −0.067 | +0.488 | Front-Loaded |
| PAKISTAN LEGACY | +0.161 | +0.593 | Front-Loaded |

At 1200m the SSI gap between winners and field is smaller (−0.224 vs −0.490 at 1800m). RISING FORCE and PAKISTAN LEGACY can still win from the front. But GALACTIC VOYAGE's finishing power (SSI = −1.013) makes it dangerous even in a sprint.

---

## 6. Sectional Type Distribution (all 1,659 profiled horses)

| Type | Count | Definition |
|------|-------|-----------|
| Front-Loaded | 536 (32%) | SSI > +0.15, early_dev < −0.05 |
| Strong Finisher | 444 (27%) | SSI < −0.15, late_dev < −0.05 |
| Weak Finisher | 329 (20%) | late_dev > +0.15 |
| Even-Paced | 175 (11%) | |SSI| ≤ 0.15, |early_dev| < 0.15 |
| Mixed | 144 (9%) | Doesn't fit other categories |
| Speed Merchant | 31 (2%) | early_dev < −0.15 with neutral/positive late |

---

## 7. How to Utilise This in the Model (Recommendations)

### What to integrate

1. **Horse-level `avg_late_dev` as an additive signal** — This has ρ = 0.25–0.36 with actual place across distances. It's independent of the residual-based projection because it measures *how* the horse runs, not just *how fast*. Could be added as a small adjustment to projected_time.

2. **Distance suitability scoring** — A horse's SSI should inform distance confidence. A Front-Loaded horse stepping up from 1200m to 1600m deserves a distance penalty beyond what the ET system gives. The data shows they place 1.3 positions worse at 1600m+ vs Strong Finishers.

3. **Pace interaction** — The current `predict_race_pace_v3` predicts whether the race will be fast/slow. Combined with SSI:
   - Fast pace + Strong Finisher → double benefit (leaders tire, closer has the engine)
   - Slow pace + Front-Loaded on rail → the one exception where front-loaded wins at distance

4. **late_std as a reliability factor** — AMAZING PARTNERS has late_std=0.087 (rock-steady finisher). STAR SATYR has late_std=0.917 (wildly inconsistent). The former is a safer projection; the latter is higher variance.

### What NOT to integrate

- **The raw same-race SSI/late_dev** — These are outcome variables, not predictors. You can't know late_dev before the race is run. Only the HISTORICAL profile (from prior runs) is predictive.
- **Weight × late sectional adjustment** — The cross-sectional data shows a selection effect, not a causal effect. Need within-horse analysis before making weight-based sectional adjustments.

### Course-specific application

The analysis showed that sectional shape patterns are consistent across ST and HV, but the **magnitude** differs:
- **HV (tight track):** Front-loaded horses are MORE penalised because the tighter turns amplify energy cost of leading. Strong finishers benefit more from the shorter straight (less ground to make up but easier to thread through traffic on a tight track — if clear run is available).
- **ST (wide track):** More room for front-runners to sustain. Even-paced profiles are relatively stronger here.

---

## 8. Files Created

| File | Purpose |
|------|---------|
| `sectional_decomposition.py` | Full analysis script — generates all findings |
| `sectional_decomposition_findings.md` | This document |

Both are standalone. No model files were modified.
