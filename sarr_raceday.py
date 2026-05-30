#!/usr/bin/env python3
"""
SARR Race-Day PDF Generator
============================
Produces an analytical PDF for a specific meeting using the SARR
(Sectional-Anchored Relative Rating) method.

Usage:
    python sarr_raceday.py --date 2026-04-19

Standalone — does NOT modify or depend on the v3.4.8/v4.4 model.
"""

import os, sys, re, json, math, warnings, shutil, argparse
import numpy as np
import pandas as pd
from collections import defaultdict
from scipy import stats

warnings.filterwarnings("ignore")

# ════════════════════════════════════════════════════════════════════════
# REPORTLAB
# ════════════════════════════════════════════════════════════════════════
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
    PageBreak, KeepTogether,
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

# ════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════
from pathlib import Path as _Path
BASE = _Path(__file__).resolve().parent
os.chdir(str(BASE))

def _find_data_file(name: str) -> str:
    """Locate a data file: check BASE first, then $TEMP."""
    p = BASE / name
    if p.exists():
        return str(p)
    t = os.path.join(os.environ.get("TEMP", "/tmp"), name)
    if os.path.exists(t):
        return t
    return str(p)  # fallback

DB_SRC = _find_data_file("hkjc_results_updated.xlsx")
DB_TMP = os.path.join(os.environ.get("TEMP", "/tmp"), "hkjc_sarr_raceday.xlsx")

RECENCY_LAMBDA = 0.85
MAX_PRIOR_RUNS = 15

# v4.6 (May 2026 backtest improvements):
#   - Going-band place_rate: filter horse history to today's going band when
#     ≥3 same-band runs exist; otherwise fall back to overall place_rate.
#     Backtest showed SARR collapses to 0% W on Yielding because overall
#     place_rate masks soft-going specialists/duds.
#   - Last-start style boost: weight the most recent run's style ×3 when
#     deriving the dominant style.  Horses' tactical positions drift; recent
#     intent is more predictive than long-run mode.
#   - Trainer-debut win rate: replace SARR=0.50 default for debutants with
#     the trainer's historical first-up place rate (with shrinkage toward
#     0.20 league average).
GOING_BAND_MIN_N        = 3       # min same-band runs to use specific place_rate
LAST_STYLE_BOOST        = 3.0     # weight on most recent run's style
DEBUT_TRAINER_SHRINK_N  = 10      # Bayesian shrinkage prior count
DEBUT_LEAGUE_PLACE_RATE = 0.20    # baseline first-up place rate

SECTION_LENGTHS = {
    1000: [200, 400, 400], 1200: [400, 400, 400],
    1400: [200, 400, 400, 400], 1600: [400, 400, 400, 400],
    1650: [450, 400, 400, 400], 1800: [200, 400, 400, 400, 400],
    2000: [400, 400, 400, 400, 400],
    2200: [200, 400, 400, 400, 400, 400],
    2400: [400, 400, 400, 400, 400, 400],
}

STYLE_ADV = {
    1000: {"Leader": 2.034, "On-Pace": 1.162, "Midfield": 0.573, "Closer": 0.418},
    1200: {"Leader": 1.708, "On-Pace": 1.204, "Midfield": 0.765, "Closer": 0.428},
    1400: {"Leader": 1.370, "On-Pace": 1.395, "Midfield": 0.954, "Closer": 0.424},
    1600: {"Leader": 1.455, "On-Pace": 1.439, "Midfield": 0.829, "Closer": 0.448},
    1650: {"Leader": 1.287, "On-Pace": 1.398, "Midfield": 0.861, "Closer": 0.542},
    1800: {"Leader": 1.951, "On-Pace": 0.627, "Midfield": 1.184, "Closer": 0.451},
    2000: {"Leader": 1.771, "On-Pace": 1.165, "Midfield": 0.866, "Closer": 0.450},
    2200: {"Leader": 1.142, "On-Pace": 1.004, "Midfield": 0.690, "Closer": 1.380},
}

HV_STYLE_MOD = {"Leader": 1.15, "On-Pace": 1.10, "Midfield": 0.95, "Closer": 0.80}

IDEAL_SSI = {
    1000: 0.0, 1200: -0.10, 1400: -0.20, 1600: -0.30,
    1650: -0.30, 1800: -0.35, 2000: -0.40, 2200: -0.45, 2400: -0.50,
}

# Colours (matching existing PDF style)
HDR_BG  = colors.HexColor("#2C3E50")
ROW_ALT = colors.HexColor("#F0F4F8")
TOP1_BG = colors.HexColor("#D4EFDF")
TOP2_BG = colors.HexColor("#EBF5FB")
TOP3_BG = colors.HexColor("#FEF9E7")
GREEN   = "#27AE60"
RED     = "#E74C3C"

# ════════════════════════════════════════════════════════════════════════
# UTILITY
# ════════════════════════════════════════════════════════════════════════
def safe_place(val):
    if pd.isna(val): return 99
    m = re.match(r"^(\d+)", str(val).strip())
    return int(m.group(1)) if m else 99


def going_band(going_label):
    """v4.6: Group going codes into bands for filtering.
    DB codes: GF/G/GY/Y/SE/WF/WS/FM/F/HD."""
    if going_label is None:
        return "unknown"
    g = str(going_label).strip().upper()
    if not g:
        return "unknown"
    if g in ("SE", "SEALED", "WF", "WS", "AWT"):
        return "awt"
    if g in ("FM", "F", "HD", "GF"):
        return "firm"
    if g == "G":
        return "good"
    if g in ("GY", "Y", "S", "SOFT", "HEAVY", "H"):
        return "soft"
    return "unknown"

