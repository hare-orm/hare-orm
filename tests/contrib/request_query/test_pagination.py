"""Offset and cursor pagination of a request query."""

from dataclasses import dataclass

import pytest

from hare.contrib.request_query import (
    Cursor,
    CursorDirection,
    CursorPage,
    CursorPagination,
    InvalidRequestQuery,
    OrderingConfig,
    Page,
    RequestQuery,
    ScrollPage,
)
from hare.contrib.test import assert_query_count
from hare.exceptions import (
    QueryError,
)
from tests.contrib.request_query.models import Book, OrderLine
from tests.contrib.request_query.queries import AuthorQuery, BookQuery, BookScrollQuery, OrderLineQuery


@dataclass
class FakeRequest:
    url: str


class BookCursorQuery(RequestQuery[Book]):
    author: int | None = None

    class Meta:
        queryset = Book.objects.all()
        ordering = OrderingConfig(fields=("published_at", "title"), default=("-published_at",))
        pagination = CursorPagination(default_limit=2, max_limit=3)


class LineByBookQuery(RequestQuery[OrderLine]):
    class Meta:
        queryset = OrderLine.objects.all()
        ordering = OrderingConfig(default=("book__title",))
        pagination = CursorPagination(default_limit=4)


@pytest.mark.asyncio
async def test_an_offset_page_with_its_count_and_links(library):
    request = FakeRequest("http://api.test/books?status=published&tag=a&tag=b")
    page = await BookQuery(request=request, ordering="title").page()
    assert isinstance(page, Page)
    assert [book.title for book in page.result] == ["Alpha", "Beta"]
    assert (page.count, page.limit, page.offset) == (5, 2, 0)
    assert page.next == "http://api.test/books?status=published&tag=a&tag=b&offset=2"
    assert page.previous is None


@pytest.mark.asyncio
async def test_a_scroll_page_reads_one_row_past_itself_instead_of_counting(library):
    request = FakeRequest("http://api.test/books?author=1")
    async with assert_query_count(1, using="models") as counter:
        page = await BookScrollQuery(request=request).page()
    assert "COUNT" not in counter.queries[0].upper()
    assert isinstance(page, ScrollPage)
    assert not hasattr(page, "count")
    assert [book.title for book in page.result] == ["Alpha", "Beta"]
    assert (page.limit, page.offset, page.previous) == (2, 0, None)
    assert page.next == "http://api.test/books?author=1&offset=2"


@pytest.mark.asyncio
async def test_the_last_scroll_page(library):
    request = FakeRequest("http://api.test/books?offset=3")
    ending_exactly = await BookScrollQuery(request=request, offset=3).page()
    assert [book.title for book in ending_exactly.result] == ["Epsilon", "Gamma"]
    assert ending_exactly.next is None
    assert ending_exactly.previous == "http://api.test/books?offset=1"
    past_the_end = await BookScrollQuery(offset=9).page()
    assert (past_the_end.result, past_the_end.next) == ([], None)


@pytest.mark.asyncio
async def test_the_last_offset_page_and_a_previous_page_before_an_uneven_offset(library):
    last = await BookQuery(request=FakeRequest("http://api.test/books?offset=4"), offset=4, ordering="title").page()
    assert [book.title for book in last.result] == ["Gamma"]
    assert last.next is None
    assert last.previous == "http://api.test/books?offset=2"
    # Offset 1 with a page of 2: the previous page is the first row, from offset 0.
    uneven = await BookQuery(request=FakeRequest("http://api.test/books?offset=1"), offset=1).page()
    assert uneven.previous == "http://api.test/books"


@pytest.mark.asyncio
async def test_without_a_request_address_a_page_has_no_links(library):
    page = await BookQuery(limit=1).page()
    assert page.next is None and page.previous is None
    assert page.count == 5


