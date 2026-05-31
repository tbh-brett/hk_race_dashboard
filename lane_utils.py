"""Running-lane helpers.

Reads HKJC per-race running-position composite photos (OCR'd into
running_position_photos/YYYYMMDD/R*.json by parse_rp_photos.py) and
classifies each horse's lane at every call into 4 buckets.

IMPORTANT — Which field encodes lane:
    `y_in_band` ∈ [0,1] with 0 = top of band = **inside rail** is the lane
    metric. `x_frac` is the horse's horizontal position in the photo
    (timeline axis) and does NOT encode lane width — do not use it.

Thresholds (calibrated against Apr-15 manual lane observations, n≈70):
    y < 0.42           → Rail
    0.42 ≤ y < 0.84    → 2-wide
    0.84 ≤ y < 0.96    → 3-wide
    y ≥ 0.96           → 4+ wide
Best-simple-thresholds achieve ~68% exact match vs manual labels; borderline
errors cluster in the 2-wide / 3-wide overlap zone (expected — visual
human calls on that boundary are noisy too).

Ground-lost convention:
    Only the 800M and 400M calls contribute to ground-lost metres.
    The 200M call happens after horses arrive on the straight, so any
    lateral movement from there does not impose an extra-distance penalty.
    (User observation, Apr-15 review.)
"""
from __future__ import annotations
import json
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional, Tuple

BASE = Path(__file__).parent
RP_ROOT = BASE / "running_position_photos"

# Lane buckets — (label, css_color, max_y_exclusive)
LANE_BUCKETS: List[Tuple[str, str, float]] = [
    ("Rail",    "#28a745", 0.42),   # green — saving maximum ground
    ("2-wide",  "#1f6feb", 0.84),   # blue
    ("3-wide",  "#d39e00", 0.96),   # amber
    ("4+ wide", "#d73a49", 1.01),   # red — losing most ground
]

LANE_COLOUR = {name: col for name, col, _ in LANE_BUCKETS}
LANE_ORDER = {name: i for i, (name, _, _) in enumerate(LANE_BUCKETS)}

# Metres of extra ground travelled vs 2-wide baseline, per call attended
# (800M and 400M only — the 200M call is on the straight so no further
# ground-loss is incurred).
GROUND_LOST_M = {"Rail": -3.0, "2-wide": 0.0, "3-wide": 4.0, "4+ wide": 9.0}

# Bands that DO accrue ground-lost.
GROUND_LOST_BANDS = {"800M", "400M"}


def classify_lane(y_in_band: Optional[float]) -> Optional[str]:
    """y_in_band (0=rail side, 1=outer) → bucket label. None → None."""
    if y_in_band is None:
        return None
    try:
        y = float(y_in_band)
    except (TypeError, ValueError):
        return None
    for name, _, upper in LANE_BUCKETS:
        if y < upper:
            return name
    return LANE_BUCKETS[-1][0]


def lane_colour(bucket: Optional[str]) -> str:
    return LANE_COLOUR.get(bucket or "", "#6a737d")


def load_race_lanes(date_compact: str, race_number: int) -> Optional[Dict]:
    """Load a R*.json if present and return a {HORSE_NAME_UPPER: {...}} map.

    Each value contains:
      horse_name, horse_no, avg_y, avg_bucket,
      bucket_at / y_at (dicts of band_label → bucket / y_in_band),
      n_frames_seen, ground_lost_m (from 800M+400M only).
    """
    path = RP_ROOT / date_compact / f"R{int(race_number)}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    band_label_by_idx = {i: b.get("label") for i, b in enumerate(raw.get("bands", []))}

    out: Dict[str, Dict] = {}
    for h in raw.get("horses", []):
        name = (h.get("horse_name") or "").strip()
        if not name:
            continue
        frames = h.get("frames", []) or []

        y_at: Dict[str, float] = {}
        bucket_at: Dict[str, str] = {}
        for fr in frames:
            lbl = fr.get("band_label") or band_label_by_idx.get(fr.get("band_idx"))
            y = fr.get("y_in_band")
            if lbl is None or y is None:
                continue
            y_at[str(lbl)] = float(y)
            b = classify_lane(y)
            if b:
                bucket_at[str(lbl)] = b

        avg_y = h.get("avg_y_in_band")
        if avg_y is None and y_at:
            avg_y = round(mean(y_at.values()), 3)
        avg_bucket = classify_lane(avg_y)

        # Ground lost: average across 800M + 400M only (200M excluded).
        gl_bands = [b for lbl, b in bucket_at.items() if lbl in GROUND_LOST_BANDS]
        if gl_bands:
            ground_lost = mean(GROUND_LOST_M.get(b, 0.0) for b in gl_bands)
        elif avg_bucket:
            ground_lost = GROUND_LOST_M.get(avg_bucket, 0.0)
        else:
            ground_lost = None

        out[name.upper()] = {
            "horse_name": name,
            "horse_no": h.get("horse_no"),
            "avg_y": avg_y,
            "avg_bucket": avg_bucket,
            "y_at": y_at,
            "bucket_at": bucket_at,
            "n_frames_seen": h.get("n_frames_seen"),
            "wide_frames": h.get("wide_frames"),
            "rail_frames": h.get("rail_frames"),
            "wide_all_way": bool(h.get("wide_all_way")),
            "rail_all_way": bool(h.get("rail_all_way")),
            "ground_lost_m": round(ground_lost, 1) if ground_lost is not None else None,
        }
    return out


def has_lane_data(date_compact: str) -> bool:
    """True if any R*.json exists for the meeting."""
    folder = RP_ROOT / date_compact
    if not folder.exists():
        return False
    return any(folder.glob("R*.json"))


def trip_note(lane_rec: Optional[Dict], place: Optional[int]) -> Optional[str]:
    """Post-race trip context for a single runner, or None if nothing notable.

    Backtest evidence (n=5,178 runs, Jan–May 2026; lane vs finish rho≈-0.03,
    i.e. lane is NOT a forward predictor). The ONLY robust, directional signal
    is a TRIP-EXCUSE one, correct only as a *post-race* annotation:

      * rail_all_way win% = 3.0% vs wide_all_way 8.7% (full season).
      * Within favourites, rail-trapped win 11.4% vs 16.9% base — they
        underperform their market price because they get held up / no clear run.
      * 2-wide is the productive lane (9.1% win); horses race wide because they
        are travelling forward into contention, not because wide "saves" them.

    So we flag rail-trapped horses that ran poorly as having a trip excuse
    (do NOT downgrade their ability on this run), and note wide horses that
    still placed as having earned it the hard way. Returns a short tag.
    """
    if not lane_rec:
        return None
    in_money = place is not None and place <= 3
    poor = place is not None and place >= 6
    avg_bucket = lane_rec.get("avg_bucket")
    rail_trapped = bool(lane_rec.get("rail_all_way")) or avg_bucket == "Rail"
    wide_all = bool(lane_rec.get("wide_all_way"))

    if rail_trapped and not in_money:
        # Strongest, most actionable flag — likely held up / no clear run.
        return "🛤️ Rail-trapped — trip excuse" if poor else "🛤️ Rail-trapped"
    if rail_trapped and in_money:
        return "Rail trip, still placed"
    if wide_all and in_money:
        return "Wide trip, placed on merit"
    if wide_all:
        return "Wide all the way"
    return None


__all__ = [
    "LANE_BUCKETS", "LANE_COLOUR", "LANE_ORDER", "GROUND_LOST_M",
    "GROUND_LOST_BANDS", "classify_lane", "lane_colour",
    "load_race_lanes", "has_lane_data", "trip_note",
]
