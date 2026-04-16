"""Deep comparison: Apr 15 model predictions vs actual results."""
import json, sys, os
sys.stdout.reconfigure(encoding='utf-8')

BASE = r'c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards\reports'
with open(os.path.join(BASE, 'race_day_report_20260415_v4.4.json'), 'r', encoding='utf-8') as f:
    pred = json.load(f)
with open(os.path.join(BASE, 'results_20260415.json'), 'r', encoding='utf-8') as f:
    res = json.load(f)

BB_PATH = os.path.join(os.path.dirname(BASE), 'blackbook.json')
bb_horses = set()
bb_map = {}
if os.path.exists(BB_PATH):
    with open(BB_PATH, 'r', encoding='utf-8') as f:
        bb_raw = json.load(f)
    bb_data = bb_raw.get('entries', []) if isinstance(bb_raw, dict) else bb_raw
    for entry in bb_data:
        nm = entry.get('horse_name', '').upper().strip()
        bb_horses.add(nm)
        bb_map[nm] = entry

# Build results lookup: race_number -> {horse_name_upper -> runner_dict}
res_lookup = {}
for race in res['races']:
    rn = race['race_number']
    res_lookup[rn] = {}
    for r in race['runners']:
        nm = r['horse_name'].upper().strip()
        res_lookup[rn][nm] = r

# ────────────────────────── RACE BY RACE ──────────────────────────
winners_found_rk = []  # rank at which winner appeared (None if not in top 6)
all_pick_finishes = []  # (race, rank, finish)
time_errors = []        # (race, horse, proj, actual, error)
esz_data = []           # (race, horse, esz, style, finish, rank)
ssi_data = []           # (race, horse, ssi, finish)
sm_data = []            # (race, horse, finish)
style_data = []         # (race, horse, style, finish, positions)
leader_hold = []        # (race, horse, style, held_top3)

print("=" * 80)
print("APR 15 HAPPY VALLEY — DETAILED MODEL ACCURACY REPORT")
print("=" * 80)
print(f"Results available: R1-R{len(res['races'])} of 9\n")

