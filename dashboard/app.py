"""
Local, read-only Flask dashboard.

This module adds NO new source of truth: every request builds a fresh
`DashboardData` via analytics.dashboard.build_dashboard_data(), which is
the exact same function the console dashboard (`python main.py
--dashboard-cli`) uses. Wallet config, verification results, and tracked
activity are read from the SAME files the rest of the tool already uses
(data/wallets.json, data/verification.json, data/wallet_activity_log.json)
-- nothing is recomputed independently here.

There is exactly one route (`GET /`). There is no POST/PUT/DELETE route
anywhere in this module, and nothing here can place an order, enable a
wallet, or start tracking -- see polymarket/api_client.py, which has no
order-placement method at all, and wallet/monitor.py, which this module
never imports or invokes.
"""
from __future__ import annotations

from flask import Flask, render_template

from analytics.dashboard import build_dashboard_data
from config.settings import settings
from storage import WalletStateError


def create_app() -> Flask:
    app = Flask(__name__)

    @app.route("/", methods=["GET"])
    def index():
        # A fresh DashboardData every request, so "Refresh" (button or
        # auto-refresh) always reflects the current on-disk state -- and
        # reading state never starts or restarts wallet tracking (that
        # only happens via a separate `--track-wallets` process).
        try:
            data = build_dashboard_data()
        except WalletStateError as exc:
            # 500, not an empty-but-successful page: a broken state file is
            # an error, and the message names the file and the backup copy.
            return render_template(
                "error.html",
                error=str(exc),
                error_path=str(exc.path),
                backup_path=str(exc.backup_path) if exc.backup_path else None,
            ), 500
        return render_template(
            "index.html",
            data=data,
            settings=settings,
            top_wallets=data.wallet_table[:10],
        )

    return app


def run_dashboard(host: str = "127.0.0.1", port: int = 5000) -> None:
    """Starts the dashboard web server. Does NOT start wallet tracking."""
    app = create_app()
    print(f"Dashboard running at http://{host}:{port}  (Ctrl+C to stop)")
    print("This is a READ-ONLY monitoring view. It does not start --track-wallets.")
    app.run(host=host, port=port, debug=False)
