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

# Bet-type keywords we recognise (substring, lowercase). Order matters for the
# longest-match-first detection in `_detect_bet_type_line`.
BET_TYPE_KEYWORDS = (
    "quinella - quinella place",
    "quinella-quinella place",
    "quinella place",
    "quinella",
    "first 4",
    "first four",
    "quartet",
    "qtt",
    "trio",
    "win-place",
    "win - place",
    "place",
    "win",
    "tierce",
    "trifecta",
    "double trio",
    "six up",
)
SUBTYPE_KEYWORDS = ("multi-banker", "multi banker")
DEBUG_PARSE = False  # toggled by `--debug` on the CLI; emits skip diagnostics


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


def _detect_bet_type_line(lines: list[str]) -> tuple[Optional[int], str, str]:
    """Locate the bet-type line by scanning for a known keyword.

    Returns ``(index, bet_type_line, sub_type_line)`` or ``(None, "", "")``.
    The sub-type line is appended (e.g. "Multi-Banker") if it appears
    immediately below the bet-type line.
    """
    for idx, raw in enumerate(lines):
        s = raw.strip().lower()
        if not s:
            continue
        if any(kw in s for kw in BET_TYPE_KEYWORDS):
            bet_type = lines[idx].strip()
            sub_type = ""
            # Look at the very next non-blank line for a Multi-Banker tag
            j = idx + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                nxt = lines[j].strip().lower()
                if any(sub in nxt for sub in SUBTYPE_KEYWORDS):
                    sub_type = lines[j].strip()
            return idx, bet_type, sub_type
    return None, "", ""


