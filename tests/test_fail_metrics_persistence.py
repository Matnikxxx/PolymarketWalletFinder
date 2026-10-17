"""
Regression tests for P3: a FAIL/INSUFFICIENT_DATA record must carry the
metrics that produced the decision.

Defect: only PASS records persisted positions / consistency / drawdown /
trade counts. A rejected wallet kept ROI and win-rate and lost everything
else, so a rejection could not be audited or shown per-candidate.

Scope guarantees pinned down here:
* the persisted values are the engine's own numbers (no recalculation drift);
* a metric that could not be computed is stored as None, never as 0;
* existing records, tracking choices and merge/stale behaviour are unchanged;
* no filter decision, scoring formula, threshold or wallet status changes --
  the persisted status/reasons/score are compared against what
  ``filter_wallet`` and ``rank_wallets`` produce for the same statistics.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import main
from analytics.consistency import consistency_score_from_dispersion
from config.settings import settings
from filters.wallet_filter import filter_wallet
from polymarket.api_client import ApiError
from wallet.monitor import save_wallet_config
from wallet.ranking import rank_wallets


W_FAIL_ROI = "0x" + "a1" * 20
W_PASS = "0x" + "b2" * 20
W_THIN = "0x" + "c3" * 20
W_NO_TS = "0x" + "d4" * 20
W_EMPTY = "0x" + "e5" * 20

BASE_TS = 1_690_000_000


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _iso(day_offset: int) -> str:
    return datetime.fromtimestamp(
        BASE_TS + day_offset * 86_400, tz=timezone.utc
    ).isoformat()


def _closed(i: int, pnl: float = 5.0, cost: float = 100.0, with_ts: bool = True) -> dict:
    row = {
        "conditionId": f"0xcond{i}",
        "asset": f"tok{i}",
        "title": f"Market {i}",
        "outcome": "Yes",
        "realizedPnl": pnl,
        "totalBought": cost,
        "avgPrice": 0.5,
        "category": "Crypto",
    }
    if with_ts:
        row["timestamp"] = BASE_TS + i * 86_400
    return row


def _event(i: int, side: str) -> dict:
    return {
        "side": side,
        "condition_id": f"0xcond{i}",
        "asset": f"tok{i}",
        "timestamp": _iso(i),
        "tx_hash": f"0xtx{i}{side}",
        "usdc_size": 100.0,
        "size": 100.0,
    }


def _aligned_events(n: int) -> list[dict]:
    """n fills whose dates coincide with the closed positions' dates."""
    return [_event(i, "BUY" if i % 2 == 0 else "SELL") for i in range(n)]


class FakeClient:
    def __init__(self, closed, events, *, closed_error=False):
        self.closed = list(closed)
        self.events = list(events)
        self.closed_error = closed_error

    def get_wallet_closed_positions_paginated(self, wallet, max_pages=5, raise_on_error=False, **_kw):
        if self.closed_error:
            if raise_on_error:
                raise ApiError("closed-positions API down")
            return []
        return list(self.closed)

    def get_wallet_positions(self, wallet):
        return []

    def get_wallet_value(self, wallet):
        return 1000.0


def _run_scan(monkeypatch, tmp_path, wallet, closed, events, *, closed_error=False,
              stats_spy=None, freq_spy=None):
    """Run cmd_scan_wallets for one wallet against a temporary wallets file."""
    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    client = FakeClient(closed, events, closed_error=closed_error)
    monkeypatch.setattr(main, "PolymarketApiClient", lambda *a, **kw: client)
    monkeypatch.setattr(main, "scan_top_wallets", lambda c, limit=None, **kw: [wallet])
    monkeypatch.setattr(
        main, "fetch_wallet_events_paginated",
        lambda c, w, max_pages=5, page_size=100, raise_on_error=False: list(client.events),
    )

    if stats_spy is not None:
        real = main.compute_wallet_statistics

        def _stats(w, positions, **kwargs):
            stats = real(w, positions, **kwargs)
            stats_spy[w] = stats
            return stats

        monkeypatch.setattr(main, "compute_wallet_statistics", _stats)

    if freq_spy is not None:
        real_freq = main.compute_frequency_profile

        def _freq(w, events_, **kwargs):
            profile = real_freq(w, events_, **kwargs)
            freq_spy[w] = profile
            return profile

        monkeypatch.setattr(main, "compute_frequency_profile", _freq)

    main.cmd_scan_wallets(None)
    return wallets_file


