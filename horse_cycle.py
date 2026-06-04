"""horse_cycle.py — Class-dropper / competitive-rating engine (v0.1)

For each horse on an upcoming card, classify its "cycle state" — where its
current rating sits relative to its historic winning rating band on similar
course/distance/surface. Output flags horses that are PRIMED (returning to
their competitive window) or OVERRATED (likely to fade at career-high).

This is the universal mechanism described in Trainer intention.txt:
    win → rating inflated → uncompetitive → losses (rating −2/run) →
    rating decays back toward historic winning band → wins again

The engine reads:
    - hkjc.db (historic results, per-race rating column)
    - racecards/racecard_YYYYMMDD.xlsx (today's runners + current_rating)

Output: JSON file horse_cycle_<date>.json with per-horse classification.
"""
from __future__ import annotations
import json
import sqlite3
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import pandas as pd

# ── Tunable thresholds ───────────────────────────────────────────────────
BAND_TIGHT = 2          # within ±BAND_TIGHT pts of historic mean = at_band
BAND_LOOSE = 5          # within ±BAND_LOOSE pts = "approaching band"
PEAK_OVER  = 3          # current_rating > max_hist_win + PEAK_OVER = at_peak
MIN_HIST_WINS_FOR_BAND = 2  # need ≥2 wins to compute a band
DECLINE_LOOKBACK = 4    # last N starts to detect declining rating trajectory
DECLINE_MIN_DROP = 3    # rating must have dropped ≥ this over lookback to count
DIST_TOL_M     = 100    # distance match tolerance (metres)
PRIMED_MIN_RUNS_SINCE_WIN = 3

# ── Data classes ─────────────────────────────────────────────────────────
@dataclass
class HorseCycle:
    horse_id: str
    horse_name: str
    trainer: str
    race_number: int
    today_venue: str
    today_surface: str
    today_distance: int
    today_class: Optional[str]
    today_rating: Optional[float]
    n_hist_runs: int
    n_hist_wins: int
    # Band over ALL wins (any conditions)
    band_overall_mean: Optional[float]
    band_overall_min: Optional[float]
    band_overall_max: Optional[float]
    # Band over wins at MATCHING venue+surface+~distance
    band_match_mean: Optional[float]
    band_match_n: int
    course_dist_match: bool
    runs_since_last_win: Optional[int]
    last_win_rating: Optional[float]
    rating_trend_declining: bool
    rating_drop_last_n: float       # +ve if dropped recently
    cycle_state: str                # at_peak / overrated / approaching / at_band / below_band / unknown
    primary_flag: str               # PRIMED / EARLY_DROPPER / FADE_OVERRATED / WATCH / NEUTRAL / NO_DATA
    note: str                       # human-readable summary
    # Recent PLACE/competitive-form overlay (credits strong runs without a win).
    # Wins drive the rating band above; this scores how well the horse has been
    # *performing* lately (placings, beaten-lengths) so consistent placers that
    # have not won still surface a signal.
    recent_starts: int = 0
    recent_top3: int = 0
    recent_place_score: float = 0.0
    place_form: str = ""            # HOT / WARM / ""
    place_note: str = ""
    # Stable overlay (filled by analyse_card)
    stable_mode: str = "unknown"    # championship / elite / mid / survival / new / visiting
    combined_tier: str = ""         # A/B/C grading after combining cycle + stable


