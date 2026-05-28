"""
Generate a readable text report + PDF for form_screen_<YYYYMMDD>.json.
Usage: python _form_screen_report.py 2026-05-31
"""
import json
import sys
import pathlib
import textwrap
from datetime import date

# ── Config ────────────────────────────────────────────────────────────────────
ROOT      = pathlib.Path(__file__).parent
REPORTS   = ROOT / "reports"
RACECARD  = ROOT / "racecards"
FONT_PATH = r"C:\Windows\Fonts\msjh.ttc"          # CJK fallback
FONT_PATH_ALT = r"C:\Windows\Fonts\arial.ttf"     # basic fallback

DATE_ISO  = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
YYYYMMDD  = DATE_ISO.replace("-", "")
JSON_PATH = REPORTS / f"form_screen_{YYYYMMDD}.json"

# ── Load ──────────────────────────────────────────────────────────────────────
if not JSON_PATH.exists():
    sys.exit(f"ERROR: {JSON_PATH} not found — run form_screener.py --date {DATE_ISO} first")

data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
races = data.get("races", [])

# ─────────────────────────────────────────────────────────────────────────────
# 1.  Plain-text / Markdown report
# ─────────────────────────────────────────────────────────────────────────────
SEP1 = "=" * 72
SEP2 = "-" * 72
SEP3 = "·" * 72

def fmt_score(v) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.3f}"
    except Exception:
        return str(v)

def extras_line(ex: dict) -> str:
    chips = []
    if ex.get("E1_gear_change"):
        chips.append(f"GEAR {ex['E1_gear_change']}")
    if ex.get("E3_run_up"):
        chips.append(f"RUN-UP:{ex['E3_run_up']}")
    if ex.get("E4_trip_change"):
        chips.append(f"TRIP:{ex['E4_trip_change']}")
    if ex.get("E8_going_record"):
        chips.append(f"GOING:{ex['E8_going_record']}")
    if ex.get("E9_freshness"):
        chips.append(f"FRESH:{ex['E9_freshness']}")
    if ex.get("E7_time_vs_class_par"):
        t = ex["E7_time_vs_class_par"]
        chips.append(f"TIME_VS_PAR:{t.get('delta_sec',0):+.2f}s({t.get('verdict','')})")
    return "  | ".join(chips) if chips else "(none)"

def h2h_block(h2h: list, horse_name: str) -> list[str]:
    lines = []
    for r in h2h:
        rival = r.get("rival", "?")
        lines.append(f"    vs {rival}  (rival wt today: {r.get('rival_actual_today','?')} lb)")
        for m in r.get("meetings", []):
            a_p = m.get("a_place", "?")
            b_p = m.get("b_place", "?")
            swing = m.get("swing_lbs")
            swing_s = f"  swing vs today: {swing:+.1f} lb" if swing is not None else ""
            lines.append(
                f"      {m.get('race_date','?')}  {m.get('distance','?')}m  "
                f"{m.get('going','?')}  Cls{m.get('race_class','?')}  "
                f"{horse_name[:14]} P{a_p}  {rival[:14]} P{b_p}  "
                f"A_wt:{m.get('a_weight','?')}  B_wt:{m.get('b_weight','?')}"
                f"{swing_s}"
            )
        if not r.get("meetings"):
            lines.append("      (no prior meetings found)")
    return lines

def form_lines_block(fls: list) -> list[str]:
    if not fls:
        return ["    (no prior runs in DB / form guide)"]
    headers = ("Date       Cls  Dist Going  Trk  Pos  Margin  Wt  Drw  "
               "CondMatch  ClassMv  Coll   Tags")
    lines = ["    " + headers,
             "    " + "-" * len(headers)]
    for fl in fls[:6]:                      # cap at 6 lines for readability
        tags = ", ".join(fl.get("incident_tags") or []) or "-"
        lines.append(
            f"    {str(fl.get('date','?')):<10} "
            f"{str(fl.get('race_class','?')):<4} "
            f"{str(fl.get('distance','?')):<5} "
            f"{str(fl.get('going','?')):<6} "
            f"{str(fl.get('track','?'))[:4]:<4} "
            f"{str(fl.get('place','?')):<4} "
            f"{str(fl.get('margin_lengths','?')):<7} "
            f"{str(fl.get('actual_weight','?')):<3} "
            f"{str(fl.get('draw','?')):<4} "
            f"{fmt_score(fl.get('conditions_match')):<10} "
            f"{str(fl.get('class_move_vs_today','?')):<8} "
            f"{fmt_score(fl.get('top5_next_score')):<6} "
            f"{tags[:40]}"
        )
    if len(fls) > 6:
        lines.append(f"    … (+{len(fls)-6} more runs)")
    return lines


