"""all_up.py — HKJC-spec All-Up (cross-race parlay) for QIN / QPL.

HKJC All-Up structure
---------------------
Pick N races as legs. For each leg pick ONE selection (here: a Quinella
pair). Pick which subset-sizes to parlay:

    size 1 = single legs        (N units)
    size 2 = any 2 of N legs    (C(N,2) units)
    ...
    size N = full parlay        (1 unit)

Total ticket units = sum of C(N, k) for chosen k. Each unit = stake_per_unit
(min $1). Settlement: a unit covering legs S returns
    stake_per_unit * Π_{leg in S} (div/10)   if every leg in S wins, else 0.

This module produces ONE all-up ticket for QIN and ONE for QPL, given the
same leg list — the user's "Q+QPL = 2 bets" structure across multiple races.

Public API:
    SHAPE_PRESETS                                    — common HKJC shapes
    units_for_shape(n_legs, sizes)                   — int
    build_all_up_ticket(legs, pool, sizes)           — dict (no settlement)
    settle_all_up_ticket(ticket, date_compact)       — dict (return + hits)
    blended_pair(picks, edge_rows, mode)             — (h1, h2) per race
"""
from __future__ import annotations

import json
from itertools import combinations
from math import comb
from pathlib import Path
from typing import Iterable

REPORTS = Path(__file__).parent / "reports"

# HKJC's published preset shapes (ordered as on the all-up table)
# label : (n_legs, set-of-sizes)
SHAPE_PRESETS: dict[str, tuple[int, frozenset[int]]] = {
    "2x1":   (2, frozenset({2})),
    "2x3":   (2, frozenset({1, 2})),
    "3x1":   (3, frozenset({3})),
    "3x3":   (3, frozenset({2})),
    "3x4":   (3, frozenset({2, 3})),
    "3x6":   (3, frozenset({1, 2})),
    "3x7":   (3, frozenset({1, 2, 3})),
    "4x1":   (4, frozenset({4})),
    "4x4":   (4, frozenset({3})),
    "4x5":   (4, frozenset({3, 4})),
    "4x6":   (4, frozenset({2})),
    "4x10":  (4, frozenset({1, 2})),
    "4x11":  (4, frozenset({2, 3, 4})),
    "4x14":  (4, frozenset({1, 2, 3})),
    "4x15":  (4, frozenset({1, 2, 3, 4})),
    "5x1":   (5, frozenset({5})),
    "5x6":   (5, frozenset({4, 5})),
    "5x16":  (5, frozenset({3, 4, 5})),
    "5x26":  (5, frozenset({2, 3, 4, 5})),
    "5x31":  (5, frozenset({1, 2, 3, 4, 5})),
    "6x1":   (6, frozenset({6})),
    "6x7":   (6, frozenset({5, 6})),
    "6x22":  (6, frozenset({4, 5, 6})),
    "6x42":  (6, frozenset({3, 4, 5, 6})),
    "6x57":  (6, frozenset({2, 3, 4, 5, 6})),
    "6x63":  (6, frozenset({1, 2, 3, 4, 5, 6})),
}


def units_for_shape(n_legs: int, sizes: Iterable[int]) -> int:
    return sum(comb(n_legs, k) for k in sizes if 1 <= k <= n_legs)


def build_all_up_ticket(
    *, legs: list[dict], pool: str,
    sizes: Iterable[int],
    stake_per_unit: float = 1.0,
) -> dict:
    """Build one all-up ticket.

    legs:  [{race_no:int, pair:(int,int)}, ...]
    pool:  "QIN" or "QPL"
    sizes: subset-sizes to parlay (e.g. {2,3,4} for 4x11)
    """
    n = len(legs)
    sizes = sorted({int(k) for k in sizes if 1 <= int(k) <= n})
    if n < 2 or not sizes:
        return {"valid": False, "reason": "need >=2 legs and >=1 subset size"}
    units = units_for_shape(n, sizes)
    return {
        "valid": True,
        "pool": pool,
        "legs": [{"race_no": int(lg["race_no"]),
                   "pair": tuple(sorted(int(x) for x in lg["pair"]))}
                  for lg in legs],
        "sizes": sizes,
        "n_legs": n,
        "n_units": units,
        "stake_per_unit": float(stake_per_unit),
        "total_stake": float(units * stake_per_unit),
        "shape_label": _shape_label(n, sizes),
    }


def _shape_label(n: int, sizes: list[int]) -> str:
    for lbl, (nn, ss) in SHAPE_PRESETS.items():
        if nn == n and frozenset(sizes) == ss:
            return lbl
    return f"{n}x{units_for_shape(n, sizes)}"


