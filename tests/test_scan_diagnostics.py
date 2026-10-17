"""
Phase C tests: scan diagnostics (monitor/scan_report.py) and candidate
identity history (first_seen_at / last_seen_at / times_seen / discovery_source).

Verified here:
- the summary counts PASS / FAIL / INSUFFICIENT_DATA / STALE truthfully
- rejection reasons are grouped by cause (numbers stripped) with a concrete example
- previously known vs new candidates, and wallets retained although not rediscovered
- frequency classification is reported as a SCORE PENALTY (never an exclusion)
- discovery counters (pages, rows, validation, duplicates, seeds) are surfaced
- the end-to-end scan prints the diagnostics block
- repeated candidates across scans are visible via times_seen/first_seen_at
- a failed (STALE) refresh still records that the wallet was seen, without
  touching its preserved metrics

Everything runs against temporary wallets.json files; the real data/wallets.json
is never read or written.
"""
from __future__ import annotations

from filters.wallet_filter import WalletFilterResult
from config.settings import settings
from monitor.scan_report import (
    build_scan_summary,
    format_candidate_table,
    format_scan_summary,
    normalize_reason,
)
from wallet.monitor import save_wallet_config

from test_stale_data_handling import (
    FakeClient,
    GOOD_CLOSED,
    GOOD_EVENTS,
    W1,
    W2,
    W3,
    _previous_record,
    _scan,
    _stored,
)


def _result(status: str, reasons=None, insufficient=None) -> WalletFilterResult:
    return WalletFilterResult(
        passed=status == "PASS",
        reasons=list(reasons or []),
        status=status,
        insufficient_criteria=list(insufficient or []),
    )


class _Stats:
    """Minimal stand-in carrying exactly the fields the report reads."""

    def __init__(self, wallet, roi=None, win_rate=None, positions=0, active_days=0,
                 drawdown=None, mean=None, stdev=None):
        self.wallet = wallet
        self.roi_fee_exclusive_pct = roi
        self.win_rate_pct = win_rate
        self.positions_counted = positions
        self.active_days = active_days
        self.drawdown_cost_pct = drawdown
        self.consistency_mean_roi = mean
        self.consistency_stdev_roi = stdev


class _Freq:
    def __init__(self, classification):
        self.classification = classification


# ---------------------------------------------------------------------------
# Reason grouping
# ---------------------------------------------------------------------------

def test_normalize_reason_groups_reasons_that_differ_only_by_numbers():
    a = normalize_reason("Consistency 1.1 below minimum 4.4")
    b = normalize_reason("Consistency 9.0 below minimum 4.4")
    assert a == b
    assert normalize_reason("ROI 84.5% above reject threshold 80.0%") != a


def test_rejection_reasons_are_grouped_with_a_concrete_example():
    wallets = [W1, W2, W3]
    summary = build_scan_summary(
        wallets=wallets,
        filter_map={
            W1: _result("FAIL", ["Consistency 1.1 below minimum 4.4"]),
            W2: _result("FAIL", ["Consistency 3.2 below minimum 4.4"]),
            W3: _result("FAIL", ["ROI 84.5% above reject threshold 80.0%"]),
        },
    )

    assert summary.status_counts["FAIL"] == 3
    counts = list(summary.reason_counts.values())
    assert sorted(counts, reverse=True) == [2, 1]
    grouped = summary.reason_counts[normalize_reason("Consistency 1.1 below minimum 4.4")]
    assert grouped == 2
    assert summary.reason_examples[normalize_reason("Consistency 1.1 below minimum 4.4")] == (
        "Consistency 1.1 below minimum 4.4"
    )


def test_meets_all_thresholds_line_is_not_counted_as_a_rejection_reason():
    summary = build_scan_summary(
        wallets=[W1],
        filter_map={W1: _result("PASS", ["Wallet meets all configured filter thresholds."])},
    )

    assert summary.status_counts["PASS"] == 1
    assert not summary.reason_counts


def test_insufficient_data_criteria_are_counted():
    summary = build_scan_summary(
        wallets=[W1],
        filter_map={W1: _result("INSUFFICIENT_DATA", ["Wallet meets all evaluable filter thresholds."],
                                insufficient=["roi", "drawdown"])},
    )

    assert summary.status_counts["INSUFFICIENT_DATA"] == 1
    assert summary.insufficient_counts["roi"] == 1
    assert summary.insufficient_counts["drawdown"] == 1


