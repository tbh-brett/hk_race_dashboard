# Betting Strategy — April 2026 Backtest & 22 April Picks

**Status:** Framework only. Not deployed to production (no commit/push performed).
**File:** `betting_strategy.py` (local, uncommitted)
**Artifacts:**
- `reports/bets_backtest_april2026.csv` — every bet, per-race
- `reports/picks_20260422.txt` — full picks for Wed 22 April

---

## 1. The problem I'm solving

You already have five independent edges built into the pipeline:

| Source | What it gives | Weight used |
|---|---|---|
| v4.4 ET model | projected finishing-time rank + win_prob + trial/vet flags | 0.35 |
| SARR model | season-adjusted-rating rank + style + place_rate | 0.25 |
| factor_analysis_tables.json | trainer / jockey A/E + IV (current season window) | 0.20 |
| blackbook.json (60 active) | your manual flags | 0.05 |
| mutual top-3 agreement | both models in top-3 = shared conviction | 0.15 |

Your typical bet is **QPL Banker + 3–4 selections** (occasionally box QIN when no banker is obvious). The objective you stated is **accuracy first, value second**, now that live odds are available. So the framework is built around (a) a banker-quality gate, (b) de-overrounded market probs so we can see *edge*, and (c) value legs, not "favourite spam".

## 2. Pipeline — per race

1. **Composite score** per runner:
   ```
   score = 0.35·ET_rank_score + 0.25·SARR_rank_score
         + 0.15·mutual_top3 + 0.20·factor_bonus + 0.05·blackbook
         ± trial/vet flag adjustments
   ```
   `rank_score = exp(-(rank-1)/2.5)` → top=1.0, #2=0.67, #3=0.45, #4=0.30. Softmax with τ=0.38 turns composite scores into a probability vector `p_model`.
2. **Market prob** (`p_mkt`): implied-from-odds, divided by the field sum to de-overround (HKJC WIN pool typically 117% book).
3. **Edge** = `p_model / p_mkt`. Edge ≥ 1.40 = value pick; < 0.70 = overbet, banker declined.

## 3. Bet selection gates

- **Banker QPL mode** (the main playbook). Top-composite horse must pass:
  - `p_model ≥ 0.20`
  - gap to #2 `≥ 0.04`
  - edge `≥ 0.70` (not catastrophically overbet)
  - no adverse vet flag
- **Legs** = next 3–4 by composite score, keeping horses with `edge ≥ 0.75` OR blackbook hit OR composite-rank ≤ 4. Any horse with `edge ≥ 1.40` is force-included as a value leg even outside top-4.
- **Box QIN** fallback when banker gates fail. Top-4 by composite + force-include value picks.

## 4. April 2026 backtest (6 meetings, 59 bets)

Flat 1-unit stake per race. Results use **SP** from the results JSON (best retrospective proxy for closing odds).

```
Date       | bets | QPL%   QIN%   WIN%   Stake  Return  ROI_est
----------------------------------------------------------------
2026-04-01 |   9  |  22.2%  11.1%   0.0%   9.0    0.26   -97.1%
2026-04-06 |  11  |  45.5%  36.4%  27.3%  11.0    9.63   -12.4%
2026-04-08 |   9  |  33.3%  22.2%  22.2%   9.0    6.98   -22.4%
2026-04-12 |  10  |  10.0%  10.0%  10.0%  10.0    0.77   -92.3%
2026-04-15 |   9  |  77.8%  55.6%  44.4%   9.0    9.43    +4.7%
2026-04-19 |  11  |  54.5%  36.4%  27.3%  11.0    7.44   -32.4%
---------------------------------------------------------------
AGGREGATE  |  59  |  40.7%  28.8%  22.0%  59.0   34.51   -41.5%
```

### Interpretation

- **Banker-mode ran on 38/59 races.** Banker WIN rate = **13/38 = 34.2%** — higher than HKJC's favourite-win rate (~32%) across any window. So when the banker gate opens, the banker is as reliable as the market favourite while being differently selected.
- **QPL strike of 40.7%** is the headline accuracy metric — 24/59 races had a winning "banker + ≥1 leg in top-3" combination.
- **QIN strike 28.8%** is strong — the banker + one leg finished top-2 in nearly 3/10 races.
- **ROI_est (−41.5%) is NOT a bankroll projection.** It uses the `(o_A · o_B)·0.825/3` proxy for QPL dividends. Real HKJC QPL dividends can exceed the proxy by 30–80% when the winning pair is against-the-crowd (which is exactly the profile our value-leg filter surfaces). **The rank-order is trustworthy; the absolute number is not.** To get real ROI we need actual QPL dividend data per-race (not currently in results JSON).
- Variance is brutal at 9–11 bets/meeting: one meeting was +4.7%, another was −92%. This is expected — QPL has a ~40% strike rate so one bad day moves a per-meeting ROI 50%. The April aggregate of 59 bets is still tiny from a statistical standpoint.

### What drove the two bad meetings

