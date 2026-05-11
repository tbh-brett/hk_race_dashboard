"""
Walk-forward evaluation of all four frameworks against the market baseline.

Split:
  - Anchor history starts at the earliest race in the DB (2024-09).
  - For each race date in the validation window, all four frameworks are
    *re-fit* (or just re-prior'd) using STRICTLY-prior data only, then asked
    to score that day's runners. No row from the date being scored is ever
    used to construct features for itself.

Reported metrics (per framework) on the held-out window:
    log_loss_runner, log_loss_race, brier, top1, top3
plus the same four metrics for the market baseline (`mkt_prob`).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import numpy as np
import pandas as pd

from frameworks.common import load_dataset, evaluate, RACE_KEYS
from frameworks.framework_a_residual import FrameworkA
from frameworks.framework_b_pace import FrameworkB
from frameworks.framework_c_form import FrameworkC
from frameworks.framework_d_connections import FrameworkD


def race_id_series(df: pd.DataFrame) -> pd.Series:
    return df["race_date"].astype(str) + "_" + df["race_number"].astype(str)


def run_walk_forward(start: str, end: str, fit_every: int = 14):
    ds = load_dataset()
    df = ds.df

    test_dates = sorted(df.loc[(df["race_date_dt"] >= pd.Timestamp(start)) &
                                (df["race_date_dt"] <= pd.Timestamp(end)),
                                "race_date_dt"].unique())
    print(f"Walk-forward over {len(test_dates)} race dates: {start} -> {end}")

    # Pre-fit Framework A periodically (it's the only one with sklearn fitting).
    a_model: FrameworkA | None = None
    last_fit_date: pd.Timestamp | None = None

    rows_a, rows_b, rows_c, rows_d, rows_mkt = [], [], [], [], []

    fb = FrameworkB()
    fc = FrameworkC()
    fd = FrameworkD()

    for i, day in enumerate(test_dates):
        day = pd.Timestamp(day)
        history = df[df["race_date_dt"] < day]
        runners = df[df["race_date_dt"] == day].copy()
        if runners.empty:
            continue
        # Filter to runners with a market prob (drop scratchings)
        runners = runners[runners["mkt_prob"].notna() & runners["completed"]]
        if runners.empty:
            continue

        # ---- Framework A: refit every `fit_every` days
        if (a_model is None) or (last_fit_date is None) or ((day - last_fit_date).days >= fit_every):
            train = history[history["completed"] & history["mkt_prob"].notna()]
            # use most recent 6 months of completed races for training; older as priors
            cut = day - pd.Timedelta(days=180)
            train_recent = train[train["race_date_dt"] >= cut]
            if len(train_recent) >= 1000:
                a_model = FrameworkA().fit(train_recent, history)
                last_fit_date = day

        if a_model is not None:
            p_a = a_model.predict_proba(runners, history).values
        else:
            p_a = runners["mkt_prob"].values  # fallback: cold-start uses market

        p_b = fb.predict_proba(runners, history).values
        p_c = fc.predict_proba(runners, history).values
        p_d = fd.predict_proba(runners, history).values

        y = runners["won"].values
        rid = race_id_series(runners).values
        mkt = runners["mkt_prob"].values

        rows_a.append((p_a, y, rid, mkt))
        rows_b.append((p_b, y, rid, mkt))
        rows_c.append((p_c, y, rid, mkt))
        rows_d.append((p_d, y, rid, mkt))
        rows_mkt.append((mkt, y, rid))

        if (i + 1) % 10 == 0 or i == len(test_dates) - 1:
            print(f"  [{i+1}/{len(test_dates)}] {day.date()}  "
                  f"runners={len(runners)}  history={len(history)}")

    def _stack(rows):
        p = np.concatenate([r[0] for r in rows])
        y = np.concatenate([r[1] for r in rows])
        rid = np.concatenate([r[2] for r in rows])
        mkt = np.concatenate([r[3] for r in rows]) if len(rows[0]) == 4 else None
        return p, y, rid, mkt

    pa, y, rid, mkt = _stack(rows_a)
    pb, *_ = _stack(rows_b)
    pc, *_ = _stack(rows_c)
    pd_, *_ = _stack(rows_d)

    results = []
    results.append(evaluate("Market (baseline)", mkt, y, rid))
    results.append(evaluate("A: Market-residual", pa, y, rid, mkt_p=mkt))
    results.append(evaluate("B: Pace-tactics", pb, y, rid, mkt_p=mkt))
    results.append(evaluate("C: Form-quality", pc, y, rid, mkt_p=mkt))
    results.append(evaluate("D: Connections", pd_, y, rid, mkt_p=mkt))

    out_df = pd.DataFrame(results)
    return out_df, {
        "Market": (mkt, y, rid),
        "A": (pa, y, rid),
        "B": (pb, y, rid),
        "C": (pc, y, rid),
        "D": (pd_, y, rid),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-05-06")
    ap.add_argument("--fit-every", type=int, default=14)
    ap.add_argument("--out", default=None, help="optional JSON file to write")
    args = ap.parse_args()

    t0 = time.time()
    out_df, probs = run_walk_forward(args.start, args.end, fit_every=args.fit_every)
    elapsed = time.time() - t0
    print()
    print("=" * 88)
    print(f"WALK-FORWARD RESULTS  {args.start} -> {args.end}   (elapsed {elapsed:.1f}s)")
    print("=" * 88)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    print(out_df.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print()

    # Coverage / agreement (top-1 picks)
    df_top = pd.DataFrame()
    for k, (p, y, rid) in probs.items():
        s = pd.DataFrame({"p": p, "y": y, "rid": rid})
        s["rank"] = s.groupby("rid")["p"].rank(method="first", ascending=False)
        top1_y = s[s["rank"] == 1].groupby("rid")["y"].first().rename(k + "_won")
        df_top = pd.concat([df_top, top1_y], axis=1) if not df_top.empty else top1_y.to_frame()
    print("Top-1 strike rate by framework:")
    print((df_top.mean()).to_string())
    print()

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(out_df.to_json(orient="records", indent=2))
        print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
