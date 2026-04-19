"""Backfill actual-pace fields into every reports/results_*.json (idempotent)."""
import json
from pathlib import Path
from pace_utils import annotate_results_meeting, classify_pace

REPORTS = Path(__file__).parent / "reports"


def main():
    files = sorted(REPORTS.glob("results_*.json"))
    if not files:
        print("No results files found.")
        return
    print(f"Processing {len(files)} result files ...")
    summary = []
    for p in files:
        try:
            meeting = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ! {p.name}: unreadable ({e})")
            continue
        before = sum(1 for r in meeting.get("races", []) if r.get("actual_pace_label"))
        annotate_results_meeting(meeting)
        after = sum(1 for r in meeting.get("races", [])
                    if r.get("actual_pace_label") and r["actual_pace_label"] != "N/A")
        p.write_text(json.dumps(meeting, ensure_ascii=False, indent=2), encoding="utf-8")
        labels = [r.get("actual_pace_label", "N/A") for r in meeting.get("races", [])]
        print(f"  {p.name}: {after}/{len(labels)} races annotated "
              f"({', '.join(labels)})")
        summary.append((p.name, labels))
    print(f"\nDone: {len(files)} files updated.")


if __name__ == "__main__":
    main()
