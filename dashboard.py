#!/usr/bin/env python3
"""
HKJC Race Day Dashboard — dashboard.py
========================================
Streamlit dashboard displaying model picks for upcoming HKJC meetings.
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
from functools import lru_cache
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests as _requests
import streamlit as st

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
_LOCAL_PYTHON = Path(r"C:\Users\tbhbr\miniconda3\python.exe")
PYTHON = str(_LOCAL_PYTHON) if _LOCAL_PYTHON.exists() else sys.executable
BLACKBOOK_FILE = BASE / "blackbook.json"


# ── HKT timezone helpers (Asia/Hong_Kong = UTC+8, no DST) ───────────────────
HKT = timezone(timedelta(hours=8))

def hkt_now() -> datetime:
    """Current wall-clock in Hong Kong (timezone-aware)."""
    return datetime.now(tz=HKT)

def hkt_from_ts(ts: float) -> datetime:
    """Convert a POSIX timestamp (UTC seconds) to HKT-aware datetime."""
    return datetime.fromtimestamp(ts, tz=HKT)

def hkt_fmt(dt_or_ts, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format any timestamp/datetime as HKT. ``dt_or_ts`` may be a float
    POSIX timestamp, a naive datetime (assumed local), or aware datetime."""
    if isinstance(dt_or_ts, (int, float)):
        dt = hkt_from_ts(float(dt_or_ts))
    elif isinstance(dt_or_ts, datetime):
        if dt_or_ts.tzinfo is None:
            # naive — assume it was produced on the same host; convert via UTC
            dt = dt_or_ts.astimezone(HKT) if False else dt_or_ts.replace(tzinfo=HKT)
        else:
            dt = dt_or_ts.astimezone(HKT)
    else:
        return str(dt_or_ts)
    return dt.strftime(fmt) + " HKT"


# ── Per-run commentary lookup (for Form Guide rows) ──────────────────────────
def _hkjc_video_url(date_dc: str, race_no: int,
                    track: str | None = None) -> str:
    """Return a URL to the HKJC race replay page.

    ``date_dc`` is ``YYYY/MM/DD``.  When ``track`` (``ST``/``HV``) is supplied
    we point at the public English LocalResults page which embeds the
    replay player and works when opened standalone in a new tab.  Otherwise
    we fall back to the older iframe player URL.
    """
    rn = int(race_no)
    if track:
        tk = str(track).strip().upper()
        rc = "ST" if tk in ("ST", "SHA TIN") else (
            "HV" if tk in ("HV", "HAPPY VALLEY") else tk or "ST")
        return (
            "https://racing.hkjc.com/racing/information/English/Racing/"
            f"LocalResults.aspx?RaceDate={date_dc}&Racecourse={rc}"
            f"&RaceNo={rn:02d}"
        )
    return (
        "https://racing.hkjc.com/contentAsset/videoplayer_v4/"
        "video-player-iframe_v4.html?type=replay-full"
        f"&date={date_dc}&no={rn:02d}&lang=eng"
        "&noPTbar=false&noLeading=false&videoParam=PAD"
    )


_TRIAL_COURSE_TO_RC = {
    "CONGHUA": "ch",
    "CONGHUA TURF": "ch",
    "CONGHUA AWT": "ch",
    "SHA TIN": "st",
    "SHA TIN TURF": "st",
    "SHA TIN AWT": "st",
    "HAPPY VALLEY": "hv",
}


def _trial_rc_code(course: str | None) -> str:
    """Map a trial batch course string (e.g. 'CONGHUA TURF') to the HKJC rc code."""
    if not course:
        return "ch"
    k = str(course).strip().upper()
    if k in _TRIAL_COURSE_TO_RC:
        return _TRIAL_COURSE_TO_RC[k]
    if "CONGHUA" in k:
        return "ch"
    if "HAPPY" in k or k == "HV":
        return "hv"
    if "SHA TIN" in k or k == "ST":
        return "st"
    return "ch"


def _hkjc_trial_video_url(date_dc: str, batch_no: int, course: str | None = None) -> str:
    """Build the HKJC barrier-trial replay iframe URL for (date, batch)."""
    rc = _trial_rc_code(course)
    return (
        "https://racing.hkjc.com/contentAsset/videoplayer_v4/"
        "video-player-iframe_v4.html?type=brts"
        f"&date={date_dc}&rc={rc}&no={int(batch_no):02d}&lang=eng"
        "&rf=http://racing.hkjc.com/en-us/local/information/btresult"
        "&pageid=racing/local"
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
                "polarity_score": h.get("polarity_score"),
                "incident_text": h.get("incident_text") or "",
            }
    return out


def _run_commentary_lookup(date_dc: str, race_no: int, horse_name: str) -> dict:
    """Return {'short', 'tags', 'incident_text'} for a given past run, or empty."""
    if not date_dc or not race_no or not horse_name:
        return {}
    cm = _load_commentary(date_dc)
    return cm.get((int(race_no), horse_name.upper()), {})


def _load_commentary_races(date_dc: str) -> dict:
    """Load the *full* commentary_YYYYMMDD.json as {race_number: race_dict}.

    Unlike ``_load_commentary`` (which flattens to per-horse short/tags for the
    form-guide cell), this preserves the race-level ``narrative``,
    ``blackbook_suggestions`` and the complete per-horse objects so the Results
    page can render the post-race commentary view."""
    path = REPORTS / f"commentary_{date_dc}.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {r.get("race_number"): r for r in raw.get("races", [])}


def _commentary_tag_chips(tags, polarity_score=None) -> str:
    """Render a horse's commentary tags as small coloured chips.

    Colour follows polarity: ``+`` (trip/excuse → forgive) green, ``-``
    (keen/bled/weakened) red, neutral grey. ``polarity_score`` (signed int from
    race_commentary.py) tints the whole group when individual polarity is
    unknown."""
    if not tags:
        return ""
    pos_tags = {
        "too_far_back", "held_up", "no_clear_run", "bumped_start",
        "crowded", "hampered_start", "unbalanced", "wide_no_cover",
        "jumped_fairly", "wide_all_way",
    }
    neg_tags = {"bleeding", "vet_hold", "raced_keenly", "disappointing", "weakened"}

    def _tag_cols(tag):
        t = str(tag).strip()
        if t in pos_tags:
            return "#13351f", "#3fb950"
        if t in neg_tags:
            return "#3a1416", "#f85149"
        if isinstance(polarity_score, (int, float)) and polarity_score > 0:
            return "#13351f", "#3fb950"
        if isinstance(polarity_score, (int, float)) and polarity_score < 0:
            return "#3a1416", "#f85149"
        return "#21262d", "#9da7b3"

    chips = ""
    for t in tags:
        bg, fg = _tag_cols(t)
        chips += (
        f'<span style="display:inline-block;background:{bg};color:{fg};'
        f'border-radius:8px;padding:1px 7px;margin:1px 3px 1px 0;'
        f'font-size:10px;font-weight:600;white-space:nowrap">'
        f'{str(t).replace("_", " ")}</span>'
        )
    return chips

# ── Playwright browser pre-install (Streamlit Cloud has no post-install hook) ─
def _ensure_playwright_chromium():
    """Install Playwright Chromium once per container boot (cached in session).

    NOTE: Do NOT use sync_playwright() inline here. Streamlit's Windows event
    loop (SelectorEventLoop) cannot spawn subprocesses from sync_playwright,
    which raises NotImplementedError on every page load. Instead we probe via
    subprocess only.
    """
    if st.session_state.get("_pw_checked"):
        return
    st.session_state["_pw_checked"] = True
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True, timeout=300,
            )
    except Exception:
        try:
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True, timeout=300,
            )
        except Exception:
            pass

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

    /* Pace bar — colour + fill track race tempo:
       fast  → high fill, green  (good for closers / pressure races)
       slow  → low  fill, red    (slow-run, leader-friendly)
       avg   → mid  fill, amber  (neutral tempo)                          */
    .pace-bar-wrap {
        display: inline-flex; align-items: center; gap: 7px;
        flex-shrink: 0; vertical-align: middle;
    }
    .pace-bar-track {
        display: inline-block;
        width: 110px; height: 10px; border-radius: 5px;
        background: rgba(128,128,128,0.22);
        border: 1px solid rgba(128,128,128,0.45);
        overflow: hidden; flex-shrink: 0;
        vertical-align: middle;
        position: relative;
    }
    .pace-bar-fill {
        display: block;
        height: 100%; min-height: 8px;
        border-radius: 5px 0 0 5px;
        transition: width 0.25s;
    }
    .pace-fast    { background: #22c55e; }  /* green  */
    .pace-sl-fast { background: #86efac; }  /* light  green */
    .pace-neutral { background: #f59e0b; }  /* amber */
    .pace-sl-slow { background: #fca5a5; }  /* light  red   */
    .pace-slow    { background: #ef4444; }  /* red    */
    .pace-label           { font-size: 0.8em; font-weight: 700; }
    .pace-label.fast      { color: #22c55e; }
    .pace-label.sl-fast   { color: #4ade80; }
    .pace-label.neutral   { color: #f59e0b; }
    .pace-label.sl-slow   { color: #f87171; }
    .pace-label.slow      { color: #ef4444; }

    /* ══ THEMED RACE-CARD TABLE (Styler HTML) ══
       Used in place of st.dataframe for the primary race cards so that
       light/dark mode CSS actually applies (glide-data-grid canvas cannot
       be themed via CSS).                                                */
    .themed-table { margin: 2px 0 10px 0; overflow-x: auto; }
    .themed-table table {
        width: 100%; border-collapse: collapse;
        font-family: 'JetBrains Mono', 'Courier New', monospace;
        font-size: 0.82em;
    }
    .themed-table th, .themed-table td {
        padding: 6px 10px; border-bottom: 1px solid rgba(128,128,128,0.18);
        white-space: nowrap;
    }
    .themed-table th {
        background: rgba(230,57,70,0.10);
        color: inherit; font-weight: 700; text-align: center;
        letter-spacing: 0.03em; text-transform: uppercase; font-size: 0.78em;
        border-bottom: 1px solid rgba(230,57,70,0.35);
    }
    .themed-table tbody tr:hover { background: rgba(230,57,70,0.06); }
    .themed-table tbody tr:nth-child(even) {
        background: rgba(128,128,128,0.05);
    }

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
    .form-tbl a.vid-link {
        color: #1f6feb; text-decoration: none;
        font-weight: 700; font-size: 1.05em;
    }
    .form-tbl a.vid-link:hover { text-decoration: underline; color: #4c8ef0; }
    .form-tbl td.form-comment {
        font-size: 0.82em; opacity: 0.85; font-style: italic;
        max-width: 260px; white-space: normal; line-height: 1.3;
    }
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

    /* ══ RESPONSIVE / NARROW SCREENS (#18) ══ */
    @media (max-width: 640px) {
        /* Tighten the main content padding so tables get more room. */
        .block-container {
            padding-left: 0.6rem !important;
            padding-right: 0.6rem !important;
            padding-top: 2.6rem !important;
        }
        /* Shrink page chrome. */
        .page-title { font-size: 1.0em !important; }
        div[data-testid="stMetric"] [data-testid="stMetricValue"] {
            font-size: 1.15em !important;
        }
        div[data-testid="stMetric"] { padding: 7px 9px !important; }
        /* Race-tab / R1…Rn button rows: compact so they don't overflow. */
        div[data-testid="stHorizontalBlock"] .stButton > button {
            padding: 2px 4px !important;
            font-size: 0.78em !important;
        }
        /* Let dataframes scroll horizontally instead of squashing. */
        [data-testid="stDataFrame"] { overflow-x: auto !important; }
        /* Keep the sticky race-tab bar clear of the smaller mobile header. */
        .element-container:has(#rd-tabbar-anchor)
            + div[data-testid="stHorizontalBlock"] {
            top: 2.4rem !important;
        }
    }
</style>
""", unsafe_allow_html=True)


# ── Light-mode toggle (persisted per-session) ─────────────────────────────────
if "light_mode" not in st.session_state:
    st.session_state["light_mode"] = False

st.sidebar.markdown(
    '<div class="sb-brand" style="padding:4px 14px 8px;border:none;margin-bottom:0">'
    '<span style="font-size:0.78em;opacity:0.6;letter-spacing:0.06em;">APPEARANCE</span>'
    '</div>', unsafe_allow_html=True
)
_lm_prev = st.session_state["light_mode"]
_lm = st.sidebar.toggle(
    "☀ Light mode" if not _lm_prev else "🌙 Dark mode",
    value=_lm_prev,
    key="__light_mode_toggle__",
    help="Switch between light and dark dashboard theme",
)
if _lm != _lm_prev:
    st.session_state["light_mode"] = _lm
    st.rerun()

if st.session_state["light_mode"]:
    st.markdown("""
<style>
/* ══ LIGHT-MODE OVERRIDE ══ */
html, body, .stApp,
[data-testid="stAppViewContainer"],
[data-testid="stHeader"],
[data-testid="stMain"],
[data-testid="stMainBlockContainer"] {
    background-color: #fafaf7 !important;
    color: #1a1a1a !important;
}
[data-testid="stSidebar"],
[data-testid="stSidebarContent"] {
    background-color: #f0ede6 !important;
    color: #1a1a1a !important;
}
[data-testid="stSidebar"] * { color: #1a1a1a !important; }
/* Headings / titles */
h1, h2, h3, h4, h5, h6, .page-title { color: #111 !important; }
/* Markdown bodies */
.stMarkdown, [data-testid="stMarkdownContainer"],
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li,
[data-testid="stMarkdownContainer"] span {
    color: #1a1a1a !important;
}
/* Keep our coloured spans */
.pace-label.fast     { color: #16803c !important; }
.pace-label.sl-fast  { color: #16803c !important; }
.pace-label.neutral  { color: #b45309 !important; }
.pace-label.sl-slow  { color: #b91c1c !important; }
.pace-label.slow     { color: #b91c1c !important; }
/* Race header block */
.race-hdr-block {
    background: #ffffff !important;
    border-left: 3px solid #e63946 !important;
    color: #1a1a1a !important;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
.race-hdr-meta, .race-hdr-title { color: #1a1a1a !important; }
/* Tables */
table, th, td { color: #1a1a1a !important; }
.stDataFrame, [data-testid="stDataFrame"] { background-color: #fff !important; }
[data-testid="stDataFrame"] div { color: #1a1a1a !important; }
/* Note: st.dataframe (glide-data-grid canvas) cannot be themed via CSS.
   Race-card tables now render as HTML via `.themed-table` which does
   respect these variables.                                                */
/* Inline HTML tables we control (speed map, research panel, themed-table) */
[data-testid="stMarkdownContainer"] table { background-color: transparent !important; }
[data-testid="stMarkdownContainer"] th,
[data-testid="stMarkdownContainer"] td { color: #1a1a1a !important; }
.themed-table table { background: #ffffff !important; }
.themed-table th { background: rgba(230,57,70,0.08) !important; color: #1a1a1a !important; }
.themed-table tbody tr:nth-child(even) { background: #f7f7f5 !important; }
.themed-table tbody tr:hover { background: #fff1f2 !important; }
/* Speed map header bars use inline #2C3E50 → soften to readable slate on light */
[data-testid="stMarkdownContainer"] td[style*="#2C3E50"] {
    background: #334155 !important;  /* keep readable slate header */
    color: #fff !important;
}
/* Inputs / buttons */
.stButton > button,
.stDownloadButton > button {
    background-color: #fff !important;
    color: #1a1a1a !important;
    border: 1px solid rgba(0,0,0,0.2) !important;
}
.stButton > button:hover { background-color: #f3f3f3 !important; }
input, textarea, select,
[data-baseweb="input"] input,
[data-baseweb="select"] * {
    background-color: #fff !important;
    color: #1a1a1a !important;
}
/* Expander, tabs */
[data-testid="stExpander"] { background-color: #fff !important; }
[data-baseweb="tab"] { color: #1a1a1a !important; }
/* Pace bar — nudge track for legibility on light bg */
.pace-bar-track {
    background: rgba(0,0,0,0.08) !important;
    border-color: rgba(0,0,0,0.15) !important;
}
/* Code / mono backgrounds */
code, pre, .stCode {
    background-color: #f3f3f3 !important;
    color: #1a1a1a !important;
}
/* Alerts retain colour but softer backgrounds */
[data-baseweb="notification"] { color: #1a1a1a !important; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# Data loading
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300)
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


@st.cache_data(ttl=120, show_spinner=False)
def _load_meeting_data_cached(path_str: str, mtime: float, size: int) -> dict:
    """Internal cached loader. Keyed by (path, mtime, size) so the cache
    auto-invalidates whenever the file is rewritten by run_meeting.py
    or _append_results_to_db. ``mtime``/``size`` are part of the cache
    key only — they are not used inside the body."""
    del mtime, size  # cache-key only
    with open(path_str, "r", encoding="utf-8") as f:
        return json.load(f)


def load_meeting_data(path: Path) -> dict:
    """Load full meeting JSON data (mtime-cached, 120s TTL).

    Re-parses automatically when the underlying file is rewritten, so
    callers never see stale data after a Run Analysis / scrape Results
    pass — but repeated reads within the same Streamlit rerun (and
    across reruns within 120s) hit the cache instead of re-parsing
    multi-MB JSON each time.
    """
    try:
        st_ = os.stat(path)
        return _load_meeting_data_cached(str(path), st_.st_mtime, st_.st_size)
    except OSError:
        # Path vanished between caller's existence check and stat() —
        # fall back to the original direct read so the caller gets the
        # same FileNotFoundError it would have seen before.
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def load_sarr_data(date_str: str) -> dict | None:
    """Load SARR JSON report for a given date (YYYYMMDD). Returns None if missing.

    NOT cached on purpose — SARR JSON may be rewritten on every analysis run
    and we don't want stale results lingering after Run Analysis completes.
    Cheap read (single JSON parse), so the lack of caching is fine.
    """
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
    token = _gh_token_value()
    if not token:
        return None
    return {"Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"}


def _gh_token_value() -> str:
    """Return a normalized GitHub token from Streamlit secrets or env."""
    token = st.secrets.get("GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
    token = str(token or "").strip()
    m = re.search(
        r"(?im)\bGITHUB_TOKEN\b\s*=\s*(?P<quote>['\"]?)(?P<value>[^'\"\r\n#,]+)(?P=quote)",
        token,
    )
    if m:
        token = m.group("value").strip()
    lower = token.lower()
    for prefix in ("token ", "bearer "):
        if lower.startswith(prefix):
            token = token[len(prefix):].strip()
            lower = token.lower()
            break
    m = re.search(r"(github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+)", token)
    if m:
        token = m.group(1)
    token = token.rstrip(",").strip().strip('"').strip("'").strip()
    return token


def _gh_auth_block_reason() -> str:
    """Return a session-local auth block reason after GitHub rejects a token."""
    try:
        return str(st.session_state.get("_gh_auth_block_reason", "") or "")
    except Exception:
        return ""


def _gh_note_auth_failure(status_code: int, detail: str = "") -> None:
    """Block further GitHub writes in this session after an auth rejection."""
    msg = f"token rejected (HTTP {status_code}) — update GITHUB_TOKEN secret"
    if detail and "bad credentials" in detail.lower():
        msg = "token rejected (Bad credentials) — expired, revoked, or malformed GITHUB_TOKEN"
    try:
        if st.session_state.get("_gh_auth_block_reason") != msg:
            st.session_state["_gh_auth_block_reason"] = msg
            _gh_record_error(f"GitHub sync disabled for this session: {msg}")
    except Exception:
        pass
    try:
        _gh_token_check.clear()
    except Exception:
        pass


def _gh_get_file_sha() -> str | None:
    """Get the current SHA of blackbook.json on GitHub (needed for updates)."""
    if _gh_auth_block_reason():
        return None
    headers = _gh_headers()
    if not headers:
        return None
    try:
        r = _requests.get(
            f"https://api.github.com/repos/{_GH_REPO}/contents/{_GH_BB_PATH}",
            headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json().get("sha")
        if r.status_code == 401:
            _gh_note_auth_failure(r.status_code, r.text)
    except Exception:
        pass
    return None


def _gh_push_blackbook(content_bytes: bytes) -> bool:
    """Push blackbook.json to GitHub via the Contents API."""
    if _gh_auth_block_reason():
        return False
    headers = _gh_headers()
    if not headers:
        _gh_record_error("blackbook.json: no GITHUB_TOKEN")
        return False
    sha = _gh_get_file_sha()
    payload = {
        "message": f"blackbook: auto-sync {date.today().isoformat()}",
        "content": base64.b64encode(content_bytes).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    if _gh_auth_block_reason():
        return False
    try:
        r = _requests.put(
            f"https://api.github.com/repos/{_GH_REPO}/contents/{_GH_BB_PATH}",
            headers=headers, json=payload, timeout=15)
        if r.status_code in (200, 201):
            _gh_record_push(_GH_BB_PATH)
            return True
        if r.status_code == 401:
            _gh_note_auth_failure(r.status_code, r.text)
        _gh_record_error(
            f"blackbook.json: HTTP {r.status_code} {r.text[:160]}")
        return False
    except Exception as e:
        _gh_record_error(f"blackbook.json: {e}")
        return False


def _is_streamlit_cloud() -> bool:
    """True when running on Streamlit Community Cloud (ephemeral filesystem).

    Streamlit Cloud doesn't reliably set a single canonical env-var, so we
    probe several signals: the documented ``STREAMLIT_SERVER_HEADLESS``
    flag, the ``/mount/src`` working-directory prefix used by the
    platform, and the ``HOSTNAME``/``HOME`` patterns. Any one is enough.
    """
    if os.environ.get("STREAMLIT_SERVER_HEADLESS", "").lower() == "true":
        return True
    try:
        cwd = str(Path.cwd())
        if cwd.startswith("/mount/src") or "/mount/src/" in cwd:
            return True
    except Exception:
        pass
    if os.environ.get("HOSTNAME", "").startswith("streamlit"):
        return True
    if os.environ.get("HOME") == "/home/appuser":
        return True
    return False


@st.cache_data(ttl=300, show_spinner=False)
def _gh_token_check() -> tuple[bool, bool, str]:
    """Return ``(token_present, repo_writable, detail)``.

    Cached for 5 minutes so the sidebar panel doesn't hammer the GitHub
    API on every rerun. Performs a lightweight ``GET /repos/<repo>`` call;
    a 200 with ``permissions.push == True`` means we can write.
    """
    blocked = _gh_auth_block_reason()
    if blocked:
        return (True, False, blocked)
    token = _gh_token_value()
    if not token:
        return (False, False, "no token configured")
    try:
        r = _requests.get(
            f"https://api.github.com/repos/{_GH_REPO}",
            headers={"Authorization": f"token {token}",
                     "Accept": "application/vnd.github.v3+json"},
            timeout=8)
        if r.status_code == 200:
            perms = (r.json() or {}).get("permissions", {})
            if perms.get("push"):
                return (True, True, "OK (push permission verified)")
            return (True, False, "token valid but lacks push permission")
        if r.status_code == 401:
            return (True, False, "token rejected (401) — expired or revoked")
        if r.status_code == 404:
            return (True, False, f"repo {_GH_REPO} not found via this token (404)")
        return (True, False, f"GitHub HTTP {r.status_code}")
    except Exception as e:
        return (True, False, f"network error: {e}")



# ══════════════════════════════════════════════════════════════════════════════

def _ntfy_topic() -> str:
    """Resolve NTFY_TOPIC from Streamlit secrets or env. Empty string if unset."""
    try:
        v = st.secrets.get("NTFY_TOPIC", "")
    except Exception:
        v = ""
    return v or os.environ.get("NTFY_TOPIC", "") or ""


def _push_notify(title: str, body: str, tags: str = "racing",
                 priority: int = 3, click: str = "") -> bool:
    """Send a push notification via ntfy.sh. No-op if NTFY_TOPIC not configured.

    Topic is a private UUID string stored in Streamlit secrets (and GitHub
    Actions secrets for the cron pre-race reminder). User subscribes once
    in Chrome on their phone at ``https://ntfy.sh/<topic>`` and accepts
    the Web Push permission prompt — no app install required.

    Args:
        title: short header (≤100 chars)
        body: free text, supports newlines
        tags: comma-separated ntfy tag aliases (renders as emoji prefix)
        priority: 1 (min) .. 5 (urgent, bypasses DND on most phones)
        click: optional URL the notification opens on tap
    Returns True if delivered.
    """
    topic = _ntfy_topic()
    if not topic:
        return False
    try:
        import requests as _req
        headers = {
            "Title": title[:100],
            "Tags": tags,
            "Priority": str(int(priority)),
        }
        if click:
            headers["Click"] = click
        resp = _req.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers=headers,
            timeout=5,
        )
        return resp.status_code == 200
    except Exception:
        return False


def _render_push_sidebar() -> None:
    """Sidebar expander for push-notification setup/status."""
    topic = _ntfy_topic()
    with st.sidebar.expander("📲 Phone push", expanded=False):
        if not topic:
            st.caption(
                "Add `NTFY_TOPIC = \"<your-uuid>\"` to Streamlit secrets, "
                "then visit `https://ntfy.sh/<your-uuid>` in Chrome on your "
                "phone and tap **Subscribe → Enable web notifications**."
            )
            st.markdown(
                "Generate a private topic UUID: "
                "`python -c \"import uuid;print(uuid.uuid4())\"`"
            )
        else:
            mask = topic[:8] + "…" + topic[-4:] if len(topic) > 12 else topic
            st.caption(f"Topic: `{mask}` · ready")
            st.markdown(f"[Subscribe in Chrome →](https://ntfy.sh/{topic})")
            if st.button("🔔 Send test", key="push_test_btn",
                         width='stretch'):
                ok = _push_notify(
                    "🔔 Dashboard test",
                    f"Push notifications wired up · {date.today().isoformat()}",
                    tags="white_check_mark",
                    priority=3,
                )
                if ok:
                    st.toast("Sent — check your phone")
                else:
                    st.toast("Failed — see logs")



def _gh_emergency_sync_all() -> tuple[int, int, list[str]]:
    """Walk every persistence-relevant file on disk and push to GitHub.

    Used by the sidebar **[ Sync All Data → GitHub ]** button as a safety
    valve when the user notices data wasn't auto-synced. Returns
    ``(n_pushed, n_failed, sample_errors)``.

    Pushes:
      - ``blackbook.json``
      - ``reports/user_bets_log.jsonl``
      - ``reports/{results,dividends,incidents,commentary,backtest,
            backtest_unified,trials}_*.json``
      - ``reports/race_day_report_*.json`` and ``race_day_analysis_*.txt``
      - ``reports/vet_report_*.json``
      - ``racecards/racecard_*.xlsx``
      - ``cache/racecard_*.json`` and ``cache/form_guide_*.json``
      - ``running_position_photos/*/R*.json|jpg``
    """
    if not _gh_headers():
        return (0, 0, ["no GITHUB_TOKEN — cannot sync"])

    candidates: list[tuple[Path, str]] = []

    def _add(p: Path, repo_path: str):
        if p.exists() and p.is_file():
            candidates.append((p, repo_path))

    _add(BLACKBOOK_FILE, "blackbook.json")
    _add(REPORTS / "user_bets_log.jsonl", "reports/user_bets_log.jsonl")

    # reports/ JSONs we know we want to persist
    if REPORTS.exists():
        report_globs = (
            "results_*.json", "dividends_*.json", "incidents_*.json",
            "commentary_*.json", "backtest_*.json", "backtest_unified_*.json",
            "trials_*.json", "race_day_report_*.json",
            "race_day_analysis_*.txt", "vet_report_*.json",
            "factor_analysis_tables.json",
        )
        for pat in report_globs:
            for f in REPORTS.glob(pat):
                _add(f, f"reports/{f.name}")

    # racecards/
    rc_dir = BASE / "racecards"
    if rc_dir.exists():
        for f in rc_dir.glob("racecard_*.xlsx"):
            _add(f, f"racecards/{f.name}")

    # cache/
    cache_dir = BASE / "cache"
    if cache_dir.exists():
        for pat in ("racecard_*.json", "form_guide_*.json"):
            for f in cache_dir.glob(pat):
                _add(f, f"cache/{f.name}")

    # running-position photos (per-meeting subdirs)
    rp_root = BASE / "running_position_photos"
    if rp_root.exists():
        for sub in rp_root.iterdir():
            if not sub.is_dir():
                continue
            for f in sub.glob("R*.json"):
                _add(f, f"running_position_photos/{sub.name}/{f.name}")
            for f in sub.glob("R*.jpg"):
                _add(f, f"running_position_photos/{sub.name}/{f.name}")

    n_ok = 0
    errors: list[str] = []
    msg = f"sync-all: emergency persist {date.today().isoformat()} [skip ci]"
    progress = st.progress(0.0, text=f"Pushing {len(candidates)} file(s)…")
    for i, (local, repo_path) in enumerate(candidates, 1):
        try:
            if _gh_push_file(repo_path, local.read_bytes(), msg):
                n_ok += 1
            else:
                errors.append(repo_path)
        except Exception as e:
            errors.append(f"{repo_path}: {e}")
        progress.progress(i / max(1, len(candidates)),
                          text=f"Pushed {n_ok}/{len(candidates)}…")
    progress.empty()
    return (n_ok, len(errors), errors[:8])


def _render_persistence_sidebar() -> None:
    """Render the Cloud Persistence status panel in the sidebar.

    Shows whether we are on Streamlit Cloud, whether ``GITHUB_TOKEN`` is
    configured + valid, the most-recent successful push, recent push
    errors, and an emergency **Sync All Data → GitHub** button. The
    panel only renders when running on cloud — locally the workspace
    folder is the source of truth and persistence is a no-op.
    """
    if not _is_streamlit_cloud():
        return

    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
    st.sidebar.markdown('<div class="sb-nav-section">Cloud Persistence</div>',
                        unsafe_allow_html=True)

    has_tok, can_push, detail = _gh_token_check()
    if has_tok and can_push:
        st.sidebar.success(f"☁ GitHub sync: {detail}", icon="✅")
    elif has_tok:
        st.sidebar.error(f"⚠ GitHub sync DISABLED — {detail}", icon="🚨")
        st.sidebar.caption(
            "All scrapes / bets / results written this session will be "
            "**lost** on next reboot until this is fixed. Update the "
            "`GITHUB_TOKEN` secret in Streamlit Cloud → app settings."
        )
    else:
        st.sidebar.error("⚠ No `GITHUB_TOKEN` secret — data is NOT being "
                         "persisted across reboots.", icon="🚨")
        st.sidebar.caption(
            "Configure a fine-grained PAT with **Contents: write** scope "
            "for repo " + _GH_REPO + " in Streamlit Cloud secrets, "
            "then refresh this page."
        )

    last = st.session_state.get("_gh_last_push")
    pushes = st.session_state.get("_gh_push_count", 0)
    if last:
        ts, repo_path = last
        st.sidebar.caption(f"Last push: `{repo_path}` @ {ts} "
                           f"(total this session: {pushes})")

    errs = st.session_state.get("_gh_errors") or []
    if errs:
        with st.sidebar.expander(f"⚠ {len(errs)} push error(s) this session",
                                 expanded=False):
            for line in errs[-10:]:
                st.code(line, language="text")
            if st.button("Clear errors", key="_gh_clear_errs",
                         width='stretch'):
                st.session_state["_gh_errors"] = []
                st.rerun()

    if st.sidebar.button(
        "[ Sync All Data → GitHub ]",
        key="_gh_sync_all_btn",
        width='stretch',
        help="Walks racecards/, cache/, reports/, blackbook.json, "
             "user_bets_log.jsonl and running_position_photos/ and pushes "
             "everything to GitHub. Use this if data wasn't auto-synced "
             "(e.g. token was missing during the session).",
        disabled=not (has_tok and can_push),
    ):
        n_ok, n_fail, sample = _gh_emergency_sync_all()
        if n_ok and not n_fail:
            st.sidebar.success(f"☁ Synced {n_ok} file(s) to GitHub.")
        elif n_ok:
            st.sidebar.warning(
                f"Pushed {n_ok}, failed {n_fail}. First errors:\n"
                + "\n".join(f"• {x}" for x in sample))
        else:
            st.sidebar.error("Nothing pushed. Errors:\n"
                             + "\n".join(f"• {x}" for x in sample))


def _gh_get_path_sha(repo_path: str) -> str | None:
    """Get SHA for an arbitrary path on GitHub. Returns None if missing/no auth."""
    if _gh_auth_block_reason():
        return None
    headers = _gh_headers()
    if not headers:
        return None
    try:
        r = _requests.get(
            f"https://api.github.com/repos/{_GH_REPO}/contents/{repo_path}",
            headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json().get("sha")
        if r.status_code == 401:
            _gh_note_auth_failure(r.status_code, r.text)
    except Exception:
        pass
    return None


def _gh_record_error(msg: str) -> None:
    """Append a push-error message to a small ring buffer in session state.

    Used so that silent ``return False`` paths surface as visible warnings
    in the sidebar persistence panel. Capped at 30 entries.
    """
    try:
        buf = st.session_state.setdefault("_gh_errors", [])
        ts = hkt_now().strftime("%H:%M:%S") + " HKT"
        buf.append(f"[{ts}] {msg}")
        del buf[:-30]
    except Exception:
        pass


def _gh_record_push(repo_path: str) -> None:
    """Track the most-recent successful push for the status panel."""
    try:
        st.session_state["_gh_last_push"] = (
            hkt_now().strftime("%H:%M:%S") + " HKT", repo_path)
        st.session_state["_gh_push_count"] = (
            st.session_state.get("_gh_push_count", 0) + 1)
    except Exception:
        pass


def _gh_push_file(repo_path: str, content_bytes: bytes, message: str) -> bool:
    """Generic single-file push via the GitHub Contents API.

    Used to persist pipeline-generated artifacts (racecards, reports, caches)
    so they survive Streamlit Cloud restarts. Returns True on success.

    On failure the reason is recorded via :func:`_gh_record_error` so the
    sidebar persistence panel can surface it (instead of silent loss).
    Retries once on 409/422 (sha race) by re-fetching the SHA.
    """
    if _gh_auth_block_reason():
        return False
    headers = _gh_headers()
    if not headers:
        _gh_record_error(f"{repo_path}: no GITHUB_TOKEN")
        return False
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
    }
    sha = _gh_get_path_sha(repo_path)
    if _gh_auth_block_reason():
        return False
    if sha:
        payload["sha"] = sha
    try:
        r = _requests.put(
            f"https://api.github.com/repos/{_GH_REPO}/contents/{repo_path}",
            headers=headers, json=payload, timeout=30)
        if r.status_code in (200, 201):
            _gh_record_push(repo_path)
            return True
        if r.status_code == 401:
            _gh_note_auth_failure(r.status_code, r.text)
        # Retry once on sha conflict (someone else pushed between get/put)
        if r.status_code in (409, 422):
            sha2 = _gh_get_path_sha(repo_path)
            if sha2 and sha2 != sha:
                payload["sha"] = sha2
                r2 = _requests.put(
                    f"https://api.github.com/repos/{_GH_REPO}/contents/{repo_path}",
                    headers=headers, json=payload, timeout=30)
                if r2.status_code in (200, 201):
                    _gh_record_push(repo_path)
                    return True
                if r2.status_code == 401:
                    _gh_note_auth_failure(r2.status_code, r2.text)
                _gh_record_error(
                    f"{repo_path}: HTTP {r2.status_code} {r2.text[:160]}")
                return False
        _gh_record_error(f"{repo_path}: HTTP {r.status_code} {r.text[:160]}")
        return False
    except Exception as e:
        _gh_record_error(f"{repo_path}: {e}")
        return False


def _gh_push_user_bets() -> bool:
    """Sync reports/user_bets_log.jsonl to GitHub so it survives Streamlit
    Cloud restarts.

    Pushes whenever a ``GITHUB_TOKEN`` is configured — not only on cloud.
    This avoids the previous failure mode where ``_is_streamlit_cloud()``
    mis-detected the runtime, the push was silently skipped, and imports
    appeared to revert on the next container restart.
    """
    if not _gh_headers():
        return False
    bets_file = REPORTS / "user_bets_log.jsonl"
    if not bets_file.exists():
        return False
    try:
        data = bets_file.read_bytes()
    except Exception:
        return False
    return _gh_push_file(
        "reports/user_bets_log.jsonl",
        data,
        f"my-bets: auto-sync {date.today().isoformat()} [skip ci]",
    )


def _gh_push_trials(trial_date_iso: str) -> bool:
    """Sync trials_YYYYMMDD.json to GitHub. No-op locally."""
    if not _is_streamlit_cloud():
        return False
    if not _gh_headers():
        return False
    dc = trial_date_iso.replace("-", "")
    f = REPORTS / f"trials_{dc}.json"
    if not f.exists():
        return False
    try:
        data = f.read_bytes()
    except Exception:
        return False
    return _gh_push_file(
        f"reports/trials_{dc}.json",
        data,
        f"trials: auto-sync {trial_date_iso} [skip ci]",
    )


def _gh_persist_postrace_outputs(date_str: str) -> tuple[int, int, list[str]]:
    """Push post-race pipeline artifacts back to GitHub on Streamlit Cloud.

    Mirrors `_gh_persist_pipeline_outputs` for the full results scraper path
    (results / incidents / RP photos+OCR / commentary / backtest). Running-
    position photos and OCR JSONs are pushed per-race (R1..R12 typically).
    No-op locally.
    """
    if not _is_streamlit_cloud():
        return (0, 0, [])
    if not _gh_headers():
        return (0, 0, ["No GITHUB_TOKEN — post-race outputs will be lost on next restart"])

    dc = date_str.replace("-", "")
    candidates: list[tuple[Path, str]] = [
        (REPORTS / f"results_{dc}.json",       f"reports/results_{dc}.json"),
        (REPORTS / f"dividends_{dc}.json",     f"reports/dividends_{dc}.json"),
        (REPORTS / f"incidents_{dc}.json",     f"reports/incidents_{dc}.json"),
        (REPORTS / f"commentary_{dc}.json",    f"reports/commentary_{dc}.json"),
        (REPORTS / f"backtest_{dc}.json",      f"reports/backtest_{dc}.json"),
        (REPORTS / f"backtest_unified_{dc}.json", f"reports/backtest_unified_{dc}.json"),
        (BASE / "cache" / "race_pace_index.json", "cache/race_pace_index.json"),
        # Form guide cache often gets lane data added during step 6.
        (BASE / "cache" / f"form_guide_{date_str}.json",
         f"cache/form_guide_{date_str}.json"),
    ]
    rp_dir = BASE / "running_position_photos" / dc
    if rp_dir.exists():
        for f in sorted(rp_dir.glob("R*.json")):
            candidates.append((f, f"running_position_photos/{dc}/{f.name}"))
        # JPGs are larger but small enough for the Contents API; keep them
        # so re-OCR can run on a fresh container without re-scraping HKJC.
        for f in sorted(rp_dir.glob("R*.jpg")):
            candidates.append((f, f"running_position_photos/{dc}/{f.name}"))

    pushed = 0
    missing = 0
    errors: list[str] = []
    msg = f"post-race: auto-sync {date_str} [skip ci]"
    for local, repo_path in candidates:
        if not local.exists():
            missing += 1
            continue
        try:
            data = local.read_bytes()
            if _gh_push_file(repo_path, data, msg):
                pushed += 1
            else:
                errors.append(repo_path)
        except Exception as e:
            errors.append(f"{repo_path}: {e}")
    return (pushed, missing, errors)


def _gh_persist_live_odds_snapshots(date_iso: str, venue: str
                                    , file_names: list[str] | None = None
                                    ) -> tuple[int, int, list[str]]:
    """Push live-odds JSON snapshots to GitHub so they survive Cloud reboots.

    Streamlit Cloud's filesystem is ephemeral — anything not in git is wiped
    on every redeploy. Without this, scraping odds in the deployed app
    produces snapshots that vanish on the next push, and gradual price
    movements are lost. This function mirrors `_gh_persist_postrace_outputs`
    for the `cache/live_odds/YYYYMMDD/{VENUE}_R*.json` files.

    Returns (n_pushed, n_skipped_missing, errors). No-op locally (local
    workflows commit via git directly).
    """
    if not _is_streamlit_cloud():
        return (0, 0, [])
    if not _gh_headers():
        return (0, 0, ["No GITHUB_TOKEN — live odds snapshots will be lost on restart"])

    dc = date_iso.replace("-", "")
    snap_dir = BASE / "cache" / "live_odds" / dc
    if not snap_dir.exists():
        return (0, 0, [])

    pushed = 0
    errors: list[str] = []
    msg = f"live-odds: auto-sync {date_iso} {venue} [skip ci]"
    wanted = set(file_names or [])
    fps = sorted(snap_dir.glob(f"{venue}_R*.json"))
    if wanted:
        fps = [fp for fp in fps if fp.name in wanted]
    for fp in fps:
        repo_path = f"cache/live_odds/{dc}/{fp.name}"
        try:
            data = fp.read_bytes()
            if _gh_push_file(repo_path, data, msg):
                pushed += 1
            else:
                errors.append(repo_path)
        except Exception as e:
            errors.append(f"{repo_path}: {e}")
    return (pushed, 0, errors)


@lru_cache(maxsize=4)
def _ensure_playwright_runtime(python_exe: str) -> tuple[bool, str]:
    """Probe/install Playwright Chromium once per container/interpreter.

    Live-odds scraping is the only feature that needs Chromium. Doing this on
    every scrape burns CPU and can destabilize a small Streamlit host.
    """
    try:
        probe = subprocess.run(
            [python_exe, "-m", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True, text=True, timeout=20, cwd=str(BASE),
        )
        loc = ""
        for line in (probe.stdout or "").splitlines():
            if "Install location:" in line:
                loc = line.split("Install location:", 1)[1].strip()
                break
        if probe.returncode == 0 and loc and Path(loc).exists():
            return True, "chromium already installed"
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        probe_err = f"dry-run probe failed: {type(exc).__name__}: {exc}"
    else:
        probe_err = (probe.stderr or probe.stdout or f"dry-run exit {probe.returncode}").strip()

    try:
        install = subprocess.run(
            [python_exe, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=180, cwd=str(BASE),
        )
        if install.returncode == 0:
            return True, "chromium installed"
        msg = (install.stderr or install.stdout or f"install exit {install.returncode}").strip()
        return False, f"Playwright install failed: {msg[:200]}"
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return False, f"Playwright install failed after {probe_err[:120]}: {type(exc).__name__}: {exc}"


def _gh_persist_pipeline_outputs(date_str: str, model: str) -> tuple[int, int, list[str]]:
    """Push all artifacts a single pipeline run produces back to GitHub.

    Only operates when running on Streamlit Cloud (ephemeral FS) AND a
    GITHUB_TOKEN is configured. Skips silently otherwise (local runs commit
    via git directly).

    Returns (n_pushed, n_skipped_missing, errors).
    """
    if not _is_streamlit_cloud():
        return (0, 0, [])
    if not _gh_headers():
        return (0, 0, ["No GITHUB_TOKEN — pipeline outputs will be lost on next restart"])

    dc = date_str.replace("-", "")
    candidates: list[tuple[Path, str]] = [
        # (local file, repo path)
        (BASE / "racecards" / f"racecard_{dc}.xlsx",            f"racecards/racecard_{dc}.xlsx"),
        (BASE / "cache" / f"racecard_{date_str}.json",          f"cache/racecard_{date_str}.json"),
        (BASE / "cache" / f"form_guide_{date_str}.json",        f"cache/form_guide_{date_str}.json"),
        (REPORTS / f"race_day_report_{dc}_{model}.json",        f"reports/race_day_report_{dc}_{model}.json"),
        (REPORTS / f"race_day_analysis_{dc}_{model}.txt",       f"reports/race_day_analysis_{dc}_{model}.txt"),
        (REPORTS / f"race_day_report_{dc}_SARR.json",           f"reports/race_day_report_{dc}_SARR.json"),
        (REPORTS / f"race_day_report_{dc}_FUSE.json",           f"reports/race_day_report_{dc}_FUSE.json"),
        (REPORTS / f"vet_report_{dc}.json",                     f"reports/vet_report_{dc}.json"),
        (REPORTS / f"mutual_{dc}.json",                         f"reports/mutual_{dc}.json"),
        (REPORTS / f"pace_v2_{dc}.json",                        f"reports/pace_v2_{dc}.json"),
        (REPORTS / f"form_screen_{dc}.json",                    f"reports/form_screen_{dc}.json"),
    ]
    pushed = 0
    missing = 0
    errors: list[str] = []
    msg = f"pipeline: auto-sync {date_str} ({model}+SARR) [skip ci]"
    for local, repo_path in candidates:
        if not local.exists():
            missing += 1
            continue
        try:
            data = local.read_bytes()
            ok = _gh_push_file(repo_path, data, msg)
            if ok:
                pushed += 1
            else:
                errors.append(repo_path)
        except Exception as e:
            errors.append(f"{repo_path}: {e}")
    return (pushed, missing, errors)


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
    try:
        mtime = p.stat().st_mtime
    except Exception:
        mtime = 0.0
    return _load_results_json_cached(date_compact, mtime)


@st.cache_data(show_spinner=False)
def _load_results_json_cached(date_compact: str, _mtime: float) -> dict | None:
    p = REPORTS / f"results_{date_compact}.json"
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


@st.cache_data(show_spinner=False)
def _horse_name_lookup(date_compact: str) -> dict:
    """Build a {(race_number, horse_no): horse_name} map for one meeting.

    Tries reports/results_{dc}.json first (post-race, complete), falls back
    to reports/race_day_report_{dc}_v4.4.json (picks only — partial), then to
    cache/racecard_{YYYY-MM-DD}.json (pre-race, complete).
    """
    out: dict = {}
    # 1) Post-race results JSON
    try:
        rj = _load_results_json(date_compact)
        if rj:
            for race in rj.get("races", []):
                rn = int(race.get("race_number", 0) or 0)
                for ru in race.get("runners", []) or []:
                    hn = ru.get("horse_no")
                    nm = ru.get("horse_name")
                    if hn is not None and nm:
                        try:
                            out[(rn, int(hn))] = str(nm).strip()
                        except (TypeError, ValueError):
                            pass
    except (OSError, json.JSONDecodeError):
        pass
    # 2) Race-day report (picks only — fills gaps)
    try:
        rdp = REPORTS / f"race_day_report_{date_compact}_v4.4.json"
        if rdp.exists():
            d = json.loads(rdp.read_text(encoding="utf-8"))
            for race in d.get("races", []):
                rn = int(race.get("race_number", 0) or 0)
                for p in race.get("picks", []) or []:
                    hn = p.get("horse_no")
                    nm = p.get("horse_name")
                    if hn is not None and nm:
                        out.setdefault((rn, int(hn)), str(nm).strip())
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    # 3) Pre-race racecard cache (full field)
    try:
        iso = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"
        rc = CACHE_DIR / f"racecard_{iso}.json"
        if rc.exists():
            d = json.loads(rc.read_text(encoding="utf-8"))
            for race in d.get("races", []):
                rn = int((race.get("meta") or {}).get("race_no", 0) or 0)
                if not rn:
                    # try alternate keys
                    rn = int(race.get("race_number", 0) or 0)
                for h in race.get("horses", []) or []:
                    hn = h.get("horse_no")
                    nm = h.get("horse_name")
                    if hn is not None and nm:
                        out.setdefault((rn, int(hn)), str(nm).strip())
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return out


def _format_bet_selections(bet: dict) -> str:
    """Render bet selections with horse names — e.g. ``3 PRINCE · 4 NEBRASKAN``.

    Falls back to bare numbers when no name lookup is available.
    """
    md = (bet.get("meeting_date") or "").replace("-", "")
    rn_raw = bet.get("race_number")
    try:
        rn = int(rn_raw) if rn_raw is not None else None
    except (TypeError, ValueError):
        rn = None
    sels = bet.get("selections") or []
    banker = bet.get("banker")

    lookup = _horse_name_lookup(md) if md else {}

    def _fmt(n):
        try:
            n_int = int(n)
        except (TypeError, ValueError):
            return str(n)
        nm = lookup.get((rn, n_int)) if rn is not None else None
        return f"#{n_int} {nm}" if nm else f"#{n_int}"

    parts = []
    if banker is not None:
        parts.append(f"BANKER {_fmt(banker)}")
    # Quartet Multi-Banker: show one line per finishing position.
    legs = bet.get("legs") or []
    if bet.get("bet_type") == "QTT_MB" and len(legs) == 4:
        for i, lg in enumerate(legs, start=1):
            parts.append(f"P{i} " + " · ".join(_fmt(x) for x in lg))
    elif sels:
        parts.append("SEL " + " · ".join(_fmt(x) for x in sels))
    return "  |  ".join(parts) if parts else "(no selections)"


# ══════════════════════════════════════════════════════════════════════════════
# Form Guide — data helpers
# ══════════════════════════════════════════════════════════════════════════════

CACHE_DIR = BASE / "cache"
FORM_COLS = [
    "horse_name", "race_date", "race_number", "race_track", "race_course",
    "going", "race_class", "jockey", "trainer", "rating", "draw", "running_positions",
    "place", "lbw", "finish_time_seconds", "distance", "actual_weight",
    "sire", "dam_sire", "current_rating", "last_rating", "declared_weight",
    "sectiontimes",
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


# NOTE: cached as a *resource* (not data) so that the many
# ``st.cache_data.clear()`` calls scattered across the app do NOT evict
# this expensive xlsx -> DataFrame load. The form DB only changes when
# the post-race pipeline rebuilds it; the two call sites that trigger
# that rebuild call ``_load_form_db.clear()`` explicitly.
@st.cache_resource(ttl=120, show_spinner=False)
def _load_form_db() -> pd.DataFrame:
    """Load hkjc_results_updated.xlsx for form guide lookups.

    Reads from a parquet sidecar (``cache/form_db.parquet``) when it
    exists and is at least as fresh as the canonical xlsx — parquet
    deserialises ~100x faster than the multi-MB OneDrive xlsx and
    avoids the PermissionError / temp-copy hack on Windows. The
    sidecar is regenerated automatically on cache miss whenever the
    xlsx is newer (or the parquet is missing / corrupt).
    """
    db_file = BASE / "hkjc_results_updated.xlsx"
    if not db_file.exists():
        return pd.DataFrame()

    # v4.7: fastest path — sqlite mirror (hkjc.db). Auto-rebuilt by
    # db_utils.append_results_to_db on every results scrape, so it tracks
    # the xlsx automatically. ~0.8 s vs 27 s xlsx, no parquet rebuild step.
    try:
        from db_utils import SQLITE_FILE, read_sqlite
        sqlite_path = Path(SQLITE_FILE)
        if sqlite_path.exists():
            # On Streamlit Cloud prefer sqlite unconditionally. Git checkout
            # mtimes are not a reliable freshness signal there, and falling
            # through to the xlsx->parquet branch has triggered hard crashes
            # under Python 3.14 / pandas 3 / pyarrow.
            if _is_streamlit_cloud():
                cols = ", ".join(f'"{c}"' for c in FORM_COLS)
                df = read_sqlite(f"SELECT {cols} FROM results")
                df["race_date"] = pd.to_datetime(df["race_date"]).dt.date
                df["place_num"] = pd.to_numeric(df["place"], errors="coerce")
                df["horse_name_upper"] = df["horse_name"].str.upper().str.strip()
                return df

            try:
                xlsx_mtime = db_file.stat().st_mtime
            except OSError:
                xlsx_mtime = 0.0
            try:
                sql_mtime = sqlite_path.stat().st_mtime
            except OSError:
                sql_mtime = 0.0
            # Only trust sqlite if it's at least as fresh as the xlsx
            # (otherwise fall through to parquet/xlsx path which will
            # see the new rows from the xlsx).
            if sql_mtime >= xlsx_mtime:
                cols = ", ".join(f'"{c}"' for c in FORM_COLS)
                df = read_sqlite(f"SELECT {cols} FROM results")
                df["race_date"] = pd.to_datetime(df["race_date"]).dt.date
                df["place_num"] = pd.to_numeric(df["place"], errors="coerce")
                df["horse_name_upper"] = df["horse_name"].str.upper().str.strip()
                return df
    except Exception as exc:
        if _is_streamlit_cloud():
            raise RuntimeError(f"sqlite fast path failed in _load_form_db: {exc}") from exc
        pass

    parquet_file = CACHE_DIR / "form_db.parquet"
    try:
        xlsx_mtime = db_file.stat().st_mtime
    except OSError:
        xlsx_mtime = 0.0

    # Fast path: parquet sidecar is fresh
    if parquet_file.exists():
        try:
            pq_mtime = parquet_file.stat().st_mtime
        except OSError:
            pq_mtime = 0.0
        if pq_mtime >= xlsx_mtime:
            try:
                df = pd.read_parquet(parquet_file)
                if "race_date" in df.columns:
                    df["race_date"] = pd.to_datetime(df["race_date"]).dt.date
                return df
            except Exception:
                try:
                    parquet_file.unlink()
                except OSError:
                    pass

    # Slow path: read xlsx, normalise, write parquet sidecar for next time.
    df = _safe_read_excel(db_file)
    keep = [c for c in FORM_COLS if c in df.columns]
    df = df[keep].copy()
    df["race_date"] = pd.to_datetime(df["race_date"]).dt.date
    df["place_num"] = pd.to_numeric(df["place"], errors="coerce")
    df["horse_name_upper"] = df["horse_name"].str.upper().str.strip()

    # On Streamlit Cloud, stop here. Writing the parquet sidecar has been
    # observed to segfault the process under Python 3.14 / pandas 3.0 when
    # mixed object/string columns are coerced for Arrow serialisation.
    if _is_streamlit_cloud():
        return df

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Coerce mixed-type object columns (e.g. ``place`` = int + 'DH' / 'WV')
        # to plain strings so pyarrow can serialise without ArrowTypeError.
        # Preserve NaN cells (don't let astype(str) turn them into 'nan').
        df_pq = df.copy()
        for col in df_pq.select_dtypes(include=["object", "string"]).columns:
            mask_na = df_pq[col].isna()
            df_pq[col] = df_pq[col].astype(str)
            df_pq.loc[mask_na, col] = None
        df_pq.to_parquet(parquet_file, index=False)
    except Exception:
        # pyarrow / fastparquet missing or write failed — next call falls
        # back to xlsx path; non-fatal.
        pass
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


def _fmt_top5_html(top5: list[tuple], current_horse: str,
                   top5_next: list | None = None) -> str:
    """Top-5 finishers as HTML with bold horse names; current horse in amber.

    If ``top5_next`` is supplied (aligned with ``top5``), each co-runner gets
    a small badge showing where they finished in their NEXT race after this
    run. Badges are colour-coded: 1st green, 2nd–3rd amber, 4th cyan, others
    muted. Entries with no next-run yet show '—'.
    """
    parts = []
    h_up = current_horse.strip().upper()
    next_list = top5_next or []
    for i, (place, name) in enumerate(top5):
        name_str = str(name)
        is_self = name_str.strip().upper() == h_up
        # Build next-run badge
        nxt_html = ""
        if i < len(next_list):
            nxt = next_list[i] or {}
            np = nxt.get("place")
            if np is not None and not is_self:
                try:
                    np_int = int(np)
                    if np_int == 1:
                        col = "#22c55e"
                    elif np_int <= 3:
                        col = "#f59e0b"
                    elif np_int == 4:
                        col = "#22d3ee"
                    else:
                        col = "#9ca3af"
                    nxt_html = (
                        f'<span title="Next race finish: {np_int} on {nxt.get("date","?")}" '
                        f'style="margin-left:3px;padding:0 4px;border-radius:4px;'
                        f'background:rgba(255,255,255,0.06);color:{col};'
                        f'font-size:0.78em;font-weight:700">→{np_int}</span>'
                    )
                except (ValueError, TypeError):
                    pass
        if is_self:
            parts.append(
                f'<span class="t5-entry"><strong class="t5-self">{place}. {name_str}</strong>{nxt_html}</span>'
            )
        else:
            parts.append(f'<span class="t5-entry">{place}. <strong>{name_str}</strong>{nxt_html}</span>')
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


def _load_vet_full_lookup(date_compact: str) -> dict:
    """Full vet records per runner: {race_number: {horse_no(int): {...}}}.

    Unlike ``_load_vet_lookup`` (which keeps only the summary flag), this keeps
    the per-horse ``records`` list (each with date / details / flag / days_ago)
    so we can surface a vet record that landed on a horse's *previous immediate
    race* on the race-day highlights strip.
    """
    vj = REPORTS / f"vet_report_{date_compact}.json"
    if not vj.exists():
        return {}
    try:
        with open(vj, "r", encoding="utf-8") as f:
            vd = json.load(f)
    except Exception:
        return {}
    out: dict = {}
    for rd in vd.get("races", []):
        rn = rd.get("race_number")
        m: dict = {}
        for h in rd.get("horses", []) or []:
            try:
                no = int(h.get("horse_no"))
            except (TypeError, ValueError):
                continue
            m[no] = {
                "horse_name": h.get("horse_name", ""),
                "max_flag": h.get("max_flag", ""),
                "records": h.get("records", []) or [],
            }
        out[rn] = m
    return out


def _render_raceday_highlights(race: dict, date_str: str,
                               fg_cache: dict | None,
                               vet_full: dict) -> None:
    """Per-race highlights strip for the Model Analysis page.

    Surfaces two things the user asked to be flagged front-and-centre:
      • horses that **changed stables** since their last start (current
        racecard trainer ≠ trainer in the most recent cached run), and
      • horses that carry a **vet record / injury** dated on or after their
        **previous immediate race** (from this meeting's vet report).
    """
    rn = race.get("race_number")
    fg_race = None
    if fg_cache:
        fg_race = next((r for r in fg_cache.get("races", [])
                        if r.get("race_number") == rn), None)
    fg_horses = (fg_race or {}).get("horses", []) or []
    vet_race = vet_full.get(rn, {}) if vet_full else {}

    stable_changes = []   # (no, name, prev_trainer, cur_trainer)
    vet_prev = []         # (no, name, flag, details, date, days_ago)

    for h in fg_horses:
        cur_tr = str(h.get("trainer", "") or "").strip()
        runs = h.get("runs", []) or []
        prev_date = None
        if runs:
            last = runs[-1]
            prev_tr = str(last.get("trainer", "") or "").strip()
            prev_date = str(last.get("date", "") or "")
            if (cur_tr and prev_tr and cur_tr != "?" and prev_tr != "?"
                    and cur_tr.upper() != prev_tr.upper()):
                stable_changes.append(
                    (h.get("horse_no", "?"), h.get("horse_name", "?"),
                     prev_tr, cur_tr))
        # Vet record on / since the previous immediate race
        try:
            no = int(h.get("horse_no"))
        except (TypeError, ValueError):
            no = None
        vinfo = vet_race.get(no) if no is not None else None
        if vinfo and prev_date:
            relevant = [
                rec for rec in vinfo.get("records", [])
                if rec.get("flag") in ("RED", "AMBER")
                and str(rec.get("date", "")) >= prev_date
            ]
            if relevant:
                rec = relevant[0]
                vet_prev.append(
                    (h.get("horse_no", "?"), h.get("horse_name", "?"),
                     vinfo.get("max_flag") or rec.get("flag"),
                     str(rec.get("details", "") or ""),
                     str(rec.get("date", "") or ""),
                     rec.get("days_ago")))

    if not stable_changes and not vet_prev:
        return

    chips = []
    for no, name, prev_tr, cur_tr in stable_changes:
        chips.append(
            f'<span title="Stable change: {prev_tr} → {cur_tr}" '
            f'style="display:inline-block;margin:2px 6px 2px 0;padding:2px 9px;'
            f'border-radius:11px;background:rgba(212,55,0,0.14);'
            f'border:1px solid rgba(212,55,0,0.5);color:#ff7a3d;'
            f'font-size:0.82em;font-weight:700">'
            f'\u2691 #{no} {str(name).title()} '
            f'<span style="opacity:0.8;font-weight:600">{prev_tr}→{cur_tr}</span>'
            f'</span>')
    for no, name, flag, details, vdate, days_ago in vet_prev:
        col = "#ef4444" if flag == "RED" else "#f59e0b"
        bg = "rgba(239,68,68,0.14)" if flag == "RED" else "rgba(245,158,11,0.14)"
        when = (f"{days_ago}d ago" if isinstance(days_ago, (int, float))
                else vdate)
        det = (details[:48] + "…") if len(details) > 49 else details
        chips.append(
            f'<span title="{details} ({vdate})" '
            f'style="display:inline-block;margin:2px 6px 2px 0;padding:2px 9px;'
            f'border-radius:11px;background:{bg};'
            f'border:1px solid {col}88;color:{col};'
            f'font-size:0.82em;font-weight:700">'
            f'\U0001fa7a #{no} {str(name).title()} · {flag} · {when} '
            f'<span style="opacity:0.8;font-weight:600">{det}</span>'
            f'</span>')

    bits = []
    if stable_changes:
        bits.append(f"{len(stable_changes)} stable change"
                    f"{'s' if len(stable_changes) != 1 else ''}")
    if vet_prev:
        bits.append(f"{len(vet_prev)} vet/injury last start")
    st.markdown(
        f'<div style="margin:2px 0 8px 0;padding:7px 10px;border-radius:8px;'
        f'background:rgba(255,255,255,0.03);border:1px solid '
        f'rgba(255,255,255,0.08)">'
        f'<span style="font-weight:800;color:#a3b3c7;font-size:0.82em;'
        f'letter-spacing:0.5px">\u2691 RACE HIGHLIGHTS</span> '
        f'<span style="opacity:0.6;font-size:0.78em">· {" · ".join(bits)}</span>'
        f'<div style="margin-top:5px">{"".join(chips)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


@st.cache_data(show_spinner=False)
def _load_fuse_report(date_compact: str, mtime: float = 0.0) -> dict:
    """Load the FUSE report, keyed by file mtime so new reports invalidate."""
    try:
        p = REPORTS / f"race_day_report_{date_compact}_FUSE.json"
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fuse_report_mtime(date_compact: str) -> float:
    p = REPORTS / f"race_day_report_{date_compact}_FUSE.json"
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _run_fuse_refresh(date_iso: str, going_turf: str, going_awt: str
                      ) -> tuple[bool, list[tuple[str, int, str]]]:
    """Run only the FUSE report refresh, then rebuild mutual picks."""
    dc = date_iso.replace("-", "")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    outputs: list[tuple[str, int, str]] = []
    racecard_xlsx = BASE / "racecards" / f"racecard_{dc}.xlsx"
    if not racecard_xlsx.exists():
        return False, [("FUSE", 1, f"Missing racecard: {racecard_xlsx.name}")]

    try:
        res_fu = subprocess.run(
            [PYTHON, str(BASE / "fuse_raceday.py"),
             "--date", dc, "--refresh-odds-only", "--going", going_turf or "Good",
             "--going-awt", going_awt or "Good"],
            env=env, cwd=str(BASE), capture_output=True, text=True,
            encoding="utf-8", timeout=600,
        )
        outputs.append(("FUSE", res_fu.returncode,
                        (res_fu.stdout or "") + ("\n" + res_fu.stderr
                                             if res_fu.stderr else "")))
    except subprocess.TimeoutExpired as exc:
        return False, [("FUSE", 124, f"Timed out after {exc.timeout}s")]
    except OSError as exc:
        return False, [("FUSE", 1, f"{type(exc).__name__}: {exc}")]

    fuse_json = REPORTS / f"race_day_report_{dc}_FUSE.json"
    fuse_ok = res_fu.returncode == 0 and fuse_json.exists()
    if not fuse_ok:
        return False, outputs

    try:
        res_mut = subprocess.run(
            [PYTHON, str(BASE / "build_mutual_picks.py"), "--date", dc],
            env=env, cwd=str(BASE), capture_output=True, text=True,
            encoding="utf-8", timeout=60,
        )
        outputs.append(("Mutual picks", res_mut.returncode,
                        (res_mut.stdout or "") + ("\n" + res_mut.stderr
                                             if res_mut.stderr else "")))
    except subprocess.TimeoutExpired as exc:
        outputs.append(("Mutual picks", 124, f"Timed out after {exc.timeout}s"))
    except OSError as exc:
        outputs.append(("Mutual picks", 1, f"{type(exc).__name__}: {exc}"))

    if _is_streamlit_cloud():
        try:
            pushed, _missing, errors = _gh_persist_pipeline_outputs(date_iso, "v4.4")
            msg = f"pushed {pushed} file(s)" if pushed else "no files pushed"
            if errors:
                msg += "; " + "; ".join(errors[:3])
            outputs.append(("GitHub sync", 0 if not errors else 1, msg))
        except Exception as exc:
            outputs.append(("GitHub sync", 1, f"{type(exc).__name__}: {exc}"))
    return True, outputs


def _count_live_odds_snapshots(date_compact: str, venue_code: str) -> int:
    snap_dir = BASE / "cache" / "live_odds" / date_compact
    if not snap_dir.exists():
        return 0
    return len(list(snap_dir.glob(f"{venue_code}_R*.json")))


def _render_fuse_race_segment(race: dict, date_compact: str,
                              venue_code: str, meeting_info: dict) -> None:
    """Selected-race FUSE table plus refresh actions for Race Day Insight."""
    rn = race.get("race_number")
    try:
        rn_int = int(rn)
    except (TypeError, ValueError):
        rn_int = 0
    date_iso = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"
    turf_default = str(st.session_state.get("going_turf") or
                       race.get("going") or "Good")
    awt_default = str(st.session_state.get("going_awt") or "Good")
    n_races = int(meeting_info.get("n_races") or 0)
    if not n_races:
        n_races = max(1, rn_int)

    st.markdown("#### FUSE table")
    st.caption(
        "FUSE refresh is intentionally separate from core ET/SARR analysis. "
        "Use odds + FUSE when fresh market prices are available; use FUSE-only "
        "when the odds snapshots are already current."
    )
    ctl1, ctl2, ctl3 = st.columns([1, 1.25, 1.25])
    with ctl1:
        st.metric("Live odds", _count_live_odds_snapshots(date_compact, venue_code)
                  if venue_code else 0)
    with ctl2:
        if st.button("Refresh FUSE", key=f"fuse_refresh_{date_compact}_{rn_int}",
                     width='stretch'):
            with st.spinner("Refreshing FUSE and mutual picks..."):
                ok, outputs = _run_fuse_refresh(date_iso, turf_default, awt_default)
            if ok:
                st.success("FUSE refreshed.")
            else:
                st.error("FUSE refresh failed.")
            with st.expander("FUSE refresh log", expanded=not ok):
                for label, rc, log in outputs:
                    st.markdown(f"**{label}** exit `{rc}`")
                    if log:
                        st.code(log[-3000:])
            try:
                _load_fuse_report.clear()
            except Exception:
                pass
            if ok:
                st.rerun()
    with ctl3:
        disabled = not bool(venue_code)
        if st.button("Scrape odds + Refresh FUSE",
                     key=f"fuse_odds_refresh_{date_compact}_{rn_int}",
                     width='stretch', disabled=disabled,
                     help="Sequential: scrape live odds for the full meeting, then rebuild FUSE."):
            races_arg = f"1-{n_races}"
            before = _count_live_odds_snapshots(date_compact, venue_code)
            with st.spinner(f"Scraping live odds {venue_code} R{races_arg}..."):
                rc, log = _run_live_odds_scraper(date_iso, venue_code, races_arg)
            after = _count_live_odds_snapshots(date_compact, venue_code)
            if rc == 0:
                new_n = max(0, after - before)
                st.success(f"Odds scrape complete ({new_n} new snapshot(s)).")
                if _is_streamlit_cloud():
                    pushed, _missing, errs = _gh_persist_live_odds_snapshots(
                        date_iso, venue_code)
                    if pushed:
                        st.info(f"Synced {pushed} live-odds snapshot(s) to GitHub.")
                    if errs:
                        st.warning("Live-odds sync had errors: " + "; ".join(errs[:3]))
                with st.spinner("Refreshing FUSE with latest odds..."):
                    ok, outputs = _run_fuse_refresh(date_iso, turf_default, awt_default)
                if ok:
                    st.success("FUSE refreshed with latest available odds.")
                else:
                    st.error("Odds scraped, but FUSE refresh failed.")
                with st.expander("Odds + FUSE log", expanded=not ok):
                    if log.strip():
                        st.markdown("**Live odds scraper**")
                        st.code(log[-3000:])
                    for label, rc2, out in outputs:
                        st.markdown(f"**{label}** exit `{rc2}`")
                        if out:
                            st.code(out[-3000:])
                try:
                    _load_fuse_report.clear()
                except Exception:
                    pass
                if ok:
                    st.rerun()
            else:
                st.error(f"Live odds scrape failed (exit {rc}); FUSE was not refreshed.")
                if log.strip():
                    with st.expander("Live odds scraper log", expanded=True):
                        st.code(log[-4000:])

    fuse_rep = _load_fuse_report(date_compact, _fuse_report_mtime(date_compact))
    if not fuse_rep:
        st.info("No FUSE report yet. Click **Refresh FUSE** after ET/SARR have a racecard.")
        return
    fr = next((r for r in fuse_rep.get("races", [])
               if int(r.get("race_number") or 0) == rn_int), None)
    if not fr:
        st.warning(f"FUSE report exists, but R{rn_int} is not in it.")
        return

    rows = []
    for pick in fr.get("picks", []) or []:
        edge = pick.get("edge")
        rows.append({
            "Rank": pick.get("rank"),
            "No": pick.get("horse_no"),
            "Horse": pick.get("horse_name"),
            "Win %": round((pick.get("p_win") or 0) * 100, 1),
            "Top2 %": round((pick.get("p_top2") or 0) * 100, 1),
            "Top3 %": round((pick.get("p_top3") or 0) * 100, 1),
            "Win Odds": pick.get("win_odds"),
            "Edge pp": round(edge * 100, 1) if edge is not None else None,
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width='stretch')

    qps = fr.get("quinella_pairs") or []
    if qps:
        qtxt = " · ".join(
            f"{q.get('a')}-{q.get('b')} {q.get('p', 0) * 100:.1f}% "
            f"(fair {q.get('fair_odds', 0):.0f})"
            for q in qps[:4]
        )
        st.caption(f"Quinella: {qtxt}")
    mode = "live-odds anchored" if fr.get("odds_anchored") else "fundamentals only"
    st.caption(
        f"{mode} · odds mode {fuse_rep.get('odds_mode', '?')} · trained on "
        f"{fuse_rep.get('n_train', 0):,} runner-starts · generated "
        f"{fuse_rep.get('generated_at_hkt', '?')} HKT"
    )


@st.cache_data(show_spinner=False)
def _load_mutual_picks(date_compact: str) -> dict:
    """Load model-agreed (ET ∩ SARR) picks for a meeting, keyed by race_no.

    Source: ``reports/mutual_<date>.json`` (built by ``build_mutual_picks.py``).
    Each race carries ``et_picks`` / ``sarr_picks`` (top-3 label strings) and
    ``mutual_horses`` (the intersection, with per-engine ranks).
    """
    try:
        p = REPORTS / f"mutual_{date_compact}.json"
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return {r.get("race_number"): r for r in data.get("races", [])}
    except Exception:
        return {}


def _render_model_confluence(race: dict, date_compact: str):
    """Per-race cross-engine signal strip for the Model Analysis page.

    Surfaces the three signals the user previously had to open the Race Day
    Insight tab for:
      • Ensemble picks — ET top-3 and SARR top-3 side by side.
      • Model-agreed   — horses both engines rank top-3 (mutual), ★-flagged.
      • Rating signals — horse_cycle flags (PRIMED / DROPPER / WATCH / FADE)
                         for any runner in this race.
    Display-only; nothing here feeds the model.
    """
    rn = race.get("race_number")
    if rn is None:
        return
    mrec = (_load_mutual_picks(date_compact) or {}).get(rn) or {}
    et_picks = mrec.get("et_picks") or []
    sarr_picks = mrec.get("sarr_picks") or []
    fuse_picks = mrec.get("fuse_picks") or []
    mutual_horses = mrec.get("mutual_horses") or []
    triple_horses = mrec.get("triple_horses") or []

    # Rating-cycle flags for runners in this race.
    cyc_rows = []
    place_rows = []
    try:
        _conf = _mb_confluence_maps(date_compact) or {}
        cmap = _conf.get("cycle", {})
        pmap = _conf.get("place", {})
        for pick in (race.get("picks") or []):
            nm = str(pick.get("horse_name", "")).strip().upper()
            flag = cmap.get((rn, nm))
            if flag in ("PRIMED", "EARLY_DROPPER", "WATCH", "FADE_OVERRATED"):
                cyc_rows.append((pick.get("horse_no", "?"),
                                 pick.get("horse_name", ""), flag))
            _pf = pmap.get((rn, nm))
            if _pf and _pf.get("form") in ("HOT", "WARM"):
                place_rows.append((pick.get("horse_no", "?"),
                                   pick.get("horse_name", ""), _pf))
    except Exception:
        pass

    if not (et_picks or sarr_picks or fuse_picks or mutual_horses
            or cyc_rows or place_rows):
        return

    flag_disp = {
        "PRIMED": ("#22c55e", "🟢 PRIMED"),
        "EARLY_DROPPER": ("#38bdf8", "🫒 DROPPER"),
        "WATCH": ("#f59e0b", "🟡 WATCH"),
        "FADE_OVERRATED": ("#ef4444", "🔴 FADE"),
    }

    # Agreed (mutual) horse names for ★ highlighting in the pick lists.
    agreed = {str(m.get("horse_name", "")).strip().upper()
              for m in mutual_horses}
    tripled = {str(m.get("horse_name", "")).strip().upper()
               for m in triple_horses}

    def _pick_chip(label: str) -> str:
        nm = (label.split(" ", 1)[1].strip().upper()
              if " " in label else label.upper())
        star = '<span style="color:#fbbf24">★</span> ' if nm in agreed else ""
        return (f'<span style="display:inline-block;margin:2px 5px 2px 0;'
                f'padding:2px 8px;border-radius:11px;'
                f'background:rgba(255,255,255,0.05);'
                f'border:1px solid rgba(255,255,255,0.12);'
                f'font-size:0.82em">{star}{label}</span>')

    cols_html = []
    if et_picks:
        cols_html.append(
            '<div style="display:inline-block;vertical-align:top;'
            'margin-right:18px">'
            '<div style="font-size:0.74em;font-weight:700;color:#60a5fa;'
            'letter-spacing:0.5px;margin-bottom:3px">ET TOP 3</div>'
            + "".join(_pick_chip(p) for p in et_picks) + '</div>')
    if sarr_picks:
        cols_html.append(
            '<div style="display:inline-block;vertical-align:top;'
            'margin-right:18px">'
            '<div style="font-size:0.74em;font-weight:700;color:#c084fc;'
            'letter-spacing:0.5px;margin-bottom:3px">SARR TOP 3</div>'
            + "".join(_pick_chip(p) for p in sarr_picks) + '</div>')
    if fuse_picks:
        cols_html.append(
            '<div style="display:inline-block;vertical-align:top;'
            'margin-right:18px">'
            '<div style="font-size:0.74em;font-weight:700;color:#34d399;'
            'letter-spacing:0.5px;margin-bottom:3px">FUSE TOP 3</div>'
            + "".join(_pick_chip(p) for p in fuse_picks) + '</div>')

    agree_html = ""
    if mutual_horses:
        chips = []
        for m in mutual_horses:
            nm_u = str(m.get("horse_name", "")).strip().upper()
            is3 = nm_u in tripled
            fr = m.get("fuse_rank")
            rk_bits = f'ET#{m.get("et_rank","?")}·SARR#{m.get("sarr_rank","?")}'
            if fr:
                rk_bits += f'·FUSE#{fr}'
            col_bg = "rgba(52,211,153,0.16)" if is3 else "rgba(251,191,36,0.12)"
            col_bd = "rgba(52,211,153,0.65)" if is3 else "rgba(251,191,36,0.5)"
            col_fg = "#34d399" if is3 else "#fbbf24"
            star = "★★★" if is3 else "★"
            chips.append(
                f'<span style="display:inline-block;margin:2px 5px 2px 0;'
                f'padding:2px 9px;border-radius:11px;'
                f'background:{col_bg};'
                f'border:1px solid {col_bd};color:{col_fg};'
                f'font-size:0.82em;font-weight:700">'
                f'{star} #{m.get("horse_no","?")} {m.get("horse_name","")} '
                f'<span style="opacity:0.7;font-weight:600">'
                f'{rk_bits}</span>'
                f'</span>')
        agree_html = (
            '<div style="margin-top:6px">'
            '<span style="font-size:0.74em;font-weight:700;color:#fbbf24;'
            'letter-spacing:0.5px;margin-right:6px" '
            'title="★ = ET∩SARR top-3 · ★★★ = all three engines agree">'
            'AGREED</span>'
            + "".join(chips) + '</div>')

    cyc_html = ""
    if cyc_rows:
        chips = []
        for no, nm, flag in cyc_rows:
            col, lbl = flag_disp.get(flag, ("#9ca3af", flag))
            chips.append(
                f'<span style="display:inline-block;margin:2px 5px 2px 0;'
                f'padding:2px 9px;border-radius:11px;'
                f'background:{col}22;border:1px solid {col}88;color:{col};'
                f'font-size:0.82em;font-weight:700">'
                f'{lbl} · #{no} {nm}</span>')
        cyc_html = (
            '<div style="margin-top:6px">'
            '<span style="font-size:0.74em;font-weight:700;color:#a3b3c7;'
            'letter-spacing:0.5px;margin-right:6px">RATING CYCLE</span>'
            + "".join(chips) + '</div>')

    place_html = ""
    if place_rows:
        chips = []
        for no, nm, pf in place_rows:
            hot = pf.get("form") == "HOT"
            col = "#22c55e" if hot else "#84cc16"
            lbl = "🔥 HOT" if hot else "📈 WARM"
            t3 = pf.get("top3")
            st_ = pf.get("starts")
            frame = (f' <span style="opacity:0.7;font-weight:600">'
                     f'{t3}/{st_} top-3</span>'
                     if t3 is not None and st_ else "")
            chips.append(
                f'<span style="display:inline-block;margin:2px 5px 2px 0;'
                f'padding:2px 9px;border-radius:11px;'
                f'background:{col}22;border:1px solid {col}88;color:{col};'
                f'font-size:0.82em;font-weight:700">'
                f'{lbl} · #{no} {nm}{frame}</span>')
        place_html = (
            '<div style="margin-top:6px">'
            '<span style="font-size:0.74em;font-weight:700;color:#a3b3c7;'
            'letter-spacing:0.5px;margin-right:6px" '
            'title="Recent competitive places (not just wins) — contextual '
            'place-form overlay from the rating cycle">PLACE FORM</span>'
            + "".join(chips) + '</div>')

    st.markdown(
        '<div style="margin:2px 0 10px 0;padding:8px 11px;border-radius:8px;'
        'background:rgba(96,165,250,0.05);'
        'border:1px solid rgba(96,165,250,0.18)">'
        '<span style="font-weight:800;color:#9cc0ff;font-size:0.82em;'
        'letter-spacing:0.5px">⚖️ CROSS-ENGINE CONFLUENCE</span>'
        '<div style="margin-top:6px">' + "".join(cols_html) + '</div>'
        + agree_html + cyc_html + place_html +
        '</div>',
        unsafe_allow_html=True,
    )


def render_speed_map(race: dict):
    """Render speed map HTML table above the analysis table."""
    smap = race.get("speed_map")
    if not smap or not smap.get("grid"):
        return

    n_cols = smap["n_cols"]
    n_rows = smap["n_rows"]

    dist = race["distance"]
    race_course = race.get("race_course", "")
    is_straight = dist == 1000 and str(race_course).upper() in ("ST", "SHA TIN")

    # ── Render-time placement (visual-only) ───────────────────────────
    # The upstream grid packing assigns the lateral row by draw-RANK *within*
    # each pace column and can shuffle horses between columns on collision,
    # which produces two well-known artefacts: inside-drawn horses (gate 1-4)
    # shown several lanes off the rail, and front-runners drawn into the back
    # columns. Here we re-derive cell positions for DISPLAY ONLY from the
    # per-horse signals the grid already carries:
    #   • column (front ⇄ back) = early-speed-z band — lowest ESZ (most early
    #     speed) sits at FRONT (rightmost column), highest ESZ at the BACK.
    #   • row    (rail ⇄ wide)  = ABSOLUTE draw band — lowest draw on the RAIL
    #     (row 1), highest draw on WIDE; reversed on the ST 1000m straight.
    # Per-horse advantages, beneficiary flags and colours are untouched.
    horses = list(smap["grid"])
    field = len(horses)

    def _esz_val(h):
        try:
            return float(h.get("esz"))
        except (TypeError, ValueError):
            return 0.0

    def _draw_val(h):
        try:
            return int(h.get("draw"))
        except (TypeError, ValueError):
            return 99

    def _draw_key(h):
        # HV conceded-weight horses settle on the rail regardless of gate.
        conceded = "conceded" in (h.get("notes") or "").lower()
        d = _draw_val(h)
        if is_straight:
            return (0 if conceded else 1, -d)
        return (0 if conceded else 1, d)

    # Column band by early speed (front-to-back), spread across all columns.
    col_of: dict[int, int] = {}
    for i, h in enumerate(sorted(horses, key=_esz_val)):
        band = (i * n_cols) // field if field else 0          # 0 = fastest
        col_of[id(h)] = max(1, min(n_cols, n_cols - band))    # fastest → FRONT

    # Row band by absolute draw (rail-to-wide), spread across all rows.
    row_of: dict[int, int] = {}
    for i, h in enumerate(sorted(horses, key=_draw_key)):
        band = (i * n_rows) // field if field else 0          # 0 = innermost
        row_of[id(h)] = max(1, min(n_rows, band + 1))         # innermost → RAIL

    # Place into the grid. Within each pace column lay the horses out rail→wide
    # in DRAW order (lowest draw nearest the rail), honouring each runner's
    # global draw band wherever the column has room. This keeps inside-drawn
    # runners off the wide lanes, gives every horse a distinct cell, and never
    # reorders the front/back columns.
    grid = {}
    cols_horses: dict[int, list[dict]] = {}
    for h in horses:
        cols_horses.setdefault(col_of[id(h)], []).append(h)
    for col, chs in cols_horses.items():
        chs.sort(key=_draw_key)                       # rail-most (+ conceded) first
        k = len(chs)
        lo = 1                                         # next free row from the rail
        for i, h in enumerate(chs):
            remaining = k - i - 1
            upper = max(lo, n_rows - remaining)        # leave room for the rest
            row = min(n_rows, max(lo, min(row_of[id(h)], upper)))
            grid[(col, row)] = h
            lo = row + 1
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
        parts = []
        for b in bens:
            name = b.get("horse_name", "")
            reason = b.get("reason", "") or "favourable position"
            style = b.get("style", "")
            bonus_s = b.get("pace_style_bonus_s", 0.0)
            bonus_html = ""
            if bonus_s:
                col = "#1D9E75" if bonus_s < 0 else "#C0392B"
                bonus_html = (
                    f' <span style="color:{col};font-weight:600;font-size:0.86em">'
                    f'[pace×style {bonus_s:+.2f}s]</span>'
                )
            style_html = (
                f' <span style="opacity:0.55;font-size:0.82em">({style})</span>'
                if style else ""
            )
            parts.append(
                f'<span style="color:#1D9E75;font-weight:700;">{name}</span>'
                f'{style_html}{bonus_html}'
                f' <span style="opacity:0.6;font-size:0.88em;">— {reason}</span>'
            )
        html += (f'<div style="font-size:0.82em;margin-top:6px;line-height:1.5">'
                 f'★ Beneficiaries: {" | ".join(parts)}</div>')

    # Speed map legend
    html += ('<div style="font-size:0.76em;margin-top:4px;opacity:0.55;">'
             '<span style="color:#1D9E75;font-weight:700">●</span> Pace beneficiary &nbsp; '
             '<span style="color:#C0392B;font-weight:700">●</span> Disadvantaged by pace &nbsp; '
             '○ Neutral</div>')

    html += '</div>'
    st.markdown(html, unsafe_allow_html=True)

    # ── Empirical pace×style research panel ───────────────────────────────
    _render_pace_research_panel(race)


@st.cache_data(ttl=3600)
def _load_pace_benefit_cache() -> dict:
    """Read cache/pace_style_benefit.json once per hour."""
    path = BASE / "cache" / "pace_style_benefit.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _render_pace_research_panel(race: dict):
    """Render the empirical pace×style benefit lookup for the current
    race's (venue, distance-band) slice, sourced from
    cache/pace_style_benefit.json (built by _analyse_pace_benefit.py)."""
    data = _load_pace_benefit_cache()
    if not data:
        return

    try:
        distance = int(race.get("distance") or 0)
    except (TypeError, ValueError):
        distance = 0

    # Venue from session-stashed meeting_venue (set in page_race_day).
    # race.race_course is the rail config ("A"/"B"/"C"), NOT venue.
    venue_str = str(st.session_state.get("_rd_meeting_venue", "")).upper()
    if "HAPPY VALLEY" in venue_str or venue_str.strip() == "HV":
        venue = "HV"
    elif "SHA TIN" in venue_str or venue_str.strip() == "ST":
        venue = "ST"
    else:
        venue = "ST"  # fallback

    # Distance bands differ by venue:
    #   HV has no 1400-1600 races → 2 bands: Sprint (≤1200) / Route (>1200)
    #   ST has 3 bands: Sprint (≤1200) / Mile (1400-1600) / Route (≥1650)
    if venue == "HV":
        band = "sprint" if distance <= 1200 else "route"
        band_label = "SPRINT (≤1200m)" if band == "sprint" else "MIDDLE+ (>1200m)"
    else:
        if distance <= 1200:
            band, band_label = "sprint", "SPRINT (≤1200m)"
        elif distance <= 1600:
            band, band_label = "mile", "MILE (1400-1600m)"
        else:
            band, band_label = "route", "ROUTE (≥1650m)"

    # Lookup priority: venue_band → band → _default
    key_specific = f"{venue}_{band}"
    tbl = data.get(key_specific) or data.get(band) or data.get("_default") or {}
    if not tbl:
        return

    if key_specific in data:
        scope_label = f"{venue} {band_label}"
    elif band in data:
        scope_label = f"All venues · {band_label}"
    else:
        scope_label = "Overall average"

    styles = ["Leader", "On-Pace", "Midfield", "Closer"]
    groups = ["Slow", "Avg", "Fast"]

    # Current race predicted pace group
    pace_label = str(race.get("pace") or "Normal")
    try:
        from pace_utils import pace_group
        cur_group = pace_group(pace_label)
    except Exception:
        cur_group = "Avg"

    # Build table HTML — seconds adjustment per (group, style)
    def _cell(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return '<td style="padding:4px 8px;text-align:center;opacity:0.4">—</td>'
        if v == 0:
            return '<td style="padding:4px 8px;text-align:center;opacity:0.5">0.00</td>'
        col = "#1D9E75" if v < 0 else "#C0392B"
        return (f'<td style="padding:4px 8px;text-align:center;'
                f'color:{col};font-weight:600">{v:+.2f}s</td>')

    rows_html = ""
    for g in groups:
        rrow = tbl.get(g, {})
        hl = "background:rgba(255,215,0,0.08);" if g == cur_group else ""
        cells = "".join(_cell(rrow.get(s, 0.0)) for s in styles)
        tag = "← predicted pace" if g == cur_group else ""
        rows_html += (
            f'<tr style="{hl}">'
            f'<th style="padding:4px 8px;text-align:left;font-weight:600;">{g}</th>'
            f'{cells}'
            f'<td style="padding:4px 8px;opacity:0.55;font-size:0.8em">{tag}</td>'
            f'</tr>'
        )

    samples = data.get("_samples", {}).get(band) if band in data.get("_samples", {}) else \
              data.get("_samples", {}).get("_all", {})
    sample_note = ""
    if samples:
        tot = sum(samples.get(g, {}).get(s, 0) for g in groups for s in styles)
        sample_note = f" · n={tot:,} runner-rows"

    panel = f"""
<div style="margin:6px 0 12px 0;padding:10px 14px;
            background:rgba(128,128,128,0.05);
            border-left:3px solid #f59e0b;
            border-radius:0 6px 6px 0;font-size:0.85em;">
  <div style="font-weight:700;letter-spacing:0.03em;
              font-size:0.88em;margin-bottom:6px;opacity:0.85;">
    📈 Empirical pace × style benefit — {scope_label}{sample_note}
  </div>
  <div style="opacity:0.6;font-size:0.8em;margin-bottom:6px">
    Seconds adjusted per runner vs baseline (Avg pace).
    <span style="color:#1D9E75;font-weight:600">negative = faster</span>,
    <span style="color:#C0392B;font-weight:600">positive = slower</span>.
    &nbsp;·&nbsp; <b>ET model</b> folds this bonus directly into each horse's
    <i>projected_time</i> (via <code>smap_advantage × 0.10 s</code>).
    &nbsp;·&nbsp; <b>SARR model</b> uses a separate style × venue fit score
    (independent of predicted pace) — so SARR ranking will not always reflect this table.
  </div>
  <table style="border-collapse:collapse;width:100%;font-size:0.88em;">
    <thead>
      <tr style="border-bottom:1px solid rgba(128,128,128,0.3)">
        <th style="padding:4px 8px;text-align:left;opacity:0.7;">Pace</th>
        {''.join(f'<th style="padding:4px 8px;text-align:center;opacity:0.7;">{s}</th>' for s in styles)}
        <th></th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>
"""
    st.markdown(panel, unsafe_allow_html=True)


# Column tooltip text (hover help on the race-card tables). Mirrors the
# bottom-of-page legend so the explanation lives next to the data.
_ET_COL_HELP = {
    "Rk": "Model rank — lower is a stronger pick (1 = best).",
    "No": "Saddle-cloth number.",
    "Fin": "Actual finishing position (post-race). 1 = gold, 2–3 = placed.",
    "Draw": "Barrier draw (gate position).",
    "Win%": "Estimated win probability — ≥20% is strong.",
    "Edge": "Model Win% minus market-implied Win% (live odds, de-overround). "
            "Positive = potential value / overlay.",
    "ESZ": "Early Speed Z — negative = faster early; ≥1.0 very slow out.",
    "Fin Sec": "Projected final sectional (s) — lower = stronger finish.",
    "SSI": "Sectional Speed Index — negative = consistently faster than field.",
    "Eff Resid": "Effective Residual — negative = runs faster than its rating implies.",
    "Trial": "Trial performance (++ outstanding … -- very poor).",
    "Vet": "Vet flag — RED concern, AMB monitor, INF informational.",
    "Flags": "Model flags (↑IMP improving, U unreliable form, …).",
    "Jockey": "Jockey.",
}

_SARR_COL_HELP = {
    "Rk": "SARR rank — lower is stronger (1 = best).",
    "No": "Saddle-cloth number.",
    "Fin": "Actual finishing position (post-race). 1 = gold, 2–3 = placed.",
    "Draw": "Barrier draw (gate position).",
    "Wt": "Carried weight (lbs).",
    "Style": "Predicted running style.",
    "BB": "In your Blackbook.",
    "WPR%": "Win/Place rate — higher = better strike rate.",
    "ESZ": "Early Speed Z — negative = faster early.",
    "SARR": "Composite score — negative = outperforms expectations (better).",
    "FMRP": "Form Residual Performance — negative = form better than rating.",
    "LSA": "Late Speed Advantage — negative = strong finisher.",
    "SSI": "Sectional Speed Index — negative = consistently faster.",
    "Traj": "Trajectory — negative = improving form trend.",
    "Late Std": "Late-section time SD — lower = more consistent (≤0.20 reliable).",
    "Jockey": "Jockey.",
}


# ── Shared glossary (cross-page) ──────────────────────────────────────────
# Single source of truth for metric definitions surfaced as an expander on
# the analysis pages so terminology stays consistent everywhere.
GLOSSARY = {
    "ESZ (Early Speed Z)": "Standardised early-speed rating. Negative = faster "
        "away from the gate; ≥1.0 = very slow to begin. Strongest single signal.",
    "SARR": "Composite speed/ability residual score. Negative = the horse "
        "outperforms expectations (better).",
    "FMRP": "Form Residual Performance — negative = recent form better than the "
        "official rating implies.",
    "LSA (Late Speed Advantage)": "Finishing-speed measure. Negative = strong "
        "closer late in the race.",
    "SSI (Sectional Speed Index)": "Consistency of sectional times. Negative = "
        "reliably faster than the field.",
    "Eff Resid (Effective Residual)": "Negative = the horse runs faster than its "
        "rating would predict.",
    "WPR% (Win/Place Rate)": "Historic strike rate — higher = more consistent.",
    "Edge": "Model win probability minus market-implied probability (de-overround "
        "live odds). Positive = potential value / overlay.",
    "Traj (Trajectory)": "Form trend. Negative = improving.",
    "Win%": "Estimated win probability. ≥20% is a strong pick.",
    "Style": "Predicted running style (Lead / Prominent / Midfield / Closer).",
    "BB": "Horse is in your Blackbook watch-list.",
    "Vet": "Vet flag — RED concern, AMB monitor, INF informational.",
    "Fin": "Actual finishing position after the race. 1 = gold, 2–3 = placed.",
}


def _render_glossary(key: str = "glossary") -> None:
    """Drop-in collapsible glossary of model metrics for any page."""
    with st.expander("ℹ️ Glossary — metric definitions", expanded=False):
        st.markdown(
            "\n".join(f"- **{term}** — {desc}" for term, desc in GLOSSARY.items())
        )


def render_race_card(race: dict, vet_lookup: dict | None = None, show_top: int = 4,
                     bb_lookup: dict | None = None, odds_lookup: dict | None = None,
                     result_lookup: dict | None = None):
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
        _imp = (odds_lookup or {}).get(p["horse_no"])
        _edge = (p["win_prob"] - _imp) if _imp is not None else None
        rows.append({
            "Rk": p["rank"],
            "No": p["horse_no"],
            "Fin": (str(result_lookup.get(p["horse_no"], "")) or "—")
                   if result_lookup else "—",
            "Horse": p["horse_name"],
            "Draw": p.get("draw", "—") or "—",
            "Wt": p.get("weight", "—") or "—",
            "Style": p.get("style", "?"),
            "BB": "BB" if bb_entry else "",
            "Proj (s)": f"{p['projected_time']:.2f}",
            "Win%": f"{p['win_prob']:.0f}%",
            "Edge": f"{_edge:+.0f}%" if _edge is not None else "—",
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
    # Drop the Edge column entirely when no live odds are available, so it
    # isn't a column of dashes.
    if not odds_lookup and "Edge" in df.columns:
        df = df.drop(columns=["Edge"])
    # Drop the Fin (result) column pre-race when no results exist yet.
    if not result_lookup and "Fin" in df.columns:
        df = df.drop(columns=["Fin"])

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

    def style_effresid(val):
        try:
            v = float(str(val).replace("%", ""))
        except (ValueError, TypeError):
            return ""
        if v <= -0.05: return "color: #22c55e; font-weight: bold"
        elif v <= -0.01: return "color: #22c55e"
        elif v >= 0.05: return "color: #ef4444; font-weight: bold"
        elif v >= 0.01: return "color: #ef4444"
        return ""

    def style_edge(val):
        try:
            v = float(str(val).replace("%", ""))
        except (ValueError, TypeError):
            return ""
        if v >= 5: return "color: #22c55e; font-weight: bold"
        elif v > 0: return "color: #22c55e"
        elif v <= -5: return "color: #ef4444"
        return ""

    def style_fin(val):
        s = str(val).strip()
        if s == "1": return "color: #fbbf24; font-weight: bold"
        if s in ("2", "3"): return "color: #22c55e; font-weight: bold"
        try:
            if int(s) <= 6: return "color: #cbd5e1"
        except ValueError:
            return "color: #64748b"
        return "color: #64748b"

    styled = df.style.map(style_ssi, subset=["SSI"]) \
                      .map(style_winprob, subset=["Win%"]) \
                      .map(style_vet, subset=["Vet"]) \
                      .map(style_trial, subset=["Trial"]) \
                      .map(style_esz, subset=["ESZ"]) \
                      .map(style_effresid, subset=["Eff Resid"]) \
                      .format({"ESZ": lambda v: f"{v:+.1f}" if pd.notna(v) else "—",
                               "SSI": lambda v: f"{v:+.2f}" if pd.notna(v) else "—"}) \
                      .set_properties(**{"text-align": "center"}) \
                      .set_properties(subset=["Horse"], **{"text-align": "left", "font-weight": "600"})
    if "Edge" in df.columns:
        styled = styled.map(style_edge, subset=["Edge"])
    if "Fin" in df.columns:
        styled = styled.map(style_fin, subset=["Fin"])

    col_cfg = {c: st.column_config.Column(help=h)
               for c, h in _ET_COL_HELP.items() if c in df.columns}

    # Unique key per (model, race) so Streamlit's column-reorder state
    # for the SARR table doesn't bleed into the ET table and vice versa.
    # Set explicit height so the entire field shows without an inner
    # scrollbar (35px / row + ~38px header).
    _et_h = 38 + 35 * max(len(rows), 1) + 4
    st.dataframe(
        styled, width='stretch', hide_index=True,
        key=f"rd_et_table_{race['race_number']}",
        column_config=col_cfg,
        height=_et_h,
    )

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
                          bb_lookup: dict | None = None,
                          result_lookup: dict | None = None):
    """Render a single race's SARR picks as a terminal-style card."""
    picks = race.get("picks", [])
    if not picks:
        st.warning(f"No SARR projections for Race {race['race_number']}")
        return

    # ── Race header ───────────────────────────────────────────────────────
    cls_str = f"Class {race.get('race_class', '')}" if race.get('race_class') else "Group"
    surface = "AWT" if race.get("is_awt") else "Turf"
    # Pull pace from paired ET race when present (SARR doesn't compute pace itself)
    pace_src = et_race or race
    pace_html = _pace_bar_html(
        pace_src.get("pace", "Neutral"), pace_src.get("pace_score", 0.0)
    )
    st.markdown(
        f'<div class="race-hdr-block">'
        f'<div class="race-hdr-title">R{race["race_number"]} — {race.get("race_name", "")}'
        f' <span style="opacity:0.5;font-size:0.75em">(SARR)</span></div>'
        f'<div class="race-hdr-meta">'
        f'<span>{race.get("distance", "?")}m {surface} ({race.get("race_course", "")})</span>'
        f'<span>{cls_str}</span>'
        f'<span>Pace: {pace_html}</span>'
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
            "Fin": (str(result_lookup.get(p.get("horse_no"), "")) or "—")
                   if result_lookup else "—",
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
    if not result_lookup and "Fin" in df.columns:
        df = df.drop(columns=["Fin"])

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

    def _style_ssi(val):
        try: v = float(val)
        except (ValueError, TypeError): return ""
        if v <= -0.30: return "color: #22c55e; font-weight: bold"
        elif v <= -0.10: return "color: #22c55e"
        elif v >= 0.30: return "color: #ef4444"
        return ""

    def _style_fin(val):
        s = str(val).strip()
        if s == "1": return "color: #fbbf24; font-weight: bold"
        if s in ("2", "3"): return "color: #22c55e; font-weight: bold"
        try:
            if int(s) <= 6: return "color: #cbd5e1"
        except ValueError:
            return "color: #64748b"
        return "color: #64748b"

    styled = df.style.map(_style_sarr, subset=["SARR"]) \
                      .map(_style_fmrp, subset=["FMRP"]) \
                      .map(_style_lsa_esz, subset=["LSA", "ESZ"]) \
                      .map(_style_ssi, subset=["SSI"]) \
                      .map(_style_traj, subset=["Traj"]) \
                      .map(_style_wpr, subset=["WPR%"]) \
                      .map(_style_late_std, subset=["Late Std"]) \
                      .format({"SARR": "{:+.3f}", "FMRP": "{:+.2f}",
                               "LSA": "{:+.2f}", "ESZ": "{:+.2f}",
                               "SSI": "{:+.2f}", "Traj": "{:+.3f}"}) \
                      .set_properties(**{"text-align": "center"}) \
                      .set_properties(subset=["Horse"], **{"text-align": "left", "font-weight": "600"})

    if "Fin" in df.columns:
        styled = styled.map(_style_fin, subset=["Fin"])

    _sarr_cfg = {c: st.column_config.Column(help=h)
                 for c, h in _SARR_COL_HELP.items() if c in df.columns}
    # Unique per-(model, race) key — see render_race_card for rationale.
    # Height tuned so all rows fit without an inner scrollbar.
    _sarr_h = 38 + 35 * max(len(rows), 1) + 4
    st.dataframe(
        styled, width='stretch', hide_index=True,
        key=f"rd_sarr_table_{race['race_number']}",
        column_config=_sarr_cfg,
        height=_sarr_h,
    )
    st.markdown('<hr class="term-divider">', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline runner
# ══════════════════════════════════════════════════════════════════════════════


def run_pipeline(date_str: str, no_cache: bool, going_turf: str, going_awt: str,
                 model: str = "v4.4", skip_scrape: bool = False,
                 run_fuse: bool = False):
    """Run the race-day pipeline as three EXPLICIT, user-visible stages:

        [1/3] Scrape the race card for the meeting
        [2/3] Run SARR model on the freshly-scraped card
        [3/3] Run ET (v4.4) model on the freshly-scraped card

    SARR is run BEFORE ET so that a SARR JSON is available even if the ET
    analysis script later errors out. ET is invoked via run_meeting.py with
    `--skip-scrape --skip-sarr`, which handles vet scraping, form-guide cache,
    and the v4.4 analysis script generation.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    dc = date_str.replace("-", "")
    scraper_script = BASE / "scrape_hkjc_racecard.py"
    sarr_script = BASE / "sarr_raceday.py"
    sarr_json = REPORTS / f"race_day_report_{dc}_SARR.json"
    et_json = REPORTS / f"race_day_report_{dc}_{model}.json"
    racecard_xlsx = BASE / "racecards" / f"racecard_{dc}.xlsx"

    sarr_mtime_before = sarr_json.stat().st_mtime if sarr_json.exists() else 0.0
    et_mtime_before   = et_json.stat().st_mtime   if et_json.exists()   else 0.0
    rc_mtime_before   = racecard_xlsx.stat().st_mtime if racecard_xlsx.exists() else 0.0

    def _run(label: str, cmd: list, timeout: int):
        status = st.empty()
        status.info(f"{label} — running…")
        try:
            res = subprocess.run(
                cmd, env=env, cwd=str(BASE),
                capture_output=True, text=True, encoding="utf-8",
                timeout=timeout,
            )
            return res
        except subprocess.TimeoutExpired as e:
            st.error(f"✗ {label} timed out after {timeout}s")
            return e
        except OSError as e:
            st.error(f"✗ {label} failed to launch: {e}")
            return e

    with st.spinner(f"Running pipeline for {date_str}..."):
        _pbar = st.progress(0, text="[1/3] Scraping race card…")
        # ── [1/3] Scrape race card ──────────────────────────────────
        if skip_scrape:
            st.info(f"[1/3] Scrape SKIPPED (using existing {racecard_xlsx.name})")
        else:
            cmd = [PYTHON, str(scraper_script), "--date", date_str]
            if no_cache:
                cmd.append("--no-cache")
            res = _run("[1/3] Scrape race card", cmd, timeout=300)
            if isinstance(res, subprocess.CompletedProcess):
                rc_mtime_after = racecard_xlsx.stat().st_mtime if racecard_xlsx.exists() else 0.0
                if res.returncode == 0 and racecard_xlsx.exists() and rc_mtime_after > rc_mtime_before:
                    st.success(f"✓ [1/3] Race card scraped: {racecard_xlsx.name}")
                elif res.returncode == 0:
                    st.warning(f"⚠ [1/3] Scraper exited cleanly but {racecard_xlsx.name} "
                               f"was not updated. Proceeding with existing file.")
                else:
                    st.error(f"✗ [1/3] Scrape failed (exit {res.returncode}). "
                             f"Aborting pipeline.")
                    with st.expander("Scrape error output"):
                        st.code((res.stderr or res.stdout or "")[-3000:])
                    return
                with st.expander("Scrape output"):
                    st.code((res.stdout or "")[-2500:])

        # ── [1.5/3] Sync any freshly-scraped results → hkjc.db ──────
        # SARR reads hkjc.db, so sync results_*.json before SARR, not only
        # later before ET/form-guide. The later sync is then a cheap no-op.
        try:
            n_mtg_pre, n_rows_pre = _auto_sync_results_to_db(verbose=False)
            if n_mtg_pre:
                st.info(
                    f"Auto-synced {n_mtg_pre} meeting(s) → hkjc.db before SARR "
                    f"({n_rows_pre} rows) so SARR analyses the latest results."
                )
        except Exception as _e:
            st.warning(f"Pre-SARR DB sync skipped: {_e}")

        # ── [2/3] SARR model on the fresh card ─────────────────────
        _pbar.progress(33, text="[2/3] Running SARR model…")
        if not sarr_script.exists():
            st.error(f"✗ [2/3] SARR script not found at {sarr_script}")
        else:
            res = _run("[2/3] SARR model", [PYTHON, str(sarr_script), "--date", date_str], timeout=600)
            if isinstance(res, subprocess.CompletedProcess):
                sarr_mtime_after = sarr_json.stat().st_mtime if sarr_json.exists() else 0.0
                sarr_updated = sarr_mtime_after > sarr_mtime_before
                if res.returncode == 0 and sarr_json.exists():
                    _mt = hkt_from_ts(sarr_mtime_after)
                    if sarr_updated:
                        st.success(f"✓ [2/3] SARR generated: {sarr_json.name} ({_mt:%H:%M:%S} HKT)")
                    else:
                        st.warning(
                            f"⚠ [2/3] SARR exited cleanly but did NOT write a new file "
                            f"(existing JSON unchanged at {_mt:%H:%M:%S})."
                        )
                    with st.expander("SARR output"):
                        st.code((res.stdout or "")[-4000:])
                else:
                    st.error(f"✗ [2/3] SARR failed (exit {res.returncode}). "
                             f"JSON exists: {sarr_json.exists()}")
                    with st.expander("SARR error output"):
                        st.code((res.stderr or res.stdout or "")[-3000:])

        # ── [3/3] ET (v4.4) model — vet scrape + form-guide + analysis ──
        _pbar.progress(66, text="[3/3] Running ET (v4.4) model…")
        # Always pass --skip-scrape (we just scraped) and --skip-sarr (step 2/3
        # already ran SARR). run_meeting.py handles vet + form-guide cache +
        # analysis-script generation.
        # First: ensure hkjc.db has every results JSON on disk, so the form
        # guide built inside run_meeting picks up the latest meetings. Without
        # this, a horse who ran on (e.g.) 17 May would be missing from a
        # racecard generated for 20 May.
        try:
            n_mtg, n_rows = _auto_sync_results_to_db(verbose=False)
            if n_mtg:
                st.info(
                    f"Auto-synced {n_mtg} meeting(s) to hkjc.db before form-guide "
                    f"build ({n_rows} rows)."
                )
        except Exception as _e:
            st.warning(f"DB auto-sync skipped: {_e}")

        et_cmd = [PYTHON, str(BASE / "run_meeting.py"),
                  "--date", date_str, "--model", model,
                  "--skip-scrape", "--skip-sarr",
                  "--going-turf", going_turf, "--going-awt", going_awt]
        res = _run(f"[3/3] ET {model} model", et_cmd, timeout=900)
        if isinstance(res, subprocess.CompletedProcess):
            et_mtime_after = et_json.stat().st_mtime if et_json.exists() else 0.0
            if res.returncode == 0 and et_json.exists() and et_mtime_after > et_mtime_before:
                st.success(f"✓ [3/3] ET generated: {et_json.name}")
            elif res.returncode == 0:
                st.warning(f"⚠ [3/3] ET pipeline exited cleanly but {et_json.name} "
                           f"was not updated.")
            else:
                st.error(f"✗ [3/3] ET pipeline failed (exit {res.returncode})")
                with st.expander("ET error output"):
                    st.code((res.stderr or res.stdout or "")[-3000:])
            with st.expander("ET pipeline output"):
                st.code((res.stdout or "")[-4000:])
        _pbar.progress(100, text="Pipeline complete ✓")

    # ── Post-pipeline hooks: persist mutual picks + pace_v2 ─────────
    # These derive canonical artifacts from the freshly-generated ET + SARR
    # reports so the dashboard's read paths don't need to recompute live.
    et_json_now = REPORTS / f"race_day_report_{dc}_{model}.json"
    sarr_json_now = REPORTS / f"race_day_report_{dc}_SARR.json"

    # 0) Optional FUSE model. This is kept out of the default core pipeline
    # so routine ET/SARR refreshes finish quickly; Race Day Insight has a
    # dedicated FUSE refresh button for odds-sensitive rebuilds.
    if run_fuse and racecard_xlsx.exists():
        try:
            with st.spinner("[post] FUSE model training + inference…"):
                res_fu = subprocess.run(
                    [PYTHON, str(BASE / "fuse_raceday.py"),
                     "--date", dc, "--going", going_turf,
                     "--going-awt", going_awt],
                    env=env, cwd=str(BASE),
                    capture_output=True, text=True, encoding="utf-8",
                    timeout=600,
                )
            fuse_json = REPORTS / f"race_day_report_{dc}_FUSE.json"
            if res_fu.returncode == 0 and fuse_json.exists():
                st.success(f"✓ [post] FUSE report → {fuse_json.name}")
            else:
                st.warning(
                    f"⚠ [post] FUSE failed (exit {res_fu.returncode})"
                    + f": {(res_fu.stderr or res_fu.stdout or '')[-200:]}")
        except Exception as _e:
            st.warning(f"⚠ [post] FUSE model failed: {_e}")
    elif racecard_xlsx.exists():
        st.info("[post] FUSE skipped for speed. Use Race Day Insight → Refresh FUSE when needed.")

    if et_json_now.exists() and sarr_json_now.exists():
        # 1) Mutual picks (ET ∩ SARR top-3, + FUSE ranks when available)
        try:
            res_mut = subprocess.run(
                [PYTHON, str(BASE / "build_mutual_picks.py"),
                 "--date", dc],
                env=env, cwd=str(BASE),
                capture_output=True, text=True, encoding="utf-8",
                timeout=60,
            )
            if res_mut.returncode == 0:
                st.success(f"✓ [post] Persisted mutual picks → mutual_{dc}.json")
            else:
                st.warning(f"⚠ [post] mutual picks build failed (exit "
                           f"{res_mut.returncode})")
        except Exception as _e:
            st.warning(f"⚠ [post] mutual picks build failed: {_e}")

        # 2) Pace v2 inference (uses cached model if present)
        try:
            res_p2 = subprocess.run(
                [PYTHON, str(BASE / "pace_classifier_v2.py"),
                 "infer", "--date", dc],
                env=env, cwd=str(BASE),
                capture_output=True, text=True, encoding="utf-8",
                timeout=60,
            )
            if res_p2.returncode == 0 and (REPORTS / f"pace_v2_{dc}.json").exists():
                st.success(f"✓ [post] Persisted pace v2 → pace_v2_{dc}.json")
            elif "No trained model" in (res_p2.stdout or ""):
                st.info("ℹ [post] No pace_v2 model trained yet — skip "
                        "(run `python pace_classifier_v2.py train`).")
            else:
                st.warning(f"⚠ [post] pace_v2 inference failed (exit "
                           f"{res_p2.returncode})")
        except Exception as _e:
            st.warning(f"⚠ [post] pace_v2 inference failed: {_e}")

        # 3) Form screener — always run explicitly for the current meeting
        # (not deferred to _rebuild_caches_after_sync, which only fires when
        # new past-results are synced and silently skips when the DB is
        # already up to date, leaving form_screen_<dc>.json unwritten).
        try:
            res_fs = subprocess.run(
                [PYTHON, str(BASE / "form_screener.py"), "--date", date_str],
                env=env, cwd=str(BASE),
                capture_output=True, text=True, encoding="utf-8",
                timeout=300,
            )
            fs_json = REPORTS / f"form_screen_{dc}.json"
            if res_fs.returncode == 0 and fs_json.exists():
                st.success(f"✓ [post] Form screen → form_screen_{dc}.json")
            else:
                st.warning(
                    f"⚠ [post] form screener failed (exit {res_fs.returncode})"
                    + (f": {(res_fs.stderr or res_fs.stdout or '')[-200:]}"
                       if res_fs.returncode != 0 else ""))
        except Exception as _e:
            st.warning(f"⚠ [post] form screener failed: {_e}")

    # ── Persist artifacts back to GitHub on Streamlit Cloud ─────────
    # Streamlit Cloud's filesystem is ephemeral — anything written by the
    # subprocess (racecards, reports, caches) is wiped when the container
    # sleeps and restarts. Push them via the GitHub Contents API so the
    # next cold-start sees them. No-op locally.
    if _is_streamlit_cloud():
        try:
            n_pushed, n_missing, errors = _gh_persist_pipeline_outputs(date_str, model)
            if n_pushed:
                st.success(f"☁ Synced {n_pushed} pipeline file(s) to GitHub "
                           f"(skipped {n_missing} missing).")
            elif errors:
                st.warning("⚠ Pipeline outputs were NOT synced to GitHub: "
                           + "; ".join(errors[:3]))
            else:
                st.info("ℹ No pipeline outputs synced to GitHub "
                        "(none generated, or no GITHUB_TOKEN configured).")
        except Exception as e:
            st.warning(f"⚠ GitHub sync failed: {e}")

    # ── Clear data caches so fresh JSONs are picked up immediately ──
    try:
        st.cache_data.clear()
    except Exception:
        pass
    try:
        st.rerun()
    except Exception:
        pass


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

    # ── Persist uploaded racecard to GitHub on Streamlit Cloud ─────────
    # Without this push, the uploaded JSON + Excel live only on the
    # ephemeral container FS and disappear on the next reboot/redeploy.
    if _is_streamlit_cloud() and _gh_headers():
        try:
            msg = f"racecard: upload {date_str} [skip ci]"
            n_ok = 0
            if _gh_push_file(
                f"cache/racecard_{date_str}.json",
                cache_path.read_bytes(), msg):
                n_ok += 1
            if _gh_push_file(
                f"racecards/racecard_{date_compact}.xlsx",
                xl_path.read_bytes(), msg):
                n_ok += 1
            if n_ok:
                st.toast(f"☁ Synced {n_ok} racecard file(s) to GitHub",
                         icon="✅")
            else:
                st.warning("⚠ Uploaded racecard NOT synced to GitHub — "
                           "see sidebar persistence status for details.")
        except Exception as e:
            st.warning(f"⚠ GitHub sync of uploaded racecard failed: {e}")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Main page — Race Day view
# ══════════════════════════════════════════════════════════════════════════════

def _pace_bar_html(pace: str, score: float) -> str:
    """Render a compact inline pace bar.

    Fill width encodes race tempo on a single linear axis:
        dev = -1.0s  →  ~100%  (very fast — full bar, green)
        dev =  0.0s  →   50%   (neutral — half bar, amber)
        dev = +1.0s  →   ~5%   (very slow — barely-filled, red)

    Colour band is chosen from the predicted early-sectional deviation
    `score` (seconds vs HKJC reference; negative = faster).
    """
    try:
        s = float(score or 0.0)
    except (TypeError, ValueError):
        s = 0.0

    # Linear fill: 50% at s=0, capped [5, 100]
    width = max(5.0, min(100.0, 50.0 - s * 50.0))

    # Class / label from deviation thresholds (match classify_pace bands)
    if s <= -0.40:
        cls, label = "fast",    "FAST"
    elif s <= -0.20:
        cls, label = "sl-fast", "SL.FAST"
    elif s < 0.20:
        cls, label = "neutral", "NEUTRAL"
    elif s < 0.35:
        cls, label = "sl-slow", "SL.SLOW"
    else:
        cls, label = "slow",    "SLOW"

    # If caller already passed a descriptive pace string (e.g. "V.Fast",
    # "Slow") prefer it for the visible label so UI matches historical text.
    pace_txt = str(pace or "").strip()
    if pace_txt and pace_txt.lower() not in ("neutral", "-", "n/a", "normal"):
        label = pace_txt.upper()

    return (
        f'<span class="pace-bar-wrap">'
        f'<span class="pace-bar-track">'
        f'<span class="pace-bar-fill pace-{cls}" style="width:{width:.0f}%"></span>'
        f'</span>'
        f'<span class="pace-label {cls}">{label}</span>'
        f'<span style="font-size:0.78em;opacity:0.55">({s:+.2f}s)</span>'
        f'</span>'
    )


# ══════════════════════════════════════════════════════════════════════════════
# Overview page — Home / wagering briefing
# ══════════════════════════════════════════════════════════════════════════════

# ── Factor-analysis helpers (shared by Overview section + Data Analysis page)

FACTOR_TABLES_PATH = BASE / "reports" / "factor_analysis_tables.json"

# Numeric columns inside factor_analysis_tables.json (stored as strings on disk).
_FACTOR_NUMERIC_COLS = ("N", "Wins", "IV", "A_E", "ROI",
                        "Win_pct", "Plc_pct", "Base_win", "Exp_mkt")

# matplotlib is required by pandas Styler.background_gradient. On hosts where
# it's missing (e.g. minimal Streamlit Cloud images) the gradient call itself
# succeeds but rendering the styler later raises ImportError. Detect once.
try:
    import matplotlib  # noqa: F401
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False


def _factor_tables_mtime() -> float:
    """Returns the mtime of the factor tables file (0.0 if missing).
    Used as a cache key so caches invalidate automatically when the file changes."""
    try:
        return FACTOR_TABLES_PATH.stat().st_mtime
    except OSError:
        return 0.0


@st.cache_data(show_spinner=False)
def _load_factor_tables_cached(_mtime: float) -> dict:
    """Load factor_analysis_tables.json. `_mtime` is part of the cache key so
    updates on disk invalidate the cache without a manual clear."""
    if not FACTOR_TABLES_PATH.exists():
        return {}
    try:
        with open(FACTOR_TABLES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _load_factor_tables(_mtime: float | None = None) -> dict:
    """Public accessor: mtime is fetched automatically so every caller sees
    the freshest parsed JSON without needing to pass the mtime explicitly."""
    return _load_factor_tables_cached(
        _mtime if _mtime is not None else _factor_tables_mtime()
    )


@st.cache_data(show_spinner=False)
def _get_factor_df(window: str, key: str, _mtime: float = 0.0) -> pd.DataFrame:
    """Return the factor table `window/key` as a numeric-typed DataFrame.
    Cached per (window, key, mtime) so switching tabs / adjusting min-N
    sliders never reparses the JSON or recasts columns."""
    tables = _load_factor_tables_cached(_mtime)
    rows = (tables.get(window) or {}).get(key, [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for c in _FACTOR_NUMERIC_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


@st.cache_data(show_spinner=False)
def _get_factor_summary_edges(window: str, _mtime: float = 0.0) -> pd.DataFrame:
    """Pre-aggregated 'top edges' table for the Summary tab.
    Cached per (window, mtime) so Streamlit reruns (slider moves, tab switches)
    don't recompute the 9-table scan every time."""
    combined: list[pd.DataFrame] = []
    sources = ("jockey", "trainer", "sire", "dam_sire",
               "jockey_x_trainer", "sire_x_dist_bucket", "sire_x_going",
               "jockey_x_dist_bucket", "trainer_x_dist_bucket")
    for k in sources:
        df = _get_factor_df(window, k, _mtime)
        if df.empty or "A_E" not in df.columns or "N" not in df.columns:
            continue
        sub = df[(df["A_E"] >= 1.2) & (df["N"] >= 30)].copy()
        if sub.empty:
            continue
        meta_cols = [c for c in sub.columns if c not in _FACTOR_NUMERIC_COLS]
        sub["Bucket"] = (sub[meta_cols].astype(str).agg(" · ".join, axis=1)
                         if meta_cols else "")
        sub["Factor"] = k
        combined.append(sub[["Factor", "Bucket", "N", "Win_pct",
                             "IV", "A_E", "ROI"]])
    if not combined:
        return pd.DataFrame()
    out = (pd.concat(combined, ignore_index=True)
             .sort_values("A_E", ascending=False)
             .head(30)
             .reset_index(drop=True))
    out = out.rename(columns={"Win_pct": "Win%", "A_E": "A/E"})
    return out


def _dist_bucket_label(dist) -> str:
    try:
        d = float(dist)
    except (TypeError, ValueError):
        return ""
    if d <= 1050:   return "1000"
    if d <= 1250:   return "1200"
    if d <= 1450:   return "1400"
    if d <= 1700:   return "1600"
    if d <= 2000:   return "1800"
    return "2000+"


def _factor_lookup(rows: list[dict], key_fields: list[str], key_vals: list) -> dict | None:
    """Find the first row whose `key_fields` all match `key_vals` (case-insensitive)."""
    if not rows:
        return None
    targets = [str(v).strip().upper() for v in key_vals]
    for r in rows:
        if all(str(r.get(k, "")).strip().upper() == t for k, t in zip(key_fields, targets)):
            return r
    return None


def _fnum(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@st.cache_data(ttl=120)
def _build_horse_attribute_lookup() -> dict:
    """horse_name_upper → {trainer, sire, dam_sire, last_date, rating, prev_rating, declared_weight, prev_decl_weight}."""
    df = _load_form_db()
    if df.empty:
        return {}
    if "trainer" not in df.columns:
        return {}
    # Take latest row per horse
    df = df.sort_values("race_date")
    latest = df.groupby("horse_name_upper").tail(1).set_index("horse_name_upper")
    # Second-latest for delta calcs
    second_df = df.groupby("horse_name_upper").tail(2)
    prev = (second_df.groupby("horse_name_upper")
                    .head(1)   # earliest of the last two
                    .set_index("horse_name_upper"))
    out: dict = {}
    for name, row in latest.iterrows():
        p = prev.loc[name] if name in prev.index else None
        out[name] = {
            "trainer":   str(row.get("trainer", "")).strip() if pd.notna(row.get("trainer")) else "",
            "sire":      str(row.get("sire", "")).strip() if pd.notna(row.get("sire")) else "",
            "dam_sire":  str(row.get("dam_sire", "")).strip() if pd.notna(row.get("dam_sire")) else "",
            "last_date": row.get("race_date"),
            "last_race_class": str(row.get("race_class", "")).strip() if pd.notna(row.get("race_class")) else "",
            "rating":    _fnum(row.get("rating")),
            "prev_rating":        _fnum(p.get("rating")) if p is not None else None,
            "declared_weight":    _fnum(row.get("declared_weight")),
            "prev_decl_weight":   _fnum(p.get("declared_weight")) if p is not None else None,
        }
    return out


@st.cache_data(ttl=900, show_spinner=False)
def _lookup_condition_stats(horse_name: str, distance: int | None,
                             going: str, race_class: str,
                             dist_tolerance: int = 200) -> dict:
    """Query SQLite for a horse's win/place rate under TODAY'S conditions.

    Returns:
        {
          "n_runs": int,                     total matched runs
          "win_rate": float | None,          wins / n_runs
          "place_rate": float | None,        places (≤3) / n_runs
          "n_wins": int,
          "n_places": int,
          "conditions": str,                 human-readable filter description
          "jockey_cond_stat": str,           e.g. "Purton: 4W/8R here"
        }
    Falls back to distance-only + same going-band if n_runs < 3.
    Returns None dict when no data found.
    """
    try:
        from db_utils import read_sqlite
    except ImportError:
        return {}

    hn_upper = str(horse_name).upper().strip()

    # Going band grouping
    _GOING_BANDS: dict[str, set[str]] = {
        "firm":     {"FM", "F", "HD"},
        "good":     {"G", "GF", "GOOD", "GOOD TO FIRM"},
        "soft":     {"GY", "Y", "SE", "S", "GOOD TO YIELDING", "YIELDING",
                     "SOFT", "HEAVY", "WF", "WS"},
    }
    going_upper = str(going).upper().strip()
    going_band_members: set[str] = set()
    for _band, members in _GOING_BANDS.items():
        if going_upper in members:
            going_band_members = members
            break
    if not going_band_members:
        going_band_members = {going_upper}

    # Build SQL
    try:
        sql = (
            "SELECT horse_name, place, distance, going, race_class "
            "FROM results "
            "WHERE UPPER(horse_name) = ? "
        )
        params: list = [hn_upper]
        if distance:
            sql += f" AND distance BETWEEN ? AND ? "
            params += [distance - dist_tolerance, distance + dist_tolerance]
        df = read_sqlite(sql, params=params)
    except Exception:
        return {}

    if df.empty:
        return {}

    # Further filter by going band (Python-side for flexibility)
    df["going_u"] = df["going"].astype(str).str.upper().str.strip()
    band_df = df[df["going_u"].isin({g.upper() for g in going_band_members})]
    use_df = band_df if len(band_df) >= 2 else df

    if use_df.empty:
        return {}

    use_df = use_df.copy()
    use_df["place_n"] = pd.to_numeric(use_df["place"], errors="coerce")
    n_runs   = int(len(use_df))
    n_wins   = int((use_df["place_n"] == 1).sum())
    n_places = int((use_df["place_n"] <= 3).sum())

    going_desc = "/".join(sorted(going_band_members)[:3]) if band_df is not use_df else "all going"
    conditions_desc = (
        f"dist ≈{distance}m ±{dist_tolerance}m, going={going_desc}"
        if distance else f"going={going_desc}"
    )

    return {
        "n_runs":      n_runs,
        "win_rate":    n_wins / n_runs if n_runs else None,
        "place_rate":  n_places / n_runs if n_runs else None,
        "n_wins":      n_wins,
        "n_places":    n_places,
        "conditions":  conditions_desc,
    }


def _render_ensemble_votes(race: dict, sarr_race: dict | None,
                            edges_for_race: list[dict] | None,
                            bb_active: dict | None,
                            date_compact: str, venue_code: str, rn) -> None:
    """v4.6 — Ensemble 5-vote banker panel.

    For each horse on the card, count how many of the five independent
    signals support it:
       1. ET top-2 (race.picks rank 1 or 2)
       2. SARR top-2
       3. Factor edge green/amber tier
       4. Market value bet (p_model / p_market >= 1.15)
       5. Blackbook (active entry)

    Horses with 3+ votes are shown as bankers; 2 votes as watch list.
    """
    et_top2 = {str(p.get("horse_name","")).upper().strip()
               for p in (race.get("picks") or [])[:2]}
    sarr_top2 = ({str(p.get("horse_name","")).upper().strip()
                  for p in (sarr_race.get("picks") or [])[:2]}
                 if sarr_race else set())
    edge_green_amber = {str(e.get("horse","")).upper().strip()
                        for e in (edges_for_race or [])
                        if e.get("tier") in ("green", "amber")}
    bb_set = {str(k).upper().strip() for k in (bb_active or {}).keys()}

    # Market value bet: load p_market from live odds + ET win_prob normalised.
    value_set: set[str] = set()
    try:
        picks = race.get("picks") or []
        win_probs = {str(p.get("horse_name","")).upper().strip():
                     float(p.get("win_prob") or 0) / 100.0
                     for p in picks}
        # Normalise to sum=1
        s = sum(win_probs.values()) or 1.0
        win_probs = {k: v / s for k, v in win_probs.items()}
        if date_compact and venue_code and rn != "?":
            try:
                _drift = _compute_race_drift(date_compact, venue_code, int(rn))
            except Exception:
                _drift = None
            if _drift and _drift.get("horses"):
                # Build implied probability with de-overround
                imp = {}
                for o in _drift["horses"]:
                    try:
                        w = float(o.get("win_last") or 0)
                        if w > 1.01:
                            hn = str(o.get("horse","")).upper().strip()
                            imp[hn] = 1.0 / w
                    except (TypeError, ValueError):
                        continue
                overround = sum(imp.values()) or 1.0
                for hn, ip in imp.items():
                    p_mkt = ip / overround
                    p_mdl = win_probs.get(hn, 0.0)
                    if p_mkt > 0 and p_mdl / p_mkt >= 1.15:
                        value_set.add(hn)
    except Exception:
        value_set = set()

    # All candidate horse names (union of picks + sarr picks + edges + bb)
    candidates: dict[str, str] = {}
    for p in (race.get("picks") or [])[:8]:
        k = str(p.get("horse_name","")).upper().strip()
        if k: candidates[k] = str(p.get("horse_name",""))
    if sarr_race:
        for p in (sarr_race.get("picks") or [])[:8]:
            k = str(p.get("horse_name","")).upper().strip()
            if k and k not in candidates:
                candidates[k] = str(p.get("horse_name",""))

    votes = []
    for k, display in candidates.items():
        v = []
        if k in et_top2:        v.append("ET")
        if k in sarr_top2:      v.append("SARR")
        if k in edge_green_amber: v.append("Factor")
        if k in value_set:      v.append("Market")
        if k in bb_set:         v.append("BB")
        if v:
            votes.append((display, v, len(v)))

    votes.sort(key=lambda r: -r[2])
    if not votes or votes[0][2] < 2:
        return  # nothing useful to show

    bankers = [v for v in votes if v[2] >= 3]
    watch = [v for v in votes if v[2] == 2]

    rows_html = []
    for disp, sigs, n in bankers:
        chips = " ".join(
            f'<span style="background:rgba(34,197,94,0.18);color:#22c55e;'
            f'border-radius:8px;padding:1px 7px;margin-right:3px;font-size:0.78em;'
            f'font-weight:700">{s}</span>'
            for s in sigs)
        rows_html.append(
            f'<div style="padding:5px 0;border-left:4px solid #22c55e;'
            f'padding-left:9px;margin-bottom:4px">'
            f'<span style="color:#22c55e;font-weight:800;font-size:1.0em">'
            f'BANKER · {n}/5</span> &nbsp; '
            f'<span style="font-weight:700">{disp.title()}</span>'
            f'<div style="margin-top:3px">{chips}</div></div>')
    for disp, sigs, n in watch[:3]:
        chips = " ".join(
            f'<span style="background:rgba(245,158,11,0.18);color:#f59e0b;'
            f'border-radius:8px;padding:1px 7px;margin-right:3px;font-size:0.78em;'
            f'font-weight:700">{s}</span>'
            for s in sigs)
        rows_html.append(
            f'<div style="padding:4px 0;border-left:3px solid #f59e0b;'
            f'padding-left:9px;margin-bottom:3px">'
            f'<span style="color:#f59e0b;font-weight:700">Watch · 2/5</span> &nbsp; '
            f'<span style="font-weight:700">{disp.title()}</span>'
            f'<div style="margin-top:2px">{chips}</div></div>')

    st.markdown(
        '<div style="margin:10px 0 4px 0">'
        '<div style="font-size:0.95em;font-weight:800;color:#e5e7eb;'
        'letter-spacing:0.04em;margin-bottom:6px">'
        '🗳️ ENSEMBLE — 5-vote banker (ET · SARR · Factor · Market · Blackbook)'
        '</div>'
        + "".join(rows_html) +
        '</div>',
        unsafe_allow_html=True,
    )


def _compute_factor_edges(races: list[dict], window: str = "current_season_25_26",
                          top_n_per_race: int = 6) -> list[dict]:
    """For each top-N pick on the card, compute factor-edge signals from
    `factor_analysis_tables.json` and the horse attribute lookup."""
    tables = _load_factor_tables()
    win = tables.get(window) or tables.get("all_time") or {}
    if not win:
        return []
    attrs = _build_horse_attribute_lookup()
    today = date.today()

    edges: list[dict] = []
    for race in races:
        rn = race.get("race_number")
        dist_b = _dist_bucket_label(race.get("distance"))
        going  = str(race.get("going", "")).strip()
        today_class = str(race.get("race_class", "")).strip()
        for pick in race.get("picks", [])[:top_n_per_race]:
            hn_u = str(pick.get("horse_name", "")).upper().strip()
            jockey = str(pick.get("jockey", "")).strip()
            a = attrs.get(hn_u, {})
            trainer = a.get("trainer", "")
            sire     = a.get("sire", "")
            dam_sire = a.get("dam_sire", "")
            last_class = a.get("last_race_class", "")
            # Classify today's class move (HK: lower number = higher grade)
            class_step = ""
            try:
                if last_class and today_class:
                    lc = float(last_class); tc = float(today_class)
                    if tc < lc:   class_step = "step_up"
                    elif tc > lc: class_step = "step_down"
                    else:         class_step = "same_class"
            except (TypeError, ValueError):
                class_step = ""

            signals: list[tuple[str, str, float]] = []   # (label, colour, score contrib)

            # Jockey
            j_row = _factor_lookup(win.get("jockey", []), ["jockey"], [jockey])
            if j_row:
                iv = _fnum(j_row.get("IV"))
                ae = _fnum(j_row.get("A_E"))
                if iv and iv >= 1.5:
                    score = (iv - 1.0) * 0.8
                    if ae and ae >= 1.10: score += 0.4
                    signals.append((f"Jky IV {iv:.2f}"
                                    + (f" A/E {ae:.2f}" if ae else ""),
                                    "#22c55e" if iv >= 2.0 else "#86efac", score))

            # Trainer
            t_row = _factor_lookup(win.get("trainer", []), ["trainer"], [trainer]) if trainer else None
            if t_row:
                iv = _fnum(t_row.get("IV"))
                ae = _fnum(t_row.get("A_E"))
                if iv and iv >= 1.20:
                    score = (iv - 1.0) * 0.5
                    if ae and ae >= 1.15: score += 0.3
                    signals.append((f"Trn IV {iv:.2f}"
                                    + (f" A/E {ae:.2f}" if ae else ""),
                                    "#86efac", score))

            # Trainer × class step — only when today's run is a genuine class move
            if trainer and class_step in ("step_up", "step_down"):
                tcs_row = _factor_lookup(
                    win.get("trainer_x_class_step", []),
                    ["trainer", "class_step"], [trainer, class_step],
                )
                if tcs_row:
                    iv = _fnum(tcs_row.get("IV"))
                    ae = _fnum(tcs_row.get("A_E"))
                    n  = _fnum(tcs_row.get("N"))
                    # Need a real sample + clear out-performance
                    if iv and iv >= 1.6 and n and n >= 15:
                        score = (iv - 1.0) * 0.6
                        if ae and ae >= 1.3: score += 0.5
                        arrow = "↑" if class_step == "step_up" else "↓"
                        signals.append((
                            f"Trn{arrow}Class IV {iv:.2f} (N={int(n)})",
                            "#22c55e" if iv >= 2.5 else "#86efac", score))

            # Jockey × Trainer
            jt_row = _factor_lookup(win.get("jockey_x_trainer", []),
                                    ["jockey", "trainer"], [jockey, trainer]) \
                     if jockey and trainer else None
            if jt_row:
                iv = _fnum(jt_row.get("IV"))
                ae = _fnum(jt_row.get("A_E"))
                n  = _fnum(jt_row.get("N"))
                if iv and iv >= 2.0 and n and n >= 15:
                    score = (iv - 1.0) * 0.6
                    if ae and ae >= 1.2: score += 0.5
                    signals.append((f"J×T IV {iv:.2f} (N={int(n)})",
                                    "#22c55e" if iv >= 3.0 else "#86efac", score))

            # Sire × distance bucket (falls back to sire-only)
            if sire:
                sd_row = _factor_lookup(win.get("sire_x_dist_bucket", []),
                                        ["sire", "dist_bucket"], [sire, dist_b])
                if sd_row:
                    iv = _fnum(sd_row.get("IV")); ae = _fnum(sd_row.get("A_E"))
                    if iv and iv >= 1.6:
                        score = (iv - 1.0) * 0.5
                        if ae and ae >= 1.3: score += 0.4
                        signals.append((f"Sire@{dist_b}m IV {iv:.2f}",
                                        "#86efac", score))
                else:
                    s_row = _factor_lookup(win.get("sire", []), ["sire"], [sire])
                    if s_row:
                        iv = _fnum(s_row.get("IV")); ae = _fnum(s_row.get("A_E"))
                        if iv and iv >= 1.8:
                            score = (iv - 1.0) * 0.4
                            if ae and ae >= 1.3: score += 0.4
                            signals.append((f"Sire IV {iv:.2f}"
                                            + (f" A/E {ae:.2f}" if ae else ""),
                                            "#86efac", score))

            # Dam sire
            if dam_sire:
                ds_row = _factor_lookup(win.get("dam_sire", []),
                                        ["dam_sire"], [dam_sire])
                if ds_row:
                    iv = _fnum(ds_row.get("IV")); ae = _fnum(ds_row.get("A_E"))
                    if iv and iv >= 1.8:
                        score = (iv - 1.0) * 0.4
                        if ae and ae >= 1.3: score += 0.4
                        signals.append((f"DamSire IV {iv:.2f}", "#86efac", score))

            # Days-off freshness (120+ has A/E ≈ 1.44)
            last_d = a.get("last_date")
            if last_d:
                try:
                    days = (today - last_d).days
                except TypeError:
                    days = None
                if days is not None:
                    if days >= 120:
                        signals.append((f"Fresh {days}d (+edge)", "#22c55e", 0.6))
                    elif days <= 14:
                        signals.append((f"Quick b/u {days}d", "#ef4444", -0.4))

            # Rating delta (+2 or more ⇒ IV ~2.0)
            cr = a.get("rating"); pr = a.get("prev_rating")
            if cr is not None and pr is not None:
                dr = cr - pr
                if dr >= 2:
                    signals.append((f"Rtg Δ +{int(dr)}", "#22c55e", 0.5))
                elif dr <= -2:
                    signals.append((f"Rtg Δ {int(dr)}", "#ef4444", -0.3))

            if not signals:
                continue

            total_score = sum(s[2] for s in signals)
            # Tier
            if total_score >= 1.6 and len([s for s in signals if s[2] > 0]) >= 3:
                tier = "green"
            elif total_score >= 0.9:
                tier = "amber"
            else:
                tier = "none"

            edges.append({
                "race": rn,
                "rank": pick.get("rank"),
                "horse_no": pick.get("horse_no", ""),
                "horse": pick.get("horse_name", ""),
                "jockey": jockey,
                "trainer": trainer,
                "sire": sire,
                "dam_sire": dam_sire,
                "class_step": class_step,
                "signals": signals,
                "score": round(total_score, 2),
                "tier": tier,
            })
    return edges


@st.cache_data(show_spinner=False, ttl=3600)
def _factor_edges_for_meeting(date_compact: str, window: str,
                              report_path_str: str,
                              report_mtime: float, factor_mtime: float,
                              top_n: int = 6) -> list[dict]:
    """Cached wrapper over `_compute_factor_edges`. Reloads races from disk
    and recomputes only when the ET report or factor tables change.
    `report_mtime` and `factor_mtime` are part of the cache key so edits to
    either source invalidate the cache automatically."""
    _ = (date_compact, report_mtime, factor_mtime)  # cache-key signal only
    try:
        with open(report_path_str, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return []
    return _compute_factor_edges(data.get("races", []),
                                 window=window, top_n_per_race=top_n)


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


@st.cache_data(show_spinner=False, ttl=600)
def _form_screen_cached(date_compact: str, _mtime: float) -> dict | None:
    """Load reports/form_screen_<date>.json keyed on file mtime."""
    p = REPORTS / f"form_screen_{date_compact}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_form_screen(date_compact: str) -> dict | None:
    p = REPORTS / f"form_screen_{date_compact}.json"
    mtime = p.stat().st_mtime if p.exists() else 0.0
    return _form_screen_cached(date_compact, mtime)


def _h2h_recency_weight(date_str: str, today_str: str) -> float:
    """Half-life weight: a meeting loses half its importance every ~12 months."""
    from datetime import date as _date
    try:
        d = _date.fromisoformat(str(date_str)[:10])
        t = _date.fromisoformat(str(today_str)[:10])
        days = max((t - d).days, 0)
    except Exception:
        return 0.5
    return 0.5 ** (days / 365.0)


def _top_h2h_pairs(fs_race: dict, today_iso: str = "",
                   top_n: int | None = None) -> list[dict]:
    """Aggregate head-to-head pairings among today's runners, with full context.

    Each horse in the form-screen carries a `head_to_head` list of prior
    meetings vs other runners in THIS race. A-vs-B appears under both horses,
    so we collapse to an unordered pair, tally who finished ahead, and retain
    each meeting's barrier gates + handicap weights so we can show what's
    changed today. Pairs are ranked by *recency-weighted importance* (recent
    clashes count for more than old ones); ``top_n=None`` returns all pairs.
    """
    # Today's gates/weights per runner (for the "difference" context).
    today_meta: dict[str, dict] = {}
    for h in fs_race.get("horses", []) or []:
        nm = str(h.get("horse_name", "")).strip().upper()
        if nm:
            today_meta[nm] = {
                "draw": h.get("draw"),
                "weight": (h.get("actual_weight_today")
                           or h.get("declared_weight")),
                "no": h.get("horse_no"),
            }

    pairs: dict[frozenset, dict] = {}
    for h in fs_race.get("horses", []) or []:
        a = str(h.get("horse_name", "")).strip()
        if not a:
            continue
        for blk in h.get("head_to_head", []) or []:
            b = str(blk.get("rival", "")).strip()
            if not b:
                continue
            key = frozenset({a.upper(), b.upper()})
            rec = pairs.get(key)
            if rec is None:
                rec = {"a": a, "b": b, "a_ahead": 0, "b_ahead": 0,
                       "n": 0, "last_date": "", "importance": 0.0,
                       "meetings": [], "_seen": set()}
                pairs[key] = rec
            a_is_rec_a = (a == rec["a"])
            for m in blk.get("meetings", []) or []:
                try:
                    ap = float(m.get("a_place"))
                    bp = float(m.get("b_place"))
                except (TypeError, ValueError):
                    continue
                # Same meeting appears under BOTH horses' h2h lists — dedupe
                # by (date, race_number) so each meeting is counted once.
                rd = str(m.get("race_date", ""))[:10]
                mkey = (rd, m.get("race_number"))
                if mkey in rec["_seen"]:
                    continue
                rec["_seen"].add(mkey)
                a_ahead = ap < bp
                if a_ahead:
                    rec["a_ahead" if a_is_rec_a else "b_ahead"] += 1
                else:
                    rec["b_ahead" if a_is_rec_a else "a_ahead"] += 1
                rec["n"] += 1
                rec["importance"] += _h2h_recency_weight(rd, today_iso or rd)
                if rd > rec["last_date"]:
                    rec["last_date"] = rd
                # Orient all meeting fields to rec['a'] / rec['b'].
                if a_is_rec_a:
                    om = {
                        "a_place": m.get("a_place"), "b_place": m.get("b_place"),
                        "a_draw": m.get("a_draw"), "b_draw": m.get("b_draw"),
                        "a_weight": m.get("a_weight"), "b_weight": m.get("b_weight"),
                    }
                else:
                    om = {
                        "a_place": m.get("b_place"), "b_place": m.get("a_place"),
                        "a_draw": m.get("b_draw"), "b_draw": m.get("a_draw"),
                        "a_weight": m.get("b_weight"), "b_weight": m.get("a_weight"),
                    }
                om["date"] = rd
                om["distance"] = m.get("distance")
                om["going"] = m.get("going")
                om["race_class"] = m.get("race_class")
                rec["meetings"].append(om)

    out = [r for r in pairs.values() if r["n"] > 0]
    for r in out:
        r.pop("_seen", None)
        r["meetings"].sort(key=lambda x: x.get("date", ""), reverse=True)
        # Attach today's gates/weights for the difference context.
        ta = today_meta.get(r["a"].upper(), {})
        tb = today_meta.get(r["b"].upper(), {})
        r["today_a_draw"] = ta.get("draw")
        r["today_b_draw"] = tb.get("draw")
        r["today_a_weight"] = ta.get("weight")
        r["today_b_weight"] = tb.get("weight")
        # Compute weight swing vs last meeting: |Δ(A−B gap)|.
        # Bigger swing = bigger reversal of conditions = more notable.
        ws = 0
        last_m = r["meetings"][0] if r["meetings"] else {}
        def _wi(v):
            try: return int(float(v))
            except (TypeError, ValueError): return None
        la_w = _wi(last_m.get("a_weight"))
        lb_w = _wi(last_m.get("b_weight"))
        ta_w = _wi(r["today_a_weight"])
        tb_w = _wi(r["today_b_weight"])
        if None not in (la_w, lb_w, ta_w, tb_w):
            ws = abs((ta_w - tb_w) - (la_w - lb_w))
        r["weight_swing"] = ws
    # Primary: weight swing (bigger = more notable first).
    # Secondary: recency-weighted importance, then meeting count, then recency.
    out.sort(key=lambda r: (r["weight_swing"], round(r["importance"], 3),
                            r["n"], r["last_date"]),
             reverse=True)
    return out if top_n is None else out[:top_n]


def _fmt_h2h_pair(p: dict) -> str:
    """Render one head-to-head pair card: record + last clash gates/weights +
    what's changed today (gate moves, weight swing)."""
    def _i(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    a, b = p["a"].title(), p["b"].title()
    if p["a_ahead"] == p["b_ahead"]:
        lead = f'level {p["a_ahead"]}–{p["b_ahead"]}'
        col = "#9ca3af"
    else:
        leader = a if p["a_ahead"] > p["b_ahead"] else b
        hi, lo = max(p["a_ahead"], p["b_ahead"]), min(p["a_ahead"], p["b_ahead"])
        lead = f'{leader} leads {hi}–{lo}'
        col = "#22c55e"

    meets = p.get("meetings", []) or []
    last = meets[0] if meets else {}
    # Last-clash context line: gates drawn + weights carried then.
    ctx = ""
    if last:
        la_d, lb_d = _i(last.get("a_draw")), _i(last.get("b_draw"))
        la_w, lb_w = _i(last.get("a_weight")), _i(last.get("b_weight"))
        dist = _i(last.get("distance"))
        going = str(last.get("going") or "").strip()
        ap, bp = last.get("a_place"), last.get("b_place")
        gpart_a = f'gate {la_d}' if la_d is not None else 'gate ?'
        gpart_b = f'gate {lb_d}' if lb_d is not None else 'gate ?'
        wpart_a = f'{la_w}lb' if la_w is not None else ''
        wpart_b = f'{lb_w}lb' if lb_w is not None else ''
        head = (f'{last.get("date","")} · {dist}m'
                + (f' {going}' if going else ''))
        ctx = (
            f'<div style="font-size:0.72em;opacity:0.7;margin-top:2px">'
            f'last: {head}<br>'
            f'<b>{a}</b> {gpart_a} {wpart_a} → {ap} · '
            f'<b>{b}</b> {gpart_b} {wpart_b} → {bp}</div>')

        # "Difference today" line: gate moves + weight swing vs that clash.
        diffs = []
        ta_d, tb_d = _i(p.get("today_a_draw")), _i(p.get("today_b_draw"))
        ta_w, tb_w = _i(p.get("today_a_weight")), _i(p.get("today_b_weight"))
        if la_d is not None and ta_d is not None and ta_d != la_d:
            diffs.append(f'{a.split()[0]} gate {la_d}→{ta_d}')
        if lb_d is not None and tb_d is not None and tb_d != lb_d:
            diffs.append(f'{b.split()[0]} gate {lb_d}→{tb_d}')
        # Weight swing: change in the A−B weight gap (− = A better off today).
        if None not in (la_w, lb_w, ta_w, tb_w):
            prior_gap = la_w - lb_w
            today_gap = ta_w - tb_w
            swing = today_gap - prior_gap
            if swing != 0:
                better = a if swing < 0 else b
                diffs.append(f'{abs(swing)}lb swing to {better.split()[0]}')
        if diffs:
            ctx += (f'<div style="font-size:0.72em;opacity:0.6;'
                    f'color:#fbbf24;margin-top:1px">Δ today: '
                    f'{" · ".join(diffs)}</div>')

    # Weight-swing badge: shown when swing ≥ 4lb (notable shift in conditions).
    ws = p.get("weight_swing", 0) or 0
    ws_badge = ""
    if ws >= 4:
        ws_col = "#ef4444" if ws >= 8 else "#f97316" if ws >= 6 else "#fbbf24"
        ws_badge = (f' <span style="background:{ws_col};color:#0b0b0b;'
                    f'padding:0 5px;border-radius:5px;font-size:0.70em;'
                    f'font-weight:800;margin-left:4px">⚖ {ws}lb swing</span>')

    return (
        f'<div style="padding:4px 0;border-left:3px solid {col};'
        f'padding-left:8px;margin-bottom:5px">'
        f'<span style="font-weight:700">{a}</span>'
        f' <span style="opacity:0.55">vs</span> '
        f'<span style="font-weight:700">{b}</span>'
        f' <span style="opacity:0.6;font-size:0.82em">'
        f'· {p["n"]} mtg{"s" if p["n"] != 1 else ""}</span>'
        f'{ws_badge}'
        f'<div style="font-size:0.78em;opacity:0.78">{lead}</div>'
        f'{ctx}</div>')


def _render_form_screen_panel(race: dict, date_compact: str) -> None:
    """Form-screen notable mentions + top-5 H2H encounters for this race."""
    fs = _load_form_screen(date_compact)
    if not fs:
        return
    rn = race.get("race_number")
    fs_race = next((r for r in fs.get("races", [])
                    if r.get("race_no") == rn), None)
    if not fs_race:
        return

    st.markdown("#### 🔬 Form Screen")
    c1, c2 = st.columns(2)

    with c1:
        st.markdown("**Notable mentions**")
        shortlist = fs_race.get("shortlist", []) or []
        horses = {str(h.get("horse_name", "")).upper(): h
                  for h in fs_race.get("horses", []) or []}
        if shortlist:
            for item in shortlist[:5]:
                hn = str(item.get("horse", "")).strip()
                h = horses.get(hn.upper(), {})
                no = h.get("horse_no", "?")
                bb_chip = " ★" if h.get("blackbook") else ""
                rationale = str(item.get("rationale", "")).strip()
                st.markdown(
                    f'<div style="padding:3px 0;border-left:3px solid #60a5fa;'
                    f'padding-left:8px;margin-bottom:3px">'
                    f'<span style="font-weight:700">{hn.title()}</span>'
                    f' <span style="opacity:0.6;font-size:0.85em">(#{no})</span>'
                    f'{bb_chip}'
                    f'<div style="font-size:0.78em;opacity:0.7">{rationale}</div>'
                    f'</div>', unsafe_allow_html=True)
        else:
            st.caption("No standout shortlist for this race.")

    with c2:
        st.markdown("**Head-to-head (runners who've met)**")
        pairs = _top_h2h_pairs(fs_race, today_iso=fs.get("date_iso", ""),
                               top_n=None)
        if pairs:
            # "Important ones": recent clashes weighted highest. Show the most
            # important inline; collapse the long tail into an expander.
            INLINE = 6
            for p in pairs[:INLINE]:
                st.markdown(_fmt_h2h_pair(p), unsafe_allow_html=True)
            if len(pairs) > INLINE:
                with st.expander(f"➕ {len(pairs) - INLINE} more pairings",
                                 expanded=False):
                    for p in pairs[INLINE:]:
                        st.markdown(_fmt_h2h_pair(p), unsafe_allow_html=True)
        else:
            st.caption("No prior meetings between today's runners.")


def _render_cycle_signals_for_race(race: dict, date_compact: str) -> None:
    """Rating-cycle (class-dropper) signals for this race, from horse_cycle."""
    try:
        from horse_cycle import load_or_build_card_cycle
        payload = load_or_build_card_cycle(date_compact)
    except Exception:
        return
    if not payload:
        return
    rn = race.get("race_number")
    rows = [h for h in payload.get("horses", [])
            if h.get("race_number") == rn
            and h.get("primary_flag") in ("PRIMED", "EARLY_DROPPER",
                                          "WATCH", "FADE_OVERRATED")]
    if not rows:
        return
    order = {"PRIMED": 0, "EARLY_DROPPER": 1, "WATCH": 2, "FADE_OVERRATED": 3}
    rows.sort(key=lambda r: (order.get(r["primary_flag"], 9),
                             r.get("combined_tier", "Z")))
    flag_disp = {
        "PRIMED": ("#22c55e", "🟢 PRIMED"),
        "EARLY_DROPPER": ("#84cc16", "🫒 DROPPER"),
        "WATCH": ("#f59e0b", "🟡 WATCH"),
        "FADE_OVERRATED": ("#ef4444", "🔴 FADE"),
    }
    st.markdown("#### 🌡️ Rating-cycle signals")
    for r in rows:
        col, lbl = flag_disp.get(r["primary_flag"], ("#9ca3af", r["primary_flag"]))
        tier = r.get("combined_tier", "")
        tier_chip = (f'<span style="background:{col};color:#0b0b0b;'
                     f'padding:0 6px;border-radius:6px;font-size:0.72em;'
                     f'font-weight:800;margin-left:6px">Tier {tier}</span>'
                     if tier else "")
        st.markdown(
            f'<div style="padding:3px 0;border-left:3px solid {col};'
            f'padding-left:8px;margin-bottom:3px">'
            f'<span style="color:{col};font-weight:800">{lbl}</span> '
            f'<span style="font-weight:700">{str(r.get("horse_name","")).title()}</span>'
            f' <span style="opacity:0.6;font-size:0.82em">'
            f'· {r.get("trainer","")} · {r.get("stable_mode","")}</span>'
            f'{tier_chip}'
            f'<div style="font-size:0.78em;opacity:0.72">{r.get("note","")}</div>'
            f'</div>', unsafe_allow_html=True)


# ── Pace / sectional-metric colour scales (shared) ───────────────────────
# Temperature scale: fast = red, slow = blue, normal = gray. Accepts canonical
# labels ("Very Fast") and legacy abbreviations ("V.Fast").
_PACE_LABEL_COLOUR = {
    "VERY FAST": "#b91c1c", "VFAST": "#b91c1c",
    "FAST": "#ef4444",
    "SLIGHTLY FAST": "#fca5a5", "SLFAST": "#fca5a5",
    "NORMAL": "#9ca3af",
    "SLIGHTLY SLOW": "#93c5fd", "SLSLOW": "#93c5fd",
    "SLOW": "#3b82f6",
    "VERY SLOW": "#1d4ed8", "VSLOW": "#1d4ed8",
}


def _pace_label_colour(label) -> str:
    """Temperature-scale colour for a race-pace label."""
    if not label:
        return "#9ca3af"
    raw = str(label).strip().upper()
    if raw in _PACE_LABEL_COLOUR:
        return _PACE_LABEL_COLOUR[raw]
    key = raw.replace(".", "")
    return _PACE_LABEL_COLOUR.get(key, "#9ca3af")


def _metric_z_colour(z) -> str:
    """Temperature colour for a standardised metric: negative=fast/red."""
    import math as _m
    if z is None:
        return "#9ca3af"
    try:
        v = float(z)
    except (TypeError, ValueError):
        return "#9ca3af"
    if _m.isnan(v) or v == 0.0:
        return "#9ca3af"
    if v <= -2.0:
        return "#b91c1c"
    if v <= -1.0:
        return "#ef4444"
    if v <= -0.4:
        return "#fca5a5"
    if v < 0.4:
        return "#9ca3af"
    if v < 1.0:
        return "#93c5fd"
    if v < 2.0:
        return "#3b82f6"
    return "#1d4ed8"


def _fmt_signed(v, nd: int = 1):
    """Format a float with a leading sign, or return None for missing values."""
    import math as _m
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if _m.isnan(f):
        return None
    return f"{f:+.{nd}f}"


@st.cache_data(show_spinner=False)
def _sectional_metrics_cached(sqlite_mtime: float, xlsx_mtime: float) -> dict:
    """Cached per-run ESZ + Fin Δ lookup for the Form Guide."""
    from pace_utils import compute_sectional_metrics
    from db_utils import load_results_db
    db = load_results_db()
    if db is None or db.empty:
        return {}
    return compute_sectional_metrics(db)


def _render_race_cockpit(race: dict, sarr_race: dict | None,
                         edges_for_race: list[dict],
                         bb_active: dict, trial_index: dict | None,
                         meeting_info: dict):
    """Render the Race-Time Cockpit block for a single race.

    Hero layout:
      ┌─ race-meta ─┐─ mutual top-3 (ET ∩ SARR) ─┐─ factor edges top-3 ─┐
                    │                             │
                    └─ mini speedmap (optional) ──┘
    Flags on each mutual pick: ★ blackbook, 🏇 recent trial, ● pace beneficiary.
    """
    # ── Header strip ─────────────────────────────────────────────────
    rn   = race.get("race_number", "?")
    name = race.get("race_name", "")
    dist = race.get("distance", "?")
    klass = race.get("race_class", "?")
    going = race.get("going", "")
    surf  = "AWT" if race.get("is_awt") else "Turf"
    course = race.get("race_course", "")
    pace = race.get("pace", "Normal")
    pace_score = race.get("pace_score", 0.0)
    field_size = len(race.get("picks", []) or race.get("speed_map", {}).get("grid", []))

    pace_colour = _pace_label_colour(pace)

    st.markdown(
        f'<div style="background:linear-gradient(90deg,rgba(96,165,250,0.12),rgba(96,165,250,0));'
        f'padding:10px 14px;border-radius:8px;border-left:4px solid #60a5fa;margin-bottom:12px">'
        f'<div style="font-size:1.15em;font-weight:800">R{rn} — {name}</div>'
        f'<div style="opacity:0.85;font-size:0.92em;margin-top:2px">'
        f'{dist}m {surf}{f" ({course})" if course else ""} &nbsp;·&nbsp; Class {klass}'
        f'{f" &nbsp;·&nbsp; Going: {going}" if going else ""}'
        f' &nbsp;·&nbsp; Field {field_size}'
        f' &nbsp;·&nbsp; Pace: <span style="color:{pace_colour};font-weight:700">{pace}</span>'
        f' <span style="opacity:0.6">({pace_score:+.2f}s)</span>'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    # ── Blackbooked picks for THIS race (always-visible strip) ──────
    # Surfaces every active blackbook horse running in this race so the
    # user sees them on first sight without needing to open the rollup.
    # Picks list is checked first (preferred: includes ET rank / win%);
    # speed-map grid is checked as a fallback so horses outside ET top-N
    # still show.
    bb_in_race: list[dict] = []
    _seen_bb: set[str] = set()
    for pick in (race.get("picks") or []):
        _hn = str(pick.get("horse_name", "")).upper().strip()
        if _hn and _hn in bb_active and _hn not in _seen_bb:
            _seen_bb.add(_hn)
            bb_in_race.append({
                "horse": pick.get("horse_name", ""),
                "horse_no": pick.get("horse_no", "?"),
                "draw": pick.get("draw", ""),
                "rank": pick.get("rank"),
                "win_pct": pick.get("win_prob", 0),
                "jockey": pick.get("jockey", ""),
                "entry": bb_active[_hn],
            })
    for sm in (race.get("speed_map", {}).get("grid", []) or []):
        _hn = str(sm.get("horse_name", "")).upper().strip()
        if _hn and _hn in bb_active and _hn not in _seen_bb:
            _seen_bb.add(_hn)
            bb_in_race.append({
                "horse": sm.get("horse_name", ""),
                "horse_no": sm.get("horse_no", "?"),
                "draw": sm.get("draw", ""),
                "rank": None,
                "win_pct": 0,
                "jockey": sm.get("jockey", ""),
                "entry": bb_active[_hn],
            })

    if bb_in_race:
        _conf_colour = {"high": "#22c55e", "medium": "#f59e0b",
                        "low": "#9ca3af"}
        cards: list[str] = []
        for m in bb_in_race:
            e = m["entry"]
            conf = str(e.get("confidence", "")).lower()
            ccol = _conf_colour.get(conf, "#9ca3af")
            tags = e.get("tags", []) or []
            tag_html = ""
            if tags:
                tag_html = (
                    '<span style="opacity:0.65;font-size:0.75em">&nbsp;· '
                    + " · ".join(str(t) for t in tags[:3]) + "</span>"
                )
            rank_html = (f' <span style="opacity:0.55;font-size:0.78em">'
                         f'ET&nbsp;#{m["rank"]} · {m["win_pct"]:.1f}%</span>'
                         if m.get("rank") is not None else "")
            reasoning = str(e.get("reasoning", "")).strip()
            reason_html = ""
            if reasoning:
                # Trim long reasoning so the strip stays compact.
                short = reasoning if len(reasoning) <= 90 else reasoning[:87] + "…"
                reason_html = (f'<div style="opacity:0.7;font-size:0.78em;'
                               f'margin-top:2px">{short}</div>')
            draw_str = (f", draw {m['draw']}"
                        if m.get("draw") not in ("", None) else "")
            cards.append(
                f'<div style="display:inline-block;border-left:3px solid {ccol};'
                f'background:rgba(251,191,36,0.06);padding:5px 9px;margin:0 6px 6px 0;'
                f'border-radius:4px;vertical-align:top;max-width:320px">'
                f'<div style="font-size:0.92em">'
                f'<span style="color:#fbbf24">★</span> '
                f'<span style="font-weight:700">{m["horse"]}</span>'
                f' <span style="opacity:0.6;font-size:0.82em">'
                f'(#{m["horse_no"]}{draw_str})</span>'
                f'{rank_html}'
                f' <span style="color:{ccol};font-size:0.78em;font-weight:700;'
                f'text-transform:uppercase">&nbsp;· {conf or "?"}</span>'
                f'{tag_html}</div>'
                f'{reason_html}'
                f'</div>'
            )
        st.markdown(
            '<div style="margin:-4px 0 10px 0;padding:8px 10px;'
            'background:rgba(251,191,36,0.05);border:1px solid rgba(251,191,36,0.25);'
            'border-radius:6px">'
            '<div style="font-size:0.82em;font-weight:700;color:#fbbf24;'
            'letter-spacing:0.04em;margin-bottom:4px">'
            f'★ BLACKBOOK · {len(bb_in_race)} active in R{rn}</div>'
            + "".join(cards)
            + '</div>',
            unsafe_allow_html=True,
        )

    # ── 4 columns: ET top4 / SARR top4 / mutual / factor edges ─────────
    c1, c_sarr, c2, c3 = st.columns([1.05, 1.05, 1.25, 1.35])

    # Helpers
    bb_names = set(bb_active.keys())
    trial_names: set[str] = set()
    if trial_index:
        from datetime import datetime as _dto, timedelta as _td
        cutoff = _dto.now() - _td(days=60)
        for h, entries in trial_index.items():
            for e in entries:
                try:
                    tdt = _dto.strptime(e["date"], "%Y-%m-%d")
                except (ValueError, TypeError):
                    continue
                if tdt >= cutoff:
                    trial_names.add(h.upper().strip())
                    break

    beneficiary_names = {
        str(b.get("horse_name", "")).upper().strip()
        for b in race.get("speed_map", {}).get("beneficiaries", [])
    }

    def _flags(hn_upper: str) -> str:
        chips = []
        if hn_upper in bb_names:
            chips.append('<span title="Blackbook" style="color:#fbbf24">★</span>')
        if hn_upper in trial_names:
            chips.append('<span title="Recent trial" style="color:#a78bfa">🏇</span>')
        if hn_upper in beneficiary_names:
            chips.append('<span title="Pace beneficiary" style="color:#22c55e">●</span>')
        return " ".join(chips)

    # ── Col 1: meta / top ET picks (solo) ────────────────────────────
    with c1:
        st.markdown("**ET top 4**", help="Top 4 from the v4.4 ET model for this race")
        for pick in (race.get("picks") or [])[:4]:
            hn_u = str(pick.get("horse_name", "")).upper().strip()
            flags = _flags(hn_u)
            st.markdown(
                f'<div style="padding:3px 0">'
                f'<span style="opacity:0.55">#{pick.get("rank","?")}</span> '
                f'<span style="font-weight:700">{pick.get("horse_name","")}</span>'
                f' <span style="opacity:0.6;font-size:0.85em">({pick.get("horse_no","?")})</span>'
                f' &nbsp; {flags}'
                f'<div style="font-size:0.82em;opacity:0.7">'
                f'Win {pick.get("win_prob",0):.1f}% · {pick.get("jockey","")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # ── Col SARR: SARR top 4 ─────────────────────────────────────────
    with c_sarr:
        st.markdown("**SARR top 4**", help="Top 4 from the SARR (speed-adjusted) model for this race")
        if not sarr_race:
            st.caption("SARR not available — run [2/3] to populate.")
        else:
            for pick in (sarr_race.get("picks") or [])[:4]:
                hn_u = str(pick.get("horse_name", "")).upper().strip()
                flags = _flags(hn_u)
                st.markdown(
                    f'<div style="padding:3px 0">'
                    f'<span style="opacity:0.55">#{pick.get("rank","?")}</span> '
                    f'<span style="font-weight:700">{pick.get("horse_name","")}</span>'
                    f' <span style="opacity:0.6;font-size:0.85em">({pick.get("horse_no","?")})</span>'
                    f' &nbsp; {flags}'
                    f'<div style="font-size:0.82em;opacity:0.7">'
                    f'Win {pick.get("win_prob",0):.1f}% · {pick.get("jockey","")}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ── Col 2: Mutual ET ∩ SARR top-4 ──────────────────────────────
    with c2:
        st.markdown("**Mutual picks (ET ∩ SARR)**")
        if not sarr_race:
            st.caption("SARR not available — run [2/3] to populate.")
        else:
            et_top = {str(p["horse_name"]).upper().strip(): p
                      for p in race.get("picks", [])[:4]}
            sa_top = {str(p["horse_name"]).upper().strip(): p
                      for p in sarr_race.get("picks", [])[:4]}
            mutual = set(et_top) & set(sa_top)
            if not mutual:
                st.caption("No mutual top-4 picks.")
            else:
                rows = []
                for hn in mutual:
                    ep = et_top[hn]; sp = sa_top[hn]
                    e_rk = ep.get("rank", 99); s_rk = sp.get("rank", 99)
                    if e_rk <= 2 and s_rk <= 2:
                        col = "#22c55e"
                    elif e_rk <= 3 and s_rk <= 3:
                        col = "#f59e0b"
                    else:
                        col = "inherit"
                    rows.append((min(e_rk, s_rk), hn, ep, sp, e_rk, s_rk, col))
                rows.sort(key=lambda r: r[0])
                # Going from race result info or ET race dict
                race_going = str(race.get("going") or meeting_info.get("going", "")).strip()
                race_dist  = None
                try:
                    race_dist = int(race.get("distance") or 0) or None
                except (TypeError, ValueError):
                    pass
                race_class = str(race.get("race_class", "")).strip()
                for _, hn, ep, sp, e_rk, s_rk, col in rows[:3]:
                    flags = _flags(hn)
                    # Race Lookup inline: condition-specific win/place rate
                    cond_stat = _lookup_condition_stats(
                        ep.get("horse_name", ""),
                        race_dist,
                        race_going,
                        race_class,
                    )
                    if cond_stat and cond_stat.get("n_runs", 0) >= 2:
                        wr = cond_stat["win_rate"]; pr = cond_stat["place_rate"]
                        nr = cond_stat["n_runs"]
                        nw = cond_stat["n_wins"];  np_ = cond_stat["n_places"]
                        wr_str = f"{wr:.0%}" if wr is not None else "—"
                        pr_str = f"{pr:.0%}" if pr is not None else "—"
                        cond_desc = cond_stat["conditions"]
                        cond_html = (
                            f'<div style="font-size:0.76em;color:#60a5fa;margin-top:1px" '
                            f'title="{cond_desc}">'
                            f'🔍 {nw}W/{np_}P/{nr}R (W={wr_str} P={pr_str})</div>'
                        )
                    else:
                        cond_html = ""
                    st.markdown(
                        f'<div style="padding:4px 0;border-left:3px solid {col};'
                        f'padding-left:8px;margin-bottom:3px">'
                        f'<span style="color:{col};font-weight:800">{ep.get("horse_name","").title()}</span>'
                        f' <span style="opacity:0.6;font-size:0.85em">(#{ep.get("horse_no","?")})</span>'
                        f' &nbsp; {flags}'
                        f'<div style="font-size:0.82em;opacity:0.75">'
                        f'ET #{e_rk} · SARR #{s_rk} · {ep.get("jockey","")}</div>'
                        f'{cond_html}'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

    # ── Col 3: Factor edges top 3 for this race ────────────────────
    with c3:
        st.markdown("**Factor edges**", help="Historical IV / A-E signals for this race only")
        if not edges_for_race:
            st.caption("No qualifying signals for this race.")
        else:
            top_edges = sorted(edges_for_race, key=lambda e: -e["score"])[:3]
            for e in top_edges:
                col = ("#22c55e" if e["tier"] == "green"
                       else "#f59e0b" if e["tier"] == "amber" else "inherit")
                sig_short = " · ".join(lbl for lbl, _, _ in e["signals"][:3])
                flags = _flags(str(e.get("horse","")).upper().strip())
                st.markdown(
                    f'<div style="padding:4px 0;border-left:3px solid {col};'
                    f'padding-left:8px;margin-bottom:3px">'
                    f'<span style="color:{col};font-weight:800">{e.get("horse","")}</span>'
                    f' <span style="opacity:0.6;font-size:0.85em">(#{e.get("horse_no","?")})</span>'
                    f' &nbsp; <span style="color:{col};font-weight:700">{e["score"]:+.2f}</span>'
                    f' &nbsp; {flags}'
                    f'<div style="font-size:0.80em;opacity:0.7">{sig_short}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ── Market Pulse (live-odds drift) for the selected race ────────
    # Stashed by page_overview so we don't have to re-derive venue here.
    try:
        _date_compact = st.session_state.get("_rdi_date_compact", "")
        _venue_code = st.session_state.get("_rdi_venue_code", "")
        if _date_compact and _venue_code and rn != "?":
            _render_race_day_market_pulse(
                _date_compact, _venue_code, int(rn),
                race.get("picks") or [],
            )
    except Exception as _mp_err:
        st.caption(f"_Market Pulse unavailable: {_mp_err}_")

    # ── Value Lens (static p_model vs p_market) ─────────────────────
    # Companion to Market Pulse: Pulse = "where is money moving?",
    # Lens = "is the current price a value vs our model?".
    try:
        if _date_compact and _venue_code and rn != "?":
            _render_race_day_value_lens(
                _date_compact, _venue_code, int(rn),
                race.get("picks") or [],
            )
    except Exception as _vl_err:
        st.caption(f"_Value Lens unavailable: {_vl_err}_")

    # ── Ensemble 5-Vote Banker (v4.6) ──────────────────────────────
    # Five independent signals vote on each horse. 3+/5 = banker tier.
    # Backtest insight: when ≥3 signals agree on a horse, hit rates lift
    # materially. Signals: ET top-2, SARR top-2, Factor edge (green/amber),
    # Market value bet (model/market ≥ 1.15), Blackbook.
    try:
        _render_ensemble_votes(race, sarr_race, edges_for_race,
                               bb_active, _date_compact, _venue_code, rn)
    except Exception as _ev_err:
        st.caption(f"_Ensemble votes unavailable: {_ev_err}_")

    # ── FUSE table + refresh actions ───────────────────────────────
    try:
        _render_fuse_race_segment(
            race,
            st.session_state.get("_rdi_date_compact", ""),
            st.session_state.get("_rdi_venue_code", ""),
            meeting_info,
        )
    except Exception as _fu_err:
        st.caption(f"_FUSE table unavailable: {_fu_err}_")

    # ── Form Screen (notable mentions + top-5 H2H) ────────────────
    try:
        _date_compact2 = st.session_state.get("_rdi_date_compact", "")
        if _date_compact2:
            _render_form_screen_panel(race, _date_compact2)
    except Exception as _fs_err:
        st.caption(f"_Form Screen unavailable: {_fs_err}_")

    # ── Rating-cycle / class-dropper signals ──────────────────────
    try:
        if _date_compact2:
            _render_cycle_signals_for_race(race, _date_compact2)
    except Exception as _cy_err:
        st.caption(f"_Rating-cycle signals unavailable: {_cy_err}_")

    # Footer: cross-navigation
    st.caption("★ Blackbook &nbsp; 🏇 Recent trial &nbsp; ● Pace beneficiary "
               "&nbsp; · &nbsp; Jump to full analysis on **Model Analysis** page.")


def _set_meeting_ctx(date_iso: str, venue: str, n_races: int) -> None:
    """Stash the active meeting for the cross-page context bar."""
    st.session_state["_meeting_ctx"] = {
        "date": date_iso or "",
        "venue": str(venue or ""),
        "n_races": int(n_races or 0),
    }


# ── Scratching / reserve-promotion tracking (#1) ──────────────────────────
# We snapshot the active field each time a meeting is loaded with a changed
# line-up, then diff the two most recent snapshots so a late scratching or a
# promoted reserve can be flagged on the readiness strip.
_RACECARD_SNAP_DIR = BASE / "cache" / "racecard_snapshots"


def _field_signature(races: list) -> dict:
    """Build a per-race {active:[no], standby:[no], names:{no:name}} map."""
    field = {}
    for r in races or []:
        rn = r.get("race_number")
        if rn is None:
            continue
        active, standby, names = [], [], {}
        for p in r.get("picks", []) or []:
            hno = p.get("horse_no")
            if hno is None:
                continue
            try:
                hno = int(hno)
            except (TypeError, ValueError):
                continue
            names[hno] = p.get("horse_name", "")
            if p.get("is_standby"):
                standby.append(hno)
            else:
                active.append(hno)
        field[str(rn)] = {
            "active": sorted(active),
            "standby": sorted(standby),
            "names": names,
        }
    return field


def _snapshot_racecard_field(date_compact: str, races: list) -> None:
    """Append a field snapshot for `date_compact` when the line-up changes."""
    if not date_compact or not races:
        return
    try:
        field = _field_signature(races)
        if not field:
            return
        _RACECARD_SNAP_DIR.mkdir(parents=True, exist_ok=True)
        snap_fp = _RACECARD_SNAP_DIR / f"{date_compact}.json"
        snaps = []
        if snap_fp.exists():
            try:
                snaps = json.loads(snap_fp.read_text(encoding="utf-8"))
            except Exception:
                snaps = []
        # Only append if the active/standby signature changed from the last.
        def _sig(f):
            return {k: (v["active"], v["standby"]) for k, v in f.items()}
        if snaps and _sig(snaps[-1].get("field", {})) == _sig(field):
            return
        snaps.append({"ts": hkt_now().isoformat(timespec="seconds"),
                      "field": field})
        snaps = snaps[-10:]  # keep the last 10 versions
        snap_fp.write_text(json.dumps(snaps, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _detect_field_changes(date_compact: str) -> dict | None:
    """Diff the two most recent field snapshots for a meeting.

    Returns ``{scratched, promoted, since}`` (lists of "R<n> #<no> <name>")
    or ``None`` when there is nothing to report.
    """
    if not date_compact:
        return None
    snap_fp = _RACECARD_SNAP_DIR / f"{date_compact}.json"
    if not snap_fp.exists():
        return None
    try:
        snaps = json.loads(snap_fp.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not snaps or len(snaps) < 2:
        return None
    prev, curr = snaps[-2]["field"], snaps[-1]["field"]
    scratched, promoted = [], []
    for rn, cur in curr.items():
        old = prev.get(rn)
        if not old:
            continue
        old_active = set(old.get("active", []))
        new_active = set(cur.get("active", []))
        old_standby = set(old.get("standby", []))
        names = {**old.get("names", {}), **cur.get("names", {})}
        names = {int(k): v for k, v in names.items()} if names else {}
        for hno in sorted(old_active - new_active):
            scratched.append(f"R{rn} #{hno} {names.get(hno, '')}".strip())
        for hno in sorted(new_active - old_active):
            # A newly-active horse that was previously a standby = promotion.
            tag = "(reserve)" if hno in old_standby else ""
            promoted.append(f"R{rn} #{hno} {names.get(hno, '')} {tag}".strip())
    if not scratched and not promoted:
        return None
    return {"scratched": scratched, "promoted": promoted,
            "since": snaps[-2].get("ts", "")}


def _render_scratch_chip(date_compact: str) -> None:
    """Surface a compact scratchings / reserve-promotion alert."""
    changes = _detect_field_changes(date_compact)
    if not changes:
        return
    n_s = len(changes["scratched"])
    n_p = len(changes["promoted"])
    bits = []
    if n_s:
        bits.append(f"**{n_s}** scratched")
    if n_p:
        bits.append(f"**{n_p}** reserve promoted")
    since = changes.get("since", "")[11:16]
    head = "⚠️ Field changed: " + ", ".join(bits)
    if since:
        head += f" (since {since} HKT)"
    with st.expander(head, expanded=bool(n_s)):
        if changes["scratched"]:
            st.markdown("**Scratched**\n"
                        + "\n".join(f"- {x}" for x in changes["scratched"]))
        if changes["promoted"]:
            st.markdown("**Promoted / added**\n"
                        + "\n".join(f"- {x}" for x in changes["promoted"]))


def _db_results_integrity() -> list:
    """Return ISO dates that have a results JSON on disk but are NOT in hkjc.db."""
    try:
        from db_utils import read_sqlite
    except Exception:
        return []
    results_dir = BASE / "reports"
    if not results_dir.exists():
        return []
    try:
        df = read_sqlite("SELECT DISTINCT race_date FROM results")
        have = {str(d)[:10] for d in df["race_date"].astype(str)}
    except Exception:
        have = set()
    missing = []
    for fp in sorted(results_dir.glob("results_*.json")):
        stem = fp.stem.replace("results_", "")
        if len(stem) != 8 or not stem.isdigit():
            continue
        iso = f"{stem[:4]}-{stem[4:6]}-{stem[6:]}"
        if iso not in have:
            missing.append(iso)
    return missing


def _render_db_integrity_panel() -> None:
    """Surface a DB-integrity check with a one-click sync (#2)."""
    missing = _db_results_integrity()
    if not missing:
        st.caption("🗄️ DB integrity ✓ — every results file is in `hkjc.db`.")
        return
    st.warning(
        f"🗄️ **DB integrity: {len(missing)} meeting(s) missing from `hkjc.db`** "
        f"— {', '.join(missing)}. These won't appear in the Form Guide until synced."
    )
    if st.button("🔄 Sync missing meetings to DB", key="db_integrity_sync",
                 type="primary"):
        n_mtg, n_rows = _auto_sync_results_to_db(verbose=False)
        st.success(f"Synced {n_mtg} meeting(s), {n_rows} rows. Rebuild the "
                   "Form Guide to pick them up.")
        try:
            st.cache_data.clear()
        except Exception:
            pass
        st.rerun()


def _render_meeting_ctx_bar() -> None:
    """Render a thin global context bar showing the active meeting."""
    ctx = st.session_state.get("_meeting_ctx")
    if not ctx or not ctx.get("date"):
        return
    venue = ctx.get("venue") or "—"
    n = ctx.get("n_races") or 0
    st.markdown(
        '<div style="display:flex;gap:14px;align-items:center;font-size:0.82rem;'
        'padding:4px 12px;margin:0 0 8px 0;border-radius:6px;'
        'background:rgba(250,250,250,0.05);'
        'border:1px solid rgba(250,250,250,0.10);color:#cbd5e1;">'
        f'<span>📍 <b>{venue}</b></span>'
        f'<span>🗓 {ctx["date"]}</span>'
        f'<span>🏇 {n} races</span>'
        '<span style="margin-left:auto;opacity:0.6;">active meeting</span>'
        '</div>',
        unsafe_allow_html=True,
    )


def page_overview():

    st.markdown('<div class="page-title">Race Day Insight</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">Race-time cockpit &middot; one race at a time</div>',
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

    # Stash for _render_race_cockpit → Market Pulse panel.
    st.session_state["_rdi_date_compact"] = dstr
    st.session_state["_rdi_venue_code"] = _venue_to_code(
        data.get("meeting_venue", ""))

    # Stash global meeting context (cross-page context bar).
    _set_meeting_ctx(nice_date, data.get("meeting_venue", ""), len(races))

    # Load SARR data for this meeting
    sarr_data = load_sarr_data(dstr)
    sarr_races = sarr_data.get("races", []) if sarr_data else []

    # Merge going from results file (if available) into ET race dicts
    # so _lookup_condition_stats can filter by today's going conditions.
    _res_path = REPORTS / f"results_{dstr}.json"
    if _res_path.exists():
        try:
            _res_data = json.loads(_res_path.read_text(encoding="utf-8"))
            _going_by_race = {r["race_number"]: r.get("going", "")
                              for r in _res_data.get("races", [])}
            for r in races:
                if not r.get("going"):
                    r["going"] = _going_by_race.get(r["race_number"], "")
        except Exception:
            pass


    st.markdown(f"### {data.get('meeting_title', nice_date)}")
    if version:
        st.caption(f"Model {version}  ·  {len(races)} races")

    _render_glossary(key="rdi_glossary")

    # Field-change tracking: snapshot line-up & flag scratchings / promotions.
    _snapshot_racecard_field(dstr, races)
    _render_scratch_chip(dstr)

    # ══════════════════════════════════════════════════════════════════
    # RACE-TIME COCKPIT — top-of-page, single-race focus
    # ══════════════════════════════════════════════════════════════════
    if races:
        rns = [r["race_number"] for r in races]
        # Race selector — button row matching the SARR/ET Race-Day Analysis
        # page (one button per race, current race highlighted as primary).
        if "cockpit_race" not in st.session_state or \
                st.session_state["cockpit_race"] not in rns:
            st.session_state["cockpit_race"] = rns[0]
        _btn_cols = st.columns(len(rns))
        for _i, _rn in enumerate(rns):
            with _btn_cols[_i]:
                _is_active = (st.session_state["cockpit_race"] == _rn)
                if st.button(
                    f"R{_rn}",
                    key=f"rdi_tab_{_rn}",
                    width='stretch',
                    type="primary" if _is_active else "secondary",
                ):
                    st.session_state["cockpit_race"] = _rn
                    st.rerun()
        sel_rn = st.session_state["cockpit_race"]
        sel_race = next((r for r in races if r["race_number"] == sel_rn), None)
        sel_sarr = next((r for r in sarr_races if r["race_number"] == sel_rn), None) \
                   if sarr_races else None

        # Cached factor edges for the meeting; filter to this race.
        fe_window = st.session_state.get("overview_fe_window", "current_season_25_26")
        all_edges = _factor_edges_for_meeting(
            dstr, fe_window,
            str(meeting_info["file"]),
            meeting_info["file"].stat().st_mtime if meeting_info["file"].exists() else 0.0,
            FACTOR_TABLES_PATH.stat().st_mtime if FACTOR_TABLES_PATH.exists() else 0.0,
            6,
        )
        edges_for_race = [e for e in all_edges if e.get("race") == sel_rn]

        bb = _load_blackbook()
        active = _bb_active_lookup(bb)
        trial_index = _load_all_trial_horse_index()

        if sel_race:
            _render_race_cockpit(sel_race, sel_sarr, edges_for_race,
                                 active, trial_index, meeting_info)

    st.markdown("---")

    # ── FUSE model board (always visible — flagship engine) ────────────
    st.markdown("### 🧬 FUSE Model Board")
    st.caption(
        "FUSE = leak-free LightGBM over the full results DB, anchored on live "
        "odds when available. Jan–Jun 2026 walk-forward: **29.9% win / 61.4% "
        "place** on top pick, 27.5% quinella in box-3 — strongest single "
        "engine. ⚡ = positive edge vs market."
    )

    fuse_rep = _load_fuse_report(dstr, _fuse_report_mtime(dstr))
    if not fuse_rep:
        st.info(
            f"FUSE report not found for this meeting. Generate with "
            f"`python fuse_raceday.py --date {dstr}` or re-run the Model "
            f"Analysis pipeline."
        )
    else:
        _fr_races = fuse_rep.get("races", [])
        rows_html = []
        for fr in _fr_races:
            rn = fr.get("race_number")
            picks = (fr.get("picks") or [])[:3]
            chip_bits = []
            for p in picks:
                edge = p.get("edge")
                pos_edge = edge is not None and edge >= 0.02
                col = "#34d399" if p.get("rank") == 1 else "#9cc0ff"
                bolt = (' <span style="color:#fbbf24">⚡</span>'
                        if pos_edge else "")
                odds_s = (f' <span style="opacity:0.55">@{p["win_odds"]:.1f}</span>'
                          if p.get("win_odds") else "")
                chip_bits.append(
                    f'<span style="display:inline-block;margin:2px 6px 2px 0;'
                    f'padding:2px 9px;border-radius:11px;'
                    f'background:rgba(255,255,255,0.05);'
                    f'border:1px solid {col}55;font-size:0.86em">'
                    f'<b style="color:{col}">#{p.get("horse_no","?")}</b> '
                    f'{p.get("horse_name","")} '
                    f'<span style="opacity:0.75">{(p.get("p_win") or 0)*100:.0f}%'
                    f'</span>{odds_s}{bolt}</span>')
            qp = (fr.get("quinella_pairs") or [])[:2]
            q_bits = [
                f'<span style="display:inline-block;margin:2px 6px 2px 0;'
                f'padding:2px 8px;border-radius:10px;'
                f'background:rgba(52,211,153,0.08);'
                f'border:1px solid rgba(52,211,153,0.35);font-size:0.84em">'
                f'{q["a"]}–{q["b"]} <span style="opacity:0.65">'
                f'{q["p"]*100:.1f}% · fair {q["fair_odds"]:.0f}</span></span>'
                for q in qp]
            anchored = ("🟢" if fr.get("odds_anchored")
                        else '<span style="opacity:0.45">⚪</span>')
            rows_html.append(
                f'<tr style="border-bottom:1px solid rgba(128,128,128,0.12)">'
                f'<td style="padding:6px 8px;font-weight:800;'
                f'white-space:nowrap">R{rn} {anchored}</td>'
                f'<td style="padding:6px 8px">{"".join(chip_bits)}</td>'
                f'<td style="padding:6px 8px;white-space:nowrap">'
                f'{"".join(q_bits)}</td>'
                f'</tr>')
        st.markdown(
            '<table style="width:100%;border-collapse:collapse;font-size:0.92em">'
            '<thead><tr style="border-bottom:2px solid rgba(128,128,128,0.3)">'
            '<th style="text-align:left;padding:6px">Race</th>'
            '<th style="text-align:left;padding:6px">FUSE top 3 (win prob)</th>'
            '<th style="text-align:left;padding:6px">Quinella pairs</th>'
            '</tr></thead><tbody>'
            + "".join(rows_html)
            + '</tbody></table>'
            '<div style="margin-top:6px;font-size:0.78em;opacity:0.6">'
            f'🟢 = live-odds anchored · ⚪ = fundamentals only · '
            f'⚡ = model prob ≥ market +2pp · trained on '
            f'{fuse_rep.get("n_train", 0):,} runner-starts · generated '
            f'{fuse_rep.get("generated_at_hkt", "?")} HKT</div>',
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ── Cross-race rollup (collapsible for decluttered race-day view) ──
    show_rollup = st.toggle(
        "Show cross-race rollup (blackbook · mutual picks · factor edges · trials)",
        value=False, key="overview_show_rollup",
    )
    if not show_rollup:
        return

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
                    "horse_no": pick.get("horse_no", ""),
                    "draw": pick.get("draw", ""),
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
                    "horse_no": sm.get("horse_no", ""),
                    "draw": sm.get("draw", ""),
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
            no_chip = (
                f'<span style="background:rgba(96,165,250,0.18);color:#60a5fa;'
                f'padding:1px 7px;border-radius:8px;font-size:0.82em;'
                f'font-weight:700;margin-right:4px">#{bm["horse_no"]}</span>'
                if bm.get("horse_no") not in ("", None) else ""
            )
            draw_chip = (
                f'<span style="background:rgba(167,139,250,0.18);color:#a78bfa;'
                f'padding:1px 7px;border-radius:8px;font-size:0.82em;'
                f'font-weight:700;margin-right:4px">Gate&nbsp;{bm["draw"]}</span>'
                if bm.get("draw") not in ("", None) else ""
            )
            st.markdown(
                f'**R{bm["race"]}** {bm["dist"]}m — '
                f'{no_chip}{draw_chip}'
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


    # ── Section 2: Mutual model top picks (per selected race) ─────────
    st.markdown("### 🤝 Mutual Model Top Picks (ET ∩ SARR)")
    if sarr_races:
        # Race selector — only show races that exist in BOTH reports.
        common_rns = sorted({r["race_number"] for r in races}
                            & {r["race_number"] for r in sarr_races})
        if not common_rns:
            st.caption("No common races between ET and SARR reports.")
        else:
            # Optional 'All' at the end for a holistic view.
            options = list(common_rns) + ["All races"]
            sel = st.radio(
                "Race",
                options=options,
                index=0, horizontal=True,
                format_func=lambda x: f"R{x}" if isinstance(x, int) else x,
                key="overview_mutual_race",
                label_visibility="collapsed",
            )
            selected_rns = common_rns if sel == "All races" else [sel]

            mutual_rows = []
            for et_race in races:
                rn = et_race["race_number"]
                if rn not in selected_rns:
                    continue
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
                if sel == "All races":
                    st.caption("No mutual top-4 picks between ET and SARR.")
                else:
                    st.caption(f"No mutual top-4 picks between ET and SARR for R{sel}.")
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
                    "horse_no": pick.get("horse_no", ""),
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

            # Use the shared sentiment helper so Overview matches Form Guide
            flag, reasons = _trial_sentiment(latest)
            if not flag:
                continue

            ch = card_horses[horse.upper().strip()]
            trial_hits.append({
                "race": ch["race"],
                "dist": ch["dist"],
                "horse": horse,
                "horse_no": ch.get("horse_no", ""),
                "rank": ch["rank"],
                "wp": ch["wp"],
                "flag": flag,
                "detail": "; ".join(reasons),
                "date": latest["date"],
                "fp": latest.get("running_positions", [])[-1] if latest.get("running_positions") else None,
                "n": latest.get("n_horses", 0),
                "dist_m": latest.get("distance_m", 0),
            })

        # Sort: ++ first (by race), then +, then -. Within each tier by race.
        _TIER_ORDER = {"++": 0, "+": 1, "-": 2}
        trial_hits.sort(key=lambda x: (_TIER_ORDER.get(x["flag"], 9), x["race"]))

        if trial_hits:
            from collections import defaultdict as _dd
            grouped = _dd(list)
            for th in trial_hits:
                grouped[th["flag"]].append(th)

            _TIER_META = {
                "++": ("🟢 Strong Positives",   "#22c55e", "rgba(34,197,94,0.10)"),
                "+":  ("🟡 Positive",            "#86efac", "rgba(134,239,172,0.08)"),
                "-":  ("🔴 Negative",            "#ef4444", "rgba(239,68,68,0.08)"),
            }

            # Tier counts header
            count_cols = st.columns(3)
            for i, tier in enumerate(("++", "+", "-")):
                label, colour, _bg = _TIER_META[tier]
                n_items = len(grouped.get(tier, []))
                count_cols[i].markdown(
                    f'<div style="text-align:center;padding:6px;'
                    f'border:1px solid {colour};border-radius:6px;'
                    f'background:{_TIER_META[tier][2]}">'
                    f'<div style="font-size:0.85em;opacity:0.85">{label}</div>'
                    f'<div style="font-size:1.4em;font-weight:800;color:{colour}">{n_items}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

            # One compact block per tier — table-like rows
            for tier in ("++", "+", "-"):
                items = grouped.get(tier, [])
                if not items:
                    continue
                label, colour, bg = _TIER_META[tier]
                rows_html = []
                for th in items:
                    rk_str = f"#{th['rank']}" if th["rank"] else "—"
                    hno = f"({th['horse_no']})" if th.get("horse_no") else ""
                    wp = f"{th['wp']:.0f}%" if th["wp"] else "—"
                    fp_str = f"{th['fp']}/{th['n']}" if th["fp"] else "—"
                    dist_m_str = f"{th['dist_m']}m" if th.get("dist_m") else ""
                    rows_html.append(
                        f'<tr>'
                        f'<td style="padding:3px 8px;color:{colour};font-weight:700">R{th["race"]}</td>'
                        f'<td style="padding:3px 8px;font-weight:700">{th["horse"]}</td>'
                        f'<td style="padding:3px 8px;opacity:0.7;font-size:0.85em">{hno}</td>'
                        f'<td style="padding:3px 8px;text-align:center">{rk_str}</td>'
                        f'<td style="padding:3px 8px;text-align:center;opacity:0.85">{wp}</td>'
                        f'<td style="padding:3px 8px;opacity:0.7;font-size:0.85em">Last trial {th["date"][5:]} {dist_m_str} · {fp_str}</td>'
                        f'<td style="padding:3px 8px;opacity:0.7;font-size:0.85em;font-style:italic">{th["detail"]}</td>'
                        f'</tr>'
                    )
                st.markdown(
                    f'<div style="border-left:3px solid {colour};'
                    f'background:{bg};border-radius:4px;'
                    f'margin-bottom:10px;padding:6px 4px">'
                    f'<div style="padding:2px 10px 4px 10px;'
                    f'color:{colour};font-weight:700;font-size:0.9em">{label} ({len(items)})</div>'
                    f'<table style="width:100%;border-collapse:collapse;font-size:0.9em">'
                    + "".join(rows_html) + '</table></div>',
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
        st.dataframe(pd.DataFrame(bt_rows), width='stretch', hide_index=True)
    else:
        st.caption("No backtests available yet.")


# ══════════════════════════════════════════════════════════════════════════════
# Main page — Race Day view
# ══════════════════════════════════════════════════════════════════════════════

def page_race_day(selected):
    if selected is None:
        st.markdown('<div class="page-title">Model Analysis</div>', unsafe_allow_html=True)
        st.info("No meetings available. Use the sidebar to run your first analysis.")
        return

    _maybe_auto_sync_results("race_day")

    # Racecard-only meeting (analysis pending) — selected['file'] is None.
    # Show a clear placeholder instead of crashing on load_meeting_data().
    if selected.get("status") == "pending" or not selected.get("file"):
        st.markdown(
            f'<div class="page-title">Model Analysis</div>'
            f'<div class="page-subtitle">{selected.get("title", "")}</div>',
            unsafe_allow_html=True,
        )
        ds = selected.get("date_str", "")
        iso = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}" if len(ds) == 8 else ds
        st.warning(
            f"⚠️ Racecard for **{iso}** is scraped but the ET (v4.4) model "
            "analysis JSON has not been generated yet (or the last pipeline "
            "run failed at step [2/3] SARR or [3/3] ET).\n\n"
            "Click **[ RUN ANALYSIS ]** in the sidebar with this date selected "
            "to (re)compute the model report. If the scrape succeeds but the "
            "model step fails, expand the pipeline output to read the error."
        )
        rc_xlsx = BASE / "racecards" / f"racecard_{ds}.xlsx"
        if rc_xlsx.exists():
            try:
                size_kb = rc_xlsx.stat().st_size / 1024
                st.caption(
                    f"Racecard file: `{rc_xlsx.name}` — "
                    f"{size_kb:.0f} KB · {selected.get('n_races', 0)} races"
                )
            except OSError:
                pass
        return

    data = load_meeting_data(selected["file"])

    # Stash meeting venue for render_speed_map → _render_pace_research_panel.
    # race_course field is the rail/track config ("A", "B", etc.), NOT venue.
    st.session_state["_rd_meeting_venue"] = str(
        data.get("meeting_venue") or data.get("meeting_title") or ""
    )

    _date_match = re.search(r"(\d{8})", str(selected["file"]))
    date_str = _date_match.group(1) if _date_match else ""
    vet_lookup = _load_vet_lookup(date_str) if date_str else {}

    # Stash global meeting context (cross-page context bar).
    _ctx_iso = (f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
                if len(date_str) == 8 else date_str)
    _set_meeting_ctx(_ctx_iso, data.get("meeting_venue", ""),
                     len(data.get("races", [])))

    # ── DB freshness banner: results scraped but not yet in master DB ─────
    # The model analysis reads from hkjc.db. If a meeting was scraped but the
    # DB-append step never ran, those results silently vanish from form lines.
    try:
        _pending = _db_pending_meetings()
    except Exception:
        _pending = []
    if _pending:
        _pb1, _pb2 = st.columns([3, 1])
        with _pb1:
            _shown = ", ".join(_pending[-4:]) + (" …" if len(_pending) > 4 else "")
            st.warning(
                f"⚠️ **Master DB is {len(_pending)} meeting(s) behind.** "
                f"Scraped but not yet synced: {_shown}. Form lines & pace for "
                "these dates won't appear in analysis until you sync."
            )
        with _pb2:
            if st.button("🔄 Sync DB now", key="rd_sync_db_banner",
                         type="primary", width='stretch'):
                try:
                    from db_utils import append_results_to_db as _ap
                    _tot = 0
                    with st.spinner("Appending results + rebuilding caches …"):
                        for _iso in _pending:
                            _dc = _iso.replace("-", "")
                            _fp = REPORTS / f"results_{_dc}.json"
                            if _fp.exists():
                                _tot += int(_ap(_fp, verbose=False) or 0)
                        _msgs = _rebuild_caches_after_sync()
                    try:
                        st.cache_data.clear()
                    except Exception:
                        pass
                    st.success(
                        f"Synced {len(_pending)} meeting(s), {_tot} rows. "
                        + " · ".join(_msgs))
                    st.rerun()
                except Exception as _e:
                    st.error(f"Sync failed: {_e}")

    bb = _load_blackbook()
    _bb_expire_stale(bb)
    bb_lookup = _bb_active_lookup(bb)

    et_races = data.get("races", [])

    # ── Load SARR data for same date ─────────────────────────────────────
    sarr_data = load_sarr_data(date_str) if date_str else None
    sarr_races = sarr_data.get("races", []) if sarr_data else []
    sarr_available = bool(sarr_races)

    # ── Post-race results lookup (for the per-pick Fin column) ───────────
    _rd_results_json = _load_results_json(date_str) if date_str else None

    def _rd_result_lk(rn: int) -> dict:
        """Return {horse_no: finishing_position_str} for race `rn`, or {}."""
        if not _rd_results_json:
            return {}
        for rr in _rd_results_json.get("races", []):
            try:
                if int(rr.get("race_number", 0) or 0) != int(rn):
                    continue
            except (TypeError, ValueError):
                continue
            out = {}
            for ru in rr.get("runners", []) or []:
                hn, pos = ru.get("horse_no"), ru.get("place")
                if hn is not None and pos not in (None, ""):
                    try:
                        out[int(hn)] = str(pos)
                    except (TypeError, ValueError):
                        pass
            return out
        return {}

    # ── Page header ──────────────────────────────────────────────────────
    st.markdown(
        f'<div class="page-title">{data["meeting_title"]}</div>'
        f'<div class="page-subtitle">Model {data.get("model_version", "v3.4.8")} '
        f'&nbsp;·&nbsp; Generated {data.get("generated_at", "")[:16]}</div>',
        unsafe_allow_html=True,
    )

    # ── One-click race-day control (ALWAYS available) ────────────────────
    # The stale-card banner below only appears when the card is >12 h old AND
    # the meeting is today, so it is hidden exactly when a *late* scratching /
    # reserve swap lands on a freshly-scraped card. This control is always
    # present so the field can be refreshed on demand, and doubles as the
    # "run everything when the card is out" one-click button: it re-scrapes the
    # racecard (dropping scratched horses, pulling in promoted reserves /
    # replacements), then rebuilds SARR + ET + form guide on the fresh field.
    if date_str:
        _iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        _rc = BASE / "racecards" / f"racecard_{date_str}.xlsx"
        _age_txt = "no racecard on disk"
        if _rc.exists():
            try:
                _age_h = (hkt_now() - hkt_from_ts(_rc.stat().st_mtime)
                          ).total_seconds() / 3600.0
                _age_txt = (f"card scraped {int(_age_h)} h ago"
                            if _age_h >= 1 else "card scraped <1 h ago")
            except OSError:
                pass
        # ── Consolidated readiness strip (single status line) ──────────
        _odds_dir = BASE / "cache" / "live_odds" / date_str
        _odds_n = len(list(_odds_dir.glob("*_R*.json"))) if _odds_dir.exists() else 0
        _strip = [
            f"🗓 **{_iso}**",
            _age_txt,
            ("SARR ✓" if sarr_available else "SARR ✗"),
            ("ET ✓" if et_races else "ET ✗"),
            (f"live odds ✓ ({_odds_n})" if _odds_n else "live odds ✗"),
        ]
        st.caption(" &nbsp;·&nbsp; ".join(_strip)
                   + " &nbsp;·&nbsp; *open controls below to re-scrape / run.*")
        # Field-change tracking: snapshot the current line-up and flag any
        # scratchings / reserve promotions versus the previous version.
        _snapshot_racecard_field(date_str, et_races or data.get("races", []))
        _render_scratch_chip(date_str)
        with st.expander("🏁 Race-day controls — re-scrape & run core analysis",
                         expanded=False):
            rc1, rc2 = st.columns([3, 2])
            with rc1:
                st.caption(
                    f"**{_iso}** · {_age_txt}. Use this whenever the card is "
                    f"out or a late scratching / reserve swap is announced — it "
                    "re-scrapes the live field and rebuilds SARR + ET + form "
                    "guide in one pass. Refresh FUSE separately after live odds "
                    "are current."
                )
                _gt = st.text_input("Turf going", value=str(
                    st.session_state.get("going_turf", "Good")),
                    key=f"rd_ctl_gt_{date_str}")
                _ga = st.text_input("AWT going", value=str(
                    st.session_state.get("going_awt", "Good")),
                    key=f"rd_ctl_ga_{date_str}")
            with rc2:
                if st.button("🔄 Re-scrape & run core analysis",
                             key=f"rd_runall_{date_str}",
                             type="primary", width='stretch'):
                    try:
                        st.cache_data.clear()
                    except Exception:
                        pass
                    run_pipeline(_iso, no_cache=True,
                                 going_turf=_gt or "Good",
                                 going_awt=_ga or "Good",
                                 skip_scrape=False,
                                 run_fuse=False)
                    st.rerun()
                st.caption("Takes a few minutes. Picks below refresh "
                           "automatically when it finishes.")

    # ── Stale-racecard banner ────────────────────────────────────────────
    # If today's meeting card was scraped >12h ago, late scratchings (e.g.
    # reserves replacing scratched horses) may NOT have propagated. Offer a
    # one-click full pre-race refresh (re-scrape racecard + form guide +
    # SARR + ET model) so picks reflect the current field.
    try:
        import datetime as _dt
        if date_str:
            _rc_path = BASE / "racecards" / f"racecard_{date_str}.xlsx"
            _today = _dt.date.today()
            _meeting_date = _dt.date(int(date_str[:4]), int(date_str[4:6]),
                                     int(date_str[6:]))
            _is_today = (_meeting_date == _today)
            if _rc_path.exists():
                _rc_age_h = (hkt_now() -
                             hkt_from_ts(_rc_path.stat().st_mtime)
                             ).total_seconds() / 3600.0
                # Show banner ONLY when meeting is today AND card is stale.
                # Past meetings keep the original card; future cards fresh.
                if _is_today and _rc_age_h > 12:
                    _hours_ago = int(_rc_age_h)
                    sb1, sb2 = st.columns([3, 1])
                    with sb1:
                        st.warning(
                            f"⚠ **Race card last scraped {_hours_ago} h ago.** "
                            "Late scratchings (reserves replacing "
                            "scratched horses) may NOT be reflected in the "
                            "model picks below. Click → to re-scrape and "
                            "rebuild SARR + ET on the fresh field."
                        )
                    with sb2:
                        if st.button(
                            "🔄 Refresh meeting",
                            key=f"rd_refresh_{date_str}",
                            type="primary",
                            width='stretch',
                        ):
                            iso = (f"{date_str[:4]}-{date_str[4:6]}-"
                                   f"{date_str[6:]}")
                            try:
                                st.cache_data.clear()
                            except Exception:
                                pass
                            run_pipeline(iso, no_cache=True,
                                         going_turf="Good", going_awt="Good",
                                         skip_scrape=False)
                            st.rerun()
    except (OSError, ValueError):
        pass

    # ── SARR missing banner: one-click regeneration ──────────────────────
    # When ET succeeded but SARR JSON is missing, offer a focused button that
    # invokes ONLY the SARR script for the current date (no re-scrape, no
    # re-ET) — fastest path to unblock the SARR toggle.
    if not sarr_available and date_str:
        _sarr_col1, _sarr_col2 = st.columns([3, 1])
        with _sarr_col1:
            st.warning(
                f"⚠️ SARR analysis not available for {date_str}. "
                "ET is shown below. Click the button to generate SARR now."
            )
        with _sarr_col2:
            if st.button("▶ Run SARR now",
                          key=f"run_sarr_now_{date_str}",
                          width='stretch',
                          type="primary"):
                iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
                script = BASE / "sarr_raceday.py"
                if not script.exists():
                    st.error(f"sarr_raceday.py not found at {script}")
                else:
                    env = os.environ.copy()
                    env["PYTHONIOENCODING"] = "utf-8"
                    with st.spinner(f"Running SARR for {iso}..."):
                        try:
                            r = subprocess.run(
                                [PYTHON, str(script), "--date", iso],
                                env=env, cwd=str(BASE),
                                capture_output=True, text=True,
                                encoding="utf-8", timeout=600,
                            )
                            out_path = REPORTS / f"race_day_report_{date_str}_SARR.json"
                            if r.returncode == 0 and out_path.exists():
                                st.success(f"✓ SARR generated for {iso}.")
                                try:
                                    st.cache_data.clear()
                                except Exception:
                                    pass
                                st.rerun()
                            else:
                                st.error(
                                    f"SARR failed (exit {r.returncode}). "
                                    f"Output:\n{(r.stderr or r.stdout)[-2000:]}"
                                )
                        except (OSError, subprocess.TimeoutExpired) as e:
                            st.error(f"SARR run errored: {e}")

    # ── Model toggle state (UI rendered below, near the race-tab row) ───
    # The actual radio is rendered just above the race tabs so the model
    # selector lives next to the search bar and tabs (one toggle, no dupes).
    if sarr_available:
        if "rd_model_toggle" not in st.session_state:
            st.session_state["rd_model_toggle"] = "SARR (Sectional-Anchored)"
        use_sarr = st.session_state["rd_model_toggle"].startswith("SARR")
    else:
        use_sarr = False

    active_races = sarr_races if use_sarr else et_races

    # ── Compact summary line (replaces the old 4 metric tiles) ───────────
    # The four metric tiles pushed the actual picks below the fold; collapse
    # them into one caption so the Top Picks table sits high on the page.
    total_runners = sum(r.get("runners", 0) for r in active_races)
    total_projected = sum(r.get("projected", 0) for r in active_races)
    _gen = (data.get("generated_at", "") or "")[:16].replace("T", " ")
    st.caption(
        f"🏟 **{data.get('meeting_venue', '?')}** · {len(active_races)} races · "
        f"{total_runners} runners · {total_projected} projected · "
        f"model {data.get('model_version', 'v3.4.8')}"
        + (f" · generated {_gen} HKT" if _gen else "")
    )

    st.markdown('<hr class="term-divider">', unsafe_allow_html=True)

    # ── Top Picks Summary table (expanded — primary content) ───────────
    with st.expander("**🏆 TOP PICKS SUMMARY**", expanded=True):
        # Build a quick lookup of SARR top-1 by race # so we can flag
        # ET/SARR agreement (★) in the ET summary rows. Backtest shows
        # races where both models pick the same horse have a 33% Win
        # rate vs ~23% baseline — strongest single confidence signal.
        _sarr_top1_by_race: dict = {}
        if sarr_available:
            for _r in sarr_races:
                _picks = _r.get("picks") or []
                if _picks:
                    _sarr_top1_by_race[_r.get("race_number")] = (
                        _picks[0].get("horse_no")
                        or _picks[0].get("horse_name")
                    )

        summary_rows = []
        for race in active_races:
            picks = race.get("picks", [])
            if not picks:
                continue
            top = picks[0]
            cls_str = f"C{race.get('race_class', '')}" if race.get('race_class') else "Grp"
            surface = "AWT" if race.get("is_awt") else "Turf"
            # Agreement marker — only meaningful when both models exist.
            _top_id = top.get("horse_no") or top.get("horse_name")
            _sarr_id = _sarr_top1_by_race.get(race.get("race_number"))
            _agree = bool(_sarr_id) and (_sarr_id == _top_id)
            _name = top.get("horse_name", "?")
            _name_disp = f"★ {_name}" if _agree else _name
            if use_sarr:
                summary_rows.append({
                    "Race": f"R{race['race_number']}",
                    "Dist": f"{race.get('distance', '?')}m",
                    "Surf": surface,
                    "Cls": cls_str,
                    "Top Pick": _name_disp,
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
                    "Top Pick": _name_disp,
                    "Proj (s)": f"{top['projected_time']:.2f}",
                    "Win%": f"{top['win_prob']:.0f}%",
                    "2nd": picks[1]["horse_name"] if len(picks) > 1 else "—",
                    "3rd": picks[2]["horse_name"] if len(picks) > 2 else "—",
                    "4th": picks[3]["horse_name"] if len(picks) > 3 else "—",
                })

        if summary_rows:
            st.dataframe(pd.DataFrame(summary_rows), width='stretch', hide_index=True)
            if sarr_available:
                _n_agree = sum(1 for r in summary_rows
                               if r.get("Top Pick", "").startswith("★"))
                if _n_agree:
                    st.caption(
                        f"★ AGREEMENT — ET top-1 = SARR top-1 in **{_n_agree}** "
                        f"race(s). Historically these win at ~33% (vs ~23% baseline)."
                    )

    # ── Market Pulse alerts banner (meeting-level) ──────────────────────
    try:
        _venue_code_top = _venue_to_code(data.get("meeting_venue", ""))
        if date_str and _venue_code_top:
            _alerts = _compute_meeting_alerts(
                date_str, _venue_code_top, active_races)
            if _alerts:
                _warn = [a for a in _alerts if a["severity"] == "WARN"]
                _rev = [a for a in _alerts if a["severity"] == "REVIEW"]
                _info = [a for a in _alerts if a["severity"] == "INFO"]
                _hdr_bits = []
                if _warn: _hdr_bits.append(f"🔴 {len(_warn)}")
                if _rev: _hdr_bits.append(f"🟡 {len(_rev)}")
                if _info: _hdr_bits.append(f"⚪ {len(_info)}")
                with st.expander(
                    f"📡 Market alerts — { ' · '.join(_hdr_bits) }",
                    expanded=False,
                ):
                    _icon = {"WARN": "🔴", "REVIEW": "🟡", "INFO": "⚪"}
                    _lines = [
                        f"- {_icon.get(a['severity'], '·')} "
                        f"**R{a['race']}** · {a['msg']}"
                        for a in _alerts[:12]
                    ]
                    st.markdown("\n".join(_lines))
                    if len(_alerts) > 12:
                        st.caption(f"…and {len(_alerts) - 12} more.")
                    st.caption(
                        "WARN = top-3 pick drifting ≥+40% · "
                        "REVIEW = outsider steaming ≤-40% or top-1 drifting ≥+25%."
                    )
            elif date_str and _venue_code_top:
                _has_any = bool((BASE / "cache" / "live_odds" / date_str).exists()
                                and any((BASE / "cache" / "live_odds" / date_str).iterdir()))
                if _has_any:
                    st.caption(
                        "📡 Market alerts: no significant moves yet "
                        "(needs ≥2 snapshots per race). "
                        "Re-run scraper later from **Live Odds** page."
                    )
    except Exception as _alert_err:
        st.caption(f"_Market alerts unavailable: {_alert_err}_")

    st.markdown('<hr class="term-divider">', unsafe_allow_html=True)

    # ── Cross-model agreement set (ET top-1 == SARR top-1) ───────────────
    # Flag high-confidence races on the tab row (★) and verdict banner.
    # Backtest: races where both models agree win at ~33% vs ~23% baseline.
    _agree_set: set = set()
    if sarr_available:
        _et_top1: dict = {}
        for _r in et_races:
            _p = _r.get("picks") or []
            if _p:
                _et_top1[_r.get("race_number")] = (
                    _p[0].get("horse_no") or _p[0].get("horse_name"))
        for _r in sarr_races:
            _p = _r.get("picks") or []
            if _p:
                _sid = _p[0].get("horse_no") or _p[0].get("horse_name")
                if _et_top1.get(_r.get("race_number")) == _sid:
                    _agree_set.add(_r.get("race_number"))

    # ── Race tab selector ────────────────────────────────────────────────
    race_numbers = [r["race_number"] for r in active_races]
    if "rd_active_race" not in st.session_state:
        st.session_state["rd_active_race"] = race_numbers[0] if race_numbers else None
    # Heal active race if the toggle changed (SARR/ET may differ in coverage)
    if race_numbers and st.session_state["rd_active_race"] not in race_numbers:
        st.session_state["rd_active_race"] = race_numbers[0]

    # ── Horse search box (above tabs) ────────────────────────────────────
    # Use ONLY the widget key so Streamlit doesn't complain about
    # value= conflicting with session_state.
    rd_search = st.text_input(
        "🔍 Find horse",
        key="rd_search_input",
        placeholder="Type a horse name to jump to their race…",
        label_visibility="collapsed",
    )
    rd_q = (rd_search or "").strip().upper()
    if rd_q:
        match_races = []
        for r in active_races:
            # SARR JSON has speed_map=None — guard against AttributeError
            _smap = r.get("speed_map") or {}
            _grid = _smap.get("grid", []) if isinstance(_smap, dict) else []
            for pool in (r.get("picks", []) or [], _grid):
                if any(rd_q in (p.get("horse_name", "") or "").upper() for p in pool):
                    match_races.append(r["race_number"])
                    break
        if not match_races:
            st.warning(f"No horse matching “{rd_search}” on this card.")
        else:
            # Auto-jump only when the query has changed since last render
            # AND the current active race isn't already a match.
            last_q = st.session_state.get("_rd_search_last_q", "")
            current_rn = st.session_state.get("rd_active_race")
            if last_q != rd_q and current_rn not in match_races:
                st.session_state["rd_active_race"] = match_races[0]
                st.session_state["_rd_search_last_q"] = rd_q
                st.rerun()
            st.session_state["_rd_search_last_q"] = rd_q
            chips = " ".join(
                f'<span style="background:rgba(34,197,94,0.15);color:#22c55e;'
                f'padding:2px 8px;border-radius:10px;font-size:0.85em;'
                f'font-weight:700;margin-right:4px">R{rn}</span>'
                for rn in match_races
            )
            st.markdown(
                f'<div style="margin:-4px 0 8px 0">Found in: {chips}</div>',
                unsafe_allow_html=True,
            )
    else:
        st.session_state.pop("_rd_search_last_q", None)

    # ── Compact model toggle right above the race-tab row (mirror) ───────
    # So users don't need to scroll up to switch ET / SARR. Label sits on
    # the same horizontal line as the radio options. The label and radio
    # share one wide column so "MODEL" doesn't wrap to "MO/DEL" and the
    # two long radio labels stay on one row.
    if sarr_available:
        def _sync_inline_toggle():
            choice = st.session_state.get("rd_model_toggle_inline")
            if choice:
                st.session_state["rd_model_toggle"] = choice
        _ilbl_col, _irad_col, _spacer = st.columns([0.6, 4.0, 0.5])
        with _ilbl_col:
            st.markdown(
                "<div style='padding-top:6px;font-weight:700;"
                "color:#a3b3c7;font-size:0.85em;white-space:nowrap'>MODEL</div>",
                unsafe_allow_html=True,
            )
        with _irad_col:
            st.radio(
                "Model (inline)",
                ["SARR (Sectional-Anchored)", "ET (Expected Time)"],
                horizontal=True,
                key="rd_model_toggle_inline",
                label_visibility="collapsed",
                on_change=_sync_inline_toggle,
                index=(0 if use_sarr else 1),
            )

    # Render tab row — made sticky so the R1…Rn selector stays visible while
    # scrolling a long race card. The marker span lets CSS target *only* the
    # immediately-following horizontal block (the tab buttons).
    st.markdown(
        """
        <span id="rd-tabbar-anchor"></span>
        <style>
        .element-container:has(#rd-tabbar-anchor) + div[data-testid="stHorizontalBlock"] {
            position: sticky;
            top: 2.875rem;
            z-index: 999;
            background: var(--background-color, #0e1117);
            padding: 4px 0 6px 0;
            border-bottom: 1px solid rgba(250,250,250,0.12);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    tab_cols = st.columns(len(race_numbers))
    for i, rn in enumerate(race_numbers):
        with tab_cols[i]:
            is_active = st.session_state["rd_active_race"] == rn
            btn_label = f"R{rn}★" if rn in _agree_set else f"R{rn}"
            if st.button(btn_label, key=f"rd_tab_{rn}",
                         width='stretch',
                         type="primary" if is_active else "secondary"):
                st.session_state["rd_active_race"] = rn
                st.session_state.pop(f"rd_full_{rn}", None)
                st.session_state.pop(f"rd_sarr_full_{rn}", None)
                st.rerun()

    if _agree_set:
        st.caption("★ on a race tab = ET & SARR agree on the top pick "
                   "(historically ~33% win rate).")

    # ── Render selected race ─────────────────────────────────────────────
    active_rn = st.session_state["rd_active_race"]
    if active_rn:
        # One-line verdict banner — the "what do I do here" summary so the
        # key call sits above the dense stats table.
        _vr = next((r for r in active_races
                    if r["race_number"] == active_rn), None)
        _vp = (_vr.get("picks") or []) if _vr else []
        if _vp:
            _t = _vp[0]
            _vb = [f"**R{active_rn}** · {_vr.get('distance', '?')}m · "
                   f"{'AWT' if _vr.get('is_awt') else 'Turf'}"]
            if _vr.get("pace"):
                _vb.append(f"{_vr.get('pace')} pace")
            _tn = _t.get("horse_name", "?")
            _tno = _t.get("horse_no", "?")
            if use_sarr:
                _vb.append(
                    f"Top **#{_tno} {_tn}** (SARR {_t.get('sarr', 0):+.3f})")
            else:
                _vb.append(
                    f"Top **#{_tno} {_tn}** (Win {_t.get('win_prob', 0):.0f}%)")
            if active_rn in _agree_set:
                _vb.append("★ both models agree (~33% win)")
            st.info(" · ".join(_vb))

        # ── Per-race highlights strip: stable changes + vet-on-last-start ─
        try:
            _hl_fg = _load_form_guide_cache(_ctx_iso) if _ctx_iso else None
            _hl_vet = _load_vet_full_lookup(date_str) if date_str else {}
            _render_raceday_highlights(_vr or {}, date_str, _hl_fg, _hl_vet)
        except Exception as _hl_err:
            st.caption(f"_Highlights unavailable: {_hl_err}_")

        # ── Cross-engine confluence: mutual/ensemble picks + rating cycle ─
        # Surfaces the model-agreed picks, ET/SARR ensemble top-3 and the
        # rating-cycle flags inline so studying a race no longer requires
        # switching to the Race Day Insight tab.
        try:
            _render_model_confluence(_vr or {}, date_str.replace("-", ""))
        except Exception as _cf_err:
            st.caption(f"_Confluence unavailable: {_cf_err}_")

        if use_sarr:
            sarr_race = next((r for r in sarr_races if r["race_number"] == active_rn), None)
            et_race = next((r for r in et_races if r["race_number"] == active_rn), None)
            if sarr_race:
                render_sarr_race_card(sarr_race, et_race=et_race, bb_lookup=bb_lookup,
                                      result_lookup=_rd_result_lk(active_rn))
        else:
            race = next((r for r in et_races if r["race_number"] == active_rn), None)
            if race:
                _odds_lk = _market_implied_winpct(
                    date_str.replace("-", ""),
                    _venue_to_code(data.get("meeting_venue", "")),
                    active_rn,
                )
                render_race_card(race, vet_lookup=vet_lookup, show_top=4,
                                 bb_lookup=bb_lookup, odds_lookup=_odds_lk,
                                 result_lookup=_rd_result_lk(active_rn))

        # ── Notable head-to-head + Form Screen (reinstated per race) ─────
        try:
            if date_str:
                _render_form_screen_panel(_vr or {}, date_str)
        except Exception as _fs_err:
            st.caption(f"_Form Screen unavailable: {_fs_err}_")

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
        st.dataframe(pd.DataFrame(race_rows), width='stretch', hide_index=True)

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
        st.dataframe(pd.DataFrame(fi_rows), width='stretch', hide_index=True)

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
            st.dataframe(pd.DataFrame(h_rows), width='stretch', hide_index=True)


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
                st.dataframe(pdf, width='stretch', hide_index=True)

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
            st.dataframe(pd.DataFrame(tier_rows), width='stretch', hide_index=True)

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
            st.dataframe(pd.DataFrame(rows_cm), width='stretch', hide_index=True)

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

def _load_unified_backtest(label: str) -> dict | None:
    """Load a unified backtest JSON by label (e.g. 'all', 'last7', 'month-2026-04',
    or a YYYYMMDD date_compact for a per-meeting file)."""
    if re.fullmatch(r"\d{8}", label):
        path = REPORTS / f"backtest_unified_{label}.json"
    else:
        path = REPORTS / f"backtest_unified_{label}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _list_unified_backtests() -> dict:
    """Return {'meeting': {dc: data}, 'window': {label: data}, 'month': {ym: data}}."""
    out = {"meeting": {}, "window": {}, "month": {}}
    for p in REPORTS.glob("backtest_unified_*.json"):
        label = p.stem[len("backtest_unified_"):]
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if re.fullmatch(r"\d{8}", label):
            out["meeting"][label] = data
        elif label.startswith("month-"):
            out["month"][label[len("month-"):]] = data
        else:
            out["window"][label] = data
    return out


def _fmt_pct_or_dash(v) -> str:
    return f"{v*100:.1f}%" if isinstance(v, (int, float)) else "—"


def _fmt_signed_pct(v) -> str:
    return f"{v*100:+.1f}%" if isinstance(v, (int, float)) else "—"


def _fmt_secs(v) -> str:
    if isinstance(v, (int, float)):
        return f"{v:+.2f}s"
    return "—"


def _agreement_chip_html(agreed: bool) -> str:
    if agreed:
        return ('<span style="background:#1f6f3a;color:#dff5e5;padding:2px 8px;'
                'border-radius:10px;font-size:0.8em;font-weight:600;">★ AGREE</span>')
    return ('<span style="background:#5a3030;color:#f5d5d5;padding:2px 8px;'
            'border-radius:10px;font-size:0.8em;">SPLIT</span>')


def _render_unified_overview(data: dict, prefix: str = ""):
    """Headline ET / SARR / Market comparison + agreement signal."""
    s = data.get("summary") or data
    n = data.get("n_races") or s.get("n_races", 0)
    et = s.get("et", {}) or {}
    sa = s.get("sa", {}) or {}
    mk = s.get("mk", {}) or {}
    agree = s.get("agree_top1", {}) or {}
    union = s.get("union_top3_winner_coverage")

    if not n:
        st.info("No backtestable races in this period.")
        return

    period = data.get("period") or data.get("date") or ""
    n_meets = data.get("n_meetings", 1)
    st.markdown(f"**Sample:** {n_meets} meeting{'s' if n_meets != 1 else ''} · "
                f"{n} races · {period}")

    st.markdown("##### Top-1 Pick Performance")
    rows = [
        {"Model": "ET (v4.4)",
         "Top-1 Win": _fmt_pct_or_dash(et.get("top1_win")),
         "Top-1 Place": _fmt_pct_or_dash(et.get("top1_plc")),
         "Top-3 Has Winner": _fmt_pct_or_dash(et.get("top3_has_w")),
         "Avg Top-3 Overlap": (f"{et['avg_top3_overlap']:.2f}/3"
                                if et.get("avg_top3_overlap") is not None else "—"),
         "Rank ρ": (f"{s.get('et_rank_corr_avg'):.3f}"
                    if s.get("et_rank_corr_avg") is not None else "—")},
        {"Model": "SARR",
         "Top-1 Win": _fmt_pct_or_dash(sa.get("top1_win")),
         "Top-1 Place": _fmt_pct_or_dash(sa.get("top1_plc")),
         "Top-3 Has Winner": _fmt_pct_or_dash(sa.get("top3_has_w")),
         "Avg Top-3 Overlap": (f"{sa['avg_top3_overlap']:.2f}/3"
                                if sa.get("avg_top3_overlap") is not None else "—"),
         "Rank ρ": (f"{s.get('sa_rank_corr_avg'):.3f}"
                    if s.get("sa_rank_corr_avg") is not None else "—")},
        {"Model": "Market fav",
         "Top-1 Win": _fmt_pct_or_dash(mk.get("top1_win")),
         "Top-1 Place": _fmt_pct_or_dash(mk.get("top1_plc")),
         "Top-3 Has Winner": _fmt_pct_or_dash(mk.get("top3_has_w")),
         "Avg Top-3 Overlap": (f"{mk['avg_top3_overlap']:.2f}/3"
                                if mk.get("avg_top3_overlap") is not None else "—"),
         "Rank ρ": "—"},
    ]
    st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

    st.markdown("##### Agreement Signal")
    if agree.get("n"):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Races where ET=SARR top-1",
                  f"{agree.get('n', 0)}/{n}",
                  f"{(agree.get('rate') or 0)*100:.0f}% rate")
        c2.metric("Win rate when agree",
                  _fmt_pct_or_dash(agree.get("win_rate")),
                  f"vs ET solo {(et.get('top1_win') or 0)*100:.1f}%")
        c3.metric("Place rate when agree",
                  _fmt_pct_or_dash(agree.get("place_rate")),
                  f"vs ET solo {(et.get('top1_plc') or 0)*100:.1f}%")
        c4.metric("ET ∪ SARR top-3 covers winner",
                  _fmt_pct_or_dash(union),
                  f"vs ET solo {(et.get('top3_has_w') or 0)*100:.1f}%")
    else:
        st.caption("No model-agreement races in sample.")


def _render_unified_strategies(data: dict, prefix: str = ""):
    """ROI by odds bucket + class/distance breakdown."""
    s = data.get("summary") or data
    buckets = s.get("et_odds_buckets") or []
    if buckets:
        st.markdown("##### ROI by ET Top-1 Odds Bucket  *(level $1 stakes)*")
        rows = [{"Odds Bucket": b["label"],
                 "Races": b["n"],
                 "Win %": _fmt_pct_or_dash(b["win_rate"]),
                 "Place %": _fmt_pct_or_dash(b["place_rate"]),
                 "Win-only ROI": _fmt_signed_pct(b["win_roi"])}
                for b in buckets]
        st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

    bk = s.get("breakdown") or {}
    by_class = bk.get("by_class") or {}
    by_dist = bk.get("by_distance") or {}
    if by_class:
        st.markdown("##### Win Rate by Race Class")
        rows = [{"Class": k, "Races": v["n"],
                 "ET Win": _fmt_pct_or_dash(v["et_win"]),
                 "ET Plc": _fmt_pct_or_dash(v["et_plc"]),
                 "SARR Win": _fmt_pct_or_dash(v["sa_win"]),
                 "SARR Plc": _fmt_pct_or_dash(v["sa_plc"]),
                 "Market Win": _fmt_pct_or_dash(v["mk_win"])}
                for k, v in sorted(by_class.items())]
        st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)
    if by_dist:
        st.markdown("##### Win Rate by Distance")
        rows = [{"Distance": k, "Races": v["n"],
                 "ET Win": _fmt_pct_or_dash(v["et_win"]),
                 "SARR Win": _fmt_pct_or_dash(v["sa_win"]),
                 "Market Win": _fmt_pct_or_dash(v["mk_win"])}
                for k, v in by_dist.items()]
        st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)


def _render_unified_pace_proj(data: dict, prefix: str = ""):
    """Pace classifier accuracy + ET projection bias."""
    s = data.get("summary") or data
    pace = s.get("pace") or {}
    if pace.get("total"):
        st.markdown("##### Pace Classifier Accuracy")
        c1, c2, c3 = st.columns(3)
        c1.metric("Exact match",
                  f"{pace.get('correct',0)}/{pace.get('total',0)}",
                  _fmt_pct_or_dash(pace.get("exact_pct")))
        c2.metric("Same-side (F/N/S)",
                  f"{pace.get('close',0)}/{pace.get('total',0)}",
                  _fmt_pct_or_dash(pace.get("close_pct")))
        c3.metric("Wrong direction",
                  f"{pace.get('wrong',0)}/{pace.get('total',0)}",
                  _fmt_pct_or_dash(1 - (pace.get('close_pct') or 0)))
        by_label = pace.get("by_label") or {}
        if by_label:
            with st.expander("Per-predicted-label breakdown"):
                rows = [{"Predicted": k, "Races": v["n"],
                         "Hit": v["correct"],
                         "Hit %": _fmt_pct_or_dash(
                             (v["correct"] / v["n"]) if v["n"] else None)}
                        for k, v in by_label.items()]
                st.dataframe(pd.DataFrame(rows), width='stretch',
                             hide_index=True)

    pe = s.get("proj_err") or {}
    raw = pe.get("raw") or {}
    cor = pe.get("corrected") or {}
    if raw:
        st.markdown("##### ET Projected-Time Bias  *(actual winners)*")
        c1, c2, c3 = st.columns(3)
        c1.metric("Mean error (raw)",
                  _fmt_secs(raw.get("mean")),
                  f"MAE {raw.get('mae', 0):.2f}s")
        c2.metric("Mean error (v4.7-corrected)",
                  _fmt_secs(cor.get("mean")),
                  f"MAE {cor.get('mae', 0):.2f}s")
        c3.metric("% within ±0.5s (corrected)",
                  _fmt_pct_or_dash(cor.get("within_05")),
                  f"raw {(raw.get('within_05') or 0)*100:.0f}%")
        st.caption("v4.7 applies a uniform distance-aware shift to projected times "
                   "(sprint −0.77s, mile −0.71s, route 0). Ranks are unchanged.")


def _render_unified_per_race(data: dict, prefix: str = ""):
    """Race selector with ET / SARR / Market picks + actual finish + horse table."""
    races = data.get("races") or []
    if not races:
        # Aggregate window — race-level detail not stored at window level;
        # the meeting JSON keeps them. Direct user to per-meeting view.
        st.info("Per-race details are stored on each per-meeting file. "
                "Switch the period selector to a single meeting to see this.")
        return

    label_for = lambda r: (
        f"R{r['race_number']}  {r.get('distance','?')}m  "
        f"C{r.get('race_class','?')}  ·  "
        f"winner #{r.get('winner','?')} {r.get('winner_name','')}"
    )
    sel_idx = st.selectbox("Select race:", range(len(races)),
                           format_func=lambda i: label_for(races[i]),
                           key=f"{prefix}race_sel")
    r = races[sel_idx]

    # Top picks summary row
    agreed = bool(r.get("agree_top1"))
    chip = _agreement_chip_html(agreed)
    st.markdown(
        f"### Race {r['race_number']} — {r.get('distance','?')}m  "
        f"&nbsp; {chip}",
        unsafe_allow_html=True,
    )

    cols = st.columns(3)
    with cols[0]:
        st.markdown("**ET Top-1**")
        st.markdown(f"#{r.get('et_top1','?')} {r.get('et_top1_name','') or ''}")
        st.caption(f"Win odds: {r.get('et_top1_odds') or '—'}")
    with cols[1]:
        st.markdown("**SARR Top-1**")
        st.markdown(f"#{r.get('sa_top1','?')} {r.get('sa_top1_name','') or ''}")
        st.caption(f"Win odds: {r.get('sa_top1_odds') or '—'}")
    with cols[2]:
        st.markdown("**Actual Winner**")
        st.markdown(f"#{r.get('winner','?')} {r.get('winner_name','') or ''}")
        st.caption(
            f"Top-3: {', '.join('#'+str(h) for h in (r.get('actual_top3') or []))} "
            f"· Fav #{r.get('fav_horse_no','?')} @ {r.get('fav_odds') or '—'}"
        )

    # Per-model hit summary
    et_m = r.get("et") or {}
    sa_m = r.get("sa") or {}
    mk_m = r.get("mk") or {}
    chk = lambda v: "✓" if v else ("—" if v is None else "✗")
    st.markdown("##### Race-level Hit Summary")
    rows = [
        {"Model": "ET", "Top-1 Win": chk(et_m.get("win")),
         "Top-1 Place": chk(et_m.get("plc")),
         "Top-3 has winner": chk(et_m.get("top3_has_w")),
         "Top-3 overlap": et_m.get("top3_overlap"),
         "Rank ρ": (f"{r['et_rank_corr']:.2f}"
                    if r.get("et_rank_corr") is not None else "—")},
        {"Model": "SARR", "Top-1 Win": chk(sa_m.get("win")),
         "Top-1 Place": chk(sa_m.get("plc")),
         "Top-3 has winner": chk(sa_m.get("top3_has_w")),
         "Top-3 overlap": sa_m.get("top3_overlap"),
         "Rank ρ": (f"{r['sa_rank_corr']:.2f}"
                    if r.get("sa_rank_corr") is not None else "—")},
        {"Model": "Market", "Top-1 Win": chk(mk_m.get("win")),
         "Top-1 Place": chk(mk_m.get("plc")),
         "Top-3 has winner": chk(mk_m.get("top3_has_w")),
         "Top-3 overlap": mk_m.get("top3_overlap"),
         "Rank ρ": "—"},
    ]
    st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

    # Pace prediction vs actual
    pp, pa = r.get("pace_predicted"), r.get("pace_actual")
    if pp or pa:
        st.markdown(f"**Pace** — predicted: `{pp or '—'}`  ·  actual: `{pa or '—'}`  "
                    + ("✓" if pp == pa else "✗"))

    # Horse-level comparison table
    horses = r.get("horses") or []
    if horses:
        st.markdown("##### Horse-by-Horse")
        rows = []
        for h in horses:
            rows.append({
                "Place": h.get("actual_place"),
                "#": h.get("horse_no"),
                "Horse": h.get("horse_name"),
                "Odds": h.get("win_odds"),
                "Time": h.get("finish_time"),
                "ET rank": h.get("et_rank") or "—",
                "ET proj": (f"{h['et_proj']:.2f}"
                            if h.get("et_proj") is not None else "—"),
                "ET err": (f"{(h['et_proj'] - h['finish_time']):+.2f}"
                           if (h.get("et_proj") is not None
                               and h.get("finish_time") is not None) else "—"),
                "ET win%": (f"{h['et_win_prob']*100:.1f}"
                            if h.get("et_win_prob") is not None else "—"),
                "ET style": h.get("et_style") or "",
                "SA rank": h.get("sa_rank") or "—",
                "SA score": (f"{h['sa_score']:.2f}"
                             if h.get("sa_score") is not None else "—"),
                "SA style": h.get("sa_style") or "",
            })
        st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)


def _render_unified_trends(data: dict, prefix: str = ""):
    """Per-meeting trend chart for window / month / season views."""
    s = data.get("summary") or data
    pm = s.get("per_meeting") or []
    if not pm:
        st.info("Trend chart only available on aggregate windows (Last 7 / 30 / Month / All).")
        return
    df = pd.DataFrame(pm)
    if "date" not in df.columns:
        st.warning("Per-meeting data missing date column.")
        return
    df = df.sort_values("date")
    df_idx = df.set_index("date")
    show_cols = [c for c in ("et_top1_win", "sa_top1_win", "mk_top1_win") if c in df_idx.columns]
    if show_cols:
        st.markdown("##### Top-1 Win Rate by Meeting")
        st.line_chart(df_idx[show_cols], width='stretch')
    if "agree_rate" in df_idx.columns:
        st.markdown("##### Model-Agreement Rate by Meeting")
        st.line_chart(df_idx[["agree_rate"]], width='stretch')
    st.markdown("##### Per-Meeting Detail")
    fmt_df = df.copy()
    for col in ("et_top1_win", "sa_top1_win", "mk_top1_win", "agree_rate", "agree_win_rate"):
        if col in fmt_df.columns:
            fmt_df[col] = fmt_df[col].apply(
                lambda x: f"{x*100:.1f}%" if isinstance(x, (int, float)) else "—")
    st.dataframe(fmt_df, width='stretch', hide_index=True)


def _render_unified_backtest(data: dict, prefix: str = ""):
    """Top-level renderer: 5 tabs over the unified backtest JSON."""
    if not data:
        st.info("No data loaded.")
        return
    tabs = st.tabs(["Overview", "Per-Race", "Strategies",
                    "Pace & Projection", "Trends"])
    with tabs[0]:
        _render_unified_overview(data, prefix)
    with tabs[1]:
        _render_unified_per_race(data, prefix)
    with tabs[2]:
        _render_unified_strategies(data, prefix)
    with tabs[3]:
        _render_unified_pace_proj(data, prefix)
    with tabs[4]:
        _render_unified_trends(data, prefix)


# ══════════════════════════════════════════════════════════════════════════════
# Model Comparison — ET vs SARR + mutual picks tracker
# ══════════════════════════════════════════════════════════════════════════════

def page_model_comparison():
    """ET vs SARR head-to-head + mutual (ET ∩ SARR) picks performance.

    Reads ``cache/et_vs_sarr.json`` which is produced by
    ``compare_et_vs_sarr.py``. Provides a regenerate button so users can
    refresh after a new meeting publishes results.
    """
    import json as _json
    import subprocess as _sp
    import sys as _sys
    from pathlib import Path as _P

    st.markdown('<div class="page-title">⚖️ Model Comparison</div>',
                unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">ET vs SARR head-to-head, and '
                'how often their <b>mutual top-3 picks</b> land on the '
                'podium.</div>', unsafe_allow_html=True)

    cache_path = _P("cache") / "et_vs_sarr.json"

    c1, c2 = st.columns([3, 1])
    with c2:
        if st.button("🔄 Recompute", width='stretch',
                     help="Re-runs compare_et_vs_sarr.py across all "
                          "meetings with ET + SARR + results."):
            with st.spinner("Comparing ET vs SARR across all meetings…"):
                try:
                    proc = _sp.run(
                        [_sys.executable, "compare_et_vs_sarr.py"],
                        capture_output=True, text=True, timeout=180,
                        cwd=str(_P.cwd()),
                    )
                    if proc.returncode == 0:
                        st.success("Refresh complete.")
                    else:
                        st.error(f"Exit {proc.returncode}: "
                                 f"{proc.stderr[-400:] or proc.stdout[-400:]}")
                except Exception as e:
                    st.error(f"Recompute failed: {e}")
            st.rerun()

    if not cache_path.exists():
        st.info("No comparison data yet — click **Recompute** to build it.")
        return

    try:
        data = _json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception as e:
        st.error(f"Failed to read {cache_path}: {e}")
        return

    n_meetings = data.get("n_meetings", 0)
    n_races = data.get("n_races", 0)
    gen_at = data.get("generated_at", "")
    with c1:
        st.caption(f"📅 **{n_meetings} meetings · {n_races} races** · "
                   f"last refreshed {gen_at[:16]}")

    o = data.get("overall")
    if not o:
        st.warning("Empty overall summary.")
        return

    # ── Tabs inside the page ────────────────────────────────────────
    t_overall, t_mutual, t_breakdown, t_strength, t_today, t_pace = st.tabs([
        "📊 Overall", "🤝 Mutual picks", "🗺️ Breakdown",
        "🔥 Strength heatmap", "📅 Today", "🏁 Pace v2",
    ])

    # ============================================================
    # TAB 1 — Overall ET vs SARR
    # ============================================================
    with t_overall:
        st.markdown("### ET vs SARR — head-to-head")
        cols = st.columns(5)
        def _pct(v):
            return f"{v*100:.1f}%" if v is not None else "—"
        with cols[0]:
            st.metric("Top-1 win", _pct(o["et_top_pick_win_mean"]),
                      delta=f"{(o['sarr_top_pick_win_mean']-o['et_top_pick_win_mean'])*100:+.1f}pp SARR")
            st.caption(f"SARR: {_pct(o['sarr_top_pick_win_mean'])}")
        with cols[1]:
            st.metric("Top-1 place", _pct(o["et_top_pick_place_mean"]),
                      delta=f"{(o['sarr_top_pick_place_mean']-o['et_top_pick_place_mean'])*100:+.1f}pp SARR")
            st.caption(f"SARR: {_pct(o['sarr_top_pick_place_mean'])}")
        with cols[2]:
            st.metric("Top-3 has winner", _pct(o["et_top3_any_1st_mean"]),
                      delta=f"{(o['sarr_top3_any_1st_mean']-o['et_top3_any_1st_mean'])*100:+.1f}pp SARR")
            st.caption(f"SARR: {_pct(o['sarr_top3_any_1st_mean'])}")
        with cols[3]:
            st.metric("Top-3 = trifecta", _pct(o["et_top3_trifecta_mean"]),
                      delta=f"{(o['sarr_top3_trifecta_mean']-o['et_top3_trifecta_mean'])*100:+.1f}pp SARR")
            st.caption(f"SARR: {_pct(o['sarr_top3_trifecta_mean'])}")
        with cols[4]:
            st.metric("Top-1 agreement", f"{o['agree_top1_pct']:.1f}%",
                      help="Share of races where both models pick the "
                           "same rank-1 horse.")

        st.divider()

        # By-venue and by-distance tables
        import pandas as _pd
        st.markdown("#### By venue")
        df_v = _pd.DataFrame(data.get("by_venue", []))
        if not df_v.empty:
            df_v = df_v[["venue", "n",
                         "et_top_pick_win_mean", "sarr_top_pick_win_mean",
                         "et_top_pick_place_mean", "sarr_top_pick_place_mean",
                         "agree_top1_pct"]]
            df_v.columns = ["Venue", "N", "ET win%", "SARR win%",
                            "ET place%", "SARR place%", "Agree top-1 %"]
            for c in ["ET win%", "SARR win%", "ET place%", "SARR place%"]:
                df_v[c] = (df_v[c] * 100).round(1)
            st.dataframe(df_v, width='stretch', hide_index=True)

        st.markdown("#### By distance bucket")
        df_d = _pd.DataFrame(data.get("by_dist_bucket", []))
        if not df_d.empty:
            df_d = df_d[["dist_bucket", "n",
                         "et_top_pick_win_mean", "sarr_top_pick_win_mean",
                         "et_top_pick_place_mean", "sarr_top_pick_place_mean"]]
            df_d.columns = ["Distance", "N", "ET win%", "SARR win%",
                            "ET place%", "SARR place%"]
            for c in ["ET win%", "SARR win%", "ET place%", "SARR place%"]:
                df_d[c] = (df_d[c] * 100).round(1)
            st.dataframe(df_d, width='stretch', hide_index=True)

    # ============================================================
    # TAB 2 — Mutual picks (ET ∩ SARR)
    # ============================================================
    with t_mutual:
        st.markdown("### Mutual picks — ET top-3 ∩ SARR top-3")
        st.caption("A *mutual pick* is any horse that appears in BOTH ET's "
                   "top-3 AND SARR's top-3 for the same race. Below: how "
                   "often these mutual horses actually land on the "
                   "podium.")

        cols = st.columns(4)
        with cols[0]:
            st.metric("Avg mutual set size",
                      f"{o['mutual_size_mean']:.2f} / 3")
            st.caption("Out of 3 possible")
        with cols[1]:
            st.metric("P(mutual horse in top-3)",
                      f"{o['mutual_any_in_top3_mean']*100:.1f}%",
                      help="Unconditional. Includes races where no "
                           "mutual pick exists.")
            cond_t3 = o.get("cond_mut_any_in_top3")
            if cond_t3 is not None:
                st.caption(f"When ≥1 mutual exists: "
                           f"**{cond_t3*100:.1f}%**")
        with cols[2]:
            st.metric("P(mutual horse wins)",
                      f"{o['mutual_winner_hit_mean']*100:.1f}%",
                      help="Unconditional.")
            cond_w = o.get("cond_mut_winner_hit")
            if cond_w is not None:
                st.caption(f"When ≥1 mutual exists: "
                           f"**{cond_w*100:.1f}%**")
        with cols[3]:
            st.metric("P(2+ mutual in top-3)",
                      f"{o['mutual_2plus_in_top3_mean']*100:.1f}%")
            st.caption(f"P(all 3 in top-3): "
                       f"**{o['mutual_3_in_top3_mean']*100:.1f}%**")

        st.divider()
        st.markdown("#### Hit rates by mutual set size")
        st.caption("The bigger the mutual set, the stronger the signal. "
                   "Size=3 races (≈13% of slate) are the highest-confidence "
                   "trio/trifecta plays.")
        import pandas as _pd
        df_m = _pd.DataFrame(data.get("by_mutual_size", []))
        if not df_m.empty:
            df_m["share"] = (df_m["n"] / df_m["n"].sum() * 100).round(1)
            df_m = df_m[[
                "mutual_size", "n", "share",
                "mutual_any_in_top3_mean",
                "mutual_winner_hit_mean",
                "mutual_2plus_in_top3_mean",
                "mutual_3_in_top3_mean",
            ]]
            df_m.columns = ["Size", "N races", "Share %",
                            "P(any in top-3)",
                            "P(any wins)",
                            "P(2+ in top-3)",
                            "P(all 3 in top-3)"]
            for c in df_m.columns[3:]:
                df_m[c] = (df_m[c].fillna(0) * 100).round(1)
            st.dataframe(df_m, width='stretch', hide_index=True)

        # Practical takeaway
        if len(data.get("by_mutual_size", [])) >= 2:
            size_lookup = {r["mutual_size"]: r
                           for r in data["by_mutual_size"]}
            s3 = size_lookup.get(3) or {}
            s2 = size_lookup.get(2) or {}
            insight_parts = []
            if s3.get("n", 0) > 0:
                insight_parts.append(
                    f"**Size-3 mutual sets** (all three picks shared, "
                    f"{s3['n']} races): "
                    f"{s3['mutual_winner_hit_mean']*100:.0f}% winner hit, "
                    f"{s3['mutual_any_in_top3_mean']*100:.0f}% in-top-3.")
            if s2.get("n", 0) > 0:
                insight_parts.append(
                    f"**Size-2 mutual sets** ({s2['n']} races): "
                    f"{s2['mutual_winner_hit_mean']*100:.0f}% winner hit, "
                    f"{s2['mutual_any_in_top3_mean']*100:.0f}% in-top-3.")
            if insight_parts:
                st.info("💡 " + "  \n".join(insight_parts))

    # ============================================================
    # TAB 3 — Breakdown (venue × dist, pace, etc.)
    # ============================================================
    with t_breakdown:
        import pandas as _pd
        st.markdown("### Venue × Distance — model strengths")
        df_vd = _pd.DataFrame(data.get("by_venue_dist", []))
        if not df_vd.empty:
            df_vd = df_vd[[
                "venue", "dist_bucket", "n",
                "et_top_pick_win_mean", "sarr_top_pick_win_mean",
                "et_top_pick_place_mean", "sarr_top_pick_place_mean",
                "mutual_any_in_top3_mean",
            ]]
            df_vd.columns = ["Venue", "Distance", "N",
                             "ET win%", "SARR win%",
                             "ET place%", "SARR place%",
                             "Mutual in top-3 %"]
            for c in df_vd.columns[3:]:
                df_vd[c] = (df_vd[c].fillna(0) * 100).round(1)
            st.dataframe(df_vd, width='stretch', hide_index=True)

        st.markdown("### By pace projection")
        df_p = _pd.DataFrame(data.get("by_pace", []))
        if not df_p.empty:
            df_p = df_p[[
                "pace", "n",
                "et_top_pick_win_mean", "sarr_top_pick_win_mean",
                "et_top_pick_place_mean", "sarr_top_pick_place_mean",
                "mutual_any_in_top3_mean",
            ]]
            df_p.columns = ["Pace", "N",
                            "ET win%", "SARR win%",
                            "ET place%", "SARR place%",
                            "Mutual in top-3 %"]
            for c in df_p.columns[2:]:
                df_p[c] = (df_p[c].fillna(0) * 100).round(1)
            st.dataframe(df_p, width='stretch', hide_index=True)

    # ============================================================
    # TAB 4 — Strength heatmap (which model wins where?)
    # ============================================================
    with t_strength:
        import pandas as _pd
        st.markdown("### Which model is best where?")
        st.caption("For every dimension cell, shows the **better model** (ET "
                   "vs SARR) at the chosen metric. Green = SARR wins, "
                   "Blue = ET wins, Grey = tied. Cell value is the Δ "
                   "(SARR − ET) in percentage points; absolute value "
                   "indicates strength of edge.")

        metric_choice = st.radio(
            "Metric",
            ["WIN", "PLACE", "Top-3 has winner", "Top-3 = trifecta"],
            horizontal=True, key="mc_strength_metric",
        )
        metric_map = {
            "WIN": ("et_top_pick_win_mean", "sarr_top_pick_win_mean"),
            "PLACE": ("et_top_pick_place_mean", "sarr_top_pick_place_mean"),
            "Top-3 has winner": ("et_top3_any_1st_mean", "sarr_top3_any_1st_mean"),
            "Top-3 = trifecta": ("et_top3_trifecta_mean", "sarr_top3_trifecta_mean"),
        }
        ek, sk = metric_map[metric_choice]
        min_n = st.slider("Min sample (N races per cell)", 3, 30, 5,
                          key="mc_strength_minn")

        def _make_heat(rows, group_label):
            df = _pd.DataFrame(rows)
            if df.empty:
                st.caption(f"_(no data for {group_label})_")
                return
            df = df[df["n"] >= min_n].copy()
            if df.empty:
                st.caption(f"_(no cells with N ≥ {min_n})_")
                return
            df["ET %"] = (df[ek].fillna(0) * 100).round(1)
            df["SARR %"] = (df[sk].fillna(0) * 100).round(1)
            df["Δ (SARR-ET) pp"] = (df["SARR %"] - df["ET %"]).round(1)
            df["Winner"] = df["Δ (SARR-ET) pp"].apply(
                lambda v: "SARR" if v > 1 else ("ET" if v < -1 else "tie"))
            # Mutual conviction
            if "mutual_any_in_top3_mean" in df.columns:
                df["Mutual top-3 %"] = (
                    df["mutual_any_in_top3_mean"].fillna(0) * 100).round(1)
            return df

        # Build display tables per dimension
        dims = [
            ("Venue",            "by_venue",       ["venue"]),
            ("Distance",         "by_dist_bucket", ["dist_bucket"]),
            ("Venue × Distance", "by_venue_dist",  ["venue", "dist_bucket"]),
            ("Going",            "by_going",       ["going"]),
            ("Class",            "by_class",       ["race_class"]),
            ("Surface",          "by_surface",     ["surface"]),
            ("Projected pace",   "by_pace",        ["pace"]),
        ]

        for label, key, group_cols in dims:
            st.markdown(f"#### {label}")
            df = _make_heat(data.get(key, []), label)
            if df is None or df.empty:
                continue
            keep = group_cols + ["n", "ET %", "SARR %", "Δ (SARR-ET) pp",
                                 "Winner"]
            if "Mutual top-3 %" in df.columns:
                keep.append("Mutual top-3 %")
            df_show = df[keep].sort_values("n", ascending=False)
            def _style(row):
                d = row["Δ (SARR-ET) pp"]
                bg = ("background-color: #2d5a2d; color:white" if d > 1
                      else "background-color: #1f4060; color:white" if d < -1
                      else "")
                return [bg] * len(row)
            st.dataframe(df_show.style.apply(_style, axis=1),
                         width='stretch', hide_index=True)

        # ── Auto-recommendation ─────────────────────────────────────
        st.divider()
        st.markdown("#### 🧭 Auto-recommendation per cell")
        st.caption("For each venue × distance cell with N ≥ 10, this "
                   "table shows which model to trust for **place pool**, "
                   "and how strong the mutual ET ∩ SARR signal is.")
        df_vd = _make_heat(data.get("by_venue_dist", []), "Venue × Distance")
        if df_vd is not None and not df_vd.empty:
            rec_df = df_vd[df_vd["n"] >= 10].copy()
            ek2 = "et_top_pick_place_mean"
            sk2 = "sarr_top_pick_place_mean"
            rec_df["ET place %"] = (rec_df[ek2].fillna(0) * 100).round(1)
            rec_df["SARR place %"] = (rec_df[sk2].fillna(0) * 100).round(1)
            rec_df["Recommended"] = rec_df.apply(
                lambda r: ("SARR" if r["SARR place %"] - r["ET place %"] > 5
                           else "ET" if r["ET place %"] - r["SARR place %"] > 5
                           else "Either / use mutual"), axis=1)
            cols_rec = ["venue", "dist_bucket", "n",
                        "ET place %", "SARR place %",
                        "Mutual top-3 %", "Recommended"]
            rec_df = rec_df[[c for c in cols_rec if c in rec_df.columns]]
            st.dataframe(rec_df.sort_values("n", ascending=False),
                         width='stretch', hide_index=True)

    # ============================================================
    # TAB 5 — Today's mutual picks
    # ============================================================
    with t_today:
        st.markdown("### Today's mutual picks")
        st.caption("Reads `reports/mutual_{DC}.json` (built post-pipeline). "
                   "ET top-3 ∩ SARR top-3 per race — your highest-confidence shortlist.")

        import datetime as _dt
        today_dc = _dt.date.today().strftime("%Y%m%d")
        d_input = st.text_input("Meeting date (YYYYMMDD)", value=today_dc,
                                key="mc_today_date")
        if not d_input or len(d_input) != 8 or not d_input.isdigit():
            st.warning("Enter a valid YYYYMMDD date.")
            return

        reports_dir = _P("reports")
        mut_p = reports_dir / f"mutual_{d_input}.json"

        if mut_p.exists():
            # Persistent path — preferred
            mut_d = _json.loads(mut_p.read_text(encoding="utf-8"))
            import pandas as _pd
            has_fuse = bool(mut_d.get("has_fuse"))
            rows_out = []
            for r in mut_d.get("races", []):
                triple_nos = {m.get("horse_no")
                              for m in r.get("triple_horses", [])}
                disp = ", ".join(
                    (("★" if m.get("horse_no") in triple_nos else "")
                     + f"{m.get('horse_no')} {m.get('horse_name','')}").strip()
                    for m in r.get("mutual_horses", [])
                ) if r.get("mutual_horses") else "—"
                row = {
                    "Race": r.get("race_number"),
                    "ET top-3": ", ".join(str(x) for x in r.get("et_picks", [])),
                    "SARR top-3": ", ".join(str(x) for x in r.get("sarr_picks", [])),
                }
                if has_fuse:
                    row["FUSE top-3"] = ", ".join(
                        str(x) for x in r.get("fuse_picks", []))
                row["Mutual size"] = r.get("mutual_size", 0)
                if has_fuse:
                    row["3-way"] = r.get("triple_size", 0)
                row["Mutual picks"] = disp
                rows_out.append(row)
            if rows_out:
                df_t = _pd.DataFrame(rows_out)
                def _style(row):
                    if row.get("3-way", 0):
                        return ["background-color: #1f4a38; color: #a7f3d0"] * len(row)
                    size = row["Mutual size"]
                    if size >= 3:
                        return ["background-color: #2d5a2d; color: white"] * len(row)
                    if size == 2:
                        return ["background-color: #4a4a1f; color: #f0f0a0"] * len(row)
                    return [""] * len(row)
                st.dataframe(df_t.style.apply(_style, axis=1),
                             width='stretch', hide_index=True)
                n_high = sum(1 for r in rows_out if r["Mutual size"] >= 2)
                n_triple = sum(1 for r in rows_out if r.get("3-way", 0))
                st.caption(
                    f"📌 **{n_high}** high-conviction races "
                    f"(mutual size ≥ 2)"
                    + (f" · **{n_triple}** races with 3-way ET∩SARR∩FUSE "
                       f"agreement (★, green rows)" if has_fuse else "")
                    + f". Source: persisted `mutual_{d_input}.json` "
                    f"({mut_d.get('n_mutual_total','?')} total mutual picks)."
                )
            else:
                st.info("Persistent mutual file is empty.")
            return

        # ── Fallback: live recompute (legacy path) ────────────────
        st.warning(
            "No persistent `mutual_{DC}.json` for this date — falling back "
            "to live recompute. Run `python build_mutual_picks.py "
            f"--date {d_input}` to persist."
        )

        et_p = None
        for tag in ("v4.4", "v3.4.8"):
            cand = reports_dir / f"race_day_report_{d_input}_{tag}.json"
            if cand.exists():
                et_p = cand
                break
        sarr_p = reports_dir / f"race_day_report_{d_input}_SARR.json"

        if not et_p or not et_p.exists():
            st.info(f"No ET report for {d_input}.")
            return
        if not sarr_p.exists():
            st.info(f"No SARR report for {d_input}.")
            return

        et_d = _json.loads(et_p.read_text(encoding="utf-8"))
        sarr_d = _json.loads(sarr_p.read_text(encoding="utf-8"))
        et_by = {r["race_number"]: r for r in et_d.get("races", [])}
        sa_by = {r["race_number"]: r for r in sarr_d.get("races", [])}
        all_rn = sorted(set(et_by) | set(sa_by))

        import pandas as _pd
        rows_out = []
        for rn in all_rn:
            er = et_by.get(rn) or {}
            sr = sa_by.get(rn) or {}
            et_top3 = []
            for p in (er.get("picks") or [])[:3]:
                try:
                    et_top3.append(int(p.get("horse_no")))
                except (TypeError, ValueError):
                    pass
            sa_top3 = []
            for p in (sr.get("picks") or [])[:3]:
                try:
                    sa_top3.append(int(p.get("horse_no")))
                except (TypeError, ValueError):
                    pass
            mut = sorted(set(et_top3) & set(sa_top3))
            # Build name lookup from ET race
            name_map = {}
            for p in (er.get("picks") or []) + (er.get("runners") or []):
                try:
                    name_map[int(p.get("horse_no"))] = p.get("horse_name") \
                                                       or p.get("name") or ""
                except (TypeError, ValueError):
                    pass
            mut_disp = ", ".join(
                f"{h} {name_map.get(h, '')}".strip() for h in mut
            ) if mut else "—"
            rows_out.append({
                "Race": rn,
                "Dist": er.get("distance") or sr.get("distance"),
                "Pace": er.get("pace"),
                "ET top-3": ", ".join(str(x) for x in et_top3),
                "SARR top-3": ", ".join(str(x) for x in sa_top3),
                "Mutual size": len(mut),
                "Mutual picks": mut_disp,
            })

        if rows_out:
            df_t = _pd.DataFrame(rows_out)
            # Highlight size-2/3 rows
            def _style(row):
                size = row["Mutual size"]
                if size >= 3:
                    return ["background-color: #2d5a2d; color: white"] * len(row)
                if size == 2:
                    return ["background-color: #4a4a1f; color: #f0f0a0"] * len(row)
                return [""] * len(row)
            st.dataframe(df_t.style.apply(_style, axis=1),
                         width='stretch', hide_index=True)
            n_high = sum(1 for r in rows_out if r["Mutual size"] >= 2)
            st.caption(f"📌 **{n_high}** high-conviction races today "
                       f"(mutual size ≥ 2).")
        else:
            st.info("No races found in reports.")

    # ============================================================
    # TAB 6 — Pace v2 (rebuilt classifier + advantage)
    # ============================================================
    with t_pace:
        st.markdown("### 🏁 Pace classifier v2 + advantage_v2")
        st.caption(
            "**Rebuild of broken pace pipeline.** Old classifier predicted "
            "'Fast' 58/59 races (acc ≈ 14 %). New regression-on-actual_dev "
            "model is gated for production use. Old advantage score had "
            "ρ = −0.066 vs finishing position (noise); the new mechanical "
            "score has ρ ≈ −0.10 over 1 038 horse-races."
        )

        import datetime as _dt
        import pandas as _pd
        from pathlib import Path as _Pp
        pace_today_dc = _dt.date.today().strftime("%Y%m%d")
        pd_input = st.text_input("Meeting date (YYYYMMDD)", value=pace_today_dc,
                                  key="mc_pace_date")
        if not pd_input or len(pd_input) != 8 or not pd_input.isdigit():
            st.warning("Enter a valid YYYYMMDD date.")
            return

        # ── Model status block ────────────────────────────────
        try:
            import pickle
            model_p = _Pp("models/pace_classifier_v2.pkl")
            if model_p.exists():
                with open(model_p, "rb") as _f:
                    _art = pickle.load(_f)
                cv = _art.get("cv", {}) or {}
                ship = bool(_art.get("ship"))
                badge = "✅ shipped" if ship else "⚠ experimental"
                cols_st = st.columns(4)
                cols_st[0].metric("Status", badge)
                cols_st[1].metric("3-band CV acc",
                                  f"{cv.get('acc_3band', 0)*100:.1f}%",
                                  delta="vs 33% baseline")
                cols_st[2].metric("5-class CV acc",
                                  f"{cv.get('acc_5class', 0)*100:.1f}%",
                                  delta="vs 20% baseline")
                cols_st[3].metric("MAE (sec)",
                                  f"{cv.get('mae', 0):.2f}")
            else:
                st.error("No trained model yet. Run "
                         "`python pace_classifier_v2.py train`.")
        except Exception as _e:
            st.warning(f"Could not load model status: {_e}")

        # ── Per-meeting pace v2 table ─────────────────────────
        pv2_p = _Pp("reports") / f"pace_v2_{pd_input}.json"
        et_p_p = None
        for tag in ("v4.4", "v3.4.8"):
            cand = _Pp("reports") / f"race_day_report_{pd_input}_{tag}.json"
            if cand.exists():
                et_p_p = cand
                break

        if not pv2_p.exists():
            st.info(
                f"No `pace_v2_{pd_input}.json` for this date. Run "
                f"`python pace_classifier_v2.py infer --date {pd_input}`."
            )
        else:
            pv2 = _json.loads(pv2_p.read_text(encoding="utf-8"))
            conf = pv2.get("confidence", "experimental")
            if conf != "ok":
                st.warning(
                    f"⚠ Model is **{conf}** — band-accuracy CV "
                    f"{(pv2.get('cv_band_acc') or 0)*100:.1f}% < 50% gate. "
                    "Treat as guide, not gospel."
                )

            rows_pv = []
            for r in pv2.get("races", []):
                rows_pv.append({
                    "Race":          r.get("race_number"),
                    "Distance":      r.get("distance"),
                    "Legacy label":  r.get("legacy_label") or "—",
                    "v2 label (5)":  r.get("label_5") or "—",
                    "v2 band":       r.get("label_3") or "—",
                    "v2 dev_pred":   f"{r.get('dev_pred', 0):+.2f}s",
                })
            df_pv = _pd.DataFrame(rows_pv)
            def _hl(row):
                lbl = row["v2 band"]
                if lbl == "Fast":
                    return ["background-color: #4a1f1f; color: #ffcccc"] * len(row)
                if lbl == "Slow":
                    return ["background-color: #1f3a4a; color: #ccddff"] * len(row)
                return [""] * len(row)
            st.dataframe(df_pv.style.apply(_hl, axis=1),
                          width='stretch', hide_index=True)
            from collections import Counter as _Cnt
            cnt_old = _Cnt(r.get("legacy_label") for r in pv2.get("races", []))
            cnt_new = _Cnt(r.get("label_3") for r in pv2.get("races", []))
            st.caption(
                f"**Legacy distribution:** {dict(cnt_old)}  ·  "
                f"**v2 (3-band):** {dict(cnt_new)}"
            )

        # ── Per-race advantage_v2 inspector ───────────────────
        if et_p_p:
            st.markdown("---")
            st.markdown("#### Advantage v2 inspector")
            try:
                from pace_advantage_v2 import compute_for_race as _adv_calc
                et_d_p = _json.loads(et_p_p.read_text(encoding="utf-8"))
                race_opts = [
                    (r.get("race_number"), r.get("distance"))
                    for r in et_d_p.get("races", [])
                ]
                race_pick = st.selectbox(
                    "Race",
                    options=race_opts,
                    format_func=lambda x: f"R{x[0]} ({x[1]}m)",
                    key="mc_adv_race",
                )
                if race_pick:
                    target_r = next(
                        (r for r in et_d_p.get("races", [])
                         if r.get("race_number") == race_pick[0]),
                        None,
                    )
                    if target_r:
                        adv_rows = _adv_calc(target_r)
                        # join with horse name
                        adv_df = _pd.DataFrame(adv_rows)
                        if not adv_df.empty:
                            adv_df = adv_df.sort_values(
                                "pace_advantage_v2", ascending=False
                            )
                            st.dataframe(
                                adv_df, width='stretch',
                                hide_index=True,
                            )
                            st.caption(
                                "**adv_v2 = 0.50·own_ESZ_pct + "
                                "0.50·(1−inside_handicap) + 0.10·style_match**. "
                                "Validated ρ = −0.104 vs finishing position "
                                "across 1 038 horse-races (old adv: −0.066)."
                            )
            except Exception as _e:
                st.warning(f"Could not compute advantage: {_e}")


@st.cache_data(ttl=300, show_spinner=False)
def _fwlab_available_racecards() -> list[str]:
    """Return ISO dates for which a racecard cache exists, newest first."""
    out: list[str] = []
    rc_dir = BASE / "cache"
    if not rc_dir.exists():
        return out
    for fp in rc_dir.glob("racecard_*.json"):
        try:
            iso = fp.stem.replace("racecard_", "")
            datetime.strptime(iso, "%Y-%m-%d")
            out.append(iso)
        except ValueError:
            continue
    return sorted(out, reverse=True)


@st.cache_resource(show_spinner=False)
def _fwlab_load_dataset():
    """Cache the cleaned dataset across reruns (heavy DB read + parsing)."""
    from frameworks.live_predict import load_dataset as _ld
    return _ld()


def _fwlab_run_predictions(date_iso: str, fit_window_days: int,
                           use_live_odds: bool):
    """Run the four frameworks against the chosen racecard."""
    from frameworks.live_predict import predict_racecard, load_racecard
    ds = _fwlab_load_dataset()
    rc = load_racecard(date_iso)
    return predict_racecard(date_iso, fit_window_days=fit_window_days,
                            dataset=ds, racecard=rc,
                            use_live_odds=use_live_odds)


def page_framework_lab():
    st.markdown('<div class="page-title">Framework Lab</div>',
                unsafe_allow_html=True)
    st.markdown(
        '<div class="page-subtitle">'
        'Four candidate ranking models from the May-2026 EDA, scored against '
        'the next racecard. A: market-residual logistic · B: pace-tactics '
        'simulator · C: form-quality (par-time + EWMA) · D: connections '
        '(Beta-Bernoulli on jockey × trainer × horse).'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── Legend / interpretation guide ───────────────────────────────────
    with st.expander("Legend — how each framework is calculated", expanded=False):
        st.markdown(
            """
**Mkt** — Implied win probability from the live HKJC win pool (or last
snapshot in `cache/odds_history/`), de-vigged via normalisation across the
field. This is the market's collective opinion.

**A · Market-residual logistic** — Starts from market probability as a
prior, then a logistic model trained on the last *N* days (slider in the
sidebar) adjusts each runner using features the market is known to
mis-price: draw × distance × surface, ESZ (early-speed z-score from the
speedmap), pace-shape benefit, ratings drift, jockey/trainer hot-form,
and going-fit. Output is `p_market × exp(adjustment)` then re-normalised.
*Use when*: you trust the market broadly but want to amplify documented
edges.

**B · Pace-tactics simulator** — Monte-Carlo speedmap shootout. Each
horse's running style + ESZ + draw produces a sectional trajectory; the
sim resolves contested leads and ground-loss on the bend. *Does not use
market input at all* — pure structural prediction. *Use when*: pace
projection is strong (clear lone leader, or wall-to-wall speed); fade
when the speedmap is uncertain.

**C · Form-quality (par-time + EWMA)** — Each runner's last 6 starts are
weighted exponentially (recency bias), normalised against the HKJC par
time for that class/distance/going, and projected forward to today's
conditions. *Use when*: the class/trip is stable and recent form is
trustworthy.

**D · Connections (hierarchical Beta-Bernoulli)** — Bayesian hit-rate
model on jockey × trainer × horse triplets, with shrinkage toward the
population mean when sample sizes are thin. Captures stable form, jockey
booking signals, and horse-specific quirks. *Use when*: the field has
clear J/T combinations with strong track records.

**Ens** — Equal-weight mean of A/B/C/D. Smoother and less prone to a
single-framework error. *EnsRk* shows the ensemble ranking; *MktRk*
shows where the market has it.
"""
        )

    racecards = _fwlab_available_racecards()
    if not racecards:
        st.info("No racecards in `cache/racecard_*.json`. "
                "Scrape an upcoming meeting first (Race Day page → "
                "**Pipeline → Scrape racecard**).")
        return

    # ── Sidebar controls ────────────────────────────────────────────────
    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
    st.sidebar.markdown('<div class="sb-nav-section">Framework Lab</div>',
                        unsafe_allow_html=True)
    date_iso = st.sidebar.selectbox("Racecard", racecards, index=0,
                                    key="fwlab_date")
    fit_window = st.sidebar.slider("Framework A fit window (days)",
                                   min_value=60, max_value=540, value=180,
                                   step=30, key="fwlab_fit_window")
    use_live = st.sidebar.toggle(
        "Use latest live odds for market prob",
        value=True, key="fwlab_use_live",
        help="If off, Framework A's market-anchor degrades to a uniform "
             "prior. B/C/D are unaffected.")
    run_btn = st.sidebar.button("Run frameworks", type="primary",
                                width='stretch',
                                key="fwlab_run_btn")

    # Lazy-run: only on button press, cache result in session_state by
    # (date, window, live) tuple so the user can flip between races without
    # re-fitting.
    cache_key = ("fwlab_pred", date_iso, fit_window, bool(use_live))
    if run_btn or cache_key in st.session_state:
        if run_btn or cache_key not in st.session_state:
            with st.spinner(f"Loading history + fitting Framework A "
                            f"({fit_window}d window)…"):
                try:
                    res = _fwlab_run_predictions(date_iso, fit_window, use_live)
                except (FileNotFoundError, ValueError) as e:
                    st.error(f"Could not score racecard: {e}")
                    return
                except Exception as e:  # noqa: BLE001
                    st.error(f"Framework run failed: {type(e).__name__}: {e}")
                    return
            st.session_state[cache_key] = res
        res = st.session_state[cache_key]
    else:
        st.info("Pick a meeting and press **Run frameworks** in the sidebar.")
        return

    runners: pd.DataFrame = res.runners
    if runners.empty:
        st.warning("No runners scored.")
        return

    # ── Header metrics ──────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Meeting", date_iso)
    c2.metric("Races", res.n_races)
    c3.metric("Runners", res.n_runners)
    c4.metric("Live odds", "Yes" if res.used_live_odds else "No")

    # ── Race button row (matches Race Day Insight) ─────────────────────
    race_nums = sorted(int(rn) for rn in runners["race_number"].unique())
    if race_nums:
        state_key = f"fwlab_sel_race_{date_iso}"
        if state_key not in st.session_state or st.session_state[state_key] not in race_nums:
            st.session_state[state_key] = race_nums[0]
        _btn_cols = st.columns(len(race_nums))
        for _i, _rn in enumerate(race_nums):
            with _btn_cols[_i]:
                _is_active = (st.session_state[state_key] == _rn)
                if st.button(
                    f"R{_rn}",
                    key=f"fwlab_tab_{date_iso}_{_rn}",
                    width='stretch',
                    type="primary" if _is_active else "secondary",
                ):
                    st.session_state[state_key] = _rn
                    st.rerun()
        sel_rn = st.session_state[state_key]

        sub = runners[runners["race_number"] == sel_rn].copy()
        meta_dist = int(sub["distance"].iloc[0]) if pd.notna(sub["distance"].iloc[0]) else 0
        meta_track = sub["race_track"].iloc[0]
        meta_going = sub["going"].iloc[0] or "—"
        st.caption(f"Race {int(sel_rn)} · {meta_track} {meta_dist}m · "
                   f"going {meta_going} · field of {len(sub)}")

        disp = sub[[
            "horse_number", "horse_name", "draw_num", "jockey", "trainer",
            "p_market", "p_A", "p_B", "p_C", "p_D", "p_ens",
            "rank_p_market", "rank_p_ens",
        ]].rename(columns={
            "horse_number": "No",
            "horse_name":   "Horse",
            "draw_num":     "Gt",
            "jockey":       "Jockey",
            "trainer":      "Trainer",
            "p_market":     "Mkt",
            "p_A":          "A",
            "p_B":          "B",
            "p_C":          "C",
            "p_D":          "D",
            "p_ens":        "Ens",
            "rank_p_market": "MktRk",
            "rank_p_ens":    "EnsRk",
        }).sort_values("Ens", ascending=False)

        disp["No"] = disp["No"].astype("Int64")
        disp["Gt"] = disp["Gt"].astype("Int64")
        for col in ("Mkt", "A", "B", "C", "D", "Ens"):
            disp[col] = (disp[col] * 100).round(1)

        # quick highlights
        top_ens = disp.iloc[0]
        mkt_top = sub.loc[sub["rank_p_market"] == 1].iloc[0]
        agree = top_ens["No"] == mkt_top["horse_number"]
        cols = st.columns(3)
        cols[0].metric("Ensemble top pick",
                       f"#{int(top_ens['No'])} {top_ens['Horse']}",
                       f"{top_ens['Ens']:.1f}%")
        cols[1].metric("Market favourite",
                       f"#{int(mkt_top['horse_number'])} "
                       f"{mkt_top['horse_name']}",
                       f"{mkt_top['p_market']*100:.1f}%")
        cols[2].metric("Models vs market",
                       "Agree" if agree else "Disagree",
                       "—" if agree else
                       f"value on #{int(top_ens['No'])}")

        st.dataframe(
            disp,
            width='stretch',
            hide_index=True,
            column_config={
                "Mkt": st.column_config.NumberColumn("Mkt %", format="%.1f"),
                "A":   st.column_config.NumberColumn("A %",   format="%.1f",
                        help="Market-residual logistic (market-anchored)"),
                "B":   st.column_config.NumberColumn("B %",   format="%.1f",
                        help="Pace-tactics simulator (no market input)"),
                "C":   st.column_config.NumberColumn("C %",   format="%.1f",
                        help="Form-quality (par-time + EWMA)"),
                "D":   st.column_config.NumberColumn("D %",   format="%.1f",
                        help="Connections hierarchical Beta-Bernoulli"),
                "Ens": st.column_config.NumberColumn("Ens %", format="%.1f",
                        help="Equal-weight ensemble of A/B/C/D"),
            },
        )

    # ── Whole-card summary ──────────────────────────────────────────────
    st.markdown("### Cross-race summary — ensemble top picks")
    top_each = (runners.sort_values(["race_number", "p_ens"],
                                    ascending=[True, False])
                       .groupby("race_number")
                       .head(1)
                       [["race_number", "horse_number", "horse_name",
                         "jockey", "draw_num", "p_market", "p_ens",
                         "rank_p_market"]]
                       .copy())
    top_each["edge_vs_mkt"] = (top_each["p_ens"] - top_each["p_market"]) * 100
    top_each = top_each.rename(columns={
        "race_number": "R",
        "horse_number": "No",
        "horse_name": "Horse",
        "jockey": "Jockey",
        "draw_num": "Gt",
        "p_market": "Mkt%",
        "p_ens": "Ens%",
        "rank_p_market": "MktRk",
        "edge_vs_mkt": "Edge (pp)",
    })
    top_each["Mkt%"] = (top_each["Mkt%"] * 100).round(1)
    top_each["Ens%"] = (top_each["Ens%"] * 100).round(1)
    top_each["Edge (pp)"] = top_each["Edge (pp)"].round(1)
    top_each["No"] = top_each["No"].astype("Int64")
    top_each["Gt"] = top_each["Gt"].astype("Int64")
    top_each["MktRk"] = top_each["MktRk"].astype("Int64")
    st.dataframe(top_each, width='stretch', hide_index=True)

    # CSV export
    csv = runners.to_csv(index=False).encode("utf-8")
    st.download_button("Download full prediction CSV", data=csv,
                       file_name=f"framework_lab_{date_iso}.csv",
                       mime="text/csv")



def page_backtest():
    st.markdown('<div class="page-title">Model Backtest</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">Compare ET · SARR · Market across any time window</div>',
                unsafe_allow_html=True)

    # ── Sidebar: scrape results + run backtest ─────────────
    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
    st.sidebar.markdown('<div class="sb-nav-section">Post-Race Data</div>', unsafe_allow_html=True)

    scrape_date = st.sidebar.date_input("Results date", value=date.today(),
                                        key="bt_scrape_date")
    col_a, col_b = st.sidebar.columns(2)
    with col_a:
        if st.button("[ Run Post-Race ]", width='stretch',
                      key="btn_scrape_results",
                      help="Full 8-step pipeline: results · DB scrape · "
                           "incidents · RP photos · OCR · form-guide rebuild · "
                           "commentary · backtest. Pushes outputs to GitHub on cloud."):
            _run_results_scraper(scrape_date.isoformat(), full=True)
            st.cache_data.clear()
            # Form DB lives on cache_resource (survives cache_data.clear) —
            # the pipeline rewrites the underlying xlsx so invalidate now.
            try:
                _load_form_db.clear()
            except Exception:
                pass
            st.rerun()
    with col_b:
        if st.button("[ Backtest only ]", width='stretch',
                      key="btn_run_backtest",
                      help="Re-run backtest (legacy + unified) for the selected date."):
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

    if st.sidebar.button("[ Rebuild Unified Backtest ]",
                         width='stretch',
                         key="btn_unified_all",
                         help="Regenerate every unified per-meeting JSON + "
                              "rolling windows (last7/30/90/all) + per-month "
                              "aggregates. On cloud, pushes the JSONs to GitHub."):
        _run_backtest_unified_all()
        st.cache_data.clear()
        st.rerun()

    agg_month = st.sidebar.text_input("Legacy Month (YYYY-MM)",
                                       value=date.today().strftime("%Y-%m"),
                                       key="bt_month")
    if st.sidebar.button("[ Legacy Monthly ]", width='stretch',
                          key="btn_monthly_bt"):
        _run_backtest_agg("--month", agg_month)
        st.cache_data.clear()
        st.rerun()
    agg_season = st.sidebar.text_input("Legacy Season", value="2025-2026",
                                        key="bt_season")
    if st.sidebar.button("[ Legacy Seasonal ]", width='stretch',
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
                st.text(f"  {d}  — needs results scrape")

    st.markdown("---")

    # ── Period selector ──────────────────────────────────
    inventory = _list_unified_backtests()
    have_meetings = sorted(inventory["meeting"].keys(), reverse=True)
    have_windows = sorted(inventory["window"].keys())
    have_months = sorted(inventory["month"].keys(), reverse=True)

    if not (have_meetings or have_windows or have_months):
        st.warning(
            "No unified backtest JSONs yet. Click **[ Rebuild Unified Backtest ]** "
            "in the sidebar to build them from existing predictions + results."
        )
        return

    period_options = []
    if "all" in have_windows:
        period_options.append(("all", "All Time"))
    for w in ("last7", "last30", "last90"):
        if w in have_windows:
            period_options.append((w, {"last7": "Last 7 Days",
                                       "last30": "Last 30 Days",
                                       "last90": "Last 90 Days"}[w]))
    for ym in have_months:
        period_options.append((f"month-{ym}", f"Month {ym}"))
    for w in have_windows:
        if w.startswith("season-"):
            period_options.append((w, w.replace("season-", "Season ")))
    for dc in have_meetings:
        d = f"{dc[:4]}-{dc[4:6]}-{dc[6:]}"
        period_options.append((dc, f"Meeting {d}"))

    keys = [k for k, _ in period_options]
    labels = {k: lbl for k, lbl in period_options}
    sel_period = st.selectbox(
        "Period:",
        keys,
        format_func=lambda k: labels[k],
        key="bt_period_sel",
        index=0,
    )

    data = _load_unified_backtest(sel_period)
    if not data:
        st.error(f"Could not load unified backtest for `{sel_period}`.")
        return

    _render_unified_backtest(data, prefix=f"u_{sel_period}_")

    # Footer: download
    st.markdown("---")
    json_blob = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button(
        "[ Download Unified Backtest JSON ]",
        json_blob,
        file_name=f"backtest_unified_{sel_period}.json",
        mime="application/json",
        key=f"dl_bt_{sel_period}",
    )


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


def _auto_sync_results_to_db(verbose: bool = False) -> tuple[int, int]:
    """Backfill any reports/results_*.json files missing from hkjc.db.

    This guards against the recurring failure mode where the post-race
    pipeline scraped a results JSON but the DB-append step never ran
    (e.g. the pipeline was interrupted, the safety-net was added later,
    or a meeting was scraped from a different machine and only the JSON
    was synced via git). Form Guide and Race Lookup both read from
    hkjc.db, so any missing date silently disappears from the form lines
    of downstream race cards.

    Strategy: for every results_*.json on disk, check if its race_date is
    already present in hkjc.db. If not, call ``db_utils.append_results_to_db``
    on it. ``append_results_to_db`` is itself idempotent, so re-running it
    over already-imported dates is safe — but we skip them anyway to keep
    the no-op path cheap.

    Returns ``(n_meetings_synced, n_rows_appended)``. Silent if nothing
    to do; callers may surface the count via st.toast / st.info.
    """
    try:
        from db_utils import append_results_to_db, read_sqlite
    except Exception:
        return (0, 0)
    results_dir = BASE / "reports"
    if not results_dir.exists():
        return (0, 0)
    try:
        df = read_sqlite("SELECT DISTINCT race_date FROM results")
        have = {str(d)[:10] for d in df["race_date"].astype(str)}
    except Exception:
        have = set()
    n_meetings = 0
    n_rows = 0
    for fp in sorted(results_dir.glob("results_*.json")):
        stem = fp.stem.replace("results_", "")
        if len(stem) != 8 or not stem.isdigit():
            continue
        iso = f"{stem[:4]}-{stem[4:6]}-{stem[6:]}"
        if iso in have:
            continue
        try:
            n = append_results_to_db(fp, verbose=verbose)
            if n:
                n_meetings += 1
                n_rows += int(n)
                have.add(iso)
        except Exception:
            continue
    # A freshly-synced past meeting invalidates the pace index and every
    # upcoming form-guide cache — rebuild them so the new results actually
    # surface in race-day analysis.
    if n_meetings:
        try:
            _rebuild_caches_after_sync()
        except Exception:
            pass
    return (n_meetings, n_rows)


def _upcoming_racecard_dates() -> list[str]:
    """ISO dates of racecards on disk for today or later (upcoming meetings).

    These are the cards whose form lines depend on freshly-synced past
    results, so their form-guide caches must be rebuilt after a DB sync.
    """
    import datetime as _dt
    today = _dt.date.today()
    out: list[str] = []
    rc_dir = BASE / "racecards"
    if not rc_dir.exists():
        return out
    for fp in rc_dir.glob("racecard_*.xlsx"):
        stem = fp.stem.replace("racecard_", "")
        if len(stem) != 8 or not stem.isdigit():
            continue
        try:
            d = _dt.date(int(stem[:4]), int(stem[4:6]), int(stem[6:]))
        except ValueError:
            continue
        if d >= today:
            out.append(f"{stem[:4]}-{stem[4:6]}-{stem[6:]}")
    return sorted(out)


def _rebuild_caches_after_sync() -> list[str]:
    """Rebuild pace index + upcoming form-guide caches after a DB sync.

    A freshly-synced past meeting changes the form lines of every upcoming
    racecard, so the pace index and form-guide JSON caches must be rebuilt or
    the new results won't surface in race-day analysis. Returns short status
    strings for toasting.
    """
    import subprocess as _sp2
    msgs: list[str] = []
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = _sp2.run([PYTHON, str(BASE / "build_pace_index.py")],
                     capture_output=True, text=True, timeout=600,
                     cwd=str(BASE), env=env)
        msgs.append("pace ✓" if r.returncode == 0 else "pace ✗")
    except (OSError, _sp2.TimeoutExpired):
        msgs.append("pace ✗")
    for iso in _upcoming_racecard_dates():
        try:
            r = _sp2.run([PYTHON, str(BASE / "build_form_guide.py"), iso],
                         capture_output=True, text=True, timeout=600,
                         cwd=str(BASE), env=env)
            msgs.append(f"form {iso} ✓" if r.returncode == 0
                        else f"form {iso} ✗")
        except (OSError, _sp2.TimeoutExpired):
            msgs.append(f"form {iso} ✗")
        # Form screen (notable mentions + head-to-head) also depends on the
        # freshly-synced results, so regenerate it for the upcoming card.
        try:
            r = _sp2.run([PYTHON, str(BASE / "form_screener.py"),
                          "--date", iso],
                         capture_output=True, text=True, timeout=600,
                         cwd=str(BASE), env=env)
            msgs.append(f"screen {iso} ✓" if r.returncode == 0
                        else f"screen {iso} ✗")
        except (OSError, _sp2.TimeoutExpired):
            msgs.append(f"screen {iso} ✗")
    return msgs


def _db_pending_meetings() -> list[str]:
    """ISO dates with a reports/results_*.json that are missing from hkjc.db."""
    try:
        from db_utils import read_sqlite
    except Exception:
        return []
    results_dir = BASE / "reports"
    if not results_dir.exists():
        return []
    try:
        df = read_sqlite("SELECT DISTINCT race_date FROM results")
        have = {str(d)[:10] for d in df["race_date"].astype(str)}
    except Exception:
        have = set()
    pending: list[str] = []
    for fp in sorted(results_dir.glob("results_*.json")):
        stem = fp.stem.replace("results_", "")
        if len(stem) != 8 or not stem.isdigit():
            continue
        iso = f"{stem[:4]}-{stem[4:6]}-{stem[6:]}"
        if iso not in have:
            pending.append(iso)
    return pending


def _maybe_auto_sync_results(context_key: str) -> None:
    """Auto-sync results→DB once per page/session when pending meetings exist.

    This keeps Race Day / Form Guide / Results self-healing after a restart:
    if reports/results_*.json are present but the DB or form-guide caches are
    stale, sync before the page consumes cached analysis state.
    """
    try:
        pending = _db_pending_meetings()
    except Exception:
        pending = []
    sig = tuple(pending)
    state_key = f"_autosync_results_{context_key}"
    if not sig:
        st.session_state[state_key] = ()
        return
    if st.session_state.get(state_key) == sig:
        return
    st.session_state[state_key] = sig
    try:
        n_mtg, n_rows = _auto_sync_results_to_db(verbose=False)
    except Exception:
        return
    if not n_mtg:
        return
    try:
        _load_form_db.clear()
    except Exception:
        pass
    try:
        st.cache_data.clear()
    except Exception:
        pass
    st.toast(f"Auto-synced {n_mtg} meeting(s) to DB ({n_rows} rows).", icon="🔄")
    st.rerun()


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


def _run_commentary_and_formguide(date_str: str) -> None:
    """Re-run steps 6-7 of the post-race pipeline without touching the DB scrape.

    Safe to call when results are already scraped (no network scraping, no risk
    of duplicate DB rows).  Runs:
        - DB belt-and-braces append (idempotent — skips if date already present)
        - Step 6: build_form_guide.py → cache/form_guide_YYYY-MM-DD.json
        - Step 7: race_commentary.py  → reports/commentary_YYYYMMDD.json
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    date_compact = date_str.replace("-", "")

    # Belt-and-braces DB append (idempotent — skips if already in DB).
    results_json = REPORTS / f"results_{date_compact}.json"
    if results_json.exists():
        try:
            from db_utils import append_results_to_db, read_sqlite
            df_chk = read_sqlite(
                f"SELECT COUNT(*) n FROM results WHERE race_date='{date_str}'"
            )
            already = int(df_chk.iloc[0, 0]) if not df_chk.empty else 0
            if already == 0:
                n_db = append_results_to_db(results_json, verbose=False)
                if n_db:
                    st.info(f"DB: appended {n_db} rows for {date_str}")
            else:
                st.info(f"DB: {already} rows already present for {date_str} — skipped")
        except Exception as e:
            st.warning(f"DB append skipped: {e}")
    else:
        st.warning(f"No results JSON found for {date_str} — DB append skipped. "
                   "Run 'Scrape Results' first.")

    steps = [
        ("Rebuild form guide (with lane data)",
         [PYTHON, str(BASE / "build_form_guide.py"), date_str]),
        ("Race commentary",
         [PYTHON, str(BASE / "race_commentary.py"), "--date", date_str]),
    ]
    progress = st.progress(0.0, text=f"Commentary + form guide for {date_str}…")
    outputs: list[tuple[str, int, str]] = []
    for i, (label, cmd) in enumerate(steps, 1):
        progress.progress((i - 1) / len(steps), text=f"{label}…")
        try:
            result = subprocess.run(
                cmd, env=env, cwd=str(BASE),
                capture_output=True, text=True, encoding="utf-8", timeout=300,
            )
            tail = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
            outputs.append((label, result.returncode, tail[-1500:]))
        except subprocess.TimeoutExpired as e:
            outputs.append((label, -1, f"TIMEOUT after {e.timeout}s"))
        except Exception as e:
            outputs.append((label, -2, f"EXCEPTION: {e}"))
    progress.progress(1.0, text="Done.")

    n_ok = sum(1 for _, rc, _ in outputs if rc == 0)
    if n_ok == len(steps):
        st.success(f"Commentary + form guide regenerated for {date_str}")
    else:
        st.warning(f"Partial: {n_ok}/{len(steps)} steps succeeded")
    for label, rc, tail in outputs:
        icon = "✅" if rc == 0 else "❌"
        with st.expander(f"{icon} {label} (exit {rc})", expanded=(rc != 0)):
            st.code(tail or "(no output)")


def _run_results_scraper(date_str: str, *, full: bool = False):
    """Invoke results scraper from the dashboard.

    *full=False*  (Live Feed): lightweight ``scrape_hkjc_results.py`` only.
    *full=True*   (Results page / post-race): runs the *full* post-race pipeline:
        1. ``scrape_hkjc_results.py`` → ``reports/results_YYYYMMDD.json``
        2. ``scrape_hkjc.py``         → merged into ``hkjc_results_updated.xlsx``
        3. ``scrape_hkjc_incident_reports.py`` → ``reports/incidents_YYYYMMDD.json``
        4. ``scrape_hkjc_rp_photos.py`` → ``running_position_photos/YYYYMMDD/R*.jpg``
        5. ``parse_rp_photos.py``     → ``running_position_photos/YYYYMMDD/R*.json`` (OCR)
        6. ``build_form_guide.py``    → refresh ``cache/form_guide_YYYY-MM-DD.json`` with lane data
        7. ``race_commentary.py``     → ``reports/commentary_YYYYMMDD.json``
        8. ``backtest_model.py``      → ``reports/backtest_YYYYMMDD.json`` (model evaluation)
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    if full:
        date_compact = date_str.replace("-", "")
        dd_mm_yyyy = f"{date_str[8:10]}/{date_str[5:7]}/{date_str[:4]}"
        horse_cache = Path(tempfile.gettempdir()) / "horse_cache_hkjc.json"
        # OneDrive locks xlsx files that live under the workspace folder —
        # write to TEMP then copy into the workspace after the scrape succeeds.
        full_scrape_tmp = Path(tempfile.gettempdir()) / "hkjc_results.xlsx"

        steps = [
            ("1/8 Results JSON",
             [PYTHON, str(BASE / "scrape_hkjc_results.py"), "--date", date_str]),
            ("2/8 Full DB scrape (sectionals, horse profiles)",
             [PYTHON, str(BASE / "scrape_hkjc.py"),
              "--dates", dd_mm_yyyy,
              "--horse-cache", str(horse_cache),
              "--no-cache",
              "--output", str(full_scrape_tmp)]),
            ("3/8 Incident reports",
             [PYTHON, str(BASE / "scrape_hkjc_incident_reports.py"), "--date", date_str]),
            ("4/8 Running-position photos",
             [PYTHON, str(BASE / "scrape_hkjc_rp_photos.py"), "--date", date_str]),
            ("5/8 OCR running-position photos → lane JSON",
             [PYTHON, str(BASE / "parse_rp_photos.py"), "--date", date_str]),
            ("6/8 Rebuild form guide (with lane data)",
             [PYTHON, str(BASE / "build_form_guide.py"), date_str]),
            ("7/8 Race commentary",
             [PYTHON, str(BASE / "race_commentary.py"), "--date", date_str]),
            ("8/8 Backtest model vs actual",
             [PYTHON, str(BASE / "backtest_model.py"), "--date", date_str]),
        ]

        outputs: list[tuple[str, int, str]] = []  # (label, rc, tail)
        progress = st.progress(0.0, text=f"Post-race pipeline for {date_str}…")
        for i, (label, cmd) in enumerate(steps, 1):
            progress.progress((i - 1) / len(steps), text=f"{label}…")
            try:
                result = subprocess.run(
                    cmd, env=env, cwd=str(BASE),
                    capture_output=True, text=True, encoding="utf-8",
                    timeout=900,
                )
                tail = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
                outputs.append((label, result.returncode, tail[-1500:]))
            except subprocess.TimeoutExpired as e:
                outputs.append((label, -1, f"TIMEOUT after {e.timeout}s"))
            except Exception as e:  # pragma: no cover
                outputs.append((label, -2, f"EXCEPTION: {e}"))
        progress.progress(1.0, text="Done.")

        # Step 2 (full DB scrape) writes the xlsx to TEMP — merge if present
        if full_scrape_tmp.exists() and outputs[1][1] == 0:
            try:
                _merge_full_scrape_to_db(full_scrape_tmp, date_str)
            except Exception as e:
                st.warning(f"Merge to main DB failed: {e}")

        # v4.9: Belt-and-braces DB-append. Step 1 (scrape_hkjc_results.py)
        # already calls db_utils.append_results_to_db, but if that step
        # failed/timed-out the master DB would silently miss this date.
        # Force-append from the results JSON if it landed on disk.
        date_compact = date_str.replace("-", "")
        results_json = REPORTS / f"results_{date_compact}.json"
        if results_json.exists():
            try:
                from db_utils import append_results_to_db
                n_db = append_results_to_db(results_json, verbose=False)
                if n_db:
                    st.info(f"DB safety-net: appended {n_db} rows from results_{date_compact}.json")
            except Exception as e:
                st.error(f"DB safety-net append failed for {date_str}: {e}")

        # Rebuild measured race-pace cache after DB/full-scrape merge, before
        # upcoming form guides are rebuilt from it.
        try:
            _rp = subprocess.run(
                [PYTHON, str(BASE / "build_pace_index.py")],
                env=env, cwd=str(BASE), capture_output=True,
                text=True, encoding="utf-8", timeout=600,
            )
            if _rp.returncode == 0:
                st.info("Measured race-pace index rebuilt.")
            else:
                st.warning("Pace-index rebuild failed: " + ((_rp.stderr or _rp.stdout or "")[-300:]))
        except Exception as e:
            st.warning(f"Pace-index rebuild skipped: {e}")

        # v4.9: Auto-rebuild form guides for any upcoming meeting (next 14 days)
        # so freshly-ingested results show up on Race Card / Form Guide tabs
        # without the user having to re-run pre-race pipeline.
        try:
            import datetime as _dt
            today = _dt.date.fromisoformat(date_str)
            rebuilt = []
            for rc_path in sorted((BASE / "racecards").glob("racecard_*.xlsx")):
                try:
                    dc = rc_path.stem.split("_", 1)[1]
                    mtg = _dt.date(int(dc[:4]), int(dc[4:6]), int(dc[6:8]))
                except Exception:
                    continue
                if mtg <= today:
                    continue
                if (mtg - today).days > 14:
                    continue
                mtg_iso = mtg.isoformat()
                _r = subprocess.run(
                    [PYTHON, str(BASE / "build_form_guide.py"), mtg_iso],
                    env=env, cwd=str(BASE),
                    capture_output=True, text=True, encoding="utf-8", timeout=300,
                )
                if _r.returncode == 0:
                    rebuilt.append(mtg_iso)
            if rebuilt:
                st.info(f"Form guides rebuilt for upcoming meetings: {', '.join(rebuilt)}")
                try:
                    _load_form_db.clear()
                except Exception:
                    pass
        except Exception as e:
            st.warning(f"Upcoming form-guide rebuild skipped: {e}")

        # Summary
        n_ok = sum(1 for _, rc, _ in outputs if rc == 0)
        if n_ok == len(steps):
            st.success(f"Full post-race pipeline complete ({n_ok}/{len(steps)}) for {date_str}")
        else:
            st.warning(f"Pipeline partial: {n_ok}/{len(steps)} steps succeeded for {date_str}")

        for label, rc, tail in outputs:
            icon = "✅" if rc == 0 else "❌"
            with st.expander(f"{icon} {label} (exit {rc})", expanded=(rc != 0)):
                st.code(tail or "(no output)")

        # ── Persist post-race artifacts to GitHub on Streamlit Cloud ──
        if _is_streamlit_cloud():
            try:
                n_pushed, n_miss, errors = _gh_persist_postrace_outputs(date_str)
                if n_pushed:
                    st.success(f"☁ Synced {n_pushed} post-race file(s) to GitHub "
                               f"(skipped {n_miss} missing).")
                elif errors:
                    st.warning("⚠ Post-race outputs were NOT synced to GitHub: "
                               + "; ".join(errors[:3]))
                else:
                    st.info("ℹ No post-race outputs synced "
                            "(none generated, or no GITHUB_TOKEN).")
            except Exception as e:
                st.warning(f"⚠ GitHub sync failed: {e}")
        return

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
            # If running-position photos already exist but lack OCR (or were
            # stubbed before results existed), re-run OCR now so lane data
            # becomes available immediately.
            rp_dir = BASE / "running_position_photos" / date_compact
            if rp_dir.exists() and any(rp_dir.glob("R*.jpg")):
                need_ocr = False
                for jpg in rp_dir.glob("R*.jpg"):
                    js = jpg.with_suffix(".json")
                    if not js.exists():
                        need_ocr = True; break
                    try:
                        _j = json.loads(js.read_text(encoding="utf-8"))
                        if (_j.get("meta", {}) or {}).get("field_size", 0) == 0:
                            need_ocr = True; break
                    except Exception:
                        need_ocr = True; break
                if need_ocr:
                    with st.spinner("Re-OCR running-position photos against refreshed roster…"):
                        _r = subprocess.run(
                            [PYTHON, str(BASE / "parse_rp_photos.py"),
                             "--date", date_str, "--force"],
                            env=env, cwd=str(BASE),
                            capture_output=True, text=True, encoding="utf-8", timeout=600,
                        )
                    if _r.returncode == 0:
                        st.info("Running-lane OCR refreshed.")
                        # rebuild form guide so Race Card shows updated lanes
                        try:
                            subprocess.run(
                                [PYTHON, str(BASE / "build_form_guide.py"), date_str],
                                env=env, cwd=str(BASE),
                                capture_output=True, text=True, encoding="utf-8", timeout=600,
                            )
                        except Exception:
                            pass
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
    """Run backtest for a single meeting (legacy ET + unified ET+SARR+market)."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    with st.spinner(f"Running backtest for {date_str}..."):
        # Legacy ET-only (kept for monthly/seasonal aggregates)
        legacy = subprocess.run(
            [PYTHON, str(BASE / "backtest_model.py"), "--date", date_str],
            env=env, cwd=str(BASE), capture_output=True, text=True, encoding="utf-8")
        # New unified ET+SARR+market
        unified = subprocess.run(
            [PYTHON, str(BASE / "backtest_unified.py"), "--date", date_str],
            env=env, cwd=str(BASE), capture_output=True, text=True, encoding="utf-8")
        ok = (legacy.returncode == 0) and (unified.returncode == 0)
        if ok:
            st.success(f"Backtest complete for {date_str}")
            with st.expander("Output"):
                st.code((legacy.stdout or "") + "\n--- unified ---\n" + (unified.stdout or ""))
        else:
            st.error(f"Backtest failed (legacy={legacy.returncode}, unified={unified.returncode})")
            with st.expander("Error"):
                st.code((legacy.stderr or legacy.stdout or "")[-2000:]
                        + "\n--- unified ---\n"
                        + (unified.stderr or unified.stdout or "")[-2000:])


def _run_backtest_unified_all():
    """Generate every unified per-meeting JSON + standard windows + per-month."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    with st.spinner("Building unified backtest (all meetings + windows)..."):
        result = subprocess.run(
            [PYTHON, str(BASE / "backtest_unified.py"), "--all"],
            env=env, cwd=str(BASE), capture_output=True, text=True, encoding="utf-8")
    if result.returncode == 0:
        st.success("Unified backtest built")
        with st.expander("Output"):
            st.code(result.stdout or "(no output)")
        # Push every unified JSON we just produced to GitHub on cloud.
        if _is_streamlit_cloud() and _gh_headers():
            pushed = 0
            for p in REPORTS.glob("backtest_unified_*.json"):
                try:
                    if _gh_push_file(f"reports/{p.name}", p.read_bytes(),
                                     "backtest_unified: rebuild [skip ci]"):
                        pushed += 1
                except Exception:
                    pass
            if pushed:
                st.caption(f"Pushed {pushed} unified backtest JSONs to GitHub.")
    else:
        st.error("Unified backtest failed")
        with st.expander("Error"):
            st.code(result.stderr or result.stdout or "(no output)")


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

                st.dataframe(styled_tbl, width='stretch', hide_index=True,
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
                    st.dataframe(pd.DataFrame(p_rows), width='stretch', hide_index=True)

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
        # ── Subsequent-run ROI (real dividends) ──
        st.markdown("### Subsequent-run ROI — would 1u flat-stake on every BB pick have been profitable?")
        st.caption("Walks every BB entry's runs since `added_date` and prices each at real HKJC "
                     "WIN/PLACE dividends (SP fallback). 50/50 = ½u WIN + ½u PLACE per run.")
        bb_perf_path = REPORTS / "blackbook_performance.json"
        bb_chart_path = REPORTS / "blackbook_roi_chart.png"
        col_a, col_b = st.columns([1, 4])
        with col_a:
            if st.button("🔄 Recompute", key="bb_roi_refresh",
                            help="Re-runs analyze_blackbook.py against the live DB."):
                with st.spinner("Crunching subsequent runs…"):
                    try:
                        import analyze_blackbook
                        import importlib
                        importlib.reload(analyze_blackbook)
                        analyze_blackbook.analyse()
                        st.success("Refreshed.")
                    except Exception as exc:
                        st.error(f"Failed: {exc}")
        if bb_perf_path.exists():
            try:
                bb_perf = json.loads(bb_perf_path.read_text(encoding="utf-8"))
            except Exception as exc:
                bb_perf = None
                st.warning(f"Could not load blackbook_performance.json: {exc}")
        else:
            bb_perf = None
            st.info("Click **Recompute** to generate the first ROI snapshot.")

        if bb_perf:
            g = bb_perf.get("grand", {})
            ua = bb_perf.get("user_actual", {})
            with col_b:
                st.caption(f"Snapshot: {bb_perf.get('generated_at','?')}  · "
                              f"{g.get('horses_with_runs',0)}/{g.get('blackbook_size',0)} horses with subsequent runs")
            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("Total runs tracked", g.get("total_runs", 0))
            mc2.metric("WIN strike",
                          f"{(g.get('win_strike_rate') or 0)*100:.1f}%",
                          delta=f"ROI {(g.get('win_roi') or 0)*100:+.1f}%")
            mc3.metric("PLACE strike",
                          f"{(g.get('pla_strike_rate') or 0)*100:.1f}%",
                          delta=f"ROI {(g.get('pla_roi') or 0)*100:+.1f}%")
            mc4.metric("50/50 ROI",
                          f"{(g.get('split_50_50_roi') or 0)*100:+.1f}%",
                          help="½u WIN + ½u PLACE per run, real dividends, SP fallback for missing.")

            if bb_chart_path.exists():
                st.image(str(bb_chart_path), width='stretch')

            with st.expander("By confidence cohort", expanded=False):
                rows = []
                for c, v in (bb_perf.get("by_confidence") or {}).items():
                    rows.append({
                        "Confidence": c.title(), "Horses": v.get("horses", 0),
                        "Runs": v.get("runs", 0),
                        "WIN hits": v.get("win_hits", 0), "PLA hits": v.get("pla_hits", 0),
                        "WIN ROI": f"{(v.get('win_roi') or 0)*100:+.1f}%",
                        "PLA ROI": f"{(v.get('pla_roi') or 0)*100:+.1f}%",
                    })
                st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

            with st.expander("Per-horse verdicts (KEEP / WATCH / EXPIRE)", expanded=False):
                horses = bb_perf.get("horses", [])
                from collections import Counter as _Cnt
                vc = _Cnt(h.get("verdict") for h in horses)
                st.caption(f"KEEP={vc.get('KEEP',0)} · WATCH={vc.get('WATCH',0)} · "
                              f"EXPIRE={vc.get('EXPIRE',0)} · NO_RUNS={vc.get('NO_RUNS',0)}")
                tab_k, tab_e = st.tabs([f"KEEP ({vc.get('KEEP',0)})",
                                              f"EXPIRE candidates ({vc.get('EXPIRE',0)})"])
                with tab_k:
                    rows = []
                    for h in sorted([x for x in horses if x.get("verdict") == "KEEP"],
                                       key=lambda x: (x.get("pla_roi") or 0), reverse=True):
                        rows.append({
                            "Horse": h["horse"], "Conf": (h.get("confidence") or "").title(),
                            "Runs": h["runs"], "WIN": h["win_hits"], "PLA": h["pla_hits"],
                            "WIN ROI": f"{(h.get('win_roi') or 0)*100:+.1f}%",
                            "PLA ROI": f"{(h.get('pla_roi') or 0)*100:+.1f}%",
                            "Tags": ", ".join(h.get("tags") or []),
                        })
                    st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)
                with tab_e:
                    rows = []
                    for h in sorted([x for x in horses if x.get("verdict") == "EXPIRE"],
                                       key=lambda x: (x.get("pla_roi") or 0)):
                        rows.append({
                            "Horse": h["horse"], "Conf": (h.get("confidence") or "").title(),
                            "Runs": h["runs"], "PLA": h["pla_hits"],
                            "PLA ROI": f"{(h.get('pla_roi') or 0)*100:+.1f}%",
                            "Reasoning": h.get("reasoning", ""),
                        })
                    st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

            with st.expander("Cross-check: BB vs your actual bookie bets", expanded=False):
                if ua.get("settled_bets"):
                    u1, u2, u3, u4 = st.columns(4)
                    u1.metric("Your settled bets", ua.get("settled_bets", 0))
                    u2.metric("Your hit rate",
                                 f"{(ua.get('user_hit_rate') or 0)*100:.1f}%")
                    u3.metric("Your ROI",
                                 f"{(ua.get('user_roi') or 0)*100:+.1f}%",
                                 delta=f"PnL ${ua.get('user_pnl',0):+,.0f}")
                    u4.metric("BB-overlap bets", ua.get("bb_overlap_count", 0),
                                 delta=f"PnL attr ${ua.get('bb_overlap_pnl_attr',0):+,.0f}")
                    examples = ua.get("bb_overlap_examples") or []
                    if examples:
                        st.caption("Where a BB horse appeared in one of your tickets:")
                        st.dataframe(pd.DataFrame(examples), width='stretch', hide_index=True)
                else:
                    st.caption("No settled user bets yet — upload bookie statements via My Bets.")
        st.markdown("---")

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
            st.dataframe(pd.DataFrame(t_rows), width='stretch', hide_index=True)

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
            st.dataframe(pd.DataFrame(c_rows), width='stretch', hide_index=True)

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

def _signal_review(pred_data: dict, results_races: list) -> None:
    """Render a per-meeting Signal Effectiveness Review.

    Compares each pick's model signals against actual finishing position to
    show which signals (model rank, smap_total_adj, vet_flag, trial_flag,
    risk_tier, predicted pace) actually worked on this card.
    """
    import pandas as _pd

    # Build {race_no: {horse_name_upper: place_int}} from results
    place_map: dict = {}
    field_size_by_race: dict = {}
    for r in results_races:
        rn = r.get("race_number")
        if rn is None:
            continue
        runners = r.get("runners") or []
        field_size_by_race[rn] = len(runners)
        m = {}
        for ru in runners:
            try:
                pl = int(str(ru.get("place", "")).strip())
            except (TypeError, ValueError):
                pl = None
            if pl is not None and ru.get("horse_name"):
                m[str(ru["horse_name"]).strip().upper()] = pl
        place_map[rn] = m

    # Iterate model picks → score signals
    rank1_top1 = rank1_top3 = top3_winner = pace_match = 0
    rank1_n = top3_n = pace_n = 0
    smap_neg_top_half = smap_neg_n = 0
    trial_plus_top_half = trial_plus_n = 0
    vet_clean_top_half = vet_clean_n = 0
    vet_flagged_top_half = vet_flagged_n = 0
    detail_rows: list = []

    try:
        from pace_utils import pace_band_distance as _pbd
    except Exception:
        _pbd = None

    for race in pred_data.get("races", []) or []:
        rn = race.get("race_number")
        picks = race.get("picks") or []
        places = place_map.get(rn, {})
        fs = field_size_by_race.get(rn, len(picks)) or len(picks) or 12
        if not places:
            continue

        # Pace label match
        _pred_lbl = race.get("pace")
        _act_lbl = next(
            (rr.get("actual_pace_label") for rr in results_races
             if rr.get("race_number") == rn),
            None,
        )
        if _pred_lbl and _act_lbl and _act_lbl != "N/A":
            pace_n += 1
            if _pbd is not None:
                d = _pbd(_pred_lbl, _act_lbl)
                if d is not None and d <= 1:
                    pace_match += 1
            elif _pred_lbl == _act_lbl:
                pace_match += 1

        # Per-pick signal evaluation
        for p in picks:
            nm = (p.get("horse_name") or "").strip().upper()
            if not nm or nm not in places:
                continue
            place = places[nm]
            top1 = (place == 1)
            top3 = (place <= 3)
            top_half = (place <= max(1, fs // 2))
            rank = p.get("rank")
            smap = p.get("smap_total_adj")
            vet = (p.get("vet_flag") or "").upper()
            trial = (p.get("trial_flag") or "").strip()

            if rank == 1:
                rank1_n += 1
                if top1:
                    rank1_top1 += 1
                if top3:
                    rank1_top3 += 1
            if rank in (1, 2, 3):
                top3_n += 1
                if top1:
                    top3_winner += 1

            if isinstance(smap, (int, float)) and smap <= -0.10:
                smap_neg_n += 1
                if top_half:
                    smap_neg_top_half += 1

            if trial == "+":
                trial_plus_n += 1
                if top_half:
                    trial_plus_top_half += 1

            if vet in ("AMBER", "RED"):
                vet_flagged_n += 1
                if top_half:
                    vet_flagged_top_half += 1
            elif vet in ("GREEN", "", "—"):
                vet_clean_n += 1
                if top_half:
                    vet_clean_top_half += 1

            detail_rows.append({
                "R":       rn,
                "Rk":      rank,
                "Horse":   p.get("horse_name", ""),
                "Place":   place,
                "Top1":    "✅" if top1 else "—",
                "Top3":    "✅" if top3 else "—",
                "smap":    f"{smap:+.2f}" if isinstance(smap, (int, float)) else "—",
                "Vet":     vet or "—",
                "Trial":   trial or "—",
                "Risk":    p.get("risk_tier", "?"),
                "Style":   p.get("style", "?"),
                "ESZ":     f"{p.get('early_speed_z', 0):+.2f}",
            })

    def _pct(n, d):
        return f"{(n/d)*100:.0f}% ({n}/{d})" if d else "—"

    summary_rows = [
        {"Signal": "Model rank-1 → won (Top1)",
         "Hit Rate": _pct(rank1_top1, rank1_n),
         "What it means": "Did the model's #1 pick win each race?"},
        {"Signal": "Model rank-1 → placed (Top3)",
         "Hit Rate": _pct(rank1_top3, rank1_n),
         "What it means": "Did the model's #1 pick finish top-3?"},
        {"Signal": "Any of model top-3 → won",
         "Hit Rate": _pct(top3_winner, max(1, len(pred_data.get('races', [])))),
         "What it means": "Did any of model picks 1-3 win?"},
        {"Signal": "Pace label match (within 1 band)",
         "Hit Rate": _pct(pace_match, pace_n),
         "What it means": "Did predicted pace match actual within ±1 band?"},
        {"Signal": "smap_total_adj ≤ −0.10 (favourable)",
         "Hit Rate": _pct(smap_neg_top_half, smap_neg_n),
         "What it means": "Did sec-map-favoured picks finish in the top half?"},
        {"Signal": "Trial flag '+' (positive prior)",
         "Hit Rate": _pct(trial_plus_top_half, trial_plus_n),
         "What it means": "Did + trial-flagged picks finish in the top half?"},
        {"Signal": "Vet flag GREEN (clean)",
         "Hit Rate": _pct(vet_clean_top_half, vet_clean_n),
         "What it means": "Did vet-clean picks finish in the top half?"},
        {"Signal": "Vet flag AMBER/RED (warning)",
         "Hit Rate": _pct(vet_flagged_top_half, vet_flagged_n),
         "What it means": "Did vet-flagged picks STILL finish top half? (lower = signal worked)"},
    ]

    st.markdown("#### Signal hit-rate summary (this meeting)")
    st.dataframe(_pd.DataFrame(summary_rows), hide_index=True,
                 width='stretch')

    st.markdown("#### Per-pick detail")
    if detail_rows:
        df_d = _pd.DataFrame(detail_rows).sort_values(["R", "Rk"])
        st.dataframe(df_d, hide_index=True, width='stretch')
    else:
        st.info("No overlap between model picks and result runners — "
                "results may still be incomplete.")


def page_results():
    st.markdown('<div class="page-title">Race Results</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">Browse scraped results &middot; add horses to Blackbook</div>', unsafe_allow_html=True)

    _maybe_auto_sync_results("results")

    # Sidebar: scrape + date select
    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
    st.sidebar.markdown('<div class="sb-nav-section">Results Data</div>', unsafe_allow_html=True)
    scrape_date = st.sidebar.date_input("Date", value=date.today(), key="res_scrape_date")
    if st.sidebar.button("[ Scrape Results ]", width='stretch', key="res_btn_scrape"):
        _run_results_scraper(scrape_date.isoformat(), full=True)
        st.cache_data.clear()
        # Form DB lives on cache_resource — clear explicitly because the
        # post-race pipeline rewrites the underlying xlsx.
        try:
            _load_form_db.clear()
        except Exception:
            pass
        st.rerun()
    if st.sidebar.button("[ Re-run Commentary + Form Guide ]", width='stretch',
                         key="res_btn_rerun_commentary",
                         help="Re-runs only steps 6-7 (form guide rebuild + commentary) "
                              "and the DB belt-and-braces append — safe when results "
                              "are already scraped. No network scraping."):
        _run_commentary_and_formguide(scrape_date.isoformat())
        st.cache_data.clear()
        st.rerun()

    res_dates = find_results_dates()
    if not res_dates:
        st.info("No scraped results found. Use the sidebar to scrape results for a date.")
        return

    # ── DB-integrity check (#2): results-on-disk vs hkjc.db ──────────────
    _render_db_integrity_panel()

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

    # ── Meeting-level pace strip (all races at a glance) ────────────────
    _has_pace = any(r.get("actual_pace_label") and r["actual_pace_label"] != "N/A"
                    for r in races)
    if _has_pace:
        _lbl_col = {
            "Very Fast": "#d73a49", "Fast": "#e85d75",
            "Slightly Fast": "#f0a0a0", "Normal": "#6a737d",
            "Slightly Slow": "#a0c0f0", "Slow": "#5d8fe8", "Very Slow": "#3a69d7",
        }
        _cells = []
        for _r in races:
            _rn = _r.get("race_number", "?")
            _lbl = _r.get("actual_pace_label") or "N/A"
            _dev = _r.get("actual_dev")
            _ws = _r.get("actual_winner_style") or ""
            _c = _lbl_col.get(_lbl, "#444")
            _dev_s = f"{_dev:+.2f}" if isinstance(_dev, (int, float)) else "—"
            _cells.append(
                f"<div style='flex:0 0 auto;min-width:78px;padding:6px 8px;"
                f"border:1px solid #30363d;border-radius:4px;background:#0d1117;"
                f"text-align:center;font-size:11px'>"
                f"<div style='font-weight:700;font-size:12px'>R{_rn}</div>"
                f"<div style='color:{_c};font-weight:600;margin-top:2px'>{_lbl}</div>"
                f"<div style='opacity:0.65'>{_dev_s}s</div>"
                f"<div style='opacity:0.55;font-size:10px'>{_ws}</div>"
                f"</div>"
            )
        st.markdown(
            "<div style='margin-bottom:12px'>"
            "<div style='font-size:0.85em;opacity:0.7;margin-bottom:4px'>"
            "Meeting pace profile (actual, HKJC-anchored)</div>"
            f"<div style='display:flex;gap:6px;flex-wrap:wrap'>{''.join(_cells)}</div>"
            "</div>",
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
    predicted_pace_by_race = {}
    if pred_path.exists():
        try:
            with open(pred_path, "r", encoding="utf-8") as f:
                pred_data = json.load(f)
            for race in pred_data.get("races", []):
                for pick in race.get("picks", []):
                    model_ranks[(race["race_number"],
                                 pick["horse_name"].upper())] = pick["rank"]
                predicted_pace_by_race[race["race_number"]] = {
                    "pace": race.get("pace"),
                    "pace_score": race.get("pace_score"),
                }
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
                         width='stretch',
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

    # ── Race pace banner (actual vs predicted) ────────────────────────────
    _pred_pace = predicted_pace_by_race.get(race.get("race_number"), {})
    _pred_lbl = _pred_pace.get("pace")
    _pred_sc = _pred_pace.get("pace_score")
    _act_lbl = race.get("actual_pace_label")
    _act_dev = race.get("actual_dev")
    _winner_style = race.get("actual_winner_style")
    if _act_lbl and _act_lbl != "N/A":
        _lbl_colour = {
            "Very Fast": "#d73a49", "Fast": "#e85d75",
            "Slightly Fast": "#f0a0a0", "Normal": "#6a737d",
            "Slightly Slow": "#a0c0f0", "Slow": "#5d8fe8", "Very Slow": "#3a69d7",
        }
        _ac = _lbl_colour.get(_act_lbl, "#888")
        _pc = _lbl_colour.get(_pred_lbl or "", "#888")
        _dev_str = f"{_act_dev:+.2f}s vs HKJC std" if isinstance(_act_dev, (int, float)) else ""
        _pred_str = (f"<span style='color:{_pc};font-weight:600'>{_pred_lbl}</span>"
                     f" ({_pred_sc:+.2f}s)") if _pred_lbl is not None else \
                    "<span style='opacity:0.5'>—</span>"
        _match = ""
        if _pred_lbl and _act_lbl:
            from pace_utils import pace_band_distance
            _d = pace_band_distance(_pred_lbl, _act_lbl)
            if _d == 0:
                _match = "<span style='color:#28a745;font-weight:600'> ✓ exact</span>"
            elif _d == 1:
                _match = "<span style='color:#b08800'> ±1 band</span>"
            elif _d is not None:
                _match = f"<span style='color:#d73a49'> off by {_d} bands</span>"
        st.markdown(
            f"<div style='background:#0e1117;border:1px solid #30363d;"
            f"border-radius:6px;padding:10px 14px;margin:8px 0 14px 0;"
            f"display:flex;gap:24px;flex-wrap:wrap;align-items:center;font-size:13px'>"
            f"<div><span style='opacity:0.65'>Actual pace:</span> "
            f"<span style='color:{_ac};font-weight:700'>{_act_lbl}</span> "
            f"<span style='opacity:0.55'>({_dev_str})</span></div>"
            f"<div><span style='opacity:0.65'>Model predicted:</span> {_pred_str}{_match}</div>"
            f"<div><span style='opacity:0.65'>Winner style:</span> "
            f"<span style='font-weight:600'>{_winner_style or '—'}</span></div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    # ── Race Replay button (top of race view) ────────────────────────────
    _video_url = _hkjc_video_url(selected_dc, int(selected_rn))
    st.markdown(
        f'<div style="margin:4px 0 14px 0;">'
        f'<a href="{_video_url}" target="_blank" rel="noopener noreferrer" '
        f'style="display:inline-block;padding:8px 16px;background:#1f6feb;'
        f'color:#fff;border-radius:6px;text-decoration:none;font-weight:600;'
        f'font-size:14px;">▶ Watch Race Replay (HKJC)</a>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Results table ────────────────────────────────────────────────────
    runners = race.get("runners", [])

    # ── Finishing speed (final 400m sectional) ───────────────────────
    # NOTE (Jun 2026): the OCR "lane" (vertical position of a name label in the
    # running-position photo) turned out to be a label-layout artifact, NOT real
    # lane — horses 3-wide read as "rail", etc. — with ~zero correlation to finish
    # (ρ≈-0.03, n=5,178). Lane columns were therefore removed. The photo viewer
    # and running order (x) remain. The reliable, HKJC-scraped signal for spotting
    # strong finishers is the final-400m sectional time (always the last entry of
    # `sectiontimes`, regardless of race distance).
    def _last_sect(_st):
        _vals = [s for s in (_st or []) if str(s).strip() not in ("", "-", "---")]
        try:
            return float(_vals[-1]) if _vals else None
        except (TypeError, ValueError):
            return None
    _fin_secs = [s for s in (_last_sect(r.get("sectiontimes")) for r in runners) if s]
    _fin_field_avg = (sum(_fin_secs) / len(_fin_secs)) if _fin_secs else None

    res_rows = []
    # ── Running-lane context (restored Jun 2026, display-only) ───────────
    # OCR'd from the HKJC running-position photos. NOTE: the lane read is an
    # approximation (label-layout artifact; ~zero correlation to finish), so it
    # is shown purely as race-reading context — never fed into the model.
    _lane_map = {}
    try:
        from lane_utils import load_race_lanes as _load_race_lanes
        _lane_map = _load_race_lanes(selected_dc, int(selected_rn)) or {}
    except Exception:
        _lane_map = {}
    for r in runners:
        hname = r.get("horse_name", "")
        in_bb = hname.upper() in bb_names
        m_rank = model_ranks.get((race["race_number"], hname.upper()), "—")
        positions_list = r.get("positions", [])
        running_pos = "-".join(p for p in positions_list if p) if positions_list else r.get("running_position", "")

        # Numeric coercion for clean sorting in Streamlit header-click
        def _to_float(v):
            if v in (None, "", "---", "-"):
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        def _to_int(v):
            if v in (None, "", "---", "-"):
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        _lane_rec = _lane_map.get(hname.upper()) or {}
        _lane_bucket = _lane_rec.get("avg_bucket") or ""
        _lane_gl = _lane_rec.get("ground_lost_m")
        _lane_disp = _lane_bucket
        if _lane_bucket and isinstance(_lane_gl, (int, float)) and abs(_lane_gl) >= 1:
            _lane_disp = f"{_lane_bucket} ({_lane_gl:+.0f}m)"

        row = {
            "Place": _to_int(r.get("place")),
            "No": _to_int(r.get("horse_no")),
            "Horse": hname,
            "BB": "BB" if in_bb else "",
            "Model Rk": m_rank if isinstance(m_rank, int) else (_to_int(m_rank) or "—"),
            "Jockey": r.get("jockey", ""),
            "Trainer": r.get("trainer", ""),
            "Wt": _to_int(r.get("actual_weight")),
            "Draw": _to_int(r.get("draw")),
            "Running Pos": running_pos,
            "Lane~": _lane_disp,
            "Finish Time": r.get("finish_time", ""),
            "Win Odds": _to_float(r.get("win_odds")),
            "LBW": r.get("lbw", ""),
        }
        _last = _last_sect(r.get("sectiontimes"))
        row["L400m"] = _last
        row["Fin Δ"] = ((_last - _fin_field_avg)
                        if (_last is not None and _fin_field_avg) else None)
        res_rows.append(row)

    _res_df = pd.DataFrame(res_rows)
    # Model Rk is mixed int / "—" — guard by converting plain ints to Int64 later
    _res_col_cfg = {
        "Place":    st.column_config.NumberColumn(format="%d"),
        "No":       st.column_config.NumberColumn(format="%d"),
        "Wt":       st.column_config.NumberColumn(format="%d"),
        "Draw":     st.column_config.NumberColumn(format="%d"),
        "Win Odds": st.column_config.NumberColumn(format="%.1f"),
        "Lane~":    st.column_config.TextColumn(
            "Lane~",
            help="Approximate running lane (rail / 2-wide / 3-wide / 4+ wide) "
                 "OCR'd from the HKJC running-position photo, with metres of "
                 "extra ground in brackets. Context only — it is an approximation "
                 "(label-layout artifact, ~zero correlation to finish) and is "
                 "NOT used by the model."),
        "L400m":    st.column_config.NumberColumn(
            "L400m", format="%.2f",
            help="Final 400m sectional time (seconds). Lower = faster finish."),
        "Fin Δ":    st.column_config.NumberColumn(
            "Fin Δ", format="%+.2f",
            help="This horse's final 400m vs the race field average. Strong "
                 "negative = exceptional closer (≤ -0.5s historically wins 21% / "
                 "places 54% / gains +5.5 positions, n=5,495)."),
    }
    def _fin_style(val):
        if not isinstance(val, (int, float)):
            return ""
        if val <= -0.5:
            return "background-color:#28a74522;color:#28a745;font-weight:700"
        if val <= -0.2:
            return "color:#28a745;font-weight:600"
        if val >= 0.5:
            return "color:#d73a49"
        return ""
    try:
        _styled = _res_df.style.map(_fin_style, subset=["Fin Δ"])
        st.dataframe(_styled, width='stretch', hide_index=True,
                        column_config=_res_col_cfg)
    except Exception:
        st.dataframe(_res_df, width='stretch', hide_index=True,
                        column_config=_res_col_cfg)
    st.markdown(
        "<div style='margin:6px 0 4px 0;font-size:12px;opacity:0.8'>"
        "<b>L400m</b> = final 400m sectional (s, lower = faster finish). "
        "<b>Fin Δ</b> = that horse's last 400m vs the race field average — "
        "<span style='color:#28a745;font-weight:600'>green</span> = closed faster "
        "than the field (exceptional finisher at ≤ -0.5s: 21% win / 54% place / "
        "+5.5 positions gained, n=5,495). Surfaces horses whose run was better "
        "than the bare finishing position suggests. "
        "<b>Lane~</b> = approximate running lane from the position photo "
        "(context only, not a model input)."
        "</div>",
        unsafe_allow_html=True,
    )

    # ── Race Commentary (scraped + interpreted + tagged) ─────────────────
    # Restored Jun 2026: race_commentary.py (step 7/8 of the post-race pipeline)
    # writes reports/commentary_YYYYMMDD.json — race narrative, per-horse
    # blurbs/tags/polarity and blackbook (trip-excuse) suggestions. Surface it
    # here so the read of the race lives alongside the result, not in another tab.
    _cmt_races = _load_commentary_races(selected_dc)
    _cmt_race = _cmt_races.get(selected_rn)
    if _cmt_race:
        _c_horses = _cmt_race.get("horses", []) or []
        _c_tagged = [h for h in _c_horses if (h.get("short") or h.get("tags")
                                              or h.get("incident_text"))]
        _c_bb = _cmt_race.get("blackbook_suggestions", []) or []
        _c_narr = (_cmt_race.get("narrative") or "").strip()
        with st.expander(
            f"📝 Race Commentary — R{selected_rn} "
            f"({len(_c_tagged)} horse{'s' if len(_c_tagged) != 1 else ''} noted)",
            expanded=bool(_c_tagged or _c_narr),
        ):
            if _c_narr:
                st.markdown(
                    f"<div style='font-size:13px;line-height:1.5;"
                    f"margin-bottom:10px;opacity:0.92'>{_c_narr}</div>",
                    unsafe_allow_html=True,
                )
            if _c_tagged:
                _rows = []
                for h in sorted(_c_tagged,
                                key=lambda x: (x.get("place") or 99)):
                    _pl = h.get("place") or "—"
                    _hn = h.get("horse_name", "")
                    _short = h.get("short") or ""
                    _chips = _commentary_tag_chips(
                        h.get("tags"), h.get("polarity_score"))
                    _inc = (h.get("incident_text") or "").strip()
                    _inc_html = (
                        f"<div style='font-size:11px;opacity:0.6;margin-top:2px'>"
                        f"{_inc}</div>" if _inc else "")
                    _rows.append(
                        f"<tr>"
                        f"<td style='text-align:center;font-weight:700;"
                        f"padding:4px 8px;vertical-align:top'>{_pl}</td>"
                        f"<td style='padding:4px 8px;vertical-align:top'>"
                        f"<span style='font-weight:600'>{_hn}</span>"
                        f"{_inc_html}</td>"
                        f"<td style='padding:4px 8px;vertical-align:top;"
                        f"font-size:12px'>{_short}</td>"
                        f"<td style='padding:4px 8px;vertical-align:top'>"
                        f"{_chips}</td>"
                        f"</tr>"
                    )
                st.markdown(
                    "<table style='width:100%;border-collapse:collapse;"
                    "font-size:12px'>"
                    "<thead><tr style='border-bottom:1px solid #30363d;"
                    "text-align:left;opacity:0.7'>"
                    "<th style='padding:4px 8px'>Pl</th>"
                    "<th style='padding:4px 8px'>Horse</th>"
                    "<th style='padding:4px 8px'>Comment</th>"
                    "<th style='padding:4px 8px'>Tags</th>"
                    "</tr></thead><tbody>"
                    + "".join(_rows) +
                    "</tbody></table>",
                    unsafe_allow_html=True,
                )
            if _c_bb:
                _bb_items = "".join(
                    f"<li style='margin:2px 0;font-size:12px'>"
                    f"<span style='font-weight:600'>{b.get('horse_name','')}</span> "
                    f"<span style='color:"
                    f"{'#3fb950' if b.get('polarity') == '+' else '#f85149' if b.get('polarity') == '-' else '#9da7b3'}'>"
                    f"[{b.get('tag','')}]</span> "
                    f"<span style='opacity:0.8'>{b.get('reason','')}</span>"
                    f"<span style='opacity:0.5'> (finished {b.get('place','—')})</span>"
                    f"</li>"
                    for b in _c_bb
                )
                st.markdown(
                    "<div style='margin-top:10px;font-size:12px'>"
                    "<div style='font-weight:600;opacity:0.8;margin-bottom:2px'>"
                    "📓 Blackbook suggestions (trip excuses to forgive)</div>"
                    f"<ul style='margin:0;padding-left:18px'>{_bb_items}</ul>"
                    "</div>",
                    unsafe_allow_html=True,
                )
            if not (_c_tagged or _c_narr or _c_bb):
                st.caption("No notable trouble/incidents flagged for this race.")
    else:
        st.caption(
            f"📝 No commentary file for this meeting. Run the full post-race "
            f"pipeline (step 7/8 → `race_commentary.py`) to generate "
            f"`reports/commentary_{selected_dc}.json`."
        )

    _xl_bytes = _results_json_to_excel_bytes(REPORTS / f"results_{selected_dc}.json")
    if _xl_bytes:
        st.download_button(
            "[ Download Results (Excel) ]", _xl_bytes,
            file_name=f"results_{selected_dc}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="res_dl_xlsx",
        )

    # ── Dividends / Payouts (from scrape_hkjc_dividends.py) ──────────────
    _div_path = REPORTS / f"dividends_{selected_dc}.json"
    if _div_path.exists():
        try:
            _div_data = json.loads(_div_path.read_text(encoding="utf-8"))
            _div_race = next(
                (r for r in _div_data.get("races", [])
                 if r.get("race_number") == selected_rn),
                None,
            )
        except Exception:
            _div_race = None
        if _div_race and _div_race.get("dividends"):
            # Group by pool, preserving HKJC canonical order
            _pool_order = ["WIN", "PLACE", "QIN", "QPL", "FCT", "TCE",
                           "TRIO", "F4", "QTT", "DBL", "TBL", "DT", "TT", "6UP"]
            _pool_labels = {
                "WIN": "Win", "PLACE": "Place", "QIN": "Quinella",
                "QPL": "Quinella Place", "FCT": "Forecast", "TCE": "Tierce",
                "TRIO": "Trio", "F4": "First 4", "QTT": "Quartet",
                "DBL": "Double", "TBL": "Treble", "DT": "Double Trio",
                "TT": "Triple Trio", "6UP": "Six Up",
            }
            _by_pool: dict[str, list[dict]] = {}
            for d in _div_race["dividends"]:
                _by_pool.setdefault(d.get("pool", "?"), []).append(d)
            with st.expander(
                f"💰 Dividends — R{selected_rn} (HK$10 unit)", expanded=True):
                _cards = []
                for _pk in _pool_order:
                    if _pk not in _by_pool:
                        continue
                    _items = _by_pool[_pk]
                    _rows_html = "".join(
                        f"<div style='display:flex;justify-content:space-between;"
                        f"padding:2px 0;font-size:12px'>"
                        f"<span style='font-family:monospace;opacity:0.85'>"
                        f"{d.get('combination','?')}</span>"
                        f"<span style='font-weight:600'>"
                        f"${d.get('dividend_per_10','?'):,.1f}</span>"
                        f"</div>"
                        for d in _items
                    )
                    _cards.append(
                        f"<div style='flex:0 0 auto;min-width:150px;"
                        f"border:1px solid #30363d;border-radius:6px;"
                        f"padding:8px 10px;background:#0d1117'>"
                        f"<div style='font-weight:700;font-size:12px;"
                        f"color:#58a6ff;margin-bottom:4px'>"
                        f"{_pool_labels.get(_pk, _pk)} "
                        f"<span style='opacity:0.5;font-weight:400'>"
                        f"({_pk})</span></div>"
                        f"{_rows_html}"
                        f"</div>"
                    )
                # Remaining pools not in canonical order
                for _pk, _items in _by_pool.items():
                    if _pk in _pool_order:
                        continue
                    _rows_html = "".join(
                        f"<div style='display:flex;justify-content:space-between;"
                        f"padding:2px 0;font-size:12px'>"
                        f"<span style='font-family:monospace;opacity:0.85'>"
                        f"{d.get('combination','?')}</span>"
                        f"<span style='font-weight:600'>"
                        f"${d.get('dividend_per_10','?'):,.1f}</span>"
                        f"</div>"
                        for d in _items
                    )
                    _cards.append(
                        f"<div style='flex:0 0 auto;min-width:150px;"
                        f"border:1px solid #30363d;border-radius:6px;"
                        f"padding:8px 10px;background:#0d1117'>"
                        f"<div style='font-weight:700;font-size:12px;"
                        f"color:#58a6ff;margin-bottom:4px'>{_pk}</div>"
                        f"{_rows_html}"
                        f"</div>"
                    )
                st.markdown(
                    f"<div style='display:flex;gap:8px;flex-wrap:wrap'>"
                    f"{''.join(_cards)}</div>",
                    unsafe_allow_html=True,
                )
                st.caption(
                    "Dividends shown per HK$10 unit (HKJC standard). "
                    "Scraped via `scrape_hkjc_dividends.py` → "
                    f"`reports/dividends_{selected_dc}.json`."
                )
        else:
            st.caption(
                f"💰 No dividends found for R{selected_rn} in "
                f"`dividends_{selected_dc}.json`."
            )
    else:
        st.caption(
            "💰 No dividends file. Run "
            f"`python scrape_hkjc_dividends.py --date "
            f"{selected_dc[:4]}-{selected_dc[4:6]}-{selected_dc[6:]}` "
            "to generate `reports/dividends_*.json`."
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
            st.image(str(rp_photo_path), width='stretch')
        else:
            st.info(
                f"No photo cached for R{selected_rn}. "
                f"Run `python scrape_hkjc_rp_photos.py --date {selected_dc[:4]}-{selected_dc[4:6]}-{selected_dc[6:]}` "
                f"to download."
            )

    # ── Race Replay (HKJC video) ─────────────────────────────────────────
    # (button is rendered at the top of the race view — see above)

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
                st.dataframe(pd.DataFrame(_hrows), width='stretch', hide_index=True)
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
        f"Generated {hkt_now().strftime('%d %b %Y %H:%M')} HKT · "
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
        f"Generated {hkt_now().strftime('%d %b %Y %H:%M')} HKT · Pre-built cache",
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
    """Run live_analysis engine for a meeting date.

    Returns the analysis dict, or a dict containing an ``error`` key so the
    UI can surface the real reason. Previously this function swallowed all
    exceptions and returned ``None`` — which made the dashboard show
    'No results scraped yet…' even when results existed but the analyser
    crashed (e.g. missing module / KeyError). That made the page appear
    completely non-functional.
    """
    try:
        from live_analysis import run_live_analysis
    except Exception as e:
        import traceback as _tb
        return {"error": f"Cannot import live_analysis: "
                         f"{type(e).__name__}: {e}",
                "traceback": _tb.format_exc()}
    try:
        result = run_live_analysis(dstr)
        # run_live_analysis already returns {"error": ...} on its own
        # known failures (e.g. no predictions / no results). Pass through.
        return result
    except Exception as e:
        import traceback as _tb
        return {"error": f"run_live_analysis crashed: "
                         f"{type(e).__name__}: {e}",
                "traceback": _tb.format_exc()}


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
            st.session_state["live_last_refresh"] = hkt_now().strftime("%H:%M:%S") + " HKT"
            st.rerun()
    with col_s2:
        if st.button("📊 Run Analysis", key="live_analyse"):
            st.session_state["live_last_refresh"] = hkt_now().strftime("%H:%M:%S") + " HKT"
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
    if "error" in analysis:
        st.error(f"Live analysis failed: {analysis['error']}")
        tb = analysis.get("traceback")
        if tb:
            with st.expander("Traceback (for debugging)"):
                st.code(tb[-3000:])
        # Common known reasons — give actionable guidance
        msg = str(analysis.get("error", "")).lower()
        if "no predictions" in msg or "prediction" in msg:
            st.caption("💡 Make sure the **pre-race meeting pipeline** has been "
                       "run for this date (Race Day page → Run Meeting). The "
                       "Live Feed needs ET + SARR predictions to compare against "
                       "actual results.")
        elif "no results" in msg or "results_" in msg:
            st.caption("💡 Click **Scrape Results** above once at least one race "
                       "has been run. The analyser needs the `results_YYYYMMDD.json` "
                       "file to exist.")
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

    _maybe_auto_sync_results("form_guide")

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

    # Per-run ESZ + Fin Δ columns (early speed vs field / finishing vs field).
    # Keyed (iso_date_str, race_number) → {horse_name_upper: {esz, fin_delta, fin_z}}.
    sec_metrics: dict = {}
    try:
        from db_utils import SQLITE_FILE
        try:
            _sm_sql = SQLITE_FILE.stat().st_mtime if SQLITE_FILE.exists() else 0.0
        except OSError:
            _sm_sql = 0.0
        try:
            from db_utils import DB_FILE as _RESULTS_XLSX
            _sm_xls = _RESULTS_XLSX.stat().st_mtime if _RESULTS_XLSX.exists() else 0.0
        except Exception:
            _sm_xls = 0.0
        sec_metrics = _sectional_metrics_cached(_sm_sql, _sm_xls)
    except Exception:
        sec_metrics = {}

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
        if not sec_metrics:
            try:
                from pace_utils import compute_sectional_metrics
                sec_metrics = compute_sectional_metrics(form_db)
            except Exception:
                sec_metrics = {}

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

    # ── Horse search box ─────────────────────────────────────────────────
    search_q = st.text_input(
        "🔍 Find horse",
        key="fg_search_input",
        placeholder="Type a horse name to jump to their race…",
        label_visibility="collapsed",
    )
    search_upper = (search_q or "").strip().upper()
    matched_race_idx = None
    matched_horse_uppers: set[str] = set()
    if search_upper:
        # Find races containing a horse whose name contains the query
        # Use fg_cache if available (cheapest), otherwise racecard/picks.
        def _race_horse_names(race_dict, race_num):
            names = []
            if fg_cache:
                cached = next((r for r in fg_cache.get("races", [])
                               if r.get("race_number") == race_num), None)
                if cached:
                    names.extend(h.get("horse_name", "") for h in cached.get("horses", []))
            if racecard:
                for rc_race in racecard.get("races", []):
                    if rc_race.get("meta", {}).get("race_number") == race_num:
                        names.extend(h.get("horse_name", "") for h in rc_race.get("horses", [])
                                     if not h.get("is_standby"))
                        break
            names.extend(p.get("horse_name", "") for p in race_dict.get("picks", []))
            return names

        for i, r in enumerate(races):
            names = _race_horse_names(r, r["race_number"])
            hits = [n for n in names if n and search_upper in n.upper()]
            if hits:
                if matched_race_idx is None:
                    matched_race_idx = i
                matched_horse_uppers.update(h.upper().strip() for h in hits)
        if matched_race_idx is None:
            st.warning(f"No horse matching “{search_q}” on this card.")
        else:
            # Auto-jump only once per query change
            last_q = st.session_state.get("_fg_search_last_q", "")
            if last_q != search_upper:
                st.session_state["fg_active_race"] = matched_race_idx
                st.session_state["_fg_search_last_q"] = search_upper
                active_idx = matched_race_idx
                st.rerun()
            else:
                active_idx = st.session_state.get("fg_active_race", matched_race_idx)
            # Show match chip(s)
            match_chips = " ".join(
                f'<span style="background:rgba(34,197,94,0.15);color:#22c55e;'
                f'padding:2px 8px;border-radius:10px;font-size:0.85em;'
                f'font-weight:700;margin-right:4px">R{r["race_number"]} · {len([n for n in _race_horse_names(r, r["race_number"]) if n.upper() in matched_horse_uppers])} match(es)</span>'
                for r in races
                if any(n.upper() in matched_horse_uppers for n in _race_horse_names(r, r["race_number"]))
            )
            st.markdown(
                f'<div style="margin:-4px 0 8px 0">{match_chips}</div>',
                unsafe_allow_html=True,
            )
    else:
        # clear the sentinel when search is empty
        st.session_state.pop("_fg_search_last_q", None)

    btn_cols = st.columns(len(races))
    for i, race in enumerate(races):
        with btn_cols[i]:
            is_active = (i == active_idx)
            if st.button(f"R{race['race_number']}", key=f"fg_btn_{race['race_number']}",
                         width='stretch',
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
        prev_rating: str | None = None
        prev_weight: str | None = None
        if fg_cache and not is_debutant:
            cached_runs_check = horse.get("runs", [])
            if cached_runs_check:
                last_run = cached_runs_check[-1]
                prev_trainer = str(last_run.get("trainer", ""))
                if prev_trainer and prev_trainer != "?" and current_trainer != "?" and prev_trainer.strip().upper() != current_trainer.strip().upper():
                    trainer_changed = True
                prev_rating = str(last_run.get("rating", "")) or None
                prev_weight = str(last_run.get("actual_weight", "")) or None
        elif not fg_cache and not is_debutant:
            mask = form_db["horse_name_upper"] == hname.strip().upper()
            trainer_hist = form_db[mask & (form_db["race_date"] < meeting_date)]
            if not trainer_hist.empty:
                last_row = trainer_hist.sort_values("race_date", ascending=False).iloc[0]
                prev_trainer = str(last_row.get("trainer", "")).strip()
                if prev_trainer and current_trainer != "?" and prev_trainer.upper() != current_trainer.strip().upper():
                    trainer_changed = True
                try:
                    prev_rating = str(int(float(last_row.get("rating")))) if pd.notna(last_row.get("rating")) else None
                except (ValueError, TypeError):
                    prev_rating = None
                try:
                    prev_weight = str(int(float(last_row.get("actual_weight")))) if pd.notna(last_row.get("actual_weight")) else None
                except (ValueError, TypeError):
                    prev_weight = None
        trainer_display = (
            f'<span style="color:#d43700;font-weight:700" title="Stable change from {prev_trainer}">'
            f'\u2691 {current_trainer}</span>'
            if trainer_changed
            else current_trainer
        )

        # ── Rating / weight delta chips ───────────────────────────────────
        def _delta_chip(curr, prev, label, units=""):
            try:
                c = int(float(curr)); p = int(float(prev))
            except (ValueError, TypeError):
                return ""
            d = c - p
            if d == 0:
                return ""
            if label == "RTG":
                # Rating up = horse promoted (harder class); neutral colour
                col = "#fbbf24" if d > 0 else "#60a5fa"
            else:
                # Weight: dropping kg = lighter (positive); rising = harder
                col = "#22c55e" if d < 0 else "#ef4444"
            sign = f"+{d}" if d > 0 else f"{d}"
            return (
                f'<span style="margin-left:4px;padding:0 5px;border-radius:5px;'
                f'background:rgba(255,255,255,0.05);color:{col};font-size:0.78em;'
                f'font-weight:700" title="Change vs last run ({prev}{units})">'
                f'{sign}{units}</span>'
            )
        rtg_delta_chip = _delta_chip(current_rtg, prev_rating, "RTG") if prev_rating else ""
        wt_delta_chip = _delta_chip(current_weight, prev_weight, "WT", "LB") if prev_weight else ""

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
            f'<span class="h-meta">RTG {current_rtg}{rtg_delta_chip}</span>'
            f'<span class="h-sep">·</span>'
            f'<span class="h-meta">{current_weight} LB{wt_delta_chip}</span>'
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
                sm_run = None
                _rdate = run.get("date")
                _rnum = run.get("race_number")
                if isinstance(_rdate, str) and _rnum is not None:
                    try:
                        sm_run = (sec_metrics.get((_rdate[:10], int(_rnum))) or {})\
                            .get(hname.strip().upper())
                    except (ValueError, TypeError):
                        sm_run = None
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
                    "pace": str(run.get("pace", "-")),
                    "pace_dev": run.get("pace_dev"),
                    "esz":       (sm_run or {}).get("esz"),
                    "esz_z":     (sm_run or {}).get("esz_z"),
                    "ftime":     str(run.get("time", "-")),
                    "fin_delta": (sm_run or {}).get("fin_delta"),
                    "fin_z":     (sm_run or {}).get("fin_z"),
                    "top5": top5,
                    "top5_next": run.get("top5_next") or [],
                    "lane_avg": run.get("lane_avg"),
                    "lane_at":  run.get("lane_at") or {},
                    "ground_lost_m": run.get("ground_lost_m"),
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
                _sm = None
                _rd_iso = rd.strftime("%Y-%m-%d") if hasattr(rd, "strftime") else str(rd)[:10]
                if pd.notna(rnum):
                    try:
                        _sm = (sec_metrics.get((_rd_iso, int(rnum))) or {})\
                            .get(hname.strip().upper())
                    except (ValueError, TypeError):
                        _sm = None
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
                    "pace": "-",
                    "pace_dev": None,
                    "esz":       (_sm or {}).get("esz"),
                    "esz_z":     (_sm or {}).get("esz_z"),
                    "ftime":     ftime,
                    "fin_delta": (_sm or {}).get("fin_delta"),
                    "fin_z":     (_sm or {}).get("fin_z"),
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
            t5_html = _fmt_top5_html(top5, hname, dr.get("top5_next")) if top5 else "&mdash;"

            # Pace cell — temperature colour (fast=red, slow=blue).
            pace_label = str(dr.get("pace", "-")) or "-"
            pace_dev = dr.get("pace_dev")
            pace_colour = _pace_label_colour(pace_label)
            dev_title = f" ({pace_dev:+.2f}s vs HKJC)" if isinstance(pace_dev, (int, float)) else ""
            pace_cell = (
                f'<span title="Race-pace deviation from HKJC standard{dev_title}" '
                f'style="color:{pace_colour};font-weight:600">{pace_label}</span>'
                if pace_label and pace_label != "-" else "&mdash;"
            )

            esz_raw = dr.get("esz")
            esz_z = dr.get("esz_z")
            fin_delta = dr.get("fin_delta")
            fin_z = dr.get("fin_z")
            esz_disp = _fmt_signed(esz_raw)
            fin_disp = _fmt_signed(fin_delta)
            esz_cell = (
                f'<span title="SARR ESZ: early 400m-pace deviation vs race median (s; negative = faster early)" '
                f'style="color:{_metric_z_colour(esz_z)};font-weight:600">'
                f'{esz_disp}</span>' if esz_disp is not None else "&mdash;"
            )
            fin_cell = (
                f'<span title="Finish time vs field median (s, negative = faster than the field)" '
                f'style="color:{_metric_z_colour(fin_z)};font-weight:600">'
                f'{fin_disp}</span>' if fin_disp is not None else "&mdash;"
            )

            # Per-run video link + short commentary (inline cells)
            dc = dr.get("date_dc") or ""
            rnum = dr.get("race_num")
            vid_cell = "&mdash;"
            comment_cell = ""
            if dc and rnum:
                vurl = _hkjc_video_url(dc, rnum)
                vid_cell = (
                    f'<a class="vid-link" href="{vurl}" target="_blank" '
                    f'rel="noopener noreferrer" title="Watch replay">&#9654;</a>'
                )
                comment_cell = _run_commentary_lookup(dc, rnum, hname)
                _c_short = comment_cell.get("short", "") or ""
                _c_chips = _commentary_tag_chips(
                    comment_cell.get("tags"), comment_cell.get("polarity_score"))
                comment_cell = (_c_short + (" " if _c_short and _c_chips else "") + _c_chips)

            # v4.5: lane cell — coloured dot + 800/400/200 mini-track
            lane_avg = dr.get("lane_avg")
            lane_at = dr.get("lane_at") or {}
            gl = dr.get("ground_lost_m")
            if lane_avg or lane_at:
                try:
                    from lane_utils import lane_colour as _lc
                except Exception:
                    _lc = lambda _b: "#888"
                def _dot(b):
                    if not b:
                        return '<span style="display:inline-block;width:8px;height:8px;background:#444;border-radius:50%;margin:0 1px;opacity:0.3"></span>'
                    return (f'<span title="{b}" style="display:inline-block;'
                            f'width:8px;height:8px;background:{_lc(b)};'
                            f'border-radius:50%;margin:0 1px"></span>')
                dots = _dot(lane_at.get("800M")) + _dot(lane_at.get("400M")) + _dot(lane_at.get("200M"))
                gl_txt = f"{gl:+.0f}" if isinstance(gl, (int, float)) else ""
                lane_cell = (f'<span title="800m / 400m / 200m  (avg={lane_avg or "?"}, '
                             f'ground={gl_txt}m)">{dots}</span>')
            else:
                lane_cell = "&mdash;"

            html_rows.append(
                f'<tr class="form-data-row">'
                f'<td>{date_disp}</td>'
                f'<td>{pl_cell}</td>'
                f'<td>{dist}</td><td>{trk}</td><td>{crs}</td>'
                f'<td>{going}</td><td>{cls_val}</td>'
                f'<td class="td-left">{jock}</td>'
                f'<td>{rtg}</td><td>{wt}</td><td>{gate}</td>'
                f'<td class="td-pos">{pos}</td>'
                f'<td>{lane_cell}</td>'
                f'<td>{margin_cell}</td>'
                f'<td>{pace_cell}</td>'
                f'<td>{esz_cell}</td>'
                f'<td>{ftime}</td>'
                f'<td>{fin_cell}</td>'
                f'<td>{vid_cell}</td>'
                f'<td class="td-left form-comment">{comment_cell}</td>'
                f'</tr>'
                f'<tr class="top5-row">'
                f'<td colspan="20">{t5_html}</td>'
                f'</tr>'
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
                "Pace": pace_label,
                "ESZ": _fmt_signed(esz_raw) or "",
                "Finish Time": ftime,
                "Fin Delta": _fmt_signed(fin_delta) or "",
                "Top 5": t5_plain,
                "Comment": comment_cell,
            })

        table_html = (
            '<table class="form-tbl">'
            '<thead><tr>'
            '<th>Date</th><th>Pl</th><th>Dist</th><th>Trk</th><th>Crs</th>'
            '<th>Gng</th><th>Cls</th><th class="th-left">Jockey</th>'
            '<th>Rtg</th><th>Wt</th><th>Gt</th><th>Pos</th><th>Ln</th><th>Mrgn</th>'
            '<th>Pace</th><th>ESZ</th><th>Time</th><th>Fin &Delta;</th>'
            '<th>Vid</th><th class="th-left">Comment</th>'
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


@st.cache_data(show_spinner=False, ttl=3600)
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


def _trial_time_per_100m(entry: dict) -> float | None:
    """Return trial pace in seconds per 100m, or None if unavailable.

    Trial-time strings are typically ``MM.SS.hh`` (e.g. ``1.00.42``) or
    ``SS.hh`` (e.g. ``59.43``). We parse defensively.
    """
    t_str = str(entry.get("time", "") or "").strip()
    dist = entry.get("distance_m")
    if not t_str or not dist:
        return None
    try:
        dist = float(dist)
        if dist <= 0:
            return None
    except (TypeError, ValueError):
        return None
    # Parse time → seconds
    parts = t_str.split(".")
    try:
        if len(parts) == 3:           # M.SS.hh
            secs = int(parts[0]) * 60 + int(parts[1]) + int(parts[2]) / 100.0
        elif len(parts) == 2:         # SS.hh
            secs = int(parts[0]) + int(parts[1]) / 100.0
        else:
            secs = float(t_str)
    except (TypeError, ValueError):
        return None
    if secs <= 0:
        return None
    return secs / dist * 100.0


def _trial_time_signal(entry: dict) -> tuple[str, str]:
    """Classify a trial time relative to a per-100m benchmark.

    Returns (flag, reason) where flag is "fast" / "good" / "slow" / "".
    Even slow-paced trials can hide strong finishes, so this is purely a
    time-quality cue — combine it with sentiment for the full picture.
    """
    p100 = _trial_time_per_100m(entry)
    if p100 is None:
        return "", ""
    if p100 <= 5.85:
        return "fast", f"Fast trial time ({p100:.2f}s/100m)"
    if p100 <= 5.95:
        return "good", f"Good trial time ({p100:.2f}s/100m)"
    if p100 >= 6.30:
        return "slow", f"Slow trial time ({p100:.2f}s/100m)"
    return "", ""


def _trial_sentiment(entry: dict) -> tuple[str, list[str]]:
    """Return (flag, reasons) for a single trial entry.

    Flag is one of: "++", "+", "-", "" (empty = neutral / insufficient signal).
    Used by both the Form Guide per-run badge and the Overview standouts list
    so the two views always agree.
    """
    comment = (entry.get("comment", "") or "").lower()
    rp = entry.get("running_positions", []) or []
    fp = rp[-1] if rp else None
    sp = rp[0] if rp else None
    n = entry.get("n_horses", 0) or 0

    has_pos = any(p in comment for p in _POS_PHRASES)
    has_neg = any(nk in comment for nk in _NEG_KW)
    is_concealed = any(kw in comment for kw in _CONCEAL_KW) and not has_neg
    has_eased = "eased" in comment and not has_neg
    top_half = isinstance(fp, int) and n > 0 and fp <= max(1, n // 2)
    won = isinstance(fp, int) and fp == 1 and n >= 3
    gained = (isinstance(sp, int) and isinstance(fp, int) and (sp - fp) >= 2)
    bottom_q = (isinstance(fp, int) and n >= 4 and fp >= n - 1)

    time_flag, time_reason = _trial_time_signal(entry)
    is_fast = time_flag == "fast"
    is_good_time = time_flag in ("fast", "good")

    reasons: list[str] = []

    # ── ++ tier ─────────────────────────────────────────────
    if won and (is_concealed or has_pos):
        reasons.append("Won under hold / with finish")
        if time_reason:
            reasons.append(time_reason)
        return "++", reasons
    if won:
        reasons.append("Won trial")
        if time_reason:
            reasons.append(time_reason)
        return "++", reasons
    if is_fast and (top_half or is_concealed or has_pos):
        reasons.append(time_reason)
        if is_concealed: reasons.append("concealed")
        elif has_pos: reasons.append("positive phrase")
        elif top_half: reasons.append(f"top half ({fp}/{n})")
        return "++", reasons
    if is_concealed and top_half:
        reasons.append("Concealed + top half")
        if time_reason: reasons.append(time_reason)
        return "++", reasons
    if has_eased and has_pos:
        reasons.append("Eased + strong finish")
        if time_reason: reasons.append(time_reason)
        return "++", reasons
    if has_pos and top_half:
        reasons.append("Positive phrase + top half")
        if time_reason: reasons.append(time_reason)
        return "++", reasons

    # ── - tier (explicit negatives) ────────────────────────
    if has_neg:
        reasons.append("Negative trial signal")
        return "-", reasons
    if bottom_q and not has_pos and not is_concealed and not is_good_time:
        reasons.append(f"Bottom-quartile finish ({fp}/{n})")
        return "-", reasons

    # ── + tier (any positive signal) ───────────────────────
    if is_good_time:
        reasons.append(time_reason)
        if top_half: reasons.append(f"top half ({fp}/{n})")
        return "+", reasons
    if has_eased:
        reasons.append("Eased (deliberately held)")
        return "+", reasons
    if is_concealed:
        reasons.append("Concealed form")
        return "+", reasons
    if gained:
        reasons.append(f"Gained {(sp or 0) - (fp or 0)} positions")
        return "+", reasons
    if has_pos:
        reasons.append("Positive phrase")
        return "+", reasons
    if top_half and not has_neg:
        reasons.append(f"Top-half finish ({fp}/{n})")
        return "+", reasons

    return "", reasons


def _trial_sentiment_badge(entry: dict) -> str:
    """Compact coloured span of the sentiment flag for inline rendering.

    Uses the shared ``_trial_sentiment`` helper so Form Guide and Overview
    always agree on the flag.
    """
    flag, reasons = _trial_sentiment(entry)
    if not flag:
        return ""
    title = "; ".join(reasons) if reasons else {
        "++": "Very positive trial",
        "+":  "Positive trial",
        "-":  "Negative trial",
    }.get(flag, "")
    colour_weight = {
        "++": ("#22c55e", 800),
        "+":  ("#86efac", 700),
        "-":  ("#ef4444", 800),
    }[flag]
    display = flag if flag != "-" else "&minus;"
    colour, weight = colour_weight
    return (f'<span style="color:{colour};font-weight:{weight};'
            f'font-size:0.85em;margin-right:4px" '
            f'title="{title}">{display}</span>')


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

        # Trial replay button — HKJC trial videos exist ~2020 onwards
        vid_html = ""
        dt_iso = e.get("date", "")
        batch_n = e.get("batch_number", 0)
        if dt_iso and dt_iso >= "2020-01-01" and batch_n:
            dc = dt_iso.replace("-", "")
            vurl = _hkjc_trial_video_url(dc, int(batch_n), e.get("course", ""))
            vid_html = (
                f' <a href="{vurl}" target="_blank" rel="noopener noreferrer" '
                f'class="vid-link" title="Watch trial" '
                f'style="color:#1f6feb;font-weight:700;text-decoration:none;">'
                f'&#9654;</a>'
            )

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

        sentiment_html = _trial_sentiment_badge(e)

        # Time colouring — fast trials are a strong stand-alone signal
        t_flag, t_reason = _trial_time_signal(e)
        t_raw = e.get("time", "")
        if t_raw and t_flag == "fast":
            time_html = (f'<span style="color:#22c55e;font-weight:700" '
                         f'title="{t_reason}">{t_raw}</span>')
        elif t_raw and t_flag == "good":
            time_html = (f'<span style="color:#86efac;font-weight:600" '
                         f'title="{t_reason}">{t_raw}</span>')
        elif t_raw and t_flag == "slow":
            time_html = (f'<span style="color:#fca5a5" '
                         f'title="{t_reason}">{t_raw}</span>')
        else:
            time_html = str(t_raw)

        rows_html.append(
            f'{sentiment_html}'
            f'<span style="color:#6b7280;font-size:0.85em">{dt_disp}</span> '
            f'{dist}m '
            f'<span style="{pos_style}">{pos_str}</span>'
            f'({finish_pos}/{n}) '
            f'{lbw} '
            f'{time_html}'
            f'{gear_html}'
            f'{res_html}'
            f'{vid_html}'
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

    if st.sidebar.button("[ SCRAPE TRIALS ]", type="primary", width='stretch'):
        with st.spinner(f"Scraping trials for {trial_date.isoformat()}..."):
            result = subprocess.run(
                [PYTHON, "scrape_hkjc_trials.py", "--date", trial_date.isoformat()],
                capture_output=True, text=True, encoding="utf-8",
                cwd=str(BASE),
            )
        if result.returncode == 0:
            # Sync trial JSON to GitHub on Streamlit Cloud (no-op locally).
            if _is_streamlit_cloud():
                if _gh_push_trials(trial_date.isoformat()):
                    st.toast("☁ Trial JSON synced to GitHub", icon="✅")
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
    if st.sidebar.button("[ BULK SCRAPE ]", width='stretch', key="trial_bulk_btn"):
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
            # Sync every newly created trials_*.json to GitHub on cloud.
            if _is_streamlit_cloud():
                pushed = 0
                d = bulk_from
                while d <= bulk_to:
                    if _gh_push_trials(d.isoformat()):
                        pushed += 1
                    d += timedelta(days=1)
                if pushed:
                    st.toast(f"☁ Synced {pushed} trial JSON(s) to GitHub", icon="✅")
            st.toast(f"\u2713 {summary}", icon="\u2705")
            st.cache_data.clear()
            st.rerun()
        else:
            st.sidebar.error(
                f"Bulk scrape failed:\n```\n{result.stderr[-500:] if result.stderr else result.stdout[-500:]}\n```"
            )


def _render_trial_batch_table(batch: dict, search_upper: str = "",
                              date_iso: str = ""):
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
        # Replay button (HKJC videos from ~2020 onwards)
        if date_iso and date_iso >= "2020-01-01":
            vurl = _hkjc_trial_video_url(
                date_iso.replace("-", ""),
                int(batch["batch_number"]),
                batch.get("course", ""),
            )
            st.markdown(
                f'<div style="margin:0 0 8px 0;">'
                f'<a href="{vurl}" target="_blank" rel="noopener noreferrer" '
                f'style="display:inline-block;padding:6px 12px;background:#1f6feb;'
                f'color:#fff;border-radius:5px;text-decoration:none;font-weight:600;'
                f'font-size:13px;">▶ Watch Trial Batch {batch["batch_number"]} (HKJC)</a>'
                f'</div>',
                unsafe_allow_html=True,
            )
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
        '<th>Fin</th><th>Time</th><th>Result</th><th>Vid</th>'
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

        # Trial replay button (HKJC videos from ~2020 onwards)
        dt_iso = e.get("date", "") or ""
        batch_n = e.get("batch_number", 0)
        if dt_iso and dt_iso >= "2020-01-01" and batch_n:
            dc = dt_iso.replace("-", "")
            vurl = _hkjc_trial_video_url(dc, int(batch_n), e.get("course", ""))
            vid_cell = (
                f'<a href="{vurl}" target="_blank" rel="noopener noreferrer" '
                f'class="vid-link" title="Watch trial">&#9654;</a>'
            )
        else:
            vid_cell = "&mdash;"

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
            f'<td>{vid_cell}</td>'
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
        st.dataframe(styled, width='stretch', hide_index=True)

        # Expandable details
        with st.expander(f"Show comments ({len(group)} horses)"):
            for s in group:
                cmt = s["comment"] or "(no comment)"
                st.caption(f"**{s['horse']}** ({s['date']}) — {cmt}")


# ════════════════════════════════════════════════════════════════════════
# Trial Blackbook: auto-suggest from scraped trials, user confirms.
# Entries are written via _bb_add_entry() with tag 'trial' and a very
# distant expiry so they never auto-expire — user manages removal in
# the Blackbook page.
# ════════════════════════════════════════════════════════════════════════
def _render_trial_blackbook_tab(trials_index: list[dict]):
    """Surface high-scoring trial runners, let user add to blackbook."""
    try:
        from trial_blackbook import (
            suggest_for_date, already_in_blackbook, build_reasoning,
        )
    except ImportError as e:
        st.error(f"trial_blackbook module unavailable: {e}")
        return

    st.markdown("### 👀 Trial Blackbook — Auto-suggest")
    st.caption(
        "Eye-catchers from the most recent scraped trial card. Tick the "
        "horses you want to follow and click **Add selected**. Entries "
        "are tagged `trial` and never auto-expire — remove them manually "
        "from the Blackbook page when no longer relevant."
    )

    # Date picker — default to most recent trial date
    options = {t["date_display"]: t for t in trials_index}
    sel_label = st.selectbox(
        "Trial date", list(options.keys()), index=0,
        key="trial_bb_date_select",
    )
    sel = options[sel_label]
    min_score = st.slider(
        "Minimum eye-catcher score", 1.0, 6.0, 2.0, 0.5,
        key="trial_bb_min_score",
        help="Higher = stricter. 2.0 surfaces ~15-20 horses per card; "
             "3.0 is winners + clear closers only.",
    )

    rows = suggest_for_date(sel["file"], min_score=min_score)
    if not rows:
        st.info("No horses meet the threshold on this card.")
        return

    bb = _load_blackbook()
    existing_lookup = {
        (e.get("horse_name") or "").strip().upper(): e
        for e in bb.get("entries", [])
        if e.get("status") == "active"
    }

    st.caption(f"**{len(rows)} suggestions** from {sel['date_display']}. "
               "Already-blackbooked horses are marked ✓ and pre-disabled.")

    # Form with per-row checkbox + optional inline edit
    with st.form("trial_bb_add_form"):
        chosen: list[dict] = []
        for r in rows:
            hn = r["horse_name"]
            in_bb = hn in existing_lookup
            cols = st.columns([0.5, 2.4, 0.6, 0.6, 1.2, 3.0, 0.8])
            with cols[0]:
                pick = st.checkbox(
                    "✓", value=False, key=f"trial_bb_pick_{hn}_{r['batch_number']}",
                    label_visibility="collapsed",
                    disabled=in_bb,
                )
            with cols[1]:
                badge = " ✓ already in BB" if in_bb else ""
                st.markdown(f"**{hn}**{badge}")
                st.caption(f"{r.get('trainer','')} · {r.get('jockey','')}")
            with cols[2]:
                st.markdown(f"`{r['score']:.1f}`")
            with cols[3]:
                fp = r.get("final_pos")
                st.markdown(f"pos {fp}" if fp else "—")
            with cols[4]:
                st.caption(f"{r['distance_m']}m · {r['course']}")
                st.caption(f"draw {r.get('draw','-')} · {r.get('lbw','-')}")
            with cols[5]:
                st.caption((r.get("comment") or "")[:120])
                if r["reasons"]:
                    st.caption("• " + " · ".join(r["reasons"][:3]))
            with cols[6]:
                conf = st.selectbox(
                    "conf", ["high", "medium", "low"],
                    index=1, key=f"trial_bb_conf_{hn}_{r['batch_number']}",
                    label_visibility="collapsed",
                    disabled=in_bb,
                )
            if pick and not in_bb:
                chosen.append({**r, "confidence": conf})

        submitted = st.form_submit_button(
            f"[ Add selected to Blackbook ]", type="primary",
        )

    if submitted:
        if not chosen:
            st.warning("No new horses selected.")
            return
        added = 0
        # Use a very distant expiry (~10 yrs) since user wants manual-only
        # expiry. _bb_add_entry() supports expiry_days override.
        for c in chosen:
            try:
                dist = int(c.get("distance_m")) if c.get("distance_m") else None
            except (TypeError, ValueError):
                dist = None
            surface = "Turf"  # All HK barrier trials are Turf on the main tracks
            _bb_add_entry(
                bb,
                horse_name=c["horse_name"],
                reasoning=build_reasoning(c),
                tags=["trial"],
                confidence=c.get("confidence", "medium"),
                source_race=f"Trial {c['trial_date']} B{c['batch_number']}",
                preferred_distance=[dist] if dist else [],
                preferred_surface=surface,
                expiry_days=3650,
                category="Pre-Race",
            )
            added += 1
        st.toast(f"✓ Added {added} horse(s) to Blackbook", icon="⭐")
        st.rerun()


@st.dialog("Add note to Blackbook", width="large")
def _tw_comment_dialog(eid: str, horse_name: str):
    """Append a dated note to an existing Blackbook entry (Talent Watch)."""
    bb = _load_blackbook()
    entry = next((e for e in bb["entries"] if e["id"] == eid), None)
    if not entry:
        st.error("Entry not found.")
        return
    st.markdown(f"### {horse_name}")
    st.caption(f"Added {entry.get('added_date', '')} · "
               f"{entry.get('source_race', '—')} · "
               f"confidence: {entry.get('confidence', '—')}")
    if entry.get("reasoning"):
        st.markdown("**Current notes:**")
        st.info(entry["reasoning"])
    with st.form("tw_bb_note_form", clear_on_submit=True):
        note = st.text_area(
            "New note *", height=110,
            placeholder="e.g. Eye-catching trial 31 May — concealed under a "
                        "hold, watch for first-up improvement next start.")
        c1, c2 = st.columns(2)
        with c1:
            new_conf = st.selectbox(
                "Update confidence",
                ["(keep)", "high", "medium", "low"], index=0)
        with c2:
            add_tags = st.multiselect(
                "Add tags", sorted(bb.get("tag_definitions", {}).keys()))
        submitted = st.form_submit_button("[ Save note ]", type="primary")
        if submitted:
            if not note.strip():
                st.error("Note is required.")
            else:
                stamp = date.today().isoformat()
                merged = (entry.get("reasoning", "").rstrip()
                          + f"\n\n[{stamp}] {note.strip()}").strip()
                kw = {"reasoning": merged}
                if new_conf != "(keep)":
                    kw["confidence"] = new_conf
                if add_tags:
                    kw["tags"] = sorted(set(entry.get("tags", []) + add_tags))
                _bb_update_entry(bb, eid, **kw)
                st.toast(f"✓ Note added to {horse_name.upper()}", icon="📝")
                st.rerun()


def _tw_surface_of(course: str) -> str:
    """Map a trial/race course string to a Blackbook surface label."""
    return "AWT" if "AWT" in (course or "").upper() else "Turf"


def _tw_trial_card_html(e: dict) -> str:
    """Compact one-line HTML card for a single trial entry (Talent Watch)."""
    badge = _trial_sentiment_badge(e)
    name = e.get("horse_name", "?")
    iso = e.get("date", "")
    dist = e.get("distance_m", "?")
    course = e.get("course", "")
    rp = e.get("running_positions", []) or []
    pos_str = "-".join(str(p) for p in rp)
    fp = rp[-1] if rp else "?"
    n = e.get("n_horses", "?")
    gear = e.get("gear", "")
    gear_html = (f' <span style="color:#a78bfa;font-size:0.85em">{gear}</span>'
                 if gear else "")

    t_flag, t_reason = _trial_time_signal(e)
    t_raw = e.get("time", "")
    if t_raw and t_flag == "fast":
        t_html = (f'<span style="color:#22c55e;font-weight:700" '
                  f'title="{t_reason}">{t_raw}</span>')
    elif t_raw and t_flag == "good":
        t_html = (f'<span style="color:#86efac;font-weight:600" '
                  f'title="{t_reason}">{t_raw}</span>')
    elif t_raw and t_flag == "slow":
        t_html = f'<span style="color:#fca5a5" title="{t_reason}">{t_raw}</span>'
    else:
        t_html = str(t_raw)

    if fp == 1:
        pos_style = "color:#22c55e;font-weight:700"
    elif isinstance(fp, int) and fp <= 3:
        pos_style = "color:#3b82f6;font-weight:600"
    else:
        pos_style = ""

    vid_html = ""
    bn = e.get("batch_number", 0)
    if iso and iso >= "2020-01-01" and bn:
        dc = iso.replace("-", "")
        vurl = _hkjc_trial_video_url(dc, int(bn), course)
        vid_html = (
            f' <a href="{vurl}" target="_blank" rel="noopener noreferrer" '
            f'style="color:#1f6feb;font-weight:700;text-decoration:none" '
            f'title="Watch trial replay">&#9654; replay</a>')

    comment = e.get("comment", "")
    comment_html = (f'<div style="color:#9ca3af;font-size:0.85em;margin-top:2px">'
                    f'{comment}</div>' if comment else "")

    return (
        '<div style="background:#16161f;border-left:3px solid #6366f1;'
        'padding:6px 10px;border-radius:5px">'
        f'{badge}<b style="font-size:1.02em">{name}</b> '
        f'<span style="color:#6b7280;font-size:0.85em">{iso}</span> · '
        f'{dist}m {course} · '
        f'pos <span style="{pos_style}">{pos_str}</span> ({fp}/{n}) '
        f'{t_html}{gear_html}{vid_html}'
        f'{comment_html}'
        '</div>'
    )


def _tw_detect_result_standouts(rj: dict) -> list[dict]:
    """Flag eye-catching performers from a results meeting (Talent Watch).

    Surfaces closers (from the back to the placings), the fastest closing
    sectional in each race, and big-priced top-3 finishers — i.e. horses
    whose run was better than the bare result suggests.
    """
    def _last_sect(ru):
        sects = [s for s in (ru.get("sectiontimes") or []) if str(s).strip()]
        try:
            return float(sects[-1]) if sects else None
        except (ValueError, TypeError):
            return None

    out: list[dict] = []
    for race in (rj or {}).get("races", []):
        rn = race.get("race_number")
        dist = race.get("distance")
        course = race.get("race_course", "")
        runners = race.get("runners") or []
        n = len(runners)
        valid_close = [v for v in (_last_sect(ru) for ru in runners)
                       if v is not None]
        fastest_close = min(valid_close) if valid_close else None
        for ru in runners:
            try:
                place = int(str(ru.get("place", "")).strip())
            except (ValueError, TypeError):
                continue
            reasons: list[str] = []
            poss = [int(p) for p in (ru.get("positions") or [])
                    if str(p).strip().isdigit()]
            early = poss[0] if poss else None
            sfx = ("st" if place == 1 else "nd" if place == 2
                   else "rd" if place == 3 else "th")
            if (early is not None and n >= 6
                    and early >= max(4, int(round(n * 0.6))) and place <= 3):
                reasons.append(f"Closed from {early}th to {place}{sfx}")
            ls = _last_sect(ru)
            if (ls is not None and fastest_close is not None
                    and abs(ls - fastest_close) < 1e-6 and place <= 4):
                reasons.append(f"Fastest last section ({ls:.2f}s)")
            try:
                odds = float(str(ru.get("win_odds", "")).strip())
            except (ValueError, TypeError):
                odds = None
            if odds is not None and odds >= 8 and place <= 3:
                reasons.append(f"Big-odds top-3 (${odds:.0f})")
            if reasons:
                out.append({
                    "race_no": rn, "distance": dist, "course": course,
                    "horse_no": ru.get("horse_no"),
                    "horse_name": ru.get("horse_name", ""),
                    "place": place, "win_odds": ru.get("win_odds", ""),
                    "jockey": ru.get("jockey", ""), "reasons": reasons,
                })
    out.sort(key=lambda d: (d["place"], -len(d["reasons"])))
    return out


def page_talent_watch():
    """Secondary race-day-insight page: latest trials + result standouts.

    Focuses on raw ability (often unrelated to the immediate card) — spot
    horses worth following, watch the replay inline, and bank them straight
    to the Blackbook or annotate an existing selection.
    """
    st.markdown('<div class="page-title">🔭 Talent Watch</div>',
                unsafe_allow_html=True)
    st.caption("Latest barrier trials & standout race runs — find horses with "
               "ability before the market does, watch the replay, and add them "
               "to your Blackbook or comment on existing picks.")

    bb = _load_blackbook()
    bb_lookup = _bb_active_lookup(bb)

    tab_trials, tab_results, tab_watch = st.tabs(
        ["🎽 Fresh Trial Watch", "🏆 Result Standouts",
         f"⭐ Watchlist ({len(bb_lookup)})"])

    # ── TAB 1 · Fresh trial watch ───────────────────────────────────────
    with tab_trials:
        avail = load_available_trials()
        if not avail:
            st.info("No trial reports found. Scrape trials from the Trials page.")
        else:
            c1, c2, c3 = st.columns([1, 1, 1])
            with c1:
                lookback = st.slider("Lookback (days)", 7, 60, 21,
                                     key="tw_trial_lookback")
            with c2:
                strong_only = st.checkbox("Only ★★/★ trials", value=True,
                                          key="tw_trial_strong")
            with c3:
                hide_booked = st.checkbox("Hide already-booked", value=False,
                                          key="tw_trial_hidebooked")
            cutoff = (date.today() - timedelta(days=lookback)).isoformat()

            cards: list[tuple] = []
            for meta in avail:
                iso = meta["date_display"]
                if iso < cutoff:
                    continue
                data = _load_trial_data(str(meta["file"]))
                for batch in data.get("batches", []):
                    bn = batch.get("batch_number", 0)
                    course = batch.get("course", "")
                    bdist = batch.get("distance_m", 0)
                    horses = batch.get("horses", []) or []
                    sorted_fin = sorted(
                        horses,
                        key=lambda h: (h["running_positions"][-1]
                                       if h.get("running_positions") else 999))
                    top4 = [h.get("horse_name", "") for h in sorted_fin[:4]]
                    for h in horses:
                        entry = {
                            **h, "date": iso, "course": course,
                            "distance_m": bdist, "batch_number": bn,
                            "n_horses": batch.get("n_horses", len(horses)),
                            "overall_time": batch.get("overall_time", ""),
                            "top4": top4,
                        }
                        flag, reasons = _trial_sentiment(entry)
                        if strong_only and flag not in ("++", "+"):
                            continue
                        cards.append((flag, entry, reasons))

            if not cards:
                st.info("No trials in the selected window match the filter.")
            else:
                rank = {"++": 0, "+": 1, "": 2, "-": 3}
                cards.sort(key=lambda c: c[1]["date"], reverse=True)
                cards.sort(key=lambda c: rank.get(c[0], 9))
                n_strong = sum(1 for f, _, _ in cards if f == "++")
                st.caption(f"**{len(cards)}** trials flagged "
                           f"(**{n_strong}** very-positive ★★) since {cutoff}.")
                shown = 0
                for flag, entry, reasons in cards:
                    name = entry.get("horse_name", "?")
                    booked = name.strip().upper() in bb_lookup
                    if hide_booked and booked:
                        continue
                    shown += 1
                    if shown > 60:
                        st.caption("… more hidden (narrow the window/filter).")
                        break
                    col_card, col_btn = st.columns([5, 1])
                    with col_card:
                        st.markdown(_tw_trial_card_html(entry),
                                    unsafe_allow_html=True)
                        if reasons:
                            st.caption("· ".join(reasons))
                    with col_btn:
                        key_sfx = f"{name}_{entry.get('date')}_{entry.get('batch_number')}"
                        if booked:
                            eid = bb_lookup[name.strip().upper()]["id"]
                            if st.button("📝 Note", key=f"tw_t_note_{key_sfx}",
                                         width='stretch'):
                                _tw_comment_dialog(eid, name)
                        else:
                            if st.button("⭐ Add", key=f"tw_t_add_{key_sfx}",
                                         width='stretch'):
                                _fg_bb_dialog(
                                    name,
                                    f"Trial {entry.get('date')} B{entry.get('batch_number')}",
                                    entry.get("jockey", ""),
                                    _tw_surface_of(entry.get("course", "")),
                                    entry.get("distance_m", ""))

    # ── TAB 2 · Result standouts ────────────────────────────────────────
    with tab_results:
        dates = sorted(find_results_dates(), reverse=True)
        if not dates:
            st.info("No results found yet.")
        else:
            dc = st.selectbox(
                "Meeting", dates[:10],
                format_func=lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}",
                key="tw_res_date")
            rj = _load_results_json(dc)
            if not rj:
                st.warning("Could not load that results file.")
            else:
                venue = rj.get("venue", "")
                vu = venue.upper()
                track = "ST" if "SHA" in vu else ("HV" if "HAPPY" in vu else "")
                date_slash = f"{dc[:4]}/{dc[4:6]}/{dc[6:]}"
                standouts = _tw_detect_result_standouts(rj)
                st.caption(f"{venue} · {rj.get('n_races', '?')} races · "
                           f"**{len(standouts)}** eye-catching runs flagged.")
                if not standouts:
                    st.info("No standout runs flagged for this meeting.")
                for s in standouts:
                    name = s["horse_name"]
                    booked = name.strip().upper() in bb_lookup
                    col_card, col_btn = st.columns([5, 1])
                    with col_card:
                        rn = s["race_no"]
                        vurl = _hkjc_video_url(date_slash, int(rn), track)
                        place = s["place"]
                        pcol = ("#22c55e" if place == 1 else
                                "#3b82f6" if place <= 3 else "#9ca3af")
                        st.markdown(
                            f'<div style="background:#16161f;border-left:3px '
                            f'solid #f59e0b;padding:6px 10px;border-radius:5px">'
                            f'<b style="font-size:1.02em">{name}</b> '
                            f'<span style="color:{pcol};font-weight:700">'
                            f'· {place}{"st" if place==1 else "nd" if place==2 else "rd" if place==3 else "th"}</span> '
                            f'<span style="color:#6b7280;font-size:0.85em">'
                            f'R{rn} · {s.get("distance","?")}m · ${s.get("win_odds","?")}</span> '
                            f'<a href="{vurl}" target="_blank" '
                            f'rel="noopener noreferrer" '
                            f'style="color:#1f6feb;font-weight:700;'
                            f'text-decoration:none" title="Watch race replay">'
                            f'&#9654; replay</a>'
                            f'<div style="color:#fbbf24;font-size:0.85em;'
                            f'margin-top:2px">{" · ".join(s["reasons"])}</div>'
                            f'</div>', unsafe_allow_html=True)
                    with col_btn:
                        key_sfx = f"{name}_{dc}_{s['race_no']}"
                        if booked:
                            eid = bb_lookup[name.strip().upper()]["id"]
                            if st.button("📝 Note", key=f"tw_r_note_{key_sfx}",
                                         width='stretch'):
                                _tw_comment_dialog(eid, name)
                        else:
                            if st.button("⭐ Add", key=f"tw_r_add_{key_sfx}",
                                         width='stretch'):
                                _fg_bb_dialog(
                                    name,
                                    f"{dc[:4]}-{dc[4:6]}-{dc[6:]} R{s['race_no']}",
                                    s.get("jockey", ""),
                                    _tw_surface_of(s.get("course", "")),
                                    s.get("distance", ""))

    # ── TAB 3 · Watchlist (active blackbook) ────────────────────────────
    with tab_watch:
        active = _bb_active_entries(bb)
        if not active:
            st.info("Blackbook is empty. Add horses from the tabs above.")
        else:
            active.sort(key=lambda e: ({"high": 0, "medium": 1, "low": 2}
                                       .get(e.get("confidence"), 3),
                                       e.get("added_date", "")), reverse=False)
            trial_idx = _load_all_trial_horse_index()
            st.caption(f"**{len(active)}** active selections — newest insight "
                       "first. Click 📝 to annotate.")
            for e in active:
                name = e["horse_name"]
                conf = e.get("confidence", "—")
                ccol = {"high": "#22c55e", "medium": "#fbbf24",
                        "low": "#9ca3af"}.get(conf, "#9ca3af")
                tags = ", ".join(e.get("tags", []) or [])
                recent_trial = ""
                tl = trial_idx.get(name.strip().upper())
                if tl:
                    tl_sorted = sorted(tl, key=lambda x: x.get("date", ""),
                                       reverse=True)
                    rt = tl_sorted[0]
                    badge = _trial_sentiment_badge(rt)
                    recent_trial = (f' · latest trial {badge}'
                                    f'<span style="color:#6b7280;'
                                    f'font-size:0.85em">{rt.get("date","")}</span>')
                col_card, col_btn = st.columns([5, 1])
                with col_card:
                    st.markdown(
                        f'<div style="background:#16161f;border-left:3px solid '
                        f'{ccol};padding:6px 10px;border-radius:5px">'
                        f'<b style="font-size:1.02em">{name}</b> '
                        f'<span style="color:{ccol};font-weight:700;'
                        f'font-size:0.85em">· {conf}</span> '
                        f'<span style="color:#6b7280;font-size:0.85em">'
                        f'· {e.get("source_race","—")}</span>'
                        f'{recent_trial}'
                        + (f'<span style="color:#a78bfa;font-size:0.82em">'
                           f' · {tags}</span>' if tags else "")
                        + f'<div style="color:#9ca3af;font-size:0.85em;'
                        f'margin-top:2px">{e.get("reasoning","")}</div>'
                        f'</div>', unsafe_allow_html=True)
                with col_btn:
                    if st.button("📝 Note", key=f"tw_w_note_{e['id']}",
                                 width='stretch'):
                        _tw_comment_dialog(e["id"], name)


def page_trials():
    """Barrier Trials results page."""
    st.markdown('<div class="page-title">Barrier Trials</div>', unsafe_allow_html=True)

    sidebar_trials()

    trials = load_available_trials()
    if not trials:
        st.info("No trial data available. Use the sidebar to scrape trial results.")
        return

    # ── Tabs: Browse | Lookup | Standouts | Trial Blackbook ─────────────
    tab_browse, tab_lookup, tab_standouts, tab_watch = st.tabs(
        ["Browse by Date", "Horse Lookup", "★ Standouts",
         "👀 Trial Blackbook"])

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

        _trial_date_iso = data.get("date", "") or ""
        for batch in batches:
            _render_trial_batch_table(batch, search_upper, _trial_date_iso)

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

    # ── TAB 4: Trial Blackbook (auto-suggest + manual confirm) ──────────
    with tab_watch:
        _render_trial_blackbook_tab(trials)


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
        f"Generated {hkt_now().strftime('%d %b %Y %H:%M')} HKT "
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


# ══════════════════════════════════════════════════════════════════════════════
# Signal Audit — daily review of factor edges (Data Analysis page)
# ══════════════════════════════════════════════════════════════════════════════

SIGNAL_AUDIT_DIR = REPORTS  # signal_audit_{date_compact}.json lives in reports/


def _audit_meetings_with_results() -> list[str]:
    """Return list of date_compact strings for meetings that have BOTH a
    race_day_report (predictions) AND results JSON (actuals). Newest first."""
    out = []
    for f in REPORTS.glob("results_*.json"):
        m = re.search(r"results_(\d{8})\.json$", f.name)
        if not m:
            continue
        dc = m.group(1)
        # Need at least one prediction file alongside it
        if (REPORTS / f"race_day_report_{dc}_v4.4.json").exists() or \
           (REPORTS / f"race_day_report_{dc}_v3.4.8.json").exists():
            out.append(dc)
    return sorted(set(out), reverse=True)


@st.cache_data(show_spinner=False, ttl=120)
def _build_signal_audit(date_compact: str, _factor_mtime: float) -> dict:
    """For one meeting, compute a per-pick factor-edge audit cross-referenced
    with the actual finishing position from results_*.json. Returns:

        {
            "date":   "YYYY-MM-DD",
            "n_picks": int,
            "rows": [
                {race, rank, horse_no, horse, jockey, trainer, sire,
                 dam_sire, class_step, score, tier, place, top1, top3,
                 top_half, signals: [{label, score, polarity}]},
                ...
            ],
            "summary": [
                {signal_type, n_fired, n_top1, n_top3, n_top_half, hit_rate_top3},
                ...
            ],
        }

    Output is also persisted to reports/signal_audit_{dc}.json so the user can
    diff across days even if the factor table re-bakes.
    """
    pred_path = REPORTS / f"race_day_report_{date_compact}_v4.4.json"
    if not pred_path.exists():
        pred_path = REPORTS / f"race_day_report_{date_compact}_v3.4.8.json"
    res_path  = REPORTS / f"results_{date_compact}.json"
    if not pred_path.exists() or not res_path.exists():
        return {"date": date_compact, "n_picks": 0, "rows": [], "summary": []}

    try:
        pred = json.loads(pred_path.read_text(encoding="utf-8"))
        res  = json.loads(res_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"date": date_compact, "n_picks": 0, "rows": [], "summary": []}

    # Build {race_no: {horse_name_upper: (place, field_size)}}
    place_idx: dict = {}
    for race in res.get("races", []):
        rn = race.get("race_number")
        runners = race.get("runners") or []
        fs = len(runners)
        m = {}
        for ru in runners:
            try:
                pl = int(str(ru.get("place", "")).strip())
            except (TypeError, ValueError):
                pl = None
            if pl is not None and ru.get("horse_name"):
                m[str(ru["horse_name"]).strip().upper()] = (pl, fs)
        place_idx[rn] = m

    # Compute factor edges for the predictions
    edges = _compute_factor_edges(pred.get("races", []),
                                  window="current_season_25_26",
                                  top_n_per_race=8)

    rows: list[dict] = []
    sig_stats: dict = {}   # signal_type -> {n, top1, top3, top_half}

    def _signal_type(label: str) -> str:
        # Coerce labels like "Jky IV 2.47 A/E 0.91" → "Jockey IV"
        s = label.strip()
        if s.startswith("Jky"):    return "Jockey IV"
        if s.startswith("Trn↑"):   return "Trainer × Class Up"
        if s.startswith("Trn↓"):   return "Trainer × Class Down"
        if s.startswith("Trn"):    return "Trainer IV"
        if s.startswith("J×T"):    return "Jockey × Trainer"
        if s.startswith("Sire@"):  return "Sire × Distance"
        if s.startswith("Sire"):   return "Sire IV"
        if s.startswith("DamSire"):return "Dam Sire IV"
        if s.startswith("Fresh"):  return "Days-off (fresh)"
        if s.startswith("Quick"):  return "Days-off (quick b/u)"
        if s.startswith("Rtg Δ +"):return "Rating Δ (up)"
        if s.startswith("Rtg Δ"):  return "Rating Δ (down)"
        return "Other"

    for e in edges:
        rn = e["race"]
        hn_u = (e["horse"] or "").strip().upper()
        place_tup = place_idx.get(rn, {}).get(hn_u)
        if place_tup is None:
            place, fs = None, None
        else:
            place, fs = place_tup
        top1 = (place == 1) if place is not None else None
        top3 = (place is not None and place <= 3)
        top_half = (place is not None and fs and place <= max(1, fs // 2))

        signal_objs = []
        for label, _colour, score in e["signals"]:
            polarity = "pos" if score > 0 else "neg"
            stype = _signal_type(label)
            signal_objs.append({"label": label, "score": round(score, 3),
                                "polarity": polarity, "type": stype})
            # Tally only if outcome known
            if place is not None:
                d = sig_stats.setdefault(
                    stype, {"n": 0, "top1": 0, "top3": 0, "top_half": 0,
                            "polarity": polarity})
                d["n"] += 1
                if top1: d["top1"] += 1
                if top3: d["top3"] += 1
                if top_half: d["top_half"] += 1

        rows.append({
            "race":     rn,
            "rank":     e["rank"],
            "horse_no": e["horse_no"],
            "horse":    e["horse"],
            "jockey":   e["jockey"],
            "trainer":  e["trainer"],
            "sire":     e["sire"],
            "score":    e["score"],
            "tier":     e["tier"],
            "place":    place,
            "field_size": fs,
            "top1":     top1,
            "top3":     top3 if place is not None else None,
            "top_half": top_half if place is not None else None,
            "signals":  signal_objs,
        })

    summary = []
    for stype, d in sorted(sig_stats.items(),
                           key=lambda kv: -kv[1]["top3"] / max(1, kv[1]["n"])):
        n = d["n"] or 1
        summary.append({
            "Signal":   stype,
            "Polarity": d["polarity"],
            "Fired":    d["n"],
            "Top-1":    d["top1"],
            "Top-3":    d["top3"],
            "Top-Half": d["top_half"],
            "Top-1 %":  round(d["top1"] / n * 100, 1),
            "Top-3 %":  round(d["top3"] / n * 100, 1),
            "Top-Half %": round(d["top_half"] / n * 100, 1),
        })

    iso_date = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"
    out = {"date": iso_date, "date_compact": date_compact,
           "n_picks": len(rows), "rows": rows, "summary": summary}

    # Persist (best-effort) so daily history is queryable
    try:
        (REPORTS / f"signal_audit_{date_compact}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass

    return out


def _signal_audit_tab(window: str, factor_mtime: float) -> None:
    """Render the Signal Audit tab on the Data Analysis page.

    Lets the user pick one or more race days, see for each pick which factor
    edges fired, and audit those edges against actual finishing positions.
    """
    st.markdown("### 🎯 Signal Audit — factor edges vs actuals")
    st.caption(
        "For each day's top picks, list every factor-edge signal that fired "
        "(jockey/trainer/sire/days-off/etc.) and check whether the horse "
        "actually delivered. **Use this to track which data-analysis signals "
        "are paying off day by day.**"
    )

    dcs = _audit_meetings_with_results()
    if not dcs:
        st.info(
            "No meetings yet have BOTH a model report and a results file. "
            "Once a card is analysed AND results are scraped, the audit will "
            "appear here."
        )
        return

    # Date selector
    label_map = {dc: f"{dc[:4]}-{dc[4:6]}-{dc[6:]}" for dc in dcs}
    cA, cB = st.columns([1.4, 2])
    with cA:
        sel_dcs = st.multiselect(
            "Meeting(s) to audit",
            options=dcs,
            default=[dcs[0]],
            format_func=lambda dc: label_map[dc],
            key="sa_dates",
        )
    with cB:
        view_mode = st.radio(
            "View",
            ["Per-pick detail", "Per-signal summary", "Cross-meeting trend"],
            horizontal=True, key="sa_view",
        )

    if not sel_dcs:
        st.info("Select at least one meeting.")
        return

    # Compute audits for all selected days (cached)
    audits = [_build_signal_audit(dc, factor_mtime) for dc in sel_dcs]

    # ── Per-pick detail ─────────────────────────────────────────────────
    if view_mode == "Per-pick detail":
        for a in audits:
            st.markdown(f"#### {a['date']} — {a['n_picks']} picks audited")
            if not a["rows"]:
                st.caption("No picks with both predictions and results.")
                continue
            flat_rows = []
            for r in a["rows"]:
                # Render signals as one cell of compact chips
                if r["signals"]:
                    sig_str = " · ".join(
                        f"[{s['type']}] {s['label']}" for s in r["signals"]
                    )
                else:
                    sig_str = "—"
                flat_rows.append({
                    "R":      r["race"],
                    "Rk":     r["rank"],
                    "#":      r["horse_no"],
                    "Horse":  r["horse"],
                    "Jockey": r["jockey"],
                    "Tier":   r["tier"],
                    "Score":  r["score"],
                    "Place":  r["place"] if r["place"] is not None else "—",
                    "Top-1":  "✅" if r["top1"] else ("—" if r["top1"] is None else "❌"),
                    "Top-3":  "✅" if r["top3"] else ("—" if r["top3"] is None else "❌"),
                    "Top-½":  "✅" if r["top_half"] else ("—" if r["top_half"] is None else "❌"),
                    "Signals": sig_str,
                })
            df = pd.DataFrame(flat_rows)
            st.dataframe(
                df, hide_index=True, width='stretch',
                column_config={
                    "R":     st.column_config.NumberColumn(format="%d"),
                    "Rk":    st.column_config.NumberColumn(format="%d"),
                    "Score": st.column_config.NumberColumn(format="%.2f"),
                },
            )

    # ── Per-signal summary ──────────────────────────────────────────────
    elif view_mode == "Per-signal summary":
        # Aggregate across selected meetings
        agg: dict = {}
        for a in audits:
            for s in a["summary"]:
                d = agg.setdefault(
                    s["Signal"],
                    {"Polarity": s["Polarity"], "Fired": 0,
                     "Top-1": 0, "Top-3": 0, "Top-Half": 0},
                )
                d["Fired"]    += s["Fired"]
                d["Top-1"]    += s["Top-1"]
                d["Top-3"]    += s["Top-3"]
                d["Top-Half"] += s["Top-Half"]
        rows = []
        for sig, d in agg.items():
            n = d["Fired"] or 1
            rows.append({
                "Signal":     sig,
                "Polarity":   d["Polarity"],
                "Fired":      d["Fired"],
                "Top-1":      d["Top-1"],
                "Top-3":      d["Top-3"],
                "Top-Half":   d["Top-Half"],
                "Top-1 %":    round(d["Top-1"]    / n * 100, 1),
                "Top-3 %":    round(d["Top-3"]    / n * 100, 1),
                "Top-Half %": round(d["Top-Half"] / n * 100, 1),
            })
        if not rows:
            st.info("No signals fired across selected meetings.")
            return
        df_sum = pd.DataFrame(rows)
        # User can sort interactively via dataframe column headers.
        st.dataframe(
            df_sum.sort_values("Top-3 %", ascending=False),
            hide_index=True, width='stretch',
            column_config={
                "Fired":      st.column_config.NumberColumn(format="%d"),
                "Top-1":      st.column_config.NumberColumn(format="%d"),
                "Top-3":      st.column_config.NumberColumn(format="%d"),
                "Top-Half":   st.column_config.NumberColumn(format="%d"),
                "Top-1 %":    st.column_config.NumberColumn(format="%.1f%%"),
                "Top-3 %":    st.column_config.NumberColumn(format="%.1f%%"),
                "Top-Half %": st.column_config.NumberColumn(format="%.1f%%"),
            },
        )
        st.caption(
            "**Interpretation**: a positive signal is *working* if its Top-3% "
            "exceeds the baseline (~25–30% for a random pick from the field). "
            "A negative signal is *working* if Top-3% stays LOW."
        )

    # ── Cross-meeting trend ─────────────────────────────────────────────
    else:  # Cross-meeting trend
        rows = []
        for a in audits:
            by_sig = {s["Signal"]: s for s in a["summary"]}
            for sig, s in by_sig.items():
                rows.append({
                    "Date":     a["date"],
                    "Signal":   sig,
                    "Fired":    s["Fired"],
                    "Top-3":    s["Top-3"],
                    "Top-3 %":  s["Top-3 %"],
                    "Top-Half %": s["Top-Half %"],
                })
        if not rows:
            st.info("No signals fired across selected meetings.")
            return
        df_t = pd.DataFrame(rows).sort_values(["Signal", "Date"])
        st.dataframe(df_t, hide_index=True, width='stretch')


# ──────────────────────────────────────────────────────────────────────────────
# Horse Profile (per-horse pivot of model rank vs finish, perf score, excuses)
# Top-level nav page — backed by horse_intel.py + master DB
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=120, show_spinner=False)
def _horse_intel_index(_mtime: float) -> dict:
    """Load horse_intel/_index.json. Cache key is the file's mtime so a
    rebuild invalidates automatically."""
    p = BASE / "reports" / "horse_intel" / "_index.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


@st.cache_data(ttl=120, show_spinner=False)
def _horse_intel_record(slug: str, _mtime: float) -> dict | None:
    p = BASE / "reports" / "horse_intel" / f"{slug}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _horse_intel_mtime() -> float:
    p = BASE / "reports" / "horse_intel" / "_index.json"
    return p.stat().st_mtime if p.exists() else 0.0


def _horse_search(query: str, idx: dict, limit: int = 12) -> list[str]:
    """Substring + fuzzy match horse names. Ranks by:
       1. exact equality
       2. starts-with
       3. token starts-with (any word starts with the query)
       4. substring
    Within each tier, prefers more recently active horses."""
    q = (query or "").upper().strip()
    if not q:
        # No query → most recently active horses (top-N by last_run_date)
        scored = []
        for nm, meta in idx.items():
            d = (meta or {}).get("last_run_date", "") if isinstance(meta, dict) else ""
            scored.append((d, nm))
        scored.sort(reverse=True)
        return [nm for _, nm in scored[:limit]]

    tiers: list[list[tuple[str, str]]] = [[], [], [], []]
    for nm, meta in idx.items():
        d = (meta or {}).get("last_run_date", "") if isinstance(meta, dict) else ""
        if nm == q:
            tiers[0].append((d, nm))
        elif nm.startswith(q):
            tiers[1].append((d, nm))
        elif any(tok.startswith(q) for tok in nm.split()):
            tiers[2].append((d, nm))
        elif q in nm:
            tiers[3].append((d, nm))
    out: list[str] = []
    for t in tiers:
        t.sort(reverse=True)  # recent first
        for _, nm in t:
            if nm not in out:
                out.append(nm)
            if len(out) >= limit:
                return out
    return out


@st.cache_data(show_spinner=False, ttl=900)
def _horse_cycle_profile_cached(horse_name: str) -> dict | None:
    """Cached wrapper around horse_cycle.compute_profile_cycle (by name)."""
    try:
        from horse_cycle import compute_profile_cycle
        return compute_profile_cycle(horse_name=horse_name)
    except Exception:
        return None


def _render_horse_cycle_panel(horse_name: str) -> None:
    """Rating-cycle / class-dropper panel for the Horse Profile page.

    Shows where the horse's current rating sits inside its own winning band
    (a thermometer), its per-condition winning bands, and a plain-language
    read on its cycle state (PRIMED / fading / approaching, etc.).
    """
    cyc = _horse_cycle_profile_cached(horse_name)
    if not cyc:
        return
    cur = cyc.get("current_rating")
    bmin, bmean, bmax = cyc.get("band_min"), cyc.get("band_mean"), cyc.get("band_max")
    flag = cyc.get("primary_flag", "NO_DATA")
    state = cyc.get("cycle_state", "unknown")

    flag_style = {
        "PRIMED":         ("#22c55e", "🟢 PRIMED — back in winning band"),
        "EARLY_DROPPER":  ("#84cc16", "🫒 EARLY DROPPER — at band, freshly off a win"),
        "WATCH":          ("#f59e0b", "🟡 WATCH — easing toward band"),
        "FADE_OVERRATED": ("#ef4444", "🔴 FADE — at career-high after a win"),
        "NEUTRAL":        ("#9ca3af", "⚪ NEUTRAL"),
        "NO_DATA":        ("#6b7280", "· insufficient win history"),
    }
    col, label = flag_style.get(flag, ("#9ca3af", flag))

    st.markdown("#### 🌡️ Rating Cycle")
    top = st.columns([1.1, 1.6])
    with top[0]:
        st.markdown(
            f'<div style="border-left:4px solid {col};padding:6px 12px;'
            f'background:rgba(255,255,255,0.03);border-radius:6px">'
            f'<div style="font-weight:800;color:{col};font-size:1.0em">{label}</div>'
            f'<div style="opacity:0.8;font-size:0.85em;margin-top:3px">{cyc.get("note","")}</div>'
            f'</div>', unsafe_allow_html=True)
        rsw = cyc.get("runs_since_last_win")
        st.caption(
            f"Current rating **{cur:.0f}**" if cur is not None else "Current rating —"
        )
        st.caption(
            f"Winning band **{bmin:.0f}–{bmax:.0f}** (mean {bmean:.0f}) · "
            f"{cyc.get('n_wins',0)}W/{cyc.get('n_runs',0)}R · "
            f"{rsw if rsw is not None else '—'} runs since last win"
            if bmean is not None else "No rated wins on record."
        )

    with top[1]:
        # Thermometer: current rating marker inside the band range (+/- margins)
        if cur is not None and bmin is not None and bmax is not None:
            lo = min(bmin, cur) - 3
            hi = max(bmax, cur) + 3
            span = max(1.0, hi - lo)
            band_l = (bmin - lo) / span * 100
            band_w = (bmax - bmin) / span * 100
            cur_p = (cur - lo) / span * 100
            st.markdown(
                f'<div style="margin-top:24px;position:relative;height:34px">'
                f'<div style="position:absolute;top:12px;left:0;right:0;height:10px;'
                f'background:rgba(255,255,255,0.06);border-radius:5px"></div>'
                f'<div style="position:absolute;top:12px;left:{band_l:.1f}%;'
                f'width:{max(band_w,1.5):.1f}%;height:10px;background:rgba(34,197,94,0.45);'
                f'border-radius:5px" title="Winning band"></div>'
                f'<div style="position:absolute;top:6px;left:{cur_p:.1f}%;'
                f'transform:translateX(-50%);width:3px;height:22px;background:{col}"></div>'
                f'<div style="position:absolute;top:-6px;left:{cur_p:.1f}%;'
                f'transform:translateX(-50%);font-size:0.72em;color:{col};'
                f'font-weight:700;white-space:nowrap">now {cur:.0f}</div>'
                f'<div style="position:absolute;bottom:-8px;left:{band_l:.1f}%;'
                f'font-size:0.68em;opacity:0.6">{bmin:.0f}</div>'
                f'<div style="position:absolute;bottom:-8px;left:{(band_l+band_w):.1f}%;'
                f'transform:translateX(-100%);font-size:0.68em;opacity:0.6">{bmax:.0f}</div>'
                f'</div>', unsafe_allow_html=True)

    cond_bands = cyc.get("cond_bands") or []
    if cond_bands:
        with st.expander("Per-condition winning bands", expanded=False):
            df = pd.DataFrame([{
                "Venue": c["venue"], "Surface": c["surface"],
                "Dist": f'{c["distance"]}m', "Wins": c["n_wins"],
                "Band": f'{c["band_min"]:.0f}–{c["band_max"]:.0f}',
                "Mean": f'{c["band_mean"]:.0f}',
            } for c in cond_bands])
            st.dataframe(df, hide_index=True, width='stretch')
    st.divider()


def page_fixtures():
    """Fixture-targeting board — anticipate which horses are aimed where.

    Renders a month-style calendar of upcoming fixtures with the number of
    anticipated targets per day, plus per-fixture detail panels. The
    anticipation is an inference from each horse's preferred conditions,
    prep timing, and rating-cycle state — NOT an official declaration.
    """
    from datetime import datetime as _dt, timedelta as _td

    st.markdown('<div class="page-title">Fixture Targeting</div>',
                unsafe_allow_html=True)
    st.markdown(
        '<div class="page-subtitle">Where we anticipate horses are being '
        'aimed &middot; inference, not official entries</div>',
        unsafe_allow_html=True)

    ctrl = st.columns([1, 1, 2])
    with ctrl[0]:
        weeks = st.slider("Weeks ahead", 2, 10, 6, key="fx_weeks")
    with ctrl[1]:
        rebuild = st.button("🔄 Rebuild", key="fx_rebuild")
    with ctrl[2]:
        only_signals = st.toggle(
            "Cycle signals only (PRIMED / dropper / watch)",
            value=True, key="fx_only_signals")

    try:
        import fixture_targeting as _ft
        if rebuild:
            payload = _ft.anticipate_targets(weeks=weeks)
            out = REPORTS / "fixture_targets.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, indent=2, default=str),
                           encoding="utf-8")
        else:
            payload = _ft.load_or_build_targets(weeks=weeks)
    except Exception as e:
        st.error(f"Fixture targeting failed: {e}")
        return

    fixtures = payload.get("fixtures", [])
    by_date = payload.get("by_date", {})
    if not fixtures:
        st.info("No upcoming fixtures resolved.")
        return

    n_prov = sum(1 for f in fixtures if f.get("provisional"))
    st.caption(
        f"Data through **{payload.get('latest_data_date','?')}** · "
        f"{len(fixtures)} fixtures · {len(payload.get('targets', []))} "
        f"anticipated targets · generated {payload.get('generated_at','')[:16]}")
    if n_prov:
        st.warning(
            f"⚠️ {n_prov} of {len(fixtures)} fixtures are **provisional** "
            "(standard Wed-HV / Sun-ST cadence). Drop an authoritative "
            "`data/fixtures.json` (list of `{date, venue, surface}`) to "
            "override.")

    # ── Calendar grid ─────────────────────────────────────────────
    fx_by_date = {f["date"]: f for f in fixtures}
    fdates = sorted(_dt.strptime(f["date"], "%Y-%m-%d").date() for f in fixtures)
    start = fdates[0] - _td(days=fdates[0].weekday())          # Monday on/before
    end = fdates[-1] + _td(days=(6 - fdates[-1].weekday()))    # Sunday on/after

    st.markdown("#### 🗓️ Calendar")
    hdr = st.columns(7)
    for i, dn in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        hdr[i].markdown(
            f"<div style='text-align:center;opacity:0.6;font-size:0.8em;"
            f"font-weight:700'>{dn}</div>", unsafe_allow_html=True)

    day = start
    while day <= end:
        wk = st.columns(7)
        for i in range(7):
            iso = day.isoformat()
            fx = fx_by_date.get(iso)
            with wk[i]:
                if fx:
                    venue = fx["venue"]
                    vcol = "#60a5fa" if venue == "ST" else "#a78bfa"
                    n_t = len(by_date.get(iso, []))
                    n_sig = sum(1 for t in by_date.get(iso, [])
                                if t.get("cycle_flag") in
                                ("PRIMED", "EARLY_DROPPER", "WATCH"))
                    prov = "·prov" if fx.get("provisional") else ""
                    st.markdown(
                        f"<div style='border:1px solid {vcol};border-radius:6px;"
                        f"padding:4px 6px;min-height:62px;background:"
                        f"rgba(96,165,250,0.06)'>"
                        f"<div style='font-size:0.78em;opacity:0.7'>"
                        f"{day.day}{prov}</div>"
                        f"<div style='font-weight:800;color:{vcol}'>{venue}</div>"
                        f"<div style='font-size:0.72em;opacity:0.8'>"
                        f"{n_t} aimed · <b>{n_sig}</b> sig</div>"
                        f"</div>", unsafe_allow_html=True)
                else:
                    faint = "0.25" if day.month == fdates[0].month else "0.12"
                    st.markdown(
                        f"<div style='padding:4px 6px;min-height:62px;'>"
                        f"<div style='font-size:0.78em;opacity:{faint}'>"
                        f"{day.day}</div></div>", unsafe_allow_html=True)
            day += _td(days=1)

    st.markdown("---")

    # ── Per-fixture anticipated targets ───────────────────────────
    st.markdown("#### Anticipated targets by fixture")
    flag_disp = {
        "PRIMED": ("#22c55e", "🟢 PRIMED"),
        "EARLY_DROPPER": ("#84cc16", "🫒 DROPPER"),
        "WATCH": ("#f59e0b", "🟡 WATCH"),
        "FADE_OVERRATED": ("#ef4444", "🔴 FADE"),
        "NEUTRAL": ("#9ca3af", "⚪"),
        "NO_DATA": ("#6b7280", "·"),
    }
    for f in fixtures:
        iso = f["date"]
        items = by_date.get(iso, [])
        if only_signals:
            items = [t for t in items if t.get("cycle_flag") in
                     ("PRIMED", "EARLY_DROPPER", "WATCH")]
        if not items:
            continue
        venue = f["venue"]
        vname = {"ST": "Sha Tin", "HV": "Happy Valley"}.get(venue, venue)
        prov = " · provisional" if f.get("provisional") else ""
        with st.expander(
            f"{iso} — {vname} ({venue}){prov} · {len(items)} candidates",
            expanded=False):
            for t in items[:25]:
                col, lbl = flag_disp.get(t.get("cycle_flag"), ("#9ca3af", ""))
                rtg = t.get("current_rating")
                rtg_s = f"R{rtg:.0f}" if rtg is not None else "R—"
                st.markdown(
                    f'<div style="padding:3px 0;border-left:3px solid {col};'
                    f'padding-left:8px;margin-bottom:3px">'
                    f'<span style="opacity:0.55;font-size:0.8em">'
                    f'{t.get("score",0):.2f}</span> '
                    f'<span style="font-weight:700">'
                    f'{str(t.get("horse_name","")).title()}</span> '
                    f'<span style="color:{col};font-size:0.78em;font-weight:700">'
                    f'{lbl}</span>'
                    f' <span style="opacity:0.6;font-size:0.82em">'
                    f'· {t.get("trainer","")} · {rtg_s}</span>'
                    f'<div style="font-size:0.76em;opacity:0.72">'
                    f'{t.get("reason","")}</div>'
                    f'</div>', unsafe_allow_html=True)


def page_horse_profile():
    """Per-horse contextualised performance — pivot of master DB +
    reports + commentary into a single horse view. Top-level nav page."""
    import subprocess as _sp
    import datetime as _dt

    st.title("🐴 Horse Profile")
    st.caption(
        "Per-horse pivot of model rank vs. finish, performance score, "
        "and excuse tags. Sourced from `hkjc_results_updated.xlsx` "
        "(full season history) + `reports/*` (commentary, ET/SARR ranks, "
        "actual pace). Pure read on existing data — no new model. Use as "
        "a complement to the factor model, not a substitute."
    )

    mtime = _horse_intel_mtime()
    idx = _horse_intel_index(mtime)

    # ── Staleness check — DB newer than horse-intel index? ─────────────
    db_path = BASE / "hkjc_results_updated.xlsx"
    db_mtime = db_path.stat().st_mtime if db_path.exists() else 0.0
    is_stale = bool(db_mtime and mtime and db_mtime > mtime + 1)
    if is_stale:
        try:
            with st.spinner("Master DB has new data — rebuilding Horse "
                            "Intel index…"):
                r = _sp.run(
                    [sys.executable, str(BASE / "horse_intel.py")],
                    capture_output=True, text=True, timeout=300,
                    cwd=str(BASE),
                )
            if r.returncode == 0:
                _horse_intel_index.clear()
                _horse_intel_record.clear()
                mtime = _horse_intel_mtime()
                idx = _horse_intel_index(mtime)
                st.toast("Horse Intel auto-rebuilt from latest DB.",
                         icon="🐴")
            else:
                st.warning(
                    "Auto-rebuild failed — click **🔄 Rebuild** below.\n\n"
                    f"```\n{(r.stderr or r.stdout)[-800:]}\n```"
                )
        except (OSError, _sp.TimeoutExpired) as e:
            st.warning(f"Auto-rebuild skipped: {e}")

    # ── Top status bar ─────────────────────────────────
    c_st1, c_st2, c_st3 = st.columns([3, 1, 1])
    with c_st1:
        if mtime:
            mt = hkt_from_ts(mtime)
            total_runs = sum(
                m.get("n_runs", 0) if isinstance(m, dict) else 0
                for m in idx.values()
            )
            st.caption(
                f"Index built **{mt:%Y-%m-%d %H:%M} HKT** · "
                f"**{len(idx):,}** horses · **{total_runs:,}** runs"
            )
        else:
            st.warning("Horse intel index not built yet — click Rebuild.")
    with c_st2:
        only_today = st.checkbox(
            "Today's card only", value=False, key="hi_only_today",
            help="Restrict search to runners on the most recent racecard.",
        )
    with c_st3:
        if st.button("🔄 Rebuild", key="hi_rebuild",
                     width='stretch',
                     help="Re-run horse_intel.py against the master DB."):
            try:
                with st.spinner("Rebuilding from master DB…"):
                    r = _sp.run(
                        [sys.executable, str(BASE / "horse_intel.py")],
                        capture_output=True, text=True, timeout=300,
                        cwd=str(BASE),
                    )
                if r.returncode == 0:
                    st.success(r.stdout.strip()[-200:] or "Rebuilt.")
                    _horse_intel_index.clear()
                    _horse_intel_record.clear()
                    st.rerun()
                else:
                    st.error((r.stderr or r.stdout)[-1500:])
            except (OSError, _sp.TimeoutExpired) as e:
                st.error(str(e))

    if not idx:
        st.info(
            "No horses indexed. Click **Rebuild** above (it reads from "
            "`hkjc_results_updated.xlsx` and takes ~5 s)."
        )
        return

    # ── Filter pool by today's card if asked ───────────
    pool = idx
    if only_today:
        try:
            cards = sorted((BASE / "racecards").glob("racecard_*.xlsx"))
            if cards:
                df_card = pd.read_excel(cards[-1], sheet_name="All Races")
                if "is_standby" in df_card.columns:
                    df_card = df_card[df_card["is_standby"] == False]
                today_runners = {
                    str(n).upper().strip()
                    for n in df_card["horse_name"].dropna().tolist()
                }
                pool = {k: v for k, v in idx.items() if k in today_runners}
                if not pool:
                    st.warning(
                        "No runners on today's card are in the index "
                        "(first-starters won't have history). Showing all "
                        "horses instead."
                    )
                    pool = idx
        except Exception as e:
            st.caption(f"Could not load today's racecard: {e}")

    # ── Search bar (intuitive: as-you-type substring + fuzzy) ──
    pre_pick = st.session_state.get("hi_pick", "")
    query = st.text_input(
        "🔍 Search horse",
        value=st.session_state.get("hi_search", ""),
        key="hi_search",
        placeholder="Type any part of a name — e.g. 'might', 'profit', "
                    "'son pak', 'galaxy'…",
        help="Substring match. Hit Enter or click a result below to "
             "open. Empty = most recently active horses.",
    )

    matches = _horse_search(query, pool, limit=12)

    if not matches:
        st.info(f"No horses match **{query!r}**.")
        return

    # Render result cards as buttons in a 3-column grid
    st.caption(f"**{len(matches)}** result(s)" + (
        " (top 12 — refine your search to narrow)" if len(matches) >= 12 else ""
    ))
    pick = pre_pick if pre_pick in matches else None

    cols = st.columns(3)
    for i, name in enumerate(matches):
        meta = pool.get(name, {}) if isinstance(pool.get(name), dict) else {}
        n = meta.get("n_runs", 0)
        wp = meta.get("win_pct", 0.0)
        last_d = meta.get("last_run_date", "")
        last_f = meta.get("last_finish", "")
        psm = meta.get("perf_score_mean")
        bb = meta.get("blackbook", False)
        psm_str = f"{psm:+.2f}" if psm is not None else "—"
        label = (
            f"**{name}**" + (" 📓" if bb else "")
            + f"  \n{n} runs · W {wp:.0f}% · μperf {psm_str}"
            + (f"  \nlast: {last_d} → P{last_f}" if last_d else "")
        )
        with cols[i % 3]:
            # Use a button styled to look like a card
            if st.button(label, key=f"hi_pick_{name}",
                         width='stretch',
                         type="primary" if name == pick else "secondary"):
                st.session_state["hi_pick"] = name
                pick = name
                st.rerun()

    if pick is None:
        # Auto-pick top result if user hasn't clicked yet
        pick = matches[0]

    st.divider()

    meta = pool.get(pick, {}) if isinstance(pool.get(pick), dict) else {}
    fname = meta.get("file") if isinstance(meta, dict) else f"{pick}.json"
    slug = (fname or "").replace(".json", "") if fname else pick.replace(" ", "_")
    rec = _horse_intel_record(slug, mtime)
    if not rec:
        st.error(f"Could not load record for **{pick}** (slug={slug}).")
        return

    s = rec.get("summary", {}) or {}
    runs = rec.get("runs", []) or []

    # ── Header ────────────────────────────────────────
    head_l, head_r = st.columns([3, 1])
    with head_l:
        st.markdown(f"## {pick}")
    with head_r:
        # HKJC profile link (search redirect — robust to any horse_id format)
        url = ("https://racing.hkjc.com/racing/information/English/Horse/"
               "SelectHorse.aspx?HorseName=" + pick.replace(" ", "+"))
        st.markdown(f"[🔗 HKJC profile]({url})")

    h1, h2, h3, h4, h5 = st.columns(5)
    h1.metric("Runs", s.get("n_runs", 0))
    h2.metric("Win %", f"{s.get('win_pct', 0):.1f}%")
    h3.metric("Top-3 %", f"{s.get('top3_pct', 0):.1f}%")
    abp = s.get("avg_beat_proj_s")
    h4.metric(
        "Avg vs proj",
        f"{abp:+.2f}s" if abp is not None else "—",
        help="Negative = ran faster than ET projected. "
             "Only counts runs covered by an ET race-day report (Apr-26+).",
    )
    psm = s.get("perf_score_mean")
    h5.metric(
        "Avg perf score",
        f"{psm:+.2f}" if psm is not None else "—",
        help="Mean contextualised performance score. Higher = consistently "
             "positive context (late kicks, beats projection, dominant "
             "wins). Negative = recurring underperformance.",
    )

    bb_status = s.get("blackbook_status")
    if bb_status:
        st.success(
            f"📓 **In blackbook** — {bb_status}"
            + (f" — _{s.get('blackbook_note', '')}_"
               if s.get("blackbook_note") else "")
        )

    # ── Rating-cycle / class-dropper panel ──────────────────
    _render_horse_cycle_panel(pick)

    # Recurring excuses
    recs = s.get("recurring_excuses") or {}
    if recs:
        chips = " ".join(
            f"<span style='display:inline-block;padding:2px 8px;margin:2px;"
            f"border-radius:10px;background:#3a2540;color:#f4d4ff;"
            f"font-size:0.8em;'>{tag} ×{ct}</span>"
            for tag, ct in recs.items()
        )
        st.markdown(f"**Recurring tags:** {chips}", unsafe_allow_html=True)

    if not runs:
        st.info("No runs in history.")
        return

    # ── Run history table ────────────────────────────
    rows_t = []
    for r in runs:
        beat = r.get("beat_proj_s")
        rows_t.append({
            "Date":     r.get("date", ""),
            "Vn":       r.get("venue", ""),
            "R":        r.get("race_number"),
            "Dist":     r.get("distance"),
            "Surf":     r.get("surface"),
            "Going":    r.get("going"),
            "Cls":      r.get("race_class"),
            "Drw":      r.get("draw"),
            "Fin":      r.get("finish"),
            "LBW":      r.get("lbw"),
            "Odds":     r.get("win_odds"),
            "Jockey":   r.get("jockey"),
            "ET#":      r.get("et_rank"),
            "SARR#":    r.get("sarr_rank"),
            "Δproj(s)": (round(beat, 2) if beat is not None else None),
            "Perf":     r.get("perf_score"),
            "Tags":     ", ".join(r.get("tags") or []),
            "Note":     r.get("comment_short", ""),
        })
    df_runs = pd.DataFrame(rows_t)
    # Show most recent first by default
    df_runs = df_runs.iloc[::-1].reset_index(drop=True)

    st.markdown("#### Run history (most recent first)")
    st.dataframe(
        df_runs, hide_index=True, width='stretch',
        column_config={
            "R":        st.column_config.NumberColumn(format="%d"),
            "Dist":     st.column_config.NumberColumn(format="%d"),
            "Drw":      st.column_config.NumberColumn(format="%d"),
            "Fin":      st.column_config.NumberColumn(format="%d"),
            "ET#":      st.column_config.NumberColumn(format="%d"),
            "SARR#":    st.column_config.NumberColumn(format="%d"),
            "Δproj(s)": st.column_config.NumberColumn(format="%+.2f"),
            "Perf":     st.column_config.NumberColumn(format="%+.2f"),
        },
    )

    # ── Perf score timeline ──────────────────────────
    try:
        import altair as _alt
        df_ts = df_runs[["Date", "Perf", "Fin"]].copy()
        df_ts["Date"] = pd.to_datetime(df_ts["Date"], errors="coerce")
        df_ts = df_ts.dropna(subset=["Date"]).sort_values("Date")
        if not df_ts.empty:
            df_ts["Color"] = df_ts["Perf"].apply(
                lambda v: "good" if (v or 0) > 0.5
                else ("bad" if (v or 0) < -0.3 else "neutral")
            )
            chart = _alt.Chart(df_ts).mark_bar(
                cornerRadiusTopLeft=2, cornerRadiusTopRight=2,
            ).encode(
                x=_alt.X("Date:T", title=None),
                y=_alt.Y("Perf:Q", title="Perf score"),
                color=_alt.Color(
                    "Color:N",
                    scale=_alt.Scale(
                        domain=["good", "neutral", "bad"],
                        range=["#22c55e", "#94a3b8", "#ef4444"],
                    ),
                    legend=None,
                ),
                tooltip=["Date:T", "Fin:Q", "Perf:Q"],
            ).properties(height=180).configure_view(strokeWidth=0)
            st.markdown("#### Perf score timeline")
            st.altair_chart(chart, width='stretch')
    except ImportError:
        pass

    # ── Per-run reasons drill-down ───────────────────
    with st.expander("Per-run perf reasons (model excuses & highlights)",
                     expanded=False):
        for r in reversed(runs):
            d = r.get("date", "")
            rn = r.get("race_number", "?")
            fin = r.get("finish", "?")
            ps = r.get("perf_score", 0.0)
            reasons = r.get("perf_reasons") or []
            tags = r.get("tags") or []
            note = r.get("comment_short", "")
            line = f"- **{d} R{rn}** · P{fin} · perf {ps:+.2f}"
            if reasons:
                line += " · " + "; ".join(reasons)
            if tags:
                line += f" · _tags: {', '.join(tags)}_"
            if note:
                line += f" · _{note}_"
            st.markdown(line)

    st.caption(
        "**Reading guide.** `Perf` is a contextualised performance score "
        "borrowed from `backtest_model.find_exceptional_performers` — it "
        "rewards dominant wins, late kicks, and beating projection; it "
        "penalises high-rank model picks that miss the board. "
        "**ET#/SARR#/Δproj** are populated only for meetings that have a "
        "race-day report on disk (Apr-26 onwards) — older runs show as —. "
        "Use as a sanity-check for blackbook adds and to spot recurring "
        "trip issues, not as a standalone bet trigger."
    )


def page_data_analysis():
    """Factor-analysis browser backed by reports/factor_analysis_tables.json.

    All heavy work (JSON parse, numeric casting, Summary aggregation) is cached
    per (window, key, file-mtime) so UI interactions (sliders, tab switches)
    do NOT re-run the analysis pipeline — they only re-filter cached DataFrames.
    """
    st.markdown('<div class="page-title">Data Analysis</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="page-subtitle">Independent factor model — jockey / trainer / '
        'pedigree / form-context impact on HKJC outcomes</div>',
        unsafe_allow_html=True,
    )

    mtime = _factor_tables_mtime()
    tables = _load_factor_tables_cached(mtime)

    # Quick diagnostic — if class-step tables are missing, user probably
    # has a stale deployment and should trigger a redeploy or regenerate.
    _cls_keys = ("class_step", "trainer_x_class_step",
                 "trainer_class_up", "trainer_class_down")
    _has_cls = any(
        (tables.get(w, {}) or {}).get(k)
        for w in ("current_season_25_26", "all_time", "last_90d")
        for k in _cls_keys
    )
    if tables and not _has_cls:
        st.error(
            "⚠️ Factor tables are loaded but **class-step keys are missing**. "
            "This means the deployed `reports/factor_analysis_tables.json` is "
            "out of date. Click **Regenerate** below (if available) or pull "
            "the latest commit and redeploy."
        )

    # ── Header: regenerate + metadata ───────────────────────
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        if mtime > 0:
            mt = hkt_from_ts(mtime)
            st.caption(f"Last generated: **{mt:%Y-%m-%d %H:%M} HKT** · source: "
                       f"`reports/factor_analysis_tables.json` · "
                       f"tables served from in-memory cache")
        else:
            st.warning("Factor tables not found. Click **Regenerate** to build them.")
    with c2:
        window = st.selectbox(
            "Window",
            options=["current_season_25_26", "last_90d", "all_time"],
            format_func=lambda s: {"current_season_25_26": "Current season 25/26",
                                   "last_90d": "Last 90 days",
                                   "all_time": "All-time"}[s],
            index=0, key="da_window",
        )
    with c3:
        # The Regenerate button only makes sense locally — on Streamlit Cloud
        # the file system is ephemeral and the xlsx source isn't deployed.
        can_regen = (os.environ.get("STREAMLIT_SERVER_HEADLESS") != "true"
                     or (BASE / "hkjc_results_updated.xlsx").exists())
        if st.button("Regenerate", width='stretch',
                     key="da_regen", disabled=not can_regen,
                     help=None if can_regen
                          else "Regeneration disabled on hosted deployment "
                               "— run factor_model_analysis.py locally and commit."):
            with st.spinner("Running factor_model_analysis.py …"):
                try:
                    import subprocess
                    r = subprocess.run(
                        [sys.executable, "factor_model_analysis.py"],
                        cwd=str(BASE), capture_output=True, text=True, timeout=600,
                    )
                    if r.returncode == 0:
                        st.success("Regenerated.")
                        # Clear only factor caches — leaves form/results caches alone.
                        _load_factor_tables_cached.clear()
                        _get_factor_df.clear()
                        _get_factor_summary_edges.clear()
                        st.rerun()
                    else:
                        st.error(f"Exit {r.returncode}")
                        st.code((r.stderr or r.stdout)[-2000:])
                except (OSError, subprocess.TimeoutExpired) as e:
                    st.error(str(e))

    if not tables:
        st.info("No factor tables available yet. If you're running locally, "
                "click **Regenerate** above or run "
                "`python factor_model_analysis.py` in a terminal.")
        return

    # ── Tabs ───────────────────────────────────────────
    tab_names = ["Summary", "Jockeys", "Trainers", "Jockey × Trainer",
                 "Pedigree", "Class Moves", "Form & Context",
                 "Draw / Dist / Going", "Benchmark ML",
                 "🎯 Signal Audit", "Methodology"]
    tabs = st.tabs(tab_names)

    def _style_df(df: pd.DataFrame):
        if df is None or df.empty:
            return df
        num_fmt = {}
        for col, fmt in (("Win_pct", "{:.1%}"), ("Plc_pct", "{:.1%}"),
                         ("Base_win", "{:.3f}"), ("IV", "{:.2f}"),
                         ("A_E", "{:.2f}"), ("ROI", "{:+.2%}"),
                         ("N", "{:.0f}"), ("Wins", "{:.0f}"),
                         ("Exp_mkt", "{:.1f}")):
            if col in df.columns:
                num_fmt[col] = fmt
        sty = df.style.format(num_fmt, na_rep="—")
        # background_gradient needs matplotlib; if absent, skip heatmap styling
        # entirely (it raises lazily during st.dataframe render, not here).
        if _HAS_MPL:
            for col, (vmin, vmax) in (("IV", (0.5, 3.0)),
                                      ("A_E", (0.7, 1.8)),
                                      ("ROI", (-0.4, 0.4))):
                if col in df.columns and df[col].notna().any():
                    try:
                        sty = sty.background_gradient(subset=[col], cmap="RdYlGn",
                                                      vmin=vmin, vmax=vmax)
                    except (ValueError, TypeError, ImportError):
                        pass
        return sty

    def _show_table(key: str, label: str, min_n_default: int = 30,
                    min_n_max: int = 500):
        df = _get_factor_df(window, key, mtime)
        if df.empty:
            st.caption(f"No data for {label} in this window "
                       f"(key `{key}` missing or empty).")
            return
        total_rows = len(df)
        cc1, cc2 = st.columns([1, 2])
        with cc1:
            min_n = st.slider(f"Min N ({label})",
                              min_value=10, max_value=min_n_max,
                              value=min(min_n_default, min_n_max),
                              step=5,
                              key=f"da_minn_{key}_{window}")
        if "N" in df.columns:
            df = df[df["N"] >= min_n]
        if "IV" in df.columns:
            df = df.sort_values("IV", ascending=False)
        with cc2:
            st.caption(f"{len(df)} of {total_rows} rows after min-N filter")
        if df.empty:
            st.info(f"No rows with N ≥ {min_n}. Lower the Min-N slider "
                    f"(table has {total_rows} rows in total).")
            return
        st.dataframe(_style_df(df), width='stretch', hide_index=True)

    # ── Summary tab ───────────────────────────────────
    with tabs[0]:
        st.markdown("#### What's in the factor model")
        st.markdown("""
- **IV** (Impact Value) = Win% ÷ field-size baseline. `>1` = over-performs its field share.
- **A/E** = Actual wins ÷ market-expected wins. `>1` = market underestimates (real edge); `≈1` = already priced in.
- **ROI** = flat `$1` win-bet return.
- Metrics are computed per bucket with `win_odds` available.
""")
        c1, c2, c3, c4 = st.columns(4)
        for i, (k, label) in enumerate([
            ("jockey", "Jockeys"), ("trainer", "Trainers"),
            ("sire", "Sires"), ("dam_sire", "Dam-sires"),
        ]):
            df = _get_factor_df(window, k, mtime)
            top_iv = (df["IV"].max() if (not df.empty and "IV" in df.columns
                                          and df["IV"].notna().any()) else None)
            [c1, c2, c3, c4][i].metric(
                label, f"{len(df)} rows",
                delta=f"Top IV {top_iv:.2f}" if top_iv is not None else None,
            )

        st.markdown("#### Top edges in this window (A/E ≥ 1.2, N ≥ 30)")
        cdf = _get_factor_summary_edges(window, mtime)
        if cdf.empty:
            st.caption("No rows met the A/E ≥ 1.2 threshold.")
        else:
            st.dataframe(
                cdf.style.format({"Win%": "{:.1%}", "IV": "{:.2f}",
                                  "A/E": "{:.2f}", "ROI": "{:+.2%}",
                                  "N": "{:.0f}"}),
                width='stretch', hide_index=True,
            )

    with tabs[1]:
        st.info("**What this shows:** each rider's historical Win%, IV and A/E "
                "over the selected window. IV > 1 means they win more often than "
                "their field share; A/E > 1 means the market consistently "
                "under-prices them. ROI is the flat-$1 win-bet return — a negative "
                "ROI can still be useful if the A/E is above 1 (edge exists but "
                "betting at tote price gives the edge back to the pool).")
        _show_table("jockey", "Jockey", min_n_default=30, min_n_max=500)

    with tabs[2]:
        st.info("**What this shows:** trainer strike-rate and market edge. "
                "Trainers rarely change mid-season so signals here are more "
                "stable than jockey signals. Look for yards with A/E > 1.15 and "
                "positive ROI — these are stables the tote systematically "
                "under-rates.")
        _show_table("trainer", "Trainer", min_n_default=30, min_n_max=500)

    with tabs[3]:
        st.info("**What this shows:** specific jockey-trainer partnerships. "
                "Hong Kong stables have strong preferences for certain riders, "
                "and those partnerships often significantly out-perform the "
                "tote's expectation. Pairs with A/E > 1.3 at N ≥ 15 are the "
                "sharpest book-able combinations.")
        _show_table("jockey_x_trainer", "Jockey × Trainer",
                    min_n_default=15, min_n_max=100)
        st.caption("Tip: look at A/E and ROI together to spot yards the market mis-prices.")

    with tabs[4]:
        st.info("**What this shows:** pedigree-level strike rates. Sire and "
                "dam-sire effects are small individually (the market prices "
                "name-brand sires efficiently), but sire × distance and sire × "
                "going buckets frequently reveal breeding bias the market "
                "ignores — e.g. a sprint-bred sire over-achieving at 1400m.")
        st.markdown("**Sire**")
        _show_table("sire", "Sire", min_n_default=25, min_n_max=200)
        st.markdown("**Dam sire**")
        _show_table("dam_sire", "Dam sire", min_n_default=25, min_n_max=200)
        st.markdown("**Sire × distance bucket**")
        _show_table("sire_x_dist_bucket", "Sire × distance", 20, 100)
        st.markdown("**Sire × going**")
        _show_table("sire_x_going", "Sire × going", 20, 100)

    # ── Class Moves tab (NEW) ────────────────────────────────
    with tabs[5]:
        st.info(
            "**What this shows:** how horses perform when they **step up** "
            "(promoted to a higher-grade race, e.g. Class 4 → Class 3), **step "
            "down** (demoted), or stay in the same class. Because trainers — "
            "unlike jockeys — stay with a horse long-term, this table isolates "
            "which yards are best at placing their horses in the *right* race. "
            "An A/E > 1.3 on step-up runners means the trainer is being "
            "rewarded for an under-priced class promotion; the market typically "
            "over-reacts to a class rise and under-bets these horses."
        )
        st.markdown("##### Baseline: what happens after a class move?")
        _show_table("class_step", "class_step", min_n_default=50, min_n_max=2000)

        st.markdown("##### Trainer × class move — who handles promotions best?")
        st.caption(
            "Trainers at the top of this table are consistently winning with "
            "horses stepping up a grade — a strong positive indicator when one "
            "of their runners is in today's card promoted from its last race."
        )
        _show_table("trainer_x_class_step", "trainer × class_step",
                    min_n_default=15, min_n_max=100)

        st.markdown("##### Trainer — horses stepping UP in class only")
        _show_table("trainer_class_up", "trainer (step-up only)",
                    min_n_default=10, min_n_max=50)

        st.markdown("##### Trainer — horses stepping DOWN in class only")
        _show_table("trainer_class_down", "trainer (step-down only)",
                    min_n_default=10, min_n_max=50)

        st.markdown("##### Follow-up race *after* a class step-up")
        st.caption(
            "Did the promotion stick? This table scores the race **after** a "
            "horse was stepped up. A trainer with a high NextPlc% is not just "
            "winning one race with a promoted horse — they're permanently "
            "improving the horse. This is the truest test of a class-placement "
            "skill."
        )
        df_fu = _get_factor_df(window, "trainer_class_up_followup", mtime)
        if df_fu.empty:
            st.caption("No follow-up data for this window.")
        else:
            for c in ("N", "NextWin_pct", "NextPlc_pct"):
                if c in df_fu.columns:
                    df_fu[c] = pd.to_numeric(df_fu[c], errors="coerce")
            df_fu = df_fu.sort_values("NextPlc_pct", ascending=False)
            fmt = {"NextWin_pct": "{:.1%}", "NextPlc_pct": "{:.1%}", "N": "{:.0f}"}
            st.dataframe(df_fu.style.format(fmt, na_rep="—"),
                         width='stretch', hide_index=True)

        st.markdown("##### Jockey × class move (supplementary)")
        st.caption(
            "Weaker signal than trainer × class (jockeys change between runs) "
            "but useful when the same rider keeps the mount through a promotion."
        )
        _show_table("jockey_x_class_step", "jockey × class_step",
                    min_n_default=15, min_n_max=100)

    with tabs[6]:
        st.info("**What this shows:** features that describe a horse's *state* "
                "coming into today — recent form (last 3 runs), freshness, "
                "rating change since last start, weight change, career "
                "experience, age, and gear switches. These are the "
                "short-horizon signals the market incorporates last, so shifts "
                "here frequently reveal under-bet runners.")
        st.markdown("**Rolling last-3 win rate**")
        _show_table("last3_win_bucket", "last3_win", 50, 2000)
        st.markdown("**Days off (freshness)**")
        _show_table("days_off_bucket", "days_off", 50, 5000)
        st.markdown("**Rating delta vs previous run**")
        _show_table("rating_delta_bucket", "rating_delta", 50, 5000)
        st.markdown("**Declared-weight change vs previous run**")
        _show_table("decl_wt_chg_bucket", "decl_wt_chg", 50, 3000)
        st.markdown("**Career runs (pre)**")
        _show_table("career_runs_pre_bucket", "career_runs", 50, 5000)
        st.markdown("**Age (effective)**")
        _show_table("age_eff", "age", 50, 3000)
        st.markdown("**Gear change**")
        _show_table("gear_change", "gear", 50, 5000)

    with tabs[7]:
        st.info("**What this shows:** the structural course / draw / distance "
                "biases in HKJC. Inside draws (1-3) historically over-perform "
                "field share, especially at Happy Valley; some trainers/jockeys "
                "also have distance sweet-spots. Use these as a context filter "
                "on top-picks — a good runner badly drawn is a downgrade; a "
                "mediocre runner perfectly drawn at its preferred distance is "
                "an upgrade.")
        st.markdown("**Draw bucket**")
        _show_table("draw_bucket", "draw", 50, 5000)
        st.markdown("**Draw number**")
        _show_table("draw_num_bucket", "draw_num", 50, 5000)
        st.markdown("**Draw × course × distance**")
        _show_table("draw_bucket_x_race_course_x_dist_bucket",
                    "draw×course×dist", 30, 500)
        st.markdown("**Distance × going**")
        _show_table("dist_bucket_x_going", "dist×going", 50, 2000)
        st.markdown("**Jockey × distance**")
        _show_table("jockey_x_dist_bucket", "jockey×distance", 20, 200)
        st.markdown("**Trainer × distance**")
        _show_table("trainer_x_dist_bucket", "trainer×distance", 20, 200)

    with tabs[8]:
        st.info("**What this shows:** an independent GradientBoosted classifier "
                "trained on every factor in this page (no time/relativity "
                "features). Its purpose is a **sanity check**: if the model's "
                "AUC approaches the market's AUC, our factor set captures most "
                "of the public information; the gap is the remaining market "
                "inefficiency. Use the feature-importance chart to prioritise "
                "which factors to trust most.")
        st.markdown("#### Benchmark Gradient-Boosted classifier")
        bench = tables.get("benchmark_model", {}) or {}
        if not bench or "error" in bench:
            st.warning(bench.get("error", "No benchmark output available."))
        else:
            bc1, bc2, bc3, bc4 = st.columns(4)
            bc1.metric("Train / Test", f'{bench.get("n_train","?")} / {bench.get("n_test","?")}')
            bc2.metric("Top-1 hit rate",
                       f'{100*_fnum(bench.get("top1_model"), 0):.1f}%',
                       delta=f'Market {100*_fnum(bench.get("top1_market"),0):.1f}%')
            bc3.metric("AUC",
                       f'{_fnum(bench.get("auc_model"),0):.3f}',
                       delta=f'Market {_fnum(bench.get("auc_market"),0):.3f}')
            bc4.metric("Log-loss",
                       f'{_fnum(bench.get("logloss_model"),0):.3f}',
                       delta=f'Market {_fnum(bench.get("logloss_market"),0):.3f}',
                       delta_color="inverse")

            st.markdown("**Feature importance (one-hot collapsed)**")
            feat = bench.get("feat_imp_top25", {}) or {}
            if feat:
                fdf = (pd.DataFrame(list(feat.items()),
                                    columns=["Feature", "Importance"])
                         .sort_values("Importance", ascending=True))
                st.bar_chart(fdf.set_index("Feature"))

    with tabs[9]:
        _signal_audit_tab(window, mtime)

    with tabs[10]:
        st.markdown("""
#### Methodology

**Pipeline**: [`factor_model_analysis.py`](factor_model_analysis.py) reads
`hkjc_results_updated.xlsx`, engineers rolling-form / days-off / rating-delta /
weight-change / market-implied probability features, then aggregates metrics
per bucket per time window.

**Windows produced**
- `all_time` — full history available
- `current_season_25_26` — from 2025-09-01
- `last_90d` — most-recent 90 days

**Feature engineering (no leakage — all shifts use previous runs only)**
- Rolling last-3 Win% / top-3% / avg finishing position
- Days since last run (freshness)
- Rating delta vs previous run
- Declared-weight change vs previous run
- Effective age (recovered from `horse_id` when missing)
- Market-implied fair probability (per-race overround removed)

**How to use on race day**
1. The **Overview** page surfaces signals automatically ("Factor Edges Today").
2. Tiers combine multiple independent factors:
   - 🟢 **Green** — 3+ positive signals, composite score ≥ 1.6
   - 🟡 **Amber** — composite score ≥ 0.9
3. A green runner at ≥ 4-1 market price is usually the best A/E candidate.

**Known limits**
- Dam-sire and sire categories are sparse for new bloodlines; small-N rows
  will look extreme — always check `N` before acting.
- Odds are closing HKJC tote (post-race), not live. Live odds integration is
  tracked separately.
- This model is independent of the time/relativity projection; blend via
  the Race Day page (ET ∩ SARR) for the strongest signal.
""")


# ══════════════════════════════════════════════════════════════════════════════
# Live Odds page
# ══════════════════════════════════════════════════════════════════════════════

def _load_live_odds_snapshots(date_compact: str, venue: str) -> list[dict]:
    """Load all JSON snapshots for a given meeting, sorted by scraped_at."""
    import json as _json
    d = BASE / "cache" / "live_odds" / date_compact
    if not d.exists():
        return []
    snaps = []
    for fp in sorted(d.glob(f"{venue}_R*.json")):
        try:
            snaps.append(_json.loads(fp.read_text(encoding="utf-8")))
        except Exception:
            pass
    return snaps


def _venue_to_code(venue_str: str) -> str | None:
    """Map dashboard meeting_venue ('Happy Valley'/'HV'/...) to two-letter code."""
    if not venue_str:
        return None
    v = str(venue_str).upper().strip()
    if "HAPPY VALLEY" in v or v == "HV":
        return "HV"
    if "SHA TIN" in v or v == "ST":
        return "ST"
    return None


def _compute_race_drift(date_compact: str, venue_code: str,
                        race_no: int) -> dict:
    """Compute per-horse Win-odds drift (%) for one race across all snapshots.

    Returns:
        {
          "snapshots": [...],         # ordered by scraped_at ascending
          "n_snaps":   int,
          "first_ts":  str | "",
          "last_ts":   str | "",
          "horses":    [{
              "no": int, "horse": str,
              "win_first": float|None, "win_last": float|None,
              "place_last": float|None, "dpct": float|None,
          }, ...]                     # ordered by Δ% ascending (steamers first)
        }
    """
    snaps_all = _load_live_odds_snapshots(date_compact, venue_code)
    rs = [s for s in snaps_all if str(s.get("race_no")) == str(race_no)]
    rs.sort(key=lambda s: s.get("scraped_at", ""))
    if not rs:
        return {"snapshots": [], "n_snaps": 0, "first_ts": "",
                "last_ts": "", "horses": []}
    earliest, latest = rs[0], rs[-1]
    first_by = {str(h.get("no")): h for h in earliest.get("odds") or []}
    horses = []
    for h in latest.get("odds") or []:
        no_s = str(h.get("no"))
        try:
            no_n = int(no_s)
        except (TypeError, ValueError):
            continue
        try:
            wf = float(first_by.get(no_s, {}).get("win"))
        except (TypeError, ValueError):
            wf = None
        try:
            wl = float(h.get("win"))
        except (TypeError, ValueError):
            wl = None
        try:
            pl = float(h.get("place"))
        except (TypeError, ValueError):
            pl = None
        d = None
        if wf is not None and wl is not None and wf > 0:
            d = round((wl - wf) / wf * 100, 1)
        horses.append({
            "no": no_n, "horse": str(h.get("horse") or ""),
            "win_first": wf, "win_last": wl, "place_last": pl, "dpct": d,
        })
    horses.sort(key=lambda x: (float("inf") if x["dpct"] is None else x["dpct"]))
    return {
        "snapshots": rs, "n_snaps": len(rs),
        "first_ts": earliest.get("scraped_at", ""),
        "last_ts": latest.get("scraped_at", ""),
        "horses": horses,
    }


def _market_implied_winpct(date_compact: str, venue_code: str | None,
                           race_no: int) -> dict:
    """Return {horse_no: market-implied win % } from the latest live-odds snapshot.

    Implied probabilities are de-overround (normalised so they sum to 100%),
    so the result is directly comparable to the model's Win%.
    Empty dict when no live odds are available.
    """
    if not (date_compact and venue_code):
        return {}
    try:
        drift = _compute_race_drift(date_compact, venue_code, race_no)
    except Exception:
        return {}
    invs = {}
    for h in drift.get("horses", []):
        wl = h.get("win_last")
        if wl and wl > 0:
            invs[h["no"]] = 1.0 / wl
    tot = sum(invs.values())
    if tot <= 0:
        return {}
    return {no: (inv / tot) * 100.0 for no, inv in invs.items()}


def _compute_pair_drift(date_compact: str, venue_code: str,
                        race_no: int, pool: str = "qin") -> dict:
    """Compute drift for Quinella (qin) or Quinella-Place (qpl) pair odds.

    Args:
        pool: 'qin' or 'qpl'.

    Returns a dict mirroring _compute_race_drift but keyed by horse pairs::

        {
          "pool":      "qin" | "qpl",
          "n_snaps":   int,
          "first_ts":  str | "",
          "last_ts":   str | "",
          "pairs":     [{
              "a": int, "b": int,
              "odds_first": float|None,
              "odds_last":  float|None,
              "dpct":       float|None,
          }, ...]  # sorted by dpct ascending (steamers first)
        }
    """
    key = "qin_odds" if pool == "qin" else "qpl_odds"
    snaps_all = _load_live_odds_snapshots(date_compact, venue_code)
    rs = [s for s in snaps_all if str(s.get("race_no")) == str(race_no)
          and isinstance(s.get(key), list) and s.get(key)]
    rs.sort(key=lambda s: s.get("scraped_at", ""))
    if not rs:
        return {"pool": pool, "n_snaps": 0, "first_ts": "",
                "last_ts": "", "pairs": []}
    earliest, latest = rs[0], rs[-1]

    def _flat(snap):
        out = {}
        for e in snap.get(key) or []:
            try:
                a, b = int(e.get("a")), int(e.get("b"))
            except (TypeError, ValueError):
                continue
            try:
                o = float(e.get("odds"))
            except (TypeError, ValueError):
                continue
            out[(min(a, b), max(a, b))] = o
        return out

    f_map = _flat(earliest)
    l_map = _flat(latest)
    pairs = []
    for ab, ol in l_map.items():
        of = f_map.get(ab)
        d = None
        if of is not None and ol is not None and of > 0:
            d = round((ol - of) / of * 100, 1)
        pairs.append({
            "a": ab[0], "b": ab[1],
            "odds_first": of, "odds_last": ol, "dpct": d,
        })
    pairs.sort(key=lambda x: (float("inf") if x["dpct"] is None else x["dpct"]))
    return {
        "pool": pool, "n_snaps": len(rs),
        "first_ts": earliest.get("scraped_at", ""),
        "last_ts": latest.get("scraped_at", ""),
        "pairs": pairs,
    }


def _render_pair_drift_block(date_compact: str, venue_code: str,
                             race_no: int, top_pick_nos: set[int] | None = None,
                             threshold: float = 25.0,
                             max_each: int = 5) -> None:
    """Compact UI block: side-by-side QIN & QPL steamer/drifter lists.
    No-op when there isn't enough data."""
    top_pick_nos = top_pick_nos or set()
    cols = st.columns(2)
    for col, pool, label in zip(cols, ("qin", "qpl"),
                                ("Quinella", "Quinella Place")):
        with col:
            drift = _compute_pair_drift(date_compact, venue_code, int(race_no),
                                        pool=pool)
            st.markdown(f"**{label} pair moves**")
            if drift["n_snaps"] < 2 or not any(p["dpct"] is not None
                                               for p in drift["pairs"]):
                st.caption(f"_Need \u22652 {label} snapshots._")
                continue
            steamers = [p for p in drift["pairs"]
                        if p["dpct"] is not None and p["dpct"] <= -threshold]
            drifters = [p for p in drift["pairs"]
                        if p["dpct"] is not None and p["dpct"] >= threshold]
            drifters.sort(key=lambda p: -(p["dpct"] or 0))

            def _pair_row(rows, bg):
                if not rows:
                    st.caption("_no significant moves_")
                    return
                for p in rows[:max_each]:
                    a, b = p["a"], p["b"]
                    star = " \u2b50" if (a in top_pick_nos or b in top_pick_nos) else ""
                    o_f = p["odds_first"]; o_l = p["odds_last"]; d = p["dpct"]
                    of_str = f"{o_f:.0f}" if o_f is not None else "\u2014"
                    ol_str = f"{o_l:.0f}" if o_l is not None else "\u2014"
                    d_str = f"{d:+.1f}%"
                    st.markdown(
                        f'<div style="background:{bg};padding:4px 8px;'
                        f'border-radius:5px;margin-bottom:3px;'
                        f'display:flex;justify-content:space-between;'
                        f'font-size:0.9em">'
                        f'<span><b>{a}\u2013{b}</b>{star}</span>'
                        f'<span style="font-family:monospace">'
                        f'{of_str} \u2192 {ol_str} <b>({d_str})</b>'
                        f'</span></div>',
                        unsafe_allow_html=True,
                    )

            st.markdown(
                f'<span style="color:#22c55e;font-weight:600;font-size:0.85em">'
                f'\u25bc Steamers (\u2264 {-threshold:+.0f}%)</span>',
                unsafe_allow_html=True,
            )
            _pair_row(steamers, "rgba(34,139,34,0.15)")
            st.markdown(
                f'<span style="color:#ef4444;font-weight:600;font-size:0.85em">'
                f'\u25b2 Drifters (\u2265 {threshold:+.0f}%)</span>',
                unsafe_allow_html=True,
            )
            _pair_row(drifters, "rgba(192,57,43,0.15)")


def _pick_alignment(picks: list[dict], drift: dict,
                    steamer_thr: float = -25.0,
                    drifter_thr: float = 25.0,
                    top_n: int = 3) -> dict:
    """Score the alignment between our model picks and market drift.

    Args:
        picks:        list of model picks (top-N first), each {horse_no, horse_name, ...}
        drift:        output of _compute_race_drift
        steamer_thr:  Δ% ≤ this counts as a steamer (negative)
        drifter_thr:  Δ% ≥ this counts as a drifter (positive)
        top_n:        size of model "top picks" cohort

    Returns:
        {
          "verdict":     "AGREE" | "DISAGREE" | "MIXED" | "NEUTRAL" | "NO_DATA"
          "score":       int  (positive = bullish, negative = bearish)
          "agree":       [(rank, no, horse, dpct), ...]   our top picks that steamed
          "disagree":    [(rank, no, horse, dpct), ...]   our top picks that drifted
          "outsiders":   [(no, horse, dpct), ...]         non-top picks that steamed
          "messages":    [str, ...]                       ready-to-render bullets
        }
    """
    horses = drift.get("horses") or []
    if not picks or not horses:
        return {"verdict": "NO_DATA", "score": 0, "agree": [], "disagree": [],
                "outsiders": [], "messages": []}
    by_no = {h["no"]: h for h in horses}
    top_nos: list[int] = []
    rank_lookup: dict[int, int] = {}
    for i, p in enumerate(picks[:top_n]):
        try:
            n = int(p.get("horse_no") or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            top_nos.append(n)
            rank_lookup[n] = i + 1
    agree, disagree = [], []
    for n in top_nos:
        h = by_no.get(n)
        if not h:
            continue
        d = h["dpct"]
        if d is None:
            continue
        rk = rank_lookup[n]
        if d <= steamer_thr:
            agree.append((rk, n, h["horse"], d))
        elif d >= drifter_thr:
            disagree.append((rk, n, h["horse"], d))
    outsiders = []
    for h in horses:
        if h["no"] in top_nos:
            continue
        d = h["dpct"]
        if d is None or d > steamer_thr:
            continue
        outsiders.append((h["no"], h["horse"], d))
    outsiders.sort(key=lambda t: t[2])
    score = len(agree) * 2 - len(disagree) * 2 - min(len(outsiders), 2)
    if not agree and not disagree and not outsiders:
        verdict = "NEUTRAL"
    elif agree and not disagree:
        verdict = "AGREE"
    elif disagree and not agree:
        verdict = "DISAGREE"
    else:
        verdict = "MIXED"
    msgs: list[str] = []
    for rk, n, name, d in agree:
        msgs.append(
            f"🟢 **AGREEMENT** — our #{rk} pick **#{n} {name}** is steaming "
            f"(**{d:+.1f}%**). Market backs the model.")
    for rk, n, name, d in disagree:
        msgs.append(
            f"🔴 **DISAGREEMENT** — our #{rk} pick **#{n} {name}** is drifting "
            f"(**{d:+.1f}%**). Market sees something we don't — re-check.")
    for n, name, d in outsiders[:2]:
        msgs.append(
            f"⚠️ **OUTSIDER STEAMING** — **#{n} {name}** is being backed "
            f"(**{d:+.1f}%**) but isn't in our top {top_n}. Consider QPL cover.")
    return {"verdict": verdict, "score": score,
            "agree": agree, "disagree": disagree,
            "outsiders": outsiders, "messages": msgs}


def _render_race_day_market_pulse(date_compact: str, venue_code: str,
                                  race_no: int, picks: list[dict]) -> None:
    """Render a compact Market Pulse panel for a single race within Race Day Insight.

    Shows per-race steamers and drifters from live odds snapshots, plus the
    alignment with our model top picks. No-op when no snapshots exist.
    """
    if not date_compact or not venue_code or not race_no:
        return
    drift = _compute_race_drift(date_compact, venue_code, int(race_no))
    if drift["n_snaps"] == 0:
        return
    horses = drift["horses"]
    if drift["n_snaps"] < 2 or not any(h["dpct"] is not None for h in horses):
        st.markdown("##### 📡 Market Pulse")
        st.caption(
            f"Only **{drift['n_snaps']}** snapshot for R{race_no} so far — "
            "drift signal needs ≥2 captures. Run the scraper again later "
            "(see Live Odds page) to build drift history."
        )
        return
    st.markdown("##### 📡 Market Pulse")
    cap_first = drift["first_ts"][11:19] if drift["first_ts"] else "?"
    cap_last = drift["last_ts"][11:19] if drift["last_ts"] else "?"
    st.caption(
        f"{drift['n_snaps']} snapshots · first **{cap_first}** → "
        f"latest **{cap_last}** · Δ% on Win odds vs first capture."
    )

    align = _pick_alignment(picks or [], drift)

    # Calibration: ±25% catches genuinely meaningful moves while ignoring
    # the wider overnight-odds settling that dominates the first 1–2 hrs
    # after market opens. Re-tighten if more granular signal is needed.
    steamer_thr, drifter_thr = -25.0, 25.0

    def _tile_row(rows: list[tuple], color: str, empty: str) -> None:
        if not rows:
            st.caption(f"_{empty}_")
            return
        for no, name, wf, wl, d, badge in rows:
            d_str = f"{d:+.1f}%" if d is not None else "—"
            wf_str = f"{wf:.1f}" if wf is not None else "—"
            wl_str = f"{wl:.1f}" if wl is not None else "—"
            st.markdown(
                f'<div style="background:{color};padding:6px 10px;'
                f'border-radius:6px;margin-bottom:4px;'
                f'display:flex;justify-content:space-between;align-items:center">'
                f'<span><b>#{no}</b> {name} {badge}</span>'
                f'<span style="font-family:monospace">'
                f'{wf_str} → {wl_str} <b>({d_str})</b></span></div>',
                unsafe_allow_html=True,
            )

    top_pick_nos = set()
    for i, p in enumerate(picks[:3] if picks else []):
        try:
            top_pick_nos.add(int(p.get("horse_no") or 0))
        except (TypeError, ValueError):
            pass

    steamers, drifters = [], []
    for h in horses:
        d = h["dpct"]
        if d is None:
            continue
        badge = ("🎯" if h["no"] in top_pick_nos else "")
        if d <= steamer_thr:
            steamers.append((h["no"], h["horse"], h["win_first"],
                             h["win_last"], d, badge))
        elif d >= drifter_thr:
            drifters.append((h["no"], h["horse"], h["win_first"],
                             h["win_last"], d, badge))
    drifters.sort(key=lambda t: -(t[4] or 0))

    cL, cR = st.columns(2)
    with cL:
        st.markdown(f"**🟢 Steamers (≤ {steamer_thr:+.0f}%)**")
        _tile_row(steamers[:5],
                  color="rgba(34,139,34,0.18)",
                  empty="No significant steamers yet.")
    with cR:
        st.markdown(f"**🔴 Drifters (≥ {drifter_thr:+.0f}%)**")
        _tile_row(drifters[:5],
                  color="rgba(192,57,43,0.18)",
                  empty="No significant drifters yet.")

    # Alignment verdict line
    verdict = align["verdict"]
    if verdict == "AGREE":
        st.success(
            f"**Verdict: 🟢 AGREEMENT** — market endorses our top pick(s). "
            "Strong confidence signal — full Kelly is reasonable."
        )
    elif verdict == "DISAGREE":
        st.error(
            f"**Verdict: 🔴 DISAGREEMENT** — market is fading our top pick(s). "
            "Cut stake or skip; double-check vet/draw/trainer-jockey notes."
        )
    elif verdict == "MIXED":
        st.warning(
            "**Verdict: 🟡 MIXED** — some agreement, some disagreement. "
            "Treat as lower-confidence; QPL cover may de-risk."
        )
    elif verdict == "NEUTRAL":
        st.info("**Verdict: ⚪ NEUTRAL** — no significant moves either way.")
    for m in align["messages"]:
        st.markdown(f"- {m}")
    if not align["messages"] and verdict == "NEUTRAL":
        st.caption(
            "Move thresholds: a horse must move ≥25% on Win odds for "
            "either column to populate (filters out the wider overnight "
            "settling phase)."
        )

    # ── Quinella / Quinella-Place pair drift ────────────────────────────
    with st.expander("Quinella / Quinella-Place pair moves", expanded=False):
        st.caption(
            "Pair-odds drift on QIN & QPL pools (≥25% move vs first "
            "snapshot). Highlights pair combinations being smashed in or "
            "drifted. A ⭐ indicates one leg is a model top-3 pick — "
            "good QPL cover candidates when the pair is steaming."
        )
        _render_pair_drift_block(
            date_compact, venue_code, int(race_no),
            top_pick_nos=top_pick_nos,
        )


# ── Value Lens (cross-sectional probability comparison) ─────────────────
# DIFFERENT from Market Pulse:
#   • Market Pulse  = TIME (Δ% odds across snapshots, steamers/drifters)
#   • Value Lens    = NOW (p_model vs p_market right now, edge & Kelly)
# Both can run in the same cockpit; they answer different questions.
@st.cache_data(ttl=120, show_spinner=False)
def _value_lens_table(date_compact: str, venue_code: str, race_no: int,
                      picks_key: str):
    """Cached wrapper around market_loader.compute_edge_table.

    `picks_key` is a deterministic JSON string of (horse_no, win_prob)
    so the cache invalidates when the model output changes.
    """
    try:
        import json as _json
        from market_loader import compute_edge_table  # local import
        picks = _json.loads(picks_key) if picks_key else []
        return compute_edge_table(date_compact, venue_code,
                                  int(race_no), picks)
    except Exception as e:
        return {"meta": {"source": "error", "error": str(e)}, "rows": []}


def _render_race_day_value_lens(date_compact: str, venue_code: str,
                                race_no: int, picks: list[dict]) -> None:
    """Static probability comparison: model vs market, right now.

    Uses the latest live-odds snapshot if available, otherwise final SP
    (post-race) as fallback for retrospective review. No-op when neither
    is available or the picks don't contain win_prob.
    """
    if not date_compact or not venue_code or not race_no or not picks:
        return
    # Quick gate — if no picks have win_prob, this is a SARR-only race,
    # there's nothing to compare against.
    if not any(p.get("win_prob") is not None for p in picks):
        return
    import json as _json
    picks_key = _json.dumps(
        sorted(
            [(int(p.get("horse_no") or 0), float(p.get("win_prob") or 0))
             for p in picks if p.get("horse_no") is not None]
        )
    )
    try:
        out = _value_lens_table(date_compact, venue_code,
                                int(race_no), picks_key)
    except Exception as e:
        st.caption(f"_Value Lens unavailable: {e}_")
        return
    meta = out.get("meta", {})
    rows = out.get("rows", [])
    if not rows or meta.get("source") in ("none", "error"):
        return

    st.markdown("##### 🎯 Value Lens")
    src = meta.get("source", "?")
    src_label = {"live": "live odds", "sp": "final SP (post-race)"
                 }.get(src, src)
    ts = meta.get("scraped_at", "")
    ts_short = ts[11:19] if len(ts) >= 19 else ts
    st.caption(
        f"Snapshot: **{src_label}** · "
        f"{ts_short or '—'} · "
        f"`p_market` from implied odds (basic) · "
        f"`p_model` from v4.4 win-prob · "
        f"`edge = p_model − p_market`"
    )

    # Quadrant counts
    quads = {"A": 0, "B": 0, "C": 0, "D": 0}
    for r in rows:
        quads[r.get("quadrant", "D")] = quads.get(r.get("quadrant", "D"), 0) + 1
    qA, qB, qC, qD = st.columns(4)
    qA.metric("A · Consensus", quads["A"],
              help="Both model and market rate ≥15% — strongest signal.")
    qB.metric("B · Market-only", quads["B"],
              help="Market loves it, model doesn't → likely overbet.")
    qC.metric("C · Model-only", quads["C"],
              help="Model loves it, market doesn't → potential overlay.")
    qD.metric("D · Ignore", quads["D"],
              help="Both rate it low — skip.")

    # Top edges table
    top = [r for r in rows if r["edge"] > 0][:6]
    if top:
        st.markdown("**Top positive edges**")
        df = pd.DataFrame([
            {
                "#": r["horse_no"],
                "Horse": r["horse"],
                "Odds": f"{r['win_odds']:.1f}",
                "p_market": f"{r['p_market']*100:5.1f}%",
                "p_model":  f"{r['p_model']*100:5.1f}%",
                "Edge":     f"{r['edge']*100:+5.1f}pp",
                "Kelly":    f"{r['kelly']*100:.1f}%",
                "Quad":     r["quadrant"],
            }
            for r in top
        ])
        st.dataframe(df, hide_index=True, width='stretch')
    else:
        st.caption(
            "_No positive-edge runners — model agrees with the market._"
        )

    # Bottom edges (overbet by market) — useful as a fade list
    bot = sorted([r for r in rows if r["edge"] < 0],
                 key=lambda r: r["edge"])[:3]
    if bot:
        with st.expander(f"Market overbets vs model "
                         f"({len(bot)} shown)", expanded=False):
            df2 = pd.DataFrame([
                {
                    "#": r["horse_no"],
                    "Horse": r["horse"],
                    "Odds": f"{r['win_odds']:.1f}",
                    "p_market": f"{r['p_market']*100:5.1f}%",
                    "p_model":  f"{r['p_model']*100:5.1f}%",
                    "Edge":     f"{r['edge']*100:+5.1f}pp",
                    "Quad":     r["quadrant"],
                }
                for r in bot
            ])
            st.dataframe(df2, hide_index=True, width='stretch')

    # Backtest reference
    st.caption(
        "Reference (Apr 2026, n=58): rank-1 model picks **with** "
        "positive edge returned **+23.3% ROI / 22.4% strike**. "
        "Rank-1 picks with negative edge: **−61% ROI**. "
        "See `reports/COMBINED_EDGE_BACKTEST.md`."
    )


def _compute_meeting_alerts(date_compact: str, venue_code: str,
                            races: list[dict],
                            steamer_thr: float = -25.0,
                            drifter_thr: float = 25.0,
                            big_steamer_thr: float = -40.0,
                            big_drifter_thr: float = 40.0) -> list[dict]:
    """Aggregate alerts across all races in a meeting.

    Severity tiers:
      WARN   — top-3 model pick drifted ≥+40%
      REVIEW — outsider (rank > 3) steamed ≤-40%, or top-1 drifted ≥+25%
      INFO   — other notable moves

    Each alert: {race, severity, kind, no, horse, dpct, msg}.
    """
    alerts: list[dict] = []
    for r in races or []:
        rn = r.get("race_number")
        picks = r.get("picks") or []
        if not rn:
            continue
        drift = _compute_race_drift(date_compact, venue_code, int(rn))
        if drift["n_snaps"] < 2:
            continue
        align = _pick_alignment(
            picks, drift, steamer_thr=steamer_thr,
            drifter_thr=drifter_thr, top_n=3)
        # disagreements
        for rk, no, name, d in align["disagree"]:
            sev = "WARN" if d >= big_drifter_thr else "REVIEW"
            alerts.append({
                "race": int(rn), "severity": sev, "kind": "TOP_PICK_DRIFT",
                "no": no, "horse": name, "rank": rk, "dpct": d,
                "msg": f"R{rn}: model #{rk} **{name}** (#{no}) drifting {d:+.1f}%",
            })
        for no, name, d in align["outsiders"]:
            sev = "REVIEW" if d <= big_steamer_thr else "INFO"
            alerts.append({
                "race": int(rn), "severity": sev, "kind": "OUTSIDER_STEAM",
                "no": no, "horse": name, "rank": None, "dpct": d,
                "msg": f"R{rn}: outsider **{name}** (#{no}) steaming {d:+.1f}%",
            })
        for rk, no, name, d in align["agree"]:
            if d <= big_steamer_thr:
                alerts.append({
                    "race": int(rn), "severity": "INFO", "kind": "TOP_PICK_STEAM",
                    "no": no, "horse": name, "rank": rk, "dpct": d,
                    "msg": f"R{rn}: model #{rk} **{name}** (#{no}) steaming {d:+.1f}%",
                })
    sev_order = {"WARN": 0, "REVIEW": 1, "INFO": 2}
    alerts.sort(key=lambda a: (sev_order.get(a["severity"], 9),
                               a["race"], -abs(a.get("dpct") or 0)))
    return alerts


def _run_live_odds_scraper(date_iso: str, venue: str, races: str,
                           pools: str = "wp,qin,qpl") -> tuple[int, str]:
    """Run scrape_hkjc_live_odds.py as a subprocess. Returns (returncode, log)."""
    import subprocess
    script = BASE / "scrape_hkjc_live_odds.py"
    if not script.exists():
        return 1, f"scrape_hkjc_live_odds.py not found at {script}"
    cmd = [sys.executable, str(script),
           "--date", date_iso, "--venue", venue, "--races", races,
           "--pools", pools]
    ok_pw, pw_msg = _ensure_playwright_runtime(sys.executable)
    if not ok_pw:
        return 1, pw_msg
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=300, cwd=str(BASE))
        log = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
        if pw_msg:
            log = f"[playwright] {pw_msg}\n" + log
        return r.returncode, log
    except subprocess.TimeoutExpired:
        return 124, "Scraper timed out after 5 min."
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def page_live_odds():
    """Live odds snapshots scraped from bet.hkjc.com.

    Captures Win/Place + Quinella + Quinella-Place pair-odds matrices.
    Each race may have N snapshots taken across the day (overnight, morning,
    near-post). The page shows:

    * Top market movers across the meeting (biggest Win-odds drops/rises)
    * Per-race table with green/red highlighting on Δ Win %
    * Win-odds time-slider to inspect any captured snapshot
    * QIN and QPL matrices with the same colour-coded drift
    * Meeting summary (favourite + steamer per race, no horse names)
    """
    import json as _json
    import pandas as _pd
    import datetime as _dt
    from collections import defaultdict

    st.markdown("## 💹 Live Odds")
    st.info(
        "Win / Place / Quinella / Quinella-Place odds scraped from "
        "**bet.hkjc.com**. Each race may carry multiple snapshots. "
        "**🟢 Odds dropped → money flowing in (market support).**  "
        "**🔴 Odds drifted out → market losing confidence.**  "
        "Use as a sanity-check on the model — the market is not always smart, "
        "but persistent ≥20% drops on horses we *don't* rank are worth a look, "
        "and ≥20% drifts on our top picks deserve a re-examination."
    )

    # ── Scraper controls ───────────────────────────────────────────────
    # Venue caps: HV = max 9 races, ST = max 11 races. Used to set the
    # default race-range and to validate user input before running.
    _VENUE_MAX = {"HV": 9, "ST": 11}

    def _parse_race_list(spec: str) -> list[int]:
        out: list[int] = []
        for part in (spec or "").split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                try:
                    a, b = part.split("-", 1)
                    out.extend(range(int(a), int(b) + 1))
                except ValueError:
                    return []
            else:
                try:
                    out.append(int(part))
                except ValueError:
                    return []
        return sorted(set(out))

    with st.expander("🔄 Run scraper now", expanded=False):
        st.caption(
            "Fetches a fresh snapshot from bet.hkjc.com (WP + QIN + QPL "
            "from the public /wpq/ page). Run several times during the day "
            "(overnight, morning, ~1 h before post) to build drift history. "
            "**Happy Valley** runs up to 9 races, **Sha Tin** up to 11."
        )
        sc1, sc2, sc3, sc4, sc5 = st.columns([1.2, 0.8, 1, 1.2, 1])
        with sc1:
            scr_date = st.date_input("Meeting date", value=_dt.date.today(),
                                     key="liveodds_scr_date")
        with sc2:
            scr_venue = st.selectbox("Venue", ["HV", "ST"], index=0,
                                     key="liveodds_scr_venue")
        # Default race range follows venue cap; updates on venue change
        # because the selectbox key triggers a rerun.
        _default_range = f"1-{_VENUE_MAX.get(scr_venue, 11)}"
        if st.session_state.get("_liveodds_last_venue") != scr_venue:
            st.session_state["liveodds_scr_races"] = _default_range
            st.session_state["_liveodds_last_venue"] = scr_venue
        with sc3:
            scr_races = st.text_input(
                "Races",
                key="liveodds_scr_races",
                help=f"e.g. 1-{_VENUE_MAX[scr_venue]} or 1,2,3",
            )
        with sc4:
            scr_pools = st.multiselect("Pools", ["wp", "qin", "qpl"],
                                       default=["wp", "qin", "qpl"],
                                       key="liveodds_scr_pools")
        with sc5:
            st.write(""); st.write("")
            run_btn = st.button("▶ Run scraper", type="primary",
                                width='stretch',
                                key="liveodds_run_btn")
        if run_btn:
            req_races = _parse_race_list(scr_races)
            cap = _VENUE_MAX.get(scr_venue, 11)
            invalid = [r for r in req_races if r < 1 or r > cap]
            if not req_races:
                st.error("Could not parse race list. Use e.g. `1-9` or `1,2,3`.")
                return
            if invalid:
                kept = [r for r in req_races if 1 <= r <= cap]
                st.warning(
                    f"⚠ {scr_venue} only runs up to {cap} races — "
                    f"dropping invalid: {invalid}. Scraping {kept}."
                )
                req_races = kept
                if not req_races:
                    st.error("No valid races left after capping.")
                    return
            races_arg = ",".join(str(r) for r in req_races)
            # Pre-count snapshots so we can show how many NEW files appeared
            _ymd = scr_date.isoformat().replace("-", "")
            _snap_dir = BASE / "cache" / "live_odds" / _ymd
            _before = (set(p.name for p in _snap_dir.glob(f"{scr_venue}_R*.json"))
                       if _snap_dir.exists() else set())
            with st.spinner(
                f"Scraping {scr_venue} {scr_date} races {races_arg}…"
            ):
                rc, log = _run_live_odds_scraper(
                    scr_date.isoformat(), scr_venue, races_arg,
                    pools=",".join(scr_pools or ["wp"]))
            _after = (set(p.name for p in _snap_dir.glob(f"{scr_venue}_R*.json"))
                      if _snap_dir.exists() else set())
            n_new = len(_after - _before)
            if rc == 0 and n_new > 0:
                st.success(
                    f"✓ Scrape complete — **{n_new}** new snapshot(s) written."
                )
                # On Streamlit Cloud, immediately push snapshots to GitHub so
                # they survive the next redeploy/reboot. Without this, only
                # the snapshots committed at deploy time would persist.
                if _is_streamlit_cloud():
                    _new_files = sorted(_after - _before)
                    with st.spinner("Persisting snapshots to GitHub…"):
                        pushed, _missing, errs = _gh_persist_live_odds_snapshots(
                            scr_date.isoformat(), scr_venue, _new_files)
                    if pushed:
                        st.info(f"☁ Synced {pushed} snapshot file(s) to GitHub "
                                f"(survives Cloud reboot).")
                    if errs:
                        with st.expander(f"⚠ {len(errs)} sync error(s)"):
                            for e in errs[:20]:
                                st.code(str(e))
            elif rc == 0:
                st.warning(
                    "Scraper exited cleanly but **no new snapshot files** "
                    "appeared. Check the log below — typical causes: HKJC "
                    "page didn't render odds (too early in the day), or "
                    "Playwright Chromium failed to launch on Streamlit Cloud."
                )
            else:
                st.error(f"Scraper exited {rc}")
            if log.strip():
                with st.expander("Scraper log", expanded=(rc != 0 or n_new == 0)):
                    st.code(log[-4000:])
            if n_new > 0:
                st.rerun()

    root = BASE / "cache" / "live_odds"
    if not root.exists() or not any(root.iterdir()):
        st.warning(
            "No snapshots yet. Use **Run scraper now** above, or run locally:\n\n"
            "`python scrape_hkjc_live_odds.py --date YYYY-MM-DD --venue HV`"
        )
        return

    # Discover available meetings
    meetings = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        for fp in d.glob("*.json"):
            parts = fp.stem.split("_")
            if len(parts) >= 2:
                meetings.append((d.name, parts[0]))
                break
    meetings = sorted(set(meetings), reverse=True)
    if not meetings:
        st.warning("No valid snapshots found in `cache/live_odds/`.")
        return

    labels = [f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]} · {v}" for ymd, v in meetings]
    pick = st.selectbox("Meeting", labels, index=0, key="liveodds_meeting")
    ymd, venue = meetings[labels.index(pick)]

    snaps = _load_live_odds_snapshots(ymd, venue)
    if not snaps:
        st.warning("No snapshots for this meeting.")
        return

    # Group snapshots by race
    by_race: dict[int, list[dict]] = defaultdict(list)
    for s in snaps:
        try:
            by_race[int(s["race_no"])].append(s)
        except Exception:
            pass
    for rn in by_race:
        by_race[rn].sort(key=lambda s: s.get("scraped_at", ""))
    races = sorted(by_race.keys())

    # All distinct snapshot timestamps across the meeting (used by slider)
    all_ts = sorted({s.get("scraped_at", "") for s in snaps if s.get("scraped_at")})

    st.caption(
        f"**{len(races)} races** · {sum(len(v) for v in by_race.values())} snapshots · "
        f"{len(all_ts)} distinct capture time(s)"
    )

    # ── Highlight thresholds ───────────────────────────────────────────
    col_thr1, col_thr2 = st.columns(2)
    with col_thr1:
        green_thr = st.slider(
            "🟢 Significant Win-odds drop (≤)", -50.0, -5.0, -20.0, step=1.0,
            help="Δ ≤ this percentage is considered a 'steamer' (money in).",
            key="liveodds_green_thr",
        )
    with col_thr2:
        red_thr = st.slider(
            "🔴 Significant Win-odds drift (≥)", 5.0, 50.0, 20.0, step=1.0,
            help="Δ ≥ this percentage is considered a 'drifter' (market cooling).",
            key="liveodds_red_thr",
        )

    def _fnum(v):
        try: return float(v)
        except (TypeError, ValueError): return None

    def _delta_pct(first, last):
        if first is None or last is None or first <= 0:
            return None
        return round((last - first) / first * 100, 1)

    def _delta_color(d):
        """Background colour for a Δ% cell."""
        if d is None: return ""
        if d <= green_thr: return "background-color: #1b7837; color: white;"  # strong green
        if d <= -5: return "background-color: #b7e3b7; color: black;"          # mild green
        if d >= red_thr: return "background-color: #c0392b; color: white;"     # strong red
        if d >= 5: return "background-color: #f5cbcb; color: black;"           # mild red
        return ""

    # ════════════════════════════════════════════════════════════════
    # 1) MEETING SUMMARY TABLE — one row per race, with horse names
    # ════════════════════════════════════════════════════════════════
    st.markdown("### 📋 Meeting summary")
    summary_rows = []
    for rn in races:
        rows = by_race[rn]
        latest = rows[-1]
        earliest = rows[0]
        first_by_no = {h["no"]: h for h in earliest.get("odds", [])}
        last_odds = latest.get("odds", []) or []
        name_by_no = {h["no"]: h.get("horse", "") for h in last_odds}
        # Favourite = lowest Win at latest snapshot
        fav = None
        for h in last_odds:
            wn = _fnum(h.get("win"))
            if wn is None: continue
            if fav is None or wn < fav[1]: fav = (h["no"], wn, h.get("horse", ""))
        # Biggest steamer & drifter on Win
        big_steamer = None  # (no, Δ%, name)
        big_drifter = None
        for h in last_odds:
            no = h["no"]
            f = _fnum(first_by_no.get(no, {}).get("win"))
            l = _fnum(h.get("win"))
            d = _delta_pct(f, l)
            if d is None: continue
            if big_steamer is None or d < big_steamer[1]:
                big_steamer = (no, d, h.get("horse", ""))
            if big_drifter is None or d > big_drifter[1]:
                big_drifter = (no, d, h.get("horse", ""))

        def _no_name(tup):
            if not tup:
                return None
            n = int(tup[0]) if str(tup[0]).isdigit() else tup[0]
            nm = name_by_no.get(tup[0]) or (tup[2] if len(tup) > 2 else "")
            return f"#{n} {nm}".strip()

        summary_rows.append({
            "Race": rn,
            "Runners": latest.get("n_runners", len(last_odds)),
            "Snapshots": len(rows),
            "Favourite": _no_name(fav),
            "Fav Win": fav[1] if fav else None,
            "Top steamer": _no_name(big_steamer),
            "Steamer Δ%": big_steamer[1] if big_steamer else None,
            "Top drifter": _no_name(big_drifter),
            "Drifter Δ%": big_drifter[1] if big_drifter else None,
            "Latest update": latest.get("last_update", "").replace("Last Update:", "").strip(),
        })
    df_sum = _pd.DataFrame(summary_rows)
    sty_sum = (df_sum.style
               .map(_delta_color, subset=["Steamer Δ%", "Drifter Δ%"])
               .format({
                   "Fav Win": "{:.1f}",
                   "Steamer Δ%": "{:+.1f}%",
                   "Drifter Δ%": "{:+.1f}%",
               }, na_rep="—"))
    st.dataframe(sty_sum, hide_index=True, width='stretch')

    # ════════════════════════════════════════════════════════════════
    # 2) TIME SLIDER — Win odds across races at a chosen capture time
    # ════════════════════════════════════════════════════════════════
    if len(all_ts) >= 2:
        st.markdown("### 🕒 Win-odds across the meeting (time slider)")
        st.caption(
            "Pick a snapshot time. The grid shows every race × horse Win-odds "
            "at that moment, with Δ% vs the earliest snapshot for the same race "
            "colour-coded."
        )
        ts_labels = [t[11:19] + "  (" + t[:10] + ")" for t in all_ts]
        idx = st.select_slider(
            "Capture time", options=list(range(len(all_ts))),
            value=len(all_ts) - 1,
            format_func=lambda i: ts_labels[i],
            key="liveodds_ts_slider",
        )
        target_ts = all_ts[idx]

        # Build wide grid: rows = races, cols = horse numbers (1..max)
        max_no = max((int(h["no"]) for s in snaps for h in s.get("odds", [])
                      if str(h.get("no", "")).isdigit()), default=0)

        # Pick snapshot for each race closest to (≤) target_ts
        chosen = {}
        first = {}
        for rn in races:
            rows = by_race[rn]
            # closest snapshot at or before target_ts (fallback: nearest)
            cand = [r for r in rows if r.get("scraped_at", "") <= target_ts]
            if not cand: cand = [min(rows, key=lambda r: r.get("scraped_at", ""))]
            chosen[rn] = cand[-1]
            first[rn] = rows[0]

        grid_odds = []
        grid_drift = []
        for rn in races:
            ld = chosen[rn]
            fd = first[rn]
            l_by = {str(h["no"]): _fnum(h.get("win")) for h in ld.get("odds", [])}
            f_by = {str(h["no"]): _fnum(h.get("win")) for h in fd.get("odds", [])}
            row_o = {"Race": rn}
            row_d = {"Race": rn}
            for n in range(1, max_no + 1):
                row_o[str(n)] = l_by.get(str(n))
                row_d[str(n)] = _delta_pct(f_by.get(str(n)), l_by.get(str(n)))
            grid_odds.append(row_o)
            grid_drift.append(row_d)
        df_o = _pd.DataFrame(grid_odds)
        df_d = _pd.DataFrame(grid_drift)

        # Colour the odds grid using the matching drift grid
        def _style_odds(_):
            return df_d.drop(columns=["Race"]).map(_delta_color)
        sty_o = (df_o.style
                 .apply(lambda _: df_d.drop(columns=["Race"]).map(_delta_color)
                        .reindex(columns=[c for c in df_o.columns if c != "Race"])
                        .pipe(lambda x: x.assign(**{"Race": ""})[df_o.columns.tolist()])
                        if False else None, axis=None))
        # simpler: build matching style df via apply on whole frame
        def _style_full(df):
            sty = _pd.DataFrame("", index=df.index, columns=df.columns)
            for col in df.columns:
                if col == "Race": continue
                sty[col] = df_d[col].map(_delta_color)
            return sty
        sty_o = (df_o.style.apply(_style_full, axis=None)
                 .format({c: "{:.1f}" for c in df_o.columns if c != "Race"},
                         na_rep="—"))
        st.dataframe(sty_o, hide_index=True, width='stretch')
        st.caption(f"Snapshot time shown: **{target_ts}** · "
                   f"green = Win odds dropped vs first snapshot, red = drifted.")

    # ════════════════════════════════════════════════════════════════
    # 3) PER-RACE PANELS — WP table + QIN / QPL matrices, with drift
    # ════════════════════════════════════════════════════════════════
    st.markdown("### 🏇 Per-race panels")

    # Race button row — replaces infinite scroll with per-race selection
    sel_key = f"liveodds_sel_race_{ymd}_{venue}"
    if sel_key not in st.session_state or st.session_state[sel_key] not in races:
        st.session_state[sel_key] = races[0]
    btn_cols = st.columns(len(races))
    for i, rn in enumerate(races):
        is_active = st.session_state[sel_key] == rn
        label = f"▶ R{rn}" if is_active else f"R{rn}"
        if btn_cols[i].button(
            label, key=f"liveodds_btn_r{rn}", width='stretch',
            type="primary" if is_active else "secondary",
        ):
            st.session_state[sel_key] = rn
            st.rerun()
    sel_rn = st.session_state[sel_key]

    for rn in [sel_rn]:
        rows = by_race[rn]
        latest = rows[-1]
        earliest = rows[0]
        first_by_no = {h["no"]: h for h in earliest.get("odds", [])}

        hdr_bits = [f"**Race {rn}**"]
        if latest.get("race_info"):
            hdr_bits.append(latest["race_info"])
        st.markdown("#### " + " · ".join(hdr_bits))
        meta_bits = [f"{len(rows)} snapshot(s)"]
        if latest.get("last_update"):
            meta_bits.append(latest["last_update"])
        st.caption(" · ".join(meta_bits))

        # Per-snapshot picker (default: latest)
        snap_idx = len(rows) - 1
        if len(rows) >= 2:
            snap_idx = st.select_slider(
                f"Snapshot for R{rn}", options=list(range(len(rows))),
                value=len(rows) - 1,
                format_func=lambda i, _rs=rows: _rs[i].get("scraped_at", "?")[11:19],
                key=f"liveodds_snap_r{rn}",
            )
        sel = rows[snap_idx]

        # ─ WP table ──────────────────────────────────────────────────
        sel_by_no = {h["no"]: h for h in sel.get("odds", [])}
        table = []
        for h in sel.get("odds", []):
            no = h["no"]
            try: no_n = int(no)
            except (TypeError, ValueError): no_n = None
            w_first = _fnum(first_by_no.get(no, {}).get("win"))
            w_sel = _fnum(h.get("win"))
            p_sel = _fnum(h.get("place"))
            table.append({
                "No": no_n, "Horse": h.get("horse", ""),
                "Win (first)": w_first, "Win": w_sel, "Place": p_sel,
                "Δ Win %": _delta_pct(w_first, w_sel),
            })
        if table:
            df_wp = _pd.DataFrame(table).sort_values(
                "Win", na_position="last", kind="mergesort").reset_index(drop=True)
            sty_wp = (df_wp.style
                      .map(_delta_color, subset=["Δ Win %"])
                      .format({"Win (first)": "{:.1f}", "Win": "{:.1f}",
                               "Place": "{:.1f}", "Δ Win %": "{:+.1f}%"},
                              na_rep="—"))
            st.dataframe(sty_wp, hide_index=True, width='stretch',
                         height=min(420, 38 + 35 * len(df_wp)))

        # ─ Notable Quinella / Quinella-Place pair moves ──────────────
        if len(rows) >= 2:
            with st.expander(
                f"📈 Notable pair moves (QIN / QPL) — R{rn}",
                expanded=False,
            ):
                _render_pair_drift_block(
                    ymd, venue, int(rn),
                    top_pick_nos=set(),
                    threshold=float(red_thr),
                )

        # ─ QIN / QPL matrices ────────────────────────────────────────
        def _build_pair_matrix(pool_key: str):
            """Render a triangular pair-odds matrix with drift highlighting."""
            sel_pairs = sel.get(pool_key) or []
            first_pairs = earliest.get(pool_key) or []
            if not sel_pairs:
                return None, None
            sel_map = {(int(p["a"]), int(p["b"])): _fnum(p["odds"]) for p in sel_pairs}
            first_map = {(int(p["a"]), int(p["b"])): _fnum(p["odds"]) for p in first_pairs}
            nos = sorted({n for pair in sel_map for n in pair})
            mat = _pd.DataFrame("", index=nos, columns=[str(n) for n in nos])
            drift = _pd.DataFrame(None, index=nos, columns=[str(n) for n in nos],
                                  dtype="float")
            for (a, b), v in sel_map.items():
                if v is None: continue
                f = first_map.get((a, b))
                d = _delta_pct(f, v)
                # Place upper-triangle entries: row=lower, col=higher
                mat.at[a, str(b)] = f"{v:.1f}" if v < 100 else f"{v:.0f}"
                if d is not None:
                    drift.at[a, str(b)] = d
            mat.index.name = pool_key.upper()
            return mat, drift

        for pool_label, pool_key in [("Quinella (QIN)", "qin_odds"),
                                     ("Quinella Place (QPL)", "qpl_odds")]:
            mat, drift = _build_pair_matrix(pool_key)
            if mat is None or mat.empty:
                continue
            with st.expander(f"📊 {pool_label} matrix ({len(sel.get(pool_key, []))} pairs)",
                             expanded=False):
                # Style cells using drift-coloring
                def _style_pair(df):
                    out = _pd.DataFrame("", index=df.index, columns=df.columns)
                    for r in df.index:
                        for c in df.columns:
                            d = drift.at[r, c] if c in drift.columns and r in drift.index else None
                            try:
                                if _pd.notna(d): out.at[r, c] = _delta_color(float(d))
                            except Exception:
                                pass
                    return out
                sty = mat.style.apply(_style_pair, axis=None)
                st.dataframe(sty, width='stretch')
                st.caption(
                    f"Cells coloured by Δ% vs earliest snapshot. "
                    f"Pairs in the upper triangle (row #, col #). "
                    f"Snapshot: {sel.get('scraped_at', '?')[11:19]}."
                )
        st.markdown("")

    # ════════════════════════════════════════════════════════════════
    # 4) MARKET-MOVERS BOARD — biggest steamers / drifters across meeting
    # ════════════════════════════════════════════════════════════════
    movers = []
    for rn in races:
        rows = by_race[rn]
        if len(rows) < 2: continue
        first = {h["no"]: h for h in rows[0].get("odds", [])}
        last  = {h["no"]: h for h in rows[-1].get("odds", [])}
        for no, h in last.items():
            fw = _fnum(first.get(no, {}).get("win"))
            lw = _fnum(h.get("win"))
            d = _delta_pct(fw, lw)
            if d is None: continue
            try: no_n = int(no)
            except (TypeError, ValueError): no_n = None
            movers.append({
                "Race": rn, "No": no_n, "Horse": h.get("horse", ""),
                "First Win": fw, "Latest Win": lw, "Δ%": d,
            })
    if movers:
        df_mov = _pd.DataFrame(movers)
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("### 🟢 Top steamers (biggest Win-odds drops)")
            top = df_mov.sort_values("Δ%").head(12)
            st.dataframe(
                top.style.map(_delta_color, subset=["Δ%"])
                .format({"First Win": "{:.1f}", "Latest Win": "{:.1f}",
                         "Δ%": "{:+.1f}%"}, na_rep="—"),
                hide_index=True, width='stretch',
            )
        with c2:
            st.markdown("### 🔴 Top drifters (biggest Win-odds rises)")
            top = df_mov.sort_values("Δ%", ascending=False).head(12)
            st.dataframe(
                top.style.map(_delta_color, subset=["Δ%"])
                .format({"First Win": "{:.1f}", "Latest Win": "{:.1f}",
                         "Δ%": "{:+.1f}%"}, na_rep="—"),
                hide_index=True, width='stretch',
            )

    # ════════════════════════════════════════════════════════════════
    # 5) CROSS-POOL SMART-MONEY SIGNAL — horses where money comes in
    #    across BOTH the Win pool AND the QIN/QPL exotic pools.
    #    A Win-only steam can be a punter splash; a *cross-pool* steam
    #    is harder to fake and far more often informed.
    # ════════════════════════════════════════════════════════════════
    smart_rows = []
    for rn in races:
        rows = by_race[rn]
        if len(rows) < 2:
            continue
        first_w = {str(h.get("no")): _fnum(h.get("win"))
                   for h in rows[0].get("odds", [])}
        last_w = {str(h.get("no")): h for h in rows[-1].get("odds", [])}
        # Pair drifts (QIN + QPL) for this race using the helper
        qin_d = _compute_pair_drift(ymd, venue, int(rn), pool="qin")
        qpl_d = _compute_pair_drift(ymd, venue, int(rn), pool="qpl")

        def _per_horse_pair_stats(pairs):
            """Aggregate {horse_no: (n_firming, avg_dpct, n_total)} from a list
            of pair drift records."""
            buckets: dict[int, list[float]] = {}
            for p in pairs:
                d = p.get("dpct")
                if d is None:
                    continue
                for k in (p["a"], p["b"]):
                    buckets.setdefault(int(k), []).append(float(d))
            out = {}
            for k, vs in buckets.items():
                n_firm = sum(1 for v in vs if v <= -10.0)
                out[k] = (n_firm, round(sum(vs) / len(vs), 1), len(vs))
            return out

        qin_stats = _per_horse_pair_stats(qin_d.get("pairs", []))
        qpl_stats = _per_horse_pair_stats(qpl_d.get("pairs", []))

        for no_s, h in last_w.items():
            try:
                no_n = int(no_s)
            except (TypeError, ValueError):
                continue
            wf = first_w.get(no_s)
            wl = _fnum(h.get("win"))
            wd = _delta_pct(wf, wl)
            qin_firm, qin_avg, qin_n = qin_stats.get(no_n, (0, None, 0))
            qpl_firm, qpl_avg, qpl_n = qpl_stats.get(no_n, (0, None, 0))
            # Composite score: more negative = stronger smart-money signal.
            # Weight Win heavily, plus contributions from each exotic pool's
            # average drift on pairs involving this horse.
            parts = []
            if wd is not None:
                parts.append(("W", wd, 1.0))
            if qin_avg is not None and qin_n >= 2:
                parts.append(("QIN", qin_avg, 0.6))
            if qpl_avg is not None and qpl_n >= 2:
                parts.append(("QPL", qpl_avg, 0.6))
            if not parts:
                continue
            score = sum(d * w for _, d, w in parts) / sum(w for _, _, w in parts)
            # Count pools where this horse is firming meaningfully
            pools_in = sum(1 for _, d, _ in parts if d <= -10.0)
            smart_rows.append({
                "Race": rn,
                "No": no_n,
                "Horse": h.get("horse", ""),
                "Win Δ%": wd,
                "QIN avg Δ%": qin_avg,
                "QIN firm": f"{qin_firm}/{qin_n}" if qin_n else "—",
                "QPL avg Δ%": qpl_avg,
                "QPL firm": f"{qpl_firm}/{qpl_n}" if qpl_n else "—",
                "Pools↓": pools_in,
                "Score": round(score, 1),
            })
    if smart_rows:
        st.markdown("---")
        st.markdown("### 💰 Cross-Pool Smart-Money Board")
        st.caption(
            "Horses where money is coming in across **multiple pools** are a "
            "stronger informed-money signal than a Win-only steam (which can be "
            "noise from late public flash). `Pools↓` counts how many of "
            "{W, QIN, QPL} show ≤-10% drift for this horse. Score is a weighted "
            "average of those pool drifts (Win=1.0, QIN=0.6, QPL=0.6)."
        )
        df_sm = _pd.DataFrame(smart_rows)
        # Show only horses with at least one significant move OR with money in
        # ≥2 pools (avoids cluttering the board with 200 horses).
        df_show = df_sm[(df_sm["Pools↓"] >= 2)
                        | (df_sm["Score"] <= -10.0)
                        | (df_sm["Score"] >= 15.0)].copy()
        if df_show.empty:
            st.caption("_No cross-pool moves yet — wait for more snapshots._")
        else:
            df_show = df_show.sort_values("Score").head(20)
            st.dataframe(
                df_show.style.map(_delta_color,
                                  subset=["Win Δ%", "QIN avg Δ%",
                                          "QPL avg Δ%", "Score"])
                .format({"Win Δ%": "{:+.1f}%",
                         "QIN avg Δ%": "{:+.1f}%",
                         "QPL avg Δ%": "{:+.1f}%",
                         "Score": "{:+.1f}"}, na_rep="—"),
                hide_index=True, width='stretch',
            )
            st.caption(
                "💡 **How to read:** Horses near the top of this list (most "
                "negative Score) are the closest the live market gets to a "
                "'smart money' tell. A horse with Pools↓ = 3 and Score ≤ -15% "
                "is being keyed by people who put money in *all three* pools — "
                "if that horse isn't already in your top-3, consider QPL cover."
            )

    # ── Strategy doc ──────────────────────────────────────────────────
    with st.expander("📘 How to use live-odds drift as a sanity-check signal",
                     expanded=False):
        st.markdown(
            """
**Treat the market as a *second model*, not the truth.** Hong Kong tote money
is a mix of public, syndicates, and stable connections. Persistent moves
(≥20% over hours) carry more signal than late-flash moves (last 5–10 min,
often noise from late stable money).

**Cross-checking against our model:**

| Our top pick? | Market move | Action |
|---|---|---|
| 🟢 yes | 🟢 steaming (≥-20%) | **Strong agreement.** Increase confidence; consider full Kelly. |
| 🟢 yes | 🔴 drifting (≥+20%) | **Disagreement.** Lower stake or skip — late info we don't have. |
| 🔴 no | 🟢 steaming (≥-20%) | **Market sees something we missed.** Add to QPL/QIN as cover. |
| 🔴 no | 🔴 drifting | Confirms our fade — usually nothing to do. |

**How to capture useful drift:**
1. **Overnight snapshot** (T-12h to T-8h before post): baseline opinion.
2. **Morning** (~T-4h): public response to mornlines / scratchings.
3. **Pre-post** (~T-30 min): smart money + stable confidence.
4. **Final** (T-2 min): flash, often misleading — log but discount.

**Interpreting QIN/QPL drift** — pair-pool moves can reveal connection-level
information that doesn't show up in Win odds alone (e.g. a stable backing
the EXACTA, not the win pool). Look for QIN pairs where **both Win odds
drifted** but the *pair* odds dropped sharply: someone is keying that exact
combination.

**Notification-worthy drift events** (future automation):
- Our top-3 horse drifts ≥+30% from earliest snapshot → **WARN before stake**.
- A horse outside our top-5 steams ≥-30% → **REVIEW** (re-run model, may want QPL cover).
- A QIN pair drops ≥-25% but neither horse is in our top-3 → **REVIEW** (possible info edge).
"""
        )


# ─────────────────────────────────────────────────────────────────────────────
# Multi Builder page — per-race banker-box + cross-race All-Up
# ─────────────────────────────────────────────────────────────────────────────
def page_multi_builder():
    """Two builders, one page:
      • Per-race — banker × N-leg QIN+QPL coverage (one race at a time)
      • All-Up  — HKJC cross-race parlay over the QIN and QPL pools
    """
    st.markdown('<div class="page-title">🧮 Multi Builder</div>',
                unsafe_allow_html=True)

    main_tabs = st.tabs(["🪜 All-Up (cross-race)", "🎯 Per-race banker"])
    with main_tabs[0]:
        _mb_render_allup_tab()
    with main_tabs[1]:
        _mb_render_per_race_tab()


def _mb_render_per_race_tab():
    """Original per-race banker-box builder (one banker × N legs)."""
    import user_bets as ub
    from multi_builder import (
        build_multi_suggestion, evaluate_user_choice, build_meeting_multi,
    )

    # ── Meeting selector ────────────────────────────────────────────
    meetings = load_available_meetings()
    if not meetings:
        st.info("No analysed meetings found. Run an analysis from Race Day first.")
        return
    options = {m["title"]: m for m in meetings}
    sel_title = st.selectbox("Meeting", list(options.keys()), key="mb_mt")
    meeting = options[sel_title]
    date_compact = meeting["date_str"]
    venue_code = _venue_to_code(meeting.get("venue", "")) or "ST"

    data = load_meeting_data(meeting["file"])
    races = data.get("races", []) or []
    if not races:
        st.warning("No races in this meeting report.")
        return

    # ── Race selector ───────────────────────────────────────────────
    race_labels = {f"R{r.get('race_number')} — {r.get('race_class', '?')} "
                    f"{r.get('distance_m', '?')}m": r for r in races}
    sel_race_label = st.selectbox("Race", list(race_labels.keys()), key="mb_rc")
    race = race_labels[sel_race_label]
    rn = int(race.get("race_number") or 0)
    picks = race.get("picks") or []
    if not picks:
        st.warning("No picks in this race.")
        return

    # Build horse table
    runners = []
    for p in sorted(picks, key=lambda x: int(x.get("rank") or 99)):
        runners.append({
            "no": int(p.get("horse_no") or 0),
            "name": p.get("horse_name") or p.get("horse") or "",
            "rank": int(p.get("rank") or 99),
            "win_prob": float(p.get("win_prob") or 0),
        })

    # ── AI Suggestion panel ────────────────────────────────────────
    st.markdown("##### 🤖 AI suggestion")
    ai_col1, ai_col2 = st.columns([1, 3])
    with ai_col1:
        ai_mode = st.selectbox("Mode",
                                ["contrarian_banker", "model_consensus"],
                                key="mb_ai_mode",
                                help="contrarian_banker = rank 2-6 banker w/ edge "
                                     "(default); model_consensus = rank-1 banker "
                                     "(historical loser, shown for reference).")
    ai = build_multi_suggestion(
        date_compact=date_compact, venue_code=venue_code,
        race_no=rn, picks=picks, mode=ai_mode,
    )
    with ai_col2:
        if ai.get("skip_reason"):
            st.warning(f"AI skipped: {ai['skip_reason']}")
        else:
            ev = ai["evidence"]
            st.markdown(
                f"**Banker:** #{ai['banker']} (model rank {ev['banker_rank']}, "
                f"edge {ev['banker_edge_pp']:+.1f}pp)  \n"
                f"**Legs:** {ai['legs']} (ranks {ev['leg_ranks']}, "
                f"{ev['consensus_count']}/3 in model top-3)  \n"
                f"**Shape:** {ai['shape']} · "
                f"**Stake total:** ${ai['stake_total']:.0f} (@ "
                f"${ai['stake_per_pair']:.0f} per pair × 2 pools)"
            )
            if st.button("⬇ Use this suggestion", key="mb_use_ai"):
                st.session_state[f"mb_banker_{date_compact}_{rn}"] = ai["banker"]
                st.session_state[f"mb_legs_{date_compact}_{rn}"] = list(ai["legs"])
                st.rerun()

    st.markdown("---")
    st.markdown("##### 🛠️ Builder")

    # ── Builder controls ───────────────────────────────────────────
    runner_options = {f"#{r['no']} {r['name']} (rank {r['rank']})": r["no"]
                       for r in runners}
    runner_labels_by_no = {r["no"]: f"#{r['no']} {r['name']} (rank {r['rank']})"
                            for r in runners}

    default_banker = st.session_state.get(f"mb_banker_{date_compact}_{rn}")
    if default_banker is None and runners:
        default_banker = runners[0]["no"]
    default_banker_label = runner_labels_by_no.get(default_banker,
                                                     list(runner_options.keys())[0])

    bcol, sccol = st.columns([2, 1])
    with bcol:
        banker_label = st.selectbox(
            "Banker", list(runner_options.keys()),
            index=list(runner_options.keys()).index(default_banker_label)
            if default_banker_label in runner_options else 0,
            key="mb_banker_pick",
        )
        banker_no = runner_options[banker_label]
    with sccol:
        stake_per_pair = st.selectbox(
            "$ per pair (each pool)",
            [10, 20, 30, 50],
            index=0,
            key="mb_spp",
            help="$10 per pair = $20 per leg ($10 QIN + $10 QPL). "
                 "Apr +45% ROI band was $200-$300 total.",
        )

    # Default legs: model top-4 minus banker
    default_legs = st.session_state.get(f"mb_legs_{date_compact}_{rn}")
    if default_legs is None:
        default_legs = [r["no"] for r in runners[:4] if r["no"] != banker_no][:4]
    default_leg_labels = [runner_labels_by_no[h] for h in default_legs
                           if h in runner_labels_by_no and h != banker_no]
    leg_choices = {k: v for k, v in runner_options.items() if v != banker_no}
    leg_labels = st.multiselect(
        f"Legs (suggested: model top-4)",
        list(leg_choices.keys()),
        default=default_leg_labels,
        key="mb_legs_pick",
    )
    legs = [leg_choices[lbl] for lbl in leg_labels]

    # ── Evidence panel ─────────────────────────────────────────────
    if not legs:
        st.info("Pick at least one leg to see evidence + stake breakdown.")
        return
    evald = evaluate_user_choice(
        date_compact=date_compact, venue_code=venue_code,
        race_no=rn, picks=picks,
        banker=banker_no, legs=legs,
        stake_per_pair=stake_per_pair,
    )
    ev = evald["evidence"]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Coverage shape", evald["shape"])
    m2.metric("Total stake", f"${evald['stake_total']:.0f}")
    m3.metric("Banker class", "")
    m3.caption(ev.get("banker_class", ""))
    # Bucket badge
    if (ev["banker_rank"] <= 3 and ev["consensus_count"] >= 2):
        badge = "🟢 Agree (consensus)"
    elif (4 <= ev["banker_rank"] <= 6
          and (ev.get("banker_edge_pp") or 0) >= 5):
        badge = "🟡 Mid (edge play)"
    elif ev["banker_rank"] >= 7:
        badge = "🔴 Contrarian (high-variance)"
    else:
        badge = "⚪ Mixed"
    m4.metric("Bucket", "")
    m4.caption(badge)

    st.caption(evald["rationale"])

    # Per-leg evidence table
    leg_rows = []
    for h, rk, eg in zip(legs, ev["leg_ranks"], ev["leg_edges_pp"]):
        leg_rows.append({
            "#": h, "Horse": runner_labels_by_no.get(h, str(h)),
            "Mdl rank": rk, "Edge": f"{eg:+.1f}pp",
            "In top-3": "✓" if rk <= 3 else "",
        })
    st.dataframe(pd.DataFrame(leg_rows), hide_index=True,
                 width='stretch')

    st.markdown(
        f"**Will submit:** {evald['n_pairs']} × QIN_BANKER + "
        f"{evald['n_pairs']} × QPL_BANKER = "
        f"**{2*evald['n_pairs']} bets** at ${stake_per_pair} each = "
        f"**${evald['stake_total']:.0f} total.**"
    )

    # ── Submit button ──────────────────────────────────────────────
    submit_col, _ = st.columns([1, 2])
    with submit_col:
        confirm = st.checkbox("Confirm submission", key="mb_confirm")
        if st.button("💸 Submit to My Bets", type="primary",
                      disabled=not confirm, width='stretch'):
            note = (f"Multi Builder · {evald['shape']} · {badge} · "
                    f"banker rank {ev['banker_rank']} edge "
                    f"{ev.get('banker_edge_pp', 0):+.1f}pp")
            n_ok = 0
            for leg in legs:
                try:
                    ub.submit_bet(
                        meeting_date=date_compact,
                        venue=venue_code, race_number=rn,
                        bet_type="QIN_BANKER",
                        selections=[leg], banker=banker_no,
                        stake_hkd=float(stake_per_pair), notes=note,
                    )
                    ub.submit_bet(
                        meeting_date=date_compact,
                        venue=venue_code, race_number=rn,
                        bet_type="QPL_BANKER",
                        selections=[leg], banker=banker_no,
                        stake_hkd=float(stake_per_pair), notes=note,
                    )
                    n_ok += 2
                except Exception as e:
                    st.error(f"Failed leg {leg}: {e}")
            if n_ok > 0:
                # Trigger GitHub push so the bets persist on Cloud
                try:
                    push_ok = _gh_push_user_bets()
                except Exception:
                    push_ok = False
                msg = f"✅ Submitted {n_ok} bets (${evald['stake_total']:.0f} total)"
                if push_ok:
                    msg += " · Pushed to GitHub"
                st.success(msg)
                st.cache_data.clear()

    # ── Meeting-wide overview ──────────────────────────────────────
    st.markdown("---")
    with st.expander("📊 Meeting-wide AI scan", expanded=False):
        st.caption("Runs the AI rule across every race in this meeting "
                   "and ranks them by banker edge.")
        picks_by_race = {int(r.get("race_number") or 0): (r.get("picks") or [])
                          for r in races if r.get("picks")}
        all_sugs = build_meeting_multi(
            date_compact=date_compact, venue_code=venue_code,
            picks_by_race=picks_by_race, mode=ai_mode,
            stake_per_pair=stake_per_pair,
        )
        scan_rows = []
        for s in all_sugs:
            ev = s.get("evidence") or {}
            scan_rows.append({
                "R#": s["race_number"],
                "Banker": s["banker"] if s["banker"] else "—",
                "Bk rank": ev.get("banker_rank", "—"),
                "Bk edge": (f"{ev['banker_edge_pp']:+.1f}pp"
                            if ev.get("banker_edge_pp") is not None else "—"),
                "Legs": ",".join(str(x) for x in s["legs"]) if s["legs"] else "—",
                "Shape": s["shape"] or "—",
                "Stake": f"${s['stake_total']:.0f}" if s["stake_total"] else "—",
                "Status": s.get("skip_reason") or "ok",
            })
        st.dataframe(pd.DataFrame(scan_rows), hide_index=True,
                     width='stretch')
        actionable = [s for s in all_sugs if not s.get("skip_reason")]
        if actionable:
            total_stake = sum(s["stake_total"] for s in actionable)
            st.caption(f"Actionable races: {len(actionable)} · "
                       f"Total stake if you took every AI suggestion: "
                       f"${total_stake:.0f}")


def _mb_leg_block(*, rn: int, race: dict, edges: list[dict],
                   sarr_p_map: dict, rec_pairs,
                   is_pair_pool: bool, primary_pool: str,
                   prob_mode: str) -> dict | None:
    """Render one leg's selection editor in a single column.

    Returns the leg spec {race_no, selections} or None on validation error.
    """
    from all_up import horse_prob, rank_horses
    picks = race.get("picks") or []
    edge_lookup = {int(r["horse_no"]): r for r in edges
                    if r.get("horse_no") is not None}
    pick_lookup = {int(p["horse_no"]): p for p in picks
                    if p.get("horse_no") is not None}
    # Build ranked list using SARR-aware prob
    horses = sorted(set(pick_lookup.keys()) | set(edge_lookup.keys()))
    ranked = sorted(
        ((h, horse_prob(pick_lookup.get(h), edge_lookup.get(h),
                           prob_mode, sarr_p=sarr_p_map.get(h)))
          for h in horses),
        key=lambda kv: -kv[1],
    )
    horse_label = {}
    for h, p_h in ranked:
        pk = pick_lookup.get(h, {})
        er = edge_lookup.get(h, {})
        name = pk.get("horse_name", f"#{h}")
        odds = er.get("win_odds")
        odds_str = f" · ${float(odds):.1f}" if odds else ""
        horse_label[f"#{h} {name} ({p_h*100:.1f}%{odds_str})"] = h

    race_name = race.get("race_name") or ""
    race_dist = race.get("distance") or ""
    st.markdown(f"**R{rn}** · {race_name} · {race_dist}")

    if is_pair_pool:
        mode_q = st.radio(
            f"R{rn} structure", ["Banker × partners", "Free pairs"],
            index=0, horizontal=True, key=f"mb_au_qmode_{rn}",
        )
        default_pairs = rec_pairs or []
        if mode_q == "Banker × partners":
            default_banker = (default_pairs[0][0]
                                if default_pairs else
                                next(iter(horse_label.values()), None))
            banker_label_default = next(
                (lbl for lbl, h in horse_label.items()
                  if h == default_banker), list(horse_label.keys())[0]
            )
            banker_label = st.selectbox(
                f"R{rn} banker", list(horse_label.keys()),
                index=list(horse_label.keys()).index(banker_label_default),
                key=f"mb_au_bnk_{rn}",
            )
            banker = horse_label[banker_label]
            partner_options = {lbl: h for lbl, h in horse_label.items()
                                 if h != banker}
            default_partners = [
                lbl for lbl, h in partner_options.items()
                if any(h in p for p in default_pairs)
            ][:3] or list(partner_options.keys())[:2]
            partner_labels = st.multiselect(
                f"R{rn} partners (≥1)", list(partner_options.keys()),
                default=default_partners, key=f"mb_au_prt_{rn}",
            )
            pairs = [(min(banker, partner_options[lbl]),
                        max(banker, partner_options[lbl]))
                      for lbl in partner_labels]
        else:
            free_labels = st.multiselect(
                f"R{rn} pool of horses (≥2)", list(horse_label.keys()),
                default=list(horse_label.keys())[:3],
                key=f"mb_au_free_{rn}",
            )
            hs = [horse_label[lbl] for lbl in free_labels]
            from itertools import combinations as _comb
            pairs = [(min(a, b), max(a, b)) for a, b in _comb(hs, 2)]
        if not pairs:
            st.warning(f"R{rn}: at least 1 pair required.")
            return None
        return {"race_no": rn, "selections": pairs}
    # singles
    default_singles = (rec_pairs or [list(horse_label.values())[0]])
    default_lbls = [lbl for lbl, h in horse_label.items()
                      if h in default_singles]
    sel_labels = st.multiselect(
        f"R{rn} horses (≥1)", list(horse_label.keys()),
        default=default_lbls or list(horse_label.keys())[:1],
        key=f"mb_au_sel_{rn}",
    )
    sels = [horse_label[lbl] for lbl in sel_labels]
    if not sels:
        st.warning(f"R{rn}: at least 1 horse required.")
        return None
    return {"race_no": rn, "selections": sels}


def _mb_render_allup_tab():
    """HKJC-spec All-Up cross-race parlay with EV / Kelly / shape optimiser."""
    from all_up import (build_all_up_ticket, settle_all_up_ticket,
                         blended_pair, rank_horses, prob_for_pool,
                         evaluate_ticket, compare_shapes, recommend_legs,
                         kelly_fraction, horse_prob, _place_prob, _pair_prob,
                         SHAPE_PRESETS, POOLS, PAIR_POOLS, SINGLE_POOLS)
    from market_loader import compute_edge_table
    import all_up_log as aul

    st.caption(
        "HKJC All-Up cross-race parlay. Pick legs, choose any pool "
        "(WIN / PLACE / QIN / QPL), add as many selections per leg as "
        "you like — units = base shape × Π selections. EV, full-parlay "
        "probability, Kelly stake and best-shape recommendation update "
        "live as you change picks."
    )

    # ── Meeting selector ───────────────────────────────────────
    meetings = load_available_meetings()
    if not meetings:
        st.info("No analysed meetings found.")
        return
    options = {m["title"]: m for m in meetings}
    sel_title = st.selectbox("Meeting", list(options.keys()), key="mb_au_mt")
    meeting = options[sel_title]
    date_compact = meeting["date_str"]
    venue_code = _venue_to_code(meeting.get("venue", "")) or "ST"
    data = load_meeting_data(meeting["file"])
    races = data.get("races", []) or []
    if not races:
        st.warning("No races in this meeting report.")
        return
    races_by_no = {int(r.get("race_number") or 0): r for r in races}

    # ── Edge tables (cached, lazy per race) ────────────────────
    @st.cache_data(ttl=120, show_spinner=False)
    def _edges(date_compact: str, venue_code: str, rn: int,
                _picks_sig: tuple) -> list[dict]:
        race = races_by_no.get(int(rn))
        picks = race.get("picks") if race else []
        try:
            return compute_edge_table(date_compact, venue_code,
                                        int(rn), picks).get("rows") or []
        except Exception:
            return []

    def edges_of(rn: int) -> list[dict]:
        race = races_by_no.get(int(rn))
        sig = tuple(int(p.get("horse_no") or 0) for p in (race or {}).get("picks") or [])
        return _edges(date_compact, venue_code, int(rn), sig)

    # ── Pool / mode / stake controls ───────────────────────────
    c1, c2, c3, c4 = st.columns([1.2, 1.1, 1.1, 1])
    with c1:
        pool_label = st.selectbox(
            "Pool",
            ["WIN", "PLACE", "WIN + PLACE", "QIN", "QPL", "QIN + QPL"],
            index=4, key="mb_au_pool",
            help="WIN/PLACE = single horse per leg. "
                 "QIN/QPL = pair per leg. "
                 "WIN + PLACE issues two tickets. "
                 "**QIN + QPL = one combined leg**: the pair must finish "
                 "1st-2nd (Quinella), which also wins the Quinella Place, so "
                 "each leg pays BOTH dividends (QIN×QPL). Low hit-rate, "
                 "jackpot payout — your ‘one big win’ structure.",
        )
    with c2:
        prob_mode = st.selectbox(
            "Probability source",
            ["market", "blend", "model"],
            index=0, key="mb_au_pmode",
            help="market = market-implied (Apr backtest winner). "
                 "blend = 50/50 model+market. "
                 "model = v4.4 win_prob (calibration TODO).",
        )
    with c3:
        spu = st.selectbox("$ per unit", [1, 2, 5, 10], index=0,
                            key="mb_au_spu",
                            help="HKJC minimum is $1.")
    with c4:
        bankroll = st.number_input("Bankroll $", min_value=100,
                                     value=2000, step=100,
                                     key="mb_au_bk",
                                     help="Used by Kelly stake suggestion.")

    pools_to_build = (["WIN", "PLACE"] if pool_label == "WIN + PLACE"
                       else ["QIN+QPL"] if pool_label == "QIN + QPL"
                       else [pool_label])
    primary_pool = pools_to_build[0]
    is_pair_pool = primary_pool in PAIR_POOLS

    # ── Race selection ─────────────────────────────────────────
    st.markdown("##### 1. Pick races (legs)")
    race_options = {f"R{rn}": rn for rn in races_by_no.keys()}
    chosen_labels = st.multiselect(
        "Legs (2–6 races)", list(race_options.keys()),
        default=list(race_options.keys())[:3], key="mb_au_legs",
    )
    chosen_races = [race_options[lbl] for lbl in chosen_labels]
    if not (2 <= len(chosen_races) <= 6):
        st.warning("Pick between 2 and 6 races.")
        return
    n_legs = len(chosen_races)

    # ── Auto-recommend selections by edge ──────────────────────
    edges_by_race = {rn: edges_of(rn) for rn in chosen_races}
    # Pre-compute SARR softmax probabilities per race (model source)
    from all_up import sarr_probs as _sarr
    sarr_by_race = {rn: _sarr(races_by_no[rn].get("picks") or [])
                     for rn in chosen_races}
    rec = recommend_legs(
        races_by_no={rn: races_by_no[rn] for rn in chosen_races},
        edges_by_race=edges_by_race, pool=primary_pool,
        n_legs=n_legs, mode=prob_mode, sel_per_leg=2 if is_pair_pool else 1,
    )
    rec_by_race = {r["race_no"]: r["selections"] for r in rec}

    # ── Per-leg selection editor with horse names ─────────────
    st.markdown("##### 2. Selections per leg")
    if is_pair_pool:
        st.caption("For QIN / QPL each selection is a pair. Pick a banker "
                    "and the partners ‘with’ it; or define free pairs.")
    else:
        st.caption("Pick one or more horses per leg (multiple selections "
                    "multiply the unit count).")
    st.caption(
        "_The %% next to each horse is the **chosen probability source** — "
        "**model** = SARR softmax (ESZ + projected sectional + speedmap "
        "adjustments), **market** = market-implied from current odds, "
        "**blend** = 50/50. PLACE prob is derived from win prob._"
    )

    leg_specs: list[dict] = []
    # Render legs in a 2-column grid (rows of two)
    for row_start in range(0, len(chosen_races), 2):
        row_races = chosen_races[row_start:row_start + 2]
        cols = st.columns(len(row_races))
        for col, rn in zip(cols, row_races):
            with col:
                spec = _mb_leg_block(
                    rn=rn, race=races_by_no[rn],
                    edges=edges_by_race[rn],
                    sarr_p_map=sarr_by_race[rn],
                    rec_pairs=rec_by_race.get(rn),
                    is_pair_pool=is_pair_pool,
                    primary_pool=primary_pool,
                    prob_mode=prob_mode,
                )
                if spec is None:
                    return
                leg_specs.append(spec)

    # ── Probability + payout lookups for evaluator ────────────
    def prob_lookup(rn, sel):
        edges = edges_by_race.get(rn, [])
        picks = races_by_no[rn].get("picks") or []
        return prob_for_pool(rn, sel, pool=primary_pool,
                              pick_lookup={int(p["horse_no"]): p
                                            for p in picks if p.get("horse_no")},
                              edge_lookup={int(r["horse_no"]): r
                                            for r in edges if r.get("horse_no")},
                              mode=prob_mode,
                              sarr_lookup=sarr_by_race.get(rn))

    def payout_lookup(rn, sel):
        """Return $/$1 payout multiplier (so dividend/10) using market odds."""
        edges = edges_by_race.get(rn, [])
        edge_lookup = {int(r["horse_no"]): r for r in edges
                        if r.get("horse_no") is not None}
        if primary_pool == "WIN":
            r = edge_lookup.get(int(sel))
            if r and float(r.get("win_odds") or 0) > 1.0:
                return float(r["win_odds"])
            return 1.0
        if primary_pool == "PLACE":
            r = edge_lookup.get(int(sel))
            if r and float(r.get("win_odds") or 0) > 1.0:
                return float(r["win_odds"]) / 3.5
            return 1.0
        # QIN / QPL — proxy: 1/p_pair × takeout factor
        a, b = int(sel[0]), int(sel[1])
        picks = races_by_no[rn].get("picks") or []
        pick_lookup = {int(p["horse_no"]): p for p in picks
                        if p.get("horse_no") is not None}
        sl = sarr_by_race.get(rn, {})
        pa = horse_prob(pick_lookup.get(a), edge_lookup.get(a), prob_mode,
                          sarr_p=sl.get(a))
        pb = horse_prob(pick_lookup.get(b), edge_lookup.get(b), prob_mode,
                          sarr_p=sl.get(b))
        if primary_pool == "QIN+QPL":
            # Combined leg pays QIN × QPL dividends. Proxy each pool payout
            # off its own pair probability, then multiply.
            qp = _pair_prob(pa, pb, pool="QIN")
            pp = _pair_prob(pa, pb, pool="QPL")
            if qp <= 0 or pp <= 0:
                return 1.0
            qin_pay = (1 / qp) * 0.825
            qpl_pay = (1 / pp) * 0.825
            return max(1.0, qin_pay * qpl_pay)
        pp = _pair_prob(pa, pb, pool=primary_pool)
        if pp <= 0:
            return 1.0
        # HKJC takeout ≈ 17.5% on QIN, 17.5% on QPL
        return max(1.0, (1 / pp) * 0.825)

    # ── Shape comparison + best-shape pick ────────────────────
    st.markdown("##### 3. Shape optimiser")
    shape_rows = compare_shapes(legs=leg_specs, pool=primary_pool,
                                 prob_lookup=prob_lookup,
                                 payout_lookup=payout_lookup,
                                 stake_per_unit=spu)
    if not shape_rows:
        st.warning("No applicable shapes for this leg count.")
        return
    cmp_table = pd.DataFrame([{
        "Shape": r["shape"],
        "Units": r["n_units"],
        "Stake": f"${r['stake']:.0f}",
        "EV": f"${r['ev']:+.1f}",
        "ROI": f"{r['roi']*100:+.1f}%",
        "P(full hit)": f"{r['p_full']*100:.2f}%",
        "Sharpe": f"{r['sharpe']:.2f}",
    } for r in shape_rows])
    st.dataframe(cmp_table, hide_index=True, width='stretch')

    best = shape_rows[0]
    st.info(
        f"🏆 **Best EV shape: `{best['shape']}`** — "
        f"EV ${best['ev']:+.1f} on ${best['stake']:.0f} stake "
        f"({best['roi']*100:+.1f}% expected ROI, "
        f"P(full parlay) = {best['p_full']*100:.2f}%)."
    )

    # User picks shape
    shape_keys = [r["shape"] for r in shape_rows]
    shape_pick = st.selectbox(
        "Shape", shape_keys, index=0, key="mb_au_shape",
        help="Default = best EV. All shapes ranked above.",
    )
    sizes = SHAPE_PRESETS[shape_pick][1]

    # ── Build tickets for every requested pool ────────────────
    tickets = {}
    for pool in pools_to_build:
        t = build_all_up_ticket(legs=leg_specs, pool=pool, sizes=sizes,
                                 stake_per_unit=spu)
        if t.get("valid"):
            tickets[pool] = t
    if not tickets:
        st.error("Couldn’t build any ticket — check selections.")
        return

    # ── Live evaluation ───────────────────────────────────────
    st.markdown("##### 4. Live evaluation")
    eval_rows = []
    total_stake = 0.0
    total_ev = 0.0
    for pool, t in tickets.items():
        # rebuild pool-specific lookups (single primary used here for both —
        # fine as both pools use same selections shape if compound was W+P)
        e = evaluate_ticket(t, prob_lookup=prob_lookup,
                              payout_lookup=payout_lookup)
        eval_rows.append({
            "Pool": pool, "Units": t["n_units"],
            "Stake": f"${t['total_stake']:.0f}",
            "EV": f"${e['ev']:+.1f}",
            "ROI": f"{e['roi']*100:+.1f}%",
            "P(full)": f"{e['p_full_parlay']*100:.2f}%",
            "Exp full payout": f"${e['expected_full_payout']:.0f}",
        })
        total_stake += t["total_stake"]
        total_ev += e["ev"]
    st.dataframe(pd.DataFrame(eval_rows), hide_index=True,
                 width='stretch')

    # Per-leg breakdown
    with st.expander("🔍 Per-leg probabilities & payouts", expanded=False):
        primary_t = tickets[primary_pool]
        eprim = evaluate_ticket(primary_t, prob_lookup=prob_lookup,
                                  payout_lookup=payout_lookup)
        leg_table = []
        for i, lg in enumerate(primary_t["legs"]):
            sels = lg["selections"]
            sel_names = []
            picks = races_by_no[lg["race_no"]].get("picks") or []
            namebh = {int(p["horse_no"]): p.get("horse_name", "?")
                       for p in picks if p.get("horse_no")}
            for s in sels:
                if isinstance(s, tuple):
                    sel_names.append(f"{namebh.get(s[0],s[0])} + "
                                     f"{namebh.get(s[1],s[1])}")
                else:
                    sel_names.append(f"{namebh.get(s, s)}")
            leg_table.append({
                "Leg": f"R{lg['race_no']}",
                "Selections": " | ".join(sel_names),
                "P(hit)": f"{eprim['leg_p'][i]*100:.1f}%",
                "Avg payout/$1": f"${eprim['leg_payout'][i]:.1f}",
            })
        st.dataframe(pd.DataFrame(leg_table), hide_index=True,
                     width='stretch')

    # ── Kelly stake suggestion ────────────────────────────────
    st.markdown("##### 5. Stake sizing (quarter-Kelly cap)")
    primary_t = tickets[primary_pool]
    eprim = evaluate_ticket(primary_t, prob_lookup=prob_lookup,
                              payout_lookup=payout_lookup)
    p_full = eprim["p_full_parlay"]
    full_pay = eprim["expected_full_payout"]
    f_kelly = kelly_fraction(p_full, max(0.0, full_pay - 1))
    rec_stake = bankroll * f_kelly
    rec_units = max(0, int(rec_stake // max(spu, 1)))
    k1, k2, k3 = st.columns(3)
    k1.metric("Kelly fraction", f"{f_kelly*100:.2f}%")
    k1.caption("¼-Kelly cap applied")
    k2.metric("Suggested stake", f"${rec_stake:.0f}")
    k2.caption(f"On bankroll ${bankroll}")
    k3.metric("Suggested units", f"{rec_units}")
    k3.caption(f"@ ${spu}/unit · current ticket = {primary_t['n_units']} units")
    if rec_stake < primary_t["total_stake"] * 0.5:
        st.warning(
            f"⚠️ Kelly suggests **${rec_stake:.0f}** but current shape "
            f"requires ${primary_t['total_stake']:.0f}. Consider a "
            f"smaller shape (e.g. {shape_keys[-1]}) or skip."
        )
    elif f_kelly == 0.0:
        st.error("⛔ Kelly = 0 — this ticket has non-positive edge. Skip "
                  "or change selections / pool.")
    else:
        st.success(
            f"✅ Edge present. {f_kelly*100:.1f}% of bankroll = "
            f"${rec_stake:.0f}. Current ticket stake "
            f"${primary_t['total_stake']:.0f} is "
            f"{primary_t['total_stake']/max(rec_stake,1)*100:.0f}% of Kelly."
        )

    # ── Dynamic suggestions panel ─────────────────────────────
    st.markdown("##### 6. Suggestions to improve this ticket")
    suggestions: list[str] = []
    # 1) Coverage thinness
    leg_p = eprim["leg_p"]
    weakest_idx = min(range(len(leg_p)), key=lambda i: leg_p[i])
    weakest_leg = primary_t["legs"][weakest_idx]
    if leg_p[weakest_idx] < 0.18 and primary_pool in PAIR_POOLS:
        suggestions.append(
            f"🔧 R{weakest_leg['race_no']} hit-prob is "
            f"{leg_p[weakest_idx]*100:.1f}% — add 1 more partner to widen."
        )
    # 2) Over-coverage
    over = [i for i, p in enumerate(leg_p) if p > 0.65]
    if over and primary_pool in PAIR_POOLS:
        i = over[0]
        suggestions.append(
            f"✂️ R{primary_t['legs'][i]['race_no']} hit-prob is already "
            f"{leg_p[i]*100:.1f}% — drop the weakest partner to cut units."
        )
    # 3) Banker pool suggestion
    if primary_pool in PAIR_POOLS and prob_mode == "model":
        suggestions.append(
            "📊 Apr-2026 backtest: market mode > model on QIN/QPL parlays. "
            "Consider switching probability source to **market**."
        )
    # 4) Pool comparison: if PLACE EV > QIN EV at same stake
    if primary_pool in PAIR_POOLS and total_stake > 50:
        place_legs = [{"race_no": lg["race_no"],
                        "selections": [s[0] for s in lg["selections"]]}
                       for lg in leg_specs]
        place_t = build_all_up_ticket(legs=place_legs, pool="PLACE",
                                        sizes=sizes, stake_per_unit=spu)
        if place_t.get("valid"):
            ev_pl = evaluate_ticket(place_t, prob_lookup=lambda rn, s:
                                       prob_for_pool(rn, s, pool="PLACE",
                                          pick_lookup={int(p["horse_no"]): p for p in
                                              races_by_no[rn].get("picks") or []
                                              if p.get("horse_no")},
                                          edge_lookup={int(r["horse_no"]): r for r in
                                              edges_by_race.get(rn, []) if r.get("horse_no")},
                                          mode=prob_mode),
                                     payout_lookup=lambda rn, s:
                                       (lambda er: float(er.get("win_odds", 0))/3.5
                                          if er and float(er.get("win_odds") or 0) > 1
                                          else 1.0)(
                                           {int(r["horse_no"]): r for r in
                                              edges_by_race.get(rn, [])}.get(int(s))))
            if ev_pl["roi"] > eprim["roi"] + 0.05:
                suggestions.append(
                    f"💡 Same legs in PLACE all-up = "
                    f"{ev_pl['roi']*100:+.1f}% ROI vs "
                    f"{eprim['roi']*100:+.1f}% on {primary_pool} — "
                    f"PLACE pool has stronger edge here."
                )
    # 5) Best alternative shape
    if shape_pick != best["shape"]:
        delta = best["ev"] - next(r["ev"] for r in shape_rows
                                     if r["shape"] == shape_pick)
        suggestions.append(
            f"🎯 Switching shape `{shape_pick}` → `{best['shape']}` improves "
            f"EV by ${delta:+.1f}."
        )
    # 6) Kelly under-/over-shoot
    if rec_stake > 0 and primary_t["total_stake"] > rec_stake * 1.5:
        suggestions.append(
            f"⚠️ Stake ${primary_t['total_stake']:.0f} > 1.5× Kelly "
            f"(${rec_stake:.0f}) — over-betting."
        )
    if not suggestions:
        suggestions.append("✅ No improvements detected for current shape.")
    for s in suggestions:
        st.markdown(f"- {s}")

    # ── Submit + log ───────────────────────────────────────────
    st.markdown("##### 7. Log tickets")
    confirm = st.checkbox("Confirm — log all built tickets",
                           key="mb_au_confirm")
    notes = st.text_input("Notes (optional)", key="mb_au_notes")
    cols = st.columns([1, 3])
    with cols[0]:
        clicked = st.button("💸 Log", type="primary", disabled=not confirm)
    if clicked:
        try:
            ids = []
            for pool, t in tickets.items():
                tid = aul.submit_ticket(
                    t, meeting_date=date_compact, venue=venue_code,
                    pair_mode=prob_mode, notes=notes,
                )
                ids.append(f"{pool}={tid}")
            try:
                push_ok = _gh_push_user_bets()
            except Exception:
                push_ok = False
            msg = "✅ Logged: " + ", ".join(ids)
            if push_ok:
                msg += "  ·  Pushed to GitHub"
            st.success(msg)
        except Exception as e:
            st.error(f"Log failed: {e}")

    # ── Existing tickets table ─────────────────────────────────
    st.markdown("---")
    st.markdown("##### 📒 Logged All-Up tickets")
    rows = aul.load_tickets(settle=True)
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    if not rows:
        st.caption("_No tickets logged yet._")
    else:
        table = []
        for r in rows[:30]:
            t = r["ticket"]
            def _fmt_leg(lg):
                sels = lg.get("selections") or [lg.get("pair")]
                parts = []
                for s in sels:
                    if isinstance(s, (list, tuple)) and len(s) == 2:
                        parts.append(f"{s[0]}-{s[1]}")
                    else:
                        parts.append(str(s))
                return f"R{lg['race_no']}({'/'.join(parts)})"
            legs_str = " → ".join(_fmt_leg(l) for l in t["legs"])
            table.append({
                "Date": r["meeting_date"], "Pool": t["pool"],
                "Shape": t["shape_label"], "Legs": legs_str,
                "Stake": f"${t['total_stake']:.0f}",
                "Mode": r.get("pair_mode", ""),
                "Status": r.get("status", "open"),
                "Return": (f"${r.get('return_hkd', 0):.0f}"
                            if r.get("status") == "settled" else "—"),
                "PnL": (f"${r.get('pnl_hkd', 0):+.0f}"
                         if r.get("status") == "settled" else "—"),
            })
        st.dataframe(pd.DataFrame(table), hide_index=True,
                     width='stretch')


# ─────────────────────────────────────────────────────────────────────────────
# My Bets page — personal wager tracker (submit / edit / settle / summary)
# ─────────────────────────────────────────────────────────────────────────────
def page_my_bets():
    """User-submitted bets, auto-settled vs reports/dividends_*.json."""
    import user_bets as ub
    import datetime as _dt

    # ── Password gate ──────────────────────────────────────────────────────
    # MyBets contains real wagers + bookie statement uploads — gate behind a
    # session-scoped password. Persist auth in st.session_state so once
    # logged in, the user can navigate away and back without re-entering.
    if not st.session_state.get("_mb_authed"):
        st.markdown('<div class="page-title">🔒 My Bets</div>',
                    unsafe_allow_html=True)
        st.markdown(
            '<div class="page-subtitle">Password-protected — enter to '
            'continue.</div>', unsafe_allow_html=True,
        )
        with st.form("mb_login_form", clear_on_submit=False):
            pw = st.text_input("Password", type="password", key="mb_pw")
            ok = st.form_submit_button("Unlock", width='stretch')
        if ok:
            if (pw or "").strip() == "mighty_commander":
                st.session_state["_mb_authed"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        return

    st.markdown('<div class="page-title">💰 My Bets</div>',
                unsafe_allow_html=True)
    st.markdown(
        '<div class="page-subtitle">Personal wager log · auto-settled from '
        'HKJC dividends · timeline performance view</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Every bet you log here is saved to `reports/user_bets_log.jsonl`. "
        "Once the results & dividends for its meeting are scraped, the bet "
        "is automatically settled (return + PnL computed)."
    )

    tabs = st.tabs(
        ["➕ New bet", "📒 Open bets", "✅ Settled history",
         "📊 Summary", "📅 Calendar", "🆚 vs Model",
         "🔬 Forensic Analyzer"]
    )

    # ── TAB 1 — Submit ──────────────────────────────────────────────────
    with tabs[0]:
        # ── Statement import expander ────────────────────────────────
        with st.expander("📥 Import bookie statement (.txt)", expanded=False):
            st.caption(
                "Upload the text file downloaded from your HKJC account "
                "(Account Records) — every Quinella + Quinella Place bundle "
                "is split into a QIN + QPL record (½ debit each). "
                "Already-imported bets (by bookie ref #) are skipped."
            )
            up = st.file_uploader("Statement .txt", type=["txt"],
                                  key="mb_stmt_upload")
            force_reimport = st.checkbox(
                "🔁 Force re-import (delete existing rows for these refs first)",
                key="mb_stmt_force",
                value=False,
                help="Tick this if you've edited / deleted bets manually and want "
                     "the statement file to be the source of truth. Every bookie "
                     "ref that appears in the uploaded file is REMOVED from the "
                     "log and re-inserted with fresh values.",
            )
            if up is not None:
                import tempfile
                import parse_acct_statement as pas
                # IMPORTANT: use getvalue() (pointer-independent), NOT read().
                # This block re-runs on every Streamlit rerun (e.g. when the
                # Preview / Import buttons are clicked). read() consumes the
                # uploaded buffer, so on the rerun that actually triggers the
                # import it would return b"" → an empty temp file → "0 blocks"
                # parsed and nothing imported. getvalue() always returns the
                # full bytes regardless of the read position.
                _raw = up.getvalue()
                with tempfile.NamedTemporaryFile(
                        mode="wb", suffix=".txt", delete=False) as f:
                    f.write(_raw)
                    tmp_path = Path(f.name)
                if not _raw:
                    st.error(
                        "Uploaded file is empty (0 bytes). Re-select the "
                        "statement .txt and try again."
                    )
                try:
                    cprev, cimp = st.columns([1, 1])
                    with cprev:
                        preview = st.button(
                            "🔍 Preview only",
                            key="mb_stmt_prev",
                            width='stretch',
                        )
                    with cimp:
                        do_import = st.button(
                            "✅ Import now",
                            key="mb_stmt_imp",
                            type="primary",
                            width='stretch',
                        )
                    if preview:
                        parsed = pas.parse_statement(tmp_path)
                        # Count bet-classified blocks so the user can see if
                        # any wager failed to parse (and would be silently
                        # dropped on import).
                        try:
                            _blocks = pas._split_blocks(
                                tmp_path.read_text(encoding="utf-8"))
                            _n_bet_blocks = sum(
                                1 for b in _blocks
                                if pas._classify_block(b) == "bet")
                        except Exception:
                            _n_bet_blocks = len(parsed)
                        _failed = max(0, _n_bet_blocks - len(parsed))
                        if _failed:
                            st.warning(
                                f"Parsed **{len(parsed)}** of "
                                f"**{_n_bet_blocks}** bet block(s) — "
                                f"**{_failed}** could not be parsed and would "
                                "be skipped. Check the statement formatting "
                                "for those wagers."
                            )
                        else:
                            st.info(f"Parsed {len(parsed)} bet block(s).")
                        if parsed:
                            st.dataframe(
                                pd.DataFrame(parsed),
                                hide_index=True,
                                width='stretch',
                            )
                    if do_import:
                        summary = pas.import_statement(
                            tmp_path, force=force_reimport
                        )
                        # Force a fresh settlement pass so every bet from races
                        # with published dividends shows return_hkd immediately.
                        try:
                            ub.load_bets(settle=True)
                        except Exception:
                            pass
                        push_ok = _gh_push_user_bets()
                        purged = summary.get("purged", 0)
                        purge_msg = (
                            f" Purged **{purged}** pre-existing row(s) before re-insert."
                            if purged else ""
                        )
                        st.success(
                            f"Inserted **{summary['inserted']}** record(s); "
                            f"skipped **{summary['skipped']}** "
                            f"(from {summary['total_blocks']} blocks).{purge_msg}"
                        )
                        # Surface persistence status — critical on Streamlit
                        # Cloud where the local filesystem is ephemeral.
                        if push_ok:
                            st.success(
                                "☁ Pushed to GitHub — changes will survive "
                                "the next container restart.",
                                icon="✅",
                            )
                        elif _gh_headers():
                            st.error(
                                "⚠ GitHub push FAILED. Imports are saved to "
                                "local disk only — they will revert on the "
                                "next container restart. Check the **Cloud "
                                "Persistence** panel in the sidebar for the "
                                "exact error.",
                                icon="🚨",
                            )
                        else:
                            st.warning(
                                "No `GITHUB_TOKEN` configured — imports are "
                                "saved to local disk only and will revert on "
                                "container restart. Add a fine-grained PAT to "
                                "Streamlit Cloud secrets to enable persistence.",
                                icon="⚠️",
                            )
                        if summary["inserted_details"]:
                            st.dataframe(
                                pd.DataFrame(summary["inserted_details"]),
                                hide_index=True,
                                width='stretch',
                            )
                        if summary["skipped_refs"]:
                            st.caption(
                                "Skipped refs: "
                                + ", ".join(summary["skipped_refs"])
                            )
                finally:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass
        st.markdown("### Submit a new bet")

        # Friendly labels + inline "how to enter" hints per bet type
        _BET_TYPE_LABELS = {
            "WIN":        "Win (single horse)",
            "PLACE":      "Place (single horse)",
            "QIN":        "Quinella — box across selections",
            "QPL":        "Quinella Place — box across selections",
            "QIN_BANKER": "Quinella Banker — 1 banker × N legs",
            "QPL_BANKER": "Quinella Place Banker — 1 banker × N legs",
            "TRIO":       "Trio — box across selections (C(n,3) combos)",
            "TCE":         "Tierce — exact-order 1st-2nd-3rd (single permutation)",
            "TCE_BOX":     "Tierce Box — box across selections (n×(n-1)×(n-2) perms)",
            "F4_BOX":     "First 4 Box — box across selections (C(n,4) combos)",
            "QTT_BOX":    "Quartet Box — box across selections (top-4 in EXACT order)",
            "QTT_MB":     "Quartet Multi-Banker — 4 leg-lists, one per finishing position",
        }
        _BET_TYPE_HELP = {
            "WIN":        "Enter ONE horse number in *Selections*. "
                          "Wins if that horse finishes 1st.",
            "PLACE":      "Enter ONE horse number in *Selections*. "
                          "Wins if that horse finishes 1st–3rd.",
            "QIN":        "Enter 2+ horse numbers in *Selections* "
                          "(e.g. `2,5,7` = 3 combos: 2-5, 2-7, 5-7). "
                          "Wins if any pair finishes 1st+2nd in either order.",
            "QPL":        "Enter 2+ horse numbers in *Selections* "
                          "(e.g. `2,5,7` = 3 combos). "
                          "Wins if any pair both finish 1st–3rd.",
            "QIN_BANKER": "Enter ONE banker in *Banker*, then 1+ other horses "
                          "in *Legs* (e.g. banker=4, legs=`2,5,7` = 3 combos: "
                          "4-2, 4-5, 4-7). Wins if banker finishes 1st-or-2nd "
                          "AND one of the legs takes the other placing.",
            "QPL_BANKER": "Enter ONE banker in *Banker*, then 1+ other horses "
                          "in *Legs* (e.g. banker=4, legs=`2,5,7` = 3 combos). "
                          "Wins whenever banker + one of the legs both finish "
                          "1st–3rd. HKJC bundles this with QIN_BANKER as "
                          "\"1 banker × 3 legs = 6 bets\" — log each leg "
                          "separately here if you want per-pool PnL.",
            "TRIO":       "Enter 3+ horse numbers in *Selections*. "
                          "Wins if any triple matches top-3 in any order.",
            "TCE":        "Enter exactly 3 horse numbers in *Selections* "
                          "in the ORDER you expect them to finish "
                          "(1st, 2nd, 3rd). Wins only if the finish is "
                          "exactly that order.",
            "TCE_BOX":    "Enter 3+ horse numbers in *Selections*. Wins "
                          "ONLY if the actual top-3 in EXACT order is one "
                          "of the permutations covered by your box. Stake "
                          "is split across n×(n-1)×(n-2) permutations.",
            "F4_BOX":     "Enter 4+ horse numbers in *Selections*. "
                          "Wins if any 4 of them are the top-4 finishers "
                          "in any order.",
            "QTT_BOX":    "Enter 4+ horse numbers in *Selections*. Wins ONLY "
                          "if any 4 of them finish 1st–2nd–3rd–4th in EXACT "
                          "order. Stake is split across C(n,4)×24 "
                          "permutations — typically a much smaller "
                          "per-combo unit than First 4.",
            "QTT_MB":     "Quartet Multi-Banker is currently **import-only** "
                          "(via `Import bookie statement`). It carries four "
                          "separate leg-lists (one per 1st/2nd/3rd/4th "
                          "position) and auto-settles correctly. To log a "
                          "new one manually, paste the relevant block into a "
                          "`acctstmt*.txt` file and re-import.",
        }

        with st.form("my_bets_submit_form", clear_on_submit=True):
            c1, c2, c3 = st.columns([1.2, 0.7, 0.8])
            with c1:
                mdate = st.date_input(
                    "Meeting date", value=_dt.date.today(),
                    key="mb_submit_date",
                )
            with c2:
                venue = st.selectbox("Venue", ["HV", "ST"],
                                         key="mb_submit_venue")
            with c3:
                race = st.number_input("Race #", min_value=1, max_value=12,
                                            step=1, value=1,
                                            key="mb_submit_race")
            c4, c5 = st.columns([1.4, 0.6])
            with c4:
                # QTT_MB needs four leg-lists which the manual form doesn't
                # capture — keep it import-only.
                _manual_types = [t for t in ub.BET_TYPES if t != "QTT_MB"]
                bet_type = st.selectbox(
                    "Bet type", _manual_types,
                    index=_manual_types.index("QIN"),
                    format_func=lambda t: _BET_TYPE_LABELS.get(t, t),
                    key="mb_submit_type",
                )
            with c5:
                stake = st.number_input(
                    "Stake (HK$)", min_value=10.0, step=10.0, value=20.0,
                    key="mb_submit_stake",
                )

            # Contextual hint for the selected bet type
            st.info(f"**How to enter {_BET_TYPE_LABELS.get(bet_type, bet_type)}**  \n"
                    f"{_BET_TYPE_HELP.get(bet_type, '')}")

            needs_banker = bet_type in ("QIN_BANKER", "QPL_BANKER")
            banker_no = None
            if needs_banker:
                bcol, _ = st.columns([0.4, 1.6])
                with bcol:
                    banker_no = st.number_input(
                        "Banker horse #", min_value=1, max_value=14,
                        step=1, value=1, key="mb_submit_banker",
                        help="The horse you think is most likely to place; "
                             "combined with each of your legs.",
                    )
            sels_text = st.text_input(
                ("Legs (comma-sep horse #s, excl. banker)" if needs_banker else
                  "Selections (comma-sep horse #s)"),
                placeholder="e.g. 2,5,7",
                key="mb_submit_sels",
                help=("Pair/triple combos are generated automatically. "
                      "You DO NOT need to list the banker here."
                      if needs_banker else
                      "For WIN/PLACE enter one number; for box bets enter "
                      "all horses you want pairs/triples generated from."),
            )
            notes = st.text_area("Notes (optional)", height=60,
                                    key="mb_submit_notes",
                                    placeholder="e.g. \"HKJC bundle: QIN+QPL "
                                                "1 banker × 3 sels\"")
            submit = st.form_submit_button("💾 Save bet",
                                                width='stretch',
                                                type="primary")
            if submit:
                try:
                    sels = [int(x.strip()) for x in sels_text.split(",")
                              if x.strip()]
                    # Validation per bet type
                    if bet_type in ("WIN", "PLACE"):
                        if len(sels) != 1:
                            st.error(f"{bet_type} requires exactly ONE "
                                     f"selection (got {len(sels)}).")
                            st.stop()
                    elif bet_type in ("QIN", "QPL"):
                        if len(sels) < 2:
                            st.error(f"{bet_type} box requires 2+ "
                                     f"selections (got {len(sels)}).")
                            st.stop()
                    elif bet_type in ("QIN_BANKER", "QPL_BANKER"):
                        if not banker_no:
                            st.error("Banker horse # is required.")
                            st.stop()
                        if len(sels) < 1:
                            st.error("Need 1+ leg horses.")
                            st.stop()
                        if banker_no in sels:
                            st.error(f"Banker ({banker_no}) must not appear "
                                     f"in legs.")
                            st.stop()
                    elif bet_type == "TRIO":
                        if len(sels) < 3:
                            st.error("TRIO box requires 3+ selections.")
                            st.stop()
                    elif bet_type == "TCE":
                        if len(sels) != 3:
                            st.error("TCE requires exactly 3 selections "
                                     "in finishing order (1st, 2nd, 3rd).")
                            st.stop()
                    elif bet_type == "TCE_BOX":
                        if len(sels) < 3:
                            st.error("TCE_BOX requires 3+ selections.")
                            st.stop()
                    elif bet_type == "F4_BOX":
                        if len(sels) < 4:
                            st.error("F4 box requires 4+ selections.")
                            st.stop()

                    bid = ub.submit_bet(
                        meeting_date=mdate.strftime("%Y%m%d"),
                        venue=venue, race_number=int(race),
                        bet_type=bet_type,
                        selections=sels or [banker_no],
                        banker=int(banker_no) if needs_banker else None,
                        stake_hkd=float(stake), notes=notes,
                    )
                    _gh_push_user_bets()
                    st.success(
                        f"✅ Bet saved (id {bid[:6]}…) — "
                        f"{_BET_TYPE_LABELS.get(bet_type, bet_type)}, "
                        f"${float(stake):.0f}."
                    )
                except ValueError as e:
                    st.error(str(e))

    rows = ub.load_bets(settle=True)
    # Persist auto-settlement updates to GitHub so they survive a redeploy.
    # Only pushes when running on Streamlit Cloud + the settled count has
    # changed since the last render this session (avoids redundant API calls).
    try:
        _settled_now = sum(1 for r in rows if r.get("status") == "settled")
        if st.session_state.get("_mb_last_settled_count") != _settled_now:
            st.session_state["_mb_last_settled_count"] = _settled_now
            _gh_push_user_bets()
    except Exception:
        pass

    # ── TAB 2 — Open bets (edit / delete) ───────────────────────────────
    with tabs[1]:
        open_rows = [r for r in rows if r.get("status") != "settled"]
        if not open_rows:
            st.info("No open bets. Use the **New bet** tab to add one.")
        else:
            open_rows.sort(key=lambda r: (r.get("meeting_date",""), r.get("race_number",0)))
            st.markdown(f"**{len(open_rows)} open** bet(s)")

            # Bulk-delete by meeting date (handy after a bad re-import).
            with st.expander("🗑️ Bulk delete", expanded=False):
                _dates = sorted({r.get("meeting_date", "") for r in open_rows})
                _bulk_pick = st.multiselect(
                    "Delete ALL open bets for these meeting dates",
                    options=_dates,
                    key="mb_bulk_dates",
                    help="Use this if you accidentally imported a statement "
                         "twice and want a clean slate before re-importing.",
                )
                _confirm = st.checkbox(
                    "I understand this cannot be undone.",
                    key="mb_bulk_confirm",
                )
                if st.button(
                    "Delete selected dates",
                    key="mb_bulk_delete_btn",
                    type="primary",
                    disabled=not (_bulk_pick and _confirm),
                ):
                    _n_del = 0
                    for _r in list(open_rows):
                        if _r.get("meeting_date") in _bulk_pick:
                            ub.delete_bet(_r["bet_id"])
                            _n_del += 1
                    _gh_push_user_bets()
                    st.success(f"Deleted {_n_del} open bet(s).")
                    st.rerun()

            for r in open_rows:
                _sel_str = _format_bet_selections(r)
                with st.expander(
                    (f"{r['meeting_date']} · {r['venue']} R{r['race_number']} · "
                      f"{r['bet_type']} · ${r['stake_hkd']:.0f}  —  {_sel_str}"),
                    expanded=False,
                ):
                    st.markdown(f"**Selections:** {_sel_str}")
                    st.write({
                        "Selections (raw)": r.get("selections"),
                        "Banker (raw)":     r.get("banker"),
                        "Created":          r.get("created_at"),
                        "Notes":            r.get("notes"),
                    })
                    cdel, cref = st.columns([1, 1])
                    with cdel:
                        if st.button("🗑️ Delete",
                                          key=f"mb_del_{r['bet_id']}"):
                            ub.delete_bet(r["bet_id"])
                            _gh_push_user_bets()
                            st.rerun()
                    with cref:
                        new_stake = st.number_input(
                            "Adjust stake", min_value=10.0, step=10.0,
                            value=float(r["stake_hkd"]),
                            key=f"mb_stake_{r['bet_id']}",
                        )
                        if st.button("💾 Update",
                                          key=f"mb_upd_{r['bet_id']}"):
                            ub.edit_bet(r["bet_id"],
                                         stake_hkd=float(new_stake))
                            _gh_push_user_bets()
                            st.rerun()

    # ── TAB 3 — Settled history ─────────────────────────────────────────
    with tabs[2]:
        settled_rows = [r for r in rows if r.get("status") == "settled"]
        if not settled_rows:
            st.info("No settled bets yet. They settle automatically once "
                       "`reports/dividends_YYYYMMDD.json` exists for the meeting.")
        else:
            settled_rows.sort(key=lambda r: r.get("settled_at", ""),
                                 reverse=True)

            # Bulk-delete by meeting date for cleaning up duplicates.
            with st.expander("🗑️ Delete settled records", expanded=False):
                _sdates = sorted({r.get("meeting_date", "") for r in settled_rows})
                _sbulk_pick = st.multiselect(
                    "Delete ALL settled bets for these meeting dates",
                    options=_sdates,
                    key="mb_sbulk_dates",
                    help="Use this to remove duplicated imports. Records are "
                         "erased from `reports/user_bets_log.jsonl`.",
                )
                _sconfirm = st.checkbox(
                    "I understand this cannot be undone.",
                    key="mb_sbulk_confirm",
                )
                if st.button(
                    "Delete selected dates",
                    key="mb_sbulk_delete_btn",
                    type="primary",
                    disabled=not (_sbulk_pick and _sconfirm),
                ):
                    _n_sdel = 0
                    for _r in list(settled_rows):
                        if _r.get("meeting_date") in _sbulk_pick:
                            ub.delete_bet(_r["bet_id"])
                            _n_sdel += 1
                    _gh_push_user_bets()
                    st.success(f"Deleted {_n_sdel} settled bet(s).")
                    st.rerun()

            table = []
            for r in settled_rows:
                _md = (r.get("meeting_date") or "").replace("-", "")
                try:
                    _rn = int(r.get("race_number")) if r.get("race_number") is not None else None
                except (TypeError, ValueError):
                    _rn = None
                _lookup = _horse_name_lookup(_md) if _md else {}
                def _name_only(n):
                    try:
                        n_int = int(n)
                    except (TypeError, ValueError):
                        return str(n)
                    nm = _lookup.get((_rn, n_int)) if _rn is not None else None
                    return f"{n_int} {nm}" if nm else str(n_int)
                _sels_named = " · ".join(_name_only(x) for x in r.get("selections", []))
                _banker_named = _name_only(r["banker"]) if r.get("banker") is not None else ""
                table.append({
                    "Date":    r["meeting_date"],
                    "R":       r["race_number"],
                    "Type":    r["bet_type"],
                    "Selections": _sels_named,
                    "Banker":  _banker_named,
                    "Stake":   float(r["stake_hkd"]),
                    "Return":  float(r.get("return_hkd", 0)),
                    "PnL":     float(r.get("pnl_hkd", 0)),
                    "Hit":     "✅" if r.get("hit") else "❌",
                    "Notes":   r.get("notes", ""),
                })
            st.dataframe(
                pd.DataFrame(table), hide_index=True, width='stretch',
                column_config={
                    "R":      st.column_config.NumberColumn(format="%d"),
                    "Stake":  st.column_config.NumberColumn(format="$%.0f"),
                    "Return": st.column_config.NumberColumn(format="$%.2f"),
                    "PnL":    st.column_config.NumberColumn(format="%+.2f"),
                },
            )

    # ── TAB 4 — Summary & timeline ──────────────────────────────────────
    with tabs[3]:
        if not rows:
            st.info("No bets logged yet.")
        else:
            win_opts = {"Last 7 days": "7d", "Last 30 days": "30d",
                           "Year-to-date": "ytd", "All time": "all"}
            win_label = st.radio("Timeline", list(win_opts.keys()),
                                      horizontal=True, index=3,
                                      key="mb_summary_win")
            filt = ub.filter_by_window(rows, win_opts[win_label])
            s = ub.summarise(filt)

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Bets", s["total_bets"],
                        delta=f"{s['open']} open" if s["open"] else None)
            m2.metric("Stake", f"${s['stake']:.0f}")
            m3.metric("Return", f"${s['return']:.2f}",
                        delta=f"PnL {s['pnl']:+.2f}")
            m4.metric("ROI",
                        f"{s['roi']*100:+.1f}%" if s["stake"] else "—")
            m5.metric("Hit rate",
                        f"{s['hit_rate']*100:.1f}%",
                        delta=f"{s['hits']} wins")

            if s["by_type"]:
                st.markdown("#### By bet type")
                bt_rows = []
                for bt, v in s["by_type"].items():
                    bt_rows.append({
                        "Type":   bt,
                        "Bets":   v["bets"],
                        "Hits":   v["hits"],
                        "Hit %":  (v["hits"]/v["bets"]*100) if v["bets"] else 0.0,
                        "Stake":  v["stake"],
                        "Return": v["ret"],
                        "PnL":    v["ret"] - v["stake"],
                        "ROI %":  ((v["ret"]-v["stake"])/v["stake"]*100)
                                        if v["stake"] else 0.0,
                    })
                st.dataframe(
                    pd.DataFrame(bt_rows).sort_values("ROI %", ascending=False),
                    hide_index=True, width='stretch',
                    column_config={
                        "Bets":   st.column_config.NumberColumn(format="%d"),
                        "Hits":   st.column_config.NumberColumn(format="%d"),
                        "Hit %":  st.column_config.NumberColumn(format="%.1f%%"),
                        "Stake":  st.column_config.NumberColumn(format="$%.0f"),
                        "Return": st.column_config.NumberColumn(format="$%.2f"),
                        "PnL":    st.column_config.NumberColumn(format="%+.2f"),
                        "ROI %":  st.column_config.NumberColumn(format="%+.1f%%"),
                    },
                )

            # Equity curve (cumulative PnL by settled_at) — richer Altair view
            settled_sorted = sorted(
                [r for r in filt if r.get("status") == "settled"],
                key=lambda r: (r.get("meeting_date", ""),
                               int(r.get("race_number") or 0),
                               r.get("settled_at", "")),
            )
            if settled_sorted:
                import pandas as _pd
                try:
                    import altair as _alt
                except ImportError:
                    _alt = None
                # ── View toggle: per-bet (every bet = one dot) or per race-day ──
                view_mode = st.radio(
                    "View",
                    ["Per race day", "Per bet"],
                    index=0, horizontal=True, key="mb_curve_view",
                    help="Per race day aggregates all bets from a meeting "
                         "into a single point — much less visual noise.",
                )
                cum = 0.0
                cum_stake = 0.0
                curve = []
                if view_mode == "Per race day":
                    by_day: dict[str, dict] = {}
                    for r in settled_sorted:
                        md = r.get("meeting_date", "")
                        d = by_day.setdefault(md, {
                            "stake": 0.0, "pnl": 0.0, "ret": 0.0,
                            "n_bets": 0, "n_hits": 0,
                        })
                        d["stake"] += float(r.get("stake_hkd", 0))
                        d["pnl"]   += float(r.get("pnl_hkd", 0))
                        d["ret"]   += float(r.get("return_hkd", 0))
                        d["n_bets"] += 1
                        if r.get("hit"):
                            d["n_hits"] += 1
                    for _i, md in enumerate(sorted(by_day.keys()), 1):
                        d = by_day[md]
                        cum += d["pnl"]
                        cum_stake += d["stake"]
                        _md_iso = (f"{md[:4]}-{md[4:6]}-{md[6:]}"
                                   if len(md) == 8 else md)
                        curve.append({
                            "#":          _i,
                            "Date":       _md_iso,
                            "Race":       d["n_bets"],
                            "Type":       f"{d['n_hits']}/{d['n_bets']} hits",
                            "Stake":      d["stake"],
                            "PnL":        d["pnl"],
                            "Cumulative": round(cum, 2),
                            "Cum Stake":  round(cum_stake, 2),
                            "Cum ROI %":  (round(cum / cum_stake * 100, 2)
                                           if cum_stake else 0.0),
                            "Hit":        d["n_hits"] > 0,
                        })
                else:
                    for _i, r in enumerate(settled_sorted, 1):
                        _pnl = float(r.get("pnl_hkd", 0))
                        _stake = float(r.get("stake_hkd", 0))
                        cum += _pnl
                        cum_stake += _stake
                        _md = r.get("meeting_date", "")
                        _md_iso = (f"{_md[:4]}-{_md[4:6]}-{_md[6:]}"
                                   if len(_md) == 8 else _md)
                        curve.append({
                            "#":             _i,
                            "Date":          _md_iso,
                            "Race":          int(r.get("race_number") or 0),
                            "Type":          r.get("bet_type", ""),
                            "Stake":         _stake,
                            "PnL":           _pnl,
                            "Cumulative":    round(cum, 2),
                            "Cum Stake":     round(cum_stake, 2),
                            "Cum ROI %":     (round(cum / cum_stake * 100, 2)
                                              if cum_stake else 0.0),
                            "Hit":           bool(r.get("hit")),
                        })
                df_c = _pd.DataFrame(curve)

                st.markdown("#### Cumulative PnL")
                if _alt is None:
                    st.line_chart(df_c.set_index("#")["Cumulative"],
                                  width='stretch')
                else:
                    df_c["Cum$"] = df_c["Cumulative"].astype(float)
                    df_c["Color"] = df_c["PnL"].apply(
                        lambda v: "win" if v > 0 else ("loss" if v < 0 else "push")
                    )
                    base = _alt.Chart(df_c).encode(
                        x=_alt.X("#:Q", title="Bet # (chronological)",
                                 axis=_alt.Axis(grid=False)),
                    )
                    # Smoothed curve via cardinal interpolation; zero baseline
                    zero_rule = _alt.Chart(
                        _pd.DataFrame({"y": [0]})
                    ).mark_rule(
                        color="#64748b", strokeDash=[4, 3], opacity=0.6,
                    ).encode(y="y:Q")
                    area = base.mark_area(
                        interpolate="monotone",
                        line={"color": "#60a5fa", "strokeWidth": 2.4},
                        color=_alt.Gradient(
                            gradient="linear",
                            stops=[
                                _alt.GradientStop(color="#60a5fa", offset=0),
                                _alt.GradientStop(color="#0b1220", offset=1),
                            ],
                            x1=1, x2=1, y1=1, y2=0,
                        ),
                        opacity=0.55,
                    ).encode(
                        y=_alt.Y("Cum$:Q", title="Cumulative PnL ($)"),
                        tooltip=[
                            _alt.Tooltip("#:Q", title="Bet #"),
                            "Date", "Race", "Type",
                            _alt.Tooltip("Stake:Q", format="$.0f"),
                            _alt.Tooltip("PnL:Q", format="+$.2f"),
                            _alt.Tooltip("Cum$:Q", title="Cumulative",
                                         format="+$.2f"),
                            _alt.Tooltip("Cum ROI %:Q", format="+.2f"),
                        ],
                    )
                    pts = base.mark_circle(size=70, opacity=0.95).encode(
                        y="Cum$:Q",
                        color=_alt.Color(
                            "Color:N",
                            scale=_alt.Scale(
                                domain=["win", "push", "loss"],
                                range=["#22c55e", "#a3b3c7", "#ef4444"],
                            ),
                            legend=_alt.Legend(title="Bet outcome",
                                               orient="top"),
                        ),
                        tooltip=[
                            _alt.Tooltip("#:Q", title="Bet #"),
                            "Date", "Race", "Type",
                            _alt.Tooltip("Stake:Q", format="$.0f"),
                            _alt.Tooltip("PnL:Q", format="+$.2f"),
                            _alt.Tooltip("Cum$:Q", title="Cumulative",
                                         format="+$.2f"),
                            _alt.Tooltip("Cum ROI %:Q", format="+.2f"),
                        ],
                    )
                    chart = (zero_rule + area + pts).properties(
                        height=320,
                    ).configure_view(strokeWidth=0)
                    st.altair_chart(chart, width='stretch')

                    # Per-bet PnL bar chart (red/green) for context.
                    df_b = df_c.copy()
                    bars = _alt.Chart(df_b).mark_bar(
                        cornerRadiusTopLeft=2, cornerRadiusTopRight=2,
                    ).encode(
                        x=_alt.X("#:Q", title="Bet #",
                                 axis=_alt.Axis(grid=False)),
                        y=_alt.Y("PnL:Q", title="Per-bet PnL ($)"),
                        color=_alt.Color(
                            "Color:N",
                            scale=_alt.Scale(
                                domain=["win", "push", "loss"],
                                range=["#22c55e", "#a3b3c7", "#ef4444"],
                            ),
                            legend=None,
                        ),
                        tooltip=[
                            _alt.Tooltip("#:Q", title="Bet #"),
                            "Date", "Race", "Type",
                            _alt.Tooltip("Stake:Q", format="$.0f"),
                            _alt.Tooltip("PnL:Q", format="+$.2f"),
                        ],
                    ).properties(height=180).configure_view(strokeWidth=0)
                    st.altair_chart(bars, width='stretch')

                    # Daily aggregation (clearer signal than per-bet noise).
                    if df_c["Date"].nunique() >= 2:
                        daily = (
                            df_c.groupby("Date", as_index=False)
                                .agg(PnL=("PnL", "sum"),
                                     Stake=("Stake", "sum"),
                                     Bets=("PnL", "size"))
                                .sort_values("Date")
                        )
                        daily["Cum$"] = daily["PnL"].cumsum()
                        daily["ROI %"] = (daily["PnL"] / daily["Stake"] * 100).round(2)
                        daily["Color"] = daily["PnL"].apply(
                            lambda v: "win" if v > 0
                            else ("loss" if v < 0 else "push")
                        )
                        st.markdown("#### Daily PnL")
                        daily_bars = _alt.Chart(daily).mark_bar(
                            cornerRadiusTopLeft=3, cornerRadiusTopRight=3,
                        ).encode(
                            x=_alt.X("Date:N", title="Meeting date",
                                     axis=_alt.Axis(labelAngle=-30)),
                            y=_alt.Y("PnL:Q", title="Daily PnL ($)"),
                            color=_alt.Color(
                                "Color:N",
                                scale=_alt.Scale(
                                    domain=["win", "push", "loss"],
                                    range=["#22c55e", "#a3b3c7", "#ef4444"],
                                ),
                                legend=None,
                            ),
                            tooltip=[
                                "Date",
                                _alt.Tooltip("Bets:Q", format="d"),
                                _alt.Tooltip("Stake:Q", format="$.0f"),
                                _alt.Tooltip("PnL:Q", format="+$.2f"),
                                _alt.Tooltip("ROI %:Q", format="+.2f"),
                                _alt.Tooltip("Cum$:Q", title="Cumulative",
                                             format="+$.2f"),
                            ],
                        ).properties(height=200).configure_view(strokeWidth=0)
                        st.altair_chart(daily_bars, width='stretch')

    # ── TAB 5 — Calendar (month/day W-L heatmap) ───────────────────────
    with tabs[4]:
        st.markdown("### Race-day calendar")
        st.caption(
            "Each cell is one calendar day. Colour = net PnL on bets that "
            "settled for that meeting. Hover for bet count, stake, and "
            "hit-rate. Use the toggle to switch between a month grid and "
            "a chronological day list."
        )
        settled_cal = [r for r in rows if r.get("status") == "settled"]
        if not settled_cal:
            st.info("No settled bets yet — calendar will populate as "
                    "meetings settle.")
        else:
            import datetime as _dt2
            try:
                import altair as _alt2
            except ImportError:
                _alt2 = None

            # Build daily aggregate keyed by ISO date.
            day_agg: dict[str, dict] = {}
            for r in settled_cal:
                md = (r.get("meeting_date") or "").strip()
                if len(md) != 8:
                    continue
                iso = f"{md[:4]}-{md[4:6]}-{md[6:]}"
                d = day_agg.setdefault(iso, {
                    "stake": 0.0, "pnl": 0.0, "ret": 0.0,
                    "n_bets": 0, "n_hits": 0,
                })
                d["stake"]  += float(r.get("stake_hkd") or 0)
                d["pnl"]    += float(r.get("pnl_hkd") or 0)
                d["ret"]    += float(r.get("return_hkd") or 0)
                d["n_bets"] += 1
                if r.get("hit"):
                    d["n_hits"] += 1

            view_mode = st.radio(
                "View",
                ["Month grid", "Day list"],
                index=0, horizontal=True, key="mb_cal_view",
            )

            iso_dates = sorted(day_agg.keys())
            month_options = sorted({d[:7] for d in iso_dates}, reverse=True)

            if view_mode == "Month grid" and month_options:
                msel = st.selectbox(
                    "Month", month_options, index=0,
                    key="mb_cal_month",
                    help="Months with at least one settled bet.",
                )
                # Build full calendar grid for that month.
                yr, mo = int(msel[:4]), int(msel[5:7])
                first = _dt2.date(yr, mo, 1)
                if mo == 12:
                    next_first = _dt2.date(yr + 1, 1, 1)
                else:
                    next_first = _dt2.date(yr, mo + 1, 1)
                ndays = (next_first - first).days
                # Monday-start week index within the month so the grid
                # aligns with HK convention.
                cells = []
                for i in range(ndays):
                    d = first + _dt2.timedelta(days=i)
                    iso = d.isoformat()
                    info = day_agg.get(iso)
                    pnl = info["pnl"] if info else 0.0
                    has_bets = info is not None
                    # Week-of-month (rows): based on Monday-start weeks.
                    days_from_mon = (first.weekday() + i) // 7
                    cells.append({
                        "Date":  iso,
                        "Day":   d.day,
                        "DOW":   d.strftime("%a"),
                        "DOWi":  d.weekday(),       # 0=Mon..6=Sun
                        "Week":  days_from_mon,
                        "PnL":   pnl,
                        "Stake": info["stake"] if info else 0.0,
                        "Bets":  info["n_bets"] if info else 0,
                        "Hits":  info["n_hits"] if info else 0,
                        "Has":   has_bets,
                        "HitRate": (info["n_hits"]/info["n_bets"]*100)
                                   if (info and info["n_bets"]) else 0.0,
                    })
                df_cal = pd.DataFrame(cells)

                if _alt2 is None:
                    st.dataframe(df_cal[df_cal["Has"]], hide_index=True,
                                 width='stretch')
                else:
                    pnl_max = max(
                        abs(df_cal[df_cal["Has"]]["PnL"].max() or 0.0),
                        abs(df_cal[df_cal["Has"]]["PnL"].min() or 0.0),
                        1.0,
                    )
                    dow_labels = ["Mon", "Tue", "Wed", "Thu",
                                  "Fri", "Sat", "Sun"]
                    base = _alt2.Chart(df_cal).encode(
                        x=_alt2.X(
                            "DOW:N",
                            sort=dow_labels,
                            title=None,
                            axis=_alt2.Axis(orient="top", labelAngle=0,
                                             labelFontSize=13,
                                             labelFontWeight="bold"),
                            scale=_alt2.Scale(paddingInner=0.04,
                                              paddingOuter=0.02),
                        ),
                        y=_alt2.Y(
                            "Week:O",
                            title=None,
                            axis=_alt2.Axis(labels=False, ticks=False),
                            sort="ascending",
                            scale=_alt2.Scale(paddingInner=0.04,
                                              paddingOuter=0.02),
                        ),
                    )
                    # Diverging red→neutral→green PnL fill, neutral grey
                    # for days with no bets.
                    cells_chart = base.mark_rect(
                        stroke="#0b1220", strokeWidth=2,
                        cornerRadius=6,
                    ).encode(
                        color=_alt2.condition(
                            "datum.Has",
                            _alt2.Color(
                                "PnL:Q",
                                scale=_alt2.Scale(
                                    domain=[-pnl_max, 0, pnl_max],
                                    range=["#ef4444", "#1f2937", "#22c55e"],
                                ),
                                legend=_alt2.Legend(
                                    title="Daily PnL ($)",
                                    orient="bottom",
                                ),
                            ),
                            _alt2.value("#0f172a"),
                        ),
                        tooltip=[
                            _alt2.Tooltip("Date:N"),
                            _alt2.Tooltip("Bets:Q", format="d"),
                            _alt2.Tooltip("Hits:Q", format="d"),
                            _alt2.Tooltip("HitRate:Q",
                                          title="Hit %",
                                          format=".1f"),
                            _alt2.Tooltip("Stake:Q", format="$.0f"),
                            _alt2.Tooltip("PnL:Q",
                                          title="PnL",
                                          format="+$.2f"),
                        ],
                    )
                    # Day-of-month label.
                    day_text = base.mark_text(
                        baseline="top", align="left",
                        dx=-38, dy=-32,
                        fontSize=14, fontWeight="bold",
                        color="#cbd5e1",
                    ).encode(text="Day:Q")
                    # PnL annotation for days with bets.
                    pnl_text = _alt2.Chart(df_cal[df_cal["Has"]]).mark_text(
                        fontSize=15, fontWeight="bold", color="white",
                    ).encode(
                        x=_alt2.X("DOW:N", sort=dow_labels),
                        y=_alt2.Y("Week:O", sort="ascending"),
                        text=_alt2.Text("PnL:Q", format="+$.0f"),
                    )
                    n_weeks = int(df_cal["Week"].max()) + 1
                    cal = (cells_chart + day_text + pnl_text).properties(
                        height=max(280, 95 * n_weeks),
                    ).configure_view(strokeWidth=0)
                    st.altair_chart(cal, width='stretch')

                # Month totals strip.
                month_rows = [c for c in cells if c["Has"]]
                if month_rows:
                    m_pnl   = sum(c["PnL"]   for c in month_rows)
                    m_stake = sum(c["Stake"] for c in month_rows)
                    m_bets  = sum(c["Bets"]  for c in month_rows)
                    m_hits  = sum(c["Hits"]  for c in month_rows)
                    m_w     = sum(1 for c in month_rows if c["PnL"] > 0)
                    m_l     = sum(1 for c in month_rows if c["PnL"] < 0)
                    m1, m2, m3, m4, m5 = st.columns(5)
                    m1.metric("Meetings", len(month_rows),
                              delta=f"{m_w}W / {m_l}L")
                    m2.metric("Bets", m_bets,
                              delta=(f"{m_hits/m_bets*100:.1f}% hit"
                                     if m_bets else None))
                    m3.metric("Stake", f"${m_stake:.0f}")
                    m4.metric("PnL", f"{m_pnl:+.2f}")
                    m5.metric("ROI",
                              f"{m_pnl/m_stake*100:+.1f}%"
                              if m_stake else "—")
            else:
                # Day-list view (chronological).
                rows_dl = []
                cum = 0.0
                for iso in iso_dates:
                    info = day_agg[iso]
                    cum += info["pnl"]
                    rows_dl.append({
                        "Date":     iso,
                        "Bets":     info["n_bets"],
                        "Hits":     info["n_hits"],
                        "Hit %":    (info["n_hits"]/info["n_bets"]*100)
                                    if info["n_bets"] else 0.0,
                        "Stake":    info["stake"],
                        "PnL":      info["pnl"],
                        "Cum PnL":  round(cum, 2),
                        "Outcome":  "WIN" if info["pnl"] > 0 else
                                    ("LOSS" if info["pnl"] < 0 else "PUSH"),
                    })
                st.dataframe(
                    pd.DataFrame(rows_dl), hide_index=True,
                    width='stretch',
                    column_config={
                        "Bets":    st.column_config.NumberColumn(format="%d"),
                        "Hits":    st.column_config.NumberColumn(format="%d"),
                        "Hit %":   st.column_config.NumberColumn(format="%.1f%%"),
                        "Stake":   st.column_config.NumberColumn(format="$%.0f"),
                        "PnL":     st.column_config.NumberColumn(format="%+.2f"),
                        "Cum PnL": st.column_config.NumberColumn(format="%+.2f"),
                    },
                )

    # ── TAB 6 — vs Model (ROI comparison & deviation notes) ─────────────
    with tabs[5]:
        st.markdown("### Your bets vs the model")
        st.caption(
            "For each user bet we load that meeting's race_day_report (v4.4). "
            "We compare your selections to the model's top picks for that "
            "race and compute ROI on both sides. Helps answer: *am I "
            "out-performing the model when I deviate, or not?*"
        )
        settled = [r for r in rows if r.get("status") == "settled"]
        if not settled:
            st.info("No settled bets yet. Come back after meetings settle.")
        else:
            # Load model reports cache
            import json as _json
            reports_by_date: dict[str, dict] = {}
            sarr_by_date: dict[str, dict] = {}
            for r in settled:
                d = r.get("meeting_date")
                if not d:
                    continue
                if d not in reports_by_date:
                    p = BASE / "reports" / f"race_day_report_{d}_v4.4.json"
                    if p.exists():
                        try:
                            reports_by_date[d] = _json.loads(
                                p.read_text(encoding="utf-8"))
                        except Exception:
                            pass
                if d not in sarr_by_date:
                    ps = BASE / "reports" / f"race_day_report_{d}_SARR.json"
                    if ps.exists():
                        try:
                            sarr_by_date[d] = _json.loads(
                                ps.read_text(encoding="utf-8"))
                        except Exception:
                            pass

            def _top_picks(rep: dict, race_no: int) -> list[dict]:
                """Return picks list sorted by rank for that race."""
                for race in rep.get("races", []):
                    if race.get("race_number") == race_no:
                        picks = race.get("picks") or []
                        picks_sorted = sorted(
                            picks, key=lambda p: p.get("rank", 99))
                        return picks_sorted
                return []

            # Build comparison rows (ET + SARR side-by-side)
            rows_cmp = []
            for b in settled:
                rep = reports_by_date.get(b.get("meeting_date"))
                sarr_rep = sarr_by_date.get(b.get("meeting_date"))
                if not rep and not sarr_rep:
                    continue
                rn = int(b.get("race_number") or 0)
                et_picks = _top_picks(rep, rn) if rep else []
                sa_picks = _top_picks(sarr_rep, rn) if sarr_rep else []
                et_top = [int(p.get("horse_no") or 0) for p in et_picks[:3]
                          if p.get("horse_no")]
                sa_top = [int(p.get("horse_no") or 0) for p in sa_picks[:3]
                          if p.get("horse_no")]
                sels_set = set(int(x) for x in (b.get("selections") or []))
                if b.get("banker"):
                    sels_set.add(int(b["banker"]))
                et_overlap = len(sels_set & set(et_top))
                sa_overlap = len(sels_set & set(sa_top))
                et_dev = len(sels_set - set(et_top)) if et_top else None
                sa_dev = len(sels_set - set(sa_top)) if sa_top else None
                # Aligned with BOTH models = strongest signal
                both_top = set(et_top) & set(sa_top)
                ensemble_dev = (len(sels_set - both_top)
                                if both_top else None)
                user_pnl = float(b.get("pnl_hkd") or 0)
                user_stake = float(b.get("stake_hkd") or 0)
                rows_cmp.append({
                    "Date":        b["meeting_date"],
                    "Venue":       b["venue"],
                    "R":           rn,
                    "Bet":         b["bet_type"],
                    "Your sel":    ",".join(str(x) for x in sorted(sels_set)),
                    "ET top3":     ",".join(str(x) for x in et_top),
                    "SARR top3":   ",".join(str(x) for x in sa_top),
                    "ET ∩ SARR":   ",".join(str(x) for x in sorted(both_top)),
                    "ET ov":       et_overlap,
                    "SARR ov":     sa_overlap,
                    "ET dev":      et_dev,
                    "SARR dev":    sa_dev,
                    "Ens dev":     ensemble_dev,
                    "Stake $":     user_stake,
                    "Your PnL $":  round(user_pnl, 2),
                    "ROI %":       ((user_pnl / user_stake * 100)
                                    if user_stake else 0),
                })
            if not rows_cmp:
                st.warning("No model reports (ET or SARR) found for your "
                           "settled meetings — nothing to compare.")
            else:
                df_cmp = pd.DataFrame(rows_cmp)
                # Aggregate by alignment vs each model
                def _bucket(dev) -> str:
                    if dev is None:
                        return "— no report"
                    if dev == 0:
                        return "🟢 fully aligned"
                    if dev == 1:
                        return "🟡 1 off"
                    return "🔴 deviated ≥2"

                colL, colR = st.columns(2)
                for label, dev_col, container in (
                        ("vs ET",   "ET dev",   colL),
                        ("vs SARR", "SARR dev", colR)):
                    sub = df_cmp.copy()
                    sub["Alignment"] = sub[dev_col].map(_bucket)
                    agg = (sub.groupby("Alignment")
                           .agg(Bets=("Your PnL $", "size"),
                                Stake=("Stake $", "sum"),
                                PnL=("Your PnL $", "sum"))
                           .reset_index())
                    agg["ROI %"] = (agg["PnL"] / agg["Stake"].replace(
                        0, pd.NA) * 100).round(1)
                    with container:
                        st.markdown(f"#### Alignment — {label}")
                        st.dataframe(
                            agg, hide_index=True, width='stretch',
                            column_config={
                                "Bets":  st.column_config.NumberColumn(format="%d"),
                                "Stake": st.column_config.NumberColumn(format="$%.0f"),
                                "PnL":   st.column_config.NumberColumn(format="%+.2f"),
                                "ROI %": st.column_config.NumberColumn(format="%+.1f%%"),
                            },
                        )

                # Ensemble bucket — bets where ET ∩ SARR agree
                st.markdown("#### 🤝 Ensemble bucket (when BOTH models agree)")
                sub_e = df_cmp[df_cmp["Ens dev"].notna()].copy()
                if sub_e.empty:
                    st.caption("No overlapping-top3 races across models yet.")
                else:
                    sub_e["Alignment"] = sub_e["Ens dev"].map(_bucket)
                    agg_e = (sub_e.groupby("Alignment")
                             .agg(Bets=("Your PnL $", "size"),
                                  Stake=("Stake $", "sum"),
                                  PnL=("Your PnL $", "sum"))
                             .reset_index())
                    agg_e["ROI %"] = (agg_e["PnL"] / agg_e["Stake"].replace(
                        0, pd.NA) * 100).round(1)
                    st.dataframe(
                        agg_e, hide_index=True, width='stretch',
                        column_config={
                            "Bets":  st.column_config.NumberColumn(format="%d"),
                            "Stake": st.column_config.NumberColumn(format="$%.0f"),
                            "PnL":   st.column_config.NumberColumn(format="%+.2f"),
                            "ROI %": st.column_config.NumberColumn(format="%+.1f%%"),
                        },
                    )

                st.markdown("#### Per-bet comparison")
                st.dataframe(
                    df_cmp.sort_values(["Date", "R"]),
                    hide_index=True, width='stretch',
                    column_config={
                        "Stake $":    st.column_config.NumberColumn(format="$%.0f"),
                        "Your PnL $": st.column_config.NumberColumn(format="%+.2f"),
                        "ROI %":      st.column_config.NumberColumn(format="%+.1f%%"),
                    },
                )

                # Best deviations (hits) / worst deviations (misses) — vs ET
                deviated = df_cmp[(df_cmp["ET dev"].fillna(0) > 0) |
                                  (df_cmp["SARR dev"].fillna(0) > 0)]
                if not deviated.empty:
                    wins = deviated[deviated["Your PnL $"] > 0].nlargest(
                        5, "Your PnL $")
                    losses = deviated[deviated["Your PnL $"] < 0].nsmallest(
                        5, "Your PnL $")
                    c1, c2 = st.columns(2)
                    with c1:
                        st.markdown("##### 🎯 Deviated & hit (top 5)")
                        if wins.empty:
                            st.caption("None yet.")
                        else:
                            st.dataframe(
                                wins[["Date", "R", "Bet", "Your sel",
                                      "ET top3", "SARR top3", "Your PnL $"]],
                                hide_index=True, width='stretch',
                            )
                    with c2:
                        st.markdown("##### ⚠️ Deviated & missed (bottom 5)")
                        if losses.empty:
                            st.caption("None yet.")
                        else:
                            st.dataframe(
                                losses[["Date", "R", "Bet", "Your sel",
                                        "ET top3", "SARR top3", "Your PnL $"]],
                                hide_index=True, width='stretch',
                            )

                st.markdown(
                    "**How to read this:** compare your ROI across 🟢 / 🟡 / 🔴 "
                    "buckets for *each* model. If SARR-aligned bets out-earn "
                    "ET-aligned bets (or vice versa), defer to that model. "
                    "The **🤝 Ensemble bucket** is the strongest signal — "
                    "your ROI when your picks match the ET ∩ SARR overlap."
                )

    # ── TAB 7 — Forensic Analyzer (brutally honest period review) ──────
    with tabs[6]:
        _render_forensic_analyzer_tab(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Forensic Analyzer — period-scoped, ruthless performance review
# ─────────────────────────────────────────────────────────────────────────────
def _render_forensic_analyzer_tab(rows: list[dict]) -> None:
    """Brutally-honest, data-driven review of bet performance over a period.

    Wraps ``bet_analyzer.analyze_betting_period`` with a Streamlit UI:
    date range + presets, bet-type filter, full-report / weaknesses-only
    toggle, CSV + markdown export. Designed for recurring use without
    needing to invoke the agent each time.
    """
    import datetime as _dt
    import bet_analyzer as ba

    st.markdown("### 🔬 Forensic period review")
    st.caption(
        "No hype, no padding. Pick a window — the analyzer ranks weaknesses "
        "by P&L impact, identifies repeatable strengths, audits your model "
        "overrides, and emits a written rule per leak. Run this weekly."
    )

    if not rows:
        st.info("No bets logged yet. Submit some bets first.")
        return

    settled_dates = sorted(
        [r["meeting_date"] for r in rows if r.get("status") == "settled"
         and r.get("meeting_date")]
    )
    if not settled_dates:
        st.info("No settled bets yet — the analyzer needs settled rows "
                "to compute P&L.")
        return

    # ── Preset window picker ─────────────────────────────────────────
    today = _dt.date.today()
    presets = {
        "Last 7 days":  today - _dt.timedelta(days=7),
        "Last 30 days": today - _dt.timedelta(days=30),
        "Last 90 days": today - _dt.timedelta(days=90),
        "Season (Sep→)": _dt.date(today.year if today.month >= 9
                                  else today.year - 1, 9, 1),
        "All time":     _dt.datetime.strptime(settled_dates[0],
                                              "%Y%m%d").date(),
        "Custom":       None,
    }
    c1, c2, c3 = st.columns([1.4, 1, 1])
    preset = c1.selectbox("Window preset",
                          list(presets.keys()), index=1,
                          key="forensic_preset")
    if preset == "Custom":
        default_start = today - _dt.timedelta(days=30)
        start = c2.date_input("Start", value=default_start,
                              key="forensic_start")
        end = c3.date_input("End", value=today, key="forensic_end")
    else:
        start = presets[preset]
        end = today
        c2.metric("Start", start.isoformat())
        c3.metric("End", end.isoformat())

    # ── Filters ──────────────────────────────────────────────────────
    all_types = sorted({r.get("bet_type", "").upper() for r in rows
                        if r.get("bet_type")})
    f1, f2 = st.columns([2, 1])
    type_filter = f1.multiselect(
        "Filter by bet type (empty = all)", all_types, default=[],
        key="forensic_type_filter",
    )
    view_mode = f2.radio("View", ["Full report", "Weaknesses only"],
                         horizontal=True, key="forensic_view")

    if not st.button("▶ Run analysis", type="primary",
                     key="forensic_run", width='stretch'):
        st.caption("Configure the window above and click **Run analysis**.")
        return

    try:
        report = ba.analyze_betting_period(
            rows, start, end,
            bet_type_filter=type_filter or None,
        )
    except ValueError as e:
        st.error(str(e))
        return

    j = report["json"]
    md = report["markdown"]

    if j["n_settled"] == 0:
        st.warning("No settled bets in this window with the chosen filters.")
        return

    # ── Snapshot strip ───────────────────────────────────────────────
    snap = j["snapshot"]
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Bets", j["n_settled"])
    m2.metric("Hit rate", f"{snap['win_rate']*100:.1f}%")
    m3.metric("ROI", f"{snap['roi']*100:+.1f}%")
    m4.metric("Net P&L", f"${snap['pnl']:+,.0f}")
    m5.metric("Avg stake", f"${snap['avg_stake']:.0f}")

    # ── Weaknesses (always shown) ───────────────────────────────────
    st.markdown("### 🩸 Weaknesses (ranked by P&L impact)")
    if not j["weaknesses"]:
        st.success("No statistically meaningful leaks detected in this "
                   "window. Either you are disciplined or the sample is "
                   "too small to flag them. Keep logging.")
    else:
        for i, w in enumerate(j["weaknesses"], 1):
            with st.container(border=True):
                st.markdown(f"**{i}. {w['title']}**")
                st.markdown(f"- **Damage:** {w['quantified']}")
                st.markdown(f"- **Likely cause:** {w['cause']}")
                st.markdown(f"- **Rule to enforce:** `{w['rule']}`")

    if view_mode == "Weaknesses only":
        st.markdown("---")
        st.markdown("### 📜 Verdict")
        st.markdown(f"> {j['verdict']}")
    else:
        # ── Strengths ──────────────────────────────────────────────
        st.markdown("### 💪 Strengths")
        if not j["strengths"]:
            st.info("No statistically convincing strengths in this window. "
                    "Until one emerges, treat every bet as if you have "
                    "no edge — flat-stake the model picks only.")
        else:
            for x in j["strengths"]:
                st.markdown(f"- **{x['title']}** — {x['evidence']}")

        # ── Model alignment audit ──────────────────────────────────
        st.markdown("### 🆚 Model alignment audit")
        a = j["model_alignment"]
        if a["n_total_with_report"] == 0:
            st.caption("No cached model reports for the bets in this range.")
        else:
            align_df = pd.DataFrame([
                {"Bucket": "ET ∩ SARR overlap", **a["aligned_both"]},
                {"Bucket": "ET or SARR (either)", **a["aligned_et_or_sarr"]},
                {"Bucket": "Deviated from both", **a["deviated"]},
                {"Bucket": "No model report", **a["no_report"]},
            ])
            align_df = align_df.rename(columns={
                "n": "Bets", "hit_rate": "Hit %",
                "stake": "Stake", "pnl": "PnL", "roi": "ROI",
            })
            align_df["Hit %"] = (align_df["Hit %"] * 100).round(1)
            align_df["ROI"] = (align_df["ROI"] * 100).round(1)
            st.dataframe(
                align_df, hide_index=True, width='stretch',
                column_config={
                    "Bets":  st.column_config.NumberColumn(format="%d"),
                    "Stake": st.column_config.NumberColumn(format="$%.0f"),
                    "PnL":   st.column_config.NumberColumn(format="$%+,.0f"),
                    "ROI":   st.column_config.NumberColumn(format="%+.1f%%"),
                    "Hit %": st.column_config.NumberColumn(format="%.1f%%"),
                },
            )

        # ── Breakdown tables ───────────────────────────────────────
        bcol1, bcol2 = st.columns(2)
        with bcol1:
            st.markdown("#### By bet type")
            bt_df = pd.DataFrame(j["by_bet_type"])
            if not bt_df.empty:
                bt_df = bt_df.rename(columns={
                    "bet_type": "Type", "n": "Bets",
                    "hit_rate": "Hit %", "stake": "Stake",
                    "pnl": "PnL", "roi": "ROI",
                })
                bt_df["Hit %"] = (bt_df["Hit %"] * 100).round(1)
                bt_df["ROI"] = (bt_df["ROI"] * 100).round(1)
                st.dataframe(
                    bt_df, hide_index=True, width='stretch',
                    column_config={
                        "Stake": st.column_config.NumberColumn(format="$%.0f"),
                        "PnL":   st.column_config.NumberColumn(format="$%+,.0f"),
                        "ROI":   st.column_config.NumberColumn(format="%+.1f%%"),
                        "Hit %": st.column_config.NumberColumn(format="%.1f%%"),
                    },
                )
        with bcol2:
            st.markdown("#### By payout band (winners only)")
            st.caption("Bets are bucketed by the payout multiple of HITS. "
                       "Losers all sit in the `lost` row. Once pre-race odds "
                       "are tracked, this becomes a true implied-odds edge "
                       "view.")
            od_df = pd.DataFrame(j["by_odds_bucket"])
            if not od_df.empty:
                od_df = od_df.rename(columns={
                    "bucket": "Band", "n": "Bets",
                    "hit_rate": "Hit %", "stake": "Stake",
                    "pnl": "PnL", "roi": "ROI",
                })
                od_df["Hit %"] = (od_df["Hit %"] * 100).round(1)
                od_df["ROI"] = (od_df["ROI"] * 100).round(1)
                st.dataframe(
                    od_df, hide_index=True, width='stretch',
                    column_config={
                        "Stake": st.column_config.NumberColumn(format="$%.0f"),
                        "PnL":   st.column_config.NumberColumn(format="$%+,.0f"),
                        "ROI":   st.column_config.NumberColumn(format="%+.1f%%"),
                        "Hit %": st.column_config.NumberColumn(format="%.1f%%"),
                    },
                )

        # ── Behaviour flags ────────────────────────────────────────
        chase = j["loss_chasing"]
        if chase.get("n_meeting_days"):
            st.markdown("#### Loss-chasing detector")
            if chase["flagged"]:
                st.error(
                    f"⚠ Flagged: {chase['n_chase_days']} of "
                    f"{chase['n_meeting_days']} meeting days showed stake "
                    "≥120% of your 7-meeting median the day after a "
                    "losing meeting."
                )
            else:
                st.success(
                    f"OK — only {chase['n_chase_days']} of "
                    f"{chase['n_meeting_days']} meeting days fit the "
                    "loss-chasing pattern."
                )
            if chase["detail"]:
                with st.expander("Detail rows"):
                    st.dataframe(pd.DataFrame(chase["detail"]),
                                 hide_index=True, width='stretch')

        # ── Verdict ────────────────────────────────────────────────
        st.markdown("### 📜 Verdict")
        st.markdown(f"> {j['verdict']}")

        # ── Data gaps ──────────────────────────────────────────────
        if j["data_gaps"]:
            with st.expander("Data gaps & caveats"):
                for g in j["data_gaps"]:
                    st.markdown(f"- {g}")

    # ── Exports ──────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 📤 Export")
    e1, e2, e3 = st.columns(3)
    fname = f"forensic_{j['period']['start']}_{j['period']['end']}"
    e1.download_button(
        "⬇ JSON", data=json.dumps(j, indent=2, default=str),
        file_name=f"{fname}.json", mime="application/json",
        key="forensic_dl_json", width='stretch',
    )
    e2.download_button(
        "⬇ Markdown", data=md, file_name=f"{fname}.md",
        mime="text/markdown", key="forensic_dl_md",
        width='stretch',
    )
    # CSV of per-bet alignment detail
    if j["model_alignment"]["detail"]:
        det_df = pd.DataFrame(j["model_alignment"]["detail"])
        e3.download_button(
            "⬇ Per-bet CSV", data=det_df.to_csv(index=False),
            file_name=f"{fname}_per_bet.csv", mime="text/csv",
            key="forensic_dl_csv", width='stretch',
        )


# ─────────────────────────────────────────────────────────────────────────────
# Model Bets page — filter-based tickets + live track record
# ─────────────────────────────────────────────────────────────────────────────
def _render_strategy_slate_tab() -> None:
    """🎯 Strategy Slate — Kelly-sized HKD bets with reasoning + stake controls.

    Reads ``decision_engine.build_meeting_slate`` for the selected meeting,
    presents per-race + all-up plans, lets the user override the bankroll,
    mode, individual stakes, and total exposure cap, and persists the chosen
    plan to ``reports/strategy_slate_<date>_<mode>.json`` for later
    settlement.
    """
    try:
        from decision_engine import build_meeting_slate, format_slate
    except Exception as exc:
        st.error(f"Could not import decision_engine: {exc}")
        return

    meetings = load_available_meetings()
    if not meetings:
        st.info("No analysed meetings found. Run a Model Analysis first.")
        return

    options = {m["title"]: m for m in meetings}
    sel_title = st.selectbox(
        "Meeting:", list(options.keys()), index=0, key="slate_meeting"
    )
    sel = options[sel_title]
    date_str = sel["date_str"]

    st.markdown(
        "**What this is:** Risk-tiered HKD bets, sized by Kelly against your "
        "bankroll. Bet types vary by mode (PLACE → QPL → WIN/QIN → all + "
        "FORECAST). A multi-leg banker is expanded into one bet per "
        "(banker, leg) pair. Override stakes below; totals recompute "
        "automatically."
    )

    cfg_cols = st.columns([1, 1, 1, 1.4])
    bankroll = cfg_cols[0].number_input(
        "Bankroll ($)", min_value=100.0, max_value=200000.0, value=2000.0,
        step=100.0, key="slate_bankroll",
    )
    mode = cfg_cols[1].selectbox(
        "Mode", ["conservative", "balanced", "aggressive", "uncapped"],
        index=1, key="slate_mode",
        help=(
            "**Bet types differ by risk tier** (lowest→highest risk: "
            "PLACE < QPL < WIN < QIN < FORECAST):\n\n"
            "• **Conservative** — PLACE on banker (1/4 Kelly)\n"
            "• **Balanced** — QPL_BANKER per (banker, leg) pair (1/2 Kelly)\n"
            "• **Aggressive** — WIN on banker + QIN_BANKER per pair (1/2 Kelly)\n"
            "• **Uncapped** — WP (W+P) + QQPL (QIN+QPL) + FORECAST per pair "
            "(full Kelly, no meeting cap, all-up satellites)\n\n"
            "All stakes scale with bankroll. A 3-leg banker emits 3 separate "
            "bets — one per (banker, leg) pair."
        ),
    )
    enable_allup = cfg_cols[2].checkbox(
        "All-up chains", value=True, key="slate_allup",
        help="Enable WIN/PLACE/QIN-banker/QPL-banker multi-leg chains.",
    )
    total_cap_pct = cfg_cols[3].slider(
        "Total exposure cap (% of bankroll)", min_value=5, max_value=100,
        value=25, step=5, key="slate_cap_pct",
        help="Hard cap on total HKD deployed across the meeting.",
    )

    cfg_cols2 = st.columns([1.5, 1.5, 3])
    force_min_stake = cfg_cols2[0].checkbox(
        "Force $10 min on every +EV pick", value=False,
        key="slate_force_min",
        help=(
            "OFF (recommended for $2-3 k bankroll): sub-$10 Kelly stakes "
            "are skipped, so the meeting cap goes to the highest-edge "
            "picks instead of being padded across many marginal bets.\n\n"
            "ON (legacy): every positive-EV pick is bumped to the $10 "
            "HKJC minimum — exhausts the meeting cap quickly."
        ),
    )
    proportional_cap = cfg_cols2[1].checkbox(
        "Proportional cap allocation", value=True,
        key="slate_prop_cap",
        help=(
            "When the sum of Kelly stakes exceeds the meeting cap, scale "
            "every accepted bet down by the same factor instead of the "
            "earlier greedy 'first-come eats the cap' behaviour. Lets "
            "every structured bet type get a fair share."
        ),
    )

    with st.spinner(f"Building slate · {sel_title} · {mode}…"):
        try:
            slate = build_meeting_slate(
                date_str, bankroll=float(bankroll), mode=mode,
                enable_allup=enable_allup,
                force_min_stake=force_min_stake,
                proportional_cap=proportional_cap,
            )
        except Exception as exc:
            st.error(f"Slate build failed: {exc}")
            import traceback
            with st.expander("Trace"):
                st.code(traceback.format_exc())
            return

    summary = slate.get("summary", {}) or {}
    races = slate.get("races", []) or []
    allup_plans = slate.get("allup_plans", []) or []

    if not races:
        reason = summary.get("reason", "no eligible races")
        st.warning(
            f"No slate generated for **{sel_title}** ({date_str}) — {reason}. "
            "This usually means the ET (v4.4) report for this meeting hasn't "
            "been built yet. Run a Model Analysis for this date first."
        )
        return

    # Apply user's exposure cap (post-build, advisory only)
    cap_hkd = bankroll * total_cap_pct / 100.0

    # ── Per-race table with editable stake column ──────────────────────────
    rows = []
    default_total = 0.0
    for r in races:
        if not r.get("accepted"):
            rows.append({
                "R": r["race_number"],
                "Play": r["play"],
                "Banker": str(r.get("banker") or ""),
                "Banker #": r.get("banker_no"),
                "Legs": ", ".join(f"#{n} {nm}" for n, nm in r.get("legs", [])),
                "Odds": None,
                "p_used": None,
                "p_mkt": None,
                "Edge %": None,
                "Stake $": 0.0,
                "Why": r.get("reason", ""),
            })
            continue
        default_total += r["stake_hkd"]
        rows.append({
            "R": r["race_number"],
            "Play": r["play"],
            "Banker": str(r.get("banker") or ""),
            "Banker #": r.get("banker_no"),
            "Legs": ", ".join(f"#{n} {nm}" for n, nm in r.get("legs", [])),
            "Odds": r.get("decimal_odds"),
            "p_used": r.get("p_used"),
            "p_mkt": r.get("p_market"),
            "Edge %": (r.get("edge_pct") or 0.0) * 100.0,
            "Stake $": float(r["stake_hkd"]),
            "Why": r.get("reason", ""),
        })

    df = pd.DataFrame(rows)
    st.markdown("#### Per-race plan")
    edited = st.data_editor(
        df, hide_index=True, width='stretch',
        key=f"slate_editor_{date_str}_{mode}",
        disabled=["R", "Play", "Banker", "Banker #", "Legs",
                  "Odds", "p_used", "p_mkt", "Edge %", "Why"],
        column_config={
            "R":       st.column_config.NumberColumn(format="%d"),
            "Banker #": st.column_config.NumberColumn(format="%d"),
            "Odds":    st.column_config.NumberColumn(format="%.2f"),
            "p_used":  st.column_config.NumberColumn(format="%.2f"),
            "p_mkt":   st.column_config.NumberColumn(format="%.2f"),
            "Edge %":  st.column_config.NumberColumn(format="%+.1f%%"),
            "Stake $": st.column_config.NumberColumn(
                format="$%.0f",
                help="Override stake — minimum $10 per HKJC; 0 = skip.",
                min_value=0.0, max_value=float(bankroll), step=10.0,
            ),
        },
    )

    user_total = float(edited["Stake $"].fillna(0.0).sum())

    # ── Apply cap warning + scale-down option ──────────────────────────────
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Bets accepted", int((edited["Stake $"] > 0).sum()))
    m2.metric("Race-stake total", f"${user_total:.0f}")
    m3.metric("Exposure cap", f"${cap_hkd:.0f}",
                 delta=f"{(user_total/cap_hkd-1)*100:+.0f}% vs cap"
                       if cap_hkd else None)
    m4.metric("Mode", mode)

    if user_total > cap_hkd:
        st.warning(
            f"⚠ Race-stake total **${user_total:.0f}** exceeds your cap "
            f"**${cap_hkd:.0f}** ({total_cap_pct}% of bankroll). "
            "Reduce individual stakes or raise the cap."
        )

    # ── All-up plans ────────────────────────────────────────────────────────
    if allup_plans:
        st.markdown("#### All-up chains")
        au_rows = []
        for i, plan in enumerate(allup_plans):
            legs_str = "  →  ".join(
                f"R{l['race']} {l['pool']} #{l['horse_no']} {l['horse_name']}"
                for l in plan["legs"]
            )
            au_rows.append({
                "#": i + 1,
                "Stake $": float(plan["stake_hkd"]),
                "Legs (n)": len(plan["legs"]),
                "p_hit": plan["chain_p_hit"],
                "Chain EV": plan["chain_ev"],
                "Exp Payout": plan["expected_payout"],
                "Exp PnL": plan["expected_pnl"],
                "Chain": legs_str,
            })
        au_df = pd.DataFrame(au_rows)
        st.dataframe(
            au_df, hide_index=True, width='stretch',
            column_config={
                "#":        st.column_config.NumberColumn(format="%d"),
                "Stake $":  st.column_config.NumberColumn(format="$%.0f"),
                "Legs (n)": st.column_config.NumberColumn(format="%d"),
                "p_hit":    st.column_config.NumberColumn(format="%.3f"),
                "Chain EV": st.column_config.NumberColumn(format="%.2f"),
                "Exp Payout": st.column_config.NumberColumn(format="$%.2f"),
                "Exp PnL":  st.column_config.NumberColumn(format="$%.2f"),
            },
        )
    else:
        st.caption("_No all-up chains generated for this slate._")

    # ── Persist ─────────────────────────────────────────────────────────────
    save_cols = st.columns([1, 1, 3])
    if save_cols[0].button("💾 Save plan", key="slate_save",
                                 width='stretch'):
        out = {
            "date": date_str, "mode": mode, "bankroll": float(bankroll),
            "exposure_cap_hkd": cap_hkd,
            "race_plan": [
                {
                    "race_number": int(row["R"]),
                    "play": row["Play"],
                    "banker_no": (int(row["Banker #"])
                                    if pd.notna(row["Banker #"]) else None),
                    "stake_hkd": float(row["Stake $"] or 0.0),
                    "reason": row["Why"],
                }
                for _, row in edited.iterrows()
                if (row["Stake $"] or 0.0) > 0
            ],
            "allup_plans": allup_plans,
            "user_total_hkd": user_total,
            "raw_summary": summary,
        }
        out_path = Path("reports") / (
            f"strategy_slate_{date_str}_{mode}.json"
        )
        out_path.parent.mkdir(exist_ok=True)
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        st.success(f"Saved → {out_path}")

    with save_cols[1]:
        with st.expander("Plain-text view"):
            st.code(format_slate(slate))

    # ── Reasoning per leg ───────────────────────────────────────────────────
    with st.expander(f"Why each bet ({len(rows)} legs)"):
        why_rows = [
            {"R": r["R"], "Play": r["Play"], "Stake $": r["Stake $"],
             "Edge %": r["Edge %"], "Reason": r["Why"]}
            for r in rows
        ]
        st.dataframe(
            pd.DataFrame(why_rows), hide_index=True,
            width='stretch',
            column_config={
                "R": st.column_config.NumberColumn(format="%d"),
                "Stake $": st.column_config.NumberColumn(format="$%.0f"),
                "Edge %":  st.column_config.NumberColumn(format="%+.1f%%"),
            },
        )

    st.caption(
        "Per-leg reasoning is the auto-generated stake-engine label. "
        "When live odds are scraped post race-card, set them on the "
        "Live Odds page and rebuild — the slate will use real market "
        "prices instead of model-implied probabilities."
    )


def _mb_confluence_maps(date_compact: str) -> dict:
    """Cross-engine agreement maps for Model Bets, keyed by (race_no, NAME).

    Pulls the rating-cycle flags (horse_cycle) and the form-screen shortlist so
    the bet table can show when independent systems back — or warn against —
    the model's banker. Cheap; results are small dicts.
    """
    cycle_map: dict = {}
    short_map: dict = {}
    place_map: dict = {}
    try:
        from horse_cycle import load_or_build_card_cycle
        payload = load_or_build_card_cycle(date_compact)
        for h in (payload or {}).get("horses", []) or []:
            rn = h.get("race_number")
            nm = str(h.get("horse_name", "")).strip().upper()
            if rn and nm:
                cycle_map[(rn, nm)] = h.get("primary_flag")
                _pf = h.get("place_form")
                if _pf:
                    place_map[(rn, nm)] = {
                        "form": _pf,
                        "top3": h.get("recent_top3"),
                        "starts": h.get("recent_starts"),
                        "score": h.get("recent_place_score"),
                    }
    except Exception:
        pass
    try:
        fs = _load_form_screen(date_compact)
        for r in (fs or {}).get("races", []) or []:
            rn = r.get("race_no")
            for item in r.get("shortlist", []) or []:
                nm = str(item.get("horse", "")).strip().upper()
                if rn and nm:
                    short_map[(rn, nm)] = True
    except Exception:
        pass
    return {"cycle": cycle_map, "short": short_map, "place": place_map}


def _mb_confluence_for(it: dict, conf_map: dict) -> dict:
    """Agreement tags for a ticket's banker.

    ``support`` = number of independent engines backing the banker;
    ``fade`` = the rating-cycle flags the banker as overrated (caution).
    """
    t = it.get("ticket") or {}
    b = t.get("banker") or {}
    rn = it.get("race_number")
    nm = str(b.get("horse_name", "")).strip().upper()
    if not nm:
        return {"tags": [], "support": 0, "fade": False}
    tags: list[str] = []
    support = 0
    fade = False
    flag = conf_map.get("cycle", {}).get((rn, nm))
    if flag == "PRIMED":
        tags.append("🌡️PRIMED"); support += 1
    elif flag == "EARLY_DROPPER":
        tags.append("🫒DROP"); support += 1
    elif flag == "FADE_OVERRATED":
        tags.append("🔴FADE"); fade = True
    elif flag == "WATCH":
        tags.append("🟡WATCH")
    if conf_map.get("short", {}).get((rn, nm)):
        tags.append("🔬form"); support += 1
    if b.get("bb_match"):
        tags.append("★BB"); support += 1
    return {"tags": tags, "support": support, "fade": fade}


def page_model_bets():
    """Filter-based betting recommendations + sustained performance tracker."""
    from betting_strategy import (build_meeting_tickets, log_meeting_picks,
                                     settle_picks_log, load_picks_log,
                                     EDGE_CFG, APRIL_DATES, V47_CFG, STRATEGY_MODE)

    st.markdown('<div class="page-title">🎯 Model Bets</div>',
                unsafe_allow_html=True)
    st.markdown(
        '<div class="page-subtitle">Filter-driven tickets &middot; Live '
        'track record &middot; Strategy sweep</div>',
        unsafe_allow_html=True,
    )

    tabs = st.tabs(
        ["📋 Today's Tickets", "🎯 Strategy Slate",
         "📈 Track Record", "🧪 Strategy Sweep", "🔧 Filter Rules"]
    )

    # ── TAB 1 — Today's / selected meeting tickets ──────────────────────────
    with tabs[0]:
        meetings = load_available_meetings()
        if not meetings:
            st.info("No analysed meetings found. Run a Model Analysis first.")
        else:
            options = {m["title"]: m for m in meetings}
            sel_title = st.selectbox("Select meeting:", list(options.keys()),
                                        index=0, key="mb_meeting")
            sel = options[sel_title]
            date_str = sel["date_str"]

            col_a, col_b = st.columns([3, 1])
            with col_b:
                log_clicked = st.button("💾 Save picks to log",
                                            key="mb_log_btn",
                                            width='stretch')
            with col_a:
                st.caption(
                    f"**{sel_title}** · {sel['n_races']} races · "
                    f"model {sel.get('model_version', '?')}"
                )

            oc1, oc2 = st.columns([2, 3])
            with oc1:
                odds_mode_label = st.radio(
                    "Odds basis",
                    ["Pre-race (live scraped)", "Review (settled SP)"],
                    horizontal=True, key="mb_odds_mode",
                    help=("Pre-race uses ONLY the live odds you scraped "
                          "(cache/live_odds). Review borrows the final SP "
                          "from the results file — that's hindsight, so only "
                          "use it to audit ROI after the fact. This is why "
                          "edges could appear 'before you scraped': a settled "
                          "meeting already carries its SP in the results JSON."),
                )
            odds_mode = ("prerace" if odds_mode_label.startswith("Pre")
                         else "review")
            with oc2:
                only_confluence = st.checkbox(
                    "⭐ Only bets with model agreement (confluence)",
                    value=False, key="mb_only_confluence",
                    help=("Show only races where the rating-cycle engine "
                          "and/or form-screen back the model's banker, and "
                          "the cycle is NOT flagging it as overrated."),
                )

            blend_market = st.checkbox(
                "🔀 Show ET + SARR + Market blend pick",
                value=False, key="mb_blend_market",
                help=("Adds a column with each race's #1 by a geometric pool "
                      "of the ET+SARR model probability and the market-implied "
                      "probability (uses the odds basis selected above). "
                      "Backtest over 16 Apr–May meetings: ET+SARR+Market top-1 "
                      "hit 60.4% PLACE / 26.4% WIN vs 54.1% / 19.5% for ET+SARR "
                      "alone. The proven v4.7 SARR banker is left unchanged — "
                      "this is an additional signal to weigh, not a replacement."),
            )

            with st.spinner("Building tickets…"):
                items = build_meeting_tickets(date_str, odds_mode=odds_mode)
            conf_map = _mb_confluence_maps(date_str)

            if log_clicked:
                n = log_meeting_picks(date_str, sel.get("venue", ""), items)
                st.success(f"Logged {n} tickets to reports/model_picks_log.jsonl")

            if not items:
                st.warning("No ET report loaded for this meeting.")
            else:
                # Odds-provenance summary so the basis is never ambiguous.
                src_counts: dict[str, int] = {}
                for it in items:
                    s = it.get("odds_source", "none")
                    src_counts[s] = src_counts.get(s, 0) + 1
                src_lbl = {"live": "🟢 live scraped",
                           "settled_sp": "🟠 settled SP (hindsight)",
                           "none": "⚪ no odds yet"}
                st.caption(
                    "Odds basis · " + " · ".join(
                        f"{src_lbl.get(k, k)}: {v}" for k, v in
                        sorted(src_counts.items())))
                n_fuse = sum(1 for it in items if it.get("fuse_used"))
                if n_fuse:
                    st.caption(
                        f"🧬 **FUSE probabilities active** for {n_fuse}/"
                        f"{len(items)} races — banker selection, edge and "
                        f"quinella/place maths use the FUSE model's calibrated "
                        f"win / top-2 / top-3 heads (Jan–Jun walk-forward: "
                        f"29.9% win, 61.4% place on top pick).")
                else:
                    st.caption(
                        "⚠ No FUSE report for this meeting — tickets fall "
                        "back to the legacy ET+SARR composite. Generate via "
                        "the Model Analysis pipeline.")
                if odds_mode == "prerace" and src_counts.get("none"):
                    st.info(
                        f"{src_counts['none']} race(s) have no live odds "
                        "scraped yet — their edge/value columns are blank "
                        "until you scrape odds. Bankers/legs still rank on "
                        "the model alone.")

                # Apply optional confluence filter.
                shown_items = items
                if only_confluence:
                    shown_items = [
                        it for it in items
                        if it["ticket"]["play"] != "SKIP"
                        and _mb_confluence_for(it, conf_map)["support"] > 0
                        and not _mb_confluence_for(it, conf_map)["fade"]
                    ]
                    if not shown_items:
                        st.warning("No bets have supporting model agreement "
                                   "for this meeting under the current filter.")

                # Build display table
                rows = []
                for it in shown_items:
                    t = it["ticket"]
                    b = t.get("banker") or {}
                    conf_sig = _mb_confluence_for(it, conf_map)
                    legs_str = ", ".join(
                        f"#{l['horse_no']} {l['horse_name']}"
                        for l in t.get("legs", [])
                    )
                    banker_str = ""
                    if b:
                        sp = b.get("win_odds")
                        edge = b.get("edge")
                        banker_str = (f"#{b['horse_no']} {b['horse_name']} "
                                        f"(p {b.get('p_model',0):.2f}"
                                        + (f", SP {sp:.1f}" if sp else "")
                                        + (f", edge {edge:.2f}" if edge else "")
                                        + ")")
                    # Flag the extras / hedge / F4 as a single marker column
                    markers = []
                    if t.get("extras"):
                        markers.append(f"💎{len(t['extras'])} value")
                    if t.get("hedge"):
                        markers.append("🛡️ hedge")
                    if t.get("f4"):
                        markers.append("⭐ F4")
                    odds_badge = {"live": "🟢 live",
                                  "settled_sp": "🟠 SP",
                                  "none": "⚪ —"}.get(
                                      it.get("odds_source", "none"), "—")
                    row = {
                        "R": it["race_number"],
                        "Class": str(it.get("race_class") or ""),
                        "Dist": it.get("distance"),
                        "Play": t["play"],
                        "Conf": t.get("confidence", "low"),
                        "Signals": " ".join(conf_sig["tags"]) or "—",
                        "Odds": odds_badge,
                        "Banker": banker_str,
                        "Legs": legs_str,
                        "Combos": t.get("n_combos", 0),
                        "Stake (u)": t.get("stake_units", 0),
                        "HKD min": t.get("stake_hkd_min", 0),
                        "Overlays": " ".join(markers),
                        "Why": t.get("reason", ""),
                    }
                    if blend_market:
                        br = next((r for r in it.get("rows", [])
                                   if r.get("blend_rank") == 1), None)
                        if br:
                            pm = br.get("p_mkt")
                            row["Blend #1"] = (
                                f"#{br['horse_no']} {br['horse_name']} "
                                f"(p {br.get('p_blend', 0):.2f}"
                                + (f", SP {pm and 1/pm:.1f}" if pm else "")
                                + ")")
                        else:
                            row["Blend #1"] = "—"
                    rows.append(row)
                df = pd.DataFrame(rows)
                # Colour-code Play column via a simple icon prefix
                play_icons = {
                    "WIN": "🟢 WIN",
                    "QIN_BANKER": "🔵 QIN",
                    "QPL_BANKER": "🟣 QPL",
                    "PLACE": "🟡 PLACE",
                    "F4_BOX_TOP5": "⭐ F4",
                    "SKIP": "⚫ SKIP",
                }
                df["Play"] = df["Play"].map(lambda p: play_icons.get(p, p))
                conf_icons = {"max": "🔥 max", "high": "🟢 high",
                                "med": "🟡 med", "low": "⚪ low"}
                df["Conf"] = df["Conf"].map(lambda c: conf_icons.get(c, c))
                st.dataframe(
                    df, hide_index=True, width='stretch',
                    column_config={
                        "R":         st.column_config.NumberColumn(format="%d"),
                        "Dist":      st.column_config.NumberColumn(format="%d"),
                        "Combos":    st.column_config.NumberColumn(format="%d"),
                        "Stake (u)": st.column_config.NumberColumn(format="%.1f"),
                        "HKD min":   st.column_config.NumberColumn(format="$%.0f"),
                    },
                )
                st.caption(
                    "**Signals** = cross-engine agreement on the banker · "
                    "🌡️PRIMED / 🫒DROP = rating-cycle backs it · "
                    "🔴FADE = cycle says overrated (caution) · "
                    "🔬form = on the form-screen shortlist · ★BB = blackbook. "
                    "**Odds**: 🟢 live scraped · 🟠 settled SP (hindsight) · ⚪ none yet."
                )

                # ── Overlays panel: Value / Hedge / F4 detail ───────────
                overlays_rows = []
                for it in shown_items:
                    t = it["ticket"]
                    rn = it["race_number"]
                    for ex in t.get("extras", []):
                        overlays_rows.append({
                            "R": rn, "Type": "💎 VALUE WIN",
                            "Detail": (f"#{ex['horse_no']} {ex['horse_name']} "
                                        f"SP {ex.get('sp') or '—'}, "
                                        f"edge {ex.get('edge')}"),
                            "Stake (u)": ex.get("stake_units", 0),
                            "Reason": ex.get("reason", ""),
                        })
                    if t.get("hedge"):
                        h = t["hedge"]
                        pair_str = " · ".join(
                            f"#{a}-{c}" for a, _, c, _ in h.get("pairs", [])
                        )
                        overlays_rows.append({
                            "R": rn, "Type": "🛡️ HEDGE",
                            "Detail": f"QIN pairs: {pair_str}",
                            "Stake (u)": h.get("stake_units", 0),
                            "Reason": h.get("reason", ""),
                        })
                    if t.get("f4"):
                        f4 = t["f4"]
                        names = ", ".join(f"#{h['horse_no']}"
                                             for h in f4.get("horses", []))
                        overlays_rows.append({
                            "R": rn, "Type": "⭐ F4 BOX",
                            "Detail": f"Top-5 box: {names}",
                            "Stake (u)": f4.get("stake_units", 0),
                            "Reason": f4.get("reason", ""),
                        })
                if overlays_rows:
                    with st.expander(f"🔍 Overlays & extras "
                                        f"({len(overlays_rows)} rows)",
                                        expanded=False):
                        st.dataframe(
                            pd.DataFrame(overlays_rows),
                            hide_index=True, width='stretch',
                            column_config={
                                "R": st.column_config.NumberColumn(format="%d"),
                                "Stake (u)": st.column_config.NumberColumn(format="%.2f"),
                            },
                        )

                non_skip = [it for it in items
                             if it["ticket"]["play"] != "SKIP"]
                st.markdown(
                    f"**{len(non_skip)} / {len(items)}** races flagged for a "
                    f"bet. Non-skip plays: "
                    f"{', '.join(sorted(set(it['ticket']['play'] for it in non_skip))) or '—'}"
                )

                with st.expander("How these picks are decided (v4.7 — SARR-QPL)",
                                   expanded=False):
                    if STRATEGY_MODE == "v47_sarr_qpl":
                        st.markdown(f"""
**Active strategy: v4.7 SARR-banker QPL** (empirically profitable on April 2026).

For every race, the model:

1. Picks **SARR top-1** as the banker (the SARR ensemble was right 4/9 on
    Apr 29 vs ET 2/9, and outperformed ET across April).
2. **Skips** the race if the SARR top-1's composite p_model is below
    **{V47_CFG['min_pmodel']:.2f}** (low conviction → wide-open scramble).
3. Plays a **QPL banker** with **{V47_CFG['n_legs']} legs** — the next
    {V47_CFG['n_legs']} runners by SARR rank (excluding the banker).
4. Stakes **{V47_CFG['stake_units']:.1f}u** total
    (= ${V47_CFG['stake_units'] * V47_CFG['hkd_per_unit']:.0f} at $10/unit),
    split equally across legs.

**8-meeting backtest (Apr 1 → Apr 29, real HKJC dividends):**

| Variant | Bets | Hit % | ROI |
|---|---|---|---|
| **→ v4.7 SARR-QPL · 4 legs · pmod≥0.18** | **43** | **58.1%** | **+11.0%** |
| SARR-QPL · 3 legs · pmod≥0.18 | 43 | 51.2% | -5.4% |
| SARR-QPL · pmod≥0.15 · 3 legs | 46 | 52.2% | -1.1% |
| Old QIN-anchored composite (v4.6) | 27 | 14.8% | -55.1% |

**Why v4.7?** The previous QIN-anchored composite was over-weighting ET
(`w_et = 0.35` vs `w_sarr = 0.25`), so when SARR strongly disagreed the
banker leaned ET — and ET was the weaker signal across April. SARR
top-1 hits the top-3 ~58% of the time, which is the natural QPL sweet spot.
The 4th leg catches winners that fall just outside SARR's top-3 (e.g.
Apr 29 R5 #8 GAMEPLAYER ELITE was SARR rank 1, not on the old ticket).

To revert: set the env var `HK_STRATEGY_MODE=legacy_qin` before launch
(falls back to the QIN-anchored composite + WIN/QPL/F4/hedge stack).
""")
                    else:
                        st.markdown(f"""
**Active strategy: legacy QIN-anchored composite (v4.6)** — set
`HK_STRATEGY_MODE=v47_sarr_qpl` to enable the empirically profitable
SARR-QPL strategy instead.

**Priority order** (first rule that fires, wins):

1. **🔵 QIN banker** — _primary_. Cls 3–5, SP {EDGE_CFG['qin_sp_min']:.0f}–{EDGE_CFG['qin_sp_max']:.0f}, p_mod ≥ {EDGE_CFG['banker_pmodel_min']:.2f}.
   - **Legs: {EDGE_CFG['legs_min']}–{EDGE_CFG['legs_max']}** chosen from gap to #2
     (`gap ≥ 0.10 → 2 legs` · `gap ≥ 0.04 → 3 legs` · `else 4 legs`).
   - **Stake: 1u base** + 1u per conviction signal
     (mutual-top3, gap ≥ 0.08, top-1 edge ≥ 1.2), capped at {EDGE_CFG['stake_cap']:.0f}u.
2. **🟢 WIN single** — Cls 3–5, SP in band, **top-1 edge ≥ 1.2** and p_mod ≥ 0.25.
   Stake 2u when edge ≥ 1.4 else 1u.
3. **🟣 QPL banker** — narrow fallback when (1)+(2) didn't fire: needs
   SARR+ET mutual-top3 AND gap ≥ {EDGE_CFG['qpl_gap_min']:.2f}.
4. **🟡 PLACE** — gap < 0.04 (low conviction safety), 0.5u.
5. **⭐ F4 box top-5** — field ≥ {EDGE_CFG['f4_field_min']}, mutual+gap ≥ {EDGE_CFG['f4_gap_min']:.2f}, p-mass ≥ {EDGE_CFG['f4_top5_mass_min']:.2f}.
6. **💎 Value overlays** — non-top runner with edge ≥ {EDGE_CFG['value_edge_min']:.2f}, 0.5u WIN.
7. **🛡️ Hedge** — banker SP < {EDGE_CFG['hedge_trigger_sp']:.1f}, longshot cover.
""")

    # ── TAB 2 — Track record from picks log ────────────────────────────────
    with tabs[2]:
        result = settle_picks_log()
        log = result["rows"]
        s = result["summary"]

        if not log:
            st.info("No logged picks yet. Use the 'Save picks to log' button "
                      "on the Tickets tab to start a track record.")
        else:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total bets", s["bets"])
            m2.metric("Hit rate",
                       f"{s['hit_rate']*100:.1f}%",
                       delta=f"{s['hits']} wins")
            m3.metric("Return", f"${s['return']:.2f}",
                       delta=f"stake ${s['stake']:.0f}")
            m4.metric("ROI", f"{s['roi']*100:+.1f}%",
                       delta=f"{s['pending']} pending" if s['pending'] else None)
            st.caption(
                "Stakes shown in **HKD** with a $10 minimum per bet "
                "(HKJC floor). Track record auto-refreshes from "
                "`reports/race_day_report_*_v4.4.json` every page load."
            )

            st.markdown("#### By filter rule")
            br_rows = []
            for k, v in s["by_filter"].items():
                roi = (v["ret"]-v["stake"])/v["stake"] if v["stake"] else 0
                hr = v["hits"]/v["bets"] if v["bets"] else 0
                br_rows.append({
                    "Filter": k,
                    "Bets": v["bets"],
                    "Hits": v["hits"],
                    "Hit %": hr * 100,
                    "Stake": v["stake"],
                    "Return": v["ret"],
                    "ROI %": roi * 100,
                })
            if br_rows:
                st.dataframe(
                    pd.DataFrame(br_rows), hide_index=True,
                    width='stretch',
                    column_config={
                        "Bets": st.column_config.NumberColumn(format="%d"),
                        "Hits": st.column_config.NumberColumn(format="%d"),
                        "Hit %":  st.column_config.NumberColumn(format="%.1f%%"),
                        "Stake":  st.column_config.NumberColumn(format="$%.1f"),
                        "Return": st.column_config.NumberColumn(format="$%.2f"),
                        "ROI %":  st.column_config.NumberColumn(format="%+.1f%%"),
                    },
                )

            # Equity curve by date
            settled = [r for r in log if r.get("status") == "settled"]
            if settled:
                st.markdown("#### Equity curve (cumulative P/L)")
                view_mode_mp = st.radio(
                    "View",
                    ["Per race day", "Per bet"],
                    index=0, horizontal=True, key="mp_curve_view",
                )
                settled_sorted = sorted(settled, key=lambda r: (r["date"], r["race_number"]))
                curve_rows = []
                if view_mode_mp == "Per race day":
                    by_day: dict[str, dict] = {}
                    for r in settled_sorted:
                        d = by_day.setdefault(r["date"],
                                                {"stake": 0.0, "ret": 0.0,
                                                 "n": 0})
                        d["stake"] += float(r.get("stake") or 0.0)
                        d["ret"]   += float(r.get("return") or 0.0)
                        d["n"]     += 1
                    cum_stake = cum_ret = 0.0
                    for d_key in sorted(by_day.keys()):
                        agg = by_day[d_key]
                        cum_stake += agg["stake"]
                        cum_ret   += agg["ret"]
                        curve_rows.append({
                            "pick": f"{d_key} ({agg['n']} bets)",
                            "cum_pnl": cum_ret - cum_stake,
                        })
                else:
                    cum_stake = cum_ret = 0.0
                    for r in settled_sorted:
                        cum_stake += r["stake"]
                        cum_ret   += r["return"]
                        curve_rows.append({
                            "pick": f"{r['date']} R{r['race_number']}",
                            "cum_pnl": cum_ret - cum_stake,
                        })
                curve_df = pd.DataFrame(curve_rows)
                st.line_chart(curve_df.set_index("pick")["cum_pnl"])

            with st.expander(f"Full log ({len(log)} rows)"):
                log_rows = []
                for r in log:
                    b_str = (f"#{r.get('banker_no')} {r.get('banker_name','')}"
                                if r.get("banker_no") else "")
                    legs = r.get("legs") or []
                    legs_str = ", ".join(
                        f"#{l.get('no')} {l.get('name','')}" for l in legs
                    )
                    status = r.get("status", "")
                    if r.get("hit") is True:
                        status_icon = "✅"
                    elif r.get("hit") is False:
                        status_icon = "❌"
                    else:
                        status_icon = "⏳"
                    try:
                        rn_int = int(r.get("race_number"))
                    except (TypeError, ValueError):
                        rn_int = None
                    log_rows.append({
                        "Date": r["date"],
                        "R": rn_int,
                        "Play": r["play"],
                        "Banker": b_str,
                        "Legs": legs_str,
                        "Fin": r.get("banker_finish"),
                        "Stake": float(r.get("stake", r.get("stake_units", 0)) or 0),
                        "Return": float(r.get("return", 0) or 0)
                                    if r.get("status") == "settled" else None,
                        "Hit": status_icon,
                        "Filter": r.get("filter", ""),
                    })
                st.dataframe(
                    pd.DataFrame(log_rows), hide_index=True,
                    width='stretch',
                    column_config={
                        "R":      st.column_config.NumberColumn(format="%d"),
                        "Fin":    st.column_config.NumberColumn(format="%d"),
                        "Stake":  st.column_config.NumberColumn(format="$%.2f"),
                        "Return": st.column_config.NumberColumn(format="$%.2f"),
                    },
                )

    # ── TAB 2 — Strategy slate (Kelly-sized $-bets with reasoning) ──────────
    with tabs[1]:
        _render_strategy_slate_tab()

    # ── TAB 4 — Strategy sweep (reads pre-computed analysis) ───────────────
    with tabs[3]:
        st.markdown("#### April 2026 strategy sweep — real HKJC dividends")
        sweep_path = REPORTS / "betting_edge_analysis.json"
        if not sweep_path.exists():
            st.info("Run `python analyze_betting_edge.py` to generate "
                      "the sweep report.")
        else:
            try:
                sweep = json.loads(sweep_path.read_text(encoding="utf-8"))
            except Exception as e:
                st.error(f"Could not load sweep JSON: {e}")
                sweep = None
            if sweep:
                agg = sweep.get("aggregate", {})
                sweep_rows = []
                for strat, a in agg.items():
                    bets = a.get("bets", 0)
                    stake = a.get("stake", 0.0)
                    ret = a.get("ret", 0.0)
                    hits = a.get("hits", 0)
                    roi = (ret - stake) / stake if stake else 0
                    hr = hits / bets if bets else 0
                    sweep_rows.append({
                        "Strategy": strat,
                        "Bets": bets,
                        "Hit %": hr * 100,
                        "Stake": stake,
                        "Return": ret,
                        "ROI %": roi * 100,
                    })
                sweep_rows.sort(key=lambda r: -r["ROI %"])
                st.dataframe(
                    pd.DataFrame(sweep_rows), hide_index=True,
                    width='stretch',
                    column_config={
                        "Bets":   st.column_config.NumberColumn(format="%d"),
                        "Hit %":  st.column_config.NumberColumn(format="%.1f%%"),
                        "Stake":  st.column_config.NumberColumn(format="%.1f"),
                        "Return": st.column_config.NumberColumn(format="%.2f"),
                        "ROI %":  st.column_config.NumberColumn(format="%+.1f%%"),
                    },
                )
                st.caption(
                    "All rows use flat 1-unit stake per race. Dividends are "
                    "real HK$1 multipliers from `reports/dividends_*.json`."
                )

                st.markdown("#### QIN vs QPL under the same filters")
                st.markdown(
                    "Same structure (banker + 3 legs), different win condition. "
                    "QIN requires banker **top-2**; QPL requires banker **top-3**. "
                    "When the model agrees with SARR strongly, it's far better at "
                    "'finishes top-3' than 'finishes top-2', so QPL captures the "
                    "edge even at a smaller dividend. In the odds-5-8 band without "
                    "mutual agreement, QIN's bigger dividend wins."
                )
                qin_qpl = pd.DataFrame([
                    {"Filter": "All April races", "QIN ROI": "-37%", "QPL ROI": "-44%", "Better": "QIN"},
                    {"Filter": "Cls 3-5 only",    "QIN ROI": "-32%", "QPL ROI": "-42%", "Better": "QIN"},
                    {"Filter": "SP 5-8 band",     "QIN ROI": "+10%", "QPL ROI": "-21%", "Better": "QIN"},
                    {"Filter": "Gap ≥ 0.08",      "QIN ROI": "-18%", "QPL ROI": "-27%", "Better": "QIN"},
                    {"Filter": "Mutual top-3",    "QIN ROI": "-46%", "QPL ROI": "+22%", "Better": "QPL"},
                    {"Filter": "Mutual + gap ≥ 0.08", "QIN ROI": "-36%", "QPL ROI": "+88%", "Better": "QPL"},
                ])
                st.dataframe(qin_qpl, hide_index=True, width='stretch')

    # ── TAB 5 — Filter rules ────────────────────────────────────────────────
    with tabs[4]:
        st.markdown("#### Current edge configuration")
        st.markdown(
            "These thresholds are hard-coded in `betting_strategy.EDGE_CFG` and "
            "drive every recommendation on the Tickets tab. Tune them if later "
            "months of data suggest a shift."
        )
        st.code(json.dumps({k: (list(v) if isinstance(v, set) else v)
                                for k, v in EDGE_CFG.items()},
                               indent=2),
                  language="json")
        st.markdown(
            "**Sustainability:** the nightly scrape now pulls dividends "
            "automatically (see `scrape_hkjc_results.py`). "
            "`reports/dividends_YYYYMMDD.json` is written alongside "
            "`results_YYYYMMDD.json` for every meeting — no extra cron job "
            "needed. After results land, click **Save picks to log** on the "
            "Tickets tab to add that meeting's tickets to the long-run log, "
            "then revisit the Track Record tab to see settled P/L."
        )


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
                 width='stretch'):
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
            width='stretch',
        )


# ══════════════════════════════════════════════════════════════════════════════
# Calibration & Value Lab — exposes calibration_harness output
# ══════════════════════════════════════════════════════════════════════════════

def page_calibration():
    """Reliability + edge-quintile + rank×edge view of model performance.

    All numbers come from reports/calibration_harness.json. The page only
    visualises; the harness itself is what produces the data.
    """
    st.caption("Probability calibration and edge stratification — "
               "does the model add information beyond the market?")

    harness_path = REPORTS / "calibration_harness.json"

    # ── Sidebar: rebuild ──────────────────────────────────────────────
    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
    st.sidebar.markdown('<div class="sb-nav-section">Calibration</div>',
                        unsafe_allow_html=True)
    cal_from = st.sidebar.text_input("From (YYYY-MM-DD)", value="2026-04-01",
                                     key="cal_from")
    cal_to = st.sidebar.text_input("To (YYYY-MM-DD)", value="",
                                   key="cal_to")
    cal_version = st.sidebar.text_input("Model version", value="v4.4",
                                        key="cal_version")
    if st.sidebar.button("[ Rebuild Harness ]", key="btn_cal_rebuild",
                         width='stretch'):
        try:
            from calibration_harness import run as _cal_run
            d_from = (cal_from or "").replace("-", "") or None
            d_to = (cal_to or "").replace("-", "") or None
            with st.spinner("Running calibration harness ..."):
                _cal_run(d_from, d_to, version=cal_version or "v4.4")
            st.success("Harness rebuilt — refresh charts below.")
            st.rerun()
        except Exception as e:
            st.error(f"Harness failed: {e}")

    if not harness_path.exists():
        st.warning("No harness output yet. Click **Rebuild Harness** in "
                   "the sidebar, or run\n\n```\npython calibration_harness.py"
                   "\n```\nfrom the project folder.")
        return

    try:
        s = json.loads(harness_path.read_text(encoding="utf-8"))
    except Exception as e:
        st.error(f"Could not parse {harness_path.name}: {e}")
        return
    if s.get("n_rows", 0) == 0:
        st.info("Harness ran but found no rows. Check date range and that "
                "race_day_report_*.json files exist for those meetings.")
        return

    # ── Header summary ────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Meetings", s["n_meetings"])
    c2.metric("Races", s["n_races"])
    c3.metric("Runners", s["n_rows"])
    c4.metric("Date range", f"{s['date_min']} → {s['date_max']}")

    cal = s["calibration"]

    # ── Section 1: scoring metrics ────────────────────────────────────
    st.markdown("### 1 · Probability scoring")
    st.caption("Lower Brier / log-loss / ECE = better calibrated. "
               "BrierSkill > 0 means the source beats the Shin market "
               "baseline. On a small sample, expect p_model to be "
               "slightly NEGATIVE — beating the HK win pool is hard.")
    metric_rows = []
    src_order = [n for n in ("p_model", "p_gbm", "p_market_basic",
                              "p_market_shin") if n in cal]
    for name in src_order:
        m = cal[name]
        metric_rows.append({
            "Source": name,
            "n": m.get("n_rows", s["n_rows"]),
            "Brier": round(m["brier"], 4),
            "LogLoss": round(m["log_loss"], 4),
            "ECE": round(m["ece"], 4),
            "BrierSkill vs Shin": (
                f"{(m.get('brier_skill_vs_shin') or 0)*100:+.2f}%"),
            "LogLoss lift vs Shin": (
                f"{(m.get('logloss_lift_vs_shin') or 0):+.4f}"),
        })
    st.dataframe(pd.DataFrame(metric_rows), hide_index=True,
                 width='stretch')

    # ── Section 2: reliability curve ──────────────────────────────────
    st.markdown("### 2 · Reliability curve")
    st.caption("Each point = a probability bucket. X-axis is mean "
               "predicted P(win); Y-axis is observed win rate. The "
               "45° line is perfect calibration. Points above the line "
               "= source UNDER-estimates probability; below = OVER.")
    rel_rows = []
    rel_sources = [s_ for s_ in ("p_model", "p_gbm", "p_market_shin")
                   if s_ in cal]
    for src in rel_sources:
        for b in cal[src]["bins"]:
            rel_rows.append({
                "source": src, "bin": b["bin"], "n": b["n"],
                "p_predicted": b["p_mean"],
                "win_rate": b["win_rate"],
                "gap": b["diff"],
            })
    rel_df = pd.DataFrame(rel_rows)
    if not rel_df.empty:
        try:
            import altair as alt
            chart = (
                alt.Chart(rel_df)
                .mark_line(point=True)
                .encode(
                    x=alt.X("p_predicted:Q",
                            scale=alt.Scale(domain=[0, 0.6]),
                            title="Predicted P(win)"),
                    y=alt.Y("win_rate:Q",
                            scale=alt.Scale(domain=[0, 0.6]),
                            title="Observed win rate"),
                    color=alt.Color("source:N",
                                    title="",
                                    scale=alt.Scale(
                                        domain=["p_model", "p_gbm",
                                                "p_market_shin"],
                                        range=["#ff6e00", "#22c55e",
                                               "#5b8def"])),
                    tooltip=["source", "bin", "n", "p_predicted",
                             "win_rate", "gap"],
                )
                .properties(height=320)
            )
            ideal = alt.Chart(pd.DataFrame({"x": [0, 0.6], "y": [0, 0.6]})).mark_line(
                strokeDash=[4, 4], color="#888").encode(x="x:Q", y="y:Q")
            st.altair_chart(ideal + chart, width='stretch')
        except Exception:
            st.dataframe(rel_df, hide_index=True, width='stretch')

    # ── Section 3: edge quintiles ─────────────────────────────────────
    st.markdown("### 3 · ROI by edge quintile")
    st.caption("Edge = p_model − p_market_shin. If the edge signal is "
               "real, ROI should rise from Q1 → Q5. If quintiles look "
               "random, edge alone is noise.")
    qrows = s.get("edge_quintiles_shin", [])
    if qrows:
        qdf = pd.DataFrame(qrows)
        qdf["edge_band"] = qdf.apply(
            lambda r: f"Q{int(r['quintile'])} "
                      f"[{r['edge_min']:+.2f}, {r['edge_max']:+.2f}]",
            axis=1)
        qdf["flat_roi_pct"] = qdf["flat_roi"] * 100
        qdf["kelly_roi_pct"] = qdf["kelly_roi"] * 100
        try:
            import altair as alt
            long = qdf.melt(id_vars=["edge_band", "quintile", "n"],
                            value_vars=["flat_roi_pct", "kelly_roi_pct"],
                            var_name="strategy", value_name="roi_pct")
            chart = (
                alt.Chart(long).mark_bar().encode(
                    x=alt.X("edge_band:N", sort=None, title="Edge quintile"),
                    y=alt.Y("roi_pct:Q", title="ROI (%)"),
                    color=alt.Color("strategy:N",
                                    scale=alt.Scale(
                                        domain=["flat_roi_pct",
                                                "kelly_roi_pct"],
                                        range=["#5b8def", "#ff6e00"])),
                    xOffset="strategy:N",
                    tooltip=["edge_band", "strategy", "roi_pct", "n"],
                )
                .properties(height=300)
            )
            st.altair_chart(chart, width='stretch')
        except Exception:
            pass
        display_df = qdf[["quintile", "n", "edge_band", "strike",
                          "flat_roi", "kelly_roi"]].copy()
        display_df["strike"] = (display_df["strike"] * 100).round(2).astype(str) + "%"
        display_df["flat_roi"] = (display_df["flat_roi"] * 100).round(2).astype(str) + "%"
        display_df["kelly_roi"] = (display_df["kelly_roi"] * 100).round(2).astype(str) + "%"
        st.dataframe(display_df, hide_index=True, width='stretch')

    # ── Section 4: rank × edge grid ───────────────────────────────────
    st.markdown("### 4 · Rank × edge-sign ROI grid")
    st.caption("This is the table your TOP1_PE backtest came from. "
               "Cells where rank≤2 AND edge≥0 are the value zone; "
               "cells where edge<0 should systematically lose — if they "
               "don't, your model rank is just tracking the market.")
    cells = (s.get("rank_edge_grid") or {}).get("cells", [])
    if cells:
        gdf = pd.DataFrame(cells)
        gdf["strike_pct"] = (gdf["strike"] * 100).round(1)
        gdf["roi_pct"] = (gdf["roi"] * 100).round(2)
        try:
            import altair as alt
            heat = (
                alt.Chart(gdf).mark_rect().encode(
                    x=alt.X("edge:N", title="Edge sign"),
                    y=alt.Y("rank:N", sort=["1", "2", "3", "4+"],
                            title="Model rank"),
                    color=alt.Color("roi_pct:Q",
                                    scale=alt.Scale(
                                        scheme="redblue", domain=[-100, 100],
                                        domainMid=0),
                                    title="ROI (%)"),
                    tooltip=["rank", "edge", "n", "strike_pct", "roi_pct"],
                )
                .properties(height=240)
            )
            text = (
                alt.Chart(gdf).mark_text(fontSize=14, fontWeight="bold")
                .encode(x="edge:N",
                        y=alt.Y("rank:N", sort=["1", "2", "3", "4+"]),
                        text=alt.Text("roi_pct:Q", format=".1f"),
                        color=alt.condition(
                            "abs(datum.roi_pct) > 30",
                            alt.value("white"), alt.value("black")))
            )
            st.altair_chart(heat + text, width='stretch')
        except Exception:
            pass
        st.dataframe(
            gdf[["rank", "edge", "n", "strike_pct", "roi_pct"]]
            .rename(columns={"strike_pct": "strike%", "roi_pct": "roi%"}),
            hide_index=True, width='stretch',
        )

    # ── Section 5: per-rank winrate ───────────────────────────────────
    st.markdown("### 5 · Per-rank win rate")
    st.caption("Sanity check — strike rate should fall monotonically as "
               "rank increases. If model and market columns diverge, "
               "the model is taking a different stance than the market "
               "at that rank tier.")
    rt = s.get("per_rank_winrate", [])
    if rt:
        rdf = pd.DataFrame(rt).copy()
        rdf["strike_pct"] = (rdf["strike"] * 100).round(1).astype(str) + "%"
        rdf["p_model"] = rdf["p_model_mean"].round(3)
        rdf["p_market_shin"] = rdf["p_market_shin_mean"].round(3)
        st.dataframe(
            rdf[["rank", "n", "wins", "strike_pct",
                 "p_model", "p_market_shin"]],
            hide_index=True, width='stretch',
        )

    md_path = REPORTS / "CALIBRATION_HARNESS.md"
    if md_path.exists():
        with st.expander("Full markdown report"):
            st.markdown(md_path.read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════════════════
# GBM Lab — gradient-boosted model trainer/inspector
# ══════════════════════════════════════════════════════════════════════════════

def page_gbm():
    """Train + inspect the LightGBM model that learns from v4.4 features."""
    st.caption("Gradient-boosted model that re-learns the v4.4 weights "
               "from data — same features, no manual tuning, regularised "
               "by race-grouped cross-validation.")

    train_path = REPORTS / "gbm_training.json"
    model_path = BASE / "models" / "gbm_v1.txt"

    # ── Sidebar controls ───────────────────────────────────────────────────
    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
    st.sidebar.markdown('<div class="sb-nav-section">GBM</div>',
                        unsafe_allow_html=True)
    g_from = st.sidebar.text_input("From (YYYY-MM-DD)", value="2026-04-01",
                                   key="gbm_from")
    g_to = st.sidebar.text_input("To (YYYY-MM-DD)", value="",
                                 key="gbm_to")
    g_version = st.sidebar.text_input("Source model version", value="v4.4",
                                      key="gbm_version")
    g_splits = st.sidebar.number_input("CV splits", min_value=2,
                                       max_value=10, value=5, step=1,
                                       key="gbm_splits")
    if st.sidebar.button("[ Train GBM ]", key="btn_gbm_train",
                         width='stretch'):
        try:
            from train_gbm import run as _gbm_run
            d_from = (g_from or "").replace("-", "") or None
            d_to = (g_to or "").replace("-", "") or None
            with st.spinner("Training LightGBM ..."):
                _gbm_run(d_from, d_to, version=g_version or "v4.4",
                         n_splits=int(g_splits))
            st.success("GBM trained — refresh below.")
            st.rerun()
        except Exception as e:
            st.error(f"Training failed: {e}")

    score_date = st.sidebar.date_input("Score a meeting",
                                        value=date.today(),
                                        key="gbm_score_date")
    if st.sidebar.button("[ Score Meeting ]", key="btn_gbm_score",
                         width='stretch'):
        try:
            from train_gbm import score_report as _gbm_score
            d = score_date.isoformat().replace("-", "")
            with st.spinner("Scoring ..."):
                out = _gbm_score(d) or {}
            if not out:
                st.warning("No predictions — missing report or model.")
            else:
                st.session_state["_gbm_last_scored"] = {
                    "date": d,
                    "rows": [{"race_no": k[0], "horse_no": k[1],
                              "p_gbm_pct": round(v * 100, 2)}
                             for k, v in sorted(out.items())],
                }
                st.success(f"Scored {len(out)} runners on {d}.")
        except Exception as e:
            st.error(f"Scoring failed: {e}")

    if not train_path.exists():
        st.warning("No training output yet. Click **Train GBM** in the "
                   "sidebar, or run\n\n```\npython train_gbm.py\n```\n"
                   "from the project folder.")
        return
    try:
        s = json.loads(train_path.read_text(encoding="utf-8"))
    except Exception as e:
        st.error(f"Could not parse {train_path.name}: {e}")
        return
    if s.get("n_rows", 0) == 0:
        st.info("Training ran but found no rows.")
        return

    # ── Header summary ────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Meetings trained", s["n_meetings"])
    c2.metric("Races", s["n_races"])
    c3.metric("Rows", s["n_rows"])
    c4.metric("Date range", f"{s['date_min']} → {s['date_max']}")

    # ── Section 1: head-to-head with v4.4 ───────────────────────────────
    st.markdown("### 1 · GBM vs handcrafted v4.4 (out-of-fold)")
    st.caption("All metrics here are computed on out-of-fold predictions "
               "— the GBM never saw the rows it's being scored on. "
               "BrierSkill > 0 means GBM beats v4.4.")
    g = s["metrics"]["p_gbm"]
    v = s["metrics"]["p_v44"]
    h2h = pd.DataFrame([
        {"Source": "p_v44 (handcrafted)",
         "Brier": round(v["brier"], 4),
         "LogLoss": round(v["log_loss"], 4),
         "BrierSkill vs v44": "baseline",
         "LogLoss lift": "baseline"},
        {"Source": "p_gbm (LightGBM)",
         "Brier": round(g["brier"], 4),
         "LogLoss": round(g["log_loss"], 4),
         "BrierSkill vs v44":
             f"{(g.get('brier_skill_vs_v44') or 0)*100:+.2f}%",
         "LogLoss lift":
             f"{(g.get('logloss_lift_vs_v44') or 0):+.4f}"},
    ])
    st.dataframe(h2h, hide_index=True, width='stretch')

    # ── Section 2: feature importance ───────────────────────────────────
    st.markdown("### 2 · What the GBM learned")
    st.caption("Higher gain = the feature explained more variance in "
               "win/loss. Compare to your v4.4 weights: features at the "
               "top that v4.4 down-weighted are where the GBM disagrees "
               "with the handcrafted score.")
    imp = s.get("feature_importance") or []
    if imp:
        idf = pd.DataFrame(imp).head(20).copy()
        try:
            import altair as alt
            chart = (
                alt.Chart(idf).mark_bar(color="#22c55e").encode(
                    x=alt.X("gain:Q", title="Gain"),
                    y=alt.Y("feature:N", sort="-x", title=""),
                    tooltip=["feature", "gain", "split"],
                )
                .properties(height=420)
            )
            st.altair_chart(chart, width='stretch')
        except Exception:
            st.dataframe(idf, hide_index=True, width='stretch')

    # ── Section 3: reliability ──────────────────────────────────────────
    st.markdown("### 3 · Reliability — p_gbm (OOF)")
    bins = g.get("bins") or []
    if bins:
        bdf = pd.DataFrame(bins)
        try:
            import altair as alt
            line = (
                alt.Chart(bdf).mark_line(point=True, color="#22c55e")
                .encode(
                    x=alt.X("p_mean:Q", scale=alt.Scale(domain=[0, 0.6]),
                            title="Predicted P(win)"),
                    y=alt.Y("win_rate:Q", scale=alt.Scale(domain=[0, 0.6]),
                            title="Observed win rate"),
                    tooltip=["bin", "n", "p_mean", "win_rate", "diff"],
                )
                .properties(height=300)
            )
            ideal = alt.Chart(
                pd.DataFrame({"x": [0, 0.6], "y": [0, 0.6]})
            ).mark_line(strokeDash=[4, 4], color="#888").encode(
                x="x:Q", y="y:Q")
            st.altair_chart(ideal + line, width='stretch')
        except Exception:
            st.dataframe(bdf, hide_index=True, width='stretch')

    # ── Section 4: per-fold CV ────────────────────────────────────────
    folds = (s.get("cv") or {}).get("folds") or []
    if folds:
        st.markdown("### 4 · Cross-validation folds")
        st.caption("Each fold trains on N−1 meetings and validates on the "
                   "held-out meeting. Stable logloss across folds = GBM "
                   "isn't overfitting one particular meeting.")
        st.dataframe(pd.DataFrame(folds), hide_index=True,
                     width='stretch')

    # ── Section 5: most-recent scored meeting (if any) ─────────────────────
    last = st.session_state.get("_gbm_last_scored")
    if last:
        st.markdown(f"### 5 · GBM scores for {last['date']}")
        st.dataframe(pd.DataFrame(last["rows"]),
                     hide_index=True, width='stretch')

    md_path = REPORTS / "GBM_TRAINING.md"
    if md_path.exists():
        with st.expander("Full markdown report"):
            st.markdown(md_path.read_text(encoding="utf-8"))

    if model_path.exists():
        st.caption(f"Model file: `{model_path.relative_to(BASE)}`  ·  "
                   f"size: {model_path.stat().st_size//1024} KB")


# ══════════════════════════════════════════════════════════════════════════════
# Model Lab — combines GBM, Calibration and Backtest under one nav entry
# ══════════════════════════════════════════════════════════════════════════════

def page_model_lab():
    """Single entry that hosts the three modelling/diagnostic views as tabs.

    GBM Lab is the default tab (first). The Backtest tab still exposes the
    full strategy backtest sidebar workflow (scrape results, run backtest,
    monthly aggregates) — those controls only render when that tab is
    active so they don't pollute other tabs.
    """
    st.markdown('<div class="page-title">Model Lab</div>',
                unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">Train, calibrate and stress-test '
                'the modelling pipeline. Tabs are independent — flick '
                'between them without losing state.</div>',
                unsafe_allow_html=True)
    tab_gbm, tab_cal, tab_bt = st.tabs([
        "🌲 GBM Lab", "📐 Calibration Lab", "🧪 Strategy Backtest",
    ])
    with tab_gbm:
        page_gbm()
    with tab_cal:
        page_calibration()
    with tab_bt:
        page_backtest()




# ══════════════════════════════════════════════════════════════════════════════
# Entry point — page router
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# Race Lookup — multi-criteria search across the master DB
# ══════════════════════════════════════════════════════════════════════════════

RL_PRESETS_PATH = BASE / "cache" / "race_lookup_presets.json"

# Filter session_state keys (used by clear-all + preset save/load).
RL_FILTER_KEYS = [
    "rl_date_mode", "rl_date_range", "rl_date_exact", "rl_date_month",
    "rl_horse_name", "rl_track", "rl_course", "rl_class", "rl_distance",
    "rl_going",
    "rl_gate_mode", "rl_gate_bands", "rl_gates",
    "rl_rating_mode", "rl_rating_bands", "rl_rating_range",
    "rl_jockey", "rl_trainer", "rl_style",
    "rl_finish_mode", "rl_finish_specific",
    "rl_pace",
    "rl_time_mode", "rl_time_range", "rl_time_pct",
    "rl_outliers_only", "rl_outlier_kind",
]


def _rl_load_presets() -> dict:
    if not RL_PRESETS_PATH.exists():
        return {}
    try:
        return json.loads(RL_PRESETS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _rl_save_presets(d: dict) -> None:
    RL_PRESETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RL_PRESETS_PATH.write_text(json.dumps(d, indent=2, default=str),
                               encoding="utf-8")


def _rl_clear_all_filters():
    """Pop every rl_* filter key so widgets snap back to defaults."""
    for k in RL_FILTER_KEYS:
        st.session_state.pop(k, None)


def _rl_apply_preset(preset: dict):
    """Push a saved preset back into session_state. Tuples (e.g. date
    range) come back from JSON as lists, so we coerce where needed."""
    _rl_clear_all_filters()
    for k, v in (preset or {}).items():
        if k == "rl_date_range" and isinstance(v, list) and len(v) == 2:
            try:
                from datetime import date as _date
                v = tuple(_date.fromisoformat(str(x)[:10]) for x in v)
            except Exception:
                continue
        elif k in ("rl_date_exact",) and v:
            try:
                from datetime import date as _date
                v = _date.fromisoformat(str(v)[:10])
            except Exception:
                continue
        elif k == "rl_rating_range" and isinstance(v, list):
            v = tuple(v)
        elif k == "rl_time_range" and isinstance(v, list):
            v = tuple(v)
        st.session_state[k] = v


@st.cache_data(ttl=600, show_spinner="Loading master DB…")
def _lookup_load_df(db_mtime: float, pace_mtime: float) -> pd.DataFrame:
    """Load every row from hkjc.db once, enrich with derived fields, and
    cache. Cache key includes the file mtimes so it auto-invalidates after
    `append_results_to_db` rewrites the SQLite mirror or the pace index is
    rebuilt. All filtering happens in-memory afterwards (instant)."""
    from db_utils import read_sqlite

    cols = (
        "race_date, race_number, race_track, race_course, track_type, "
        "race_class, distance, going, horse_id, horse_name, horsename_zh, "
        "jockey, trainer, draw, place, lbw, finish_time_seconds, "
        "running_positions, sectiontimes, win_odds, rating, current_rating, "
        "actual_weight, declared_weight, gear, age, sex, sire, owner, "
        "race_url, horse_url"
    )
    df = read_sqlite(f"SELECT {cols} FROM results")
    if df.empty:
        return df

    # ── Date helpers ─────────────────────────────────────────────────────
    df["race_date"] = pd.to_datetime(df["race_date"], errors="coerce")
    df["race_date_str"] = df["race_date"].dt.strftime("%Y-%m-%d")
    df["year"] = df["race_date"].dt.year
    df["month"] = df["race_date"].dt.to_period("M").astype(str)

    # ── Numeric coercions ────────────────────────────────────────────────
    df["draw_n"] = pd.to_numeric(df["draw"], errors="coerce")
    df["rating_n"] = pd.to_numeric(df["rating"], errors="coerce")
    df["place_n"] = pd.to_numeric(df["place"], errors="coerce")
    df["distance_n"] = pd.to_numeric(df["distance"], errors="coerce")
    df["finish_time_seconds"] = pd.to_numeric(df["finish_time_seconds"], errors="coerce")
    df["win_odds_n"] = pd.to_numeric(df["win_odds"], errors="coerce")

    # ── Gate band ────────────────────────────────────────────────────────
    def _gate_band(d):
        if pd.isna(d): return ""
        d = int(d)
        if d <= 4:  return "Inside (1-4)"
        if d <= 8:  return "Middle (5-8)"
        return "Outside (9-14)"
    df["gate_band"] = df["draw_n"].map(_gate_band)

    # ── Rating class band (HKJC standard) ────────────────────────────────
    def _rating_class(r):
        if pd.isna(r): return ""
        r = float(r)
        if r >= 100: return "C1 (100+)"
        if r >= 80:  return "C2 (80-99)"
        if r >= 60:  return "C3 (60-79)"
        if r >= 40:  return "C4 (40-59)"
        if r >= 20:  return "C5 (20-39)"
        return "C5- (<20)"
    df["rating_band"] = df["rating_n"].map(_rating_class)

    # ── Running style from running_positions ─────────────────────────────
    def _style(rp):
        if rp is None or (isinstance(rp, float) and pd.isna(rp)):
            return ""
        try:
            parts = [int(p) for p in str(rp).split() if p.strip().lstrip("-").isdigit()]
        except Exception:
            return ""
        if not parts:
            return ""
        f = parts[0]
        if f <= 2: return "Leader"
        if f <= 4: return "On-Pace"
        if f <= 7: return "Midfield"
        return "Closer"
    df["run_style"] = df["running_positions"].map(_style)

    # ── Place buckets ────────────────────────────────────────────────────
    df["is_win"] = (df["place_n"] == 1)
    df["is_place"] = df["place_n"].between(1, 3, inclusive="both")

    # ── Field size per race (date, race_number) ──────────────────────────
    fs = df.groupby(["race_date_str", "race_number"])["horse_name"].transform("count")
    df["field_size"] = fs

    # ── Surface flag ─────────────────────────────────────────────────────
    rc_up = df["race_course"].astype(str).str.upper()
    tt_up = df["track_type"].astype(str).str.upper()
    df["surface"] = ["AWT" if ("AWT" in r or "ALL WEATHER" in t) else "Turf"
                     for r, t in zip(rc_up, tt_up)]

    # ── Pace label (per race) — merge from cache/race_pace_index.json ───
    pidx_path = BASE / "cache" / "race_pace_index.json"
    pace_map = {}
    if pidx_path.exists():
        try:
            raw = json.loads(pidx_path.read_text(encoding="utf-8"))
            for k, v in raw.items():
                pace_map[k] = v.get("label") or ""
        except Exception:
            pace_map = {}
    df["race_key"] = df["race_date_str"] + "_R" + df["race_number"].astype(str)
    df["pace_label"] = df["race_key"].map(pace_map).fillna("")

    # ── Speed figure: z-score within (course, distance, going) ──────────
    # Negative = faster than bucket mean. Only for buckets with >=10 runs.
    grp = df.groupby(["race_course", "distance_n", "going"], dropna=False)
    bmean = grp["finish_time_seconds"].transform("mean")
    bstd = grp["finish_time_seconds"].transform("std")
    bcnt = grp["finish_time_seconds"].transform("count")
    z = (df["finish_time_seconds"] - bmean) / bstd
    z = z.where(bcnt >= 10)
    df["speed_fig"] = z.round(2)

    # ── Outlier flags (boilover / flop) ─────────────────────────────────
    df["is_boilover"] = (df["place_n"] == 1) & (df["win_odds_n"] >= 20)
    df["is_flop"] = (df["win_odds_n"] <= 3.0) & (df["place_n"] >= 6)

    return df


@st.cache_data(ttl=600, show_spinner=False)
def _lookup_baselines(db_mtime: float, pace_mtime: float) -> dict:
    """Pre-compute baseline win rates on the *full* unfiltered DB so the
    filtered slice can be compared (z-score / delta-vs-baseline)."""
    df = _lookup_load_df(db_mtime, pace_mtime)
    if df.empty:
        return {}
    base = {}
    base["overall_win"] = float(df["is_win"].mean())
    base["overall_place"] = float(df["is_place"].mean())
    # Per-(course, distance, gate_band) baselines
    g = df.groupby(["race_course", "distance_n", "gate_band"], dropna=False)
    base["course_dist_gate"] = (g["is_win"].mean() * 100).round(2).to_dict()
    base["course_dist_gate_n"] = g.size().to_dict()
    # Per-(course, distance) baseline (for time z-score reference)
    g2 = df.groupby(["race_course", "distance_n"], dropna=False)
    base["course_dist_time_mean"] = g2["finish_time_seconds"].mean().to_dict()
    base["course_dist_time_std"] = g2["finish_time_seconds"].std().to_dict()
    base["course_dist_n"] = g2.size().to_dict()
    # Per-jockey baseline win rate (across all his runs)
    jb = df.groupby("jockey")["is_win"].agg(["mean", "size"])
    base["jockey_win"] = jb["mean"].to_dict()
    base["jockey_n"] = jb["size"].to_dict()
    tb = df.groupby("trainer")["is_win"].agg(["mean", "size"])
    base["trainer_win"] = tb["mean"].to_dict()
    base["trainer_n"] = tb["size"].to_dict()
    return base


def _lookup_distinct(df: pd.DataFrame, col: str) -> list:
    """Sorted list of non-empty unique values for a dropdown."""
    if col not in df.columns or df.empty:
        return []
    s = df[col].dropna().astype(str).str.strip()
    s = s[(s != "") & (s.str.lower() != "nan") & (s.str.lower() != "none")]
    return sorted(s.unique().tolist())


def page_race_lookup():
    """Multi-criteria lookup against the master results DB. All data lives
    in memory after a single cached load — filters are instant.

    Includes the Horse Profile drilldown as a secondary tab (merged from
    the previous standalone Horse Profile page).
    """
    import datetime as _dt
    import subprocess as _sp

    st.markdown('<div class="page-title">Race Lookup</div>',
                unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">Multi-criteria search across the '
                'full results history. All filters apply in-memory; change '
                'anything to re-query instantly.</div>',
                unsafe_allow_html=True)

    db_path = BASE / "hkjc.db"
    pace_path = BASE / "cache" / "race_pace_index.json"
    if not db_path.exists():
        st.error("hkjc.db not found. Run `python db_utils.py --rebuild` first.")
        return

    # ── Auto-sync: backfill any results JSON that's not yet in the DB ──
    # Runs once per Streamlit session to avoid hammering on every rerun.
    if not st.session_state.get("_rl_autosync_done"):
        try:
            n_mtg, n_rows = _auto_sync_results_to_db(verbose=False)
            if n_mtg:
                _lookup_load_df.clear()
                _lookup_baselines.clear()
                st.toast(
                    f"Auto-synced {n_mtg} missing meeting(s) to DB ({n_rows} rows).",
                    icon="🔄",
                )
        except Exception:
            pass
        st.session_state["_rl_autosync_done"] = True

    db_mtime = db_path.stat().st_mtime
    pace_mtime = pace_path.stat().st_mtime if pace_path.exists() else 0.0

    df_all = _lookup_load_df(db_mtime, pace_mtime)
    if df_all.empty:
        st.warning("Master DB is empty.")
        return
    baselines = _lookup_baselines(db_mtime, pace_mtime)

    min_d = df_all["race_date"].min().date()
    max_d = df_all["race_date"].max().date()

    # ── DB freshness caption + pace-coverage ─────────────────────────────
    pace_keys = set(df_all.loc[df_all["pace_label"] != "", "race_key"])
    all_keys = set(df_all["race_key"])
    missing_pace = sorted(all_keys - pace_keys)
    db_dt = hkt_from_ts(db_mtime)
    cap_l, cap_pace, cap_sync = st.columns([3, 1, 1])
    with cap_l:
        st.caption(
            f"DB last updated **{db_dt:%Y-%m-%d %H:%M} HKT** · "
            f"{len(df_all):,} runs · {len(all_keys):,} races · "
            f"pace-labelled {len(pace_keys):,}/{len(all_keys):,} "
            f"({100 * len(pace_keys) / max(1, len(all_keys)):.0f}%)"
        )
    with cap_sync:
        if st.button(
            "🔄 Sync results→DB",
            key="rl_sync_db", width='stretch',
            help="Append every reports/results_*.json newer than the DB "
                 "into hkjc.db. Idempotent: existing dates are overwritten.",
        ):
            try:
                from db_utils import append_results_to_db as _appender
                results_dir = BASE / "reports"
                total = 0
                appended = 0
                with st.spinner("Appending results JSON files to DB …"):
                    for fp in sorted(results_dir.glob("results_*.json")):
                        if fp.stat().st_mtime <= db_mtime:
                            continue
                        n = _appender(fp, verbose=False)
                        if n:
                            appended += 1
                            total += int(n)
                _lookup_load_df.clear()
                _lookup_baselines.clear()
                if appended:
                    with st.spinner("Rebuilding pace index + form guides …"):
                        _msgs = _rebuild_caches_after_sync()
                    st.toast(
                        f"Synced {appended} meeting(s) — {total} rows. "
                        + " · ".join(_msgs),
                        icon="✅",
                    )
                else:
                    st.toast("DB already up to date.", icon="ℹ️")
                st.rerun()
            except Exception as e:
                st.error(f"Sync failed: {e}")
    with cap_pace:
        if missing_pace and st.button(
            f"⚙️ Build pace index ({len(missing_pace)} missing)",
            key="rl_build_pace", width='stretch',
            help="Run build_pace_index.py to populate `pace_label` for "
                 "races that don't yet have a label."
        ):
            try:
                with st.spinner("Running build_pace_index.py …"):
                    r = _sp.run(
                        [sys.executable, str(BASE / "build_pace_index.py")],
                        capture_output=True, text=True, timeout=300,
                        cwd=str(BASE),
                    )
                if r.returncode == 0:
                    _lookup_load_df.clear()
                    _lookup_baselines.clear()
                    st.toast("Pace index rebuilt.", icon="⚙️")
                    st.rerun()
                else:
                    st.error((r.stderr or r.stdout)[-1500:])
            except (OSError, _sp.TimeoutExpired) as e:
                st.error(str(e))

    # ── Top tabs: Lookup vs merged Horse Profile ────────────────────────
    tab_lookup, tab_patterns, tab_horse = st.tabs(
        ["🔎 Race Lookup", "🎯 Race Patterns", "🐴 Horse Profile"]
    )

    with tab_horse:
        page_horse_profile()

    with tab_lookup:
        _rl_render_lookup(df_all, baselines, min_d, max_d)

    with tab_patterns:
        _rl_render_patterns(df_all)


def _rl_profile_match(horse_runs: pd.DataFrame, target: dict) -> dict:
    """Return the most-specific historical profile bucket for a horse vs the
    target race signature, plus place / win rates inside that bucket.

    Buckets, from most → least specific:
      1. same course + distance + class + surface
      2. same course + distance + class
      3. same course + distance
      4. same distance + class
      5. same distance
    Returns the first bucket with ``min_n`` runs, or the broadest available.
    """
    if horse_runs is None or horse_runs.empty:
        return {"label": "no past runs", "n": 0, "n_place": 0, "n_win": 0,
                "place_rate": 0.0, "win_rate": 0.0}
    course = target.get("course")
    dist = target.get("distance")
    cls = target.get("race_class")
    surface = target.get("surface")
    min_n = 3
    candidates = [
        (f"same {course} · {dist}m · Class {cls} · {surface}",
         (horse_runs["race_course"] == course)
         & (horse_runs["distance_n"] == dist)
         & (horse_runs["race_class"].astype(str) == str(cls))
         & (horse_runs["surface"] == surface)),
        (f"same {course} · {dist}m · Class {cls}",
         (horse_runs["race_course"] == course)
         & (horse_runs["distance_n"] == dist)
         & (horse_runs["race_class"].astype(str) == str(cls))),
        (f"same {course} · {dist}m",
         (horse_runs["race_course"] == course)
         & (horse_runs["distance_n"] == dist)),
        (f"same {dist}m · Class {cls}",
         (horse_runs["distance_n"] == dist)
         & (horse_runs["race_class"].astype(str) == str(cls))),
        (f"same {dist}m",
         (horse_runs["distance_n"] == dist)),
    ]
    chosen_label = ""
    chosen_mask = None
    for label, mask in candidates:
        n = int(mask.sum())
        if n >= min_n:
            chosen_label = label
            chosen_mask = mask
            break
    if chosen_mask is None:
        label, mask = candidates[-1]
        chosen_label = label
        chosen_mask = mask
    sub = horse_runs[chosen_mask]
    n = len(sub)
    if n == 0:
        return {"label": chosen_label, "n": 0, "n_place": 0, "n_win": 0,
                "place_rate": 0.0, "win_rate": 0.0}
    n_place = int(sub["place_n"].between(1, 3, inclusive="both").sum())
    n_win = int((sub["place_n"] == 1).sum())
    return {
        "label": chosen_label,
        "n": n,
        "n_place": n_place,
        "n_win": n_win,
        "place_rate": n_place / n if n else 0.0,
        "win_rate": n_win / n if n else 0.0,
        "recent": sub.sort_values("race_date", ascending=False).head(3),
    }


def _rl_render_patterns(df_all: pd.DataFrame):
    """For an upcoming meeting, highlight horses whose past form shows a
    strong place rate at the matching profile bucket (course / distance /
    class / surface). The aim is to surface profile-suited runners — a
    pattern-led counterpart to the model / SARR picks.
    """
    import datetime as _dt
    st.markdown("### 🎯 Profile-suited runners by race")
    st.caption(
        "For each race we compute every runner's place-rate (top-3) and "
        "win-rate at the closest match of *course · distance · class · "
        "surface*. Horses are flagged when their bucket place-rate beats "
        "the random baseline (≈3 / field-size) by a meaningful margin and "
        "they have at least 3 past runs in that bucket."
    )

    # ── Racecard picker ───────────────────────────────────────────────
    rc_dir = BASE / "cache"
    rc_files = sorted(rc_dir.glob("racecard_*.json"), reverse=True)
    if not rc_files:
        st.info("No racecard caches found. Generate a racecard first.")
        return
    today = _dt.date.today()
    options = []
    default_idx = 0
    for i, fp in enumerate(rc_files):
        iso = fp.stem.replace("racecard_", "")
        try:
            d = _dt.date.fromisoformat(iso)
        except Exception:
            continue
        label = f"{iso} {'(upcoming)' if d >= today else '(past)'}"
        options.append((iso, label, fp))
        if d >= today and default_idx == 0:
            default_idx = len(options) - 1
    if not options:
        st.info("No valid racecards.")
        return
    labels = [o[1] for o in options]
    pick_idx = st.selectbox(
        "Meeting", range(len(options)),
        index=min(default_idx, len(options) - 1),
        format_func=lambda i: labels[i],
        key="rlpat_meeting",
    )
    iso, _, rc_path = options[pick_idx]

    try:
        racecard = json.loads(rc_path.read_text(encoding="utf-8"))
    except Exception as e:
        st.error(f"Failed to read racecard: {e}")
        return
    races = racecard.get("races", []) or []
    if not races:
        st.warning("Racecard has no races.")
        return

    # Index df_all by horse_name for fast filtering
    df_by_horse = dict(tuple(df_all.groupby(df_all["horse_name"].astype(str).str.upper())))

    surface_map = {"TURF": "Turf", "AWT": "AWT", "ALL WEATHER TRACK": "AWT"}

    for race in races:
        meta = race.get("meta", {}) or {}
        rn = meta.get("race_number")
        rname = meta.get("race_name", "")
        course = meta.get("race_course") or ""
        dist = meta.get("distance")
        cls = meta.get("race_class")
        surface_raw = (meta.get("surface") or "").upper()
        surface = surface_map.get(surface_raw, "Turf")
        going = meta.get("going") or ""
        n_field = len([h for h in race.get("horses", []) if not h.get("is_standby")])

        with st.expander(
            f"R{rn} · {dist}m · Class {cls} · {course} · {surface}"
            + (f" · {going}" if going else "")
            + f" — {rname}",
            expanded=False,
        ):
            baseline = 3 / max(1, n_field)
            st.caption(
                f"Field {n_field} · baseline place-rate ≈ {baseline:.0%}"
            )
            target = {"course": course, "distance": dist,
                      "race_class": cls, "surface": surface,
                      "going": going}
            rows = []
            for h in race.get("horses", []):
                if h.get("is_standby"):
                    continue
                name = str(h.get("horse_name", "")).upper()
                runs = df_by_horse.get(name)
                if runs is None or runs.empty:
                    rows.append({
                        "No": h.get("horse_no"), "Horse": h.get("horse_name"),
                        "Jockey": h.get("jockey", ""),
                        "Bucket": "no past runs",
                        "n": 0, "Place%": 0.0, "Win%": 0.0,
                        "vs baseline": 0.0, "flag": "",
                    })
                    continue
                m = _rl_profile_match(runs, target)
                edge = m["place_rate"] - baseline
                if m["n"] >= 3 and edge >= 0.15:
                    flag = "⭐⭐"
                elif m["n"] >= 3 and edge >= 0.05:
                    flag = "⭐"
                else:
                    flag = ""
                rows.append({
                    "No": h.get("horse_no"), "Horse": h.get("horse_name"),
                    "Jockey": h.get("jockey", ""),
                    "Bucket": m["label"],
                    "n": m["n"],
                    "Place%": m["place_rate"],
                    "Win%": m["win_rate"],
                    "vs baseline": edge,
                    "flag": flag,
                })
            if not rows:
                st.info("No runners in this race.")
                continue
            df_rows = pd.DataFrame(rows).sort_values(
                ["vs baseline", "Place%", "n"],
                ascending=[False, False, False]
            ).reset_index(drop=True)

            def _edge_color(v):
                try: v = float(v)
                except Exception: return ""
                if v >= 0.15: return "background-color:#1b7837;color:white"
                if v >= 0.05: return "background-color:#b7e3b7;color:black"
                if v <= -0.10: return "background-color:#f5cbcb;color:black"
                return ""

            sty = (df_rows.style
                   .map(_edge_color, subset=["vs baseline"])
                   .format({"Place%": "{:.0%}", "Win%": "{:.0%}",
                            "vs baseline": "{:+.0%}"}, na_rep="—"))
            st.dataframe(sty, hide_index=True, width='stretch',
                         height=min(420, 38 + 35 * len(df_rows)))


def _rl_render_lookup(df_all: pd.DataFrame, baselines: dict,
                      min_d, max_d):
    """The actual lookup UI — pulled out so page_race_lookup can host it
    as a tab next to Horse Profile."""

    # ── Top action bar: clear-all + presets ──────────────────────────────
    presets = _rl_load_presets()
    a1, a2, a3, a4 = st.columns([0.9, 1.6, 1.4, 1.1])
    with a1:
        if st.button("✖️ Clear filters", key="rl_clear",
                     width='stretch',
                     help="Reset every filter to its default state."):
            _rl_clear_all_filters()
            st.rerun()
    with a2:
        preset_names = ["—"] + sorted(presets.keys())
        chosen = st.selectbox("Load preset", preset_names,
                              key="rl_preset_pick", index=0,
                              label_visibility="collapsed")
        if chosen != "—" and st.session_state.get("rl_preset_last") != chosen:
            _rl_apply_preset(presets.get(chosen, {}))
            st.session_state["rl_preset_last"] = chosen
            st.toast(f"Loaded preset: {chosen}", icon="📥")
            st.rerun()
    with a3:
        new_name = st.text_input("Save as", key="rl_preset_name",
                                 placeholder="preset name…",
                                 label_visibility="collapsed")
    with a4:
        if st.button("💾 Save preset", key="rl_preset_save",
                     width='stretch',
                     disabled=not new_name.strip()):
            snap = {k: st.session_state.get(k) for k in RL_FILTER_KEYS
                    if k in st.session_state}
            presets[new_name.strip()] = snap
            _rl_save_presets(presets)
            st.toast(f"Saved preset: {new_name.strip()}", icon="💾")
            st.rerun()
    if presets:
        with st.expander("Manage presets", expanded=False):
            for nm in sorted(presets.keys()):
                cc1, cc2 = st.columns([4, 1])
                cc1.markdown(f"- **{nm}**")
                if cc2.button("Delete", key=f"rl_pdel_{nm}",
                              width='stretch'):
                    presets.pop(nm, None)
                    _rl_save_presets(presets)
                    st.rerun()

    # ── Filter widgets ───────────────────────────────────────────────────
    with st.expander("Filters", expanded=True):
        # Row 1: dates
        c1, c2, c3 = st.columns([1.1, 1.1, 1.1])
        with c1:
            date_mode = st.radio("Date mode",
                                 ["Range", "Exact date", "Month", "All"],
                                 horizontal=True, key="rl_date_mode")
        with c2:
            sel_start = sel_end = sel_exact = sel_month = None
            if date_mode == "Range":
                rng = st.date_input("Date range",
                                    value=(max(min_d, max_d.replace(day=1)), max_d),
                                    min_value=min_d, max_value=max_d,
                                    key="rl_date_range")
                if isinstance(rng, tuple) and len(rng) == 2:
                    sel_start, sel_end = rng
            elif date_mode == "Exact date":
                sel_exact = st.date_input("Date", value=max_d,
                                          min_value=min_d, max_value=max_d,
                                          key="rl_date_exact")
            elif date_mode == "Month":
                months = sorted(df_all["month"].dropna().unique().tolist(), reverse=True)
                sel_month = st.selectbox("Month (YYYY-MM)", months, key="rl_date_month")
        with c3:
            sel_horse_name = st.text_input("Horse name (substring)",
                                           key="rl_horse_name").strip().upper()

        # Row 2: track / surface / class / distance
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            sel_track = st.multiselect("Track",
                                       _lookup_distinct(df_all, "race_track"),
                                       key="rl_track")
        with c2:
            sel_course = st.multiselect("Course",
                                        _lookup_distinct(df_all, "race_course"),
                                        key="rl_course")
        with c3:
            sel_class = st.multiselect("Class",
                                       _lookup_distinct(df_all, "race_class"),
                                       key="rl_class")
        with c4:
            sel_dist = st.multiselect("Distance (m)",
                                      sorted(df_all["distance_n"].dropna().astype(int).unique().tolist()),
                                      key="rl_distance")

        # Row 2b: going (track condition)
        going_opts = _lookup_distinct(df_all, "going")
        sel_going = st.multiselect(
            "Going (track condition)", going_opts, key="rl_going",
            help="Filter by reported going (e.g. Good, Good to Firm, Yielding, Soft).",
        ) if going_opts else []

        # Row 3: gate / rating
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            sel_gate_mode = st.radio("Gate mode",
                                     ["Any", "Bands", "Specific"],
                                     horizontal=True, key="rl_gate_mode")
        with c2:
            sel_gate_bands = sel_gates = []
            if sel_gate_mode == "Bands":
                sel_gate_bands = st.multiselect(
                    "Gate band",
                    ["Inside (1-4)", "Middle (5-8)", "Outside (9-14)"],
                    key="rl_gate_bands")
            elif sel_gate_mode == "Specific":
                sel_gates = st.multiselect("Gate",
                                           list(range(1, 15)),
                                           key="rl_gates")
        with c3:
            sel_rating_mode = st.radio("Rating mode",
                                       ["Any", "Bands", "Range"],
                                       horizontal=True, key="rl_rating_mode")
        with c4:
            sel_rating_bands = []
            sel_rating_min = sel_rating_max = None
            if sel_rating_mode == "Bands":
                sel_rating_bands = st.multiselect(
                    "Rating band",
                    ["C1 (100+)", "C2 (80-99)", "C3 (60-79)",
                     "C4 (40-59)", "C5 (20-39)", "C5- (<20)"],
                    key="rl_rating_bands")
            elif sel_rating_mode == "Range":
                rng = st.slider("Rating", 0, 130, (40, 100), key="rl_rating_range")
                sel_rating_min, sel_rating_max = rng

        # Row 4: jockey / trainer
        c1, c2 = st.columns(2)
        with c1:
            sel_jockey = st.multiselect("Jockey",
                                        _lookup_distinct(df_all, "jockey"),
                                        key="rl_jockey")
        with c2:
            sel_trainer = st.multiselect("Trainer",
                                         _lookup_distinct(df_all, "trainer"),
                                         key="rl_trainer")

        # Row 5: profile / finish / pace / time
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            sel_style = st.multiselect("Running style (profile)",
                                       ["Leader", "On-Pace", "Midfield", "Closer"],
                                       key="rl_style")
        with c2:
            sel_finish = st.radio("Finish",
                                  ["Any", "Win (1)", "Place (1-3)",
                                   "Top-5", "Specific"],
                                  horizontal=True, key="rl_finish_mode")
            sel_finish_specific = []
            if sel_finish == "Specific":
                sel_finish_specific = st.multiselect(
                    "Position", list(range(1, 15)), key="rl_finish_specific")
        with c3:
            pace_opts = _lookup_distinct(df_all, "pace_label")
            sel_pace = st.multiselect("Race pace",
                                      pace_opts,
                                      key="rl_pace") if pace_opts else []
        with c4:
            sel_time_mode = st.radio("Time filter",
                                     ["Any", "Custom range",
                                      "Top X% in (course, distance)",
                                      "Speed fig ≤"],
                                     horizontal=True, key="rl_time_mode")
            sel_time_min = sel_time_max = None
            sel_time_pct = None
            sel_speed_fig = None
            if sel_time_mode == "Custom range":
                t_lo = float(df_all["finish_time_seconds"].min() or 50.0)
                t_hi = float(df_all["finish_time_seconds"].max() or 200.0)
                rng = st.slider("Finish time (s)",
                                int(t_lo), int(t_hi) + 1,
                                (int(t_lo), int(t_hi) + 1),
                                key="rl_time_range")
                sel_time_min, sel_time_max = rng
            elif sel_time_mode == "Top X% in (course, distance)":
                sel_time_pct = st.slider(
                    "Top X% fastest finishers",
                    1, 50, 10, key="rl_time_pct",
                    help="For each (race_course, distance) bucket, keep "
                         "rows whose finish_time is in the fastest X%.")
            elif sel_time_mode == "Speed fig ≤":
                sel_speed_fig = st.slider(
                    "Speed fig threshold (z-score)",
                    -3.0, 1.0, -1.0, 0.1, key="rl_speed_fig",
                    help="Speed fig = (time − bucket mean) / bucket std, "
                         "where bucket = (course, distance, going). "
                         "Negative = faster than average. Threshold keeps "
                         "rows with speed_fig ≤ value.")

        # Row 6: outliers
        c1, c2 = st.columns([1, 2])
        with c1:
            sel_outliers_only = st.checkbox(
                "Outliers only", key="rl_outliers_only",
                help="Only show boilovers (long-priced winners) or flops "
                     "(short-priced losers). Useful for noise/edge audit.")
        with c2:
            sel_outlier_kind = []
            if sel_outliers_only:
                sel_outlier_kind = st.multiselect(
                    "Outlier kind",
                    ["Boilover (Pl=1, odds≥20)", "Flop (odds≤3, Pl≥6)"],
                    default=["Boilover (Pl=1, odds≥20)",
                             "Flop (odds≤3, Pl≥6)"],
                    key="rl_outlier_kind")

    # ── Apply filters ────────────────────────────────────────────────────
    df = df_all.copy()

    if date_mode == "Range" and sel_start and sel_end:
        df = df[(df["race_date"].dt.date >= sel_start) &
                (df["race_date"].dt.date <= sel_end)]
    elif date_mode == "Exact date" and sel_exact:
        df = df[df["race_date"].dt.date == sel_exact]
    elif date_mode == "Month" and sel_month:
        df = df[df["month"] == sel_month]

    if sel_horse_name:
        df = df[df["horse_name"].astype(str).str.upper().str.contains(
            sel_horse_name, na=False)]

    if sel_track:   df = df[df["race_track"].isin(sel_track)]
    if sel_course:  df = df[df["race_course"].isin(sel_course)]
    if sel_class:   df = df[df["race_class"].astype(str).isin([str(c) for c in sel_class])]
    if sel_dist:    df = df[df["distance_n"].isin(sel_dist)]
    if sel_going:   df = df[df["going"].astype(str).isin([str(g) for g in sel_going])]

    if sel_gate_mode == "Bands" and sel_gate_bands:
        df = df[df["gate_band"].isin(sel_gate_bands)]
    elif sel_gate_mode == "Specific" and sel_gates:
        df = df[df["draw_n"].isin(sel_gates)]

    if sel_rating_mode == "Bands" and sel_rating_bands:
        df = df[df["rating_band"].isin(sel_rating_bands)]
    elif sel_rating_mode == "Range" and sel_rating_min is not None:
        df = df[df["rating_n"].between(sel_rating_min, sel_rating_max)]

    if sel_jockey:  df = df[df["jockey"].isin(sel_jockey)]
    if sel_trainer: df = df[df["trainer"].isin(sel_trainer)]
    if sel_style:   df = df[df["run_style"].isin(sel_style)]

    if sel_finish == "Win (1)":
        df = df[df["place_n"] == 1]
    elif sel_finish == "Place (1-3)":
        df = df[df["place_n"].between(1, 3)]
    elif sel_finish == "Top-5":
        df = df[df["place_n"].between(1, 5)]
    elif sel_finish == "Specific" and sel_finish_specific:
        df = df[df["place_n"].isin(sel_finish_specific)]

    if sel_pace:
        df = df[df["pace_label"].isin(sel_pace)]

    if sel_time_mode == "Custom range" and sel_time_min is not None:
        df = df[df["finish_time_seconds"].between(sel_time_min, sel_time_max)]
    elif sel_time_mode == "Top X% in (course, distance)" and sel_time_pct:
        rank = df.groupby(["race_course", "distance_n"])["finish_time_seconds"] \
                 .rank(method="min", pct=True)
        df = df[rank <= (sel_time_pct / 100.0)]
    elif sel_time_mode == "Speed fig ≤" and sel_speed_fig is not None:
        df = df[df["speed_fig"] <= sel_speed_fig]

    if sel_outliers_only and sel_outlier_kind:
        masks = []
        if "Boilover (Pl=1, odds≥20)" in sel_outlier_kind:
            masks.append(df["is_boilover"])
        if "Flop (odds≤3, Pl≥6)" in sel_outlier_kind:
            masks.append(df["is_flop"])
        if masks:
            m = masks[0]
            for x in masks[1:]:
                m = m | x
            df = df[m]

    df = df.sort_values(["race_date", "race_number", "place_n"],
                        ascending=[False, True, True])

    # ── Summary panel ───────────────────────────────────────────────────
    n = len(df)
    n_races = df.groupby(["race_date_str", "race_number"]).ngroups if n else 0
    n_horses = df["horse_name"].nunique() if n else 0
    win_pct = (df["is_win"].sum() / n * 100.0) if n else 0.0
    plc_pct = (df["is_place"].sum() / n * 100.0) if n else 0.0

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Rows", f"{n:,}")
    k2.metric("Races", f"{n_races:,}")
    k3.metric("Distinct horses", f"{n_horses:,}")
    base_w = baselines.get("overall_win", 0) * 100
    k4.metric("Win %", f"{win_pct:.1f}%",
              delta=f"{win_pct - base_w:+.1f} vs baseline" if base_w else None)
    base_p = baselines.get("overall_place", 0) * 100
    k5.metric("Place %", f"{plc_pct:.1f}%",
              delta=f"{plc_pct - base_p:+.1f} vs baseline" if base_p else None)

    if n == 0:
        st.info("No rows match the current filters.")
        return

    # ── Sub-tabs ────────────────────────────────────────────────────────
    sub_results, sub_insights, sub_pivot, sub_outliers = st.tabs(
        ["📋 Results", "📈 Insights", "🧮 Pivot designer", "🎲 Outliers"]
    )

    # ─────────────────────────── Results ─────────────────────────────────
    with sub_results:
        disp = df.copy()
        disp["video"] = [
            _hkjc_video_url(d.replace("-", "/"), int(rn), tk)
            for d, rn, tk in zip(disp["race_date_str"],
                                  disp["race_number"],
                                  disp.get("race_track", [""] * len(disp)))
        ]
        cols_order = [
            "race_date_str", "race_number", "race_track", "race_course",
            "surface", "race_class", "distance_n", "going", "pace_label",
            "field_size",
            "place", "horse_name", "draw", "rating", "actual_weight",
            "jockey", "trainer", "run_style", "running_positions",
            "finish_time_seconds", "speed_fig", "lbw", "win_odds", "gear",
            "video", "horse_url",
        ]
        cols_order = [c for c in cols_order if c in disp.columns]
        disp = disp[cols_order].rename(columns={
            "race_date_str": "Date", "race_number": "R",
            "race_track": "Track", "race_course": "Course",
            "surface": "Surface", "race_class": "Class",
            "distance_n": "Dist", "going": "Going",
            "pace_label": "Pace", "field_size": "Fld",
            "place": "Pl", "horse_name": "Horse",
            "draw": "Gate", "rating": "RT",
            "actual_weight": "Wt", "jockey": "Jockey",
            "trainer": "Trainer", "run_style": "Style",
            "running_positions": "Run pos.",
            "finish_time_seconds": "Time(s)", "speed_fig": "SpdFig",
            "lbw": "LBW", "win_odds": "Odds", "gear": "Gear",
            "video": "▶ Replay", "horse_url": "🐴 HKJC",
        })

        st.dataframe(
            disp, width='stretch', hide_index=True,
            height=min(900, 120 + 35 * min(len(disp), 22)),
            column_config={
                "▶ Replay": st.column_config.LinkColumn(
                    "▶ Replay", display_text="▶ Watch", width="small"),
                "🐴 HKJC": st.column_config.LinkColumn(
                    "🐴 HKJC", display_text="open", width="small"),
                "Time(s)": st.column_config.NumberColumn(format="%.2f"),
                "SpdFig":  st.column_config.NumberColumn(
                    format="%+.2f",
                    help="Speed figure z-score (negative = faster than "
                         "course/distance/going bucket mean)."),
            },
        )

        csv = disp.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Download CSV", data=csv,
                           file_name="race_lookup.csv", mime="text/csv",
                           width='content')

        # ── Form-line drilldown: pick a horse from the slice ───────────
        st.markdown("##### Drill into form line")
        unique_horses = sorted(df["horse_name"].dropna().unique().tolist())
        pick_horse = st.selectbox(
            "Horse", ["—"] + unique_horses[:2000],
            key="rl_drill_horse",
            help="Picks any horse in the current slice; displays their "
                 "last 6 runs from the *full* DB (not filtered).")
        if pick_horse and pick_horse != "—":
            hist = df_all[df_all["horse_name"] == pick_horse] \
                       .sort_values("race_date", ascending=False).head(6)
            if hist.empty:
                st.info("No history.")
            else:
                hist = hist.assign(video=[
                    _hkjc_video_url(d.replace("-", "/"), int(rn), tk)
                    for d, rn, tk in zip(hist["race_date_str"],
                                          hist["race_number"],
                                          hist.get("race_track",
                                                   [""] * len(hist)))
                ])
                show_cols = ["race_date_str", "race_number", "race_track",
                             "race_course", "race_class", "distance_n",
                             "going", "pace_label", "place", "draw",
                             "jockey", "trainer", "run_style",
                             "running_positions", "finish_time_seconds",
                             "speed_fig", "lbw", "win_odds", "video"]
                show_cols = [c for c in show_cols if c in hist.columns]
                st.dataframe(
                    hist[show_cols].rename(columns={
                        "race_date_str": "Date", "race_number": "R",
                        "race_track": "Trk", "race_course": "Crs",
                        "race_class": "Cls", "distance_n": "Dist",
                        "going": "Going", "pace_label": "Pace",
                        "place": "Pl", "draw": "Gate", "jockey": "Jky",
                        "trainer": "Trn", "run_style": "Style",
                        "running_positions": "Run pos.",
                        "finish_time_seconds": "Time(s)",
                        "speed_fig": "SpdFig", "lbw": "LBW",
                        "win_odds": "Odds", "video": "▶ Replay",
                    }),
                    width='stretch', hide_index=True,
                    column_config={
                        "▶ Replay": st.column_config.LinkColumn(
                            "▶ Replay", display_text="▶", width="small"),
                        "Time(s)": st.column_config.NumberColumn(format="%.2f"),
                        "SpdFig": st.column_config.NumberColumn(format="%+.2f"),
                    },
                )

    # ─────────────────────────── Insights ────────────────────────────────
    with sub_insights:
        def _agg(group_cols, top=20, baseline_map=None, baseline_n_map=None):
            g = df.groupby(group_cols, dropna=False).agg(
                runs=("horse_name", "size"),
                wins=("is_win", "sum"),
                places=("is_place", "sum"),
                avg_finish=("place_n", "mean"),
                avg_win_odds=("win_odds_n",
                              lambda s: s[df.loc[s.index, "is_win"]].mean()),
            ).reset_index()
            g["win%"] = (g["wins"] / g["runs"] * 100).round(1)
            g["plc%"] = (g["places"] / g["runs"] * 100).round(1)
            g["avg_finish"] = g["avg_finish"].round(2)
            g["avg_win_odds"] = g["avg_win_odds"].round(2)
            if baseline_map and len(group_cols) == 1:
                key = group_cols[0]
                g["base_win%"] = g[key].map(
                    lambda v: round((baseline_map.get(v) or 0) * 100, 1))
                g["Δ vs base"] = (g["win%"] - g["base_win%"]).round(1)
            g = g[g["runs"] >= 3].sort_values(["wins", "win%"],
                                              ascending=[False, False]).head(top)
            return g

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**By gate band**")
            st.dataframe(_agg(["gate_band"], 20),
                         width='stretch', hide_index=True)
            st.markdown("**By running style (profile)**")
            st.dataframe(_agg(["run_style"], 20),
                         width='stretch', hide_index=True)
            st.markdown("**By rating band**")
            st.dataframe(_agg(["rating_band"], 20),
                         width='stretch', hide_index=True)
            st.markdown("**By distance**")
            st.dataframe(_agg(["distance_n"], 20),
                         width='stretch', hide_index=True)
        with c2:
            st.markdown("**Top jockeys (vs their full-DB baseline)**")
            st.dataframe(_agg(["jockey"], 20,
                              baselines.get("jockey_win"),
                              baselines.get("jockey_n")),
                         width='stretch', hide_index=True)
            st.markdown("**Top trainers (vs their full-DB baseline)**")
            st.dataframe(_agg(["trainer"], 20,
                              baselines.get("trainer_win"),
                              baselines.get("trainer_n")),
                         width='stretch', hide_index=True)
            st.markdown("**Top jockey × trainer combos**")
            st.dataframe(_agg(["jockey", "trainer"], 30),
                         width='stretch', hide_index=True)
            st.markdown("**By race pace**")
            st.dataframe(_agg(["pace_label"], 20),
                         width='stretch', hide_index=True)

        # People combos
        st.markdown("##### People × Horse combos (in slice)")
        cc1, cc2 = st.columns(2)
        with cc1:
            st.markdown("**Top jockey × horse**")
            st.dataframe(_agg(["jockey", "horse_name"], 30),
                         width='stretch', hide_index=True)
        with cc2:
            st.markdown("**Top trainer × horse**")
            st.dataframe(_agg(["trainer", "horse_name"], 30),
                         width='stretch', hide_index=True)

        st.markdown("**Course-specialist jockeys (jockey × race_course)**")
        st.dataframe(_agg(["jockey", "race_course"], 30),
                     width='stretch', hide_index=True)

        # Bias matrices with baseline-deviation colouring
        st.markdown("##### Bias matrix — win% by (gate band × distance)")
        bias = (df.assign(_w=df["is_win"].astype(int))
                  .pivot_table(index="gate_band", columns="distance_n",
                               values="_w", aggfunc="mean") * 100).round(2)
        # Build deviation matrix vs full-DB baseline for the same cell
        cdg_base = baselines.get("course_dist_gate", {})
        base_full = (df_all.assign(_w=df_all["is_win"].astype(int))
                       .pivot_table(index="gate_band", columns="distance_n",
                                    values="_w", aggfunc="mean") * 100).round(2)
        try:
            dev = (bias - base_full).round(2)
            st.dataframe(
                dev.style.format("{:+.1f}").background_gradient(
                    cmap="RdYlGn", vmin=-15, vmax=15, axis=None),
                width='stretch',
            )
            st.caption("Cells = (slice win%) − (full-DB win% for same "
                       "gate-band × distance). Green = positive bias, "
                       "red = negative. Empty = bucket not in slice.")
        except Exception as e:
            st.dataframe(bias.style.format("{:.1f}%"),
                         width='stretch')
            st.caption(f"Heatmap unavailable: {e}")

        if df["pace_label"].astype(bool).any():
            st.markdown("##### Bias matrix — win% by (running style × race pace)")
            bias2 = (df.assign(_w=df["is_win"].astype(int))
                       .pivot_table(index="run_style", columns="pace_label",
                                    values="_w", aggfunc="mean") * 100).round(2)
            try:
                st.dataframe(
                    bias2.style.format("{:.1f}%").background_gradient(
                        cmap="RdYlGn", vmin=0, vmax=25, axis=None),
                    width='stretch',
                )
            except Exception:
                st.dataframe(bias2.style.format("{:.1f}%"),
                             width='stretch')

    # ───────────────────────── Pivot designer ────────────────────────────
    with sub_pivot:
        st.markdown("Build any 2-D win-rate matrix from the slice. "
                    "Rows × Columns × Metric — pick any combination.")
        groupable = ["race_track", "race_course", "surface", "race_class",
                     "distance_n", "going", "pace_label", "gate_band",
                     "rating_band", "run_style", "jockey", "trainer",
                     "horse_name", "draw_n"]
        groupable = [c for c in groupable if c in df.columns]
        cc1, cc2, cc3, cc4 = st.columns(4)
        with cc1:
            piv_x = st.selectbox("Rows", groupable,
                                 index=groupable.index("gate_band")
                                 if "gate_band" in groupable else 0,
                                 key="rl_piv_x")
        with cc2:
            piv_y = st.selectbox("Columns", groupable,
                                 index=groupable.index("distance_n")
                                 if "distance_n" in groupable else 0,
                                 key="rl_piv_y")
        with cc3:
            piv_metric = st.selectbox(
                "Metric",
                ["win%", "place%", "runs", "avg_finish",
                 "avg_win_odds", "avg_speed_fig"],
                key="rl_piv_metric")
        with cc4:
            piv_minn = st.slider("Min runs / cell", 1, 50, 5,
                                 key="rl_piv_minn")
        if piv_x == piv_y:
            st.info("Pick two different fields for rows and columns.")
        else:
            try:
                if piv_metric == "win%":
                    pv = (df.assign(_v=df["is_win"].astype(int))
                            .pivot_table(index=piv_x, columns=piv_y,
                                         values="_v", aggfunc="mean") * 100).round(2)
                elif piv_metric == "place%":
                    pv = (df.assign(_v=df["is_place"].astype(int))
                            .pivot_table(index=piv_x, columns=piv_y,
                                         values="_v", aggfunc="mean") * 100).round(2)
                elif piv_metric == "runs":
                    pv = df.pivot_table(index=piv_x, columns=piv_y,
                                        values="horse_name", aggfunc="count")
                elif piv_metric == "avg_finish":
                    pv = df.pivot_table(index=piv_x, columns=piv_y,
                                        values="place_n", aggfunc="mean").round(2)
                elif piv_metric == "avg_win_odds":
                    pv = df[df["is_win"]].pivot_table(
                        index=piv_x, columns=piv_y,
                        values="win_odds_n", aggfunc="mean").round(2)
                else:  # avg_speed_fig
                    pv = df.pivot_table(index=piv_x, columns=piv_y,
                                        values="speed_fig",
                                        aggfunc="mean").round(2)
                # Mask cells with too few rows
                cnt = df.pivot_table(index=piv_x, columns=piv_y,
                                     values="horse_name", aggfunc="count")
                pv = pv.where(cnt >= piv_minn)
                fmt = ("{:.1f}%" if piv_metric in ("win%", "place%")
                       else "{:+.2f}" if piv_metric == "avg_speed_fig"
                       else "{:.2f}" if piv_metric in ("avg_finish",
                                                      "avg_win_odds")
                       else "{:.0f}")
                cmap = ("RdYlGn" if piv_metric in
                        ("win%", "place%") else "RdYlGn_r"
                        if piv_metric in ("avg_finish",) else "viridis")
                try:
                    st.dataframe(
                        pv.style.format(fmt, na_rep="—")
                          .background_gradient(cmap=cmap, axis=None),
                        width='stretch',
                    )
                except Exception:
                    st.dataframe(pv.style.format(fmt, na_rep="—"),
                                 width='stretch')
            except Exception as e:
                st.error(f"Pivot failed: {e}")

    # ───────────────────────── Outliers ──────────────────────────────────
    with sub_outliers:
        st.markdown(
            "Auto-flagged rows that may distort analytics — review for "
            "noise vs genuine pattern."
        )
        boil = df[df["is_boilover"]].copy()
        flop = df[df["is_flop"]].copy()
        c1, c2 = st.columns(2)
        c1.metric("Boilovers (Pl=1, odds≥20)", f"{len(boil):,}")
        c2.metric("Flops (odds≤3, Pl≥6)", f"{len(flop):,}")

        st.markdown(f"**Boilovers — {len(boil)} rows**")
        cols = ["race_date_str", "race_number", "race_track", "race_course",
                "race_class", "distance_n", "going", "horse_name",
                "draw", "jockey", "trainer", "run_style", "win_odds",
                "place"]
        cols = [c for c in cols if c in boil.columns]
        st.dataframe(boil[cols].head(200), width='stretch',
                     hide_index=True)

        st.markdown(f"**Flops — {len(flop)} rows**")
        cols = ["race_date_str", "race_number", "race_track", "race_course",
                "race_class", "distance_n", "going", "horse_name",
                "draw", "jockey", "trainer", "run_style", "win_odds",
                "place"]
        cols = [c for c in cols if c in flop.columns]
        st.dataframe(flop[cols].head(200), width='stretch',
                     hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# Agent Skills — live mirror of the VS Code Copilot skills under
# .github/prompts/skills/. The agent and this page both call into
# agent_skills.py so there is one source of truth.
# ══════════════════════════════════════════════════════════════════════════════

def page_agent_skills():
    """Surface every repo-scoped agent skill as a one-click dashboard action."""
    import agent_skills as _ask

    st.markdown("## 🤖 Agent Skills")
    st.caption(
        "These are the same skills the VS Code Copilot agent uses, exposed "
        "here as buttons so the live dashboard can trigger them too. Logic "
        "lives in `agent_skills.py`; prose lives in "
        "`.github/prompts/skills/<name>/SKILL.md`."
    )

    skill_titles = [s["title"] for s in _ask.SKILLS]
    tabs = st.tabs(skill_titles)

    # ── 1. Pre-Race Meeting Run ────────────────────────────────────────────
    with tabs[0]:
        st.markdown("### Pre-Race Meeting Run")
        st.caption("Runs the full pre-race pipeline for one meeting.")
        c1, c2, c3 = st.columns(3)
        with c1:
            d = st.date_input("Race date", value=date.today(),
                              key="askill_prerace_date")
        with c2:
            going_turf = st.text_input("Turf going", value="Good",
                                       key="askill_prerace_turf")
        with c3:
            going_awt = st.text_input("AWT going", value="Good",
                                      key="askill_prerace_awt")
        skip_scrape = st.checkbox(
            "Skip scrape (use existing racecard)",
            value=_ask._auto_skip_scrape(d.isoformat()),
            key="askill_prerace_skipscrape",
        )
        if st.button("▶ Run pre-race pipeline", type="primary",
                     key="askill_prerace_go"):
            with st.spinner("Running run_meeting.py — can take several minutes…"):
                res = _ask.run_prerace_meeting(
                    d.isoformat(),
                    going_turf=going_turf,
                    going_awt=going_awt,
                    skip_scrape=skip_scrape,
                )
            m1, m2 = st.columns(2)
            m1.metric("Exit code", res.returncode)
            m2.metric("Warnings", len(res.warnings))
            if res.pdf_path:
                st.success(f"PDF: `{res.pdf_path}`")
            if res.txt_path:
                st.success(f"TXT: `{res.txt_path}`")
            for w in res.warnings:
                st.warning(w)
            with st.expander("Tail of run_meeting.py stdout", expanded=False):
                st.code(res.tail_stdout or "(empty)", language="text")

    # ── 2. Post-Race Pipeline ──────────────────────────────────────────────
    with tabs[1]:
        st.markdown("### Post-Race Pipeline")
        st.caption(
            "Belt-and-braces DB append + auto form-guide rebuild for any "
            "meeting in the next 14 days. Codified after the 9+13 May 2026 "
            "backfill (routine housekeeping — rain-affected going, results "
            "not yet appended to DB)."
        )
        d2 = st.date_input("Race date", value=date.today(),
                           key="askill_postrace_date")
        if st.button("▶ Run post-race pipeline", type="primary",
                     key="askill_postrace_go"):
            with st.spinner("Appending results, rebuilding caches…"):
                res = _ask.run_postrace_pipeline(d2.isoformat())
            m1, m2, m3 = st.columns(3)
            m1.metric("Rows appended", res.db_rows_appended)
            m2.metric("Form guides rebuilt", len(res.form_guides_rebuilt))
            m3.metric("Warnings", len(res.warnings))
            if res.form_guides_rebuilt:
                st.success("Rebuilt: " + ", ".join(res.form_guides_rebuilt))
            for w in res.warnings:
                st.warning(w)
            if res.smap_diag_path:
                st.info(f"Pace diagnostic: `{res.smap_diag_path}`")

    # ── 3. Python Env Enforcer ─────────────────────────────────────────────
    with tabs[2]:
        st.markdown("### Python Env Enforcer")
        st.caption(
            "Generate the canonical PowerShell command for any script — "
            "uses miniconda interpreter and forces UTF-8 stdio encoding."
        )
        script = st.text_input("Script", value="run_meeting.py",
                               key="askill_env_script")
        args = st.text_input("Args (space-separated)",
                             value="--date 2026-05-17 --skip-scrape",
                             key="askill_env_args")
        tail = st.number_input("Tail N lines (0 = full)", value=60, min_value=0,
                               key="askill_env_tail")
        cmd = _ask.build_python_cmd(
            script, args.split() if args else [],
            tail=int(tail) or None,
        )
        st.code(cmd, language="powershell")

    # ── 4. Scrape Doctor ───────────────────────────────────────────────────
    with tabs[3]:
        st.markdown("### Scrape Doctor")
        st.caption("Inspect today's (or any) racecard xlsx and diagnose problems.")
        d4 = st.date_input("Race date", value=date.today(),
                           key="askill_doctor_date")
        if st.button("▶ Diagnose", type="primary", key="askill_doctor_go"):
            with st.spinner("Inspecting racecard…"):
                report = _ask.diagnose_scrape(d4.isoformat())
            m1, m2 = st.columns(2)
            m1.metric("Racecard exists", "✔" if report["racecard_exists"] else "✘")
            m2.metric("Size (KB)", report["racecard_size_kb"])
            st.markdown(f"**Suggested action:** {report['suggested_action']}")
            if report.get("field_size_per_race"):
                st.markdown("**Field size per race**")
                st.json(report["field_size_per_race"])
            if report.get("standby_contamination"):
                st.warning(
                    f"Possible standby contamination: "
                    f"{', '.join(report['standby_contamination'][:10])}"
                    + ("…" if len(report['standby_contamination']) > 10 else "")
                )
            with st.expander("Full report (JSON)"):
                st.json(report)

    # ── 5. Memory Curator ──────────────────────────────────────────────────
    with tabs[4]:
        st.markdown("### Memory Curator")
        st.caption(
            "Compose a memory note. The dashboard proposes a diff — actual "
            "writes happen via the VS Code Copilot `memory` tool (or you "
            "paste it into the appropriate `/memories/` file)."
        )
        scope = st.selectbox(
            "Scope",
            ["repo", "user", "session"],
            index=0,
            key="askill_mem_scope",
            help="repo = project-scoped (recommended for HKJC facts), "
                 "user = global preferences, session = current chat only.",
        )
        notes = st.text_area("Notes", height=200, key="askill_mem_notes",
                             placeholder="e.g. \"smap_postrace_diagnostic.py "
                             "must be run within 24 h of the meeting or its "
                             "cache invalidates.\"")
        if st.button("▶ Propose memory diff", type="primary",
                     key="askill_mem_go"):
            if not notes.strip():
                st.warning("Notes are empty — nothing to propose.")
            else:
                proposal = _ask.curate_session_memory(notes, scope=scope)
                st.success(f"Proposed target: `{proposal['target_dir']}`")
                st.code(proposal["proposal"], language="markdown")
                st.caption(proposal["next_action"])

    # ── Footer: links to underlying SKILL.md files ─────────────────────────
    st.markdown("---")
    st.markdown("**Skill definitions (read-only docs):**")
    for s in _ask.SKILLS:
        st.markdown(f"- `{s['skill_md']}` — {s['summary']}")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point — page router
# ══════════════════════════════════════════════════════════════════════════════



def page_bet_optimizer():
    """Integrated per-card betting strategy — leans into the place-reliable
    QPL banker-parlay structure that has actually made money in this account.
    Backed by bet_optimizer.optimise_card (single source of truth)."""
    import bet_optimizer as _bo

    st.markdown('<div class="page-title">Bet Optimizer</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">Profitable-by-design slate &middot; '
                'QPL bankers + all-up jackpot</div>', unsafe_allow_html=True)

    meetings = load_available_meetings()
    if not meetings:
        st.info("No meeting reports found yet. Generate a race-day report first.")
        return

    date_opts = [m["date_str"] for m in meetings]
    def _fmt(ds: str) -> str:
        return f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        sel_ds = st.selectbox("Meeting", date_opts, index=0, format_func=_fmt,
                              key="bo_date")
    with c2:
        bankroll = st.number_input("Bankroll (HK$)", min_value=100.0,
                                   max_value=100000.0, value=500.0, step=100.0,
                                   key="bo_bankroll")
    with c3:
        goal = st.selectbox("Goal", ["upside", "balanced", "grind"], index=0,
                            key="bo_goal",
                            help="upside = lean into all-up jackpots (accept "
                                 "losing weeks); grind = protect bankroll, bank "
                                 "the QPL edge; balanced = both.")

    date_iso = _fmt(sel_ds)
    res = _bo.optimise_card(date_iso, float(bankroll), goal)
    if not res.get("ok"):
        st.warning(res.get("reason", "Could not build a slate for this meeting."))
        return

    # ── headline ───────────────────────────────────────────────────────────
    st.markdown(f"### {res['meeting_title'] or _fmt(sel_ds)}")
    st.caption(f"Model {res['model_version']}  ·  {len(res['race_analyses'])} races "
               f"with a usable banker  ·  goal **{res['risk_goal']}**")

    bs, sp = res["budget_split"], res["spent"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Bankroll", f"${res['bankroll']:.0f}")
    m2.metric("Jackpot slice", f"${bs['jackpot']:.0f}", f"spent ${sp['jackpot']:.0f}")
    m3.metric("Singles slice", f"${bs['singles']:.0f}", f"spent ${sp['singles']:.0f}")
    m4.metric("Total staked", f"${sp['total']:.0f}",
              f"reserve ${bs['reserve']:.0f}")

    st.info(
        "**Why this shape?** Your record (729 bets) shows QPL place pools are the "
        "only structure with positive ROI (+13%), and your one big win — the 27 May "
        "$25,003 — was QPL bankers compounded in an all-up. This slate multiplies "
        "that edge and cuts the leaks (straight QIN −26%, exotic boxes −100%).")

    # ── jackpot ticket ───────────────────────────────────────────────────────
    st.markdown("#### 🎰 Jackpot — QPL All-Up Banker Parlay")
    jp = res["jackpot"]
    if jp:
        jc1, jc2, jc3, jc4 = st.columns(4)
        jc1.metric("Shape", jp.get("shape_label", "?"))
        jc2.metric("Units", f"{jp['n_units']}")
        jc3.metric("Stake", f"${jp['total_stake']:.0f}",
                   f"${jp['stake_per_unit']:.0f}/unit")
        jc4.metric("Est. hit", f"{jp.get('est_hit_prob', 0)*100:.1f}%")
        leg_rows = []
        for lg in jp["legs_detail"]:
            leg_rows.append({
                "Race": f"R{lg['race_no']}",
                "Banker": f"#{lg['_banker']} {lg['_banker_name']}",
                "Floats": ", ".join(f"#{x}" for x in lg["_floats"]),
                "Bank top-3": f"{lg['_banker_place_prob']*100:.0f}%",
            })
        st.dataframe(leg_rows, hide_index=True, width='stretch')
        st.caption(f"Pool **QPL** · legs {', '.join('R'+str(l['race_no']) for l in jp['legs_detail'])}. "
                   "A leg wins when its banker AND one float both finish top-3.")
    else:
        st.warning("Not enough reliable bankers (after excluding rating-FADE horses) "
                   "to build a parlay for this card.")

    # ── per-race singles ─────────────────────────────────────────────────────
    st.markdown("#### 🎯 Per-Race QPL Banker Singles")
    if res["singles"]:
        rows = []
        for s in res["singles"]:
            rows.append({
                "Race": f"R{s['race_number']}",
                "Banker": f"#{s['banker']} {s['banker_name']}",
                "Floats": ", ".join(f"#{x}" for x in s["floats"]),
                "Stake": f"${s['total_stake']:.0f}",
                "Bank top-3": f"{s['banker_place_prob']*100:.0f}%",
                "Cycle": s["cycle_flag"],
                "Why": s["rationale"],
            })
        st.dataframe(rows, hide_index=True, width='stretch')
    else:
        st.write("(none)")

    # ── confidence board (all races) ─────────────────────────────────────────
    with st.expander("📊 Confidence board — every race ranked by banker quality"):
        board = []
        for r in sorted(res["race_analyses"], key=lambda x: -x["banker_quality"]):
            b = r["banker"]
            board.append({
                "Race": f"R{r['race_number']}",
                "Top banker": f"#{b['horse_no']} {b['horse_name']}",
                "Top-3 %": f"{b['place_prob']*100:.0f}%",
                "Dominance": f"{r['dominance']:.2f}",
                "Quality": f"{r['banker_quality']:.2f}",
                "Cycle": b["cycle_flag"],
            })
        st.dataframe(board, hide_index=True, width='stretch')

    # ── log this slate to My Bets ────────────────────────────────────────────
    st.markdown("#### 📝 Log this slate")
    already = _bo.already_logged(res)
    lc1, lc2, lc3 = st.columns([1, 1, 2])
    with lc1:
        log_singles = st.checkbox("Singles", value=True, key="bo_log_singles")
    with lc2:
        log_jack = st.checkbox("Jackpot all-up", value=True, key="bo_log_jack")
    with lc3:
        if already:
            st.caption("This slate is already in **My Bets** (logged earlier).")
        if st.button("➕ Add to My Bets", key="bo_log_btn",
                     disabled=already, width='stretch'):
            out = _bo.log_slate_to_user_bets(res, include_singles=log_singles,
                                             include_jackpot=log_jack)
            if out.get("skipped"):
                st.info("Already logged — nothing added.")
            else:
                st.success(f"Logged {out['logged']} bet(s) to My Bets. The all-up "
                           "stays OPEN until you reconcile the bookie statement.")
                st.cache_data.clear()

    # ── live value / drift ───────────────────────────────────────────────────
    with st.expander("📡 Live value — bankers the market under-rates"):
        market, src = _bo_market_win_probs(sel_ds, res)
        if not market:
            st.caption("No live or SP odds for this meeting yet. Open the "
                       "**Live (Feed + Odds)** page on race day to populate odds, then "
                       "value flags appear here.")
        else:
            st.caption(f"Market source: **{src}** odds, overround-normalised per race.")
            flags = _bo.compute_drift_flags(res, market)
            if flags:
                for f in flags:
                    st.success(f["msg"])
            else:
                st.caption("No banker currently offers place-value vs the market.")

    # ── backtest — does this slate actually make money? ──────────────────────
    with st.expander("🧪 Backtest — replay this slate over settled meetings"):
        st.caption("Replays the optimiser across every meeting that has both a "
                   "report and settled dividends. Singles settle from QPL "
                   "dividends; the jackpot via the all-up settler.")
        if st.button("Run backtest (all goals)", key="bo_bt_btn"):
            with st.spinner("Replaying April–May…"):
                bt_rows = []
                for g in ("grind", "balanced", "upside"):
                    b = _bo.backtest_optimiser(None, float(bankroll), g)
                    if b.get("ok"):
                        bt_rows.append({
                            "Goal": g,
                            "Meetings": b["n_meetings"],
                            "Staked": f"${b['total_stake']:.0f}",
                            "P/L": f"${b['total_pnl']:+.0f}",
                            "ROI": f"{b['roi_pct']:+.1f}%",
                            "Jackpot hits": f"{b['jackpot_hits']}/{b['n_meetings']}",
                        })
                st.session_state["bo_bt_rows"] = bt_rows
        if st.session_state.get("bo_bt_rows"):
            st.dataframe(st.session_state["bo_bt_rows"], hide_index=True,
                         width='stretch')
            st.warning(
                "**Reality check:** over the settled April–May sample every goal "
                "currently shows negative ROI and the all-up jackpot rarely lands. "
                "Treat the jackpot as a small-stake lottery; the durable edge is the "
                "QPL place bankers. Use **grind** to protect bankroll and lean on the "
                "Multi Builder's contrarian banker (rank 2–6) tab for the +ROI shape.")

    # ── copy-out ─────────────────────────────────────────────────────────────
    with st.expander("📋 Plain-text slate (copy to bookie / notes)"):
        st.code(_bo.format_card(res), language="text")


def _bo_market_win_probs(date_compact: str, res: dict) -> tuple[dict, str]:
    """Market win-prob map {race_no: {horse_no: win_prob_pct}}, overround-
    normalised. Prefers scraped LIVE odds (cache/live_odds); falls back to the
    report SP. Returns (map, source_label); ({}, "") when no odds at all."""
    import bet_optimizer as _bo
    live = _bo.market_from_live_odds(date_compact)
    if live:
        return live, "live"
    try:
        meetings = load_available_meetings()
        mf = next((m for m in meetings if m["date_str"] == date_compact), None)
        if mf:
            data = load_meeting_data(mf["file"])
            sp = _bo.market_from_report_sp(data)
            if sp:
                return sp, "SP (report)"
    except Exception:
        pass
    return {}, ""


def page_betting():
    """Unified Betting workspace — the three former betting pages as tabs:
    the auto Optimizer slate, the filter-driven Model Bets, and the manual
    Multi Builder. bet_optimizer.py is the shared engine."""
    st.markdown('<div class="page-title">Betting</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">One workspace &middot; auto slate, '
                'model tickets &amp; manual builder</div>', unsafe_allow_html=True)
    tab_opt, tab_model, tab_multi = st.tabs([
        "💡 Optimizer", "🎯 Model Bets", "🧮 Multi Builder",
    ])
    with tab_opt:
        page_bet_optimizer()
    with tab_model:
        page_model_bets()
    with tab_multi:
        page_multi_builder()


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

    # ── Navigation (grouped by workflow stage) ─────────────────────────────
    NAV_GROUPS = [
        ("Pre-Race", [
            ("Race Day Insight", "🏁 Race Day Insight"),
            ("Form Guide",     "📖 Form Guide"),
            ("Model Analysis", "📊 Model Analysis"),
            ("Race Lookup",    "🔎 Race Lookup"),
            ("Talent Watch",   "🔭 Talent Watch"),
            ("Trials",         "🎽 Trials"),
            ("Fixtures",       "🗓️ Fixtures"),
        ]),
        ("Betting", [
            ("Betting",        "💡 Betting"),
            ("My Bets",        "💰 My Bets"),
            ("Blackbook",      "📓 Blackbook"),
            ("Live",           "📡 Live (Feed + Odds)"),
        ]),
        ("Post-Race", [
            ("Results",        "🏆 Results"),
            ("Data Analysis",  "🔬 Data Analysis"),
        ]),
        ("Lab & Tools", [
            ("Framework Lab",  "🧪 Framework Lab"),
            ("Model Lab",      "🧠 Model Lab"),
            ("PDF Builder",    "📄 PDF Builder"),
            ("Agent Skills",   "🤖 Agent Skills"),
        ]),
    ]
    NAV_ITEMS = [item for _grp, _items in NAV_GROUPS for item in _items]
    if "nav_page" not in st.session_state:
        st.session_state["nav_page"] = "Race Day Insight"
    # Migrate old nav-keys if persisted
    if st.session_state["nav_page"] == "Race Day":
        st.session_state["nav_page"] = "Model Analysis"
    if st.session_state["nav_page"] == "Overview":
        st.session_state["nav_page"] = "Race Day Insight"
    if st.session_state["nav_page"] in ("Live Feed", "Live Odds"):
        st.session_state["nav_page"] = "Live"
    if st.session_state["nav_page"] in ("Bet Optimizer", "Model Bets", "Multi Builder"):
        st.session_state["nav_page"] = "Betting"

    for _grp_name, _grp_items in NAV_GROUPS:
        st.sidebar.markdown(
            f'<div class="sb-nav-section">{_grp_name}</div>',
            unsafe_allow_html=True,
        )
        for page_name, label in _grp_items:
            is_active = st.session_state["nav_page"] == page_name
            wrap_cls = "sb-active" if is_active else ""
            st.sidebar.markdown(f'<div class="{wrap_cls}">', unsafe_allow_html=True)
            if st.sidebar.button(label, key=f"nav_{page_name}", width='stretch'):
                st.session_state["nav_page"] = page_name
                st.rerun()
            st.sidebar.markdown('</div>', unsafe_allow_html=True)

    st.sidebar.markdown('<hr class="sb-divider">', unsafe_allow_html=True)

    page = st.session_state["nav_page"]

    # Global meeting context bar (shows the active meeting on every page).
    _render_meeting_ctx_bar()

    if page == "Race Day Insight":
        page_overview()
    elif page == "Betting":
        page_betting()
    elif page == "Model Analysis":
        selected = sidebar_race_day()
        model_view = st.radio(
            "Model Analysis view",
            ["🏁 Race Day Picks", "⚖️ Model Comparison"],
            horizontal=True,
            key="model_analysis_view",
            label_visibility="collapsed",
        )
        if model_view.startswith("🏁"):
            page_race_day(selected)
        else:
            page_model_comparison()
    elif page == "Data Analysis":
        page_data_analysis()
    elif page == "Horse Profile":
        # Legacy nav key — redirect to Race Lookup (which hosts Horse Profile as a tab).
        st.session_state["nav_page"] = "Race Lookup"
        page_race_lookup()

    elif page == "Race Lookup":

        page_race_lookup()

    elif page == "Framework Lab":

        page_framework_lab()
    elif page == "Live":
        tab_feed, tab_odds = st.tabs([
            "📡 Live Feed", "💹 Live Odds",
        ])
        with tab_feed:
            page_live_feed()
        with tab_odds:
            page_live_odds()
    elif page == "My Bets":
        page_my_bets()
    elif page == "Form Guide":
        page_form_guide()
    elif page == "Talent Watch":
        page_talent_watch()
    elif page == "Trials":
        page_trials()
    elif page == "Fixtures":
        page_fixtures()
    elif page == "Model Lab":
        page_model_lab()
    elif page == "Backtest":
        page_backtest()
    elif page == "Results":
        page_results()
    elif page == "Blackbook":
        page_blackbook()
    elif page == "PDF Builder":
        page_pdf_builder()
    elif page == "Agent Skills":
        page_agent_skills()

    # Push notifications panel (ntfy.sh / Chrome Web Push)
    _render_push_sidebar()

    # Cloud persistence status panel — appears at the bottom of the sidebar
    # on every page so the user always knows whether data is being synced
    # to GitHub (i.e. will survive a reboot).
    _render_persistence_sidebar()


def sidebar_race_day():
    """Race Day sidebar controls."""
    st.sidebar.markdown('<div class="sb-nav-section">Run Analysis</div>', unsafe_allow_html=True)
    col1, col2 = st.sidebar.columns(2)
    with col1:
        run_date = st.date_input("Race Date", value=date.today(), key="run_date")
    date_iso = run_date.isoformat()
    date_compact = date_iso.replace("-", "")
    _has_racecard = (BASE / "racecards" / f"racecard_{date_compact}.xlsx").exists()
    with col2:
        no_cache = st.checkbox(
            "Re-scrape",
            value=False,
            key="no_cache",
            help="Force a fresh HKJC scrape for scratches/substitutes. Leave off "
                 "to reuse the existing racecard and run the faster core ET/SARR path.",
        )
    run_fuse_after = st.sidebar.checkbox(
        "Also refresh FUSE",
        value=False,
        key="run_fuse_after_analysis",
        help="Usually leave off: refresh FUSE from the Race Day Insight FUSE table after live odds are scraped.",
    )

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
    _uploaded_for_this_date = (
        st.session_state.get("_uploaded_rc_date") == date_iso
    )

    if st.sidebar.button("[ RUN ANALYSIS ]", type="primary", width='stretch'):
        # Fast default: reuse an existing racecard. Tick Re-scrape when late
        # scratches/substitutes need to be pulled from HKJC again.
        skip = _has_racecard and not no_cache
        if _uploaded_for_this_date and not no_cache:
            skip = True
        # Clear Streamlit caches BEFORE run_pipeline (which may internally
        # rerun) so we don't race the cache clear against the rerun call.
        try:
            st.cache_data.clear()
        except Exception:
            pass
        run_pipeline(date_iso, no_cache, going_turf, going_awt,
                 skip_scrape=skip, run_fuse=run_fuse_after)

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
