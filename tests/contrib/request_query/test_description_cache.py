"""Requests reuse shared model descriptions without recomputing lookup and ordering paths."""

import pytest

from hare.contrib.request_query import RequestQuery
from hare.contrib.request_query.constants import GENERATION_BUCKET_KEY
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from tests.contrib.request_query.models import Book
from tests.contrib.request_query.queries import BookQuery, OrderLineQuery


class QuerysetOrderedBookQuery(RequestQuery[Book]):
    author: int | None = None

    class Meta:
        queryset = Book.objects.all().order_by("-title")


def refuse_descriptions(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a request rebuilt a cached lookup or ordering description")

    monkeypatch.setattr(LookupInfoBuilder, "get_ordering_info", refuse)
    monkeypatch.setattr(LookupInfoBuilder, "get_lookup_info", refuse)


@pytest.mark.asyncio
async def test_forgetting_all_rebuilds_declarations_and_parameters(library):
    schema = BookQuery.prepare_parameters()
    declaration = BookQuery.get_declaration()
    expected = [book.title for book in await BookQuery(ordering="title").fetch()]
    RequestQuery.forget_all()
    assert BookQuery.get_cache_bucket().get(GENERATION_BUCKET_KEY) is None
    assert BookQuery.prepare_parameters() is not schema
    assert BookQuery.get_declaration() is not declaration
    assert [book.title for book in await BookQuery(ordering="title").fetch()] == expected


@pytest.mark.asyncio
async def test_orderings_are_described_once(library, monkeypatch):
    first_titles = [book.title for book in (await BookQuery(ordering="-title,author__name").page()).result]
    await QuerysetOrderedBookQuery(author=1).fetch()
    await BookQuery().order_by("-published_at").fetch()
    first_lines = await OrderLineQuery(ordering="-book__title").page()
    await OrderLineQuery(ordering="-book__title", cursor=first_lines.next_cursor).page()
    await BookQuery(tag_ids=[2], search="b", author_city="Moscow").count()
    refuse_descriptions(monkeypatch)
    assert [book.title for book in (await BookQuery(ordering="-title,author__name").page()).result] == first_titles
    assert [book.title for book in await QuerysetOrderedBookQuery(author=2).fetch()] == ["Gamma", "Delta"]
    assert [book.title for book in await BookQuery().order_by("-published_at").fetch()][0] == "Alpha"
    lines = await OrderLineQuery(ordering="-book__title", cursor=first_lines.next_cursor).page()
    assert [line.pk for line in lines.result] == [(2, 1), (2, 2)]
    assert await BookQuery(tag_ids=[1], search="a", author_city="Kazan").count() == 1
