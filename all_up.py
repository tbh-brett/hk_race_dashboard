"""all_up.py — HKJC-spec All-Up (cross-race parlay) builder + analytics.

Pools supported
---------------
WIN, PLACE                : selection = single horse number (int)
QIN, QPL                  : selection = pair (int, int)
QIN+QPL                   : selection = pair (int, int) — leg wins only if the
                            pair finishes 1st-2nd (Quinella); pays BOTH the
                            QIN and QPL dividends (product). Low hit, big payout.

A leg has a list of selections. The total ticket-unit count is

    base_units(shape) * Π (len(selections_i))

Settlement enumerates the cartesian product of one selection per leg and,
for each subset S of legs whose size is a parlayed size, multiplies the
leg dividends/10 if the chosen selections of legs in S are all winners.

Public API
----------
    SHAPE_PRESETS                                — HKJC preset shapes
    POOLS                                        — supported pool list
    units_for_shape(n_legs, sizes)               — base shape units
    build_all_up_ticket(...)                     — dict (no settlement)
    settle_all_up_ticket(ticket, date_compact)   — dict (return + hits)
    blended_pair(picks, edge_rows, mode)         — (h1,h2)
    horse_prob(pick, edge_row, mode)             — float in [0,1]
    leg_hit_prob(leg, pool, prob_lookup)         — float
    evaluate_ticket(ticket, prob_lookup, payout_lookup)  — EV / variance
    compare_shapes(legs, pool, prob_lookup, …)   — list of shape rows
    recommend_legs(...)                          — ranked legs
    kelly_fraction(p_hit, payout_mult)           — float
"""
from __future__ import annotations

import json
import math
from itertools import combinations, product
from math import comb
from pathlib import Path
from typing import Iterable

REPORTS = Path(__file__).parent / "reports"

POOLS = ("WIN", "PLACE", "QIN", "QPL", "QIN+QPL")
PAIR_POOLS = {"QIN", "QPL", "QIN+QPL"}
SINGLE_POOLS = {"WIN", "PLACE"}

SHAPE_PRESETS: dict[str, tuple[int, frozenset[int]]] = {
    "2x1":  (2, frozenset({2})),
    "2x3":  (2, frozenset({1, 2})),
    "3x1":  (3, frozenset({3})),
    "3x3":  (3, frozenset({2})),
    "3x4":  (3, frozenset({2, 3})),
    "3x6":  (3, frozenset({1, 2})),
    "3x7":  (3, frozenset({1, 2, 3})),
    "4x1":  (4, frozenset({4})),
    "4x4":  (4, frozenset({3})),
    "4x5":  (4, frozenset({3, 4})),
    "4x6":  (4, frozenset({2})),
    "4x10": (4, frozenset({1, 2})),
    "4x11": (4, frozenset({2, 3, 4})),
    "4x14": (4, frozenset({1, 2, 3})),
    "4x15": (4, frozenset({1, 2, 3, 4})),
    "5x1":  (5, frozenset({5})),
    "5x6":  (5, frozenset({4, 5})),
    "5x16": (5, frozenset({3, 4, 5})),
    "5x26": (5, frozenset({2, 3, 4, 5})),
    "5x31": (5, frozenset({1, 2, 3, 4, 5})),
    "6x1":  (6, frozenset({6})),
    "6x7":  (6, frozenset({5, 6})),
    "6x22": (6, frozenset({4, 5, 6})),
    "6x42": (6, frozenset({3, 4, 5, 6})),
    "6x57": (6, frozenset({2, 3, 4, 5, 6})),
    "6x63": (6, frozenset({1, 2, 3, 4, 5, 6})),
}


# ───────────────────────── selection / leg utilities ────────────────────
def _normalise_selection(sel, pool: str):
    if pool in PAIR_POOLS:
        if isinstance(sel, (list, tuple)) and len(sel) == 2:
            a, b = int(sel[0]), int(sel[1])
            if a == b:
                raise ValueError(f"pair contains duplicate horse {a}")
            return (min(a, b), max(a, b))
        raise ValueError(f"{pool} selection must be (h1,h2): got {sel!r}")
    if isinstance(sel, (list, tuple)):
        if len(sel) != 1:
            raise ValueError(f"{pool} selection must be a single horse: got {sel!r}")
        return int(sel[0])
    return int(sel)


def _normalise_leg(leg: dict, pool: str) -> dict:
    out = {"race_no": int(leg["race_no"])}
    sels = leg.get("selections")
    if sels is None and "pair" in leg:
        sels = [leg["pair"]]
    if not sels:
        raise ValueError(f"leg R{out['race_no']} has no selections")
    out["selections"] = [_normalise_selection(s, pool) for s in sels]
    return out


