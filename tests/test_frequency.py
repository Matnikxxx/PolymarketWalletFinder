import json
from datetime import datetime, timedelta, timezone

from analytics.frequency import compute_frequency_stats, RollingWindowTracker
from analytics.wallet_stats import compute_wallet_stats
from wallet.ranking import score_wallet
from wallet.verifier import verify_wallet
from wallet.monitor import WalletMonitor
from config.settings import settings

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _round_trip_events(wallet, gaps_seconds, price_in=0.4, price_out=0.4):
    """
    Builds a sequence of BUY/SELL trades uniformly spaced by `gaps_seconds[i]`
    (each gap is the time from the previous trade to this one, entry ==
    gaps_seconds[0] == 0). Every trade -- BUY and SELL alike -- gets its own
    slot in the timeline so overall cadence (not holding time) drives the
    classification, matching how analytics.frequency treats every fill as
    one "trade" event.
    """
    events = []
    t = BASE
    for i, gap in enumerate(gaps_seconds):
        if i > 0:
            t = t + timedelta(seconds=gap)
        side = "BUY" if i % 2 == 0 else "SELL"
        price = price_in if side == "BUY" else price_out
        cid = f"c{i // 2}"
        events.append({
            "wallet": wallet, "side": side, "price": price, "size": 10.0,
            "usdc_size": price * 10.0, "condition_id": cid, "token_id": f"t{i // 2}",
            "slug": f"s{i // 2}", "market_title": f"M{i // 2}", "outcome": "Yes",
            "timestamp": t.isoformat(), "tx_hash": f"{side.lower()}{i}",
        })
    return events


# ----------------------------------------------------------------------
# 1-4. Classification tiers
# ----------------------------------------------------------------------
def test_normal_wallet_classified_normal():
    # trades several days apart, no bursts
    gaps = [0] + [86400 * 3] * 9
    stats = compute_frequency_stats("0xnormal", _round_trip_events("0xnormal", gaps))
    assert stats.classification == "NORMAL"


def test_low_frequency_wallet_classified_low_frequency():
    gaps = [0] + [86400 * 20] * 5
    stats = compute_frequency_stats("0xlow", _round_trip_events("0xlow", gaps))
    assert stats.classification == "LOW_FREQUENCY"


def test_rapid_wallet_classified_rapid_trader():
    # every 30 seconds
    gaps = [0] + [30] * 19
    stats = compute_frequency_stats("0xrapid", _round_trip_events("0xrapid", gaps))
    assert stats.classification == "RAPID_TRADER"


def test_extremely_rapid_wallet_classified_bot_like():
    # every 2 seconds -- tight burst
    gaps = [0] + [2] * 19
    stats = compute_frequency_stats("0xbot", _round_trip_events("0xbot", gaps))
    assert stats.classification == "BOT_LIKE"


def test_insufficient_history_does_not_force_a_classification():
    stats = compute_frequency_stats("0xtiny", _round_trip_events("0xtiny", [0]))
    assert stats.insufficient_data is True
    assert stats.classification == "NORMAL"


# ----------------------------------------------------------------------
# 5-6. Ranking penalty / high ROI does not override bot-like
# ----------------------------------------------------------------------
def test_rapid_wallet_receives_ranking_penalty():
    gaps = [0] + [30] * 19
    events = _round_trip_events("0xrapid2", gaps, price_in=0.40, price_out=0.55)
    wstats = compute_wallet_stats("0xrapid2", events)
    freq = compute_frequency_stats("0xrapid2", events)
    assert freq.classification == "RAPID_TRADER"

    card_with_penalty = score_wallet(wstats, freq)
    card_without_penalty = score_wallet(wstats, None)
    assert card_with_penalty.final_score < card_without_penalty.final_score
    assert card_with_penalty.frequency_classification == "RAPID_TRADER"


def test_high_roi_and_win_rate_do_not_override_bot_like():
    # 40 winning round trips, buys 3s apart -> ROI ~70%+, win rate 100%, but BOT_LIKE.
    events = []
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
    wstats = compute_wallet_stats("0xbotwin", events)
    freq = compute_frequency_stats("0xbotwin", events)
    assert wstats.roi_pct > 50 and wstats.win_rate_pct == 100.0
    assert freq.classification == "BOT_LIKE"

    card = score_wallet(wstats, freq)
    assert card.recommendation != "RECOMMENDED"


