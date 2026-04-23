"""Correlate parsed trip data with actual finish positions."""
import json

res = json.load(open(r'reports\results_20260412.json', 'r', encoding='utf-8'))
trip = json.load(open(r'running_position_photos\20260412\R2.json', 'r', encoding='utf-8'))

race2 = next(r for r in res['races'] if r['race_number'] == 2)
pos_by_name = {r['horse_name'].upper(): (r.get('place', '?'), r.get('horse_no', '?'))
               for r in race2['runners']}

rows = []
for h in trip['horses']:
    nm = h['horse_name'].upper()
    place, no = pos_by_name.get(nm, ('?', '?'))
    rows.append((place, no, h['horse_name'], h['avg_x_frac'], h['avg_y_in_band'],
                 h['n_frames_seen'], h['wide_all_way'], h['rail_all_way']))
# Sort by place
def _p(r):
    try: return int(r[0])
    except: return 99
rows.sort(key=_p)

print(f"R2  2026-04-12  ({race2.get('race_name','')})  field_size={len(race2['runners'])}")
print(f"{'Pl':>2s} {'No':>3s} {'Horse':<22s} {'avg_x':>6s} {'avg_y':>6s} {'seen':>4s} {'wAll':>5s} {'rAll':>5s}")
for r in rows:
    wa = 'WIDE' if r[6] else ''
    ra = 'rail' if r[7] else ''
    print(f"{str(r[0]):>2s} {str(r[1]):>3s} {r[2][:22]:<22s} {r[3] or 0:>6.3f} {r[4] or 0:>6.3f} "
          f"{r[5]:>4d} {wa:>5s} {ra:>5s}")
