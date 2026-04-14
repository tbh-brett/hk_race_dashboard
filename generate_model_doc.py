"""Generate a compact PDF explaining the HKJC race-day model (v4.4)."""
import os
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                 Table, TableStyle, PageBreak, KeepTogether)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY

BASE = Path(r"c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards")
OUT  = BASE / "reports" / "model_explanation_v4.4.pdf"

def build():
    doc = SimpleDocTemplate(str(OUT), pagesize=A4,
                            leftMargin=18*mm, rightMargin=18*mm,
                            topMargin=18*mm, bottomMargin=18*mm)
    ss = getSampleStyleSheet()

    # Custom styles
    title = ParagraphStyle("DocTitle", parent=ss["Title"], fontSize=18, spaceAfter=6*mm)
    h1 = ParagraphStyle("H1", parent=ss["Heading1"], fontSize=14, spaceAfter=3*mm,
                         spaceBefore=5*mm, textColor=colors.HexColor("#2C3E50"))
    h2 = ParagraphStyle("H2", parent=ss["Heading2"], fontSize=11, spaceAfter=2*mm,
                         spaceBefore=3*mm, textColor=colors.HexColor("#34495E"))
    body = ParagraphStyle("Body", parent=ss["Normal"], fontSize=9.5, leading=13,
                           alignment=TA_JUSTIFY, spaceAfter=2*mm)
    mono = ParagraphStyle("Mono", parent=ss["Code"], fontSize=8.5, leading=11,
                           spaceAfter=2*mm, leftIndent=6*mm,
                           fontName="Courier", backColor=colors.HexColor("#F5F5F5"))
    caption = ParagraphStyle("Caption", parent=ss["Normal"], fontSize=8, leading=10,
                              textColor=colors.HexColor("#777777"), spaceAfter=1*mm)

    HDR_BG = colors.HexColor("#2C3E50")
    ALT_BG = colors.HexColor("#F0F4F8")

    story = []

    # ── Title ──────────────────────────────────────────────────────────────────
    story.append(Paragraph("HKJC Race-Day Projection Model — v4.4", title))
    story.append(Paragraph("Technical Overview &amp; Acronym Reference", ss["Heading3"]))
    story.append(Spacer(1, 4*mm))

    # ── 1. Overview ────────────────────────────────────────────────────────────
    story.append(Paragraph("1. Model Overview", h1))
    story.append(Paragraph(
        "The model projects finish times for every runner in an upcoming HKJC "
        "meeting. Each horse's projection combines a <b>baseline expected time</b> "
        "(ET) for its race conditions with a <b>personalised residual</b> derived "
        "from its historical performance, plus adjustments for draw, sectional "
        "profile, and predicted race position. Horses are ranked by projected time; "
        "win probabilities are computed via a Boltzmann distribution.", body))
    story.append(Paragraph(
        "The pipeline runs five sequential stages: (1) parse the race card, "
        "(2) load ET reference tables and compute draw offsets from the historical "
        "database, (3) precompute sectional zone deviations for all horses, "
        "(4) project each race, (5) generate outputs (PDF, text, JSON).", body))

    # ── 2. Pipeline Stages ─────────────────────────────────────────────────────
    story.append(Paragraph("2. Projection Pipeline", h1))

    story.append(Paragraph("2.1  Expected Time (ET) Lookup", h2))
    story.append(Paragraph(
        "ET is the median historical finish time for a given distance, going, "
        "weight band, course configuration, and class (or track type). It acts "
        "as the <b>baseline</b> — what an average horse would run under today's "
        "conditions. The 4-tier cascade tries progressively coarser groupings "
        "until it finds a reference with sufficient sample size:", body))
    story.append(Paragraph(
        "<b>ClassFine</b> (n≥2): distance + going + weight band + course + class<br/>"
        "<b>Fine</b> (n≥5): distance + going + weight band + course (all classes pooled, "
        "with class-band correction offset)<br/>"
        "<b>Coarse</b> (n≥5): distance + going + weight band + track type<br/>"
        "<b>Ultra</b> (n≥5): distance + going + track type (weight band dropped)", mono))
    story.append(Paragraph(
        "Weight is discretised into 5 bands: 105-115, 116-120, 121-125, 126-130, "
        "131-135 lbs (after subtracting apprentice claims).", body))

    story.append(Paragraph("2.2  Recency Residual (Ability Signal)", h2))
    story.append(Paragraph(
        "For each of the horse's historical runs, the model computes a "
        "<b>contextualised residual</b>:", body))
    story.append(Paragraph(
        "residual = finish_time − ET(run conditions) − race_pace_index "
        "+ position_credit − draw_offset − wide_running_cost", mono))
    story.append(Paragraph(
        "This removes race-level pace effects (tactical/slow races), rewards "
        "good finishing positions, and strips out trip luck (draw savings or "
        "wide-running costs). The residuals capture the horse's <b>true latent "
        "ability</b>.<br/><br/>"
        "Residuals are then combined into a single number via <b>recency-weighted "
        "averaging</b> (λ = 0.85 exponential decay — most recent run gets full "
        "weight, each prior run discounted 15%). Additional discounts apply for "
        "cross-surface (AWT→Turf ×0.50), cross-venue (HV↔ST ×0.50), and "
        "asymmetric distance penalties (stepping up penalised more than down). "
        "The most recent win gets a ×2 boost.", body))

    story.append(Paragraph("2.3  Projection Formula", h2))
    story.append(Paragraph(
        "projection = ET(today's conditions) + recency_residual + draw_offset "
        "+ uncertainty_penalty + surface_penalty + class_transition_adj "
        "+ sectional_adj + speed_map_positional_adj", mono))
    story.append(Paragraph(
        "No shrinkage toward the class mean is applied — the horse's own "
        "data drives the entire projection (<b>ability-anchored</b>). Thin-data "
        "uncertainty is handled by additive penalties, not by diluting the signal.", body))

    story.append(Paragraph("2.4  Draw Offset", h2))
    story.append(Paragraph(
        "For each (distance, course, draw) combination, the historical database "
        "provides a raw offset computed via the <b>race-median method</b>: "
        "residual = finish_time − race_median_time, then the draw's mean residual "
        "minus the overall mean. This normalises within each race, removing "
        "confounders like horse quality.<br/><br/>"
        "The raw offset is Bayesian-shrunk toward zero: <b>adj = (n / (n + 8)) × raw</b>. "
        "Outer draws in large fields (≥12 runners) are amplified by a field-size "
        "factor. Safety cap: ±1.50s.", body))

    story.append(Paragraph("2.5  Sectional Decomposition (v4.4)", h2))
    story.append(Paragraph(
        "Each historical run's sectional times are split into Early / Mid / Late "
        "zones (per-400m normalised), and deviations from the race median are "
        "computed. A horse's <b>sectional profile</b> (last 6 runs, recency-weighted) "
        "captures its finishing pattern.<br/><br/>"
        "Three components adjust the projection:<br/>"
        "  (a) <b>Late finishing signal</b>: avg_late_dev × 0.12 (strong finishers save time)<br/>"
        "  (b) <b>Distance suitability</b>: front-loaded SSI > 0.15 at ≥1600m penalised ×0.10 "
        "(fade risk); sprints: SSI × 0.04<br/>"
        "  (c) <b>Reliability</b>: late_std above 0.30 adds +0.02/unit (inconsistent finishers)",
        body))

    story.append(Paragraph("2.6  Speed Map", h2))
    story.append(Paragraph(
        "A 3-row × N-column grid predicts early-race positions. Columns "
        "(front→back) sorted by ESZ; rows (rail→wide) sorted by draw within "
        "each column. Positional advantage scored by style-position alignment, "
        "rail-saving at bends, and wide draw costs.<br/><br/>"
        "Two adjustment components:<br/>"
        "  (a) <b>Positional cost</b>: advantage × 0.06s (capped ±0.15s)<br/>"
        "  (b) <b>Pace × Position × Style (PPS)</b>: 36-entry lookup table mapping "
        "(pace bucket, grid row, running style) to seconds", body))

    story.append(Paragraph("2.7  Win Probability", h2))
    story.append(Paragraph(
        "Boltzmann distribution: p(i) ∝ exp(−(time_i − time_min) / T), where "
        "T = 0.80 × σ(field times). Class-tier scaling widens T for C4 (×1.30) "
        "and C5 (×1.50) where model precision is lower. Low-SF horses get "
        "confidence-deflated probabilities.", body))

    # ── 3. Uncertainty / Penalty System ────────────────────────────────────────
    story.append(Paragraph("3. Penalty &amp; Adjustment System", h1))
    data = [
        ["Penalty / Adjustment", "Trigger", "Effect"],
        ["Uncertainty (U+)", "n_runs < 4", "+0.10/√n  (strong debut: +0.06/√n)"],
        ["Surface mismatch (SP+)", "Few/no same-surface runs", "+0.18s (0 runs) or +0.12/√n"],
        ["Class step-up (CTP+)", "Modal class < today's class", "Penalty scales with ET gap − old-class margin"],
        ["Class step-down", "Modal class > today's class", "Bonus: -ET_gap × 0.60 × dominance"],
        ["Sectional late adj", "≥2 profiled runs", "±avg_late_dev × 0.12s"],
        ["Sectional SSI dist", "SSI>0.15 at ≥1600m", "+(SSI−0.15) × 0.10s"],
        ["Speed map position", "All projected horses", "-advantage × 0.06s (±0.15s cap)"],
        ["Pace×Position×Style", "All projected horses", "Lookup: -0.06s to +0.05s"],
    ]
    tbl = Table(data, colWidths=[42*mm, 42*mm, 90*mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HDR_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTSIZE", (0, 0), (-1, 0), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ] + [("BACKGROUND", (0, i), (-1, i), ALT_BG) for i in range(2, len(data), 2)]))
    story.append(tbl)

    # ── 4. Trajectory ──────────────────────────────────────────────────────────
    story.append(Paragraph("4. Trajectory Detection (Informational)", h1))
    story.append(Paragraph(
        "Horses are classified as Improving / Declining / Stable / Insufficient "
        "based on last-2 runs vs prior-3 baseline + OLS slope confirmation. "
        "Trajectory is <b>reported in commentary only</b> — no time adjustment "
        "is applied (removed v4.1, as recency λ=0.85 already captures form trends).",
        body))

    story.append(PageBreak())

    # ── 5. Acronym Reference ──────────────────────────────────────────────────
    story.append(Paragraph("5. Acronym &amp; Column Reference", h1))
    acro = [
        ["Term", "Meaning"],
        ["ET", "Expected Time — baseline finish time for conditions (from 4-tier cascade)"],
        ["Proj T", "Projected Time — ET + residual + all adjustments. Lower = faster = better."],
        ["Win%", "Win probability from Boltzmann distribution. Confidence-deflated for thin data."],
        ["Ability", "Mean raw residual (finish_time − ET) across all runs. Negative = faster than ET."],
        ["Eff Resid", "Effective Residual — recency-weighted, contextualised. The key ability signal."],
        ["SF", "Shrinkage Factor = n/(n+5). Higher = more data = more trusted. 0.50+ = high confidence."],
        ["Draw Off", "Bayesian-shrunk draw offset in seconds. Positive = disadvantageous draw."],
        ["SSI", "Sectional Speed Index = late_dev − early_dev. Negative = strong finisher. "
                "Positive = front-loaded (fade risk at distance)."],
        ["Fin Sec", "Projected final 400m sectional time. Source indicators: (blank)=horse, ~=blend, c=class."],
        ["ESZ", "Early Speed Z-score. Negative = naturally fast early. Used for speed map placement."],
        ["Style", "Dominant running style: Leader / On-Pace / Midfield / Closer / Unknown."],
        ["Vet", "Veterinary flag from vet report: RED / AMB(er) / INF(o) / blank."],
        ["U+", "Uncertainty penalty applied (n < 4 runs)."],
        ["SP+", "Surface mismatch penalty (few same-surface runs)."],
        ["CTP+", "Class Transition Penalty (stepping up in class)."],
        ["↑IMP", "Improving trajectory detected (informational)."],
        ["↓DEC", "Declining trajectory detected (informational)."],
        ["PPS", "Pace × Position × Style interaction adjustment from speed map."],
        ["RPI", "Race Pace Index — median field residual for a historical race. "
                "Subtracted from each run to remove tactical pace effects."],
        ["λ", "Recency decay parameter (0.85). Each prior run's weight = λ^(n−1−i)."],
        ["DB", "Historical database (~18,000 race-runs from HKJC)."],
    ]
    tbl2 = Table(acro, colWidths=[22*mm, 152*mm])
    tbl2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HDR_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTSIZE", (0, 0), (-1, 0), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ] + [("BACKGROUND", (0, i), (-1, i), ALT_BG) for i in range(2, len(acro), 2)]))
    story.append(tbl2)

    # ── 6. Key Constants ──────────────────────────────────────────────────────
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph("6. Key Tunable Constants", h1))
    consts = [
        ["Constant", "Value", "Purpose"],
        ["RECENCY_LAMBDA", "0.85", "Exponential decay per prior run"],
        ["UNCERTAINTY_BASE", "0.10s", "Thin-data penalty coefficient"],
        ["DRAW_SHRINKAGE_K", "8", "Bayesian shrinkage strength for draws"],
        ["SEC_LATE_DEV_COEFF", "0.12", "Late finishing signal weight"],
        ["SEC_SSI_DIST_COEFF", "0.10", "SSI distance-suitability weight (≥1600m)"],
        ["SEC_SSI_SPRINT_COEFF", "0.04", "SSI sprint weight (≤1200m)"],
        ["SMAP_TIME_COEFF", "0.06", "Speed map advantage → seconds conversion"],
        ["SMAP_TIME_CAP", "0.15s", "Max positional adjustment"],
        ["AWT_TURF_DISCOUNT", "0.50", "AWT run weight when today is Turf"],
        ["HV_ST_DISCOUNT", "0.50", "Cross-venue run weight discount"],
        ["LAST_WIN_BOOST", "2.0", "Recency weight multiplier for last-start winner"],
        ["POSITION_CREDIT", "-0.15 / -0.08 / -0.04", "Credit for 1st / 2nd / 3rd place"],
    ]
    tbl3 = Table(consts, colWidths=[42*mm, 30*mm, 102*mm])
    tbl3.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HDR_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTSIZE", (0, 0), (-1, 0), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ] + [("BACKGROUND", (0, i), (-1, i), ALT_BG) for i in range(2, len(consts), 2)]))
    story.append(tbl3)

    # ── 7. PDF Report Layout ──────────────────────────────────────────────────
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph("7. PDF Report Column Layout", h1))
    story.append(Paragraph(
        "Each race page contains an 18-column ranking table (Rk, No, Horse, Wt, "
        "Dr, Rtg, ExpT, ProjT, Win%, Ability, EffResid, SF, DrawOff, SSI, FinSec, "
        "ESZ, Style, Vet), followed by a speed map grid (3 rows × N columns, "
        "green = advantage, red = disadvantage), positional notes, up to 4 "
        "beneficiaries, and commentary bullets.<br/><br/>"
        "Top-3 rows are shaded green/blue/yellow. Win% ≥20% is bolded green.", body))

    # ── 8. Module Interaction ──────────────────────────────────────────────────
    story.append(Paragraph("8. Module Interaction Diagram", h1))
    story.append(Paragraph(
        "<b>Race Card</b> → horse_no, weight, draw, jockey<br/>"
        "<b>ET Refs</b> → expected_time for today's conditions<br/>"
        "<b>Historical DB</b> → all past runs → recency_residual, draw_offset, "
        "horse_profile (style, SF, ESZ), sectional_deviations<br/>"
        "<b>project_race()</b> combines: ET + recency_resid + draw_off + penalties "
        "+ sectional_adj + smap_adj → projected_time<br/>"
        "<b>enrich_with_risk()</b> → win_prob (Boltzmann)<br/>"
        "<b>compute_speed_map()</b> → grid placement → smap_time_adjustment<br/>"
        "<b>race_commentary()</b> → interpretive bullets<br/>"
        "<b>generate_pdf() / generate_text_report()</b> → outputs", mono))

    doc.build(story)
    print(f"PDF saved: {OUT}")

if __name__ == "__main__":
    build()