def trial_lines_block(tls: list) -> list[str]:
    if not tls:
        return []
    headers = "Date       Course        Dist Going  Pos/N    LBW       Time      Comment"
    lines = ["      TRIAL FORM:",
             "    " + headers,
             "    " + "-" * len(headers)]
    for tl in tls[:4]:
        pos_n = f"{tl.get('final_pos','?')}/{tl.get('n_horses','?')}"
        lines.append(
            f"    {str(tl.get('date','?')):<10} "
            f"{str(tl.get('course','?'))[:12]:<13} "
            f"{str(tl.get('distance','?')):<4} "
            f"{str(tl.get('going','?'))[:5]:<6} "
            f"{pos_n:<8} "
            f"{str(tl.get('lbw','?'))[:8]:<9} "
            f"{str(tl.get('time','?'))[:8]:<9} "
            f"{(tl.get('comment') or '')[:60]}"
        )
    return lines

lines_txt: list[str] = []
lines_txt.append(SEP1)
lines_txt.append(f"  FORM SCREEN — {DATE_ISO}    (generated {date.today().isoformat()})")
lines_txt.append(SEP1)
lines_txt.append(f"  {len(races)} races")
lines_txt.append("")

for race in races:
    rno    = race.get("race_no", "?")
    rname  = race.get("race_name") or ""
    dist   = race.get("distance", "?")
    going  = race.get("going") or "n/a"
    rcls   = race.get("race_class") or "?"
    horses = race.get("horses", [])
    sl     = race.get("shortlist") or []
    sm     = race.get("stable_mates") or []

    lines_txt.append(SEP1)
    lines_txt.append(f"  RACE {rno}   {rname}")
    lines_txt.append(f"  {dist}m  |  Going: {going}  |  Class: {rcls}  |  {len(horses)} runners")
    lines_txt.append(SEP1)
    lines_txt.append("")

    # Shortlist
    lines_txt.append("  *** AUTO-SHORTLIST (top-3 by evidence weight) ***")
    if sl:
        for i, s in enumerate(sl, 1):
            lines_txt.append(f"  {i}. {s['horse']:<28}  |  {s['rationale']}")
    else:
        lines_txt.append("  (none)")
    lines_txt.append("")

    # Stable mates
    if sm:
        lines_txt.append("  STABLE-MATE WATCH:")
        for trainer, names in sm:
            lines_txt.append(f"    {trainer} → {', '.join(names)}")
        lines_txt.append("")

    lines_txt.append(SEP2)
    lines_txt.append("")

    # Per-horse
    for h in horses:
        nm   = h.get("horse_name", "?")
        hno  = h.get("horse_no", "?")
        drw  = h.get("draw", "?")
        wt   = h.get("actual_weight_today", "?")
        claim= h.get("claim", 0) or 0
        sub  = h.get("sub_scores") or h.get("scores") or {}
        ex   = h.get("extras") or {}
        bb   = h.get("blackbook")
        fls  = h.get("form_lines") or []
        tls  = h.get("trial_lines") or []
        h2h  = h.get("head_to_head") or []
        status = h.get("status") or ("raced" if fls else ("trial_only" if tls else "true_debutant"))

        claim_s = f"  (claim −{claim})" if claim else ""
        bb_s    = "  [BB]" if bb else ""
        status_badge = {
            "raced": "",
            "trial_only": "  [TRIAL-ONLY]",
            "true_debutant": "  [TRUE DEBUTANT]",
        }.get(status, "")
        lines_txt.append(
            f"  #{hno:<3} {nm:<28}  Draw {drw}  Wt {wt} lb{claim_s}{bb_s}{status_badge}"
        )
        lines_txt.append(
            f"        conditions_match={fmt_score(sub.get('conditions_match', sub.get('conditions')))}  "
            f"collateral_strength={fmt_score(sub.get('collateral_strength', sub.get('collateral')))}"
        )
        lines_txt.append(f"        Extras: {extras_line(ex)}")
        if bb:
            lines_txt.append(
                f"        Blackbook ({bb.get('status','?')}, conf {bb.get('confidence','?')}): "
                f"{bb.get('reasoning','')}"
            )
        lines_txt.append("")
        lines_txt.append("      FORM LINES:")
        lines_txt.extend(form_lines_block(fls))
        if tls:
            lines_txt.append("")
            lines_txt.extend(trial_lines_block(tls))
        if h2h:
            lines_txt.append("")
            lines_txt.append("      HEAD-TO-HEAD:")
            lines_txt.extend(h2h_block(h2h, nm))
        lines_txt.append("")
        lines_txt.append(SEP3)
        lines_txt.append("")

    lines_txt.append("")

