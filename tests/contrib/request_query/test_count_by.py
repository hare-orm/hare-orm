"""count_by(): how many rows have each value of a field, under every filter of the request but the field's own."""

import pytest

from hare.contrib.request_query import OffsetPagination, OrderingConfig, RequestQuery
from hare.exceptions import FieldError, QueryError
from hare.query.expressions import Q
from tests.contrib.request_query.models import BookStatus, LineReturn, Visit
from tests.contrib.request_query.queries import CatalogBookQuery, PublishedBookQuery


class LineReturnQuery(RequestQuery[LineReturn]):
    class Meta:
        queryset = LineReturn.objects.all()
        pagination = None


class VisitQuery(RequestQuery[Visit]):
    book__tags__in: list[int] | None = None

    class Meta:
        queryset = Visit.objects.all()
        ordering = OrderingConfig(default=("visitor",))
        pagination = OffsetPagination()


@pytest.mark.asyncio
async def test_counts_of_a_field_and_of_a_relation(library):
    counts = await CatalogBookQuery().count_by("status", "author")
    assert counts == {
        "status": {BookStatus.PUBLISHED: 3, BookStatus.DRAFT: 2},
        "author": {1: 2, 2: 2, 3: 1},
    }


@pytest.mark.asyncio
async def test_a_fields_own_filter_is_left_out(library):
    query = CatalogBookQuery(status=BookStatus.DRAFT, author=1)
    counts = await query.count_by("status", "author")
    # Status is counted under the author filter only, the author under the status filter only.
    assert counts["status"] == {BookStatus.DRAFT: 1, BookStatus.PUBLISHED: 1}
    assert counts["author"] == {1: 1, 3: 1}


@pytest.mark.asyncio
async def test_every_key_of_a_relation_is_its_own_filter(library):
    for parameters in ({"author__in": [2]}, {"author_id__in": [2]}):
        counts = await CatalogBookQuery(**parameters).count_by("author", "status")
        assert counts["author"] == {1: 2, 2: 2, 3: 1}
        assert counts["status"] == {BookStatus.PUBLISHED: 2}


@pytest.mark.asyncio
async def test_a_relation_to_many_rows_counts_a_row_per_related_row(library):
    counts = await CatalogBookQuery().count_by("tags")
    assert counts["tags"] == {1: 3, 2: 2, None: 1}


@pytest.mark.asyncio
async def test_a_filter_across_a_relation_to_many_rows_counts_each_row_once(library):
    counts = await CatalogBookQuery(tags__in=[1, 2]).count_by("status", "author")
    assert counts["status"] == {BookStatus.PUBLISHED: 2, BookStatus.DRAFT: 2}
    assert counts["author"] == {1: 2, 2: 1, 3: 1}


@pytest.mark.asyncio
async def test_a_date_part_filter_belongs_to_its_field(library):
    counts = await CatalogBookQuery(published_at__year=2026).count_by("status", "published_at")
    assert counts["status"] == {BookStatus.PUBLISHED: 1, BookStatus.DRAFT: 2}
    assert sum(counts["published_at"].values()) == 5


@pytest.mark.asyncio
async def test_search_where_and_the_access_condition_apply(library):
    assert (await CatalogBookQuery(search="a").count_by("author"))["author"] == {1: 2, 2: 2, 3: 1}
    assert (await CatalogBookQuery(search="bor").count_by("status"))["status"] == {BookStatus.PUBLISHED: 2}
    counts = await CatalogBookQuery().where(Q(id__in=[1, 2, 3])).count_by("author")
    assert counts["author"] == {1: 2, 2: 1}
    assert (await PublishedBookQuery().count_by("author"))["author"] == {2: 2, 1: 1}


@pytest.mark.asyncio
async def test_most_frequent_first_and_a_limit(library):
    counts = await CatalogBookQuery().count_by("author", limit=2)
    assert list(counts["author"].items()) == [(1, 2), (2, 2)]
    with pytest.raises(QueryError):
        await CatalogBookQuery().count_by("author", limit=0)


@pytest.mark.asyncio
async def test_a_relation_to_a_composite_key_counts_by_the_key(library):
    assert (await LineReturnQuery().count_by("line"))["line"] == {(2, 1): 1, (3, 2): 1}


@pytest.mark.asyncio
async def test_a_model_without_a_primary_key(library):
    assert (await VisitQuery().count_by("visitor"))["visitor"] == {"vera": 2, "ivan": 1}
    with pytest.raises(QueryError, match="no primary key"):
        await VisitQuery(book__tags__in=[1, 2]).count_by("visitor")


@pytest.mark.asyncio
async def test_an_unknown_field_is_refused(library):
    with pytest.raises(FieldError):
        await CatalogBookQuery().count_by("colour")
