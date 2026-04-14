"""
v4.0 Diagnostic Comparison Script
Shows the specific impact of each model change on horse projections.
Compares: old-style residual vs new contextualised residual.
"""
import pandas as pd, numpy as np, os, sys, warnings
warnings.filterwarnings("ignore")
from pathlib import Path

# ── Reuse model functions ──
BASE = Path(r"c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards")
TEMP = Path(os.environ.get("TEMP", str(BASE)))

# Load data
print("Loading data...")
db = pd.read_excel(TEMP / "hkjc_results_updated.xlsx")
et_refs = pd.ExcelFile(TEMP / "expected_time_references_v3.xlsx")
class_fine = pd.read_excel(et_refs, sheet_name=0)
fine = pd.read_excel(et_refs, sheet_name=1)
coarse = pd.read_excel(et_refs, sheet_name=2)
ultra = pd.read_excel(et_refs, sheet_name=3)
print("  DB: %d rows" % len(db))

# Import key functions from model
sys.path.insert(0, str(BASE))
import importlib.util
spec = importlib.util.spec_from_file_location("model",
    str(BASE / "race_day_analysis_20260329_v3.4.8.py"))
mod = importlib.util.module_from_spec(spec)

# We can't fully import (it runs main), so re-implement minimal versions:
# --- Minimal re-implementations ---

_ET_CACHE = {}

def weight_band(w):
    if w <= 113: return "≤113"
    if w <= 117: return "114-117"
    if w <= 120: return "118-120"
    if w <= 125: return "121-125"
    if w <= 128: return "126-128"
    if w <= 133: return "129-133"
    return "≥134"

def class_band(c):
    if c <= 0: return "Group/Other"
    if c <= 2: return "C1-C2"
    if c == 3: return "C3"
    if c == 4: return "C4"
    return "C5"

def _map_going(code):
    code = str(code).strip().upper()
    if code in ("GF","FM","F","FIRM"): return "Good-to-Firm"
    if code in ("GY","YG","YIELDING"): return "Good-to-Yielding"
    if code in ("Y","YS","SY","S","SH","SOFT","HEAVY"): return "Yielding"
    return "Good"

def lookup_expected_time(dist, going, wband, course, track_type, cband):
    key = (dist, going, wband, course, track_type, cband)
    if key in _ET_CACHE:
        return _ET_CACHE[key]
    MIN_N = 5
    for df_ref, cols, tier in [
        (class_fine, {"distance": dist, "going_group": going, "weight_band": wband,
                      "race_course": course, "class_band": cband}, "class_fine"),
        (fine, {"distance": dist, "going_group": going, "weight_band": wband,
                "race_course": course}, "fine"),
        (coarse, {"distance": dist, "going_group": going, "weight_band": wband,
                  "track_type": track_type}, "coarse"),
        (ultra, {"distance": dist, "going_group": going,
                 "track_type": track_type}, "ultra"),
    ]:
        mask = pd.Series(True, index=df_ref.index)
        for col, val in cols.items():
            mask &= (df_ref[col] == val)
        matched = df_ref.loc[mask]
        if len(matched) and matched.iloc[0].get("sample_size", 0) >= MIN_N:
            r = matched.iloc[0]
            result = (r["expected_time"], int(r["sample_size"]), tier)
            _ET_CACHE[key] = result
            return result
    result = (np.nan, 0, "none")
    _ET_CACHE[key] = result
    return result

RECENCY_LAMBDA = 0.85
POSITION_CREDIT = {1: -0.15, 2: -0.08, 3: -0.04}

# Compute draw offsets (race-median method)
print("Computing draw offsets (race-median)...")
_draw_off_rows = []
for (rd, rn), grp in db.groupby(["race_date", "race_number"]):
    if len(grp) < 5: continue
    fts = grp["finish_time_seconds"].dropna()
    if len(fts) < 5: continue
    race_median = fts.median()
    for _, row in grp.iterrows():
        ft = row.get("finish_time_seconds")
        d = row.get("draw")
        dist = row.get("distance")
        if pd.isna(ft) or pd.isna(d) or pd.isna(dist): continue
        _draw_off_rows.append({
            "distance": int(dist),
            "race_course": row.get("race_course", "A"),
            "draw": int(d),
            "draw_offset": ft - race_median,
        })
