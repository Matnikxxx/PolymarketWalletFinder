"""
Regression tests for P1: pagination completeness must be observable.

A history fetch that stopped on a full page at ``MAX_HISTORY_PAGES`` may have
left older data unfetched. These tests pin down the contract:

* only a short/empty page proves completeness;
* a full final page at the cap is reported as truncated (never "complete
  merely because the loop ended");
* the completeness flag reaches ``WalletStatistics.retrieval_status`` and the
  persisted wallet record, and explains the PARTIAL label;
* the result stays a plain ``list`` for every existing caller.
"""
from __future__ import annotations

from unittest.mock import patch

from analytics.performance import compute_wallet_statistics
from config.settings import settings
from polymarket.api_client import ApiError, PolymarketApiClient
from polymarket.pagination import (
    STOP_API_ERROR,
    STOP_EXHAUSTED,
    STOP_PAGE_CAP,
    PaginationInfo,
    history_is_complete,
    pagination_of,
)
from polymarket.wallet_activity import fetch_wallet_events_paginated
from wallet.monitor import merge_wallet_records


WALLET = "0x1111111111111111111111111111111111111111"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _raw_event(i: int) -> dict:
    return {
        "proxyWallet": WALLET,
        "side": "BUY",
        "type": "TRADE",
        "conditionId": f"cond{i}",
        "asset": f"tok{i}",
        "slug": f"market-{i}",
        "transactionHash": f"0xtx{i}",
        "timestamp": 1_700_000_000 + i,
        "price": 0.5,
        "size": 10.0,
        "usdcSize": 5.0,
        "title": "Test market",
        "outcome": "Yes",
    }


def _raw_closed(i: int, step_seconds: int = 300) -> dict:
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
        "timestamp": 1_700_000_000 + i * step_seconds,
    }


def _client_with_pages(activity_pages, closed_pages):
    """Real PolymarketApiClient bound to page-returning callables."""
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


# ---------------------------------------------------------------------------
# PaginationInfo semantics
# ---------------------------------------------------------------------------
class TestPaginationInfo:
    def test_full_final_page_at_cap_is_truncated(self):
        info = PaginationInfo(max_pages=3, stop_reason=STOP_PAGE_CAP)
        assert info.truncated is True
        assert info.is_complete is False

    def test_short_page_is_complete(self):
        info = PaginationInfo(max_pages=3, stop_reason=STOP_EXHAUSTED)
        assert info.truncated is False
        assert info.is_complete is True

    def test_api_error_is_truncated(self):
        info = PaginationInfo(max_pages=3, stop_reason=STOP_API_ERROR)
        assert info.truncated is True


