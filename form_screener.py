"""
form_screener.py — multi-pass form review for an HKJC meeting.

Mirrors the manual screening process described in
.github/prompts/skills/hkjc-form-screener/SKILL.md:

  Pass 1 — per-horse form scoring (conditions, draw, weight, rating,
           class, distance, margin, time-vs-par, incidents, blackbook)
  Pass 2 — strength of competition (collateral form via top5_next)
  Pass 3 — head-to-head & weight-swing vs today's rivals

Plus extras E1, E3, E4, E5 (post-race), E7, E8, E9, E11, E12.

Pure logic — no Streamlit imports. The dashboard and the VS Code agent
both call ``run_screener(date_iso)``.

Output: ``reports/form_screen_<YYYYMMDD>.json`` (schema_version=1).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict, Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "hkjc.db"
RACECARD_DIR = REPO_ROOT / "racecards"
CACHE_DIR = REPO_ROOT / "cache"
REPORTS_DIR = REPO_ROOT / "reports"
BLACKBOOK_PATH = REPO_ROOT / "blackbook.json"

SCHEMA_VERSION = 2   # v2: dropped form_score, added trial_lines + status
HKT = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# Going families — used for "similar conditions" matching
# ---------------------------------------------------------------------------

GOING_FAMILY = {
    "G": "good", "GF": "good", "GY": "rain", "Y": "rain",
    "YS": "rain", "S": "rain", "H": "rain",
    "Fast": "awt", "Good": "good", "Soft": "rain", "Wet Slow": "awt",
    "Wet Fast": "awt",
}


def going_family(g: str) -> str:
    if not g:
        return "unknown"
    return GOING_FAMILY.get(str(g).strip(), "unknown")


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------

_LBW_FRACTIONS = {
    "SH": 0.05, "HD": 0.1, "SHD": 0.05, "NK": 0.25, "NSE": 0.05,
    "1/2": 0.5, "3/4": 0.75, "1/4": 0.25, "DH": 0.0,
}


def lbw_to_lengths(lbw: Any) -> float | None:
    """Parse HKJC margin strings ("2-1/4", "SH", "DNF") to a float."""
    if lbw is None:
        return None
    s = str(lbw).strip().upper()
    if not s or s in {"DNF", "PU", "WD"}:
        return None
    if s in _LBW_FRACTIONS:
        return _LBW_FRACTIONS[s]
    # "2-1/4"  -> 2.25;  "10-3/4" -> 10.75
    m = re.match(r"^(\d+)\s*[- ]\s*(\d+/\d+|SH|HD|NK|NSE)?$", s)
    if m:
        whole = float(m.group(1))
        frac_str = (m.group(2) or "").strip()
        frac = 0.0
        if frac_str in _LBW_FRACTIONS:
            frac = _LBW_FRACTIONS[frac_str]
        elif "/" in frac_str:
            try:
                a, b = frac_str.split("/")
                frac = float(a) / float(b)
            except Exception:
                frac = 0.0
        return whole + frac
    try:
        return float(s)
    except Exception:
        return None


def parse_claim(jockey_raw: str) -> int:
    """Extract apprentice claim (lbs) from "Name (-N)"."""
    if not jockey_raw:
        return 0
    m = re.search(r"\(-(\d+)\)", str(jockey_raw))
    return int(m.group(1)) if m else 0


def clean_jockey(jockey_raw: str) -> str:
    if not jockey_raw:
        return ""
    return re.sub(r"\s*\(-\d+\)", "", str(jockey_raw)).strip()


def parse_time_to_seconds(t: Any) -> float | None:
    """Parse "1:09.96" or "69.96" → seconds float."""
    if t is None:
        return None
    s = str(t).strip()
    if not s or s in {"-", "--"}:
        return None
    if ":" in s:
        try:
            m, rest = s.split(":", 1)
            return int(m) * 60.0 + float(rest)
        except Exception:
            return None
    try:
        return float(s)
    except Exception:
        return None


def draw_band(draw: Any, field_size_hint: int = 12) -> str:
    try:
        d = int(float(draw))
    except Exception:
        return "unknown"
    if d <= 0:
        return "unknown"
    if d <= 4:
        return "inside"
    if d <= 9:
        return "middle"
    return "wide"


def distance_band(dist: Any) -> str:
    try:
        m = int(float(dist))
    except Exception:
        return "unknown"
    if m <= 1100:
        return "sprint"
    if m <= 1400:
        return "short_mile"
    if m <= 1800:
        return "mile"
    if m <= 2200:
        return "middle"
    return "stayer"


def freshness_band(days: Any) -> str:
    try:
        d = float(days)
    except Exception:
        return "unknown"
    if d < 0:
        return "unknown"
    if d <= 14:
        return "fresh"
    if d <= 35:
        return "normal"
    if d <= 84:
        return "let-up"
    return "spell"


# ---------------------------------------------------------------------------
# DB access (read-only)
# ---------------------------------------------------------------------------

def _connect_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"{DB_PATH} missing — head-to-head will fail")
    return sqlite3.connect(str(DB_PATH))


def fetch_horse_history(con: sqlite3.Connection, horse_name: str,
                        *, before_date: str | None = None,
                        limit: int = 30) -> list[dict]:
    sql = ("SELECT race_date, race_number, place, actual_weight, "
           "declared_weight, draw, going, distance, race_class, "
           "race_track, rating, finish_time_seconds, lbw, jockey, "
           "gear, sectiontimes "
           "FROM results WHERE UPPER(horse_name)=? ")
    args: list[Any] = [horse_name.upper()]
    if before_date:
        sql += "AND race_date < ? "
        args.append(before_date)
    sql += "ORDER BY race_date DESC LIMIT ?"
    args.append(limit)
    cur = con.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_class_par_time(con: sqlite3.Connection, *, distance: int,
                         race_class: int, track: str,
                         lookback_days: int = 90) -> float | None:
    """Median winning time for a (distance, class, track) over last N days."""
    cutoff = (datetime.now(HKT).date() - timedelta(days=lookback_days)).isoformat()
    cur = con.execute(
        "SELECT finish_time_seconds FROM results "
        "WHERE distance=? AND race_class=? AND race_track=? "
        "AND place='1' AND race_date>=? AND finish_time_seconds IS NOT NULL",
        (distance, race_class, track, cutoff),
    )
    times = [r[0] for r in cur.fetchall() if r[0]]
    if len(times) < 3:
        return None
    times.sort()
    return times[len(times) // 2]


# ---------------------------------------------------------------------------
# Pass 1 — per-run scoring
# ---------------------------------------------------------------------------

@dataclass
class FormLine:
    date: str
    race_no: int | None
    distance: int | None
    going: str | None
    track: str | None
    race_class: int | None
    place: str | None
    margin_lengths: float | None
    rating: int | None
    actual_weight: int | None
    draw: int | None
    jockey: str | None
    time_sec: float | None
    conditions_match: float
    class_move_vs_today: str  # "UP" | "SAME" | "DOWN"
    incident_tags: list[str] = field(default_factory=list)
    incident_polarity: int | None = None
    top5_next_score: float = 0.0  # collateral (Pass 2)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _conditions_match(run: dict, today: dict) -> float:
    """Score 0–1 — how similar this prior run is to today's race."""
    s = 0.0
    # Going family (0.3)
    if going_family(run.get("going")) == going_family(today.get("going")):
        s += 0.30
    # Distance band (0.3)
    if distance_band(run.get("distance")) == distance_band(today.get("distance")):
        s += 0.30
    # Track ST/HV (0.2)
    if str(run.get("track") or "").upper() == str(today.get("track") or "").upper():
        s += 0.20
    # Class within ±1 (0.2)
    try:
        rc = int(run.get("class") or run.get("race_class") or 0)
        tc = int(today.get("race_class") or 0)
        if rc and tc and abs(rc - tc) <= 1:
            s += 0.20
    except Exception:
        pass
    return round(s, 2)


