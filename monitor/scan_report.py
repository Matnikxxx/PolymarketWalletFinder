"""
Truthful scan diagnostics (Phase C).

Answering "why did only N wallets pass?" must not require reading the code or
guessing. This module turns what a scan already computed -- discovery
counters, Phase 6 WalletStatistics, hard-filter results, frequency profiles
and the Phase 10 ranking cards -- into a compact, honest summary.

Design notes
------------
* Pure formatting/aggregation only: it NEVER recomputes a metric, re-runs a
  filter, re-ranks anything, or changes any threshold. Every number it prints
  is read out of the existing pipeline's own results.
* It reports what the code actually does, including the parts that are easy to
  misread: frequency classification penalises scores but does NOT exclude a
  wallet from ranking, and a high ROI can be a *rejection* reason because
  REJECT_ROI_ABOVE caps the accepted band.
* Read-only: nothing here writes files, calls the API, or prints any
  credential (the scan has none).
"""
from __future__ import annotations

import logging
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from config.settings import CONFIG_ISSUES, settings, setting_source
from tabulate import tabulate
from wallet.scanner import SEED_CANDIDATE_WALLETS

logger = logging.getLogger("monitor.scan_report")

# Frequency classifications that cost score in wallet/ranking.py (see
# FREQUENCY_SCORE_PENALTY / BOT_LIKE_SCORE_PENALTY) -- they penalise, they do
# not exclude. Kept here only for accurate reporting.
_PENALIZED_CLASSIFICATIONS = ("BOT_LIKE", "RAPID_TRADER", "HIGH_FREQUENCY", "ACTIVE")

#: How many concrete example reasons to print under a grouped count.
_MAX_VARIANTS = 3


def normalize_reason(reason: str) -> str:
    """
    Group key for a rejection reason: the numbers are stripped so that
    "Consistency 1.1 below minimum 4.4" and "Consistency 9.0 below minimum
    4.4" count as the same cause, while the full text of the first example is
    kept alongside it in the report.
    """
    if not isinstance(reason, str):
        return ""
    return re.sub(r"-?\d+(?:\.\d+)?", "N", reason).strip()


@dataclass
class CandidateRow:
    """One scanned candidate, exactly as the pipeline classified it."""

    wallet: str
    status: str = "UNKNOWN"
    source: str = "unknown"
    roi_pct: Optional[float] = None
    win_rate_pct: Optional[float] = None
    positions: Optional[int] = None
    active_days: Optional[int] = None
    consistency: Optional[float] = None
    drawdown_pct: Optional[float] = None
    frequency: Optional[str] = None
    reason: str = ""
    # Filter criteria this candidate failed (0 for PASS). INSUFFICIENT_DATA
    # rows count their missing criteria instead, so "how far off" is honest
    # for both outcomes.
    failed_criteria: list = field(default_factory=list)


