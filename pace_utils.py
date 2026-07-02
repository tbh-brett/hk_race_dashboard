"""Shared pace utilities for HKJC race analysis.

Single source of truth for:
  - HKJC standard-time table (venue_surface, distance, class → total / early-sectional)
  - Going normalisation (scraper codes → canonical labels)
  - Going offsets (seconds adjustment vs 'Good')
  - Actual race-pace computation from a result-JSON race dict
  - Pace classification bands (7-band scale)
  - Running-style classifier from HKJC running-position calls
  - Persistent pace index (cache/race_pace_index.json) — source of truth
    for measured pace used by the Form Guide, speed map and dashboards.

Channels (keep separate — never collapse into one number):
  A. Predicted pace (pre-race)  → pace label + pace_score (seconds)
  B. Actual pace (post-race)    → actual_dev + actual_pace_label
  C. Speedmap beneficiaries     → display-only flags on horses
  D. Model scoring              → independent; pace does NOT modify horse rating
                                  until predicted-pace accuracy ≥ 50% exact.
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple


BASE = Path(__file__).parent
CACHE_DIR = BASE / "cache"
PACE_INDEX_PATH = CACHE_DIR / "race_pace_index.json"


# ── HKJC reference sectional times (Good going) ──────────────────────────
# Keyed by (venue_surface, distance, class_num) → {"total": s, "early": s}
# "early" = time to reach the "first-call" marker commonly published by HKJC
# (≈400m-600m from start, depending on distance). Values are drawn from
# racing.hkjc.com course-time charts. When a class is missing, std_lookup()
# fuzzy-matches nearby classes.
HKJC_STD: Dict[Tuple[str, int, int], Dict[str, float]] = {
    # ST Turf 1000m (no early published — sprint only)
    ("ST_Turf", 1000, 0): {"total": 55.90, "early": None},
    ("ST_Turf", 1000, 2): {"total": 56.05, "early": None},
    ("ST_Turf", 1000, 3): {"total": 56.45, "early": None},
    ("ST_Turf", 1000, 4): {"total": 56.65, "early": None},
    ("ST_Turf", 1000, 5): {"total": 57.00, "early": None},
    # ST Turf 1200m
    ("ST_Turf", 1200, 0): {"total": 68.15, "early": 22.20},
    ("ST_Turf", 1200, 1): {"total": 68.45, "early": 22.35},
    ("ST_Turf", 1200, 2): {"total": 68.65, "early": 22.45},
    ("ST_Turf", 1200, 3): {"total": 69.00, "early": 22.55},
    ("ST_Turf", 1200, 4): {"total": 69.35, "early": 22.70},
    ("ST_Turf", 1200, 5): {"total": 69.55, "early": 22.80},
    # ST Turf 1400m
    ("ST_Turf", 1400, 0): {"total": 81.10, "early": 34.90},
    ("ST_Turf", 1400, 1): {"total": 81.25, "early": 35.00},
    ("ST_Turf", 1400, 2): {"total": 81.45, "early": 35.10},
    ("ST_Turf", 1400, 3): {"total": 81.65, "early": 35.20},
    ("ST_Turf", 1400, 4): {"total": 82.00, "early": 35.35},
    ("ST_Turf", 1400, 5): {"total": 82.30, "early": 35.50},
    # ST Turf 1600m
    ("ST_Turf", 1600, 0): {"total": 93.90, "early": 47.60},
    ("ST_Turf", 1600, 1): {"total": 94.05, "early": 47.70},
    ("ST_Turf", 1600, 2): {"total": 94.25, "early": 47.80},
    ("ST_Turf", 1600, 3): {"total": 94.70, "early": 47.95},
    ("ST_Turf", 1600, 4): {"total": 94.90, "early": 48.05},
    ("ST_Turf", 1600, 5): {"total": 95.45, "early": 48.25},
    # ST Turf 1800m
    ("ST_Turf", 1800, 0): {"total": 107.10, "early": 60.00},
    ("ST_Turf", 1800, 2): {"total": 107.30, "early": 60.10},
    ("ST_Turf", 1800, 3): {"total": 107.50, "early": 60.20},
    ("ST_Turf", 1800, 4): {"total": 107.85, "early": 60.35},
    ("ST_Turf", 1800, 5): {"total": 108.45, "early": 60.55},
    # ST Turf 2000m
    ("ST_Turf", 2000, 0): {"total": 120.50, "early": 72.40},
    ("ST_Turf", 2000, 1): {"total": 121.20, "early": 72.65},
    ("ST_Turf", 2000, 2): {"total": 121.70, "early": 72.85},
    ("ST_Turf", 2000, 3): {"total": 121.90, "early": 72.95},
    ("ST_Turf", 2000, 4): {"total": 122.35, "early": 73.10},
    ("ST_Turf", 2000, 5): {"total": 122.65, "early": 73.20},
    ("ST_Turf", 2400, 0): {"total": 147.00, "early": 97.50},
    # HV Turf
    ("HV_Turf", 1000, 2): {"total": 56.40, "early": None},
    ("HV_Turf", 1000, 3): {"total": 56.65, "early": None},
    ("HV_Turf", 1000, 4): {"total": 57.20, "early": None},
    ("HV_Turf", 1000, 5): {"total": 57.35, "early": None},
    ("HV_Turf", 1200, 1): {"total": 69.10, "early": 22.60},
    ("HV_Turf", 1200, 2): {"total": 69.30, "early": 22.70},
    ("HV_Turf", 1200, 3): {"total": 69.60, "early": 22.80},
    ("HV_Turf", 1200, 4): {"total": 69.90, "early": 22.90},
    ("HV_Turf", 1200, 5): {"total": 70.10, "early": 23.00},
    ("HV_Turf", 1650, 1): {"total": 99.10, "early": 51.80},
    ("HV_Turf", 1650, 2): {"total": 99.30, "early": 51.90},
    ("HV_Turf", 1650, 3): {"total": 99.90, "early": 52.05},
    ("HV_Turf", 1650, 4): {"total": 100.10, "early": 52.15},
    ("HV_Turf", 1650, 5): {"total": 100.30, "early": 52.25},
    ("HV_Turf", 1800, 0): {"total": 108.95, "early": 60.80},
    ("HV_Turf", 1800, 2): {"total": 109.15, "early": 60.90},
    ("HV_Turf", 1800, 3): {"total": 109.45, "early": 61.05},
    ("HV_Turf", 1800, 4): {"total": 109.65, "early": 61.15},
    ("HV_Turf", 1800, 5): {"total": 109.95, "early": 61.25},
    ("HV_Turf", 2200, 3): {"total": 136.60, "early": 88.60},
    ("HV_Turf", 2200, 4): {"total": 137.05, "early": 88.80},
    ("HV_Turf", 2200, 5): {"total": 137.35, "early": 88.95},
    # ST AWT
    ("ST_AWT", 1200, 2): {"total": 68.35, "early": 22.30},
    ("ST_AWT", 1200, 3): {"total": 68.55, "early": 22.40},
    ("ST_AWT", 1200, 4): {"total": 68.95, "early": 22.55},
    ("ST_AWT", 1200, 5): {"total": 69.35, "early": 22.70},
    ("ST_AWT", 1650, 1): {"total": 97.80, "early": 50.40},
    ("ST_AWT", 1650, 2): {"total": 98.40, "early": 50.65},
    ("ST_AWT", 1650, 3): {"total": 98.60, "early": 50.75},
    ("ST_AWT", 1650, 4): {"total": 99.05, "early": 50.90},
    ("ST_AWT", 1650, 5): {"total": 99.45, "early": 51.05},
    ("ST_AWT", 1800, 3): {"total": 108.05, "early": 59.65},
    ("ST_AWT", 1800, 4): {"total": 108.55, "early": 59.80},
    ("ST_AWT", 1800, 5): {"total": 109.45, "early": 60.10},
}

# HKJC scraper abbreviations (and a few stray variants) → canonical going label.
GOING_NORMALISE = {
    "G": "Good", "GOOD": "Good",
    "GF": "Good to Firm", "GOOD TO FIRM": "Good to Firm",
    "GY": "Good to Yielding", "GOOD TO YIELDING": "Good to Yielding",
    "Y": "Yielding", "YIELDING": "Yielding",
    "YS": "Yielding to Soft", "YIELDING TO SOFT": "Yielding to Soft",
    "S": "Soft", "SOFT": "Soft", "SE": "Soft",
    "H": "Heavy", "HV": "Heavy", "HEAVY": "Heavy",
    "F": "Firm", "FT": "Firm", "FIRM": "Firm",
    "WF": "Wet Fast", "WET FAST": "Wet Fast",
    "WS": "Wet Slow", "WET SLOW": "Wet Slow",
    "FAST": "AWT Fast", "AWT FAST": "AWT Fast",
    "STD": "AWT Good", "AWT GOOD": "AWT Good",
    "SLOW": "AWT Slow", "AWT SLOW": "AWT Slow",
}

# Empirical seconds-per-race offset vs 'Good' (turf). Negative = faster.
GOING_OFFSET = {
    "Good": 0.00,
    "Good to Firm": -0.166,
    "Firm": -0.30,
    "Good to Yielding": +0.199,
    "Yielding": +0.30,
    "Yielding to Soft": +0.38,
    "Soft": +0.45,
    "Heavy": +0.60,
    "Wet Fast": +0.10,
    "Wet Slow": +0.25,
    "AWT Fast": -0.10,
    "AWT Good": 0.00,
    "AWT Slow": +0.20,
}

# 7-band classification (seconds of deviation from HKJC standard).
# Positive = slower than standard; negative = faster.
PACE_BANDS = [
    ("Very Slow",      0.50,  None),
    ("Slow",           0.35,  0.50),
    ("Slightly Slow",  0.20,  0.35),
    ("Normal",        -0.20,  0.20),
    ("Slightly Fast", -0.40, -0.20),
    ("Fast",          -1.00, -0.40),
    ("Very Fast",      None, -1.00),
]
PACE_ORDER = [b[0] for b in PACE_BANDS]
_PACE_IDX = {lbl: i for i, lbl in enumerate(PACE_ORDER)}


# ── Helpers ──────────────────────────────────────────────────────────────
def normalise_going(code: Optional[str]) -> str:
    """Map scraper code / free-text to canonical going label. Default 'Good'."""
    if not code:
        return "Good"
    key = str(code).strip().upper()
    return GOING_NORMALISE.get(key, str(code).strip() or "Good")


def going_offset(going_label: str) -> float:
    return GOING_OFFSET.get(going_label, 0.0)


def std_lookup(venue: str, distance: int, cls: int, is_awt: bool) -> Tuple[
        Optional[float], Optional[float], str, Optional[int]]:
    """Return (total_std, early_std, venue_surface_key, class_used) using fuzzy
    class match if exact is missing."""
    if is_awt:
        vs = "ST_AWT"
    elif str(venue).upper() == "HV":
        vs = "HV_Turf"
    else:
        vs = "ST_Turf"
    for d in (0, 1, -1, 2, -2):
        v = HKJC_STD.get((vs, int(distance), int(cls) + d))
        if v:
            return v.get("total"), v.get("early"), vs, int(cls) + d
    return None, None, vs, None


def parse_finish_time(s) -> Optional[float]:
    """'1:20.92' → 80.92; '57.93' → 57.93; None for unparseable."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    m = re.match(r"^(\d+):(\d+\.?\d*)$", s)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    try:
        return float(s)
    except ValueError:
        return None


