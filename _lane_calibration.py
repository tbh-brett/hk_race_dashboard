"""Compare manual Apr 15 lane observations vs current OCR x_frac → find bias."""
from __future__ import annotations
import json, re
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).parent
DATE = "20260415"
RP = BASE / "running_position_photos" / DATE

# Manual observations parsed from user's note.
# Band keys: "800M", "400M", "200M".  Bucket labels verbatim.
MANUAL = {
    1: {"ALWAYS MY FOLKS": {"800M":"2-wide","400M":"2-wide","200M":"2-wide"}},
    2: {"POWER SUMMIT":   {"800M":"Rail","400M":"Rail","200M":"Rail"},
        "PERFECTO MOMENTS":{"800M":"2-wide","400M":"3-wide","200M":"3-wide"},
        "COURIER MAGIC":  {"800M":"Rail","400M":"Rail","200M":"Rail"}},
    3: {"FRANKTANCK":     {"800M":"2-wide","400M":"2-wide","200M":"2-wide"},
        "TAKE ACTION":    {"800M":"Rail","400M":"Rail","200M":"2-wide"}},
    4: {"CAN'T GO WONG":  {"800M":"Rail","400M":"2-wide","200M":"2-wide"},
        "ROMANTIC FANTASY":{"800M":"2-wide","400M":"2-wide","200M":"3-wide"}},
    5: {"BEAUTY ALLIANCE":{"800M":"2-wide","400M":"2-wide","200M":"2-wide"},
        "WROTE A NEW PAGE":{"800M":"Rail","400M":"Rail","200M":"Rail"},
        "KING LOTUS":     {"800M":"Rail","400M":"Rail","200M":"Rail"},
        "CALIFORNIA MOXIE":{"800M":"2-wide","400M":"2-wide","200M":"3-wide"},
        "LE ZONDA":       {"800M":"3-wide","400M":"3-wide","200M":"3-wide"}},
    6: {"FLYING CHRISTIE":{"800M":"2-wide","400M":"2-wide","200M":"3-wide"},
        "QUARTZ LEGEND":  {"800M":"Rail","400M":"Rail","200M":"2-wide"},
        "MOTOR":          {"800M":"2-wide","400M":"2-wide","200M":"3-wide"}},
    7: {"GREAT LOOKING":  {"800M":"Rail","400M":"Rail","200M":"Rail"},
        "WORLD HERO":     {"800M":"2-wide","400M":"2-wide","200M":"2-wide"},
        "ARGENTO OCEAN":  {"800M":"2-wide","400M":"3-wide","200M":"3-wide"},
        "THE HEIR":       {"800M":"Rail","400M":"Rail","200M":"2-wide"},
        "FATAL BLOW":     {"800M":"3-wide","400M":"4+-wide","200M":"4+-wide"},
        "SUPERB BOY":     {"800M":"3-wide","400M":"4+-wide","200M":"4+-wide"},
        "RAINBOW SEVEN":  {"800M":"Rail","400M":"Rail","200M":"Rail"}},
    8: {"ALL ROUND WINNER":{"800M":"Rail","400M":"2-wide","200M":"3-wide"},
        "TELECOM FIGHTERS":{"800M":"Rail","400M":"Rail","200M":"Rail"},
        "ROMANTIC GLADIATOR":{"800M":"2-wide","400M":"3-wide","200M":"3-wide"},
        "KEEFY":          {"800M":"2-wide","400M":"3-wide","200M":"3-wide"},
        "ARMOR GOLDEN EAGLE":{"800M":"Rail","400M":"Rail","200M":"Rail"},
        "I CAN":          {"800M":"Rail","400M":"Rail","200M":"2-wide"},
        "JUMBO LEGEND":   {"800M":"3-wide","400M":"3-wide","200M":"4+-wide"},
        "BOOM BOX":       {"800M":"2-wide","400M":"2-wide","200M":"3-wide"},
        "FIVEFORTWO":     {"800M":"2-wide","400M":"3-wide","200M":"4+-wide"}},
    9: {"DO YOU JUST":    {"800M":"2-wide","400M":"2-wide","200M":"2-wide"},
        "LUCKY PLANET":   {"800M":"Rail","400M":"Rail","200M":"Rail"},
        "SYMBOL OF STRENGTH":{"800M":"Rail","400M":"Rail","200M":"2-wide"},
        "STRAIGHT TO GLORY":{"800M":"2-wide","400M":"2-wide","200M":"2-wide"},
        "PRESTIGE WIN":   {"800M":"Rail","400M":"Rail","200M":"Rail"}},
}

def norm(s): return re.sub(r"[^A-Z0-9]+","",str(s).upper())

