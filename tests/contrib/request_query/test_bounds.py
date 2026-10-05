"""The bounds a request gives a field must leave a value between them: gte not after lte, a range
starting before it ends."""

import datetime
from typing import Annotated
from urllib.parse import urlencode

import pytest

from hare.contrib.request_query import (
    Filter,
    FilterField,
    InvalidRequestQuery,
    LookupPairBounds,
    RangeBounds,
    RequestQuery,
    SearchConfig,
)
from hare.query.enums import Lookup
from tests.contrib.request_query.models import Book

EARLY = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
LATE = datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC)


class DatedBookQuery(RequestQuery[Book]):
    published_since: Annotated[datetime.datetime | None, Filter("published_at", lookup="gte")] = None
    author__lte: int | None = None

    class Meta:
        queryset = Book.objects.all()
        filters = (
            FilterField("published_at", lookups=(Lookup.GT, Lookup.GTE, Lookup.LT, Lookup.LTE, Lookup.RANGE)),
            FilterField("published_at__year", lookups=(Lookup.GTE, Lookup.LTE)),
        )
        search = SearchConfig(fields=("title",))
        pagination = None


def bounds_error(**values) -> dict:
    with pytest.raises(InvalidRequestQuery) as error:
        DatedBookQuery(**values)
    (item,) = error.value.errors
    return item


@pytest.mark.asyncio
async def test_a_lower_bound_after_the_upper_one_is_refused(library):
    assert bounds_error(published_at__gte=LATE, published_at__lte=EARLY) == {
        "loc": ["published_at__lte"],
        "msg": "Leaves no value together with published_at__gte",
        "type": "range",
    }
    assert bounds_error(published_at__year__gte=2027, published_at__year__lte=2026)["loc"] == [
        "published_at__year__lte"
    ]
    assert bounds_error(published_since=LATE, published_at__lte=EARLY)["msg"] == (
        "Leaves no value together with published_since"
    )


@pytest.mark.asyncio
async def test_equal_bounds_leave_a_value_unless_one_is_strict(library):
    DatedBookQuery(published_at__gte=EARLY, published_at__lte=EARLY)
    for lower, upper in (("gt", "lt"), ("gte", "lt"), ("gt", "lte")):
        error = bounds_error(**{f"published_at__{lower}": EARLY, f"published_at__{upper}": EARLY})
        assert error["loc"] == [f"published_at__{upper}"]


@pytest.mark.asyncio
async def test_a_range_starting_after_it_ends_is_refused(library):
    assert bounds_error(published_at__range=(LATE, EARLY)) == {
        "loc": ["published_at__range"],
        "msg": "The range starts after it ends",
        "type": "range",
    }
    query_string = urlencode([("published_at__range", EARLY.isoformat()), ("published_at__range", LATE.isoformat())])
    assert DatedBookQuery.from_query_string(query_string).published_at__range == (EARLY, LATE)


@pytest.mark.asyncio
async def test_what_is_not_a_pair_of_bounds_passes(library):
    DatedBookQuery(published_at__gte=LATE)
    DatedBookQuery(published_at__gte=LATE, author__lte=1)
    DatedBookQuery(published_at__year__gte=2027, published_at__lte=EARLY)
    naive = datetime.datetime(2025, 1, 1)
    DatedBookQuery(published_at__gte=LATE, published_at__lte=naive)


@pytest.mark.asyncio
async def test_the_declaration_lists_the_bounds(library):
    bounds = DatedBookQuery.get_declaration().bounds
    assert RangeBounds("published_at__range") in bounds
    assert LookupPairBounds("published_at__gte", "published_at__lte", strict=False) in bounds
    assert LookupPairBounds("published_since", "published_at__lt", strict=True) in bounds
    assert LookupPairBounds("published_at__year__gte", "published_at__year__lte", strict=False) in bounds
    assert not any(isinstance(bound, LookupPairBounds) and bound.upper_parameter == "author__lte" for bound in bounds)


@pytest.mark.asyncio
async def test_the_parameters_keep_one_order(library):
    DatedBookQuery.prepare_parameters()
    assert list(DatedBookQuery.model_fields) == [
        "published_since",
        "author__lte",
        "published_at__gt",
        "published_at__gte",
        "published_at__lt",
        "published_at__lte",
        "published_at__range",
        "published_at__year__gte",
        "published_at__year__lte",
        "search",
    ]