def _class_move(run_class: Any, today_class: Any) -> str:
    try:
        rc = int(run_class)
        tc = int(today_class)
    except Exception:
        return "UNKNOWN"
    if tc < rc:
        return "UP"   # numerically lower class number = higher class
    if tc > rc:
        return "DOWN"
    return "SAME"


# ---------------------------------------------------------------------------
# Pass 2 — collateral form via top5_next
# ---------------------------------------------------------------------------

def _collateral_score(run: dict, horse_place: int | None) -> float:
    """Use top5 + top5_next to score the strength of THIS form-line.

    Positive if the field around the horse has continued to win/place since;
    negative if the field has gone unraced or flopped.
    """
    top5 = run.get("top5") or []
    top5_next = run.get("top5_next") or []
    if not top5 or not top5_next:
        return 0.0

    score = 0.0
    for (place, name), nxt in zip(top5, top5_next):
        if not nxt:
            continue
        try:
            nxt_pl = int(nxt.get("place"))
        except Exception:
            continue
        # Was this rival placed next time?
        if nxt_pl == 1:
            weight = 1.0
        elif nxt_pl <= 3:
            weight = 0.6
        elif nxt_pl <= 5:
            weight = 0.2
        else:
            weight = -0.2

        # Direction depends on whether our horse beat or lost to this rival.
        if horse_place is not None:
            if place > horse_place:
                # rival finished BEHIND us → good when rival validates
                score += weight
            elif place < horse_place:
                # rival finished AHEAD of us → strong form line if they validated
                score += weight * 0.5
    return round(score, 2)


