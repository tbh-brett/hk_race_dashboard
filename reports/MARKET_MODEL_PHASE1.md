# Market Model — Phase 1 Validation

- Dates: **20260101 … 20260422** (33 meetings)
- Races: **331**, runners with usable SP: **4103**
- WIN book overround (mean): **1.2140** [range 0.043 – 1.263]
- Shin insider-share z (mean): **0.0232** (n=331 races)

## 1. Calibration of implied P(win)

| Bin | n | p̄ model | p range | actual win% | (shin) |
|---:|---:|---:|:--|---:|---:|
| 1 | 410 |   0.6% | 0.3–0.9% |   0.0% |   0.0% |
| 2 | 410 |   1.3% | 0.9–1.6% |   0.2% |   0.2% |
| 3 | 410 |   2.1% | 1.6–2.6% |   2.4% |   2.4% |
| 4 | 410 |   3.2% | 2.6–3.7% |   3.9% |   3.2% |
| 5 | 410 |   4.4% | 3.7–5.1% |   2.9% |   3.4% |
| 6 | 410 |   6.0% | 5.1–6.8% |   6.1% |   6.8% |
| 7 | 410 |   8.0% | 6.8–9.2% |   7.1% |   6.8% |
| 8 | 410 |  11.0% | 9.2–13.0% |  12.7% |  12.2% |
| 9 | 410 |  15.6% | 13.0–19.3% |  16.8% |  16.6% |
| 10 | 413 |  28.4% | 19.3–68.5% |  26.9% |  27.4% |

- Brier(basic 1/o) = **0.0660**, log-loss = **0.2341**
- Brier(Shin)      = **0.0661**, log-loss = **0.2339**
- ΔBrier (Shin improves): **-0.00012** (positive = Shin is better)

## 2. Favourite–Longshot Bias (flat $1 WIN on every SP-odds horse)

| Odds band | n | strike | expected | ROI |
|:--|---:|---:|---:|---:|
| 0-2.5 | 91 | 39.6% | 41.7% | -21.65% |
| 2.5-4 | 258 | 24.4% | 25.4% | -21.24% |
| 4-7 | 575 | 16.3% | 15.4% | -13.98% |
| 7-12 | 671 |  9.4% |  9.2% | -18.70% |
| 12-20 | 708 |  5.5% |  5.6% | -19.92% |
| 20-50 | 942 |  3.1% |  2.9% |  -9.77% |
| 50+ | 858 |  0.1% |  1.2% | -92.07% |

## 3. Model-vs-Market Edge Backtest (WIN, flat $1)

- Model: **v4.4** — meetings with model probs: **7**
- Baseline model-top-1: n=68, strike=19.1%, ROI= +8.38%

| Edge threshold (p_model − p_shin) | bets | strike | ROI |
|:--|---:|---:|---:|
| ≥ +0.00 | 474 |  5.7% | -36.75% |
| ≥ +0.02 | 318 |  6.6% | -32.01% |
| ≥ +0.05 | 165 |  7.9% | -34.73% |
| ≥ +0.10 | 63 | 11.1% | -27.46% |
| ≥ +0.15 | 29 | 10.3% | -44.14% |

## Interpretation guide

- **Calibration**: bins should have `actual win% ≈ p̄ model`. If the lowest-prob bin's actual is higher than predicted, the market under-rates longshots (reverse favourite-longshot). HKJC has historically shown the *normal* bias (favourites win more than their implied prob suggests) — look for that in the odds-band table.
- **Shin vs basic**: Brier should drop slightly under Shin; if it rises, the overround is already symmetric on this data and the z-correction is noise.
- **Edge ROI**: compare the `≥ 0.00` row to the baseline. A bet is only an *edge bet* — not just a model pick — if ROI rises with θ and stays positive at θ ≥ 0.05. If ROI is flat or falls, the model's over-picks are offsetting its under-picks.
