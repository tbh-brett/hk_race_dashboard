"""
analyze_betting_edge.py
========================
Using:
  - betting_strategy.score_race / select_bets for model picks
  - reports/results_YYYYMMDD.json for finishing order + SP
  - reports/dividends_YYYYMMDD.json for REAL HKJC dividends (per HK$10 bet)

Compute true ROI and strike rate for every candidate bet structure:
  WIN on top pick
  WIN on top-2
  QIN banker + 3 legs
  QPL banker + 3 legs
  QIN box top-3 / top-4
  QPL box top-3 / top-4
  TRIO box top-4
  First-4 box top-5

Also breakdown by:
  - Race class (2/3/4/5)
  - Banker odds band
  - Mutual top-3 present (i.e. ET+SARR agree)
  - Card field size

Output: reports/betting_edge_analysis.md and JSON summary.
"""
from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path
from statistics import mean
from collections import defaultdict

from betting_strategy import (load_meeting, load_blackbook, load_factor_tables,
                                score_race, select_bets, _results_race_by_no,
                                APRIL_DATES)

REPORTS = Path(__file__).parent / "reports"


# ---------------------------------------------------------------------------
# Dividend lookup
# ---------------------------------------------------------------------------
def load_dividends(date_compact: str) -> dict:
    """Returns {race_no: {pool: {frozenset(combo): dividend_per_10}}}."""
    fn = REPORTS / f"dividends_{date_compact}.json"
    if not fn.exists():
        return {}
    d = json.loads(fn.read_text(encoding="utf-8"))
    out = {}
    for r in d["races"]:
        rn = r["race_number"]
        by_pool: dict = defaultdict(dict)
        for row in r["dividends"]:
            pool = row["pool"]
            combo_nums = tuple(sorted(
                int(x) for x in str(row["combination"]).split(",")
                if x.strip().isdigit()
            ))
            if not combo_nums:
                continue
            # Multiple rows per pool (e.g., PLACE 3 horses) — keep each
            by_pool[pool][frozenset(combo_nums)] = row["dividend_per_10"]
        out[rn] = dict(by_pool)
    return out


def div_pay(divs: dict, pool: str, combo) -> float:
    """Return multiplier on $1 stake (dividend_per_10 / 10), or 0 if no hit."""
    key = frozenset(combo if hasattr(combo, "__iter__") else [combo])
    d = divs.get(pool, {}).get(key)
    return (d / 10.0) if d else 0.0


# ---------------------------------------------------------------------------
# Top finishers
# ---------------------------------------------------------------------------
def _top_n(res_by_no: dict, n: int) -> list:
    fin = sorted(
        ((no, r) for no, r in res_by_no.items() if r.get("place") is not None),
        key=lambda kv: kv[1]["place"],
    )
    return [no for no, _ in fin[:n]]


