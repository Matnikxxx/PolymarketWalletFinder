"""
D1 regression tests: the wallet-state file must never be silently treated
as empty, and must never be overwritten when it cannot be parsed.

The contract under test
-----------------------
* missing file  -> empty state (legitimate first run)
* unreadable / invalid / wrong-shape file -> WalletStateError, original
  untouched, byte-identical backup written next to it
* a scan aborts BEFORE discovery and never saves over the broken file
* tracked flags, notes and non-rediscovered records survive a normal scan
"""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

import main
import storage
from config.settings import settings
from storage import WalletStateError
from wallet.monitor import load_wallet_config, save_wallet_config

TRACKED = "0x" + "aa" * 20
OTHER = "0x" + "bb" * 20


# ---------------------------------------------------------------------------
# Loader behaviour
# ---------------------------------------------------------------------------
def test_missing_file_is_empty_state(tmp_path):
    path = tmp_path / "wallets.json"

    assert load_wallet_config(path=path) == []
    assert list(tmp_path.iterdir()) == []  # nothing created, no backup


def test_corrupt_json_is_not_treated_as_empty(tmp_path):
    path = tmp_path / "wallets.json"
    path.write_text("{not valid json", encoding="utf-8")
    original = path.read_bytes()
    original_mtime = path.stat().st_mtime_ns

    with pytest.raises(WalletStateError) as caught:
        load_wallet_config(path=path)

    err = caught.value
    assert "invalid JSON" in str(err)
    assert str(path) in str(err)
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == original_mtime
    assert err.backup_path is not None
    assert err.backup_path.exists()
    assert err.backup_path.read_bytes() == original
    assert err.backup_path.parent == path.parent
    assert str(err.backup_path) in str(err)


def test_backup_is_byte_identical_and_recoverable(tmp_path):
    path = tmp_path / "wallets.json"
    payload = json.dumps([{"wallet": TRACKED, "tracked": True, "my_note": "keep"}]).encode()
    path.write_bytes(payload[:-2])  # truncated: the closing "]" is missing

    with pytest.raises(WalletStateError) as caught:
        load_wallet_config(path=path)

    backup = caught.value.backup_path
    assert backup is not None
    assert backup.read_bytes() == path.read_bytes()  # byte-for-byte copy

    # The backup is recoverable: repairing that copy (here, restoring the
    # two missing bytes) yields a file the loader accepts again, with the
    # tracked flag and note intact.
    repaired = tmp_path / "wallets.repaired.json"
    repaired.write_bytes(backup.read_bytes() + b"}]")
    restored = load_wallet_config(path=repaired)
    assert restored == json.loads(payload)
    assert restored[0]["tracked"] is True
    assert restored[0]["my_note"] == "keep"


def test_repeated_reads_reuse_the_same_backup(tmp_path):
    path = tmp_path / "wallets.json"
    path.write_text("{broken", encoding="utf-8")

    for _ in range(3):
        with pytest.raises(WalletStateError):
            load_wallet_config(path=path)

    assert len(list(tmp_path.glob("wallets.json.corrupt-*.bak"))) == 1


def test_empty_file_is_rejected(tmp_path):
    path = tmp_path / "wallets.json"
    path.write_text("   \n", encoding="utf-8")

    with pytest.raises(WalletStateError) as caught:
        load_wallet_config(path=path)

    assert "empty" in str(caught.value)
    assert caught.value.backup_path is not None
    assert caught.value.backup_path.read_bytes() == b"   \n"


def test_non_array_root_is_rejected(tmp_path):
    path = tmp_path / "wallets.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(WalletStateError) as caught:
        load_wallet_config(path=path)

    assert "expected a JSON array" in str(caught.value)
    assert caught.value.backup_path is not None


def test_non_object_entries_are_rejected(tmp_path):
    path = tmp_path / "wallets.json"
    path.write_text('[{"wallet": "0xabc"}, "junk"]', encoding="utf-8")

    with pytest.raises(WalletStateError) as caught:
        load_wallet_config(path=path)

    assert "not a JSON object" in str(caught.value)
    assert caught.value.backup_path is not None


