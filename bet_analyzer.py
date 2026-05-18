"""bet_analyzer — forensic, brutally-honest performance review of
user-submitted bets vs the model picks.

Exposes a single public function:

    analyze_betting_period(rows, start_date, end_date, *,
                           sport_filter=None, market_filter=None,
                           bet_type_filter=None) -> {"json": dict,
                                                     "markdown": str}

`rows` is the same shape returned by ``user_bets.load_bets(settle=True)``.

Design notes
------------
- No optimism. Every finding is quantified.
- All P&L numbers are in HKD; ROI is decimal (e.g. -0.18 = -18%).
- "Model alignment" loads cached race-day reports the same way the existing
  "vs Model" tab does — ET v4.4 + SARR if available.
- "Implied odds" for a bet are derived as ``return_hkd / stake_hkd`` on
  HITS only (HKJC dividends include the stake, so this is the gross payout
  multiple). For losses we have no odds info — we proxy with bet_type.
- The function is deliberately stateless. Caching belongs to the caller.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

BASE = Path(__file__).resolve().parent


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _parse_meeting_date(s: str) -> date | None:
    try:
        return datetime.strptime(str(s), "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def _to_date(s: Any) -> date | None:
    if isinstance(s, date):
        return s
    if not s:
        return None
    if isinstance(s, str):
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
    return None


def _fmt_pct(x: float, sign: bool = True) -> str:
    if x is None:
        return "—"
    return f"{x*100:+.1f}%" if sign else f"{x*100:.1f}%"


def _fmt_money(x: float) -> str:
    if x is None:
        return "—"
    return f"${x:+,.0f}"


def _odds_bucket(payout_mult: float) -> str:
    if payout_mult is None or payout_mult <= 0:
        return "—"
    if payout_mult < 2.0:    return "<2.0"
    if payout_mult < 4.0:    return "2.0–4.0"
    if payout_mult < 8.0:    return "4.0–8.0"
    if payout_mult < 20.0:   return "8.0–20.0"
    if payout_mult < 50.0:   return "20.0–50.0"
    return "50.0+"


def _filter_rows(rows: list[dict], start: date, end: date,
                 *, sport_filter=None, market_filter=None,
                 bet_type_filter=None) -> list[dict]:
    out = []
    bt_set = {x.upper() for x in (bet_type_filter or [])}
    for r in rows:
        d = _parse_meeting_date(r.get("meeting_date"))
        if d is None or d < start or d > end:
            continue
        if bt_set and (r.get("bet_type", "").upper() not in bt_set):
            continue
        # sport_filter / market_filter reserved for future multi-sport
        out.append(r)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Model picks loader (mirrors vs Model tab)
# ─────────────────────────────────────────────────────────────────────────────
def _load_reports(dates: Iterable[str]) -> tuple[dict, dict]:
    et: dict[str, dict] = {}
    sa: dict[str, dict] = {}
    for d in set(dates):
        if not d:
            continue
        p_et = BASE / "reports" / f"race_day_report_{d}_v4.4.json"
        if p_et.exists():
            try:
                et[d] = json.loads(p_et.read_text(encoding="utf-8"))
            except Exception:
                pass
        p_sa = BASE / "reports" / f"race_day_report_{d}_SARR.json"
        if p_sa.exists():
            try:
                sa[d] = json.loads(p_sa.read_text(encoding="utf-8"))
            except Exception:
                pass
    return et, sa


def _top3(rep: dict, race_no: int) -> list[int]:
    if not rep:
        return []
    for race in rep.get("races", []):
        if int(race.get("race_number") or 0) == race_no:
            picks = sorted(race.get("picks", []) or [],
                           key=lambda p: p.get("rank", 99))
            return [int(p.get("horse_no") or 0)
                    for p in picks[:3] if p.get("horse_no")]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Core analytics blocks
# ─────────────────────────────────────────────────────────────────────────────
def _snapshot(settled: list[dict]) -> dict:
    n = len(settled)
    stake = sum(float(r.get("stake_hkd", 0)) for r in settled)
    ret = sum(float(r.get("return_hkd", 0)) for r in settled)
    hits = sum(1 for r in settled if r.get("hit"))
    avg_stake = stake / n if n else 0.0
    # avg payout multiple on HITS
    mults = [float(r["return_hkd"]) / float(r["stake_hkd"])
             for r in settled if r.get("hit")
             and float(r.get("stake_hkd", 0)) > 0]
    avg_winning_mult = statistics.mean(mults) if mults else 0.0
    return {
        "n_bets": n,
        "n_hits": hits,
        "win_rate": hits / n if n else 0.0,
        "stake": stake,
        "return": ret,
        "pnl": ret - stake,
        "roi": (ret - stake) / stake if stake else 0.0,
        "avg_stake": avg_stake,
        "avg_winning_payout_mult": avg_winning_mult,
    }


def _by_bet_type(settled: list[dict]) -> list[dict]:
    agg: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "hits": 0, "stake": 0.0, "ret": 0.0})
    for r in settled:
        bt = r.get("bet_type", "?").upper()
        a = agg[bt]
        a["n"] += 1
        if r.get("hit"): a["hits"] += 1
        a["stake"] += float(r.get("stake_hkd", 0))
        a["ret"] += float(r.get("return_hkd", 0))
    out = []
    for bt, a in agg.items():
        stake = a["stake"]
        out.append({
            "bet_type": bt,
            "n": a["n"],
            "hit_rate": a["hits"] / a["n"] if a["n"] else 0.0,
            "stake": stake,
            "pnl": a["ret"] - stake,
            "roi": (a["ret"] - stake) / stake if stake else 0.0,
        })
    out.sort(key=lambda x: x["pnl"])
    return out


def _by_odds_bucket(settled: list[dict]) -> list[dict]:
    """Bucket HIT bets by payout multiple, then add a 'loss' bucket per
    bet_type so the loser-side ROI drag is visible."""
    agg: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "hits": 0, "stake": 0.0, "ret": 0.0})
    for r in settled:
        stake = float(r.get("stake_hkd", 0))
        if stake <= 0: continue
        if r.get("hit"):
            mult = float(r.get("return_hkd", 0)) / stake
            bucket = _odds_bucket(mult)
        else:
            bucket = "lost"
        a = agg[bucket]
        a["n"] += 1
        if r.get("hit"): a["hits"] += 1
        a["stake"] += stake
        a["ret"] += float(r.get("return_hkd", 0))
    out = []
    for b, a in agg.items():
        stake = a["stake"]
        out.append({
            "bucket": b,
            "n": a["n"],
            "hit_rate": a["hits"] / a["n"] if a["n"] else 0.0,
            "stake": stake,
            "pnl": a["ret"] - stake,
            "roi": (a["ret"] - stake) / stake if stake else 0.0,
        })
    # natural ordering
    order = ["<2.0", "2.0–4.0", "4.0–8.0", "8.0–20.0", "20.0–50.0",
             "50.0+", "lost", "—"]
    out.sort(key=lambda x: order.index(x["bucket"])
             if x["bucket"] in order else 99)
    return out


def _by_dow(settled: list[dict]) -> list[dict]:
    agg: dict[int, dict] = defaultdict(
        lambda: {"n": 0, "stake": 0.0, "ret": 0.0})
    for r in settled:
        d = _parse_meeting_date(r.get("meeting_date"))
        if d is None: continue
        a = agg[d.weekday()]
        a["n"] += 1
        a["stake"] += float(r.get("stake_hkd", 0))
        a["ret"] += float(r.get("return_hkd", 0))
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    out = []
    for k in sorted(agg):
        a = agg[k]
        out.append({
            "dow": names[k],
            "n": a["n"],
            "stake": a["stake"],
            "pnl": a["ret"] - a["stake"],
            "roi": (a["ret"] - a["stake"]) / a["stake"] if a["stake"] else 0.0,
        })
    return out


def _loss_chasing(settled: list[dict]) -> dict:
    """Group settled bets by meeting_date. Compare daily total stake vs
    prior-meeting PnL. If prior day was a loss and today's stake is >120%
    of the user's rolling-7-meeting median stake → flag chase."""
    if not settled:
        return {"flagged": False, "n_chase_days": 0, "detail": []}
    # daily aggregates
    by_day: dict[date, dict] = defaultdict(
        lambda: {"stake": 0.0, "pnl": 0.0, "n": 0})
    for r in settled:
        d = _parse_meeting_date(r.get("meeting_date"))
        if d is None: continue
        a = by_day[d]
        a["stake"] += float(r.get("stake_hkd", 0))
        a["pnl"] += float(r.get("pnl_hkd", 0))
        a["n"] += 1
    days_sorted = sorted(by_day.keys())
    stakes = [by_day[d]["stake"] for d in days_sorted]
    # rolling median (use simple all-time median fallback when <7)
    detail = []
    chase_days = 0
    for i, d in enumerate(days_sorted):
        if i == 0: continue
        prev = by_day[days_sorted[i - 1]]
        cur = by_day[d]
        window = stakes[max(0, i - 7):i] or stakes[:i] or [cur["stake"]]
        med = statistics.median(window) if window else cur["stake"]
        ratio = (cur["stake"] / med) if med else 1.0
        if prev["pnl"] < 0 and ratio >= 1.20:
            chase_days += 1
            detail.append({
                "date": d.isoformat(),
                "prev_pnl": round(prev["pnl"], 2),
                "stake_today": round(cur["stake"], 2),
                "vs_median_x": round(ratio, 2),
            })
    return {
        "flagged": chase_days >= 2,
        "n_chase_days": chase_days,
        "n_meeting_days": len(days_sorted),
        "detail": detail,
    }


