"""
Tests for the read-only web dashboard. These use Flask's built-in test
client (no real network socket, no subprocess) so they're fast and
deterministic, same as the rest of the suite.

Architecture cleanup note: the dashboard no longer has any concept of a
paper account, open positions, or copy activity -- it only reports on
scanned/ranked/tracked wallets and the activity observed on tracked
wallets (see analytics/dashboard.py, wallet/monitor.py).
"""
from helpers import make_event


def _client():
    from dashboard.app import create_app
    app = create_app()
    app.testing = True
    return app.test_client()


def _isolate_data_files(monkeypatch, tmp_path):
    from config.settings import settings
    monkeypatch.setattr(settings, "WALLETS_FILE", tmp_path / "wallets.json")
    monkeypatch.setattr(settings, "VERIFICATION_FILE", tmp_path / "verification.json")
    monkeypatch.setattr(settings, "SEEN_EVENTS_FILE", tmp_path / "seen_events.json")
    monkeypatch.setattr(settings, "WALLET_ACTIVITY_LOG_FILE", tmp_path / "wallet_activity_log.json")


def test_dashboard_module_imports():
    import dashboard.app  # noqa: F401
    import analytics.dashboard  # noqa: F401
    assert hasattr(dashboard.app, "create_app")
    assert hasattr(dashboard.app, "run_dashboard")


def test_dashboard_has_no_trading_dependency():
    """The dashboard module must not import anything from the removed
    trading package -- see architecture cleanup report."""
    import dashboard.app
    import analytics.dashboard
    import sys
    for mod in (dashboard.app, analytics.dashboard):
        source_module_name = mod.__name__
        assert source_module_name in sys.modules
    assert "trading" not in dir(dashboard.app)
    assert "trading" not in dir(analytics.dashboard)


def test_dashboard_route_loads_with_no_data(tmp_path, monkeypatch):
    _isolate_data_files(monkeypatch, tmp_path)

    client = _client()
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "TRADING: DISABLED" in html
    assert "No wallets scanned yet" in html
    assert "No activity tracked yet" in html


def test_dashboard_shows_scanned_wallet_data(tmp_path, monkeypatch):
    from wallet.monitor import save_wallet_config
    _isolate_data_files(monkeypatch, tmp_path)

    save_wallet_config(
        [{
            "wallet": "0xdashwallet00000000000000000000000000",
            "enabled": True,
            "last_score": 88.5,
            "recommendation": "RECOMMENDED",
            "frequency_classification": "NORMAL",
            "win_rate_pct": 60.0,
            "roi_pct": 12.5,
            "drawdown_pct": 5.0,
            "realized_pnl": 250.0,
            "num_positions": 10,
            "data_quality": "FULL",
        }],
        path=tmp_path / "wallets.json",
    )

    client = _client()
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "0xdashwallet00000000000000000000000000"[:14] in html
    assert "RECOMMENDED" in html


def test_dashboard_shows_recent_wallet_activity(tmp_path, monkeypatch):
    import json
    _isolate_data_files(monkeypatch, tmp_path)

    activity = [{
        "event_id": "tx:abc", "wallet": "0xactivitywallet0000000000000000000000",
        "side": "BUY", "timestamp": "2026-01-01T00:00:00Z",
        "condition_id": "c1", "token_id": "t1", "slug": "s1",
        "market_title": "Dashboard Activity Market", "outcome": "Yes",
        "price": 0.4, "size": 100.0, "usdc_size": 40.0, "tx_hash": "abc",
        "category": "sports", "liquidity": 500.0, "wallet_score": 80.0,
        "is_rapid": False, "rapid_reason": None, "detected_at": "2026-01-01T00:00:05+00:00",
    }]
    (tmp_path / "wallet_activity_log.json").write_text(json.dumps(activity))

    client = _client()
    html = client.get("/").get_data(as_text=True)
    assert "Dashboard Activity Market" in html
    assert "BUY" in html


def test_dashboard_distinguishes_scanned_vs_tracked_wallets(tmp_path, monkeypatch):
    from wallet.monitor import save_wallet_config
    _isolate_data_files(monkeypatch, tmp_path)

    save_wallet_config(
        [
            {"wallet": "0xtracked1111111111111111111111111111", "enabled": True,
             "last_score": 90.0, "recommendation": "RECOMMENDED"},
            {"wallet": "0xnottracked222222222222222222222222", "enabled": False,
             "last_score": 30.0, "recommendation": "REJECT"},
        ],
        path=tmp_path / "wallets.json",
    )

    client = _client()
    html = client.get("/").get_data(as_text=True)
    assert "0xtracked1111111111111111111111111111"[:14] in html
    assert "0xnottracked222222222222222222222222"[:14] in html
    # both scanned wallets appear in the ranking table, but only the tracked
    # one should appear in the Tracked Wallets section
    tracked_section = html.split("Tracked Wallets")[1].split("Wallet Ranking")[0]
    assert "0xtracked1111111111111111111111111111"[:14] in tracked_section
    assert "0xnottracked222222222222222222222222"[:14] not in tracked_section