def main():
    # Collect (manual_bucket, x_frac) pairs per band
    pairs = []  # (band, manual_bucket, x_frac, race, horse)
    for rn, horses_manual in MANUAL.items():
        jp = RP / f"R{rn}.json"
        if not jp.exists():
            continue
        data = json.loads(jp.read_text(encoding="utf-8"))
        # Build band_idx → band_label map
        band_label_by_idx = {i: b["label"] for i, b in enumerate(data.get("bands", []))}
        by_name = {norm(h["horse_name"]): h for h in data["horses"]}
        for hname, manual_bands in horses_manual.items():
            h = by_name.get(norm(hname))
            if not h:
                print(f"[R{rn}] MISS horse in JSON: {hname}")
                continue
            # Per-frame x_frac by band label
            x_by_band = {}
            for fr in h["frames"]:
                lab = fr.get("band_label") or band_label_by_idx.get(fr.get("band_idx"))
                if lab in ("800M","400M","200M"):
                    x_by_band[lab] = fr.get("x_frac")
            for band_key, manual in manual_bands.items():
                xf = x_by_band.get(band_key)
                if xf is None:
                    print(f"[R{rn}] {hname} @ {band_key}: no frame")
                    continue
                pairs.append((band_key, manual, xf, rn, hname))

    # Summarise: what x_frac ranges correspond to each manual bucket?
    by_bucket = defaultdict(list)
    for band, manual, xf, rn, hn in pairs:
        by_bucket[manual].append(xf)
    print("\n=== x_frac distribution by manual bucket (all bands, excl. 200M where noted) ===")
    order = ["Rail", "2-wide", "3-wide", "4+-wide"]
    for b in order:
        xs = sorted(by_bucket.get(b, []))
        if not xs:
            continue
        n = len(xs)
        mn, mx = xs[0], xs[-1]
        p10 = xs[int(0.10*(n-1))]
        p50 = xs[int(0.50*(n-1))]
        p90 = xs[int(0.90*(n-1))]
        print(f"  {b:<9} n={n:>3}  min={mn:.3f}  p10={p10:.3f}  p50={p50:.3f}  p90={p90:.3f}  max={mx:.3f}")

    # By band (exclude 200M for ground loss, but show classification ranges)
    print("\n=== same, split by band ===")
    for band_key in ("800M","400M","200M"):
        print(f"\n  -- {band_key} --")
        for b in order:
            xs = sorted([xf for (bd, m, xf, _r, _h) in pairs if bd == band_key and m == b])
            if not xs: continue
            n = len(xs)
            p10 = xs[int(0.10*(n-1))]
            p50 = xs[int(0.50*(n-1))]
            p90 = xs[int(0.90*(n-1))]
            print(f"    {b:<9} n={n:>3}  p10={p10:.3f}  p50={p50:.3f}  p90={p90:.3f}  range=[{xs[0]:.3f}, {xs[-1]:.3f}]")

    # Find misclassifications under CURRENT thresholds (0.28 / 0.40 / 0.55)
    def classify_current(x):
        if x < 0.28: return "Rail"
        if x < 0.40: return "2-wide"
        if x < 0.55: return "3-wide"
        return "4+-wide"
    print("\n=== CURRENT threshold misclassifications (0.28 / 0.40 / 0.55) ===")
    err_current = 0
    for band, manual, xf, rn, hn in pairs:
        got = classify_current(xf)
        if got != manual:
            err_current += 1
            print(f"  [R{rn}] {hn:<22} {band}: x={xf:.3f} manual={manual:<8} got={got}")
    print(f"  total mismatches: {err_current} / {len(pairs)}")

    # Try better thresholds: search for optimal split points
    print("\n=== searching optimal thresholds ===")
    # Binary-search between adjacent bucket distributions
    rail_xs = sorted(by_bucket.get("Rail", []))
    w2_xs = sorted(by_bucket.get("2-wide", []))
    w3_xs = sorted(by_bucket.get("3-wide", []))
    w4_xs = sorted(by_bucket.get("4+-wide", []))
    # Threshold 1 = between Rail.max and 2wide.min (or midpoint of overlap)
    if rail_xs and w2_xs:
        t1 = (max(rail_xs) + min(w2_xs)) / 2 if max(rail_xs) < min(w2_xs) else (
            sum(rail_xs[-3:])/3 + sum(w2_xs[:3])/3) / 2
        print(f"  rail-max={max(rail_xs):.3f}  2wide-min={min(w2_xs):.3f}  suggested t1≈{t1:.3f}")
    if w2_xs and w3_xs:
        t2 = (max(w2_xs) + min(w3_xs)) / 2 if max(w2_xs) < min(w3_xs) else (
            sum(w2_xs[-3:])/3 + sum(w3_xs[:3])/3) / 2
        print(f"  2wide-max={max(w2_xs):.3f}  3wide-min={min(w3_xs):.3f}  suggested t2≈{t2:.3f}")
    if w3_xs and w4_xs:
        t3 = (max(w3_xs) + min(w4_xs)) / 2 if max(w3_xs) < min(w4_xs) else (
            sum(w3_xs[-3:])/3 + sum(w4_xs[:3])/3) / 2
        print(f"  3wide-max={max(w3_xs):.3f}  4+-min={min(w4_xs):.3f}  suggested t3≈{t3:.3f}")

    # Grid search
    import itertools
    best = None
    for t1 in [i/100 for i in range(15, 35)]:
        for t2 in [i/100 for i in range(30, 55)]:
            if t2 <= t1+0.02: continue
            for t3 in [i/100 for i in range(45, 75)]:
                if t3 <= t2+0.02: continue
                def cls(x):
                    if x<t1: return "Rail"
                    if x<t2: return "2-wide"
                    if x<t3: return "3-wide"
                    return "4+-wide"
                err = sum(1 for (_b,m,xf,_r,_h) in pairs if cls(xf)!=m)
                if best is None or err < best[0]:
                    best = (err, t1, t2, t3)
    print(f"\n  BEST thresholds: err={best[0]}/{len(pairs)}  t1={best[1]:.2f} t2={best[2]:.2f} t3={best[3]:.2f}")

if __name__ == "__main__":
    main()
