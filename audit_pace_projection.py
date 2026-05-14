"""
audit_pace_projection.py
========================

Audits the accuracy of the model's pace-projection module:

  1. Projected vs actual pace label  (Slow / Even / Fast / etc.)
  2. Beneficiary lift  — do `is_beneficiary=True` runners actually finish
     better than non-beneficiaries (overall AND vs their model-rank)?
  3. Advantage-score correlation with finishing position.
  4. Per-style finishing-position bias by projected pace.

Data sources
------------
  reports/race_day_report_{DC}_v4.4.json   (projected pace + speed_map)
  reports/race_day_report_{DC}_v3.4.8.json (fallback for older meetings)
  reports/results_{DC}.json                (actuals + actual_pace_label)

Output
------
  cache/pace_projection_audit.json   (machine-readable, full per-runner table)
  console summary                     (human-readable findings)

Run
---
    python audit_pace_projection.py
"""
from __future__ import annotations

import glob
import json
from pathlib import Path
from statistics import mean, stdev
from datetime import datetime

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
OUT = BASE / "cache" / "pace_projection_audit.json"


def _load(p: Path):
    if not p or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _et_path_for(dc: str) -> Path | None:
    for tag in ("v4.4", "v3.4.8"):
        p = REPORTS / f"race_day_report_{dc}_{tag}.json"
        if p.exists():
            return p
    return None


def _gather_dates() -> list[str]:
    et = set()
    for tag in ("v4.4", "v3.4.8"):
        for p in REPORTS.glob(f"race_day_report_????????_{tag}.json"):
            et.add(p.name[len("race_day_report_"):][:8])
    res = {p.name[len("results_"):][:8]
           for p in REPORTS.glob("results_????????.json")}
    return sorted(et & res)


# ─────────────────────────────────────────────────────────────────────
# Pace label normalisation
# ─────────────────────────────────────────────────────────────────────
# Both projected and actual labels live on a 5-point scale. We collapse
# them to a 3-point scale (Slow / Even / Fast) for accuracy scoring,
# because the model's 5-class precision is statistically noisy on a
# ~100-race sample.

_LABEL_TO_BAND = {
    # Slow band
    "slow":             "Slow",
    "very slow":        "Slow",
    "slightly slow":    "Slow",
    # Even band
    "even":             "Even",
    "moderate":         "Even",
    "average":          "Even",
    # Fast band
    "fast":             "Fast",
    "very fast":        "Fast",
    "slightly fast":    "Fast",
    "hot":              "Fast",
}


def _band(label) -> str | None:
    if not label:
        return None
    s = str(label).strip().lower()
    return _LABEL_TO_BAND.get(s)


# ─────────────────────────────────────────────────────────────────────
# Build per-runner table
# ─────────────────────────────────────────────────────────────────────
def _final_pos(runner) -> int | None:
    v = runner.get("place")
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _build_rows() -> list[dict]:
    rows: list[dict] = []
    for dc in _gather_dates():
        et = _load(_et_path_for(dc))
        res = _load(REPORTS / f"results_{dc}.json")
        if not et or not res:
            continue
        et_by_rn = {r["race_number"]: r for r in et.get("races", [])}
        for res_race in res.get("races", []):
            rn = res_race.get("race_number")
            er = et_by_rn.get(rn)
            if not er:
                continue
            proj_pace = er.get("pace")
            proj_score = er.get("pace_score")
            actual_pace = res_race.get("actual_pace_label")
            actual_dev = res_race.get("actual_dev")
            n_runners = len(res_race.get("runners") or [])
            # finishing positions keyed by horse_no
            finishers = {}
            for rr in res_race.get("runners", []):
                pos = _final_pos(rr)
                hn = rr.get("horse_no")
                if pos is not None and hn is not None:
                    finishers[int(hn)] = pos

            # speed_map.grid carries per-horse beneficiary + advantage
            sm = er.get("speed_map") or {}
            sm_grid = sm.get("grid") or []
            sm_by_no = {}
            for g in sm_grid:
                try:
                    sm_by_no[int(g.get("horse_no"))] = g
                except (TypeError, ValueError):
                    continue

            # Build per-runner rows from picks (has style/win_prob/draw)
            for p in er.get("picks", []):
                try:
                    hn = int(p.get("horse_no"))
                except (TypeError, ValueError):
                    continue
                pos = finishers.get(hn)
                if pos is None:
                    continue
                g = sm_by_no.get(hn, {})
                rows.append({
                    "date":        dc,
                    "race":        rn,
                    "distance":    res_race.get("distance"),
                    "venue":       et.get("meeting_venue") or "?",
                    "n_runners":   n_runners,
                    "horse_no":    hn,
                    "horse_name":  p.get("horse_name"),
                    "style":       p.get("style"),
                    "esz":         p.get("early_speed_z"),
                    "draw":        p.get("draw"),
                    "win_prob":    p.get("win_prob"),
                    "model_rank":  p.get("rank"),
                    "proj_pace":   proj_pace,
                    "proj_pace_band":   _band(proj_pace),
                    "proj_pace_score":  proj_score,
                    "actual_pace": actual_pace,
                    "actual_pace_band": _band(actual_pace),
                    "actual_dev":  actual_dev,
                    "is_beneficiary": bool(g.get("is_beneficiary")),
                    "advantage":   g.get("advantage"),
                    "pace_bonus_s": g.get("pace_style_bonus_s"),
                    "smap_total_adj": p.get("smap_total_adj"),
                    "final_pos":   pos,
                    "is_win":      1 if pos == 1 else 0,
                    "is_place":    1 if pos <= 3 else 0,
                    "is_top4":     1 if pos <= 4 else 0,
                })
    return rows


