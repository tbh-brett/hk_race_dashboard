"""
allup.py — HKJC All-Up cross-race chain builder + backtest
============================================================

Phase D. Implements the bettor-built All-Up structure described at:
    https://special.hkjc.com/e-win/en-US/betting-info/racing/beginners-guide/all-up-betting/

Mechanics
---------
An "all-up" is a chain of N legs across consecutive races. The user stakes
$X once, up front. After each winning leg, the *entire* return is rolled
forward as the stake for the next leg. If any leg loses, the whole chain
is dead. (HKJC also offers exotic "all-up" formulas like 4-of-5 etc.; we
implement the standard "win-all" variant which is the cleanest case for
geometric growth analysis. The "any-N-of-M" variants can be added later.)

Per-leg EV
----------
Each leg has an independent gross-return multiplier `g_i` on $1 staked:
    g_i = p_i * (dividend_$ / stake_$)        if leg hits
        = 0                                     otherwise
    E[g_i] = p_i * O_i                          (decimal odds × prob)

For a chain to have positive EV:
    E[∏ g_i] = ∏ E[g_i] > 1
              (legs are independent across races, so expectations factor)

Variance explodes faster than expectation, so we apply hard caps:
    - chain stake ≤ MODE max_per_meeting_frac × bankroll, AND
    - chain stake ≤ ALLUP_MAX_FRAC × bankroll      (additional ceiling)
    - chain length 2-4 legs (5+ adds variance with diminishing EV gain)
    - per-leg p_used ≥ MIN_LEG_P (don't chain weak signals)
    - per-leg edge_pct ≥ MIN_LEG_EDGE
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from itertools import combinations
from typing import Optional


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ALLUP_CFG = {
    "min_legs":         2,
    "max_legs":         4,
    "min_leg_p":        0.18,    # don't chain weak signals
    "min_leg_edge":     0.08,    # 8% edge per leg
    "min_chain_ev":     1.05,    # chain expected gross return ≥ 1.05
    "allup_max_frac":   0.05,    # 5% bankroll per chain (additional ceiling)
    "allowed_pools":    {"WIN", "PLACE", "QIN_BANKER", "QPL_BANKER"},
}

# UNCAPPED variant — used when mode == "uncapped". Strips floors, allows
# longer chains and bigger allocation. We still need ≥0 EV per leg
# (negative-EV chains are mathematically dominated by skipping).
ALLUP_CFG_UNCAPPED = {
    "min_legs":         2,
    "max_legs":         6,        # HKJC supports up to 6-leg all-ups (6UP)
    "min_leg_p":        0.0,
    "min_leg_edge":     0.0,
    "min_chain_ev":     1.00,     # any non-negative-EV chain is in
    "allup_max_frac":   0.25,     # 25% per chain
    "allowed_pools":    {"WIN", "PLACE", "QIN_BANKER", "QPL_BANKER"},
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class AllUpLeg:
    race_number: int
    horse_no: int                # for QIN_BANKER/QPL_BANKER this is the banker
    horse_name: str
    pool: str                    # "WIN" | "PLACE" | "QIN_BANKER" | "QPL_BANKER"
    p_used: float                # post-shrinkage P(leg hits)
    decimal_odds: float          # expected gross-return-per-$1 multiplier on hit
    edge_pct: float
    ev: float                    # = p_used * decimal_odds (gross expectation)
    # banker-style legs only:
    leg_horses: list = field(default_factory=list)
    leg_horse_names: list = field(default_factory=list)


@dataclass
class AllUpChain:
    legs: list[AllUpLeg]
    stake_hkd: float
    chain_ev: float              # ∏ leg.ev   (gross)
    chain_p_hit: float           # ∏ leg.p_used
    chain_edge_pct: float        # chain_ev - 1
    expected_payout: float       # stake * chain_ev
    expected_pnl: float          # expected_payout - stake
    reason: str

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# Chain builder
# ---------------------------------------------------------------------------
def build_chains(
    candidate_legs: list[AllUpLeg],
    *,
    bankroll: float,
    mode_max_frac: float,
    cfg: dict = ALLUP_CFG,
) -> list[AllUpChain]:
    """Enumerate viable all-up chains from a per-race candidate list.

    `candidate_legs` should already be filtered (one leg per race max — the
    caller chooses the best leg per race; we don't combine legs within the
    same race because that violates the all-up structure).

    Returns the chains sorted by `chain_ev` descending. Caller picks 1
    (typically the best by EV) or the top K satellites.
    """
    legs = [l for l in candidate_legs
             if l.pool in cfg["allowed_pools"]
             and l.p_used >= cfg["min_leg_p"]
             and l.edge_pct >= cfg["min_leg_edge"]]
    # Ensure at most one leg per race
    by_race: dict[int, AllUpLeg] = {}
    for l in legs:
        cur = by_race.get(l.race_number)
        if cur is None or l.ev > cur.ev:
            by_race[l.race_number] = l
    legs = sorted(by_race.values(), key=lambda l: l.race_number)
    if len(legs) < cfg["min_legs"]:
        return []

    cap = min(mode_max_frac, cfg["allup_max_frac"]) * bankroll
    cap = max(10.0, (cap // 10.0) * 10.0)   # quantize to $10

    chains: list[AllUpChain] = []
    for n in range(cfg["min_legs"], min(cfg["max_legs"], len(legs)) + 1):
        for combo in combinations(legs, n):
            ev = 1.0
            p_hit = 1.0
            for l in combo:
                ev *= l.ev
                p_hit *= l.p_used
            if ev < cfg["min_chain_ev"]:
                continue
            stake = cap   # full ceiling on accepted chain
            chains.append(AllUpChain(
                legs=list(combo),
                stake_hkd=stake,
                chain_ev=round(ev, 3),
                chain_p_hit=round(p_hit, 4),
                chain_edge_pct=round(ev - 1.0, 3),
                expected_payout=round(stake * ev, 1),
                expected_pnl=round(stake * (ev - 1.0), 1),
                reason=f"{n}-leg chain · p_hit {p_hit:.1%} · ev {ev:.2f}",
            ))
    chains.sort(key=lambda c: c.chain_ev, reverse=True)
    return chains


# ---------------------------------------------------------------------------
# Backtest: simulate a chain against real per-race dividends
# ---------------------------------------------------------------------------
def settle_chain(
    chain: AllUpChain,
    *,
    results_by_race: dict,        # {race_no: {horse_no: {"place": int, ...}}}
    dividends_by_race: dict,      # {race_no: {pool: {frozenset({nos}): div_per_10}}}
) -> dict:
    """Walk the chain leg-by-leg using REAL dividends. Return P&L.

    Each leg's "multiplier" is computed for $1 of stake going in:
        WIN          : div_per_10/10                if banker = 1st
        PLACE        : div_per_10/10                if banker top-3
        QIN_BANKER   : Σ div(banker, leg_i)/10 / N  if banker top-2 AND any
                        leg_i top-2 (stake split equally across N combos)
        QPL_BANKER   : Σ div(banker, leg_i)/10 / N  if banker top-3 AND any
                        leg_i top-3 (stake split equally across N combos)
    """
    running = chain.stake_hkd
    leg_outcomes: list[dict] = []
    for l in chain.legs:
        rb = results_by_race.get(l.race_number, {})
        finishers = sorted(
            ((no, r["place"]) for no, r in rb.items() if r.get("place") is not None),
            key=lambda kv: kv[1],
        )
        winners = {no for no, p in finishers if p == 1}
        top2    = {no for no, p in finishers[:2]}
        top3    = {no for no, p in finishers[:3]}

        divs = dividends_by_race.get(l.race_number, {})
        mult = 0.0   # gross return multiplier per $1 staked on this leg
        hit = False

        if l.pool == "WIN":
            if l.horse_no in winners:
                d = divs.get("WIN", {}).get(frozenset({l.horse_no}), 0.0)
                if d > 0:
                    mult = d / 10.0
                    hit = True
        elif l.pool == "PLACE":
            if l.horse_no in top3:
                d = divs.get("PLACE", {}).get(frozenset({l.horse_no}), 0.0)
                if d > 0:
                    mult = d / 10.0
                    hit = True
        elif l.pool in ("QIN_BANKER", "QPL_BANKER"):
            pool_key = "QIN" if l.pool == "QIN_BANKER" else "QPL"
            field_set = top2 if l.pool == "QIN_BANKER" else top3
            banker_in = (l.horse_no in field_set)
            n_legs = max(1, len(l.leg_horses))
            if banker_in:
                for leg_no in l.leg_horses:
                    if leg_no == l.horse_no:
                        continue
                    if leg_no in field_set:
                        d = divs.get(pool_key, {}).get(
                            frozenset({l.horse_no, leg_no}), 0.0)
                        if d > 0:
                            # stake split equally across N combos
                            mult += (d / 10.0) / n_legs
                            hit = True
        else:
            # Unknown pool — treat as miss
            pass

        if not hit or mult <= 0:
            leg_outcomes.append({"race": l.race_number, "pool": l.pool,
                                  "hit": False, "running": 0.0})
            return {"settled": True, "stake": chain.stake_hkd,
                    "payout": 0.0, "pnl": -chain.stake_hkd,
                    "legs": leg_outcomes, "broken_at": l.race_number}

        running = running * mult
        leg_outcomes.append({"race": l.race_number, "pool": l.pool,
                              "hit": True, "running": round(running, 2),
                              "multiplier": round(mult, 4)})

    return {"settled": True, "stake": chain.stake_hkd,
            "payout": round(running, 2),
            "pnl": round(running - chain.stake_hkd, 2),
            "legs": leg_outcomes, "broken_at": None}


# ---------------------------------------------------------------------------
# Convenience: convert a `decision_engine` ticket list into AllUpLegs
# ---------------------------------------------------------------------------
def candidate_legs_from_tickets(
    tickets: list[dict],
    *,
    p_market_lookup: dict,        # {(race_no, horse_no): p_market}
    n_observed: int = 0,
    shrinkage_k: int = 50,
) -> list[AllUpLeg]:
    """Extract WIN/PLACE candidate legs from per-race tickets.

    A ticket here is the dict produced by `betting_strategy.build_model_ticket`.
    We take the top composite runner whenever its play is WIN/QIN/QPL — for
    all-up we always use a WIN or PLACE leg (the simplest structure).
    """
    from kelly import shrink_probability
    legs: list[AllUpLeg] = []
    for t in tickets:
        rn = t["race_number"]
        ticket = t["ticket"]
        b = ticket.get("banker") or {}
        if not b:
            continue
        sp = b.get("win_odds")
        p_model = b.get("p_model")
        p_market = p_market_lookup.get((rn, b["horse_no"]))
        if not (sp and p_model and p_market):
            continue
        p_used = shrink_probability(p_model, p_market,
                                       n_observed=n_observed,
                                       k_prior=shrinkage_k)
        # Two candidate legs per banker: WIN @ SP, PLACE at conservative ~0.4*SP+1
        edge_pct = (p_used / p_market) - 1.0
        win_ev = p_used * sp
        legs.append(AllUpLeg(
            race_number=rn, horse_no=b["horse_no"],
            horse_name=b["horse_name"], pool="WIN",
            p_used=round(p_used, 4), decimal_odds=sp,
            edge_pct=round(edge_pct, 4), ev=round(win_ev, 4),
        ))
        # PLACE: rough proxy assuming PLACE pays ~1/3 of WIN dividend with
        # ~3x the hit probability. Caller can supply real PLACE odds via
        # candidate_legs_from_tickets when available.
        place_p = min(0.95, p_used * 2.5)
        place_o = max(1.05, sp * 0.35)
        place_ev = place_p * place_o
        place_edge = (place_p / max(0.01, min(0.99, p_market * 2.5))) - 1.0
        legs.append(AllUpLeg(
            race_number=rn, horse_no=b["horse_no"],
            horse_name=b["horse_name"], pool="PLACE",
            p_used=round(place_p, 4), decimal_odds=round(place_o, 2),
            edge_pct=round(place_edge, 4), ev=round(place_ev, 4),
        ))
    return legs


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    legs = [
        AllUpLeg(1, 3, "Horse A", "WIN", p_used=0.30, decimal_odds=4.0,
                  edge_pct=0.15, ev=0.30 * 4.0),
        AllUpLeg(2, 7, "Horse B", "WIN", p_used=0.25, decimal_odds=5.0,
                  edge_pct=0.18, ev=0.25 * 5.0),
        AllUpLeg(3, 1, "Horse C", "PLACE", p_used=0.55, decimal_odds=2.2,
                  edge_pct=0.10, ev=0.55 * 2.2),
        AllUpLeg(4, 9, "Horse D", "WIN", p_used=0.22, decimal_odds=5.5,
                  edge_pct=0.12, ev=0.22 * 5.5),
    ]
    chains = build_chains(legs, bankroll=1000, mode_max_frac=0.15)
    for c in chains[:5]:
        print(f"{len(c.legs)}-leg  ev={c.chain_ev:.2f}  p_hit={c.chain_p_hit:.1%}  "
              f"stake=${c.stake_hkd:.0f}  E[pnl]=${c.expected_pnl:+.0f}  "
              f"({'+'.join(f'R{l.race_number}' for l in c.legs)})")
