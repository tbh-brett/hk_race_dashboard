#!/usr/bin/env python3
"""
Live Race-Day Analysis Engine
==============================
Processes completed race results against ET + SARR pre-race predictions
to detect convergence/divergence patterns, surface-specific biases,
and actionable insights for remaining races.

Usage (standalone test):
    python live_analysis.py --date 2026-04-19

Designed to be imported by dashboard.py for the Live Feed page.
"""

import json, os, re, math, sys, argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime

BASE = Path(__file__).resolve().parent
REPORTS = BASE / "reports"

# ════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ════════════════════════════════════════════════════════════════════════

def load_predictions(date_str: str) -> dict:
    """Load ET + SARR predictions for a meeting date (YYYYMMDD)."""
    et_path = REPORTS / f"race_day_report_{date_str}_v4.4.json"
    if not et_path.exists():
        et_path = REPORTS / f"race_day_report_{date_str}_v3.4.8.json"
    sarr_path = REPORTS / f"race_day_report_{date_str}_SARR.json"

    et_data = None
    if et_path.exists():
        with open(et_path, "r", encoding="utf-8") as f:
            et_data = json.load(f)

    sarr_data = None
    if sarr_path.exists():
        with open(sarr_path, "r", encoding="utf-8") as f:
            sarr_data = json.load(f)

    return {"et": et_data, "sarr": sarr_data}


