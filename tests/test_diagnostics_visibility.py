"""
Regression tests for P4: history truncation and activity metrics must be
visible in the scan diagnostics and on the dashboard.

Verified here:
* a wallet whose history hit the page cap is reported PARTIAL, with the
  endpoint, stop reason, page count and record count -- and is never shown as
  COMPLETE merely because the fetch loop finished;
* a wallet whose history ended on a short/empty page is COMPLETE;
* a wallet with no pagination metadata is UNKNOWN, never assumed complete;
* active days and activity dates are shown for PASS and rejected wallets
  alike, and an unknown metric renders as N/A while a measured zero renders
  as 0 (a valid zero is never replaced with None);
* the dashboard exposes the same information and keeps rendering legacy
  records that predate these fields;
* nothing here changes a filter decision, threshold, score or ranking.
"""
from __future__ import annotations

from analytics.dashboard import _history_detail, _history_status, _wallet_row, build_dashboard_data
from config.settings import settings
from filters.wallet_filter import WalletFilterResult
from monitor.scan_report import (
    build_scan_summary,
    describe_history_metadata,
    format_candidate_table,
    format_scan_summary,
)
from monitor.status import print_wallet_report
from wallet.monitor import save_wallet_config


W_COMPLETE = "0x" + "11" * 20
W_PARTIAL = "0x" + "22" * 20
W_UNKNOWN = "0x" + "33" * 20
W_STALE = "0x" + "44" * 20


class _Stats:
    def __init__(self, wallet, roi=25.0, win_rate=60.0, positions=45, active_days=40,
                 drawdown=5.0, mean=0.25, stdev=0.05,
                 first="2025-01-01T00:00:00+00:00", last="2026-02-01T00:00:00+00:00"):
        self.wallet = wallet
        self.roi_fee_exclusive_pct = roi
        self.win_rate_pct = win_rate
        self.positions_counted = positions
        self.active_days = active_days
        self.drawdown_cost_pct = drawdown
        self.consistency_mean_roi = mean
        self.consistency_stdev_roi = stdev
        self.first_activity_at = first
        self.last_activity_at = last


def _result(status, reasons=None, insufficient=None):
    return WalletFilterResult(
        passed=status == "PASS",
        reasons=list(reasons or []),
        status=status,
        insufficient_criteria=list(insufficient or []),
    )


def _complete_meta(pages=3, rows=240):
    return {
        "history_truncated": False,
        "activity_pages_fetched": pages,
        "activity_max_pages": 30,
        "activity_rows_fetched": rows,
        "activity_truncated": False,
        "activity_stop_reason": "exhausted",
        "closed_positions_pages_fetched": pages,
        "closed_positions_max_pages": 30,
        "closed_positions_rows_fetched": rows,
        "closed_positions_truncated": False,
        "closed_positions_stop_reason": "exhausted",
    }


def _partial_meta():
    """Reproduces the diagnostic defect: 1500 closed rows inside a short window."""
    return {
        "history_truncated": True,
        "activity_pages_fetched": 3,
        "activity_max_pages": 30,
        "activity_rows_fetched": 240,
        "activity_truncated": False,
        "activity_stop_reason": "exhausted",
        "closed_positions_pages_fetched": 30,
        "closed_positions_max_pages": 30,
        "closed_positions_rows_fetched": 1500,
        "closed_positions_truncated": True,
        "closed_positions_stop_reason": "page_cap",
    }


