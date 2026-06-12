import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else 'reports/fuse_backtest.json'
d = json.load(open(path, encoding='utf-8'))
print(f"meetings={d['n_meetings']}  lambda={d['lambda']}  window={d['start']}..{d['end']}")
print()
hdr = f"{'stream':24s} {'races':>5s} {'win1':>6s} {'plc1':>6s} {'t3w':>6s} {'qTop':>6s} {'qBox3':>6s} {'qBank':>6s} {'qinROI':>7s} {'qplROI':>7s}"
print(hdr)
print('-' * len(hdr))
for k, m in d['streams'].items():
    if k.startswith('pairs:'):
        print(f"{k:24s} {m['races']:5d} {'':6s} {'':6s} {'':6s} {m['q_top_pair']*100:5.1f}% {m['q_box3']*100:5.1f}% "
              f"{m['q_banker3']*100:5.1f}% "
              f"{(m['qin_box3_roi'] or 0)*100:6.1f}% {(m['qpl_banker3_roi'] or 0)*100:6.1f}%")
        continue
    print(f"{k:24s} {m['races']:5d} {m['top1_win']*100:5.1f}% {m['top1_place']*100:5.1f}% "
          f"{m['top3_has_winner']*100:5.1f}% {m['q_top_pair']*100:5.1f}% {m['q_box3']*100:5.1f}% "
          f"{m['q_banker3']*100:5.1f}% "
          f"{(m['qin_box3_roi'] or 0)*100:6.1f}% {(m['qpl_banker3_roi'] or 0)*100:6.1f}%")
print()
print('SEGMENTS (top1 win / top1 place / t3w):')
for k in ('fund', 'mkt', 'market', 'sarr', 'fund+market', 'fund+market+sarr', 'fund+market+sarr+et', 'all_equal'):
    if k not in d['streams']:
        continue
    segs = d['streams'][k]['segments']
    line = f"{k:24s}"
    for sk in ('ST-Turf', 'HV-Turf', 'ST-AWT'):
        s = segs.get(sk)
        line += f" | {sk} {s['top1_win']*100:4.1f}/{s['top1_place']*100:4.1f}/{s['t3w']*100:4.1f}" if s else f" | {sk} --"
    print(line)

if d.get('strategies'):
    print()
    print('BETTING STRATEGIES (edge-filtered, settled on real dividends/SP):')
    for k, s in d['strategies'].items():
        print(f"  {k:22s} bets={s['bets']:4d}  strike={s['strike']*100:5.1f}%  ROI={s['roi']*100:+6.1f}%")
