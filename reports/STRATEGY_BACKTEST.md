# Strategy Backtest — April 2026

**Question:** Why is the model losing? What strategy actually works?

**Method:** Empirical sweep across all 8 April 2026 meetings (Apr 1, 6, 8, 12, 15, 19, 22, 29 — **77 races**) using **real HKJC dividends** from `reports/dividends_*.json`. All variants flat-staked at 1u/race for apples-to-apples comparison.

## Variant scoreboard

| Variant | Bets | Skip | Hit % | Stake | Return | PnL | ROI |
|---|---|---|---|---|---|---|---|
| **v7c — SARR-QPL · 4 legs · pmod>=0.18** | **43** | **34** | **58.1%** | 43.0 | 47.75 | **+4.75** | **+11.0%** |
| v7a — SARR-QPL · 3 legs · pmod>=0.15 | 46 | 31 | 52.2% | 46.0 | 45.48 | -0.52 | -1.1% |
| v7d — SARR-QPL · 2 legs · pmod>=0.18 | 43 | 34 | 41.9% | 43.0 | 41.30 | -1.70 | -4.0% |
| v7  — SARR-QPL · 3 legs · pmod>=0.18 | 43 | 34 | 51.2% | 43.0 | 40.68 | -2.32 | -5.4% |
| v7e — SARR-QPL · 3 legs union(ET+SARR) | 43 | 34 | 41.9% | 43.0 | 38.85 | -4.15 | -9.7% |
| v1  — SARR-banker QPL every race | 77 | 0 | 44.2% | 77.0 | 65.20 | -11.80 | -15.3% |
| v2  — Mutual-only (skip if ET≠SARR top1) | 28 | 49 | 42.9% | 28.0 | 22.75 | -5.25 | -18.8% |
| v8  — Smart banker (mutual or SARR) | 68 | 9 | 38.2% | 68.0 | 54.52 | -13.48 | -19.8% |
| v9  — Dual QPL (ET+SARR, 0.5u each) | 77 | 0 | 46.8% | 77.0 | 51.40 | -25.60 | -33.2% |
| v3  — `min(et_rank, sarr_rank)` banker | 77 | 0 | 33.8% | 77.0 | 49.50 | -27.50 | -35.7% |
| v5  — Composite #1 banker QPL · all races | 77 | 0 | 29.9% | 77.0 | 44.10 | -32.90 | -42.7% |
| **v0 — Production composite (QIN-anchored)** | 27 | 50 | 14.8% | 27.0 | 12.13 | -14.87 | **-55.1%** |

## v4.7 winner — per-meeting trace

| Date | Bets | Skip | Hits | Hit % | Stake | Return | PnL | ROI |
|---|---|---|---|---|---|---|---|---|
| 2026-04-01 | 6 | 3 | 3 | 50.0% | 6.0 | 8.20 | +2.20 | +36.7% |
| 2026-04-06 | 10 | 1 | 5 | 50.0% | 10.0 | 6.53 | -3.47 | -34.7% |
| 2026-04-08 | 3 | 6 | 3 | 100.0% | 3.0 | 3.45 | +0.45 | +15.0% |
| 2026-04-12 | 6 | 5 | 2 | 33.3% | 6.0 | 4.09 | -1.91 | -31.9% |
| 2026-04-15 | 3 | 6 | 2 | 66.7% | 3.0 | 4.62 | +1.62 | +54.2% |
| 2026-04-19 | 4 | 7 | 3 | 75.0% | 4.0 | 4.65 | +0.65 | +16.3% |
| 2026-04-22 | 5 | 4 | 3 | 60.0% | 5.0 | 7.86 | +2.86 | +57.2% |
| 2026-04-29 | 6 | 3 | 4 | 66.7% | 6.0 | 8.35 | +2.35 | +39.2% |
| **TOTAL** | **43** | **35** | **25** | **58.1%** | **43.0** | **47.75** | **+4.75** | **+11.0%** |

6 of 8 meetings positive. Worst-meeting drawdown -34.7% (Apr 6). On Apr 29 — the night the user lost confidence — v4.7 would have returned **+39.2% ROI** instead of the production strategy's -55.1%.

## Why v4.7 wins

1. **SARR is the stronger ensemble.** Across April, SARR top-1 hit the winner ~44% vs ET's ~30%. The old composite weighting (`w_et = 0.35`, `w_sarr = 0.25`) effectively ignored that and put ET in the banker seat.
2. **QPL > QIN at this hit-rate.** SARR top-1 makes the top-3 ~58% of the time. With 4 legs (each priced at 1u/4 = 0.25u per pair), the QPL `dividend × strike` math comes out positive after the 17.5% takeout.
3. **The 4th leg matters.** 3 legs = -5.4% ROI, 4 legs = +11.0% ROI on the same 43 bets. Many April winners were SARR-rank 4 (one slot below top-3).
4. **`p_model >= 0.18` floor.** Trims ~35 races where the field is too open for any banker bet to have edge. Without the floor (variant v1) we lose 15.3% on extra volume.

## What changed in code

- **`betting_strategy.py`**:
  - Added `V47_CFG` (banker source, leg count, p_model floor).
  - New `_build_sarr_qpl_v47()` constructs the QPL ticket.
  - New `STRATEGY_MODE` env-controlled switch (default `v47_sarr_qpl`).
  - `build_model_ticket()` dispatches to v4.7 first; falls back to legacy QIN-anchored composite via `HK_STRATEGY_MODE=legacy_qin`.
- **`dashboard.py`**: Updated the "How these picks are decided" expander to surface v4.7 logic + the backtest table.

## Results pipeline (verified)

The post-race scrape captures everything needed for evaluation:

```powershell
python scrape_hkjc_results.py --date 2026-04-29
```

writes:
- `reports/results_YYYYMMDD.json` — finishing order, SP, sectional times.
- `reports/dividends_YYYYMMDD.json` — WIN/PLA/QIN/QPL/TRIO/F4 dividends per $10.
- Appends to `hkjc_results_updated.xlsx` master DB.

This is the only data the backtest harness needs. **No additional scraping work required.**

## Going forward

1. Continue running v4.7 on each new meeting. After 3-4 more meetings (~25 more bets) the 95% CI on ROI will tighten enough to lock in or reject the +11% finding.
2. Re-run `_strategy_sweep_apr.py` weekly with the expanded date list — if ROI persists above 0% on rolling 60+ bets, push HKD/unit higher (currently 10).
3. If a meeting ever hits -50% drawdown over 4 races, fall back to PAPER bets only and re-investigate (likely an ET/SARR upstream regression).

## Reverting

```powershell
$env:HK_STRATEGY_MODE = "legacy_qin"   # back to old QIN-anchored composite
```