def load_results(date_str: str) -> dict | None:
    """Load scraped results for a meeting date (YYYYMMDD)."""
    date_iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    results_path = REPORTS / f"results_{date_str}.json"
    if results_path.exists():
        with open(results_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _parse_ft(val) -> float | None:
    """Parse finish time to seconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    m = re.match(r"(\d+):(\d+\.?\d*)", s)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _safe_int(val, default=99):
    if val is None:
        return default
    m = re.match(r"(\d+)", str(val).strip())
    return int(m.group(1)) if m else default


# ════════════════════════════════════════════════════════════════════════
# RACE-BY-RACE ANALYSIS
# ════════════════════════════════════════════════════════════════════════

def analyse_race(race_result: dict, et_race: dict | None, sarr_race: dict | None,
                 racecard_meta: dict | None = None) -> dict:
    """
    Analyse a single completed race against predictions.

    Returns a rich dict with:
      - convergence/divergence flags per model
      - winner/place analysis
      - pace read
      - underperformers / overperformers with explanations
      - surface, draw, style observations
    """
    rn = race_result["race_number"]
    runners = race_result.get("runners", [])
    if not runners:
        return {"race_number": rn, "status": "no_runners"}

    distance = race_result.get("distance") or (et_race or {}).get("distance", 0)
    is_awt = race_result.get("is_awt", False)
    if not is_awt and et_race:
        is_awt = et_race.get("is_awt", False)
    surface = "AWT" if is_awt else "Turf"
    going = race_result.get("going", "")

    # ── Build result lookup: horse_no → result ──
    result_by_no = {}
    result_by_name = {}
    for r in runners:
        hno = r.get("horse_no")
        if hno is not None and str(hno).strip():
            try:
                result_by_no[int(hno)] = r
            except (ValueError, TypeError):
                pass
        hn = (r.get("horse_name") or "").upper().strip()
        if hn:
            result_by_name[hn] = r

    # Sorted by place
    sorted_runners = sorted(runners, key=lambda r: _safe_int(r.get("place")))
    winner = sorted_runners[0] if sorted_runners else None
    top3_nos = {int(r["horse_no"]) for r in sorted_runners[:3] if r.get("horse_no")}
    top3_names = {(r.get("horse_name") or "").upper().strip() for r in sorted_runners[:3]}

    # ── ET model comparison ──
    et_analysis = _compare_model(et_race, result_by_no, result_by_name,
                                 top3_nos, top3_names, "ET") if et_race else None

    # ── SARR model comparison ──
    sarr_analysis = _compare_model(sarr_race, result_by_no, result_by_name,
                                   top3_nos, top3_names, "SARR") if sarr_race else None

    # ── Pace read (from actual running positions + sectionals) ──
    pace_read = _analyse_pace(sorted_runners, distance, et_race)

    # ── Draw analysis ──
    draw_obs = _analyse_draw(sorted_runners)

    # ── Underperformers / overperformers ──
    under, over = _find_performance_outliers(
        sorted_runners, et_race, sarr_race, result_by_no, result_by_name
    )

    # ── Winner profile ──
    winner_profile = None
    if winner:
        w_no = int(winner.get("horse_no", 0))
        w_name = (winner.get("horse_name") or "").upper().strip()
        et_pick = _find_pick(et_race, w_no, w_name) if et_race else None
        sarr_pick = _find_pick(sarr_race, w_no, w_name) if sarr_race else None
        winner_profile = {
            "horse_name": winner.get("horse_name", "?"),
            "horse_no": w_no,
            "draw": winner.get("draw"),
            "finish_time": _parse_ft(winner.get("finish_time")),
            "win_odds": winner.get("win_odds"),
            "running_position": winner.get("running_position", ""),
            "et_rank": et_pick["rank"] if et_pick else None,
            "sarr_rank": sarr_pick["rank"] if sarr_pick else None,
            "et_style": (et_pick or {}).get("style"),
            "sarr_style": (sarr_pick or {}).get("style"),
        }

    return {
        "race_number": rn,
        "distance": distance,
        "surface": surface,
        "going": going,
        "n_runners": len(runners),
        "winner": winner_profile,
        "et": et_analysis,
        "sarr": sarr_analysis,
        "pace_read": pace_read,
        "draw_obs": draw_obs,
        "underperformers": under,
        "overperformers": over,
        "status": "analysed",
    }


def _find_pick(race_data, horse_no, horse_name_upper):
    """Find a pick in ET/SARR data by horse_no or name."""
    if not race_data:
        return None
    for p in race_data.get("picks", []):
        if p.get("horse_no") == horse_no:
            return p
        if (p.get("horse_name") or "").upper().strip() == horse_name_upper:
            return p
    return None


def _compare_model(model_race, result_by_no, result_by_name,
                   top3_nos, top3_names, model_name):
    """Compare one model's predictions against actual results."""
    picks = model_race.get("picks", [])
    if not picks:
        return None

    # Match each pick to actual result
    matched = []
    for p in picks:
        hno = p.get("horse_no")
        hname = (p.get("horse_name") or "").upper().strip()
        actual = result_by_no.get(int(hno)) if hno else None
        if not actual:
            actual = result_by_name.get(hname)
        actual_place = _safe_int(actual.get("place")) if actual else 99
        actual_ft = _parse_ft(actual.get("finish_time")) if actual else None

        matched.append({
            "horse_name": p.get("horse_name", "?"),
            "horse_no": hno,
            "pred_rank": p["rank"],
            "actual_place": actual_place,
            "pred_time": p.get("projected_time"),
            "actual_time": actual_ft,
            "style": p.get("style", "?"),
            "sarr_score": p.get("sarr"),
            "win_prob": p.get("win_prob"),
            "flags": p.get("flags", []),
        })

    # ── Convergence metrics ──
    top1_pick = picks[0]
    top1_no = int(top1_pick.get("horse_no", 0))
    top1_name = (top1_pick.get("horse_name") or "").upper().strip()
    top1_hit = top1_no in top3_nos or top1_name in top3_names

    model_top3_nos = {int(p["horse_no"]) for p in picks[:3] if p.get("horse_no")}
    model_top3_names = {(p.get("horse_name") or "").upper().strip() for p in picks[:3]}
    top3_overlap = len((model_top3_nos & top3_nos) | (model_top3_names & top3_names))

    model_top4_nos = {int(p["horse_no"]) for p in picks[:4] if p.get("horse_no")}
    model_top4_names = {(p.get("horse_name") or "").upper().strip() for p in picks[:4]}
    top4_overlap = len((model_top4_nos & top3_nos) | (model_top4_names & top3_names))

    # Rank correlation (Spearman) for matched runners
    pred_ranks = []
    actual_ranks = []
    for m in matched:
        if m["actual_place"] < 90:
            pred_ranks.append(m["pred_rank"])
            actual_ranks.append(m["actual_place"])

    rho = None
    if len(pred_ranks) >= 4:
        try:
            from scipy.stats import spearmanr
            rho, _ = spearmanr(pred_ranks, actual_ranks)
        except ImportError:
            # Manual Spearman
            n = len(pred_ranks)
            d2 = sum((p - a) ** 2 for p, a in zip(
                _rankdata(pred_ranks), _rankdata(actual_ranks)))
            rho = 1 - 6 * d2 / (n * (n**2 - 1)) if n > 1 else 0

    # Time accuracy (ET only)
    time_errors = []
    for m in matched:
        if m["pred_time"] and m["actual_time"]:
            time_errors.append(m["pred_time"] - m["actual_time"])
    mae = sum(abs(e) for e in time_errors) / len(time_errors) if time_errors else None
    bias = sum(time_errors) / len(time_errors) if time_errors else None

    # Winner rank in model
    winner_rank = None
    for m in matched:
        if m["actual_place"] == 1:
            winner_rank = m["pred_rank"]
            break

    # Convergence label
    if top3_overlap >= 2 and (rho is not None and rho >= 0.3):
        convergence = "strong"
    elif top3_overlap >= 1 or (rho is not None and rho >= 0.15):
        convergence = "partial"
    else:
        convergence = "divergent"

    return {
        "model": model_name,
        "winner_rank": winner_rank,
        "top1_hit": top1_hit,
        "top3_overlap": top3_overlap,
        "top4_overlap": top4_overlap,
        "spearman_rho": round(rho, 3) if rho is not None else None,
        "mae": round(mae, 3) if mae is not None else None,
        "time_bias": round(bias, 3) if bias is not None else None,
        "convergence": convergence,
        "matched": matched,
    }


def _rankdata(values):
    """Simple rank (1-based, average ties)."""
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j + 1) / 2
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def _analyse_pace(sorted_runners, distance, et_race):
    """Analyse actual pace shape from running positions."""
    # Classify each finisher's running style from their first position
    styles_placing = []
    field_size = len(sorted_runners)
    for r in sorted_runners:
        rp = str(r.get("running_position", "")).strip().split()
        place = _safe_int(r.get("place"))
        if place >= 90 or not rp:
            continue
        try:
            first_call = int(rp[0])
        except (ValueError, IndexError):
            continue
        last_call = None
        try:
            last_call = int(rp[-1])
        except (ValueError, IndexError):
            pass

        if first_call <= 2:
            style = "Leader"
        elif first_call <= max(4, int(field_size * 0.3)):
            style = "On-Pace"
        elif first_call >= max(8, int(field_size * 0.7)):
            style = "Closer"
        else:
            style = "Midfield"

        pos_change = (first_call - last_call) if last_call is not None else 0

        styles_placing.append({
            "place": place,
            "style": style,
            "first_call": first_call,
            "last_call": last_call,
            "pos_change": pos_change,
            "horse_name": r.get("horse_name", "?"),
        })

    if not styles_placing:
        return {"shape": "unknown", "summary": "Insufficient data"}

    # Who won? Front-runner or closer?
    winner_style = styles_placing[0]["style"] if styles_placing else "?"
    top3_styles = [s["style"] for s in styles_placing[:3]]

    front_count = sum(1 for s in top3_styles if s in ("Leader", "On-Pace"))
    back_count = sum(1 for s in top3_styles if s in ("Midfield", "Closer"))

    if front_count >= 2:
        shape = "front-biased"
    elif back_count >= 2:
        shape = "closer-biased"
    else:
        shape = "even"

    # Average position gain for closers vs leaders
    closers_gain = [s["pos_change"] for s in styles_placing if s["style"] == "Closer"]
    leaders_fade = [s["pos_change"] for s in styles_placing if s["style"] == "Leader"]
    avg_closer_gain = sum(closers_gain) / len(closers_gain) if closers_gain else 0
    avg_leader_fade = sum(leaders_fade) / len(leaders_fade) if leaders_fade else 0

    # Compare to ET pace prediction
    et_pace_pred = (et_race or {}).get("pace", "")
    pace_match = None
    if et_pace_pred:
        if "Fast" in et_pace_pred and shape == "closer-biased":
            pace_match = "aligned"  # fast pace → closers benefit
        elif "Slow" in et_pace_pred and shape == "front-biased":
            pace_match = "aligned"  # slow pace → leaders hold
        elif "Fast" in et_pace_pred and shape == "front-biased":
            pace_match = "divergent"  # expected fast but leaders held — tempo wasn't severe
        elif "Slow" in et_pace_pred and shape == "closer-biased":
            pace_match = "divergent"
        else:
            pace_match = "neutral"

    summary_parts = [f"Winner ran {winner_style} style"]
    if shape == "front-biased":
        summary_parts.append("front-runners dominated (on-pace bias)")
    elif shape == "closer-biased":
        summary_parts.append("closers swept up (off-pace bias)")
    if avg_closer_gain > 3:
        summary_parts.append(f"closers gained avg {avg_closer_gain:.1f} positions")
    if avg_leader_fade < -2:
        summary_parts.append(f"leaders faded avg {abs(avg_leader_fade):.1f} positions")

    return {
        "shape": shape,
        "winner_style": winner_style,
        "top3_styles": top3_styles,
        "front_in_top3": front_count,
        "back_in_top3": back_count,
        "avg_closer_gain": round(avg_closer_gain, 1),
        "avg_leader_fade": round(avg_leader_fade, 1),
        "et_pace_pred": et_pace_pred,
        "pace_match": pace_match,
        "summary": "; ".join(summary_parts),
    }


