"""Filters of a request query: parameters named after their filter, aliases, relations and keys."""

import datetime

import pytest

from hare.contrib.request_query import InvalidRequestQuery
from hare.query.expressions import Q
from tests.contrib.request_query.models import BookStatus
from tests.contrib.request_query.queries import (
    AuthorQuery,
    BookQuery,
    EitherBookQuery,
    LineReturnQuery,
    OrderLineQuery,
    PublishedBookQuery,
    RefundQuery,
)


async def titles(query) -> list[str]:
    return sorted(book.title for book in await query.fetch())


@pytest.mark.asyncio
async def test_a_request_without_values_filters_nothing(library):
    assert await titles(BookQuery()) == ["Alpha", "Beta", "Delta", "Epsilon", "Gamma"]


@pytest.mark.asyncio
async def test_parameters_named_after_their_filter(library):
    assert await titles(BookQuery(title__icontains="ta")) == ["Beta", "Delta"]
    assert await titles(BookQuery(id__in=[1, 3])) == ["Alpha", "Gamma"]
    assert await titles(BookQuery(status=BookStatus.DRAFT)) == ["Beta", "Epsilon"]
    assert await titles(BookQuery(author__name__iexact="boris")) == ["Delta", "Gamma"]


@pytest.mark.asyncio
async def test_an_alias_keeps_the_model_out_of_the_parameter_name(library):
    assert await titles(BookQuery(author_ids=[2, 3])) == ["Delta", "Epsilon", "Gamma"]
    assert await titles(BookQuery(author_city="Kazan")) == ["Delta", "Gamma"]
    assert await titles(BookQuery(published_year=2025)) == ["Delta", "Gamma"]


@pytest.mark.asyncio
async def test_a_relation_takes_its_key_and_a_list_of_keys(library):
    assert await titles(BookQuery(author=1)) == ["Alpha", "Beta"]
    assert await titles(BookQuery(author_ids=[1])) == ["Alpha", "Beta"]
    assert await titles(BookQuery(author_keys=[3])) == ["Epsilon"]


@pytest.mark.asyncio
async def test_a_one_to_one_relation_both_ways(library):
    assert await titles(BookQuery(author_without_profile=True)) == ["Epsilon"]
    assert [author.name for author in await AuthorQuery(profile__city="Moscow").fetch()] == ["Anna"]
    assert [author.name for author in await AuthorQuery(profile__isnull=True).fetch()] == ["Clara"]


@pytest.mark.asyncio
async def test_a_many_to_many_relation_takes_each_row_once(library):
    assert await titles(BookQuery(tag_ids=[1, 2])) == ["Alpha", "Beta", "Epsilon", "Gamma"]
    assert await BookQuery(tag_ids=[1, 2]).count() == 4
    assert await titles(BookQuery(tag_name="history")) == ["Beta", "Gamma"]


@pytest.mark.asyncio
async def test_a_reverse_relation_takes_each_row_once(library):
    authors = await AuthorQuery(books__title__icontains="a").fetch()
    assert [author.name for author in authors] == ["Anna", "Boris"]
    assert [author.name for author in await AuthorQuery(books__tags=1).fetch()] == ["Anna", "Boris", "Clara"]


@pytest.mark.asyncio
async def test_a_composite_primary_key_from_one_text_value(library):
    line = await OrderLineQuery(pk="2,1").get()
    assert line.pk == (2, 1)
    lines = await OrderLineQuery(pk__in=["1,2", "3,1"]).fetch()
    assert sorted(line.pk for line in lines) == [(1, 2), (3, 1)]


@pytest.mark.asyncio
async def test_a_relation_to_a_composite_key(library):
    assert [item.reason for item in await LineReturnQuery(line="2,1").fetch()] == ["damaged"]
    assert sorted(item.reason for item in await LineReturnQuery(line__in=["2,1", "3,2"]).fetch()) == [
        "damaged",
        "late",
    ]
    assert [item.reason for item in await LineReturnQuery(line__pk__in=["3,2"]).fetch()] == ["late"]
    assert [item.reason for item in await LineReturnQuery(line__book__title="Beta").fetch()] == ["damaged"]


