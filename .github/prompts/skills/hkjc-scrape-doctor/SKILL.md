# Skill: hkjc-scrape-doctor

## Purpose
Diagnose and repair failures in the HKJC scrape pipeline
(`scrape_hkjc.py`, `scrape_hkjc_racecard.py`).

## Triggers
- "scrape failed", "scrape hangs"
- "racecard missing for <date>"
- "horse cache stale" / "horse pages 404"
- `PermissionError` on `racecard_*.xlsx`
- Empty `racecards/racecard_<YYYYMMDD>.xlsx` (file exists but Sheet1 is blank)

## Diagnostic ladder (try in order)

1. **Verify the meeting exists on HKJC.** Use `requests.head` to
   `https://racing.hkjc.com/racing/information/English/Racing/RaceCard.aspx?RaceDate=<YYYY-MM-DD>`.
   Status 200 + non-empty body → real meeting. 404/empty → no meeting that day; stop.
2. **Clear horse cache.** Run scrape with `--horse-cache "$env:TEMP\horse_cache_hkjc.json" --no-cache`.
   This is the single most common fix.
3. **OneDrive PermissionError.** Copy target xlsx to `$env:TEMP`, point
   scraper at TEMP path via env var `RACECARD_OUTPUT_DIR`, copy back.
4. **Old vs new HKJC format.**
   - Pre-April 2026 meetings: `scrape_hkjc.py`, xlsx filename `YYYYDDMM`,
     sheets incrementing `Sheet16, Sheet17…`. Use `parse_race_card()`.
   - April 2026 onwards: `scrape_hkjc_racecard.py`, `racecards/racecard_YYYYMMDD.xlsx`,
     `parse_scraped_race_card()`, `USE_SCRAPED=True`.
   - Picking the wrong parser yields an empty field.
5. **Reserve / standby contamination.** If scrape "succeeds" but the field
   has 16 horses on a 14-runner race, you are including standby horses.
   Re-filter: drop rows where `jockey` is NaN AND the horse appears in the
   `standbylist` table.
6. **Network / rate-limit.** HKJC throttles ~1 req/sec per IP. Add
   `time.sleep(1.1)` between horse-page fetches. Symptoms: scrape OK at start,
   then 100% blank columns from horse #6 onwards.

## Runtime helper
`agent_skills.diagnose_scrape(date_iso)` returns a structured report:
```python
{
    "meeting_exists": bool,
    "racecard_xlsx_path": Path | None,
    "racecard_size_kb": float,
    "field_size_per_race": dict[int, int],
    "standby_contamination": list[str],   # horse names suspected to be standby
    "suggested_action": str,
}
```

The dashboard's **Agent Skills → Scrape Doctor** panel renders this report
inline. The agent should call the runtime helper rather than re-implementing
checks ad hoc.

## What NOT to do
- Do not delete `racecards/racecard_*.xlsx` files without confirmation —
  some encode hand-corrected scratchings the user typed in manually.
- Do not bypass `--no-cache` on the user's behalf just because it is slow.
- Do not commit a `.txt` or `.log` containing scraped horse details — they
  may include the user's session cookies if env vars leaked into stdout.
