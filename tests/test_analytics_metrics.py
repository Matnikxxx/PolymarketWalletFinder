"""
Tests for Phase 6 wallet analytics:
- Exact formulas for ROI, win rate, drawdown, concentration, sizing, consistency
- Strict population accounting identities and non-overlapping buckets
- 6-decimal boundary rounding and breakeven tolerance
- Net-result based win/loss vs market outcome
- Partial payout handling (in position count and ROI, excluded from win-rate denominator)
- Clean-data vs repeat-exposure sensitivity view
- Combos and incomplete/unresolved positions excluded from ordinary statistics
"""
import pytest
from analytics.models import (
    PositionRecord,
    PositionSettlementClass,
    PositionSettlementType,
    PositionExclusionReason,
    WalletStatistics,
    PopulationAccounting,
)
from analytics.eligibility import evaluate_position_eligibility, partition_positions
from analytics.performance import compute_wallet_statistics, calculate_drawdown


def _make_pos(
    pid: str,
    cost: float = 10.0,
    proceeds: float = 15.0,
    settled_net: float = 5.0,
    sclass: PositionSettlementClass = PositionSettlementClass.CLOSED,
    stype: PositionSettlementType = PositionSettlementType.TRADING_EXIT,
    cid: str = "cond_1",
    ts: str = "2026-01-01T12:00:00+00:00",
    **kwargs,
) -> PositionRecord:
    return PositionRecord(
        position_id=pid,
        condition_id=cid,
        token_id=f"token_{pid}",
        market_slug=f"slug_{pid}",
        cost=cost,
        proceeds=proceeds,
        settled_net_result=settled_net,
        settlement_class=sclass,
        settlement_type=stype,
        opened_at=ts,
        closed_at=ts,
        resolved_at=ts,
        **kwargs,
    )


# 1. Decision 1 & 4: Position Count and Partial Payout Handling ----------------
def test_min_positions_and_partial_payout_counting():
    """
    Partial-payout positions count toward position count (|E|) and ROI,
    but are excluded from the binary (W / (W + L)) win-rate denominator.
    """
    positions = [
        _make_pos("p1", cost=20.0, proceeds=30.0, settled_net=10.0),    # Win
        _make_pos("p2", cost=30.0, proceeds=15.0, settled_net=-15.0),   # Loss
        _make_pos("p3", cost=20.0, proceeds=35.0, settled_net=15.0),    # Win
        _make_pos("p4", cost=10.0, proceeds=15.0, settled_net=5.0,     # Partial payout
                  sclass=PositionSettlementClass.PARTIAL,
                  stype=PositionSettlementType.RESOLUTION),
    ]

    stats = compute_wallet_statistics("0xwallet1", positions)

    assert stats.positions_counted == 4
    assert stats.wins == 2
    assert stats.losses == 1
    assert stats.breakeven == 0
    assert stats.partials == 1

    # Win rate denominator is wins + losses (2 + 1 = 3), partial excluded!
    assert stats.win_rate_pct == round(100.0 * 2 / 3, 4)

    # ROI includes all 4 positions:
    # Total cost = 20 + 30 + 20 + 10 = 80.
    # Total net = 10 - 15 + 15 + 5 = 15.
    assert stats.total_settled_cost == 80.0
    assert stats.total_settled_net_result == 15.0
    assert stats.roi_fee_exclusive_pct == round(100.0 * 15 / 80, 4)


# 2. Decision 2: Net-Result Classification vs Market Outcome ------------------
def test_position_win_loss_based_on_economic_net_result():
    """
    Economic win/loss is based on net economic result r, not the nominal outcome.
    """
    positions = [
        # RESOLVED_WON market, but trader sold early at a heavy loss -> net loss
        _make_pos("p_won_lost", cost=100.0, proceeds=29.0, settled_net=-71.0,
                  sclass=PositionSettlementClass.WON,
                  stype=PositionSettlementType.RESOLUTION),
        # RESOLVED_LOST market, but trader sold at peak profit earlier -> net win
        _make_pos("p_lost_won", cost=10.0, proceeds=41.0, settled_net=31.0,
                  sclass=PositionSettlementClass.LOST,
                  stype=PositionSettlementType.RESOLUTION),
    ]

    stats = compute_wallet_statistics("0xwallet2", positions)
    assert stats.positions_counted == 2
    assert stats.wins == 1     # p_lost_won
    assert stats.losses == 1   # p_won_lost
    assert stats.win_rate_pct == 50.0


