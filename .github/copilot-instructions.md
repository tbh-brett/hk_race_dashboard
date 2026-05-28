# HKJC Racing Analysis — Copilot Instructions

This repo runs an HKJC (Hong Kong Jockey Club) racing analysis & betting pipeline.
The same workflows are exposed as **agent skills** (for VS Code Copilot) and as
**dashboard actions** (in `dashboard.py` → `Agent Skills` page) via the shared
runtime module `agent_skills.py`. Whenever you change a skill, change the
corresponding runtime function so both surfaces stay in sync.

## Hard rules (apply to ALL skills)

- Python interpreter: `C:\Users\tbhbr\miniconda3\python.exe` — never `.venv`, never `python` from PATH.
- Always set `$env:PYTHONIOENCODING="utf-8"` before any Python invocation in PowerShell.
- OneDrive xlsx PermissionError: copy to `$env:TEMP`, edit, copy back.
- `scrape_hkjc.py` requires `--horse-cache "$env:TEMP\horse_cache_hkjc.json" --no-cache`.
- Reserve/standby horses: filter by `is_standby` flag OR `jockey is NaN` AND presence in `standbylist` table.
- All user-facing timestamps in the dashboard are **HKT** (UTC+8).
- Never `git push --force`, never `git reset --hard` published commits, never `--no-verify`.

## Skills (repo-scoped)

Each skill lives at `.github/prompts/skills/<name>/SKILL.md`. Use the `read_file` tool
to load full instructions when the trigger conditions below match the user request.

| Skill | Triggers | Runtime fn in `agent_skills.py` |
|---|---|---|
| `hkjc-prerace-meeting-run` | "run meeting", "pre-race for…", "generate race day report" | `run_prerace_meeting(date_iso, going_turf, going_awt, skip_scrape)` |
| `hkjc-postrace-pipeline` | "post-race for…", "process results", "what went right/wrong" | `run_postrace_pipeline(date_iso)` |
| `python-env-enforcer` | any Python execution in this repo | `build_python_cmd(script, args)` |
| `hkjc-scrape-doctor` | "scrape failed", "racecard missing", "horse cache stale", "PermissionError" | `diagnose_scrape(date_iso)` |
| `memory-curator` | "remember this", end of long session, "update memory" | `curate_session_memory(notes)` |
| `hkjc-form-screener` | "screen the form", "form review for…", "manual form scan", "head-to-head vs rivals", "weight swing" | `run_form_screener(date_iso, rebuild)` |

## Skill invocation contract

1. Read the skill's `SKILL.md` in full before acting.
2. Honour the **Hard rules** above — they override anything in a SKILL.md that conflicts.
3. If the skill has a runtime function in `agent_skills.py`, prefer calling that
   over re-implementing the logic ad hoc — it is the single source of truth and
   what the live dashboard also uses.
4. After completing a skill, record incidents/learnings via `memory-curator`.

## Project-specific context to remember

- v4.2 model: Risk / FormFranking / Consistency / PaceMultipliers all REMOVED (they were inverted in backtest).
- ESZ is the strongest single signal (ρ=0.516). Running-style 76% within-1-band. Both stay intact.
- Pace prediction accuracy is only ~14% label-match — regression too weak. Pace beneficiaries disabled until pace accuracy ≥50%.
- Combined strategy (model + user subjective picks) consistently outperforms either alone.
- Race-card Excel sheet names increment per meeting (Sheet16, Sheet17, …) in the OLD HKJC format. New format from April onwards uses `racecards/racecard_YYYYMMDD.xlsx`.
- 9+13 May 2026: normal meetings run on **rain-affected going** (Good-to-Yielding / Yielding), same category as 17 May. Results were backfilled to DB as routine housekeeping (post-race pipeline had not been run for those dates). Belt-and-braces append + auto form-guide rebuild now in `dashboard._run_results_scraper(full=True)`. Skills MUST verify DB row counts after any post-race run.
- Rain-affected meetings (Good-to-Yielding, Yielding, Yielding-to-Soft, Soft, Heavy, Wet Fast, Wet Slow) are routine — always record the exact going string in `run_meeting.py --going-turf` and in post-race notes so going-specific form can be filtered correctly.
