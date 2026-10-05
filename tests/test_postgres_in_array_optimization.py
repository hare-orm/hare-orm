from datetime import date, datetime

import pytest
import pytest_asyncio

from hare.contrib import test
from tests.testmodels import DatetimeFields, IntFields


@pytest_asyncio.fixture
async def int_fields_data(db_truncate):
    await IntFields.objects.bulk_create([IntFields(id=i, intnum=i) for i in range(1, 31)])


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_in_large_list_uses_any_and_matches_correctly(db, int_fields_data):
    """`postgres_is_in` (hare/backends/postgres_common/executor.py) switches a large `__in=`
    list from `IN (...)` to `= ANY($1::type[])` - one bind parameter for the whole list
    instead of one per element. 25 values clears POSTGRES_IN_ARRAY_THRESHOLD (20)."""
    ids = list(range(1, 26))
    sql = IntFields.objects.filter(id__in=ids).sql()
    assert "ANY" in sql
    assert "IN (" not in sql

    result = await IntFields.objects.filter(id__in=ids).order_by("id").values_list("id", flat=True)
    assert list(result) == ids


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_not_in_large_list_uses_any_and_matches_correctly(db, int_fields_data):
    ids = list(range(1, 26))
    result = await IntFields.objects.filter(id__not_in=ids).order_by("id").values_list("id", flat=True)
    assert list(result) == [26, 27, 28, 29, 30]


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_in_large_list_with_none_still_matches_null_rows(db, int_fields_data):
    """`intnum` is non-nullable, so this exercises the ANY(...) path's own None-stripping/
    isnull() OR-ing - the large-list path re-derives it independently of is_in()'s (unlarge)
    one, and must produce the same NULL semantics."""
    values = list(range(1, 26)) + [None]
    result = await IntFields.objects.filter(intnum__in=values).order_by("id").values_list("intnum", flat=True)
    assert list(result) == list(range(1, 26))


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_small_list_still_uses_plain_in(db, int_fields_data):
    """Below POSTGRES_IN_ARRAY_THRESHOLD, the plain IN (...) path is unchanged - confirms the
    threshold branch actually gates on size, not always taking the ANY path."""
    ids = [1, 2, 3]
    sql = IntFields.objects.filter(id__in=ids).sql()
    assert "IN (" in sql
    assert "ANY" not in sql

    result = await IntFields.objects.filter(id__in=ids).order_by("id").values_list("id", flat=True)
    assert list(result) == ids


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_in_large_list_params_inline_is_valid_sql(db, int_fields_data):
    """.sql(parameters_inline=True) must still produce SQL Postgres actually accepts for the ANY
    path - a plain ValueWrapper's list-literal fallback renders JSON-bracket text
    ("[1, 2, 3]"), which Postgres's own array-literal parser rejects outright. Executes the
    inlined text directly (not through the ORM's own parameter binding) to prove it's real,
    runnable SQL - this is exactly how sql(parameters_inline=True) gets used elsewhere (pasted as
    text into another raw query, per its own docstring).
    """
    ids = list(range(1, 26))
    inline_sql = IntFields.objects.filter(id__in=ids).sql(parameters_inline=True)
    assert "ANY" in inline_sql
    assert "[1, " not in inline_sql

    result = await IntFields.objects.raw(inline_sql)
    assert sorted(obj.id for obj in result) == ids


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_in_large_list_combined_with_other_filter(db, int_fields_data):
    """The ANY(...) parameter is still bound LAZILY at final render time (Array.get_sql(),
    same mechanism ParameterizedValueWrapper uses) - combining it with another filter in the
    same query must not corrupt either filter's own parameter numbering."""
    ids = list(range(1, 26))
    result = await IntFields.objects.filter(intnum__gte=10, id__in=ids).order_by("id").values_list("id", flat=True)
    assert list(result) == list(range(10, 26))


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_in_large_list_of_dates_against_datetime_field(db):
    """A `datetime.date` element (rather than a full `datetime.datetime`) in a large `__in=` list
    against a `DatetimeField` used to reach `CAST(ARRAY[...] AS TIMESTAMPTZ[])` completely
    unconverted - a raw `date`'s own (shorter) wire encoding under that declared array type
    corrupted the whole array on the rust_pg driver ("insufficient data left in message") and
    silently matched zero rows on asyncpg, even though the SAME value below
    POSTGRES_IN_ARRAY_THRESHOLD (one bind parameter per element, relying on Postgres's own
    implicit date->timestamptz cast) matched correctly on both drivers."""
    matching = await DatetimeFields.objects.create(datetime=datetime(2024, 1, 2, 0, 0, 0))
    non_matching = await DatetimeFields.objects.create(datetime=datetime(2024, 1, 2, 3, 4, 5))

    dates = [date(2020, 1, (i % 28) + 1) for i in range(24)] + [date(2024, 1, 2)]
    result = await DatetimeFields.objects.filter(datetime__in=dates).values_list("id", flat=True)
    assert list(result) == [matching.id]
    assert non_matching.id not in result
