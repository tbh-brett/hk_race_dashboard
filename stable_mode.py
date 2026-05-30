"""stable_mode.py — Trainer-state overlay (v0.1)

For each trainer, classify current season state from hkjc.db results.
Uses the season start at 1 Sept of the most recent September on or before
the latest race date.

Modes:
    championship   — top 5 by wins this season (driving for premiership)
    elite          — ranks 6-12 (consistent operators)
    mid            — ranks 13-22
    survival       — bottom 4 of active stables (saving licence)
    new            — first season with a runner in HK (e.g. B Crawford)
    unknown        — no DB activity

The overlay multiplies / shades the horse_cycle confidence:
    - championship + PRIMED → very strong (premiership win at premium time)
    - survival + PRIMED     → very strong (need the prize money)
    - championship + FADE   → still watch (they may have it spot-on)
    - survival + non-fancied dropper → highest-edge pattern (cheap entries)
    - new trainer           → wider uncertainty bands; expect surprises
"""
from __future__ import annotations
import sqlite3
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
import json
import sys
from typing import Optional

import pandas as pd


def _season_start(latest_date: date) -> date:
    """HKJC season runs Sep 1 → mid-July. Find current season's Sep 1."""
    if latest_date.month >= 9:
        return date(latest_date.year, 9, 1)
    return date(latest_date.year - 1, 9, 1)


@dataclass
class StableMode:
    trainer: str
    season_runs: int
    season_wins: int
    season_places: int       # 1-2-3 finishes
    win_rate: float          # wins / runs
    place_rate: float        # 1-2-3 / runs
    rank_by_wins: Optional[int]
    n_active: int            # number of trainers with ≥1 run this season
    first_season: bool
    mode: str                # championship / elite / mid / survival / new / unknown
    note: str


def compute_stable_modes(db_path: Path = Path("hkjc.db")) -> dict[str, StableMode]:
    con = sqlite3.connect(str(db_path))
    try:
        df = pd.read_sql_query(
            "SELECT race_date, trainer, place FROM results WHERE trainer IS NOT NULL "
            "AND trainer != ''",
            con,
        )
    finally:
        con.close()
    df["race_date"] = pd.to_datetime(df["race_date"], errors="coerce")
    df = df.dropna(subset=["race_date"])
    df["place_num"] = pd.to_numeric(df["place"], errors="coerce")
    latest = df["race_date"].max().date()
    season_start = _season_start(latest)

    in_season = df[df["race_date"] >= pd.Timestamp(season_start)]
    earlier   = df[df["race_date"] <  pd.Timestamp(season_start)]
    prior_trainers = set(earlier["trainer"].unique())

    agg = in_season.groupby("trainer").agg(
        season_runs=("place_num", "size"),
        season_wins=("place_num", lambda s: int((s == 1).sum())),
        season_places=("place_num", lambda s: int(((s >= 1) & (s <= 3)).sum())),
    ).reset_index()
    # Only treat as "licensed HK stable" if ≥50 starts in season; rest are visiting
    LICENSED_MIN_RUNS = 50
    licensed = agg[agg["season_runs"] >= LICENSED_MIN_RUNS].copy()
    licensed = licensed.sort_values("season_wins", ascending=False).reset_index(drop=True)
    licensed["rank_by_wins"] = licensed.index + 1
    n_active = len(licensed)
    licensed_set = set(licensed["trainer"])

    modes: dict[str, StableMode] = {}
    # Licensed stables: real ranking
    for _, row in licensed.iterrows():
        trainer = row["trainer"]
        runs = int(row["season_runs"])
        wins = int(row["season_wins"])
        places = int(row["season_places"])
        rank = int(row["rank_by_wins"])
        first_season = trainer not in prior_trainers
        if first_season:
            mode = "new"
        elif rank <= 5:
            mode = "championship"
        elif rank <= 12:
            mode = "elite"
        elif rank > n_active - 4:
            mode = "survival"
        else:
            mode = "mid"
        note = (f"Season {season_start.year}-{(season_start.year+1)%100:02d}: "
                f"{wins}W / {runs}R (rank {rank}/{n_active}); mode={mode}"
                + (" (first HK season)" if first_season else ""))
        modes[trainer] = StableMode(
            trainer=trainer,
            season_runs=runs, season_wins=wins, season_places=places,
            win_rate=(wins / runs) if runs else 0.0,
            place_rate=(places / runs) if runs else 0.0,
            rank_by_wins=rank,
            n_active=n_active,
            first_season=first_season,
            mode=mode,
            note=note,
        )
    # Visiting (non-licensed) trainers
    for _, row in agg[~agg["trainer"].isin(licensed_set)].iterrows():
        trainer = row["trainer"]
        modes[trainer] = StableMode(
            trainer=trainer,
            season_runs=int(row["season_runs"]),
            season_wins=int(row["season_wins"]),
            season_places=int(row["season_places"]),
            win_rate=0.0, place_rate=0.0,
            rank_by_wins=None, n_active=n_active,
            first_season=False, mode="visiting",
            note=f"Visiting / international ({int(row['season_runs'])} runs in season)",
        )
    return modes


def main() -> int:
    out_path = Path("reports/stable_modes.json")
    modes = compute_stable_modes()
    payload = {t: asdict(m) for t, m in modes.items()}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    # Print top + bottom
    sorted_modes = sorted(modes.values(), key=lambda m: m.rank_by_wins or 9999)
    print(f"\n{'='*80}")
    print(f"{'Rank':>4} {'Trainer':<18} {'Mode':<12} {'W':>3} {'R':>4} {'W%':>5} {'P%':>5}")
    print("=" * 80)
    for m in sorted_modes:
        if m.mode == "visiting":
            continue
        print(f"{m.rank_by_wins:>4} {m.trainer[:18]:<18} {m.mode:<12} "
              f"{m.season_wins:>3} {m.season_runs:>4} "
              f"{m.win_rate*100:>4.1f}% {m.place_rate*100:>4.1f}%")
    print(f"\nSaved → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
