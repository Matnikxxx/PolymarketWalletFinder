"""
Tests for the wallet-analytics pipeline fix: real statistics from
/closed-positions + /positions + /value, honest data_quality, and N/A
(None) vs a genuinely-computed 0.0 being kept distinct all the way to the
dashboard.
"""
from analytics.wallet_stats import (
    compute_wallet_stats, enrich_with_closed_positions, attach_open_positions,
    attach_portfolio_value, DataQuality,
)
from helpers import make_event


def _round_trip(wallet, n, price_in=0.4, price_out=0.55, start_day=1):
    events = []
    for i in range(n):
        day = start_day + i
        cid = f"c{i}"
        events.append(make_event(wallet=wallet, side="BUY", price=price_in, size=100,
                                  condition_id=cid, token_id=f"t{i}", slug=f"s{i}",
                                  market_title=f"Market {i}", tx_hash=f"buy{i}",
                                  timestamp=f"2026-01-{day:02d}T00:00:00+00:00"))
        events.append(make_event(wallet=wallet, side="SELL", price=price_out, size=100,
                                  condition_id=cid, token_id=f"t{i}", slug=f"s{i}",
                                  market_title=f"Market {i}", tx_hash=f"sell{i}",
                                  timestamp=f"2026-01-{day:02d}T01:00:00+00:00"))
    return events


# 1. Healthy wallet with activity -------------------------------------------
def test_healthy_wallet_with_activity_gets_activity_only_quality():
    events = _round_trip("0xhealthy", 5)
    stats = compute_wallet_stats("0xhealthy", events)
    assert stats.data_quality == DataQuality.ACTIVITY_ONLY
    assert stats.closed_positions == 5
    assert stats.total_events == 10
    assert stats.buy_trades == 5 and stats.sell_trades == 5


# 2. Wallet with no activity --------------------------------------------------
def test_wallet_with_no_activity_is_insufficient_data():
    stats = compute_wallet_stats("0xempty", [])
    assert stats.data_quality == DataQuality.INSUFFICIENT_DATA
    assert stats.total_events == 0
    assert stats.roi_pct == 0.0  # internal scoring default, NOT what gets persisted/displayed


# 3. Wallet with only one trade ------------------------------------------------
def test_wallet_with_single_trade_has_no_closed_positions():
    events = [make_event(wallet="0xone", side="BUY", price=0.4, size=10,
                          condition_id="c1", token_id="t1", slug="s1", tx_hash="only1")]
    stats = compute_wallet_stats("0xone", events)
    assert stats.closed_positions == 0
    assert stats.data_quality == DataQuality.PARTIAL
    assert stats.total_positions == 1


# 4. Wallet with buys and sells (already covered structurally, explicit assert) --
def test_wallet_with_buys_and_sells_computes_buy_sell_counts_and_volume():
    events = _round_trip("0xbs", 3)
    stats = compute_wallet_stats("0xbs", events)
    assert stats.buy_trades == 3
    assert stats.sell_trades == 3
    assert stats.total_volume > 0
    assert stats.largest_trade > 0


# 5. Wallet with closed positions (authoritative enrichment) --------------------
def test_wallet_with_closed_positions_gets_full_data_quality_and_authoritative_numbers():
    events = _round_trip("0xclosed", 4)
    stats = compute_wallet_stats("0xclosed", events)
    raw_closed = [
        {
            "proxyWallet": "0xclosed", "asset": f"t{i}", "conditionId": f"c{i}",
            "avgPrice": 0.4, "totalBought": 100, "realizedPnl": 12.5,
            "curPrice": 0.55, "timestamp": 1767225600 + i * 100,
            "title": f"Market {i}", "slug": f"s{i}", "outcome": "Yes",
        }
        for i in range(4)
    ]
    enriched = enrich_with_closed_positions(stats, raw_closed, api_ok=True)
    assert enriched.data_quality == DataQuality.FULL
    assert enriched.pnl_source == "closed_positions_api"
    assert enriched.total_realized_pnl == 50.0  # 4 * 12.5, straight from Polymarket's own numbers
    assert enriched.win_rate_pct == 100.0
    assert enriched.roi_pct > 0


# 6. Wallet with open positions ------------------------------------------------
def test_wallet_with_open_positions_sets_active_positions_count():
    stats = compute_wallet_stats("0xopen", _round_trip("0xopen", 1))
    enriched = attach_open_positions(stats, [{"conditionId": "a"}, {"conditionId": "b"}], api_ok=True)
    assert enriched.active_positions == 2


# 7. Wallet API failure --------------------------------------------------------
def test_closed_positions_api_failure_does_not_crash_or_downgrade_existing_data():
    events = _round_trip("0xfail", 3)
    stats = compute_wallet_stats("0xfail", events)
    original_quality = stats.data_quality
    enriched = enrich_with_closed_positions(stats, [], api_ok=False)
    assert enriched.data_quality == original_quality  # unchanged, not silently worsened
    assert "API call failed" in enriched.data_quality_reason


