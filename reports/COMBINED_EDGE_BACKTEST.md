# Combined-Edge Backtest — Phase 2

Window: **20260401 … 20260429** (8 meetings, 907 model-scored runners, 77 races).

`edge = p_model − p_market`, where `p_market = implied_basic(final SP)`.

## Strategy comparison (flat $1 unless noted)

| Strategy | n bets | wins | strike | ROI | Total return |
|:--|---:|---:|---:|---:|---:|
| TOP1 | 77 | 15 |  19.5% |  +2.47% | +1.90 |
| TOP1_PE | 58 | 13 |  22.4% | +23.28% | +13.50 |
| TOP1_PE5 | 49 | 10 |  20.4% | +21.84% | +10.70 |
| TOP2_PE | 111 | 18 |  16.2% | -11.44% | -12.70 |
| TOP3_PE | 160 | 21 |  13.1% | -20.81% | -33.30 |
| OVERLAY_4P | 112 | 10 |   8.9% | -35.45% | -39.70 |
| KELLY_TOP3 | 160 | 21 |  13.1% | +11.29% | +0.20 |
| TOP1_NE | 19 | 2 |  10.5% | -61.05% | -11.60 |

## Per-meeting cumulative ROI

| Date | TOP1 | TOP1_PE | TOP1_PE5 | TOP2_PE | TOP3_PE |
|:--|---:|---:|---:|---:|---:|
| 20260401 | -75.56% | -100.00% | -100.00% | -36.36% | -58.82% |
| 20260406 | -64.00% | -61.54% | -58.33% | -35.19% | -58.33% |
| 20260408 | -45.17% | -31.50% | -50.59% | -34.50% | -47.26% |
| 20260412 | -23.85% |  -1.79% |  -3.48% | -15.93% | -19.88% |
| 20260415 | +22.92% | +53.51% | +59.00% | +17.06% |  +6.93% |
| 20260419 | +16.95% | +36.89% | +41.89% |  +0.48% |  -7.54% |
| 20260422 |  +8.38% | +30.00% | +33.02% |  -8.14% | -16.07% |
| 20260429 |  +2.47% | +23.28% | +21.84% | -11.44% | -20.81% |

## Reading guide

- `TOP1_PE` = rank-1 model pick **only when** market also rates it above the model's prob. If ROI > `TOP1`, the market filter removes losing favourites.
- `TOP1_NE` is the diagnostic counterpart — if it loses heavily, the rank-1 picks the market disliked were rightly disliked.
- `OVERLAY_4P` targets value at mid-market (odds ≥ 4 with edge ≥ 5pp). High variance, but the only strategy that can deliver positive ROI from longshot strike rates.
- `KELLY_TOP3` sizes by edge; ROI is per-dollar staked (so directly comparable to flat).