def safe_float(val, default=np.nan):
    try: return float(val)
    except (ValueError, TypeError): return default

def parse_sections(s):
    if pd.isna(s) or not str(s).strip(): return []
    out = []
    for p in str(s).split(";"):
        p = p.strip()
        if not p: continue
        try:
            v = float(p)
            if v > 0: out.append(v)
        except: continue
    return out

def classify_style(rp_str, field_size):
    if pd.isna(rp_str) or not str(rp_str).strip(): return "Unknown"
    parts = str(rp_str).strip().split()
    try: fc = int(parts[0])
    except: return "Unknown"
    fs = max(field_size, 1)
    if fc <= 2: return "Leader"
    elif fc <= max(4, int(fs * 0.3)): return "On-Pace"
    elif fc >= max(8, int(fs * 0.7)): return "Closer"
    else: return "Midfield"

def dist_weight(delta_m):
    d = abs(delta_m)
    if d == 0: return 1.0
    elif d <= 100: return 0.75
    elif d <= 200: return 0.50
    elif d <= 400: return 0.25
    return 0.10

def get_style_fit(style, distance, venue):
    dists = sorted(STYLE_ADV.keys())
    closest = min(dists, key=lambda d: abs(d - distance))
    mult = STYLE_ADV[closest].get(style, 1.0)
    if venue == "HV":
        mult *= HV_STYLE_MOD.get(style, 1.0)
    return -math.log(max(mult, 0.05))

def fv(val, fmt="{:.2f}", na="—"):
    if val is None or (isinstance(val, float) and np.isnan(val)): return na
    return fmt.format(val)

def fvs(val, fmt="{:+.3f}", na="—"):
    if val is None or (isinstance(val, float) and np.isnan(val)): return na
    return fmt.format(val)

def col_tag(val, lo=None, hi=None, fmt="{:+.3f}", na="—"):
    """Return HTML-tagged string with colour."""
    if val is None or (isinstance(val, float) and np.isnan(val)): return na
    s = fmt.format(val)
    if lo is not None and val < lo:
        return f'<font color="{GREEN}"><b>{s}</b></font>'
    if hi is not None and val > hi:
        return f'<font color="{RED}">{s}</font>'
    return s


# ════════════════════════════════════════════════════════════════════════
# PROFILE BUILDING (same as sarr_prototype.py)
# ════════════════════════════════════════════════════════════════════════
def build_history(db):
    """Build horse_hist dict from full DB (chronological)."""
    db_s = db.sort_values(["race_date", "race_number"]).reset_index(drop=True)

    # Pre-compute race-level aggregates
    race_agg = db_s.groupby(["race_date", "race_number"]).agg(
        med_ft=("_ft", "median"),
        field_size=("horse_name", "count"),
    ).reset_index()
    db_s = db_s.merge(race_agg, on=["race_date", "race_number"], how="left")

    # FMRP
    db_s["fmrp"] = (db_s["_ft"] - db_s["med_ft"]) / db_s["med_ft"] * 100

    # Sectional zones
    def _zones(row):
        secs = parse_sections(row["sectiontimes"])
        if not secs: return (np.nan, np.nan, np.nan, np.nan)
        d = int(row["_distance"]) if not pd.isna(row["_distance"]) else 0
        lengths = SECTION_LENGTHS.get(d)
        if lengths is None or len(secs) != len(lengths): return (np.nan, np.nan, np.nan, np.nan)
        p4 = [t * 400.0 / l for t, l in zip(secs, lengths)]
        early = p4[0]
        late = p4[-1]
        mid = np.mean(p4[1:-1]) if len(p4) >= 3 else (early + late) / 2
        return (early, mid, late, late - early)

    zone_data = db_s.apply(_zones, axis=1, result_type="expand")
    zone_data.columns = ["_early", "_mid", "_late", "_ssi_raw"]
    db_s = pd.concat([db_s, zone_data], axis=1)

    # Race-median for zones
    has_z = db_s["_late"].notna()
    z_agg = db_s[has_z].groupby(["race_date", "race_number"]).agg(
        med_early=("_early", "median"), med_late=("_late", "median"),
    ).reset_index()
    db_s = db_s.merge(z_agg, on=["race_date", "race_number"], how="left")
    db_s["early_dev"] = db_s["_early"] - db_s["med_early"]
    db_s["late_dev"]  = db_s["_late"]  - db_s["med_late"]
    db_s["ssi"]       = db_s["late_dev"] - db_s["early_dev"]

    # Style
    db_s["_style"] = db_s.apply(
        lambda r: classify_style(r["running_positions"], r["field_size"]), axis=1
    )

    # Build per-horse history
    horse_hist = defaultdict(list)
    for _, r in db_s.iterrows():
        if r["_place"] >= 90: continue
        horse_hist[r["horse_name"]].append({
            "date":      r["race_date"],
            "fmrp":      r.get("fmrp", np.nan),
            "late_dev":  r.get("late_dev", np.nan),
            "early_dev": r.get("early_dev", np.nan),
            "ssi":       r.get("ssi", np.nan),
            "style":     r.get("_style", "Unknown"),
            "rating":    r.get("_rating", np.nan),
            "place":     r["_place"],
            "distance":  r["_distance"],
            "venue":     r.get("race_track", ""),
            "surface":   r.get("track_type", ""),
            "going":     r.get("going", ""),
        })

    return horse_hist


