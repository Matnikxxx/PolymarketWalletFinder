"""
Read-only wallet-analysis dashboard (spec item 14).

This module does NOT add any new data source or execution path -- it only
assembles a snapshot from state that already exists on disk: wallet config
+ cached scan/verification results in data/wallets.json and
data/verification.json, and the tracked-wallet activity log written by
wallet/monitor.py (data/wallet_activity_log.json).

This module only reports on wallet discovery, filtering, ranking,
verification, and tracking. It does not execute or simulate trades.

Used by both the console dashboard (monitor/status.py::print_dashboard) and
the web dashboard (dashboard/app.py) -- both render the SAME DashboardData,
so there is exactly one place that computes what the numbers mean.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from config.settings import settings
from storage import load_json
from wallet.monitor import load_wallet_config, tracked_wallets, is_wallet_tracked


@dataclass
class DashboardData:
    discovery_status: dict = field(default_factory=dict)
    recent_activity: list = field(default_factory=list)
    wallet_table: list = field(default_factory=list)
    tracked_wallet_table: list = field(default_factory=list)
    top_wallet_details: Optional[dict] = None


def _recent_activity(limit: int) -> list[dict]:
    """
    Most recent NEW BUY/SELL activity observed on tracked wallets (see
    wallet/monitor.py::WalletMonitor). This is activity the wallet took --
    never anything done in response to it, since nothing here ever acts.
    """
    raw_events = load_json(settings.WALLET_ACTIVITY_LOG_FILE, default=[])
    if not isinstance(raw_events, list):
        return []
    events = [e for e in raw_events if isinstance(e, dict)]
    events = sorted(events, key=lambda e: e.get("detected_at") or "", reverse=True)
    rows = []
    for e in events[:limit]:
        rows.append({
            "detected_at": e.get("detected_at"),
            "timestamp": e.get("timestamp"),
            "wallet": e.get("wallet"),
            "market": e.get("market_title"),
            "side": e.get("side"),
            "price": e.get("price"),
            "size": e.get("size"),
            "usdc_size": e.get("usdc_size"),
            "category": e.get("category"),
            "is_rapid": e.get("is_rapid"),
            "rapid_reason": e.get("rapid_reason"),
        })
    return rows


def _history_status(w: dict) -> str:
    """
    COMPLETE / PARTIAL / UNKNOWN for a stored wallet record (P4).

    ``history_truncated`` is written by the scan from pagination metadata:
    True when a page cap (or failed request) left older history unfetched.
    A record from before this field existed -- or one never refreshed -- has
    no value at all, and "unknown" is the honest answer; it must never be
    rendered as complete.
    """
    flag = w.get("history_truncated")
    if flag is True:
        return "PARTIAL"
    if flag is False:
        return "COMPLETE"
    return "UNKNOWN"


def _history_detail(w: dict) -> str:
    """Per-endpoint tooltip text: endpoint, stop reason, pages, records."""
    parts = []
    for endpoint, prefix in (
        ("/activity", "activity"),
        ("/closed-positions", "closed_positions"),
    ):
        stop = w.get(f"{prefix}_stop_reason")
        pages = w.get(f"{prefix}_pages_fetched")
        max_pages = w.get(f"{prefix}_max_pages")
        rows = w.get(f"{prefix}_rows_fetched")
        if stop is None and pages is None and rows is None:
            continue
        page_text = "?" if pages is None else str(pages)
        if max_pages is not None:
            page_text = f"{page_text}/{max_pages}"
        parts.append(
            f"{endpoint}: {stop or 'unknown'} after {page_text} page(s), "
            f"{'?' if rows is None else rows} record(s) fetched"
        )
    return "; ".join(parts)


def _wallet_row(w: dict, verification: dict) -> dict:
    wallet = w["wallet"]
    v = verification.get(wallet, {}) if isinstance(verification, dict) else {}
    return {
        "wallet": wallet,
        "score": w.get("last_score"),
        "recommendation": w.get("recommendation"),
        "frequency_classification": w.get("frequency_classification"),
        "frequency_median_gap_seconds": w.get("frequency_median_gap_seconds"),
        "frequency_raw_fill_count": w.get("frequency_raw_fill_count"),
        "frequency_logical_trades": w.get("frequency_logical_trades"),
        "frequency_max_trades_60s": w.get("frequency_max_trades_60s"),
        "frequency_pct_gaps_le_10s": w.get("frequency_pct_gaps_le_10s"),
        "frequency_rule_5_trades_60s": w.get("frequency_rule_5_trades_60s"),
        "frequency_rule_30pct_gaps_le_10s": w.get("frequency_rule_30pct_gaps_le_10s"),
        "verification_status": v.get("status", "UNKNOWN"),
        "tracked": is_wallet_tracked(w),
        # May be None for a wallet whose config predates the last scan, that
        # has never been scanned, or that genuinely has no closed positions
        # yet -- template must handle that, not invent a number. See
        # analytics/wallet_stats.py::DataQuality for what each value means.
        "roi_pct": w.get("roi_pct"),
        "win_rate_pct": w.get("win_rate_pct"),
        "drawdown_pct": w.get("drawdown_pct"),
        "realized_pnl": w.get("realized_pnl"),
        "num_positions": w.get("num_positions"),
        "avg_position_size": w.get("avg_position_size"),
        "risk_score": w.get("risk_score"),
        "consistency_score": w.get("consistency_score"),
        "data_quality": w.get("data_quality", "INSUFFICIENT_DATA"),
        "data_quality_reason": w.get("data_quality_reason"),
        "pnl_source": w.get("pnl_source"),
        "total_trades": w.get("total_trades"),
        "buy_trades": w.get("buy_trades"),
        "sell_trades": w.get("sell_trades"),
        "total_volume": w.get("total_volume"),
        "largest_trade": w.get("largest_trade"),
        "active_positions": w.get("active_positions"),
        "portfolio_value": w.get("portfolio_value"),
        # --- Activity span + history completeness (P4) --------------------
        # active_days / first_activity_at are None when the scan never
        # computed them; 0 is a real measured value and is preserved as 0.
        "active_days": w.get("active_days"),
        "first_activity_at": w.get("first_activity_at"),
        "last_activity_at": w.get("last_activity_at"),
        "history_status": _history_status(w),
        "history_detail": _history_detail(w),
        # D2/D3: why a PARTIAL history changed (or did not change) the
        # decision, and which criteria it left undecidable. Persisted verbatim
        # from wallets.json so later refreshes and the dashboard agree.
        "history_note": w.get("history_note"),
        "indeterminate_criteria": list(w.get("hard_filter_indeterminate_criteria") or []),
        "activity_pages_fetched": w.get("activity_pages_fetched"),
        "closed_positions_pages_fetched": w.get("closed_positions_pages_fetched"),
        "activity_stop_reason": w.get("activity_stop_reason"),
        "closed_positions_stop_reason": w.get("closed_positions_stop_reason"),
    }


def _wallet_table() -> list[dict]:
    """
    ALL scanned/ranked candidates, sorted by score (best first). This is
    "who was scanned" -- NOT "who is being tracked". See _tracked_wallet_table
    for that distinction (spec item 7's explicit requirement).
    """
    wallets_cfg = load_wallet_config()
    raw_v = load_json(settings.VERIFICATION_FILE, default=[])
    verification = {v["wallet"]: v for v in raw_v if isinstance(v, dict) and "wallet" in v} if isinstance(raw_v, list) else {}
    rows = [_wallet_row(w, verification) for w in wallets_cfg if isinstance(w, dict) and "wallet" in w]
    rows.sort(key=lambda r: (r["score"] if r["score"] is not None else -1), reverse=True)
    return rows


def _tracked_wallet_table() -> list[dict]:
    """
    Only wallets selected for tracking in data/wallets.json -- the ones
    `--track-wallets` actually polls for new activity. Does NOT imply a
    tracking process is currently running; the dashboard itself never
    starts one (spec item 2).
    """
    wallets_cfg = load_wallet_config()
    raw_v = load_json(settings.VERIFICATION_FILE, default=[])
    verification = {v["wallet"]: v for v in raw_v if isinstance(v, dict) and "wallet" in v} if isinstance(raw_v, list) else {}
    rows = [_wallet_row(w, verification) for w in wallets_cfg if isinstance(w, dict) and "wallet" in w and is_wallet_tracked(w)]
    rows.sort(key=lambda r: (r["score"] if r["score"] is not None else -1), reverse=True)
    return rows


def build_dashboard_data(recent_activity_limit: int = 20) -> DashboardData:
    wallets_cfg = load_wallet_config()
    active = tracked_wallets(wallets_cfg)

    wallet_table = _wallet_table()
    history_counts = {"COMPLETE": 0, "PARTIAL": 0, "UNKNOWN": 0}
    provisional_wallets = 0
    for row in wallet_table:
        history_counts[row["history_status"]] = history_counts.get(row["history_status"], 0) + 1
        if row.get("recommendation") == "PROVISIONAL":
            provisional_wallets += 1

    discovery_status = {
        "check_interval_seconds": settings.CHECK_INTERVAL,
        "tracked_wallets": len(active),
        "discovered_wallets": len(wallets_cfg),
        "history_complete": history_counts["COMPLETE"],
        "history_partial": history_counts["PARTIAL"],
        "history_unknown": history_counts["UNKNOWN"],
        "provisional_wallets": provisional_wallets,
    }

    return DashboardData(
        discovery_status=discovery_status,
        recent_activity=_recent_activity(recent_activity_limit),
        wallet_table=wallet_table,
        tracked_wallet_table=_tracked_wallet_table(),
        top_wallet_details=wallet_table[0] if wallet_table else None,
    )
