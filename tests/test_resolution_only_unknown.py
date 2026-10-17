"""
Regression tests for P2: unverified exit history must not read as
"resolution-only".

Defect: ``is_resolution_only`` was derived from the presence of a
TRADING_EXIT position, and TRADING_EXIT is inferred from SELL fills in the
/activity feed. When the SELL fills fell outside the fetched (newest-first,
capped) window, no exit was found and the wallet was declared
"resolution-only (no active exits)" -- a rejection based on absent evidence.

Fix: when the exit history is incomplete, "no exit observed" becomes
``is_resolution_only=None`` (unknown), which the filter reports as
INSUFFICIENT_DATA for that criterion rather than rejecting the wallet. An
exit that *is* observed still proves False.
"""
from __future__ import annotations

from unittest.mock import patch

from analytics.models import (
    PositionRecord,
    PositionSettlementClass,
    PositionSettlementType,
    WalletStatistics,
)
from analytics.performance import compute_wallet_statistics
from config.settings import settings
from filters.wallet_filter import filter_wallet
from polymarket.api_client import PolymarketApiClient


WALLET = "0x2222222222222222222222222222222222222222"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _position(
    pid: str,
    *,
    settlement_type: PositionSettlementType = PositionSettlementType.RESOLUTION,
    settlement_class: PositionSettlementClass = PositionSettlementClass.WON,
    net: float = 5.0,
    cost: float = 10.0,
    category: str = "sports",
) -> PositionRecord:
    ts = "2026-01-15T12:00:00+00:00"
    return PositionRecord(
        position_id=pid,
        condition_id=f"cond_{pid}",
        token_id=f"token_{pid}",
        market_slug=f"slug_{pid}",
        cost=cost,
        proceeds=cost + net,
        settled_net_result=net,
        settlement_class=settlement_class,
        settlement_type=settlement_type,
        opened_at=ts,
        closed_at=ts,
        resolved_at=ts,
        category=category,
    )


def _resolution_positions(n: int = 5) -> list[PositionRecord]:
    return [_position(f"r{i}") for i in range(n)]


def _raw_closed(i: int) -> dict:
    return {
        "proxyWallet": WALLET,
        "conditionId": f"cid{i}",
        "asset": f"asset{i}",
        "avgPrice": 0.5,
        "totalBought": 10.0,
        "realizedPnl": 2.0,
        "curPrice": 0.0,
        "slug": f"market-{i}",
        "title": f"Market {i}",
        "outcome": "Yes",
        "outcomeIndex": 0,
        # Spread over 60 days: 60 positions -> 61 distinct active dates.
        "timestamp": 1_700_000_000 + i * 86_400,
        "category": "Crypto",
    }


def _raw_buy(i: int) -> dict:
    return {
        "proxyWallet": WALLET,
        "side": "BUY",
        "type": "TRADE",
        "conditionId": f"open{i}",
        "asset": f"openasset{i}",
        "slug": f"open-{i}",
        "transactionHash": f"0xopen{i}",
        "timestamp": 1_700_000_000 + i,
        "price": 0.5,
        "size": 10.0,
        "usdcSize": 5.0,
        "title": "Open market",
        "outcome": "Yes",
    }


