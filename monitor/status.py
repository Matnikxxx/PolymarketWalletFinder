"""
Human-readable status and report printers used by main.py's wallet-analysis
commands. Has no dependency on any trading/paper-trading code.
"""
from __future__ import annotations

from tabulate import tabulate

from wallet.ranking import WalletScoreCard
from wallet.verifier import VerificationResult


def _format_gap(seconds):
    if seconds is None:
        return "n/a"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.0f}m"
    if seconds < 86400:
        return f"{seconds/3600:.1f}h"
    return f"{seconds/86400:.1f}d"


def print_wallet_ranking(cards: list[WalletScoreCard], provisional_wallets=None) -> None:
    """
    Readable PowerShell-friendly ranking table (spec item 9). Rank number,
    ROI, win rate, drawdown, distinct-position count, avg size,
    consistency, risk, trading-frequency classification, median gap
    between trades, and the final explainable score + recommendation.

    ``provisional_wallets`` (D3): wallets whose PASS was computed on a
    page-capped history. Their RECOMMENDED label is shown as PROVISIONAL so
    the console never claims more confidence than the stored record has.
    Ranking itself is untouched -- only the printed label changes.
    """
    provisional = set(provisional_wallets or ())

    def _label(card: WalletScoreCard) -> str:
        if card.recommendation == "RECOMMENDED" and card.wallet in provisional:
            return "PROVISIONAL"
        return card.recommendation

    rows = [
        [i + 1, c.wallet[:12] + "...", f"{c.roi_pct:.1f}%", f"{c.win_rate_pct:.1f}%", f"{c.drawdown_pct:.1f}%",
         c.num_positions, f"${c.avg_position_size:.2f}", c.consistency_score, c.risk_score,
         c.frequency_classification, _format_gap(c.frequency_median_gap_seconds),
         c.final_score, _label(c)]
        for i, c in enumerate(cards)
    ]
    headers = [
        # "Positions" (not "Trades"): count of distinct markets/positions
        # touched, not a raw activity-fill count. See
        # analytics/wallet_stats.py::WalletStats.total_positions. The raw
        # fill count (capped by the API fetch limit) is logged separately
        # as WALLET_STATS's trades=N during --scan-wallets.
        "#", "Wallet", "ROI", "Win Rate", "Drawdown", "Positions", "Avg Size", "Consistency",
        "Risk", "Frequency", "Median Gap", "Score", "Recommendation",
    ]
    print(tabulate(rows, headers=headers))
    if provisional:
        print(
            "  PROVISIONAL = PASS on PARTIAL history: every threshold is met on the "
            "fetched window, but older activity was not fetched, so this is not a "
            "fully-vetted RECOMMENDED."
        )


def print_dashboard(data) -> None:
    """Read-only console dashboard (spec item 14): wallet discovery,
    ranking, tracking status, and recently observed wallet activity. No
    paper-trading/paper-account/position data of any kind."""
    print("=" * 70)
    print("POLYMARKET WALLET FINDER - DASHBOARD (READ-ONLY)")
    print("=" * 70)

    ds = data.discovery_status
    print("\nDISCOVERY STATUS")
    print(tabulate([
        ["Tracking interval", f"{ds['check_interval_seconds']}s"],
        ["Tracked wallets", ds["tracked_wallets"]],
        ["Discovered wallets", ds["discovered_wallets"]],
    ], tablefmt="plain"))

    print("\nRECENT WALLET ACTIVITY")
    if data.recent_activity:
        rows = [
            [a["detected_at"], (a["wallet"] or "")[:10] + "...", (a["market"] or "")[:25],
             a["side"], a["price"], a["size"], a["category"], "RAPID" if a["is_rapid"] else "normal"]
            for a in data.recent_activity
        ]
        print(tabulate(rows, headers=["Detected", "Wallet", "Market", "Side", "Price", "Size", "Category", "Cadence"]))
    else:
        print("(no activity tracked yet)")

    print("\nWALLET TABLE")
    if data.wallet_table:
        rows = [
            [(w["wallet"] or "")[:12] + "...", w["score"], w["recommendation"],
             w["frequency_classification"] or "n/a", w["verification_status"],
             "yes" if w["tracked"] else "no"]
            for w in data.wallet_table
        ]
        print(tabulate(rows, headers=["Wallet", "Score", "Recommendation", "Frequency", "Verification", "Tracked"]))
    else:
        print("(no wallets scanned yet -- run --scan-wallets)")


def print_wallet_report(wallet_table: list[dict]) -> None:
    """
    Full per-wallet report (spec item 10), sourced from the last
    --scan-wallets run (data/wallets.json) -- does not re-fetch anything.
    This is the detailed report equivalent of the dashboard's wallet table.
    """
    print("=" * 70)
    print("WALLET REPORT")
    print("=" * 70)
    if not wallet_table:
        print("(no wallets scanned yet -- run --scan-wallets)")
        return
    rows = []
    for w in wallet_table:
        rows.append([
            (w["wallet"] or "")[:14] + "...",
            w["score"], w["recommendation"],
            f"{w['win_rate_pct']:.1f}%" if w["win_rate_pct"] is not None else "N/A",
            f"{w['roi_pct']:.1f}%" if w["roi_pct"] is not None else "N/A",
            f"{w['drawdown_pct']:.1f}%" if w["drawdown_pct"] is not None else "N/A",
            w["num_positions"] if w["num_positions"] is not None else "N/A",
            w["total_trades"] if w["total_trades"] is not None else "N/A",
            w["buy_trades"] if w["buy_trades"] is not None else "N/A",
            w["sell_trades"] if w["sell_trades"] is not None else "N/A",
            f"${w['total_volume']:.2f}" if w["total_volume"] is not None else "N/A",
            w["frequency_classification"] or "N/A",
            w["data_quality"],
            w.get("active_days") if w.get("active_days") is not None else "N/A",
            w.get("history_status") or "UNKNOWN",
            "yes" if w["tracked"] else "no",
        ])
    headers = [
        "Wallet", "Score", "Recommendation", "Win Rate", "ROI", "Drawdown", "Positions",
        "Total Trades", "Buys", "Sells", "Volume", "Frequency", "Data Quality",
        "Active Days", "History", "Tracked",
    ]
    print(tabulate(rows, headers=headers))
    print(
        "  N/A = not available/not computed for this wallet; 0 = a genuine measured zero. "
        "History COMPLETE = the scan proved it fetched everything; "
        "PARTIAL = a page cap left older data unfetched (active days are a lower bound); "
        "UNKNOWN = no pagination metadata stored yet. "
        "PROVISIONAL = PASS on PARTIAL history (all thresholds met on the fetched "
        "window; older data unseen; never RECOMMENDED)."
    )


def print_verification(results: list[VerificationResult]) -> None:
    rows = [[r.wallet[:14] + "...", r.status.value, sum(r.checks.values()), len(r.checks), "; ".join(r.notes)[:60]] for r in results]
    print(tabulate(rows, headers=["Wallet", "Status", "Checks Passed", "Total Checks", "Notes"]))
