"""fixture_targeting.py — Anticipate which horses are aimed at which fixtures.

For every horse currently "in the string" (a run in the recent window), we:
  1. Derive its preferred conditions (venue + surface + distance) from the
     course/distance where it places best.
  2. Read its rating-cycle state (horse_cycle.compute_profile_cycle) — a horse
     whose rating has eased back toward its winning band is "primed" and the
     stable is more likely to place it for a target.
  3. Match it to the soonest upcoming fixture at its preferred venue where the
     gap since its last run lands in a sensible prep window.

The result is a per-fixture anticipation board that the dashboard renders as a
calendar. It is an *inference*, not an official declaration — entries are only
known when HKJC publishes them ~3 days out.

Fixture source priority:
  1. data/fixtures.json  (authoritative — list of {date, venue, surface, note})
  2. Provisional weekly cadence (Wed → Happy Valley, Sun → Sha Tin) — clearly
     flagged provisional=True so the user knows to confirm against HKJC.

Output: reports/fixture_targets.json
CLI:    python fixture_targeting.py            # next 6 weeks
        python fixture_targeting.py --weeks 8
"""
from __future__ import annotations
import argparse
import json
import sqlite3
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

# ── Tunables ─────────────────────────────────────────────────────────────
ACTIVE_WINDOW_DAYS = 70      # a horse counts as "in the string" if it ran within this
MIN_RUNS_FOR_PREF = 3        # need this many runs to trust a preferred profile
PREP_MIN_GAP = 10            # plausible days between runs (lower bound)
PREP_MAX_GAP = 70            # plausible days between runs (upper bound)
PREP_IDEAL_LO = 14          # ideal turnaround window (lower)
PREP_IDEAL_HI = 42          # ideal turnaround window (upper)
DEFAULT_WEEKS = 6

VENUE_NAMES = {"ST": "Sha Tin", "HV": "Happy Valley"}


@dataclass
class Fixture:
    date: str           # ISO YYYY-MM-DD
    venue: str          # ST / HV
    surface: str        # Turf / AWT / Mixed
    provisional: bool
    note: str = ""


@dataclass
class HorseTarget:
    horse_id: str
    horse_name: str
    trainer: str
    target_date: str
    target_venue: str
    pref_venue: str
    pref_surface: str
    pref_distance: int
    pref_place_rate: float
    pref_n: int
    last_run_date: str
    days_since_last: int
    gap_to_target: int
    current_rating: Optional[float]
    cycle_state: str
    cycle_flag: str
    score: float
    reason: str


# ── Fixtures ─────────────────────────────────────────────────────────────
def _load_fixture_file(path: Path = Path("data/fixtures.json")) -> list[Fixture]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[Fixture] = []
    for r in raw:
        try:
            out.append(Fixture(
                date=str(r["date"])[:10],
                venue=str(r["venue"]).upper().strip(),
                surface=str(r.get("surface", "Turf")),
                provisional=bool(r.get("provisional", False)),
                note=str(r.get("note", "")),
            ))
        except Exception:
            continue
    return out


def _provisional_fixtures(start: date, weeks: int) -> list[Fixture]:
    """Generate provisional fixtures on the standard HKJC weekly cadence.

    Wednesday → Happy Valley (Turf, twilight), Sunday → Sha Tin (Turf).
    Clearly flagged provisional so they are never mistaken for declarations.
    """
    out: list[Fixture] = []
    end = start + timedelta(weeks=weeks)
    d = start
    while d <= end:
        if d.weekday() == 2:   # Wednesday
            out.append(Fixture(d.isoformat(), "HV", "Turf", True,
                               "Provisional (typical Wed HV)"))
        elif d.weekday() == 6:  # Sunday
            out.append(Fixture(d.isoformat(), "ST", "Turf", True,
                               "Provisional (typical Sun ST)"))
        d += timedelta(days=1)
    return out


def upcoming_fixtures(after_iso: str, weeks: int = DEFAULT_WEEKS,
                      fixture_file: Path = Path("data/fixtures.json")) -> list[Fixture]:
    """Return fixtures strictly after `after_iso` for the next `weeks` weeks.

    Authoritative file entries take priority; provisional cadence fills any
    dates the file does not cover.
    """
    after = datetime.strptime(after_iso[:10], "%Y-%m-%d").date()
    end = after + timedelta(weeks=weeks)
    declared = [f for f in _load_fixture_file(fixture_file)
                if after < datetime.strptime(f.date, "%Y-%m-%d").date() <= end]
    declared_dates = {f.date for f in declared}
    prov = [f for f in _provisional_fixtures(after + timedelta(days=1), weeks)
            if f.date not in declared_dates]
    fixtures = declared + prov
    fixtures.sort(key=lambda f: (f.date, f.venue))
    return fixtures


