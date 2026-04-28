"""Shared trial-intelligence loader.

Single source of truth used by ``dashboard.py``, ``betting_strategy.py``
and the future decision-engine plumbing so all surfaces agree on:

  * sentiment of an individual trial entry  (``++``/``+``/``0``/``-``)
  * archetype of a horse across their trial→race history
    (``HONEST_GOOD`` / ``FALSE_POSITIVE`` / ``HIDDEN_GEM`` /
     ``HONEST_POOR`` / ``MIXED`` / ``UNKNOWN``)
  * Bayesian honesty score (top-3 rate after positive trials, shrunk to
    population mean)

The two index JSONs are produced by ``trial_vs_race_study.py``::

    reports/trial_archetype_index.json
    reports/trial_honesty_index.json

This module is intentionally light (no streamlit / pandas dependency)
so it can be imported from CLI scripts and the live dashboard alike.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

BASE = Path(__file__).parent
REPORTS = BASE / "reports"

# ── Sentiment dictionaries (kept in lock-step with dashboard) ────────────
_CONCEAL_KW = [
    "held up", "under a hold", "not asked", "not extend", "eased",
    "cruised", "in hand", "restrain", "within himself", "not pushed", "no effort",
]
_NEG_KW = [
    "unimpressive", "ordinary", "poor", "disappointing", "limited",
    "failed", "struggled", "green", "slowly away",
]
_POS_PHRASES = [
    "ran on well", "quicken", "impressive", "easily", "strong",
    "stayed on", "hit the front", "to score", "won going away",
    "not fully tested", "not tested",
]


def trial_sentiment(entry: dict) -> tuple[str, list[str]]:
    """Return ``(flag, reasons)`` for a single trial entry.

    Same logic as ``dashboard._trial_sentiment`` so the two stay in sync.
    """
    comment = (entry.get("comment", "") or "").lower()
    rp = entry.get("running_positions", []) or []
    fp = rp[-1] if rp else None
    sp = rp[0] if rp else None
    n = entry.get("n_horses", 0) or 0

    has_pos = any(p in comment for p in _POS_PHRASES)
    has_neg = any(nk in comment for nk in _NEG_KW)
    is_concealed = any(kw in comment for kw in _CONCEAL_KW) and not has_neg
    has_eased = "eased" in comment and not has_neg
    top_half = isinstance(fp, int) and n > 0 and fp <= max(1, n // 2)
    won = isinstance(fp, int) and fp == 1 and n >= 3
    gained = (isinstance(sp, int) and isinstance(fp, int) and (sp - fp) >= 2)
    bottom_q = isinstance(fp, int) and n >= 4 and fp >= n - 1

    reasons: list[str] = []
    if won and (is_concealed or has_pos):
        return "++", ["Won under hold / with finish"]
    if won:
        return "++", ["Won trial"]
    if is_concealed and top_half:
        return "++", ["Concealed + top half"]
    if has_eased and has_pos:
        return "++", ["Eased + strong finish"]
    if has_pos and top_half:
        return "++", ["Positive phrase + top half"]
    if has_neg:
        return "-", ["Negative trial signal"]
    if bottom_q and not has_pos and not is_concealed:
        return "-", [f"Bottom-quartile finish ({fp}/{n})"]
    if has_eased:
        return "+", ["Eased (deliberately held)"]
    if is_concealed:
        return "+", ["Concealed form"]
    if gained:
        return "+", [f"Gained {(sp or 0) - (fp or 0)} positions"]
    if has_pos:
        return "+", ["Positive phrase"]
    if top_half and not has_neg:
        return "+", [f"Top-half finish ({fp}/{n})"]
    return "", reasons


# ── Archetype + honesty index loaders (cached on mtime) ──────────────────
_CACHE: dict[str, tuple[float, dict]] = {}


def _load_cached(name: str) -> dict:
    path = REPORTS / name
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    cur = _CACHE.get(name)
    if cur and cur[0] == mtime:
        return cur[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = {}
    _CACHE[name] = (mtime, data)
    return data


def load_archetype_index() -> dict:
    """Returns the dict from ``reports/trial_archetype_index.json``.

    Schema::
        {
          "population_top3_after_pos": 0.30,
          "population_top3_after_neg": 0.12,
          "n_horses": 1176,
          "horses": {"HORSE NAME UPPER": {archetype, n_pairs, ...}, ...}
        }
    """
    return _load_cached("trial_archetype_index.json")


def load_honesty_index() -> dict:
    """Returns the dict from ``reports/trial_honesty_index.json``."""
    return _load_cached("trial_honesty_index.json")


# ── Per-horse lookups ────────────────────────────────────────────────────
ARCHETYPE_LABELS = {
    "HONEST_GOOD":     ("Honest – delivers", "#22c55e"),
    "FALSE_POSITIVE":  ("Trial-flatterer",   "#f97316"),
    "HIDDEN_GEM":      ("Hidden gem",        "#3b82f6"),
    "HONEST_POOR":     ("Honest – poor",     "#9ca3af"),
    "MIXED":           ("Mixed",             "#a78bfa"),
    "UNKNOWN":         ("",                  "#6b7280"),
}


def archetype_for(horse_name: Optional[str]) -> dict:
    """Return ``{archetype, n_pairs, top3_rate, win_rate, summary, label, colour}``.

    Returns ``{"archetype": "UNKNOWN"}`` when not enough history exists.
    """
    if not horse_name:
        return {"archetype": "UNKNOWN", "label": "", "colour": "#6b7280"}
    idx = load_archetype_index()
    rec = (idx.get("horses") or {}).get(str(horse_name).strip().upper())
    if not rec:
        return {"archetype": "UNKNOWN", "label": "", "colour": "#6b7280"}
    label, colour = ARCHETYPE_LABELS.get(
        rec.get("archetype", "UNKNOWN"), ("", "#6b7280"))
    return {**rec, "label": label, "colour": colour}


def honesty_for(horse_name: Optional[str]) -> Optional[dict]:
    if not horse_name:
        return None
    idx = load_honesty_index()
    pop = idx.get("population_top3_rate_after_positive_trial") or 0.0
    target = str(horse_name).strip().upper()
    for rec in idx.get("horses", []) or []:
        if str(rec.get("horse", "")).strip().upper() == target:
            return {**rec, "population_top3_after_pos": pop}
    return None


# ── Score-side hook used by betting_strategy ─────────────────────────────
# Default magnitudes — tuned from the trial→race study, see
#   reports/trial_vs_race_study.md
_BASE_PLUS_BONUS  = 0.05    # match betting_strategy.CFG["trial_plus_bonus"]
_BASE_MINUS_PEN   = -0.05   # match betting_strategy.CFG["trial_minus_pen"]
_HONEST_GOOD_MULT = 1.4     # amplify positive bonus
_FALSE_POS_MULT   = 0.3     # heavily discount positive bonus
_HIDDEN_GEM_BUMP  = 0.03    # add even when current trial is neutral / negative
_HONEST_POOR_PEN  = -0.03   # extra drag on top of base
_WON_TRIAL_BONUS  = 0.04    # additional, applies only on top of trial+


def trial_score_delta(horse_name: Optional[str],
                      trial_flag: str = "",
                      won_last_trial: bool = False) -> tuple[float, list[str]]:
    """Total trial-derived score delta + audit reasons.

    Designed to be a drop-in replacement for the simple
    ``trial_plus_bonus`` / ``trial_minus_pen`` that ``betting_strategy``
    used to apply. Includes archetype / honesty modulation so a horse
    with a verified ``HONEST_GOOD`` history gets more lift than an
    unproven horse, while ``FALSE_POSITIVE`` types are heavily
    discounted even when their current trial flag is positive.

    Parameters
    ----------
    horse_name
        Used for archetype / honesty lookup. Case-insensitive.
    trial_flag
        Either the legacy ``+`` / ``-`` (from upstream race-day report)
        or the richer ``++`` / ``+`` / ``-`` from
        :func:`trial_sentiment`.
    won_last_trial
        Set when the horse's most recent trial was an outright win.

    Returns
    -------
    (delta, reasons)
    """
    delta = 0.0
    reasons: list[str] = []

    flag = (trial_flag or "").strip()
    if flag in ("++", "+"):
        delta += _BASE_PLUS_BONUS
        reasons.append(f"trial {flag}")
        if won_last_trial:
            delta += _WON_TRIAL_BONUS
            reasons.append("won trial outright")
    elif flag == "-":
        delta += _BASE_MINUS_PEN
        reasons.append("trial -")

    arc = archetype_for(horse_name)
    a = arc.get("archetype", "UNKNOWN")
    if a == "HONEST_GOOD" and delta > 0:
        bump = delta * (_HONEST_GOOD_MULT - 1.0)
        delta += bump
        reasons.append(f"HONEST_GOOD ×{_HONEST_GOOD_MULT}")
    elif a == "FALSE_POSITIVE" and delta > 0:
        cut = delta * (1.0 - _FALSE_POS_MULT)
        delta -= cut
        reasons.append(f"FALSE_POSITIVE ×{_FALSE_POS_MULT}")
    elif a == "HIDDEN_GEM" and flag in ("", "0", "-"):
        delta += _HIDDEN_GEM_BUMP
        reasons.append("HIDDEN_GEM bump")
    elif a == "HONEST_POOR" and flag != "+":
        delta += _HONEST_POOR_PEN
        reasons.append("HONEST_POOR drag")

    return round(delta, 4), reasons


def archetype_badge_html(horse_name: Optional[str]) -> str:
    """Inline-friendly coloured pill for the form guide / trials page."""
    arc = archetype_for(horse_name)
    if arc.get("archetype", "UNKNOWN") in ("UNKNOWN", "MIXED"):
        return ""
    label = arc.get("label") or arc.get("archetype")
    colour = arc.get("colour", "#6b7280")
    n = arc.get("n_pairs", 0)
    top3 = arc.get("top3_rate", 0)
    title = (f"Archetype: {arc.get('archetype')}\n"
             f"{n} trial→race pairs · top3 {top3:.0%} · win {arc.get('win_rate', 0):.0%}")
    return (f'<span title="{title}" '
            f'style="background:{colour};color:#0b0b14;'
            f'padding:1px 6px;border-radius:8px;font-size:0.72em;'
            f'font-weight:700;letter-spacing:0.3px;'
            f'margin-right:5px">{label}</span>')


# ── Smoke / self-test ────────────────────────────────────────────────────
if __name__ == "__main__":
    arch = load_archetype_index()
    hon = load_honesty_index()
    print(f"archetype index: {arch.get('n_horses', 0)} horses")
    print(f"honesty index:   {hon.get('n_horses', 0)} horses")
    if arch.get("horses"):
        sample = list(arch["horses"].keys())[:3]
        for s in sample:
            r = archetype_for(s)
            print(f"  {s:25s} {r['archetype']:14s}  {r['summary']}")
    # Demo score deltas
    for h in ["CASA OF HONOR", "BRILLIANT FIRE", "TOP TIME", "NONEXISTENT HORSE"]:
        d, why = trial_score_delta(h, trial_flag="+", won_last_trial=False)
        print(f"  {h:25s}  Δscore={d:+.3f}   ({'; '.join(why)})")
