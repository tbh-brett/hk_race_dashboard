"""
compare_et_vs_sarr.py — Compare ET vs SARR model performance across meetings.

Loads, per date:
  - reports/race_day_report_{YYYYMMDD}_v4.4.json  (ET picks)
  - reports/race_day_report_{YYYYMMDD}_SARR.json  (SARR picks)
  - reports/results_{YYYYMMDD}.json               (actuals)

Outputs:
  - cache/et_vs_sarr.json   — per-race + aggregate metrics
  - Console summary

Metrics per model per race:
  - top_pick_win:   1 if model's rank-1 horse finished 1st
  - top_pick_place: 1 if model's rank-1 horse finished 1st-3rd
  - top3_any_1st:   1 if any of model's top-3 finished 1st
  - top3_trifecta:  1 if model's top-3 are the trifecta (any order)
  - top3_in_top4:   avg # of model's top-3 inside actual top-4

Aggregated by: venue, distance_bucket, pace_label.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from datetime import datetime

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
OUT = BASE / "cache" / "et_vs_sarr.json"


def _load(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _finish_pos(runner) -> int | None:
    v = runner.get("place")
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _dist_bucket(d) -> str:
    try:
        d = int(d)
    except (TypeError, ValueError):
        return "?"
    if d <= 1200:
        return "sprint"
    if d <= 1650:
        return "mile"
    return "route"


def _et_path_for(dc: str) -> Path | None:
    """Try v4.4 first, then v3.4.8 (earlier April meetings)."""
    for tag in ("v4.4", "v3.4.8"):
        p = REPORTS / f"race_day_report_{dc}_{tag}.json"
        if p.exists():
            return p
    return None


def _gather_dates() -> list[str]:
    et = set()
    for tag in ("v4.4", "v3.4.8"):
        for p in REPORTS.glob(f"race_day_report_????????_{tag}.json"):
            et.add(p.name[len("race_day_report_"):][:8])
    sarr = {p.name[len("race_day_report_"):][:8]
            for p in REPORTS.glob("race_day_report_????????_SARR.json")}
    res = {p.name[len("results_"):][:8]
           for p in REPORTS.glob("results_????????.json")}
    return sorted(et & sarr & res)


def _picks_top_n(race: dict, n: int) -> list[int]:
    """Return model's top-N horse numbers (in rank order, int)."""
    out = []
    for p in race.get("picks", []):
        try:
            out.append(int(p.get("horse_no")))
        except (TypeError, ValueError):
            continue
        if len(out) == n:
            break
    return out


def _race_metrics(picks: list[int], finishers: dict) -> dict:
    """finishers: {horse_no: finish_pos int}."""
    if not picks:
        return {}
    top1 = picks[0]
    top3 = picks[:3]
    top1_pos = finishers.get(top1)
    top3_positions = [finishers.get(h) for h in top3]
    valid = [p for p in top3_positions if p is not None]
    return {
        "top_pick_win":   1 if top1_pos == 1 else 0,
        "top_pick_place": 1 if (top1_pos and top1_pos <= 3) else 0,
        "top3_any_1st":   1 if any(p == 1 for p in valid) else 0,
        "top3_trifecta":  1 if sorted(valid) == [1, 2, 3] else 0,
        "top3_in_top4":   sum(1 for p in valid if p and p <= 4),
    }


def _mutual_metrics(et_picks: list[int], sarr_picks: list[int],
                    finishers: dict) -> dict:
    """Metrics for the intersection of ET top-3 and SARR top-3.

    Returns keys:
      mutual_size           -- |ET_top3 ∩ SARR_top3|, 0..3
      mutual_horses         -- list[int] (the mutual set)
      mutual_any_in_top3    -- 1 if any mutual horse finished 1st-3rd
      mutual_count_in_top3  -- # of mutual horses inside actual top-3
      mutual_2plus_in_top3  -- 1 if mutual_count_in_top3 >= 2
      mutual_3_in_top3      -- 1 if mutual_count_in_top3 == 3 (clean sweep)
      mutual_winner_hit     -- 1 if any mutual horse finished 1st
    """
    et3 = set(et_picks[:3])
    sa3 = set(sarr_picks[:3])
    mutual = et3 & sa3
    in_top3 = sum(1 for h in mutual if finishers.get(h) in (1, 2, 3))
    has_winner = any(finishers.get(h) == 1 for h in mutual)
    return {
        "mutual_size":          len(mutual),
        "mutual_horses":        sorted(mutual),
        "mutual_any_in_top3":   1 if in_top3 >= 1 else 0,
        "mutual_count_in_top3": in_top3,
        "mutual_2plus_in_top3": 1 if in_top3 >= 2 else 0,
        "mutual_3_in_top3":     1 if in_top3 == 3 else 0,
        "mutual_winner_hit":    1 if has_winner else 0,
    }


def _actual_top3(res_race: dict) -> list[int]:
    finishers = []
    for r in res_race.get("runners", []):
        pos = _finish_pos(r)
        if pos is not None:
            finishers.append((pos, r.get("horse_no")))
    finishers.sort()
    return [hn for _p, hn in finishers[:3] if hn]