def _analyse_draw(sorted_runners):
    """Analyse draw bias from results."""
    if not sorted_runners:
        return {"summary": "No data"}

    draw_places = []
    for r in sorted_runners:
        d = r.get("draw")
        p = _safe_int(r.get("place"))
        if d and p < 90:
            draw_places.append({"draw": int(d), "place": p})

    if len(draw_places) < 4:
        return {"summary": "Insufficient data"}

    max_draw = max(dp["draw"] for dp in draw_places)
    inside = [dp for dp in draw_places if dp["draw"] <= max(3, max_draw // 3)]
    outside = [dp for dp in draw_places if dp["draw"] >= max(max_draw - 2, max_draw * 2 // 3)]

    avg_inside = sum(dp["place"] for dp in inside) / len(inside) if inside else 0
    avg_outside = sum(dp["place"] for dp in outside) / len(outside) if outside else 0

    # Winner's draw
    winner_draw = sorted_runners[0].get("draw") if sorted_runners else None

    if avg_inside and avg_outside:
        diff = avg_outside - avg_inside
        if diff > 2:
            bias = "inside"
        elif diff < -2:
            bias = "outside"
        else:
            bias = "neutral"
    else:
        bias = "unknown"

    return {
        "bias": bias,
        "avg_inside_place": round(avg_inside, 1),
        "avg_outside_place": round(avg_outside, 1),
        "winner_draw": winner_draw,
        "summary": f"Draw bias: {bias} (inside avg P{avg_inside:.1f}, outside avg P{avg_outside:.1f})"
                   + (f", winner from draw {winner_draw}" if winner_draw else ""),
    }


def _find_performance_outliers(sorted_runners, et_race, sarr_race,
                               result_by_no, result_by_name):
    """Identify horses that massively over/under-performed predictions."""
    underperformers = []
    overperformers = []

    for r in sorted_runners:
        raw_no = r.get("horse_no", "")
        if not str(raw_no).strip():
            continue
        try:
            hno = int(raw_no)
        except (ValueError, TypeError):
            continue
        hname = (r.get("horse_name") or "").upper().strip()
        actual_place = _safe_int(r.get("place"))
        if actual_place >= 90:
            continue

        et_pick = _find_pick(et_race, hno, hname) if et_race else None
        sarr_pick = _find_pick(sarr_race, hno, hname) if sarr_race else None

        et_rank = et_pick["rank"] if et_pick else None
        sarr_rank = sarr_pick["rank"] if sarr_pick else None

        # Best predicted rank across models
        pred_ranks = [x for x in [et_rank, sarr_rank] if x is not None]
        if not pred_ranks:
            continue
        best_pred = min(pred_ranks)
        worst_pred = max(pred_ranks)

        diff = actual_place - best_pred  # positive = worse than predicted

        reasons = []

        # ── Underperformer: predicted top 4 but finished outside top half ──
        n_runners = len(sorted_runners)
        if best_pred <= 4 and actual_place > max(6, n_runners // 2):
            reasons.append(f"predicted Rk{best_pred} but finished P{actual_place}")
            if et_pick:
                style = et_pick.get("style", "")
                flags = et_pick.get("flags", [])
                if flags:
                    reasons.append(f"flags: {', '.join(str(f) for f in flags)}")
                # Check if running style was punished
                rp = str(r.get("running_position", "")).strip().split()
                if rp:
                    try:
                        first_pos = int(rp[0])
                        last_pos = int(rp[-1]) if len(rp) > 1 else first_pos
                        if first_pos <= 3 and last_pos > actual_place:
                            reasons.append("led early but faded — possible pace collapse")
                        elif first_pos > n_runners * 0.6:
                            reasons.append("never got into contention — too far back")
                    except ValueError:
                        pass
            underperformers.append({
                "horse_name": r.get("horse_name", "?"),
                "horse_no": hno,
                "actual_place": actual_place,
                "et_rank": et_rank,
                "sarr_rank": sarr_rank,
                "win_odds": r.get("win_odds"),
                "reasons": reasons,
            })

        # ── Overperformer: predicted outside top 6 but finished top 3 ──
        elif worst_pred > 6 and actual_place <= 3:
            reasons.append(f"predicted Rk{worst_pred} but finished P{actual_place}")
            odds = r.get("win_odds")
            if odds:
                try:
                    odds_val = float(str(odds).replace("$", "").strip())
                    if odds_val >= 20:
                        reasons.append(f"longshot at ${odds_val:.0f}")
                except (ValueError, TypeError):
                    pass
            overperformers.append({
                "horse_name": r.get("horse_name", "?"),
                "horse_no": hno,
                "actual_place": actual_place,
                "et_rank": et_rank,
                "sarr_rank": sarr_rank,
                "win_odds": r.get("win_odds"),
                "reasons": reasons,
            })

    return underperformers, overperformers


# ════════════════════════════════════════════════════════════════════════
# CUMULATIVE PATTERN RECOGNITION (across completed races)
# ════════════════════════════════════════════════════════════════════════

def build_cumulative_analysis(race_analyses: list[dict]) -> dict:
    """
    Aggregate all completed race analyses into a cumulative pattern report.
    Tracks Turf and AWT independently.
    """
    if not race_analyses:
        return {"status": "no_races", "patterns": [], "alerts": []}

    # ── Surface-split tracking ──
    surfaces = {"Turf": [], "AWT": []}
    for ra in race_analyses:
        if ra.get("status") != "analysed":
            continue
        s = ra.get("surface", "Turf")
        surfaces.get(s, surfaces["Turf"]).append(ra)

    patterns = []
    alerts = []

    # ── Per-surface analysis ──
    for surf, races in surfaces.items():
        if not races:
            continue

        n = len(races)

        # 1. Model convergence tracking
        for model_key in ["et", "sarr"]:
            model_label = "ET" if model_key == "et" else "SARR"
            convergences = []
            rhos = []
            top1_hits = 0
            top3_overlaps = []
            winner_ranks = []

            for ra in races:
                ma = ra.get(model_key)
                if not ma:
                    continue
                convergences.append(ma["convergence"])
                if ma.get("spearman_rho") is not None:
                    rhos.append(ma["spearman_rho"])
                if ma.get("top1_hit"):
                    top1_hits += 1
                top3_overlaps.append(ma.get("top3_overlap", 0))
                if ma.get("winner_rank") is not None:
                    winner_ranks.append(ma["winner_rank"])

            if not convergences:
                continue

            n_strong = convergences.count("strong")
            n_div = convergences.count("divergent")
            avg_rho = sum(rhos) / len(rhos) if rhos else None
            avg_overlap = sum(top3_overlaps) / len(top3_overlaps) if top3_overlaps else 0
            avg_winner_rk = sum(winner_ranks) / len(winner_ranks) if winner_ranks else None

            # Pattern classification
            if n_strong >= n * 0.6:
                status = "converging"
                patterns.append(
                    f"[{surf}] {model_label} is CONVERGING — strong alignment in "
                    f"{n_strong}/{n} races (avg ρ={avg_rho:.2f}, "
                    f"avg top-3 overlap {avg_overlap:.1f}/3). "
                    f"Trust {model_label} rankings for remaining {surf} races."
                )
            elif n_div >= n * 0.5:
                status = "diverging"
                patterns.append(
                    f"[{surf}] {model_label} is DIVERGING — poor alignment in "
                    f"{n_div}/{n} races (avg ρ={avg_rho:.2f if avg_rho else 0:.2f}). "
                    f"De-weight {model_label} for remaining {surf} races."
                )
            else:
                status = "mixed"
                patterns.append(
                    f"[{surf}] {model_label} is MIXED — {n_strong} strong, "
                    f"{n_div} divergent out of {n} races. "
                    f"Selective trust — verify with other signals."
                )

            # Winner rank alert
            if avg_winner_rk and avg_winner_rk > 5:
                alerts.append(
                    f"[{surf}] {model_label} winner avg rank is {avg_winner_rk:.1f} "
                    f"— model is struggling to identify winners on {surf}."
                )

        # 2. Pace pattern
        pace_shapes = [ra["pace_read"]["shape"] for ra in races
                       if ra.get("pace_read", {}).get("shape") != "unknown"]
        if pace_shapes:
            front_count = pace_shapes.count("front-biased")
            closer_count = pace_shapes.count("closer-biased")
            if front_count >= len(pace_shapes) * 0.6:
                patterns.append(
                    f"[{surf}] PACE: Front-runners dominating ({front_count}/{len(pace_shapes)} races) "
                    f"— on-pace/leader styles advantaged. Upgrade frontrunners in remaining races."
                )
                alerts.append(
                    f"[{surf}] On-pace bias detected — closers are struggling to make up ground."
                )
            elif closer_count >= len(pace_shapes) * 0.6:
                patterns.append(
                    f"[{surf}] PACE: Closers sweeping ({closer_count}/{len(pace_shapes)} races) "
                    f"— strong late-speed advantage. Upgrade closers/midfielders."
                )
                alerts.append(
                    f"[{surf}] Off-pace bias detected — leaders are fading late."
                )
            else:
                patterns.append(
                    f"[{surf}] PACE: Mixed shapes ({front_count} front, {closer_count} closer "
                    f"out of {len(pace_shapes)}) — no dominant pace bias."
                )

        # ET pace prediction accuracy
        pace_matches = [ra["pace_read"].get("pace_match") for ra in races
                        if ra.get("pace_read", {}).get("pace_match")]
        if pace_matches:
            aligned = pace_matches.count("aligned")
            divergent = pace_matches.count("divergent")
            if divergent > aligned:
                alerts.append(
                    f"[{surf}] ET pace predictions are inaccurate today "
                    f"({divergent} wrong vs {aligned} correct) — "
                    f"pace-dependent adjustments may be unreliable."
                )

        # 3. Draw bias
        draw_biases = [ra["draw_obs"]["bias"] for ra in races
                       if ra.get("draw_obs", {}).get("bias") not in (None, "unknown")]
        if draw_biases:
            inside_count = draw_biases.count("inside")
            outside_count = draw_biases.count("outside")
            if inside_count >= len(draw_biases) * 0.5 and inside_count >= 2:
                patterns.append(
                    f"[{surf}] DRAW: Inside rail advantage ({inside_count}/{len(draw_biases)} races) "
                    f"— low draws are outperforming."
                )
                alerts.append(
                    f"[{surf}] Inside rail bias — upgrade horses with low draws."
                )
            elif outside_count >= len(draw_biases) * 0.5 and outside_count >= 2:
                patterns.append(
                    f"[{surf}] DRAW: Outside rail advantage ({outside_count}/{len(draw_biases)} races)."
                )
                alerts.append(
                    f"[{surf}] Outside rail bias — upgrade horses with high draws."
                )

        # 4. Underperformer/overperformer patterns
        all_under = []
        all_over = []
        for ra in races:
            all_under.extend(ra.get("underperformers", []))
            all_over.extend(ra.get("overperformers", []))

        if len(all_over) >= 3:
            alerts.append(
                f"[{surf}] {len(all_over)} longshot/overperformers across {n} races "
                f"— form may be unreliable on {surf} today. Consider wider exotics."
            )
        if len(all_under) >= 3:
            alerts.append(
                f"[{surf}] {len(all_under)} fancied horses underperformed — "
                f"conditions may not suit class horses."
            )

    # ── Cross-surface comparison ──
    if surfaces["Turf"] and surfaces["AWT"]:
        turf_rhos = []
        awt_rhos = []
        for ra in surfaces["Turf"]:
            for mk in ["et", "sarr"]:
                r = (ra.get(mk) or {}).get("spearman_rho")
                if r is not None:
                    turf_rhos.append(r)
        for ra in surfaces["AWT"]:
            for mk in ["et", "sarr"]:
                r = (ra.get(mk) or {}).get("spearman_rho")
                if r is not None:
                    awt_rhos.append(r)

        avg_turf = sum(turf_rhos) / len(turf_rhos) if turf_rhos else 0
        avg_awt = sum(awt_rhos) / len(awt_rhos) if awt_rhos else 0
        if abs(avg_turf - avg_awt) > 0.15:
            better = "Turf" if avg_turf > avg_awt else "AWT"
            worse = "AWT" if better == "Turf" else "Turf"
            patterns.append(
                f"Models performing significantly better on {better} (avg ρ={max(avg_turf, avg_awt):.2f}) "
                f"vs {worse} (avg ρ={min(avg_turf, avg_awt):.2f}) — "
                f"adjust confidence by surface."
            )

    return {
        "status": "ok",
        "n_races": len(race_analyses),
        "n_turf": len(surfaces["Turf"]),
        "n_awt": len(surfaces["AWT"]),
        "patterns": patterns,
        "alerts": alerts,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


# ════════════════════════════════════════════════════════════════════════
# READABLE REPORT COMPOSITION
# ════════════════════════════════════════════════════════════════════════

def compose_race_summary(ra: dict) -> str:
    """Compose a human-readable summary for a single completed race."""
    if ra.get("status") != "analysed":
        return f"R{ra.get('race_number', '?')}: No data"

    lines = []
    rn = ra["race_number"]
    w = ra.get("winner") or {}
    lines.append(
        f"━━━ R{rn} | {ra['distance']}m {ra['surface']} ━━━"
    )

    # Winner line
    w_name = w.get("horse_name", "?")
    w_draw = w.get("draw", "?")
    w_odds = w.get("win_odds", "?")
    w_ft = w.get("finish_time")
    ft_str = f"{w_ft:.2f}s" if w_ft else "?"
    lines.append(
        f"  Winner: {w_name} (Dr{w_draw}, ${w_odds}, {ft_str})"
    )
    et_rk = w.get("et_rank")
    sarr_rk = w.get("sarr_rank")
    if et_rk or sarr_rk:
        lines.append(
            f"  → ET rank: {et_rk or 'N/A'} | SARR rank: {sarr_rk or 'N/A'}"
        )

    # Model convergence
    for mk, label in [("et", "ET"), ("sarr", "SARR")]:
        ma = ra.get(mk)
        if not ma:
            continue
        conv = ma["convergence"].upper()
        emoji = "✓" if conv == "STRONG" else "~" if conv == "PARTIAL" else "✗"
        rho_str = f"ρ={ma['spearman_rho']:.2f}" if ma.get("spearman_rho") is not None else ""
        lines.append(
            f"  {label}: {emoji} {conv} — top3 overlap {ma['top3_overlap']}/3"
            + (f", {rho_str}" if rho_str else "")
            + (f", MAE {ma['mae']:.3f}s" if ma.get("mae") else "")
        )

    # Pace read
    pr = ra.get("pace_read", {})
    if pr.get("shape") != "unknown":
        lines.append(f"  Pace: {pr.get('summary', '?')}")
        if pr.get("pace_match"):
            pm = pr["pace_match"]
            lines.append(
                f"  → ET pace prediction: {'✓ correct' if pm == 'aligned' else '✗ wrong' if pm == 'divergent' else 'neutral'}"
            )

    # Draw
    do = ra.get("draw_obs", {})
    if do.get("bias") and do["bias"] != "unknown":
        lines.append(f"  Draw: {do['summary']}")

    # Underperformers
    for u in ra.get("underperformers", []):
        lines.append(
            f"  ⚠ UNDERPERFORMED: {u['horse_name']} (ET Rk{u.get('et_rank','?')}, "
            f"SARR Rk{u.get('sarr_rank','?')}) → P{u['actual_place']}"
        )
        for reason in u.get("reasons", []):
            lines.append(f"    → {reason}")

    # Overperformers
    for o in ra.get("overperformers", []):
        lines.append(
            f"  ★ OVERPERFORMED: {o['horse_name']} (ET Rk{o.get('et_rank','?')}, "
            f"SARR Rk{o.get('sarr_rank','?')}) → P{o['actual_place']}"
        )
        for reason in o.get("reasons", []):
            lines.append(f"    → {reason}")

    return "\n".join(lines)


def compose_cumulative_report(cumul: dict, race_analyses: list[dict]) -> str:
    """Compose a full readable report combining per-race + cumulative."""
    lines = ["═══ LIVE RACE-DAY ANALYSIS ═══", ""]

    for ra in race_analyses:
        if ra.get("status") == "analysed":
            lines.append(compose_race_summary(ra))
            lines.append("")

    lines.append("═══ CUMULATIVE PATTERNS ═══")
    lines.append("")
    for p in cumul.get("patterns", []):
        lines.append(f"  • {p}")
    lines.append("")
    lines.append("═══ ALERTS FOR REMAINING RACES ═══")
    lines.append("")
    for a in cumul.get("alerts", []):
        lines.append(f"  ⚡ {a}")

    if not cumul.get("patterns") and not cumul.get("alerts"):
        lines.append("  (Not enough races completed for pattern detection)")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════
# MAIN (standalone test)
# ════════════════════════════════════════════════════════════════════════

def run_live_analysis(date_str: str) -> dict:
    """
    Full live analysis pipeline.
    date_str: YYYYMMDD or YYYY-MM-DD
    Returns: {race_analyses, cumulative, report_text}
    """
    date_str = date_str.replace("-", "")

    preds = load_predictions(date_str)
    results = load_results(date_str)

    if not results:
        return {"error": f"No results found for {date_str}"}
    if not preds["et"] and not preds["sarr"]:
        return {"error": f"No predictions found for {date_str}"}

    et_races = {r["race_number"]: r for r in (preds["et"] or {}).get("races", [])}
    sarr_races = {r["race_number"]: r for r in (preds["sarr"] or {}).get("races", [])}

    race_analyses = []
    for rr in sorted(results.get("races", []), key=lambda r: r.get("race_number", 0)):
        rn = rr["race_number"]
        ra = analyse_race(
            rr,
            et_races.get(rn),
            sarr_races.get(rn),
        )
        race_analyses.append(ra)

    cumulative = build_cumulative_analysis(race_analyses)
    report_text = compose_cumulative_report(cumulative, race_analyses)

    return {
        "date": date_str,
        "race_analyses": race_analyses,
        "cumulative": cumulative,
        "report_text": report_text,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="20260419")
    args = parser.parse_args()

    result = run_live_analysis(args.date)

    if "error" in result:
        print(f"ERROR: {result['error']}")
        sys.exit(1)

    print(result["report_text"])
    print(f"\n\n--- JSON summary ---")
    cumul = result["cumulative"]
    print(f"Races analysed: {cumul['n_races']} (Turf: {cumul['n_turf']}, AWT: {cumul['n_awt']})")
    print(f"Patterns: {len(cumul['patterns'])}")
    print(f"Alerts: {len(cumul['alerts'])}")