def _stored(path) -> dict[str, dict]:
    with open(path) as fh:
        records = json.load(fh)
    return {r["wallet"]: r for r in records}


# ---------------------------------------------------------------------------
# 1. FAIL records retain the available diagnostic metrics
# ---------------------------------------------------------------------------
class TestFailRecordRetainsMetrics:
    def test_roi_rejection_keeps_positions_consistency_drawdown_and_trades(self, monkeypatch, tmp_path):
        # 45 positions, ROI 10% (< MIN_ROI) -> the only rejection reason is ROI.
        path = _run_scan(
            monkeypatch, tmp_path, W_FAIL_ROI,
            closed=[_closed(i) for i in range(45)],
            events=_aligned_events(45),
        )
        entry = _stored(path)[W_FAIL_ROI]

        assert entry["hard_filter_status"] == "FAIL"
        assert any("ROI 10.0% below minimum" in r for r in entry["hard_filter_reasons"])
        # The metrics that explain the decision are now persisted.
        assert entry["num_positions"] == 45
        assert entry["active_days"] == 45
        assert entry["total_trades"] == 45
        assert entry["buy_trades"] == 23
        assert entry["sell_trades"] == 22
        assert entry["consistency_score"] == pytest.approx(10.0)
        assert entry["drawdown_pct"] == pytest.approx(0.0)
        assert entry["realized_pnl"] is not None
        assert entry["avg_position_size"] is not None
        assert entry["first_activity_at"] is not None
        assert entry["last_activity_at"] is not None
        # Rejected wallets still never receive a score.
        assert entry["last_score"] is None

    def test_persisted_metrics_are_the_engine_values(self, monkeypatch, tmp_path):
        captured: dict = {}
        path = _run_scan(
            monkeypatch, tmp_path, W_FAIL_ROI,
            closed=[_closed(i) for i in range(45)],
            events=_aligned_events(45),
            stats_spy=captured,
        )
        entry = _stored(path)[W_FAIL_ROI]
        stats = captured[W_FAIL_ROI]

        assert entry["num_positions"] == stats.positions_counted
        assert entry["active_days"] == stats.active_days
        assert entry["drawdown_pct"] == stats.drawdown_cost_pct
        assert entry["realized_pnl"] == stats.total_settled_net_result
        assert entry["avg_position_size"] == stats.mean_position_cost
        assert entry["consistency_score"] == consistency_score_from_dispersion(
            stats.consistency_mean_roi, stats.consistency_stdev_roi
        )


# ---------------------------------------------------------------------------
# 2. Missing metrics stay explicitly unknown -- never a misleading zero
# ---------------------------------------------------------------------------
class TestMissingMetricsAreUnknown:
    def test_thin_history_has_unknown_consistency(self, monkeypatch, tmp_path):
        # 2 eligible positions -> dispersion undefined (n < 3).
        path = _run_scan(
            monkeypatch, tmp_path, W_THIN,
            closed=[_closed(i) for i in range(2)],
            events=_aligned_events(2),
        )
        entry = _stored(path)[W_THIN]

        assert entry["num_positions"] == 2
        assert entry["consistency_score"] is None
        assert entry["active_days"] == 2

    def test_undatable_history_has_unknown_drawdown(self, monkeypatch, tmp_path):
        # 45 settled positions with no timestamps -> drawdown cannot be ordered.
        path = _run_scan(
            monkeypatch, tmp_path, W_NO_TS,
            closed=[_closed(i, with_ts=False) for i in range(45)],
            events=[],
        )
        entry = _stored(path)[W_NO_TS]

        assert entry["num_positions"] == 45
        assert entry["drawdown_pct"] is None
        # active_days is a genuine observation (no dated positions), not a
        # missing value: 0 is what the "Only 0 active days" reason reports.
        assert entry["active_days"] == 0

    def test_empty_history_publishes_no_performance_numbers(self, monkeypatch, tmp_path):
        path = _run_scan(
            monkeypatch, tmp_path, W_EMPTY, closed=[], events=[],
        )
        entry = _stored(path)[W_EMPTY]

        assert entry["hard_filter_status"] == "FAIL"
        assert entry["num_positions"] is None
        assert entry["consistency_score"] is None
        assert entry["drawdown_pct"] is None
        assert entry["realized_pnl"] is None
        assert entry["avg_position_size"] is None
        assert entry["total_trades"] is None
        assert entry["buy_trades"] is None
        assert entry["sell_trades"] is None
        assert entry["roi_pct"] is None
        assert entry["win_rate_pct"] is None


