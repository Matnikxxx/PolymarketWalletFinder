from helpers import make_event
from polymarket.wallet_activity import event_id, dedupe_new_events


def test_event_id_deterministic_with_tx_hash():
    ev = make_event(tx_hash="0xdeadbeef")
    assert event_id(ev) == "tx:0xdeadbeef"
    assert event_id(ev) == event_id(dict(ev))  # stable across calls


def test_event_id_fallback_without_tx_hash_is_stable():
    ev1 = make_event(tx_hash=None)
    ev2 = dict(ev1)
    assert event_id(ev1) == event_id(ev2)


def test_duplicate_events_are_filtered():
    ev = make_event(tx_hash="0xdup")
    ev["event_id"] = event_id(ev)
    seen = {ev["event_id"]}
    fresh = dedupe_new_events([ev], seen)
    assert fresh == []

