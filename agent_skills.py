"""
agent_skills.py — shared runtime for the VS Code agent skills AND the live
Streamlit dashboard's *Agent Skills* page.

Each public function corresponds 1:1 with a SKILL.md under
`.github/prompts/skills/<name>/SKILL.md`. The single source of truth is here;
the dashboard buttons and the agent both call into these functions.

Design notes
------------
- All subprocess calls go through ``build_python_cmd`` so the hard rules
  (correct interpreter, UTF-8 encoding) are enforced in one place.
- Functions return *structured dicts* (never just stdout strings) so the
  dashboard can render them as tables/metrics and the agent can reason
  over them programmatically.
- No Streamlit imports — this module is UI-agnostic.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Hard rules
# ---------------------------------------------------------------------------

PYTHON_EXE = r"C:\Users\tbhbr\miniconda3\python.exe"
REPO_ROOT = Path(__file__).resolve().parent
RACECARD_DIR = REPO_ROOT / "racecards"
REPORTS_DIR = REPO_ROOT / "reports"
CACHE_DIR = REPO_ROOT / "cache"


def _utf8_env() -> dict[str, str]:
    """Return os.environ overlaid with PYTHONIOENCODING=utf-8."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# ---------------------------------------------------------------------------
# Skill: python-env-enforcer
# ---------------------------------------------------------------------------

def build_python_cmd(script: str, args: list[str] | None = None,
                     *, tail: int | None = None) -> str:
    """Return the exact PowerShell command that should be used to run *script*.

    Used both by the agent (to print the command) and by dashboard buttons
    (to show the user exactly what will execute).
    """
    args_str = " ".join(args or [])
    cmd = f'$env:PYTHONIOENCODING="utf-8"; & \'{PYTHON_EXE}\' {script} {args_str}'.strip()
    if tail:
        cmd += f" 2>&1 | Select-Object -Last {tail}"
    return cmd


def run_python(script: str | Path, args: list[str] | None = None,
               *, timeout: int = 1800,
               cwd: Path | None = None) -> dict[str, Any]:
    """Run a python script with the enforced interpreter and capture output.

    Returns ``{"returncode": int, "stdout": str, "stderr": str, "cmd": list}``.
    """
    cmd = [PYTHON_EXE, str(script)] + list(args or [])
    proc = subprocess.run(
        cmd,
        cwd=str(cwd or REPO_ROOT),
        env=_utf8_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "cmd": cmd,
    }


# ---------------------------------------------------------------------------
# Skill: hkjc-prerace-meeting-run
# ---------------------------------------------------------------------------

@dataclass
class PreraceResult:
    date_iso: str
    returncode: int
    pdf_path: str | None = None
    txt_path: str | None = None
    warnings: list[str] = field(default_factory=list)
    tail_stdout: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _racecard_path(date_iso: str) -> Path:
    yyyymmdd = date_iso.replace("-", "")
    return RACECARD_DIR / f"racecard_{yyyymmdd}.xlsx"


def _auto_skip_scrape(date_iso: str, max_age_hours: float = 12.0) -> bool:
    p = _racecard_path(date_iso)
    if not p.exists():
        return False
    age_h = (datetime.now().timestamp() - p.stat().st_mtime) / 3600.0
    return age_h < max_age_hours


def run_prerace_meeting(date_iso: str,
                        *,
                        going_turf: str = "Good",
                        going_awt: str = "Good",
                        skip_scrape: bool | None = None,
                        skip_sarr: bool = False,
                        timeout: int = 1800) -> PreraceResult:
    """Run the full pre-race pipeline for one meeting."""
    if skip_scrape is None:
        skip_scrape = _auto_skip_scrape(date_iso)

    args: list[str] = ["--date", date_iso,
                       "--going-turf", going_turf,
                       "--going-awt", going_awt]
    if skip_scrape:
        args.append("--skip-scrape")
    if skip_sarr:
        args.append("--skip-sarr")

    res = run_python("run_meeting.py", args, timeout=timeout)
    tail = "\n".join(res["stdout"].splitlines()[-60:])

    pdf_match = re.search(r"reports[\\/]race_day_(\d{8})_v[\d.]+\.pdf", res["stdout"])
    txt_match = re.search(r"reports[\\/]race_day_analysis_(\d{8})_v[\d.]+[_A-Z]*\.txt",
                          res["stdout"])

    warnings: list[str] = []
    if res["returncode"] != 0:
        warnings.append(f"non-zero exit code: {res['returncode']}")
    if "Traceback" in res["stdout"] or "Traceback" in res["stderr"]:
        warnings.append("Traceback in output — inspect stderr")
    if "PermissionError" in res["stdout"]:
        warnings.append("PermissionError detected — see hkjc-scrape-doctor")

    return PreraceResult(
        date_iso=date_iso,
        returncode=res["returncode"],
        pdf_path=pdf_match.group(0) if pdf_match else None,
        txt_path=txt_match.group(0) if txt_match else None,
        warnings=warnings,
        tail_stdout=tail,
    )


