# Skill: hkjc-postrace-pipeline

## Purpose
Process results from a completed HKJC meeting: scrape results → append to DB →
rebuild upcoming form-guide caches → run betting utility eval →
produce a "what went right / wrong" summary.

## Triggers
- "post-race for YYYY-MM-DD"
- "process results from <date>"
- "what went right/wrong on <date>"
- "results came in for <date>"

## Why this skill exists
2026-05-09 and 2026-05-13 were normal race meetings run on **rain-affected
going** (Good-to-Yielding / Yielding — same category as 17 May). Results
existed but had not been appended to `hkjc.db` because the post-race
pipeline had not been run for those dates. A routine backfill resolved it.
The skill exists to prevent this gap from occurring again: it MUST verify
DB row insertions and trigger a form-guide rebuild for any meeting in the
next 14 days.

Rain-affected meetings are **not special-cased** — they use the same
pipeline. The going string (`Good-to-Yielding`, `Yielding`, etc.) must be
recorded accurately in both the pre-race run and the post-race notes so
going-filtered form queries work correctly.

## Procedure

1. Run the 8-step results pipeline (use `agent_skills.run_postrace_pipeline(date_iso)`
   or call `dashboard._run_results_scraper(full=True, target_date=...)`).
2. **Belt-and-braces DB append** — always re-run, even if step 1 reports success:
   ```python
   from pathlib import Path
   from db_utils import append_results_to_db
   n = append_results_to_db(Path(f"reports/results_{yyyymmdd}.json"), verbose=True)
   assert n > 0, "post-race DB append inserted 0 rows — investigate"
   ```
3. **Rebuild upcoming form-guide caches** — scan `racecards/` for any
   meeting dated within the next 14 days; for each, rebuild
   `cache/form_guide_<date_iso>.json` against the refreshed DB.
4. **Betting utility eval** — run the user's bet summary against actuals.
5. **What-went-right / wrong write-up** — for each race the user bet on:
   - Did the model top pick finish top-3?
   - Did the combined (model + subjective) pick beat the model alone?
   - Pace projection vs actual speedmap (use `smap_postrace_diagnostic.py <yyyymmdd>`).
   - Any standby/reserve horse misclassification?
6. **Memory update** — if anything unusual happened, invoke `memory-curator`
   and write to `/memories/repo/incident-<date>.md`.

## Outputs to user
- Rows appended to DB (must be > 0).
- List of form-guide caches rebuilt.
- Per-race verdict table: race | bet | outcome | model rank | combined rank | notes.
- Aggregate hit-rate vs prior 30 days.

## Failure modes
- `append_results_to_db` returns 0 → results JSON is empty or already in DB.
  Inspect `reports/results_<yyyymmdd>.json` length. If empty, results scrape failed.
- Form-guide rebuild crash → report which meeting failed; do NOT abort the others.
- Smap diagnostic crash → log and continue; pace audit is non-blocking.

## Dashboard mirror
**Agent Skills → Post-Race Pipeline** runs the same function and renders the
verdict table inline. The "belt-and-braces" append already exists in
`dashboard._run_results_scraper` — do not regress it.