# 3. Decision 3: Breakeven Tolerance & Boundary Rounding ----------------------
def test_breakeven_tolerance_and_boundary_rounding():
    """
    Tolerance is +/- $0.01 USDC. Results are rounded to 6 decimals before
    inclusive boundary evaluation.
    """
    positions = [
        # Float representation noise: 0.07 - 0.06 evaluates to 0.010000000000000009
        _make_pos("p_noise", cost=10.0, proceeds=10.01, settled_net=(0.07 - 0.06)),
        # Exact positive boundary +0.01
        _make_pos("p_bound_pos", cost=10.0, proceeds=10.01, settled_net=0.010000),
        # Exact negative boundary -0.01
        _make_pos("p_bound_neg", cost=10.0, proceeds=9.99, settled_net=-0.010000),
        # Beyond boundary: +0.010001 -> Win
        _make_pos("p_win", cost=10.0, proceeds=10.010001, settled_net=0.010001),
        # Beyond boundary: -0.010001 -> Loss
        _make_pos("p_loss", cost=10.0, proceeds=9.989999, settled_net=-0.010001),
    ]

    stats = compute_wallet_statistics("0xwallet3", positions, tolerance=0.01)
    assert stats.breakeven == 3  # p_noise, p_bound_pos, p_bound_neg
    assert stats.wins == 1       # p_win
    assert stats.losses == 1     # p_loss
    assert stats.win_rate_pct == 50.0  # 1 win / (1 win + 1 loss)


# 4. Decision 5: Fee-Exclusive Settled ROI ------------------------------------
def test_roi_fee_exclusive_labeled_and_accurate():
    positions = [
        _make_pos("p1", cost=50.0, proceeds=60.0, settled_net=10.0),
        _make_pos("p2", cost=50.0, proceeds=40.0, settled_net=-10.0),
    ]
    stats = compute_wallet_statistics("0xwallet4", positions)
    assert stats.roi_fee_exclusive_pct == 0.0

    # Zero cost produces None (not a crash or 0.0)
    stats_empty = compute_wallet_statistics("0xempty", [])
    assert stats_empty.roi_fee_exclusive_pct is None
    assert stats_empty.win_rate_pct is None


# 5. Decision 6: Clean-Data vs Sensitivity View (Repeat Exposed) -------------
def test_clean_data_vs_sensitivity_repeat_exposed():
    """
    Clean data excludes repeat-exposed positions.
    Sensitivity view includes them, reporting affected counts and diagnostic ROI.
    """
    positions = [
        _make_pos("p1", cost=50.0, proceeds=55.0, settled_net=5.0),
        _make_pos("p2", cost=50.0, proceeds=55.0, settled_net=5.0),
        _make_pos("p3", cost=50.0, proceeds=55.0, settled_net=5.0),
        # Repeat-exposed loser: split across page boundary
        _make_pos("p4_repeat", cost=50.0, proceeds=0.0, settled_net=-50.0,
                  repeat_exposed_fill_count=2),
    ]

    stats = compute_wallet_statistics("0xwallet5", positions)

    # Clean data view
    assert stats.positions_counted == 3
    assert stats.wins == 3
    assert stats.losses == 0
    assert stats.win_rate_pct == 100.0
    assert stats.total_settled_cost == 150.0
    assert stats.total_settled_net_result == 15.0
    assert stats.roi_fee_exclusive_pct == 10.0
    assert stats.coverage_ratio == 0.75  # 3 of 4 ordinary positions

    # Accounting record
    assert stats.accounting.repeat_exposed_count == 1

    # Sensitivity view
    assert stats.sensitivity_positions_counted == 4
    assert stats.sensitivity_wins == 3
    assert stats.sensitivity_losses == 1
    assert stats.sensitivity_win_rate_pct == 75.0
    assert stats.sensitivity_settled_cost == 200.0
    assert stats.sensitivity_settled_net == -35.0
    assert stats.sensitivity_roi_fee_exclusive_pct == round(100.0 * (-35.0) / 200.0, 4)