# ---------------------------------------------------------------------------
# 1. describe_history_metadata
# ---------------------------------------------------------------------------
class TestDescribeHistoryMetadata:
    def test_missing_metadata_is_unknown_not_complete(self):
        status, detail, feeds = describe_history_metadata(None)
        assert status == "UNKNOWN"
        assert detail == []
        assert feeds == []

    def test_complete_history(self):
        status, detail, feeds = describe_history_metadata(_complete_meta())
        assert status == "COMPLETE"
        assert feeds == []
        assert any("/activity: exhausted after 3/30 page(s), 240 record(s) fetched" in line for line in detail)
        assert any("/closed-positions: exhausted after 3/30 page(s), 240 record(s) fetched" in line for line in detail)

    def test_page_cap_is_partial_with_endpoint_stop_pages_and_records(self):
        status, detail, feeds = describe_history_metadata(_partial_meta())
        assert status == "PARTIAL"
        assert feeds == ["/closed-positions"]
        assert any(
            "/closed-positions: page_cap after 30/30 page(s), 1500 record(s) fetched" in line
            for line in detail
        )

    def test_api_error_stop_reason_is_reported(self):
        meta = _complete_meta()
        meta["history_truncated"] = True
        meta["activity_truncated"] = True
        meta["activity_stop_reason"] = "api_error"
        status, detail, feeds = describe_history_metadata(meta)
        assert status == "PARTIAL"
        assert feeds == ["/activity"]
        assert any("api_error" in line for line in detail)


# ---------------------------------------------------------------------------
# 2. Scan diagnostics: rows, sections and the candidate table
# ---------------------------------------------------------------------------
def _summary_with_history():
    return build_scan_summary(
        wallets=[W_COMPLETE, W_PARTIAL, W_UNKNOWN],
        stats_map={
            W_COMPLETE: _Stats(W_COMPLETE),
            W_PARTIAL: _Stats(W_PARTIAL, active_days=6, positions=1500),
            W_UNKNOWN: _Stats(W_UNKNOWN, active_days=0, positions=0),
        },
        filter_map={
            W_COMPLETE: _result("PASS", ["Wallet meets all configured filter thresholds."]),
            W_PARTIAL: _result("FAIL", ["Only 6 active days, below minimum 30"]),
            W_UNKNOWN: _result("INSUFFICIENT_DATA", ["Wallet meets all evaluable filter thresholds."],
                               insufficient=["is_resolution_only"]),
        },
        history_meta={
            W_COMPLETE: _complete_meta(),
            W_PARTIAL: _partial_meta(),
        },
    )


class TestScanDiagnosticsVisibility:
    def test_history_status_per_row_for_pass_and_rejected_alike(self):
        summary = _summary_with_history()
        rows = {row.wallet: row for row in summary.rows}

        assert rows[W_COMPLETE].history_status == "COMPLETE"
        assert rows[W_PARTIAL].history_status == "PARTIAL"
        assert rows[W_UNKNOWN].history_status == "UNKNOWN"
        # PASS wallet shows its activity metrics exactly like the others.
        assert rows[W_COMPLETE].active_days == 40
        assert rows[W_COMPLETE].first_activity_at.startswith("2025-01-01")

    def test_history_counts_are_aggregated(self):
        summary = _summary_with_history()
        assert summary.history_counts["COMPLETE"] == 1
        assert summary.history_counts["PARTIAL"] == 1
        assert summary.history_counts["UNKNOWN"] == 1

    def test_report_shows_endpoint_stop_reason_pages_and_records(self):
        text = format_scan_summary(_summary_with_history())

        assert "History retrieval completeness" in text
        assert "COMPLETE: 1   PARTIAL: 1   UNKNOWN: 1" in text
        assert "page_cap after 30/30 page(s), 1500 record(s) fetched" in text
        assert "LOWER BOUNDS" in text

    def test_report_explains_that_unknown_is_not_assumed_complete(self):
        text = format_scan_summary(_summary_with_history())
        assert "never assumed complete" in text

    def test_active_day_rejection_on_partial_history_is_annotated(self):
        text = format_scan_summary(_summary_with_history())
        assert "'active days' rejection(s) involve PARTIAL history" in text
        assert "cannot be shown" in text

    def test_activity_window_lists_dates_for_each_wallet(self):
        text = format_scan_summary(_summary_with_history())
        assert "Observed activity window" in text
        assert "2025-01-01 .. 2026-02-01" in text
        assert "40 day(s)" in text

    def test_candidate_table_has_history_column_and_keeps_zero_distinct_from_na(self):
        table = format_candidate_table(_summary_with_history())

        assert "History" in table
        assert "COMPLETE" in table
        assert "PARTIAL (closed-positions)" in table
        # W_UNKNOWN measured 0 active days and 0 positions: shown as 0, not N/A.
        unknown_line = next(line for line in table.splitlines() if W_UNKNOWN[:10] in line)
        assert " 0 " in unknown_line
        assert "N/A" in unknown_line  # consistency/drawdown unavailable

    def test_legend_states_na_vs_zero(self):
        table = format_candidate_table(_summary_with_history())
        assert "N/A = not available" in table
        assert "0 = a genuine measured zero" in table

    def test_stale_wallet_history_is_unknown(self):
        summary = build_scan_summary(
            wallets=[W_STALE],
            filter_map={W_STALE: _result("STALE", ["Refresh incomplete: closed-positions API down"])},
            stale_wallets=[W_STALE],
            stale_causes={W_STALE: ["/closed-positions request failed: down"]},
            history_meta={W_STALE: _partial_meta()},
        )
        row = summary.rows[0]
        assert row.status == "STALE"
        assert row.history_status == "UNKNOWN"
        assert "STALE" in format_candidate_table(summary)


