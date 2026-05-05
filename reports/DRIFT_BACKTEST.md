# Drift Backtest — Phase 3

Races analysed: **4**  ·  horse-rows: **48**

> ⚠️  **INSUFFICIENT DATA** — fewer than 20 races with ≥2 snapshots. Numbers below are descriptive only; do not size bets on this.

## Buckets
| Bucket | n horses | wins | strike | ROI@SP | avg Δ% |
|:--|---:|---:|---:|---:|---:|
| STEAMER | 1 | 0 |   0.0% | -100.00% | -28.00% |
| STABLE | 46 | 4 |   8.7% | +118.70% |  +0.12% |
| DRIFTER | 1 | 0 |   0.0% | -100.00% | +25.00% |

## Notable rows (top 10 by |Δ%|)
| Date | R | # | Horse | morn → late | Δ% | SP | Place |
|:--|---:|---:|:--|:--|---:|---:|---:|
| 20260422 | 1 | 7 | TURF PHOENIX | 25.0 → 18.0 |  -28.0% | 28.0 | 3 |
| 20260422 | 1 | 10 | TAIHANG SCENERY | 12.0 → 15.0 |  +25.0% | 15.0 | 4 |
| 20260422 | 1 | 4 | NEBRASKAN | 5.1 → 3.9 |  -23.5% | 4.7 | 1 |
| 20260422 | 1 | 11 | RICH HORSE | 42.0 → 33.0 |  -21.4% | 56.0 | 7 |
| 20260422 | 1 | 1 | HAPPY ALLIANCE | 50.0 → 40.0 |  -20.0% | 95.0 | 9 |
| 20260429 | 2 | 11 | LEAN MASTER | 15.0 → 18.0 |  +20.0% | 8.6 | 6 |
| 20260422 | 1 | 2 | GIDDY UP | 8.1 → 9.4 |  +16.0% | 12.0 | 12 |
| 20260429 | 2 | 8 | SEA DIAMOND | 13.0 → 15.0 |  +15.4% | 36.0 | 12 |
| 20260422 | 1 | 6 | HAPPY BOYS | 6.7 → 7.7 |  +14.9% | 10.0 | 5 |
| 20260422 | 1 | 5 | THOUSAND CUPS | 7.0 → 8.0 |  +14.3% | 11.0 | 11 |

## Reading guide
- **STEAMER** = late odds collapsed ≥25% from morning. If the market is informed, steamers should win more than baseline.
- **DRIFTER** = late odds expanded ≥25%. Should win less than baseline.
- ROI @SP is computed at final SP, simulating a flat-$1 WIN bet based on the bucket assignment from the late vs morn comparison.

Threshold for inference: **20** races. Below that, treat all numbers as anecdotal.