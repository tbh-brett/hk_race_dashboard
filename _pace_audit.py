"""Pace audit for Apr 12, Apr 15, Apr 19 — actual vs HKJC standard vs model prediction."""
import json
import re
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).parent
REPORTS = BASE / "reports"

HKJC_STD = {
    ("ST_Turf", 1000, 0): 55.90, ("ST_Turf", 1000, 2): 56.05, ("ST_Turf", 1000, 3): 56.45,
    ("ST_Turf", 1000, 4): 56.65, ("ST_Turf", 1000, 5): 57.00,
    ("ST_Turf", 1200, 0): 68.15, ("ST_Turf", 1200, 1): 68.45, ("ST_Turf", 1200, 2): 68.65,
    ("ST_Turf", 1200, 3): 69.00, ("ST_Turf", 1200, 4): 69.35, ("ST_Turf", 1200, 5): 69.55,
    ("ST_Turf", 1400, 0): 81.10, ("ST_Turf", 1400, 1): 81.25, ("ST_Turf", 1400, 2): 81.45,
    ("ST_Turf", 1400, 3): 81.65, ("ST_Turf", 1400, 4): 82.00, ("ST_Turf", 1400, 5): 82.30,
    ("ST_Turf", 1600, 0): 93.90, ("ST_Turf", 1600, 1): 94.05, ("ST_Turf", 1600, 2): 94.25,
    ("ST_Turf", 1600, 3): 94.70, ("ST_Turf", 1600, 4): 94.90, ("ST_Turf", 1600, 5): 95.45,
    ("ST_Turf", 1800, 0): 107.10, ("ST_Turf", 1800, 2): 107.30, ("ST_Turf", 1800, 3): 107.50,
    ("ST_Turf", 1800, 4): 107.85, ("ST_Turf", 1800, 5): 108.45,
    ("ST_Turf", 2000, 0): 120.50, ("ST_Turf", 2000, 1): 121.20, ("ST_Turf", 2000, 2): 121.70,
    ("ST_Turf", 2000, 3): 121.90, ("ST_Turf", 2000, 4): 122.35, ("ST_Turf", 2000, 5): 122.65,
    ("ST_Turf", 2400, 0): 147.00,
    ("HV_Turf", 1000, 2): 56.40, ("HV_Turf", 1000, 3): 56.65, ("HV_Turf", 1000, 4): 57.20,
    ("HV_Turf", 1000, 5): 57.35,
    ("HV_Turf", 1200, 1): 69.10, ("HV_Turf", 1200, 2): 69.30, ("HV_Turf", 1200, 3): 69.60,
    ("HV_Turf", 1200, 4): 69.90, ("HV_Turf", 1200, 5): 70.10,
    ("HV_Turf", 1650, 1): 99.10, ("HV_Turf", 1650, 2): 99.30, ("HV_Turf", 1650, 3): 99.90,
    ("HV_Turf", 1650, 4): 100.10, ("HV_Turf", 1650, 5): 100.30,
    ("HV_Turf", 1800, 0): 108.95, ("HV_Turf", 1800, 2): 109.15, ("HV_Turf", 1800, 3): 109.45,
    ("HV_Turf", 1800, 4): 109.65, ("HV_Turf", 1800, 5): 109.95,
    ("HV_Turf", 2200, 3): 136.60, ("HV_Turf", 2200, 4): 137.05, ("HV_Turf", 2200, 5): 137.35,
    ("ST_AWT", 1200, 2): 68.35, ("ST_AWT", 1200, 3): 68.55, ("ST_AWT", 1200, 4): 68.95,
    ("ST_AWT", 1200, 5): 69.35,
    ("ST_AWT", 1650, 1): 97.80, ("ST_AWT", 1650, 2): 98.40, ("ST_AWT", 1650, 3): 98.60,
    ("ST_AWT", 1650, 4): 99.05, ("ST_AWT", 1650, 5): 99.45,
    ("ST_AWT", 1800, 3): 108.05, ("ST_AWT", 1800, 4): 108.55, ("ST_AWT", 1800, 5): 109.45,
}

# HKJC results JSON uses abbreviated going codes — normalise here.
GOING_NORMALISE = {
    "G": "Good", "GF": "Good to Firm", "GY": "Good to Yielding",
    "Y": "Yielding", "YS": "Yielding to Soft", "S": "Soft",
    "SH": "Soft to Heavy", "H": "Heavy", "F": "Firm",
    "WS": "Wet Slow", "WF": "Wet Fast",
    "FAST": "AWT Fast", "GOOD": "AWT Good", "SLOW": "AWT Slow",
}
GOING_OFFSET = {
    "Good": 0.0, "Good to Firm": -0.166, "Good to Yielding": 0.199,
    "Yielding": 0.30, "Yielding to Soft": 0.38, "Soft": 0.45, "Heavy": 0.60,
    "Firm": -0.25,
    "AWT Fast": -0.10, "AWT Good": 0.0, "AWT Slow": 0.20,
}

