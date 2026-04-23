import json
from pathlib import Path
REPORTS = Path('reports')
targets = {'GIDDY UP','SOARING BRONCO','WINNING CHAMPION'}
for f in sorted(REPORTS.glob('trials_20260*.json'), reverse=True):
    if f.name < 'trials_20260201': break
    d = json.load(open(f, encoding='utf-8'))
    td = d.get('date','')
    for b in d.get('batches', []):
        n = b.get('n_horses',0)
        for h in b.get('horses', []):
            nm = (h.get('horse_name') or '').upper().strip()
            if nm in targets:
                rp = h.get('running_positions',[])
                print(td, nm, 'rp=', rp, 'n=', n, '|', (h.get('comment','') or '')[:120])
