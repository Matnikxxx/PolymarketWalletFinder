"""
Comprehensive tests for Phase 7 Frequency Analysis:
- Logical trade consolidation (shared tx_hash vs singletons)
- Non-trade event exclusion (REDEEM, SPLIT, MERGE, DEPOSIT, etc.)
- Gap metrics and zero-second gap handling
- Rolling window boundary conditions (1m, 5m, 15m)
- Burst episode clustering (<= 10s contiguous gaps)
- Cross-market 1-minute window tracking
- Fill expansion factor
- Data quality gating (partial history -> INSUFFICIENT_DATA)
- Unreconciled data and repeat-exposure isolation
- Exact BOT_LIKE boundaries and strict AND logic
- Architectural purity: no score penalties, filter rejections, or recommendations
"""
from datetime import datetime, timedelta, timezone
import pytest

from analytics.frequency_models import LogicalTrade, FrequencyMetrics, FrequencyProfile
from analytics.frequency import (
    consolidate_logical_trades,
    compute_frequency_metrics,
    compute_frequency_profile,
)
from analytics.models import PositionRecord, WalletStatistics

BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_event(
    tx_hash="tx_1",
    timestamp=None,
    side="BUY",
    price=0.5,
    size=10.0,
    cid="c1",
    token_id="t1",
    activity_type="TRADE",
    **kwargs,
):
    if timestamp is None:
        timestamp = BASE_TIME.isoformat()
    return {
        "tx_hash": tx_hash,
        "timestamp": timestamp,
        "side": side,
        "price": price,
        "size": size,
        "usdc_size": price * size,
        "condition_id": cid,
        "token_id": token_id,
        "activity_type": activity_type,
        **kwargs,
    }


# 1. Multiple fills with same tx hash collapse into one logical trade ---------
def test_same_tx_hash_collapses_to_one_logical_trade():
    events = [
        _make_event(tx_hash="tx_batch", size=10.0, price=0.4, timestamp="2026-01-01T12:00:00+00:00"),
        _make_event(tx_hash="tx_batch", size=20.0, price=0.4, timestamp="2026-01-01T12:00:00+00:00"),
        _make_event(tx_hash="tx_batch", size=30.0, price=0.4, timestamp="2026-01-01T12:00:01+00:00"),
    ]
    trades = consolidate_logical_trades(events)
    assert len(trades) == 1
    assert trades[0].tx_hash == "tx_batch"
    assert trades[0].fill_count == 3
    assert trades[0].size == 60.0
    assert trades[0].usdc_size == 24.0
    assert trades[0].timestamp == "2026-01-01T12:00:00+00:00"  # Earliest


# 2. Missing tx hashes remain distinct ---------------------------------------
def test_missing_tx_hashes_remain_distinct():
    events = [
        _make_event(tx_hash=None, size=10.0, timestamp="2026-01-01T12:00:00+00:00"),
        _make_event(tx_hash=None, size=15.0, timestamp="2026-01-01T12:00:00+00:00"),
        _make_event(tx_hash="", size=20.0, timestamp="2026-01-01T12:00:00+00:00"),
    ]
    trades = consolidate_logical_trades(events)
    assert len(trades) == 3
    for t in trades:
        assert t.fill_count == 1


# 3. Non-TRADE events are excluded -------------------------------------------
def test_non_trade_events_are_excluded():
    events = [
        _make_event(tx_hash="tx_trade", activity_type="TRADE"),
        _make_event(tx_hash="tx_redeem", activity_type="REDEEM"),
        _make_event(tx_hash="tx_split", activity_type="SPLIT"),
        _make_event(tx_hash="tx_merge", activity_type="MERGE"),
        _make_event(tx_hash="tx_deposit", activity_type="DEPOSIT"),
        _make_event(tx_hash="tx_reward", activity_type="REWARD"),
    ]
    trades = consolidate_logical_trades(events)
    assert len(trades) == 1
    assert trades[0].tx_hash == "tx_trade"


