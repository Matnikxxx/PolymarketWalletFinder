"""
Phase B tests for safe stale-data handling in main.py::cmd_scan_wallets.

Verified here:
- a successful refresh updates metrics and both refresh timestamps
- a failed closed-positions request marks the wallet STALE
- a failed refresh preserves previous metrics, scores and recommendation
- `last_successful_refresh` is never advanced by a failed request
- `last_refresh_attempt` is always advanced, even when the request failed
- a successful EMPTY response is genuine empty history, not STALE
- a successful empty response is not published as authoritative performance
  history (activity-only reconstruction is labelled as such)
- stale wallets cannot qualify as a fresh PASS and never enter the ranking pool
- fresh PASS wallets still rank exactly as before
- `pnl_source` never claims a failed request supplied fresh P&L data
- failures in non-material endpoints (/positions, /value) do not invalidate a
  refresh, while a failure in a material one (/activity) does

Every test uses a temporary wallets.json; the real data/wallets.json is never
read or written by the suite.
"""
from __future__ import annotations

import json

from config.settings import settings
from polymarket.api_client import ApiError
from wallet.monitor import save_wallet_config


W1 = "0x" + "11" * 20
W2 = "0x" + "22" * 20
W3 = "0x" + "33" * 20


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

def _closed_position(index: int, pnl: float = 20.0, cost: float = 100.0) -> dict:
    return {
        "conditionId": f"0xcond{index}",
        "asset": f"tok{index}",
        "title": f"Market {index}",
        "outcome": "Yes",
        "realizedPnl": pnl,
        "totalBought": cost,
        "avgPrice": 0.5,
        "timestamp": 1690000000 + index * 86400,   # spans > 30 days
        "category": "Crypto",
    }


def _activity_event(index: int) -> dict:
    return {
        "side": "BUY" if index % 2 == 0 else "SELL",
        "condition_id": f"0xcond{index}",
        "asset": f"tok{index}",
        "timestamp": f"2026-01-{(index % 25) + 1:02d}T{(index % 24):02d}:00:00+00:00",
        "tx_hash": f"0xtx{index}",
        "usdc_size": 100.0,
        "size": 100.0,
    }


#: 45 authoritative closed positions -> passes MIN_POSITIONS with a clean ROI.
GOOD_CLOSED = [_closed_position(i) for i in range(45)]
#: Matching activity feed (SELLs present, so positions are TRADING_EXIT).
GOOD_EVENTS = [_activity_event(i) for i in range(50)]

#: Buy-then-sell round trips in the SAME market: enough for
#: build_position_records()'s activity-reconstruction fallback to produce
#: closed positions when /closed-positions returns an empty history.
RECON_EVENTS: list[dict] = []
for _i in range(50):
    RECON_EVENTS.append({
        "side": "BUY",
        "condition_id": f"0xrecon{_i}",
        "asset": f"recontok{_i}",
        "timestamp": f"2026-01-{(_i % 25) + 1:02d}T10:00:00+00:00",
        "tx_hash": f"0xbuy{_i}",
        "price": 0.5,
        "size": 100.0,
        "usdc_size": 50.0,
    })
    RECON_EVENTS.append({
        "side": "SELL",
        "condition_id": f"0xrecon{_i}",
        "asset": f"recontok{_i}",
        "timestamp": f"2026-02-{(_i % 25) + 1:02d}T10:00:00+00:00",
        "tx_hash": f"0xsell{_i}",
        "price": 0.8,
        "size": 100.0,
        "usdc_size": 80.0,
    })


class FakeClient:
    """Minimal read-only stand-in for PolymarketApiClient."""

    def __init__(
        self,
        *,
        closed_by_wallet: dict[str, list[dict]] | None = None,
        events_by_wallet: dict[str, list[dict]] | None = None,
        closed_error: set[str] | None = None,
        activity_error: set[str] | None = None,
        positions_error: bool = False,
        value_error: bool = False,
    ):
        self.closed_by_wallet = closed_by_wallet or {}
        self.events_by_wallet = events_by_wallet or {}
        self.closed_error = set(closed_error or ())
        self.activity_error = set(activity_error or ())
        self.positions_error = positions_error
        self.value_error = value_error
        self.calls: list[str] = []

    def get_wallet_closed_positions_paginated(self, wallet, max_pages=5, raise_on_error=False, **_kw):
        self.calls.append(f"closed:{wallet}")
        if wallet in self.closed_error:
            if raise_on_error:
                raise ApiError("closed-positions API down")
            return []
        return list(self.closed_by_wallet.get(wallet, []))

    def get_wallet_positions(self, wallet):
        self.calls.append(f"positions:{wallet}")
        if self.positions_error:
            raise ApiError("positions API down")
        return []

    def get_wallet_value(self, wallet):
        self.calls.append(f"value:{wallet}")
        if self.value_error:
            raise ApiError("value API down")
        return 1000.0