def classify_pace(dev: float) -> str:
    if dev >= 0.50: return "Very Slow"
    if dev >= 0.35: return "Slow"
    if dev >= 0.20: return "Slightly Slow"
    if dev > -0.20: return "Normal"
    if dev >= -0.40: return "Slightly Fast"
    if dev > -1.00: return "Fast"
    return "Very Fast"


def pace_band_distance(pred_label: str, actual_label: str) -> Optional[int]:
    """|ordinal distance| between two labels (0 = exact match). None if unknown."""
    if pred_label in _PACE_IDX and actual_label in _PACE_IDX:
        return abs(_PACE_IDX[pred_label] - _PACE_IDX[actual_label])
    return None


def running_style_from_positions(positions: List[int]) -> str:
    """From HKJC running positions (list of ints from 1st-call → finish),
    classify the horse's in-race running style."""
    if not positions:
        return "Unknown"
    first = positions[0]
    if first <= 2: return "Leader"
    if first <= 4: return "On-Pace"
    if first <= 7: return "Midfield"
    return "Closer"


# ── Post-race: actual race pace from a results-JSON race dict ────────────
def _parse_sects(s) -> List[float]:
    """Parse an HKJC 'sectiontimes' string (;-separated) into list of floats."""
    if s is None:
        return []
    txt = str(s).strip()
    if not txt or txt.lower() in ("nan", "none", ""):
        return []
    parts = [p.strip() for p in txt.split(";") if p.strip()]
    out: List[float] = []
    for p in parts:
        try:
            v = float(p)
        except ValueError:
            return []
        if 5.0 < v < 40.0:
            out.append(v)
    return out