@dataclass
class ScanSummary:
    """Aggregated, read-only summary of one --scan-wallets run."""

    candidates_total: int = 0
    known_before: int = 0
    new_this_scan: int = 0
    retained_not_rediscovered: int = 0

    # Discovery (None when scan_top_wallets was mocked/wrapped without counters)
    discovery: Optional[dict] = None

    status_counts: Counter = field(default_factory=Counter)
    reason_counts: Counter = field(default_factory=Counter)
    reason_examples: dict = field(default_factory=dict)
    # Up to _MAX_VARIANTS distinct concrete reasons per group, so a grouped
    # count can never hide which actual thresholds/values were involved.
    reason_variants: dict = field(default_factory=dict)
    # How many criteria each FAIL wallet missed by (1 -> n, 2 -> n, ...), and
    # the wallets that missed by exactly one -- the actionable "closest to
    # passing" list.
    failed_criteria_distribution: Counter = field(default_factory=Counter)
    near_misses: list = field(default_factory=list)
    by_source: dict = field(default_factory=dict)
    # Per-wallet causes for failed refreshes (STALE), reported separately so
    # they are not conflated with hard-filter rejections.
    stale_causes: list = field(default_factory=list)
    insufficient_counts: Counter = field(default_factory=Counter)
    frequency_counts: Counter = field(default_factory=Counter)
    frequency_penalized: int = 0
    frequency_bot_like: int = 0
    recommendation_counts: Counter = field(default_factory=Counter)
    ranked: int = 0
    rows: list[CandidateRow] = field(default_factory=list)
    thresholds: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def build_scan_summary(
    *,
    wallets: list[str],
    discovery=None,
    existing_records: Optional[list[dict]] = None,
    stats_map: Optional[dict] = None,
    filter_map: Optional[dict] = None,
    freq_map: Optional[dict] = None,
    stale_wallets: Optional[list[str]] = None,
    stale_causes: Optional[dict] = None,
    updates: Optional[list[dict]] = None,
    cards: Optional[list] = None,
) -> ScanSummary:
    """
    Build the summary from the pipeline's own objects. Every argument is read
    only; nothing is mutated and no metric is recomputed.
    """
    stats_map = stats_map or {}
    filter_map = filter_map or {}
    freq_map = freq_map or {}
    stale_wallets = list(stale_wallets or [])
    stale_causes = dict(stale_causes or {})
    updates = list(updates or [])
    cards = list(cards or [])

    summary = ScanSummary(candidates_total=len(wallets))
    summary.ranked = len(cards)
    summary.thresholds = {
        "MIN_ROI": settings.MIN_ROI,
        "REJECT_ROI_ABOVE": settings.REJECT_ROI_ABOVE,
        "MIN_POSITIONS": settings.MIN_POSITIONS,
        "MIN_ACTIVE_DAYS": settings.MIN_ACTIVE_DAYS,
        "MIN_CONSISTENCY": settings.MIN_CONSISTENCY,
        "MAX_DRAWDOWN": settings.MAX_DRAWDOWN,
        "POLITICS_ALLOWED": settings.POLITICS_ALLOWED,
    }

    # --- discovery counters ------------------------------------------------
    if discovery is not None:
        summary.discovery = {
            "pages_attempted": getattr(discovery, "pages_attempted", None),
            "pages_succeeded": getattr(discovery, "pages_succeeded", None),
            "rows_returned": getattr(discovery, "rows_returned", None),
            "invalid_addresses": getattr(discovery, "rejected_addresses", None),
            "duplicates_dropped": getattr(discovery, "duplicates_dropped", None),
            "leaderboard_candidates": getattr(discovery, "leaderboard_candidates", None),
            "seed_candidates": getattr(discovery, "seed_candidates", None),
            "seed_duplicates": getattr(discovery, "seed_duplicates", None),
            "leaderboard_budget": getattr(discovery, "leaderboard_budget", None),
            "stop_reason": getattr(discovery, "stop_reason", None),
            "page_row_counts": list(getattr(discovery, "page_row_counts", []) or []),
            "short_pages": getattr(discovery, "short_pages", 0),
            "notes": list(getattr(discovery, "notes", []) or []),
        }
        summary.discovery["sources"] = dict(getattr(discovery, "sources", {}) or {})
    else:
        summary.notes.append(
            "Discovery counters unavailable for this run (scan_top_wallets was replaced by a mock/wrapper)."
        )

    # --- known vs new ------------------------------------------------------
    known_keys = {
        str(record.get("wallet", "")).strip().lower()
        for record in (existing_records or [])
        if isinstance(record, dict) and record.get("wallet")
    }
    candidate_keys = [w.strip().lower() for w in wallets]
    summary.known_before = sum(1 for w in candidate_keys if w in known_keys)
    summary.new_this_scan = summary.candidates_total - summary.known_before
    summary.retained_not_rediscovered = sum(
        1 for key in known_keys if key not in set(candidate_keys)
    )

    sources = {}
    if summary.discovery:
        sources = summary.discovery.get("sources") or {}

    # --- per-candidate classification -------------------------------------
    for wallet in wallets:
        key = wallet.strip().lower()
        stats = stats_map.get(wallet)
        result = filter_map.get(wallet)
        freq = freq_map.get(wallet)

        row = CandidateRow(wallet=key, source=sources.get(key, "unknown"))
        if stats is not None:
            row.roi_pct = getattr(stats, "roi_fee_exclusive_pct", None)
            row.win_rate_pct = getattr(stats, "win_rate_pct", None)
            row.positions = getattr(stats, "positions_counted", None)
            row.active_days = getattr(stats, "active_days", None)
            row.drawdown_pct = getattr(stats, "drawdown_cost_pct", None)
            mean = getattr(stats, "consistency_mean_roi", None)
            stdev = getattr(stats, "consistency_stdev_roi", None)
            if mean is not None and stdev is not None and mean != 0:
                row.consistency = round(
                    max(0.0, min(10.0, 10.0 / (1.0 + abs(stdev / mean)))), 2
                )
        if freq is not None:
            row.frequency = getattr(freq, "classification", None)

        if result is not None:
            row.status = getattr(result, "status", "UNKNOWN")
            reasons = list(getattr(result, "reasons", []) or [])
            if reasons:
                row.reason = reasons[0]
        elif key in stale_wallets:
            row.status = "STALE"

        summary.status_counts[row.status] += 1

        if result is not None:
            if row.status == "FAIL":
                row.failed_criteria = [
                    reason for reason in (getattr(result, "reasons", []) or [])
                    if not reason.startswith("Wallet meets all")
                ]
            elif row.status == "INSUFFICIENT_DATA":
                row.failed_criteria = list(getattr(result, "insufficient_criteria", []) or [])

        if result is not None and row.status != "STALE":
            # STALE is a failed refresh, not a hard-filter rejection: its cause
            # is reported in its own section (see stale_causes) so the reason
            # counts stay a faithful picture of filter outcomes.
            for reason in list(getattr(result, "reasons", []) or []):
                # The "meets all thresholds" line is not a rejection reason.
                if reason.startswith("Wallet meets all"):
                    continue
                group = normalize_reason(reason)
                summary.reason_counts[group] += 1
                summary.reason_examples.setdefault(group, reason)
                variants = summary.reason_variants.setdefault(group, [])
                if reason not in variants and len(variants) < _MAX_VARIANTS:
                    variants.append(reason)
            for criterion in list(getattr(result, "insufficient_criteria", []) or []):
                summary.insufficient_counts[criterion] += 1

        if row.status == "STALE":
            causes = [str(c) for c in (stale_causes.get(key) or [])] or ["(cause not supplied)"]
            summary.stale_causes.append({
                "wallet": key,
                "causes": causes,
                "metrics_kept": True,
            })
            # Show the actual failure, not the stored boilerplate sentence.
            row.reason = causes[0]

        if row.frequency:
            summary.frequency_counts[row.frequency] += 1

        summary.rows.append(row)

    # --- near-miss analysis (read-only: derived from the filter's own output) ---
    for row in summary.rows:
        if row.status == "FAIL":
            summary.failed_criteria_distribution[len(row.failed_criteria)] += 1
            if len(row.failed_criteria) == 1:
                summary.near_misses.append({"wallet": row.wallet, "reason": row.failed_criteria[0]})
    summary.near_misses.sort(key=lambda entry: entry["wallet"])

    # --- outcome split by discovery source ---------------------------------
    for row in summary.rows:
        bucket = summary.by_source.setdefault(
            row.source, {"scanned": 0, "PASS": 0, "FAIL": 0, "INSUFFICIENT_DATA": 0, "STALE": 0, "rois": []}
        )
        bucket["scanned"] += 1
        if row.status in bucket:
            bucket[row.status] += 1
        if row.roi_pct is not None:
            bucket["rois"].append(row.roi_pct)

    # --- frequency penalties (mirrors wallet/ranking.py; reporting only) ---
    if settings.RAPID_TRADE_ENABLED:
        for row in summary.rows:
            if row.status != "PASS" or not row.frequency:
                continue
            if row.frequency in _PENALIZED_CLASSIFICATIONS:
                summary.frequency_penalized += 1
            if row.frequency == "BOT_LIKE":
                summary.frequency_bot_like += 1

    # --- recommendations actually saved -----------------------------------
    for update in updates:
        if isinstance(update, dict):
            summary.recommendation_counts[update.get("recommendation")] += 1

    return summary