@pytest.mark.asyncio
async def test_the_page_size_is_checked(db_request_query):
    with pytest.raises(InvalidRequestQuery):
        BookQuery(limit=0)
    with pytest.raises(InvalidRequestQuery):
        BookQuery(limit=11)
    with pytest.raises(InvalidRequestQuery):
        BookQuery(offset=-1)


@pytest.mark.asyncio
async def test_page_needs_a_pagination(library):
    with pytest.raises(QueryError, match="no Meta.pagination"):
        await AuthorQuery().page()


@pytest.mark.asyncio
async def test_cursor_pages_forward_and_back(library):
    first = await BookCursorQuery().page()
    assert isinstance(first, CursorPage)
    assert [book.title for book in first.result] == ["Alpha", "Epsilon"]
    assert first.previous_cursor is None and first.next_cursor is not None
    second = await BookCursorQuery(cursor=first.next_cursor).page()
    assert [book.title for book in second.result] == ["Beta", "Gamma"]
    third = await BookCursorQuery(cursor=second.next_cursor).page()
    assert [book.title for book in third.result] == ["Delta"]
    assert third.next_cursor is None
    back = await BookCursorQuery(cursor=third.previous_cursor).page()
    assert [book.title for book in back.result] == ["Beta", "Gamma"]
    assert back.next_cursor is not None
    start = await BookCursorQuery(cursor=back.previous_cursor).page()
    assert [book.title for book in start.result] == ["Alpha", "Epsilon"]
    assert start.previous_cursor is None


@pytest.mark.asyncio
async def test_cursor_links_keep_the_other_parameters(library):
    request = FakeRequest("http://api.test/books?author=1&limit=1")
    first = await BookCursorQuery(request=request, author=1, limit=1).page()
    assert first.next == f"http://api.test/books?author=1&limit=1&cursor={first.next_cursor}"
    second = await BookCursorQuery(request=request, author=1, limit=1, cursor=first.next_cursor).page()
    assert [book.title for book in second.result] == ["Beta"]
    assert second.next_cursor is None


@pytest.mark.asyncio
async def test_cursor_over_a_composite_key_and_a_relation(library):
    first = await OrderLineQuery().page()
    assert [line.pk for line in first.result] == [(1, 1), (1, 2)]
    second = await OrderLineQuery(cursor=first.next_cursor).page()
    assert [line.pk for line in second.result] == [(2, 1), (2, 2)]
    by_title = await OrderLineQuery(ordering="-book__title").page()
    assert [line.pk for line in by_title.result] == [(3, 1), (3, 2)]
    after = await OrderLineQuery(ordering="-book__title", cursor=by_title.next_cursor).page()
    assert [line.pk for line in after.result] == [(2, 1), (2, 2)]
    lines = await LineByBookQuery().page()
    assert [line.pk for line in lines.result] == [(1, 1), (1, 2), (2, 1), (2, 2)]
    rest = await LineByBookQuery(cursor=lines.next_cursor).page()
    assert [line.pk for line in rest.result] == [(3, 1), (3, 2)]


@pytest.mark.asyncio
async def test_a_cursor_of_another_ordering_or_a_forged_one_is_refused(library):
    first = await OrderLineQuery(ordering="quantity").page()
    with pytest.raises(InvalidRequestQuery, match="another ordering"):
        await OrderLineQuery(ordering="-quantity", cursor=first.next_cursor).page()
    with pytest.raises(InvalidRequestQuery) as error:
        await OrderLineQuery(cursor="not-a-cursor").page()
    assert error.value.errors[0]["loc"] == ["cursor"]
    forged = Cursor(CursorDirection.NEXT, ("order_id", "line_no"), ("x", 1)).encode()
    with pytest.raises(InvalidRequestQuery):
        await OrderLineQuery(cursor=forged).page()
    short = Cursor(CursorDirection.NEXT, ("order_id", "line_no"), (1,)).encode()
    with pytest.raises(InvalidRequestQuery):
        await OrderLineQuery(cursor=short).page()
