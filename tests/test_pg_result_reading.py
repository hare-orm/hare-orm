"""The rows of a rust.native.pg result read straight from the values the driver read (a PgResult)
against the same reader on the result's PgRow objects - every codec gives equal values of the same
type both ways - and a PgResult read as a sequence of rows."""

import datetime
import uuid
from decimal import Decimal

import pytest

from tests.testmodels import (
    BooleanFields,
    Currency,
    DateFields,
    DatetimeFields,
    DecimalFields,
    EnumFields,
    HighPrecisionDecimalFields,
    JSONFields,
    Service,
    TimeDeltaFields,
    TimeFields,
    UUIDFields,
)
from tests.utils.pg_result_reading import PgResultReading
from tests.utils.timezone_context import override_timezone

MOMENT = datetime.datetime(2024, 3, 10, 6, 59, 59, 999999, tzinfo=datetime.UTC)


@pytest.mark.asyncio
async def test_decimals(db):
    await PgResultReading.assert_result_read_as_rows(
        DecimalFields,
        [
            {"decimal": Decimal("1234.5678"), "decimal_nodec": Decimal("42"), "decimal_null": Decimal("0.0001")},
            {"decimal": Decimal("-1234.5678"), "decimal_nodec": Decimal("-7"), "decimal_null": None},
            {"decimal": Decimal("0"), "decimal_nodec": Decimal("0"), "decimal_null": Decimal("-0.0001")},
            {"decimal": Decimal("99999999999999.9999"), "decimal_nodec": Decimal("1"), "decimal_null": Decimal("5")},
        ],
    )


@pytest.mark.asyncio
async def test_decimals_past_the_default_precision(db):
    await PgResultReading.assert_result_read_as_rows(
        HighPrecisionDecimalFields,
        [
            {"big": Decimal("12345678901234567890.123456789012345678")},
            {"big": Decimal("-0.000000000000000001")},
            {"big": Decimal("0")},
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("use_timezone", "zone"), [(True, "UTC"), (True, "America/New_York"), (True, "Asia/Kolkata"), (False, "UTC")]
)
async def test_datetimes_in_every_zone_setting(db, use_timezone, zone):
    with override_timezone(use_timezone, zone):
        await PgResultReading.assert_result_read_as_rows(
            DatetimeFields,
            [
                {"datetime": MOMENT, "datetime_null": MOMENT + datetime.timedelta(hours=1)},
                {"datetime": MOMENT - datetime.timedelta(days=200), "datetime_null": None},
            ],
        )


@pytest.mark.asyncio
async def test_other_types(db):
    await PgResultReading.assert_result_read_as_rows(
        BooleanFields, [{"boolean": True, "boolean_null": False}, {"boolean": False, "boolean_null": None}]
    )
    await PgResultReading.assert_result_read_as_rows(
        UUIDFields, [{"data": uuid.uuid4(), "data_null": uuid.uuid4()}, {"data": uuid.uuid4(), "data_null": None}]
    )
    await PgResultReading.assert_result_read_as_rows(
        JSONFields,
        [{"data": {"a": [1, 2.5, "x", None, True]}, "data_null": [1, 2]}, {"data": [], "data_null": None}],
    )
    await PgResultReading.assert_result_read_as_rows(
        DateFields, [{"date": datetime.date(2024, 2, 29), "date_null": None}, {"date": datetime.date(1, 1, 1)}]
    )
    await PgResultReading.assert_result_read_as_rows(
        TimeFields, [{"time": datetime.time(23, 59, 59, 999999), "time_null": None}, {"time": datetime.time(0)}]
    )
    await PgResultReading.assert_result_read_as_rows(
        TimeDeltaFields,
        [{"timedelta": datetime.timedelta(days=-3, microseconds=7), "timedelta_null": None}],
    )
    await PgResultReading.assert_result_read_as_rows(
        EnumFields,
        [{"service": Service.database_design, "currency": Currency.EUR}, {"service": Service.python_programming}],
    )


@pytest.mark.asyncio
async def test_result_reads_as_a_sequence_of_rows(db):
    await BooleanFields.objects.bulk_create([BooleanFields(boolean=index % 2 == 0) for index in range(3)])
    rows, _reader, _names = await PgResultReading.fetch_result(BooleanFields)
    assert len(rows) == 3
    assert bool(rows)
    assert rows[0] is rows[0]
    assert rows[-1] is rows[2]
    assert rows[1:] == [rows[1], rows[2]]
    assert rows[::-1] == [rows[2], rows[1], rows[0]]
    assert list(rows) == [rows[0], rows[1], rows[2]]
    assert rows == list(rows)
    assert list(rows[0].keys()) == ["id", "boolean", "boolean_null"]
    assert repr(rows).startswith("PgResult([")
    with pytest.raises(IndexError):
        rows[3]
    with pytest.raises(TypeError):
        rows["id"]
