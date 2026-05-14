"""
pace_advantage_v2.py
====================

Mechanical replacement for the old ``advantage`` score in
``speed_map.grid`` which had Spearman ρ = −0.066 with finishing position
(i.e. pure noise). The new score is fully derived from observable race
inputs — no model, no training — and so cannot decorrelate over time.

Definition
----------
For each horse:

    own_esz_pct       = percentile of own early_speed_z in the race
                        (0 = slowest / most-back horse → 1 = fastest)

    inside_field_esz  = mean ESZ of all horses drawn STRICTLY INSIDE you

    inside_handicap   = (own_esz_pct − inside_field_esz_pct)

    style_match       = +1 if (Leader/On-Pace) AND pace_label ∈ {Slow, …}
                        +1 if (Closer)       AND pace_label ∈ {Fast, …}
                        -1 if mismatch
                         0 if pace label unreliable / no info

Then:

    pace_advantage_v2 = 0.5 * own_esz_pct + 0.5 * (1 - inside_handicap)
                        + 0.10 * style_match

Bounded approximately to [−0.1, 1.1].

This module also provides:

    `place_edge(horse, race)`  →  float in [0, 1] suitable as a
    PLACE-pool tie-breaker. Combines the demoted is_beneficiary
    flag (kept ONLY for PLACE — see audit) with the new advantage
    score.

Side-effect-free. All inputs are dicts from existing race-day reports.
"""
from __future__ import annotations

from typing import Iterable


# ── Helpers ────────────────────────────────────────────────────────────
def _rank_pct(value, all_values: list[float]) -> float:
    """Return percentile rank of value within all_values. Higher value =
    more negative ESZ = faster early. So we sort ascending (most-fast at
    rank 0) and invert."""
    if value is None or not all_values:
        return 0.5
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.5
    s = sorted(float(x) for x in all_values if x is not None)
    if not s:
        return 0.5
    # ESZ: lower = faster. We want fastest-early to be percentile 1.0.
    n_lower = sum(1 for x in s if x < v)
    return 1.0 - (n_lower / len(s))


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


_LEADER_STYLES = {"Leader", "On-Pace"}
_CLOSER_STYLES = {"Closer"}
_FAST_LABELS = {"Fast", "Very Fast", "Slightly Fast", "Hot"}
_SLOW_LABELS = {"Slow", "Very Slow", "Slightly Slow"}


def _style_match(style: str | None, pace_label: str | None) -> int:
    if not style or not pace_label:
        return 0
    p = str(pace_label).strip()
    if style in _LEADER_STYLES and p in _SLOW_LABELS:
        return 1
    if style in _CLOSER_STYLES and p in _FAST_LABELS:
        return 1
    if style in _LEADER_STYLES and p in _FAST_LABELS:
        return -1
    if style in _CLOSER_STYLES and p in _SLOW_LABELS:
        return -1
    return 0


# ── Main computation ───────────────────────────────────────────────────
def compute_for_race(race: dict, pace_label_override: str | None = None
                     ) -> list[dict]:
    """Compute pace_advantage_v2 for every pick in a race.

    Returns a list of {horse_no, horse_name, pace_advantage_v2,
    own_esz_pct, inside_handicap, style_match}.
    """
    picks = race.get("picks") or []
    if not picks:
        return []

    # All ESZs in the race
    all_esz = [p.get("early_speed_z") for p in picks
               if p.get("early_speed_z") is not None]

    # Build draw → ESZ map for inside-field calc
    by_draw: dict[int, float] = {}
    for p in picks:
        try:
            d = int(p.get("draw"))
            v = p.get("early_speed_z")
            if v is None:
                continue
            by_draw[d] = float(v)
        except (TypeError, ValueError):
            continue

    pace_label = pace_label_override or race.get("pace")

    out: list[dict] = []
    for p in picks:
        try:
            draw = int(p.get("draw"))
        except (TypeError, ValueError):
            draw = None
        own_esz = p.get("early_speed_z")
        own_pct = _rank_pct(own_esz, all_esz)

        # Inside-field mean ESZ
        if draw is not None and by_draw:
            inside_esz_vals = [v for d, v in by_draw.items() if d < draw]
        else:
            inside_esz_vals = []
        if inside_esz_vals:
            inside_mean = sum(inside_esz_vals) / len(inside_esz_vals)
            inside_pct = _rank_pct(inside_mean, all_esz)
        else:
            inside_pct = 0.5  # no horses inside → neutral

        # Negative handicap if inside horses are faster than you
        inside_handicap = max(0.0, inside_pct - own_pct)

        sm = _style_match(p.get("style"), pace_label)

        adv = (
            0.5 * own_pct
            + 0.5 * (1.0 - inside_handicap)
            + 0.10 * sm
        )
        adv = max(-0.1, min(1.1, adv))

        out.append({
            "horse_no":          p.get("horse_no"),
            "horse_name":        p.get("horse_name"),
            "pace_advantage_v2": round(adv, 3),
            "own_esz_pct":       round(own_pct, 3),
            "inside_handicap":   round(inside_handicap, 3),
            "style_match":       sm,
        })

    return out


# ── Place edge (combines beneficiary + advantage_v2) ───────────────────
def place_edge_for_pick(pick: dict, race: dict,
                         pace_advantage_v2: float | None) -> float:
    """Compose a PLACE-only edge score in [0, 1]. Combines:
      - pace_advantage_v2  (continuous)
      - is_beneficiary     (binary, demoted per audit findings: +9pp
                            place lift in isolation, kept here only.)
      - win_prob           (the model's own signal)

    Weighting:
      0.45 * win_prob_norm
      0.35 * pace_advantage_v2
      0.20 * is_beneficiary_flag
    """
    wp = _safe_float(pick.get("win_prob"))
    # Normalize win_prob: typical max ~0.30 → multiply by 3 then clip
    wp_n = max(0.0, min(1.0, wp * 3.0))

    adv = pace_advantage_v2 if pace_advantage_v2 is not None else 0.5

    # Locate beneficiary flag in speed_map.grid
    sm_grid = (race.get("speed_map") or {}).get("grid") or []
    is_ben = False
    try:
        hn = int(pick.get("horse_no"))
        for g in sm_grid:
            try:
                if int(g.get("horse_no")) == hn:
                    is_ben = bool(g.get("is_beneficiary"))
                    break
            except (TypeError, ValueError):
                continue
    except (TypeError, ValueError):
        pass

    score = 0.45 * wp_n + 0.35 * adv + 0.20 * (1.0 if is_ben else 0.0)
    return round(max(0.0, min(1.0, score)), 3)