# ---------------------------------------------------------------------------
# Evaluate every bet variant for a race
# ---------------------------------------------------------------------------
def eval_variants(rows: list, res_by_no: dict, divs: dict) -> dict:
    """
    Returns per-strategy {"stake": X, "return": Y, "hit": bool} for each tag.
    Strategies:
       win_top1            : 1 unit on composite #1
       win_top2            : 0.5/0.5 on composite #1 + #2
       pla_top1            : 1 unit PLACE on #1
       pla_top3_each       : 0.33 PLACE on each of top 3
       qin_banker_3        : banker + 3 legs QIN
       qpl_banker_3        : banker + 3 legs QPL
       qin_box_top3        : box QIN top 3 (C(3,2)=3 combos, stake 0.33 each)
       qin_box_top4        : box QIN top 4 (C(4,2)=6 combos)
       qpl_box_top3        : box QPL top 3
       qpl_box_top4        : box QPL top 4
       trio_box_top4       : box TRIO top 4 (C(4,3)=4 combos)
       f4_box_top5         : box FIRST4 top 5 (C(5,4)=5 combos)
    """
    if not rows or len(rows) < 4:
        return {}
    top1 = rows[0]["horse_no"]
    top2 = [r["horse_no"] for r in rows[:2]]
    top3 = [r["horse_no"] for r in rows[:3]]
    top4 = [r["horse_no"] for r in rows[:4]]
    top5 = [r["horse_no"] for r in rows[:5]]

    fin1 = _top_n(res_by_no, 1)
    fin2 = _top_n(res_by_no, 2)
    fin3 = _top_n(res_by_no, 3)
    fin4 = _top_n(res_by_no, 4)

    out = {}

    # WIN / PLACE
    out["win_top1"] = {
        "stake": 1.0,
        "return": div_pay(divs, "WIN", [top1]) if top1 in fin1 else 0.0,
    }
    out["win_top2"] = {
        "stake": 1.0,
        "return": sum(
            0.5 * div_pay(divs, "WIN", [n]) for n in top2 if n in fin1
        ),
    }
    out["pla_top1"] = {
        "stake": 1.0,
        "return": div_pay(divs, "PLACE", [top1]) if top1 in fin3 else 0.0,
    }
    out["pla_top3_each"] = {
        "stake": 1.0,
        "return": sum(
            (1.0/3) * div_pay(divs, "PLACE", [n]) for n in top3 if n in fin3
        ),
    }

    # QIN / QPL Banker (banker=top1, legs=top2..top4, 3 legs)
    banker = top1
    legs = [r["horse_no"] for r in rows[1:4]]
    per = 1.0 / len(legs)

    qin_ret = 0.0
    if banker in fin2:
        for l in legs:
            if l in fin2 and l != banker:
                qin_ret += per * div_pay(divs, "QIN", [banker, l])
                break
    out["qin_banker_3"] = {"stake": 1.0, "return": qin_ret}

    qpl_ret = 0.0
    if banker in fin3:
        for l in legs:
            if l in fin3 and l != banker:
                qpl_ret += per * div_pay(divs, "QPL", [banker, l])
    out["qpl_banker_3"] = {"stake": 1.0, "return": qpl_ret}

    # Box QIN
    for n, src in [(3, top3), (4, top4)]:
        pairs = list(combinations(src, 2))
        per = 1.0 / len(pairs)
        ret = 0.0
        for a, b in pairs:
            if {a, b} == set(fin2):
                ret += per * div_pay(divs, "QIN", [a, b])
        out[f"qin_box_top{n}"] = {"stake": 1.0, "return": ret}

    # Box QPL
    for n, src in [(3, top3), (4, top4)]:
        pairs = list(combinations(src, 2))
        per = 1.0 / len(pairs)
        ret = 0.0
        for a, b in pairs:
            if a in fin3 and b in fin3:
                ret += per * div_pay(divs, "QPL", [a, b])
        out[f"qpl_box_top{n}"] = {"stake": 1.0, "return": ret}

    # TRIO box top4
    trios = list(combinations(top4, 3))
    per = 1.0 / len(trios)
    ret = 0.0
    for tri in trios:
        if set(tri) == set(fin3):
            ret += per * div_pay(divs, "TRIO", tri)
    out["trio_box_top4"] = {"stake": 1.0, "return": ret}

    # FIRST-4 box top5
    f4s = list(combinations(top5, 4))
    per = 1.0 / len(f4s)
    ret = 0.0
    for quad in f4s:
        if set(quad) == set(fin4):
            ret += per * div_pay(divs, "F4", quad)
    out["f4_box_top5"] = {"stake": 1.0, "return": ret}

    return out