for pred_race in pred['races']:
    rn = pred_race['race_number']
    if rn not in res_lookup:
        continue
    
    dist = pred_race.get('distance', '?')
    cls = pred_race.get('race_class', '?')
    pace = pred_race.get('pace', '?')
    pace_score = pred_race.get('pace_score', 0)
    
    # Get actual results sorted by place
    res_runners = sorted(res_lookup[rn].values(), key=lambda x: int(x['place']) if str(x['place']).isdigit() else 99)
    actual_winner = res_runners[0]['horse_name'] if res_runners else '?'
    actual_top3 = [r['horse_name'] for r in res_runners[:3]]
    
    picks = sorted(pred_race.get('picks', []), key=lambda x: x.get('rank', 99))
    
    print(f"\n{'─'*80}")
    print(f"RACE {rn} — {dist}m {cls} | Pace: {pace} ({pace_score:+.2f}s)")
    print(f"{'─'*80}")
    print(f"ACTUAL: 1st={actual_top3[0]} | 2nd={actual_top3[1]} | 3rd={actual_top3[2]}")
    
    # Winner section times
    winner_r = res_runners[0]
    sections = winner_r.get('sectiontimes', [])
    if sections:
        print(f"Winner sections: {sections}")
    
    # Going
    going = None
    for r in res['races']:
        if r['race_number'] == rn:
            going = r.get('going', '?')
    print(f"Going: {going}")
    
    # Check if winner in picks
    winner_rank = None
    for p in picks[:6]:
        if p['horse_name'].upper().strip() == actual_winner.upper().strip():
            winner_rank = p['rank']
            break
    winners_found_rk.append(winner_rank)
    
    if winner_rank:
        print(f">>> WINNER found at RANK {winner_rank} <<<")
    else:
        # Where was winner ranked?
        for p in picks:
            if p['horse_name'].upper().strip() == actual_winner.upper().strip():
                print(f">>> WINNER was rank {p['rank']} (outside top 6) <<<")
                break
        else:
            print(f">>> WINNER {actual_winner} not ranked in picks <<<")
    
    print()
    print(f"{'Rk':<3} {'Horse':<22} {'WP%':>5} {'Fin':>4} {'Odds':>6} {'ESZ':>5} {'Style':<10} {'Gate':>4} {'SSI':>6} {'ProjT':>7} {'ActT':>7} {'Err':>7} {'Flags'}")
    print(f"{'-'*3} {'-'*22} {'-'*5} {'-'*4} {'-'*6} {'-'*5} {'-'*10} {'-'*4} {'-'*6} {'-'*7} {'-'*7} {'-'*7} {'-'*10}")
    
    for p in picks[:6]:
        rank = p['rank']
        hname = p['horse_name']
        hname_u = hname.upper().strip()
        wp = (p.get('win_prob', 0) or 0) * 100
        esz = p.get('early_speed_z', 0) or 0
        style = p.get('style', '?') or '?'
        draw = p.get('draw', '?') or '?'
        ssi = p.get('avg_ssi', 0) or 0
        proj_t = p.get('projected_time', 0) or 0
        flags = p.get('flags', [])
        
        # Results
        rd = res_lookup[rn].get(hname_u, {})
        fin = rd.get('place', '?')
        odds = rd.get('win_odds', '?')
        actual_t = rd.get('finish_time_seconds', None)
        if actual_t is None:
            actual_t = 0
        
        # Time error
        err_str = ''
        if proj_t and actual_t:
            err = actual_t - proj_t
            err_str = f'{err:+.2f}s'
            time_errors.append((rn, hname, proj_t, actual_t, err))
        
        fin_int = int(fin) if str(fin).isdigit() else 99
        all_pick_finishes.append((rn, rank, fin_int))
        esz_data.append((rn, hname, esz, style, fin_int, rank))
        ssi_data.append((rn, hname, ssi, fin_int))
        
        # Markers
        marker = ''
        if fin_int == 1: marker = '★'
        elif fin_int <= 3: marker = '✓'
        
        bb_tag = ' [BB]' if hname_u in bb_horses else ''
        flag_str = ','.join(flags) if flags else ''
        
        actual_t_str = f'{actual_t:>7.2f}' if actual_t else '    N/A'
        print(f"{marker:<1}{rank:<2} {hname:<22} {wp:>5.1f} {fin:>4} {odds:>6} {esz:>+5.1f} {style:<10} Gt{draw:<2} {ssi:>+6.2f} {proj_t:>7.2f} {actual_t_str} {err_str:>7} {flag_str}{bb_tag}")
        
        # Positions
        positions = rd.get('positions', [])
        if positions:
            print(f"   Positions: {' → '.join(str(x) for x in positions)}")
        
        # Running style vs actual positions
        style_data.append((rn, hname, style, fin_int, positions))
        if style in ('Leader', 'On-Pace') and positions:
            first_pos = positions[0] if positions else 99
            held = fin_int <= 3
            leader_hold.append((rn, hname, style, held, first_pos, fin_int))
    
    # Speed map beneficiaries
    smap = pred_race.get('speed_map', {})
    beneficiaries = smap.get('beneficiaries', [])
    if beneficiaries:
        print(f"\n  Speed Map Beneficiaries:")
        for b in beneficiaries:
            bname = b if isinstance(b, str) else b.get('horse_name', '?')
            bname_u = bname.upper().strip()
            rd = res_lookup[rn].get(bname_u, {})
            fin = rd.get('place', '?')
            fin_int = int(fin) if str(fin).isdigit() else 99
            sm_data.append((rn, bname, fin_int))
            marker = '★' if fin_int == 1 else ('✓' if fin_int <= 3 else ' ')
            print(f"    {marker} {bname}: finished {fin}")
    
    # BB horses in this race
    all_runners_pred = pred_race.get('picks', [])
    for p in all_runners_pred:
        hname_u = p.get('horse_name', '').upper().strip()
        if hname_u in bb_horses:
            rd = res_lookup[rn].get(hname_u, {})
            fin = rd.get('place', '?')
            bb_entry = bb_map.get(hname_u, {})
            print(f"\n  [BLACKBOOK] {p['horse_name']} — conf={bb_entry.get('confidence','?')} fin={fin}")
            print(f"    Reason: {bb_entry.get('reasoning','')[:80]}")

# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("AGGREGATE STATISTICS")
print("=" * 80)

# 1. Pick accuracy
print("\n── PICK ACCURACY ──")
n_races = len(res['races'])
w_in_top6 = sum(1 for w in winners_found_rk if w is not None)
w_at_rk1 = sum(1 for w in winners_found_rk if w == 1)
print(f"Winner in top 6: {w_in_top6}/{n_races} ({w_in_top6/n_races*100:.0f}%)")
print(f"Winner at Rk1: {w_at_rk1}/{n_races} ({w_at_rk1/n_races*100:.0f}%)")
for rn_idx, wr in enumerate(winners_found_rk, 1):
    print(f"  R{rn_idx}: winner at rank {wr if wr else 'OUTSIDE top 6'}")

place_hits = sum(1 for _, _, f in all_pick_finishes if f <= 3)
top4_hits = sum(1 for _, _, f in all_pick_finishes if f <= 4)
print(f"\nTop-3 hit rate (from {len(all_pick_finishes)} picks): {place_hits} = {place_hits/len(all_pick_finishes)*100:.0f}%")
print(f"Top-4 hit rate: {top4_hits} = {top4_hits/len(all_pick_finishes)*100:.0f}%")

# By rank
for r in range(1, 7):
    hits = [(rn, f) for rn, rk, f in all_pick_finishes if rk == r]
    placed = sum(1 for _, f in hits if f <= 3)
    print(f"  Rank {r}: {placed}/{len(hits)} placed (top 3)")

