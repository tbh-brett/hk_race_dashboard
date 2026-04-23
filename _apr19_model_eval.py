"""Apr 19 model-vs-actual analysis (v4.4 picks vs results)."""
import json
from pathlib import Path

BASE = Path(__file__).parent
R = BASE / "reports"

results = json.loads((R / "results_20260419.json").read_text(encoding="utf-8"))

def winners_map(results_json):
    out = {}
    for r in results_json.get("races", []):
        rn = int(r.get("race_number") or r.get("race_no") or 0)
        placings = {}
        for row in r.get("runners", r.get("results", [])):
            pos = row.get("finishing_position") or row.get("position") or row.get("place")
            try:
                pos = int(pos)
            except Exception:
                continue
            name = (row.get("horse_name") or row.get("horse") or "").strip().upper()
            placings[pos] = name
        out[rn] = placings
    return out

winners = winners_map(results)

def norm(s):
    return (s or "").strip().upper()

def eval_report(path):
    rep = json.loads(path.read_text(encoding="utf-8"))
    hits = {"top1_win": 0, "top1_place": 0, "winner_in_top3": 0,
            "winner_in_top5": 0, "any_top3_in_top3": 0, "races": 0}
    rows = []
    for race in rep.get("races", []):
        rn = int(race.get("race_number") or 0)
        if rn not in winners:
            continue
        picks = race.get("picks", [])
        if not picks:
            continue
        picks_sorted = sorted(picks, key=lambda x: x.get("rank", 99))
        names = [norm(p.get("horse_name")) for p in picks_sorted]
        actual_1 = norm(winners[rn].get(1, ""))
        actual_2 = norm(winners[rn].get(2, ""))
        actual_3 = norm(winners[rn].get(3, ""))
        actual_top3 = {n for n in (actual_1, actual_2, actual_3) if n}
        hits["races"] += 1
        if names[0] == actual_1:
            hits["top1_win"] += 1
        if names[0] in actual_top3:
            hits["top1_place"] += 1
        if actual_1 in names[:3]:
            hits["winner_in_top3"] += 1
        if actual_1 in names[:5]:
            hits["winner_in_top5"] += 1
        if any(n in actual_top3 for n in names[:3]):
            hits["any_top3_in_top3"] += 1
        rows.append((rn, names[:5], actual_1, actual_2, actual_3))
    return hits, rows

for name in ("race_day_report_20260419_v4.4.json", "race_day_report_20260419_SARR.json"):
    p = R / name
    if not p.exists():
        continue
    print(f"\n=== {name} ===")
    h, rows = eval_report(p)
    n = h["races"]
    if not n:
        print("  (no picks)"); continue
    print(f"  races: {n}")
    print(f"  top-pick = winner      : {h['top1_win']}/{n} ({100*h['top1_win']/n:.0f}%)")
    print(f"  top-pick in top-3      : {h['top1_place']}/{n} ({100*h['top1_place']/n:.0f}%)")
    print(f"  winner in model top-3  : {h['winner_in_top3']}/{n} ({100*h['winner_in_top3']/n:.0f}%)")
    print(f"  winner in model top-5  : {h['winner_in_top5']}/{n} ({100*h['winner_in_top5']/n:.0f}%)")
    print(f"  any top-3 pick placed  : {h['any_top3_in_top3']}/{n} ({100*h['any_top3_in_top3']/n:.0f}%)")
    print()
    for rn, top5, w1, w2, w3 in rows:
        in3 = "W" if w1 == top5[0] else ("T3" if w1 in top5[:3] else ("T5" if w1 in top5 else "-"))
        print(f"  R{rn:>2} [{in3:>2}] top5={top5}")
        print(f"         actual 1-2-3: {w1} / {w2} / {w3}")
"""Apr 19 model-vs-actual analysis — v4.4 picks vs results."""
import json
from pathlib import Path

BASE = Path(__file__).parent
R = BASE / "reports"

results = json.loads((R / "results_20260419.json").read_text(encoding="utf-8"))
# race_day_report naming
candidates = list(R.glob("race_day_report_20260419*.json"))
print("Model reports available:", [c.name for c in candidates])

def winners_map(results_json):
    """race_no -> {pos: horse_name}"""
    out = {}
    for r in results_json.get("races", []):
        rn = int(r.get("race_number") or r.get("race_no") or 0)
        placings = {}
        for row in r.get("runners", r.get("results", [])):
            pos = row.get("finishing_position") or row.get("position") or row.get("place")
            try:
                pos = int(pos)
            except Exception:
                continue
            name = (row.get("horse_name") or row.get("horse") or "").strip().upper()
            placings[pos] = name
        out[rn] = placings
    return out

winners = winners_map(results)
print(f"\nActual winners (Apr 19):")
for rn in sorted(winners):
    w = winners[rn]
    print(f"  R{rn}: 1st={w.get(1,'?')}, 2nd={w.get(2,'?')}, 3rd={w.get(3,'?')}")

def norm(s):
    return (s or "").strip().upper().replace("  ", " ")

def eval_report(path):
    rep = json.loads(path.read_text(encoding="utf-8"))
    hits = {"top1_win": 0, "top3_any_top3": 0, "top3_win": 0, "races": 0}
    details = []
    for race in rep.get("races", []):
        rn = int(race.get("race_number") or race.get("race_no") or 0)
        if rn not in winners:
            continue
        # Try common fields
        picks = (race.get("model_top3") or race.get("top_picks")
                 or race.get("picks") or [])
        if not picks and "predictions" in race:
            preds = race["predictions"]
            if isinstance(preds, list):
                picks = [p.get("horse_name") or p.get("horse") for p in preds[:3]]
        if not picks:
            # fall back: sort runners by rank/score if present
            runners = race.get("runners", [])
            scored = [(r, r.get("rank") or r.get("model_rank")) for r in runners]
            scored = [s for s in scored if s[1] is not None]
            scored.sort(key=lambda x: x[1])
            picks = [s[0].get("horse_name") or s[0].get("horse") for s in scored[:3]]
        picks = [norm(p) for p in picks if p]
        if not picks:
            continue
        hits["races"] += 1
        actual_1 = norm(winners[rn].get(1, ""))
        actual_top3 = {norm(winners[rn].get(k, "")) for k in (1, 2, 3)}
        if picks[0] == actual_1:
            hits["top1_win"] += 1
        if any(p in actual_top3 for p in picks):
            hits["top3_any_top3"] += 1
        if actual_1 in picks:
            hits["top3_win"] += 1
        details.append((rn, picks, actual_1, winners[rn]))
    return hits, details

for c in candidates:
    print(f"\n=== {c.name} ===")
    try:
        h, d = eval_report(c)
        if h["races"] == 0:
            print("  (no picks found in expected schema)")
            continue
        print(f"  races evaluated: {h['races']}")
        print(f"  top-pick WIN: {h['top1_win']}/{h['races']} ({100*h['top1_win']/h['races']:.0f}%)")
        print(f"  winner in top-3: {h['top3_win']}/{h['races']} ({100*h['top3_win']/h['races']:.0f}%)")
        print(f"  any pick in top-3: {h['top3_any_top3']}/{h['races']} ({100*h['top3_any_top3']/h['races']:.0f}%)")
        print("  per-race:")
        for rn, picks, actual, full in d:
            tick = "✓" if actual in picks else "·"
            print(f"   R{rn}: picks={picks[:3]} | winner={actual} {tick}")
    except Exception as e:
        print(f"  ERROR: {e}")