# ── HKJC sectional structure (section lengths in metres) ────────────────
# Mirrors sarr_raceday.py. Used to normalise the first section to 400m pace.
SECTION_LENGTHS: Dict[int, List[int]] = {
    1000: [200, 400, 400],
    1200: [400, 400, 400],
    1400: [200, 400, 400, 400],
    1600: [400, 400, 400, 400],
    1650: [450, 400, 400, 400],
    1800: [200, 400, 400, 400, 400],
    2000: [400, 400, 400, 400, 400],
    2200: [200, 400, 400, 400, 400, 400],
    2400: [400, 400, 400, 400, 400, 400],
}


def _early_zone_pace(secs: List[float], distance) -> Optional[float]:
    """First section normalised to a 400m-equivalent pace. Lower=faster."""
    if not secs:
        return None
    try:
        d = int(float(distance))
    except (TypeError, ValueError):
        return None
    lengths = SECTION_LENGTHS.get(d)
    if not lengths or len(secs) != len(lengths):
        return None
    first_len = lengths[0]
    if first_len <= 0:
        return None
    return secs[0] * 400.0 / first_len


def compute_sectional_metrics(form_db) -> dict:
    """Per-run Form Guide lookup for ESZ, Fin Δ, and finish z-score.

    ESZ is SARR-style early_dev: first-section 400m pace minus the race
    median; negative is faster early. Fin Δ is finish_time_seconds minus
    the field median; negative is faster than the field. Keys are
    (YYYY-MM-DD, race_number).
    """
    import math
    import numpy as np
    import pandas as pd

    out: Dict[tuple, Dict[str, dict]] = {}
    need = {"race_date", "race_number", "horse_name", "distance",
            "finish_time_seconds", "sectiontimes"}
    if form_db is None or not need.issubset(set(getattr(form_db, "columns", []))):
        return out

    sub = form_db.dropna(subset=["race_date", "race_number", "horse_name"]).copy()
    if sub.empty:
        return out

    sub["_ft"] = pd.to_numeric(sub["finish_time_seconds"], errors="coerce")
    sub["_early"] = [
        _early_zone_pace(_parse_sects(s), d)
        for s, d in zip(sub["sectiontimes"].tolist(), sub["distance"].tolist())
    ]

    for (rd, rn), g in sub.groupby(["race_date", "race_number"], sort=False):
        try:
            rn_i = int(rn)
        except (TypeError, ValueError):
            continue
        try:
            dkey = pd.Timestamp(rd).strftime("%Y-%m-%d")
        except Exception:
            dkey = str(rd)[:10]

        ft = g["_ft"].dropna()
        med_ft = float(ft.median()) if len(ft) >= 4 else math.nan
        ft_vals = ft.to_numpy(dtype=float)
        if len(ft_vals) >= 4 and ft_vals.std() > 1e-6:
            ft_mu = float(ft_vals.mean())
            ft_sd = float(ft_vals.std())
        else:
            ft_mu = ft_sd = math.nan

        ep = pd.to_numeric(g["_early"], errors="coerce").dropna()
        med_ep = float(ep.median()) if len(ep) >= 4 else math.nan
        if len(ep) >= 4 and float(ep.std()) > 1e-6:
            ep_mu = float(ep.mean())
            ep_sd = float(ep.std())
        else:
            ep_mu = ep_sd = math.nan

        m: Dict[str, dict] = {}
        for _, r in g.iterrows():
            h = str(r["horse_name"]).strip().upper()
            if not h or h == "NAN":
                continue
            esz = None
            esz_z = None
            early = r["_early"]
            if early is not None and not (isinstance(early, float) and math.isnan(early)):
                try:
                    e = float(early)
                except (TypeError, ValueError):
                    e = math.nan
                if not math.isnan(e) and not math.isnan(med_ep):
                    # SARR ESZ = early_dev: first-section 400m pace vs race median.
                    # Negative means faster early speed.
                    esz = e - med_ep
                if not math.isnan(e) and not math.isnan(ep_mu) and ep_sd > 0:
                    esz_z = (e - ep_mu) / ep_sd
            fin_delta = None
            f = r["_ft"]
            if f is not None and not (isinstance(f, float) and math.isnan(f)):
                try:
                    fval = float(f)
                except (TypeError, ValueError):
                    fval = math.nan
                if not math.isnan(fval) and not math.isnan(med_ft):
                    fin_delta = fval - med_ft
            fin_z = None
            if f is not None and not (isinstance(f, float) and math.isnan(f)):
                try:
                    fval = float(f)
                except (TypeError, ValueError):
                    fval = math.nan
                if not math.isnan(fval) and not math.isnan(ft_mu) and ft_sd > 0:
                    fin_z = (fval - ft_mu) / ft_sd
                m[h] = {"esz": esz, "esz_z": esz_z,
                    "fin_delta": fin_delta, "fin_z": fin_z}
        out[(dkey, rn_i)] = m
    return out


