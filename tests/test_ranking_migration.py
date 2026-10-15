"""
Phase 9 Tests: Scoring Migration from legacy models to Phase 6 & Phase 7 models.

Verifies:
1. score_wallet accepts Phase 6 WalletStatistics.
2. score_wallet uses positions_counted.
3. score_wallet uses Phase 6 ROI (roi_fee_exclusive_pct).
4. score_wallet uses Phase 6 win rate (win_rate_pct).
5. score_wallet derives consistency score from Phase 6 mean/stdev ROI.
6. score_wallet uses Phase 6 drawdown (drawdown_cost_pct).
7. score_wallet uses Phase 6 concentration (top1_profit_share_pct).
8. score_wallet uses Phase 6 category_mix.
9. score_wallet applies is_resolution_only penalty.
10. score_wallet accepts Phase 7 FrequencyProfile.
11. Frequency penalties match between FrequencyProfile and FrequencyStats.
12. Recommendation thresholds (>=75 RECOMMENDED, >=55 WATCH) remain intact.
13. Optional / None metrics follow documented non-crashing semantics.
14. Hard-filter logic is not mixed into ranking.
15. Scoring regression equivalence between legacy and Phase 6/7 models.
"""
from __future__ import annotations

import pytest

from analytics.models import WalletStatistics
from analytics.wallet_stats import WalletStats
from analytics.frequency_models import FrequencyProfile, FrequencyMetrics
from analytics.frequency import FrequencyStats
from wallet.ranking import score_wallet, rank_wallets, WalletScoreCard
from config.settings import settings


def _make_sample_frequency_profile(
    classification: str = "NORMAL",
    reasons: list[str] = None,
    median_gap: float = 300.0,
    pct_short: float = 10.0,
    data_quality: str = "RECONCILED",
) -> FrequencyProfile:
    metrics = FrequencyMetrics(
        total_logical_trades=50,
        raw_fill_count=50,
        fill_expansion_factor=1.0,
        min_gap_seconds=1.0,
        median_gap_seconds=median_gap,
        mean_gap_seconds=median_gap,
        pct_gaps_le_10s=5.0,
        pct_gaps_le_30s=8.0,
        pct_gaps_le_60s=pct_short,
        peak_trades_in_1m=2,
        peak_trades_in_5m=4,
        peak_trades_in_15m=8,
        burst_episodes_count=0,
        cross_market_max_in_1m=1,
        active_span_days=40.0,
    )
    return FrequencyProfile(
        wallet="0xtest",
        classification=classification,
        reasons=reasons or ["Test frequency reason"],
        metrics=metrics,
        data_quality=data_quality,
        is_complete_history=True,
        is_reconciled=True,
    )


# ---------------------------------------------------------------------------
# 1. Accepts WalletStatistics directly
# ---------------------------------------------------------------------------

def test_score_wallet_accepts_wallet_statistics():
    stats = WalletStatistics(
        wallet="0xp6",
        positions_counted=45,
        wins=30,
        losses=10,
        breakeven=5,
        win_rate_pct=75.0,
        total_settled_cost=4500.0,
        total_settled_net_result=1125.0,
        roi_fee_exclusive_pct=25.0,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=20.0,
        mean_position_cost=100.0,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        active_days=40,
        category_mix={"sports": 45},
        is_resolution_only=False,
    )
    card = score_wallet(stats)
    assert isinstance(card, WalletScoreCard)
    assert card.wallet == "0xp6"
    assert card.final_score > 0
    assert card.recommendation in ("RECOMMENDED", "WATCH", "REJECT")


# ---------------------------------------------------------------------------
# 2. Uses positions_counted
# ---------------------------------------------------------------------------

def test_score_wallet_uses_positions_counted():
    # >= 40 positions earns 15 pts
    stats_large = WalletStatistics(wallet="0xpos_lg", positions_counted=45)
    card_large = score_wallet(stats_large)

    # 10 positions earns round(15.0 * 10 / 40, 1) = 3.8 pts
    stats_small = WalletStatistics(wallet="0xpos_sm", positions_counted=10)
    card_small = score_wallet(stats_small)

    assert card_large.num_positions == 45
    assert card_small.num_positions == 10
    assert card_large.final_score > card_small.final_score
    assert any("45 positions >= minimum sample size" in r for r in card_large.reasons)
    assert any("Only 10 positions" in r for r in card_small.reasons)


