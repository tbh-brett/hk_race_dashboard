"""
decision_engine.py — Top-level betting decision engine
========================================================

Phases C + F.

Ties together:
    - betting_strategy.build_meeting_tickets : per-race composite + ticket
    - market_belief.implied_basic            : market probability prior
    - kelly.size_bet                         : bankroll-aware staking
    - allup.build_chains                     : cross-race compounding satellite

Inputs
------
    - date (compact YYYYMMDD)
    - bankroll (HKD)
    - mode: "conservative" | "balanced" | "aggressive"
    - optional: enable/disable all-up satellite

Outputs
-------
    - JSON slate: per-race recommended bet, stake, EV, reason
    - Optional all-up chain plan (if mode allows)
    - Total exposure summary
    - "skip" verdict per race when nothing clears the edge floor

Backtest mode
-------------
When `--backtest` is passed, replays the engine on past meetings using
real dividends. Reports per-mode metrics via `bankroll.replay`.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from betting_strategy import (
    BASE, REPORTS, build_meeting_tickets, load_meeting,
    load_blackbook, load_factor_tables, _results_race_by_no,
)
from kelly import MODE_TABLE, size_bet, shrink_probability, HKJC_MIN_STAKE
from allup import (
    AllUpLeg, build_chains, settle_chain, ALLUP_CFG,
)
from bankroll import Bet, replay
from market_belief import implied_basic
from analyze_betting_edge import load_dividends, div_pay


# Map ticket "play" → kelly pool whitelist key + dividend pool for backtest
PLAY_TO_POOL = {
    "WIN":         "WIN",
    "PLACE":       "PLACE",
    "QIN_BANKER":  "QIN_BANKER",
    "QPL_BANKER":  "QPL_BANKER",
    "F4_BOX":      "F4_BOX",
    "QTT_BOX":     "QTT_BOX",
    "F4_BOX_TOP5": "F4_BOX",
}


# ---------------------------------------------------------------------------
# Probability + odds helpers
# ---------------------------------------------------------------------------
def race_market_probs(rows: list[dict]) -> dict[int, float]:
    """Compute de-overrounded p_market for each runner with a SP."""
    odds = {r["horse_no"]: r["win_odds"] for r in rows
            if r.get("win_odds") not in (None, 0)}
    return implied_basic(odds)


def estimate_decimal_odds_for_play(ticket: dict, divs_for_race: dict,
                                     fallback_sp: Optional[float]) -> Optional[float]:
    """For staking math we need a decimal-odds estimate.

    For WIN/PLACE we use SP (or actual dividend if backtesting).
    For QIN/QPL bankers we estimate (banker_SP * leg_SP) * takeout factor.
    For F4/QTT box we use a rough proxy: the geometric mean of leg SPs * 4.
    """
    play = ticket["play"]
    banker = ticket.get("banker") or {}
    legs   = ticket.get("legs") or []
    sp = banker.get("win_odds") or fallback_sp

    if play == "WIN":
        if divs_for_race:
            d = divs_for_race.get("WIN", {}).get(frozenset({banker["horse_no"]}))
            if d: return d / 10.0
        return sp
    if play == "PLACE":
        if divs_for_race:
            d = divs_for_race.get("PLACE", {}).get(frozenset({banker["horse_no"]}))
            if d: return d / 10.0
        # Crude fallback: place pays roughly 0.35 of WIN net odds + 1
        return max(1.05, (sp or 3.0) * 0.35) if sp else 1.5
    if play in ("QIN_BANKER", "QPL_BANKER"):
        if not legs or not sp:
            return None
        avg_leg_sp = sum((l.get("win_odds") or sp) for l in legs) / max(1, len(legs))
        # QIN proxy: SP_a * SP_b * 0.825 / 2  (17.5% takeout, dividend per 1 pair)
        # QPL proxy: same but /3 (3 possible pairs)
        denom = 2.0 if play == "QIN_BANKER" else 3.0
        return (sp * avg_leg_sp) * 0.825 / denom
    if play in ("F4_BOX", "QTT_BOX", "F4_BOX_TOP5"):
        # Very rough: top-4 SP product * takeout factor
        if not banker:
            return None
        sps = [sp] + [(l.get("win_odds") or sp) for l in legs[:3]]
        prod = 1.0
        for s in sps[:4]:
            prod *= max(1.0, s)
        return prod * 0.825
    return None


def estimate_p_for_play(ticket: dict, market_probs: dict[int, float]) -> tuple[float, float]:
    """Return (p_model, p_market) appropriate to the play.

    For WIN/QIN/QPL we use the banker's win prob.
    For PLACE we approximate: p_place ≈ min(0.95, 2.5 * p_win).
    For F4/QTT we use the joint top-4 model probability mass.
    """
    play = ticket["play"]
    banker = ticket.get("banker") or {}
    p_model = banker.get("p_model") or 0.0
    p_mkt = market_probs.get(banker.get("horse_no"), 0.0) if banker else 0.0
    if play == "PLACE":
        p_model = min(0.95, p_model * 2.5)
        p_mkt = min(0.95, p_mkt * 2.5) if p_mkt > 0 else p_mkt
    elif play in ("F4_BOX", "QTT_BOX", "F4_BOX_TOP5"):
        # joint-top-4 mass — much smaller than singleton win prob
        legs = ticket.get("legs") or []
        joint = 1.0
        for h in [banker] + legs[:3]:
            joint *= h.get("p_model", 0.0)
        p_model = joint
        joint_mkt = 1.0
        for h in [banker] + legs[:3]:
            joint_mkt *= market_probs.get(h.get("horse_no", -1), 0.0)
        p_mkt = joint_mkt
    return p_model, p_mkt


# ---------------------------------------------------------------------------
# All-up candidate-leg generators (used by build_meeting_slate)
# ---------------------------------------------------------------------------
def _emit_simple_legs(
    out: list, race_number: int, horse: dict, market_probs: dict,
    *, n_observed: int, shrinkage_k: int, is_uncapped: bool,
) -> None:
    """Append WIN + PLACE legs for one horse if positive-EV (or any-EV in uncapped)."""
    sp = horse.get("win_odds")
    p_model = horse.get("p_model")
    if not sp or p_model is None:
        return
    p_market = market_probs.get(horse["horse_no"], 0.0) or (1.0 / sp)
    p_used_w = shrink_probability(p_model, p_market,
                                    n_observed=n_observed, k_prior=shrinkage_k)
    if p_used_w <= 0:
        return
    win_ev = p_used_w * sp
    edge_w = (p_used_w / max(p_market, 0.001)) - 1.0
    # In non-uncapped modes only emit if WIN ev is at least break-even
    if win_ev >= (0.95 if not is_uncapped else 0.0):
        out.append(AllUpLeg(
            race_number=race_number, horse_no=horse["horse_no"],
            horse_name=horse["horse_name"], pool="WIN",
            p_used=round(p_used_w, 4), decimal_odds=sp,
            edge_pct=round(edge_w, 4), ev=round(win_ev, 4),
        ))
    # PLACE: ~2.5x hit prob, ~0.35x decimal odds — rough HKJC ratio
    p_place = min(0.95, p_used_w * 2.5)
    o_place = max(1.05, sp * 0.35)
    place_ev = p_place * o_place
    place_edge = (p_place / max(0.01, min(0.99, p_market * 2.5))) - 1.0
    if place_ev >= (0.95 if not is_uncapped else 0.0):
        out.append(AllUpLeg(
            race_number=race_number, horse_no=horse["horse_no"],
            horse_name=horse["horse_name"], pool="PLACE",
            p_used=round(p_place, 4), decimal_odds=round(o_place, 2),
            edge_pct=round(place_edge, 4), ev=round(place_ev, 4),
        ))


def _emit_qin_qpl_legs(
    out: list, race_number: int, banker: dict, ticket_legs: list,
    market_probs: dict,
    *, n_observed: int, shrinkage_k: int, is_uncapped: bool,
) -> None:
    """Build QIN_BANKER + QPL_BANKER legs from a banker + selected legs.

    For an N-leg banker, the leg's expected gross-return per $1 staked is:
        QIN: P(banker top-2) * E[ Σ_i 1{leg_i top-2 | banker top-2} *
                                    div_per_$1(banker, leg_i) ] / N
        QPL: same but top-3.
    We approximate dividends from leg SP using the classic
    (sp_a*sp_b)*0.825 / k formula (k=2 for QIN, k=3 for QPL).
    """
    sp_b = banker.get("win_odds")
    p_b = banker.get("p_model")
    if not sp_b or p_b is None:
        return
    p_market_b = market_probs.get(banker["horse_no"], 0.0) or (1.0 / sp_b)
    # P(banker top-N) approximations
    p_b_top2 = min(0.95, shrink_probability(p_b, p_market_b,
                                              n_observed=n_observed,
                                              k_prior=shrinkage_k) * 1.9)
    p_b_top3 = min(0.97, shrink_probability(p_b, p_market_b,
                                              n_observed=n_observed,
                                              k_prior=shrinkage_k) * 2.5)

    # Per-leg conditional probs + dividend estimates
    leg_horses = []
    leg_horse_names = []
    qin_div_avg = 0.0
    qpl_div_avg = 0.0
    qin_p_any = 0.0   # P(any leg top-2 | banker top-2)
    qpl_p_any = 0.0
    n = max(1, len(ticket_legs))
    for l in ticket_legs:
        leg_horses.append(l["horse_no"])
        leg_horse_names.append(l.get("horse_name", str(l["horse_no"])))
        sp_l = l.get("win_odds") or sp_b
        p_l = l.get("p_model") or 0.10
        # Top-2/top-3 probs for the leg horse
        p_l_top2 = min(0.95, p_l * 1.9)
        p_l_top3 = min(0.97, p_l * 2.5)
        qin_p_any += p_l_top2 / n
        qpl_p_any += p_l_top3 / n
        # Dividend per $10 — classic approximation
        qin_div_avg += (sp_b * sp_l) * 0.825 / 2.0 / n
        qpl_div_avg += (sp_b * sp_l) * 0.825 / 3.0 / n

    # ----- QIN_BANKER leg
    p_qin_hit = p_b_top2 * min(0.99, qin_p_any)   # hit if banker top2 AND any leg top2
    o_qin = max(1.05, qin_div_avg)
    qin_ev = p_qin_hit * o_qin
    if qin_ev >= (0.95 if not is_uncapped else 0.0):
        out.append(AllUpLeg(
            race_number=race_number, horse_no=banker["horse_no"],
            horse_name=banker["horse_name"], pool="QIN_BANKER",
            p_used=round(p_qin_hit, 4), decimal_odds=round(o_qin, 2),
            edge_pct=round(qin_ev - 1.0, 4), ev=round(qin_ev, 4),
            leg_horses=list(leg_horses), leg_horse_names=list(leg_horse_names),
        ))

    # ----- QPL_BANKER leg (most forgiving: banker top-3 + any leg top-3)
    p_qpl_hit = p_b_top3 * min(0.99, qpl_p_any)
    o_qpl = max(1.05, qpl_div_avg)
    qpl_ev = p_qpl_hit * o_qpl
    if qpl_ev >= (0.95 if not is_uncapped else 0.0):
        out.append(AllUpLeg(
            race_number=race_number, horse_no=banker["horse_no"],
            horse_name=banker["horse_name"], pool="QPL_BANKER",
            p_used=round(p_qpl_hit, 4), decimal_odds=round(o_qpl, 2),
            edge_pct=round(qpl_ev - 1.0, 4), ev=round(qpl_ev, 4),
            leg_horses=list(leg_horses), leg_horse_names=list(leg_horse_names),
        ))


# ---------------------------------------------------------------------------
# Per-meeting decision build
# ---------------------------------------------------------------------------
def build_meeting_slate(
    date_compact: str,
    *,
    bankroll: float,
    mode: str = "balanced",
    enable_allup: bool = True,
    n_observed: int = 30,
    blackbook: Optional[dict] = None,
    factor_tbls: Optional[dict] = None,
) -> dict:
    """Return the recommended slate for a single meeting.

    The slate has, per race:
        - the originating ticket from build_meeting_tickets
        - a Stake decision (accepted/skipped + HKD)
        - reason

    Plus a (possibly empty) all-up chain plan and a meeting-level summary.
    """
    cfg = MODE_TABLE[mode]
    is_uncapped = (mode == "uncapped")
    items = build_meeting_tickets(date_compact, blackbook=blackbook,
                                   factor_tbls=factor_tbls)
    if not items:
        return {"date": date_compact, "mode": mode, "races": [],
                "allup": None, "summary": {"reason": "no items"}}

    # Cap exposure across the meeting
    meeting_cap = cfg["max_per_meeting_frac"] * bankroll
    spent = 0.0
    races_out = []
    candidate_legs: list[AllUpLeg] = []

    for item in items:
        ticket = item["ticket"]
        rows = item["rows"]
        rn = item["race_number"]

        market_probs = race_market_probs(rows)

        # In uncapped mode, override SKIP tickets with a WIN-on-top1 fallback
        # so we always place something. The user explicitly removed gates.
        if ticket["play"] == "SKIP" and is_uncapped and rows:
            top = rows[0]
            ticket = dict(ticket)
            ticket["play"] = "WIN"
            ticket["banker"] = top
            ticket["legs"] = []
            ticket["reason"] = "uncapped fallback: WIN top-1"

        if ticket["play"] == "SKIP":
            races_out.append({
                "race_number": rn,
                "play": "SKIP",
                "stake_hkd": 0.0,
                "reason": ticket.get("reason", "skip"),
                "ticket": _ticket_brief(ticket),
            })
            continue

        pool_key = PLAY_TO_POOL.get(ticket["play"], ticket["play"])
        p_model, p_market = estimate_p_for_play(ticket, market_probs)
        decimal_odds = estimate_decimal_odds_for_play(
            ticket, divs_for_race={}, fallback_sp=None
        )

        # Uncapped: synthesize p_market from raw odds if needed
        if is_uncapped:
            banker = ticket.get("banker") or {}
            if (not p_market) and banker.get("win_odds"):
                p_market = 1.0 / banker["win_odds"]
            if not p_model and banker.get("p_model"):
                p_model = banker["p_model"]
            if not decimal_odds and banker.get("win_odds"):
                decimal_odds = banker["win_odds"]

        # Pre-race fallback (any mode): if dividends are missing because
        # the meeting hasn't happened, use the banker's morning-line
        # win_odds + model probability so the slate is still usable.
        banker = ticket.get("banker") or {}
        if (not p_market) and banker.get("win_odds"):
            p_market = 1.0 / banker["win_odds"]
        if (not p_model) and banker.get("p_model"):
            p_model = banker["p_model"]
        if (not decimal_odds) and banker.get("win_odds"):
            decimal_odds = banker["win_odds"]

        # Last-resort pre-race advisory: when no SP / no live odds at all,
        # surface the pick at the HKJC minimum so the user sees it on the
        # slate with a clear "advisory" reason (rather than silently
        # skipping the whole meeting). p_market is conservatively pegged
        # at p_model so edge_pct=0 — stake stays at floor.
        if p_model and not (p_market and decimal_odds):
            p_market = p_model
            decimal_odds = 1.0 / max(p_model, 0.01)
            races_out.append({
                "race_number": rn, "play": ticket["play"],
                "banker":      (ticket.get("banker") or {}).get("horse_name"),
                "banker_no":   (ticket.get("banker") or {}).get("horse_no"),
                "legs":        [(l["horse_no"], l["horse_name"])
                                for l in ticket.get("legs", [])],
                "stake_hkd":   HKJC_MIN_STAKE,
                "p_used":      p_model, "p_market": p_market,
                "edge_pct":    0.0, "ev_per_dollar": 0.0,
                "kelly_full":  0.0, "kelly_used": 0.0,
                "decimal_odds": decimal_odds,
                "reason":      "pre-race advisory (no live odds yet) — "
                                "min stake $10",
                "accepted":    True,
                "ticket":      _ticket_brief(ticket),
            })
            spent += HKJC_MIN_STAKE
            continue

        if not (p_model and p_market and decimal_odds):
            races_out.append({
                "race_number": rn, "play": ticket["play"],
                "stake_hkd": 0.0,
                "reason": "missing p_model / p_market / odds",
                "ticket": _ticket_brief(ticket),
            })
            continue

        # Remaining cap (applied AFTER Kelly sizing — Kelly uses full bankroll)
        remaining = max(0.0, meeting_cap - spent)
        if (not is_uncapped) and remaining < HKJC_MIN_STAKE:
            races_out.append({
                "race_number": rn, "play": ticket["play"],
                "stake_hkd": 0.0,
                "reason": f"meeting cap exhausted (${spent:.0f}/${meeting_cap:.0f})",
                "ticket": _ticket_brief(ticket),
            })
            continue

        # At small bankrolls (< $5k) the $10 min is essentially the unit, so
        # force_min lets positive-edge bets fire at the minimum even when
        # the half-Kelly fraction would round below $10. Per-bet cap still
        # binds, so this can't blow up exposure.
        bet_policy = "force_min" if bankroll < 5000 else "floor"
        stake = size_bet(
            p_model=p_model, p_market=p_market,
            decimal_odds=decimal_odds, bankroll=bankroll,
            pool=pool_key, mode=mode, n_observed=n_observed,
            min_bet_policy=bet_policy,
        )

        # Apply meeting cap to the chosen stake too (skip in uncapped mode)
        if (not is_uncapped) and stake.accepted and stake.stake_hkd > remaining:
            stake.stake_hkd = math.floor(remaining / HKJC_MIN_STAKE) * HKJC_MIN_STAKE
            if stake.stake_hkd < HKJC_MIN_STAKE:
                stake.accepted = False
                stake.reason += "; clipped by meeting cap"

        if stake.accepted:
            spent += stake.stake_hkd

        races_out.append({
            "race_number":   rn,
            "play":          ticket["play"],
            "banker":        (ticket.get("banker") or {}).get("horse_name"),
            "banker_no":     (ticket.get("banker") or {}).get("horse_no"),
            "legs":          [(l["horse_no"], l["horse_name"])
                              for l in ticket.get("legs", [])],
            "stake_hkd":     stake.stake_hkd if stake.accepted else 0.0,
            "p_used":        stake.p_used,
            "p_market":      stake.p_market,
            "edge_pct":      stake.edge_pct,
            "ev_per_dollar": stake.ev_per_dollar,
            "kelly_full":    stake.kelly_full,
            "kelly_used":    stake.kelly_used,
            "decimal_odds":  decimal_odds,
            "reason":        stake.reason,
            "accepted":      stake.accepted,
            "ticket":        _ticket_brief(ticket),
        })

        # All-up candidates: build WIN, PLACE, QIN_BANKER, QPL_BANKER legs.
        # Multiple legs per race; chain enumerator picks at most one per
        # race per chain (best by EV). Value-pick horses (positive market
        # edge, not just the model's top-1) are also surfaced as WIN/PLACE
        # leg candidates.
        if cfg["allow_allup"] and enable_allup:
            banker = ticket.get("banker") or {}
            ticket_legs = ticket.get("legs") or []

            # --- (1) WIN+PLACE on the model's banker, if it has any edge
            if banker.get("win_odds") and banker.get("p_model") is not None:
                _emit_simple_legs(
                    candidate_legs, rn, banker, market_probs,
                    n_observed=n_observed,
                    shrinkage_k=cfg["shrinkage_k"],
                    is_uncapped=is_uncapped,
                )

            # --- (2) QIN_BANKER + QPL_BANKER bundles
            if ticket_legs and banker.get("p_model") is not None \
                    and banker.get("win_odds"):
                _emit_qin_qpl_legs(
                    candidate_legs, rn, banker, ticket_legs, market_probs,
                    n_observed=n_observed,
                    shrinkage_k=cfg["shrinkage_k"],
                    is_uncapped=is_uncapped,
                )

            # --- (3) Value picks: any other horse in the race with positive
            # market edge (p_model > p_market) gets WIN+PLACE legs too.
            for row in rows:
                hno = row.get("horse_no")
                if hno is None or banker.get("horse_no") == hno:
                    continue
                if row.get("p_model") is None or not row.get("win_odds"):
                    continue
                _emit_simple_legs(
                    candidate_legs, rn, row, market_probs,
                    n_observed=n_observed,
                    shrinkage_k=cfg["shrinkage_k"],
                    is_uncapped=is_uncapped,
                )

    # Build all-up chains. Aggressive/balanced fire only the single best;
    # uncapped fires up to MAX_CHAINS chains (parallel satellites).
    allup_plan = None
    allup_plans: list[dict] = []
    if cfg["allow_allup"] and enable_allup and candidate_legs:
        from allup import ALLUP_CFG_UNCAPPED
        if is_uncapped:
            chains = build_chains(
                candidate_legs, bankroll=bankroll,
                mode_max_frac=ALLUP_CFG_UNCAPPED["allup_max_frac"],
                cfg=ALLUP_CFG_UNCAPPED,
            )
        else:
            chains = build_chains(
                candidate_legs, bankroll=bankroll,
                mode_max_frac=min(cfg["max_per_meeting_frac"],
                                   ALLUP_CFG["allup_max_frac"]),
            )
        if chains:
            top_k = 5 if is_uncapped else 1
            for best in chains[:top_k]:
                plan = {
                    "stake_hkd": best.stake_hkd,
                    "chain_ev": best.chain_ev,
                    "chain_p_hit": best.chain_p_hit,
                    "expected_payout": best.expected_payout,
                    "expected_pnl": best.expected_pnl,
                    "legs": [{"race": l.race_number, "horse_no": l.horse_no,
                              "horse_name": l.horse_name, "pool": l.pool,
                              "p_used": l.p_used, "decimal_odds": l.decimal_odds,
                              "ev": l.ev,
                              "leg_horses": list(l.leg_horses),
                              "leg_horse_names": list(l.leg_horse_names)}
                             for l in best.legs],
                    "reason": best.reason,
                }
                allup_plans.append(plan)
                spent += best.stake_hkd
            allup_plan = allup_plans[0]   # keep field for back-compat

    summary = {
        "mode": mode,
        "starting_bankroll": bankroll,
        "total_exposure_hkd": round(spent, 2),
        "exposure_pct": round(spent / bankroll, 4) if bankroll else 0.0,
        "n_bets": sum(1 for r in races_out if r.get("accepted")),
        "n_skipped": sum(1 for r in races_out if not r.get("accepted")),
        "meeting_cap_hkd": round(meeting_cap, 2),
    }
    return {"date": date_compact, "mode": mode, "races": races_out,
            "allup": allup_plan, "allup_plans": allup_plans,
            "summary": summary}


def _ticket_brief(t: dict) -> dict:
    """Compact view of a ticket for the slate output (drops verbose fields)."""
    b = t.get("banker") or {}
    return {
        "play": t["play"], "filter": t.get("filter"),
        "confidence": t.get("confidence"),
        "n_combos": t.get("n_combos"),
        "stake_units_legacy": t.get("stake_units"),
        "banker_no": b.get("horse_no"),
        "banker_name": b.get("horse_name"),
        "banker_sp": b.get("win_odds"),
        "banker_pmodel": b.get("p_model"),
        "banker_edge": b.get("edge"),
        "legs": [{"no": l["horse_no"], "name": l["horse_name"],
                   "sp": l.get("win_odds"), "edge": l.get("edge")}
                  for l in t.get("legs", [])],
        "reason": t.get("reason"),
    }


# ---------------------------------------------------------------------------
# Backtest: settle a slate against real dividends
# ---------------------------------------------------------------------------
def settle_slate(slate: dict) -> list[Bet]:
    """Return Bet records (one per accepted race bet + one per allup) suitable
    for bankroll.replay."""
    date = slate["date"]
    mode = slate["mode"]
    divs = load_dividends(date)
    meet = load_meeting(date)
    res_by_race = {r["race_number"]: r
                    for r in (meet["results"]["races"]
                                if meet["results"] else [])}
    res_by_no_per_race = {rn: _results_race_by_no(r)
                            for rn, r in res_by_race.items()}

    bets: list[Bet] = []
    for race in slate["races"]:
        if not race.get("accepted"):
            continue
        rn = race["race_number"]
        play = race["play"]
        stake = race["stake_hkd"]
        rb = res_by_no_per_race.get(rn, {})
        finishers = sorted(
            ((no, r["place"]) for no, r in rb.items() if r.get("place") is not None),
            key=lambda kv: kv[1],
        )
        win_no = {no for no, p in finishers if p == 1}
        top2 = {no for no, p in finishers[:2]}
        top3 = {no for no, p in finishers[:3]}
        top4 = [no for no, p in finishers[:4]]   # ordered

        race_divs = divs.get(rn, {})
        payout = 0.0
        b_no = race.get("banker_no")
        legs_nos = [l[0] for l in race.get("legs", [])]

        if play == "WIN":
            if b_no in win_no:
                payout = stake * div_pay(race_divs, "WIN", [b_no])
        elif play == "PLACE":
            if b_no in top3:
                payout = stake * div_pay(race_divs, "PLACE", [b_no])
        elif play == "QIN_BANKER":
            if b_no in top2 and legs_nos:
                per = stake / len(legs_nos)
                for lno in legs_nos:
                    if lno in top2 and lno != b_no:
                        payout += per * div_pay(race_divs, "QIN", [b_no, lno])
                        break
        elif play == "QPL_BANKER":
            if b_no in top3 and legs_nos:
                per = stake / len(legs_nos)
                for lno in legs_nos:
                    if lno in top3 and lno != b_no:
                        payout += per * div_pay(race_divs, "QPL", [b_no, lno])
        elif play in ("F4_BOX", "F4_BOX_TOP5"):
            sel = [b_no] + legs_nos
            from itertools import combinations
            combos = list(combinations(sel, 4))
            per = stake / len(combos) if combos else 0
            for combo in combos:
                if set(combo) == set(top4):
                    payout += per * div_pay(race_divs, "F4", list(combo))
        elif play == "QTT_BOX":
            sel = [b_no] + legs_nos
            from itertools import permutations
            perms = list(permutations(sel, 4))
            per = stake / len(perms) if perms else 0
            for perm in perms:
                if list(perm) == top4:
                    payout += per * div_pay(race_divs, "QTT", list(perm))
                    break

        bets.append(Bet(
            key=f"{date}/R{rn}/{play}",
            stake=stake, payout=round(payout, 2),
            timestamp=date, tag=f"{mode}/{play}",
        ))

    # All-up chains (one or many)
    plans = slate.get("allup_plans") or ([slate["allup"]] if slate.get("allup") else [])
    for idx, plan in enumerate(plans):
        if not plan:
            continue
        chain_legs = [
            AllUpLeg(
                race_number=l["race"], horse_no=l["horse_no"],
                horse_name=l["horse_name"], pool=l["pool"],
                p_used=l["p_used"], decimal_odds=l["decimal_odds"],
                edge_pct=0.0, ev=l["ev"],
                leg_horses=l.get("leg_horses", []),
                leg_horse_names=l.get("leg_horse_names", []),
            ) for l in plan["legs"]
        ]
        from allup import AllUpChain
        chain = AllUpChain(
            legs=chain_legs, stake_hkd=plan["stake_hkd"],
            chain_ev=plan["chain_ev"], chain_p_hit=plan["chain_p_hit"],
            chain_edge_pct=plan["chain_ev"] - 1.0,
            expected_payout=plan["expected_payout"],
            expected_pnl=plan["expected_pnl"], reason=plan["reason"],
        )
        ev = settle_chain(chain, results_by_race=res_by_no_per_race,
                            dividends_by_race=divs)
        bets.append(Bet(
            key=f"{date}/ALLUP{idx+1}",
            stake=ev["stake"], payout=ev["payout"],
            timestamp=date, tag=f"{mode}/ALLUP",
        ))
    return bets


# ---------------------------------------------------------------------------
# Pretty printer
# ---------------------------------------------------------------------------
def format_slate(slate: dict) -> str:
    out = [f"=== {slate['date']} · mode={slate['mode']} ===",
            f"Bankroll start ${slate['summary']['starting_bankroll']:.0f}  "
            f"meeting cap ${slate['summary']['meeting_cap_hkd']:.0f}"]
    for r in slate["races"]:
        if not r.get("accepted"):
            out.append(f"  R{r['race_number']:>2}  SKIP  ({r['reason']})")
            continue
        out.append(
            f"  R{r['race_number']:>2}  {r['play']:<11} "
            f"#{r['banker_no']:<2} {str(r.get('banker') or '')[:18]:<18} "
            f"O={r['decimal_odds']:.2f}  p_used={r['p_used']:.2f}  "
            f"edge={r['edge_pct']:+.1%}  ${r['stake_hkd']:.0f}  "
            f"({r['reason']})"
        )
    plans = slate.get("allup_plans") or ([slate["allup"]] if slate.get("allup") else [])
    for plan in plans:
        out.append(f"  ALL-UP {len(plan['legs'])}-leg  ev={plan['chain_ev']:.2f} "
                   f"p_hit={plan['chain_p_hit']:.1%}  ${plan['stake_hkd']:.0f}  "
                   f"E[pnl]=${plan['expected_pnl']:+.0f}  ({plan['reason']})")
        for l in plan["legs"]:
            line = (f"     R{l['race']} {l['pool']:<10} "
                    f"#{l['horse_no']} {l['horse_name']}  "
                    f"O={l['decimal_odds']:.2f}  p={l['p_used']:.2f}  "
                    f"ev={l['ev']:.2f}")
            lh = l.get("leg_horses") or []
            if lh:
                names = l.get("leg_horse_names") or [str(x) for x in lh]
                line += "  legs=" + ",".join(
                    f"#{n} {nm}" for n, nm in zip(lh, names)
                )
            out.append(line)
    s = slate["summary"]
    out.append(f"  >> exposure ${s['total_exposure_hkd']:.0f} "
               f"({s['exposure_pct']:.1%})  bets={s['n_bets']}  "
               f"skipped={s['n_skipped']}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
APRIL_DATES = ["20260401", "20260406", "20260408",
                "20260412", "20260415", "20260419", "20260422"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--date", help="YYYYMMDD or YYYY-MM-DD for one meeting")
    ap.add_argument("--bankroll", type=float, default=1000.0,
                    help="Starting HKD bankroll (default 1000)")
    ap.add_argument("--mode", default="balanced",
                    choices=["conservative", "balanced", "aggressive",
                              "uncapped"])
    ap.add_argument("--no-allup", action="store_true",
                    help="Disable cross-race all-up satellite")
    ap.add_argument("--backtest", action="store_true",
                    help="Replay all April 2026 meetings; outputs metrics")
    ap.add_argument("--out-json", help="Write slate(s) to JSON file")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    bb = load_blackbook()
    factor_tbls = load_factor_tables()

    if args.backtest:
        all_bets: list[Bet] = []
        slates: list[dict] = []
        bankroll = args.bankroll
        for d in APRIL_DATES:
            slate = build_meeting_slate(
                d, bankroll=bankroll, mode=args.mode,
                enable_allup=not args.no_allup,
                blackbook=bb, factor_tbls=factor_tbls,
            )
            slates.append(slate)
            if not args.quiet:
                print(format_slate(slate))
            day_bets = settle_slate(slate)
            all_bets.extend(day_bets)
            # Compound the bankroll meeting-by-meeting
            day_pnl = sum(b.payout - b.stake for b in day_bets)
            bankroll = max(0.0, bankroll + day_pnl)
            if not args.quiet:
                print(f"  >> day pnl ${day_pnl:+.0f}  bankroll ${bankroll:.0f}\n")
        # Replay against starting bankroll for canonical metrics
        _, metrics = replay(all_bets, starting_bankroll=args.bankroll)
        print(f"\n=== BACKTEST · mode={args.mode} ===")
        for k, v in asdict(metrics).items():
            print(f"  {k:<22} {v}")
        if args.out_json:
            Path(args.out_json).write_text(json.dumps({
                "mode": args.mode,
                "bankroll": args.bankroll,
                "slates": slates,
                "bets": [asdict(b) for b in all_bets],
                "metrics": asdict(metrics),
            }, indent=2, default=str), encoding="utf-8")
            print(f"\nWrote {args.out_json}")
        return

    if args.date:
        d = args.date.replace("-", "")
        slate = build_meeting_slate(
            d, bankroll=args.bankroll, mode=args.mode,
            enable_allup=not args.no_allup,
            blackbook=bb, factor_tbls=factor_tbls,
        )
        print(format_slate(slate))
        if args.out_json:
            Path(args.out_json).write_text(json.dumps(slate, indent=2,
                                                       default=str),
                                            encoding="utf-8")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
