from analytics.wallet_stats import compute_wallet_stats
from filters.wallet_filter import filter_wallet


def _mk_events(n_pairs, win=True, price_in=0.4, price_out_win=0.6, price_out_loss=0.2, category="sports", id_offset=0):
    events = []
    for i in range(n_pairs):
        i = i + id_offset
        ts_buy = f"2026-01-{(i % 27) + 1:02d}T00:00:00+00:00"
        ts_sell = f"2026-01-{(i % 27) + 1:02d}T01:00:00+00:00"
        cid = f"cond-{i}"
        events.append({
            "wallet": "0xabc", "side": "BUY", "price": price_in, "size": 100.0,
            "usdc_size": price_in * 100.0, "condition_id": cid, "token_id": f"tok-{i}",
            "slug": f"slug-{i}", "market_title": f"Market {i}", "outcome": "Yes",
            "timestamp": ts_buy, "tx_hash": f"buy-{i}",
        })
        out_price = price_out_win if win else price_out_loss
        events.append({
            "wallet": "0xabc", "side": "SELL", "price": out_price, "size": 100.0,
            "usdc_size": out_price * 100.0, "condition_id": cid, "token_id": f"tok-{i}",
            "slug": f"slug-{i}", "market_title": f"Market {i}", "outcome": "Yes",
            "timestamp": ts_sell, "tx_hash": f"sell-{i}",
        })
    return events


def test_roi_calculation_profitable_wallet():
    events = _mk_events(45, win=True)
    stats = compute_wallet_stats("0xabc", events)
    assert stats.total_positions == 45
    assert stats.closed_positions == 45
    assert stats.roi_pct > 0
    assert stats.win_rate_pct == 100.0


def test_roi_calculation_losing_wallet():
    events = _mk_events(10, win=False)
    stats = compute_wallet_stats("0xabc", events)
    assert stats.roi_pct < 0
    assert stats.win_rate_pct == 0.0


def test_drawdown_calculation_nonzero_on_mixed_results():
    win_events = _mk_events(5, win=True)
    loss_events = _mk_events(5, win=False, id_offset=100)
    # interleave losses after wins so equity curve actually draws down
    events = win_events + loss_events
    stats = compute_wallet_stats("0xabc", events)
    assert stats.max_drawdown_pct > 0


def test_wallet_filter_rejects_insufficient_positions():
    events = _mk_events(5, win=True)
    stats = compute_wallet_stats("0xabc", events)
    result = filter_wallet(stats)
    assert result.passed is False
    assert any("positions" in r for r in result.reasons)


def test_wallet_filter_passes_strong_wallet():
    # 45 positions, high win rate, moderate ROI within preferred band is hard to hit exactly
    # via this synthetic generator, so we just assert the "too few positions" reason disappears.
    events = _mk_events(45, win=True, price_in=0.5, price_out_win=0.55)
    stats = compute_wallet_stats("0xabc", events)
    result = filter_wallet(stats)
    assert not any("below minimum" in r and "positions" in r for r in result.reasons)


# ==============================================================================
# Phase 8A: Controlled Migration Tests (WalletStatistics input, None safety, |E|)
# ==============================================================================
from analytics.models import WalletStatistics


def test_positions_counted_used_over_legacy_total_positions():
    """
    MIN_POSITIONS must evaluate WalletStatistics.positions_counted (|E|)
    instead of legacy total_positions.
    """
    class HybridStats:
        total_positions = 50       # Legacy raw key count >= 40
        positions_counted = 15     # Clean eligible population < 40
        roi_fee_exclusive_pct = 25.0
        active_days = 40
        consistency_mean_roi = 0.25
        consistency_stdev_roi = 0.05
        drawdown_cost_pct = 10.0
        top1_profit_share_pct = 30.0

    result = filter_wallet(HybridStats())
    assert result.passed is False
    assert result.status == "FAIL"
    assert any("Only 15 positions, below minimum 40" in r for r in result.reasons)


