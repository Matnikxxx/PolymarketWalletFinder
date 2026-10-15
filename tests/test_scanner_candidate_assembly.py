"""
Tests for wallet candidate discovery and assembly in wallet/scanner.py.

Phase B behaviour verified here:
1. The leaderboard is requested with the configured page size (clamped to
   the 50-row API ceiling) starting at offset 0.
2. Valid seed wallets are appended after the leaderboard candidates when the
   leaderboard does not fill the candidate budget.
3. Duplicate leaderboard/seed addresses are de-duplicated (case-insensitively).
4. Final candidate count never exceeds limit / MAX_WALLET_CANDIDATES.
5. Behaviour degrades safely when the leaderboard is unavailable (ApiError)
   or returns fewer rows than requested.
6. Every returned address is a normalized (lowercase) 0x + 40 hex address.

Pagination-specific cases (offset progression, page cap, repeated/short/empty
pages, malformed rows) live in tests/test_scanner_pagination.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from config.settings import settings
from polymarket.api_client import PolymarketApiClient, ApiError
from wallet.scanner import scan_top_wallets, SEED_CANDIDATE_WALLETS


def _addr(index: int) -> str:
    """A well-formed address: 0x + 40 hex characters (validation is strict)."""
    return "0x" + f"{index:040x}"


def _mock_leaderboard_entry(wallet: str, rank: int = 1, pnl: float = 1000.0) -> dict:
    return {
        "proxyWallet": wallet,
        "pnl": pnl,
        "vol": 5000.0,
        "rank": rank,
        "userName": f"User_{rank}",
    }


def test_leaderboard_requested_with_configured_page_size(monkeypatch):
    """The first page is requested at offset 0 with the configured page size."""
    monkeypatch.setattr(settings, "LEADERBOARD_PAGE_SIZE", 50)
    client = MagicMock(spec=PolymarketApiClient)
    client.get_leaderboard.return_value = []

    scan_top_wallets(client, limit=30)

    assert client.get_leaderboard.called
    call_kwargs = client.get_leaderboard.call_args[1]
    # Page size never exceeds the remaining candidate budget (30) or the
    # API's 50-row ceiling.
    assert call_kwargs.get("limit") == 30
    assert call_kwargs.get("offset") == 0
    assert call_kwargs.get("category") == "OVERALL"
    assert call_kwargs.get("time_period") == "MONTH"


def test_page_size_capped_at_50_when_limit_is_larger(monkeypatch):
    """A configured/requested page size above 50 is clamped to 50."""
    monkeypatch.setattr(settings, "LEADERBOARD_PAGE_SIZE", 500)
    client = MagicMock(spec=PolymarketApiClient)
    client.get_leaderboard.return_value = []

    scan_top_wallets(client, limit=300)

    assert client.get_leaderboard.call_args[1].get("limit") == 50


def test_seeds_included_when_leaderboard_is_short():
    """When the leaderboard returns fewer rows than the budget, seeds fill it."""
    client = MagicMock(spec=PolymarketApiClient)
    lb_wallets = [_addr(i) for i in range(1, 21)]
    client.get_leaderboard.return_value = [
        _mock_leaderboard_entry(w, rank=i) for i, w in enumerate(lb_wallets, 1)
    ]

    candidates = scan_top_wallets(client, limit=30)

    assert len(candidates) == 30
    # First 20 are from the leaderboard, in leaderboard order
    assert candidates[:20] == lb_wallets
    # Remaining 10 are the seeds, in seed order
    assert candidates[20:] == [s.lower() for s in SEED_CANDIDATE_WALLETS]


def test_duplicate_seed_and_leaderboard_deduplicated():
    """If a seed wallet is also in the leaderboard, it is not duplicated."""
    client = MagicMock(spec=PolymarketApiClient)
    seed_overlap = SEED_CANDIDATE_WALLETS[0].lower()
    lb_wallets = [seed_overlap] + [_addr(i) for i in range(1, 20)]
    client.get_leaderboard.return_value = [
        _mock_leaderboard_entry(w, rank=i) for i, w in enumerate(lb_wallets, 1)
    ]

    candidates = scan_top_wallets(client, limit=30)

    assert len(candidates) == 29  # 20 unique leaderboard + 9 non-overlapping seeds
    assert len(candidates) == len(set(candidates))
    assert candidates.count(seed_overlap) == 1
    # The seed keeps its leaderboard position (leaderboard ordering preserved)
    assert candidates[0] == seed_overlap


def test_candidate_count_strictly_capped_at_limit():
    """Candidates are capped at limit even if combined count exceeds limit."""
    client = MagicMock(spec=PolymarketApiClient)
    lb_wallets = [_addr(i) for i in range(1, 21)]
    client.get_leaderboard.return_value = [
        _mock_leaderboard_entry(w, rank=i) for i, w in enumerate(lb_wallets, 1)
    ]

    candidates = scan_top_wallets(client, limit=25)

    assert len(candidates) == 25
    assert candidates[:20] == lb_wallets
    assert candidates[20:] == [s.lower() for s in SEED_CANDIDATE_WALLETS[:5]]


def test_fewer_candidates_and_api_error_fallback():
    """If the leaderboard raises ApiError, all seeds are returned up to limit."""
    client = MagicMock(spec=PolymarketApiClient)
    client.get_leaderboard.side_effect = ApiError("API down")

    candidates = scan_top_wallets(client, limit=30)

    assert len(candidates) == len(SEED_CANDIDATE_WALLETS)
    assert set(candidates) == {w.lower() for w in SEED_CANDIDATE_WALLETS}
    # The failed request is still counted exactly once -- no retry loop.
    assert client.get_leaderboard.call_count == 1


def test_limit_smaller_than_page_size():
    """When limit < page size, the request never asks for more rows than needed."""
    client = MagicMock(spec=PolymarketApiClient)
    lb_wallets = [_addr(i) for i in range(1, 6)]
    client.get_leaderboard.return_value = [
        _mock_leaderboard_entry(w, rank=i) for i, w in enumerate(lb_wallets, 1)
    ]

    candidates = scan_top_wallets(client, limit=5)

    assert client.get_leaderboard.call_args[1].get("limit") == 5
    assert candidates == lb_wallets


def test_all_returned_addresses_are_normalized_lowercase():
    """Mixed-case leaderboard addresses are normalized to lowercase."""
    client = MagicMock(spec=PolymarketApiClient)
    upper = "0x" + f"{7:040X}"  # uppercase hex
    client.get_leaderboard.return_value = [_mock_leaderboard_entry(upper)]

    candidates = scan_top_wallets(client, limit=5)

    assert candidates[0] == upper.lower()
