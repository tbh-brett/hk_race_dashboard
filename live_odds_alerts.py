"""Live-odds alert engine.

Standalone CLI that:
  1. Loads all snapshots in ``cache/live_odds/{YYYYMMDD}`` for a venue.
  2. Loads the latest race-day model report (ET or SARR) for the same date.
  3. Cross-references model top-3 picks vs market drift (Win-odds Δ% from
     earliest to latest snapshot) and emits alerts:

         WARN   — top-3 model pick drifting ≥+30%
         REVIEW — outsider (rank > 3) steaming ≤-30%
                  or top-1 model pick drifting ≥+20%
         INFO   — top-3 model pick steaming ≤-30%

  4. Writes a JSON file at ``reports/live_odds_alerts/{YYYYMMDD}_{VENUE}.json``
     and prints a coloured summary to stdout.

Designed to be invoked from Windows Task Scheduler 4× per meeting day
(see ``schedule_live_odds.ps1``). Exits 0 on any outcome (including no
alerts), 1 on hard error (no snapshots, missing report).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
SNAPDIR = BASE / "cache" / "live_odds"
REPDIR = BASE / "reports"
OUTDIR = REPDIR / "live_odds_alerts"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_snapshots(date_compact: str, venue: str) -> list[dict]:
    d = SNAPDIR / date_compact
    if not d.exists():
        return []
    out = []
    for fp in sorted(d.glob(f"{venue}_R*.json")):
        try:
            out.append(json.loads(fp.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return out


def load_picks(date_compact: str, prefer: str = "auto") -> dict[int, list[dict]]:
    """Return {race_no: [picks]} from the latest race-day report.

    ``prefer`` may be "et" / "sarr" / "auto" (SARR if exists else ET).
    Each pick dict carries at minimum ``horse_no`` and ``horse_name``.
    """
    cands_sarr = sorted(REPDIR.glob(f"race_day_report_{date_compact}_SARR.json"))
    cands_et = sorted(REPDIR.glob(f"race_day_report_{date_compact}_v*.json"))
    cands_et = [p for p in cands_et if "_SARR" not in p.name]
    if prefer == "sarr":
        cands = cands_sarr or cands_et
    elif prefer == "et":
        cands = cands_et or cands_sarr
    else:
        cands = cands_sarr or cands_et
    if not cands:
        return {}
    try:
        data = json.loads(cands[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[int, list[dict]] = {}
    for r in data.get("races") or []:
        rn = r.get("race_number")
        if rn:
            out[int(rn)] = r.get("picks") or []
    return out


# ---------------------------------------------------------------------------
# Drift + alignment
# ---------------------------------------------------------------------------

def compute_drift(snaps: list[dict]) -> dict[int, list[dict]]:
    """Group snapshots by race and compute per-horse Win-odds Δ%."""
    by_race: dict[int, list[dict]] = defaultdict(list)
    for s in snaps:
        try:
            by_race[int(s["race_no"])].append(s)
        except (KeyError, TypeError, ValueError):
            pass
    out: dict[int, list[dict]] = {}
    for rn, rows in by_race.items():
        rows.sort(key=lambda s: s.get("scraped_at", ""))
        if len(rows) < 2:
            continue
        first_by = {str(h.get("no")): h for h in rows[0].get("odds") or []}
        latest = rows[-1]
        horses = []
        for h in latest.get("odds") or []:
            try:
                no_n = int(h.get("no"))
            except (TypeError, ValueError):
                continue
            try:
                wf = float(first_by.get(str(no_n), {}).get("win"))
            except (TypeError, ValueError):
                wf = None
            try:
                wl = float(h.get("win"))
            except (TypeError, ValueError):
                wl = None
            d = None
            if wf and wl and wf > 0:
                d = round((wl - wf) / wf * 100, 1)
            horses.append({
                "no": no_n, "horse": str(h.get("horse") or ""),
                "win_first": wf, "win_last": wl, "dpct": d,
                "n_snaps": len(rows),
                "first_ts": rows[0].get("scraped_at", ""),
                "last_ts": latest.get("scraped_at", ""),
            })
        out[rn] = horses
    return out


def build_alerts(picks_by_race: dict[int, list[dict]],
                 drift_by_race: dict[int, list[dict]],
                 steamer_thr: float = -20.0,
                 drifter_thr: float = 20.0,
                 big_steamer_thr: float = -30.0,
                 big_drifter_thr: float = 30.0,
                 top_n: int = 3) -> list[dict]:
    alerts: list[dict] = []
    for rn, horses in drift_by_race.items():
        picks = picks_by_race.get(rn) or []
        top_nos: list[int] = []
        rank = {}
        for i, p in enumerate(picks[:top_n]):
            try:
                n = int(p.get("horse_no") or 0)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                top_nos.append(n)
                rank[n] = i + 1
        for h in horses:
            d = h.get("dpct")
            if d is None:
                continue
            no = h["no"]
            name = h["horse"]
            if no in top_nos:
                rk = rank[no]
                if d >= big_drifter_thr:
                    alerts.append({
                        "race": rn, "severity": "WARN",
                        "kind": "TOP_PICK_DRIFT",
                        "no": no, "horse": name, "rank": rk, "dpct": d,
                        "msg": (f"R{rn}: model #{rk} {name} (#{no}) "
                                f"drifting {d:+.1f}%"),
                    })
                elif d >= drifter_thr and rk == 1:
                    alerts.append({
                        "race": rn, "severity": "REVIEW",
                        "kind": "TOP_PICK_DRIFT",
                        "no": no, "horse": name, "rank": rk, "dpct": d,
                        "msg": (f"R{rn}: model #{rk} {name} (#{no}) "
                                f"drifting {d:+.1f}%"),
                    })
                elif d <= big_steamer_thr:
                    alerts.append({
                        "race": rn, "severity": "INFO",
                        "kind": "TOP_PICK_STEAM",
                        "no": no, "horse": name, "rank": rk, "dpct": d,
                        "msg": (f"R{rn}: model #{rk} {name} (#{no}) "
                                f"steaming {d:+.1f}% (market endorses)"),
                    })
            else:
                if d <= big_steamer_thr:
                    alerts.append({
                        "race": rn, "severity": "REVIEW",
                        "kind": "OUTSIDER_STEAM",
                        "no": no, "horse": name, "rank": None, "dpct": d,
                        "msg": (f"R{rn}: outsider {name} (#{no}) "
                                f"steaming {d:+.1f}% (consider QPL cover)"),
                    })
                elif d <= steamer_thr:
                    alerts.append({
                        "race": rn, "severity": "INFO",
                        "kind": "OUTSIDER_STEAM",
                        "no": no, "horse": name, "rank": None, "dpct": d,
                        "msg": (f"R{rn}: outsider {name} (#{no}) "
                                f"steaming {d:+.1f}%"),
                    })
    sev_order = {"WARN": 0, "REVIEW": 1, "INFO": 2}
    alerts.sort(key=lambda a: (sev_order.get(a["severity"], 9),
                               a["race"], -abs(a.get("dpct") or 0)))
    return alerts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

ANSI = {
    "WARN": "\x1b[1;31m",
    "REVIEW": "\x1b[1;33m",
    "INFO": "\x1b[0;36m",
    "RESET": "\x1b[0m",
}


def format_console(alerts: list[dict], use_colour: bool = True) -> str:
    if not alerts:
        return "(no alerts)"
    lines = []
    for a in alerts:
        sev = a["severity"]
        if use_colour:
            lines.append(f"{ANSI.get(sev, '')}[{sev:6}]{ANSI['RESET']} "
                         f"{a['msg']}")
        else:
            lines.append(f"[{sev:6}] {a['msg']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--date", required=True,
                   help="Meeting date YYYY-MM-DD or YYYYMMDD")
    p.add_argument("--venue", required=True, choices=["HV", "ST"])
    p.add_argument("--model", default="auto", choices=["auto", "et", "sarr"])
    p.add_argument("--steamer-thr", type=float, default=-20.0)
    p.add_argument("--drifter-thr", type=float, default=20.0)
    p.add_argument("--big-steamer-thr", type=float, default=-30.0)
    p.add_argument("--big-drifter-thr", type=float, default=30.0)
    p.add_argument("--top-n", type=int, default=3)
    p.add_argument("--no-colour", action="store_true")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    date_compact = args.date.replace("-", "")
    if len(date_compact) != 8:
        print(f"Invalid date: {args.date}", file=sys.stderr)
        return 1

    snaps = load_snapshots(date_compact, args.venue)
    if not snaps:
        print(f"No snapshots in cache/live_odds/{date_compact} for {args.venue}",
              file=sys.stderr)
        return 1

    picks = load_picks(date_compact, prefer=args.model)
    if not picks and not args.quiet:
        print(f"WARN: no race-day report found for {date_compact}; "
              "alerts will not include pick alignment.", file=sys.stderr)

    drift = compute_drift(snaps)
    alerts = build_alerts(
        picks, drift,
        steamer_thr=args.steamer_thr, drifter_thr=args.drifter_thr,
        big_steamer_thr=args.big_steamer_thr,
        big_drifter_thr=args.big_drifter_thr,
        top_n=args.top_n,
    )

    OUTDIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTDIR / f"{date_compact}_{args.venue}.json"
    payload = {
        "date": date_compact, "venue": args.venue, "model": args.model,
        "thresholds": {
            "steamer_thr": args.steamer_thr,
            "drifter_thr": args.drifter_thr,
            "big_steamer_thr": args.big_steamer_thr,
            "big_drifter_thr": args.big_drifter_thr,
            "top_n": args.top_n,
        },
        "n_alerts": len(alerts),
        "alerts": alerts,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")

    if not args.quiet:
        n_w = sum(1 for a in alerts if a["severity"] == "WARN")
        n_r = sum(1 for a in alerts if a["severity"] == "REVIEW")
        n_i = sum(1 for a in alerts if a["severity"] == "INFO")
        print(f"\n{date_compact} · {args.venue} · "
              f"WARN={n_w} REVIEW={n_r} INFO={n_i}")
        print(format_console(alerts, use_colour=not args.no_colour))
        print(f"\nSaved → {out_path.relative_to(BASE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
