"""CLI-safe helpers for the master results xlsx.

Usable from schedulers and scrapers (no streamlit dependency).
Mirrors dashboard._append_results_to_db / _safe_read_excel logic.
"""
from __future__ import annotations
import json
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import pandas as pd

BASE = Path(__file__).parent
DB_FILE = BASE / "hkjc_results_updated.xlsx"
SQLITE_FILE = BASE / "hkjc.db"
SQLITE_TABLE = "results"
RACECARD_DIR = BASE / "racecards"


def _load_racecard_lookup(race_date: str) -> dict:
    """Return {(race_number:int, normalized_name:str): {rating, gear, horse_id}}.

    race_date is YYYY-MM-DD; racecard file is racecard_YYYYMMDD.xlsx.
    Returns {} if the racecard is unavailable.
    """
    if not race_date:
        return {}
    try:
        compact = race_date.replace("-", "")
    except Exception:
        return {}
    path = RACECARD_DIR / f"racecard_{compact}.xlsx"
    if not path.exists():
        return {}
    try:
        df = pd.read_excel(path, sheet_name="All Races")
    except Exception:
        try:
            df = pd.read_excel(path)
        except Exception:
            return {}
    if df.empty:
        return {}
    out: dict = {}
    for _, row in df.iterrows():
        try:
            rn = int(row.get("race_number"))
        except Exception:
            continue
        name = str(row.get("horse_name") or "").strip().upper()
        if not name:
            continue
        out[(rn, name)] = {
            "rating": row.get("rating"),
            "gear": row.get("gear"),
            "horse_id": row.get("horse_id"),
        }
    return out


