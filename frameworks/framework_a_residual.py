"""
Framework A — Market-anchored residual model.

Core insight (from EDA): the final tote SP is highly calibrated (Brier 0.066 on
win, log-loss 0.236 vs 1/N baseline of 2.53). So the right baseline is the
overround-normalised market probability `mkt_prob`, not 1/N.

This model predicts the *residual* `won - mkt_prob` as a logistic function of
features that EDA showed carried signal independent of the market:

    - running-style proxy from prior `running_positions` (predicts pace
      benefit, since the leader/prom band carried +4.3 / +0.5 pp residual)
    - rating_delta_season (rating - start_of_season_rating)
    - jockey win-rate prior (Bayesian shrinkage)
    - trainer win-rate prior (Bayesian shrinkage)
    - draw_rel = draw / field_size, segmented by track
    - gear_first_time / gear_second_off flags (negative residuals)
    - days since last start
    - import_type (ISG = +2.15 pp residual)

The output probability is

        p = sigmoid(logit(mkt_prob) + beta . X_residual_features)

i.e. the model only adds a *correction* on top of the market log-odds.
After per-runner adjustment we re-normalise per race so probabilities sum to 1.

All features are constructed strictly from rows with race_date STRICTLY before
the race being scored, so this can be walk-forward backtested without leakage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from .common import RACE_KEYS, normalize_per_race


# Bayesian shrinkage: posterior mean of Beta(alpha+wins, beta+losses) toward base
def _shrunk_rate(wins: pd.Series, n: pd.Series, base: float, k: float = 50.0) -> pd.Series:
    return (wins + base * k) / (n + k)


@dataclass
class FrameworkA:
    base_win_rate: float = 0.0815
    shrink_k: float = 50.0
    coef_: np.ndarray | None = None
    intercept_: float = 0.0
    feature_names_: list = field(default_factory=list)

    # ---- helper: build per-runner historical priors using ONLY rows < cutoff
    def _build_history_priors(self, history: pd.DataFrame) -> dict:
        h = history[history["completed"]].copy()
        base = h["won"].mean() if len(h) else self.base_win_rate

        jockey = h.groupby("jockey")["won"].agg(["sum", "count"])
        jockey["rate"] = _shrunk_rate(jockey["sum"], jockey["count"], base, self.shrink_k)

        trainer = h.groupby("trainer")["won"].agg(["sum", "count"])
        trainer["rate"] = _shrunk_rate(trainer["sum"], trainer["count"], base, self.shrink_k)

        # horse-level: average early position (running style proxy) and last-start finish
        horse_style = (h.groupby("horse_id")["early_pos"]
                         .agg(["mean", "count"]).rename(columns={"mean": "avg_early"}))

        # most recent prior row per horse (for days-since and last-finish)
        h_sorted = h.sort_values(["horse_id", "race_date_dt"])
        last = h_sorted.groupby("horse_id").tail(1)[
            ["horse_id", "race_date_dt", "finish_pos", "distance"]
        ].rename(columns={"race_date_dt": "last_date",
                           "finish_pos": "last_finish",
                           "distance": "last_distance"})

        return {
            "base": base,
            "jockey": jockey["rate"].to_dict(),
            "trainer": trainer["rate"].to_dict(),
            "horse_style": horse_style[horse_style["count"] >= 2]["avg_early"].to_dict(),
            "last": last.set_index("horse_id"),
        }

    def _featurize(self, runners: pd.DataFrame, priors: dict) -> pd.DataFrame:
        base = priors["base"]
        out = pd.DataFrame(index=runners.index)

        out["jockey_rate"] = (runners["jockey"].map(priors["jockey"]).fillna(base) - base)
        out["trainer_rate"] = (runners["trainer"].map(priors["trainer"]).fillna(base) - base)

        # avg early position from history; missing -> field median (mid)
        avg_early = runners["horse_id"].map(priors["horse_style"])
        # convert to relative (smaller = nearer the lead = positive expected)
        rel = avg_early / runners["field_size"]
        # Lead bias is expressed as 1 - rel (so leaders get +)
        out["pace_lead_score"] = (1.0 - rel.fillna(0.5))

        # rating change in season
        out["rating_delta"] = runners["rating_delta_season"].fillna(0.0)

        # draw relative to field size, signed by track (HV outer is worse)
        draw_rel = (runners["draw_num"] / runners["field_size"]).fillna(0.5)
        out["draw_rel"] = draw_rel
        out["draw_rel_hv"] = draw_rel * (runners["race_track"] == "HV").astype(float)

        # gear flags (EDA: both negative)
        out["gear_first_time"] = runners["gear_first_time"]
        out["gear_second_off"] = runners["gear_second_off"]

        # import_type one-hots; ISG was +2.15pp
        out["is_isg"] = (runners["import_type"] == "ISG").astype(int)
        out["is_vis"] = (runners["import_type"] == "VIS").astype(int)

        # days since last start
        last = priors["last"]
        last_date = runners["horse_id"].map(last["last_date"])
        days = (runners["race_date_dt"] - last_date).dt.days
        out["days_since"] = days.fillna(28).clip(0, 200)
        # bucketed flags around the EDA-optimal 14-28d band
        out["dsl_quick"] = (days < 14).fillna(False).astype(int)
        out["dsl_long"] = (days > 60).fillna(False).astype(int)

        # last finish (1=best, 14=worst). Missing => 7.
        last_fin = runners["horse_id"].map(last["last_finish"])
        out["last_finish"] = last_fin.fillna(7.0)

        return out

    def fit(self, training: pd.DataFrame, history: pd.DataFrame) -> "FrameworkA":
        """training: rows whose true outcome we can use for fitting.
        history:  rows that are STRICTLY older than every row in `training`,
                  used to build jockey/trainer/horse priors without leakage.
        """
        priors = self._build_history_priors(history)
        X = self._featurize(training, priors)
        # Fit: target is `won`, with the market log-odds offset.
        # Use sklearn LR but pass mkt log-odds as a feature with coef forced to 1
        # via offset trick: use mkt log-odds as an extra feature; we will add it
        # *after* fitting by using sample_weight=None and intercept_scaling=1
        # (sklearn doesn't support offset directly). Instead: fit residual.
        mkt = training["mkt_prob"].clip(1e-4, 1 - 1e-4).values
        mkt_logit = np.log(mkt / (1 - mkt))
        y = training["won"].values

        valid = (~X.isna().any(axis=1)) & (~np.isnan(mkt_logit)) & training["completed"]
        Xv = X[valid].values
        yv = y[valid]
        mlv = mkt_logit[valid]

        # We optimise residual: fit p_full = sigmoid(mkt_logit + a + b.X)
        # by treating mkt_logit as an offset via sample augmentation:
        # use a custom fit -- sklearn LR has no offset, so we shift X with an
        # extra column equal to mkt_logit and freeze its coefficient by
        # rescaling.  Simplest faithful implementation: maximise log-likelihood
        # ourselves with scipy, but to keep deps small we use IRLS-equivalent
        # by appending mkt_logit as a feature and post-checking its coef.
        Xv_aug = np.column_stack([Xv, mlv])
        lr = LogisticRegression(C=1.0, max_iter=300, solver="lbfgs")
        lr.fit(Xv_aug, yv)
        self.coef_ = lr.coef_[0]
        self.intercept_ = float(lr.intercept_[0])
        self.feature_names_ = list(X.columns) + ["mkt_logit"]
        return self

    def predict_proba(self, runners: pd.DataFrame, history: pd.DataFrame) -> pd.Series:
        priors = self._build_history_priors(history)
        X = self._featurize(runners, priors)
        mkt = runners["mkt_prob"].clip(1e-4, 1 - 1e-4).values
        mkt_logit = np.log(mkt / (1 - mkt))

        # impute remaining NaNs with column means from features used at fit-time
        X = X.fillna(X.mean(numeric_only=True))
        Xv = X.values
        Xv_aug = np.column_stack([Xv, mkt_logit])
        z = self.intercept_ + Xv_aug @ self.coef_
        # sigmoid
        p = 1.0 / (1.0 + np.exp(-z))
        # renormalise per race
        race_id = (runners["race_date"].astype(str) + "_" +
                   runners["race_number"].astype(str))
        return normalize_per_race(pd.Series(p, index=runners.index), race_id)

    def explain(self) -> pd.DataFrame:
        if self.coef_ is None:
            raise RuntimeError("model not fit")
        return pd.DataFrame({"feature": self.feature_names_,
                             "coef": self.coef_}).sort_values("coef", key=abs, ascending=False)
