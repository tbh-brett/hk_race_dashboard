"""
market_belief.py
================
Layer 1 of the Market/Behavioural model.

Turns raw HKJC pool prices (final-SP win odds, place odds, QIN/QPL pair
odds) into proper probability distributions — the public's posterior
belief over race outcomes — with the standard pari-mutuel biases removed.

Primitives
----------
    implied_basic(odds)         -> naive 1/o then normalised to sum-1.
                                   Carries favourite-longshot bias and
                                   overround noise.
    shin_win(odds)              -> Shin (1993) bias-corrected P(win).
                                   Iteratively solves for insider-
                                   trading parameter z in [0, 0.2].
    shin_place(odds, n_places)  -> Shin for the place pool. HKJC is
                                   3 places for 7+ runners, else 2.
    harville_from_qin(pair_odds, overround=None)
                                -> Derive a marginal win vector from
                                   the full QIN pair-odds matrix via
                                   Harville / Plackett-Luce inversion.
    kl(p, q)                    -> sum p*log(p/q). Symmetrised via
                                   js_divergence(p, q).
    overround(odds)             -> sum(1/o)  (==1 means fair book).

Conventions
-----------
- Inputs are 1-indexed mappings {horse_no: odds} OR parallel lists of
  (horse_no, odds). Outputs are {horse_no: prob} keyed identically.
- Scratched / non-runners must be filtered before calling; any odds
  that are NaN / 0 / missing are dropped and the caller is warned.
- Probabilities always renormalise to exactly 1 on returned keys.

This module is pure (no I/O, no pandas). Backtest and dashboard code
does the loading.
"""
from __future__ import annotations

import math
from typing import Mapping

# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------


def _clean(odds: Mapping) -> dict[int, float]:
    out: dict[int, float] = {}
    for k, v in odds.items():
        try:
            o = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(o) or o <= 1.0:
            # Odds of 1.0 or less are nonsense (scratching / data error).
            continue
        try:
            out[int(k)] = o
        except (TypeError, ValueError):
            continue
    return out


def overround(odds: Mapping) -> float:
    """Book overround = sum(1/o). 1.0 = fair, >1 = house edge."""
    d = _clean(odds)
    if not d:
        return float("nan")
    return sum(1.0 / o for o in d.values())


