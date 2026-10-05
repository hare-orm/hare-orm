"""get_lookup_info()/get_lookups() of array and range fields and of paths through them - the value
each lookup takes, and that only PostgreSQL runs them."""

import datetime

import pytest

from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.enums import PostgresqlLookup
from hare.dialects.postgresql.fields.ranges import Range
from hare.query.enums import Lookup, LookupValueShape
from tests.dialects.postgresql.models_container_paths import Crate, Shelf

SQLITE = DialectRegistry.get_dialect("sqlite")
POSTGRESQL = DialectRegistry.get_dialect("postgresql")


def describe(key: str) -> tuple:
    lookup_info = Crate._meta.get_lookup_info(key)
    return lookup_info.transforms, lookup_info.lookup, lookup_info.value_shape, lookup_info.value_type


@pytest.mark.asyncio
async def test_array_lookups(db_container_paths):
    assert describe("numbers") == ((), Lookup.EXACT, LookupValueShape.LIST, int)
    assert describe("numbers__contains") == ((), Lookup.CONTAINS, LookupValueShape.LIST, int)
    assert describe("numbers__overlap") == ((), Lookup.OVERLAP, LookupValueShape.LIST, int)
    assert describe("numbers__len") == ((), Lookup.LENGTH, LookupValueShape.VALUE, int)
    assert describe("numbers__len__gt") == (("len",), Lookup.GT, LookupValueShape.VALUE, int)
    assert describe("words__0__startswith") == (("0",), Lookup.STARTSWITH, LookupValueShape.VALUE, str)
    assert describe("grid__0__1") == (("0", "1"), Lookup.EXACT, LookupValueShape.VALUE, int)
    assert describe("numbers__isnull") == ((), Lookup.ISNULL, LookupValueShape.VALUE, bool)


@pytest.mark.asyncio
async def test_range_lookups(db_container_paths):
    assert describe("span") == ((), Lookup.EXACT, LookupValueShape.RANGE, int)
    assert describe("span__contains") == ((), Lookup.CONTAINS, LookupValueShape.VALUE, int)
    assert describe("span__overlap") == ((), Lookup.OVERLAP, LookupValueShape.RANGE, int)
    assert describe("days__fully_lt") == ((), PostgresqlLookup.FULLY_LT, LookupValueShape.RANGE, datetime.date)
    assert describe("span__startswith__gte") == (("startswith",), Lookup.GTE, LookupValueShape.VALUE, int)
    assert describe("span__isempty") == (("isempty",), Lookup.EXACT, LookupValueShape.VALUE, bool)
    assert describe("shelf__crates__span__adjacent_to")[1] == PostgresqlLookup.ADJACENT_TO


@pytest.mark.asyncio
async def test_container_fields_run_only_on_postgresql(db_container_paths):
    lookup_info = Crate._meta.get_lookup_info("numbers__contains")
    # An array takes its column type from the dialect a query runs on.
    assert lookup_info.dialects is None
    assert lookup_info.is_supported(POSTGRESQL)
    assert not lookup_info.is_supported(SQLITE)
    assert Crate._meta.get_lookups("numbers", SQLITE) == {}
    assert {"", "contains", "contained_by", "overlap", "len", "item"} <= set(
        Crate._meta.get_lookups("numbers", POSTGRESQL)
    )


@pytest.mark.asyncio
async def test_described_keys_filter(db_container_paths):
    shelf = await Shelf.objects.create(id=1)
    await Crate.objects.create(id=1, shelf=shelf, span=Range(1, 5), numbers=[3, 1, 2], words=["pear", "apple"])
    await Crate.objects.create(id=2, span=Range(7, None), numbers=[9], words=["fig"])
    for key, value, expected in (
        ("numbers__contains", [1, 2], [1]),
        ("numbers__len__gt", 1, [1]),
        ("words__0__startswith", "pe", [1]),
        ("span__contains", 8, [2]),
        ("span__startswith__gte", 7, [2]),
    ):
        Crate._meta.get_lookup_info(key)
        assert await Crate.objects.filter(**{key: value}).order_by("id").values_list("id", flat=True) == expected