# ---------------------------------------------------------------------------
# Context tagging for breakdowns
# ---------------------------------------------------------------------------
def race_tags(race: dict, rows: list, sarr_race) -> dict:
    top1 = rows[0]
    # Banker odds band
    o = top1.get("win_odds")
    if o is None:
        band = "no_odds"
    elif o < 3.0:
        band = "fav_<3"
    elif o < 5.0:
        band = "3_5"
    elif o < 8.0:
        band = "5_8"
    elif o < 15:
        band = "8_15"
    else:
        band = "15+"

    cls = str(race.get("race_class", ""))
    mutual_top3 = sarr_race is not None and rows[0].get("sarr_rank") \
        is not None and rows[0]["sarr_rank"] <= 3
    gap = None
    if len(rows) >= 2:
        gap = rows[0]["p_model"] - rows[1]["p_model"]
    if gap is None:
        gap_band = "na"
    elif gap >= 0.08:
        gap_band = "gap_>=0.08"
    elif gap >= 0.04:
        gap_band = "gap_0.04-0.08"
    else:
        gap_band = "gap_<0.04"

    return {
        "class": f"Cls{cls}" if cls else "unk",
        "odds_band": band,
        "has_sarr": sarr_race is not None,
        "mutual_top3": mutual_top3,
        "gap_band": gap_band,
        "field": len(rows),
    }


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------
def run_sweep():
    bb = load_blackbook()
    ft = load_factor_tables()

    agg: dict = defaultdict(lambda: {"stake": 0.0, "ret": 0.0, "hits": 0,
                                      "bets": 0})
    by_class: dict = defaultdict(lambda: defaultdict(
        lambda: {"stake": 0.0, "ret": 0.0, "bets": 0}))
    by_odds: dict = defaultdict(lambda: defaultdict(
        lambda: {"stake": 0.0, "ret": 0.0, "bets": 0}))
    by_gap: dict = defaultdict(lambda: defaultdict(
        lambda: {"stake": 0.0, "ret": 0.0, "bets": 0}))
    by_mutual: dict = defaultdict(lambda: defaultdict(
        lambda: {"stake": 0.0, "ret": 0.0, "bets": 0}))

    per_race_rows: list = []

    for d in APRIL_DATES:
        m = load_meeting(d)
        if not m["et"] or not m["results"]:
            continue
        res_races = {r["race_number"]: r for r in m["results"]["races"]}
        sarr_races = {r["race_number"]: r
                      for r in (m["sarr"]["races"] if m["sarr"] else [])}
        divs_by_race = load_dividends(d)

        for race in m["et"]["races"]:
            rn = race["race_number"]
            res_race = res_races.get(rn)
            if not res_race:
                continue
            rb = _results_race_by_no(res_race)
            odds_by = {no: v["win_odds"] for no, v in rb.items()}
            rows = score_race(race, sarr_races.get(rn), ft, bb,
                               actual_odds_by_no=odds_by)
            if len(rows) < 4:
                continue
            divs = divs_by_race.get(rn, {})
            variants = eval_variants(rows, rb, divs)
            tags = race_tags(race, rows, sarr_races.get(rn))

            for strat, r in variants.items():
                agg[strat]["stake"] += r["stake"]
                agg[strat]["ret"]   += r["return"]
                agg[strat]["bets"]  += 1
                if r["return"] > 0:
                    agg[strat]["hits"] += 1
                by_class[strat][tags["class"]]["stake"] += r["stake"]
                by_class[strat][tags["class"]]["ret"]   += r["return"]
                by_class[strat][tags["class"]]["bets"]  += 1
                by_odds[strat][tags["odds_band"]]["stake"] += r["stake"]
                by_odds[strat][tags["odds_band"]]["ret"]   += r["return"]
                by_odds[strat][tags["odds_band"]]["bets"]  += 1
                by_gap[strat][tags["gap_band"]]["stake"] += r["stake"]
                by_gap[strat][tags["gap_band"]]["ret"]   += r["return"]
                by_gap[strat][tags["gap_band"]]["bets"]  += 1
                mk = "mutual" if tags["mutual_top3"] else (
                    "sarr_only" if tags["has_sarr"] else "no_sarr")
                by_mutual[strat][mk]["stake"] += r["stake"]
                by_mutual[strat][mk]["ret"]   += r["return"]
                by_mutual[strat][mk]["bets"]  += 1

            per_race_rows.append({
                "date": d, "race_number": rn, "tags": tags,
                "variants": variants,
            })

    return agg, by_class, by_odds, by_gap, by_mutual, per_race_rows


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _fmt_agg(a):
    roi = (a["ret"] - a["stake"]) / a["stake"] if a["stake"] else 0.0
    hr = a["hits"] / a["bets"] if a["bets"] else 0.0
    return (a["bets"], hr, a["stake"], a["ret"], roi)