txt_out = REPORTS / f"form_screen_{YYYYMMDD}.txt"
txt_out.write_text("\n".join(lines_txt), encoding="utf-8")
print(f"Text report: {txt_out}")

# ─────────────────────────────────────────────────────────────────────────────
# 2.  PDF report (reportlab)
# ─────────────────────────────────────────────────────────────────────────────
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Register fonts
_registered_font = "Helvetica"
for _fp in [FONT_PATH, FONT_PATH_ALT]:
    if pathlib.Path(_fp).exists():
        try:
            pdfmetrics.registerFont(TTFont("CustomFont", _fp))
            _registered_font = "CustomFont"
        except Exception:
            pass
        break

styles = getSampleStyleSheet()

def _style(name, parent="Normal", **kw):
    kw.setdefault("fontName", _registered_font)
    return ParagraphStyle(name, parent=styles[parent], **kw)

H1 = _style("H1", parent="Heading1", fontSize=14, spaceAfter=4, textColor=colors.HexColor("#1a3a5c"))
H2 = _style("H2", parent="Heading2", fontSize=11, spaceAfter=2, textColor=colors.HexColor("#1a3a5c"))
H3 = _style("H3", parent="Heading3", fontSize=9.5, spaceAfter=2, textColor=colors.HexColor("#2e5e8a"))
BODY = _style("BODY", fontSize=8, leading=11)
MONO = _style("MONO", fontSize=7, leading=9.5, fontName="Courier")
SMALL = _style("SMALL", fontSize=7, leading=9.5, textColor=colors.grey)
BOLD = _style("BOLD", fontSize=8.5, leading=11, fontName=(_registered_font if _registered_font != "Helvetica" else "Helvetica-Bold"))
SL_STYLE = _style("SL", fontSize=9, leading=12, textColor=colors.HexColor("#0b4d1e"), backColor=colors.HexColor("#e8f5e9"))
BB_STYLE = _style("BB", fontSize=8, leading=11, textColor=colors.HexColor("#5b2c00"), backColor=colors.HexColor("#fff3cd"))

W, H = A4
MARGIN = 18 * mm
COL_W  = W - 2 * MARGIN

def hr():
    return HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc"), spaceAfter=4, spaceBefore=4)

def race_header_table(rno, rname, dist, going, rcls, n_runners):
    data = [[
        Paragraph(f"<b>RACE {rno}</b>", H2),
        Paragraph(rname, BODY),
        Paragraph(f"{dist}m  |  Going: {going}  |  Class: {rcls}  |  {n_runners} runners", SMALL),
    ]]
    t = Table(data, colWidths=[18*mm, 80*mm, None])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8f0f8")),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.HexColor("#e8f0f8")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (0, -1), 6),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#1a3a5c")),
    ]))
    return t

