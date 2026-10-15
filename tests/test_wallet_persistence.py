"""
Phase B tests for wallet persistence in main.py::cmd_scan_wallets and the
wallet/monitor.py merge helper.

Verified here:
- a previously tracked wallet that is NOT rediscovered is retained
- manually added wallets are retained
- custom (hand-added) fields survive a re-scan
- `tracked` preferences survive a re-scan; legacy `enabled` is honored and
  migrated to `tracked` on write
- duplicate records caused by address casing collapse into one record
- output ordering is deterministic

All tests use temporary wallets.json files -- the real data/wallets.json is
never read or written by the suite.
"""
from __future__ import annotations

import json

from config.settings import settings
from wallet.monitor import (
    is_wallet_tracked,
    merge_wallet_records,
    save_wallet_config,
    wallet_address_key,
)


W1 = "0x" + "11" * 20
W2 = "0x" + "22" * 20
W3 = "0x" + "33" * 20
W1_UPPER = "0x" + "11" * 20
W1_MIXED = "0x" + "1A" * 20   # different casing for a different address


def _record(wallet: str, **fields) -> dict:
    record = {"wallet": wallet, "tracked": False}
    record.update(fields)
    return record


# ---------------------------------------------------------------------------
# Merge helper
# ---------------------------------------------------------------------------

def test_tracked_wallet_not_rediscovered_is_retained_unchanged():
    existing = [_record(W1, tracked=True, last_score=77.5, roi_pct=30.0)]
    scanned = [_record(W2, last_score=60.0)]

    merged = merge_wallet_records(existing, scanned)

    assert [r["wallet"] for r in merged] == [W2, W1]
    retained = merged[1]
    assert retained == {"wallet": W1, "tracked": True, "last_score": 77.5, "roi_pct": 30.0}
    assert is_wallet_tracked(retained) is True


def test_manually_added_wallet_without_metrics_is_retained():
    existing = [{"wallet": W3, "tracked": True, "notes": "added by hand"}]
    scanned = [_record(W1)]

    merged = merge_wallet_records(existing, scanned)

    assert {"wallet": W3, "tracked": True, "notes": "added by hand"} in merged
    assert len(merged) == 2


def test_custom_fields_preserved_when_wallet_is_rescanned():
    existing = [_record(W1, tracked=True, my_tag="keep-me", notes="private", last_score=10.0)]
    scanned = [_record(W1, last_score=88.0, roi_pct=42.0)]

    merged = merge_wallet_records(existing, scanned)

    assert len(merged) == 1
    entry = merged[0]
    assert entry["my_tag"] == "keep-me"
    assert entry["notes"] == "private"
    assert entry["last_score"] == 88.0        # fresh value wins
    assert entry["roi_pct"] == 42.0           # fresh value added
    assert entry["tracked"] is True           # user selection survives


def test_legacy_enabled_field_is_honored_and_migrated_to_tracked():
    existing = [{"wallet": W1, "enabled": True}]
    scanned = [_record(W1)]

    merged = merge_wallet_records(existing, scanned)

    assert len(merged) == 1
    assert merged[0]["tracked"] is True
    assert "enabled" not in merged[0]
    assert is_wallet_tracked(merged[0]) is True


def test_untracked_wallet_stays_untracked_and_manual_selection_wins():
    existing = [_record(W1, tracked=False), _record(W2, tracked=True)]
    scanned = [_record(W1, tracked=True), _record(W2, tracked=False)]

    merged = merge_wallet_records(existing, scanned)

    by_wallet = {r["wallet"]: r for r in merged}
    # A scan may never silently turn tracking on or off.
    assert by_wallet[W1]["tracked"] is False
    assert by_wallet[W2]["tracked"] is True


def test_duplicate_records_caused_by_casing_collapse():
    existing = [
        _record(W1, tracked=True, roi_pct=10.0),
        _record(W1.upper(), roi_pct=99.0),        # same wallet, different casing
    ]
    scanned = [_record(W1.lower(), roi_pct=20.0)]

    merged = merge_wallet_records(existing, scanned)

    assert len(merged) == 1
    assert merged[0]["wallet"] == W1.lower()
    assert merged[0]["roi_pct"] == 20.0           # scanned value wins
    assert is_wallet_tracked(merged[0]) is True   # selection never lost