def _fake_fetch_events(client, wallet, max_pages=5, page_size=100, raise_on_error=False):
    if wallet in client.activity_error:
        if raise_on_error:
            raise ApiError("activity API down")
        return []
    return list(client.events_by_wallet.get(wallet, []))


def _scan(monkeypatch, wallets, client, rank_recorder=None):
    """Run cmd_scan_wallets against a fake client and the patched wallets file."""
    import main

    monkeypatch.setattr(main, "PolymarketApiClient", lambda *a, **kw: client)
    monkeypatch.setattr(main, "scan_top_wallets", lambda c, limit=None, **kw: list(wallets))
    monkeypatch.setattr(main, "fetch_wallet_events_paginated", _fake_fetch_events)

    if rank_recorder is not None:
        real_rank_wallets = main.rank_wallets

        def _spy(stats, freq):
            rank_recorder.append([s.wallet for s in stats])
            return real_rank_wallets(stats, freq)

        monkeypatch.setattr(main, "rank_wallets", _spy)

    main.cmd_scan_wallets(None)


def _previous_record(wallet: str, **overrides) -> dict:
    """A stored record as produced by an earlier successful scan."""
    record = {
        "wallet": wallet,
        "tracked": True,
        "last_score": 81.5,
        "recommendation": "RECOMMENDED",
        "hard_filter_status": "PASS",
        "hard_filter_reasons": ["Wallet meets all configured filter thresholds."],
        "hard_filter_insufficient_criteria": [],
        "data_quality": "SUFFICIENT",
        "data_quality_reason": "previous successful refresh",
        "pnl_source": "closed_positions_api",
        "win_rate_pct": 83.0,
        "roi_pct": 24.5,
        "drawdown_pct": 3.25,
        "realized_pnl": 12345.67,
        "num_positions": 1500,
        "avg_position_size": 12.5,
        "risk_score": 0.3,
        "consistency_score": 8.1,
        "frequency_classification": "ACTIVE",
        "frequency_median_gap_seconds": 441.0,
        "frequency_raw_fill_count": 3000,
        "frequency_logical_trades": 3000,
        "frequency_max_trades_60s": 5,
        "frequency_pct_gaps_le_10s": 4.3,
        "frequency_rule_5_trades_60s": False,
        "frequency_rule_30pct_gaps_le_10s": False,
        "total_trades": 3000,
        "buy_trades": 2828,
        "sell_trades": 172,
        "total_volume": 33647.92,
        "largest_trade": 119.64,
        "active_positions": 12,
        "portfolio_value": 9876.5,
        "last_activity_at": "2026-10-14T18:04:27+00:00",
        "last_refresh_attempt": "2026-10-14T18:05:00+00:00",
        "last_successful_refresh": "2026-10-14T18:05:00+00:00",
    }
    record.update(overrides)
    return record


def _stored(wallets_file) -> dict[str, dict]:
    return {r["wallet"]: r for r in json.loads(wallets_file.read_text())}


# ---------------------------------------------------------------------------
# Successful refresh
# ---------------------------------------------------------------------------

def test_successful_refresh_updates_metrics_and_timestamps(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W1], FakeClient(
        closed_by_wallet={W1: GOOD_CLOSED}, events_by_wallet={W1: GOOD_EVENTS},
    ))

    entry = _stored(wallets_file)[W1]
    assert entry["hard_filter_status"] == "PASS"
    assert entry["data_quality"] == "SUFFICIENT"
    assert entry["pnl_source"] == "closed_positions_api"
    assert entry["num_positions"] == len(GOOD_CLOSED)
    assert entry["win_rate_pct"] == 100.0          # all 45 positions profitable
    assert entry["last_score"] is not None
    assert entry["last_refresh_attempt"] == entry["last_successful_refresh"]
    assert entry["last_refresh_attempt"] is not None


def test_successful_refresh_updates_previous_record_in_place(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    save_wallet_config([_previous_record(W1)], path=wallets_file)
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W1], FakeClient(
        closed_by_wallet={W1: GOOD_CLOSED}, events_by_wallet={W1: GOOD_EVENTS},
    ))

    entry = _stored(wallets_file)[W1]
    assert entry["num_positions"] == len(GOOD_CLOSED)      # refreshed, not the old 1500
    assert entry["roi_pct"] != 24.5
    assert entry["last_refresh_attempt"] > "2026-10-14T18:05:00+00:00"
    assert entry["data_quality"] == "SUFFICIENT"


# ---------------------------------------------------------------------------
# Failed refresh
# ---------------------------------------------------------------------------