def test_dashboard_does_not_start_wallet_tracking(tmp_path, monkeypatch):
    """Loading the dashboard route must never create tracker state
    (seen_events.json) -- that would mean it silently started polling/
    tracking wallets as a side effect of a GET request."""
    _isolate_data_files(monkeypatch, tmp_path)
    seen_file = tmp_path / "seen_events.json"

    client = _client()
    client.get("/")
    client.get("/")
    assert not seen_file.exists()


def test_dashboard_has_no_order_or_control_routes():
    """The dashboard must not expose any route that could place an order,
    enable a wallet, or start tracking -- only the read-only GET / route
    should exist."""
    from dashboard.app import create_app
    app = create_app()
    rules = [str(r) for r in app.url_map.iter_rules()]
    non_static = [r for r in rules if "static" not in r]
    assert non_static == ["/"]
    for rule in app.url_map.iter_rules():
        methods = rule.methods or set()
        assert "POST" not in methods
        assert "PUT" not in methods
        assert "DELETE" not in methods


def test_dashboard_handles_corrupted_non_wallet_json_files(tmp_path, monkeypatch):
    """Non-critical JSON files stay lenient: a corrupt verification or
    activity log must not take the dashboard down. (The wallet-state file is
    deliberately strict -- see tests/test_wallet_state_integrity.py.)"""
    _isolate_data_files(monkeypatch, tmp_path)
    (tmp_path / "verification.json").write_text("invalid", encoding="utf-8")
    (tmp_path / "wallet_activity_log.json").write_text("[truncated", encoding="utf-8")

    client = _client()
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Polymarket Wallet Finder Dashboard" in html
    assert "No wallets scanned yet" in html


def test_dashboard_renders_responsive_css_and_safe_escaping(tmp_path, monkeypatch):
    """Verify responsive CSS media query exists and HTML special characters
    in titles or addresses are safely escaped."""
    from wallet.monitor import save_wallet_config
    import json
    _isolate_data_files(monkeypatch, tmp_path)

    save_wallet_config([
        {
            "wallet": "0x<script>alert(1)</script>",
            "tracked": True,
            "last_score": 75.0,
            "recommendation": "RECOMMENDED",
            "data_quality": "FULL",
        }
    ], path=tmp_path / "wallets.json")

    activity = [{
        "event_id": "tx:xss",
        "wallet": "0x<script>",
        "side": "BUY",
        "market_title": "Market <tag> & test",
        "detected_at": "2026-10-15T00:00:00Z",
    }]
    (tmp_path / "wallet_activity_log.json").write_text(json.dumps(activity))

    client = _client()
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Confirm media query present
    assert "@media (max-width: 768px)" in html
    # Confirm script tag is escaped, not raw executable HTML
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html or "&lt;tag&gt;" in html
    assert "Market &lt;tag&gt; &amp; test" in html


def test_dashboard_rejects_non_get_methods(tmp_path, monkeypatch):
    """Confirm the read-only dashboard explicitly rejects POST, PUT, DELETE,
    and PATCH with 405 Method Not Allowed."""
    _isolate_data_files(monkeypatch, tmp_path)
    client = _client()
    for method in ("post", "put", "delete", "patch"):
        call_fn = getattr(client, method)
        resp = call_fn("/")
        assert resp.status_code == 405


def test_dashboard_renders_with_non_dict_json_data(tmp_path, monkeypatch):
    """Non-critical JSON files may hold unexpected root types (integer,
    string, boolean) without crashing the dashboard. The wallet file is
    valid-but-empty here; an invalid wallet file is a hard error by design."""
    _isolate_data_files(monkeypatch, tmp_path)
    (tmp_path / "wallets.json").write_text("[]", encoding="utf-8")
    (tmp_path / "verification.json").write_text("12345", encoding="utf-8")
    (tmp_path / "wallet_activity_log.json").write_text("true", encoding="utf-8")

    client = _client()
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Polymarket Wallet Finder Dashboard" in html
    assert "No wallets scanned yet" in html


