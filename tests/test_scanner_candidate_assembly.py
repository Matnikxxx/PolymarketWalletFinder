"""
Tests for wallet candidate discovery and assembly in wallet/scanner.py.

Verifies:
1. The leaderboard is requested with limit=20 (min(limit, 20)), not 30.
2. Existing seed wallets are included in the final candidate list when limit > 20.
3. Duplicate leaderboard/seed addresses are deduplicated properly.
4. Final candidate count does not exceed limit / MAX_WALLET_CANDIDATES.
5. Behavior remains correct when fewer candidates or API errors occur.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from config.settings import settings
from polymarket.api_client import PolymarketApiClient, ApiError
from wallet.scanner import scan_top_wallets, SEED_CANDIDATE_WALLETS


def _mock_leaderboard_entry(wallet: str, rank: int = 1, pnl: float = 1000.0) -> dict:
    return {
        "proxyWallet": wallet,
        "pnl": pnl,
        "vol": 5000.0,
        "rank": rank,
        "userName": f"User_{rank}",
    }


def test_leaderboard_requested_with_limit_20_when_max_candidates_is_30():
    """Verify get_leaderboard is called with limit=20 even if scan_top_wallets limit=30."""
    client = MagicMock(spec=PolymarketApiClient)
    client.get_leaderboard.return_value = []

    scan_top_wallets(client, limit=30)

    assert client.get_leaderboard.called
    call_kwargs = client.get_leaderboard.call_args[1]
    assert call_kwargs.get("limit") == 20
    assert call_kwargs.get("category") == "OVERALL"
    assert call_kwargs.get("time_period") == "MONTH"


def test_seeds_included_when_leaderboard_returns_20():
    """When leaderboard returns 20 wallets, the 10 seeds are appended, totaling 30."""
    client = MagicMock(spec=PolymarketApiClient)
    lb_wallets = [f"0xlb{i:038x}" for i in range(1, 21)]
    client.get_leaderboard.return_value = [_mock_leaderboard_entry(w, rank=i) for i, w in enumerate(lb_wallets, 1)]

    candidates = scan_top_wallets(client, limit=30)

    assert len(candidates) == 30
    # First 20 are from leaderboard
    assert candidates[:20] == lb_wallets
    # Remaining 10 are from SEED_CANDIDATE_WALLETS
    for s in SEED_CANDIDATE_WALLETS:
        assert s.lower() in candidates


def test_duplicate_seed_and_leaderboard_deduplicated():
    """If a seed wallet is also in the leaderboard, it is not duplicated."""
    client = MagicMock(spec=PolymarketApiClient)
    seed_overlap = SEED_CANDIDATE_WALLETS[0].lower()
    lb_wallets = [seed_overlap] + [f"0xlb{i:038x}" for i in range(1, 20)]
    client.get_leaderboard.return_value = [_mock_leaderboard_entry(w, rank=i) for i, w in enumerate(lb_wallets, 1)]

    candidates = scan_top_wallets(client, limit=30)

    assert len(candidates) == 29  # 19 unique lb + 10 seeds (one overlap)
    assert len(candidates) == len(set(candidates))
    assert candidates.count(seed_overlap) == 1


def test_candidate_count_strictly_capped_at_limit():
    """Candidates are capped at limit even if combined count exceeds limit."""
    client = MagicMock(spec=PolymarketApiClient)
    lb_wallets = [f"0xlb{i:038x}" for i in range(1, 21)]
    client.get_leaderboard.return_value = [_mock_leaderboard_entry(w, rank=i) for i, w in enumerate(lb_wallets, 1)]

    candidates = scan_top_wallets(client, limit=25)

    assert len(candidates) == 25


def test_fewer_candidates_and_api_error_fallback():
    """If leaderboard raises ApiError, all seeds are returned up to limit."""
    client = MagicMock(spec=PolymarketApiClient)
    client.get_leaderboard.side_effect = ApiError("API down")

    candidates = scan_top_wallets(client, limit=30)

    assert len(candidates) == len(SEED_CANDIDATE_WALLETS)
    assert set(candidates) == {w.lower() for w in SEED_CANDIDATE_WALLETS}


def test_limit_smaller_than_20():
    """When limit < 20 (e.g., 5), leaderboard requests min(5, 20) = 5."""
    client = MagicMock(spec=PolymarketApiClient)
    lb_wallets = [f"0xlb{i:038x}" for i in range(1, 6)]
    client.get_leaderboard.return_value = [_mock_leaderboard_entry(w, rank=i) for i, w in enumerate(lb_wallets, 1)]

    candidates = scan_top_wallets(client, limit=5)

    call_kwargs = client.get_leaderboard.call_args[1]
    assert call_kwargs.get("limit") == 5
    assert len(candidates) == 5
