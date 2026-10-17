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

from analytics.consistency import consistency_score_from_dispersion
from analytics.models import WalletStatistics
from analytics.wallet_stats import WalletStats
from config.settings import settings


#: Criteria whose value is computed over the retrieved window and can
#: therefore be distorted by a page-capped (PARTIAL) history. A failure here
#: is never treated as a definitive rejection when history is incomplete.
#: Everything else (drawdown, resolution-only, politics) is an observed fact
#: that missing older rows cannot create.
HISTORY_DEPENDENT_CRITERIA = frozenset({
    "roi",
    "positions",
    "active_days",
    "consistency",
    "concentration",
})


def _dedupe(items: list[str]) -> list[str]:
    """Order-preserving de-duplication (criterion ids are shown to the user)."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


@dataclass
class WalletFilterResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    status: str = "PASS"  # "PASS", "FAIL", "INSUFFICIENT_DATA"
    insufficient_criteria: list[str] = field(default_factory=list)
    #: Criteria that actually drove a rejection (history-dependent misses
    #: on PARTIAL history are reported in indeterminate_criteria instead).
    failed_criteria: list[str] = field(default_factory=list)
    #: History-dependent failures that could not be decided on a PARTIAL
    #: history (empty for COMPLETE history).
    indeterminate_criteria: list[str] = field(default_factory=list)
    #: Human-readable explanation of how history completeness affected the
    #: outcome; None when the history was complete.
    history_note: Optional[str] = None


def filter_wallet(stats: Union[WalletStatistics, WalletStats]) -> WalletFilterResult:
    """
    Evaluates a wallet's statistics against the configured hard-filter thresholds.

    Supports Phase 6 WalletStatistics (primary) and legacy WalletStats (compat).
    Uses clean eligible positions_counted for MIN_POSITIONS.
    Handles None/unavailable metrics as INSUFFICIENT_DATA rather than false failures.

    PARTIAL history (D2/D3)
    -----------------------
    ``WalletStatistics.is_complete_history`` is ``False`` when a history feed
    stopped on a page cap, so the fetched window is not the wallet's lifetime.
    Missing older rows can change some metrics in either direction and cannot
    change others:

    * **history-dependent** -- ROI (minimum and cap), positions, active days,
      consistency and profit concentration are all computed over the fetched
      rows. A failure here may be an artefact of the truncated window.
    * **independent** -- a drawdown above the maximum, a resolution-only
      profile (when exit history is complete, i.e. P2 already returns
      ``None`` otherwise) and a politics-heavy mix are observed facts that
      missing older rows cannot create.

    Policy (thresholds themselves are unchanged):

    * COMPLETE history -- exactly the previous behaviour.
    * PARTIAL, only history-dependent failures -> ``INSUFFICIENT_DATA``, with
      those criteria reported as indeterminate.
    * PARTIAL, any independent failure -> ``FAIL`` on the independent
      reason(s); history-dependent failures are recorded as indeterminate and
      are not counted as rejections.
    * PARTIAL, no failures -> ``PASS`` (the caller labels the recommendation
      ``PROVISIONAL`` instead of ``RECOMMENDED``).
    """
    reasons: list[str] = []
    insufficient_criteria: list[str] = []
    failures: list[tuple[str, str]] = []
    passed = True

    def _fail(criterion: str, reason: str) -> None:
        nonlocal passed
        passed = False
        failures.append((criterion, reason))

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
            _fail("roi", f"ROI {roi:.1f}% below minimum {settings.MIN_ROI}%")

        if roi > settings.REJECT_ROI_ABOVE:
            _fail("roi", f"ROI {roi:.1f}% above reject threshold {settings.REJECT_ROI_ABOVE}%")

    # 2. Position Count (MIN_POSITIONS evaluates Phase 6 positions_counted)
    positions = getattr(stats, "positions_counted", None)
    if positions is None:
        positions = getattr(stats, "total_positions", 0)

    if positions < settings.MIN_POSITIONS:
        _fail("positions", f"Only {positions} positions, below minimum {settings.MIN_POSITIONS}")

    # 3. Active Days
    active_days = getattr(stats, "active_days", 0)
    if active_days < settings.MIN_ACTIVE_DAYS:
        _fail("active_days", f"Only {active_days} active days, below minimum {settings.MIN_ACTIVE_DAYS}")

    # 4. Consistency (Return dispersion / CV score)
    consistency = getattr(stats, "consistency_score", None)
    if consistency is None:
        # Same arithmetic as before, now shared with ranking/reporting so a
        # persisted consistency value is provably the one the filter used.
        consistency = consistency_score_from_dispersion(
            getattr(stats, "consistency_mean_roi", None),
            getattr(stats, "consistency_stdev_roi", None),
        )

    if consistency is None:
        insufficient_criteria.append("consistency")
    else:
        if consistency < settings.MIN_CONSISTENCY:
            _fail("consistency", f"Consistency {consistency:.1f} below minimum {settings.MIN_CONSISTENCY}")

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
            _fail("drawdown", f"Drawdown {drawdown:.1f}% above maximum {settings.MAX_DRAWDOWN}%")

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
            _fail("concentration", f"{concentration:.1f}% of profit concentrated in one trade")

    # 7. Resolution-Only Exclusion
    # None means data was unavailable — do not silently treat as False.
    is_resolution_only = getattr(stats, "is_resolution_only", False)
    if is_resolution_only is None:
        insufficient_criteria.append("is_resolution_only")
    elif is_resolution_only:
        _fail("is_resolution_only", "Wallet appears resolution-only (no active exits)")

    # 8. Category / Politics Restriction
    # None means category data was unavailable — do not silently treat as 0% politics.
    category_mix = getattr(stats, "category_mix", None)
    if category_mix is None:
        insufficient_criteria.append("category_mix")
    else:
        total_cat = sum(category_mix.values()) or 1
        politics_share = category_mix.get("politics", 0) / total_cat
        if not settings.POLITICS_ALLOWED and politics_share > 0.5:
            _fail("politics", f"{politics_share*100:.0f}% of markets are politics (excluded)")

    # --- PARTIAL-history policy (D2/D3) ---------------------------------
    history_complete = getattr(stats, "is_complete_history", True)
    partial = history_complete is False
    indeterminate_criteria: list[str] = []
    history_note: Optional[str] = None

    if partial:
        history_note = (
            "History is PARTIAL: only the newest pages were fetched, so older "
            "activity and closed positions are unseen."
        )

    if failures and partial:
        dependent = [(c, r) for c, r in failures if c in HISTORY_DEPENDENT_CRITERIA]
        independent = [(c, r) for c, r in failures if c not in HISTORY_DEPENDENT_CRITERIA]
        indeterminate_criteria = [c for c, _ in dependent]

        if dependent and not independent:
            # The only failures are metrics a truncated window can distort:
            # they cannot be used to reject the wallet. Each reason names the
            # criterion and says why it is undecidable, so the CLI/table shows
            # the actual threshold rather than a generic caveat.
            reasons = [
                f"{reason} (indeterminate: history is PARTIAL, so unseen older "
                f"activity could change this)"
                for _, reason in dependent
            ]
            insufficient_criteria = _dedupe(insufficient_criteria + indeterminate_criteria)
            history_note = (
                f"{history_note} Those criteria are reported as indeterminate and "
                f"are not treated as rejections."
            )
            status = "INSUFFICIENT_DATA"
        else:
            # At least one independent failure: keep the FAIL, but never
            # present a truncated-window metric as the decisive reason.
            reasons = [reason for _, reason in independent]
            history_note = (
                f"{history_note} The rejection rests on observed data that missing "
                f"older history cannot explain: "
                + "; ".join(reason for _, reason in independent) + "."
            )
            if dependent:
                history_note += (
                    " These criteria are indeterminate and are not treated as "
                    "rejections: " + "; ".join(reason for _, reason in dependent) + "."
                )
            status = "FAIL"
    elif failures:
        reasons = [reason for _, reason in failures]
        status = "FAIL"
    elif insufficient_criteria:
        status = "INSUFFICIENT_DATA"
        reasons.append("Wallet meets all evaluable filter thresholds.")
        if partial:
            history_note += (
                " Some criteria could not be evaluated: "
                + ", ".join(_dedupe(insufficient_criteria)) + "."
            )
    else:
        status = "PASS"
        reasons.append("Wallet meets all configured filter thresholds.")
        if partial:
            history_note += (
                " Every threshold is met on the fetched window, but lifetime metrics "
                "are lower bounds; the recommendation is PROVISIONAL rather than a "
                "normal RECOMMENDED."
            )

    # ``failed_criteria`` lists only the misses that actually drove the
    # decision. A history-dependent miss on PARTIAL history is indeterminate,
    # not a rejection, so it is reported there instead.
    if partial and failures:
        decisive = [
            criterion for criterion, _ in failures
            if criterion not in indeterminate_criteria
        ]
    else:
        decisive = [criterion for criterion, _ in failures]

    return WalletFilterResult(
        passed=passed,
        reasons=reasons,
        status=status,
        insufficient_criteria=_dedupe(insufficient_criteria),
        failed_criteria=decisive,
        indeterminate_criteria=indeterminate_criteria,
        history_note=history_note,
    )
