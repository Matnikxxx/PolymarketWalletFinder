"""
Phase B tests for leaderboard pagination + address validation in
wallet/scanner.py.

Verified here:
- offset progression across pages (0, page_size, 2*page_size, ...)
- page-size cap at 50 (API ceiling) regardless of configuration
- candidate limits below 50 and above 50
- invalid / malformed leaderboard rows are rejected individually
- duplicate addresses across pages and against seeds are de-duplicated once
- empty first page, short page, repeated page, and API failure all stop safely
- attempted pages are counted, including the failed request
- the page cap (MAX_LEADERBOARD_PAGES) is enforced and pagination cannot loop
"""
from __future__ import annotations

from unittest.mock import MagicMock

from config.settings import settings
from polymarket.api_client import PolymarketApiClient, ApiError
from wallet.scanner import (
    SEED_CANDIDATE_WALLETS,
    discover_candidates,
    is_valid_wallet_address,
    normalize_wallet_address,
    scan_top_wallets,
)

SEEDS = [s.lower() for s in SEED_CANDIDATE_WALLETS]


def _addr(index: int) -> str:
    """A well-formed address: 0x + 40 hex characters."""
    return "0x" + f"{index:040x}"


def _entry(wallet) -> dict:
    return {"proxyWallet": wallet, "pnl": 100.0, "vol": 10.0, "rank": 1, "userName": "u"}


def _page(start: int, count: int) -> list[dict]:
    return [_entry(_addr(i)) for i in range(start, start + count)]


def _always_full_client(limit: int = 50) -> MagicMock:
    """Client that returns a fresh, full, distinct page for every offset."""
    client = MagicMock(spec=PolymarketApiClient)

    def _get_leaderboard(**kwargs):
        offset = int(kwargs.get("offset", 0))
        return _page(offset * 100 + 1, limit)

    client.get_leaderboard.side_effect = _get_leaderboard
    return client


def _client_returning(pages_by_offset: dict[int, object]) -> MagicMock:
    """MagicMock client whose leaderboard response depends on the offset."""
    client = MagicMock(spec=PolymarketApiClient)

    def _get_leaderboard(**kwargs):
        offset = kwargs.get("offset", 0)
        response = pages_by_offset.get(offset, [])
        if isinstance(response, Exception):
            raise response
        return response

    client.get_leaderboard.side_effect = _get_leaderboard
    return client


# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------

def test_normalize_wallet_address_accepts_and_lowercases_valid_addresses():
    mixed_case = "0x" + "aB" * 20
    assert normalize_wallet_address(mixed_case) == mixed_case.lower()
    assert is_valid_wallet_address(mixed_case) is True


def test_normalize_wallet_address_rejects_malformed_values():
    bad_values = [
        None,
        12345,
        ["0x" + "a" * 40],
        {"wallet": "0x" + "a" * 40},
        "",                                    # empty
        "0x",                                  # prefix only
        "a" * 40,                              # missing 0x
        "0x" + "a" * 39,                       # too short
        "0x" + "a" * 41,                       # too long
        "0x" + "z" * 40,                       # non-hex characters
        "0X" + "a" * 40,                       # wrong prefix case
    ]
    for value in bad_values:
        assert normalize_wallet_address(value) is None, value
        assert is_valid_wallet_address(value) is False, value


def test_invalid_leaderboard_rows_are_rejected_individually():
    client = _client_returning({
        0: [_entry(_addr(1)), _entry("not-an-address"), _entry("0x1234"), _entry(None)],
    })

    result = discover_candidates(client, limit=30, page_size=50, max_pages=10)

    assert result.leaderboard_candidates == 1
    assert result.candidates[0] == _addr(1)
    assert set(result.candidates) == {_addr(1)} | set(SEEDS)
    assert result.rejected_addresses == 3


def test_malformed_rows_do_not_crash_discovery():
    client = MagicMock(spec=PolymarketApiClient)
    # A page that is not even a list -> treated as a malformed page, not a crash
    client.get_leaderboard.return_value = "not-a-list"
    result = discover_candidates(client, limit=30)
    assert result.candidates == SEEDS
    assert result.stop_reason == "malformed_page"

    # Rows of the wrong shape are skipped one by one
    weird_client = _client_returning({
        0: [None, "string", 42, {}, {"proxyWallet": None}, _entry(_addr(2))],
    })
    weird = discover_candidates(weird_client, limit=30)
    assert weird.leaderboard_candidates == 1
    assert weird.candidates[0] == _addr(2)
    assert weird.rejected_addresses == 5


# ---------------------------------------------------------------------------
# Pagination mechanics
# ---------------------------------------------------------------------------

def test_pagination_offset_progression_and_overall_limit():
    """limit=120 walks offsets 0/50/100 with a 50-row page size, then stops."""
    client = _client_returning({
        0: _page(1, 50),
        50: _page(51, 50),
        100: _page(101, 20),
    })

    result = discover_candidates(client, limit=120, page_size=50, max_pages=10)

    offsets = [call.kwargs["offset"] for call in client.get_leaderboard.call_args_list]
    limits = [call.kwargs["limit"] for call in client.get_leaderboard.call_args_list]
    assert offsets == [0, 50, 100]
    assert limits == [50, 50, 50]
    assert len(result.candidates) == 120
    assert result.stop_reason == "candidate_limit"
    assert result.pages_attempted == 3
    assert result.pages_succeeded == 3


