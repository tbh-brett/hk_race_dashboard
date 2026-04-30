# Gradient-Boosted Model — Training Report

Range **20260412 → 20260429** · meetings=5 · races=48 · rows=570

## Out-of-fold metrics (vs v4.4 handcrafted)

| Source | Brier | LogLoss | BrierSkill (vs v4.4) | LogLoss lift |
|--------|------:|--------:|---------------------:|-------------:|
| p_v44  | 0.0705 | 0.2673 | (baseline) | (baseline) |
| p_gbm  | 0.0699 | 0.2607 | +0.85% | +0.0066 |

## Reliability — p_gbm (OOF)

| bin | n | p_mean | win_rate | gap |
|----:|--:|-------:|---------:|----:|
| 1 | 57 | 0.027 | 0.035 | +0.008 |
| 2 | 57 | 0.048 | 0.035 | -0.013 |
| 3 | 57 | 0.062 | 0.053 | -0.009 |
| 4 | 57 | 0.069 | 0.000 | -0.069 |
| 5 | 57 | 0.074 | 0.070 | -0.004 |
| 6 | 57 | 0.079 | 0.105 | +0.026 |
| 7 | 57 | 0.085 | 0.000 | -0.085 |
| 8 | 57 | 0.096 | 0.140 | +0.045 |
| 9 | 57 | 0.113 | 0.193 | +0.080 |
| 10 | 57 | 0.189 | 0.140 | -0.049 |

## Top-20 feature importance (gain)

| feature | gain | split |
|---------|-----:|------:|
| esz_z | 361 | 428 |
| proj_final_sec | 265 | 378 |
| sec_total_adj | 240 | 279 |
| proj_time_z | 227 | 199 |
| win_prob | 196 | 290 |
| late_std | 187 | 404 |
| draw | 176 | 235 |
| winprob_z | 169 | 296 |
| weight | 160 | 361 |
| avg_ssi | 153 | 287 |
| avg_late_dev | 149 | 243 |
| smap_pos_adj | 122 | 162 |
| effective_resid | 122 | 378 |
| early_speed_z | 118 | 168 |
| rank_proj_time | 103 | 112 |
| smap_total_adj | 98 | 203 |
| projected_time | 97 | 229 |
| pace_score | 76 | 188 |
| rank_esz | 61 | 121 |
| proj_pre_pace | 55 | 115 |

## Interpretation

- **BrierSkill > 0** = the GBM beats the handcrafted v4.4 model.
- The reliability table shows whether the GBM's calibrated buckets
  match observed win rates. Diagonal-ish columns = well calibrated.
- Feature importance reveals which inputs the GBM thinks carry the
  most signal. Compare to the v4.4 weights — if `winprob_z` is on
  top, the GBM is mostly trusting the v4.4 score; if other features
  dominate, the GBM has found a re-weighting that helps.