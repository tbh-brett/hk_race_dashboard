"""
Framework B — Pace-tactics simulator.

Core insight (from EDA): the market under-prices the *interaction of pace
shapes*. Front-runners win at +4.3 pp residual to the market; the residual is
amplified at sprint trips and is largest when there is no other obvious
front-runner ("lone speed" scenario).

Strategy:
  1. For every horse, build a "pace profile" from its history: the empirical
     distribution of `early_pos` (sectional 1 position) NORMALISED by field
     size of each prior race. Smaller value = more likely to lead.
  2. For each upcoming race, infer each runner's *pace tier* (lead / press /
     mid / off / back) from its profile.
  3. Score each runner using two tactical adjustments:
       - leader bonus, scaled by *lone-speed factor* (large if this runner is
         the only likely leader in the field; small if many leaders contest)
       - off-pace penalty, scaled by track/distance (HV bias toward inner
         leaders, ST 1000 m bias toward speed)
  4. Combine with a simple ability anchor (recent finish-position quantile)
     so that the model is not betting hopeless leaders. The ability anchor is
     deliberately weak — this framework is *primarily* about pace shape.

Output is a per-runner relative score; we softmax-normalise per race.

This framework deliberately does NOT use the market price.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pandas as pd

from .common import softmax_per_race


# Track / distance configuration: positive = lead-favouring; negative = closer-favouring
_TRACK_DIST_BIAS = {
    ("ST", 1000): 0.30,
    ("ST", 1200): 0.25,
    ("ST", 1400): 0.15,
    ("ST", 1600): 0.05,
    ("ST", 1650): 0.10,
    ("ST", 1800): 0.05,
    ("ST", 2000): 0.05,
    ("ST", 2200): 0.00,
    ("ST", 2400): 0.00,
    ("HV", 1000): 0.25,
    ("HV", 1200): 0.30,
    ("HV", 1650): 0.20,
    ("HV", 1800): -0.05,  # 4-turn -> closers can finish strong
    ("HV", 2200): 0.10,
}


@dataclass
class FrameworkB:
    leader_bonus: float = 0.6
    presser_bonus: float = 0.25
    closer_penalty: float = -0.35
    backmarker_penalty: float = -0.55
    ability_weight: float = 0.4
    temperature: float = 0.6   # softmax temperature; smaller = more peaked

    def _build_horse_profile(self, history: pd.DataFrame) -> pd.DataFrame:
        h = history[history["completed"] & history["early_pos"].notna()].copy()
        h["early_rel"] = h["early_pos"] / h["field_size"]
        # take the last <=5 starts to keep it recent
        h = h.sort_values(["horse_id", "race_date_dt"]).groupby("horse_id").tail(5)
        prof = h.groupby("horse_id").agg(
            avg_early_rel=("early_rel", "mean"),
            min_early_rel=("early_rel", "min"),
            n_obs=("early_rel", "size"),
            avg_finish=("finish_pos", "mean"),
        )
        return prof

    def _classify_pace(self, avg_early_rel: float) -> str:
        if pd.isna(avg_early_rel):
            return "mid"
        if avg_early_rel < 0.20:
            return "lead"
        if avg_early_rel < 0.40:
            return "press"
        if avg_early_rel < 0.60:
            return "mid"
        if avg_early_rel < 0.80:
            return "off"
        return "back"

    def _ability_score(self, runner: pd.Series, profile: pd.DataFrame) -> float:
        """Cheap ability anchor: invert avg_finish from history, in [0,1]."""
        h = profile.get("avg_finish") if isinstance(profile, dict) else None
        avg_fin = profile.loc[runner["horse_id"], "avg_finish"] if runner["horse_id"] in profile.index else np.nan
        if pd.isna(avg_fin):
            return 0.0
        # Map avg_finish in [1, 14] -> [+1, -1]
        return (7.5 - avg_fin) / 6.5

    def fit(self, training: pd.DataFrame, history: pd.DataFrame) -> "FrameworkB":
        # Nothing to learn — this is a rules-based simulator.
        # We still ingest inputs so the API matches the other frameworks.
        return self

    def predict_proba(self, runners: pd.DataFrame, history: pd.DataFrame) -> pd.Series:
        profile = self._build_horse_profile(history)
        idx = runners.index
        scores = np.zeros(len(runners))
        # iterate per race so we can compute lone-speed factor in context
        for (rdate, rnum), grp in runners.groupby(["race_date", "race_number"], sort=False):
            track = grp["race_track"].iloc[0]
            dist = int(grp["distance"].iloc[0]) if pd.notna(grp["distance"].iloc[0]) else 1200
            field = int(grp["field_size"].iloc[0])
            bias = _TRACK_DIST_BIAS.get((track, dist), 0.10)

            # find each runner's pace tier
            avg_rel_list = []
            tier_list = []
            ability_list = []
            for _, runner in grp.iterrows():
                hid = runner["horse_id"]
                if hid in profile.index:
                    avg_rel = profile.loc[hid, "avg_early_rel"]
                    avg_fin = profile.loc[hid, "avg_finish"]
                else:
                    avg_rel = np.nan
                    avg_fin = np.nan
                avg_rel_list.append(avg_rel)
                tier_list.append(self._classify_pace(avg_rel))
                ability_list.append(0.0 if pd.isna(avg_fin) else (7.5 - avg_fin) / 6.5)

            # count likely leaders / pressers
            n_lead = sum(1 for t in tier_list if t == "lead")
            n_press = sum(1 for t in tier_list if t == "press")
            # lone-speed factor: 1.5 if you are the only leader, 1.0 if 2 leaders, 0.5 if 3+
            if n_lead == 0:
                lone_factor = 1.0   # everyone gets a small bonus, will be muted by mid tier
            elif n_lead == 1:
                lone_factor = 1.5
            elif n_lead == 2:
                lone_factor = 1.0
            else:
                lone_factor = 0.6

            # off-pace penalty grows with field size at lead-biased tracks
            field_press = max(0.0, (field - 10) / 4.0)

            for i, (tier, ability) in enumerate(zip(tier_list, ability_list)):
                if tier == "lead":
                    s = self.leader_bonus * lone_factor + bias
                elif tier == "press":
                    s = self.presser_bonus * (1.0 + 0.4 * (lone_factor - 1.0)) + 0.5 * bias
                elif tier == "mid":
                    s = 0.0
                elif tier == "off":
                    s = self.closer_penalty - 0.5 * bias - 0.1 * field_press
                else:  # back
                    s = self.backmarker_penalty - bias - 0.2 * field_press
                # combine with ability
                s = s + self.ability_weight * ability
                # tiny inner-draw boost for HV
                draw = grp["draw_num"].iloc[i] if pd.notna(grp["draw_num"].iloc[i]) else field / 2
                if track == "HV":
                    s += 0.15 * (1.0 - draw / max(field, 1))
                else:
                    s += 0.05 * (1.0 - draw / max(field, 1))
                scores[idx.get_loc(grp.index[i])] = s

        race_id = (runners["race_date"].astype(str) + "_" +
                   runners["race_number"].astype(str))
        return softmax_per_race(pd.Series(scores, index=runners.index), race_id,
                                temperature=self.temperature)
