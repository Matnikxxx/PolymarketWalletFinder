"""
Explicit, threshold-based wallet filter (spec section 3). This is a direct
pass/fail check against configured thresholds -- distinct from (and simpler
than) the 0-100 explainable ranking score in wallet/ranking.py. Both are
useful: ranking sorts candidates, this filter gives a crisp yes/no gate with
a reason for each threshold.

Phase 8A migration: Evaluates Phase 6 WalletStatistics (clean eligible population
and metrics), with safe backward-compatibility for legacy WalletStats.
Handles None/optional values explicitly to distinguish PASS, FAIL, and
INSUFFICIENT_DATA without false numeric failures.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

from analytics.models import WalletStatistics
from analytics.wallet_stats import WalletStats
from config.settings import settings


@dataclass
class WalletFilterResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    status: str = "PASS"  # "PASS", "FAIL", "INSUFFICIENT_DATA"
    insufficient_criteria: list[str] = field(default_factory=list)


def filter_wallet(stats: Union[WalletStatistics, WalletStats]) -> WalletFilterResult:
    """
    Evaluates a wallet's statistics against the configured hard-filter thresholds.

    Supports Phase 6 WalletStatistics (primary) and legacy WalletStats (compat).
    Uses clean eligible positions_counted for MIN_POSITIONS.
    Handles None/unavailable metrics as INSUFFICIENT_DATA rather than false failures.
    """
    reasons: list[str] = []
    insufficient_criteria: list[str] = []
    passed = True

    # 1. ROI Minimum and Maximum Cap
    if isinstance(stats, WalletStatistics):
        roi = stats.roi_fee_exclusive_pct
    else:
        roi = getattr(stats, "roi_fee_exclusive_pct", None)
        if roi is None:
            roi = getattr(stats, "roi_pct", None)

    if roi is None:
        insufficient_criteria.append("roi")
    else:
        if roi < settings.MIN_ROI:
            passed = False
            reasons.append(f"ROI {roi:.1f}% below minimum {settings.MIN_ROI}%")

        if roi > settings.REJECT_ROI_ABOVE:
            passed = False
            reasons.append(f"ROI {roi:.1f}% above reject threshold {settings.REJECT_ROI_ABOVE}%")

    # 2. Position Count (MIN_POSITIONS evaluates Phase 6 positions_counted)
    positions = getattr(stats, "positions_counted", None)
    if positions is None:
        positions = getattr(stats, "total_positions", 0)

    if positions < settings.MIN_POSITIONS:
        passed = False
        reasons.append(f"Only {positions} positions, below minimum {settings.MIN_POSITIONS}")

    # 3. Active Days
    active_days = getattr(stats, "active_days", 0)
    if active_days < settings.MIN_ACTIVE_DAYS:
        passed = False
        reasons.append(f"Only {active_days} active days, below minimum {settings.MIN_ACTIVE_DAYS}")

    # 4. Consistency (Return dispersion / CV score)
    consistency = getattr(stats, "consistency_score", None)
    if consistency is None:
        m = getattr(stats, "consistency_mean_roi", None)
        sd = getattr(stats, "consistency_stdev_roi", None)
        if m is not None and sd is not None:
            if m != 0:
                cv = abs(sd / m)
                consistency = max(0.0, min(10.0, 10.0 / (1.0 + cv)))
            else:
                consistency = 0.0

    if consistency is None:
        insufficient_criteria.append("consistency")
    else:
        if consistency < settings.MIN_CONSISTENCY:
            passed = False
            reasons.append(f"Consistency {consistency:.1f} below minimum {settings.MIN_CONSISTENCY}")

    # 5. Maximum Drawdown
    if isinstance(stats, WalletStatistics):
        drawdown = stats.drawdown_cost_pct
    else:
        drawdown = getattr(stats, "drawdown_cost_pct", None)
        if drawdown is None:
            drawdown = getattr(stats, "max_drawdown_pct", None)

    if drawdown is None:
        insufficient_criteria.append("drawdown")
    else:
        if drawdown > settings.MAX_DRAWDOWN:
            passed = False
            reasons.append(f"Drawdown {drawdown:.1f}% above maximum {settings.MAX_DRAWDOWN}%")

    # 6. Profit Concentration (Top-1 trade profit share >= 70%)
    if isinstance(stats, WalletStatistics):
        concentration = stats.top1_profit_share_pct
    else:
        concentration = getattr(stats, "top1_profit_share_pct", None)
        if concentration is None:
            concentration = getattr(stats, "concentration_pct", None)

    if concentration is None:
        insufficient_criteria.append("concentration")
    else:
        if concentration >= 70:
            passed = False
            reasons.append(f"{concentration:.1f}% of profit concentrated in one trade")

    # 7. Resolution-Only Exclusion
    # None means data was unavailable — do not silently treat as False.
    is_resolution_only = getattr(stats, "is_resolution_only", False)
    if is_resolution_only is None:
        insufficient_criteria.append("is_resolution_only")
    elif is_resolution_only:
        passed = False
        reasons.append("Wallet appears resolution-only (no active exits)")

    # 8. Category / Politics Restriction
    # None means category data was unavailable — do not silently treat as 0% politics.
    category_mix = getattr(stats, "category_mix", None)
    if category_mix is None:
        insufficient_criteria.append("category_mix")
    else:
        total_cat = sum(category_mix.values()) or 1
        politics_share = category_mix.get("politics", 0) / total_cat
        if not settings.POLITICS_ALLOWED and politics_share > 0.5:
            passed = False
            reasons.append(f"{politics_share*100:.0f}% of markets are politics (excluded)")

    # Overall Status Determination
    if not passed:
        status = "FAIL"
    elif insufficient_criteria:
        status = "INSUFFICIENT_DATA"
        reasons.append("Wallet meets all evaluable filter thresholds.")
    else:
        status = "PASS"
        reasons.append("Wallet meets all configured filter thresholds.")

    return WalletFilterResult(
        passed=passed,
        reasons=reasons,
        status=status,
        insufficient_criteria=insufficient_criteria,
    )
