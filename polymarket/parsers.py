"""
Normalizes raw Polymarket API responses into a stable internal shape so the
rest of the codebase never has to guess about field names.

If the upstream API renames/removes a field, THIS is the file to fix -
nothing downstream should touch raw API dicts directly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def _to_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _to_iso(ts: Any) -> Optional[str]:
    """Accepts unix seconds, unix ms, or an ISO string; returns ISO UTC string."""
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            # Already ISO-ish
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return ts
        except ValueError:
            pass
    try:
        num = float(ts)
        # Heuristic: ms vs s
        if num > 1e12:
            num /= 1000.0
        return datetime.fromtimestamp(num, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def normalize_activity_event(raw: dict) -> dict:
    """
    Normalize one entry from GET /activity into a stable schema:

    {
        wallet, side, condition_id, token_id, slug, market_title, outcome,
        price, size, usdc_size, timestamp, tx_hash, activity_type
    }

    Unknown/missing fields become None rather than raising, so a partial or
    evolving API response never crashes the monitor loop.

    NOTE: /activity returns non-trade row types too (SPLIT, MERGE, REDEEM,
    REWARD, CONVERSION, DEPOSIT, WITHDRAWAL, YIELD, *_REBATE,
    REFERRAL_REWARD). This function normalizes whatever row it's given;
    callers that only want trades must filter on `activity_type == "TRADE"`
    (see polymarket.wallet_activity.fetch_wallet_events, which does this).
    """
    activity_type = (raw.get("type") or "TRADE").upper()

    side = (raw.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        # Some API variants use "TRADE" plus a signed size; fall back safely.
        size_signed = _to_float(raw.get("size"), 0.0)
        side = "BUY" if size_signed >= 0 else "SELL"

    price = _to_float(raw.get("price"))
    size = abs(_to_float(raw.get("size")))
    usdc_size = _to_float(raw.get("usdcSize") or raw.get("usdc_size") or (price * size))

    return {
        "wallet": (raw.get("proxyWallet") or raw.get("user") or raw.get("wallet") or "").lower(),
        "side": side,
        "activity_type": activity_type,
        "condition_id": raw.get("conditionId") or raw.get("condition_id"),
        "token_id": raw.get("asset") or raw.get("tokenId") or raw.get("token_id"),
        "slug": raw.get("slug") or raw.get("eventSlug"),
        "market_title": raw.get("title") or raw.get("question") or raw.get("market") or raw.get("eventTitle"),
        "outcome": raw.get("outcome"),
        "price": price,
        "size": size,
        "usdc_size": usdc_size,
        "timestamp": _to_iso(raw.get("timestamp") or raw.get("time") or raw.get("createdAt")),
        "tx_hash": raw.get("transactionHash") or raw.get("txHash") or raw.get("tx_hash"),
        "raw": raw,
    }


def normalize_position(raw: dict) -> dict:
    return {
        "condition_id": raw.get("conditionId") or raw.get("condition_id"),
        "token_id": raw.get("asset") or raw.get("tokenId") or raw.get("token_id"),
        "slug": raw.get("slug"),
        "outcome": raw.get("outcome"),
        "size": _to_float(raw.get("size")),
        "avg_price": _to_float(raw.get("avgPrice") or raw.get("avg_price")),
        "current_value": _to_float(raw.get("currentValue") or raw.get("current_value")),
        "cash_pnl": _to_float(raw.get("cashPnl") or raw.get("cash_pnl")),
        "percent_pnl": _to_float(raw.get("percentPnl") or raw.get("percent_pnl")),
    }


def normalize_market(raw: dict) -> dict:
    category = (raw.get("category") or raw.get("category_slug") or "").strip()
    if not category:
        # fallback heuristic only used when the API genuinely has no category field
        title = (raw.get("title") or raw.get("question") or "").lower()
        if any(k in title for k in ("election", "president", "senate", "congress", "governor")):
            category = "politics"
        elif any(k in title for k in ("btc", "eth", "crypto", "bitcoin", "ethereum", "solana")):
            category = "crypto"
        elif any(k in title for k in ("nfl", "nba", "mlb", "nhl", "soccer", "match", "vs.", "vs ")):
            category = "sports"
        else:
            category = "other"

    return {
        "condition_id": raw.get("conditionId") or raw.get("condition_id"),
        "slug": raw.get("slug"),
        "title": raw.get("title") or raw.get("question"),
        "category": category.lower(),
        "liquidity": _to_float(raw.get("liquidity") or raw.get("liquidityNum")),
        "volume": _to_float(raw.get("volume") or raw.get("volumeNum")),
        "closed": bool(raw.get("closed", False)),
    }


def normalize_leaderboard_entry(raw: dict) -> dict:
    return {
        "wallet": (raw.get("proxyWallet") or raw.get("wallet") or raw.get("user") or "").lower(),
        "pnl": _to_float(raw.get("pnl") or raw.get("profit")),
        "volume": _to_float(raw.get("vol") or raw.get("volume")),
        "rank": raw.get("rank"),
        "user_name": raw.get("userName"),
    }


def normalize_closed_position(raw: dict) -> dict:
    """
    Normalizes one entry from GET /closed-positions into a stable schema.
    Confirmed against Polymarket's published OpenAPI ClosedPosition schema:
    proxyWallet, asset, conditionId, avgPrice, totalBought, realizedPnl,
    curPrice, timestamp, title, slug, outcome.

    `realized_pnl` here is Polymarket's own authoritative figure -- NOT
    reconstructed from raw fills.
    """
    avg_price = _to_float(raw.get("avgPrice"))
    total_bought = _to_float(raw.get("totalBought"))
    return {
        "wallet": (raw.get("proxyWallet") or "").lower(),
        "condition_id": raw.get("conditionId"),
        "token_id": raw.get("asset"),
        "slug": raw.get("slug"),
        "market_title": raw.get("title"),
        "outcome": raw.get("outcome"),
        "avg_price": avg_price,
        "total_bought": total_bought,
        "cost_basis": avg_price * total_bought,
        "realized_pnl": _to_float(raw.get("realizedPnl")),
        "cur_price": _to_float(raw.get("curPrice")),
        "timestamp": _to_iso(raw.get("timestamp")),
    }


def normalize_portfolio_value(raw: Any) -> Optional[float]:
    """GET /value returns [{"user": "0x...", "value": 123.45}]."""
    if isinstance(raw, list) and raw:
        try:
            return float(raw[0].get("value"))
        except (TypeError, ValueError, AttributeError):
            return None
    return None