# ---------------------------------------------------------------------------
# Pass 3 — head-to-head & weight swing
# ---------------------------------------------------------------------------

def _normalize_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().upper())


def head_to_head(con: sqlite3.Connection,
                 horse_a: str, horse_b: str,
                 *, before_date: str) -> list[dict]:
    """All prior meetings where horse_a and horse_b ran in the same race."""
    sql = (
        "SELECT a.race_date, a.race_number, a.distance, a.going, "
        "a.race_class, "
        "a.place AS a_place, a.actual_weight AS a_weight, a.rating AS a_rating, "
        "b.place AS b_place, b.actual_weight AS b_weight, b.rating AS b_rating, "
        "a.lbw AS a_lbw, b.lbw AS b_lbw "
        "FROM results a JOIN results b "
        "ON a.race_date=b.race_date AND a.race_number=b.race_number "
        "WHERE UPPER(a.horse_name)=? AND UPPER(b.horse_name)=? "
        "AND a.race_date < ? "
        "ORDER BY a.race_date DESC LIMIT 10"
    )
    cur = con.execute(sql, (_normalize_name(horse_a),
                            _normalize_name(horse_b), before_date))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["a_margin"] = lbw_to_lengths(r.get("a_lbw"))
        r["b_margin"] = lbw_to_lengths(r.get("b_lbw"))
    return rows


def weight_swing(a_today_weight: int, b_today_weight: int,
                 prior: dict) -> dict[str, Any]:
    """Compute weight swing today vs a single prior meeting between A and B."""
    try:
        prior_delta = int(prior["a_weight"]) - int(prior["b_weight"])
        today_delta = int(a_today_weight) - int(b_today_weight)
    except Exception:
        return {"swing_lbs": None}
    # +ve swing means A gets MORE relief today than at the prior meeting.
    swing = prior_delta - today_delta
    return {
        "prior_a_weight": prior.get("a_weight"),
        "prior_b_weight": prior.get("b_weight"),
        "today_a_weight": a_today_weight,
        "today_b_weight": b_today_weight,
        "swing_lbs": swing,
    }