def test_open_positions_api_failure_leaves_active_positions_none_not_zero():
    stats = compute_wallet_stats("0xfail2", _round_trip("0xfail2", 1))
    enriched = attach_open_positions(stats, [], api_ok=False)
    assert enriched.active_positions is None  # NOT 0 -- 0 would falsely claim "no open positions"


def test_portfolio_value_unavailable_is_none_not_zero():
    stats = compute_wallet_stats("0xfail3", _round_trip("0xfail3", 1))
    enriched = attach_portfolio_value(stats, None)
    assert enriched.portfolio_value is None


# 8. Partial API data (activity works, closed-positions empty but successful) --
def test_partial_data_empty_closed_positions_response_is_not_treated_as_failure():
    events = _round_trip("0xpartial", 2)
    stats = compute_wallet_stats("0xpartial", events)
    enriched = enrich_with_closed_positions(stats, [], api_ok=True)  # genuinely zero closed positions
    assert enriched.data_quality == DataQuality.ACTIVITY_ONLY  # unchanged, this was real info not a failure
    assert enriched.pnl_source == "activity_reconstruction"


# 9 & 10. Dashboard displays real statistics AND still shows N/A when genuinely unavailable --
def test_dashboard_wallet_row_shows_real_numbers_when_data_quality_full(tmp_path, monkeypatch):
    from config.settings import settings
    from wallet.monitor import save_wallet_config
    from analytics.dashboard import _wallet_table

    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)
    monkeypatch.setattr(settings, "VERIFICATION_FILE", tmp_path / "verification.json")

    save_wallet_config([{
        "wallet": "0xrealdata", "enabled": False,
        "last_score": 80.0, "recommendation": "RECOMMENDED",
        "data_quality": "FULL", "data_quality_reason": "closed-positions used",
        "win_rate_pct": 75.0, "roi_pct": 22.5, "drawdown_pct": 3.0, "realized_pnl": 120.0,
        "num_positions": 10, "avg_position_size": 50.0, "risk_score": 8.0,
    }], path=wallets_file)

    rows = _wallet_table()
    assert len(rows) == 1
    assert rows[0]["roi_pct"] == 22.5
    assert rows[0]["win_rate_pct"] == 75.0
    assert rows[0]["data_quality"] == "FULL"


def test_dashboard_wallet_row_shows_none_when_genuinely_unavailable(tmp_path, monkeypatch):
    from config.settings import settings
    from wallet.monitor import save_wallet_config
    from analytics.dashboard import _wallet_table

    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)
    monkeypatch.setattr(settings, "VERIFICATION_FILE", tmp_path / "verification.json")

    # A config that predates stats persistence, or a wallet that was never
    # scanned -- these keys are simply absent.
    save_wallet_config([{"wallet": "0xnodata", "enabled": False}], path=wallets_file)

    rows = _wallet_table()
    assert rows[0]["roi_pct"] is None
    assert rows[0]["win_rate_pct"] is None
    assert rows[0]["drawdown_pct"] is None
    assert rows[0]["data_quality"] == "INSUFFICIENT_DATA"  # _wallet_row's own default


def test_dashboard_renders_na_and_real_numbers_together(tmp_path, monkeypatch):
    """End-to-end: the actual rendered HTML shows N/A for one wallet and a
    real number for another, in the same page load."""
    from config.settings import settings
    from wallet.monitor import save_wallet_config

    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)
    monkeypatch.setattr(settings, "VERIFICATION_FILE", tmp_path / "verification.json")
    monkeypatch.setattr(settings, "SEEN_EVENTS_FILE", tmp_path / "seen_events.json")
    monkeypatch.setattr(settings, "WALLET_ACTIVITY_LOG_FILE", tmp_path / "wallet_activity_log.json")

    save_wallet_config([
        {"wallet": "0xreal000000000000000000000000000000001", "enabled": False,
         "last_score": 80.0, "recommendation": "RECOMMENDED", "data_quality": "FULL",
         "win_rate_pct": 66.7, "roi_pct": 15.2, "drawdown_pct": 2.1, "realized_pnl": 88.0,
         "num_positions": 6, "avg_position_size": 40.0, "risk_score": 5.0},
        {"wallet": "0xempty0000000000000000000000000000001", "enabled": False,
         "last_score": 25.0, "recommendation": "REJECT", "data_quality": "INSUFFICIENT_DATA"},
    ], path=wallets_file)

    from dashboard.app import create_app
    app = create_app()
    app.testing = True
    html = app.test_client().get("/").get_data(as_text=True)

    assert "+15.2%" in html
    assert "+$88.00" in html
    assert "Insufficient data" in html