# ---------------------------------------------------------------------------
# 3. No decision, score or status changes
# ---------------------------------------------------------------------------
class TestNoDecisionChanges:
    def test_fail_status_and_reasons_equal_filter_output(self, monkeypatch, tmp_path):
        captured: dict = {}
        path = _run_scan(
            monkeypatch, tmp_path, W_FAIL_ROI,
            closed=[_closed(i) for i in range(45)],
            events=_aligned_events(45),
            stats_spy=captured,
        )
        entry = _stored(path)[W_FAIL_ROI]
        expected = filter_wallet(captured[W_FAIL_ROI])

        assert entry["hard_filter_status"] == expected.status == "FAIL"
        assert entry["hard_filter_reasons"] == expected.reasons
        assert entry["hard_filter_insufficient_criteria"] == expected.insufficient_criteria
        assert entry["recommendation"] == "HARD_FILTER_REJECT"

    def test_pass_wallet_score_and_recommendation_equal_ranking_output(self, monkeypatch, tmp_path):
        captured: dict = {}
        freq: dict = {}
        # ROI 40% -> inside [15, 80]; passes every criterion.
        path = _run_scan(
            monkeypatch, tmp_path, W_PASS,
            closed=[_closed(i, pnl=20.0) for i in range(45)],
            events=_aligned_events(45),
            stats_spy=captured,
            freq_spy=freq,
        )
        entry = _stored(path)[W_PASS]
        card = rank_wallets([captured[W_PASS]], {W_PASS: freq[W_PASS]})[0]

        assert entry["hard_filter_status"] == "PASS"
        assert entry["last_score"] == card.final_score
        assert entry["recommendation"] == card.recommendation
        assert entry["consistency_score"] == pytest.approx(card.consistency_score, abs=0.01)

    def test_thresholds_used_by_filter_are_unchanged(self):
        # Reporting must not have retuned the filter. These are the values
        # documented in .env.example / the diagnostic report.
        assert settings.MIN_ROI == 15.0
        assert settings.REJECT_ROI_ABOVE == 80.0
        assert settings.MIN_POSITIONS == 40
        assert settings.MIN_CONSISTENCY == 4.4
        assert settings.MAX_DRAWDOWN == 20.0
        assert settings.MIN_ACTIVE_DAYS == 30


# ---------------------------------------------------------------------------
# 4. Shared consistency helper preserves the previous calculation exactly
# ---------------------------------------------------------------------------
def _legacy_inline_consistency(mean_roi, stdev_roi):
    """The expression the helper replaced, copied verbatim."""
    if mean_roi is None or stdev_roi is None:
        return None
    if mean_roi != 0:
        cv = abs(stdev_roi / mean_roi)
        return max(0.0, min(10.0, 10.0 / (1.0 + cv)))
    return 0.0


class TestConsistencyHelperEquivalence:
    @pytest.mark.parametrize(
        "mean_roi,stdev_roi",
        [
            (0.25, 0.05),
            (0.1, 0.0),
            (0.05, 0.0),
            (0.1, 0.3),
            (-0.2, 0.1),
            (0.0, 0.5),
            (0.0, 0.0),
            (1e-9, 2.0),
            (0.5, 5.0),
            (2.0, 0.001),
            (-1.0, 4.0),
        ],
    )
    def test_matches_previous_inline_formula(self, mean_roi, stdev_roi):
        assert consistency_score_from_dispersion(mean_roi, stdev_roi) == _legacy_inline_consistency(
            mean_roi, stdev_roi
        )

    def test_missing_inputs_stay_none(self):
        assert consistency_score_from_dispersion(None, 0.1) is None
        assert consistency_score_from_dispersion(0.1, None) is None
        assert consistency_score_from_dispersion(None, None) is None

    def test_score_is_clamped_to_0_10(self):
        # A huge relative dispersion approaches (but is bounded by) 0.
        assert 0.0 <= consistency_score_from_dispersion(0.001, 1000.0) < 1e-4
        # Zero dispersion is a perfect score.
        assert consistency_score_from_dispersion(1.0, 0.0) == 10.0