def std_lookup(venue, distance, cls, surface):
    if surface == "AWT":
        vs = "ST_AWT"
    elif venue == "HV":
        vs = "HV_Turf"
    else:
        vs = "ST_Turf"
    for d in (0, 1, -1, 2, -2):
        s = HKJC_STD.get((vs, distance, cls + d))
        if s:
            return s, vs, cls + d
    return None, vs, None

def parse_finish_time(s):
    if not s: return None
    s = str(s).strip()
    m = re.match(r"^(\d+):(\d+\.?\d*)$", s)
    if m: return int(m.group(1)) * 60 + float(m.group(2))
    try: return float(s)
    except ValueError: return None

def classify(dev):
    if dev >= 0.50: return "Very Slow"
    if dev >= 0.35: return "Slow"
    if dev >= 0.20: return "Slightly Slow"
    if dev > -0.20: return "Normal"
    if dev >= -0.40: return "Slightly Fast"
    if dev > -1.00: return "Fast"
    return "Very Fast"

def running_style_from_positions(positions):
    if not positions: return "Unknown"
    first = positions[0]
    if first <= 2: return "Leader"
    if first <= 4: return "On-Pace"
    if first <= 7: return "Midfield"
    return "Closer"

def analyze_meeting(date_iso):
    date_compact = date_iso.replace("-", "")
    p = REPORTS / f"results_{date_compact}.json"
    if not p.exists():
        print(f"Missing results for {date_iso}")
        return []
    results = json.loads(p.read_text(encoding="utf-8"))
    venue = results.get("venue", "ST")
    out = []
    for race in results.get("races", []):
        rn = int(race.get("race_number") or 0)
        dist = int(race.get("distance") or 0)
        cls_raw = race.get("race_class") or ""
        going_raw = race.get("going") or "G"
        going = GOING_NORMALISE.get(str(going_raw).upper(), str(going_raw))
        is_awt = bool(race.get("is_awt"))
        surface = "AWT" if is_awt else "Turf"
        mcls = re.search(r"\d+", str(cls_raw))
        cls = int(mcls.group()) if mcls else 0
        std, vs_key, used_cls = std_lookup(venue, dist, cls, surface)
        winner_time = winner_name = winner_pos = None
        runners_by_pos = {}
        for row in race.get("runners", []):
            try: fp = int(row.get("place") or 0)
            except Exception: fp = 0
            runners_by_pos[fp] = row
            if fp == 1:
                winner_time = row.get("finish_time_seconds") or parse_finish_time(row.get("finish_time"))
                winner_name = row.get("horse_name")
                rp = row.get("running_position") or ""
                winner_pos = [int(x) for x in re.findall(r"\d+", rp)]
        offset = GOING_OFFSET.get(going, 0.0)
        std_adj = (std + offset) if std else None
        dev = (winner_time - std_adj) if (winner_time and std_adj) else None
        label = classify(dev) if dev is not None else "N/A"
        winner_style = running_style_from_positions(winner_pos) if winner_pos else "N/A"
        out.append({
            "date": date_iso, "race": rn, "dist": dist, "cls": cls, "going": going,
            "surface": surface, "venue": venue,
            "std": std, "std_cls_used": used_cls, "offset": offset,
            "std_adj": round(std_adj, 2) if std_adj else None,
            "winner": winner_name, "winner_time": winner_time,
            "dev": round(dev, 2) if dev is not None else None,
            "label": label, "winner_style": winner_style,
            "winner_positions": winner_pos,
        })
    return out

def load_model_predictions(date_iso):
    date_compact = date_iso.replace("-", "")
    for suffix in ("v4.4", "v4.2"):
        p = REPORTS / f"race_day_report_{date_compact}_{suffix}.json"
        if p.exists():
            rep = json.loads(p.read_text(encoding="utf-8"))
            return {int(r.get("race_number") or 0): {
                "pace": r.get("pace"), "pace_score": r.get("pace_score"),
                "pace_leaders": r.get("pace_leaders", [])} for r in rep.get("races", [])}, suffix
    return {}, None