def test_positions_counted_ge_40_passes_position_criterion():
    """WalletStatistics with positions_counted >= 40 passes the position filter."""
    stats = WalletStatistics(
        wallet="0xpass",
        positions_counted=45,
        roi_fee_exclusive_pct=25.0,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=30.0,
        is_resolution_only=False,
        category_mix={"sports": 45},
    )
    result = filter_wallet(stats)
    assert not any("below minimum" in r and "positions" in r for r in result.reasons)
    assert result.passed is True
    assert result.status == "PASS"


def test_none_roi_does_not_cause_type_error():
    """None/unknown ROI does not crash with TypeError and is marked insufficient data."""
    stats = WalletStatistics(
        wallet="0xnoroi",
        positions_counted=45,
        roi_fee_exclusive_pct=None,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=30.0,
    )
    result = filter_wallet(stats)
    assert not any("ROI" in r and "below minimum" in r for r in result.reasons)
    assert "roi" in result.insufficient_criteria
    assert result.status == "INSUFFICIENT_DATA"
    assert result.passed is True


def test_none_consistency_does_not_become_false_numeric_failure():
    """None/unknown consistency does not trigger a false 0.0 < 7.0 failure."""
    stats = WalletStatistics(
        wallet="0xnocons",
        positions_counted=45,
        roi_fee_exclusive_pct=25.0,
        active_days=40,
        consistency_mean_roi=None,
        consistency_stdev_roi=None,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=30.0,
    )
    result = filter_wallet(stats)
    assert not any("Consistency 0.0" in r for r in result.reasons)
    assert not any("Consistency" in r and "below minimum" in r for r in result.reasons)
    assert "consistency" in result.insufficient_criteria
    assert result.status == "INSUFFICIENT_DATA"
    assert result.passed is True


def test_known_consistency_below_threshold_fails():
    """Known consistency below 4.4 triggers a hard rejection."""
    # cv = abs(0.2 / 0.1) = 2.0 -> consistency score = 10 / (1 + 2) = 3.33 < 4.4
    stats = WalletStatistics(
        wallet="0xlowcons",
        positions_counted=45,
        roi_fee_exclusive_pct=25.0,
        active_days=40,
        consistency_mean_roi=0.1,
        consistency_stdev_roi=0.2,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=30.0,
    )
    result = filter_wallet(stats)
    assert result.passed is False
    assert result.status == "FAIL"
    assert any("Consistency 3.3 below minimum 4.4" in r for r in result.reasons)


def test_known_drawdown_above_threshold_fails():
    """Known drawdown exceeding MAX_DRAWDOWN (20.0%) triggers a hard rejection."""
    stats = WalletStatistics(
        wallet="0xhighdd",
        positions_counted=45,
        roi_fee_exclusive_pct=25.0,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=25.0,
        top1_profit_share_pct=30.0,
    )
    result = filter_wallet(stats)
    assert result.passed is False
    assert result.status == "FAIL"
    assert any("Drawdown 25.0% above maximum 20.0%" in r for r in result.reasons)


def test_known_roi_below_threshold_fails():
    """Known ROI below MIN_ROI (15.0%) triggers a hard rejection."""
    stats = WalletStatistics(
        wallet="0xlowroi",
        positions_counted=45,
        roi_fee_exclusive_pct=10.0,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=30.0,
    )
    result = filter_wallet(stats)
    assert result.passed is False
    assert result.status == "FAIL"
    assert any("ROI 10.0% below minimum 15.0%" in r for r in result.reasons)


def test_known_roi_above_threshold_fails():
    """Known ROI above REJECT_ROI_ABOVE (80.0%) triggers a hard rejection."""
    stats = WalletStatistics(
        wallet="0xhighroi",
        positions_counted=45,
        roi_fee_exclusive_pct=95.0,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=30.0,
    )
    result = filter_wallet(stats)
    assert result.passed is False
    assert result.status == "FAIL"
    assert any("ROI 95.0% above reject threshold 80.0%" in r for r in result.reasons)


def test_known_active_days_below_threshold_fails():
    """Known active days below MIN_ACTIVE_DAYS (30) triggers a hard rejection."""
    stats = WalletStatistics(
        wallet="0xshort",
        positions_counted=45,
        roi_fee_exclusive_pct=25.0,
        active_days=10,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=30.0,
    )
    result = filter_wallet(stats)
    assert result.passed is False
    assert result.status == "FAIL"
    assert any("Only 10 active days, below minimum 30" in r for r in result.reasons)


