#!/usr/bin/env python3
"""
backtest_unified.py — ET + SARR + Market unified backtest engine.

Replaces the ET-only `backtest_model.py` for the dashboard. Produces a single
per-meeting JSON with comparable metrics across **three** model streams:
    • ET     (race_day_report_YYYYMMDD_v4.4.json)
    • SARR   (race_day_report_YYYYMMDD_SARR.json)
    • Market (cheapest win_odds — pure favourite baseline)

Plus a per-race + per-horse breakdown the dashboard can expand inline.

Outputs
-------
    reports/backtest_unified_YYYYMMDD.json     per meeting
    reports/backtest_unified_<window>.json     aggregate (last7, last30,
                                                month-YYYY-MM, season-YYYY,
                                                all)

Usage
-----
    python backtest_unified.py --date 2026-04-22
    python backtest_unified.py --month 2026-04
    python backtest_unified.py --window last30
    python backtest_unified.py --all

The dashboard's Backtest page consumes these unified JSONs; the legacy
`backtest_*.json` files are still produced by `backtest_model.py` and remain
readable for historical reference.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

BASE = Path(__file__).parent
REPORTS = BASE / "reports"


# ──────────────────────────────────────────────────────────────────────────
# Loading helpers
# ──────────────────────────────────────────────────────────────────────────
def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _et_path(dc: str) -> Path | None:
    for tag in ("v4.4", "v3.4.8"):
        p = REPORTS / f"race_day_report_{dc}_{tag}.json"
        if p.exists():
            return p
    return None


def _sarr_path(dc: str) -> Path:
    return REPORTS / f"race_day_report_{dc}_SARR.json"


def _results_path(dc: str) -> Path:
    return REPORTS / f"results_{dc}.json"


def _fnum(v) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _finish_pos(r: dict) -> int | None:
    try:
        return int(str(r.get("place")).strip())
    except (TypeError, ValueError):
        return None


def find_matched_dates() -> list[str]:
    """Return YYYYMMDD strings where ET, SARR, AND results all exist."""
    et = set()
    for tag in ("v4.4", "v3.4.8"):
        for p in REPORTS.glob(f"race_day_report_????????_{tag}.json"):
            et.add(p.name[len("race_day_report_"):][:8])
    sarr = {p.name[len("race_day_report_"):][:8]
            for p in REPORTS.glob("race_day_report_????????_SARR.json")}
    res = {p.name[len("results_"):][:8]
           for p in REPORTS.glob("results_????????.json")}
    return sorted(et & res)  # SARR optional — graceful degrade per meeting


# ──────────────────────────────────────────────────────────────────────────
# Per-race row builder
# ──────────────────────────────────────────────────────────────────────────
def _build_race_row(rn: int, et_race: dict | None, sa_race: dict | None,
                    res_race: dict) -> dict | None:
    """Compute one comparable row for a single race."""
    runners = res_race.get("runners", []) or []
    finishers: dict[int, int] = {}
    odds_by: dict[int, float] = {}
    time_by: dict[int, float] = {}
    name_by: dict[int, str] = {}
    sec_by: dict[int, list] = {}
    for r in runners:
        hn = r.get("horse_no")
        if hn is None:
            continue
        try:
            hn = int(hn)
        except (TypeError, ValueError):
            continue
        pos = _finish_pos(r)
        if pos is not None:
            finishers[hn] = pos
        wo = _fnum(r.get("win_odds"))
        if wo is not None:
            odds_by[hn] = wo
        ft = _fnum(r.get("finish_time_seconds"))
        if ft is not None:
            time_by[hn] = ft
        nm = r.get("horse_name")
        if nm:
            name_by[hn] = str(nm)
        st = r.get("sectiontimes")
        if isinstance(st, list):
            sec_by[hn] = st

    if not finishers:
        return None

    # Picks (top-5 each)
    et_picks = []
    if et_race:
        for p in (et_race.get("picks") or [])[:5]:
            try:
                et_picks.append(int(p["horse_no"]))
            except (KeyError, TypeError, ValueError):
                pass
    sa_picks = []
    if sa_race:
        for p in (sa_race.get("picks") or [])[:5]:
            try:
                sa_picks.append(int(p["horse_no"]))
            except (KeyError, TypeError, ValueError):
                pass
    mkt_rank = sorted(odds_by.items(), key=lambda kv: kv[1])
    mkt_picks = [hn for hn, _ in mkt_rank[:5]]

    actual_winner = next((hn for hn, p in finishers.items() if p == 1), None)
    actual_top3 = sorted([hn for hn, p in finishers.items() if p <= 3],
                         key=lambda h: finishers[h])

    def _model_metrics(picks: list[int]) -> dict:
        if not picks:
            return {"win": None, "plc": None, "top3_has_w": None,
                    "top3_overlap": None, "top1_in_top4": None}
        t1 = picks[0]
        t1_pos = finishers.get(t1)
        top3 = picks[:3]
        top3_pos = [finishers.get(h) for h in top3]
        top3_overlap = len(set(top3) & set(actual_top3))
        return {
            "win": int(t1_pos == 1) if t1_pos is not None else 0,
            "plc": int(t1_pos is not None and t1_pos <= 3),
            "top3_has_w": int(any(p == 1 for p in top3_pos if p is not None)),
            "top3_overlap": top3_overlap,
            "top1_in_top4": int(t1_pos is not None and t1_pos <= 4),
        }

    et_m = _model_metrics(et_picks)
    sa_m = _model_metrics(sa_picks)
    mk_m = _model_metrics(mkt_picks)

    agree_top1 = bool(et_picks and sa_picks and et_picks[0] == sa_picks[0])
    union_t3 = list(dict.fromkeys((et_picks[:3] + sa_picks[:3])))
    union_has_w = int(actual_winner in union_t3) if actual_winner else 0

    # ET projected-time error for the actual winner (calibration check).
    et_proj_winner = None
    et_proj_err = None
    if actual_winner is not None and et_race:
        for p in (et_race.get("picks") or []):
            if p.get("horse_no") == actual_winner:
                et_proj_winner = _fnum(p.get("projected_time"))
                break
        if et_proj_winner is not None and time_by.get(actual_winner) is not None:
            et_proj_err = round(et_proj_winner - time_by[actual_winner], 3)

    # Per-horse detail rows for the dashboard expander.
    et_pick_by = {p.get("horse_no"): p for p in (et_race.get("picks") or [])} if et_race else {}
    sa_pick_by = {p.get("horse_no"): p for p in (sa_race.get("picks") or [])} if sa_race else {}
    horse_rows = []
    for hn, place in sorted(finishers.items(), key=lambda kv: kv[1]):
        ep = et_pick_by.get(hn)
        sp = sa_pick_by.get(hn)
        horse_rows.append({
            "horse_no": hn,
            "horse_name": name_by.get(hn, "?"),
            "actual_place": place,
            "win_odds": odds_by.get(hn),
            "finish_time": time_by.get(hn),
            "et_rank": ep.get("rank") if ep else None,
            "et_proj": _fnum(ep.get("projected_time")) if ep else None,
            "et_proj_pre_pace": _fnum(ep.get("proj_pre_pace")) if ep else None,
            "et_style": ep.get("style") if ep else None,
            "et_win_prob": _fnum(ep.get("win_prob")) if ep else None,
            "sa_rank": sp.get("rank") if sp else None,
            "sa_score": _fnum(sp.get("sarr")) if sp else None,
            "sa_style": sp.get("style") if sp else None,
        })

    # Spearman rank correlation (model rank vs actual place) for both models.
    def _rank_corr(pick_list: list[dict]) -> float | None:
        pairs = []
        for p in pick_list:
            hn = p.get("horse_no")
            if hn in finishers:
                rk = p.get("rank")
                try:
                    pairs.append((int(rk), int(finishers[hn])))
                except (TypeError, ValueError):
                    pass
        return _spearman(pairs) if len(pairs) >= 3 else None

    et_rho = _rank_corr(et_race.get("picks", [])) if et_race else None
    sa_rho = _rank_corr(sa_race.get("picks", [])) if sa_race else None

    return {
        "race_number": rn,
        "distance": (et_race or sa_race or res_race).get("distance"),
        "race_class": (et_race or sa_race or res_race).get("race_class"),
        "field_size": len(finishers),
        "venue": res_race.get("venue") or res_race.get("race_course"),
        # Pace
        "pace_predicted": (et_race or {}).get("pace"),
        "pace_actual": res_race.get("actual_pace_label"),
        # Market context
        "fav_horse_no": mkt_picks[0] if mkt_picks else None,
        "fav_odds": mkt_rank[0][1] if mkt_rank else None,
        # Top-1
        "et_top1": et_picks[0] if et_picks else None,
        "et_top1_name": name_by.get(et_picks[0]) if et_picks else None,
        "et_top1_odds": odds_by.get(et_picks[0]) if et_picks else None,
        "sa_top1": sa_picks[0] if sa_picks else None,
        "sa_top1_name": name_by.get(sa_picks[0]) if sa_picks else None,
        "sa_top1_odds": odds_by.get(sa_picks[0]) if sa_picks else None,
        "agree_top1": int(agree_top1),
        # Truth
        "winner": actual_winner,
        "winner_name": name_by.get(actual_winner) if actual_winner else None,
        "actual_top3": actual_top3,
        # Model metrics
        "et": et_m,
        "sa": sa_m,
        "mk": mk_m,
        "et_rank_corr": et_rho,
        "sa_rank_corr": sa_rho,
        "union_t3_has_winner": union_has_w,
        # Calibration
        "et_proj_winner": et_proj_winner,
        "et_proj_err": et_proj_err,
        # Detail
        "horses": horse_rows,
    }


# ──────────────────────────────────────────────────────────────────────────
# Statistics helpers
# ──────────────────────────────────────────────────────────────────────────
def _spearman(pairs: list[tuple[float, float]]) -> float | None:
    """Spearman ρ via Pearson on rank-converted pairs. Returns None on n<2."""
    if len(pairs) < 2:
        return None
    xs = [a for a, _ in pairs]
    ys = [b for _, b in pairs]
    return _pearson(_rankify(xs), _rankify(ys))


def _rankify(xs: list[float]) -> list[float]:
    pairs = sorted(enumerate(xs), key=lambda kv: kv[1])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][1] == pairs[i][1]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[pairs[k][0]] = avg_rank
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


# ──────────────────────────────────────────────────────────────────────────
# Aggregation
# ──────────────────────────────────────────────────────────────────────────
def _agg_metric(rows: list[dict], model: str, key: str) -> float | None:
    vs = [r[model][key] for r in rows
          if r.get(model) and r[model].get(key) is not None]
    return mean(vs) if vs else None


def _safe_pct(num: int, denom: int) -> float | None:
    return num / denom if denom else None


def _agg_pace(rows: list[dict]) -> dict:
    fast = {"Fast", "Slightly Fast", "Very Fast"}
    slow = {"Slow", "Slightly Slow", "Very Slow"}
    correct = close = total = 0
    by_label: dict = defaultdict(lambda: {"n": 0, "correct": 0})
    for r in rows:
        pp = r.get("pace_predicted")
        ap = r.get("pace_actual")
        if not pp or not ap:
            continue
        total += 1
        by_label[pp]["n"] += 1
        if pp == ap:
            correct += 1
            by_label[pp]["correct"] += 1
        # same-side fuzzy match
        ps = "F" if pp in fast else ("S" if pp in slow else "N")
        as_ = "F" if ap in fast else ("S" if ap in slow else "N")
        if ps == as_:
            close += 1
    return {
        "total": total,
        "correct": correct,
        "exact_pct": _safe_pct(correct, total),
        "close": close,
        "close_pct": _safe_pct(close, total),
        "wrong": total - close,
        "by_label": dict(by_label),
    }


def _agg_proj_err(rows: list[dict]) -> dict:
    """ET projected-time error stats for actual winners. Reports both raw and
    v4.7-corrected residuals (sprint −0.77, mile −0.71, route 0)."""
    raw = []
    corrected = []
    for r in rows:
        if r.get("et_proj_err") is None:
            continue
        d = r.get("distance") or 0
        bias = 0.77 if d <= 1200 else (0.71 if d <= 1600 else 0.0)
        raw.append(r["et_proj_err"])
        corrected.append(r["et_proj_err"] - bias)

    def _stats(xs: list[float]) -> dict:
        if not xs:
            return {}
        return {
            "n": len(xs),
            "mean": mean(xs),
            "mae": mean(abs(x) for x in xs),
            "rmse": math.sqrt(mean(x * x for x in xs)),
            "median": median(xs),
            "within_05": sum(1 for x in xs if abs(x) <= 0.5) / len(xs),
            "within_10": sum(1 for x in xs if abs(x) <= 1.0) / len(xs),
        }

    return {"raw": _stats(raw), "corrected": _stats(corrected)}


def _agg_odds_buckets(rows: list[dict]) -> list[dict]:
    """ROI by ET top-1 odds bucket (level $1 stakes)."""
    buckets = [(0, 3, "<3.0 (chalk)"),
               (3, 6, "3.0–6.0"),
               (6, 12, "6.0–12.0"),
               (12, 999, ">12 (longshot)")]
    out = []
    for lo, hi, label in buckets:
        sub = [r for r in rows
               if r.get("et_top1_odds") is not None
               and lo < r["et_top1_odds"] <= hi]
        if not sub:
            continue
        n = len(sub)
        wins = sum((r["et"] or {}).get("win") or 0 for r in sub)
        places = sum((r["et"] or {}).get("plc") or 0 for r in sub)
        roi = sum(((r["et_top1_odds"] - 1)
                   if ((r["et"] or {}).get("win")) else -1)
                  for r in sub) / n
        out.append({"label": label, "n": n,
                    "win_rate": wins / n, "place_rate": places / n,
                    "win_roi": roi})
    return out


def _agg_class_distance(rows: list[dict]) -> dict:
    """Win rate by race class and distance bucket for each model."""
    by_class: dict = defaultdict(lambda: {"n": 0,
                                          "et_w": 0, "sa_w": 0, "mk_w": 0,
                                          "et_p": 0, "sa_p": 0, "mk_p": 0})
    for r in rows:
        c = r.get("race_class")
        if c is None:
            continue
        k = f"C{c}"
        by_class[k]["n"] += 1
        for tag in ("et", "sa", "mk"):
            m = r.get(tag) or {}
            by_class[k][f"{tag}_w"] += m.get("win") or 0
            by_class[k][f"{tag}_p"] += m.get("plc") or 0

    by_dist: dict = defaultdict(lambda: {"n": 0,
                                         "et_w": 0, "sa_w": 0, "mk_w": 0,
                                         "et_p": 0, "sa_p": 0, "mk_p": 0})
    for r in rows:
        d = r.get("distance")
        if d is None:
            continue
        k = "Sprint (≤1200)" if d <= 1200 else (
            "Mile (1300–1650)" if d <= 1650 else "Route (>1650)")
        by_dist[k]["n"] += 1
        for tag in ("et", "sa", "mk"):
            m = r.get(tag) or {}
            by_dist[k][f"{tag}_w"] += m.get("win") or 0
            by_dist[k][f"{tag}_p"] += m.get("plc") or 0

    def _normalize(d: dict) -> dict:
        out = {}
        for k, v in d.items():
            n = v["n"]
            if not n:
                continue
            out[k] = {"n": n,
                      "et_win": v["et_w"] / n, "et_plc": v["et_p"] / n,
                      "sa_win": v["sa_w"] / n, "sa_plc": v["sa_p"] / n,
                      "mk_win": v["mk_w"] / n, "mk_plc": v["mk_p"] / n}
        return out

    return {"by_class": _normalize(by_class), "by_distance": _normalize(by_dist)}


def _agg_summary(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n_races": 0}

    agree_n = sum(r["agree_top1"] for r in rows)
    agree_rows = [r for r in rows if r.get("agree_top1")]
    agree_w = sum((r["et"] or {}).get("win") or 0 for r in agree_rows)
    agree_p = sum((r["et"] or {}).get("plc") or 0 for r in agree_rows)

    def _model_summary(tag: str) -> dict:
        valid = [r[tag] for r in rows if r.get(tag) and r[tag].get("win") is not None]
        if not valid:
            return {}
        return {
            "n": len(valid),
            "top1_win": _agg_metric(rows, tag, "win"),
            "top1_plc": _agg_metric(rows, tag, "plc"),
            "top3_has_w": _agg_metric(rows, tag, "top3_has_w"),
            "avg_top3_overlap": _agg_metric(rows, tag, "top3_overlap"),
        }

    et_rho_vals = [r["et_rank_corr"] for r in rows
                   if r.get("et_rank_corr") is not None]
    sa_rho_vals = [r["sa_rank_corr"] for r in rows
                   if r.get("sa_rank_corr") is not None]

    union_w = sum(r["union_t3_has_winner"] for r in rows)

    return {
        "n_races": n,
        "et": _model_summary("et"),
        "sa": _model_summary("sa"),
        "mk": _model_summary("mk"),
        "et_rank_corr_avg": mean(et_rho_vals) if et_rho_vals else None,
        "sa_rank_corr_avg": mean(sa_rho_vals) if sa_rho_vals else None,
        "agree_top1": {
            "n": agree_n, "rate": agree_n / n,
            "win_rate": (agree_w / agree_n) if agree_n else None,
            "place_rate": (agree_p / agree_n) if agree_n else None,
        },
        "union_top3_winner_coverage": union_w / n,
        "pace": _agg_pace(rows),
        "proj_err": _agg_proj_err(rows),
        "et_odds_buckets": _agg_odds_buckets(rows),
        "breakdown": _agg_class_distance(rows),
    }


# ──────────────────────────────────────────────────────────────────────────
# Per-meeting + window builders
# ──────────────────────────────────────────────────────────────────────────
def build_meeting(dc: str) -> dict | None:
    et_data = _load(_et_path(dc)) if _et_path(dc) else None
    sa_data = _load(_sarr_path(dc))
    res_data = _load(_results_path(dc))
    if not res_data or not (et_data or sa_data):
        return None

    et_by = {r["race_number"]: r for r in (et_data or {}).get("races", [])}
    sa_by = {r["race_number"]: r for r in (sa_data or {}).get("races", [])}

    rows = []
    for rr in res_data.get("races", []):
        rn = rr.get("race_number")
        if rn is None:
            continue
        row = _build_race_row(rn, et_by.get(rn), sa_by.get(rn), rr)
        if row:
            rows.append(row)

    if not rows:
        return None

    return {
        "schema_version": "1.0",
        "computed_at": datetime.now().isoformat(timespec="seconds"),
        "date": f"{dc[:4]}-{dc[4:6]}-{dc[6:]}",
        "date_compact": dc,
        "meeting_title": (et_data or sa_data or {}).get("meeting_title", ""),
        "venue": (et_data or sa_data or {}).get("meeting_venue", ""),
        "et_present": et_data is not None,
        "sarr_present": sa_data is not None,
        "n_races": len(rows),
        "summary": _agg_summary(rows),
        "races": rows,
    }


def build_window(dates: list[str], label: str) -> dict | None:
    rows: list[dict] = []
    per_meeting = []
    for dc in dates:
        m = build_meeting(dc)
        if not m:
            continue
        rows.extend(m["races"])
        s = m["summary"]
        per_meeting.append({
            "date": m["date"],
            "date_compact": dc,
            "meeting_title": m.get("meeting_title", ""),
            "n_races": m["n_races"],
            "et_top1_win": (s.get("et") or {}).get("top1_win"),
            "sa_top1_win": (s.get("sa") or {}).get("top1_win"),
            "mk_top1_win": (s.get("mk") or {}).get("top1_win"),
            "agree_rate": (s.get("agree_top1") or {}).get("rate"),
            "agree_win_rate": (s.get("agree_top1") or {}).get("win_rate"),
        })
    if not rows:
        return None
    summary = _agg_summary(rows)
    summary["per_meeting"] = per_meeting
    return {
        "schema_version": "1.0",
        "computed_at": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "n_meetings": len(per_meeting),
        "n_races": len(rows),
        "period": f"{per_meeting[0]['date']} → {per_meeting[-1]['date']}"
                  if per_meeting else "",
        "summary": summary,
    }


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────
def _save(out: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def _filter_window(dates: list[str], window: str) -> tuple[list[str], str]:
    if not dates:
        return [], window
    today = date.today()
    if window == "last7":
        cutoff = today - timedelta(days=7)
    elif window == "last30":
        cutoff = today - timedelta(days=30)
    elif window == "last90":
        cutoff = today - timedelta(days=90)
    elif window == "all":
        return dates, "all"
    elif window.startswith("month-"):
        ym = window[len("month-"):]
        out = [dc for dc in dates if f"{dc[:4]}-{dc[4:6]}" == ym]
        return out, window
    elif window.startswith("season-"):
        # season-YYYY-YYYY (Sep–Aug typical)
        try:
            _, y1, y2 = window.split("-")
            y1, y2 = int(y1), int(y2)
        except Exception:
            return dates, window
        out = []
        for dc in dates:
            yr = int(dc[:4]); mo = int(dc[4:6])
            if (yr == y1 and mo >= 9) or (yr == y2 and mo <= 8):
                out.append(dc)
        return out, window
    else:
        return dates, window
    cutoff_str = cutoff.strftime("%Y%m%d")
    return [dc for dc in dates if dc >= cutoff_str], window


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--date", help="single meeting YYYY-MM-DD")
    g.add_argument("--month", help="aggregate YYYY-MM")
    g.add_argument("--season", help="aggregate YYYY-YYYY")
    g.add_argument("--window", choices=["last7", "last30", "last90", "all"],
                   help="rolling window")
    g.add_argument("--all", action="store_true",
                   help="every per-meeting JSON + every standard window")
    args = ap.parse_args()

    matched = find_matched_dates()
    if not matched:
        print("No matched (predictions + results) dates found.", file=sys.stderr)
        sys.exit(1)

    if args.date:
        dc = args.date.replace("-", "")
        out = build_meeting(dc)
        if not out:
            print(f"No backtestable data for {dc}.", file=sys.stderr)
            sys.exit(1)
        path = REPORTS / f"backtest_unified_{dc}.json"
        _save(out, path)
        print(f"Wrote {path.name}: n_races={out['n_races']}, "
              f"ET top1 win={out['summary'].get('et', {}).get('top1_win')}")
        return

    if args.month:
        dates = [dc for dc in matched if f"{dc[:4]}-{dc[4:6]}" == args.month]
        out = build_window(dates, f"month-{args.month}")
        if not out:
            print(f"No data for month {args.month}.", file=sys.stderr)
            sys.exit(1)
        path = REPORTS / f"backtest_unified_month-{args.month}.json"
        _save(out, path)
        print(f"Wrote {path.name}: n_meetings={out['n_meetings']}")
        return

    if args.season:
        try:
            y1, y2 = (int(x) for x in args.season.split("-"))
        except Exception:
            print(f"Invalid season {args.season!r}", file=sys.stderr)
            sys.exit(2)
        dates = []
        for dc in matched:
            yr = int(dc[:4]); mo = int(dc[4:6])
            if (yr == y1 and mo >= 9) or (yr == y2 and mo <= 8):
                dates.append(dc)
        label = f"season-{y1}-{y2}"
        out = build_window(dates, label)
        if not out:
            print(f"No data for season {args.season}.", file=sys.stderr)
            sys.exit(1)
        path = REPORTS / f"backtest_unified_{label}.json"
        _save(out, path)
        print(f"Wrote {path.name}: n_meetings={out['n_meetings']}")
        return

    if args.window:
        dates, label = _filter_window(matched, args.window)
        out = build_window(dates, label)
        if not out:
            print(f"No data in window {args.window}.", file=sys.stderr)
            sys.exit(1)
        path = REPORTS / f"backtest_unified_{label}.json"
        _save(out, path)
        print(f"Wrote {path.name}: n_meetings={out['n_meetings']}")
        return

    if args.all:
        # Every per-meeting + every standard window.
        for dc in matched:
            m = build_meeting(dc)
            if m:
                _save(m, REPORTS / f"backtest_unified_{dc}.json")
        for w in ["last7", "last30", "last90", "all"]:
            dates, label = _filter_window(matched, w)
            out = build_window(dates, label)
            if out:
                _save(out, REPORTS / f"backtest_unified_{label}.json")
        # Months covered
        months = sorted({f"{dc[:4]}-{dc[4:6]}" for dc in matched})
        for ym in months:
            dates = [dc for dc in matched if f"{dc[:4]}-{dc[4:6]}" == ym]
            out = build_window(dates, f"month-{ym}")
            if out:
                _save(out, REPORTS / f"backtest_unified_month-{ym}.json")
        print(f"Built unified backtest for {len(matched)} meetings + "
              f"{len(months)} months + 4 rolling windows.")
        return

    # Default: one-shot for the latest matched meeting.
    dc = matched[-1]
    out = build_meeting(dc)
    if out:
        _save(out, REPORTS / f"backtest_unified_{dc}.json")
        print(f"(default) wrote backtest_unified_{dc}.json")


if __name__ == "__main__":
    main()
