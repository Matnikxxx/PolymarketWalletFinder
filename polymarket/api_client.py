"""
A resilient HTTP client wrapping Polymarket's public read-only APIs.

Design notes
------------
* Only public, unauthenticated, read-only endpoints are used. No private key,
  API key, or signing is required anywhere in this module.
* This client NEVER submits orders. There is no method here capable of
  placing a trade. Order submission is intentionally absent from this build.
* Retries with exponential backoff + jitter, per spec.
* A small in-memory TTL cache avoids hammering the API when the monitor
  loop polls every few seconds.
* Every request failure is caught and logged; a bad response from one
  endpoint must never crash the whole bot.

Known endpoints (public, documented by Polymarket, verified to exist as of
this build). If Polymarket changes these, `logs/` will show repeated
API_ERROR entries and the relevant method should be revisited -- this client
deliberately does not "guess" new fields when a response shape changes.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

import requests

from config.settings import settings

logger = logging.getLogger("polymarket.api_client")


class ApiError(Exception):
    """Raised after retries are exhausted for a request."""


class _TTLCache:
    def __init__(self, ttl_seconds: float = 5.0):
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str):
        entry = self._store.get(key)
        if not entry:
            return None
        expires_at, value = entry
        if time.time() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any):
        self._store[key] = (time.time() + self.ttl, value)


def _extract_list(data: Any) -> list[dict]:
    """Safely extracts a list of dicts from API responses (both bare list and {"data": [...]})."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        items = data.get("data")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