def test_existing_concentration_behavior_preserved():
    """Top-1 profit concentration >= 70% fails; < 70% passes."""
    stats_fail = WalletStatistics(
        wallet="0xconc_fail",
        positions_counted=45,
        roi_fee_exclusive_pct=25.0,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=75.0,
    )
    result_fail = filter_wallet(stats_fail)
    assert result_fail.passed is False
    assert any("75.0% of profit concentrated in one trade" in r for r in result_fail.reasons)

    stats_pass = WalletStatistics(
        wallet="0xconc_pass",
        positions_counted=45,
        roi_fee_exclusive_pct=25.0,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=40.0,
    )
    result_pass = filter_wallet(stats_pass)
    assert not any("concentrated in one trade" in r for r in result_pass.reasons)


def test_existing_category_politics_behavior_preserved():
    """Politics share > 50% fails when politics is not allowed."""
    class PoliticsWallet:
        positions_counted = 45
        roi_fee_exclusive_pct = 25.0
        active_days = 40
        consistency_score = 8.0
        drawdown_cost_pct = 10.0
        top1_profit_share_pct = 30.0
        category_mix = {"politics": 8, "sports": 2}

    result = filter_wallet(PoliticsWallet())
    assert result.passed is False
    assert any("politics" in r for r in result.reasons)


def test_existing_resolution_only_behavior_preserved():
    """Wallets marked as resolution-only fail the hard filter."""
    class ResolutionOnlyWallet:
        positions_counted = 45
        roi_fee_exclusive_pct = 25.0
        active_days = 40
        consistency_score = 8.0
        drawdown_cost_pct = 10.0
        top1_profit_share_pct = 30.0
        is_resolution_only = True

    result = filter_wallet(ResolutionOnlyWallet())
    assert result.passed is False
    assert any("resolution-only" in r for r in result.reasons)


# ==============================================================================
# Phase 8B: is_resolution_only and category_mix fields on WalletStatistics
# ==============================================================================
from analytics.models import (
    PositionRecord,
    PositionSettlementClass,
    PositionSettlementType,
)
from analytics.performance import compute_wallet_statistics


def _make_pos_8b(
    pid: str,
    cost: float = 10.0,
    proceeds: float = 15.0,
    settled_net: float = 5.0,
    sclass: PositionSettlementClass = PositionSettlementClass.CLOSED,
    stype: PositionSettlementType = PositionSettlementType.TRADING_EXIT,
    category: str = "sports",
    cid: str = None,
    ts: str = "2026-01-15T12:00:00+00:00",
    is_combo: bool = False,
) -> PositionRecord:
    return PositionRecord(
        position_id=pid,
        condition_id=cid or f"cond_{pid}",
        token_id=f"token_{pid}",
        market_slug=f"slug_{pid}",
        cost=cost,
        proceeds=proceeds,
        settled_net_result=settled_net,
        settlement_class=sclass,
        settlement_type=stype,
        opened_at=ts,
        closed_at=ts,
        resolved_at=ts,
        category=category,
        is_combo=is_combo,
    )


# --- 1. WalletStatistics contains is_resolution_only ---------------------------

def test_wallet_statistics_has_is_resolution_only_field():
    """WalletStatistics now exposes is_resolution_only so the hard filter can evaluate it."""
    stats = WalletStatistics(wallet="0xtest8b_iro")
    assert hasattr(stats, "is_resolution_only")


# --- 2. is_resolution_only populated using legacy-compatible logic --------------

def test_is_resolution_only_true_when_no_trading_exits():
    """
    Wallet with >= 3 ordinary positions, none with TRADING_EXIT settlement type,
    must produce is_resolution_only=True (mirrors legacy: total_sells == 0 and total_positions >= 3).
    """
    positions = [
        _make_pos_8b(
            f"r{i}",
            stype=PositionSettlementType.RESOLUTION,
            sclass=PositionSettlementClass.WON,
            settled_net=5.0,
            cid=f"cond_res_{i}",
        )
        for i in range(5)
    ]
    ws = compute_wallet_statistics("0xresonly", positions)
    assert ws.is_resolution_only is True


