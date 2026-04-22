"""One-off backfill driver for 2026 results (runs scrape_hkjc_results per date)."""
from __future__ import annotations
import subprocess, sys, os
from pathlib import Path

BASE = Path(__file__).parent
REPORTS = BASE / "reports"

DATES_2026 = [
    "2026-01-01","2026-01-04","2026-01-07","2026-01-11","2026-01-14",
    "2026-01-18","2026-01-21","2026-01-25","2026-01-28",
    "2026-02-01","2026-02-04","2026-02-08","2026-02-11","2026-02-14",
    "2026-02-19","2026-02-22","2026-02-25",
    "2026-03-01","2026-03-04","2026-03-08","2026-03-11","2026-03-15",
    "2026-03-18","2026-03-22","2026-03-25","2026-03-29",
    "2026-04-22",
]

PY = os.environ.get("PYTHON_EXE", r"C:\Users\tbhbr\miniconda3\python.exe")

os.environ["PYTHONIOENCODING"] = "utf-8"

missing = []
for d in DATES_2026:
    out = REPORTS / f"results_{d.replace('-', '')}.json"
    if out.exists():
        print(f"[skip] {d}  (exists)")
        continue
    missing.append(d)

print(f"\n{len(missing)} dates to scrape: {missing}\n")

ok, fail = [], []
for i, d in enumerate(missing, 1):
    print(f"\n===== [{i}/{len(missing)}] {d} =====")
    try:
        r = subprocess.run(
            [PY, "scrape_hkjc_results.py", "--date", d],
            cwd=str(BASE), capture_output=True, text=True, timeout=180,
        )
        if r.returncode == 0:
            ok.append(d)
            # keep output short
            tail = "\n".join(r.stdout.splitlines()[-4:])
            print(tail)
        else:
            fail.append(d)
            print(f"EXIT {r.returncode}\n{r.stdout[-500:]}\n{r.stderr[-500:]}")
    except subprocess.TimeoutExpired:
        fail.append(d)
        print("TIMEOUT")
    except Exception as e:
        fail.append(d)
        print(f"ERR {e}")

print(f"\n===== DONE =====")
print(f"OK ({len(ok)}): {ok}")
print(f"FAIL ({len(fail)}): {fail}")