class PolymarketApiClient:
    """Thin, defensive wrapper around Polymarket's public HTTP APIs."""

    def __init__(
        self,
        data_api_base: Optional[str] = None,
        gamma_api_base: Optional[str] = None,
        clob_api_base: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        backoff_base: Optional[float] = None,
        cache_ttl: float = 5.0,
        session: Optional[requests.Session] = None,
    ):
        self.data_api_base = data_api_base or settings.POLYMARKET_DATA_API_BASE
        self.gamma_api_base = gamma_api_base or settings.POLYMARKET_GAMMA_API_BASE
        self.clob_api_base = clob_api_base or settings.POLYMARKET_CLOB_API_BASE
        self.timeout = timeout or settings.REQUEST_TIMEOUT
        self.max_retries = max_retries if max_retries is not None else settings.MAX_RETRIES
        self.backoff_base = backoff_base or settings.BACKOFF_BASE
        self.session = session or requests.Session()
        self.cache = _TTLCache(ttl_seconds=cache_ttl)

    # ------------------------------------------------------------------
    # Low-level request with retries / backoff / rate-limit handling
    # ------------------------------------------------------------------
    def _get(self, url: str, params: Optional[dict] = None, use_cache: bool = True) -> Any:
        cache_key = f"{url}|{sorted((params or {}).items())}"
        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", 0) or 0)
                    sleep_for = retry_after or self._backoff_delay(attempt)
                    logger.warning(
                        "RATE_LIMIT url=%s attempt=%s sleeping=%.2fs", url, attempt, sleep_for
                    )
                    time.sleep(sleep_for)
                    continue
                if resp.status_code >= 500:
                    logger.warning(
                        "API_ERROR url=%s status=%s attempt=%s (server error, retrying)",
                        url, resp.status_code, attempt,
                    )
                    time.sleep(self._backoff_delay(attempt))
                    continue
                if resp.status_code >= 400:
                    # Client error: retrying will not help.
                    raise ApiError(f"{resp.status_code} client error for {url}: {resp.text[:300]}")

                try:
                    data = resp.json()
                except ValueError as exc:
                    raise ApiError(f"Invalid JSON from {url}: {exc}") from exc

                if use_cache:
                    self.cache.set(cache_key, data)
                return data

            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                delay = self._backoff_delay(attempt)
                logger.warning(
                    "API_ERROR url=%s attempt=%s/%s error=%s retry_in=%.2fs",
                    url, attempt, self.max_retries, exc, delay,
                )
                time.sleep(delay)
                continue
            except ApiError:
                raise
            except requests.RequestException as exc:
                last_exc = exc
                delay = self._backoff_delay(attempt)
                logger.warning(
                    "API_ERROR url=%s attempt=%s/%s error=%s retry_in=%.2fs",
                    url, attempt, self.max_retries, exc, delay,
                )
                time.sleep(delay)
                continue

        logger.error("API_ERROR url=%s exhausted retries last_error=%s", url, last_exc)
        raise ApiError(f"Exhausted retries for {url}: {last_exc}")

    def _backoff_delay(self, attempt: int) -> float:
        base = self.backoff_base * (2 ** (attempt - 1))
        jitter = random.uniform(0, self.backoff_base)
        return min(base + jitter, 30.0)

    # ------------------------------------------------------------------
    # Public read-only endpoints
    # ------------------------------------------------------------------
    def get_wallet_activity(self, wallet: str, limit: int = 100, offset: int = 0) -> list[dict]:
        """
        Recent on-chain TRADE activity for a wallet.

        The /activity endpoint also returns non-trade rows (SPLIT, MERGE,
        REDEEM, REWARD, CONVERSION, DEPOSIT, WITHDRAWAL, YIELD,
        MAKER_REBATE, TAKER_REBATE, REFERRAL_REWARD) which are not buys/
        sells and must never be treated as one. We filter server-side with
        type=TRADE (and still filter defensively downstream in
        polymarket.wallet_activity, in case the API ever omits `type`).
        """
        url = f"{self.data_api_base}/activity"
        params = {"user": wallet, "limit": limit, "offset": offset, "type": "TRADE"}
        data = self._get(url, params=params, use_cache=True)
        return _extract_list(data)

    def get_wallet_positions(self, wallet: str) -> list[dict]:
        """Current open positions for a wallet."""
        url = f"{self.data_api_base}/positions"
        params = {"user": wallet}
        data = self._get(url, params=params, use_cache=True)
        return _extract_list(data)

    def get_wallet_value(self, wallet: str) -> Optional[float]:
        """
        Total portfolio value for a wallet, from GET /value.

        Real response shape is an ARRAY of one object:
        [{"user": "0x...", "value": 123.45}] -- NOT a bare dict. (Verified
        against Polymarket's published OpenAPI spec.) Returns None if the
        endpoint fails or the response doesn't match that shape, rather
        than a fabricated 0.
        """
        url = f"{self.data_api_base}/value"
        params = {"user": wallet}
        data = self._get(url, params=params, use_cache=True)
        if isinstance(data, list) and data:
            item = data[0]
            if isinstance(item, dict):
                try:
                    return float(item.get("value"))
                except (TypeError, ValueError):
                    return None
            return None
        if isinstance(data, dict) and "value" in data:
            try:
                return float(data["value"])
            except (TypeError, ValueError):
                return None
        return None

    def get_wallet_closed_positions(
        self, wallet: str, limit: int = 50, offset: int = 0,
        sort_by: str = "TIMESTAMP", sort_direction: str = "DESC",
    ) -> list[dict]:
        """
        Authoritative closed-position history for a wallet, from
        GET /closed-positions. Each item includes Polymarket's own
        `realizedPnl` and `avgPrice`/`totalBought` -- this is more
        trustworthy for win/loss/realized-P&L than reconstructing from raw
        /activity fills, since it reflects Polymarket's own accounting
        (partial fills, resolutions, etc. included).

        `limit` is capped at 50 by the API (confirmed via its OpenAPI spec);
        callers needing more history should page with `offset`.
        Default sort is now DESC (newest first) so that page 1 of closed
        positions aligns with the most-recent activity events, enabling
        accurate TRADING_EXIT detection in build_position_records().
        """
        url = f"{self.data_api_base}/closed-positions"
        params = {
            "user": wallet, "limit": min(limit, 50), "offset": offset,
            "sortBy": sort_by, "sortDirection": sort_direction,
        }
        data = self._get(url, params=params, use_cache=True)
        return _extract_list(data)

    def get_wallet_closed_positions_paginated(
        self,
        wallet: str,
        max_pages: int = 5,
        page_size: int = 50,
        sort_by: str = "TIMESTAMP",
        sort_direction: str = "DESC",
        raise_on_error: bool = False,
    ) -> list[dict]:
        """
        Retrieve the wallet's full closed-position history across up to
        `max_pages` pages (Phase 12).

        `raise_on_error=False` (default) keeps the historical best-effort
        behaviour: a failing page ends pagination and whatever was already
        collected is returned. Pass ``raise_on_error=True`` to surface the
        failure as an ``ApiError`` instead, so callers can distinguish "the
        history is empty" from "the request failed" -- Phase B's stale-data
        handling needs that distinction and must never treat a failed
        request as fresh (empty) data.

        Uses offset-based pagination.  Stops early when a page returns fewer
        records than page_size (no more data) or when max_pages is reached
        (safety bound).  De-duplicates by (conditionId, asset) pair to guard
        against any API-side overlap between pages.

        Default sort_direction=DESC keeps the newest positions first, matching
        the /activity endpoint's implicit newest-first ordering so that the
        sell-event detection in build_position_records() can correctly
        attribute TRADING_EXIT settlement types.
        """
        max_pages = max(1, max_pages)
        all_records: list[dict] = []
        seen: set[tuple] = set()

        for page in range(max_pages):
            offset = page * page_size
            try:
                page_data = self.get_wallet_closed_positions(
                    wallet, limit=page_size, offset=offset,
                    sort_by=sort_by, sort_direction=sort_direction,
                )
            except ApiError:
                if raise_on_error:
                    raise
                break
            except Exception as exc:
                if raise_on_error:
                    raise ApiError(
                        f"closed-positions pagination failed for wallet={wallet} offset={offset}: {exc}"
                    ) from exc
                break
            if not page_data:
                break
            for r in page_data:
                key = (r.get("conditionId"), r.get("asset"))
                if key not in seen:
                    seen.add(key)
                    all_records.append(r)
            if len(page_data) < page_size:
                # Fewer records than requested — we have reached the end.
                break

        return all_records

    def get_leaderboard(
        self, category: str = "OVERALL", time_period: str = "MONTH",
        order_by: str = "PNL", limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        """
        Top-trader leaderboard used for wallet discovery/scanning.

        Real endpoint is GET /v1/leaderboard (data-api), not /leaderboard.
        category in {OVERALL, POLITICS, SPORTS, ESPORTS, CRYPTO, CULTURE,
        MENTIONS, WEATHER, ECONOMICS, TECH, FINANCE}; timePeriod in
        {DAY, WEEK, MONTH, ALL}; limit is capped at 50 by the API.
        """
        url = f"{self.data_api_base}/v1/leaderboard"
        params = {
            "category": category,
            "timePeriod": time_period,
            "orderBy": order_by,
            "limit": min(limit, 50),
            "offset": offset,
        }
        data = self._get(url, params=params, use_cache=True)
        return _extract_list(data)

    def get_market(self, condition_id: Optional[str] = None, slug: Optional[str] = None) -> dict:
        """Market metadata (category, liquidity, title) by condition_id or slug."""
        url = f"{self.gamma_api_base}/markets"
        params = {}
        if condition_id:
            params["condition_ids"] = condition_id
        if slug:
            params["slug"] = slug
        data = self._get(url, params=params, use_cache=True)
        items = _extract_list(data)
        return items[0] if items else {}

    def get_orderbook(self, token_id: str) -> dict:
        """CLOB orderbook snapshot for liquidity estimation."""
        url = f"{self.clob_api_base}/book"
        params = {"token_id": token_id}
        data = self._get(url, params=params, use_cache=True)
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # NOTE: Intentionally no order-placement / signing methods exist here.
    # ------------------------------------------------------------------
