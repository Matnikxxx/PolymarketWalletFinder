"""
Phase 10 tests: Scanner pipeline alignment with Phase 6 WalletStatistics and Phase 7 FrequencyProfile.

Verifies:
- cmd_scan_wallets constructs WalletStatistics and FrequencyProfile
- build_position_records() produces valid PositionRecord objects
- filter_wallet() is invoked with canonical WalletStatistics
- rank_wallets() receives canonical WalletStatistics and FrequencyProfile
- FAIL wallets are excluded from ranking
- INSUFFICIENT_DATA wallets are excluded from ranking
- PASS wallets are ranked with canonical score_wallet
- wallets.json schema and persistence are preserved
- Backward compatibility: rank_wallets still supports legacy WalletStats / FrequencyStats
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch
import pytest

from analytics.models import (
    PositionRecord,
    PositionSettlementClass,
    PositionSettlementType,
    WalletStatistics,
)
from analytics.frequency import FrequencyProfile, compute_frequency_profile
from analytics.performance import build_position_records, compute_wallet_statistics
from filters.wallet_filter import filter_wallet
from wallet.ranking import rank_wallets, score_wallet
from analytics.wallet_stats import WalletStats
from analytics.frequency import FrequencyStats


# ---------------------------------------------------------------------------
# Test build_position_records
# ---------------------------------------------------------------------------

def test_build_position_records_closed_and_open():
    """Verify build_position_records converts closed API items and open positions."""
    closed_api_data = [
        {
            "conditionId": "0xcond1",
            "asset": "token1",
            "title": "Will X happen in 2026?",
            "outcome": "Yes",
            "realizedPnl": 150.0,
            "totalBought": 500.0,
            "avgPrice": 0.5,
            "timestamp": 1700000000,
            "category": "Technology",
        },
        {
            "conditionId": "0xcond2",
            "asset": "token2",
            "title": "Will Y happen in US Election?",
            "outcome": "No",
            "realizedPnl": -200.0,
            "totalBought": 400.0,
            "avgPrice": 0.4,
            "timestamp": 1700100000,
            "category": "Politics",
        }
    ]
    events = [
        {"action": "BUY", "asset": "token1", "timestamp": 1699900000},
        {"action": "SELL", "asset": "token1", "timestamp": 1700000000},
        {"action": "BUY", "asset": "token2", "timestamp": 1700050000},
    ]
    open_positions = [
        {
            "conditionId": "0xcond3",
            "asset": "token3",
            "title": "Will Z happen?",
            "outcome": "Yes",
            "size": 100.0,
            "currentValue": 300.0,
            "initialValue": 250.0,
            "category": "Science",
        }
    ]

    records = build_position_records(closed_api_data, events, open_positions)
    assert len(records) == 3

    # Check closed winning trade with SELL fill -> TRADING_EXIT
    rec1 = records[0]
    assert rec1.condition_id == "0xcond1"
    assert rec1.settled_net_result == 150.0
    assert rec1.settlement_class == PositionSettlementClass.CLOSED
    assert rec1.settlement_type == PositionSettlementType.TRADING_EXIT
    assert rec1.category == "Technology"

    # Check closed losing trade with no SELL fill -> RESOLUTION
    rec2 = records[1]
    assert rec2.condition_id == "0xcond2"
    assert rec2.settled_net_result == -200.0
    assert rec2.settlement_class == PositionSettlementClass.LOST
    assert rec2.settlement_type == PositionSettlementType.RESOLUTION
    assert rec2.category == "Politics"

    # Check open position -> UNRESOLVED
    rec3 = records[2]
    assert rec3.condition_id == "0xcond3"
    assert rec3.settlement_class == PositionSettlementClass.UNRESOLVED
    assert rec3.settlement_type == PositionSettlementType.NONE
    assert rec3.category == "Science"


# ---------------------------------------------------------------------------
# Test canonical objects through filter and ranking
# ---------------------------------------------------------------------------

def test_canonical_pipeline_filter_and_rank():
    """Verify WalletStatistics and FrequencyProfile pass through filter_wallet and rank_wallets."""
    passing_stat = WalletStatistics(
        wallet="0xpass",
        positions_counted=50,
        roi_fee_exclusive_pct=25.0,
        active_days=45,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.03,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=20.0,
        is_resolution_only=False,
        category_mix={"Technology": 50},
    )

    failing_stat = WalletStatistics(
        wallet="0xfail",
        positions_counted=10,  # Fails MIN_POSITIONS (40)
        roi_fee_exclusive_pct=30.0,
        active_days=50,
        consistency_mean_roi=0.30,
        consistency_stdev_roi=0.02,
        drawdown_cost_pct=5.0,
        top1_profit_share_pct=15.0,
        is_resolution_only=False,
    )

    insuf_stat = WalletStatistics(
        wallet="0xinsuf",
        positions_counted=50,
        roi_fee_exclusive_pct=None,  # Insufficient data
        active_days=45,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.03,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=20.0,
        is_resolution_only=False,
    )

    res_pass = filter_wallet(passing_stat)
    assert res_pass.passed is True
    assert res_pass.status == "PASS"

    res_fail = filter_wallet(failing_stat)
    assert res_fail.passed is False
    assert res_fail.status == "FAIL"

    res_insuf = filter_wallet(insuf_stat)
    assert res_insuf.status == "INSUFFICIENT_DATA"

    # Only pass_stat should be passed to rank_wallets
    freq_pass = compute_frequency_profile(
        "0xpass",
        [
            {
                "timestamp": f"2026-01-0{i+1}T12:00:00+00:00",
                "side": "BUY" if i % 2 == 0 else "SELL",
                "usdc_size": 100.0,
                "size": 100.0,
                "tx_hash": f"0xhash{i}",
                "condition_id": f"0xcond{i}",
            }
            for i in range(10)
        ],
    )

    ranked = rank_wallets([passing_stat], {"0xpass": freq_pass})
    assert len(ranked) == 1
    assert ranked[0].wallet == "0xpass"
    assert ranked[0].final_score > 0
    assert ranked[0].recommendation in ["RECOMMENDED", "WATCH", "MONITOR", "AVOID"]


# ---------------------------------------------------------------------------
# Test cmd_scan_wallets calls canonical functions (AST / inspection & mock)
# ---------------------------------------------------------------------------

def test_cmd_scan_wallets_uses_canonical_pipeline():
    """Verify main.cmd_scan_wallets invokes compute_wallet_statistics and compute_frequency_profile."""
    import main
    src = inspect.getsource(main.cmd_scan_wallets)

    assert "compute_wallet_statistics(" in src
    assert "compute_frequency_profile(" in src
    assert "build_position_records(" in src
    assert "filter_wallet(" in src
    assert "rank_wallets(" in src


@patch("main.scan_top_wallets")
@patch("main.fetch_wallet_events_paginated")
@patch("main.PolymarketApiClient")
@patch("main.save_wallet_config")
def test_cmd_scan_wallets_end_to_end_mock(
    mock_save, mock_client_cls, mock_events, mock_scan
):
    """End-to-end mock execution of cmd_scan_wallets verifying canonical routing."""
    import main

    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_scan.return_value = ["0x1111111111111111111111111111111111111111"]

    mock_events.return_value = [
        {
            "side": "BUY" if i % 2 == 0 else "SELL",
            "condition_id": f"0xcond{i}",
            "asset": f"tok{i}",
            "timestamp": f"2026-01-{(i % 25) + 1:02d}T{(i % 24):02d}:00:00+00:00",
            "tx_hash": f"0xtx{i}",
            "usdc_size": 100.0,
            "size": 100.0,
        }
        for i in range(50)
    ]
    # Provide 45 closed positions with timestamps spanning > 30 days.
    # cmd_scan_wallets now calls get_wallet_closed_positions_paginated (Phase 12).
    mock_client.get_wallet_closed_positions_paginated.return_value = [
        {
            "conditionId": f"0xcond{i}",
            "asset": f"tok{i}",
            "title": f"Market {i}",
            "outcome": "Yes",
            "realizedPnl": 15.0,
            "totalBought": 100.0,
            "avgPrice": 0.5,
            "timestamp": 1690000000 + i * 86400,  # spans 45 days
            "category": "Crypto",
        }
        for i in range(45)
    ]
    mock_client.get_wallet_positions.return_value = []
    mock_client.get_wallet_value.return_value = 5000.0

    with patch("main.load_wallet_config", return_value=[]):
        main.cmd_scan_wallets(None)

    # Verify save_wallet_config was called
    assert mock_save.called
    saved_wallets = mock_save.call_args[0][0]
    assert len(saved_wallets) == 1
    w_entry = saved_wallets[0]

    # Verify schema fields are preserved
    assert w_entry["wallet"] == "0x1111111111111111111111111111111111111111"
    assert "last_score" in w_entry
    assert "recommendation" in w_entry
    assert "hard_filter_status" in w_entry
    assert w_entry["hard_filter_status"] == "PASS"
    assert "frequency_classification" in w_entry
    assert w_entry["num_positions"] == 45
    assert w_entry.get("tracked") is False


def test_backward_compatibility_rank_wallets_legacy():
    """Verify rank_wallets still works when given legacy WalletStats."""
    legacy_s = WalletStats(wallet="0xlegacy")
    legacy_s.total_positions = 50
    legacy_s.closed_positions = 50
    legacy_s.wins = 35
    legacy_s.losses = 15
    legacy_s.roi_pct = 25.0
    legacy_s.win_rate_pct = 70.0
    legacy_s.max_drawdown_pct = 10.0
    legacy_s.consistency_score = 8.5
    legacy_s.concentration_pct = 20.0
    legacy_s.active_days = 40
    legacy_s.total_invested = 5000.0
    legacy_s.per_trade_pnls = [5.0] * 50

    legacy_freq = FrequencyStats(wallet="0xlegacy", total_trades=50, active_days=40)

    ranked = rank_wallets([legacy_s], {"0xlegacy": legacy_freq})
    assert len(ranked) == 1
    assert ranked[0].wallet == "0xlegacy"
    assert ranked[0].final_score > 0


@patch("main.scan_top_wallets")
@patch("main.fetch_wallet_events_paginated")
@patch("main.PolymarketApiClient")
@patch("main.save_wallet_config")
def test_cmd_scan_wallets_multi_partitioning(
    mock_save, mock_client_cls, mock_events, mock_scan
):
    """
    Test cmd_scan_wallets with 3 candidate wallets:
    - w_pass: valid positions and activity, meets all thresholds -> PASS -> ranked
    - w_fail: few positions (<40) -> FAIL -> excluded from ranking, HARD_FILTER_REJECT
    - w_insuf: no closed positions -> INSUFFICIENT_DATA -> excluded from ranking, HARD_FILTER_INSUFFICIENT_DATA
    """
    import main

    w_pass = "0xpass000000000000000000000000000000000001"
    w_fail = "0xfail000000000000000000000000000000000002"
    w_insuf = "0xinsuf00000000000000000000000000000000003"

    mock_scan.return_value = [w_pass, w_fail, w_insuf]

    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client

    def side_effect_events(client, wallet, max_pages=5):
        if wallet == w_pass:
            return [
                {
                    "side": "BUY" if i % 2 == 0 else "SELL",
                    "condition_id": f"0xcond{i}",
                    "asset": f"tok{i}",
                    "timestamp": f"2026-01-{(i % 25) + 1:02d}T{(i % 24):02d}:00:00+00:00",
                    "tx_hash": f"0xtx{i}",
                    "usdc_size": 100.0,
                    "size": 100.0,
                }
                for i in range(50)
            ]
        elif wallet == w_fail:
            return [
                {
                    "side": "BUY",
                    "condition_id": "0xfailcond",
                    "asset": "failtok",
                    "timestamp": "2026-01-01T12:00:00+00:00",
                    "tx_hash": "0xfailev",
                    "usdc_size": 50.0,
                    "size": 50.0,
                }
            ]
        else:
            return []

    mock_events.side_effect = side_effect_events

    # cmd_scan_wallets now calls get_wallet_closed_positions_paginated (Phase 12).
    def side_effect_closed_paginated(wallet, max_pages=5):
        if wallet == w_pass:
            return [
                {
                    "conditionId": f"0xcond{i}",
                    "asset": f"tok{i}",
                    "title": f"Market {i}",
                    "outcome": "Yes",
                    "realizedPnl": 15.0,
                    "totalBought": 100.0,
                    "avgPrice": 0.5,
                    "timestamp": 1690000000 + i * 86400,
                    "category": "Crypto",
                }
                for i in range(45)
            ]
        elif wallet == w_fail:
            return [
                {
                    "conditionId": "0xfailcond",
                    "asset": "failtok",
                    "title": "Fail Market",
                    "outcome": "Yes",
                    "realizedPnl": 10.0,
                    "totalBought": 50.0,
                    "avgPrice": 0.5,
                    "timestamp": 1700000000,
                    "category": "Crypto",
                }
            ]
        else:
            return []

    mock_client.get_wallet_closed_positions_paginated.side_effect = side_effect_closed_paginated
    mock_client.get_wallet_positions.return_value = []
    mock_client.get_wallet_value.return_value = 1000.0

    orig_compute_stats = main.compute_wallet_statistics

    def mocked_compute_stats(wallet, positions):
        if wallet == w_insuf:
            return WalletStatistics(
                wallet=w_insuf,
                positions_counted=45,
                roi_fee_exclusive_pct=25.0,
                active_days=40,
                consistency_mean_roi=0.25,
                consistency_stdev_roi=0.03,
                drawdown_cost_pct=10.0,
                top1_profit_share_pct=20.0,
                is_resolution_only=None,  # triggers INSUFFICIENT_DATA
                category_mix={"Crypto": 45},
            )
        return orig_compute_stats(wallet, positions)

    with patch("main.compute_wallet_statistics", side_effect=mocked_compute_stats):
        with patch("main.load_wallet_config", return_value=[]):
            main.cmd_scan_wallets(None)

    assert mock_save.called
    saved_wallets = mock_save.call_args[0][0]
    assert len(saved_wallets) == 3

    saved_map = {w["wallet"]: w for w in saved_wallets}

    # Pass wallet checks
    pass_entry = saved_map[w_pass]
    assert pass_entry["hard_filter_status"] == "PASS"
    assert pass_entry["last_score"] is not None
    assert pass_entry["recommendation"] in ["RECOMMENDED", "WATCH", "MONITOR", "AVOID"]
    assert pass_entry["num_positions"] == 45

    # Fail wallet checks
    fail_entry = saved_map[w_fail]
    assert fail_entry["hard_filter_status"] == "FAIL"
    assert fail_entry["last_score"] is None
    assert fail_entry["recommendation"] == "HARD_FILTER_REJECT"
    assert len(fail_entry["hard_filter_reasons"]) > 0

    # Insufficient wallet checks
    insuf_entry = saved_map[w_insuf]
    assert insuf_entry["hard_filter_status"] == "INSUFFICIENT_DATA"
    assert insuf_entry["last_score"] is None
    assert insuf_entry["recommendation"] == "HARD_FILTER_INSUFFICIENT_DATA"
    assert len(insuf_entry["hard_filter_insufficient_criteria"]) > 0