def _client_with_pages(activity_pages, closed_pages):
    client = PolymarketApiClient.__new__(PolymarketApiClient)
    client.get_wallet_activity = (
        lambda wallet, limit=100, offset=0: (
            activity_pages[offset // limit]
            if offset // limit < len(activity_pages) else []
        )
    )
    client.get_wallet_closed_positions = (
        lambda wallet, limit=50, offset=0, sort_by="TIMESTAMP", sort_direction="DESC": (
            closed_pages[offset // limit]
            if offset // limit < len(closed_pages) else []
        )
    )
    client.get_wallet_positions = lambda wallet: []
    client.get_wallet_value = lambda wallet: 1000.0
    return client


def _run_scan(activity_pages, closed_pages, max_pages):
    import main

    client = _client_with_pages(activity_pages, closed_pages)
    with patch.object(settings, "MAX_HISTORY_PAGES", max_pages), \
         patch("main.scan_top_wallets", return_value=[WALLET]), \
         patch("main.PolymarketApiClient", return_value=client), \
         patch("main.load_wallet_config", return_value=[]), \
         patch("main.save_wallet_config") as mock_save:
        main.cmd_scan_wallets(None)

    assert mock_save.called
    return {w["wallet"]: w for w in mock_save.call_args[0][0]}[WALLET]


# ---------------------------------------------------------------------------
# Engine semantics
# ---------------------------------------------------------------------------
class TestExitHistoryCompleteness:
    def test_unverified_exit_history_is_unknown_not_true(self):
        stats = compute_wallet_statistics(
            WALLET, _resolution_positions(), exit_history_complete=False
        )
        assert stats.is_resolution_only is None

    def test_complete_exit_history_still_detects_resolution_only(self):
        stats = compute_wallet_statistics(
            WALLET, _resolution_positions(), exit_history_complete=True
        )
        assert stats.is_resolution_only is True

    def test_incomplete_history_defaults_exit_history_to_unknown(self):
        stats = compute_wallet_statistics(
            WALLET, _resolution_positions(), is_complete_history=False
        )
        assert stats.is_resolution_only is None
        assert stats.retrieval_status == "PARTIAL"

    def test_observed_exit_is_proof_even_with_truncated_history(self):
        positions = _resolution_positions(4) + [
            _position(
                "exit1",
                settlement_type=PositionSettlementType.TRADING_EXIT,
                settlement_class=PositionSettlementClass.CLOSED,
            )
        ]
        stats = compute_wallet_statistics(
            WALLET, positions, exit_history_complete=False
        )
        assert stats.is_resolution_only is False

    def test_explicit_exit_flag_overrides_complete_history(self):
        """exit_history_complete=False wins even if the overall flag is True."""
        stats = compute_wallet_statistics(
            WALLET,
            _resolution_positions(),
            is_complete_history=True,
            exit_history_complete=False,
        )
        assert stats.is_resolution_only is None


# ---------------------------------------------------------------------------
# Filter behaviour
# ---------------------------------------------------------------------------
def _passing_except_resolution_only(**overrides) -> WalletStatistics:
    values = dict(
        wallet=WALLET,
        positions_counted=45,
        roi_fee_exclusive_pct=25.0,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=30.0,
        is_resolution_only=None,
        category_mix={"sports": 45},
    )
    values.update(overrides)
    return WalletStatistics(**values)


class TestFilterBehaviour:
    def test_unknown_is_insufficient_data_not_a_rejection(self):
        result = filter_wallet(_passing_except_resolution_only(is_resolution_only=None))

        assert result.status == "INSUFFICIENT_DATA"
        assert result.passed is True
        assert "is_resolution_only" in result.insufficient_criteria
        assert not any("resolution-only" in r for r in result.reasons)

    def test_known_resolution_only_is_still_rejected(self):
        result = filter_wallet(_passing_except_resolution_only(is_resolution_only=True))

        assert result.status == "FAIL"
        assert any("resolution-only" in r for r in result.reasons)

    def test_known_trader_passes_this_criterion(self):
        result = filter_wallet(_passing_except_resolution_only(is_resolution_only=False))

        assert result.status == "PASS"
        assert "is_resolution_only" not in result.insufficient_criteria


# ---------------------------------------------------------------------------
# End-to-end: the reproduction from the diagnostic report
# ---------------------------------------------------------------------------
class TestEndToEndDefectB:
    def test_truncated_sell_history_is_not_rejected_as_resolution_only(self):
        # 60 closed positions (complete history) with no SELL fills in the
        # activity window; that window is truncated at the page cap, so the
        # exits may simply be unseen.
        closed_pages = [
            [_raw_closed(i) for i in range(50)],
            [_raw_closed(50 + i) for i in range(10)],
        ]
        activity_pages = [
            [_raw_buy(p * 100 + i) for i in range(100)]
            for p in range(2)  # full final page at max_pages=2
        ]

        entry = _run_scan(activity_pages, closed_pages, max_pages=2)

        assert entry["activity_truncated"] is True
        assert entry["hard_filter_status"] == "INSUFFICIENT_DATA"
        assert "is_resolution_only" in entry["hard_filter_insufficient_criteria"]
        assert not any("resolution-only" in r for r in entry["hard_filter_reasons"])
        assert entry["recommendation"] == "HARD_FILTER_INSUFFICIENT_DATA"

    def test_complete_sell_history_still_rejects_resolution_only(self):
        # Same positions, but the activity history ends on a short page, so
        # the absence of SELL fills is real evidence.
        closed_pages = [
            [_raw_closed(i) for i in range(50)],
            [_raw_closed(50 + i) for i in range(10)],
        ]
        activity_pages = [[_raw_buy(i) for i in range(3)]]

        entry = _run_scan(activity_pages, closed_pages, max_pages=5)

        assert entry["activity_truncated"] is False
        assert entry["hard_filter_status"] == "FAIL"
        assert any("resolution-only" in r for r in entry["hard_filter_reasons"])
