import json
d = json.load(open('reports/race_day_report_20260412_v3.4.8.json','r',encoding='utf-8'))
for rn in [5, 10]:
    r = [x for x in d['races'] if x['race_number']==rn][0]
    sm = r['speed_map']
    print(f"R{rn}: {sm['n_cols']}x{sm['n_rows']}")
    grid = {}
    for h in sm['grid']:
        grid[(h['col'],h['row'])] = f"{h['horse_no']}.{h['horse_name'][:14]}"
    for row in range(sm['n_rows'], 0, -1):
        cells = []
        for col in range(1, sm['n_cols']+1):
            cells.append(grid.get((col,row), '').ljust(20))
        lbl = {1:'RAIL', 2:'W2', 3:'WIDE'}.get(row, f'W{row}')
        print(f"  {'|'.join(cells)} {lbl}")
    bens = sm.get('beneficiaries', [])
    print(f"  Bens: {[b['horse_name'] for b in bens]}")
    print()
