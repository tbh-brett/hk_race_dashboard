"""
train_gbm.py
============
Gradient-boosted model that learns to predict P(horse wins) from the
existing v4.4 model's intermediate features + race context.

Why this exists
---------------
Our handcrafted v4.4 model produces a numeric `win_prob` from a
hand-tuned blend of projected_time, early_speed_z, smap adjustments,
draw, weight, etc. The Calibration Lab showed BrierSkill = -3.11% vs
the Shin market — i.e. the human-tuned weights are slightly worse
than what the betting public collectively chooses. A gradient-boosted
machine can re-learn those weights from data: same features, no manual
tuning, regularised by cross-validation. The hypothesis is that some
features matter more (or less) than the v4.4 weights assume, and a
GBM will find that out.

What it does
------------
1. Walk every reports/race_day_report_<date>_v4.4.json and pull each
   pick's numeric features.
2. Join to reports/results_<date>.json to get the realised winner
   (place == 1).
3. Train a LightGBM binary classifier with race-grouped CV
   (GroupKFold over (date, race_no)).
4. Out-of-fold predictions are used for calibration metrics.
5. A final model is re-fit on ALL rows and written to
   models/gbm_v1.txt; OOF preds + feature importance + metrics
   written to reports/gbm_training.json + reports/GBM_TRAINING.md.

Inference
---------
score_report(date_compact, version) loads a report and returns
{(race_no, horse_no): p_gbm} normalised so each race sums to 1.

Usage
-----
    python train_gbm.py                      # train + cv + write outputs
    python train_gbm.py --score 20260429     # score a specific meeting
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import GroupKFold

from backtest_market import BASE, REPORTS, list_dates, load_results


def list_dates_with_reports(d_from, d_to, version: str = "v4.4") -> list[str]:
    """Like backtest_market.list_dates but driven by available
    race_day_report_<date>_<version>.json files instead of results files.
    Lets us score upcoming meetings that don't yet have results.
    """
    out = set()
    # any date with results
    for d in list_dates(d_from, d_to):
        out.add(d)
    # any date with a v4.4 race day report
    import re as _re
    pat = _re.compile(rf"race_day_report_(\d{{8}})_{_re.escape(version)}\.json$")
    for f in REPORTS.glob(f"race_day_report_*_{version}.json"):
        m = pat.search(f.name)
        if not m:
            continue
        d = m.group(1)
        if d_from and d < d_from:
            continue
        if d_to and d > d_to:
            continue
        out.add(d)
    return sorted(out)

MODELS = BASE / "models"
MODELS.mkdir(parents=True, exist_ok=True)
MODEL_FILE = MODELS / "gbm_v1.txt"
META_FILE = MODELS / "gbm_v1_meta.json"
OOF_FILE = REPORTS / "gbm_oof.json"
OUT_JSON = REPORTS / "gbm_training.json"
OUT_MD = REPORTS / "GBM_TRAINING.md"

# ---- Feature schema -------------------------------------------------------
NUM_FEATURES = [
    # per-horse model intermediates
    "projected_time", "proj_pre_pace", "proj_final_sec",
    "win_prob",                       # the v4.4 scalar — let GBM re-weight
    "risk_score", "effective_resid",
    "early_speed_z",
    "smap_pos_adj", "smap_pps_adj", "smap_total_adj", "sec_total_adj",
    "avg_late_dev", "avg_ssi", "late_std",
    "n_sec_profile",
    "draw", "weight",
    # race context
    "distance", "pace_score", "field_size", "is_awt_int",
    # rank-derived (computed within race)
    "rank_winprob", "rank_proj_time", "rank_esz",
    "winprob_z", "proj_time_z", "esz_z",
]
CAT_FEATURES = ["style", "sec_type", "race_class", "race_course",
                "risk_tier"]
ALL_FEATURES = NUM_FEATURES + CAT_FEATURES


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _to_int(x):
    try:
        return int(str(x).strip())
    except (TypeError, ValueError):
        return None


def _zscore(values: list[float]) -> list[float]:
    arr = np.array(values, dtype=float)
    mask = ~np.isnan(arr)
    if mask.sum() < 2:
        return [0.0] * len(values)
    mu, sd = arr[mask].mean(), arr[mask].std(ddof=0)
    if sd == 0 or math.isnan(sd):
        return [0.0] * len(values)
    out = []
    for v in values:
        if math.isnan(v):
            out.append(0.0)
        else:
            out.append((v - mu) / sd)
    return out


def _rank(values: list[float], descending: bool = True) -> list[int]:
    """1 = best. NaNs go last."""
    indexed = list(enumerate(values))
    indexed.sort(key=lambda t: (math.isnan(t[1]),
                                -t[1] if descending else t[1]))
    ranks = [0] * len(values)
    for rk, (i, _) in enumerate(indexed, start=1):
        ranks[i] = rk
    return ranks


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------
def build_dataset(d_from: str | None = None, d_to: str | None = None,
                  version: str = "v4.4",
                  require_results: bool = True) -> pd.DataFrame:
    """Build a feature DataFrame.

    require_results=True (training): only emit rows for which we have a
      realised winner (place == 1) so the GBM can supervise on `won`.
    require_results=False (inference on upcoming meeting): emit every
      pick from the report. `won` is set to 0 as a placeholder; do NOT
      use those rows for metric computation.
    """
    rows = []
    for d in list_dates_with_reports(d_from, d_to, version):
        rep_path = REPORTS / f"race_day_report_{d}_{version}.json"
        if not rep_path.exists():
            continue
        try:
            rep = json.loads(rep_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        runners = load_results(d)
        winners = {(r.race_no, r.horse_no): (r.place == 1) for r in runners}
        sps = {(r.race_no, r.horse_no): r.win_odds for r in runners}
        n_runners_by_race = {r.race_no: r.n_runners for r in runners}
        if require_results and not winners:
            continue
        for race in rep.get("races", []):
            rn = _to_int(race.get("race_number"))
            if rn is None:
                continue
            picks = race.get("picks") or []
            if not picks:
                continue
            # race-level
            distance = _to_float(race.get("distance"))
            pace_score = _to_float(race.get("pace_score"))
            is_awt_int = 1 if race.get("is_awt") else 0
            race_class = str(race.get("race_class") or "")
            race_course = str(race.get("race_course") or "")
            field_size = n_runners_by_race.get(rn, len(picks))

            # within-race derived features
            wps = [_to_float(p.get("win_prob")) for p in picks]
            pts = [_to_float(p.get("projected_time")) for p in picks]
            esz = [_to_float(p.get("early_speed_z")) for p in picks]
            rk_wp = _rank(wps, descending=True)
            rk_pt = _rank(pts, descending=False)
            rk_es = _rank(esz, descending=True)
            wp_z = _zscore(wps)
            pt_z = _zscore(pts)
            es_z = _zscore(esz)

            # scale detection for v4.4 percent storage
            tot_wp = sum(w for w in wps if not math.isnan(w))
            wp_scale = 0.01 if tot_wp > 5 else 1.0

            for i, p in enumerate(picks):
                hn = _to_int(p.get("horse_no"))
                if hn is None:
                    continue
                key = (rn, hn)
                if require_results and key not in winners:
                    continue
                row = {
                    "date": d,
                    "race_no": rn,
                    "horse_no": hn,
                    "horse_name": p.get("horse_name"),
                    "won": int(winners.get(key, False)),
                    "win_odds": sps.get(key, float("nan")),
                    # raw numerics
                    "projected_time":  _to_float(p.get("projected_time")),
                    "proj_pre_pace":   _to_float(p.get("proj_pre_pace")),
                    "proj_final_sec":  _to_float(p.get("proj_final_sec")),
                    "win_prob":        _to_float(p.get("win_prob")) * wp_scale,
                    "risk_score":      _to_float(p.get("risk_score")),
                    "effective_resid": _to_float(p.get("effective_resid")),
                    "early_speed_z":   _to_float(p.get("early_speed_z")),
                    "smap_pos_adj":    _to_float(p.get("smap_pos_adj")),
                    "smap_pps_adj":    _to_float(p.get("smap_pps_adj")),
                    "smap_total_adj":  _to_float(p.get("smap_total_adj")),
                    "sec_total_adj":   _to_float(p.get("sec_total_adj")),
                    "avg_late_dev":    _to_float(p.get("avg_late_dev")),
                    "avg_ssi":         _to_float(p.get("avg_ssi")),
                    "late_std":        _to_float(p.get("late_std")),
                    "n_sec_profile":   _to_float(p.get("n_sec_profile")),
                    "draw":            _to_float(p.get("draw")),
                    "weight":          _to_float(p.get("weight")),
                    "distance":        distance,
                    "pace_score":      pace_score,
                    "field_size":      field_size,
                    "is_awt_int":      is_awt_int,
                    "rank_winprob":    rk_wp[i],
                    "rank_proj_time":  rk_pt[i],
                    "rank_esz":        rk_es[i],
                    "winprob_z":       wp_z[i],
                    "proj_time_z":     pt_z[i],
                    "esz_z":           es_z[i],
                    "style":           str(p.get("style") or ""),
                    "sec_type":        str(p.get("sec_type") or ""),
                    "race_class":      race_class,
                    "race_course":     race_course,
                    "risk_tier":       str(p.get("risk_tier") or ""),
                }
                rows.append(row)
    df = pd.DataFrame(rows)
    return df


def _prep_features(df: pd.DataFrame) -> pd.DataFrame:
    X = df[ALL_FEATURES].copy()
    for c in CAT_FEATURES:
        X[c] = X[c].astype("category")
    return X


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------
LGB_PARAMS = dict(
    objective="binary",
    metric="binary_logloss",
    learning_rate=0.05,
    num_leaves=15,
    min_data_in_leaf=20,
    feature_fraction=0.9,
    bagging_fraction=0.9,
    bagging_freq=5,
    lambda_l2=1.0,
    verbosity=-1,
)


def _normalize_within_race(df_pred: pd.DataFrame,
                            col: str = "p_raw") -> np.ndarray:
    """Normalise raw probabilities so each (date, race_no) sums to 1."""
    out = df_pred[col].astype(float).to_numpy().copy()
    for (_, _), idx in df_pred.groupby(["date", "race_no"]).groups.items():
        ix = list(idx)
        s = out[ix].sum()
        if s > 0:
            out[ix] = out[ix] / s
    return out


def cross_validate(df: pd.DataFrame, n_splits: int = 5, seed: int = 42
                   ) -> tuple[np.ndarray, dict]:
    """GroupKFold over date; returns OOF probabilities + per-fold metrics."""
    X = _prep_features(df)
    y = df["won"].to_numpy()
    groups = df["date"].astype(str).to_numpy()

    n_groups = len(np.unique(groups))
    n_splits = max(2, min(n_splits, n_groups))
    gkf = GroupKFold(n_splits=n_splits)
    oof = np.zeros(len(df), dtype=float)
    fold_metrics = []
    for fold, (tr, va) in enumerate(gkf.split(X, y, groups), start=1):
        d_tr = lgb.Dataset(X.iloc[tr], y[tr],
                           categorical_feature=CAT_FEATURES)
        d_va = lgb.Dataset(X.iloc[va], y[va],
                           categorical_feature=CAT_FEATURES,
                           reference=d_tr)
        booster = lgb.train(
            LGB_PARAMS, d_tr,
            num_boost_round=600,
            valid_sets=[d_va], valid_names=["valid"],
            callbacks=[lgb.early_stopping(50, verbose=False),
                       lgb.log_evaluation(0)],
        )
        oof[va] = booster.predict(X.iloc[va], num_iteration=booster.best_iteration)
        fold_metrics.append({
            "fold": fold,
            "n_train": int(len(tr)),
            "n_valid": int(len(va)),
            "best_iter": booster.best_iteration,
            "valid_logloss": float(booster.best_score["valid"]["binary_logloss"]),
        })
    # Renormalise OOF preds within race so they're true win probs.
    pred_df = df[["date", "race_no"]].copy()
    pred_df["p_raw"] = oof
    p_norm = _normalize_within_race(pred_df, "p_raw")
    return p_norm, {"folds": fold_metrics, "n_splits": n_splits}


def train_final(df: pd.DataFrame) -> lgb.Booster:
    X = _prep_features(df)
    y = df["won"].to_numpy()
    # Use median best_iter from CV is ideal, but keep simple: train fixed.
    d_all = lgb.Dataset(X, y, categorical_feature=CAT_FEATURES)
    booster = lgb.train(
        LGB_PARAMS, d_all,
        num_boost_round=400,
        callbacks=[lgb.log_evaluation(0)],
    )
    return booster


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def _brier(p, y):
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p - y) ** 2))


def _log_loss(p, y, eps=1e-9):
    p = np.clip(np.asarray(p, float), eps, 1 - eps)
    y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _reliability(p, y, n_bins=10):
    df = pd.DataFrame({"p": p, "y": y}).sort_values("p").reset_index(drop=True)
    n = len(df)
    out = []
    step = max(1, n // n_bins)
    for b in range(n_bins):
        lo = b * step
        hi = n if b == n_bins - 1 else (b + 1) * step
        chunk = df.iloc[lo:hi]
        if len(chunk) == 0:
            continue
        out.append({
            "bin": b + 1,
            "n": int(len(chunk)),
            "p_mean": float(chunk["p"].mean()),
            "win_rate": float(chunk["y"].mean()),
            "diff": float(chunk["y"].mean() - chunk["p"].mean()),
        })
    return out


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------
def score_report(date_compact: str, version: str = "v4.4"
                 ) -> dict[tuple[int, int], float]:
    """Return {(race_no, horse_no): p_gbm} for one meeting.

    Works for upcoming meetings (no results required). Returns {} if
    the model file or race_day_report doesn't exist.
    """
    if not MODEL_FILE.exists():
        return {}
    df = build_dataset(date_compact, date_compact, version=version,
                        require_results=False)
    if df.empty:
        return {}
    booster = lgb.Booster(model_file=str(MODEL_FILE))
    X = _prep_features(df)
    df = df.copy()
    df["p_raw"] = booster.predict(X)
    df["p_gbm"] = _normalize_within_race(df, "p_raw")
    return {(int(r.race_no), int(r.horse_no)): float(r.p_gbm)
            for r in df.itertuples()}


# ---------------------------------------------------------------------------
# Top-line training entry
# ---------------------------------------------------------------------------
def run(d_from: str | None = None, d_to: str | None = None,
        version: str = "v4.4", n_splits: int = 5) -> dict:
    df = build_dataset(d_from, d_to, version=version)
    if df.empty:
        out = {"n_rows": 0}
        OUT_JSON.write_text(json.dumps(out, indent=2), encoding="utf-8")
        OUT_MD.write_text("# GBM Training\n\n_No data available._",
                          encoding="utf-8")
        return out

    oof, cv_meta = cross_validate(df, n_splits=n_splits)
    df = df.copy()
    df["p_gbm_oof"] = oof

    # Persist OOF preds keyed by (date, race_no, horse_no) so other tools
    # (e.g. calibration_harness) can use HONEST out-of-fold scores rather
    # than the leaked in-sample model output.
    oof_payload = {
        f"{r.date}_{int(r.race_no)}_{int(r.horse_no)}": float(r.p_gbm_oof)
        for r in df.itertuples()
    }
    OOF_FILE.write_text(json.dumps(oof_payload), encoding="utf-8")

    # Compare against v4.4 baseline (win_prob col, already in [0,1])
    df["p_v44"] = df["win_prob"].fillna(0.0)

    metrics = {
        "p_gbm":  {"brier": _brier(df["p_gbm_oof"], df["won"]),
                   "log_loss": _log_loss(df["p_gbm_oof"], df["won"]),
                   "bins": _reliability(df["p_gbm_oof"], df["won"])},
        "p_v44":  {"brier": _brier(df["p_v44"], df["won"]),
                   "log_loss": _log_loss(df["p_v44"], df["won"]),
                   "bins": _reliability(df["p_v44"], df["won"])},
    }
    metrics["p_gbm"]["brier_skill_vs_v44"] = (
        1 - metrics["p_gbm"]["brier"] / metrics["p_v44"]["brier"]
        if metrics["p_v44"]["brier"] else None)
    metrics["p_gbm"]["logloss_lift_vs_v44"] = (
        metrics["p_v44"]["log_loss"] - metrics["p_gbm"]["log_loss"])

    # Final fit + save
    booster = train_final(df)
    booster.save_model(str(MODEL_FILE))
    META_FILE.write_text(json.dumps({
        "version": "gbm_v1",
        "trained_on_date_min": str(df["date"].min()),
        "trained_on_date_max": str(df["date"].max()),
        "n_meetings": int(df["date"].nunique()),
        "n_races": int(df.groupby(["date", "race_no"]).ngroups),
        "n_rows": int(len(df)),
        "features_num": NUM_FEATURES,
        "features_cat": CAT_FEATURES,
        "lgb_params": LGB_PARAMS,
    }, indent=2), encoding="utf-8")

    # Feature importance
    imp_gain = booster.feature_importance(importance_type="gain")
    imp_split = booster.feature_importance(importance_type="split")
    imp = sorted(
        [{"feature": f, "gain": float(g), "split": int(s)}
         for f, g, s in zip(booster.feature_name(), imp_gain, imp_split)],
        key=lambda r: -r["gain"],
    )

    summary = {
        "version": "gbm_v1",
        "n_rows": int(len(df)),
        "n_meetings": int(df["date"].nunique()),
        "n_races": int(df.groupby(["date", "race_no"]).ngroups),
        "date_min": str(df["date"].min()),
        "date_max": str(df["date"].max()),
        "cv": cv_meta,
        "metrics": metrics,
        "feature_importance": imp,
        "model_file": str(MODEL_FILE.relative_to(BASE)),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(summary, indent=2, default=str),
                        encoding="utf-8")
    OUT_MD.write_text(_render_md(summary), encoding="utf-8")
    return summary


def _render_md(s: dict) -> str:
    L = []
    a = L.append
    a("# Gradient-Boosted Model — Training Report\n")
    a(f"Range **{s['date_min']} → {s['date_max']}** · "
      f"meetings={s['n_meetings']} · races={s['n_races']} · "
      f"rows={s['n_rows']}\n")
    a("## Out-of-fold metrics (vs v4.4 handcrafted)\n")
    a("| Source | Brier | LogLoss | BrierSkill (vs v4.4) | LogLoss lift |")
    a("|--------|------:|--------:|---------------------:|-------------:|")
    g = s["metrics"]["p_gbm"]
    v = s["metrics"]["p_v44"]
    a(f"| p_v44  | {v['brier']:.4f} | {v['log_loss']:.4f} | (baseline) | (baseline) |")
    a(f"| p_gbm  | {g['brier']:.4f} | {g['log_loss']:.4f} | "
      f"{(g.get('brier_skill_vs_v44') or 0)*100:+.2f}% | "
      f"{(g.get('logloss_lift_vs_v44') or 0):+.4f} |\n")
    a("## Reliability — p_gbm (OOF)\n")
    a("| bin | n | p_mean | win_rate | gap |")
    a("|----:|--:|-------:|---------:|----:|")
    for b in g["bins"]:
        a(f"| {b['bin']} | {b['n']} | {b['p_mean']:.3f} | "
          f"{b['win_rate']:.3f} | {b['diff']:+.3f} |")
    a("")
    a("## Top-20 feature importance (gain)\n")
    a("| feature | gain | split |")
    a("|---------|-----:|------:|")
    for r in s["feature_importance"][:20]:
        a(f"| {r['feature']} | {r['gain']:.0f} | {r['split']} |")
    a("")
    a("## Interpretation\n")
    a("- **BrierSkill > 0** = the GBM beats the handcrafted v4.4 model.")
    a("- The reliability table shows whether the GBM's calibrated buckets")
    a("  match observed win rates. Diagonal-ish columns = well calibrated.")
    a("- Feature importance reveals which inputs the GBM thinks carry the")
    a("  most signal. Compare to the v4.4 weights — if `winprob_z` is on")
    a("  top, the GBM is mostly trusting the v4.4 score; if other features")
    a("  dominate, the GBM has found a re-weighting that helps.")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", default=None)
    ap.add_argument("--to", dest="d_to", default=None)
    ap.add_argument("--version", default="v4.4")
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--score", default=None,
                    help="Score a single meeting (YYYYMMDD) and print results.")
    args = ap.parse_args()
    if args.score:
        out = score_report(args.score.replace("-", ""))
        if not out:
            print("No score (model missing or no data).")
            return
        print(f"Scored {len(out)} (race,horse) pairs from {args.score}:")
        for k, v in sorted(out.items()):
            print(f"  R{k[0]} H{k[1]:>2}: {v*100:5.2f}%")
        return
    d_from = args.d_from.replace("-", "") if args.d_from else None
    d_to = args.d_to.replace("-", "") if args.d_to else None
    s = run(d_from, d_to, version=args.version, n_splits=args.splits)
    if s.get("n_rows", 0) == 0:
        print("No rows.")
        return
    g = s["metrics"]["p_gbm"]
    v = s["metrics"]["p_v44"]
    print(f"\nGBM training: {s['date_min']} → {s['date_max']}  "
          f"({s['n_meetings']} mtg, {s['n_races']} races, {s['n_rows']} rows)")
    print(f"  p_v44   brier={v['brier']:.4f}  logloss={v['log_loss']:.4f}")
    print(f"  p_gbm   brier={g['brier']:.4f}  logloss={g['log_loss']:.4f}  "
          f"skill={(g.get('brier_skill_vs_v44') or 0)*100:+.2f}%  "
          f"ll_lift={(g.get('logloss_lift_vs_v44') or 0):+.4f}")
    print("\n  Top features:")
    for r in s["feature_importance"][:8]:
        print(f"    {r['feature']:<18} gain={r['gain']:>10.0f}  split={r['split']}")
    print(f"\nWrote {OUT_JSON}")
    print(f"Wrote {OUT_MD}")
    print(f"Saved {MODEL_FILE}")


if __name__ == "__main__":
    main()