def _parse_bet_block(block: list[str]) -> Optional[dict]:
    """Parse a single bet block → dict or None if malformed.

    Content-scanning parser — does not assume fixed positional indices.
    Handles:
      * Standard            : ref / dt / venue / weekday / bet-type / Race N / selections
      * Banker (single)     : adds one ``Banker with`` section
      * Multi-Banker quartet: ``Multi-Banker`` line below bet-type, then N
        ``Banker with`` sub-sections (one leg-list per finishing position)
    """
    lines = [ln.rstrip() for ln in block]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 5:
        if DEBUG_PARSE:
            print(f"[skip] block too short ({len(lines)} lines)")
        return None

    # ---- Header (positional, robust) ----
    ref_no = lines[0].strip()
    if not ref_no.isdigit():
        # Defensive: in some statement variants the very first non-blank line
        # may be something other than a pure digit ref. Scan ahead.
        for ln in lines[:4]:
            if ln.strip().isdigit():
                ref_no = ln.strip()
                break

    # date/time line — find it by regex anywhere in first 6 lines
    dt_idx = next((i for i, ln in enumerate(lines[:6])
                    if DATE_RE.match(ln.strip())), None)
    if dt_idx is None:
        if DEBUG_PARSE:
            print(f"[skip] no date/time line; ref={ref_no!r} head="
                    f"{[l.strip() for l in lines[:5]]}")
        return None
    dt_m = DATE_RE.match(lines[dt_idx].strip())
    dd, mm, yyyy, hhmm = dt_m.groups()
    meeting_date = f"{yyyy}{mm}{dd}"
    placed_at = f"{yyyy}-{mm}-{dd}T{hhmm}:00"

    # venue — first matching line after dt_idx
    venue = None
    for ln in lines[dt_idx + 1: dt_idx + 4]:
        v = VENUE_MAP.get(ln.strip().lower())
        if v:
            venue = v
            break
    if not venue:
        if DEBUG_PARSE:
            print(f"[skip] unknown venue; ref={ref_no!r} after-dt="
                    f"{[l.strip() for l in lines[dt_idx+1:dt_idx+4]]}")
        return None

    # bet-type by content scan
    bt_idx, bet_type_line, sub_type_line = _detect_bet_type_line(lines)
    if bt_idx is None:
        if DEBUG_PARSE:
            print(f"[skip] bet-type not detected; ref={ref_no!r} head="
                    f"{[l.strip() for l in lines[:8]]}")
        return None
    is_multi_banker = bool(sub_type_line)
    if is_multi_banker:
        bet_type_line = f"{bet_type_line} {sub_type_line}".strip()

    # Race N — first RACE_RE match AFTER bet-type / sub-type
    search_start = bt_idx + (2 if is_multi_banker else 1)
    race_idx = None
    for i in range(search_start, len(lines)):
        if RACE_RE.match(lines[i].strip()):
            race_idx = i
            break
    if race_idx is None:
        if DEBUG_PARSE:
            print(f"[skip] no 'Race N' line; ref={ref_no!r} bt_idx={bt_idx}")
        return None
    race_number = int(RACE_RE.match(lines[race_idx].strip()).group(1))
    sel_start = race_idx + 1

    # ---- Selections (with optional banker / multi-banker structure) ----
    multi_legs: list[list[int]] = []
    banker: Optional[int] = None
    legs: list[int] = []
    i = sel_start
    n = len(lines)

    if is_multi_banker:
        cur_leg: list[int] = []
        while i < n:
            stripped = lines[i].strip()
            if not stripped:
                i += 1
                continue
            if MONEY_RE.match(stripped):
                break
            if stripped.lower().startswith("banker with"):
                if cur_leg:
                    multi_legs.append(cur_leg)
                cur_leg = []
                i += 1
                continue
            m = SELECTION_RE.match(stripped)
            if m:
                cur_leg.append(int(m.group(1)))
            i += 1
        if cur_leg:
            multi_legs.append(cur_leg)

        seen: set[int] = set()
        for lg in multi_legs:
            for h in lg:
                if h not in seen:
                    legs.append(h)
                    seen.add(h)
        if not legs or not multi_legs:
            if DEBUG_PARSE:
                print(f"[skip] multi-banker block had no legs; ref={ref_no!r}")
            return None
    else:
        pre_banker: list[tuple[int, str]] = []
        post_banker: list[tuple[int, str]] = []
        banker_seen = False
        while i < n:
            stripped = lines[i].strip()
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
            if not pre_banker or not post_banker:
                if DEBUG_PARSE:
                    print(f"[skip] banker block missing pre/post lists; "
                            f"ref={ref_no!r}")
                return None
            banker = pre_banker[-1][0]
            legs = [h for h, _ in post_banker]
        else:
            legs = [h for h, _ in pre_banker]

        if not legs:
            if DEBUG_PARSE:
                print(f"[skip] no selections found; ref={ref_no!r}")
            return None

    # ---- Stakes ----
    stakes: list[float] = []
    while i < n:
        stripped = lines[i].strip()
        if stripped:
            v = _parse_money(stripped)
            if v is not None:
                stakes.append(v)
        i += 1

    if len(stakes) < 2:
        if DEBUG_PARSE:
            print(f"[skip] not enough $ amounts ({len(stakes)}); "
                    f"ref={ref_no!r}")
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
        "multi_legs": multi_legs if is_multi_banker else None,
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
    elif "tierce" in bt_text or "trifecta" in bt_text:
        # HKJC "Tierce" = trifecta: first 3 in exact order.
        # Bookie statement typically presents a box listing of 3+ horses.
        # We emit a single TCE_BOX row; the settler computes stake/n_perms.
        codes = ["TCE_BOX"]
        per_code_stake = parsed["total_debit"]
    elif "first 4" in bt_text or "first four" in bt_text:
        codes = ["F4_BOX"]
        per_code_stake = parsed["total_debit"]
    elif "quartet" in bt_text or "qtt" in bt_text:
        if parsed.get("multi_legs"):
            # Quartet Multi-Banker — 4 leg-lists, one per finish position.
            codes = ["QTT_MB"]
        else:
            # Standard quartet box.
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
            "legs": parsed.get("multi_legs") if code == "QTT_MB" else None,
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