@pytest.mark.asyncio
async def test_a_chain_of_relations_ending_at_a_composite_key(library):
    assert [item.amount for item in await RefundQuery(line_return__line="2,1").fetch()] == [100]
    assert await RefundQuery(line_return__line__pk__in=["3,2"]).fetch() == []
    assert [item.amount for item in await RefundQuery(line_return__line__book__author__name="Anna").fetch()] == [100]


@pytest.mark.asyncio
async def test_a_composite_key_with_a_wrong_number_of_values_is_refused(db_request_query):
    with pytest.raises(InvalidRequestQuery) as error:
        OrderLineQuery(pk="2")
    assert error.value.errors[0]["loc"][0] == "pk"
    with pytest.raises(InvalidRequestQuery):
        OrderLineQuery(pk="2,x")


@pytest.mark.asyncio
async def test_a_comma_separated_parameter(library):
    assert await titles(BookQuery(ids="1,3")) == ["Alpha", "Gamma"]
    assert await titles(BookQuery(ids=["1,2", "5"])) == ["Alpha", "Beta", "Epsilon"]


@pytest.mark.asyncio
async def test_a_value_of_the_wrong_type_is_refused(db_request_query):
    with pytest.raises(InvalidRequestQuery) as error:
        BookQuery(author="anna")
    assert error.value.errors[0]["loc"][0] == "author"
    with pytest.raises(InvalidRequestQuery):
        BookQuery(status="lost")


@pytest.mark.asyncio
async def test_a_date_part_and_a_comparison(library):
    since = datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC)
    assert await titles(BookQuery(published_since=since)) == ["Alpha", "Epsilon"]


@pytest.mark.asyncio
async def test_filter_methods_sync_and_async(library):
    assert await titles(BookQuery(author_rating_at_least=4)) == ["Alpha", "Beta"]
    assert await titles(BookQuery(tag_count_at_least=2)) == ["Gamma"]
    # A method returning None adds no condition.
    assert await BookQuery(tag_count_at_least=0).count() == 5


@pytest.mark.asyncio
async def test_a_no_filter_parameter_is_read_but_filters_nothing(library):
    query = BookQuery(compact=True)
    assert query.compact is True
    assert await query.count() == 5


@pytest.mark.asyncio
async def test_parameters_join_with_and_or_with_or(library):
    assert await titles(BookQuery(author=1, status=BookStatus.DRAFT)) == ["Beta"]
    assert await titles(EitherBookQuery(title__icontains="eps", author=2)) == ["Delta", "Epsilon", "Gamma"]


@pytest.mark.asyncio
async def test_where_adds_the_handlers_conditions(library):
    assert await titles(BookQuery(author=1).where(status=BookStatus.PUBLISHED)) == ["Alpha"]
    query = BookQuery().where(Q(id__in=[1, 2, 3])).where(Q(status=BookStatus.DRAFT) | Q(author=2))
    assert await titles(query) == ["Beta", "Gamma"]


@pytest.mark.asyncio
async def test_the_access_condition_joins_every_query(library):
    assert await titles(PublishedBookQuery()) == ["Alpha", "Delta", "Gamma"]
    assert await titles(PublishedBookQuery(author=1)) == ["Alpha"]
    assert await PublishedBookQuery(status=BookStatus.DRAFT).count() == 0
    assert await PublishedBookQuery(id=2).get_or_none() is None


class TaggedBookQuery(BookQuery):
    async def get_access_condition(self) -> Q | None:
        return Q(tags__id__in=[1, 2])


@pytest.mark.asyncio
async def test_conditions_across_a_relation_to_many_rows_take_each_row_once(library):
    tagged_titles = ["Alpha", "Beta", "Epsilon", "Gamma"]
    assert sorted(book.title for book in await TaggedBookQuery().fetch()) == tagged_titles
    assert await TaggedBookQuery().count() == 4
    page = await TaggedBookQuery(limit=10).page()
    assert page.count == 4
    assert sorted(book.title for book in page.result) == tagged_titles
    assert (await TaggedBookQuery(title__icontains="gamma").get()).title == "Gamma"

    handler_query = BookQuery().where(tags__name__in=["fantasy", "history"])
    assert sorted(book.title for book in await handler_query.fetch()) == tagged_titles
    assert await BookQuery().where(tags__name__in=["fantasy", "history"]).count() == 4
