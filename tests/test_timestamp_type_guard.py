"""
Regression tests for P5: `_parse_utc_date` must not raise on non-string
timestamps.

Defect: the helper assumed a string and called ``.replace`` on it, so a
numeric (or otherwise non-string) timestamp raised
``AttributeError: 'int' object has no attribute 'replace'`` and aborted the
whole scan. The real pipeline normalizes numeric timestamps in
``build_position_records``, so this was a latent robustness gap rather than a
live wrong answer -- but one unexpected payload shape should never crash a run.

The guard returns None for non-strings, which is the same outcome as an
unparseable string: the value is simply not counted as an active day.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from analytics.models import (
    PositionRecord,
    PositionSettlementClass,
    PositionSettlementType,
)
from analytics.performance import _parse_utc_date, compute_wallet_statistics


WALLET = "0x" + "5a" * 20


# ---------------------------------------------------------------------------
# Direct helper behaviour
# ---------------------------------------------------------------------------
class TestParseUtcDateAccepts:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("2026-01-15T12:00:00+00:00", "2026-01-15"),
            ("2026-01-15T12:00:00Z", "2026-01-15"),
            ("2026-01-15", "2026-01-15"),
            ("2026-01-15T23:30:00-05:00", "2026-01-16"),  # normalized to UTC
            ("2025-12-31T00:00:00+00:00", "2025-12-31"),
        ],
    )
    def test_iso_strings_still_parse(self, value, expected):
        assert _parse_utc_date(value) == expected

    @pytest.mark.parametrize("value", [None, "", 0, False])
    def test_empty_values_return_none(self, value):
        assert _parse_utc_date(value) is None

    @pytest.mark.parametrize("value", ["not-a-date", "2026-13-45T99:99:99", "   "])
    def test_malformed_strings_return_none(self, value):
        assert _parse_utc_date(value) is None


class TestParseUtcDateRejectsNonStrings:
    @pytest.mark.parametrize(
        "value",
        [
            1_700_000_000,      # the defect: epoch int
            1_700_000_000.5,    # epoch float
            [1_700_000_000],
            {"timestamp": 1_700_000_000},
            (1_700_000_000,),
            datetime(2026, 1, 15, tzinfo=timezone.utc),  # datetime, not str
            b"2026-01-15T12:00:00+00:00",                # bytes
            True,
        ],
    )
    def test_non_string_returns_none_instead_of_raising(self, value):
        assert _parse_utc_date(value) is None

    def test_defect_reproduction_would_have_raised(self):
        """Documents the pre-fix failure mode: an int has no .replace()."""
        value = 1_700_000_000
        assert not hasattr(value, "replace")


# ---------------------------------------------------------------------------
# End-to-end: one odd record must not abort the statistics computation
# ---------------------------------------------------------------------------
def _position(pid: str, opened_at, closed_at) -> PositionRecord:
    return PositionRecord(
        position_id=pid,
        condition_id=f"cond_{pid}",
        token_id=f"token_{pid}",
        market_slug=f"slug_{pid}",
        cost=100.0,
        proceeds=115.0,
        settled_net_result=15.0,
        settlement_class=PositionSettlementClass.CLOSED,
        settlement_type=PositionSettlementType.TRADING_EXIT,
        opened_at=opened_at,
        closed_at=closed_at,
        resolved_at=None,
        category="sports",
    )


class TestStatisticsRobustness:
    def test_numeric_timestamps_do_not_crash_statistics(self):
        positions = [
            _position("ok1", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
            _position("int1", 1_700_000_000, 1_700_086_400),      # would previously raise
            _position("none1", None, None),
            _position("bad1", "nonsense", "2026-01-03T00:00:00+00:00"),
        ]
        # Must not raise.
        stats = compute_wallet_statistics(WALLET, positions)

        # Only the parseable strings are counted; nothing is invented.
        assert stats.active_days == 3  # 2026-01-01, 2026-01-02, 2026-01-03
        assert stats.positions_counted == 4

    def test_all_numeric_timestamps_leave_the_window_unknown(self):
        positions = [
            _position("a", 1_700_000_000, 1_700_086_400),
            _position("b", 1_700_086_400, 1_700_172_800),
        ]
        stats = compute_wallet_statistics(WALLET, positions)

        assert stats.active_days == 0        # nothing parseable, nothing invented
        assert stats.first_activity_at is None
        assert stats.last_activity_at is None

    def test_mixed_timestamp_types_do_not_break_ordering(self):
        """min()/max() over a mixed str/int set used to raise TypeError."""
        positions = [
            _position("a", 1_700_000_000, 1_700_086_400),
            _position("b", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
        ]
        stats = compute_wallet_statistics(WALLET, positions)

        assert stats.active_days == 2         # only the string-derived dates
        assert stats.first_activity_at == "2026-01-01T00:00:00+00:00"
        # A position without a usable timestamp makes ordered drawdown undefined.
        assert stats.drawdown_cost_pct is None

    def test_drawdown_with_undatable_positions_stays_unknown(self):
        positions = [_position("a", 1_700_000_000, 1_700_086_400)]
        stats = compute_wallet_statistics(WALLET, positions)

        # Ordering requires timestamps; without usable ones drawdown is
        # undefined, never silently zero.
        assert stats.drawdown_cost_pct is None

    def test_string_timestamps_are_completely_unaffected(self):
        before = compute_wallet_statistics(
            WALLET,
            [_position(f"p{i}", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00")
             for i in range(5)],
        )
        assert before.active_days == 2
        assert before.roi_fee_exclusive_pct == pytest.approx(15.0)
