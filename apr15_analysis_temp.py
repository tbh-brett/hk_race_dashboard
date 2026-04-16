import json

bb = json.load(open(r'c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards\blackbook.json'))
entries = bb.get('entries', [])
print(f'Total entries: {len(entries)}')
active = [e for e in entries if e.get('status') == 'active']
print(f'Active: {len(active)}')
print()

for e in entries:
    hn = e.get('horse_name', '?')
    conf = e.get('confidence', '?')
    status = e.get('status', '?')
    added = e.get('added_date', '?')
    tags = e.get('tags', [])
    reason = e.get('reasoning', '')[:80]
    perfs = e.get('performances', [])
    dists = e.get('conditions', {}).get('preferred_distance', [])
    print(f'{hn:28s} | {status:8s} | {conf:6s} | added {added} | dists {dists}')
    print(f'  reason: {reason}')
    if perfs:
        for perf in perfs[-3:]:
            print(f'  perf: {perf.get("date","?")} R{perf.get("race_number","?")} finish={perf.get("finish","?")} verdict={perf.get("bb_verdict","?")}')
    print()

# Now load Apr 15 prediction
pred = json.load(open(r'c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards\reports\race_day_report_20260415_v4.4.json'))
results = json.load(open(r'c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards\reports\results_20260415.json'))

print('='*80)
print('APRIL 15 — PREDICTION vs RESULTS')
print('='*80)

pred_races = {r['race_number']: r for r in pred.get('races', [])}
res_races = {r['race_number']: r for r in results.get('races', [])}

# Build bb lookup
bb_horses = {e['horse_name'].upper().strip(): e for e in entries if e.get('status') == 'active'}

for rn in sorted(pred_races.keys()):
    pr = pred_races[rn]
    rr = res_races.get(rn, {})
    
    picks = pr.get('picks', [])
    runners = rr.get('runners', [])
    
    # Build result lookup
    res_lookup = {}
    for r in runners:
        res_lookup[r['horse_name'].upper().strip()] = r
    
    dist = pr.get('distance', '?')
    cls = pr.get('race_class', '?')
    pace = pr.get('pace', '?')
    pace_score = pr.get('pace_score', 0)
    surface = 'AWT' if pr.get('is_awt') else 'Turf'
    course = pr.get('race_course', '?')
    
    print(f'\nR{rn} — {dist}m {surface} ({course}) C{cls} | Predicted pace: {pace} ({pace_score:+.2f}s)')
    
    # Actual result top 3
    placed = sorted(runners, key=lambda x: int(x['place']) if x['place'].isdigit() else 99)[:3]
    print(f'  RESULT: 1st={placed[0]["horse_name"] if placed else "?"} | 2nd={placed[1]["horse_name"] if len(placed)>1 else "?"} | 3rd={placed[2]["horse_name"] if len(placed)>2 else "?"}')
    
    # Did our picks hit?
    for i, p in enumerate(picks[:6]):
        hn = p['horse_name']
        hn_up = hn.upper().strip()
        actual = res_lookup.get(hn_up, {})
        actual_place = actual.get('place', '?')
        actual_odds = actual.get('win_odds', '?')
        actual_rp = actual.get('running_position', '?')
        actual_ft = actual.get('finish_time_seconds', None)
        
        wp = p.get('win_prob', 0)
        esz = p.get('early_speed_z', 0)
        ssi = p.get('avg_ssi', None)
        style = p.get('style', '?')
        draw = p.get('draw', '?')
        proj_time = p.get('projected_time', None)
        flags = p.get('flags', [])
        
        bb_tag = ' [BB]' if hn_up in bb_horses else ''
        ssi_str = f' SSI={ssi:+.2f}' if ssi is not None else ''
        proj_str = f' proj={proj_time:.2f}s' if proj_time else ''
        ft_str = f' actual={actual_ft:.2f}s' if actual_ft else ''
        time_err = f' err={actual_ft - proj_time:+.2f}s' if actual_ft and proj_time else ''
        
        hit = '✓' if str(actual_place) in ('1','2','3') else ' '
        win = '★' if str(actual_place) == '1' else ' '
        
        print(f'  {win}{hit} Rk{i+1}: #{p.get("horse_no","?")} {hn:22s} | WP={wp*100:4.0f}% | Fin={actual_place:>3s} odds={actual_odds:>5s} | ESZ={esz:+.1f} {style:10s} Gt{draw}{ssi_str}{proj_str}{ft_str}{time_err} {flags}{bb_tag}')
        
        # Running position comparison with speed map
        if actual_rp and actual_rp != '?':
            print(f'       Positions: {actual_rp}')
    
    # Speed map
    smap = pr.get('speed_map', {})
    beneficiaries = smap.get('beneficiaries', [])
    if beneficiaries:
        ben_names = [b['horse_name'] for b in beneficiaries]
        ben_results = []
        for bn in ben_names:
            actual = res_lookup.get(bn.upper().strip(), {})
            ben_results.append(f'{bn}={actual.get("place","?")}')
        print(f'  Speed map beneficiaries: {", ".join(ben_results)}')
    
    # Section times from results
    if runners:
        winner = [r for r in runners if r['place'] == '1']
        if winner:
            st_raw = winner[0].get('sectiontimes', [])
            secs = [float(s) for s in st_raw if s]
            if secs:
                print(f'  Winner sections: {secs}')

print('\n\n' + '='*80)
print('BLACKBOOK HORSES ON APR 15 CARD')
print('='*80)
for rn in sorted(pred_races.keys()):
    pr = pred_races[rn]
    rr = res_races.get(rn, {})
    picks = pr.get('picks', [])
    runners = rr.get('runners', [])
    res_lookup = {r['horse_name'].upper().strip(): r for r in runners}
    
    for p in picks:
        hn_up = p['horse_name'].upper().strip()
        if hn_up in bb_horses:
            actual = res_lookup.get(hn_up, {})
            bb_entry = bb_horses[hn_up]
            print(f'  R{rn} {p["horse_name"]:22s} Rk{p.get("rank","?")} WP={p.get("win_prob",0)*100:.0f}% -> Fin={actual.get("place","?")} odds={actual.get("win_odds","?")}')
            print(f'    BB: {bb_entry.get("confidence","?")} conf | {bb_entry.get("reasoning","")[:80]}')
