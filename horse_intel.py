#!/usr/bin/env python3
"""Horse Intel aggregator — pivots existing results/predictions/commentary
into per-horse run histories.

Produces:
  reports/horse_intel/_index.json          {horse_name_upper: file_path, ...}
  reports/horse_intel/{slug}.json          {name, runs[...], summary{...}}

Each `runs[i]` contains:
  date, race_number, venue, distance, race_class, going, surface,
  finish, lbw, win_odds, draw, weight, jockey, trainer,
  finish_time_seconds, running_position, sectiontimes,
  et_rank, sarr_rank, projected_time, beat_proj_s,
  perf_score, perf_reasons[], tags[], polarity, comment_short

`summary` includes:
  n_runs, n_wins, n_top3, win_pct, top3_pct,
  avg_beat_proj, recurring_excuses{tag: count},
  blackbook_status (entry|None), perf_score_recent (last 5)

Idempotent — rebuild from scratch in a few seconds. Reads only files
already on disk (no scraping).

Usage:
    python horse_intel.py            # rebuild for all horses
    python horse_intel.py --horse "SON PAK FU"   # one horse, prints to stdout
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
OUT = REPORTS / "horse_intel"
OUT.mkdir(parents=True, exist_ok=True)


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Z0-9]+", "_", name.upper().strip()).strip("_")
    return s or "UNKNOWN"


def _u(s) -> str:
    return (s or "").upper().strip()


def _load_json(p: Path) -> Optional[dict]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _safe_int(v, default=99) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_float(v) -> Optional[float]:
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


# ── Performance scoring (mirrors backtest_model.find_exceptional_performers) ─

def _score_run(runner: dict, race: dict,
               et_pick: Optional[dict],
               race_median_ft: Optional[float],
               winner_ft: Optional[float]) -> tuple[float, list[str]]:
    """Same logic as backtest_model.find_exceptional_performers, but per
    runner. Returns (score, reasons[])."""
    reasons: list[str] = []
    score = 0.0
    place = _safe_int(runner.get("place"), 99)
    ft = _safe_float(runner.get("finish_time_seconds"))
    odds = _safe_float(str(runner.get("win_odds", "")).replace("$", "").strip())
    rp = str(runner.get("running_position") or "").strip()
    draw = runner.get("draw")
    pred_rank = (et_pick or {}).get("rank")
    pred_time = _safe_float((et_pick or {}).get("projected_time"))

    # 1. Dominant winner
    if place == 1 and winner_ft and race_median_ft:
        margin = race_median_ft - winner_ft
        if margin > 1.0:
            reasons.append(f"won by {margin:.1f}s vs median — dominant")
            score += margin

    # 2. Exceptional late kick
    secs_raw = runner.get("sectiontimes", []) or []
    valid_secs: list[float] = []
    for s in secs_raw:
        f = _safe_float(s)
        if f:
            valid_secs.append(f)
    if len(valid_secs) >= 2:
        last_sec = valid_secs[-1]
        avg_other = sum(valid_secs[:-1]) / max(len(valid_secs) - 1, 1)
        if last_sec < avg_other - 0.5 and place <= 4:
            reasons.append(
                f"late kick {last_sec:.2f}s vs {avg_other:.2f}s avg early"
            )
            score += (avg_other - last_sec)

    # 3. Beat projected time
    if pred_time and ft:
        beat_proj = pred_time - ft
        if beat_proj > 0.5 and place <= 4:
            reasons.append(f"ran {beat_proj:.2f}s faster than projected")
            score += beat_proj * 0.8

    # 4. Wide-draw place
    if place <= 3 and draw and str(draw).isdigit() and int(draw) >= 10:
        reasons.append(f"placed from wide draw ({draw})")
        score += 0.5

    # 5. Big-odds winner
    if place == 1 and odds and odds >= 10:
        reasons.append(f"won at ${odds:.1f} — market underrated")
        score += min(odds / 10, 2.0)

    # 6. Model wrong (low rank → high finish)
    if pred_rank and pred_rank >= 8 and place <= 3:
        reasons.append(f"model Rk#{pred_rank} → P{place}")
        score += 1.0

    # 7. Closed from far back
    first_pos = None
    m = re.match(r"(\d+)", rp)
    if m:
        first_pos = int(m.group(1))
    if first_pos and first_pos >= 8 and place <= 3:
        reasons.append(f"closed from pos {first_pos} to P{place}")
        score += 0.5

    # 8. Negative outliers (suggest underperformance)
    if pred_rank and pred_rank <= 4 and place > 6:
        reasons.append(f"model Rk#{pred_rank} → P{place} (underperformed)")
        score -= 0.7
    if pred_time and ft and (ft - pred_time) > 0.7:
        reasons.append(f"ran {ft - pred_time:.2f}s slower than projected")
        score -= (ft - pred_time) * 0.5

    return round(score, 3), reasons


def _race_median_winner_ft(race: dict) -> tuple[Optional[float], Optional[float]]:
    fts = [_safe_float(r.get("finish_time_seconds"))
           for r in race.get("runners", []) or []]
    fts = [f for f in fts if f]
    median_ft = (sorted(fts)[len(fts) // 2] if fts else None)
    winner_ft = None
    for r in race.get("runners", []) or []:
        if _safe_int(r.get("place"), 99) == 1:
            winner_ft = _safe_float(r.get("finish_time_seconds"))
            break
    return median_ft, winner_ft


def _find_pick(rdr_race: Optional[dict], horse_name_u: str,
               horse_no: Optional[int]) -> Optional[dict]:
    if not rdr_race:
        return None
    for p in rdr_race.get("picks", []) or []:
        if horse_no is not None and p.get("horse_no") == horse_no:
            return p
        if _u(p.get("horse_name")) == horse_name_u:
            return p
    return None


# ── Main aggregator ─────────────────────────────────────────────────────────

def build_index() -> dict:
    by_horse: Dict[str, List[dict]] = {}

    for res_path in sorted(REPORTS.glob("results_*.json")):
        d = _load_json(res_path)
        if not d:
            continue
        date_iso = d.get("date") or ""
        venue = d.get("venue") or ""
        # date_compact for paired report lookups
        dc = date_iso.replace("-", "") if date_iso else ""
        if not dc:
            m = re.search(r"results_(\d{8})", res_path.name)
            if m:
                dc = m.group(1)

        # Companion files
        comm = _load_json(REPORTS / f"commentary_{dc}.json") if dc else None
        # ET / v4.4 race day report. Try a couple of suffixes.
        rdr_et = None
        for suffix in ("v4.4", "v4.6", "v3.4.8"):
            p = REPORTS / f"race_day_report_{dc}_{suffix}.json"
            if p.exists():
                rdr_et = _load_json(p)
                break
        rdr_sarr = _load_json(REPORTS / f"race_day_report_{dc}_SARR.json") \
            if dc else None

        # Index commentary by (race_no, horse_no/upper-name)
        comm_lookup: Dict[tuple, dict] = {}
        for race in (comm or {}).get("races", []) or []:
            rn = _safe_int(race.get("race_number"), -1)
            for h in race.get("horses", []) or []:
                key_no = (rn, _safe_int(h.get("horse_no"), -1))
                key_nm = (rn, _u(h.get("horse_name")))
                comm_lookup[key_no] = h
                comm_lookup[key_nm] = h

        # Index ET/SARR picks by race_no
        et_by_rn = {_safe_int(r.get("race_number"), -1): r
                    for r in (rdr_et or {}).get("races", []) or []}
        sarr_by_rn = {_safe_int(r.get("race_number"), -1): r
                      for r in (rdr_sarr or {}).get("races", []) or []}

        for race in d.get("races", []) or []:
            rn = _safe_int(race.get("race_number"), -1)
            distance = race.get("distance")
            race_class = race.get("race_class")
            going = race.get("going") or race.get("actual_going")
            surface = "AWT" if race.get("is_awt") else "Turf"
            actual_pace = race.get("actual_pace_label")
            median_ft, winner_ft = _race_median_winner_ft(race)
            et_race = et_by_rn.get(rn)
            sarr_race = sarr_by_rn.get(rn)

            for runner in race.get("runners", []) or []:
                hname_u = _u(runner.get("horse_name"))
                if not hname_u:
                    continue
                hno = _safe_int(runner.get("horse_no"), -1)
                place = _safe_int(runner.get("place"), 99)
                if place >= 90:
                    # WD / scratch / DNF — skip
                    continue

                et_pick = _find_pick(et_race, hname_u, hno)
                sarr_pick = _find_pick(sarr_race, hname_u, hno)
                perf, reasons = _score_run(
                    runner, race, et_pick, median_ft, winner_ft
                )

                # Commentary join
                cmt = (comm_lookup.get((rn, hno))
                       or comm_lookup.get((rn, hname_u)))
                tags = list((cmt or {}).get("tags") or [])
                polarity = (cmt or {}).get("polarity_score")
                cmt_short = (cmt or {}).get("short") or ""

                proj = _safe_float((et_pick or {}).get("projected_time"))
                ft = _safe_float(runner.get("finish_time_seconds"))
                beat = (proj - ft) if (proj and ft) else None

                run = {
                    "date":             date_iso,
                    "venue":            venue,
                    "race_number":      rn,
                    "distance":         distance,
                    "race_class":       race_class,
                    "going":            going,
                    "surface":          surface,
                    "actual_pace":      actual_pace,
                    "horse_no":         hno if hno != -1 else None,
                    "finish":           place,
                    "lbw":              runner.get("lbw"),
                    "win_odds":         runner.get("win_odds"),
                    "draw":             runner.get("draw"),
                    "weight":           runner.get("actual_weight"),
                    "jockey":           runner.get("jockey"),
                    "trainer":          runner.get("trainer"),
                    "finish_time_s":    ft,
                    "running_position": runner.get("running_position"),
                    "sectiontimes":     runner.get("sectiontimes"),
                    "et_rank":          (et_pick or {}).get("rank"),
                    "et_proj_time":     proj,
                    "et_win_prob":      (et_pick or {}).get("win_prob"),
                    "sarr_rank":        (sarr_pick or {}).get("rank"),
                    "beat_proj_s":      round(beat, 3) if beat is not None else None,
                    "perf_score":       perf,
                    "perf_reasons":     reasons,
                    "tags":             tags,
                    "polarity":         polarity,
                    "comment_short":    cmt_short,
                }
                by_horse.setdefault(hname_u, []).append(run)

    # Pull blackbook into a quick-lookup
    bb = _load_json(ROOT / "blackbook.json") or {}
    bb_lookup: Dict[str, dict] = {}
    for entry in bb.get("entries", []) or []:
        bb_lookup[_u(entry.get("horse_name"))] = entry

    # Write per-horse files + index
    index: Dict[str, str] = {}
    for hname, runs in by_horse.items():
        runs.sort(key=lambda r: (r["date"] or "", r["race_number"]))
        n = len(runs)
        n_w = sum(1 for r in runs if r["finish"] == 1)
        n_t3 = sum(1 for r in runs if r["finish"] <= 3)
        beats = [r["beat_proj_s"] for r in runs if r["beat_proj_s"] is not None]
        avg_beat = round(sum(beats) / len(beats), 2) if beats else None
        rec_tags = Counter()
        for r in runs:
            for t in r.get("tags", []) or []:
                # Skip neutral/admin tags; focus on actionable ones
                if t in ("sampling", "vet_ok", "jumped_fairly", "bumped_mid"):
                    continue
                rec_tags[t] += 1
        recent_perf = [r["perf_score"] for r in runs[-5:]]

        bb_entry = bb_lookup.get(hname)

        summary = {
            "n_runs":            n,
            "n_wins":            n_w,
            "n_top3":            n_t3,
            "win_pct":           round(n_w / n * 100, 1) if n else 0.0,
            "top3_pct":          round(n_t3 / n * 100, 1) if n else 0.0,
            "avg_beat_proj_s":   avg_beat,
            "recurring_excuses": dict(rec_tags.most_common(8)),
            "perf_score_recent": recent_perf,
            "perf_score_mean":   round(sum(r["perf_score"] for r in runs) / n, 3)
                                  if n else None,
            "blackbook_status":  bb_entry.get("confidence") if bb_entry else None,
            "blackbook_note":    bb_entry.get("note") if bb_entry else None,
        }

        slug = _slug(hname)
        rec = {
            "horse_name": hname,
            "summary":    summary,
            "runs":       runs,
        }
        (OUT / f"{slug}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        index[hname] = f"{slug}.json"

    (OUT / "_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"horses": len(by_horse),
            "total_runs": sum(len(rs) for rs in by_horse.values())}


def load_horse(horse_name: str) -> Optional[dict]:
    name_u = _u(horse_name)
    idx_path = OUT / "_index.json"
    if not idx_path.exists():
        return None
    idx = _load_json(idx_path) or {}
    fname = idx.get(name_u)
    if not fname:
        return None
    return _load_json(OUT / fname)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horse", help="print one horse's record after build")
    ap.add_argument("--no-rebuild", action="store_true",
                    help="skip rebuild, just print --horse")
    args = ap.parse_args()

    if not args.no_rebuild:
        stats = build_index()
        print(f"[horse_intel] horses: {stats['horses']}  "
              f"total runs: {stats['total_runs']}")

    if args.horse:
        rec = load_horse(args.horse)
        if rec is None:
            print(f"no record for {args.horse!r}")
        else:
            print(json.dumps(rec, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
