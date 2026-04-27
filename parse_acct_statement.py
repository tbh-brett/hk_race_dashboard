"""
parse_acct_statement.py — Parse HKJC bookie account-statement .txt files
and import the extracted bets into reports/user_bets_log.jsonl.

Expected statement format (one block per bet, separated by 40 asterisks):

    2209
    22/04/2026 18:40
    Happy Valley
    Wednesday
    Quinella - Quinella Place      ← bet-type line (ALWAYS QIN + QPL bundle)
    Race 1
    3 CONCORDE STAR +              ← selection lines
    4 NEBRASKAN +
    8 TEAM HAPPY
                                   ← blank line

    $10                            ← per-combo stake
                                   ← blank line(s)

    $60.00                         ← total debit
    $129.50                        ← optional credit/return

Banker variant (first horse is banker, then "Banker with" header, then legs):

    6 DOUBLE BINGO
    Banker with
    4 TELECOM POWER +
    7 PODIUM +
    8 VERBIER +
    9 DOUBLE SHOW

For "Quinella - Quinella Place" we emit TWO records per block (one QIN/QIN_BANKER
+ one QPL/QPL_BANKER), each carrying stake_hkd = total_debit / 2 and
"bookie_ref" for dedup.

Usage:
    python parse_acct_statement.py "acctstmt (22 April).txt"                     # preview
    python parse_acct_statement.py "acctstmt (22 April).txt" --import            # write to log
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Optional

import user_bets

BASE = Path(__file__).parent
USER_BETS_PATH = BASE / "reports" / "user_bets_log.jsonl"

SEP = "*" * 40

VENUE_MAP = {
    "happy valley": "HV",
    "sha tin": "ST",
}

# "3 CONCORDE STAR +" or "11 SMILING EMPEROR" (may or may not end with '+')
SELECTION_RE = re.compile(r"^(\d{1,2})\s+([^+]+?)\s*\+?\s*$")
MONEY_RE = re.compile(r"^\$([\d,]+(?:\.\d+)?)$")
DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})\s+(\d{2}:\d{2})$")
RACE_RE = re.compile(r"^Race\s+(\d+)$", re.IGNORECASE)


def _parse_money(s: str) -> Optional[float]:
    m = MONEY_RE.match(s.strip())
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def _split_blocks(text: str) -> list[list[str]]:
    """Split statement into blocks on separator lines; return list of stripped-line-lists."""
    lines = text.splitlines()
    blocks: list[list[str]] = []
    cur: list[str] = []
    for ln in lines:
        if ln.strip() == SEP:
            if cur:
                blocks.append(cur)
            cur = []
        else:
            cur.append(ln)
    if cur:
        blocks.append(cur)
    return blocks


def _classify_block(block: list[str]) -> str:
    """'bet' | 'header' | 'balance' | 'footer' | 'unknown'."""
    joined = "\n".join(block)
    if "Account Records" in joined:
        return "header"
    if "Account Balance as of" in joined:
        return "balance"
    if "- End -" in joined:
        return "footer"
    if any(DATE_RE.match(ln.strip()) for ln in block):
        return "bet"
    return "unknown"


def _parse_bet_block(block: list[str]) -> Optional[dict]:
    """Parse a single bet block → dict or None if malformed."""
    lines = [ln.rstrip() for ln in block]
    # Strip fully-blank leading/trailing lines
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 6:
        return None

    # Fixed positional: 0=ref, 1=date/time, 2=venue, 3=weekday, 4=bet-type, 5=race
    ref_no = lines[0].strip()
    dt_line = lines[1].strip()
    venue_line = lines[2].strip()
    bet_type_line = lines[4].strip()
    race_line = lines[5].strip()

    dt_m = DATE_RE.match(dt_line)
    if not dt_m:
        return None
    dd, mm, yyyy, hhmm = dt_m.groups()
    meeting_date = f"{yyyy}{mm}{dd}"
    placed_at = f"{yyyy}-{mm}-{dd}T{hhmm}:00"

    venue = VENUE_MAP.get(venue_line.lower())
    if not venue:
        return None

    race_m = RACE_RE.match(race_line)
    if not race_m:
        return None
    race_number = int(race_m.group(1))

    # Collect selections + banker from line 6 onwards until we hit the stake line ($NN).
    banker: Optional[int] = None
    legs: list[int] = []
    i = 6
    n = len(lines)
    # Phase A: lines before "Banker with" (if present) OR before blank/$
    pre_banker: list[tuple[int, str]] = []
    post_banker: list[tuple[int, str]] = []
    banker_seen = False
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if not stripped:
            i += 1
            continue
        if stripped.lower().startswith("banker with"):
            banker_seen = True
            i += 1
            continue
        if MONEY_RE.match(stripped):
            break
        m = SELECTION_RE.match(stripped)
        if m:
            horse_no = int(m.group(1))
            horse_name = m.group(2).strip()
            if banker_seen:
                post_banker.append((horse_no, horse_name))
            else:
                pre_banker.append((horse_no, horse_name))
        i += 1

    if banker_seen:
        # Banker is the last horse before "Banker with"; legs are post-banker list.
        if not pre_banker or not post_banker:
            return None
        banker = pre_banker[-1][0]
        legs = [h for h, _ in post_banker]
    else:
        legs = [h for h, _ in pre_banker]

    if not legs:
        return None

    # Remaining lines: per-combo stake ($10), blanks, total debit ($XX), optional credit ($YY).
    stakes: list[float] = []
    while i < n:
        stripped = lines[i].strip()
        if stripped:
            v = _parse_money(stripped)
            if v is not None:
                stakes.append(v)
        i += 1

    if len(stakes) < 2:
        return None
    per_combo_stake = stakes[0]
    total_debit = stakes[1]
    total_credit = stakes[2] if len(stakes) >= 3 else 0.0

    return {
        "bookie_ref": ref_no,
        "placed_at": placed_at,
        "meeting_date": meeting_date,
        "venue": venue,
        "race_number": race_number,
        "bet_type_text": bet_type_line,
        "banker": banker,
        "selections": legs,
        "per_combo_stake": per_combo_stake,
        "total_debit": total_debit,
        "total_credit": total_credit,
    }


def _expand_to_user_bet_records(parsed: dict) -> list[dict]:
    """Convert one parsed block into one or more user_bets records.

    'Quinella - Quinella Place' bundle → two records (QIN + QPL) each at 1/2 debit.
    """
    bt_text = parsed["bet_type_text"].lower()
    banker = parsed.get("banker")
    records: list[dict] = []

    # Determine emitted bet-type codes
    if "quinella - quinella place" in bt_text or "quinella-quinella place" in bt_text:
        codes = [
            "QIN_BANKER" if banker else "QIN",
            "QPL_BANKER" if banker else "QPL",
        ]
        per_code_stake = round(parsed["total_debit"] / 2.0, 2)
    elif "quinella place" in bt_text:
        codes = ["QPL_BANKER" if banker else "QPL"]
        per_code_stake = parsed["total_debit"]
    elif "quinella" in bt_text:
        codes = ["QIN_BANKER" if banker else "QIN"]
        per_code_stake = parsed["total_debit"]
    elif "trio" in bt_text:
        codes = ["TRIO"]
        per_code_stake = parsed["total_debit"]
    elif "first 4" in bt_text or "first four" in bt_text:
        codes = ["F4_BOX"]
        per_code_stake = parsed["total_debit"]
    elif "quartet" in bt_text:
        # Quartet — top-4 in EXACT order. Treat box-style (selections form
        # the 4-horse set; per-permutation stake = total / (C(n,4)*24)).
        codes = ["QTT_BOX"]
        per_code_stake = parsed["total_debit"]
    elif "win" in bt_text and "place" not in bt_text:
        codes = ["WIN"]
        per_code_stake = parsed["total_debit"]
    elif "place" in bt_text:
        codes = ["PLACE"]
        per_code_stake = parsed["total_debit"]
    else:
        # Unknown bet type — skip (can't auto-settle)
        return []

    for code in codes:
        records.append({
            "meeting_date": parsed["meeting_date"],
            "venue": parsed["venue"],
            "race_number": parsed["race_number"],
            "bet_type": code,
            "selections": parsed["selections"],
            "banker": parsed.get("banker"),
            "stake_hkd": per_code_stake,
            "notes": f"Imported from bookie statement (ref {parsed['bookie_ref']}). "
                     f"Per-combo ${parsed['per_combo_stake']}, "
                     f"debit ${parsed['total_debit']}, "
                     f"credit ${parsed['total_credit']}.",
            "_bookie_ref": parsed["bookie_ref"],
            "_bookie_bet_type_text": parsed["bet_type_text"],
            "_bookie_total_debit": parsed["total_debit"],
            "_bookie_total_credit": parsed["total_credit"],
            "_bookie_placed_at": parsed["placed_at"],
        })
    return records


def parse_statement(path: Path) -> list[dict]:
    """Return list of parsed bet blocks (raw, pre-expansion)."""
    text = path.read_text(encoding="utf-8")
    out = []
    for block in _split_blocks(text):
        if _classify_block(block) != "bet":
            continue
        parsed = _parse_bet_block(block)
        if parsed:
            out.append(parsed)
    return out


def _existing_bookie_refs() -> set[str]:
    refs: set[str] = set()
    if not USER_BETS_PATH.exists():
        return refs
    for ln in USER_BETS_PATH.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        # The submit_bet() call strips underscored keys, so check notes instead.
        if "_bookie_ref" in r:
            refs.add(str(r["_bookie_ref"]))
        notes = r.get("notes", "")
        m = re.search(r"ref (\d+)", notes)
        if m:
            refs.add(m.group(1))
    return refs


def import_statement(path: Path) -> dict:
    """Parse + insert new bets (skipping already-imported by bookie_ref).

    Returns summary dict {inserted, skipped, records_by_ref}.
    """
    parsed_blocks = parse_statement(path)
    existing = _existing_bookie_refs()

    inserted = 0
    skipped = 0
    inserted_details: list[dict] = []
    skipped_refs: list[str] = []

    for parsed in parsed_blocks:
        ref = parsed["bookie_ref"]
        if ref in existing:
            skipped += 1
            skipped_refs.append(ref)
            continue
        records = _expand_to_user_bet_records(parsed)
        if not records:
            skipped += 1
            skipped_refs.append(
                f"{ref} (unsupported bet type: {parsed.get('bet_type_text', '?')!r})"
            )
            continue
        for rec in records:
            user_bets.submit_bet(
                meeting_date=rec["meeting_date"],
                venue=rec["venue"],
                race_number=rec["race_number"],
                bet_type=rec["bet_type"],
                selections=rec["selections"],
                banker=rec["banker"],
                stake_hkd=rec["stake_hkd"],
                notes=rec["notes"],
            )
            inserted += 1
            inserted_details.append({
                "bookie_ref": ref,
                "bet_type": rec["bet_type"],
                "meeting_date": rec["meeting_date"],
                "venue": rec["venue"],
                "race": rec["race_number"],
                "selections": rec["selections"],
                "banker": rec["banker"],
                "stake_hkd": rec["stake_hkd"],
            })

    return {
        "inserted": inserted,
        "skipped": skipped,
        "inserted_details": inserted_details,
        "skipped_refs": skipped_refs,
        "total_blocks": len(parsed_blocks),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="Path to acctstmt*.txt file")
    ap.add_argument("--import", dest="do_import", action="store_true",
                    help="Actually write to reports/user_bets_log.jsonl (default: preview)")
    args = ap.parse_args()

    p = Path(args.path)
    if not p.exists():
        print(f"File not found: {p}")
        return 1

    if args.do_import:
        summary = import_statement(p)
        print(f"Imported {summary['inserted']} record(s), skipped {summary['skipped']}. "
              f"(from {summary['total_blocks']} blocks)")
        for d in summary["inserted_details"]:
            bk = f" banker={d['banker']}" if d['banker'] else ""
            print(f"  {d['bookie_ref']:>6}  {d['meeting_date']} R{d['race']}  "
                  f"{d['bet_type']:>11} {d['selections']}{bk}  "
                  f"${d['stake_hkd']:.2f}")
        if summary["skipped_refs"]:
            print(f"  Skipped: {', '.join(summary['skipped_refs'])}")
    else:
        parsed = parse_statement(p)
        print(f"Parsed {len(parsed)} bet block(s) (preview only, --import to commit):\n")
        for pb in parsed:
            bk = f" banker={pb['banker']}" if pb['banker'] else ""
            print(f"  {pb['bookie_ref']:>6}  {pb['meeting_date']} {pb['venue']} "
                  f"R{pb['race_number']}  {pb['bet_type_text']}{bk}  "
                  f"legs={pb['selections']}  combo=${pb['per_combo_stake']} "
                  f"debit=${pb['total_debit']} credit=${pb['total_credit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
