"""
Transparent wallet ranking/scoring (spec section 3). Every score comes with
a written explanation of why it was awarded -- no black-box numbers.

Phase 9 migration: Canonical input models are Phase 6 WalletStatistics and
Phase 7 FrequencyProfile, with full backward compatibility for legacy
WalletStats and FrequencyStats.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

from analytics.frequency import FrequencyStats
from analytics.frequency_models import FrequencyProfile
from analytics.models import WalletStatistics
from analytics.wallet_stats import WalletStats
from config.settings import settings


@dataclass
class WalletScoreCard:
    wallet: str
    roi_pct: float
    win_rate_pct: float
    drawdown_pct: float
    num_positions: int
    avg_position_size: float
    recent_profitability: bool
    consistency_score: float
    category_mix: dict
    risk_score: float
    final_score: float
    recommendation: str
    frequency_classification: str = "UNKNOWN"
    frequency_median_gap_seconds: Optional[float] = None
    frequency_pct_short: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _extract_wallet_inputs(stats: Union[WalletStatistics, WalletStats]):
    if isinstance(stats, WalletStatistics):
        wallet = stats.wallet
        roi = stats.roi_fee_exclusive_pct
        win_rate = stats.win_rate_pct
        drawdown = stats.drawdown_cost_pct
        positions = stats.positions_counted
        avg_size = stats.mean_position_cost if stats.mean_position_cost is not None else 0.0
        active_days = stats.active_days
        is_resolution_only = bool(stats.is_resolution_only)
        category_mix = dict(stats.category_mix) if stats.category_mix else {}
        wins = stats.wins
        losses = stats.losses

        # Consistency score
        consistency = getattr(stats, "consistency_score", None)
        if consistency is None:
            m = stats.consistency_mean_roi
            sd = stats.consistency_stdev_roi
            if m is not None and sd is not None:
                if m != 0:
                    cv = abs(sd / m)
                    consistency = max(0.0, min(10.0, 10.0 / (1.0 + cv)))
                else:
                    consistency = 0.0
            else:
                consistency = None

        has_dispersion_history = consistency is not None
        dispersion_count = stats.positions_counted
        concentration = stats.top1_profit_share_pct
    else:
        wallet = stats.wallet
        roi = getattr(stats, "roi_fee_exclusive_pct", None)
        if roi is None:
            roi = getattr(stats, "roi_pct", 0.0)
        win_rate = getattr(stats, "win_rate_pct", 0.0)
        drawdown = getattr(stats, "drawdown_cost_pct", None)
        if drawdown is None:
            drawdown = getattr(stats, "max_drawdown_pct", 0.0)
        positions = getattr(stats, "positions_counted", None)
        if positions is None:
            positions = getattr(stats, "total_positions", 0)
        avg_size = getattr(stats, "mean_position_cost", None)
        if avg_size is None:
            avg_size = getattr(stats, "avg_position_size", 0.0)
        active_days = getattr(stats, "active_days", 0)
        is_resolution_only = bool(getattr(stats, "is_resolution_only", False))
        category_mix = getattr(stats, "category_mix", {}) or {}
        wins = getattr(stats, "wins", 0)
        losses = getattr(stats, "losses", 0)

        consistency = getattr(stats, "consistency_score", None)
        per_trade_pnls = getattr(stats, "per_trade_pnls", [])
        has_dispersion_history = len(per_trade_pnls) >= 3
        dispersion_count = len(per_trade_pnls)
        if not has_dispersion_history:
            consistency = None

        concentration = getattr(stats, "top1_profit_share_pct", None)
        if concentration is None:
            concentration = getattr(stats, "concentration_pct", 0.0)

    return (
        wallet, roi, win_rate, drawdown, positions, avg_size, active_days,
        is_resolution_only, category_mix, wins, losses, consistency,
        has_dispersion_history, dispersion_count, concentration
    )


def _extract_frequency_inputs(freq: Optional[Union[FrequencyProfile, FrequencyStats]]):
    if freq is None:
        return None, [], False, None, 0.0

    if isinstance(freq, FrequencyProfile):
        classification = freq.classification
        reasons = freq.reasons
        is_insufficient = (classification == "INSUFFICIENT_DATA") or (freq.data_quality in ("PARTIAL", "INSUFFICIENT"))
        median_gap = freq.metrics.median_gap_seconds if freq.metrics else None
        pct_short = freq.metrics.pct_gaps_le_60s if freq.metrics else 0.0
    else:
        classification = getattr(freq, "classification", "UNKNOWN")
        reasons = getattr(freq, "classification_reasons", []) or getattr(freq, "reasons", [])
        is_insufficient = getattr(freq, "insufficient_data", False) or (classification == "INSUFFICIENT_DATA")
        median_gap = getattr(freq, "median_gap_seconds", None)
        pct_short = getattr(freq, "pct_short_holds", 0.0)

    return classification, reasons, is_insufficient, median_gap, pct_short


def score_wallet(
    stats: Union[WalletStatistics, WalletStats],
    freq: Optional[Union[FrequencyProfile, FrequencyStats]] = None,
) -> WalletScoreCard:
    """
    Weighted 0-100 score, recalibrated against what the scanner's current
    data pipeline can reliably measure:

      ROI            20 pts
      Win rate       20 pts
      Sample size    15 pts
      Drawdown       15 pts
      Concentration  10 pts
      Consistency    10 pts  (uncertainty / half credit if < 3 positions)
      Category        5 pts  (neutral / 3 pts if unknown)
      Recency         5 pts  (active_days + positive ROI)

    Primary input: Phase 6 WalletStatistics and Phase 7 FrequencyProfile.
    Backward compatibility adapter: legacy WalletStats and FrequencyStats.
    """
    reasons: list[str] = []
    score = 0.0

    (
        wallet, roi, win_rate, drawdown, positions, avg_size, active_days,
        is_resolution_only, category_mix, wins, losses, consistency,
        has_dispersion_history, dispersion_count, concentration
    ) = _extract_wallet_inputs(stats)

    (
        freq_classification, freq_reasons, is_insufficient_freq,
        freq_median_gap, freq_pct_short
    ) = _extract_frequency_inputs(freq)

    # --- ROI (20 pts) ------------------------------------------------
    if roi is None:
        roi_points = 0.0
        reasons.append("No settled ROI available to evaluate -- earns 0/20 pts.")
    elif roi > settings.REJECT_ROI_ABOVE:
        reasons.append(
            f"ROI {roi:.1f}% exceeds reject threshold "
            f"({settings.REJECT_ROI_ABOVE}%) -- suspiciously extreme, likely small-sample luck."
        )
        roi_points = 0.0
    elif roi >= settings.PREFERRED_ROI_MIN and roi <= settings.PREFERRED_ROI_MAX:
        roi_points = 20.0
        reasons.append(
            f"ROI {roi:.1f}% is within the preferred "
            f"{settings.PREFERRED_ROI_MIN}-{settings.PREFERRED_ROI_MAX}% band -- earns {roi_points:.0f}/20 pts."
        )
    elif roi >= settings.MIN_ROI:
        roi_points = 16.0
        reasons.append(
            f"ROI {roi:.1f}% clears the {settings.MIN_ROI}% minimum "
            f"(outside the preferred band) -- earns {roi_points:.0f}/20 pts."
        )
    else:
        roi_points = 0.0
        reasons.append(f"ROI {roi:.1f}% is below the {settings.MIN_ROI}% minimum -- earns 0/20 pts.")
    score += roi_points

    # --- Win rate (20 pts) --------------------------------------------
    if win_rate is not None and win_rate >= 60:
        win_points = 20.0
        reasons.append(f"Strong win rate ({win_rate:.1f}%) -- earns {win_points:.0f}/20 pts.")
    elif win_rate is not None and win_rate >= 50:
        win_points = 13.0
        reasons.append(f"Solid win rate ({win_rate:.1f}%) -- earns {win_points:.0f}/20 pts.")
    elif (wins + losses) > 0:
        win_points = 5.0 if (win_rate is not None and win_rate > 0) else 0.0
        reasons.append(f"Weak win rate ({win_rate:.1f}%) -- earns {win_points:.0f}/20 pts.")
    else:
        win_points = 0.0
        reasons.append("No decided (win/loss) trades to evaluate win rate -- earns 0/20 pts.")
    score += win_points

    # --- Position count / sample size (15 pts) --------------------------
    if positions >= settings.MIN_POSITIONS:
        pos_points = 15.0
        reasons.append(
            f"{positions} positions >= minimum sample size of "
            f"{settings.MIN_POSITIONS} -- earns {pos_points:.0f}/15 pts."
        )
    else:
        pos_points = round(15.0 * positions / max(1, settings.MIN_POSITIONS), 1)
        reasons.append(
            f"Only {positions} positions, below the minimum sample size of "
            f"{settings.MIN_POSITIONS} -- historical picture may be unreliable, earns {pos_points:.1f}/15 pts."
        )
    score += pos_points

    # --- Consistency (10 pts) -- uncertainty, not automatic failure -------
    if has_dispersion_history and consistency is not None:
        consistency_points = round(min(10.0, consistency), 1)
        if consistency >= settings.MIN_CONSISTENCY:
            reasons.append(
                f"Consistency score {consistency:.1f}/10 meets the "
                f"{settings.MIN_CONSISTENCY}/10 bar -- earns {consistency_points:.1f}/10 pts."
            )
        else:
            reasons.append(
                f"Consistency score {consistency:.1f}/10 is below the "
                f"{settings.MIN_CONSISTENCY}/10 bar -- earns {consistency_points:.1f}/10 pts."
            )
    else:
        consistency_points = 5.0
        reasons.append(
            f"Only {dispersion_count} closed position(s) with PnL data -- too few to "
            "reliably score consistency. Treated as uncertainty, not a failure: earns 5.0/10 "
            "(neutral) pts rather than 0."
        )
    score += consistency_points

    # --- Drawdown (15 pts, inverse) ------------------------------------
    dd_val = drawdown if drawdown is not None else 0.0
    if dd_val <= settings.MAX_DRAWDOWN:
        dd_points = 15.0
        reasons.append(
            f"Max drawdown {dd_val:.1f}% is within the "
            f"{settings.MAX_DRAWDOWN}% limit -- earns {dd_points:.0f}/15 pts."
        )
    else:
        dd_points = max(0.0, 15.0 - (dd_val - settings.MAX_DRAWDOWN) / 4.0)
        reasons.append(
            f"Max drawdown {dd_val:.1f}% exceeds the {settings.MAX_DRAWDOWN}% "
            f"limit -- earns {dd_points:.1f}/15 pts."
        )
    score += dd_points

    # --- Concentration risk (10 pts, inverse) ---------------------------
    conc_val = concentration if concentration is not None else 0.0
    if conc_val < 50:
        conc_points = 10.0
        reasons.append(
            f"Profit is spread out (top trade is {conc_val:.1f}% of total profit) "
            f"-- earns {conc_points:.0f}/10 pts."
        )
    else:
        conc_points = max(0.0, 10.0 - (conc_val - 50) / 5.0)
        reasons.append(
            f"{conc_val:.1f}% of total profit came from a single trade -- "
            f"performance may not be repeatable, earns {conc_points:.1f}/10 pts."
        )
    score += conc_points

    # --- Category preference (5 pts) -- de-weighted, neutral by default ---
    total_cat = sum(category_mix.values()) or 1
    politics_share = category_mix.get("politics", 0) / total_cat
    sports_crypto_share = (
        category_mix.get("sports", 0) + category_mix.get("crypto", 0)
    ) / total_cat
    known_category_share = sum(
        v for k, v in category_mix.items() if k != "unknown"
    ) / total_cat
    if not settings.POLITICS_ALLOWED and politics_share > 0.5:
        cat_points = 0.0
        reasons.append(
            f"{politics_share*100:.0f}% of markets are politics, which is excluded by "
            f"configuration -- earns 0/5 pts."
        )
    elif known_category_share < 0.1:
        cat_points = 3.0
        reasons.append(
            "Market category data unavailable for this wallet -- neutral score applied "
            f"(3.0/5 pts) rather than treating the missing data as a penalty."
        )
    elif sports_crypto_share >= 0.5:
        cat_points = 5.0
        reasons.append(f"{sports_crypto_share*100:.0f}% of markets are sports/crypto (preferred) -- earns 5.0/5 pts.")
    else:
        cat_points = 2.5
        reasons.append(f"Only {sports_crypto_share*100:.0f}% of markets are in preferred sports/crypto categories -- earns 2.5/5 pts.")
    score += cat_points

    # --- Recency / ongoing activity (5 pts) --------------------------------
    recent_profitability = active_days >= settings.MIN_ACTIVE_DAYS and (roi is not None and roi > 0)
    if recent_profitability:
        recency_points = 5.0
        reasons.append(
            f"{active_days} days of tracked activity with positive overall ROI -- "
            f"earns {recency_points:.0f}/5 pts."
        )
    elif active_days > 0:
        recency_points = 2.5
        reasons.append(
            f"Only {active_days} days of tracked activity or non-positive ROI "
            f"(minimum {settings.MIN_ACTIVE_DAYS} days preferred) -- earns {recency_points:.1f}/5 pts."
        )
    else:
        recency_points = 0.0
        reasons.append("No dated activity to assess recency -- earns 0/5 pts.")
    score += recency_points

    # --- Resolution-only penalty ------------------------------------------
    if is_resolution_only:
        score -= 20
        reasons.append("Wallet shows no active SELL exits -- likely a resolution-only holder, penalized.")

    # --- Trading-frequency / rapid-trade / bot-like penalty ----------------
    bot_like = False
    if settings.RAPID_TRADE_ENABLED and freq is not None and not is_insufficient_freq:
        if freq_classification == "BOT_LIKE":
            bot_like = True
            score -= settings.BOT_LIKE_SCORE_PENALTY
            reasons.append(
                f"Frequency classification BOT_LIKE: {'; '.join(freq_reasons)} "
                f"(-{settings.BOT_LIKE_SCORE_PENALTY:.0f} pts, and this wallet cannot be RECOMMENDED)."
            )
        elif freq_classification == "RAPID_TRADER":
            score -= settings.FREQUENCY_SCORE_PENALTY
            reasons.append(
                f"Frequency classification RAPID_TRADER: {'; '.join(freq_reasons)} "
                f"(-{settings.FREQUENCY_SCORE_PENALTY:.0f} pts)."
            )
        elif freq_classification in ("HIGH_FREQUENCY", "ACTIVE"):
            score -= settings.FREQUENCY_SCORE_PENALTY / 2.0
            reasons.append(
                f"Frequency classification {freq_classification}: {'; '.join(freq_reasons)} "
                f"(-{settings.FREQUENCY_SCORE_PENALTY / 2.0:.1f} pts)."
            )
        else:
            reasons.append(f"Frequency classification {freq_classification}: no penalty applied.")
    elif freq is not None and is_insufficient_freq:
        reasons.append("Insufficient trade history to classify trading frequency.")

    score = max(0.0, min(100.0, score))

    risk_score = round(
        (dd_val * 0.5) + (conc_val * 0.3) + (politics_share * 100 * 0.2),
        1,
    )

    if bot_like:
        recommendation = "REJECT"
    elif score >= 75 and (roi is None or roi <= settings.REJECT_ROI_ABOVE):
        recommendation = "RECOMMENDED"
    elif score >= 55:
        recommendation = "WATCH"
    else:
        recommendation = "REJECT"

    return WalletScoreCard(
        wallet=wallet,
        roi_pct=round(roi, 2) if roi is not None else 0.0,
        win_rate_pct=round(win_rate, 2) if win_rate is not None else 0.0,
        drawdown_pct=round(dd_val, 2),
        num_positions=positions,
        avg_position_size=round(avg_size, 2),
        recent_profitability=recent_profitability,
        consistency_score=round(consistency, 2) if consistency is not None else 0.0,
        category_mix=category_mix,
        risk_score=risk_score,
        final_score=round(score, 1),
        recommendation=recommendation,
        frequency_classification=freq_classification if freq_classification is not None else "UNKNOWN",
        frequency_median_gap_seconds=freq_median_gap,
        frequency_pct_short=round(freq_pct_short, 2),
        reasons=reasons,
    )


def rank_wallets(
    stats_list: list[Union[WalletStatistics, WalletStats]],
    freq_map: Optional[dict[str, Union[FrequencyProfile, FrequencyStats]]] = None,
) -> list[WalletScoreCard]:
    freq_map = freq_map or {}
    cards = [score_wallet(s, freq_map.get(getattr(s, "wallet", ""))) for s in stats_list]
    cards.sort(key=lambda c: c.final_score, reverse=True)
    return cards
