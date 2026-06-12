import json
from betting_strategy import build_meeting_tickets
from decision_engine import build_meeting_slate

items = build_meeting_tickets("20260610", odds_mode="review")
print("tickets:", len(items), "fuse_used:", sum(1 for i in items if i.get("fuse_used")))
it = items[0]
r0 = it["rows"][0]
print("R1 top row:", r0["horse_no"], r0["horse_name"], "p_model:", r0["p_model"],
      "fuse_rank:", r0.get("fuse_rank"), "p_top3_fuse:", r0.get("p_top3_fuse"))
t = it["ticket"]
print("R1 play:", t.get("play"), "banker:", (t.get("banker") or {}).get("horse_no"))

slate = build_meeting_slate("20260610", bankroll=10000, mode="balanced")
s = slate.get("summary", {})
print("slate races:", len(slate.get("races", [])), "| accepted:",
      s.get("n_bets", s.get("accepted")), "| exposure:", s.get("total_stake", s.get("exposure")))
for r in slate.get("races", [])[:3]:
    st = r.get("stake") or {}
    print(" R", r.get("race_number"), r.get("ticket", {}).get("play"),
          "accepted" if st.get("accepted") else "skip", st.get("stake_hkd"),
          (r.get("reason") or st.get("reason") or "")[:60])