# ---------------------------------------------------------------------------
# Fetcher metadata
# ---------------------------------------------------------------------------
class TestActivityFetcherMetadata:
    def test_full_pages_at_cap_marked_truncated(self):
        pages = [[_raw_event(p * 100 + i) for i in range(100)] for p in range(5)]
        client = _client_with_pages(pages, [])

        events = fetch_wallet_events_paginated(client, WALLET, max_pages=3)

        assert len(events) == 300
        assert events.pagination.stop_reason == STOP_PAGE_CAP
        assert events.pagination.pages_fetched == 3
        assert events.pagination.rows_fetched == 300
        assert events.is_complete is False
        assert events.truncated is True
        assert history_is_complete(events) is False

    def test_short_page_proves_completeness(self):
        pages = [
            [_raw_event(i) for i in range(100)],
            [_raw_event(100 + i) for i in range(7)],
        ]
        client = _client_with_pages(pages, [])

        events = fetch_wallet_events_paginated(client, WALLET, max_pages=5)

        assert len(events) == 107
        assert events.pagination.stop_reason == STOP_EXHAUSTED
        assert events.is_complete is True

    def test_empty_page_after_full_pages_proves_completeness(self):
        pages = [
            [_raw_event(i) for i in range(100)],
            [_raw_event(100 + i) for i in range(100)],
            [],
        ]
        client = _client_with_pages(pages, [])

        events = fetch_wallet_events_paginated(client, WALLET, max_pages=5)

        assert len(events) == 200
        assert events.pagination.pages_fetched == 3
        assert events.is_complete is True

    def test_best_effort_api_error_is_truncated(self):
        calls = {"n": 0}

        def _activity(wallet, limit=100, offset=0):
            calls["n"] += 1
            if calls["n"] == 1:
                return [_raw_event(i) for i in range(100)]
            raise ApiError("boom")

        client = PolymarketApiClient.__new__(PolymarketApiClient)
        client.get_wallet_activity = _activity

        events = fetch_wallet_events_paginated(client, WALLET, max_pages=5)

        assert len(events) == 100
        assert events.pagination.stop_reason == STOP_API_ERROR
        assert events.truncated is True

    def test_rows_fetched_counts_raw_and_unique_rows_counts_deduped(self):
        duplicate = _raw_event(0)
        pages = [
            [_raw_event(i) for i in range(100)],
            [duplicate for _ in range(100)],
        ]
        client = _client_with_pages(pages, [])

        events = fetch_wallet_events_paginated(client, WALLET, max_pages=5)

        assert events.pagination.rows_fetched == 200
        assert events.pagination.unique_rows == 100
        assert len(events) == 100


class TestClosedPositionsFetcherMetadata:
    def test_full_final_page_at_cap_is_truncated(self):
        pages = [[_raw_closed(p * 50 + i) for i in range(50)] for p in range(5)]
        client = _client_with_pages([], pages)

        rows = client.get_wallet_closed_positions_paginated(WALLET, max_pages=2)

        assert len(rows) == 100
        assert rows.pagination.stop_reason == STOP_PAGE_CAP
        assert rows.pagination.pages_fetched == 2
        assert rows.is_complete is False
        assert history_is_complete(rows) is False

    def test_short_final_page_is_complete(self):
        pages = [
            [_raw_closed(i) for i in range(50)],
            [_raw_closed(50 + i) for i in range(3)],
        ]
        client = _client_with_pages([], pages)

        rows = client.get_wallet_closed_positions_paginated(WALLET, max_pages=5)

        assert len(rows) == 53
        assert rows.pagination.stop_reason == STOP_EXHAUSTED
        assert rows.is_complete is True


# ---------------------------------------------------------------------------
# Backwards compatibility
# ---------------------------------------------------------------------------
class TestListCompatibility:
    def test_paged_records_is_a_plain_list_for_callers(self):
        client = _client_with_pages([[ _raw_event(i) for i in range(2) ]], [])

        events = fetch_wallet_events_paginated(client, WALLET, max_pages=5)
        raw_list = list(events)

        assert isinstance(events, list)
        assert events == raw_list
        assert raw_list == events
        assert len(events) == 2
        assert events[0]["event_id"]

    def test_plain_list_without_metadata_is_treated_as_complete(self):
        assert pagination_of([]) is None
        assert pagination_of([{"a": 1}]) is None
        assert history_is_complete([]) is True
        assert history_is_complete([{"a": 1}]) is True

    def test_merge_preserves_truncation_fields_round_trip(self):
        stored = [{"wallet": WALLET, "tracked": True, "history_truncated": True,
                   "activity_pages_fetched": 30}]
        scanned = [{"wallet": WALLET, "history_truncated": False,
                    "activity_pages_fetched": 3}]

        merged = merge_wallet_records(stored, scanned)

        assert merged[0]["tracked"] is True
        assert merged[0]["history_truncated"] is False
        assert merged[0]["activity_pages_fetched"] == 3


# ---------------------------------------------------------------------------
# Analytics propagation
# ---------------------------------------------------------------------------
class TestStatisticsPropagation:
    def test_incomplete_history_yields_partial_status(self):
        partial = compute_wallet_statistics(WALLET, [], is_complete_history=False)
        complete = compute_wallet_statistics(WALLET, [])

        assert partial.retrieval_status == "PARTIAL"
        assert partial.is_complete_history is False
        assert complete.retrieval_status == "INSUFFICIENT"
        assert complete.is_complete_history is True