# 6. Decision 7: Partially Retrieved History Flagged --------------------------
def test_partially_retrieved_history_flagged():
    positions = [_make_pos("p1", cost=10.0, proceeds=15.0, settled_net=5.0)]
    stats = compute_wallet_statistics("0xwallet6", positions, is_complete_history=False)
    assert stats.is_complete_history is False
    assert stats.retrieval_status == "PARTIAL"


# 7. Decision 8: Combos Excluded from Ordinary Statistics ---------------------
def test_combos_excluded_from_ordinary_statistics():
    positions = [
        _make_pos("p1", cost=10.0, proceeds=15.0, settled_net=5.0),
        _make_pos("p_combo", cost=50.0, proceeds=60.0, settled_net=10.0, is_combo=True),
    ]
    stats = compute_wallet_statistics("0xwallet7", positions)
    assert stats.positions_counted == 1
    assert stats.accounting.combo_positions == 1
    assert stats.accounting.ordinary_positions == 1
    assert stats.accounting.total_positions == 2


# 8. Decision 9: Conservative Resolution (Never Assume Zero or Wins/Losses) ---
def test_unknown_unresolved_never_treated_as_zero_or_assumed():
    positions = [
        _make_pos("p1", cost=10.0, proceeds=15.0, settled_net=5.0),
        # Unresolved position
        _make_pos("p_open", cost=20.0, proceeds=0.0, settled_net=None,
                  sclass=PositionSettlementClass.UNRESOLVED, remaining_shares=20.0),
        # Incomplete / unknown resolution position
        _make_pos("p_unk", cost=15.0, proceeds=0.0, settled_net=None,
                  sclass=PositionSettlementClass.UNKNOWN),
    ]
    stats = compute_wallet_statistics("0xwallet8", positions)
    assert stats.positions_counted == 1
    assert stats.accounting.unresolved_open_count == 1
    assert stats.accounting.incomplete_count == 1
    assert stats.total_settled_cost == 10.0
    assert stats.total_settled_net_result == 5.0


# 9. Check A: Drawdown with Reinvestment & Large Peak Giveback ---------------
def test_drawdown_large_gains_followed_by_losses():
    """
    Validates dollar drawdown and cost-based / peak-based percentages.
    """
    positions = [
        _make_pos("p1", cost=10.0, proceeds=1010.0, settled_net=1000.0,
                  ts="2026-01-01T00:00:00+00:00"),
        _make_pos("p2", cost=200.0, proceeds=0.0, settled_net=-200.0,
                  ts="2026-01-02T00:00:00+00:00"),
        _make_pos("p3", cost=200.0, proceeds=0.0, settled_net=-200.0,
                  ts="2026-01-03T00:00:00+00:00"),
        _make_pos("p4", cost=200.0, proceeds=0.0, settled_net=-200.0,
                  ts="2026-01-04T00:00:00+00:00"),
        _make_pos("p5", cost=200.0, proceeds=0.0, settled_net=-200.0,
                  ts="2026-01-05T00:00:00+00:00"),
        _make_pos("p6", cost=200.0, proceeds=0.0, settled_net=-200.0,
                  ts="2026-01-06T00:00:00+00:00"),
    ]
    stats = compute_wallet_statistics("0xwallet9", positions)

    # Dollar drawdown from peak 1000 to trough 0 is exactly 1000.0
    assert stats.max_drawdown_usd == 1000.0

    # Total settled cost: 10 + 5 * 200 = 1010.0
    assert stats.total_settled_cost == 1010.0
    assert stats.drawdown_cost_pct == round(100.0 * 1000.0 / 1010.0, 4)

    # Peak equity: 1000.0 -> peak-based drawdown is 100.0%
    assert stats.drawdown_peak_pct == 100.0


