"""Tests for cursor pagination."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import aclosing

import pytest

from predarb.venues.kalshi.pagination import (
    CursorLoopError,
    Page,
    PaginationLimitError,
    collect,
    paginate,
)

pytestmark = pytest.mark.unit


def pages_from(
    script: list[tuple[list[int], str | None]],
) -> tuple[Callable[[str | None], Awaitable[Page[int]]], list[str | None]]:
    """Build a fetch function that replays a scripted sequence of pages."""
    calls: list[str | None] = []

    async def fetch(cursor: str | None) -> Page[int]:
        calls.append(cursor)
        items, next_cursor = script[len(calls) - 1]
        return Page.create(tuple(items), next_cursor)

    return fetch, calls


class TestPageNormalisation:
    def test_absent_cursor_means_last_page(self):
        assert Page.create((1,), None).is_last

    def test_empty_string_cursor_means_last_page(self):
        # Kalshi has been observed returning ""; treating it as a real cursor
        # would re-request the first page forever.
        assert Page.create((1,), "").is_last

    def test_whitespace_cursor_means_last_page(self):
        assert Page.create((1,), "   ").is_last

    def test_real_cursor_is_kept(self):
        page = Page.create((1,), "abc")
        assert not page.is_last
        assert page.next_cursor == "abc"


class TestPagination:
    async def test_three_pages_then_termination(self):
        fetch, calls = pages_from([([1, 2], "A"), ([3, 4], "B"), ([5], None)])
        assert [x async for x in paginate(fetch)] == [1, 2, 3, 4, 5]
        assert calls == [None, "A", "B"]

    async def test_first_request_sends_no_cursor(self):
        fetch, calls = pages_from([([1], None)])
        await collect(fetch)
        assert calls == [None]

    async def test_single_empty_page(self):
        fetch, _ = pages_from([([], None)])
        assert await collect(fetch) == []

    async def test_empty_page_with_a_cursor_still_continues(self):
        fetch, _ = pages_from([([], "A"), ([1], None)])
        assert await collect(fetch) == [1]

    async def test_order_is_preserved_exactly(self):
        # Replay determinism depends on this.
        fetch, _ = pages_from([([3, 1], "A"), ([2], None)])
        assert await collect(fetch) == [3, 1, 2]

    async def test_streams_lazily(self):
        """Items must be yielded before later pages are fetched."""
        fetched: list[str | None] = []

        async def fetch(cursor: str | None) -> Page[int]:
            fetched.append(cursor)
            return Page.create((1,), "A" if cursor is None else None)

        async with aclosing(paginate(fetch)) as agen:
            first = await anext(agen)
            assert first == 1
            assert fetched == [None]  # second page not yet requested


class TestSafety:
    async def test_repeated_cursor_raises(self):
        fetch, _ = pages_from([([1], "A"), ([2], "A")])
        with pytest.raises(CursorLoopError, match="repeated cursor"):
            await collect(fetch)

    async def test_cursor_loop_over_several_pages_is_detected(self):
        fetch, _ = pages_from([([1], "A"), ([2], "B"), ([3], "A")])
        with pytest.raises(CursorLoopError):
            await collect(fetch)

    async def test_max_pages_raises_rather_than_truncating(self):
        # A truncated metadata sync that reports success would leave the
        # catalogue quietly incomplete.
        async def fetch(cursor: str | None) -> Page[int]:
            index = int(cursor or "0")
            return Page.create((index,), str(index + 1))

        with pytest.raises(PaginationLimitError, match="max_pages"):
            await collect(fetch, max_pages=3)

    async def test_max_items_raises(self):
        async def fetch(cursor: str | None) -> Page[int]:
            index = int(cursor or "0")
            return Page.create((index, index, index), str(index + 1))

        with pytest.raises(PaginationLimitError, match="max_items"):
            await collect(fetch, max_items=5)

    @pytest.mark.parametrize(("pages", "items"), [(0, 10), (-1, 10), (10, 0), (10, -1)])
    async def test_invalid_caps_rejected(self, pages, items):
        fetch, _ = pages_from([([1], None)])
        with pytest.raises(ValueError, match="must be positive"):
            await collect(fetch, max_pages=pages, max_items=items)


class TestErrorPropagation:
    async def test_error_midway_propagates(self):
        calls = {"n": 0}

        async def fetch(cursor: str | None) -> Page[int]:  # noqa: ARG001
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("venue exploded")
            return Page.create((1,), "A")

        with pytest.raises(RuntimeError, match="venue exploded"):
            await collect(fetch)

    async def test_partial_results_are_not_silently_returned(self):
        """A mid-walk failure must not look like a completed walk."""
        calls = {"n": 0}

        async def fetch(cursor: str | None) -> Page[int]:  # noqa: ARG001
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("boom")
            return Page.create((calls["n"],), "next" + str(calls["n"]))

        collected: list[int] = []
        with pytest.raises(RuntimeError):
            async for item in paginate(fetch):
                collected.append(item)
        assert collected == [1, 2]  # yielded before the failure, then it raised
