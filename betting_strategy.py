"""
betting_strategy.py  —  HKJC Banker-Q (QIN / QPL) strategy framework
=====================================================================

DESIGN OVERVIEW
---------------

The user places mostly **Banker + Selections** bets on:
    - QIN  (Quinella)         : pick the top-2, any order
    - QPL  (Quinella Place)   : pick 2 of the top-3
Occasionally WIN singles and box-QIN/QPL when no clear banker exists.

This module produces, for each race, a **ranked shortlist + a Banker
recommendation + legs** by blending every information source already
built into the pipeline:

    1. ET model  (v4.4 preferred, fallback v3.4.8)
          - rank, win_prob, risk flags
    2. SARR model (when available)
          - sarr score, style, place_rate
    3. Factor-analysis edges  (reports/factor_analysis_tables.json)
          - trainer / jockey / class-step A/E and IV buckets
    4. Blackbook  (blackbook.json)
          - manually-tagged horses + conditions
    5. Market odds  (win_odds from results JSON, or live if plugged in)
          - implied probability (de-overrounded)

PIPELINE
--------

For each runner we build a CompositeScore:

    CompositeScore = 0.35 * ET_rank_score
                   + 0.25 * SARR_rank_score         (redistributes if missing)
                   + 0.15 * mutual_top3_bonus
                   + 0.20 * factor_edge_bonus       (capped)
                   + 0.05 * blackbook_bonus

and apply flag adjustments (vet / trial).  We then softmax the scores
across the field to get a model-implied **p_model**.  We compare against
the market-normalised **p_mkt**:

    edge = p_model / p_mkt           (>1.15 = value, <0.7 = overbet)

BANKER ELIGIBILITY
------------------
    - Top-1 by CompositeScore
    - Top-3 in BOTH models when SARR is available (mutual agreement)
    - p_model >= 0.22
    - No adverse vet flag
    - Gap to #2 >= 0.08 in p_model (else we go Box mode)

LEGS SELECTION
--------------
    - Next 3-4 horses by CompositeScore
    - Keep if edge >= 0.80 OR blackbook hit OR mutual-top3
    - Drop anything with p_model < 0.05

STAKING
-------
Flat-bet 1 unit per race.  For a Banker + n legs, each leg gets 1/n of
the unit.  Box-3 QIN gets 1/3 per pair.  WIN singles get the full unit.

DIVIDEND PROXIES  (no actual dividend data available per-race)
--------------------------------------------------------------
We estimate dividends from SP using the classic approximation:

    QIN_div_est  ≈  (odds_A * odds_B) * 0.825 / 2       (~17.5% takeout)
    QPL_div_est  ≈  (odds_A * odds_B) * 0.825 / n_pairs_qpl

This is ROUGH — QIN dividends can under- or over-shoot by ±40%.  We
therefore report both **strike rate** (the primary accuracy metric) and
**ROI_estimate** (value secondary metric).

USAGE
-----

    # Backtest on all April 2026 meetings that have results
    python betting_strategy.py --backtest

    # Produce picks for a specific date (results optional)
    python betting_strategy.py --picks 2026-04-22

    # Dump a CSV of per-race bets
    python betting_strategy.py --backtest --csv reports/bets_backtest.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
from collections import defaultdict as defaultdict_local
from pathlib import Path
from typing import Optional

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
BLACKBOOK_PATH = BASE / "blackbook.json"
FACTOR_TABLES_PATH = REPORTS / "factor_analysis_tables.json"

# ---------------------------------------------------------------------------
# Config — tweak here; backtest CLI will report sensitivity
# ---------------------------------------------------------------------------
CFG = {
    "softmax_tau":        0.38,   # lower = more concentrated; higher = flatter
    "w_et":               0.35,
    "w_sarr":             0.25,
    "w_mutual_top3":      0.15,
    "w_factor":           0.20,
    "w_blackbook":        0.05,
    "trial_plus_bonus":   0.05,
    "trial_minus_pen":    -0.05,
    "vet_flag_pen":       -0.15,
    "ht_overround":       1.175,  # HKJC WIN pool overround, informational only
    "takeout_factor":     0.825,  # QIN/QPL dividend proxy 1 - 17.5%
    # Banker gates
    "banker_min_pmodel":  0.20,
    "banker_min_gap":     0.04,   # top-1 p_model minus top-2 p_model
    "banker_min_edge":    0.70,
    # Legs gates
    "n_legs_target":      4,
    "leg_min_pmodel":     0.05,
    "leg_min_edge":       0.75,
    "value_edge":         1.40,   # any horse with edge ≥ this is included
    # Factor signal
    "factor_min_n":       15,
    "factor_min_ae":      1.30,
    "factor_min_iv":      1.30,
    "factor_window":      "current_season_25_26",
}


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def _load_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_meeting(date_compact: str) -> dict:
    """Returns {et, sarr, results, live_odds} — each may be None if unavailable."""
    et_path = REPORTS / f"race_day_report_{date_compact}_v4.4.json"
    if not et_path.exists():
        et_path = REPORTS / f"race_day_report_{date_compact}_v3.4.8.json"
    sarr_path = REPORTS / f"race_day_report_{date_compact}_SARR.json"
    res_path  = REPORTS / f"results_{date_compact}.json"
    return {
        "et":        _load_json(et_path),
        "sarr":      _load_json(sarr_path),
        "results":   _load_json(res_path),
        "live_odds": _load_live_odds(date_compact),
        "date_compact": date_compact,
    }


def _load_live_odds(date_compact: str) -> dict:
    """Scan cache/live_odds/YYYYMMDD/*.json and return {race_no: {horse_no: win_odds}}"""
    out: dict = {}
    folder = BASE / "cache" / "live_odds" / date_compact
    if not folder.exists():
        return out
    # keep only most recent snapshot per race
    latest: dict = {}
    for f in folder.glob("*.json"):
        parts = f.stem.split("_")  # e.g. HV_R01_153726
        if len(parts) < 3:
            continue
        try:
            rn = int(parts[1].lstrip("R"))
        except ValueError:
            continue
        ts = parts[2]
        if rn not in latest or ts > latest[rn][0]:
            latest[rn] = (ts, f)
    for rn, (_ts, f) in latest.items():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        per_race = {}
        for row in d.get("odds", []):
            try:
                no = int(row.get("no"))
                wo = float(row.get("win"))
                per_race[no] = wo
            except (TypeError, ValueError):
                continue
        if per_race:
            out[rn] = per_race
    return out


def load_blackbook() -> dict:
    bb = _load_json(BLACKBOOK_PATH) or {"entries": []}
    # Active horses only; index by name (upper, stripped)
    idx = {}
    for e in bb.get("entries", []):
        if e.get("status") != "active":
            continue
        name = str(e.get("horse_name", "")).upper().strip()
        if name:
            idx[name] = e
    return idx


def load_factor_tables() -> dict:
    d = _load_json(FACTOR_TABLES_PATH) or {}
    return d.get(CFG["factor_window"], {})


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------
def _rank_score(rank: int, field: int) -> float:
    """Top rank = 1.0, decays by position. Field-size aware."""
    if rank <= 0:
        return 0.0
    # top-1 → 1.0, top-2 → ~0.67, top-3 → ~0.45, top-4 → ~0.30
    return math.exp(-(rank - 1) / 2.5)


def _softmax(scores: list[float], tau: float) -> list[float]:
    if not scores:
        return []
    m = max(scores)
    exps = [math.exp((s - m) / max(tau, 1e-3)) for s in scores]
    z = sum(exps) or 1.0
    return [e / z for e in exps]


def _implied_prob(win_odds: Optional[float]) -> Optional[float]:
    try:
        o = float(win_odds)
        if o <= 1.0:
            return None
        return 1.0 / o
    except (TypeError, ValueError):
        return None


def _normalise_market(odds_list: list[Optional[float]]) -> list[Optional[float]]:
    raw = [_implied_prob(o) for o in odds_list]
    z = sum(p for p in raw if p is not None) or 0.0
    if z <= 0:
        return [None] * len(raw)
    return [None if p is None else p / z for p in raw]


def _lookup_factor_bonus(horse_runner: dict, factor_tbls: dict) -> tuple[float, list[str]]:
    """Return (bonus 0..1 capped, list of matched-signal notes)."""
    notes = []
    bonus = 0.0
    tr = str(horse_runner.get("trainer", "")).strip()
    jk_raw = str(horse_runner.get("jockey", "")).strip()
    # Strip e.g. "P N Wong (-7)" -> "P N Wong"
    jk = jk_raw.split(" (")[0].strip()

    def _hit(tbl_name: str, key_col: str, key_val: str):
        rows = factor_tbls.get(tbl_name, [])
        if not rows or not key_val:
            return None
        for row in rows:
            if str(row.get(key_col, "")).strip() == key_val:
                try:
                    n = float(row.get("N", 0) or 0)
                    ae = float(row.get("A_E", 0) or 0)
                    iv = float(row.get("IV", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if (n >= CFG["factor_min_n"]
                        and ae >= CFG["factor_min_ae"]
                        and iv >= CFG["factor_min_iv"]):
                    return (ae, iv, n)
        return None

    # Trainer general edge
    h = _hit("trainer", "trainer", tr)
    if h:
        bonus += 0.5
        notes.append(f"Trn A/E {h[0]:.2f} IV {h[1]:.2f} N{int(h[2])}")
    # Jockey general edge
    h = _hit("jockey", "jockey", jk)
    if h:
        bonus += 0.4
        notes.append(f"Jky A/E {h[0]:.2f}")
    # Jockey × trainer combo
    # (table key is "jockey_x_trainer" with Bucket cols)
    for row in factor_tbls.get("jockey_x_trainer", []):
        if str(row.get("jockey", "")).strip() == jk and \
           str(row.get("trainer", "")).strip() == tr:
            try:
                n = float(row.get("N", 0) or 0)
                ae = float(row.get("A_E", 0) or 0)
                iv = float(row.get("IV", 0) or 0)
            except (TypeError, ValueError):
                break
            if n >= CFG["factor_min_n"] and ae >= CFG["factor_min_ae"]:
                bonus += 0.4
                notes.append(f"Jky×Trn A/E {ae:.2f}")
            break
    # Trainer class step-up (handled outside — need today's class vs last class)
    # (omitted here because we don't carry last_race_class into per-race picks
    # without a separate lookup; the dashboard's _compute_factor_edges does this
    # at the horse-attribute-lookup level. Kept lightweight on purpose.)
    return min(bonus, 1.0), notes


# ---------------------------------------------------------------------------
# Main scoring
# ---------------------------------------------------------------------------
def score_race(et_race: dict, sarr_race: Optional[dict],
               factor_tbls: dict, blackbook: dict,
               actual_odds_by_no: Optional[dict] = None) -> list[dict]:
    """
    Returns a list of runner dicts (one per horse) with scoring fields added.

    Each runner has:
        horse_no, horse_name, jockey, trainer, draw, weight,
        et_rank, sarr_rank, composite, p_model, p_mkt, edge,
        flags, trial_flag, vet_flag, factor_notes, bb_match, bb_tags
    """
    et_picks = et_race.get("picks", []) or []
    field = len(et_picks) or et_race.get("runners", 0) or 1
    sarr_picks = (sarr_race or {}).get("picks", []) or []
    sarr_by_no = {p["horse_no"]: p for p in sarr_picks}
    have_sarr = len(sarr_by_no) > 0

    # Redistribute SARR weight into ET if SARR absent
    w_et = CFG["w_et"] + (0 if have_sarr else CFG["w_sarr"] * 0.6)
    w_sarr = CFG["w_sarr"] if have_sarr else 0.0
    # Agreement bonus only makes sense when we have two models
    w_mutual = CFG["w_mutual_top3"] if have_sarr else 0.0

    rows: list[dict] = []
    for p in et_picks:
        no = p["horse_no"]
        name = p["horse_name"]
        name_key = str(name).upper().strip()
        sarr = sarr_by_no.get(no, {})
        et_rank = p.get("rank", 99)
        sarr_rank = sarr.get("rank", 99) if have_sarr else None

        # Base score components
        et_s = _rank_score(et_rank, field)
        sarr_s = _rank_score(sarr_rank, field) if have_sarr else 0.0

        # Mutual top-3 agreement
        mutual = (1.0 if (et_rank <= 3 and (sarr_rank or 99) <= 3) else 0.0)

        # Factor bonus
        fac_bonus, fac_notes = _lookup_factor_bonus(p, factor_tbls)

        # Blackbook
        bb_hit = blackbook.get(name_key)
        bb_s = 1.0 if bb_hit else 0.0

        score = (w_et * et_s
                 + w_sarr * sarr_s
                 + w_mutual * mutual
                 + CFG["w_factor"] * fac_bonus
                 + CFG["w_blackbook"] * bb_s)

        # Flag adjustments
        trial_flag = str(p.get("trial_flag", "")).strip()
        vet_flag = str(p.get("vet_flag", "")).strip()
        if trial_flag == "+":
            score += CFG["trial_plus_bonus"]
        elif trial_flag == "-":
            score += CFG["trial_minus_pen"]
        if vet_flag and vet_flag not in ("", "+"):
            score += CFG["vet_flag_pen"]

        rows.append({
            "horse_no": no,
            "horse_name": name,
            "jockey": p.get("jockey"),
            "trainer": sarr.get("trainer") or p.get("trainer"),
            "draw": p.get("draw"),
            "weight": p.get("weight"),
            "et_rank": et_rank,
            "sarr_rank": sarr_rank,
            "style": p.get("style") or sarr.get("style"),
            "trial_flag": trial_flag,
            "vet_flag": vet_flag,
            "_score": score,
            "factor_notes": fac_notes,
            "bb_match": bool(bb_hit),
            "bb_tags": (bb_hit or {}).get("tags", []),
            "bb_reason": (bb_hit or {}).get("reasoning", ""),
        })

    # Softmax composite → p_model
    probs = _softmax([r["_score"] for r in rows], CFG["softmax_tau"])
    for r, p in zip(rows, probs):
        r["p_model"] = round(p, 4)

    # Market probs
    odds_by_no = actual_odds_by_no or {}
    odds = [odds_by_no.get(r["horse_no"]) for r in rows]
    mkt = _normalise_market(odds)
    for r, pm, o in zip(rows, mkt, odds):
        r["win_odds"] = o
        r["p_mkt"] = None if pm is None else round(pm, 4)
        if pm and pm > 0:
            r["edge"] = round(r["p_model"] / pm, 2)
        else:
            r["edge"] = None

    # Sort by composite score descending
    rows.sort(key=lambda r: r["_score"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["composite_rank"] = i
    return rows


# ---------------------------------------------------------------------------
# Bet selection
# ---------------------------------------------------------------------------
def select_bets(rows: list[dict]) -> dict:
    """
    Returns:
        {
            "mode":   "banker_qpl" | "box_qin" | "skip",
            "banker": runner dict or None,
            "legs":   list of runner dicts,
            "value":  list of runner dicts flagged as value (edge >= 1.4),
            "reason": str
        }
    """
    if not rows:
        return {"mode": "skip", "banker": None, "legs": [], "value": [],
                "reason": "no runners"}

    r1 = rows[0]
    r2 = rows[1] if len(rows) > 1 else None

    # Value picks (regardless of mode)
    value = [r for r in rows
             if r.get("edge") is not None and r["edge"] >= CFG["value_edge"]
             and r["p_model"] >= 0.05]

    # Banker eligibility
    banker_ok = True
    reasons = []
    if r1["p_model"] < CFG["banker_min_pmodel"]:
        banker_ok = False
        reasons.append(f"top p_model {r1['p_model']:.2f} < {CFG['banker_min_pmodel']}")
    if r2 and (r1["p_model"] - r2["p_model"]) < CFG["banker_min_gap"]:
        banker_ok = False
        reasons.append(f"gap to #2 only {r1['p_model']-r2['p_model']:.3f}")
    if r1.get("edge") is not None and r1["edge"] < CFG["banker_min_edge"]:
        banker_ok = False
        reasons.append(f"top edge {r1['edge']:.2f} < {CFG['banker_min_edge']}")
    if r1.get("vet_flag") and r1["vet_flag"] not in ("", "+"):
        banker_ok = False
        reasons.append(f"vet flag {r1['vet_flag']}")

    if banker_ok:
        legs: list[dict] = []
        for r in rows[1:]:
            if r["p_model"] < CFG["leg_min_pmodel"]:
                continue
            if (r.get("edge") is None or r["edge"] >= CFG["leg_min_edge"]
                    or r["bb_match"] or r["composite_rank"] <= 4):
                legs.append(r)
            if len(legs) >= CFG["n_legs_target"]:
                break
        # Always include value picks among legs even if outside top-4
        for v in value:
            if v is r1 or v in legs:
                continue
            legs.append(v)
        return {"mode": "banker_qpl", "banker": r1, "legs": legs,
                "value": value, "reason": "banker OK"}

    # No banker — Box mode (top 3-4 by composite within p_model and edge filter)
    box = [r for r in rows[:4] if r["p_model"] >= CFG["leg_min_pmodel"]]
    # Always include value picks
    for v in value:
        if v not in box:
            box.append(v)
    return {"mode": "box_qin", "banker": None, "legs": box, "value": value,
            "reason": "; ".join(reasons) or "banker gated"}


# ---------------------------------------------------------------------------
# Evaluation vs results
# ---------------------------------------------------------------------------
def _results_race_by_no(race: dict) -> dict:
    out = {}
    for h in race.get("runners", []):
        try:
            place = int(h.get("place"))
        except (TypeError, ValueError):
            place = None
        no = h.get("horse_no")
        try:
            wo = float(h.get("win_odds")) if h.get("win_odds") not in (None, "", "---") else None
        except (TypeError, ValueError):
            wo = None
        out[no] = {
            "place": place,
            "win_odds": wo,
            "horse_name": h.get("horse_name"),
        }
    return out


def evaluate_race(bet: dict, res_by_no: dict) -> dict:
    """Compute strike-rate + rough ROI for the selected bet structure."""
    finishers = {no: r for no, r in res_by_no.items() if r["place"] is not None}
    top2 = sorted(finishers.items(), key=lambda kv: kv[1]["place"])[:2]
    top3 = sorted(finishers.items(), key=lambda kv: kv[1]["place"])[:3]
    top2_nos = {k for k, _ in top2}
    top3_nos = {k for k, _ in top3}

    mode = bet["mode"]
    out = {
        "mode": mode, "stake": 1.0, "return": 0.0, "pnl": -1.0,
        "win_hit": False, "qin_hit": False, "qpl_hit": False,
        "banker_finish": None, "legs_finish": [],
    }
    if mode == "skip":
        out["stake"] = 0.0
        out["pnl"] = 0.0
        return out

    banker = bet.get("banker")
    legs = bet["legs"]

    # Record actual finishing positions
    if banker:
        out["banker_finish"] = res_by_no.get(banker["horse_no"], {}).get("place")
    out["legs_finish"] = [(l["horse_no"],
                            res_by_no.get(l["horse_no"], {}).get("place"))
                           for l in legs]

    # WIN hit — only pays if we had a banker and it won
    if banker and res_by_no.get(banker["horse_no"], {}).get("place") == 1:
        out["win_hit"] = True

    # QIN / QPL hit logic
    if mode == "banker_qpl" and banker:
        b_no = banker["horse_no"]
        # Banker + one leg covers one QIN combo & one QPL combo each
        banker_in_top2 = b_no in top2_nos
        banker_in_top3 = b_no in top3_nos
        hit_qin_legs = [l for l in legs
                         if l["horse_no"] in top2_nos and banker_in_top2]
        hit_qpl_legs = [l for l in legs
                         if l["horse_no"] in top3_nos and banker_in_top3]
        out["qin_hit"] = bool(hit_qin_legs)
        out["qpl_hit"] = bool(hit_qpl_legs)
        # Stake = 1 unit split across n legs. Winning leg pays div_est.
        n = max(len(legs), 1)
        per_leg = 1.0 / n
        ret = 0.0
        # QIN estimated dividend using SP
        if hit_qin_legs:
            l = hit_qin_legs[0]
            o1 = res_by_no.get(b_no, {}).get("win_odds") or 0
            o2 = res_by_no.get(l["horse_no"], {}).get("win_odds") or 0
            # Use QPL bet style: user actually plays QPL most often
            # so we also compute QPL estimate below; reporting QIN separately
        # Primary reported bet: QPL Banker (what user does most)
        if hit_qpl_legs:
            l = hit_qpl_legs[0]
            o1 = res_by_no.get(b_no, {}).get("win_odds") or 0
            o2 = res_by_no.get(l["horse_no"], {}).get("win_odds") or 0
            # QPL dividend rough proxy (takeout 17.5%, 3 possible pairs)
            div_est = (o1 * o2) * CFG["takeout_factor"] / 3.0
            ret = per_leg * div_est
        out["return"] = ret
        out["pnl"] = ret - 1.0
    elif mode == "box_qin":
        # Box QIN: C(n,2) combos, equal stakes
        from itertools import combinations
        nos = [l["horse_no"] for l in legs]
        pairs = list(combinations(nos, 2))
        if not pairs:
            out["stake"] = 0.0; out["pnl"] = 0.0
            return out
        per = 1.0 / len(pairs)
        hit_pair = None
        for a, b in pairs:
            if a in top2_nos and b in top2_nos:
                hit_pair = (a, b)
                break
        out["qin_hit"] = hit_pair is not None
        # Also track QPL-ish hits (any pair in top 3)
        for a, b in pairs:
            if a in top3_nos and b in top3_nos:
                out["qpl_hit"] = True
                break
        ret = 0.0
        if hit_pair:
            oA = res_by_no.get(hit_pair[0], {}).get("win_odds") or 0
            oB = res_by_no.get(hit_pair[1], {}).get("win_odds") or 0
            div_est = (oA * oB) * CFG["takeout_factor"] / 2.0
            ret = per * div_est
        out["return"] = ret
        out["pnl"] = ret - 1.0
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def format_bet_line(race: dict, rows: list[dict], bet: dict,
                    eval_out: Optional[dict] = None) -> str:
    rn = race.get("race_number")
    cls = race.get("race_class")
    dist = race.get("distance")
    course = race.get("race_course")
    hdr = f"R{rn} · Cls{cls} · {dist}m {course}"
    if bet["mode"] == "skip":
        return f"{hdr} :: SKIP ({bet['reason']})"
    lines = [hdr]
    if bet["mode"] == "banker_qpl":
        b = bet["banker"]
        pmkt = f"{b['p_mkt']:.2f}" if b.get('p_mkt') is not None else "--"
        edge = b.get('edge') if b.get('edge') is not None else "--"
        ind = (f"B: #{b['horse_no']} {b['horse_name']} "
               f"(p_mod {b['p_model']:.2f}, p_mkt {pmkt} -> "
               f"edge {edge}, {b.get('jockey','')})")
        lines.append(ind)
        lines.append("  Legs:")
        for l in bet["legs"]:
            bb = " [BB]" if l["bb_match"] else ""
            fn = " " + "; ".join(l["factor_notes"]) if l["factor_notes"] else ""
            pmkt = f"{l['p_mkt']:.3f}" if l.get('p_mkt') is not None else "--"
            edge = l.get('edge') if l.get('edge') is not None else "--"
            lines.append(f"   #{l['horse_no']} {l['horse_name']:<22} "
                         f"p_mod {l['p_model']:.2f}  p_mkt {pmkt}  "
                         f"edge {edge}{bb}{fn}")
    else:  # box_qin
        lines.append(f"  BOX ({bet['reason']}):")
        for l in bet["legs"]:
            bb = " [BB]" if l["bb_match"] else ""
            edge = l.get('edge') if l.get('edge') is not None else "--"
            lines.append(f"   #{l['horse_no']} {l['horse_name']:<22} "
                         f"p_mod {l['p_model']:.2f}  edge {edge}{bb}")
    if bet["value"]:
        lines.append("  Value picks (edge ≥ "
                     f"{CFG['value_edge']}): "
                     + ", ".join(f"#{v['horse_no']} {v['horse_name']}"
                                 f"(edge {v['edge']})" for v in bet["value"]))
    if eval_out and eval_out["mode"] != "skip":
        tag = []
        if eval_out["win_hit"]: tag.append("WIN")
        if eval_out["qin_hit"]: tag.append("QIN")
        if eval_out["qpl_hit"]: tag.append("QPL")
        status = "✓ " + "/".join(tag) if tag else "✗"
        lines.append(f"  → {status}  pnl≈{eval_out['pnl']:+.2f}  "
                     f"banker_fin={eval_out['banker_finish']} "
                     f"legs_fin={eval_out['legs_finish']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Meeting runner
# ---------------------------------------------------------------------------
def run_meeting(date_compact: str, blackbook: dict, factor_tbls: dict,
                verbose: bool = True) -> dict:
    meet = load_meeting(date_compact)
    et = meet["et"]
    sarr = meet["sarr"]
    results = meet["results"]
    if et is None:
        if verbose:
            print(f"[{date_compact}] no ET report — skipping")
        return {"date": date_compact, "races": [], "summary": None}

    sarr_by_no = {r["race_number"]: r for r in (sarr["races"] if sarr else [])}
    res_by_no = {r["race_number"]: r for r in (results["races"] if results else [])}
    live_odds = meet.get("live_odds") or {}

    race_logs = []
    total_stake = total_ret = 0.0
    n_bets = n_win = n_qin = n_qpl = n_skip = 0
    for race in et["races"]:
        rn = race["race_number"]
        sarr_race = sarr_by_no.get(rn)
        res_race = res_by_no.get(rn)
        res_odds = {}
        res_by_no_race = {}
        if res_race:
            res_by_no_race = _results_race_by_no(res_race)
            res_odds = {no: v["win_odds"] for no, v in res_by_no_race.items()}
        # Prefer live odds if no results-SP available for this race
        if not res_odds and rn in live_odds:
            res_odds = live_odds[rn]
        rows = score_race(race, sarr_race, factor_tbls, blackbook,
                          actual_odds_by_no=res_odds)
        bet = select_bets(rows)
        ev = None
        if res_race:
            ev = evaluate_race(bet, res_by_no_race)
            total_stake += ev["stake"]
            total_ret += ev["return"]
            if bet["mode"] == "skip":
                n_skip += 1
            else:
                n_bets += 1
                if ev["win_hit"]: n_win += 1
                if ev["qin_hit"]: n_qin += 1
                if ev["qpl_hit"]: n_qpl += 1
        if verbose:
            print(format_bet_line(race, rows, bet, ev))
            print()
        race_logs.append({"race_number": rn, "bet": bet, "eval": ev,
                          "rows": rows})

    summary = None
    if res_by_no:
        roi = (total_ret - total_stake) / total_stake if total_stake > 0 else 0.0
        summary = {
            "n_bets": n_bets, "n_skip": n_skip,
            "qpl_strike": n_qpl / n_bets if n_bets else 0.0,
            "qin_strike": n_qin / n_bets if n_bets else 0.0,
            "win_strike": n_win / n_bets if n_bets else 0.0,
            "total_stake": total_stake, "total_return": total_ret,
            "roi_est": roi,
        }
        if verbose:
            print(f"--- {date_compact} SUMMARY ({n_bets} bets, {n_skip} skipped) ---")
            print(f"   QPL strike: {summary['qpl_strike']:.1%}")
            print(f"   QIN strike: {summary['qin_strike']:.1%}")
            print(f"   WIN strike: {summary['win_strike']:.1%}")
            print(f"   Stake: {total_stake:.1f}  Return: {total_ret:.1f}  "
                  f"ROI_est: {roi:+.1%}")
    return {"date": date_compact, "races": race_logs, "summary": summary}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
APRIL_DATES = ["20260401", "20260406", "20260408",
               "20260412", "20260415", "20260419"]


def _csv_dump(path: str, meetings: list[dict]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "race", "mode", "banker_no", "banker_name",
                    "legs", "banker_finish", "legs_finish",
                    "win_hit", "qin_hit", "qpl_hit", "pnl_est"])
        for m in meetings:
            for r in m["races"]:
                bet = r["bet"]
                ev = r["eval"] or {}
                b = bet.get("banker") or {}
                legs_str = "|".join(f"#{l['horse_no']} {l['horse_name']}"
                                    for l in bet.get("legs", []))
                w.writerow([
                    m["date"], r["race_number"], bet["mode"],
                    b.get("horse_no"), b.get("horse_name"), legs_str,
                    ev.get("banker_finish"),
                    "|".join(f"{a}:{b}" for a, b in ev.get("legs_finish", [])),
                    ev.get("win_hit"), ev.get("qin_hit"), ev.get("qpl_hit"),
                    f"{ev.get('pnl', 0):.2f}" if ev else "",
                ])


# ---------------------------------------------------------------------------
# v4.6 — Filter-based ticket builder (uses empirical April edges)
# ---------------------------------------------------------------------------
#
# Empirical April 2026 findings (59 races, real HKJC dividends):
#   WIN top-1 · Cls3-5 · SP 3-8       : 28 bets, 42.9% hit, +120% ROI
#   QPL banker · mutual+gap>=0.08     :  5 bets, 80% hit, +88% ROI
#   QIN banker · Cls3-5 · SP 5-8 and
#     NOT mutual (market edge only)   : 29 bets, 24.1% hit, +10.2% ROI
#   PLACE top-1 · Cls3-5 · SP<8       : 38 bets, 63.2% hit, +2.7% ROI
#
# Rule of thumb: when SARR+ET mutually agree on top-3 with a wide gap,
# the model is much stronger at "top-3 finish" than "top-2 finish" so QPL
# outperforms QIN. Absent that mutual signal, any banker-Q bet that hits
# at all relies on the SP band → QIN pays better for the same structure.
# ---------------------------------------------------------------------------

# Edge thresholds derived from April 2026 analysis.
EDGE_CFG = {
    # ------------------------------------------------------------------
    # Core class / SP gates (April 2026 empirical edges)
    # ------------------------------------------------------------------
    "win_class_ok":       {"3", "4", "5"},
    "win_sp_min":         3.0,
    "win_sp_max":         8.0,
    "place_sp_max":       8.0,
    "qin_sp_min":         3.0,
    "qin_sp_max":         8.0,
    "qin_class_ok":       {"3", "4", "5"},
    "banker_pmodel_min":  0.20,

    # ------------------------------------------------------------------
    # QPL banker — now a narrow fallback only when QIN isn't fireable
    # ------------------------------------------------------------------
    "qpl_mutual_required": True,
    "qpl_gap_min":        0.10,        # raised: only extreme conviction

    # ------------------------------------------------------------------
    # Flexible leg count (was hardcoded 2)
    # ------------------------------------------------------------------
    "legs_min":           2,
    "legs_max":           4,
    "legs_gap_tight":     0.030,       # gap between consecutive legs < this
                                        # → keep the extra leg (field is spread)
    "leg_min_pmodel":     0.05,

    # ------------------------------------------------------------------
    # Value gates (market edge flagging)
    # ------------------------------------------------------------------
    "value_edge_min":     1.25,        # overlay: edge ≥ this → value pick
    "value_pmodel_min":   0.10,
    "chalk_edge_max":     0.90,        # top pick trading as overbet favourite
    "chalk_sp_max":       3.0,         # SP < this is the "hot fav" band

    # ------------------------------------------------------------------
    # Confidence-based stake sizing (multipliers on the 1u base)
    # Output is in units; dashboard converts to HKD at $10/unit.
    # ------------------------------------------------------------------
    "stake_base":         1.0,
    "stake_bonus_mutual": 1.0,         # +1u if SARR & ET agree top-3
    "stake_bonus_gap":    1.0,         # +1u if top-1 gap to #2 ≥ 0.08
    "stake_bonus_value":  1.0,         # +1u if top-1 edge ≥ 1.2
    "stake_cap":          5.0,         # cap regardless of signals

    # ------------------------------------------------------------------
    # F4 box top-5 — ultra-high conviction only
    # ------------------------------------------------------------------
    "f4_field_min":       10,
    "f4_top5_mass_min":   0.55,        # sum of p_model over top-5 ≥ this
    "f4_gap_min":         0.05,        # top-1 gap to #2
    "f4_stake":           0.5,         # 0.5u = ~$5 per combo if base is $10

    # ------------------------------------------------------------------
    # Hedge (longshot cover): when banker SP is very short, cheap cover
    # on composite-rank 4-6 horses with SP ≥ 10.
    # ------------------------------------------------------------------
    "hedge_trigger_sp":   4.0,         # banker SP below → consider hedge
    "hedge_longshot_min": 10.0,        # cover candidate SP ≥ this
    "hedge_rank_range":   (4, 6),      # composite rank range for cover legs
    "hedge_stake":        0.2,         # per pair

    # HKD-per-unit conversion (dashboard consumes this)
    "hkd_per_unit":       10.0,
}


def _class_key(race: dict) -> str:
    """Extract numeric class token from 'Class 3' / 'Cls3' / '3' etc."""
    raw = str(race.get("race_class", "")).strip()
    m = re.search(r"(\d+)", raw)
    return m.group(1) if m else ""


def _mutual_top3(top: dict) -> bool:
    if top.get("sarr_rank") is None:
        return False
    return top["sarr_rank"] <= 3 and top.get("et_rank", 99) <= 3


def _choose_n_legs(rows: list[dict], gap_top12: float) -> int:
    """Pick 2-4 legs based on field spread.

    - gap_top12 ≥ 0.10: very concentrated → 2 legs (cheap, high per-combo ROI)
    - gap_top12 ≥ 0.04: standard → 3 legs
    - otherwise: 4 legs (field is spread, wider net required)
    Clamped by EDGE_CFG["legs_min"/"legs_max"] and available runners.
    """
    available = max(0, len(rows) - 1)
    if gap_top12 >= 0.10:
        n = 2
    elif gap_top12 >= 0.04:
        n = 3
    else:
        n = 4
    n = max(EDGE_CFG["legs_min"], min(n, EDGE_CFG["legs_max"]))
    return min(n, available)


def _value_overlays(rows: list[dict]) -> list[dict]:
    """Flag non-top runners that show market value (edge ≥ threshold)."""
    out = []
    if not rows:
        return out
    top_no = rows[0]["horse_no"]
    for r in rows[1:]:
        edge = r.get("edge")
        if edge is None:
            continue
        if (edge >= EDGE_CFG["value_edge_min"]
                and r["p_model"] >= EDGE_CFG["value_pmodel_min"]
                and r["horse_no"] != top_no):
            out.append(r)
    return out[:3]   # cap at 3 overlays to avoid scatter


def _compute_stake_units(top: dict, mutual: bool, gap: float) -> tuple[float, list[str]]:
    """Scale stake by conviction signals. Returns (units, reasons)."""
    units = EDGE_CFG["stake_base"]
    reasons = [f"base {units:.1f}u"]
    if mutual:
        units += EDGE_CFG["stake_bonus_mutual"]
        reasons.append(f"+{EDGE_CFG['stake_bonus_mutual']:.1f}u mutual-top3")
    if gap >= 0.08:
        units += EDGE_CFG["stake_bonus_gap"]
        reasons.append(f"+{EDGE_CFG['stake_bonus_gap']:.1f}u gap {gap:.2f}")
    edge = top.get("edge")
    if edge is not None and edge >= 1.2:
        units += EDGE_CFG["stake_bonus_value"]
        reasons.append(f"+{EDGE_CFG['stake_bonus_value']:.1f}u edge {edge:.2f}")
    units = min(units, EDGE_CFG["stake_cap"])
    return round(units, 2), reasons


def _confidence_tier(units: float) -> str:
    if units >= 4.0: return "max"
    if units >= 3.0: return "high"
    if units >= 2.0: return "med"
    return "low"


def _maybe_f4_box(race: dict, rows: list[dict], mutual: bool,
                    gap: float) -> Optional[dict]:
    """Evaluate F4 box top-5 eligibility.

    F4 is the Pick-4 finish in any-order exacta-style bet. Here we interpret
    "F4 box top-5" as boxing the top-5 composite picks as a 4-horse box
    (i.e. the user plays first-4 box over their top-5 → C(5,4)=5 combinations,
    at 0.5u per combination). Fires ONLY on ultra-high conviction to keep
    variance bounded.
    """
    cls = _class_key(race)
    if cls not in EDGE_CFG["win_class_ok"]:
        return None
    if len(rows) < 5:
        return None
    if not mutual:
        return None
    if gap < EDGE_CFG["f4_gap_min"]:
        return None
    field = len(rows)
    if field < EDGE_CFG["f4_field_min"]:
        return None
    top5_mass = sum(r.get("p_model", 0.0) for r in rows[:5])
    if top5_mass < EDGE_CFG["f4_top5_mass_min"]:
        return None
    return {
        "play": "F4_BOX_TOP5",
        "horses": rows[:5],
        "n_combos": 5,                                   # C(5,4)
        "stake_units_per_combo": EDGE_CFG["f4_stake"],
        "stake_units": EDGE_CFG["f4_stake"] * 5,
        "reason": (f"Cls{cls}, field {field}, mutual+gap {gap:.2f}, "
                    f"top-5 p-mass {top5_mass:.2f}"),
    }


def _maybe_hedge(top: dict, rows: list[dict]) -> Optional[dict]:
    """Longshot cover: when banker is a short-priced favourite, box
    composite-rank 4-6 horses with SP ≥ 10 as cheap insurance."""
    sp = top.get("win_odds")
    if sp is None or sp >= EDGE_CFG["hedge_trigger_sp"]:
        return None
    lo, hi = EDGE_CFG["hedge_rank_range"]
    candidates = []
    for r in rows:
        rank = r.get("composite_rank", 99)
        r_sp = r.get("win_odds")
        if lo <= rank <= hi and r_sp is not None \
                and r_sp >= EDGE_CFG["hedge_longshot_min"]:
            candidates.append(r)
    if len(candidates) < 2:
        return None
    from itertools import combinations
    pairs = list(combinations(candidates[:3], 2))
    return {
        "type": "QIN_LONGSHOT_COVER",
        "pairs": [(a["horse_no"], a["horse_name"],
                    b["horse_no"], b["horse_name"]) for a, b in pairs],
        "stake_units_per_pair": EDGE_CFG["hedge_stake"],
        "stake_units": EDGE_CFG["hedge_stake"] * len(pairs),
        "reason": (f"Banker SP {sp:.1f} — covering rank {lo}-{hi} "
                    f"longshots (SP≥{EDGE_CFG['hedge_longshot_min']})"),
    }


def build_model_ticket(race: dict, rows: list[dict],
                        live_odds: Optional[dict] = None) -> dict:
    """Return the recommended ticket for a race.

    Philosophy (post-user-feedback v4.7):
      • QIN is primary (flexible legs 2-4, scaled stake) — positive ROI edge.
      • QPL is a narrow fallback only when QIN isn't fireable + strong mutual+gap.
      • F4 box top-5 fires only on ultra-high conviction (bounded variance).
      • Value overlays added as `extras` when any non-top runner has edge ≥ 1.25.
      • Stakes scale with conviction (base 1u → up to 5u).
      • Hedge added when banker is a hot favourite (SP < 4): cheap longshot cover.

    Output schema:
        {
            "play": "WIN" | "QIN_BANKER" | "QPL_BANKER" | "PLACE"
                      | "F4_BOX_TOP5" | "SKIP",
            "banker":   runner | None,
            "legs":     [runner, ...],   # 2-4 typically
            "n_combos": int,              # number of $10-min combos
            "stake_units":         float, # primary stake total
            "stake_units_per_combo": float, # usually stake/n_combos
            "stake_hkd_min":       float, # HKD at $10/unit (informational)
            "confidence":  "low"|"med"|"high"|"max",
            "stake_reasons": [str],
            "extras":       [ {play,horse,stake_units,reason}, ... ],
            "hedge":        dict | None,
            "f4":           dict | None,  # F4 box plan if eligible
            "reason":      str,
            "filter":      str,
        }
    """
    if not rows:
        return {"play": "SKIP", "banker": None, "legs": [], "n_combos": 0,
                "stake_units": 0.0, "stake_units_per_combo": 0.0,
                "stake_hkd_min": 0.0, "confidence": "low",
                "stake_reasons": [], "extras": [], "hedge": None, "f4": None,
                "reason": "no runners", "filter": "no_rows"}

    top = rows[0]
    top2 = rows[1] if len(rows) > 1 else None
    cls = _class_key(race)

    # Odds: prefer live, fall back to stored win_odds on the row
    sp = top.get("win_odds")
    if live_odds and top["horse_no"] in live_odds:
        sp = live_odds[top["horse_no"]]

    mutual = _mutual_top3(top)
    gap = (top["p_model"] - top2["p_model"]) if top2 else 0.0
    pmodel = top.get("p_model", 0.0)

    value_overlays = _value_overlays(rows)
    extras: list[dict] = []
    for v in value_overlays:
        extras.append({
            "play": "WIN",
            "horse_no": v["horse_no"],
            "horse_name": v["horse_name"],
            "sp": v.get("win_odds"),
            "edge": v.get("edge"),
            "stake_units": 0.5,
            "reason": (f"value overlay: edge {v['edge']:.2f} "
                        f"p_mod {v['p_model']:.2f}"),
        })

    f4_plan = _maybe_f4_box(race, rows, mutual, gap)

    def _finalize(play: str, banker, legs, filter_tag, reason_str,
                    base_units: Optional[float] = None,
                    stake_reasons: Optional[list] = None,
                    n_combos_override: Optional[int] = None):
        units = base_units if base_units is not None else EDGE_CFG["stake_base"]
        n_combos = n_combos_override if n_combos_override is not None else \
            (len(legs) if play in ("QIN_BANKER", "QPL_BANKER") and legs else 1)
        per_combo = units / n_combos if n_combos else units
        hedge = _maybe_hedge(banker, rows) if play == "QIN_BANKER" and banker else None
        return {
            "play":   play,
            "banker": banker,
            "legs":   legs,
            "n_combos": n_combos,
            "stake_units":           round(units, 2),
            "stake_units_per_combo": round(per_combo, 2),
            "stake_hkd_min":         round(units * EDGE_CFG["hkd_per_unit"], 1),
            "confidence": _confidence_tier(units),
            "stake_reasons": stake_reasons or [],
            "extras": extras,
            "hedge":  hedge,
            "f4":     f4_plan,
            "reason": reason_str,
            "filter": filter_tag,
        }

    # ───────────────────────────────────────────────────────────────
    # Priority 1: QIN banker (primary, flexible legs, scaled stake)
    # ───────────────────────────────────────────────────────────────
    if (cls in EDGE_CFG["qin_class_ok"] and sp is not None
            and EDGE_CFG["qin_sp_min"] <= sp <= EDGE_CFG["qin_sp_max"]
            and pmodel >= EDGE_CFG["banker_pmodel_min"]):
        n_legs = _choose_n_legs(rows, gap)
        legs = rows[1:1 + n_legs]
        units, reasons = _compute_stake_units(top, mutual, gap)
        filt = f"QIN Cls{cls}+SP{EDGE_CFG['qin_sp_min']:.0f}-{EDGE_CFG['qin_sp_max']:.0f}"
        if mutual: filt += "+mutual"
        return _finalize(
            "QIN_BANKER", top, legs, filt,
            (f"Cls{cls}, SP {sp:.1f}; {n_legs} legs (gap {gap:.2f}); "
             + ", ".join(reasons)),
            base_units=units, stake_reasons=reasons,
        )

    # ───────────────────────────────────────────────────────────────
    # Priority 2: WIN single on value-overlay top pick
    # Used when top-pick's edge itself is strong (not chalk).
    # ───────────────────────────────────────────────────────────────
    top_edge = top.get("edge")
    if (cls in EDGE_CFG["win_class_ok"] and sp is not None
            and EDGE_CFG["win_sp_min"] <= sp <= EDGE_CFG["win_sp_max"]
            and top_edge is not None and top_edge >= 1.2
            and pmodel >= 0.25):
        units = 2.0 if top_edge >= 1.4 else 1.0
        reasons = [f"WIN value — edge {top_edge:.2f}"]
        return _finalize(
            "WIN", top, [], "WIN value (edge≥1.2)",
            f"Cls{cls}, SP {sp:.1f}, edge {top_edge:.2f} → WIN {units:.0f}u",
            base_units=units, stake_reasons=reasons, n_combos_override=1,
        )

    # ───────────────────────────────────────────────────────────────
    # Priority 3: QPL banker — narrow fallback on extreme mutual+gap
    # when QIN wasn't fireable (e.g. SP outside band).
    # ───────────────────────────────────────────────────────────────
    if (mutual and gap >= EDGE_CFG["qpl_gap_min"]
            and pmodel >= EDGE_CFG["banker_pmodel_min"]):
        n_legs = max(3, _choose_n_legs(rows, gap))
        legs = rows[1:1 + n_legs]
        units, reasons = _compute_stake_units(top, mutual, gap)
        return _finalize(
            "QPL_BANKER", top, legs, "QPL mutual+gap (fallback)",
            f"Extreme mutual+gap {gap:.2f}, {n_legs} legs; "
            + ", ".join(reasons),
            base_units=units, stake_reasons=reasons,
        )

    # ───────────────────────────────────────────────────────────────
    # Priority 4: PLACE safety (Cls3-5 + SP in band + low conviction gap)
    # ───────────────────────────────────────────────────────────────
    if (cls in EDGE_CFG["win_class_ok"] and sp is not None
            and EDGE_CFG["win_sp_min"] <= sp <= EDGE_CFG["place_sp_max"]
            and gap < 0.04):
        return _finalize(
            "PLACE", top, [], "PLACE low-gap safety",
            f"Cls{cls}, SP {sp:.1f}, low gap {gap:.2f} → PLACE",
            base_units=0.5, stake_reasons=["base 0.5u (low conviction)"],
            n_combos_override=1,
        )

    # ───────────────────────────────────────────────────────────────
    # SKIP — no edge. F4 plan (if any) still surfaced via extras.
    # ───────────────────────────────────────────────────────────────
    skip_reasons = []
    if cls not in EDGE_CFG["win_class_ok"]:
        skip_reasons.append(f"Cls{cls} outside sweet spot")
    if sp is None:
        skip_reasons.append("no odds")
    elif sp > EDGE_CFG["qin_sp_max"]:
        skip_reasons.append(f"SP {sp:.1f} > {EDGE_CFG['qin_sp_max']}")
    elif sp < EDGE_CFG["qin_sp_min"]:
        skip_reasons.append(f"SP {sp:.1f} < {EDGE_CFG['qin_sp_min']} (chalk)")
    if pmodel < EDGE_CFG["banker_pmodel_min"]:
        skip_reasons.append(f"p_model {pmodel:.2f} low")

    return {
        "play": "SKIP",
        "banker": None, "legs": [], "n_combos": 0,
        "stake_units": 0.0, "stake_units_per_combo": 0.0,
        "stake_hkd_min": 0.0, "confidence": "low",
        "stake_reasons": [], "extras": extras, "hedge": None, "f4": f4_plan,
        "reason": "; ".join(skip_reasons) or "no edge",
        "filter": "no_edge",
    }


def build_meeting_tickets(date_compact: str,
                            blackbook: Optional[dict] = None,
                            factor_tbls: Optional[dict] = None) -> list[dict]:
    """Build model tickets for every race on a meeting.

    Returns a list of {race_number, race_class, distance, course, ticket, top_rows}.
    """
    if blackbook is None:
        blackbook = load_blackbook()
    if factor_tbls is None:
        factor_tbls = load_factor_tables()
    meet = load_meeting(date_compact)
    et = meet["et"]
    if et is None:
        return []
    sarr_by_no = {r["race_number"]: r
                   for r in (meet["sarr"]["races"] if meet["sarr"] else [])}
    res_by_no = {r["race_number"]: r
                  for r in (meet["results"]["races"]
                              if meet["results"] else [])}
    live = meet.get("live_odds") or {}

    out: list[dict] = []
    for race in et["races"]:
        rn = race["race_number"]
        sarr_race = sarr_by_no.get(rn)
        res_race = res_by_no.get(rn)
        if res_race:
            rb = _results_race_by_no(res_race)
            odds_by = {no: v["win_odds"] for no, v in rb.items()}
        else:
            odds_by = live.get(rn, {})
        rows = score_race(race, sarr_race, factor_tbls, blackbook,
                           actual_odds_by_no=odds_by)
        ticket = build_model_ticket(race, rows, live_odds=live.get(rn))
        out.append({
            "race_number": rn,
            "race_class": race.get("race_class"),
            "distance": race.get("distance"),
            "course": race.get("race_course"),
            "ticket": ticket,
            "rows": rows,
        })
    return out


# ---------------------------------------------------------------------------
# v4.6 — Picks log (tracks what the model recommended for later ROI audit)
# ---------------------------------------------------------------------------
PICKS_LOG_PATH = REPORTS / "model_picks_log.jsonl"


def _auto_refresh_picks_log() -> int:
    """Ensure every analysed meeting on disk is represented in the picks log.

    Scans ``reports/race_day_report_*_v4.4.json`` and, for any meeting whose
    date is not already in the log (or whose analysis JSON is newer than the
    log), regenerates the picks via :func:`build_meeting_tickets` and writes
    them via :func:`log_meeting_picks`. Returns the number of meetings
    refreshed.
    """
    pattern = "race_day_report_*_v4.4.json"
    analysis_files = sorted(REPORTS.glob(pattern))
    if not analysis_files:
        return 0

    existing_dates: set[str] = set()
    log_mtime = 0.0
    if PICKS_LOG_PATH.exists():
        log_mtime = PICKS_LOG_PATH.stat().st_mtime
        for ln in PICKS_LOG_PATH.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                existing_dates.add(json.loads(ln)["date"])
            except Exception:
                continue

    refreshed = 0
    bb = factor_tbls = None
    for fp in analysis_files:
        # Extract YYYYMMDD from filename
        stem = fp.stem  # race_day_report_YYYYMMDD_v4.4
        parts = stem.split("_")
        if len(parts) < 4:
            continue
        date_compact = parts[3]
        if not (len(date_compact) == 8 and date_compact.isdigit()):
            continue
        is_new = date_compact not in existing_dates
        is_stale = fp.stat().st_mtime > log_mtime
        if not (is_new or is_stale):
            continue
        try:
            if bb is None:
                bb = load_blackbook()
                factor_tbls = load_factor_tables()
            items = build_meeting_tickets(date_compact, bb, factor_tbls)
            if not items:
                continue
            # Venue: best-effort from the analysis JSON
            venue = ""
            try:
                meta = json.loads(fp.read_text(encoding="utf-8"))
                venue = meta.get("venue") or meta.get("meeting_venue") or ""
            except Exception:
                pass
            log_meeting_picks(date_compact, venue, items)
            refreshed += 1
        except Exception as exc:
            print(f"[auto-refresh] {date_compact}: {exc}")
    return refreshed


def _ticket_log_row(date_compact: str, venue: str, meeting_item: dict) -> dict:
    """Normalise a meeting_item (from build_meeting_tickets) into a log row."""
    t = meeting_item["ticket"]
    b = t.get("banker") or {}
    hedge = t.get("hedge") or {}
    f4 = t.get("f4") or {}
    return {
        "date": date_compact,
        "venue": venue,
        "race_number": meeting_item["race_number"],
        "race_class": str(meeting_item.get("race_class") or ""),
        "distance":   meeting_item.get("distance"),
        "course":     meeting_item.get("course"),
        "play":       t["play"],
        "filter":     t.get("filter"),
        "stake_units": t.get("stake_units", 0.0),
        "stake_units_per_combo": t.get("stake_units_per_combo", 0.0),
        "n_combos":   t.get("n_combos", 0),
        "stake_hkd_min": t.get("stake_hkd_min", 0.0),
        "confidence": t.get("confidence", "low"),
        "stake_reasons": t.get("stake_reasons", []),
        "banker_no":  b.get("horse_no"),
        "banker_name": b.get("horse_name"),
        "banker_sp":  b.get("win_odds"),
        "banker_pmodel": b.get("p_model"),
        "banker_edge": b.get("edge"),
        "legs": [
            {"no": l["horse_no"], "name": l["horse_name"],
             "sp": l.get("win_odds"), "edge": l.get("edge")}
            for l in t.get("legs", [])
        ],
        "extras": t.get("extras", []),
        "hedge": ({
            "type": hedge.get("type"),
            "pairs": hedge.get("pairs"),
            "stake_units": hedge.get("stake_units"),
            "reason": hedge.get("reason"),
        } if hedge else None),
        "f4": ({
            "play": f4.get("play"),
            "horses": [{"no": h["horse_no"], "name": h["horse_name"]}
                         for h in f4.get("horses", [])],
            "n_combos": f4.get("n_combos"),
            "stake_units": f4.get("stake_units"),
            "reason": f4.get("reason"),
        } if f4 else None),
        "reason":     t.get("reason"),
    }


def log_meeting_picks(date_compact: str, venue: str,
                       items: list[dict]) -> int:
    """Append one JSON line per non-SKIP ticket to model_picks_log.jsonl.

    Idempotent: removes any previous rows for the same (date, race_number)
    before re-appending, so re-running analysis for a day refreshes cleanly.
    Returns number of rows written.
    """
    PICKS_LOG_PATH.parent.mkdir(exist_ok=True)
    existing: list[dict] = []
    if PICKS_LOG_PATH.exists():
        for ln in PICKS_LOG_PATH.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                existing.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    # Drop rows for the meeting we're refreshing
    existing = [r for r in existing if r.get("date") != date_compact]
    new_rows = [_ticket_log_row(date_compact, venue, it)
                 for it in items if it["ticket"]["play"] != "SKIP"]
    with open(PICKS_LOG_PATH, "w", encoding="utf-8") as f:
        for row in existing + new_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(new_rows)


def load_picks_log() -> list[dict]:
    """Read the entire picks log."""
    if not PICKS_LOG_PATH.exists():
        return []
    out: list[dict] = []
    for ln in PICKS_LOG_PATH.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _evaluate_logged_pick(row: dict, results_by_race: dict,
                             dividends_by_race: dict,
                             *, stake_min_hkd: float = 10.0) -> dict:
    """Match a logged pick against real results + dividends.

    Stake is denominated in **HKD** (real money). Falls back to
    ``stake_units * 10`` when ``stake_hkd_min`` is missing on legacy rows;
    enforces a per-bet floor of ``stake_min_hkd`` (HKJC minimum is $10).

    Returns {"stake": X, "return": Y, "hit": bool, "banker_finish": N}.
    Unresolved races (no results yet) yield stake=0.
    """
    rn = row["race_number"]
    res_race = results_by_race.get(rn)
    if not res_race:
        return {"stake": 0.0, "return": 0.0, "hit": None,
                "banker_finish": None, "status": "pending"}

    rb = _results_race_by_no(res_race)
    # top1 / top2 / top3
    fin = sorted(
        [(no, r["place"]) for no, r in rb.items() if r["place"] is not None],
        key=lambda kv: kv[1],
    )
    fin1 = {fin[0][0]} if fin else set()
    fin2 = {no for no, p in fin[:2]}
    fin3 = {no for no, p in fin[:3]}

    banker_no = row["banker_no"]
    legs_no = [l["no"] for l in row.get("legs", [])]

    # Stake: prefer explicit HKD; legacy rows use stake_units (1u == $10).
    stake_hkd = row.get("stake_hkd_min")
    if stake_hkd is None or stake_hkd <= 0:
        stake_hkd = float(row.get("stake_units", 1.0) or 1.0) * 10.0
    stake_hkd = max(float(stake_hkd), stake_min_hkd)
    # Dividends are quoted *per $10 stake*, so convert to that basis.
    stake_units_for_div = stake_hkd / 10.0

    play = row["play"]
    divs = dividends_by_race.get(rn, {})

    def _div(pool: str, combo) -> float:
        from analyze_betting_edge import div_pay
        return div_pay(divs, pool, combo)

    ret = 0.0
    hit = False
    if play == "WIN":
        if banker_no in fin1:
            ret = stake_units_for_div * _div("WIN", [banker_no])
            hit = ret > 0
    elif play == "PLACE":
        if banker_no in fin3:
            ret = stake_units_for_div * _div("PLACE", [banker_no])
            hit = ret > 0
    elif play == "QIN_BANKER":
        if banker_no in fin2 and legs_no:
            per = stake_units_for_div / len(legs_no)
            for l in legs_no:
                if l in fin2 and l != banker_no:
                    ret += per * _div("QIN", [banker_no, l])
                    hit = True
                    break
    elif play == "QPL_BANKER":
        if banker_no in fin3 and legs_no:
            per = stake_units_for_div / len(legs_no)
            for l in legs_no:
                if l in fin3 and l != banker_no:
                    ret += per * _div("QPL", [banker_no, l])
                    hit = True
    banker_finish = rb.get(banker_no, {}).get("place")
    return {"stake": stake_hkd, "return": round(ret, 2), "hit": hit,
            "banker_finish": banker_finish, "status": "settled"}


def settle_picks_log(*, stake_min_hkd: float = 10.0,
                       auto_refresh: bool = True) -> dict:
    """Walk the picks log, attach results+dividends where available.

    When ``auto_refresh=True`` (default), re-builds picks for every meeting
    that has a ``race_day_report_*.json`` on disk so newly-analysed
    meetings show up automatically. Stakes are reported in **HKD** with a
    minimum floor of ``stake_min_hkd`` (HKJC minimum is $10).

    Returns {"rows": [...augmented rows...], "summary": {...}}.
    """
    if auto_refresh:
        try:
            _auto_refresh_picks_log()
        except Exception as exc:
            # Never let refresh failures block settlement.
            print(f"[settle_picks_log] auto-refresh skipped: {exc}")

    rows = load_picks_log()
    by_date: dict = defaultdict_local(lambda: {"results": {}, "dividends": {}})
    settled_rows: list[dict] = []

    cache_meta: dict = {}
    for row in rows:
        d = row["date"]
        if d not in cache_meta:
            meet = load_meeting(d)
            res_races = {r["race_number"]: r
                            for r in (meet["results"]["races"]
                                        if meet["results"] else [])}
            div_path = REPORTS / f"dividends_{d}.json"
            div_races: dict = {}
            if div_path.exists():
                try:
                    raw = json.loads(div_path.read_text(encoding="utf-8"))
                    for r in raw.get("races", []):
                        by_pool: dict = {}
                        for drow in r.get("dividends", []):
                            pool = drow["pool"]
                            combo = tuple(sorted(
                                int(x) for x in str(drow["combination"]).split(",")
                                if x.strip().isdigit()
                            ))
                            if not combo:
                                continue
                            by_pool.setdefault(pool, {})[frozenset(combo)] \
                                = drow["dividend_per_10"]
                        div_races[r["race_number"]] = by_pool
                except Exception:
                    div_races = {}
            cache_meta[d] = {"results": res_races, "dividends": div_races}

        ev = _evaluate_logged_pick(row, cache_meta[d]["results"],
                                     cache_meta[d]["dividends"],
                                     stake_min_hkd=stake_min_hkd)
        merged = dict(row)
        merged.update(ev)
        settled_rows.append(merged)

    settled = [r for r in settled_rows if r.get("status") == "settled"]
    tot_stake = sum(r["stake"] for r in settled)
    tot_ret   = sum(r["return"] for r in settled)
    hits      = sum(1 for r in settled if r.get("hit"))

    by_filter: dict = {}
    for r in settled:
        key = r.get("filter") or "unknown"
        d = by_filter.setdefault(key, {"bets": 0, "hits": 0,
                                         "stake": 0.0, "ret": 0.0})
        d["bets"] += 1
        d["stake"] += r["stake"]
        d["ret"] += r["return"]
        if r.get("hit"):
            d["hits"] += 1

    return {
        "rows": settled_rows,
        "summary": {
            "bets": len(settled),
            "pending": sum(1 for r in settled_rows
                             if r.get("status") == "pending"),
            "hits": hits,
            "hit_rate": hits / len(settled) if settled else 0.0,
            "stake": tot_stake,
            "return": tot_ret,
            "roi": (tot_ret - tot_stake) / tot_stake if tot_stake else 0.0,
            "by_filter": by_filter,
        },
    }



    ap.add_argument("--backtest", action="store_true",
                    help="Run on all April 2026 meetings with results.")
    ap.add_argument("--picks", help="Date YYYY-MM-DD to generate picks for.")
    ap.add_argument("--csv", help="Optional CSV dump path.")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    bb = load_blackbook()
    factor_tbls = load_factor_tables()
    print(f"[ready] blackbook: {len(bb)} active  |  factor window: "
          f"{CFG['factor_window']}  ({len(factor_tbls)} tables)")
    print()

    meetings = []
    if args.backtest:
        dates = APRIL_DATES
    elif args.picks:
        dates = [args.picks.replace("-", "")]
    else:
        ap.print_help()
        return

    totals = {"stake": 0.0, "ret": 0.0, "bets": 0, "skip": 0,
              "win": 0, "qin": 0, "qpl": 0}
    for d in dates:
        m = run_meeting(d, bb, factor_tbls, verbose=not args.quiet)
        meetings.append(m)
        s = m.get("summary") or {}
        if s:
            totals["stake"] += s["total_stake"]
            totals["ret"]   += s["total_return"]
            totals["bets"]  += s["n_bets"]
            totals["skip"]  += s["n_skip"]
            totals["win"]   += int(s["win_strike"] * s["n_bets"])
            totals["qin"]   += int(s["qin_strike"] * s["n_bets"])
            totals["qpl"]   += int(s["qpl_strike"] * s["n_bets"])
        print()

    if args.backtest and totals["bets"] > 0:
        roi = (totals["ret"] - totals["stake"]) / totals["stake"] \
            if totals["stake"] > 0 else 0.0
        print("=" * 64)
        print(f"APRIL BACKTEST — {totals['bets']} bets, {totals['skip']} skipped")
        print(f"   QPL strike: {totals['qpl']/totals['bets']:.1%}   "
              f"(hits {totals['qpl']}/{totals['bets']})")
        print(f"   QIN strike: {totals['qin']/totals['bets']:.1%}   "
              f"(hits {totals['qin']}/{totals['bets']})")
        print(f"   WIN strike: {totals['win']/totals['bets']:.1%}   "
              f"(hits {totals['win']}/{totals['bets']})")
        print(f"   Stake: {totals['stake']:.1f}   Return: {totals['ret']:.1f}")
        print(f"   ROI_est (QPL-biased, SP-dividend proxy): {roi:+.1%}")
        print("=" * 64)
        print("NOTE: ROI uses SP-based dividend proxy (QPL_div ≈ o_A*o_B*0.825/3).")
        print("Actual HKJC QPL dividends can vary ±30% from this proxy — treat")
        print("ROI as a rank-order signal, not a bankroll guarantee.")

    if args.csv:
        _csv_dump(args.csv, meetings)
        print(f"\nCSV written → {args.csv}")


if __name__ == "__main__":
    main()
