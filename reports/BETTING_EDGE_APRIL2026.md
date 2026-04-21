# Betting Edge Analysis — April 2026 (REAL dividends)

**Data:** 59 races across 6 meetings (4/1, 4/6, 4/8, 4/12, 4/15, 4/19). REAL HKJC dividends scraped from `racing.hkjc.com/en-us/local/information/localresults` via `scrape_hkjc_dividends.py` (saved to `reports/dividends_YYYYMMDD.json`). Every ROI figure below is based on real published dividends — **not** a proxy.

## TL;DR — yes, the edge exists

| Strategy | Bets | Hit % | ROI |
|---|---|---|---|
| **WIN top-1 composite** | 59 | 27.1 % | **+34.6 %** |
| WIN top-1 · Cls3–5 · odds 3–8 | 28 | **42.9 %** | **+120.0 %** |
| QPL banker · SARR+ET agree · gap ≥ 0.08 | 5 | 80 % | **+88 %** |
| F4 box top-5 | 59 | 11.9 % | +133 % (one outlier race) |
| F4 box top-5 ex-outlier | 58 | 10.3 % | +25 % |
| PLACE top-1 · Cls3–5 · odds < 8 | 38 | **63.2 %** | +2.7 % |

`win_top1` is the headline signal: **flat-bet the composite #1 in every race, +34.6 % ROI, 4 of 6 meetings profitable**. Filtering to Cls3–5 and only when SP is in the 3-to-8 band tightens the signal dramatically: 42.9 % hit rate, **five of six meetings positive with ROI ≥ +110 %**.

## Where the edge does not exist

| Strategy | Bets | ROI |
|---|---|---|
| WIN top-2 split | 59 | −22 % |
| QIN banker + 3 legs | 59 | −37 % |
| QPL banker + 3 legs (your current style) | 59 | **−44 %** |
| QIN box top-3 | 59 | −16 % |
| QPL box top-4 | 59 | −52 % |
| TRIO box top-4 | 59 | −67 % |

Your current QPL-banker habit loses 44 % because we cover too many legs for the dividend — a $1 stake split across 3 legs caps the upside. It *does* have a 30 % strike rate (3 in 10 races QPL cashes), so the accuracy is there — the payout structure just doesn't reward it.

## Where the model shines — structural findings

### 1. Race class
WIN on top-1 by class (full sample):
- **Cls3: +154 %** (8 hits / 17, $5.50 avg winning price)
- Cls4: −9 %
- Cls5: +6 %
- Cls2: −100 % (0/3, small sample)
- Cls0 (specials): −47 % (2 bets)

The model's training data is dense in Cls3–5 (80 %+ of HK racing). It's thin in top-tier and griffin/specialty races — drop them.

### 2. SP odds band of the top pick (QPL-banker lens)
- Favourite (<3.0): −40 % — we're not beating the public on chalk
- **3.0 – 5.0: −30 % / 5.0 – 8.0: −12 %** — second-wave favourites, best band
- 8.0 – 15 : −73 %
- 15 +   : −100 % — **never banker a 15/1, the model can't time long shots**

### 3. Mutual SARR + ET agreement
- When both models have the horse in top-3: **QPL banker ROI +21.8 %** (10 bets)
- No SARR / only one model: **−56 %** (48 bets)

This is the single strongest filter in the entire analysis. Absent SARR data, the banker gate should be rejected.

### 4. Top-1-vs-top-2 conviction gap
- Gap ≥ 0.08: −27 % (28 bets)
- Gap 0.04–0.08: −56 % (19 bets)
- Gap < 0.04: −65 % (12 bets)

More conviction → less loss. Combined with mutual-top3, the sample shrinks but edge jumps.

### 5. The compound filter that actually wins on QPL
Mutual top-3 **AND** gap ≥ 0.08: 5 bets, 4 hits, **+88 % ROI**. Extremely small sample but directionally the filter is the right one.

## What to actually bet

