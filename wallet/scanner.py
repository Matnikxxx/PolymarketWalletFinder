"""
Wallet discovery. Produces a bounded candidate list by combining the
public leaderboard (walked with limit/offset pagination) with a small seed
list. Discovery never automatically selects a wallet for ongoing tracking.

Phase B behaviour
-----------------
* The leaderboard is paged with ``limit``/``offset`` (``LEADERBOARD_PAGE_SIZE``
  rows per request, at most ``MAX_LEADERBOARD_PAGES`` requests). 50 is the
  hard maximum page size (Polymarket caps ``limit`` at 50 server-side).
* Every address is validated against ``^0x[a-fA-F0-9]{40}$`` and normalized
  to lowercase before it enters the candidate list. Malformed rows are
  skipped individually -- a single bad row never aborts a scan.
* Candidates are de-duplicated globally across pages and seed wallets.
* Leaderboard ordering is preserved and valid seeds are appended afterwards.
* Pagination stops safely on: empty page, short page, repeated page, API
  error, page cap, or once the overall candidate limit is reached.
* The overall limit (``MAX_WALLET_CANDIDATES``) covers the union of
  leaderboard and seed candidates. A healthy leaderboard can therefore fill
  the whole pool on its own; seeds are then only a fallback for when the
  leaderboard is short, unavailable, or when the limit is raised above a
  single page's worth of rows. Wallets already stored in data/wallets.json
  are never dropped just because they were not rediscovered (see
  wallet/monitor.py::merge_wallet_records).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

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

# Ethereum-style address as used by Polymarket proxy wallets: 0x + 40 hex chars.
ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")

# Hard ceiling on rows requested per leaderboard page. Polymarket's
# /v1/leaderboard caps `limit` at 50; anything larger is clamped here so a
# misconfiguration can never ask for an unbounded page.
LEADERBOARD_MAX_PAGE_SIZE = 50


def normalize_wallet_address(raw: object) -> str | None:
    """
    Validate and normalize one candidate address.

    Returns the lowercase address when it matches ``^0x[a-fA-F0-9]{40}$``,
    otherwise ``None``. Anything that is not a string (None, numbers, dicts
    from a malformed API row) is rejected rather than coerced.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not ADDRESS_PATTERN.match(candidate):
        return None
    return candidate.lower()


def is_valid_wallet_address(raw: object) -> bool:
    """True when `raw` is a well-formed 0x-prefixed 40-hex-character address."""
    return normalize_wallet_address(raw) is not None


@dataclass
class DiscoveryResult:
    """Outcome of one discovery pass (leaderboard pagination + seeds)."""

    candidates: list[str] = field(default_factory=list)
    leaderboard_candidates: int = 0
    seed_candidates: int = 0
    rejected_addresses: int = 0
    pages_attempted: int = 0
    pages_succeeded: int = 0
    stop_reason: str = "not_started"

    @property
    def total_candidates(self) -> int:
        return len(self.candidates)


