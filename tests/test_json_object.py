"""JSONObject, and paths into a JSON field or annotation in values()/values_list()/order_by()/F()."""

import datetime
import os
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from hare.contrib.test.helpers import hare_test_context, truncate_all_models
from hare.exceptions import FieldError, QueryError
from hare.query.expressions import F, Q, Value
from hare.query.functions import Count, JSONObject, Sum, Upper
from tests.typed_row_models import TypedRow, TypedRowNote


def get_test_db_url() -> str:
    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:").replace("\\{", "{").replace("\\}", "}")
    return raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url


@pytest_asyncio.fixture(scope="module")
async def json_object_context() -> AsyncGenerator[Any]:
    async with hare_test_context(["tests.typed_row_models"], db_url=get_test_db_url(), use_tz=True) as ctx:
        yield ctx


@pytest_asyncio.fixture
async def rows(json_object_context: Any) -> AsyncGenerator[list[TypedRow]]:
    created = [
        await TypedRow.objects.create(
            id=1,
            number=3,
            big_number=2**40,
            ratio=1.5,
            amount=Decimal("1.20"),
            name="a\"b\\c'",
            body="é\n\t",
            flag=True,
            day=datetime.date(2020, 1, 2),
            at=datetime.datetime(2020, 1, 2, 3, 4, 5, 500000, tzinfo=datetime.UTC),
            clock=datetime.time(12, 30, 0, 250),
            span=datetime.timedelta(days=1, seconds=5),
            uid=uuid.UUID(int=1),
            payload=b"\x01\xff",
            data={"k": [1, None, "x"], "n": {"m": True}},
        ),
        await TypedRow.objects.create(
            id=2,
            number=-7,
            ratio=1e20,
            amount=Decimal("-0.05"),
            name="z",
            flag=False,
            at=datetime.datetime(2020, 1, 2, tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))),
            clock=datetime.time(8, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))),
            data={"k": 2, "n": {"m": False}},
        ),
        await TypedRow.objects.create(id=3, ratio=2.0, amount=Decimal("100"), data="text"),
        await TypedRow.objects.create(id=4, number=4, ratio=0.1 + 0.2, name="z", data={"k": 2}),
        await TypedRow.objects.create(id=5, ratio=1e-7, data=12.5),
    ]
    yield created
    await truncate_all_models()


COLUMNS = (
    "number",
    "big_number",
    "ratio",
    "amount",
    "name",
    "body",
    "flag",
    "day",
    "at",
    "clock",
    "span",
    "uid",
    "payload",
    "data",
)


async def get_objects(expression: Any) -> list[Any]:
    return await TypedRow.objects.annotate(value=expression).order_by("id").values_list("value", flat=True)


@pytest.mark.asyncio
async def test_json_object_writes_every_type_as_postgres_jsonb_holds_it(rows):
    objects = await get_objects(JSONObject(**{column: column for column in COLUMNS}))
    assert objects[0] == {
        "number": 3,
        "big_number": 2**40,
        "ratio": 1.5,
        "amount": 1.2,
        "name": "a\"b\\c'",
        "body": "é\n\t",
        "flag": True,
        "day": "2020-01-02",
        "at": "2020-01-02T03:04:05.5+00:00",
        "clock": "12:30:00.00025+00",
        "span": 86405000000,
        "uid": "00000000-0000-0000-0000-000000000001",
        "payload": "\\x01ff",
        "data": {"k": [1, None, "x"], "n": {"m": True}},
    }
    assert objects[1]["flag"] is False
    assert objects[1]["at"] == "2020-01-01T18:30:00+00:00"
    assert objects[1]["clock"] == "08:00:00+05:30"
    assert objects[1]["amount"] == -0.05
    assert objects[4]["body"] is None
    assert objects[2]["data"] == "text"
    assert objects[4]["data"] == 12.5


