import json
from pathlib import Path
from collections import Counter, defaultdict
from statistics import mean

REPORTS=Path('reports')
def _load(p):
    try: return json.loads(p.read_text(encoding='utf-8'))
    except: return None
def _et(dc):
    for tag in ('v4.4','v3.4.8'):
        p=REPORTS/f'race_day_report_{dc}_{tag}.json'
        if p.exists(): return p
    return None

dates=sorted(set(p.name[len('race_day_report_'):][:8] for p in REPORTS.glob('race_day_report_????????_SARR.json')) &
             set(p.name[len('results_'):][:8] for p in REPORTS.glob('results_????????.json')))

pred=Counter(); actual=Counter(); confusion=defaultdict(Counter)
for dc in dates:
    et=_load(_et(dc)); rs=_load(REPORTS/f'results_{dc}.json')
    if not (et and rs): continue
    res_by={r['race_number']:r for r in rs['races']}
    for er in et['races']:
        rr=res_by.get(er['race_number'])
        if not rr: continue
        pp=er.get('pace'); ap=rr.get('actual_pace_label')
        if pp: pred[pp]+=1
        if ap: actual[ap]+=1
        if pp and ap: confusion[pp][ap]+=1

print('Predicted distribution:', dict(pred.most_common()))
print('Actual    distribution:', dict(actual.most_common()))
print()
labels=['Fast','Slightly Fast','Normal','Slightly Slow','Slow']
header = 'pred\\actual'.ljust(15) + ' '.join(f'{l[:5]:>6}' for l in labels)
print(header)
for pp in labels:
    if pp not in confusion: continue
    row=confusion[pp]
    print(pp.ljust(15) + ' '.join(f'{row.get(a,0):>6}' for a in labels))
