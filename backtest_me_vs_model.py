"""
Counter-factual portfolio simulator: ME vs MODEL.

Simulates several flat-stake strategies using the persisted ET (v4.4) and SARR
model reports, settles them against official results + dividends, and compares
total PnL against the user's actual bets in `reports/user_bets_log.jsonl`.

Strategies simulated (flat $100 per race unless stated):
  1. ET_WIN_TOP1          — bet WIN on ET top-1
  2. SARR_WIN_TOP1        — bet WIN on SARR top-1
  3. ET_PLACE_TOP1        — bet PLACE on ET top-1
  4. SARR_PLACE_TOP1      — bet PLACE on SARR top-1
  5. MUTUAL_WIN_TOP1      — bet WIN on top-1 only when ET and SARR agree (else skip)
  6. MUTUAL_QIN_BOX_TOP2  — QIN box of top-2 mutual horses (needs 2 overlaps)
  7. ME_ACTUAL            — user's real bets from user_bets_log.jsonl

Dividends are quoted per HK$10. We scale proportionally for $100 stakes.

Run:
    python backtest_me_vs_model.py
    python backtest_me_vs_model.py --stake 200 --out cache/me_vs_model_backtest.json
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"

STAKE_DEFAULT = 100.0  # HK$ per race per strategy


# ---------- loaders ----------

def _read_json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def _et_path(date_compact: str) -> Path | None:
    for variant in ("v4.4", "v3.4.8"):
        p = REPORTS / f"race_day_report_{date_compact}_{variant}.json"
        if p.exists():
            return p
    return None


def _sarr_path(date_compact: str) -> Path | None:
    p = REPORTS / f"race_day_report_{date_compact}_SARR.json"
    return p if p.exists() else None


def _results_path(date_compact: str) -> Path | None:
    p = REPORTS / f"results_{date_compact}.json"
    return p if p.exists() else None


def _dividends_path(date_compact: str) -> Path | None:
    p = REPORTS / f"dividends_{date_compact}.json"
    return p if p.exists() else None


def _load_user_bets() -> list[dict]:
    p = REPORTS / "user_bets_log.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _gather_dates() -> list[str]:
    dates: set[str] = set()
    for p in REPORTS.glob("results_*.json"):
        stem = p.stem  # results_20260422
        dc = stem.split("_", 1)[1]
        if len(dc) == 8 and dc.isdigit():
            dates.add(dc)
    # must also have BOTH ET and SARR
    ok = [d for d in dates if _et_path(d) and _sarr_path(d) and _dividends_path(d)]
    return sorted(ok)


# ---------- index helpers ----------

def _race_index_by_number(report: dict) -> dict[int, dict]:
    return {int(r.get("race_number")): r for r in report.get("races", [])}


def _top_n_horse_nos(race_report: dict, n: int) -> list[int]:
    picks = race_report.get("picks") or []
    out: list[int] = []
    for p in picks:
        hn = p.get("horse_no")
        if hn is None:
            continue
        try:
            out.append(int(hn))
        except (TypeError, ValueError):
            continue
        if len(out) >= n:
            break
    return out


def _winner_and_placings(result_race: dict) -> tuple[int | None, set[int]]:
    winner: int | None = None
    placings: set[int] = set()
    for runner in result_race.get("runners", []):
        place = str(runner.get("place") or "").strip()
        try:
            hn = int(runner.get("horse_no"))
        except (TypeError, ValueError):
            continue
        if place == "1":
            winner = hn
            placings.add(hn)
        elif place in ("2", "3"):
            placings.add(hn)
    return winner, placings


def _dividends_lookup(div_race: dict) -> dict[tuple[str, tuple[int, ...]], float]:
    """Return {(pool, sorted_combo_ints): dividend_per_10}."""
    out: dict[tuple[str, tuple[int, ...]], float] = {}
    for d in div_race.get("dividends", []) or []:
        pool = str(d.get("pool") or "").upper().strip()
        combo_raw = str(d.get("combination") or "").strip()
        try:
            div = float(d.get("dividend_per_10"))
        except (TypeError, ValueError):
            continue
        parts: list[int] = []
        for token in combo_raw.replace("-", ",").replace("/", ",").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                parts.append(int(token))
            except ValueError:
                parts = []
                break
        if not parts:
            continue
        key = (pool, tuple(sorted(parts)))
        out[key] = div
    return out


# ---------- settlement ----------

def _settle_win(horse_no: int, stake: float, winner: int | None,
                divs: dict[tuple[str, tuple[int, ...]], float]) -> float:
    if winner is None or horse_no != winner:
        return -stake
    div = divs.get(("WIN", (horse_no,)))
    if div is None:
        return -stake  # paid to scratched? treat as loss
    return stake * (div / 10.0) - stake


def _settle_place(horse_no: int, stake: float, placings: set[int],
                  divs: dict[tuple[str, tuple[int, ...]], float]) -> float:
    if horse_no not in placings:
        return -stake
    div = divs.get(("PLACE", (horse_no,)))
    if div is None:
        return -stake
    return stake * (div / 10.0) - stake


def _settle_qin_box(selections: list[int], stake_per_combo: float,
                    divs: dict[tuple[str, tuple[int, ...]], float]) -> float:
    """QIN box: all pairs among selections. Stake is per combo."""
    combos = list(combinations(sorted(set(selections)), 2))
    if not combos:
        return 0.0
    total_cost = stake_per_combo * len(combos)
    payout = 0.0
    for c in combos:
        div = divs.get(("QIN", tuple(sorted(c))))
        if div is not None:
            payout += stake_per_combo * (div / 10.0)
    return payout - total_cost


def _settle_qpl_box(selections: list[int], stake_per_combo: float,
                    divs: dict[tuple[str, tuple[int, ...]], float]) -> float:
    combos = list(combinations(sorted(set(selections)), 2))
    if not combos:
        return 0.0
    total_cost = stake_per_combo * len(combos)
    payout = 0.0
    for c in combos:
        div = divs.get(("QPL", tuple(sorted(c))))
        if div is not None:
            payout += stake_per_combo * (div / 10.0)
    return payout - total_cost


def _settle_user_bet(bet: dict, winner: int | None, placings: set[int],
                     divs: dict[tuple[str, tuple[int, ...]], float]) -> float:
    bt = str(bet.get("bet_type") or "").upper().strip()
    sels = bet.get("selections") or []
    try:
        sels = [int(x) for x in sels]
    except (TypeError, ValueError):
        return 0.0
    stake = float(bet.get("stake_hkd") or 0.0)
    if stake <= 0:
        return 0.0

    if bt == "WIN":
        if not sels:
            return 0.0
        per = stake / len(sels)
        pnl = 0.0
        for hn in sels:
            pnl += _settle_win(hn, per, winner, divs)
        return pnl
    if bt == "PLACE":
        if not sels:
            return 0.0
        per = stake / len(sels)
        pnl = 0.0
        for hn in sels:
            pnl += _settle_place(hn, per, placings, divs)
        return pnl
    if bt in ("QIN", "QINELLA"):
        combos = list(combinations(sorted(set(sels)), 2))
        if not combos:
            return -stake
        per = stake / len(combos)
        return _settle_qin_box(sels, per, divs)
    if bt in ("QPL", "QPLACE", "QUINELLAPLACE"):
        combos = list(combinations(sorted(set(sels)), 2))
        if not combos:
            return -stake
        per = stake / len(combos)
        return _settle_qpl_box(sels, per, divs)
    # Exotic bets unsupported — count stake as sunk
    return -stake


# ---------- simulator ----------

def simulate(dates: list[str], stake: float = STAKE_DEFAULT) -> dict:
    strategy_totals: dict[str, float] = defaultdict(float)
    strategy_races: dict[str, int] = defaultdict(int)
    strategy_wins: dict[str, int] = defaultdict(int)   # hits
    per_meeting: list[dict] = []
    user_bets = _load_user_bets()
    user_by_date: dict[str, list[dict]] = defaultdict(list)
    for b in user_bets:
        d = str(b.get("meeting_date") or "")
        if d:
            user_by_date[d].append(b)

    for dc in dates:
        et = _read_json(_et_path(dc))
        sarr = _read_json(_sarr_path(dc))
        res = _read_json(_results_path(dc))
        div = _read_json(_dividends_path(dc))
        et_by_r = _race_index_by_number(et)
        sarr_by_r = _race_index_by_number(sarr)
        res_by_r = _race_index_by_number(res)
        div_by_r = {int(r.get("race_number")): r for r in div.get("races", [])}

        meeting = {"date": dc, "venue": res.get("venue"), "strategies": defaultdict(float),
                   "hits": defaultdict(int), "races": defaultdict(int)}

        for rn, res_race in res_by_r.items():
            winner, placings = _winner_and_placings(res_race)
            div_race = div_by_r.get(rn)
            if not div_race:
                continue
            divs = _dividends_lookup(div_race)
            if winner is None:
                continue

            et_race = et_by_r.get(rn, {})
            sarr_race = sarr_by_r.get(rn, {})
            et_top = _top_n_horse_nos(et_race, 3)
            sarr_top = _top_n_horse_nos(sarr_race, 3)
            mutual = [h for h in et_top if h in sarr_top]

            def _record(name: str, pnl: float, hit: bool):
                strategy_totals[name] += pnl
                strategy_races[name] += 1
                if hit:
                    strategy_wins[name] += 1
                meeting["strategies"][name] += pnl
                meeting["races"][name] += 1
                if hit:
                    meeting["hits"][name] += 1

            if et_top:
                p = _settle_win(et_top[0], stake, winner, divs)
                _record("ET_WIN_TOP1", p, et_top[0] == winner)
                p2 = _settle_place(et_top[0], stake, placings, divs)
                _record("ET_PLACE_TOP1", p2, et_top[0] in placings)
            if sarr_top:
                p = _settle_win(sarr_top[0], stake, winner, divs)
                _record("SARR_WIN_TOP1", p, sarr_top[0] == winner)
                p2 = _settle_place(sarr_top[0], stake, placings, divs)
                _record("SARR_PLACE_TOP1", p2, sarr_top[0] in placings)

            # Mutual top-1 = both models' top-1 agrees
            if et_top and sarr_top and et_top[0] == sarr_top[0]:
                p = _settle_win(et_top[0], stake, winner, divs)
                _record("MUTUAL_WIN_TOP1", p, et_top[0] == winner)

            # QIN box of top-2 mutuals (if there are at least 2 overlapping)
            if len(mutual) >= 2:
                sel = mutual[:2]
                combos = list(combinations(sorted(sel), 2))
                per = stake / len(combos) if combos else 0.0
                p = _settle_qin_box(sel, per, divs)
                hit = any(divs.get(("QIN", tuple(sorted(c)))) for c in combos)
                _record("MUTUAL_QIN_BOX_TOP2", p, hit)

        # User actual bets on this date
        for b in user_by_date.get(dc, []):
            try:
                rn = int(b.get("race_number"))
            except (TypeError, ValueError):
                continue
            res_race = res_by_r.get(rn)
            div_race = div_by_r.get(rn)
            if not res_race or not div_race:
                continue
            winner, placings = _winner_and_placings(res_race)
            if winner is None:
                continue
            divs = _dividends_lookup(div_race)
            pnl = _settle_user_bet(b, winner, placings, divs)
            stake_amt = float(b.get("stake_hkd") or 0.0)
            strategy_totals["ME_ACTUAL"] += pnl
            strategy_races["ME_ACTUAL"] += 1
            if pnl > 0:
                strategy_wins["ME_ACTUAL"] += 1
            meeting["strategies"]["ME_ACTUAL"] += pnl
            meeting["races"]["ME_ACTUAL"] += 1
            if pnl > 0:
                meeting["hits"]["ME_ACTUAL"] += 1
            # also track total stake risked for ROI
            strategy_totals.setdefault("_ME_STAKE_TOTAL", 0.0)
            strategy_totals["_ME_STAKE_TOTAL"] += stake_amt

        # Finalise meeting row
        meeting["strategies"] = dict(meeting["strategies"])
        meeting["hits"] = dict(meeting["hits"])
        meeting["races"] = dict(meeting["races"])
        per_meeting.append(meeting)

    # ROI for model strategies = pnl / (races * stake)
    summary = {}
    for name, pnl in strategy_totals.items():
        if name.startswith("_"):
            continue
        n = strategy_races[name]
        hits = strategy_wins[name]
        if name == "ME_ACTUAL":
            total_stake = strategy_totals.get("_ME_STAKE_TOTAL", 0.0) or 1.0
            roi = pnl / total_stake if total_stake else 0.0
        else:
            total_stake = n * stake
            roi = pnl / total_stake if total_stake else 0.0
        summary[name] = {
            "bets": n,
            "hits": hits,
            "hit_rate": round(100.0 * hits / n, 1) if n else 0.0,
            "pnl_hkd": round(pnl, 1),
            "stake_hkd": round(total_stake, 1),
            "roi_pct": round(100.0 * roi, 1),
        }

    return {
        "stake_per_race": stake,
        "dates": dates,
        "summary": summary,
        "per_meeting": per_meeting,
    }


# ---------- printers ----------

def _print_summary(result: dict) -> None:
    print(f"\n=== Portfolio backtest — {len(result['dates'])} meetings, stake HK${result['stake_per_race']}/race ===\n")
    rows = result["summary"]
    order = [
        "ET_WIN_TOP1", "SARR_WIN_TOP1",
        "ET_PLACE_TOP1", "SARR_PLACE_TOP1",
        "MUTUAL_WIN_TOP1", "MUTUAL_QIN_BOX_TOP2",
        "ME_ACTUAL",
    ]
    print(f"{'Strategy':<24} {'Bets':>5} {'Hits':>5} {'Hit%':>6} {'Stake':>10} {'PnL':>10} {'ROI%':>7}")
    print("-" * 72)
    for name in order:
        r = rows.get(name)
        if not r:
            continue
        print(f"{name:<24} {r['bets']:>5} {r['hits']:>5} {r['hit_rate']:>6.1f} "
              f"{r['stake_hkd']:>10.0f} {r['pnl_hkd']:>10.1f} {r['roi_pct']:>7.1f}")
    print()

    # Per-meeting ET vs SARR vs ME (WIN top-1 + ME actual for quick glance)
    print(f"{'Date':<10} {'Venue':<4} {'ET WIN':>10} {'SARR WIN':>10} {'Mutual WIN':>12} {'ME actual':>11}")
    print("-" * 60)
    for m in result["per_meeting"]:
        s = m["strategies"]
        print(f"{m['date']:<10} {str(m.get('venue') or ''):<4} "
              f"{s.get('ET_WIN_TOP1', 0):>10.0f} "
              f"{s.get('SARR_WIN_TOP1', 0):>10.0f} "
              f"{s.get('MUTUAL_WIN_TOP1', 0):>12.0f} "
              f"{s.get('ME_ACTUAL', 0):>11.0f}")
    print()


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dates", help="Comma-separated YYYY-MM-DD or YYYYMMDD dates (default = all available)")
    ap.add_argument("--stake", type=float, default=STAKE_DEFAULT, help="Flat stake per race per model strategy")
    ap.add_argument("--out", default="cache/me_vs_model_backtest.json", help="Output JSON path")
    args = ap.parse_args()

    if args.dates:
        dates = []
        for d in args.dates.split(","):
            d = d.strip().replace("-", "")
            if d:
                dates.append(d)
    else:
        dates = _gather_dates()

    if not dates:
        print("No settled meetings with ET + SARR + dividends found.")
        return

    result = simulate(dates, stake=args.stake)
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_summary(result)
    print(f"Saved {out_path}")


if __name__ == "__main__":
    main()
