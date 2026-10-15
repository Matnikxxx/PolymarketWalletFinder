"""
Reconstructs a wallet's trading history from its normalized activity feed
and derives the statistics needed for ranking/verification (spec sections
3 and 22): ROI, win rate, drawdown, consistency, concentration, position
count, average size, category mix, simultaneous-position tracking.

This is a *read-only historical analysis* of a target wallet -- separate
from trading/position_manager.py, which manages OUR paper positions.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from statistics import pstdev, mean
from typing import Optional


@dataclass
class ReconstructedPosition:
    key: str  # condition_id/token_id
    outcome: Optional[str]
    category: str = "unknown"
    total_bought: float = 0.0
    total_bought_cost: float = 0.0
    total_sold: float = 0.0
    total_sold_proceeds: float = 0.0
    remaining_shares: float = 0.0
    opened_at: Optional[str] = None
    last_activity_at: Optional[str] = None
    closed: bool = False
    realized_pnl: float = 0.0

    @property
    def avg_entry(self) -> float:
        return self.total_bought_cost / self.total_bought if self.total_bought else 0.0


class DataQuality:
    """
    How much of a WalletStats object is backed by real data, so callers
    (main.py::cmd_scan_wallets, the dashboard) can decide when to persist/
    show a real number vs. an honest N/A -- instead of a fake 0.0 that
    looks like a real, computed zero.
    """
    FULL = "FULL"  # authoritative /closed-positions data used for P&L/win-rate
    ACTIVITY_ONLY = "ACTIVITY_ONLY"  # only /activity fills; P&L/win-rate are our own reconstruction
    PARTIAL = "PARTIAL"  # some activity, but too thin to trust (e.g. no closed positions yet)
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # no trade activity at all
    API_UNAVAILABLE = "API_UNAVAILABLE"  # every relevant endpoint failed


@dataclass
class WalletStats:
    wallet: str
    total_events: int = 0
    total_positions: int = 0
    closed_positions: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    total_invested: float = 0.0
    total_realized_pnl: float = 0.0
    roi_pct: float = 0.0
    win_rate_pct: float = 0.0
    avg_position_size: float = 0.0
    max_drawdown_pct: float = 0.0
    consistency_score: float = 0.0
    concentration_pct: float = 0.0  # % of total profit from single biggest win
    category_mix: dict = field(default_factory=dict)
    first_activity_at: Optional[str] = None
    last_activity_at: Optional[str] = None
    active_days: int = 0
    max_simultaneous_positions: int = 0
    avg_simultaneous_positions: float = 0.0
    is_resolution_only: bool = False
    per_trade_pnls: list = field(default_factory=list)

    # --- richer fields (this pass) ------------------------------------
    buy_trades: int = 0
    sell_trades: int = 0
    total_volume: float = 0.0  # sum of usdc_size across all activity fills
    largest_trade: float = 0.0  # largest single fill's usdc_size
    active_positions: Optional[int] = None  # from /positions; None if not fetched/unavailable
    portfolio_value: Optional[float] = None  # from /value; None if not fetched/unavailable
    data_quality: str = DataQuality.INSUFFICIENT_DATA
    data_quality_reason: str = "No trade activity retrieved for this wallet."
    pnl_source: str = "none"  # "closed_positions_api" | "activity_reconstruction" | "none"


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def compute_wallet_stats(
    wallet: str,
    events: list[dict],
    market_categories: Optional[dict[str, str]] = None,
) -> WalletStats:
    """
    events: normalized activity events (see polymarket.parsers.normalize_activity_event),
            sorted chronologically (oldest first).
    market_categories: optional {condition_id_or_slug: category} lookup so callers
            can batch-resolve categories instead of one API call per event.
    """
    market_categories = market_categories or {}
    positions: dict[str, ReconstructedPosition] = {}
    stats = WalletStats(wallet=wallet, total_events=len(events))

    if not events:
        stats.data_quality = DataQuality.INSUFFICIENT_DATA
        stats.data_quality_reason = "No trade activity retrieved for this wallet."
        return stats

    open_count_timeline: list[int] = []
    category_counter: defaultdict[str, int] = defaultdict(int)

    for ev in events:
        key = ev.get("condition_id") or ev.get("token_id") or ev.get("slug") or ev.get("market_title")
        if not key:
            continue
        pos = positions.get(key)
        if pos is None:
            cat = market_categories.get(key, "unknown")
            pos = ReconstructedPosition(key=key, outcome=ev.get("outcome"), category=cat)
            pos.opened_at = ev.get("timestamp")
            positions[key] = pos
            category_counter[cat] += 1

        pos.last_activity_at = ev.get("timestamp") or pos.last_activity_at
        price = ev.get("price", 0.0)
        size = ev.get("size", 0.0)

        if ev.get("side") == "BUY":
            pos.total_bought += size
            pos.total_bought_cost += price * size
            pos.remaining_shares += size
        elif ev.get("side") == "SELL":
            sell_size = min(size, pos.remaining_shares) if pos.remaining_shares else size
            avg_entry = pos.avg_entry
            proceeds = price * sell_size
            cost_basis = avg_entry * sell_size
            realized = proceeds - cost_basis
            pos.total_sold += sell_size
            pos.total_sold_proceeds += proceeds
            pos.remaining_shares = max(0.0, pos.remaining_shares - sell_size)
            pos.realized_pnl += realized
            if pos.remaining_shares <= 1e-9:
                pos.closed = True

        # simultaneous open-position snapshot after processing this event
        open_now = sum(1 for p in positions.values() if not p.closed and p.remaining_shares > 1e-9)
        open_count_timeline.append(open_now)

    stats.first_activity_at = events[0].get("timestamp")
    stats.last_activity_at = events[-1].get("timestamp")

    first_dt, last_dt = _parse_ts(stats.first_activity_at), _parse_ts(stats.last_activity_at)
    if first_dt and last_dt:
        stats.active_days = max(1, (last_dt - first_dt).days)

    closed = [p for p in positions.values() if p.closed]
    stats.total_positions = len(positions)
    stats.closed_positions = len(closed)
    stats.total_invested = sum(p.total_bought_cost for p in positions.values())
    stats.total_realized_pnl = sum(p.realized_pnl for p in closed)
    stats.per_trade_pnls = [round(p.realized_pnl, 4) for p in closed]

    stats.roi_pct = (
        (stats.total_realized_pnl / stats.total_invested) * 100.0
        if stats.total_invested > 0
        else 0.0
    )

    for p in closed:
        if p.realized_pnl > 0.01:
            stats.wins += 1
        elif p.realized_pnl < -0.01:
            stats.losses += 1
        else:
            stats.breakeven += 1

    decided = stats.wins + stats.losses
    stats.win_rate_pct = (stats.wins / decided * 100.0) if decided else 0.0

    stats.avg_position_size = (
        stats.total_invested / stats.total_positions if stats.total_positions else 0.0
    )

    # Drawdown: running realized-PnL equity curve over closed trades in order,
    # measured against total capital invested (rather than the running peak
    # alone) so early losses aren't hidden by a peak that is still at/near 0.
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    dd_base = stats.total_invested if stats.total_invested > 0 else 1.0
    for pnl in stats.per_trade_pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / dd_base * 100.0)
    stats.max_drawdown_pct = max_dd

    # Concentration: how much of total profit came from the single best trade.
    if stats.total_realized_pnl > 0 and stats.per_trade_pnls:
        best = max(stats.per_trade_pnls)
        stats.concentration_pct = (best / stats.total_realized_pnl * 100.0) if best > 0 else 0.0

    # Consistency: inverse of the coefficient of variation of per-trade PnL,
    # scaled to a 0-10 score. Low variance relative to mean => high consistency.
    if len(stats.per_trade_pnls) >= 3:
        m = mean(stats.per_trade_pnls)
        sd = pstdev(stats.per_trade_pnls)
        if m != 0:
            cv = abs(sd / m)
            stats.consistency_score = max(0.0, min(10.0, 10.0 / (1.0 + cv)))
        else:
            stats.consistency_score = 0.0
    else:
        stats.consistency_score = 0.0

    stats.category_mix = dict(category_counter)

    # Resolution-only heuristic: wallet only ever appears with SELL-less BUY
    # events that get resolved (i.e., extremely few/no active SELL exits).
    total_sells = sum(1 for ev in events if ev.get("side") == "SELL")
    stats.is_resolution_only = total_sells == 0 and stats.total_positions >= 3

    if open_count_timeline:
        stats.max_simultaneous_positions = max(open_count_timeline)
        stats.avg_simultaneous_positions = round(mean(open_count_timeline), 2)

    # --- richer counts, always derivable straight from activity fills ----
    stats.buy_trades = sum(1 for ev in events if ev.get("side") == "BUY")
    stats.sell_trades = sum(1 for ev in events if ev.get("side") == "SELL")
    stats.total_volume = round(sum(ev.get("usdc_size", 0.0) or 0.0 for ev in events), 2)
    stats.largest_trade = round(max((ev.get("usdc_size", 0.0) or 0.0) for ev in events), 2)

    # --- baseline data quality (may be upgraded to FULL by
    # enrich_with_closed_positions() once/if authoritative data is fetched) ---
    if stats.closed_positions > 0:
        stats.data_quality = DataQuality.ACTIVITY_ONLY
        stats.data_quality_reason = (
            "Win/loss and realized P&L reconstructed from raw activity fills "
            "(FIFO cost basis) -- not yet confirmed against Polymarket's own "
            "/closed-positions data."
        )
        stats.pnl_source = "activity_reconstruction"
    else:
        stats.data_quality = DataQuality.PARTIAL
        stats.data_quality_reason = (
            f"{stats.total_positions} position(s) seen, but none are closed yet -- "
            "no realized P&L, win rate, or drawdown to report."
        )
        stats.pnl_source = "none"

    return stats


def enrich_with_closed_positions(
    stats: WalletStats, raw_closed_positions: list[dict], api_ok: bool
) -> WalletStats:
    """
    Overrides wins/losses/win_rate/realized-P&L/ROI/drawdown/concentration
    with Polymarket's own authoritative /closed-positions data
    (realizedPnl, avgPrice, totalBought per position) when available.

    This is preferred over the activity-fill reconstruction in
    compute_wallet_stats() because Polymarket's own figure already accounts
    for partial fills, resolutions, and anything else our own FIFO
    reconstruction from raw fills can't see. When this succeeds with at
    least one closed position, data_quality is upgraded to FULL.

    Formulas (documented per spec item 4/5):
      cost_basis(position)  = avg_price * total_bought
      roi_pct                = total_realized_pnl / sum(cost_basis) * 100
      win / loss              = realized_pnl > +$0.01 / < -$0.01 (breakeven between)
      win_rate_pct             = wins / (wins + losses) * 100
      max_drawdown_pct          = running peak-to-trough dip of the cumulative
                                   realized-P&L equity curve (closed positions
                                   ordered oldest-first by their `timestamp`
                                   field), expressed as % of total cost basis.
                                   NOTE: Polymarket's OpenAPI spec does not
                                   document exactly what real-world event
                                   `timestamp` marks for a ClosedPosition; we
                                   assume it is close to the position's close
                                   time (it's the default sort key alongside
                                   REALIZEDPNL) -- if that assumption turns
                                   out wrong, drawdown ordering would be off,
                                   though the total P&L/ROI/win-rate numbers
                                   are unaffected either way.

    If `api_ok` is False (the API call itself failed) or the list is empty
    (wallet genuinely has zero closed positions), the activity-reconstructed
    stats are left untouched -- this function only ever upgrades data
    quality, never silently downgrades a number to worse information.
    """
    if not api_ok:
        stats.data_quality_reason += " (/closed-positions API call failed; using activity-only estimate.)"
        return stats

    if not raw_closed_positions:
        # Genuinely zero closed positions -- nothing to enrich with, but
        # this IS real information (not a failure), so no reason to
        # downgrade or change data_quality here.
        return stats

    from polymarket.parsers import normalize_closed_position

    normalized = [normalize_closed_position(r) for r in raw_closed_positions]
    normalized = [n for n in normalized if n.get("condition_id")]
    if not normalized:
        return stats

    pnls = [n["realized_pnl"] for n in normalized]
    cost_bases = [n["cost_basis"] for n in normalized]
    total_cost_basis = sum(cost_bases)
    total_realized = sum(pnls)

    wins = sum(1 for p in pnls if p > 0.01)
    losses = sum(1 for p in pnls if p < -0.01)
    breakeven = len(pnls) - wins - losses
    decided = wins + losses

    stats.closed_positions = len(normalized)
    stats.total_realized_pnl = round(total_realized, 4)
    stats.per_trade_pnls = [round(p, 4) for p in pnls]
    stats.wins = wins
    stats.losses = losses
    stats.breakeven = breakeven
    stats.win_rate_pct = round((wins / decided * 100.0), 2) if decided else 0.0
    stats.roi_pct = round((total_realized / total_cost_basis * 100.0), 2) if total_cost_basis > 0 else 0.0

    if total_realized > 0 and pnls:
        best = max(pnls)
        stats.concentration_pct = round((best / total_realized * 100.0), 2) if best > 0 else 0.0

    # Drawdown, ordered oldest-first by each position's own timestamp
    # (see docstring note on what that timestamp is assumed to represent).
    ordered = sorted(normalized, key=lambda n: n.get("timestamp") or "")
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    dd_base = total_cost_basis if total_cost_basis > 0 else 1.0
    for n in ordered:
        equity += n["realized_pnl"]
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / dd_base * 100.0)
    stats.max_drawdown_pct = round(max_dd, 2)

    # Recompute consistency from these SAME authoritative per-position PnLs
    # -- same CV-based formula as compute_wallet_stats() uses for the
    # activity-reconstruction case, applied here too. Without this,
    # consistency_score would stay stuck at whatever compute_wallet_stats()
    # computed from the (often much smaller/less reliable) activity-only
    # reconstruction, even after per_trade_pnls above was just replaced
    # with better data -- a wallet with e.g. 20 highly consistent
    # authoritative closed positions could otherwise be stuck showing the
    # stale consistency_score from an activity reconstruction that only
    # saw 2 of them.
    if len(pnls) >= 3:
        pnl_mean = mean(pnls)
        pnl_sd = pstdev(pnls)
        if pnl_mean != 0:
            cv = abs(pnl_sd / pnl_mean)
            stats.consistency_score = max(0.0, min(10.0, 10.0 / (1.0 + cv)))
        else:
            stats.consistency_score = 0.0
    # else: fewer than 3 authoritative closed positions -- leave
    # consistency_score as compute_wallet_stats() set it (also 0.0 in that
    # case, since the same >=3 rule applies there). Scoring layer
    # (wallet/ranking.py) is responsible for treating a low sample size as
    # uncertainty rather than a proven-bad consistency score.

    stats.data_quality = DataQuality.FULL
    stats.data_quality_reason = (
        f"Win/loss, realized P&L, ROI, and drawdown computed from "
        f"{len(normalized)} authoritative closed position(s) via Polymarket's "
        f"/closed-positions endpoint."
    )
    stats.pnl_source = "closed_positions_api"
    return stats


def attach_open_positions(stats: WalletStats, raw_positions: list[dict], api_ok: bool) -> WalletStats:
    """Sets active_positions from GET /positions. None (not 0) if the call failed."""
    if not api_ok:
        stats.active_positions = None
        return stats
    stats.active_positions = len(raw_positions)
    return stats


def attach_portfolio_value(stats: WalletStats, value: Optional[float]) -> WalletStats:
    """Sets portfolio_value from GET /value. None if unavailable -- never fabricated."""
    stats.portfolio_value = value
    return stats
