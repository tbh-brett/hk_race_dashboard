"""
backtest_market.py
==================
Phase-2 validation for the market/behavioural model (Layer 1 only —
final-SP odds). Answers three questions on 2026 HKJC history:

    1. Is Shin-corrected P(win) well-calibrated?
    2. How bad is the favourite-longshot bias in the naive 1/o prob?
    3. Where a model report exists, does the model-vs-market edge
       translate into a flat-stake WIN P&L > break-even?

Output
------
    reports/market_calibration.json   machine-readable tables
    reports/MARKET_MODEL_PHASE1.md    human-readable summary

Usage
-----
    python backtest_market.py                      # all dates in reports/
    python backtest_market.py --from 2026-01-01 --to 2026-04-22
    python backtest_market.py --model v4.4         # only v4.4 edge backtest
"""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

from market_belief import (implied_basic, overround, shin_win, shin_z)

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
OUT_JSON = REPORTS / "market_calibration.json"
OUT_MD = REPORTS / "MARKET_MODEL_PHASE1.md"

_DATE_RE = re.compile(r"results_(\d{8})\.json$")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
@dataclass
class Runner:
    date: str            # YYYYMMDD
    race_no: int
    horse_no: int
    horse_name: str
    win_odds: float      # final SP
    place: int | None    # finish position, None = DNF / scratched
    n_runners: int       # field size (after scratchings)


def _to_int(x):
    try:
        return int(str(x).strip())
    except (TypeError, ValueError):
        return None


def _to_float(x):
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


