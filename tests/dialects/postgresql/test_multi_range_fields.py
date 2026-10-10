"""Multirange fields: values written as lists of ranges and read back merged the way PostgreSQL stores
them, the range lookups against a multirange or one range, bounds, flags and the span as paths,
RangeAgg - and the values and databases they refuse."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.enums import DialectName
from hare.dialects.postgresql.fields.multiranges import (
    DateMultiRangeField,
    DecimalMultiRangeField,
    IntMultiRangeField,
    MultiRangeField,
)
from hare.dialects.postgresql.fields.ranges import DateRangeField, Range
from hare.dialects.postgresql.functions.aggregates import RangeAgg
from hare.exceptions import UnSupportedError, ValidationError
from hare.inspectdb.generation.column_type_mapper import ColumnTypeMapper
from hare.inspectdb.introspection.column_info import ColumnInfo
from tests.dialects.postgresql.models_multi_range import RoomBooking, RoomSchedule

JAN = datetime.date(2024, 1, 1)


def day(number: int) -> datetime.date:
    return JAN + datetime.timedelta(days=number - 1)


async def create_schedules() -> None:
    await RoomSchedule.objects.create(
        id=1,
        building="north",
        busy_days=[(day(1), day(5)), Range(day(10), day(12), upper_inc=True)],
        seats=[(1, 10)],
        prices=[(Decimal("1.5"), Decimal("3")), (Decimal("3"), Decimal("4"))],
    )
    await RoomSchedule.objects.create(id=2, building="south", busy_days=[(day(20), day(25))], seats=[(5, 7), (8, 9)])
    await RoomSchedule.objects.create(id=3, building="south", busy_days=[], seats=None)


@pytest.mark.asyncio
async def test_values_are_read_merged_as_postgres_stores_them(db_multi_range):
    await create_schedules()
    schedules = {schedule.id: schedule for schedule in await RoomSchedule.objects.all()}
    assert schedules[1].busy_days == [Range(day(1), day(5)), Range(day(10), day(13))]
    assert schedules[1].prices == [Range(Decimal("1.5"), Decimal("4"))]
    assert schedules[2].seats == [Range(5, 7), Range(8, 9)]
    assert schedules[3].busy_days == []
    assert schedules[3].seats is None
    assert await RoomSchedule.objects.filter(id=1).values_list("seats", flat=True) == [[Range(1, 10)]]


@pytest.mark.asyncio
async def test_memory_holds_what_a_read_returns(db_multi_range):
    schedule = RoomSchedule(id=9, building="east", seats=[(8, 9), (1, 3), (3, 5), Range(4, 4)])
    assert schedule.seats == [Range(1, 5), Range(8, 9)]
    await schedule.save()
    await schedule.refresh_from_db()
    assert schedule.seats == [Range(1, 5), Range(8, 9)]


@pytest.mark.asyncio
async def test_timestamp_multiranges_keep_their_zone(db_multi_range):
    start = datetime.datetime(2024, 1, 1, 9, tzinfo=datetime.UTC)
    hour = datetime.timedelta(hours=1)
    await RoomSchedule.objects.create(
        id=4, building="west", busy_times=[(start, start + hour), (start + 2 * hour, None)]
    )
    schedule = await RoomSchedule.objects.get(id=4)
    assert schedule.busy_times == [Range(start, start + hour), Range(start + 2 * hour, None)]
    assert await RoomSchedule.objects.filter(busy_times__contains=start + 3 * hour).count() == 1


@pytest.mark.asyncio
async def test_lookups(db_multi_range):
    await create_schedules()

    async def get_ids(**kwargs) -> list[int]:
        return list(await RoomSchedule.objects.filter(**kwargs).order_by("id").values_list("id", flat=True))

    assert await get_ids(seats=[(1, 10)]) == [1]
    assert await get_ids(seats=(1, 10)) == [1]
    assert await get_ids(seats__not=[(1, 10)]) == [2, 3]
    assert await get_ids(seats__contains=6) == [1, 2]
    assert await get_ids(seats__contains=(5, 7)) == [1, 2]
    assert await get_ids(seats__contains=[(2, 3), (8, 9)]) == [1]
    assert await get_ids(busy_days__contains=day(11)) == [1]
    assert await get_ids(busy_days__contains="2024-01-21") == [2]
    assert await get_ids(seats__contained_by=(0, 20)) == [1, 2]
    assert await get_ids(busy_days__overlap=[(day(4), day(6)), (day(30), day(31))]) == [1]
    assert await get_ids(busy_days__fully_lt=(day(15), day(16))) == [1]
    assert await get_ids(busy_days__fully_gt=(day(15), day(16))) == [2]
    assert await get_ids(seats__not_gt=(0, 9)) == [2]
    assert await get_ids(seats__not_lt=(5, 6)) == [2]
    assert await get_ids(seats__adjacent_to=(9, 12)) == [2]
    assert await get_ids(seats__isnull=True) == [3]


@pytest.mark.asyncio
async def test_bounds_flags_and_span_as_paths(db_multi_range):
    await create_schedules()
    rows = (
        await RoomSchedule.objects.filter(id__in=[1, 2])
        .order_by("id")
        .values_list("busy_days__startswith", "busy_days__endswith", "busy_days__span", "seats__upper_inc")
    )
    assert rows == [
        (day(1), day(13), Range(day(1), day(13)), False),
        (day(20), day(25), Range(day(20), day(25)), False),
    ]
    assert await RoomSchedule.objects.filter(busy_days__isempty=True).values_list("id", flat=True) == [3]
    assert await RoomSchedule.objects.filter(busy_days__startswith__gte=day(10)).values_list("id", flat=True) == [2]


@pytest.mark.asyncio
async def test_range_agg_merges_a_group_of_ranges(db_multi_range):
    await RoomBooking.objects.create(id=1, building="north", during=(day(1), day(3)))
    await RoomBooking.objects.create(id=2, building="north", during=(day(3), day(6)))
    await RoomBooking.objects.create(id=3, building="north", during=(day(9), day(10)))
    await RoomBooking.objects.create(id=4, building="south", during=(day(2), day(4)))
    busy = await RoomBooking.objects.values("building").annotate(busy=RangeAgg("during")).order_by("building")
    assert busy == [
        {"building": "north", "busy": [Range(day(1), day(6)), Range(day(9), day(10))]},
        {"building": "south", "busy": [Range(day(2), day(4))]},
    ]


@pytest.mark.asyncio
async def test_queryset_update(db_multi_range):
    await create_schedules()
    assert await RoomSchedule.objects.filter(id=3).update(seats=[(3, 4), (1, 2)]) == 1
    schedule = await RoomSchedule.objects.get(id=3)
    assert schedule.seats == [Range(1, 2), Range(3, 4)]


def test_merging_follows_bound_inclusion():
    field = DecimalMultiRangeField()
    field.model_field_name = "value"
    one, two, three = Decimal(1), Decimal(2), Decimal(3)
    # Touching at an excluded point on both sides keeps the gap; included on either side closes it.
    assert field.to_python([Range(one, two), Range(two, three, lower_inc=False)]) == [
        Range(one, two),
        Range(two, three, lower_inc=False),
    ]
    assert field.to_python([Range(one, two, upper_inc=True), Range(two, three, lower_inc=False)]) == [
        Range(one, three)
    ]
    assert field.to_python([Range(None, one), Range(two, None), Range(one, two)]) == [
        Range(None, None, lower_inc=False)
    ]
    assert field.to_python([Range(one, three, upper_inc=True), Range(two, three)]) == [
        Range(one, three, upper_inc=True)
    ]
    assert field.to_python("{[1,2),[5,6]}") == [Range(one, two), Range(Decimal(5), Decimal(6), upper_inc=True)]
    assert field.to_python("{}") == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (IntMultiRangeField(), (1, 5)),
        (IntMultiRangeField(), Range(1, 5)),
        (IntMultiRangeField(), 5),
        (IntMultiRangeField(), [5]),
        (IntMultiRangeField(), [(5, 1)]),
        (IntMultiRangeField(), [("a", 3)]),
        (DateMultiRangeField(), [(day(1), "not a date")]),
        (IntMultiRangeField(), "[1,5)"),
    ],
)
def test_a_wrong_value_is_refused(field, value):
    field.model_field_name = "value"
    with pytest.raises(ValidationError, match=r"^value"):
        field.to_db_value(value, None)


def test_a_range_field_without_a_multirange_type_is_refused():
    assert isinstance(MultiRangeField.get_field_for_range_field(DateRangeField()), DateMultiRangeField)
    with pytest.raises(ValidationError, match="has no multirange type"):
        MultiRangeField.get_field_for_range_field(IntMultiRangeField())


@pytest.mark.parametrize(
    ("db_type", "field_name"),
    [
        ("int4multirange", "IntMultiRangeField"),
        ("int8multirange", "BigIntMultiRangeField"),
        ("nummultirange", "DecimalMultiRangeField"),
        ("datemultirange", "DateMultiRangeField"),
        ("tstzmultirange", "DateTimeMultiRangeField"),
    ],
)
def test_inspectdb_maps_multirange_columns(db_type, field_name):
    column = ColumnInfo(name="busy", db_type=db_type, nullable=True, is_pk=False, is_unique=False)
    field_path, _, is_ambiguous = ColumnTypeMapper.map_column_type(DialectName.POSTGRESQL, column)
    assert (field_path, is_ambiguous) == (f"hare.dialects.postgresql.fields.multiranges.{field_name}", False)


def test_a_database_without_the_types_refuses_the_fields():
    sqlite_dialect = DialectRegistry.get_dialect("sqlite")
    with pytest.raises(UnSupportedError):
        IntMultiRangeField().get_column_type(sqlite_dialect)
