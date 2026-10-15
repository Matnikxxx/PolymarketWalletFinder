"""
Wallet trading-frequency analysis.

This is a NEW module (see CLAUDE.md audit note): no rapid-trade / frequency
/ bot-like detection existed anywhere in this codebase before this change.
It reconstructs trade timing from a wallet's normalized activity feed and
produces both raw statistics and an explainable classification.

Two separate concerns live here on purpose:

* Historical classification (`compute_frequency_stats` / `classify_frequency`)
  -- "how has this wallet generally behaved" -- feeds wallet ranking.
* Live burst detection (`RollingWindowTracker`) -- "is this wallet behaving
  like a bot RIGHT NOW" -- feeds the live monitor's per-trade decision.

Neither of these is copy delay (how long *we* wait before simulating a
copy). Copy delay lives in config.settings.PREFERRED_COPY_DELAY /
trading/execution_simulator.py and is untouched by this module.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean, median
from typing import Optional, List, Tuple, Union

from config.settings import settings
from analytics.frequency_models import (
    LogicalTrade,
    FrequencyMetrics,
    FrequencyProfile,
)


FREQUENCY_LABELS = (
    "LOW_FREQUENCY",
    "NORMAL",
    "HIGH_FREQUENCY",
    "RAPID_TRADER",
    "BOT_LIKE",
)


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None

    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class FrequencyStats:
    wallet: str

    total_trades: int = 0

    # Raw /activity rows before tx_hash grouping.
    raw_fill_count: int = 0

    # Number of trade-to-trade gaps analyzed.
    gap_count: int = 0

    # Distinct markets touched.
    unique_markets: int = 0

    active_days: float = 0.0
    trades_per_day: float = 0.0
    trades_per_hour: float = 0.0

    median_gap_seconds: Optional[float] = None
    mean_gap_seconds: Optional[float] = None
    min_gap_seconds: Optional[float] = None

    pct_within_10s: float = 0.0
    pct_within_30s: float = 0.0
    pct_within_60s: float = 0.0
    pct_within_5m: float = 0.0
    pct_within_15m: float = 0.0
    pct_within_1h: float = 0.0

    max_trades_in_1m_window: int = 0
    max_trades_in_5m_window: int = 0
    max_trades_in_15m_window: int = 0

    avg_holding_seconds: Optional[float] = None
    median_holding_seconds: Optional[float] = None

    # % of matched positions held <= RAPID_WINDOW_SECONDS.
    pct_short_holds: float = 0.0

    classification: str = "NORMAL"
    classification_reasons: list = field(default_factory=list)

    insufficient_data: bool = False


def _max_in_rolling_window(
    timestamps: list[datetime],
    window_seconds: float,
) -> int:
    """Return the maximum number of trades inside any rolling window."""

    if not timestamps:
        return 0

    window = deque()
    best = 0

    for ts in timestamps:
        window.append(ts)

        while (ts - window[0]).total_seconds() > window_seconds:
            window.popleft()

        best = max(best, len(window))

    return best


def _reconstruct_holding_times(events: list[dict]) -> list[float]:
    """
    Lightweight BUY -> fully-closed holding-time reconstruction.

    This is independent of trading/position_manager.py because that tracks
    our paper positions, not the historical positions of another wallet.
    """

    open_since: dict[str, datetime] = {}
    remaining: dict[str, float] = {}

    holding_times: list[float] = []

    for ev in events:
        key = (
            ev.get("condition_id")
            or ev.get("token_id")
            or ev.get("slug")
            or ev.get("market_title")
        )

        ts = _parse_ts(ev.get("timestamp"))

        if not key or ts is None:
            continue

        size = ev.get("size", 0.0) or 0.0

        if ev.get("side") == "BUY":

            if key not in open_since:
                open_since[key] = ts
                remaining[key] = 0.0

            remaining[key] += size

        elif ev.get("side") == "SELL":

            if key not in open_since:
                continue

            remaining[key] = max(
                0.0,
                remaining.get(key, 0.0) - size,
            )

            if remaining[key] <= 1e-9:

                holding_times.append(
                    (ts - open_since[key]).total_seconds()
                )

                del open_since[key]
                del remaining[key]

    return holding_times


def _group_fills_into_trade_events(events: list[dict]) -> list[dict]:
    """
    Groups raw activity fill rows sharing the same tx_hash into ONE
    logical trade event.

    Events without tx_hash remain separate.

    This prevents multiple partial fills from one transaction being
    incorrectly counted as multiple independent trading decisions.
    """

    groups: dict[str, list[dict]] = {}

    singleton_counter = 0

    for ev in events:

        key = ev.get("tx_hash")

        if not key:
            singleton_counter += 1
            key = f"__no_tx_hash_{singleton_counter}"

        groups.setdefault(key, []).append(ev)

    trade_events = []

    for group in groups.values():

        group_sorted = sorted(
            group,
            key=lambda e: e.get("timestamp") or "",
        )

        first = group_sorted[0]

        trade_events.append(
            {
                "wallet": first.get("wallet"),
                "side": first.get("side"),
                "condition_id": first.get("condition_id"),
                "token_id": first.get("token_id"),
                "slug": first.get("slug"),
                "market_title": first.get("market_title"),
                "timestamp": first.get("timestamp"),

                "size": sum(
                    (e.get("size") or 0.0)
                    for e in group
                ),

                "usdc_size": sum(
                    (e.get("usdc_size") or 0.0)
                    for e in group
                ),

                "tx_hash": first.get("tx_hash"),

                "fill_count": len(group),
            }
        )

    trade_events.sort(
        key=lambda e: e.get("timestamp") or ""
    )

    return trade_events


NON_TRADE_TYPES = {
    "REDEEM",
    "SPLIT",
    "MERGE",
    "DEPOSIT",
    "WITHDRAWAL",
    "REWARD",
    "YIELD",
    "REBATE",
}


def consolidate_logical_trades(
    events: list[dict],
    exclude_repeat_exposed: bool = True,
) -> list[LogicalTrade]:
    """
    Public wrapper for consolidating raw fill events into LogicalTrade objects.

    - Excludes non-TRADE activity types (REDEEM, SPLIT, MERGE, DEPOSIT, WITHDRAWAL, REWARD, YIELD, REBATE).
    - If exclude_repeat_exposed is True, excludes events marked as repeat-exposed.
    - Multiple fills sharing the same non-empty tx_hash are collapsed into one logical trade.
    - Events without a tx_hash (or empty string) remain separate singleton trades.
    - Preserves chronological sorting by timestamp.
    """
    groups: dict[str, list[dict]] = {}
    singleton_id = 0

    for ev in events:
        act_type = ev.get("activity_type") or ev.get("type")
        if act_type:
            act_str = str(act_type).strip().upper()
            if act_str != "TRADE" or act_str in NON_TRADE_TYPES:
                continue

        if exclude_repeat_exposed:
            if ev.get("repeat_exposed") or (ev.get("repeat_exposed_fill_count", 0) > 0):
                continue

        tx = ev.get("tx_hash")
        if not tx:
            singleton_id += 1
            key = f"__no_tx_hash_{singleton_id}"
        else:
            key = str(tx)

        groups.setdefault(key, []).append(ev)

    trade_events: list[LogicalTrade] = []

    for group in groups.values():
        group_sorted = sorted(
            group,
            key=lambda e: e.get("timestamp") or "",
        )
        first = group_sorted[0]
        tx_val = first.get("tx_hash") or None

        trade_events.append(
            LogicalTrade(
                tx_hash=tx_val,
                timestamp=str(first.get("timestamp") or ""),
                condition_id=first.get("condition_id"),
                token_id=first.get("token_id"),
                side=str(first.get("side") or ""),
                size=float(sum((e.get("size") or 0.0) for e in group)),
                usdc_size=float(sum((e.get("usdc_size") or 0.0) for e in group)),
                fill_count=len(group),
            )
        )

    trade_events.sort(
        key=lambda e: e.timestamp or ""
    )

    return trade_events


def compute_frequency_stats(
    wallet: str,
    events: list[dict],
) -> FrequencyStats:
    """
    Calculate historical trading-frequency statistics.

    events:
        Normalized activity events.

    The function sorts events internally.

    Raw fills are first grouped by tx_hash into logical trade events.

    `total_trades` therefore represents logical trades rather than raw
    partial-fill rows.

    `raw_fill_count` preserves the original number of activity rows.
    """

    raw_fill_count = len(events)

    trade_events = _group_fills_into_trade_events(events)

    stats = FrequencyStats(
        wallet=wallet,
        total_trades=len(trade_events),
    )

    stats.raw_fill_count = raw_fill_count

    if not trade_events:

        stats.insufficient_data = True

        stats.classification_reasons.append(
            "No trade activity available."
        )

        return stats

    timestamps = sorted(
        t
        for t in (
            _parse_ts(e.get("timestamp"))
            for e in trade_events
        )
        if t is not None
    )

    stats.total_trades = len(timestamps)

    if len(timestamps) < settings.MIN_TRADES_FOR_FREQUENCY_CLASSIFICATION:

        stats.insufficient_data = True
        stats.classification = "NORMAL"

        stats.unique_markets = len(
            {
                e.get("condition_id")
                or e.get("token_id")
                or e.get("slug")
                for e in trade_events
            }
        )

        stats.classification_reasons.append(
            f"Only {len(timestamps)} trade(s) with a timestamp -- "
            f"below the "
            f"{settings.MIN_TRADES_FOR_FREQUENCY_CLASSIFICATION}"
            f"-trade minimum to classify frequency reliably."
        )

        return stats

    span_seconds = (
        timestamps[-1] - timestamps[0]
    ).total_seconds()

    stats.active_days = max(
        span_seconds / 86400.0,
        1.0 / 86400.0,
    )

    stats.trades_per_day = (
        stats.total_trades / stats.active_days
    )

    stats.trades_per_hour = (
        stats.trades_per_day / 24.0
    )

    gaps = [
        (timestamps[i] - timestamps[i - 1]).total_seconds()
        for i in range(1, len(timestamps))
    ]

    stats.gap_count = len(gaps)

    if gaps:

        stats.median_gap_seconds = median(gaps)
        stats.mean_gap_seconds = mean(gaps)
        stats.min_gap_seconds = min(gaps)

        n = len(gaps)

        stats.pct_within_10s = (
            sum(1 for g in gaps if g <= 10)
            / n
            * 100.0
        )

        stats.pct_within_30s = (
            sum(1 for g in gaps if g <= 30)
            / n
            * 100.0
        )

        stats.pct_within_60s = (
            sum(1 for g in gaps if g <= 60)
            / n
            * 100.0
        )

        stats.pct_within_5m = (
            sum(1 for g in gaps if g <= 300)
            / n
            * 100.0
        )

        stats.pct_within_15m = (
            sum(1 for g in gaps if g <= 900)
            / n
            * 100.0
        )

        stats.pct_within_1h = (
            sum(1 for g in gaps if g <= 3600)
            / n
            * 100.0
        )

    stats.max_trades_in_1m_window = (
        _max_in_rolling_window(
            timestamps,
            60,
        )
    )

    stats.max_trades_in_5m_window = (
        _max_in_rolling_window(
            timestamps,
            300,
        )
    )

    stats.max_trades_in_15m_window = (
        _max_in_rolling_window(
            timestamps,
            900,
        )
    )

    stats.unique_markets = len(
        {
            e.get("condition_id")
            or e.get("token_id")
            or e.get("slug")
            for e in trade_events
        }
    )

    holding_times = _reconstruct_holding_times(
        trade_events
    )

    if holding_times:

        stats.avg_holding_seconds = mean(
            holding_times
        )

        stats.median_holding_seconds = median(
            holding_times
        )

        stats.pct_short_holds = (
            sum(
                1
                for h in holding_times
                if h <= settings.RAPID_WINDOW_SECONDS
            )
            / len(holding_times)
            * 100.0
        )

    classify_frequency(stats)

    return stats


def classify_frequency(
    stats: FrequencyStats,
) -> FrequencyStats:
    """
    Classify a wallet using the configured frequency thresholds.

    The checks are ordered from most severe to least severe.
    """

    if stats.insufficient_data:
        return stats

    reasons = []

    # ---------------------------------------------------------------
    # BOT_LIKE
    #
    # BOTH conditions must be true.
    # ---------------------------------------------------------------

    is_bot_like = (
        stats.max_trades_in_1m_window
        >= settings.BOT_LIKE_MAX_TRADES_IN_1M
        and
        stats.pct_within_10s
        >= settings.BOT_LIKE_PCT_WITHIN_10S
    )

    if is_bot_like:

        stats.classification = "BOT_LIKE"

        if (
            stats.max_trades_in_1m_window
            >= settings.BOT_LIKE_MAX_TRADES_IN_1M
        ):
            reasons.append(
                f"{stats.max_trades_in_1m_window} trades occurred "
                f"within a single 1-minute window "
                f"(>= {settings.BOT_LIKE_MAX_TRADES_IN_1M})."
            )

        if (
            stats.pct_within_10s
            >= settings.BOT_LIKE_PCT_WITHIN_10S
        ):
            reasons.append(
                f"{stats.pct_within_10s:.1f}% of trade gaps "
                f"are <= 10 seconds "
                f"(>= {settings.BOT_LIKE_PCT_WITHIN_10S}%)."
            )

        stats.classification_reasons = reasons

        return stats

    # ---------------------------------------------------------------
    # RAPID_TRADER
    # ---------------------------------------------------------------

    is_rapid = (
        stats.max_trades_in_5m_window
        >= settings.MAX_TRADES_IN_WINDOW

        or

        stats.pct_within_60s
        >= settings.MAX_SHORT_INTERVAL_PERCENT

        or

        (
            stats.median_gap_seconds is not None
            and
            stats.median_gap_seconds
            < settings.RAPID_WINDOW_SECONDS * 5
        )
    )

    if is_rapid:

        stats.classification = "RAPID_TRADER"

        if (
            stats.max_trades_in_5m_window
            >= settings.MAX_TRADES_IN_WINDOW
        ):
            reasons.append(
                f"{stats.max_trades_in_5m_window} trades occurred "
                f"within a single 5-minute window "
                f"(>= {settings.MAX_TRADES_IN_WINDOW})."
            )

        if (
            stats.pct_within_60s
            >= settings.MAX_SHORT_INTERVAL_PERCENT
        ):
            reasons.append(
                f"{stats.pct_within_60s:.1f}% of trade gaps "
                f"are <= 60 seconds "
                f"(>= {settings.MAX_SHORT_INTERVAL_PERCENT}%)."
            )

        if (
            stats.median_gap_seconds is not None
            and
            stats.median_gap_seconds
            < settings.RAPID_WINDOW_SECONDS * 5
        ):
            reasons.append(
                f"Median gap between trades is only "
                f"{stats.median_gap_seconds:.0f}s."
            )

        stats.classification_reasons = reasons

        return stats

    # ---------------------------------------------------------------
    # HIGH_FREQUENCY
    # ---------------------------------------------------------------

    is_high_frequency = (
        stats.trades_per_day
        >= settings.HIGH_FREQUENCY_TRADES_PER_DAY

        or

        (
            stats.median_gap_seconds is not None
            and
            stats.median_gap_seconds
            < settings.HIGH_FREQUENCY_MEDIAN_GAP_SECONDS
        )
    )

    if is_high_frequency:

        stats.classification = "HIGH_FREQUENCY"

        if (
            stats.trades_per_day
            >= settings.HIGH_FREQUENCY_TRADES_PER_DAY
        ):
            reasons.append(
                f"{stats.trades_per_day:.1f} trades/day "
                f"(>= {settings.HIGH_FREQUENCY_TRADES_PER_DAY})."
            )

        if (
            stats.median_gap_seconds is not None
            and
            stats.median_gap_seconds
            < settings.HIGH_FREQUENCY_MEDIAN_GAP_SECONDS
        ):
            reasons.append(
                f"Median gap "
                f"{stats.median_gap_seconds:.0f}s "
                f"is under "
                f"{settings.HIGH_FREQUENCY_MEDIAN_GAP_SECONDS:.0f}s."
            )

        stats.classification_reasons = reasons

        return stats

    # ---------------------------------------------------------------
    # LOW_FREQUENCY
    # ---------------------------------------------------------------

    if (
        stats.trades_per_day
        < settings.LOW_FREQUENCY_MAX_TRADES_PER_DAY
    ):

        stats.classification = "LOW_FREQUENCY"

        reasons.append(
            f"{stats.trades_per_day:.2f} trades/day "
            f"is below the "
            f"{settings.LOW_FREQUENCY_MAX_TRADES_PER_DAY} "
            f"occasional-trader threshold."
        )

        stats.classification_reasons = reasons

        return stats

    # ---------------------------------------------------------------
    # NORMAL
    # ---------------------------------------------------------------

    stats.classification = "NORMAL"

    reasons.append(
        "Trading cadence does not trip any "
        "low/high/rapid/bot-like threshold."
    )

    stats.classification_reasons = reasons

    return stats


# ---------------------------------------------------------------------
# Live burst detection
# ---------------------------------------------------------------------

class RollingWindowTracker:
    """
    Tracks recent trade timestamps per wallet during a live monitoring
    session.

    This is memory-only and is not persisted.
    """

    def __init__(self):
        self._recent: dict[str, deque] = {}

    def record_and_check(
        self,
        wallet: str,
        timestamp: datetime,
    ) -> tuple[bool, str, int, Optional[float]]:

        dq = self._recent.setdefault(
            wallet.lower(),
            deque(),
        )

        seconds_since_last = None

        if dq:
            seconds_since_last = (
                timestamp - dq[-1]
            ).total_seconds()

        dq.append(timestamp)

        while (
            dq
            and
            (timestamp - dq[0]).total_seconds()
            > settings.RAPID_WINDOW_SECONDS
        ):
            dq.popleft()

        trades_in_window = len(dq)

        if (
            seconds_since_last is not None
            and
            seconds_since_last
            < settings.MIN_SECONDS_BETWEEN_TRADES
        ):
            return (
                True,
                (
                    f"Only {seconds_since_last:.1f}s since "
                    f"this wallet's last trade "
                    f"(< {settings.MIN_SECONDS_BETWEEN_TRADES}s "
                    f"minimum spacing)."
                ),
                trades_in_window,
                seconds_since_last,
            )

        if (
            trades_in_window
            >= settings.MAX_TRADES_IN_WINDOW
        ):
            return (
                True,
                (
                    f"{trades_in_window} trades from this wallet "
                    f"within the last "
                    f"{settings.RAPID_WINDOW_SECONDS}s "
                    f"(>= {settings.MAX_TRADES_IN_WINDOW})."
                ),
                trades_in_window,
                seconds_since_last,
            )

        return (
            False,
            "within normal trading cadence",
            trades_in_window,
            seconds_since_last,
        )


# ---------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------

def build_frequency_diagnostic(
    stats: Union[FrequencyProfile, FrequencyStats],
) -> dict:
    """
    Build a diagnostic dictionary explaining the frequency classification.
    Supports canonical Phase 7 FrequencyProfile and legacy FrequencyStats.
    """
    if isinstance(stats, FrequencyProfile):
        m = stats.metrics
        min_gap = (
            f"{m.min_gap_seconds:.0f}s"
            if m.min_gap_seconds is not None
            else "n/a"
        )
        median_gap = (
            f"{m.median_gap_seconds:.0f}s"
            if m.median_gap_seconds is not None
            else "n/a"
        )
        rule_trades = (
            m.peak_trades_in_1m
            >= settings.BOT_LIKE_MAX_TRADES_IN_1M
        )
        rule_percentage = (
            m.pct_gaps_le_10s
            >= settings.BOT_LIKE_PCT_WITHIN_10S
        )
        return {
            "wallet": stats.wallet,
            "raw_fill_count": m.raw_fill_count,
            "logical_trades": m.total_logical_trades,
            "unique_markets": m.cross_market_max_in_1m,
            "gap_count": max(0, m.total_logical_trades - 1),
            "min_gap": min_gap,
            "median_gap": median_gap,
            "pct_gaps_le_10s": round(m.pct_gaps_le_10s, 1),
            "max_trades_60s": m.peak_trades_in_1m,
            "rule_5_trades_60s": rule_trades,
            "rule_5_trades_60s_threshold": settings.BOT_LIKE_MAX_TRADES_IN_1M,
            "rule_30pct_gaps_le_10s": rule_percentage,
            "rule_30pct_gaps_le_10s_threshold": settings.BOT_LIKE_PCT_WITHIN_10S,
            "classification": stats.classification,
            "insufficient_data": (stats.classification == "INSUFFICIENT_DATA"),
        }

    min_gap = (
        f"{stats.min_gap_seconds:.0f}s"
        if stats.min_gap_seconds is not None
        else "n/a"
    )

    median_gap = (
        f"{stats.median_gap_seconds:.0f}s"
        if stats.median_gap_seconds is not None
        else "n/a"
    )

    rule_trades = (
        stats.max_trades_in_1m_window
        >= settings.BOT_LIKE_MAX_TRADES_IN_1M
    )

    rule_percentage = (
        stats.pct_within_10s
        >= settings.BOT_LIKE_PCT_WITHIN_10S
    )

    return {
        "wallet": stats.wallet,

        "raw_fill_count": stats.raw_fill_count,

        "logical_trades": stats.total_trades,

        "unique_markets": stats.unique_markets,

        "gap_count": stats.gap_count,

        "min_gap": min_gap,

        "median_gap": median_gap,

        "pct_gaps_le_10s": round(
            stats.pct_within_10s,
            1,
        ),

        "max_trades_60s": (
            stats.max_trades_in_1m_window
        ),

        "rule_5_trades_60s": rule_trades,

        "rule_5_trades_60s_threshold": (
            settings.BOT_LIKE_MAX_TRADES_IN_1M
        ),

        "rule_30pct_gaps_le_10s": rule_percentage,

        "rule_30pct_gaps_le_10s_threshold": (
            settings.BOT_LIKE_PCT_WITHIN_10S
        ),

        "classification": stats.classification,

        "insufficient_data": stats.insufficient_data,
    }


def format_frequency_diagnostic(
    diag: dict,
) -> str:
    """
    Format the diagnostic dictionary as a compact log block.
    """

    trades_threshold = (
        diag["rule_5_trades_60s_threshold"]
    )

    pct_threshold = (
        diag["rule_30pct_gaps_le_10s_threshold"]
    )

    trades_label = (
        f"rule_{int(trades_threshold)}_trades_60s"
    )

    pct_label = (
        f"rule_{pct_threshold:g}pct_gaps_le_10s"
    )

    return (
        "FREQUENCY_DIAGNOSTIC\n"

        f"wallet={diag['wallet']}\n"

        f"raw_fill_count="
        f"{diag['raw_fill_count']}\n"

        f"logical_trades="
        f"{diag['logical_trades']}\n"

        f"unique_markets="
        f"{diag['unique_markets']}\n"

        f"gap_count="
        f"{diag['gap_count']}\n"

        f"min_gap="
        f"{diag['min_gap']}\n"

        f"median_gap="
        f"{diag['median_gap']}\n"

        f"pct_gaps_le_10s="
        f"{diag['pct_gaps_le_10s']}%\n"

        f"max_trades_60s="
        f"{diag['max_trades_60s']}\n"

        f"{trades_label}="
        f"{'YES' if diag['rule_5_trades_60s'] else 'NO'} "
        f"(threshold: {trades_threshold:g})\n"

        f"{pct_label}="
        f"{'YES' if diag['rule_30pct_gaps_le_10s'] else 'NO'} "
        f"(threshold: {pct_threshold:g}%)\n"

        f"classification="
        f"{diag['classification']}"
    )


# ---------------------------------------------------------------------
# Phase 7: Frequency Metrics & Profile Classification
# ---------------------------------------------------------------------

def _max_cross_market_in_rolling_window(
    pairs: list[tuple[datetime, LogicalTrade]],
    window_sec: float = 60.0,
) -> int:
    """
    Finds the maximum number of distinct condition IDs traded within any
    rolling window of `window_sec` seconds.
    """
    if not pairs:
        return 0
    max_markets = 0
    for i in range(len(pairs)):
        t_start = pairs[i][0]
        markets = set()
        for j in range(i, len(pairs)):
            t_curr, trade = pairs[j]
            if (t_curr - t_start).total_seconds() <= window_sec:
                cid = trade.condition_id or trade.token_id
                if cid:
                    markets.add(cid)
            else:
                break
        if len(markets) > max_markets:
            max_markets = len(markets)
    return max_markets


def compute_frequency_metrics(
    trades: list[LogicalTrade],
    raw_fill_count: int,
) -> FrequencyMetrics:
    """
    Computes mathematical metrics describing trade timing, inter-trade intervals,
    rolling burst intensity, and order-book fill expansion.
    """
    total_logical = len(trades)
    fill_expansion = (float(raw_fill_count) / float(total_logical)) if total_logical > 0 else 0.0

    pairs: list[tuple[datetime, LogicalTrade]] = []
    for t in trades:
        dt = _parse_ts(t.timestamp)
        if dt is not None:
            pairs.append((dt, t))

    pairs.sort(key=lambda p: p[0])

    if len(pairs) <= 1:
        min_gap = None
        median_gap = None
        mean_gap = None
        pct_10s = 0.0
        pct_30s = 0.0
        pct_60s = 0.0
        burst_episodes = 0
        active_span = 0.0
    else:
        gaps = [
            (pairs[i][0] - pairs[i - 1][0]).total_seconds()
            for i in range(1, len(pairs))
        ]
        min_gap = float(min(gaps))
        median_gap = float(median(gaps))
        mean_gap = float(mean(gaps))
        n_gaps = len(gaps)
        pct_10s = (sum(1 for g in gaps if g <= 10.0) / n_gaps) * 100.0
        pct_30s = (sum(1 for g in gaps if g <= 30.0) / n_gaps) * 100.0
        pct_60s = (sum(1 for g in gaps if g <= 60.0) / n_gaps) * 100.0

        burst_episodes = 0
        in_burst = False
        for g in gaps:
            if g <= 10.0:
                if not in_burst:
                    burst_episodes += 1
                    in_burst = True
            else:
                in_burst = False

        active_span = (pairs[-1][0] - pairs[0][0]).total_seconds() / 86400.0

    timestamps = [p[0] for p in pairs]

    def _calc_peak(window_sec: float) -> int:
        if not timestamps:
            return 0
        max_c = 1
        left = 0
        for right in range(len(timestamps)):
            while (timestamps[right] - timestamps[left]).total_seconds() > window_sec:
                left += 1
            c = right - left + 1
            if c > max_c:
                max_c = c
        return max_c

    peak_1m = _calc_peak(60.0)
    peak_5m = _calc_peak(300.0)
    peak_15m = _calc_peak(900.0)

    cross_market_1m = _max_cross_market_in_rolling_window(pairs, 60.0)

    return FrequencyMetrics(
        total_logical_trades=total_logical,
        raw_fill_count=raw_fill_count,
        fill_expansion_factor=fill_expansion,
        min_gap_seconds=min_gap,
        median_gap_seconds=median_gap,
        mean_gap_seconds=mean_gap,
        pct_gaps_le_10s=pct_10s,
        pct_gaps_le_30s=pct_30s,
        pct_gaps_le_60s=pct_60s,
        peak_trades_in_1m=peak_1m,
        peak_trades_in_5m=peak_5m,
        peak_trades_in_15m=peak_15m,
        burst_episodes_count=burst_episodes,
        cross_market_max_in_1m=cross_market_1m,
        active_span_days=active_span,
    )


def compute_frequency_profile(
    wallet: str,
    events: list[dict],
    is_complete_history: bool = True,
    is_reconciled: bool = True,
) -> FrequencyProfile:
    """
    Computes a descriptive FrequencyProfile for the given wallet and activity events.

    Purely descriptive: does NOT modify scores, does NOT apply filter rejections,
    and does NOT generate recommendations.
    """
    trades = consolidate_logical_trades(events, exclude_repeat_exposed=True)
    metrics = compute_frequency_metrics(trades, raw_fill_count=len(events))

    reasons: list[str] = []

    if not is_complete_history:
        data_quality = "PARTIAL"
    elif not is_reconciled:
        data_quality = "UNRECONCILED"
    else:
        data_quality = "RECONCILED"

    if not is_reconciled:
        reasons.append("Activity data has not undergone complete reconciliation against position records.")

    if not is_complete_history:
        classification = "INSUFFICIENT_DATA"
        reasons.append("Activity history is incomplete / truncated; frequency cannot be reliably determined.")
    elif len(trades) < settings.MIN_TRADES_FOR_FREQUENCY_CLASSIFICATION:
        classification = "INSUFFICIENT_DATA"
        reasons.append(
            f"Only {len(trades)} logical trade(s) available, below the "
            f"{settings.MIN_TRADES_FOR_FREQUENCY_CLASSIFICATION}-trade minimum for frequency classification."
        )
    elif (
        metrics.peak_trades_in_1m >= settings.BOT_LIKE_MAX_TRADES_IN_1M
        and metrics.pct_gaps_le_10s >= settings.BOT_LIKE_PCT_WITHIN_10S
    ):
        classification = "BOT_LIKE"
        reasons.append(
            f"Bot-like burst activity: {metrics.peak_trades_in_1m} trades in 1m "
            f"(>= {settings.BOT_LIKE_MAX_TRADES_IN_1M}) and "
            f"{metrics.pct_gaps_le_10s:.1f}% gaps <= 10s "
            f"(>= {settings.BOT_LIKE_PCT_WITHIN_10S}%)."
        )
    elif (
        metrics.peak_trades_in_5m >= settings.MAX_TRADES_IN_WINDOW
        or metrics.pct_gaps_le_60s >= settings.MAX_SHORT_INTERVAL_PERCENT
        or (
            metrics.median_gap_seconds is not None
            and metrics.median_gap_seconds < settings.RAPID_WINDOW_SECONDS * 5
        )
    ):
        classification = "RAPID_TRADER"
        if metrics.peak_trades_in_5m >= settings.MAX_TRADES_IN_WINDOW:
            reasons.append(
                f"Rapid trading burst: {metrics.peak_trades_in_5m} trades in 5m "
                f"(>= {settings.MAX_TRADES_IN_WINDOW})."
            )
        if metrics.pct_gaps_le_60s >= settings.MAX_SHORT_INTERVAL_PERCENT:
            reasons.append(
                f"{metrics.pct_gaps_le_60s:.1f}% gaps <= 60s "
                f"(>= {settings.MAX_SHORT_INTERVAL_PERCENT}%)."
            )
        if (
            metrics.median_gap_seconds is not None
            and metrics.median_gap_seconds < settings.RAPID_WINDOW_SECONDS * 5
        ):
            reasons.append(
                f"Median gap {metrics.median_gap_seconds:.1f}s is below "
                f"{settings.RAPID_WINDOW_SECONDS * 5}s."
            )
    elif (
        (
            metrics.active_span_days > 0
            and (metrics.total_logical_trades / max(metrics.active_span_days, 1.0 / 86400.0))
            >= settings.HIGH_FREQUENCY_TRADES_PER_DAY
        )
        or (
            metrics.median_gap_seconds is not None
            and metrics.median_gap_seconds < settings.HIGH_FREQUENCY_MEDIAN_GAP_SECONDS
        )
    ):
        classification = "ACTIVE"
        reasons.append("High trading frequency / short inter-trade cadence.")
    else:
        classification = "NORMAL"
        reasons.append("Trading cadence within normal non-burst parameters.")

    return FrequencyProfile(
        wallet=wallet,
        classification=classification,
        reasons=reasons,
        metrics=metrics,
        data_quality=data_quality,
        is_complete_history=is_complete_history,
        is_reconciled=is_reconciled,
    )