from datetime import UTC, date, datetime, timedelta, timezone

import pytest
import pytest_asyncio

from hare.contrib import test
from hare.exceptions import DoesNotExist, QueryError, UnSupportedError
from hare.query.expressions import F, Q
from tests.testmodels import JSONFields


async def get_by_data_filter(obj, **kwargs) -> JSONFields:
    return await JSONFields.objects.get(data__filter=kwargs)


async def get_ids_by_data_filter(**kwargs) -> list[int]:
    return sorted(await JSONFields.objects.filter(data__filter=kwargs).values_list("id", flat=True))


@pytest_asyncio.fixture
async def json_obj(db):
    """Create test object with JSON data for postgres tests."""
    obj = await JSONFields.objects.create(
        data={
            "val": "word1",
            "int_val": 123,
            "float_val": 123.1,
            "bool_val": True,
            "date_val": datetime(1970, 1, 1, 12, 36, 59, 123456),
            "int_list": [1, 2, 3],
            "nested": {
                "val": "word2",
                "int_val": 456,
                "int_list": [4, 5, 6],
                "date_val": datetime(1970, 1, 1, 12, 36, 59, 123456),
                "nested": {
                    "val": "word3",
                },
            },
        }
    )
    return obj


@pytest.mark.asyncio
async def test_json_in(json_obj):
    assert await get_by_data_filter(json_obj, val__in=["word1", "word2"]) == json_obj
    assert await get_by_data_filter(json_obj, val__not_in=["word3", "word4"]) == json_obj

    with pytest.raises(DoesNotExist):
        await get_by_data_filter(json_obj, val__in=["doesnotexist"])


@pytest.mark.asyncio
async def test_json_numeric_in(json_obj):
    """_create_json_criterion's numeric-cast decision checked `type(value) in (int, float,
    Decimal)`, always False for is_in/not_in's list-shaped value - int_val__in=[123, 456] crashed
    outright (asyncpg tried to bind an int against the JSON path's ->>'...' text extraction),
    where int_val__gte=100 (same field, a scalar comparison operator) already worked."""
    assert await get_by_data_filter(json_obj, int_val__in=[123, 456]) == json_obj
    assert await get_by_data_filter(json_obj, int_val__not_in=[999, 888]) == json_obj

    with pytest.raises(DoesNotExist):
        await get_by_data_filter(json_obj, int_val__in=[999, 888])


@pytest.mark.asyncio
async def test_json_bool_comparisons(json_obj):
    """_create_json_criterion's numeric-cast decision checked `type(value) in (int, float,
    Decimal)`, deliberately excluding bool (a Python int subtype) - but there was no boolean
    branch anywhere either, so the extracted ->>'...' text column stayed uncast against a bool
    parameter, and every one of these crashed outright with "operator does not exist: text =
    boolean" (or "numeric >= boolean" for the __gte case, since a comparison operator forced a
    numeric cast regardless of the value's actual type)."""
    assert await get_by_data_filter(json_obj, bool_val=True) == json_obj
    assert await get_by_data_filter(json_obj, bool_val__not=False) == json_obj
    assert await get_by_data_filter(json_obj, bool_val__in=[True]) == json_obj
    assert await get_by_data_filter(json_obj, bool_val__gte=True) == json_obj

    with pytest.raises(DoesNotExist):
        await get_by_data_filter(json_obj, bool_val=False)


@pytest.mark.asyncio
async def test_json_defaults(json_obj):
    assert await get_by_data_filter(json_obj, val__not="word2") == json_obj
    assert await get_by_data_filter(json_obj, val__isnull=False) == json_obj
    assert await get_by_data_filter(json_obj, val__not_isnull=True) == json_obj


@pytest.mark.asyncio
async def test_json_int_comparisons(json_obj):
    assert await get_by_data_filter(json_obj, int_val=123) == json_obj
    assert await get_by_data_filter(json_obj, int_val__gt=100) == json_obj
    assert await get_by_data_filter(json_obj, int_val__gte=100) == json_obj
    assert await get_by_data_filter(json_obj, int_val__lt=200) == json_obj
    assert await get_by_data_filter(json_obj, int_val__lte=200) == json_obj
    assert await get_by_data_filter(json_obj, int_val__range=[100, 200]) == json_obj

    with pytest.raises(DoesNotExist):
        await get_by_data_filter(json_obj, int_val__gt=1000)


@pytest.mark.asyncio
async def test_json_float_comparisons(json_obj):
    assert await get_by_data_filter(json_obj, float_val__gt=100.0) == json_obj
    assert await get_by_data_filter(json_obj, float_val__gte=100.0) == json_obj
    assert await get_by_data_filter(json_obj, float_val__lt=200.0) == json_obj
    assert await get_by_data_filter(json_obj, float_val__lte=200.0) == json_obj
    assert await get_by_data_filter(json_obj, float_val__range=[100.0, 200.0]) == json_obj

    with pytest.raises(DoesNotExist):
        await get_by_data_filter(json_obj, int_val__gt=1000.0)


@pytest.mark.asyncio
async def test_json_string_comparisons(json_obj):
    assert await get_by_data_filter(json_obj, val__contains="ord") == json_obj
    assert await get_by_data_filter(json_obj, val__icontains="OrD") == json_obj
    assert await get_by_data_filter(json_obj, val__startswith="wor") == json_obj
    assert await get_by_data_filter(json_obj, val__istartswith="wOr") == json_obj
    assert await get_by_data_filter(json_obj, val__endswith="rd1") == json_obj
    assert await get_by_data_filter(json_obj, val__iendswith="Rd1") == json_obj
    assert await get_by_data_filter(json_obj, val__iexact="wOrD1") == json_obj

    with pytest.raises(DoesNotExist):
        await get_by_data_filter(json_obj, val__contains="doesnotexist")