def _selection_multiplier(legs: list[dict]) -> int:
    m = 1
    for lg in legs:
        m *= max(1, len(lg["selections"]))
    return m


# ───────────────────────────── builder ──────────────────────────────────
def units_for_shape(n_legs: int, sizes: Iterable[int]) -> int:
    return sum(comb(n_legs, k) for k in sizes if 1 <= k <= n_legs)


def build_all_up_ticket(*, legs: list[dict], pool: str,
                         sizes: Iterable[int],
                         stake_per_unit: float = 1.0) -> dict:
    pool = str(pool).upper()
    if pool not in POOLS:
        return {"valid": False, "reason": f"unknown pool {pool}"}
    n = len(legs)
    sizes = sorted({int(k) for k in sizes if 1 <= int(k) <= n})
    if n < 2 or not sizes:
        return {"valid": False, "reason": "need >=2 legs and >=1 subset size"}
    try:
        norm_legs = [_normalise_leg(lg, pool) for lg in legs]
    except ValueError as e:
        return {"valid": False, "reason": str(e)}
    base_units = units_for_shape(n, sizes)
    sel_mult = _selection_multiplier(norm_legs)
    units = base_units * sel_mult
    return {
        "valid": True,
        "pool": pool,
        "legs": norm_legs,
        "sizes": sizes,
        "n_legs": n,
        "base_units": int(base_units),
        "selection_multiplier": int(sel_mult),
        "n_units": int(units),
        "stake_per_unit": float(stake_per_unit),
        "total_stake": float(units * stake_per_unit),
        "shape_label": _shape_label(n, sizes),
    }


def _shape_label(n: int, sizes: list[int]) -> str:
    for lbl, (nn, ss) in SHAPE_PRESETS.items():
        if nn == n and frozenset(sizes) == ss:
            return lbl
    return f"{n}x{units_for_shape(n, sizes)}"


# ─────────────────────── dividend lookup + settlement ───────────────────
def _read_dividends(date_compact: str) -> dict:
    p = REPORTS / f"dividends_{date_compact}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _race_div(div_doc: dict, race_no: int, pool: str, sel) -> float:
    # "QIN+QPL" combined leg: the pair must win the Quinella (finish 1st-2nd),
    # which also wins the Quinella Place. The leg pays BOTH dividends, re-staked
    # as a product → dividend_per_10 = (QIN_div/10)·(QPL_div/10)·10.
    if pool == "QIN+QPL":
        qin = _race_div(div_doc, race_no, "QIN", sel)
        qpl = _race_div(div_doc, race_no, "QPL", sel)
        if qin > 0 and qpl > 0:
            return qin * qpl / 10.0
        return 0.0
    if isinstance(sel, tuple):
        target = frozenset(int(x) for x in sel)
    else:
        target = frozenset({int(sel)})
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
    if not ticket.get("valid"):
        return {"settled": False, "stake": 0.0, "return": 0.0,
                "pnl": 0.0, "hits": []}
    div_doc = _read_dividends(date_compact)
    if not div_doc:
        return {"settled": False, "stake": ticket["total_stake"],
                "return": 0.0, "pnl": -ticket["total_stake"], "hits": []}
    pool = ticket["pool"]
    legs = ticket["legs"]
    spu = float(ticket["stake_per_unit"])
    leg_mults: list[list[float]] = []
    leg_hits: list[dict] = []
    for lg in legs:
        per_sel = []
        any_hit = False
        for sel in lg["selections"]:
            d = _race_div(div_doc, lg["race_no"], pool, sel)
            m = d / 10.0 if d > 0 else 0.0
            per_sel.append(m)
            if m > 0:
                any_hit = True
        leg_mults.append(per_sel)
        leg_hits.append({
            "race_no": lg["race_no"],
            "selections": [list(s) if isinstance(s, tuple) else s
                            for s in lg["selections"]],
            "hit": any_hit,
        })
    total_ret = 0.0
    n = ticket["n_legs"]
    for combo in product(*[range(len(m)) for m in leg_mults]):
        for k in ticket["sizes"]:
            for idx in combinations(range(n), k):
                m = 1.0
                for i in idx:
                    m *= leg_mults[i][combo[i]]
                if m > 0:
                    total_ret += spu * m
    stake = ticket["total_stake"]
    return {
        "settled": True,
        "stake": float(stake),
        "return": float(total_ret),
        "pnl": float(total_ret - stake),
        "hits": leg_hits,
        "n_hits": sum(1 for h in leg_hits if h["hit"]),
    }