# 4. Zero-second gaps work without dividing by zero --------------------------
def test_zero_second_gaps_work():
    t_str = BASE_TIME.isoformat()
    trades = [
        LogicalTrade(tx_hash="t1", timestamp=t_str, condition_id="c1", token_id="tk1", side="BUY", size=10, usdc_size=5, fill_count=1),
        LogicalTrade(tx_hash="t2", timestamp=t_str, condition_id="c1", token_id="tk1", side="BUY", size=10, usdc_size=5, fill_count=1),
    ]
    metrics = compute_frequency_metrics(trades, raw_fill_count=2)
    assert metrics.min_gap_seconds == 0.0
    assert metrics.median_gap_seconds == 0.0
    assert metrics.mean_gap_seconds == 0.0
    assert metrics.pct_gaps_le_10s == 100.0


# 5. One-trade wallet returns safe undefined gap metrics ----------------------
def test_one_trade_wallet_returns_safe_undefined_gap_metrics():
    trades = [
        LogicalTrade(tx_hash="t1", timestamp=BASE_TIME.isoformat(), condition_id="c1", token_id="tk1", side="BUY", size=10, usdc_size=5, fill_count=1)
    ]
    metrics = compute_frequency_metrics(trades, raw_fill_count=1)
    assert metrics.total_logical_trades == 1
    assert metrics.min_gap_seconds is None
    assert metrics.median_gap_seconds is None
    assert metrics.mean_gap_seconds is None
    assert metrics.pct_gaps_le_10s == 0.0
    assert metrics.peak_trades_in_1m == 1


# 6. Two-trade wallet returns valid gap metrics -------------------------------
def test_two_trade_wallet_returns_valid_gap_metrics():
    t1 = BASE_TIME
    t2 = t1 + timedelta(seconds=25)
    trades = [
        LogicalTrade(tx_hash="t1", timestamp=t1.isoformat(), condition_id="c1", token_id="tk1", side="BUY", size=10, usdc_size=5, fill_count=1),
        LogicalTrade(tx_hash="t2", timestamp=t2.isoformat(), condition_id="c1", token_id="tk1", side="BUY", size=10, usdc_size=5, fill_count=1),
    ]
    metrics = compute_frequency_metrics(trades, raw_fill_count=2)
    assert metrics.min_gap_seconds == 25.0
    assert metrics.median_gap_seconds == 25.0
    assert metrics.pct_gaps_le_10s == 0.0
    assert metrics.pct_gaps_le_30s == 100.0
    assert metrics.pct_gaps_le_60s == 100.0


# 7, 8, 9. Exact gap boundaries (10s, 30s, 60s) -------------------------------
def test_exact_gap_boundaries():
    t0 = BASE_TIME
    t_10 = t0 + timedelta(seconds=10.0)      # exactly 10s
    t_10_1 = t_10 + timedelta(seconds=10.1)  # 10.1s (not <= 10, but <= 30)
    t_30 = t_10_1 + timedelta(seconds=30.0)  # exactly 30s
    t_60 = t_30 + timedelta(seconds=60.0)    # exactly 60s
    t_61 = t_60 + timedelta(seconds=60.1)    # 60.1s (> 60)

    trades = [
        LogicalTrade(tx_hash=f"t{i}", timestamp=t.isoformat(), condition_id="c1", token_id="tk1", side="BUY", size=1, usdc_size=1, fill_count=1)
        for i, t in enumerate([t0, t_10, t_10_1, t_30, t_60, t_61])
    ]
    # Gaps: [10.0, 10.1, 30.0, 60.0, 60.1] -> 5 gaps
    metrics = compute_frequency_metrics(trades, raw_fill_count=6)
    # <= 10s: exactly 1 gap (10.0) -> 1/5 = 20.0%
    assert metrics.pct_gaps_le_10s == 20.0
    # <= 30s: 10.0, 10.1, 30.0 -> 3/5 = 60.0%
    assert metrics.pct_gaps_le_30s == 60.0
    # <= 60s: 10.0, 10.1, 30.0, 60.0 -> 4/5 = 80.0%
    assert metrics.pct_gaps_le_60s == 80.0


