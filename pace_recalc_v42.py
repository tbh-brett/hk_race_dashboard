"""
v4.2 Pace Recalculation — Compare old vs new pace predictions for 3 meetings.
Also compute actual pace from DB using HKJC reference standard times.
"""
import importlib.util, sys, os, math
import pandas as pd
import numpy as np

# ── Load model ──
MODEL_PATH = os.path.join(os.path.dirname(__file__),
                          "race_day_analysis_20260329_v3.4.8.py")
spec = importlib.util.spec_from_file_location("model", MODEL_PATH)
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)

# ── Load HKJC standard lookup from model ──
get_hkjc_standard = model.get_hkjc_standard
HKJC_STANDARD_TIMES = model.HKJC_STANDARD_TIMES

# ── Load DB ──
DB_FILE = os.path.join(os.environ["TEMP"], "hkjc_results_updated.xlsx")
db_full = pd.read_excel(DB_FILE)
db_full["race_date"] = pd.to_datetime(db_full["race_date"])
print(f"DB loaded: {len(db_full)} rows")

# ── Meetings ──
meetings = [
    {"date": "2026-04-01", "venue": "ST", "going": "Good", "track_type": "All Weather Track",
     "card": os.path.join(os.path.dirname(__file__), "racecards", "racecard_20260401.xlsx")},
    {"date": "2026-04-06", "venue": "ST", "going": "Good", "track_type": "Turf",
     "card": os.path.join(os.path.dirname(__file__), "racecards", "racecard_20260406.xlsx")},
    {"date": "2026-04-08", "venue": "HV", "going": "Good", "track_type": "Turf",
     "card": os.path.join(os.path.dirname(__file__), "racecards", "racecard_20260408.xlsx")},
]

print("\n" + "=" * 100)
print("PACE RECALCULATION: v4.2 (HKJC-anchored, no venue offsets) vs Actual")
print("=" * 100)

