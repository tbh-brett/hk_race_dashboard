"""Blackbook performance analysis — subsequent-run ROI by entry.

For each blackbook entry:
  - Find all runs in the master DB on/after `added_date` (and on/before
    `expiry_date` if set).
  - Compute hypothetical 1u flat-stake WIN, PLACE and "1u split between WIN+PLACE"
    using real HKJC dividends where available, falling back to SP * 0.825 for WIN
    and SP-derived approximation for PLACE.
  - Aggregate across the blackbook and cross-reference the user's actual
    bookie bet log to see whether the user is "using" the blackbook.

Outputs:
  reports/blackbook_performance.json      — per-horse summary + grand totals
  reports/blackbook_performance.csv       — flat table for the dashboard
  reports/blackbook_roi_chart.png         — cumulative ROI line chart
"""
from __future__ import annotations

import json, os, shutil, re
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import pandas as pd

BASE = Path(__file__).parent
BB_PATH = BASE / "blackbook.json"
DB_PATH = BASE / "hkjc_results_updated.xlsx"
DIVS_DIR = BASE / "reports"
USER_BETS = BASE / "reports" / "user_bets_log.jsonl"
OUT_JSON = BASE / "reports" / "blackbook_performance.json"
OUT_CSV  = BASE / "reports" / "blackbook_performance.csv"
OUT_PNG  = BASE / "reports" / "blackbook_roi_chart.png"


def _norm_date(v) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, str):
        s = v.strip()
        m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return None
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return None


def _load_db() -> pd.DataFrame:
    # v4.7: prefer sqlite mirror (~35x faster than xlsx).
    try:
        from db_utils import read_sqlite, SQLITE_FILE
    except ImportError:
        SQLITE_FILE = None
    if SQLITE_FILE is not None and Path(SQLITE_FILE).exists():
        df = read_sqlite()
    else:
        tmp = Path(os.environ["TEMP"]) / "_bb_db.xlsx"
        shutil.copy2(DB_PATH, tmp)
        df = pd.read_excel(tmp)
    df["race_date_norm"] = df["race_date"].apply(_norm_date)
    df = df[df["race_date_norm"].notna()].copy()
    df["horse_name_u"] = df["horse_name"].astype(str).str.upper().str.strip()
    df["place_num"] = pd.to_numeric(df["place"], errors="coerce")
    df["win_odds_num"] = pd.to_numeric(df["win_odds"], errors="coerce")
    df["horse_number_num"] = pd.to_numeric(df["horse_number"], errors="coerce")
    return df


def _load_dividends() -> dict:
    """date_compact -> {race_no: {pool: {frozenset(combo): div_per10}}}."""
    out: dict = {}
    for fp in DIVS_DIR.glob("dividends_*.json"):
        m = re.match(r"dividends_(\d{8})\.json", fp.name)
        if not m:
            continue
        d = m.group(1)
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Format: {"races": [{"race_number":1, "dividends":[{"pool":"WIN","combination":"4","dividend_per_10":54.0},...]}]}
        races = {}
        if isinstance(raw, dict) and "races" in raw:
            for r in raw["races"]:
                rn = int(r.get("race_number") or r.get("race") or 0)
                pmap: dict = defaultdict(dict)
                for e in r.get("dividends") or []:
                    pool = (e.get("pool") or "").upper()
                    combo_str = e.get("combination") or e.get("combo") or ""
                    div = e.get("dividend_per_10") or e.get("dividend") or e.get("div")
                    if not pool or div is None:
                        continue
                    try:
                        nums = [int(x.strip()) for x in str(combo_str).replace("/", ",").split(",") if x.strip()]
                        if not nums:
                            continue
                        pmap[pool][frozenset(nums)] = float(div)
                    except Exception:
                        continue
                races[rn] = dict(pmap)
        out[d] = races
    return out


def _bb_active_window(entry: dict) -> tuple[str, str | None]:
    added = _norm_date(entry.get("added_date"))
    exp   = _norm_date(entry.get("expiry_date"))
    return added, exp


def _row_key(date_norm: str) -> str:
    return date_norm.replace("-", "")


def _win_return(stake: float, sp: float | None, div_per10: float | None,
                hit: bool) -> float:
    if not hit:
        return 0.0
    if div_per10 and div_per10 > 0:
        return stake * (div_per10 / 10.0)
    if sp and sp > 0:
        # SP rebate ≈ 17.5% takeout. Use sp directly (decimal odds).
        return stake * sp
    return 0.0