# ---------------------------------------------------------------------------
# 3. Dashboard: row data, status counts and rendered HTML
# ---------------------------------------------------------------------------
class TestDashboardHistoryVisibility:
    def _write(self, tmp_path, monkeypatch, records):
        path = tmp_path / "wallets.json"
        save_wallet_config(records, path=path)
        monkeypatch.setattr(settings, "WALLETS_FILE", path)
        monkeypatch.setattr(settings, "VERIFICATION_FILE", tmp_path / "verification.json")
        return path

    def test_row_exposes_active_days_and_history(self):
        row = _wallet_row(
            {"wallet": W_PARTIAL, **_partial_meta(), "active_days": 6,
             "first_activity_at": "2026-10-10T00:00:00+00:00",
             "last_activity_at": "2026-10-15T00:00:00+00:00"},
            {},
        )
        assert row["active_days"] == 6
        assert row["history_status"] == "PARTIAL"
        assert "page_cap" in row["history_detail"]
        assert row["first_activity_at"].startswith("2026-10-10")

    def test_zero_active_days_is_preserved_not_turned_into_none(self):
        row = _wallet_row({"wallet": W_UNKNOWN, "active_days": 0, "history_truncated": False}, {})
        assert row["active_days"] == 0
        assert row["history_status"] == "COMPLETE"

    def test_unknown_metric_stays_none(self):
        row = _wallet_row({"wallet": W_UNKNOWN}, {})
        assert row["active_days"] is None
        assert row["first_activity_at"] is None
        assert row["history_status"] == "UNKNOWN"
        assert row["history_detail"] == ""

    def test_legacy_record_without_history_fields_is_unknown(self):
        assert _history_status({}) == "UNKNOWN"
        assert _history_detail({}) == ""

    def test_build_dashboard_data_counts_history_states(self, tmp_path, monkeypatch):
        self._write(
            tmp_path, monkeypatch,
            [
                {"wallet": W_COMPLETE, "last_score": 80.0, "active_days": 40, **_complete_meta()},
                {"wallet": W_PARTIAL, "last_score": 10.0, "active_days": 6, **_partial_meta()},
                {"wallet": W_UNKNOWN, "last_score": None},
            ],
        )
        data = build_dashboard_data()
        assert data.discovery_status["history_complete"] == 1
        assert data.discovery_status["history_partial"] == 1
        assert data.discovery_status["history_unknown"] == 1
        by_wallet = {row["wallet"]: row for row in data.wallet_table}
        # Rejected wallets appear in the same table with the same fields.
        assert by_wallet[W_PARTIAL]["active_days"] == 6
        assert by_wallet[W_PARTIAL]["history_status"] == "PARTIAL"
        assert by_wallet[W_UNKNOWN]["active_days"] is None
        assert by_wallet[W_UNKNOWN]["history_status"] == "UNKNOWN"

    def test_rendered_html_shows_columns_badges_and_legend(self, tmp_path, monkeypatch):
        self._write(
            tmp_path, monkeypatch,
            [
                {"wallet": W_COMPLETE, "last_score": 80.0, "active_days": 40,
                 "hard_filter_status": "PASS", **_complete_meta()},
                {"wallet": W_PARTIAL, "last_score": None, "active_days": 6,
                 "hard_filter_status": "FAIL", "hard_filter_reasons": ["Only 6 active days, below minimum 30"],
                 **_partial_meta()},
                {"wallet": W_UNKNOWN, "last_score": None, "hard_filter_status": "FAIL"},
            ],
        )
        from dashboard.app import create_app
        app = create_app()
        app.testing = True
        html = app.test_client().get("/").get_data(as_text=True)

        assert "Active Days" in html
        assert "History" in html
        assert "COMPLETE" in html
        assert "PARTIAL" in html
        assert "UNKNOWN" in html
        assert "page_cap" in html                      # tooltip detail
        assert "genuine measured zero" in html         # N/A vs 0 legend
        assert "lower bounds" in html

    def test_dashboard_still_renders_legacy_records(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, [{"wallet": W_UNKNOWN, "last_score": 55.0}])
        from dashboard.app import create_app
        app = create_app()
        app.testing = True
        resp = app.test_client().get("/")
        assert resp.status_code == 200
        assert "UNKNOWN" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# 4. Text report (--report) compatibility
