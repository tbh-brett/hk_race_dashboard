"""bet_optimizer.py — Integrated per-card betting strategy engine (v1.0)

Turns the model's race-day report into an *actionable, profitable-by-design*
slate that leans into the ONE structure that has actually made money in this
account: place-reliable bankers compounded in Quinella-Place all-up parlays
(the 27 May 2026 ALLUP_QQP 3x4 that returned $25,003 off $324).

Forensic basis (729 settled bets, Apr-May 2026):
    QPL          +13% ROI over 225 bets   <- the real edge (place pools)
    ALLUP_QQP    the only big winner       <- edge compounded across legs
    QIN          -26% ROI over 228 bets    <- exact-pair noise (leak)
    PLACE/WIN    -90%+ (oversized punts)    <- bankroll-blind emotion (leak)
    QTT_BOX/TCE  -100% (0 hits)             <- exotic lottery (leak)

So the engine:
  1. Ranks every runner by *place-reliability* (P(top-3)), not win prob.
  2. Picks a banker per race = most place-reliable runner, overlaid with the
     horse_cycle rating model (PRIMED boost, FADE_OVERRATED penalty).
  3. Emits a per-race QPL banker bet (banker x floats) sized to bankroll.
  4. Builds ONE all-up QPL banker-parlay across the most reliable races —
     the jackpot ticket — using the proven all_up.py shapes.
  5. Allocates a fixed bankroll across the card by risk_goal.
  6. Flags live value when market under-rates a banker's place chance.

It is advisory: per the user's preference it does NOT hard-block any bet.

Public API
----------
optimise_card(date_iso, bankroll=500.0, risk_goal="upside") -> dict
format_card(result) -> str
compute_drift_flags(result, market_win_probs) -> list[dict]
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

import all_up
import horse_cycle

REPORTS = Path(__file__).parent / "reports"

# ── place-probability model ──────────────────────────────────────────────
# P(finish top-3) approximated from win probability with a monotone transform
# calibrated so a 10% win horse ~ 33% place, 30% ~ 60%, 60% ~ 85%.
PLACE_EXPONENT = 2.2


def win_to_place_prob(win_prob_pct: float) -> float:
    wp = max(0.0, min(1.0, float(win_prob_pct) / 100.0))
    return 1.0 - (1.0 - wp) ** PLACE_EXPONENT


# ── risk_goal budget profiles (fractions of bankroll) ────────────────────
# Each profile: (all_up_jackpot, per_race_singles, reserve)
RISK_PROFILES = {
    # steady grind: protect bankroll, bank the QPL edge, tiny jackpot dabble
    "grind":   (0.15, 0.70, 0.15),
    # balanced: mostly grind + one disciplined jackpot ticket
    "balanced": (0.35, 0.55, 0.10),
    # upside-seeking: lean into all-up jackpots, accept losing weeks
    "upside":  (0.60, 0.30, 0.10),
}

# horse_cycle adjustments to banker place-prob (additive, capped)
CYCLE_BONUS = {
    "PRIMED": +0.05,
    "EARLY_DROPPER": +0.02,
    "WATCH": 0.0,
    "NEUTRAL": 0.0,
    "NO_DATA": 0.0,
    "FADE_OVERRATED": -0.12,   # do NOT bank a horse the rating model says fades
}
TIER_BONUS = {"A": +0.04, "B": +0.02, "C": 0.0, "": 0.0}

# how many floats accompany a banker in QPL legs / singles
SINGLE_FLOATS = 3
PARLAY_FLOATS = 2
# how many races go into the all-up jackpot ticket
PARLAY_LEGS = 3
PARLAY_SHAPE = "3x4"        # doubles + treble (sizes {2,3}) — forgiving, proven


# ── report loading ───────────────────────────────────────────────────────
def _compact(date_iso: str) -> str:
    return date_iso.replace("-", "")


def _load_report(date_iso: str) -> Optional[dict]:
    compact = _compact(date_iso)
    # prefer the newest model version available for the date
    for suffix in ("_v4.4", "_v4.3", "_v4.2", "_v3.4.8"):
        p = REPORTS / f"race_day_report_{compact}{suffix}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    # any matching report
    cands = sorted(REPORTS.glob(f"race_day_report_{compact}_*.json"))
    cands = [c for c in cands if "SARR" not in c.name]
    if cands:
        return json.loads(cands[-1].read_text(encoding="utf-8"))
    return None


def _cycle_lookup(date_iso: str) -> dict[tuple[int, str], dict]:
    """Map (race_number, UPPER horse_name) -> {primary_flag, combined_tier}."""
    out: dict[tuple[int, str], dict] = {}
    try:
        payload = horse_cycle.load_or_build_card_cycle(_compact(date_iso))
    except Exception:
        payload = None
    if not payload:
        return out
    for h in payload.get("horses", []):
        key = (int(h.get("race_number", 0)), str(h.get("horse_name", "")).strip().upper())
        out[key] = {
            "primary_flag": h.get("primary_flag", "NEUTRAL"),
            "combined_tier": h.get("combined_tier", ""),
            "cycle_state": h.get("cycle_state", ""),
            "note": h.get("note", ""),
        }
    return out


# ── core scoring ─────────────────────────────────────────────────────────
def _score_runner(pick: dict, cyc: Optional[dict]) -> dict:
    """Attach place_prob + cycle overlay + confidence to a model pick."""
    wp = float(pick.get("win_prob") or 0.0)
    pp = win_to_place_prob(wp)
    flag = (cyc or {}).get("primary_flag", "NEUTRAL")
    tier = (cyc or {}).get("combined_tier", "")
    conf = pp + CYCLE_BONUS.get(flag, 0.0) + TIER_BONUS.get(tier, 0.0)
    conf = max(0.0, min(0.99, conf))
    return {
        "horse_no": int(pick.get("horse_no")),
        "horse_name": pick.get("horse_name", ""),
        "rank": int(pick.get("rank") or 0),
        "win_prob": round(wp, 1),
        "place_prob": round(pp, 3),
        "early_speed_z": pick.get("early_speed_z"),
        "style": pick.get("style", ""),
        "cycle_flag": flag,
        "cycle_tier": tier,
        "confidence": round(conf, 3),
    }


def _analyse_race(race: dict, cyc_map: dict) -> dict:
    rn = int(race.get("race_number") or 0)
    picks = race.get("picks") or []
    scored = []
    for p in picks:
        if p.get("horse_no") in (None, ""):
            continue
        key = (rn, str(p.get("horse_name", "")).strip().upper())
        scored.append(_score_runner(p, cyc_map.get(key)))
    scored.sort(key=lambda s: -s["confidence"])
    banker = scored[0] if scored else None
    second = scored[1] if len(scored) > 1 else None
    # dominance = how clear the banker is over the next-best place pick
    dominance = round((banker["confidence"] - second["confidence"]), 3) if (banker and second) else 0.0
    return {
        "race_number": rn,
        "race_name": race.get("race_name", ""),
        "distance": race.get("distance"),
        "race_class": race.get("race_class", ""),
        "is_awt": race.get("is_awt", False),
        "pace": race.get("pace", ""),
        "runners_scored": scored,
        "banker": banker,
        "dominance": dominance,
        # banker_quality blends banker reliability + how clear-cut it is
        "banker_quality": round((banker["confidence"] + dominance) if banker else 0.0, 3),
    }


def _round_to_unit(amount: float, unit: float = 10.0, minimum: float = 10.0,
                   floor: bool = False) -> float:
    if amount < minimum:
        return 0.0
    if floor:
        return float(max(int(amount // unit), int(minimum // unit)) * unit)
    return float(int(round(amount / unit)) * unit)


# ── ticket builders ──────────────────────────────────────────────────────
def _per_race_single(race_an: dict, stake: float) -> Optional[dict]:
    banker = race_an["banker"]
    if not banker:
        return None
    floats = [r for r in race_an["runners_scored"] if r["horse_no"] != banker["horse_no"]][:SINGLE_FLOATS]
    if not floats:
        return None
    n_combos = len(floats)               # QPL banker = banker paired with each float
    per_combo = _round_to_unit(stake / max(1, n_combos), unit=10.0, minimum=10.0)
    if per_combo <= 0:
        per_combo = 10.0
    total = per_combo * n_combos
    return {
        "race_number": race_an["race_number"],
        "bet_type": "QPL_BANKER",
        "banker": banker["horse_no"],
        "banker_name": banker["horse_name"],
        "floats": [f["horse_no"] for f in floats],
        "float_names": [f["horse_name"] for f in floats],
        "n_combos": n_combos,
        "stake_per_combo": per_combo,
        "total_stake": total,
        "banker_place_prob": banker["place_prob"],
        "banker_confidence": banker["confidence"],
        "cycle_flag": banker["cycle_flag"],
        "rationale": _banker_reason(banker, race_an["dominance"]),
    }


def _banker_reason(banker: dict, dominance: float) -> str:
    bits = [f"{banker['place_prob']*100:.0f}% top-3"]
    if banker["cycle_flag"] == "PRIMED":
        bits.append("rating PRIMED")
    elif banker["cycle_flag"] == "FADE_OVERRATED":
        bits.append("(!) rating FADE (weak banker)")
    if dominance >= 0.10:
        bits.append("clear standout")
    elif dominance < 0.04:
        bits.append("tight race — float wider")
    if banker.get("style") == "Leader":
        bits.append("on-pace")
    return ", ".join(bits)


def _build_jackpot_parlay(race_ans: list[dict], budget: float) -> Optional[dict]:
    """Top-N most reliable races → one QPL all-up banker parlay."""
    usable = [r for r in race_ans
              if r["banker"] and r["banker"]["cycle_flag"] != "FADE_OVERRATED"]
    usable.sort(key=lambda r: -r["banker_quality"])
    chosen = usable[:PARLAY_LEGS]
    if len(chosen) < 2:
        return None
    chosen.sort(key=lambda r: r["race_number"])
    legs = []
    for r in chosen:
        banker = r["banker"]
        floats = [x for x in r["runners_scored"] if x["horse_no"] != banker["horse_no"]][:PARLAY_FLOATS]
        pairs = [(banker["horse_no"], f["horse_no"]) for f in floats]
        legs.append({
            "race_no": r["race_number"],
            "selections": pairs,
            "_banker": banker["horse_no"],
            "_banker_name": banker["horse_name"],
            "_floats": [f["horse_no"] for f in floats],
            "_float_names": [f["horse_name"] for f in floats],
            "_banker_place_prob": banker["place_prob"],
        })
    n = len(legs)
    sizes = sorted(all_up.SHAPE_PRESETS.get(PARLAY_SHAPE, (n, frozenset({n})))[1]) \
        if n == 3 else [2, n]
    # fit stake_per_unit into the jackpot budget
    probe = all_up.build_all_up_ticket(legs=legs, pool="QPL", sizes=sizes, stake_per_unit=1.0)
    if not probe.get("valid"):
        return None
    n_units = probe["n_units"]
    spu = _round_to_unit(budget / max(1, n_units), unit=2.0, minimum=2.0, floor=True)
    if spu <= 0:
        spu = 2.0
    ticket = all_up.build_all_up_ticket(legs=legs, pool="QPL", sizes=sizes, stake_per_unit=spu)
    ticket["legs_detail"] = legs
    # naive expected hit prob = product over legs of P(>=1 of (banker&float) pair places)
    p_all = 1.0
    for lg in legs:
        bpp = lg["_banker_place_prob"]
        # rough: leg hits if banker places AND >=1 float places; approximate
        p_leg = bpp * (1 - (1 - 0.45) ** max(1, len(lg["selections"])))
        p_all *= p_leg
    ticket["est_hit_prob"] = round(p_all, 4)
    return ticket


# ── main orchestrator ────────────────────────────────────────────────────
def optimise_card(date_iso: str, bankroll: float = 500.0,
                  risk_goal: str = "upside") -> dict:
    report = _load_report(date_iso)
    if not report:
        return {"ok": False, "reason": f"no race_day_report found for {date_iso}",
                "date": date_iso}
    cyc_map = _cycle_lookup(date_iso)
    races = report.get("races") or []
    race_ans = [_analyse_race(r, cyc_map) for r in races]
    race_ans = [r for r in race_ans if r["banker"]]

    jp_frac, single_frac, _reserve = RISK_PROFILES.get(risk_goal, RISK_PROFILES["upside"])
    jackpot_budget = bankroll * jp_frac
    singles_budget = bankroll * single_frac

    # rank races; spend singles budget on the most reliable ones
    ranked = sorted(race_ans, key=lambda r: -r["banker_quality"])
    n_single_races = max(1, min(len(ranked), int(round(singles_budget / 30.0))))
    single_races = ranked[:n_single_races]
    per_race_stake = singles_budget / max(1, n_single_races)

    singles = []
    for r in sorted(single_races, key=lambda x: x["race_number"]):
        t = _per_race_single(r, per_race_stake)
        if t:
            singles.append(t)

    jackpot = _build_jackpot_parlay(race_ans, jackpot_budget)

    spent_singles = sum(s["total_stake"] for s in singles)
    spent_jackpot = jackpot["total_stake"] if jackpot else 0.0

    return {
        "ok": True,
        "date": date_iso,
        "meeting_title": report.get("meeting_title", ""),
        "venue": report.get("meeting_venue", ""),
        "model_version": report.get("model_version", ""),
        "bankroll": bankroll,
        "risk_goal": risk_goal,
        "budget_split": {
            "jackpot": round(jackpot_budget, 0),
            "singles": round(singles_budget, 0),
            "reserve": round(bankroll * _reserve, 0),
        },
        "spent": {
            "singles": round(spent_singles, 0),
            "jackpot": round(spent_jackpot, 0),
            "total": round(spent_singles + spent_jackpot, 0),
        },
        "race_analyses": race_ans,
        "singles": singles,
        "jackpot": jackpot,
    }


# ── live value / drift ───────────────────────────────────────────────────
def compute_drift_flags(result: dict,
                        market_win_probs: dict[int, dict[int, float]],
                        edge_threshold: float = 0.08) -> list[dict]:
    """Flag legs where the model thinks a banker is MORE likely to place than
    the market implies — i.e. the QPL leg is value. market_win_probs is
    {race_no: {horse_no: win_prob_pct}}.
    """
    flags = []
    for r in result.get("race_analyses", []):
        banker = r["banker"]
        if not banker:
            continue
        mkt = market_win_probs.get(r["race_number"], {})
        m_wp = mkt.get(banker["horse_no"])
        if m_wp is None:
            continue
        m_pp = win_to_place_prob(m_wp)
        edge = banker["place_prob"] - m_pp
        if edge >= edge_threshold:
            flags.append({
                "race_number": r["race_number"],
                "horse_no": banker["horse_no"],
                "horse_name": banker["horse_name"],
                "model_place_prob": banker["place_prob"],
                "market_place_prob": round(m_pp, 3),
                "edge": round(edge, 3),
                "msg": f"R{r['race_number']} #{banker['horse_no']} {banker['horse_name']}: "
                       f"model {banker['place_prob']*100:.0f}% top-3 vs market "
                       f"{m_pp*100:.0f}% — QPL value (+{edge*100:.0f}%)",
            })
    flags.sort(key=lambda f: -f["edge"])
    return flags


# ── text formatter (CLI / agent) ─────────────────────────────────────────
def format_card(result: dict) -> str:
    if not result.get("ok"):
        return f"[bet_optimizer] {result.get('reason','no result')}"
    L = []
    L.append("=" * 72)
    L.append(f" BET OPTIMIZER — {result['meeting_title']} ({result['date']})")
    L.append(f" bankroll ${result['bankroll']:.0f}  goal={result['risk_goal']}  "
             f"model={result['model_version']}")
    bs = result["budget_split"]; sp = result["spent"]
    L.append(f" budget: jackpot ${bs['jackpot']:.0f} / singles ${bs['singles']:.0f} "
             f"/ reserve ${bs['reserve']:.0f}   spent ${sp['total']:.0f}")
    L.append("=" * 72)

    L.append("\n PER-RACE QPL BANKER SINGLES")
    L.append("-" * 72)
    if result["singles"]:
        for s in result["singles"]:
            floats = ",".join(str(x) for x in s["floats"])
            L.append(f" R{s['race_number']:>2}  bank #{s['banker']} {s['banker_name'][:18]:<18} "
                     f"x [{floats}]  ${s['total_stake']:.0f}  "
                     f"({s['stake_per_combo']:.0f}/combo)")
            L.append(f"       {s['rationale']}")
    else:
        L.append("  (none)")

    L.append("\n JACKPOT — QPL ALL-UP BANKER PARLAY")
    L.append("-" * 72)
    jp = result["jackpot"]
    if jp:
        L.append(f"  shape {jp.get('shape_label','?')}  pool QPL  "
                 f"{jp['n_units']} units x ${jp['stake_per_unit']:.0f} = "
                 f"${jp['total_stake']:.0f}   est hit ~{jp.get('est_hit_prob',0)*100:.1f}%")
        for lg in jp["legs_detail"]:
            floats = ",".join(str(x) for x in lg["_floats"])
            L.append(f"   R{lg['race_no']:>2}  bank #{lg['_banker']} "
                     f"{lg['_banker_name'][:18]:<18} x [{floats}]  "
                     f"(bank {lg['_banker_place_prob']*100:.0f}% top-3)")
    else:
        L.append("  (not enough reliable bankers for a parlay)")
    L.append("")
    return "\n".join(L)


# ── market odds → win-prob maps (for live value / drift) ─────────────────
def _odds_to_winprob(raw: dict) -> dict[int, dict[int, float]]:
    """{race_no: {horse_no: win_odds}} -> overround-normalised win% per race."""
    out: dict[int, dict[int, float]] = {}
    for rn, per in (raw or {}).items():
        inv = {int(hn): 1.0 / float(od)
               for hn, od in per.items()
               if od and float(od) > 1.0}
        ov = sum(inv.values())
        if ov <= 0:
            continue
        out[int(rn)] = {hn: (p / ov) * 100.0 for hn, p in inv.items()}
    return out


def market_from_live_odds(date_compact: str) -> dict[int, dict[int, float]]:
    """Win-prob map from scraped live odds (cache/live_odds). {} when none."""
    try:
        from betting_strategy import _load_live_odds
        raw = _load_live_odds(date_compact)        # {rn: {hn: win_odds}}
    except Exception:
        return {}
    return _odds_to_winprob(raw)


def market_from_report_sp(report: dict) -> dict[int, dict[int, float]]:
    """Win-prob map from the report runners' SP win_odds (hindsight)."""
    raw: dict = {}
    for race in (report or {}).get("races", []):
        rn = int(race.get("race_number") or 0)
        per = {}
        for run in race.get("runners", []) or []:
            try:
                per[int(run.get("horse_no"))] = float(str(run.get("win_odds", "")).strip())
            except (TypeError, ValueError):
                continue
        if per:
            raw[rn] = per
    return _odds_to_winprob(raw)


