"""
Central configuration for the Polymarket Wallet Finder / Tracker.

Everything important is read from environment variables (via .env), never
hard-coded deep in the business logic.

Configuration for wallet discovery, analysis, filtering, ranking, frequency,
verification, tracking, and dashboard display. There are no trade-execution,
copy-trading, or bankroll settings.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from dotenv import load_dotenv, dotenv_values

logger = logging.getLogger("config.settings")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
ENV_FILE = BASE_DIR / ".env"

# `override=False` (the python-dotenv default) means a value already present in
# the process environment WINS over .env. That is deliberate -- an explicit
# shell/CI variable should beat a local file -- but it is also the single most
# confusing way to "change a setting and see nothing happen", so every
# conflict and every unparseable value is recorded in CONFIG_ISSUES below and
# surfaced in the scan diagnostics.
load_dotenv(ENV_FILE)


def _env_file_values() -> dict:
    """Values declared in .env (empty dict when there is no readable .env)."""
    try:
        values = dotenv_values(ENV_FILE) or {}
    except Exception:  # unreadable/malformed .env must never crash startup
        return {}
    return {key: value for key, value in values.items() if value is not None}


_ENV_FILE_VALUES: dict = _env_file_values()

#: Human-readable notes about configuration that did not apply as written.
#: Read by monitor/scan_report.py so the user sees them without hunting logs.
CONFIG_ISSUES: list[str] = []


def setting_source(name: str) -> str:
    """
    Where the EFFECTIVE value of `name` came from: ".env", "process
    environment" or "default".

    This is what makes "I changed .env but nothing changed" answerable at a
    glance instead of guessable.
    """
    effective = os.environ.get(name)
    if effective is None:
        return "default"
    from_file = _ENV_FILE_VALUES.get(name)
    if from_file is not None and str(from_file).strip() == str(effective).strip():
        return ".env"
    return "process environment"


def _clean_env_value(raw: str) -> str:
    """
    Tolerate the ways people actually write values in a .env file:
    surrounding whitespace, 'quotes', and a trailing inline comment.
    """
    value = str(raw).strip()
    if "#" in value:
        value = value.split("#", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def _record_issue(name: str, raw: str, default, kind: str, source: str) -> None:
    message = (
        f"{name}={raw[:40]!r} (from {source}) is not a valid {kind}; "
        f"falling back to the default {default!r}."
    )
    if message not in CONFIG_ISSUES:
        CONFIG_ISSUES.append(message)
    logger.warning("CONFIG_VALUE_INVALID %s", message)


def _check_env_shadowing(name: str) -> None:
    """Record when a process-environment value is overriding .env."""
    from_file = _ENV_FILE_VALUES.get(name)
    if from_file is None:
        return
    effective = os.environ.get(name)
    if effective is not None and str(from_file).strip() != str(effective).strip():
        message = (
            f"{name}: .env says {from_file.strip()!r} but the process environment sets "
            f"{effective.strip()!r}, which wins (edit the environment or unset it to use .env)."
        )
        if message not in CONFIG_ISSUES:
            CONFIG_ISSUES.append(message)
        logger.warning("CONFIG_ENV_OVERRIDES_ENV_FILE %s", message)


def _raw(name: str, default, kind: str):
    """Raw string value for `name`, or None when it is unset. Records shadowing."""
    _check_env_shadowing(name)
    raw = os.getenv(name)
    if raw is None:
        return None
    return raw


def parse_bool(name: str, default: bool) -> bool:
    raw = _raw(name, default, "boolean")
    if raw is None:
        return default
    value = _clean_env_value(raw).lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off", ""):
        return False
    _record_issue(name, raw, default, "boolean (use true/false)", setting_source(name))
    return default


def parse_float(name: str, default: float) -> float:
    raw = _raw(name, default, "number")
    if raw is None:
        return default
    value = _clean_env_value(raw)
    try:
        return float(value)
    except ValueError:
        _record_issue(name, raw, default, "number", setting_source(name))
        return default


def parse_int(name: str, default: int) -> int:
    raw = _raw(name, default, "integer")
    if raw is None:
        return default
    value = _clean_env_value(raw)
    try:
        return int(value)
    except ValueError:
        # Tolerate a plain decimal that happens to be a whole number ("100.0"),
        # but not scientific/exponent forms ("1e2") -- those are far more
        # likely to be a misunderstanding than an intentional 100.
        if "." in value:
            try:
                as_float = float(value)
            except ValueError:
                as_float = None
            if as_float is not None and as_float.is_integer():
                return int(as_float)
        _record_issue(name, raw, default, "integer", setting_source(name))
        return default


# Backwards-compatible private aliases (the Settings class below uses these).
def _bool(name: str, default: bool) -> bool:
    return parse_bool(name, default)


def _float(name: str, default: float) -> float:
    return parse_float(name, default)


def _int(name: str, default: int) -> int:
    return parse_int(name, default)


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
    # Hard cap on the number of distinct candidate wallets a single scan
    # will consider. The cap applies to the UNION of leaderboard-sourced
    # candidates and the seed list (see wallet/scanner.py::scan_top_wallets).
    MAX_WALLET_CANDIDATES: int = _int("MAX_WALLET_CANDIDATES", 30)
    # Leaderboard pagination (Phase B). The scanner walks the public
    # leaderboard with limit/offset pages, requesting LEADERBOARD_PAGE_SIZE
    # rows per page for at most MAX_LEADERBOARD_PAGES requests. 50 is the
    # server-side maximum page size (Polymarket caps `limit` at 50), so any
    # larger configured value is clamped down to 50 by the scanner.
    # Pagination always stops early on an empty page, a short page, a
    # repeated page, an API error, or once MAX_WALLET_CANDIDATES is filled.
    LEADERBOARD_PAGE_SIZE: int = _int("LEADERBOARD_PAGE_SIZE", 50)
    MAX_LEADERBOARD_PAGES: int = _int("MAX_LEADERBOARD_PAGES", 10)
    # Hard ceiling enforced by the scanner regardless of configuration
    # (mirrors Polymarket's documented per-request cap).
    LEADERBOARD_MAX_PAGE_SIZE: int = 50

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