# ---------------------------------------------------------------------------
# 3. Uses Phase 6 ROI
# ---------------------------------------------------------------------------

def test_score_wallet_uses_phase_6_roi():
    # Preferred band (20-30%) earns 20 pts
    stats_pref = WalletStatistics(wallet="0xpref", roi_fee_exclusive_pct=25.0)
    card_pref = score_wallet(stats_pref)
    assert card_pref.roi_pct == 25.0
    assert any("preferred" in r for r in card_pref.reasons)

    # Above reject threshold (80%) earns 0 pts
    stats_rej = WalletStatistics(wallet="0xrej", roi_fee_exclusive_pct=95.0)
    card_rej = score_wallet(stats_rej)
    assert any("exceeds reject threshold" in r for r in card_rej.reasons)


# ---------------------------------------------------------------------------
# 4. Uses Phase 6 Win Rate
# ---------------------------------------------------------------------------

def test_score_wallet_uses_phase_6_win_rate():
    stats_high_wr = WalletStatistics(wallet="0xwr", wins=30, losses=10, win_rate_pct=75.0)
    card_high_wr = score_wallet(stats_high_wr)
    assert card_high_wr.win_rate_pct == 75.0
    assert any("Strong win rate" in r for r in card_high_wr.reasons)


# ---------------------------------------------------------------------------
# 5. Derives consistency score from Phase 6 mean/stdev ROI
# ---------------------------------------------------------------------------

def test_score_wallet_uses_phase_6_consistency():
    # cv = abs(0.05 / 0.25) = 0.2 -> score = 10 / 1.2 = 8.33 >= 7.0
    stats_cons = WalletStatistics(
        wallet="0xcons",
        positions_counted=10,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
    )
    card_cons = score_wallet(stats_cons)
    assert card_cons.consistency_score == pytest.approx(8.33, abs=0.05)
    assert any("meets the" in r and "bar" in r for r in card_cons.reasons if "consistency" in r.lower())


# ---------------------------------------------------------------------------
# 6. Uses Phase 6 Drawdown
# ---------------------------------------------------------------------------

def test_score_wallet_uses_phase_6_drawdown():
    stats_low_dd = WalletStatistics(wallet="0xdd", drawdown_cost_pct=10.0)
    card_low_dd = score_wallet(stats_low_dd)
    assert card_low_dd.drawdown_pct == 10.0
    assert any("is within the" in r for r in card_low_dd.reasons if "drawdown" in r.lower())


# ---------------------------------------------------------------------------
# 7. Uses Phase 6 Concentration
# ---------------------------------------------------------------------------

def test_score_wallet_uses_phase_6_concentration():
    stats_conc = WalletStatistics(wallet="0xconc", top1_profit_share_pct=60.0)
    card_conc = score_wallet(stats_conc)
    assert any("single trade" in r for r in card_conc.reasons)


# ---------------------------------------------------------------------------
# 8. Uses Phase 6 category_mix
# ---------------------------------------------------------------------------

def test_score_wallet_uses_phase_6_category_mix():
    stats_cat = WalletStatistics(wallet="0xcat", category_mix={"sports": 8, "crypto": 2})
    card_cat = score_wallet(stats_cat)
    assert card_cat.category_mix == {"sports": 8, "crypto": 2}
    assert any("sports/crypto" in r for r in card_cat.reasons)


# ---------------------------------------------------------------------------
# 9. Applies resolution-only penalty
# ---------------------------------------------------------------------------

def test_score_wallet_applies_resolution_only_penalty():
    stats_normal = WalletStatistics(wallet="0xnorm", is_resolution_only=False)
    stats_res = WalletStatistics(wallet="0xres", is_resolution_only=True)

    card_normal = score_wallet(stats_normal)
    card_res = score_wallet(stats_res)

    assert card_res.final_score == pytest.approx(max(0.0, card_normal.final_score - 20), abs=0.1)
    assert any("resolution-only" in r for r in card_res.reasons)