# ─────────────────────── probability helpers ─────────────────────────────
def sarr_score(pick: dict, *, pt_z: float = 0.0) -> float:
    """SARR composite score (higher = stronger).

    pt_z is the z-scored projected_time within the race (caller computes).
    Combines:
      pt_z                 : speed (race-relative, primary signal)
      +early_speed_z       : ESZ (ρ=0.516 with finish position)
      −sec_total_adj       : sectional adjustment (s, lower better)
      −smap_total_adj      : speedmap fit
      −effective_resid     : negative residual = faster than expected
    """
    if not pick:
        return -999.0
    esz  = float(pick.get("early_speed_z") or 0)
    sec  = float(pick.get("sec_total_adj") or 0)
    smap = float(pick.get("smap_total_adj") or 0)
    eres = float(pick.get("effective_resid") or 0)
    return (1.2 * pt_z
            + 0.8 * esz
            - 1.0 * sec
            - 0.6 * smap
            - 0.7 * eres)


def sarr_probs(picks: list[dict], *, temperature: float = 2.2) -> dict[int, float]:
    """Softmax SARR scores → win-probabilities per horse for one race.

    Projected_time is z-scored within race so the scoring scales correctly
    across 1000–2400m distances.
    """
    if not picks:
        return {}
    pts = [float(p.get("projected_time") or p.get("proj_pre_pace") or 0)
            for p in picks]
    pts_valid = [t for t in pts if t > 0]
    if pts_valid:
        mean = sum(pts_valid) / len(pts_valid)
        var = sum((t - mean) ** 2 for t in pts_valid) / len(pts_valid)
        std = math.sqrt(var) or 1.0
    else:
        mean, std = 0.0, 1.0
    scored = []
    for p in picks:
        if p.get("horse_no") is None:
            continue
        pt = float(p.get("projected_time") or p.get("proj_pre_pace") or 0)
        pt_z = -(pt - mean) / std if pt > 0 else 0.0
        scored.append((int(p["horse_no"]), sarr_score(p, pt_z=pt_z)))
    if not scored:
        return {}
    smax = max(s for _, s in scored)
    exps = [(h, math.exp((s - smax) / max(0.05, temperature)))
             for h, s in scored]
    z = sum(e for _, e in exps) or 1.0
    return {h: e / z for h, e in exps}


def horse_prob(pick: dict | None, edge_row: dict | None,
                mode: str = "blend",
                sarr_p: float | None = None) -> float:
    """Single-horse WIN probability in [0,1].

    mode='market'  — market-implied probability (uses p_market)
    mode='model'   — SARR-derived probability (caller passes sarr_p)
    mode='blend'   — 50/50 SARR + market
    """
    pm = 0.0
    if edge_row is not None:
        pm = float(edge_row.get("p_market") or 0)
    sp = float(sarr_p) if sarr_p is not None else None
    if sp is None and pick is not None:
        # fallback: use stored win_prob (handle 0-100 vs 0-1 ambiguity)
        wp = float(pick.get("win_prob") or 0)
        sp = wp / 100.0 if wp > 1.0 else wp
    sp = sp or 0.0
    if mode == "model":
        return max(0.0, min(1.0, sp))
    if mode == "market":
        return max(0.0, min(1.0, pm or sp))
    if pm <= 0:
        return sp
    if sp <= 0:
        return pm
    return max(0.0, min(1.0, 0.5 * sp + 0.5 * pm))


def _place_prob(p_win: float) -> float:
    if p_win <= 0:
        return 0.0
    if p_win >= 0.5:
        return min(0.97, 0.55 + p_win * 0.7)
    return min(0.95, p_win * 2.6)


def _pair_prob(p1: float, p2: float, *, pool: str) -> float:
    if p1 <= 0 or p2 <= 0:
        return 0.0
    if pool == "QIN":
        a = (p1 * p2 / (1 - p1)) if p1 < 0.999 else p2
        b = (p2 * p1 / (1 - p2)) if p2 < 0.999 else p1
        return max(0.0, min(0.95, a + b))
    if pool == "QPL":
        pl1 = _place_prob(p1)
        pl2 = _place_prob(p2)
        return max(0.0, min(0.97, 0.92 * pl1 * pl2 + 0.06 * min(pl1, pl2)))
    if pool == "QIN+QPL":
        # The pair must finish 1st & 2nd (Quinella). That outcome also
        # satisfies the Quinella Place (both in the top 3), so QIN ⟹ QPL and
        # the binding event is the Quinella: P(QIN and QPL) = P(QIN). The
        # payout, however, multiplies BOTH dividends (see _race_div), giving a
        # low-hit / jackpot-payout leg.
        return _pair_prob(p1, p2, pool="QIN")
    raise ValueError(f"_pair_prob unsupported pool {pool}")


