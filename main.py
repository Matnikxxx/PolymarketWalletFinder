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
from datetime import datetime, timezone

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
from wallet.monitor import (
    WalletMonitor,
    load_wallet_config,
    merge_wallet_records,
    save_wallet_config,
    tracked_wallets,
    wallet_address_key,
)
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


# ---------------------------------------------------------------------------
# Phase B: fresh-vs-stale scan bookkeeping
# ---------------------------------------------------------------------------

# Value written to data_quality / hard_filter_status when a refresh could not
# be completed. Deliberately distinct from the Phase 6 retrieval_status
# values (SUFFICIENT / PARTIAL / INSUFFICIENT) and from the legacy
# DataQuality constants: "we could not look" is not the same statement as
# "the wallet has no history".
DATA_QUALITY_STALE = "STALE"

# Historical performance / ranking / diagnostic fields that a scan must NOT
# overwrite when a refresh is incomplete. A failed request means we could not
# observe the wallet this time -- it says nothing about the wallet's history,
# so whatever the last successful refresh stored stays exactly as it was.
PRESERVED_ON_INCOMPLETE_REFRESH = (
    "last_score",
    "recommendation",
    "win_rate_pct",
    "roi_pct",
    "drawdown_pct",
    "realized_pnl",
    "num_positions",
    "avg_position_size",
    "risk_score",
    "consistency_score",
    "frequency_classification",
    "frequency_median_gap_seconds",
    "frequency_raw_fill_count",
    "frequency_logical_trades",
    "frequency_max_trades_60s",
    "frequency_pct_gaps_le_10s",
    "frequency_rule_5_trades_60s",
    "frequency_rule_30pct_gaps_le_10s",
    "total_trades",
    "buy_trades",
    "sell_trades",
    "total_volume",
    "largest_trade",
    "active_positions",
    "portfolio_value",
    "last_activity_at",
)


def _utc_now_iso() -> str:
    """UTC timestamp for last_refresh_attempt / last_successful_refresh."""
    return datetime.now(timezone.utc).isoformat()


def _stale_wallet_update(
    wallet: str, previous: dict, attempted_at: str, reason: str
) -> dict:
    """
    Build the wallets.json update for a wallet whose refresh failed (Phase B,
    section C).

    Every performance-related field is carried over from `previous` verbatim
    (None when the wallet has no successful history yet); only the freshness
    and data-quality fields change. `last_successful_refresh` is deliberately
    left untouched, so a failed request can never look like a successful one,
    and `pnl_source` is never reset to a source that implies fresh data.
    """
    previous = previous if isinstance(previous, dict) else {}
    update = {field: previous.get(field) for field in PRESERVED_ON_INCOMPLETE_REFRESH}
    update["wallet"] = wallet
    update["data_quality"] = DATA_QUALITY_STALE
    update["data_quality_reason"] = reason
    update["hard_filter_status"] = DATA_QUALITY_STALE
    update["hard_filter_reasons"] = [reason]
    update["hard_filter_insufficient_criteria"] = list(
        previous.get("hard_filter_insufficient_criteria") or []
    )
    update["last_refresh_attempt"] = attempted_at
    update["last_successful_refresh"] = previous.get("last_successful_refresh")
    # `pnl_source` describes where the RETAINED numbers came from; when there
    # is no retained history it stays "none" -- a failed request is never a
    # source of P&L data.
    update["pnl_source"] = previous.get("pnl_source") or "none"
    if not update.get("recommendation"):
        update["recommendation"] = "DATA_STALE"
    return update


def _fresh_data_quality(stats, closed_rows_returned: bool) -> tuple[str, str, str]:
    """
    Honest ``(data_quality, data_quality_reason, pnl_source)`` for a wallet
    whose closed-positions refresh SUCCEEDED.

    ``closed_rows_returned`` is False when the request succeeded but the
    history was empty. That is a genuine, fresh "no closed positions" answer
    -- never STALE, because the request itself did work -- but it is also not
    authoritative performance history: any numbers that exist in that case
    come from build_position_records()'s activity-reconstruction fallback and
    are labelled as such instead of being presented as closed-position
    accounting. A wallet with no eligible positions at all publishes no
    ROI/win-rate/P&L.
    """
    counted = stats.positions_counted
    if counted <= 0:
        return (
            stats.retrieval_status,
            "Closed-positions request succeeded but returned no eligible closed positions; "
            "no ROI, win rate or realized P&L is published from an empty history.",
            "none",
        )
    if not closed_rows_returned:
        return (
            "ACTIVITY_ONLY",
            f"Closed-positions API returned an empty history; the {counted} eligible position(s) "
            "were reconstructed from /activity fills, so ROI/win-rate/realized P&L are our own "
            "reconstruction rather than authoritative closed-position accounting.",
            "activity_reconstruction",
        )
    return (
        stats.retrieval_status,
        f"Win/loss, realized P&L, ROI, and drawdown computed from "
        f"{counted} eligible position(s) via Phase 6 engine.",
        "closed_positions_api",
    )


