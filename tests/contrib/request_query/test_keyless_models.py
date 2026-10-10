"""A request query of a model without a primary key (Meta.primary_key = None)."""

import pytest

from hare.contrib.request_query import CursorPagination, OffsetPagination, OrderingConfig, RequestQuery
from hare.exceptions import ConfigurationError
from tests.contrib.request_query.models import Visit


class VisitQuery(RequestQuery[Visit]):
    visitor: str | None = None
    book__title: str | None = None

    class Meta:
        queryset = Visit.objects.all()
        ordering = OrderingConfig(fields=("visitor",), default=("visitor",))
        pagination = OffsetPagination(default_limit=2)


@pytest.mark.asyncio
async def test_filters_ordering_and_offset_pages_without_a_primary_key(library):
    page = await VisitQuery(book__title="Alpha").page()
    assert page.count == 2
    assert [visit.visitor for visit in page.result] == ["ivan", "vera"]
    assert await VisitQuery(visitor="vera").count() == 2
    query = VisitQuery()
    assert query.get_ordering(await query.get_filtered_queryset()) == ("visitor",)
    assert await VisitQuery(visitor="ivan").delete() == 1
    assert await Visit.objects.all().count() == 2


@pytest.mark.asyncio
async def test_a_cursor_pagination_needs_a_primary_key(library):
    class VisitCursorQuery(RequestQuery[Visit]):
        class Meta:
            queryset = Visit.objects.all()
            ordering = OrderingConfig(default=("visitor",))
            pagination = CursorPagination()

    with pytest.raises(ConfigurationError, match="needs a primary key"):
        VisitCursorQuery.get_declaration()
