"""
Fetches and normalizes a wallet's recent activity, and builds the
deterministic event IDs used for duplicate protection (spec section 28).
"""
from __future__ import annotations

import hashlib
from typing import Iterable

from polymarket.api_client import PolymarketApiClient, ApiError
from polymarket.parsers import normalize_activity_event


def event_id(event: dict) -> str:
    """
    Deterministic ID for one wallet activity event.

    Prefers the on-chain transaction hash. Falls back to a stable hash of
    (wallet, timestamp, token, side, price, size, market identifier) when a
    tx hash isn't available from the API, per spec section 28.
    """
    if event.get("tx_hash"):
        return f"tx:{event['tx_hash']}"

    market_id = event.get("condition_id") or event.get("slug") or event.get("market_title") or ""
    fingerprint = "|".join(
        str(x)
        for x in (
            event.get("wallet"),
            event.get("timestamp"),
            event.get("token_id"),
            event.get("side"),
            event.get("price"),
            event.get("size"),
            market_id,
        )
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
    return f"fp:{digest}"


def fetch_wallet_events(client: PolymarketApiClient, wallet: str, limit: int = 100) -> list[dict]:
    """
    Fetch and normalize a wallet's recent TRADE activity. Never raises past
    this point. The API is already asked to filter to type=TRADE
    server-side (see PolymarketApiClient.get_wallet_activity), but we filter
    defensively here too in case a response includes other activity types
    (SPLIT/MERGE/REDEEM/REWARD/CONVERSION/DEPOSIT/WITHDRAWAL/etc.) -- those
    are never trades and must never be copied as one.
    """
    try:
        raw_events = client.get_wallet_activity(wallet, limit=limit)
    except ApiError:
        return []

    events = []
    for raw in raw_events:
        try:
            ev = normalize_activity_event(raw)
        except Exception:
            continue
        if ev.get("activity_type") != "TRADE":
            continue
        if not ev.get("wallet"):
            ev["wallet"] = wallet.lower()
        ev["event_id"] = event_id(ev)
        events.append(ev)

    # Oldest first so processing order matches chronological order.
    events.sort(key=lambda e: e.get("timestamp") or "")
    return events


def fetch_wallet_events_paginated(
    client: PolymarketApiClient,
    wallet: str,
    max_pages: int = 5,
    page_size: int = 100,
    raise_on_error: bool = False,
) -> list[dict]:
    """
    Fetch and normalize a wallet's complete TRADE activity across up to
    `max_pages` pages of the /activity endpoint (Phase 12).

    Uses offset-based pagination (offset = page * page_size).  Stops early
    when a page returns fewer records than page_size (no more data) or when
    max_pages is reached (safety bound — prevents unbounded API loops).

    Returns events sorted oldest-first (ascending timestamp), deduplicated
    by event_id so that duplicate fills from overlapping page windows never
    produce duplicate PositionRecords downstream.

    `raise_on_error=False` (default) keeps the historical best-effort
    behaviour: a failed page ends pagination and the events collected so far
    are returned. Pass ``raise_on_error=True`` to surface the failure as an
    ``ApiError`` instead, so callers can distinguish "no activity" from "the
    request failed" (Phase B stale-data handling).
    """
    from config.settings import settings
    max_pages = max(1, max_pages)

    all_raw: list[dict] = []
    for page in range(max_pages):
        offset = page * page_size
        try:
            raw_page = client.get_wallet_activity(wallet, limit=page_size, offset=offset)
        except ApiError:
            if raise_on_error:
                raise
            break
        if not raw_page:
            break
        all_raw.extend(raw_page)
        if len(raw_page) < page_size:
            # Last page — no more records available.
            break

    seen_ids: set[str] = set()
    events: list[dict] = []
    for raw in all_raw:
        try:
            ev = normalize_activity_event(raw)
        except Exception:
            continue
        if ev.get("activity_type") != "TRADE":
            continue
        if not ev.get("wallet"):
            ev["wallet"] = wallet.lower()
        ev["event_id"] = event_id(ev)
        if ev["event_id"] not in seen_ids:
            seen_ids.add(ev["event_id"])
            events.append(ev)

    # Oldest first so processing order matches chronological order.
    events.sort(key=lambda e: e.get("timestamp") or "")
    return events


def dedupe_new_events(events: Iterable[dict], seen_ids: set[str]) -> list[dict]:
    """Return only events whose event_id has not been seen/processed before."""
    fresh = []
    for ev in events:
        if ev["event_id"] not in seen_ids:
            fresh.append(ev)
    return fresh

