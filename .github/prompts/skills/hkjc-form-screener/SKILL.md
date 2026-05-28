# Skill: hkjc-form-screener

## Purpose
Produce a thorough, **rule-based** form review of every horse on a race card.
Mirrors the manual screening process: per-run form scoring, strength-of-
competition (collateral form), and weight-swing / head-to-head vs today's
rivals — all serialised to JSON and surfaced in the dashboard.

## Triggers
- "screen the form for YYYY-MM-DD"
- "form review for <date>"
- "manual form scan", "scan every horse"
- "head-to-head vs rivals", "weight swing"

## Inputs
- `date_iso` (required) — meeting date `YYYY-MM-DD`.
- `rebuild` (default False) — recompute even if `reports/form_screen_<YYYYMMDD>.json` exists.
- `max_prior_runs` (default 6) — matches the form-guide cache structure.

## Required data (all already in workspace)
| Source | Used for |
|---|---|
| `cache/form_guide_<date>.json` | Per-run history with `top5` + `top5_next` |
| `hkjc.db` (table `results`) | Head-to-head / weight-swing across all dates |
| `reports/commentary_<date>.json` | Incident text + polarity tags |
| `blackbook.json` | Active blackbook overlay per horse |
| `racecards/racecard_<YYYYMMDD>.xlsx` | Today's field + draws + weights + gear |
| `pace_index*.json` (optional) | Pace map overlay for tag enrichment |

If `cache/form_guide_<date>.json` is missing, the skill rebuilds it from the
DB before screening.

## Procedure

### Pass 1 — Per-horse form scoring (spec §1a + §1b)
For each prior run of each horse, compute structured signals:

- `conditions_match` — categorical similarity (going family, track, distance band, class) → 0–1.
- `draw_band` — inside (1–4) / middle (5–9) / wide (10+) for that track/distance.
- `actual_weight_today` = `declared_weight − apprentice_claim` (jockey `(-N)` regex).
- `rating_trajectory` — rating delta vs each prior run (rising/flat/falling).
- `class_move` — UP / SAME / DOWN vs today.
- `distance_pattern` — first-time-at-trip vs preferred-trip-band.
- `margin_lengths` — parse `lbw` string ("2-1/4", "SH", "DNF") into float lengths.
- `time_vs_par` — finish_time vs `HKJC_STANDARD_TIMES[(distance, going_family)]`.
- `incidents` — pulled from `commentary_<date>.json`, weighted by `polarity_score`.
- `blackbook_status` — ACTIVE / EXPIRED / NONE.

### Pass 2 — Strength of competition (spec §2a + §2b)
For each prior run:
- Walk `top5` finishers + their `top5_next` follow-ups.
- Award positive collateral if horses our subject **beat** went on to win/place next out.
- Penalise if top finishers in that race flopped next out.
- Weight by recency (1-week-old line > 6-week-old line).

### Pass 3 — Weight-swing & head-to-head (spec §2c)
For each rival in today's race, query the DB for prior meetings with the
same two horses. Compute:
- `prior_meetings`: list of `(date, going, A_weight, B_weight, A_pos, B_pos, margin_lengths)`.
- `weight_delta_today` = (A_today − A_prior) − (B_today − B_prior).
- Rating-adjusted version normalises by `(A_rating − B_rating)` each time.

### Extras included (per user selection)
| Code | Extra |
|---|---|
| E1 | Equipment-change flag (gear vs last race) |
| E3 | First-up / 2nd-up / 3rd-up resumption pattern |
| E4 | Trip up/down vs usual distance |
| E5 | Track-bias overlay (post-race only, optional) |
| E7 | Last-600 m sectional vs class par |
| E8 | Going-specific record summary ("3-1-0 on Y+") |
| E9 | Days-since-last-run freshness band |
| E11 | Stable-mate watch (same trainer multiple runners) |
| E12 | Auto-shortlist top-3 form-screen + 3-way confluence (form / model / blackbook) |

## Output

### File: `reports/form_screen_<YYYYMMDD>.json`
Schema v1:
```jsonc
{
  "schema_version": 1,
  "date_iso": "2026-05-31",
  "generated_at": "<HKT iso>",
  "races": [
    {
      "race_no": 1,
      "distance": 1200, "going": "Y", "track": "ST", "class": 4,
      "stable_mates": [["TrainerX", ["HorseA", "HorseB"]]],
      "shortlist": [{"horse": "...", "score": 0.78, "rationale": "..."}],
      "horses": [ /* HorseScreen … */ ]
    }
  ]
}
```

### Verdict text (rule-based, v1)
Templates assembled from the score cards. No LLM call. Future v2 may add an
LLM polish pass — schema accommodates `verdict_text_llm` field.

## Dashboard mirror
`agent_skills.run_form_screener(date_iso, rebuild=False)` is called by the
**Agent Skills → 🔍 Form Screener** tab. UI:
- Date picker + "rebuild" toggle
- One sub-tab per race
- Per-horse expander: score cards, form-line table, head-to-head matrix, incident chips, blackbook badge

## Failure modes
- Form guide cache missing → auto-rebuild via `build_form_guide.build_for_date()`.
- Commentary JSON missing → incidents flagged "no_report" (non-blocking).
- DB head-to-head query slow → cache per-meeting results in `cache/h2h_<date>.json`.
- New horse (debutant) → emit minimal screen with `is_debutant: True` and skip Pass 2 / Pass 3.
