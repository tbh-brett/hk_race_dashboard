# Calibration & Value Backtest Harness — v4.4

Range: **20260401 → 20260429**  ·  meetings=8  ·  races=77  ·  rows=907

## 1. Probability calibration

Lower is better for Brier and log-loss. Skill scores are vs Shin market baseline (positive = beats the market).

| Source | Brier | LogLoss | ECE | BrierSkill | LogLossLift |
|--------|------:|--------:|----:|-----------:|------------:|
| p_model | 0.0722 | 0.2728 | 0.0370 | -3.11% | -0.0251 |
| p_market_basic | 0.0694 | 0.2463 | 0.0287 | +0.83% | +0.0014 |
| p_market_shin | 0.0700 | 0.2478 | 0.0232 | +0.00% | +0.0000 |

### Reliability bins — p_model

| Bin | n | p_mean | win_rate | gap |
|----:|--:|-------:|---------:|----:|
| 1 | 90 | 0.007 | 0.011 | +0.004 |
| 2 | 90 | 0.016 | 0.078 | +0.061 |
| 3 | 90 | 0.028 | 0.011 | -0.017 |
| 4 | 90 | 0.040 | 0.089 | +0.049 |
| 5 | 90 | 0.053 | 0.100 | +0.047 |
| 6 | 90 | 0.066 | 0.067 | +0.001 |
| 7 | 90 | 0.086 | 0.067 | -0.019 |
| 8 | 90 | 0.112 | 0.089 | -0.023 |
| 9 | 90 | 0.148 | 0.078 | -0.070 |
| 10 | 97 | 0.260 | 0.186 | -0.075 |

### Reliability bins — p_market_shin

| Bin | n | p_mean | win_rate | gap |
|----:|--:|-------:|---------:|----:|
| 1 | 90 | 0.001 | 0.000 | -0.001 |
| 2 | 90 | 0.006 | 0.011 | +0.005 |
| 3 | 90 | 0.013 | 0.011 | -0.002 |
| 4 | 90 | 0.026 | 0.044 | +0.019 |
| 5 | 90 | 0.043 | 0.033 | -0.009 |
| 6 | 90 | 0.061 | 0.100 | +0.039 |
| 7 | 90 | 0.082 | 0.078 | -0.005 |
| 8 | 90 | 0.115 | 0.133 | +0.018 |
| 9 | 90 | 0.165 | 0.178 | +0.012 |
| 10 | 97 | 0.299 | 0.186 | -0.114 |

## 2. ROI by edge quintile (edge = p_model - p_market_shin)

If edge has signal, ROI should rise monotonically across quintiles.

| Q | n | edge_range | strike | flat_roi | kelly_roi |
|--:|--:|------------|-------:|---------:|----------:|
| 1 | 181 | -0.360 → -0.050 | 12.2% | -44.48% | +0.00% |
| 2 | 181 | -0.050 → -0.001 | 9.9% | +0.55% | +0.00% |
| 3 | 181 | -0.001 → +0.019 | 4.4% | -3.70% | +25.93% |
| 4 | 181 | +0.020 → +0.048 | 5.0% | -37.85% | -18.94% |
| 5 | 183 | +0.049 → +0.602 | 7.7% | -39.78% | +8.02% |

## 3. Rank × edge-sign ROI grid (flat $1 WIN)

| rank | edge | n | strike | roi |
|------|------|--:|-------:|----:|
| 1 | edge>=0 | 58 | 22.4% | +23.28% |
| 1 | edge<0 | 19 | 10.5% | -61.05% |
| 2 | edge>=0 | 50 | 8.0% | -56.20% |
| 2 | edge<0 | 27 | 18.5% | -19.26% |
| 3 | edge>=0 | 49 | 6.1% | -42.04% |
| 3 | edge<0 | 28 | 10.7% | -56.43% |
| 4+ | edge>=0 | 379 | 2.9% | -27.39% |
| 4+ | edge<0 | 297 | 10.1% | -18.82% |

## 4. Per-rank win rate (sanity check)

If model rank is meaningful, strike rate should decline as rank rises. Compare to p_model_mean and p_market_shin_mean to see whether the model is **more** or **less** confident than the market for each rank.

| rank | n | strike | p_model | p_market_shin |
|-----:|--:|-------:|--------:|--------------:|
| 1 | 77 | 19.5% | 0.264 | 0.175 |
| 2 | 77 | 11.7% | 0.166 | 0.136 |
| 3 | 77 | 7.8% | 0.124 | 0.120 |
| 4 | 77 | 5.2% | 0.100 | 0.109 |
| 5 | 77 | 11.7% | 0.079 | 0.096 |
| 6 | 77 | 3.9% | 0.064 | 0.071 |
| 7 | 76 | 5.3% | 0.053 | 0.063 |
| 8 | 75 | 8.0% | 0.043 | 0.071 |

## 5. Interpretation cheat-sheet

- **BrierSkill > 0**: model's probabilities beat the market.
  In practice you should expect this to be NEGATIVE on a small
  sample — the HK win pool is one of the hardest markets to beat.
- **Edge-quintile monotonicity**: if Q5 ROI ≥ Q4 ≥ Q3 ≥ Q2 ≥ Q1,
  edge is a real ranking signal even if the model is mis-scaled.
- **Rank×edge grid**: cells with edge>=0 should outperform cells
  with edge<0 at every rank tier. If they don't, the edge
  signal isn't real — it's just leaking the rank ordering.