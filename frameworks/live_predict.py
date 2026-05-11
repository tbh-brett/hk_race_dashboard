"""
Live prediction wrapper around Frameworks A/B/C/D.

Reads a scraped pre-race racecard JSON (``cache/racecard_YYYY-MM-DD.json``),
optionally enriches it with the latest live-odds snapshot per race
(``cache/live_odds/YYYYMMDD/{HV|ST}_R{NN}*.json``), converts it into a
``runners`` DataFrame in the same schema the frameworks consume during
walk-forward, and returns per-race normalised win probabilities for each
framework plus a simple equal-weight ensemble.

This module is intentionally side-effect-free so the Streamlit dashboard can
import and call it without triggering a refit on every interaction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from .common import load_dataset, Dataset, RACE_KEYS
from .framework_a_residual import FrameworkA
from .framework_b_pace import FrameworkB
from .framework_c_form import FrameworkC
from .framework_d_connections import FrameworkD

BASE = Path(__file__).resolve().parent.parent
CACHE = BASE / "cache"


# ── racecard / live-odds loaders ────────────────────────────────────────────

def load_racecard(date_iso: str) -> dict | None:
    p = CACHE / f"racecard_{date_iso}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_latest_live_odds(date_iso: str, venue: str) -> dict[int, dict[int, float]]:
    """
    Return {race_no -> {horse_no -> win_odds}} using the *latest* snapshot
    file per race in ``cache/live_odds/YYYYMMDD/``.
    """
    date_compact = date_iso.replace("-", "")
    d = CACHE / "live_odds" / date_compact
    if not d.exists():
        return {}
    by_race: dict[int, list[Path]] = {}
    for fp in d.glob(f"{venue}_R*.json"):
        try:
            # filename pattern HV_R07_101823.json -> race 7
            rn = int(fp.stem.split("_")[1].lstrip("R"))
        except (ValueError, IndexError):
            continue
        by_race.setdefault(rn, []).append(fp)
    out: dict[int, dict[int, float]] = {}
    for rn, files in by_race.items():
        latest = max(files, key=lambda p: p.stat().st_mtime)
        try:
            j = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        m: dict[int, float] = {}
        for row in j.get("odds") or []:
            try:
                no = int(row.get("no"))
                w = float(row.get("win"))
                if w > 0:
                    m[no] = w
            except (TypeError, ValueError):
                continue
        if m:
            out[rn] = m
    return out


# ── racecard -> runners DataFrame ───────────────────────────────────────────

_TRACK_FROM_VENUE = {"HV": "HV", "ST": "ST", "Happy Valley": "HV", "Sha Tin": "ST"}


def racecard_to_runners(racecard: dict,
                        live_odds: dict[int, dict[int, float]] | None,
                        history: pd.DataFrame) -> pd.DataFrame:
    """Convert one racecard JSON dict into a runners DataFrame with the
    columns the frameworks need.
    """
    if not racecard:
        return pd.DataFrame()
    venue = (racecard.get("racecourse") or "").strip()
    race_track = _TRACK_FROM_VENUE.get(venue, venue if venue in {"HV", "ST"} else "ST")
    race_date = racecard.get("race_date") or ""
    race_date_dt = pd.to_datetime(race_date, errors="coerce")

    # season start-of-rating per horse, taken from the most recent prior row
    sos = (history.sort_values("race_date_dt")
                  .dropna(subset=["horse_id"])
                  .groupby("horse_id")["start_of_season_rating"].last())

    rows: list[dict] = []
    for race in racecard.get("races", []):
        meta = race.get("meta", {}) or {}
        rn = int(meta.get("race_number") or meta.get("race_no") or 0)
        dist = meta.get("distance")
        rc = meta.get("race_course") or ""
        going = (meta.get("going") or "").strip()
        # try to coerce race_class to an integer (1..5); group/listed -> 1
        rcls_raw = str(meta.get("race_class") or "").strip()
        rcls = _coerce_class(rcls_raw)

        horses = [h for h in race.get("horses", []) if not h.get("is_standby")]
        field_size = len(horses)
        live_for_race = (live_odds or {}).get(rn, {})

        for h in horses:
            try:
                horse_no = int(h.get("horse_no")) if h.get("horse_no") is not None else None
            except (TypeError, ValueError):
                horse_no = None
            try:
                draw = float(h.get("draw")) if h.get("draw") not in (None, "", "-") else np.nan
            except (TypeError, ValueError):
                draw = np.nan
            try:
                rating = float(h.get("rating")) if h.get("rating") not in (None, "", "-") else np.nan
            except (TypeError, ValueError):
                rating = np.nan

            hid = h.get("horse_id") or ""
            sos_r = sos.get(hid, np.nan)
            rating_delta = (rating - sos_r) if pd.notna(rating) and pd.notna(sos_r) else 0.0

            gear = str(h.get("gear") or "")
            win_odds = live_for_race.get(horse_no) if horse_no is not None else None

            rows.append({
                "race_date": race_date,
                "race_date_dt": race_date_dt,
                "race_number": rn,
                "race_track": race_track,
                "race_course": rc,
                "going": going,
                "distance": dist,
                "race_class": rcls,
                "horse_id": hid,
                "horse_name": h.get("horse_name", ""),
                "horse_number": horse_no,
                "jockey": h.get("jockey", ""),
                "trainer": h.get("trainer", ""),
                "draw_num": draw,
                "actual_weight": _to_float(h.get("weight")),
                "declared_weight": _to_float(h.get("horse_wt_declaration")),
                "rating": rating,
                "rating_delta_season": rating_delta,
                "gear_first_time": int("1" in gear),
                "gear_second_off": int("2" in gear),
                "import_type": h.get("import_cat") or "",
                "field_size": field_size,
                "win_odds": float(win_odds) if win_odds else np.nan,
                "completed": True,   # placeholder so frameworks treat row as scoreable
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # market probability from latest live odds (overround-normalised per race)
    df["inv_odds"] = np.where(df["win_odds"].gt(0), 1.0 / df["win_odds"], np.nan)
    ovr = df.groupby(RACE_KEYS)["inv_odds"].transform("sum")
    df["mkt_prob"] = df["inv_odds"] / ovr
    # races with no odds yet → uniform prior so framework A degrades gracefully
    uniform = 1.0 / df["field_size"].clip(lower=1)
    df["mkt_prob"] = df["mkt_prob"].fillna(uniform)
    return df


def _coerce_class(s: str) -> float:
    s2 = s.lower()
    if "group" in s2 or "grade" in s2 or "listed" in s2:
        return 1.0
    digits = "".join(ch for ch in s2 if ch.isdigit())
    try:
        return float(digits) if digits else 4.0
    except ValueError:
        return 4.0


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return np.nan


# ── master entry point ─────────────────────────────────────────────────────

@dataclass
class FrameworkPredictions:
    runners: pd.DataFrame      # all input cols + p_A, p_B, p_C, p_D, p_ens
    n_races: int
    n_runners: int
    used_live_odds: bool
    fit_window_days: int


def predict_racecard(date_iso: str,
                     fit_window_days: int = 180,
                     dataset: Dataset | None = None,
                     racecard: dict | None = None,
                     use_live_odds: bool = True) -> FrameworkPredictions:
    """
    Run all four frameworks against an upcoming racecard.

    Parameters
    ----------
    date_iso : str
        Meeting date, ``YYYY-MM-DD``.
    fit_window_days : int
        Look-back window (days) used to fit Framework A on completed races.
    dataset : Dataset, optional
        Pre-loaded dataset (skip the ~1 s DB read on Streamlit re-runs).
    racecard : dict, optional
        Pre-loaded racecard JSON (skip the disk read).
    use_live_odds : bool
        If True, merge latest live-odds snapshot for ``mkt_prob``; otherwise
        every race uses a uniform prior (Framework A becomes near-uninformative
        because its market-offset term collapses to log(1/N)).
    """
    if dataset is None:
        dataset = load_dataset()
    if racecard is None:
        racecard = load_racecard(date_iso)
    if not racecard:
        raise FileNotFoundError(f"No racecard cache for {date_iso}; scrape it first.")

    cutoff = pd.to_datetime(date_iso)
    history = dataset.runners_before(cutoff)
    # training rows for Framework A: completed races inside fit window
    train_start = cutoff - pd.Timedelta(days=fit_window_days)
    training = history[(history["race_date_dt"] >= train_start)
                       & history["completed"]
                       & history["mkt_prob"].notna()].copy()

    venue = (racecard.get("racecourse") or "").strip()
    live_odds: dict = {}
    if use_live_odds:
        live_odds = load_latest_live_odds(date_iso, venue)

    runners = racecard_to_runners(racecard, live_odds, history)
    if runners.empty:
        raise ValueError(f"Racecard for {date_iso} produced 0 runners.")

    # ── fit Framework A on history (B/C/D have no-op .fit)
    a = FrameworkA().fit(training, history)
    b = FrameworkB().fit(training, history)
    c = FrameworkC().fit(training, history)
    d = FrameworkD().fit(training, history)

    runners = runners.copy()
    runners["p_A"] = a.predict_proba(runners, history).values
    runners["p_B"] = b.predict_proba(runners, history).values
    runners["p_C"] = c.predict_proba(runners, history).values
    runners["p_D"] = d.predict_proba(runners, history).values
    runners["p_market"] = runners["mkt_prob"]
    # equal-weight ensemble of the four model probabilities
    runners["p_ens"] = runners[["p_A", "p_B", "p_C", "p_D"]].mean(axis=1)
    # renormalise ensemble per race
    grp = runners.groupby(RACE_KEYS)["p_ens"].transform("sum")
    runners["p_ens"] = runners["p_ens"] / grp

    # convenience rank columns
    for col in ("p_market", "p_A", "p_B", "p_C", "p_D", "p_ens"):
        runners[f"rank_{col}"] = (runners.groupby(RACE_KEYS)[col]
                                  .rank(method="min", ascending=False)
                                  .astype(int))

    return FrameworkPredictions(
        runners=runners,
        n_races=runners["race_number"].nunique(),
        n_runners=len(runners),
        used_live_odds=bool(live_odds),
        fit_window_days=fit_window_days,
    )