# ---------------------------------------------------------------------------
# Known vs new, retained, sources
# ---------------------------------------------------------------------------

def test_known_versus_new_candidates_are_counted():
    existing = [{"wallet": W1}, {"wallet": W3}]
    summary = build_scan_summary(wallets=[W1, W2], existing_records=existing)

    assert summary.candidates_total == 2
    assert summary.known_before == 1        # W1
    assert summary.new_this_scan == 1       # W2
    # W3 was known but not rediscovered -> retained, counted separately
    assert summary.retained_not_rediscovered == 1


def test_wallet_sources_are_reported_when_discovery_counters_exist():
    class _Discovery:
        pages_attempted = 1
        pages_succeeded = 1
        rows_returned = 20
        rejected_addresses = 2
        duplicates_dropped = 0
        leaderboard_candidates = 1
        seed_candidates = 1
        seed_duplicates = 0
        leaderboard_budget = 20
        stop_reason = "leaderboard_budget"
        sources = {W1: "leaderboard", W2: "seed"}

    summary = build_scan_summary(wallets=[W1, W2], discovery=_Discovery())
    by_wallet = {row.wallet: row for row in summary.rows}

    assert by_wallet[W1].source == "leaderboard"
    assert by_wallet[W2].source == "seed"
    assert summary.discovery["rows_returned"] == 20
    text = format_scan_summary(summary)
    assert "pages attempted" in text
    assert "candidates from seeds" in text


def test_missing_discovery_counters_are_reported_honestly_not_invented():
    summary = build_scan_summary(wallets=[W1], discovery=None)

    assert summary.discovery is None
    text = format_scan_summary(summary)
    assert "counters unavailable" in text


# ---------------------------------------------------------------------------
# Frequency: penalty, never exclusion
# ---------------------------------------------------------------------------

def test_stale_reason_is_reported_separately_not_as_a_filter_rejection():
    stale_reason = (
        "Refresh incomplete: /closed-positions request failed: boom. Previously stored "
        "performance metrics, scores and recommendations were retained unchanged; this "
        "scan's data is STALE and cannot qualify as a fresh PASS."
    )
    summary = build_scan_summary(
        wallets=[W1, W2],
        filter_map={W1: _result("STALE", [stale_reason]), W2: _result("FAIL", ["Only 4 positions, below minimum 40"])},
        stale_wallets=[W1],
        stale_causes={W1: ["/closed-positions request failed: boom"]},
    )

    assert summary.status_counts["STALE"] == 1
    # the filter-reason counts describe FILTER outcomes only
    assert summary.reason_counts[normalize_reason("Only 4 positions, below minimum 40")] == 1
    assert normalize_reason(stale_reason) not in summary.reason_counts
    assert summary.stale_causes == [{
        "wallet": W1,
        "causes": ["/closed-positions request failed: boom"],
        "metrics_kept": True,
    }]
    text = format_scan_summary(summary)
    assert "STALE refreshes" in text
    assert "closed-positions request failed: boom" in text
    # the per-candidate row shows the real cause, not the stored boilerplate
    stale_row = next(r for r in summary.rows if r.wallet == W1)
    assert stale_row.reason == "/closed-positions request failed: boom"


def test_grouped_reasons_list_their_distinct_concrete_examples():
    """A grouped count must not hide which values were actually involved."""
    summary = build_scan_summary(
        wallets=[W1, W2],
        filter_map={
            W1: _result("FAIL", ["Only 4 positions, below minimum 40"]),
            W2: _result("FAIL", ["Only 0 positions, below minimum 40"]),
        },
    )
    group = normalize_reason("Only 4 positions, below minimum 40")
    assert summary.reason_counts[group] == 2
    assert set(summary.reason_variants[group]) == {
        "Only 4 positions, below minimum 40", "Only 0 positions, below minimum 40",
    }

    text = format_scan_summary(summary)
    assert "Only N positions, below minimum N" in text     # the grouped cause
    assert "Only 4 positions, below minimum 40" in text    # concrete example 1
    assert "Only 0 positions, below minimum 40" in text    # concrete example 2