@pytest.mark.asyncio
async def test_date_comparisons(json_obj):
    assert await get_by_data_filter(json_obj, date_val=datetime(1970, 1, 1, 12, 36, 59, 123456)) == json_obj
    assert await get_by_data_filter(json_obj, date_val__year=1970) == json_obj
    assert await get_by_data_filter(json_obj, date_val__month=1) == json_obj
    assert await get_by_data_filter(json_obj, date_val__day=1) == json_obj
    assert await get_by_data_filter(json_obj, date_val__hour=12) == json_obj
    assert await get_by_data_filter(json_obj, date_val__minute=36) == json_obj
    assert await get_by_data_filter(json_obj, date_val__second=59) == json_obj
    assert await get_by_data_filter(json_obj, date_val__microsecond=123456) == json_obj


@pytest.mark.asyncio
async def test_json_date_range(json_obj):
    """_create_json_criterion's numeric-cast branch listed `between_and` unconditionally, with
    no check against the range value's own contents - a date's between_and tuple never matched
    the isinstance(value, (date, datetime)) check (only ever true for a bare, non-tuple value),
    so date_val__range fell through to the numeric-cast branch and crashed with an
    invalid-numeric-syntax error against a date-shaped JSON string."""
    assert (
        await get_by_data_filter(
            json_obj,
            date_val__range=[datetime(1970, 1, 1, 0, 0, 0), datetime(1970, 1, 2, 0, 0, 0)],
        )
        == json_obj
    )

    with pytest.raises(DoesNotExist):
        await get_by_data_filter(
            json_obj,
            date_val__range=[datetime(1971, 1, 1, 0, 0, 0), datetime(1971, 1, 2, 0, 0, 0)],
        )


@pytest.mark.asyncio
async def test_json_list(json_obj):
    assert await get_by_data_filter(json_obj, int_list__0__gt=0) == json_obj
    assert await get_by_data_filter(json_obj, int_list__0__lt=2) == json_obj

    with pytest.raises(DoesNotExist):
        await get_by_data_filter(json_obj, int_list__0__range=(20, 30))


@pytest.mark.asyncio
async def test_nested(json_obj):
    assert await get_by_data_filter(json_obj, nested__val="word2") == json_obj
    assert await get_by_data_filter(json_obj, nested__int_val=456) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val=datetime(1970, 1, 1, 12, 36, 59, 123456)) == json_obj
    assert await get_by_data_filter(json_obj, nested__val__icontains="orD") == json_obj
    assert await get_by_data_filter(json_obj, nested__int_val__gte=400) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val__year=1970) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val__month=1) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val__day=1) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val__hour=12) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val__minute=36) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val__second=59) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val__microsecond=123456) == json_obj
    assert await get_by_data_filter(json_obj, nested__val__iexact="wOrD2") == json_obj
    assert await get_by_data_filter(json_obj, nested__int_val__lt=500) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val__year=1970) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val__month=1) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val__day=1) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val__hour=12) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val__minute=36) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val__second=59) == json_obj
    assert await get_by_data_filter(json_obj, nested__date_val__microsecond=123456) == json_obj
    assert await get_by_data_filter(json_obj, nested__val__iexact="wOrD2") == json_obj


@pytest.mark.asyncio
async def test_nested_nested(json_obj):
    assert await get_by_data_filter(json_obj, nested__nested__val="word3") == json_obj


@pytest.mark.asyncio
async def test_json_numeric_filter_skips_non_numeric_values(db):
    """Every row's extracted value used to be CAST to NUMERIC unconditionally, so one row holding
    a non-numeric string made numeric lookups crash for the whole table ('invalid input syntax
    for type numeric'), depending purely on the stored data rather than the queried key."""
    text_row = await JSONFields.objects.create(data={"a": "x"})
    one_row = await JSONFields.objects.create(data={"a": 1})
    five_row = await JSONFields.objects.create(data={"a": 5})

    assert await get_ids_by_data_filter(a=5) == [five_row.id]
    assert await get_ids_by_data_filter(a__gt=0) == [one_row.id, five_row.id]
    assert await get_ids_by_data_filter(a__in=[1, 5]) == [one_row.id, five_row.id]
    assert await get_ids_by_data_filter(a__range=[0, 10]) == [one_row.id, five_row.id]
    assert await get_ids_by_data_filter(a__not=5) == [text_row.id, one_row.id]
    assert await get_ids_by_data_filter(a__not_in=[1, 5]) == [text_row.id]


@pytest.mark.asyncio
async def test_json_numeric_filter_on_array_index_skips_non_numeric_values(db):
    await JSONFields.objects.create(data={"arr": ["x"]})
    matching_row = await JSONFields.objects.create(data={"arr": [1]})
    await JSONFields.objects.create(data={"arr": []})

    assert await get_ids_by_data_filter(arr__0=1) == [matching_row.id]
    assert await get_ids_by_data_filter(arr__0__gt=0) == [matching_row.id]


@pytest.mark.asyncio
async def test_json_filter_distinguishes_number_from_string(db):
    """The text-extracted value used to be compared as-is regardless of the stored JSON type, so
    `{"a": 3}` matched a `"3"` string filter and (through the numeric cast) `{"a": "3"}` matched
    a `3` numeric filter."""
    number_row = await JSONFields.objects.create(data={"a": 3})
    string_row = await JSONFields.objects.create(data={"a": "3"})

    assert await get_ids_by_data_filter(a=3) == [number_row.id]
    assert await get_ids_by_data_filter(a="3") == [string_row.id]
    assert await get_ids_by_data_filter(a__in=["3"]) == [string_row.id]
    assert await get_ids_by_data_filter(a__in=[3]) == [number_row.id]
    assert await get_ids_by_data_filter(a__startswith="3") == [string_row.id]