def score_table(sub, ex, drw_band):
    cond = fmt_score(sub.get("conditions_match", sub.get("conditions")))
    coll = fmt_score(sub.get("collateral_strength", sub.get("collateral")))
    ex_s = extras_line(ex)
    data = [
        ["Conditions Match", "Collateral Strength", "Draw Band"],
        [cond, coll, str(drw_band or "-")],
        ["Extras", Paragraph(ex_s, MONO), ""],
    ]
    cw = [35*mm, 38*mm, 28*mm]
    t = Table(data, colWidths=cw)
    t.setStyle(TableStyle([
        ("FONTNAME",  (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",  (0, 0), (-1, 0), 7),
        ("FONTSIZE",  (0, 1), (-1, 1), 8),
        ("FONTSIZE",  (0, 2), (-1, -1), 7),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dce8f5")),
        ("BACKGROUND", (0, 1), (-1, 1), colors.white),
        ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#f8f8f8")),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#aaaaaa")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("SPAN", (1, 2), (2, 2)),
    ]))
    return t


def trial_lines_table(tls):
    if not tls:
        return None
    headers = ["Date", "Course", "Dist", "Going", "Pos/N", "LBW", "Time", "Comment"]
    rows = [headers]
    for tl in tls[:4]:
        pos_n = f"{tl.get('final_pos','?')}/{tl.get('n_horses','?')}"
        rows.append([
            str(tl.get("date", "?"))[:10],
            str(tl.get("course", "?"))[:14],
            str(tl.get("distance", "?")),
            str(tl.get("going", "?"))[:6],
            pos_n,
            str(tl.get("lbw", "?"))[:8],
            str(tl.get("time", "?"))[:8],
            Paragraph((tl.get("comment") or "")[:80], MONO),
        ])
    cws = [18*mm, 26*mm, 10*mm, 14*mm, 12*mm, 13*mm, 15*mm, None]
    t = Table(rows, colWidths=cws, repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTNAME",  (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",  (0, 0), (-1, -1), 6.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8f5e9")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f6fbf6")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bbbbbb")),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t

def form_lines_table(fls):
    if not fls:
        return Paragraph("(no prior runs)", SMALL)
    headers = ["Date", "Cls", "Dist", "Going", "Trk", "Pos", "Margin", "Wt",
               "Drw", "CondMatch", "ClassMv", "CollScore", "Tags"]
    rows = [headers]
    for fl in fls[:6]:
        tags = ", ".join(fl.get("incident_tags") or []) or "-"
        rows.append([
            str(fl.get("date", "?"))[:10],
            str(fl.get("race_class", "?")),
            str(fl.get("distance", "?")),
            str(fl.get("going", "?")),
            str(fl.get("track", "?"))[:5],
            str(fl.get("place", "?")),
            str(fl.get("margin_lengths", "?")),
            str(fl.get("actual_weight", "?")),
            str(fl.get("draw", "?")),
            fmt_score(fl.get("conditions_match")),
            str(fl.get("class_move_vs_today", "?")),
            fmt_score(fl.get("top5_next_score")),
            Paragraph(tags[:50], MONO),
        ])
    cws = [18*mm, 10*mm, 12*mm, 14*mm, 10*mm, 9*mm, 14*mm, 9*mm,
           9*mm, 17*mm, 14*mm, 16*mm, None]
    t = Table(rows, colWidths=cws, repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTNAME",  (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",  (0, 0), (-1, -1), 6.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dce8f5")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f5f8fc")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bbbbbb")),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t

def h2h_table(h2h, horse_name):
    if not h2h:
        return None
    all_rows = [["vs", "Date", "Dist", "Going", "Cls",
                 f"{horse_name[:10]}_P", "Rival_P",
                 "A_wt", "B_wt", "Swing"]]
    for r in h2h:
        rival = r.get("rival", "?")
        mtgs  = r.get("meetings", [])
        if not mtgs:
            all_rows.append([f"vs {rival}", "—", "—", "—", "—", "—", "—", "—", "—", "—"])
            continue
        for m in mtgs:
            swing = m.get("swing_lbs")
            all_rows.append([
                f"vs {rival[:16]}",
                str(m.get("race_date", "?"))[:10],
                str(m.get("distance", "?")),
                str(m.get("going", "?")),
                str(m.get("race_class", "?")),
                str(m.get("a_place", "?")),
                str(m.get("b_place", "?")),
                str(m.get("a_weight", "?")),
                str(m.get("b_weight", "?")),
                f"{swing:+.1f}" if swing is not None else "—",
            ])
    cws = [28*mm, 18*mm, 12*mm, 15*mm, 10*mm, 12*mm, 12*mm,
           12*mm, 12*mm, 12*mm]
    t = Table(all_rows, colWidths=cws, repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTNAME",  (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",  (0, 0), (-1, -1), 6.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0ead8")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#fffdf0")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bbbbbb")),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t

# Build flowables
story = []

# Cover
story.append(Spacer(1, 8*mm))
story.append(Paragraph(f"HKJC FORM SCREEN — {DATE_ISO}", H1))
story.append(Paragraph(f"Generated: {date.today().isoformat()}  |  "
                        f"{len(races)} races", SMALL))
story.append(Spacer(1, 4*mm))

# Quick summary table — one row per race
summary_hdr = ["Race", "Name", "Dist", "Going", "Class", "Runners", "Top-3 Shortlist"]
summary_rows = [summary_hdr]
for race in races:
    sl = [s["horse"] for s in (race.get("shortlist") or [])]
    summary_rows.append([
        str(race.get("race_no", "?")),
        Paragraph((race.get("race_name") or "")[:40], SMALL),
        str(race.get("distance", "?")),
        str(race.get("going") or "n/a"),
        str(race.get("race_class") or "?"),
        str(len(race.get("horses", []))),
        Paragraph(", ".join(sl), BODY),
    ])
sum_cws = [10*mm, 55*mm, 12*mm, 14*mm, 12*mm, 14*mm, None]
sum_t = Table(summary_rows, colWidths=sum_cws, repeatRows=1)
sum_t.setStyle(TableStyle([
    ("FONTNAME",  (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE",  (0, 0), (-1, -1), 7),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3a5c")),
    ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1),
     [colors.white, colors.HexColor("#f0f4fa")]),
    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#aaaaaa")),
    ("TOPPADDING",    (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ("LEFTPADDING",   (0, 0), (-1, -1), 4),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
]))
story.append(sum_t)
story.append(Spacer(1, 8*mm))

# Per-race detail
for race in races:
    rno     = race.get("race_no", "?")
    rname   = race.get("race_name") or ""
    dist    = race.get("distance", "?")
    going   = race.get("going") or "n/a"
    rcls    = race.get("race_class") or "?"
    horses  = race.get("horses", [])
    sl      = race.get("shortlist") or []
    sm      = race.get("stable_mates") or []

    block = []
    block.append(race_header_table(rno, rname, dist, going, rcls, len(horses)))
    block.append(Spacer(1, 2*mm))

    # Shortlist
    if sl:
        for i, s in enumerate(sl, 1):
            block.append(Paragraph(
                f"<b>#{i} {s['horse']}</b>  |  {s['rationale']}",
                SL_STYLE
            ))
    else:
        block.append(Paragraph("(shortlist not available)", SMALL))
    block.append(Spacer(1, 1.5*mm))

    # Stable mates
    if sm:
        sm_txt = "  |  ".join(f"{t}: {', '.join(ns)}" for t, ns in sm)
        block.append(Paragraph(f"<b>Stable-mate watch:</b> {sm_txt}", SMALL))
    block.append(Spacer(1, 3*mm))

    story.append(KeepTogether(block))
    story.append(hr())

    # Per-horse
    for h in horses:
        nm    = h.get("horse_name", "?")
        hno   = h.get("horse_no", "?")
        drw   = h.get("draw", "?")
        wt    = h.get("actual_weight_today", "?")
        claim = h.get("claim", 0) or 0
        sub   = h.get("sub_scores") or h.get("scores") or {}
        ex    = h.get("extras") or {}
        bb    = h.get("blackbook")
        fls   = h.get("form_lines") or []
        tls   = h.get("trial_lines") or []
        h2h   = h.get("head_to_head") or []
        drw_b = h.get("draw_band", "-")
        status = h.get("status") or ("raced" if fls else ("trial_only" if tls else "true_debutant"))

        claim_s = f" (claim −{claim})" if claim else ""
        bb_badge = " ★ BLACKBOOK" if bb else ""
        status_badge = {
            "raced": "",
            "trial_only": "  <font color='#1565c0'>[TRIAL-ONLY]</font>",
            "true_debutant": "  <font color='#c62828'>[TRUE DEBUTANT]</font>",
        }.get(status, "")

        horse_block = []
        horse_block.append(Paragraph(
            f"<b>#{hno} {nm}</b>  Draw {drw}  Wt {wt} lb{claim_s}{bb_badge}{status_badge}",
            H3
        ))
        horse_block.append(score_table(sub, ex, drw_b))
        if bb:
            horse_block.append(Paragraph(
                f"Blackbook ({bb.get('status','?')}, conf {bb.get('confidence','?')}): "
                + bb.get("reasoning", ""),
                BB_STYLE
            ))
        horse_block.append(Spacer(1, 1*mm))
        if fls:
            horse_block.append(Paragraph("<b>Form lines:</b>", BOLD))
            horse_block.append(form_lines_table(fls))
        if tls:
            horse_block.append(Spacer(1, 1*mm))
            horse_block.append(Paragraph("<b>Trial form:</b>", BOLD))
            t_tl = trial_lines_table(tls)
            if t_tl:
                horse_block.append(t_tl)
        if not fls and not tls:
            horse_block.append(Paragraph("(no prior runs or trials — true debutant)", SMALL))
        if h2h:
            horse_block.append(Spacer(1, 1*mm))
            horse_block.append(Paragraph("<b>Head-to-head vs today's rivals:</b>", BOLD))
            t_h2h = h2h_table(h2h, nm)
            if t_h2h:
                horse_block.append(t_h2h)
        horse_block.append(Spacer(1, 2*mm))

        story.append(KeepTogether(horse_block))

    story.append(Spacer(1, 6*mm))

pdf_out = REPORTS / f"form_screen_{YYYYMMDD}.pdf"
doc = SimpleDocTemplate(
    str(pdf_out),
    pagesize=A4,
    leftMargin=MARGIN, rightMargin=MARGIN,
    topMargin=16*mm, bottomMargin=16*mm,
    title=f"HKJC Form Screen {DATE_ISO}",
    author="HKJC Analysis Pipeline",
)
doc.build(story)
print(f"PDF report:  {pdf_out}")
print("Done.")
