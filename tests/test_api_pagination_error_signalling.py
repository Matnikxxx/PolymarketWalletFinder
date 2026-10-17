"""
Phase B tests for the opt-in error signalling on the paginated fetch helpers.

The Phase 12 pagination helpers are best-effort by default: a failing page
ends pagination and whatever was collected so far is returned. Phase B's
stale-data handling needs to tell "the history is empty" apart from "the
request failed", so both helpers accept `raise_on_error=True`.

These tests pin down BOTH behaviours, so the default Phase 12 behaviour cannot
be changed accidentally and the new opt-in behaviour is documented by test.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from polymarket.api_client import PolymarketApiClient, ApiError
from polymarket.wallet_activity import fetch_wallet_events_paginated

WALLET = "0x" + "11" * 20


def _closed_row(index: int) -> dict:
    return {"conditionId": f"0xc{index}", "asset": f"t{index}", "realizedPnl": 1.0, "timestamp": 1700000000 + index}


def _raw_event(index: int, tx_hash: str | None = None) -> dict:
    return {
        "proxyWallet": WALLET,
        "type": "TRADE",
        "side": "BUY",
        "conditionId": f"0xc{index}",
        "asset": f"t{index}",
        "price": 0.5,
        "size": 10.0,
        "usdcSize": 5.0,
        "timestamp": 1700000000 + index,
        "transactionHash": tx_hash or f"0xtx{index}",
    }


# ---------------------------------------------------------------------------
# Closed positions
# ---------------------------------------------------------------------------

def test_closed_positions_default_behaviour_still_swallows_page_failure():
    client = PolymarketApiClient(session=MagicMock())
    calls = {"n": 0}

    def _get(wallet, limit=50, offset=0, sort_by="TIMESTAMP", sort_direction="DESC"):
        calls["n"] += 1
        if offset == 0:
            return [_closed_row(i) for i in range(50)]
        raise ApiError("page 2 failed")

    client.get_wallet_closed_positions = _get

    records = client.get_wallet_closed_positions_paginated(WALLET, max_pages=3)

    assert len(records) == 50          # partial data returned, nothing raised
    assert calls["n"] == 2


def test_closed_positions_raise_on_error_propagates():
    client = PolymarketApiClient(session=MagicMock())

    def _get(wallet, limit=50, offset=0, sort_by="TIMESTAMP", sort_direction="DESC"):
        if offset == 0:
            return [_closed_row(i) for i in range(50)]
        raise ApiError("page 2 failed")

    client.get_wallet_closed_positions = _get

    with pytest.raises(ApiError):
        client.get_wallet_closed_positions_paginated(WALLET, max_pages=3, raise_on_error=True)


def test_closed_positions_successful_empty_page_is_not_an_error():
    client = PolymarketApiClient(session=MagicMock())
    client.get_wallet_closed_positions = lambda *a, **kw: []

    assert client.get_wallet_closed_positions_paginated(WALLET, raise_on_error=True) == []


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------

def test_activity_default_behaviour_still_swallows_page_failure():
    client = MagicMock(spec=PolymarketApiClient)
    calls = {"n": 0}

    def _get_activity(wallet, limit=100, offset=0):
        calls["n"] += 1
        if offset == 0:
            return [_raw_event(i) for i in range(100)]
        raise ApiError("page 2 failed")

    client.get_wallet_activity.side_effect = _get_activity

    events = fetch_wallet_events_paginated(client, WALLET, max_pages=3)

    assert len(events) == 100          # partial data returned, nothing raised
    assert calls["n"] == 2


def test_activity_raise_on_error_propagates():
    client = MagicMock(spec=PolymarketApiClient)
    client.get_wallet_activity.side_effect = ApiError("activity down")

    with pytest.raises(ApiError):
        fetch_wallet_events_paginated(client, WALLET, max_pages=3, raise_on_error=True)


def test_activity_successful_empty_page_is_not_an_error():
    client = MagicMock(spec=PolymarketApiClient)
    client.get_wallet_activity.return_value = []

    assert fetch_wallet_events_paginated(client, WALLET, raise_on_error=True) == []
