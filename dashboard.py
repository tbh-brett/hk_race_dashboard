#!/usr/bin/env python3
"""
HKJC Race Day Dashboard — dashboard.py
========================================
Streamlit dashboard displaying model picks for upcoming HKJC meetings.

Launch:
    streamlit run dashboard.py

Features:
  - Browse all analysed meetings from reports/*.json
  - Top 4 picks per race with colour-coded risk tiers
  - Full field view (expandable)
  - "Run New Meeting" button to trigger the orchestrator
  - Auto-refresh when new results arrive
  - Backtest page: Weekly / Monthly / Seasonal model accuracy
"""
from __future__ import annotations

import io
import json
import os
import re
import base64
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

import pandas as pd
import requests as _requests
import streamlit as st

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
PYTHON = sys.executable
BLACKBOOK_FILE = BASE / "blackbook.json"


# ── Per-run commentary lookup (for Form Guide rows) ──────────────────────────
def _hkjc_video_url(date_dc: str, race_no: int) -> str:
    return (
        "https://racing.hkjc.com/contentAsset/videoplayer_v4/"
        "video-player-iframe_v4.html?type=replay-full"
        f"&date={date_dc}&no={int(race_no):02d}&lang=eng"
        "&noPTbar=false&noLeading=false&videoParam=PAD"
    )


@st.cache_data(show_spinner=False)
def _load_commentary(date_dc: str) -> dict:
    """Load commentary_YYYYMMDD.json once per date (cached). Returns
    dict keyed by (race_number, HORSE_NAME_UPPER) → {short, tags, incident_text}."""
    path = REPORTS / f"commentary_{date_dc}.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for race in raw.get("races", []):
        rn = race.get("race_number")
        for h in race.get("horses", []):
            key = (rn, (h.get("horse_name") or "").upper())
            out[key] = {
                "short": h.get("short") or "",
                "tags": h.get("tags") or [],
                "incident_text": h.get("incident_text") or "",
            }
    return out


def _run_commentary_lookup(date_dc: str, race_no: int, horse_name: str) -> dict:
    """Return {'short', 'tags', 'incident_text'} for a given past run, or empty."""
    if not date_dc or not race_no or not horse_name:
        return {}
    cm = _load_commentary(date_dc)
    return cm.get((int(race_no), horse_name.upper()), {})

# ── Playwright browser pre-install (Streamlit Cloud has no post-install hook) ─
def _ensure_playwright_chromium():
    """Install Playwright Chromium once per container boot (cached in session)."""
    if st.session_state.get("_pw_checked"):
        return
    st.session_state["_pw_checked"] = True
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
    except Exception:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, timeout=180,
        )

try:
    _ensure_playwright_chromium()
except Exception:
    pass  # Non-critical — scraper will retry at runtime