def compute_actual_race_pace(race: dict, venue: str) -> dict:
    """Given a race dict as produced by scrape_hkjc_results.scrape_race()
    (or the equivalent shape read from hkjc_results_updated.xlsx), return
    the race augmented with measured-pace fields.

    PRIMARY pace measure = WINNER'S TOTAL-TIME DEVIATION vs the HKJC
    reference total time (going-adjusted).  This is the most robust
    apples-to-apples comparison — single number vs single number — and
    matches the user's request: "official sectional time in the results
    page vs HKJC reference standard time".

    SECONDARY display-only measure = winner's early-sectional time
    (sum(winner_sects[:-1])), surfaced so the UI can show pace pressure at
    the first-call markers.  Not used for classification because the
    "early" column in HKJC_STD does not align with sum(sects[:-1]) at
    every distance.

    Returns a dict of fields; input `race` is NOT modified.
    """
    out = {
        # Canonical going
        "actual_going": None,
        # HKJC references
        "actual_std_total": None, "actual_std_early": None, "actual_std_adj": None,
        # Winner measurements
        "actual_winner_time": None, "actual_winner_sects": None,
        "actual_winner_early_s": None,
        # Raw (pre-going) and going-adjusted deviations (TOTAL time = authoritative)
        "actual_raw_dev_s": None, "actual_going_adj_s": None,
        "actual_dev": None,              # ← authoritative (total-time, going-adjusted)
        "actual_pace_label": "N/A",
        "actual_source": None,           # "total_time" | None
        # Winner style
        "actual_winner_style": "N/A", "actual_winner_positions": None,
    }

    try:
        distance = int(race.get("distance") or 0)
    except (TypeError, ValueError):
        distance = 0
    cls_raw = race.get("race_class") or ""
    m = re.search(r"\d+", str(cls_raw))
    cls = int(m.group()) if m else 0
    is_awt = bool(race.get("is_awt"))
    if not is_awt:
        tt = str(race.get("track_type") or "").upper()
        rc = str(race.get("race_course") or "").upper()
        if "AWT" in tt or "ALL WEATHER" in tt or "AWT" in rc:
            is_awt = True

    going = normalise_going(race.get("going"))
    std_total, std_early, vs_key, cls_used = std_lookup(venue, distance, cls, is_awt)
    offset = going_offset(going)
    std_total_adj = (std_total + offset) if std_total is not None else None

    # Find winner's row
    winner_time = None
    winner_sects: List[float] = []
    winner_positions = None
    runners = race.get("runners") or race.get("results") or race.get("horses") or []
    for row in runners:
        plc = row.get("place")
        try:
            fp = int(re.search(r"\d+", str(plc)).group()) if plc not in (None, "") else 0
        except (AttributeError, TypeError, ValueError):
            fp = 0
        if fp == 1:
            wt = row.get("finish_time_seconds")
            if wt in (None, 0, ""):
                wt = parse_finish_time(row.get("finish_time"))
            winner_time = wt
            winner_sects = _parse_sects(row.get("sectiontimes") or row.get("sectional_times"))
            rp = row.get("running_position") or row.get("running_positions") or ""
            winner_positions = [int(x) for x in re.findall(r"\d+", str(rp))]
            break

    # Fallback: per-segment minimum across the whole field (= leader at each call)
    if len(winner_sects) < 2:
        all_sects = [s for s in (_parse_sects(r.get("sectiontimes") or r.get("sectional_times"))
                                 for r in runners) if len(s) >= 2]
        if all_sects:
            n = min(len(s) for s in all_sects)
            winner_sects = [min(s[i] for s in all_sects) for i in range(n)]

    # If winner_time missing but we have sectionals, reconstruct it
    if (winner_time is None or winner_time == 0) and len(winner_sects) >= 2:
        winner_time = round(sum(winner_sects), 2)

    # ── Primary: total-time deviation (going-adjusted) ──
    raw_dev_total = None
    dev_total = None
    source = None
    if winner_time and std_total is not None:
        raw_dev_total = round(winner_time - std_total, 2)
        dev_total = round(raw_dev_total - offset, 2)
        source = "total_time"

    # ── Winner's early-sectional time (sum of all segments except final 400m) ──
    # Useful for UI display; not used for classification.
    winner_early = None
    if len(winner_sects) >= 2:
        winner_early = round(sum(winner_sects[:-1]), 2)

    label = classify_pace(dev_total) if dev_total is not None else "N/A"
    winner_style = running_style_from_positions(winner_positions) if winner_positions else "N/A"

    out.update({
        "actual_going":         going,
        "actual_std_total":     round(std_total, 2) if std_total else None,
        "actual_std_early":     round(std_early, 2) if std_early else None,
        "actual_std_adj":       round(std_total_adj, 2) if std_total_adj else None,
        "actual_winner_time":   round(winner_time, 2) if winner_time else None,
        "actual_winner_sects":  [round(v, 2) for v in winner_sects] if winner_sects else None,
        "actual_winner_early_s": winner_early,
        "actual_raw_dev_s":     raw_dev_total,
        "actual_going_adj_s":   round(offset, 3),
        "actual_dev":           dev_total,
        "actual_pace_label":    label,
        "actual_source":        source,
        "actual_winner_style":  winner_style,
        "actual_winner_positions": winner_positions,
    })
    return out


