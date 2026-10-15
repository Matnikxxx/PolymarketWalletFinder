"""
Polymarket Wallet Finder / Tracker CLI entrypoint.

Purpose: discover, analyze, filter, rank, verify, and track Polymarket
wallets. This tool NEVER executes or simulates any trade, paper or live --
see CLAUDE.md for the architecture cleanup that removed all copy-trading
functionality from this project.
"""
from __future__ import annotations

import argparse
import logging
import sys

from config.settings import settings
from polymarket.api_client import PolymarketApiClient, ApiError
from polymarket.wallet_activity import fetch_wallet_events, fetch_wallet_events_paginated
from analytics.models import WalletStatistics
from analytics.frequency_models import FrequencyProfile
from analytics.performance import compute_wallet_statistics, build_position_records
from analytics.frequency import (
    compute_frequency_profile,
    compute_frequency_stats,
    build_frequency_diagnostic,
    format_frequency_diagnostic,
)
from analytics.wallet_stats import (
    compute_wallet_stats, enrich_with_closed_positions, attach_open_positions,
    attach_portfolio_value,
)
from filters.wallet_filter import filter_wallet, WalletFilterResult
from wallet.ranking import rank_wallets
from wallet.scanner import scan_top_wallets
from wallet.verifier import verify_wallet
from wallet.monitor import WalletMonitor, load_wallet_config, save_wallet_config, tracked_wallets
from monitor.status import print_wallet_ranking, print_verification, print_dashboard, print_wallet_report
from analytics.dashboard import build_dashboard_data

logger = logging.getLogger("main")


def setup_logging() -> None:
    from config.settings import DATA_DIR
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(settings.LOG_FILE)],
    )