def test_unreadable_file_raises_without_backup(tmp_path, monkeypatch):
    path = tmp_path / "wallets.json"
    path.write_text("[]", encoding="utf-8")

    def _deny(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(type(path), "read_bytes", _deny)

    with pytest.raises(WalletStateError) as caught:
        load_wallet_config(path=path)

    assert "could not be read" in str(caught.value)
    assert caught.value.backup_path is None
    assert "No backup could be created" in str(caught.value)


def test_backup_failure_is_reported_and_original_kept(tmp_path, monkeypatch):
    path = tmp_path / "wallets.json"
    path.write_text("{broken", encoding="utf-8")
    original = path.read_bytes()

    def _fail_backup(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "_preserve_state_copy", _fail_backup)

    with pytest.raises(WalletStateError) as caught:
        load_wallet_config(path=path)

    assert "backup copy could not be created" in str(caught.value)
    assert "disk full" in str(caught.value)
    assert caught.value.backup_path is None
    assert path.read_bytes() == original


def test_save_refuses_to_overwrite_corrupt_state(tmp_path):
    path = tmp_path / "wallets.json"
    path.write_text("{broken", encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(WalletStateError):
        save_wallet_config([{"wallet": TRACKED, "tracked": True}], path=path)

    assert path.read_bytes() == original


# ---------------------------------------------------------------------------
# Scan must abort before discovery and never write
# ---------------------------------------------------------------------------
def test_scan_aborts_before_discovery_and_never_saves(tmp_path, monkeypatch):
    path = tmp_path / "wallets.json"
    path.write_text("{broken", encoding="utf-8")
    original = path.read_bytes()

    monkeypatch.setattr(settings, "WALLETS_FILE", path)
    calls = []
    monkeypatch.setattr(main, "scan_top_wallets", lambda *a, **kw: calls.append("discovery"))
    monkeypatch.setattr(main, "save_wallet_config", lambda *a, **kw: calls.append("save"))
    monkeypatch.setattr(main, "PolymarketApiClient", lambda *a, **kw: calls.append("client"))

    with pytest.raises(WalletStateError):
        main.cmd_scan_wallets(None)

    assert calls == []  # no network, no discovery, no save
    assert path.read_bytes() == original
    backups = list(tmp_path.glob("wallets.json.corrupt-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


# ---------------------------------------------------------------------------
# Successful scan must preserve tracked flags, notes and retained records
# ---------------------------------------------------------------------------
class _FakeClient:
    def __init__(self, closed):
        self.closed = closed

    def get_wallet_closed_positions_paginated(self, wallet, max_pages=5, raise_on_error=False, **_kw):
        return list(self.closed)

    def get_wallet_positions(self, wallet):
        return []

    def get_wallet_value(self, wallet):
        return 0.0


def _closed_row(index, wallet=TRACKED):
    return {
        "proxyWallet": wallet,
        "conditionId": f"0xc{index:04d}",
        "asset": f"asset-{index}",
        "avgPrice": 0.5,
        "totalBought": 100.0,
        "realizedPnl": 20.0,
        "curPrice": 0.0,
        "timestamp": 1767225600 + index * 86400,
    }


def test_successful_scan_preserves_tracked_flags_notes_and_records(tmp_path, monkeypatch):
    path = tmp_path / "wallets.json"
    monkeypatch.setattr(settings, "WALLETS_FILE", path)
    save_wallet_config([
        {
            "wallet": TRACKED,
            "tracked": True,
            "my_note": "keep me",
            "first_seen_at": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-01-01T00:00:00+00:00",
            "times_seen": 4,
        },
        {
            "wallet": OTHER,
            "tracked": False,
            "my_note": "not rediscovered",
            "last_score": 44.4,
            "times_seen": 2,
        },
    ], path=path)

    client = _FakeClient([_closed_row(i) for i in range(3)])
    monkeypatch.setattr(main, "PolymarketApiClient", lambda *a, **kw: client)
    monkeypatch.setattr(main, "scan_top_wallets", lambda *a, **kw: [TRACKED])
    monkeypatch.setattr(
        main, "fetch_wallet_events_paginated",
        lambda *a, **kw: [],
    )

    main.cmd_scan_wallets(None)

    records = {r["wallet"]: r for r in load_wallet_config(path=path)}
    assert set(records) == {TRACKED, OTHER}

    kept = records[TRACKED]
    assert kept["tracked"] is True
    assert kept["my_note"] == "keep me"
    assert kept["first_seen_at"] == "2026-01-01T00:00:00+00:00"
    assert kept["times_seen"] == 5

    retained = records[OTHER]
    assert retained["my_note"] == "not rediscovered"
    assert retained["last_score"] == 44.4
    assert retained["times_seen"] == 2


# ---------------------------------------------------------------------------
# CLI and dashboard surface the diagnostic
# ---------------------------------------------------------------------------
def test_report_command_exits_2_with_diagnostic(tmp_path, monkeypatch, capsys):
    path = tmp_path / "wallets.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(settings, "WALLETS_FILE", path)
    monkeypatch.setattr(settings, "VERIFICATION_FILE", tmp_path / "verification.json")
    monkeypatch.setattr(settings, "WALLET_ACTIVITY_LOG_FILE", tmp_path / "activity.json")
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(sys, "argv", ["main.py", "--report"])

    with pytest.raises(SystemExit) as caught:
        main.main()

    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "WALLET STATE ERROR" in err
    assert str(path) in err
    assert "corrupt-" in err  # names the backup copy


def test_dashboard_returns_500_for_corrupt_wallet_state(tmp_path, monkeypatch):
    path = tmp_path / "wallets.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(settings, "WALLETS_FILE", path)
    monkeypatch.setattr(settings, "VERIFICATION_FILE", tmp_path / "verification.json")
    monkeypatch.setattr(settings, "SEEN_EVENTS_FILE", tmp_path / "seen_events.json")
    monkeypatch.setattr(settings, "WALLET_ACTIVITY_LOG_FILE", tmp_path / "activity.json")

    from dashboard.app import create_app

    app = create_app()
    app.testing = True
    resp = app.test_client().get("/")

    assert resp.status_code == 500
    html = resp.get_data(as_text=True)
    assert "Wallet state could not be loaded" in html
    assert "wallets.json" in html
    assert "Recoverable backup" in html
    assert list(tmp_path.glob("wallets.json.corrupt-*.bak"))


# ---------------------------------------------------------------------------
# The strict loader is scoped to wallet state only
# ---------------------------------------------------------------------------
def test_non_wallet_json_stays_lenient(tmp_path):
    path = tmp_path / "verification.json"
    path.write_text("{broken", encoding="utf-8")

    assert storage.load_json(path, default=[]) == []
    assert not list(tmp_path.glob("*.bak"))


# ---------------------------------------------------------------------------
# Backup permissions and platform behaviour (POSIX / Windows / OneDrive)
# ---------------------------------------------------------------------------
def test_backup_mode_is_owner_only_on_posix(tmp_path):
    """POSIX: the backup is created 0600 (owner-only), subject to umask."""
    if os.name == "nt":
        pytest.skip("POSIX permission bits do not exist on Windows")

    path = tmp_path / "wallets.json"
    path.write_text("{broken", encoding="utf-8")

    old_umask = os.umask(0o022)
    try:
        with pytest.raises(WalletStateError) as caught:
            load_wallet_config(path=path)
    finally:
        os.umask(old_umask)

    mode = stat.S_IMODE(caught.value.backup_path.stat().st_mode)
    assert mode == 0o600, f"backup mode is {oct(mode)}, expected 0o600"


def test_backup_creation_requests_mode_0600_on_every_platform(tmp_path, monkeypatch):
    """The 0600 mode is passed explicitly even where the OS ignores it.

    Windows does not map POSIX mode bits to NTFS ACLs, but the intent must
    stay visible in code and tests (the backup inherits the folder ACL).
    """
    path = tmp_path / "wallets.json"
    path.write_text("{broken", encoding="utf-8")

    captured = {}
    real_open = os.open

    def _spy(file, flags, mode=0o777, *args, **kwargs):
        captured["mode"] = mode
        return real_open(file, flags, mode, *args, **kwargs)

    monkeypatch.setattr(storage.os, "open", _spy)

    with pytest.raises(WalletStateError):
        load_wallet_config(path=path)

    assert captured["mode"] == 0o600


def test_failed_backup_write_leaves_no_partial_file_and_keeps_original(tmp_path, monkeypatch):
    """If the backup write/fsync fails (quota, sync lock, ACL), no partial
    .bak is left behind and the original stays byte-identical."""
    path = tmp_path / "wallets.json"
    path.write_text("{broken", encoding="utf-8")
    original = path.read_bytes()

    def _fsync_fails(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage.os, "fsync", _fsync_fails)

    with pytest.raises(WalletStateError) as caught:
        load_wallet_config(path=path)

    assert "backup copy could not be created" in str(caught.value)
    assert path.read_bytes() == original
    assert list(tmp_path.glob("wallets.json.corrupt-*.bak")) == []


def test_cloud_provider_read_error_is_safe(tmp_path, monkeypatch):
    """OneDrive/Dropbox placeholders can fail to materialise with OSError.
    That must be reported, not treated as an empty wallet list."""
    path = tmp_path / "wallets.json"
    path.write_text("[]", encoding="utf-8")
    original = path.read_bytes()

    real_read_bytes = Path.read_bytes

    def _cloud_unavailable(self, *args, **kwargs):
        raise OSError(5, "cloud provider unavailable")

    monkeypatch.setattr(Path, "read_bytes", _cloud_unavailable)

    with pytest.raises(WalletStateError) as caught:
        load_wallet_config(path=path)

    assert "could not be read" in str(caught.value)
    assert "cloud provider unavailable" in str(caught.value)
    assert caught.value.backup_path is None
    assert real_read_bytes(path) == original
