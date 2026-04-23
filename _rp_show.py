import json, sys
p = sys.argv[1] if len(sys.argv) > 1 else r'running_position_photos\20260412\R2.json'
d = json.load(open(p, 'r', encoding='utf-8'))
print('Bands:')
for b in d['bands']:
    print(f"  {str(b['label']):>6s}  y={b['y_top']:>3d}-{b['y_bot']:<3d}  kind={b['kind']}")
print()
print(f"{'Horse':<22s} {'No':>3s} {'seen':>4s} {'avg_x':>6s} {'avg_y':>6s} {'wideF':>5s} {'railF':>5s} {'wideAll':>7s} {'railAll':>7s}")
for h in sorted(d['horses'], key=lambda e: -e['n_frames_seen']):
    ax = h['avg_x_frac'] or 0.0
    ay = h['avg_y_in_band'] or 0.0
    print(f"{h['horse_name'][:22]:<22s} {str(h['horse_no']):>3s} {h['n_frames_seen']:>4d} "
          f"{ax:>6.3f} {ay:>6.3f} {h['wide_frames']:>5d} {h['rail_frames']:>5d} "
          f"{str(h['wide_all_way']):>7s} {str(h['rail_all_way']):>7s}")