def cmd_scan_wallets(args) -> None:
    client = PolymarketApiClient()
    wallets = scan_top_wallets(client, limit=settings.MAX_WALLET_CANDIDATES)

    stats_map = {}
    freq_map = {}
    diag_map = {}
    filter_map = {}
    extra_details = {}
    for w in wallets:
        short = w[:10] + "..."
        # Phase 12: paginate activity and closed-positions so that recent sell
        # events overlap with the closed-position records, allowing accurate
        # TRADING_EXIT classification in build_position_records().
        events = fetch_wallet_events_paginated(
            client, w, max_pages=settings.MAX_HISTORY_PAGES
        )

        # --- authoritative closed-position P&L/win-rate --------------------
        raw_closed = []
        try:
            raw_closed = client.get_wallet_closed_positions_paginated(
                w, max_pages=settings.MAX_HISTORY_PAGES
            )
        except ApiError as exc:
            logger.warning("WALLET_STATS_PARTIAL wallet=%s reason=closed_positions_api_failed:%s", short, exc)

        # --- active open positions ------------------------------------------
        raw_positions = []
        try:
            raw_positions = client.get_wallet_positions(w)
        except ApiError as exc:
            logger.warning("WALLET_STATS_PARTIAL wallet=%s reason=positions_api_failed:%s", short, exc)

        # --- portfolio value -------------------------------------------------
        portfolio_val = None
        try:
            portfolio_val = client.get_wallet_value(w)
        except ApiError as exc:
            logger.warning("WALLET_STATS_PARTIAL wallet=%s reason=value_api_failed:%s", short, exc)

        # Build canonical Phase 6 PositionRecords
        positions = build_position_records(raw_closed, events, raw_positions)

        # Compute canonical Phase 6 WalletStatistics
        stats = compute_wallet_statistics(w, positions)
        stats_map[w] = stats

        # Compute canonical Phase 7 FrequencyProfile
        freq_profile = compute_frequency_profile(w, events)
        freq_map[w] = freq_profile
        diag_map[w] = build_frequency_diagnostic(freq_profile)

        # Diagnostics display and logging
        diag = diag_map[w]
        print(format_frequency_diagnostic(diag))
        print(
            f"TRADE_COUNT_BREAKDOWN wallet={short} raw_activity={len(events)} "
            f"logical_trades={diag['logical_trades']} unique_markets={diag['unique_markets']} "
            f"closed_positions={stats.positions_counted}"
        )
        trades_threshold = diag["rule_5_trades_60s_threshold"]
        pct_threshold = diag["rule_30pct_gaps_le_10s_threshold"]
        logger.info(
            "FREQUENCY_DIAGNOSTIC wallet=%s raw_fill_count=%d logical_trades=%d unique_markets=%d "
            "gap_count=%d min_gap=%s median_gap=%s pct_gaps_le_10s=%s max_trades_60s=%d "
            f"rule_{int(trades_threshold)}_trades_60s=" + "%s "
            f"rule_{pct_threshold:g}pct_gaps_le_10s=" + "%s classification=%s",
            short, diag["raw_fill_count"], diag["logical_trades"], diag["unique_markets"],
            diag["gap_count"], diag["min_gap"], diag["median_gap"], diag["pct_gaps_le_10s"],
            diag["max_trades_60s"], "YES" if diag["rule_5_trades_60s"] else "NO",
            "YES" if diag["rule_30pct_gaps_le_10s"] else "NO", diag["classification"],
        )

        logger.info(
            "WALLET_STATS wallet=%s trades=%d volume=%.2f win_rate=%s roi=%s quality=%s",
            short, len(events), sum(e.get("usdc_size", 0.0) for e in events),
            f"{stats.win_rate_pct:.1f}" if stats.win_rate_pct is not None else "n/a",
            f"{stats.roi_fee_exclusive_pct:.1f}" if stats.roi_fee_exclusive_pct is not None else "n/a",
            stats.retrieval_status,
        )

        extra_details[w] = {
            "total_trades": len(events) if events else None,
            "buy_trades": sum(1 for e in events if e.get("side") == "BUY") if events else None,
            "sell_trades": sum(1 for e in events if e.get("side") == "SELL") if events else None,
            "total_volume": round(sum(e.get("usdc_size", 0.0) for e in events), 2) if events else None,
            "largest_trade": round(max((e.get("usdc_size", 0.0) for e in events), default=0.0), 2) if events else None,
            "active_positions": len(raw_positions) if raw_positions else None,
            "portfolio_value": portfolio_val,
        }

        # --- Phase 8C & 10: Hard-filter gate on canonical WalletStatistics ---
        filter_result = filter_wallet(stats)
        if filter_result.status == "FAIL":
            print(
                f"HARD_FILTER_REJECT wallet={short} "
                f"reasons={'; '.join(filter_result.reasons)}"
            )
            logger.info(
                "HARD_FILTER_REJECT wallet=%s reasons=%s",
                short, "; ".join(filter_result.reasons),
            )
        elif filter_result.status == "INSUFFICIENT_DATA":
            print(
                f"HARD_FILTER_INSUFFICIENT_DATA wallet={short} "
                f"missing={','.join(filter_result.insufficient_criteria)}"
            )
            logger.info(
                "HARD_FILTER_INSUFFICIENT_DATA wallet=%s missing=%s",
                short, ",".join(filter_result.insufficient_criteria),
            )
        else:
            print(f"HARD_FILTER_PASS wallet={short}")
            logger.info("HARD_FILTER_PASS wallet=%s", short)

        filter_map[w] = filter_result

    # Wallets that hard-failed are excluded from the ranking/recommendation pool.
    pass_wallets = [
        w for w in wallets
        if filter_map[w].status == "PASS"
    ]
    insufficient_wallets = [
        w for w in wallets
        if filter_map[w].status == "INSUFFICIENT_DATA"
    ]
    fail_wallets = [
        w for w in wallets
        if filter_map[w].status == "FAIL"
    ]

    logger.info(
        "HARD_FILTER_SUMMARY pass=%d insufficient=%d fail=%d total=%d",
        len(pass_wallets), len(insufficient_wallets), len(fail_wallets), len(wallets),
    )

    # Canonical Phase 10 ranking: pass_stats are WalletStatistics, pass_freq are FrequencyProfile
    pass_stats = [stats_map[w] for w in pass_wallets]
    pass_freq = {w: freq_map[w] for w in pass_wallets}
    cards = rank_wallets(pass_stats, pass_freq)
    print_wallet_ranking(cards)

    existing = {w["wallet"].lower(): w for w in load_wallet_config()}
    new_config = []

    # --- Ranked (PASS) wallets -------------------------------------------
    for card in cards:
        stats = stats_map[card.wallet]
        extra = extra_details.get(card.wallet, {})
        cfg = existing.get(card.wallet, {"wallet": card.wallet, "tracked": False})
        if "tracked" not in cfg:
            cfg["tracked"] = bool(cfg.get("enabled", False))
        cfg.pop("enabled", None)
        cfg["last_score"] = card.final_score
        cfg["recommendation"] = card.recommendation
        cfg["frequency_classification"] = card.frequency_classification
        cfg["frequency_median_gap_seconds"] = card.frequency_median_gap_seconds

        wdiag = diag_map.get(card.wallet)
        if wdiag is not None:
            cfg["frequency_raw_fill_count"] = wdiag["raw_fill_count"]
            cfg["frequency_logical_trades"] = wdiag["logical_trades"]
            cfg["frequency_max_trades_60s"] = wdiag["max_trades_60s"]
            cfg["frequency_pct_gaps_le_10s"] = wdiag["pct_gaps_le_10s"]
            cfg["frequency_rule_5_trades_60s"] = wdiag["rule_5_trades_60s"]
            cfg["frequency_rule_30pct_gaps_le_10s"] = wdiag["rule_30pct_gaps_le_10s"]

        fres = filter_map.get(card.wallet)
        cfg["hard_filter_status"] = fres.status if fres else "PASS"
        cfg["hard_filter_reasons"] = fres.reasons if fres else []
        cfg["hard_filter_insufficient_criteria"] = fres.insufficient_criteria if fres else []

        has_closed_data = stats.positions_counted > 0
        cfg["data_quality"] = stats.retrieval_status
        cfg["data_quality_reason"] = f"Win/loss, realized P&L, ROI, and drawdown computed from {stats.positions_counted} eligible position(s) via Phase 6 engine."
        cfg["pnl_source"] = "closed_positions_api"
        cfg["win_rate_pct"] = stats.win_rate_pct if has_closed_data else None
        cfg["roi_pct"] = stats.roi_fee_exclusive_pct if has_closed_data else None
        cfg["drawdown_pct"] = stats.drawdown_cost_pct if has_closed_data else None
        cfg["realized_pnl"] = stats.total_settled_net_result if has_closed_data else None
        cfg["num_positions"] = stats.positions_counted if stats.positions_counted else None
        cfg["avg_position_size"] = stats.mean_position_cost if stats.positions_counted else None
        cfg["risk_score"] = card.risk_score if stats.positions_counted else None
        cfg["consistency_score"] = card.consistency_score if has_closed_data else None
        cfg["total_trades"] = extra.get("total_trades")
        cfg["buy_trades"] = extra.get("buy_trades")
        cfg["sell_trades"] = extra.get("sell_trades")
        cfg["total_volume"] = extra.get("total_volume")
        cfg["largest_trade"] = extra.get("largest_trade")
        cfg["active_positions"] = extra.get("active_positions")
        cfg["portfolio_value"] = extra.get("portfolio_value")
        cfg["last_activity_at"] = stats.last_activity_at
        new_config.append(cfg)

    # --- FAIL and INSUFFICIENT_DATA wallets (not ranked) -----------------
    for w in fail_wallets + insufficient_wallets:
        stats = stats_map[w]
        fres = filter_map[w]
        extra = extra_details.get(w, {})
        cfg = existing.get(w, {"wallet": w, "tracked": False})
        if "tracked" not in cfg:
            cfg["tracked"] = bool(cfg.get("enabled", False))
        cfg.pop("enabled", None)
        cfg["last_score"] = None
        cfg["recommendation"] = "HARD_FILTER_REJECT" if fres.status == "FAIL" else "HARD_FILTER_INSUFFICIENT_DATA"
        cfg["hard_filter_status"] = fres.status
        cfg["hard_filter_reasons"] = fres.reasons
        cfg["hard_filter_insufficient_criteria"] = fres.insufficient_criteria
        has_closed_data = stats.positions_counted > 0
        cfg["data_quality"] = stats.retrieval_status
        cfg["data_quality_reason"] = f"Win/loss, realized P&L, ROI, and drawdown computed from {stats.positions_counted} eligible position(s) via Phase 6 engine."
        cfg["pnl_source"] = "closed_positions_api"
        cfg["win_rate_pct"] = stats.win_rate_pct if has_closed_data else None
        cfg["roi_pct"] = stats.roi_fee_exclusive_pct if has_closed_data else None
        cfg["last_activity_at"] = stats.last_activity_at
        new_config.append(cfg)

    save_wallet_config(new_config)
    print(f"\nSaved {len(new_config)} candidate wallets to {settings.WALLETS_FILE}")
    print(
        f"  {len(cards)} passed hard filter and were ranked."
    )
    if fail_wallets:
        print(f"  {len(fail_wallets)} rejected by hard filter (see hard_filter_status=FAIL).")
    if insufficient_wallets:
        print(f"  {len(insufficient_wallets)} have insufficient data for full filtering.")
    print("Set \"tracked\": true for any wallet you want to track.")
    print(
        f"(Candidate pool is currently ~{len(wallets)} wallets -- see "
        "MAX_WALLET_CANDIDATES in .env / wallet/scanner.py's docstring for why.)"
    )