def parse_statement(path: Path, *, debug: bool = False) -> list[dict]:
    """Return list of parsed bet blocks (raw, pre-expansion).

    Set ``debug=True`` to print one diagnostic line per skipped block — useful
    when investigating why a bet failed to import.
    """
    global DEBUG_PARSE
    DEBUG_PARSE = debug
    text = path.read_text(encoding="utf-8")
    out = []
    skipped = 0
    for block in _split_blocks(text):
        if _classify_block(block) != "bet":
            continue
        parsed = _parse_bet_block(block)
        if parsed:
            out.append(parsed)
        else:
            skipped += 1
    if debug and skipped:
        print(f"[parse_statement] {skipped} bet block(s) failed to parse "
                f"(see diagnostics above).")
    DEBUG_PARSE = False
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


def _purge_bookie_refs(refs_to_remove: set[str]) -> int:
    """Remove every record whose notes/_bookie_ref matches one of *refs_to_remove*.

    Returns the number of rows deleted. Used by the force-reimport path so
    that callers don't have to manually delete via the dashboard UI first.
    """
    if not USER_BETS_PATH.exists() or not refs_to_remove:
        return 0
    refs_to_remove = {str(r) for r in refs_to_remove}
    kept: list[str] = []
    removed = 0
    for ln in USER_BETS_PATH.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if not s:
            continue
        try:
            r = json.loads(s)
        except json.JSONDecodeError:
            kept.append(s)
            continue
        ref = None
        if "_bookie_ref" in r:
            ref = str(r["_bookie_ref"])
        else:
            m = re.search(r"ref (\d+)", r.get("notes", "") or "")
            if m:
                ref = m.group(1)
        if ref and ref in refs_to_remove:
            removed += 1
            continue
        kept.append(s)
    USER_BETS_PATH.write_text(
        ("\n".join(kept) + "\n") if kept else "",
        encoding="utf-8",
    )
    return removed


def import_statement(path: Path, *, debug: bool = False,
                     force: bool = False) -> dict:
    """Parse + insert new bets.

    Default behaviour skips bets already present (by bookie_ref). When
    ``force=True`` every record matching a ref in the parsed file is FIRST
    deleted from the log, then re-inserted — useful when the user has
    edited/deleted bets manually and wants the importer to be the source
    of truth.

    Returns summary dict {inserted, skipped, purged, records_by_ref}.
    """
    parsed_blocks = parse_statement(path, debug=debug)

    purged = 0
    if force:
        refs_in_file = {p["bookie_ref"] for p in parsed_blocks}
        purged = _purge_bookie_refs(refs_in_file)

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
                legs=rec.get("legs"),
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

    # Re-load with settlement so any newly-imported records are scored
    # against existing dividends (no-op for races without published divs).
    try:
        user_bets.load_bets(settle=True)
    except Exception:
        pass

    return {
        "inserted": inserted,
        "skipped": skipped,
        "purged": purged,
        "inserted_details": inserted_details,
        "skipped_refs": skipped_refs,
        "total_blocks": len(parsed_blocks),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="Path to acctstmt*.txt file")
    ap.add_argument("--import", dest="do_import", action="store_true",
                    help="Actually write to reports/user_bets_log.jsonl (default: preview)")
    ap.add_argument("--debug", action="store_true",
                    help="Print diagnostics for each block that fails to parse.")
    ap.add_argument("--force", action="store_true",
                    help="Delete-and-replace any record whose bookie ref is in the file.")
    args = ap.parse_args()

    p = Path(args.path)
    if not p.exists():
        print(f"File not found: {p}")
        return 1

    if args.do_import:
        summary = import_statement(p, debug=args.debug, force=args.force)
        purged = summary.get("purged", 0)
        purge_msg = f", purged {purged} pre-existing" if purged else ""
        print(f"Imported {summary['inserted']} record(s), "
              f"skipped {summary['skipped']}{purge_msg}. "
              f"(from {summary['total_blocks']} blocks)")
        for d in summary["inserted_details"]:
            bk = f" banker={d['banker']}" if d['banker'] else ""
            print(f"  {d['bookie_ref']:>6}  {d['meeting_date']} R{d['race']}  "
                  f"{d['bet_type']:>11} {d['selections']}{bk}  "
                  f"${d['stake_hkd']:.2f}")
        if summary["skipped_refs"]:
            print(f"  Skipped: {', '.join(summary['skipped_refs'])}")
    else:
        parsed = parse_statement(p, debug=args.debug)
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
