"""
calibration_harness.py
======================
Probability-calibration & value-betting backtest harness.

This is the next-generation evaluation tool for the model. Where
backtest_combined_edge.py asks "did filter X make money?", this harness
asks two more fundamental questions:

  1. Calibration:  when our model says P(win)=p, does the horse really
     win p fraction of the time? (If not, the model is mis-scaled.)
  2. Edge stratification: across edge quintiles (p_model - p_market),
     does ROI scale monotonically with edge magnitude? (If not, edge
     is noise; if yes, we have a real value-betting signal.)

Everything is computed against three probability sources so we can
compare them on the same footing:

    p_model        (race_day_report.json picks.win_prob, normalised)
    p_market_basic (1/SP, normalised by overround)
    p_market_shin  (Shin's z-corrected favourite-longshot bias)

Output
------
    reports/calibration_harness.json   machine-readable
    reports/CALIBRATION_HARNESS.md     human-readable

Usage
-----
    python calibration_harness.py
    python calibration_harness.py --from 2026-04-01 --to 2026-04-29
    python calibration_harness.py --version v4.4 --bins 8
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Iterable

from market_belief import implied_basic, shin_win
from backtest_market import (
    BASE, REPORTS, list_dates, load_results, load_model_winprob,
)

OUT_JSON = REPORTS / "calibration_harness.json"
OUT_MD = REPORTS / "CALIBRATION_HARNESS.md"

KELLY_CAP = 0.05  # fraction-of-bankroll cap for Kelly-staked ROI


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------
def build_rows(d_from: str | None, d_to: str | None,
               version: str = "v4.4") -> list[dict]:
    """Return one row per runner that has BOTH p_model and SP.

    Each row contains the three probability sources, the model rank,
    the edge, the realised win indicator, and the SP.
    """
    rows: list[dict] = []
    for d in list_dates(d_from, d_to):
        runners = load_results(d)
        if not runners:
            continue
        p_model = load_model_winprob(d, version=version)
        if not p_model:
            continue
        by_race: dict[int, list] = defaultdict(list)
        for r in runners:
            by_race[r.race_no].append(r)
        for rn, rs in by_race.items():
            odds = {r.horse_no: r.win_odds for r in rs}
            if not odds:
                continue
            p_basic = implied_basic(odds)
            p_shin = shin_win(odds)
            scored = []
            for r in rs:
                pm = p_model.get((rn, r.horse_no))
                if pm is None:
                    continue
                scored.append((r, pm))
            if not scored:
                continue
            # rank by model probability descending
            scored.sort(key=lambda t: -t[1])
            for rank, (r, pm) in enumerate(scored, start=1):
                ps_b = p_basic.get(r.horse_no, 0.0)
                ps_s = p_shin.get(r.horse_no, 0.0)
                rows.append({
                    "date": d, "race_no": rn, "horse_no": r.horse_no,
                    "horse_name": r.horse_name,
                    "win_odds": r.win_odds,
                    "place": r.place,
                    "won": (r.place == 1),
                    "n_runners": r.n_runners,
                    "p_model": pm,
                    "p_market_basic": ps_b,
                    "p_market_shin": ps_s,
                    "rank_model": rank,
                    "edge": pm - ps_s,
                    "edge_basic": pm - ps_b,
                })
    return rows


# ---------------------------------------------------------------------------
# Scoring metrics
# ---------------------------------------------------------------------------
def brier(rows: Iterable[tuple[float, bool]]) -> float:
    rows = list(rows)
    if not rows:
        return float("nan")
    return sum((p - (1.0 if w else 0.0)) ** 2 for p, w in rows) / len(rows)


def log_loss(rows: Iterable[tuple[float, bool]], eps: float = 1e-9) -> float:
    rows = list(rows)
    if not rows:
        return float("nan")
    s = 0.0
    for p, w in rows:
        p = min(max(p, eps), 1.0 - eps)
        s += -(math.log(p) if w else math.log(1.0 - p))
    return s / len(rows)


def reliability_bins(rows: list[tuple[float, bool]],
                     n_bins: int = 10) -> list[dict]:
    """Equal-frequency reliability bins. Each bin reports
    (mean predicted prob, observed win rate, n)."""
    if not rows:
        return []
    rows = sorted(rows, key=lambda t: t[0])
    n = len(rows)
    out: list[dict] = []
    step = max(1, n // n_bins)
    for b in range(n_bins):
        lo = b * step
        hi = n if b == n_bins - 1 else (b + 1) * step
        chunk = rows[lo:hi]
        if not chunk:
            continue
        preds = [p for p, _ in chunk]
        hits = [1 if w else 0 for _, w in chunk]
        out.append({
            "bin": b + 1,
            "n": len(chunk),
            "p_mean": mean(preds),
            "p_min": min(preds),
            "p_max": max(preds),
            "win_rate": mean(hits),
            "diff": mean(hits) - mean(preds),
        })
    return out


def expected_calibration_error(bins: list[dict]) -> float:
    """ECE = Σ (n_b / N) * |mean_pred_b - mean_actual_b|."""
    if not bins:
        return float("nan")
    n_total = sum(b["n"] for b in bins)
    if n_total == 0:
        return float("nan")
    return sum(b["n"] / n_total * abs(b["diff"]) for b in bins)


# ---------------------------------------------------------------------------
# Edge-quintile ROI
# ---------------------------------------------------------------------------
def quintile_roi(rows: list[dict], edge_key: str = "edge",
                 n_q: int = 5) -> list[dict]:
    """Sort rows by edge ascending, split into n_q equal-frequency
    buckets, and report flat-stake + Kelly ROI per bucket."""
    if not rows:
        return []
    rows = sorted(rows, key=lambda r: r[edge_key])
    n = len(rows)
    out: list[dict] = []
    step = max(1, n // n_q)
    for b in range(n_q):
        lo = b * step
        hi = n if b == n_q - 1 else (b + 1) * step
        chunk = rows[lo:hi]
        if not chunk:
            continue
        edges = [r[edge_key] for r in chunk]
        bets = len(chunk)
        wins = sum(1 for r in chunk if r["won"])
        # flat stake $1 WIN
        flat_stake = float(bets)
        flat_ret = sum((r["win_odds"] - 1.0) if r["won"] else -1.0
                       for r in chunk)
        # Kelly-fraction (cap KELLY_CAP)
        kelly_stake = 0.0
        kelly_ret = 0.0
        for r in chunk:
            b_decimal = r["win_odds"] - 1.0
            if b_decimal <= 0:
                continue
            f = r[edge_key] / b_decimal
            f = max(0.0, min(KELLY_CAP, f))
            if f <= 0:
                continue
            kelly_stake += f
            kelly_ret += (f * b_decimal) if r["won"] else -f
        out.append({
            "quintile": b + 1,
            "n": bets,
            "wins": wins,
            "edge_min": min(edges),
            "edge_max": max(edges),
            "edge_mean": mean(edges),
            "strike": wins / bets if bets else 0.0,
            "flat_stake": round(flat_stake, 4),
            "flat_return": round(flat_ret, 4),
            "flat_roi": (flat_ret / flat_stake) if flat_stake else 0.0,
            "kelly_stake": round(kelly_stake, 4),
            "kelly_return": round(kelly_ret, 4),
            "kelly_roi": (kelly_ret / kelly_stake) if kelly_stake else 0.0,
        })
    return out


# ---------------------------------------------------------------------------
# Rank × edge-sign grid
# ---------------------------------------------------------------------------
def rank_edge_grid(rows: list[dict]) -> dict:
    """Cell = (rank_bucket, edge_sign) → roi & strike."""
    rank_buckets = [(1, 1, "1"),
                    (2, 2, "2"),
                    (3, 3, "3"),
                    (4, 99, "4+")]
    cells: list[dict] = []
    for r_lo, r_hi, label in rank_buckets:
        for sign_label, sign_pred in (
                ("edge>=0", lambda e: e >= 0),
                ("edge<0",  lambda e: e < 0)):
            sub = [r for r in rows
                   if r_lo <= r["rank_model"] <= r_hi and sign_pred(r["edge"])]
            if not sub:
                cells.append({"rank": label, "edge": sign_label,
                              "n": 0, "strike": 0.0, "roi": 0.0})
                continue
            stake = float(len(sub))
            ret = sum((r["win_odds"] - 1.0) if r["won"] else -1.0
                      for r in sub)
            wins = sum(1 for r in sub if r["won"])
            cells.append({
                "rank": label, "edge": sign_label,
                "n": len(sub),
                "wins": wins,
                "strike": wins / len(sub),
                "stake": round(stake, 2),
                "return": round(ret, 4),
                "roi": ret / stake if stake else 0.0,
            })
    return {"cells": cells}


# ---------------------------------------------------------------------------
# Per-rank probability check
# ---------------------------------------------------------------------------
def per_rank_winrate(rows: list[dict], max_rank: int = 8) -> list[dict]:
    out = []
    for k in range(1, max_rank + 1):
        sub = [r for r in rows if r["rank_model"] == k]
        if not sub:
            continue
        wins = sum(1 for r in sub if r["won"])
        out.append({
            "rank": k,
            "n": len(sub),
            "wins": wins,
            "strike": wins / len(sub),
            "p_model_mean": mean(r["p_model"] for r in sub),
            "p_market_shin_mean": mean(r["p_market_shin"] for r in sub),
        })
    return out


# ---------------------------------------------------------------------------
# Top-line evaluation
# ---------------------------------------------------------------------------
def evaluate(rows: list[dict], n_bins: int = 10) -> dict:
    if not rows:
        return {"n_rows": 0, "n_races": 0, "n_meetings": 0}
    races = {(r["date"], r["race_no"]) for r in rows}
    meetings = {r["date"] for r in rows}

    sources = {
        "p_model":         [(r["p_model"], r["won"]) for r in rows],
        "p_market_basic":  [(r["p_market_basic"], r["won"]) for r in rows],
        "p_market_shin":   [(r["p_market_shin"], r["won"]) for r in rows],
    }
    cal: dict[str, dict] = {}
    for name, pairs in sources.items():
        bins = reliability_bins(pairs, n_bins=n_bins)
        cal[name] = {
            "brier": brier(pairs),
            "log_loss": log_loss(pairs),
            "ece": expected_calibration_error(bins),
            "bins": bins,
        }
    # Skill scores referenced to market_shin (the strongest baseline).
    ref_brier = cal["p_market_shin"]["brier"]
    ref_ll = cal["p_market_shin"]["log_loss"]
    for name, m in cal.items():
        m["brier_skill_vs_shin"] = (
            1.0 - m["brier"] / ref_brier if ref_brier else None)
        m["logloss_lift_vs_shin"] = (
            ref_ll - m["log_loss"] if ref_ll else None)

    # Edge analyses
    q_edge_shin = quintile_roi(rows, edge_key="edge", n_q=5)
    q_edge_basic = quintile_roi(rows, edge_key="edge_basic", n_q=5)
    grid = rank_edge_grid(rows)
    rank_table = per_rank_winrate(rows)

    return {
        "n_rows": len(rows),
        "n_races": len(races),
        "n_meetings": len(meetings),
        "date_min": min(meetings),
        "date_max": max(meetings),
        "calibration": cal,
        "edge_quintiles_shin": q_edge_shin,
        "edge_quintiles_basic": q_edge_basic,
        "rank_edge_grid": grid,
        "per_rank_winrate": rank_table,
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------
def render_md(summary: dict, version: str) -> str:
    lines: list[str] = []
    a = lines.append
    a(f"# Calibration & Value Backtest Harness — {version}")
    a("")
    if summary.get("n_rows", 0) == 0:
        a("_No data._")
        return "\n".join(lines)
    a(f"Range: **{summary['date_min']} → {summary['date_max']}**  ·  "
      f"meetings={summary['n_meetings']}  ·  "
      f"races={summary['n_races']}  ·  rows={summary['n_rows']}")
    a("")

    # --- Calibration ------------------------------------------------------
    a("## 1. Probability calibration")
    a("")
    a("Lower is better for Brier and log-loss. Skill scores are vs Shin "
      "market baseline (positive = beats the market).")
    a("")
    a("| Source | Brier | LogLoss | ECE | BrierSkill | LogLossLift |")
    a("|--------|------:|--------:|----:|-----------:|------------:|")
    for name in ("p_model", "p_market_basic", "p_market_shin"):
        m = summary["calibration"][name]
        a(f"| {name} | {m['brier']:.4f} | {m['log_loss']:.4f} | "
          f"{m['ece']:.4f} | "
          f"{(m['brier_skill_vs_shin'] or 0)*100:+.2f}% | "
          f"{(m['logloss_lift_vs_shin'] or 0):+.4f} |")
    a("")

    # Reliability table for model
    a("### Reliability bins — p_model")
    a("")
    a("| Bin | n | p_mean | win_rate | gap |")
    a("|----:|--:|-------:|---------:|----:|")
    for b in summary["calibration"]["p_model"]["bins"]:
        a(f"| {b['bin']} | {b['n']} | {b['p_mean']:.3f} | "
          f"{b['win_rate']:.3f} | {b['diff']:+.3f} |")
    a("")
    a("### Reliability bins — p_market_shin")
    a("")
    a("| Bin | n | p_mean | win_rate | gap |")
    a("|----:|--:|-------:|---------:|----:|")
    for b in summary["calibration"]["p_market_shin"]["bins"]:
        a(f"| {b['bin']} | {b['n']} | {b['p_mean']:.3f} | "
          f"{b['win_rate']:.3f} | {b['diff']:+.3f} |")
    a("")

    # --- Edge quintile ROI -----------------------------------------------
    a("## 2. ROI by edge quintile (edge = p_model - p_market_shin)")
    a("")
    a("If edge has signal, ROI should rise monotonically across quintiles.")
    a("")
    a("| Q | n | edge_range | strike | flat_roi | kelly_roi |")
    a("|--:|--:|------------|-------:|---------:|----------:|")
    for q in summary["edge_quintiles_shin"]:
        a(f"| {q['quintile']} | {q['n']} | "
          f"{q['edge_min']:+.3f} → {q['edge_max']:+.3f} | "
          f"{q['strike']*100:.1f}% | "
          f"{q['flat_roi']*100:+.2f}% | {q['kelly_roi']*100:+.2f}% |")
    a("")

    # --- Rank × edge grid -------------------------------------------------
    a("## 3. Rank × edge-sign ROI grid (flat $1 WIN)")
    a("")
    a("| rank | edge | n | strike | roi |")
    a("|------|------|--:|-------:|----:|")
    for c in summary["rank_edge_grid"]["cells"]:
        a(f"| {c['rank']} | {c['edge']} | {c['n']} | "
          f"{c.get('strike',0)*100:.1f}% | "
          f"{c.get('roi',0)*100:+.2f}% |")
    a("")

    # --- Per-rank win rate ------------------------------------------------
    a("## 4. Per-rank win rate (sanity check)")
    a("")
    a("If model rank is meaningful, strike rate should decline as rank "
      "rises. Compare to p_model_mean and p_market_shin_mean to see "
      "whether the model is **more** or **less** confident than the "
      "market for each rank.")
    a("")
    a("| rank | n | strike | p_model | p_market_shin |")
    a("|-----:|--:|-------:|--------:|--------------:|")
    for r in summary["per_rank_winrate"]:
        a(f"| {r['rank']} | {r['n']} | {r['strike']*100:.1f}% | "
          f"{r['p_model_mean']:.3f} | {r['p_market_shin_mean']:.3f} |")
    a("")

    # --- Interpretation ---------------------------------------------------
    a("## 5. Interpretation cheat-sheet")
    a("")
    a("- **BrierSkill > 0**: model's probabilities beat the market.")
    a("  In practice you should expect this to be NEGATIVE on a small")
    a("  sample — the HK win pool is one of the hardest markets to beat.")
    a("- **Edge-quintile monotonicity**: if Q5 ROI ≥ Q4 ≥ Q3 ≥ Q2 ≥ Q1,")
    a("  edge is a real ranking signal even if the model is mis-scaled.")
    a("- **Rank×edge grid**: cells with edge>=0 should outperform cells")
    a("  with edge<0 at every rank tier. If they don't, the edge")
    a("  signal isn't real — it's just leaking the rank ordering.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def run(d_from: str | None, d_to: str | None,
        version: str = "v4.4", n_bins: int = 10) -> dict:
    rows = build_rows(d_from, d_to, version=version)
    summary = evaluate(rows, n_bins=n_bins)
    summary["version"] = version
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(summary, indent=2, default=str),
                        encoding="utf-8")
    OUT_MD.write_text(render_md(summary, version), encoding="utf-8")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", default=None,
                    help="YYYY-MM-DD or YYYYMMDD")
    ap.add_argument("--to", dest="d_to", default=None)
    ap.add_argument("--version", default="v4.4")
    ap.add_argument("--bins", type=int, default=10)
    args = ap.parse_args()
    d_from = args.d_from.replace("-", "") if args.d_from else None
    d_to = args.d_to.replace("-", "") if args.d_to else None
    s = run(d_from, d_to, version=args.version, n_bins=args.bins)
    if s.get("n_rows", 0) == 0:
        print("No rows.")
        return
    cal = s["calibration"]
    print(f"\nCalibration & Value Harness  ({s['date_min']} → {s['date_max']})")
    print(f"  meetings={s['n_meetings']}  races={s['n_races']}  "
          f"rows={s['n_rows']}")
    print()
    print(f"  {'source':18} {'brier':>8} {'logloss':>8} {'ece':>6} "
          f"{'skill':>8}")
    for name in ("p_model", "p_market_basic", "p_market_shin"):
        m = cal[name]
        sk = (m['brier_skill_vs_shin'] or 0) * 100
        print(f"  {name:18} {m['brier']:>8.4f} {m['log_loss']:>8.4f} "
              f"{m['ece']:>6.3f} {sk:>+7.2f}%")
    print()
    print("  Edge quintile ROI (flat / kelly)")
    for q in s["edge_quintiles_shin"]:
        print(f"    Q{q['quintile']}: n={q['n']:3d}  "
              f"edge∈[{q['edge_min']:+.3f},{q['edge_max']:+.3f}]  "
              f"strike={q['strike']*100:5.1f}%  "
              f"flat={q['flat_roi']*100:+6.2f}%  "
              f"kelly={q['kelly_roi']*100:+6.2f}%")
    print()
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_MD}")


if __name__ == "__main__":
    main()