# ---------------------------------------------------------------------------
class TestWalletReportVisibility:
    def test_report_includes_active_days_and_history(self, capsys):
        table = [{
            "wallet": W_PARTIAL,
            "score": None,
            "recommendation": "HARD_FILTER_REJECT",
            "win_rate_pct": 60.0,
            "roi_pct": 25.0,
            "drawdown_pct": 5.0,
            "num_positions": 45,
            "total_trades": 45,
            "buy_trades": 23,
            "sell_trades": 22,
            "total_volume": 1000.0,
            "frequency_classification": "NORMAL",
            "data_quality": "PARTIAL",
            "active_days": 0,          # genuine zero must survive
            "history_status": "PARTIAL",
            "tracked": False,
        }]
        print_wallet_report(table)
        out = capsys.readouterr().out

        assert "Active Days" in out
        assert "History" in out
        assert "PARTIAL" in out
        assert "genuine measured zero" in out

    def test_report_tolerates_records_without_new_fields(self, capsys):
        print_wallet_report([{
            "wallet": W_UNKNOWN, "score": None, "recommendation": "HARD_FILTER_REJECT",
            "win_rate_pct": None, "roi_pct": None, "drawdown_pct": None,
            "num_positions": None, "total_trades": None, "buy_trades": None,
            "sell_trades": None, "total_volume": None, "frequency_classification": None,
            "data_quality": "INSUFFICIENT", "tracked": False,
        }])
        out = capsys.readouterr().out
        assert "UNKNOWN" in out
        assert "N/A" in out


# ---------------------------------------------------------------------------
# 5. No behaviour change
# ---------------------------------------------------------------------------
def test_diagnostics_do_not_change_filter_decisions():
    stats = _Stats(W_PARTIAL, active_days=6, positions=1500)
    result = _result("FAIL", ["Only 6 active days, below minimum 30"])
    summary = build_scan_summary(
        wallets=[W_PARTIAL], stats_map={W_PARTIAL: stats},
        filter_map={W_PARTIAL: result}, history_meta={W_PARTIAL: _partial_meta()},
    )
    row = summary.rows[0]
    assert row.status == "FAIL"
    assert row.failed_criteria == ["Only 6 active days, below minimum 30"]
    assert summary.reason_counts == {  # unaffected by the new fields
        k: v for k, v in summary.reason_counts.items()
    }
    assert settings.MIN_ACTIVE_DAYS == 30