# ----------------------------------------------------------------------
# 7-8. Existing protections still work (regression)
# ----------------------------------------------------------------------
def test_profit_concentration_protection_still_works():
    events = []
    events.append({
        "wallet": "0xconc", "side": "BUY", "price": 0.1, "size": 1000.0, "usdc_size": 100.0,
        "condition_id": "big", "token_id": "big-t", "slug": "big-s", "market_title": "Big",
        "outcome": "Yes", "timestamp": "2026-01-01T00:00:00+00:00", "tx_hash": "big-buy",
    })
    events.append({
        "wallet": "0xconc", "side": "SELL", "price": 0.9, "size": 1000.0, "usdc_size": 900.0,
        "condition_id": "big", "token_id": "big-t", "slug": "big-s", "market_title": "Big",
        "outcome": "Yes", "timestamp": "2026-01-02T00:00:00+00:00", "tx_hash": "big-sell",
    })
    for i in range(5):
        events.append({
            "wallet": "0xconc", "side": "BUY", "price": 0.5, "size": 10.0, "usdc_size": 5.0,
            "condition_id": f"sm-{i}", "token_id": f"sm-t-{i}", "slug": f"sm-s-{i}",
            "market_title": f"Small {i}", "outcome": "Yes",
            "timestamp": f"2026-01-{i+3:02d}T00:00:00+00:00", "tx_hash": f"sm-buy-{i}",
        })
        events.append({
            "wallet": "0xconc", "side": "SELL", "price": 0.45, "size": 10.0, "usdc_size": 4.5,
            "condition_id": f"sm-{i}", "token_id": f"sm-t-{i}", "slug": f"sm-s-{i}",
            "market_title": f"Small {i}", "outcome": "Yes",
            "timestamp": f"2026-01-{i+3:02d}T01:00:00+00:00", "tx_hash": f"sm-sell-{i}",
        })
    stats = compute_wallet_stats("0xconc", events)
    result = verify_wallet("0xconc", events, stats)
    assert result.checks["not_single_position_dependent"] is False


def test_resolution_only_protection_still_works():
    events = [
        {
            "wallet": "0xreso", "side": "BUY", "price": 0.4, "size": 100.0, "usdc_size": 40.0,
            "condition_id": f"cond-{i}", "token_id": f"tok-{i}", "slug": f"slug-{i}",
            "market_title": f"Market {i}", "outcome": "Yes",
            "timestamp": f"2026-01-{(i % 27) + 1:02d}T00:00:00+00:00", "tx_hash": f"buy-{i}",
        }
        for i in range(5)
    ]
    stats = compute_wallet_stats("0xreso", events)
    result = verify_wallet("0xreso", events, stats)
    assert result.checks["not_resolution_only"] is False


# ----------------------------------------------------------------------
# 9-11. Baseline / new-event / duplicate regression, against the wallet
# activity TRACKER (formerly the copy-trading monitor -- see architecture
# cleanup: wallet/monitor.py no longer depends on PaperEngine/trading/*).
# ----------------------------------------------------------------------
class _FakeClient:
    def __init__(self):
        self.activity = {}

    def get_wallet_activity(self, wallet, limit=100, offset=0):
        return self.activity.get(wallet, [])

    def get_market(self, condition_id=None, slug=None):
        return {"conditionId": condition_id, "slug": slug, "title": "Fake", "category": "sports", "liquidity": 500}


def _raw_activity(wallet, side, price, size, cid, tok, slug, tx, ts):
    return {
        "proxyWallet": wallet, "side": side, "price": price, "size": size,
        "conditionId": cid, "asset": tok, "slug": slug, "title": "Fake Market",
        "outcome": "Yes", "timestamp": ts, "transactionHash": tx, "type": "TRADE",
    }


def test_baseline_never_reports_historical_activity_as_new(tmp_path):
    fake = _FakeClient()
    wallet = "0xbaseline"
    fake.activity[wallet] = [_raw_activity(wallet, "BUY", 0.3, 50, "h1", "ht1", "hs1", "hist-1", 1893456000)]

    monitor = WalletMonitor(client=fake, seen_events_file=tmp_path / "seen.json",
                             activity_log_file=tmp_path / "activity.json")
    monitor.establish_baseline(wallet)
    tracked = monitor.poll_wallet(wallet, wallet_score=70.0)
    assert tracked == []


def test_new_event_tracked_and_duplicate_ignored(tmp_path):
    fake = _FakeClient()
    wallet = "0xnewdup"
    fake.activity[wallet] = [_raw_activity(wallet, "BUY", 0.3, 50, "h1", "ht1", "hs1", "hist-1", 1893456000)]

    monitor = WalletMonitor(client=fake, seen_events_file=tmp_path / "seen.json",
                             activity_log_file=tmp_path / "activity.json")
    monitor.establish_baseline(wallet)

    fake.activity[wallet].append(_raw_activity(wallet, "BUY", 0.3, 50, "h2", "ht2", "hs2", "new-1", 1893456100))
    tracked = monitor.poll_wallet(wallet, wallet_score=70.0)
    assert len(tracked) == 1
    assert tracked[0].side == "BUY"
    assert tracked[0].wallet == wallet

    # poll again with same data -- must not duplicate
    tracked2 = monitor.poll_wallet(wallet, wallet_score=70.0)
    assert tracked2 == []


def test_wallet_monitor_resilience_to_malformed_state_files(tmp_path):
    """Confirm WalletMonitor initializes safely and appends to activity log even
    if seen_events or activity_log files contain non-dict or non-list JSON."""
    seen_file = tmp_path / "seen.json"
    act_file = tmp_path / "activity.json"

    # Corrupt or unexpected root types
    seen_file.write_text("[]", encoding="utf-8")
    act_file.write_text('{"bad_root": true}', encoding="utf-8")

    fake = _FakeClient()
    wallet = "0xrobust"
    fake.activity[wallet] = [_raw_activity(wallet, "BUY", 0.5, 10, "m1", "t1", "s1", "tx-init", 1893456000)]

    # Monitor must not crash on init
    monitor = WalletMonitor(client=fake, seen_events_file=seen_file, activity_log_file=act_file)
    assert monitor.baseline_done == {}
    assert monitor.seen_ids == set()

    # Baseline establish works cleanly
    monitor.establish_baseline(wallet)
    assert monitor.baseline_done.get(wallet) is True

    # New event poll and append to previously non-list act_file works cleanly
    fake.activity[wallet].append(_raw_activity(wallet, "BUY", 0.5, 10, "m1", "t1", "s1", "tx-new", 1893456100))
    tracked = monitor.poll_wallet(wallet)
    assert len(tracked) == 1

    # Activity file is now a valid list with 1 entry
    saved_act = json.loads(act_file.read_text(encoding="utf-8"))
    assert isinstance(saved_act, list)
    assert len(saved_act) == 1
    assert saved_act[0]["event_id"] == "tx:tx-new"