def cmd_verify_wallets(args) -> None:
    client = PolymarketApiClient()
    wallets_cfg = load_wallet_config()
    if not wallets_cfg:
        print("No wallets found. Run --scan-wallets first.")
        return

    results = []
    for w in wallets_cfg:
        wallet = w["wallet"]
        events = fetch_wallet_events(client, wallet)
        stats = compute_wallet_stats(wallet, events) if events else None
        freq = compute_frequency_stats(wallet, events) if events else None
        result = verify_wallet(wallet, events, stats, freq)
        results.append(result)

    print_verification(results)
    from storage import save_json_atomic
    save_json_atomic(settings.VERIFICATION_FILE, [r.as_dict() for r in results])


def cmd_track_wallets(args) -> None:
    """Polls selected wallets and records their new activity.
    Never places, simulates, or copies any trade."""
    monitor = WalletMonitor()

    wallets_cfg = load_wallet_config()
    wallet_scores = {w["wallet"].lower(): w.get("last_score") for w in wallets_cfg}
    active = tracked_wallets(wallets_cfg)

    if not active:
        print("No wallets are tracked. Edit data/wallets.json and set \"tracked\": true for wallets you want to track.")
        return

    print(f"Tracking {len(active)} wallet(s) for new activity. Ctrl+C to stop.")
    try:
        monitor.run_forever(wallet_scores, iterations=args.iterations)
    except KeyboardInterrupt:
        print("\nStopping...")


