"""
backtest_combined_edge.py
=========================
Phase-2 follow-up to backtest_market.py.

Phase-1 finding: model-top-1 alone = +8.4% ROI; positive-edge alone =
-37% ROI. The intersection — top-rank AND value vs market — was never
tested. This file does that and writes:

    reports/combined_edge_backtest.json   (machine-readable)
    reports/COMBINED_EDGE_BACKTEST.md     (human-readable)

Strategies tested (flat $1 WIN unless suffix _K = Kelly-fraction)
----------------------------------------------------------------
  TOP1            rank-1 model pick (no market filter)        baseline
  TOP1_PE         rank-1 AND edge >= 0
  TOP1_PE5        rank-1 AND edge >= 0.05
  TOP2_PE         rank<=2 AND edge >= 0
  TOP3_PE         rank<=3 AND edge >= 0
  TOP1_NE         rank-1 AND edge < 0    (diagnostic — fade overbet fav)
  OVERLAY_4P      rank<=3 AND edge >= 0.05 AND odds >= 4
  KELLY_TOP3      rank<=3 AND edge >= 0, stake = f* = edge/(o-1)

Where  edge = p_model - p_market  and  p_market = implied_basic(SP).

Usage
-----
    python backtest_combined_edge.py
    python backtest_combined_edge.py --from 2026-04-01 --to 2026-04-29
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from market_belief import implied_basic
from backtest_market import (
    BASE, REPORTS, list_dates, load_results, load_model_winprob,
)

OUT_JSON = REPORTS / "combined_edge_backtest.json"
OUT_MD = REPORTS / "COMBINED_EDGE_BACKTEST.md"


# ---------------------------------------------------------------------------
# Per-runner enrichment
# ---------------------------------------------------------------------------
def build_runner_table(d_from: str | None, d_to: str | None,
                       version: str = "v4.4") -> list[dict]:
    """Return a flat list of runner rows enriched with model + market data.

    Each row:
        date, race_no, horse_no, horse_name, win_odds, place,
        n_runners, p_market, p_model, rank_model, edge.

    Only includes runners from races where BOTH model and SP are
    available (so the comparison is meaningful).
    """
    dates = list_dates(d_from, d_to)
    rows: list[dict] = []
    for d in dates:
        runners = load_results(d)
        if not runners:
            continue
        p_model = load_model_winprob(d, version=version)
        if not p_model:
            continue
        # group by race
        by_race: dict[int, list] = defaultdict(list)
        for r in runners:
            by_race[r.race_no].append(r)
        for rn, rs in by_race.items():
            odds = {r.horse_no: r.win_odds for r in rs}
            p_mkt = implied_basic(odds)
            # Build (horse_no, p_model) for ranking — only horses with
            # a model probability participate.
            scored = []
            for r in rs:
                pm = p_model.get((rn, r.horse_no))
                if pm is None:
                    continue
                scored.append((r, pm))
            if not scored:
                continue
            scored.sort(key=lambda t: -t[1])
            for rank, (r, pm) in enumerate(scored, start=1):
                ps = p_mkt.get(r.horse_no, 0.0)
                rows.append({
                    "date": d, "race_no": rn, "horse_no": r.horse_no,
                    "horse_name": r.horse_name, "win_odds": r.win_odds,
                    "place": r.place, "n_runners": r.n_runners,
                    "p_market": ps, "p_model": pm,
                    "rank_model": rank, "edge": pm - ps,
                })
    return rows


# ---------------------------------------------------------------------------
# Strategy filters
# ---------------------------------------------------------------------------
def _make_filters() -> dict[str, callable]:
    return {
        "TOP1":        lambda r: r["rank_model"] == 1,
        "TOP1_PE":     lambda r: r["rank_model"] == 1 and r["edge"] >= 0,
        "TOP1_PE5":    lambda r: r["rank_model"] == 1 and r["edge"] >= 0.05,
        "TOP2_PE":     lambda r: r["rank_model"] <= 2 and r["edge"] >= 0,
        "TOP3_PE":     lambda r: r["rank_model"] <= 3 and r["edge"] >= 0,
        "TOP1_NE":     lambda r: r["rank_model"] == 1 and r["edge"] < 0,
        "OVERLAY_4P":  lambda r: (r["rank_model"] <= 3 and
                                  r["edge"] >= 0.05 and
                                  r["win_odds"] >= 4.0),
    }


def _stake(strategy: str, row: dict) -> float:
    """Return stake size for a row that already passed the filter."""
    if strategy == "KELLY_TOP3":
        # f* = edge / (o - 1), capped at 5% bankroll-equiv per bet.
        b = row["win_odds"] - 1.0
        if b <= 0:
            return 0.0
        f = max(0.0, min(0.05, row["edge"] / b))
        return f
    return 1.0


def evaluate(rows: list[dict]) -> dict[str, dict]:
    filters = _make_filters()
    # Add KELLY_TOP3 (TOP3_PE filter, fractional stake).
    filters["KELLY_TOP3"] = lambda r: (r["rank_model"] <= 3 and
                                       r["edge"] >= 0)
    out: dict[str, dict] = {}
    for name, f in filters.items():
        bets = [r for r in rows if f(r)]
        if not bets:
            out[name] = {"n_bets": 0, "stake": 0.0, "return": 0.0,
                         "strike": 0.0, "roi": 0.0,
                         "by_meeting": {}, "n_winners": 0}
            continue
        total_stake = 0.0
        total_return = 0.0
        winners = 0
        by_meeting: dict[str, dict] = defaultdict(
            lambda: {"stake": 0.0, "return": 0.0, "bets": 0, "wins": 0})
        for r in bets:
            stake = _stake(name, r)
            if stake <= 0:
                continue
            total_stake += stake
            won = (r["place"] == 1)
            ret = stake * (r["win_odds"] - 1.0) if won else -stake
            total_return += ret
            if won:
                winners += 1
            m = by_meeting[r["date"]]
            m["stake"] += stake
            m["return"] += ret
            m["bets"] += 1
            if won:
                m["wins"] += 1
        roi = total_return / total_stake if total_stake > 0 else 0.0
        strike = winners / len(bets) if bets else 0.0
        out[name] = {
            "n_bets": len(bets),
            "n_winners": winners,
            "stake": total_stake,
            "return": total_return,
            "strike": strike,
            "roi": roi,
            "by_meeting": dict(by_meeting),
        }
    return out


def per_meeting_curve(eval_out: dict, strategies: list[str], dates: list[str]
                      ) -> dict[str, list]:
    """Cumulative P&L per strategy for charting."""
    out: dict[str, list] = {}
    for s in strategies:
        bm = eval_out[s]["by_meeting"]
        cum_stake = 0.0
        cum_return = 0.0
        rows = []
        for d in dates:
            m = bm.get(d, {"stake": 0.0, "return": 0.0,
                           "bets": 0, "wins": 0})
            cum_stake += m["stake"]
            cum_return += m["return"]
            rows.append({
                "date": d,
                "bets_today": m["bets"],
                "wins_today": m["wins"],
                "stake_today": round(m["stake"], 3),
                "return_today": round(m["return"], 3),
                "cum_stake": round(cum_stake, 3),
                "cum_return": round(cum_return, 3),
                "cum_roi": (cum_return / cum_stake) if cum_stake > 0 else 0.0,
            })
        out[s] = rows
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _fmt_pct(x):
    return f"{x*100:+6.2f}%"


def write_markdown(summary: dict, path: Path) -> None:
    L = []
    a = L.append
    dates = summary["dates"]
    a("# Combined-Edge Backtest — Phase 2")
    a("")
    a(f"Window: **{dates[0]} … {dates[-1]}** ({len(dates)} meetings,"
      f" {summary['n_rows']} model-scored runners,"
      f" {summary['n_races']} races).")
    a("")
    a("`edge = p_model − p_market`, where `p_market = implied_basic(final SP)`.")
    a("")
    a("## Strategy comparison (flat $1 unless noted)")
    a("")
    a("| Strategy | n bets | wins | strike | ROI | Total return |")
    a("|:--|---:|---:|---:|---:|---:|")
    order = ["TOP1", "TOP1_PE", "TOP1_PE5", "TOP2_PE", "TOP3_PE",
             "OVERLAY_4P", "KELLY_TOP3", "TOP1_NE"]
    for s in order:
        e = summary["strategies"].get(s)
        if not e:
            continue
        a(f"| {s} | {e['n_bets']} | {e['n_winners']} |"
          f" {e['strike']*100:5.1f}% | {_fmt_pct(e['roi'])} |"
          f" {e['return']:+.2f} |")
    a("")
    a("## Per-meeting cumulative ROI")
    a("")
    a("| Date | TOP1 | TOP1_PE | TOP1_PE5 | TOP2_PE | TOP3_PE |")
    a("|:--|---:|---:|---:|---:|---:|")
    rows_by_date: dict[str, dict] = defaultdict(dict)
    for s in ("TOP1", "TOP1_PE", "TOP1_PE5", "TOP2_PE", "TOP3_PE"):
        for row in summary["per_meeting"][s]:
            rows_by_date[row["date"]][s] = row["cum_roi"]
    for d in dates:
        cells = [rows_by_date[d].get(s) for s in
                 ("TOP1", "TOP1_PE", "TOP1_PE5", "TOP2_PE", "TOP3_PE")]
        a(f"| {d} | "
          + " | ".join(_fmt_pct(c) if c is not None else "—" for c in cells)
          + " |")
    a("")
    a("## Reading guide")
    a("")
    a("- `TOP1_PE` = rank-1 model pick **only when** market also rates "
      "it above the model's prob. If ROI > `TOP1`, the market filter "
      "removes losing favourites.")
    a("- `TOP1_NE` is the diagnostic counterpart — if it loses heavily, "
      "the rank-1 picks the market disliked were rightly disliked.")
    a("- `OVERLAY_4P` targets value at mid-market (odds ≥ 4 with "
      "edge ≥ 5pp). High variance, but the only strategy that can "
      "deliver positive ROI from longshot strike rates.")
    a("- `KELLY_TOP3` sizes by edge; ROI is per-dollar staked (so "
      "directly comparable to flat).")
    a("")
    path.write_text("\n".join(L), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(d_from: str | None, d_to: str | None, version: str) -> dict:
    rows = build_runner_table(d_from, d_to, version=version)
    if not rows:
        raise SystemExit("No runner rows produced — check date range / "
                         "model report files.")
    dates = sorted({r["date"] for r in rows})
    n_races = len({(r["date"], r["race_no"]) for r in rows})
    eval_out = evaluate(rows)
    strategies = list(eval_out.keys())
    curves = per_meeting_curve(eval_out, strategies, dates)
    return {
        "dates": dates,
        "n_rows": len(rows),
        "n_races": n_races,
        "model_version": version,
        "strategies": eval_out,
        "per_meeting": curves,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", default=None,
                    help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--to", dest="d_to", default=None,
                    help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--model", default="v4.4")
    args = ap.parse_args()

    d_from = args.d_from.replace("-", "") if args.d_from else None
    d_to = args.d_to.replace("-", "") if args.d_to else None

    summary = run(d_from, d_to, version=args.model)

    OUT_JSON.write_text(json.dumps(summary, indent=2, default=str),
                        encoding="utf-8")
    write_markdown(summary, OUT_MD)
    print(f"[OK] wrote {OUT_JSON.relative_to(BASE)}")
    print(f"[OK] wrote {OUT_MD.relative_to(BASE)}")
    print()
    print(f"Window: {summary['dates'][0]} → {summary['dates'][-1]}"
          f"  ({len(summary['dates'])} meetings, {summary['n_races']} races)")
    print()
    order = ["TOP1", "TOP1_PE", "TOP1_PE5", "TOP2_PE", "TOP3_PE",
             "OVERLAY_4P", "KELLY_TOP3", "TOP1_NE"]
    print(f"{'Strategy':<12} {'Bets':>5} {'Wins':>5} {'Strike':>7} {'ROI':>9}")
    for s in order:
        e = summary["strategies"].get(s, {})
        if not e or e.get("n_bets", 0) == 0:
            continue
        print(f"{s:<12} {e['n_bets']:>5} {e['n_winners']:>5}"
              f" {e['strike']*100:6.1f}% {e['roi']*100:+8.2f}%")


if __name__ == "__main__":
    main()
