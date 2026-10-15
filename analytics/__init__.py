"""
Analytics package providing:
- Phase 6: Position eligibility partitioning, population accounting, performance metrics
- Phase 7: Consolidated logical trades, frequency metrics, descriptive frequency profiles
- Legacy analytics: FrequencyStats, WalletStats, RollingWindowTracker
"""
from analytics.models import (
    PositionRecord,
    PositionEligibility,
    PopulationAccounting,
    WalletStatistics,
    PositionSettlementClass,
    PositionExclusionReason,
    PositionSettlementType,
)
from analytics.eligibility import (
    evaluate_position_eligibility,
    partition_positions,
)
from analytics.performance import (
    compute_wallet_statistics,
    calculate_drawdown,
    build_position_records,
)
from analytics.frequency_models import (
    LogicalTrade,
    FrequencyMetrics,
    FrequencyProfile,
)
from analytics.frequency import (
    FrequencyStats,
    RollingWindowTracker,
    compute_frequency_stats,
    classify_frequency,
    build_frequency_diagnostic,
    format_frequency_diagnostic,
    consolidate_logical_trades,
    compute_frequency_metrics,
    compute_frequency_profile,
)
from analytics.wallet_stats import (
    WalletStats,
    compute_wallet_stats,
)

__all__ = [
    # Phase 6
    "PositionRecord",
    "PositionEligibility",
    "PopulationAccounting",
    "WalletStatistics",
    "PositionSettlementClass",
    "PositionExclusionReason",
    "PositionSettlementType",
    "evaluate_position_eligibility",
    "partition_positions",
    "compute_wallet_statistics",
    "calculate_drawdown",
    "build_position_records",
    # Phase 7
    "LogicalTrade",
    "FrequencyMetrics",
    "FrequencyProfile",
    "consolidate_logical_trades",
    "compute_frequency_metrics",
    "compute_frequency_profile",
    # Legacy
    "FrequencyStats",
    "RollingWindowTracker",
    "compute_frequency_stats",
    "classify_frequency",
    "build_frequency_diagnostic",
    "format_frequency_diagnostic",
    "WalletStats",
    "compute_wallet_stats",
]
