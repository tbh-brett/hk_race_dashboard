#!/usr/bin/env python3
"""
Race Commentary Generator
==========================
Fuses three inputs into structured, rule-based commentary:

  1. Race results           — reports/results_YYYYMMDD.json
  2. Incident report        — reports/incidents_YYYYMMDD.json
  3. Running-position photo — running_position_photos/YYYYMMDD/R{N}.json

Outputs:

  reports/commentary_YYYYMMDD.json  — per-race narrative + per-horse short blurbs
                                      + tag flags usable for blackbook suggestions

Usage:
    python race_commentary.py --date 2026-04-15
    python race_commentary.py --dates 2026-04-01,2026-04-06,2026-04-08,2026-04-12,2026-04-15
    python race_commentary.py --date 2026-04-15 --race 8 --print
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
PHOTOS = BASE / "running_position_photos"

# ── Signal patterns (regex rules over incident text) ──────────────────────────
RULES = [
    # (tag, regex, polarity {+, -, 0}, short_blurb_template)
    ("bleeding",        r"blood\s+in\s+the?\s*horse.?s?\s*trachea|substantial amount of blood|epistaxis|bleed", "-",
        "Post-race scope: blood in trachea (bleeder)"),
    ("vet_hold",        r"will be subjected to an official veterinary examination", "-",
        "Must pass veterinary exam before racing again"),
    ("vet_ok",          r"veterinary (inspection|examination)(?:[^.]|\.(?!\s*[A-Z]))*?did not show any significant findings", "0",
        "Vet-checked post-race, no findings"),
    ("sampling",        r"Sent for sampling post-race", "0", ""),
    ("raced_keenly",    r"raced (too )?keenly|raced keenly|keen early", "-",
        "Raced keenly, failed to settle"),
    ("too_far_back",    r"ridden (slightly )?further back in the field than intended|disadvantaged.{0,40}ridden", "+",
        "Ridden too far back, unsuited setup"),
    ("disappointing",   r"could offer no excuse.{0,60}disappoint|disappointing performance", "-",
        "Disappointing run, no excuse offered"),
    ("held_up",         r"[Hh]eld up", "+", "Held up for run"),
    ("no_clear_run",    r"did not obtain clear running|could not obtain clear|no clear run", "+",
        "Denied clear running"),
    ("bumped_start",    r"[Bb]umped (on jumping|shortly after the start|at the start)", "+",
        "Bumped at start"),
    ("bumped_mid",      r"made contact with|bumped|hampered", "0", ""),
    ("crowded",         r"[Cc]rowded", "+", "Crowded"),
    ("hampered_start",  r"hampered.{0,40}(jump|start)", "+", "Hampered at start"),
    ("unbalanced",      r"unbalanced|became unbalanced", "+", "Unbalanced"),
    ("wide_no_cover",   r"wide and without cover|without cover for the majority", "+",
        "Wide without cover (tough trip)"),
    ("steadied",        r"[Ss]teadied (away from|to avoid)", "0", "Steadied"),
    ("jumped_fairly",   r"[Jj]umped only fairly|missed the kick|slow away", "+",
        "Missed the kick / jumped slowly"),
    ("one_paced",       r"one-paced", "0", "One-paced"),
    ("shifted_out",     r"[Ss]hifted out at the start|shifted out on jumping", "0", "Shifted out at start"),
    ("shifted_in",      r"[Ss]hifted in", "0", ""),
    ("weakened",        r"weakened noticeably|weakened over the concluding stages", "-",
        "Weakened in the straight"),
    ("wide_barrier",    r"from the outside barrier|from a wide barrier", "0", ""),
    ("conservative",    r"ride the horse in a conservative manner|conservative ride", "0",
        "Ridden conservatively"),
]

# Trip polarity from photo (avg_y in-band)
def _trip_flag(h_photo: Dict) -> Tuple[str, str]:
    """Return (tag, short_blurb) from photo data."""
    avg_y = h_photo.get("avg_y_in_band")
    avg_x = h_photo.get("avg_x_frac")
    wide_all = h_photo.get("wide_all_way")
    rail_all = h_photo.get("rail_all_way")
    n_frames = h_photo.get("n_frames_seen", 0)
    if avg_y is None or n_frames < 2:
        return ("", "")
    if wide_all:
        if avg_x is not None and avg_x < 0.30:
            return ("wide_leader", "Wide leader in the clear")
        return ("wide_all_way", "Caught wide the entire run")
    if rail_all:
        return ("rail_all_way", "Saved ground on the rail")
    return ("", "")


# ── I/O helpers ───────────────────────────────────────────────────────────────
def load_json(path: Path) -> Optional[Dict]:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _iso_to_dc(date_iso: str) -> str:
    return date_iso.replace("-", "")


# ── Per-horse blurb ───────────────────────────────────────────────────────────
def apply_rules(incident_text: str) -> List[Dict]:
    """Returns list of {tag, polarity, blurb} triggered by the text."""
    out: List[Dict] = []
    t = incident_text or ""
    for tag, pat, pol, blurb in RULES:
        if re.search(pat, t, flags=re.I):
            out.append({"tag": tag, "polarity": pol, "blurb": blurb})
    return out


def short_blurb(place: Optional[int], field_size: int,
                incident_tags: List[Dict], trip_tag: str, trip_blurb: str) -> str:
    """<= ~70 char short commentary for a single past run (form-guide)."""
    parts: List[str] = []
    # Trouble/trip signals — prefer actionable positive (excuse) tags first
    priority_pos = ["bleeding", "vet_hold", "wide_no_cover", "no_clear_run",
                    "hampered_start", "bumped_start", "crowded",
                    "held_up", "unbalanced", "jumped_fairly", "too_far_back"]
    priority_neg = ["disappointing", "weakened", "raced_keenly", "one_paced"]
    seen_tags = {t["tag"]: t for t in incident_tags}
    picked = None
    for tag in priority_pos + priority_neg:
        if tag in seen_tags and seen_tags[tag]["blurb"]:
            picked = seen_tags[tag]["blurb"]
            break
    if picked:
        parts.append(picked)
    elif trip_blurb and trip_tag in {"wide_all_way", "rail_all_way"}:
        parts.append(trip_blurb)
    # Never exceed 80 chars
    s = " · ".join(parts)
    return s[:80]


def build_horse_entry(runner: Dict, incident: Optional[Dict],
                      photo_h: Optional[Dict], field_size: int) -> Dict:
    place = runner.get("place", "")
    try:
        place_i = int(str(place).strip().rstrip("stndrdth"))
    except Exception:
        place_i = None
    inc_text = (incident or {}).get("incident", "") if incident else ""
    tags = apply_rules(inc_text) if inc_text else []
    trip_tag, trip_blurb = _trip_flag(photo_h or {})
    # Polarity score for blackbook suggestion
    pol_score = 0
    for tg in tags:
        if tg["polarity"] == "+":
            pol_score += 1
        elif tg["polarity"] == "-":
            pol_score -= 1
    # Photo polarity: wide-all-way within LEADER cohort is neutral; mid-pack wide is neutral;
    # only the rare explicit "ran wide" with bad placing reinforces +.
    short = short_blurb(place_i, field_size, tags, trip_tag, trip_blurb)
    return {
        "horse_name": runner.get("horse_name", ""),
        "horse_no":   runner.get("horse_no"),
        "place":      place_i,
        "lbw":        runner.get("lbw", ""),
        "running_position": runner.get("running_position", ""),
        "draw":       runner.get("draw", ""),
        "win_odds":   runner.get("win_odds", ""),
        "short":      short,
        "tags":       [t["tag"] for t in tags] + ([trip_tag] if trip_tag else []),
        "polarity_score": pol_score,
        "incident_text": inc_text,
    }


# ── Race-level narrative ──────────────────────────────────────────────────────
def field_size_of(horse_entries: List[Dict]) -> int:
    places = [h.get("place") for h in horse_entries if h.get("place")]
    return max(places) if places else len(horse_entries)


def race_narrative(race: Dict, horse_entries: List[Dict],
                   photo_race: Optional[Dict]) -> str:
    """Paragraph-long plain-English summary of how the race unfolded."""
    rn = race.get("race_number")
    name = race.get("race_name", "")
    dist = race.get("distance", "")
    going = race.get("going", "")
    cls = race.get("race_class", "")

    by_place = {h["place"]: h for h in horse_entries if h.get("place")}
    winner = by_place.get(1)
    second = by_place.get(2)
    third  = by_place.get(3)
    last   = max((h for h in horse_entries if h.get("place")),
                 key=lambda h: h["place"], default=None)

    lines: List[str] = []
    hdr = f"Race {rn}: {name} — {dist}m, Class {cls}, going {going}."
    lines.append(hdr)

    # Opening scene from photo band-0 (if avail)
    if photo_race:
        photo_by_name = {h["horse_name"].upper(): h
                         for h in photo_race.get("horses", [])}
        # Find fastest away (lowest avg_x in first band)
        starters = []
        for h in photo_race.get("horses", []):
            frames = [f for f in h.get("frames", []) if f.get("band_idx") == 0]
            if frames:
                starters.append((h["horse_name"], frames[0]["x_frac"]))
        starters.sort(key=lambda x: x[1])
        if starters:
            lead_names = ", ".join(s[0].title() for s in starters[:2])
            lines.append(f"Early lead: {lead_names}.")

    # Winning margin + style
    if winner:
        wlbw = winner.get("lbw") or ""
        wrp = winner.get("running_position") or ""
        odds = winner.get("win_odds") or ""
        # Describe style from running position sequence
        positions = [p for p in wrp.split() if p]
        style_desc = ""
        if positions:
            try:
                first = int(positions[0])
                last_call = int(positions[-1])
                fs = field_size_of(horse_entries)
                if fs and first <= 2:
                    style_desc = "led throughout" if last_call == 1 else f"led early then settled"
                elif fs and first >= fs - 1:
                    style_desc = "from the back"
                else:
                    style_desc = "from midfield"
            except Exception:
                pass
        bits = [f"{winner['horse_name'].title()} (#{winner['horse_no']}) won "]
        if style_desc:
            bits.append(style_desc + ", ")
        if second:
            bits.append(f"defeating {second['horse_name'].title()} by {second.get('lbw') or '?'}")
            if third:
                bits.append(f" with {third['horse_name'].title()} third")
        bits.append(f" at SP {odds}." if odds else ".")
        lines.append("".join(bits))
    # Trouble notes — pull out headline-worthy incidents
    trouble: List[str] = []
    for h in horse_entries:
        tags = h.get("tags", [])
        if "bleeding" in tags:
            trouble.append(f"{h['horse_name'].title()} (P{h.get('place','?')}) — "
                           f"scoped blood in trachea post-race")
        elif "vet_hold" in tags:
            trouble.append(f"{h['horse_name'].title()} (P{h.get('place','?')}) — "
                           f"needs to pass vet exam before racing again")
    if trouble:
        lines.append("Veterinary: " + "; ".join(trouble) + ".")

    # Forgivable excuses (wide trips, denied running, hampered starts)
    excuses: List[str] = []
    for h in horse_entries:
        tags = set(h.get("tags", []))
        if "wide_no_cover" in tags or ("wide_all_way" in tags and h.get("place", 99) >= 7):
            excuses.append(f"{h['horse_name'].title()} (P{h.get('place','?')}) wide no cover")
        elif "no_clear_run" in tags:
            excuses.append(f"{h['horse_name'].title()} (P{h.get('place','?')}) denied clear running")
        elif "hampered_start" in tags:
            excuses.append(f"{h['horse_name'].title()} (P{h.get('place','?')}) hampered at start")
    if excuses:
        lines.append("Forgive: " + "; ".join(excuses[:4]) + ".")

    return " ".join(lines)


# ── Blackbook suggestions ─────────────────────────────────────────────────────
POSITIVE_TAG_REASONS = {
    "wide_no_cover":    "Raced wide without cover — forgive",
    "no_clear_run":     "Denied clear running — upgrade",
    "hampered_start":   "Hampered at start",
    "bumped_start":     "Bumped at start",
    "crowded":          "Crowded at start",
    "held_up":          "Held up for run",
    "unbalanced":       "Became unbalanced in-running",
    "too_far_back":     "Ridden too far back, unsuited setup",
    "jumped_fairly":    "Missed the kick",
}
NEGATIVE_TAG_REASONS = {
    "bleeding":         "Post-race blood in trachea (bleeder)",
    "vet_hold":         "Must pass vet exam before racing again",
    "disappointing":    "Disappointing run, no excuse offered",
    "weakened":         "Weakened in the straight",
    "raced_keenly":     "Raced keenly, failed to settle",
}


def blackbook_suggestions(horse_entries: List[Dict]) -> List[Dict]:
    """Return list of {horse_name, tag, reason, polarity} from commentary."""
    out: List[Dict] = []
    for h in horse_entries:
        tags = h.get("tags", [])
        pos_hit = next((t for t in tags if t in POSITIVE_TAG_REASONS), None)
        neg_hit = next((t for t in tags if t in NEGATIVE_TAG_REASONS), None)
        if neg_hit:
            out.append({
                "horse_name": h["horse_name"],
                "tag": neg_hit,
                "reason": NEGATIVE_TAG_REASONS[neg_hit],
                "polarity": "-",
                "place": h.get("place"),
            })
        if pos_hit:
            # Only propose positive (forgive/upgrade) if the horse actually finished poorly
            # Otherwise it's not a blackbook-worthy excuse
            pl = h.get("place") or 99
            if pl >= 4:
                out.append({
                    "horse_name": h["horse_name"],
                    "tag": pos_hit,
                    "reason": POSITIVE_TAG_REASONS[pos_hit],
                    "polarity": "+",
                    "place": pl,
                })
    return out


# ── Main per-meeting ─────────────────────────────────────────────────────────
def build_meeting(date_iso: str) -> Optional[Dict]:
    dc = _iso_to_dc(date_iso)
    res = load_json(REPORTS / f"results_{dc}.json")
    if res is None:
        print(f"  [{date_iso}] no results JSON — skipping")
        return None
    inc = load_json(REPORTS / f"incidents_{dc}.json") or {"races": []}
    inc_by_rn = {r["race_number"]: r for r in inc.get("races", [])}

    out_races: List[Dict] = []
    for race in res.get("races", []):
        rn = race.get("race_number")
        # Incident rows for this race
        ir = inc_by_rn.get(rn, {})
        incidents_list = ir.get("incident_report", [])
        # Index by uppercase horse name AND by horse_no
        inc_by_name = {r["horse_name"].upper(): r for r in incidents_list}
        inc_by_no   = {r["horse_no"]: r for r in incidents_list if r.get("horse_no")}
        # Photo
        photo = load_json(PHOTOS / dc / f"R{rn}.json")
        photo_by_name = {}
        if photo:
            photo_by_name = {h["horse_name"].upper(): h for h in photo.get("horses", [])}

        horse_entries: List[Dict] = []
        field_size = len(race.get("runners", []))
        for r in race.get("runners", []):
            nm = r.get("horse_name", "").upper()
            ic = inc_by_name.get(nm) or inc_by_no.get(r.get("horse_no"))
            ph = photo_by_name.get(nm)
            horse_entries.append(build_horse_entry(r, ic, ph, field_size))

        narrative = race_narrative(race, horse_entries, photo)
        bb_sugg = blackbook_suggestions(horse_entries)

        out_races.append({
            "race_number": rn,
            "race_name":   race.get("race_name", ""),
            "distance":    race.get("distance", ""),
            "race_class":  race.get("race_class", ""),
            "going":       race.get("going", ""),
            "narrative":   narrative,
            "horses":      horse_entries,
            "blackbook_suggestions": bb_sugg,
        })

    return {
        "date":        date_iso,
        "venue":       res.get("venue", ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_races":     len(out_races),
        "races":       out_races,
    }


def save(meeting: Dict) -> Path:
    dc = _iso_to_dc(meeting["date"])
    path = REPORTS / f"commentary_{dc}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meeting, f, ensure_ascii=False, indent=2)
    return path


def date_range(d_from: str, d_to: str) -> List[str]:
    a = datetime.strptime(d_from, "%Y-%m-%d")
    b = datetime.strptime(d_to, "%Y-%m-%d")
    return [(a + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range((b - a).days + 1)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date")
    p.add_argument("--dates")
    p.add_argument("--from", dest="d_from")
    p.add_argument("--to", dest="d_to")
    p.add_argument("--race", type=int, help="Print narrative + horses for this race only")
    p.add_argument("--print", dest="do_print", action="store_true")
    args = p.parse_args()

    dates: List[str] = []
    if args.date:
        dates.append(args.date)
    if args.dates:
        dates.extend([s.strip() for s in args.dates.split(",") if s.strip()])
    if args.d_from and args.d_to:
        dates.extend(date_range(args.d_from, args.d_to))
    if not dates:
        p.error("provide --date / --dates / --from+--to")

    for d in dates:
        meeting = build_meeting(d)
        if not meeting:
            continue
        path = save(meeting)
        print(f"[{d}] {meeting['n_races']} races → {path.name}")
        if args.do_print:
            for race in meeting["races"]:
                if args.race and race["race_number"] != args.race:
                    continue
                print("=" * 72)
                print(race["narrative"])
                print("-" * 72)
                for h in race["horses"]:
                    s = h.get("short") or ""
                    print(f"  P{h.get('place','?'):<3} #{h.get('horse_no','?'):<2} "
                          f"{h['horse_name'][:22]:<22s}  {s}")
                if race["blackbook_suggestions"]:
                    print("-" * 72)
                    print(f"  BB suggestions:")
                    for s in race["blackbook_suggestions"]:
                        sign = "NEG" if s["polarity"] == "-" else "POS"
                        print(f"    [{sign}] {s['horse_name']}: {s['reason']}")


if __name__ == "__main__":
    main()