for meeting in meetings:
    mdate = meeting["date"]
    mdate_ts = pd.Timestamp(mdate)
    venue = meeting["venue"]
    track_type = meeting["track_type"]

    # Get actual results for this date
    actuals = db_full[db_full["race_date"] == mdate_ts].copy()
    if len(actuals) == 0:
        print(f"\n{mdate}: No actual results found, skipping")
        continue

    # Filter DB for blind test (exclude this date and later)
    db = db_full[db_full["race_date"] < mdate_ts].copy()

    # Parse racecard
    card_path = meeting["card"]
    if not os.path.exists(card_path):
        print(f"\n{mdate}: Card not found at {card_path}, skipping")
        continue

    races = model.parse_race_card_v2(card_path)

    # ── Set model globals + load references ONCE per meeting ──
    model.MEETING_VENUE = venue
    model.TURF_GOING_ASSUMED = "Good"
    model.AWT_GOING_ASSUMED = "Good"
    ref_data = model.load_references_v3()
    class_fine, fine, coarse, ultra, draw_off = ref_data

    print(f"\n{'─' * 100}")
    print(f"MEETING: {mdate}  Venue={venue}  Surface={'AWT' if 'Weather' in track_type else 'Turf'}")
    print(f"{'─' * 100}")
    print(f"{'Race':>5s}  {'Dist':>5s}  {'Cls':>4s}  {'HKJC Std':>9s}  {'HKJC Early':>10s}  "
          f"{'v4.2 Pred':>10s}  {'v4.2 Label':>14s}  "
          f"{'Actual Dev':>10s}  {'Actual Label':>14s}  {'Error':>7s}")

    for race in races:
        rn = race["race_number"]
        dist = race["distance"]
        rc_class = race.get("race_class", 0) or 0
        is_awt = race["is_awt"]
        tt = "All Weather Track" if is_awt else "Turf"
        going = "Good"

        # ── HKJC reference for this race ──
        hkjc_ref = get_hkjc_standard(venue, dist, rc_class, tt)
        hkjc_std_s = f"{hkjc_ref['total']:.2f}" if hkjc_ref else "N/A"
        hkjc_early_s = f"{hkjc_ref['early']:.2f}" if hkjc_ref else "N/A"

        # ── Compute profiles for all horses (need for pace prediction) ──
        horse_data_for_pace = []
        for h in race["horses"]:
            profile = model.compute_horse_profile_runtime(h["horse_name"], db,
                                                           class_fine, fine, coarse, ultra)
            horse_data_for_pace.append({
                "horse_name": h["horse_name"],
                "leader_frac": profile["leader_frac"],
                "front_frac": profile["front_frac"],
                "early_speed_z": profile["early_speed_z"],
                "style_entropy": profile["style_entropy"],
                "dominant_style": profile["dominant_style"],
            })

        # ── v4.2 pace prediction (HKJC-anchored, no venue offsets) ──
        v42_label, v42_dev, v42_reasons, v42_leaders = model.predict_race_pace_v3(
            horse_data_for_pace, dist, going, venue=venue,
            race_class=rc_class, track_type=tt)

        # ── Compute actual pace from DB ──
        race_results = actuals[actuals["race_number"] == rn]
        actual_dev = None
        actual_label = "N/A"

        if hkjc_ref and len(race_results) > 0:
            # Get actual early sectional time from results
            # Sum of all sectionals except the last (400m-finish)
            early_times = []
            for _, rr in race_results.iterrows():
                sect_str = str(rr.get("sectiontimes", ""))
                if pd.isna(sect_str) or not sect_str.strip() or sect_str == "nan":
                    continue
                parts = [p.strip() for p in sect_str.split(";")]
                nums = []
                for p in parts:
                    try:
                        v = float(p)
                        if 5.0 < v < 40.0:
                            nums.append(v)
                    except:
                        continue
                if len(nums) >= 2:
                    # Early sections = all except last
                    early_sum = sum(nums[:-1])
                    early_times.append(early_sum)

            if early_times:
                # Use median of all horses' early section times as "race early pace"
                actual_early = np.median(early_times)
                actual_dev = actual_early - hkjc_ref["early"]

                # Classify actual
                if actual_dev >= 0.50:
                    actual_label = "Very Slow"
                elif actual_dev >= 0.35:
                    actual_label = "Slow"
                elif actual_dev >= 0.20:
                    actual_label = "Slightly Slow"
                elif actual_dev > -0.20:
                    actual_label = "Normal"
                elif actual_dev >= -0.40:
                    actual_label = "Slightly Fast"
                elif actual_dev > -1.00:
                    actual_label = "Fast"
                else:
                    actual_label = "Very Fast"

        actual_dev_s = f"{actual_dev:+.2f}s" if actual_dev is not None else "N/A"
        error = (v42_dev - actual_dev) if actual_dev is not None else None
        error_s = f"{error:+.2f}s" if error is not None else "N/A"

        cls_label = f"C{rc_class}" if rc_class > 0 else "Grp"
        print(f"  R{rn:>2d}  {dist:>5d}  {cls_label:>4s}  {hkjc_std_s:>9s}  {hkjc_early_s:>10s}  "
              f"{v42_dev:+.2f}s     {v42_label:>14s}  "
              f"{actual_dev_s:>10s}  {actual_label:>14s}  {error_s:>7s}")

print("\n" + "=" * 100)
print("SUMMARY: Compare v4.2 predicted pace labels vs actual pace labels")
print("=" * 100)
print("""
Key changes in v4.2 pace prediction:
  1. HKJC Official Reference Standard Times used as anchor
  2. Ad-hoc venue offsets REMOVED (ST -0.45s, HV 1200m -0.35s)
  3. PACE_STYLE_MULTIPLIERS REMOVED (no per-horse pace adjustment)
  4. Pace deviation = predicted early sectional - HKJC standard early sectional
  5. Form franking, risk metric, consistency flag all removed

The baseline heuristic (pace_index regression) still predicts field-level pace.
Without venue offsets, the model runs 'warmer' (less negative dev) which should
help at HV and reduce the systematic over-prediction of 'Fast' at ST Turf.
""")