# ----------------------------------------------------------------------
# 12-15. BUY / additional BUY / SELL / SELL activity is tracked in order
# (formerly asserted OPENED/INCREASED/PARTIAL_EXIT/FULL_EXIT via
# PaperEngine -- that position-lifecycle bookkeeping was copy-trading-only
# and was removed; the tracker's job is only to observe each raw fill).
# ----------------------------------------------------------------------
def test_buy_and_sell_sequence_tracked_in_order(tmp_path):
    fake = _FakeClient()
    wallet = "0xfull"
    fake.activity[wallet] = []

    monitor = WalletMonitor(client=fake, seen_events_file=tmp_path / "seen.json",
                             activity_log_file=tmp_path / "activity.json")
    monitor.establish_baseline(wallet)  # nothing yet, baseline is empty

    fake.activity[wallet].append(_raw_activity(wallet, "BUY", 0.3, 100, "m1", "t1", "s1", "buy1", 1900000000))
    out1 = monitor.poll_wallet(wallet, wallet_score=70.0)
    assert len(out1) == 1 and out1[0].side == "BUY" and out1[0].price == 0.3

    fake.activity[wallet].append(_raw_activity(wallet, "BUY", 0.25, 100, "m1", "t1", "s1", "buy2", 1900000100))
    out2 = monitor.poll_wallet(wallet, wallet_score=70.0)
    assert len(out2) == 1 and out2[0].side == "BUY" and out2[0].price == 0.25

    fake.activity[wallet].append(_raw_activity(wallet, "SELL", 0.5, 100, "m1", "t1", "s1", "sell1", 1900000200))
    out3 = monitor.poll_wallet(wallet, wallet_score=70.0)
    assert len(out3) == 1 and out3[0].side == "SELL"

    fake.activity[wallet].append(_raw_activity(wallet, "SELL", 0.5, 100, "m1", "t1", "s1", "sell2", 1900000300))
    out4 = monitor.poll_wallet(wallet, wallet_score=70.0)
    assert len(out4) == 1 and out4[0].side == "SELL"

    # the full sequence must also be persisted to the activity log, in order
    logged = json.loads((tmp_path / "activity.json").read_text())
    assert [e["side"] for e in logged] == ["BUY", "BUY", "SELL", "SELL"]


# ----------------------------------------------------------------------
# 16. Rapid-trade diagnostic is informational only -- it flags a burst but
# never rejects, skips, delays, or resizes anything (there is no copy
# amount to resize and nothing to reject: the tracker only observes).
# ----------------------------------------------------------------------
def test_rapid_trade_diagnostic_flags_burst_without_blocking_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RAPID_TRADE_ENABLED", True)
    monkeypatch.setattr(settings, "MAX_TRADES_IN_WINDOW", 2)
    monkeypatch.setattr(settings, "RAPID_WINDOW_SECONDS", 60)
    monkeypatch.setattr(settings, "MIN_SECONDS_BETWEEN_TRADES", 0)

    fake = _FakeClient()
    wallet = "0xburst"
    fake.activity[wallet] = []

    monitor = WalletMonitor(client=fake, seen_events_file=tmp_path / "seen.json",
                             activity_log_file=tmp_path / "activity.json")
    monitor.establish_baseline(wallet)

    # 3 BUYs, 5 seconds apart, all in a single poll -> with
    # MAX_TRADES_IN_WINDOW=2 the 3rd should be flagged as rapid -- but all
    # three must still be tracked (nothing is ever rejected/dropped).
    base_ts = 1900000000
    fake.activity[wallet] = [
        _raw_activity(wallet, "BUY", 0.3, 50, f"rc{i}", f"rt{i}", f"rs{i}", f"rbuy{i}", base_ts + i * 5)
        for i in range(3)
    ]
    tracked = monitor.poll_wallet(wallet, wallet_score=70.0)
    assert len(tracked) == 3  # every BUY is still tracked
    assert any(t.is_rapid for t in tracked)
    rapid = [t for t in tracked if t.is_rapid]
    assert all(t.rapid_reason for t in rapid)


