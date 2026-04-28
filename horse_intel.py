#!/usr/bin/env python3
"""Horse Intel aggregator — pivots the master results database
(`hkjc_results_updated.xlsx`, ~18k rows / 1.8k horses since Sep-2024)
plus all per-meeting reports into per-horse run histories.

Primary source : `hkjc_results_updated.xlsx`  (full 2024-25 + 2025-26 seasons)
Enrichments    : `reports/results_YYYYMMDD.json`        (latest sectional data,
                                                         actual_pace, is_awt)
                 `reports/commentary_YYYYMMDD.json`     (incident tags,
                                                         polarity, short notes)
                 `reports/race_day_report_*_v4.6.json`  (ET picks → ranks,
                  /v4.4 / v3.4.8                         projected times,
                                                         win probs)
                 `reports/race_day_report_*_SARR.json`  (SARR ranks)
                 `blackbook.json`                       (subjective notes)

Produces:
  reports/horse_intel/_index.json   {horse_name_upper: {file, n_runs, ...}}
  reports/horse_intel/{slug}.json   {horse_name, summary{...}, runs[...]}
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
OUT = REPORTS / "horse_intel"
OUT.mkdir(parents=True, exist_ok=True)
DB_FILE = ROOT / "hkjc_results_updated.xlsx"


# ── helpers ────────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    s = re.sub(r"[^A-Z0-9]+", "_", name.upper().strip()).strip("_")
    return s or "UNKNOWN"


def _u(s) -> str:
    return (str(s) if s is not None else "").upper().strip()


def _load_json(p: Path) -> Optional[dict]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _safe_int(v, default: int = 99) -> int:
    try:
        if v is None or (isinstance(v, float) and v != v):
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _safe_float(v) -> Optional[float]:
    try:
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.upper() in {"NAN", "NA", "-", ""}:
            return None
        f = float(s)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_read_excel(path: Path) -> pd.DataFrame:
    """OneDrive-tolerant xlsx read."""
    for attempt in range(2):
        target = path
        if attempt == 1:
            target = Path(tempfile.gettempdir()) / path.name
            shutil.copy2(path, target)
        try:
            return pd.read_excel(target)
        except (PermissionError, zipfile.BadZipFile):
            if attempt == 0:
                continue
            raise


# ── Performance scoring ────────────────────────────────────────────────────

def _score_run(run: dict,
               et_pick: Optional[dict],
               race_median_ft: Optional[float],
               winner_ft: Optional[float]) -> Tuple[float, list[str]]:
    """8-rule contextualised performance score (mirrors
    backtest_model.find_exceptional_performers)."""
    reasons: list[str] = []
    score = 0.0
    place = _safe_int(run.get("finish"), 99)
    ft = _safe_float(run.get("finish_time_s"))
    odds = _safe_float(str(run.get("win_odds", "")).replace("$", "").strip())
    rp = str(run.get("running_position") or "").strip()
    draw = run.get("draw")
    pred_rank = (et_pick or {}).get("rank")
    pred_time = _safe_float((et_pick or {}).get("projected_time"))

    # 1. Dominant winner
    if place == 1 and winner_ft and race_median_ft:
        margin = race_median_ft - winner_ft
        if margin > 1.0:
            reasons.append(f"won by {margin:.1f}s vs median — dominant")
            score += margin

    # 2. Late kick
    secs_raw = run.get("sectiontimes") or []
    if isinstance(secs_raw, str):
        secs_raw = [x.strip() for x in re.split(r"[;\s,]+", secs_raw)
                    if x.strip()]
    valid_secs = [f for f in (_safe_float(s) for s in secs_raw) if f]
    if len(valid_secs) >= 2:
        last_sec = valid_secs[-1]
        avg_other = sum(valid_secs[:-1]) / max(len(valid_secs) - 1, 1)
        if last_sec < avg_other - 0.5 and place <= 4:
            reasons.append(
                f"late kick {last_sec:.2f}s vs {avg_other:.2f}s avg early"
            )
            score += (avg_other - last_sec)

    # 3. Beat projection
    if pred_time and ft:
        beat_proj = pred_time - ft
        if beat_proj > 0.5 and place <= 4:
            reasons.append(f"ran {beat_proj:.2f}s faster than projected")
            score += beat_proj * 0.8

    # 4. Wide-draw place
    if place <= 3 and _safe_int(draw, 0) >= 10:
        reasons.append(f"placed from wide draw ({draw})")
        score += 0.5

    # 5. Big-odds winner
    if place == 1 and odds and odds >= 10:
        reasons.append(f"won at ${odds:.1f} — market underrated")
        score += min(odds / 10, 2.0)

    # 6. Model wrong
    if pred_rank and pred_rank >= 8 and place <= 3:
        reasons.append(f"model Rk#{pred_rank} → P{place}")
        score += 1.0

    # 7. Closed from far back
    m = re.match(r"(\d+)", rp)
    if m and int(m.group(1)) >= 8 and place <= 3:
        reasons.append(f"closed from pos {m.group(1)} to P{place}")
        score += 0.5

    # 8. Negative outliers
    if pred_rank and pred_rank <= 4 and place > 6:
        reasons.append(f"model Rk#{pred_rank} → P{place} (underperformed)")
        score -= 0.7
    if pred_time and ft and (ft - pred_time) > 0.7:
        reasons.append(f"ran {ft - pred_time:.2f}s slower than projected")
        score -= (ft - pred_time) * 0.5

    return round(score, 3), reasons


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


# ── Step 1 — base history from master DB ───────────────────────────────────

def _runs_from_db() -> Tuple[Dict[str, List[dict]],
                              Dict[Tuple[str, int], dict]]:
    """Returns
        by_horse  : { 'HORSE NAME': [run_dict, ...] }
        race_pack : { (date_iso, race_no): {'median_ft', 'winner_ft'} }
    """
    if not DB_FILE.exists():
        print(f"[horse_intel] master DB not found: {DB_FILE}")
        return {}, {}

    df = _safe_read_excel(DB_FILE)
    df["race_date"] = pd.to_datetime(df["race_date"], errors="coerce")
    df = df.dropna(subset=["race_date", "horse_name"])
    df["place_int"] = pd.to_numeric(df["place"], errors="coerce")
    df["ft"] = pd.to_numeric(df.get("finish_time_seconds"), errors="coerce")

    by_horse: Dict[str, List[dict]] = defaultdict(list)
    race_pack: Dict[Tuple[str, int], dict] = {}

    track_to_venue = {
        "ST": "ST", "HV": "HV",
        "SHA TIN": "ST", "SHATIN": "ST",
        "HAPPY VALLEY": "HV",
    }

    for (rd, rn), grp in df.groupby(["race_date", "race_number"], sort=False):
        rn_int = _safe_int(rn, -1)
        if rn_int < 0:
            continue
        date_iso = rd.strftime("%Y-%m-%d")

        fts = [f for f in grp["ft"].tolist() if pd.notna(f)]
        median_ft = sorted(fts)[len(fts) // 2] if fts else None
        winners = grp[grp["place_int"] == 1]["ft"].tolist()
        winner_ft = next((f for f in winners if pd.notna(f)), None)
        race_pack[(date_iso, rn_int)] = {
            "median_ft": median_ft,
            "winner_ft": winner_ft,
        }

        first = grp.iloc[0]
        venue = track_to_venue.get(_u(first.get("race_track")),
                                   _u(first.get("race_track")))
        track_type = _u(first.get("track_type"))
        surface = "AWT" if "ALL WEATHER" in track_type else "Turf"

        for _, row in grp.iterrows():
            place = _safe_int(row.get("place_int"), 99)
            if place >= 90:
                continue
            hname_u = _u(row.get("horse_name"))
            if not hname_u:
                continue

            sec_raw = row.get("sectiontimes")
            if isinstance(sec_raw, str) and sec_raw:
                sec_list = [x.strip() for x in re.split(r"[;,]+", sec_raw)
                            if x.strip()]
            else:
                sec_list = []

            run = {
                "date":             date_iso,
                "venue":            venue,
                "race_number":      rn_int,
                "distance":         _safe_int(row.get("distance"), 0) or None,
                "race_class":       (str(row.get("race_class"))
                                     if pd.notna(row.get("race_class"))
                                     else None),
                "going":            (str(row.get("going"))
                                     if pd.notna(row.get("going")) else None),
                "surface":          surface,
                "actual_pace":      None,
                "horse_no":         (_safe_int(row.get("horse_number"), 0)
                                     or None),
                "horse_name":       hname_u,
                "finish":           place,
                "lbw":              (str(row.get("lbw"))
                                     if pd.notna(row.get("lbw")) else None),
                "win_odds":         (str(row.get("win_odds"))
                                     if pd.notna(row.get("win_odds"))
                                     else None),
                "draw":             (_safe_int(row.get("draw"), 0) or None),
                "weight":           (_safe_int(row.get("actual_weight"), 0)
                                     or None),
                "jockey":           (str(row.get("jockey"))
                                     if pd.notna(row.get("jockey"))
                                     else None),
                "trainer":          (str(row.get("trainer"))
                                     if pd.notna(row.get("trainer"))
                                     else None),
                "finish_time_s":    (float(row.get("ft"))
                                     if pd.notna(row.get("ft")) else None),
                "running_position": (str(row.get("running_positions"))
                                     if pd.notna(row.get("running_positions"))
                                     else None),
                "sectiontimes":     sec_list,
                "et_rank":          None,
                "et_proj_time":     None,
                "et_win_prob":      None,
                "sarr_rank":        None,
                "beat_proj_s":      None,
                "perf_score":       0.0,
                "perf_reasons":     [],
                "tags":             [],
                "polarity":         None,
                "comment_short":    "",
            }
            by_horse[hname_u].append(run)

    return by_horse, race_pack


# ── Step 2 — reports overlay ───────────────────────────────────────────────

def _apply_reports_overlay(by_horse: Dict[str, List[dict]],
                           race_pack: Dict[Tuple[str, int], dict]) -> None:
    """Enrich existing DB-sourced runs with actual_pace, AWT flag,
    et_rank/projection, sarr_rank, commentary tags. Synthesise runs for
    meetings that are in reports but not yet in the DB."""

    run_lookup: Dict[Tuple[str, int, str], dict] = {}
    for hname, runs in by_horse.items():
        for r in runs:
            run_lookup[(r["date"], r["race_number"], hname)] = r

    for res_path in sorted(REPORTS.glob("results_*.json")):
        d = _load_json(res_path)
        if not d:
            continue
        date_iso = d.get("date") or ""
        venue_top = (d.get("venue") or "").upper()
        m = re.search(r"results_(\d{8})", res_path.name)
        dc = (date_iso.replace("-", "") if date_iso else
              (m.group(1) if m else ""))
        if not dc:
            continue
        if not date_iso:
            date_iso = f"{dc[:4]}-{dc[4:6]}-{dc[6:8]}"

        comm = _load_json(REPORTS / f"commentary_{dc}.json")
        rdr_et = None
        for suffix in ("v4.6", "v4.4", "v3.4.8"):
            p = REPORTS / f"race_day_report_{dc}_{suffix}.json"
            if p.exists():
                rdr_et = _load_json(p)
                break
        rdr_sarr = _load_json(REPORTS / f"race_day_report_{dc}_SARR.json")

        comm_lookup: Dict[Tuple[int, str], dict] = {}
        for race in (comm or {}).get("races", []) or []:
            rn = _safe_int(race.get("race_number"), -1)
            for h in race.get("horses", []) or []:
                comm_lookup[(rn, _u(h.get("horse_name")))] = h
        et_by_rn = {_safe_int(r.get("race_number"), -1): r
                    for r in (rdr_et or {}).get("races", []) or []}
        sarr_by_rn = {_safe_int(r.get("race_number"), -1): r
                      for r in (rdr_sarr or {}).get("races", []) or []}

        for race in d.get("races", []) or []:
            rn = _safe_int(race.get("race_number"), -1)
            actual_pace = race.get("actual_pace_label")
            is_awt = bool(race.get("is_awt"))
            et_race = et_by_rn.get(rn)
            sarr_race = sarr_by_rn.get(rn)

            pack = race_pack.get((date_iso, rn))
            if not pack:
                fts = [_safe_float(r.get("finish_time_seconds"))
                       for r in race.get("runners", []) or []]
                fts = [f for f in fts if f]
                pack = {
                    "median_ft": (sorted(fts)[len(fts) // 2]
                                  if fts else None),
                    "winner_ft": next(
                        (_safe_float(r.get("finish_time_seconds"))
                         for r in race.get("runners", []) or []
                         if _safe_int(r.get("place"), 99) == 1), None),
                }
                race_pack[(date_iso, rn)] = pack

            for runner in race.get("runners", []) or []:
                hname_u = _u(runner.get("horse_name"))
                if not hname_u:
                    continue
                hno = _safe_int(runner.get("horse_no"), -1)
                place = _safe_int(runner.get("place"), 99)
                if place >= 90:
                    continue

                run = run_lookup.get((date_iso, rn, hname_u))
                if run is None:
                    run = {
                        "date":             date_iso,
                        "venue":            venue_top,
                        "race_number":      rn,
                        "distance":         race.get("distance"),
                        "race_class":       race.get("race_class"),
                        "going":            race.get("going")
                                            or race.get("actual_going"),
                        "surface":          "AWT" if is_awt else "Turf",
                        "actual_pace":      None,
                        "horse_no":         hno if hno != -1 else None,
                        "horse_name":       hname_u,
                        "finish":           place,
                        "lbw":              runner.get("lbw"),
                        "win_odds":         runner.get("win_odds"),
                        "draw":             runner.get("draw"),
                        "weight":           runner.get("actual_weight"),
                        "jockey":           runner.get("jockey"),
                        "trainer":          runner.get("trainer"),
                        "finish_time_s":    _safe_float(
                            runner.get("finish_time_seconds")),
                        "running_position": runner.get("running_position"),
                        "sectiontimes":     runner.get("sectiontimes") or [],
                        "et_rank":          None,
                        "et_proj_time":     None,
                        "et_win_prob":      None,
                        "sarr_rank":        None,
                        "beat_proj_s":      None,
                        "perf_score":       0.0,
                        "perf_reasons":     [],
                        "tags":             [],
                        "polarity":         None,
                        "comment_short":    "",
                    }
                    by_horse.setdefault(hname_u, []).append(run)
                    run_lookup[(date_iso, rn, hname_u)] = run

                run["actual_pace"] = actual_pace
                if is_awt:
                    run["surface"] = "AWT"

                et_pick = _find_pick(et_race, hname_u, hno)
                if et_pick:
                    run["et_rank"] = et_pick.get("rank")
                    run["et_proj_time"] = _safe_float(
                        et_pick.get("projected_time"))
                    run["et_win_prob"] = et_pick.get("win_prob")
                sarr_pick = _find_pick(sarr_race, hname_u, hno)
                if sarr_pick:
                    run["sarr_rank"] = sarr_pick.get("rank")

                cmt = comm_lookup.get((rn, hname_u))
                if cmt:
                    run["tags"] = list(cmt.get("tags") or [])
                    run["polarity"] = cmt.get("polarity_score")
                    run["comment_short"] = cmt.get("short") or ""


# ── Step 3 — score & write ─────────────────────────────────────────────────

def build_index(verbose: bool = True) -> dict:
    by_horse, race_pack = _runs_from_db()
    if verbose:
        print(f"[horse_intel] DB: {len(by_horse)} horses, "
              f"{sum(len(v) for v in by_horse.values())} runs")
    _apply_reports_overlay(by_horse, race_pack)

    for hname, runs in by_horse.items():
        for run in runs:
            pack = race_pack.get((run["date"], run["race_number"])) or {}
            et_pick = None
            if run.get("et_rank") is not None:
                et_pick = {
                    "rank": run["et_rank"],
                    "projected_time": run["et_proj_time"],
                    "win_prob": run["et_win_prob"],
                }
            beat = None
            if run.get("et_proj_time") and run.get("finish_time_s"):
                beat = run["et_proj_time"] - run["finish_time_s"]
            run["beat_proj_s"] = (round(beat, 3)
                                  if beat is not None else None)
            score, reasons = _score_run(
                run, et_pick, pack.get("median_ft"), pack.get("winner_ft"))
            run["perf_score"] = score
            run["perf_reasons"] = reasons

    bb = _load_json(ROOT / "blackbook.json") or {}
    bb_lookup: Dict[str, dict] = {}
    for entry in bb.get("entries", []) or []:
        bb_lookup[_u(entry.get("horse_name"))] = entry

    # Wipe stale per-horse files (DB may now include horses that were
    # not in the previous reports-only build, and vice-versa).
    if OUT.exists():
        for old in OUT.glob("*.json"):
            if old.name != "_index.json":
                try:
                    old.unlink()
                except OSError:
                    pass

    index: Dict[str, dict] = {}
    for hname, runs in by_horse.items():
        runs.sort(key=lambda r: (r["date"] or "", r["race_number"]))
        n = len(runs)
        if n == 0:
            continue
        n_w = sum(1 for r in runs if r["finish"] == 1)
        n_t3 = sum(1 for r in runs if r["finish"] <= 3)
        beats = [r["beat_proj_s"] for r in runs
                 if r["beat_proj_s"] is not None]
        avg_beat = round(sum(beats) / len(beats), 2) if beats else None
        rec_tags: Counter = Counter()
        for r in runs:
            for t in r.get("tags", []) or []:
                if t in ("sampling", "vet_ok",
                         "jumped_fairly", "bumped_mid"):
                    continue
                rec_tags[t] += 1
        recent_perf = [r["perf_score"] for r in runs[-5:]]

        bb_entry = bb_lookup.get(hname)
        last = runs[-1]

        summary = {
            "n_runs":            n,
            "n_wins":            n_w,
            "n_top3":            n_t3,
            "win_pct":           round(n_w / n * 100, 1) if n else 0.0,
            "top3_pct":          round(n_t3 / n * 100, 1) if n else 0.0,
            "avg_beat_proj_s":   avg_beat,
            "recurring_excuses": dict(rec_tags.most_common(8)),
            "perf_score_recent": recent_perf,
            "perf_score_mean":   round(
                sum(r["perf_score"] for r in runs) / n, 3) if n else None,
            "blackbook_status":  (bb_entry.get("confidence")
                                  if bb_entry else None),
            "blackbook_note":    bb_entry.get("note") if bb_entry else None,
            "last_run_date":     last["date"],
            "last_finish":       last["finish"],
            "last_jockey":       last.get("jockey"),
            "last_trainer":      last.get("trainer"),
        }

        slug = _slug(hname)
        rec = {"horse_name": hname, "summary": summary, "runs": runs}
        (OUT / f"{slug}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        index[hname] = {
            "file":             f"{slug}.json",
            "n_runs":           n,
            "win_pct":          summary["win_pct"],
            "last_run_date":    summary["last_run_date"],
            "last_finish":      summary["last_finish"],
            "perf_score_mean":  summary["perf_score_mean"],
            "blackbook":        bool(bb_entry),
            "last_jockey":      summary["last_jockey"],
            "last_trainer":     summary["last_trainer"],
        }

    (OUT / "_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "horses": len(index),
        "total_runs": sum(len(rs) for rs in by_horse.values()),
    }


def load_horse(horse_name: str) -> Optional[dict]:
    name_u = _u(horse_name)
    idx_path = OUT / "_index.json"
    if not idx_path.exists():
        return None
    idx = _load_json(idx_path) or {}
    rec = idx.get(name_u)
    if not rec:
        return None
    fname = rec["file"] if isinstance(rec, dict) else rec
    return _load_json(OUT / fname)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horse", help="print one horse after build")
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
