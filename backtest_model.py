#!/usr/bin/env python3
"""
HKJC Model Backtest Engine — backtest_model.py
=================================================
Compares pre-race model predictions against actual post-race results
across six accuracy dimensions.

Usage:
    # Single meeting
    python backtest_model.py --date 2026-04-01

    # Monthly report (all meetings in month)
    python backtest_model.py --month 2026-04

    # Seasonal report (full season)
    python backtest_model.py --season 2025-2026

    # All available data
    python backtest_model.py --all

Output:
    reports/backtest_YYYYMMDD.json       (per-meeting)
    reports/backtest_monthly_YYYY-MM.json (monthly aggregate)
    reports/backtest_season_YYYY-YYYY.json (seasonal aggregate)

Dimensions:
    1. Projected Time Accuracy
    2. Pace Prediction Accuracy
    3. Pace Beneficiary Analysis
    4. Risk Reflection Accuracy
    5. Running Style Prediction
    6. Draw Offset Accuracy
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

BASE = Path(__file__).parent
REPORTS = BASE / "reports"

# ── Distance-specific pace thresholds from MODEL_BRIEFING_v3.4.8 ─────────
PACE_THRESHOLDS = {
    1000: {"normal": 0.20, "slight": 0.40, "strong": 0.65},
    1200: {"normal": 0.25, "slight": 0.50, "strong": 0.85},
    1400: {"normal": 0.30, "slight": 0.60, "strong": 1.00},
    1600: {"normal": 0.32, "slight": 0.65, "strong": 1.10},
    1650: {"normal": 0.32, "slight": 0.65, "strong": 1.10},
    1800: {"normal": 0.40, "slight": 0.80, "strong": 1.20},
    2000: {"normal": 0.47, "slight": 0.95, "strong": 1.50},
    2200: {"normal": 0.47, "slight": 0.95, "strong": 1.50},
    2400: {"normal": 0.47, "slight": 0.95, "strong": 1.50},
}


# ══════════════════════════════════════════════════════════════════════════════
# Data loading
# ══════════════════════════════════════════════════════════════════════════════

def load_prediction(date_compact: str) -> Optional[Dict]:
    """Load pre-race prediction JSON."""
    path = REPORTS / f"race_day_report_{date_compact}_v3.4.8.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_results(date_compact: str) -> Optional[Dict]:
    """Load post-race results JSON."""
    path = REPORTS / f"results_{date_compact}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_meeting_dates(pattern: str = "all") -> List[str]:
    """Find all date_compact strings that have BOTH prediction + results."""
    pred_dates = set()
    for f in REPORTS.glob("race_day_report_*_v3.4.8.json"):
        m = re.search(r"race_day_report_(\d{8})_v3\.4\.8\.json", f.name)
        if m:
            pred_dates.add(m.group(1))

    result_dates = set()
    for f in REPORTS.glob("results_*.json"):
        m = re.search(r"results_(\d{8})\.json", f.name)
        if m:
            result_dates.add(m.group(1))

    matched = sorted(pred_dates & result_dates)

    if pattern == "all":
        return matched

    # Month filter: YYYY-MM
    if re.match(r"\d{4}-\d{2}$", pattern):
        prefix = pattern.replace("-", "")[:6]
        return [d for d in matched if d[:6] == prefix]

    # Season filter: YYYY-YYYY (Sep start)
    if re.match(r"\d{4}-\d{4}$", pattern):
        y1, y2 = pattern.split("-")
        # HK racing season: Sep year1 → Jul year2
        start = f"{y1}09"
        end = f"{y2}07"
        return [d for d in matched if start <= d[:6] <= end]

    # Single date: YYYY-MM-DD
    dc = pattern.replace("-", "")
    return [dc] if dc in matched else []


# ══════════════════════════════════════════════════════════════════════════════
# Matching logic
# ══════════════════════════════════════════════════════════════════════════════

def match_race_runners(pred_race: Dict, result_race: Dict) -> List[Dict]:
    """Match predicted and actual runners by horse_no."""
    result_index = {}
    for r in result_race.get("runners", []):
        try:
            hno = int(r["horse_no"])
        except (ValueError, TypeError):
            continue
        result_index[hno] = r

    matched = []
    for p in pred_race.get("picks", []):
        hno = p.get("horse_no")
        if hno in result_index:
            matched.append({"pred": p, "actual": result_index[hno]})
    return matched


def classify_actual_pace(sectiontimes: List[str], distance: int) -> Tuple[Optional[float], str]:
    """Compute actual race pace deviation from sectional times.

    NOTE: This function uses a single horse's sectionals. For accurate race pace,
    use _get_leader_early_sectionals() with all runners' positions + sectionals
    to find the actual pace set by the leading horse at each checkpoint.

    Returns (early_sum, "measured") or (None, "Unknown").
    """
    # Parse section times (excluding final section = last 400m)
    parsed = []
    for st in sectiontimes:
        if not st:
            continue
        try:
            parsed.append(float(st))
        except (ValueError, TypeError):
            continue

    if len(parsed) < 2:
        return None, "Unknown"

    # Sum of all sections except the last (final 400m)
    early_sum = sum(parsed[:-1])
    total_sum = sum(parsed)

    # The "deviation" is relative; we compare early_sum / total_sum ratio
    # against a neutral fraction. For now, use the winner's sectionals.
    # We'll compute per-race deviation in the aggregate step.
    return early_sum, "measured"


def classify_pace_label(deviation: float, distance: int) -> str:
    """Classify pace deviation into label using distance thresholds."""
    closest = min(PACE_THRESHOLDS.keys(), key=lambda d: abs(d - distance))
    th = PACE_THRESHOLDS[closest]

    if deviation <= -th["strong"]:
        return "Very Fast"
    elif deviation <= -th["slight"]:
        return "Fast"
    elif deviation <= -th["normal"]:
        return "Slightly Fast"
    elif deviation <= th["normal"]:
        return "Normal"
    elif deviation <= th["slight"]:
        return "Slightly Slow"
    elif deviation <= th["strong"]:
        return "Slow"
    else:
        return "Very Slow"


def actual_running_style(positions: List[str], field_size: int) -> str:
    """Classify running style from first-checkpoint position."""
    if not positions:
        return "Unknown"
    # Use first non-empty position
    for p in positions:
        if p:
            try:
                pos = int(p)
                break
            except (ValueError, TypeError):
                continue
    else:
        return "Unknown"

    if field_size <= 0:
        field_size = 14

    frac = pos / field_size
    if frac <= 0.15:
        return "Leader"
    elif frac <= 0.35:
        return "On-Pace"
    elif frac <= 0.65:
        return "Midfield"
    else:
        return "Closer"


# ══════════════════════════════════════════════════════════════════════════════
# Dimension 1: Projected Time Accuracy
# ══════════════════════════════════════════════════════════════════════════════

def compute_time_accuracy(matched_races: List[Dict]) -> Dict:
    """Compute MAE, Spearman rank correlation, Top-N hit rates."""
    all_errors = []
    rank_correlations = []
    top1_hits = 0
    top3_hits = 0
    top1_total = 0
    top3_total = 0

    for race_data in matched_races:
        pairs = race_data["pairs"]
        if len(pairs) < 3:
            continue

        errors = []
        pred_ranks = []
        actual_ranks = []

        # Determine actual finish order by place
        for pair in pairs:
            proj = pair["pred"].get("projected_time")
            actual_ft = pair["actual"].get("finish_time_seconds")
            if proj and actual_ft:
                errors.append(abs(proj - actual_ft))

            pred_ranks.append(pair["pred"]["rank"])
            place_str = str(pair["actual"].get("place", "99"))
            try:
                actual_ranks.append(int(place_str))
            except ValueError:
                actual_ranks.append(99)

        all_errors.extend(errors)

        # Spearman rank correlation
        if len(pred_ranks) >= 4:
            rho = _spearman(pred_ranks, actual_ranks)
            if rho is not None:
                rank_correlations.append(rho)

        # Top-N hit rates
        if pairs:
            top_pred = pairs[0]
            actual_place = _parse_place(top_pred["actual"].get("place", ""))
            top1_total += 1
            if actual_place and actual_place == 1:
                top1_hits += 1

            for p in pairs[:3]:
                top3_total += 1
                ap = _parse_place(p["actual"].get("place", ""))
                if ap and ap <= 3:
                    top3_hits += 1

    mae = np.mean(all_errors) if all_errors else None
    median_ae = float(np.median(all_errors)) if all_errors else None
    avg_rho = float(np.mean(rank_correlations)) if rank_correlations else None

    return {
        "mae_seconds": round(mae, 3) if mae is not None else None,
        "median_ae_seconds": round(median_ae, 3) if median_ae is not None else None,
        "n_observations": len(all_errors),
        "rank_correlation_avg": round(avg_rho, 3) if avg_rho is not None else None,
        "n_races_ranked": len(rank_correlations),
        "top1_win_rate": round(top1_hits / top1_total, 3) if top1_total else None,
        "top1_total": top1_total,
        "top3_place_rate": round(top3_hits / top3_total, 3) if top3_total else None,
        "top3_total": top3_total,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Dimension 2: Pace Prediction Accuracy
# ══════════════════════════════════════════════════════════════════════════════

def _get_leader_early_sectionals(all_runners: List[Dict], distance: int) -> Optional[float]:
    """Compute actual race pace from the leading horse's sectionals at each checkpoint.

    For each sectional checkpoint (excluding the final 400m), identifies the horse
    in position 1 at that checkpoint and uses their sectional time.
    Returns the sum of leader sectional times (the actual early pace), or None
    if insufficient data.

    Section count is determined from the data (not distance), since HKJC uses
    a variable first-section length (e.g., 1000m = 200+400+400 = 3 sections).
    """
    # Determine actual number of sections from the data
    max_sections = 0
    for runner in all_runners:
        positions = runner.get("positions", [])
        n = sum(1 for p in positions if p and str(p).strip())
        if n > max_sections:
            max_sections = n

    if max_sections < 2:
        return None

    # Build leader time for each early section (all except the final one)
    leader_total = 0.0
    sections_found = 0

    for sect_idx in range(max_sections - 1):  # exclude final section
        best_pos = 999
        best_time = None

        for runner in all_runners:
            positions = runner.get("positions", [])
            sectiontimes = runner.get("sectiontimes", [])
            if sect_idx >= len(positions) or sect_idx >= len(sectiontimes):
                continue
            pos_str = str(positions[sect_idx]).strip()
            time_str = str(sectiontimes[sect_idx]).strip()
            if not pos_str or not time_str:
                continue
            try:
                pos = int(pos_str)
                sect_time = float(time_str)
            except (ValueError, TypeError):
                continue

            if pos < best_pos:
                best_pos = pos
                best_time = sect_time

        if best_time is not None:
            leader_total += best_time
            sections_found += 1

    # Need at least 1 early section with leader data
    if sections_found == 0:
        return None

    return leader_total


def compute_pace_accuracy(matched_races: List[Dict]) -> Dict:
    """Compare predicted pace label vs actual pace from leader's sectionals.

    Pace is determined by the horse in position 1 at each checkpoint, NOT the
    winner (who may have raced from behind).
    """
    exact_matches = 0
    within_one = 0
    total = 0

    pace_labels_ordered = [
        "Very Fast", "Fast", "Slightly Fast", "Normal",
        "Slightly Slow", "Slow", "Very Slow"
    ]
    label_index = {l: i for i, l in enumerate(pace_labels_ordered)}

    pred_devs = []
    actual_devs = []
    details = []

    for race_data in matched_races:
        pred_pace = race_data.get("pred_pace", "")
        pred_score = race_data.get("pred_pace_score", 0)
        distance = race_data.get("distance", 1200) or 1200
        all_runners = race_data.get("all_result_runners", [])

        # Compute actual early pace from leader's sectionals at each checkpoint
        leader_early = _get_leader_early_sectionals(all_runners, distance)
        if leader_early is None:
            # Fallback: try winner's early sections (pre-v4.0 behaviour)
            winner = None
            for pair in race_data["pairs"]:
                place = _parse_place(pair["actual"].get("place", ""))
                if place == 1:
                    winner = pair["actual"]
                    break
            if not winner:
                continue
            sects = winner.get("sectiontimes", [])
            parsed = []
            for s in sects:
                try:
                    parsed.append(float(s))
                except (ValueError, TypeError):
                    continue
            if len(parsed) < 2:
                continue
            leader_early = sum(parsed[:-1])
            actual_total = sum(parsed)
        else:
            # Need total race time for deviation calc — use winner's total
            winner = None
            for pair in race_data["pairs"]:
                place = _parse_place(pair["actual"].get("place", ""))
                if place == 1:
                    winner = pair["actual"]
                    break
            if winner:
                sects = winner.get("sectiontimes", [])
                parsed = [float(s) for s in sects if s and str(s).strip()]
                actual_total = sum(parsed) if parsed else None
            else:
                # Use any runner with full sectionals
                actual_total = None
                for r in all_runners:
                    sects = r.get("sectiontimes", [])
                    parsed = []
                    for s in sects:
                        try:
                            parsed.append(float(s))
                        except (ValueError, TypeError):
                            continue
                    if len(parsed) >= 2:
                        actual_total = sum(parsed)
                        break
            if actual_total is None:
                continue

        # Actual pace deviation: compare leader early split against neutral proportion
        expected_early_frac = (distance - 400) / distance
        actual_early_frac = leader_early / actual_total if actual_total else 0.5
        actual_dev = (actual_early_frac - expected_early_frac) * actual_total

        actual_label = classify_pace_label(actual_dev, distance)

        total += 1
        pred_devs.append(pred_score)
        actual_devs.append(actual_dev)

        pred_idx = label_index.get(pred_pace, 3)
        act_idx = label_index.get(actual_label, 3)

        if pred_idx == act_idx:
            exact_matches += 1
        if abs(pred_idx - act_idx) <= 1:
            within_one += 1

        details.append({
            "race": race_data.get("race_number"),
            "predicted": pred_pace,
            "predicted_dev": round(pred_score, 3),
            "actual": actual_label,
            "actual_dev": round(actual_dev, 3),
            "match": pred_idx == act_idx,
        })

    correlation = None
    if len(pred_devs) >= 3:
        correlation = _pearson(pred_devs, actual_devs)

    return {
        "exact_match_rate": round(exact_matches / total, 3) if total else None,
        "within_one_category": round(within_one / total, 3) if total else None,
        "n_races": total,
        "deviation_correlation": round(correlation, 3) if correlation is not None else None,
        "details": details,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Dimension 3: Pace Beneficiary Analysis
# ══════════════════════════════════════════════════════════════════════════════

def compute_pace_beneficiary(matched_races: List[Dict]) -> Dict:
    """Check if horses predicted to benefit from pace actually outperform."""
    benefit_results = []
    penalised_results = []

    for race_data in matched_races:
        pred_pace = race_data.get("pred_pace", "Normal")
        for pair in race_data["pairs"]:
            style = pair["pred"].get("style", "?")
            rank = pair["pred"]["rank"]
            place = _parse_place(pair["actual"].get("place", ""))
            if place is None:
                continue

            rank_diff = place - rank  # negative = finished better than predicted

            # Determine if this style should benefit in the predicted pace
            is_benefit = _style_benefits_from_pace(style, pred_pace)

            if is_benefit:
                benefit_results.append(rank_diff)
            else:
                penalised_results.append(rank_diff)

    avg_benefit = float(np.mean(benefit_results)) if benefit_results else None
    avg_penalised = float(np.mean(penalised_results)) if penalised_results else None

    return {
        "pace_beneficiary_avg_rank_diff": round(avg_benefit, 2) if avg_benefit is not None else None,
        "pace_penalised_avg_rank_diff": round(avg_penalised, 2) if avg_penalised is not None else None,
        "n_beneficiaries": len(benefit_results),
        "n_penalised": len(penalised_results),
        "benefit_advantage": round((avg_penalised or 0) - (avg_benefit or 0), 2)
            if avg_benefit is not None and avg_penalised is not None else None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Dimension 4: Risk Reflection Accuracy
# ══════════════════════════════════════════════════════════════════════════════

def compute_risk_accuracy(matched_races: List[Dict]) -> Dict:
    """Check if risk predicts prediction error and failure."""
    risk_vs_error = []    # (risk_score, |time_error|)
    tier_results = defaultdict(lambda: {"wins": 0, "places": 0, "total": 0,
                                         "odds_sum": 0})

    for race_data in matched_races:
        for pair in race_data["pairs"]:
            risk = pair["pred"].get("risk_score", 50)
            tier = pair["pred"].get("risk_tier", "Medium")
            proj = pair["pred"].get("projected_time")
            actual_ft = pair["actual"].get("finish_time_seconds")
            place = _parse_place(pair["actual"].get("place", ""))
            odds = _parse_odds(pair["actual"].get("win_odds", ""))
            rank = pair["pred"]["rank"]

            # Only look at top-ranked picks for ROI analysis
            if rank <= 4:
                t = tier_results[tier]
                t["total"] += 1
                if place == 1:
                    t["wins"] += 1
                    if odds:
                        t["odds_sum"] += odds
                if place and place <= 3:
                    t["places"] += 1

            if proj and actual_ft:
                risk_vs_error.append((risk, abs(proj - actual_ft)))

    # Correlation: risk_score vs |time_error|
    corr = None
    if len(risk_vs_error) >= 5:
        risks, errors = zip(*risk_vs_error)
        corr = _pearson(list(risks), list(errors))

    # ROI by tier (flat $10 bet on top-4 picks)
    tier_summary = {}
    for tier_name, data in tier_results.items():
        total = data["total"]
        if total == 0:
            continue
        win_rate = data["wins"] / total
        place_rate = data["places"] / total
        roi = ((data["odds_sum"] * 10) - (total * 10)) / (total * 10) if total else 0
        tier_summary[tier_name] = {
            "n_bets": total,
            "win_rate": round(win_rate, 3),
            "place_rate": round(place_rate, 3),
            "roi_pct": round(roi * 100, 1),
        }

    return {
        "risk_error_correlation": round(corr, 3) if corr is not None else None,
        "n_observations": len(risk_vs_error),
        "tier_performance": tier_summary,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Dimension 5: Running Style Prediction
# ══════════════════════════════════════════════════════════════════════════════

def compute_style_accuracy(matched_races: List[Dict]) -> Dict:
    """Compare predicted dominant_style vs actual running position."""
    confusion = defaultdict(lambda: defaultdict(int))
    exact = 0
    total = 0

    for race_data in matched_races:
        field_size = race_data.get("field_size", 14)
        for pair in race_data["pairs"]:
            pred_style = (pair["pred"].get("style") or "?").strip()
            positions = pair["actual"].get("positions", [])
            actual_style = actual_running_style(positions, field_size)

            if pred_style in ("?", "") or actual_style == "Unknown":
                continue

            total += 1
            # Normalize style names
            pred_norm = _normalize_style(pred_style)
            act_norm = _normalize_style(actual_style)

            confusion[pred_norm][act_norm] += 1
            if pred_norm == act_norm:
                exact += 1

    return {
        "exact_match_rate": round(exact / total, 3) if total else None,
        "n_observations": total,
        "confusion_matrix": {k: dict(v) for k, v in confusion.items()},
    }


# ══════════════════════════════════════════════════════════════════════════════
# Dimension 6: Draw Offset Accuracy
# ══════════════════════════════════════════════════════════════════════════════

def compute_draw_accuracy(matched_races: List[Dict]) -> Dict:
    """Check if predicted draw advantage direction matches actual draw bias."""
    draw_tercile_results = {"inside": [], "middle": [], "outside": []}
    direction_matches = 0
    direction_total = 0

    for race_data in matched_races:
        field_size = race_data.get("field_size", 14)
        if field_size < 6:
            continue

        # Group finish positions by draw tercile
        tercile_positions = {"inside": [], "middle": [], "outside": []}
        for pair in race_data["pairs"]:
            draw = pair["actual"].get("draw") or pair["pred"].get("draw")
            place = _parse_place(pair["actual"].get("place", ""))
            if draw is None or place is None:
                continue
            try:
                draw = int(draw)
            except (ValueError, TypeError):
                continue

            t3 = field_size / 3
            if draw <= t3:
                tercile = "inside"
            elif draw <= 2 * t3:
                tercile = "middle"
            else:
                tercile = "outside"
            tercile_positions[tercile].append(place)

        # Compute average placement by tercile
        for tercile, places in tercile_positions.items():
            if places:
                draw_tercile_results[tercile].append(float(np.mean(places)))

    # Overall draw bias direction
    inside_avg = float(np.mean(draw_tercile_results["inside"])) if draw_tercile_results["inside"] else None
    outside_avg = float(np.mean(draw_tercile_results["outside"])) if draw_tercile_results["outside"] else None

    draw_bias = None
    if inside_avg is not None and outside_avg is not None:
        # Lower avg place = better
        draw_bias = "inside" if inside_avg < outside_avg else "outside" if outside_avg < inside_avg else "neutral"

    return {
        "inside_avg_place": round(inside_avg, 2) if inside_avg is not None else None,
        "middle_avg_place": round(float(np.mean(draw_tercile_results["middle"])), 2) if draw_tercile_results["middle"] else None,
        "outside_avg_place": round(outside_avg, 2) if outside_avg is not None else None,
        "draw_bias_detected": draw_bias,
        "n_races_inside": len(draw_tercile_results["inside"]),
        "n_races_outside": len(draw_tercile_results["outside"]),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Helper functions
# ══════════════════════════════════════════════════════════════════════════════

def _spearman(x: List, y: List) -> Optional[float]:
    """Compute Spearman rank correlation."""
    n = min(len(x), len(y))
    if n < 3:
        return None
    rx = _rank_data(x[:n])
    ry = _rank_data(y[:n])
    return _pearson(rx, ry)


def _rank_data(data: List) -> List[float]:
    """Assign ranks (1-based, average ties)."""
    indexed = sorted(enumerate(data), key=lambda t: t[1])
    ranks = [0.0] * len(data)
    i = 0
    while i < len(indexed):
        j = i
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j + 1) / 2.0
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def _pearson(x: List[float], y: List[float]) -> Optional[float]:
    """Compute Pearson correlation coefficient."""
    n = min(len(x), len(y))
    if n < 3:
        return None
    mx = sum(x[:n]) / n
    my = sum(y[:n]) / n
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    sx = math.sqrt(sum((x[i] - mx) ** 2 for i in range(n)))
    sy = math.sqrt(sum((y[i] - my) ** 2 for i in range(n)))
    if sx == 0 or sy == 0:
        return None
    return cov / (sx * sy)


def _parse_place(place_str) -> Optional[int]:
    """Parse place string to int. Returns None for scratched/DH/etc."""
    s = str(place_str).strip()
    try:
        return int(s)
    except (ValueError, TypeError):
        # Handle "1DH" etc.
        m = re.match(r"(\d+)", s)
        return int(m.group(1)) if m else None


def _parse_odds(odds_str) -> Optional[float]:
    """Parse win odds string to float."""
    s = str(odds_str).strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _normalize_style(style: str) -> str:
    """Normalize style name."""
    s = style.strip().lower()
    if "lead" in s:
        return "Leader"
    if "on" in s and "pace" in s:
        return "On-Pace"
    if "mid" in s:
        return "Midfield"
    if "clos" in s or "back" in s:
        return "Closer"
    if "on-pace" in s or "on_pace" in s:
        return "On-Pace"
    return style.strip()[:8]


def _style_benefits_from_pace(style: str, pace: str) -> bool:
    """Does this running style benefit from the predicted pace?"""
    s = _normalize_style(style)
    p = pace.lower()
    if "fast" in p:
        return s in ("Midfield", "Closer")
    elif "slow" in p:
        return s in ("Leader", "On-Pace")
    return False  # Normal pace — no strong beneficiary


# ══════════════════════════════════════════════════════════════════════════════
# Main backtest pipeline
# ══════════════════════════════════════════════════════════════════════════════

def backtest_meeting(date_compact: str) -> Optional[Dict]:
    """Run full backtest for a single meeting."""
    pred_data = load_prediction(date_compact)
    result_data = load_results(date_compact)

    if not pred_data or not result_data:
        return None

    pred_races = {r["race_number"]: r for r in pred_data.get("races", [])}
    result_races = {r["race_number"]: r for r in result_data.get("races", [])}

    matched_races = []
    for rn, pred_race in pred_races.items():
        if rn not in result_races:
            continue
        result_race = result_races[rn]
        pairs = match_race_runners(pred_race, result_race)
        if not pairs:
            continue

        matched_races.append({
            "race_number": rn,
            "distance": pred_race.get("distance") or result_race.get("distance"),
            "field_size": pred_race.get("runners", len(pairs)),
            "pred_pace": pred_race.get("pace", "Normal"),
            "pred_pace_score": pred_race.get("pace_score", 0),
            "pairs": pairs,
            "all_result_runners": result_race.get("runners", []),
        })

    if not matched_races:
        return None

    return {
        "date": f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}",
        "date_compact": date_compact,
        "meeting_title": pred_data.get("meeting_title", ""),
        "n_races_matched": len(matched_races),
        "n_runners_matched": sum(len(r["pairs"]) for r in matched_races),
        "computed_at": datetime.now().isoformat(),
        "time_accuracy": compute_time_accuracy(matched_races),
        "pace_accuracy": compute_pace_accuracy(matched_races),
        "pace_beneficiary": compute_pace_beneficiary(matched_races),
        "risk_accuracy": compute_risk_accuracy(matched_races),
        "style_accuracy": compute_style_accuracy(matched_races),
        "draw_accuracy": compute_draw_accuracy(matched_races),
    }


def aggregate_backtests(backtests: List[Dict]) -> Dict:
    """Combine multiple meeting backtests into one aggregate report."""
    if not backtests:
        return {}

    # Re-collect all matched races across meetings for proper aggregation
    all_matched = []
    total_races = 0
    total_runners = 0
    dates = []

    for bt in backtests:
        dates.append(bt["date"])
        total_races += bt["n_races_matched"]
        total_runners += bt["n_runners_matched"]

        # Reload data to get raw pairs
        pred = load_prediction(bt["date_compact"])
        result = load_results(bt["date_compact"])
        if not pred or not result:
            continue

        pred_races = {r["race_number"]: r for r in pred.get("races", [])}
        result_races = {r["race_number"]: r for r in result.get("races", [])}

        for rn, pr in pred_races.items():
            if rn not in result_races:
                continue
            pairs = match_race_runners(pr, result_races[rn])
            if pairs:
                all_matched.append({
                    "race_number": rn,
                    "distance": pr.get("distance") or result_races[rn].get("distance"),
                    "field_size": pr.get("runners", len(pairs)),
                    "pred_pace": pr.get("pace", "Normal"),
                    "pred_pace_score": pr.get("pace_score", 0),
                    "pairs": pairs,
                    "all_result_runners": result_races[rn].get("runners", []),
                })

    return {
        "period": f"{dates[0]} to {dates[-1]}" if dates else "",
        "n_meetings": len(backtests),
        "n_races": total_races,
        "n_runners": total_runners,
        "meeting_dates": dates,
        "computed_at": datetime.now().isoformat(),
        "time_accuracy": compute_time_accuracy(all_matched),
        "pace_accuracy": compute_pace_accuracy(all_matched),
        "pace_beneficiary": compute_pace_beneficiary(all_matched),
        "risk_accuracy": compute_risk_accuracy(all_matched),
        "style_accuracy": compute_style_accuracy(all_matched),
        "draw_accuracy": compute_draw_accuracy(all_matched),
        "per_meeting": [{
            "date": bt["date"],
            "title": bt.get("meeting_title", ""),
            "races": bt["n_races_matched"],
            "mae": bt["time_accuracy"].get("mae_seconds"),
            "rank_corr": bt["time_accuracy"].get("rank_correlation_avg"),
            "pace_exact": bt["pace_accuracy"].get("exact_match_rate"),
            "top1_win": bt["time_accuracy"].get("top1_win_rate"),
        } for bt in backtests],
    }


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Backtest HKJC model predictions against actual results.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date", help="Single meeting YYYY-MM-DD")
    group.add_argument("--month", help="Monthly aggregate YYYY-MM")
    group.add_argument("--season", help="Season aggregate YYYY-YYYY")
    group.add_argument("--all", action="store_true", help="All available data")
    args = parser.parse_args()

    REPORTS.mkdir(parents=True, exist_ok=True)

    if args.date:
        dc = args.date.replace("-", "")
        bt = backtest_meeting(dc)
        if not bt:
            print(f"No matched data for {args.date}. Need both prediction + results JSON.")
            sys.exit(1)
        out = REPORTS / f"backtest_{dc}.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(bt, f, ensure_ascii=False, indent=2)
        print(f"✓ Backtest saved: {out.name}")
        _print_summary(bt)

    elif args.month:
        dates = find_meeting_dates(args.month)
        if not dates:
            print(f"No matched meetings for {args.month}")
            sys.exit(1)
        backtests = []
        for dc in dates:
            bt = backtest_meeting(dc)
            if bt:
                backtests.append(bt)
                print(f"  ✓ {bt['date']}: {bt['n_races_matched']} races")
        if not backtests:
            print("No valid backtests produced")
            sys.exit(1)
        agg = aggregate_backtests(backtests)
        month_key = args.month
        out = REPORTS / f"backtest_monthly_{month_key}.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(agg, f, ensure_ascii=False, indent=2)
        print(f"\n✓ Monthly backtest saved: {out.name}")
        _print_aggregate_summary(agg)

    elif args.season:
        dates = find_meeting_dates(args.season)
        if not dates:
            print(f"No matched meetings for season {args.season}")
            sys.exit(1)
        backtests = []
        for dc in dates:
            bt = backtest_meeting(dc)
            if bt:
                backtests.append(bt)
        if not backtests:
            print("No valid backtests produced")
            sys.exit(1)
        agg = aggregate_backtests(backtests)
        out = REPORTS / f"backtest_season_{args.season}.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(agg, f, ensure_ascii=False, indent=2)
        print(f"\n✓ Season backtest saved: {out.name}")
        _print_aggregate_summary(agg)

    elif args.all:
        dates = find_meeting_dates("all")
        if not dates:
            print("No matched meetings found")
            sys.exit(1)
        backtests = []
        for dc in dates:
            bt = backtest_meeting(dc)
            if bt:
                backtests.append(bt)
        if not backtests:
            print("No valid backtests produced")
            sys.exit(1)
        agg = aggregate_backtests(backtests)
        out = REPORTS / f"backtest_all.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(agg, f, ensure_ascii=False, indent=2)
        print(f"\n✓ Full backtest saved: {out.name}")
        _print_aggregate_summary(agg)


def _print_summary(bt: Dict):
    """Print single-meeting backtest summary."""
    ta = bt["time_accuracy"]
    pa = bt["pace_accuracy"]
    print(f"\n{'─'*50}")
    print(f"Meeting: {bt['date']}  ({bt['n_races_matched']} races, "
          f"{bt['n_runners_matched']} matched runners)")
    print(f"{'─'*50}")
    print(f"  1. TIME:  MAE={_fmt(ta.get('mae_seconds'),'s')}  "
          f"Rank ρ={_fmt(ta.get('rank_correlation_avg'))}  "
          f"Top1 Win={_fmt(ta.get('top1_win_rate'),'%')}")
    print(f"  2. PACE:  Exact={_fmt(pa.get('exact_match_rate'),'%')}  "
          f"±1 Cat={_fmt(pa.get('within_one_category'),'%')}  "
          f"r={_fmt(pa.get('deviation_correlation'))}")
    pb = bt["pace_beneficiary"]
    print(f"  3. BENEFICIARY: Adv={_fmt(pb.get('benefit_advantage'))} ranks")
    ra = bt["risk_accuracy"]
    print(f"  4. RISK:  Error corr={_fmt(ra.get('risk_error_correlation'))}")
    for tier, perf in ra.get("tier_performance", {}).items():
        print(f"     {tier}: Win={_fmt(perf.get('win_rate'),'%')} "
              f"Place={_fmt(perf.get('place_rate'),'%')} "
              f"ROI={_fmt(perf.get('roi_pct'),'%')} (n={perf.get('n_bets')})")
    sa = bt["style_accuracy"]
    print(f"  5. STYLE: Exact match={_fmt(sa.get('exact_match_rate'),'%')}")
    da = bt["draw_accuracy"]
    print(f"  6. DRAW:  Inside avg={_fmt(da.get('inside_avg_place'))} "
          f"Outside avg={_fmt(da.get('outside_avg_place'))} "
          f"Bias={da.get('draw_bias_detected', '?')}")


def _print_aggregate_summary(agg: Dict):
    """Print aggregate backtest summary."""
    print(f"\n{'═'*50}")
    print(f"AGGREGATE: {agg.get('n_meetings',0)} meetings, "
          f"{agg.get('n_races',0)} races, "
          f"{agg.get('n_runners',0)} runners")
    print(f"Period: {agg.get('period','')}")
    print(f"{'═'*50}")
    ta = agg.get("time_accuracy", {})
    pa = agg.get("pace_accuracy", {})
    print(f"  1. TIME:  MAE={_fmt(ta.get('mae_seconds'),'s')}  "
          f"Rank ρ={_fmt(ta.get('rank_correlation_avg'))}  "
          f"Top1={_fmt(ta.get('top1_win_rate'),'%')}")
    print(f"  2. PACE:  Exact={_fmt(pa.get('exact_match_rate'),'%')}  "
          f"±1={_fmt(pa.get('within_one_category'),'%')}")
    pb = agg.get("pace_beneficiary", {})
    print(f"  3. BENEFICIARY: Adv={_fmt(pb.get('benefit_advantage'))}")
    ra = agg.get("risk_accuracy", {})
    print(f"  4. RISK:  r={_fmt(ra.get('risk_error_correlation'))}")
    sa = agg.get("style_accuracy", {})
    print(f"  5. STYLE: Exact={_fmt(sa.get('exact_match_rate'),'%')}")
    da = agg.get("draw_accuracy", {})
    print(f"  6. DRAW:  Bias={da.get('draw_bias_detected','?')}")
    print()
    if agg.get("per_meeting"):
        print("Per-meeting breakdown:")
        for m in agg["per_meeting"]:
            print(f"  {m['date']}: MAE={_fmt(m.get('mae'),'s')} "
                  f"ρ={_fmt(m.get('rank_corr'))} "
                  f"Pace={_fmt(m.get('pace_exact'),'%')} "
                  f"Top1={_fmt(m.get('top1_win'),'%')}")


def _fmt(val, suffix="") -> str:
    if val is None:
        return "—"
    if suffix == "%":
        return f"{val*100:.1f}%" if isinstance(val, float) and val <= 1 else f"{val}%"
    if suffix == "s":
        return f"{val:.3f}s"
    return f"{val:.3f}" if isinstance(val, float) else str(val)


if __name__ == "__main__":
    main()
