"""
Deterministic position eligibility evaluation and population partitioning.

Implements the strict, mutually exclusive priority cascade for assigning
each position to exactly one terminal classification bucket.
"""
from __future__ import annotations

from typing import List, Tuple
from analytics.models import (
    PositionRecord,
    PositionEligibility,
    PositionSettlementClass,
    PositionExclusionReason,
    PositionSettlementType,
    PopulationAccounting,
)


def evaluate_position_eligibility(
    pos: PositionRecord,
    tolerance: float = 0.01,
) -> PositionEligibility:
    """
    Evaluates a single PositionRecord against the approved Phase 6 rules.
    Guarantees assignment to exactly one bucket via a strict priority cascade.
    """
    # Normalize a non-string timestamp to None. Ordering/parsing helpers treat
    # a missing timestamp as "unknown" (e.g. drawdown becomes undefined rather
    # than raising TypeError when a mixed str/int set is sorted); valid data is
    # unaffected because build_position_records() emits ISO strings.
    effective_ts = pos.closed_at or pos.resolved_at or pos.opened_at
    if effective_ts is not None and not isinstance(effective_ts, str):
        effective_ts = None

    # Priority 1: Combos
    if pos.is_combo:
        return PositionEligibility(
            position_id=pos.position_id,
            is_eligible=False,
            bucket="COMBO_EXCLUDED",
            exclusion_reason=PositionExclusionReason.COMBO_EXCLUDED,
            settlement_class=pos.settlement_class,
            net_result=None,
            cost=pos.cost,
            effective_timestamp=effective_ts,
        )

    # Priority 2: Unreliable Data Quality
    if pos.has_excluded_fills:
        return PositionEligibility(
            position_id=pos.position_id,
            is_eligible=False,
            bucket="UNRELIABLE_DATA",
            exclusion_reason=PositionExclusionReason.EXCLUDED_FILLS,
            settlement_class=pos.settlement_class,
            net_result=None,
            cost=pos.cost,
            effective_timestamp=effective_ts,
        )
    if pos.has_unknown_cash:
        return PositionEligibility(
            position_id=pos.position_id,
            is_eligible=False,
            bucket="UNRELIABLE_DATA",
            exclusion_reason=PositionExclusionReason.UNKNOWN_CASH,
            settlement_class=pos.settlement_class,
            net_result=None,
            cost=pos.cost,
            effective_timestamp=effective_ts,
        )
    if pos.has_unknown_size:
        return PositionEligibility(
            position_id=pos.position_id,
            is_eligible=False,
            bucket="UNRELIABLE_DATA",
            exclusion_reason=PositionExclusionReason.UNKNOWN_SIZE,
            settlement_class=pos.settlement_class,
            net_result=None,
            cost=pos.cost,
            effective_timestamp=effective_ts,
        )
    if not pos.condition_id and pos.settlement_type != PositionSettlementType.TRADING_EXIT:
        return PositionEligibility(
            position_id=pos.position_id,
            is_eligible=False,
            bucket="UNRELIABLE_DATA",
            exclusion_reason=PositionExclusionReason.MISSING_CONDITION_ID,
            settlement_class=pos.settlement_class,
            net_result=None,
            cost=pos.cost,
            effective_timestamp=effective_ts,
        )
    if pos.cost <= 0.0:
        return PositionEligibility(
            position_id=pos.position_id,
            is_eligible=False,
            bucket="UNRELIABLE_DATA",
            exclusion_reason=PositionExclusionReason.ZERO_OR_NEGATIVE_COST,
            settlement_class=pos.settlement_class,
            net_result=None,
            cost=pos.cost,
            effective_timestamp=effective_ts,
        )

    # Priority 3: Repeat Exposure (split / repeated fills across page boundaries)
    if pos.repeat_exposed_fill_count > 0:
        net_res = round(pos.settled_net_result, 6) if pos.settled_net_result is not None else None
        return PositionEligibility(
            position_id=pos.position_id,
            is_eligible=False,
            bucket="REPEAT_EXPOSED",
            exclusion_reason=PositionExclusionReason.REPEAT_EXPOSED,
            settlement_class=pos.settlement_class,
            net_result=net_res,
            cost=pos.cost,
            effective_timestamp=effective_ts,
        )

    # Priority 4: Unresolved / Open Positions
    if pos.settlement_class == PositionSettlementClass.UNRESOLVED or pos.remaining_shares > 1e-9:
        return PositionEligibility(
            position_id=pos.position_id,
            is_eligible=False,
            bucket="UNRESOLVED_OPEN",
            exclusion_reason=PositionExclusionReason.UNRESOLVED_OPEN,
            settlement_class=pos.settlement_class,
            net_result=None,
            cost=pos.cost,
            effective_timestamp=effective_ts,
        )

    # Priority 5: Incomplete History / Ambiguous Attribution
    if (
        pos.settlement_class == PositionSettlementClass.UNKNOWN
        or pos.ambiguous_redemption
        or pos.settled_net_result is None
    ):
        reason = (
            PositionExclusionReason.AMBIGUOUS_REDEMPTION
            if pos.ambiguous_redemption
            else PositionExclusionReason.INCOMPLETE_HISTORY
        )
        return PositionEligibility(
            position_id=pos.position_id,
            is_eligible=False,
            bucket="INCOMPLETE",
            exclusion_reason=reason,
            settlement_class=pos.settlement_class,
            net_result=None,
            cost=pos.cost,
            effective_timestamp=effective_ts,
        )

    # Priority 6: Eligible Settled Position (Set E)
    # Net economic result rounded to 6 decimal places (USDC standard precision)
    r = round(pos.settled_net_result, 6)
    is_part = (pos.settlement_class == PositionSettlementClass.PARTIAL)

    if is_part:
        # Partial payout positions are partitioned into partials bucket
        # and excluded from binary win/loss denominator
        return PositionEligibility(
            position_id=pos.position_id,
            is_eligible=True,
            bucket="ELIGIBLE",
            exclusion_reason=PositionExclusionReason.NONE,
            settlement_class=pos.settlement_class,
            net_result=r,
            cost=pos.cost,
            is_win=False,
            is_loss=False,
            is_breakeven=False,
            is_partial=True,
            effective_timestamp=effective_ts,
        )

    # For full settlement (CLOSED trading exit, RESOLVED_WON, RESOLVED_LOST)
    if abs(r) <= tolerance:
        is_breakeven = True
        is_win = False
        is_loss = False
    elif r > tolerance:
        is_breakeven = False
        is_win = True
        is_loss = False
    else:
        is_breakeven = False
        is_win = False
        is_loss = True

    return PositionEligibility(
        position_id=pos.position_id,
        is_eligible=True,
        bucket="ELIGIBLE",
        exclusion_reason=PositionExclusionReason.NONE,
        settlement_class=pos.settlement_class,
        net_result=r,
        cost=pos.cost,
        is_win=is_win,
        is_loss=is_loss,
        is_breakeven=is_breakeven,
        is_partial=False,
        effective_timestamp=effective_ts,
    )