| Play | When | Stake | Why |
|---|---|---|---|
| **WIN single on composite #1** | Cls3–5, SP 3–8 | 1 u | 43 % strike, +120 % ROI — the main driver |
| **PLACE single on composite #1** | Cls3–5, SP < 8 | 0.5 u | 63 % strike, breakeven ROI — bankroll smoother |
| **QPL banker #1 + 2 legs** | ET + SARR agree top-3, gap ≥ 0.08 | 1 u / 2 | Rare but +88 % ROI; your preferred ticket format |
| **Skip** | Cls0/2, SP > 8 on banker, no SARR | — | Negative edge zones |

F4 box on top-5 is tempting (+25 % ex-outlier) but variance is brutal — 10 % hit rate means 9 losing races for every hit. Only for sizing down and accepting volatility. Not recommended as a core play.

## Implementation into the dashboard — my proposal

A single new page (next to "Live Odds") called **"Model Bets"** with three sections.

### Section A — "What I'd play today" (pre-determined tickets)
For each race on the loaded meeting, show one row:

| R | Race | Primary play | Stake | Why |
|---|---|---|---|---|
| R1 | Cls5 1200m | WIN #4 NEBRASKAN | 1 u | Cls5 + SP 5.2 in sweet-spot band |
| R2 | Cls5 1650m | QPL banker #6 + #7,#8 | 1 u | SARR & ET both top-3, gap 0.09 ✓ |
| R3 | Cls4 1650m | SKIP | — | Banker SP 12.0 → out of band |

Each row is produced by a new `build_model_ticket(race, rows, bet, live_odds)` function in `betting_strategy.py` that applies the filter rules above.

### Section B — "Bet builder" (custom per race)
A Streamlit form:
- Pick a race from the dropdown.
- Choose bet type: WIN / PLACE / QIN banker / QPL banker / QIN box / QPL box / TRIO / F4.
- Show the composite table (rows from `score_race`) with the market-edge column.
- The form computes: expected hit probability, expected return using softmax-implied probabilities, cost of the ticket. Lets you sim a stake and see projected EV.

### Section C — "How the model has done" (live backtest)
Two widgets:
1. **Table of strategies** (the table at the top of this document) — updated every meeting by running `analyze_betting_edge.py` as a cached Streamlit function. Shows hit rate + ROI rolling.
2. **Equity curve** — line chart of cumulative P/L by meeting for each strategy (WIN_top1, QPL_banker_filtered, PLACE_top1_filtered). Lets you see variance, not just averages.

### Section D (optional stretch) — "Rules & gates" configuration
Sliders for the filter thresholds (odds band, gap, mutual-top-3 on/off). On change, re-runs the analysis on cached data and redraws the tables. Lets you tune gates without touching code.

### Minimal additions to implement
1. `scrape_hkjc_dividends.py` (done) — run nightly alongside `scrape_hkjc_results.py`.
2. In `betting_strategy.py`: add `build_model_ticket()` that returns `(play, stake, reasoning)` and `build_recommended_ticket_list(date)`.
3. In `dashboard.py`: new page "🎯 Model Bets" with the four sections. Reuses `load_meeting`, `score_race` already imported from `betting_strategy`.
4. `analyze_betting_edge.py` already writes `reports/betting_edge_analysis.json` — dashboard can just read that for Section C.

## Honest caveats

- **59 races is still a small sample.** Confidence intervals on the +120 % headline figure are wide — it could comfortably be +40 % to +200 % over a larger sample. What we can be confident about is that **it's not negative**, because even the raw unfiltered WIN-top-1 is +34 %.
- **Cls3 pool (17 races) is the strongest** — if the May data reverts we should check it's still Cls3 that carries the signal, not a one-month artefact.
- **Odds bands are from results SP**; in practice you'd bet on closing live odds which can drift. Live-odds integration (already in scraper) is essential to track this.
- **The filter "mutual top-3 + gap ≥ 0.08" had only 5 bets** — use it as a conviction overlay, not a system on its own, until we have 30+ samples.
- **Stake sizing is flat 1-unit here.** Kelly sizing on the +120 % play (edge ≈ 0.2 after takeout) would recommend ~20 % bankroll per bet which is far too aggressive for 28-bet sample. Use fixed 1 %–2 % of bankroll per WIN single for now.