def _iso_to_dc(date_iso: str) -> str:
    """YYYY-MM-DD -> DD/MM/YYYY (HKJC URL format)."""
    try:
        y, m, d = date_iso.split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return ""


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

    # v4.8: enrich post-race results with racecard data (rating/gear/horse_id)
    # so DB rows have the same columns populated as the legacy xlsx-scraped
    # rows.  Falls back to None when the racecard is unavailable.
    rc_lookup = _load_racecard_lookup(race_date)
    date_dc = _iso_to_dc(race_date)
    race_url_by_no: dict = {}
    if date_dc:
        for r in data.get("races", []):
            try:
                rn = int(r.get("race_number"))
            except Exception:
                continue
            race_url_by_no[rn] = (
                "https://racing.hkjc.com/en-us/local/information/"
                f"displaysectionaltime?racedate={date_dc}&RaceNo={rn}"
            )

    new_rows = []
    for race in data.get("races", []):
        is_awt = bool(race.get("is_awt"))
        track_type = "All Weather Track" if is_awt else "Turf"
        try:
            race_no_int = int(race.get("race_number"))
        except Exception:
            race_no_int = None
        for runner in race.get("runners", []):
            pos_list = runner.get("positions", []) or []
            running_positions = " ".join(p for p in pos_list if p)
            sec_list = runner.get("sectiontimes", []) or []
            sectiontimes_str = "; ".join(s for s in sec_list if s)

            # Enrich from racecard
            rc_row = {}
            if race_no_int is not None:
                rc_row = rc_lookup.get(
                    (race_no_int,
                     str(runner.get("horse_name", "") or "").strip().upper()),
                    {},
                )
            horse_id_val = (
                runner.get("horse_id")
                or rc_row.get("horse_id")
                or None
            )
            if isinstance(horse_id_val, str):
                horse_id_val = horse_id_val.strip() or None
            horse_url_val = (
                runner.get("horse_url")
                or (
                    f"https://racing.hkjc.com/en-us/local/information/"
                    f"horse?horseid={horse_id_val}"
                    if horse_id_val
                    else None
                )
            )
            rating_val = runner.get("rating")
            if rating_val in (None, "") and "rating" in rc_row:
                rating_val = rc_row.get("rating")
            if pd.isna(rating_val) if rating_val is not None else False:
                rating_val = None
            gear_val = runner.get("gear")
            if gear_val in (None, "") and "gear" in rc_row:
                gear_val = rc_row.get("gear")
            if isinstance(gear_val, float) and pd.isna(gear_val):
                gear_val = None

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
                "rating": rating_val,
                "gear": gear_val,
                "horse_id": horse_id_val,
                "horse_url": horse_url_val,
                "race_url": race_url_by_no.get(race_no_int)
                if race_no_int is not None else None,
            })

    if not new_rows:
        if verbose:
            print(f"  [db] no runner rows in {results_path.name}")
        return 0

    new_df = pd.DataFrame(new_rows)

    # Merge with existing (de-duplicate on race_date).
    # Safety net (v4.8.1): if the master xlsx is unreadable (e.g. zip
    # corruption from a partial OneDrive sync) but the SQLite mirror exists,
    # fall back to it — *never* write a fresh xlsx containing only the new
    # rows, which would silently nuke the master DB.
    existing = None
    if db_file.exists():
        try:
            existing = safe_read_excel(db_file)
        except Exception as e:
            if verbose:
                print(f"  [db] cannot read {db_file.name}: {e}; "
                      f"falling back to sqlite mirror")
    if existing is None and SQLITE_FILE.exists():
        try:
            with sqlite3.connect(SQLITE_FILE) as conn:
                existing = pd.read_sql_query(
                    f"SELECT * FROM {SQLITE_TABLE}", conn)
            if verbose:
                print(f"  [db] loaded {len(existing)} rows from "
                      f"sqlite mirror as fallback")
        except Exception as e:
            if verbose:
                print(f"  [db] sqlite fallback failed: {e}")
            existing = None

    if existing is not None and "race_date" in existing.columns:
        # Robust dedup — compare on YYYY-MM-DD string regardless of
        # whether existing race_date is stored as Timestamp or string.
        ex_keys = pd.to_datetime(
            existing["race_date"], errors="coerce"
        ).dt.strftime("%Y-%m-%d")
        existing = existing[ex_keys != race_date]
        combined = pd.concat([existing, new_df], ignore_index=True)
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
    # Retry the copy a few times — OneDrive can briefly lock the xlsx while
    # it syncs the previous version. A silent failure here is exactly what
    # leaves the master xlsx stale relative to sqlite (the symptom the user
    # hit with the post-29-April scrape).
    import time as _time
    xlsx_status = "stale"
    for _attempt in range(5):
        try:
            shutil.copy2(tmp_out, db_file)
            if verbose:
                print(f"  [db] appended {len(new_df)} rows to {db_file.name} "
                      f"(total: {len(combined)}).")
            xlsx_status = "ok"
            break
        except PermissionError:
            if verbose:
                print(f"  [db] xlsx locked (attempt {_attempt+1}/5) — retrying…")
            _time.sleep(1.5)
        except OSError as e:
            if verbose:
                print(f"  [db] xlsx copy failed: {e}")
            xlsx_status = f"error: {e}"
            break
    if xlsx_status == "stale" and verbose:
        print(f"  [db] WARN: xlsx still locked after 5 attempts — sqlite "
              f"mirror IS up to date but {db_file.name} on disk is stale. "
              f"Run rebuild_xlsx_from_sqlite() once the lock clears, or "
              f"close any Excel windows holding the file.")

    # v4.7: also mirror full table to SQLite (hkjc.db). Drop-in replacement
    # for xlsx reads — no OneDrive lock, ~10x faster, queryable with SQL.
    try:
        n_sql = write_sqlite(combined, SQLITE_FILE)
        if verbose:
            print(f"  [db] mirrored {n_sql} rows → {SQLITE_FILE.name}")
    except Exception as e:
        if verbose:
            print(f"  [db] sqlite mirror skipped: {e}")

    return len(new_df)


# ─────────────────────────────────────────────────────────────────────────────
# SQLite mirror (v4.7)
# ─────────────────────────────────────────────────────────────────────────────

