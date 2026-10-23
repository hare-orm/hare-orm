"""count_by(): how many rows have each value of a field, under every filter of the request but the field's own."""

import pytest

from hare.contrib.request_query import OffsetPagination, OrderingConfig, RequestQuery
from hare.exceptions import FieldError, QueryError
from hare.query.expressions import Q
from tests.contrib.request_query.models import Book, BookStatus, LineReturn, Visit
from tests.contrib.request_query.queries import CatalogBookQuery, PublishedBookQuery


class LineReturnQuery(RequestQuery[LineReturn]):
    class Meta:
        queryset = LineReturn.objects.all()
        pagination = None


class JoinedBookQuery(RequestQuery[Book]):
    tags__in: list[int] | None = None

    class Meta:
        queryset = Book.objects.all().select_related("author").prefetch_related("tags")
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


@pytest.mark.database_independent
def test_a_row_without_a_composite_key_counts_under_none():
    from hare.contrib.request_query.value_counter import ValueCounter

    assert ValueCounter.get_value({"shelf_row": None, "shelf_slot": None}, ["shelf_row", "shelf_slot"]) is None
    assert ValueCounter.get_value({"shelf_row": 1, "shelf_slot": 2}, ["shelf_row", "shelf_slot"]) == (1, 2)


@pytest.mark.asyncio
async def test_an_encrypted_field_is_refused_in_the_ordering_of_a_declaration(library):
    """The declaration took it, and every request ordering by it failed with a 500."""
    from hare.exceptions import ConfigurationError
    from tests.contrib.request_query.models import SecretNote

    class SecretOrderedQuery(RequestQuery[SecretNote]):
        class Meta:
            queryset = SecretNote.objects.all()
            ordering = OrderingConfig(fields=("secret", "title"))

    with pytest.raises(ConfigurationError, match="Meta.ordering: SecretNote.secret is encrypted"):
        SecretOrderedQuery.get_declaration()


@pytest.mark.asyncio
async def test_counting_by_an_encrypted_field_is_refused_by_name(library):
    """It was refused as an ORDER BY of the query count_by() builds."""
    from tests.contrib.request_query.models import SecretNote

    class SecretQuery(RequestQuery[SecretNote]):
        class Meta:
            queryset = SecretNote.objects.all()
            pagination = None

    with pytest.raises(FieldError, match=r"SecretQuery.count_by\(\) can't use it"):
        await SecretQuery().count_by("secret")


@pytest.mark.asyncio
async def test_counting_by_a_sensitive_field_is_refused(library):
    """The counts are keyed by the values - count_by() handed a sensitive field's values out."""
    from tests.contrib.request_query.models import SecretNote

    class ContactQuery(RequestQuery[SecretNote]):
        class Meta:
            queryset = SecretNote.objects.all()
            pagination = None

    with pytest.raises(FieldError, match=r"can't count by 'contact': contact is sensitive"):
        await ContactQuery().count_by("contact")
    assert await ContactQuery().count_by("title") == {"title": {}}


@pytest.mark.asyncio
async def test_a_declared_queryset_joining_and_prefetching_relations_is_counted(library):
    """count_by() of a queryset with select_related() raised QueryError: values() takes no select_related()."""
    counts = await JoinedBookQuery().count_by("status", "author")
    assert counts == {
        "status": {BookStatus.PUBLISHED: 3, BookStatus.DRAFT: 2},
        "author": {1: 2, 2: 2, 3: 1},
    }
    counts = await JoinedBookQuery(tags__in=[1, 2]).count_by("status")
    assert counts["status"] == {BookStatus.PUBLISHED: 2, BookStatus.DRAFT: 2}