# ── Horse profiling ──────────────────────────────────────────────────────
def _latest_date(con: sqlite3.Connection) -> str:
    return con.execute("SELECT MAX(race_date) FROM results").fetchone()[0]


def active_horses(con: sqlite3.Connection, latest_iso: str,
                  window_days: int = ACTIVE_WINDOW_DAYS) -> pd.DataFrame:
    """Horses with at least one run within `window_days` of the latest date."""
    cutoff = (datetime.strptime(latest_iso[:10], "%Y-%m-%d").date()
              - timedelta(days=window_days)).isoformat()
    q = """
    SELECT horse_id,
           MAX(race_date) AS last_run_date
    FROM results
    WHERE race_date >= ?
    GROUP BY horse_id
    """
    df = pd.read_sql_query(q, con, params=(cutoff,))
    return df


def _horse_meta(con: sqlite3.Connection, horse_id: str) -> tuple[str, str, Optional[float]]:
    row = con.execute(
        "SELECT horse_name, trainer, rating FROM results "
        "WHERE horse_id=? ORDER BY race_date DESC, race_number DESC LIMIT 1",
        (horse_id,)
    ).fetchone()
    if not row:
        return horse_id, "", None
    name, trainer, rating = row
    try:
        rating = float(rating)
    except (TypeError, ValueError):
        rating = None
    return name or horse_id, trainer or "", rating


def preferred_profile(con: sqlite3.Connection, horse_id: str) -> Optional[dict]:
    """Best (venue, surface, distance) for a horse by place rate (top-3 finish)."""
    q = """
    SELECT race_track, track_type, distance, place
    FROM results WHERE horse_id=?
    """
    df = pd.read_sql_query(q, con, params=(horse_id,))
    if df.empty:
        return None
    df["distance"] = pd.to_numeric(df["distance"], errors="coerce")
    df["place_num"] = pd.to_numeric(df["place"], errors="coerce")
    df = df.dropna(subset=["distance"])
    if df.empty:
        return None
    df["surface"] = df["track_type"].fillna("").apply(
        lambda t: "AWT" if str(t).lower().startswith("all") else "Turf")
    grp = df.groupby(["race_track", "surface", "distance"])
    best = None
    for (trk, surf, dist), g in grp:
        n = len(g)
        if n < MIN_RUNS_FOR_PREF:
            continue
        placed = int((g["place_num"] <= 3).sum())
        won = int((g["place_num"] == 1).sum())
        pr = placed / n
        # Prefer higher place rate, then more runs as tiebreak
        key = (round(pr, 3), n)
        if best is None or key > best["_key"]:
            best = {"venue": str(trk), "surface": surf, "distance": int(dist),
                    "n": n, "place_rate": pr, "wins": won, "_key": key}
    if best is None:
        # Fall back to most-run condition regardless of count
        g = grp.size().sort_values(ascending=False)
        (trk, surf, dist) = g.index[0]
        sub = df[(df["race_track"] == trk) & (df["surface"] == surf)
                 & (df["distance"] == dist)]
        best = {"venue": str(trk), "surface": surf, "distance": int(dist),
                "n": int(len(sub)),
                "place_rate": float((sub["place_num"] <= 3).mean()),
                "wins": int((sub["place_num"] == 1).sum())}
    best.pop("_key", None)
    return best


# ── Anticipation engine ──────────────────────────────────────────────────
def _gap_score(gap: int) -> float:
    """Reward target dates that fall in a plausible/ideal prep window."""
    if gap < PREP_MIN_GAP or gap > PREP_MAX_GAP:
        return 0.0
    if PREP_IDEAL_LO <= gap <= PREP_IDEAL_HI:
        return 1.0
    # linear falloff outside ideal but inside plausible
    if gap < PREP_IDEAL_LO:
        return 0.5 + 0.5 * (gap - PREP_MIN_GAP) / max(1, PREP_IDEAL_LO - PREP_MIN_GAP)
    return 0.5 + 0.5 * (PREP_MAX_GAP - gap) / max(1, PREP_MAX_GAP - PREP_IDEAL_HI)


_CYCLE_BONUS = {
    "PRIMED": 1.0, "EARLY_DROPPER": 0.6, "WATCH": 0.4,
    "NEUTRAL": 0.1, "FADE_OVERRATED": -0.3, "NO_DATA": 0.0,
}