# ---------------------------------------------------------------------------
# Extras
# ---------------------------------------------------------------------------

def gear_change(today_gear: str, last_gear: str | None) -> str | None:
    """E1 — flag if gear differs from last race."""
    today_gear = (today_gear or "").strip()
    last_gear = (last_gear or "").strip()
    if not today_gear and not last_gear:
        return None
    if today_gear and not last_gear:
        return f"GEAR_ON:{today_gear}"
    if last_gear and not today_gear:
        return f"GEAR_OFF:{last_gear}"
    if today_gear != last_gear:
        return f"GEAR_CHANGE:{last_gear}->{today_gear}"
    return None


def run_up_pattern(days_since_last: Any, prior_runs: list[dict]) -> str:
    """E3 — first-up / 2nd-up / 3rd-up classification."""
    fresh = freshness_band(days_since_last)
    if fresh == "spell":
        return "1st-up"
    if fresh == "let-up":
        return "1st-up_after_letup"
    if not prior_runs:
        return "unknown"
    # Look at the gap BEFORE the most recent run to decide 2nd-up vs 3rd-up
    try:
        last_date = datetime.fromisoformat(str(prior_runs[0].get("date")))
        if len(prior_runs) > 1:
            prev_date = datetime.fromisoformat(str(prior_runs[1].get("date")))
            gap = (last_date - prev_date).days
            if gap > 84:
                return "2nd-up"
            if gap > 35 and len(prior_runs) > 2:
                third_date = datetime.fromisoformat(str(prior_runs[2].get("date")))
                gap2 = (prev_date - third_date).days
                if gap2 > 84:
                    return "3rd-up"
    except Exception:
        pass
    return "in-form_cycle"


def trip_change(today_distance: int, prior_runs: list[dict]) -> str:
    """E4 — distance change vs the most-frequent prior trip."""
    if not prior_runs:
        return "first_run"
    dists = [r.get("distance") for r in prior_runs if r.get("distance")]
    if not dists:
        return "unknown"
    most_common = Counter(dists).most_common(1)[0][0]
    delta = int(today_distance) - int(most_common)
    if delta == 0:
        return "SAME_TRIP"
    return f"{'UP' if delta>0 else 'DOWN'}_{abs(delta)}m_vs_{most_common}m"


def sectional_vs_par(time_sec: float | None, par: float | None) -> dict | None:
    """E7 — simple overall-time delta vs class median win time."""
    if time_sec is None or par is None:
        return None
    return {"time_sec": time_sec, "par_sec": par,
            "delta_sec": round(time_sec - par, 2)}


def going_specific_record(prior_runs: list[dict], today_going: str) -> str:
    """E8 — '3-1-0 from 4 starts on Y+'."""
    fam = going_family(today_going)
    w = p = s = n = 0
    for r in prior_runs:
        if going_family(r.get("going")) != fam:
            continue
        n += 1
        try:
            pl = int(r.get("place"))
        except Exception:
            continue
        if pl == 1:
            w += 1
        elif pl == 2:
            p += 1
        elif pl == 3:
            s += 1
    return f"{w}-{p}-{s} from {n} starts ({fam})"


# ---------------------------------------------------------------------------
# Incident / blackbook helpers
# ---------------------------------------------------------------------------

def _load_commentary(date_iso: str) -> dict:
    yyyymmdd = date_iso.replace("-", "")
    path = REPORTS_DIR / f"commentary_{yyyymmdd}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _incident_for(commentary: dict, race_no: int, horse_name: str) -> dict:
    if not commentary:
        return {}
    races = commentary.get("races") if isinstance(commentary, dict) else None
    if not races:
        return {}
    for r in races:
        if int(r.get("race_number", 0)) != int(race_no):
            continue
        for h in r.get("horses", []):
            if _normalize_name(h.get("horse_name")) == _normalize_name(horse_name):
                return h
    return {}