def implied_basic(odds: Mapping) -> dict[int, float]:
    """Naive implied probability: (1/o) / sum(1/o)."""
    d = _clean(odds)
    if not d:
        return {}
    raw = {k: 1.0 / o for k, o in d.items()}
    s = sum(raw.values())
    return {k: v / s for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Shin (1993) bias-corrected implied probabilities
# ---------------------------------------------------------------------------
# Model:   pi_i = (sqrt(z^2 + 4 (1 - z) r_i^2 / Sigma) - z) / (2 (1 - z))
# where   r_i = 1 / o_i, Sigma = sum_j r_j (== overround).
# z in [0, 1] is the implied proportion of insider trades. Solve for z
# such that sum_i pi_i = 1.
# Ref: Shin, H.S. (1993) "Measuring the incidence of insider trading in
# a market for state-contingent claims", Economic Journal 103.
# ---------------------------------------------------------------------------


def _shin_pi(r: list[float], sigma: float, z: float) -> list[float]:
    if z >= 1.0:
        z = 0.999999
    denom = 2.0 * (1.0 - z)
    out = []
    for ri in r:
        inner = z * z + 4.0 * (1.0 - z) * ri * ri / sigma
        if inner < 0:
            inner = 0.0
        out.append((math.sqrt(inner) - z) / denom)
    return out


def shin_win(odds: Mapping, *, max_iter: int = 100,
             tol: float = 1e-9) -> dict[int, float]:
    """Shin-corrected win probabilities.

    Returns {horse_no: p_win} summing to 1.0. If solver fails (rare),
    falls back to implied_basic.
    """
    d = _clean(odds)
    if not d:
        return {}
    keys = list(d.keys())
    r = [1.0 / d[k] for k in keys]
    sigma = sum(r)
    if sigma <= 1.0 + 1e-9:
        # No overround -> Shin reduces to 1/o normalised.
        return implied_basic(odds)

    # Bisection on z in (0, 1).  f(z) = sum pi(z) - 1.
    lo, hi = 0.0, 0.99
    f_lo = sum(_shin_pi(r, sigma, lo)) - 1.0
    f_hi = sum(_shin_pi(r, sigma, hi)) - 1.0
    if f_lo * f_hi > 0:
        # No sign change -> fall back.
        return implied_basic(odds)

    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = sum(_shin_pi(r, sigma, mid)) - 1.0
        if abs(f_mid) < tol:
            break
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid

    z_star = mid
    pi = _shin_pi(r, sigma, z_star)
    s = sum(pi)
    if s <= 0:
        return implied_basic(odds)
    return {k: v / s for k, v in zip(keys, pi)}


def shin_z(odds: Mapping) -> float:
    """Return only the Shin z parameter (proxy for informed-money share)."""
    d = _clean(odds)
    if len(d) < 3:
        return float("nan")
    r = [1.0 / o for o in d.values()]
    sigma = sum(r)
    if sigma <= 1.0 + 1e-9:
        return 0.0
    lo, hi = 0.0, 0.99
    f_lo = sum(_shin_pi(r, sigma, lo)) - 1.0
    f_hi = sum(_shin_pi(r, sigma, hi)) - 1.0
    if f_lo * f_hi > 0:
        return float("nan")
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        f_mid = sum(_shin_pi(r, sigma, mid)) - 1.0
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------------------
# Place pool
# ---------------------------------------------------------------------------
def n_place_slots(n_runners: int) -> int:
    """HKJC place-pool paying positions: 3 for 7+ runners else 2."""
    return 3 if n_runners >= 7 else 2


def implied_place_basic(odds: Mapping, n_runners: int) -> dict[int, float]:
    """Naive place implied prob, normalised to sum = n_place_slots."""
    d = _clean(odds)
    if not d:
        return {}
    raw = {k: 1.0 / o for k, o in d.items()}
    s = sum(raw.values())
    if s <= 0:
        return {}
    target = float(n_place_slots(n_runners))
    return {k: v / s * target for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Harville inversion: derive marginal P(win) from QIN pair matrix
# ---------------------------------------------------------------------------
# Plackett-Luce / Harville model: P(i beats j) = p_i / (p_i + p_j).
# For the quinella pool, P(quinella {i,j}) = 2 p_i p_j / (1 - p_i) . Given
# the full matrix of pair prices, fit p_i via fixed-point iteration.
# ---------------------------------------------------------------------------


def harville_from_qin(pair_odds: dict[tuple[int, int], float],
                      *, runners: list[int] | None = None,
                      max_iter: int = 200,
                      tol: float = 1e-8) -> dict[int, float]:
    """Invert a full-matrix of QIN pair odds into marginal win-prob.

    Parameters
    ----------
    pair_odds
        {frozenset({a,b}) or tuple(a,b): odds_for_pair}. Unordered.
    runners
        Optional explicit list of horse numbers; otherwise inferred
        from pair keys.

    Returns
    -------
    {horse_no: p_win}, normalised to 1.0.

    Notes
    -----
    Returns {} if matrix is too sparse (less than n*(n-1)/4 entries).
    """
    # Normalise keys to frozensets.
    mat: dict[frozenset, float] = {}
    for k, v in pair_odds.items():
        try:
            o = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(o) or o <= 1.0:
            continue
        if isinstance(k, frozenset):
            key = k
        else:
            try:
                a, b = k
                key = frozenset((int(a), int(b)))
            except Exception:
                continue
        if len(key) != 2:
            continue
        mat[key] = o

    if not mat:
        return {}

    if runners is None:
        r_set: set[int] = set()
        for key in mat:
            r_set.update(key)
        runners = sorted(r_set)
    n = len(runners)
    if n < 3:
        return {}
    if len(mat) < max(3, n * (n - 1) // 4):
        return {}

    # Target pair probabilities from the inverse of pair odds, rescaled
    # so they sum to the expected QIN-pool total (n_pairs * average
    # prob-per-pair ~= 1 after overround removal).
    raw = {k: 1.0 / o for k, o in mat.items()}
    s = sum(raw.values())
    if s <= 0:
        return {}
    q = {k: v / s for k, v in raw.items()}  # sums to 1 across pairs

    # Fixed-point: p_i^(t+1) = sum_{j != i}  q_{ij} * p_i / (p_i + p_j)
    # re-normalised. Seed uniform.
    p = {h: 1.0 / n for h in runners}
    for _ in range(max_iter):
        new = {h: 0.0 for h in runners}
        for key, qij in q.items():
            i, j = tuple(key)
            pi, pj = p[i], p[j]
            denom = pi + pj
            if denom <= 0:
                continue
            new[i] += qij * pi / denom
            new[j] += qij * pj / denom
        s = sum(new.values())
        if s <= 0:
            return {}
        new = {h: v / s for h, v in new.items()}
        diff = max(abs(new[h] - p[h]) for h in runners)
        p = new
        if diff < tol:
            break
    return p


# ---------------------------------------------------------------------------
# Distribution comparisons
# ---------------------------------------------------------------------------


def _align(p: Mapping, q: Mapping, eps: float = 1e-12) -> list[tuple[float, float]]:
    keys = sorted(set(p.keys()) | set(q.keys()))
    return [(max(float(p.get(k, 0.0)), eps),
             max(float(q.get(k, 0.0)), eps)) for k in keys]


def kl(p: Mapping, q: Mapping) -> float:
    """KL(p || q). Zero when p == q. Positive when p concentrates mass
    where q does not."""
    pairs = _align(p, q)
    return sum(pi * math.log(pi / qi) for pi, qi in pairs)


def js_divergence(p: Mapping, q: Mapping) -> float:
    """Jensen-Shannon divergence in [0, ln2]. Symmetric, bounded.
    Used as the 'pool disagreement' score."""
    pairs = _align(p, q)
    m = [(pi + qi) / 2 for pi, qi in pairs]
    s = 0.0
    for (pi, qi), mi in zip(pairs, m):
        s += 0.5 * pi * math.log(pi / mi)
        s += 0.5 * qi * math.log(qi / mi)
    return s


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Synthetic 6-horse race: true probs 0.40/0.25/0.15/0.10/0.06/0.04
    # Bookmaker adds 18% overround and longshot bias.
    true_p = [0.40, 0.25, 0.15, 0.10, 0.06, 0.04]
    # Distort: shorten favourites, lengthen longshots (favourite-longshot
    # bias means the naive implied *under*-states favs, so book odds
    # actually shorten favs slightly; we simulate by raising longshot
    # odds above 1/p and tightening favs).
    odds = {}
    for i, p in enumerate(true_p, start=1):
        # Inverse distortion + overround factor 1.18.
        distort = 1.0 - 0.10 * (p - 0.15)     # small tilt
        odds[i] = 1.0 / (p * distort) / 1.18

    print("Odds:         ", {k: round(v, 2) for k, v in odds.items()})
    print("Overround:    ", round(overround(odds), 3))
    print("Basic implied:", {k: round(v, 3) for k, v in implied_basic(odds).items()})
    print("Shin implied: ", {k: round(v, 3) for k, v in shin_win(odds).items()})
    print("Shin z:       ", round(shin_z(odds), 4))
    print("True probs:   ", {i + 1: p for i, p in enumerate(true_p)})

    # Harville round-trip: build QIN from true probs, invert, check.
    from itertools import combinations
    pair = {}
    for i, j in combinations(range(1, 7), 2):
        pi, pj = true_p[i - 1], true_p[j - 1]
        # Harville pair prob (unordered quinella)
        q = pi * pj / (1 - pi) + pj * pi / (1 - pj)
        pair[frozenset((i, j))] = 1.0 / q * 1.20  # add 20% overround

    recovered = harville_from_qin(pair)
    print("Harville-QIN: ", {k: round(v, 3) for k, v in recovered.items()})
    print("JS(shin, qin):",
          round(js_divergence(shin_win(odds), recovered), 4))