def anticipate_targets(db_path: Path = Path("hkjc.db"),
                       weeks: int = DEFAULT_WEEKS,
                       window_days: int = ACTIVE_WINDOW_DAYS,
                       fixture_file: Path = Path("data/fixtures.json")
                       ) -> dict:
    """Build the fixture-targeting board.

    Returns a payload: generated_at, latest_data_date, fixtures[], targets[]
    (each a HorseTarget) and a `by_date` index for calendar rendering.
    """
    from horse_cycle import compute_profile_cycle

    con = sqlite3.connect(str(db_path))
    try:
        latest = _latest_date(con)
        fixtures = upcoming_fixtures(latest, weeks, fixture_file)
        fx_by_venue: dict[str, list[Fixture]] = {}
        for f in fixtures:
            fx_by_venue.setdefault(f.venue, []).append(f)

        act = active_horses(con, latest, window_days)
        targets: list[HorseTarget] = []
        for _, arow in act.iterrows():
            hid = str(arow["horse_id"])
            last_run = str(arow["last_run_date"])[:10]
            pref = preferred_profile(con, hid)
            if not pref:
                continue
            venue = pref["venue"]
            venue_fx = fx_by_venue.get(venue, [])
            if not venue_fx:
                continue
            name, trainer, rating = _horse_meta(con, hid)

            # Find best candidate fixture at preferred venue.
            last_d = datetime.strptime(last_run, "%Y-%m-%d").date()
            best_fx = None
            best_gscore = 0.0
            for f in venue_fx:
                fd = datetime.strptime(f.date, "%Y-%m-%d").date()
                gap = (fd - last_d).days
                gs = _gap_score(gap)
                if gs > best_gscore:
                    best_gscore = gs
                    best_fx = (f, gap, gs)
            if best_fx is None:
                continue
            f, gap, gscore = best_fx

            # Cycle overlay (full-history profile, no today context needed)
            try:
                cyc = compute_profile_cycle(horse_id=hid, db_path=db_path)
            except Exception:
                cyc = None
            cflag = (cyc or {}).get("primary_flag", "NO_DATA")
            cstate = (cyc or {}).get("cycle_state", "unknown")

            score = (0.55 * gscore
                     + 0.30 * pref["place_rate"]
                     + 0.15 * max(0.0, _CYCLE_BONUS.get(cflag, 0.0)))

            reason_bits = [
                f"prefers {VENUE_NAMES.get(venue, venue)} "
                f"{pref['distance']}m {pref['surface']} "
                f"({pref['wins']}W, {pref['place_rate']:.0%} placed/{pref['n']})",
                f"{gap}d after last run",
            ]
            if cflag in ("PRIMED", "EARLY_DROPPER", "WATCH"):
                reason_bits.append(cflag.lower().replace("_", " "))

            targets.append(HorseTarget(
                horse_id=hid, horse_name=name, trainer=trainer,
                target_date=f.date, target_venue=venue,
                pref_venue=venue, pref_surface=pref["surface"],
                pref_distance=pref["distance"],
                pref_place_rate=round(pref["place_rate"], 3),
                pref_n=pref["n"], last_run_date=last_run,
                days_since_last=(datetime.strptime(latest, "%Y-%m-%d").date() - last_d).days,
                gap_to_target=gap, current_rating=rating,
                cycle_state=cstate, cycle_flag=cflag,
                score=round(score, 4), reason="; ".join(reason_bits),
            ))
    finally:
        con.close()

    targets.sort(key=lambda t: (t.target_date, -t.score))
    by_date: dict[str, list[dict]] = {}
    for t in targets:
        by_date.setdefault(t.target_date, []).append(asdict(t))

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "latest_data_date": latest,
        "weeks": weeks,
        "fixtures": [asdict(f) for f in fixtures],
        "targets": [asdict(t) for t in targets],
        "by_date": by_date,
    }


def load_or_build_targets(db_path: Path = Path("hkjc.db"),
                          weeks: int = DEFAULT_WEEKS,
                          max_age_hours: float = 12.0) -> dict:
    """Cached entry point for the dashboard.

    Reuses reports/fixture_targets.json when it is fresh (younger than
    max_age_hours); otherwise rebuilds.
    """
    out = Path("reports") / "fixture_targets.json"
    if out.exists():
        age_h = (datetime.now().timestamp() - out.stat().st_mtime) / 3600.0
        if age_h <= max_age_hours:
            try:
                return json.loads(out.read_text(encoding="utf-8"))
            except Exception:
                pass
    payload = anticipate_targets(db_path, weeks)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=DEFAULT_WEEKS)
    ap.add_argument("--db", default="hkjc.db")
    args = ap.parse_args()
    payload = anticipate_targets(Path(args.db), args.weeks)
    out = Path("reports") / "fixture_targets.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    print(f"Latest data: {payload['latest_data_date']}  ·  "
          f"{len(payload['fixtures'])} fixtures  ·  "
          f"{len(payload['targets'])} anticipated targets")
    print("=" * 92)
    for d, items in payload["by_date"].items():
        prov = next((f["provisional"] for f in payload["fixtures"]
                     if f["date"] == d), True)
        tag = " (provisional)" if prov else ""
        print(f"\n{d}{tag}  —  {len(items)} candidates")
        for t in items[:8]:
            print(f"  {t['score']:.2f}  {t['horse_name'][:22]:<22} "
                  f"{t['trainer'][:14]:<14} {t['cycle_flag']:<14} {t['reason']}")
    print(f"\nJSON saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