def write_sqlite(df: pd.DataFrame,
                 sqlite_path: Path = SQLITE_FILE,
                 table: str = SQLITE_TABLE) -> int:
    """Write the master results DataFrame to SQLite, replacing the table.

    Atomic: writes to a sibling .tmp file then os.replace.
    Adds indexes on race_date, horse_id, horse_name, race_track for fast
    lookups (the three common access patterns in the dashboard + backtests).
    Returns the row count written.
    """
    import os
    sqlite_path = Path(sqlite_path)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = sqlite_path.with_suffix(sqlite_path.suffix + ".tmp")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass

    # Coerce race_date → ISO string so SQLite stores it as TEXT (sortable,
    # comparable with WHERE race_date >= '2026-04-01').
    out = df.copy()
    if "race_date" in out.columns:
        out["race_date"] = pd.to_datetime(
            out["race_date"], errors="coerce"
        ).dt.strftime("%Y-%m-%d")

    conn = sqlite3.connect(str(tmp))
    try:
        out.to_sql(table, conn, if_exists="replace", index=False)
        cur = conn.cursor()
        for col in ("race_date", "horse_id", "horse_name", "race_track"):
            if col in out.columns:
                cur.execute(
                    f'CREATE INDEX IF NOT EXISTS '
                    f'idx_{table}_{col} ON {table}("{col}")'
                )
        conn.commit()
    finally:
        conn.close()

    os.replace(tmp, sqlite_path)
    return len(out)


def rebuild_sqlite_from_xlsx(xlsx_path: Path = DB_FILE,
                             sqlite_path: Path = SQLITE_FILE) -> int:
    """Bootstrap helper: rebuild hkjc.db from the master xlsx in one shot."""
    df = safe_read_excel(Path(xlsx_path))
    return write_sqlite(df, Path(sqlite_path))


def rebuild_xlsx_from_sqlite(xlsx_path: Path = DB_FILE,
                             sqlite_path: Path = SQLITE_FILE,
                             *, verbose: bool = False) -> tuple[int, str]:
    """Rebuild the master xlsx from the SQLite mirror.

    SQLite is the runtime source of truth (Step 1 of the post-race pipeline
    always keeps it current via ``append_results_to_db`` + ``write_sqlite``).
    The xlsx, by contrast, lives under OneDrive and can silently fail to
    update when OneDrive holds a sync lock — leaving the on-disk xlsx
    (and any copy pushed to GitHub) stale relative to sqlite.

    This helper regenerates the xlsx from sqlite via the OneDrive-safe
    TEMP → copy pattern with 5x retry. Returns ``(rows_written, status)``.
    Status is one of: ``"ok"``, ``"locked"`` (xlsx in TEMP, target locked),
    or ``"error: <msg>"``.
    """
    import os
    import time
    sqlite_path = Path(sqlite_path)
    xlsx_path = Path(xlsx_path)
    if not sqlite_path.exists():
        return (0, f"error: {sqlite_path} missing")
    try:
        with sqlite3.connect(str(sqlite_path)) as conn:
            df = pd.read_sql_query(f"SELECT * FROM {SQLITE_TABLE}", conn)
    except Exception as e:
        return (0, f"error: sqlite read failed: {e}")
    if df.empty:
        return (0, "error: sqlite empty")
    tmp_out = Path(tempfile.gettempdir()) / xlsx_path.name
    try:
        df.to_excel(tmp_out, index=False)
    except Exception as e:
        return (0, f"error: xlsx write to TEMP failed: {e}")
    if xlsx_path.exists():
        try:
            shutil.copy2(xlsx_path, xlsx_path.with_suffix(".xlsx.bak"))
        except Exception:
            pass
    for attempt in range(5):
        try:
            shutil.copy2(tmp_out, xlsx_path)
            if verbose:
                print(f"  [db] rebuilt {xlsx_path.name} from sqlite "
                      f"({len(df)} rows).")
            return (len(df), "ok")
        except PermissionError:
            if verbose:
                print(f"  [db] xlsx locked (attempt {attempt+1}/5) — retrying…")
            time.sleep(1.5)
        except OSError as e:
            return (len(df), f"error: {e}")
    if verbose:
        print(f"  [db] xlsx still locked after 5 attempts; left at {tmp_out}.")
    return (len(df), "locked")


def read_sqlite(query: str = f"SELECT * FROM {SQLITE_TABLE}",
                sqlite_path: Path = SQLITE_FILE) -> pd.DataFrame:
    """Convenience reader. Returns empty DF if hkjc.db missing."""
    sqlite_path = Path(sqlite_path)
    if not sqlite_path.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(str(sqlite_path))
    try:
        df = pd.read_sql_query(query, conn)
    finally:
        conn.close()
    if "race_date" in df.columns:
        df["race_date"] = pd.to_datetime(df["race_date"], errors="coerce")
    return df