def test_page_size_is_capped_at_50_even_when_configured_higher():
    client = _client_returning({0: _page(1, 50), 50: [], 500: _page(1, 50)})

    discover_candidates(client, limit=500, page_size=999, max_pages=2)

    for call in client.get_leaderboard.call_args_list:
        assert call.kwargs["limit"] <= 50
        assert call.kwargs["offset"] % 50 == 0


def test_page_cap_is_enforced():
    """At most MAX_LEADERBOARD_PAGES requests are made, even with plenty of data."""
    client = _always_full_client()

    result = discover_candidates(client, limit=10_000, page_size=50, max_pages=3)

    assert client.get_leaderboard.call_count == 3
    assert result.pages_attempted == 3
    assert result.stop_reason == "page_cap"
    assert result.leaderboard_candidates == 150


def test_configured_max_pages_setting_is_used_and_cannot_loop():
    client = _always_full_client()

    result = discover_candidates(client, limit=10_000, page_size=50)

    assert client.get_leaderboard.call_count == settings.MAX_LEADERBOARD_PAGES
    assert result.pages_attempted == settings.MAX_LEADERBOARD_PAGES
    assert result.stop_reason == "page_cap"


def test_zero_max_pages_disables_leaderboard_but_keeps_seeds():
    client = _client_returning({0: _page(1, 50)})

    result = discover_candidates(client, limit=30, page_size=50, max_pages=0)

    assert client.get_leaderboard.call_count == 0
    assert result.candidates == SEEDS
    assert result.stop_reason == "page_cap_disabled"


def test_empty_first_page_stops_immediately():
    client = _client_returning({0: []})

    result = discover_candidates(client, limit=30, page_size=50, max_pages=10)

    assert client.get_leaderboard.call_count == 1
    assert result.stop_reason == "empty_page"
    assert result.candidates == SEEDS


def test_short_page_stops_before_next_request():
    client = _client_returning({0: _page(1, 20), 50: _page(100, 50)})

    result = discover_candidates(client, limit=120, page_size=50, max_pages=10)

    assert client.get_leaderboard.call_count == 1
    assert result.stop_reason == "short_page"
    assert len(result.candidates) == 30  # 20 leaderboard + 10 seeds


def test_repeated_page_stops_pagination():
    page = _page(1, 50)
    client = _client_returning({0: page, 50: page, 100: page})

    result = discover_candidates(client, limit=10_000, page_size=50, max_pages=10)

    assert client.get_leaderboard.call_count == 2
    assert result.stop_reason == "repeated_page"
    assert len(result.candidates) == 60  # 50 unique leaderboard + 10 seeds


def test_api_failure_stops_and_is_counted_as_an_attempt():
    client = _client_returning({
        0: _page(1, 50),
        50: ApiError("boom"),
    })

    result = discover_candidates(client, limit=120, page_size=50, max_pages=10)

    assert result.pages_attempted == 2      # the failed request counts
    assert result.pages_succeeded == 1
    assert result.stop_reason == "api_error"
    assert result.leaderboard_candidates == 50
    assert set(result.candidates) == {_addr(i) for i in range(1, 51)} | set(SEEDS)

    # A failure on the very first page still yields the seeds, with no retry loop
    failing = _client_returning({0: ApiError("boom")})
    failed = discover_candidates(failing, limit=30, page_size=50, max_pages=10)
    assert failing.get_leaderboard.call_count == 1
    assert failed.candidates == SEEDS


def test_unexpected_exception_is_treated_as_an_api_failure():
    client = MagicMock(spec=PolymarketApiClient)
    client.get_leaderboard.side_effect = RuntimeError("unexpected")

    result = discover_candidates(client, limit=30)

    assert result.stop_reason == "api_error"
    assert result.candidates == SEEDS


def test_duplicate_addresses_across_pages_are_deduplicated():
    client = _client_returning({
        0: _page(1, 50),
        50: _page(26, 50),   # 25 already-seen + 25 new
        100: [],
    })

    result = discover_candidates(client, limit=200, page_size=50, max_pages=10)

    assert len(result.candidates) == len(set(result.candidates))
    assert result.leaderboard_candidates == 75
    assert result.stop_reason == "empty_page"


def test_candidate_ordering_leaderboard_first_then_seeds():
    client = _client_returning({0: _page(1, 3)})

    candidates = scan_top_wallets(client, limit=10, page_size=50, max_pages=5)

    assert candidates[:3] == [_addr(1), _addr(2), _addr(3)]
    assert candidates[3:] == SEEDS[:7]


def test_overall_limit_leaves_no_room_for_seeds():
    client = _client_returning({0: _page(1, 50)})

    result = discover_candidates(client, limit=8, page_size=50, max_pages=5)

    assert result.candidates == [_addr(i) for i in range(1, 9)]
    assert result.seed_candidates == 0
    assert not any(c in SEEDS for c in result.candidates)


def test_non_positive_limit_returns_no_candidates_without_calling_the_api():
    client = _client_returning({0: _page(1, 50)})

    result = discover_candidates(client, limit=0)

    assert result.candidates == []
    assert client.get_leaderboard.call_count == 0


def test_scan_top_wallets_returns_a_plain_address_list():
    client = _client_returning({0: _page(1, 2)})

    candidates = scan_top_wallets(client, limit=5, page_size=50, max_pages=5)

    assert isinstance(candidates, list)
    assert all(isinstance(c, str) for c in candidates)
    assert all(c == c.lower() for c in candidates)
