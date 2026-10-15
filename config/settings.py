"""
Central configuration for the Polymarket Wallet Finder / Tracker.

Everything important is read from environment variables (via .env), never
hard-coded deep in the business logic.

Configuration for wallet discovery, analysis, filtering, ranking, frequency,
verification, tracking, and dashboard display. There are no trade-execution,
copy-trading, or bankroll settings.
"""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"

load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    try:
        return float(val) if val is not None else default
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    try:
        return int(val) if val is not None else default
    except ValueError:
        return default


class Settings:
    # ---- WALLET TRACKING / POLLING -------------------------------------
    CHECK_INTERVAL: int = _int("CHECK_INTERVAL", 5)
    REQUEST_TIMEOUT: int = _int("REQUEST_TIMEOUT", 15)
    MAX_RETRIES: int = _int("MAX_RETRIES", 5)
    BACKOFF_BASE: float = _float("BACKOFF_BASE", 0.5)

    # ---- WALLET FILTER THRESHOLDS --------------------------------------
    MIN_ROI: float = _float("MIN_ROI", 15.0)
    PREFERRED_ROI_MIN: float = _float("PREFERRED_ROI_MIN", 20.0)
    PREFERRED_ROI_MAX: float = _float("PREFERRED_ROI_MAX", 30.0)
    REJECT_ROI_ABOVE: float = _float("REJECT_ROI_ABOVE", 80.0)
    MIN_POSITIONS: int = _int("MIN_POSITIONS", 40)
    MIN_CONSISTENCY: float = _float("MIN_CONSISTENCY", 4.4)
    MAX_DRAWDOWN: float = _float("MAX_DRAWDOWN", 20.0)
    MIN_ACTIVE_DAYS: int = _int("MIN_ACTIVE_DAYS", 30)
    BREAKEVEN_TOLERANCE_USDC: float = _float("BREAKEVEN_TOLERANCE_USDC", 0.01)

    # ---- WALLET DISCOVERY -----------------------------------------------
    # How many candidate wallets the scanner will consider. The current
    # scanner (wallet/scanner.py) can only source ~20 from the public
    # leaderboard + seed list, so this is currently a soft cap on that --
    # but pulling it into config means a future scanner that can paginate
    # to a larger universe doesn't require touching this constant.
    MAX_WALLET_CANDIDATES: int = _int("MAX_WALLET_CANDIDATES", 30)

    # ---- HISTORICAL DATA COLLECTION (Phase 12 / 15) ----------------------
    # Maximum number of pages to retrieve for /activity and /closed-positions
    # when building PositionRecords for a wallet.  Each page is 100 fills or
    # 50 closed-positions, so MAX_HISTORY_PAGES=30 yields up to 3000 fills and
    # 1500 closed-positions.  Increasing this gives a more complete picture
    # at the cost of more API calls per wallet.  Must be >= 1.
    MAX_HISTORY_PAGES: int = _int("MAX_HISTORY_PAGES", 30)

    # ---- TRADING-FREQUENCY / RAPID-TRADE DETECTION -----------------------
    # Used to classify a wallet's trading cadence (LOW_FREQUENCY / NORMAL /
    # HIGH_FREQUENCY / RAPID_TRADER / BOT_LIKE) for scoring/filtering, and
    # to flag (diagnostic only -- never gates/blocks anything) when a
    # tracked wallet's CURRENT live activity looks like a burst. See
    # analytics/frequency.py for how these are combined -- none of these
    # numbers are load-bearing on their own, they're deliberately layered
    # (burst window checks catch a fast wallet even if its overall
    # trades/day is low; trades/day catches a wallet that's fast on average
    # even without a single dramatic burst).
    RAPID_TRADE_ENABLED: bool = _bool("RAPID_TRADE_ENABLED", True)
    # Minimum gap the live tracker uses between two consecutive fills from
    # the same wallet before flagging the second one as suspiciously fast
    # (separate from FREQ_* classification thresholds below, which are
    # about a wallet's historical pattern, not a single live gap).
    MIN_SECONDS_BETWEEN_TRADES: int = _int("MIN_SECONDS_BETWEEN_TRADES", 5)
    # Rolling-window burst check used both for historical classification
    # and live monitoring: if a wallet places >= MAX_TRADES_IN_WINDOW fills
    # within any RAPID_WINDOW_SECONDS window, that's a burst. Must be >= 2
    # -- a threshold of 1 would flag every wallet's very first trade.
    RAPID_WINDOW_SECONDS: int = _int("RAPID_WINDOW_SECONDS", 60)
    MAX_TRADES_IN_WINDOW: int = _int("MAX_TRADES_IN_WINDOW", 10)
    # % of a wallet's historical gaps that fall within RAPID_WINDOW_SECONDS;
    # above this, the wallet is at least RAPID_TRADER (see frequency.py).
    MAX_SHORT_INTERVAL_PERCENT: float = _float("MAX_SHORT_INTERVAL_PERCENT", 25.0)
    # Trades/day and median-gap thresholds for the HIGH_FREQUENCY tier.
    HIGH_FREQUENCY_TRADES_PER_DAY: float = _float("HIGH_FREQUENCY_TRADES_PER_DAY", 10.0)
    HIGH_FREQUENCY_MEDIAN_GAP_SECONDS: float = _float("HIGH_FREQUENCY_MEDIAN_GAP_SECONDS", 3600.0)
    # Below this trades/day, a wallet is LOW_FREQUENCY (occasional trader).
    LOW_FREQUENCY_MAX_TRADES_PER_DAY: float = _float("LOW_FREQUENCY_MAX_TRADES_PER_DAY", 0.34)
    # BOT_LIKE tier: extremely tight bursts.
    BOT_LIKE_MAX_TRADES_IN_1M: int = _int("BOT_LIKE_MAX_TRADES_IN_1M", 10)
    BOT_LIKE_PCT_WITHIN_10S: float = _float("BOT_LIKE_PCT_WITHIN_10S", 50.0)
    # Minimum trade count before we trust a frequency classification at all.
    MIN_TRADES_FOR_FREQUENCY_CLASSIFICATION: int = _int("MIN_TRADES_FOR_FREQUENCY_CLASSIFICATION", 3)
    # Score penalties applied in wallet/ranking.py.
    FREQUENCY_SCORE_PENALTY: float = _float("FREQUENCY_SCORE_PENALTY", 15.0)
    BOT_LIKE_SCORE_PENALTY: float = _float("BOT_LIKE_SCORE_PENALTY", 40.0)

    # ---- CATEGORY PREFERENCES -------------------------------------------
    POLITICS_ALLOWED: bool = _bool("POLITICS_ALLOWED", False)
    SPORTS_PREFERRED: bool = _bool("SPORTS_PREFERRED", True)
    CRYPTO_PREFERRED: bool = _bool("CRYPTO_PREFERRED", True)

    # ---- API ---------------------------------------------------------------
    POLYMARKET_DATA_API_BASE: str = os.getenv(
        "POLYMARKET_DATA_API_BASE", "https://data-api.polymarket.com"
    )
    POLYMARKET_GAMMA_API_BASE: str = os.getenv(
        "POLYMARKET_GAMMA_API_BASE", "https://gamma-api.polymarket.com"
    )
    POLYMARKET_CLOB_API_BASE: str = os.getenv(
        "POLYMARKET_CLOB_API_BASE", "https://clob.polymarket.com"
    )

    # ---- LOGGING -------------------------------------------------------------
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: str = os.getenv("LOG_FILE", str(DATA_DIR / "bot.log"))

    # ---- DATA FILES ------------------------------------------------------------
    WALLETS_FILE = DATA_DIR / "wallets.json"
    WALLET_STATS_FILE = DATA_DIR / "wallet_stats.json"
    VERIFICATION_FILE = DATA_DIR / "verification.json"
    SEEN_EVENTS_FILE = DATA_DIR / "seen_events.json"
    # Append-only log of new BUY/SELL activity observed on tracked wallets
    # (wallet/monitor.py::WalletMonitor). Replaces the old paper_trades.json
    # copy-trade log -- this records what a wallet did, never anything we
    # did about it.
    WALLET_ACTIVITY_LOG_FILE = DATA_DIR / "wallet_activity_log.json"


settings = Settings()
