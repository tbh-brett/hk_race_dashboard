"""
rebuild_et_references.py — Regenerate expected_time_references from hkjc_results_updated.xlsx

v4 — Finer weight bands (3-lb instead of 5-lb), updated date range, all 10 sheets rebuilt.
"""
import os, sys
from pathlib import Path
import pandas as pd
import numpy as np
from scipy import stats

TEMP = Path(os.environ.get("TEMP", "."))
DB_FILE = TEMP / "hkjc_results_updated.xlsx"
OUT_FILE = TEMP / "expected_time_references_v4.xlsx"

# ── Going code mapping (must match model) ────────────────────────────────────
GOING_CODE_MAP = {
    "GF": "Good-to-Firm", "G": "Good", "GY": "Good-to-Yielding",
    "Y": "Yielding", "WF": "AWT-Wet-Fast", "WS": "AWT-Wet-Slow",
    "SE": "AWT-Standard", "SEALED": "AWT-Standard",
    "GOOD TO FIRM FAST": "Good-to-Firm",
}

# ── NEW 3-lb weight bands (8 bands vs old 5) ────────────────────────────────
def weight_band(w):
    """3-lb weight bands.  ≤112 and 131+ are wider tails."""
    if w <= 112: return "≤112"
    if w <= 115: return "113-115"
    if w <= 118: return "116-118"
    if w <= 121: return "119-121"
    if w <= 124: return "122-124"
    if w <= 127: return "125-127"
    if w <= 130: return "128-130"
    return "131+"

# ── Class bands (unchanged) ──────────────────────────────────────────────────
def class_band(c):
    if c <= 0: return "Group/Other"
    if c <= 2: return "C1-C2"
    if c == 3: return "C3"
    if c == 4: return "C4"
    return "C5"


def agg_stats(grp):
    """Compute summary statistics for a group of finish times."""
    ft = grp["finish_time_seconds"]
    return pd.Series({
        "expected_time": round(ft.median(), 3),
        "mean_time":     round(ft.mean(), 3),
        "std_time":      round(ft.std(), 3) if len(ft) >= 2 else np.nan,
        "sample_size":   len(ft),
        "q25":           round(ft.quantile(0.25), 4),
        "q75":           round(ft.quantile(0.75), 4),
        "iqr":           round(ft.quantile(0.75) - ft.quantile(0.25), 3),
    })