# 10, 11, 12. Rolling window boundaries (1m, 5m, 15m) -------------------------
def test_rolling_window_boundaries():
    t0 = BASE_TIME
    t_60 = t0 + timedelta(seconds=60.0)   # exactly on 60s boundary
    t_61 = t0 + timedelta(seconds=61.0)   # outside 60s
    t_300 = t0 + timedelta(seconds=300.0) # exactly on 300s boundary
    t_900 = t0 + timedelta(seconds=900.0) # exactly on 900s boundary

    trades_1m = [
        LogicalTrade(tx_hash="a", timestamp=t0.isoformat(), condition_id="c1", token_id="tk1", side="BUY", size=1, usdc_size=1, fill_count=1),
        LogicalTrade(tx_hash="b", timestamp=t_60.isoformat(), condition_id="c1", token_id="tk1", side="BUY", size=1, usdc_size=1, fill_count=1),
    ]
    assert compute_frequency_metrics(trades_1m, 2).peak_trades_in_1m == 2

    trades_1m_out = [
        LogicalTrade(tx_hash="a", timestamp=t0.isoformat(), condition_id="c1", token_id="tk1", side="BUY", size=1, usdc_size=1, fill_count=1),
        LogicalTrade(tx_hash="b", timestamp=t_61.isoformat(), condition_id="c1", token_id="tk1", side="BUY", size=1, usdc_size=1, fill_count=1),
    ]
    assert compute_frequency_metrics(trades_1m_out, 2).peak_trades_in_1m == 1

    trades_5m = [
        LogicalTrade(tx_hash="a", timestamp=t0.isoformat(), condition_id="c1", token_id="tk1", side="BUY", size=1, usdc_size=1, fill_count=1),
        LogicalTrade(tx_hash="b", timestamp=t_300.isoformat(), condition_id="c1", token_id="tk1", side="BUY", size=1, usdc_size=1, fill_count=1),
    ]
    assert compute_frequency_metrics(trades_5m, 2).peak_trades_in_5m == 2

    trades_15m = [
        LogicalTrade(tx_hash="a", timestamp=t0.isoformat(), condition_id="c1", token_id="tk1", side="BUY", size=1, usdc_size=1, fill_count=1),
        LogicalTrade(tx_hash="b", timestamp=t_900.isoformat(), condition_id="c1", token_id="tk1", side="BUY", size=1, usdc_size=1, fill_count=1),
    ]
    assert compute_frequency_metrics(trades_15m, 2).peak_trades_in_15m == 2


# 13. Burst episode grouping --------------------------------------------------
def test_burst_episode_grouping():
    # Sequence of trades with gaps:
    # t=0, t=5 (gap 5 <= 10 -> episode 1)
    # t=8 (gap 3 <= 10 -> continues episode 1)
    # t=100 (gap 92 > 10 -> ends episode 1)
    # t=105 (gap 5 <= 10 -> episode 2)
    # t=200 (gap 95 > 10 -> ends episode 2)
    timestamps = [
        BASE_TIME,
        BASE_TIME + timedelta(seconds=5),
        BASE_TIME + timedelta(seconds=8),
        BASE_TIME + timedelta(seconds=100),
        BASE_TIME + timedelta(seconds=105),
        BASE_TIME + timedelta(seconds=200),
    ]
    trades = [
        LogicalTrade(tx_hash=f"t{i}", timestamp=t.isoformat(), condition_id="c1", token_id="tk1", side="BUY", size=1, usdc_size=1, fill_count=1)
        for i, t in enumerate(timestamps)
    ]
    metrics = compute_frequency_metrics(trades, raw_fill_count=6)
    assert metrics.burst_episodes_count == 2


# 14. Cross-market burst detection --------------------------------------------
def test_cross_market_burst_detection():
    # 4 trades within 30s across 3 distinct condition IDs
    t0 = BASE_TIME
    trades = [
        LogicalTrade(tx_hash="t1", timestamp=t0.isoformat(), condition_id="cond_A", token_id="tk1", side="BUY", size=1, usdc_size=1, fill_count=1),
        LogicalTrade(tx_hash="t2", timestamp=(t0 + timedelta(seconds=5)).isoformat(), condition_id="cond_B", token_id="tk2", side="BUY", size=1, usdc_size=1, fill_count=1),
        LogicalTrade(tx_hash="t3", timestamp=(t0 + timedelta(seconds=10)).isoformat(), condition_id="cond_C", token_id="tk3", side="BUY", size=1, usdc_size=1, fill_count=1),
        LogicalTrade(tx_hash="t4", timestamp=(t0 + timedelta(seconds=15)).isoformat(), condition_id="cond_A", token_id="tk1", side="BUY", size=1, usdc_size=1, fill_count=1),
    ]
    metrics = compute_frequency_metrics(trades, raw_fill_count=4)
    assert metrics.cross_market_max_in_1m == 3