# ---------------------------------------------------------------------------
# End-to-end: the page cap must reach the persisted record
# ---------------------------------------------------------------------------
def _run_scan_with_pages(activity_pages, closed_pages):
    """Run cmd_scan_wallets against page-returning real fetchers."""
    import main

    client = _client_with_pages(activity_pages, closed_pages)

    with patch("main.scan_top_wallets", return_value=[WALLET]), \
         patch("main.PolymarketApiClient", return_value=client), \
         patch("main.load_wallet_config", return_value=[]), \
         patch("main.save_wallet_config") as mock_save:
        main.cmd_scan_wallets(None)

    assert mock_save.called
    return {w["wallet"]: w for w in mock_save.call_args[0][0]}[WALLET]


class TestEndToEndTruncation:
    def test_capped_history_is_persisted_as_partial(self):
        # 1500 closed positions (30 full pages of 50) inside a 6-day window.
        closed_pages = [
            [_raw_closed(p * 50 + i) for i in range(50)]
            for p in range(settings.MAX_HISTORY_PAGES)
        ]
        activity_pages = [[_raw_event(i) for i in range(3)]]  # short page → complete

        entry = _run_scan_with_pages(activity_pages, closed_pages)

        assert entry["closed_positions_truncated"] is True
        assert entry["activity_truncated"] is False
        assert entry["history_truncated"] is True
        assert entry["closed_positions_stop_reason"] == STOP_PAGE_CAP
        assert entry["closed_positions_pages_fetched"] == settings.MAX_HISTORY_PAGES
        # PARTIAL, never SUFFICIENT, and the reason says why.
        assert entry["data_quality"] == "PARTIAL"
        assert "PARTIAL" in entry["data_quality_reason"]
        assert "page cap" in entry["data_quality_reason"]
        # D2/D3: a day count from a page-capped window can no longer reject
        # the wallet outright. The threshold is still evaluated, but the
        # outcome is INSUFFICIENT_DATA and the criterion is named as
        # indeterminate rather than failed.
        assert entry["hard_filter_status"] == "INSUFFICIENT_DATA"
        assert "active_days" in entry["hard_filter_indeterminate_criteria"]
        assert any("active days" in r for r in entry["hard_filter_reasons"])
        assert "indeterminate" in entry["hard_filter_reasons"][0]

    def test_short_history_is_not_marked_truncated(self):
        # 100 positions: two full pages then an empty page proves completeness.
        closed_pages = [
            [_raw_closed(0 * 50 + i) for i in range(50)],
            [_raw_closed(1 * 50 + i) for i in range(50)],
            [],
        ]
        activity_pages = [[_raw_event(i) for i in range(3)]]

        entry = _run_scan_with_pages(activity_pages, closed_pages)

        assert entry["closed_positions_truncated"] is False
        assert entry["history_truncated"] is False
        assert entry["closed_positions_stop_reason"] == STOP_EXHAUSTED
        assert entry["data_quality"] == "SUFFICIENT"

    def test_activity_truncation_alone_marks_history_truncated(self):
        activity_pages = [
            [_raw_event(p * 100 + i) for i in range(100)]
            for p in range(settings.MAX_HISTORY_PAGES)
        ]
        closed_pages = [[_raw_closed(i) for i in range(5)]]  # complete

        entry = _run_scan_with_pages(activity_pages, closed_pages)

        assert entry["activity_truncated"] is True
        assert entry["closed_positions_truncated"] is False
        assert entry["history_truncated"] is True
        assert entry["activity_stop_reason"] == STOP_PAGE_CAP
        assert entry["data_quality"] == "PARTIAL"
        assert "/activity" in entry["data_quality_reason"]