def build_all():
    print(f"Reading database: {DB_FILE}")
    db = pd.read_excel(DB_FILE)
    db = db[db["finish_time_seconds"].notna() & (db["finish_time_seconds"] > 0)].copy()
    print(f"  Valid runs: {len(db)}")

    # ── Derived columns ──────────────────────────────────────────────────────
    db["going_group"] = db["going"].map(lambda x: GOING_CODE_MAP.get(str(x).strip(), "Good"))
    db["weight_band"] = db["actual_weight"].apply(weight_band)
    db["class_num"]   = pd.to_numeric(db.get("race_class", pd.Series(dtype=float)),
                                       errors="coerce").fillna(0).astype(int)
    db["class_band"]  = db["class_num"].apply(class_band)

    # Track type
    is_awt = db["track_type"].str.contains("All Weather", case=False, na=False)
    db["track_label"] = "Turf"
    db.loc[is_awt, "track_label"] = "All Weather Track"

    # Course: AWT gets "AWT", turf uses race_course
    db["draw_course"] = db["race_course"]
    db.loc[is_awt, "draw_course"] = "AWT"

    date_min = db["race_date"].min()
    date_max = db["race_date"].max()
    print(f"  Date range: {date_min} to {date_max}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 1: ClassFine (distance × going × weight_band × course × class)
    # ═══════════════════════════════════════════════════════════════════════════
    print("\nBuilding ClassFine tier...")
    turf = db[~is_awt].copy()
    keys_cf = ["distance", "going_group", "weight_band", "race_course", "class_band"]
    class_fine = turf.groupby(keys_cf).apply(agg_stats, include_groups=False).reset_index()
    print(f"  {len(class_fine)} rows  (n≥2: {(class_fine['sample_size']>=2).sum()})")

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 2: Fine (distance × going × weight_band × course)
    # ═══════════════════════════════════════════════════════════════════════════
    print("Building Fine tier...")
    keys_fn = ["distance", "going_group", "weight_band", "race_course"]
    fine = turf.groupby(keys_fn).apply(agg_stats, include_groups=False).reset_index()
    # Add AWT rows to Fine as well (course = "AWT")
    awt = db[is_awt].copy()
    awt["race_course"] = "AWT"
    if len(awt):
        fine_awt = awt.groupby(keys_fn).apply(agg_stats, include_groups=False).reset_index()
        fine = pd.concat([fine, fine_awt], ignore_index=True)
    print(f"  {len(fine)} rows  (n≥5: {(fine['sample_size']>=5).sum()})")

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 3: Coarse (distance × going × weight_band × track_type)
    # ═══════════════════════════════════════════════════════════════════════════
    print("Building Coarse tier...")
    keys_co = ["distance", "going_group", "weight_band", "track_type"]
    coarse = db.groupby(keys_co).apply(agg_stats, include_groups=False).reset_index()
    print(f"  {len(coarse)} rows  (n≥5: {(coarse['sample_size']>=5).sum()})")

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 4: Ultra (distance × going × track_type)  — no weight
    # ═══════════════════════════════════════════════════════════════════════════
    print("Building Ultra tier...")
    keys_ul = ["distance", "going_group", "track_type"]
    ultra = db.groupby(keys_ul).apply(agg_stats, include_groups=False).reset_index()
    print(f"  {len(ultra)} rows  (n≥5: {(ultra['sample_size']>=5).sum()})")

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 5: Distance-only fallback
    # ═══════════════════════════════════════════════════════════════════════════
    print("Building Dist-only fallback...")
    dist_only = db.groupby(["distance", "track_type"]).apply(
        agg_stats, include_groups=False).reset_index()
    print(f"  {len(dist_only)} rows")

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 6: Draw Offsets (race-median method, per draw per dist×course)
    # ═══════════════════════════════════════════════════════════════════════════
    print("Building Draw Offsets...")
    db["draw_num"] = pd.to_numeric(db["draw"], errors="coerce")
    draw_data = db[db["draw_num"].notna()].copy()
    all_draw_rows = []
    for (dist, course), subset in draw_data.groupby(["distance", "draw_course"]):
        race_med = subset.groupby(["race_date", "race_number"])["finish_time_seconds"].median()
        subset = subset.merge(race_med.rename("race_median"),
                              on=["race_date", "race_number"])
        subset["resid"] = subset["finish_time_seconds"] - subset["race_median"]
        overall_mean = subset["resid"].mean()
        for dr_num, grp in subset.groupby("draw_num"):
            n = len(grp)
            if n >= 3:  # lowered from 5 to capture thin outer draws
                raw_off = grp["resid"].mean() - overall_mean
                all_draw_rows.append({
                    "distance": int(dist),
                    "race_course": course,
                    "draw": int(dr_num),
                    "mean_resid": round(grp["resid"].mean(), 6),
                    "n": n,
                    "overall_mean": round(overall_mean, 6),
                    "draw_offset": round(raw_off, 6),
                })
    draw_off = pd.DataFrame(all_draw_rows) if all_draw_rows else pd.DataFrame()
    turf_n = len(draw_off[draw_off["race_course"] != "AWT"]) if len(draw_off) else 0
    awt_n  = len(draw_off[draw_off["race_course"] == "AWT"]) if len(draw_off) else 0
    print(f"  {turf_n} turf + {awt_n} AWT draw entries")

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 7: Weight Coefficients (within-horse — from earlier analysis)
    # ═══════════════════════════════════════════════════════════════════════════
    print("Building Weight Coefficients (within-horse slope by distance)...")
    # --- Compute per-horse within-horse weight slopes at same distance ---
    wt_db = db[db["actual_weight"].notna() & db["draw_num"].notna()].copy()
    horse_dist_groups = wt_db.groupby(["horse_name", "distance"])
    wt_rows = []
    for (horse, dist), grp in horse_dist_groups:
        if len(grp) < 4:
            continue
        wt = grp["actual_weight"].values
        ft = grp["finish_time_seconds"].values
        if wt.std() < 1.0:
            continue
        slope, _, _, p, _ = stats.linregress(wt, ft)
        wt_rows.append({"horse": horse, "distance": dist, "slope": slope, "p": p, "n": len(grp)})

    if wt_rows:
        wt_df = pd.DataFrame(wt_rows)
        # Aggregate to distance level: median within-horse slope
        wt_summary = []
        for dist, g in wt_df.groupby("distance"):
            trk = "Turf" if dist not in [1650] else "Mixed"
            # Determine track type from most common in DB
            dist_trk = db[db["distance"] == dist]["track_label"].mode()
            trk = dist_trk.iloc[0] if len(dist_trk) else "Turf"
            wt_summary.append({
                "distance": int(dist),
                "track_type": trk,
                "within_horse_slope": round(g["slope"].median(), 6),
                "within_horse_mean":  round(g["slope"].mean(), 6),
                "pct_significant_p10": round((g["p"] < 0.10).mean() * 100, 1),
                "n_horses": len(g),
                "population_slope": np.nan,  # filled next
            })
        wt_coeff = pd.DataFrame(wt_summary)

        # Population-level slope for comparison
        for i, row in wt_coeff.iterrows():
            d_sub = db[db["distance"] == row["distance"]]
            if len(d_sub) > 20:
                sl, _, _, p, _ = stats.linregress(d_sub["actual_weight"], d_sub["finish_time_seconds"])
                wt_coeff.at[i, "population_slope"] = round(sl, 6)
    else:
        wt_coeff = pd.DataFrame(columns=["distance", "track_type", "within_horse_slope",
                                          "within_horse_mean", "pct_significant_p10",
                                          "n_horses", "population_slope"])
    print(f"  {len(wt_coeff)} distance groups")

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 8: Pace References
    # ═══════════════════════════════════════════════════════════════════════════
    print("Building Pace References...")
    # Extract first sectional from sectiontimes column (semicolon-delimited)
    def _first_sec(st):
        if pd.isna(st) or not str(st).strip():
            return np.nan
        parts = str(st).split(";")
        try:
            v = float(parts[0].strip())
            return v if v > 0 else np.nan
        except (ValueError, IndexError):
            return np.nan

    db["first_sectional"] = db["sectiontimes"].apply(_first_sec)
    has_sec = db["first_sectional"].notna() & (db["first_sectional"] > 0) & (db["finish_time_seconds"] > 0)
    pace_db = db[has_sec].copy()
    pace_db["pace_frac"] = pace_db["first_sectional"] / pace_db["finish_time_seconds"]
    pace_rows = []
    for (dist, going, cb), grp in pace_db.groupby(["distance", "going_group", "class_band"]):
        if len(grp) >= 5:
            pace_rows.append({
                "distance": int(dist),
                "going_group": going,
                "class_band": cb,
                "pace_frac_mean": round(grp["pace_frac"].mean(), 4),
                "pace_frac_std":  round(grp["pace_frac"].std(), 4),
                "n_pace": len(grp),
            })
    pace_refs = pd.DataFrame(pace_rows)
    print(f"  {len(pace_refs)} groups")

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 9: First-Sec Population
    # ═══════════════════════════════════════════════════════════════════════════
    print("Building First-Sec Population...")
    fs_rows = []
    for dist, grp in pace_db.groupby("distance"):
        fs_rows.append({
            "distance": int(dist),
            "pop_mean": round(grp["first_sectional"].mean(), 3),
            "pop_std":  round(grp["first_sectional"].std(), 3),
            "pop_n":    len(grp),
        })
    first_sec = pd.DataFrame(fs_rows)
    print(f"  {len(first_sec)} distances")

    # ═══════════════════════════════════════════════════════════════════════════
    # Sheet 10: Methodology
    # ═══════════════════════════════════════════════════════════════════════════
    methodology = pd.DataFrame({
        "Parameter": [
            "Version",
            "Data source",
            "Date range",
            "Total runs",
            "Weight bands",
            "Weight band list",
            "Class bands",
            "ET tiers",
            "Draw offset method",
            "Draw min sample",
            "Within-horse weight",
            "Enhancements (v4)",
        ],
        "Value": [
            "v4 (3-lb weight bands, within-horse weight slopes, updated)",
            "hkjc_results_updated.xlsx",
            f"{date_min} to {date_max}",
            f"{len(db):,}",
            "8 bands (3-lb core, wider tails)",
            "≤112, 113-115, 116-118, 119-121, 122-124, 125-127, 128-130, 131+",
            "Group/Other, C1-C2, C3, C4, C5",
            "ClassFine (min 2) → Fine (min 5, +class correction) → Coarse (min 5) → Ultra (min 5)",
            "Race-median residual method (removes horse-quality confound)",
            "3 (lowered from 5 to capture thin outer draws)",
            "Per-distance median within-horse slope (causal, not confounded)",
            "Finer weight bands, within-horse weight coefficients, lowered draw min-n",
        ],
    })

    # ═══════════════════════════════════════════════════════════════════════════
    # Write to Excel
    # ═══════════════════════════════════════════════════════════════════════════
    print(f"\nWriting to {OUT_FILE} ...")
    with pd.ExcelWriter(OUT_FILE, engine="openpyxl") as writer:
        class_fine.to_excel(writer, sheet_name="ClassFine (D×G×W×Crs×Cl)", index=False)
        fine.to_excel(writer, sheet_name="Fine (Dist×Going×Wt×Crs)", index=False)
        coarse.to_excel(writer, sheet_name="Coarse (Dist×Going×Wt×Trk)", index=False)
        ultra.to_excel(writer, sheet_name="Ultra (Dist×Going×Trk)", index=False)
        dist_only.to_excel(writer, sheet_name="Dist-only fallback", index=False)
        draw_off.to_excel(writer, sheet_name="Draw Offsets", index=False)
        wt_coeff.to_excel(writer, sheet_name="Weight Coefficients", index=False)
        pace_refs.to_excel(writer, sheet_name="Pace References", index=False)
        first_sec.to_excel(writer, sheet_name="First-Sec Population", index=False)
        methodology.to_excel(writer, sheet_name="Methodology", index=False)

    print("\n=== Summary comparison vs v3 ===")
    old_file = TEMP / "expected_time_references_v3.xlsx"
    if old_file.exists():
        old_cf = pd.read_excel(old_file, sheet_name=0)
        old_fn = pd.read_excel(old_file, sheet_name=1)
        old_co = pd.read_excel(old_file, sheet_name=2)
        old_ul = pd.read_excel(old_file, sheet_name=3)
        print(f"  ClassFine: {len(old_cf)} → {len(class_fine)} rows")
        print(f"  Fine:      {len(old_fn)} → {len(fine)} rows")
        print(f"  Coarse:    {len(old_co)} → {len(coarse)} rows")
        print(f"  Ultra:     {len(old_ul)} → {len(ultra)} rows")
        # Weight band comparison
        print(f"\n  Old weight bands: {sorted(old_fn['weight_band'].unique())}")
        print(f"  New weight bands: {sorted(fine['weight_band'].unique())}")
    else:
        print("  (v3 file not found for comparison)")

    # Quick quality check: sample sizes at each tier
    print(f"\n=== Cell quality ===")
    for label, df, min_n in [("ClassFine", class_fine, 2), ("Fine", fine, 5),
                              ("Coarse", coarse, 5), ("Ultra", ultra, 5)]:
        ss = df["sample_size"]
        above = (ss >= min_n).sum()
        pct = 100 * above / len(df) if len(df) else 0
        print(f"  {label:10s}: {len(df):5d} cells, {above:5d} with n≥{min_n} ({pct:.0f}%), "
              f"median n={ss.median():.0f}")

    print(f"\nDone! Saved to {OUT_FILE}")


if __name__ == "__main__":
    build_all()