@pytest.mark.asyncio
async def test_json_filter_distinguishes_bool_from_string(db):
    bool_row = await JSONFields.objects.create(data={"a": True})
    string_row = await JSONFields.objects.create(data={"a": "true"})

    assert await get_ids_by_data_filter(a=True) == [bool_row.id]
    assert await get_ids_by_data_filter(a="true") == [string_row.id]
    assert await get_ids_by_data_filter(a__not=True) == [string_row.id]


@pytest.mark.asyncio
async def test_json_filter_digit_segment_is_object_key_or_array_index(db):
    """A digit-only path segment was always rendered as an array index (`->1`), which is NULL
    against an object - `data__filter={"1": ...}` never found an object's own "1" key even
    though `data__contains` did."""
    object_row = await JSONFields.objects.create(data={"1": "numkey", "nested": {"2": {"k": 7}}})
    array_row = await JSONFields.objects.create(data={"list": ["p", "q"], "nested": [{"k": 8}, {"k": 9}]})

    assert await get_ids_by_data_filter(**{"1": "numkey"}) == [object_row.id]
    assert await JSONFields.objects.filter(data__contains={"1": "numkey"}).count() == 1
    assert await get_ids_by_data_filter(nested__2__k=7) == [object_row.id]
    assert await get_ids_by_data_filter(list__1="q") == [array_row.id]
    assert await get_ids_by_data_filter(list__0="p") == [array_row.id]
    assert await get_ids_by_data_filter(nested__1__k=9) == [array_row.id]
    assert await get_ids_by_data_filter(nested__1__k__gt=8) == [array_row.id]


@pytest.mark.asyncio
async def test_json_f_expression_digit_segment_is_object_key_or_array_index(db):
    await JSONFields.objects.create(data={"1": "numkey"})
    await JSONFields.objects.create(data=["zero", "one"])

    values = await JSONFields.objects.annotate(picked=F("data__1")).order_by("id").values_list("picked", flat=True)

    assert values == ["numkey", "one"]


@pytest.mark.asyncio
async def test_json_isnull_means_no_value_at_path(db):
    """`__isnull=True` on a `__filter` path means "no value at that path" - a key holding JSON
    `null` AND a key that's absent from the object both count (`->>` returns SQL NULL for both),
    and `__isnull=False` means "a non-null value is there". Use `has_key` to tell the first two
    apart."""
    json_null_row = await JSONFields.objects.create(data={"key": None, "other": 1})
    missing_key_row = await JSONFields.objects.create(data={"other": 2})
    present_value_row = await JSONFields.objects.create(data={"key": "value", "other": 3})

    assert await get_ids_by_data_filter(key__isnull=True) == [json_null_row.id, missing_key_row.id]
    assert await get_ids_by_data_filter(key__not_isnull=False) == [json_null_row.id, missing_key_row.id]
    assert await get_ids_by_data_filter(key__isnull=False) == [present_value_row.id]
    assert await get_ids_by_data_filter(key__not_isnull=True) == [present_value_row.id]


@pytest.mark.asyncio
async def test_json_has_key_tells_json_null_from_missing_key_and_value(db):
    """The three states behind `->>` being NULL/non-NULL, told apart by combining `isnull` with
    `has_key`: JSON null = isnull AND has_key, missing key = isnull AND NOT has_key, value present
    = NOT isnull."""
    json_null_row = await JSONFields.objects.create(data={"key": None, "other": 1})
    missing_key_row = await JSONFields.objects.create(data={"other": 2})
    present_value_row = await JSONFields.objects.create(data={"key": "value", "other": 3})

    async def get_ids(*args, **kwargs) -> list[int]:
        return sorted(await JSONFields.objects.filter(*args, **kwargs).values_list("id", flat=True))

    assert await get_ids(Q(data__filter={"key__isnull": True}) & Q(data__has_key="key")) == [json_null_row.id]
    assert await get_ids(Q(data__filter={"key__isnull": True}) & ~Q(data__has_key="key")) == [missing_key_row.id]
    assert await get_ids(data__filter={"key__isnull": False}) == [present_value_row.id]
    assert await get_ids(data__has_key="key") == [json_null_row.id, present_value_row.id]
    assert await get_ids(data__has_key="other") == [json_null_row.id, missing_key_row.id, present_value_row.id]


@pytest.mark.asyncio
async def test_json_has_key(db):
    dog = await JSONFields.objects.create(data={"breed": "labrador", "age": 3})
    cat = await JSONFields.objects.create(data={"color": "black"})
    await JSONFields.objects.create(data={}, data_null=None)

    assert await JSONFields.objects.filter(data__has_key="breed").values_list("id", flat=True) == [dog.id]
    assert await JSONFields.objects.filter(data__has_key="color").values_list("id", flat=True) == [cat.id]
    assert await JSONFields.objects.filter(data__has_key="missing").count() == 0
    # Only a top-level key counts, not one nested inside a value.
    nested = await JSONFields.objects.create(data={"outer": {"inner": 1}})
    assert await JSONFields.objects.filter(data__has_key="inner").count() == 0
    assert await JSONFields.objects.filter(data__has_key="outer").values_list("id", flat=True) == [nested.id]
    assert dog.id not in await JSONFields.objects.exclude(data__has_key="breed").values_list("id", flat=True)


