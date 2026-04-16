"""
Pace Analysis: Actual race pace from results JSON + comparison with model predictions.
"""
import json, os, numpy as np

base = r'c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards\reports'

# Load all results JSON
results_files = sorted([f for f in os.listdir(base) if f.startswith('results_') and f.endswith('.json')])
# Load prediction reports
pred_files = sorted([f for f in os.listdir(base) if f.startswith('race_day_report_') and f.endswith('.json')])

print(f'Results files: {[f for f in results_files]}')
print(f'Prediction files: {[f for f in pred_files]}')

# Build prediction lookup: date -> race_number -> pace info
pred_lookup = {}
for pf in pred_files:
    with open(os.path.join(base, pf)) as fp:
        pd_data = json.load(fp)
    date_str = pf.replace('race_day_report_','').replace('_v4.4.json','')
    pred_lookup[date_str] = {}
    for race in pd_data.get('races', []):
        rn = race.get('race_number', 0)
        pred_lookup[date_str][rn] = {
            'pace': race.get('pace', '?'),
            'pace_score': race.get('pace_score', 0),
            'pace_reasons': race.get('pace_reasons', []),
            'pace_leaders': race.get('pace_leaders', []),
        }

# Stats
all_leader_outcomes = []  # (held_top3, total_leaders)
pace_correct = 0
pace_total = 0

for rf in results_files:
    with open(os.path.join(base, rf)) as fp:
        data = json.load(fp)
    
    date_str = rf.replace('results_','').replace('.json','')
    races = data.get('races', [])
    
    print(f'\n{"="*80}')
    print(f'DATE: {date_str}  ({len(races)} races)')
    print(f'{"="*80}')
    
    for race in races:
        rn = race['race_number']
        dist = race.get('distance', '?')
        going = race.get('going', '?')
        rc = race.get('race_class', '?')
        runners = race.get('runners', [])
        n = len(runners)
        
        # Parse section times for each runner
        runner_secs = []
        for h in runners:
            st_raw = h.get('sectiontimes', [])
            secs = []
            for s in st_raw:
                try:
                    secs.append(float(s))
                except:
                    pass
            if secs:
                runner_secs.append(secs)
        
        # Parse running positions
        first_call = []
        actual_leaders = []
        for h in runners:
            rp = str(h.get('running_position', ''))
            parts = rp.strip().split()
            if parts:
                try:
                    fp = int(parts[0])
                    first_call.append((h['horse_name'], fp))
                    if fp <= 2:
                        actual_leaders.append(h['horse_name'])
                except:
                    pass
        
        # Winner
        winner = None
        winner_time = None
        for h in runners:
            if str(h.get('place','')) == '1':
                winner = h['horse_name']
                winner_time = h.get('finish_time_seconds')
                break
        
        # Did leaders hold top 3?
        finish_pos = {}
        for h in runners:
            try:
                finish_pos[h['horse_name']] = int(h['place'])
            except:
                pass
        
        leader_results = []
        for l in actual_leaders:
            fp = finish_pos.get(l, 99)
            leader_results.append((l, fp))
        leaders_held = sum(1 for _, p in leader_results if p <= 3)
        all_leader_outcomes.append((leaders_held, len(leader_results)))
        
        # Compute pace from section times
        if runner_secs:
            # Average section times across field
            max_secs = max(len(s) for s in runner_secs)
            avg_secs = []
            for i in range(max_secs):
                vals = [s[i] for s in runner_secs if len(s) > i]
                avg_secs.append(np.mean(vals))
            
            total_time = sum(avg_secs)
            first_sec = avg_secs[0]
            last_sec = avg_secs[-1]
            
            # Early pace ratio (first section / average section)
            avg_sec = total_time / len(avg_secs) if avg_secs else 0
            early_ratio = first_sec / avg_sec if avg_sec > 0 else 1.0
            
            # Closer's advantage: last sec vs first non-400m sec  
            # Fast early = low first_sec relative to distance
            # Compute per-200m speed
            per_200m = [s for s in avg_secs]  # each section ~ 200m (HKJC standard)
            early_half = per_200m[:len(per_200m)//2]
            late_half = per_200m[len(per_200m)//2:]
            
            early_avg_spd = np.mean(early_half) if early_half else 0
            late_avg_spd = np.mean(late_half) if late_half else 0
            
            # Pace diff: positive = fast start (early sections faster/lower time)
            pace_diff = late_avg_spd - early_avg_spd
            
            if pace_diff > 0.7:
                actual_pace = 'FAST (front-loaded)'
            elif pace_diff > 0.3:
                actual_pace = 'SLIGHTLY FAST'
            elif pace_diff < -0.7:
                actual_pace = 'SLOW (back-loaded)'
            elif pace_diff < -0.3:
                actual_pace = 'SLIGHTLY SLOW'
            else:
                actual_pace = 'EVEN'
        else:
            avg_secs = []
            pace_diff = 0
            actual_pace = 'NO DATA'
        
        # Model prediction for this race (if available)
        pred = pred_lookup.get(date_str, {}).get(rn, None)
        pred_str = ''
        if pred:
            pred_str = f' | MODEL: {pred["pace"]} ({pred["pace_score"]:+.2f}s)'
            pace_total += 1
        
        print(f'\n  R{rn} {dist}m C{rc} {going} | {n} runners')
        print(f'    Winner: {winner} ({winner_time}s)')
        print(f'    Section avgs: {[round(x,2) for x in avg_secs]}' if avg_secs else '    NO SECTION DATA')
        
        if avg_secs:
            print(f'    Early half avg: {early_avg_spd:.2f}s  Late half avg: {late_avg_spd:.2f}s  Diff: {pace_diff:+.2f}s')
        
        print(f'    ACTUAL PACE: {actual_pace}{pred_str}')
        print(f'    Leaders at 1st call: {actual_leaders[:4]}')
        print(f'    Leader finishes: {leader_results[:4]} | Held top3: {leaders_held}/{len(leader_results)}')

# Summary stats
print(f'\n\n{"="*80}')
print(f'SUMMARY')
print(f'{"="*80}')
total_l = sum(t for _, t in all_leader_outcomes)
held_l = sum(h for h, _ in all_leader_outcomes)
print(f'Leader hold rate: {held_l}/{total_l} = {held_l/total_l*100:.0f}%' if total_l > 0 else 'No leader data')
print(f'Across {len(all_leader_outcomes)} races from {len(results_files)} race days')
