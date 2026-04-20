"""Apr 22 2026 HV — v4.5 pace projection walkthrough.

Produces a verbose, per-race breakdown:
  • who leads (and who else is forward)
  • pressure index
  • predicted early-sectional deviation
  • pace label (7-band)
  • beneficiaries / penalised

Uses pace_utils.predict_early_sectional_dev (v4.5).
Reads racecard from cache/racecard_YYYY-MM-DD.json and dominant running
styles from hkjc_results_updated.xlsx (past ~20 runs per horse).
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from collections import Counter

import pandas as pd

from pace_utils import (
    predict_early_sectional_dev,
    normalise_going,
    classify_pace,
)

BASE = Path(__file__).parent
DATE = "2026-04-22"
RACECARD = BASE / "cache" / f"racecard_{DATE}.json"
DB_FILE = BASE / "hkjc_results_updated.xlsx"


def _classify_first_call(pos: int, field_size: int) -> str:
    if pos is None or field_size <= 0:
        return "Unknown"
    if pos <= 2:
        return "Leader"
    if pos <= max(4, int(field_size * 0.3)):
        return "On-Pace"
    if pos >= max(8, int(field_size * 0.7)):
        return "Closer"
    return "Midfield"


def compute_dominant_style(horse_name: str, db: pd.DataFrame,
                            n_recent: int = 8) -> dict:
    """Look at the most recent n runs for the horse. Return the modal style
    + leader_frac + n_runs used."""
    runs = db[db["horse_name"].str.upper() == horse_name.upper()].copy()
    if runs.empty:
        return {"style": "Unknown", "n": 0, "leader_frac": 0.0, "front_frac": 0.0}
    runs = runs.sort_values("race_date", ascending=False).head(n_recent)

    styles = []
    for _, r in runs.iterrows():
        rp = str(r.get("running_positions", "")).strip()
        if not rp or rp.lower() == "nan":
            continue
        toks = [t for t in rp.split() if t.isdigit()]
        if not toks:
            continue
        first = int(toks[0])
        # Field size: infer from same-race rows (fallback 12)
        fs = int(db[(db["race_date"] == r["race_date"]) &
                    (db["race_number"] == r["race_number"])].shape[0]) or 12
        styles.append(_classify_first_call(first, fs))

    if not styles:
        return {"style": "Unknown", "n": 0, "leader_frac": 0.0, "front_frac": 0.0}
    c = Counter(styles)
    mode_style = c.most_common(1)[0][0]
    n = len(styles)
    leader_frac = c["Leader"] / n
    front_frac = (c["Leader"] + c["On-Pace"]) / n
    return {"style": mode_style, "n": n,
            "leader_frac": round(leader_frac, 2),
            "front_frac": round(front_frac, 2)}


def beneficiaries_from_pace(label: str, horses: list) -> dict:
    """Return {winners:[name,...], losers:[name,...]} given pace label + horse
    style list.

    Rules (display-only; not applied to model scoring yet):
      Fast / Very Fast pace → Closers benefit; Leaders suffer.
      Slow / Very Slow pace → Leaders/On-Pace benefit; Closers suffer.
      Normal / Slightly* → neutral.
    """
    winners, losers = [], []
    if label in ("Fast", "Very Fast", "Slightly Fast"):
        for h in horses:
            s = h["style"]
            if s in ("Closer", "Midfield"):
                winners.append(h["name"])
            elif s == "Leader":
                losers.append(h["name"])
    elif label in ("Slow", "Very Slow", "Slightly Slow"):
        for h in horses:
            s = h["style"]
            if s in ("Leader", "On-Pace"):
                winners.append(h["name"])
            elif s == "Closer":
                losers.append(h["name"])
    return {"winners": winners, "losers": losers}


def main():
    if not RACECARD.exists():
        print(f"!! Racecard missing: {RACECARD}")
        print(f"   Run: python scrape_hkjc_racecard.py --date {DATE}")
        sys.exit(1)
    if not DB_FILE.exists():
        print(f"!! DB missing: {DB_FILE}")
        sys.exit(1)

    print(f"Loading DB...", flush=True)
    db = pd.read_excel(DB_FILE)
    print(f"  {len(db):,} rows, {db['horse_name'].nunique()} unique horses\n")

    rc = json.loads(RACECARD.read_text(encoding="utf-8"))

    # Group by race_number. Schema: {race_date, racecourse, races: [{meta, horses: [...]}]}
    by_race: dict = {}
    for race_block in rc.get("races", []):
        for h in race_block.get("horses", []):
            by_race.setdefault(h["race_number"], []).append(h)

    if not by_race:
        print("!! Could not parse racecard structure. Top-level keys:", list(rc)[:10])
        sys.exit(1)

    print(f"═════════════════════════════════════════════════════════════")
    print(f"  {DATE}  Happy Valley  —  v4.5 pace projection")
    print(f"═════════════════════════════════════════════════════════════\n")

    for rn in sorted(by_race):
        entries = by_race[rn]
        # Filter out standby/reserves
        runners = [e for e in entries
                   if e.get("jockey") and not e.get("is_standby")]
        if not runners:
            continue

        first = runners[0]
        distance = int(first.get("distance") or 0)
        race_class = first.get("race_class") or ""
        going_raw = first.get("going") or "G"
        going = normalise_going(going_raw)
        race_name = first.get("race_name") or ""

        print(f"┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"┃ R{rn}  {race_name}")
        print(f"┃ {distance}m  Class {race_class}  Going {going}  ({len(runners)} runners)")
        print(f"┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        # Resolve style for each horse
        horses = []
        for r in runners:
            name = r.get("horse_name") or "?"
            prof = compute_dominant_style(name, db, n_recent=8)
            horses.append({
                "name": name, "no": r.get("horse_no"),
                "draw": r.get("draw"), "style": prof["style"],
                "n": prof["n"], "leader_frac": prof["leader_frac"],
                "front_frac": prof["front_frac"],
            })

        # Summarise styles
        style_counts = Counter(h["style"] for h in horses)
        leaders = [h for h in horses if h["style"] == "Leader"]
        on_pace = [h for h in horses if h["style"] == "On-Pace"]
        mids    = [h for h in horses if h["style"] == "Midfield"]
        closers = [h for h in horses if h["style"] == "Closer"]
        unk     = [h for h in horses if h["style"] == "Unknown"]

        print(f"\n  Field-style profile: "
              + "  ".join(f"{k}={v}" for k, v in
                          [("L", style_counts.get("Leader", 0)),
                           ("OP", style_counts.get("On-Pace", 0)),
                           ("Mid", style_counts.get("Midfield", 0)),
                           ("Cl", style_counts.get("Closer", 0)),
                           ("?", style_counts.get("Unknown", 0))]))

        if leaders:
            print(f"  Confirmed leader(s) (first-call ≤2 in recent form):")
            for h in sorted(leaders, key=lambda x: -x["leader_frac"]):
                print(f"    #{h['no']:>2} draw {h['draw']:>2}  {h['name']:<22} "
                      f"leader_frac={h['leader_frac']:.0%} over last {h['n']} runs")
        else:
            print(f"  No committed front-runners — race may walk / closer-friendly tempo")

        if on_pace:
            names_op = ", ".join(f"{h['name']}(f={h['front_frac']:.0%})" for h in on_pace[:6])
            print(f"  On-pace backers: {names_op}")

        # Run v4.5 predictor
        label, pred_dev, reasons, leader_names = predict_early_sectional_dev(
            [{"dominant_style": h["style"]} for h in horses],
            distance=distance,
            going=going,
            venue="HV",
            race_class=race_class,
            is_awt=False,
            field_size=len(horses),
        )

        print(f"\n  ── v4.5 calculation ──")
        for r in reasons:
            print(f"    • {r}")
        print(f"  → Predicted early-sectional dev: {pred_dev:+.2f}s")
        print(f"  → PACE LABEL:  {label}")

        # Beneficiaries / penalised
        outcome = beneficiaries_from_pace(label, horses)
        if outcome["winners"]:
            print(f"\n  Pace BENEFICIARIES (suits {label.lower()} tempo):")
            for n in outcome["winners"]:
                h = next(x for x in horses if x["name"] == n)
                print(f"    + {n} ({h['style']})")
        else:
            print(f"\n  No clear pace beneficiaries for a {label} tempo")
        if outcome["losers"]:
            print(f"  Pace PENALISED (disadvantaged by {label.lower()} tempo):")
            for n in outcome["losers"]:
                h = next(x for x in horses if x["name"] == n)
                print(f"    − {n} ({h['style']})")

        if unk:
            print(f"  (note: {len(unk)} runner(s) with no historical style data — "
                  f"treated as Midfield)")

        print()


if __name__ == "__main__":
    main()