def format_scan_summary(summary: ScanSummary) -> str:
    """Human-readable, deterministic text block for one scan."""
    lines: list[str] = []
    add = lines.append

    add("=" * 72)
    add("SCAN DIAGNOSTICS")
    add("=" * 72)

    # --- discovery ---------------------------------------------------------
    if summary.discovery:
        d = summary.discovery
        add("Discovery (leaderboard pagination + seeds):")
        add(
            f"  pages attempted        : {d['pages_attempted']}   "
            f"(successful: {d['pages_succeeded']})  stop reason: {d['stop_reason']}"
        )
        add(
            f"  leaderboard rows       : {d['rows_returned']} returned, "
            f"{d['invalid_addresses']} rejected (bad address), "
            f"{d['duplicates_dropped']} duplicate(s) dropped"
        )
        if d.get("page_row_counts"):
            short = d.get("short_pages", 0)
            note = " (server-clipped pages are walked through, not treated as the end)" if short else ""
            add(f"  rows per page          : {d['page_row_counts']}  short pages: {short}{note}")
        add(
            f"  candidates from board  : {d['leaderboard_candidates']} "
            f"(leaderboard budget: {d['leaderboard_budget']})"
        )
        add(
            f"  candidates from seeds  : {d['seed_candidates']} "
            f"(already discovered from the board: {d['seed_duplicates']})"
        )
    else:
        add("Discovery: counters unavailable for this run.")

    add(f"  candidates this scan   : {summary.candidates_total}")
    add(f"  previously known       : {summary.known_before}")
    add(f"  new this scan          : {summary.new_this_scan}")
    if summary.retained_not_rediscovered:
        add(
            f"  not rediscovered       : {summary.retained_not_rediscovered} "
            "(kept unchanged in data/wallets.json)"
        )

    # --- effective configuration -------------------------------------------
    add("")
    add("Configuration in force (effective value, source):")
    add(
        f"  MAX_WALLET_CANDIDATES : {settings.MAX_WALLET_CANDIDATES} "
        f"(from {setting_source('MAX_WALLET_CANDIDATES')})"
    )
    add(
        f"  LEADERBOARD_PAGE_SIZE : {settings.LEADERBOARD_PAGE_SIZE} "
        f"(effective max {settings.LEADERBOARD_MAX_PAGE_SIZE}, "
        f"from {setting_source('LEADERBOARD_PAGE_SIZE')})"
    )
    add(
        f"  MAX_LEADERBOARD_PAGES : {settings.MAX_LEADERBOARD_PAGES} "
        f"(from {setting_source('MAX_LEADERBOARD_PAGES')})"
    )
    if summary.discovery and summary.discovery.get("leaderboard_budget") is not None:
        add(
            f"  this scan's budget    : {summary.discovery['leaderboard_budget']} leaderboard row(s) "
            f"+ {summary.discovery.get('seed_candidates', 0)} of {len(SEED_CANDIDATE_WALLETS)} seed(s) used"
        )
    if setting_source("MAX_WALLET_CANDIDATES") == "process environment":
        add(
            "  NOTE: the process environment -- not .env -- supplied MAX_WALLET_CANDIDATES. "
            "An exported/IDE variable wins over .env; unset it to use .env."
        )
    for issue in CONFIG_ISSUES:
        add(f"  CONFIG ISSUE: {issue}")
    if summary.discovery and summary.discovery.get("notes"):
        for note in summary.discovery["notes"]:
            add(f"  CONFIG NOTE: {note}")

    # --- outcomes ----------------------------------------------------------
    add("")
    add("Outcomes:")
    for status in ("PASS", "FAIL", "INSUFFICIENT_DATA", "STALE"):
        add(f"  {status:<18}: {summary.status_counts.get(status, 0)}")
    if summary.ranked or summary.recommendation_counts:
        add(f"  ranked             : {summary.ranked}")
        for rec, count in sorted(summary.recommendation_counts.items(), key=lambda kv: (-kv[1], str(kv[0]))):
            add(f"    recommendation {rec}: {count}")

    # --- why rejections happened ------------------------------------------
    if summary.reason_counts:
        add("")
        add("Hard-filter rejection reasons (grouped by cause):")
        for group, count in summary.reason_counts.most_common():
            add(f"  {count:>3}  {group}")
            variants = summary.reason_variants.get(group) or []
            single = len(variants) == 1
            for variant in variants:
                prefix = "        e.g. " if single else "         - "
                add(f"{prefix}{variant}")
    if summary.insufficient_counts:
        add("")
        add("Missing/inconclusive criteria (INSUFFICIENT_DATA):")
        for criterion, count in summary.insufficient_counts.most_common():
            add(f"  {count:>3}  {criterion}")

    # --- frequency ---------------------------------------------------------
    if summary.frequency_counts:
        add("")
        add("Trading-frequency classification (all scanned candidates):")
        for classification, count in summary.frequency_counts.most_common():
            add(f"  {classification:<18}: {count}")
        add(
            f"  among RANKED wallets: {summary.frequency_penalized} penalised, "
            f"{summary.frequency_bot_like} BOT_LIKE"
        )
        add("  NOTE: frequency never excludes a wallet from ranking; it only")
        add("        changes the score/recommendation (wallet/ranking.py), and only")
        add("        PASS wallets reach the ranking step at all.")

    # --- near misses -------------------------------------------------------
    if summary.near_misses or summary.failed_criteria_distribution:
        add("")
        add("How far off the rejected wallets were (FAIL wallets, criteria missed):")
        for count, n in sorted(summary.failed_criteria_distribution.items()):
            label = "criterion" if count == 1 else "criteria"
            add(f"  missed exactly {count} {label}: {n}")
        if summary.near_misses:
            add(f"  CLOSEST TO PASSING ({len(summary.near_misses)} wallet(s) one criterion away):")
            for entry in summary.near_misses:
                add(f"    {entry['wallet'][:12]}...: {entry['reason']}")

    # --- by discovery source ----------------------------------------------
    if summary.by_source:
        add("")
        add("Outcome by discovery source (where candidates came from):")
        for source, bucket in sorted(summary.by_source.items()):
            median_roi = f"{statistics.median(bucket['rois']):.1f}%" if bucket["rois"] else "N/A"
            add(
                f"  {source:<12}: {bucket['scanned']:>3} scanned, {bucket['PASS']} PASS, "
                f"{bucket['FAIL']} FAIL, {bucket['INSUFFICIENT_DATA']} INSUFFICIENT_DATA, "
                f"{bucket['STALE']} STALE (median ROI: {median_roi})"
            )

    # --- stale refreshes ---------------------------------------------------
    if summary.stale_causes:
        add("")
        add("STALE refreshes (previous metrics/scores kept, excluded from ranking):")
        for entry in summary.stale_causes:
            add(f"  {entry['wallet'][:12]}...: {'; '.join(entry['causes'])}")

    # --- thresholds --------------------------------------------------------
    t = summary.thresholds
    if t:
        add("")
        add("Acceptance band in force (unchanged by this build):")
        add(
            f"  ROI must be within [{t['MIN_ROI']}, {t['REJECT_ROI_ABOVE']}] -- "
            "a HIGHER ROI above the cap is a rejection reason,"
        )
        add(
            f"  positions >= {t['MIN_POSITIONS']}, active days >= {t['MIN_ACTIVE_DAYS']}, "
            f"consistency >= {t['MIN_CONSISTENCY']}, drawdown <= {t['MAX_DRAWDOWN']}, "
            f"politics allowed: {t['POLITICS_ALLOWED']}"
        )

    # --- the headline answer ----------------------------------------------
    passed = summary.status_counts.get("PASS", 0)
    add("")
    if summary.candidates_total == 0:
        add("WHY: no candidates were discovered this scan (see discovery section).")
    elif passed == 0:
        add("WHY: no candidate met every threshold this scan; see the reason counts above.")
    elif passed == 1:
        add(
            "WHY only 1 passed: the other candidates were rejected by the configured "
            "thresholds (see reason counts) -- the filter is selective by design, not broken."
        )
        if summary.near_misses:
            add(
                f"  {len(summary.near_misses)} wallet(s) missed by a SINGLE criterion "
                "(listed above) -- those are the ones worth reviewing first."
            )
    else:
        add(f"WHY {passed} passed: the rest are below/above the configured thresholds (above).")
        if summary.near_misses:
            add(
                f"  {len(summary.near_misses)} rejected wallet(s) missed by a SINGLE criterion "
                "(listed above)."
            )

    for note in summary.notes:
        add(f"NOTE: {note}")

    add("=" * 72)
    return "\n".join(lines)