def leg_hit_prob(leg: dict, *, pool: str, prob_lookup) -> float:
    sels = leg["selections"]
    if pool in SINGLE_POOLS:
        return max(0.0, min(0.99, sum(prob_lookup(leg["race_no"], s)
                                          for s in sels)))
    surv = 1.0
    for s in sels:
        p = prob_lookup(leg["race_no"], s)
        surv *= (1 - p)
    return max(0.0, min(0.99, 1 - surv))


# ─────────────────────── ticket evaluation: EV ───────────────────────────
def evaluate_ticket(ticket: dict, *, prob_lookup, payout_lookup) -> dict:
    if not ticket.get("valid"):
        return {"valid": False}
    pool = ticket["pool"]
    legs = ticket["legs"]
    n = ticket["n_legs"]
    spu = ticket["stake_per_unit"]
    leg_p: list[float] = []
    leg_pay: list[float] = []
    for lg in legs:
        p_total = leg_hit_prob(lg, pool=pool, prob_lookup=prob_lookup)
        num = den = 0.0
        for s in lg["selections"]:
            p = prob_lookup(lg["race_no"], s)
            num += p * payout_lookup(lg["race_no"], s)
            den += p
        leg_p.append(p_total)
        leg_pay.append((num / den) if den > 0 else 0.0)
    p_full = 1.0
    pay_full = 1.0
    for p, m in zip(leg_p, leg_pay):
        p_full *= p
        pay_full *= m
    expected_return = 0.0
    var_proxy = 0.0
    for k in ticket["sizes"]:
        for idx in combinations(range(n), k):
            p = 1.0
            payoff = 1.0
            for i in idx:
                p *= leg_p[i]
                payoff *= leg_pay[i]
            expected_return += spu * p * payoff
            var_proxy += (spu * payoff) ** 2 * p * (1 - p)
    sel_mult = ticket["selection_multiplier"]
    expected_return *= sel_mult
    var_proxy *= sel_mult
    stake = ticket["total_stake"]
    ev = expected_return - stake
    roi = (ev / stake) if stake > 0 else 0.0
    return {
        "valid": True,
        "stake": stake,
        "expected_return": float(expected_return),
        "ev": float(ev),
        "roi": float(roi),
        "p_full_parlay": float(p_full),
        "expected_full_payout": float(pay_full),
        "leg_p": [float(x) for x in leg_p],
        "leg_payout": [float(x) for x in leg_pay],
        "var_proxy": float(var_proxy),
        "sharpe_proxy": float(ev / math.sqrt(var_proxy)) if var_proxy > 0 else 0.0,
    }


def kelly_fraction(p: float, b: float) -> float:
    """f* = (b*p - q)/b, capped at quarter-Kelly (0.25)."""
    if b <= 0 or p <= 0:
        return 0.0
    q = 1.0 - p
    f = (b * p - q) / b
    return max(0.0, min(0.25, f))


# ───────────────────────── pair / leg recommenders ──────────────────────
def blended_pair(picks: list[dict],
                 edge_rows: list[dict] | None = None,
                 mode: str = "blend") -> tuple[int, int] | None:
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
    scores: dict[int, float] = {}
    for h, p in by_horse.items():
        wp = float(p.get("win_prob") or 0)
        if wp > 1.0:
            wp = wp / 100.0
        pm = float(edges.get(h, {}).get("p_market") or wp)
        scores[h] = 0.5 * wp + 0.5 * pm
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if len(ranked) >= 2:
        return (int(ranked[0][0]), int(ranked[1][0]))
    return None


def rank_horses(picks: list[dict], edges: list[dict] | None,
                 mode: str = "blend") -> list[tuple[int, float]]:
    by_p = {int(p["horse_no"]): p for p in picks
             if p.get("horse_no") is not None}
    by_e = {int(r["horse_no"]): r for r in (edges or [])
             if r.get("horse_no") is not None}
    sp_map = sarr_probs(picks)
    horses = set(by_p.keys()) | set(by_e.keys())
    out = [(h, horse_prob(by_p.get(h), by_e.get(h), mode,
                            sarr_p=sp_map.get(h))) for h in horses]
    out.sort(key=lambda kv: -kv[1])
    return out