def analyze(dates: list[str]) -> dict:
    rows: list[dict] = []
    for dc in dates:
        et = _load(_et_path_for(dc)) if _et_path_for(dc) else None
        sarr = _load(REPORTS / f"race_day_report_{dc}_SARR.json")
        res = _load(REPORTS / f"results_{dc}.json")
        if not (et and sarr and res):
            continue
        venue = et.get("meeting_venue") or res.get("venue", "?")
        et_by_rn = {r["race_number"]: r for r in et.get("races", [])}
        sarr_by_rn = {r["race_number"]: r for r in sarr.get("races", [])}
        for res_race in res.get("races", []):
            rn = res_race.get("race_number")
            er = et_by_rn.get(rn)
            sr = sarr_by_rn.get(rn)
            if not er or not sr:
                continue
            finishers = {}
            for r in res_race.get("runners", []):
                pos = _finish_pos(r)
                hn = r.get("horse_no")
                if pos is not None and hn is not None:
                    finishers[hn] = pos
            if not finishers:
                continue

            et_picks = _picks_top_n(er, 5)
            sarr_picks = _picks_top_n(sr, 5)
            et_m = _race_metrics(et_picks, finishers)
            sarr_m = _race_metrics(sarr_picks, finishers)
            mut_m = _mutual_metrics(et_picks, sarr_picks, finishers)
            actual_top3 = _actual_top3(res_race)

            rows.append({
                "date": dc,
                "venue": venue,
                "race": rn,
                "distance": res_race.get("distance"),
                "dist_bucket": _dist_bucket(res_race.get("distance")),
                "going": res_race.get("going") or res_race.get("actual_going"),
                "race_class": (res_race.get("race_class") or "").strip() or None,
                "surface": "AWT" if res_race.get("is_awt") else "Turf",
                "pace": er.get("pace"),
                "et_top1": et_picks[0] if et_picks else None,
                "sarr_top1": sarr_picks[0] if sarr_picks else None,
                "actual_top3": actual_top3,
                "agree_top1": 1 if (et_picks and sarr_picks
                                    and et_picks[0] == sarr_picks[0]) else 0,
                **{f"et_{k}": v for k, v in et_m.items()},
                **{f"sarr_{k}": v for k, v in sarr_m.items()},
                **mut_m,
            })

    def _agg(rs: list[dict], keys: list[str]) -> list[dict]:
        buckets: dict[tuple, list[dict]] = {}
        for r in rs:
            k = tuple(r.get(x) for x in keys)
            buckets.setdefault(k, []).append(r)
        out = []
        for k, vs in sorted(buckets.items(), key=lambda kv: str(kv[0])):
            row = {**{x: kv for x, kv in zip(keys, k)}, "n": len(vs)}
            for mk in ["top_pick_win", "top_pick_place", "top3_any_1st",
                       "top3_trifecta", "top3_in_top4"]:
                for model in ("et", "sarr"):
                    vals = [v[f"{model}_{mk}"] for v in vs
                            if v.get(f"{model}_{mk}") is not None]
                    row[f"{model}_{mk}_mean"] = (
                        round(mean(vals), 3) if vals else None)
            # Mutual metrics
            for mk in ["mutual_size", "mutual_count_in_top3",
                       "mutual_any_in_top3", "mutual_2plus_in_top3",
                       "mutual_3_in_top3", "mutual_winner_hit"]:
                vals = [v[mk] for v in vs if v.get(mk) is not None]
                row[f"{mk}_mean"] = (
                    round(mean(vals), 3) if vals else None)
            # Conditional: when mutual_size >= 1
            with_mut = [v for v in vs if v.get("mutual_size", 0) >= 1]
            if with_mut:
                row["cond_mut_any_in_top3"] = round(
                    mean(v["mutual_any_in_top3"] for v in with_mut), 3)
                row["cond_mut_winner_hit"] = round(
                    mean(v["mutual_winner_hit"] for v in with_mut), 3)
                row["n_with_mutual"] = len(with_mut)
            else:
                row["cond_mut_any_in_top3"] = None
                row["cond_mut_winner_hit"] = None
                row["n_with_mutual"] = 0
            row["agree_top1_pct"] = round(
                100 * mean([v["agree_top1"] for v in vs]), 1) if vs else None
            out.append(row)
        return out

    return {
        "dates_analysed": dates,
        "n_meetings": len(dates),
        "n_races": len(rows),
        "overall": _agg(rows, [])[0] if rows else None,
        "by_venue": _agg(rows, ["venue"]),
        "by_dist_bucket": _agg(rows, ["dist_bucket"]),
        "by_venue_dist": _agg(rows, ["venue", "dist_bucket"]),
        "by_pace": _agg(rows, ["pace"]),
        "by_going": _agg(rows, ["going"]),
        "by_class": _agg(rows, ["race_class"]),
        "by_surface": _agg(rows, ["surface"]),
        "by_mutual_size": _agg(rows, ["mutual_size"]),
        "per_race": rows,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _print_row(label: str, et_val, sarr_val, suffix: str = ""):
    def _fmt(v):
        if v is None:
            return "  —  "
        if isinstance(v, float):
            return f"{v*100:>5.1f}%"
        return str(v)
    delta = ""
    if et_val is not None and sarr_val is not None:
        d = (sarr_val - et_val) * 100
        delta = f"   Δ {d:+.1f}pp"
    print(f"  {label:<24}  ET {_fmt(et_val)}   SARR {_fmt(sarr_val)}{delta}{suffix}")


def _print_summary(result: dict):
    print(f"\n=== ET vs SARR — {result['n_meetings']} meetings, "
          f"{result['n_races']} races ===")
    o = result.get("overall")
    if not o:
        print("  (no data)")
        return
    print(f"  Top-1 agreement: {o['agree_top1_pct']:.1f}% "
          f"(models pick same rank-1)")
    print()
    _print_row("Top pick wins",      o["et_top_pick_win_mean"],   o["sarr_top_pick_win_mean"])
    _print_row("Top pick places",    o["et_top_pick_place_mean"], o["sarr_top_pick_place_mean"])
    _print_row("Top-3 contains 1st", o["et_top3_any_1st_mean"],   o["sarr_top3_any_1st_mean"])
    _print_row("Top-3 = trifecta",   o["et_top3_trifecta_mean"],  o["sarr_top3_trifecta_mean"])
    print(f"  Top-3 in top-4       ET {o['et_top3_in_top4_mean']:.2f}   "
          f"SARR {o['sarr_top3_in_top4_mean']:.2f}   "
          f"Δ {(o['sarr_top3_in_top4_mean']-o['et_top3_in_top4_mean']):+.2f}")

    # ── Mutual ET ∩ SARR picks ──────────────────────────────────────
    print(f"\n  --- Mutual ET ∩ SARR top-3 picks ---")
    print(f"  avg mutual set size   {o['mutual_size_mean']:.2f} / 3")
    print(f"  avg mutual in actual top-3   {o['mutual_count_in_top3_mean']:.2f}")
    print(f"  P(any mutual horse in top-3, unconditional) "
          f"{o['mutual_any_in_top3_mean']*100:.1f}%")
    print(f"  P(any mutual horse wins,    unconditional) "
          f"{o['mutual_winner_hit_mean']*100:.1f}%")
    print(f"  P(2+ mutual in top-3, unconditional) "
          f"{o['mutual_2plus_in_top3_mean']*100:.1f}%")
    print(f"  P(3  mutual in top-3, unconditional) "
          f"{o['mutual_3_in_top3_mean']*100:.1f}%")
    if o.get("n_with_mutual"):
        print(f"  Conditional on \u2265 1 mutual pick "
              f"(n={o['n_with_mutual']}):")
        print(f"     P(mutual in top-3) {o['cond_mut_any_in_top3']*100:.1f}%   "
              f"P(mutual wins) {o['cond_mut_winner_hit']*100:.1f}%")

    print(f"\n  --- By mutual_size ---")
    for row in result["by_mutual_size"]:
        ms = row.get("mutual_size")
        n = row["n"]
        pct = 100 * n / result["n_races"] if result["n_races"] else 0
        p_t3 = row.get("mutual_any_in_top3_mean")
        p_w = row.get("mutual_winner_hit_mean")
        p_2p = row.get("mutual_2plus_in_top3_mean")
        def _pct(v): return f"{v*100:>5.1f}%" if v is not None else "  —  "
        print(f"    size={ms}  n={n:>3} ({pct:>4.1f}%)  "
              f"any-in-top-3 {_pct(p_t3)}   "
              f"winner {_pct(p_w)}   "
              f"\u22652-in-top-3 {_pct(p_2p)}")

    for title, key in (("By venue", "by_venue"),
                       ("By distance bucket", "by_dist_bucket"),
                       ("By venue × distance", "by_venue_dist")):
        print(f"\n  --- {title} ---")
        for row in result[key]:
            label_parts = [str(row.get(k)) for k in row
                           if k not in ("n",) and not (
                               k.endswith("_mean") or k == "agree_top1_pct")]
            label = " · ".join(label_parts)
            et_w = row.get("et_top_pick_win_mean")
            sa_w = row.get("sarr_top_pick_win_mean")
            et_p = row.get("et_top_pick_place_mean")
            sa_p = row.get("sarr_top_pick_place_mean")
            def _pct(v): return f"{v*100:>5.1f}%" if v is not None else "  —  "
            print(f"    {label:<30}  n={row['n']:>3}  "
                  f"WIN  ET {_pct(et_w)} SARR {_pct(sa_w)}   "
                  f"PLC  ET {_pct(et_p)} SARR {_pct(sa_p)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", help="comma-separated YYYY-MM-DD list")
    args = ap.parse_args()

    available = _gather_dates()
    if args.dates:
        want = [s.strip().replace("-", "") for s in args.dates.split(",")
                if s.strip()]
        dates = [d for d in want if d in available]
    else:
        dates = available

    if not dates:
        print("No dates with ET report + SARR report + results.")
        print(f"(ET ∩ SARR ∩ results available: {available})")
        return 1

    result = analyze(dates)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    _print_summary(result)
    print(f"\nSaved → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