def partition_positions(
    positions: List[PositionRecord],
    tolerance: float = 0.01,
) -> Tuple[List[PositionEligibility], PopulationAccounting]:
    """
    Partitions a list of PositionRecords and calculates population accounting.
    Ensures that every position belongs to exactly one bucket and verifies
    the accounting identities.
    """
    evaluations: List[PositionEligibility] = []

    combo_count = 0
    excluded_fills_count = 0
    unknown_cash_count = 0
    unknown_size_count = 0
    missing_condition_id_count = 0
    zero_cost_count = 0
    repeat_exposed_count = 0
    unresolved_open_count = 0
    incomplete_count = 0

    wins_count = 0
    losses_count = 0
    breakeven_count = 0
    partial_count = 0

    for pos in positions:
        eval_res = evaluate_position_eligibility(pos, tolerance=tolerance)
        evaluations.append(eval_res)

        if eval_res.bucket == "COMBO_EXCLUDED":
            combo_count += 1
        elif eval_res.bucket == "UNRELIABLE_DATA":
            if eval_res.exclusion_reason == PositionExclusionReason.EXCLUDED_FILLS:
                excluded_fills_count += 1
            elif eval_res.exclusion_reason == PositionExclusionReason.UNKNOWN_CASH:
                unknown_cash_count += 1
            elif eval_res.exclusion_reason == PositionExclusionReason.UNKNOWN_SIZE:
                unknown_size_count += 1
            elif eval_res.exclusion_reason == PositionExclusionReason.MISSING_CONDITION_ID:
                missing_condition_id_count += 1
            elif eval_res.exclusion_reason == PositionExclusionReason.ZERO_OR_NEGATIVE_COST:
                zero_cost_count += 1
        elif eval_res.bucket == "REPEAT_EXPOSED":
            repeat_exposed_count += 1
        elif eval_res.bucket == "UNRESOLVED_OPEN":
            unresolved_open_count += 1
        elif eval_res.bucket == "INCOMPLETE":
            incomplete_count += 1
        elif eval_res.bucket == "ELIGIBLE":
            if eval_res.is_partial:
                partial_count += 1
            elif eval_res.is_win:
                wins_count += 1
            elif eval_res.is_loss:
                losses_count += 1
            elif eval_res.is_breakeven:
                breakeven_count += 1

    unreliable_total = (
        excluded_fills_count
        + unknown_cash_count
        + unknown_size_count
        + missing_condition_id_count
        + zero_cost_count
    )
    eligible_total = wins_count + losses_count + breakeven_count + partial_count
    ordinary_total = (
        eligible_total
        + unreliable_total
        + repeat_exposed_count
        + unresolved_open_count
        + incomplete_count
    )
    total_positions = combo_count + ordinary_total

    accounting = PopulationAccounting(
        total_positions=total_positions,
        combo_positions=combo_count,
        ordinary_positions=ordinary_total,
        unreliable_data_count=unreliable_total,
        excluded_fills_count=excluded_fills_count,
        unknown_cash_count=unknown_cash_count,
        unknown_size_count=unknown_size_count,
        missing_condition_id_count=missing_condition_id_count,
        zero_or_negative_cost_count=zero_cost_count,
        repeat_exposed_count=repeat_exposed_count,
        unresolved_open_count=unresolved_open_count,
        incomplete_count=incomplete_count,
        eligible_count=eligible_total,
        wins_count=wins_count,
        losses_count=losses_count,
        breakeven_count=breakeven_count,
        partial_count=partial_count,
    )

    if not accounting.is_identity_valid:
        raise ValueError("Population accounting identity invariant violation.")

    return evaluations, accounting
