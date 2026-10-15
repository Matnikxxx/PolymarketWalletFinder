"""
Wallet activity tracker.

This module observes and records public activity from wallets the user has
chosen to track. It is read-only: it does not place orders, simulate trades,
or change any wallet's positions.

Responsibilities kept from the original spec:
  - poll tracked wallets for activity
  - establish a first-run baseline so pre-existing history is never
    reported as "new" (spec item 29)
  - de-duplicate activity by event_id (spec item 28)
  - detect and record new BUY and SELL activity, with timestamp, market
    identifiers/title, price, size, side, and transaction/event id
  - record rapid-trading diagnostics (spec item 12) as an informational
    flag on the observed activity -- this NEVER rejects, delays, resizes,
    or otherwise gates anything; it is purely "this wallet's current
    cadence looks like a burst", for reporting/dashboard consumption
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from config.settings import settings
from analytics.frequency import RollingWindowTracker
from polymarket.api_client import PolymarketApiClient
from polymarket.market_client import get_market_info
from polymarket.wallet_activity import dedupe_new_events, fetch_wallet_events
from storage import load_json, save_json_atomic

logger = logging.getLogger("wallet.monitor")


def _parse_event_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_wallet_config(path: Optional[Path] = None) -> list[dict]:
    """
    `path` defaults to `settings.WALLETS_FILE`, resolved at CALL time (not
    import time) -- a plain `path: Path = settings.WALLETS_FILE` default
    would bake in whatever WALLETS_FILE was when this module was first
    imported, so changing settings.WALLETS_FILE later (e.g. in tests via
    monkeypatch) would silently have no effect.
    """
    data = load_json(path if path is not None else settings.WALLETS_FILE, default=[])
    if not isinstance(data, list):
        return []
    return [w for w in data if isinstance(w, dict)]


def save_wallet_config(wallets: list[dict], path: Optional[Path] = None) -> None:
    save_json_atomic(path if path is not None else settings.WALLETS_FILE, wallets)


def is_wallet_tracked(wallet: dict) -> bool:
    """Return whether a wallet is tracked.

    New configuration uses ``tracked``. For older wallets.json files, fall
    back to ``enabled`` only when ``tracked`` is absent. This preserves the
    user's previous selection during the terminology migration.
    """
    if not isinstance(wallet, dict):
        return False
    if "tracked" in wallet:
        return bool(wallet.get("tracked"))
    return bool(wallet.get("enabled", False))


def tracked_wallets(wallets: list[dict]) -> list[str]:
    """Return addresses selected for tracking, normalized to lowercase."""
    if not isinstance(wallets, list):
        return []
    return [
        wallet["wallet"].lower()
        for wallet in wallets
        if isinstance(wallet, dict) and wallet.get("wallet") and is_wallet_tracked(wallet)
    ]


# Backwards-compatible function alias for external scripts/tests.
def enabled_wallets(wallets: list[dict]) -> list[str]:
    """Deprecated alias; use tracked_wallets()."""
    return tracked_wallets(wallets)


@dataclass
class TrackedActivity:
    """
    One newly-observed activity event for a tracked wallet. This describes
    ONLY what the wallet did -- there is no outcome/decision/action field,
    because tracking never acts on the activity it observes.
    """
    event_id: str
    wallet: str
    side: str  # "BUY" | "SELL"
    timestamp: Optional[str]
    condition_id: Optional[str]
    token_id: Optional[str]
    slug: Optional[str]
    market_title: Optional[str]
    outcome: Optional[str]
    price: float
    size: float
    usdc_size: float
    tx_hash: Optional[str] = None
    category: str = "unknown"
    liquidity: float = 0.0
    wallet_score: Optional[float] = None
    is_rapid: bool = False
    rapid_reason: Optional[str] = None
    detected_at: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "wallet": self.wallet,
            "side": self.side,
            "timestamp": self.timestamp,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "slug": self.slug,
            "market_title": self.market_title,
            "outcome": self.outcome,
            "price": self.price,
            "size": self.size,
            "usdc_size": self.usdc_size,
            "tx_hash": self.tx_hash,
            "category": self.category,
            "liquidity": self.liquidity,
            "wallet_score": self.wallet_score,
            "is_rapid": self.is_rapid,
            "rapid_reason": self.rapid_reason,
            "detected_at": self.detected_at,
        }


class WalletMonitor:
    """Polls tracked wallets and records their new activity. Read-only:
    never creates a position, never places or simulates an order."""

    def __init__(
        self,
        client: Optional[PolymarketApiClient] = None,
        seen_events_file: Optional[Path] = None,
        activity_log_file: Optional[Path] = None,
    ):
        self.client = client or PolymarketApiClient()
        self.seen_events_file = seen_events_file if seen_events_file is not None else settings.SEEN_EVENTS_FILE
        self.activity_log_file = (
            activity_log_file if activity_log_file is not None else settings.WALLET_ACTIVITY_LOG_FILE
        )
        state = load_json(self.seen_events_file, default={"baseline_done": {}, "seen_ids": []})
        self.baseline_done: dict[str, bool] = state.get("baseline_done", {})
        self.seen_ids: set[str] = set(state.get("seen_ids", []))
        # Live burst tracker (spec item 12). In-memory only, per run -- see
        # analytics.frequency.RollingWindowTracker for why that's safe.
        self.rapid_tracker = RollingWindowTracker()

    def _save_seen_state(self) -> None:
        save_json_atomic(
            self.seen_events_file,
            {"baseline_done": self.baseline_done, "seen_ids": list(self.seen_ids)},
        )

    def _append_activity_log(self, tracked: list[TrackedActivity]) -> None:
        if not tracked:
            return
        existing = load_json(self.activity_log_file, default=[])
        existing.extend(t.as_dict() for t in tracked)
        save_json_atomic(self.activity_log_file, existing)

    # ------------------------------------------------------------------
    def establish_baseline(self, wallet: str) -> None:
        """First-run baseline (spec 29): mark all currently-existing activity as
        already seen WITHOUT reporting it as new."""
        if self.baseline_done.get(wallet):
            return
        events = fetch_wallet_events(self.client, wallet)
        for ev in events:
            self.seen_ids.add(ev["event_id"])
        self.baseline_done[wallet] = True
        self._save_seen_state()
        logger.info("BASELINE_ESTABLISHED wallet=%s events=%d", wallet, len(events))

    # ------------------------------------------------------------------
    def poll_wallet(self, wallet: str, wallet_score: Optional[float] = None) -> list[TrackedActivity]:
        """
        Fetches a tracked wallet's current activity, de-dupes against
        already-seen events, and returns a TrackedActivity for every new
        BUY/SELL fill. Every new fill is recorded regardless of side or
        cadence -- SELLs and BUYs are both always observable (nothing here
        ever "rejects" or "skips" an event; the only diagnostic applied is
        the rapid-trade flag below, which is informational only).
        """
        events = fetch_wallet_events(self.client, wallet)
        new_events = dedupe_new_events(events, self.seen_ids)
        tracked: list[TrackedActivity] = []

        for ev in new_events:
            logger.info(
                "NEW_ACTIVITY_DETECTED wallet=%s side=%s market=%s price=%s size=%s",
                wallet, ev["side"], ev.get("market_title"), ev.get("price"), ev.get("size"),
            )

            # --- Rapid-trade / bot-like live diagnostic (spec item 12) ------
            # Informational only: this flags a wallet's CURRENT cadence as
            # a burst so it can be surfaced in reports/dashboard. It never
            # rejects, delays, or resizes anything -- there is nothing to
            # gate in a tracker that doesn't act on activity. Only BUYs are
            # fed into the rolling-window tracker, matching the historical
            # classification's treatment of overall trade cadence; SELLs
            # are always tracked plainly, same as BUYs, just without this
            # extra diagnostic flag.
            is_rapid = False
            rapid_reason: Optional[str] = None
            if settings.RAPID_TRADE_ENABLED and ev["side"] == "BUY":
                ts = _parse_event_ts(ev.get("timestamp"))
                if ts is not None:
                    is_rapid, rapid_reason, trades_in_window, _seconds_since_last = (
                        self.rapid_tracker.record_and_check(wallet, ts)
                    )
                    if is_rapid:
                        logger.info(
                            "RAPID_TRADE_DETECTED wallet=%s reason=%s trades_in_window=%d",
                            wallet, rapid_reason, trades_in_window,
                        )

            market_info = get_market_info(self.client, ev.get("condition_id"), ev.get("slug"))

            activity = TrackedActivity(
                event_id=ev["event_id"],
                wallet=wallet,
                side=ev["side"],
                timestamp=ev.get("timestamp"),
                condition_id=ev.get("condition_id"),
                token_id=ev.get("token_id"),
                slug=ev.get("slug"),
                market_title=ev.get("market_title"),
                outcome=ev.get("outcome"),
                price=ev.get("price", 0.0),
                size=ev.get("size", 0.0),
                usdc_size=ev.get("usdc_size", 0.0),
                tx_hash=ev.get("tx_hash"),
                category=market_info.get("category", "unknown"),
                liquidity=market_info.get("liquidity", 0.0),
                wallet_score=wallet_score,
                is_rapid=is_rapid,
                rapid_reason=rapid_reason,
                detected_at=datetime.now().astimezone().isoformat(),
            )
            tracked.append(activity)
            self.seen_ids.add(ev["event_id"])

        if new_events:
            self._append_activity_log(tracked)
            self._save_seen_state()

        return tracked

    # ------------------------------------------------------------------
    def run_once(self, wallet_scores: Optional[dict[str, float]] = None) -> list[TrackedActivity]:
        wallet_scores = wallet_scores or {}
        wallets_cfg = load_wallet_config()
        active = tracked_wallets(wallets_cfg)
        all_tracked: list[TrackedActivity] = []
        for wallet in active:
            self.establish_baseline(wallet)
            score = wallet_scores.get(wallet)
            all_tracked.extend(self.poll_wallet(wallet, score))
        return all_tracked

    def run_forever(self, wallet_scores: Optional[dict[str, float]] = None, iterations: Optional[int] = None):
        """
        Blocking loop. `iterations` is only used by tests to bound the loop;
        production use (main.py) leaves it as None to run indefinitely.
        """
        count = 0
        while iterations is None or count < iterations:
            try:
                self.run_once(wallet_scores)
            except Exception:
                logger.exception("MONITOR_LOOP_ERROR")
            count += 1
            if iterations is None or count < iterations:
                time.sleep(settings.CHECK_INTERVAL)