@pytest.mark.asyncio
async def test_json_has_keys_requires_all_keys(db):
    both = await JSONFields.objects.create(data={"a": 1, "b": 2, "c": 3})
    await JSONFields.objects.create(data={"a": 1})
    await JSONFields.objects.create(data={"b": None})

    assert await JSONFields.objects.filter(data__has_keys=["a", "b"]).values_list("id", flat=True) == [both.id]
    assert await JSONFields.objects.filter(data__has_keys=["a", "b", "z"]).count() == 0
    assert await JSONFields.objects.filter(data__has_keys=["a"]).count() == 2
    # An empty key list is vacuously satisfied by every row that has a document.
    assert await JSONFields.objects.filter(data__has_keys=[]).count() == 3


@pytest.mark.asyncio
async def test_json_has_any_keys_requires_one_key(db):
    only_a = await JSONFields.objects.create(data={"a": 1})
    only_b = await JSONFields.objects.create(data={"b": None})
    await JSONFields.objects.create(data={"c": 3})

    assert sorted(await JSONFields.objects.filter(data__has_any_keys=["a", "b"]).values_list("id", flat=True)) == [
        only_a.id,
        only_b.id,
    ]
    assert await JSONFields.objects.filter(data__has_any_keys=["x", "y"]).count() == 0
    assert await JSONFields.objects.filter(data__has_any_keys=[]).count() == 0
    assert await JSONFields.objects.filter(data__has_any_keys=("a",)).values_list("id", flat=True) == [only_a.id]


@pytest.mark.asyncio
async def test_json_has_key_lookups_do_not_match_sql_null_column(db):
    """A SQL-NULL column has no document at all - no key lookup matches it."""
    await JSONFields.objects.create(data={"a": 1}, data_null=None)
    with_key = await JSONFields.objects.create(data={"a": 1}, data_null={"key": None})

    assert await JSONFields.objects.filter(data_null__has_key="key").values_list("id", flat=True) == [with_key.id]
    assert await JSONFields.objects.filter(data_null__has_keys=["key"]).values_list("id", flat=True) == [with_key.id]
    assert await JSONFields.objects.filter(data_null__has_any_keys=["key", "other"]).values_list("id", flat=True) == [
        with_key.id
    ]
    assert await JSONFields.objects.filter(data_null__has_keys=[]).values_list("id", flat=True) == [with_key.id]
    # __isnull on the nested path still counts the NULL column (no value at that path).
    assert await JSONFields.objects.filter(data_null__filter={"key__isnull": True}).count() == 2


@pytest.mark.asyncio
async def test_json_has_key_on_nested_path_through_filter(db):
    nested = await JSONFields.objects.create(data={"pet": {"breed": "labrador", "name": None}})
    await JSONFields.objects.create(data={"pet": {"color": "black"}})
    await JSONFields.objects.create(data={"other": 1})

    assert await get_ids_by_data_filter(pet__has_key="breed") == [nested.id]
    assert await get_ids_by_data_filter(pet__has_key="name") == [nested.id]
    assert await get_ids_by_data_filter(pet__has_keys=["breed", "name"]) == [nested.id]
    assert len(await get_ids_by_data_filter(pet__has_any_keys=["breed", "color"])) == 2
    assert await get_ids_by_data_filter(pet__has_key="missing") == []
    # An empty path (`has_key` alone) tests the column itself, same as `data__has_key`.
    assert len(await get_ids_by_data_filter(has_key="pet")) == 2


@pytest.mark.asyncio
async def test_json_has_key_rejects_non_string_arguments(db):
    with pytest.raises(UnSupportedError):
        await JSONFields.objects.filter(data__has_keys="abc").count()
    with pytest.raises(UnSupportedError):
        await JSONFields.objects.filter(data__has_any_keys="abc").count()
    with pytest.raises(UnSupportedError):
        await JSONFields.objects.filter(data__has_key=None).count()
    with pytest.raises(UnSupportedError):
        await JSONFields.objects.filter(data__filter={"pet__has_key": 1}).count()
    with pytest.raises(UnSupportedError):
        await JSONFields.objects.filter(data__filter={"pet__has_keys": ["a", 1]}).count()


@pytest.mark.asyncio
async def test_json_filter_rejects_malformed_argument(db):
    with pytest.raises(QueryError):
        await JSONFields.objects.filter(data__filter={}).count()
    with pytest.raises(QueryError):
        await JSONFields.objects.filter(data__filter={"a": 1, "b": 2}).count()
    with pytest.raises(QueryError):
        await JSONFields.objects.filter(data__filter="not-a-dict").count()


@pytest.mark.asyncio
async def test_json_string_ordering_comparisons(db):
    """A string value under __gt/__gte/__lt/__lte/__range compares as text, not NUMERIC."""
    apple = await JSONFields.objects.create(data={"s": "apple"})
    banana = await JSONFields.objects.create(data={"s": "banana"})
    cherry = await JSONFields.objects.create(data={"s": "cherry"})
    await JSONFields.objects.create(data={"s": 5})

    assert await get_ids_by_data_filter(s__gt="b") == [banana.id, cherry.id]
    assert await get_ids_by_data_filter(s__gte="banana") == [banana.id, cherry.id]
    assert await get_ids_by_data_filter(s__lt="b") == [apple.id]
    assert await get_ids_by_data_filter(s__lte="banana") == [apple.id, banana.id]
    assert await get_ids_by_data_filter(s__range=("b", "c")) == [banana.id]
    assert await get_ids_by_data_filter(s__gt=1) == [cherry.id + 1]