draw_df = pd.DataFrame(_draw_off_rows)
draw_off = draw_df.groupby(["distance", "race_course", "draw"]).agg(
    draw_offset=("draw_offset", "mean"), n=("draw_offset", "count")).reset_index()
print("  %d draw offset entries" % len(draw_off))

_RACE_PACE_CACHE = {}
def _get_rpi(race_date, race_number):
    key = (race_date, race_number)
    if key in _RACE_PACE_CACHE:
        return _RACE_PACE_CACHE[key]
    rr = db[(db["race_date"]==race_date) & (db["race_number"]==race_number)]
    resids = []
    for _, r in rr.iterrows():
        ft = r.get("finish_time_seconds")
        dist = r.get("distance")
        if pd.isna(ft) or pd.isna(dist) or ft <= 0: continue
        et, _, _ = lookup_expected_time(int(dist), _map_going(r.get("going","G")),
            weight_band(int(r["actual_weight"]) if pd.notna(r.get("actual_weight")) and r["actual_weight"]>0 else 120),
            r.get("race_course","A"), r.get("track_type","Turf"),
            class_band(int(r["race_class"])) if pd.notna(r.get("race_class")) else "Group/Other")
        if pd.notna(et) and et > 0:
            resids.append(ft - et)
    rpi = float(np.median(resids)) if len(resids)>=3 else 0.0
    _RACE_PACE_CACHE[key] = rpi
    return rpi

# Parse first position from running_positions
def _parse_fp(rp_str):
    if pd.isna(rp_str) or not str(rp_str).strip(): return None
    try: return int(float(str(rp_str).strip().split()[0]))
    except: return None

_FS_CACHE = {}
def _field_size(rd, rn):
    key = (rd, rn)
    if key not in _FS_CACHE:
        _FS_CACHE[key] = int(((db["race_date"]==rd) & (db["race_number"]==rn)).sum())
    return _FS_CACHE[key]

def _get_draw_offset(dist, course, draw_num):
    if pd.isna(draw_num): return 0.0
    m = ((draw_off["distance"]==dist)&(draw_off["race_course"]==course)&(draw_off["draw"]==int(draw_num)))
    rows = draw_off.loc[m]
    return float(rows.iloc[0]["draw_offset"]) if len(rows) else 0.0

WIDE_COST_OUTER_FRAC = 0.65
WIDE_COST_LEADER_PEN = 0.08
WIDE_COST_ONPACE_PEN = 0.05
WIDE_COST_MIDFIELD_PEN = 0.03
COURSE_WIDTH = {"A": 0.6, "A+3": 0.75, "B": 0.85, "B+2": 1.0, "C": 1.15, "C+3": 1.30, "AWT": 0.7}

def _wide_cost(draw_num, field_size, first_call, course):
    if draw_num is None or field_size <= 0 or first_call is None: return 0.0
    pos_ratio = int(draw_num) / field_size
    if pos_ratio <= WIDE_COST_OUTER_FRAC: return 0.0
    cf = COURSE_WIDTH.get(str(course), 0.85)
    pfrac = first_call / field_size
    if pfrac <= 0.20: base = WIDE_COST_LEADER_PEN
    elif pfrac <= 0.40: base = WIDE_COST_ONPACE_PEN
    elif pfrac <= 0.60: base = WIDE_COST_MIDFIELD_PEN
    else: base = 0.0
    excess = pos_ratio - WIDE_COST_OUTER_FRAC
    severity = min(1.0, excess / 0.35)
    return round(base * cf * severity, 4)