def build_profile(hist, today_dist, today_venue, today_surface, today_going=None):
    if not hist: return None
    runs = list(reversed(hist[-MAX_PRIOR_RUNS:]))

    weights = []
    for i, run in enumerate(runs):
        w = RECENCY_LAMBDA ** i
        rd = run.get("distance", np.nan)
        if not np.isnan(rd) and not np.isnan(today_dist):
            w *= dist_weight(rd - today_dist)
        rv = run.get("venue", "")
        if rv and today_venue and rv != today_venue:
            w *= 0.60
        rs = run.get("surface", "")
        if rs and today_surface and rs != today_surface:
            w *= 0.50
        weights.append(max(w, 0.01))
    weights = np.array(weights)

    def wmean(key):
        vals = np.array([r.get(key, np.nan) for r in runs], dtype=float)
        m = ~np.isnan(vals)
        if not m.any(): return np.nan
        return np.average(vals[m], weights=weights[m])

    fmrp_val = wmean("fmrp")
    lsa_val  = wmean("late_dev")
    esz_val  = wmean("early_dev")
    ssi_val  = wmean("ssi")

    late_vals = np.array([r.get("late_dev", np.nan) for r in runs], dtype=float)
    late_valid = late_vals[~np.isnan(late_vals)]
    late_std = np.std(late_valid) if len(late_valid) >= 2 else 0.5

    styles = [r["style"] for r in runs if r.get("style", "Unknown") != "Unknown"]
    if styles:
        # v4.6: Last-start style boost — weight the most recent valid style
        # heavier so tactical drift (Closer → On-Pace) is reflected quickly.
        sc = defaultdict(float)
        last_style = None
        for r in runs:
            s = r.get("style", "Unknown")
            if s != "Unknown" and last_style is None:
                last_style = s
                break
        for s in styles:
            sc[s] += 1.0
        if last_style is not None:
            sc[last_style] += LAST_STYLE_BOOST - 1.0
        style = max(sc, key=sc.get)
    else:
        style = "Midfield"

    rating = np.nan
    for run in runs:
        r = run.get("rating", np.nan)
        if not np.isnan(r):
            rating = r; break

    fmrp_recent = [r.get("fmrp", np.nan) for r in runs[:5]]
    fmrp_recent = [v for v in fmrp_recent if not np.isnan(v)]
    if len(fmrp_recent) >= 3:
        slope = stats.linregress(np.arange(len(fmrp_recent)), fmrp_recent).slope
    else:
        slope = 0.0

    places = [r.get("place", 99) for r in runs]
    n_runs = len(places)
    place_rate = sum(1 for p in places if p <= 3) / max(n_runs, 1)

    # v4.6: Going-band specific place_rate — when ≥GOING_BAND_MIN_N runs at
    # today's going band exist, expose that filtered rate; else None.
    place_rate_band = None
    n_band = 0
    if today_going:
        today_band = going_band(today_going)
        if today_band != "unknown":
            band_places = [r.get("place", 99) for r in runs
                           if going_band(r.get("going", "")) == today_band]
            n_band = len(band_places)
            if n_band >= GOING_BAND_MIN_N:
                place_rate_band = (sum(1 for p in band_places if p <= 3)
                                   / max(n_band, 1))

    last6_places = [r.get("place", 99) for r in runs[:6]]
    last6_str = "/".join(str(p) if p < 90 else "—" for p in last6_places)

    return {
        "fmrp": fmrp_val,
        "lsa": lsa_val,
        "esz": esz_val,
        "avg_ssi": ssi_val,
        "late_std": late_std,
        "style": style,
        "rating": rating,
        "traj": slope,
        "place_rate": place_rate,
        "place_rate_band": place_rate_band,
        "n_band": n_band,
        "n_runs": n_runs,
        "last6": last6_str,
    }


# ════════════════════════════════════════════════════════════════════════
# OLS WEIGHTS (from training in sarr_prototype.py)
# ════════════════════════════════════════════════════════════════════════
# These were optimised on ~15,500 runners / 145 meetings pre-Mar 29
WEIGHTS = {
    "f_fmrp":   0.2901,
    "f_lsa":    0.0912,
    "f_esz":    0.0688,
    "f_style":  -0.0267,
    "f_rating": 0.0137,
    "f_traj":   -0.0711,
    "f_wpr":    0.0540,
    "f_dist":   -0.0147,
}


# ════════════════════════════════════════════════════════════════════════
# DRAW STATS (precomputed means; Bayesian-shrunk)
# ════════════════════════════════════════════════════════════════════════
def compute_draw_stats(db):
    draw_stats = {}
    for (venue, draw), grp in db.groupby(["race_track", "_draw"]):
        if pd.isna(draw) or draw < 1: continue
        resids = []
        for _, r in grp.iterrows():
            if r["_place"] >= 90: continue
            race_agg = db[(db["race_date"] == r["race_date"]) &
                          (db["race_number"] == r["race_number"])]
            med_p = (len(race_agg) + 1) / 2.0
            resids.append(r["_place"] - med_p)
        if len(resids) >= 10:
            draw_stats[(venue, int(draw))] = (len(resids), np.mean(resids))
    return draw_stats


