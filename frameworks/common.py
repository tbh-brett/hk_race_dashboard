"""
Shared data layer for the four candidate frameworks.

Responsibilities:
  - Load `results` table from hkjc.db.
  - Clean / coerce all feature columns identified in EDA.
  - Parse string-encoded sectional fields (sectiontimes, running_positions, lbws).
  - Provide a strict point-in-time view: at any cutoff date, give back rows whose
    race_date <  cutoff for training, and rows whose race_date == cutoff for scoring.
  - Provide normalised market probability per race (overround-adjusted).
  - Provide canonical race / runner keys.

Keep this module side-effect free: framework modules import from it only.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "hkjc.db"

NON_FINISHER_CODES = {
    "WV", "WV-A", "UR", "PU", "DNF", "FE", "WX", "WX-A",
    "WXNR", "TNP", "DISQ", "---", "None", "",
}
VALID_SEX = {"Gelding", "Horse", "Colt", "Mare", "Filly"}

RACE_KEYS = ["race_date", "race_number"]
RUNNER_KEY = ["race_date", "race_number", "horse_id"]


def _parse_finish_pos(v) -> float:
    if pd.isna(v):
        return np.nan
    s = str(v).strip()
    if s in NON_FINISHER_CODES:
        return np.nan
    s = s.split()[0]  # "8 DH" -> "8"
    try:
        return float(int(s))
    except Exception:
        return np.nan


def _parse_float_list_semi(s):
    """'24.22; 22.27; 23.13; ; ; ' -> [24.22, 22.27, 23.13]"""
    if pd.isna(s):
        return []
    out = []
    for t in str(s).split(";"):
        t = t.strip()
        if not t:
            continue
        try:
            out.append(float(t))
        except Exception:
            pass
    return out


def _parse_int_list_space(s):
    """'4 4 8' -> [4, 4, 8]"""
    if pd.isna(s):
        return []
    out = []
    for t in str(s).split():
        try:
            out.append(int(t))
        except Exception:
            pass
    return out


# Beaten-lengths parser. HKJC uses tokens like '3-1/4', '1/2', 'SH', 'N', 'NK'.
_LBW_NAMED = {
    "SH": 0.05,   # short head
    "HD": 0.10,   # head
    "SHD": 0.05,
    "HEAD": 0.10,
    "N": 0.20,    # neck
    "NK": 0.20,
    "NS": 0.05,   # nose
    "NSE": 0.05,
    "SN": 0.05,
    "DH": 0.0,    # dead heat (used as join token, ignored here)
    "-": 0.0,     # placeholder for winner
    "---": np.nan,
}


def parse_lbw(token: str) -> float:
    if token is None:
        return np.nan
    s = str(token).strip().upper()
    if s == "" or s.lower() == "nan":
        return np.nan
    if s in _LBW_NAMED:
        return _LBW_NAMED[s]
    # patterns like "3-1/4" or "1/2" or "12"
    if "-" in s and "/" in s:
        whole, frac = s.split("-", 1)
        try:
            num, den = frac.split("/")
            return float(whole) + float(num) / float(den)
        except Exception:
            return np.nan
    if "/" in s:
        try:
            num, den = s.split("/")
            return float(num) / float(den)
        except Exception:
            return np.nan
    try:
        return float(s)
    except Exception:
        return np.nan


def _parse_lbws_string(s):
    """'2-3/4; 2-3/4; 5-3/4; ; ;' -> [0.75-ish, ..., 5.75]"""
    if pd.isna(s):
        return []
    out = []
    for t in str(s).split(";"):
        t = t.strip()
        if not t:
            continue
        v = parse_lbw(t)
        if not pd.isna(v):
            out.append(v)
    return out


@dataclass
class Dataset:
    df: pd.DataFrame   # cleaned, indexed by integer; one row per runner-in-race

    def runners_before(self, cutoff: pd.Timestamp) -> pd.DataFrame:
        return self.df[self.df["race_date_dt"] < cutoff]

    def runners_on(self, cutoff: pd.Timestamp) -> pd.DataFrame:
        return self.df[self.df["race_date_dt"] == cutoff]

    def runners_between(self, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        # inclusive both ends
        m = (self.df["race_date_dt"] >= start) & (self.df["race_date_dt"] <= end)
        return self.df[m]

    def race_dates(self) -> list:
        return sorted(self.df["race_date_dt"].dropna().unique())


def load_dataset(db_path: Path | str = DB_PATH) -> Dataset:
    con = sqlite3.connect(str(db_path))
    df = pd.read_sql_query("SELECT * FROM results", con)
    con.close()

    # ---- numeric coercions
    for c in ("win_odds", "declared_weight", "rating", "current_rating",
              "start_of_season_rating", "last_rating",
              "finish_time_seconds", "distance", "actual_weight", "age"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["draw_num"] = pd.to_numeric(
        df["draw"].astype(str).str.split(".").str[0].replace({"---": np.nan}),
        errors="coerce",
    )
    df["horse_number"] = pd.to_numeric(df["horse_number"], errors="coerce")

    # ---- finishing outcome
    df["finish_pos"] = df["place"].map(_parse_finish_pos)
    df["won"] = (df["finish_pos"] == 1).astype(int)
    df["placed"] = (df["finish_pos"] <= 3).astype(int)
    df["completed"] = df["finish_pos"].notna()

    # ---- date
    df["race_date_dt"] = pd.to_datetime(df["race_date"], errors="coerce")

    # ---- sex sanitisation (column-shift bug)
    df.loc[~df["sex"].isin(VALID_SEX), "sex"] = np.nan

    # ---- field size + market prob
    field = df.groupby(RACE_KEYS).size().rename("field_size")
    df = df.merge(field, left_on=RACE_KEYS, right_index=True)

    df["inv_odds"] = np.where(df["win_odds"].gt(0), 1.0 / df["win_odds"], np.nan)
    ovr = df.groupby(RACE_KEYS)["inv_odds"].transform("sum")
    df["mkt_prob"] = df["inv_odds"] / ovr
    df["fav_rank"] = df.groupby(RACE_KEYS)["win_odds"].rank(method="min")

    # ---- parsed sectional features
    df["sec_list"] = df["sectiontimes"].map(_parse_float_list_semi)
    df["pos_list"] = df["running_positions"].map(_parse_int_list_space)
    df["lbw_list"] = df["lbws"].map(_parse_lbws_string)
    df["n_sec"] = df["sec_list"].map(len)

    # early/mid/late position with safe fallbacks
    df["early_pos"] = df["pos_list"].map(lambda L: L[0] if L else np.nan)
    df["late_pos"] = df["pos_list"].map(lambda L: L[-2] if len(L) >= 2 else (L[-1] if L else np.nan))

    # final beaten lengths (winner is "-" -> 0.0)
    df["beaten_lengths"] = df["lbw"].map(parse_lbw).fillna(0.0)

    # gear flags
    g = df["gear"].astype("string").fillna("")
    df["gear_first_time"] = g.str.contains("1", regex=False).astype(int)
    df["gear_second_off"] = g.str.contains("2", regex=False).astype(int)

    # rating delta within season
    df["rating_delta_season"] = df["rating"] - df["start_of_season_rating"]

    # sort by date for any rolling lookups downstream
    df = df.sort_values(["race_date_dt", "race_number", "horse_number"]).reset_index(drop=True)
    return Dataset(df=df)


# -----------------------------------------------------------------------------
# Evaluation helpers
# -----------------------------------------------------------------------------

def normalize_per_race(scores: pd.Series, race_id: pd.Series) -> pd.Series:
    """Softmax-normalise per race so probabilities sum to 1."""
    s = scores.copy()
    # protect against all-zero or NaN groups
    df = pd.DataFrame({"s": s, "r": race_id})
    out = df.groupby("r")["s"].transform(lambda x: x / x.sum() if x.sum() > 0 else x)
    return out


def softmax_per_race(scores: pd.Series, race_id: pd.Series, temperature: float = 1.0) -> pd.Series:
    df = pd.DataFrame({"s": scores.astype(float), "r": race_id})
    def _sm(x):
        x = x / temperature
        x = x - x.max()
        e = np.exp(x)
        return e / e.sum()
    return df.groupby("r")["s"].transform(_sm)


def log_loss_win(p: np.ndarray, y: np.ndarray, eps: float = 1e-9) -> float:
    p = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def race_log_loss(p: np.ndarray, y: np.ndarray, race_id: np.ndarray, eps: float = 1e-9) -> float:
    """Multinomial log-loss: per race, use the probability assigned to the winner."""
    df = pd.DataFrame({"p": np.clip(p, eps, 1.0), "y": y, "r": race_id})
    # one winner per race
    win_p = df[df["y"] == 1].groupby("r")["p"].first()
    if win_p.empty:
        return float("nan")
    return float(-np.log(win_p).mean())


def brier_win(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def topk_hit_rate(p: pd.Series, y: pd.Series, race_id: pd.Series, k: int = 1) -> float:
    """Fraction of races where the winner is within the top-k by predicted prob."""
    df = pd.DataFrame({"p": p, "y": y, "r": race_id})
    df["rank"] = df.groupby("r")["p"].rank(method="first", ascending=False)
    hit = df.groupby("r").apply(lambda g: int(((g["rank"] <= k) & (g["y"] == 1)).any()),
                                 include_groups=False)
    return float(hit.mean())


def evaluate(name: str, p: np.ndarray, y: np.ndarray, race_id: np.ndarray,
             mkt_p: np.ndarray | None = None) -> dict:
    out = {
        "framework": name,
        "n_runners": int(len(y)),
        "n_races": int(pd.Series(race_id).nunique()),
        "log_loss_runner": log_loss_win(p, y),
        "log_loss_race": race_log_loss(p, y, race_id),
        "brier": brier_win(p, y),
        "top1": topk_hit_rate(pd.Series(p), pd.Series(y), pd.Series(race_id), k=1),
        "top3": topk_hit_rate(pd.Series(p), pd.Series(y), pd.Series(race_id), k=3),
    }
    if mkt_p is not None:
        out["log_loss_runner_mkt"] = log_loss_win(mkt_p, y)
        out["log_loss_race_mkt"] = race_log_loss(mkt_p, y, race_id)
        out["delta_log_loss_runner"] = out["log_loss_runner"] - out["log_loss_runner_mkt"]
        out["delta_log_loss_race"] = out["log_loss_race"] - out["log_loss_race_mkt"]
    return out


# -----------------------------------------------------------------------------
# Standard time-split (configurable)
# -----------------------------------------------------------------------------

@dataclass
class TimeSplit:
    train_end: pd.Timestamp     # exclusive
    val_end: pd.Timestamp       # exclusive
    test_end: pd.Timestamp      # inclusive

    @classmethod
    def default(cls) -> "TimeSplit":
        return cls(
            train_end=pd.Timestamp("2026-01-01"),
            val_end=pd.Timestamp("2026-04-01"),
            test_end=pd.Timestamp("2026-12-31"),
        )

    def split(self, df: pd.DataFrame):
        d = df["race_date_dt"]
        train = df[d < self.train_end]
        val = df[(d >= self.train_end) & (d < self.val_end)]
        test = df[(d >= self.val_end) & (d <= self.test_end)]
        return train, val, test
