"""CLI-safe helpers for the master results xlsx.

Usable from schedulers and scrapers (no streamlit dependency).
Mirrors dashboard._append_results_to_db / _safe_read_excel logic.
"""
from __future__ import annotations
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import pandas as pd

BASE = Path(__file__).parent
DB_FILE = BASE / "hkjc_results_updated.xlsx"


def safe_read_excel(path: Path) -> pd.DataFrame:
    """Read xlsx tolerating OneDrive locks + zip corruption."""
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


def append_results_to_db(results_path: Path,
                         db_file: Path = DB_FILE,
                         verbose: bool = True) -> Optional[int]:
    """Append a reports/results_YYYYMMDD.json file to the master xlsx.

    Idempotent: if the date already exists in the DB it is overwritten.
    Returns the number of rows inserted, or None on failure.
    """
    results_path = Path(results_path)
    if not results_path.exists():
        if verbose:
            print(f"  [db] results file not found: {results_path}")
        return None
    try:
        data = json.loads(results_path.read_text(encoding="utf-8"))
    except Exception as e:
        if verbose:
            print(f"  [db] could not parse {results_path.name}: {e}")
        return None

    race_date = data.get("date", "")
    venue = (data.get("venue") or "").upper()
    race_track = "ST" if venue in ("ST", "SHA TIN") else (
        "HV" if venue in ("HV", "HAPPY VALLEY") else venue)

    new_rows = []
    for race in data.get("races", []):
        is_awt = bool(race.get("is_awt"))
        track_type = "All Weather Track" if is_awt else "Turf"
        for runner in race.get("runners", []):
            pos_list = runner.get("positions", []) or []
            running_positions = " ".join(p for p in pos_list if p)
            sec_list = runner.get("sectiontimes", []) or []
            sectiontimes_str = "; ".join(s for s in sec_list if s)
            new_rows.append({
                "race_date": race_date,
                "race_number": race.get("race_number"),
                "horse_number": runner.get("horse_no"),
                "horse_name": runner.get("horse_name", ""),
                "place": runner.get("place", ""),
                "jockey": runner.get("jockey", ""),
                "trainer": runner.get("trainer", ""),
                "actual_weight": runner.get("actual_weight"),
                "declared_weight": runner.get("declared_weight"),
                "draw": runner.get("draw"),
                "lbw": runner.get("lbw", ""),
                "running_positions": running_positions,
                "finish_time_seconds": runner.get("finish_time_seconds"),
                "win_odds": runner.get("win_odds", ""),
                "going": race.get("going", ""),
                "race_class": race.get("race_class", ""),
                "race_course": race.get("race_course", ""),
                "race_track": race_track,
                "track_type": track_type,
                "distance": race.get("distance"),
                "sectiontimes": sectiontimes_str,
            })

    if not new_rows:
        if verbose:
            print(f"  [db] no runner rows in {results_path.name}")
        return 0

    new_df = pd.DataFrame(new_rows)

    # Merge with existing (de-duplicate on race_date)
    if db_file.exists():
        try:
            existing = safe_read_excel(db_file)
        except Exception as e:
            if verbose:
                print(f"  [db] cannot read {db_file.name}: {e}; writing new only")
            existing = None
        if existing is not None and "race_date" in existing.columns:
            existing = existing[existing["race_date"] != race_date]
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df
    else:
        combined = new_df

    # Write via temp → copy (avoids OneDrive partial-write corruption)
    tmp_out = Path(tempfile.gettempdir()) / db_file.name
    combined.to_excel(tmp_out, index=False)
    if db_file.exists():
        try:
            shutil.copy2(db_file, db_file.with_suffix(".xlsx.bak"))
        except Exception:
            pass
    try:
        shutil.copy2(tmp_out, db_file)
        if verbose:
            print(f"  [db] appended {len(new_df)} rows to {db_file.name} "
                  f"(total: {len(combined)}).")
    except PermissionError:
        if verbose:
            print(f"  [db] OneDrive lock — saved to {tmp_out} instead.")
    return len(new_df)