def get_draw_score(draw, venue, draw_stats):
    if pd.isna(draw) or draw < 1: return 0.0
    key = (venue, int(draw))
    if key not in draw_stats: return 0.0
    n, mean_resid = draw_stats[key]
    return (n / (n + 10)) * mean_resid


# ════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-04-19", help="YYYY-MM-DD")
    parser.add_argument("--going-turf", default="", help="Turf going (e.g. 'Good', 'Good-to-Yielding')")
    parser.add_argument("--going-awt",  default="", help="AWT going (e.g. 'Good', 'Wet Fast')")
    args = parser.parse_args()
    race_date = args.date

    rc_path = os.path.join("cache", f"racecard_{race_date}.json")
    if not os.path.exists(rc_path):
        print(f"ERROR: Racecard not found: {rc_path}")
        sys.exit(1)

    with open(rc_path, "r", encoding="utf-8") as f:
        rc = json.load(f)

    venue = rc.get("racecourse", "ST")
    n_races = len(rc["races"])
    print(f"SARR Race-Day Analysis: {race_date} ({venue}) | {n_races} races")

    # ── Load historical DB ────────────────────────────────────────────
    print("  Loading historical DB...")
    try:
        from db_utils import load_results_db
        db = load_results_db()
        if db.empty:
            raise RuntimeError("empty results DB")
    except Exception as _exc:
        print(f"  (sqlite loader unavailable: {_exc}; reading xlsx)")
        shutil.copy2(DB_SRC, DB_TMP)
        db = pd.read_excel(DB_TMP)
    db["race_date"]  = pd.to_datetime(db["race_date"])
    db["_place"]     = db["place"].apply(safe_place)
    db["_ft"]        = db["finish_time_seconds"].apply(safe_float)
    db["_draw"]      = db["draw"].apply(safe_float)
    db["_rating"]    = db["rating"].apply(safe_float)
    db["_distance"]  = db["distance"].apply(safe_float)

    # Only use data strictly before race day
    cutoff = pd.Timestamp(race_date)
    db = db[(db["_place"] < 90) & db["_ft"].notna() & (db["_ft"] > 0)
            & (db["race_date"] < cutoff)].copy()
    print(f"  {len(db):,} historical finishers loaded")

    print("  Building horse profiles...")
    horse_hist = build_history(db)
    print(f"  {len(horse_hist):,} horses profiled")

    # Precompute draw stats
    draw_stats = {}
    for (v, d), grp in db.groupby(["race_track", "_draw"]):
        if pd.isna(d) or d < 1: continue
        n_g = len(grp)
        if n_g >= 10:
            # Simple: avg place vs expected mid-place
            places = grp["_place"].values
            draw_stats[(v, int(d))] = (n_g, np.mean(places) - 6.5)

    # v4.6: Trainer first-up (debut) place-rate stats — for each trainer,
    # find the FIRST recorded run per horse and compute their place rate.
    print("  Building trainer-debut stats...")
    db_chrono = db.sort_values(["horse_name", "race_date"]).reset_index(drop=True)
    first_runs = db_chrono.groupby("horse_name", as_index=False).first()
    trainer_debut_stats = {}  # trainer -> (n, place_rate)
    for trainer, grp in first_runs.groupby("trainer"):
        places = grp["_place"].values
        n = len(places)
        if n >= 3:
            pr = float((places <= 3).mean())
            trainer_debut_stats[str(trainer).strip()] = (n, pr)
    print(f"    {len(trainer_debut_stats):,} trainers with debut data")

    # ── Score each race ───────────────────────────────────────────────
    print("  Scoring races...")
    all_race_results = []

    # v4.6: Today's going (turf vs AWT) — used to filter place_rate.
    _GT = (args.going_turf or "").strip()
    _GA = (args.going_awt  or "").strip()

    for race in rc["races"]:
        meta = race["meta"]
        rnum = meta["race_number"]
        dist = meta["distance"]
        surface = meta["surface"]
        course = meta.get("race_course", "")
        rclass = meta.get("race_class", "")
        rname = meta.get("race_name", "")
        rating_range = meta.get("rating_range", "")
        prize = meta.get("prize", "")

        # Resolve today's going for this race (racecard meta or CLI fallback)
        meta_going = str(meta.get("going", "") or "").strip()
        if surface and "AWT" in str(surface).upper():
            today_going = meta_going or _GA
        else:
            today_going = meta_going or _GT
        # Map full label to short DB code so going_band() works on history
        _GOING_TO_CODE = {
            "Good": "G", "Good-to-Firm": "GF", "Good to Firm": "GF",
            "Good-to-Yielding": "GY", "Good to Yielding": "GY",
            "Yielding": "Y", "Soft": "S", "Heavy": "H",
            "Wet Fast": "WF", "Wet Slow": "WS", "Standard": "SE",
        }
        today_going_code = _GOING_TO_CODE.get(today_going, today_going)

        horses = [h for h in race["horses"] if not h.get("is_standby", False)]
        field_sz = len(horses)

        # Collect ratings for field median
        ratings = []
        for h in horses:
            rat = safe_float(h.get("rating"))
            if not np.isnan(rat):
                ratings.append(rat)
        med_rat = np.median(ratings) if ratings else 60.0

        scored = []
        for h in horses:
            hn = h["horse_name"]
            hist = horse_hist.get(hn, [])
            prof = (build_profile(hist, dist, venue, surface, today_going_code)
                    if hist else None)

            if prof is None:
                # v4.6: Debut — use trainer's historical first-up place rate
                # with Bayesian shrinkage toward league baseline (0.20).
                trainer_name = str(h.get("trainer", "")).strip()
                n_t, pr_t = trainer_debut_stats.get(trainer_name, (0, 0.0))
                # Shrunken estimate: (n*pr + k*league) / (n+k)
                pr_est = ((n_t * pr_t + DEBUT_TRAINER_SHRINK_N * DEBUT_LEAGUE_PLACE_RATE)
                          / (n_t + DEBUT_TRAINER_SHRINK_N))
                # Map place rate to SARR score (lower = better). Range roughly
                # [0.05, 0.40] place-rate → SARR [0.65, 0.35]; pivot at 0.20.
                debut_sarr = 0.50 - (pr_est - DEBUT_LEAGUE_PLACE_RATE) * 1.5
                debut_sarr = float(max(0.30, min(0.70, debut_sarr)))
                scored.append({
                    "horse_name": hn,
                    "horse_no": h.get("horse_no", ""),
                    "draw": h.get("draw", ""),
                    "weight": h.get("weight", ""),
                    "jockey": h.get("jockey", ""),
                    "trainer": h.get("trainer", ""),
                    "rating": h.get("rating", ""),
                    "last_6": h.get("last_6_runs", "—"),
                    "gear": h.get("gear", ""),
                    "n_runs": 0,
                    "f_fmrp": 0.0, "f_lsa": 0.0, "f_esz": 0.0,
                    "f_style": 0.0, "f_rating": 0.0, "f_traj": 0.0,
                    "f_wpr": 0.0, "f_dist": 0.0, "draw_adj": 0.0,
                    "sarr": debut_sarr,  # v4.6: trainer-debut-rate aware
                    "style": "?",
                    "avg_ssi": 0.0,
                    "late_std": 0.5,
                    "place_rate": float(pr_est),
                    "is_debut": True,
                })
                continue

            rat = prof["rating"]
            if np.isnan(rat):
                rat = safe_float(h.get("rating"), med_rat)

            # NaN-safe factor extraction — use raw NaN for display, 0.0 for composite
            _nan0 = lambda v: 0.0 if (isinstance(v, float) and np.isnan(v)) else v
            f_fmrp   = prof["fmrp"]
            f_lsa    = prof["lsa"]
            f_esz    = prof["esz"]
            f_style  = get_style_fit(prof["style"], dist, venue)
            f_rating = -(rat - med_rat) / 10.0
            f_traj   = prof["traj"]
            # v4.6: Use going-band-specific place_rate when available; else
            # fall back to overall place_rate.
            pr_for_wpr = prof.get("place_rate_band")
            if pr_for_wpr is None:
                pr_for_wpr = prof["place_rate"]
            f_wpr    = -pr_for_wpr * 5
            f_dist   = abs(_nan0(prof["avg_ssi"]) - IDEAL_SSI.get(int(dist), -0.20))
            draw_adj = get_draw_score(h.get("draw", 0), venue, draw_stats) * 0.3

            sarr = (WEIGHTS["f_fmrp"]   * _nan0(f_fmrp)
                  + WEIGHTS["f_lsa"]    * _nan0(f_lsa)
                  + WEIGHTS["f_esz"]    * _nan0(f_esz)
                  + WEIGHTS["f_style"]  * f_style
                  + WEIGHTS["f_rating"] * f_rating
                  + WEIGHTS["f_traj"]   * f_traj
                  + WEIGHTS["f_wpr"]    * f_wpr
                  + WEIGHTS["f_dist"]   * f_dist
                  + draw_adj)

            scored.append({
                "horse_name": hn,
                "horse_no": h.get("horse_no", ""),
                "draw": h.get("draw", ""),
                "weight": h.get("weight", ""),
                "jockey": h.get("jockey", ""),
                "trainer": h.get("trainer", ""),
                "rating": h.get("rating", ""),
                "last_6": h.get("last_6_runs", "—"),
                "gear": h.get("gear", ""),
                "n_runs": prof["n_runs"],
                "f_fmrp": f_fmrp, "f_lsa": f_lsa, "f_esz": f_esz,
                "f_style": f_style, "f_rating": f_rating,
                "f_traj": f_traj, "f_wpr": f_wpr, "f_dist": f_dist,
                "draw_adj": draw_adj,
                "sarr": sarr,
                "style": prof["style"],
                "avg_ssi": prof["avg_ssi"],
                "late_std": prof["late_std"],
                "place_rate": prof["place_rate"],
                "is_debut": False,
            })

        scored.sort(key=lambda x: x["sarr"])
        for i, s in enumerate(scored):
            s["rank"] = i + 1

        all_race_results.append({
            "meta": meta,
            "scored": scored,
        })

        # Console summary (guard against tiny fields)
        top = scored[:3]
        names = [t["horse_name"] for t in top] + ["—", "—", "—"]
        print(f"  R{rnum:>2} {dist}m {surface[:3]} C{rclass}: "
              f"1.{names[0]} 2.{names[1]} 3.{names[2]}")

    # ── Generate JSON (for dashboard integration) ─────────────────────
    json_path = os.path.join("reports", f"race_day_report_{race_date.replace('-','')}_SARR.json")
    _write_sarr_json(json_path, race_date, venue, n_races, all_race_results)
    print(f"  JSON saved: {json_path}")

    # ── Generate PDF ──────────────────────────────────────────────────
    pdf_path = os.path.join("reports", f"SARR_{race_date}.pdf")
    generate_pdf(pdf_path, race_date, venue, all_race_results)
    print(f"  PDF saved: {pdf_path}")