def main():
    all_rows = []
    for date in ("2026-04-12","2026-04-15","2026-04-19"):
        rows = analyze_meeting(date)
        preds, ver = load_model_predictions(date)
        print(f"\n════════ {date} ({len(rows)} races, model={ver}) ════════")
        header = f"{'R':>3} {'D':>4} {'C':>2} {'Going':<18} {'Std':>6} {'Adj':>6} {'Actual':>7} {'Dev':>6} {'Actual_lbl':<14} {'Pred_lbl':<14} {'WStyle':<8}"
        print(header)
        for r in rows:
            p = preds.get(r["race"], {})
            pred_lbl = p.get("pace","?"); pred_sc = p.get("pace_score")
            std_s = f"{r['std']:.2f}" if r["std"] else "n/a"
            adj_s = f"{r['std_adj']:.2f}" if r["std_adj"] else "n/a"
            wt_s  = f"{r['winner_time']:.2f}" if r['winner_time'] else "n/a"
            dev_s = f"{r['dev']:+.2f}" if r['dev'] is not None else "n/a"
            pred_str = f"{pred_lbl}" + (f"({pred_sc:+.2f})" if isinstance(pred_sc,(int,float)) else "")
            print(f"R{r['race']:>2} {r['dist']:>4} {r['cls']:>2} {r['going']:<18} {std_s:>6} {adj_s:>6} {wt_s:>7} {dev_s:>6} {r['label']:<14} {pred_str:<14} {r['winner_style']:<8}")
            r["pred_lbl"] = pred_lbl; r["pred_score"] = pred_sc
            all_rows.append(r)

    n = len([r for r in all_rows if r["dev"] is not None and r.get("pred_lbl")]) or 1
    ORDER = ["Very Fast","Fast","Slightly Fast","Normal","Slightly Slow","Slow","Very Slow"]
    IDX = {l:i for i,l in enumerate(ORDER)}
    exact = sum(1 for r in all_rows if r["label"] == r.get("pred_lbl"))
    within1 = sum(1 for r in all_rows if r["label"] in IDX and r.get("pred_lbl") in IDX
                  and abs(IDX[r["label"]]-IDX[r["pred_lbl"]])<=1)
    print(f"\n──── PACE PREDICTION ACCURACY (N={n}) ────")
    print(f"  Exact label match : {exact}/{n} ({100*exact/n:.0f}%)")
    print(f"  Within-1 band     : {within1}/{n} ({100*within1/n:.0f}%)")
    actuals = [r["dev"] for r in all_rows if r["dev"] is not None]
    preds   = [r["pred_score"] for r in all_rows if isinstance(r.get("pred_score"),(int,float))]
    if actuals:
        print(f"  Mean ACTUAL dev   : {sum(actuals)/len(actuals):+.3f}s (min={min(actuals):+.2f}, max={max(actuals):+.2f})")
    if preds:
        print(f"  Mean PRED score   : {sum(preds)/len(preds):+.3f}s (min={min(preds):+.2f}, max={max(preds):+.2f})")

    # Regression-style correlation pred_score vs actual dev
    pairs = [(r["pred_score"], r["dev"]) for r in all_rows
             if isinstance(r.get("pred_score"),(int,float)) and r["dev"] is not None]
    if len(pairs) >= 3:
        mx = sum(p[0] for p in pairs)/len(pairs)
        my = sum(p[1] for p in pairs)/len(pairs)
        num = sum((p[0]-mx)*(p[1]-my) for p in pairs)
        dx = sum((p[0]-mx)**2 for p in pairs); dy = sum((p[1]-my)**2 for p in pairs)
        if dx>0 and dy>0:
            r_pearson = num/((dx*dy)**0.5)
            print(f"  Pearson(pred,actual): {r_pearson:+.3f}  (positive = calibrated)")

    style_counts = defaultdict(int)
    for r in all_rows: style_counts[r["winner_style"]] += 1
    print(f"\n──── WINNER RUNNING STYLE (N={len(all_rows)}) ────")
    for s in ("Leader","On-Pace","Midfield","Closer","Unknown","N/A"):
        c = style_counts.get(s,0)
        print(f"  {s:<10}: {c:>2}  ({100*c/len(all_rows):.0f}%)")

    print(f"\n──── Winner style × ACTUAL pace label ────")
    cross = defaultdict(lambda: defaultdict(int))
    for r in all_rows: cross[r["label"]][r["winner_style"]] += 1
    for lbl in ORDER:
        row = cross.get(lbl)
        if not row: continue
        print(f"  {lbl:<14}: " + ", ".join(f"{k}={v}" for k,v in row.items()))

    # Going × actual label
    print(f"\n──── Going × ACTUAL pace label ────")
    gcross = defaultdict(lambda: defaultdict(int))
    for r in all_rows: gcross[r["going"]][r["label"]] += 1
    for g, m in gcross.items():
        print(f"  {g:<18}: " + ", ".join(f"{k}={v}" for k,v in m.items()))

if __name__ == "__main__":
    main()
