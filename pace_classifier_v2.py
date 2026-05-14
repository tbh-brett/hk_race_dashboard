"""
pace_classifier_v2.py
=====================

Rebuilds the broken race-pace classifier (which previously predicted
"Fast" 58/59 races). Training data comes from past meetings where we
have BOTH a race_day_report (with picks → ESZ stats per horse) AND a
matching results file (with `actual_dev` = winner-vs-reference total
time deviation).

Approach
--------
  Features (per race):
    - distance
    - venue (ST/HV)
    - surface (Turf/AWT)
    - going (1-hot: Good, Good-to-Yielding, Yielding, etc.)
    - race_class numeric (1-5; 'Group 1' → 0 etc.)
    - n_runners
    - esz_mean, esz_top3_mean, esz_p75
    - n_leaders (style == "Leader"), n_pace, n_midfield, n_closer
    - early_speed_z_min  (the truly fast frontrunner ESZ)
    - draw_spread_of_pacesetters  (range of draws among top-3 ESZ)

  Target:
    - actual_dev (signed seconds; negative = fast, positive = slow).
      Used as a regression target — categorical label is derived from
      bins on `_DEV_BINS`.

  Model:
    - sklearn GradientBoostingRegressor (50 trees, depth 3).
    - Evaluated by leave-one-meeting-out CV.

  Gate:
    - Band-match accuracy on holdout must be >= MIN_BAND_ACCURACY.
    - If gate passes, model is saved to models/pace_classifier_v2.pkl.

Usage
-----
    python pace_classifier_v2.py train
    python pace_classifier_v2.py infer --date 20260517

The inference helper `predict_for_race(race_dict)` is callable from
elsewhere (post-pipeline hook).
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import math
from pathlib import Path
from typing import Iterable

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
MODELS = BASE / "models"
MODEL_PATH = MODELS / "pace_classifier_v2.pkl"

# Acceptance gate for shipping the model
MIN_BAND_ACCURACY = 0.50      # 3-class (Slow/Even/Fast) ≥50%
MIN_5CLASS_ACCURACY = 0.30    # 5-class baseline ≈ 20%

# 5-class label bins on actual_dev (seconds). Negative = faster than ref.
_DEV_BINS = [
    (-99.0, -1.00, "Very Fast"),
    (-1.00, -0.40, "Fast"),
    (-0.40,  0.20, "Even"),
    ( 0.20,  0.50, "Slow"),
    ( 0.50,  99.0, "Very Slow"),
]

# 3-class band collapse
_5_TO_3 = {
    "Very Fast": "Fast",
    "Fast":      "Fast",
    "Even":      "Even",
    "Slow":      "Slow",
    "Very Slow": "Slow",
}

_GOING_BUCKETS = ["Good", "Good-to-Firm", "Good-to-Yielding",
                  "Yielding", "Soft", "AWT"]
_VENUES = ["ST", "HV"]


def dev_to_5class(dev: float) -> str:
    for lo, hi, lbl in _DEV_BINS:
        if lo <= dev < hi:
            return lbl
    return "Even"


def dev_to_3band(dev: float) -> str:
    return _5_TO_3[dev_to_5class(dev)]


# ── Feature builder ────────────────────────────────────────────────────
def _bucket_going(g: str | None) -> str:
    if not g:
        return "Good"
    s = str(g).strip()
    # Normalise common HKJC strings
    if "AWT" in s.upper() or "ALL-WEATHER" in s.upper():
        return "AWT"
    for b in _GOING_BUCKETS:
        if s.lower().startswith(b.lower()) or b.lower() in s.lower():
            return b
    return "Good"


def _class_to_num(c) -> float:
    if c is None:
        return 4.0
    s = str(c).strip()
    if not s:
        return 4.0
    if s.lower().startswith("group"):
        # Group 1 → 0.5, Group 2 → 1.0, Group 3 → 1.5 (well above class 1)
        for tok in s.split():
            if tok.isdigit():
                return 0.5 * int(tok)
        return 0.5
    # "Class 4", or just "4"
    for tok in s.replace("Class", "").split():
        try:
            return float(tok)
        except ValueError:
            continue
    return 4.0


def _surface_flag(surface) -> int:
    if not surface:
        return 0
    s = str(surface).upper()
    return 1 if "AWT" in s or "ALL" in s else 0


def _style_counts(picks: list[dict]) -> tuple[int, int, int, int]:
    n_lead = n_pace = n_mid = n_close = 0
    for p in picks:
        st = (p.get("style") or "").strip().lower()
        if st == "leader":          n_lead += 1
        elif st == "on-pace":       n_pace += 1
        elif st == "midfield":      n_mid += 1
        elif st == "closer":        n_close += 1
    return n_lead, n_pace, n_mid, n_close


def _esz_stats(picks: list[dict]) -> dict:
    vals = []
    for p in picks:
        v = p.get("early_speed_z")
        if isinstance(v, (int, float)) and not math.isnan(float(v)):
            vals.append(float(v))
    if not vals:
        return {"esz_mean": 0.0, "esz_min": 0.0, "esz_top3_mean": 0.0, "esz_p75": 0.0}
    vals_sorted = sorted(vals)
    top3 = vals_sorted[:3]  # most negative = fastest
    return {
        "esz_mean":      sum(vals) / len(vals),
        "esz_min":       vals_sorted[0],
        "esz_top3_mean": sum(top3) / len(top3),
        "esz_p75":       vals_sorted[max(0, int(0.75 * len(vals_sorted)) - 1)],
    }


def _draw_spread_top_esz(picks: list[dict]) -> float:
    """Range of draws for the 3 horses with lowest (= fastest) ESZ."""
    candidates: list[tuple[float, int]] = []
    for p in picks:
        v = p.get("early_speed_z")
        d = p.get("draw")
        if isinstance(v, (int, float)) and isinstance(d, (int, float)):
            try:
                candidates.append((float(v), int(d)))
            except (TypeError, ValueError):
                continue
    if len(candidates) < 2:
        return 0.0
    candidates.sort(key=lambda t: t[0])
    draws = [d for _, d in candidates[:3]]
    return float(max(draws) - min(draws))


def build_features(race: dict, race_meta: dict | None = None) -> dict:
    """Build feature row for one race.

    `race` is one entry from race_day_report.races[].
    `race_meta` overrides venue/surface if provided.
    """
    picks = race.get("picks") or []
    esz = _esz_stats(picks)
    n_lead, n_pace, n_mid, n_close = _style_counts(picks)
    surface = (race.get("race_course") or "")
    if race_meta and race_meta.get("surface"):
        surface = race_meta["surface"]

    feats = {
        "distance":      float(race.get("distance") or 1400),
        "n_runners":     float(len(picks)),
        "class_num":     _class_to_num(race.get("race_class")),
        "going_bucket":  _bucket_going(race.get("going")),
        "venue":         (race_meta or {}).get("venue") or "ST",
        "is_awt":        _surface_flag(surface),
        "n_leaders":     float(n_lead),
        "n_pace":        float(n_pace),
        "n_midfield":    float(n_mid),
        "n_closer":      float(n_close),
        "draw_spread_top_esz": _draw_spread_top_esz(picks),
        **esz,
    }
    return feats


def _flatten_features(feats: dict) -> tuple[list[float], list[str]]:
    """One-hot encode going_bucket + venue, return (vector, name_list)."""
    names: list[str] = []
    vec: list[float] = []

    numeric_cols = ["distance", "n_runners", "class_num", "is_awt",
                    "n_leaders", "n_pace", "n_midfield", "n_closer",
                    "draw_spread_top_esz", "esz_mean", "esz_min",
                    "esz_top3_mean", "esz_p75"]
    for c in numeric_cols:
        names.append(c)
        vec.append(float(feats.get(c, 0.0)))

    for g in _GOING_BUCKETS:
        names.append(f"going_{g}")
        vec.append(1.0 if feats.get("going_bucket") == g else 0.0)
    for v in _VENUES:
        names.append(f"venue_{v}")
        vec.append(1.0 if feats.get("venue") == v else 0.0)

    return vec, names


# ── Training data assembly ─────────────────────────────────────────────
def _et_path(dc: str) -> Path | None:
    for tag in ("v4.4", "v3.4.8"):
        p = REPORTS / f"race_day_report_{dc}_{tag}.json"
        if p.exists():
            return p
    return None


def gather_training_rows() -> list[dict]:
    """Returns list of {features, target_dev, meta} for all (race, result)
    pairs we can join."""
    rows: list[dict] = []
    for et_path in sorted(REPORTS.glob("race_day_report_????????_v4.4.json")):
        dc = et_path.name[len("race_day_report_"):][:8]
        res_path = REPORTS / f"results_{dc}.json"
        if not res_path.exists():
            continue
        try:
            et = json.loads(et_path.read_text(encoding="utf-8"))
            res = json.loads(res_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        venue = et.get("meeting_venue") or "ST"
        et_by_rn = {int(r["race_number"]): r for r in et.get("races", [])}
        for rr in res.get("races", []):
            rn = rr.get("race_number")
            if rn is None or int(rn) not in et_by_rn:
                continue
            dev = rr.get("actual_dev")
            if dev is None:
                continue
            try:
                dev_f = float(dev)
            except (TypeError, ValueError):
                continue
            er = et_by_rn[int(rn)]
            surface = rr.get("surface") or er.get("race_course") or ""
            feats = build_features(er, race_meta={
                "venue": venue, "surface": surface})
            rows.append({
                "features":  feats,
                "target":    dev_f,
                "date":      dc,
                "race":      int(rn),
            })
    return rows


# ── Training (LOMO-CV) ─────────────────────────────────────────────────
def _train_eval(rows: list[dict]) -> dict:
    """Leave-one-meeting-out CV using GradientBoostingRegressor."""
    try:
        from sklearn.ensemble import GradientBoostingRegressor
    except ImportError:
        sys.stderr.write("ERROR: scikit-learn is required.\n")
        sys.exit(1)

    meetings = sorted({r["date"] for r in rows})
    correct_5 = 0
    correct_3 = 0
    abs_err = 0.0
    n = 0
    preds_all: list[tuple[str, int, float, float]] = []  # date, race, pred, actual

    for hold in meetings:
        train_X = []
        train_y = []
        for r in rows:
            if r["date"] == hold:
                continue
            vec, _ = _flatten_features(r["features"])
            train_X.append(vec)
            train_y.append(r["target"])
        if not train_X:
            continue
        mdl = GradientBoostingRegressor(
            n_estimators=80, max_depth=3, learning_rate=0.07,
            random_state=42)
        mdl.fit(train_X, train_y)

        for r in rows:
            if r["date"] != hold:
                continue
            vec, _ = _flatten_features(r["features"])
            pred = float(mdl.predict([vec])[0])
            actual = r["target"]
            abs_err += abs(pred - actual)
            n += 1
            if dev_to_5class(pred) == dev_to_5class(actual):
                correct_5 += 1
            if dev_to_3band(pred) == dev_to_3band(actual):
                correct_3 += 1
            preds_all.append((r["date"], r["race"], pred, actual))

    return {
        "n":            n,
        "mae":          abs_err / max(1, n),
        "acc_5class":   correct_5 / max(1, n),
        "acc_3band":    correct_3 / max(1, n),
        "preds":        preds_all,
    }


def train_and_save() -> dict:
    rows = gather_training_rows()
    print(f"Training data: {len(rows)} races across "
          f"{len({r['date'] for r in rows})} meetings")
    if len(rows) < 30:
        sys.exit(f"too few rows ({len(rows)}) — need ≥ 30")

    cv = _train_eval(rows)
    print(f"\nLOMO-CV results:")
    print(f"  MAE (actual_dev seconds)      = {cv['mae']:.3f}")
    print(f"  5-class label accuracy        = {cv['acc_5class']:.3f}  "
          f"(baseline 0.20)")
    print(f"  3-band  (Slow/Even/Fast) acc  = {cv['acc_3band']:.3f}  "
          f"(baseline 0.33, gate {MIN_BAND_ACCURACY})")

    if cv["acc_3band"] < MIN_BAND_ACCURACY:
        print(f"\n✗ Did NOT pass gate ({MIN_BAND_ACCURACY:.0%} band acc). "
              "Model will be saved but flagged 'experimental'.")
        ship = False
    elif cv["acc_5class"] < MIN_5CLASS_ACCURACY:
        print(f"\n⚠ Band gate passed but 5-class accuracy is weak "
              f"({cv['acc_5class']:.2%}). Saving but flagged 'experimental'.")
        ship = False
    else:
        print(f"\n✓ Passed all gates.")
        ship = True

    # Final model on full data
    from sklearn.ensemble import GradientBoostingRegressor
    X_all = []
    y_all = []
    for r in rows:
        vec, names = _flatten_features(r["features"])
        X_all.append(vec)
        y_all.append(r["target"])
    final_mdl = GradientBoostingRegressor(
        n_estimators=80, max_depth=3, learning_rate=0.07, random_state=42)
    final_mdl.fit(X_all, y_all)

    MODELS.mkdir(exist_ok=True)
    artifact = {
        "model":          final_mdl,
        "feature_names":  names,
        "trained_on":     {"n_rows": len(rows),
                           "n_meetings": len({r['date'] for r in rows})},
        "cv":             {k: v for k, v in cv.items() if k != "preds"},
        "ship":           ship,
        "min_band_acc":   MIN_BAND_ACCURACY,
        "version":        "v2.0",
    }
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(artifact, f)
    print(f"\nSaved to {MODEL_PATH}  (ship={ship})")
    return artifact


# ── Inference ──────────────────────────────────────────────────────────
def _load_model() -> dict | None:
    if not MODEL_PATH.exists():
        return None
    with open(MODEL_PATH, "rb") as f:
        return pickle.load(f)


def predict_for_race(race: dict, race_meta: dict | None = None) -> dict | None:
    """Run inference for one race.

    Returns:
        {"dev_pred": float, "label_5": str, "label_3": str,
         "label": str (legacy 5-class), "confidence": "experimental"|"ok"}
    """
    art = _load_model()
    if not art:
        return None
    feats = build_features(race, race_meta=race_meta)
    vec, _ = _flatten_features(feats)
    dev_pred = float(art["model"].predict([vec])[0])
    return {
        "dev_pred":    dev_pred,
        "label_5":     dev_to_5class(dev_pred),
        "label_3":     dev_to_3band(dev_pred),
        "label":       dev_to_5class(dev_pred),
        "confidence":  "ok" if art.get("ship") else "experimental",
        "version":     art.get("version", "v2.0"),
    }


def infer_for_meeting(date_compact: str, out_path: Path | None = None) -> dict | None:
    """Run inference for every race in a meeting; write to
    reports/pace_v2_{DC}.json. Returns the payload."""
    art = _load_model()
    if not art:
        print("No trained model — run `python pace_classifier_v2.py train` first.")
        return None
    et = _et_path(date_compact)
    if not et:
        print(f"No ET report for {date_compact}")
        return None
    data = json.loads(et.read_text(encoding="utf-8"))
    venue = data.get("meeting_venue") or "ST"
    rows = []
    for race in data.get("races", []):
        meta = {"venue": venue}
        rows.append({
            "race_number": race.get("race_number"),
            "distance":    race.get("distance"),
            "legacy_label": race.get("pace"),
            "legacy_score": race.get("pace_score"),
            **predict_for_race(race, race_meta=meta),
        })
    payload = {
        "date":          date_compact,
        "schema_version": 1,
        "model_version": art.get("version"),
        "confidence":    "ok" if art.get("ship") else "experimental",
        "cv_band_acc":   art.get("cv", {}).get("acc_3band"),
        "races":         rows,
    }
    if out_path is None:
        out_path = REPORTS / f"pace_v2_{date_compact}.json"
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"✓ Wrote {out_path} ({len(rows)} races, conf={payload['confidence']})")
    return payload


def infer_all() -> int:
    n = 0
    for et_path in sorted(REPORTS.glob("race_day_report_????????_v4.4.json")):
        dc = et_path.name[len("race_day_report_"):][:8]
        if infer_for_meeting(dc):
            n += 1
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["train", "infer", "infer-all"])
    ap.add_argument("--date", help="YYYYMMDD for infer mode")
    args = ap.parse_args()

    if args.mode == "train":
        train_and_save()
    elif args.mode == "infer-all":
        n = infer_all()
        print(f"\nInferred {n} meetings.")
    else:
        if not args.date:
            ap.error("Pass --date YYYYMMDD")
        infer_for_meeting(args.date)