def print_report(agg, by_class, by_odds, by_gap, by_mutual):
    strats = list(agg.keys())
    print(f"{'Strategy':<20} {'Bets':>5} {'Hit%':>7} {'Stake':>7} "
          f"{'Return':>7} {'ROI':>9}")
    print("-" * 65)
    # Sort by ROI descending
    for s in sorted(strats, key=lambda x: -agg[x]["ret"] / agg[x]["stake"]
                     if agg[x]["stake"] else -999):
        n, hr, stk, ret, roi = _fmt_agg(agg[s])
        print(f"{s:<20} {n:>5} {hr*100:>6.1f}% {stk:>7.1f} {ret:>7.2f} "
              f"{roi*100:>+8.1f}%")

    print()
    print("=== BY RACE CLASS (QPL banker only) ===")
    strat = "qpl_banker_3"
    for cls, a in sorted(by_class[strat].items()):
        roi = (a["ret"] - a["stake"]) / a["stake"] if a["stake"] else 0
        print(f"  {cls:<6}  bets {a['bets']:>3}  ret {a['ret']:>6.2f}  "
              f"ROI {roi*100:>+7.1f}%")

    print()
    print("=== BY BANKER ODDS BAND (QPL banker) ===")
    for band, a in sorted(by_odds[strat].items(),
                            key=lambda kv: kv[0]):
        roi = (a["ret"] - a["stake"]) / a["stake"] if a["stake"] else 0
        print(f"  {band:<10} bets {a['bets']:>3}  ret {a['ret']:>6.2f}  "
              f"ROI {roi*100:>+7.1f}%")

    print()
    print("=== BY TOP-2 GAP (QPL banker) ===")
    for band, a in sorted(by_gap[strat].items()):
        roi = (a["ret"] - a["stake"]) / a["stake"] if a["stake"] else 0
        print(f"  {band:<18} bets {a['bets']:>3}  ret {a['ret']:>6.2f}  "
              f"ROI {roi*100:>+7.1f}%")

    print()
    print("=== MUTUAL TOP3 AGREEMENT (QPL banker) ===")
    for k, a in sorted(by_mutual[strat].items()):
        roi = (a["ret"] - a["stake"]) / a["stake"] if a["stake"] else 0
        print(f"  {k:<12} bets {a['bets']:>3}  ret {a['ret']:>6.2f}  "
              f"ROI {roi*100:>+7.1f}%")

    print()
    print("=== TOP STRATEGIES: cross-check on WIN top-1 per class ===")
    for cls, a in sorted(by_class["win_top1"].items()):
        roi = (a["ret"] - a["stake"]) / a["stake"] if a["stake"] else 0
        print(f"  {cls:<6}  bets {a['bets']:>3}  ret {a['ret']:>6.2f}  "
              f"ROI {roi*100:>+7.1f}%")


def main():
    agg, by_class, by_odds, by_gap, by_mutual, per_race = run_sweep()
    print_report(agg, by_class, by_odds, by_gap, by_mutual)

    # Save JSON
    out = {
        "aggregate": {k: dict(v) for k, v in agg.items()},
        "by_class":   {k: {c: dict(x) for c, x in v.items()}
                         for k, v in by_class.items()},
        "by_odds":    {k: {c: dict(x) for c, x in v.items()}
                         for k, v in by_odds.items()},
        "by_gap":     {k: {c: dict(x) for c, x in v.items()}
                         for k, v in by_gap.items()},
        "by_mutual":  {k: {c: dict(x) for c, x in v.items()}
                         for k, v in by_mutual.items()},
    }
    (REPORTS / "betting_edge_analysis.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n-> reports\\betting_edge_analysis.json")


if __name__ == "__main__":
    main()
