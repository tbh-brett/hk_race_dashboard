# ET vs SARR — April 2026 Forensic Analysis

_Generated from `compare_et_vs_sarr.py` on 7 meetings (01, 06, 08, 12, 15, 19, 22 April), 69 races._
_Data: `cache/et_vs_sarr.json`_

---

## 1. Executive summary — why SARR *feels* more reliable

**It's not winning more. It's _placing_ more.**

| Metric | ET | SARR | Δ |
|---|---:|---:|---:|
| Top-pick **WIN** (1st) | 23.5 % | 23.2 % | −0.3 pp |
| Top-pick **PLACE** (top-3) | 45.6 % | **53.6 %** | **+8.0 pp** |
| Top-3 contains 1st | 39.7 % | 34.8 % | −4.9 pp |
| Top-3 = trifecta (any order) | 1.5 % | **4.3 %** | **+2.8 pp** |
| Top-1 agreement between models | 34.8 % |  |  |

**The two models pick the same rank-1 horse only 1-in-3 races**, yet win at almost
identical rates. What SARR does better is _avoid disasters_: its top pick
lands in the money 53.6 % of the time — **+8 pp over ET**. That is the
"felt reliability" the user is describing. SARR's top-3 also assembles the
full trifecta 3× as often (4.3 vs 1.5 %).

---

## 2. Where each model actually wins

### Venue (the big surprise — SARR is NOT the HV specialist)

| Venue | n | ET WIN | SARR WIN | ET PLC | SARR PLC |
|---|---:|---:|---:|---:|---:|
| **HV** | 27 | **29.6 %** | 25.9 % | 51.9 % | 51.9 % |
| **ST** | 42 | 19.5 % | 21.4 % | 41.5 % | **54.8 %** |

- At **HV** ET is slightly ahead on WIN, PLACE is tied. The hypothesis
  "SARR is better because of HV" is **not supported** by April data.
- At **ST** SARR converts far more placings (+13 pp). This is where SARR's
  perceived reliability really lives — and it happens to be 60 % of the
  sample.

### Distance bucket

| Bucket | n | ET WIN | SARR WIN | ET PLC | SARR PLC |
|---|---:|---:|---:|---:|---:|
| Sprint (≤1200) | 35 | 23.5 % | 22.9 % | 44.1 % | **54.3 %** |
| Mile (1201-1650) | 26 | **23.1 %** | 15.4 % | 50.0 % | 50.0 % |
| Route (>1650) | 6 | 16.7 % | **50.0 %** | 33.3 % | 50.0 % |

- **Mile is ET's home turf** (23 % WIN vs SARR 15 %).
- **Route is SARR's blow-out win** (50 % vs 17 %). Small n (6) but 3/1
  ratio is striking — SARR's sectional-anchored stamina read dominates
  when late-speed tokens matter most.
- **Sprint WIN is tied; SARR still +10 pp PLACE**.

### Venue × distance crosstab

| Venue · Bucket | n | ET WIN | SARR WIN | ET PLC | SARR PLC |
|---|---:|---:|---:|---:|---:|
| HV · mile | 9 | **33.3 %** | 11.1 % | **66.7 %** | 44.4 % |
| HV · sprint | 16 | 31.2 % | 31.2 % | 43.8 % | 56.2 % |
| HV · route | 2 | 0.0 % | 50.0 % | 50.0 % | 50.0 % |
| ST · sprint | 19 | 16.7 % | 15.8 % | 44.4 % | **52.6 %** |
| ST · mile | 17 | 17.6 % | 17.6 % | 41.2 % | **52.9 %** |
| ST · route | 4 | 25.0 % | **50.0 %** | 25.0 % | 50.0 % |

**The standout gap**: _HV Mile (1650m)_ — ET **triples** SARR's WIN rate
(33 % vs 11 %) and is ~20 pp ahead on PLACE. This is the pocket where
ET's pace/ESZ stack is genuinely working and SARR loses its edge.

---

## 3. What each model is missing

### SARR misses
1. **HV mile (1650m)**: tactical, tight-turn races where pace scenario +
   ESZ dominate. SARR has no pace model of its own — it borrows from
   ET's pace label but doesn't weight draw × pace interactions the way
   ET does. Biggest fix opportunity.
2. **Top-3 depth**: ET's top-3 is slightly better at harbouring the
   eventual winner (39.7 vs 34.8 %). SARR's top-3 is tighter but not as
   rich — if you're casting a WIN + 2 bankers, ET's basket is marginally
   safer.

### ET misses
1. **ST place consistency**: 41.5 vs 54.8 % (-13 pp). ET's rank-1 is too
   often a "boom-or-bust" horse at ST, landing 1st or 6+. SARR's
   style × venue fit stabilises the bottom of the top-3.
2. **Route races**: 17 vs 50 % WIN. Route staying ability isn't well
   encoded in ET's projected-time logic. SARR's `f_lsa` (late-sectional
   ability) and `f_fmrp` (form-mirrored race performance) carry the
   signal.
3. **Sprint place**: 44 vs 54 %. Same story — SARR avoids the "big-name
   disappointment" bet.

---

## 4. Recommendations — gap fills

| Priority | Fix | Expected gain |
|---|---|---|
| **P1** | Add an **ensemble rank** to the dashboard: `0.5·ET + 0.5·SARR` for ST; `0.65·ET + 0.35·SARR` for HV-mile; `0.3·ET + 0.7·SARR` for route. Default to SARR overall because the PLACE uplift is larger than the tiny WIN trade-off. | +4-6 pp PLACE |
| P2 | Import SARR's `f_lsa` and `f_fmrp` as extra regressors into ET's projected-time model. They are the features most correlated with SARR's route wins. | Closes ET route gap |
| P3 | Add a **style × draw × pace** interaction term to SARR (currently SARR has style × venue only). This would reclaim the HV-mile territory where ET wins today. | Closes SARR HV-mile gap |
| P4 | Dashboard: the **Mutual Model Top Picks (ET ∩ SARR)** section already exists — promote it. Races where both models agree on rank-1 are high-confidence plays; when they disagree, fall back to the venue/distance rule above. | Reduces cognitive load |
| P5 | For **WIN bets**, prefer ET's top pick at HV-mile and use SARR at ST/route. For **PLACE / QPL / Q-Place** bets, prefer SARR everywhere — its PLACE rate is the single clearest edge in the April data. | Direct betting rule |

---

## 5. Caveats

- **Sample is small** (69 races, 7 meetings). Route bucket has only 6
  rows — the "+33 pp SARR route WIN" is suggestive, not proven.
- Apr-05 and Apr-13 SARR reports failed to generate (IndexError in PDF
  step); if those meetings are back-filled, recompute.
- Results / dividends were missing for Apr-22 earlier — restored in this
  session. The above numbers include Apr-22.
- ET versions vary: early April uses v3.4.8, later uses v4.4. The
  comparison treats them as one "ET family"; if you want strict v4.4-only,
  pass `--dates 2026-04-12,2026-04-15,2026-04-19,2026-04-22`.

---

_To regenerate: `python compare_et_vs_sarr.py` → updates `cache/et_vs_sarr.json`._