def test_failed_closed_positions_request_marks_data_stale(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    save_wallet_config([_previous_record(W1)], path=wallets_file)
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W1], FakeClient(closed_error={W1}))

    entry = _stored(wallets_file)[W1]
    assert entry["data_quality"] == "STALE"
    assert entry["hard_filter_status"] == "STALE"
    assert "closed-positions request failed" in entry["data_quality_reason"]
    assert entry["hard_filter_reasons"] == [entry["data_quality_reason"]]


def test_failed_refresh_preserves_previous_performance_metrics(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    previous = _previous_record(W1)
    save_wallet_config([previous], path=wallets_file)
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W1], FakeClient(closed_error={W1}))

    entry = _stored(wallets_file)[W1]
    for field in (
        "last_score", "recommendation", "win_rate_pct", "roi_pct", "drawdown_pct",
        "realized_pnl", "num_positions", "avg_position_size", "risk_score",
        "consistency_score", "frequency_classification", "frequency_median_gap_seconds",
        "frequency_raw_fill_count", "frequency_logical_trades", "total_trades",
        "buy_trades", "sell_trades", "total_volume", "largest_trade",
        "active_positions", "portfolio_value", "last_activity_at",
    ):
        assert entry[field] == previous[field], field


def test_failed_refresh_keeps_last_successful_refresh_but_advances_attempt(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    previous = _previous_record(W1)
    save_wallet_config([previous], path=wallets_file)
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W1], FakeClient(closed_error={W1}))

    entry = _stored(wallets_file)[W1]
    assert entry["last_successful_refresh"] == previous["last_successful_refresh"]
    assert entry["last_refresh_attempt"] > previous["last_refresh_attempt"]


def test_failed_refresh_preserves_tracking_selection_and_custom_fields(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    save_wallet_config(
        [_previous_record(W1, tracked=True, my_note="keep-me")], path=wallets_file
    )
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W1], FakeClient(closed_error={W1}))

    entry = _stored(wallets_file)[W1]
    assert entry["tracked"] is True
    assert entry["my_note"] == "keep-me"


def test_new_wallet_with_failed_refresh_is_safe_and_never_claims_fresh_pnl(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W3], FakeClient(closed_error={W3}))

    entry = _stored(wallets_file)[W3]
    assert entry["data_quality"] == "STALE"
    assert entry["hard_filter_status"] == "STALE"
    assert entry["pnl_source"] == "none"                 # never "closed_positions_api"
    assert entry["last_successful_refresh"] is None      # there never was one
    assert entry["last_refresh_attempt"] is not None
    assert entry["num_positions"] is None
    assert entry["roi_pct"] is None
    assert entry["win_rate_pct"] is None
    assert entry["realized_pnl"] is None
    assert entry["last_score"] is None
    assert entry["recommendation"] == "DATA_STALE"
    assert entry["tracked"] is False


def test_retained_pnl_source_on_failure_describes_previous_data_only(tmp_path, monkeypatch):
    """A failed request must not be recorded as a successful data source."""
    wallets_file = tmp_path / "wallets.json"
    save_wallet_config([_previous_record(W1, pnl_source="closed_positions_api")], path=wallets_file)
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W1], FakeClient(closed_error={W1}))

    entry = _stored(wallets_file)[W1]
    assert entry["data_quality"] == "STALE"
    # The metrics kept are the previously CLOSED-POSITIONS-API ones; the record
    # says so explicitly and marks itself stale, so nothing claims the failed
    # request delivered fresh data.
    assert entry["pnl_source"] == "closed_positions_api"
    assert entry["last_successful_refresh"] != entry["last_refresh_attempt"]
    assert entry["last_successful_refresh"] == "2026-10-14T18:05:00+00:00"


def test_failed_activity_request_also_marks_stale(tmp_path, monkeypatch):
    """The activity feed is material (TRADING_EXIT detection drives the filter)."""
    wallets_file = tmp_path / "wallets.json"
    previous = _previous_record(W2)
    save_wallet_config([previous], path=wallets_file)
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W2], FakeClient(
        closed_by_wallet={W2: GOOD_CLOSED}, activity_error={W2}, events_by_wallet={W2: GOOD_EVENTS},
    ))

    entry = _stored(wallets_file)[W2]
    assert entry["data_quality"] == "STALE"
    assert entry["num_positions"] == previous["num_positions"]
    assert "activity request failed" in entry["data_quality_reason"]