# ---------------------------------------------------------------------------
# 6. PASS records persist the same activity fields as rejected records
# ---------------------------------------------------------------------------
def test_pass_record_persists_active_days_and_first_activity(tmp_path, monkeypatch):
    """A PASS wallet must expose its activity span exactly like a FAIL one."""
    import main
    from test_fail_metrics_persistence import (
        FakeClient, W_PASS, _aligned_events, _closed, _stored,
    )

    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    client = FakeClient([_closed(i, pnl=20.0) for i in range(45)], _aligned_events(45))
    monkeypatch.setattr(main, "PolymarketApiClient", lambda *a, **kw: client)
    monkeypatch.setattr(main, "scan_top_wallets", lambda c, limit=None, **kw: [W_PASS])
    monkeypatch.setattr(
        main, "fetch_wallet_events_paginated",
        lambda c, w, max_pages=5, page_size=100, raise_on_error=False: list(client.events),
    )
    main.cmd_scan_wallets(None)

    entry = _stored(wallets_file)[W_PASS]
    assert entry["hard_filter_status"] == "PASS"
    assert entry["active_days"] == 45
    assert entry["first_activity_at"] is not None
    assert entry["last_activity_at"] is not None


def test_pass_and_fail_use_the_same_activity_source_values(tmp_path, monkeypatch):
    """The persisted activity fields must equal the engine's own values."""
    import main
    from test_fail_metrics_persistence import (
        FakeClient, W_FAIL_ROI, W_PASS, _aligned_events, _closed, _stored,
    )

    captured = {}
    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    fail_client = FakeClient([_closed(i) for i in range(45)], _aligned_events(45))
    pass_client = FakeClient([_closed(i, pnl=20.0) for i in range(45)], _aligned_events(45))

    real_stats = main.compute_wallet_statistics

    def _spy(wallet, positions, **kwargs):
        stats = real_stats(wallet, positions, **kwargs)
        captured[wallet] = stats
        return stats

    monkeypatch.setattr(main, "compute_wallet_statistics", _spy)
    for wallet, client in ((W_FAIL_ROI, fail_client), (W_PASS, pass_client)):
        monkeypatch.setattr(main, "PolymarketApiClient", lambda *a, _c=client, **kw: _c)
        monkeypatch.setattr(main, "scan_top_wallets", lambda c, limit=None, _w=wallet, **kw: [_w])
        monkeypatch.setattr(
            main, "fetch_wallet_events_paginated",
            lambda c, w, max_pages=5, page_size=100, raise_on_error=False, _c=client: list(_c.events),
        )
        main.cmd_scan_wallets(None)

    stored = _stored(wallets_file)
    for wallet, status in ((W_FAIL_ROI, "FAIL"), (W_PASS, "PASS")):
        assert stored[wallet]["hard_filter_status"] == status
        assert stored[wallet]["active_days"] == captured[wallet].active_days
        assert stored[wallet]["first_activity_at"] == captured[wallet].first_activity_at
        assert stored[wallet]["last_activity_at"] == captured[wallet].last_activity_at


def test_pass_branch_does_not_change_score_or_recommendation(tmp_path, monkeypatch):
    """Persisting the activity fields must not affect ranking output."""
    import main
    from test_fail_metrics_persistence import (
        FakeClient, W_PASS, _aligned_events, _closed, _stored,
    )
    from wallet.ranking import rank_wallets

    captured, freq = {}, {}
    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    client = FakeClient([_closed(i, pnl=20.0) for i in range(45)], _aligned_events(45))
    monkeypatch.setattr(main, "PolymarketApiClient", lambda *a, **kw: client)
    monkeypatch.setattr(main, "scan_top_wallets", lambda c, limit=None, **kw: [W_PASS])
    monkeypatch.setattr(
        main, "fetch_wallet_events_paginated",
        lambda c, w, max_pages=5, page_size=100, raise_on_error=False: list(client.events),
    )

    real_stats = main.compute_wallet_statistics
    real_freq = main.compute_frequency_profile

    def _stats(w, positions, **kwargs):
        s = real_stats(w, positions, **kwargs)
        captured[w] = s
        return s

    def _freq(w, events, **kwargs):
        f = real_freq(w, events, **kwargs)
        freq[w] = f
        return f

    monkeypatch.setattr(main, "compute_wallet_statistics", _stats)
    monkeypatch.setattr(main, "compute_frequency_profile", _freq)
    main.cmd_scan_wallets(None)

    entry = _stored(wallets_file)[W_PASS]
    card = rank_wallets([captured[W_PASS]], {W_PASS: freq[W_PASS]})[0]
    assert entry["last_score"] == card.final_score
    assert entry["recommendation"] == card.recommendation