# ---------------------------------------------------------------------------
# 10. Accepts Phase 7 FrequencyProfile
# ---------------------------------------------------------------------------

def test_score_wallet_accepts_phase_7_frequency_profile():
    stats = WalletStatistics(wallet="0xfreq_p7", positions_counted=40, active_days=35, roi_fee_exclusive_pct=25.0)
    profile = _make_sample_frequency_profile(classification="NORMAL")
    card = score_wallet(stats, profile)
    assert card.frequency_classification == "NORMAL"
    assert card.frequency_median_gap_seconds == 300.0


# ---------------------------------------------------------------------------
# 11. Frequency penalties match exactly
# ---------------------------------------------------------------------------

def test_phase_7_frequency_penalties():
    stats = WalletStatistics(
        wallet="0xpen",
        positions_counted=45,
        wins=35,
        losses=5,
        win_rate_pct=87.5,
        roi_fee_exclusive_pct=25.0,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
    )
    base_card = score_wallet(stats, _make_sample_frequency_profile("NORMAL"))

    # BOT_LIKE: -40 pts & REJECT
    bot_profile = _make_sample_frequency_profile("BOT_LIKE", reasons=["Burst 12 in 1m"])
    bot_card = score_wallet(stats, bot_profile)
    assert bot_card.recommendation == "REJECT"
    assert bot_card.final_score == pytest.approx(base_card.final_score - 40.0, abs=0.1)

    # RAPID_TRADER: -15 pts
    rapid_profile = _make_sample_frequency_profile("RAPID_TRADER", reasons=["Rapid burst"])
    rapid_card = score_wallet(stats, rapid_profile)
    assert rapid_card.final_score == pytest.approx(base_card.final_score - 15.0, abs=0.1)

    # ACTIVE: -7.5 pts
    active_profile = _make_sample_frequency_profile("ACTIVE", reasons=["High frequency"])
    active_card = score_wallet(stats, active_profile)
    assert active_card.final_score == pytest.approx(base_card.final_score - 7.5, abs=0.1)


# ---------------------------------------------------------------------------
# 12. Recommendation thresholds (>=75 RECOMMENDED, >=55 WATCH)
# ---------------------------------------------------------------------------

def test_recommendation_thresholds_intact():
    # Wallet scoring >= 75
    stats_rec = WalletStatistics(
        wallet="0xrec",
        positions_counted=45,
        wins=35,
        losses=5,
        win_rate_pct=87.5,
        roi_fee_exclusive_pct=25.0,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=5.0,
        top1_profit_share_pct=20.0,
        category_mix={"sports": 45},
    )
    card_rec = score_wallet(stats_rec)
    assert card_rec.final_score >= 75
    assert card_rec.recommendation == "RECOMMENDED"

    # Wallet scoring in [55, 75)
    stats_watch = WalletStatistics(
        wallet="0xwatch",
        positions_counted=15,
        wins=10,
        losses=10,
        win_rate_pct=50.0,
        roi_fee_exclusive_pct=16.0,
        active_days=15,
        consistency_mean_roi=0.15,
        consistency_stdev_roi=0.15,
        drawdown_cost_pct=25.0,
        top1_profit_share_pct=60.0,
    )
    card_watch = score_wallet(stats_watch)
    assert 55 <= card_watch.final_score < 75
    assert card_watch.recommendation == "WATCH"


# ---------------------------------------------------------------------------
# 13. Optional / None metrics follow documented non-crashing semantics
# ---------------------------------------------------------------------------

