"""The search and the ordering of a request query."""

import pytest

from hare.contrib.request_query import InvalidRequestQuery, OrderingConfig, RequestQuery, SearchConfig
from hare.query.enums import Connector
from hare.query.expressions.ordering import Ordering
from hare.sql.enums import Order
from tests.contrib.request_query.models import Book, OrderLine
from tests.contrib.request_query.queries import AuthorQuery, BookQuery, OrderLineQuery, TitledBookQuery


async def titles(query) -> list[str]:
    return [book.title for book in await query.fetch()]


class AllFieldsSearchQuery(RequestQuery[Book]):
    class Meta:
        queryset = Book.objects.all()
        search = SearchConfig(fields=("title", "author__name"), join_type=Connector.AND, parameter="q")
        ordering = OrderingConfig(default=("title",))
        pagination = None


class OrderedQuerysetQuery(RequestQuery[Book]):
    class Meta:
        queryset = Book.objects.all().order_by("-title")
        pagination = None


class DefaultOrderingQuery(RequestQuery[OrderLine]):
    class Meta:
        queryset = OrderLine.objects.all()
        ordering = OrderingConfig(default=("-quantity",))
        pagination = None


@pytest.mark.asyncio
async def test_search_matches_any_field(library):
    assert sorted(await titles(BookQuery(search="bor"))) == ["Delta", "Gamma"]
    assert sorted(await titles(BookQuery(search="ALP"))) == ["Alpha"]


@pytest.mark.asyncio
async def test_search_joined_with_and_and_a_parameter_of_its_own_name(library):
    assert await titles(AllFieldsSearchQuery(q="a")) == ["Alpha", "Beta"]
    assert await titles(AllFieldsSearchQuery(q="nn")) == []


@pytest.mark.asyncio
async def test_a_blank_search_filters_nothing(library):
    assert await BookQuery(search="   ").count() == 5
    assert await BookQuery(search="").count() == 5


@pytest.mark.asyncio
async def test_search_through_a_relation_to_many_takes_each_row_once(library):
    assert [author.name for author in await AuthorQuery(search="a").fetch()] == ["Anna", "Boris", "Clara"]


@pytest.mark.asyncio
async def test_search_joins_the_parameters(library):
    assert await titles(BookQuery(search="a", author=1, ordering="title")) == ["Alpha", "Beta"]


@pytest.mark.asyncio
async def test_the_requested_ordering_several_names_and_directions(library):
    assert await titles(BookQuery(ordering="-title")) == ["Gamma", "Epsilon", "Delta", "Beta", "Alpha"]
    assert await titles(BookQuery(ordering="author__name, -title")) == ["Beta", "Alpha", "Gamma", "Delta", "Epsilon"]


@pytest.mark.asyncio
async def test_an_ordering_the_query_doesnt_allow_is_refused(db_request_query):
    with pytest.raises(InvalidRequestQuery) as error:
        BookQuery(ordering="status")
    assert error.value.errors[0]["loc"] == ["ordering"]
    with pytest.raises(InvalidRequestQuery):
        BookQuery(ordering="title,-title")


@pytest.mark.asyncio
async def test_the_default_ordering_then_the_querysets_then_the_primary_key(library):
    assert await titles(TitledBookQuery()) == ["Alpha", "Beta", "Delta", "Epsilon", "Gamma"]
    assert await titles(OrderedQuerysetQuery()) == ["Gamma", "Epsilon", "Delta", "Beta", "Alpha"]
    # No ordering at all: the primary key.
    assert await titles(BookQuery()) == ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]


@pytest.mark.asyncio
async def test_equal_values_are_ordered_by_every_field_of_a_composite_key(library):
    lines = await DefaultOrderingQuery().fetch()
    assert [line.pk for line in lines] == [(1, 2), (2, 2), (3, 2), (1, 1), (2, 1), (3, 1)]
    query = DefaultOrderingQuery()
    queryset = await query.get_filtered_queryset()
    assert query.get_ordering(queryset) == ("-quantity", "order_id", "line_no")


@pytest.mark.asyncio
async def test_the_handlers_ordering_wins(library):
    assert await titles(BookQuery(ordering="title").order_by("-id")) == ["Epsilon", "Delta", "Gamma", "Beta", "Alpha"]
    ordered = BookQuery().order_by(Ordering("author__rating", Order.DESC_NULLS_LAST), "title")
    assert await titles(ordered) == ["Alpha", "Beta", "Delta", "Gamma", "Epsilon"]


@pytest.mark.asyncio
async def test_ordering_through_a_relation_with_a_composite_key(library):
    lines = await OrderLineQuery(ordering="-book__title").fetch()
    assert [line.pk for line in lines] == [(3, 1), (3, 2), (2, 1), (2, 2), (1, 1), (1, 2)]


class RelationOrderedQuerysetQuery(RequestQuery[Book]):
    class Meta:
        queryset = Book.objects.all().order_by("-author", "pk")
        pagination = None


class KeyOrderedLineQuery(RequestQuery[OrderLine]):
    class Meta:
        queryset = OrderLine.objects.all().order_by("-pk")
        pagination = None


@pytest.mark.asyncio
async def test_the_querysets_ordering_by_a_relation_and_the_key(library):
    query = RelationOrderedQuerysetQuery()
    queryset = await query.get_filtered_queryset()
    assert query.get_ordering(queryset) == (Ordering("author_id", Order.DESC), Ordering("id", Order.ASC))
    assert await titles(query) == ["Epsilon", "Gamma", "Delta", "Alpha", "Beta"]
    line_query = KeyOrderedLineQuery()
    line_queryset = await line_query.get_filtered_queryset()
    assert line_query.get_ordering(line_queryset) == (
        Ordering("order_id", Order.DESC),
        Ordering("line_no", Order.DESC),
    )
    assert [line.pk for line in await line_query.fetch()][:2] == [(3, 2), (3, 1)]
