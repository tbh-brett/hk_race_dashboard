"""
fuse_model.py — FUSE (Fundamental Unified Signal Engine)
=========================================================

A leak-free LightGBM fundamental model over the full hkjc.db history,
blended with the live market and (optionally) the ET / SARR engines.
Outputs per-runner WIN / PLACE probabilities plus a Henery-corrected
quinella pair head.

Design notes
------------
- One long table, one row per runner-start, sorted by (race_date, race_number).
- Every rolling feature is shifted: it only sees rows strictly BEFORE the
  current race date (time-windowed features use closed='left' so same-day
  races at the same meeting are excluded too).
- Inference appends the day's racecard rows (outcomes NaN) to the long
  table and recomputes features — guaranteeing train/serve parity.
- Two model variants are trained side by side:
    * fund : fundamental only (no market features)
    * mkt  : fundamental + SP/live odds features
- Final production probability = log-opinion pool of (fund, market[, SARR, ET]).

CLI
---
  python fuse_model.py backtest [--start 2026-04-01] [--end 2026-06-10]
  python fuse_model.py train    [--out models/fuse_v1.pkl]
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import re
import sqlite3
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

BASE = Path(__file__).parent
DB = BASE / "hkjc.db"
REPORTS = BASE / "reports"
MODELS = BASE / "models"

# ---------------------------------------------------------------------------
# Vocabulary (structural, outcome-independent — safe to hard-code)
# ---------------------------------------------------------------------------
GOING_ORD = {
    "F": 1.0, "GF": 1.5, "G": 2.0, "GY": 3.0, "Y": 4.0, "YS": 5.0, "S": 6.0,
    "H": 7.0,
    # AWT states
    "WF": 2.0, "FT": 2.0, "GD": 2.5, "WS": 4.0, "SL": 5.0, "SEALED": 4.5,
    # verbose variants seen in DB
    "GOOD": 2.0, "GOOD TO FIRM": 1.5, "GOOD TO FIRM FAST": 1.5,
    "GOOD TO YIELDING": 3.0, "YIELDING": 4.0, "YIELDING TO SOFT": 5.0,
    "SOFT": 6.0, "WET FAST": 2.0, "WET SLOW": 4.0, "FAST": 1.0,
}
CLASS_MAP = {
    "1": 1.0, "2": 2.0, "3": 3.0, "4": 4.0, "5": 5.0,
    "0": 0.0,  # Group / graded
    "GRIFFIN RACE": 5.5, "GRIFFIN": 5.5,
    "GROUP 1": 0.0, "GROUP 2": 0.3, "GROUP 3": 0.6, "G1": 0.0, "G2": 0.3,
    "G3": 0.6, "4 (RESTRICTED)": 4.0, "4 (SPECIAL CONDITIONS)": 4.0,
    "3 (SPECIAL CONDITIONS)": 3.0,
}
COURSE_MAP = {"A": 0, "A+3": 1, "B": 2, "B+2": 3, "C": 4, "C+3": 5, "AWT": 6}

FUND_FEATURES = [
    # today's card
    "draw", "draw_pct", "field_size", "dist", "class_num", "going_ord",
    "venue_i", "surface_i", "course_i", "rating", "act_wt", "dec_wt",
    "gear_first", "yrs_import", "days_since",
    "rating_rel", "wt_rel", "dist_change", "draw_bias_top3",
    "jockey_switch", "j_win_delta", "early_draw_ix",
    # horse rolling
    "hx_starts", "hx_win_rate", "hx_top3_rate",
    "hx_last_fp", "hx_last_fp_pct", "hx_avg_fp_pct3", "hx_avg_fp_pct5",
    "hx_last_beaten", "hx_avg_beaten3",
    "hx_last_sf", "hx_avg_sf3", "hx_best_sf5",
    "hx_last_close_rel", "hx_avg_close_rel3",
    "hx_early_pct5",
    "hx_fp_pct_std5",
    "hx_vs_starts", "hx_vs_top3_rate",
    "hx_db_top3_rate", "hx_cd_starts", "hx_cd_top3_rate",
    "hx_wt_delta", "hx_decwt_delta", "hx_rating_delta", "hx_rating_vs_lastwin",
    "hx_class_delta",
    # people
    "j_n180", "j_win180", "j_top3_180",
    "t_n180", "t_win180", "t_top3_180",
    "jt_win365", "j_venue_win365",
]
MKT_FEATURES = FUND_FEATURES + ["ln_odds", "sp_implied", "odds_rank"]

LGB_PARAMS = dict(
    objective="binary", n_estimators=600, learning_rate=0.04,
    num_leaves=31, min_child_samples=40, subsample=0.9, subsample_freq=1,
    colsample_bytree=0.8, reg_alpha=0.1, reg_lambda=1.0,
    verbose=-1, n_jobs=-1,
)

GLOBAL_WIN_PRIOR = 0.08      # ≈ 1 / mean field size; structural constant
GLOBAL_TOP3_PRIOR = 0.24


# ---------------------------------------------------------------------------
# Loading & cleaning
# ---------------------------------------------------------------------------
def _place_num(s) -> float:
    m = re.match(r"\s*(\d+)", str(s))
    return float(m.group(1)) if m else np.nan


def _last_section(s) -> float:
    try:
        parts = [p.strip() for p in str(s).split(";")]
        vals = [float(p) for p in parts if p and re.match(r"^\d+(\.\d+)?$", p)]
        return vals[-1] if vals else np.nan
    except Exception:
        return np.nan


def _first_pos(s) -> float:
    m = re.match(r"\s*(\d+)", str(s))
    return float(m.group(1)) if m else np.nan


def _clean_jockey(s) -> str:
    # "C L Chau (-2)" -> "C L Chau"
    return re.sub(r"\s*\(.*?\)\s*", "", str(s or "")).strip()


def _norm_going(s) -> float:
    return GOING_ORD.get(str(s or "").strip().upper(), 2.5)


def _norm_class(s) -> float:
    return CLASS_MAP.get(str(s or "").strip().upper(), np.nan)


def _imp_year(hid) -> float:
    m = re.search(r"_(\d{4})_", str(hid or ""))
    return float(m.group(1)) if m else np.nan


def load_history(db_path: Path = DB) -> pd.DataFrame:
    """One cleaned row per runner-start (rows that actually ran)."""
    con = sqlite3.connect(str(db_path))
    df = pd.read_sql(
        """SELECT horse_id, race_date, race_number, horse_number, horse_name,
                  place, finish_time_seconds, draw, going, running_positions,
                  sectiontimes, gear, rating, distance, jockey, trainer,
                  actual_weight, win_odds, declared_weight, race_course,
                  race_class, race_track, track_type
           FROM results""", con)
    con.close()
    return clean_table(df)


def clean_table(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame()
    out["horse_id"] = df["horse_id"].astype(str)
    out["horse_name"] = df["horse_name"].astype(str).str.upper().str.strip()
    out["race_date"] = pd.to_datetime(df["race_date"])
    out["race_number"] = pd.to_numeric(df["race_number"], errors="coerce")
    out["horse_no"] = pd.to_numeric(df.get("horse_number"), errors="coerce")
    out["place_num"] = df["place"].map(_place_num)
    out["fin_t"] = pd.to_numeric(df["finish_time_seconds"], errors="coerce")
    out["draw"] = pd.to_numeric(df["draw"], errors="coerce")
    out["going_ord"] = df["going"].map(_norm_going)
    out["first_pos"] = df["running_positions"].map(_first_pos)
    out["last_sec"] = df["sectiontimes"].map(_last_section)
    gear = df["gear"].astype(str)
    out["gear_first"] = gear.str.contains(r"1", regex=True).astype(float)
    out.loc[gear.isin(["None", "nan", ""]), "gear_first"] = 0.0
    out["rating"] = pd.to_numeric(df["rating"], errors="coerce")
    out["dist"] = pd.to_numeric(df["distance"], errors="coerce")
    out["jockey"] = df["jockey"].map(_clean_jockey)
    out["trainer"] = df["trainer"].astype(str).str.strip()
    out["act_wt"] = pd.to_numeric(df["actual_weight"], errors="coerce")
    out["dec_wt"] = pd.to_numeric(df["declared_weight"], errors="coerce")
    out["win_odds"] = pd.to_numeric(df["win_odds"], errors="coerce")
    out["course_i"] = df["race_course"].astype(str).str.strip().map(
        lambda c: COURSE_MAP.get(c, -1))
    out["class_num"] = df["race_class"].map(_norm_class)
    out["venue_i"] = (df["race_track"].astype(str).str.strip() == "HV").astype(int)
    out["surface_i"] = df["track_type"].astype(str).str.contains(
        "All Weather", case=False).astype(int)
    # keep only rows that ran (place parses) — WV/WX rows never jumped
    out = out[out["place_num"].notna()].copy()
    out["won"] = (out["place_num"] == 1).astype(int)
    out["top2"] = (out["place_num"] <= 2).astype(int)
    out["top3"] = (out["place_num"] <= 3).astype(int)
    return out


# ---------------------------------------------------------------------------
# Feature engineering (leak-free)
# ---------------------------------------------------------------------------
def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """df = history rows (+ optionally appended card rows with NaN outcomes).

    Returns same frame with feature columns added.  Every history-derived
    column is shifted so row i only sees rows strictly before its date.
    """
    df = df.sort_values(["race_date", "race_number"]).reset_index(drop=True)
    rkey = ["race_date", "race_number"]

    # --- within-race context (uses only that race's own runners) ----------
    grp = df.groupby(rkey)
    df["field_size"] = grp["horse_id"].transform("size")
    df["draw_pct"] = df["draw"] / df["field_size"]
    win_t = grp["fin_t"].transform("min")
    df["beaten_sec"] = df["fin_t"] - win_t
    # speed figure: beaten seconds per 1000m (negated → higher = better)
    df["speed_fig"] = -(df["beaten_sec"] / df["dist"] * 1000.0)
    med_close = grp["last_sec"].transform("median")
    df["close_rel"] = df["last_sec"] - med_close          # negative = faster close
    df["fp_pct"] = df["place_num"] / df["field_size"]
    df["early_pct"] = df["first_pos"] / df["field_size"]
    # pre-race within-race relatives (card data only — no outcome leak)
    df["rating_rel"] = df["rating"] - grp["rating"].transform("median")
    df["wt_rel"] = df["act_wt"] - grp["act_wt"].transform("median")

    df["dist_bucket"] = pd.cut(df["dist"], [0, 1200, 1650, 9999],
                               labels=[0, 1, 2]).astype(float)
    df["yrs_import"] = df["race_date"].dt.year - df["horse_id"].map(_imp_year)

    # --- per-horse expanding/rolling (shifted) -----------------------------
    g = df.groupby("horse_id", sort=False)
    df["days_since"] = (df["race_date"] - g["race_date"].shift()).dt.days

    def shifted(col):
        return g[col].shift()

    df["_p_won"] = shifted("won")
    df["_p_top3"] = shifted("top3")
    df["_p_fp"] = shifted("place_num")
    df["_p_fp_pct"] = shifted("fp_pct")
    df["_p_beaten"] = shifted("beaten_sec")
    df["_p_sf"] = shifted("speed_fig")
    df["_p_close"] = shifted("close_rel")
    df["_p_early"] = shifted("early_pct")
    df["_p_actwt"] = shifted("act_wt")
    df["_p_decwt"] = shifted("dec_wt")
    df["_p_rating"] = shifted("rating")
    df["_p_class"] = shifted("class_num")
    df["_p_dist"] = shifted("dist")
    df["_p_jockey"] = g["jockey"].shift()

    g2 = df.groupby("horse_id", sort=False)
    df["hx_starts"] = g2.cumcount()
    df["hx_win_rate"] = g2["_p_won"].transform(lambda s: s.expanding().mean())
    df["hx_top3_rate"] = g2["_p_top3"].transform(lambda s: s.expanding().mean())
    df["hx_last_fp"] = df["_p_fp"]
    df["hx_last_fp_pct"] = df["_p_fp_pct"]
    df["hx_avg_fp_pct3"] = g2["_p_fp_pct"].transform(
        lambda s: s.rolling(3, min_periods=1).mean())
    df["hx_avg_fp_pct5"] = g2["_p_fp_pct"].transform(
        lambda s: s.rolling(5, min_periods=1).mean())
    df["hx_fp_pct_std5"] = g2["_p_fp_pct"].transform(
        lambda s: s.rolling(5, min_periods=2).std())
    df["hx_last_beaten"] = df["_p_beaten"]
    df["hx_avg_beaten3"] = g2["_p_beaten"].transform(
        lambda s: s.rolling(3, min_periods=1).mean())
    df["hx_last_sf"] = df["_p_sf"]
    df["hx_avg_sf3"] = g2["_p_sf"].transform(
        lambda s: s.rolling(3, min_periods=1).mean())
    df["hx_best_sf5"] = g2["_p_sf"].transform(
        lambda s: s.rolling(5, min_periods=1).max())
    df["hx_last_close_rel"] = df["_p_close"]
    df["hx_avg_close_rel3"] = g2["_p_close"].transform(
        lambda s: s.rolling(3, min_periods=1).mean())
    df["hx_early_pct5"] = g2["_p_early"].transform(
        lambda s: s.rolling(5, min_periods=1).mean())
    df["hx_wt_delta"] = df["act_wt"] - df["_p_actwt"]
    df["hx_decwt_delta"] = df["dec_wt"] - df["_p_decwt"]
    df["hx_rating_delta"] = df["rating"] - df["_p_rating"]
    df["hx_class_delta"] = df["class_num"] - df["_p_class"]

    # rating at last win
    df["_rating_if_win"] = df["rating"].where(df["won"] == 1)
    df["_last_win_rating"] = df.groupby("horse_id", sort=False)[
        "_rating_if_win"].transform(lambda s: s.shift().ffill())
    df["hx_rating_vs_lastwin"] = df["rating"] - df["_last_win_rating"]
    df["dist_change"] = df["dist"] - df["_p_dist"]
    df["jockey_switch"] = (
        (df["jockey"] != df["_p_jockey"]) & df["_p_jockey"].notna()
    ).astype(float)

    # venue+surface form
    gvs = df.groupby(["horse_id", "venue_i", "surface_i"], sort=False)
    df["_vs_top3"] = gvs["top3"].shift()
    gvs2 = df.groupby(["horse_id", "venue_i", "surface_i"], sort=False)
    df["hx_vs_starts"] = gvs2.cumcount()
    df["hx_vs_top3_rate"] = gvs2["_vs_top3"].transform(
        lambda s: s.expanding().mean())

    # distance-bucket form
    gdb = df.groupby(["horse_id", "dist_bucket"], sort=False)
    df["_db_top3"] = gdb["top3"].shift()
    gdb2 = df.groupby(["horse_id", "dist_bucket"], sort=False)
    df["hx_db_top3_rate"] = gdb2["_db_top3"].transform(
        lambda s: s.expanding().mean())

    # exact course+distance form
    gcd = df.groupby(["horse_id", "venue_i", "course_i", "dist"], sort=False)
    df["_cd_top3"] = gcd["top3"].shift()
    gcd2 = df.groupby(["horse_id", "venue_i", "course_i", "dist"], sort=False)
    df["hx_cd_starts"] = gcd2.cumcount()
    df["hx_cd_top3_rate"] = gcd2["_cd_top3"].transform(
        lambda s: s.expanding().mean())

    # --- jockey / trainer time-windowed (closed='left' => strictly before today)
    def _windowed(group_key: str | list, col_out: str, days: int,
                  prior: float, k: float, target: str):
        keys = group_key if isinstance(group_key, list) else [group_key]
        tmp = df[keys + ["race_date", target]].copy()
        sums = np.zeros(len(df))
        cnts = np.zeros(len(df))
        for _, pos in tmp.groupby(keys, sort=False).indices.items():
            sub = tmp.iloc[pos].set_index("race_date")[target]
            roll = sub.rolling(f"{days}D", closed="left")
            sums[pos] = roll.sum().fillna(0.0).to_numpy()
            cnts[pos] = roll.count().fillna(0.0).to_numpy()
        df[col_out] = (sums + k * prior) / (cnts + k)
        return cnts

    df = df.sort_values(["race_date", "race_number"]).reset_index(drop=True)
    n_j = _windowed("jockey", "j_win180", 180, GLOBAL_WIN_PRIOR, 15, "won")
    df["j_n180"] = n_j
    _windowed("jockey", "j_top3_180", 180, GLOBAL_TOP3_PRIOR, 15, "top3")
    n_t = _windowed("trainer", "t_win180", 180, GLOBAL_WIN_PRIOR, 15, "won")
    df["t_n180"] = n_t
    _windowed("trainer", "t_top3_180", 180, GLOBAL_TOP3_PRIOR, 15, "top3")
    _windowed(["jockey", "trainer"], "jt_win365", 365, GLOBAL_WIN_PRIOR, 8, "won")
    _windowed(["jockey", "venue_i"], "j_venue_win365", 365,
              GLOBAL_WIN_PRIOR, 12, "won")
    # draw bias: historical top-3 rate of this draw at (venue, course, dist)
    _windowed(["venue_i", "course_i", "dist", "draw"], "draw_bias_top3",
              1500, GLOBAL_TOP3_PRIOR, 25, "top3")
    # jockey upgrade: today's jockey 180d win rate minus last start's jockey's
    last_jw = df.groupby("horse_id", sort=False)["j_win180"].shift()
    df["j_win_delta"] = df["j_win180"] - last_jw
    # early speed from good draw (front-runner + inside gate interaction)
    df["early_draw_ix"] = (1.0 - df["hx_early_pct5"].fillna(0.5)) * \
        (1.0 - df["draw_pct"].fillna(0.5))

    # --- market ------------------------------------------------------------
    df["ln_odds"] = np.log(df["win_odds"].clip(lower=1.01))
    inv = 1.0 / df["win_odds"].clip(lower=1.01)
    df["sp_implied"] = inv / df.groupby(rkey)["win_odds"].transform(
        lambda s: (1.0 / s.clip(lower=1.01)).sum())
    df["odds_rank"] = df.groupby(rkey)["win_odds"].rank(method="min")

    drop = [c for c in df.columns if c.startswith("_")]
    return df.drop(columns=drop)


# ---------------------------------------------------------------------------
# Training / prediction
# ---------------------------------------------------------------------------
def train_models(feat: pd.DataFrame, cutoff: pd.Timestamp) -> dict:
    """Train fund + mkt variants (win & top3 heads) on rows < cutoff."""
    from lightgbm import LGBMClassifier

    tr = feat[(feat["race_date"] < cutoff) & feat["place_num"].notna()]
    models = {}
    for variant, cols in (("fund", FUND_FEATURES), ("mkt", MKT_FEATURES)):
        for target in ("won", "top2", "top3"):
            m = LGBMClassifier(**LGB_PARAMS)
            m.fit(tr[cols], tr[target])
            models[f"{variant}_{target}"] = m
    models["cutoff"] = str(cutoff.date())
    models["n_train"] = int(len(tr))
    return models


def predict_meeting(models: dict, feat: pd.DataFrame,
                    date: pd.Timestamp) -> pd.DataFrame:
    """Per-runner probabilities for one meeting, race-normalized."""
    day = feat[feat["race_date"] == date].copy()
    if day.empty:
        return day
    for variant, cols in (("fund", FUND_FEATURES), ("mkt", MKT_FEATURES)):
        if variant == "mkt" and day["win_odds"].isna().all():
            day[f"p_{variant}_win"] = np.nan
            day[f"p_{variant}_top2"] = np.nan
            day[f"p_{variant}_top3"] = np.nan
            continue
        day[f"p_{variant}_win"] = models[f"{variant}_won"].predict_proba(
            day[cols])[:, 1]
        day[f"p_{variant}_top2"] = models[f"{variant}_top2"].predict_proba(
            day[cols])[:, 1]
        day[f"p_{variant}_top3"] = models[f"{variant}_top3"].predict_proba(
            day[cols])[:, 1]
    # normalize win probs within race; scale top2/top3 to sum≈2/3
    for variant in ("fund", "mkt"):
        pcol = f"p_{variant}_win"
        if day[pcol].isna().all():
            continue
        g = day.groupby("race_number")[pcol]
        day[pcol] = day[pcol] / g.transform("sum")
        for tcol, tot in ((f"p_{variant}_top2", 2.0), (f"p_{variant}_top3", 3.0)):
            gs = day.groupby("race_number")[tcol].transform("sum")
            day[tcol] = (day[tcol] / gs * tot).clip(upper=0.97)
    return day


# ---------------------------------------------------------------------------
# Quinella head (Henery-discounted Harville)
# ---------------------------------------------------------------------------
def pair_probs(p: np.ndarray, lam: float = 0.81) -> dict[tuple[int, int], float]:
    """P(unordered pair {i,j} fills top-2) via discounted Harville."""
    q = np.power(np.clip(p, 1e-6, None), lam)
    q = q / q.sum()
    out = {}
    n = len(q)
    for i in range(n):
        for j in range(i + 1, n):
            pij = q[i] * q[j] / (1 - q[i]) + q[j] * q[i] / (1 - q[j])
            out[(i, j)] = pij
    return out


def fit_henery_lambda(probs: list[np.ndarray], top2_idx: list[set]) -> float:
    """Grid-fit the discount λ by top-2 pair log-likelihood."""
    grid = np.arange(0.55, 1.01, 0.05)
    best, best_ll = 1.0, -1e18
    for lam in grid:
        ll = 0.0
        for p, t2 in zip(probs, top2_idx):
            if len(t2) != 2:
                continue
            pp = pair_probs(p, lam)
            i, j = sorted(t2)
            ll += math.log(max(pp.get((i, j), 1e-9), 1e-9))
        if ll > best_ll:
            best_ll, best = ll, lam
    return float(best)


# ---------------------------------------------------------------------------
# Log-opinion pool
# ---------------------------------------------------------------------------
def log_pool(prob_sets: list[np.ndarray], weights: list[float]) -> np.ndarray:
    z = np.zeros_like(prob_sets[0], dtype=float)
    wtot = 0.0
    for p, w in zip(prob_sets, weights):
        if p is None or w == 0:
            continue
        z += w * np.log(np.clip(p, 1e-9, None))
        wtot += w
    if wtot == 0:
        return prob_sets[0]
    e = np.exp(z / 1.0)
    return e / e.sum()


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------
def _load_report_probs(dc: str) -> dict:
    """{(race_number, NAME): {'p_et':., 'et_rank':., 'p_sarr':., 'sarr_rank':.}}"""
    out = {}
    etp = REPORTS / f"race_day_report_{dc}_v4.4.json"
    sap = REPORTS / f"race_day_report_{dc}_SARR.json"
    if etp.exists():
        try:
            data = json.loads(etp.read_text(encoding="utf-8"))
            for r in data.get("races", []):
                rn = r.get("race_number")
                picks = r.get("picks") or []
                tot = sum(float(p.get("win_prob") or 0) for p in picks) or 1.0
                for p in picks:
                    key = (rn, str(p.get("horse_name", "")).upper().strip())
                    out.setdefault(key, {})["p_et"] = float(
                        p.get("win_prob") or 0) / tot
                    out[key]["et_rank"] = p.get("rank")
        except Exception:
            pass
    if sap.exists():
        try:
            data = json.loads(sap.read_text(encoding="utf-8"))
            for r in data.get("races", []):
                rn = r.get("race_number")
                picks = r.get("picks") or r.get("runners") or []
                vals = [float(p.get("sarr")) for p in picks
                        if p.get("sarr") is not None]
                if not vals:
                    continue
                mu, sd = float(np.mean(vals)), float(np.std(vals) or 1.0)
                zs = {}
                for p in picks:
                    if p.get("sarr") is None:
                        continue
                    key = (rn, str(p.get("horse_name", "")).upper().strip())
                    zs[key] = -(float(p["sarr"]) - mu) / sd  # higher = better
                ez = {k: math.exp(v / 0.6) for k, v in zs.items()}
                tot = sum(ez.values()) or 1.0
                for p in picks:
                    key = (rn, str(p.get("horse_name", "")).upper().strip())
                    if key in ez:
                        out.setdefault(key, {})["p_sarr"] = ez[key] / tot
                        out[key]["sarr_rank"] = p.get("rank")
        except Exception:
            pass
    return out


def _load_dividends(dc: str) -> dict:
    """{(race_number, pool, frozenset(combination)): payout_per_$1}"""
    p = REPORTS / f"dividends_{dc}.json"
    out = {}
    if not p.exists():
        return out
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        for r in data.get("races", []):
            rn = r.get("race_number")
            for d in r.get("dividends", []):
                pool = d.get("pool")
                try:
                    combo = frozenset(int(x) for x in
                                      str(d.get("combination")).split(","))
                except ValueError:
                    continue
                div = d.get("dividend_per_10")
                if div is None:
                    continue
                out[(rn, pool, combo)] = float(div) / 10.0
    except Exception:
        pass
    return out


def run_backtest(start: str, end: str, refit_every: int = 1,
                 out_json: Path | None = None) -> dict:
    t0 = pd.Timestamp.now()
    hist = load_history()
    feat = add_features(hist)
    meetings = sorted(feat.loc[(feat["race_date"] >= start)
                               & (feat["race_date"] <= end),
                               "race_date"].unique())
    print(f"[fuse] features ready ({len(feat)} rows) in "
          f"{(pd.Timestamp.now()-t0).total_seconds():.1f}s; "
          f"{len(meetings)} test meetings {start} → {end}")

    # ---- fit Henery λ + blend weight on a pre-window validation -----------
    lam = 0.81
    w_market = 1.0
    try:
        lam_lo = pd.Timestamp(start) - pd.Timedelta(days=150)
        val_meets = sorted(feat.loc[(feat["race_date"] >= lam_lo)
                                    & (feat["race_date"] < start),
                                    "race_date"].unique())
        if len(val_meets) >= 8:
            mdl = train_models(feat, pd.Timestamp(val_meets[0]))
            ps, t2s, val_recs = [], [], []
            for d in val_meets:
                day = predict_meeting(mdl, feat, pd.Timestamp(d))
                for rn, race in day.groupby("race_number"):
                    p = race["p_mkt_win"].to_numpy()
                    if np.isnan(p).any():
                        continue
                    ps.append(p)
                    pl = race["place_num"].to_numpy()
                    t2s.append(set(np.where(pl <= 2)[0]))
                    val_recs.append((p, race["sp_implied"].to_numpy(), pl))
            lam = fit_henery_lambda(ps, t2s)
            # tune market anchor weight by win1+plc1 on validation
            best_score = -1.0
            for w in (0.0, 0.5, 1.0, 1.5, 2.0):
                wn = pl1 = n = 0
                for p, inv, pl in val_recs:
                    if np.isnan(inv).any():
                        continue
                    pf = log_pool([p, inv / inv.sum()], [1.0, w])
                    top = int(np.argmax(pf))
                    wn += int(pl[top] == 1)
                    pl1 += int(pl[top] <= 3)
                    n += 1
                sc = (wn + pl1) / max(n, 1)
                if sc > best_score:
                    best_score, w_market = sc, w
            print(f"[fuse] Henery λ fitted on {len(ps)} races: {lam:.2f}; "
                  f"market anchor w={w_market}")
    except Exception as e:
        print(f"[fuse] λ/weight fit failed ({e}); defaults λ=0.81 w=1.0")

    rows = []          # per-race records
    models = None
    for mi, d in enumerate(meetings):
        d = pd.Timestamp(d)
        if models is None or mi % refit_every == 0:
            models = train_models(feat, d)
        day = predict_meeting(models, feat, d)
        dc = d.strftime("%Y%m%d")
        rp = _load_report_probs(dc)
        divs = _load_dividends(dc)
        for rn, race in day.groupby("race_number"):
            race = race.reset_index(drop=True)
            n = len(race)
            if n < 4 or race["place_num"].isna().all():
                continue
            rec = {
                "date": dc, "race": int(rn), "n": n,
                "venue": "HV" if race["venue_i"].iloc[0] == 1 else "ST",
                "surface": "AWT" if race["surface_i"].iloc[0] == 1 else "Turf",
                "dist": float(race["dist"].iloc[0]),
                "class": float(race["class_num"].iloc[0]) if pd.notna(race["class_num"].iloc[0]) else None,
            }
            place = race["place_num"].to_numpy()
            rec["_place"] = place
            # probability streams
            p_fund = race["p_fund_win"].to_numpy()
            p_mkt = race["p_mkt_win"].to_numpy()
            inv = race["sp_implied"].to_numpy()
            streams = {"fund": p_fund, "mkt": p_mkt, "market": inv}
            # ET / SARR
            names = race["horse_name"].tolist()
            pe = np.array([rp.get((rn, nm), {}).get("p_et", np.nan)
                           for nm in names])
            psa = np.array([rp.get((rn, nm), {}).get("p_sarr", np.nan)
                            for nm in names])
            et_rank = np.array([rp.get((rn, nm), {}).get("et_rank", np.nan)
                                for nm in names], dtype=float)
            sa_rank = np.array([rp.get((rn, nm), {}).get("sarr_rank", np.nan)
                                for nm in names], dtype=float)
            rec["_et_rank"], rec["_sa_rank"] = et_rank, sa_rank
            if not np.isnan(pe).all():
                pe = np.nan_to_num(pe, nan=float(np.nanmin(pe) * 0.5 if not np.isnan(pe).all() else 0.01))
                streams["et"] = pe / pe.sum()
            if not np.isnan(psa).all():
                psa = np.nan_to_num(psa, nan=float(np.nanmin(psa) * 0.5))
                streams["sarr"] = psa / psa.sum()
            rec["_streams"] = streams
            rec["_top3p"] = {
                "fund": race["p_fund_top3"].to_numpy(),
                "mkt": race["p_mkt_top3"].to_numpy(),
            }
            rec["_top2p"] = {
                "fund": race["p_fund_top2"].to_numpy(),
                "mkt": race["p_mkt_top2"].to_numpy(),
            }
            rec["_sp"] = race["win_odds"].to_numpy()
            rec["_divs"] = divs
            rec["_horse_no"] = race["horse_no"].to_numpy() if "horse_no" in race.columns else None
            rec["_draw"] = race["draw"].to_numpy()
            rows.append(rec)
        if (mi + 1) % 5 == 0:
            print(f"[fuse]   {mi+1}/{len(meetings)} meetings predicted")

    result = score_all(rows, lam, w_market=w_market)
    result["lambda"] = lam
    result["w_market"] = w_market
    result["n_meetings"] = len(meetings)
    result["start"], result["end"] = str(start), str(end)
    if out_json:
        out_json.write_text(json.dumps(result, indent=2, default=str),
                            encoding="utf-8")
        print(f"[fuse] wrote {out_json}")
    return result


def _eval_stream(rows: list[dict], key_or_probs, lam: float,
                 weights: dict | None = None, quinella: bool = True) -> dict:
    """Standard metric block for one probability stream / blend."""
    win1 = pl1 = t3w = qtop = qbox3 = qbank3 = n = 0
    pair_n = 0
    qin_stake = qin_ret = 0.0
    qpl_stake = qpl_ret = 0.0
    by_seg: dict[str, list] = {}
    for rec in rows:
        if isinstance(key_or_probs, str):
            p = rec["_streams"].get(key_or_probs)
        else:
            p = key_or_probs(rec)
        if p is None or np.isnan(p).any():
            continue
        place = rec["_place"]
        order = np.argsort(-p)
        n += 1
        top1 = order[0]
        win1 += int(place[top1] == 1)
        pl1 += int(place[top1] <= 3)
        t3w += int((place[order[:3]] == 1).any())
        # quinella metrics
        t2 = set(np.where(place <= 2)[0])
        if quinella and len(t2) == 2:
            pair_n += 1
            pp = pair_probs(p, lam)
            best_pair = max(pp, key=pp.get)
            qtop += int(set(best_pair) == t2)
            box3 = set(order[:3])
            qbox3 += int(t2 <= box3)
            bank = order[0]
            legs = set(order[1:4])
            qbank3 += int(bank in t2 and (t2 - {bank}) <= legs)
            # ROI with real dividends (box3 = 3 pairs $1 each; banker3 = 3 pairs)
            divs = rec["_divs"]
            hn = rec["_horse_no"]
            if divs and hn is not None:
                rn = rec["race"]
                combos_box = [frozenset({int(hn[a]), int(hn[b])})
                              for a, b in
                              [(order[0], order[1]), (order[0], order[2]),
                               (order[1], order[2])]]
                for c in combos_box:
                    qin_stake += 1.0
                    qin_ret += divs.get((rn, "QIN", c), 0.0)
                combos_bank = [frozenset({int(hn[bank]), int(hn[l])})
                               for l in order[1:4]]
                for c in combos_bank:
                    qpl_stake += 1.0
                    qpl_ret += divs.get((rn, "QPL", c), 0.0)
        seg = f"{rec['venue']}-{rec['surface']}"
        by_seg.setdefault(seg, []).append(
            (int(place[top1] == 1), int(place[top1] <= 3), int((place[order[:3]] == 1).any())))
    if n == 0:
        return {}
    out = {
        "races": n,
        "top1_win": round(win1 / n, 4),
        "top1_place": round(pl1 / n, 4),
        "top3_has_winner": round(t3w / n, 4),
        "q_top_pair": round(qtop / max(pair_n, 1), 4),
        "q_box3": round(qbox3 / max(pair_n, 1), 4),
        "q_banker3": round(qbank3 / max(pair_n, 1), 4),
        "qin_box3_roi": round((qin_ret - qin_stake) / qin_stake, 4) if qin_stake else None,
        "qpl_banker3_roi": round((qpl_ret - qpl_stake) / qpl_stake, 4) if qpl_stake else None,
        "segments": {k: {
            "races": len(v),
            "top1_win": round(np.mean([x[0] for x in v]), 3),
            "top1_place": round(np.mean([x[1] for x in v]), 3),
            "t3w": round(np.mean([x[2] for x in v]), 3),
        } for k, v in sorted(by_seg.items())},
    }
    return out


def score_all(rows: list[dict], lam: float, w_market: float = 1.0) -> dict:
    res = {"n_races": len(rows), "streams": {}}
    for key in ("fund", "mkt", "market", "et", "sarr"):
        m = _eval_stream(rows, key, lam)
        if m:
            res["streams"][key] = m

    # mutual baseline: best ET∩SARR top-3 agreed horse (user's current method)
    def mutual_pick(rec):
        er, sr = rec.get("_et_rank"), rec.get("_sa_rank")
        if er is None or sr is None or np.isnan(er).all() or np.isnan(sr).all():
            return None
        agreed = np.where((er <= 3) & (sr <= 3))[0]
        if len(agreed) == 0:
            return None
        combo = er + sr
        scores = np.full(len(er), 1e-6)
        for i in agreed:
            scores[i] = 1.0 / combo[i]
        return scores / scores.sum()
    m = _eval_stream(rows, mutual_pick, lam, quinella=False)
    if m:
        res["streams"]["mutual_et_sarr"] = m

    # blends (log pools)
    def blend(keys_weights):
        def fn(rec):
            ps, ws = [], []
            for k, w in keys_weights:
                p = rec["_streams"].get(k)
                if p is None or (isinstance(p, np.ndarray) and np.isnan(p).any()):
                    return None
                ps.append(p)
                ws.append(w)
            return log_pool(ps, ws)
        return fn

    blends = {
        "FUSE": [("mkt", 1.0), ("market", w_market)],
        "fund+market": [("fund", 1.0), ("market", 1.0)],
        "fund+market1.5": [("fund", 1.0), ("market", 1.5)],
        "FUSE+sarr": [("mkt", 1.0), ("market", w_market), ("sarr", 0.5)],
        "mkt+sarr": [("mkt", 1.0), ("sarr", 1.0)],
        "mkt+sarr+et": [("mkt", 1.0), ("sarr", 1.0), ("et", 0.5)],
        "fund+market+sarr": [("fund", 1.0), ("market", 1.0), ("sarr", 1.0)],
        "fund+market+sarr+et": [("fund", 1.0), ("market", 1.0),
                                ("sarr", 1.0), ("et", 0.5)],
        "all_equal": [("fund", 1.0), ("market", 1.0), ("sarr", 1.0), ("et", 1.0)],
    }
    for name, kw in blends.items():
        m = _eval_stream(rows, blend(kw), lam)
        if m:
            res["streams"][name] = m

    # ---- dedicated pair-head strategies (quinella focus) ------------------
    def _pair_eval(get_scores, get_banker=None):
        n = qtop = qbox3 = qbank = 0
        qin_stake = qin_ret = 0.0
        qpl_stake = qpl_ret = 0.0
        for rec in rows:
            s = get_scores(rec)
            if s is None or np.isnan(s).any():
                continue
            place = rec["_place"]
            t2 = set(np.where(place <= 2)[0])
            if len(t2) != 2:
                continue
            n += 1
            order = np.argsort(-s)
            qtop += int(set(order[:2]) == t2)
            qbox3 += int(t2 <= set(order[:3]))
            bank = order[0] if get_banker is None else get_banker(rec)
            legs = [i for i in order if i != bank][:3]
            qbank += int(bank in t2 and (t2 - {bank}) <= set(legs))
            divs, hn, rn = rec["_divs"], rec["_horse_no"], rec["race"]
            if divs and hn is not None:
                for a, b in [(order[0], order[1]), (order[0], order[2]),
                             (order[1], order[2])]:
                    qin_stake += 1.0
                    qin_ret += divs.get(
                        (rn, "QIN", frozenset({int(hn[a]), int(hn[b])})), 0.0)
                for l in legs:
                    qpl_stake += 1.0
                    qpl_ret += divs.get(
                        (rn, "QPL", frozenset({int(hn[bank]), int(hn[l])})), 0.0)
        if n == 0:
            return None
        return {
            "races": n,
            "q_top_pair": round(qtop / n, 4),
            "q_box3": round(qbox3 / n, 4),
            "q_banker3": round(qbank / n, 4),
            "qin_box3_roi": round((qin_ret - qin_stake) / qin_stake, 4) if qin_stake else None,
            "qpl_banker3_roi": round((qpl_ret - qpl_stake) / qpl_stake, 4) if qpl_stake else None,
        }

    def _market_top2_marg(rec):
        inv = rec["_streams"]["market"]
        pp = pair_probs(inv, lam)
        marg = np.zeros(len(inv))
        for (i, j), v in pp.items():
            marg[i] += v
            marg[j] += v
        return marg

    pair_strats = {
        "pairs:mkt_top2head": lambda rec: rec["_top2p"]["mkt"],
        "pairs:fund_top2head": lambda rec: rec["_top2p"]["fund"],
        "pairs:market_marg": _market_top2_marg,
        "pairs:top2head_x_market": lambda rec: np.sqrt(
            np.clip(rec["_top2p"]["mkt"], 1e-6, None)
            * np.clip(_market_top2_marg(rec), 1e-6, None)),
    }
    for name, fn in pair_strats.items():
        try:
            m = _pair_eval(fn)
            if m:
                res["streams"][name] = m
        except Exception:
            pass

    # ---- edge-filtered betting strategies (settled on real dividends) -----
    def fuse_probs(rec):
        ps = [rec["_streams"]["mkt"], rec["_streams"]["market"]]
        if any(p is None or np.isnan(p).any() for p in ps):
            return None
        return log_pool(ps, [1.0, w_market])

    strat = {}
    # WIN: bet stream's top-1 only when its own prob beats the market price
    for src in ("fund", "mkt"):
        for ethr in (0.00, 0.02, 0.04):
            bets = wins = 0
            stake = ret = 0.0
            for rec in rows:
                p = rec["_streams"].get(src)
                inv = rec["_streams"].get("market")
                sp = rec.get("_sp")
                if p is None or inv is None or np.isnan(p).any() or np.isnan(inv).any():
                    continue
                pf = fuse_probs(rec)
                top = int(np.argmax(pf if pf is not None else p))
                if p[top] - inv[top] < ethr:
                    continue
                bets += 1
                stake += 1.0
                if rec["_place"][top] == 1 and sp is not None and not np.isnan(sp[top]):
                    wins += 1
                    ret += float(sp[top])
            if bets:
                strat[f"WIN_top1_PE_{src}{int(ethr*100)}"] = {
                    "bets": bets, "strike": round(wins / bets, 4),
                    "roi": round((ret - stake) / stake, 4)}

    # QIN: pairs among FUSE top-4 with model-vs-market pair edge
    for src in ("mkt",):
        for ethr in (1.00, 1.15, 1.30, 1.50):
            bets = hits = 0
            stake = ret = 0.0
            for rec in rows:
                p = rec["_streams"].get(src)
                inv = rec["_streams"].get("market")
                if (p is None or inv is None or np.isnan(p).any()
                        or np.isnan(inv).any() or not rec["_divs"]
                        or rec["_horse_no"] is None):
                    continue
                pf = fuse_probs(rec)
                pp_m = pair_probs(p, lam)
                pp_mkt = pair_probs(inv, lam)
                hn, rn = rec["_horse_no"], rec["race"]
                order = np.argsort(-(pf if pf is not None else p))
                cand = {(min(i, j), max(i, j))
                        for i in order[:4] for j in order[:4] if i != j}
                for (i, j) in cand:
                    pm = pp_m.get((i, j), 0.0)
                    pq = pp_mkt.get((i, j), 1e-9)
                    approx_div = 0.825 / max(pq, 1e-9)
                    if pm * approx_div < ethr:
                        continue
                    bets += 1
                    stake += 1.0
                    d = rec["_divs"].get(
                        (rn, "QIN", frozenset({int(hn[i]), int(hn[j])})), 0.0)
                    if d > 0:
                        hits += 1
                    ret += d
            if bets:
                strat[f"QIN_PE_{src}{int(ethr*100)}"] = {
                    "bets": bets, "strike": round(hits / bets, 4),
                    "roi": round((ret - stake) / stake, 4)}

    # QPL: banker (FUSE top-1) × legs = model top-4 pairs with QPL edge
    for ethr in (1.00, 1.20):
        bets = hits = 0
        stake = ret = 0.0
        for rec in rows:
            p = rec["_streams"].get("mkt")
            inv = rec["_streams"].get("market")
            if (p is None or inv is None or np.isnan(p).any()
                    or np.isnan(inv).any() or not rec["_divs"]
                    or rec["_horse_no"] is None):
                continue
            pf = fuse_probs(rec)
            t3 = rec["_top3p"]["mkt"]
            hn, rn = rec["_horse_no"], rec["race"]
            order = np.argsort(-(pf if pf is not None else p))
            bank = order[0]
            for l in order[1:4]:
                # crude joint top-3 prob for the pair
                pm = float(t3[bank] * t3[l]) * 0.85
                inv3 = np.clip(inv * 2.6, None, 0.95)
                pq = float(inv3[bank] * inv3[l]) * 0.85
                approx_div = 0.825 / max(pq, 1e-9)
                if pm * approx_div < ethr:
                    continue
                bets += 1
                stake += 1.0
                d = rec["_divs"].get(
                    (rn, "QPL", frozenset({int(hn[bank]), int(hn[l])})), 0.0)
                if d > 0:
                    hits += 1
                ret += d
        if bets:
            strat[f"QPL_PE_{int(ethr*100)}"] = {
                "bets": bets, "strike": round(hits / bets, 4),
                "roi": round((ret - stake) / stake, 4)}
    res["strategies"] = strat
    return res


# ---------------------------------------------------------------------------
# Train production artifact
# ---------------------------------------------------------------------------
def train_production(out_path: Path) -> None:
    hist = load_history()
    feat = add_features(hist)
    cutoff = feat["race_date"].max() + pd.Timedelta(days=1)
    models = train_models(feat, cutoff)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        pickle.dump({"models": models, "fund_features": FUND_FEATURES,
                     "mkt_features": MKT_FEATURES}, f)
    print(f"[fuse] production model trained to {models['cutoff']} "
          f"({models['n_train']} rows) → {out_path}")


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    bt = sub.add_parser("backtest")
    bt.add_argument("--start", default="2026-04-01")
    bt.add_argument("--end", default="2026-06-10")
    bt.add_argument("--refit-every", type=int, default=1)
    bt.add_argument("--out", default=str(REPORTS / "fuse_backtest.json"))
    tr = sub.add_parser("train")
    tr.add_argument("--out", default=str(MODELS / "fuse_v1.pkl"))
    args = ap.parse_args()
    if args.cmd == "backtest":
        res = run_backtest(args.start, args.end, refit_every=args.refit_every,
                           out_json=Path(args.out))
        print(json.dumps({k: v for k, v in res["streams"].items()},
                         indent=1, default=str)[:4000])
    elif args.cmd == "train":
        train_production(Path(args.out))


if __name__ == "__main__":
    main()