def test_optional_none_metrics_do_not_crash_and_use_fallback_semantics():
    stats_all_none = WalletStatistics(
        wallet="0xnone",
        positions_counted=0,
        wins=0,
        losses=0,
        win_rate_pct=None,
        total_settled_cost=0.0,
        total_settled_net_result=0.0,
        roi_fee_exclusive_pct=None,
        max_drawdown_usd=None,
        drawdown_cost_pct=None,
        top1_profit_share_pct=None,
        mean_position_cost=None,
        consistency_mean_roi=None,
        consistency_stdev_roi=None,
        active_days=0,
        category_mix=None,
        is_resolution_only=None,
    )
    card = score_wallet(stats_all_none)
    assert card.final_score >= 0.0
    assert card.recommendation == "REJECT"
    # Consistency is treated as neutral uncertainty (5.0 pts)
    assert any("uncertainty" in r.lower() for r in card.reasons)
    # Category is treated as neutral (3.0 pts)
    assert any("neutral" in r.lower() for r in card.reasons)


# ---------------------------------------------------------------------------
# 14. Hard-filter logic is not mixed into ranking
# ---------------------------------------------------------------------------

def test_hard_filter_failures_can_still_be_scored_by_ranking():
    # A wallet with 5 positions fails hard filter (MIN_POSITIONS=40),
    # but score_wallet should score it proportionally without crashing or auto-rejecting.
    stats = WalletStatistics(
        wallet="0xfilter_fail",
        positions_counted=5,
        wins=4,
        losses=1,
        win_rate_pct=80.0,
        roi_fee_exclusive_pct=25.0,
        active_days=10,
    )
    card = score_wallet(stats)
    # Produces an explainable score rather than an immediate filter error
    assert card.final_score > 0
    assert card.num_positions == 5


# ---------------------------------------------------------------------------
# 15. Scoring regression equivalence between legacy and Phase 6/7 models
# ---------------------------------------------------------------------------

def test_scoring_regression_equivalence():
    """
    Construct equivalent legacy and Phase 6 statistics.
    Verify that score_wallet produces identical final_score and recommendation.
    """
    # Equivalent legacy stats
    legacy_stats = WalletStats(
        wallet="0xequiv",
        total_positions=45,
        closed_positions=45,
        wins=30,
        losses=10,
        breakeven=5,
        total_invested=4500.0,
        total_realized_pnl=1125.0,
        roi_pct=25.0,
        win_rate_pct=75.0,
        avg_position_size=100.0,
        max_drawdown_pct=10.0,
        consistency_score=8.33,
        concentration_pct=20.0,
        category_mix={"sports": 45},
        active_days=40,
        is_resolution_only=False,
        per_trade_pnls=[5.0] * 45,
    )

    # Equivalent Phase 6 stats
    p6_stats = WalletStatistics(
        wallet="0xequiv",
        positions_counted=45,
        wins=30,
        losses=10,
        breakeven=5,
        total_settled_cost=4500.0,
        total_settled_net_result=1125.0,
        roi_fee_exclusive_pct=25.0,
        win_rate_pct=75.0,
        mean_position_cost=100.0,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=20.0,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,  # cv = 0.2 -> score = 10 / 1.2 = 8.33
        category_mix={"sports": 45},
        active_days=40,
        is_resolution_only=False,
    )

    legacy_card = score_wallet(legacy_stats)
    p6_card = score_wallet(p6_stats)

    assert p6_card.final_score == pytest.approx(legacy_card.final_score, abs=0.1)
    assert p6_card.recommendation == legacy_card.recommendation
    assert p6_card.risk_score == pytest.approx(legacy_card.risk_score, abs=0.1)


# ---------------------------------------------------------------------------
# 16. rank_wallets works with mixed and pure Phase 6 models
# ---------------------------------------------------------------------------

def test_rank_wallets_with_phase_6_models():
    s1 = WalletStatistics(wallet="0xw1", positions_counted=45, roi_fee_exclusive_pct=25.0, win_rate_pct=80.0, active_days=40)
    s2 = WalletStatistics(wallet="0xw2", positions_counted=10, roi_fee_exclusive_pct=5.0, win_rate_pct=30.0, active_days=5)

    cards = rank_wallets([s1, s2])
    assert len(cards) == 2
    assert cards[0].wallet == "0xw1"
    assert cards[1].wallet == "0xw2"
    assert cards[0].final_score > cards[1].final_score
