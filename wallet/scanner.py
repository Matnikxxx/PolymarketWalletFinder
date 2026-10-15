"""
Wallet discovery. Produces a bounded candidate list by combining the
public leaderboard with a small seed list. Discovery never automatically
selects a wallet for ongoing tracking.
"""
from __future__ import annotations

import logging

from config.settings import settings
from polymarket.api_client import PolymarketApiClient, ApiError
from polymarket.parsers import normalize_leaderboard_entry

logger = logging.getLogger("wallet.scanner")

# Seed candidates are scan inputs only; they are not tracked by default.
SEED_CANDIDATE_WALLETS = [
    "0x61e1d5f5970bb82377a294d59608200cacd66b15",
    "0x8afaebae4eebe80c080b822412647244570e28f0",
    "0x5d33877d252a480fe3bdb8c272893fd52b3a7eaf",
    "0x4f352650b41fc94b753f252a6da93433e154d812",
    "0x07df0bd851dc5dc8dc9e9448e32bd50eb9ea9f0c",
    "0x3de6a6e50a6ec2b12bfb32b1adb5423c7e1da4d9",
    "0x4bfe3d336144e2ec201b50ce900b7e7a7e939cbf",
    "0x86c194d6db46277803ab465d44a83f7c3ad53ba9",
    "0x03c580ac16418c6ffed2cfe09a86bd88d62a5f61",
    "0x2142bad7fb69872a3db885f50c2ad714446ad311",
]


def scan_top_wallets(client: PolymarketApiClient, limit: int | None = None) -> list[str]:
    """
    Returns up to `limit` candidate wallet addresses: leaderboard wallets
    first (if the API call succeeds), padded out with the seed list,
    de-duplicated, capped at `limit`.

    `limit` defaults to settings.MAX_WALLET_CANDIDATES so the candidate
    pool size is one config knob, not a hardcoded constant. NOTE: today
    this can only actually reach ~20-50 candidates (the public leaderboard
    caps at 50 per request, plus the 10-wallet seed list) -- this function
    does NOT scan "thousands" of wallets, and nothing in this codebase
    claims otherwise. The `limit` param and the discovery/stats/
    verification/ranking split (see wallet/monitor.py, wallet/verifier.py,
    wallet/ranking.py, and data/wallets.json tracking selection) is what would
    let a future version paginate to a larger universe without changing
    the pipeline shape.
    """
    limit = limit if limit is not None else settings.MAX_WALLET_CANDIDATES
    candidates: list[str] = []

    try:
        leaderboard_limit = min(limit, 20)
        raw_board = client.get_leaderboard(category="OVERALL", time_period="MONTH", limit=leaderboard_limit)
        for raw in raw_board:
            entry = normalize_leaderboard_entry(raw)
            if entry["wallet"]:
                candidates.append(entry["wallet"])
        logger.info("WALLET_DISCOVERED source=leaderboard count=%d", len(candidates))
    except ApiError as exc:
        logger.warning("API_ERROR scan_top_wallets leaderboard unavailable: %s", exc)

    for w in SEED_CANDIDATE_WALLETS:
        wl = w.lower()
        if wl not in candidates:
            candidates.append(wl)

    deduped = list(dict.fromkeys(candidates))[:limit]
    logger.info("WALLET_DISCOVERED total_candidates=%d", len(deduped))
    return deduped
