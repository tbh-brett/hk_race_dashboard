"""
bankroll.py — Bankroll dynamics, risk metrics, and walk-forward splits
=======================================================================

Phase A of the high-aggression strategy stack.

What this module does:
    - Replays a list of settled bets (any granularity: per-bet, per-race,
      per-meeting) into an equity curve.
    - Reports growth and risk metrics that matter for "long-term capital
      growth", not hit rate:
        * terminal bankroll, total ROI, geometric mean return per bet
        * max drawdown (peak-to-trough), longest underwater stretch
        * volatility of per-bet log-returns
        * Sharpe-like ratio   = mean(log_return) / std(log_return)  * sqrt(N)
        * Sortino-like ratio  = mean(log_return) / std(neg_log_return) * sqrt(N)
        * Calmar ratio        = total_ROI / max_drawdown
    - Provides walk-forward train/test splits for backtests, so we can
      detect (and refuse to deploy) strategies that only worked in-sample.

Design notes:
    - We use *log returns* of bankroll for volatility math because the
      whole point of geometric growth optimisation is multiplicative.
      A flat additive view would mask compounding effects.
    - Sample size on April 2026 is tiny (~7 meetings). Every metric
      below is therefore reported with `n_samples` so the caller can
      sanity-check before betting real money.
    - This module is **pure functions over data**. It does not load
      or save anything itself.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, asdict
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------
@dataclass
class Bet:
    """One settled bet.

    Attributes
    ----------
    key : str
        Free-form identifier (e.g. "20260412/R3/QIN_BANKER").
    stake : float
        HKD risked. Must be > 0 for the bet to count toward metrics.
    payout : float
        HKD returned (incl. stake on a winner). 0 on loss.
    timestamp : str
        ISO date, used for chronological ordering.
    tag : str
        Strategy tag (mode, pool, filter) for grouping.
    """
    key: str
    stake: float
    payout: float
    timestamp: str = ""
    tag: str = ""

    @property
    def pnl(self) -> float:
        return self.payout - self.stake

    @property
    def gross_return(self) -> float:
        """Multiplier on staked capital. 0 = total loss, 1 = break-even, >1 = win."""
        if self.stake <= 0:
            return 1.0
        return self.payout / self.stake


@dataclass
class EquityPoint:
    idx: int
    timestamp: str
    bankroll: float
    drawdown_frac: float   # (peak - bankroll) / peak, ≥ 0
    bet_key: str = ""


@dataclass
class BankrollMetrics:
    n_bets: int
    n_hits: int
    hit_rate: float
    total_stake: float
    total_payout: float
    starting_bankroll: float
    ending_bankroll: float
    total_roi: float          # (end - start) / start
    geom_return_per_bet: float  # (end/start)^(1/n) - 1; "compound rate"
    log_return_mean: float
    log_return_std: float
    log_return_neg_std: float
    sharpe_like: float        # 0 if std==0 or n<2
    sortino_like: float
    max_drawdown_frac: float
    max_drawdown_hkd: float
    longest_underwater: int   # bets between peak and recovery
    calmar: float             # total_roi / max_drawdown (capped if MDD≈0)


# ---------------------------------------------------------------------------
# Equity curve replay
# ---------------------------------------------------------------------------
def replay(
    bets: Iterable[Bet],
    starting_bankroll: float,
    *,
    bankrupt_floor: float = 0.0,
) -> tuple[list[EquityPoint], BankrollMetrics]:
    """Replay bets in iteration order; produce equity curve + metrics.

    The bets are NOT re-sorted: the caller is responsible for chronological
    order. (We avoid implicit sorts to keep walk-forward splits honest.)

    `bankrupt_floor` stops betting once bankroll falls below this value. Any
    further bets are recorded with stake=0/payout=0 (idle), so equity stays
    flat. Default 0 means "never bankrupt unless bankroll <= 0".
    """
    bets = list(bets)
    bankroll = float(starting_bankroll)
    peak = bankroll
    points: list[EquityPoint] = [EquityPoint(0, "", bankroll, 0.0, "<start>")]
    log_returns: list[float] = []
    n_hits = 0
    total_stake = 0.0
    total_payout = 0.0

    underwater_streak = 0
    longest_underwater = 0

    for i, b in enumerate(bets, start=1):
        if bankroll <= bankrupt_floor or b.stake <= 0:
            # Idle: no bankroll change.
            points.append(EquityPoint(i, b.timestamp, bankroll,
                                      _dd_frac(peak, bankroll), b.key))
            if bankroll < peak:
                underwater_streak += 1
                longest_underwater = max(longest_underwater, underwater_streak)
            else:
                underwater_streak = 0
            continue

        prev = bankroll
        bankroll = bankroll - b.stake + b.payout
        if bankroll <= 0:
            bankroll = 0.0  # bust

        total_stake += b.stake
        total_payout += b.payout
        if b.payout > b.stake:
            n_hits += 1

        if prev > 0 and bankroll > 0:
            log_returns.append(math.log(bankroll / prev))
        elif bankroll == 0 and prev > 0:
            # Treat bust as -ln(very large) — practical sentinel
            log_returns.append(-10.0)

        if bankroll > peak:
            peak = bankroll
            underwater_streak = 0
        else:
            underwater_streak += 1
            longest_underwater = max(longest_underwater, underwater_streak)

        points.append(EquityPoint(i, b.timestamp, bankroll,
                                  _dd_frac(peak, bankroll), b.key))

    metrics = _compute_metrics(
        bets=bets, log_returns=log_returns, points=points,
        starting_bankroll=starting_bankroll, ending_bankroll=bankroll,
        n_hits=n_hits, total_stake=total_stake, total_payout=total_payout,
        longest_underwater=longest_underwater,
    )
    return points, metrics


def _dd_frac(peak: float, current: float) -> float:
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - current) / peak)


def _compute_metrics(*, bets, log_returns, points, starting_bankroll,
                     ending_bankroll, n_hits, total_stake, total_payout,
                     longest_underwater) -> BankrollMetrics:
    n = len(bets)
    n_settled = len([b for b in bets if b.stake > 0])

    if starting_bankroll > 0 and ending_bankroll > 0 and n_settled > 0:
        geom = (ending_bankroll / starting_bankroll) ** (1 / n_settled) - 1
    else:
        geom = -1.0 if ending_bankroll == 0 else 0.0

    if len(log_returns) >= 2:
        mu = statistics.mean(log_returns)
        sd = statistics.pstdev(log_returns)
        neg = [r for r in log_returns if r < 0]
        sd_neg = statistics.pstdev(neg) if len(neg) >= 2 else 0.0
        sharpe = (mu / sd) * math.sqrt(len(log_returns)) if sd > 0 else 0.0
        sortino = (mu / sd_neg) * math.sqrt(len(log_returns)) if sd_neg > 0 else 0.0
    else:
        mu = sd = sd_neg = sharpe = sortino = 0.0

    mdd_frac = max((p.drawdown_frac for p in points), default=0.0)
    # MDD in HKD: search for the largest peak->trough $ drop
    peak = starting_bankroll
    mdd_hkd = 0.0
    for p in points:
        if p.bankroll > peak:
            peak = p.bankroll
        mdd_hkd = max(mdd_hkd, peak - p.bankroll)

    total_roi = (ending_bankroll - starting_bankroll) / starting_bankroll \
        if starting_bankroll > 0 else 0.0

    if mdd_frac > 1e-9:
        calmar = total_roi / mdd_frac
    else:
        calmar = float("inf") if total_roi > 0 else 0.0

    return BankrollMetrics(
        n_bets=n_settled,
        n_hits=n_hits,
        hit_rate=(n_hits / n_settled) if n_settled else 0.0,
        total_stake=round(total_stake, 2),
        total_payout=round(total_payout, 2),
        starting_bankroll=round(starting_bankroll, 2),
        ending_bankroll=round(ending_bankroll, 2),
        total_roi=round(total_roi, 4),
        geom_return_per_bet=round(geom, 5),
        log_return_mean=round(mu, 5),
        log_return_std=round(sd, 5),
        log_return_neg_std=round(sd_neg, 5),
        sharpe_like=round(sharpe, 3),
        sortino_like=round(sortino, 3),
        max_drawdown_frac=round(mdd_frac, 4),
        max_drawdown_hkd=round(mdd_hkd, 2),
        longest_underwater=longest_underwater,
        calmar=round(calmar, 3) if math.isfinite(calmar) else float("inf"),
    )


# ---------------------------------------------------------------------------
# Walk-forward split
# ---------------------------------------------------------------------------
def walk_forward_splits(
    items: list,
    *,
    n_splits: int = 4,
    min_train: int = 1,
) -> list[tuple[list, list]]:
    """Generate expanding-window train/test splits.

    Given an ordered list (e.g. meetings sorted by date), yield:
        [(train[0:k], test[k:k+step]), ...]

    Where k starts at min_train and grows in (len-min_train)/n_splits chunks.

    Useful for: training edge thresholds on weeks 1..k, evaluating on
    week k+1, sliding forward — to detect overfit configs.
    """
    n = len(items)
    if n < min_train + 1 or n_splits < 1:
        return []
    test_size = max(1, (n - min_train) // n_splits)
    splits: list[tuple[list, list]] = []
    k = min_train
    while k < n:
        end = min(k + test_size, n)
        splits.append((items[:k], items[k:end]))
        k = end
        if len(splits) >= n_splits:
            break
    return splits


# ---------------------------------------------------------------------------
# Convenience: summarise per-tag breakdowns
# ---------------------------------------------------------------------------
def by_tag(bets: list[Bet]) -> dict[str, BankrollMetrics]:
    """Group bets by `.tag` and run replay independently on each.

    Each group starts with a virtual bankroll of 1000 (so ROIs are
    comparable) — the absolute terminal value is informational; what
    matters is geom_return_per_bet, sharpe_like, max_drawdown_frac.
    """
    groups: dict[str, list[Bet]] = {}
    for b in bets:
        groups.setdefault(b.tag or "untagged", []).append(b)
    out: dict[str, BankrollMetrics] = {}
    for tag, lst in groups.items():
        _, m = replay(lst, starting_bankroll=1000.0)
        out[tag] = m
    return out


def metrics_to_dict(m: BankrollMetrics) -> dict:
    return asdict(m)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # 5 fake bets: 1 winner @ 5x, 4 losers
    sample = [
        Bet("R1", 100, 0,   "2026-04-01", "test"),
        Bet("R2", 100, 500, "2026-04-01", "test"),  # +400
        Bet("R3", 100, 0,   "2026-04-08", "test"),
        Bet("R4", 100, 0,   "2026-04-12", "test"),
        Bet("R5", 100, 0,   "2026-04-15", "test"),
    ]
    pts, met = replay(sample, starting_bankroll=1000)
    print("ending bankroll:", met.ending_bankroll)
    print("total ROI:     ", met.total_roi)
    print("geom/bet:      ", met.geom_return_per_bet)
    print("sharpe-like:   ", met.sharpe_like)
    print("max DD frac:   ", met.max_drawdown_frac)
    print("max DD HKD:    ", met.max_drawdown_hkd)
    print("calmar:        ", met.calmar)