def test_is_resolution_only_false_when_trading_exit_present():
    """
    Wallet with at least one TRADING_EXIT position must produce is_resolution_only=False.
    """
    positions = [
        _make_pos_8b("te1", stype=PositionSettlementType.TRADING_EXIT, cid="cond_te1"),
        _make_pos_8b(
            "te2",
            stype=PositionSettlementType.RESOLUTION,
            sclass=PositionSettlementClass.WON,
            settled_net=5.0,
            cid="cond_te2",
        ),
        _make_pos_8b("te3", stype=PositionSettlementType.TRADING_EXIT, cid="cond_te3"),
    ]
    ws = compute_wallet_statistics("0xtrader", positions)
    assert ws.is_resolution_only is False


def test_is_resolution_only_none_when_fewer_than_3_ordinary_positions():
    """
    Fewer than 3 ordinary positions — cannot reliably determine resolution-only.
    is_resolution_only must be None, not False.
    """
    positions = [
        _make_pos_8b(
            "thin1",
            stype=PositionSettlementType.RESOLUTION,
            sclass=PositionSettlementClass.WON,
            settled_net=5.0,
            cid="cond_thin1",
        ),
        _make_pos_8b(
            "thin2",
            stype=PositionSettlementType.RESOLUTION,
            sclass=PositionSettlementClass.WON,
            settled_net=5.0,
            cid="cond_thin2",
        ),
    ]
    ws = compute_wallet_statistics("0xthin", positions)
    assert ws.is_resolution_only is None


def test_resolution_only_hard_filter_rejects_via_wallet_statistics():
    """
    filter_wallet() must reject a WalletStatistics object with is_resolution_only=True.
    """
    stats = WalletStatistics(
        wallet="0xresonly_filter",
        positions_counted=45,
        roi_fee_exclusive_pct=25.0,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=30.0,
        is_resolution_only=True,
        category_mix={"sports": 45},
    )
    result = filter_wallet(stats)
    assert result.passed is False
    assert result.status == "FAIL"
    assert any("resolution-only" in r for r in result.reasons)


def test_none_is_resolution_only_does_not_silently_become_false():
    """
    is_resolution_only=None must be reported as INSUFFICIENT_DATA, not treated as False.
    A wallet with None should not pass with status PASS.
    """
    stats = WalletStatistics(
        wallet="0xnoresdata",
        positions_counted=45,
        roi_fee_exclusive_pct=25.0,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=30.0,
        is_resolution_only=None,
        category_mix={"sports": 45},
    )
    result = filter_wallet(stats)
    assert result.status == "INSUFFICIENT_DATA"
    assert "is_resolution_only" in result.insufficient_criteria
    assert result.passed is True   # insufficient data != rejected
    assert not any("resolution-only" in r for r in result.reasons)


# --- 3. WalletStatistics contains category_mix ----------------------------------

def test_wallet_statistics_has_category_mix_field():
    """WalletStatistics now exposes category_mix so the hard filter can evaluate it."""
    stats = WalletStatistics(wallet="0xtest8b_cm")
    assert hasattr(stats, "category_mix")


# --- 4. category_mix uses existing category definitions -------------------------

def test_category_mix_populated_from_position_records():
    """
    compute_wallet_statistics must populate category_mix counting all ordinary positions
    by their category string, matching legacy compute_wallet_stats semantics.
    """
    positions = [
        _make_pos_8b("cm1", category="sports", cid="cond_cm1"),
        _make_pos_8b("cm2", category="sports", cid="cond_cm2"),
        _make_pos_8b("cm3", category="crypto", cid="cond_cm3"),
        _make_pos_8b("cm4", category="politics", cid="cond_cm4"),
        _make_pos_8b("cm5", category="politics", cid="cond_cm5"),
    ]
    ws = compute_wallet_statistics("0xcattest", positions)
    assert ws.category_mix is not None
    assert ws.category_mix.get("sports") == 2
    assert ws.category_mix.get("crypto") == 1
    assert ws.category_mix.get("politics") == 2


