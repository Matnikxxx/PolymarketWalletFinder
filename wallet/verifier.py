"""
Wallet verification pipeline (spec section 22). Runs a checklist of
data-sufficiency and sanity checks before a wallet may be manually activated.
Never auto-activates a wallet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from analytics.frequency import FrequencyStats
from analytics.wallet_stats import WalletStats
from config.settings import settings


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass
class VerificationResult:
    wallet: str
    status: VerificationStatus
    checks: dict = field(default_factory=dict)  # check_name -> bool
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "wallet": self.wallet,
            "status": self.status.value,
            "checks": self.checks,
            "notes": self.notes,
        }


def verify_wallet(
    wallet: str, events: list[dict], stats: WalletStats | None, freq: Optional[FrequencyStats] = None
) -> VerificationResult:
    checks: dict[str, bool] = {}
    notes: list[str] = []

    checks["wallet_exists"] = bool(wallet)
    checks["activity_retrievable"] = len(events) > 0
    if not checks["activity_retrievable"]:
        notes.append("No activity could be retrieved for this wallet.")
        return VerificationResult(wallet=wallet, status=VerificationStatus.FAILED, checks=checks, notes=notes)

    if stats is None:
        notes.append("Activity was retrieved but statistics could not be computed.")
        return VerificationResult(wallet=wallet, status=VerificationStatus.UNKNOWN, checks=checks, notes=notes)

    checks["sufficient_history"] = stats.total_positions >= max(5, settings.MIN_POSITIONS // 4)
    checks["roi_calculable"] = stats.total_invested > 0
    checks["win_rate_calculable"] = (stats.wins + stats.losses) > 0
    checks["drawdown_estimable"] = len(stats.per_trade_pnls) >= 2
    checks["position_history_meaningful"] = stats.total_positions >= 3
    checks["not_single_position_dependent"] = stats.concentration_pct < 70
    checks["not_resolution_only"] = not stats.is_resolution_only
    checks["recent_enough"] = stats.active_days >= max(3, settings.MIN_ACTIVE_DAYS // 4)

    total_cat = sum(stats.category_mix.values()) or 1
    politics_share = stats.category_mix.get("politics", 0) / total_cat
    checks["category_acceptable"] = settings.POLITICS_ALLOWED or politics_share < 0.9

    # Frequency is optional (caller may not have computed it) so it's only
    # added to the checklist -- and only counted toward VERIFIED/FAILED --
    # when it was actually provided, to avoid silently downgrading every
    # existing caller that doesn't pass `freq`.
    if freq is not None and not freq.insufficient_data:
        checks["not_bot_like"] = freq.classification != "BOT_LIKE"
        if not checks["not_bot_like"]:
            notes.append(
                "Wallet trades like a bot (" + "; ".join(freq.classification_reasons) + ")."
            )

    if not checks["not_single_position_dependent"]:
        notes.append(f"{stats.concentration_pct:.1f}% of profit is concentrated in one trade.")
    if not checks["not_resolution_only"]:
        notes.append("Wallet has no SELL exits recorded -- looks resolution-only.")
    if not checks["recent_enough"]:
        notes.append(f"Only {stats.active_days} days of activity history.")
    if not checks["category_acceptable"]:
        notes.append("Wallet activity is dominated by politics markets, which are excluded.")

    passed = sum(1 for v in checks.values() if v)
    total = len(checks)

    if passed == total:
        status = VerificationStatus.VERIFIED
    elif passed >= total * 0.6:
        status = VerificationStatus.PARTIALLY_VERIFIED
    else:
        status = VerificationStatus.FAILED

    return VerificationResult(wallet=wallet, status=status, checks=checks, notes=notes)
