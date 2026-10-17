"""
D2/D3 regression tests: PROVISIONAL recommendations and indeterminate
criteria for PARTIAL (page-capped) history.

Policy under test
-----------------
* COMPLETE history: filtering, reasons, scoring and recommendations are
  byte-for-byte identical to the pre-D2 behaviour.
* PARTIAL history with no failing criterion: still PASS, still scored, but
  the recommendation is ``PROVISIONAL`` -- never ``RECOMMENDED``.
* PARTIAL history where the only failures are criteria the truncated window
  can distort (ROI, positions, active days, consistency, concentration):
  ``INSUFFICIENT_DATA``, and those criteria are named as indeterminate.
* PARTIAL history with an independent failure (drawdown, politics,
  resolution-only): still ``FAIL`` for that independent reason; the
  history-dependent criteria are never presented as the decisive reason.
* No threshold, scoring formula or ranking rule is changed here; the tests
  compare against the same constants ``config/settings.py`` defines.

The persisted record, the dashboard and the CLI all have to carry the same
explanation, so the last block runs a real ``cmd_scan_wallets`` against a
page-returning client and inspects the saved JSON.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import main
from analytics.models import WalletStatistics
from config.settings import settings
from filters.wallet_filter import filter_wallet
from monitor.status import print_wallet_ranking
from polymarket.api_client import PolymarketApiClient
from wallet.ranking import WalletScoreCard


WALLET = "0x" + "c1" * 20
BASE_TS = 1_700_000_000
DAYS = 45


# ---------------------------------------------------------------------------
# Filter-level fixtures
# ---------------------------------------------------------------------------
def _stats(**overrides) -> WalletStatistics:
    """A wallet that passes every threshold on complete history."""
    values = dict(
        wallet=WALLET,
        positions_counted=45,
        roi_fee_exclusive_pct=20.0,
        win_rate_pct=100.0,
        active_days=45,
        drawdown_cost_pct=5.0,
        top1_profit_share_pct=20.0,
        consistency_mean_roi=0.20,
        consistency_stdev_roi=0.04,      # consistency = 5.0 >= 4.4
        is_resolution_only=False,
        category_mix={"crypto": 40, "sports": 5},
        is_complete_history=True,
        retrieval_status="SUFFICIENT",
    )
    values.update(overrides)
    return WalletStatistics(**values)


def _partial(**overrides) -> WalletStatistics:
    return _stats(is_complete_history=False, retrieval_status="PARTIAL", **overrides)


# ---------------------------------------------------------------------------
# 1. COMPLETE history is unchanged
# ---------------------------------------------------------------------------
class TestCompleteHistoryUnchanged:
    def test_pass_reasons_and_fields_are_exactly_the_old_ones(self):
        result = filter_wallet(_stats())

        assert result.status == "PASS"
        assert result.passed is True
        assert result.reasons == ["Wallet meets all configured filter thresholds."]
        assert result.insufficient_criteria == []
        assert result.failed_criteria == []
        assert result.indeterminate_criteria == []
        assert result.history_note is None

    def test_fail_reasons_are_unchanged_and_never_marked_indeterminate(self):
        result = filter_wallet(_stats(roi_fee_exclusive_pct=10.0, drawdown_cost_pct=30.0))

        assert result.status == "FAIL"
        assert result.passed is False
        assert result.reasons == [
            "ROI 10.0% below minimum 15.0%",
            "Drawdown 30.0% above maximum 20.0%",
        ]
        assert result.failed_criteria == ["roi", "drawdown"]
        assert result.indeterminate_criteria == []
        assert result.history_note is None

    def test_insufficient_data_is_unchanged(self):
        result = filter_wallet(_stats(roi_fee_exclusive_pct=None))

        assert result.status == "INSUFFICIENT_DATA"
        assert result.insufficient_criteria == ["roi"]
        assert result.indeterminate_criteria == []
        assert result.history_note is None

    def test_thresholds_are_still_the_configured_ones(self):
        # Exactly on the minimum passes; one cent below does not.
        assert filter_wallet(_stats(roi_fee_exclusive_pct=settings.MIN_ROI)).status == "PASS"
        assert filter_wallet(_stats(roi_fee_exclusive_pct=settings.MIN_ROI - 0.01)).status == "FAIL"
        assert filter_wallet(_stats(active_days=settings.MIN_ACTIVE_DAYS)).status == "PASS"
        assert filter_wallet(_stats(active_days=settings.MIN_ACTIVE_DAYS - 1)).status == "FAIL"


# ---------------------------------------------------------------------------
# 2. PARTIAL history, no failures -> PASS (caller labels it PROVISIONAL)
# ---------------------------------------------------------------------------
class TestPartialHistoryPass:
    def test_pass_is_retained_with_an_explanation(self):
        result = filter_wallet(_partial())

        assert result.status == "PASS"
        assert result.passed is True
        assert result.reasons == ["Wallet meets all configured filter thresholds."]
        assert result.indeterminate_criteria == []
        assert "PARTIAL" in result.history_note
        assert "PROVISIONAL" in result.history_note
        assert "lower bound" in result.history_note


# ---------------------------------------------------------------------------
# 3. PARTIAL history, only history-dependent failures -> INSUFFICIENT_DATA
# ---------------------------------------------------------------------------
class TestPartialHistoryDependentFailures:
    @pytest.mark.parametrize(
        "overrides, criterion, needle",
        [
            ({"roi_fee_exclusive_pct": 10.0}, "roi", "ROI 10.0% below minimum"),
            ({"roi_fee_exclusive_pct": 240.0}, "roi", "above reject threshold"),
            ({"positions_counted": 12}, "positions", "Only 12 positions"),
            ({"active_days": 7}, "active_days", "Only 7 active days"),
            ({"consistency_mean_roi": 0.20, "consistency_stdev_roi": 0.80},
             "consistency", "Consistency 2.0 below minimum"),
            ({"top1_profit_share_pct": 85.0}, "concentration",
             "85.0% of profit concentrated in one trade"),
        ],
    )
    def test_dependent_failure_is_indeterminate_not_a_rejection(
        self, overrides, criterion, needle
    ):
        result = filter_wallet(_partial(**overrides))

        assert result.status == "INSUFFICIENT_DATA"
        assert result.passed is False
        assert criterion in result.insufficient_criteria
        assert criterion in result.indeterminate_criteria
        # The specific threshold is still named, and it says why it cannot decide.
        assert any(needle in reason for reason in result.reasons)
        assert any("indeterminate" in reason for reason in result.reasons)
        assert "PARTIAL" in result.history_note

    def test_no_failure_is_reported_as_a_rejection(self):
        result = filter_wallet(_partial(active_days=7))

        assert result.failed_criteria == []
        assert all("indeterminate" in reason for reason in result.reasons)


# ---------------------------------------------------------------------------
# 4. PARTIAL history, independent failures -> FAIL
# ---------------------------------------------------------------------------
class TestPartialHistoryIndependentFailures:
    def test_drawdown_failure_remains_a_fail(self):
        result = filter_wallet(_partial(drawdown_cost_pct=30.0))

        assert result.status == "FAIL"
        assert result.reasons == ["Drawdown 30.0% above maximum 20.0%"]
        assert result.failed_criteria == ["drawdown"]
        assert result.indeterminate_criteria == []
        assert "cannot explain" in result.history_note

    def test_politics_failure_remains_a_fail(self):
        result = filter_wallet(
            _partial(category_mix={"politics": 90, "sports": 10})
        )

        assert result.status == "FAIL"
        assert any("politics" in reason for reason in result.reasons)
        assert result.indeterminate_criteria == []

    def test_mixed_failure_rejects_only_on_the_independent_reason(self):
        result = filter_wallet(_partial(active_days=7, drawdown_cost_pct=30.0))

        assert result.status == "FAIL"
        assert result.reasons == ["Drawdown 30.0% above maximum 20.0%"]
        assert result.failed_criteria == ["drawdown"]
        assert result.indeterminate_criteria == ["active_days"]
        # The reason list never presents the truncated-window metric as the cause.
        assert all("active days" not in reason for reason in result.reasons)
        assert "indeterminate" in result.history_note

    def test_resolution_only_with_complete_exit_history_fails(self):
        result = filter_wallet(_partial(is_resolution_only=True))

        assert result.status == "FAIL"
        assert any("resolution-only" in reason for reason in result.reasons)


# ---------------------------------------------------------------------------
# 5. P2 interaction: an unknown exit history cannot become a PASS
# ---------------------------------------------------------------------------
class TestUnknownExitHistory:
    def test_unknown_resolution_flag_stays_insufficient(self):
        result = filter_wallet(_partial(is_resolution_only=None))

        assert result.status == "INSUFFICIENT_DATA"
        assert "is_resolution_only" in result.insufficient_criteria
        assert result.indeterminate_criteria == []
        assert "PARTIAL" in result.history_note


# ---------------------------------------------------------------------------
# 6. End-to-end persistence through a real scan
# ---------------------------------------------------------------------------
def _closed(i: int, day: int) -> dict:
    return {
        "proxyWallet": WALLET,
        "conditionId": f"0xcond{i}",
        "asset": f"tok{i}",
        "avgPrice": 0.5,
        "totalBought": 100.0,
        "realizedPnl": 8.0 if i % 2 == 0 else 12.0,  # ROI 16%/24%, mean 20%
        "curPrice": 0.0,
        "slug": f"market-{i}",
        "title": f"Market {i}",
        "outcome": "Yes",
        "timestamp": BASE_TS + day * 86_400 + (i // DAYS) * 60,
    }


def _event(i: int, side: str) -> dict:
    return {
        "proxyWallet": WALLET,
        "side": side,
        "type": "TRADE",
        "conditionId": f"0xcond{i}",
        "asset": f"tok{i}",
        "slug": f"market-{i}",
        "transactionHash": f"0xtx{i}{side}",
        "timestamp": BASE_TS + (i % DAYS) * 86_400,
        "price": 0.5,
        "size": 100.0,
        "usdcSize": 50.0,
        "title": f"Market {i}",
        "outcome": "Yes",
    }


def _client_with_pages(activity_pages, closed_pages) -> PolymarketApiClient:
    client = PolymarketApiClient.__new__(PolymarketApiClient)
    client.get_wallet_activity = (
        lambda wallet, limit=100, offset=0: (
            activity_pages[offset // limit] if offset // limit < len(activity_pages) else []
        )
    )
    client.get_wallet_closed_positions = (
        lambda wallet, limit=50, offset=0, sort_by="TIMESTAMP", sort_direction="DESC": (
            closed_pages[offset // limit] if offset // limit < len(closed_pages) else []
        )
    )
    client.get_wallet_positions = lambda wallet: []
    client.get_wallet_value = lambda wallet: 1000.0
    return client


def _run_scan(monkeypatch, tmp_path, closed_pages, activity_pages) -> dict:
    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    client = _client_with_pages(activity_pages, closed_pages)
    with patch("main.scan_top_wallets", return_value=[WALLET]), \
         patch("main.PolymarketApiClient", return_value=client):
        main.cmd_scan_wallets(None)

    records = json.loads(wallets_file.read_text(encoding="utf-8"))
    return {record["wallet"]: record for record in records}[WALLET]


def _capped_closed(active_days: int = DAYS) -> list[list[dict]]:
    """MAX_HISTORY_PAGES full pages -> provably truncated history."""
    total = settings.MAX_HISTORY_PAGES * 50
    rows = [_closed(i, day=i % active_days) for i in range(total)]
    return [rows[p * 50:(p + 1) * 50] for p in range(settings.MAX_HISTORY_PAGES)]


def _complete_rows(count: int = 45) -> list[list[dict]]:
    """One short page with a trading exit -> provably complete history."""
    return [[_closed(i, day=i % DAYS) for i in range(count)]]


class TestPersistedProvisional:
    def test_partial_pass_is_saved_as_provisional(self, monkeypatch, tmp_path):
        closed_pages = _capped_closed()
        activity_pages = [[_event(i, "BUY") for i in range(5)]
                          + [_event(i, "SELL") for i in range(5)]]

        entry = _run_scan(monkeypatch, tmp_path, closed_pages, activity_pages)

        assert entry["hard_filter_status"] == "PASS"
        assert entry["recommendation"] == "PROVISIONAL"
        assert entry["recommendation"] != "RECOMMENDED"
        assert entry["last_score"] is not None
        assert entry["history_truncated"] is True
        assert entry["data_quality"] == "PARTIAL"
        assert entry["hard_filter_indeterminate_criteria"] == []
        assert "PARTIAL" in entry["history_note"]
        assert "PROVISIONAL" in entry["history_note"]

    def test_complete_pass_is_saved_as_recommended(self, monkeypatch, tmp_path):
        activity_pages = [[_event(i, "BUY") for i in range(5)]
                          + [_event(i, "SELL") for i in range(5)]]

        entry = _run_scan(monkeypatch, tmp_path, _complete_rows(), activity_pages)

        assert entry["hard_filter_status"] == "PASS"
        assert entry["recommendation"] == "RECOMMENDED"
        assert entry["history_note"] is None
        assert entry["hard_filter_indeterminate_criteria"] == []
        assert entry["history_truncated"] is False

    def test_partial_active_day_rejection_is_saved_as_indeterminate(
        self, monkeypatch, tmp_path
    ):
        closed_pages = _capped_closed(active_days=10)
        activity_pages = [[_event(i, "BUY") for i in range(5)]
                          + [_event(i, "SELL") for i in range(5)]]

        entry = _run_scan(monkeypatch, tmp_path, closed_pages, activity_pages)

        assert entry["hard_filter_status"] == "INSUFFICIENT_DATA"
        assert entry["recommendation"] == "HARD_FILTER_INSUFFICIENT_DATA"
        assert entry["last_score"] is None
        assert "active_days" in entry["hard_filter_indeterminate_criteria"]
        assert "active_days" in entry["hard_filter_insufficient_criteria"]
        assert any("indeterminate" in reason for reason in entry["hard_filter_reasons"])
        assert "PARTIAL" in entry["history_note"]

    def test_stale_refresh_keeps_the_provisional_explanation(self):
        previous = {
            "wallet": WALLET,
            "recommendation": "PROVISIONAL",
            "history_note": "History is PARTIAL ...",
            "hard_filter_indeterminate_criteria": ["active_days"],
            "history_truncated": True,
            "last_score": 81.2,
        }

        update = main._stale_wallet_update(WALLET, previous, "2026-10-17T00:00:00+00:00", "boom")

        assert update["recommendation"] == "PROVISIONAL"
        assert update["history_note"] == "History is PARTIAL ..."
        assert update["hard_filter_indeterminate_criteria"] == ["active_days"]
        assert update["history_truncated"] is True
        assert update["last_score"] == 81.2
        assert update["hard_filter_status"] == "STALE"


# ---------------------------------------------------------------------------
# 7. Dashboard + CLI surface the same words
# ---------------------------------------------------------------------------
class TestPresentation:
    def _write(self, tmp_path, monkeypatch, records):
        from analytics import dashboard as dashboard_module
        wallets_file = tmp_path / "wallets.json"
        wallets_file.write_text(json.dumps(records), encoding="utf-8")
        monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)
        monkeypatch.setattr(settings, "VERIFICATION_FILE", tmp_path / "v.json")
        monkeypatch.setattr(settings, "SEEN_EVENTS_FILE", tmp_path / "seen.json")
        monkeypatch.setattr(
            settings, "WALLET_ACTIVITY_LOG_FILE", tmp_path / "activity.json"
        )
        return dashboard_module

    def test_dashboard_row_exposes_note_and_indeterminate_criteria(
        self, tmp_path, monkeypatch
    ):
        dashboard_module = self._write(
            tmp_path, monkeypatch,
            [{
                "wallet": WALLET,
                "last_score": 81.2,
                "recommendation": "PROVISIONAL",
                "hard_filter_status": "PASS",
                "history_truncated": False,
                "history_note": "History is PARTIAL: older rows were not fetched.",
                "hard_filter_indeterminate_criteria": [],
            }],
        )

        data = dashboard_module.build_dashboard_data()
        row = data.wallet_table[0]

        assert row["recommendation"] == "PROVISIONAL"
        assert "PARTIAL" in row["history_note"]
        assert row["indeterminate_criteria"] == []
        assert data.discovery_status["provisional_wallets"] == 1

    def test_dashboard_renders_a_distinct_provisional_badge(
        self, tmp_path, monkeypatch
    ):
        self._write(
            tmp_path, monkeypatch,
            [{
                "wallet": WALLET,
                "last_score": 81.2,
                "recommendation": "PROVISIONAL",
                "hard_filter_status": "PASS",
                "history_truncated": True,
                "history_note": "History is PARTIAL.",
            }],
        )
        from dashboard.app import create_app
        app = create_app()
        app.testing = True
        html = app.test_client().get("/").get_data(as_text=True)

        assert "badge-provisional" in html
        assert ">PROVISIONAL<" in html
        # Never rendered with the RECOMMENDED badge label.
        assert ">RECOMMENDED<" not in html

    def test_console_ranking_labels_partial_pass_provisional(self, capsys):
        card = WalletScoreCard(
            wallet=WALLET,
            roi_pct=20.0,
            win_rate_pct=100.0,
            drawdown_pct=5.0,
            num_positions=45,
            avg_position_size=50.0,
            recent_profitability=True,
            consistency_score=5.0,
            category_mix={"crypto": 45},
            risk_score=3.0,
            final_score=81.2,
            recommendation="RECOMMENDED",
        )

        print_wallet_ranking([card], {WALLET})
        out = capsys.readouterr().out

        row = next(line for line in out.splitlines() if "81.2" in line)
        assert "PROVISIONAL" in row
        assert "RECOMMENDED" not in row
        assert "fully-vetted RECOMMENDED" in out