def _place_return(stake: float, div_per10: float | None, hit: bool) -> float:
    if not hit or not div_per10 or div_per10 <= 0:
        return 0.0
    return stake * (div_per10 / 10.0)


def analyse() -> dict:
    bb = json.loads(BB_PATH.read_text(encoding="utf-8")).get("entries", [])
    df = _load_db()
    divs = _load_dividends()
    user_bets = []
    if USER_BETS.exists():
        for line in USER_BETS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    user_bets.append(json.loads(line))
                except Exception:
                    pass

    # Index user bets: meeting_date+race_number -> list of bets
    user_idx: dict = defaultdict(list)
    for b in user_bets:
        user_idx[(b.get("meeting_date"), b.get("race_number"))].append(b)

    today = datetime.now().strftime("%Y-%m-%d")
    horse_summaries = []
    grand_runs = 0
    grand_win_hits = 0
    grand_pla_hits = 0
    grand_win_stake = 0.0
    grand_win_ret   = 0.0
    grand_pla_stake = 0.0
    grand_pla_ret   = 0.0
    timeline = []   # (date_norm, pnl_split) for cumulative chart

    for e in bb:
        name = (e.get("horse_name") or "").upper().strip()
        if not name:
            continue
        added, exp = _bb_active_window(e)
        if not added:
            continue
        runs = df[(df["horse_name_u"] == name) & (df["race_date_norm"] > added)].copy()
        if exp:
            runs = runs[runs["race_date_norm"] <= exp]
        runs = runs.sort_values("race_date_norm")

        per_run = []
        h_runs = 0; h_win_hits = 0; h_pla_hits = 0
        h_win_stake = 0.0; h_win_ret = 0.0
        h_pla_stake = 0.0; h_pla_ret = 0.0

        for _, r in runs.iterrows():
            place = r["place_num"]
            if pd.isna(place):
                continue   # scratched / DNF: skip
            place = int(place)
            sp = r["win_odds_num"]
            sp = float(sp) if not pd.isna(sp) else None
            hno = r["horse_number_num"]
            hno = int(hno) if not pd.isna(hno) else None
            d_norm = r["race_date_norm"]
            d_compact = _row_key(d_norm)
            rn = int(r["race_number"])
            divs_race = divs.get(d_compact, {}).get(rn, {})
            win_div = pla_div = None
            if hno is not None:
                win_div = divs_race.get("WIN", {}).get(frozenset([hno]))
                pla_div = divs_race.get("PLACE", {}).get(frozenset([hno])) or \
                          divs_race.get("PLA",   {}).get(frozenset([hno]))

            # 1u WIN
            win_hit = (place == 1)
            win_stake = 1.0
            win_ret = _win_return(win_stake, sp, win_div, win_hit)

            # 1u PLACE — always staked. Use real PLACE dividend on hit; if hit
            # but no dividend recorded (e.g. older meeting) fall back to a
            # conservative SP-derived approximation: PLA_div ≈ 1 + (SP-1)/4.
            pla_hit = (place <= 3)
            pla_stake = 1.0
            if pla_hit:
                if pla_div and pla_div > 0:
                    pla_ret = pla_stake * (pla_div / 10.0)
                elif sp and sp > 1:
                    pla_ret = pla_stake * (1.0 + (sp - 1.0) / 4.0)
                else:
                    pla_ret = 0.0
            else:
                pla_ret = 0.0

            # User actually bet on this runner?
            user_match = False
            user_pnl_share = 0.0
            for b in user_idx.get((d_compact, rn), []):
                sels = b.get("selections") or []
                bk = b.get("banker")
                involved = (hno in sels) or (bk == hno)
                if involved:
                    user_match = True
                    # rough pnl attribution: if user hit & this horse was selected,
                    # claim full pnl (multiple bb horses in same ticket double-count)
                    user_pnl_share += float(b.get("pnl_hkd") or 0.0)

            per_run.append({
                "date": d_norm, "race": rn,
                "horse_no": hno, "place": place, "sp": sp,
                "win_div": win_div, "pla_div": pla_div,
                "win_ret": round(win_ret, 3),
                "pla_stake": pla_stake, "pla_ret": round(pla_ret, 3),
                "user_bet": user_match,
                "user_pnl_attr": round(user_pnl_share, 2),
            })
            h_runs += 1
            h_win_stake += win_stake; h_win_ret += win_ret
            h_pla_stake += pla_stake; h_pla_ret += pla_ret
            if win_hit: h_win_hits += 1
            if pla_hit: h_pla_hits += 1
            # split-stake pnl: 0.5 WIN + 0.5 PLACE per run
            if pla_stake > 0:
                split_pnl = (0.5 * win_ret + 0.5 * pla_ret) - 1.0
            else:
                split_pnl = win_ret - 1.0
            timeline.append((d_norm, split_pnl, name))

        if h_runs == 0:
            verdict = "NO_RUNS"
        else:
            roi_split_stake = (h_win_stake + h_pla_stake) / 2.0 if h_pla_stake > 0 else h_win_stake
            roi_split_ret   = (h_win_ret + h_pla_ret) / 2.0 if h_pla_stake > 0 else h_win_ret
            roi_split = (roi_split_ret - roi_split_stake) / roi_split_stake if roi_split_stake else 0
            if roi_split >= 0.10 and h_pla_hits / h_runs >= 0.40:
                verdict = "KEEP"
            elif roi_split <= -0.30 or (h_runs >= 3 and h_pla_hits / h_runs < 0.20):
                verdict = "EXPIRE"
            else:
                verdict = "WATCH"

        horse_summaries.append({
            "id": e.get("id"), "horse": e.get("horse_name"),
            "added_date": added, "expiry_date": exp,
            "confidence": e.get("confidence"),
            "tags": e.get("tags") or [],
            "reasoning": (e.get("reasoning") or "")[:140],
            "runs": h_runs,
            "win_hits": h_win_hits, "pla_hits": h_pla_hits,
            "win_hit_pct":  round(h_win_hits / h_runs, 3) if h_runs else None,
            "pla_hit_pct":  round(h_pla_hits / h_runs, 3) if h_runs else None,
            "win_stake": round(h_win_stake, 2), "win_ret": round(h_win_ret, 2),
            "win_roi": round((h_win_ret - h_win_stake) / h_win_stake, 3) if h_win_stake else None,
            "pla_stake": round(h_pla_stake, 2), "pla_ret": round(h_pla_ret, 2),
            "pla_roi": round((h_pla_ret - h_pla_stake) / h_pla_stake, 3) if h_pla_stake else None,
            "verdict": verdict,
            "per_run": per_run,
        })
        grand_runs += h_runs
        grand_win_hits += h_win_hits
        grand_pla_hits += h_pla_hits
        grand_win_stake += h_win_stake; grand_win_ret += h_win_ret
        grand_pla_stake += h_pla_stake; grand_pla_ret += h_pla_ret

    # Confidence cohort breakdown
    cohort = defaultdict(lambda: {"horses": 0, "runs": 0, "win_hits": 0, "pla_hits": 0,
                                   "win_stake": 0.0, "win_ret": 0.0,
                                   "pla_stake": 0.0, "pla_ret": 0.0})
    for h in horse_summaries:
        c = h.get("confidence") or "?"
        cohort[c]["horses"] += 1
        cohort[c]["runs"] += h["runs"]
        cohort[c]["win_hits"] += h["win_hits"]
        cohort[c]["pla_hits"] += h["pla_hits"]
        cohort[c]["win_stake"] += h["win_stake"]; cohort[c]["win_ret"] += h["win_ret"]
        cohort[c]["pla_stake"] += h["pla_stake"]; cohort[c]["pla_ret"] += h["pla_ret"]

    # Tag breakdown (top tags)
    tag_stats = defaultdict(lambda: {"horses": 0, "runs": 0, "pla_hits": 0,
                                       "win_stake": 0.0, "win_ret": 0.0})
    for h in horse_summaries:
        for tg in (h.get("tags") or []):
            tag_stats[tg]["horses"] += 1
            tag_stats[tg]["runs"] += h["runs"]
            tag_stats[tg]["pla_hits"] += h["pla_hits"]
            tag_stats[tg]["win_stake"] += h["win_stake"]; tag_stats[tg]["win_ret"] += h["win_ret"]

    # Compare BB-runs ROI vs user actual bets
    user_settled = [b for b in user_bets if b.get("status") == "settled"]
    user_total_stake = sum(float(b.get("stake_hkd") or 0) for b in user_settled)
    user_total_ret   = sum(float(b.get("return_hkd") or 0) for b in user_settled)
    user_pnl = user_total_ret - user_total_stake
    user_roi = user_pnl / user_total_stake if user_total_stake else 0
    user_hits = sum(1 for b in user_settled if b.get("hit"))
    bb_overlap_bets = []
    bb_horse_set = {h["horse"].upper() for h in horse_summaries}
    for h in horse_summaries:
        for run in h["per_run"]:
            if run["user_bet"]:
                bb_overlap_bets.append({"horse": h["horse"], "date": run["date"],
                                          "race": run["race"], "place": run["place"],
                                          "user_pnl_attr": run["user_pnl_attr"]})

    grand = {
        "blackbook_size": len(bb),
        "horses_with_runs": sum(1 for h in horse_summaries if h["runs"] > 0),
        "total_runs": grand_runs,
        "win_hits": grand_win_hits, "pla_hits": grand_pla_hits,
        "win_strike_rate": round(grand_win_hits / grand_runs, 3) if grand_runs else None,
        "pla_strike_rate": round(grand_pla_hits / grand_runs, 3) if grand_runs else None,
        "win_stake": round(grand_win_stake, 2), "win_ret": round(grand_win_ret, 2),
        "win_roi": round((grand_win_ret - grand_win_stake) / grand_win_stake, 3) if grand_win_stake else None,
        "pla_stake": round(grand_pla_stake, 2), "pla_ret": round(grand_pla_ret, 2),
        "pla_roi": round((grand_pla_ret - grand_pla_stake) / grand_pla_stake, 3) if grand_pla_stake else None,
    }
    if grand_win_stake and grand_pla_stake:
        s = (grand_win_stake + grand_pla_stake) / 2
        r = (grand_win_ret + grand_pla_ret) / 2
        grand["split_50_50_roi"] = round((r - s) / s, 3)
    user = {
        "settled_bets": len(user_settled),
        "user_stake": round(user_total_stake, 2),
        "user_return": round(user_total_ret, 2),
        "user_pnl":    round(user_pnl, 2),
        "user_roi":    round(user_roi, 3),
        "user_hits":   user_hits,
        "user_hit_rate": round(user_hits / len(user_settled), 3) if user_settled else None,
        "bb_overlap_count": len(bb_overlap_bets),
        "bb_overlap_pnl_attr": round(sum(b["user_pnl_attr"] for b in bb_overlap_bets), 2),
        "bb_overlap_examples": bb_overlap_bets[:30],
    }

    # Build cumulative ROI chart
    timeline.sort(key=lambda x: x[0])
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        if timeline:
            cum = []
            tot = 0.0
            for d, pnl, _ in timeline:
                tot += pnl
                cum.append((d, tot))
            xs = [c[0] for c in cum]
            ys = [c[1] for c in cum]
            fig, ax = plt.subplots(figsize=(11, 4.5))
            ax.plot(range(len(xs)), ys, "-", color="#1f77b4", lw=1.6)
            ax.fill_between(range(len(xs)), ys, 0, where=[y >= 0 for y in ys],
                            alpha=0.18, color="green")
            ax.fill_between(range(len(xs)), ys, 0, where=[y < 0 for y in ys],
                            alpha=0.18, color="red")
            ax.axhline(0, color="black", lw=0.7)
            ax.set_title(f"Blackbook cumulative PnL — 0.5u WIN + 0.5u PLACE per run "
                         f"({len(timeline)} runs across {grand['horses_with_runs']} horses)")
            ax.set_xlabel("Run # (chronological)")
            ax.set_ylabel("Cumulative PnL (units of 1u stake)")
            ax.grid(alpha=0.3)
            fig.tight_layout()
            fig.savefig(OUT_PNG, dpi=110)
            plt.close(fig)
    except Exception as exc:
        print(f"[chart] skipped: {exc}")

    # Write outputs
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "grand": grand,
        "user_actual": user,
        "by_confidence": {k: {**v,
                                 "win_roi": round((v["win_ret"]-v["win_stake"])/v["win_stake"],3) if v["win_stake"] else None,
                                 "pla_roi": round((v["pla_ret"]-v["pla_stake"])/v["pla_stake"],3) if v["pla_stake"] else None,
                                } for k, v in cohort.items()},
        "by_tag": {k: {**v,
                         "win_roi": round((v["win_ret"]-v["win_stake"])/v["win_stake"],3) if v["win_stake"] else None,
                        } for k, v in tag_stats.items()},
        "horses": horse_summaries,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # Flat CSV
    rows = []
    for h in horse_summaries:
        rows.append({
            "horse": h["horse"], "added": h["added_date"], "expires": h["expiry_date"],
            "conf": h["confidence"], "tags": "|".join(h["tags"]),
            "runs": h["runs"], "win_hits": h["win_hits"], "pla_hits": h["pla_hits"],
            "win_hit_pct": h["win_hit_pct"], "pla_hit_pct": h["pla_hit_pct"],
            "win_roi": h["win_roi"], "pla_roi": h["pla_roi"],
            "verdict": h["verdict"], "reasoning": h["reasoning"],
        })
    pd.DataFrame(rows).to_csv(OUT_CSV, index=False, encoding="utf-8")
    return payload


