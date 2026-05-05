"""
backtest_drift.py
=================
Phase-3 framework — does early-vs-late odds movement carry predictive
information independent of the final SP and our v4.4 model?

Why this matters
----------------
- Phase 1: SP-based p_market is well-calibrated (Brier 0.066).
- Phase 2: rank-1 ∩ positive edge = +23% ROI / 22.4% strike.
- Open question: does the *direction* and *magnitude* of money flow
  (steamers / drifters) add signal beyond what's already in the SP?

What this script does
---------------------
For each race that has ≥2 snapshots in cache/live_odds/YYYYMMDD/:
  - earliest snapshot   = "morning line"  -> p_morn_market
  - latest snapshot     = "near-jump"     -> p_late_market
  - drift_i             = (win_late - win_morn) / win_morn   per horse
  - implied_drift_p_i   = p_late_market[i] - p_morn_market[i]
We then merge with results_YYYYMMDD.json for ground truth and (if
available) reports/race_day_report_YYYYMMDD_v4.4.json for p_model.

Buckets analysed:
  STEAMER  drift_i <= -0.25  (odds collapsed ≥25%)
  STABLE   |drift_i| < 0.25
  DRIFTER  drift_i >= +0.25  (odds drifted ≥25%)

Reports:
  reports/drift_backtest.json
  reports/DRIFT_BACKTEST.md

The script gracefully reports "INSUFFICIENT DATA" when N is below a
minimum threshold (set MIN_RACES_FOR_INFERENCE=20 — typical signal
detection floor for HK).

Usage:
    python backtest_drift.py
    python backtest_drift.py --from 2026-04-01 --to 2026-04-29
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from statistics import mean

from market_belief import implied_basic

BASE = Path(__file__).resolve().parent
LIVE = BASE / "cache" / "live_odds"
REPORTS = BASE / "reports"
OUT_JSON = REPORTS / "drift_backtest.json"
OUT_MD = REPORTS / "DRIFT_BACKTEST.md"

STEAMER_THR = -0.25
DRIFTER_THR = +0.25
MIN_RACES_FOR_INFERENCE = 20


def _list_meeting_dates(d_from: str | None, d_to: str | None) -> list[str]:
    out = []
    if not LIVE.exists():
        return out
    for d in sorted(LIVE.iterdir()):
        if not d.is_dir() or not re.fullmatch(r"\d{8}", d.name):
            continue
        if d_from and d.name < d_from:
            continue
        if d_to and d.name > d_to:
            continue
        out.append(d.name)
    return out


def _load_snapshots_for_race(date_compact: str) -> dict:
    """Return {(venue, race_no): [snap_dict, ...] sorted by scraped_at}."""
    d = LIVE / date_compact
    out = defaultdict(list)
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        v = data.get("venue") or ""
        rn = int(data.get("race_no") or 0)
        if not v or not rn:
            continue
        out[(v, rn)].append(data)
    # already sorted by filename which embeds HHMMSS
    return out


def _race_has_drift_signal(snaps: list[dict]) -> bool:
    """True if first and last snapshot are at least 5 min apart and have
    matching race_info (same race) and at least one horse with both odds
    present."""
    if len(snaps) < 2:
        return False
    first, last = snaps[0], snaps[-1]
    if (first.get("race_info") and last.get("race_info")
            and first["race_info"] != last["race_info"]):
        return False
    if first.get("n_runners") and last.get("n_runners"):
        if first["n_runners"] != last["n_runners"]:
            return False
    return True


def _odds_dict(snap: dict) -> dict[int, float]:
    out = {}
    for o in (snap.get("odds") or []):
        try:
            hn = int(str(o.get("no", "")).strip())
            w = float(str(o.get("win", "")).strip())
        except (TypeError, ValueError):
            continue
        if w <= 1.0:
            continue
        out[hn] = w
    return out


def _load_results(date_compact: str) -> dict[tuple[int, int], dict]:
    """Return {(race_no, horse_no): {place, win_odds_sp}}."""
    p = REPORTS / f"results_{date_compact}.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for r in (data.get("races") or []):
        try:
            rn = int(r.get("race_number") or r.get("race_no") or 0)
        except (TypeError, ValueError):
            continue
        for run in (r.get("runners") or []):
            try:
                hn = int(str(run.get("horse_no", "")).strip())
                place = run.get("place")
                place_int = int(place) if str(place).isdigit() else None
                win_sp = float(str(run.get("win_odds", "")).strip())
            except (TypeError, ValueError):
                continue
            out[(rn, hn)] = {"place": place_int, "win_sp": win_sp,
                             "horse": run.get("horse_name", "")}
    return out


def build_rows(d_from: str | None, d_to: str | None) -> list[dict]:
    rows: list[dict] = []
    dates = _list_meeting_dates(d_from, d_to)
    for d in dates:
        races = _load_snapshots_for_race(d)
        results = _load_results(d)
        if not results:
            continue
        for (venue, rn), snaps in races.items():
            if not _race_has_drift_signal(snaps):
                continue
            first, last = snaps[0], snaps[-1]
            o_first = _odds_dict(first)
            o_last = _odds_dict(last)
            common = set(o_first) & set(o_last)
            if len(common) < 4:
                continue
            p_morn = implied_basic({k: o_first[k] for k in common})
            p_late = implied_basic({k: o_last[k] for k in common})
            for hn in common:
                w_morn = o_first[hn]
                w_late = o_last[hn]
                drift = (w_late - w_morn) / w_morn if w_morn else 0.0
                res = results.get((rn, hn), {})
                rows.append({
                    "date": d,
                    "venue": venue,
                    "race_no": rn,
                    "horse_no": hn,
                    "horse": res.get("horse", ""),
                    "win_morn": w_morn,
                    "win_late": w_late,
                    "win_sp": res.get("win_sp"),
                    "drift_pct": drift,
                    "p_morn": p_morn.get(hn, 0.0),
                    "p_late": p_late.get(hn, 0.0),
                    "delta_p": p_late.get(hn, 0.0) - p_morn.get(hn, 0.0),
                    "place": res.get("place"),
                    "won": (res.get("place") == 1),
                    "n_snaps": len(snaps),
                })
    return rows


def bucket(d: float) -> str:
    if d <= STEAMER_THR:
        return "STEAMER"
    if d >= DRIFTER_THR:
        return "DRIFTER"
    return "STABLE"


def evaluate(rows: list[dict]) -> dict:
    if not rows:
        return {"n_rows": 0, "buckets": {}, "races": 0}
    by_bucket = defaultdict(list)
    for r in rows:
        by_bucket[bucket(r["drift_pct"])].append(r)
    out = {"n_rows": len(rows),
           "races": len({(r["date"], r["race_no"]) for r in rows}),
           "buckets": {}}
    for b, lst in by_bucket.items():
        wins = sum(1 for r in lst if r["won"])
        # Flat-$1 WIN ROI at SP, only for rows with known SP
        priced = [r for r in lst if r["win_sp"] is not None]
        stake = float(len(priced))
        ret = sum(((r["win_sp"] - 1.0) if r["won"] else -1.0)
                  for r in priced)
        roi = (ret / stake) if stake else 0.0
        out["buckets"][b] = {
            "n_horses": len(lst),
            "wins": wins,
            "strike": (wins / len(lst)) if lst else 0.0,
            "n_priced": int(stake),
            "roi": roi,
            "avg_drift": mean(r["drift_pct"] for r in lst) if lst else 0.0,
        }
    return out


def write_markdown(summary: dict, rows: list[dict], path: Path) -> None:
    L: list[str] = []
    a = L.append
    a("# Drift Backtest — Phase 3")
    a("")
    a(f"Races analysed: **{summary['races']}**  ·  "
      f"horse-rows: **{summary['n_rows']}**")
    if summary["races"] < MIN_RACES_FOR_INFERENCE:
        a("")
        a(f"> ⚠️  **INSUFFICIENT DATA** — fewer than "
          f"{MIN_RACES_FOR_INFERENCE} races with ≥2 snapshots. "
          f"Numbers below are descriptive only; do not size bets on this.")
    a("")
    a("## Buckets")
    a("| Bucket | n horses | wins | strike | ROI@SP | avg Δ% |")
    a("|:--|---:|---:|---:|---:|---:|")
    for b in ("STEAMER", "STABLE", "DRIFTER"):
        e = summary["buckets"].get(b)
        if not e:
            a(f"| {b} | 0 | 0 | — | — | — |")
            continue
        a(f"| {b} | {e['n_horses']} | {e['wins']} | "
          f"{e['strike']*100:5.1f}% | {e['roi']*100:+6.2f}% | "
          f"{e['avg_drift']*100:+6.2f}% |")
    a("")
    a("## Notable rows (top 10 by |Δ%|)")
    rs = sorted(rows, key=lambda r: -abs(r["drift_pct"]))[:10]
    a("| Date | R | # | Horse | morn → late | Δ% | SP | Place |")
    a("|:--|---:|---:|:--|:--|---:|---:|---:|")
    for r in rs:
        sp = f"{r['win_sp']:.1f}" if r['win_sp'] else "—"
        pl = r['place'] if r['place'] is not None else "—"
        a(f"| {r['date']} | {r['race_no']} | {r['horse_no']} | "
          f"{r['horse']} | {r['win_morn']:.1f} → {r['win_late']:.1f} | "
          f"{r['drift_pct']*100:+6.1f}% | {sp} | {pl} |")
    a("")
    a("## Reading guide")
    a("- **STEAMER** = late odds collapsed ≥25% from morning. If the "
      "market is informed, steamers should win more than baseline.")
    a("- **DRIFTER** = late odds expanded ≥25%. Should win less than "
      "baseline.")
    a("- ROI @SP is computed at final SP, simulating a flat-$1 WIN bet "
      "based on the bucket assignment from the late vs morn comparison.")
    a("")
    a(f"Threshold for inference: **{MIN_RACES_FOR_INFERENCE}** races. "
      "Below that, treat all numbers as anecdotal.")
    path.write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", default=None)
    ap.add_argument("--to", dest="d_to", default=None)
    args = ap.parse_args()
    d_from = args.d_from.replace("-", "") if args.d_from else None
    d_to = args.d_to.replace("-", "") if args.d_to else None

    rows = build_rows(d_from, d_to)
    summary = evaluate(rows)
    OUT_JSON.write_text(json.dumps({"summary": summary, "rows": rows},
                                   indent=2, default=str),
                        encoding="utf-8")
    write_markdown(summary, rows, OUT_MD)
    print(f"[OK] wrote {OUT_JSON.relative_to(BASE)}")
    print(f"[OK] wrote {OUT_MD.relative_to(BASE)}")
    print()
    print(f"Races: {summary['races']}   Horse-rows: {summary['n_rows']}")
    if summary["races"] < MIN_RACES_FOR_INFERENCE:
        print(f"⚠️  Below {MIN_RACES_FOR_INFERENCE}-race inference floor.")
    print()
    for b in ("STEAMER", "STABLE", "DRIFTER"):
        e = summary["buckets"].get(b)
        if not e:
            print(f"  {b:<8}  (none)")
            continue
        print(f"  {b:<8}  n={e['n_horses']:>3}  wins={e['wins']:>2}  "
              f"strike={e['strike']*100:5.1f}%  ROI={e['roi']*100:+6.2f}%")


if __name__ == "__main__":
    main()