def load_results(date_compact: str) -> list[Runner]:
    fn = REPORTS / f"results_{date_compact}.json"
    if not fn.exists():
        return []
    try:
        data = json.loads(fn.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[Runner] = []
    for race in data.get("races", []):
        rn = _to_int(race.get("race_number"))
        if rn is None:
            continue
        runners = race.get("runners", []) or []
        # Filter runners with usable SP.
        usable = []
        for r in runners:
            wo = _to_float(r.get("win_odds"))
            hn = _to_int(r.get("horse_no"))
            if wo is None or hn is None or wo <= 1.0:
                continue
            usable.append((r, hn, wo))
        n = len(usable)
        if n < 3:
            continue
        for r, hn, wo in usable:
            out.append(Runner(
                date=date_compact,
                race_no=rn,
                horse_no=hn,
                horse_name=str(r.get("horse_name", "")),
                win_odds=wo,
                place=_to_int(r.get("place")),
                n_runners=n,
            ))
    return out


def list_dates(d_from: str | None, d_to: str | None) -> list[str]:
    out = []
    for f in REPORTS.glob("results_*.json"):
        m = _DATE_RE.search(f.name)
        if not m:
            continue
        d = m.group(1)
        if d_from and d < d_from:
            continue
        if d_to and d > d_to:
            continue
        out.append(d)
    return sorted(out)


def load_model_winprob(date_compact: str, version: str = "v4.4"
                      ) -> dict[tuple[int, int], float]:
    """Return {(race_no, horse_no): p_model} from a race_day_report.

    Tries v4.4, then v3.4.8. Returns {} if neither exists. Probs are
    returned as fractions in [0, 1].
    """
    candidates = [
        REPORTS / f"race_day_report_{date_compact}_{version}.json",
    ]
    if version == "v4.4":
        candidates.append(REPORTS / f"race_day_report_{date_compact}_v3.4.8.json")
    for fn in candidates:
        if not fn.exists():
            continue
        try:
            data = json.loads(fn.read_text(encoding="utf-8"))
        except Exception:
            continue
        out: dict[tuple[int, int], float] = {}
        for race in data.get("races", []):
            rn = _to_int(race.get("race_number"))
            if rn is None:
                continue
            picks = race.get("picks") or []
            rows: list[tuple[int, float]] = []
            for pick in picks:
                hn = _to_int(pick.get("horse_no"))
                wp = _to_float(pick.get("win_prob"))
                if hn is None or wp is None:
                    continue
                rows.append((hn, wp))
            if not rows:
                continue
            # Detect scale per race: v4.4 stores percentages (sum ~= 100),
            # SARR / normalised stores fractions (sum ~= 1). Some long-
            # shots have values < 1.0 in either scale, so per-row logic
            # misclassifies. Use the race-wide sum.
            total = sum(wp for _, wp in rows)
            scale = 0.01 if total > 5.0 else 1.0
            for hn, wp in rows:
                out[(rn, hn)] = wp * scale
        if out:
            return out
    return {}


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
def decile_calibration(rows: list[tuple[float, bool]],
                       n_bins: int = 10) -> list[dict]:
    """Equal-frequency bins by predicted prob. Returns per-bin stats."""
    if not rows:
        return []
    rows = sorted(rows, key=lambda t: t[0])
    n = len(rows)
    bins: list[dict] = []
    step = max(1, n // n_bins)
    for b in range(n_bins):
        lo = b * step
        hi = n if b == n_bins - 1 else (b + 1) * step
        chunk = rows[lo:hi]
        if not chunk:
            continue
        preds = [p for p, _ in chunk]
        hits = [1 if w else 0 for _, w in chunk]
        bins.append({
            "bin": b + 1,
            "n": len(chunk),
            "p_mean": mean(preds),
            "p_min": min(preds),
            "p_max": max(preds),
            "win_rate": mean(hits),
        })
    return bins


def fav_longshot_bands(rows: list[tuple[float, bool, float]]) -> list[dict]:
    """rows = (implied_prob, won?, odds). Banded ROI of flat $1 bets."""
    bands = [(0, 2.5), (2.5, 4), (4, 7), (7, 12), (12, 20), (20, 50), (50, 999)]
    out = []
    for lo, hi in bands:
        sel = [(p, w, o) for p, w, o in rows if lo <= o < hi]
        if not sel:
            continue
        n = len(sel)
        wins = sum(1 for _, w, _ in sel if w)
        returns = sum((o - 1.0) if w else -1.0 for _, w, o in sel)
        exp_p = mean(p for p, _, _ in sel)
        out.append({
            "odds_band": f"{lo}-{hi}" if hi < 999 else f"{lo}+",
            "n": n,
            "strike": wins / n,
            "expected_strike": exp_p,
            "roi": returns / n,
        })
    return out


# ---------------------------------------------------------------------------
# Model-vs-market edge backtest
# ---------------------------------------------------------------------------
def edge_pnl(runners: list[Runner],
             p_shin_by_race: dict[tuple[str, int], dict[int, float]],
             p_model_by_meeting: dict[str, dict[tuple[int, int], float]],
             edge_thresholds: list[float]
             ) -> dict:
    """Flat $1 WIN bet on every horse with (p_model - p_shin) >= theta."""
    results: dict[float, dict] = {}
    for theta in edge_thresholds:
        results[theta] = {"bets": 0, "wins": 0, "stake": 0.0, "return": 0.0}

    # Also record naive (no threshold) model-top-1 for baseline.
    model_top_stats = {"bets": 0, "wins": 0, "return": 0.0}

    # Group by (date, race)
    by_race: dict[tuple[str, int], list[Runner]] = defaultdict(list)
    for r in runners:
        by_race[(r.date, r.race_no)].append(r)

    edge_samples: list[dict] = []  # for scatter plot

    for (date, rn), rs in by_race.items():
        p_shin = p_shin_by_race.get((date, rn), {})
        p_model = p_model_by_meeting.get(date, {})
        if not p_shin or not p_model:
            continue

        # Top model pick for baseline
        contenders = [(r, p_model.get((rn, r.horse_no))) for r in rs]
        contenders = [(r, p) for r, p in contenders if p is not None]
        if not contenders:
            continue

        # Baseline: model-top-1 flat $1
        top_r, _top_p = max(contenders, key=lambda t: t[1])
        won = (top_r.place == 1)
        model_top_stats["bets"] += 1
        if won:
            model_top_stats["wins"] += 1
            model_top_stats["return"] += (top_r.win_odds - 1.0)
        else:
            model_top_stats["return"] -= 1.0

        # Edge bets
        for r, pm in contenders:
            ps = p_shin.get(r.horse_no)
            if ps is None:
                continue
            edge = pm - ps
            edge_samples.append({
                "date": date, "race": rn, "horse_no": r.horse_no,
                "p_model": pm, "p_shin": ps, "edge": edge,
                "odds": r.win_odds, "won": bool(r.place == 1),
            })
            for theta, stats in results.items():
                if edge >= theta:
                    stats["bets"] += 1
                    stats["stake"] += 1.0
                    if r.place == 1:
                        stats["wins"] += 1
                        stats["return"] += (r.win_odds - 1.0)
                    else:
                        stats["return"] -= 1.0

    for theta, stats in results.items():
        stats["roi"] = (stats["return"] / stats["stake"]
                        if stats["stake"] > 0 else 0.0)
        stats["strike"] = (stats["wins"] / stats["bets"]
                           if stats["bets"] > 0 else 0.0)

    model_top_stats["roi"] = (model_top_stats["return"] / model_top_stats["bets"]
                              if model_top_stats["bets"] > 0 else 0.0)
    model_top_stats["strike"] = (model_top_stats["wins"] / model_top_stats["bets"]
                                 if model_top_stats["bets"] > 0 else 0.0)

    return {
        "by_threshold": {f"{t:+.2f}": s for t, s in results.items()},
        "model_top1_baseline": model_top_stats,
        "n_edge_samples": len(edge_samples),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(d_from: str | None, d_to: str | None, version: str) -> dict:
    dates = list_dates(d_from, d_to)
    if not dates:
        raise SystemExit("No results_*.json in date range.")

    all_runners: list[Runner] = []
    p_shin_by_race: dict[tuple[str, int], dict[int, float]] = {}
    p_basic_by_race: dict[tuple[str, int], dict[int, float]] = {}
    overround_samples: list[float] = []
    shin_z_samples: list[float] = []

    for d in dates:
        runners = load_results(d)
        if not runners:
            continue
        # Group within this date
        by_race: dict[int, list[Runner]] = defaultdict(list)
        for r in runners:
            by_race[r.race_no].append(r)
        for rn, rs in by_race.items():
            odds = {r.horse_no: r.win_odds for r in rs}
            p_basic = implied_basic(odds)
            p_shin = shin_win(odds)
            p_basic_by_race[(d, rn)] = p_basic
            p_shin_by_race[(d, rn)] = p_shin
            overround_samples.append(overround(odds))
            z = shin_z(odds)
            if math.isfinite(z):
                shin_z_samples.append(z)
        all_runners.extend(runners)

    # --- Calibration rows --------------------------------------------------
    cal_shin: list[tuple[float, bool]] = []
    cal_basic: list[tuple[float, bool]] = []
    fl_rows: list[tuple[float, bool, float]] = []
    for r in all_runners:
        won = (r.place == 1)
        ps = p_shin_by_race[(r.date, r.race_no)].get(r.horse_no)
        pb = p_basic_by_race[(r.date, r.race_no)].get(r.horse_no)
        if ps is not None:
            cal_shin.append((ps, won))
        if pb is not None:
            cal_basic.append((pb, won))
            fl_rows.append((pb, won, r.win_odds))

    cal_shin_bins = decile_calibration(cal_shin)
    cal_basic_bins = decile_calibration(cal_basic)
    fl_bands = fav_longshot_bands(fl_rows)

    # Brier score
    def brier(rows):
        return mean((p - (1.0 if w else 0.0)) ** 2 for p, w in rows) if rows else None
    brier_shin = brier(cal_shin)
    brier_basic = brier(cal_basic)

    # Log-loss
    def logloss(rows):
        if not rows:
            return None
        eps = 1e-12
        return -mean(math.log(max(p, eps)) if w else math.log(max(1 - p, eps))
                     for p, w in rows)
    ll_shin = logloss(cal_shin)
    ll_basic = logloss(cal_basic)

    # --- Edge P&L ----------------------------------------------------------
    p_model_by_meeting: dict[str, dict[tuple[int, int], float]] = {}
    for d in dates:
        pm = load_model_winprob(d, version=version)
        if pm:
            p_model_by_meeting[d] = pm

    edge = edge_pnl(all_runners, p_shin_by_race, p_model_by_meeting,
                    edge_thresholds=[0.00, 0.02, 0.05, 0.10, 0.15])

    summary = {
        "dates": dates,
        "n_runners": len(all_runners),
        "n_races": len({(r.date, r.race_no) for r in all_runners}),
        "meetings_with_model": sorted(p_model_by_meeting.keys()),
        "overround": {
            "mean": mean(overround_samples) if overround_samples else None,
            "min": min(overround_samples) if overround_samples else None,
            "max": max(overround_samples) if overround_samples else None,
        },
        "shin_z": {
            "mean": mean(shin_z_samples) if shin_z_samples else None,
            "n": len(shin_z_samples),
        },
        "calibration_shin": {
            "brier": brier_shin, "logloss": ll_shin, "bins": cal_shin_bins,
        },
        "calibration_basic": {
            "brier": brier_basic, "logloss": ll_basic, "bins": cal_basic_bins,
        },
        "fav_longshot_bands": fl_bands,
        "edge_backtest": edge,
        "model_version": version,
    }
    return summary


def _fmt_pct(x):
    return f"{x*100:+6.2f}%" if x is not None else "   n/a"


def _fmt_prob(x):
    return f"{x*100:5.1f}%" if x is not None else "  n/a"


def write_markdown(summary: dict, out_path: Path):
    lines: list[str] = []
    a = lines.append
    a("# Market Model — Phase 1 Validation")
    a("")
    a(f"- Dates: **{summary['dates'][0]} … {summary['dates'][-1]}**"
      f" ({len(summary['dates'])} meetings)")
    a(f"- Races: **{summary['n_races']}**,"
      f" runners with usable SP: **{summary['n_runners']}**")
    ov = summary["overround"]
    a(f"- WIN book overround (mean): **{ov['mean']:.4f}**"
      f" [range {ov['min']:.3f} – {ov['max']:.3f}]")
    a(f"- Shin insider-share z (mean): **{summary['shin_z']['mean']:.4f}**"
      f" (n={summary['shin_z']['n']} races)")
    a("")

    a("## 1. Calibration of implied P(win)")
    a("")
    a("| Bin | n | p̄ model | p range | actual win% | (shin) |")
    a("|---:|---:|---:|:--|---:|---:|")
    zipped = list(zip(summary["calibration_basic"]["bins"],
                      summary["calibration_shin"]["bins"]))
    for b_basic, b_shin in zipped:
        a(f"| {b_basic['bin']} | {b_basic['n']} |"
          f" {_fmt_prob(b_basic['p_mean'])} |"
          f" {b_basic['p_min']*100:.1f}–{b_basic['p_max']*100:.1f}% |"
          f" {_fmt_prob(b_basic['win_rate'])} |"
          f" {_fmt_prob(b_shin['win_rate'])} |")
    a("")
    a(f"- Brier(basic 1/o) = **{summary['calibration_basic']['brier']:.4f}**"
      f", log-loss = **{summary['calibration_basic']['logloss']:.4f}**")
    a(f"- Brier(Shin)      = **{summary['calibration_shin']['brier']:.4f}**"
      f", log-loss = **{summary['calibration_shin']['logloss']:.4f}**")
    delta_brier = (summary['calibration_basic']['brier']
                   - summary['calibration_shin']['brier'])
    a(f"- ΔBrier (Shin improves): **{delta_brier:+.5f}**"
      " (positive = Shin is better)")
    a("")

    a("## 2. Favourite–Longshot Bias (flat $1 WIN on every SP-odds horse)")
    a("")
    a("| Odds band | n | strike | expected | ROI |")
    a("|:--|---:|---:|---:|---:|")
    for row in summary["fav_longshot_bands"]:
        a(f"| {row['odds_band']} | {row['n']} |"
          f" {row['strike']*100:4.1f}% |"
          f" {row['expected_strike']*100:4.1f}% |"
          f" {_fmt_pct(row['roi'])} |")
    a("")

    a("## 3. Model-vs-Market Edge Backtest (WIN, flat $1)")
    a("")
    a(f"- Model: **{summary['model_version']}** — meetings with model"
      f" probs: **{len(summary['meetings_with_model'])}**")
    eb = summary["edge_backtest"]
    top = eb["model_top1_baseline"]
    a(f"- Baseline model-top-1: n={top['bets']},"
      f" strike={top['strike']*100:.1f}%, ROI={_fmt_pct(top['roi'])}")
    a("")
    a("| Edge threshold (p_model − p_shin) | bets | strike | ROI |")
    a("|:--|---:|---:|---:|")
    for theta, stats in sorted(eb["by_threshold"].items()):
        a(f"| ≥ {theta} | {stats['bets']} |"
          f" {stats['strike']*100:4.1f}% |"
          f" {_fmt_pct(stats['roi'])} |")
    a("")

    a("## Interpretation guide")
    a("")
    a("- **Calibration**: bins should have `actual win% ≈ p̄ model`. If the"
      " lowest-prob bin's actual is higher than predicted, the market"
      " under-rates longshots (reverse favourite-longshot). HKJC has"
      " historically shown the *normal* bias (favourites win more than"
      " their implied prob suggests) — look for that in the odds-band"
      " table.")
    a("- **Shin vs basic**: Brier should drop slightly under Shin; if it"
      " rises, the overround is already symmetric on this data and the"
      " z-correction is noise.")
    a("- **Edge ROI**: compare the `≥ 0.00` row to the baseline. A bet"
      " is only an *edge bet* — not just a model pick — if ROI rises"
      " with θ and stays positive at θ ≥ 0.05. If ROI is flat or falls,"
      " the model's over-picks are offsetting its under-picks.")
    a("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", default=None,
                    help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--to", dest="d_to", default=None,
                    help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--model", default="v4.4",
                    help="Model version for edge backtest (default v4.4)")
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
    print(f"Runners scored: {summary['n_runners']}"
          f" across {summary['n_races']} races"
          f" ({len(summary['dates'])} meetings)")
    print(f"Mean overround: {summary['overround']['mean']:.4f}"
          f"  |  mean Shin z: {summary['shin_z']['mean']:.4f}")
    print(f"Brier: basic={summary['calibration_basic']['brier']:.4f}"
          f"  shin={summary['calibration_shin']['brier']:.4f}"
          f"  Δ={summary['calibration_basic']['brier'] - summary['calibration_shin']['brier']:+.5f}")
    eb = summary["edge_backtest"]
    top = eb["model_top1_baseline"]
    print(f"Baseline (model-top-1): n={top['bets']}"
          f" strike={top['strike']*100:.1f}% ROI={top['roi']*100:+.2f}%")
    for theta, stats in sorted(eb["by_threshold"].items()):
        print(f"  edge>={theta}: n={stats['bets']:4d}"
              f" strike={stats['strike']*100:4.1f}%"
              f" ROI={stats['roi']*100:+6.2f}%")


if __name__ == "__main__":
    main()