# ---------------------------------------------------------------------------
# 5. Existing records, refresh and merge behaviour stay compatible
# ---------------------------------------------------------------------------
class TestRecordCompatibility:
    def test_rescan_updates_metrics_and_keeps_tracking_and_custom_fields(self, monkeypatch, tmp_path):
        wallets_file = tmp_path / "wallets.json"
        save_wallet_config(
            [{
                "wallet": W_FAIL_ROI,
                "tracked": True,
                "my_note": "keep-me",
                "active_days": 999,          # stale value from an older scan
                "num_positions": 999,
            }],
            path=wallets_file,
        )
        monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

        client = FakeClient([_closed(i) for i in range(45)], _aligned_events(45))
        monkeypatch.setattr(main, "PolymarketApiClient", lambda *a, **kw: client)
        monkeypatch.setattr(main, "scan_top_wallets", lambda c, limit=None, **kw: [W_FAIL_ROI])
        monkeypatch.setattr(
            main, "fetch_wallet_events_paginated",
            lambda c, w, max_pages=5, page_size=100, raise_on_error=False: list(client.events),
        )
        main.cmd_scan_wallets(None)

        entry = _stored(wallets_file)[W_FAIL_ROI]
        assert entry["tracked"] is True
        assert entry["my_note"] == "keep-me"
        assert entry["active_days"] == 45      # refreshed, not the stale 999
        assert entry["num_positions"] == 45

    def test_legacy_record_without_new_fields_merges_cleanly(self, monkeypatch, tmp_path):
        wallets_file = tmp_path / "wallets.json"
        save_wallet_config(
            [{"wallet": W_FAIL_ROI, "tracked": False, "recommendation": "HARD_FILTER_REJECT"}],
            path=wallets_file,
        )
        monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

        client = FakeClient([_closed(i) for i in range(45)], _aligned_events(45))
        monkeypatch.setattr(main, "PolymarketApiClient", lambda *a, **kw: client)
        monkeypatch.setattr(main, "scan_top_wallets", lambda c, limit=None, **kw: [W_FAIL_ROI])
        monkeypatch.setattr(
            main, "fetch_wallet_events_paginated",
            lambda c, w, max_pages=5, page_size=100, raise_on_error=False: list(client.events),
        )
        main.cmd_scan_wallets(None)

        entry = _stored(wallets_file)[W_FAIL_ROI]
        assert entry["tracked"] is False
        assert entry["num_positions"] == 45
        assert entry["active_days"] == 45
        assert entry["consistency_score"] is not None

    def test_failed_refresh_preserves_active_days_and_first_activity(self, monkeypatch, tmp_path):
        wallets_file = tmp_path / "wallets.json"
        save_wallet_config(
            [{
                "wallet": W_FAIL_ROI,
                "tracked": True,
                "active_days": 41,
                "first_activity_at": "2025-01-01T00:00:00+00:00",
                "num_positions": 77,
                "win_rate_pct": 61.0,
                "roi_pct": 22.0,
            }],
            path=wallets_file,
        )
        monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

        client = FakeClient([], [], closed_error=True)
        monkeypatch.setattr(main, "PolymarketApiClient", lambda *a, **kw: client)
        monkeypatch.setattr(main, "scan_top_wallets", lambda c, limit=None, **kw: [W_FAIL_ROI])
        monkeypatch.setattr(
            main, "fetch_wallet_events_paginated",
            lambda c, w, max_pages=5, page_size=100, raise_on_error=False: [],
        )
        main.cmd_scan_wallets(None)

        entry = _stored(wallets_file)[W_FAIL_ROI]
        assert entry["hard_filter_status"] == "STALE"
        assert entry["active_days"] == 41
        assert entry["first_activity_at"] == "2025-01-01T00:00:00+00:00"
        assert entry["num_positions"] == 77
        assert entry["tracked"] is True