# ─────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────
def _pace_label_accuracy(rows: list[dict]) -> dict:
    """Per-race (not per-runner) pace-label accuracy."""
    seen = set()
    races = []
    for r in rows:
        k = (r["date"], r["race"])
        if k in seen:
            continue
        seen.add(k)
        races.append(r)
    full_match = 0
    band_match = 0
    cm = {}     # confusion matrix on bands
    n = 0
    n_band = 0
    for r in races:
        proj = r["proj_pace"]
        act = r["actual_pace"]
        if not proj or not act:
            continue
        n += 1
        if str(proj).strip().lower() == str(act).strip().lower():
            full_match += 1
        pb, ab = r["proj_pace_band"], r["actual_pace_band"]
        if pb and ab:
            n_band += 1
            cm.setdefault(pb, {}).setdefault(ab, 0)
            cm[pb][ab] += 1
            if pb == ab:
                band_match += 1
    return {
        "n_races_with_labels": n,
        "n_races_with_bands":  n_band,
        "exact_label_acc":     round(full_match / n, 3) if n else None,
        "band_acc":            round(band_match / n_band, 3) if n_band else None,
        "confusion":           cm,
    }


def _beneficiary_lift(rows: list[dict]) -> dict:
    """Average finishing position rank ratio for beneficiaries vs others.

    Finishing-position rank ratio = final_pos / n_runners (lower = better).
    Compare beneficiary horses vs non-beneficiary horses, also broken down
    by whether the actual pace matched the projected band.
    """
    def _pos_pct(r):
        return r["final_pos"] / r["n_runners"] if r["n_runners"] else None

    ben = [r for r in rows if r["is_beneficiary"]]
    non = [r for r in rows if not r["is_beneficiary"]]

    def _stats(group):
        if not group:
            return None
        pp = [_pos_pct(r) for r in group if _pos_pct(r) is not None]
        wins = sum(r["is_win"] for r in group)
        places = sum(r["is_place"] for r in group)
        n = len(group)
        return {
            "n":            n,
            "avg_pos_pct":  round(mean(pp), 3) if pp else None,
            "win_rate":     round(wins / n, 3) if n else None,
            "place_rate":   round(places / n, 3) if n else None,
        }

    # Conditional: when pace projection was CORRECT (band match)
    aligned = [r for r in rows
               if r["proj_pace_band"] and r["actual_pace_band"]
               and r["proj_pace_band"] == r["actual_pace_band"]]
    mis = [r for r in rows
           if r["proj_pace_band"] and r["actual_pace_band"]
           and r["proj_pace_band"] != r["actual_pace_band"]]

    ben_aligned = [r for r in aligned if r["is_beneficiary"]]
    non_aligned = [r for r in aligned if not r["is_beneficiary"]]
    ben_mis     = [r for r in mis     if r["is_beneficiary"]]
    non_mis     = [r for r in mis     if not r["is_beneficiary"]]

    return {
        "overall": {
            "beneficiary":      _stats(ben),
            "non_beneficiary":  _stats(non),
        },
        "pace_projection_correct": {
            "beneficiary":      _stats(ben_aligned),
            "non_beneficiary":  _stats(non_aligned),
        },
        "pace_projection_wrong": {
            "beneficiary":      _stats(ben_mis),
            "non_beneficiary":  _stats(non_mis),
        },
    }