def discover_candidates(
    client: PolymarketApiClient,
    limit: int | None = None,
    page_size: int | None = None,
    max_pages: int | None = None,
) -> DiscoveryResult:
    """
    Walk the public leaderboard with limit/offset pagination and combine the
    validated, de-duplicated addresses with the seed list.

    `limit` defaults to ``settings.MAX_WALLET_CANDIDATES`` (hard cap on the
    combined candidate count), `page_size` to ``settings.LEADERBOARD_PAGE_SIZE``
    (clamped to 50) and `max_pages` to ``settings.MAX_LEADERBOARD_PAGES``.
    Never raises: an API failure simply ends leaderboard discovery and the
    valid seeds are used instead.
    """
    limit = settings.MAX_WALLET_CANDIDATES if limit is None else int(limit)
    configured_page_size = settings.LEADERBOARD_PAGE_SIZE if page_size is None else page_size
    configured_max_pages = settings.MAX_LEADERBOARD_PAGES if max_pages is None else max_pages

    result = DiscoveryResult()
    if limit <= 0:
        result.stop_reason = "candidate_limit"
        logger.info("WALLET_DISCOVERED total_candidates=0 stop_reason=%s", result.stop_reason)
        return result

    # Page stride is fixed for the whole walk so offsets stay aligned:
    # never ask for more rows than the remaining candidate budget, never
    # more than the API's 50-row ceiling, and never fewer than 1 row.
    try:
        requested_page_size = int(configured_page_size)
    except (TypeError, ValueError):
        requested_page_size = LEADERBOARD_MAX_PAGE_SIZE
    page_stride = max(1, min(requested_page_size, LEADERBOARD_MAX_PAGE_SIZE, limit))

    try:
        page_cap = max(0, int(configured_max_pages))
    except (TypeError, ValueError):
        page_cap = settings.MAX_LEADERBOARD_PAGES

    seen: set[str] = set()
    page_signatures: set[tuple[str, ...]] = set()

    for page_index in range(page_cap):
        if len(result.candidates) >= limit:
            result.stop_reason = "candidate_limit"
            break

        offset = page_index * page_stride
        result.pages_attempted += 1
        try:
            raw_board = client.get_leaderboard(
                category="OVERALL",
                time_period="MONTH",
                order_by="PNL",
                limit=page_stride,
                offset=offset,
            )
        except ApiError as exc:
            result.stop_reason = "api_error"
            logger.warning(
                "API_ERROR scan_top_wallets leaderboard unavailable page=%d offset=%d: %s",
                page_index + 1, offset, exc,
            )
            break
        except Exception as exc:  # defensive: discovery must never crash a scan
            result.stop_reason = "api_error"
            logger.warning(
                "API_ERROR scan_top_wallets unexpected leaderboard failure page=%d offset=%d: %s",
                page_index + 1, offset, exc,
            )
            break

        result.pages_succeeded += 1

        if not isinstance(raw_board, list):
            result.stop_reason = "malformed_page"
            logger.warning(
                "WALLET_DISCOVERY_MALFORMED_PAGE page=%d offset=%d type=%s",
                page_index + 1, offset, type(raw_board).__name__,
            )
            break

        if not raw_board:
            result.stop_reason = "empty_page"
            break

        page_addresses: list[str] = []
        for raw in raw_board:
            entry = normalize_leaderboard_entry(raw) if isinstance(raw, dict) else {}
            address = normalize_wallet_address(entry.get("wallet"))
            if address is None:
                result.rejected_addresses += 1
                continue
            page_addresses.append(address)

        signature = tuple(page_addresses)
        if signature and signature in page_signatures:
            # The API returned an already-seen page (e.g. an offset the
            # endpoint ignores). Stop instead of re-walking it forever.
            result.stop_reason = "repeated_page"
            logger.warning(
                "WALLET_DISCOVERY_REPEATED_PAGE page=%d offset=%d rows=%d",
                page_index + 1, offset, len(page_addresses),
            )
            break
        page_signatures.add(signature)

        for address in page_addresses:
            if address in seen:
                continue
            if len(result.candidates) >= limit:
                break
            seen.add(address)
            result.candidates.append(address)

        if len(result.candidates) >= limit:
            result.stop_reason = "candidate_limit"
            break

        if len(raw_board) < page_stride:
            # Fewer rows than requested -> the leaderboard is exhausted.
            result.stop_reason = "short_page"
            break
    else:
        if page_cap == 0:
            result.stop_reason = "page_cap_disabled"
        else:
            result.stop_reason = "page_cap"

    result.leaderboard_candidates = len(result.candidates)

    for raw_seed in SEED_CANDIDATE_WALLETS:
        if len(result.candidates) >= limit:
            break
        seed = normalize_wallet_address(raw_seed)
        if seed is None:
            result.rejected_addresses += 1
            logger.warning("WALLET_DISCOVERY_INVALID_SEED value=%r", raw_seed)
            continue
        if seed in seen:
            continue
        seen.add(seed)
        result.candidates.append(seed)
        result.seed_candidates += 1

    logger.info(
        "WALLET_DISCOVERED source=leaderboard pages_attempted=%d pages_succeeded=%d "
        "rejected_addresses=%d stop_reason=%s count=%d",
        result.pages_attempted, result.pages_succeeded, result.rejected_addresses,
        result.stop_reason, result.leaderboard_candidates,
    )
    logger.info(
        "WALLET_DISCOVERED source=seed count=%d", result.seed_candidates
    )
    logger.info("WALLET_DISCOVERED total_candidates=%d", result.total_candidates)
    return result


def scan_top_wallets(
    client: PolymarketApiClient,
    limit: int | None = None,
    page_size: int | None = None,
    max_pages: int | None = None,
) -> list[str]:
    """
    Returns up to `limit` candidate wallet addresses: valid leaderboard
    wallets first (paged, in leaderboard order), padded out with the valid
    seed list, globally de-duplicated and normalized to lowercase.

    `limit` defaults to settings.MAX_WALLET_CANDIDATES; `page_size` and
    `max_pages` default to settings.LEADERBOARD_PAGE_SIZE (max 50) and
    settings.MAX_LEADERBOARD_PAGES. Discovery is best-effort: it degrades to
    the seed list rather than raising when the leaderboard is unavailable.
    """
    return discover_candidates(
        client, limit=limit, page_size=page_size, max_pages=max_pages
    ).candidates