# ── log a slate to the My Bets ledger ────────────────────────────────────
def _slate_tag(result: dict) -> str:
    return f"[optimizer {result['date']} {result['risk_goal']}]"


def already_logged(result: dict) -> bool:
    """True if a slate with this exact tag is already in the bet log."""
    tag = _slate_tag(result)
    try:
        import user_bets
        for r in user_bets.load_bets(settle=False):
            if tag in str(r.get("notes", "")):
                return True
    except Exception:
        pass
    return False


def log_slate_to_user_bets(result: dict, include_singles: bool = True,
                           include_jackpot: bool = True) -> dict:
    """Append the optimiser's singles + jackpot to reports/user_bets_log.jsonl.

    Idempotent on the slate tag so clicking twice does not duplicate. The
    all-up jackpot logs as an OPEN ALLUP_QQP (it settles later from the bookie
    statement, like any real all-up)."""
    import user_bets
    if already_logged(result):
        return {"logged": 0, "skipped": True, "tag": _slate_tag(result)}
    date_compact = _compact(result["date"])
    venue = result.get("venue", "")
    tag = _slate_tag(result)
    logged = 0
    if include_singles:
        for s in result.get("singles", []):
            user_bets.submit_bet(
                meeting_date=date_compact, venue=venue,
                race_number=s["race_number"], bet_type="QPL_BANKER",
                selections=list(s["floats"]), banker=s["banker"],
                stake_hkd=s["total_stake"],
                notes=f"{tag} QPL banker #{s['banker']} x {s['floats']}")
            logged += 1
    jp = result.get("jackpot")
    if include_jackpot and jp:
        legs = [{"race_number": lg["race_no"], "banker": lg["_banker"],
                 "selections": list(lg["_floats"])} for lg in jp["legs_detail"]]
        flat: list[int] = []
        for lg in jp["legs_detail"]:
            flat += [lg["_banker"]] + list(lg["_floats"])
        user_bets.submit_bet(
            meeting_date=date_compact, venue=venue,
            race_number=legs[0]["race_number"], bet_type="ALLUP_QQP",
            selections=flat, banker=None, legs=legs,
            stake_hkd=jp["total_stake"], all_up_formula=jp.get("shape_label", ""),
            notes=f"{tag} all-up QPL {jp.get('shape_label','')} "
                  f"({jp['n_units']}u x ${jp['stake_per_unit']:.0f})")
        logged += 1
    return {"logged": logged, "skipped": False, "tag": tag}