def _model_rank_adjusted_lift(rows: list[dict]) -> dict:
    """Did beneficiaries outperform their MODEL rank?

    Compare (actual finishing pos) vs (model rank). Negative = outperformed.
    Stratify by beneficiary flag.
    """
    def _delta(r):
        try:
            return r["final_pos"] - r["model_rank"]
        except (TypeError, KeyError):
            return None

    ben_d = [_delta(r) for r in rows if r["is_beneficiary"]]
    ben_d = [v for v in ben_d if v is not None]
    non_d = [_delta(r) for r in rows if not r["is_beneficiary"]]
    non_d = [v for v in non_d if v is not None]

    return {
        "beneficiary":      {
            "n": len(ben_d),
            "mean_delta_finish_minus_modelrank":
                round(mean(ben_d), 2) if ben_d else None,
        },
        "non_beneficiary":  {
            "n": len(non_d),
            "mean_delta_finish_minus_modelrank":
                round(mean(non_d), 2) if non_d else None,
        },
    }


def _advantage_correlation(rows: list[dict]) -> dict:
    """Spearman-ish correlation between advantage and pos_pct.

    Use Pearson on ranks (avoids scipy dep).
    """
    pairs = [(r["advantage"], r["final_pos"] / r["n_runners"])
             for r in rows
             if r["advantage"] is not None
             and r["n_runners"]]
    if len(pairs) < 5:
        return {"n": len(pairs), "spearman_rho": None}
    # rank both
    n = len(pairs)
    xs = sorted(range(n), key=lambda i: pairs[i][0])
    ys = sorted(range(n), key=lambda i: pairs[i][1])
    rx = [0] * n; ry = [0] * n
    for rank, i in enumerate(xs): rx[i] = rank
    for rank, i in enumerate(ys): ry[i] = rank
    mx, my = mean(rx), mean(ry)
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    sx = (sum((v - mx) ** 2 for v in rx)) ** 0.5
    sy = (sum((v - my) ** 2 for v in ry)) ** 0.5
    rho = num / (sx * sy) if sx and sy else 0.0
    return {"n": n, "spearman_rho": round(rho, 3)}


def _style_x_pace(rows: list[dict]) -> list[dict]:
    """Avg finishing pos-pct by (proj_pace_band × style)."""
    buckets: dict[tuple, list[float]] = {}
    for r in rows:
        b = r["proj_pace_band"]
        s = r.get("style")
        if not b or not s or not r["n_runners"]:
            continue
        k = (b, s)
        buckets.setdefault(k, []).append(r["final_pos"] / r["n_runners"])
    out = []
    for (b, s), vs in sorted(buckets.items()):
        out.append({
            "proj_pace_band": b,
            "style":          s,
            "n":              len(vs),
            "avg_pos_pct":    round(mean(vs), 3),
        })
    return out


def analyze() -> dict:
    rows = _build_rows()
    label_acc = _pace_label_accuracy(rows)
    ben_lift = _beneficiary_lift(rows)
    rank_lift = _model_rank_adjusted_lift(rows)
    adv_corr = _advantage_correlation(rows)
    sxp = _style_x_pace(rows)

    return {
        "n_rows":           len(rows),
        "n_meetings":       len({r["date"] for r in rows}),
        "n_races":          len({(r["date"], r["race"]) for r in rows}),
        "label_accuracy":   label_acc,
        "beneficiary_lift": ben_lift,
        "model_rank_adj":   rank_lift,
        "advantage_corr":   adv_corr,
        "style_x_pace":     sxp,
        "per_runner":       rows,
        "generated_at":     datetime.now().isoformat(timespec="seconds"),
    }


