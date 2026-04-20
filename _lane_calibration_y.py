"""Redo calibration using y_in_band (which actually encodes lane)."""
from __future__ import annotations
import json, re
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).parent
DATE = "20260415"
RP = BASE / "running_position_photos" / DATE

from _lane_calibration import MANUAL, norm

def main():
    pairs = []
    for rn, horses_manual in MANUAL.items():
        jp = RP / f"R{rn}.json"
        if not jp.exists(): continue
        data = json.loads(jp.read_text(encoding="utf-8"))
        band_label_by_idx = {i: b["label"] for i, b in enumerate(data.get("bands", []))}
        by_name = {norm(h["horse_name"]): h for h in data["horses"]}
        for hname, manual_bands in horses_manual.items():
            h = by_name.get(norm(hname))
            if not h: continue
            y_by_band = {}
            for fr in h["frames"]:
                lab = fr.get("band_label") or band_label_by_idx.get(fr.get("band_idx"))
                if lab in ("800M","400M","200M"):
                    y_by_band[lab] = fr.get("y_in_band")
            for bk, manual in manual_bands.items():
                y = y_by_band.get(bk)
                if y is None: continue
                pairs.append((bk, manual, y, rn, hname))

    by = defaultdict(list)
    for bd, m, y, _r, _h in pairs:
        by[m].append(y)
    order = ["Rail", "2-wide", "3-wide", "4+-wide"]
    print("=== y_in_band distribution by manual bucket ===")
    for b in order:
        ys = sorted(by.get(b, []))
        if not ys: continue
        n = len(ys)
        print(f"  {b:<9} n={n:>3}  min={ys[0]:.3f}  p10={ys[int(0.10*(n-1))]:.3f}  "
              f"p50={ys[int(0.50*(n-1))]:.3f}  p90={ys[int(0.90*(n-1))]:.3f}  max={ys[-1]:.3f}")

    for bk in ("800M","400M","200M"):
        print(f"\n-- {bk} --")
        for b in order:
            ys = sorted([y for (bd, m, y, _r, _h) in pairs if bd==bk and m==b])
            if not ys: continue
            n = len(ys)
            print(f"  {b:<9} n={n:>3}  p10={ys[int(0.10*(n-1))]:.3f}  "
                  f"p50={ys[int(0.50*(n-1))]:.3f}  p90={ys[int(0.90*(n-1))]:.3f}  range=[{ys[0]:.3f},{ys[-1]:.3f}]")

    # Grid search optimal 3 thresholds
    best = None
    for t1 in [i/100 for i in range(10, 60)]:
        for t2 in [i/100 for i in range(20, 85)]:
            if t2 <= t1+0.03: continue
            for t3 in [i/100 for i in range(40, 99)]:
                if t3 <= t2+0.03: continue
                def cls(x):
                    if x<t1: return "Rail"
                    if x<t2: return "2-wide"
                    if x<t3: return "3-wide"
                    return "4+-wide"
                err = sum(1 for (_bd, m, y, _r, _h) in pairs if cls(y)!=m)
                if best is None or err < best[0]:
                    best = (err, t1, t2, t3)
    print(f"\n  BEST y_in_band thresholds: err={best[0]}/{len(pairs)}  "
          f"t1={best[1]:.2f} t2={best[2]:.2f} t3={best[3]:.2f}")

    # Show remaining errors
    t1, t2, t3 = best[1], best[2], best[3]
    def cls(x):
        if x<t1: return "Rail"
        if x<t2: return "2-wide"
        if x<t3: return "3-wide"
        return "4+-wide"
    print("\n=== remaining mismatches ===")
    for bd, m, y, rn, hn in pairs:
        got = cls(y)
        if got != m:
            print(f"  [R{rn}] {hn:<22} {bd}: y={y:.3f} manual={m:<8} got={got}")

if __name__ == "__main__":
    main()