def _read_dividends(date_compact: str) -> dict:
    p = REPORTS / f"dividends_{date_compact}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _race_div(div_doc: dict, race_no: int, pool: str,
              pair: tuple[int, int]) -> float:
    """Return $-per-$10 dividend for a pair in a pool, or 0.0 if missed."""
    target = frozenset(pair)
    for race in (div_doc or {}).get("races", []):
        if int(race.get("race_number") or 0) != int(race_no):
            continue
        for div in race.get("dividends", []):
            if div.get("pool") != pool:
                continue
            try:
                got = frozenset(int(x) for x in str(div["combination"]).split(",")
                                 if x.strip().isdigit())
            except Exception:
                continue
            if got == target:
                return float(div["dividend_per_10"])
    return 0.0


def settle_all_up_ticket(ticket: dict, date_compact: str) -> dict:
    """Compute total return for an all-up ticket.

    Per-leg payout multiplier = dividend/10 if leg hits, else 0.
    Each subset S of legs returns stake_per_unit * Π_{leg in S} mult[leg].
    """
    if not ticket.get("valid"):
        return {"settled": False, "stake": 0.0, "return": 0.0,
                "pnl": 0.0, "hits": []}
    div_doc = _read_dividends(date_compact)
    if not div_doc:
        return {"settled": False, "stake": ticket["total_stake"],
                "return": 0.0, "pnl": -ticket["total_stake"], "hits": []}
    pool = ticket["pool"]
    legs = ticket["legs"]
    mults: list[float] = []
    hits: list[dict] = []
    for lg in legs:
        d = _race_div(div_doc, lg["race_no"], pool, lg["pair"])
        m = d / 10.0 if d > 0 else 0.0
        mults.append(m)
        hits.append({"race_no": lg["race_no"], "pair": list(lg["pair"]),
                     "dividend": d, "hit": m > 0})
    spu = float(ticket["stake_per_unit"])
    total_ret = 0.0
    for k in ticket["sizes"]:
        for idx in combinations(range(ticket["n_legs"]), k):
            unit_mult = 1.0
            for i in idx:
                unit_mult *= mults[i]
            total_ret += spu * unit_mult
    stake = ticket["total_stake"]
    return {
        "settled": True,
        "stake": float(stake),
        "return": float(total_ret),
        "pnl": float(total_ret - stake),
        "hits": hits,
        "n_hits": sum(1 for h in hits if h["hit"]),
    }


# ─────────────────────────────────────────────────────────────────────
# Pair selection: blend model rank with market for better banker calib
# ─────────────────────────────────────────────────────────────────────
def blended_pair(picks: list[dict],
                 edge_rows: list[dict] | None = None,
                 mode: str = "blend",
                 ) -> tuple[int, int] | None:
    """Return the (h1, h2) pair to use for a leg.

    mode='model'   — use raw model rank-1 + rank-2
    mode='market'  — use market favourite + 2nd favourite (from edge_rows)
    mode='blend'   — 50/50 blend of model win_prob & market p_market

    April-2026 calibration:
      • model rank-1 top-2 strike: 36.4%   (Brier penalty implies miscal)
      • market favourite top-2:    39.7%
      • blend should pick up the better of the two on consensus + still
        catch model overlays.
    """
    if not picks:
        return None
    by_horse = {int(p["horse_no"]): p for p in picks
                if p.get("horse_no") is not None}

    if mode == "model":
        ranked = sorted(by_horse.values(),
                         key=lambda p: int(p.get("rank") or 99))
        if len(ranked) >= 2:
            return (int(ranked[0]["horse_no"]), int(ranked[1]["horse_no"]))
        return None

    edges = {int(r["horse_no"]): r for r in (edge_rows or [])
             if r.get("horse_no") is not None}

    if mode == "market":
        ranked = sorted(edges.values(),
                         key=lambda r: -float(r.get("p_market") or 0))
        if len(ranked) >= 2:
            return (int(ranked[0]["horse_no"]), int(ranked[1]["horse_no"]))
        return None

    # blend
    scores: dict[int, float] = {}
    for h, p in by_horse.items():
        wp = float(p.get("win_prob") or 0)
        # win_prob in this codebase is on 0-100 scale per Brier check
        if wp > 1.0:
            wp = wp / 100.0
        pm = float(edges.get(h, {}).get("p_market") or wp)
        scores[h] = 0.5 * wp + 0.5 * pm
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if len(ranked) >= 2:
        return (int(ranked[0][0]), int(ranked[1][0]))
    return None