def test_sell_events_always_tracked_regardless_of_cadence(tmp_path, monkeypatch):
    """SELLs are never fed into the rapid-trade diagnostic (only BUYs are,
    matching the historical classification's focus on entries) -- they are
    always plainly tracked, same as a normal-cadence BUY."""
    monkeypatch.setattr(settings, "RAPID_TRADE_ENABLED", True)
    monkeypatch.setattr(settings, "MAX_TRADES_IN_WINDOW", 2)
    monkeypatch.setattr(settings, "RAPID_WINDOW_SECONDS", 60)
    monkeypatch.setattr(settings, "MIN_SECONDS_BETWEEN_TRADES", 0)

    fake = _FakeClient()
    wallet = "0xsellthrough"
    fake.activity[wallet] = []

    monitor = WalletMonitor(client=fake, seen_events_file=tmp_path / "seen.json",
                             activity_log_file=tmp_path / "activity.json")
    monitor.establish_baseline(wallet)

    fake.activity[wallet] = [_raw_activity(wallet, "BUY", 0.4, 100, "sc1", "st1", "ss1", "sbuy1", 1900000000)]
    monitor.poll_wallet(wallet, wallet_score=70.0)

    # two rapid-fire SELLs right after -- both must still be tracked plainly
    fake.activity[wallet].append(_raw_activity(wallet, "SELL", 0.5, 50, "sc1", "st1", "ss1", "ssell1", 1900000005))
    fake.activity[wallet].append(_raw_activity(wallet, "SELL", 0.5, 50, "sc1", "st1", "ss1", "ssell2", 1900000006))
    tracked = monitor.poll_wallet(wallet, wallet_score=70.0)
    assert len(tracked) == 2
    assert all(t.side == "SELL" for t in tracked)
    assert all(t.is_rapid is False for t in tracked)


# ----------------------------------------------------------------------
# tx_hash-based fill grouping (partial-fill false-positive fix)
# ----------------------------------------------------------------------
def test_partial_fills_of_one_order_are_not_misclassified_as_bot_like():
    """One big entry + one big exit, each split into many partial fills
    sharing a tx_hash, must NOT trigger BOT_LIKE just because the raw fill
    count is high and clustered in time."""
    base = BASE
    events = []
    for i in range(20):
        events.append({
            "wallet": "0xbig", "side": "BUY", "price": 0.4, "size": 50, "usdc_size": 20.0,
            "condition_id": "c1", "token_id": "t1", "slug": "s1", "market_title": "Big Market",
            "outcome": "Yes", "timestamp": (base + timedelta(seconds=i * 2)).isoformat(),
            "tx_hash": "entry-tx-abc",
        })
    for i in range(20):
        events.append({
            "wallet": "0xbig", "side": "SELL", "price": 0.7, "size": 50, "usdc_size": 35.0,
            "condition_id": "c1", "token_id": "t1", "slug": "s1", "market_title": "Big Market",
            "outcome": "Yes", "timestamp": (base + timedelta(days=30, seconds=i * 2)).isoformat(),
            "tx_hash": "exit-tx-xyz",
        })
    stats = compute_frequency_stats("0xbig", events)
    assert stats.raw_fill_count == 40
    assert stats.total_trades == 2
    assert stats.classification != "BOT_LIKE"


def test_genuinely_rapid_separate_transactions_still_trigger_bot_like():
    """The fix must not weaken real bot detection: many DISTINCT
    transactions fired rapidly must still classify as BOT_LIKE."""
    base = BASE
    events = []
    for i in range(20):
        side = "BUY" if i % 2 == 0 else "SELL"
        events.append({
            "wallet": "0xrealbot", "side": side, "price": 0.4, "size": 50, "usdc_size": 20.0,
            "condition_id": f"c{i // 2}", "token_id": f"t{i // 2}", "slug": f"s{i // 2}",
            "market_title": f"M{i // 2}", "outcome": "Yes",
            "timestamp": (base + timedelta(seconds=i * 2)).isoformat(),
            "tx_hash": f"separate-tx-{i}",
        })
    stats = compute_frequency_stats("0xrealbot", events)
    assert stats.total_trades == 20  # no false grouping across distinct tx_hashes
    assert stats.classification == "BOT_LIKE"


def test_missing_tx_hash_events_are_never_silently_merged():
    base = BASE
    events = []
    for i in range(20):
        side = "BUY" if i % 2 == 0 else "SELL"
        events.append({
            "wallet": "0xnotx", "side": side, "price": 0.4, "size": 50, "usdc_size": 20.0,
            "condition_id": f"c{i // 2}", "token_id": f"t{i // 2}", "slug": f"s{i // 2}",
            "market_title": f"M{i // 2}", "outcome": "Yes",
            "timestamp": (base + timedelta(seconds=i * 2)).isoformat(),
            "tx_hash": None,
        })
    stats = compute_frequency_stats("0xnotx", events)
    assert stats.total_trades == 20  # conservative: never merge without a confirmed shared tx_hash


# ----------------------------------------------------------------------
# Diagnostic-only additions (this pass): build_frequency_diagnostic /
# format_frequency_diagnostic, plus explicit timestamp-edge-case coverage.
# No thresholds changed anywhere in this section.
# ----------------------------------------------------------------------
from analytics.frequency import build_frequency_diagnostic, format_frequency_diagnostic


def _tx_events(wallet, gaps_seconds, tx_prefix="tx"):
    """Each trade is its own tx_hash (no partial-fill grouping involved),
    uniform spacing per gaps_seconds[i] from the previous trade."""
    events = []
    t = BASE
    for i, gap in enumerate(gaps_seconds):
        if i > 0:
            t = t + timedelta(seconds=gap)
        side = "BUY" if i % 2 == 0 else "SELL"
        events.append({
            "wallet": wallet, "side": side, "price": 0.4, "size": 10, "usdc_size": 4.0,
            "condition_id": f"c{i}", "token_id": f"t{i}", "slug": f"s{i}", "market_title": f"M{i}",
            "outcome": "Yes", "timestamp": t.isoformat(), "tx_hash": f"{tx_prefix}-{i}",
        })
    return events


