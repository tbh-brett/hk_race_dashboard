#!/usr/bin/env python3
"""
Parse HKJC Running Position Photos → trip JSON.

For each photo `running_position_photos/YYYYMMDD/R{N}.jpg`, produces
`running_position_photos/YYYYMMDD/R{N}.json` with:

{
  "date":          "YYYY-MM-DD",
  "race_number":   N,
  "img_w": W, "img_h": H,
  "bands": [
     {"label": "594", "y_top": 10, "y_bot": 131, "kind": "start"},
     {"label": "800M", "y_top": 131, "y_bot": 280, "kind": "800M"},
     ...
  ],
  "horses": [
     {
       "horse_name": "SILVERY KNIGHT",
       "ocr_text":   "Silvery Knight",
       "frames": [
          {"band_idx": 0, "band_label": "594", "cx": 179, "cy":  97,
           "x_frac": 0.25, "y_in_band": 0.72, "conf": 0.88},
          ...
       ],
       "avg_x_frac":     0.41,
       "avg_y_in_band":  0.62,
       "n_frames_seen":  4,
       "wide_all_way":   false,
       "rail_all_way":   true,
       "wide_frames":    0
     },
     ...
  ],
  "meta": {"ocr_boxes": 33, "matched_horses": 8, "field_size": 14}
}

Notes
-----
* HKJC only annotates a subset of runners per band (typically 4–8). A horse
  may not appear in every band. Unmatched OCR boxes are ignored.
* `y_in_band` ∈ [0,1] with 0 = top of band = inside rail (standard HK
  convention; Sha Tin 1000m runs on back straight → rail_y is still the top).
* Horse name matching uses normalised string similarity ≥ 0.80 against the
  racecard/results roster for that date + race.

Usage
-----
    python parse_rp_photos.py --date 2026-04-12
    python parse_rp_photos.py --dates 2026-04-01,2026-04-06
    python parse_rp_photos.py --from 2026-04-01 --to 2026-04-17
    python parse_rp_photos.py --date 2026-04-12 --force     # re-OCR
    python parse_rp_photos.py --date 2026-04-12 --race 3    # single race
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

BASE_DIR = Path(__file__).parent
PHOTOS_DIR = BASE_DIR / "running_position_photos"
REPORTS_DIR = BASE_DIR / "reports"

# ── Thresholds ────────────────────────────────────────────────────────────────
RED_ROW_MIN_PIXELS = 5        # min red pixels/row on left edge to count as meter-mark label
BAND_GROUP_GAP = 4            # rows distance that merges red pixels into one label
NAME_SIM_THR = 0.80           # SequenceMatcher cutoff for horse name match
OCR_CONF_MIN = 0.40           # ignore very low-confidence OCR boxes
WIDE_THRESHOLD = 0.55         # y_in_band > this ⇒ "wide"
RAIL_THRESHOLD = 0.35         # y_in_band < this ⇒ "on rail / 1-out"


# ── Utilities ─────────────────────────────────────────────────────────────────

def _normalise(s: str) -> str:
    """Normalise for fuzzy horse name matching."""
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _sim(a: str, b: str) -> float:
    na, nb = _normalise(a), _normalise(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def parse_date_arg(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def date_range(a: date, b: date) -> List[date]:
    if b < a:
        raise ValueError("--to must be on or after --from")
    out, cur = [], a
    while cur <= b:
        out.append(cur)
        cur += timedelta(days=1)
    return out


# ── Band detection ────────────────────────────────────────────────────────────

def detect_bands(img: np.ndarray) -> List[Dict[str, Any]]:
    """Detect the horizontal bands (594/800M/400M/200M/...) via red numerals
    on the left edge. Returns list of {y_top, y_bot, label, kind}."""
    H, W, _ = img.shape
    left = img[:, :80]
    b, g, r = cv2.split(left)
    red_mask = (r > 150) & (g < 80) & (b < 80)
    row_red = red_mask.sum(axis=1)
    strong = np.where(row_red >= RED_ROW_MIN_PIXELS)[0]

    groups: List[Tuple[int, int]] = []
    if len(strong):
        cur = [int(strong[0])]
        for y in strong[1:]:
            if int(y) - cur[-1] <= BAND_GROUP_GAP:
                cur.append(int(y))
            else:
                groups.append((cur[0], cur[-1]))
                cur = [int(y)]
        groups.append((cur[0], cur[-1]))

    bands: List[Dict[str, Any]] = []
    for i, (y0, y1) in enumerate(groups):
        y_top = y0
        y_bot = groups[i + 1][0] if i + 1 < len(groups) else H
        bands.append({"y_top": int(y_top), "y_bot": int(y_bot),
                      "label": None, "kind": None})
    return bands


def classify_band(label: str) -> str:
    lab = (label or "").upper().strip()
    if lab.endswith("M") and lab[:-1].isdigit():
        return lab  # 800M, 400M, 200M
    if lab.isdigit():
        return "start"
    return "other"


# ── Core parser ───────────────────────────────────────────────────────────────

def load_roster(dc: str, race_no: int) -> Tuple[List[Dict[str, Any]], int]:
    """Return (horses, field_size) from results JSON, falling back to racecard.

    horses is a list of dicts with keys: horse_name, horse_no (optional).
    """
    rpath = REPORTS_DIR / f"results_{dc}.json"
    if rpath.exists():
        try:
            data = json.loads(rpath.read_text(encoding="utf-8"))
            for race in data.get("races", []):
                if race.get("race_number") == race_no:
                    runners = race.get("runners", [])
                    return (
                        [{"horse_name": r.get("horse_name", ""),
                          "horse_no": r.get("horse_no", "")}
                         for r in runners if r.get("horse_name")],
                        len(runners),
                    )
        except Exception:
            pass
    # Fallback: racecard
    for cand in [BASE_DIR / "cache" / f"racecard_{dc[:4]}-{dc[4:6]}-{dc[6:]}.json"]:
        if cand.exists():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
                for race in data.get("races", []):
                    meta = race.get("meta") or {}
                    if meta.get("race_number") == race_no:
                        horses = race.get("horses", [])
                        return (
                            [{"horse_name": h.get("horse_name", ""),
                              "horse_no": h.get("horse_no", "")}
                             for h in horses if h.get("horse_name")],
                            len(horses),
                        )
            except Exception:
                pass
    return [], 0


def match_text_to_horse(text: str, roster: List[Dict[str, Any]]) -> Tuple[Optional[Dict], float]:
    best, best_score = None, 0.0
    for h in roster:
        s = _sim(text, h["horse_name"])
        if s > best_score:
            best, best_score = h, s
    return (best, best_score) if best_score >= NAME_SIM_THR else (None, best_score)


def parse_photo(
    img_path: Path,
    dc: str,
    race_no: int,
    ocr,
) -> Dict[str, Any]:
    img = cv2.imread(str(img_path))
    if img is None:
        raise RuntimeError(f"Failed to read image: {img_path}")
    H, W, _ = img.shape

    # 1. Detect bands
    bands = detect_bands(img)
    if not bands:
        bands = [{"y_top": 0, "y_bot": H, "label": None, "kind": "unknown"}]

    # 2. Load roster
    roster, field_size = load_roster(dc, race_no)

    # 3. Run OCR
    result, _ = ocr(str(img_path))
    boxes = result or []

    # 4. Classify each OCR box: is it a band label (left edge red) or a horse label?
    #    Also assign to a band by center-y.
    per_horse: Dict[str, Dict[str, Any]] = {}
    band_labels_by_idx: Dict[int, List[Tuple[float, str]]] = {i: [] for i in range(len(bands))}

    for box, text, conf_raw in boxes:
        try:
            conf = float(conf_raw)
        except Exception:
            conf = 0.5
        if conf < OCR_CONF_MIN:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cx = sum(xs) / 4.0
        cy = sum(ys) / 4.0

        # assign band
        band_idx = 0
        for bi, b in enumerate(bands):
            if b["y_top"] <= cy < b["y_bot"]:
                band_idx = bi
                break

        # Band-label detector: left-edge (x < 60), short text, numeric or "NNNM"
        norm = (text or "").upper().strip()
        if cx < 60 and len(norm) <= 5 and re.fullmatch(r"\d{2,4}M?", norm):
            band_labels_by_idx[band_idx].append((conf, norm))
            continue

        # Otherwise treat as horse label candidate
        match, score = match_text_to_horse(text, roster)
        if not match:
            continue
        key = match["horse_name"].upper()
        entry = per_horse.setdefault(key, {
            "horse_name": match["horse_name"],
            "horse_no":   match.get("horse_no", ""),
            "frames":     [],
            "ocr_texts":  [],
        })
        entry["frames"].append({
            "band_idx":   band_idx,
            "cx":         int(cx),
            "cy":         int(cy),
            "x_frac":     float(cx) / W,
            "conf":       round(conf, 3),
            "match_score": round(score, 3),
            "ocr_text":   text,
        })
        entry["ocr_texts"].append(text)

    # 5. Label bands from their collected text
    for bi, labels in band_labels_by_idx.items():
        if labels:
            labels.sort(reverse=True)  # highest conf first
            lab = labels[0][1]
            bands[bi]["label"] = lab
            bands[bi]["kind"] = classify_band(lab)

    # 6. For each frame, compute y_in_band (0 = rail/top, 1 = wide/bottom)
    for entry in per_horse.values():
        for f in entry["frames"]:
            b = bands[f["band_idx"]]
            span = max(b["y_bot"] - b["y_top"], 1)
            f["band_label"] = b["label"]
            f["y_in_band"] = round((f["cy"] - b["y_top"]) / span, 3)

        # Per-horse summary
        ys = [f["y_in_band"] for f in entry["frames"]]
        xs = [f["x_frac"]    for f in entry["frames"]]
        entry["n_frames_seen"] = len(ys)
        entry["avg_y_in_band"] = round(float(np.mean(ys)), 3) if ys else None
        entry["avg_x_frac"]    = round(float(np.mean(xs)), 3) if xs else None
        entry["wide_frames"]   = int(sum(1 for y in ys if y > WIDE_THRESHOLD))
        entry["rail_frames"]   = int(sum(1 for y in ys if y < RAIL_THRESHOLD))
        entry["wide_all_way"]  = bool(ys) and all(y > WIDE_THRESHOLD for y in ys)
        entry["rail_all_way"]  = bool(ys) and all(y < RAIL_THRESHOLD for y in ys)

    # 7. Add band y_center attribute
    for b in bands:
        b["y_center"] = int((b["y_top"] + b["y_bot"]) / 2)

    return {
        "date": f"{dc[:4]}-{dc[4:6]}-{dc[6:]}",
        "race_number": race_no,
        "img_w": W,
        "img_h": H,
        "bands": bands,
        "horses": sorted(per_horse.values(), key=lambda e: e["horse_name"]),
        "meta": {
            "ocr_boxes":       len(boxes),
            "matched_horses":  len(per_horse),
            "field_size":      field_size,
            "coverage":        round(len(per_horse) / field_size, 3) if field_size else None,
            "parsed_at":       datetime.now().isoformat(timespec="seconds"),
        },
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def iter_photos_for_date(dc: str, race_filter: Optional[int] = None) -> List[Tuple[Path, int]]:
    day_dir = PHOTOS_DIR / dc
    if not day_dir.exists():
        return []
    out = []
    for p in sorted(day_dir.glob("R*.jpg")):
        m = re.match(r"R(\d+)\.jpg$", p.name)
        if not m:
            continue
        rn = int(m.group(1))
        if race_filter is not None and rn != race_filter:
            continue
        out.append((p, rn))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--date")
    g.add_argument("--dates")
    g.add_argument("--from", dest="d_from")
    ap.add_argument("--to", dest="d_to")
    ap.add_argument("--race", type=int, default=None, help="Only this race number")
    ap.add_argument("--force", action="store_true", help="Re-parse even if JSON exists")
    args = ap.parse_args()

    if args.date:
        dates = [parse_date_arg(args.date)]
    elif args.dates:
        dates = [parse_date_arg(s.strip()) for s in args.dates.split(",") if s.strip()]
    else:
        if not args.d_to:
            ap.error("--to required with --from")
        dates = date_range(parse_date_arg(args.d_from), parse_date_arg(args.d_to))

    # Lazy-import OCR (heavy)
    from rapidocr_onnxruntime import RapidOCR
    print("Loading RapidOCR model ...")
    ocr = RapidOCR()

    total_parsed = 0
    total_skipped = 0
    total_unmatched_horses = 0
    total_matched_horses = 0
    t0 = time.time()

    for d in dates:
        dc = d.strftime("%Y%m%d")
        photos = iter_photos_for_date(dc, race_filter=args.race)
        if not photos:
            continue
        print(f"\n[{d.isoformat()}] {len(photos)} photo(s)")
        for img_path, rn in photos:
            out_path = img_path.with_suffix(".json")
            # Auto-detect stub JSONs (created when roster was empty): re-OCR them
            is_stub = False
            if out_path.exists() and not args.force:
                try:
                    _existing = json.loads(out_path.read_text(encoding="utf-8"))
                    _meta = _existing.get("meta", {}) or {}
                    if (_meta.get("field_size") or 0) == 0 or not _existing.get("horses"):
                        is_stub = True
                except Exception:
                    is_stub = True
            if out_path.exists() and not args.force and not is_stub:
                print(f"  R{rn}: skipped (exists)")
                total_skipped += 1
                continue
            if is_stub:
                print(f"  R{rn}: re-OCR (prior stub, empty roster)")
            try:
                data = parse_photo(img_path, dc, rn, ocr)
            except Exception as e:
                print(f"  R{rn}: ERROR — {e}")
                continue
            out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            meta = data["meta"]
            cov = meta["coverage"]
            cov_s = f"{cov*100:.0f}%" if cov is not None else "?"
            print(f"  R{rn}: {meta['matched_horses']}/{meta['field_size']} horses "
                  f"({cov_s}) · {meta['ocr_boxes']} OCR boxes · "
                  f"{len(data['bands'])} bands")
            total_parsed += 1
            total_matched_horses += meta["matched_horses"]
            if meta["field_size"]:
                total_unmatched_horses += max(meta["field_size"] - meta["matched_horses"], 0)

    dt = time.time() - t0
    print(f"\nDone. Parsed={total_parsed}  Skipped={total_skipped}  "
          f"Matched horses={total_matched_horses}  Unmatched={total_unmatched_horses}  "
          f"({dt:.1f}s total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