@pytest.mark.asyncio
async def test_json_object_writes_a_float_as_a_positional_number(rows):
    ratios = [value["ratio"] for value in await get_objects(JSONObject(ratio="ratio"))]
    assert ratios == [1.5, 10**20, 2, 0.30000000000000004, 1e-07]
    assert [type(ratio) for ratio in ratios] == [float, int, int, float, float]


@pytest.mark.asyncio
async def test_json_object_literals_expressions_and_nesting(rows):
    [value] = (
        await TypedRow.objects.filter(id=1)
        .annotate(
            value=JSONObject(
                text=Value("x'y"),
                integer=Value(5),
                number=Value(2.5),
                flag=Value(True),
                nothing=Value(None),
                amount=Value(Decimal("1.50")),
                day=Value(datetime.date(2021, 3, 4)),
                at=Value(datetime.datetime(2021, 3, 4, 5, 6, 7, tzinfo=datetime.UTC)),
                raw_integer=7,
                raw_number=0.5,
                document=Value({"a": [1]}),
                payload=Value(b"\x00\x10"),
                nested=JSONObject(upper=Upper("name"), doubled=F("number") * 2, empty=JSONObject()),
                computed=F("amount") * 2,
            )
        )
        .values_list("value", flat=True)
    )
    assert value == {
        "text": "x'y",
        "integer": 5,
        "number": 2.5,
        "flag": True,
        "nothing": None,
        "amount": 1.5,
        "day": "2021-03-04",
        "at": "2021-03-04T05:06:07+00:00",
        "raw_integer": 7,
        "raw_number": 0.5,
        "document": {"a": [1]},
        "payload": "\\x0010",
        "nested": {"upper": "A\"B\\C'", "doubled": 6, "empty": {}},
        "computed": 2.4,
    }


@pytest.mark.asyncio
async def test_json_object_keys_are_written_as_given(rows):
    [value] = (
        await TypedRow.objects.filter(id=1)
        .annotate(value=JSONObject(**{"quote \"'\\": "id", "": "id", "é": "id"}))
        .values_list("value", flat=True)
    )
    assert value == {"quote \"'\\": 1, "": 1, "é": 1}


@pytest.mark.asyncio
async def test_json_object_empty(rows):
    assert await TypedRow.objects.filter(id=1).annotate(value=JSONObject()).values_list("value", flat=True) == [{}]


def test_json_object_rejects_a_null_byte_key():
    with pytest.raises(QueryError, match="null byte"):
        JSONObject(**{"a\x00": "id"})


@pytest.mark.asyncio
async def test_json_object_of_aggregates(rows):
    grouped = (
        await TypedRow.objects.annotate(value=JSONObject(total=Sum("number"), rows=Count("id")))
        .group_by("name")
        .order_by(F("name").asc(nulls_first=True))
        .values_list("name", "value")
    )
    assert grouped == [
        (None, {"total": None, "rows": 2}),
        ("a\"b\\c'", {"total": 3, "rows": 1}),
        ("z", {"total": -3, "rows": 2}),
    ]


@pytest.mark.asyncio
async def test_json_object_decodes_into_model_instances(rows):
    row = await TypedRow.objects.annotate(value=JSONObject(name="name", number="number")).get(id=4)
    assert row.value == {"name": "z", "number": 4}


@pytest.mark.asyncio
async def test_filter_on_json_object_annotation(rows):
    queryset = TypedRow.objects.annotate(value=JSONObject(number="number", name="name"))
    assert await queryset.filter(value__filter={"number__gt": 3}).values_list("id", flat=True) == [4]
    assert sorted(await queryset.filter(value__contains={"name": "z"}).values_list("id", flat=True)) == [2, 4]
    assert sorted(await queryset.filter(value__has_key="name").values_list("id", flat=True)) == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_f_path_into_json_annotation(rows):
    queryset = TypedRow.objects.annotate(value=JSONObject(number="number", name="name"), picked=F("value__number"))
    assert await queryset.filter(picked__gt=3).values_list("id", flat=True) == [4]
    assert await queryset.order_by("id").values_list("picked", flat=True) == [3, -7, None, 4, None]
    nested = TypedRow.objects.annotate(document=F("data"), deep=F("document__n__m"))
    assert await nested.filter(deep=True).values_list("id", flat=True) == [1]


