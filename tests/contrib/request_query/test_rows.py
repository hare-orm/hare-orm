"""One row, the count, existence and deletion of what a request query asks for."""

import pytest

from hare.exceptions import DoesNotExist, MultipleObjectsReturned, QueryError
from tests.contrib.request_query.models import Book, BookStatus, OrderLine
from tests.contrib.request_query.queries import (
    BookQuery,
    OrderLineQuery,
    PublishedBookQuery,
    TitledBookQuery,
)


@pytest.mark.asyncio
async def test_get_one_row(library):
    assert (await BookQuery(id=3).get()).title == "Gamma"
    with pytest.raises(DoesNotExist):
        await BookQuery(id=99).get()
    with pytest.raises(MultipleObjectsReturned):
        await BookQuery(author=1).get()


@pytest.mark.asyncio
async def test_get_or_none(library):
    assert (await BookQuery(title__icontains="gam").get(does_not_exist_exception=None)).id == 3
    assert await BookQuery(id=99).get(does_not_exist_exception=None) is None


@pytest.mark.asyncio
async def test_get_takes_the_exception_parameters_of_queryset_get(library):
    class MissingBook(Exception):
        pass

    class TooManyBooks(Exception):
        pass

    with pytest.raises(MissingBook):
        await BookQuery(id=99).get(does_not_exist_exception=MissingBook)
    with pytest.raises(TooManyBooks):
        await BookQuery(author=1).get(multiple_objects_returned_exception=TooManyBooks)
    book = await BookQuery(author=1).get(multiple_objects_returned_exception=None)
    assert book.author_id == 1
    assert await BookQuery(id=99).get(does_not_exist_exception=None, multiple_objects_returned_exception=None) is None
    with pytest.raises(TypeError, match="multiple_objects_returned_exception"):
        await BookQuery(author=1).get(multiple_objects_returned_exception="many")


@pytest.mark.asyncio
async def test_get_through_a_relation_to_many_takes_the_row_once(library):
    book = await BookQuery(id=3, tag_ids=[1, 2]).get()
    assert book.title == "Gamma"


@pytest.mark.asyncio
async def test_count_and_exists(library):
    assert await BookQuery(author=2).count() == 2
    assert await BookQuery(author=2).exists() is True
    assert await BookQuery(author=2, status=BookStatus.DRAFT).exists() is False


@pytest.mark.asyncio
async def test_after_fetch_runs_on_every_way_of_fetching(library):
    assert [book.shouted_title for book in await TitledBookQuery().fetch()][:2] == ["ALPHA", "BETA"]
    assert (await TitledBookQuery(id=2).get()).shouted_title == "BETA"
    page = await TitledBookQuery().page()
    assert page.result[-1].shouted_title == "GAMMA"


@pytest.mark.asyncio
async def test_delete_the_filtered_rows(library):
    assert await BookQuery(status=BookStatus.DRAFT).delete() == 2
    assert sorted(await Book.objects.all().values_list("title", flat=True)) == ["Alpha", "Delta", "Gamma"]


@pytest.mark.asyncio
async def test_delete_by_a_composite_key(library):
    assert await OrderLineQuery(pk__in=["1,1", "1,2"]).delete() == 2
    assert await OrderLine.objects.filter(order_id=1).count() == 0
    assert await OrderLine.objects.all().count() == 4


@pytest.mark.asyncio
async def test_delete_needs_a_filter_and_keeps_to_the_access_condition(library):
    with pytest.raises(QueryError, match="needs a filter"):
        await BookQuery().delete()
    with pytest.raises(QueryError, match="needs a filter"):
        await PublishedBookQuery().delete()
    assert await PublishedBookQuery(author=1).delete() == 1
    assert sorted(await Book.objects.filter(author_id=1).values_list("title", flat=True)) == ["Beta"]
    assert await BookQuery().where(author=2).delete() == 2


@pytest.mark.asyncio
async def test_the_cache_key_names_the_query_and_its_values(db_request_query):
    assert BookQuery(author=1, ordering="title").cache_key() == BookQuery(ordering="title", author=1).cache_key()
    assert BookQuery(author=1).cache_key() != BookQuery(author=2).cache_key()
    assert BookQuery(author=1).cache_key() != PublishedBookQuery(author=1).cache_key()
    assert BookQuery(author=1).cache_key().startswith("tests.contrib.request_query.queries.BookQuery:")