def cmd_status(args) -> None:
    """Quick discovery/tracking status summary (no trading state exists to show)."""
    data = build_dashboard_data(recent_activity_limit=5)
    print("=" * 60)
    print("POLYMARKET WALLET FINDER - STATUS")
    print("=" * 60)
    ds = data.discovery_status
    print(f"Discovered wallets : {ds['discovered_wallets']}")
    print(f"Tracked wallets    : {ds['tracked_wallets']}")
    print(f"Tracking interval  : {ds['check_interval_seconds']}s")
    print(f"Recent activity    : {len(data.recent_activity)} event(s) (showing up to 5)")


def cmd_report(args) -> None:
    data = build_dashboard_data()
    print_wallet_report(data.wallet_table)


def cmd_dashboard(args) -> None:
    """
    Starts the local web dashboard (spec: `python main.py --dashboard` ->
    http://127.0.0.1:5000). Read-only: does NOT start --track-wallets. See
    dashboard/app.py -- it renders the exact same DashboardData this
    console version used, so there is one source of truth either way.
    """
    from dashboard.app import run_dashboard
    run_dashboard(host=args.host, port=args.port)


def cmd_dashboard_cli(args) -> None:
    """Console (text) version of the dashboard -- kept for anyone who was
    already using it before the web version existed."""
    data = build_dashboard_data()
    print_dashboard(data)


def cmd_reset(args) -> None:
    import os
    for f in (settings.SEEN_EVENTS_FILE, settings.WALLET_ACTIVITY_LOG_FILE):
        if f.exists():
            os.remove(f)
    print("Wallet tracking data reset (seen-events baseline + activity log).")
    print("Wallet config and verification results were preserved.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py", description="Polymarket Wallet Finder / Tracker (analysis only -- never trades)"
    )
    parser.add_argument("--scan-wallets", action="store_true", help="Discover and rank candidate wallets")
    parser.add_argument("--verify-wallets", action="store_true", help="Verify configured wallets")
    parser.add_argument("--track-wallets", action="store_true", help="Poll selected wallets for new activity")
    parser.add_argument("--status", action="store_true", help="Show discovery/tracking status")
    parser.add_argument("--report", action="store_true", help="Show full per-wallet report")
    parser.add_argument("--dashboard", action="store_true", help="Start the local web dashboard (http://127.0.0.1:5000)")
    parser.add_argument("--dashboard-cli", action="store_true", help="Show the read-only dashboard as console text instead of the web UI")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Dashboard host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000, help="Dashboard port (default 5000)")
    parser.add_argument("--reset", action="store_true", help="Reset wallet tracking data (keeps wallet config)")
    parser.add_argument("--iterations", type=int, default=None, help="Limit --track-wallets to N poll iterations (testing)")
    return parser


def main() -> None:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args()

    if args.scan_wallets:
        cmd_scan_wallets(args)
    elif args.verify_wallets:
        cmd_verify_wallets(args)
    elif args.track_wallets:
        cmd_track_wallets(args)
    elif args.status:
        cmd_status(args)
    elif args.report:
        cmd_report(args)
    elif args.dashboard:
        cmd_dashboard(args)
    elif args.dashboard_cli:
        cmd_dashboard_cli(args)
    elif args.reset:
        cmd_reset(args)
    else:
        print("Polymarket Wallet Finder / Tracker")
        print("Run with one of: --scan-wallets --verify-wallets --track-wallets --status --report --dashboard --dashboard-cli --reset")
        print("Example: python main.py --scan-wallets")


if __name__ == "__main__":
    main()