if __name__ == "__main__":
    p = analyse()
    g = p["grand"]; u = p["user_actual"]
    print(f"BB size={g['blackbook_size']}  with-runs={g['horses_with_runs']}  total runs={g['total_runs']}")
    print(f"  WIN  : {g['win_hits']:3d}/{g['total_runs']:3d} = {(g['win_strike_rate'] or 0)*100:5.1f}%  "
          f"ROI={(g['win_roi'] or 0)*100:+6.1f}%  (1u flat stake, SP-priced)")
    print(f"  PLACE: {g['pla_hits']:3d}/{g['total_runs']:3d} = {(g['pla_strike_rate'] or 0)*100:5.1f}%  "
          f"ROI={(g['pla_roi'] or 0)*100:+6.1f}%  (1u flat stake, real PLACE div where known)")
    if "split_50_50_roi" in g:
        print(f"  50/50: ROI={g['split_50_50_roi']*100:+.1f}%")
    print()
    print("By confidence:")
    for c, v in p["by_confidence"].items():
        print(f"  {c:6s} horses={v['horses']:2d} runs={v['runs']:3d} "
              f"WIN={v['win_hits']:2d} PLA={v['pla_hits']:2d}  "
              f"win_roi={(v['win_roi'] or 0)*100:+5.1f}%  "
              f"pla_roi={(v['pla_roi'] or 0)*100:+5.1f}%")
    print()
    print("USER actual betting (uploaded statements):")
    print(f"  bets={u['settled_bets']}  hit={u['user_hits']} ({(u['user_hit_rate'] or 0)*100:.1f}%)  "
          f"stake={u['user_stake']:.0f}  ret={u['user_return']:.0f}  pnl={u['user_pnl']:+.0f}  ROI={u['user_roi']*100:+.1f}%")
    print(f"  BB overlap: {u['bb_overlap_count']} runs that featured a BB horse in user ticket  "
          f"(attributed pnl ≈ {u['bb_overlap_pnl_attr']:+.0f})")
    # Verdict counts
    from collections import Counter as _C
    vc = _C(h["verdict"] for h in p["horses"])
    print(f"\nVerdict counts: {dict(vc)}")
    print(f"\nTop-10 KEEP:")
    keepers = sorted([h for h in p["horses"] if h["verdict"] == "KEEP"],
                       key=lambda x: (x["pla_roi"] or 0), reverse=True)[:10]
    for k in keepers:
        print(f"  {k['horse']:25s} runs={k['runs']} pla={k['pla_hits']}/{k['runs']}  "
              f"win_roi={(k['win_roi'] or 0)*100:+6.1f}%  pla_roi={(k['pla_roi'] or 0)*100:+6.1f}%")
    print(f"\nEXPIRE candidates ({sum(1 for h in p['horses'] if h['verdict']=='EXPIRE')} total):")
    expirees = [h for h in p["horses"] if h["verdict"] == "EXPIRE"]
    for k in expirees[:15]:
        print(f"  {k['horse']:25s} runs={k['runs']} pla={k['pla_hits']}/{k['runs']}  "
              f"pla_roi={(k['pla_roi'] or 0)*100:+6.1f}%  ({k['reasoning'][:60]}...)")