@pytest.mark.asyncio
async def test_f_path_into_non_json_annotation_raises(rows):
    with pytest.raises(FieldError, match="isn't a JSON value"):
        await TypedRow.objects.annotate(copied=F("number"), picked=F("copied__key")).values_list("picked")


@pytest.mark.asyncio
async def test_values_read_paths_into_json_field(rows):
    assert await TypedRow.objects.all().order_by("id").values_list("data__k", flat=True) == [
        [1, None, "x"],
        2,
        None,
        2,
        None,
    ]
    assert await TypedRow.objects.filter(id=1).values_list("data__n__m", "data__k__0", "data__k__-1").get() == (
        True,
        1,
        "x",
    )
    assert await TypedRow.objects.filter(id__in=[1, 2]).order_by("id").values("data__n__m", inner="data__n") == [
        {"data__n__m": True, "inner": {"m": True}},
        {"data__n__m": False, "inner": {"m": False}},
    ]


@pytest.mark.asyncio
async def test_values_read_paths_into_json_annotation(rows):
    queryset = TypedRow.objects.annotate(value=JSONObject(number="number", name="name")).order_by("id")
    assert await queryset.values_list("value__number", flat=True) == [3, -7, None, 4, None]
    assert (await queryset.values("value__name"))[3] == {"value__name": "z"}


@pytest.mark.asyncio
async def test_values_group_by_json_path(rows):
    grouped = await TypedRow.objects.filter(id__in=[2, 4]).values("data__k").annotate(rows=Count("id"))
    assert grouped == [{"data__k": 2, "rows": 2}]


@pytest.mark.asyncio
async def test_order_by_json_paths(rows):
    by_field = TypedRow.objects.filter(id__in=[1, 2]).order_by("-data__n__m")
    assert await by_field.values_list("id", flat=True) == [1, 2]
    assert [row.id for row in await by_field] == [1, 2]
    missing_last = TypedRow.objects.filter(id__in=[1, 2, 4]).order_by(F("data__n__m").asc(nulls_last=True))
    assert await missing_last.values_list("id", flat=True) == [2, 1, 4]
    by_annotation = TypedRow.objects.annotate(value=JSONObject(number="number")).filter(number__isnull=False)
    assert await by_annotation.order_by("-value__number").values_list("id", flat=True) == [4, 1, 2]
    assert await by_annotation.order_by(F("value__number").asc()).values_list("id", flat=True) == [2, 1, 4]


@pytest.mark.asyncio
async def test_json_path_comparisons_follow_jsonb_order(json_object_context):
    values = [1, 5, None, "abc", True, [7], [], {"a": 1}]
    for row_id, value in enumerate(values, 10):
        await TypedRow.objects.create(id=row_id, data={"k": value})
    await TypedRow.objects.create(id=30, data={})
    queryset = TypedRow.objects.annotate(picked=F("data__k"))

    async def get_ids(**lookup: Any) -> list[int]:
        return sorted(await queryset.filter(**lookup).values_list("id", flat=True))

    try:
        # Object > array > boolean > number > string > null; the empty top-level array sorts below null.
        assert await get_ids(picked__gt=3) == [11, 14, 15, 17]
        assert await get_ids(picked__lt=3) == [10, 12, 13, 16]
        assert await get_ids(picked__gte=True) == [14, 15, 17]
        assert await get_ids(picked__gt="a") == [10, 11, 13, 14, 15, 17]
        assert await get_ids(picked__lte=None) == [12, 16]
        assert await get_ids(picked__gt=[]) == [10, 11, 12, 13, 14, 15, 17]
        assert await get_ids(picked__range=(1, 5)) == [10, 11]
        assert await get_ids(picked__lt={"a": 2}) == [10, 11, 12, 13, 14, 15, 16, 17]
    finally:
        await truncate_all_models()


