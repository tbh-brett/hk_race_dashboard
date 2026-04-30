"""
market_loader.py
================
Cross-sectional **Value Lens** loader for the dashboard.

This is *not* the same as the existing Market Pulse module:

  * Market Pulse  = temporal Δ% (steamers / drifters across snapshots).
  * Value Lens    = static probability comparison RIGHT NOW
                    (p_model vs p_market, edge, Kelly, quadrant).

Both can — and should — coexist in the cockpit. Pulse tells you which
direction money is moving; Lens tells you whether the current price
itself is a value vs the model.

Public API
----------
    load_latest_snapshot(date_compact, venue, race_no)
        Returns dict {horse_no: {"horse": str, "win": float, "place": float},
                      "_scraped_at": str, "_source": "live"|"sp"} | None

    compute_edge_table(date_compact, venue, race_no, picks)
        Returns list of dicts ready to render:
            horse_no, horse, win_odds, p_market, p_model,
            edge, kelly, quadrant ('A'|'B'|'C'|'D')

`picks` is the v4.4 picks list from race_day_report — items are dicts
with `horse_no` + `win_prob` (percentage scale, 0-100).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from market_belief import implied_basic

BASE = Path(__file__).resolve().parent
LIVE_ODDS_DIR = BASE / "cache" / "live_odds"
REPORTS_DIR = BASE / "reports"

# Quadrant geometry
EDGE_HI = 0.05         # ≥ +5pp model over market = model "loves"
EDGE_LO = -0.05        # ≤ -5pp model under market = market "loves"
KELLY_CAP = 0.05


# ---------------------------------------------------------------------------
# Snapshot loading
# ---------------------------------------------------------------------------
def _list_race_snapshots(date_compact: str, venue: str, race_no: int
                         ) -> list[Path]:
    d = LIVE_ODDS_DIR / date_compact
    if not d.exists():
        return []
    pat = f"{venue.upper()}_R{int(race_no):02d}_*.json"
    return sorted(d.glob(pat))


def _parse_snapshot(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    odds_list = data.get("odds") or []
    out = {}
    for r in odds_list:
        try:
            hn = int(str(r.get("no", "")).strip())
        except (TypeError, ValueError):
            continue
        try:
            win = float(r.get("win"))
        except (TypeError, ValueError):
            continue
        if win <= 1.0:
            continue
        try:
            plc = float(r.get("place"))
        except (TypeError, ValueError):
            plc = None
        out[hn] = {"horse": r.get("horse", ""),
                   "win": win, "place": plc}
    if not out:
        return None
    out["_scraped_at"] = data.get("scraped_at") or path.stem.split("_")[-1]
    out["_source"] = "live"
    out["_path"] = str(path.name)
    return out


def _load_sp_fallback(date_compact: str, race_no: int) -> Optional[dict]:
    """Final SP from results_YYYYMMDD.json — used when no live snapshot."""
    p = REPORTS_DIR / f"results_{date_compact}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    races = data.get("races") or []
    for r in races:
        rn = r.get("race_number", r.get("race_no", 0))
        try:
            if int(rn) != int(race_no):
                continue
        except (TypeError, ValueError):
            continue
        out = {}
        for run in (r.get("runners") or []):
            try:
                hn = int(str(run.get("horse_no", "")).strip())
                win = float(str(run.get("win_odds", "")).strip())
            except (TypeError, ValueError):
                continue
            if win <= 1.0:
                continue
            out[hn] = {"horse": run.get("horse_name", ""),
                       "win": win, "place": None}
        if not out:
            return None
        out["_scraped_at"] = data.get("date") or date_compact
        out["_source"] = "sp"
        out["_path"] = p.name
        return out
    return None


def load_latest_snapshot(date_compact: str, venue: str, race_no: int
                         ) -> Optional[dict]:
    """Most recent live-odds snapshot, or SP fallback, or None."""
    snaps = _list_race_snapshots(date_compact, venue, race_no)
    if snaps:
        latest = _parse_snapshot(snaps[-1])
        if latest:
            return latest
    return _load_sp_fallback(date_compact, int(race_no))


# ---------------------------------------------------------------------------
# Edge table
# ---------------------------------------------------------------------------
def _normalise_model_pct(picks: list[dict]) -> dict[int, float]:
    """Picks store win_prob as % (0-100). Convert to fractional [0,1].

    Auto-detect scale via sum: if total > 5 we assume %.
    """
    if not picks:
        return {}
    scored = []
    for p in picks:
        try:
            hn = int(str(p.get("horse_no", "")).strip())
        except (TypeError, ValueError):
            continue
        wp = p.get("win_prob")
        if wp is None:
            continue
        try:
            wp = float(wp)
        except (TypeError, ValueError):
            continue
        scored.append((hn, wp))
    if not scored:
        return {}
    total = sum(v for _, v in scored)
    scale = 0.01 if total > 5.0 else 1.0
    out = {hn: v * scale for hn, v in scored}
    # Renormalise to sum to 1 (model probs sometimes don't, esp after
    # rounding to integer percentages).
    s = sum(out.values())
    if s > 0:
        out = {k: v / s for k, v in out.items()}
    return out


def _quadrant(p_model: float, p_market: float) -> str:
    """A=consensus love, B=market only, C=model only, D=both ignore."""
    edge = p_model - p_market
    if p_model >= 0.15 and p_market >= 0.15:
        return "A"           # both rate it ≥ 15% — consensus
    if edge >= EDGE_HI and p_market < 0.15:
        return "C"           # model loves, market doesn't
    if edge <= EDGE_LO and p_model < 0.15:
        return "B"           # market loves, model doesn't
    if p_model < 0.10 and p_market < 0.10:
        return "D"           # both think no chance
    if edge >= EDGE_HI:
        return "C"
    if edge <= EDGE_LO:
        return "B"
    return "A"


def _kelly(edge: float, win_odds: float) -> float:
    b = win_odds - 1.0
    if b <= 0:
        return 0.0
    return max(0.0, min(KELLY_CAP, edge / b))


def compute_edge_table(date_compact: str, venue: str, race_no: int,
                       picks: list[dict]) -> dict:
    """Return {'meta':{...}, 'rows':[...]} or {'meta':{},'rows':[]}.

    `meta` contains: source ('live'|'sp'|'none'), scraped_at, n_runners.
    `rows` sorted by edge descending. Each row:
        horse_no, horse, win_odds, p_market, p_model,
        edge, kelly, quadrant.
    """
    snap = load_latest_snapshot(date_compact, venue, int(race_no))
    if not snap:
        return {"meta": {"source": "none"}, "rows": []}

    odds_dict = {hn: v["win"] for hn, v in snap.items()
                 if isinstance(hn, int)}
    if not odds_dict:
        return {"meta": {"source": "none"}, "rows": []}

    p_market = implied_basic(odds_dict)
    p_model = _normalise_model_pct(picks or [])

    rows = []
    for hn, w in odds_dict.items():
        pm = p_market.get(hn, 0.0)
        pmo = p_model.get(hn, 0.0)
        edge = pmo - pm
        rows.append({
            "horse_no": hn,
            "horse": snap[hn].get("horse", ""),
            "win_odds": w,
            "p_market": pm,
            "p_model": pmo,
            "edge": edge,
            "kelly": _kelly(edge, w),
            "quadrant": _quadrant(pmo, pm),
        })
    rows.sort(key=lambda r: -r["edge"])

    meta = {
        "source": snap.get("_source", "live"),
        "scraped_at": snap.get("_scraped_at", ""),
        "n_runners": len(rows),
        "path": snap.get("_path", ""),
    }
    return {"meta": meta, "rows": rows}


__all__ = ["load_latest_snapshot", "compute_edge_table"]
