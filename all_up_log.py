"""all_up_log.py — append-only log + auto-settler for All-Up tickets.

File: reports/all_up_log.jsonl. One row per ticket (Q or QPL). Settled
against reports/dividends_YYYYMMDD.json when available.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from all_up import settle_all_up_ticket

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
LOG_PATH = REPORTS / "all_up_log.jsonl"


def submit_ticket(ticket: dict, *, meeting_date: str, venue: str,
                   pair_mode: str, notes: str = "") -> str:
    REPORTS.mkdir(exist_ok=True)
    ticket_id = uuid.uuid4().hex[:10]
    row = {
        "ticket_id": ticket_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "meeting_date": meeting_date,
        "venue": venue,
        "pair_mode": pair_mode,
        "ticket": ticket,
        "notes": notes,
        "status": "open",
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return ticket_id


def load_tickets(*, settle: bool = True) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    out = []
    for ln in LOG_PATH.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if settle and row.get("status") != "settled":
            res = settle_all_up_ticket(row["ticket"], row["meeting_date"])
            if res.get("settled"):
                row["status"] = "settled"
                row["return_hkd"] = res["return"]
                row["pnl_hkd"] = res["pnl"]
                row["n_hits"] = res["n_hits"]
                row["leg_hits"] = res["hits"]
                row["settled_at"] = datetime.now().isoformat(timespec="seconds")
        out.append(row)
    return out


def delete_ticket(ticket_id: str) -> bool:
    if not LOG_PATH.exists():
        return False
    rows = []
    found = False
    for ln in LOG_PATH.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if r.get("ticket_id") == ticket_id:
            found = True
            continue
        rows.append(r)
    if found:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return found