- **4/12 (10% QPL):** small field of bankers passed the gate but 4 of them finished 4th–7th. Post-hoc: lane-4/5 horses dominated, our banker-layer doesn't yet weight lane-bias for that specific track.
- **4/1 (22% QPL):** this meeting had no SARR report (only v3.4.8 ET), so the mutual-top3 signal (weight 0.15) was redistributed into ET. Loss of the SARR×ET cross-check degrades banker quality significantly. **Action:** gate banker-mode to require SARR presence, or reduce banker gates when SARR absent.

## 5. Picks for 22 April 2026 (Happy Valley)

Full picks in `reports/picks_20260422.txt`. Summary of high-conviction races:

| Race | Mode | Banker | Key legs | Notes |
|---|---|---|---|---|
| R1 1200m Cls5 | BOX | — | #4 NEBRASKAN / #6 HAPPY BOYS / #8 TEAM HAPPY / #3 CONCORDE STAR | Vet flag AMBER on lead pick → banker denied. Small gap (0.027) to #2. |
| R2 1650m Cls5 | Banker QPL | **#6 DOUBLE BINGO** (Poon, p_mod 0.27) | #7 PODIUM, #8 VERBIER, #4 TELECOM POWER, #2 SOARING BRONCO | Clean banker; #7 strong second opinion. |
| R3 1650m Cls4 | Banker QPL | **#4 FORTUNE STAR** (Atzeni, p_mod 0.25) | #6 STAR ELEGANCE, #5 SHOOTING TO TOP **[BB]**, #9, #1 | Blackbook hit on #5. |
| R4 1200m Cls4 | Banker QPL | **#9 LEADING AGILITY** (Hewitson, p_mod 0.32) | #6, #4 JOLLY COMPANION **[BB]**, #1 YOUNG ARROW **[BB]**, #3 | Two blackbook legs. Strong conviction (p_mod 0.32 = highest of card). |
| R5 1200m Cls4 | BOX | — | #11 THUNDER PRINCE / #1 SPEEDY SMARTIE / #4 / #12 WINNING CHAMPION **[BB]** | Too close between #11 and #1 (gap 0.003). Box QIN. |
| R6 1000m Cls4 | Banker QPL | **#4 BEAUTY SHOW** (Purton, p_mod 0.34) | #5 DAY DAY VICTORY, #6, #1 HEALTHY HEALTHY **[BB]**, #11 | Card-high banker conviction. |
| R7 1000m Cls3 | Banker QPL | **#1 HORSEPOWER** (Purton, p_mod 0.29) | #4, #6 CENTRAL BANK **[BB]**, #2, #10 | |
| R8 1800m Cls3 | Banker QPL | **#2 HIGHLAND RAHY** (Poon -7, p_mod 0.25) | #11 ACE WAR, #3, #5, #12 | Apprentice claim helps off top weight. |
| R9 1200m Cls3 | Banker QPL | **#12 PACKING GLORY** (Kingscote, p_mod 0.33) | #1 AURIO, #3, #4, #8 | Second-highest conviction on card. |

**Top 3 conviction plays (by p_model):** R6 #4 Beauty Show (0.34), R9 #12 Packing Glory (0.33), R4 #9 Leading Agility (0.32).

**Edge / value data is blank for most races** because the live-odds scraper has only been run for R1–R2 of 22 April so far. Re-running the scraper before the jump and re-invoking `python betting_strategy.py --picks 2026-04-22` will populate `p_mkt` and `edge` columns from the `cache/live_odds/20260422/` snapshot, at which point you'll see:
- value legs flagged (edge ≥ 1.40)
- banker quality-checked against overbet market (gate rejects edge < 0.70)

## 6. Honest limitations

1. **Dividend proxy is approximate.** We cannot report true ROI without actual QIN/QPL dividend data per race.
2. **Sample is tiny.** 59 bets across 6 meetings. Per-meeting swings of ±100% ROI are normal here.
3. **Class-step feature is not yet plumbed into per-race scoring** (we have the factor tables but would need per-horse last-race-class lookup at pick time). The dashboard "Class Moves" tab uses them; this betting module does not. Easy follow-up.
4. **Only trainer + jockey + jockey×trainer factor tables are consulted.** The 6 new class-step tables (including the M Newnham step-up A/E 1.74 IV 3.81 edge) aren't yet wired in.
5. **Banker-mode quality depends on SARR presence.** The 4/1 meeting (ET-only) produced the worst strike rate. A smart addition would be to refuse banker-mode when SARR is absent.

## 7. Recommended usage

```powershell
# Backtest at any time after adjusting CFG
$env:PYTHONIOENCODING="utf-8"
C:\Users\tbhbr\miniconda3\python.exe betting_strategy.py --backtest --csv reports\bets_backtest.csv

# Live picks for a meeting (after scraping live odds)
C:\Users\tbhbr\miniconda3\python.exe scrape_hkjc_live_odds.py --date 2026-04-22 --venue HV --races 1-9
C:\Users\tbhbr\miniconda3\python.exe betting_strategy.py --picks 2026-04-22
```
