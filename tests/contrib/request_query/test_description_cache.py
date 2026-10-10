"""A request reads no description of a filter or an ordering - the class's declaration holds them."""

import pytest

from hare.contrib.request_query import RequestQuery
from hare.query.queryset import QuerySet
from tests.contrib.request_query.models import Book
from tests.contrib.request_query.queries import BookQuery, OrderLineQuery


class QuerysetOrderedBookQuery(RequestQuery[Book]):
    author: int | None = None

    class Meta:
        queryset = Book.objects.all().order_by("-title")


def refuse_descriptions(monkeypatch):
    def refuse(self, name):
        raise AssertionError(f"a request described {name!r} again")

    monkeypatch.setattr(QuerySet, "get_ordering_info", refuse)
    monkeypatch.setattr(QuerySet, "get_lookup_info", refuse)


@pytest.mark.asyncio
async def test_orderings_are_described_once(library, monkeypatch):
    first_titles = [book.title for book in (await BookQuery(ordering="-title,author__name").page()).result]
    await QuerysetOrderedBookQuery().fetch()
    await BookQuery().order_by("-published_at").fetch()
    first_lines = await OrderLineQuery(ordering="-book__title").page()
    refuse_descriptions(monkeypatch)
    assert [book.title for book in (await BookQuery(ordering="-title,author__name").page()).result] == first_titles
    assert [book.title for book in await QuerysetOrderedBookQuery(author=2).fetch()] == ["Gamma", "Delta"]
    assert [book.title for book in await BookQuery().order_by("-published_at").fetch()][0] == "Alpha"
    lines = await OrderLineQuery(ordering="-book__title", cursor=first_lines.next_cursor).page()
    assert [line.pk for line in lines.result] == [(2, 1), (2, 2)]
    assert await BookQuery(tag_ids=[1], search="a", author_city="Kazan").count() == 1
