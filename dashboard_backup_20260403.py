#!/usr/bin/env python3
"""
HKJC Race Day Dashboard — dashboard.py
========================================
Streamlit dashboard displaying model picks for upcoming HKJC meetings.

Launch:
    streamlit run dashboard.py

Features:
  - Browse all analysed meetings from reports/*.json
  - Top 4 picks per race with colour-coded risk tiers
  - Full field view (expandable)
  - "Run New Meeting" button to trigger the orchestrator
  - Auto-refresh when new results arrive
  - Backtest page: Weekly / Monthly / Seasonal model accuracy
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
PYTHON = sys.executable
BLACKBOOK_FILE = BASE / "blackbook.json"

DEFAULT_EXPIRY_DAYS = 90
CEILING_EXPIRY_DAYS = 45

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="HKJC Race Analysis Dashboard",
    page_icon="🏇",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Styling ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .risk-low  { color: #22c55e; font-weight: bold; }
    .risk-med  { color: #f59e0b; font-weight: bold; }
    .risk-high { color: #ef4444; font-weight: bold; }
    .pick-rank { font-size: 1.3em; font-weight: bold; color: #6366f1; }
    .horse-name { font-size: 1.05em; font-weight: 600; }
    .flag-tag {
        display: inline-block;
        padding: 1px 6px;
        margin: 1px 2px;
        border-radius: 4px;
        font-size: 0.75em;
        font-weight: 600;
    }
    .flag-inc  { background: #fef3c7; color: #92400e; }
    .flag-imp  { background: #d1fae5; color: #065f46; }
    .flag-dec  { background: #fee2e2; color: #991b1b; }
    .flag-u    { background: #e0e7ff; color: #3730a3; }
    .flag-sg   { background: #f3e8ff; color: #6b21a8; }
    div[data-testid="stMetric"] {
        background: var(--secondary-background-color, #f8fafc);
        border: 1px solid var(--faded-text-05, #e2e8f0);
        border-radius: 8px;
        padding: 8px 12px;
    }
    .bt-good  { color: #22c55e; font-weight: bold; }
    .bt-ok    { color: #f59e0b; font-weight: bold; }
    .bt-poor  { color: #ef4444; font-weight: bold; }
    .bt-card {
        background: var(--secondary-background-color, #f8fafc);
        border: 1px solid var(--faded-text-05, #e2e8f0);
        border-radius: 10px; padding: 16px; margin-bottom: 12px;
    }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# Data loading
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=30)
def load_available_meetings() -> list[dict]:
    """Scan reports/ for JSON result files and return sorted list."""
    meetings = []
    if not REPORTS.exists():
        return meetings
    for f in sorted(REPORTS.glob("race_day_report_*_v3.4.8.json"), reverse=True):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            meetings.append({
                "file": f,
                "title": data.get("meeting_title", f.stem),
                "generated_at": data.get("generated_at", ""),
                "venue": data.get("meeting_venue", "?"),
                "n_races": len(data.get("races", [])),
                "date_str": f.stem.replace("race_day_report_", "").replace("_v3.4.8", ""),
            })
        except (json.JSONDecodeError, KeyError):
            continue
    return meetings


def load_meeting_data(path: Path) -> dict:
    """Load full meeting JSON data."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Backtest data loading ─────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def load_backtest_files() -> dict:
    """Load all backtest JSONs. Returns {type: {key: data}}."""
    out = {"meeting": {}, "monthly": {}, "season": {}, "all": None}
    if not REPORTS.exists():
        return out
    for f in sorted(REPORTS.glob("backtest_*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (json.JSONDecodeError, KeyError):
            continue
        name = f.stem
        if name.startswith("backtest_monthly_"):
            key = name.replace("backtest_monthly_", "")
            out["monthly"][key] = data
        elif name.startswith("backtest_season_"):
            key = name.replace("backtest_season_", "")
            out["season"][key] = data
        elif name == "backtest_all":
            out["all"] = data
        elif name.startswith("backtest_"):
            key = name.replace("backtest_", "")
            out["meeting"][key] = data
    return out


def find_results_dates() -> list[str]:
    """Find all results_YYYYMMDD.json dates."""
    dates = []
    for f in sorted(REPORTS.glob("results_*.json")):
        m = re.search(r"results_(\d{8})\.json", f.name)
        if m:
            dates.append(m.group(1))
    return dates


def find_prediction_dates() -> list[str]:
    """Find all prediction JSON dates."""
    dates = []
    for f in sorted(REPORTS.glob("race_day_report_*_v3.4.8.json")):
        m = re.search(r"race_day_report_(\d{8})_v3\.4\.8\.json", f.name)
        if m:
            dates.append(m.group(1))
    return dates


# ══════════════════════════════════════════════════════════════════════════════
# Blackbook — load / save / helpers
# ══════════════════════════════════════════════════════════════════════════════

def _load_blackbook() -> dict:
    if BLACKBOOK_FILE.exists():
        with open(BLACKBOOK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"entries": [], "tag_definitions": {}, "next_id": 1}


def _save_blackbook(bb: dict):
    with open(BLACKBOOK_FILE, "w", encoding="utf-8") as f:
        json.dump(bb, f, ensure_ascii=False, indent=2)


def _bb_active_entries(bb: dict) -> list[dict]:
    """Return entries with status 'active' and not past expiry."""
    today = date.today().isoformat()
    return [e for e in bb["entries"]
            if e.get("status") == "active"
            and (e.get("expiry_date", "9999-12-31") >= today)]


def _bb_active_lookup(bb: dict) -> dict:
    """Returns {horse_name_upper: entry} for active entries."""
    return {e["horse_name"].upper(): e for e in _bb_active_entries(bb)}


def _bb_add_entry(bb: dict, horse_name: str, reasoning: str, tags: list[str],
                  confidence: str, source_race: str = "",
                  preferred_distance: list | None = None,
                  preferred_surface: str | None = None,
                  jockey_preference: str = "",
                  expiry_days: int | None = None) -> dict:
    eid = f"bb_{bb['next_id']:04d}"
    bb["next_id"] += 1
    exp_days = expiry_days or (CEILING_EXPIRY_DAYS if "rating_ceiling" in tags else DEFAULT_EXPIRY_DAYS)
    entry = {
        "id": eid,
        "horse_name": horse_name.strip().upper(),
        "added_date": date.today().isoformat(),
        "expiry_date": (date.today() + timedelta(days=exp_days)).isoformat(),
        "status": "active",
        "reasoning": reasoning.strip(),
        "tags": tags,
        "conditions": {
            "preferred_distance": preferred_distance or [],
            "preferred_surface": preferred_surface,
            "jockey_preference": jockey_preference.strip(),
        },
        "confidence": confidence,
        "source_race": source_race,
        "performances": [],
    }
    bb["entries"].append(entry)
    _save_blackbook(bb)
    return entry


def _bb_update_entry(bb: dict, eid: str, **kwargs):
    for e in bb["entries"]:
        if e["id"] == eid:
            e.update(kwargs)
            break
    _save_blackbook(bb)


def _bb_add_performance(bb: dict, eid: str, perf: dict):
    for e in bb["entries"]:
        if e["id"] == eid:
            e["performances"].append(perf)
            # Auto-extend expiry on partial validation (top-5 finish)
            try:
                place = int(str(perf.get("finish", "99")).strip().rstrip("stndrdth"))
            except ValueError:
                place = 99
            if place <= 5 and e.get("status") == "active":
                exp = date.fromisoformat(e["expiry_date"])
                new_exp = max(exp, date.today() + timedelta(days=45))
                e["expiry_date"] = new_exp.isoformat()
            break
    _save_blackbook(bb)


def _bb_expire_stale(bb: dict):
    """Move past-expiry active entries to expired."""
    today = date.today().isoformat()
    changed = False
    for e in bb["entries"]:
        if e["status"] == "active" and e.get("expiry_date", "9999-12-31") < today:
            e["status"] = "expired"
            changed = True
    if changed:
        _save_blackbook(bb)


def _load_results_json(date_compact: str) -> dict | None:
    p = REPORTS / f"results_{date_compact}.json"
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════════════════════════
# Form Guide — data helpers
# ══════════════════════════════════════════════════════════════════════════════

CACHE_DIR = BASE / "cache"
FORM_COLS = [
    "horse_name", "race_date", "race_number", "race_track", "race_course",
    "going", "race_class", "jockey", "rating", "draw", "running_positions",
    "place", "lbw", "finish_time_seconds", "distance", "actual_weight",
]


@st.cache_data(ttl=120)
def _load_form_db() -> pd.DataFrame:
    """Load hkjc_results_updated.xlsx for form guide lookups."""
    db_file = BASE / "hkjc_results_updated.xlsx"
    if not db_file.exists():
        return pd.DataFrame()
    try:
        df = pd.read_excel(db_file)
    except PermissionError:
        tmp = Path(tempfile.gettempdir()) / db_file.name
        shutil.copy2(db_file, tmp)
        df = pd.read_excel(tmp)
    keep = [c for c in FORM_COLS if c in df.columns]
    df = df[keep].copy()
    df["race_date"] = pd.to_datetime(df["race_date"]).dt.date
    df["place_num"] = pd.to_numeric(df["place"], errors="coerce")
    df["horse_name_upper"] = df["horse_name"].str.upper().str.strip()
    return df


def _load_racecard_cache(date_iso: str) -> dict | None:
    """Load cached racecard JSON for full horse details."""
    p = CACHE_DIR / f"racecard_{date_iso}.json"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _build_race_index(form_db: pd.DataFrame) -> dict:
    """Pre-build index: (race_date, race_number) → sorted runners DataFrame."""
    idx = {}
    for key, grp in form_db.groupby(["race_date", "race_number"]):
        sorted_g = grp.sort_values("place_num")
        top5 = [(int(r["place_num"]) if pd.notna(r["place_num"]) else "?",
                 r["horse_name"])
                for _, r in sorted_g.head(5).iterrows()]
        second = sorted_g[sorted_g["place_num"] == 2]
        margin_2nd = str(second.iloc[0]["lbw"]) if not second.empty else "-"
        idx[key] = {"top5": top5, "margin_2nd": margin_2nd}
    return idx


def _fmt_positions(pos_str) -> str:
    """Format running positions: '8 9 12 12' → '8-9-12-12'."""
    if not pos_str or (isinstance(pos_str, float) and pd.isna(pos_str)):
        return "-"
    parts = str(pos_str).strip().split()
    clean = [p for p in parts if p.strip()]
    return "-".join(clean) if clean else "-"


def _fmt_time(secs) -> str:
    """Convert seconds to MM:SS.SS display."""
    if pd.isna(secs):
        return "-"
    try:
        s = float(secs)
    except (ValueError, TypeError):
        return "-"
    mins = int(s // 60)
    remainder = s - mins * 60
    return f"{mins}:{remainder:05.2f}"


def _fmt_margin(place, lbw, race_idx_entry: dict) -> str:
    """Format margin: winning margin for 1st, LBW for others."""
    try:
        p = int(float(place))
    except (ValueError, TypeError):
        return str(lbw) if lbw and str(lbw) not in ("", "nan") else "-"
    if p == 1:
        m2 = race_idx_entry.get("margin_2nd", "-")
        return f"+{m2}" if m2 and m2 != "-" else "+0"
    lbw_s = str(lbw) if lbw and str(lbw) not in ("", "nan") else "-"
    return lbw_s


def _fmt_top5(top5: list[tuple], current_horse: str) -> str:
    """Top-5 finishers as compact string, current horse marked with *."""
    parts = []
    h_up = current_horse.strip().upper()
    for place, name in top5:
        short = str(name)[:12]
        if str(name).strip().upper() == h_up:
            parts.append(f"{place}.{short}*")
        else:
            parts.append(f"{place}.{short}")
    return ", ".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# Rendering helpers
# ══════════════════════════════════════════════════════════════════════════════

def risk_badge(tier: str, score: float) -> str:
    cls = {"Low": "risk-low", "Medium": "risk-med", "High": "risk-high"}.get(tier, "risk-med")
    return f'<span class="{cls}">{score:.0f} ({tier[0]})</span>'


def flag_badges(flags: list[str]) -> str:
    mapping = {
        "INC": ("INC", "flag-inc"),
        "↑IMP": ("↑IMP", "flag-imp"),
        "↓DEC": ("↓DEC", "flag-dec"),
        "U": ("U", "flag-u"),
        "SG": ("SG", "flag-sg"),
    }
    parts = []
    for f in flags:
        label, cls = mapping.get(f, (f, "flag-inc"))
        parts.append(f'<span class="flag-tag {cls}">{label}</span>')
    return " ".join(parts)


def _load_vet_lookup(date_compact: str) -> dict:
    """Load vet flags from vet_report JSON. Returns {race_number: {horse_no: flag}}."""
    vj = REPORTS / f"vet_report_{date_compact}.json"
    if not vj.exists():
        return {}
    try:
        with open(vj, "r", encoding="utf-8") as f:
            vd = json.load(f)
        lookup = {}
        for rd in vd.get("races", []):
            lookup[rd["race_number"]] = {
                h["horse_no"]: h["max_flag"]
                for h in rd.get("horses", [])
                if h.get("max_flag") in ("RED", "AMBER", "INFO")
            }
        return lookup
    except Exception:
        return {}


def _vet_display(flag: str) -> str:
    """Short vet flag text for table display."""
    return {"RED": "RED", "AMBER": "AMB", "INFO": "INF"}.get(flag, "-")


def render_race_card(race: dict, vet_lookup: dict | None = None, show_top: int = 4,
                     bb_lookup: dict | None = None):
    """Render a single race's picks as a styled table."""
    picks = race.get("picks", [])
    if not picks:
        st.warning(f"No projections for Race {race['race_number']}")
        return

    # Race header
    cls_str = f"Class {race['race_class']}" if race['race_class'] else "Group"
    surface = "AWT" if race["is_awt"] else "Turf"
    pace_badge = {"Fast": "🔴", "Neutral": "🟡", "Slow": "🟢"}.get(race["pace"], "⚪")

    st.markdown(
        f"### R{race['race_number']}  —  {race.get('race_name', '')}\n"
        f"**{race['distance']}m {surface} ({race['race_course']})** · "
        f"{cls_str} · Pace: {pace_badge} {race['pace']} ({race['pace_score']:+.2f}s) · "
        f"{race['projected']}/{race['runners']} projected"
    )

    _bb = bb_lookup or {}

    # Top picks table
    top = picks[:show_top]
    _race_vet = (vet_lookup or {}).get(race["race_number"], {})
    rows = []
    bb_alerts = []
    for p in top:
        vf = p.get("vet_flag") or _race_vet.get(p["horse_no"], "")
        bb_entry = _bb.get(p["horse_name"].upper())
        rows.append({
            "Rank": p["rank"],
            "No": p["horse_no"],
            "Horse": p["horse_name"],
            "BB": "📓" if bb_entry else "",
            "Proj (s)": f"{p['projected_time']:.2f}",
            "Win%": f"{p['win_prob']:.0f}%",
            "Risk": f"{p['risk_score']:.0f}({p['risk_tier'][0]})",
            "Vet": _vet_display(vf),
            "Draw": p.get("draw", "—") or "—",
            "Wt": p.get("weight", "—") or "—",
            "Jockey": p.get("jockey", ""),
            "Style": p.get("style", "?"),
            "Eff Resid": f"{p['effective_resid']:+.3f}",
            "Fin Sec": f"{p['proj_final_sec']:.2f}" if p.get("proj_final_sec") else "—",
            "ESZ": f"{p['early_speed_z']:+.1f}" if p.get("early_speed_z", 0) != 0 else "—",
            "Flags": ", ".join(p.get("flags", [])),
        })

    # Check for BB horses NOT in top picks
    top_names = {p["horse_name"].upper() for p in top}
    for p in picks[show_top:]:
        bb_entry = _bb.get(p["horse_name"].upper())
        if bb_entry:
            bb_alerts.append((p, bb_entry))

    df = pd.DataFrame(rows)

    # Color the Risk column
    def style_risk(val):
        if "(L)" in str(val):
            return "color: #22c55e; font-weight: bold"
        elif "(H)" in str(val):
            return "color: #ef4444; font-weight: bold"
        elif "(M)" in str(val):
            return "color: #f59e0b; font-weight: bold"
        return ""

    def style_winprob(val):
        pct = float(str(val).replace("%", "")) if "%" in str(val) else 0
        if pct >= 20:
            return "color: #22c55e; font-weight: bold"
        elif pct >= 12:
            return "color: #f59e0b"
        return ""

    def style_vet(val):
        v = str(val).strip()
        if v == "RED":
            return "color: #ef4444; font-weight: bold"
        elif v == "AMB":
            return "color: #f59e0b; font-weight: bold"
        elif v == "INF":
            return "color: #3b82f6"
        return ""

    styled = df.style.map(style_risk, subset=["Risk"]) \
                      .map(style_winprob, subset=["Win%"]) \
                      .map(style_vet, subset=["Vet"]) \
                      .set_properties(**{"text-align": "center"}) \
                      .set_properties(subset=["Horse"], **{"text-align": "left", "font-weight": "600"})

    st.dataframe(styled, use_container_width=True, hide_index=True)

    # BB discrepancy alerts — horses in blackbook but not in model top picks
    for p, bbe in bb_alerts:
        tags_str = ", ".join(bbe.get("tags", []))
        st.info(
            f"📓 **Blackbook alert:** {p['horse_name']} (#{p['horse_no']}) "
            f"is ranked **#{p['rank']}** by model.  "
            f"Confidence: {bbe.get('confidence', '?')} · Tags: {tags_str}\n\n"
            f"*{bbe.get('reasoning', '')}*"
        )

    # Expandable full field
    if len(picks) > show_top:
        with st.expander(f"Full field ({len(picks)} runners)"):
            full_rows = []
            for p in picks:
                vf = p.get("vet_flag") or _race_vet.get(p["horse_no"], "")
                bb_entry = _bb.get(p["horse_name"].upper())
                full_rows.append({
                    "#": p["rank"],
                    "No": p["horse_no"],
                    "Horse": p["horse_name"],
                    "BB": "📓" if bb_entry else "",
                    "Proj": f"{p['projected_time']:.2f}",
                    "Win%": f"{p['win_prob']:.0f}%",
                    "Risk": f"{p['risk_score']:.0f}({p['risk_tier'][0]})",
                    "Vet": _vet_display(vf),
                    "Draw": p.get("draw", "—") or "—",
                    "Jockey": p.get("jockey", ""),
                    "Style": p.get("style", "?"),
                    "Flags": ", ".join(p.get("flags", [])),
                })
            st.dataframe(pd.DataFrame(full_rows), use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline runner
# ══════════════════════════════════════════════════════════════════════════════


def run_pipeline(date_str: str, no_cache: bool, going_turf: str, going_awt: str):
    """Run the orchestrator from the dashboard."""
    cmd = [PYTHON, str(BASE / "run_meeting.py"), "--date", date_str]
    if no_cache:
        cmd.append("--no-cache")
    cmd.extend(["--going-turf", going_turf, "--going-awt", going_awt])

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    with st.spinner(f"Running pipeline for {date_str}..."):
        status = st.empty()
        status.info(f"Scraping race card for {date_str}...")

        result = subprocess.run(
            cmd, env=env, cwd=str(BASE),
            capture_output=True, text=True, encoding="utf-8",
        )

        if result.returncode == 0:
            st.success(f"Pipeline complete for {date_str}!")
            with st.expander("Pipeline output"):
                st.code(result.stdout[-3000:] if len(result.stdout) > 3000
                        else result.stdout)
        else:
            st.error(f"Pipeline failed (exit code {result.returncode})")
            with st.expander("Error output"):
                st.code(result.stderr[-2000:] if result.stderr else result.stdout[-2000:])


# ══════════════════════════════════════════════════════════════════════════════
# Main page — Race Day view
# ══════════════════════════════════════════════════════════════════════════════

def page_race_day(selected):
    if selected is None:
        st.title("🏇 HKJC Race Day Analysis")
        st.info("No meetings available. Use the sidebar to run your first analysis.")
        return

    data = load_meeting_data(selected["file"])

    # Load vet lookup (for older JSONs that lack embedded vet_flag)
    _date_match = re.search(r"(\d{8})", str(selected["file"]))
    vet_lookup = _load_vet_lookup(_date_match.group(1)) if _date_match else {}

    # Load blackbook for BB badge
    bb = _load_blackbook()
    _bb_expire_stale(bb)
    bb_lookup = _bb_active_lookup(bb)

    # Header
    st.title(f"🏇 {data['meeting_title']}")
    st.caption(f"Model {data.get('model_version', 'v3.4.8')} · "
               f"Generated {data.get('generated_at', '')[:16]}")

    # Summary metrics
    races = data.get("races", [])
    total_runners = sum(r.get("runners", 0) for r in races)
    total_projected = sum(r.get("projected", 0) for r in races)

    cols = st.columns(4)
    cols[0].metric("Races", len(races))
    cols[1].metric("Total Runners", total_runners)
    cols[2].metric("Projected", total_projected)
    cols[3].metric("Venue", data.get("meeting_venue", "?"))

    st.markdown("---")

    # Quick summary: top pick per race
    st.subheader("🏆 Top Picks Summary")
    summary_rows = []
    for race in races:
        picks = race.get("picks", [])
        if not picks:
            continue
        top = picks[0]
        cls_str = f"C{race['race_class']}" if race['race_class'] else "Grp"
        surface = "AWT" if race["is_awt"] else "Turf"
        summary_rows.append({
            "Race": f"R{race['race_number']}",
            "Dist": f"{race['distance']}m",
            "Surface": surface,
            "Class": cls_str,
            "Pace": race["pace"],
            "Top Pick": top["horse_name"],
            "Proj (s)": f"{top['projected_time']:.2f}",
            "Win%": f"{top['win_prob']:.0f}%",
            "Risk": f"{top['risk_score']:.0f}({top['risk_tier'][0]})",
            "2nd": picks[1]["horse_name"] if len(picks) > 1 else "—",
            "3rd": picks[2]["horse_name"] if len(picks) > 2 else "—",
            "4th": picks[3]["horse_name"] if len(picks) > 3 else "—",
        })

    if summary_rows:
        st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

    st.markdown("---")

    # Per-race detail
    st.subheader("📋 Detailed Race Analysis")

    # Race filter
    race_numbers = [r["race_number"] for r in races]
    selected_races = st.multiselect(
        "Filter races (leave empty for all):",
        race_numbers,
        default=[],
        key="race_filter",
    )

    for race in races:
        if selected_races and race["race_number"] not in selected_races:
            continue
        render_race_card(race, vet_lookup=vet_lookup, show_top=4, bb_lookup=bb_lookup)
        st.markdown("---")

    # Download links
    st.sidebar.markdown("---")
    st.sidebar.subheader("Downloads")
    pdf_name = data.get("pdf_file", "")
    txt_name = data.get("text_file", "")
    pdf_path = REPORTS / pdf_name
    txt_path = REPORTS / txt_name

    if pdf_path.exists():
        with open(pdf_path, "rb") as f:
            st.sidebar.download_button(
                "📄 Download PDF Report",
                f.read(),
                file_name=pdf_name,
                mime="application/pdf",
            )

    if txt_path.exists():
        with open(txt_path, "r", encoding="utf-8") as f:
            st.sidebar.download_button(
                "📝 Download Text Report",
                f.read(),
                file_name=txt_name,
                mime="text/plain",
            )


# ══════════════════════════════════════════════════════════════════════════════
# Backtest page — rendering helpers
# ══════════════════════════════════════════════════════════════════════════════

def _bt_color(val, thresholds, higher_better=True):
    """Return good/ok/poor CSS class for a value."""
    if val is None:
        return ""
    good, ok = thresholds
    if higher_better:
        if val >= good:
            return "bt-good"
        elif val >= ok:
            return "bt-ok"
        return "bt-poor"
    else:
        if val <= good:
            return "bt-good"
        elif val <= ok:
            return "bt-ok"
        return "bt-poor"


def _fmt_pct(val):
    if val is None:
        return "—"
    return f"{val*100:.1f}%" if isinstance(val, float) and val <= 1 else f"{val}%"


def _fmt_val(val, suffix=""):
    if val is None:
        return "—"
    if suffix == "s":
        return f"{val:.3f}s"
    if suffix == "%":
        return _fmt_pct(val)
    return f"{val:.3f}" if isinstance(val, float) else str(val)


def render_backtest_metrics(data: dict, prefix: str = ""):
    """Render the six-dimension backtest card for a given dataset."""
    ta = data.get("time_accuracy", {})
    pa = data.get("pace_accuracy", {})
    pb = data.get("pace_beneficiary", {})
    ra = data.get("risk_accuracy", {})
    sa = data.get("style_accuracy", {})
    da = data.get("draw_accuracy", {})

    # ── Dimension 1: Time Accuracy ─────────────────────────
    st.markdown("### 1. Projected Time Accuracy")
    c1, c2, c3, c4 = st.columns(4)
    mae = ta.get("mae_seconds")
    c1.metric("MAE", _fmt_val(mae, "s"), help="Mean Absolute Error of projected vs actual finish time")
    c2.metric("Median AE", _fmt_val(ta.get("median_ae_seconds"), "s"))
    rho = ta.get("rank_correlation_avg")
    c3.metric("Rank ρ", _fmt_val(rho), help="Spearman rank correlation — does model ordering match finish order?")
    c4.metric("Observations", ta.get("n_observations", 0))

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Top-1 Win Rate", _fmt_pct(ta.get("top1_win_rate")),
              help="How often does the model's #1 pick actually win?")
    c6.metric("Top-1 Races", ta.get("top1_total", 0))
    c7.metric("Top-3 Place Rate", _fmt_pct(ta.get("top3_place_rate")),
              help="How often do model top-3 picks finish top 3?")
    c8.metric("Top-3 Obs", ta.get("top3_total", 0))

    _traffic_light("MAE", mae, good=0.8, ok=1.2, lower_is_better=True)
    _traffic_light("Rank Correlation", rho, good=0.5, ok=0.3, lower_is_better=False)

    st.markdown("---")

    # ── Dimension 2: Pace Accuracy ─────────────────────────
    st.markdown("### 2. Pace Prediction Accuracy")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Exact Match", _fmt_pct(pa.get("exact_match_rate")),
              help="Predicted pace label matches actual label exactly")
    c2.metric("±1 Category", _fmt_pct(pa.get("within_one_category")),
              help="Predicted pace within one category of actual")
    c3.metric("Deviation r", _fmt_val(pa.get("deviation_correlation")),
              help="Pearson correlation of predicted vs actual pace deviation")
    c4.metric("Races", pa.get("n_races", 0))

    _traffic_light("Pace Exact", pa.get("exact_match_rate"), good=0.4, ok=0.25, lower_is_better=False)

    # Pace detail table
    pace_details = pa.get("details", [])
    if pace_details:
        with st.expander(f"Pace details ({len(pace_details)} races)"):
            pdf = pd.DataFrame(pace_details)
            if not pdf.empty:
                pdf["match"] = pdf["match"].map({True: "✓", False: "✗"})
                st.dataframe(pdf, use_container_width=True, hide_index=True)

    st.markdown("---")

    # ── Dimension 3: Pace Beneficiary ──────────────────────
    st.markdown("### 3. Pace Beneficiary Analysis")
    c1, c2, c3, c4 = st.columns(4)
    adv = pb.get("benefit_advantage")
    c1.metric("Advantage", _fmt_val(adv) + " ranks" if adv is not None else "—",
              help="Positive = horses predicted to benefit from pace finish closer to predicted rank")
    c2.metric("Beneficiary Avg", _fmt_val(pb.get("pace_beneficiary_avg_rank_diff")),
              help="Avg (actual_place − predicted_rank) for predicted beneficiaries")
    c3.metric("Penalised Avg", _fmt_val(pb.get("pace_penalised_avg_rank_diff")))
    c4.metric("N (benefit/penalty)", f"{pb.get('n_beneficiaries',0)}/{pb.get('n_penalised',0)}")

    _traffic_light("Pace Benefit Adv", adv, good=0.5, ok=0.0, lower_is_better=False)

    st.markdown("---")

    # ── Dimension 4: Risk Reflection ───────────────────────
    st.markdown("### 4. Risk Reflection Accuracy")
    c1, c2 = st.columns(2)
    c1.metric("Risk↔Error Correlation", _fmt_val(ra.get("risk_error_correlation")),
              help="Positive = higher risk predictions have larger time errors (desirable)")
    c2.metric("Observations", ra.get("n_observations", 0))

    tier_perf = ra.get("tier_performance", {})
    if tier_perf:
        st.markdown("**ROI by Risk Tier (top-4 picks, flat $10 bet):**")
        tier_rows = []
        for tier in ["Low", "Medium", "High"]:
            p = tier_perf.get(tier, {})
            if not p:
                continue
            tier_rows.append({
                "Tier": tier,
                "Bets": p.get("n_bets", 0),
                "Win Rate": _fmt_pct(p.get("win_rate")),
                "Place Rate": _fmt_pct(p.get("place_rate")),
                "ROI": f"{p.get('roi_pct', 0):+.1f}%",
            })
        if tier_rows:
            st.dataframe(pd.DataFrame(tier_rows), use_container_width=True, hide_index=True)

    st.markdown("---")

    # ── Dimension 5: Running Style ─────────────────────────
    st.markdown("### 5. Running Style Prediction")
    c1, c2 = st.columns(2)
    c1.metric("Exact Match Rate", _fmt_pct(sa.get("exact_match_rate")),
              help="Predicted running style matches actual first-checkpoint position")
    c2.metric("Observations", sa.get("n_observations", 0))

    _traffic_light("Style Match", sa.get("exact_match_rate"), good=0.45, ok=0.30, lower_is_better=False)

    confusion = sa.get("confusion_matrix", {})
    if confusion:
        with st.expander("Style confusion matrix"):
            styles = ["Leader", "On-Pace", "Midfield", "Closer"]
            rows_cm = []
            for pred_s in styles:
                row = {"Predicted": pred_s}
                for act_s in styles:
                    row[f"Act: {act_s}"] = confusion.get(pred_s, {}).get(act_s, 0)
                rows_cm.append(row)
            st.dataframe(pd.DataFrame(rows_cm), use_container_width=True, hide_index=True)

    st.markdown("---")

    # ── Dimension 6: Draw Offset ───────────────────────────
    st.markdown("### 6. Draw Offset Accuracy")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Inside Avg Place", _fmt_val(da.get("inside_avg_place")))
    c2.metric("Middle Avg Place", _fmt_val(da.get("middle_avg_place")))
    c3.metric("Outside Avg Place", _fmt_val(da.get("outside_avg_place")))
    c4.metric("Detected Bias", da.get("draw_bias_detected", "—"))


def _traffic_light(label: str, val, good: float, ok: float,
                   lower_is_better: bool = False):
    """Render a small traffic-light indicator."""
    if val is None:
        return
    if lower_is_better:
        if val <= good:
            colour, word = "#22c55e", "Good"
        elif val <= ok:
            colour, word = "#f59e0b", "OK"
        else:
            colour, word = "#ef4444", "Review"
    else:
        if val >= good:
            colour, word = "#22c55e", "Good"
        elif val >= ok:
            colour, word = "#f59e0b", "OK"
        else:
            colour, word = "#ef4444", "Review"
    st.markdown(
        f'<span style="color:{colour};font-weight:bold;font-size:0.85em">'
        f'● {label}: {word}</span>',
        unsafe_allow_html=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Backtest page — main
# ══════════════════════════════════════════════════════════════════════════════

def page_backtest():
    st.title("📊 Model Backtest")
    st.caption("Compare pre-race predictions against actual results")

    # ── Sidebar: scrape results + run backtest ─────────────
    st.sidebar.markdown("---")
    st.sidebar.subheader("Post-Race Data")

    scrape_date = st.sidebar.date_input("Results date", value=date.today(),
                                        key="bt_scrape_date")
    col_a, col_b = st.sidebar.columns(2)
    with col_a:
        if st.button("⬇ Scrape Results", use_container_width=True,
                      key="btn_scrape_results"):
            _run_results_scraper(scrape_date.isoformat())
            st.cache_data.clear()
            st.rerun()
    with col_b:
        if st.button("⚡ Run Backtest", use_container_width=True,
                      key="btn_run_backtest"):
            _run_backtest_single(scrape_date.isoformat())
            st.cache_data.clear()
            st.rerun()

    # Download scraped results as Excel
    _scrape_compact = scrape_date.isoformat().replace("-", "")
    _results_json = REPORTS / f"results_{_scrape_compact}.json"
    if _results_json.exists():
        _xl_bytes = _results_json_to_excel_bytes(_results_json)
        if _xl_bytes:
            st.sidebar.download_button(
                "📥 Download Results (Excel)",
                _xl_bytes,
                file_name=f"results_{_scrape_compact}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="btn_dl_results_xlsx",
            )

    st.sidebar.markdown("---")
    st.sidebar.subheader("Aggregate Reports")

    agg_month = st.sidebar.text_input("Month (YYYY-MM)", value=date.today().strftime("%Y-%m"),
                                       key="bt_month")
    if st.sidebar.button("📅 Monthly Backtest", use_container_width=True,
                          key="btn_monthly_bt"):
        _run_backtest_agg("--month", agg_month)
        st.cache_data.clear()
        st.rerun()

    agg_season = st.sidebar.text_input("Season (YYYY-YYYY)", value="2025-2026",
                                        key="bt_season")
    if st.sidebar.button("📆 Seasonal Backtest", use_container_width=True,
                          key="btn_season_bt"):
        _run_backtest_agg("--season", agg_season)
        st.cache_data.clear()
        st.rerun()

    # ── Status: what data exists ─────────────────────────
    pred_dates = find_prediction_dates()
    res_dates = find_results_dates()
    matched_dates = sorted(set(pred_dates) & set(res_dates))
    unmatched_pred = sorted(set(pred_dates) - set(res_dates))

    st.markdown("### 📁 Data Inventory")
    c1, c2, c3 = st.columns(3)
    c1.metric("Predictions", len(pred_dates))
    c2.metric("Results Scraped", len(res_dates))
    c3.metric("Matched (backtestable)", len(matched_dates))

    if unmatched_pred:
        with st.expander(f"{len(unmatched_pred)} meetings awaiting results"):
            for dc in unmatched_pred:
                d = f"{dc[:4]}-{dc[4:6]}-{dc[6:]}"
                st.text(f"  {d}  — need to scrape results")

    st.markdown("---")

    # ── Tabs: Weekly / Monthly / Seasonal ─────────────────
    tab_weekly, tab_monthly, tab_seasonal = st.tabs([
        "📋 Weekly (Per-Meeting)", "📅 Monthly", "📆 Seasonal"])

    bt_files = load_backtest_files()

    # ── Weekly tab ─────────────────────────────────────────
    with tab_weekly:
        meetings = bt_files.get("meeting", {})
        if not meetings:
            st.info("No per-meeting backtests yet. Scrape results and run backtest above.")
        else:
            meeting_keys = sorted(meetings.keys(), reverse=True)
            labels = {k: f"{k[:4]}-{k[4:6]}-{k[6:]}  ({meetings[k].get('meeting_title','')})"
                      for k in meeting_keys}
            selected_key = st.selectbox("Select meeting:", meeting_keys,
                                        format_func=lambda k: labels[k],
                                        key="bt_weekly_select")
            if selected_key:
                render_backtest_metrics(meetings[selected_key], prefix="wk_")

    # ── Monthly tab ────────────────────────────────────────
    with tab_monthly:
        monthlies = bt_files.get("monthly", {})
        if not monthlies:
            st.info("No monthly backtests yet. Use the sidebar to generate one.")
        else:
            month_keys = sorted(monthlies.keys(), reverse=True)
            selected_month = st.selectbox("Select month:", month_keys,
                                          key="bt_month_select")
            if selected_month:
                mdata = monthlies[selected_month]
                st.markdown(f"**Period:** {mdata.get('period','')}  |  "
                            f"**Meetings:** {mdata.get('n_meetings',0)}  |  "
                            f"**Races:** {mdata.get('n_races',0)}  |  "
                            f"**Runners:** {mdata.get('n_runners',0)}")

                # Per-meeting breakdown table
                per_m = mdata.get("per_meeting", [])
                if per_m:
                    st.markdown("#### Per-Meeting Breakdown")
                    rows_pm = []
                    for m in per_m:
                        rows_pm.append({
                            "Date": m.get("date", ""),
                            "Title": m.get("title", ""),
                            "Races": m.get("races", 0),
                            "MAE": _fmt_val(m.get("mae"), "s"),
                            "Rank ρ": _fmt_val(m.get("rank_corr")),
                            "Pace Exact": _fmt_pct(m.get("pace_exact")),
                            "Top-1 Win": _fmt_pct(m.get("top1_win")),
                        })
                    st.dataframe(pd.DataFrame(rows_pm), use_container_width=True,
                                 hide_index=True)
                    st.markdown("---")

                render_backtest_metrics(mdata, prefix="mo_")

    # ── Seasonal tab ───────────────────────────────────────
    with tab_seasonal:
        seasons = bt_files.get("season", {})
        all_bt = bt_files.get("all")
        if not seasons and not all_bt:
            st.info("No seasonal backtests yet. Use the sidebar to generate one.")
        else:
            options_s = list(seasons.keys())
            if all_bt:
                options_s.insert(0, "__all__")
            if options_s:
                sel_s = st.selectbox(
                    "Select period:", options_s,
                    format_func=lambda k: "All Data" if k == "__all__" else f"Season {k}",
                    key="bt_season_select",
                )
                sdata = all_bt if sel_s == "__all__" else seasons.get(sel_s, {})
                if sdata:
                    st.markdown(f"**Period:** {sdata.get('period','')}  |  "
                                f"**Meetings:** {sdata.get('n_meetings',0)}  |  "
                                f"**Races:** {sdata.get('n_races',0)}")

                    per_m = sdata.get("per_meeting", [])
                    if per_m:
                        st.markdown("#### Meeting-by-Meeting Trend")
                        trend_df = pd.DataFrame(per_m)
                        if "mae" in trend_df.columns:
                            st.line_chart(trend_df.set_index("date")[["mae"]],
                                          use_container_width=True)
                        st.markdown("---")

                    render_backtest_metrics(sdata, prefix="se_")


def _results_json_to_excel_bytes(results_path: Path) -> bytes | None:
    """Convert a results JSON to Excel bytes for download."""
    try:
        with open(results_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    rows = []
    race_date = data.get("date", "")
    venue = data.get("venue", "")
    for race in data.get("races", []):
        for runner in race.get("runners", []):
            rows.append({
                "race_date": race_date,
                "venue": venue,
                "race_number": race.get("race_number"),
                "race_name": race.get("race_name", ""),
                "distance": race.get("distance"),
                "race_class": race.get("race_class", ""),
                "race_course": race.get("race_course", ""),
                "going": race.get("going", ""),
                "place": runner.get("place", ""),
                "horse_no": runner.get("horse_no"),
                "horse_name": runner.get("horse_name", ""),
                "jockey": runner.get("jockey", ""),
                "trainer": runner.get("trainer", ""),
                "actual_weight": runner.get("actual_weight"),
                "draw": runner.get("draw"),
                "lbw": runner.get("lbw", ""),
                "finish_time": runner.get("finish_time", ""),
                "finish_time_seconds": runner.get("finish_time_seconds"),
                "win_odds": runner.get("win_odds", ""),
            })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, sheet_name="Results")
    return buf.getvalue()


def _append_results_to_db(results_path: Path):
    """Append scraped results to hkjc_results_updated.xlsx."""
    db_file = BASE / "hkjc_results_updated.xlsx"
    try:
        with open(results_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        st.error(f"Failed to read results JSON: {e}")
        return

    new_rows = []
    race_date = data.get("date", "")
    for race in data.get("races", []):
        is_awt = race.get("is_awt", False)
        track_type = "All Weather Track" if is_awt else "Turf"
        for runner in race.get("runners", []):
            new_rows.append({
                "race_date": race_date,
                "race_number": race.get("race_number"),
                "horse_no": runner.get("horse_no"),
                "horse_name": runner.get("horse_name", ""),
                "jockey": runner.get("jockey", ""),
                "trainer": runner.get("trainer", ""),
                "draw": runner.get("draw"),
                "finish_time_seconds": runner.get("finish_time_seconds"),
                "track_type": track_type,
                "race_course": race.get("race_course", ""),
                "distance": race.get("distance"),
            })
    if not new_rows:
        st.warning("No runner data found in results JSON.")
        return

    new_df = pd.DataFrame(new_rows)

    # Read existing DB (handle OneDrive lock)
    if db_file.exists():
        try:
            existing = pd.read_excel(db_file)
        except PermissionError:
            tmp = Path(tempfile.gettempdir()) / db_file.name
            shutil.copy2(db_file, tmp)
            existing = pd.read_excel(tmp)
        # Avoid duplicate rows for the same date
        if "race_date" in existing.columns:
            existing = existing[existing["race_date"] != race_date]
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df

    # Write back (handle OneDrive lock)
    try:
        combined.to_excel(db_file, index=False)
    except PermissionError:
        tmp_out = Path(tempfile.gettempdir()) / db_file.name
        combined.to_excel(tmp_out, index=False)
        shutil.copy2(tmp_out, db_file)

    st.success(f"Appended {len(new_df)} rows to {db_file.name} "
               f"(total: {len(combined)} rows)")


def _run_results_scraper(date_str: str):
    """Invoke scrape_hkjc_results.py from the dashboard."""
    cmd = [PYTHON, str(BASE / "scrape_hkjc_results.py"), "--date", date_str]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    with st.spinner(f"Scraping results for {date_str}..."):
        result = subprocess.run(cmd, env=env, cwd=str(BASE),
                                capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0:
            st.success(f"Results scraped for {date_str}")
            with st.expander("Output"):
                st.code(result.stdout[-2000:] if len(result.stdout) > 2000
                        else result.stdout)
            # Auto-append to DB
            date_compact = date_str.replace("-", "")
            results_json = REPORTS / f"results_{date_compact}.json"
            if results_json.exists():
                _append_results_to_db(results_json)
        else:
            st.error(f"Scraper failed (exit code {result.returncode})")
            with st.expander("Error"):
                st.code(result.stderr[-2000:] if result.stderr
                        else result.stdout[-2000:])


def _run_backtest_single(date_str: str):
    """Run backtest for a single meeting."""
    cmd = [PYTHON, str(BASE / "backtest_model.py"), "--date", date_str]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    with st.spinner(f"Running backtest for {date_str}..."):
        result = subprocess.run(cmd, env=env, cwd=str(BASE),
                                capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0:
            st.success(f"Backtest complete for {date_str}")
            with st.expander("Output"):
                st.code(result.stdout[-2000:] if len(result.stdout) > 2000
                        else result.stdout)
        else:
            st.error(f"Backtest failed (exit code {result.returncode})")
            with st.expander("Error"):
                st.code(result.stderr[-2000:] if result.stderr
                        else result.stdout[-2000:])


def _run_backtest_agg(flag: str, value: str):
    """Run aggregate backtest (--month or --season)."""
    cmd = [PYTHON, str(BASE / "backtest_model.py"), flag, value]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    with st.spinner(f"Running aggregate backtest ({flag} {value})..."):
        result = subprocess.run(cmd, env=env, cwd=str(BASE),
                                capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0:
            st.success(f"Aggregate backtest complete")
            with st.expander("Output"):
                st.code(result.stdout[-2000:] if len(result.stdout) > 2000
                        else result.stdout)
        else:
            st.error(f"Backtest failed")
            with st.expander("Error"):
                st.code(result.stderr[-2000:] if result.stderr
                        else result.stdout[-2000:])


# ══════════════════════════════════════════════════════════════════════════════
# Blackbook page
# ══════════════════════════════════════════════════════════════════════════════

def page_blackbook():
    st.title("📓 Blackbook")
    st.caption("Qualitative picks — track your eye vs the model")

    bb = _load_blackbook()
    _bb_expire_stale(bb)
    tag_defs = bb.get("tag_definitions", {})
    all_tags = sorted(tag_defs.keys())

    tab_manage, tab_analytics = st.tabs(["📝 Manage", "📊 Analytics"])

    # ── Manage tab ──────────────────────────────────────────
    with tab_manage:
        st.markdown("### Add New Entry")
        with st.form("bb_add_form", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1:
                horse_name = st.text_input("Horse Name *")
                source_race = st.text_input("Source Race (e.g. 2026-04-01 R4)")
                confidence = st.selectbox("Confidence", ["high", "medium", "low"])
            with c2:
                jockey_pref = st.text_input("Jockey Preference (optional)")
                pref_surface = st.selectbox("Preferred Surface", [None, "Turf", "AWT"])
                dist_text = st.text_input("Preferred Distance(s) (comma-sep, e.g. 1200,1400)")

            selected_tags = st.multiselect("Tags", all_tags,
                                           help="Select reasoning patterns")
            reasoning = st.text_area("Reasoning *", height=100,
                                     placeholder="e.g. Held up in traffic R4, only clear last 100m. "
                                     "Ran 23.4 final sec — top-3 sectional. Watch for better draw.")

            submitted = st.form_submit_button("💾 Save to Blackbook", type="primary")
            if submitted:
                if not horse_name.strip():
                    st.error("Horse name is required.")
                elif not reasoning.strip():
                    st.error("Reasoning is required.")
                else:
                    dists = []
                    if dist_text.strip():
                        dists = [int(d.strip()) for d in dist_text.split(",") if d.strip().isdigit()]
                    _bb_add_entry(bb, horse_name, reasoning, selected_tags, confidence,
                                  source_race=source_race, preferred_distance=dists,
                                  preferred_surface=pref_surface, jockey_preference=jockey_pref)
                    st.success(f"Added **{horse_name.upper()}** to blackbook!")
                    st.rerun()

        st.markdown("---")

        # ── Import from Excel / CSV ───────────────────────
        with st.expander("📤 Bulk Import (Excel / CSV)"):
            st.markdown("Upload a file with columns: **horse_name**, **reasoning**, "
                        "**tags** (comma-separated), **confidence**, **source_race** (optional)")
            uploaded = st.file_uploader("Choose file", type=["xlsx", "csv"],
                                        key="bb_import_file")
            if uploaded:
                try:
                    if uploaded.name.endswith(".csv"):
                        imp_df = pd.read_csv(uploaded)
                    else:
                        imp_df = pd.read_excel(uploaded)
                    n_imported = 0
                    for _, row in imp_df.iterrows():
                        hn = str(row.get("horse_name", "")).strip()
                        rsn = str(row.get("reasoning", "")).strip()
                        if not hn or not rsn:
                            continue
                        tags_raw = str(row.get("tags", ""))
                        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
                        conf = str(row.get("confidence", "medium")).strip().lower()
                        src = str(row.get("source_race", "")).strip()
                        _bb_add_entry(bb, hn, rsn, tags, conf, source_race=src)
                        n_imported += 1
                    st.success(f"Imported {n_imported} entries.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Import failed: {e}")

        st.markdown("---")

        # ── Active entries ────────────────────────────────
        st.markdown("### Active Entries")
        active = [e for e in bb["entries"] if e["status"] == "active"]
        expired = [e for e in bb["entries"] if e["status"] == "expired"]
        archived = [e for e in bb["entries"] if e["status"] == "archived"]

        if not active:
            st.info("No active blackbook entries yet.")
        else:
            for e in sorted(active, key=lambda x: x["added_date"], reverse=True):
                tags_str = " · ".join(e.get("tags", []))
                n_perf = len(e.get("performances", []))
                perf_summary = ""
                if n_perf:
                    validated = sum(1 for p in e["performances"]
                                   if p.get("bb_verdict") == "VALIDATED")
                    perf_summary = f" | Runs: {n_perf} (✓{validated})"

                with st.expander(
                    f"**{e['horse_name']}** — {e['confidence'].upper()} · "
                    f"{tags_str}{perf_summary} "
                    f"(expires {e['expiry_date']})"
                ):
                    st.markdown(f"**ID:** {e['id']}  |  **Added:** {e['added_date']}  |  "
                                f"**Source:** {e.get('source_race', 'N/A')}")
                    st.markdown(f"**Reasoning:** {e['reasoning']}")
                    cond = e.get("conditions", {})
                    if any(cond.values()):
                        parts = []
                        if cond.get("preferred_distance"):
                            parts.append(f"Dist: {cond['preferred_distance']}")
                        if cond.get("preferred_surface"):
                            parts.append(f"Surface: {cond['preferred_surface']}")
                        if cond.get("jockey_preference"):
                            parts.append(f"Jockey: {cond['jockey_preference']}")
                        st.markdown(f"**Conditions:** {' · '.join(parts)}")

                    # Performance history
                    if e.get("performances"):
                        st.markdown("**Performances:**")
                        for pf in e["performances"]:
                            verdict_icon = {"VALIDATED": "✅", "PARTIAL": "🟡",
                                            "MISSED": "❌"}.get(pf.get("bb_verdict", ""), "⚪")
                            st.markdown(
                                f"- {pf.get('date', '?')} R{pf.get('race_number', '?')} — "
                                f"Finish: **{pf.get('finish', '?')}** "
                                f"(Model rank #{pf.get('model_rank', '?')}) "
                                f"{verdict_icon} {pf.get('bb_verdict', '')}"
                                f"{' — ' + pf['notes'] if pf.get('notes') else ''}"
                            )

                    # Actions
                    c1, c2, c3 = st.columns(3)
                    if c1.button("🗑 Archive", key=f"arch_{e['id']}"):
                        _bb_update_entry(bb, e["id"], status="archived")
                        st.rerun()
                    if c2.button("📅 +45 days", key=f"ext_{e['id']}"):
                        new_exp = (date.fromisoformat(e["expiry_date"])
                                   + timedelta(days=45)).isoformat()
                        _bb_update_entry(bb, e["id"], expiry_date=new_exp)
                        st.rerun()
                    if c3.button("🔄 Reactivate" if e["status"] != "active" else "⏹ Expire",
                                 key=f"toggle_{e['id']}"):
                        new_status = "active" if e["status"] != "active" else "expired"
                        _bb_update_entry(bb, e["id"], status=new_status)
                        st.rerun()

        # ── Expired / Archived ────────────────────────────
        if expired or archived:
            with st.expander(f"📦 Expired ({len(expired)}) / Archived ({len(archived)})"):
                for e in expired + archived:
                    st.markdown(
                        f"- **{e['horse_name']}** ({e['status']}) — "
                        f"Added {e['added_date']}, {len(e.get('performances', []))} runs "
                        f"| {', '.join(e.get('tags', []))}"
                    )
                    if st.button(f"🔄 Reactivate {e['horse_name']}", key=f"react_{e['id']}"):
                        new_exp = (date.today() + timedelta(days=DEFAULT_EXPIRY_DAYS)).isoformat()
                        _bb_update_entry(bb, e["id"], status="active", expiry_date=new_exp)
                        st.rerun()

    # ── Analytics tab ──────────────────────────────────────
    with tab_analytics:
        all_entries = bb["entries"]
        all_perfs = [(e, p) for e in all_entries for p in e.get("performances", [])]

        if not all_perfs:
            st.info("No performance data yet. Add entries and record results to see analytics.")
        else:
            st.markdown("### Tag Hit-Rates")
            tag_stats = defaultdict(lambda: {"total": 0, "top3": 0, "top5": 0, "wins": 0})
            for e, p in all_perfs:
                try:
                    place = int(str(p.get("finish", "99")).strip().rstrip("stndrdth"))
                except ValueError:
                    place = 99
                for tag in e.get("tags", []):
                    tag_stats[tag]["total"] += 1
                    if place <= 1:
                        tag_stats[tag]["wins"] += 1
                    if place <= 3:
                        tag_stats[tag]["top3"] += 1
                    if place <= 5:
                        tag_stats[tag]["top5"] += 1

            t_rows = []
            for tag, st_d in sorted(tag_stats.items()):
                n = st_d["total"]
                t_rows.append({
                    "Tag": tag,
                    "Runs": n,
                    "Win%": f"{st_d['wins']/n*100:.0f}%" if n else "—",
                    "Top-3%": f"{st_d['top3']/n*100:.0f}%" if n else "—",
                    "Top-5%": f"{st_d['top5']/n*100:.0f}%" if n else "—",
                })
            st.dataframe(pd.DataFrame(t_rows), use_container_width=True, hide_index=True)

            st.markdown("---")
            st.markdown("### Confidence Calibration")
            conf_stats = defaultdict(lambda: {"total": 0, "top3": 0, "wins": 0})
            for e, p in all_perfs:
                try:
                    place = int(str(p.get("finish", "99")).strip().rstrip("stndrdth"))
                except ValueError:
                    place = 99
                conf = e.get("confidence", "medium")
                conf_stats[conf]["total"] += 1
                if place <= 1:
                    conf_stats[conf]["wins"] += 1
                if place <= 3:
                    conf_stats[conf]["top3"] += 1

            c_rows = []
            for conf in ["high", "medium", "low"]:
                sd = conf_stats[conf]
                n = sd["total"]
                c_rows.append({
                    "Confidence": conf.title(),
                    "Runs": n,
                    "Win%": f"{sd['wins']/n*100:.0f}%" if n else "—",
                    "Top-3%": f"{sd['top3']/n*100:.0f}%" if n else "—",
                })
            st.dataframe(pd.DataFrame(c_rows), use_container_width=True, hide_index=True)

            st.markdown("---")
            st.markdown("### Model Alignment")
            agree = 0
            bb_only = 0
            model_only_wins = 0
            agree_wins = 0
            bb_only_wins = 0
            for e, p in all_perfs:
                try:
                    place = int(str(p.get("finish", "99")).strip().rstrip("stndrdth"))
                except ValueError:
                    place = 99
                model_rank = p.get("model_rank", 99)
                is_win = place <= 3
                if model_rank <= 4:
                    agree += 1
                    if is_win:
                        agree_wins += 1
                else:
                    bb_only += 1
                    if is_win:
                        bb_only_wins += 1

            c1, c2, c3 = st.columns(3)
            c1.metric("Aligned (model top-4 + BB)", agree,
                      help="Horse in both your blackbook and model top 4")
            c2.metric("BB-Only (model missed)", bb_only,
                      help="Horse in your blackbook but not in model top 4")
            c3.metric("Total Tracked Runs", len(all_perfs))

            mc1, mc2 = st.columns(2)
            mc1.metric("Aligned Top-3%",
                       f"{agree_wins/agree*100:.0f}%" if agree else "—")
            mc2.metric("BB-Only Top-3%",
                       f"{bb_only_wins/bb_only*100:.0f}%" if bb_only else "—")


# ══════════════════════════════════════════════════════════════════════════════
# Results browser page
# ══════════════════════════════════════════════════════════════════════════════

def page_results():
    st.title("📋 Race Results Browser")
    st.caption("Browse scraped results and add horses to Blackbook")

    # Sidebar: scrape + date select
    st.sidebar.markdown("---")
    st.sidebar.subheader("Results Data")
    scrape_date = st.sidebar.date_input("Date", value=date.today(), key="res_scrape_date")
    if st.sidebar.button("⬇ Scrape Results", use_container_width=True, key="res_btn_scrape"):
        _run_results_scraper(scrape_date.isoformat())
        st.cache_data.clear()
        st.rerun()

    # Find available results
    res_dates = find_results_dates()
    if not res_dates:
        st.info("No scraped results found. Use the sidebar to scrape results for a date.")
        return

    # Date selector
    date_labels = {dc: f"{dc[:4]}-{dc[4:6]}-{dc[6:]}" for dc in sorted(res_dates, reverse=True)}
    selected_dc = st.selectbox("Select meeting date:", list(date_labels.keys()),
                               format_func=lambda k: date_labels[k], key="res_date_select")

    data = _load_results_json(selected_dc)
    if not data:
        st.error("Could not load results file.")
        return

    races = data.get("races", [])
    if not races:
        st.warning("No races found in results file.")
        return

    st.markdown(f"**Venue:** {data.get('venue', '?')}  |  "
                f"**Races:** {data.get('n_races', 0)}  |  "
                f"**Scraped:** {data.get('scraped_at', '?')[:16]}")

    # Load BB and prediction data for model rank comparison
    bb = _load_blackbook()
    _bb_expire_stale(bb)
    bb_names = {e["horse_name"].upper() for e in _bb_active_entries(bb)}

    # Load prediction JSON if available for model rank display
    pred_path = REPORTS / f"race_day_report_{selected_dc}_v3.4.8.json"
    model_ranks = {}  # {(race_number, horse_name_upper): rank}
    if pred_path.exists():
        try:
            with open(pred_path, "r", encoding="utf-8") as f:
                pred_data = json.load(f)
            for race in pred_data.get("races", []):
                for pick in race.get("picks", []):
                    model_ranks[(race["race_number"],
                                 pick["horse_name"].upper())] = pick["rank"]
        except Exception:
            pass

    # Race selector
    race_nums = [r.get("race_number", i+1) for i, r in enumerate(races)]
    selected_rn = st.selectbox("Race:", race_nums,
                               format_func=lambda rn: f"Race {rn}",
                               key="res_race_select")

    race = next((r for r in races if r.get("race_number") == selected_rn), None)
    if not race:
        return

    # Race header
    st.markdown(
        f"### Race {race['race_number']} — {race.get('race_name', '')}\n"
        f"**{race.get('distance', '?')}m** · "
        f"{'AWT' if race.get('is_awt') else 'Turf'} · "
        f"Course {race.get('race_course', '?')} · "
        f"Class {race.get('race_class', '?')} · "
        f"Going: {race.get('going', '?')}"
    )

    # Results table
    runners = race.get("runners", [])
    res_rows = []
    for r in runners:
        hname = r.get("horse_name", "")
        in_bb = hname.upper() in bb_names
        m_rank = model_ranks.get((race["race_number"], hname.upper()), "—")
        res_rows.append({
            "Place": r.get("place", ""),
            "No": r.get("horse_no", ""),
            "Horse": hname,
            "BB": "📓" if in_bb else "",
            "Model Rk": m_rank,
            "Jockey": r.get("jockey", ""),
            "Trainer": r.get("trainer", ""),
            "Wt": r.get("actual_weight", ""),
            "Draw": r.get("draw", ""),
            "Finish Time": r.get("finish_time", ""),
            "Win Odds": r.get("win_odds", ""),
            "LBW": r.get("lbw", ""),
        })

    st.dataframe(pd.DataFrame(res_rows), use_container_width=True, hide_index=True)

    # Download results as Excel
    _xl_bytes = _results_json_to_excel_bytes(REPORTS / f"results_{selected_dc}.json")
    if _xl_bytes:
        st.download_button("📥 Download Results (Excel)", _xl_bytes,
                           file_name=f"results_{selected_dc}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           key="res_dl_xlsx")

    st.markdown("---")

    # ── Add to Blackbook from results ─────────────────────
    st.markdown("### 📓 Add to Blackbook")
    runner_names = [r.get("horse_name", "") for r in runners if r.get("horse_name")]
    tag_defs = bb.get("tag_definitions", {})
    all_tags = sorted(tag_defs.keys())

    with st.form(f"bb_from_results_{selected_dc}_{selected_rn}", clear_on_submit=True):
        sel_horse = st.selectbox("Select horse:", runner_names, key="res_bb_horse")
        sel_tags = st.multiselect("Tags:", all_tags, key="res_bb_tags")
        sel_conf = st.selectbox("Confidence:", ["high", "medium", "low"], key="res_bb_conf")
        sel_reason = st.text_area("Reasoning *", height=80, key="res_bb_reason",
                                  placeholder="Why is this horse worth following?")
        sel_jockey = st.text_input("Jockey preference (optional)", key="res_bb_jockey")

        if st.form_submit_button("📓 Add to Blackbook", type="primary"):
            if not sel_reason.strip():
                st.error("Reasoning is required.")
            else:
                src = f"{selected_dc[:4]}-{selected_dc[4:6]}-{selected_dc[6:]} R{selected_rn}"
                _bb_add_entry(bb, sel_horse, sel_reason, sel_tags, sel_conf,
                              source_race=src, jockey_preference=sel_jockey)
                st.success(f"Added **{sel_horse.upper()}** to Blackbook!")
                st.rerun()

    # ── Record performance for existing BB entries ────────
    bb_in_race = [(r_item, e) for r_item in runners
                  for e in _bb_active_entries(bb)
                  if e["horse_name"] == r_item.get("horse_name", "").upper()]
    if bb_in_race:
        st.markdown("---")
        st.markdown("### 📊 Record Blackbook Performance")
        for r_item, e in bb_in_race:
            place = r_item.get("place", "")
            m_rank = model_ranks.get((race["race_number"],
                                     r_item["horse_name"].upper()), "?")
            # Check if already recorded
            already = any(
                p.get("date") == f"{selected_dc[:4]}-{selected_dc[4:6]}-{selected_dc[6:]}"
                and p.get("race_number") == race["race_number"]
                for p in e.get("performances", [])
            )
            if already:
                st.markdown(f"✓ **{e['horse_name']}** — already recorded for this race.")
                continue

            try:
                place_num = int(str(place).strip().rstrip("stndrdth"))
            except (ValueError, TypeError):
                place_num = 99
            if place_num <= 3:
                verdict = "VALIDATED"
            elif place_num <= 5:
                verdict = "PARTIAL"
            else:
                verdict = "MISSED"

            with st.form(f"perf_{e['id']}_{selected_dc}_{selected_rn}"):
                st.markdown(f"**{e['horse_name']}** — Finished **{place}** "
                            f"(Model rank: #{m_rank}) — Verdict: **{verdict}**")
                perf_notes = st.text_input("Notes (optional)",
                                           key=f"pnote_{e['id']}_{selected_dc}")
                if st.form_submit_button(f"💾 Save Performance"):
                    perf = {
                        "date": f"{selected_dc[:4]}-{selected_dc[4:6]}-{selected_dc[6:]}",
                        "race_number": race["race_number"],
                        "finish": place,
                        "model_rank": m_rank if m_rank != "?" else None,
                        "bb_verdict": verdict,
                        "notes": perf_notes.strip(),
                    }
                    _bb_add_performance(bb, e["id"], perf)
                    st.success(f"Performance recorded for {e['horse_name']}!")
                    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Form Guide page
# ══════════════════════════════════════════════════════════════════════════════

def page_form_guide():
    st.title("📋 Form Guide")

    meetings = load_available_meetings()
    if not meetings:
        st.info("No analysed meetings found. Run an analysis first.")
        return

    # Sidebar: meeting selection
    st.sidebar.subheader("Form Guide")
    meeting_titles = [m["title"] for m in meetings]
    chosen_title = st.sidebar.selectbox("Meeting", meeting_titles, index=0,
                                        key="fg_meeting")
    sel = next(m for m in meetings if m["title"] == chosen_title)

    date_compact = sel["date_str"]
    date_iso = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:8]}"

    # Load data
    meeting_data = load_meeting_data(sel["file"])
    racecard = _load_racecard_cache(date_iso)
    form_db = _load_form_db()
    if form_db.empty:
        st.error("Historical database (hkjc_results_updated.xlsx) not found or empty.")
        return

    bb = _load_blackbook()
    _bb_expire_stale(bb)
    bb_lookup = _bb_active_lookup(bb)

    race_idx = _build_race_index(form_db)

    races = meeting_data.get("races", [])
    if not races:
        st.warning("No races in this meeting.")
        return

    # ── Static race navigation buttons ────────────────────────────────────
    st.caption(f"{meeting_data.get('meeting_title', '')} · "
               f"Historical DB: {len(form_db):,} records")

    btn_cols = st.columns(len(races))
    for i, race in enumerate(races):
        with btn_cols[i]:
            if st.button(f"R{race['race_number']}", key=f"fg_btn_{race['race_number']}",
                         use_container_width=True):
                st.session_state["fg_active_race"] = i

    active_idx = st.session_state.get("fg_active_race", 0)
    if active_idx >= len(races):
        active_idx = 0
    race = races[active_idx]

    # ── Race header ───────────────────────────────────────────────────────
    rn = race["race_number"]
    cls_str = f"Class {race['race_class']}" if race.get("race_class") else "Group"
    surface = "AWT" if race.get("is_awt") else "Turf"
    pace_info = ""
    if race.get("pace"):
        pace_badge = {"Fast": "🔴", "Neutral": "🟡", "Slow": "🟢"}.get(
            race["pace"], "⚪")
        pace_info = f" · Pace: {pace_badge} {race['pace']}"

    st.markdown(
        f"### R{rn} — {race.get('race_name', '')}\n"
        f"**{race['distance']}m {surface} ({race['race_course']})** · "
        f"{cls_str}{pace_info}"
    )

    # ── Get horse list from racecard cache ────────────────────────────────
    horses = []
    if racecard:
        for rc_race in racecard.get("races", []):
            if rc_race["meta"]["race_number"] == rn:
                horses = [h for h in rc_race["horses"]
                          if not h.get("is_standby")]
                break
    if not horses:
        horses = [
            {"horse_name": p["horse_name"], "horse_no": p["horse_no"],
             "jockey": p.get("jockey", ""), "draw": p.get("draw", ""),
             "rating": "?", "last_6_runs": ""}
            for p in race.get("picks", [])
        ]

    all_form_rows = []  # for download across all races
    any_form = False
    meeting_date = date.fromisoformat(date_iso)

    for horse in horses:
        hname = horse["horse_name"]
        hno = horse.get("horse_no", "?")

        mask = form_db["horse_name_upper"] == hname.strip().upper()
        horse_hist = form_db[mask & (form_db["race_date"] < meeting_date)]
        horse_hist = horse_hist.sort_values("race_date", ascending=False).head(5)

        if horse_hist.empty:
            continue  # Debutant — skip

        any_form = True
        bb_entry = bb_lookup.get(hname.strip().upper())
        bb_badge = " 📓" if bb_entry else ""
        current_rtg = horse.get("rating", "?")
        current_jockey = horse.get("jockey", "?")
        last6 = horse.get("last_6_runs", "")
        last6_str = f"  |  Last 6: {last6}" if last6 else ""

        # Horse header — inline, no expander
        st.markdown(
            f"**#{hno}  {hname}{bb_badge}**  |  Rtg: {current_rtg}"
            f"  |  J: {current_jockey}{last6_str}"
        )
        if bb_entry:
            tags = ", ".join(bb_entry.get("tags", []))
            st.caption(
                f"📓 Blackbook: {bb_entry.get('reasoning', '')}  "
                f"(Conf: {bb_entry.get('confidence', '?')}"
                f"{', Tags: ' + tags if tags else ''})"
            )

        form_rows = []
        for _, row in horse_hist.iterrows():
            rd = row["race_date"]
            rnum = row["race_number"]
            key = (rd, rnum)
            ri = race_idx.get(key, {"top5": [], "margin_2nd": "-"})

            date_disp = rd.strftime("%d/%m/%y") if hasattr(rd, "strftime") else str(rd)
            trk = str(row.get("race_track", "?"))[:2]
            crs = str(row.get("race_course", "?"))
            dist = int(row["distance"]) if pd.notna(row.get("distance")) else "?"
            going = str(row.get("going", "?"))
            # Class — strip decimals
            cls_raw = row.get("race_class", "?")
            try:
                cls_val = str(int(float(cls_raw)))
            except (ValueError, TypeError):
                cls_val = str(cls_raw)
            jock = str(row.get("jockey", "?"))
            rtg = str(int(row["rating"])) if pd.notna(row.get("rating")) else "?"
            gate = str(int(row["draw"])) if pd.notna(row.get("draw")) else "?"
            pos = _fmt_positions(row.get("running_positions"))
            margin = _fmt_margin(row.get("place"), row.get("lbw"), ri)
            ftime = _fmt_time(row.get("finish_time_seconds"))
            place_val = str(int(row["place_num"])) if pd.notna(row.get("place_num")) else "?"
            t5_str = _fmt_top5(ri["top5"], hname) if ri["top5"] else ""

            form_rows.append({
                "Date": date_disp,
                "Pl": place_val,
                "Dist": dist,
                "Trk": trk,
                "Crs": crs,
                "Gng": going,
                "Cls": cls_val,
                "Jockey": jock,
                "Rtg": rtg,
                "Gate": gate,
                "Pos": pos,
                "Margin": margin,
                "Time": ftime,
                "Top 5 (* = self)": t5_str,
            })

            # For download
            t5_plain = " | ".join(f"{p}.{n}" for p, n in ri.get("top5", []))
            all_form_rows.append({
                "Race": f"R{rn}",
                "Horse#": hno,
                "Horse": hname,
                "BB": "Y" if bb_entry else "",
                "Date": date_disp,
                "Place": place_val,
                "Dist": dist,
                "Track": trk,
                "Course": crs,
                "Going": going,
                "Class": cls_val,
                "Jockey": jock,
                "Rating": rtg,
                "Gate": gate,
                "Positions": pos,
                "Margin": margin,
                "Finish Time": ftime,
                "Top 5": t5_plain,
            })

        fdf = pd.DataFrame(form_rows)

        # Style: highlight winning margins in red+bold
        def _style_margin(val):
            if str(val).startswith("+"):
                return "color: #ef4444; font-weight: bold"
            return ""

        styled = fdf.style.map(_style_margin, subset=["Margin"]) \
                          .set_properties(**{"text-align": "center", "font-size": "0.85em"}) \
                          .set_properties(subset=["Jockey", "Top 5 (* = self)"],
                                          **{"text-align": "left"})
        st.dataframe(styled, use_container_width=True, hide_index=True)

    if not any_form:
        st.info("No historical form found for horses in this race (all debutants).")

    # Sidebar download — collects current race only but still useful
    if all_form_rows:
        dl_df = pd.DataFrame(all_form_rows)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            dl_df.to_excel(writer, index=False, sheet_name="Form Guide")
        st.sidebar.download_button(
            "📥 Download Form Guide",
            data=buf.getvalue(),
            file_name=f"form_guide_{date_compact}_R{rn}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


# ══════════════════════════════════════════════════════════════════════════════
# Entry point — page router
# ══════════════════════════════════════════════════════════════════════════════

def main():
    # Sidebar navigation
    st.sidebar.title("🏇 HKJC Dashboard")
    page = st.sidebar.radio("Navigate", ["Race Day", "Form Guide", "Backtest", "Results", "Blackbook"],
                            index=0, key="nav_page")
    st.sidebar.markdown("---")

    if page == "Race Day":
        selected = sidebar_race_day()
        page_race_day(selected)
    elif page == "Form Guide":
        page_form_guide()
    elif page == "Backtest":
        page_backtest()
    elif page == "Results":
        page_results()
    elif page == "Blackbook":
        page_blackbook()


def sidebar_race_day():
    """Race Day sidebar controls (moved from old sidebar())."""
    # Run new meeting
    st.sidebar.subheader("Run New Meeting")
    col1, col2 = st.sidebar.columns(2)
    with col1:
        run_date = st.date_input("Race Date", value=date.today(), key="run_date")
    with col2:
        no_cache = st.checkbox("Re-scrape", value=False, key="no_cache")

    going_turf = st.sidebar.text_input("Turf Going", value="Good", key="going_turf")
    going_awt = st.sidebar.text_input("AWT Going", value="Good", key="going_awt")

    if st.sidebar.button("▶ Run Analysis", type="primary", use_container_width=True):
        run_pipeline(run_date.isoformat(), no_cache, going_turf, going_awt)
        st.cache_data.clear()
        st.rerun()

    st.sidebar.markdown("---")

    # Meeting selector
    st.sidebar.subheader("Meetings")
    meetings = load_available_meetings()

    if not meetings:
        st.sidebar.info("No analysed meetings found.\nRun your first analysis above!")
        return None

    options = {m["title"]: m for m in meetings}
    selected = st.sidebar.radio(
        "Select meeting:",
        list(options.keys()),
        index=0,
        key="meeting_select",
    )

    m = options[selected]
    st.sidebar.caption(
        f"📅 {m['date_str'][:4]}-{m['date_str'][4:6]}-{m['date_str'][6:]}  "
        f"| 🏟 {m['venue']}  | 🏁 {m['n_races']} races"
    )

    if m.get("generated_at"):
        try:
            gen = datetime.fromisoformat(m["generated_at"])
            st.sidebar.caption(f"Generated: {gen.strftime('%d %b %Y %H:%M')}")
        except ValueError:
            pass

    return m


if __name__ == "__main__":
    main()
