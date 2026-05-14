"""
trial_blackbook.py
==================

Helpers for surfacing trialed horses worth blackbook-watching.

Given a scraped trial JSON (``reports/trials_{YYYYMMDD}.json`` produced by
``scrape_hkjc_trials.py``), score each runner on a 0–10 scale based on:

  * Finishing position in the trial
  * Late closing kick (running_positions tail-drop)
  * Eye-catcher language in the steward's comment
  * Trouble-in-running phrases (suggests a better-trip upgrade is on)

Suggestions are surfaced in the dashboard with a one-click "add to
blackbook with tag 'trial'" action. We DO NOT auto-add anything — the
user always confirms.

This module is intentionally side-effect free (no I/O on import) so the
dashboard can import it cheaply.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

# ── Keyword lexicons (case-insensitive substring match) ────────────────
EYE_CATCHER_KW = (
    "eye-catcher", "eye catch", "ran on", "finished strongly",
    "big move", "easily", "effortless", "unextended", "without effort",
    "untested", "merit win", "good late",
    "hard held", "in hand",                # rider had reserves
    "ran on well", "strong finish",
)

TROUBLE_KW = (
    "traffic", "held up", "blocked", "checked", "tightened",
    "had no room", "no clear run", "shut off", "interfered",
)

NEG_KW = (
    "weakly", "stopped", "no impression", "tailed off", "no response",
    "found nothing", "disappointing", "below expectation",
)


# ── Public API ─────────────────────────────────────────────────────────
def score_runner(runner: dict) -> dict:
    """Return ``{score, reasons}`` for one trial-runner dict."""
    score = 0.0
    reasons: list[str] = []
    pos = runner.get("running_positions") or []
    comment = (runner.get("comment") or "").lower()
    lbw_raw = (runner.get("lbw") or "").strip()

    # Final position (last element of running_positions). Some trials
    # show running_positions only — `result` is the official finish but
    # trials don't have one, so the last element is canonical.
    final_pos = None
    if pos:
        try:
            final_pos = int(pos[-1])
        except (TypeError, ValueError):
            final_pos = None

    if final_pos == 1:
        score += 3.0
        reasons.append("Won trial")
    elif final_pos == 2:
        score += 1.5
        reasons.append("2nd in trial")
    elif final_pos == 3:
        score += 1.0
        reasons.append("3rd in trial")

    # Closing-kick = early position - final position. A horse that was
    # 6th at the turn but 2nd at the line dropped 4 positions.
    if final_pos is not None and len(pos) >= 2:
        try:
            early = int(pos[0])
            kick = early - final_pos
            if kick >= 4:
                score += 2.0
                reasons.append(f"Strong late kick (+{kick} positions)")
            elif kick >= 2:
                score += 1.0
                reasons.append(f"Closed late (+{kick} positions)")
        except (TypeError, ValueError):
            pass

    # Beaten margin — close trial behind a winner is fine.
    # lbw "1/2L", "1L", "SH" all small; "5L+" weakens the signal.
    m = re.match(r"^([\d\.]+)L?$", lbw_raw)
    if m:
        try:
            bl = float(m.group(1))
            if bl >= 5:
                score -= 1.0
                reasons.append(f"Beaten {bl}L")
        except ValueError:
            pass

    # Eye-catcher comments
    hits = [kw for kw in EYE_CATCHER_KW if kw in comment]
    if hits:
        score += 1.5 * len(hits)
        reasons.append(f"Eye-catcher language: {', '.join(hits[:2])}")

    # Trouble in running — implies trip-upgrade potential
    troubles = [kw for kw in TROUBLE_KW if kw in comment]
    if troubles:
        score += 1.0
        reasons.append(f"Trouble in running: {troubles[0]}")

    # Negatives
    negs = [kw for kw in NEG_KW if kw in comment]
    if negs:
        score -= 1.5
        reasons.append(f"Negative comment: {negs[0]}")

    return {
        "score":      round(score, 2),
        "reasons":    reasons,
        "final_pos":  final_pos,
    }


def suggest_for_date(trials_path: Path | str,
                     min_score: float = 2.0) -> list[dict]:
    """Read a trials JSON file and return ranked suggestion rows.

    Each row contains the horse, score, reasons, and source-batch info.
    Sorted by score descending.
    """
    p = Path(trials_path)
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[dict] = []
    date_str = d.get("date") or p.stem.replace("trials_", "")
    for batch in d.get("batches", []):
        for r in batch.get("horses", []):
            s = score_runner(r)
            if s["score"] < min_score:
                continue
            out.append({
                "horse_name":   (r.get("horse_name") or "").strip().upper(),
                "horse_code":   r.get("horse_code"),
                "trainer":      r.get("trainer"),
                "jockey":       r.get("jockey"),
                "gear":         r.get("gear"),
                "draw":         r.get("draw"),
                "trial_date":   date_str,
                "course":       batch.get("course"),
                "distance_m":   batch.get("distance_m"),
                "going":        batch.get("going"),
                "batch_number": batch.get("batch_number"),
                "running_positions": r.get("running_positions"),
                "lbw":          r.get("lbw"),
                "comment":      r.get("comment"),
                "score":        s["score"],
                "reasons":      s["reasons"],
                "final_pos":    s["final_pos"],
            })
    out.sort(key=lambda r: (-r["score"], r["horse_name"]))
    return out


def already_in_blackbook(horse_name: str, bb_entries: Iterable[dict]) -> bool:
    """True if any active blackbook entry matches the horse name."""
    name = horse_name.strip().upper()
    for e in bb_entries:
        if e.get("status") != "active":
            continue
        if (e.get("horse_name") or "").strip().upper() == name:
            return True
    return False


def build_reasoning(row: dict) -> str:
    """Pre-fill text for blackbook reasoning."""
    parts = []
    fp = row.get("final_pos")
    if fp:
        parts.append(f"Trial: pos {fp}")
    rp = row.get("running_positions")
    if rp:
        parts.append(f"running positions {rp}")
    if row.get("lbw") and row["lbw"] not in ("-", ""):
        parts.append(f"{row['lbw']} beaten")
    if row.get("comment"):
        parts.append(f"Steward: '{row['comment']}'")
    if row.get("reasons"):
        parts.append("Score reasons — " + "; ".join(row["reasons"]))
    return ". ".join(parts)


if __name__ == "__main__":
    import sys
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYYMMDD trials date")
    ap.add_argument("--min-score", type=float, default=2.0)
    args = ap.parse_args()
    if not args.date:
        # Latest available
        cands = sorted(Path("reports").glob("trials_????????.json"))
        if not cands:
            print("No trials_*.json files found in reports/.")
            sys.exit(1)
        path = cands[-1]
    else:
        path = Path("reports") / f"trials_{args.date}.json"
    rows = suggest_for_date(path, min_score=args.min_score)
    print(f"\n{len(rows)} trial-watch suggestions from {path.name} "
          f"(score ≥ {args.min_score}):\n")
    for r in rows:
        print(f"  [{r['score']:>4.1f}]  {r['horse_name']:<24} "
              f"({r['trainer']:<14}) pos={r['final_pos']}  "
              f"{r['distance_m']}m {r['course']}")
        for reason in r["reasons"]:
            print(f"           · {reason}")