@pytest.mark.asyncio
async def test_json_aware_datetime_comparisons_keep_stored_offset(db):
    """An aware datetime value compares against the stored string's own UTC offset."""
    row = await JSONFields.objects.create(data={"t": "2024-01-01T12:00:00+03:00"})

    assert await get_ids_by_data_filter(t__gt=datetime(2024, 1, 1, 9, 30, tzinfo=UTC)) == []
    assert await get_ids_by_data_filter(t__lt=datetime(2024, 1, 1, 9, 30, tzinfo=UTC)) == [row.id]
    assert await get_ids_by_data_filter(t=datetime(2024, 1, 1, 9, 0, tzinfo=UTC)) == [row.id]
    assert await get_ids_by_data_filter(
        t__range=(datetime(2024, 1, 1, 8, 0, tzinfo=UTC), datetime(2024, 1, 1, 10, 0, tzinfo=UTC))
    ) == [row.id]
    assert await get_ids_by_data_filter(t__gt=datetime(2024, 1, 1, 11, 0)) == [row.id]


@pytest.mark.asyncio
async def test_json_filter_object_and_array_values_compare_as_jsonb(db):
    object_row = await JSONFields.objects.create(data={"a": {"x": 1, "y": [1, 2]}})
    array_row = await JSONFields.objects.create(data={"a": [1, 2]})
    await JSONFields.objects.create(data={"a": 1})

    assert await get_ids_by_data_filter(a={"y": [1, 2], "x": 1.0}) == [object_row.id]
    assert await get_ids_by_data_filter(a=[1, 2]) == [array_row.id]
    assert await get_ids_by_data_filter(a__in=[[1, 2], {"z": 0}]) == [array_row.id]
    assert array_row.id not in await get_ids_by_data_filter(a__not=[1, 2])
    assert object_row.id in await get_ids_by_data_filter(a__not=[1, 2])
    with pytest.raises(QueryError):
        await get_ids_by_data_filter(a__gt={"x": 1})


@pytest.mark.asyncio
async def test_json_filter_nested_operator_dict(db):
    """{"path": {"not": value}} is the nested spelling of {"path__not": value}."""
    enabled = await JSONFields.objects.create(data={"enabled": True})
    disabled = await JSONFields.objects.create(data={"enabled": False})

    enabled_ids = await get_ids_by_data_filter(enabled={"not": False})
    assert enabled.id in enabled_ids
    assert disabled.id not in enabled_ids
    assert await get_ids_by_data_filter(enabled={"in": [False]}) == [disabled.id]


@pytest.mark.asyncio
async def test_json_filter_in_rejects_mixed_value_types(db):
    await JSONFields.objects.create(data={"a": 1})

    with pytest.raises(QueryError):
        await get_ids_by_data_filter(a__in=[1, "abc"])


@pytest.mark.asyncio
async def test_json_filter_array_index_does_not_match_a_scalar(db):
    """Postgres answers `scalar -> 0` with the scalar itself - an index must only match arrays."""
    await JSONFields.objects.create(data={"a": 1})
    array_row = await JSONFields.objects.create(data={"a": [1, 2]})

    assert await get_ids_by_data_filter(a__0=1) == [array_row.id]


@pytest.mark.asyncio
async def test_json_date_filters_skip_non_date_strings(db):
    """A non-date string under the path (or a day its month doesn't have) never matches instead
    of failing the whole query on a CAST error."""
    dated = await JSONFields.objects.create(data={"created": "2024-05-01T10:00:00"})
    await JSONFields.objects.create(data={"created": "n/a"})
    await JSONFields.objects.create(data={"created": "2024-02-30"})
    await JSONFields.objects.create(data={"created": 1})

    assert await get_ids_by_data_filter(created__year=2024) == [dated.id]
    assert await get_ids_by_data_filter(created__day=1) == [dated.id]
    assert await get_ids_by_data_filter(created__gt=datetime(2024, 1, 1)) == [dated.id]
    assert await get_ids_by_data_filter(created__lt=datetime(2025, 1, 1, tzinfo=UTC)) == [dated.id]


@pytest_asyncio.fixture
async def json_rows_with_nulls(db):
    """Rows whose "a"/"flag" key holds a number, a string, a bool, JSON null, or is missing."""
    rows = {
        "number": await JSONFields.objects.create(data={"a": 1, "flag": True}),
        "float": await JSONFields.objects.create(data={"a": 1.0, "flag": None}),
        "string": await JSONFields.objects.create(data={"a": "1", "flag": "true"}),
        "null": await JSONFields.objects.create(data={"a": None}),
        "missing": await JSONFields.objects.create(data={"b": 1}),
        "array": await JSONFields.objects.create(data={"a": [10, 20, 30]}),
        "other_number": await JSONFields.objects.create(data={"a": 2, "flag": False}),
    }
    return {name: row.id for name, row in rows.items()}


def get_row_ids(rows: dict[str, int], *names: str) -> list[int]:
    return sorted(rows[name] for name in names)


@pytest.mark.asyncio
async def test_json_filter_in_with_none_matches_null_and_missing_key(json_rows_with_nulls):
    """None inside an in-list matches a JSON null or a missing key, every other value is still
    compared with its type guard - a mixed [1, None] list used to skip the guard, failing on
    asyncpg and matching the string "1" on rust_pg."""
    rows = json_rows_with_nulls

    assert await get_ids_by_data_filter(a__in=[1, None]) == get_row_ids(rows, "number", "float", "null", "missing")
    assert await get_ids_by_data_filter(a__in=[None]) == get_row_ids(rows, "null", "missing")
    assert await get_ids_by_data_filter(a__in=["1", None]) == get_row_ids(rows, "string", "null", "missing")


