"""backtest_allup.py — All-up Q+QPL ROI on April 2026.

For each meeting, build one all-up Q ticket and one all-up QPL ticket
covering N legs (default top-3 races by banker edge), with shape preset.
Tests model / market / blend pair-pickers across multiple shape presets.

Output: ROI per (pair-mode, shape) cell.
"""
from __future__ import annotations

import json
from pathlib import Path
from collections import defaultdict

from all_up import (build_all_up_ticket, settle_all_up_ticket,
                     blended_pair, SHAPE_PRESETS)

REPORTS = Path("reports")


def load_picks(date: str):
    for tag in ("v4.4", "v3.4.8"):
        p = REPORTS / f"race_day_report_{date}_{tag}.json"
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            venue = d.get("meeting", {}).get("venue_code") or "ST"
            races = {int(r.get("race_number") or 0): r.get("picks") or []
                     for r in d.get("races", []) if r.get("picks")}
            return races, venue
    return {}, "ST"


def _edge_rows(date: str, venue: str, race_no: int, picks: list[dict]):
    try:
        from market_loader import compute_edge_table
        return compute_edge_table(date, venue, race_no, picks).get("rows") or []
    except Exception:
        return []


def april():
    return sorted(p.stem.replace("results_", "")
                   for p in REPORTS.glob("results_202604*.json"))


def pick_legs(date: str, picks_by_race: dict, venue: str,
              n_legs: int, mode: str) -> list[dict]:
    """Pick n_legs (race_no, pair) tuples. Race choice = top by combined edge.
    """
    candidates = []
    for rn, picks in picks_by_race.items():
        rows = _edge_rows(date, venue, rn, picks)
        pair = blended_pair(picks, rows, mode=mode)
        if not pair:
            continue
        # Race "score" for ordering: max edge of the pair (0 if no edges)
        edges = {int(r["horse_no"]): float(r.get("edge", 0) or 0)
                 for r in rows if r.get("horse_no") is not None}
        score = max(edges.get(pair[0], 0), edges.get(pair[1], 0))
        candidates.append((score, rn, pair))
    candidates.sort(reverse=True)
    return [{"race_no": rn, "pair": pair}
             for _, rn, pair in candidates[:n_legs]]


def run(shape_label: str, mode: str, stake_per_unit: float = 1.0):
    n_legs, sizes = SHAPE_PRESETS[shape_label]
    rows_out = []
    for date in april():
        picks_by_race, venue = load_picks(date)
        if len(picks_by_race) < n_legs:
            continue
        legs = pick_legs(date, picks_by_race, venue, n_legs, mode)
        if len(legs) < n_legs:
            continue
        qin = build_all_up_ticket(legs=legs, pool="QIN", sizes=sizes,
                                    stake_per_unit=stake_per_unit)
        qpl = build_all_up_ticket(legs=legs, pool="QPL", sizes=sizes,
                                    stake_per_unit=stake_per_unit)
        comb = build_all_up_ticket(legs=legs, pool="QIN+QPL", sizes=sizes,
                                    stake_per_unit=stake_per_unit)
        sQ = settle_all_up_ticket(qin, date)
        sP = settle_all_up_ticket(qpl, date)
        sC = settle_all_up_ticket(comb, date)
        if not (sQ["settled"] and sP["settled"]):
            continue
        rows_out.append({
            "date": date, "stake": sQ["stake"] + sP["stake"],
            "return": sQ["return"] + sP["return"],
            "qin_hits": sQ["n_hits"], "qpl_hits": sP["n_hits"],
            # Combined QIN+QPL all-up (the user's "one big win" structure):
            # each leg must win the Quinella, paying QIN×QPL dividends.
            "comb_stake": sC["stake"] if sC["settled"] else 0.0,
            "comb_return": sC["return"] if sC["settled"] else 0.0,
            "comb_hits": sC["n_hits"] if sC["settled"] else 0,
        })
    return rows_out


if __name__ == "__main__":
    print(f"{'Shape':>6} {'Mode':>8} {'meets':>5} {'stake':>7} {'ret':>7} "
          f"{'pnl':>7} {'roi':>7}  per-meeting QIN_hit/QPL_hit")
    print("-" * 96)
    for shape in ["3x1", "3x4", "3x7", "4x1", "4x11", "4x15", "5x1", "5x16"]:
        for mode in ["model", "market", "blend"]:
            rows = run(shape, mode)
            if not rows:
                continue
            stake = sum(r["stake"] for r in rows)
            ret = sum(r["return"] for r in rows)
            pnl = ret - stake
            roi = pnl / stake * 100 if stake else 0
            tally = " ".join(f"{r['qin_hits']}/{r['qpl_hits']}" for r in rows)
            print(f"{shape:>6} {mode:>8} {len(rows):5d} "
                  f"{stake:7.0f} {ret:7.0f} {pnl:+7.0f} {roi:+6.1f}%  {tally}")

    # ── Combined QIN+QPL all-up ("one big win") comparison ──────────────
    print()
    print("Combined QIN+QPL all-up (each leg must win the Quinella; "
          "pays QIN×QPL dividends):")
    print(f"{'Shape':>6} {'Mode':>8} {'meets':>5} {'stake':>7} {'ret':>7} "
          f"{'pnl':>7} {'roi':>7}  per-meeting comb_hits/legs")
    print("-" * 96)
    for shape in ["3x1", "3x4", "3x7", "4x1", "4x11", "4x15", "5x1", "5x16"]:
        n_legs = SHAPE_PRESETS[shape][0]
        for mode in ["model", "market", "blend"]:
            rows = run(shape, mode)
            if not rows:
                continue
            stake = sum(r["comb_stake"] for r in rows)
            ret = sum(r["comb_return"] for r in rows)
            if stake <= 0:
                continue
            pnl = ret - stake
            roi = pnl / stake * 100 if stake else 0
            tally = " ".join(f"{r['comb_hits']}/{n_legs}" for r in rows)
            print(f"{shape:>6} {mode:>8} {len(rows):5d} "
                  f"{stake:7.0f} {ret:7.0f} {pnl:+7.0f} {roi:+6.1f}%  {tally}")
