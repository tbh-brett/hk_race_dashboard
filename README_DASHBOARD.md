# HKJC Racing Dashboard — Operating Manual

A practical guide to **what every page does** and **what you must do before / after each
meeting, each week, and each month.** Read the *Operating cadence* section first if you
just want the checklist.

---

## 0. Quick start

**Launch / restart the dashboard** (PowerShell). This kills whatever is holding the port
*first* — a stale process is the #1 reason "my changes didn't show up":

```powershell
$env:PYTHONIOENCODING="utf-8"
cd "c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards"
Get-NetTCPConnection -LocalPort 8502 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Get-Job -Name hkjc_dash -ErrorAction SilentlyContinue | Stop-Job -PassThru | Remove-Job
Start-Sleep -Seconds 3
Start-Job -Name hkjc_dash -ScriptBlock {
    $env:PYTHONIOENCODING="utf-8"
    Set-Location "c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards"
    & "C:\Users\tbhbr\miniconda3\python.exe" -m streamlit run dashboard.py --server.port 8502 --server.headless true
} | Out-Null
Start-Sleep -Seconds 20
Receive-Job -Name hkjc_dash -Keep | Select-String "view your Streamlit|not available"
```

Then open **http://localhost:8502**. You should see *"You can now view your Streamlit
app"* in the job log — **not** *"Port 8502 is not available"*.

**Hard rules (never break these):**

