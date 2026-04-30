"""multi_builder.py — Q+QPL multi-bet suggestion engine.

Built on the corrected April-2026 analysis findings:

  • `mixed_banker+box` (1 banker × 4-6 legs) was your +28% ROI bread-and-butter
  • Pure boxes (no banker) bleed (-13% to -63%)
  • Inverted banker edge: rank 1-2 banker = -59% ROI, rank 5+ = +63% to +76%
  • Stake band $200-$300 = +45% ROI; ≥$300 over-staked = -84%

Suggestion rule (default `contrarian_banker` mode):
  banker  : best positive-edge horse with model rank ∈ [2..6]
  legs    : model top-4 horses excluding banker, plus any horse with
            edge ≥ +5pp not already included
  stake   : flat $10 QIN + $10 QPL per banker pair (= 2 bets per leg)
  total   : 2 × n_legs × $10 (e.g. 4 legs → $80, 6 legs → $120)

Alternative `model_consensus` mode (the "agree" play):
  banker  : model rank-1 horse
  legs    : model top-4 plus quadrant-A consensus horses
  caveat  : negative ROI in April — keep for reference, not recommended

Public API:
  build_multi_suggestion(date_compact, venue_code, race_no, picks,
                          mode='contrarian_banker',
                          stake_per_pair=10.0,
                          min_legs=3, max_legs=6) → dict
  build_meeting_multi(date_compact, venue_code, picks_by_race,
                       **kwargs) → list[dict]
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

BASE = Path(__file__).parent
REPORTS = BASE / "reports"


def _safe_value_lens_rows(date_compact: str, venue_code: str, race_no: int,
                          picks: list[dict]) -> list[dict]:
    """Pull edge table from market_loader; empty list on any failure."""
    try:
        from market_loader import compute_edge_table
        out = compute_edge_table(date_compact, venue_code, int(race_no), picks)
        return out.get("rows") or []
    except Exception:
        return []


def _picks_by_rank(picks: list[dict]) -> dict[int, dict]:
    """Map horse_no → pick row (with 'rank')."""
    return {int(p["horse_no"]): p for p in (picks or [])
            if p.get("horse_no") is not None}


def _edge_by_horse(rows: list[dict]) -> dict[int, dict]:
    return {int(r["horse_no"]): r for r in rows
            if r.get("horse_no") is not None}


def build_multi_suggestion(
    *, date_compact: str, venue_code: str, race_no: int,
    picks: list[dict],
    mode: str = "contrarian_banker",
    stake_per_pair: float = 10.0,
    min_legs: int = 3,
    max_legs: int = 6,
    min_edge_pp: float = 5.0,
) -> dict:
    """Return a multi-bet suggestion for one race.

    Output schema:
      {
        "race_number": int,
        "mode": str,
        "banker": int | None,
        "legs": list[int],
        "rationale": str,
        "shape": str,            # e.g. "1x4"
        "n_pairs": int,
        "stake_total": float,    # n_pairs * 2 * stake_per_pair
        "skip_reason": str | None,  # set when no actionable suggestion
        "evidence": {
            "banker_rank": int | None,
            "banker_edge_pp": float | None,
            "leg_ranks": list[int],
            "leg_edges_pp": list[float],
            "consensus_count": int,   # legs in model top-3
        },
      }
    """
    by_rank = _picks_by_rank(picks)
    rows = _safe_value_lens_rows(date_compact, venue_code, race_no, picks)
    by_edge = _edge_by_horse(rows)

    # Sort horses by model rank
    ranked = sorted(by_rank.values(), key=lambda p: int(p.get("rank") or 99))
    if len(ranked) < min_legs + 1:
        return _skip(race_no, mode, stake_per_pair,
                     "not enough horses with model picks")

    top_horses = [int(p["horse_no"]) for p in ranked]

    # ── Banker selection ────────────────────────────────────────────
    if mode == "model_consensus":
        banker = top_horses[0]
        rationale_b = f"Model rank-1 (consensus play)"
    else:  # contrarian_banker (default)
        # Find a horse with rank 2-6 and positive edge ≥ +3pp;
        # prefer the one with HIGHEST edge in that band.
        candidates = []
        for h in top_horses:
            rk = int(by_rank[h].get("rank") or 99)
            if 2 <= rk <= 6:
                e = by_edge.get(h, {})
                edge_pp = float(e.get("edge", 0.0)) * 100
                if edge_pp >= 3.0:
                    candidates.append((edge_pp, rk, h))
        if not candidates:
            # Fall back: use rank-2 horse if edge ≥ 0
            for h in top_horses[1:6]:
                e = by_edge.get(h, {})
                edge_pp = float(e.get("edge", 0.0)) * 100
                if edge_pp >= 0:
                    candidates.append((edge_pp, int(by_rank[h].get("rank") or 99), h))
                    break
        if not candidates:
            # Final fallback: rank-2 unconditionally
            banker = top_horses[1] if len(top_horses) > 1 else top_horses[0]
            rationale_b = f"Model rank-{by_rank[banker].get('rank')} (no edge filter)"
        else:
            candidates.sort(reverse=True)
            edge_pp, rk, banker = candidates[0]
            rationale_b = (f"Contrarian banker — model rank-{rk}, "
                           f"edge +{edge_pp:.1f}pp")

    # ── Leg selection ───────────────────────────────────────────────
    legs: list[int] = []
    # 1. Always include model top-3 (excluding banker)
    for h in top_horses[:3]:
        if h != banker:
            legs.append(h)
    # 2. Add rank-4 if we have room
    if len(top_horses) > 3 and top_horses[3] != banker and len(legs) < max_legs:
        legs.append(top_horses[3])
    # 3. Add positive-edge overlays not yet included
    edge_overlays = sorted(
        ((float(by_edge.get(h, {}).get("edge", 0.0)) * 100, h)
         for h in top_horses if h not in legs and h != banker),
        reverse=True,
    )
    for edge_pp, h in edge_overlays:
        if len(legs) >= max_legs:
            break
        if edge_pp >= min_edge_pp:
            legs.append(h)

    # Trim down if over max
    legs = legs[:max_legs]
    if len(legs) < min_legs:
        return _skip(race_no, mode, stake_per_pair,
                     f"only {len(legs)} legs available (min {min_legs})")

    n_pairs = len(legs)
    stake_total = n_pairs * 2 * stake_per_pair

    # Build evidence
    consensus_count = sum(1 for h in legs
                          if int(by_rank.get(h, {}).get("rank", 99)) <= 3)
    leg_ranks = [int(by_rank.get(h, {}).get("rank", 99)) for h in legs]
    leg_edges = [round(float(by_edge.get(h, {}).get("edge", 0.0)) * 100, 1)
                 for h in legs]
    banker_rank = int(by_rank.get(banker, {}).get("rank", 99))
    banker_edge = round(float(by_edge.get(banker, {}).get("edge", 0.0)) * 100, 1)

    rationale = (
        f"{rationale_b}; legs = model top "
        f"{','.join('R'+str(r) for r in leg_ranks)} "
        f"({consensus_count} in mdl-top3)"
    )

    return {
        "race_number": int(race_no),
        "mode": mode,
        "banker": int(banker),
        "legs": [int(h) for h in legs],
        "rationale": rationale,
        "shape": f"1x{n_pairs}",
        "n_pairs": n_pairs,
        "stake_per_pair": float(stake_per_pair),
        "stake_total": float(stake_total),
        "skip_reason": None,
        "evidence": {
            "banker_rank": banker_rank,
            "banker_edge_pp": banker_edge,
            "leg_ranks": leg_ranks,
            "leg_edges_pp": leg_edges,
            "consensus_count": consensus_count,
        },
    }


def evaluate_user_choice(
    *, date_compact: str, venue_code: str, race_no: int,
    picks: list[dict], banker: int, legs: list[int],
    stake_per_pair: float = 10.0,
) -> dict:
    """Wrap a user's hand-picked banker+legs in the same evidence/cost
    schema as build_multi_suggestion. Used by the dashboard builder so
    the user sees the same model-rank/edge breakdown they'd see for an
    AI suggestion.
    """
    by_rank = _picks_by_rank(picks)
    rows = _safe_value_lens_rows(date_compact, venue_code, race_no, picks)
    by_edge = _edge_by_horse(rows)
    legs = [int(h) for h in legs if int(h) != int(banker)]
    n_pairs = len(legs)
    if n_pairs == 0:
        return _skip(race_no, "user", stake_per_pair, "no legs selected")

    banker_rank = int(by_rank.get(int(banker), {}).get("rank", 99))
    banker_edge = round(float(by_edge.get(int(banker), {}).get("edge", 0.0)) * 100, 1)
    leg_ranks = [int(by_rank.get(h, {}).get("rank", 99)) for h in legs]
    leg_edges = [round(float(by_edge.get(h, {}).get("edge", 0.0)) * 100, 1)
                 for h in legs]
    consensus_count = sum(1 for r in leg_ranks if r <= 3)
    stake_total = n_pairs * 2 * stake_per_pair

    # Classify the play vs April-2026 historical buckets
    if banker_rank <= 2:
        banker_class = "model favourite (Apr ROI: -59%)"
    elif banker_rank <= 4:
        banker_class = "model top-3/4 (Apr ROI: -4%)"
    elif banker_rank <= 6:
        banker_class = "mid-rank (Apr ROI: +63%)"
    else:
        banker_class = "contrarian (Apr ROI: +76%, 21% hit-rate)"

    rationale = (
        f"User pick — banker rank-{banker_rank} {banker_class}; "
        f"legs ranks={leg_ranks} ({consensus_count} in mdl-top3)"
    )

    return {
        "race_number": int(race_no),
        "mode": "user",
        "banker": int(banker),
        "legs": [int(h) for h in legs],
        "rationale": rationale,
        "shape": f"1x{n_pairs}",
        "n_pairs": n_pairs,
        "stake_per_pair": float(stake_per_pair),
        "stake_total": float(stake_total),
        "skip_reason": None,
        "evidence": {
            "banker_rank": banker_rank,
            "banker_edge_pp": banker_edge,
            "banker_class": banker_class,
            "leg_ranks": leg_ranks,
            "leg_edges_pp": leg_edges,
            "consensus_count": consensus_count,
        },
    }


def _skip(race_no: int, mode: str, stake_per_pair: float,
          reason: str) -> dict:
    return {
        "race_number": int(race_no), "mode": mode, "banker": None,
        "legs": [], "rationale": "", "shape": "", "n_pairs": 0,
        "stake_per_pair": float(stake_per_pair), "stake_total": 0.0,
        "skip_reason": reason, "evidence": {},
    }


def build_meeting_multi(
    *, date_compact: str, venue_code: str,
    picks_by_race: dict[int, list[dict]],
    mode: str = "contrarian_banker",
    stake_per_pair: float = 10.0,
    min_legs: int = 3, max_legs: int = 6,
    edge_filter_pp: Optional[float] = None,
) -> list[dict]:
    """Generate suggestions for every race in a meeting.

    `edge_filter_pp` — if set, ONLY include races where the banker's edge
    meets the threshold (used for "high-conviction filter" in dashboard).
    """
    out = []
    for rn in sorted(picks_by_race):
        sug = build_multi_suggestion(
            date_compact=date_compact, venue_code=venue_code, race_no=rn,
            picks=picks_by_race[rn], mode=mode,
            stake_per_pair=stake_per_pair,
            min_legs=min_legs, max_legs=max_legs,
        )
        if edge_filter_pp is not None and sug.get("banker") is not None:
            if (sug["evidence"].get("banker_edge_pp") or 0) < edge_filter_pp:
                sug["skip_reason"] = (
                    f"banker edge below filter (+{edge_filter_pp}pp)"
                )
        out.append(sug)
    return out


# ── Settlement helpers (for backtests + post-race auto-eval) ──────────
def settle_suggestion(sug: dict, date_compact: str) -> dict:
    """Given a suggestion + actual results, return PnL.

    Reads reports/dividends_YYYYMMDD.json and reports/results_YYYYMMDD.json.
    """
    if sug.get("skip_reason") or not sug.get("banker"):
        return {"stake": 0.0, "return": 0.0, "pnl": 0.0,
                "qin_hits": [], "qpl_hits": [], "settled": False}
    rn = int(sug["race_number"])
    banker = int(sug["banker"])
    legs = [int(x) for x in sug["legs"]]
    spp = float(sug.get("stake_per_pair", 10.0))

    div_path = REPORTS / f"dividends_{date_compact}.json"
    if not div_path.exists():
        return {"stake": sug["stake_total"], "return": 0.0,
                "pnl": -sug["stake_total"], "qin_hits": [], "qpl_hits": [],
                "settled": False}
    div_doc = json.loads(div_path.read_text(encoding="utf-8"))
    qin_divs: dict[frozenset, float] = {}
    qpl_divs: dict[frozenset, float] = {}
    for race in div_doc.get("races", []):
        if int(race.get("race_number") or 0) != rn:
            continue
        for div in race.get("dividends", []):
            try:
                combo = frozenset(int(x) for x in str(div["combination"]).split(",")
                                   if x.strip().isdigit())
            except Exception:
                continue
            if div.get("pool") == "QIN":
                qin_divs[combo] = float(div["dividend_per_10"])
            elif div.get("pool") == "QPL":
                qpl_divs[combo] = float(div["dividend_per_10"])

    qin_hits = []
    qpl_hits = []
    total_ret = 0.0
    for h in legs:
        pair = frozenset({banker, h})
        qd = qin_divs.get(pair, 0.0)
        pd = qpl_divs.get(pair, 0.0)
        if qd > 0:
            qin_hits.append((h, qd))
            total_ret += qd * (spp / 10.0)
        if pd > 0:
            qpl_hits.append((h, pd))
            total_ret += pd * (spp / 10.0)
    stake = sug["stake_total"]
    return {
        "stake": float(stake), "return": float(total_ret),
        "pnl": float(total_ret - stake),
        "qin_hits": qin_hits, "qpl_hits": qpl_hits,
        "settled": True,
    }
