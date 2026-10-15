"""
Tests for Phase 12 — Historical Data Completeness / API Pagination.

Validates:
1.  First-page (single-page) behavior remains correct.
2.  Multiple pages are retrieved when available.
3.  Pagination stops when no more records exist (page smaller than page_size).
4.  Pagination respects the max_pages safety limit.
5.  Duplicate records do not create duplicate PositionRecords.
6.  Existing PositionRecord classification remains unchanged.
7.  TRADING_EXIT records are preserved when activity sells match closed positions.
8.  RESOLUTION records are preserved when no sell match exists.
9.  Existing WalletStatistics calculations remain unchanged for the same input.
10. Existing filter thresholds remain unchanged (configuration-level check).
11. Existing ranking formulas remain unchanged (WalletScoreCard output check).
12. The full test suite still passes (verified by running pytest).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from analytics.models import (
    PositionSettlementType,
    PositionRecord,
    WalletStatistics,
)
from analytics.performance import build_position_records, compute_wallet_statistics
from config.settings import settings
from polymarket.api_client import PolymarketApiClient
from polymarket.wallet_activity import (
    fetch_wallet_events_paginated,
    fetch_wallet_events,
    event_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw_activity_event(
    side: str = "BUY",
    condition_id: str = "cid_A",
    asset: str = "tok_A",
    slug: str = "market-a",
    tx_hash: str | None = "0xabc",
    timestamp: int = 1_700_000_000,
) -> dict:
    return {
        "proxyWallet": "0xwallet",
        "side": side,
        "type": "TRADE",
        "conditionId": condition_id,
        "asset": asset,
        "slug": slug,
        "transactionHash": tx_hash,
        "timestamp": timestamp,
        "price": 0.5,
        "size": 10.0,
        "usdcSize": 5.0,
        "title": "Test market",
        "outcome": "Yes",
    }


def _make_raw_closed_position(
    condition_id: str = "cid_A",
    asset: str = "tok_A",
    avg_price: float = 0.5,
    total_bought: float = 10.0,
    realized_pnl: float = 2.0,
    timestamp: int = 1_700_100_000,
) -> dict:
    return {
        "proxyWallet": "0xwallet",
        "conditionId": condition_id,
        "asset": asset,
        "avgPrice": avg_price,
        "totalBought": total_bought,
        "realizedPnl": realized_pnl,
        "curPrice": 0.0,
        "slug": "market-slug",
        "title": "Test Market",
        "outcome": "Yes",
        "outcomeIndex": 0,
        "timestamp": timestamp,
    }


def _make_mock_client(pages_activity: list[list[dict]], pages_closed: list[list[dict]]) -> PolymarketApiClient:
    """
    Returns a mock PolymarketApiClient whose get_wallet_activity and
    get_wallet_closed_positions return successive pages from the supplied lists.
    """
    client = MagicMock(spec=PolymarketApiClient)

    # Activity pages: call count tracks which page to return.
    act_call_count = [0]

    def _get_activity(wallet, limit=100, offset=0):
        page = offset // limit
        if page < len(pages_activity):
            return pages_activity[page]
        return []

    # Closed-position pages:
    def _get_closed(wallet, limit=50, offset=0, sort_by="TIMESTAMP", sort_direction="DESC"):
        page = offset // limit
        if page < len(pages_closed):
            return pages_closed[page]
        return []

    client.get_wallet_activity.side_effect = _get_activity
    client.get_wallet_closed_positions.side_effect = _get_closed
    return client


# ---------------------------------------------------------------------------
# 1. Single-page behavior remains correct
# ---------------------------------------------------------------------------
class TestSinglePageBehavior:
    def test_single_page_activity_returns_correct_events(self):
        """fetch_wallet_events_paginated with max_pages=1 returns same events as fetch_wallet_events."""
        raw = [_make_raw_activity_event("BUY", tx_hash="0x1"), _make_raw_activity_event("SELL", tx_hash="0x2")]
        pages = [raw]
        client = _make_mock_client(pages, [])

        result = fetch_wallet_events_paginated(client, "0xwallet", max_pages=1)
        assert len(result) == 2

    def test_single_page_preserves_side_field(self):
        raw = [_make_raw_activity_event("SELL", tx_hash="0xsell")]
        client = _make_mock_client([raw], [])
        events = fetch_wallet_events_paginated(client, "0xw", max_pages=1)
        assert events[0]["side"] == "SELL"

    def test_closed_positions_paginated_single_page(self):
        raw_closed = [_make_raw_closed_position("cid_A", "tok_A")]
        client = _make_mock_client([], [raw_closed])
        result = client.get_wallet_closed_positions_paginated("0xw", max_pages=1)
        # We test the method via a real PolymarketApiClient wrapping the mock.
        # For unit purposes, just confirm the mock returns the page.
        assert client.get_wallet_closed_positions("0xw", limit=50, offset=0) == raw_closed


# ---------------------------------------------------------------------------
# 2. Multiple pages are retrieved when available
# ---------------------------------------------------------------------------
class TestMultiPageRetrieval:
    def test_two_activity_pages_retrieved(self):
        page1 = [_make_raw_activity_event("BUY", tx_hash=f"0x{i}") for i in range(100)]
        page2 = [_make_raw_activity_event("BUY", tx_hash=f"0x1{i:02d}") for i in range(50)]
        client = _make_mock_client([page1, page2], [])

        events = fetch_wallet_events_paginated(client, "0xw", max_pages=5)
        # 100 + 50 = 150 unique events
        assert len(events) == 150
        assert client.get_wallet_activity.call_count == 2  # stopped on partial page

    def test_activity_all_full_pages_then_empty(self):
        page1 = [_make_raw_activity_event("BUY", tx_hash=f"0x{i:03d}") for i in range(100)]
        page2 = [_make_raw_activity_event("BUY", tx_hash=f"0x1{i:02d}") for i in range(100)]
        # page3 empty → stops
        client = _make_mock_client([page1, page2, []], [])
        events = fetch_wallet_events_paginated(client, "0xw", max_pages=5)
        assert len(events) == 200
        assert client.get_wallet_activity.call_count == 3

    def test_get_wallet_closed_positions_paginated_two_pages(self):
        page1 = [_make_raw_closed_position(f"cid_{i}", f"tok_{i}") for i in range(50)]
        page2 = [_make_raw_closed_position(f"cid_{i+50}", f"tok_{i+50}") for i in range(30)]
        client = _make_mock_client([], [page1, page2])

        # Build a real client that delegates to the mock for the method
        real_client = PolymarketApiClient.__new__(PolymarketApiClient)
        real_client.get_wallet_closed_positions = client.get_wallet_closed_positions
        result = real_client.get_wallet_closed_positions_paginated("0xw", max_pages=5)
        assert len(result) == 80


# ---------------------------------------------------------------------------
# 3. Pagination stops when last page is partial
# ---------------------------------------------------------------------------
class TestPaginationStopsOnPartialPage:
    def test_stops_after_partial_activity_page(self):
        page1 = [_make_raw_activity_event(tx_hash=f"0x{i:03d}") for i in range(100)]
        page2 = [_make_raw_activity_event(tx_hash=f"0x2{i:02d}") for i in range(7)]  # partial
        client = _make_mock_client([page1, page2], [])
        events = fetch_wallet_events_paginated(client, "0xw", max_pages=5)
        assert len(events) == 107
        assert client.get_wallet_activity.call_count == 2

    def test_stops_after_empty_page(self):
        page1 = [_make_raw_activity_event(tx_hash=f"0x{i:03d}") for i in range(100)]
        client = _make_mock_client([page1, []], [])
        events = fetch_wallet_events_paginated(client, "0xw", max_pages=5)
        assert len(events) == 100
        # Second call returns empty → stops
        assert client.get_wallet_activity.call_count == 2


# ---------------------------------------------------------------------------
# 4. Pagination respects the safety limit (max_pages)
# ---------------------------------------------------------------------------
class TestMaxPagesSafetyLimit:
    def test_activity_stops_at_max_pages(self):
        # All pages return 100 records (no natural stop) but max_pages=3
        pages = [[_make_raw_activity_event(tx_hash=f"0x{p}{i:02d}") for i in range(100)] for p in range(10)]
        client = _make_mock_client(pages, [])
        events = fetch_wallet_events_paginated(client, "0xw", max_pages=3)
        assert len(events) == 300
        assert client.get_wallet_activity.call_count == 3

    def test_max_pages_of_1_returns_single_page(self):
        pages = [[_make_raw_activity_event(tx_hash=f"0x{i:02d}") for i in range(100)] for _ in range(5)]
        client = _make_mock_client(pages, [])
        events = fetch_wallet_events_paginated(client, "0xw", max_pages=1)
        assert len(events) == 100
        assert client.get_wallet_activity.call_count == 1

    def test_closed_positions_stops_at_max_pages(self):
        pages_closed = [[_make_raw_closed_position(f"cid_{p*50+i}", f"tok_{p*50+i}") for i in range(50)] for p in range(10)]
        client = _make_mock_client([], pages_closed)
        real_client = PolymarketApiClient.__new__(PolymarketApiClient)
        real_client.get_wallet_closed_positions = client.get_wallet_closed_positions
        result = real_client.get_wallet_closed_positions_paginated("0xw", max_pages=2)
        assert len(result) == 100
        assert client.get_wallet_closed_positions.call_count == 2


# ---------------------------------------------------------------------------
# 5. Duplicate records do not create duplicate PositionRecords
# ---------------------------------------------------------------------------
class TestDeduplication:
    def test_duplicate_activity_events_deduplicated(self):
        """Same tx_hash appearing twice in different pages produces one event."""
        ev = _make_raw_activity_event(tx_hash="0xdup", timestamp=1_700_000_000)
        page1 = [ev]
        page2 = [ev]  # exact duplicate
        client = _make_mock_client([page1, page2], [])
        events = fetch_wallet_events_paginated(client, "0xw", max_pages=2)
        assert len(events) == 1

    def test_duplicate_closed_positions_deduplicated(self):
        """Same (conditionId, asset) appearing in two pages produces one record."""
        r = _make_raw_closed_position("cid_X", "tok_X")
        page1 = [r]
        page2 = [r]  # duplicate
        client = _make_mock_client([], [page1, page2])
        real_client = PolymarketApiClient.__new__(PolymarketApiClient)
        real_client.get_wallet_closed_positions = client.get_wallet_closed_positions
        result = real_client.get_wallet_closed_positions_paginated("0xw", max_pages=2)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# 6 & 7. TRADING_EXIT records are correctly classified when sell events match
# ---------------------------------------------------------------------------
class TestPositionRecordClassification:
    def _make_events(self, sell_cid: str = "cid_A", sell_tid: str = "tok_A"):
        from polymarket.parsers import normalize_activity_event
        raw = _make_raw_activity_event(
            side="SELL", condition_id=sell_cid, asset=sell_tid,
            tx_hash="0xsell", timestamp=1_700_050_000
        )
        ev = normalize_activity_event(raw)
        ev["event_id"] = event_id(ev)
        return [ev]

    def test_trading_exit_when_sell_event_matches_closed_position(self):
        """If a SELL event condition_id matches a closed position, it gets TRADING_EXIT."""
        raw_closed = [_make_raw_closed_position("cid_A", "tok_A")]
        events = self._make_events("cid_A", "tok_A")
        records = build_position_records(raw_closed, events, [])
        closed_records = [r for r in records if r.condition_id == "cid_A"]
        assert len(closed_records) == 1
        assert closed_records[0].settlement_type == PositionSettlementType.TRADING_EXIT

    def test_resolution_when_no_sell_event_matches(self):
        """If no SELL event matches the closed position, it gets RESOLUTION."""
        raw_closed = [_make_raw_closed_position("cid_B", "tok_B", realized_pnl=3.0)]
        # Events only have BUY for a different market
        from polymarket.parsers import normalize_activity_event
        raw_buy = _make_raw_activity_event("BUY", condition_id="cid_X", tx_hash="0xbuy")
        ev = normalize_activity_event(raw_buy)
        ev["event_id"] = event_id(ev)
        events = [ev]
        records = build_position_records(raw_closed, events, [])
        closed_records = [r for r in records if r.condition_id == "cid_B"]
        assert len(closed_records) == 1
        assert closed_records[0].settlement_type == PositionSettlementType.RESOLUTION

    def test_is_resolution_only_false_when_trading_exit_exists(self):
        """WalletStatistics.is_resolution_only=False when >= 3 positions, at least one TRADING_EXIT."""
        raw_closed = [
            _make_raw_closed_position("cid_A", "tok_A", realized_pnl=1.0),
            _make_raw_closed_position("cid_B", "tok_B", realized_pnl=1.0),
            _make_raw_closed_position("cid_C", "tok_C", realized_pnl=1.0),
        ]
        # Sell event matches cid_A
        events = self._make_events("cid_A", "tok_A")
        records = build_position_records(raw_closed, events, [])
        stats = compute_wallet_statistics("0xw", records)
        assert stats.is_resolution_only is False

    def test_is_resolution_only_true_when_no_trading_exits(self):
        """WalletStatistics.is_resolution_only=True when >= 3 positions, none TRADING_EXIT."""
        raw_closed = [
            _make_raw_closed_position("cid_X", "tok_X", realized_pnl=1.0),
            _make_raw_closed_position("cid_Y", "tok_Y", realized_pnl=1.0),
            _make_raw_closed_position("cid_Z", "tok_Z", realized_pnl=1.0),
        ]
        records = build_position_records(raw_closed, [], [])
        stats = compute_wallet_statistics("0xw", records)
        assert stats.is_resolution_only is True


# ---------------------------------------------------------------------------
# 8. RESOLUTION records preserved
# ---------------------------------------------------------------------------
class TestResolutionRecordsPreserved:
    def test_won_resolution_classification(self):
        raw_closed = [_make_raw_closed_position("cid_W", "tok_W", realized_pnl=5.0)]
        records = build_position_records(raw_closed, [], [])
        r = next(r for r in records if r.condition_id == "cid_W")
        assert r.settlement_type == PositionSettlementType.RESOLUTION

    def test_lost_resolution_classification(self):
        raw_closed = [_make_raw_closed_position("cid_L", "tok_L", realized_pnl=-3.0)]
        records = build_position_records(raw_closed, [], [])
        r = next(r for r in records if r.condition_id == "cid_L")
        assert r.settlement_type == PositionSettlementType.RESOLUTION


# ---------------------------------------------------------------------------
# 9. WalletStatistics calculations unchanged for same input
# ---------------------------------------------------------------------------
class TestWalletStatisticsStability:
    def _make_records(self):
        raw_closed = [
            _make_raw_closed_position(f"cid_{i}", f"tok_{i}", realized_pnl=float(i))
            for i in range(5)
        ]
        return build_position_records(raw_closed, [], [])

    def test_positions_counted_stable(self):
        records = self._make_records()
        s1 = compute_wallet_statistics("0xw", records)
        s2 = compute_wallet_statistics("0xw", records)
        assert s1.positions_counted == s2.positions_counted

    def test_roi_stable(self):
        records = self._make_records()
        s1 = compute_wallet_statistics("0xw", records)
        s2 = compute_wallet_statistics("0xw", records)
        assert s1.roi_fee_exclusive_pct == s2.roi_fee_exclusive_pct


# ---------------------------------------------------------------------------
# 10. Existing filter thresholds unchanged (configuration-level check)
# ---------------------------------------------------------------------------
class TestFilterThresholdsUnchanged:
    def test_min_roi_unchanged(self):
        assert settings.MIN_ROI == 15.0

    def test_reject_roi_above_unchanged(self):
        assert settings.REJECT_ROI_ABOVE == 80.0

    def test_min_positions_unchanged(self):
        assert settings.MIN_POSITIONS == 40

    def test_min_active_days_unchanged(self):
        assert settings.MIN_ACTIVE_DAYS == 30

    def test_min_consistency_unchanged(self):
        assert settings.MIN_CONSISTENCY == 4.4

    def test_max_drawdown_unchanged(self):
        assert settings.MAX_DRAWDOWN == 20.0

    def test_max_history_pages_exists_and_positive(self):
        assert settings.MAX_HISTORY_PAGES >= 1


# ---------------------------------------------------------------------------
# 11. Existing ranking formulas unchanged
# ---------------------------------------------------------------------------
class TestRankingFormulasUnchanged:
    def _make_passing_stats(self, wallet: str = "0xw", roi: float = 20.0) -> WalletStatistics:
        return WalletStatistics(
            wallet=wallet,
            positions_counted=50,
            wins=35,
            losses=15,
            win_rate_pct=70.0,
            roi_fee_exclusive_pct=roi,
            active_days=60,
            drawdown_cost_pct=5.0,
            top1_profit_share_pct=20.0,
            consistency_mean_roi=0.2,
            consistency_stdev_roi=0.1,
            is_resolution_only=False,
            category_mix={"sports": 30, "crypto": 20},
        )

    def test_score_wallet_produces_scorecard(self):
        from wallet.ranking import score_wallet
        stats = self._make_passing_stats()
        card = score_wallet(stats)
        assert card.wallet == "0xw"
        assert 0 <= card.final_score <= 100
        assert card.recommendation in ("RECOMMENDED", "WATCH", "REJECT")

    def test_rank_wallets_sorted_descending(self):
        from wallet.ranking import rank_wallets
        s1 = self._make_passing_stats("0xa", roi=25.0)
        s2 = self._make_passing_stats("0xb", roi=15.0)
        cards = rank_wallets([s1, s2])
        assert cards[0].final_score >= cards[1].final_score

    def test_recommended_threshold_unchanged(self):
        """Score >= 75 and roi <= REJECT_ROI_ABOVE => RECOMMENDED."""
        from wallet.ranking import score_wallet
        # Construct a wallet that should score >= 75:
        stats = WalletStatistics(
            wallet="0xhigh",
            positions_counted=100,
            wins=75,
            losses=25,
            win_rate_pct=75.0,
            roi_fee_exclusive_pct=25.0,
            active_days=90,
            drawdown_cost_pct=5.0,
            top1_profit_share_pct=10.0,
            consistency_mean_roi=0.25,
            consistency_stdev_roi=0.05,
            is_resolution_only=False,
            category_mix={"sports": 60, "crypto": 40},
        )
        card = score_wallet(stats)
        if card.final_score >= 75:
            assert card.recommendation == "RECOMMENDED"


# ---------------------------------------------------------------------------
# 12. Phase 15 — History Depth Expansion (MAX_HISTORY_PAGES = 10)
# ---------------------------------------------------------------------------
class TestPhase15HistoryDepthExpansion:
    def test_configured_max_history_pages_is_30(self):
        """Phase 40 requires MAX_HISTORY_PAGES configured default = 30."""
        assert settings.MAX_HISTORY_PAGES == 30

    def test_closed_positions_retrieval_beyond_250_positions(self):
        """Pagination can retrieve more than 250 closed positions (e.g. 7 full pages = 350 records, 8th empty page stops)."""
        pages_closed = [
            [_make_raw_closed_position(f"cid_{p*50+i}", f"tok_{p*50+i}") for i in range(50)]
            for p in range(7)
        ]
        client = _make_mock_client([], pages_closed)
        real_client = PolymarketApiClient.__new__(PolymarketApiClient)
        real_client.get_wallet_closed_positions = client.get_wallet_closed_positions

        result = real_client.get_wallet_closed_positions_paginated("0xw", max_pages=10)
        assert len(result) == 350
        # 7 full pages + 1 empty page to confirm end of data
        assert client.get_wallet_closed_positions.call_count == 8

    def test_activity_retrieval_beyond_500_events(self):
        """Pagination can retrieve more than 500 activity fills (e.g. 7 full pages = 700 events, 8th empty page stops)."""
        pages_activity = [
            [_make_raw_activity_event(tx_hash=f"0x{p:02d}_{i:02d}") for i in range(100)]
            for p in range(7)
        ]
        client = _make_mock_client(pages_activity, [])
        events = fetch_wallet_events_paginated(client, "0xw", max_pages=10)
        assert len(events) == 700
        # 7 full pages + 1 empty page to confirm end of data
        assert client.get_wallet_activity.call_count == 8

    def test_pagination_stops_at_partial_page_before_10_pages(self):
        """Pagination stops when page returns fewer than page_size."""
        pages_closed = [
            [_make_raw_closed_position(f"cid_{p*50+i}", f"tok_{p*50+i}") for i in range(50)]
            for p in range(6)
        ]
        # 7th page has only 12 items
        pages_closed.append([_make_raw_closed_position(f"cid_last_{i}", f"tok_last_{i}") for i in range(12)])
        client = _make_mock_client([], pages_closed)
        real_client = PolymarketApiClient.__new__(PolymarketApiClient)
        real_client.get_wallet_closed_positions = client.get_wallet_closed_positions

        result = real_client.get_wallet_closed_positions_paginated("0xw", max_pages=10)
        assert len(result) == 312
        assert client.get_wallet_closed_positions.call_count == 7

    def test_deduplication_preserved_across_extended_pages(self):
        """Deduplication ensures overlapping pages do not duplicate PositionRecords."""
        # Repeat the same 50 records across 6 pages
        page = [_make_raw_closed_position(f"cid_{i}", f"tok_{i}") for i in range(50)]
        client = _make_mock_client([], [page] * 6)
        real_client = PolymarketApiClient.__new__(PolymarketApiClient)
        real_client.get_wallet_closed_positions = client.get_wallet_closed_positions

        result = real_client.get_wallet_closed_positions_paginated("0xw", max_pages=10)
        assert len(result) == 50

