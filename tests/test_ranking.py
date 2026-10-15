"""
Tests for the recalibrated scoring in wallet/ranking.py::score_wallet
(this pass). Covers: strong/medium/weak wallets landing in the right
tier, BOT_LIKE always REJECT regardless of score, RAPID_TRADER's penalty,
REJECT_ROI_ABOVE's hard cap, sample-size/drawdown/concentration penalties
still working, and missing/weak consistency being treated as uncertainty
rather than an automatic disqualifier.
"""
from datetime import datetime, timedelta, timezone

from analytics.wallet_stats import compute_wallet_stats, enrich_with_closed_positions
from analytics.frequency import compute_frequency_stats
from wallet.ranking import score_wallet
from config.settings import settings

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_wallet(
    wallet, n, win_pnl=6.0, loss_pnl=-3.0, win_every=5, day_step=4,
    entry_price=0.4, exit_price=0.52, size=50,
):
    """Builds n well-spaced round trips (no bursty/bot-like timing) with
    authoritative closed-position data, so data_quality == FULL."""
    events, closed = [], []
    for i in range(n):
        day = i * day_step
        t = BASE + timedelta(days=day)
        exit_t = t + timedelta(hours=12)
        events.append({
            "wallet": wallet, "side": "BUY", "price": entry_price, "size": size,
            "usdc_size": entry_price * size, "condition_id": f"c{i}", "token_id": f"t{i}",
            "slug": f"s{i}", "market_title": f"M{i}", "outcome": "Yes",
            "timestamp": t.isoformat(), "tx_hash": f"buy-{i}",
        })
        events.append({
            "wallet": wallet, "side": "SELL", "price": exit_price, "size": size,
            "usdc_size": exit_price * size, "condition_id": f"c{i}", "token_id": f"t{i}",
            "slug": f"s{i}", "market_title": f"M{i}", "outcome": "Yes",
            "timestamp": exit_t.isoformat(), "tx_hash": f"sell-{i}",
        })
        pnl = loss_pnl if (win_every and i % win_every == 0) else win_pnl
        closed.append({
            "proxyWallet": wallet, "asset": f"t{i}", "conditionId": f"c{i}", "avgPrice": entry_price,
            "totalBought": size, "realizedPnl": pnl, "curPrice": exit_price,
            "timestamp": int(exit_t.timestamp()), "title": f"M{i}", "slug": f"s{i}", "outcome": "Yes",
        })
    return events, closed


def _score(wallet, n, **kwargs):
    events, closed = _make_wallet(wallet, n, **kwargs)
    stats = compute_wallet_stats(wallet, events)
    stats = enrich_with_closed_positions(stats, closed, api_ok=True)
    freq = compute_frequency_stats(wallet, events)
    return score_wallet(stats, freq), stats, freq


# 1. Strong wallet + acceptable frequency -> capable of RECOMMENDED --------
def test_strong_wallet_reaches_recommended():
    card, stats, freq = _score("0xstrong", 45, win_pnl=6.0, loss_pnl=-3.0, win_every=5)
    assert stats.data_quality == "FULL"
    assert freq.classification == "NORMAL"
    assert card.recommendation == "RECOMMENDED"
    assert card.final_score >= 75


# 2. Medium wallet -> WATCH --------------------------------------------------
def test_medium_wallet_lands_in_watch():
    # Decent but unremarkable: ROI just above minimum (not in the preferred
    # band), 50% win rate, below the position-count minimum -- good enough
    # to watch, not yet strong enough to recommend outright.
    card, stats, freq = _score("0xmedium", 20, win_pnl=10.0, loss_pnl=-3.2, win_every=2)
    assert 55 <= card.final_score < 75
    assert card.recommendation == "WATCH"


# 3. Weak wallet -> REJECT ----------------------------------------------------
def test_weak_wallet_is_rejected():
    # Losing wallet: mostly losses, tiny sample.
    card, stats, freq = _score("0xweak", 6, win_pnl=1.0, loss_pnl=-5.0, win_every=1)
    assert stats.roi_pct < settings.MIN_ROI
    assert card.recommendation == "REJECT"


# 4. BOT_LIKE wallet -> always REJECT regardless of score --------------------
def test_bot_like_wallet_always_rejected_even_with_great_stats():
    events, closed = [], []
    for i in range(40):
        t_buy = BASE + timedelta(seconds=i * 6)
        t_sell = t_buy + timedelta(seconds=3)
        events.append({
            "wallet": "0xbotwin", "side": "BUY", "price": 0.40, "size": 100.0, "usdc_size": 40.0,
            "condition_id": f"bc{i}", "token_id": f"bt{i}", "slug": f"bs{i}", "market_title": f"M{i}",
            "outcome": "Yes", "timestamp": t_buy.isoformat(), "tx_hash": f"bb{i}",
        })
        events.append({
            "wallet": "0xbotwin", "side": "SELL", "price": 0.69, "size": 100.0, "usdc_size": 69.0,
            "condition_id": f"bc{i}", "token_id": f"bt{i}", "slug": f"bs{i}", "market_title": f"M{i}",
            "outcome": "Yes", "timestamp": t_sell.isoformat(), "tx_hash": f"bs{i}",
        })
        closed.append({
            "proxyWallet": "0xbotwin", "asset": f"bt{i}", "conditionId": f"bc{i}", "avgPrice": 0.40,
            "totalBought": 100.0, "realizedPnl": 29.0, "curPrice": 0.69,
            "timestamp": int(t_sell.timestamp()), "title": f"M{i}", "slug": f"bs{i}", "outcome": "Yes",
        })
    stats = compute_wallet_stats("0xbotwin", events)
    stats = enrich_with_closed_positions(stats, closed, api_ok=True)
    freq = compute_frequency_stats("0xbotwin", events)
    card = score_wallet(stats, freq)
    assert freq.classification == "BOT_LIKE"
    assert stats.roi_pct > 50 and stats.win_rate_pct == 100.0
    assert card.recommendation == "REJECT"