def test_frequency_is_reported_as_a_penalty_not_an_exclusion():
    summary = build_scan_summary(
        wallets=[W1, W2, W3],
        filter_map={
            W1: _result("PASS"), W2: _result("PASS"),
            W3: _result("FAIL", ["Wallet appears resolution-only (no active exits)"]),
        },
        freq_map={W1: _Freq("BOT_LIKE"), W2: _Freq("ACTIVE"), W3: _Freq("RAPID_TRADER")},
    )

    # Only PASS wallets can be ranked, so only those are counted as penalised.
    assert summary.frequency_bot_like == 1
    assert summary.frequency_penalized == 2
    text = format_scan_summary(summary)
    assert "never excludes a wallet from ranking" in text
    assert "RAPID_TRADER" in text


# ---------------------------------------------------------------------------
# End-to-end scan output
# ---------------------------------------------------------------------------

def test_scan_prints_diagnostics_and_headline_explanation(tmp_path, monkeypatch, capsys):
    wallets_file = tmp_path / "wallets.json"
    save_wallet_config([_previous_record(W2)], path=wallets_file)
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W1, W2], FakeClient(
        closed_by_wallet={W1: GOOD_CLOSED}, events_by_wallet={W1: GOOD_EVENTS},
    ))
    out = capsys.readouterr().out

    assert "SCAN DIAGNOSTICS" in out
    assert "candidates this scan   : 2" in out
    assert "previously known       : 1" in out
    assert "new this scan          : 1" in out
    assert "Acceptance band in force" in out
    assert "WHY only 1 passed" in out


def test_candidate_table_shows_na_instead_of_fake_zeros(capsys):
    summary = build_scan_summary(
        wallets=[W1],
        stats_map={W1: _Stats(W1, roi=42.0, positions=50, active_days=40)},
        filter_map={W1: _result("FAIL", ["Drawdown 30.0% above maximum 20.0%"])},
    )

    table = format_candidate_table(summary)

    assert "N/A" in table          # win rate / consistency / drawdown unavailable
    assert "Drawdown" in table


# ---------------------------------------------------------------------------
# Candidate identity history
# ---------------------------------------------------------------------------

def test_first_scan_records_candidate_history(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W1], FakeClient(
        closed_by_wallet={W1: GOOD_CLOSED}, events_by_wallet={W1: GOOD_EVENTS},
    ))

    entry = _stored(wallets_file)[W1]
    assert entry["times_seen"] == 1
    assert entry["first_seen_at"] == entry["last_seen_at"]
    assert entry["first_seen_at"] == entry["last_refresh_attempt"]
    assert entry["discovery_source"] in ("leaderboard", "seed", "unknown")


def test_repeated_scan_increments_times_seen_and_keeps_first_seen_at(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    client = FakeClient(closed_by_wallet={W1: GOOD_CLOSED}, events_by_wallet={W1: GOOD_EVENTS})
    _scan(monkeypatch, [W1], client)
    first = _stored(wallets_file)[W1]

    _scan(monkeypatch, [W1], client)
    second = _stored(wallets_file)[W1]

    assert second["times_seen"] == first["times_seen"] + 1
    assert second["first_seen_at"] == first["first_seen_at"]     # never rewritten
    assert second["last_seen_at"] >= first["last_seen_at"]


def test_history_is_not_invented_for_wallets_that_were_not_rediscovered(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    manual = {"wallet": W3, "tracked": True, "notes": "added by hand"}   # no history fields
    save_wallet_config([manual], path=wallets_file)
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W1], FakeClient(
        closed_by_wallet={W1: GOOD_CLOSED}, events_by_wallet={W1: GOOD_EVENTS},
    ))

    stored = _stored(wallets_file)
    assert stored[W3] == manual                          # untouched, no fabricated history
    assert "first_seen_at" not in stored[W3]
    assert stored[W1]["times_seen"] == 1


def test_stale_wallet_records_being_seen_without_touching_its_metrics(tmp_path, monkeypatch):
    wallets_file = tmp_path / "wallets.json"
    previous = _previous_record(
        W2, times_seen=4, first_seen_at="2026-10-01T00:00:00+00:00",
        last_seen_at="2026-10-14T18:05:00+00:00", discovery_source="leaderboard",
    )
    save_wallet_config([previous], path=wallets_file)
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, [W2], FakeClient(closed_error={W2}))

    entry = _stored(wallets_file)[W2]
    assert entry["data_quality"] == "STALE"
    assert entry["times_seen"] == 5                                  # it WAS rediscovered
    assert entry["first_seen_at"] == "2026-10-01T00:00:00+00:00"     # unchanged
    assert entry["last_seen_at"] > previous["last_seen_at"]
    # ...while every performance metric stays exactly as the last success left it
    for field in ("last_score", "roi_pct", "win_rate_pct", "num_positions",
                  "realized_pnl", "recommendation", "last_successful_refresh"):
        assert entry[field] == previous[field], field
    assert entry["discovery_source"] == "leaderboard"


