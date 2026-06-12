"""
fuse_raceday.py — FUSE production inference for a race meeting
================================================================

Loads the racecard, appends it to the cleaned DB history (outcomes NaN),
recomputes the leak-free feature table (train/serve parity), trains the
fund + mkt LightGBM heads on everything strictly before the meeting date,
anchors on the latest live odds when available, and writes
reports/race_day_report_{DC}_FUSE.json.

Final probability:
    odds available : log-pool( mkt-variant, market-implied ; w = 1, 2 )
    no odds        : fund variant only

CLI
---
  python fuse_raceday.py --date 20260613 [--going G] [--going-awt GD]
  python fuse_raceday.py --backfill            # all racecards with results
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from fuse_model import (
    BASE, REPORTS, COURSE_MAP, FUND_FEATURES,
    _clean_jockey, _norm_class, _norm_going,
    add_features, load_history, log_pool, pair_probs, predict_meeting,
    train_models,
)

RACECARDS = BASE / "racecards"
LIVE_ODDS = BASE / "cache" / "live_odds"
HKT = timezone(timedelta(hours=8))

FUSE_LAMBDA = 0.60      # Henery discount (fitted 0.55–0.65 across windows)
W_MARKET = 2.0          # market anchor weight (validation-fitted Jan 2026)


# ---------------------------------------------------------------------------
# Racecard → history-compatible rows
# ---------------------------------------------------------------------------
def load_card(dc: str, going_turf: str = "G", going_awt: str = "GD"):
    """Return (card_rows_df, meta_df). card_rows matches clean_table schema."""
    p = RACECARDS / f"racecard_{dc}.xlsx"
    if not p.exists():
        raise FileNotFoundError(p)
    raw = pd.read_excel(p, sheet_name="All Races")
    # drop reserves: explicit standby flag or no jockey booked
    if "is_standby" in raw.columns:
        raw = raw[~raw["is_standby"].fillna(False).astype(bool)]
    raw = raw[raw["jockey"].notna() & (raw["jockey"].astype(str).str.strip() != "")]

    surface = raw.get("surface", pd.Series("", index=raw.index)).astype(str)
    is_awt = surface.str.contains("all weather|awt", case=False, na=False)

    out = pd.DataFrame()
    out["horse_id"] = raw["horse_id"].astype(str)
    out["horse_name"] = raw["horse_name"].astype(str).str.upper().str.strip()
    out["race_date"] = pd.to_datetime(f"{dc[:4]}-{dc[4:6]}-{dc[6:]}")
    out["race_number"] = pd.to_numeric(raw["race_number"], errors="coerce")
    out["horse_no"] = pd.to_numeric(raw["horse_no"], errors="coerce")
    out["place_num"] = np.nan
    out["fin_t"] = np.nan
    out["draw"] = pd.to_numeric(raw["draw"], errors="coerce")
    out["going_ord"] = np.where(is_awt, _norm_going(going_awt),
                                _norm_going(going_turf))
    out["first_pos"] = np.nan
    out["last_sec"] = np.nan
    gear = raw.get("gear", pd.Series("", index=raw.index)).astype(str)
    out["gear_first"] = gear.str.contains("1", regex=False).astype(float)
    out.loc[gear.isin(["None", "nan", "", "--", "-"]), "gear_first"] = 0.0
    out["rating"] = pd.to_numeric(raw.get("rating"), errors="coerce")
    out["dist"] = pd.to_numeric(raw["distance"], errors="coerce")
    out["jockey"] = raw["jockey"].map(_clean_jockey)
    out["trainer"] = raw["trainer"].astype(str).str.strip()
    out["act_wt"] = pd.to_numeric(raw.get("weight"), errors="coerce")
    out["dec_wt"] = pd.to_numeric(raw.get("horse_wt_declaration"),
                                  errors="coerce")
    out["win_odds"] = np.nan          # filled from live odds later
    rc = raw.get("race_course", pd.Series("", index=raw.index)).astype(str)
    out["course_i"] = [
        COURSE_MAP.get("AWT" if a else str(c).strip().upper()
                       .replace('"', "").replace("COURSE", "").strip(), -1)
        for c, a in zip(rc, is_awt)]
    out["class_num"] = raw["race_class"].map(_norm_class)
    out["venue_i"] = (raw["racecourse"].astype(str).str.strip()
                      .str.upper() == "HV").astype(int)
    out["surface_i"] = is_awt.astype(int)
    out["won"] = np.nan
    out["top2"] = np.nan
    out["top3"] = np.nan

    meta_cols = {}
    for c in ("race_class", "distance", "racecourse", "race_time", "surface"):
        if c in raw.columns:
            meta_cols[c] = raw[c]
    meta = pd.DataFrame(meta_cols)
    meta["race_number"] = pd.to_numeric(raw["race_number"], errors="coerce")
    meta = meta.drop_duplicates("race_number").set_index("race_number")
    return out.reset_index(drop=True), meta


# ---------------------------------------------------------------------------
# Live odds
# ---------------------------------------------------------------------------
def latest_live_odds(dc: str) -> dict[int, dict[int, float]]:
    """{race_number: {horse_no: win_odds}} from newest snapshot per race."""
    d = LIVE_ODDS / dc
    out: dict[int, dict[int, float]] = {}
    if not d.exists():
        return out
    files: dict[int, Path] = {}
    for f in d.glob("*_R*_*.json"):
        m = re.search(r"_R(\d+)_(\d+)\.json$", f.name)
        if not m:
            continue
        rn = int(m.group(1))
        if rn not in files or f.name > files[rn].name:
            files[rn] = f
    for rn, f in files.items():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            o = {}
            for e in data.get("odds", []):
                try:
                    v = float(e.get("win") or 0)
                    if v >= 1.01:
                        o[int(e["no"])] = v
                except (TypeError, ValueError):
                    continue
            if o:
                out[rn] = o
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def generate_fuse_report(dc: str, going_turf: str = "G", going_awt: str = "GD",
                         quiet: bool = False) -> Path:
    def say(msg):
        if not quiet:
            print(msg)

    t0 = datetime.now()
    date = pd.Timestamp(f"{dc[:4]}-{dc[4:6]}-{dc[6:]}")
    card, meta = load_card(dc, going_turf, going_awt)
    say(f"[fuse] card {dc}: {card['race_number'].nunique()} races, "
        f"{len(card)} runners")

    odds_map = latest_live_odds(dc)
    n_odds = 0
    for i in card.index:
        rn, hn = int(card.at[i, "race_number"]), card.at[i, "horse_no"]
        if rn in odds_map and not pd.isna(hn) and int(hn) in odds_map[rn]:
            card.at[i, "win_odds"] = odds_map[rn][int(hn)]
            n_odds += 1
    say(f"[fuse] live odds attached: {n_odds}/{len(card)} runners "
        f"({len(odds_map)} races)")

    hist = load_history()
    hist = hist[hist["race_date"] != date]          # honest pre-race view
    feat = add_features(pd.concat([hist, card], ignore_index=True))
    models = train_models(feat, date)
    say(f"[fuse] trained on {models['n_train']} rows < {models['cutoff']} "
        f"({(datetime.now()-t0).total_seconds():.0f}s)")

    day = predict_meeting(models, feat, date)
    use_market = day["win_odds"].notna().any()

    races_out = []
    for rn, race in day.groupby("race_number"):
        rn = int(rn)
        race = race.reset_index(drop=True)
        p_fund = race["p_fund_win"].to_numpy(dtype=float)
        p_fund = p_fund / p_fund.sum()
        p_mkt = race["p_mkt_win"].to_numpy(dtype=float)
        inv = race["sp_implied"].to_numpy(dtype=float)
        have_odds = use_market and not np.isnan(inv).any() and not np.isnan(p_mkt).any()
        if have_odds:
            p_win = log_pool([p_mkt, inv], [1.0, W_MARKET])
            p_t2 = race["p_mkt_top2"].to_numpy(dtype=float)
            p_t3 = race["p_mkt_top3"].to_numpy(dtype=float)
        else:
            p_win = p_fund
            p_t2 = race["p_fund_top2"].to_numpy(dtype=float)
            p_t3 = race["p_fund_top3"].to_numpy(dtype=float)

        order = np.argsort(-p_win)
        picks = []
        for rank, ix in enumerate(order, 1):
            r = race.iloc[ix]
            picks.append({
                "rank": rank,
                "horse_no": int(r["horse_no"]) if not pd.isna(r["horse_no"]) else None,
                "horse_name": r["horse_name"],
                "draw": int(r["draw"]) if not pd.isna(r["draw"]) else None,
                "jockey": r["jockey"], "trainer": r["trainer"],
                "p_win": round(float(p_win[ix]), 4),
                "p_top2": round(float(p_t2[ix]), 4) if not np.isnan(p_t2[ix]) else None,
                "p_top3": round(float(p_t3[ix]), 4) if not np.isnan(p_t3[ix]) else None,
                "p_fund": round(float(p_fund[ix]), 4),
                "p_market": round(float(inv[ix]), 4) if not np.isnan(inv[ix]) else None,
                "edge": round(float(p_win[ix] - inv[ix]), 4)
                        if not np.isnan(inv[ix]) else None,
                "win_odds": round(float(r["win_odds"]), 2)
                            if not pd.isna(r["win_odds"]) else None,
            })

        # quinella head
        pp = pair_probs(p_win, FUSE_LAMBDA)
        hn = race["horse_no"].to_numpy()
        top_pairs = sorted(pp.items(), key=lambda kv: -kv[1])[:6]
        q_out = [{
            "a": int(hn[i]), "b": int(hn[j]), "p": round(float(v), 4),
            "fair_odds": round(0.825 / max(v, 1e-6), 1),
        } for (i, j), v in top_pairs if not (pd.isna(hn[i]) or pd.isna(hn[j]))]

        m = meta.loc[rn] if rn in meta.index else {}
        races_out.append({
            "race_number": rn,
            "venue": str(m.get("racecourse", "")),
            "race_class": str(m.get("race_class", "")),
            "distance": int(m["distance"]) if "distance" in m and not pd.isna(m.get("distance")) else None,
            "race_time": str(m.get("race_time", "")),
            "field_size": int(len(race)),
            "odds_anchored": bool(have_odds),
            "picks": picks,
            "quinella_pairs": q_out,
        })

    report = {
        "model": "FUSE v1",
        "date": f"{dc[:4]}-{dc[4:6]}-{dc[6:]}",
        "date_compact": dc,
        "generated_at_hkt": datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S"),
        "lambda": FUSE_LAMBDA,
        "w_market": W_MARKET,
        "going_turf": going_turf,
        "going_awt": going_awt,
        "odds_mode": "live" if odds_map else "none",
        "n_train": models["n_train"],
        "races": races_out,
    }
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"race_day_report_{dc}_FUSE.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    say(f"[fuse] report written → {out.name} "
        f"({(datetime.now()-t0).total_seconds():.0f}s total)")
    return out


def backfill_all(force: bool = False):
    dcs = sorted(re.search(r"racecard_(\d{8})", p.name).group(1)
                 for p in RACECARDS.glob("racecard_*.xlsx"))
    for dc in dcs:
        out = REPORTS / f"race_day_report_{dc}_FUSE.json"
        if out.exists() and not force:
            print(f"[fuse] {dc}: exists, skip")
            continue
        try:
            generate_fuse_report(dc, quiet=True)
            print(f"[fuse] {dc}: done")
        except Exception as e:
            print(f"[fuse] {dc}: FAILED — {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="meeting date YYYYMMDD")
    ap.add_argument("--going", default="G", help="turf going code")
    ap.add_argument("--going-awt", default="GD", help="AWT going code")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.backfill:
        backfill_all(force=a.force)
    elif a.date:
        generate_fuse_report(a.date, a.going, a.going_awt)
    else:
        ap.error("--date or --backfill required")