# 5. RAPID_TRADER receives its penalty ---------------------------------------
def test_rapid_trader_receives_score_penalty():
    events, closed = _make_wallet("0xrapid", 30, day_step=4)
    stats = compute_wallet_stats("0xrapid", events)
    stats = enrich_with_closed_positions(stats, closed, api_ok=True)
    card_no_penalty = score_wallet(stats, None)

    # Same stats, but a RAPID_TRADER frequency classification attached.
    from analytics.frequency import FrequencyStats
    rapid_freq = FrequencyStats(wallet="0xrapid", classification="RAPID_TRADER", insufficient_data=False)
    card_with_penalty = score_wallet(stats, rapid_freq)

    assert card_with_penalty.final_score < card_no_penalty.final_score
    assert any("RAPID_TRADER" in r for r in card_with_penalty.reasons)


# 6. ROI above REJECT_ROI_ABOVE remains rejected ------------------------------
def test_extreme_roi_above_reject_threshold_stays_rejected():
    card, stats, freq = _score(
        "0xextreme", 45, win_pnl=100.0, loss_pnl=-1.0, win_every=100,  # never lose, huge wins
    )
    assert stats.roi_pct > settings.REJECT_ROI_ABOVE
    assert card.recommendation != "RECOMMENDED"
    assert any("exceeds reject threshold" in r for r in card.reasons)


# 7. Insufficient sample size is still penalized ------------------------------
def test_insufficient_sample_size_still_penalized():
    card_small, _, _ = _score("0xsmall", 5, win_pnl=6.0, loss_pnl=-3.0, win_every=5)
    card_large, _, _ = _score("0xlarge", 45, win_pnl=6.0, loss_pnl=-3.0, win_every=5)
    assert card_small.final_score < card_large.final_score
    assert any("below the minimum sample size" in r for r in card_small.reasons)


# 8. High drawdown is still penalized -----------------------------------------
def test_high_drawdown_still_penalized():
    # Front-load losses so the equity curve genuinely draws down hard.
    events, closed = [], []
    for i in range(20):
        day = i * 4
        t = BASE + timedelta(days=day)
        exit_t = t + timedelta(hours=12)
        events.append({
            "wallet": "0xdrawdown", "side": "BUY", "price": 0.4, "size": 50, "usdc_size": 20.0,
            "condition_id": f"c{i}", "token_id": f"t{i}", "slug": f"s{i}", "market_title": f"M{i}",
            "outcome": "Yes", "timestamp": t.isoformat(), "tx_hash": f"buy-{i}",
        })
        events.append({
            "wallet": "0xdrawdown", "side": "SELL", "price": 0.5, "size": 50, "usdc_size": 25.0,
            "condition_id": f"c{i}", "token_id": f"t{i}", "slug": f"s{i}", "market_title": f"M{i}",
            "outcome": "Yes", "timestamp": exit_t.isoformat(), "tx_hash": f"sell-{i}",
        })
        pnl = -10.0 if i < 10 else 9.0  # 10 losses up front, then wins -- deep drawdown
        closed.append({
            "proxyWallet": "0xdrawdown", "asset": f"t{i}", "conditionId": f"c{i}", "avgPrice": 0.4,
            "totalBought": 50, "realizedPnl": pnl, "curPrice": 0.5,
            "timestamp": int(exit_t.timestamp()), "title": f"M{i}", "slug": f"s{i}", "outcome": "Yes",
        })
    stats = compute_wallet_stats("0xdrawdown", events)
    stats = enrich_with_closed_positions(stats, closed, api_ok=True)
    card = score_wallet(stats, None)
    assert stats.max_drawdown_pct > settings.MAX_DRAWDOWN
    assert any("exceeds the" in r and "limit" in r for r in card.reasons if "drawdown" in r.lower())