# 2. Time errors
print("\n── TIME PROJECTION ──")
errs = [e[4] for e in time_errors]
if errs:
    mean_err = sum(errs) / len(errs)
    abs_errs = [abs(e) for e in errs]
    mean_abs = sum(abs_errs) / len(abs_errs)
    print(f"Mean error: {mean_err:+.2f}s  (neg = actual faster than projected)")
    print(f"Mean absolute error: {mean_abs:.2f}s")
    print(f"Range: {min(errs):+.2f}s to {max(errs):+.2f}s")
    faster = sum(1 for e in errs if e < 0)
    print(f"Horses ran faster than projected: {faster}/{len(errs)} ({faster/len(errs)*100:.0f}%)")
    # Per race mean
    for rn in sorted(set(e[0] for e in time_errors)):
        race_errs = [e[4] for e in time_errors if e[0] == rn]
        print(f"  R{rn} mean error: {sum(race_errs)/len(race_errs):+.2f}s")

# 3. ESZ analysis
print("\n── ESZ ACCURACY ──")
# ESZ negative = fast early (leaders/on-pace), ESZ positive = slow early (closers)
for style_group, label in [('Leader', 'Leader'), ('On-Pace', 'On-Pace'), ('Midfield', 'Midfield'), ('Closer', 'Closer')]:
    group = [(r, h, e, s, f, rk) for r, h, e, s, f, rk in esz_data if s == style_group]
    if group:
        placed = sum(1 for _, _, _, _, f, _ in group if f <= 3)
        avg_esz = sum(e for _, _, e, _, _, _ in group) / len(group)
        print(f"  {label}: {len(group)} picks, {placed} placed, avg ESZ={avg_esz:+.1f}")

# 4. SSI analysis
print("\n── SSI (Section Speed Index) ──")
placed_ssi = [(h, s, f) for _, h, s, f in ssi_data if f <= 3]
missed_ssi = [(h, s, f) for _, h, s, f in ssi_data if f > 6]
if placed_ssi:
    avg_placed = sum(s for _, s, _ in placed_ssi) / len(placed_ssi)
    print(f"Placed horses (top 3) avg SSI: {avg_placed:+.2f}")
if missed_ssi:
    avg_missed = sum(s for _, s, _ in missed_ssi) / len(missed_ssi)
    print(f"Missed horses (7th+) avg SSI: {avg_missed:+.2f}")
# SSI correlation direction
for _, h, s, f in ssi_data:
    if f <= 3:
        print(f"  ✓ {h}: SSI={s:+.2f} fin={f}")
    elif f >= 9:
        print(f"  ✗ {h}: SSI={s:+.2f} fin={f}")

# 5. Speed map
print("\n── SPEED MAP BENEFICIARY ACCURACY ──")
sm_with_results = [(r, h, f) for r, h, f in sm_data if f < 99]
if sm_with_results:
    sm_placed = sum(1 for _, _, f in sm_with_results if f <= 3)
    sm_won = sum(1 for _, _, f in sm_with_results if f == 1)
    print(f"Total: {len(sm_with_results)}, Won: {sm_won}, Placed: {sm_placed} ({sm_placed/len(sm_with_results)*100:.0f}%)")

# 6. Leader hold rate
print("\n── FRONT-RUNNER / LEADER ANALYSIS ──")
if leader_hold:
    held = sum(1 for _, _, _, h, _, _ in leader_hold if h)
    print(f"Leaders/On-Pace who placed: {held}/{len(leader_hold)} ({held/len(leader_hold)*100:.0f}%)")
    for rn, h, st, held, fpos, fin in leader_hold:
        tag = '✓' if held else '✗'
        print(f"  {tag} R{rn} {h} ({st}) gate_pos→{fpos} fin={fin}")

# 7. Position movement (style validation)
print("\n── RUNNING STYLE vs ACTUAL MOVEMENT ──")
for rn, h, style, fin, positions in style_data:
    if not positions or len(positions) < 2:
        continue
    first = positions[0]
    last_before_fin = positions[-1]
    movement = first - last_before_fin  # positive = gained positions
    expected_forward = style in ('Closer', 'Midfield')
    actual_forward = movement > 0
    correct = (expected_forward == actual_forward) or style == 'Leader'
    tag = '✓' if correct else '✗'
    if fin <= 3:
        print(f"  {tag} R{rn} {h} style={style} pos={first}→{last_before_fin}→fin{fin} move={'gained' if movement>0 else 'lost'} {abs(movement)}")

# 8. Blackbook summary
print("\n── BLACKBOOK HORSES ON CARD ──")
for rn in sorted(res_lookup.keys()):
    for hname_u in res_lookup[rn]:
        if hname_u in bb_horses:
            rd = res_lookup[rn][hname_u]
            fin = rd.get('place', '?')
            odds = rd.get('win_odds', '?')
            bb_e = bb_map.get(hname_u, {})
            print(f"  R{rn} {hname_u}: fin={fin} odds={odds} conf={bb_e.get('confidence','?')}")
            print(f"    Reason: {bb_e.get('reasoning','')[:100]}")

print("\n" + "=" * 80)
print("KEY TAKEAWAYS")
print("=" * 80)
