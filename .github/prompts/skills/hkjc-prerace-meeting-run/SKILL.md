# Skill: hkjc-prerace-meeting-run

## Purpose
Generate the full pre-race analysis package for a single HKJC meeting:
racecard → form guide → SARR → pace projection → speedmap → race-day PDF/txt.

## Triggers
- "run meeting for YYYY-MM-DD"
- "generate race day report"
- "pre-race for <date>"
- "build the race card for <date>"

## Inputs
- `date_iso` (required): meeting date as `YYYY-MM-DD`.
- `going_turf` (default `"Good"`): one of HKJC going codes (Good, Good to Firm, Good to Yielding, Yielding, Yielding to Soft, Soft, Heavy, Wet Fast, Wet Slow).
- `going_awt` (default `"Good"`): all-weather track going.
- `skip_scrape` (default auto): True if `racecards/racecard_<YYYYMMDD>.xlsx` already exists AND was modified within last 12 h.
- `skip_sarr` (default `False`): set True only if SARR build was already done today.

## Procedure

1. Verify Python env per **Hard rules** in `copilot-instructions.md`.
2. Check `racecards/racecard_<YYYYMMDD>.xlsx`:
   - Missing → must scrape; consult skill `hkjc-scrape-doctor` first if previous scrape attempts failed.
   - Present and fresh → set `skip_scrape=True`.
3. Invoke (call directly OR via `agent_skills.run_prerace_meeting(...)`):
   ```pwsh
   $env:PYTHONIOENCODING="utf-8"
   & 'C:\Users\tbhbr\miniconda3\python.exe' run_meeting.py `
       --date <YYYY-MM-DD> `
       [--skip-scrape] [--skip-sarr] `
       --going-turf "<turf>" --going-awt "<awt>"
   ```
4. Capture last 60 lines of output. Look for:
   - `WROTE reports/race_day_analysis_<YYYYMMDD>_v4.4_*.txt`
   - `WROTE reports/race_day_<YYYYMMDD>_v4.4.pdf`
   - any `ERROR` / `Traceback` → STOP and report to user.
5. After successful completion:
   - Open the .txt report path in VS Code.
   - Surface model top-3 per race in the chat reply (use the parser pattern in `_apr22_pace_walkthrough.py`).
   - Remind the user to cross-check against blackbook before betting.

## Outputs to user
- Path of the generated PDF + txt report.
- Brief race-by-race summary: distance, going, model top pick + confidence.
- Any warnings (missing horse history, standby horses still in field, calibration drift, etc.).

## Failure modes
- Racecard scrape fails → delegate to `hkjc-scrape-doctor`.
- SARR build crash → re-run with `--skip-sarr` and warn user that pace projections are stale.
- xlsx PermissionError → apply OneDrive→TEMP workaround from hard rules.

## Dashboard mirror
The **Agent Skills → Pre-Race Meeting Run** panel calls
`agent_skills.run_prerace_meeting(...)` with the same signature. Keep them aligned.