# ---------------------------------------------------------------------------
# Skill: hkjc-postrace-pipeline
# ---------------------------------------------------------------------------

@dataclass
class PostraceResult:
    date_iso: str
    db_rows_appended: int = 0
    form_guides_rebuilt: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    smap_diag_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rebuild_form_guide(date_iso: str) -> bool:
    """Rebuild cache/form_guide_<date>.json. Returns True on success."""
    try:
        # Local import — build_form_guide may import heavy deps.
        import build_form_guide  # type: ignore

        if hasattr(build_form_guide, "build_for_date"):
            build_form_guide.build_for_date(date_iso)
            return True
    except Exception:
        pass
    # Fallback: shell out
    res = run_python("build_form_guide.py", ["--date", date_iso], timeout=600)
    return res["returncode"] == 0


def run_postrace_pipeline(date_iso: str) -> PostraceResult:
    """Append results, rebuild upcoming form guides, run pace diagnostic."""
    yyyymmdd = date_iso.replace("-", "")
    out = PostraceResult(date_iso=date_iso)

    # 1. Belt-and-braces DB append.
    results_json = REPORTS_DIR / f"results_{yyyymmdd}.json"
    if not results_json.exists():
        out.warnings.append(f"missing {results_json.name} — results scrape likely failed")
    else:
        try:
            from db_utils import append_results_to_db  # type: ignore
            n = append_results_to_db(results_json, verbose=False)
            out.db_rows_appended = int(n or 0)
            if out.db_rows_appended == 0:
                out.warnings.append("0 rows appended — already in DB, or results JSON is empty; verify the scrape completed")
        except Exception as exc:  # pragma: no cover
            out.warnings.append(f"append_results_to_db failed: {exc!r}")

    # 2. Rebuild form-guide caches for meetings in the next 14 days.
    if RACECARD_DIR.exists():
        today = datetime.strptime(date_iso, "%Y-%m-%d").date()
        for rc in sorted(RACECARD_DIR.glob("racecard_*.xlsx")):
            m = re.match(r"racecard_(\d{8})\.xlsx$", rc.name)
            if not m:
                continue
            try:
                d = datetime.strptime(m.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            if today <= d <= today + timedelta(days=14):
                iso = d.isoformat()
                if _rebuild_form_guide(iso):
                    out.form_guides_rebuilt.append(iso)
                else:
                    out.warnings.append(f"form-guide rebuild failed for {iso}")

    # 3. Pace diagnostic (non-blocking).
    smap_script = REPO_ROOT / "smap_postrace_diagnostic.py"
    if smap_script.exists():
        res = run_python(smap_script, [yyyymmdd], timeout=300)
        if res["returncode"] == 0:
            out.smap_diag_path = f"reports/smap_diag_{yyyymmdd}.txt"
        else:
            out.warnings.append("smap diagnostic non-zero exit (non-blocking)")

    return out


# ---------------------------------------------------------------------------
# Skill: hkjc-scrape-doctor
# ---------------------------------------------------------------------------

def diagnose_scrape(date_iso: str) -> dict[str, Any]:
    """Inspect scrape artifacts and suggest a remediation action."""
    yyyymmdd = date_iso.replace("-", "")
    rc = _racecard_path(date_iso)
    report: dict[str, Any] = {
        "date_iso": date_iso,
        "racecard_xlsx_path": str(rc),
        "racecard_exists": rc.exists(),
        "racecard_size_kb": round(rc.stat().st_size / 1024, 1) if rc.exists() else 0.0,
        "field_size_per_race": {},
        "standby_contamination": [],
        "suggested_action": "",
    }

    if not rc.exists():
        report["suggested_action"] = (
            "Racecard missing. Run scrape_hkjc_racecard.py with "
            "--horse-cache $env:TEMP\\horse_cache_hkjc.json --no-cache. "
            "If HKJC returns 404 for this date, no meeting is scheduled."
        )
        return report

    if report["racecard_size_kb"] < 5:
        report["suggested_action"] = (
            "Racecard exists but is suspiciously small (<5 KB). "
            "Delete and re-scrape with --no-cache."
        )
        return report

    # Per-race field size + standby detection
    try:
        import pandas as pd  # type: ignore
        xls = pd.ExcelFile(rc)
        for sheet in xls.sheet_names:
            df = pd.read_excel(rc, sheet_name=sheet)
            if "race_no" in df.columns:
                for race_no, sub in df.groupby("race_no"):
                    report["field_size_per_race"][int(race_no)] = int(len(sub))
                    if "jockey" in sub.columns:
                        no_jockey = sub[sub["jockey"].isna()]
                        if "horse" in no_jockey.columns:
                            report["standby_contamination"].extend(
                                no_jockey["horse"].dropna().astype(str).tolist()
                            )
    except Exception as exc:
        report["warnings"] = [f"could not parse racecard xlsx: {exc!r}"]

    if report["standby_contamination"]:
        report["suggested_action"] = (
            f"{len(report['standby_contamination'])} horses appear to be "
            "reserve/standby (no jockey assigned). Filter them out before "
            "running pre-race analysis."
        )
    elif not report["suggested_action"]:
        report["suggested_action"] = "Racecard looks healthy."

    return report


# ---------------------------------------------------------------------------
# Skill: memory-curator
# ---------------------------------------------------------------------------

def curate_session_memory(notes: str, *, scope: str = "repo") -> dict[str, Any]:
    """Propose (do NOT write) a memory diff for the agent or user to confirm.

    Scopes: ``user`` → /memories/, ``session`` → /memories/session/,
    ``repo`` → /memories/repo/.
    """
    scope = scope.lower().strip()
    if scope not in {"user", "session", "repo"}:
        raise ValueError(f"unknown scope {scope!r}")

    return {
        "scope": scope,
        "target_dir": {
            "user": "/memories/",
            "session": "/memories/session/",
            "repo": "/memories/repo/",
        }[scope],
        "proposal": notes.strip(),
        "next_action": (
            "Agent: use the `memory` tool with command=create or str_replace. "
            "Dashboard: copy the proposal to clipboard and review before writing."
        ),
    }


# ---------------------------------------------------------------------------
# Registry — used by the dashboard's *Agent Skills* page
# ---------------------------------------------------------------------------

SKILLS = [
    {
        "id": "hkjc-prerace-meeting-run",
        "title": "🏁 Pre-Race Meeting Run",
        "summary": "Scrape → form guide → SARR → pace → speedmap → PDF+txt report.",
        "skill_md": ".github/prompts/skills/hkjc-prerace-meeting-run/SKILL.md",
    },
    {
        "id": "hkjc-postrace-pipeline",
        "title": "🏆 Post-Race Pipeline",
        "summary": "Results → DB append (belt-and-braces) → rebuild upcoming form guides → pace diag.",
        "skill_md": ".github/prompts/skills/hkjc-postrace-pipeline/SKILL.md",
    },
    {
        "id": "python-env-enforcer",
        "title": "🐍 Python Env Enforcer",
        "summary": "Generate the canonical PowerShell command for any script.",
        "skill_md": ".github/prompts/skills/python-env-enforcer/SKILL.md",
    },
    {
        "id": "hkjc-scrape-doctor",
        "title": "🩺 Scrape Doctor",
        "summary": "Diagnose racecard/scrape problems and suggest a remediation.",
        "skill_md": ".github/prompts/skills/hkjc-scrape-doctor/SKILL.md",
    },
    {
        "id": "memory-curator",
        "title": "🧠 Memory Curator",
        "summary": "Propose a tidy memory diff (user/session/repo) for review.",
        "skill_md": ".github/prompts/skills/memory-curator/SKILL.md",
    },
    {
        "id": "hkjc-form-screener",
        "title": "🔍 Form Screener",
        "summary": "Per-horse form review: conditions, collateral form, weight-swing vs rivals + 9 extras.",
        "skill_md": ".github/prompts/skills/hkjc-form-screener/SKILL.md",
    },
]


# ---------------------------------------------------------------------------
# Skill: hkjc-form-screener (delegates to form_screener module)
# ---------------------------------------------------------------------------

def run_form_screener(date_iso: str, *, rebuild: bool = False) -> dict[str, Any]:
    """Thin wrapper so callers don't need to import form_screener directly."""
    import form_screener  # local import — heavy deps (sqlite, pandas)
    return form_screener.run_screener(date_iso, rebuild=rebuild)