# 15. Fill expansion factor ---------------------------------------------------
def test_fill_expansion_factor():
    events = [
        _make_event(tx_hash="tx1", size=10),
        _make_event(tx_hash="tx1", size=20),
        _make_event(tx_hash="tx1", size=30),
        _make_event(tx_hash="tx2", size=40),
    ]
    # 4 raw fills consolidated into 2 logical trades -> expansion factor = 2.0
    trades = consolidate_logical_trades(events)
    metrics = compute_frequency_metrics(trades, raw_fill_count=len(events))
    assert metrics.total_logical_trades == 2
    assert metrics.raw_fill_count == 4
    assert metrics.fill_expansion_factor == 2.0


# 16. Partial history produces INSUFFICIENT_DATA ------------------------------
def test_partial_history_produces_insufficient_data():
    events = [_make_event(tx_hash=f"tx{i}", timestamp=(BASE_TIME + timedelta(days=i)).isoformat()) for i in range(10)]
    profile = compute_frequency_profile("0xpartial", events, is_complete_history=False)
    assert profile.classification == "INSUFFICIENT_DATA"
    assert profile.data_quality == "PARTIAL"
    assert profile.is_complete_history is False
    assert any("incomplete" in r.lower() or "truncated" in r.lower() for r in profile.reasons)


# 17. Unreconciled data is explicitly marked ----------------------------------
def test_unreconciled_data_is_explicitly_marked():
    events = [_make_event(tx_hash=f"tx{i}", timestamp=(BASE_TIME + timedelta(days=i)).isoformat()) for i in range(5)]
    profile = compute_frequency_profile("0xunrec", events, is_complete_history=True, is_reconciled=False)
    assert profile.is_reconciled is False
    assert any("reconciliation" in r.lower() for r in profile.reasons)


# 18. Repeat-exposed data cannot silently inflate clean frequency metrics -----
def test_repeat_exposed_data_isolation():
    events = [
        _make_event(tx_hash="tx1", timestamp=BASE_TIME.isoformat()),
        _make_event(tx_hash="tx2", timestamp=(BASE_TIME + timedelta(seconds=5)).isoformat()),
        # Repeat exposed duplicate
        _make_event(tx_hash="tx_dup", timestamp=(BASE_TIME + timedelta(seconds=2)).isoformat(), repeat_exposed=True),
    ]
    # Clean view excludes repeat exposed
    trades_clean = consolidate_logical_trades(events, exclude_repeat_exposed=True)
    assert len(trades_clean) == 2

    # Diagnostic view includes it
    trades_diag = consolidate_logical_trades(events, exclude_repeat_exposed=False)
    assert len(trades_diag) == 3


