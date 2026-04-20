#!/usr/bin/env python3
"""
build_form_guide.py — Pre-build form guide JSON cache for a meeting date.

Usage:
    python build_form_guide.py 2026-04-01

Reads:
    - cache/racecard_YYYY-MM-DD.json  (horse list per race)
    - hkjc_results_updated.xlsx       (historical results)

Writes:
    - cache/form_guide_YYYY-MM-DD.json
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

BASE = Path(__file__).parent
CACHE_DIR = BASE / "cache"
RACECARDS_DIR = BASE / "racecards"

FORM_COLS = [
    "horse_name", "race_date", "race_number", "race_track", "race_course",
    "going", "race_class", "jockey", "rating", "draw", "running_positions",
    "place", "lbw", "finish_time_seconds", "distance", "actual_weight",
]


def _load_form_db() -> pd.DataFrame:
    db_file = BASE / "hkjc_results_updated.xlsx"
    if not db_file.exists():
        sys.exit(f"ERROR: {db_file} not found.")
    try:
        df = pd.read_excel(db_file)
    except PermissionError:
        tmp = Path(tempfile.gettempdir()) / db_file.name
        shutil.copy2(db_file, tmp)
        df = pd.read_excel(tmp)
    keep = [c for c in FORM_COLS if c in df.columns]
    df = df[keep].copy()
    df["race_date"] = pd.to_datetime(df["race_date"]).dt.date
    df["place_num"] = pd.to_numeric(df["place"], errors="coerce")
    df["horse_name_upper"] = df["horse_name"].str.upper().str.strip()
    return df


def _build_race_index(form_db: pd.DataFrame) -> dict:
    idx = {}
    for key, grp in form_db.groupby(["race_date", "race_number"]):
        sorted_g = grp.sort_values("place_num")
        valid = sorted_g.dropna(subset=["place_num"])
        unique_places = sorted(valid["place_num"].unique())[:5]
        top_runners = valid[valid["place_num"].isin(unique_places)]
        top5 = [(int(r["place_num"]), r["horse_name"])
                for _, r in top_runners.iterrows()]
        second = sorted_g[sorted_g["place_num"] == 2]
        margin_2nd = str(second.iloc[0]["lbw"]) if not second.empty else "-"
        idx[(str(key[0]), int(key[1]))] = {"top5": top5, "margin_2nd": margin_2nd}
    return idx


def _fmt_positions(pos_str) -> str:
    if not pos_str or (isinstance(pos_str, float) and pd.isna(pos_str)):
        return "-"
    parts = str(pos_str).strip().split()
    return "-".join(p for p in parts if p.strip()) or "-"


def _fmt_time(secs) -> str:
    if pd.isna(secs):
        return "-"
    try:
        s = float(secs)
    except (ValueError, TypeError):
        return "-"
    mins = int(s // 60)
    remainder = s - mins * 60
    return f"{mins}:{remainder:05.2f}"


def _fmt_margin(place, lbw, race_idx_entry: dict) -> str:
    try:
        p = int(float(place))
    except (ValueError, TypeError):
        return str(lbw) if lbw and str(lbw) not in ("", "nan") else "-"
    if p == 1:
        m2 = race_idx_entry.get("margin_2nd", "-")
        return m2 if m2 and m2 != "-" else "0"
    lbw_s = str(lbw) if lbw and str(lbw) not in ("", "nan") else "-"
    return lbw_s


def _racecard_xlsx_to_dict(xlsx_path: Path, date_iso: str) -> dict:
    """Reconstruct a racecard JSON-cache dict directly from the xlsx.

    Used as a fallback when cache/racecard_YYYY-MM-DD.json is a stub or missing
    (e.g. HKJC live page was scraped post-meeting and returned no horses).
    """
    df = pd.read_excel(str(xlsx_path), sheet_name="All Races")
    if df.empty:
        raise RuntimeError(f"xlsx at {xlsx_path} is empty")

    racecourse = ""
    if "racecourse" in df.columns and pd.notna(df["racecourse"].iloc[0]):
        racecourse = str(df["racecourse"].iloc[0])

    races_out: list[dict] = []
    meta_fields = [
        "race_number", "race_name", "race_class", "distance", "surface",
        "race_course", "going", "rating_range", "prize", "race_time",
    ]
    for rn, grp in df.groupby("race_number"):
        if pd.isna(rn):
            continue
        first = grp.iloc[0]
        meta = {}
        for k in meta_fields:
            v = first.get(k) if k in grp.columns else None
            if pd.isna(v):
                meta[k] = ""
            else:
                # Keep numeric fields numeric where appropriate
                if k in ("race_number", "distance"):
                    try:
                        meta[k] = int(float(v))
                    except (ValueError, TypeError):
                        meta[k] = v
                else:
                    meta[k] = v
        horses: list[dict] = []
        for _, row in grp.iterrows():
            h: dict = {}
            for col in grp.columns:
                if col in meta_fields or col in ("race_date", "racecourse"):
                    continue
                v = row[col]
                if pd.isna(v):
                    continue
                h[col] = v
            if not h.get("horse_name"):
                continue
            horses.append(h)
        races_out.append({"meta": meta, "horses": horses})

    return {
        "race_date": date_iso,
        "racecourse": racecourse,
        "races": races_out,
    }


def build(date_iso: str) -> None:
    meeting_date = date.fromisoformat(date_iso)

    rc_path = CACHE_DIR / f"racecard_{date_iso}.json"
    racecard = None
    if rc_path.exists():
        with open(rc_path, "r", encoding="utf-8") as f:
            racecard = json.load(f)
        # Detect stub / empty-horses case and fall back to xlsx
        if not racecard.get("races") or all(
            not r.get("horses") for r in racecard.get("races", [])
        ):
            print(f"Racecard JSON empty/stub ({rc_path.name}); falling back to xlsx...")
            racecard = None

    if racecard is None:
        xl_path = RACECARDS_DIR / f"racecard_{date_iso.replace('-', '')}.xlsx"
        if not xl_path.exists():
            sys.exit(f"ERROR: No racecard JSON cache and no xlsx at {xl_path}")
        racecard = _racecard_xlsx_to_dict(xl_path, date_iso)
        # Persist rebuilt JSON cache so downstream code has it available
        with open(rc_path, "w", encoding="utf-8") as f:
            json.dump(racecard, f, ensure_ascii=False, indent=2, default=str)
        print(f"Rebuilt racecard JSON cache from xlsx: {rc_path}")

    print(f"Loading historical results...")
    form_db = _load_form_db()
    print(f"  {len(form_db):,} records loaded.")

    # v4.5: lane cache keyed by (date_compact, race_number) → {HORSE_UPPER: rec}
    try:
        from lane_utils import load_race_lanes
        def _lane_for(date_iso_str: str, rn: int):
            dc = date_iso_str.replace("-", "")
            return load_race_lanes(dc, int(rn)) or {}
        _LANE_CACHE: dict = {}
        def _get_lane(date_iso_str, rn, horse_upper):
            key = (date_iso_str, int(rn))
            if key not in _LANE_CACHE:
                _LANE_CACHE[key] = _lane_for(date_iso_str, rn)
            return _LANE_CACHE[key].get(horse_upper)
    except Exception as _lane_err:
        print(f"  (lane_utils unavailable: {_lane_err})")
        _get_lane = lambda *_a, **_k: None

    def _lane_fields_for_run(rd, rnum, hname):
        """Lookup lane record for a historical run; return display fields.
        Empty dict when no OCR data exists for that past meeting.
        """
        try:
            if pd.isna(rd) or pd.isna(rnum):
                return {}
            date_str = rd.isoformat() if hasattr(rd, "isoformat") else str(rd)
            rec = _get_lane(date_str, rnum, hname.strip().upper())
            if not rec:
                return {}
            return {
                "lane_avg": rec.get("avg_bucket"),
                "lane_at": rec.get("bucket_at") or {},
                "ground_lost_m": rec.get("ground_lost_m"),
            }
        except Exception:
            return {}

    race_idx = _build_race_index(form_db)

    output = {"date": date_iso, "races": []}

    for rc_race in racecard.get("races", []):
        meta = rc_race["meta"]
        rn = meta["race_number"]
        horses_out = []

        horses = [h for h in rc_race["horses"] if not h.get("is_standby")]
        for horse in horses:
            hname = horse["horse_name"]
            mask = form_db["horse_name_upper"] == hname.strip().upper()
            hist = form_db[mask & (form_db["race_date"] < meeting_date)]
            hist = hist.sort_values("race_date", ascending=False).head(6).sort_values("race_date", ascending=True)

            runs = []
            for _, row in hist.iterrows():
                rd = row["race_date"]
                rnum = row["race_number"]
                ri = race_idx.get((str(rd), int(rnum)), {"top5": [], "margin_2nd": "-"})

                try:
                    rtg = str(int(float(row["rating"]))) if pd.notna(row.get("rating")) else "?"
                except (ValueError, TypeError):
                    rtg = str(row.get("rating", "?"))

                place_val = str(int(row["place_num"])) if pd.notna(row.get("place_num")) else "?"
                try:
                    cls_val = str(int(float(row.get("race_class", "?"))))
                except (ValueError, TypeError):
                    cls_val = str(row.get("race_class", "?"))

                runs.append({
                    "date": rd.isoformat() if hasattr(rd, "isoformat") else str(rd),
                    "race_number": int(rnum) if pd.notna(rnum) else None,
                    "place": place_val,
                    "distance": int(row["distance"]) if pd.notna(row.get("distance")) else None,
                    "track": str(row.get("race_track", "?"))[:2],
                    "course": str(row.get("race_course", "?")),
                    "going": str(row.get("going", "?")),
                    "class": cls_val,
                    "jockey": str(row.get("jockey", "?")),
                    "trainer": str(row.get("trainer", "?")),
                    "rating": rtg,
                    "actual_weight": str(int(float(row["actual_weight"]))) if pd.notna(row.get("actual_weight")) else "?",
                    "draw": str(int(float(row["draw"]))) if pd.notna(row.get("draw")) and str(row["draw"]).strip().replace(".", "", 1).isdigit() else "?",
                    "positions": _fmt_positions(row.get("running_positions")),
                    "margin": _fmt_margin(row.get("place"), row.get("lbw"), ri),
                    "time": _fmt_time(row.get("finish_time_seconds")),
                    "top5": [(int(p), n) for p, n in ri.get("top5", [])],
                    "margin_2nd": ri.get("margin_2nd", "-"),
                    **_lane_fields_for_run(rd, rnum, hname),
                })

            horses_out.append({
                "horse_name": hname,
                "horse_no": horse.get("horse_no", "?"),
                "jockey": horse.get("jockey", "?"),
                "trainer": horse.get("trainer", "?"),
                "draw": horse.get("draw", "?"),
                "rating": horse.get("rating", "?"),
                "weight": horse.get("weight", "?"),
                "overweight": horse.get("overweight", ""),
                "last_6_runs": horse.get("last_6_runs", ""),
                "is_debutant": len(runs) == 0,
                "runs": runs,
            })

        output["races"].append({
            "race_number": rn,
            "race_name": meta.get("race_name", ""),
            "race_class": meta.get("race_class", ""),
            "distance": meta.get("distance", 0),
            "race_course": meta.get("race_course", ""),
            "horses": horses_out,
        })

    out_path = CACHE_DIR / f"form_guide_{date_iso}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"Form guide cache written: {out_path}")
    print(f"  {sum(len(r['horses']) for r in output['races'])} horses across {len(output['races'])} races")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python build_form_guide.py YYYY-MM-DD")
        sys.exit(1)
    build(sys.argv[1])
