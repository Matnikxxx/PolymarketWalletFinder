"""
Immutable dataclasses for Phase 7 Frequency Analysis.

Provides data models for consolidated logical trades, comprehensive frequency metrics,
and descriptive frequency profiles without business-rule rejections or scoring mutations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass(frozen=True)
class LogicalTrade:
    """
    Consolidated logical trade grouping one or more raw fills sharing
    the same on-chain transaction hash.
    """
    tx_hash: Optional[str]
    timestamp: str
    condition_id: Optional[str]
    token_id: Optional[str]
    side: str
    size: float
    usdc_size: float
    fill_count: int


@dataclass(frozen=True)
class FrequencyMetrics:
    """
    Exact mathematical metrics describing trade timing, inter-trade intervals,
    rolling burst intensity, and order-book fill expansion.
    """
    total_logical_trades: int
    raw_fill_count: int
    fill_expansion_factor: float

    min_gap_seconds: Optional[float]
    median_gap_seconds: Optional[float]
    mean_gap_seconds: Optional[float]

    pct_gaps_le_10s: float
    pct_gaps_le_30s: float
    pct_gaps_le_60s: float

    peak_trades_in_1m: int
    peak_trades_in_5m: int
    peak_trades_in_15m: int

    burst_episodes_count: int

    cross_market_max_in_1m: int

    active_span_days: float


@dataclass(frozen=True)
class FrequencyProfile:
    """
    Descriptive frequency profile containing pure analytical facts,
    classification, and data-quality metadata.

    Does NOT mutate wallet scores, does NOT apply filter rejections,
    and does NOT generate recommendations.
    """
    wallet: str

    classification: str
    reasons: list[str]

    metrics: FrequencyMetrics

    data_quality: str
    is_complete_history: bool
    is_reconciled: bool