# 9. High concentration is still penalized -------------------------------------
def test_high_concentration_still_penalized():
    events, closed = [], []
    # one huge win, several small losses -> concentrated profit
    t = BASE
    exit_t = t + timedelta(hours=12)
    events.append({
        "wallet": "0xconc", "side": "BUY", "price": 0.1, "size": 1000, "usdc_size": 100.0,
        "condition_id": "big", "token_id": "bigt", "slug": "bigs", "market_title": "Big",
        "outcome": "Yes", "timestamp": t.isoformat(), "tx_hash": "big-buy",
    })
    events.append({
        "wallet": "0xconc", "side": "SELL", "price": 0.9, "size": 1000, "usdc_size": 900.0,
        "condition_id": "big", "token_id": "bigt", "slug": "bigs", "market_title": "Big",
        "outcome": "Yes", "timestamp": exit_t.isoformat(), "tx_hash": "big-sell",
    })
    closed.append({
        "proxyWallet": "0xconc", "asset": "bigt", "conditionId": "big", "avgPrice": 0.1,
        "totalBought": 1000, "realizedPnl": 800.0, "curPrice": 0.9,
        "timestamp": int(exit_t.timestamp()), "title": "Big", "slug": "bigs", "outcome": "Yes",
    })
    for i in range(10):
        day = i * 4 + 5
        t2 = BASE + timedelta(days=day)
        exit_t2 = t2 + timedelta(hours=6)
        events.append({
            "wallet": "0xconc", "side": "BUY", "price": 0.5, "size": 10, "usdc_size": 5.0,
            "condition_id": f"sm{i}", "token_id": f"smt{i}", "slug": f"sms{i}", "market_title": f"Small{i}",
            "outcome": "Yes", "timestamp": t2.isoformat(), "tx_hash": f"sm-buy-{i}",
        })
        events.append({
            "wallet": "0xconc", "side": "SELL", "price": 0.4, "size": 10, "usdc_size": 4.0,
            "condition_id": f"sm{i}", "token_id": f"smt{i}", "slug": f"sms{i}", "market_title": f"Small{i}",
            "outcome": "Yes", "timestamp": exit_t2.isoformat(), "tx_hash": f"sm-sell-{i}",
        })
        closed.append({
            "proxyWallet": "0xconc", "asset": f"smt{i}", "conditionId": f"sm{i}", "avgPrice": 0.5,
            "totalBought": 10, "realizedPnl": -1.0, "curPrice": 0.4,
            "timestamp": int(exit_t2.timestamp()), "title": f"Small{i}", "slug": f"sms{i}", "outcome": "Yes",
        })
    stats = compute_wallet_stats("0xconc", events)
    stats = enrich_with_closed_positions(stats, closed, api_ok=True)
    card = score_wallet(stats, None)
    assert stats.concentration_pct >= 50
    assert any("single trade" in r for r in card.reasons)


# 10. Missing/weak consistency doesn't block an otherwise-strong wallet -------
def test_missing_consistency_data_does_not_block_recommendation():
    # Only 2 closed positions (below the >=3 needed to compute a real
    # consistency score) but everything else about the wallet is strong.
    # Must be treated as uncertainty (partial credit), not a hard 0/fail,
    # and must not by itself prevent a well-scoring wallet from evaluation.
    events, closed = _make_wallet("0xthin", 45, win_pnl=6.0, loss_pnl=-3.0, win_every=5)
    stats = compute_wallet_stats("0xthin", events)
    # Enrich with only 2 closed positions worth of authoritative data,
    # simulating a case where consistency can't be reliably computed.
    stats = enrich_with_closed_positions(stats, closed[:2], api_ok=True)
    freq = compute_frequency_stats("0xthin", events)
    card = score_wallet(stats, freq)
    assert len(stats.per_trade_pnls) < 3
    assert any("uncertainty" in r.lower() for r in card.reasons)
    # consistency being unscoreable must not have zeroed out the whole card
    assert card.final_score > 50


# 11. Existing frequency tests continue passing -- covered by the full
# regression suite (tests/test_frequency.py is untouched by this change).


# ----------------------------------------------------------------------
# Phase 8C audit lock: filters/wallet_filter.py::filter_wallet() IS now
# intentionally called from cmd_scan_wallets (Phase 8C scanner integration).
# The hard filter acts as a gate BEFORE ranking -- FAIL wallets are excluded
# from ranking and cannot receive RECOMMENDED/WATCH recommendations.
# INSUFFICIENT_DATA wallets are tracked separately and also excluded from
# normal ranking candidates.
# This test locks that the wiring is present and cannot be silently removed.
# ----------------------------------------------------------------------
def test_filter_wallet_is_invoked_by_scan_pipeline():
    import inspect
    import main as main_module

    source = inspect.getsource(main_module.cmd_scan_wallets)
    assert "filter_wallet(" in source, (
        "filter_wallet() must be called from cmd_scan_wallets (Phase 8C). "
        "If this wiring was intentionally removed, update this test and the "
        "Phase 8C audit notes accordingly."
    )
    # The integration MUST check result.status explicitly, not just result.passed,
    # because INSUFFICIENT_DATA returns passed=True.
    assert "result.status" in source or 'filter_result.status' in source, (
        "cmd_scan_wallets must inspect filter result.status, not just result.passed, "
        "because INSUFFICIENT_DATA returns passed=True."
    )
    # The integration MUST NOT promote INSUFFICIENT_DATA wallets as normal PASS.
    assert "INSUFFICIENT_DATA" in source, (
        "cmd_scan_wallets must explicitly handle the INSUFFICIENT_DATA filter status."
    )