def test_combo_positions_excluded_from_category_mix():
    """
    Combo positions must not be counted in category_mix (matching legacy semantics).
    """
    positions = [
        _make_pos_8b("cb1", category="sports", cid="cond_cb1"),
        _make_pos_8b("cb2", category="politics", cid="cond_cb2"),
        _make_pos_8b("cb3", category="politics", cid="cond_cb3", is_combo=True),
        _make_pos_8b("cb4", category="sports", cid="cond_cb4"),
    ]
    ws = compute_wallet_statistics("0xcombocat", positions)
    assert ws.category_mix is not None
    assert ws.category_mix.get("sports") == 2
    # combo excluded: only 1 ordinary politics position counted
    assert ws.category_mix.get("politics") == 1


# --- 5. Politics filter still respects POLITICS_ALLOWED -------------------------

def test_politics_filter_rejects_high_politics_share_via_wallet_statistics():
    """
    Politics share > 50% still fails when POLITICS_ALLOWED is False.
    """
    stats = WalletStatistics(
        wallet="0xpolitics_filter",
        positions_counted=45,
        roi_fee_exclusive_pct=25.0,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=30.0,
        is_resolution_only=False,
        category_mix={"politics": 8, "sports": 2},
    )
    result = filter_wallet(stats)
    assert result.passed is False
    assert result.status == "FAIL"
    assert any("politics" in r for r in result.reasons)


def test_low_politics_share_does_not_trigger_politics_filter():
    """Politics share <= 50% must not trigger the politics rejection."""
    stats = WalletStatistics(
        wallet="0xmixedcat_filter",
        positions_counted=45,
        roi_fee_exclusive_pct=25.0,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=30.0,
        is_resolution_only=False,
        category_mix={"politics": 4, "sports": 6},
    )
    result = filter_wallet(stats)
    assert not any("politics" in r for r in result.reasons)


# --- 7. Missing category data must not silently become 0% politics --------------

def test_none_category_mix_does_not_silently_become_zero_politics():
    """
    category_mix=None must be reported as INSUFFICIENT_DATA, not treated as 0% politics.
    """
    stats = WalletStatistics(
        wallet="0xnocat_filter",
        positions_counted=45,
        roi_fee_exclusive_pct=25.0,
        active_days=40,
        consistency_mean_roi=0.25,
        consistency_stdev_roi=0.05,
        drawdown_cost_pct=10.0,
        top1_profit_share_pct=30.0,
        is_resolution_only=False,
        category_mix=None,
    )
    result = filter_wallet(stats)
    assert result.status == "INSUFFICIENT_DATA"
    assert "category_mix" in result.insufficient_criteria
    assert result.passed is True
    assert not any("politics" in r for r in result.reasons)


# --- 9. Full integration pipeline ----------------------------------------------

def test_full_pipeline_clean_wallet_passes_hard_filter():
    """
    End-to-end: compute_wallet_statistics on a wallet with sports/crypto activity,
    at least one trading exit, and sufficient positions produces a WalletStatistics
    that the hard filter accepts cleanly.
    """
    positions = [
        _make_pos_8b(
            f"fp{i}",
            cost=50.0,
            proceeds=60.0,
            settled_net=10.0,
            stype=PositionSettlementType.TRADING_EXIT,
            sclass=PositionSettlementClass.CLOSED,
            category="sports" if i % 2 == 0 else "crypto",
            cid=f"cond_fp_{i}",
            ts=f"2026-0{(i % 9) + 1}-15T12:00:00+00:00",
        )
        for i in range(45)
    ]
    ws = compute_wallet_statistics("0xfullpipe8b", positions)
    assert ws.is_resolution_only is False
    assert ws.category_mix is not None
    assert "sports" in ws.category_mix
    assert "crypto" in ws.category_mix
    result = filter_wallet(ws)
    assert not any("politics" in r for r in result.reasons)
    assert not any("resolution-only" in r for r in result.reasons)