def _model_alignment(settled: list[dict]) -> dict:
    et, sa = _load_reports(r.get("meeting_date") for r in settled)
    buckets = {"aligned_et_or_sarr": [], "aligned_both": [],
               "deviated": [], "no_report": []}
    detail = []
    for r in settled:
        d = r.get("meeting_date")
        rn = int(r.get("race_number") or 0)
        et_top = _top3(et.get(d), rn) if d else []
        sa_top = _top3(sa.get(d), rn) if d else []
        if not et_top and not sa_top:
            buckets["no_report"].append(r); continue
        sels = set(int(x) for x in (r.get("selections") or []))
        if r.get("banker"):
            sels.add(int(r["banker"]))
        in_et = bool(sels & set(et_top))
        in_sa = bool(sels & set(sa_top))
        both = set(et_top) & set(sa_top)
        in_both = bool(sels & both) if both else False
        if in_both:
            cat = "aligned_both"
        elif in_et or in_sa:
            cat = "aligned_et_or_sarr"
        else:
            cat = "deviated"
        buckets[cat].append(r)
        detail.append({
            "date": d, "race": rn, "bet_type": r.get("bet_type"),
            "category": cat,
            "stake": float(r.get("stake_hkd", 0)),
            "pnl": float(r.get("pnl_hkd", 0)),
        })

    def _agg(bs: list[dict]) -> dict:
        n = len(bs)
        stake = sum(float(r.get("stake_hkd", 0)) for r in bs)
        ret = sum(float(r.get("return_hkd", 0)) for r in bs)
        hits = sum(1 for r in bs if r.get("hit"))
        return {
            "n": n,
            "hit_rate": hits / n if n else 0.0,
            "stake": stake,
            "pnl": ret - stake,
            "roi": (ret - stake) / stake if stake else 0.0,
        }

    return {
        "aligned_both": _agg(buckets["aligned_both"]),
        "aligned_et_or_sarr": _agg(buckets["aligned_et_or_sarr"]),
        "deviated": _agg(buckets["deviated"]),
        "no_report": _agg(buckets["no_report"]),
        "n_total_with_report": (len(buckets["aligned_both"])
                                + len(buckets["aligned_et_or_sarr"])
                                + len(buckets["deviated"])),
        "detail": detail,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Strength / weakness scoring
# ─────────────────────────────────────────────────────────────────────────────
def _identify_strengths(by_type: list[dict], by_odds: list[dict],
                        align: dict) -> list[dict]:
    out = []
    # Profitable bet types (≥10 bets, ROI ≥ +5%)
    for x in by_type:
        if x["n"] >= 10 and x["roi"] >= 0.05:
            out.append({
                "title": f"{x['bet_type']} is genuinely profitable",
                "evidence": (f"{x['n']} bets · hit-rate "
                             f"{x['hit_rate']*100:.0f}% · "
                             f"ROI {x['roi']*100:+.1f}% · "
                             f"PnL {_fmt_money(x['pnl'])}"),
                "edge_pct": round(x["roi"] * 100, 1),
            })
    # NOTE: payout-band ROI is NOT a true edge — bets are only bucketed by
    # the band they LANDED in (winners only). Losers all go in the "lost"
    # bucket. Reporting a per-band ROI here would always be positive and
    # would mislead. Skip the payout-band strengths until pre-race odds
    # are tracked (then real implied-odds bands can be computed).
    # Ensemble alignment ROI > deviated ROI by ≥15pp
    e = align["aligned_both"]
    dv = align["deviated"]
    if e["n"] >= 5 and dv["n"] >= 5 and (e["roi"] - dv["roi"]) >= 0.15:
        out.append({
            "title": "When you back ET ∩ SARR overlap, you do less damage",
            "evidence": (f"Ensemble-aligned ROI "
                         f"{e['roi']*100:+.1f}% over {e['n']} bets "
                         f"vs deviated ROI {dv['roi']*100:+.1f}% "
                         f"over {dv['n']} bets. "
                         f"Gap = {(e['roi']-dv['roi'])*100:+.1f}pp."),
            "edge_pct": round((e["roi"] - dv["roi"]) * 100, 1),
        })
    return out


def _identify_weaknesses(snap: dict, by_type: list[dict],
                         by_odds: list[dict], by_dow: list[dict],
                         chase: dict, align: dict,
                         settled: list[dict]) -> list[dict]:
    out = []
    total_stake = snap["stake"] or 1.0

    # 1. Loss-making bet types
    for x in by_type:
        if x["n"] >= 5 and x["roi"] <= -0.10:
            share = x["stake"] / total_stake
            out.append({
                "title": f"You bleed money on {x['bet_type']}",
                "quantified": (f"{x['n']} bets · ROI "
                               f"{x['roi']*100:+.1f}% · "
                               f"PnL {_fmt_money(x['pnl'])} · "
                               f"{share*100:.0f}% of your stake volume"),
                "pnl_impact": x["pnl"],
                "cause": ("Either this bet type doesn't suit your selection "
                          "process, or the take-out is grinding you down. "
                          "If ROI < -10% over ≥5 bets, the sample is no "
                          "longer noise."),
                "rule": (f"Stop placing {x['bet_type']} until you can "
                         "articulate, in writing, why each one has +EV "
                         "above market price. Default action: cap at "
                         f"50% of current avg stake (${snap['avg_stake']*0.5:.0f}) "
                         "for the next 10 bets and re-evaluate."),
            })
    # 2. Loss-making odds bands
    for x in by_odds:
        if x["bucket"] in ("lost", "—"): continue
        if x["n"] >= 4 and x["roi"] <= -0.15:
            out.append({
                "title": f"The {x['bucket']} payout band is a trap for you",
                "quantified": (f"{x['n']} hits in this band, ROI "
                               f"{x['roi']*100:+.1f}%, "
                               f"PnL {_fmt_money(x['pnl'])}"),
                "pnl_impact": x["pnl"],
                "cause": ("Likely favourite-chasing (very short prices) "
                          "or lottery-ticket bias (very long shots). "
                          "Both fail without a structural edge."),
                "rule": (f"Avoid bets whose expected payout multiple sits "
                         f"in {x['bucket']} unless model confidence ≥ 65% "
                         "AND you can name the specific edge."),
            })
    # 3. Deviating from model destroys value
    e = align["aligned_both"]
    dv = align["deviated"]
    if dv["n"] >= 5 and dv["roi"] < 0 and dv["roi"] < e["roi"] - 0.10:
        out.append({
            "title": "Overriding the model is costing you",
            "quantified": (f"{dv['n']} deviated bets, ROI "
                           f"{dv['roi']*100:+.1f}%, "
                           f"PnL {_fmt_money(dv['pnl'])}. "
                           f"Aligned-with-both-models bets ran at "
                           f"{e['roi']*100:+.1f}% over {e['n']} bets."),
            "pnl_impact": dv["pnl"],
            "cause": ("You are introducing personal opinions on top of "
                      "calibrated models without any tracked edge. "
                      "Recency bias and gut-feel reads are the usual "
                      "culprits."),
            "rule": ("HARD RULE: do not place a bet that contains NO "
                     "selection from the ET top-3 AND no selection from "
                     "the SARR top-3 unless the override is logged in "
                     "writing with a specific, falsifiable edge claim."),
        })
    # 4. Loss chasing
    if chase["flagged"]:
        out.append({
            "title": "You chase losses by inflating stakes",
            "quantified": (f"{chase['n_chase_days']} of "
                           f"{chase['n_meeting_days']} meeting days "
                           "showed stake ≥120% of your 7-meeting median "
                           "the day after a losing meeting."),
            "pnl_impact": None,
            "cause": ("Classic post-loss tilt. Increases variance exactly "
                      "when your judgment is most compromised."),
            "rule": ("Cap next-meeting total stake at 100% of your "
                     "30-day median whenever the previous meeting PnL "
                     "was negative. No exceptions."),
        })
    # 5. Day-of-week emotional volume
    if by_dow:
        worst = min(by_dow, key=lambda x: x["pnl"])
        if worst["n"] >= 5 and worst["roi"] <= -0.10:
            out.append({
                "title": f"{worst['dow']} is your worst trading day",
                "quantified": (f"{worst['n']} bets · ROI "
                               f"{worst['roi']*100:+.1f}% · "
                               f"PnL {_fmt_money(worst['pnl'])}"),
                "pnl_impact": worst["pnl"],
                "cause": ("Possibly a heavy meeting day where field sizes "
                          "and exotic combinations explode, or simply "
                          "the day you bet on autopilot."),
                "rule": (f"Pre-commit your {worst['dow']} stake budget "
                         "before the first race goes off. If you exceed "
                         "it, stop."),
            })
    # 6. Stake variance
    stakes = [float(r.get("stake_hkd", 0)) for r in settled
              if float(r.get("stake_hkd", 0)) > 0]
    if len(stakes) >= 10:
        med = statistics.median(stakes)
        big = [s for s in stakes if s >= 3 * med]
        if big and len(big) / len(stakes) >= 0.10:
            big_bets = [r for r in settled
                        if float(r.get("stake_hkd", 0)) >= 3 * med]
            stake = sum(float(r["stake_hkd"]) for r in big_bets)
            ret = sum(float(r.get("return_hkd", 0)) for r in big_bets)
            roi = (ret - stake) / stake if stake else 0.0
            if roi < 0:
                out.append({
                    "title": "Your big-stake bets lose money",
                    "quantified": (f"{len(big_bets)} bets ≥ 3× median "
                                   f"stake (${med:.0f}) — ROI "
                                   f"{roi*100:+.1f}%, "
                                   f"PnL {_fmt_money(ret-stake)}"),
                    "pnl_impact": ret - stake,
                    "cause": ("Conviction bets that fail at a worse rate "
                              "than your baseline = your highest-conviction "
                              "reads are mis-calibrated."),
                    "rule": (f"Cap any single bet at 2× your median stake "
                             f"(${med*2:.0f}) until your big-stake ROI is "
                             "positive over 20+ bets."),
                })
    # Rank by P&L impact (most negative = worst)
    out.sort(key=lambda x: (x["pnl_impact"] if x["pnl_impact"] is not None
                            else 0))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────
def analyze_betting_period(rows: list[dict], start_date, end_date,
                           *, sport_filter=None, market_filter=None,
                           bet_type_filter=None) -> dict:
    """Return ``{"json": <structured>, "markdown": <human-readable>}``."""
    start = _to_date(start_date)
    end = _to_date(end_date)
    if start is None or end is None or start > end:
        raise ValueError("invalid date range")

    in_range = _filter_rows(rows, start, end,
                            sport_filter=sport_filter,
                            market_filter=market_filter,
                            bet_type_filter=bet_type_filter)
    settled = [r for r in in_range if r.get("status") == "settled"]
    open_ = [r for r in in_range if r.get("status") != "settled"]

    snap = _snapshot(settled)
    by_type = _by_bet_type(settled)
    by_odds = _by_odds_bucket(settled)
    by_dow = _by_dow(settled)
    chase = _loss_chasing(settled)
    align = _model_alignment(settled)
    strengths = _identify_strengths(by_type, by_odds, align)
    weaknesses = _identify_weaknesses(snap, by_type, by_odds, by_dow,
                                      chase, align, settled)

    # One-paragraph verdict
    verdict = _verdict(snap, weaknesses, align)

    rep_json = {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "filters": {
            "sport": sport_filter, "market": market_filter,
            "bet_type": list(bet_type_filter or []),
        },
        "n_in_range": len(in_range),
        "n_settled": len(settled),
        "n_open": len(open_),
        "snapshot": snap,
        "by_bet_type": by_type,
        "by_odds_bucket": by_odds,
        "by_dow": by_dow,
        "loss_chasing": chase,
        "model_alignment": align,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "verdict": verdict,
        "data_gaps": _data_gaps(settled, align),
    }
    md = _render_markdown(rep_json)
    return {"json": rep_json, "markdown": md}


def _data_gaps(settled: list[dict], align: dict) -> list[str]:
    gaps = []
    if len(settled) < 30:
        gaps.append(f"Only {len(settled)} settled bets in range — "
                    "patterns are suggestive, not statistically certain. "
                    "Need ≥30 bets per bet-type before treating findings "
                    "as confirmed.")
    if align["no_report"]["n"]:
        gaps.append(f"{align['no_report']['n']} bets have no cached model "
                    "report — ET/SARR alignment numbers exclude these.")
    if not any(r.get("return_hkd") and r.get("hit") for r in settled):
        gaps.append("No hits in range — odds-band analysis is empty.")
    gaps.append("Closing-line value (CLV) is not yet tracked. Once "
                "pre-race odds snapshots are wired in, CLV will replace "
                "raw ROI as the primary edge signal.")
    return gaps


def _verdict(snap: dict, weaknesses: list[dict], align: dict) -> str:
    if not snap["n_bets"]:
        return ("No bets in range. Nothing to analyse — log some "
                "wagers and come back.")
    worst = weaknesses[0] if weaknesses else None
    overall_kind = ("profitable" if snap["roi"] > 0.02
                    else "break-even" if snap["roi"] > -0.05
                    else "losing")
    main_problem = (worst["title"] if worst
                    else "you don't have a single dominant leak — your "
                         "issue is volume in marginal +EV spots")
    e = align["aligned_both"]
    dv = align["deviated"]
    fix = ""
    if dv["n"] >= 5 and dv["pnl"] < 0 and (
            not e["n"] or e["roi"] > dv["roi"]):
        fix = ("Stop overriding the model. When ET and SARR both list a "
               "horse in the top 3, that is your highest-EV input — "
               "everything else is noise unless you can write down why.")
    elif worst and worst.get("rule"):
        fix = worst["rule"]
    else:
        fix = ("Tighten stake discipline and commit to a written rule "
               "before each meeting. Discipline > insight at your "
               "current sample size.")
    return (
        f"You are a {overall_kind} bettor over this window "
        f"({snap['n_bets']} bets, ROI {snap['roi']*100:+.1f}%, "
        f"PnL {_fmt_money(snap['pnl'])}). Your single biggest problem "
        f"is: {main_problem}. The one change that moves the needle "
        f"most: {fix}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Markdown renderer
# ─────────────────────────────────────────────────────────────────────────────
def _render_markdown(j: dict) -> str:
    s = j["snapshot"]
    lines = []
    lines.append(f"# Forensic bet review · "
                 f"{j['period']['start']} → {j['period']['end']}")
    lines.append("")
    lines.append("## 1 · Performance snapshot")
    lines.append(f"- Bets in range: **{j['n_in_range']}** "
                 f"(settled {j['n_settled']}, open {j['n_open']})")
    lines.append(f"- Hit rate: **{s['win_rate']*100:.1f}%** "
                 f"({s['n_hits']}/{s['n_bets']})")
    lines.append(f"- Stake: **${s['stake']:,.0f}** · "
                 f"Return: **${s['return']:,.0f}**")
    lines.append(f"- Net P&L: **{_fmt_money(s['pnl'])}** · "
                 f"ROI **{s['roi']*100:+.1f}%**")
    lines.append(f"- Avg stake: ${s['avg_stake']:.0f} · "
                 f"Avg winning payout multiple: "
                 f"{s['avg_winning_payout_mult']:.2f}x")
    lines.append("")
    a = j["model_alignment"]
    if a["n_total_with_report"]:
        lines.append("**Model alignment (bets with a cached report)**")
        for key, label in [("aligned_both",       "ET ∩ SARR overlap"),
                            ("aligned_et_or_sarr", "ET or SARR (either)"),
                            ("deviated",          "Deviated from both")]:
            x = a[key]
            lines.append(f"- {label}: {x['n']} bets · ROI "
                         f"{x['roi']*100:+.1f}% · "
                         f"PnL {_fmt_money(x['pnl'])}")
        lines.append("")
    lines.append("> CLV not yet tracked. Add pre-race odds snapshots to "
                 "unlock closing-line analysis.")
    lines.append("")

    # Strengths
    lines.append("## 2 · Strengths")
    if not j["strengths"]:
        lines.append("None statistically convincing in this window.")
    else:
        for x in j["strengths"]:
            lines.append(f"- **{x['title']}** — {x['evidence']}")
    lines.append("")

    # Weaknesses
    lines.append("## 3 · Weaknesses (ranked by P&L impact)")
    if not j["weaknesses"]:
        lines.append("No statistically meaningful leaks in this window — "
                     "either you are disciplined or the sample is too "
                     "small to detect them.")
    else:
        for i, w in enumerate(j["weaknesses"], 1):
            lines.append(f"### {i}. {w['title']}")
            lines.append(f"- **Damage:** {w['quantified']}")
            lines.append(f"- **Likely cause:** {w['cause']}")
            lines.append(f"- **Rule to enforce:** {w['rule']}")
    lines.append("")

    # Exploit strengths
    lines.append("## 4 · How to exploit strengths")
    if not j["strengths"]:
        lines.append("Insufficient evidence of a repeatable edge. "
                     "Until one appears, treat every bet as if you have "
                     "no edge — i.e. flat-stake the model picks only.")
    else:
        for x in j["strengths"]:
            lines.append(f"- Continue concentrating volume on **{x['title']}**. "
                         f"Edge ≈ {x['edge_pct']:+.1f}pp. Bias future stake "
                         "growth into this segment first.")
    lines.append("")

    # Fix weaknesses
    lines.append("## 5 · How to fix weaknesses — immediate action plan")
    if not j["weaknesses"]:
        lines.append("Nothing actionable yet — keep logging bets.")
    else:
        for i, w in enumerate(j["weaknesses"], 1):
            lines.append(f"{i}. **{w['title']}** → {w['rule']}")
    lines.append("")

    # Alignment audit
    lines.append("## 6 · Model alignment audit")
    if a["n_total_with_report"] == 0:
        lines.append("No cached model reports for the bets in this range.")
    else:
        total = a["n_total_with_report"]
        pct_ali = (a["aligned_both"]["n"]
                   + a["aligned_et_or_sarr"]["n"]) / total * 100
        pct_dev = a["deviated"]["n"] / total * 100
        lines.append(f"- {pct_ali:.0f}% of your bets contained at least "
                     f"one model top-3 horse; {pct_dev:.0f}% deviated "
                     "entirely.")
        e = a["aligned_both"]; dv = a["deviated"]
        if dv["n"] >= 5 and e["n"] >= 5:
            verdict = ("destroying alpha" if dv["roi"] < e["roi"]
                       else "adding alpha")
            lines.append(f"- Override accuracy verdict: **you are "
                         f"{verdict}** ({dv['roi']*100:+.1f}% on overrides "
                         f"vs {e['roi']*100:+.1f}% on ensemble-aligned).")
    lines.append("")

    # Verdict
    lines.append("## 7 · Verdict")
    lines.append(j["verdict"])
    lines.append("")

    # Data gaps
    if j["data_gaps"]:
        lines.append("## Data gaps & caveats")
        for g in j["data_gaps"]:
            lines.append(f"- {g}")

    return "\n".join(lines)