def _load_blackbook() -> list[dict]:
    if not BLACKBOOK_PATH.exists():
        return []
    try:
        data = json.loads(BLACKBOOK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("entries") or data.get("horses") or []
    return []


def _load_trial_index() -> dict:
    """horse_name_upper -> list of trial entries (most-recent first).

    Mirrors dashboard._load_all_trial_horse_index but kept self-contained
    so the screener has no dashboard dependency.
    """
    index: dict[str, list] = {}
    if not REPORTS_DIR.exists():
        return index
    for f in sorted(REPORTS_DIR.glob("trials_*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        trial_date = data.get("date", "")
        for batch in data.get("batches", []):
            for h in batch.get("horses", []):
                nm = (h.get("horse_name") or "").strip().upper()
                if not nm:
                    continue
                rps = h.get("running_positions") or []
                final_pos = rps[-1] if rps else None
                index.setdefault(nm, []).append({
                    "date": trial_date,
                    "course": batch.get("course", ""),
                    "distance_m": batch.get("distance_m", 0),
                    "going": batch.get("going", ""),
                    "draw": h.get("draw"),
                    "gear": h.get("gear", ""),
                    "lbw": h.get("lbw", "-"),
                    "final_pos": final_pos,
                    "running_positions": rps,
                    "time": h.get("time", ""),
                    "n_horses": batch.get("n_horses", 0),
                    "comment": h.get("comment", ""),
                })
    return index


def _blackbook_for(blackbook: list[dict], horse_name: str,
                   today: str) -> dict | None:
    nn = _normalize_name(horse_name)
    for e in blackbook:
        if _normalize_name(e.get("horse_name")) != nn:
            continue
        status = (e.get("status") or "").lower()
        if status == "archived":
            continue
        expiry = e.get("expiry_date")
        if expiry and expiry < today:
            continue
        return e
    return None


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------

def _load_form_guide(date_iso: str) -> dict:
    path = CACHE_DIR / f"form_guide_{date_iso}.json"
    if not path.exists():
        # Try to rebuild via build_form_guide
        try:
            import build_form_guide  # type: ignore
            if hasattr(build_form_guide, "build_for_date"):
                build_form_guide.build_for_date(date_iso)
        except Exception:
            pass
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found and could not be auto-rebuilt; "
            f"run build_form_guide.py --date {date_iso}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _load_racecard(date_iso: str):
    import pandas as pd
    yyyymmdd = date_iso.replace("-", "")
    path = RACECARD_DIR / f"racecard_{yyyymmdd}.xlsx"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing — scrape first")
    df = pd.read_excel(path, sheet_name="All Races")
    # Replace NaN with None so downstream .strip()/.upper() don't blow up.
    return df.astype(object).where(df.notna(), None)


def _s(v) -> str:
    """NaN/None-safe string coercion."""
    if v is None:
        return ""
    try:
        import math
        if isinstance(v, float) and math.isnan(v):
            return ""
    except Exception:
        pass
    return str(v)


def screen_horse(horse_fg: dict, today: dict, racecard_row: dict,
                 con: sqlite3.Connection, commentary: dict,
                 blackbook: list[dict], date_iso: str,
                 *, rivals: list[dict],
                 trial_index: dict | None = None) -> dict:
    """Build a HorseScreen dict for one horse."""
    name = horse_fg.get("horse_name") or racecard_row.get("horse_name")
    runs = horse_fg.get("runs") or []
    trial_index = trial_index or {}

    # Apprentice claim → effective weight today
    jockey_raw = racecard_row.get("jockey") or horse_fg.get("jockey") or ""
    claim = parse_claim(jockey_raw)
    declared = int(racecard_row.get("weight") or horse_fg.get("weight") or 0)
    actual_today = max(declared - claim, 0)

    today_gear = _s(racecard_row.get("gear")).strip()
    last_gear = None  # form guide doesn't carry gear; DB does.
    if name:
        hist = fetch_horse_history(con, name, before_date=date_iso, limit=12)
        if hist:
            last_gear = _s(hist[0].get("gear")).strip()
    else:
        hist = []

    today_ctx = {
        "going": today.get("going"),
        "distance": today.get("distance"),
        "race_class": today.get("race_class"),
        "track": today.get("track"),
    }

    # Pass 1 — per form line
    form_lines: list[dict] = []
    for run in runs:
        try:
            horse_place = int(run.get("place"))
        except Exception:
            horse_place = None
        fl = FormLine(
            date=str(run.get("date") or ""),
            race_no=run.get("race_number"),
            distance=run.get("distance"),
            going=run.get("going"),
            track=run.get("track"),
            race_class=run.get("class") or run.get("race_class"),
            place=str(run.get("place") or ""),
            margin_lengths=lbw_to_lengths(run.get("margin")),
            rating=int(run["rating"]) if str(run.get("rating") or "").isdigit() else None,
            actual_weight=int(run["actual_weight"]) if str(run.get("actual_weight") or "").isdigit() else None,
            draw=int(run["draw"]) if str(run.get("draw") or "").isdigit() else None,
            jockey=run.get("jockey"),
            time_sec=parse_time_to_seconds(run.get("time")),
            conditions_match=_conditions_match(run, today_ctx),
            class_move_vs_today=_class_move(run.get("class") or run.get("race_class"),
                                            today_ctx["race_class"]),
            top5_next_score=_collateral_score(run, horse_place),
        )
        inc = _incident_for(_load_commentary(str(run.get("date") or "")),
                            run.get("race_number", 0), name or "")
        if inc:
            fl.incident_tags = inc.get("tags") or []
            fl.incident_polarity = inc.get("polarity_score")
        form_lines.append(fl.to_dict())

    # Pass 2 — overall collateral score (weighted by recency)
    collateral = 0.0
    for i, fl in enumerate(form_lines):
        decay = 1.0 / (1 + i)
        collateral += fl["top5_next_score"] * decay
    collateral = round(collateral, 2)

    # Pass 3 — head-to-head vs each rival in today's race
    h2h: list[dict] = []
    for rival in rivals:
        if not rival or rival.get("horse_name") == name:
            continue
        meets = head_to_head(con, name, rival["horse_name"],
                             before_date=date_iso)
        if not meets:
            continue
        swings = []
        for m in meets:
            swings.append({**m, **weight_swing(actual_today,
                                              rival.get("actual_today", 0), m)})
        h2h.append({
            "rival": rival["horse_name"],
            "rival_actual_today": rival.get("actual_today"),
            "meetings": swings,
        })

    # Extras
    extras = {
        "E1_gear_change": gear_change(today_gear, last_gear),
        "E3_run_up": run_up_pattern(racecard_row.get("days_since_last"), runs),
        "E4_trip_change": trip_change(int(today.get("distance") or 0), runs),
        "E8_going_record": going_specific_record(runs, today.get("going")),
        "E9_freshness": freshness_band(racecard_row.get("days_since_last")),
    }
    # E7 — sectional / time vs par on most recent run
    if runs:
        last_run = runs[0]
        try:
            par = fetch_class_par_time(
                con,
                distance=int(last_run.get("distance") or 0),
                race_class=int(last_run.get("class") or 0),
                track=_s(last_run.get("track")),
            )
        except (ValueError, TypeError):
            par = None  # non-numeric class (e.g. "Griffin Race", "Group 1")
        sv = sectional_vs_par(parse_time_to_seconds(last_run.get("time")), par)
        if sv:
            extras["E7_time_vs_class_par"] = sv

    # Conditions score = mean of top-3 form-line condition matches
    cond_scores = sorted([fl["conditions_match"] for fl in form_lines],
                         reverse=True)[:3]
    conditions = round(sum(cond_scores) / len(cond_scores), 2) if cond_scores else 0.0

    # Aggregate form-screen score 0..1 (rule-based heuristic)
    score = (
        0.35 * conditions
        + 0.25 * max(min((collateral + 2) / 4, 1.0), 0.0)
        + 0.15 * (1.0 if extras["E9_freshness"] in {"normal", "fresh"} else 0.5)
        + 0.10 * (1.0 if extras["E8_going_record"].startswith(("1-", "2-", "3-", "4-", "5-")) else 0.3)
        + 0.15 * (1.0 if extras["E1_gear_change"] else 0.5)
    )
    score = round(score, 3)

    bb = _blackbook_for(blackbook, name or "", date_iso)

    # Trial lines — for PPGs without HK race form, or to enrich any horse.
    nm_key = (name or "").strip().upper()
    raw_trials = trial_index.get(nm_key, []) if nm_key else []
    trial_lines = []
    for t in raw_trials[:6]:                 # cap to 6 most-recent trials
        if str(t.get("date", "")) >= date_iso:
            continue                          # ignore trials on/after the meeting
        trial_lines.append({
            "date": t.get("date"),
            "course": t.get("course"),
            "distance": t.get("distance_m"),
            "going": t.get("going"),
            "final_pos": t.get("final_pos"),
            "n_horses": t.get("n_horses"),
            "lbw": t.get("lbw"),
            "time": t.get("time"),
            "gear": t.get("gear"),
            "comment": t.get("comment"),
        })

    # Status — single source of truth for downstream renderers.
    if runs:
        status = "raced"
    elif trial_lines:
        status = "trial_only"
    else:
        status = "true_debutant"

    return {
        "horse_name": name,
        "horse_no": racecard_row.get("horse_no") or horse_fg.get("horse_no"),
        "horse_id": racecard_row.get("horse_id"),
        "draw": int(racecard_row.get("draw")) if racecard_row.get("draw") else None,
        "draw_band": draw_band(racecard_row.get("draw")),
        "declared_weight": declared,
        "claim": claim,
        "actual_weight_today": actual_today,
        "jockey": clean_jockey(jockey_raw),
        "trainer": racecard_row.get("trainer") or horse_fg.get("trainer"),
        "rating_today": int(racecard_row.get("rating") or 0) or None,
        "status": status,                    # raced | trial_only | true_debutant
        "is_standby": bool(racecard_row.get("is_standby")),
        # _rank_score is INTERNAL (used to order shortlist only); not a "rating".
        "_rank_score": score,
        "sub_scores": {                       # raw component evidence
            "conditions_match": conditions,
            "collateral_strength": collateral,
        },
        "extras": extras,
        "blackbook": ({
            "status": bb.get("status"),
            "tags": bb.get("tags"),
            "reasoning": bb.get("reasoning"),
            "confidence": bb.get("confidence"),
        } if bb else None),
        "form_lines": form_lines,
        "trial_lines": trial_lines,
        "head_to_head": h2h,
    }


def _stable_mate_groups(horses: list[dict]) -> list[list[str]]:
    """E11 — list of (trainer, [horse names]) where trainer fields >1 horse."""
    by_trainer: dict[str, list[str]] = defaultdict(list)
    for h in horses:
        t = (h.get("trainer") or "").strip()
        if t:
            by_trainer[t].append(h.get("horse_name") or "")
    return [[t, names] for t, names in by_trainer.items() if len(names) > 1]


def _shortlist(scored_horses: list[dict], top_n: int = 3) -> list[dict]:
    # Only consider horses with usable evidence (race form OR trials).
    eligible = [h for h in scored_horses if h.get("status") != "true_debutant"]
    pool = eligible if eligible else scored_horses
    ranked = sorted(pool, key=lambda x: x.get("_rank_score", 0.0), reverse=True)
    out = []
    for h in ranked[:top_n]:
        bits = []
        sub = h.get("sub_scores", {})
        bits.append(f"cond={sub.get('conditions_match', 0)}")
        bits.append(f"coll={sub.get('collateral_strength', 0)}")
        if h["extras"].get("E1_gear_change"):
            bits.append(h["extras"]["E1_gear_change"])
        if h["extras"].get("E3_run_up") not in {"in-form_cycle", "unknown"}:
            bits.append(h["extras"]["E3_run_up"])
        if h["blackbook"]:
            bits.append("BLACKBOOK")
        out.append({
            "horse": h["horse_name"],
            "rationale": " | ".join(bits),
        })
    return out


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def run_screener(date_iso: str, *, rebuild: bool = False,
                 write: bool = True) -> dict:
    """Screen every horse on the meeting and emit the JSON report."""
    yyyymmdd = date_iso.replace("-", "")
    out_path = REPORTS_DIR / f"form_screen_{yyyymmdd}.json"
    if out_path.exists() and not rebuild:
        try:
            cached = json.loads(out_path.read_text(encoding="utf-8"))
            if cached.get("schema_version") == SCHEMA_VERSION:
                cached["_loaded_from_cache"] = True
                return cached
        except Exception:
            pass

    fg = _load_form_guide(date_iso)
    racecard = _load_racecard(date_iso)
    blackbook = _load_blackbook()
    trial_index = _load_trial_index()
    con = _connect_db()

    races_out: list[dict] = []
    for r in fg.get("races", []):
        race_no = int(r.get("race_number"))
        rc_rows = racecard[racecard["race_number"] == race_no].to_dict("records")
        rc_by_name = {_normalize_name(row.get("horse_name")): row for row in rc_rows}

        today_ctx = {
            "distance": r.get("distance"),
            "going": rc_rows[0].get("going") if rc_rows else None,
            "track": rc_rows[0].get("racecourse") if rc_rows else None,
            "race_class": r.get("race_class"),
        }

        # Build rivals list first so head-to-head can use today's weights.
        rivals_for_today: list[dict] = []
        for h in r.get("horses", []):
            row = rc_by_name.get(_normalize_name(h.get("horse_name"))) or {}
            if row.get("is_standby"):
                continue
            claim = parse_claim(row.get("jockey"))
            declared = int(row.get("weight") or 0)
            rivals_for_today.append({
                "horse_name": h.get("horse_name"),
                "actual_today": max(declared - claim, 0),
            })

        scored = []
        for h in r.get("horses", []):
            row = rc_by_name.get(_normalize_name(h.get("horse_name")))
            if not row:
                continue
            if row.get("is_standby"):
                continue
            commentary = {}  # commentary is per-prior-date, loaded inside form lines
            screen = screen_horse(h, today_ctx, row, con, commentary,
                                  blackbook, date_iso,
                                  rivals=rivals_for_today,
                                  trial_index=trial_index)
            scored.append(screen)

        races_out.append({
            "race_no": race_no,
            "race_name": r.get("race_name"),
            "distance": r.get("distance"),
            "going": today_ctx["going"],
            "track": today_ctx["track"],
            "race_class": r.get("race_class"),
            "stable_mates": _stable_mate_groups(scored),
            "shortlist": _shortlist(scored, top_n=3),
            "horses": scored,
        })

    con.close()

    payload = {
        "schema_version": SCHEMA_VERSION,
        "date_iso": date_iso,
        "generated_at": datetime.now(HKT).isoformat(),
        "races": races_out,
    }

    if write:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                            encoding="utf-8")

    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--rebuild", action="store_true")
    args = p.parse_args()
    res = run_screener(args.date, rebuild=args.rebuild)
    n_horses = sum(len(r["horses"]) for r in res["races"])
    print(f"OK — {len(res['races'])} races, {n_horses} horses screened, "
          f"written to reports/form_screen_{args.date.replace('-','')}.json")