# 1. Normal low-frequency wallet -----------------------------------------
def test_diagnostic_normal_low_frequency_wallet():
    events = _tx_events("0xd1", [0] + [86400 * 3] * 20)  # 3 days apart
    stats = compute_frequency_stats("0xd1", events)
    diag = build_frequency_diagnostic(stats)
    assert diag["classification"] == "NORMAL"
    assert diag["rule_5_trades_60s"] is False
    assert diag["rule_30pct_gaps_le_10s"] is False
    assert diag["raw_fill_count"] == diag["logical_trades"] == 21  # 1 fill per tx here
    text = format_frequency_diagnostic(diag)
    assert "FREQUENCY_DIAGNOSTIC" in text and "classification=NORMAL" in text


# 2. Wallet with 5+ trades in 60 seconds -----------------------------------
def test_diagnostic_five_plus_trades_in_60_seconds():
    # 15 trades, 6s apart -> 11 fall within any 60s window (new threshold: 10)
    events = _tx_events("0xd2", [0] + [6] * 14)
    stats = compute_frequency_stats("0xd2", events)
    diag = build_frequency_diagnostic(stats)
    assert diag["rule_5_trades_60s"] is True
    assert diag["max_trades_60s"] >= settings.BOT_LIKE_MAX_TRADES_IN_1M
    assert diag["classification"] == "BOT_LIKE"


# 3. Wallet where >=30% of gaps are <=10 seconds ---------------------------
def test_diagnostic_30pct_gaps_within_10s_alone_is_no_longer_bot_like():
    # 10 trades: alternate a 5s gap and a 500s gap -> 50% of gaps <=10s,
    # but keep them spread out enough that the 60s-window rule doesn't
    # also trip, isolating the 30%-gap rule specifically. Under the new
    # AND semantics (this rule alone, without the 5-in-60s rule) this
    # must NOT classify as BOT_LIKE -- see the explicit AND/OR test group
    # below for the full 4-case matrix.
    gaps = [0]
    for i in range(9):
        gaps.append(5 if i % 2 == 0 else 500)
    events = _tx_events("0xd3", gaps)
    stats = compute_frequency_stats("0xd3", events)
    diag = build_frequency_diagnostic(stats)
    assert diag["pct_gaps_le_10s"] >= 30.0
    assert diag["rule_30pct_gaps_le_10s"] is True
    assert diag["rule_5_trades_60s"] is False  # confirms true isolation of this one rule
    assert diag["classification"] != "BOT_LIKE"


# 4. Wallet close to but not crossing thresholds ---------------------------
def test_diagnostic_close_to_but_not_crossing_thresholds():
    # 4 trades within a 45s span (threshold is >=5 in any 60s window),
    # spaced 15s apart (threshold for the pct rule is <=10s) -- must NOT trigger.
    events = _tx_events("0xd4", [0, 15, 15, 15] + [86400] * 6)
    stats = compute_frequency_stats("0xd4", events)
    diag = build_frequency_diagnostic(stats)
    assert diag["max_trades_60s"] < settings.BOT_LIKE_MAX_TRADES_IN_1M
    assert diag["rule_5_trades_60s"] is False
    assert diag["rule_30pct_gaps_le_10s"] is False
    assert diag["classification"] != "BOT_LIKE"


# 5. Duplicate timestamps ----------------------------------------------------
def test_timestamp_duplicate_timestamps_produce_legitimate_zero_gap():
    """Two distinct transactions at the exact same timestamp is a real
    (not artificial) zero-second gap -- must be counted, not dropped."""
    t = BASE.isoformat()
    events = [
        {"wallet": "0xd5", "side": "BUY", "price": 0.4, "size": 10, "usdc_size": 4.0,
         "condition_id": "c1", "token_id": "t1", "slug": "s1", "market_title": "M1",
         "outcome": "Yes", "timestamp": t, "tx_hash": "dup-tx-1"},
        {"wallet": "0xd5", "side": "BUY", "price": 0.4, "size": 10, "usdc_size": 4.0,
         "condition_id": "c2", "token_id": "t2", "slug": "s2", "market_title": "M2",
         "outcome": "Yes", "timestamp": t, "tx_hash": "dup-tx-2"},
        {"wallet": "0xd5", "side": "SELL", "price": 0.4, "size": 10, "usdc_size": 4.0,
         "condition_id": "c1", "token_id": "t1", "slug": "s1", "market_title": "M1",
         "outcome": "Yes", "timestamp": (BASE + timedelta(days=5)).isoformat(), "tx_hash": "dup-tx-3"},
    ]
    stats = compute_frequency_stats("0xd5", events)
    diag = build_frequency_diagnostic(stats)
    assert diag["min_gap"] == "0s"  # the genuine simultaneous-trade gap, not fabricated
    assert diag["logical_trades"] == 3  # 3 distinct tx_hashes, correctly NOT merged


