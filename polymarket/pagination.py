"""
Pagination completeness metadata for the wallet-history fetchers.

Both history endpoints are paged newest-first with an offset and a hard page
cap (``settings.MAX_HISTORY_PAGES``). Reaching that cap means the fetched
window may not cover the wallet's whole history. The fetcher cannot tell
"there is another page" apart from "the history ends exactly on the cap"
without an extra request, so it reports ``truncated=True`` whenever the last
allowed page came back full. Ending on a short or empty page is the only
observation that proves completeness.

``PagedRecords`` is a ``list`` subclass so every existing caller that treats
the fetch result as a plain list keeps working unchanged (``len``, iteration,
indexing and equality are unaffected). Code that cares about completeness
reads the metadata via ``pagination_of`` / ``history_is_complete`` and treats
results without metadata as complete -- only the paginated helpers return
plain lists, and they attach metadata.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

#: A short or empty page was returned: the history is provably complete.
STOP_EXHAUSTED = "exhausted"
#: The last allowed page was full: older history may exist but was not fetched.
STOP_PAGE_CAP = "page_cap"
#: A best-effort fetch stopped on an error: the history is partial.
STOP_API_ERROR = "api_error"

TRUNCATING_STOP_REASONS = (STOP_PAGE_CAP, STOP_API_ERROR)


@dataclass(frozen=True)
class PaginationInfo:
    """How a paginated history fetch ended, and whether it can be trusted."""

    endpoint: str = ""
    pages_fetched: int = 0
    page_size: int = 0
    max_pages: int = 0
    rows_fetched: int = 0
    unique_rows: int = 0
    stop_reason: str = STOP_EXHAUSTED

    @property
    def truncated(self) -> bool:
        """True when the fetch may have left older history unfetched."""
        return self.stop_reason in TRUNCATING_STOP_REASONS

    @property
    def is_complete(self) -> bool:
        return not self.truncated

    def as_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "pages_fetched": self.pages_fetched,
            "page_size": self.page_size,
            "max_pages": self.max_pages,
            "rows_fetched": self.rows_fetched,
            "unique_rows": self.unique_rows,
            "stop_reason": self.stop_reason,
            "truncated": self.truncated,
        }


class PagedRecords(list):
    """A list of fetched records that also knows whether the cap was hit."""

    def __init__(
        self,
        records: Iterable = (),
        pagination: Optional[PaginationInfo] = None,
    ) -> None:
        super().__init__(records)
        if pagination is None:
            pagination = PaginationInfo(unique_rows=len(self))
        self.pagination = pagination

    @property
    def truncated(self) -> bool:
        return self.pagination.truncated

    @property
    def is_complete(self) -> bool:
        return self.pagination.is_complete

    @property
    def stop_reason(self) -> str:
        return self.pagination.stop_reason

    @property
    def pages_fetched(self) -> int:
        return self.pagination.pages_fetched

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"PagedRecords({len(self)} rows, pages={self.pages_fetched}, "
            f"stop={self.stop_reason}, truncated={self.truncated})"
        )


def pagination_of(records) -> Optional[PaginationInfo]:
    """Return the pagination metadata attached to `records`, if any."""
    info = getattr(records, "pagination", None)
    return info if isinstance(info, PaginationInfo) else None


def history_is_complete(records) -> bool:
    """
    Whether a fetch result represents complete history.

    A result without metadata (a plain list from a caller or test double) is
    treated as complete: only the paginated helpers return plain lists, and
    those always attach metadata when they perform the fetch.
    """
    info = pagination_of(records)
    return True if info is None else info.is_complete