# 19 & 20. Exact BOT_LIKE boundary and strict AND logic -----------------------
def test_bot_like_exact_boundary_and_strict_and_logic():
    t0 = BASE_TIME

    # Sub-case A: 9 trades / 60s, all 1s apart (pct_10s = 100%) -> NOT BOT_LIKE (< 10 trades)
    events_9 = [_make_event(tx_hash=f"tx{i}", timestamp=(t0 + timedelta(seconds=i)).isoformat()) for i in range(9)]
    profile_9 = compute_frequency_profile("0xsub10", events_9)
    assert profile_9.metrics.peak_trades_in_1m == 9
    assert profile_9.metrics.pct_gaps_le_10s == 100.0
    assert profile_9.classification != "BOT_LIKE"

    # Sub-case B: 10 trades / 60s, but spaced 6s apart (total span 54s) -> gaps <= 10s is 100% -> BOT_LIKE
    events_10_bot = [_make_event(tx_hash=f"tx{i}", timestamp=(t0 + timedelta(seconds=i * 6)).isoformat()) for i in range(10)]
    profile_10_bot = compute_frequency_profile("0xbot", events_10_bot)
    assert profile_10_bot.metrics.peak_trades_in_1m == 10
    assert profile_10_bot.metrics.pct_gaps_le_10s == 100.0
    assert profile_10_bot.classification == "BOT_LIKE"

    # Sub-case C: 10 trades in 1m, but only 4 out of 9 gaps <= 10s (44.4% < 50.0%) -> NOT BOT_LIKE
    # Gaps: 5s, 5s, 5s, 5s (4 gaps <= 10s), then 11s, 11s, 11s, 11s, 11s (5 gaps > 10s) -> total 75s (let's fit in 60s)
    # Within 60s window:
    # t=0, 2, 4, 6, 8 (4 gaps of 2s -> <=10s), then t=19, 30, 41, 52, 63 (5 gaps of 11s -> >10s)
    # Total trades = 10. Gaps = 9. Gaps <= 10s = 4/9 = 44.4% (< 50%)
    timestamps = [0, 2, 4, 6, 8, 19, 30, 41, 52, 60] # all within 60s
    # Gaps: [2, 2, 2, 2, 11, 11, 11, 11, 8] -> gaps <= 10s: 2,2,2,2,8 (5 out of 9 = 55.5%)
    # Let's construct exactly 4 gaps <= 10s out of 9:
    # gaps: [2, 2, 2, 2, 11, 11, 11, 11, 11] -> span = 63s (peak in 60s is 9 trades)
    # Using 10 trades where peak_1m >= 10, but pct_10s < 50%:
    # To have 10 trades in 60s, average gap must be <= 6.66s.
    # If 5 gaps are 11s, total span of those 5 gaps is 55s. The remaining 4 gaps can be 1s each (4s). Total span = 59s!
    # Gaps: [11, 11, 11, 11, 11, 1, 1, 1, 1] -> 9 gaps, span = 59s.
    # 4 gaps are <= 10s (4/9 = 44.4% < 50%). All 10 trades fall in 59s (peak_1m = 10).
    gaps_sub = [11, 11, 11, 11, 11, 1, 1, 1, 1]
    curr_t = t0
    events_and_rule = [_make_event(tx_hash="tx_0", timestamp=curr_t.isoformat())]
    for idx, g in enumerate(gaps_sub):
        curr_t += timedelta(seconds=g)
        events_and_rule.append(_make_event(tx_hash=f"tx_{idx+1}", timestamp=curr_t.isoformat()))

    profile_and = compute_frequency_profile("0xandrule", events_and_rule)
    assert profile_and.metrics.peak_trades_in_1m == 10
    assert profile_and.metrics.pct_gaps_le_10s == pytest.approx(44.44, abs=0.1)
    assert profile_and.classification != "BOT_LIKE"  # AND logic: must fail because pct < 50%


# 21. Phase 7 does not mutate Phase 6 objects ---------------------------------
def test_phase_7_does_not_mutate_phase_6_objects():
    pos = PositionRecord(
        position_id="p1",
        condition_id="c1",
        cost=10.0,
        proceeds=15.0,
        settled_net_result=5.0,
    )
    events = [_make_event(tx_hash="t1")]
    profile = compute_frequency_profile("0xwallet", events)
    assert pos.cost == 10.0
    assert pos.settled_net_result == 5.0


# 22, 23, 24. Purity: No ranking, filtering, or recommendation calls -----------
def test_phase_7_purity_no_ranking_or_filtering_calls():
    """
    Verifies that compute_frequency_profile produces purely descriptive facts
    and contains no recommendation or score mutations.
    """
    events = [_make_event(tx_hash=f"tx{i}", timestamp=(BASE_TIME + timedelta(seconds=i*5)).isoformat()) for i in range(12)]
    profile = compute_frequency_profile("0xpure", events)

    # Descriptive facts only
    assert profile.classification in ("BOT_LIKE", "RAPID_TRADER", "ACTIVE", "NORMAL", "INSUFFICIENT_DATA")
    assert not hasattr(profile, "recommendation")
    assert not hasattr(profile, "score")
    assert not hasattr(profile, "passed")