# 10. Check B & C: Population Accounting Identity Invariants ------------------
def test_accounting_identity_invariants():
    """
    Tests that a complex heterogeneous universe of positions partitions
    cleanly into mutually exclusive buckets with zero overlap.
    """
    positions = [
        # Eligible
        _make_pos("e_win", cost=10.0, proceeds=15.0, settled_net=5.0),
        _make_pos("e_loss", cost=10.0, proceeds=5.0, settled_net=-5.0),
        _make_pos("e_be", cost=10.0, proceeds=10.0, settled_net=0.005),
        _make_pos("e_partial", cost=10.0, proceeds=12.0, settled_net=2.0,
                  sclass=PositionSettlementClass.PARTIAL),

        # Combos
        _make_pos("c1", is_combo=True),

        # Unreliable Data
        _make_pos("u_fills", has_excluded_fills=True),
        _make_pos("u_cash", has_unknown_cash=True),
        _make_pos("u_size", has_unknown_size=True),
        _make_pos("u_cid", cid=None, stype=PositionSettlementType.RESOLUTION),
        _make_pos("u_zero_cost", cost=0.0),

        # Repeat Exposed
        _make_pos("r1", repeat_exposed_fill_count=1),

        # Unresolved Open
        _make_pos("o1", sclass=PositionSettlementClass.UNRESOLVED, remaining_shares=10.0),

        # Incomplete
        _make_pos("i1", sclass=PositionSettlementClass.UNKNOWN),
        _make_pos("i2", ambiguous_redemption=True),
    ]

    evaluations, accounting = partition_positions(positions)

    assert accounting.is_identity_valid is True
    assert accounting.total_positions == 14
    assert accounting.combo_positions == 1
    assert accounting.ordinary_positions == 13
    assert accounting.eligible_count == 4
    assert accounting.wins_count == 1
    assert accounting.losses_count == 1
    assert accounting.breakeven_count == 1
    assert accounting.partial_count == 1
    assert accounting.unreliable_data_count == 5
    assert accounting.repeat_exposed_count == 1
    assert accounting.unresolved_open_count == 1
    assert accounting.incomplete_count == 2


# 11. Sizing, Concentration, Consistency & Active Days -----------------------
def test_sizing_concentration_and_consistency():
    positions = [
        _make_pos("p1", cost=10.0, proceeds=20.0, settled_net=10.0, ts="2026-01-01T00:00:00+00:00"),
        _make_pos("p2", cost=20.0, proceeds=40.0, settled_net=20.0, ts="2026-01-02T00:00:00+00:00"),
        _make_pos("p3", cost=30.0, proceeds=30.0, settled_net=0.0,  ts="2026-01-03T00:00:00+00:00"),
        _make_pos("p4", cost=40.0, proceeds=80.0, settled_net=40.0, ts="2026-01-04T00:00:00+00:00"),
    ]
    stats = compute_wallet_statistics("0xwallet_metrics", positions)

    # Position sizing: costs = [10, 20, 30, 40]
    assert stats.mean_position_cost == 25.0
    assert stats.median_position_cost == 25.0
    assert stats.max_position_cost == 40.0

    # Gross profit: 10 + 20 + 40 = 70. Top 1 = 40 (57.1429%). Top 3 = 70 (100.0%).
    assert stats.top1_profit_share_pct == round(100.0 * 40 / 70, 4)
    assert stats.top3_profit_share_pct == 100.0

    # Consistency: 4 positions -> mean, median, stdev of rho = r / c
    # rhos: 10/10=1.0, 20/20=1.0, 0/30=0.0, 40/40=1.0
    assert stats.consistency_mean_roi == 0.75
    assert stats.consistency_median_roi == 1.0
    assert stats.consistency_stdev_roi is not None

    # Active days: 4 distinct UTC dates
    assert stats.active_days == 4
