"""backtest_qqpl_multi.py — validate the multi_builder rule on April 2026.

For each meeting in April:
  1. Load v4.4 race-day report → picks per race
  2. Run build_multi_suggestion (contrarian_banker mode, default params)
  3. Settle vs reports/dividends_YYYYMMDD.json
  4. Tally QIN hits, QPL hits, ROI

Compares:
  • contrarian_banker (default)
  • model_consensus  (banker = rank-1)
  • contrarian_banker w/ +5pp edge filter ("high conviction" subset)

Run: python backtest_qqpl_multi.py
"""
from __future__ import annotations

import json
from pathlib import Path
from collections import defaultdict

from multi_builder import build_multi_suggestion, settle_suggestion

REPORTS = Path("reports")


def load_picks_by_race(date_compact: str) -> tuple[dict, str]:
    """Return ({race_no: [picks]}, venue_code) from the v4.4 report.

    Falls back to v3.4.8 reports for early-April meetings.
    """
    for tag in ("v4.4", "v3.4.8"):
        p = REPORTS / f"race_day_report_{date_compact}_{tag}.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        picks_by_race: dict = {}
        venue = d.get("meeting", {}).get("venue_code") or "ST"
        for race in d.get("races", []):
            rn = int(race.get("race_number") or 0)
            if rn == 0:
                continue
            picks_by_race[rn] = race.get("picks") or []
        return picks_by_race, venue
    return {}, "ST"


def list_april_meetings() -> list[str]:
    out = []
    for p in REPORTS.glob("dividends_2026*.json"):
        date = p.stem.replace("dividends_", "")
        if date.startswith("202604"):
            out.append(date)
    return sorted(out)


def run(mode: str, edge_filter: float | None = None,
        label: str = "") -> dict:
    rows = []
    for date in list_april_meetings():
        picks_by_race, venue = load_picks_by_race(date)
        if not picks_by_race:
            continue
        for rn in sorted(picks_by_race):
            sug = build_multi_suggestion(
                date_compact=date, venue_code=venue, race_no=rn,
                picks=picks_by_race[rn], mode=mode,
            )
            if (edge_filter is not None and sug.get("banker") is not None
                    and (sug["evidence"].get("banker_edge_pp") or -99)
                    < edge_filter):
                sug["skip_reason"] = f"edge<{edge_filter}"
            if sug.get("skip_reason"):
                continue
            settled = settle_suggestion(sug, date)
            if not settled["settled"]:
                continue
            rows.append({
                "date": date, "race": rn, "banker": sug["banker"],
                "legs": sug["legs"],
                "shape": sug["shape"],
                "stake": settled["stake"], "return": settled["return"],
                "pnl": settled["pnl"],
                "qin_hits": settled["qin_hits"],
                "qpl_hits": settled["qpl_hits"],
                "banker_rank": sug["evidence"]["banker_rank"],
                "banker_edge": sug["evidence"]["banker_edge_pp"],
                "consensus": sug["evidence"]["consensus_count"],
            })

    n = len(rows)
    if n == 0:
        return {"label": label, "n": 0}
    stake = sum(r["stake"] for r in rows)
    ret = sum(r["return"] for r in rows)
    pnl = ret - stake
    n_qin = sum(1 for r in rows if r["qin_hits"])
    n_qpl = sum(1 for r in rows if r["qpl_hits"])
    by_day = defaultdict(lambda: {"stake": 0, "ret": 0, "n": 0})
    for r in rows:
        by_day[r["date"]]["stake"] += r["stake"]
        by_day[r["date"]]["ret"] += r["return"]
        by_day[r["date"]]["n"] += 1

    return {
        "label": label or mode,
        "n": n, "stake": stake, "return": ret, "pnl": pnl,
        "roi_pct": pnl / stake * 100 if stake else 0,
        "qin_hit_rate": n_qin / n * 100,
        "qpl_hit_rate": n_qpl / n * 100,
        "by_day": dict(by_day),
        "rows": rows,
    }


def fmt(s: dict) -> None:
    print(f"\n{'='*78}")
    print(f"  {s['label'].upper()}")
    print(f"{'='*78}")
    if s.get("n", 0) == 0:
        print("  (no rows)")
        return
    print(f"  Races covered: {s['n']:3d}   Stake: ${s['stake']:>7,.0f}   "
          f"Return: ${s['return']:>7,.0f}   PnL: ${s['pnl']:+7,.0f}   "
          f"ROI: {s['roi_pct']:+6.1f}%")
    print(f"  QIN hit-rate (any leg): {s['qin_hit_rate']:5.1f}%   "
          f"QPL hit-rate: {s['qpl_hit_rate']:5.1f}%")
    print(f"\n  Per-day:")
    print(f"  {'Date':10} {'n':>3} {'stake':>7} {'ret':>7} {'pnl':>7} {'roi':>7}")
    for d, x in sorted(s["by_day"].items()):
        pnl = x["ret"] - x["stake"]
        roi = pnl / x["stake"] * 100 if x["stake"] else 0
        print(f"  {d:10} {x['n']:3d} {x['stake']:>7,.0f} {x['ret']:>7,.0f} "
              f"{pnl:>+7,.0f} {roi:>+6.1f}%")


if __name__ == "__main__":
    print(f"April 2026 meetings: {list_april_meetings()}")

    for cfg in [
        {"mode": "contrarian_banker", "label": "contrarian_banker (default)"},
        {"mode": "model_consensus",   "label": "model_consensus (rank-1 banker)"},
        {"mode": "contrarian_banker", "edge_filter": 5.0,
         "label": "contrarian + edge≥+5pp filter"},
        {"mode": "contrarian_banker", "edge_filter": 8.0,
         "label": "contrarian + edge≥+8pp filter (high conviction)"},
    ]:
        s = run(**cfg)
        fmt(s)
