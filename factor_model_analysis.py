"""
factor_model_analysis.py
------------------------
Comprehensive, INDEPENDENT analytical model for HKJC race outcomes.

This is *not* a time/relativity projection. It scans the full results
history (hkjc_results_updated.xlsx) for correlations and impact of:
  - Jockey, Trainer, Jockey x Trainer
  - Race course (ST/HV), Distance, Track/Going
  - Pedigree: Sire, Dam sire
  - Horse profile: age, sex, country, import type
  - Gear changes
  - Horse form (rolling last-3 / last-5 placings, days since run)
  - Ratings (current, last, delta), Class
  - Weight: actual_weight, declared_weight, weight change between runs
  - Draw x Distance x Course
  - Odds (market efficiency / A/E)
  - Combinations (multi-factor)

Outputs:
  - console + reports/factor_analysis_report.md
  - reports/factor_analysis_tables.json (machine-readable for dashboard)

Metrics used
------------
  N             sample size (runs)
  Win%          wins / N
  Plc%          top-3 / N
  IV            Impact Value = Win% / baseline_win_rate_in_same_races
  A/E           Actual wins / Expected wins (expected = sum(1/odds_fair))
  ROI           P&L on flat $1 win bets (mean of payoff - 1)

Baseline win rate is computed race-by-race (1 / field_size) so IV is
field-size adjusted. A/E uses market-implied probability (1/odds with
overround removed per race).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

WORKSPACE = Path(__file__).parent
SRC_XLSX = WORKSPACE / "hkjc_results_updated.xlsx"
REPORTS = WORKSPACE / "reports"
REPORTS.mkdir(exist_ok=True)

# -------------------------------------------------------------------
# Load
# -------------------------------------------------------------------
def load() -> pd.DataFrame:
    tmp = Path(tempfile.gettempdir()) / "hkjc_results.xlsx"
    shutil.copy2(SRC_XLSX, tmp)
    df = pd.read_excel(tmp)
    df["race_date"] = pd.to_datetime(df["race_date"])
    # place is mostly numeric, but includes WV / DISQ etc
    df["place_num"] = pd.to_numeric(df["place"], errors="coerce")
    df["win"] = (df["place_num"] == 1).astype(int)
    df["plc"] = (df["place_num"].between(1, 3)).astype(int)
    df["win_odds"] = pd.to_numeric(df["win_odds"], errors="coerce")

    # Age: recover from horse_id when missing (horse_id = HK_YYYY_xxxx → birth year)
    hid_year = df["horse_id"].astype(str).str.extract(r"HK_(\d{4})_")[0].astype(float)
    season_year = df["race_date"].dt.year + (df["race_date"].dt.month >= 9).astype(int)
    recovered_age = season_year - hid_year
    df["age_eff"] = df["age"].fillna(recovered_age)

    # Gear change flag (presence of digit in gear string implies "added/removed this run" per HKJC convention)
    df["gear_str"] = df["gear"].astype(str).replace({"nan": ""})
    df["gear_change"] = df["gear_str"].str.contains(r"[0-9\-]", regex=True).astype(int)

    # Field size
    fs = df.groupby(["race_date", "race_number"])["horse_id"].transform("count")
    df["field_size"] = fs
    df["baseline_win"] = 1.0 / fs

    # Draw bucket (1-3 inside, 4-8 mid, 9+ wide)
    draw = pd.to_numeric(df["draw"], errors="coerce")
    df["draw_num"] = draw
    df["draw_bucket"] = pd.cut(
        draw, bins=[0, 3, 8, 99], labels=["inside(1-3)", "mid(4-8)", "wide(9+)"]
    )

    # Distance bucket
    dist = pd.to_numeric(df["distance"], errors="coerce")
    df["dist_num"] = dist
    df["dist_bucket"] = pd.cut(
        dist, bins=[0, 1050, 1250, 1450, 1700, 2000, 9999],
        labels=["1000", "1200", "1400", "1600", "1800", "2000+"],
    )

    # Weight change = actual - last actual for same horse
    df = df.sort_values(["horse_id", "race_date", "race_number"]).reset_index(drop=True)
    df["prev_actual_wt"] = df.groupby("horse_id")["actual_weight"].shift(1)
    df["prev_decl_wt"] = df.groupby("horse_id")["declared_weight"].shift(1)
    df["decl_wt_chg"] = pd.to_numeric(df["declared_weight"], errors="coerce") - pd.to_numeric(df["prev_decl_wt"], errors="coerce")

    # Rating delta
    df["prev_rating"] = df.groupby("horse_id")["rating"].shift(1)
    df["rating_delta"] = pd.to_numeric(df["rating"], errors="coerce") - pd.to_numeric(df["prev_rating"], errors="coerce")

    # Days since last run
    df["prev_date"] = df.groupby("horse_id")["race_date"].shift(1)
    df["days_off"] = (df["race_date"] - df["prev_date"]).dt.days

    # Rolling last-3 strike rate / avg finish (shift(1) so no leakage)
    g = df.groupby("horse_id")
    df["last3_win"] = g["win"].shift(1).rolling(3, min_periods=1).mean().reset_index(level=0, drop=True)
    df["last3_plc"] = g["plc"].shift(1).rolling(3, min_periods=1).mean().reset_index(level=0, drop=True)
    df["last3_avg_plc_num"] = g["place_num"].shift(1).rolling(3, min_periods=1).mean().reset_index(level=0, drop=True)
    df["career_runs_pre"] = g.cumcount()

    # Market-implied prob (per-race overround-adjusted)
    df["imp_raw"] = 1.0 / df["win_odds"]
    or_sum = df.groupby(["race_date", "race_number"])["imp_raw"].transform("sum")
    df["imp_fair"] = df["imp_raw"] / or_sum

    return df


# -------------------------------------------------------------------
# Generic grouped analysis
# -------------------------------------------------------------------
def summarise(df: pd.DataFrame, by, min_n: int = 30, topk: int | None = 25) -> pd.DataFrame:
    """Return per-group Win%, Plc%, IV, A/E, ROI. `by` can be str or list."""
    if isinstance(by, str):
        by = [by]
    d = df.dropna(subset=["win_odds"]).copy()
    d = d[d["win_odds"] > 0]
    grp = d.groupby(by, dropna=False)
    out = grp.agg(
        N=("win", "size"),
        Wins=("win", "sum"),
        Win_pct=("win", "mean"),
        Plc_pct=("plc", "mean"),
        Base_win=("baseline_win", "mean"),
        Exp_mkt=("imp_fair", "sum"),
        ROI=("win_odds", lambda s: ((s.loc[d.loc[s.index, "win"] == 1]).sum() - len(s)) / len(s)),
    )
    out["IV"] = out["Win_pct"] / out["Base_win"]
    out["A_E"] = out["Wins"] / out["Exp_mkt"].replace(0, np.nan)
    out = out[out["N"] >= min_n].copy()
    out = out.sort_values("IV", ascending=False)
    if topk:
        out = out.head(topk)
    return out.round({"Win_pct": 4, "Plc_pct": 4, "Base_win": 4, "IV": 2, "A_E": 2, "ROI": 3, "Exp_mkt": 1})


def window(df: pd.DataFrame, days: int | None = None, season_start_month: int = 9) -> pd.DataFrame:
    if days is None:
        return df
    cutoff = df["race_date"].max() - pd.Timedelta(days=days)
    return df[df["race_date"] >= cutoff]


# -------------------------------------------------------------------
# Benchmark ML model
# -------------------------------------------------------------------
def fit_benchmark(df: pd.DataFrame):
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import log_loss, roc_auc_score
        from sklearn.preprocessing import OneHotEncoder
        from sklearn.compose import ColumnTransformer
        from sklearn.pipeline import Pipeline
        from sklearn.impute import SimpleImputer
    except Exception as e:
        return {"error": str(e)}

    d = df.copy()
    d = d[d["win_odds"].notna() & (d["win_odds"] > 0)]
    # Train on everything except last 4 race meetings; test on those
    test_dates = sorted(d["race_date"].unique())[-4:]
    tr = d[~d["race_date"].isin(test_dates)]
    te = d[d["race_date"].isin(test_dates)]

    cat_cols = ["jockey", "trainer", "sire", "dam_sire", "going", "track_type",
                "race_course", "dist_bucket", "draw_bucket", "sex", "country",
                "race_class"]
    num_cols = ["actual_weight", "declared_weight", "decl_wt_chg",
                "rating", "rating_delta", "days_off", "age_eff",
                "last3_win", "last3_plc", "last3_avg_plc_num", "career_runs_pre",
                "field_size", "draw_num", "dist_num", "gear_change"]

    tr = tr.copy(); te = te.copy()
    for c in cat_cols:
        tr[c] = tr[c].astype(str)
        te[c] = te[c].astype(str)
    for c in num_cols:
        tr[c] = pd.to_numeric(tr[c], errors="coerce")
        te[c] = pd.to_numeric(te[c], errors="coerce")

    pre = ColumnTransformer([
        ("num", SimpleImputer(strategy="median"), num_cols),
        ("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=15), cat_cols),
    ])
    pipe = Pipeline([("pre", pre), ("clf", GradientBoostingClassifier(
        n_estimators=300, max_depth=3, learning_rate=0.05, random_state=0))])

    Xtr, ytr = tr[cat_cols + num_cols], tr["win"].values
    Xte, yte = te[cat_cols + num_cols], te["win"].values
    pipe.fit(Xtr, ytr)
    p = pipe.predict_proba(Xte)[:, 1]

    # Normalise per race to a prob that sums to 1
    te = te.assign(p_raw=p)
    te["p_norm"] = te.groupby(["race_date", "race_number"])["p_raw"].transform(lambda s: s / s.sum())
    # Compare to market-implied fair prob
    mkt_ll = log_loss(yte, te["imp_fair"].clip(1e-6, 1 - 1e-6))
    mdl_ll = log_loss(yte, te["p_norm"].clip(1e-6, 1 - 1e-6))

    # Rank accuracy: did the top-picked horse win?
    per_race = te.groupby(["race_date", "race_number"])
    top1_mdl = per_race.apply(lambda g: int(g.loc[g["p_norm"].idxmax(), "win"] == 1), include_groups=False).mean()
    top1_mkt = per_race.apply(lambda g: int(g.loc[g["imp_fair"].idxmax(), "win"] == 1), include_groups=False).mean()

    auc_mdl = roc_auc_score(yte, te["p_norm"])
    auc_mkt = roc_auc_score(yte, te["imp_fair"])

    # Feature importance
    clf = pipe.named_steps["clf"]
    ohe = pipe.named_steps["pre"].named_transformers_["cat"]
    feat_names = num_cols + list(ohe.get_feature_names_out(cat_cols))
    imp = pd.Series(clf.feature_importances_, index=feat_names).sort_values(ascending=False)

    # Collapse one-hot importance back to parent feature
    def parent(name):
        for c in cat_cols:
            if name.startswith(c + "_"):
                return c
        return name
    imp_parent = imp.groupby(parent).sum().sort_values(ascending=False)

    return {
        "n_train": len(tr), "n_test": len(te),
        "logloss_model": mdl_ll, "logloss_market": mkt_ll,
        "auc_model": auc_mdl, "auc_market": auc_mkt,
        "top1_model": top1_mdl, "top1_market": top1_mkt,
        "feat_imp_top25": imp_parent.head(25).round(4).to_dict(),
    }


# -------------------------------------------------------------------
# Reporting helpers
# -------------------------------------------------------------------
def tbl(md_lines, title, df_: pd.DataFrame):
    md_lines.append(f"\n### {title}\n")
    if df_ is None or len(df_) == 0:
        md_lines.append("_no rows meet min-N threshold_\n")
        return
    md_lines.append(df_.to_markdown())
    md_lines.append("")


def main():
    df = load()
    last_date = df["race_date"].max()
    print(f"Loaded {len(df):,} rows, {df['race_date'].nunique()} meetings, latest={last_date.date()}")

    md = [f"# HKJC Factor-Analysis Report\n",
          f"Generated from `hkjc_results_updated.xlsx` — {len(df):,} starters, "
          f"{df.groupby(['race_date','race_number']).ngroups} races, "
          f"{df['race_date'].min().date()} → {last_date.date()}\n",
          "\nMetrics: **N** runs; **Win%**/**Plc%** strike rates; "
          "**IV** (Impact Value = Win% ÷ race-field baseline, >1.00 means over-performing); "
          "**A/E** (actual wins ÷ market-expected wins, >1.00 = market underestimates); "
          "**ROI** (flat $1 win-bet return).\n"]

    # Stratify by time window
    windows = {
        "all_time": df,
        "last_90d": window(df, 90),
        "current_season_25_26": df[df["race_date"] >= "2025-09-01"],
    }
    json_out: dict = {}

    for wname, dW in windows.items():
        md.append(f"\n---\n## Window: {wname}  (N={len(dW):,})\n")
        j_w: dict = {}

        # Single factors
        single = [
            ("jockey", 50),
            ("trainer", 50),
            ("sire", 30),
            ("dam_sire", 30),
            ("going", 100),
            ("track_type", 100),
            ("race_course", 100),
            ("dist_bucket", 100),
            ("draw_bucket", 100),
            ("race_class", 100),
            ("age_eff", 100),
            ("sex", 100),
            ("country", 100),
            ("import_type", 100),
        ]
        for col, minn in single:
            t = summarise(dW, col, min_n=minn, topk=20)
            tbl(md, f"Top 20 {col} by IV (min N={minn})", t)
            j_w[col] = t.reset_index().astype(str).to_dict(orient="records")

        # Gear change
        t = summarise(dW, "gear_change", min_n=100, topk=None)
        tbl(md, "Gear change this run (0=no, 1=yes)", t)
        j_w["gear_change"] = t.reset_index().astype(str).to_dict(orient="records")

        # Combinations
        combos = [
            (["jockey", "trainer"], 20),
            (["jockey", "race_course"], 40),
            (["jockey", "dist_bucket"], 30),
            (["trainer", "dist_bucket"], 30),
            (["sire", "going"], 25),
            (["sire", "dist_bucket"], 25),
            (["sire", "track_type"], 25),
            (["draw_bucket", "race_course", "dist_bucket"], 50),
            (["dist_bucket", "going"], 100),
        ]
        for cols, minn in combos:
            t = summarise(dW, cols, min_n=minn, topk=25)
            tbl(md, f"Top 25 {' × '.join(cols)} by IV (min N={minn})", t)
            j_w["_x_".join(cols)] = t.reset_index().astype(str).to_dict(orient="records")

        # Numeric buckets (rolling form etc)
        for col, bins, labels in [
            ("last3_win", [-0.01, 0.001, 0.33, 0.66, 1.01], ["0%", "1-33%", "34-66%", "67-100%"]),
            ("last3_plc", [-0.01, 0.33, 0.66, 1.01], ["0-33%", "34-66%", "67-100%"]),
            ("days_off", [-1, 14, 28, 56, 120, 9999], ["<=14", "15-28", "29-56", "57-120", "120+"]),
            ("rating_delta", [-99, -3, -1, 0, 1, 3, 99], ["<=-3", "-2/-1", "0", "+1", "+2/+3", "+3+"]),
            ("decl_wt_chg", [-999, -10, -3, 3, 10, 999], ["<=-10", "-9/-3", "±3", "+3/+10", "+10+"]),
            ("career_runs_pre", [-1, 0, 3, 10, 30, 9999], ["debut(0)", "1-3", "4-10", "11-30", "30+"]),
            ("draw_num", [0, 3, 6, 9, 12, 99], ["1-3", "4-6", "7-9", "10-12", "13+"]),
        ]:
            tmp = dW.copy()
            tmp[col + "_b"] = pd.cut(tmp[col], bins=bins, labels=labels)
            t = summarise(tmp, col + "_b", min_n=50, topk=None)
            tbl(md, f"Bucketed: {col}", t)
            j_w[col + "_bucket"] = t.reset_index().astype(str).to_dict(orient="records")

        json_out[wname] = j_w

    # Save factor tables first (so a benchmark crash doesn't lose them)
    md_path = REPORTS / "factor_analysis_report.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    json_path = REPORTS / "factor_analysis_tables.json"
    json_path.write_text(json.dumps(json_out, indent=2, default=str), encoding="utf-8")
    print(f"Saved factor tables: {md_path}")

    # Benchmark ML
    md.append("\n---\n## Benchmark ML model (Gradient Boosted, holdout = last 4 meetings)\n")
    try:
        bench = fit_benchmark(df)
    except Exception as e:
        bench = {"error": repr(e)}
    md.append("```json\n" + json.dumps(bench, indent=2, default=str) + "\n```\n")
    json_out["benchmark_model"] = bench
    md_path.write_text("\n".join(md), encoding="utf-8")
    json_path.write_text(json.dumps(json_out, indent=2, default=str), encoding="utf-8")
    print(f"\nFinal write:\n  {md_path}\n  {json_path}")


if __name__ == "__main__":
    main()