def test_scanned_address_casing_is_normalized():
    merged = merge_wallet_records([], [_record("0x" + "AB" * 20)])

    assert merged[0]["wallet"] == "0x" + "ab" * 20


def test_ordering_is_deterministic():
    existing = [_record(W1), _record(W2), _record(W3)]
    scanned = [_record(W2, last_score=5.0), _record(W1, last_score=6.0)]

    first = merge_wallet_records(existing, scanned)
    second = merge_wallet_records(existing, scanned)

    # scanned order first, then retained wallets in stored order
    assert [r["wallet"] for r in first] == [W2, W1, W3]
    assert first == second


def test_invalid_entries_are_skipped_not_crashed_on():
    existing = [None, "not-a-record", {"no_wallet": True}, _record(W1)]
    scanned = [42, {}, {"wallet": None}, _record(W2)]

    merged = merge_wallet_records(existing, scanned)

    assert [r["wallet"] for r in merged] == [W2, W1]


def test_wallet_address_key_is_case_and_whitespace_insensitive():
    assert wallet_address_key({"wallet": "  0xAbC  "}) == "0xabc"
    assert wallet_address_key("0xAbC") == "0xabc"
    assert wallet_address_key(None) == ""
    assert wallet_address_key({}) == ""


# ---------------------------------------------------------------------------
# End-to-end through cmd_scan_wallets
# ---------------------------------------------------------------------------

def test_scan_retains_wallet_that_is_not_rediscovered(tmp_path, monkeypatch):
    """A stored tracked wallet absent from the scan results must survive."""
    from test_stale_data_handling import FakeClient, GOOD_CLOSED, GOOD_EVENTS, _scan

    wallets_file = tmp_path / "wallets.json"
    stored = _record(W3, tracked=True, notes="hand-added", last_score=71.0)
    save_wallet_config([stored], path=wallets_file)
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, wallets=[W1], client=FakeClient(
        closed_by_wallet={W1: GOOD_CLOSED}, events_by_wallet={W1: GOOD_EVENTS},
    ))

    saved = json.loads(wallets_file.read_text())
    by_wallet = {r["wallet"]: r for r in saved}
    assert set(by_wallet) == {W1, W3}
    assert by_wallet[W3] == stored          # untouched, not even re-serialized oddly
    assert by_wallet[W1]["hard_filter_status"] == "PASS"


def test_scan_preserves_tracked_and_custom_fields(tmp_path, monkeypatch):
    from test_stale_data_handling import FakeClient, GOOD_CLOSED, GOOD_EVENTS, _scan

    wallets_file = tmp_path / "wallets.json"
    save_wallet_config(
        [{"wallet": W1, "enabled": True, "my_note": "keep"}], path=wallets_file
    )
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, wallets=[W1], client=FakeClient(
        closed_by_wallet={W1: GOOD_CLOSED}, events_by_wallet={W1: GOOD_EVENTS},
    ))

    saved = json.loads(wallets_file.read_text())
    assert len(saved) == 1
    entry = saved[0]
    assert entry["tracked"] is True          # legacy `enabled` honored
    assert "enabled" not in entry            # migrated on write
    assert entry["my_note"] == "keep"        # custom field preserved
    assert entry["num_positions"] == len(GOOD_CLOSED)


def test_scan_never_enables_tracking_for_new_candidates(tmp_path, monkeypatch):
    from test_stale_data_handling import FakeClient, GOOD_CLOSED, GOOD_EVENTS, _scan

    wallets_file = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", wallets_file)

    _scan(monkeypatch, wallets=[W1, W2], client=FakeClient(
        closed_by_wallet={W1: GOOD_CLOSED, W2: GOOD_CLOSED},
        events_by_wallet={W1: GOOD_EVENTS, W2: GOOD_EVENTS},
    ))

    saved = json.loads(wallets_file.read_text())
    assert saved
    assert all(entry["tracked"] is False for entry in saved)