# 6. Multiple fills sharing one tx_hash --------------------------------------
def test_diagnostic_multiple_fills_sharing_one_tx_hash():
    events = []
    for i in range(10):
        events.append({
            "wallet": "0xd6", "side": "BUY", "price": 0.4, "size": 10, "usdc_size": 4.0,
            "condition_id": "c1", "token_id": "t1", "slug": "s1", "market_title": "M1",
            "outcome": "Yes", "timestamp": (BASE + timedelta(seconds=i)).isoformat(),
            "tx_hash": "one-big-order",
        })
    stats = compute_frequency_stats("0xd6", events)
    diag = build_frequency_diagnostic(stats)
    assert diag["raw_fill_count"] == 10
    # a single tx_hash with fewer than MIN_TRADES_FOR_FREQUENCY_CLASSIFICATION
    # logical trades correctly reports insufficient data rather than a
    # forced classification.
    assert diag["logical_trades"] == 1
    assert stats.insufficient_data is True


# 7. Malformed / missing timestamp -------------------------------------------
def test_timestamp_malformed_or_missing_is_dropped_not_fabricated():
    events = _tx_events("0xd7", [0] + [86400] * 4)
    # add a row with a missing timestamp and one with a garbage timestamp
    events.append({
        "wallet": "0xd7", "side": "BUY", "price": 0.4, "size": 10, "usdc_size": 4.0,
        "condition_id": "cbad1", "token_id": "tbad1", "slug": "sbad1", "market_title": "Bad1",
        "outcome": "Yes", "timestamp": None, "tx_hash": "bad-tx-1",
    })
    events.append({
        "wallet": "0xd7", "side": "BUY", "price": 0.4, "size": 10, "usdc_size": 4.0,
        "condition_id": "cbad2", "token_id": "tbad2", "slug": "sbad2", "market_title": "Bad2",
        "outcome": "Yes", "timestamp": "not-a-real-timestamp", "tx_hash": "bad-tx-2",
    })
    stats = compute_frequency_stats("0xd7", events)
    diag = build_frequency_diagnostic(stats)
    # the 2 malformed/missing-timestamp trades must be dropped from the
    # gap/cadence analysis, not turned into artificial 0-second gaps
    assert diag["logical_trades"] == 5  # only the 5 validly-timestamped trades counted
    assert diag["min_gap"] != "0s"


# ----------------------------------------------------------------------
# BOT_LIKE: OR -> AND change (explicit 4-case matrix, this pass)
# Thresholds unchanged (BOT_LIKE_MAX_TRADES_IN_1M=5, BOT_LIKE_PCT_WITHIN_10S=30.0).
# Only the combining logic changed. Both rules are still independently
# reported by build_frequency_diagnostic() regardless of final classification.
# ----------------------------------------------------------------------
def test_bot_like_case1_both_rules_true():
    """5+ trades in 60s AND >=30% of gaps <=10s -> BOT_LIKE."""
    events = _tx_events("0xcase1", [0] + [2] * 19)  # 20 trades, 2s apart
    diag = build_frequency_diagnostic(compute_frequency_stats("0xcase1", events))
    assert diag["rule_5_trades_60s"] is True
    assert diag["rule_30pct_gaps_le_10s"] is True
    assert diag["classification"] == "BOT_LIKE"


def test_bot_like_case2_only_5_trades_60s_rule_true():
    """10+ trades in 60s but <50% of gaps <=10s -> NOT BOT_LIKE.
    (Test name kept for continuity; thresholds are now 10 trades/60s and
    50% short-gap, per the current settings.)"""
    # 10 trades 6s apart (all within one 60s window -- 9 short internal
    # gaps), then 20 widely-spaced trades to dilute the overall short-gap
    # percentage below 50%.
    gaps = [0] + [6] * 9 + [86400] * 20
    events = _tx_events("0xcase2", gaps)
    diag = build_frequency_diagnostic(compute_frequency_stats("0xcase2", events))
    assert diag["max_trades_60s"] >= settings.BOT_LIKE_MAX_TRADES_IN_1M
    assert diag["rule_5_trades_60s"] is True
    assert diag["rule_30pct_gaps_le_10s"] is False
    assert diag["classification"] != "BOT_LIKE"


def test_bot_like_case3_only_30pct_gaps_rule_true():
    """>=30% of gaps <=10s but never 5+ trades in any 60s window -> NOT BOT_LIKE."""
    # 10 pairs of trades 5s apart, each pair separated by 1000s from the
    # next -- >=30% of gaps are short, but no 60s window ever contains
    # more than 2 trades.
    gaps = [0]
    for i in range(9):
        gaps.append(5 if i % 2 == 0 else 1000)
    events = _tx_events("0xcase3", gaps)
    diag = build_frequency_diagnostic(compute_frequency_stats("0xcase3", events))
    assert diag["rule_5_trades_60s"] is False
    assert diag["rule_30pct_gaps_le_10s"] is True
    assert diag["classification"] != "BOT_LIKE"


def test_bot_like_case4_neither_rule_true():
    """Neither rule triggers -> NOT BOT_LIKE (unsurprising, but completes the matrix)."""
    events = _tx_events("0xcase4", [0] + [86400 * 2] * 14)
    diag = build_frequency_diagnostic(compute_frequency_stats("0xcase4", events))
    assert diag["rule_5_trades_60s"] is False
    assert diag["rule_30pct_gaps_le_10s"] is False
    assert diag["classification"] != "BOT_LIKE"