# ---------------------------------------------------------------------------
# Near-miss analysis (Phase C "try again" pass)
# ---------------------------------------------------------------------------

def test_near_misses_lists_wallets_that_missed_by_exactly_one_criterion():
    summary = build_scan_summary(
        wallets=[W1, W2, W3],
        filter_map={
            W1: _result("FAIL", ["ROI 84.5% above reject threshold 80.0%"]),
            W2: _result("FAIL", ["Consistency 3.0 below minimum 4.4",
                                 "Only 8 active days, below minimum 30"]),
            W3: _result("FAIL", ["Wallet appears resolution-only (no active exits)"]),
        },
    )

    near = {entry["wallet"] for entry in summary.near_misses}
    assert near == {W1, W3}                      # W2 missed by two criteria
    assert summary.failed_criteria_distribution[1] == 2
    assert summary.failed_criteria_distribution[2] == 1

    text = format_scan_summary(summary)
    assert "CLOSEST TO PASSING" in text
    assert "ROI 84.5% above reject threshold 80.0%" in text
    assert "missed exactly 1 criterion: 2" in text


def test_failed_criteria_counts_exclude_the_informational_line():
    summary = build_scan_summary(
        wallets=[W1],
        filter_map={W1: _result("FAIL", [
            "Wallet meets all evaluable filter thresholds.",   # informational
            "Only 8 positions, below minimum 40",
        ])},
    )

    assert summary.rows[0].failed_criteria == ["Only 8 positions, below minimum 40"]
    assert 1 in summary.failed_criteria_distribution


def test_insufficient_data_rows_count_missing_criteria_instead():
    summary = build_scan_summary(
        wallets=[W1],
        filter_map={W1: _result("INSUFFICIENT_DATA", ["Wallet meets all evaluable filter thresholds."],
                                insufficient=["roi", "drawdown"])},
    )

    # INSUFFICIENT_DATA is not a FAIL: it must not appear as a near miss
    assert not summary.near_misses
    assert not summary.failed_criteria_distribution
    assert summary.rows[0].failed_criteria == ["roi", "drawdown"]


def test_outcome_is_split_by_discovery_source_with_median_roi():
    class _Discovery:
        pages_attempted = 1
        pages_succeeded = 1
        rows_returned = 30
        rejected_addresses = 0
        duplicates_dropped = 0
        leaderboard_candidates = 1
        seed_candidates = 2
        seed_duplicates = 0
        leaderboard_budget = 28
        stop_reason = "short_page"
        sources = {W1: "leaderboard", W2: "seed", W3: "seed"}

    summary = build_scan_summary(
        wallets=[W1, W2, W3],
        discovery=_Discovery(),
        stats_map={
            W1: _Stats(W1, roi=24.1, positions=50, active_days=40),
            W2: _Stats(W2, roi=84.5, positions=50, active_days=40),
            W3: _Stats(W3, roi=90.3, positions=50, active_days=40),
        },
        filter_map={
            W1: _result("FAIL", ["ROI 24.1% below minimum 25.0%"]),
            W2: _result("PASS"),
            W3: _result("FAIL", ["ROI 90.3% above reject threshold 80.0%"]),
        },
    )

    assert summary.by_source["leaderboard"]["scanned"] == 1
    assert summary.by_source["leaderboard"]["FAIL"] == 1
    assert summary.by_source["seed"]["scanned"] == 2
    assert summary.by_source["seed"]["PASS"] == 1
    assert summary.by_source["seed"]["rois"] == [84.5, 90.3]

    text = format_scan_summary(summary)
    assert "Outcome by discovery source" in text
    # a TRUE median (mean of the two middle values), not the upper-middle pick
    assert "median ROI: 87.4%" in text
    assert "median ROI: 24.1%" in text


def test_headline_points_at_single_criterion_misses(tmp_path, monkeypatch, capsys):
    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    good = FakeClient(closed_by_wallet={W1: GOOD_CLOSED}, events_by_wallet={W1: GOOD_EVENTS})
    _scan(monkeypatch, [W1], good)
    out = capsys.readouterr().out

    # 1 PASS / 0 rejected -> no near-miss claim is fabricated
    assert "CLOSEST TO PASSING" not in out