# ── DB access ────────────────────────────────────────────────────────────
def load_horse_history(con: sqlite3.Connection, horse_id: str,
                       before_date: str) -> pd.DataFrame:
    q = """
    SELECT race_date, race_number, place, lbw, rating, distance,
           race_track, track_type, race_class, going, finish_time_seconds
    FROM results
    WHERE horse_id = ?
      AND race_date < ?
    ORDER BY race_date ASC, race_number ASC
    """
    df = pd.read_sql_query(q, con, params=(horse_id, before_date))
    # Numeric coercions
    for col in ("rating", "distance", "finish_time_seconds"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # 'place' is text; we need int wins. Treat "1" as win, ignore non-numeric.
    df["place_num"] = pd.to_numeric(df["place"], errors="coerce")
    return df


# ── Per-horse classifier ─────────────────────────────────────────────────
PLACE_LOOKBACK = 6      # recent starts considered for the place-form overlay


def _score_place_form(hist: pd.DataFrame) -> dict:
    """Contextual recent-form score that credits competitive PLACES.

    Wins set the rating band elsewhere; this scores *how well* a horse has been
    running lately using finishing position + beaten-lengths the cycle DB
    already carries. A horse that keeps hitting the frame (or finishing within a
    length) without winning still earns a positive signal — the "contextualised
    places" the rating cycle previously ignored.
    """
    empty = {"starts": 0, "top3": 0, "score": 0.0, "form": "", "note": ""}
    if hist is None or hist.empty:
        return empty
    recent = hist.tail(PLACE_LOOKBACK)
    places = pd.to_numeric(recent.get("place_num"), errors="coerce")
    if "lbw" in recent.columns:
        lbws = pd.to_numeric(recent["lbw"], errors="coerce")
    else:
        lbws = pd.Series([None] * len(recent), index=recent.index)
    starts = int(places.notna().sum())
    top3 = 0
    score = 0.0
    for p, l in zip(places.tolist(), lbws.tolist()):
        if p is None or pd.isna(p):
            continue
        p = int(p)
        lb = float(l) if (l is not None and not pd.isna(l)) else None
        if p <= 3:
            top3 += 1
        if p == 1:
            continue                      # wins handled by the rating band
        if p == 2:
            score += 1.0
        elif p == 3:
            score += 0.7
        elif p <= 5 and lb is not None and lb <= 2.0:
            score += 0.4                  # close-up midfield finish
        elif lb is not None and lb <= 1.0:
            score += 0.5                  # beaten under a length anywhere
        elif p >= 8 and lb is not None and lb > 5.0:
            score -= 0.3                  # well beaten
    score = round(score, 2)
    if score >= 1.5:
        form = "HOT"
    elif score >= 0.7:
        form = "WARM"
    else:
        form = ""
    note = (f"{top3} top-3 in last {starts} (place-form {score:+.1f})"
            if form else "")
    return {"starts": starts, "top3": top3, "score": score,
            "form": form, "note": note}


def classify_horse(hist: pd.DataFrame, *, horse_id: str, horse_name: str,
                   trainer: str, race_number: int, today_venue: str,
                   today_surface: str, today_distance: int,
                   today_class: Optional[str],
                   today_rating: Optional[float]) -> HorseCycle:

    n_hist_runs = len(hist)
    wins_df = hist[hist["place_num"] == 1].copy()
    n_hist_wins = len(wins_df)

    # Defaults
    band_overall_mean = band_overall_min = band_overall_max = None
    band_match_mean = None
    band_match_n = 0
    course_dist_match = False
    runs_since_last_win = None
    last_win_rating = None
    rating_trend_declining = False
    rating_drop_last_n = 0.0
    cycle_state = "unknown"
    primary_flag = "NO_DATA"
    note = ""

    if n_hist_wins >= 1:
        win_ratings = wins_df["rating"].dropna()
        if len(win_ratings) >= 1:
            band_overall_mean = float(win_ratings.mean())
            band_overall_min  = float(win_ratings.min())
            band_overall_max  = float(win_ratings.max())
            last_win_rating   = float(wins_df.iloc[-1]["rating"]) if pd.notna(wins_df.iloc[-1]["rating"]) else None

        # Match: same venue + same surface (broad) + distance within tol
        wins_match = wins_df[
            (wins_df["race_track"] == today_venue) &
            (wins_df["track_type"].fillna("").str.lower() == today_surface.lower()) &
            ((wins_df["distance"] - today_distance).abs() <= DIST_TOL_M)
        ]
        band_match_n = len(wins_match)
        if band_match_n >= 1:
            wm = wins_match["rating"].dropna()
            if len(wm) >= 1:
                band_match_mean = float(wm.mean())
                course_dist_match = True

        # Runs since last win
        last_win_idx = wins_df.index[-1]
        runs_since_last_win = int((hist.index > last_win_idx).sum())

    # Rating trajectory over the last DECLINE_LOOKBACK starts
    recent = hist.tail(DECLINE_LOOKBACK)
    recent_ratings = recent["rating"].dropna()
    if len(recent_ratings) >= 2:
        rating_drop_last_n = float(recent_ratings.iloc[0] - recent_ratings.iloc[-1])
        rating_trend_declining = rating_drop_last_n >= DECLINE_MIN_DROP

    # Determine cycle_state — use both the matched-band mean AND the overall
    # winning ceiling (max rating-at-win) to avoid false "overrated" calls when
    # a horse is simply at its proven competitive ceiling.
    band_ref = band_match_mean if band_match_mean is not None else band_overall_mean
    band_max = band_overall_max
    if today_rating is None or band_ref is None or band_max is None:
        cycle_state = "unknown"
    else:
        # Distance ABOVE proven ceiling
        above_max = today_rating - band_max
        delta_mean = today_rating - band_ref
        if above_max > PEAK_OVER:
            cycle_state = "at_peak"
        elif above_max > 0:
            cycle_state = "approaching_ceiling"      # at career-high band
        elif abs(delta_mean) <= BAND_TIGHT:
            cycle_state = "at_band"
        elif delta_mean > BAND_TIGHT:
            cycle_state = "above_mean_within_ceiling"
        elif delta_mean >= -BAND_LOOSE:
            cycle_state = "approaching"
        else:
            cycle_state = "below_band"

    # Primary flag
    if today_rating is None:
        primary_flag = "NO_DATA"
        note = "No current rating (griffin or unrated)"
    elif n_hist_wins < MIN_HIST_WINS_FOR_BAND:
        primary_flag = "NO_DATA"
        note = f"Only {n_hist_wins} historic HK win(s); band unreliable"
    elif cycle_state == "at_band" and course_dist_match and \
            (runs_since_last_win or 0) >= PRIMED_MIN_RUNS_SINCE_WIN:
        primary_flag = "PRIMED"
        note = (f"At winning band ({band_match_mean:.0f}±) on matching "
                f"{today_venue} {today_surface} {today_distance}m, "
                f"{runs_since_last_win} starts since last win")
    elif cycle_state == "at_band" and course_dist_match:
        primary_flag = "EARLY_DROPPER"
        note = (f"Back at band ({band_match_mean:.0f}±) on matching conditions "
                f"but only {runs_since_last_win} runs since last win — watch market")
    elif cycle_state == "approaching" and course_dist_match and rating_trend_declining:
        primary_flag = "WATCH"
        note = (f"Approaching band (now {today_rating:.0f}, band {band_ref:.0f}); "
                f"rating dropped {rating_drop_last_n:.0f} last {DECLINE_LOOKBACK} starts")
    elif cycle_state == "at_peak" and (runs_since_last_win or 0) <= 1:
        # Only fade if fresh off a win AND meaningfully above ceiling
        primary_flag = "FADE_OVERRATED"
        note = (f"Above career-best win rating ({today_rating:.0f} vs max {band_max:.0f}, "
                f"mean band {band_ref:.0f}) — likely overpriced after recent win")
    elif cycle_state == "below_band":
        primary_flag = "WATCH"
        note = (f"Below historic band ({today_rating:.0f} vs {band_ref:.0f}) "
                f"— could be ready if conditions match")
    else:
        primary_flag = "NEUTRAL"
        bref = f"{band_ref:.0f}" if band_ref is not None else "—"
        bmax = f"{band_max:.0f}" if band_max is not None else "—"
        note = (f"Cycle: {cycle_state}, band {bref} (max {bmax}), "
                f"course-dist match: {course_dist_match}")

    # Recent place/competitive-form overlay (informational; does not override
    # the win-band primary flag, but flags consistent placers running into form).
    pf = _score_place_form(hist)
    if pf["form"] and pf["note"]:
        note = f"{note} · {pf['form']} place-form: {pf['note']}" if note \
            else f"{pf['form']} place-form: {pf['note']}"

    return HorseCycle(
        horse_id=horse_id, horse_name=horse_name, trainer=trainer,
        race_number=race_number, today_venue=today_venue,
        today_surface=today_surface, today_distance=today_distance,
        today_class=today_class, today_rating=today_rating,
        n_hist_runs=n_hist_runs, n_hist_wins=n_hist_wins,
        band_overall_mean=band_overall_mean, band_overall_min=band_overall_min,
        band_overall_max=band_overall_max,
        band_match_mean=band_match_mean, band_match_n=band_match_n,
        course_dist_match=course_dist_match,
        runs_since_last_win=runs_since_last_win,
        last_win_rating=last_win_rating,
        rating_trend_declining=rating_trend_declining,
        rating_drop_last_n=rating_drop_last_n,
        cycle_state=cycle_state, primary_flag=primary_flag, note=note,
        recent_starts=pf["starts"], recent_top3=pf["top3"],
        recent_place_score=pf["score"], place_form=pf["form"],
        place_note=pf["note"],
    )


# ── Card-level driver ───────────────────────────────────────────────────
def analyse_card(racecard_path: Path, db_path: Path = Path("hkjc.db")) -> list[HorseCycle]:
    from stable_mode import compute_stable_modes
    stable_modes = compute_stable_modes(db_path)
    df = pd.read_excel(racecard_path, sheet_name="All Races")
    if df.empty:
        return []
    date_iso = str(df["race_date"].iloc[0])[:10]
    con = sqlite3.connect(str(db_path))
    results: list[HorseCycle] = []
    try:
        for _, row in df.iterrows():
            if bool(row.get("is_standby", False)):
                continue
            horse_id = str(row.get("horse_id", ""))
            if not horse_id or horse_id == "nan":
                continue
            hist = load_horse_history(con, horse_id, date_iso)
            today_rating = row.get("rating")
            try:
                today_rating = float(today_rating) if pd.notna(today_rating) else None
            except (TypeError, ValueError):
                today_rating = None
            try:
                today_distance = int(row.get("distance"))
            except (TypeError, ValueError):
                continue
            today_venue = str(row.get("racecourse") or row.get("race_track") or "").strip().upper()
            today_surface = str(row.get("surface", "")).strip()
            results.append(classify_horse(
                hist,
                horse_id=horse_id,
                horse_name=str(row.get("horse_name", "")).strip(),
                trainer=str(row.get("trainer", "")).strip(),
                race_number=int(row.get("race_number", 0)),
                today_venue=today_venue,
                today_surface=today_surface,
                today_distance=today_distance,
                today_class=str(row.get("race_class", "")).strip(),
                today_rating=today_rating,
            ))
    finally:
        con.close()
    # Stable overlay + combined tier
    for r in results:
        sm = stable_modes.get(r.trainer)
        r.stable_mode = sm.mode if sm else "unknown"
        r.combined_tier = _combined_tier(r.primary_flag, r.stable_mode)
    return results


def _combined_tier(flag: str, mode: str) -> str:
    """A = highest conviction, C = lowest. Empty for non-signals."""
    if flag == "PRIMED":
        if mode in ("championship", "survival"):
            return "A"        # Best money pattern: strong intent + cycle ripe
        if mode in ("elite", "mid", "new"):
            return "B"
        return "C"
    if flag == "EARLY_DROPPER":
        if mode in ("championship", "survival"):
            return "B"
        return "C"
    if flag == "FADE_OVERRATED":
        # Championship trainers fading their own horse = unusual; weaker fade call
        if mode == "championship":
            return "C"
        return "B"
    if flag == "WATCH":
        return "C"
    return ""


def save_results(results: list[HorseCycle], out_path: Path) -> None:
    payload = {
        "n_horses": len(results),
        "n_primed": sum(1 for r in results if r.primary_flag == "PRIMED"),
        "n_early":  sum(1 for r in results if r.primary_flag == "EARLY_DROPPER"),
        "n_fade":   sum(1 for r in results if r.primary_flag == "FADE_OVERRATED"),
        "n_watch":  sum(1 for r in results if r.primary_flag == "WATCH"),
        "horses":   [asdict(r) for r in results],
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def print_summary(results: list[HorseCycle], top: int = 30) -> None:
    flag_order = {"PRIMED": 0, "EARLY_DROPPER": 1, "FADE_OVERRATED": 2,
                  "WATCH": 3, "NEUTRAL": 4, "NO_DATA": 5}
    tier_order = {"A": 0, "B": 1, "C": 2, "": 9}
    ordered = sorted(results, key=lambda r: (flag_order.get(r.primary_flag, 9),
                                              tier_order.get(r.combined_tier, 9),
                                              r.race_number))
    print(f"\n{'='*112}")
    print(f"{'Tier':<5} {'R':>2} {'Horse':<22} {'Trainer':<14} {'Stable':<13} "
          f"{'Flag':<16} {'Rtg':>4} {'Band':>5}  Note")
    print("=" * 112)
    for r in ordered:
        if r.primary_flag in ("NEUTRAL", "NO_DATA"):
            continue
        band = (f"{r.band_match_mean:.0f}" if r.band_match_mean is not None
                else (f"{r.band_overall_mean:.0f}" if r.band_overall_mean is not None else "—"))
        rtg = f"{r.today_rating:.0f}" if r.today_rating is not None else "—"
        print(f"{r.combined_tier:<5} {r.race_number:>2} {r.horse_name[:22]:<22} "
              f"{r.trainer[:14]:<14} {r.stable_mode[:13]:<13} "
              f"{r.primary_flag:<16} {rtg:>4} {band:>5}  {r.note}")
    n_signal = sum(1 for r in results if r.primary_flag in
                   ("PRIMED", "EARLY_DROPPER", "FADE_OVERRATED", "WATCH"))
    n_A = sum(1 for r in results if r.combined_tier == "A")
    n_B = sum(1 for r in results if r.combined_tier == "B")
    print(f"\n{n_signal} signals across {len({r.race_number for r in results})} races "
          f"(of {len(results)} runners). Tier A={n_A}, B={n_B}.")


# ── Dashboard reuse helpers ─────────────────────────────────────────────
def load_or_build_card_cycle(date_compact: str,
                             db_path: Path = Path("hkjc.db")) -> Optional[dict]:
    """Return the horse-cycle payload for a meeting, building it if stale.

    Reads reports/horse_cycle_<date>.json when it is newer than the racecard;
    otherwise runs analyse_card and writes a fresh report. Returns None when
    no racecard exists. Safe to call from the dashboard (read-only on DB).
    """
    rc = Path("racecards") / f"racecard_{date_compact}.xlsx"
    out = Path("reports") / f"horse_cycle_{date_compact}.json"
    if not rc.exists():
        if out.exists():
            try:
                return json.loads(out.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None
    fresh = out.exists() and out.stat().st_mtime >= rc.stat().st_mtime
    if fresh:
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            fresh = False
    results = analyse_card(rc, db_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    save_results(results, out)
    return json.loads(out.read_text(encoding="utf-8"))


def resolve_horse_id(con: sqlite3.Connection, horse_name: str) -> Optional[str]:
    """Best-effort horse_id lookup from a (case-insensitive) horse name."""
    name = (horse_name or "").strip().upper()
    if not name:
        return None
    row = con.execute(
        "SELECT horse_id FROM results WHERE UPPER(horse_name)=? "
        "ORDER BY race_date DESC LIMIT 1", (name,)
    ).fetchone()
    return row[0] if row else None


def compute_profile_cycle(*, horse_name: Optional[str] = None,
                          horse_id: Optional[str] = None,
                          db_path: Path = Path("hkjc.db")) -> Optional[dict]:
    """Compute a horse's rating-cycle profile from history alone.

    Unlike classify_horse (which needs today's race context), this summarises
    where the horse currently sits relative to its own winning bands across
    every condition it has won at. Intended for the Horse Profile page.

    Returns a dict with: current_rating, n_runs, n_wins, runs_since_last_win,
    overall band (min/mean/max), per-condition winning bands, the latest
    rating trend, and a narrative cycle_state. Returns None when the horse is
    unknown or has no rated runs.
    """
    con = sqlite3.connect(str(db_path))
    try:
        if not horse_id and horse_name:
            horse_id = resolve_horse_id(con, horse_name)
        if not horse_id:
            return None
        hist = load_horse_history(con, horse_id, "9999-12-31")
        disp_name = horse_name
        if not disp_name and not hist.empty:
            # recover a display name
            row = con.execute(
                "SELECT horse_name FROM results WHERE horse_id=? "
                "ORDER BY race_date DESC LIMIT 1", (horse_id,)
            ).fetchone()
            disp_name = row[0] if row else horse_id
    finally:
        con.close()

    if hist.empty:
        return None

    rated = hist["rating"].dropna()
    current_rating = float(rated.iloc[-1]) if len(rated) else None

    wins_df = hist[hist["place_num"] == 1].copy()
    n_wins = len(wins_df)
    win_ratings = wins_df["rating"].dropna()
    band_mean = float(win_ratings.mean()) if len(win_ratings) else None
    band_min = float(win_ratings.min()) if len(win_ratings) else None
    band_max = float(win_ratings.max()) if len(win_ratings) else None

    runs_since_last_win = None
    last_win_rating = None
    if n_wins:
        last_win_idx = wins_df.index[-1]
        runs_since_last_win = int((hist.index > last_win_idx).sum())
        lw = wins_df.iloc[-1]["rating"]
        last_win_rating = float(lw) if pd.notna(lw) else None

    # Per-condition winning bands (venue + surface + exact distance)
    cond_bands: list[dict] = []
    if n_wins:
        grp = wins_df.dropna(subset=["rating"]).groupby(
            ["race_track", "track_type", "distance"], dropna=True)
        for (trk, ttype, dist), g in grp:
            gr = g["rating"].dropna()
            if not len(gr):
                continue
            cond_bands.append({
                "venue": str(trk),
                "surface": "AWT" if str(ttype).lower().startswith("all") else "Turf",
                "distance": int(dist),
                "n_wins": int(len(gr)),
                "band_min": float(gr.min()),
                "band_mean": float(gr.mean()),
                "band_max": float(gr.max()),
            })
        cond_bands.sort(key=lambda c: (-c["n_wins"], c["distance"]))

    # Rating trajectory over last DECLINE_LOOKBACK starts
    recent = hist.tail(DECLINE_LOOKBACK)["rating"].dropna()
    rating_drop = (float(recent.iloc[0] - recent.iloc[-1])
                   if len(recent) >= 2 else 0.0)
    declining = rating_drop >= DECLINE_MIN_DROP

    # Overall cycle narrative relative to career band
    cycle_state = "unknown"
    primary_flag = "NO_DATA"
    note = ""
    if current_rating is not None and band_mean is not None and band_max is not None:
        above_max = current_rating - band_max
        delta_mean = current_rating - band_mean
        if above_max > PEAK_OVER:
            cycle_state = "at_peak"
        elif above_max > 0:
            cycle_state = "approaching_ceiling"
        elif abs(delta_mean) <= BAND_TIGHT:
            cycle_state = "at_band"
        elif delta_mean > BAND_TIGHT:
            cycle_state = "above_mean_within_ceiling"
        elif delta_mean >= -BAND_LOOSE:
            cycle_state = "approaching"
        else:
            cycle_state = "below_band"

        if n_wins < MIN_HIST_WINS_FOR_BAND:
            primary_flag = "NO_DATA"
            note = f"Only {n_wins} HK win(s) — band unreliable."
        elif cycle_state == "at_band" and (runs_since_last_win or 0) >= PRIMED_MIN_RUNS_SINCE_WIN:
            primary_flag = "PRIMED"
            note = (f"Back inside winning band ({band_mean:.0f}±), "
                    f"{runs_since_last_win} starts since last win.")
        elif cycle_state == "at_band":
            primary_flag = "EARLY_DROPPER"
            note = (f"At winning band ({band_mean:.0f}±) but only "
                    f"{runs_since_last_win} runs since last win.")
        elif cycle_state in ("approaching", "below_band") and declining:
            primary_flag = "WATCH"
            note = (f"Rating easing toward band (now {current_rating:.0f}, "
                    f"band {band_mean:.0f}); dropped {rating_drop:.0f} "
                    f"over last {DECLINE_LOOKBACK} starts.")
        elif cycle_state == "at_peak" and (runs_since_last_win or 0) <= 1:
            primary_flag = "FADE_OVERRATED"
            note = (f"At career-high ({current_rating:.0f} vs ceiling "
                    f"{band_max:.0f}) fresh off a win — vulnerable.")
        else:
            primary_flag = "NEUTRAL"
            note = (f"Cycle: {cycle_state}; current {current_rating:.0f} vs "
                    f"band {band_mean:.0f} (ceiling {band_max:.0f}).")

    return {
        "horse_id": horse_id,
        "horse_name": disp_name,
        "current_rating": current_rating,
        "n_runs": int(len(hist)),
        "n_wins": int(n_wins),
        "runs_since_last_win": runs_since_last_win,
        "last_win_rating": last_win_rating,
        "band_min": band_min,
        "band_mean": band_mean,
        "band_max": band_max,
        "cond_bands": cond_bands,
        "rating_drop_last_n": rating_drop,
        "rating_trend_declining": declining,
        "cycle_state": cycle_state,
        "primary_flag": primary_flag,
        "note": note,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: horse_cycle.py YYYYMMDD")
        return 1
    date_compact = sys.argv[1]
    rc = Path("racecards") / f"racecard_{date_compact}.xlsx"
    if not rc.exists():
        print(f"Racecard not found: {rc}")
        return 1
    results = analyse_card(rc)
    out = Path("reports") / f"horse_cycle_{date_compact}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    save_results(results, out)
    print_summary(results)
    print(f"\nJSON saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
