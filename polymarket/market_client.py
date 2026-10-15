"""
Helpers for resolving market metadata (category, liquidity) used by filters.
Always resolves by condition_id/slug first; title is fallback-only display
info per spec section 10.
"""
from __future__ import annotations

from polymarket.api_client import PolymarketApiClient, ApiError
from polymarket.parsers import normalize_market


def get_market_info(client: PolymarketApiClient, condition_id: str | None, slug: str | None) -> dict:
    """
    Returns normalized market info. On API failure, returns a safe unknown
    record rather than raising -- callers must decide how to treat unknown
    liquidity/category (spec section 13: never silently ignore).
    """
    try:
        raw = client.get_market(condition_id=condition_id, slug=slug)
        if raw:
            return normalize_market(raw)
    except ApiError:
        pass

    return {
        "condition_id": condition_id,
        "slug": slug,
        "title": None,
        "category": "unknown",
        "liquidity": 0.0,
        "volume": 0.0,
        "closed": False,
    }
