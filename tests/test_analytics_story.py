"""
Golden fixture tests for Phase 6 wallet analytics derived from Demo 1 (wallet_story).
Verifies the exact scenario outcomes for unresolved, won, lost, and partial resolutions.
"""
import pytest
from analytics.models import (
    PositionRecord,
    PositionSettlementClass,
    PositionSettlementType,
)
from analytics.performance import compute_wallet_statistics


def _get_base_story_positions():
    """
    Returns Positions A, B, D and incomplete Position E from Demo 1.
    """
    pos_a = PositionRecord(
        position_id="pos_A",
        condition_id="c_A",
        cost=12.0,
        proceeds=30.15,
        settled_net_result=18.15,
        settlement_class=PositionSettlementClass.CLOSED,
        settlement_type=PositionSettlementType.TRADING_EXIT,
        opened_at="2026-01-01T00:00:00+00:00",
        closed_at="2026-01-01T12:00:00+00:00",
    )
    pos_b = PositionRecord(
        position_id="pos_B",
        condition_id="c_B",
        cost=20.0,
        proceeds=29.35,
        settled_net_result=9.35,
        settlement_class=PositionSettlementClass.CLOSED,
        settlement_type=PositionSettlementType.TRADING_EXIT,
        opened_at="2026-01-02T00:00:00+00:00",
        closed_at="2026-01-02T12:00:00+00:00",
    )
    pos_d = PositionRecord(
        position_id="pos_D",
        condition_id="c_D",
        cost=50.5,
        proceeds=80.5,
        settled_net_result=30.0,
        settlement_class=PositionSettlementClass.CLOSED,
        settlement_type=PositionSettlementType.TRADING_EXIT,
        opened_at="2026-01-03T00:00:00+00:00",
        closed_at="2026-01-03T12:00:00+00:00",
    )
    pos_e = PositionRecord(
        position_id="pos_E",
        condition_id="c_E",
        cost=15.0,
        proceeds=0.0,
        settled_net_result=None,
        settlement_class=PositionSettlementClass.UNKNOWN,
        has_excluded_fills=True,
    )
    return [pos_a, pos_b, pos_d, pos_e]


def test_story_scenario_c_unresolved():
    positions = _get_base_story_positions()
    pos_c = PositionRecord(
        position_id="pos_C",
        condition_id="c_C",
        cost=3.0,
        proceeds=0.0,
        settled_net_result=None,
        settlement_class=PositionSettlementClass.UNRESOLVED,
        remaining_shares=3.0,
    )
    positions.append(pos_c)

    stats = compute_wallet_statistics("0xstory", positions)

    assert stats.positions_counted == 3
    assert stats.wins == 3
    assert stats.losses == 0
    assert stats.partials == 0
    assert stats.win_rate_pct == 100.0
    assert stats.roi_fee_exclusive_pct == pytest.approx(69.70, abs=0.01)


def test_story_scenario_c_resolves_yes():
    positions = _get_base_story_positions()
    pos_c = PositionRecord(
        position_id="pos_C",
        condition_id="c_C",
        cost=3.0,
        proceeds=10.0,
        settled_net_result=7.0,
        settlement_class=PositionSettlementClass.WON,
        settlement_type=PositionSettlementType.RESOLUTION,
        resolved_at="2026-01-04T12:00:00+00:00",
    )
    positions.append(pos_c)

    stats = compute_wallet_statistics("0xstory", positions)

    assert stats.positions_counted == 4
    assert stats.wins == 4
    assert stats.losses == 0
    assert stats.partials == 0
    assert stats.win_rate_pct == 100.0
    assert stats.roi_fee_exclusive_pct == pytest.approx(75.44, abs=0.01)


def test_story_scenario_c_resolves_no():
    positions = _get_base_story_positions()
    pos_c = PositionRecord(
        position_id="pos_C",
        condition_id="c_C",
        cost=3.0,
        proceeds=0.0,
        settled_net_result=-3.0,
        settlement_class=PositionSettlementClass.LOST,
        settlement_type=PositionSettlementType.RESOLUTION,
        resolved_at="2026-01-04T12:00:00+00:00",
    )
    positions.append(pos_c)

    stats = compute_wallet_statistics("0xstory", positions)

    assert stats.positions_counted == 4
    assert stats.wins == 3
    assert stats.losses == 1
    assert stats.partials == 0
    assert stats.win_rate_pct == 75.0
    assert stats.roi_fee_exclusive_pct == pytest.approx(63.74, abs=0.01)

    # Full formulas on the "resolves NO" case:
    assert stats.total_settled_net_result == 54.50
    assert stats.total_settled_cost == 85.50
    assert stats.max_drawdown_usd == 3.00
    assert stats.drawdown_cost_pct == pytest.approx(3.509, abs=0.001)
    assert stats.top1_profit_share_pct == pytest.approx(52.17, abs=0.01)
    assert stats.top3_profit_share_pct == 100.0

    # Position size stats:
    assert stats.mean_position_cost == 21.375
    assert stats.median_position_cost == 16.0
    assert stats.max_position_cost == 50.5

    # Sample standard deviation of per-position returns:
    assert stats.consistency_stdev_roi == pytest.approx(1.039, abs=0.001)


def test_story_scenario_c_resolves_partial_50_50():
    positions = _get_base_story_positions()
    pos_c = PositionRecord(
        position_id="pos_C",
        condition_id="c_C",
        cost=3.0,
        proceeds=5.0,
        settled_net_result=2.0,
        settlement_class=PositionSettlementClass.PARTIAL,
        settlement_type=PositionSettlementType.RESOLUTION,
        resolved_at="2026-01-04T12:00:00+00:00",
    )
    positions.append(pos_c)

    stats = compute_wallet_statistics("0xstory", positions)

    assert stats.positions_counted == 4
    assert stats.wins == 3
    assert stats.losses == 0
    assert stats.partials == 1
    assert stats.win_rate_pct == 100.0  # Denominator is 3 positions (W/(W+L))
    assert stats.roi_fee_exclusive_pct == pytest.approx(69.59, abs=0.01)