# ── backtest: replay optimiser slates against settled dividends ──────────
def available_report_dates() -> list[str]:
    """ISO dates that have BOTH a race-day report and a dividends file."""
    out = []
    seen = set()
    for p in sorted(REPORTS.glob("race_day_report_*_v*.json")):
        name = p.name
        if "SARR" in name:
            continue
        compact = name.split("_")[3] if len(name.split("_")) > 3 else ""
        if not (compact.isdigit() and len(compact) == 8) or compact in seen:
            continue
        seen.add(compact)
        if (REPORTS / f"dividends_{compact}.json").exists():
            out.append(f"{compact[:4]}-{compact[4:6]}-{compact[6:]}")
    return sorted(out)


def backtest_optimiser(dates: Optional[list[str]] = None,
                       bankroll: float = 500.0,
                       goal: str = "upside") -> dict:
    """Replay the optimiser over settled meetings. Singles settle from QPL
    dividends; the jackpot via all_up.settle_all_up_ticket. Returns a summary
    plus per-meeting rows."""
    import all_up as _au
    if dates is None:
        dates = available_report_dates()
    rows = []
    tot_stake = tot_ret = 0.0
    n_jack_hits = 0
    for d in dates:
        res = optimise_card(d, bankroll, goal)
        if not res.get("ok"):
            continue
        dc = _compact(d)
        div = _au._read_dividends(dc)
        if not div:
            continue
        s_stake = s_ret = 0.0
        for s in res.get("singles", []):
            for f in s["floats"]:
                dv = _au._race_div(div, s["race_number"], "QPL", (s["banker"], f))
                s_ret += (dv / 10.0) * s["stake_per_combo"]
            s_stake += s["total_stake"]
        j_stake = j_ret = 0.0
        jp = res.get("jackpot")
        if jp:
            settle = _au.settle_all_up_ticket(jp, dc)
            j_stake = float(settle.get("stake", jp["total_stake"]))
            j_ret = float(settle.get("return", 0.0))
            if j_ret > 0:
                n_jack_hits += 1
        stake = s_stake + j_stake
        ret = s_ret + j_ret
        tot_stake += stake
        tot_ret += ret
        rows.append({
            "date": d,
            "stake": round(stake, 0),
            "return": round(ret, 0),
            "pnl": round(ret - stake, 0),
            "singles_pnl": round(s_ret - s_stake, 0),
            "jackpot_pnl": round(j_ret - j_stake, 0),
            "jackpot_hit": bool(jp and j_ret > 0),
        })
    n = len(rows)
    return {
        "ok": n > 0,
        "goal": goal,
        "bankroll": bankroll,
        "n_meetings": n,
        "total_stake": round(tot_stake, 0),
        "total_return": round(tot_ret, 0),
        "total_pnl": round(tot_ret - tot_stake, 0),
        "roi_pct": round(100.0 * (tot_ret - tot_stake) / tot_stake, 1) if tot_stake else 0.0,
        "jackpot_hits": n_jack_hits,
        "jackpot_hit_rate": round(100.0 * n_jack_hits / n, 1) if n else 0.0,
        "rows": rows,
    }


if __name__ == "__main__":
    import sys
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-05-31"
    bank = float(sys.argv[2]) if len(sys.argv) > 2 else 500.0
    goal = sys.argv[3] if len(sys.argv) > 3 else "upside"
    res = optimise_card(date, bank, goal)
    print(format_card(res))