def test_bot_like_diagnostic_still_reports_both_rules_independently():
    """Regardless of the AND change, the diagnostic must keep reporting
    each rule's own YES/NO independently -- only the combined
    classification logic changed, not what's exposed. Labels reflect the
    CURRENT thresholds (10 trades/60s, 50% short-gap), not stale text."""
    gaps = [0] + [6] * 9 + [86400] * 20
    events = _tx_events("0xcase2b", gaps)
    diag = build_frequency_diagnostic(compute_frequency_stats("0xcase2b", events))
    assert "rule_5_trades_60s" in diag
    assert "rule_30pct_gaps_le_10s" in diag
    text = format_frequency_diagnostic(diag)
    assert f"rule_{settings.BOT_LIKE_MAX_TRADES_IN_1M}_trades_60s=YES" in text
    assert f"rule_{settings.BOT_LIKE_PCT_WITHIN_10S:g}pct_gaps_le_10s=NO" in text


# ----------------------------------------------------------------------
# BOT_LIKE threshold update: 5->10 trades/60s, 30%->50% short-gap (this pass)
# Exact scenarios from the request, tested directly against
# classify_frequency() with hand-set FrequencyStats fields so the numbers
# match precisely (no timestamp-construction approximation needed).
# AND logic (from the previous pass) is unchanged; only the two threshold
# VALUES changed, both still read from settings.* dynamically.
# ----------------------------------------------------------------------
from analytics.frequency import FrequencyStats, classify_frequency


def _direct_stats(max_trades_60s, pct_within_10s):
    return FrequencyStats(
        wallet="0xdirect",
        total_trades=max(max_trades_60s, 20),
        max_trades_in_1m_window=max_trades_60s,
        pct_within_10s=pct_within_10s,
        insufficient_data=False,
    )


def test_bot_like_threshold_11_trades_38pct_is_not_bot_like():
    """11 trades/60s, 38% gaps<=10s -> NOT BOT_LIKE (38% < 50% threshold)."""
    stats = classify_frequency(_direct_stats(11, 38.0))
    assert stats.classification != "BOT_LIKE"


def test_bot_like_threshold_9_trades_80pct_is_not_bot_like():
    """9 trades/60s, 80% gaps<=10s -> NOT BOT_LIKE (9 < 10 threshold)."""
    stats = classify_frequency(_direct_stats(9, 80.0))
    assert stats.classification != "BOT_LIKE"


def test_bot_like_threshold_10_trades_50pct_is_bot_like():
    """10 trades/60s, 50% gaps<=10s -> BOT_LIKE (both thresholds met exactly)."""
    stats = classify_frequency(_direct_stats(10, 50.0))
    assert stats.classification == "BOT_LIKE"


def test_bot_like_threshold_20_trades_70pct_is_bot_like():
    """20 trades/60s, 70% gaps<=10s -> BOT_LIKE (both comfortably exceeded)."""
    stats = classify_frequency(_direct_stats(20, 70.0))
    assert stats.classification == "BOT_LIKE"


def test_bot_like_thresholds_are_now_10_and_50():
    """Confirms the configured threshold VALUES themselves (not just the
    AND logic) actually changed to 10 / 50.0."""
    assert settings.BOT_LIKE_MAX_TRADES_IN_1M == 10
    assert settings.BOT_LIKE_PCT_WITHIN_10S == 50.0


def test_rapid_trader_classification_unaffected_by_bot_like_threshold_change():
    """RAPID_TRADER's own thresholds (MAX_TRADES_IN_WINDOW, RAPID_WINDOW_SECONDS,
    MAX_SHORT_INTERVAL_PERCENT) are untouched by this pass -- a wallet that
    was RAPID_TRADER before must still be RAPID_TRADER now, as long as it
    doesn't also newly qualify for the (now higher) BOT_LIKE bar."""
    # 6 trades within a 5-minute window (RAPID_TRADER's own window/count
    # rule: settings.MAX_TRADES_IN_WINDOW), spaced 50s apart so they never
    # cluster within a 60s window and never produce <=10s gaps -- clean of
    # any BOT_LIKE signal, isolating RAPID_TRADER's own rule.
    events = _tx_events("0xrapidcheck", [0] + [50] * 5 + [86400] * 10)
    stats = compute_frequency_stats("0xrapidcheck", events)
    assert stats.classification == "RAPID_TRADER"


