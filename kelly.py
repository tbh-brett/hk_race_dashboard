"""
kelly.py — Fractional Kelly with edge shrinkage and $10-min quantization
=========================================================================

Phase B. Stake-sizing module. Pure functions over (p, b, bankroll, mode).

Kelly classic
-------------
For a binary bet at decimal odds `O` (net odds `b = O-1`) with subjective
probability `p` of winning:

    f* = (b*p - q) / b           where q = 1 - p

`f*` is the fraction of bankroll to risk. f* < 0 → don't bet (no edge).

Fractional Kelly
----------------
Real bookies and the bettor (you) are not infinitely confident in `p`.
Half-Kelly trades ~75% of the geometric growth for ~50% of the variance
[Thorp 2006]. Quarter-Kelly is even more conservative. We use:

    fraction = MODE_TABLE[mode]["kelly_fraction"]
    f_bet = max(0, f*) * fraction

Edge shrinkage (small-sample correction)
----------------------------------------
With ~7 meetings of data, raw `p_model` is noisy. We shrink toward the
market-implied prior:

    p_shrunk = (1 - lam) * p_market + lam * p_model

`lam ∈ [0, 1]`:
    - lam=1  : trust model fully (use raw p_model)
    - lam=0  : trust market fully (no edge possible)
    - lam=k/(k+n_eff) : James-Stein-flavoured pull toward market when n_eff is small

We default to `lam = n_eff / (n_eff + k)` with k = K_PRIOR (≈ 50). At 100
historical bets, lam ≈ 0.67 (model dominates); at 10 bets, lam ≈ 0.17
(strongly pulled to market). This mirrors the "moderate" stance the user
selected.

HKJC $10-min quantization
-------------------------
HKJC takes whole-$10 stakes only. After computing `bet_hkd`, we floor to
the nearest $10. If the floor would be 0, we either bet $10 (when EV is
clearly positive and bankroll permits) or skip — the choice is given by
`min_bet_policy`.

Output schema
-------------
    Stake(stake_hkd, kelly_full, kelly_fraction_used, p_used, p_market,
          edge_pct, ev_per_dollar, reason, accepted)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Literal, Optional


# ---------------------------------------------------------------------------
# Mode parameters (single source of truth for "aggression tier")
# ---------------------------------------------------------------------------
# Each mode controls:
#   kelly_fraction      : multiplier on raw f*
#   min_edge_pct        : minimum (p_used / p_market - 1) to fire (e.g. 0.10 = 10%)
#   min_p_used          : minimum subjective win prob to fire
#   max_per_bet_frac    : hard cap as fraction of bankroll
#   max_per_meeting_frac: hard cap on total exposure across a meeting
#   shrinkage_k         : pseudo-count for shrinkage (lower = more shrinkage)
#   pool_whitelist      : which bet types are allowed in this mode
#   allow_allup         : enable cross-race chains
# ---------------------------------------------------------------------------
MODE_TABLE = {
    "conservative": {
        "kelly_fraction":       0.25,   # quarter-Kelly
        "min_edge_pct":         0.20,   # +20% over market
        "min_p_used":           0.20,
        "max_per_bet_frac":     0.020,  # 2%
        "max_per_meeting_frac": 0.10,   # 10%
        "shrinkage_k":          80,
        "pool_whitelist":       {"WIN", "PLACE", "QPL_BANKER"},
        "allow_allup":          False,
    },
    "balanced": {
        "kelly_fraction":       0.50,   # half-Kelly  (moderate stance)
        "min_edge_pct":         0.10,
        "min_p_used":           0.12,
        "max_per_bet_frac":     0.030,
        "max_per_meeting_frac": 0.15,
        "shrinkage_k":          50,
        "pool_whitelist":       {"WIN", "PLACE", "QIN_BANKER", "QPL_BANKER"},
        "allow_allup":          True,
    },
    "aggressive": {
        "kelly_fraction":       0.50,   # cap at half-Kelly
        "min_edge_pct":         0.05,
        "min_p_used":           0.08,
        "max_per_bet_frac":     0.050,
        "max_per_meeting_frac": 0.20,
        "shrinkage_k":          35,
        "pool_whitelist":       {"WIN", "PLACE", "QIN_BANKER", "QPL_BANKER",
                                  "F4_BOX", "QTT_BOX"},
        "allow_allup":          True,
    },
    # ---------------------------------------------------------------
    # UNCAPPED — user explicitly removed all constraints.
    #
    # No edge floor, no probability floor, no per-bet cap, no meeting
    # cap, no shrinkage (raw p_model trusted), every pool whitelisted,
    # all-up enabled with maximum allocation. Full Kelly.
    #
    # WARNING: pure Kelly on small-sample noisy probability estimates
    # is mathematically expected to bust the bankroll a non-trivial
    # fraction of the time even with a true edge — and *certainly*
    # busts it when the edge estimate is wrong. We're running this
    # because the user asked for it, not because it's wise.
    # ---------------------------------------------------------------
    "uncapped": {
        "kelly_fraction":       1.00,   # FULL Kelly
        "min_edge_pct":         -1.0,   # no floor — bet anything ≥ 0 EV
        "min_p_used":           0.0,
        "max_per_bet_frac":     1.0,    # no cap (bounded only by Kelly itself)
        "max_per_meeting_frac": 5.0,    # 5x bankroll — effectively no cap
        "shrinkage_k":          0,      # interpreted as "no shrinkage"
        "pool_whitelist":       {"WIN", "PLACE", "QIN_BANKER", "QPL_BANKER",
                                  "F4_BOX", "QTT_BOX", "F4_BOX_TOP5",
                                  "TRIO_BOX", "TCE", "FCT"},
        "allow_allup":          True,
    },
}

HKJC_MIN_STAKE = 10.0


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------
@dataclass
class Stake:
    accepted: bool
    stake_hkd: float
    kelly_full: float            # raw f* (before fractional + caps)
    kelly_used: float            # fraction actually used
    p_used: float
    p_market: float
    edge_pct: float              # (p_used / p_market) - 1
    ev_per_dollar: float         # b*p - q   (expected $ return per $1 staked)
    fraction_of_bankroll: float
    reason: str
    mode: str

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Core sizing
# ---------------------------------------------------------------------------
def shrink_probability(
    p_model: float,
    p_market: float,
    *,
    n_observed: int,
    k_prior: float,
) -> float:
    """James-Stein-style pull toward market.

    `n_observed` is the number of historical bets we've evaluated for the
    relevant strategy slice (e.g., "QIN_BANKER, Cls3-5, SP 3-8 bets to date").
    Use 0 if unknown — function falls back to lam=0.5.
    """
    if p_model is None:
        return p_market or 0.0
    if p_market is None or p_market <= 0:
        return p_model
    if k_prior <= 0:
        # k_prior == 0 → trust model fully (uncapped mode)
        return p_model
    if n_observed <= 0:
        lam = 0.5
    else:
        lam = n_observed / (n_observed + k_prior)
    return lam * p_model + (1.0 - lam) * p_market


def kelly_fraction_full(p: float, decimal_odds: float) -> float:
    """Classic Kelly fraction. Returns 0 if no edge (negative or zero)."""
    if p is None or decimal_odds is None or decimal_odds <= 1.0:
        return 0.0
    b = decimal_odds - 1.0
    q = 1.0 - p
    f = (b * p - q) / b
    return max(0.0, f)


def size_bet(
    *,
    p_model: float,
    p_market: float,
    decimal_odds: float,
    bankroll: float,
    pool: str,
    mode: Literal["conservative", "balanced", "aggressive"] = "balanced",
    n_observed: int = 0,
    min_bet_policy: Literal["floor", "skip", "force_min"] = "floor",
    edge_pct_override: Optional[float] = None,
) -> Stake:
    """Return a `Stake` with the recommended HKD bet.

    Parameters
    ----------
    p_model, p_market : float
        Raw probabilities. Both must be in (0, 1).
    decimal_odds : float
        Pool dividend / 1, e.g. WIN at 5.0 means +400% on hit. For QIN/QPL,
        pass the (estimated) per-combo dividend as decimal odds.
    bankroll : float
        Current HKD bankroll.
    pool : str
        e.g. "WIN", "QIN_BANKER", "QPL_BANKER", "F4_BOX". Used for whitelist.
    mode : str
        "conservative" | "balanced" | "aggressive".
    n_observed : int
        Historical sample size for this strategy slice. Drives shrinkage.
    min_bet_policy : str
        - "floor" : floor to $10 multiple, skip if floor==0
        - "skip"  : skip if computed bet < $10
        - "force_min" : if the bet has positive edge but rounds to <$10,
                        bet $10 anyway (caps at max_per_bet)
    edge_pct_override : float, optional
        For pools where p_market is unknown but the user has a manual
        confidence, supply edge_pct directly. Bypasses shrinkage.
    """
    cfg = MODE_TABLE[mode]

    # Pool whitelist
    if pool not in cfg["pool_whitelist"]:
        return Stake(False, 0.0, 0.0, 0.0, p_model or 0.0,
                     p_market or 0.0, 0.0, 0.0, 0.0,
                     f"pool {pool} not in {mode} whitelist", mode)

    if bankroll <= 0:
        return Stake(False, 0.0, 0.0, 0.0, p_model or 0.0,
                     p_market or 0.0, 0.0, 0.0, 0.0,
                     "bankroll depleted", mode)

    # Shrink probability
    if p_model is None or p_market is None or p_model <= 0 or p_market <= 0:
        return Stake(False, 0.0, 0.0, 0.0, p_model or 0.0,
                     p_market or 0.0, 0.0, 0.0, 0.0,
                     "missing p_model or p_market", mode)

    p_used = shrink_probability(
        p_model, p_market,
        n_observed=n_observed, k_prior=cfg["shrinkage_k"],
    )
    edge_pct = (p_used / p_market) - 1.0 if p_market > 0 else 0.0
    if edge_pct_override is not None:
        edge_pct = edge_pct_override

    # EV check
    b = max(0.0, decimal_odds - 1.0)
    q = 1.0 - p_used
    ev = b * p_used - q

    # Edge & probability gates
    if edge_pct < cfg["min_edge_pct"]:
        return Stake(False, 0.0, 0.0, 0.0, p_used, p_market, edge_pct, ev, 0.0,
                     f"edge {edge_pct:+.1%} < {cfg['min_edge_pct']:+.0%} for {mode}",
                     mode)
    if p_used < cfg["min_p_used"]:
        return Stake(False, 0.0, 0.0, 0.0, p_used, p_market, edge_pct, ev, 0.0,
                     f"p_used {p_used:.2f} < {cfg['min_p_used']:.2f} for {mode}",
                     mode)
    if ev <= 0:
        return Stake(False, 0.0, 0.0, 0.0, p_used, p_market, edge_pct, ev, 0.0,
                     "EV per $ ≤ 0", mode)

    # Kelly
    f_full = kelly_fraction_full(p_used, decimal_odds)
    f_used = f_full * cfg["kelly_fraction"]
    f_used = min(f_used, cfg["max_per_bet_frac"])

    raw_hkd = f_used * bankroll

    # $10 quantization
    if min_bet_policy == "floor":
        hkd = math.floor(raw_hkd / HKJC_MIN_STAKE) * HKJC_MIN_STAKE
        if hkd < HKJC_MIN_STAKE:
            return Stake(False, 0.0, f_full, f_used, p_used, p_market,
                         edge_pct, ev, raw_hkd / bankroll if bankroll else 0,
                         f"raw bet ${raw_hkd:.1f} below ${HKJC_MIN_STAKE} min", mode)
    elif min_bet_policy == "skip":
        if raw_hkd < HKJC_MIN_STAKE:
            return Stake(False, 0.0, f_full, f_used, p_used, p_market,
                         edge_pct, ev, raw_hkd / bankroll if bankroll else 0,
                         f"raw bet ${raw_hkd:.1f} below ${HKJC_MIN_STAKE} min", mode)
        hkd = math.floor(raw_hkd / HKJC_MIN_STAKE) * HKJC_MIN_STAKE
    elif min_bet_policy == "force_min":
        hkd = max(HKJC_MIN_STAKE,
                  math.floor(raw_hkd / HKJC_MIN_STAKE) * HKJC_MIN_STAKE)
        if hkd > cfg["max_per_bet_frac"] * bankroll:
            hkd = math.floor(cfg["max_per_bet_frac"] * bankroll
                              / HKJC_MIN_STAKE) * HKJC_MIN_STAKE
        if hkd < HKJC_MIN_STAKE:
            return Stake(False, 0.0, f_full, f_used, p_used, p_market,
                         edge_pct, ev, 0.0,
                         "max_per_bet cap below $10 min", mode)
    else:
        raise ValueError(f"Unknown min_bet_policy: {min_bet_policy}")

    return Stake(
        accepted=True,
        stake_hkd=hkd,
        kelly_full=round(f_full, 4),
        kelly_used=round(f_used, 4),
        p_used=round(p_used, 4),
        p_market=round(p_market, 4),
        edge_pct=round(edge_pct, 4),
        ev_per_dollar=round(ev, 4),
        fraction_of_bankroll=round(hkd / bankroll, 4),
        reason=f"Kelly {cfg['kelly_fraction']:.0%}-frac · edge {edge_pct:+.1%} · "
               f"p_shr {p_used:.2f} (k={cfg['shrinkage_k']})",
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    cases = [
        # 30% true prob, market thinks 22% (odds 4.5), bankroll 1000
        dict(p_model=0.30, p_market=0.22, decimal_odds=4.5,
             bankroll=1000, pool="WIN"),
        # Tiny edge (5%) — should clear aggressive but not balanced
        dict(p_model=0.23, p_market=0.22, decimal_odds=4.5,
             bankroll=1000, pool="WIN"),
        # No edge
        dict(p_model=0.20, p_market=0.22, decimal_odds=4.5,
             bankroll=1000, pool="WIN"),
        # Pool not whitelisted in conservative
        dict(p_model=0.30, p_market=0.22, decimal_odds=4.5,
             bankroll=1000, pool="QIN_BANKER", mode="conservative"),
    ]
    for c in cases:
        m = c.pop("mode", "balanced")
        s = size_bet(mode=m, n_observed=20, **c)
        print(f"[{m}] {c['pool']:<10} p_mod {c['p_model']:.2f} O {c['decimal_odds']:.1f} "
              f"-> ${s.stake_hkd:>5.0f}  edge {s.edge_pct:+.1%}  {s.reason}")