# ── Race quality (vectorised) ──
print("Computing race quality scores...")
# Pre-compute ET + residual for every row (vectorised where possible)
_valid = db["finish_time_seconds"].notna() & (db["finish_time_seconds"]>0) & db["distance"].notna()
_db_v = db.loc[_valid].copy()
_db_v["_going_g"] = _db_v["going"].fillna("G").apply(_map_going)
_db_v["_wband"] = _db_v["actual_weight"].apply(lambda w: weight_band(int(w)) if pd.notna(w) and w > 0 else "121-125")
_db_v["_cband"] = _db_v["race_class"].apply(lambda c: class_band(int(c)) if pd.notna(c) else "Group/Other")
_db_v["_dist_i"] = _db_v["distance"].astype(int)
_db_v["_rc"] = _db_v["race_course"].fillna("A")
_db_v["_tt"] = _db_v["track_type"].fillna("Turf")

# Build ET lookup keys and batch lookup
_et_keys = list(zip(_db_v["_dist_i"], _db_v["_going_g"], _db_v["_wband"],
                     _db_v["_rc"], _db_v["_tt"], _db_v["_cband"]))
_ets = [lookup_expected_time(*k)[0] for k in _et_keys]
_db_v["_et"] = _ets
_db_v["_resid"] = np.where(pd.notna(_db_v["_et"]) & (_db_v["_et"]>0),
                           _db_v["finish_time_seconds"] - _db_v["_et"], np.nan)
_db_v["_place_i"] = pd.to_numeric(_db_v["place"], errors="coerce").fillna(99).astype(int)

# Build horse_runs dict from vectorised data
horse_runs = {}
for hn, grp in _db_v.sort_values("race_date").groupby("horse_name"):
    horse_runs[hn] = list(grp[["race_date","race_number","_place_i","_resid"]].rename(
        columns={"race_date":"date","race_number":"rn","_place_i":"place","_resid":"resid"}).to_dict("records"))

# Compute RQ per race
RQ_CACHE = {}
for (rd, rn), grp in _db_v.groupby(["race_date","race_number"]):
    validated = total = 0
    for hn in grp["horse_name"].dropna().unique():
        rl = horse_runs.get(hn)
        if not rl: continue
        idx = next((i for i, r in enumerate(rl) if r["date"]==rd and r["rn"]==rn), None)
        if idx is None or idx+1 >= len(rl): continue
        if pd.isna(rl[idx]["resid"]) or pd.isna(rl[idx+1]["resid"]): continue
        total += 1
        if rl[idx+1]["place"] <= 3 or rl[idx+1]["resid"] < rl[idx]["resid"]:
            validated += 1
    if total >= 3:
        RQ_CACHE[(rd,rn)] = round(0.70 + (validated/total)*0.60, 3)
    else:
        RQ_CACHE[(rd,rn)] = 1.0
print("  %d races scored, avg=%.3f" % (len(RQ_CACHE), np.mean(list(RQ_CACHE.values()))))

# ── Asymmetric distance params ──
DIST_STEP_DOWN_NEAR, DIST_STEP_DOWN_MED, DIST_STEP_DOWN_FAR = 0.75, 0.45, 0.20
DIST_STEP_UP_NEAR, DIST_STEP_UP_MED, DIST_STEP_UP_FAR = 0.60, 0.30, 0.10
# Old symmetric
DIST_RECENCY_NEAR, DIST_RECENCY_MED, DIST_RECENCY_FAR = 0.70, 0.40, 0.15

# ══════════════════════════════════════════════════════════════════════════════
#  COMPARISON: Compute residual with OLD vs NEW method for selected horses
# ══════════════════════════════════════════════════════════════════════════════

test_horses = [
    ("SHOTGUN", 1400, "ST"),
    ("SMART GOLF", 1200, "ST"),
    ("POSITIVE SMILE", 1400, "ST"),
    ("ENDUED", 1600, "ST"),
    ("GALACTIC VOYAGE", 1200, "ST"),
    ("ELITE GOLF", 1200, "ST"),
    ("ISLAND GOLDEN", 1400, "ST"),
    ("COOL BLUE", 1600, "ST"),
]

print("\n" + "="*110)
print("v4.0 CHANGE IMPACT ANALYSIS")
print("="*110)

def safe_place(v):
    try: return int(float(str(v)))
    except: return 99