def test_non_material_endpoint_failures_do_not_invalidate_a_refresh(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W1], FakeClient(
        closed_by_wallet={W1: GOOD_CLOSED}, events_by_wallet={W1: GOOD_EVENTS},
        positions_error=True, value_error=True,
    ))

    entry = _stored(wallets_file)[W1]
    assert entry["data_quality"] == "SUFFICIENT"          # not stale
    assert entry["hard_filter_status"] == "PASS"
    assert entry["active_positions"] is None
    assert entry["portfolio_value"] is None
    assert "/positions request failed" in entry["data_quality_reason"]


# ---------------------------------------------------------------------------
# Successful but empty history
# ---------------------------------------------------------------------------

def test_successful_empty_response_is_not_stale(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W1], FakeClient(
        closed_by_wallet={W1: []}, events_by_wallet={W1: []},
    ))

    entry = _stored(wallets_file)[W1]
    assert entry["data_quality"] != "STALE"
    assert entry["hard_filter_status"] != "STALE"
    assert entry["data_quality"] == "INSUFFICIENT"
    assert entry["pnl_source"] == "none"                 # no P&L was retrieved
    assert entry["roi_pct"] is None
    assert entry["win_rate_pct"] is None
    assert entry.get("realized_pnl") is None
    assert entry.get("num_positions") is None
    assert entry["last_successful_refresh"] is not None  # the request DID succeed
    assert entry["last_refresh_attempt"] == entry["last_successful_refresh"]


def test_successful_empty_history_is_labelled_as_reconstruction_not_authoritative(tmp_path, monkeypatch):
    """Empty closed-positions + activity fills -> ACTIVITY_ONLY, never STALE."""
    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W1], FakeClient(
        closed_by_wallet={W1: []}, events_by_wallet={W1: RECON_EVENTS},
    ))

    entry = _stored(wallets_file)[W1]
    assert entry["data_quality"] == "ACTIVITY_ONLY"
    assert entry["pnl_source"] == "activity_reconstruction"
    assert entry["data_quality"] != "STALE"
    assert entry["data_quality_reason"].startswith("Closed-positions API returned an empty history")


# ---------------------------------------------------------------------------
# Ranking pool
# ---------------------------------------------------------------------------

def test_stale_wallet_is_excluded_from_the_fresh_ranking_pool(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    save_wallet_config([_previous_record(W2)], path=wallets_file)
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    ranked_pools: list[list[str]] = []
    _scan(
        monkeypatch,
        [W1, W2],
        FakeClient(
            closed_by_wallet={W1: GOOD_CLOSED}, events_by_wallet={W1: GOOD_EVENTS},
            closed_error={W2},
        ),
        rank_recorder=ranked_pools,
    )

    assert ranked_pools, "rank_wallets should have been called for the fresh PASS wallet"
    assert W2 not in ranked_pools[0]          # stale wallet never ranked
    assert W1 in ranked_pools[0]              # fresh PASS wallet ranked normally

    stored = _stored(wallets_file)
    assert stored[W2]["last_score"] == 81.5   # previous score kept, not recomputed
    assert stored[W1]["hard_filter_status"] == "PASS"


def test_fresh_pass_wallets_still_rank_normally(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W1, W2], FakeClient(
        closed_by_wallet={W1: GOOD_CLOSED, W2: GOOD_CLOSED},
        events_by_wallet={W1: GOOD_EVENTS, W2: GOOD_EVENTS},
    ))

    stored = _stored(wallets_file)
    assert stored[W1]["hard_filter_status"] == "PASS"
    assert stored[W2]["hard_filter_status"] == "PASS"
    assert stored[W1]["last_score"] is not None
    assert stored[W1]["recommendation"] in ("RECOMMENDED", "WATCH", "REJECT")
    assert stored[W1]["last_refresh_attempt"] == stored[W1]["last_successful_refresh"]


# ---------------------------------------------------------------------------
# Read-only dashboard labelling
# ---------------------------------------------------------------------------

def test_dashboard_labels_stale_wallets_as_stale(tmp_path, monkeypatch):
    """A STALE wallet must not be displayed as merely "Insufficient data"."""
    from dashboard.app import create_app

    monkeypatch.setattr(settings, "WALLETS_FILE", tmp_path / "wallets.json")
    monkeypatch.setattr(settings, "VERIFICATION_FILE", tmp_path / "verification.json")
    monkeypatch.setattr(settings, "SEEN_EVENTS_FILE", tmp_path / "seen_events.json")
    monkeypatch.setattr(settings, "WALLET_ACTIVITY_LOG_FILE", tmp_path / "wallet_activity_log.json")

    save_wallet_config(
        [_previous_record(W1, data_quality="STALE", hard_filter_status="STALE")],
        path=tmp_path / "wallets.json",
    )

    app = create_app()
    app.testing = True
    html = app.test_client().get("/").get_data(as_text=True)

    assert "Stale" in html
    assert "Insufficient data" not in html