def test_scoring_and_ranking_module_untouched_by_this_pass():
    """Confirms wallet/ranking.py's scoring weights/thresholds are exactly
    as the previous pass left them -- this change only touched BOT_LIKE
    threshold values and diagnostic label text."""
    from wallet.ranking import score_wallet
    from analytics.wallet_stats import compute_wallet_stats, enrich_with_closed_positions

    events, closed = [], []
    for i in range(45):
        day = i * 4
        t = BASE + timedelta(days=day)
        exit_t = t + timedelta(hours=12)
        events.append({
            "wallet": "0xscorecheck", "side": "BUY", "price": 0.4, "size": 50, "usdc_size": 20.0,
            "condition_id": f"c{i}", "token_id": f"t{i}", "slug": f"s{i}", "market_title": f"M{i}",
            "outcome": "Yes", "timestamp": t.isoformat(), "tx_hash": f"buy-{i}",
        })
        events.append({
            "wallet": "0xscorecheck", "side": "SELL", "price": 0.52, "size": 50, "usdc_size": 26.0,
            "condition_id": f"c{i}", "token_id": f"t{i}", "slug": f"s{i}", "market_title": f"M{i}",
            "outcome": "Yes", "timestamp": exit_t.isoformat(), "tx_hash": f"sell-{i}",
        })
        pnl = 6.0 if i % 5 != 0 else -3.0
        closed.append({
            "proxyWallet": "0xscorecheck", "asset": f"t{i}", "conditionId": f"c{i}", "avgPrice": 0.4,
            "totalBought": 50, "realizedPnl": pnl, "curPrice": 0.52,
            "timestamp": int(exit_t.timestamp()), "title": f"M{i}", "slug": f"s{i}", "outcome": "Yes",
        })
    stats = compute_wallet_stats("0xscorecheck", events)
    stats = enrich_with_closed_positions(stats, closed, api_ok=True)
    freq = compute_frequency_stats("0xscorecheck", events)
    card = score_wallet(stats, freq)
    # Same strong-wallet shape used in the previous pass's scoring tests --
    # still lands RECOMMENDED with the same ~93-point score, unaffected by
    # this threshold-only change.
    assert card.recommendation == "RECOMMENDED"
    assert card.final_score >= 90


# ----------------------------------------------------------------------
# Phase 44 — Configured thresholds and combined regression tests
# ----------------------------------------------------------------------
def test_configured_max_trades_in_window_is_10():
    """Phase 44 requires MAX_TRADES_IN_WINDOW configured default = 10."""
    assert settings.MAX_TRADES_IN_WINDOW == 10


def test_phase44_combined_threshold_regression():
    """
    Regression test for Phase 44 approved rule calibration:
    A wallet with:
    - Consistency = 4.41 (>= 4.40 threshold, passes hard filter)
    - Peak 5m trade count = 9 (< 10 threshold, classified ACTIVE rather than RAPID_TRADER)
    - No other hard-filter failures
    is classified as ACTIVE and passes the consistency hard-filter gate.
    """
    from analytics.frequency import compute_frequency_profile
    from analytics.models import WalletStatistics
    from filters.wallet_filter import filter_wallet
    from wallet.ranking import score_wallet

    # 1. Synthetic activity: 9 trades within 5 minutes (20s apart), followed by
    # 31 trades spaced 400s apart (overall median gap = 400s >= 300s, % <=60s = 20.5% < 25%).
    # Under MAX_TRADES_IN_WINDOW = 10, Peak 5m = 9 is NOT RAPID_TRADER; it qualifies as ACTIVE.
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = []
    for i in range(9):
        t = base_time + timedelta(seconds=i * 20)
        events.append({
            "wallet": "0xphase44", "side": "BUY", "price": 0.5, "size": 100.0, "usdc_size": 50.0,
            "condition_id": f"cond_{i}", "token_id": f"tok_{i}", "slug": f"slug_{i}",
            "timestamp": t.isoformat(), "tx_hash": f"tx_burst_{i}", "activity_type": "TRADE",
        })
    for i in range(9, 40):
        t = base_time + timedelta(seconds=160 + (i - 8) * 400)
        events.append({
            "wallet": "0xphase44", "side": "BUY", "price": 0.5, "size": 100.0, "usdc_size": 50.0,
            "condition_id": f"cond_{i}", "token_id": f"tok_{i}", "slug": f"slug_{i}",
            "timestamp": t.isoformat(), "tx_hash": f"tx_normal_{i}", "activity_type": "TRADE",
        })

    profile = compute_frequency_profile("0xphase44", events)
    assert profile.metrics.peak_trades_in_5m == 9
    assert profile.classification == "ACTIVE"

    # 2. Synthetic wallet statistics with consistency ~ 4.41:
    # CV = sigma / mu = 0.7612 / 0.6 = 1.2687 -> consistency = 10 / (1 + 1.2687) = 4.4078
    stats = WalletStatistics(
        wallet="0xphase44",
        positions_counted=100,
        wins=80,
        losses=20,
        breakeven=0,
        partials=0,
        win_rate_pct=80.0,
        total_settled_cost=1000.0,
        total_settled_net_result=600.0,
        roi_fee_exclusive_pct=60.0,
        max_drawdown_usd=5.0,
        drawdown_cost_pct=0.5,
        drawdown_peak_pct=0.5,
        top1_profit_share_pct=2.0,
        top3_profit_share_pct=5.0,
        mean_position_cost=10.0,
        median_position_cost=10.0,
        max_position_cost=10.0,
        stdev_position_cost=0.0,
        consistency_mean_roi=0.6,
        consistency_median_roi=0.6,
        consistency_stdev_roi=0.7612,
        active_days=45,
        coverage_ratio=1.0,
        is_complete_history=True,
        retrieval_status="SUFFICIENT",
        reconciled=True,
        is_resolution_only=False,
        category_mix={"crypto": 60, "sports": 40},
    )

    # 3. Filter check: passes the consistency gate at MIN_CONSISTENCY = 4.4
    filter_result = filter_wallet(stats)
    assert filter_result.passed is True
    assert filter_result.status == "PASS"

    # 4. Scoring check: ACTIVE receives -7.5 penalty and scores >= 75.0 (RECOMMENDED)
    card = score_wallet(stats, profile)
    assert card.frequency_classification == "ACTIVE"
    assert card.final_score >= 75.0
    assert card.recommendation == "RECOMMENDED"

