from parse_acct_statement import parse_statement
from user_bets import qtt_mb_perms
from pathlib import Path

parsed = parse_statement(Path(r"C:\Users\tbhbr\Downloads\acctstmt (2).txt"))
for p in parsed:
    legs = p.get("multi_legs")
    if not legs:
        continue
    n = qtt_mb_perms(legs)
    per = p["per_combo_stake"]
    debit = p["total_debit"]
    ok = abs(n * per - debit) < 0.01
    status = "OK" if ok else "MISMATCH"
    print(f"ref {p['bookie_ref']} R{p['race_number']}  legs={legs}  "
          f"n_perms={n}  ${per:.0f} x {n} = ${n*per:.0f}  vs debit ${debit:.0f}  {status}")