def cmd_scan_wallets(args) -> None:
    client = PolymarketApiClient()
    wallets = scan_top_wallets(client, limit=settings.MAX_WALLET_CANDIDATES)
    refresh_attempt_at = _utc_now_iso()

    # Existing records are loaded BEFORE anything is written, so a scan can
    # merge into them instead of rebuilding the file: tracked preferences,
    # hand-added wallets, custom fields and wallets that simply did not
    # reappear on the leaderboard this time all survive a re-scan.
    existing_records = load_wallet_config()
    existing_by_key: dict[str, dict] = {}
    for record in existing_records:
        key = wallet_address_key(record)
        if key and key not in existing_by_key:
            existing_by_key[key] = record

    stats_map = {}
    freq_map = {}
    diag_map = {}
    filter_map = {}
    extra_details = {}
    endpoint_notes_map: dict[str, list[str]] = {}
    closed_rows_returned_map: dict[str, bool] = {}
    stale_updates: dict[str, dict] = {}
    stale_wallets: list[str] = []

    for w in wallets:
        short = w[:10] + "..."
        # Phase 12: paginate activity and closed-positions so that recent sell
        # events overlap with the closed-position records, allowing accurate
        # TRADING_EXIT classification in build_position_records().
        # Phase B: raise_on_error=True makes "the request failed" tellable
        # apart from "the response was genuinely empty". Both endpoints are
        # material to the P&L metrics AND to the hard filter (TRADING_EXIT
        # detection drives is_resolution_only), so a failure must never be
        # silently treated as fresh data.
        stale_reasons: list[str] = []
        events = []
        try:
            events = fetch_wallet_events_paginated(
                client, w, max_pages=settings.MAX_HISTORY_PAGES, raise_on_error=True
            )
        except ApiError as exc:
            stale_reasons.append(f"/activity request failed: {exc}")
            logger.warning("WALLET_STATS_PARTIAL wallet=%s reason=activity_api_failed:%s", short, exc)

        # --- authoritative closed-position P&L/win-rate --------------------
        raw_closed: list[dict] = []
        try:
            raw_closed = client.get_wallet_closed_positions_paginated(
                w, max_pages=settings.MAX_HISTORY_PAGES, raise_on_error=True
            )
        except ApiError as exc:
            stale_reasons.append(f"/closed-positions request failed: {exc}")
            logger.warning("WALLET_STATS_PARTIAL wallet=%s reason=closed_positions_api_failed:%s", short, exc)
        except Exception as exc:  # defensive: a bad wallet must not kill the scan
            stale_reasons.append(f"/closed-positions request failed: {exc}")
            logger.warning("WALLET_STATS_PARTIAL wallet=%s reason=closed_positions_api_failed:%s", short, exc)

        # --- active open positions ------------------------------------------
        # Not material to ROI/win-rate/P&L (those come from closed positions),
        # so a failure here is recorded as a note rather than invalidating the
        # whole refresh.
        endpoint_notes: list[str] = []
        raw_positions = []
        try:
            raw_positions = client.get_wallet_positions(w)
        except ApiError as exc:
            endpoint_notes.append("(/positions request failed this scan; open-position fields are null.)")
            logger.warning("WALLET_STATS_PARTIAL wallet=%s reason=positions_api_failed:%s", short, exc)

        # --- portfolio value -------------------------------------------------
        portfolio_val = None
        try:
            portfolio_val = client.get_wallet_value(w)
        except ApiError as exc:
            endpoint_notes.append("(/value request failed this scan; portfolio value is null.)")
            logger.warning("WALLET_STATS_PARTIAL wallet=%s reason=value_api_failed:%s", short, exc)
        endpoint_notes_map[w] = endpoint_notes
        closed_rows_returned_map[w] = bool(raw_closed)

        # Build canonical Phase 6 PositionRecords
        positions = build_position_records(raw_closed, events, raw_positions)

        # Compute canonical Phase 6 WalletStatistics. These are computed on
        # every scan for diagnostics, but are only PERSISTED for a refresh
        # that actually succeeded -- see the stale branch below.
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

        if stale_reasons:
            # Phase B (section C): the refresh failed. This scan's data is
            # STALE: no metric is recomputed from it, the last successful
            # refresh's metrics/scores/recommendation are carried over
            # untouched, and the wallet is barred from a fresh PASS/ranking.
            reason = (
                "Refresh incomplete: " + "; ".join(stale_reasons) + ". "
                "Previously stored performance metrics, scores and recommendations were "
                "retained unchanged; this scan's data is STALE and cannot qualify as a fresh PASS."
            )
            stale_updates[w] = _stale_wallet_update(
                w, existing_by_key.get(w, {}), refresh_attempt_at, reason
            )
            stale_wallets.append(w)
            filter_map[w] = WalletFilterResult(
                passed=False,
                reasons=[reason],
                status=DATA_QUALITY_STALE,
                insufficient_criteria=[],
            )
            print(f"HARD_FILTER_STALE wallet={short}")
            logger.warning("HARD_FILTER_STALE wallet=%s reasons=%s", short, "; ".join(stale_reasons))
            continue

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

    # Wallets that hard-failed, lacked data, or could not be refreshed are
    # excluded from the ranking/recommendation pool.
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
        "HARD_FILTER_SUMMARY pass=%d insufficient=%d fail=%d stale=%d total=%d",
        len(pass_wallets), len(insufficient_wallets), len(fail_wallets),
        len(stale_wallets), len(wallets),
    )

    # Canonical Phase 10 ranking: only wallets with FRESH data that passed the
    # hard filter enter the pool; STALE wallets keep their previous score.
    pass_stats = [stats_map[w] for w in pass_wallets]
    pass_freq = {w: freq_map[w] for w in pass_wallets}
    cards = rank_wallets(pass_stats, pass_freq)
    print_wallet_ranking(cards)

    updates: list[dict] = []

    # --- Ranked (PASS) wallets -------------------------------------------
    for card in cards:
        stats = stats_map[card.wallet]
        extra = extra_details.get(card.wallet, {})
        cfg = {
            "wallet": card.wallet,
            "last_refresh_attempt": refresh_attempt_at,
            "last_successful_refresh": refresh_attempt_at,
        }
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
        data_quality, quality_reason, pnl_source = _fresh_data_quality(
            stats, closed_rows_returned_map.get(card.wallet, False)
        )
        cfg["data_quality"] = data_quality
        cfg["data_quality_reason"] = quality_reason
        cfg["pnl_source"] = pnl_source
        if endpoint_notes_map.get(card.wallet):
            cfg["data_quality_reason"] += " " + " ".join(endpoint_notes_map[card.wallet])
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
        updates.append(cfg)

    # --- FAIL and INSUFFICIENT_DATA wallets (not ranked) -----------------
    for w in fail_wallets + insufficient_wallets:
        stats = stats_map[w]
        fres = filter_map[w]
        extra = extra_details.get(w, {})
        cfg = {
            "wallet": w,
            "last_refresh_attempt": refresh_attempt_at,
            "last_successful_refresh": refresh_attempt_at,
        }
        cfg["last_score"] = None
        cfg["recommendation"] = "HARD_FILTER_REJECT" if fres.status == "FAIL" else "HARD_FILTER_INSUFFICIENT_DATA"
        cfg["hard_filter_status"] = fres.status
        cfg["hard_filter_reasons"] = fres.reasons
        cfg["hard_filter_insufficient_criteria"] = fres.insufficient_criteria
        has_closed_data = stats.positions_counted > 0
        data_quality, quality_reason, pnl_source = _fresh_data_quality(
            stats, closed_rows_returned_map.get(w, False)
        )
        cfg["data_quality"] = data_quality
        cfg["data_quality_reason"] = quality_reason
        cfg["pnl_source"] = pnl_source
        if endpoint_notes_map.get(w):
            cfg["data_quality_reason"] += " " + " ".join(endpoint_notes_map[w])
        cfg["win_rate_pct"] = stats.win_rate_pct if has_closed_data else None
        cfg["roi_pct"] = stats.roi_fee_exclusive_pct if has_closed_data else None
        cfg["last_activity_at"] = stats.last_activity_at
        updates.append(cfg)

    # --- STALE wallets: previous metrics preserved, never overwritten -----
    for w in stale_wallets:
        updates.append(stale_updates[w])

    # Merge into the stored configuration: matching is case-insensitive, the
    # stored `tracked` selection and any custom fields survive, and wallets
    # that were not rediscovered are retained unchanged.
    merged_records = merge_wallet_records(existing_records, updates)
    save_wallet_config(merged_records)
    retained = max(0, len(merged_records) - len(updates))
    print(f"\nSaved {len(merged_records)} wallet records to {settings.WALLETS_FILE}")
    print(
        f"  {len(cards)} passed hard filter and were ranked."
    )
    if fail_wallets:
        print(f"  {len(fail_wallets)} rejected by hard filter (see hard_filter_status=FAIL).")
    if insufficient_wallets:
        print(f"  {len(insufficient_wallets)} have insufficient data for full filtering.")
    if stale_wallets:
        print(
            f"  {len(stale_wallets)} could not be refreshed (hard_filter_status=STALE); "
            "previously stored metrics kept."
        )
    if retained:
        print(f"  {retained} previously stored wallet(s) were not rediscovered and were kept unchanged.")
    print("Set \"tracked\": true for any wallet you want to track.")
    print(
        f"(Candidate pool was {len(wallets)} wallet(s) this scan -- capped by "
        "MAX_WALLET_CANDIDATES and paged via LEADERBOARD_PAGE_SIZE / MAX_LEADERBOARD_PAGES in .env.)"
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
