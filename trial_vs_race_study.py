"""Trial → Next-Race correlation study.

Joins every (horse, trial) entry from ``reports/trials_*.json`` with that
horse's next race in ``hkjc_results_updated.xlsx`` and quantifies how well
trial signals predict race outcome.

Outputs
-------
1. Console / stdout report (overall + subgroup hit rates, confusion matrix,
   four archetype lists: HONEST_GOOD, FALSE_POSITIVE, HIDDEN_GEM, HONEST_POOR)
2. ``reports/trial_vs_race_study.md``      — same report as markdown
3. ``reports/trial_vs_race_pairs.csv``     — every joined pair (auditable)
4. ``reports/trial_honesty_index.json``    — per-horse honesty score, usable
   live by the dashboard / decision engine to discount or amplify the trial
   signal on a horse-by-horse basis.

Usage
-----
    python trial_vs_race_study.py [--max-gap-days 60] [--min-pairs 200]
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from db_utils import DB_FILE, safe_read_excel

BASE = Path(__file__).parent
REPORTS = BASE / "reports"

# ── Keyword dictionaries (mirror dashboard._trial_sentiment) ─────────────
_CONCEAL_KW = [
    "held up", "under a hold", "not asked", "not extend", "eased",
    "cruised", "in hand", "restrain", "within himself", "not pushed", "no effort",
]
_NEG_KW = [
    "unimpressive", "ordinary", "poor", "disappointing", "limited",
    "failed", "struggled", "green", "slowly away",
]
_POS_PHRASES = [
    "ran on well", "quicken", "impressive", "easily", "strong",
    "stayed on", "hit the front", "to score", "won going away",
    "not fully tested", "not tested",
]


def _trial_sentiment(entry: dict) -> str:
    """Return sentiment flag for a trial entry: '++', '+', '-', or ''."""
    comment = (entry.get("comment", "") or "").lower()
    rp = entry.get("running_positions", []) or []
    fp = rp[-1] if rp else None
    sp = rp[0] if rp else None
    n = entry.get("n_horses", 0) or 0

    has_pos = any(p in comment for p in _POS_PHRASES)
    has_neg = any(nk in comment for nk in _NEG_KW)
    is_concealed = any(kw in comment for kw in _CONCEAL_KW) and not has_neg
    has_eased = "eased" in comment and not has_neg
    top_half = isinstance(fp, int) and n > 0 and fp <= max(1, n // 2)
    won = isinstance(fp, int) and fp == 1 and n >= 3
    gained = (isinstance(sp, int) and isinstance(fp, int) and (sp - fp) >= 2)
    bottom_q = (isinstance(fp, int) and n >= 4 and fp >= n - 1)

    if won and (is_concealed or has_pos):
        return "++"
    if won:
        return "++"
    if is_concealed and top_half:
        return "++"
    if has_eased and has_pos:
        return "++"
    if has_pos and top_half:
        return "++"
    if has_neg:
        return "-"
    if bottom_q and not has_pos and not is_concealed:
        return "-"
    if has_eased or is_concealed or gained or has_pos:
        return "+"
    if top_half and not has_neg:
        return "+"
    return ""


# ── LBW parser ───────────────────────────────────────────────────────────
def parse_lbw(s: str) -> float:
    s = (s or "").strip().upper()
    if s in ("", "-", "0"):
        return 0.0
    if s == "SH":
        return 0.1
    if s == "HD":
        return 0.2
    if s == "NK":
        return 0.3
    s = s.replace("L", "").replace(" ", "")
    try:
        if "-" in s:
            whole, frac = s.split("-", 1)
            w = float(whole) if whole else 0.0
            if "/" in frac:
                a, b = frac.split("/")
                return w + float(a) / float(b)
            return w + float(frac)
        if "/" in s:
            a, b = s.split("/")
            return float(a) / float(b)
        return float(s)
    except (ValueError, ZeroDivisionError):
        return 0.0


def parse_place(p) -> int | None:
    """Parse the messy 'place' column → int, or None if non-finisher."""
    if p is None:
        return None
    s = str(p).strip()
    m = re.match(r"^(\d+)", s)
    if not m:
        return None
    return int(m.group(1))


# ── Load trials ──────────────────────────────────────────────────────────
def load_trial_entries() -> list[dict]:
    """Flat list of trial entries with all fields needed downstream."""
    out: list[dict] = []
    for f in sorted(REPORTS.glob("trials_*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        trial_date = data.get("date", "")
        if not trial_date:
            continue
        try:
            td = datetime.strptime(trial_date, "%Y-%m-%d")
        except ValueError:
            continue
        for batch in data.get("batches", []):
            sec_times = batch.get("sectional_times") or []
            last_sec = None
            try:
                if sec_times:
                    last_sec = float(sec_times[-1])
            except (TypeError, ValueError):
                last_sec = None
            for h in batch.get("horses", []):
                rp = h.get("running_positions") or []
                out.append({
                    "horse": (h.get("horse_name") or "").strip().upper(),
                    "trial_date": td,
                    "trial_dist": int(batch.get("distance_m") or 0),
                    "course": batch.get("course", ""),
                    "n_horses": batch.get("n_horses") or len(batch.get("horses") or []),
                    "running_positions": rp,
                    "fp": rp[-1] if rp else None,
                    "sp": rp[0] if rp else None,
                    "lbw_l": parse_lbw(h.get("lbw", "")),
                    "comment": (h.get("comment") or ""),
                    "gear": h.get("gear", ""),
                    "batch_last_sec": last_sec,
                })
    return out


# ── Feature engineering ──────────────────────────────────────────────────
def annotate_trials(trials: list[dict]) -> list[dict]:
    """Add sentiment + improvement-vs-prior-trial features in-place."""
    # Sort each horse's trials by date so we can diff consecutive entries
    by_horse: dict[str, list[dict]] = defaultdict(list)
    for t in trials:
        by_horse[t["horse"]].append(t)

    for horse, lst in by_horse.items():
        lst.sort(key=lambda x: x["trial_date"])
        prev = None
        for t in lst:
            t["sentiment"] = _trial_sentiment(t)
            t["finish_pct"] = (
                t["fp"] / t["n_horses"]
                if t["fp"] and t["n_horses"]
                else None
            )
            t["concealed"] = any(kw in t["comment"].lower() for kw in _CONCEAL_KW)
            t["positive_phrase"] = any(p in t["comment"].lower() for p in _POS_PHRASES)
            t["negative"] = any(nk in t["comment"].lower() for nk in _NEG_KW)
            t["won_trial"] = (t["fp"] == 1 and (t["n_horses"] or 0) >= 3)

            # Improvement vs previous trial (any distance, any course)
            if prev is not None:
                d_fin = None
                if t["finish_pct"] is not None and prev["finish_pct"] is not None:
                    d_fin = t["finish_pct"] - prev["finish_pct"]  # negative = improved
                d_lbw = (prev["lbw_l"] or 0) - (t["lbw_l"] or 0)  # positive = improved
                tier = {"++": 3, "+": 2, "": 1, "-": 0}
                d_sent = tier.get(t["sentiment"], 1) - tier.get(prev["sentiment"], 1)
                t["improver"] = (
                    (d_fin is not None and d_fin <= -0.20) or
                    (d_lbw is not None and d_lbw >= 1.0) or
                    d_sent >= 2
                )
            else:
                t["improver"] = False
            prev = t
    return trials


# ── Load races ───────────────────────────────────────────────────────────
def load_races() -> pd.DataFrame:
    df = safe_read_excel(DB_FILE)
    df = df[["race_date", "race_number", "horse_name", "place",
             "distance", "race_course", "draw"]].copy()
    df["horse_name"] = df["horse_name"].astype(str).str.upper().str.strip()
    df["race_date"] = pd.to_datetime(df["race_date"], errors="coerce")
    df = df.dropna(subset=["race_date", "horse_name"])
    df["place_int"] = df["place"].apply(parse_place)
    # n_runners per (date, race_no)
    df["n_runners"] = df.groupby(["race_date", "race_number"])["horse_name"].transform("count")
    return df


# ── Join: trial → next race ──────────────────────────────────────────────
def build_pairs(trials: list[dict], races: pd.DataFrame,
                max_gap_days: int) -> pd.DataFrame:
    races_by_horse = {h: g.sort_values("race_date") for h, g in races.groupby("horse_name")}
    rows = []
    for t in trials:
        if not t["horse"]:
            continue
        g = races_by_horse.get(t["horse"])
        if g is None or g.empty:
            continue
        cutoff = t["trial_date"] + timedelta(days=max_gap_days)
        nxt = g[(g["race_date"] > t["trial_date"]) & (g["race_date"] <= cutoff)]
        if nxt.empty:
            continue
        r = nxt.iloc[0]
        if pd.isna(r["place_int"]) or pd.isna(r["n_runners"]) or r["n_runners"] < 4:
            continue
        race_dist = int(r["distance"]) if pd.notna(r["distance"]) else 0
        rows.append({
            "horse": t["horse"],
            "trial_date": t["trial_date"].date().isoformat(),
            "race_date": r["race_date"].date().isoformat(),
            "gap_days": (r["race_date"].date() - t["trial_date"].date()).days,
            "trial_dist": t["trial_dist"],
            "race_dist": race_dist,
            "dist_match": abs(t["trial_dist"] - race_dist) <= 200,
            "trial_sentiment": t["sentiment"] or "0",
            "concealed": t["concealed"],
            "won_trial": t["won_trial"],
            "improver": t["improver"],
            "trial_finish_pct": t["finish_pct"],
            "race_place": int(r["place_int"]),
            "race_n": int(r["n_runners"]),
            "race_finish_pct": float(r["place_int"]) / float(r["n_runners"]),
            "race_win": int(r["place_int"] == 1),
            "race_top3": int(r["place_int"] <= 3),
            "race_top_half": int(r["place_int"] <= max(1, r["n_runners"] // 2)),
        })
    return pd.DataFrame(rows)


# ── Reporting ────────────────────────────────────────────────────────────
def rate_block(df: pd.DataFrame, by: str, label: str = "") -> str:
    out = [f"\n### {label or by}"]
    g = df.groupby(by, dropna=False).agg(
        n=("race_win", "size"),
        win=("race_win", "mean"),
        top3=("race_top3", "mean"),
        top_half=("race_top_half", "mean"),
        avg_finish_pct=("race_finish_pct", "mean"),
    ).round(3)
    g = g.sort_index()
    out.append(g.to_string())
    return "\n".join(out)


def confusion_matrix(df: pd.DataFrame) -> str:
    """Trial sentiment × race outcome bucket."""
    def bucket(p):
        if p == 1:
            return "win"
        if p <= 3:
            return "top3"
        if p <= 6:
            return "mid"
        return "back"

    df = df.copy()
    df["outcome"] = df["race_place"].apply(bucket)
    pivot = pd.crosstab(df["trial_sentiment"], df["outcome"],
                        normalize="index").round(3)
    pivot = pivot.reindex(["++", "+", "0", "-"]).reindex(
        columns=["win", "top3", "mid", "back"], fill_value=0)
    counts = df["trial_sentiment"].value_counts().to_dict()
    pivot["n"] = pivot.index.map(counts).fillna(0).astype(int)
    return pivot.to_string()


def archetypes(df: pd.DataFrame, n_each: int = 25) -> dict[str, list[dict]]:
    """Tag each pair into an archetype the user described."""
    df = df.copy()

    def label(row):
        good_trial = row["trial_sentiment"] in ("++", "+") or row["won_trial"] or row["concealed"]
        bad_trial = row["trial_sentiment"] == "-"
        good_race = row["race_top3"] == 1
        poor_race = row["race_finish_pct"] >= 0.6
        if good_trial and good_race:
            return "HONEST_GOOD"
        if good_trial and poor_race:
            return "FALSE_POSITIVE"
        if not good_trial and good_race:
            return "HIDDEN_GEM"
        if bad_trial and poor_race:
            return "HONEST_POOR"
        return "MIXED"

    df["archetype"] = df.apply(label, axis=1)
    out: dict[str, list[dict]] = {}
    for arch in ["HONEST_GOOD", "FALSE_POSITIVE", "HIDDEN_GEM", "HONEST_POOR"]:
        sub = df[df["archetype"] == arch].sort_values("race_date", ascending=False).head(n_each)
        out[arch] = sub[[
            "horse", "trial_date", "race_date", "trial_sentiment",
            "trial_finish_pct", "race_place", "race_n",
            "trial_dist", "race_dist", "concealed", "improver",
        ]].to_dict(orient="records")
    return out


def horse_honesty_index(df: pd.DataFrame, min_obs: int = 2) -> dict:
    """Per-horse: among that horse's positive trials, how often did they
    deliver top3? Used as a live confidence multiplier on the trial signal.
    """
    pos = df[df["trial_sentiment"].isin(["++", "+"]) | df["won_trial"] | df["concealed"]]
    if pos.empty:
        return {}
    g = pos.groupby("horse").agg(
        n_positive_trials=("race_top3", "size"),
        top3_rate=("race_top3", "mean"),
        win_rate=("race_win", "mean"),
        avg_race_finish_pct=("race_finish_pct", "mean"),
        last_trial=("trial_date", "max"),
    )
    g = g[g["n_positive_trials"] >= min_obs]
    # Honesty score: top3 rate, shrunk toward population mean by Bayesian prior
    pop = pos["race_top3"].mean()
    k = 3.0  # prior weight (3 pseudo-observations)
    g["honesty_score"] = (g["top3_rate"] * g["n_positive_trials"] + pop * k) / (g["n_positive_trials"] + k)
    g["honesty_score"] = g["honesty_score"].round(3)
    g["top3_rate"] = g["top3_rate"].round(3)
    g["win_rate"] = g["win_rate"].round(3)
    g["avg_race_finish_pct"] = g["avg_race_finish_pct"].round(3)
    return {
        "population_top3_rate_after_positive_trial": round(float(pop), 3),
        "n_horses": int(g.shape[0]),
        "horses": g.reset_index().to_dict(orient="records"),
    }


def horse_archetype_index(df: pd.DataFrame, min_pairs: int = 2) -> dict:
    """Classify each horse into a trial-archetype based on ALL their pairs.

    Archetypes (the four patterns the user articulated):
      HONEST_GOOD    — positive trials translate into top-3 races
      FALSE_POSITIVE — positive trials but back-half races (Brilliant Fire pattern)
      HIDDEN_GEM     — neutral / negative trials but races top-3
      HONEST_POOR    — negative trials and bad races
      MIXED          — none of the above dominates
    """
    df = df.copy()
    df["pos_trial"] = (df["trial_sentiment"].isin(["++", "+"])
                      | df["won_trial"] | df["concealed"])
    df["neg_trial"] = df["trial_sentiment"] == "-"
    df["good_race"] = df["race_top3"] == 1
    df["poor_race"] = df["race_finish_pct"] >= 0.6

    horses: dict[str, dict] = {}
    pop_top3_after_pos = float(df.loc[df["pos_trial"], "race_top3"].mean())
    pop_top3_after_neg = float(df.loc[df["neg_trial"], "race_top3"].mean()) if df["neg_trial"].any() else 0.0

    for horse, sub in df.groupby("horse"):
        if len(sub) < min_pairs:
            continue
        n = len(sub)
        n_pos = int(sub["pos_trial"].sum())
        n_neg = int(sub["neg_trial"].sum())
        good_after_pos = int((sub["pos_trial"] & sub["good_race"]).sum())
        poor_after_pos = int((sub["pos_trial"] & sub["poor_race"]).sum())
        good_after_neut_or_neg = int(((~sub["pos_trial"]) & sub["good_race"]).sum())
        poor_after_neg = int((sub["neg_trial"] & sub["poor_race"]).sum())

        # Bayesian-smoothed top3 rate after the horse's positive trials.
        if n_pos >= 1:
            top3_after_pos = sub.loc[sub["pos_trial"], "race_top3"].mean()
            honesty = (top3_after_pos * n_pos + pop_top3_after_pos * 3.0) / (n_pos + 3.0)
        else:
            honesty = pop_top3_after_pos

        # Decide archetype.
        archetype = "MIXED"
        if n_pos >= 2 and good_after_pos / max(n_pos, 1) >= 0.5:
            archetype = "HONEST_GOOD"
        elif n_pos >= 2 and poor_after_pos / max(n_pos, 1) >= 0.6 and good_after_pos / max(n_pos, 1) <= 0.2:
            archetype = "FALSE_POSITIVE"
        elif (n - n_pos) >= 2 and good_after_neut_or_neg / max(n - n_pos, 1) >= 0.4:
            archetype = "HIDDEN_GEM"
        elif n_neg >= 2 and poor_after_neg / max(n_neg, 1) >= 0.6:
            archetype = "HONEST_POOR"

        horses[horse] = {
            "archetype": archetype,
            "n_pairs": int(n),
            "n_pos_trials": n_pos,
            "n_neg_trials": n_neg,
            "honesty_score": round(float(honesty), 3),
            "win_rate":   round(float(sub["race_win"].mean()), 3),
            "top3_rate":  round(float(sub["race_top3"].mean()), 3),
            "avg_finish_pct": round(float(sub["race_finish_pct"].mean()), 3),
            "last_trial": sub["trial_date"].max(),
            "last_race":  sub["race_date"].max(),
            "summary": (f"{n} pairs · {n_pos}+ / {n_neg}-  "
                        f"·  win {sub['race_win'].mean():.0%}  "
                        f"top3 {sub['race_top3'].mean():.0%}"),
        }

    return {
        "population_top3_after_pos": round(pop_top3_after_pos, 3),
        "population_top3_after_neg": round(pop_top3_after_neg, 3),
        "n_horses": len(horses),
        "horses": horses,
    }


# ── Main ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-gap-days", type=int, default=60,
                    help="Max days between trial and next race")
    ap.add_argument("--min-pairs", type=int, default=100,
                    help="Min pairs to bother emitting per-horse honesty")
    args = ap.parse_args()

    print("[1/4] loading trials …")
    trials = annotate_trials(load_trial_entries())
    print(f"      {len(trials):,} trial entries from {len(set(t['horse'] for t in trials)):,} horses")

    print("[2/4] loading races …")
    races = load_races()
    print(f"      {len(races):,} starter rows from master xlsx")

    print(f"[3/4] joining (≤ {args.max_gap_days} days gap) …")
    df = build_pairs(trials, races, args.max_gap_days)
    print(f"      {len(df):,} trial→race pairs")
    if df.empty:
        print("No pairs found. Check that hkjc_results_updated.xlsx is populated.")
        return

    REPORTS.mkdir(exist_ok=True)
    df.to_csv(REPORTS / "trial_vs_race_pairs.csv", index=False)

    print("[4/4] computing rates …")
    sections: list[str] = []
    sections.append(f"# Trial → Next Race study  ({len(df):,} pairs, gap ≤ {args.max_gap_days}d)\n")
    sections.append("Generated: " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    sections.append("\n## Headline rates\n")
    sections.append(f"- Overall win rate:     **{df['race_win'].mean():.3f}**")
    sections.append(f"- Overall top-3 rate:   **{df['race_top3'].mean():.3f}**")
    sections.append(f"- Overall avg finish%:  **{df['race_finish_pct'].mean():.3f}**  (lower = better)")

    sections.append("\n## Confusion matrix — trial sentiment × race outcome\n")
    sections.append("```\n" + confusion_matrix(df) + "\n```")

    sections.append("\n## Hit rates by trial sentiment\n```\n" +
                    rate_block(df, "trial_sentiment", "Trial sentiment").split("\n", 1)[1] + "\n```")
    sections.append("\n## Hit rates by trial sentiment AND distance match (≤200m)\n```\n" +
                    df.groupby(["trial_sentiment", "dist_match"])
                      .agg(n=("race_win", "size"),
                           win=("race_win", "mean"),
                           top3=("race_top3", "mean"))
                      .round(3).to_string() + "\n```")
    sections.append("\n## Concealed-form trials (hidden positive)\n```\n" +
                    df.groupby("concealed").agg(
                        n=("race_win", "size"),
                        win=("race_win", "mean"),
                        top3=("race_top3", "mean")).round(3).to_string() + "\n```")
    sections.append("\n## Trial winners (won the trial outright)\n```\n" +
                    df.groupby("won_trial").agg(
                        n=("race_win", "size"),
                        win=("race_win", "mean"),
                        top3=("race_top3", "mean")).round(3).to_string() + "\n```")
    sections.append("\n## Improvers (better than previous trial)\n```\n" +
                    df.groupby("improver").agg(
                        n=("race_win", "size"),
                        win=("race_win", "mean"),
                        top3=("race_top3", "mean")).round(3).to_string() + "\n```")
    sections.append("\n## Stacked: improver × positive sentiment\n```\n" +
                    df.assign(pos=df["trial_sentiment"].isin(["++", "+"]))
                      .groupby(["improver", "pos"]).agg(
                        n=("race_win", "size"),
                        win=("race_win", "mean"),
                        top3=("race_top3", "mean")).round(3).to_string() + "\n```")

    arch = archetypes(df)
    sections.append("\n## Archetype counts\n```")
    for k, v in arch.items():
        sections.append(f"  {k:<16} {len(v):>4}")
    sections.append("```")
    for k, v in arch.items():
        sections.append(f"\n### Recent {k} (latest {len(v)})")
        for row in v[:15]:
            sections.append(
                f"- **{row['horse']}**  trial {row['trial_date']} ({row['trial_sentiment']}, "
                f"{row['trial_dist']}m) → race {row['race_date']} "
                f"placed {row['race_place']}/{row['race_n']} ({row['race_dist']}m)"
            )

    report = "\n".join(sections)
    print("\n" + report)

    (REPORTS / "trial_vs_race_study.md").write_text(report, encoding="utf-8")

    if len(df) >= args.min_pairs:
        idx = horse_honesty_index(df, min_obs=2)
        (REPORTS / "trial_honesty_index.json").write_text(
            json.dumps(idx, indent=2, default=str), encoding="utf-8")
        print(f"\nWrote honesty index for {idx.get('n_horses', 0)} horses → "
              "reports/trial_honesty_index.json")

        arch_idx = horse_archetype_index(df, min_pairs=2)
        (REPORTS / "trial_archetype_index.json").write_text(
            json.dumps(arch_idx, indent=2, default=str), encoding="utf-8")
        print(f"Wrote archetype index for {arch_idx.get('n_horses', 0)} horses → "
              "reports/trial_archetype_index.json")
        # Distribution preview
        from collections import Counter as _Ctr
        dist = _Ctr(h["archetype"] for h in arch_idx["horses"].values())
        print("  archetype distribution:", dict(dist))

    print("\nWrote:")
    print("  reports/trial_vs_race_pairs.csv")
    print("  reports/trial_vs_race_study.md")


if __name__ == "__main__":
    main()