DEFAULT_EXPIRY_DAYS = 90
CEILING_EXPIRY_DAYS = 45

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="HKJC Race Analysis Dashboard",
    page_icon="▶",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Styling ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* ══ GLOBAL TYPOGRAPHY — Figtree ══ */
    @import url('https://fonts.googleapis.com/css2?family=Figtree:ital,wght@0,300..900;1,300..900&display=swap');
    html, body, .stApp, .stMarkdown, .stDataFrame,
    [data-testid="stMarkdownContainer"],
    [data-testid="stSidebarContent"] {
        font-family: 'Figtree', sans-serif !important;
    }
    [data-testid="stMarkdownContainer"] p,
    [data-testid="stMarkdownContainer"] li { font-size: 1.0em; }

    /* ══ SIDEBAR SHELL ══ */
    [data-testid="stSidebar"] { border-right: 1px solid rgba(230,57,70,0.25); }
    [data-testid="stSidebarContent"] { padding-top: 1rem; }

    /* ── Sidebar brand header ── */
    .sb-brand {
        display: flex; align-items: center; gap: 10px;
        padding: 10px 14px 16px;
        border-bottom: 1px solid rgba(128,128,128,0.2);
        margin-bottom: 10px;
    }
    .sb-brand-icon {
        width: 32px; height: 32px; border-radius: 6px;
        background: #e63946; display: flex; align-items: center;
        justify-content: center; font-size: 15px; font-weight: 700;
        color: #fff; letter-spacing: -0.03em; flex-shrink: 0;
    }
    .sb-brand-text { font-size: 0.98em; font-weight: 700; letter-spacing: 0.04em; }
    .sb-brand-sub  { font-size: 0.72em; opacity: 0.45; margin-top: 1px; }

    /* ── Sidebar nav items via button overrides ── */
    [data-testid="stSidebarContent"] .stButton > button {
        font-family: 'Figtree', sans-serif !important;
        text-align: left !important;
        justify-content: flex-start !important;
        background: transparent !important;
        border: none !important;
        border-radius: 5px !important;
        padding: 5px 12px !important;
        font-size: 0.88em !important;
        font-weight: 400 !important;
        color: var(--text-color) !important;
        width: 100% !important;
        letter-spacing: 0.03em;
        transition: background 0.12s;
    }
    [data-testid="stSidebarContent"] .stButton > button:hover {
        background: rgba(230,57,70,0.1) !important;
        color: #e63946 !important;
    }
    /* Active nav item — injected via a wrapper div with class sb-active */
    .sb-active button {
        background: rgba(230,57,70,0.15) !important;
        color: #e63946 !important;
        font-weight: 700 !important;
        border-left: 3px solid #e63946 !important;
        padding-left: 9px !important;
    }
    .sb-nav-section {
        font-size: 0.65em; letter-spacing: 0.12em; opacity: 0.4;
        text-transform: uppercase; padding: 12px 12px 5px;
    }
    .sb-divider {
        border: none; border-top: 1px solid rgba(128,128,128,0.18);
        margin: 10px 0;
    }

    /* ══ METRIC CARDS ══ */
    div[data-testid="stMetric"] {
        background: var(--secondary-background-color, rgba(128,128,128,0.06));
        border: 1px solid rgba(230,57,70,0.3);
        border-radius: 6px; padding: 10px 14px;
    }
    div[data-testid="stMetric"] label {
        font-size: 0.75em; opacity: 0.55; letter-spacing: 0.07em;
        text-transform: uppercase;
    }
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        font-size: 1.5em; font-weight: 700;
    }

    /* ══ RACE TAB BUTTONS (Race Day + Results) ══ */
    .race-tab-row { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 12px; }
    .rtab {
        font-family: 'Figtree', sans-serif;
        font-size: 0.82em; font-weight: 400; letter-spacing: 0.04em;
        padding: 5px 12px; border-radius: 4px; cursor: pointer;
        border: 1px solid rgba(128,128,128,0.3);
        background: transparent; color: inherit;
        transition: all 0.1s;
    }
    .rtab:hover  { border-color: #e63946; color: #e63946; }
    .rtab.active { background: #e63946; color: #fff; border-color: #e63946; font-weight: 700; }

    /* ══ RACE HEADER BLOCK ══ */
    .race-hdr-block {
        background: var(--secondary-background-color, rgba(128,128,128,0.06));
        border-left: 3px solid #e63946;
        border-radius: 0 6px 6px 0;
        padding: 10px 16px; margin-bottom: 10px;
    }
    .race-hdr-title { font-size: 1.15em; font-weight: 700; letter-spacing: 0.04em; margin-bottom: 4px; }
    .race-hdr-meta  { font-size: 0.82em; opacity: 0.65; display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }

    /* Pace bar */
    .pace-bar-wrap { display: flex; align-items: center; gap: 7px; }
    .pace-bar-track {
        width: 72px; height: 6px; border-radius: 3px;
        background: rgba(128,128,128,0.2); overflow: hidden;
    }
    .pace-bar-fill { height: 100%; border-radius: 3px; }
    .pace-fast   { background: #ef4444; }
    .pace-neutral{ background: #f59e0b; }
    .pace-slow   { background: #22c55e; }
    .pace-label  { font-size: 0.8em; font-weight: 700; }
    .pace-label.fast   { color: #ef4444; }
    .pace-label.neutral{ color: #f59e0b; }
    .pace-label.slow   { color: #22c55e; }

    /* ══ FIELD TOGGLE ══ */
    .field-toggle {
        display: inline-flex; gap: 0; border-radius: 5px;
        overflow: hidden; border: 1px solid rgba(128,128,128,0.3);
        margin: 8px 0 10px 0; font-family: 'Figtree', sans-serif;
    }
    .ft-btn {
        padding: 5px 14px; font-size: 0.8em; letter-spacing: 0.04em;
        cursor: pointer; border: none; background: transparent;
        font-family: inherit; font-weight: 400;
    }
    .ft-btn + .ft-btn { border-left: 1px solid rgba(128,128,128,0.3); }
    .ft-btn.on { background: rgba(230,57,70,0.18); color: #e63946; font-weight: 700; }

    /* ══ RISK / STATUS COLOURS ══ */
    .risk-low  { color: #22c55e; font-weight: 700; }
    .risk-med  { color: #f59e0b; font-weight: 700; }
    .risk-high { color: #ef4444; font-weight: 700; }
    .bt-good   { color: #22c55e; font-weight: 700; }
    .bt-ok     { color: #f59e0b; font-weight: 700; }
    .bt-poor   { color: #ef4444; font-weight: 700; }

    /* ══ FLAG BADGES ══ */
    .flag-tag {
        display: inline-block; padding: 2px 7px; margin: 1px 2px;
        border-radius: 3px; font-size: 0.78em; font-weight: 700;
        letter-spacing: 0.04em;
    }
    .flag-inc  { background: #fef3c7; color: #92400e; }
    .flag-imp  { background: #d1fae5; color: #065f46; }
    .flag-dec  { background: #fee2e2; color: #991b1b; }
    .flag-u    { background: #e0e7ff; color: #3730a3; }
    .flag-sg   { background: #f3e8ff; color: #6b21a8; }

    /* ══ BACKTEST CARD ══ */
    .bt-card {
        background: var(--secondary-background-color, rgba(128,128,128,0.06));
        border: 1px solid rgba(128,128,128,0.18);
        border-radius: 8px; padding: 16px; margin-bottom: 12px;
    }

    /* ══ FORM GUIDE: horse header ══ */
    .horse-header {
        display: flex; align-items: center; gap: 10px;
        padding: 7px 12px;
        background: var(--secondary-background-color, rgba(128,128,128,0.07));
        border-left: 3px solid #e63946;
        border-radius: 0 5px 5px 0;
        margin: 14px 0 3px 0;
        font-family: 'Figtree', sans-serif;
    }
    /* Golden '+' Blackbook button in form guide */
    .fg-bb-col button {
        background: var(--secondary-background-color, rgba(128,128,128,0.07)) !important;
        border: none !important; border-radius: 0 5px 5px 0 !important;
        color: #e63946 !important; font-size: 1.3em !important;
        font-weight: 700 !important; opacity: 0.45;
        min-height: 42px !important; width: 100% !important;
        transition: opacity 0.15s, background 0.15s;
    }
    .fg-bb-col button:hover {
        opacity: 1 !important; background: rgba(230,57,70,0.12) !important;
    }


    .h-num  { font-size: 1.05em; font-weight: 700; color: #e63946; min-width: 28px; }
    .h-name { font-size: 1.1em;  font-weight: 700; letter-spacing: 0.03em; }
    .h-sep  { opacity: 0.3; }
    .h-meta { font-size: 0.92em; opacity: 0.75; }
    .h-l6   { display: flex; align-items: center; gap: 3px; margin-left: auto; }
    .h-l6-label { font-size: 0.78em; opacity: 0.55; margin-right: 4px; }
    .l6b {
        display: inline-flex; align-items: center; justify-content: center;
        min-width: 20px; height: 20px; border-radius: 3px;
        font-size: 0.82em; font-weight: 700; padding: 0 3px;
    }
    .l6-1  { background: #e63946; color: #fff; }
    .l6-2  { background: #1D9E75; color: #fff; }
    .l6-3  { background: #0F6E56; color: #d1fae5; }
    .l6-45 { background: rgba(128,128,128,0.18); }
    .l6-x  { background: rgba(128,128,128,0.08); opacity: 0.55; }

    /* ══ FORM GUIDE: history table ══ */
    .form-tbl {
        width: 100%; border-collapse: collapse;
        font-family: 'Figtree', sans-serif;
        font-size: 0.93em; margin-bottom: 4px;
    }
    .form-tbl th {
        font-size: 0.72em; text-transform: uppercase;
        letter-spacing: 0.08em; opacity: 0.5;
        padding: 4px 8px 5px; text-align: center;
        border-bottom: 1px solid rgba(128,128,128,0.25);
        font-weight: 400; white-space: nowrap;
    }
    .form-tbl th.th-left { text-align: left; }
    .form-tbl td {
        padding: 5px 8px; text-align: center;
        vertical-align: middle; line-height: 1.4; border-bottom: none;
    }
    .form-tbl td.td-left  { text-align: left; }
    .form-tbl td.td-pos   { white-space: nowrap; letter-spacing: -0.02em; }
    .form-tbl tr.top5-row td {
        border-bottom: 1px solid rgba(128,128,128,0.15);
        text-align: left; padding: 1px 8px 7px 2em;
        font-size: 0.9em; opacity: 0.82; line-height: 1.6;
    }
    .form-tbl tr.run-note-row td {
        border-bottom: 1px solid rgba(128,128,128,0.15);
        text-align: left; padding: 1px 8px 6px 2em;
        font-size: 0.85em; opacity: 0.78; line-height: 1.45;
        font-style: italic;
    }
    .form-tbl tr.run-note-row a.vid-link {
        color: #1f6feb; text-decoration: none; font-style: normal;
        font-weight: 600; margin-right: 8px;
    }
    .form-tbl tr.run-note-row a.vid-link:hover { text-decoration: underline; }
    .t5-entry { display: inline-block; min-width: 18%; box-sizing: border-box; }
    .form-margin { }
    .frac { font-feature-settings: 'frac'; }
    /* Place badges */
    .pl-badge {
        display: inline-flex; align-items: center; justify-content: center;
        min-width: 22px; height: 20px; border-radius: 3px;
        font-size: 0.88em; font-weight: 700; padding: 0 4px;
    }
    .pl-1  { background: #e63946; color: #fff; }
    .pl-2  { background: #1D9E75; color: #fff; }
    .pl-3  { background: #0F6E56; color: #d1fae5; }
    .pl-45 { background: rgba(128,128,128,0.18); }
    .pl-x  { opacity: 0.6; }
    .t5-self { color: #e63946 !important; font-weight: 700 !important; }

    /* ══ SECTION DIVIDER ══ */
    .term-divider {
        border: none; border-top: 1px solid rgba(128,128,128,0.18); margin: 14px 0;
    }
    /* ══ PAGE TITLE ══ */
    .page-title {
        font-size: 1.2em; font-weight: 700; letter-spacing: 0.06em;
        text-transform: uppercase; opacity: 0.9; margin-bottom: 2px;
        border-bottom: 2px solid #e63946; display: inline-block;
        padding-bottom: 3px;
    }
    .page-subtitle { font-size: 0.8em; opacity: 0.5; margin-bottom: 14px; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# Data loading
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=30)
def load_available_meetings() -> list[dict]:
    """Scan reports/ for JSON result files and return sorted list."""
    meetings = []
    if not REPORTS.exists():
        return meetings
    seen_dates: set[str] = set()
    # Prefer v4.4 when both exist for same date; scan newest version first
    for pattern in ["race_day_report_*_v4.4.json", "race_day_report_*_v3.4.8.json"]:
        for f in sorted(REPORTS.glob(pattern), reverse=True):
            m = re.search(r"race_day_report_(\d{8})_v", f.name)
            if not m:
                continue
            date_str = m.group(1)
            if date_str in seen_dates:
                continue
            seen_dates.add(date_str)
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                version = data.get("model_version", "v3.4.8")
                meetings.append({
                    "file": f,
                    "title": data.get("meeting_title", f.stem),
                    "generated_at": data.get("generated_at", ""),
                    "venue": data.get("meeting_venue", "?"),
                    "n_races": len(data.get("races", [])),
                    "date_str": date_str,
                    "model_version": version,
                })
            except (json.JSONDecodeError, KeyError):
                continue
    meetings.sort(key=lambda m: m["date_str"], reverse=True)
    return meetings


def load_meeting_data(path: Path) -> dict:
    """Load full meeting JSON data."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_sarr_data(date_str: str) -> dict | None:
    """Load SARR JSON report for a given date (YYYYMMDD). Returns None if missing."""
    sarr_path = REPORTS / f"race_day_report_{date_str}_SARR.json"
    if not sarr_path.exists():
        return None
    try:
        with open(sarr_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ── Backtest data loading ─────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def load_backtest_files() -> dict:
    """Load all backtest JSONs. Returns {type: {key: data}}."""
    out = {"meeting": {}, "monthly": {}, "season": {}, "all": None}
    if not REPORTS.exists():
        return out
    for f in sorted(REPORTS.glob("backtest_*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (json.JSONDecodeError, KeyError):
            continue
        name = f.stem
        if name.startswith("backtest_monthly_"):
            key = name.replace("backtest_monthly_", "")
            out["monthly"][key] = data
        elif name.startswith("backtest_season_"):
            key = name.replace("backtest_season_", "")
            out["season"][key] = data
        elif name == "backtest_all":
            out["all"] = data
        elif name.startswith("backtest_"):
            key = name.replace("backtest_", "")
            out["meeting"][key] = data
    return out


def find_results_dates() -> list[str]:
    """Find all results_YYYYMMDD.json dates."""
    dates = []
    for f in sorted(REPORTS.glob("results_*.json")):
        m = re.search(r"results_(\d{8})\.json", f.name)
        if m:
            dates.append(m.group(1))
    return dates


def find_prediction_dates() -> list[str]:
    """Find all prediction JSON dates (any model version)."""
    dates: set[str] = set()
    for pattern in ["race_day_report_*_v4.4.json", "race_day_report_*_v3.4.8.json"]:
        for f in sorted(REPORTS.glob(pattern)):
            m = re.search(r"race_day_report_(\d{8})_v", f.name)
            if m:
                dates.add(m.group(1))
    return sorted(dates)


# ══════════════════════════════════════════════════════════════════════════════
# Blackbook — load / save / helpers
# ══════════════════════════════════════════════════════════════════════════════

# ── GitHub sync for Streamlit Cloud persistence ──────────────────────────────
_GH_REPO = "tbh-brett/hk_race_dashboard"
_GH_BB_PATH = "blackbook.json"


def _gh_headers() -> dict | None:
    """Return GitHub API auth headers, or None if no token configured."""
    token = st.secrets.get("GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
    if not token:
        return None
    return {"Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"}


def _gh_get_file_sha() -> str | None:
    """Get the current SHA of blackbook.json on GitHub (needed for updates)."""
    headers = _gh_headers()
    if not headers:
        return None
    try:
        r = _requests.get(
            f"https://api.github.com/repos/{_GH_REPO}/contents/{_GH_BB_PATH}",
            headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json().get("sha")
    except Exception:
        pass
    return None


def _gh_push_blackbook(content_bytes: bytes) -> bool:
    """Push blackbook.json to GitHub via the Contents API."""
    headers = _gh_headers()
    if not headers:
        return False
    sha = _gh_get_file_sha()
    payload = {
        "message": f"blackbook: auto-sync {date.today().isoformat()}",
        "content": base64.b64encode(content_bytes).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    try:
        r = _requests.put(
            f"https://api.github.com/repos/{_GH_REPO}/contents/{_GH_BB_PATH}",
            headers=headers, json=payload, timeout=15)
        return r.status_code in (200, 201)
    except Exception:
        return False


def _load_blackbook() -> dict:
    if BLACKBOOK_FILE.exists():
        with open(BLACKBOOK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"entries": [], "tag_definitions": {}, "next_id": 1}


def _save_blackbook(bb: dict):
    content = json.dumps(bb, ensure_ascii=False, indent=2)
    # Always write locally (fast, used by current session)
    with open(BLACKBOOK_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    # Sync to GitHub so data survives Streamlit Cloud restarts/redeploys
    _gh_push_blackbook(content.encode("utf-8"))


def _bb_active_entries(bb: dict) -> list[dict]:
    """Return entries with status 'active' and not past expiry."""
    today = date.today().isoformat()
    return [e for e in bb["entries"]
            if e.get("status") == "active"
            and (e.get("expiry_date", "9999-12-31") >= today)]


def _bb_active_lookup(bb: dict) -> dict:
    """Returns {horse_name_upper: entry} for active entries."""
    return {e["horse_name"].upper(): e for e in _bb_active_entries(bb)}


@st.dialog("Add to Blackbook", width="large")
def _fg_bb_dialog(horse_name: str, source_race: str, jockey: str,
                  surface: str, distance: int | str):
    """Modal dialog for adding a horse to blackbook from Form Guide."""
    bb = _load_blackbook()
    tag_defs = bb.get("tag_definitions", {})
    all_tags = sorted(tag_defs.keys())

    # Check for existing active entry
    existing = next(
        (e for e in bb["entries"]
         if e["horse_name"] == horse_name.strip().upper()
         and e["status"] == "active"),
        None,
    )
    if existing:
        st.warning(
            f"**{horse_name.upper()}** already has an active Blackbook entry "
            f"(added {existing['added_date']}, source: {existing.get('source_race', '—')}).\n\n"
            f"Edit it in the Blackbook page instead."
        )
        return

    st.markdown(f"### {horse_name}")
    st.caption(f"From {source_race} · {distance}m {surface}")

    with st.form("fg_bb_add_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            confidence = st.selectbox("Confidence", ["high", "medium", "low"], index=1)
            pref_surface = st.selectbox(
                "Preferred Surface",
                ["Turf", "AWT", None],
                index=0 if str(surface).upper().startswith("T") else (1 if str(surface).upper().startswith("A") else 2),
            )
        with c2:
            dist_text = st.text_input(
                "Preferred Distance(s) (comma-sep)",
                value=str(distance) if distance else "",
            )
        selected_tags = st.multiselect("Tags", all_tags)
        reasoning = st.text_area(
            "Reasoning *", height=120,
            placeholder="e.g. Held up in traffic, only clear last 100m. "
            "Ran 23.4 final sec — top-3 sectional. Watch for better draw.",
        )
        submitted = st.form_submit_button("[ Save to Blackbook ]", type="primary")
        if submitted:
            if not reasoning.strip():
                st.error("Reasoning is required.")
            else:
                dists = []
                if dist_text.strip():
                    dists = [int(d.strip()) for d in dist_text.split(",") if d.strip().isdigit()]
                _bb_add_entry(
                    bb, horse_name, reasoning, selected_tags, confidence,
                    source_race=source_race, preferred_distance=dists,
                    preferred_surface=pref_surface,
                )
                st.toast(f"✓ {horse_name.upper()} added to Blackbook", icon="⭐")
                st.rerun()


def _bb_add_entry(bb: dict, horse_name: str, reasoning: str, tags: list[str],
                  confidence: str, source_race: str = "",
                  preferred_distance: list | None = None,
                  preferred_surface: str | None = None,
                  expiry_days: int | None = None,
                  category: str = "Pre-Race",
                  **_kwargs) -> dict:
    eid = f"bb_{bb['next_id']:04d}"
    bb["next_id"] += 1
    exp_days = expiry_days or (CEILING_EXPIRY_DAYS if "rating_ceiling" in tags else DEFAULT_EXPIRY_DAYS)
    entry = {
        "id": eid,
        "horse_name": horse_name.strip().upper(),
        "added_date": date.today().isoformat(),
        "expiry_date": (date.today() + timedelta(days=exp_days)).isoformat(),
        "status": "active",
        "reasoning": reasoning.strip(),
        "tags": tags,
        "conditions": {
            "preferred_distance": preferred_distance or [],
            "preferred_surface": preferred_surface,
        },
        "confidence": confidence,
        "source_race": source_race,
        "category": category,
        "performances": [],
    }
    bb["entries"].append(entry)
    _save_blackbook(bb)
    return entry


def _bb_update_entry(bb: dict, eid: str, **kwargs):
    for e in bb["entries"]:
        if e["id"] == eid:
            e.update(kwargs)
            break
    _save_blackbook(bb)


def _bb_add_performance(bb: dict, eid: str, perf: dict):
    for e in bb["entries"]:
        if e["id"] == eid:
            e["performances"].append(perf)
            # Auto-extend expiry on partial validation (top-5 finish)
            try:
                place = int(str(perf.get("finish", "99")).strip().rstrip("stndrdth"))
            except ValueError:
                place = 99
            if place <= 5 and e.get("status") == "active":
                exp = date.fromisoformat(e["expiry_date"])
                new_exp = max(exp, date.today() + timedelta(days=45))
                e["expiry_date"] = new_exp.isoformat()
            break
    _save_blackbook(bb)


def _bb_expire_stale(bb: dict):
    """Move past-expiry active entries to expired."""
    today = date.today().isoformat()
    changed = False
    for e in bb["entries"]:
        if e["status"] == "active" and e.get("expiry_date", "9999-12-31") < today:
            e["status"] = "expired"
            changed = True
    if changed:
        _save_blackbook(bb)


def _load_results_json(date_compact: str) -> dict | None:
    p = REPORTS / f"results_{date_compact}.json"
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════════════════════════
# Form Guide — data helpers
# ══════════════════════════════════════════════════════════════════════════════

CACHE_DIR = BASE / "cache"
FORM_COLS = [
    "horse_name", "race_date", "race_number", "race_track", "race_course",
    "going", "race_class", "jockey", "rating", "draw", "running_positions",
    "place", "lbw", "finish_time_seconds", "distance", "actual_weight",
]


def _safe_read_excel(path: Path) -> pd.DataFrame:
    """Read an Excel file, handling OneDrive PermissionError and zip corruption."""
    import zipfile
    for attempt in range(2):
        target = path
        if attempt == 1:
            target = Path(tempfile.gettempdir()) / path.name
            shutil.copy2(path, target)
        try:
            return pd.read_excel(target)
        except (PermissionError, zipfile.BadZipFile):
            if attempt == 0:
                continue
            raise


@st.cache_data(ttl=120)
def _load_form_db() -> pd.DataFrame:
    """Load hkjc_results_updated.xlsx for form guide lookups."""
    db_file = BASE / "hkjc_results_updated.xlsx"
    if not db_file.exists():
        return pd.DataFrame()
    df = _safe_read_excel(db_file)
    keep = [c for c in FORM_COLS if c in df.columns]
    df = df[keep].copy()
    df["race_date"] = pd.to_datetime(df["race_date"]).dt.date
    df["place_num"] = pd.to_numeric(df["place"], errors="coerce")
    df["horse_name_upper"] = df["horse_name"].str.upper().str.strip()
    return df


def _load_racecard_cache(date_iso: str) -> dict | None:
    """Load cached racecard JSON for full horse details."""
    p = CACHE_DIR / f"racecard_{date_iso}.json"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _load_form_guide_cache(date_iso: str) -> dict | None:
    """Load pre-built form guide JSON cache (from build_form_guide.py)."""
    p = CACHE_DIR / f"form_guide_{date_iso}.json"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _build_race_index(form_db: pd.DataFrame) -> dict:
    """Pre-build index: (race_date, race_number) → sorted runners DataFrame."""
    idx = {}
    for key, grp in form_db.groupby(["race_date", "race_number"]):
        sorted_g = grp.sort_values("place_num")
        # Handle dead heats: take runners whose place_num is within the first
        # 5 distinct finishing positions (not just head(5) rows).
        valid = sorted_g.dropna(subset=["place_num"])
        unique_places = sorted(valid["place_num"].unique())[:5]
        top_runners = valid[valid["place_num"].isin(unique_places)]
        top5 = [(int(r["place_num"]), r["horse_name"])
                for _, r in top_runners.iterrows()]
        second = sorted_g[sorted_g["place_num"] == 2]
        margin_2nd = str(second.iloc[0]["lbw"]) if not second.empty else "-"
        idx[key] = {"top5": top5, "margin_2nd": margin_2nd}
    return idx


def _fmt_positions(pos_str) -> str:
    """Format running positions: '8 9 12 12' → '8-9-12-12'."""
    if not pos_str or (isinstance(pos_str, float) and pd.isna(pos_str)):
        return "-"
    parts = str(pos_str).strip().split()
    clean = [p for p in parts if p.strip()]
    return "-".join(clean) if clean else "-"


def _fmt_time(secs) -> str:
    """Convert seconds to MM:SS.SS display."""
    if pd.isna(secs):
        return "-"
    try:
        s = float(secs)
    except (ValueError, TypeError):
        return "-"
    mins = int(s // 60)
    remainder = s - mins * 60
    return f"{mins}:{remainder:05.2f}"


def _fmt_margin(place, lbw, race_idx_entry: dict) -> str:
    """Format margin: winning margin for 1st, LBW for others."""
    try:
        p = int(float(place))
    except (ValueError, TypeError):
        return str(lbw) if lbw and str(lbw) not in ("", "nan") else "-"
    if p == 1:
        m2 = race_idx_entry.get("margin_2nd", "-")
        return m2 if m2 and m2 != "-" else "0"
    lbw_s = str(lbw) if lbw and str(lbw) not in ("", "nan") else "-"
    return lbw_s


def _fmt_top5(top5: list[tuple], current_horse: str) -> str:
    """Top-5 finishers as compact string, current horse marked with *."""
    parts = []
    h_up = current_horse.strip().upper()
    for place, name in top5:
        short = str(name)[:12]
        if str(name).strip().upper() == h_up:
            parts.append(f"{place}.{short}*")
        else:
            parts.append(f"{place}.{short}")
    return ", ".join(parts)


def _fmt_top5_html(top5: list[tuple], current_horse: str) -> str:
    """Top-5 finishers as HTML with bold horse names; current horse in amber."""
    parts = []
    h_up = current_horse.strip().upper()
    for place, name in top5:
        name_str = str(name)
        is_self = name_str.strip().upper() == h_up
        if is_self:
            parts.append(
                f'<span class="t5-entry"><strong class="t5-self">{place}. {name_str}</strong></span>'
            )
        else:
            parts.append(f'<span class="t5-entry">{place}. <strong>{name_str}</strong></span>')
    return " ".join(parts)


def _last6_html(last6_str: str) -> str:
    """Convert '3/4/3/3/11/5' into coloured badge HTML spans."""
    if not last6_str:
        return ""
    nums = [n.strip() for n in str(last6_str).split("/") if n.strip()]
    badges = []
    for n in nums:
        try:
            p = int(n)
        except ValueError:
            p = 99
        if p == 1:
            cls = "l6b l6-1"
        elif p == 2:
            cls = "l6b l6-2"
        elif p == 3:
            cls = "l6b l6-3"
        elif p <= 5:
            cls = "l6b l6-45"
        else:
            cls = "l6b l6-x"
        badges.append(f'<span class="{cls}">{n}</span>')
    return "".join(badges)


_FRAC_RE = re.compile(r'(\d+)[\s\-]+(\d+/\d+)')

def _smart_frac_html(text: str) -> str:
    """Wrap only the fractional part of margins in <span class="frac">.
    '3 1/4' or '1-1/4' → '3 <span class="frac">1/4</span>'
    Pure fractions like '1/4' stay as plain text."""
    t = str(text).strip()
    m = _FRAC_RE.search(t)
    if m:
        return _FRAC_RE.sub(lambda mo: f'{mo.group(1)}<span class="frac">{mo.group(2)}</span>', t)
    return t


def _place_badge_html(place_val: str) -> str:
    """Return HTML badge span for a finishing position."""
    try:
        p = int(place_val)
    except (ValueError, TypeError):
        p = 99
    if p == 1:
        cls = "pl-badge pl-1"
    elif p == 2:
        cls = "pl-badge pl-2"
    elif p == 3:
        cls = "pl-badge pl-3"
    elif p <= 5:
        cls = "pl-badge pl-45"
    else:
        cls = "pl-badge pl-x"
    return f'<span class="{cls}">{place_val}</span>'


# ══════════════════════════════════════════════════════════════════════════════
# Rendering helpers
# ══════════════════════════════════════════════════════════════════════════════

def risk_badge(tier: str, score: float) -> str:
    cls = {"Low": "risk-low", "Medium": "risk-med", "High": "risk-high"}.get(tier, "risk-med")
    return f'<span class="{cls}">{score:.0f} ({tier[0]})</span>'


def flag_badges(flags: list[str]) -> str:
    mapping = {
        "INC": ("INC", "flag-inc"),
        "↑IMP": ("↑IMP", "flag-imp"),
        "↓DEC": ("↓DEC", "flag-dec"),
        "U": ("U", "flag-u"),
        "SG": ("SG", "flag-sg"),
    }
    parts = []
    for f in flags:
        label, cls = mapping.get(f, (f, "flag-inc"))
        parts.append(f'<span class="flag-tag {cls}">{label}</span>')
    return " ".join(parts)


def _load_vet_lookup(date_compact: str) -> dict:
    """Load vet flags from vet_report JSON. Returns {race_number: {horse_no: flag}}."""
    vj = REPORTS / f"vet_report_{date_compact}.json"
    if not vj.exists():
        return {}
    try:
        with open(vj, "r", encoding="utf-8") as f:
            vd = json.load(f)
        lookup = {}
        for rd in vd.get("races", []):
            lookup[rd["race_number"]] = {
                h["horse_no"]: h["max_flag"]
                for h in rd.get("horses", [])
                if h.get("max_flag") in ("RED", "AMBER", "INFO")
            }
        return lookup
    except Exception:
        return {}


def _vet_display(flag: str) -> str:
    """Short vet flag text for table display."""
    return {"RED": "RED", "AMBER": "AMB", "INFO": "INF"}.get(flag, "-")


def render_speed_map(race: dict):
    """Render speed map HTML table above the analysis table."""
    smap = race.get("speed_map")
    if not smap or not smap.get("grid"):
        return

    n_cols = smap["n_cols"]
    n_rows = smap["n_rows"]
    grid = {}
    for h in smap["grid"]:
        grid[(h["col"], h["row"])] = h

    dist = race["distance"]
    pace = race.get("pace", "Normal")

    # Row labels
    row_labels = {1: "RAIL", n_rows: "WIDE"}
    for r in range(2, n_rows):
        row_labels[r] = f"W{r}"

    # Build HTML table
    html = '<div style="margin:0 0 12px 0;overflow-x:auto;">'
    html += (f'<div style="font-weight:700;font-size:0.95em;margin-bottom:6px;">'
             f'Speed Map — {dist}m | Pace: {pace}</div>')
    html += ('<table style="width:100%;border-collapse:collapse;'
             'font-family:Figtree,sans-serif;font-size:0.82em;">')

    # Header row
    html += '<tr>'
    html += ('<td style="background:#2C3E50;color:#fff;font-size:0.85em;'
             'padding:4px 8px;font-weight:600;">← BACK</td>')
    for c in range(2, n_cols):
        html += '<td style="background:#2C3E50;padding:4px 8px;"></td>'
    html += ('<td style="background:#2C3E50;color:#fff;font-size:0.85em;'
             'padding:4px 8px;font-weight:600;text-align:right;">FRONT →</td>')
    html += '<td style="background:#2C3E50;padding:4px 8px;width:48px;"></td>'
    html += '</tr>'

    # Grid rows (WIDE first = row n_rows down to row 1)
    for row in range(n_rows, 0, -1):
        html += '<tr>'
        for col in range(1, n_cols + 1):
            h = grid.get((col, row))
            if h:
                label = f"{h['horse_no']}.{h['horse_name']}"
                if h.get("is_beneficiary"):
                    color = "#1D9E75"
                    weight = "700"
                elif h.get("advantage", 0) < -0.3:
                    color = "#C0392B"
                    weight = "600"
                else:
                    color = "inherit"
                    weight = "400"
                cell_style = (
                    f"padding:6px 8px;border:1px solid rgba(128,128,128,0.15);"
                    f"color:{color};font-weight:{weight};white-space:nowrap;"
                    f"text-align:center;font-size:0.92em;"
                )
                html += f'<td style="{cell_style}">{label}</td>'
            else:
                html += ('<td style="padding:6px 8px;'
                         'border:1px solid rgba(128,128,128,0.08);"></td>')
        # Row label
        lbl = row_labels.get(row, f"W{row}")
        lbl_style = ("padding:6px 8px;font-size:0.78em;font-weight:600;"
                     "opacity:0.5;text-align:center;white-space:nowrap;")
        html += f'<td style="{lbl_style}">{lbl}</td>'
        html += '</tr>'

    html += '</table>'

    # Beneficiaries line
    bens = smap.get("beneficiaries", [])
    if bens:
        ben_html = ' | '.join(
            f'<span style="color:#1D9E75;font-weight:700;">{b["horse_name"]}</span>'
            f' <span style="opacity:0.6;font-size:0.88em;">— {b.get("reason", "")}</span>'
            for b in bens
        )
        html += (f'<div style="font-size:0.82em;margin-top:6px;">'
                 f'★ Beneficiaries: {ben_html}</div>')

    # Speed map legend
    html += ('<div style="font-size:0.76em;margin-top:4px;opacity:0.55;">'
             '<span style="color:#1D9E75;font-weight:700">●</span> Pace beneficiary &nbsp; '
             '<span style="color:#C0392B;font-weight:700">●</span> Disadvantaged by pace &nbsp; '
             '○ Neutral</div>')

    html += '</div>'
    st.markdown(html, unsafe_allow_html=True)


def render_race_card(race: dict, vet_lookup: dict | None = None, show_top: int = 4,
                     bb_lookup: dict | None = None):
    """Render a single race's picks as a terminal-style card with field toggle."""
    picks = race.get("picks", [])
    if not picks:
        st.warning(f"No projections for Race {race['race_number']}")
        return

    # ── Race header ───────────────────────────────────────────────────────
    cls_str = f"Class {race['race_class']}" if race['race_class'] else "Group"
    surface = "AWT" if race["is_awt"] else "Turf"
    pace_html = _pace_bar_html(race.get("pace", "Neutral"), race.get("pace_score", 0.0))
    proj_info = f"{race['projected']}/{race['runners']} projected"

    st.markdown(
        f'<div class="race-hdr-block">'
        f'<div class="race-hdr-title">R{race["race_number"]} &mdash; {race.get("race_name", "")}</div>'
        f'<div class="race-hdr-meta">'
        f'<span>{race["distance"]}m {surface} ({race["race_course"]})</span>'
        f'<span>{cls_str}</span>'
        f'<span>Pace: {pace_html}</span>'
        f'<span style="margin-left:auto;opacity:0.5">{proj_info}</span>'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    # ── Speed map (above analysis table) ──────────────────────────────────
    render_speed_map(race)

    # ── Top / Full field toggle ───────────────────────────────────────────
    toggle_key = f"rd_full_{race['race_number']}"
    if toggle_key not in st.session_state:
        st.session_state[toggle_key] = True  # default: show full field
    show_full = st.session_state[toggle_key]

    col_tog, _ = st.columns([2, 8])
    with col_tog:
        tog_label = "Full Field" if not show_full else "Top 4 Only"
        if st.button(f"[ {tog_label} ]", key=f"tog_{race['race_number']}"):
            st.session_state[toggle_key] = not show_full
            st.rerun()

    _bb = bb_lookup or {}
    _race_vet = (vet_lookup or {}).get(race["race_number"], {})

    # Decide which picks to show
    display_picks = picks if show_full else picks[:show_top]

    rows = []
    bb_alerts = []
    for p in display_picks:
        vf = p.get("vet_flag") or _race_vet.get(p["horse_no"], "")
        bb_entry = _bb.get(p["horse_name"].upper())
        esz_val = p.get("early_speed_z", 0) or 0
        ssi_val = p.get("avg_ssi", None)
        rows.append({
            "Rk": p["rank"],
            "No": p["horse_no"],
            "Horse": p["horse_name"],
            "Draw": p.get("draw", "—") or "—",
            "Wt": p.get("weight", "—") or "—",
            "Style": p.get("style", "?"),
            "BB": "BB" if bb_entry else "",
            "Proj (s)": f"{p['projected_time']:.2f}",
            "Win%": f"{p['win_prob']:.0f}%",
            "ESZ": round(esz_val, 1) if esz_val != 0 else None,
            "Fin Sec": f"{p['proj_final_sec']:.2f}" if p.get("proj_final_sec") else "—",
            "SSI": round(ssi_val, 2) if ssi_val is not None else None,
            "Jockey": p.get("jockey", ""),
            "Eff Resid": f"{p['effective_resid']:+.3f}",
            "Trial": p.get("trial_flag", "") or "—",
            "Vet": _vet_display(vf),
            "Flags": ", ".join(p.get("flags", [])),
        })

    # BB horses outside top picks (only shown in top-4 view)
    if not show_full:
        top_names = {p["horse_name"].upper() for p in picks[:show_top]}
        for p in picks[show_top:]:
            bb_entry = _bb.get(p["horse_name"].upper())
            if bb_entry:
                bb_alerts.append((p, bb_entry))

    df = pd.DataFrame(rows)

    def style_ssi(val):
        try:
            v = float(val)
        except (ValueError, TypeError):
            return ""
        if v >= 0.3: return "color: #ef4444; font-weight: bold"
        elif v >= 0.1: return "color: #ef4444"
        elif v <= -0.3: return "color: #22c55e; font-weight: bold"
        elif v <= -0.1: return "color: #22c55e"
        return ""

    def style_winprob(val):
        pct = float(str(val).replace("%", "")) if "%" in str(val) else 0
        if pct >= 20: return "color: #22c55e; font-weight: bold"
        elif pct >= 12: return "color: #f59e0b"
        return ""

    def style_vet(val):
        v = str(val).strip()
        if v == "RED": return "color: #ef4444; font-weight: bold"
        elif v == "AMB": return "color: #f59e0b; font-weight: bold"
        elif v == "INF": return "color: #3b82f6"
        return ""

    def style_trial(val):
        v = str(val).strip()
        if v == "++": return "color: #22c55e; font-weight: bold"
        elif v == "+": return "color: #22c55e"
        elif v == "--": return "color: #ef4444; font-weight: bold"
        elif v == "-": return "color: #ef4444"
        return ""

    def style_esz(val):
        try:
            v = float(val)
        except (ValueError, TypeError):
            return ""
        if v >= 1.0: return "color: #ef4444; font-weight: bold"
        elif v >= 0.5: return "color: #ef4444"
        elif v <= -1.0: return "color: #22c55e; font-weight: bold"
        elif v <= -0.5: return "color: #22c55e"
        return ""

    styled = df.style.map(style_ssi, subset=["SSI"]) \
                      .map(style_winprob, subset=["Win%"]) \
                      .map(style_vet, subset=["Vet"]) \
                      .map(style_trial, subset=["Trial"]) \
                      .map(style_esz, subset=["ESZ"]) \
                      .format({"ESZ": lambda v: f"{v:+.1f}" if pd.notna(v) else "—",
                               "SSI": lambda v: f"{v:+.2f}" if pd.notna(v) else "—"}) \
                      .set_properties(**{"text-align": "center"}) \
                      .set_properties(subset=["Horse"], **{"text-align": "left", "font-weight": "600"})

    st.dataframe(styled, use_container_width=True, hide_index=True)

    # BB alerts
    for p, bbe in bb_alerts:
        tags_str = ", ".join(bbe.get("tags", []))
        st.info(
            f"[BB] {p['horse_name']} (#{p['horse_no']}) ranked #{p['rank']} by model.  "
            f"Confidence: {bbe.get('confidence', '?')} · Tags: {tags_str}\n\n"
            f"*{bbe.get('reasoning', '')}*"
        )

    st.markdown('<hr class="term-divider">', unsafe_allow_html=True)


def render_sarr_race_card(race: dict, et_race: dict | None = None,
                          bb_lookup: dict | None = None):
    """Render a single race's SARR picks as a terminal-style card."""
    picks = race.get("picks", [])
    if not picks:
        st.warning(f"No SARR projections for Race {race['race_number']}")
        return

    # ── Race header ───────────────────────────────────────────────────────
    cls_str = f"Class {race.get('race_class', '')}" if race.get('race_class') else "Group"
    surface = "AWT" if race.get("is_awt") else "Turf"
    st.markdown(
        f'<div class="race-hdr-block">'
        f'<div class="race-hdr-title">R{race["race_number"]} — {race.get("race_name", "")}'
        f' <span style="opacity:0.5;font-size:0.75em">(SARR)</span></div>'
        f'<div class="race-hdr-meta">'
        f'<span>{race.get("distance", "?")}m {surface} ({race.get("race_course", "")})</span>'
        f'<span>{cls_str}</span>'
        f'<span style="margin-left:auto;opacity:0.5">{race.get("runners", len(picks))} runners</span>'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    # ── Speed map (from ET data if available) ─────────────────────────────
    if et_race:
        render_speed_map(et_race)

    # ── Toggle ────────────────────────────────────────────────────────────
    toggle_key = f"rd_sarr_full_{race['race_number']}"
    if toggle_key not in st.session_state:
        st.session_state[toggle_key] = True
    show_full = st.session_state[toggle_key]

    col_tog, _ = st.columns([2, 8])
    with col_tog:
        tog_label = "Full Field" if not show_full else "Top 4 Only"
        if st.button(f"[ {tog_label} ]", key=f"sarr_tog_{race['race_number']}"):
            st.session_state[toggle_key] = not show_full
            st.rerun()

    _bb = bb_lookup or {}
    display_picks = picks if show_full else picks[:4]

    rows = []
    for p in display_picks:
        bb_entry = _bb.get(p.get("horse_name", "").upper())
        wpr_pct = (p.get("place_rate", 0) or 0) * 100
        rows.append({
            "Rk": p["rank"],
            "No": p.get("horse_no", ""),
            "Horse": p.get("horse_name", ""),
            "Draw": p.get("draw", "—") or "—",
            "Wt": p.get("weight", "—") or "—",
            "Style": p.get("style", "?"),
            "BB": "BB" if bb_entry else "",
            "WPR%": f"{wpr_pct:.0f}%",
            "ESZ": round(p.get("f_esz", 0), 2),
            "SARR": round(p.get("sarr", 0), 3),
            "FMRP": round(p.get("f_fmrp", 0), 2),
            "LSA": round(p.get("f_lsa", 0), 2),
            "SSI": round(p.get("avg_ssi", 0), 2),
            "Traj": round(p.get("f_traj", 0), 3),
            "Late Std": round(p.get("late_std", 0.5), 2),
            "Jockey": p.get("jockey", ""),
        })

    df = pd.DataFrame(rows)

    def _style_sarr(val):
        try: v = float(val)
        except (ValueError, TypeError): return ""
        if v <= -0.30: return "color: #22c55e; font-weight: bold"
        elif v <= -0.10: return "color: #22c55e"
        elif v >= 0.05: return "color: #ef4444"
        return ""

    def _style_fmrp(val):
        try: v = float(val)
        except (ValueError, TypeError): return ""
        if v <= -0.30: return "color: #22c55e; font-weight: bold"
        elif v >= 0.10: return "color: #ef4444"
        return ""

    def _style_lsa_esz(val):
        try: v = float(val)
        except (ValueError, TypeError): return ""
        if v <= -0.15: return "color: #22c55e; font-weight: bold"
        elif v >= 0.15: return "color: #ef4444"
        return ""

    def _style_traj(val):
        try: v = float(val)
        except (ValueError, TypeError): return ""
        if v <= -0.03: return "color: #22c55e; font-weight: bold"
        elif v >= 0.03: return "color: #ef4444"
        return ""

    def _style_wpr(val):
        pct = float(str(val).replace("%", "")) if "%" in str(val) else 0
        if pct >= 40: return "color: #22c55e; font-weight: bold"
        elif pct <= 10: return "color: #ef4444"
        return ""

    def _style_late_std(val):
        try: v = float(val)
        except (ValueError, TypeError): return ""
        if v <= 0.20: return "color: #22c55e; font-weight: bold"
        elif v >= 0.50: return "color: #ef4444"
        return ""

    styled = df.style.map(_style_sarr, subset=["SARR"]) \
                      .map(_style_fmrp, subset=["FMRP"]) \
                      .map(_style_lsa_esz, subset=["LSA", "ESZ"]) \
                      .map(_style_traj, subset=["Traj"]) \
                      .map(_style_wpr, subset=["WPR%"]) \
                      .map(_style_late_std, subset=["Late Std"]) \
                      .format({"SARR": "{:+.3f}", "FMRP": "{:+.2f}",
                               "LSA": "{:+.2f}", "ESZ": "{:+.2f}",
                               "SSI": "{:+.2f}", "Traj": "{:+.3f}"}) \
                      .set_properties(**{"text-align": "center"}) \
                      .set_properties(subset=["Horse"], **{"text-align": "left", "font-weight": "600"})

    st.dataframe(styled, use_container_width=True, hide_index=True)
    st.markdown('<hr class="term-divider">', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline runner
# ══════════════════════════════════════════════════════════════════════════════


def run_pipeline(date_str: str, no_cache: bool, going_turf: str, going_awt: str,
                 model: str = "v4.4", skip_scrape: bool = False):
    """Run the orchestrator from the dashboard."""
    cmd = [PYTHON, str(BASE / "run_meeting.py"), "--date", date_str,
           "--model", model]
    if no_cache:
        cmd.append("--no-cache")
    if skip_scrape:
        cmd.append("--skip-scrape")
    cmd.extend(["--going-turf", going_turf, "--going-awt", going_awt])

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    with st.spinner(f"Running pipeline for {date_str}..."):
        status = st.empty()
        if skip_scrape:
            status.info(f"Running analysis for {date_str} (using uploaded racecard)...")
        else:
            status.info(f"Scraping race card for {date_str}...")

        result = subprocess.run(
            cmd, env=env, cwd=str(BASE),
            capture_output=True, text=True, encoding="utf-8",
            timeout=300,
        )

        if result.returncode == 0:
            st.success(f"ET pipeline complete for {date_str}!")
            with st.expander("ET pipeline output"):
                st.code(result.stdout[-3000:] if len(result.stdout) > 3000
                        else result.stdout)
        else:
            st.error(f"ET pipeline failed (exit code {result.returncode})")
            with st.expander("Error output"):
                st.code(result.stderr[-2000:] if result.stderr else result.stdout[-2000:])

    # ── Run SARR model (independent, always runs after ET) ──
    sarr_script = BASE / "sarr_raceday.py"
    if sarr_script.exists():
        with st.spinner(f"Running SARR analysis for {date_str}..."):
            sarr_cmd = [PYTHON, str(sarr_script), "--date", date_str]
            sarr_result = subprocess.run(
                sarr_cmd, env=env, cwd=str(BASE),
                capture_output=True, text=True, encoding="utf-8",
                timeout=300,
            )
            if sarr_result.returncode == 0:
                st.success(f"SARR analysis complete for {date_str}!")
                with st.expander("SARR output"):
                    st.code(sarr_result.stdout[-2000:] if len(sarr_result.stdout) > 2000
                            else sarr_result.stdout)
            else:
                st.warning(f"SARR analysis failed (non-critical)")
                with st.expander("SARR error"):
                    st.code(sarr_result.stderr[-1500:] if sarr_result.stderr
                            else sarr_result.stdout[-1500:])


def _save_uploaded_racecard(uploaded_json: bytes, date_str: str) -> bool:
    """Save an uploaded racecard cache JSON and regenerate the Excel file.

    Returns True on success.
    """
    import json as _json
    try:
        data = _json.loads(uploaded_json)
    except (ValueError, TypeError) as exc:
        st.error(f"Invalid JSON: {exc}")
        return False

    if "races" not in data or not data["races"]:
        st.error("JSON has no 'races' key or races list is empty.")
        return False

    # Write cache JSON
    cache_dir = BASE / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"racecard_{date_str}.json"
    cache_path.write_text(
        _json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # Regenerate Excel from the JSON data
    racecourse = data.get("racecourse", "")
    race_date = data.get("race_date", date_str)
    all_race_data = []
    for race_entry in data["races"]:
        meta = race_entry.get("meta", {})
        horses = race_entry.get("horses", [])
        all_race_data.append((meta, horses))

    # Build DataFrame (inline — mirrors scrape_hkjc_racecard.normalize_data)
    rows = []
    for race_meta, horse_list in all_race_data:
        for horse in horse_list:
            row = {"race_date": race_date, "racecourse": racecourse}
            row.update(horse)
            rows.append(row)

    if not rows:
        st.error("Uploaded JSON contains no horse data.")
        return False

    df = pd.DataFrame(rows)
    leading_cols = [
        "race_date", "racecourse", "race_number", "race_name", "race_class",
        "distance", "surface", "race_course", "going", "rating_range",
        "horse_no", "horse_name", "horse_id", "brand_no", "draw",
        "jockey", "overweight", "trainer",
        "weight", "rating", "rating_change", "intl_rating",
        "age", "sex", "colour",
        "last_6_runs", "gear", "priority",
        "horse_wt_declaration", "wt_change", "best_time", "wfa",
        "season_stakes", "days_since_last",
        "owner", "sire", "dam", "import_cat",
        "is_standby", "prize", "race_time",
    ]
    present = [c for c in leading_cols if c in df.columns]
    remaining = [c for c in df.columns if c not in present]
    df = df[present + remaining]

    # Write Excel
    rc_dir = BASE / "racecards"
    rc_dir.mkdir(parents=True, exist_ok=True)
    date_compact = date_str.replace("-", "")
    xl_path = rc_dir / f"racecard_{date_compact}.xlsx"
    with pd.ExcelWriter(str(xl_path), engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="All Races", index=False)
        if "race_number" in df.columns:
            for rn in sorted(df["race_number"].dropna().unique()):
                race_df = df[df["race_number"] == rn]
                race_df.to_excel(writer, sheet_name=f"Race {int(rn)}", index=False)

    st.toast(
        f"Racecard saved: {cache_path.name} + {xl_path.name}  "
        f"({len(df)} horses, {df['race_number'].nunique() if 'race_number' in df.columns else '?'} races)",
        icon="\u2705",
    )
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Main page — Race Day view
# ══════════════════════════════════════════════════════════════════════════════

def _pace_bar_html(pace: str, score: float) -> str:
    """Render a compact inline pace bar."""
    pace_lower = (pace or "").lower()
    if "fast" in pace_lower:
        cls, label, pct = "fast", "FAST", min(100, 50 + abs(score) * 25)
    elif "slow" in pace_lower:
        cls, label, pct = "slow", "SLOW", min(100, 50 + abs(score) * 25)
    else:
        cls, label, pct = "neutral", "NEUTRAL", 50
    return (
        f'<span class="pace-bar-wrap">'
        f'<span class="pace-bar-track">'
        f'<span class="pace-bar-fill pace-{cls}" style="width:{pct:.0f}%"></span>'
        f'</span>'
        f'<span class="pace-label {cls}">{label}</span>'
        f'<span style="font-size:0.78em;opacity:0.55">({score:+.2f}s)</span>'
        f'</span>'
    )


# ══════════════════════════════════════════════════════════════════════════════
# Overview page — Home / wagering briefing
# ══════════════════════════════════════════════════════════════════════════════

def _overview_find_today_meeting() -> dict | None:
    """Find the most recent (or today's) meeting report."""
    meetings = load_available_meetings()
    if not meetings:
        return None
    today_str = date.today().strftime("%Y%m%d")
    for m in meetings:
        if m["date_str"] == today_str:
            return m
    return meetings[0]  # fallback to most recent


def page_overview():

    st.markdown('<div class="page-title">Overview</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">Blackbook &middot; Mutual model picks &middot; Trial standouts</div>',
                unsafe_allow_html=True)

    meeting_info = _overview_find_today_meeting()
    if not meeting_info:
        st.info("No meeting reports found yet. Run analysis from the Race Day page.")
        return


    data = load_meeting_data(meeting_info["file"])
    races = data.get("races", [])
    dstr = meeting_info["date_str"]
    nice_date = f"{dstr[:4]}-{dstr[4:6]}-{dstr[6:]}"
    version = meeting_info.get("model_version", data.get("model_version", ""))

    # Load SARR data for this meeting
    sarr_data = load_sarr_data(dstr)
    sarr_races = sarr_data.get("races", []) if sarr_data else []


    st.markdown(f"### {data.get('meeting_title', nice_date)}")
    if version:
        st.caption(f"Model {version}  ·  {len(races)} races")

    # ── Section 1: Blackbooked horses running today ─────────
    bb = _load_blackbook()
    active = _bb_active_lookup(bb)

    # Collect all horse names from today's card
    bb_matches = []
    for race in races:
        for pick in race.get("picks", []):
            hn = pick.get("horse_name", "").upper().strip()
            if hn in active:
                entry = active[hn]
                bb_matches.append({
                    "race": race["race_number"],
                    "dist": race.get("distance", "?"),
                    "horse": pick["horse_name"],
                    "rank": pick.get("rank"),
                    "win_pct": pick.get("win_prob", 0),
                    "confidence": entry.get("confidence", "?"),
                    "reasoning": entry.get("reasoning", ""),
                    "tags": ", ".join(entry.get("tags", [])),
                })
        # Also check speed_map entries (for horses that may not have picks, e.g. R1)
        for sm in race.get("speed_map", {}).get("grid", []):
            hn = sm.get("horse_name", "").upper().strip()
            if hn in active and not any(b["horse"].upper() == hn for b in bb_matches):
                entry = active[hn]
                bb_matches.append({
                    "race": race["race_number"],
                    "dist": race.get("distance", "?"),
                    "horse": sm["horse_name"],
                    "rank": None,
                    "win_pct": 0,
                    "confidence": entry.get("confidence", "?"),
                    "reasoning": entry.get("reasoning", ""),
                    "tags": ", ".join(entry.get("tags", [])),
                })

    st.markdown("---")
    st.markdown("### ★ Blackbooked Horses Running Today")
    if bb_matches:
        bb_matches.sort(key=lambda x: x["race"])
        for bm in bb_matches:
            rk_str = f"Rk#{bm['rank']}" if bm["rank"] else "N/P"
            conf_colour = {"high": "#22c55e", "medium": "#f59e0b", "low": "#ef4444"}.get(
                bm["confidence"], "#888")
            st.markdown(
                f'**R{bm["race"]}** {bm["dist"]}m — '
                f'**{bm["horse"]}** &nbsp; {rk_str} &nbsp; '
                f'Win% {bm["win_pct"]:.1f} &nbsp; '
                f'<span style="color:{conf_colour};font-weight:700">'
                f'● {bm["confidence"].title()}</span>'
                f'{" &nbsp; " + bm["tags"] if bm["tags"] else ""}',
                unsafe_allow_html=True,
            )
            if bm["reasoning"]:
                st.caption(f'  ↳ {bm["reasoning"][:120]}')
    else:
        st.caption("No active blackbook entries match today's card.")


    # ── Section 2: Mutual model top picks ─────────────
    st.markdown("### 🤝 Mutual Model Top Picks (ET ∩ SARR)")
    if sarr_races:
        mutual_rows = []
        for et_race in races:
            rn = et_race["race_number"]
            sarr_race = next((r for r in sarr_races if r["race_number"] == rn), None)
            if not sarr_race:
                continue
            et_top = {p["horse_name"].upper().strip(): p for p in et_race.get("picks", [])[:4]}
            sarr_top = {p["horse_name"].upper().strip(): p for p in sarr_race.get("picks", [])[:4]}
            mutual = set(et_top.keys()) & set(sarr_top.keys())
            for hn in mutual:
                et_pick = et_top[hn]
                sarr_pick = sarr_top[hn]
                et_rk = et_pick["rank"] if et_pick else 99
                sarr_rk = sarr_pick["rank"] if sarr_pick else 99
                horse_no = et_pick.get("horse_no", sarr_pick.get("horse_no", ""))
                # Highlight tier: green = both top 2, amber = both top 3
                if et_rk <= 2 and sarr_rk <= 2:
                    tier = "green"
                elif et_rk <= 3 and sarr_rk <= 3:
                    tier = "amber"
                else:
                    tier = "none"
                mutual_rows.append({
                    "race": rn,
                    "horse_no": horse_no,
                    "horse": hn.title(),
                    "et_rank": et_rk,
                    "sarr_rank": sarr_rk,
                    "et_proj": f"{et_pick['projected_time']:.2f}" if et_pick and 'projected_time' in et_pick else "—",
                    "sarr_val": f"{sarr_pick['sarr']:+.3f}" if sarr_pick and 'sarr' in sarr_pick else "—",
                    "tier": tier,
                })
        if mutual_rows:
            mutual_rows.sort(key=lambda r: (r["race"], r["et_rank"]))
            html_rows = []
            for mr in mutual_rows:
                if mr["tier"] == "green":
                    bg = "rgba(34,197,94,0.18)"
                    name_style = "font-weight:800;color:#22c55e"
                    rk_style = "font-weight:700;color:#22c55e"
                elif mr["tier"] == "amber":
                    bg = "rgba(245,158,11,0.15)"
                    name_style = "font-weight:800;color:#f59e0b"
                    rk_style = "font-weight:700;color:#f59e0b"
                else:
                    bg = "transparent"
                    name_style = "font-weight:600"
                    rk_style = ""
                html_rows.append(
                    f'<tr style="background:{bg}">'
                    f'<td>R{mr["race"]}</td>'
                    f'<td>{mr["horse_no"]}</td>'
                    f'<td style="{name_style}">{mr["horse"]}</td>'
                    f'<td style="{rk_style}">{mr["et_rank"]}</td>'
                    f'<td style="{rk_style}">{mr["sarr_rank"]}</td>'
                    f'<td>{mr["et_proj"]}</td>'
                    f'<td>{mr["sarr_val"]}</td>'
                    f'</tr>'
                )
            st.markdown(
                '<table style="width:100%;border-collapse:collapse;font-size:0.92em">'
                '<thead><tr style="border-bottom:2px solid rgba(128,128,128,0.3)">'
                '<th style="text-align:left;padding:6px">Race</th>'
                '<th style="text-align:left;padding:6px">No</th>'
                '<th style="text-align:left;padding:6px">Horse</th>'
                '<th style="text-align:left;padding:6px">ET Rk</th>'
                '<th style="text-align:left;padding:6px">SARR Rk</th>'
                '<th style="text-align:left;padding:6px">ET Proj</th>'
                '<th style="text-align:left;padding:6px">SARR</th>'
                '</tr></thead><tbody>'
                + "".join(html_rows)
                + '</tbody></table>'
                '<div style="margin-top:6px;font-size:0.78em;opacity:0.6">'
                '🟢 Both top 2 &nbsp; 🟡 Both top 3</div>',
                unsafe_allow_html=True,
            )
        else:
            st.caption("No mutual top-4 picks between ET and SARR.")
    else:
        st.caption("SARR analysis not available for this meeting.")

    st.markdown("---")

    # ── Section 3: Trial standouts running today ──────────
    st.markdown("### 🏇 Trial Standouts Running Today")
    trial_index = _load_all_trial_horse_index()
    if trial_index:
        from datetime import datetime as _dto, timedelta as _td
        cutoff = _dto.now() - _td(days=60)
        card_horses = {}  # UPPER name → {race, dist, rank, wp}
        for race in races:
            for pick in race.get("picks", []):
                hn = pick.get("horse_name", "").upper().strip()
                card_horses[hn] = {
                    "race": race["race_number"],
                    "dist": race.get("distance", "?"),
                    "rank": pick.get("rank"),
                    "wp": pick.get("win_prob", 0),
                }

        trial_hits = []
        for horse, entries in trial_index.items():
            if horse.upper().strip() not in card_horses:
                continue
            recent = []
            for e in entries:
                try:
                    tdt = _dto.strptime(e["date"], "%Y-%m-%d")
                except (ValueError, TypeError):
                    continue
                if tdt >= cutoff:
                    recent.append(e)
            if not recent:
                continue
            recent.sort(key=lambda x: x["date"], reverse=True)
            latest = recent[0]
            comment = (latest.get("comment", "") or "").lower()
            rp = latest.get("running_positions", [])
            fp = rp[-1] if rp else None
            sp = rp[0] if rp else None
            n = latest.get("n_horses", 0)

            is_concealed = (any(kw in comment for kw in _CONCEAL_KW)
                            and not any(nk in comment for nk in _NEG_KW))
            top_half = fp is not None and n > 0 and fp <= (n / 2)
            has_pos = any(p in comment for p in _POS_PHRASES)
            has_eased = "eased" in comment and not any(nk in comment for nk in _NEG_KW)
            has_neg = any(nk in comment for nk in _NEG_KW)
            pos_gained = (sp - fp) if sp is not None and fp is not None else 0
            won_trial = fp == 1 and n and n >= 3

            flag = ""
            details = []
            if is_concealed and top_half:
                flag, details = "++", ["Concealed + top half"]
            elif has_eased and has_pos:
                flag, details = "++", ["Eased + strong finish"]
            elif won_trial and is_concealed:
                flag, details = "++", ["Won trial under hold"]
            elif won_trial and has_pos:
                flag, details = "+", ["Trial winner, positive"]
            elif has_eased:
                flag, details = "+", ["Eased (deliberately held)"]
            elif has_pos and top_half:
                flag, details = "+", ["Positive trial, top half"]
            elif pos_gained >= 3:
                flag, details = "+", [f"Gained {pos_gained} positions"]
            elif is_concealed:
                flag, details = "+", ["Concealed form"]
            elif has_neg:
                flag, details = "—", ["Negative trial signal"]

            if not flag:
                continue
            ch = card_horses[horse.upper().strip()]
            trial_hits.append({
                "race": ch["race"], "dist": ch["dist"], "horse": horse,
                "rank": ch["rank"], "wp": ch["wp"], "flag": flag,
                "detail": "; ".join(details), "date": latest["date"],
            })

        trial_hits.sort(key=lambda x: (0 if x["flag"] == "++" else 1 if x["flag"] == "+" else 2, x["race"]))
        if trial_hits:
            for th in trial_hits:
                flag_colour = {"++": "#22c55e", "+": "#3b82f6", "—": "#ef4444"}.get(th["flag"], "#888")
                rk_str = f"Rk#{th['rank']}" if th["rank"] else ""
                st.markdown(
                    f'**R{th["race"]}** {th["dist"]}m — '
                    f'<span style="color:{flag_colour};font-weight:700">[{th["flag"]}]</span> '
                    f'**{th["horse"]}** &nbsp; {rk_str} '
                    f'Win% {th["wp"]:.1f} &nbsp; '
                    f'<span style="font-size:0.85em;opacity:0.7">{th["detail"]} ({th["date"]})</span>',
                    unsafe_allow_html=True,
                )
        else:
            st.caption("No flagged trial horses on today's card.")
    else:
        st.caption("No trial data loaded.")

    st.markdown("---")

    # ── Section 4: Quick meeting stats ─────────────────────
    st.markdown("### 📊 Meeting Quick Stats")
    col1, col2, col3, col4 = st.columns(4)

    total_proj = sum(len(r.get("picks", [])) for r in races)
    pace_fast = sum(1 for r in races if "Fast" in str(r.get("pace", "")))
    pace_slow = sum(1 for r in races if "Slow" in str(r.get("pace", "")))
    avg_field = sum(r.get("runners", len(r.get("picks", []))) for r in races) / max(len(races), 1)
    n_races = len(races)
    col1.metric("Races", n_races)
    col2.metric("Avg Field Size", f"{avg_field:.1f}")
    col3.metric("Fast Pace", f"{pace_fast} / {n_races}")
    col4.metric("Slow Pace", f"{pace_slow} / {n_races}")

    # ── Section 4: Risk overview ───────────────────────────
    st.markdown("### ⚠️ Risk Flags Summary")
    flag_counts: dict[str, int] = defaultdict(int)
    vet_counts: dict[str, int] = defaultdict(int)
    for race in races:
        for pick in race.get("picks", []):
            for fl in pick.get("flags", []):
                flag_counts[str(fl)] += 1
            vf = pick.get("vet_flag", "")
            if vf:
                vet_counts[vf] += 1

    if flag_counts or vet_counts:
        cols = st.columns(max(len(flag_counts) + len(vet_counts), 1))
        idx = 0
        for fl, cnt in sorted(flag_counts.items(), key=lambda x: -x[1]):
            if idx < len(cols):
                cols[idx].metric(fl, cnt)
                idx += 1
        for vf, cnt in sorted(vet_counts.items(), key=lambda x: -x[1]):
            if idx < len(cols):
                cols[idx].metric(f"Vet: {vf}", cnt)
                idx += 1
    else:
        st.caption("No risk flags in this meeting.")

    # ── Section 5: Recent backtest performance ─────────────
    st.markdown("---")
    st.markdown("### 📈 Recent Model Performance")
    bt_files = load_backtest_files()
    bt_meetings = bt_files.get("meeting", {})
    if bt_meetings:
        recent_keys = sorted(bt_meetings.keys(), reverse=True)[:5]
        bt_rows = []
        for k in recent_keys:
            d = bt_meetings[k]
            metrics = d.get("metrics", d.get("time_accuracy", {}))
            bt_rows.append({
                "Date": f"{k[:4]}-{k[4:6]}-{k[6:8]}",
                "Model": d.get("model_version", "?"),
                "MAE": f'{metrics.get("mae", metrics.get("mae_seconds", "?"))}s',
                "Rank ρ": metrics.get("mean_spearman_rho",
                                      metrics.get("rank_correlation_avg", "?")),
                "Top-1%": f'{100*metrics.get("top1_rate", metrics.get("top1_win_rate", 0)):.0f}%',
                "Top-3%": f'{100*metrics.get("top3_rate", metrics.get("top3_place_rate", 0)):.0f}%',
            })
        st.dataframe(pd.DataFrame(bt_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("No backtests available yet.")


# ══════════════════════════════════════════════════════════════════════════════
# Main page — Race Day view
# ══════════════════════════════════════════════════════════════════════════════

def page_race_day(selected):
    if selected is None:
        st.markdown('<div class="page-title">Race Day Analysis</div>', unsafe_allow_html=True)
        st.info("No meetings available. Use the sidebar to run your first analysis.")
        return

    data = load_meeting_data(selected["file"])

    _date_match = re.search(r"(\d{8})", str(selected["file"]))
    date_str = _date_match.group(1) if _date_match else ""
    vet_lookup = _load_vet_lookup(date_str) if date_str else {}

    bb = _load_blackbook()
    _bb_expire_stale(bb)
    bb_lookup = _bb_active_lookup(bb)

    et_races = data.get("races", [])

    # ── Load SARR data for same date ─────────────────────────────────────
    sarr_data = load_sarr_data(date_str) if date_str else None
    sarr_races = sarr_data.get("races", []) if sarr_data else []
    sarr_available = bool(sarr_races)

    # ── Page header ──────────────────────────────────────────────────────
    st.markdown(
        f'<div class="page-title">{data["meeting_title"]}</div>'
        f'<div class="page-subtitle">Model {data.get("model_version", "v3.4.8")} '
        f'&nbsp;·&nbsp; Generated {data.get("generated_at", "")[:16]}</div>',
        unsafe_allow_html=True,
    )

    # ── Model toggle (ET / SARR) ─────────────────────────────────────────
    if sarr_available:
        model_options = ["ET (Expected Time)", "SARR (Sectional-Anchored)"]
        sel_model = st.radio(
            "Model", model_options, index=0, horizontal=True,
            key="rd_model_toggle",
        )
        use_sarr = sel_model.startswith("SARR")
    else:
        use_sarr = False

    active_races = sarr_races if use_sarr else et_races

    # ── Summary metrics ──────────────────────────────────────────────────
    total_runners = sum(r.get("runners", 0) for r in active_races)
    total_projected = sum(r.get("projected", 0) for r in active_races)
    cols = st.columns(4)
    cols[0].metric("Races", len(active_races))
    cols[1].metric("Total Runners", total_runners)
    cols[2].metric("Projected", total_projected)
    cols[3].metric("Venue", data.get("meeting_venue", "?"))

    st.markdown('<hr class="term-divider">', unsafe_allow_html=True)

    # ── Top Picks Summary table (collapsible) ──────────────────────────
    with st.expander("**TOP PICKS SUMMARY**", expanded=False):
        summary_rows = []
        for race in active_races:
            picks = race.get("picks", [])
            if not picks:
                continue
            top = picks[0]
            cls_str = f"C{race.get('race_class', '')}" if race.get('race_class') else "Grp"
            surface = "AWT" if race.get("is_awt") else "Turf"
            if use_sarr:
                summary_rows.append({
                    "Race": f"R{race['race_number']}",
                    "Dist": f"{race.get('distance', '?')}m",
                    "Surf": surface,
                    "Cls": cls_str,
                    "Top Pick": top.get("horse_name", "?"),
                    "SARR": f"{top.get('sarr', 0):+.3f}",
                    "Style": top.get("style", "?"),
                    "2nd": picks[1]["horse_name"] if len(picks) > 1 else "—",
                    "3rd": picks[2]["horse_name"] if len(picks) > 2 else "—",
                    "4th": picks[3]["horse_name"] if len(picks) > 3 else "—",
                })
            else:
                summary_rows.append({
                    "Race": f"R{race['race_number']}",
                    "Dist": f"{race['distance']}m",
                    "Surf": surface,
                    "Cls": cls_str,
                    "Pace": race.get("pace", "?"),
                    "Top Pick": top["horse_name"],
                    "Proj (s)": f"{top['projected_time']:.2f}",
                    "Win%": f"{top['win_prob']:.0f}%",
                    "Risk": f"{top['risk_score']:.0f}({top['risk_tier'][0]})",
                    "2nd": picks[1]["horse_name"] if len(picks) > 1 else "—",
                    "3rd": picks[2]["horse_name"] if len(picks) > 2 else "—",
                    "4th": picks[3]["horse_name"] if len(picks) > 3 else "—",
                })

        if summary_rows:
            st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

    st.markdown('<hr class="term-divider">', unsafe_allow_html=True)

    # ── Race tab selector ────────────────────────────────────────────────
    race_numbers = [r["race_number"] for r in active_races]
    if "rd_active_race" not in st.session_state:
        st.session_state["rd_active_race"] = race_numbers[0] if race_numbers else None

    # Render tab row
    tab_cols = st.columns(len(race_numbers))
    for i, rn in enumerate(race_numbers):
        with tab_cols[i]:
            is_active = st.session_state["rd_active_race"] == rn
            btn_label = f"R{rn}"
            if st.button(btn_label, key=f"rd_tab_{rn}",
                         use_container_width=True,
                         type="primary" if is_active else "secondary"):
                st.session_state["rd_active_race"] = rn
                st.session_state.pop(f"rd_full_{rn}", None)
                st.session_state.pop(f"rd_sarr_full_{rn}", None)
                st.rerun()

    # ── Render selected race ─────────────────────────────────────────────
    active_rn = st.session_state["rd_active_race"]
    if active_rn:
        if use_sarr:
            sarr_race = next((r for r in sarr_races if r["race_number"] == active_rn), None)
            et_race = next((r for r in et_races if r["race_number"] == active_rn), None)
            if sarr_race:
                render_sarr_race_card(sarr_race, et_race=et_race, bb_lookup=bb_lookup)
        else:
            race = next((r for r in et_races if r["race_number"] == active_rn), None)
            if race:
                render_race_card(race, vet_lookup=vet_lookup, show_top=4, bb_lookup=bb_lookup)

    # ── Column acronym legend ────────────────────────────────────────────
    with st.expander("📖 Column Legend & Interpretation Guide"):
        st.markdown(
            """
**ET Model (Expected Time)**
| Column | Meaning | Interpretation |
|--------|---------|---------------|
| **Rk** | Model rank | Lower = stronger pick (1 = best) |
| **No** | Saddle cloth number | Horse's race-day number |
| **Proj (s)** | Projected finish time (seconds) | Lower = faster; best horse has lowest time |
| **Win%** | Estimated win probability | Higher = more likely to win; ≥20% is strong |
| **ESZ** | Early Speed Z-score | **Negative** = faster early speed; **Positive** = slower starter; ≥1.0 is very slow out |
| **Fin Sec** | Projected final sectional (seconds) | Lower = stronger finishing burst |
| **SSI** | Sectional Speed Index (consistency) | **Negative** = consistently faster than field; **Positive** = inconsistent/slower |
| **Eff Resid** | Effective Residual | **Negative** = horse runs faster than expected from its rating; **Positive** = slower than expected |
| **Trial** | Trial performance flag | ++ outstanding, + positive, - poor, -- very poor |
| **Vet** | Vet report flag | RED = significant concern, AMB = monitor, INF = informational |
| **Flags** | Model flags | ↑IMP = improving, U = unreliable form, etc. |

**SARR Model (Style-Adjusted Residual Ranking)**
| Column | Meaning | Interpretation |
|--------|---------|---------------|
| **SARR** | Composite score | **Negative** = outperforms expectations (better); positive = underperforms |
| **FMRP** | Form Residual Performance | **Negative** = recent form better than rating suggests |
| **LSA** | Late Speed Advantage | **Negative** = strong finishing ability relative to field |
| **Traj** | Trajectory (form trend) | **Negative** = improving trend; positive = declining |
| **WPR%** | Win/Place Rate | Higher = better historical strike rate |
| **Late Std** | Late-section time standard deviation | Lower = more consistent finisher (≤0.20 very reliable) |

**Shared Columns**
| Column | Meaning |
|--------|---------|
| **Draw** | Barrier draw (gate position) |
| **Wt** | Carried weight (lbs) |
| **Style** | Predicted running style (Leader / On-Pace / Midfield / Closer) |
| **BB** | In your Blackbook |
            """,
            unsafe_allow_html=False,
        )

    # ── Sidebar downloads ────────────────────────────────────────────────
    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
    st.sidebar.markdown('<div class="sb-nav-section">Downloads</div>', unsafe_allow_html=True)
    pdf_name = data.get("pdf_file", "")
    txt_name = data.get("text_file", "")
    pdf_path = REPORTS / pdf_name
    txt_path = REPORTS / txt_name

    if pdf_path.exists():
        with open(pdf_path, "rb") as f:
            st.sidebar.download_button(
                "[ ET PDF Report ]", f.read(),
                file_name=pdf_name, mime="application/pdf",
            )
    if txt_path.exists():
        with open(txt_path, "r", encoding="utf-8") as f:
            st.sidebar.download_button(
                "[ Text Report ]", f.read(),
                file_name=txt_name, mime="text/plain",
            )
    # SARR PDF download
    if sarr_data:
        sarr_pdf_name = sarr_data.get("pdf_file", "")
        sarr_pdf_path = REPORTS / sarr_pdf_name
        if sarr_pdf_path.exists():
            with open(sarr_pdf_path, "rb") as f:
                st.sidebar.download_button(
                    "[ SARR PDF Report ]", f.read(),
                    file_name=sarr_pdf_name, mime="application/pdf",
                )


# ══════════════════════════════════════════════════════════════════════════════
# Backtest page — rendering helpers
# ══════════════════════════════════════════════════════════════════════════════

def _bt_color(val, thresholds, higher_better=True):
    """Return good/ok/poor CSS class for a value."""
    if val is None:
        return ""
    good, ok = thresholds
    if higher_better:
        if val >= good:
            return "bt-good"
        elif val >= ok:
            return "bt-ok"
        return "bt-poor"
    else:
        if val <= good:
            return "bt-good"
        elif val <= ok:
            return "bt-ok"
        return "bt-poor"


def _fmt_pct(val):
    if val is None:
        return "—"
    return f"{val*100:.1f}%" if isinstance(val, float) and val <= 1 else f"{val}%"


def _fmt_val(val, suffix=""):
    if val is None:
        return "—"
    if suffix == "s":
        return f"{val:.3f}s"
    if suffix == "%":
        return _fmt_pct(val)
    return f"{val:.3f}" if isinstance(val, float) else str(val)


def _render_backtest_v4(data: dict, prefix: str = ""):
    """Render v4 backtest format (from backtest_apr12_v4.py)."""
    m = data.get("metrics", {})
    races = data.get("races", [])
    fi = data.get("factor_importance", [])

    # ── Header metrics ─────────────────────────────────────
    st.markdown("### Model Accuracy")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("MAE", f'{m.get("mae", "?")}s',
              help="Mean Absolute Error: projected vs actual finish time")
    c2.metric("Median AE", f'{m.get("median_ae", "?")}s')
    bias_val = m.get("bias")
    c3.metric("Bias", f'{bias_val:+.3f}s' if bias_val is not None else "—",
              help="Positive = predicted too fast")
    c4.metric("Rank ρ", f'{m.get("mean_spearman_rho", "?")}',
              help="Average Spearman ρ — does model ordering match actual?")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Top-1 Win%", f'{100*(m.get("top1_rate") or 0):.0f}%')
    c6.metric("Top-3 Hit%", f'{100*(m.get("top3_rate") or 0):.0f}%')
    c7.metric("Top-5 Hit%", f'{100*(m.get("top5_rate") or 0):.0f}%')
    c8.metric("Top-3 Overlap", f'{(m.get("mean_top3_overlap") or 0):.1f}/3')

    _traffic_light("MAE", m.get("mae"), good=0.8, ok=1.2, lower_is_better=True)
    _traffic_light("Rank ρ", m.get("mean_spearman_rho"), good=0.5, ok=0.3, lower_is_better=False)

    st.markdown("---")

    # ── Per-race breakdown ─────────────────────────────────
    st.markdown("### Per-Race Breakdown")
    race_rows = []
    for r in races:
        wr = r.get("winner_pred_rank")
        wr_str = f'Rk#{wr}' if wr else "N/P"
        wr_colour = "#22c55e" if wr and wr <= 3 else "#f59e0b" if wr and wr <= 5 else "#ef4444"
        race_rows.append({
            "Race": f'R{r["race_number"]}',
            "Dist": r.get("distance", "?"),
            "Class": f'C{r.get("class", "?")}' if r.get("class") else "Grp",
            "Winner": r.get("winner", "?"),
            "Win Rank": wr_str,
            "Top-3 ∩": f'{r.get("top3_overlap", 0)}/3',
            "ρ": f'{r.get("spearman_rho", 0):.2f}' if r.get("spearman_rho") is not None else "—",
            "MAE": f'{r.get("mae", 0):.2f}s' if r.get("mae") is not None else "—",
        })
    if race_rows:
        st.dataframe(pd.DataFrame(race_rows), use_container_width=True, hide_index=True)

    st.markdown("---")

    # ── Factor importance ──────────────────────────────────
    if fi:
        st.markdown("### Hierarchical Factor Importance")
        st.caption("Which model components best predict actual finishing position?")
        fi_sorted = sorted(fi, key=lambda x: abs(x.get("rho_place", 0)), reverse=True)
        fi_rows = []
        for f in fi_sorted:
            rho = f.get("rho_place", 0)
            p = f.get("p", 1)
            sig = "★★★" if p < 0.001 else "★★" if p < 0.01 else "★" if p < 0.05 else ""
            colour = "#22c55e" if p < 0.05 else "#888"
            fi_rows.append({
                "Factor": f["factor"],
                "|ρ|": f'{abs(rho):.3f}',
                "ρ (place)": f'{rho:+.3f}',
                "Significance": sig,
                "ρ (FT)": f'{f.get("rho_ft", 0):+.3f}',
            })
        st.dataframe(pd.DataFrame(fi_rows), use_container_width=True, hide_index=True)

    st.markdown("---")

    # ── Horse-level detail (expandable per race) ───────────
    st.markdown("### Detailed Horse Comparisons")
    for r in races:
        horses = r.get("horses", [])
        if not horses:
            continue
        with st.expander(f'R{r["race_number"]} {r.get("distance","?")}m — Winner: {r.get("winner","?")}'):
            h_rows = []
            for h in sorted(horses, key=lambda x: x.get("actual_place", 99)):
                te = h.get("time_error")
                te_str = f'{te:+.2f}s' if te is not None else "—"
                te_colour = "✓" if te is not None and abs(te) < 0.3 else ""
                style_match = "✓" if h.get("pred_style") == h.get("actual_style") else "✗"
                h_rows.append({
                    "Horse": h["horse"],
                    "Pred Rk": h.get("pred_rank"),
                    "Actual P": h.get("actual_place"),
                    "Proj Time": f'{h.get("pred_time", 0):.2f}' if h.get("pred_time") else "—",
                    "Actual FT": f'{h.get("actual_ft", 0):.2f}' if h.get("actual_ft") else "—",
                    "Error": te_str,
                    "Style": style_match,
                    "Odds": f'${h.get("actual_odds", 0):.1f}' if h.get("actual_odds") else "—",
                })
            st.dataframe(pd.DataFrame(h_rows), use_container_width=True, hide_index=True)


def render_backtest_metrics(data: dict, prefix: str = ""):
    """Render backtest card — auto-detects v4 vs legacy format."""
    if "metrics" in data and "factor_importance" in data:
        return _render_backtest_v4(data, prefix)
    ta = data.get("time_accuracy", {})
    pa = data.get("pace_accuracy", {})
    pb = data.get("pace_beneficiary", {})
    ra = data.get("risk_accuracy", {})
    sa = data.get("style_accuracy", {})
    da = data.get("draw_accuracy", {})

    # ── Dimension 1: Time Accuracy ─────────────────────────
    st.markdown("### 1. Projected Time Accuracy")
    c1, c2, c3, c4 = st.columns(4)
    mae = ta.get("mae_seconds")
    c1.metric("MAE", _fmt_val(mae, "s"), help="Mean Absolute Error of projected vs actual finish time")
    c2.metric("Median AE", _fmt_val(ta.get("median_ae_seconds"), "s"))
    rho = ta.get("rank_correlation_avg")
    c3.metric("Rank ρ", _fmt_val(rho), help="Spearman rank correlation — does model ordering match finish order?")
    c4.metric("Observations", ta.get("n_observations", 0))

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Top-1 Win Rate", _fmt_pct(ta.get("top1_win_rate")),
              help="How often does the model's #1 pick actually win?")
    c6.metric("Top-1 Races", ta.get("top1_total", 0))
    c7.metric("Top-3 Place Rate", _fmt_pct(ta.get("top3_place_rate")),
              help="How often do model top-3 picks finish top 3?")
    c8.metric("Top-3 Obs", ta.get("top3_total", 0))

    _traffic_light("MAE", mae, good=0.8, ok=1.2, lower_is_better=True)
    _traffic_light("Rank Correlation", rho, good=0.5, ok=0.3, lower_is_better=False)

    st.markdown("---")

    # ── Dimension 2: Pace Accuracy ─────────────────────────
    st.markdown("### 2. Pace Prediction Accuracy")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Exact Match", _fmt_pct(pa.get("exact_match_rate")),
              help="Predicted pace label matches actual label exactly")
    c2.metric("±1 Category", _fmt_pct(pa.get("within_one_category")),
              help="Predicted pace within one category of actual")
    c3.metric("Deviation r", _fmt_val(pa.get("deviation_correlation")),
              help="Pearson correlation of predicted vs actual pace deviation")
    c4.metric("Races", pa.get("n_races", 0))

    _traffic_light("Pace Exact", pa.get("exact_match_rate"), good=0.4, ok=0.25, lower_is_better=False)

    # Pace detail table
    pace_details = pa.get("details", [])
    if pace_details:
        with st.expander(f"Pace details ({len(pace_details)} races)"):
            pdf = pd.DataFrame(pace_details)
            if not pdf.empty:
                pdf["match"] = pdf["match"].map({True: "✓", False: "✗"})
                st.dataframe(pdf, use_container_width=True, hide_index=True)

    st.markdown("---")

    # ── Dimension 3: Pace Beneficiary ──────────────────────
    st.markdown("### 3. Pace Beneficiary Analysis")
    c1, c2, c3, c4 = st.columns(4)
    adv = pb.get("benefit_advantage")
    c1.metric("Advantage", _fmt_val(adv) + " ranks" if adv is not None else "—",
              help="Positive = horses predicted to benefit from pace finish closer to predicted rank")
    c2.metric("Beneficiary Avg", _fmt_val(pb.get("pace_beneficiary_avg_rank_diff")),
              help="Avg (actual_place − predicted_rank) for predicted beneficiaries")
    c3.metric("Penalised Avg", _fmt_val(pb.get("pace_penalised_avg_rank_diff")))
    c4.metric("N (benefit/penalty)", f"{pb.get('n_beneficiaries',0)}/{pb.get('n_penalised',0)}")

    _traffic_light("Pace Benefit Adv", adv, good=0.5, ok=0.0, lower_is_better=False)

    st.markdown("---")

    # ── Dimension 4: Risk Reflection ───────────────────────
    st.markdown("### 4. Risk Reflection Accuracy")
    c1, c2 = st.columns(2)
    c1.metric("Risk↔Error Correlation", _fmt_val(ra.get("risk_error_correlation")),
              help="Positive = higher risk predictions have larger time errors (desirable)")
    c2.metric("Observations", ra.get("n_observations", 0))

    tier_perf = ra.get("tier_performance", {})
    if tier_perf:
        st.markdown("**ROI by Risk Tier (top-4 picks, flat $10 bet):**")
        tier_rows = []
        for tier in ["Low", "Medium", "High"]:
            p = tier_perf.get(tier, {})
            if not p:
                continue
            tier_rows.append({
                "Tier": tier,
                "Bets": p.get("n_bets", 0),
                "Win Rate": _fmt_pct(p.get("win_rate")),
                "Place Rate": _fmt_pct(p.get("place_rate")),
                "ROI": f"{p.get('roi_pct', 0):+.1f}%",
            })
        if tier_rows:
            st.dataframe(pd.DataFrame(tier_rows), use_container_width=True, hide_index=True)

    st.markdown("---")

    # ── Dimension 5: Running Style ─────────────────────────
    st.markdown("### 5. Running Style Prediction")
    c1, c2 = st.columns(2)
    c1.metric("Exact Match Rate", _fmt_pct(sa.get("exact_match_rate")),
              help="Predicted running style matches actual first-checkpoint position")
    c2.metric("Observations", sa.get("n_observations", 0))

    _traffic_light("Style Match", sa.get("exact_match_rate"), good=0.45, ok=0.30, lower_is_better=False)

    confusion = sa.get("confusion_matrix", {})
    if confusion:
        with st.expander("Style confusion matrix"):
            styles = ["Leader", "On-Pace", "Midfield", "Closer"]
            rows_cm = []
            for pred_s in styles:
                row = {"Predicted": pred_s}
                for act_s in styles:
                    row[f"Act: {act_s}"] = confusion.get(pred_s, {}).get(act_s, 0)
                rows_cm.append(row)
            st.dataframe(pd.DataFrame(rows_cm), use_container_width=True, hide_index=True)

    st.markdown("---")

    # ── Dimension 6: Draw Offset ───────────────────────────
    st.markdown("### 6. Draw Offset Accuracy")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Inside Avg Place", _fmt_val(da.get("inside_avg_place")))
    c2.metric("Middle Avg Place", _fmt_val(da.get("middle_avg_place")))
    c3.metric("Outside Avg Place", _fmt_val(da.get("outside_avg_place")))
    c4.metric("Detected Bias", da.get("draw_bias_detected", "—"))


def _traffic_light(label: str, val, good: float, ok: float,
                   lower_is_better: bool = False):
    """Render a small traffic-light indicator."""
    if val is None:
        return
    if lower_is_better:
        if val <= good:
            colour, word = "#22c55e", "Good"
        elif val <= ok:
            colour, word = "#f59e0b", "OK"
        else:
            colour, word = "#ef4444", "Review"
    else:
        if val >= good:
            colour, word = "#22c55e", "Good"
        elif val >= ok:
            colour, word = "#f59e0b", "OK"
        else:
            colour, word = "#ef4444", "Review"
    st.markdown(
        f'<span style="color:{colour};font-weight:bold;font-size:0.85em">'
        f'● {label}: {word}</span>',
        unsafe_allow_html=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Backtest page — main
# ══════════════════════════════════════════════════════════════════════════════

def page_backtest():
    st.markdown('<div class="page-title">Model Backtest</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">Compare pre-race predictions against actual results</div>', unsafe_allow_html=True)

    # ── Sidebar: scrape results + run backtest ─────────────
    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
    st.sidebar.markdown('<div class="sb-nav-section">Post-Race Data</div>', unsafe_allow_html=True)

    scrape_date = st.sidebar.date_input("Results date", value=date.today(),
                                        key="bt_scrape_date")
    col_a, col_b = st.sidebar.columns(2)
    with col_a:
        if st.button("[ Scrape ]", use_container_width=True,
                      key="btn_scrape_results"):
            _run_results_scraper(scrape_date.isoformat(), full=True)
            st.cache_data.clear()
            st.rerun()
    with col_b:
        if st.button("[ Backtest ]", use_container_width=True,
                      key="btn_run_backtest"):
            _run_backtest_single(scrape_date.isoformat())
            st.cache_data.clear()
            st.rerun()

    # Download scraped results as Excel
    _scrape_compact = scrape_date.isoformat().replace("-", "")
    _results_json = REPORTS / f"results_{_scrape_compact}.json"
    if _results_json.exists():
        _xl_bytes = _results_json_to_excel_bytes(_results_json)
        if _xl_bytes:
            st.sidebar.download_button(
                "[ Download Results (Excel) ]",
                _xl_bytes,
                file_name=f"results_{_scrape_compact}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="btn_dl_results_xlsx",
            )

    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
    st.sidebar.markdown('<div class="sb-nav-section">Aggregate Reports</div>', unsafe_allow_html=True)

    agg_month = st.sidebar.text_input("Month (YYYY-MM)", value=date.today().strftime("%Y-%m"),
                                       key="bt_month")
    if st.sidebar.button("[ Monthly Backtest ]", use_container_width=True,
                          key="btn_monthly_bt"):
        _run_backtest_agg("--month", agg_month)
        st.cache_data.clear()
        st.rerun()

    agg_season = st.sidebar.text_input("Season (YYYY-YYYY)", value="2025-2026",
                                        key="bt_season")
    if st.sidebar.button("[ Seasonal Backtest ]", use_container_width=True,
                          key="btn_season_bt"):
        _run_backtest_agg("--season", agg_season)
        st.cache_data.clear()
        st.rerun()

    # ── Status: what data exists ─────────────────────────
    pred_dates = find_prediction_dates()
    res_dates = find_results_dates()
    matched_dates = sorted(set(pred_dates) & set(res_dates))
    unmatched_pred = sorted(set(pred_dates) - set(res_dates))

    st.markdown("**DATA INVENTORY**")
    c1, c2, c3 = st.columns(3)
    c1.metric("Predictions", len(pred_dates))
    c2.metric("Results Scraped", len(res_dates))
    c3.metric("Matched (backtestable)", len(matched_dates))

    if unmatched_pred:
        with st.expander(f"{len(unmatched_pred)} meetings awaiting results"):
            for dc in unmatched_pred:
                d = f"{dc[:4]}-{dc[4:6]}-{dc[6:]}"
                st.text(f"  {d}  — need to scrape results")

    st.markdown("---")

    # ── Tabs: Weekly / Monthly / Seasonal ─────────────────
    tab_weekly, tab_monthly, tab_seasonal = st.tabs([
        "Weekly (Per-Meeting)", "Monthly", "Seasonal"])

    bt_files = load_backtest_files()

    # ── Weekly tab ─────────────────────────────────────────
    with tab_weekly:
        meetings = bt_files.get("meeting", {})
        if not meetings:
            st.info("No per-meeting backtests yet. Scrape results and run backtest above.")
        else:
            meeting_keys = sorted(meetings.keys(), reverse=True)
            labels = {k: f"{k[:4]}-{k[4:6]}-{k[6:]}  ({meetings[k].get('meeting_title','')})"
                      for k in meeting_keys}
            selected_key = st.selectbox("Select meeting:", meeting_keys,
                                        format_func=lambda k: labels[k],
                                        key="bt_weekly_select")
            if selected_key:
                render_backtest_metrics(meetings[selected_key], prefix="wk_")

    # ── Monthly tab ────────────────────────────────────────
    with tab_monthly:
        monthlies = bt_files.get("monthly", {})
        if not monthlies:
            st.info("No monthly backtests yet. Use the sidebar to generate one.")
        else:
            month_keys = sorted(monthlies.keys(), reverse=True)
            selected_month = st.selectbox("Select month:", month_keys,
                                          key="bt_month_select")
            if selected_month:
                mdata = monthlies[selected_month]
                st.markdown(f"**Period:** {mdata.get('period','')}  |  "
                            f"**Meetings:** {mdata.get('n_meetings',0)}  |  "
                            f"**Races:** {mdata.get('n_races',0)}  |  "
                            f"**Runners:** {mdata.get('n_runners',0)}")

                # Per-meeting breakdown table
                per_m = mdata.get("per_meeting", [])
                if per_m:
                    st.markdown("#### Per-Meeting Breakdown")
                    rows_pm = []
                    for m in per_m:
                        rows_pm.append({
                            "Date": m.get("date", ""),
                            "Title": m.get("title", ""),
                            "Races": m.get("races", 0),
                            "MAE": _fmt_val(m.get("mae"), "s"),
                            "Rank ρ": _fmt_val(m.get("rank_corr")),
                            "Pace Exact": _fmt_pct(m.get("pace_exact")),
                            "Top-1 Win": _fmt_pct(m.get("top1_win")),
                        })
                    st.dataframe(pd.DataFrame(rows_pm), use_container_width=True,
                                 hide_index=True)
                    st.markdown("---")

                render_backtest_metrics(mdata, prefix="mo_")

    # ── Seasonal tab ───────────────────────────────────────
    with tab_seasonal:
        seasons = bt_files.get("season", {})
        all_bt = bt_files.get("all")
        if not seasons and not all_bt:
            st.info("No seasonal backtests yet. Use the sidebar to generate one.")
        else:
            options_s = list(seasons.keys())
            if all_bt:
                options_s.insert(0, "__all__")
            if options_s:
                sel_s = st.selectbox(
                    "Select period:", options_s,
                    format_func=lambda k: "All Data" if k == "__all__" else f"Season {k}",
                    key="bt_season_select",
                )
                sdata = all_bt if sel_s == "__all__" else seasons.get(sel_s, {})
                if sdata:
                    st.markdown(f"**Period:** {sdata.get('period','')}  |  "
                                f"**Meetings:** {sdata.get('n_meetings',0)}  |  "
                                f"**Races:** {sdata.get('n_races',0)}")

                    per_m = sdata.get("per_meeting", [])
                    if per_m:
                        st.markdown("#### Meeting-by-Meeting Trend")
                        trend_df = pd.DataFrame(per_m)
                        if "mae" in trend_df.columns:
                            st.line_chart(trend_df.set_index("date")[["mae"]],
                                          use_container_width=True)
                        st.markdown("---")

                    render_backtest_metrics(sdata, prefix="se_")


def _results_json_to_excel_bytes(results_path: Path) -> bytes | None:
    """Convert a results JSON to Excel bytes for download."""
    try:
        with open(results_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    rows = []
    race_date = data.get("date", "")
    venue = data.get("venue", "")
    for race in data.get("races", []):
        for runner in race.get("runners", []):
            rows.append({
                "race_date": race_date,
                "venue": venue,
                "race_number": race.get("race_number"),
                "race_name": race.get("race_name", ""),
                "distance": race.get("distance"),
                "race_class": race.get("race_class", ""),
                "race_course": race.get("race_course", ""),
                "going": race.get("going", ""),
                "place": runner.get("place", ""),
                "horse_no": runner.get("horse_no"),
                "horse_name": runner.get("horse_name", ""),
                "jockey": runner.get("jockey", ""),
                "trainer": runner.get("trainer", ""),
                "actual_weight": runner.get("actual_weight"),
                "draw": runner.get("draw"),
                "lbw": runner.get("lbw", ""),
                "finish_time": runner.get("finish_time", ""),
                "finish_time_seconds": runner.get("finish_time_seconds"),
                "win_odds": runner.get("win_odds", ""),
            })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, sheet_name="Results")
    return buf.getvalue()


def _append_results_to_db(results_path: Path):
    """Append scraped results to hkjc_results_updated.xlsx."""
    db_file = BASE / "hkjc_results_updated.xlsx"
    try:
        with open(results_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        st.error(f"Failed to read results JSON: {e}")
        return

    new_rows = []
    race_date = data.get("date", "")
    venue = data.get("venue", "")
    race_track = "ST" if venue.upper() in ("ST", "SHA TIN") else (
        "HV" if venue.upper() in ("HV", "HAPPY VALLEY") else venue)
    for race in data.get("races", []):
        is_awt = race.get("is_awt", False)
        track_type = "All Weather Track" if is_awt else "Turf"
        for runner in race.get("runners", []):
            positions_list = runner.get("positions", [])
            running_positions = " ".join(p for p in positions_list if p) if positions_list else ""
            sectiontimes_list = runner.get("sectiontimes", [])
            sectiontimes_str = "; ".join(s for s in sectiontimes_list if s) if sectiontimes_list else ""
            new_rows.append({
                "race_date": race_date,
                "race_number": race.get("race_number"),
                "horse_number": runner.get("horse_no"),
                "horse_name": runner.get("horse_name", ""),
                "place": runner.get("place", ""),
                "jockey": runner.get("jockey", ""),
                "trainer": runner.get("trainer", ""),
                "actual_weight": runner.get("actual_weight"),
                "declared_weight": runner.get("declared_weight"),
                "draw": runner.get("draw"),
                "lbw": runner.get("lbw", ""),
                "running_positions": running_positions,
                "finish_time_seconds": runner.get("finish_time_seconds"),
                "win_odds": runner.get("win_odds", ""),
                "going": race.get("going", ""),
                "race_class": race.get("race_class", ""),
                "race_course": race.get("race_course", ""),
                "race_track": race_track,
                "track_type": track_type,
                "distance": race.get("distance"),
                "sectiontimes": sectiontimes_str,
            })
    if not new_rows:
        st.warning("No runner data found in results JSON.")
        return

    new_df = pd.DataFrame(new_rows)

    # Read existing DB (handle OneDrive lock + zip corruption)
    if db_file.exists():
        try:
            existing = _safe_read_excel(db_file)
        except Exception as e:
            st.error(f"Cannot read {db_file.name} (corrupt?): {e}")
            st.info("Writing new results only. Restore a backup and retry.")
            existing = None
        if existing is not None:
            # Avoid duplicate rows for the same date
            if "race_date" in existing.columns:
                existing = existing[existing["race_date"] != race_date]
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df
    else:
        combined = new_df

    # Write via temp then copy (prevents OneDrive partial-write corruption)
    tmp_out = Path(tempfile.gettempdir()) / db_file.name
    combined.to_excel(tmp_out, index=False)
    # Keep a .bak of the previous version
    if db_file.exists():
        bak = db_file.with_suffix(".xlsx.bak")
        try:
            shutil.copy2(db_file, bak)
        except Exception:
            pass
    try:
        shutil.copy2(tmp_out, db_file)
    except PermissionError:
        st.warning(f"OneDrive lock — saved to {tmp_out} instead. Copy manually.")

    st.success(f"Appended {len(new_df)} rows to {db_file.name} "
               f"(total: {len(combined)} rows)")


def _run_results_scraper(date_str: str, *, full: bool = False):
    """Invoke results scraper from the dashboard.

    *full=False*  (default / Live Feed): lightweight ``scrape_hkjc_results.py``
        — fast, captures ~20 fields, good for mid-meeting live scores.
    *full=True*   (Results page / Backtest): full ``scrape_hkjc.py``
        — slower, captures all 55+ fields including horse profiles, Chinese
        names, gear, rating, sire/dam.  Merges directly into the Excel DB.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    if full:
        # Full scraper: scrape_hkjc.py --date DD/MM/YYYY ...
        dd_mm_yyyy = f"{date_str[8:10]}/{date_str[5:7]}/{date_str[:4]}"
        horse_cache = Path(tempfile.gettempdir()) / "horse_cache_hkjc.json"
        cmd = [PYTHON, str(BASE / "scrape_hkjc.py"),
               "--date", dd_mm_yyyy,
               "--horse-cache", str(horse_cache),
               "--no-cache"]
        with st.spinner(f"Full-scraping results for {date_str} (includes horse profiles)…"):
            result = subprocess.run(cmd, env=env, cwd=str(BASE),
                                    capture_output=True, text=True, encoding="utf-8",
                                    timeout=300)
        if result.returncode == 0:
            st.success(f"Full scrape complete for {date_str}")
            with st.expander("Output"):
                st.code(result.stdout[-2000:] if len(result.stdout) > 2000
                        else result.stdout)
            # Merge hkjc_results.xlsx into the main DB
            fresh_path = BASE / "hkjc_results.xlsx"
            if fresh_path.exists():
                _merge_full_scrape_to_db(fresh_path, date_str)
        else:
            st.error(f"Full scraper failed (exit code {result.returncode})")
            with st.expander("Error"):
                st.code(result.stderr[-2000:] if result.stderr
                        else result.stdout[-2000:])
    else:
        # Lightweight scraper (Live Feed)
        cmd = [PYTHON, str(BASE / "scrape_hkjc_results.py"), "--date", date_str]
        with st.spinner(f"Scraping results for {date_str}..."):
            result = subprocess.run(cmd, env=env, cwd=str(BASE),
                                    capture_output=True, text=True, encoding="utf-8")
            if result.returncode == 0:
                st.success(f"Results scraped for {date_str}")
                with st.expander("Output"):
                    st.code(result.stdout[-2000:] if len(result.stdout) > 2000
                            else result.stdout)
                # Auto-append to DB
                date_compact = date_str.replace("-", "")
                results_json = REPORTS / f"results_{date_compact}.json"
                if results_json.exists():
                    _append_results_to_db(results_json)
            else:
                st.error(f"Scraper failed (exit code {result.returncode})")
                with st.expander("Error"):
                    st.code(result.stderr[-2000:] if result.stderr
                            else result.stdout[-2000:])


def _merge_full_scrape_to_db(fresh_path: Path, date_str: str):
    """Merge a full scrape Excel (hkjc_results.xlsx) into the main DB."""
    db_file = BASE / "hkjc_results_updated.xlsx"
    try:
        fresh = pd.read_excel(fresh_path)
        fresh["race_date"] = pd.to_datetime(fresh["race_date"], errors="coerce")
    except Exception as e:
        st.error(f"Failed to read full scrape output: {e}")
        return

    if db_file.exists():
        try:
            existing = _safe_read_excel(db_file)
            existing["race_date"] = pd.to_datetime(existing["race_date"], errors="coerce")
        except Exception as e:
            st.error(f"Cannot read {db_file.name}: {e}")
            existing = None
        if existing is not None:
            target_date = pd.Timestamp(date_str)
            existing = existing[existing["race_date"] != target_date]
            # Align columns
            for c in existing.columns:
                if c not in fresh.columns:
                    fresh[c] = pd.NA
            for c in fresh.columns:
                if c not in existing.columns:
                    existing[c] = pd.NA
            combined = pd.concat([existing, fresh[existing.columns]], ignore_index=True)
        else:
            combined = fresh
    else:
        combined = fresh

    tmp_out = Path(tempfile.gettempdir()) / db_file.name
    combined.to_excel(tmp_out, index=False)
    if db_file.exists():
        bak = db_file.with_suffix(".xlsx.bak")
        try:
            shutil.copy2(db_file, bak)
        except Exception:
            pass
    try:
        shutil.copy2(tmp_out, db_file)
        st.success(f"Merged {len(fresh)} rows into {db_file.name} (total: {len(combined)})")
    except PermissionError:
        st.warning(f"OneDrive lock — saved to {tmp_out}. Copy manually.")


def _run_backtest_single(date_str: str):
    """Run backtest for a single meeting."""
    cmd = [PYTHON, str(BASE / "backtest_model.py"), "--date", date_str]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    with st.spinner(f"Running backtest for {date_str}..."):
        result = subprocess.run(cmd, env=env, cwd=str(BASE),
                                capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0:
            st.success(f"Backtest complete for {date_str}")
            with st.expander("Output"):
                st.code(result.stdout[-2000:] if len(result.stdout) > 2000
                        else result.stdout)
        else:
            st.error(f"Backtest failed (exit code {result.returncode})")
            with st.expander("Error"):
                st.code(result.stderr[-2000:] if result.stderr
                        else result.stdout[-2000:])


def _run_backtest_agg(flag: str, value: str):
    """Run aggregate backtest (--month or --season)."""
    cmd = [PYTHON, str(BASE / "backtest_model.py"), flag, value]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    with st.spinner(f"Running aggregate backtest ({flag} {value})..."):
        result = subprocess.run(cmd, env=env, cwd=str(BASE),
                                capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0:
            st.success(f"Aggregate backtest complete")
            with st.expander("Output"):
                st.code(result.stdout[-2000:] if len(result.stdout) > 2000
                        else result.stdout)
        else:
            st.error(f"Backtest failed")
            with st.expander("Error"):
                st.code(result.stderr[-2000:] if result.stderr
                        else result.stdout[-2000:])


# ══════════════════════════════════════════════════════════════════════════════
# Blackbook page
# ══════════════════════════════════════════════════════════════════════════════

def page_blackbook():
    st.markdown('<div class="page-title">Blackbook</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">Qualitative picks &middot; track your eye vs the model</div>', unsafe_allow_html=True)

    bb = _load_blackbook()
    _bb_expire_stale(bb)
    tag_defs = bb.get("tag_definitions", {})
    all_tags = sorted(tag_defs.keys())

    active = [e for e in bb["entries"] if e["status"] == "active"]
    expired = [e for e in bb["entries"] if e["status"] == "expired"]
    archived = [e for e in bb["entries"] if e["status"] == "archived"]

    # Summary metrics row
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Active", len(active))
    c2.metric("Expired", len(expired))
    c3.metric("Archived", len(archived))
    total_runs = sum(len(e.get("performances", [])) for e in bb["entries"])
    c4.metric("Total Runs", total_runs)

    tab_browse, tab_add, tab_analytics, tab_exceptional = st.tabs(
        ["Browse", "Add / Import", "Analytics", "★ Exceptional Performers"])

    # ══ BROWSE TAB ═══════════════════════════════════════════
    with tab_browse:
        if not bb["entries"]:
            st.info("No blackbook entries yet. Use the **Add / Import** tab to get started.")
        else:
            # ── Filters row ──
            fc1, fc2, fc3, fc4 = st.columns(4)
            with fc1:
                filter_status = st.selectbox("Status", ["Active", "All", "Expired", "Archived"],
                                             key="bb_filter")
            with fc2:
                filter_category = st.selectbox("Category", ["All", "Pre-Race", "Post-Race"],
                                               key="bb_cat_filter")
            with fc3:
                filter_conf = st.selectbox("Confidence", ["All", "High", "Medium", "Low"],
                                           key="bb_conf_filter")
            with fc4:
                search_text = st.text_input("Search", placeholder="Horse name…",
                                            key="bb_search")

            # Build filtered pool
            pool = bb["entries"]
            if filter_status != "All":
                pool = [e for e in pool if e["status"] == filter_status.lower()]
            if filter_category != "All":
                pool = [e for e in pool if e.get("category", "Pre-Race") == filter_category]
            if filter_conf != "All":
                pool = [e for e in pool if e.get("confidence", "medium") == filter_conf.lower()]
            if search_text.strip():
                _q = search_text.strip().upper()
                pool = [e for e in pool if _q in e["horse_name"].upper()]

            if not pool:
                st.caption("No entries match the current filters.")
            else:
                # ── Build summary table ──
                _tbl_rows = []
                for e in sorted(pool, key=lambda x: x["added_date"], reverse=True):
                    n_runs = len(e.get("performances", []))
                    last_run = ""
                    if e.get("performances"):
                        last_run = e["performances"][-1].get("date", "")
                    _tbl_rows.append({
                        "Horse": e["horse_name"],
                        "Category": e.get("category", "Pre-Race"),
                        "Confidence": e.get("confidence", "medium").title(),
                        "Status": e["status"].title(),
                        "Added": e.get("added_date", ""),
                        "Expires": e.get("expiry_date", ""),
                        "Tags": ", ".join(e.get("tags", [])),
                        "Runs": n_runs,
                        "Last Run": last_run,
                        "Source": e.get("source_race", ""),
                        "_id": e["id"],
                    })

                tbl_df = pd.DataFrame(_tbl_rows)

                # Style confidence column
                def _style_conf_col(val):
                    _cm = {"High": "color:#d43700;font-weight:600",
                           "Medium": "color:#8a6d00;font-weight:600",
                           "Low": "color:#0066cc;font-weight:600"}
                    return _cm.get(val, "")

                def _style_status_col(val):
                    _sm = {"Active": "color:#22c55e;font-weight:600",
                           "Expired": "color:#888", "Archived": "color:#666"}
                    return _sm.get(val, "")

                display_cols = ["Horse", "Category", "Confidence", "Status",
                                "Added", "Expires", "Tags", "Runs", "Last Run", "Source"]
                styled_tbl = (tbl_df[display_cols].style
                              .map(_style_conf_col, subset=["Confidence"])
                              .map(_style_status_col, subset=["Status"])
                              .set_properties(**{"text-align": "center"})
                              .set_properties(subset=["Horse", "Tags", "Source"],
                                              **{"text-align": "left"}))

                st.dataframe(styled_tbl, use_container_width=True, hide_index=True,
                             height=min(400, 35 * len(tbl_df) + 38))

                # ── Detail panel — select horse from table ──
                st.markdown("---")
                horse_names_in_pool = [e["horse_name"] for e in
                                       sorted(pool, key=lambda x: x["added_date"], reverse=True)]
                selected_horse = st.selectbox(
                    "Select horse for details / actions:",
                    horse_names_in_pool, key="bb_detail_select",
                )
                entry = next(e for e in pool if e["horse_name"] == selected_horse)
                eid = entry["id"]
                conf = entry.get("confidence", "medium")
                conf_colours = {"high": "#d43700", "medium": "#8a6d00", "low": "#0066cc"}
                conf_colour = conf_colours.get(conf, "#888")

                # ── Horse detail card ─────────────────────────
                _cat = entry.get("category", "Pre-Race")
                _cat_icon = "📋" if _cat == "Pre-Race" else "🏁"
                st.markdown(
                    f'<div style="border-left:4px solid {conf_colour};padding:8px 14px;'
                    f'margin:10px 0;border-radius:0 6px 6px 0;'
                    f'background:var(--secondary-background-color,rgba(128,128,128,0.05))">'
                    f'<span style="font-size:1.3em;font-weight:700">{entry["horse_name"]}</span>'
                    f' &nbsp; <span style="color:{conf_colour};font-weight:600">'
                    f'● {conf.title()}</span>'
                    f' &nbsp; <span style="font-size:0.85em;opacity:0.6">{entry["status"].title()}</span>'
                    f' &nbsp; <span style="font-size:0.82em;border:1px solid #888;border-radius:4px;'
                    f'padding:1px 6px">{_cat_icon} {_cat}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                mc1, mc2, mc3, mc4 = st.columns(4)
                mc1.markdown(f"**Source:** {entry.get('source_race', 'N/A')}")
                mc2.markdown(f"**Added:** {entry.get('added_date', '—')}")
                mc3.markdown(f"**Expires:** {entry.get('expiry_date', '—')}")
                tags_str = ", ".join(entry.get("tags", [])) or "—"
                mc4.markdown(f"**Tags:** {tags_str}")

                cond = entry.get("conditions", {})
                cond_parts = []
                if cond.get("preferred_distance"):
                    cond_parts.append(f"Dist: {cond['preferred_distance']}")
                if cond.get("preferred_surface"):
                    cond_parts.append(f"Surface: {cond['preferred_surface']}")
                if cond_parts:
                    st.markdown(f"**Conditions:** {' · '.join(cond_parts)}")

                st.markdown(f"**Reasoning:** {entry.get('reasoning', '—')}")

                # ── Performances ──────────────────────────────
                perfs = entry.get("performances", [])
                if perfs:
                    st.markdown("##### Race History")
                    p_rows = []
                    for pf in perfs:
                        verdict = pf.get("bb_verdict", "")
                        v_icon = {"VALIDATED": "✓", "PARTIAL": "~", "MISSED": "✗"}.get(verdict, "·")
                        p_rows.append({
                            "Date": pf.get("date", "?"),
                            "Race": f'R{pf.get("race_number", "?")}',
                            "Finish": pf.get("finish", "?"),
                            "Verdict": f"{v_icon} {verdict}",
                            "Notes": pf.get("notes", ""),
                        })
                    st.dataframe(pd.DataFrame(p_rows), use_container_width=True, hide_index=True)

                # ── Actions ───────────────────────────────────
                st.markdown("##### Actions")
                ac1, ac2, ac3, ac4 = st.columns(4)
                if entry["status"] == "active":
                    if ac1.button("Archive", key=f"bb_arch_{eid}"):
                        _bb_update_entry(bb, eid, status="archived")
                        st.toast(f"{entry['horse_name']} archived", icon="📦")
                        st.rerun()
                    if ac2.button("+45 days", key=f"bb_ext_{eid}"):
                        new_exp = (date.fromisoformat(entry["expiry_date"])
                                   + timedelta(days=45)).isoformat()
                        _bb_update_entry(bb, eid, expiry_date=new_exp)
                        st.toast(f"{entry['horse_name']} extended 45 days", icon="📅")
                        st.rerun()
                    if ac3.button("Expire", key=f"bb_exp_{eid}"):
                        _bb_update_entry(bb, eid, status="expired")
                        st.toast(f"{entry['horse_name']} expired", icon="⏰")
                        st.rerun()
                else:
                    if ac1.button("Reactivate", key=f"bb_react_{eid}"):
                        new_exp = (date.today() + timedelta(days=DEFAULT_EXPIRY_DAYS)).isoformat()
                        _bb_update_entry(bb, eid, status="active", expiry_date=new_exp)
                        st.toast(f"{entry['horse_name']} reactivated", icon="✅")
                        st.rerun()
                if ac4.button("Delete", key=f"bb_del_{eid}", type="secondary"):
                    bb["entries"] = [e for e in bb["entries"] if e["id"] != eid]
                    _save_blackbook(bb)
                    st.toast(f"{entry['horse_name']} deleted", icon="🗑️")
                    st.rerun()

                # ── Edit form ─────────────────────────────────
                with st.expander("Edit Entry", expanded=False):
                    with st.form(f"bb_edit_{eid}", clear_on_submit=False):
                        ec1, ec2 = st.columns(2)
                        with ec1:
                            new_conf = st.selectbox(
                                "Confidence",
                                ["high", "medium", "low"],
                                index=["high", "medium", "low"].index(conf) if conf in ["high", "medium", "low"] else 1,
                                key=f"bb_ed_conf_{eid}",
                            )
                            new_source = st.text_input(
                                "Source Race",
                                value=entry.get("source_race", ""),
                                key=f"bb_ed_src_{eid}",
                            )
                        with ec2:
                            _cur_cat = entry.get("category", "Pre-Race")
                            new_category = st.selectbox(
                                "Category",
                                ["Pre-Race", "Post-Race"],
                                index=["Pre-Race", "Post-Race"].index(_cur_cat) if _cur_cat in ["Pre-Race", "Post-Race"] else 0,
                                key=f"bb_ed_cat_{eid}",
                            )
                            new_surface = st.selectbox(
                                "Preferred Surface",
                                [None, "Turf", "AWT"],
                                index=[None, "Turf", "AWT"].index(cond.get("preferred_surface"))
                                    if cond.get("preferred_surface") in [None, "Turf", "AWT"] else 0,
                                key=f"bb_ed_surf_{eid}",
                            )
                            cur_dists = ", ".join(str(d) for d in cond.get("preferred_distance", []))
                            new_dist_text = st.text_input(
                                "Preferred Distance(s)",
                                value=cur_dists,
                                key=f"bb_ed_dist_{eid}",
                            )
                        new_tags = st.multiselect(
                            "Tags", all_tags,
                            default=[t for t in entry.get("tags", []) if t in all_tags],
                            key=f"bb_ed_tags_{eid}",
                        )
                        new_reasoning = st.text_area(
                            "Reasoning",
                            value=entry.get("reasoning", ""),
                            height=100,
                            key=f"bb_ed_reason_{eid}",
                        )
                        if st.form_submit_button("Save Changes", type="primary"):
                            new_dists = [int(d.strip()) for d in new_dist_text.split(",")
                                         if d.strip().isdigit()]
                            _bb_update_entry(
                                bb, eid,
                                confidence=new_conf,
                                source_race=new_source,
                                category=new_category,
                                tags=new_tags,
                                reasoning=new_reasoning.strip(),
                                conditions={
                                    "preferred_distance": new_dists,
                                    "preferred_surface": new_surface,
                                },
                            )
                            st.toast(f"✓ {entry['horse_name']} updated", icon="✏️")
                            st.rerun()

    # ══ ADD / IMPORT TAB ═════════════════════════════════════
    with tab_add:
        st.markdown("### Add New Entry")
        with st.form("bb_add_form", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1:
                horse_name = st.text_input("Horse Name *")
                source_race = st.text_input("Source Race (e.g. 2026-04-01 R4)")
                confidence = st.selectbox("Confidence", ["high", "medium", "low"])
            with c2:
                add_category = st.selectbox("Category", ["Pre-Race", "Post-Race"],
                                            help="Pre-Race = spotted during form study / model review. "
                                                 "Post-Race = added after seeing a good run in results.")
                pref_surface = st.selectbox("Preferred Surface", [None, "Turf", "AWT"])
                dist_text = st.text_input("Preferred Distance(s) (comma-sep, e.g. 1200,1400)")

            selected_tags = st.multiselect("Tags", all_tags,
                                           help="Select reasoning patterns")
            reasoning = st.text_area("Reasoning *", height=100,
                                     placeholder="e.g. Held up in traffic R4, only clear last 100m. "
                                     "Ran 23.4 final sec — top-3 sectional. Watch for better draw.")

            submitted = st.form_submit_button("[ Save to Blackbook ]", type="primary")
            if submitted:
                if not horse_name.strip():
                    st.error("Horse name is required.")
                elif not reasoning.strip():
                    st.error("Reasoning is required.")
                else:
                    dists = []
                    if dist_text.strip():
                        dists = [int(d.strip()) for d in dist_text.split(",") if d.strip().isdigit()]
                    _bb_add_entry(bb, horse_name, reasoning, selected_tags, confidence,
                                  source_race=source_race, preferred_distance=dists,
                                  preferred_surface=pref_surface, category=add_category)
                    st.toast(f"✓ {horse_name.upper()} added to Blackbook", icon="⭐")
                    st.rerun()

        st.markdown("---")

        # ── Import from Excel / CSV ───────────────────────
        with st.expander("Bulk Import (Excel / CSV)"):
            st.markdown("Upload a file with columns: **horse_name**, **reasoning**, "
                        "**tags** (comma-separated), **confidence**, **source_race** (optional)")
            uploaded = st.file_uploader("Choose file", type=["xlsx", "csv"],
                                        key="bb_import_file")
            if uploaded:
                try:
                    if uploaded.name.endswith(".csv"):
                        imp_df = pd.read_csv(uploaded)
                    else:
                        imp_df = pd.read_excel(uploaded)
                    n_imported = 0
                    for _, row in imp_df.iterrows():
                        hn = str(row.get("horse_name", "")).strip()
                        rsn = str(row.get("reasoning", "")).strip()
                        if not hn or not rsn:
                            continue
                        tags_raw = str(row.get("tags", ""))
                        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
                        conf = str(row.get("confidence", "medium")).strip().lower()
                        src = str(row.get("source_race", "")).strip()
                        _bb_add_entry(bb, hn, rsn, tags, conf, source_race=src)
                        n_imported += 1
                    st.success(f"Imported {n_imported} entries.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Import failed: {e}")

        st.markdown("---")

        # ── Tag management ────────────────────────────────
        with st.expander("Manage Tags"):
            st.caption("Add, edit, or remove the tags available for Blackbook entries.")
            if tag_defs:
                for tname in sorted(tag_defs.keys()):
                    tc1, tc2, tc3 = st.columns([3, 6, 1])
                    tc1.markdown(f"`{tname}`")
                    tc2.markdown(tag_defs[tname])
                    if tc3.button("✕", key=f"tag_del_{tname}"):
                        del bb["tag_definitions"][tname]
                        _save_blackbook(bb)
                        st.toast(f"Removed tag '{tname}'", icon="🗑️")
                        st.rerun()
            else:
                st.info("No tags defined yet.")

            st.markdown("---")
            st.markdown("**Add / Edit Tag**")
            with st.form("tag_mgmt_form", clear_on_submit=True):
                tg_name = st.text_input("Tag name (lowercase, underscores)")
                tg_desc = st.text_input("Description")
                if st.form_submit_button("Save Tag"):
                    clean = tg_name.strip().lower().replace(" ", "_")
                    if clean and tg_desc.strip():
                        bb["tag_definitions"][clean] = tg_desc.strip()
                        _save_blackbook(bb)
                        st.toast(f"Tag '{clean}' saved", icon="🏷️")
                        st.rerun()
                    else:
                        st.error("Both name and description are required.")

    # ══ ANALYTICS TAB ════════════════════════════════════════
    with tab_analytics:
        all_entries = bb["entries"]
        all_perfs = [(e, p) for e in all_entries for p in e.get("performances", [])]

        if not all_perfs:
            st.info("No performance data yet. Add entries and record results to see analytics.")
        else:
            st.markdown("### Tag Hit-Rates")
            tag_stats = defaultdict(lambda: {"total": 0, "top3": 0, "top5": 0, "wins": 0})
            for e, p in all_perfs:
                try:
                    place = int(str(p.get("finish", "99")).strip().rstrip("stndrdth"))
                except ValueError:
                    place = 99
                for tag in e.get("tags", []):
                    tag_stats[tag]["total"] += 1
                    if place <= 1:
                        tag_stats[tag]["wins"] += 1
                    if place <= 3:
                        tag_stats[tag]["top3"] += 1
                    if place <= 5:
                        tag_stats[tag]["top5"] += 1

            t_rows = []
            for tag, st_d in sorted(tag_stats.items()):
                n = st_d["total"]
                t_rows.append({
                    "Tag": tag,
                    "Runs": n,
                    "Win%": f"{st_d['wins']/n*100:.0f}%" if n else "—",
                    "Top-3%": f"{st_d['top3']/n*100:.0f}%" if n else "—",
                    "Top-5%": f"{st_d['top5']/n*100:.0f}%" if n else "—",
                })
            st.dataframe(pd.DataFrame(t_rows), use_container_width=True, hide_index=True)

            st.markdown("---")
            st.markdown("### Confidence Calibration")
            conf_stats = defaultdict(lambda: {"total": 0, "top3": 0, "wins": 0})
            for e, p in all_perfs:
                try:
                    place = int(str(p.get("finish", "99")).strip().rstrip("stndrdth"))
                except ValueError:
                    place = 99
                conf = e.get("confidence", "medium")
                conf_stats[conf]["total"] += 1
                if place <= 1:
                    conf_stats[conf]["wins"] += 1
                if place <= 3:
                    conf_stats[conf]["top3"] += 1

            c_rows = []
            for conf in ["high", "medium", "low"]:
                sd = conf_stats[conf]
                n = sd["total"]
                c_rows.append({
                    "Confidence": conf.title(),
                    "Runs": n,
                    "Win%": f"{sd['wins']/n*100:.0f}%" if n else "—",
                    "Top-3%": f"{sd['top3']/n*100:.0f}%" if n else "—",
                })
            st.dataframe(pd.DataFrame(c_rows), use_container_width=True, hide_index=True)

            st.markdown("---")
            st.markdown("### Model Alignment")
            agree = 0
            bb_only = 0
            agree_wins = 0
            bb_only_wins = 0
            for e, p in all_perfs:
                try:
                    place = int(str(p.get("finish", "99")).strip().rstrip("stndrdth"))
                except ValueError:
                    place = 99
                model_rank = p.get("model_rank") or 99
                is_win = place <= 3
                if model_rank <= 4:
                    agree += 1
                    if is_win:
                        agree_wins += 1
                else:
                    bb_only += 1
                    if is_win:
                        bb_only_wins += 1

            c1, c2, c3 = st.columns(3)
            c1.metric("Aligned (model top-4 + BB)", agree,
                      help="Horse in both your blackbook and model top 4")
            c2.metric("BB-Only (model missed)", bb_only,
                      help="Horse in your blackbook but not in model top 4")
            c3.metric("Total Tracked Runs", len(all_perfs))

            mc1, mc2 = st.columns(2)
            mc1.metric("Aligned Top-3%",
                       f"{agree_wins/agree*100:.0f}%" if agree else "—")
            mc2.metric("BB-Only Top-3%",
                       f"{bb_only_wins/bb_only*100:.0f}%" if bb_only else "—")

    # ══ EXCEPTIONAL PERFORMERS TAB ═══════════════════════════
    with tab_exceptional:
        st.markdown("### ★ Exceptional Performers — Blackbook Candidates")
        st.caption("Horses flagged via objective post-race criteria from backtest analysis")

        bt_files = load_backtest_files()
        meeting_bts = bt_files.get("meeting", {})

        # Build list of (date_key, exc_list) pairs that have exceptional performers
        exc_dates = []
        for dk in sorted(meeting_bts.keys(), reverse=True):
            exc = meeting_bts[dk].get("exceptional_performers", [])
            if exc:
                # dk is like "20260412_v4" → extract date portion
                date_part = dk.split("_")[0] if "_" in dk else dk
                try:
                    d = date.fromisoformat(f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}")
                    label = d.strftime("%d %b %Y")
                except (ValueError, IndexError):
                    label = dk
                exc_dates.append({"key": dk, "label": label, "exc": exc})

        if not exc_dates:
            st.info("No exceptional performers found in any backtest. "
                    "Run a backtest after a meeting to identify candidates.")
        else:
            date_options = {ed["label"]: ed for ed in exc_dates}
            sel_label = st.selectbox(
                "Meeting date", list(date_options.keys()), index=0,
                key="bb_exc_date_select",
            )
            sel_ed = date_options[sel_label]
            exc = sel_ed["exc"]

            st.markdown(
                f'<div class="page-subtitle">{sel_label} &nbsp;·&nbsp; '
                f'{len(exc)} horse(s) flagged</div>',
                unsafe_allow_html=True,
            )

            for i, ep in enumerate(sorted(exc, key=lambda x: -x.get("score", 0))):
                hname = ep.get("horse", "?")
                race_lbl = f'R{ep.get("race", "?")} {ep.get("distance", "?")}m'
                place = ep.get("place", "?")
                odds = ep.get("odds")
                draw = ep.get("draw", "?")
                score = ep.get("score", 0)
                reasons = ep.get("reasons", [])

                odds_str = f"${odds:.1f}" if odds else "—"
                score_colour = "#22c55e" if score >= 3 else "#f59e0b" if score >= 2 else "#888"
                st.markdown(
                    f'<div style="border-left:3px solid {score_colour};padding:4px 10px;'
                    f'margin:6px 0;border-radius:0 4px 4px 0;'
                    f'background:var(--secondary-background-color,rgba(128,128,128,0.05))">'
                    f'<strong>{hname}</strong> &nbsp; {race_lbl} &nbsp; '
                    f'P{place} &nbsp; {odds_str} &nbsp; Dr{draw} &nbsp; '
                    f'<span style="color:{score_colour};font-weight:700">'
                    f'Score {score:.2f}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                for reason in reasons:
                    st.caption(f"  ★ {reason}")
                # Add to Blackbook button
                already = any(
                    e["horse_name"] == hname.strip().upper() and e["status"] == "active"
                    for e in bb.get("entries", [])
                )
                if already:
                    st.caption("  ✓ Already in Blackbook")
                else:
                    if st.button(f"Add {hname} to Blackbook",
                                 key=f"bb_exc_{sel_ed['key']}_{i}_{hname}",
                                 type="tertiary"):
                        dist_val = ep.get("distance")
                        dists = [int(dist_val)] if dist_val else []
                        _bb_add_entry(
                            bb, hname,
                            reasoning=f"Exceptional performer: {'; '.join(reasons[:3])}",
                            tags=["exceptional_performer"],
                            confidence="medium",
                            source_race=race_lbl,
                            preferred_distance=dists,
                            category="Post-Race",
                        )
                        st.toast(f"✓ {hname.upper()} added to Blackbook", icon="⭐")
                        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Results browser page
# ══════════════════════════════════════════════════════════════════════════════

def page_results():
    st.markdown('<div class="page-title">Race Results</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">Browse scraped results &middot; add horses to Blackbook</div>', unsafe_allow_html=True)

    # Sidebar: scrape + date select
    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
    st.sidebar.markdown('<div class="sb-nav-section">Results Data</div>', unsafe_allow_html=True)
    scrape_date = st.sidebar.date_input("Date", value=date.today(), key="res_scrape_date")
    if st.sidebar.button("[ Scrape Results ]", use_container_width=True, key="res_btn_scrape"):
        _run_results_scraper(scrape_date.isoformat(), full=True)
        st.cache_data.clear()
        st.rerun()

    res_dates = find_results_dates()
    if not res_dates:
        st.info("No scraped results found. Use the sidebar to scrape results for a date.")
        return

    date_labels = {dc: f"{dc[:4]}-{dc[4:6]}-{dc[6:]}" for dc in sorted(res_dates, reverse=True)}
    selected_dc = st.selectbox("Meeting date:", list(date_labels.keys()),
                               format_func=lambda k: date_labels[k], key="res_date_select")

    data = _load_results_json(selected_dc)
    if not data:
        st.error("Could not load results file.")
        return

    races = data.get("races", [])
    if not races:
        st.warning("No races found in results file.")
        return

    st.markdown(
        f'<div style="font-size:0.85em;opacity:0.6;margin-bottom:10px">'
        f'Venue: {data.get("venue","?")} &nbsp;·&nbsp; '
        f'{data.get("n_races",0)} races &nbsp;·&nbsp; '
        f'Scraped: {data.get("scraped_at","?")[:16]}'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Load BB and prediction data
    bb = _load_blackbook()
    _bb_expire_stale(bb)
    bb_names = {e["horse_name"].upper() for e in _bb_active_entries(bb)}

    pred_path = REPORTS / f"race_day_report_{selected_dc}_v4.4.json"
    if not pred_path.exists():
        pred_path = REPORTS / f"race_day_report_{selected_dc}_v3.4.8.json"
    model_ranks = {}
    if pred_path.exists():
        try:
            with open(pred_path, "r", encoding="utf-8") as f:
                pred_data = json.load(f)
            for race in pred_data.get("races", []):
                for pick in race.get("picks", []):
                    model_ranks[(race["race_number"],
                                 pick["horse_name"].upper())] = pick["rank"]
        except Exception:
            pass

    # ── Race tab selector ────────────────────────────────────────────────
    race_nums = [r.get("race_number", i + 1) for i, r in enumerate(races)]
    if "res_active_rn" not in st.session_state or st.session_state.get("res_active_dc") != selected_dc:
        st.session_state["res_active_rn"] = race_nums[0] if race_nums else None
        st.session_state["res_active_dc"] = selected_dc

    tab_cols = st.columns(len(race_nums))
    for i, rn in enumerate(race_nums):
        with tab_cols[i]:
            is_active = st.session_state["res_active_rn"] == rn
            if st.button(f"R{rn}", key=f"res_tab_{selected_dc}_{rn}",
                         use_container_width=True,
                         type="primary" if is_active else "secondary"):
                st.session_state["res_active_rn"] = rn
                st.rerun()

    selected_rn = st.session_state["res_active_rn"]
    race = next((r for r in races if r.get("race_number") == selected_rn), None)
    if not race:
        return

    # ── Race header ───────────────────────────────────────────────────────
    st.markdown(
        f'<div class="race-hdr-block">'
        f'<div class="race-hdr-title">R{race["race_number"]} &mdash; {race.get("race_name","")}</div>'
        f'<div class="race-hdr-meta">'
        f'<span>{race.get("distance","?")}m</span>'
        f'<span>{"AWT" if race.get("is_awt") else "Turf"}</span>'
        f'<span>Course {race.get("race_course","?")}</span>'
        f'<span>Class {race.get("race_class","?")}</span>'
        f'<span>Going: {race.get("going","?")}</span>'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    # ── Results table ────────────────────────────────────────────────────
    runners = race.get("runners", [])
    res_rows = []
    for r in runners:
        hname = r.get("horse_name", "")
        in_bb = hname.upper() in bb_names
        m_rank = model_ranks.get((race["race_number"], hname.upper()), "—")
        positions_list = r.get("positions", [])
        running_pos = "-".join(p for p in positions_list if p) if positions_list else r.get("running_position", "")
        res_rows.append({
            "Place": r.get("place", ""),
            "No": r.get("horse_no", ""),
            "Horse": hname,
            "BB": "BB" if in_bb else "",
            "Model Rk": m_rank,
            "Jockey": r.get("jockey", ""),
            "Trainer": r.get("trainer", ""),
            "Wt": r.get("actual_weight", ""),
            "Draw": r.get("draw", ""),
            "Running Pos": running_pos,
            "Finish Time": r.get("finish_time", ""),
            "Win Odds": r.get("win_odds", ""),
            "LBW": r.get("lbw", ""),
        })

    st.dataframe(pd.DataFrame(res_rows), use_container_width=True, hide_index=True)

    _xl_bytes = _results_json_to_excel_bytes(REPORTS / f"results_{selected_dc}.json")
    if _xl_bytes:
        st.download_button(
            "[ Download Results (Excel) ]", _xl_bytes,
            file_name=f"results_{selected_dc}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="res_dl_xlsx",
        )

    # ── Running Position Photo ───────────────────────────────────────────
    rp_photo_path = BASE / "running_position_photos" / selected_dc / f"R{selected_rn}.jpg"
    with st.expander("📸 Running Position Photo (HKJC)", expanded=False):
        if rp_photo_path.exists():
            st.caption(
                "Left = front of pack · right = back · top = inside rail "
                "(except Sha Tin 1000m). Use to validate speed-map predictions "
                "and check if horses ran rail, 1-out, or 3–4 wide."
            )
            st.image(str(rp_photo_path), use_container_width=True)
        else:
            st.info(
                f"No photo cached for R{selected_rn}. "
                f"Run `python scrape_hkjc_rp_photos.py --date {selected_dc[:4]}-{selected_dc[4:6]}-{selected_dc[6:]}` "
                f"to download."
            )

    # ── Race Replay (HKJC video) ─────────────────────────────────────────
    _video_url = (
        "https://racing.hkjc.com/contentAsset/videoplayer_v4/"
        "video-player-iframe_v4.html?type=replay-full"
        f"&date={selected_dc}&no={int(selected_rn):02d}&lang=eng"
        "&noPTbar=false&noLeading=false&videoParam=PAD"
    )
    with st.expander("🎬 Race Replay (HKJC video)", expanded=False):
        st.markdown(
            f'<a href="{_video_url}" target="_blank" rel="noopener noreferrer" '
            f'style="display:inline-block;padding:8px 14px;background:#1f6feb;'
            f'color:#fff;border-radius:6px;text-decoration:none;font-weight:600;">'
            f'▶ Watch Full Replay (opens HKJC player)</a>',
            unsafe_allow_html=True,
        )
        st.caption(
            "Opens the HKJC Multi-Angle Race Replay player in a new tab. "
            f"URL: `{_video_url}`"
        )

    # ── AI Race Commentary ───────────────────────────────────────────────
    _comm_path = BASE / "reports" / f"commentary_{selected_dc}.json"
    _comm_race = None
    if _comm_path.exists():
        try:
            _comm_all = json.loads(_comm_path.read_text(encoding="utf-8"))
            _comm_race = next(
                (r for r in _comm_all.get("races", []) if r.get("race_number") == selected_rn),
                None,
            )
        except Exception:
            _comm_race = None
    with st.expander("📝 Race Commentary & Blackbook Suggestions", expanded=False):
        if _comm_race is None:
            st.info(
                f"No commentary cached for R{selected_rn}. Run "
                f"`python race_commentary.py --date {selected_dc[:4]}-{selected_dc[4:6]}-{selected_dc[6:]}` "
                f"after scraping incidents + photos."
            )
        else:
            st.markdown(f"**Narrative**")
            st.write(_comm_race.get("narrative", ""))
            # Per-horse table (short blurb + tags)
            horses_data = _comm_race.get("horses", [])
            if horses_data:
                _hrows = []
                for h in horses_data:
                    _hrows.append({
                        "Pl": h.get("place", "?"),
                        "No": h.get("horse_no", ""),
                        "Horse": h.get("horse_name", ""),
                        "Short": h.get("short", "") or "—",
                        "Tags": ", ".join(h.get("tags", [])) or "—",
                        "Score": h.get("polarity_score", 0),
                    })
                st.markdown("**Per-horse short commentary**")
                st.dataframe(pd.DataFrame(_hrows), use_container_width=True, hide_index=True)
            # Blackbook suggestions
            bb_sugg = _comm_race.get("blackbook_suggestions", [])
            if bb_sugg:
                st.markdown("**🔖 Blackbook suggestions** (rule-based from incident + trip)")
                for s in bb_sugg:
                    sign = "➖" if s.get("polarity") == "-" else "➕"
                    st.markdown(
                        f"- {sign} **{s['horse_name']}** (P{s.get('place','?')}) — {s['reason']}"
                    )
            # Raw per-horse incident text (collapsible)
            with st.expander("Full incident text per horse", expanded=False):
                for h in horses_data:
                    if h.get("incident_text"):
                        st.markdown(
                            f"**P{h.get('place','?')} #{h.get('horse_no','')} "
                            f"{h.get('horse_name','')}**"
                        )
                        st.caption(h["incident_text"])

    st.markdown('<hr class="term-divider">', unsafe_allow_html=True)

    # ── Add to Blackbook ─────────────────────────────────────────────────
    st.markdown("**ADD TO BLACKBOOK**")
    runner_names = [r.get("horse_name", "") for r in runners if r.get("horse_name")]
    tag_defs = bb.get("tag_definitions", {})
    all_tags = sorted(tag_defs.keys())

    with st.form(f"bb_from_results_{selected_dc}_{selected_rn}", clear_on_submit=True):
        sel_horse = st.selectbox("Select horse:", runner_names, key="res_bb_horse")
        sel_tags = st.multiselect("Tags:", all_tags, key="res_bb_tags")
        sel_conf = st.selectbox("Confidence:", ["high", "medium", "low"], key="res_bb_conf")
        sel_reason = st.text_area("Reasoning *", height=80, key="res_bb_reason",
                                  placeholder="Why is this horse worth following?")
        if st.form_submit_button("[ Add to Blackbook ]", type="primary"):
            if not sel_reason.strip():
                st.error("Reasoning is required.")
            else:
                src = f"{selected_dc[:4]}-{selected_dc[4:6]}-{selected_dc[6:]} R{selected_rn}"
                _bb_add_entry(bb, sel_horse, sel_reason, sel_tags, sel_conf,
                              source_race=src)
                st.toast(f"✓ {sel_horse.upper()} added to Blackbook", icon="⭐")
                st.rerun()

    # ── Record performance for existing BB entries ───────────────────────
    bb_in_race = [(r_item, e) for r_item in runners
                  for e in _bb_active_entries(bb)
                  if e["horse_name"] == r_item.get("horse_name", "").upper()]
    if bb_in_race:
        st.markdown('<hr class="term-divider">', unsafe_allow_html=True)
        st.markdown("**RECORD BLACKBOOK PERFORMANCE**")
        for r_item, e in bb_in_race:
            place = r_item.get("place", "")
            m_rank = model_ranks.get((race["race_number"],
                                     r_item["horse_name"].upper()), "?")
            already = any(
                p.get("date") == f"{selected_dc[:4]}-{selected_dc[4:6]}-{selected_dc[6:]}"
                and p.get("race_number") == race["race_number"]
                for p in e.get("performances", [])
            )
            if already:
                st.markdown(f"[OK] **{e['horse_name']}** — already recorded for this race.")
                continue

            try:
                place_num = int(str(place).strip().rstrip("stndrdth"))
            except (ValueError, TypeError):
                place_num = 99
            verdict = "VALIDATED" if place_num <= 3 else ("PARTIAL" if place_num <= 5 else "MISSED")

            with st.form(f"perf_{e['id']}_{selected_dc}_{selected_rn}"):
                st.markdown(f"**{e['horse_name']}** — Finished **{place}** "
                            f"(Model rank: #{m_rank}) — Verdict: **{verdict}**")
                perf_notes = st.text_input("Notes (optional)", key=f"pnote_{e['id']}_{selected_dc}")
                if st.form_submit_button("[ Save Performance ]"):
                    perf = {
                        "date": f"{selected_dc[:4]}-{selected_dc[4:6]}-{selected_dc[6:]}",
                        "race_number": race["race_number"],
                        "finish": place,
                        "model_rank": m_rank if m_rank != "?" else None,
                        "bb_verdict": verdict,
                        "notes": perf_notes.strip(),
                    }
                    _bb_add_performance(bb, e["id"], perf)
                    st.success(f"Performance recorded for {e['horse_name']}!")
                    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Form Guide PDF builder
# ══════════════════════════════════════════════════════════════════════════════

def _build_form_guide_pdf(meeting_data: dict, racecard: dict | None,
                          form_db: pd.DataFrame, race_idx: dict,
                          bb_lookup: dict, date_iso: str) -> bytes:
    """Generate a full form guide PDF for all races in a meeting.

    Returns PDF bytes.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak,
        KeepTogether,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=8*mm, rightMargin=8*mm,
                            topMargin=10*mm, bottomMargin=10*mm)
    styles = getSampleStyleSheet()
    sN = styles["Normal"]
    sN.fontSize = 6.5
    sN.leading = 8.5

    sTitle = ParagraphStyle("FGTitle", parent=styles["Heading1"],
                            fontSize=13, leading=15, spaceAfter=3)
    sRace = ParagraphStyle("FGRace", parent=styles["Heading2"],
                           fontSize=10, leading=12, spaceAfter=2,
                           textColor=colors.HexColor("#e63946"))
    sHorse = ParagraphStyle("FGHorse", parent=styles["Heading3"],
                            fontSize=8, leading=10, spaceBefore=5, spaceAfter=1)
    sSmall = ParagraphStyle("FGSmall", parent=sN, fontSize=6, leading=7.5,
                            textColor=colors.grey)
    sDeb = ParagraphStyle("FGDeb", parent=sN, fontSize=6.5, leading=8.5,
                          textColor=colors.grey, fontStyle="italic")

    meeting_date = date.fromisoformat(date_iso)
    races = meeting_data.get("races", [])
    elements = []

    elements.append(Paragraph(
        f"Form Guide — {meeting_data.get('meeting_title', '')}",
        sTitle))
    elements.append(Paragraph(
        f"Generated {datetime.now().strftime('%d %b %Y %H:%M')} · "
        f"{len(form_db):,} historical records",
        sSmall))
    elements.append(Spacer(1, 3*mm))

    col_headers = ["Date", "Pl", "Dist", "Trk", "Crs", "Gng", "Cls",
                    "Jockey", "Rtg", "Wt", "Gt", "Positions", "Mrgn", "Time"]
    col_widths = [30, 13, 20, 15, 15, 22, 15, 46, 17, 17, 13, 40, 24, 28]
    n_cols = len(col_headers)

    sTop5 = ParagraphStyle("FGTop5", parent=sN, fontSize=5.8, leading=8,
                           textColor=colors.HexColor("#444444"))

    for race in races:
        rn = race["race_number"]
        cls_str = f"Class {race['race_class']}" if race.get("race_class") else "Group"
        surface = "AWT" if race.get("is_awt") else "Turf"
        elements.append(Paragraph(
            f"R{rn} — {race.get('race_name', '')} · "
            f"{race['distance']}m {surface} ({race['race_course']}) · {cls_str}",
            sRace))

        # Gather horses
        horses = []
        if racecard:
            for rc_race in racecard.get("races", []):
                if rc_race["meta"]["race_number"] == rn:
                    horses = [h for h in rc_race["horses"]
                              if not h.get("is_standby")]
                    break
        if not horses:
            horses = [
                {"horse_name": p["horse_name"], "horse_no": p["horse_no"],
                 "jockey": p.get("jockey", ""), "draw": p.get("draw", ""),
                 "rating": "?", "last_6_runs": ""}
                for p in race.get("picks", [])
            ]

        for horse in horses:
            hname = horse["horse_name"]
            hno = horse.get("horse_no", "?")
            h_draw = horse.get("draw", "?")
            h_rtg = horse.get("rating", "?")
            h_jockey = horse.get("jockey", "?")
            h_weight = horse.get("weight", "?")
            h_trainer = horse.get("trainer", "?")
            h_ow = horse.get("overweight", "")
            bb_tag = " [BB]" if bb_lookup.get(hname.strip().upper()) else ""
            ow_part = f" ({h_ow})" if h_ow else ""

            elements.append(Paragraph(
                f"<b>#{hno} {hname}{bb_tag}</b> · RTG {h_rtg} · {h_weight} LB · "
                f"{h_jockey}{ow_part} · {h_trainer} · GT {h_draw}",
                sHorse))

            mask = form_db["horse_name_upper"] == hname.strip().upper()
            horse_hist = form_db[mask & (form_db["race_date"] < meeting_date)]
            horse_hist = horse_hist.sort_values("race_date", ascending=False).head(6).sort_values("race_date", ascending=True)

            if horse_hist.empty:
                elements.append(Paragraph("Debutant — no historical form", sDeb))
                continue

            h_up = hname.strip().upper()
            rows_data = [col_headers]
            winner_rows = []   # row indices where place == "1"
            top5_rows = []     # row indices that are top-5 spanning rows

            for _, row in horse_hist.iterrows():
                rd = row["race_date"]
                rnum = row["race_number"]
                ri = race_idx.get((rd, rnum), {"top5": [], "margin_2nd": "-"})
                date_disp = rd.strftime("%d/%m/%y") if hasattr(rd, "strftime") else str(rd)
                trk = str(row.get("race_track", "?"))[:2]
                crs = str(row.get("race_course", "?"))
                dist = int(row["distance"]) if pd.notna(row.get("distance")) else "?"
                going = str(row.get("going", "?"))
                try:
                    cls_val = str(int(float(row.get("race_class", "?"))))
                except (ValueError, TypeError):
                    cls_val = str(row.get("race_class", "?"))
                jock = str(row.get("jockey", "?"))
                try:
                    rtg = str(int(float(row["rating"]))) if pd.notna(row.get("rating")) else "?"
                except (ValueError, TypeError):
                    rtg = str(row.get("rating", "?"))
                try:
                    gate = str(int(float(row["draw"]))) if pd.notna(row.get("draw")) else "?"
                except (ValueError, TypeError):
                    gate = "?"
                pos = _fmt_positions(row.get("running_positions"))
                margin = _fmt_margin(row.get("place"), row.get("lbw"), ri)
                ftime = _fmt_time(row.get("finish_time_seconds"))
                try:
                    place_val = str(int(row["place_num"])) if pd.notna(row.get("place_num")) else "?"
                except (ValueError, TypeError):
                    place_val = "?"
                try:
                    wt = str(int(float(row["actual_weight"]))) if pd.notna(row.get("actual_weight")) else "?"
                except (ValueError, TypeError):
                    wt = "?"

                data_idx = len(rows_data)
                rows_data.append([
                    date_disp, place_val, str(dist), trk, crs, going,
                    cls_val, jock, rtg, wt, gate, pos, margin, ftime,
                ])
                if place_val == "1":
                    winner_rows.append(data_idx)

                # Build top-5 spanning row — bold the featured horse
                top5 = ri.get("top5", [])
                t5_parts = []
                for p_num, name in top5:
                    name_str = str(name)
                    if name_str.strip().upper() == h_up:
                        t5_parts.append(f"<b>{p_num}. {name_str}</b>")
                    else:
                        t5_parts.append(f"{p_num}. {name_str}")
                t5_text = "  ·  ".join(t5_parts) if t5_parts else "—"
                t5_idx = len(rows_data)
                rows_data.append([Paragraph(t5_text, sTop5)] + [""] * (n_cols - 1))
                top5_rows.append(t5_idx)

            tbl = Table(rows_data, colWidths=col_widths, repeatRows=1)
            style_cmds = [
                ("FONTSIZE", (0, 0), (-1, -1), 6.5),
                ("LEADING", (0, 0), (-1, -1), 8),
                ("FONTSIZE", (0, 0), (-1, 0), 6),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f5f0e8")),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("ALIGN", (7, 0), (7, -1), "LEFT"),  # Jockey left-aligned
            ]
            # Highlight winning margin in red/bold
            for w_idx in winner_rows:
                style_cmds.append(("TEXTCOLOR", (12, w_idx), (12, w_idx), colors.HexColor("#ef4444")))
                style_cmds.append(("FONTNAME", (12, w_idx), (12, w_idx), "Helvetica-Bold"))
            # Style top-5 spanning rows
            for t5_idx in top5_rows:
                style_cmds += [
                    ("SPAN", (0, t5_idx), (-1, t5_idx)),
                    ("ALIGN", (0, t5_idx), (0, t5_idx), "LEFT"),
                    ("VALIGN", (0, t5_idx), (0, t5_idx), "MIDDLE"),
                    ("BACKGROUND", (0, t5_idx), (-1, t5_idx), colors.HexColor("#fafafa")),
                    ("TOPPADDING", (0, t5_idx), (-1, t5_idx), 2),
                    ("BOTTOMPADDING", (0, t5_idx), (-1, t5_idx), 5),
                    ("LEFTPADDING", (0, t5_idx), (-1, t5_idx), 10),
                    ("LINEBELOW", (0, t5_idx), (-1, t5_idx), 0.6, colors.HexColor("#bbbbbb")),
                ]

            tbl.setStyle(TableStyle(style_cmds))
            elements.append(tbl)
            elements.append(Spacer(1, 1.5*mm))

        elements.append(PageBreak())

    doc.build(elements)
    return buf.getvalue()


def _build_form_guide_pdf_from_cache(fg_cache: dict, bb_lookup: dict) -> bytes:
    """Generate form guide PDF from pre-built JSON cache (no XLSX needed)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=8*mm, rightMargin=8*mm,
                            topMargin=10*mm, bottomMargin=10*mm)
    styles = getSampleStyleSheet()
    sN = styles["Normal"]
    sN.fontSize = 6.5
    sN.leading = 8.5
    sTitle = ParagraphStyle("FGTitle2", parent=styles["Heading1"], fontSize=13, leading=15, spaceAfter=3)
    sRace = ParagraphStyle("FGRace2", parent=styles["Heading2"], fontSize=10, leading=12,
                           spaceAfter=2, textColor=colors.HexColor("#e63946"))
    sHorse = ParagraphStyle("FGHorse2", parent=styles["Heading3"], fontSize=8, leading=10,
                            spaceBefore=5, spaceAfter=1)
    sSmall = ParagraphStyle("FGSmall2", parent=sN, fontSize=6, leading=7.5, textColor=colors.grey)
    sDeb = ParagraphStyle("FGDeb2", parent=sN, fontSize=6.5, leading=8.5, textColor=colors.grey)
    sTop5 = ParagraphStyle("FGTop5c", parent=sN, fontSize=5.8, leading=8,
                           textColor=colors.HexColor("#444444"))

    col_headers = ["Date", "Pl", "Dist", "Trk", "Crs", "Gng", "Cls",
                    "Jockey", "Rtg", "Wt", "Gt", "Positions", "Mrgn", "Time"]
    col_widths = [30, 13, 20, 15, 15, 22, 15, 46, 17, 17, 13, 40, 24, 28]
    n_cols = len(col_headers)

    meeting_title = fg_cache.get("meeting_title", "")
    date_iso = fg_cache.get("date", "")
    elements = []
    elements.append(Paragraph(f"Form Guide — {meeting_title}", sTitle))
    elements.append(Paragraph(
        f"Generated {datetime.now().strftime('%d %b %Y %H:%M')} · Pre-built cache",
        sSmall))
    elements.append(Spacer(1, 4*mm))

    for race in fg_cache.get("races", []):
        rn = race["race_number"]
        cls_str = f"Class {race['race_class']}" if race.get("race_class") else "Group"
        surface = "AWT" if race.get("is_awt") else "Turf"
        elements.append(Paragraph(
            f"R{rn} \u2014 {race.get('race_name', '')} \u00b7 "
            f"{race.get('distance', '')}m {surface} ({race.get('race_course', '')}) \u00b7 {cls_str}",
            sRace))

        for horse in race.get("horses", []):
            hname = horse["horse_name"]
            hno = horse.get("horse_no", "?")
            h_draw = horse.get("draw", "?")
            h_rtg = horse.get("rating", "?")
            h_jockey = horse.get("jockey", "?")
            h_weight = horse.get("weight", "?")
            h_trainer = horse.get("trainer", "?")
            h_ow = horse.get("overweight", "")
            bb_tag = " [BB]" if bb_lookup.get(hname.strip().upper()) else ""
            ow_part = f" ({h_ow})" if h_ow else ""

            elements.append(Paragraph(
                f"<b>#{hno} {hname}{bb_tag}</b> \u00b7 RTG {h_rtg} \u00b7 {h_weight} LB \u00b7 "
                f"{h_jockey}{ow_part} \u00b7 {h_trainer} \u00b7 GT {h_draw}",
                sHorse))

            runs = horse.get("runs", [])
            if horse.get("is_debutant", not bool(runs)):
                elements.append(Paragraph("Debutant \u2014 no historical form", sDeb))
                continue

            h_up = hname.strip().upper()
            rows_data = [col_headers]
            winner_rows = []
            top5_rows = []

            for run in runs:
                try:
                    rd_date = date.fromisoformat(run["date"])
                    date_disp = rd_date.strftime("%d/%m/%y")
                except (ValueError, KeyError):
                    date_disp = str(run.get("date", "?"))[:8]
                place_val = str(run.get("place", "?"))
                dist = str(run["distance"]) if run.get("distance") else "?"
                trk = str(run.get("track", "?"))
                crs = str(run.get("course", "?"))
                going = str(run.get("going", "?"))
                cls_val = str(run.get("class", "?"))
                jock = str(run.get("jockey", "?"))
                rtg = str(run.get("rating", "?"))
                wt = str(run.get("actual_weight", "?"))
                gate = str(run.get("draw", "?"))
                pos = str(run.get("positions", "-"))
                margin = str(run.get("margin", "-"))
                ftime = str(run.get("time", "-"))

                data_idx = len(rows_data)
                rows_data.append([
                    date_disp, place_val, dist, trk, crs, going,
                    cls_val, jock, rtg, wt, gate, pos, margin, ftime,
                ])
                if place_val == "1":
                    winner_rows.append(data_idx)

                top5 = run.get("top5") or []
                t5_parts = []
                for entry in top5:
                    p_num, name = entry[0], entry[1]
                    name_str = str(name)
                    if name_str.strip().upper() == h_up:
                        t5_parts.append(f"<b>{p_num}. {name_str}</b>")
                    else:
                        t5_parts.append(f"{p_num}. {name_str}")
                t5_text = "  \u00b7  ".join(t5_parts) if t5_parts else "\u2014"
                t5_idx = len(rows_data)
                rows_data.append([Paragraph(t5_text, sTop5)] + [""] * (n_cols - 1))
                top5_rows.append(t5_idx)

            tbl = Table(rows_data, colWidths=col_widths, repeatRows=1)
            style_cmds = [
                ("FONTSIZE", (0, 0), (-1, -1), 6.5),
                ("LEADING", (0, 0), (-1, -1), 8),
                ("FONTSIZE", (0, 0), (-1, 0), 6),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f5f0e8")),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("ALIGN", (7, 0), (7, -1), "LEFT"),
            ]
            for w_idx in winner_rows:
                style_cmds.append(("TEXTCOLOR", (12, w_idx), (12, w_idx), colors.HexColor("#ef4444")))
                style_cmds.append(("FONTNAME", (12, w_idx), (12, w_idx), "Helvetica-Bold"))
            for t5_idx in top5_rows:
                style_cmds += [
                    ("SPAN", (0, t5_idx), (-1, t5_idx)),
                    ("ALIGN", (0, t5_idx), (0, t5_idx), "LEFT"),
                    ("VALIGN", (0, t5_idx), (0, t5_idx), "MIDDLE"),
                    ("BACKGROUND", (0, t5_idx), (-1, t5_idx), colors.HexColor("#fafafa")),
                    ("TOPPADDING", (0, t5_idx), (-1, t5_idx), 2),
                    ("BOTTOMPADDING", (0, t5_idx), (-1, t5_idx), 5),
                    ("LEFTPADDING", (0, t5_idx), (-1, t5_idx), 10),
                    ("LINEBELOW", (0, t5_idx), (-1, t5_idx), 0.6, colors.HexColor("#bbbbbb")),
                ]
            tbl.setStyle(TableStyle(style_cmds))
            elements.append(tbl)
            elements.append(Spacer(1, 1.5*mm))

        elements.append(PageBreak())

    doc.build(elements)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
# Live Feed page — real-time race analysis engine
# ══════════════════════════════════════════════════════════════════════════════

def _live_load_analysis(dstr: str) -> dict | None:
    """Run live_analysis engine for a meeting date."""
    try:
        from live_analysis import run_live_analysis
        result = run_live_analysis(dstr)
        if "error" in result:
            return None
        return result
    except Exception:
        return None


def page_live_feed():
    st.markdown('<div class="page-title">Live Feed</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="page-subtitle">Real-time race analysis &middot; '
        'convergence detection &middot; pattern recognition</div>',
        unsafe_allow_html=True,
    )

    # Find today's or most recent meeting
    meeting_info = _overview_find_today_meeting()
    if not meeting_info:
        st.info("No meeting report found. Run analysis from the Race Day page first.")
        return

    data = load_meeting_data(meeting_info["file"])
    races = data.get("races", [])
    dstr = meeting_info["date_str"]
    nice_date = f"{dstr[:4]}-{dstr[4:6]}-{dstr[6:]}"

    # Also load SARR if available
    sarr_data = load_sarr_data(dstr)

    st.markdown(f"**Meeting:** {data.get('meeting_title', nice_date)}")

    # ── Controls ───────────────────────────────────────────
    col_s1, col_s2, col_s3 = st.columns([2, 2, 4])
    with col_s1:
        scrape_date = f"{dstr[:4]}-{dstr[4:6]}-{dstr[6:]}"
        if st.button("🔄 Scrape Results", key="live_scrape"):
            _run_results_scraper(scrape_date)
            st.session_state["live_last_refresh"] = datetime.now().strftime("%H:%M:%S")
            st.rerun()
    with col_s2:
        if st.button("📊 Run Analysis", key="live_analyse"):
            st.session_state["live_last_refresh"] = datetime.now().strftime("%H:%M:%S")
            st.rerun()
    with col_s3:
        res_file = REPORTS / f"results_{dstr}.json"
        n_results = 0
        if res_file.exists():
            try:
                with open(res_file, "r", encoding="utf-8") as f:
                    n_results = len(json.load(f).get("races", []))
            except Exception:
                pass
        st.metric("Results Available", f"{n_results}/{len(races)} races")
        if "live_last_refresh" in st.session_state:
            st.caption(f"Last refresh: {st.session_state['live_last_refresh']}")

    st.markdown("---")

    # ── Load analysis ──────────────────────────────────────
    analysis = _live_load_analysis(dstr)
    if not analysis:
        st.info(
            "No results scraped yet for this meeting. "
            "Click **Scrape Results** once races have been run, then **Run Analysis**."
        )
        return

    race_analyses = analysis.get("race_analyses", [])
    cumulative = analysis.get("cumulative", {})

    # ── Cumulative Patterns & Alerts (top of page) ─────────
    patterns = cumulative.get("patterns", [])
    alerts = cumulative.get("alerts", [])

    if patterns or alerts:
        st.markdown("### 📡 Live Pattern Detection")
        n_turf = cumulative.get("n_turf", 0)
        n_awt = cumulative.get("n_awt", 0)
        surf_str = []
        if n_turf:
            surf_str.append(f"{n_turf} Turf")
        if n_awt:
            surf_str.append(f"{n_awt} AWT")
        st.caption(f"After {cumulative.get('n_races', 0)} race(s) ({', '.join(surf_str)})")

        if alerts:
            for a in alerts:
                # Colour by surface tag
                if "[AWT]" in a:
                    icon = "🟠"
                elif "DIVERGING" in a.upper() or "struggling" in a.lower() or "inaccurate" in a.lower():
                    icon = "🔴"
                elif "CONVERGING" in a.upper() or "advantage" in a.lower():
                    icon = "🟢"
                else:
                    icon = "⚡"
                st.markdown(
                    f'<div style="border-left:3px solid rgba(245,158,11,0.7);'
                    f'padding:4px 10px;margin:4px 0;border-radius:0 4px 4px 0;'
                    f'background:rgba(245,158,11,0.06)">'
                    f'{icon} {a}</div>',
                    unsafe_allow_html=True,
                )

        if patterns:
            with st.expander("Detailed Patterns", expanded=False):
                for p in patterns:
                    st.markdown(f"• {p}")

        st.markdown("---")

    # ── Race-by-Race Analysis ──────────────────────────────
    st.markdown("### 🏇 Race-by-Race Analysis")
    for ra in race_analyses:
        if ra.get("status") != "analysed":
            continue
        rn = ra["race_number"]
        w = ra.get("winner") or {}
        dist = ra.get("distance", "?")
        surface = ra.get("surface", "?")

        # Race header
        w_name = w.get("horse_name", "?")
        w_odds = w.get("win_odds", "?")
        w_draw = w.get("draw", "?")
        et_rk = w.get("et_rank")
        sarr_rk = w.get("sarr_rank")

        header_parts = [f"**R{rn}** {dist}m {surface}"]
        header_parts.append(f"— Winner: **{w_name}** (Dr{w_draw}, ${w_odds})")
        if et_rk or sarr_rk:
            ranks = []
            if et_rk:
                ranks.append(f"ET Rk{et_rk}")
            if sarr_rk:
                ranks.append(f"SARR Rk{sarr_rk}")
            header_parts.append(f"[{' | '.join(ranks)}]")

        with st.expander(" ".join(header_parts), expanded=(rn == max(r["race_number"] for r in race_analyses if r.get("status") == "analysed"))):
            # Model convergence badges
            conv_cols = st.columns(2)
            for ci, (mk, label) in enumerate([("et", "ET"), ("sarr", "SARR")]):
                ma = ra.get(mk)
                if not ma:
                    with conv_cols[ci]:
                        st.caption(f"{label}: N/A")
                    continue
                conv = ma["convergence"]
                if conv == "strong":
                    badge_color = "#22c55e"
                    badge_icon = "✓"
                elif conv == "partial":
                    badge_color = "#f59e0b"
                    badge_icon = "~"
                else:
                    badge_color = "#ef4444"
                    badge_icon = "✗"
                rho_str = f"ρ={ma['spearman_rho']:.2f}" if ma.get("spearman_rho") is not None else ""
                mae_str = f"MAE {ma['mae']:.3f}s" if ma.get("mae") is not None else ""
                detail = f"top3 overlap {ma['top3_overlap']}/3"
                if rho_str:
                    detail += f", {rho_str}"
                if mae_str:
                    detail += f", {mae_str}"

                with conv_cols[ci]:
                    st.markdown(
                        f'<div style="border:1px solid {badge_color};border-radius:6px;'
                        f'padding:6px 10px;text-align:center;'
                        f'background:{badge_color}15">'
                        f'<span style="color:{badge_color};font-weight:700;font-size:1.1em">'
                        f'{badge_icon} {label}: {conv.upper()}</span><br>'
                        f'<span style="font-size:0.82em;opacity:0.7">{detail}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

            # Pace read
            pr = ra.get("pace_read", {})
            if pr.get("shape") != "unknown":
                pace_parts = [f"**Pace:** {pr.get('summary', '?')}"]
                pm = pr.get("pace_match")
                if pm == "aligned":
                    pace_parts.append("— ET pace prediction ✓ correct")
                elif pm == "divergent":
                    pace_parts.append("— ET pace prediction ✗ wrong")
                st.markdown(" ".join(pace_parts))

            # Draw
            do = ra.get("draw_obs", {})
            if do.get("bias") and do["bias"] != "unknown":
                st.markdown(f"**Draw:** {do['summary']}")

            # Under/overperformers
            for u in ra.get("underperformers", []):
                et_r = f"ET Rk{u.get('et_rank','?')}" if u.get('et_rank') else ""
                sarr_r = f"SARR Rk{u.get('sarr_rank','?')}" if u.get('sarr_rank') else ""
                model_str = " | ".join(x for x in [et_r, sarr_r] if x)
                st.markdown(
                    f'<div style="border-left:3px solid #ef4444;padding:2px 8px;margin:2px 0;'
                    f'border-radius:0 3px 3px 0;background:rgba(239,68,68,0.06)">'
                    f'⚠ <strong>{u["horse_name"]}</strong> ({model_str}) → P{u["actual_place"]}'
                    + "".join(f'<br><span style="font-size:0.85em;opacity:0.7">→ {r}</span>'
                              for r in u.get("reasons", []))
                    + '</div>',
                    unsafe_allow_html=True,
                )
            for o in ra.get("overperformers", []):
                et_r = f"ET Rk{o.get('et_rank','?')}" if o.get('et_rank') else ""
                sarr_r = f"SARR Rk{o.get('sarr_rank','?')}" if o.get('sarr_rank') else ""
                model_str = " | ".join(x for x in [et_r, sarr_r] if x)
                odds_v = o.get("win_odds")
                odds_str = f" ${odds_v}" if odds_v else ""
                st.markdown(
                    f'<div style="border-left:3px solid #22c55e;padding:2px 8px;margin:2px 0;'
                    f'border-radius:0 3px 3px 0;background:rgba(34,197,94,0.06)">'
                    f'★ <strong>{o["horse_name"]}</strong> ({model_str}) → P{o["actual_place"]}{odds_str}'
                    + "".join(f'<br><span style="font-size:0.85em;opacity:0.7">→ {r}</span>'
                              for r in o.get("reasons", []))
                    + '</div>',
                    unsafe_allow_html=True,
                )

    st.markdown("---")

    # ── Full text report download ──────────────────────────
    report_text = analysis.get("report_text", "")
    if report_text:
        st.download_button(
            "📥 Download Full Report",
            data=report_text,
            file_name=f"live_analysis_{dstr}.txt",
            mime="text/plain",
        )


# ══════════════════════════════════════════════════════════════════════════════
# Form Guide page
# ══════════════════════════════════════════════════════════════════════════════

def page_form_guide():
    st.markdown('<div class="page-title">Form Guide</div>', unsafe_allow_html=True)

    meetings = load_available_meetings()
    if not meetings:
        st.info("No analysed meetings found. Run an analysis first.")
        return

    # Sidebar: meeting selection
    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
    st.sidebar.markdown('<div class="sb-nav-section">Form Guide</div>', unsafe_allow_html=True)
    meeting_titles = [m["title"] for m in meetings]
    chosen_title = st.sidebar.selectbox("Meeting", meeting_titles, index=0,
                                        key="fg_meeting")
    sel = next(m for m in meetings if m["title"] == chosen_title)

    date_compact = sel["date_str"]
    date_iso = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:8]}"

    # Load data — try pre-built cache first, fall back to XLSX
    meeting_data = load_meeting_data(sel["file"])
    racecard = _load_racecard_cache(date_iso)
    fg_cache = _load_form_guide_cache(date_iso)

    bb = _load_blackbook()
    _bb_expire_stale(bb)
    bb_lookup = _bb_active_lookup(bb)
    trial_index = _load_all_trial_horse_index()

    # Build / Rebuild form guide button
    if st.sidebar.button("Build Form Guide cache", key="fg_build_btn",
                         help=f"Runs build_form_guide.py {date_iso}"):
        with st.spinner(f"Building form guide for {date_iso}..."):
            result = subprocess.run(
                [PYTHON, "build_form_guide.py", date_iso],
                capture_output=True, text=True, encoding="utf-8",
                cwd=str(Path(__file__).parent),
            )
        if result.returncode == 0:
            st.toast(f"\u2713 Form guide cache built for {date_iso}", icon="\u2705")
            st.rerun()
        else:
            st.error(f"Build failed:\n```\n{result.stderr[-800:] if result.stderr else result.stdout[-800:]}\n```")

    if fg_cache:
        form_db = pd.DataFrame()  # not needed for HTML when cache available
        race_idx = {}
        st.sidebar.success("\u2713 Using pre-built form guide cache")
    else:
        form_db = _load_form_db()
        if form_db.empty:
            st.error(
                "No form guide cache found and hkjc_results_updated.xlsx is missing.\n\n"
                "Use the **Build Form Guide cache** button in the sidebar."
            )
            return
        race_idx = _build_race_index(form_db)

    races = meeting_data.get("races", [])
    if not races:
        st.warning("No races in this meeting.")
        return

    # ── Static race navigation buttons ────────────────────────────────────
    subtitle_info = "pre-built cache" if fg_cache else f"{len(form_db):,} historical records"
    st.markdown(
        f'<div class="page-subtitle">{meeting_data.get("meeting_title", "")} '
        f'&nbsp;·&nbsp; {subtitle_info}</div>',
        unsafe_allow_html=True,
    )

    active_idx = st.session_state.get("fg_active_race", 0)
    if active_idx >= len(races):
        active_idx = 0

    btn_cols = st.columns(len(races))
    for i, race in enumerate(races):
        with btn_cols[i]:
            is_active = (i == active_idx)
            if st.button(f"R{race['race_number']}", key=f"fg_btn_{race['race_number']}",
                         use_container_width=True,
                         type="primary" if is_active else "secondary"):
                st.session_state["fg_active_race"] = i
                st.rerun()

    race = races[active_idx]

    # ── Race header ───────────────────────────────────────────────────────
    rn = race["race_number"]
    cls_str = f"Class {race['race_class']}" if race.get("race_class") else "Group"
    surface = "AWT" if race.get("is_awt") else "Turf"
    pace_html_fg = ""
    if race.get("pace"):
        pace_html_fg = f" &nbsp;·&nbsp; Pace: {_pace_bar_html(race['pace'], race.get('pace_score', 0.0))}"

    st.markdown(
        f'<div class="race-hdr-block">'
        f'<div class="race-hdr-title">R{rn} &mdash; {race.get("race_name", "")}</div>'
        f'<div class="race-hdr-meta">'
        f'<span>{race["distance"]}m {surface} ({race["race_course"]})</span>'
        f'<span>{cls_str}</span>'
        f'{pace_html_fg}'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    # ── Get horse list — from form guide cache or racecard ───────────────
    if fg_cache:
        cached_race_data = next(
            (r for r in fg_cache.get("races", []) if r["race_number"] == rn), None
        )
        horses = cached_race_data["horses"] if cached_race_data else []
    else:
        horses = []
        if racecard:
            for rc_race in racecard.get("races", []):
                if rc_race["meta"]["race_number"] == rn:
                    horses = [h for h in rc_race["horses"]
                              if not h.get("is_standby")]
                    break
        if not horses:
            horses = [
                {"horse_name": p["horse_name"], "horse_no": p["horse_no"],
                 "jockey": p.get("jockey", ""), "draw": p.get("draw", ""),
                 "rating": "?", "last_6_runs": ""}
                for p in race.get("picks", [])
            ]

    all_form_rows = []  # for download across all races
    any_form = False
    meeting_date = date.fromisoformat(date_iso)

    for horse in horses:
        hname = horse["horse_name"]
        hno = horse.get("horse_no", "?")
        current_draw = horse.get("draw", "?")

        if fg_cache:
            cached_runs = horse.get("runs", [])
            is_debutant = horse.get("is_debutant", not bool(cached_runs))
        else:
            mask = form_db["horse_name_upper"] == hname.strip().upper()
            horse_hist = form_db[mask & (form_db["race_date"] < meeting_date)]
            horse_hist = horse_hist.sort_values("race_date", ascending=False).head(6).sort_values("race_date", ascending=True)
            is_debutant = horse_hist.empty

        bb_entry = bb_lookup.get(hname.strip().upper())
        current_rtg = horse.get("rating", "?")
        current_jockey = horse.get("jockey", "?")
        current_weight = horse.get("weight", "?")
        current_trainer = horse.get("trainer", "?")
        current_overweight = horse.get("overweight", "")
        last6 = horse.get("last_6_runs", "")

        # ── Detect trainer / stable change ────────────────────────────────
        trainer_changed = False
        prev_trainer = ""
        if fg_cache and not is_debutant:
            cached_runs_check = horse.get("runs", [])
            if cached_runs_check:
                prev_trainer = str(cached_runs_check[-1].get("trainer", ""))
                if prev_trainer and prev_trainer != "?" and current_trainer != "?" and prev_trainer.strip().upper() != current_trainer.strip().upper():
                    trainer_changed = True
        elif not fg_cache and not is_debutant:
            mask = form_db["horse_name_upper"] == hname.strip().upper()
            trainer_hist = form_db[mask & (form_db["race_date"] < meeting_date)]
            if not trainer_hist.empty:
                last_row = trainer_hist.sort_values("race_date", ascending=False).iloc[0]
                prev_trainer = str(last_row.get("trainer", "")).strip()
                if prev_trainer and current_trainer != "?" and prev_trainer.upper() != current_trainer.strip().upper():
                    trainer_changed = True
        trainer_display = (
            f'<span style="color:#d43700;font-weight:700" title="Stable change from {prev_trainer}">'
            f'{current_trainer}</span>'
            if trainer_changed
            else current_trainer
        )

        # ── Horse header ─────────────────────────────────────────────────
        l6_badges = _last6_html(last6)
        l6_section = (
            f'<span class="h-l6">'
            f'<span class="h-l6-label">L6</span>{l6_badges}</span>'
        ) if last6 else ""
        bb_icon = " [BB]" if bb_entry else ""
        ow_part = f" ({current_overweight})" if current_overweight else ""

        # Horse header row
        hdr_col, bb_col = st.columns([20, 1]) if not bb_entry else (st.container(), None)
        hdr_target = hdr_col if not bb_entry else hdr_col
        hdr_target.markdown(
            f'<div class="horse-header">'
            f'<span class="h-num">#{hno}</span>'
            f'<span class="h-name">{hname}{bb_icon}</span>'
            f'<span class="h-sep">·</span>'
            f'<span class="h-meta">RTG {current_rtg}</span>'
            f'<span class="h-sep">·</span>'
            f'<span class="h-meta">{current_weight} LB</span>'
            f'<span class="h-sep">·</span>'
            f'<span class="h-meta">{current_jockey}{ow_part}</span>'
            f'<span class="h-sep">·</span>'
            f'<span class="h-meta">{trainer_display}</span>'
            f'<span class="h-sep">·</span>'
            f'<span class="h-meta">GT {current_draw}</span>'
            f'{l6_section}'
            f'</div>',
            unsafe_allow_html=True,
        )
        # Golden Blackbook '+' button
        if not bb_entry and bb_col is not None:
            bb_col.markdown('<div class="fg-bb-col">', unsafe_allow_html=True)
            if bb_col.button("+", key=f"fg_bb_add_{rn}_{hno}",
                             help="Add to Blackbook"):
                _fg_bb_dialog(
                    horse_name=hname,
                    source_race=f"{date_iso} R{rn}",
                    jockey=current_jockey,
                    surface=surface,
                    distance=race.get("distance", ""),
                )
            bb_col.markdown('</div>', unsafe_allow_html=True)

        if bb_entry:
            tags = ", ".join(bb_entry.get("tags", []))
            st.caption(
                f"[BB] {bb_entry.get('reasoning', '')}  "
                f"(Conf: {bb_entry.get('confidence', '?')}"
                f"{', Tags: ' + tags if tags else ''})"
            )

        # ── Compact trial info (if available) ─────────────────────────────
        trial_entries = trial_index.get(hname.strip().upper(), [])
        if trial_entries:
            st.markdown(_trial_compact_html(trial_entries), unsafe_allow_html=True)

        if is_debutant:
            st.caption("*Debutant — no historical form*")
            any_form = True
            continue

        any_form = True

        # ── Build HTML form table ─────────────────────────────────────────
        html_rows = []

        # Normalise runs from either cache or XLSX into a common list of dicts
        display_runs = []
        if fg_cache:
            for run in cached_runs:
                try:
                    rd_date = date.fromisoformat(run["date"])
                    date_disp = rd_date.strftime("%d/%m/%y")
                    date_dc   = rd_date.strftime("%Y%m%d")
                except (ValueError, KeyError):
                    date_disp = str(run.get("date", "?"))[:8]
                    date_dc   = ""
                top5 = [(int(entry[0]), entry[1]) for entry in (run.get("top5") or [])]
                display_runs.append({
                    "date_disp": date_disp,
                    "date_dc":   date_dc,
                    "race_num":  run.get("race_number"),
                    "place_val": str(run.get("place", "?")),
                    "dist": str(run["distance"]) if run.get("distance") else "?",
                    "trk": str(run.get("track", "?")),
                    "crs": str(run.get("course", "?")),
                    "going": str(run.get("going", "?")),
                    "cls_val": str(run.get("class", "?")),
                    "jock": str(run.get("jockey", "?")),
                    "rtg": str(run.get("rating", "?")),
                    "wt": str(run.get("actual_weight", "?")),
                    "gate": str(run.get("draw", "?")),
                    "pos": str(run.get("positions", "-")),
                    "margin": str(run.get("margin", "-")),
                    "ftime": str(run.get("time", "-")),
                    "top5": top5,
                })
        else:
            for _, row in horse_hist.iterrows():
                rd = row["race_date"]
                rnum = row["race_number"]
                ri = race_idx.get((rd, rnum), {"top5": [], "margin_2nd": "-"})
                date_disp = rd.strftime("%d/%m/%y") if hasattr(rd, "strftime") else str(rd)
                date_dc = rd.strftime("%Y%m%d") if hasattr(rd, "strftime") else ""
                trk = str(row.get("race_track", "?"))[:2]
                crs = str(row.get("race_course", "?"))
                dist = int(row["distance"]) if pd.notna(row.get("distance")) else "?"
                going = str(row.get("going", "?"))
                cls_raw = row.get("race_class", "?")
                try:
                    cls_val = str(int(float(cls_raw)))
                except (ValueError, TypeError):
                    cls_val = str(cls_raw)
                jock = str(row.get("jockey", "?"))
                try:
                    rtg = str(int(float(row["rating"]))) if pd.notna(row.get("rating")) else "?"
                except (ValueError, TypeError):
                    rtg = str(row.get("rating", "?"))
                try:
                    wt = str(int(float(row["actual_weight"]))) if pd.notna(row.get("actual_weight")) else "?"
                except (ValueError, TypeError):
                    wt = str(row.get("actual_weight", "?"))
                try:
                    gate = str(int(float(row["draw"]))) if pd.notna(row.get("draw")) else "?"
                except (ValueError, TypeError):
                    gate = "?"
                pos = _fmt_positions(row.get("running_positions"))
                margin = _fmt_margin(row.get("place"), row.get("lbw"), ri)
                ftime = _fmt_time(row.get("finish_time_seconds"))
                try:
                    place_val = str(int(row["place_num"])) if pd.notna(row.get("place_num")) else "?"
                except (ValueError, TypeError):
                    place_val = "?"
                display_runs.append({
                    "date_disp": date_disp,
                    "date_dc":   date_dc,
                    "race_num":  int(rnum) if pd.notna(rnum) else None,
                    "place_val": place_val,
                    "dist": dist,
                    "trk": trk,
                    "crs": crs,
                    "going": going,
                    "cls_val": cls_val,
                    "jock": jock,
                    "rtg": rtg,
                    "wt": wt,
                    "gate": gate,
                    "pos": pos,
                    "margin": margin,
                    "ftime": ftime,
                    "top5": ri.get("top5", []),
                })

        for dr in display_runs:
            date_disp = dr["date_disp"]
            place_val = dr["place_val"]
            dist = dr["dist"]
            trk = dr["trk"]
            crs = dr["crs"]
            going = dr["going"]
            cls_val = dr["cls_val"]
            jock = dr["jock"]
            rtg = dr["rtg"]
            wt = dr.get("wt", "?")
            gate = dr["gate"]
            pos = dr["pos"]
            margin = dr["margin"]
            ftime = dr["ftime"]
            top5 = dr["top5"]

            pl_cell = _place_badge_html(place_val)
            margin_style = "color:#ef4444;font-weight:700;" if place_val == "1" else ""
            margin_cell = f'<span class="form-margin" style="{margin_style}">{_smart_frac_html(margin)}</span>'
            t5_html = _fmt_top5_html(top5, hname) if top5 else "&mdash;"

            # Video link + short commentary (if we have date+race+horse)
            vid_note_row = ""
            dc = dr.get("date_dc") or ""
            rnum = dr.get("race_num")
            if dc and rnum:
                vurl = _hkjc_video_url(dc, rnum)
                vlink = (f'<a class="vid-link" href="{vurl}" target="_blank" '
                         f'rel="noopener noreferrer">▶ Replay</a>')
                note = _run_commentary_lookup(dc, rnum, hname).get("short", "")
                note_html = (f'{vlink}<span>{note}</span>'
                             if note else vlink)
                vid_note_row = (
                    f'<tr class="run-note-row">'
                    f'<td colspan="14">{note_html}</td>'
                    f'</tr>'
                )

            html_rows.append(
                f'<tr class="form-data-row">'
                f'<td>{date_disp}</td>'
                f'<td>{pl_cell}</td>'
                f'<td>{dist}</td><td>{trk}</td><td>{crs}</td>'
                f'<td>{going}</td><td>{cls_val}</td>'
                f'<td class="td-left">{jock}</td>'
                f'<td>{rtg}</td><td>{wt}</td><td>{gate}</td>'
                f'<td class="td-pos">{pos}</td>'
                f'<td>{margin_cell}</td>'
                f'<td>{ftime}</td>'
                f'</tr>'
                f'<tr class="top5-row">'
                f'<td colspan="14">{t5_html}</td>'
                f'</tr>'
                + vid_note_row
            )

            # For Excel download
            t5_plain = " | ".join(f"{p}.{n}" for p, n in top5)
            all_form_rows.append({
                "Race": f"R{rn}",
                "Horse#": hno,
                "Horse": hname,
                "BB": "Y" if bb_entry else "",
                "Date": date_disp,
                "Place": place_val,
                "Dist": dist,
                "Track": trk,
                "Course": crs,
                "Going": going,
                "Class": cls_val,
                "Jockey": jock,
                "Rating": rtg,
                "Weight": wt,
                "Gate": gate,
                "Positions": pos,
                "Margin": margin,
                "Finish Time": ftime,
                "Top 5": t5_plain,
            })

        table_html = (
            '<table class="form-tbl">'
            '<thead><tr>'
            '<th>Date</th><th>Pl</th><th>Dist</th><th>Trk</th><th>Crs</th>'
            '<th>Gng</th><th>Cls</th><th class="th-left">Jockey</th>'
            '<th>Rtg</th><th>Wt</th><th>Gt</th><th>Pos</th><th>Mrgn</th><th>Time</th>'
            '</tr></thead>'
            '<tbody>' + "".join(html_rows) + '</tbody>'
            '</table>'
        )
        st.markdown(table_html, unsafe_allow_html=True)

    if not any_form:
        st.info("No historical form found for horses in this race (all debutants).")

    # Sidebar download — collects current race only but still useful
    if all_form_rows:
        dl_df = pd.DataFrame(all_form_rows)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            dl_df.to_excel(writer, index=False, sheet_name="Form Guide")
        st.sidebar.download_button(
            "[ Download Form Guide ]",
            data=buf.getvalue(),
            file_name=f"form_guide_{date_compact}_R{rn}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    # Full form guide PDF — all races
    if fg_cache:
        pdf_bytes = _build_form_guide_pdf_from_cache(fg_cache, bb_lookup)
    else:
        pdf_bytes = _build_form_guide_pdf(
            meeting_data, racecard, form_db, race_idx, bb_lookup, date_iso,
        )
    st.sidebar.download_button(
        "[ Download Full Form Guide PDF ]",
        data=pdf_bytes,
        file_name=f"form_guide_{date_compact}_all.pdf",
        mime="application/pdf",
        key="fg_pdf_dl",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Barrier Trials page
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=30)
def load_available_trials() -> list[dict]:
    """Scan reports/ for trial JSON files and return sorted list."""
    trials = []
    if not REPORTS.exists():
        return trials
    for f in sorted(REPORTS.glob("trials_*.json"), reverse=True):
        m = re.search(r"trials_(\d{8})\.json$", f.name)
        if not m:
            continue
        date_str = m.group(1)
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            trials.append({
                "file": f,
                "date_str": date_str,
                "date_display": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
                "n_batches": data.get("n_batches", 0),
                "n_horses": data.get("n_horses", 0),
                "scraped_at": data.get("scraped_at", ""),
            })
        except (json.JSONDecodeError, KeyError):
            continue
    return trials


@st.cache_data(ttl=30)
def _load_trial_data(path_str: str) -> dict:
    with open(path_str, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_all_trial_horse_index() -> dict:
    """Build horse_name_upper → list of recent trial entries across all trial files."""
    index: dict[str, list] = {}
    if not REPORTS.exists():
        return index
    for f in sorted(REPORTS.glob("trials_*.json"), reverse=True):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (json.JSONDecodeError, KeyError):
            continue
        trial_date = data.get("date", "")
        for batch in data.get("batches", []):
            # Build top-4 finishers for this batch by final running position
            batch_horses = batch.get("horses", [])
            sorted_by_finish = sorted(
                batch_horses,
                key=lambda h: h["running_positions"][-1] if h.get("running_positions") else 999,
            )
            top4_names = [h["horse_name"] for h in sorted_by_finish[:4]]

            for h in batch_horses:
                key = h["horse_name"].strip().upper()
                index.setdefault(key, []).append({
                    "date": trial_date,
                    "course": batch.get("course", ""),
                    "distance_m": batch.get("distance_m", 0),
                    "going": batch.get("going", ""),
                    "draw": h.get("draw", 0),
                    "gear": h.get("gear", ""),
                    "lbw": h.get("lbw", "-"),
                    "running_positions": h.get("running_positions", []),
                    "time": h.get("time", ""),
                    "result": h.get("result", ""),
                    "comment": h.get("comment", ""),
                    "batch_number": batch.get("batch_number", 0),
                    "n_horses": batch.get("n_horses", 0),
                    "overall_time": batch.get("overall_time", ""),
                    "top4": top4_names,
                    "trainer": h.get("trainer", ""),
                    "jockey": h.get("jockey", ""),
                })
    return index


def _trial_compact_html(entries: list) -> str:
    """Render compact trial info for Form Guide horse cards."""
    if not entries:
        return ""
    # Show latest 2 trials max — each on its own row
    rows_html = []
    for e in entries[:2]:
        pos_str = "-".join(str(p) for p in e.get("running_positions", []))
        finish_pos = e["running_positions"][-1] if e.get("running_positions") else "?"
        n = e.get("n_horses", "?")
        lbw = e.get("lbw", "-")
        result = e.get("result", "")
        comment = e.get("comment", "")

        # Result badge
        if result == "Passed":
            res_html = ' <span style="color:#22c55e;font-weight:600">P</span>'
        elif result == "Failed":
            res_html = ' <span style="color:#ef4444;font-weight:600">F</span>'
        else:
            res_html = ""

        # Finish position colour
        if finish_pos == 1:
            pos_style = "color:#22c55e;font-weight:700"
        elif isinstance(finish_pos, int) and finish_pos <= 3:
            pos_style = "color:#3b82f6;font-weight:600"
        else:
            pos_style = ""

        dist = e.get("distance_m", "?")
        dt = e.get("date", "?")
        if len(dt) == 10:
            dt_disp = dt[5:]  # MM-DD
        else:
            dt_disp = dt

        gear_html = f' <span style="color:#a78bfa;font-size:0.85em">{e["gear"]}</span>' if e.get("gear") else ""

        comment_short = comment[:45] + (".." if len(comment) > 45 else "") if comment else ""
        comment_html = f' <span style="color:#9ca3af;font-size:0.83em">{comment_short}</span>' if comment_short else ""

        # Top-4 finishers
        top4 = e.get("top4", [])
        if top4:
            medals = ["\U0001f947", "\U0001f948", "\U0001f949", "4."]
            t4_parts = []
            for rank, name in enumerate(top4):
                t4_parts.append(f'{medals[rank]}{name}')
            top4_html = (
                ' <span style="color:#d1d5db;font-size:0.80em">'
                + " ".join(t4_parts)
                + '</span>'
            )
        else:
            top4_html = ""

        rows_html.append(
            f'<span style="color:#6b7280;font-size:0.85em">{dt_disp}</span> '
            f'{dist}m '
            f'<span style="{pos_style}">{pos_str}</span>'
            f'({finish_pos}/{n}) '
            f'{lbw} '
            f'{e.get("time", "")}'
            f'{gear_html}'
            f'{res_html}'
            f'{comment_html}'
            f'{top4_html}'
        )

    return (
        '<div style="background:#1a1a2e;border-left:3px solid #6366f1;padding:4px 8px;'
        'margin:2px 0 6px 0;border-radius:4px;font-size:0.88em">'
        '<span style="color:#818cf8;font-weight:600;font-size:0.85em">TRIAL</span><br>'
        + '<br>'.join(rows_html)
        + '</div>'
    )


def sidebar_trials():
    """Trials page sidebar — scrape controls only."""
    st.sidebar.markdown('<div class="sb-nav-section">Scrape Trials</div>', unsafe_allow_html=True)
    trial_date = st.sidebar.date_input("Trial Date", value=date.today(), key="trial_date")

    if st.sidebar.button("[ SCRAPE TRIALS ]", type="primary", use_container_width=True):
        with st.spinner(f"Scraping trials for {trial_date.isoformat()}..."):
            result = subprocess.run(
                [PYTHON, "scrape_hkjc_trials.py", "--date", trial_date.isoformat()],
                capture_output=True, text=True, encoding="utf-8",
                cwd=str(BASE),
            )
        if result.returncode == 0:
            st.toast(f"\u2713 Trials scraped for {trial_date.isoformat()}", icon="\u2705")
            st.cache_data.clear()
            st.rerun()
        else:
            st.sidebar.error(
                f"Scrape failed:\n```\n{result.stderr[-500:] if result.stderr else result.stdout[-500:]}\n```"
            )

    # Bulk scrape helper
    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
    st.sidebar.markdown('<div class="sb-nav-section">Bulk Scrape</div>', unsafe_allow_html=True)
    col_from, col_to = st.sidebar.columns(2)
    with col_from:
        bulk_from = st.date_input("From", value=date.today() - timedelta(days=30), key="trial_bulk_from")
    with col_to:
        bulk_to = st.date_input("To", value=date.today(), key="trial_bulk_to")
    if st.sidebar.button("[ BULK SCRAPE ]", use_container_width=True, key="trial_bulk_btn"):
        with st.spinner(f"Bulk scraping {bulk_from} to {bulk_to}..."):
            result = subprocess.run(
                [PYTHON, "scrape_hkjc_trials.py",
                 "--from", bulk_from.isoformat(),
                 "--to", bulk_to.isoformat(),
                 "--skip-existing", "--sleep", "1.5"],
                capture_output=True, text=True, encoding="utf-8",
                cwd=str(BASE),
            )
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            summary = lines[-1] if lines else "Done"
            st.toast(f"\u2713 {summary}", icon="\u2705")
            st.cache_data.clear()
            st.rerun()
        else:
            st.sidebar.error(
                f"Bulk scrape failed:\n```\n{result.stderr[-500:] if result.stderr else result.stdout[-500:]}\n```"
            )


def _render_trial_batch_table(batch: dict, search_upper: str = ""):
    """Render a single trial batch as an expandable table."""
    horses = batch.get("horses", [])
    if search_upper:
        horses = [h for h in horses if search_upper in h["horse_name"].upper()]
        if not horses:
            return False

    with st.expander(
        f"**Batch {batch['batch_number']}** \u2014 {batch['course']} "
        f"{batch['distance_m']}m \u00b7 Going: {batch['going']} \u00b7 "
        f"Time: {batch['overall_time']} \u00b7 "
        f"Sectionals: {' / '.join(batch.get('sectional_times', []))} \u00b7 "
        f"{batch['n_horses']} horses",
        expanded=True,
    ):
        header = (
            '<table class="form-tbl"><thead><tr>'
            '<th>#</th><th>Horse</th><th>Jockey</th><th>Trainer</th>'
            '<th>Draw</th><th>Gear</th><th>LBW</th><th>Pos</th>'
            '<th>Time</th><th>Result</th><th class="th-left">Comment</th>'
            '</tr></thead><tbody>'
        )
        rows_html = []
        for i, h in enumerate(horses, 1):
            pos_str = " ".join(str(p) for p in h.get("running_positions", []))
            result_val = h.get("result", "")
            if result_val == "Passed":
                res_cell = '<span style="color:#22c55e;font-weight:600">Passed</span>'
            elif result_val == "Failed":
                res_cell = '<span style="color:#ef4444;font-weight:600">Failed</span>'
            else:
                res_cell = ""
            gear = h.get("gear", "")
            gear_cell = f'<span style="color:#a78bfa">{gear}</span>' if gear else ""
            rows_html.append(
                f'<tr class="form-data-row">'
                f'<td>{i}</td>'
                f'<td style="text-align:left;font-weight:600">{h["horse_name"]}</td>'
                f'<td>{h["jockey"]}</td>'
                f'<td>{h["trainer"]}</td>'
                f'<td>{h["draw"]}</td>'
                f'<td>{gear_cell}</td>'
                f'<td>{h["lbw"]}</td>'
                f'<td>{pos_str}</td>'
                f'<td>{h["time"]}</td>'
                f'<td>{res_cell}</td>'
                f'<td class="td-left" style="font-size:0.88em;color:#d1d5db">'
                f'{h.get("comment", "")}</td>'
                f'</tr>'
            )
        st.markdown(
            header + "".join(rows_html) + '</tbody></table>',
            unsafe_allow_html=True,
        )
    return True


def _render_horse_trial_history(horse_name: str, trial_index: dict):
    """Render full trial history for a single horse."""
    key = horse_name.strip().upper()
    entries = trial_index.get(key, [])
    if not entries:
        st.info(f"No trial records found for '{horse_name}'.")
        return

    st.markdown(
        f'<div class="page-subtitle">{horse_name.upper()} \u2014 '
        f'{len(entries)} trial{"s" if len(entries) != 1 else ""}</div>',
        unsafe_allow_html=True,
    )

    header = (
        '<table class="form-tbl"><thead><tr>'
        '<th>Date</th><th>Course</th><th>Dist</th><th>Going</th>'
        '<th>Draw</th><th>Gear</th><th>LBW</th><th>Pos</th>'
        '<th>Fin</th><th>Time</th><th>Result</th>'
        '<th class="th-left">Comment</th><th>Top 4</th>'
        '</tr></thead><tbody>'
    )
    rows_html = []
    for e in entries:
        pos_str = "-".join(str(p) for p in e.get("running_positions", []))
        finish_pos = e["running_positions"][-1] if e.get("running_positions") else "?"
        n = e.get("n_horses", "?")

        # Finish position colour
        if finish_pos == 1:
            fin_cell = f'<span style="color:#22c55e;font-weight:700">1/{n}</span>'
        elif isinstance(finish_pos, int) and finish_pos <= 3:
            fin_cell = f'<span style="color:#3b82f6;font-weight:600">{finish_pos}/{n}</span>'
        else:
            fin_cell = f'{finish_pos}/{n}'

        result_val = e.get("result", "")
        if result_val == "Passed":
            res_cell = '<span style="color:#22c55e;font-weight:600">Passed</span>'
        elif result_val == "Failed":
            res_cell = '<span style="color:#ef4444;font-weight:600">Failed</span>'
        else:
            res_cell = ""

        gear = e.get("gear", "")
        gear_cell = f'<span style="color:#a78bfa">{gear}</span>' if gear else ""

        # Top-4
        top4 = e.get("top4", [])
        medals = ["\U0001f947", "\U0001f948", "\U0001f949", "4."]
        t4_html = " ".join(f'{medals[r]}{nm}' for r, nm in enumerate(top4)) if top4 else ""

        rows_html.append(
            f'<tr class="form-data-row">'
            f'<td>{e.get("date", "?")}</td>'
            f'<td style="text-align:left">{e.get("course", "?")}</td>'
            f'<td>{e.get("distance_m", "?")}m</td>'
            f'<td>{e.get("going", "?")}</td>'
            f'<td>{e.get("draw", "")}</td>'
            f'<td>{gear_cell}</td>'
            f'<td>{e.get("lbw", "-")}</td>'
            f'<td>{pos_str}</td>'
            f'<td>{fin_cell}</td>'
            f'<td>{e.get("time", "")}</td>'
            f'<td>{res_cell}</td>'
            f'<td class="td-left" style="font-size:0.88em;color:#d1d5db">'
            f'{e.get("comment", "")}</td>'
            f'<td style="font-size:0.80em;color:#d1d5db">{t4_html}</td>'
            f'</tr>'
        )

    st.markdown(
        header + "".join(rows_html) + '</tbody></table>',
        unsafe_allow_html=True,
    )


# ── Trial Standouts Spotter ──────────────────────────────────────────────
_CONCEAL_KW = [
    "held up", "under a hold", "not asked", "not extend", "eased",
    "cruised", "in hand", "restrain", "within himself", "not pushed", "no effort",
]
_NEG_KW = [
    "unimpressive", "ordinary", "poor", "disappointing", "limited",
    "failed", "struggled", "green", "slowly away",
]
_POS_PHRASES = ["ran on well", "quicken", "impressive", "easily", "strong",
                "stayed on", "hit the front", "to score", "won going away",
                "not fully tested", "not tested"]


def _render_trial_standouts():
    """Analyse recent trials and flag standout horses."""
    from datetime import datetime as _dt, timedelta
    trial_index = _load_all_trial_horse_index()
    if not trial_index:
        st.info("No trial data available.")
        return

    lookback = st.slider("Lookback window (days)", 7, 90, 30,
                         key="standout_lookback")
    cutoff = _dt.now() - timedelta(days=lookback)

    standouts = []  # list of dicts: horse, flag, detail, trials_summary, trainer
    for horse, entries in trial_index.items():
        # Filter to recent entries only
        recent = []
        for e in entries:
            try:
                tdt = _dt.strptime(e["date"], "%Y-%m-%d")
            except (ValueError, TypeError):
                continue
            if tdt >= cutoff:
                recent.append(e)
        if not recent:
            continue

        # Sort newest first
        recent.sort(key=lambda x: x["date"], reverse=True)
        latest = recent[0]
        comment = (latest.get("comment", "") or "").lower()
        rp = latest.get("running_positions", [])
        fp = rp[-1] if rp else None
        sp = rp[0] if rp else None
        n = latest.get("n_horses", 0)
        gear = latest.get("gear", "")

        is_concealed = (any(kw in comment for kw in _CONCEAL_KW)
                        and not any(nk in comment for nk in _NEG_KW))
        top_half = fp is not None and n > 0 and fp <= (n / 2)
        has_pos = any(p in comment for p in _POS_PHRASES)
        has_eased = "eased" in comment and not any(nk in comment for nk in _NEG_KW)
        is_green = "green" in comment
        has_neg = any(nk in comment for nk in _NEG_KW)
        pos_gained = (sp - fp) if sp is not None and fp is not None else 0
        won_trial = fp == 1 and n and n >= 3

        # Style change detection (back-runner → front-runner)
        # Compare latest recent trial vs most recent prior trial (even outside window)
        style_change = False
        all_entries = sorted(entries, key=lambda x: x.get("date", ""), reverse=True)
        if len(all_entries) >= 2 and rp:
            prev = all_entries[1]  # previous trial (may be outside window)
            prev_rp = prev.get("running_positions", [])
            if prev_rp:
                curr_early = rp[0]
                prev_early = prev_rp[0]
                curr_n = n or 1
                prev_n = prev.get("n_horses", 0) or 1
                # Moved from back half to front third early
                if prev_early > prev_n / 2 and curr_early <= max(curr_n / 3, 2):
                    style_change = True

        flag = ""
        details = []

        # Strong positive
        if is_concealed and top_half:
            flag = "++"
            details.append("Concealed + top half")
        elif has_eased and has_pos:
            flag = "++"
            details.append("Eased + strong finish")
        elif won_trial and is_concealed:
            flag = "++"
            details.append("Won trial under hold")

        if not flag:
            if won_trial and has_pos:
                flag = "+"
                details.append("Trial winner, positive comment")
            elif has_eased:
                flag = "+"
                details.append("Eased (deliberately held)")
            elif has_pos and top_half:
                flag = "+"
                details.append("Positive trial, top half")
            elif pos_gained >= 3:
                flag = "+"
                details.append(f"Gained {pos_gained} positions")
            elif is_concealed:
                flag = "+"
                details.append("Concealed form")
            elif won_trial:
                flag = "+"
                details.append("Trial winner")

        # Style change (e.g. Top Time: back-marker → front-runner)
        if style_change:
            details.append("Style change (back→front)")
            if not flag:
                flag = "+"

        # Multi-trial improvement
        if len(recent) >= 2 and flag in ("", "+"):
            t1, t2 = recent[0], recent[1]
            rp1 = t1.get("running_positions", [])
            rp2 = t2.get("running_positions", [])
            fp1 = rp1[-1] if rp1 else None
            fp2 = rp2[-1] if rp2 else None
            n1 = t1.get("n_horses", 0)
            n2 = t2.get("n_horses", 0)
            if all(v and v > 0 for v in [fp1, fp2, n1, n2]):
                pct1 = fp1 / n1
                pct2 = fp2 / n2
                if pct1 < pct2 - 0.2:
                    details.append("Multi-trial improvement")
                    if not flag:
                        flag = "+"

        # Gear trial (first time in blinkers etc.)
        if gear and "1" in gear:
            details.append(f"First-time gear: {gear}")
            if not flag:
                flag = "+"

        if not flag:
            continue

        # Build position string
        pos_str = "-".join(str(p) for p in rp) if rp else "?"
        finish_str = f"{fp}/{n}" if fp and n else "?"

        standouts.append({
            "horse": horse,
            "flag": flag,
            "detail": "; ".join(details),
            "date": latest["date"],
            "distance_m": latest.get("distance_m", 0),
            "finish": finish_str,
            "positions": pos_str,
            "comment": latest.get("comment", ""),
            "gear": gear or "—",
            "n_trials": len(recent),
            "trainer": latest.get("trainer", ""),
        })

    # Sort: ++ first, then + ; within each tier by date descending (newest first)
    standouts.sort(
        key=lambda x: (0 if x["flag"] == "++" else 1, x["date"]),
    )
    # Reverse date within tiers: re-sort by tier asc, date desc
    standouts.sort(key=lambda x: (0 if x["flag"] == "++" else 1))
    strong = [s for s in standouts if s["flag"] == "++"]
    positive = [s for s in standouts if s["flag"] == "+"]
    strong.sort(key=lambda x: x["date"], reverse=True)
    positive.sort(key=lambda x: x["date"], reverse=True)

    if not standouts:
        st.info(f"No standout horses found in the last {lookback} days.")
        return

    st.markdown(
        f'<div class="page-subtitle">{len(standouts)} standout(s) '
        f'from last {lookback} days</div>',
        unsafe_allow_html=True,
    )

    for label, group, colour in [
        ("★★ STRONG SIGNALS", strong, "#22c55e"),
        ("★ POSITIVE SIGNALS", positive, "#3b82f6"),
    ]:
        if not group:
            continue
        st.markdown(f"**{label}** ({len(group)})")
        rows_data = []
        for s in group:
            rows_data.append({
                "Horse": s["horse"],
                "Flag": s["flag"],
                "Signal": s["detail"],
                "Date": s["date"],
                "Dist": f"{s['distance_m']}m" if s["distance_m"] else "?",
                "Finish": s["finish"],
                "Positions": s["positions"],
                "Trainer": s["trainer"],
                "Gear": s["gear"],
                "#Trials": s["n_trials"],
            })
        sdf = pd.DataFrame(rows_data)

        def _style_flag(val):
            v = str(val).strip()
            if v == "++":
                return "color: #22c55e; font-weight: bold"
            elif v == "+":
                return "color: #3b82f6; font-weight: bold"
            return ""

        styled = sdf.style.map(_style_flag, subset=["Flag"]) \
                          .set_properties(**{"text-align": "center"}) \
                          .set_properties(subset=["Horse"], **{
                              "text-align": "left", "font-weight": "600"}) \
                          .set_properties(subset=["Signal"], **{"text-align": "left"})
        st.dataframe(styled, use_container_width=True, hide_index=True)

        # Expandable details
        with st.expander(f"Show comments ({len(group)} horses)"):
            for s in group:
                cmt = s["comment"] or "(no comment)"
                st.caption(f"**{s['horse']}** ({s['date']}) — {cmt}")


def page_trials():
    """Barrier Trials results page."""
    st.markdown('<div class="page-title">Barrier Trials</div>', unsafe_allow_html=True)

    sidebar_trials()

    trials = load_available_trials()
    if not trials:
        st.info("No trial data available. Use the sidebar to scrape trial results.")
        return

    # ── Tabs: Browse by Date | Horse Lookup | Standouts ─────────────────
    tab_browse, tab_lookup, tab_standouts = st.tabs(
        ["Browse by Date", "Horse Lookup", "★ Standouts"])

    # ── TAB 1: Browse by Date ─────────────────────────────────────────────
    with tab_browse:
        options = {t["date_display"]: t for t in trials}
        sel_label = st.selectbox(
            "Trial date", list(options.keys()), index=0, key="trial_date_select",
        )
        sel = options[sel_label]

        data = _load_trial_data(str(sel["file"]))
        batches = data.get("batches", [])

        st.markdown(
            f'<div class="page-subtitle">{sel["date_display"]} '
            f'&nbsp;\u00b7&nbsp; {data.get("n_batches", 0)} batches '
            f'&nbsp;\u00b7&nbsp; {data.get("n_horses", 0)} horses</div>',
            unsafe_allow_html=True,
        )

        search = st.text_input("Filter horse name", key="trial_search",
                               placeholder="Type to filter within this date...")
        search_upper = search.strip().upper() if search else ""

        for batch in batches:
            _render_trial_batch_table(batch, search_upper)

        with st.expander("Gear Legend"):
            st.markdown(
                "**B** Blinkers \u00b7 **BO** Blinker one cowl \u00b7 **CC** Cornell Collar \u00b7 "
                "**CP** Cheek Pieces \u00b7 **CO** Combination \u00b7 **E** Eye Shield \u00b7 "
                "**H** Hood \u00b7 **P** Pacifier \u00b7 **PC** Pacifier one cowl \u00b7 "
                "**SB** Side Blinds \u00b7 **SR** Side Reins \u00b7 **TT** Tongue Tie \u00b7 "
                "**V** Visor \u00b7 **VO** Visor one cowl \u00b7 **XB** Cross-over Noseband\n\n"
                "**1** = first time wearing \u00b7 **2** = replaced \u00b7 **-** = removed"
            )

    # ── TAB 2: Horse Lookup ───────────────────────────────────────────────
    with tab_lookup:
        trial_index = _load_all_trial_horse_index()
        all_names = sorted(trial_index.keys())

        lookup_input = st.text_input(
            "Horse name", key="trial_horse_lookup",
            placeholder="Enter horse name to see all trial history...",
        )
        lookup_upper = lookup_input.strip().upper() if lookup_input else ""

        if lookup_upper:
            # Exact match first, then partial matches
            if lookup_upper in trial_index:
                _render_horse_trial_history(lookup_upper, trial_index)
            else:
                matches = [n for n in all_names if lookup_upper in n]
                if not matches:
                    st.info(f"No trial records found matching '{lookup_input}'.")
                elif len(matches) == 1:
                    _render_horse_trial_history(matches[0], trial_index)
                else:
                    st.info(f"{len(matches)} matches found — select one:")
                    chosen = st.selectbox(
                        "Select horse", matches, key="trial_lookup_select",
                    )
                    _render_horse_trial_history(chosen, trial_index)
        else:
            st.caption(
                f"{len(all_names)} unique horses in trial database. "
                "Type a name above to look up full trial history."
            )

    # ── TAB 3: Standouts Spotter ──────────────────────────────────────────
    with tab_standouts:
        st.markdown("### ★ Trial Standouts Spotter")
        st.caption(
            "Horses flagged from recent trials as potential performers. "
            "Based on concealment patterns, position gains, eased form, "
            "multi-trial improvement, and gear changes."
        )
        _render_trial_standouts()


# ══════════════════════════════════════════════════════════════════════════════
# PDF Builder page
# ══════════════════════════════════════════════════════════════════════════════

def _auto_notes_for_pick(pick: dict, race: dict, bb_lookup: dict,
                         trial_index: dict) -> list[str]:
    """Generate evidence-backed reasoning notes for a single pick."""
    notes = []
    hn = pick.get("horse_name", "")
    hn_upper = hn.strip().upper()

    # Flags
    flags = pick.get("flags", [])
    if flags:
        flag_map = {
            "INC": "Inconsistent recent form",
            "\u2191IMP": "Improving form trajectory",
            "\u2193DEC": "Declining form",
            "DEB": "Debutant \u2014 no race history",
            "LDIST": "Lightly raced at this distance",
            "SPELL": "Returning from a spell",
            "CU": "Class upgrade from last start",
            "CD": "Class drop from last start",
        }
        for f in flags:
            desc = flag_map.get(f)
            if desc:
                notes.append(desc)

    # ESZ / pace style
    esz = pick.get("early_speed_z")
    style = pick.get("style", "")
    if esz is not None:
        if esz <= -1.0:
            notes.append(f"Strong early speed (ESZ {esz:+.1f}) \u2014 likely prominent")
        elif esz >= 1.5:
            notes.append(f"Deep closer (ESZ {esz:+.1f}) \u2014 needs genuine pace")
    if style:
        pace_label = race.get("pace", "Normal")
        if style == "Leader" and pace_label in ("Fast", "Very Fast"):
            notes.append("Leader in fast-predicted pace \u2014 risk of over-racing")
        elif style == "Closer" and pace_label in ("Slow", "Very Slow"):
            notes.append("Closer in slow-predicted pace \u2014 may lack momentum")

    # SSI
    ssi = pick.get("avg_ssi")
    if ssi is not None:
        if ssi >= 0.3:
            notes.append(f"Strong sectional index (SSI {ssi:+.2f})")
        elif ssi <= -0.3:
            notes.append(f"Weak sectional profile (SSI {ssi:+.2f})")

    # Projected final section
    pfs = pick.get("proj_final_sec")
    if pfs and pfs < 22.5:
        notes.append(f"Projected strong finish ({pfs:.2f}s final 400m)")

    # Win probability
    wp = pick.get("win_prob", 0)
    if wp >= 0.20:
        notes.append(f"High model confidence (Win% {wp*100:.0f}%)")

    # Vet flag
    vf = pick.get("vet_flag", "")
    if vf == "RED":
        notes.append("VET: Recent vet concern (red flag)")
    elif vf == "AMBER":
        notes.append("VET: Minor vet concern (amber)")

    # Draw
    draw = pick.get("draw")
    n_runners = race.get("runners", 14)
    course = race.get("race_course", "")
    if draw and n_runners:
        if draw <= 3 and "C" in course.upper():
            notes.append(f"Inside draw ({draw}) \u2014 potential rail advantage")
        elif draw >= n_runners - 1 and n_runners >= 12:
            notes.append(f"Wide draw ({draw}/{n_runners}) \u2014 must overcome barrier")

    # Blackbook
    bb_entry = bb_lookup.get(hn_upper)
    if bb_entry:
        conf = bb_entry.get("confidence", "")
        reason = bb_entry.get("reasoning", "")
        note = f"BLACKBOOK ({conf})"
        if reason:
            note += f": {reason[:80]}"
        notes.append(note)

    # Trial
    trial_flag = pick.get("trial_flag", "")
    trial_entries = trial_index.get(hn_upper, [])
    if trial_entries:
        from datetime import datetime as _dto, timedelta as _td
        cutoff = _dto.now() - _td(days=60)
        recent = []
        for e in trial_entries:
            try:
                tdt = _dto.strptime(e["date"], "%Y-%m-%d")
            except (ValueError, TypeError):
                continue
            if tdt >= cutoff:
                recent.append(e)
        if recent:
            recent.sort(key=lambda x: x["date"], reverse=True)
            latest = recent[0]
            rp = latest.get("running_positions", [])
            comment = (latest.get("comment", "") or "")
            fp = rp[-1] if rp else None
            n = latest.get("n_horses", 0)
            parts = [f"Trial {latest['date']}"]
            if fp and n:
                parts.append(f"finished {fp}/{n}")
            if comment:
                parts.append(comment[:60])
            notes.append(" \u2014 ".join(parts))
    elif trial_flag:
        notes.append(f"Trial form: {trial_flag}")

    return notes


def _build_pdfbuilder_pdf(meeting_data: dict, selected_races: list[int],
                          race_selections: dict, race_notes: dict,
                          bb_lookup: dict, trial_index: dict,
                          include_bb: bool, include_trials: bool,
                          include_speed_map: bool) -> bytes:
    """Generate a custom race-day PDF with user selections and auto-notes.

    race_selections: {race_num: {"banker": horse_name|None, "others": [horse_name, ...]}}
    race_notes: {race_num: {"auto": [str], "manual": str}}
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak,
        HRFlowable,
    )

    RED = colors.HexColor("#e63946")
    DARK = colors.HexColor("#0e1117")
    GREY = colors.HexColor("#888888")
    GREEN = colors.HexColor("#22c55e")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=10*mm, rightMargin=10*mm,
                            topMargin=12*mm, bottomMargin=12*mm)
    styles = getSampleStyleSheet()

    sTitle = ParagraphStyle("PBTitle", parent=styles["Heading1"],
                            fontSize=16, leading=20, spaceAfter=2,
                            textColor=DARK)
    sSub = ParagraphStyle("PBSub", parent=styles["Normal"],
                          fontSize=8, leading=10, textColor=GREY,
                          spaceAfter=6)
    sRace = ParagraphStyle("PBRace", parent=styles["Heading2"],
                           fontSize=12, leading=14, spaceAfter=4,
                           textColor=RED)
    sPace = ParagraphStyle("PBPace", parent=styles["Normal"],
                           fontSize=8, leading=10, textColor=GREY,
                           spaceAfter=4)
    sHorse = ParagraphStyle("PBHorse", parent=styles["Normal"],
                            fontSize=10, leading=13, spaceAfter=1)
    sNote = ParagraphStyle("PBNote", parent=styles["Normal"],
                           fontSize=8, leading=10, textColor=DARK,
                           leftIndent=12, spaceAfter=1)
    sManual = ParagraphStyle("PBManual", parent=styles["Normal"],
                             fontSize=8, leading=10,
                             textColor=colors.HexColor("#333333"),
                             leftIndent=12, spaceBefore=2, spaceAfter=4,
                             fontName="Helvetica-Oblique")
    sBB = ParagraphStyle("PBBB", parent=styles["Normal"],
                         fontSize=8, leading=10,
                         textColor=colors.HexColor("#d97706"),
                         leftIndent=8, spaceAfter=2)
    sTrial = ParagraphStyle("PBTrial", parent=styles["Normal"],
                            fontSize=8, leading=10,
                            textColor=colors.HexColor("#6366f1"),
                            leftIndent=8, spaceAfter=2)
    sSMap = ParagraphStyle("PBSMap", parent=styles["Normal"],
                           fontSize=7, leading=9, textColor=GREY,
                           leftIndent=8, spaceAfter=2)

    elements = []

    # Title
    title = meeting_data.get("meeting_title", "Race Day Analysis")
    elements.append(Paragraph(title, sTitle))
    elements.append(Paragraph(
        f"Generated {datetime.now().strftime('%d %b %Y %H:%M')} "
        f"\u00b7 {len(selected_races)} race(s) selected",
        sSub))
    elements.append(HRFlowable(width="100%", thickness=1,
                                color=RED, spaceAfter=8))

    races = meeting_data.get("races", [])
    race_map = {r["race_number"]: r for r in races}

    for rn in selected_races:
        race = race_map.get(rn)
        if not race:
            continue

        cls_str = f"Class {race['race_class']}" if race.get("race_class") else "Group"
        surface = "AWT" if race.get("is_awt") else "Turf"
        elements.append(Paragraph(
            f"R{rn} \u2014 {race.get('race_name', '')} \u00b7 "
            f"{race['distance']}m {surface} ({race.get('race_course', '')}) "
            f"\u00b7 {cls_str}",
            sRace))

        # Pace
        pace = race.get("pace", "Normal")
        pace_score = race.get("pace_score", 0)
        pace_leaders = race.get("pace_leaders", [])
        pace_reasons = race.get("pace_reasons", [])
        pace_parts = [f"Pace: {pace} ({pace_score:+.2f}s)"]
        if pace_leaders:
            pace_parts.append(f"Front-runners: {', '.join(pace_leaders[:4])}")
        if pace_reasons:
            pace_parts.append(" | ".join(pace_reasons[:2]))
        elements.append(Paragraph(" \u00b7 ".join(pace_parts), sPace))

        sel = race_selections.get(rn, {})
        banker = sel.get("banker")
        others = sel.get("others", [])
        picks = race.get("picks", [])
        pick_map = {p["horse_name"]: p for p in picks}

        # Banker
        if banker and banker in pick_map:
            p = pick_map[banker]
            wp = p.get("win_prob", 0)
            elements.append(Paragraph(
                f'<b><font color="#{RED.hexval()[2:]}">\u2605 BANKER: '
                f'#{p.get("horse_no", "?")} {banker}</font></b>'
                f' \u00b7 {p.get("jockey", "")} \u00b7 '
                f'Gt {p.get("draw", "?")} \u00b7 '
                f'Win% {wp*100:.0f} \u00b7 '
                f'{p.get("style", "")} (ESZ {p.get("early_speed_z", 0):+.1f})',
                sHorse))
            # Auto-notes for banker
            rnotes = race_notes.get(rn, {})
            auto = rnotes.get(f"auto_{banker}", [])
            for n in auto:
                elements.append(Paragraph(f"\u2022 {n}", sNote))

        # Other selections
        for hname in others:
            if hname == banker:
                continue
            p = pick_map.get(hname, {})
            wp = p.get("win_prob", 0)
            elements.append(Paragraph(
                f'<b>#{p.get("horse_no", "?")} {hname}</b>'
                f' \u00b7 {p.get("jockey", "")} \u00b7 '
                f'Gt {p.get("draw", "?")} \u00b7 '
                f'Win% {wp*100:.0f} \u00b7 '
                f'{p.get("style", "")} (ESZ {p.get("early_speed_z", 0):+.1f})',
                sHorse))
            rnotes = race_notes.get(rn, {})
            auto = rnotes.get(f"auto_{hname}", [])
            for n in auto:
                elements.append(Paragraph(f"\u2022 {n}", sNote))

        # Manual notes
        rnotes = race_notes.get(rn, {})
        manual = rnotes.get("manual", "")
        if manual.strip():
            elements.append(Paragraph(f"Notes: {manual}", sManual))

        # Blackbook callouts for this race
        if include_bb:
            for p in picks:
                hn_up = p["horse_name"].strip().upper()
                bb = bb_lookup.get(hn_up)
                if bb and p["horse_name"] not in ([banker] if banker else []) + others:
                    conf = bb.get("confidence", "")
                    reason = bb.get("reasoning", "")[:80]
                    elements.append(Paragraph(
                        f'\u26a0 BB: {p["horse_name"]} ({conf}) '
                        f'{"\u2014 " + reason if reason else ""}',
                        sBB))

        # Trial standouts for this race
        if include_trials:
            for p in picks:
                hn_up = p["horse_name"].strip().upper()
                entries = trial_index.get(hn_up, [])
                if not entries:
                    continue
                if p["horse_name"] in ([banker] if banker else []) + others:
                    continue  # already covered in auto-notes
                from datetime import datetime as _dto, timedelta as _td
                cutoff = _dto.now() - _td(days=60)
                recent = [e for e in entries
                          if e.get("date", "") >= cutoff.strftime("%Y-%m-%d")]
                if recent:
                    recent.sort(key=lambda x: x["date"], reverse=True)
                    lat = recent[0]
                    rp = lat.get("running_positions", [])
                    fp = rp[-1] if rp else "?"
                    n = lat.get("n_horses", "?")
                    comm = (lat.get("comment", "") or "")[:60]
                    elements.append(Paragraph(
                        f'\U0001f3c7 Trial: {p["horse_name"]} \u2014 '
                        f'{lat["date"]} {fp}/{n} {comm}',
                        sTrial))

        # Speed map summary
        if include_speed_map:
            smap = race.get("speed_map", {})
            beneficiaries = smap.get("beneficiaries", [])
            if beneficiaries:
                ben_str = ", ".join(
                    f'{b["horse_name"]} ({b["reason"]})'
                    for b in beneficiaries[:4])
                elements.append(Paragraph(
                    f"Speed map advantage: {ben_str}", sSMap))

        elements.append(Spacer(1, 6*mm))

    doc.build(elements)
    return buf.getvalue()


def page_pdf_builder():
    """Custom PDF Builder — select races, pick bankers, generate notes, export."""
    st.markdown("## \U0001f4c4 PDF Builder")

    meetings = load_available_meetings()
    if not meetings:
        st.info("No analysed meetings available. Run an analysis first.")
        return

    options = {m["title"]: m for m in meetings}
    selected = st.selectbox("Select meeting:", list(options.keys()),
                            index=0, key="pb_meeting")
    meeting = options[selected]
    data = load_meeting_data(meeting["file"])
    races = data.get("races", [])

    if not races:
        st.warning("No races in this meeting.")
        return

    bb = _load_blackbook()
    bb_lookup = _bb_active_lookup(bb)
    trial_index = _load_all_trial_horse_index()

    # ── Race selection ─────────────────────────────────────────────────────
    st.markdown("### Race Selection")
    race_nums = [r["race_number"] for r in races]
    race_labels = {
        r["race_number"]: (
            f'R{r["race_number"]} — {r["distance"]}m '
            f'{"AWT" if r.get("is_awt") else "Turf"} '
            f'C{r.get("race_class", "?")} '
            f'{r.get("race_name", "")[:30]}'
        )
        for r in races
    }
    selected_races = st.multiselect(
        "Include races:",
        race_nums,
        default=race_nums,
        format_func=lambda x: race_labels.get(x, f"R{x}"),
        key="pb_races",
    )

    if not selected_races:
        st.info("Select at least one race.")
        return

    # ── Toggles ────────────────────────────────────────────────────────────
    col_t1, col_t2, col_t3 = st.columns(3)
    with col_t1:
        include_bb = st.checkbox("Include blackbook callouts", value=True,
                                 key="pb_inc_bb")
    with col_t2:
        include_trials = st.checkbox("Include trial standouts", value=True,
                                     key="pb_inc_trials")
    with col_t3:
        include_speed_map = st.checkbox("Include speed map notes", value=True,
                                        key="pb_inc_smap")

    st.markdown("---")

    # ── Per-race selections ────────────────────────────────────────────────
    race_selections = {}
    race_notes = {}
    race_map = {r["race_number"]: r for r in races}

    for rn in selected_races:
        race = race_map.get(rn)
        if not race:
            continue
        picks = race.get("picks", [])
        horse_names = [p["horse_name"] for p in picks]

        cls_str = f"C{race.get('race_class', '?')}" if race.get("race_class") else "Grp"
        surface = "AWT" if race.get("is_awt") else "Turf"
        pace = race.get("pace", "Normal")
        pace_score = race.get("pace_score", 0)

        st.markdown(
            f'<div style="border-left:3px solid #e63946; padding-left:10px; '
            f'margin-bottom:4px;">'
            f'<strong>R{rn}</strong> — {race["distance"]}m {surface} {cls_str} '
            f'| Pace: {pace} ({pace_score:+.2f}s)'
            f'</div>',
            unsafe_allow_html=True,
        )

        # Quick model ranking view
        if picks:
            rank_parts = []
            for i, p in enumerate(picks[:6]):
                wp = p.get("win_prob", 0)
                esz = p.get("early_speed_z", 0)
                ssi = p.get("avg_ssi")
                ssi_str = f" SSI{ssi:+.1f}" if ssi is not None else ""
                rank_parts.append(
                    f'**{i+1}.** #{p.get("horse_no", "?")} {p["horse_name"]} '
                    f'({wp*100:.0f}% ESZ{esz:+.1f}{ssi_str})'
                )
            st.caption(" | ".join(rank_parts))

        col1, col2 = st.columns(2)
        with col1:
            banker = st.selectbox(
                f"R{rn} Banker (top selection):",
                ["(None)"] + horse_names,
                index=1 if horse_names else 0,
                key=f"pb_banker_{rn}",
            )
            if banker == "(None)":
                banker = None
        with col2:
            default_others = horse_names[1:4] if len(horse_names) > 1 else []
            others = st.multiselect(
                f"R{rn} Other selections:",
                horse_names,
                default=default_others,
                key=f"pb_others_{rn}",
            )

        race_selections[rn] = {"banker": banker, "others": others}

        # Auto-generate notes for each selected horse
        all_selected = ([banker] if banker else []) + [o for o in others if o != banker]
        pick_map = {p["horse_name"]: p for p in picks}
        auto_notes_for_race = {}
        for hname in all_selected:
            p = pick_map.get(hname, {})
            auto = _auto_notes_for_pick(p, race, bb_lookup, trial_index)
            auto_notes_for_race[f"auto_{hname}"] = auto

        # Show auto-notes in expander
        if all_selected:
            with st.expander(f"R{rn} Auto-generated notes", expanded=False):
                for hname in all_selected:
                    auto = auto_notes_for_race.get(f"auto_{hname}", [])
                    if auto:
                        prefix = "\u2605 " if hname == banker else ""
                        st.markdown(f"**{prefix}{hname}:**")
                        for n in auto:
                            st.markdown(f"- {n}")
                    else:
                        st.caption(f"{hname}: No notable flags")

        manual = st.text_area(
            f"R{rn} Manual notes:",
            value="", height=68,
            key=f"pb_manual_{rn}",
            placeholder="Add personal observations, betting angles...",
        )

        auto_notes_for_race["manual"] = manual
        race_notes[rn] = auto_notes_for_race
        st.markdown("")

    # ── Export ─────────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### Export PDF")

    if st.button("Generate PDF", type="primary", key="pb_generate",
                 use_container_width=True):
        with st.spinner("Building PDF..."):
            pdf_bytes = _build_pdfbuilder_pdf(
                data, selected_races, race_selections, race_notes,
                bb_lookup, trial_index,
                include_bb, include_trials, include_speed_map,
            )
        st.success("PDF generated!")
        date_str = meeting.get("date_str", "meeting")
        st.download_button(
            label="Download PDF",
            data=pdf_bytes,
            file_name=f"race_analysis_{date_str}.pdf",
            mime="application/pdf",
            key="pb_download",
            use_container_width=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Entry point — page router
# ══════════════════════════════════════════════════════════════════════════════

def main():
    # ── Sidebar brand header ───────────────────────────────────────────────
    st.sidebar.markdown(
        '<div class="sb-brand">'
        '<div class="sb-brand-icon">HK</div>'
        '<div><div class="sb-brand-text">HKJC DASHBOARD</div>'
        '<div class="sb-brand-sub">Race Analysis Terminal</div></div>'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── Navigation ────────────────────────────────────────────────────────
    NAV_ITEMS = [
        ("Overview",     "🏁 Overview"),
        ("Race Day",     "📊 Race Day"),
        ("Live Feed",    "📡 Live Feed"),
        ("Form Guide",   "📖 Form Guide"),
        ("Trials",       "🎽 Trials"),
        ("Backtest",     "🧪 Backtest"),
        ("Results",      "🏆 Results"),
        ("Blackbook",    "📓 Blackbook"),
        ("PDF Builder",  "📄 PDF Builder"),
    ]
    if "nav_page" not in st.session_state:
        st.session_state["nav_page"] = "Overview"

    for page_name, label in NAV_ITEMS:
        is_active = st.session_state["nav_page"] == page_name
        wrap_cls = "sb-active" if is_active else ""
        st.sidebar.markdown(f'<div class="{wrap_cls}">', unsafe_allow_html=True)
        if st.sidebar.button(label, key=f"nav_{page_name}", use_container_width=True):
            st.session_state["nav_page"] = page_name
            st.rerun()
        st.sidebar.markdown('</div>', unsafe_allow_html=True)

    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)

    page = st.session_state["nav_page"]

    if page == "Overview":
        page_overview()
    elif page == "Race Day":
        selected = sidebar_race_day()
        page_race_day(selected)
    elif page == "Live Feed":
        page_live_feed()
    elif page == "Form Guide":
        page_form_guide()
    elif page == "Trials":
        page_trials()
    elif page == "Backtest":
        page_backtest()
    elif page == "Results":
        page_results()
    elif page == "Blackbook":
        page_blackbook()
    elif page == "PDF Builder":
        page_pdf_builder()


def sidebar_race_day():
    """Race Day sidebar controls."""
    st.sidebar.markdown('<div class="sb-nav-section">Run Analysis</div>', unsafe_allow_html=True)
    col1, col2 = st.sidebar.columns(2)
    with col1:
        run_date = st.date_input("Race Date", value=date.today(), key="run_date")
    with col2:
        no_cache = st.checkbox("Re-scrape", value=False, key="no_cache")

    going_turf = st.sidebar.text_input("Turf Going", value="Good", key="going_turf")
    going_awt = st.sidebar.text_input("AWT Going", value="Good", key="going_awt")

    # ── Upload fallback (for when cloud scraper can't reach HKJC) ──
    uploaded = st.sidebar.file_uploader(
        "Or upload racecard JSON (from local scrape)",
        type=["json"], key="racecard_upload",
        help="Upload cache/racecard_YYYY-MM-DD.json produced by a local scrape",
    )
    if uploaded is not None:
        date_iso = run_date.isoformat()
        # Only save once per unique upload (avoid re-trigger on every rerun)
        upload_id = f"{uploaded.name}_{uploaded.size}_{date_iso}"
        if st.session_state.get("_last_upload_id") != upload_id:
            if _save_uploaded_racecard(uploaded.getvalue(), date_iso):
                st.session_state["_uploaded_rc_date"] = date_iso
                st.session_state["_last_upload_id"] = upload_id

    # Detect whether uploaded racecard is available for the selected date
    date_iso = run_date.isoformat()
    date_compact = date_iso.replace("-", "")
    _has_racecard = (BASE / "racecards" / f"racecard_{date_compact}.xlsx").exists()

    if st.sidebar.button("[ RUN ANALYSIS ]", type="primary", use_container_width=True):
        # Skip scrape if we already have a racecard file (uploaded or cached)
        skip = _has_racecard and not no_cache
        run_pipeline(date_iso, no_cache, going_turf, going_awt, skip_scrape=skip)
        st.cache_data.clear()
        st.rerun()

    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
    st.sidebar.markdown('<div class="sb-nav-section">Meetings</div>', unsafe_allow_html=True)
    meetings = load_available_meetings()

    if not meetings:
        st.sidebar.info("No analysed meetings found.\nRun your first analysis above!")
        return None

    options = {m["title"]: m for m in meetings}
    selected = st.sidebar.selectbox(
        "Select meeting:",
        list(options.keys()),
        index=0,
        key="meeting_select",
    )

    m = options[selected]
    st.sidebar.caption(
        f"{m['date_str'][:4]}-{m['date_str'][4:6]}-{m['date_str'][6:]}  "
        f"| {m['venue']}  | {m['n_races']} races"
    )

    if m.get("generated_at"):
        try:
            gen = datetime.fromisoformat(m["generated_at"])
            st.sidebar.caption(f"Generated: {gen.strftime('%d %b %Y %H:%M')}")
        except ValueError:
            pass

    return m


if __name__ == "__main__":
    main()