# ════════════════════════════════════════════════════════════════════════
# JSON OUTPUT (for dashboard integration)
# ════════════════════════════════════════════════════════════════════════
def _write_sarr_json(path, race_date, venue, n_races, all_races):
    """Write a dashboard-compatible JSON report for SARR analysis."""
    from datetime import datetime as _dt

    races_out = []
    for rd in all_races:
        meta = rd["meta"]
        scored = rd["scored"]
        surface = meta.get("surface", "")
        is_awt = "Weather" in str(surface) or "AWT" in str(surface).upper()

        picks = []
        for h in scored:
            picks.append({
                "rank": h["rank"],
                "horse_no": h.get("horse_no", ""),
                "horse_name": h["horse_name"],
                "sarr": round(h["sarr"], 4),
                "f_fmrp": round(h.get("f_fmrp", 0), 4),
                "f_lsa": round(h.get("f_lsa", 0), 4),
                "f_esz": round(h.get("f_esz", 0), 4),
                "avg_ssi": round(h.get("avg_ssi", 0), 3),
                "style": h.get("style", "?"),
                "f_traj": round(h.get("f_traj", 0), 4),
                "place_rate": round(h.get("place_rate", 0), 3),
                "late_std": round(h.get("late_std", 0.5), 3),
                "draw": h.get("draw", ""),
                "weight": h.get("weight", ""),
                "jockey": h.get("jockey", ""),
                "trainer": h.get("trainer", ""),
                "rating": h.get("rating", ""),
                "n_runs": h.get("n_runs", 0),
                "last_6": h.get("last_6", ""),
                "gear": h.get("gear", ""),
                "draw_adj": round(h.get("draw_adj", 0), 4),
                "is_debut": h.get("is_debut", False),
                "flags": [],
            })

        races_out.append({
            "race_number": meta["race_number"],
            "race_name": meta.get("race_name", ""),
            "distance": meta["distance"],
            "race_course": meta.get("race_course", ""),
            "race_class": meta.get("race_class", ""),
            "is_awt": is_awt,
            "runners": len(scored),
            "projected": len(scored),
            "picks": picks,
            "speed_map": None,
        })

    report = {
        "meeting_title": f"SARR Analysis — {race_date} ({venue})",
        "meeting_venue": venue,
        "model_version": "SARR",
        "generated_at": _dt.now().isoformat(timespec="seconds"),
        "pdf_file": f"SARR_{race_date}.pdf",
        "text_file": "",
        "races": races_out,
    }

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)


