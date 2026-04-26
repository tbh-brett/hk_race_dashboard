"""
user_bets.py — Personal bet tracker storage and settlement.

Maintains an append-only JSONL log of the user's actual wagers, with:
  - Submit / edit / delete (by bet_id)
  - Auto-settlement against reports/dividends_YYYYMMDD.json
  - Summary metrics (stake, return, PnL, ROI, hit%) with timeline filter

File: reports/user_bets_log.jsonl
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Optional

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
USER_BETS_PATH = REPORTS / "user_bets_log.jsonl"

# Supported bet types (subset of HKJC pools we can auto-settle)
BET_TYPES = [
    "WIN", "PLACE", "QIN", "QPL", "QIN_BANKER", "QPL_BANKER",
    "F4_BOX", "TRIO", "QTT_BOX",
]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def _load_all() -> list[dict]:
    if not USER_BETS_PATH.exists():
        return []
    out = []
    for ln in USER_BETS_PATH.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _save_all(rows: list[dict]) -> None:
    USER_BETS_PATH.parent.mkdir(exist_ok=True)
    with open(USER_BETS_PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def submit_bet(*, meeting_date: str, venue: str, race_number: int,
                bet_type: str, selections: list[int],
                banker: Optional[int], stake_hkd: float,
                notes: str = "") -> str:
    """Create a new bet record. Returns the bet_id.

    meeting_date: 'YYYYMMDD'
    selections: list of horse numbers (for QIN/QPL/TRIO/F4 this is the "legs")
    banker: banker horse number or None
    """
    bet_type = (bet_type or "").upper().strip()
    if bet_type not in BET_TYPES:
        raise ValueError(f"unsupported bet_type {bet_type!r}")
    bet_id = uuid.uuid4().hex[:10]
    row = {
        "bet_id": bet_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "meeting_date": meeting_date,
        "venue": venue,
        "race_number": int(race_number),
        "bet_type": bet_type,
        "selections": [int(x) for x in selections if str(x).strip()],
        "banker": int(banker) if banker not in (None, "", 0) else None,
        "stake_hkd": float(stake_hkd),
        "notes": (notes or "").strip(),
        "status": "open",
    }
    rows = _load_all()
    rows.append(row)
    _save_all(rows)
    return bet_id


def edit_bet(bet_id: str, **patch) -> bool:
    rows = _load_all()
    changed = False
    for r in rows:
        if r["bet_id"] == bet_id:
            for k, v in patch.items():
                if k in ("bet_id", "created_at"):
                    continue
                r[k] = v
            r["updated_at"] = datetime.now().isoformat(timespec="seconds")
            # Any edit invalidates a previous settlement
            if r.get("status") == "settled":
                r["status"] = "open"
                for key in ("return_hkd", "pnl_hkd", "hit", "settled_at"):
                    r.pop(key, None)
            changed = True
    if changed:
        _save_all(rows)
    return changed


def delete_bet(bet_id: str) -> bool:
    rows = _load_all()
    new_rows = [r for r in rows if r["bet_id"] != bet_id]
    if len(new_rows) == len(rows):
        return False
    _save_all(new_rows)
    return True


def load_bets(*, settle: bool = True) -> list[dict]:
    """Return all bets. If settle=True, compute payouts for any bet whose
    meeting has published dividends."""
    rows = _load_all()
    if not settle:
        return rows
    # Group by meeting_date to minimise disk reads
    by_date: dict[str, dict] = {}
    for r in rows:
        if r.get("status") == "settled":
            continue
        d = r["meeting_date"]
        if d not in by_date:
            by_date[d] = _load_meeting_pack(d)
        pack = by_date[d]
        if not pack["results"]:
            continue   # race not finished yet
        ev = _settle_one(r, pack)
        if ev is None:
            continue
        r.update(ev)
    _save_all(rows)
    return rows


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------
def _load_meeting_pack(date_compact: str) -> dict:
    res_path = REPORTS / f"results_{date_compact}.json"
    div_path = REPORTS / f"dividends_{date_compact}.json"
    results = None
    dividends = None
    if res_path.exists():
        try:
            results = json.loads(res_path.read_text(encoding="utf-8"))
        except Exception:
            results = None
    if div_path.exists():
        try:
            dividends = json.loads(div_path.read_text(encoding="utf-8"))
        except Exception:
            dividends = None
    return {"results": results, "dividends": dividends}


def _race_finishers(pack: dict, race_number: int) -> list[int]:
    """Return horse_no of top-3 finishers by place (1st, 2nd, 3rd)."""
    if not pack["results"]:
        return []
    for race in pack["results"].get("races", []):
        if int(race.get("race_number") or 0) != int(race_number):
            continue
        places = []
        for r in race.get("runners", []):
            try:
                p = int(r.get("place"))
                no = int(r.get("horse_no"))
            except (TypeError, ValueError):
                continue
            if 1 <= p <= 3:
                places.append((p, no))
        places.sort()
        return [no for _, no in places]
    return []


def _race_dividends(pack: dict, race_number: int) -> dict:
    """Return {pool: {frozenset(combo): div_per_10}}."""
    if not pack["dividends"]:
        return {}
    for race in pack["dividends"].get("races", []):
        if int(race.get("race_number") or 0) != int(race_number):
            continue
        by_pool: dict = {}
        for d in race.get("dividends", []):
            try:
                combo = frozenset(
                    int(x) for x in str(d["combination"]).split(",")
                    if x.strip().isdigit()
                )
                by_pool.setdefault(d["pool"], {})[combo] = \
                    float(d["dividend_per_10"])
            except (TypeError, ValueError, KeyError):
                continue
        return by_pool
    return {}


def _settle_one(bet: dict, pack: dict) -> Optional[dict]:
    """Compute return_hkd / pnl_hkd / hit for a single bet."""
    rn = bet["race_number"]
    finishers = _race_finishers(pack, rn)
    if not finishers:
        return None
    divs = _race_dividends(pack, rn)
    if not divs:
        return None

    top1 = {finishers[0]} if finishers else set()
    top2 = set(finishers[:2])
    top3 = set(finishers[:3])

    bet_type = bet["bet_type"]
    stake = float(bet["stake_hkd"])
    sels = list(bet["selections"])
    banker = bet.get("banker")

    def _lookup(pool: str, combo) -> float:
        tbl = divs.get(pool) or {}
        return tbl.get(frozenset(combo), 0.0)

    ret = 0.0
    hit = False

    if bet_type == "WIN":
        # one selection
        h = sels[0] if sels else banker
        if h in top1:
            # HKJC WIN div is per $10
            ret = stake / 10.0 * _lookup("WIN", [h])
            hit = ret > 0
    elif bet_type == "PLACE":
        h = sels[0] if sels else banker
        if h in top3:
            ret = stake / 10.0 * _lookup("PLACE", [h])
            hit = ret > 0
    elif bet_type == "QIN":
        # box across selections; C(n,2) combos
        from itertools import combinations
        pairs = list(combinations(sorted(set(sels)), 2))
        if pairs:
            per_combo = stake / len(pairs)
            for a, b in pairs:
                if {a, b} <= top2:
                    ret += per_combo / 10.0 * _lookup("QIN", [a, b])
                    hit = True
                    break
    elif bet_type == "QPL":
        from itertools import combinations
        pairs = list(combinations(sorted(set(sels)), 2))
        if pairs:
            per_combo = stake / len(pairs)
            for a, b in pairs:
                if {a, b} <= top3:
                    ret += per_combo / 10.0 * _lookup("QPL", [a, b])
                    hit = True
    elif bet_type == "QIN_BANKER":
        if banker and sels:
            per_combo = stake / len(sels)
            if banker in top2:
                for leg in sels:
                    if leg == banker:
                        continue
                    if leg in top2:
                        ret += per_combo / 10.0 \
                                 * _lookup("QIN", [banker, leg])
                        hit = True
                        break
    elif bet_type == "QPL_BANKER":
        if banker and sels:
            per_combo = stake / len(sels)
            if banker in top3:
                for leg in sels:
                    if leg == banker:
                        continue
                    if leg in top3:
                        ret += per_combo / 10.0 \
                                 * _lookup("QPL", [banker, leg])
                        hit = True
    elif bet_type == "TRIO":
        from itertools import combinations
        trios = list(combinations(sorted(set(sels)), 3))
        if trios:
            per_combo = stake / len(trios)
            for trio in trios:
                if set(trio) == top3:
                    ret += per_combo / 10.0 * _lookup("TRIO", list(trio))
                    hit = True
                    break
    elif bet_type in ("F4_BOX", "QTT_BOX"):
        # First-4 / Quartet box. F4 pays for the 4 horses in any order;
        # Quartet pays for the 4 horses in EXACT finishing order. HKJC
        # publishes one dividend per pool per race (the actual finishing
        # combination), so we just need to verify our box covers the
        # actual top-4 set. Stake math differs:
        #   F4_BOX  : C(n,4) box combos
        #   QTT_BOX : C(n,4) * 24 permutations  (1 of which is the winning order)
        from itertools import combinations
        if not pack["results"]:
            return None
        top4_list = []
        for race in pack["results"].get("races", []):
            if int(race.get("race_number") or 0) != int(rn):
                continue
            for r in race.get("runners", []):
                try:
                    p = int(r.get("place")); no = int(r.get("horse_no"))
                except (TypeError, ValueError):
                    continue
                if 1 <= p <= 4:
                    top4_list.append((p, no))
        top4_list.sort()
        top4 = {no for _, no in top4_list}
        combos = list(combinations(sorted(set(sels)), 4))
        if combos:
            if bet_type == "F4_BOX":
                per_combo = stake / len(combos)
                pool_code = "F4"
            else:  # QTT_BOX — 24 perms per 4-horse combo
                per_combo = stake / (len(combos) * 24)
                pool_code = "QTT"
            for c in combos:
                if set(c) == top4:
                    # For QTT only the 1-of-24 winning permutation pays;
                    # the dividend in the table is keyed on that perm.
                    ret += per_combo / 10.0 * _lookup(pool_code, list(c))
                    hit = True
                    break

    return {
        "status": "settled",
        "return_hkd": round(ret, 2),
        "pnl_hkd":    round(ret - stake, 2),
        "hit":        bool(hit),
        "settled_at": datetime.now().isoformat(timespec="seconds"),
        "finishers":  finishers,
    }


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------
def summarise(rows: list[dict]) -> dict:
    settled = [r for r in rows if r.get("status") == "settled"]
    open_ = [r for r in rows if r.get("status") != "settled"]
    stake = sum(float(r.get("stake_hkd", 0)) for r in settled)
    ret   = sum(float(r.get("return_hkd", 0)) for r in settled)
    hits  = sum(1 for r in settled if r.get("hit"))
    by_type: dict = {}
    for r in settled:
        bt = r["bet_type"]
        d = by_type.setdefault(bt, {"bets": 0, "hits": 0, "stake": 0.0, "ret": 0.0})
        d["bets"] += 1
        if r.get("hit"): d["hits"] += 1
        d["stake"] += float(r.get("stake_hkd", 0))
        d["ret"]   += float(r.get("return_hkd", 0))
    return {
        "total_bets": len(rows),
        "settled": len(settled),
        "open": len(open_),
        "stake": stake,
        "return": ret,
        "pnl": ret - stake,
        "roi": (ret - stake) / stake if stake else 0.0,
        "hit_rate": hits / len(settled) if settled else 0.0,
        "hits": hits,
        "by_type": by_type,
    }


def filter_by_window(rows: list[dict], window: str) -> list[dict]:
    """window ∈ {'7d','30d','ytd','all'} — filter by meeting_date."""
    if window == "all":
        return rows
    today = date.today()
    from datetime import timedelta
    if window == "7d":
        cutoff = today - timedelta(days=7)
    elif window == "30d":
        cutoff = today - timedelta(days=30)
    elif window == "ytd":
        cutoff = date(today.year, 1, 1)
    else:
        return rows
    out = []
    for r in rows:
        try:
            d = datetime.strptime(r["meeting_date"], "%Y%m%d").date()
        except (ValueError, KeyError):
            continue
        if d >= cutoff:
            out.append(r)
    return out
