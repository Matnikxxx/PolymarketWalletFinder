"""
Data models and accounting structures for Phase 6 wallet analytics.

Provides frozen dataclasses representing position records, eligibility classification,
strict population accounting identities, and comprehensive wallet performance statistics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any, FrozenSet


class PositionSettlementClass(str, Enum):
    CLOSED = "CLOSED"              # Exited via trading fills before resolution
    WON = "WON"                    # Settled via market resolution (100% win)
    LOST = "LOST"                  # Settled via market resolution (100% loss)
    PARTIAL = "PARTIAL"            # Settled via market resolution with partial payout
    UNRESOLVED = "UNRESOLVED"      # Open position, market not yet resolved
    UNKNOWN = "UNKNOWN"            # Resolution unknown, incomplete, or unclassifiable


class PositionExclusionReason(str, Enum):
    NONE = "NONE"
    COMBO_EXCLUDED = "COMBO_EXCLUDED"
    EXCLUDED_FILLS = "EXCLUDED_FILLS"
    UNKNOWN_CASH = "UNKNOWN_CASH"
    UNKNOWN_SIZE = "UNKNOWN_SIZE"
    MISSING_CONDITION_ID = "MISSING_CONDITION_ID"
    ZERO_OR_NEGATIVE_COST = "ZERO_OR_NEGATIVE_COST"
    REPEAT_EXPOSED = "REPEAT_EXPOSED"
    UNRESOLVED_OPEN = "UNRESOLVED_OPEN"
    INCOMPLETE_HISTORY = "INCOMPLETE_HISTORY"
    AMBIGUOUS_REDEMPTION = "AMBIGUOUS_REDEMPTION"


class PositionSettlementType(str, Enum):
    TRADING_EXIT = "TRADING_EXIT"
    RESOLUTION = "RESOLUTION"
    REDEMPTION = "REDEMPTION"
    NONE = "NONE"


@dataclass(frozen=True)
class PositionRecord:
    """
    Immutable representation of an observed position across its lifecycle.
    """
    position_id: str
    condition_id: Optional[str] = None
    token_id: Optional[str] = None
    market_slug: Optional[str] = None
    outcome: Optional[str] = None
    category: str = "unknown"
    cost: float = 0.0
    proceeds: float = 0.0
    settled_net_result: Optional[float] = None
    settlement_class: PositionSettlementClass = PositionSettlementClass.UNKNOWN
    settlement_type: PositionSettlementType = PositionSettlementType.NONE
    opened_at: Optional[str] = None
    closed_at: Optional[str] = None
    resolved_at: Optional[str] = None
    is_combo: bool = False
    has_excluded_fills: bool = False
    has_unknown_cash: bool = False
    has_unknown_size: bool = False
    repeat_exposed_fill_count: int = 0
    redemption_inferred: bool = False
    ambiguous_redemption: bool = False
    remaining_shares: float = 0.0


@dataclass(frozen=True)
class PositionEligibility:
    """
    Result of evaluating a single PositionRecord for inclusion in eligible set E.
    """
    position_id: str
    is_eligible: bool
    bucket: str                       # "ELIGIBLE", "COMBO_EXCLUDED", "UNRELIABLE_DATA", "REPEAT_EXPOSED", "UNRESOLVED_OPEN", "INCOMPLETE"
    exclusion_reason: PositionExclusionReason
    settlement_class: PositionSettlementClass
    net_result: Optional[float]      # Rounded to 6 decimal places (USDC precision)
    cost: float
    is_win: bool = False             # net_result > +0.01 (non-partial)
    is_loss: bool = False            # net_result < -0.01 (non-partial)
    is_breakeven: bool = False       # |net_result| <= 0.01 (non-partial)
    is_partial: bool = False         # settlement_class == PARTIAL
    effective_timestamp: Optional[str] = None


@dataclass(frozen=True)
class PopulationAccounting:
    """
    Deterministic population accounting verifying that every observed position
    is partitioned into exactly one mutually exclusive bucket.
    """
    total_positions: int
    combo_positions: int
    ordinary_positions: int

    # Excluded buckets for ordinary positions
    unreliable_data_count: int
    excluded_fills_count: int
    unknown_cash_count: int
    unknown_size_count: int
    missing_condition_id_count: int
    zero_or_negative_cost_count: int
    repeat_exposed_count: int
    unresolved_open_count: int
    incomplete_count: int

    # Eligible bucket
    eligible_count: int

    # Sub-breakdown of eligible bucket
    wins_count: int
    losses_count: int
    breakeven_count: int
    partial_count: int

    @property
    def is_identity_valid(self) -> bool:
        """
        Verifies all three fundamental accounting identities:
        1. total_positions == combo_positions + ordinary_positions
        2. ordinary_positions == eligible_count + unreliable_data_count + repeat_exposed_count + unresolved_open_count + incomplete_count
        3. eligible_count == wins_count + losses_count + breakeven_count + partial_count
        """
        total_sum = self.combo_positions + self.ordinary_positions
        ord_sum = (
            self.eligible_count
            + self.unreliable_data_count
            + self.repeat_exposed_count
            + self.unresolved_open_count
            + self.incomplete_count
        )
        unreliable_sum = (
            self.excluded_fills_count
            + self.unknown_cash_count
            + self.unknown_size_count
            + self.missing_condition_id_count
            + self.zero_or_negative_cost_count
        )
        eligible_sum = (
            self.wins_count
            + self.losses_count
            + self.breakeven_count
            + self.partial_count
        )
        return (
            self.total_positions == total_sum
            and self.ordinary_positions == ord_sum
            and self.unreliable_data_count == unreliable_sum
            and self.eligible_count == eligible_sum
        )


@dataclass(frozen=True)
class WalletStatistics:
    """
    Comprehensive, immutable Phase 6 performance statistics for a target wallet.
    Maintains clean-data statistics alongside a diagnostic sensitivity view
    accounting for repeat exposure and data truncation.
    """
    wallet: str
    as_of: Optional[str] = None

    # --- Core Counts (Clean Data) ---
    positions_counted: int = 0         # |E| (input to MIN_POSITIONS filter)
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    partials: int = 0

    # Win rate: 100 * wins / (wins + losses). None if (wins + losses) == 0.
    win_rate_pct: Optional[float] = None

    # --- Financials (Clean Data, Fee-Exclusive) ---
    total_settled_cost: float = 0.0
    total_settled_net_result: float = 0.0
    roi_fee_exclusive_pct: Optional[float] = None  # None if total_settled_cost == 0

    # --- Drawdown (Clean Data) ---
    max_drawdown_usd: Optional[float] = None
    drawdown_cost_pct: Optional[float] = None       # None if total_settled_cost == 0
    drawdown_peak_pct: Optional[float] = None       # None if peak <= 0

    # --- Profit Concentration (Clean Data) ---
    top1_profit_share_pct: Optional[float] = None  # None if gross profit <= 0
    top3_profit_share_pct: Optional[float] = None  # None if gross profit <= 0

    # --- Position Sizing (Clean Data) ---
    mean_position_cost: Optional[float] = None
    median_position_cost: Optional[float] = None
    max_position_cost: Optional[float] = None
    stdev_position_cost: Optional[float] = None     # None if n < 2

    # --- Consistency / Return Dispersion (Clean Data, rho = r / c) ---
    consistency_mean_roi: Optional[float] = None    # None if n < 3
    consistency_median_roi: Optional[float] = None  # None if n < 3
    consistency_stdev_roi: Optional[float] = None   # None if n < 3

    # --- Activity & Timing ---
    active_days: int = 0
    first_activity_at: Optional[str] = None
    last_activity_at: Optional[str] = None

    # --- Data Quality, Coverage & Integrity ---
    coverage_ratio: float = 0.0        # |E| / ordinary_positions
    is_complete_history: bool = True
    retrieval_status: str = "SUFFICIENT" # "SUFFICIENT", "PARTIAL", "INSUFFICIENT"
    reconciled: bool = True

    # --- Disjoint Population Accounting ---
    accounting: Optional[PopulationAccounting] = None

    # --- Sensitivity View (Repeat-Exposed Positions Included) ---
    sensitivity_positions_counted: int = 0
    sensitivity_wins: int = 0
    sensitivity_losses: int = 0
    sensitivity_breakeven: int = 0
    sensitivity_partials: int = 0
    sensitivity_win_rate_pct: Optional[float] = None
    sensitivity_settled_cost: float = 0.0
    sensitivity_settled_net: float = 0.0
    sensitivity_roi_fee_exclusive_pct: Optional[float] = None
    sensitivity_max_drawdown_usd: Optional[float] = None
    sensitivity_drawdown_cost_pct: Optional[float] = None

    # --- Hard-Filter Criteria (Phase 8B) ---
    # is_resolution_only: True  → wallet never exits via trading sells (resolution-only).
    #                     False → wallet has at least one trading-exit position.
    #                     None  → cannot be determined (insufficient position history).
    # Logic mirrors legacy wallet_stats.py: is_resolution_only when no positions have
    # settlement_type == TRADING_EXIT and total ordinary positions >= 3.
    is_resolution_only: Optional[bool] = None

    # category_mix: position counts per category string (e.g. {"politics": 3, "sports": 5}).
    # Counts all ordinary (non-combo) positions to match legacy semantics.
    # None means category information was not available at compute time.
    # An empty dict {} means data was available but all categories were "unknown".
    # Callers must treat None differently from {} — None suppresses the politics filter.
    category_mix: Optional[Dict[str, int]] = None
