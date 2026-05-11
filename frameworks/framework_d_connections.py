"""
Framework D — Connections-conditional Beta-Bernoulli.

Core insight (from EDA): jockey × trainer pair effects are large and only
partly priced. Top combo (Purton / C S Shum): 30.4% strike on 56 rides,
+9.8 pp residual. Conversely some combos sit at near-zero conversion.

Strategy:
  1. For each pair (jockey, trainer) compute Beta-Bernoulli posterior with
     hierarchical shrinkage:
        - level 0: overall base rate p0
        - level 1: jockey marginal pj (shrunk to p0)
        - level 2: trainer marginal pt (shrunk to p0)
        - level 3: pair pjt (shrunk to a blend of pj and pt)
     using prior strength k that scales sample size.
  2. Pair posterior gives an unconditional "win expectation" for the
     jockey/trainer combo. Translate to a per-runner score by ranking
     within each race against the posterior of every other (jockey, trainer)
     in the field.
  3. Convert those scores to per-race probabilities via softmax.

Optional ability anchor: blend with an extremely simple horse career
posterior (also Beta-Bernoulli) so completely unraced horses paired with a
strong jockey/trainer combo still get a sensible score, but the model is
*primarily* about the connections.

Like B and C, this framework deliberately does NOT use the market price.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pandas as pd

from .common import softmax_per_race


def _beta_post_mean(wins: float, n: float, prior_p: float, prior_k: float) -> float:
    return (wins + prior_p * prior_k) / (n + prior_k)


@dataclass
class FrameworkD:
    k_global: float = 60.0       # strength of global prior on jockey/trainer marginals
    k_pair: float = 30.0         # strength of (jockey-prior + trainer-prior) blend on pairs
    k_horse: float = 25.0        # horse-career posterior strength
    horse_blend: float = 0.35    # weight of horse posterior vs connections posterior
    temperature: float = 0.35

    def _build_posteriors(self, history: pd.DataFrame) -> dict:
        h = history[history["completed"]]
        p0 = float(h["won"].mean()) if len(h) else 0.0815

        jockey_n = h.groupby("jockey")["won"].agg(["sum", "count"])
        jockey_n["post"] = jockey_n.apply(
            lambda r: _beta_post_mean(r["sum"], r["count"], p0, self.k_global), axis=1
        )

        trainer_n = h.groupby("trainer")["won"].agg(["sum", "count"])
        trainer_n["post"] = trainer_n.apply(
            lambda r: _beta_post_mean(r["sum"], r["count"], p0, self.k_global), axis=1
        )

        pair_n = h.groupby(["jockey", "trainer"])["won"].agg(["sum", "count"])

        # per-pair prior is the *average* of the jockey and trainer posteriors
        def _pair_post(row):
            jname, tname = row.name
            pj = jockey_n.loc[jname, "post"] if jname in jockey_n.index else p0
            pt = trainer_n.loc[tname, "post"] if tname in trainer_n.index else p0
            prior = 0.5 * (pj + pt)
            return _beta_post_mean(row["sum"], row["count"], prior, self.k_pair)

        pair_n["post"] = pair_n.apply(_pair_post, axis=1)

        horse_n = h.groupby("horse_id")["won"].agg(["sum", "count"])
        horse_n["post"] = horse_n.apply(
            lambda r: _beta_post_mean(r["sum"], r["count"], p0, self.k_horse), axis=1
        )

        return {
            "p0": p0,
            "jockey": jockey_n["post"].to_dict(),
            "trainer": trainer_n["post"].to_dict(),
            "pair": pair_n["post"].to_dict(),
            "horse": horse_n["post"].to_dict(),
        }

    def fit(self, training: pd.DataFrame, history: pd.DataFrame) -> "FrameworkD":
        return self

    def predict_proba(self, runners: pd.DataFrame, history: pd.DataFrame) -> pd.Series:
        post = self._build_posteriors(history)
        p0 = post["p0"]

        def _score_row(row):
            j, t = row["jockey"], row["trainer"]
            key = (j, t)
            if key in post["pair"]:
                conn = post["pair"][key]
            else:
                pj = post["jockey"].get(j, p0)
                pt = post["trainer"].get(t, p0)
                conn = 0.5 * (pj + pt)
            horse = post["horse"].get(row["horse_id"], p0)
            blended = (1 - self.horse_blend) * conn + self.horse_blend * horse
            # log-odds is the natural score for softmax
            blended = float(np.clip(blended, 1e-4, 1 - 1e-4))
            return np.log(blended / (1 - blended))

        scores = runners.apply(_score_row, axis=1)
        race_id = (runners["race_date"].astype(str) + "_" +
                   runners["race_number"].astype(str))
        return softmax_per_race(scores, race_id, temperature=self.temperature)
