"""
Framework C — Form-quality (margin / time based) ranking.

Core insight (from EDA): finishing position is coarse and field-size-dependent.
The data carries richer per-horse signals: `finish_time_seconds`, sectional
splits, beaten lengths, and conditions (going, weight, class). A horse
beaten 0.3L in a strong race is qualitatively different from one beaten 0.3L
in a weak race.

Strategy:
  1. For every (race_track, distance, going) cell, compute a "par time" =
     median winner finish time using ONLY history strictly before the cutoff.
     This is the day-quality / track-pace baseline.
  2. For every prior runner, compute its per-start `time_figure`:
        time_fig = par_time - finish_time_seconds + lbw_seconds_adjust
                    + class_adj + weight_adj
     where:
        lbw_seconds_adjust : convert beaten lengths to seconds at the
            sectional pace (1 length ~ 0.17s at racing pace);
        class_adj : +0.5 sec per class step DOWN from class 4 (i.e. higher
            class racing is implicitly faster);
        weight_adj : +0.05 sec per pound carried above 125 lb (the median).
     Higher = better trip.
  3. For each horse, the "form rating" is the EWMA of its last <=5 time_figs
     (more weight on recent).
  4. To predict the upcoming race, score each runner with its form rating
     (NaN for first-time-starters / runners with no prior). Apply a small
     distance-mismatch penalty: prior trips with very different distance
     get exponentially down-weighted.
  5. Softmax-normalise per race.

This framework deliberately does NOT use the market price OR connections.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pandas as pd

from .common import softmax_per_race


# Convert beaten lengths to seconds at racing pace.
# At ~16 m/s gallop, 1 length (~2.4 m) = 0.15 s. We use 0.17 to be a touch
# more generous to closers who lose ground in the straight.
_LBW_TO_SEC = 0.17


@dataclass
class FrameworkC:
    par_min_n: int = 5         # need at least this many history rows per cell
    ewma_halflife: float = 2.5  # in starts
    distance_decay: float = 250.0  # metres of mismatch where weight halves
    temperature: float = 0.5

    # ---- pars: median winner time per (track, distance, going)
    def _build_par_table(self, history: pd.DataFrame) -> pd.DataFrame:
        h = history[(history["finish_pos"] == 1) & history["finish_time_seconds"].notna()]
        agg = h.groupby(["race_track", "distance", "going"])["finish_time_seconds"].agg(["median", "count"])
        # Fallback layer: median by (track, distance) ignoring going for cells with too few wins.
        agg2 = h.groupby(["race_track", "distance"])["finish_time_seconds"].agg(["median", "count"])
        return {"by_tdg": agg, "by_td": agg2}

    def _par_for(self, par_table: dict, track: str, distance: float, going: str) -> float:
        try:
            row = par_table["by_tdg"].loc[(track, distance, going)]
            if row["count"] >= self.par_min_n:
                return float(row["median"])
        except (KeyError, TypeError):
            pass
        try:
            row2 = par_table["by_td"].loc[(track, distance)]
            return float(row2["median"])
        except (KeyError, TypeError):
            return np.nan

    def _per_run_time_figs(self, history: pd.DataFrame, par_table: dict) -> pd.Series:
        h = history[history["completed"]
                    & history["finish_time_seconds"].notna()
                    & history["distance"].notna()].copy()

        pars = h.apply(lambda r: self._par_for(par_table, r["race_track"],
                                                r["distance"], r["going"]), axis=1)
        # adjust the runner's time *up* by their beaten-length deficit
        # (so a horse beaten 5L is treated as having a slower time than the winner)
        eff_time = h["finish_time_seconds"] + h["beaten_lengths"] * _LBW_TO_SEC

        # For winners, beaten_lengths is 0 -> eff_time == finish_time_seconds. Good.
        # For non-winners, finish_time_seconds in this DB *is* the horse's own time
        # (HKJC publishes per-horse times), so adding lbw is double-counting.
        # We treat finish_time_seconds AS the per-horse time and use beaten_lengths
        # purely to break ties when finish_time is missing (already filtered out).
        eff_time = h["finish_time_seconds"]

        # class adjustment: class 4 baseline; lower-numbered class = higher quality
        cls = pd.to_numeric(h["race_class"], errors="coerce").fillna(4)
        class_adj = (4 - cls) * 0.5  # seconds. Class 1 race is 1.5s "faster" than class 4.

        # weight adjustment: heavier weight = bigger handicap = expect slower
        wt = h["actual_weight"].fillna(125)
        weight_adj = (wt - 125) * 0.05  # +0.05 s per extra lb

        # time figure: positive is good (we beat par after weight/class adjustment)
        time_fig = pars - eff_time + class_adj + weight_adj
        return pd.Series(time_fig.values, index=h.index, name="time_fig")

    def _ewma_form(self, history: pd.DataFrame, time_fig: pd.Series) -> pd.DataFrame:
        h = history.loc[time_fig.index].copy()
        h["time_fig"] = time_fig
        h = h.sort_values(["horse_id", "race_date_dt"])

        def _agg(g: pd.DataFrame):
            g = g.tail(8)  # cap history depth
            wt = 0.5 ** (np.arange(len(g))[::-1] / self.ewma_halflife)
            wt /= wt.sum()
            return pd.Series({
                "form": float((g["time_fig"].values * wt).sum()),
                "n_form": int(g["time_fig"].notna().sum()),
                "last_distance": float(g["distance"].iloc[-1]),
            })

        return h.groupby("horse_id").apply(_agg, include_groups=False)

    def fit(self, training: pd.DataFrame, history: pd.DataFrame) -> "FrameworkC":
        # rules-based: nothing to learn. Provided for API parity.
        return self

    def predict_proba(self, runners: pd.DataFrame, history: pd.DataFrame) -> pd.Series:
        h = history[history["completed"]]
        par_table = self._build_par_table(h)
        time_fig = self._per_run_time_figs(h, par_table)
        form = self._ewma_form(h, time_fig)
        # form may have NaN time_fig if pars unavailable; default to 0 (par-level).
        form["form"] = form["form"].fillna(0.0)

        # par for THIS race (used to normalise the meaning of "form" across cells)
        scored = runners.copy()
        scored["form"] = scored["horse_id"].map(form["form"])
        scored["last_distance"] = scored["horse_id"].map(form["last_distance"])
        scored["n_form"] = scored["horse_id"].map(form["n_form"]).fillna(0)

        # distance mismatch penalty (in standard deviations of seconds)
        dist_diff = (scored["distance"] - scored["last_distance"]).abs()
        dist_weight = np.exp(-(dist_diff.fillna(0.0)) / self.distance_decay)

        # baseline for first-time starters: small negative (~unknown is slightly worse than mean)
        ftime = scored["form"].fillna(-0.5) * dist_weight + (
            scored["form"].isna().astype(float) * (-0.3)
        )

        # confidence shrinkage: horses with fewer prior runs get pulled to 0
        conf = (scored["n_form"] / (scored["n_form"] + 3.0))
        score = ftime * conf

        race_id = (scored["race_date"].astype(str) + "_" +
                   scored["race_number"].astype(str))
        return softmax_per_race(pd.Series(score.values, index=scored.index),
                                race_id, temperature=self.temperature)