def prob_for_pool(race_no: int, sel, *, pool: str,
                   pick_lookup: dict, edge_lookup: dict,
                   mode: str = "blend",
                   sarr_lookup: dict | None = None) -> float:
    sl = sarr_lookup or {}
    if pool == "WIN":
        return horse_prob(pick_lookup.get(int(sel)),
                           edge_lookup.get(int(sel)), mode,
                           sarr_p=sl.get(int(sel)))
    if pool == "PLACE":
        return _place_prob(horse_prob(pick_lookup.get(int(sel)),
                                         edge_lookup.get(int(sel)), mode,
                                         sarr_p=sl.get(int(sel))))
    a, b = int(sel[0]), int(sel[1])
    pa = horse_prob(pick_lookup.get(a), edge_lookup.get(a), mode,
                     sarr_p=sl.get(a))
    pb = horse_prob(pick_lookup.get(b), edge_lookup.get(b), mode,
                     sarr_p=sl.get(b))
    return _pair_prob(pa, pb, pool=pool)


def _exp_payout_singles(horses: list[int], pool: str,
                         edge_lookup: dict) -> float:
    if not horses:
        return 0.0
    pays = []
    for h in horses:
        r = edge_lookup.get(h)
        if not r:
            continue
        odds = float(r.get("win_odds") or 0)
        if odds <= 1.0:
            continue
        pays.append(odds if pool == "WIN" else odds / 3.5)
    return (sum(pays) / len(pays)) if pays else 0.0


def recommend_legs(*, races_by_no: dict[int, dict],
                    edges_by_race: dict[int, list[dict]],
                    pool: str, n_legs: int,
                    mode: str = "blend",
                    sel_per_leg: int = 1) -> list[dict]:
    rows = []
    for rn, race in races_by_no.items():
        picks = race.get("picks") or []
        edges = edges_by_race.get(rn, [])
        ranked = rank_horses(picks, edges, mode)
        if not ranked:
            continue
        edge_lookup = {int(r["horse_no"]): r for r in edges
                        if r.get("horse_no") is not None}
        pick_lookup = {int(p["horse_no"]): p for p in picks
                        if p.get("horse_no") is not None}
        if pool in SINGLE_POOLS:
            top = [h for h, _ in ranked[:max(1, sel_per_leg)]]
            sels = top
            p_hit = sum(prob_for_pool(rn, s, pool=pool,
                                         pick_lookup=pick_lookup,
                                         edge_lookup=edge_lookup,
                                         mode=mode) for s in top)
            p_hit = min(0.99, p_hit)
            exp_pay = _exp_payout_singles(top, pool, edge_lookup)
        else:
            top1 = ranked[0][0]
            partners = [h for h, _ in ranked[1:1 + max(1, sel_per_leg)]]
            sels = [(min(top1, p), max(top1, p)) for p in partners]
            surv = 1.0
            for (a, b) in sels:
                pa = horse_prob(pick_lookup.get(a), edge_lookup.get(a), mode)
                pb = horse_prob(pick_lookup.get(b), edge_lookup.get(b), mode)
                surv *= (1 - _pair_prob(pa, pb, pool=pool))
            p_hit = 1 - surv
            exp_pay = 0.0
        rows.append({
            "race_no": rn,
            "selections": sels,
            "p_hit": float(p_hit),
            "exp_payout": float(exp_pay),
        })
    rows.sort(key=lambda r: -r["p_hit"])
    return rows[:n_legs]


# ─────────────────────────── shape comparison ───────────────────────────
def compare_shapes(*, legs: list[dict], pool: str,
                    prob_lookup, payout_lookup,
                    stake_per_unit: float = 1.0) -> list[dict]:
    n = len(legs)
    rows = []
    for label, (nn, sizes) in SHAPE_PRESETS.items():
        if nn != n:
            continue
        ticket = build_all_up_ticket(legs=legs, pool=pool, sizes=sizes,
                                      stake_per_unit=stake_per_unit)
        if not ticket.get("valid"):
            continue
        ev = evaluate_ticket(ticket, prob_lookup=prob_lookup,
                                payout_lookup=payout_lookup)
        rows.append({
            "shape": label,
            "stake": ticket["total_stake"],
            "n_units": ticket["n_units"],
            "ev": ev["ev"],
            "roi": ev["roi"],
            "p_full": ev["p_full_parlay"],
            "exp_full_pay": ev["expected_full_payout"],
            "sharpe": ev["sharpe_proxy"],
        })
    rows.sort(key=lambda r: -r["ev"])
    return rows
