"""
Core metrics and statistical calculations for Phase 6 wallet analytics.

Implements all approved formulas:
- Exact fee-exclusive ROI
- Net-result based win rate (partials excluded from denominator)
- Maximum drawdown (dollar and percentage against settled cost and peak)
- Gross profit concentration (Top-1 and Top-3 share)
- Position sizing and return consistency dispersion
- Clean vs sensitivity diagnostics
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Dict, Any

from analytics.models import (
    PositionRecord,
    PositionEligibility,
    PositionSettlementType,
    PositionSettlementClass,
    WalletStatistics,
    PopulationAccounting,
)
from analytics.eligibility import partition_positions


def _parse_utc_date(ts_str: Optional[str]) -> Optional[str]:
    """Extracts UTC YYYY-MM-DD from an ISO timestamp string."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def calculate_drawdown(
    positions: List[PositionEligibility],
    total_cost: float,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Calculates running peak-to-trough drawdown over positions ordered by timestamp.
    Same-timestamp positions are netted into a single step delta.

    Returns:
        (max_drawdown_usd, drawdown_cost_pct, drawdown_peak_pct)
        Returns (None, None, None) if any position lacks a timestamp.
    """
    if not positions:
        return 0.0, (0.0 if total_cost > 0 else None), None

    # If any eligible position has no timestamp, time-series drawdown is undefined
    if any(p.effective_timestamp is None for p in positions):
        return None, None, None

    # Group net results by timestamp
    grouped: Dict[str, float] = defaultdict(float)
    for p in positions:
        grouped[p.effective_timestamp] += (p.net_result or 0.0)

    # Sort timestamps chronologically
    sorted_ts = sorted(grouped.keys())

    equity = 0.0
    peak = 0.0
    max_dd_usd = 0.0

    for ts in sorted_ts:
        delta = grouped[ts]
        equity += delta
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd_usd:
            max_dd_usd = dd

    max_dd_usd_round = round(max_dd_usd, 6)
    dd_cost_pct = (
        round(100.0 * max_dd_usd / total_cost, 4) if total_cost > 0 else None
    )
    dd_peak_pct = (
        round(100.0 * max_dd_usd / peak, 4) if peak > 0 else None
    )

    return max_dd_usd_round, dd_cost_pct, dd_peak_pct


def compute_wallet_statistics(
    wallet: str,
    positions: List[PositionRecord],
    is_complete_history: bool = True,
    reconciled: bool = True,
    as_of: Optional[str] = None,
    tolerance: float = 0.01,
) -> WalletStatistics:
    """
    Computes all Phase 6 core statistics for a wallet based on the approved rules.
    """
    evaluations, accounting = partition_positions(positions, tolerance=tolerance)
    eligible = [e for e in evaluations if e.is_eligible]

    # --- Core Counts (Clean Data) ---
    positions_counted = len(eligible)
    wins = accounting.wins_count
    losses = accounting.losses_count
    breakeven = accounting.breakeven_count
    partials = accounting.partial_count

    # Win rate: wins / (wins + losses). Undefined (None) if denominator is zero.
    decided = wins + losses
    win_rate_pct = round(100.0 * wins / decided, 4) if decided > 0 else None

    # --- Financials (Fee-Exclusive) ---
    total_settled_cost = round(sum(e.cost for e in eligible), 6)
    total_settled_net_result = round(sum((e.net_result or 0.0) for e in eligible), 6)
    roi_fee_exclusive_pct = (
        round(100.0 * total_settled_net_result / total_settled_cost, 4)
        if total_settled_cost > 0
        else None
    )

    # --- Drawdown (Clean Data) ---
    max_dd_usd, dd_cost_pct, dd_peak_pct = calculate_drawdown(
        eligible, total_settled_cost
    )

    # --- Profit Concentration ---
    positive_pnls = sorted(
        [e.net_result for e in eligible if e.net_result is not None and e.net_result > 0.0],
        reverse=True,
    )
    gross_profit = sum(positive_pnls)
    if gross_profit > 0 and positive_pnls:
        top1_profit_share_pct = round(100.0 * positive_pnls[0] / gross_profit, 4)
        top3_profit_share_pct = round(100.0 * sum(positive_pnls[:3]) / gross_profit, 4)
    else:
        top1_profit_share_pct = None
        top3_profit_share_pct = None

    # --- Position Sizing ---
    costs = [e.cost for e in eligible]
    if costs:
        mean_position_cost = round(statistics.mean(costs), 4)
        median_position_cost = round(statistics.median(costs), 4)
        max_position_cost = round(max(costs), 4)
        stdev_position_cost = (
            round(statistics.stdev(costs), 4) if len(costs) >= 2 else None
        )
    else:
        mean_position_cost = median_position_cost = max_position_cost = stdev_position_cost = None

    # --- Consistency / Return Dispersion (rho = r / c) ---
    if len(eligible) >= 3:
        rhos = [(e.net_result / e.cost) for e in eligible]
        consistency_mean_roi = round(statistics.mean(rhos), 6)
        consistency_median_roi = round(statistics.median(rhos), 6)
        consistency_stdev_roi = round(statistics.stdev(rhos), 6)
    else:
        consistency_mean_roi = consistency_median_roi = consistency_stdev_roi = None

    # --- Activity & Timing ---
    all_timestamps: List[str] = []
    distinct_dates = set()
    for p in positions:
        for ts in (p.opened_at, p.closed_at, p.resolved_at):
            if ts:
                all_timestamps.append(ts)
                d = _parse_utc_date(ts)
                if d:
                    distinct_dates.add(d)

    active_days = len(distinct_dates)
    first_activity_at = min(all_timestamps) if all_timestamps else None
    last_activity_at = max(all_timestamps) if all_timestamps else None

    # --- Data Quality & Coverage ---
    coverage_ratio = (
        round(positions_counted / accounting.ordinary_positions, 4)
        if accounting.ordinary_positions > 0
        else 0.0
    )

    if not is_complete_history or not reconciled:
        retrieval_status = "PARTIAL"
    elif positions_counted == 0:
        retrieval_status = "INSUFFICIENT"
    else:
        retrieval_status = "SUFFICIENT"

    # --- Sensitivity View (Including Repeat-Exposed Positions) ---
    sens_evals = [
        e for e in evaluations
        if e.is_eligible or (e.bucket == "REPEAT_EXPOSED" and e.cost > 0 and e.net_result is not None)
    ]
    sens_counted = len(sens_evals)
    sens_wins = sum(1 for e in sens_evals if not e.is_partial and (e.net_result or 0.0) > tolerance)
    sens_losses = sum(1 for e in sens_evals if not e.is_partial and (e.net_result or 0.0) < -tolerance)
    sens_breakeven = sum(1 for e in sens_evals if not e.is_partial and abs(e.net_result or 0.0) <= tolerance)
    sens_partials = sum(1 for e in sens_evals if e.is_partial)

    sens_decided = sens_wins + sens_losses
    sens_win_rate_pct = round(100.0 * sens_wins / sens_decided, 4) if sens_decided > 0 else None
    sens_cost = round(sum(e.cost for e in sens_evals), 6)
    sens_net = round(sum((e.net_result or 0.0) for e in sens_evals), 6)
    sens_roi = round(100.0 * sens_net / sens_cost, 4) if sens_cost > 0 else None

    sens_max_dd, sens_dd_cost_pct, _ = calculate_drawdown(sens_evals, sens_cost)

    # --- Hard-Filter Criteria (Phase 8B) ---
    # is_resolution_only: mirrors legacy wallet_stats.py heuristic.
    # Legacy: total_sells == 0 and total_positions >= 3.
    # Phase 6 equivalent: no ordinary position has settlement_type == TRADING_EXIT
    # and there are at least 3 ordinary positions.
    # If there are no positions at all (ordinary_positions == 0), we cannot
    # determine this reliably, so we leave it as None rather than guessing False.
    ordinary_positions = accounting.ordinary_positions
    if ordinary_positions >= 3:
        has_trading_exit = any(
            p.settlement_type == PositionSettlementType.TRADING_EXIT
            for p in positions
            if not p.is_combo
        )
        is_resolution_only: Optional[bool] = not has_trading_exit
    elif ordinary_positions > 0:
        # Some positions but below the minimum count for a reliable determination.
        # Cannot declare resolution-only on thin history — leave None.
        is_resolution_only = None
    else:
        # No positions at all — cannot determine.
        is_resolution_only = None

    # category_mix: position counts per category for all ordinary positions.
    # Matches legacy semantics: category_counter incremented once per position key.
    # Only ordinary (non-combo) positions are counted to stay consistent with
    # what the activity-reconstruction loop in wallet_stats.py counts.
    if ordinary_positions > 0:
        cat_counter: Counter[str] = Counter(
            p.category for p in positions if not p.is_combo
        )
        category_mix: Optional[Dict[str, int]] = dict(cat_counter)
    else:
        # No positions → category information is genuinely unavailable.
        # Return None rather than {} to let the filter distinguish
        # "no data" from "data available, all categories unknown".
        category_mix = None

    return WalletStatistics(
        wallet=wallet,
        as_of=as_of,
        positions_counted=positions_counted,
        wins=wins,
        losses=losses,
        breakeven=breakeven,
        partials=partials,
        win_rate_pct=win_rate_pct,
        total_settled_cost=total_settled_cost,
        total_settled_net_result=total_settled_net_result,
        roi_fee_exclusive_pct=roi_fee_exclusive_pct,
        max_drawdown_usd=max_dd_usd,
        drawdown_cost_pct=dd_cost_pct,
        drawdown_peak_pct=dd_peak_pct,
        top1_profit_share_pct=top1_profit_share_pct,
        top3_profit_share_pct=top3_profit_share_pct,
        mean_position_cost=mean_position_cost,
        median_position_cost=median_position_cost,
        max_position_cost=max_position_cost,
        stdev_position_cost=stdev_position_cost,
        consistency_mean_roi=consistency_mean_roi,
        consistency_median_roi=consistency_median_roi,
        consistency_stdev_roi=consistency_stdev_roi,
        active_days=active_days,
        first_activity_at=first_activity_at,
        last_activity_at=last_activity_at,
        coverage_ratio=coverage_ratio,
        is_complete_history=is_complete_history,
        retrieval_status=retrieval_status,
        reconciled=reconciled,
        accounting=accounting,
        sensitivity_positions_counted=sens_counted,
        sensitivity_wins=sens_wins,
        sensitivity_losses=sens_losses,
        sensitivity_breakeven=sens_breakeven,
        sensitivity_partials=sens_partials,
        sensitivity_win_rate_pct=sens_win_rate_pct,
        sensitivity_settled_cost=sens_cost,
        sensitivity_settled_net=sens_net,
        sensitivity_roi_fee_exclusive_pct=sens_roi,
        sensitivity_max_drawdown_usd=sens_max_dd,
        sensitivity_drawdown_cost_pct=sens_dd_cost_pct,
        is_resolution_only=is_resolution_only,
        category_mix=category_mix,
    )


def build_position_records(
    raw_closed_positions: list[dict],
    events: list[dict],
    raw_open_positions: Optional[list[dict]] = None,
) -> List[PositionRecord]:
    """
    Constructs canonical Phase 6 PositionRecords from raw Polymarket API data:
    - Authoritative closed positions from /closed-positions
    - Activity fills from /activity (to infer exit types, categories, and timestamps)
    - Open positions from /positions (partitioned as UNRESOLVED)
    """
    records: List[PositionRecord] = []

    # Map condition_id / slug -> category from events
    category_map: Dict[str, str] = {}
    for ev in events:
        cat = ev.get("category")
        if cat and cat != "unknown":
            if ev.get("condition_id"):
                category_map[ev["condition_id"]] = cat
            if ev.get("slug"):
                category_map[ev["slug"]] = cat

    # Track which condition_ids / token_ids / slugs had a SELL in events
    sell_cids = {
        (ev.get("condition_id") or ev.get("conditionId")) for ev in events
        if (ev.get("side") == "SELL" or ev.get("action") == "SELL") and (ev.get("condition_id") or ev.get("conditionId"))
    }
    sell_tids = {
        (ev.get("token_id") or ev.get("asset")) for ev in events
        if (ev.get("side") == "SELL" or ev.get("action") == "SELL") and (ev.get("token_id") or ev.get("asset"))
    }
    sell_slugs = {
        ev.get("slug") for ev in events
        if (ev.get("side") == "SELL" or ev.get("action") == "SELL") and ev.get("slug")
    }

    # Earliest timestamp per condition_id in events
    opened_at_map: Dict[str, str] = {}
    for ev in events:
        ts = ev.get("timestamp")
        cid = ev.get("condition_id")
        if cid and ts:
            if cid not in opened_at_map or ts < opened_at_map[cid]:
                opened_at_map[cid] = ts

    if raw_closed_positions:
        from polymarket.parsers import normalize_closed_position
        for idx, r in enumerate(raw_closed_positions):
            norm = normalize_closed_position(r)
            cid = norm.get("condition_id")
            tid = norm.get("token_id")
            slug = norm.get("slug")
            cost = norm.get("cost_basis", 0.0)
            pnl = norm.get("realized_pnl", 0.0)
            proceeds = cost + pnl
            raw_ts = norm.get("timestamp")
            if isinstance(raw_ts, (int, float)):
                ts = datetime.fromtimestamp(raw_ts, tz=timezone.utc).isoformat()
            else:
                ts = str(raw_ts) if raw_ts else None

            opened_at = opened_at_map.get(cid) or ts
            closed_at = ts

            pid = f"{cid}_{tid}" if cid and tid else f"closed_{idx}"
            cat = r.get("category") or category_map.get(cid) or category_map.get(slug) or "unknown"

            # Check if this position had active sell trading exit
            has_sell = (cid in sell_cids) or (tid in sell_tids) or (slug in sell_slugs)
            if has_sell:
                stype = PositionSettlementType.TRADING_EXIT
                sclass = PositionSettlementClass.CLOSED
            else:
                stype = PositionSettlementType.RESOLUTION
                if pnl > 0.01:
                    sclass = PositionSettlementClass.WON
                elif pnl < -0.01:
                    sclass = PositionSettlementClass.LOST
                else:
                    sclass = PositionSettlementClass.CLOSED

            records.append(
                PositionRecord(
                    position_id=pid,
                    condition_id=cid,
                    token_id=tid,
                    market_slug=slug,
                    outcome=norm.get("outcome"),
                    category=cat,
                    cost=round(cost, 6),
                    proceeds=round(proceeds, 6),
                    settled_net_result=round(pnl, 6),
                    settlement_class=sclass,
                    settlement_type=stype,
                    opened_at=opened_at,
                    closed_at=closed_at,
                    resolved_at=closed_at if stype == PositionSettlementType.RESOLUTION else None,
                )
            )
    elif events:
        # Fall back to reconstructing closed positions from events
        from analytics.wallet_stats import compute_wallet_stats
        legacy_stats = compute_wallet_stats("reconstruct", events)
        for idx, pnl in enumerate(legacy_stats.per_trade_pnls):
            records.append(
                PositionRecord(
                    position_id=f"event_closed_{idx}",
                    cost=round(legacy_stats.total_invested / max(1, len(legacy_stats.per_trade_pnls)), 6),
                    proceeds=round(legacy_stats.total_invested / max(1, len(legacy_stats.per_trade_pnls)) + pnl, 6),
                    settled_net_result=round(pnl, 6),
                    settlement_class=PositionSettlementClass.CLOSED,
                    settlement_type=PositionSettlementType.TRADING_EXIT if legacy_stats.sell_trades > 0 else PositionSettlementType.RESOLUTION,
                    opened_at=legacy_stats.first_activity_at,
                    closed_at=legacy_stats.last_activity_at,
                )
            )

    # Open positions
    if raw_open_positions:
        from polymarket.parsers import _to_float
        for idx, op in enumerate(raw_open_positions):
            cid = op.get("conditionId") or op.get("condition_id")
            tid = op.get("asset") or op.get("token_id")
            shares = _to_float(op.get("size") or op.get("shares") or 0.0)
            if shares > 1e-9:
                cost = _to_float(op.get("initialValue") or op.get("costBasis") or 0.0)
                records.append(
                    PositionRecord(
                        position_id=f"open_{cid or tid or idx}",
                        condition_id=cid,
                        token_id=tid,
                        category=op.get("category") or category_map.get(cid) or "unknown",
                        cost=round(cost, 6),
                        settlement_class=PositionSettlementClass.UNRESOLVED,
                        settlement_type=PositionSettlementType.NONE,
                        remaining_shares=shares,
                    )
                )

    return records