# ─────────────────────────────────────────────────────────────────────
# Print human-readable summary
# ─────────────────────────────────────────────────────────────────────
def _print(result: dict):
    print(f"\n=== Pace Projection Audit ===")
    print(f"meetings: {result['n_meetings']}   races: {result['n_races']}   "
          f"runners: {result['n_rows']}")

    print(f"\n-- Pace LABEL accuracy --")
    la = result["label_accuracy"]
    print(f"  exact label match: {la['exact_label_acc'] and la['exact_label_acc']*100:.1f}%   "
          f"({la['n_races_with_labels']} races)")
    print(f"  band match (Slow/Even/Fast): "
          f"{la['band_acc'] and la['band_acc']*100:.1f}%   "
          f"({la['n_races_with_bands']} races)")
    print(f"  confusion (projected → actual band):")
    cm = la["confusion"]
    bands = ["Slow", "Even", "Fast"]
    head = "          " + "   ".join(f"{b:>6}" for b in bands)
    print(head)
    for pb in bands:
        row = cm.get(pb, {})
        cells = [f"{row.get(ab, 0):>6}" for ab in bands]
        print(f"  proj={pb:<5}" + "   ".join(cells))

    print(f"\n-- Beneficiary flag — does it help? --")
    bl = result["beneficiary_lift"]
    for tag, label in (("overall", "Overall"),
                       ("pace_projection_correct", "When pace projection RIGHT"),
                       ("pace_projection_wrong",   "When pace projection WRONG")):
        d = bl[tag]
        b = d["beneficiary"]; n = d["non_beneficiary"]
        if not (b and n): continue
        print(f"  [{label}]")
        print(f"    beneficiary     n={b['n']:>4}  pos%={b['avg_pos_pct']*100:>5.1f}%  "
              f"win={b['win_rate']*100:>5.1f}%  place={b['place_rate']*100:>5.1f}%")
        print(f"    non-beneficiary n={n['n']:>4}  pos%={n['avg_pos_pct']*100:>5.1f}%  "
              f"win={n['win_rate']*100:>5.1f}%  place={n['place_rate']*100:>5.1f}%")
        d_pos = (n['avg_pos_pct'] - b['avg_pos_pct']) * 100
        d_win = (b['win_rate'] - n['win_rate']) * 100
        d_pl  = (b['place_rate'] - n['place_rate']) * 100
        print(f"    Δ benefit:      pos% {d_pos:+.1f}pp better   "
              f"win {d_win:+.1f}pp   place {d_pl:+.1f}pp")

    print(f"\n-- Beneficiary vs MODEL RANK (did they outperform model expectation?) --")
    mra = result["model_rank_adj"]
    b = mra["beneficiary"]; n = mra["non_beneficiary"]
    print(f"  beneficiary     n={b['n']:>4}  finish_pos − model_rank = "
          f"{b['mean_delta_finish_minus_modelrank']:+.2f}")
    print(f"  non-beneficiary n={n['n']:>4}  finish_pos − model_rank = "
          f"{n['mean_delta_finish_minus_modelrank']:+.2f}")
    print(f"  (negative = outperformed model rank; flag is useful if "
          f"beneficiary number is MORE negative)")

    print(f"\n-- Advantage score (speed_map.grid[].advantage) vs finishing position --")
    ac = result["advantage_corr"]
    print(f"  Spearman ρ = {ac['spearman_rho']}   (n={ac['n']})")
    print(f"  (negative ρ = higher advantage → lower finishing position = "
          f"GOOD; positive = the score is mis-signed or noise)")

    print(f"\n-- Style × projected pace (avg finish pos%) --")
    print(f"  {'Band':<6} {'Style':<10} {'N':>4}  {'pos%':>6}")
    for r in result["style_x_pace"]:
        print(f"  {r['proj_pace_band']:<6} {r['style']:<10} "
              f"{r['n']:>4}  {r['avg_pos_pct']*100:>5.1f}%")


def main():
    result = analyze()
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    _print(result)
    print(f"\nSaved → {OUT}")


if __name__ == "__main__":
    main()