def _coerce_excel_like(df: pd.DataFrame) -> pd.DataFrame:
    """Make a sqlite-loaded frame dtype-match ``pd.read_excel``.

    SQLite stores some numeric columns (notably ``draw``) as TEXT, so a
    value reads back as the string ``"12.0"`` instead of the float ``12.0``
    that ``read_excel`` infers. Downstream model code does ``int(draw)`` and
    crashes on ``int("12.0")``. To keep ``load_results_db`` a true drop-in for
    ``read_excel``, replicate pandas' Excel numeric inference: any object
    column whose every non-null value parses as a number is converted to
    numeric (float64 when nulls/fractions are present, int64 otherwise).
    Genuinely textual columns (horse names, section times, running
    positions, place codes like ``"PU"``) are left untouched.
    """
    for col in df.columns:
        if df[col].dtype != object:
            continue
        nonnull = df[col].dropna()
        if nonnull.empty:
            continue
        coerced = pd.to_numeric(nonnull, errors="coerce")
        if coerced.isna().any():
            continue  # at least one genuinely non-numeric value -> keep text
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_results_db(*, verbose: bool = False) -> pd.DataFrame:
    """Fast loader for the full historical results table.

    Prefers the sqlite mirror (``hkjc.db``, ~0.7 s) over the multi-MB
    OneDrive xlsx (~27 s). The sqlite mirror is auto-rebuilt by
    ``append_results_to_db`` on every results scrape, so it tracks the
    xlsx automatically. The xlsx is only consulted when the mirror is
    missing or staler than the xlsx, in which case it is also read via a
    $TEMP copy to dodge OneDrive PermissionError / zip-lock issues.

    Returns a DataFrame with ``race_date`` coerced to datetime. This is a
    drop-in replacement for ``pd.read_excel(hkjc_results_updated.xlsx)``.
    """
    try:
        xlsx_mtime = DB_FILE.stat().st_mtime if DB_FILE.exists() else 0.0
    except OSError:
        xlsx_mtime = 0.0
    try:
        sql_mtime = SQLITE_FILE.stat().st_mtime if SQLITE_FILE.exists() else 0.0
    except OSError:
        sql_mtime = 0.0

    # Fast path: trust the sqlite mirror when it's at least as fresh as the
    # xlsx (append_results_to_db writes both, so this is the common case).
    if SQLITE_FILE.exists() and sql_mtime >= xlsx_mtime:
        try:
            df = read_sqlite()
            if not df.empty:
                df = _coerce_excel_like(df)
                if verbose:
                    print(f"  [db] loaded {len(df):,} rows from "
                          f"{SQLITE_FILE.name} (sqlite mirror)")
                return df
        except Exception as exc:  # pragma: no cover - defensive
            if verbose:
                print(f"  [db] sqlite read failed ({exc}); falling back to xlsx")

    # Slow path: xlsx via a $TEMP copy (OneDrive lock-safe).
    if not DB_FILE.exists():
        return pd.DataFrame()
    target = DB_FILE
    for attempt in range(2):
        if attempt == 1:
            target = Path(tempfile.gettempdir()) / DB_FILE.name
            shutil.copy2(DB_FILE, target)
        try:
            df = pd.read_excel(target)
            break
        except (PermissionError, zipfile.BadZipFile):
            if attempt == 0:
                continue
            raise
    if "race_date" in df.columns:
        df["race_date"] = pd.to_datetime(df["race_date"], errors="coerce")
    if verbose:
        print(f"  [db] loaded {len(df):,} rows from {DB_FILE.name} (xlsx)")
    return df


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="db_utils: master DB helpers (xlsx + sqlite mirror)")
    ap.add_argument("--rebuild-sqlite", action="store_true",
                    help="Rebuild hkjc.db from hkjc_results_updated.xlsx")
    args = ap.parse_args()
    if args.rebuild_sqlite:
        n = rebuild_sqlite_from_xlsx()
        print(f"[db] rebuilt {SQLITE_FILE.name}: {n} rows")
    else:
        ap.print_help()