def format_candidate_table(summary: ScanSummary) -> str:
    """Compact per-candidate table; missing values are shown as N/A, never 0."""
    if not summary.rows:
        return "(no candidates)"

    def fmt(value, pattern="{:.1f}"):
        return pattern.format(value) if value is not None else "N/A"

    rows = []
    for row in summary.rows:
        rows.append([
            row.wallet[:12] + "...",
            row.status,
            row.source,
            fmt(row.roi_pct),
            fmt(row.win_rate_pct),
            row.positions if row.positions is not None else "N/A",
            row.active_days if row.active_days is not None else "N/A",
            fmt(row.consistency),
            fmt(row.drawdown_pct),
            row.frequency or "N/A",
            (row.reason[:44] + "...") if len(row.reason) > 47 else row.reason,
        ])
    headers = [
        "Wallet", "Status", "Source", "ROI%", "WinRate%", "Pos", "Days", "Cons", "DD%",
        "Frequency", "Primary reason",
    ]
    return tabulate(rows, headers=headers)


def print_scan_summary(summary: ScanSummary) -> None:
    """Print the summary + per-candidate table, and log the machine-readable totals."""
    print()
    print(format_scan_summary(summary))
    print()
    print(format_candidate_table(summary))
    logger.info(
        "SCAN_SUMMARY candidates=%d known=%d new=%d pass=%d fail=%d insufficient=%d stale=%d ranked=%d",
        summary.candidates_total, summary.known_before, summary.new_this_scan,
        summary.status_counts.get("PASS", 0), summary.status_counts.get("FAIL", 0),
        summary.status_counts.get("INSUFFICIENT_DATA", 0), summary.status_counts.get("STALE", 0),
        summary.ranked,
    )
    for group, count in summary.reason_counts.most_common():
        logger.info("SCAN_REJECTION_REASON count=%d reason=%s", count, summary.reason_examples.get(group, group))
