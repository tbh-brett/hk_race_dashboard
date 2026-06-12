"""
build_mutual_picks.py
=====================

After ET (race_day_report_{DC}_v4.4.json) and SARR
(race_day_report_{DC}_SARR.json) have both been written, emit a
companion `reports/mutual_{DC}.json` capturing the per-race intersection
of their top-N picks.

Schema
------
    {
      "date":          "20260513",
      "schema_version": 1,
      "top_n":         3,
      "races": [
        { "race_number":   1,
          "et_picks":     ["#3 ALPHA HORSE", ...],     # top-N strings
          "sarr_picks":   ["#3 ALPHA HORSE", ...],
          "mutual_horses": [
              {"horse_no": 3, "horse_name": "ALPHA HORSE",
               "et_rank": 1, "sarr_rank": 2}
          ],
          "mutual_size":   1
        },
        ...
      ]
    }

The dashboard previously computed this on-the-fly in `t_today`; the
persisted file is now the canonical source. The race-day PDF can also
read this to surface a "🤝 Mutual picks" line.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
SCHEMA_VERSION = 2


def _load(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _et_report_path(dc: str) -> Path | None:
    for tag in ("v4.4", "v3.4.8"):
        p = REPORTS / f"race_day_report_{dc}_{tag}.json"
        if p.exists():
            return p
    return None


def _picks_by_rank(race: dict, top_n: int) -> list[dict]:
    """Return top-N picks (rank-sorted) with horse_no, horse_name, rank."""
    picks = race.get("picks") or []
    # picks are typically already sorted by rank; defensive sort:
    def _rk(p):
        try:
            return int(p.get("rank", 99))
        except (TypeError, ValueError):
            return 99
    sorted_picks = sorted(picks, key=_rk)[:top_n]
    out: list[dict] = []
    for p in sorted_picks:
        try:
            hn = int(p.get("horse_no")) if p.get("horse_no") is not None else None
        except (TypeError, ValueError):
            hn = None
        out.append({
            "horse_no":   hn,
            "horse_name": (p.get("horse_name") or "").strip().upper(),
            "rank":       p.get("rank"),
        })
    return out


def build_mutual_for_date(date_compact: str, top_n: int = 3,
                          out_path: Path | None = None) -> dict | None:
    """Build mutual-picks file for one meeting. Returns the dict written
    (or None if either report is missing)."""
    et_path = _et_report_path(date_compact)
    sarr_path = REPORTS / f"race_day_report_{date_compact}_SARR.json"
    fuse_path = REPORTS / f"race_day_report_{date_compact}_FUSE.json"
    et = _load(et_path) if et_path else None
    sarr = _load(sarr_path)
    fuse = _load(fuse_path)
    if not et or not sarr:
        return None

    et_by_rn = {int(r["race_number"]): r for r in et.get("races", []) if "race_number" in r}
    sarr_by_rn = {int(r["race_number"]): r for r in sarr.get("races", []) if "race_number" in r}
    fuse_by_rn = {int(r["race_number"]): r for r in (fuse or {}).get("races", [])
                  if "race_number" in r}

    races_out: list[dict] = []
    for rn in sorted(et_by_rn):
        et_race = et_by_rn[rn]
        sarr_race = sarr_by_rn.get(rn)
        if not sarr_race:
            continue
        et_top = _picks_by_rank(et_race, top_n)
        sarr_top = _picks_by_rank(sarr_race, top_n)
        fuse_race = fuse_by_rn.get(rn)
        fuse_top = _picks_by_rank(fuse_race, top_n) if fuse_race else []
        et_keys = {p["horse_no"] for p in et_top if p["horse_no"] is not None}
        sarr_keys = {p["horse_no"] for p in sarr_top if p["horse_no"] is not None}
        fuse_keys = {p["horse_no"] for p in fuse_top if p["horse_no"] is not None}
        mutual_keys = et_keys & sarr_keys

        et_rank_lookup = {p["horse_no"]: p["rank"] for p in et_top}
        sarr_rank_lookup = {p["horse_no"]: p["rank"] for p in sarr_top}
        fuse_rank_lookup = {p["horse_no"]: p["rank"] for p in fuse_top}
        name_lookup = {p["horse_no"]: p["horse_name"] for p in et_top}
        for p in fuse_top:
            name_lookup.setdefault(p["horse_no"], p["horse_name"])

        mutual_horses = [
            {
                "horse_no":   hn,
                "horse_name": name_lookup.get(hn, ""),
                "et_rank":    et_rank_lookup.get(hn),
                "sarr_rank":  sarr_rank_lookup.get(hn),
                "fuse_rank":  fuse_rank_lookup.get(hn),
            }
            for hn in sorted(mutual_keys)
        ]
        triple_keys = mutual_keys & fuse_keys if fuse_keys else set()
        triple_horses = [
            {
                "horse_no":   hn,
                "horse_name": name_lookup.get(hn, ""),
                "et_rank":    et_rank_lookup.get(hn),
                "sarr_rank":  sarr_rank_lookup.get(hn),
                "fuse_rank":  fuse_rank_lookup.get(hn),
            }
            for hn in sorted(triple_keys)
        ]

        races_out.append({
            "race_number":   rn,
            "et_picks":      [f"#{p['horse_no']} {p['horse_name']}" for p in et_top],
            "sarr_picks":    [f"#{p['horse_no']} {p['horse_name']}" for p in sarr_top],
            "fuse_picks":    [f"#{p['horse_no']} {p['horse_name']}" for p in fuse_top],
            "mutual_horses": mutual_horses,
            "mutual_size":   len(mutual_horses),
            "triple_horses": triple_horses,
            "triple_size":   len(triple_horses),
        })

    payload = {
        "date":            date_compact,
        "schema_version":  SCHEMA_VERSION,
        "top_n":           top_n,
        "has_fuse":        bool(fuse),
        "n_races":         len(races_out),
        "n_mutual_total":  sum(r["mutual_size"] for r in races_out),
        "n_triple_total":  sum(r["triple_size"] for r in races_out),
        "races":           races_out,
    }

    if out_path is None:
        out_path = REPORTS / f"mutual_{date_compact}.json"
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    return payload


def build_all() -> int:
    """Backfill mutual files for every meeting that has both ET + SARR."""
    dates = set()
    for tag in ("v4.4", "v3.4.8"):
        for p in REPORTS.glob(f"race_day_report_????????_{tag}.json"):
            dates.add(p.name[len("race_day_report_"):][:8])
    have_sarr = {
        p.name[len("race_day_report_"):][:8]
        for p in REPORTS.glob("race_day_report_????????_SARR.json")
    }
    common = sorted(dates & have_sarr)
    n_ok = 0
    for dc in common:
        result = build_mutual_for_date(dc)
        if result:
            n_ok += 1
            print(f"  ✓ {dc}  ({result['n_races']} races, "
                  f"{result['n_mutual_total']} mutual picks)")
    return n_ok


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYYMMDD compact date")
    ap.add_argument("--all", action="store_true", help="Backfill every meeting")
    ap.add_argument("--top-n", type=int, default=3)
    args = ap.parse_args()

    if args.all:
        n = build_all()
        print(f"\nbuilt {n} mutual files")
        sys.exit(0)
    if not args.date:
        ap.error("Pass --date YYYYMMDD or --all")
    out = build_mutual_for_date(args.date, top_n=args.top_n)
    if out:
        print(f"✓ Wrote reports/mutual_{args.date}.json "
              f"({out['n_races']} races, {out['n_mutual_total']} mutual picks)")
    else:
        print(f"✗ Missing ET or SARR report for {args.date}")
        sys.exit(1)