for horse, today_dist, today_venue in test_horses:
    runs = db[db["horse_name"]==horse].sort_values("race_date").reset_index(drop=True)
    if len(runs) == 0: continue
    
    # Compute per-run residuals both ways
    old_resids, new_resids = [], []
    old_weights, new_weights = [], []
    draw_adjs, wide_adjs, rq_weights = [], [], []
    run_descs = []
    
    n_valid = 0
    for _, run in runs.iterrows():
        ft = run.get("finish_time_seconds")
        dist = run.get("distance")
        if pd.isna(ft) or pd.isna(dist) or ft <= 0: continue
        dist_i = int(dist)
        go = _map_going(run.get("going","G"))
        wt = run.get("actual_weight")
        wb = weight_band(int(wt)) if pd.notna(wt) and wt > 0 else "121-125"
        rc = run.get("race_course","A")
        tt = run.get("track_type","Turf")
        cb = class_band(int(run.get("race_class",0))) if pd.notna(run.get("race_class")) else "Group/Other"
        et, _, _ = lookup_expected_time(dist_i, go, wb, rc, tt, cb)
        if pd.isna(et) or et <= 0: continue
        
        raw = ft - et
        rpi = _get_rpi(run["race_date"], run["race_number"])
        place = safe_place(run.get("place", 99))
        pcred = POSITION_CREDIT.get(place, 0.0)
        
        # OLD residual (v3.4.8): raw - rpi + pcred
        old_r = raw - rpi + pcred
        
        # NEW residual (v4.0): additionally subtract draw context + wide cost
        draw_num = run.get("draw")
        dctx = _get_draw_offset(dist_i, rc, draw_num) if pd.notna(draw_num) else 0.0
        fp = _parse_fp(run.get("running_positions",""))
        fs = _field_size(run["race_date"], run["race_number"])
        wc = _wide_cost(draw_num, fs, fp, rc) if fp is not None and fs > 0 else 0.0
        new_r = old_r - dctx - wc
        
        old_resids.append(old_r)
        new_resids.append(new_r)
        draw_adjs.append(dctx)
        wide_adjs.append(wc)
        
        # Race quality weight for this run
        rq = RQ_CACHE.get((run["race_date"], run["race_number"]), 1.0)
        rq_weights.append(rq)
        
        # Distance weighting
        n_valid += 1
        run_descs.append("%dm P%d D%s" % (dist_i, place, 
            str(int(draw_num)) if pd.notna(draw_num) else "?"))
    
    nv = len(old_resids)
    if nv == 0: continue
    
    # Recency weights (same for old and new)
    base_w = np.array([RECENCY_LAMBDA ** (nv-1-i) for i in range(nv)])
    
    # OLD distance-aware weights (symmetric)
    old_dw = base_w.copy()
    for i, (r, rq) in enumerate(zip(old_resids, rq_weights)):
        # In old model, distance was symmetric & no RQ weighting
        d = int(runs.iloc[i].get("distance", today_dist)) if i < len(runs) else today_dist
        # find actual distance for this valid run... use run_descs
        pass
    
    # Simple: compute weighted mean both ways
    # OLD: symmetric distance, no RQ
    w_old = base_w.copy()
    w_new = base_w.copy()
    
    for i in range(nv):
        desc = run_descs[i]
        d_i = int(desc.split("m")[0])
        diff = abs(d_i - today_dist)
        direction = d_i - today_dist
        
        # OLD symmetric
        if diff == 0: old_dm = 1.0
        elif diff <= 100: old_dm = DIST_RECENCY_NEAR
        elif diff <= 200: old_dm = DIST_RECENCY_MED
        else: old_dm = DIST_RECENCY_FAR
        w_old[i] *= old_dm
        
        # NEW asymmetric
        if diff == 0: new_dm = 1.0
        elif direction > 0:  # stepping DOWN
            if diff <= 100: new_dm = DIST_STEP_DOWN_NEAR
            elif diff <= 200: new_dm = DIST_STEP_DOWN_MED
            else: new_dm = DIST_STEP_DOWN_FAR
        else:  # stepping UP
            if diff <= 100: new_dm = DIST_STEP_UP_NEAR
            elif diff <= 200: new_dm = DIST_STEP_UP_MED
            else: new_dm = DIST_STEP_UP_FAR
        
        # NEW also applies race quality weight
        w_new[i] *= new_dm * rq_weights[i]
    
    # Normalize
    w_old /= w_old.sum()
    w_new /= w_new.sum()
    
    old_result = float(np.dot(old_resids, w_old))
    new_result = float(np.dot(new_resids, w_new))
    delta = new_result - old_result
    
    # Decompose: what caused the change?
    # Component 1: context adjustment only (using old weights, new resids)
    wt_ctx = base_w.copy()
    for i in range(nv):
        desc = run_descs[i]
        d_i = int(desc.split("m")[0])
        diff = abs(d_i - today_dist)
        if diff == 0: dm = 1.0
        elif diff <= 100: dm = DIST_RECENCY_NEAR
        elif diff <= 200: dm = DIST_RECENCY_MED
        else: dm = DIST_RECENCY_FAR
        wt_ctx[i] *= dm
    wt_ctx /= wt_ctx.sum()
    ctx_only = float(np.dot(new_resids, wt_ctx))
    ctx_effect = ctx_only - old_result
    
    # Component 2: asymmetric distance effect (using new resids, asym weights, no RQ)
    w_asym_only = base_w.copy()
    for i in range(nv):
        desc = run_descs[i]
        d_i = int(desc.split("m")[0])
        diff = abs(d_i - today_dist)
        direction = d_i - today_dist
        if diff == 0: dm = 1.0
        elif direction > 0:
            if diff <= 100: dm = DIST_STEP_DOWN_NEAR
            elif diff <= 200: dm = DIST_STEP_DOWN_MED
            else: dm = DIST_STEP_DOWN_FAR
        else:
            if diff <= 100: dm = DIST_STEP_UP_NEAR
            elif diff <= 200: dm = DIST_STEP_UP_MED
            else: dm = DIST_STEP_UP_FAR
        w_asym_only[i] *= dm
    w_asym_only /= w_asym_only.sum()
    asym_only = float(np.dot(new_resids, w_asym_only))
    asym_effect = asym_only - ctx_only
    
    # Component 3: race quality effect
    rq_effect = new_result - asym_only
    
    total_draw_adj = sum(abs(x) for x in draw_adjs)
    total_wide_adj = sum(abs(x) for x in wide_adjs)
    mean_rq = np.mean(rq_weights)
    
    print("\n" + "-"*110)
    print("%-22s %dm  (%d valid runs)" % (horse, today_dist, nv))
    print("-"*110)
    print("  OLD residual (v3.4.8): %+.4fs" % old_result)
    print("  NEW residual (v4.0):   %+.4fs  (delta: %+.4fs)" % (new_result, delta))
    print("  Decomposition:")
    print("    Change 2 (contextualise):   %+.4fs  (draw adj sum=%.3fs, wide cost sum=%.3fs)" % (
        ctx_effect, total_draw_adj, total_wide_adj))
    print("    Change 3 (asym distance):   %+.4fs" % asym_effect)
    print("    Change 4 (race quality):    %+.4fs  (mean RQ weight=%.3f)" % (rq_effect, mean_rq))
    
    # Show per-run details
    print("  Per-run breakdown:")
    for i in range(min(nv, 8)):
        j = nv - 1 - i  # most recent first
        print("    [-%d] %s  old_r=%+.3f  new_r=%+.3f  draw_adj=%+.3f  wide=%.3f  RQ=%.2f" % (
            i+1, run_descs[j], old_resids[j], new_resids[j],
            draw_adjs[j], wide_adjs[j], rq_weights[j]))

print("\n" + "="*110)
print("KEY INSIGHTS:")
print("="*110)
print("- Negative delta = horse projected FASTER (better) under v4.0")
print("- Positive delta = horse projected SLOWER (worse) under v4.0")
print("- Context adjustment subtracts draw luck → reveals TRUE ability")
print("- Asym distance: stepping UP penalised more than stepping DOWN")
print("- Race quality > 1.0 = strong form race; < 1.0 = weak form race")