@pytest.mark.asyncio
async def test_json_filter_not_in_with_none_excludes_null_and_missing_key(json_rows_with_nulls):
    rows = json_rows_with_nulls

    assert await get_ids_by_data_filter(a__not_in=[1, None]) == get_row_ids(rows, "string", "array", "other_number")
    assert await get_ids_by_data_filter(a__not_in=[1]) == get_row_ids(
        rows, "string", "null", "missing", "array", "other_number"
    )


@pytest.mark.asyncio
async def test_json_filter_bool_in_with_none(json_rows_with_nulls):
    rows = json_rows_with_nulls
    all_rows_without_bool_flag = ["float", "null", "missing", "array"]

    assert await get_ids_by_data_filter(flag__in=[True, None]) == get_row_ids(
        rows, "number", *all_rows_without_bool_flag
    )
    assert await get_ids_by_data_filter(flag__not_in=[True, None]) == get_row_ids(rows, "string", "other_number")


@pytest.mark.asyncio
async def test_json_filter_container_in_with_none(json_rows_with_nulls):
    rows = json_rows_with_nulls

    assert await get_ids_by_data_filter(a__in=[[10, 20, 30], None]) == get_row_ids(rows, "array", "null", "missing")


@pytest.mark.asyncio
async def test_json_filter_date_range_with_open_bound(db):
    """A date range with a None bound is compared as dates, not forced to a numeric cast."""
    later = await JSONFields.objects.create(data={"created": "2024-05-01"})
    await JSONFields.objects.create(data={"created": "2023-05-01"})

    assert await get_ids_by_data_filter(created__range=(datetime(2024, 1, 1), None)) == [later.id]


@pytest.mark.asyncio
async def test_json_filter_negative_array_index(db):
    """A negative path part counts from the array's end, like Postgres's own `-> -1`."""
    array_row = await JSONFields.objects.create(data={"a": [10, 20, 30]})
    await JSONFields.objects.create(data={"a": [30, 20]})
    object_row = await JSONFields.objects.create(data={"a": {"-1": 30}})

    assert await get_ids_by_data_filter(**{"a__-1": 30}) == sorted([array_row.id, object_row.id])
    assert await get_ids_by_data_filter(**{"a__-2": 20}) == [array_row.id]


@pytest.mark.asyncio
async def test_json_f_negative_array_index(db):
    """F("data__a__-1") reads the array's last element on every dialect."""
    await JSONFields.objects.create(data={"a": [10, 20, 30]})

    last_values = await JSONFields.objects.annotate(last=F("data__a__-1")).values_list("last", flat=True)
    # Postgres extracts the value as text, SQLite as its JSON type.
    assert [str(value) for value in last_values] == ["30"]


async def create_deeply_nested_row() -> int:
    nested_value: object = 7
    for _ in range(12):
        nested_value = [nested_value]
    await JSONFields.objects.create(data={"0": [1]})
    return (await JSONFields.objects.create(data=nested_value)).id


@pytest.mark.asyncio
async def test_json_f_path_of_many_digit_segments_grows_linearly(db):
    """Each digit segment used to repeat the path built so far three times - 10 segments made a
    4 MB query, 12 ran the server out of memory."""
    matching_id = await create_deeply_nested_row()
    path = "__".join(["0"] * 12)

    annotated = (
        JSONFields.objects.annotate(picked=F(f"data__{path}")).filter(id=matching_id).values_list("picked", flat=True)
    )

    assert len(annotated.sql()) < 1500
    assert await annotated == [7]


@pytest.mark.asyncio
async def test_json_filter_path_of_many_digit_segments_grows_linearly(db):
    matching_id = await create_deeply_nested_row()
    path = "__".join(["0"] * 12)

    filtered = JSONFields.objects.filter(data__filter={path: 7}).values_list("id", flat=True)

    assert len(filtered.sql()) < 1500
    assert await filtered == [matching_id]