@pytest.mark.asyncio
async def test_json_ordering_follows_jsonb_order(json_object_context):
    values = [1, 5, None, "abc", True, [7], [], {"a": 1}, 2.5, False, [1, 2]]
    for row_id, value in enumerate(values, 10):
        await TypedRow.objects.create(id=row_id, data={"k": value})
    await TypedRow.objects.create(id=30, data={})
    try:
        # At the top level the empty array sorts below null, a scalar below an array.
        by_path = TypedRow.objects.all().order_by(F("data__k").asc(nulls_last=True))
        assert await by_path.values_list("id", flat=True) == [16, 12, 13, 10, 18, 11, 19, 14, 15, 20, 17, 30]
        assert [row.id for row in await by_path] == [16, 12, 13, 10, 18, 11, 19, 14, 15, 20, 17, 30]
        distinct_values = await by_path.distinct().values_list("data__k", flat=True)
        assert distinct_values == [[], None, "abc", 1, 2.5, 5, False, True, [7], [1, 2], {"a": 1}, None]
        # Inside an object the empty array is an array like any other; the empty object sorts first.
        by_column = TypedRow.objects.all().order_by("data").values_list("id", flat=True)
        assert await by_column == [30, 12, 13, 10, 18, 11, 19, 14, 16, 15, 20, 17]
    finally:
        await truncate_all_models()


@pytest.mark.asyncio
async def test_values_path_into_non_json_field_raises(rows):
    with pytest.raises(FieldError):
        await TypedRow.objects.all().values_list("name__first")


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ["UTC", "Asia/Tokyo"])
async def test_json_object_naive_timestamp_is_its_wall_clock(zone):
    async with hare_test_context(["tests.typed_row_models"], db_url=get_test_db_url(), use_tz=False, timezone=zone):
        await TypedRow.objects.create(id=1, at=datetime.datetime(2020, 1, 2, 1, 4, 5, 250000))
        value = await TypedRow.objects.annotate(value=JSONObject(at="at")).values_list("value", flat=True)
        assert value == [{"at": "2020-01-02T01:04:05.25"}]


@pytest.mark.asyncio
async def test_json_path_through_a_relation(json_object_context):
    """``values()``/``order_by()``/``F()`` read a JSON path after a relation, as ``filter()`` does."""
    try:
        first = await TypedRow.objects.create(id=1, data={"owner": {"name": "b"}, "rank": 2})
        second = await TypedRow.objects.create(id=2, data={"owner": {"name": "a"}, "rank": 1})
        await TypedRowNote.objects.create(id=10, row=first)
        await TypedRowNote.objects.create(id=20, row=second)

        names = TypedRowNote.objects.all().order_by("id").values_list("row__data__owner__name", flat=True)
        assert await names == ["b", "a"]
        by_rank = TypedRowNote.objects.all().order_by("row__data__rank").values_list("id", flat=True)
        assert await by_rank == [20, 10]
        as_dicts = TypedRowNote.objects.all().order_by("id").values("id", owner="row__data__owner")
        assert await as_dicts == [{"id": 10, "owner": {"name": "b"}}, {"id": 20, "owner": {"name": "a"}}]
        annotated = (
            TypedRowNote.objects.annotate(rank=F("row__data__rank")).filter(rank=2).values_list("id", flat=True)
        )
        assert await annotated == [10]
        assert await TypedRowNote.objects.filter(row__data__owner__name="a").values_list("id", flat=True) == [20]
        assert await TypedRowNote.objects.filter(row__data__rank__gt=1).values_list("id", flat=True) == [10]
    finally:
        await truncate_all_models()