# ════════════════════════════════════════════════════════════════════════
# PDF GENERATION
# ════════════════════════════════════════════════════════════════════════
def generate_pdf(path, race_date, venue, all_races):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    doc = SimpleDocTemplate(
        str(path), pagesize=landscape(A4),
        leftMargin=12*mm, rightMargin=12*mm,
        topMargin=14*mm, bottomMargin=14*mm,
    )

    # Styles
    sTitle = ParagraphStyle("Title", fontSize=16, leading=20, fontName="Helvetica-Bold")
    sSubtitle = ParagraphStyle("Subtitle", fontSize=9, leading=12, textColor=colors.HexColor("#444444"))
    sRaceTitle = ParagraphStyle("RaceTitle", fontSize=13, leading=16, fontName="Helvetica-Bold")
    sRaceSub = ParagraphStyle("RaceSub", fontSize=9, leading=11, textColor=colors.HexColor("#444444"))
    sTblHdr = ParagraphStyle("TblHdr", fontSize=7, leading=9, alignment=TA_CENTER,
                             textColor=colors.white, fontName="Helvetica-Bold")
    sTblCell = ParagraphStyle("TblCell", fontSize=7, leading=9, alignment=TA_RIGHT)
    sTblCellL = ParagraphStyle("TblCellL", fontSize=7, leading=9, alignment=TA_LEFT)
    sTblCellC = ParagraphStyle("TblCellC", fontSize=7, leading=9, alignment=TA_CENTER)
    sComm = ParagraphStyle("Comm", fontSize=8.5, leading=11)
    sLegend = ParagraphStyle("Legend", fontSize=7.5, leading=10, textColor=colors.HexColor("#666666"))

    story = []

    # Title page header
    story.append(Paragraph(
        f"SARR Race Analysis — {race_date} ({venue})", sTitle))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        "Sectional-Anchored Relative Rating | Independent Prototype<br/>"
        "Factors: FMRP (field-relative speed) + LSA (late sectional) + ESZ (early speed) + "
        "Style Fit + Rating Edge + Trajectory + Win/Place Rate + Distance Affinity + Draw",
        sSubtitle))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f'<font color="{GREEN}"><b>Green = strength</b></font> | '
        f'<font color="{RED}"><b>Red = weakness</b></font> | '
        f'Lower SARR = better projected rank | '
        f'WPR% = historical top-3 rate | '
        f'LSA/ESZ: negative = faster than field',
        sLegend))
    story.append(Spacer(1, 4*mm))

    # Column definitions
    col_headers = ["Rk", "No", "Horse", "Wt", "Dr", "Rtg",
                   "SARR", "FMRP", "LSA", "ESZ", "SSI",
                   "Style", "Traj", "WPR%", "Late\nStd", "Runs",
                   "Last 6", "Jockey"]
    col_widths  = [9*mm, 8*mm, 38*mm, 10*mm, 8*mm, 10*mm,
                   14*mm, 14*mm, 13*mm, 13*mm, 12*mm,
                   15*mm, 12*mm, 12*mm, 11*mm, 9*mm,
                   30*mm, 32*mm]

    for race_data in all_races:
        meta = race_data["meta"]
        scored = race_data["scored"]
        rnum = meta["race_number"]
        dist = meta["distance"]
        surface = meta["surface"]
        course = meta.get("race_course", "")
        rclass = meta.get("race_class", "")
        rname = meta.get("race_name", "")
        rating_range = meta.get("rating_range", "")

        surf_short = "AWT" if "Weather" in str(surface) else "Turf"
        race_title = f"Race {rnum} — {rname}"
        race_sub = f"{dist}m {surf_short} ({course}) | Class {rclass} | Rating: {rating_range} | Field: {len(scored)}"

        # Build table rows
        header_row = [Paragraph(h, sTblHdr) for h in col_headers]
        data_rows = [header_row]

        for h in scored:
            rank = h["rank"]
            is_deb = h.get("is_debut", False)

            # Colour-coded SARR
            sarr_str = col_tag(h["sarr"], lo=-0.30, hi=0.05, fmt="{:+.3f}")
            fmrp_str = col_tag(h["f_fmrp"], lo=None, hi=0.10, fmt="{:+.2f}")
            _fmrp = h["f_fmrp"]
            if isinstance(_fmrp, float) and not np.isnan(_fmrp) and _fmrp < -0.30:
                fmrp_str = f'<font color="{GREEN}"><b>{_fmrp:+.2f}</b></font>'
            lsa_str = col_tag(h["f_lsa"], lo=-0.15, hi=0.15, fmt="{:+.2f}")
            esz_str = col_tag(h["f_esz"], lo=-0.15, hi=0.15, fmt="{:+.2f}")
            ssi_str = col_tag(h["avg_ssi"], lo=-0.20, hi=0.20, fmt="{:+.2f}")

            _traj = h["f_traj"]
            traj_str = fvs(_traj, "{:+.3f}")
            if isinstance(_traj, float) and not np.isnan(_traj) and _traj < -0.03:
                traj_str = f'<font color="{GREEN}"><b>{_traj:+.3f}</b></font>'
            elif isinstance(_traj, float) and not np.isnan(_traj) and _traj > 0.03:
                traj_str = f'<font color="{RED}">{_traj:+.3f}</font>'

            wpr_pct = h["place_rate"] * 100
            wpr_str = f"{wpr_pct:.0f}%"
            if wpr_pct >= 40:
                wpr_str = f'<font color="{GREEN}"><b>{wpr_pct:.0f}%</b></font>'
            elif wpr_pct <= 10:
                wpr_str = f'<font color="{RED}">{wpr_pct:.0f}%</font>'

            _ls = h["late_std"]
            late_std_str = fv(_ls, "{:.2f}")
            if isinstance(_ls, float) and not np.isnan(_ls) and _ls <= 0.20:
                late_std_str = f'<font color="{GREEN}"><b>{_ls:.2f}</b></font>'
            elif isinstance(_ls, float) and not np.isnan(_ls) and _ls >= 0.50:
                late_std_str = f'<font color="{RED}">{_ls:.2f}</font>'

            style_str = h["style"]
            if is_deb:
                style_str = "DEBUT"
                fmrp_str = "—"
                lsa_str = "—"
                esz_str = "—"
                ssi_str = "—"
                traj_str = "—"
                wpr_str = "—"
                late_std_str = "—"

            row = [
                Paragraph(str(rank), sTblCellC),
                Paragraph(str(h["horse_no"]), sTblCellC),
                Paragraph(h["horse_name"], sTblCellL),
                Paragraph(str(h["weight"]), sTblCellC),
                Paragraph(str(h["draw"]), sTblCellC),
                Paragraph(str(h["rating"]), sTblCellC),
                Paragraph(sarr_str, sTblCell),
                Paragraph(fmrp_str, sTblCell),
                Paragraph(lsa_str, sTblCell),
                Paragraph(esz_str, sTblCell),
                Paragraph(ssi_str, sTblCell),
                Paragraph(style_str, sTblCellC),
                Paragraph(traj_str, sTblCell),
                Paragraph(wpr_str, sTblCellC),
                Paragraph(late_std_str, sTblCell),
                Paragraph(str(h["n_runs"]), sTblCellC),
                Paragraph(h.get("last_6", "—"), sTblCellL),
                Paragraph(h.get("jockey", ""), sTblCellL),
            ]
            data_rows.append(row)

        # Table style
        ts_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), HDR_BG),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ]
        # Top-N highlights (guard against empty/tiny fields)
        n_rows = len(data_rows) - 1  # excluding header
        top_bgs = [TOP1_BG, TOP2_BG, TOP3_BG]
        for rank_idx in range(min(3, n_rows)):
            ts_cmds.append(
                ("BACKGROUND", (0, rank_idx + 1), (-1, rank_idx + 1), top_bgs[rank_idx])
            )
        # Alternating rows from row 5 onwards
        for i in range(5, len(data_rows), 2):
            ts_cmds.append(("BACKGROUND", (0, i), (-1, i), ROW_ALT))

        tbl = Table(data_rows, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(TableStyle(ts_cmds))

        # Commentary
        top3 = scored[:3]
        commentary_lines = []
        if not top3:
            commentary_lines.append("<b>SARR:</b> no scored runners for this race.")
        else:
            tp = top3[0]
            runs_note = "DEBUT (no form)" if tp.get("is_debut") else f"{tp['n_runs']} runs"
            commentary_lines.append(
                f"<b>SARR Top Pick: {tp['horse_name']}</b> — "
                f"FMRP {tp['f_fmrp']:+.2f}, LSA {tp['f_lsa']:+.2f}, "
                f"Style: {tp['style']}, {runs_note}"
            )

        # Flag any strong divergences
        for h in scored[:4]:
            flags = []
            if h["f_lsa"] < -0.30:
                flags.append("elite finisher")
            if h["f_esz"] < -0.20:
                flags.append("fast early speed")
            if h["f_traj"] < -0.05:
                flags.append("improving form")
            if h["f_traj"] > 0.05:
                flags.append("declining form")
            if h["late_std"] <= 0.15:
                flags.append("very consistent finisher")
            if h["late_std"] >= 0.60:
                flags.append("inconsistent")
            if h.get("is_debut"):
                flags.append("DEBUT — no historical data")
            if flags:
                commentary_lines.append(
                    f"• {h['horse_name']}: {', '.join(flags)}"
                )

        # Distance fit note
        if dist >= 1600:
            strong_finishers = [h for h in scored[:6] if h["avg_ssi"] < -0.20 and not h.get("is_debut")]
            if strong_finishers:
                names = ", ".join(h["horse_name"] for h in strong_finishers[:3])
                commentary_lines.append(
                    f"• Distance fit (SSI < -0.20 in top 6): {names} — "
                    f"strong finisher profiles suit {dist}m"
                )

        race_elements = [
            Paragraph(race_title, sRaceTitle),
            Spacer(1, 1*mm),
            Paragraph(race_sub, sRaceSub),
            Spacer(1, 2*mm),
            tbl,
            Spacer(1, 2*mm),
        ]
        for line in commentary_lines:
            race_elements.append(Paragraph(line, sComm))
        race_elements.append(Spacer(1, 2*mm))

        story.append(KeepTogether(race_elements))
        story.append(PageBreak())

    # ── Methodology appendix ─────────────────────────────────────────
    story.append(Paragraph("SARR Methodology — Quick Reference", sTitle))
    story.append(Spacer(1, 3*mm))

    method_text = """
    <b>SARR (Sectional-Anchored Relative Rating)</b> ranks horses by a weighted composite
    of 8 factors derived from field-relative historical performance. Lower SARR = better.<br/><br/>

    <b>FMRP</b> (Field-Median Relative Performance) — How fast/slow the horse finishes vs the median
    finisher in each of its races, recency-weighted (λ=0.85). Negative = faster than field.
    <i>Dominant factor (β=0.290, ρ≈0.47 vs place).</i><br/><br/>

    <b>LSA</b> (Late Sectional Ability) — The horse's historical late_dev: how fast it runs the
    final 400m relative to the race-median final 400m. Negative = strong finisher.
    <i>Second strongest independent signal (ρ≈0.26).</i><br/><br/>

    <b>ESZ</b> (Early Speed Z) — Same concept but for the first 400m. Negative = fast starter.
    Captures tactical position advantage, especially at sprints and HV.<br/><br/>

    <b>SSI</b> (Sectional Shape Index) = late_dev − early_dev. Negative = improves position from
    start to finish. Used for distance affinity scoring — middle-distance winners need SSI &lt; -0.30.<br/><br/>

    <b>Style</b> — Dominant running style from historical positions. Fit score uses empirical
    top-3 rate multipliers by distance × venue (e.g. Leaders +70% at 1200m, Closers −57%).<br/><br/>

    <b>Traj</b> (Trajectory) — Linear regression slope of recent FMRP. Negative = improving.<br/><br/>

    <b>WPR%</b> — Historical top-3 placing rate. 40%+ is elite. Below 10% signals struggling form.<br/><br/>

    <b>Late Std</b> — Standard deviation of late_dev across recent runs. Low (&lt;0.20) = reliable finisher.
    High (&gt;0.50) = inconsistent, higher-variance projection.<br/><br/>

    <b>Draw Adj</b> — Bayesian-shrunk draw advantage from historical data per venue/draw number.<br/><br/>

    <b>Validation (Apr 2026):</b> Mean Spearman ρ = +0.405 across 46 races.
    Outperformed ET-Residual (ρ = +0.384) on comparable meetings.
    Top-1 pick win rate: 15.2%. Top-3 overlap: 1.33/3.
    """
    story.append(Paragraph(method_text, ParagraphStyle(
        "Method", fontSize=8.5, leading=12, spaceAfter=3*mm)))

    doc.build(story)
    print(f"  PDF generated: {path} ({len(all_races)} races)")


if __name__ == "__main__":
    main()
