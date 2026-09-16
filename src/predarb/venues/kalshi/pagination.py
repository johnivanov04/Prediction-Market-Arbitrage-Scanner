"""Cursor pagination.

Kalshi paginates with an opaque ``cursor`` string: a response carries the cursor
for the *next* page, and the walk ends when that cursor is absent or empty.

This module streams. A metadata sync can cover tens of thousands of markets, and
materialising every page into one list before the caller sees anything wastes
memory and delays all work until the slowest page arrives. Callers that genuinely
want a list can build one, but the default shape is an async iterator.

Safety properties
-----------------
An opaque cursor from a remote server is untrusted input, and a paginator that
trusts it can hang forever. So:

* a repeated cursor raises :class:`CursorLoopError` rather than looping;
* ``max_pages`` and ``max_items`` bound every walk, and exceeding them raises
  rather than silently truncating -- a truncated metadata sync that looks
  successful is worse than a loud failure;
* an empty-string cursor terminates, exactly as an absent one does. Kalshi has
  been observed returning ``""``, and treating that as a valid next page would
  request the first page forever.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_MAX_PAGES",
    "CursorLoopError",
    "Page",
    "PaginationError",
    "PaginationLimitError",
    "collect",
    "paginate",
]

DEFAULT_MAX_PAGES: Final = 500
DEFAULT_MAX_ITEMS: Final = 200_000


class PaginationError(Exception):
    """Base for pagination failures."""


class CursorLoopError(PaginationError):
    """The venue returned a cursor that had already been used.

    Following it would repeat a page indefinitely. Raised rather than silently
    stopping, because a loop means either a venue bug or a misunderstanding of
    the protocol, and both deserve attention.
    """


class PaginationLimitError(PaginationError):
    """A configured page or item cap was exceeded.

    Raised rather than truncating: a partial metadata sync that reports success
    would leave the catalogue quietly incomplete.
    """


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of results plus the cursor for the next.

    ``next_cursor`` is normalised: the venue's absent cursor and its empty
    string both arrive here as ``None``, so callers have one termination test.
    """

    items: tuple[T, ...]
    next_cursor: str | None = None

    @classmethod
    def create(cls, items: tuple[T, ...], raw_cursor: str | None) -> Page[T]:
        cursor = raw_cursor.strip() if isinstance(raw_cursor, str) else None
        return cls(items=items, next_cursor=cursor or None)

    @property
    def is_last(self) -> bool:
        return self.next_cursor is None


async def paginate[T](
    fetch_page: Callable[[str | None], Awaitable[Page[T]]],
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> AsyncGenerator[T]:
    """Stream every item across pages, in the order the venue returned them.

    ``fetch_page`` receives the cursor (``None`` for the first page) and returns
    a :class:`Page`. Any exception it raises propagates immediately and
    pagination stops -- a mid-walk failure must not look like a completed walk.

    Order is preserved exactly as received, so a replay over the same captured
    responses yields the same sequence.
    """
    if max_pages <= 0:
        raise ValueError(f"max_pages must be positive, got {max_pages}")
    if max_items <= 0:
        raise ValueError(f"max_items must be positive, got {max_items}")

    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages = 0
    items = 0

    while True:
        page = await fetch_page(cursor)
        pages += 1

        for item in page.items:
            items += 1
            if items > max_items:
                raise PaginationLimitError(
                    f"exceeded max_items={max_items} after {pages} page(s); "
                    "raise the cap deliberately or narrow the query"
                )
            yield item

        next_cursor = page.next_cursor
        if next_cursor is None:
            return

        if next_cursor in seen_cursors:
            raise CursorLoopError(
                f"venue returned a repeated cursor after {pages} page(s); "
                "following it would loop forever"
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor

        if pages >= max_pages:
            raise PaginationLimitError(
                f"exceeded max_pages={max_pages} with more pages available; "
                "raise the cap deliberately or narrow the query"
            )


async def collect[T](
    fetch_page: Callable[[str | None], Awaitable[Page[T]]],
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> list[T]:
    """Materialise every item into a list.

    Prefer :func:`paginate` for large walks. This exists for the cases where the
    result set is known to be small and a list is simply more convenient.
    """
    return [item async for item in paginate(fetch_page, max_pages=max_pages, max_items=max_items)]