async def get_ids(queryset: Any) -> list[int]:
    return sorted(await queryset.values_list("id", flat=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("column", COLUMNS)
async def test_json_path_compares_with_its_typed_column(rows, column):
    """The value a JSONObject wrote from a column compares equal to the column itself, both ways."""
    queryset = TypedRow.objects.annotate(written=JSONObject(**{column: column}))
    expected = [row.id for row in rows if getattr(row, column) is not None]
    assert await get_ids(queryset.filter(**{f"written__{column}": F(column)})) == expected
    assert await get_ids(queryset.filter(**{f"written__{column}__gte": F(column)})) == expected
    assert await get_ids(queryset.filter(**{f"written__{column}__lt": F(column)})) == []
    assert await get_ids(queryset.filter(**{column: F(f"written__{column}")})) == expected
    assert await get_ids(queryset.filter(**{f"{column}__lte": F(f"written__{column}")})) == expected


@pytest.mark.asyncio
async def test_json_key_transforms_filter(rows):
    """``data__k=2``, ``data__n__m=True``, ``data__k__gt=1``: a path into a JSON field with its
    value's lookups, as Django's key transforms."""
    assert await get_ids(TypedRow.objects.filter(data__k=2)) == [2, 4]
    assert await get_ids(TypedRow.objects.filter(data__n__m=True)) == [1]
    assert await get_ids(TypedRow.objects.filter(data__n__m=False)) == [2]
    assert await get_ids(TypedRow.objects.filter(data__k__gt=1)) == [1, 2, 4]
    assert await get_ids(TypedRow.objects.filter(data__k__in=[2, "x"])) == [2, 4]
    assert await get_ids(TypedRow.objects.filter(data__k__0=1)) == [1]
    assert await get_ids(TypedRow.objects.filter(data__k__1=None)) == [1]
    assert await get_ids(TypedRow.objects.filter(**{"data__k__-1__startswith": "x"})) == [1]
    assert await get_ids(TypedRow.objects.filter(data__n__isnull=True)) == [3, 4, 5]
    assert await get_ids(TypedRow.objects.filter(data__n__has_key="m")) == [1, 2]
    assert await get_ids(TypedRow.objects.filter(data__n={"m": True})) == [1]
    assert await get_ids(TypedRow.objects.exclude(data__k=2)) == [1, 3, 5]
    assert await get_ids(TypedRow.objects.filter(Q(data__k=2) | Q(data__n__m=True))) == [1, 2, 4]
    assert await get_ids(TypedRow.objects.filter(data__k=F("number"))) == []
    assert await get_ids(TypedRow.objects.filter(data__k__lt=F("number"))) == [4]
    assert await get_ids(TypedRow.objects.filter(number__gt=F("data__k"))) == [4]
    # A lookup name right after the field is a lookup of the whole value, never a key.
    assert await get_ids(TypedRow.objects.filter(data__has_key="n")) == [1, 2]
    assert await get_ids(TypedRow.objects.filter(data__contains={"k": 2})) == [2, 4]
    assert await get_ids(TypedRow.objects.filter(data__gt=100)) == [1, 2, 4]
    assert await get_ids(TypedRow.objects.filter(data__lt=13)) == [3, 5]
    assert await get_ids(TypedRow.objects.filter(data__range=["a", 20])) == [3, 5]
    with pytest.raises(FieldError):
        await get_ids(TypedRow.objects.filter(data__startswith="t"))


@pytest.mark.asyncio
async def test_json_key_transforms_filter_json_annotation(rows):
    """A path into a JSON-valued annotation filters the same way - ``day`` is a key there, not a
    date part."""
    queryset = TypedRow.objects.annotate(summary=JSONObject(day="day", total="number", inner="data"))
    assert await get_ids(queryset.filter(summary__day="2020-01-02")) == [1]
    assert await get_ids(queryset.filter(summary__total__gte=0)) == [1, 4]
    assert await get_ids(queryset.filter(summary__inner__k=2)) == [2, 4]
    assert await get_ids(queryset.filter(summary__has_key="total")) == [1, 2, 3, 4, 5]
