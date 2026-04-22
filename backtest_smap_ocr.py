"""
backtest_smap_ocr.py — Validate speedmap predictions against actuals.

Inputs (per date YYYYMMDD):
  - reports/race_day_report_{YYYYMMDD}_v4.4.json  → predicted smap col/row
  - reports/results_{YYYYMMDD}.json               → actual running_position
                                                    (authoritative front/back order)
  - running_position_photos/{YYYYMMDD}/R*.json    → OCR lane (row) actuals

Output:
  - cache/smap_residuals.json                     → per-horse + aggregations

Usage:
  python backtest_smap_ocr.py                                # all available
  python backtest_smap_ocr.py --dates 2026-04-15,2026-04-19
  python backtest_smap_ocr.py --from 2026-01-01 --to 2026-04-22

Residuals (positive means predicted MORE FORWARD / MORE WIDE than actual):
  col_res = pred_col − act_col
  row_res = pred_row − act_row

Col band: rank horse at 800M-call from actual running_position. Split field
into n_cols contiguous bands; front band = col=n_cols.
Row (1=Rail, 2=Mid, 3=Wide): OCR avg_y_in_band via lane_utils buckets, 3-way
(Rail; 2-wide→Mid; 3+wide→Wide).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Iterable

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
PHOTOS = BASE / "running_position_photos"
CACHE_OUT = BASE / "cache" / "smap_residuals.json"


def _load_json(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _find_dates_with_all() -> list[str]:
    reports = {p.name[len("race_day_report_"):][:8]
               for p in REPORTS.glob("race_day_report_????????_v4.4.json")}
    results = {p.name[len("results_"):][:8]
               for p in REPORTS.glob("results_????????.json")}
    return sorted(reports & results)


def _positions_to_pos_800m(positions) -> int | None:
    """Extract position at 800M call from positions list or 'x x x x x' string.

    HKJC running position has: [start, (1200M), 800M, 400M, finish].
    Always take entry at index -3 when len ≥ 3 (finish, 400M, 800M).
    """
    if positions is None:
        return None
    if isinstance(positions, str):
        parts = positions.strip().split()
    else:
        parts = [str(p) for p in positions]
    clean = [p for p in parts if p and p not in ("-", "—")]
    if not clean:
        return None
    idx = -3 if len(clean) >= 3 else 0
    try:
        return int(str(clean[idx]).strip())
    except (TypeError, ValueError):
        return None


def _col_band(rank_at_800: int, field_size: int, n_cols: int) -> int:
    if field_size <= 0 or n_cols <= 0 or rank_at_800 is None:
        return 1
    per = max(1, (field_size + n_cols - 1) // n_cols)
    band_from_front = (rank_at_800 - 1) // per
    return max(1, min(n_cols, n_cols - band_from_front))


def _ocr_row(ocr_race, name_upper: str) -> int | None:
    if not ocr_race:
        return None
    for h in ocr_race.get("horses", []):
        if (h.get("horse_name") or "").strip().upper() != name_upper:
            continue
        y = h.get("avg_y_in_band")
        if y is None:
            return None
        try:
            y = float(y)
        except (TypeError, ValueError):
            return None
        if y < 0.42:
            return 1
        if y < 0.84:
            return 2
        return 3
    return None


def _draw_bucket(draw, field_size) -> str:
    try:
        d = int(draw)
    except (TypeError, ValueError):
        return "mid"
    if d <= max(2, field_size * 0.3):
        return "low"
    if d >= field_size - max(1, int(field_size * 0.3)) + 1:
        return "high"
    return "mid"


def _parse_date_arg(s: str) -> str:
    return s.strip().replace("-", "")


def _daterange(start: str, end: str) -> list[str]:
    d0 = datetime.strptime(start, "%Y%m%d").date()
    d1 = datetime.strptime(end, "%Y%m%d").date()
    out, cur = [], d0
    while cur <= d1:
        out.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return out


def backtest(dates: Iterable[str]) -> dict:
    all_rows: list[dict] = []
    per_race: list[dict] = []

    for dc in dates:
        rep = _load_json(REPORTS / f"race_day_report_{dc}_v4.4.json")
        res = _load_json(REPORTS / f"results_{dc}.json")
        if not rep or not res:
            continue

        res_lookup: dict = {}
        for r in res.get("races", []):
            rn = r.get("race_number")
            for run in r.get("runners", []):
                name = (run.get("horse_name") or "").strip().upper()
                if name:
                    res_lookup[(rn, name)] = run

        venue = rep.get("meeting_venue") or res.get("venue", "?")

        for race in rep.get("races", []):
            rn = race.get("race_number")
            if rn is None:
                continue
            smap = race.get("speed_map") or {}
            grid = smap.get("grid") or []
            if not grid:
                continue
            n_cols = int(smap.get("n_cols") or 4)
            field_size = race.get("runners") or len(grid)
            pace = race.get("pace") or "?"
            distance = race.get("distance")

            ocr = _load_json(PHOTOS / dc / f"R{rn}.json")

            pos_at_800: list[tuple[int, str]] = []
            for h in grid:
                name = (h.get("horse_name") or "").strip().upper()
                runner = res_lookup.get((rn, name))
                if not runner:
                    continue
                p800 = _positions_to_pos_800m(runner.get("positions")
                                              or runner.get("running_position"))
                if p800 is None:
                    continue
                pos_at_800.append((p800, name))
            pos_at_800.sort()
            rank_lookup = {name: idx + 1 for idx, (_, name) in enumerate(pos_at_800)}
            actual_fs = len(pos_at_800)

            race_n = 0
            race_hits = {"col_exact": 0, "col_within1": 0,
                         "row_exact": 0, "row_within1": 0, "n_row": 0}
            for h in grid:
                name = (h.get("horse_name") or "").strip().upper()
                if not name:
                    continue
                pred_col = h.get("col")
                pred_row = h.get("row")
                draw = h.get("draw")
                rank = rank_lookup.get(name)
                act_col = _col_band(rank, actual_fs, n_cols) if rank else None
                act_row = _ocr_row(ocr, name) if ocr else None

                col_res = (int(pred_col) - int(act_col)
                           if pred_col is not None and act_col is not None else None)
                row_res = (int(pred_row) - int(act_row)
                           if pred_row is not None and act_row is not None else None)

                all_rows.append({
                    "date": dc, "venue": venue, "race": rn,
                    "pace": pace, "distance": distance,
                    "horse": h.get("horse_name"), "style": h.get("style"),
                    "draw": draw, "draw_bucket": _draw_bucket(draw, field_size),
                    "pred_col": pred_col, "act_col": act_col, "col_res": col_res,
                    "pred_row": pred_row, "act_row": act_row, "row_res": row_res,
                    "advantage": h.get("advantage"),
                })

                if col_res is not None:
                    race_n += 1
                    if col_res == 0:
                        race_hits["col_exact"] += 1
                    if abs(col_res) <= 1:
                        race_hits["col_within1"] += 1
                if row_res is not None:
                    race_hits["n_row"] += 1
                    if row_res == 0:
                        race_hits["row_exact"] += 1
                    if abs(row_res) <= 1:
                        race_hits["row_within1"] += 1

            if race_n:
                per_race.append({
                    "date": dc, "venue": venue, "race": rn,
                    "pace": pace, "n": race_n,
                    "col_exact_pct": round(100 * race_hits["col_exact"] / race_n, 1),
                    "col_within1_pct": round(100 * race_hits["col_within1"] / race_n, 1),
                    "n_row": race_hits["n_row"],
                    "row_exact_pct": (round(100 * race_hits["row_exact"] / race_hits["n_row"], 1)
                                      if race_hits["n_row"] else None),
                    "row_within1_pct": (round(100 * race_hits["row_within1"] / race_hits["n_row"], 1)
                                        if race_hits["n_row"] else None),
                })

    def _agg(rows: list[dict], keys: list[str]) -> list[dict]:
        buckets: dict[tuple, list[dict]] = {}
        for r in rows:
            k = tuple(r.get(x) for x in keys)
            buckets.setdefault(k, []).append(r)
        out = []
        for k, vs in sorted(buckets.items(), key=lambda kv: str(kv[0])):
            col = [v["col_res"] for v in vs if v.get("col_res") is not None]
            row = [v["row_res"] for v in vs if v.get("row_res") is not None]
            out.append({
                **{x: kv for x, kv in zip(keys, k)},
                "n": len(vs),
                "col_bias_mean": round(mean(col), 2) if col else None,
                "col_bias_median": round(median(col), 2) if col else None,
                "col_exact_pct": (round(100 * sum(1 for r in col if r == 0) / len(col), 1)
                                  if col else None),
                "col_within1_pct": (round(100 * sum(1 for r in col if abs(r) <= 1) / len(col), 1)
                                    if col else None),
                "row_bias_mean": round(mean(row), 2) if row else None,
                "row_exact_pct": (round(100 * sum(1 for r in row if r == 0) / len(row), 1)
                                  if row else None),
                "row_within1_pct": (round(100 * sum(1 for r in row if abs(r) <= 1) / len(row), 1)
                                    if row else None),
            })
        return out

    overall = _agg(all_rows, []) if all_rows else []
    return {
        "summary": {
            "n_rows": len(all_rows),
            "n_races": len(per_race),
            "overall": overall[0] if overall else None,
            "by_pace": _agg(all_rows, ["pace"]),
            "by_pace_style": _agg(all_rows, ["pace", "style"]),
            "by_pace_draw": _agg(all_rows, ["pace", "draw_bucket"]),
            "by_venue": _agg(all_rows, ["venue"]),
            "by_venue_pace": _agg(all_rows, ["venue", "pace"]),
            "per_race": per_race,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
        "rows": all_rows,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", help="comma-separated YYYY-MM-DD list")
    ap.add_argument("--from", dest="d_from")
    ap.add_argument("--to", dest="d_to")
    args = ap.parse_args()

    available = set(_find_dates_with_all())
    if args.dates:
        dates = [_parse_date_arg(s) for s in args.dates.split(",") if s.strip()]
    elif args.d_from and args.d_to:
        dates = _daterange(_parse_date_arg(args.d_from), _parse_date_arg(args.d_to))
    else:
        dates = sorted(available)

    dates = [d for d in dates if d in available]
    if not dates:
        print("No dates with both v4.4 reports AND results_*.json.")
        return

    print(f"Backtesting smap across {len(dates)} date(s)...")
    result = backtest(dates)
    CACHE_OUT.parent.mkdir(exist_ok=True)
    CACHE_OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                         encoding="utf-8")

    s = result["summary"]
    print(f"  rows: {s['n_rows']}   races: {s['n_races']}")
    if s["overall"]:
        o = s["overall"]
        print(f"  overall col_bias={o['col_bias_mean']}  "
              f"col_exact={o['col_exact_pct']}%  col_±1={o['col_within1_pct']}%  "
              f"row_exact={o['row_exact_pct']}%  row_±1={o['row_within1_pct']}%")
    print("\n  By pace:")
    for row in s["by_pace"]:
        print(f"    {str(row['pace']):>14}  n={row['n']:>4}  "
              f"col_bias={row['col_bias_mean']}  col_±1={row['col_within1_pct']}%  "
              f"row_±1={row['row_within1_pct']}%")
    print(f"\nSaved → {CACHE_OUT}")


if __name__ == "__main__":
    main()