# ── Persistent pace index (cache/race_pace_index.json) ────────────────────
# Single source of truth used by the Form Guide (no recomputation per load)
# and by the speed-map benefit analysis.

def _pace_key(date_iso: str, race_number: int) -> str:
    return f"{str(date_iso)[:10]}_R{int(race_number)}"


def load_pace_index() -> dict:
    if not PACE_INDEX_PATH.exists():
        return {}
    try:
        with open(PACE_INDEX_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_pace_index(idx: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    with open(PACE_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1, sort_keys=True)


def get_pace(idx: dict, date_iso: str, race_number: int) -> Optional[dict]:
    return idx.get(_pace_key(date_iso, race_number))


def upsert_pace(idx: dict, date_iso: str, race_number: int, pace_dict: dict) -> None:
    idx[_pace_key(date_iso, race_number)] = pace_dict


def annotate_results_meeting(meeting: dict, persist: bool = True) -> dict:
    """Mutates meeting dict in place — adds actual-pace fields to each race,
    and (when `persist=True`) upserts a compact entry into the persistent
    pace index at cache/race_pace_index.json.  Returns the mutated meeting.
    """
    venue = meeting.get("venue", "ST")
    date_iso = (meeting.get("date") or meeting.get("race_date")
                or meeting.get("meeting_date") or "")
    date_iso = str(date_iso)[:10]

    idx = load_pace_index() if persist else None
    changed = False

    for race in meeting.get("races", []):
        pace = compute_actual_race_pace(race, venue)
        race.update(pace)
        rn = race.get("race_number") or race.get("race_no")
        if persist and idx is not None and date_iso and rn is not None and pace.get("actual_dev") is not None:
            upsert_pace(idx, date_iso, int(rn), {
                "date":          date_iso,
                "race_number":   int(rn),
                "venue":         venue,
                "distance":      race.get("distance"),
                "race_class":    race.get("race_class"),
                "is_awt":        bool(race.get("is_awt")) or "AWT" in str(race.get("track_type") or "").upper(),
                "going":         pace["actual_going"],
                "winner_sects":  pace["actual_winner_sects"],
                "winner_early_s": pace["actual_winner_early_s"],
                "winner_time_s": pace["actual_winner_time"],
                "hkjc_std_early": pace["actual_std_early"],
                "hkjc_std_total": pace["actual_std_total"],
                "raw_dev_s":     pace["actual_raw_dev_s"],
                "going_adj_s":   pace["actual_going_adj_s"],
                "adj_dev_s":     pace["actual_dev"],
                "label":         pace["actual_pace_label"],
                "source":        pace["actual_source"],
                "winner_style":  pace["actual_winner_style"],
            })
            changed = True

    if changed:
        save_pace_index(idx)
    return meeting


# ── Pace-style benefit (applied to speed map + dashboards) ────────────────
# Populated by _analyse_pace_benefit.py → cache/pace_style_benefit.json.
# Seconds value is SUBTRACTED from a horse's projected time:
#   negative = beneficiary (faster); positive = penalised.

_PACE_GROUP = {
    "Very Slow": "Slow", "Slow": "Slow", "Slightly Slow": "Slow",
    "V.Slow": "Slow",  # short form
    "Normal": "Avg", "Avg": "Avg",
    "Slightly Fast": "Fast", "Fast": "Fast", "Very Fast": "Fast",
    "Sl.Fast": "Fast", "Sl.Slow": "Slow", "V.Fast": "Fast",
}

# Prior (used until the empirical lookup is generated):
_DEFAULT_BENEFIT = {
    "Slow": {"Leader": -0.15, "On-Pace": -0.08, "Midfield": +0.05, "Closer": +0.15},
    "Avg":  {"Leader":  0.00, "On-Pace":  0.00, "Midfield":  0.00, "Closer":  0.00},
    "Fast": {"Leader": +0.15, "On-Pace": +0.08, "Midfield": -0.05, "Closer": -0.15},
}

_BENEFIT_CACHE: Optional[dict] = None


def load_benefit() -> dict:
    """Load the pace-style benefit lookup from disk (cached)."""
    global _BENEFIT_CACHE
    if _BENEFIT_CACHE is not None:
        return _BENEFIT_CACHE
    path = CACHE_DIR / "pace_style_benefit.json"
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                _BENEFIT_CACHE = json.load(f)
                return _BENEFIT_CACHE
        except Exception:
            pass
    _BENEFIT_CACHE = {"_default": _DEFAULT_BENEFIT}
    return _BENEFIT_CACHE


def pace_group(label: str) -> str:
    return _PACE_GROUP.get(str(label), "Avg")


def pace_style_bonus_seconds(pace_label: str, style: str,
                             distance: Optional[int] = None,
                             venue: Optional[str] = None) -> float:
    """Seconds adjustment for a horse given the predicted race pace and
    their running style.  NEGATIVE = faster = beneficiary."""
    data = load_benefit()
    group = pace_group(pace_label)

    # Distance-specific lookup if provided
    if distance is not None:
        if distance <= 1200:
            band = "sprint"
        elif distance <= 1600:
            band = "mile"
        else:
            band = "route"
        specific = None
        if venue:
            specific = data.get(f"{str(venue).upper()}_{band}")
        if specific is None:
            specific = data.get(band)
        if specific and group in specific and style in specific[group]:
            return float(specific[group][style])

    default = data.get("_default", _DEFAULT_BENEFIT)
    return float(default.get(group, {}).get(style, 0.0))


# ── Pre-race: early-sectional pace prediction (v4.5) ─────────────────────
def predict_early_sectional_dev(horses: List[dict],
                                 distance: int,
                                 going: str,
                                 venue: str,
                                 race_class,
                                 is_awt: bool,
                                 field_size: Optional[int] = None) -> Tuple[
        str, float, List[str], List[str]]:
    """v4.5 pace prediction — targets EARLY sectional deviation (not total).

    Rationale: total-time deviation conflates ability and tempo. Early
    sectional (first ~400-600m) is what determines pace pressure, who leads,
    and whether closers will benefit. HKJC publishes early-sectional
    standards directly; we target those.

    Inputs:
      horses: list of per-horse dicts carrying at least {"horse_name",
              "dominant_style" | "running_style" | "style"} (accepts several key names)
      distance, going (canonical), venue ("ST"/"HV"), race_class, is_awt

    Returns: (pace_label, predicted_dev_seconds, reasons, leader_names)
      Bands use the same 7-label scale as actual pace, but thresholds are
      *early-sectional* seconds. Early sectional is ~30% of total, so we
      scale the Normal band to ±0.15s here (vs ±0.20 on total).
    """
    reasons: List[str] = []

    # Count styles
    def _style(h):
        return (h.get("dominant_style") or h.get("running_style") or h.get("style") or "Unknown")

    leaders = [h for h in horses if _style(h) == "Leader"]
    on_pace = [h for h in horses if _style(h) == "On-Pace"]
    leader_names = [h.get("horse_name", "?") for h in leaders]
    n = field_size or len(horses) or 1

    # Base pressure: share of field that wants to go forward, with a taper for
    # small fields (a 6-horse Leader-only race isn't 100% pressure — one horse
    # alone can dictate a steady pace).
    leader_share = len(leaders) / n
    front_share = (len(leaders) + 0.4 * len(on_pace)) / n

    # Pressure index (0..1, loosely): 0.5 = one pronounced leader, 1.0 = several
    if len(leaders) == 0:
        base = 0.20     # slow scenario — no natural pacesetter
    elif len(leaders) == 1:
        base = 0.50     # sole frontrunner dictates own tempo
    elif len(leaders) == 2:
        base = 0.80     # likely duel
    else:
        base = 1.00     # congested leader group
    base += 0.15 * min(len(on_pace), 3) / 3.0   # on-pace presence taper

    # Distance shaping: sprints run harder early; routes slower early.
    if distance <= 1200:
        dist_shape = +0.15
    elif distance <= 1400:
        dist_shape = +0.05
    elif distance <= 1650:
        dist_shape = 0.00
    elif distance <= 1800:
        dist_shape = -0.08
    else:
        dist_shape = -0.15

    # Convert pressure → early-sectional seconds deviation.
    # Calibration (seconds): pressure 1.00 → −0.35s; pressure 0.20 → +0.30s.
    # Linear between. This gives a usable ±0.5s spread (vs v4.4's ±0.3s total).
    pressure = max(0.0, min(1.15, base + dist_shape))
    # y = a + b*x with (0.20, +0.30), (1.00, -0.35) → b=-0.8125, a=+0.4625
    predicted_early_dev = 0.4625 - 0.8125 * pressure

    # Going adjustment (reduced for early sectional — roughly 35% of total-time offset)
    predicted_early_dev += 0.35 * going_offset(going)

    # Class gentle adjustment — Class 1/Group races run fractionally harder early
    try:
        cls_int = int(re.search(r"\d+", str(race_class)).group())
    except Exception:
        cls_int = 4
    if cls_int <= 1:
        predicted_early_dev -= 0.05
    elif cls_int >= 5:
        predicted_early_dev += 0.05

    # Early-sectional band thresholds (slightly tighter than total-time bands
    # because early-sectional noise is smaller).
    def _early_classify(d):
        if d >= 0.40: return "Very Slow"
        if d >= 0.25: return "Slow"
        if d >= 0.15: return "Slightly Slow"
        if d > -0.15: return "Normal"
        if d >= -0.30: return "Slightly Fast"
        if d > -0.70: return "Fast"
        return "Very Fast"

    label = _early_classify(predicted_early_dev)

    if len(leaders) == 0:
        reasons.append("No established front-runners")
    elif len(leaders) == 1:
        reasons.append(f"Sole front-runner ({leader_names[0]})")
    else:
        reasons.append(f"{len(leaders)} front-runners ({', '.join(leader_names[:4])})")
    reasons.append(f"v4.5 early-dev={predicted_early_dev:+.2f}s "
                   f"(pressure={pressure:.2f}, going={going})")

    return label, round(predicted_early_dev, 2), reasons, leader_names


__all__ = [
    "HKJC_STD", "GOING_NORMALISE", "GOING_OFFSET", "PACE_BANDS", "PACE_ORDER",
    "PACE_INDEX_PATH",
    "normalise_going", "going_offset", "std_lookup", "parse_finish_time",
    "classify_pace", "pace_band_distance", "running_style_from_positions",
    "compute_actual_race_pace", "annotate_results_meeting",
    "predict_early_sectional_dev",
    "load_pace_index", "save_pace_index", "get_pace", "upsert_pace",
    "pace_style_bonus_seconds", "pace_group", "load_benefit",
]
