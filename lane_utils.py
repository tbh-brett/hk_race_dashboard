"""Running-lane helpers.

Converts the horizontal fraction (x_frac, 0=inside rail, 1=outer) stored in
each running_position_photos/YYYYMMDD/R*.json into a 4-bucket lateral-position
label, and exposes per-horse / per-call lane summaries for display + modelling.

Data source: HKJC per-race running-position composite photos, OCR'd into
R*.json with per-horse `frames[].x_frac` at four calls (start, 800M, 400M,
200M). The OCR pipeline itself is not in this repo.

Bucket thresholds were calibrated from the 2026-04-15 meeting (n=101 horses,
quartiles 0.28 / 0.39 / 0.51).
"""
from __future__ import annotations
import json
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional, Tuple

BASE = Path(__file__).parent
RP_ROOT = BASE / "running_position_photos"

# Lane buckets — (label, css_color, max_x_frac_exclusive)
LANE_BUCKETS: List[Tuple[str, str, float]] = [
    ("Rail",        "#28a745", 0.28),   # green — saving maximum ground
    ("2-wide",      "#1f6feb", 0.40),   # blue
    ("3-wide",      "#d39e00", 0.55),   # amber
    ("4+ wide",     "#d73a49", 1.01),   # red — losing most ground
]

LANE_COLOUR = {name: col for name, col, _ in LANE_BUCKETS}
LANE_ORDER = {name: i for i, (name, _, _) in enumerate(LANE_BUCKETS)}

# "Ground saved" penalty per bucket (relative to 2-wide neutral), metres of
# extra distance travelled over a 1200m+ race. Rough HKJC-specific estimates:
#   rail  → −3m saved
#   2-wide→  0m
#   3-wide→ +4m lost
#   4+wide→ +9m lost
# Used by forthcoming model integration; NOT applied to scoring until
# validated on ≥30 races.
GROUND_LOST_M = {"Rail": -3.0, "2-wide": 0.0, "3-wide": 4.0, "4+ wide": 9.0}


def classify_lane(x_frac: Optional[float]) -> Optional[str]:
    """x_frac (0=rail, 1=outer) → bucket label. None → None."""
    if x_frac is None:
        return None
    try:
        x = float(x_frac)
    except (TypeError, ValueError):
        return None
    for name, _, upper in LANE_BUCKETS:
        if x < upper:
            return name
    return LANE_BUCKETS[-1][0]


def lane_colour(bucket: Optional[str]) -> str:
    return LANE_COLOUR.get(bucket or "", "#6a737d")


def load_race_lanes(date_compact: str, race_number: int) -> Optional[Dict]:
    """Load a R*.json if present and return a {horse_name: {...}} map.

    Each value contains:
      avg_x_frac, avg_bucket, bucket_at (dict of band_label→bucket),
      x_at (dict of band_label→x_frac), n_frames_seen, wide_frames,
      rail_frames, wide_all_way, rail_all_way, ground_lost_m.
    """
    path = RP_ROOT / date_compact / f"R{int(race_number)}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    out: Dict[str, Dict] = {}
    for h in raw.get("horses", []):
        name = (h.get("horse_name") or "").strip()
        if not name:
            continue
        avg_x = h.get("avg_x_frac")
        frames = h.get("frames", []) or []

        # Per-call (keep latest frame per band_label)
        x_at: Dict[str, float] = {}
        bucket_at: Dict[str, str] = {}
        for fr in frames:
            lbl = fr.get("band_label")
            xf = fr.get("x_frac")
            if lbl is None or xf is None:
                continue
            x_at[str(lbl)] = float(xf)
            b = classify_lane(xf)
            if b:
                bucket_at[str(lbl)] = b

        avg_bucket = classify_lane(avg_x)
        # Average ground lost across available calls (fallback to avg bucket)
        if bucket_at:
            ground_lost = mean(GROUND_LOST_M.get(b, 0.0) for b in bucket_at.values())
        elif avg_bucket:
            ground_lost = GROUND_LOST_M.get(avg_bucket, 0.0)
        else:
            ground_lost = None

        out[name.upper()] = {
            "horse_name": name,
            "horse_no": h.get("horse_no"),
            "avg_x_frac": avg_x,
            "avg_bucket": avg_bucket,
            "x_at": x_at,
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


__all__ = [
    "LANE_BUCKETS", "LANE_COLOUR", "LANE_ORDER", "GROUND_LOST_M",
    "classify_lane", "lane_colour", "load_race_lanes", "has_lane_data",
]