@pytest.mark.asyncio
async def test_json_filter_in_and_not_in_with_many_values(db):
    """A long in/not_in list is bound as one array parameter - one parameter per value hit the
    driver's parameter limit."""
    number_row = await JSONFields.objects.create(data={"a": 1, "s": "x1", "b": True, "d": "2024-01-02T00:00:00"})
    fraction_row = await JSONFields.objects.create(data={"a": 2.5, "s": "x2", "b": False})
    null_row = await JSONFields.objects.create(data={"a": None})
    missing_row = await JSONFields.objects.create(data={"z": 1})
    many_numbers = [*range(3, 40003), 1, 2.5]

    assert await get_ids_by_data_filter(a__in=many_numbers) == [number_row.id, fraction_row.id]
    assert await get_ids_by_data_filter(a__not_in=many_numbers) == [null_row.id, missing_row.id]
    assert await get_ids_by_data_filter(a__in=[*many_numbers, None]) == sorted(
        [number_row.id, fraction_row.id, null_row.id, missing_row.id]
    )
    assert await get_ids_by_data_filter(a__not_in=[*many_numbers, None]) == []
    assert await get_ids_by_data_filter(s__in=[f"x{index}" for index in range(40000)]) == [
        number_row.id,
        fraction_row.id,
    ]
    assert await get_ids_by_data_filter(b__in=[True] * 40000) == [number_row.id]
    assert await get_ids_by_data_filter(
        d__in=[datetime(2024, 1, 2, second=index % 60, minute=index // 60 % 60) for index in range(40000)]
    ) == [number_row.id]


@pytest.mark.asyncio
async def test_json_filter_in_with_many_objects(db):
    """A long in/not_in list of JSON objects/arrays used to be an OR chain deep enough to raise
    RecursionError while rendering."""
    object_row = await JSONFields.objects.create(data={"o": {"k": 1}})
    array_row = await JSONFields.objects.create(data={"o": [1, 2]})
    null_row = await JSONFields.objects.create(data={"o": None})
    many_objects = [*({"k": index} for index in range(1, 40000)), [1, 2]]

    assert await get_ids_by_data_filter(o__in=many_objects) == [object_row.id, array_row.id]
    assert await get_ids_by_data_filter(o__not_in=many_objects) == [null_row.id]
    assert await get_ids_by_data_filter(o__in=[{"k": 1}, None]) == [object_row.id, null_row.id]


async def create_json_path_rows() -> dict[str, int]:
    rows = {
        "one": {"a": 1, "s": "abc", "o": {"k": 9}, "b": True, "n": None},
        "ten": {"a": 10, "s": "10", "o": [1], "b": False},
        "ten_text": {"a": "10", "s": "Abe"},
        "fraction": {"a": 2.5},
    }
    return {name: (await JSONFields.objects.create(data=data)).id for name, data in rows.items()}


@pytest.mark.asyncio
async def test_json_f_path_reads_the_parsed_json_value(db):
    """F("data__key") is the key's JSON value - a nested object/array parsed, a boolean a bool,
    a number a number (it used to be the text form on Postgres)."""
    await create_json_path_rows()

    def picked(path):
        return JSONFields.objects.annotate(picked=F(path)).order_by("id").values_list("picked", flat=True)

    assert await picked("data__a") == [1, 10, "10", 2.5]
    assert await picked("data__o") == [{"k": 9}, [1], None, None]
    assert await picked("data__b") == [True, False, None, None]
    assert await picked("data__s") == ["abc", "10", "Abe", None]
    assert await picked("data__n") == [None, None, None, None]


@pytest.mark.asyncio
async def test_json_f_path_filters_compare_json_values(db):
    """A filter on F("data__key") compares JSON values - the string "10" and the number 10
    differ (Postgres used to match both)."""
    ids = await create_json_path_rows()

    async def matching(**filters):
        return sorted(
            await JSONFields.objects.annotate(picked=F(f"data__{filters.pop('path')}"))
            .filter(**filters)
            .values_list("id", flat=True)
        )

    assert await matching(path="a", picked=10) == [ids["ten"]]
    assert await matching(path="a", picked="10") == [ids["ten_text"]]
    assert await matching(path="a", picked__not=1) == sorted([ids["ten"], ids["ten_text"], ids["fraction"]])
    assert await matching(path="a", picked__in=[1, 2.5, "10"]) == sorted(
        [ids["one"], ids["ten_text"], ids["fraction"]]
    )
    assert await matching(path="a", picked__in=list(range(3, 40000))) == [ids["ten"]]
    assert await matching(path="b", picked=True) == [ids["one"]]
    assert await matching(path="o", picked={"k": 9}) == [ids["one"]]
    assert await matching(path="o", picked__isnull=True) == sorted([ids["ten_text"], ids["fraction"]])
    assert await matching(path="s", picked__startswith="a") == [ids["one"]]
    assert await matching(path="s", picked__icontains="B") == sorted([ids["one"], ids["ten_text"]])


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_json_f_path_orders_and_compares_as_jsonb(db):
    """On Postgres the comparison and ordering follow jsonb: numbers numerically (it used to be
    text order), a string never greater than a number."""
    ids = await create_json_path_rows()

    def picked_ids(**filters):
        return (
            JSONFields.objects.annotate(picked=F("data__a"))
            .filter(**filters)
            .order_by("id")
            .values_list("id", flat=True)
        )

    ordered = await JSONFields.objects.annotate(picked=F("data__a")).order_by("picked").values_list("id", flat=True)

    assert ordered == [ids["ten_text"], ids["one"], ids["fraction"], ids["ten"]]
    assert await picked_ids(picked__gt=5) == [ids["ten"]]
    assert await picked_ids(picked__gte=2.5) == [ids["ten"], ids["fraction"]]
    assert await picked_ids(picked__range=(1, 5)) == [ids["one"], ids["fraction"]]


@pytest.mark.asyncio
async def test_json_f_path_none_is_json_null_and_isnull_is_a_missing_path(db):
    """As Django's key transforms: `v=None` (and a None inside `__in`) matches a JSON null,
    `v__isnull=True` a missing path - a JSON null isn't missing."""
    json_null_row = await JSONFields.objects.create(data={"a": None})
    number_row = await JSONFields.objects.create(data={"a": 1})
    missing_row = await JSONFields.objects.create(data={"z": 1})

    def matching(**filters):
        return (
            JSONFields.objects.annotate(picked=F("data__a"))
            .filter(**filters)
            .order_by("id")
            .values_list("id", flat=True)
        )

    assert await matching(picked=None) == [json_null_row.id]
    assert await matching(picked__not=None) == [number_row.id, missing_row.id]
    assert await JSONFields.objects.annotate(picked=F("data__a")).exclude(picked=None).order_by("id").values_list(
        "id", flat=True
    ) == [number_row.id, missing_row.id]
    assert await matching(picked__isnull=True) == [missing_row.id]
    assert await matching(picked__isnull=False) == [json_null_row.id, number_row.id]
    assert await matching(picked__in=[None]) == [json_null_row.id]
    assert await matching(picked__in=[1, None]) == [json_null_row.id, number_row.id]
    assert await matching(picked__not_in=[1, None]) == [missing_row.id]
    assert await matching(picked__not_in=[1]) == [json_null_row.id, missing_row.id]
    assert await matching(picked__in=[*range(2, 40000), None]) == [json_null_row.id]


@pytest.mark.asyncio
async def test_json_filter_datetime_forms_offsets_and_rounding(db):
    """Every ISO offset spelling, fractional seconds rounded to microseconds (carrying into the next
    day or year), and a stored text that isn't a real date - the same rows on every backend."""
    stamps = [
        "2024-01-01T10:00:00+0530",
        "2024-01-01T10:00:00+05",
        "2024-01-01T04:30:00Z",
        "2024-01-01 04:30:00",
        "2024-01-01T04:29:59.9999996",
        "2024-01-01T04:29:59.9999994",
        "2024-01-01",
        "2023-12-31T23:59:59.9999999",
        "9999-12-31T23:59:59.9999999",
        "0001-01-01T00:00:00+14:00",
        "2024-02-30",
        "2024-13-01",
    ]
    ids = [(await JSONFields.objects.create(data={"t": stamp})).id for stamp in stamps]

    def matching(**lookup):
        return get_ids_by_data_filter(**lookup)

    def rows(*positions):
        return sorted(ids[position - 1] for position in positions)

    assert await matching(t=datetime(2024, 1, 1, 4, 30, tzinfo=UTC)) == rows(1, 3, 4, 5)
    assert await matching(t__gte=datetime(2024, 1, 1, 4, 30)) == rows(1, 2, 3, 4, 5, 9)
    assert await matching(t__lt=datetime(2024, 1, 1, 4, 30, tzinfo=UTC)) == rows(6, 7, 8, 10)
    assert await matching(t=datetime(2024, 1, 1, 4, 30)) == rows(3, 4, 5)
    assert await matching(t=date(2024, 1, 1)) == rows(7, 8)
    assert await matching(t__year=2024) == rows(1, 2, 3, 4, 5, 6, 7, 8)
    assert await matching(t__hour=10) == rows(1, 2)
    assert await matching(t__day=1) == rows(1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
    assert await matching(t__second=59) == rows(6)
    assert await matching(t__in=[datetime(2024, 1, 1, 10, 0), datetime(2024, 1, 1)]) == rows(1, 2, 7, 8)
    upper = datetime(2024, 1, 1, 4, 30, tzinfo=timezone(timedelta(hours=-1)))
    assert await matching(t__range=[date(2023, 12, 31), upper]) == rows(1, 2, 3, 4, 5, 6, 7, 8)
    assert await matching(t__lt=datetime(1, 1, 2, tzinfo=UTC)) == rows(10)


@pytest.mark.asyncio
async def test_json_filter_text_lookups_take_a_string(db):
    await JSONFields.objects.create(data={"a": 12, "s": "x2y"})

    assert await get_ids_by_data_filter(s__contains="2") != []
    with pytest.raises(QueryError, match="contains expects a string"):
        await get_ids_by_data_filter(a__contains=2)
    with pytest.raises(QueryError, match="can.t apply istartswith to a JSON object/array value"):
        await get_ids_by_data_filter(s__istartswith=["x"])
    with pytest.raises(QueryError, match="istartswith expects a string"):
        await get_ids_by_data_filter(s__istartswith=True)
    with pytest.raises(UnSupportedError, match="use isnull"):
        await get_ids_by_data_filter(s__icontains=None)
    with pytest.raises(QueryError, match="can't apply gt to a JSON object/array value"):
        await get_ids_by_data_filter(a__gt=[1])


@pytest.mark.asyncio
async def test_json_path_compares_an_integer_wider_than_64_bits(db):
    big = await JSONFields.objects.create(data={"a": 10**20, "l": [10**20]})
    await JSONFields.objects.create(data={"a": 1})

    def picked(path, **filters):
        return JSONFields.objects.annotate(picked=F(path)).filter(**filters).values_list("id", flat=True)

    assert await picked("data__a", picked=10**20) == [big.id]
    assert await picked("data__l__0", picked__in=[10**20, 5]) == [big.id]
    assert await picked("data__a", picked__gt=10**19) == [big.id]
    assert await get_ids_by_data_filter(a=10**20) == [big.id]
    assert await get_ids_by_data_filter(l__0__gte=10**20) == [big.id]


@pytest.mark.asyncio
async def test_json_path_annotation_takes_the_whole_field_container_lookups(db):
    """`F("data__b")` is a JSON value: has_key/has_keys/has_any_keys/contained_by/filter test the
    object or array at the path, the way they test a whole JSONField."""
    object_row = await JSONFields.objects.create(data={"b": {"c": 1, "d": [1, 2]}})
    array_row = await JSONFields.objects.create(data={"b": ["c", "x"]})
    string_row = await JSONFields.objects.create(data={"b": "c"})
    await JSONFields.objects.create(data={"z": 1})

    def matching(**filters):
        return (
            JSONFields.objects.annotate(picked=F("data__b"))
            .filter(**filters)
            .order_by("id")
            .values_list("id", flat=True)
        )

    assert await matching(picked__has_key="c") == [object_row.id, array_row.id, string_row.id]
    assert await matching(picked__has_keys=["c", "d"]) == [object_row.id]
    assert await matching(picked__has_any_keys=["x"]) == [array_row.id]
    assert await matching(picked__contained_by={"c": 1, "d": [1, 2], "e": 0}) == [object_row.id]
    assert await matching(picked__filter={"d__1": 2}) == [object_row.id]
    with pytest.raises(QueryError, match="text lookup of a JSON path"):
        await matching(picked__contains={"c": 1})