- Python interpreter is **`C:\Users\tbhbr\miniconda3\python.exe`** only — never `.venv`,
  never bare `python`. (`.venv` exists only for the editor's read-only analysis.)
- Always set `$env:PYTHONIOENCODING="utf-8"` before any Python command.
- xlsx files on OneDrive throw `PermissionError` — copy to `$env:TEMP`, edit, copy back.
- All timestamps in the dashboard are **HKT (UTC+8)**.
- Git: stage **only** the file you changed (never `git add -A`); never `git push --force`,
  never `git reset --hard` published commits, never `--no-verify`.

---

## 1. Navigation — what each page does

The sidebar is grouped by workflow stage.

### 🟦 Pre-Race

| Page | What it does | When you use it |
|---|---|---|
| **🏁 Race Day Insight** | Race-time cockpit. One race at a time: top picks, ★ model-agreement, verdict banner, market pulse, readiness strip, glossary. The "what do I bet" view on the day. | On the day, race by race. |
| **📖 Form Guide** | Per-horse form history compiled from the results DB. *Build Form Guide cache* button rebuilds it. | Before a meeting; after new results land. |
| **📊 Model Analysis** | The full model output: **Race Day Picks** tab (ET + SARR cards, Edge vs market, Fin column post-race) and **Model Comparison** tab. The sidebar **[ RUN ANALYSIS ]** drives the 3-step pipeline (scrape → SARR → ET). | Generating the card for a meeting. |
| **🔎 Race Lookup** | Look up any past race / horse profile. | Research. |
| **🎽 Trials** | Barrier-trial data + *Scrape Trials* / *Bulk Scrape*. Trials feed the model's trial flag. | Weekly, and before a meeting with first-starters. |
| **🗓️ Fixtures** | Upcoming meeting calendar. | Planning. |

### 🟩 Betting

| Page | What it does | When you use it |
|---|---|---|
| **💡 Betting** | Bet construction / staking from the model edges. | Pre-race once the card is built. |
| **💰 My Bets** | Your bet log + P/L tracking. | Logging bets, reviewing returns. |
| **📓 Blackbook** | Watch-list of horses to follow. Surfaces as the **BB** flag on cards. | Add horses post-race; review weekly. |
| **📡 Live (Feed + Odds)** | Live race feed + live odds snapshots (drift, de-overround implied %, alerts). Live odds power the **Edge** column. | On the day, near post time. |

### 🟧 Post-Race

| Page | What it does | When you use it |
|---|---|---|
| **🏆 Results** | *Scrape Results* pulls finishing positions, **auto-syncs to `hkjc.db` and rebuilds the form guide** (`_run_results_scraper(full=True)`). Also runs the systematic what-went-right/wrong signal eval (rank-1 hit rate, smap, vet, trial). | After every meeting. |
| **🔬 Data Analysis** | Deeper EDA / factor analysis across the results DB. | Periodic review. |

### 🟥 Lab & Tools

| Page | What it does | When you use it |
|---|---|---|
| **🧪 Framework Lab** | Run/compare experimental frameworks against a racecard. | Research. |
| **🧠 Model Lab** | Hosts **GBM**, **Calibration**, and **Backtest** sub-tabs — model fitting, probability calibration, and the *Rebuild Unified Backtest* / legacy monthly + seasonal reports. | Monthly model maintenance. |
| **📄 PDF Builder** | Generates the printable race-day report (CJK via MSJH font). | Pre-race, if you want a PDF. |
| **🤖 Agent Skills** | Runs the same skills as Copilot (pre-race meeting run, post-race pipeline, scrape doctor, form screener, memory curator) from the UI. | Anytime as a shortcut. |

**Global aids (every page):** the meeting **context bar** (📍 venue · 🗓 date · 🏇 races),
and the sidebar **Cloud Persistence** panel (confirm data is syncing to GitHub so it
survives a reboot).

---

## 2. Operating cadence — the checklists

### ✅ Before EVERY race day (T-1 day → morning)

1. **Fixtures** → confirm the meeting date/venue.
2. **Trials** → *Scrape Trials* if there are recent trials / first-starters.
3. **Model Analysis** → set sidebar **Race Date**, **Turf Going**, **AWT Going**
   (use the *exact* HKJC going string, e.g. `Good`, `Good to Yielding`, `Yielding`),
   keep **Re-scrape ON**, click **[ RUN ANALYSIS ]**.
   - Watch the progress bar: [1/3] scrape → [2/3] SARR → [3/3] ET.
4. **Race Day Insight** → check the **readiness strip**: SARR ✓, ET ✓, card-age fresh.
5. **Form Guide** → rebuild cache if results from the previous meeting just landed.
6. Optional: **Form Screener** (Agent Skills) + **PDF Builder** for a printable card.

> ⚠️ **Late scratchings / reserve swaps**: if the card was scraped >12h ago, re-run
> **[ RUN ANALYSIS ]** on the morning of the meeting so scratched horses drop out and
> promoted reserves come in. There is currently **no automatic scratch alert** (see §3).

### ✅ ON race day (live)

1. **Live (Feed + Odds)** → start odds snapshots; the **Edge** column on the ET card
   lights up once live odds exist.
2. **Race Day Insight** → work race by race; ★ tabs = ET & SARR agree (~33% win rate).
3. **Betting** / **My Bets** → place and log bets.

### ✅ After EVERY race day

1. **Results** → *Scrape Results*. This:
   - writes `reports/results_YYYYMMDD.json`,
   - appends rows to `hkjc.db`,
   - rebuilds the form guide,
   - runs the what-went-right/wrong eval.
2. **Verify DB row counts** after the run (skills are required to — you should too):
   the meeting should now appear in the results table.
3. **Blackbook** → add any horses worth following.
4. **My Bets** → reconcile actual returns; parse the account statement
   (`acctstmt (DD Month).txt` → `parse_acct_statement.py`).
5. Note the **exact going** in your post-race notes so going-specific form filters work.

### ✅ Weekly

1. Confirm **all meetings of the week are in `hkjc.db`** (run the missing-meeting sync
   snippet you already use, or re-*Scrape Results* for any gaps).
2. **Form Guide** → rebuild cache.
3. **Trials** → *Bulk Scrape* to keep trial history current.
4. **Blackbook** → prune expired/stale entries.
5. **Cloud Persistence** panel → confirm the latest data pushed to GitHub.

### ✅ Monthly / seasonal

1. **Model Lab → Backtest** → *Rebuild Unified Backtest*; review monthly + seasonal.
2. **Model Lab → Calibration** → re-check probability calibration; refit if drifting.
3. **Data Analysis** → factor/edge review; sanity-check that ESZ (ρ≈0.52) and running-style
   remain the strongest signals.
4. Re-confirm disabled components stay disabled until they earn their place:
   - Pace beneficiaries — only re-enable when pace label accuracy ≥ 50% (currently ~14%).
   - v4.2 removed factors (Risk / FormFranking / Consistency / PaceMultipliers) stay removed.

---

## 3. What you've (probably) missed — gaps & risks

These are not yet automated or are easy to forget:

1. **No automatic scratch / reserve-promotion alert.** Detecting "2 scratched, 1 reserve
   promoted" needs snapshotting each scraped racecard and diffing successive versions —
   not built. Mitigation today: always re-run **[ RUN ANALYSIS ]** on the morning of the
   meeting. *(This was UX item #7, deferred for lack of a scratch data source.)*
2. **DB integrity isn't verified automatically.** Add a weekly check that
   `COUNT(DISTINCT race_date)` in `results` matches the number of `reports/results_*.json`
   files. A meeting can silently be missing from the form guide if a sync fails.
3. **Cloud-persistence verification is manual.** If the GitHub sync fails, a reboot can
   lose data. Glance at the sidebar persistence panel after each session.
4. **Account-statement reconciliation drifts.** Bank P/L (`acctstmt`) vs **My Bets**
   should be reconciled every meeting, or discrepancies pile up.
5. **Calibration cadence is undefined.** Probabilities should be re-calibrated on a fixed
   schedule (monthly), not ad hoc — drift erodes the **Edge** column's reliability.
6. **Going strings must be exact.** Free-text Turf/AWT going fields are easy to mistype;
   a wrong string breaks going-specific form filtering. Rain-affected meetings
   (Good-to-Yielding … Heavy) are routine — record the exact string every time.
7. **Trials lag.** First-starters have no model signal until trials are scraped; easy to
   forget before a maiden-heavy card.
8. **No backup of `hkjc.db` / reports outside Git.** Consider a periodic copy to a
   second location.
9. **Stale dashboard process.** After deploying changes, restart with the *kill-port-first*
   procedure in §0 and confirm the job log line — a silent stale process serves old code.

---

## 4. Key files & scripts (reference)

| File | Role |
|---|---|
| `dashboard.py` | The Streamlit app (all pages). |
| `agent_skills.py` | Shared runtime for the skills (single source of truth for UI + Copilot). |
| `run_meeting.py` | Pre-race pipeline driver (scrape → SARR → ET / vet / form-guide). |
| `scrape_hkjc_racecard.py` | New-format racecard scraper → `racecards/racecard_YYYYMMDD.xlsx`. |
| `build_form_guide.py` | Builds the form-guide cache from `hkjc.db`. |
| `form_screener.py` | Manual form-screen / head-to-head review. |
| `db_utils.py` | `hkjc.db` read/append helpers (`append_results_to_db`, `read_sqlite`). |
| `parse_acct_statement.py` | Parses `acctstmt (DD Month).txt` into bet/P-L records. |
| `reports/results_YYYYMMDD.json` | Scraped finishing positions per meeting. |
| `reports/race_day_report_*_v4.4.json` | ET model output per meeting. |

> Tip: the **🤖 Agent Skills** page runs the same routines as Copilot, so you can trigger
> the pre-race run, post-race pipeline, scrape-doctor, form-screener, and memory-curator
> without leaving the browser.
