from analytics.wallet_stats import compute_wallet_stats
from wallet.verifier import verify_wallet, VerificationStatus


def _mk_events(n_pairs, win=True, price_in=0.4, price_out_win=0.6, price_out_loss=0.2):
    events = []
    for i in range(n_pairs):
        cid = f"cond-{i}"
        events.append({
            "wallet": "0xabc", "side": "BUY", "price": price_in, "size": 100.0,
            "usdc_size": price_in * 100.0, "condition_id": cid, "token_id": f"tok-{i}",
            "slug": f"slug-{i}", "market_title": f"Market {i}", "outcome": "Yes",
            "timestamp": f"2026-01-{(i % 27) + 1:02d}T00:00:00+00:00", "tx_hash": f"buy-{i}",
        })
        out_price = price_out_win if win else price_out_loss
        events.append({
            "wallet": "0xabc", "side": "SELL", "price": out_price, "size": 100.0,
            "usdc_size": out_price * 100.0, "condition_id": cid, "token_id": f"tok-{i}",
            "slug": f"slug-{i}", "market_title": f"Market {i}", "outcome": "Yes",
            "timestamp": f"2026-01-{(i % 27) + 1:02d}T01:00:00+00:00", "tx_hash": f"sell-{i}",
        })
    return events


def test_verification_fails_with_no_activity():
    result = verify_wallet("0xabc", [], None)
    assert result.status == VerificationStatus.FAILED


def test_verification_verified_for_healthy_wallet():
    events = _mk_events(20, win=True)
    stats = compute_wallet_stats("0xabc", events)
    result = verify_wallet("0xabc", events, stats)
    assert result.status in (VerificationStatus.VERIFIED, VerificationStatus.PARTIALLY_VERIFIED)
    assert result.checks["activity_retrievable"] is True
    assert result.checks["roi_calculable"] is True


def test_verification_flags_resolution_only_wallet():
    events = [
        {
            "wallet": "0xabc", "side": "BUY", "price": 0.4, "size": 100.0, "usdc_size": 40.0,
            "condition_id": f"cond-{i}", "token_id": f"tok-{i}", "slug": f"slug-{i}",
            "market_title": f"Market {i}", "outcome": "Yes",
            "timestamp": f"2026-01-{(i % 27) + 1:02d}T00:00:00+00:00", "tx_hash": f"buy-{i}",
        }
        for i in range(5)
    ]
    stats = compute_wallet_stats("0xabc", events)
    result = verify_wallet("0xabc", events, stats)
    assert result.checks["not_resolution_only"] is False


def test_verification_flags_concentrated_wallet():
    events = []
    events.append({
        "wallet": "0xabc", "side": "BUY", "price": 0.1, "size": 1000.0, "usdc_size": 100.0,
        "condition_id": "big", "token_id": "big-t", "slug": "big-s", "market_title": "Big",
        "outcome": "Yes", "timestamp": "2026-01-01T00:00:00+00:00", "tx_hash": "big-buy",
    })
    events.append({
        "wallet": "0xabc", "side": "SELL", "price": 0.9, "size": 1000.0, "usdc_size": 900.0,
        "condition_id": "big", "token_id": "big-t", "slug": "big-s", "market_title": "Big",
        "outcome": "Yes", "timestamp": "2026-01-02T00:00:00+00:00", "tx_hash": "big-sell",
    })
    for i in range(5):
        events.append({
            "wallet": "0xabc", "side": "BUY", "price": 0.5, "size": 10.0, "usdc_size": 5.0,
            "condition_id": f"small-{i}", "token_id": f"small-t-{i}", "slug": f"small-s-{i}",
            "market_title": f"Small {i}", "outcome": "Yes",
            "timestamp": f"2026-01-{i+3:02d}T00:00:00+00:00", "tx_hash": f"small-buy-{i}",
        })
        events.append({
            "wallet": "0xabc", "side": "SELL", "price": 0.45, "size": 10.0, "usdc_size": 4.5,
            "condition_id": f"small-{i}", "token_id": f"small-t-{i}", "slug": f"small-s-{i}",
            "market_title": f"Small {i}", "outcome": "Yes",
            "timestamp": f"2026-01-{i+3:02d}T01:00:00+00:00", "tx_hash": f"small-sell-{i}",
        })
    stats = compute_wallet_stats("0xabc", events)
    result = verify_wallet("0xabc", events, stats)
    assert result.checks["not_single_position_dependent"] is False



def test_tracked_wallets_prefers_new_tracked_field():
    from wallet.monitor import tracked_wallets

    wallets = [
        {"wallet": "0xNEWTRUE", "tracked": True},
        {"wallet": "0xNEWFALSE", "tracked": False, "enabled": True},
        {"wallet": "0xLEGACYTRUE", "enabled": True},
        {"wallet": "0xLEGACYFALSE", "enabled": False},
    ]

    assert tracked_wallets(wallets) == ["0xnewtrue", "0xlegacytrue"]
