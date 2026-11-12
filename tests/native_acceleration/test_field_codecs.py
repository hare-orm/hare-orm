"""Field conversion grids through bound readers and writers.

Model hydration and annotation columns retain values, errors and warnings of the field
contract across driver representations, time zones, NULLs and invalid input.
"""

from __future__ import annotations

import datetime
import math
import uuid
import warnings
from decimal import Decimal
from enum import IntEnum, StrEnum
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from hare import fields
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.types.type_registry import TypeRegistry
from hare.dialects.postgresql.client.postgresql_client import PostgresqlClient
from hare.dialects.postgresql.fields.ranges import (
    DateRangeField,
    DateTimeRangeField,
    DecimalRangeField,
    IntRangeField,
    Range,
)
from hare.dialects.postgresql.types.postgresql_types import PostgresqlTypes
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.dialects.sqlite.parameters.sqlite_parameter_adapters import SqliteParameterAdapters
from hare.dialects.sqlite.types.sqlite_types import SqliteTypes
from hare.fields import ArrayField
from hare.fields.validators import MaxValueValidator, MinLengthValidator, MinValueValidator, Validator
from hare.models.instances.dirty_fields import DirtyFields
from hare.models.write.instance_values import InstanceValues
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.native.native_modules import NativeModules
from hare.query.rows.model_rows.model_columns import ModelColumns
from hare.query.rows.native.hydration_layout import HydrationLayout
from hare.query.rows.row_column import RowColumn
from hare.query.rows.row_layout import RowLayout
from hare.time import Timezone
from tests.shared_models.testmodels import (
    BinaryFields,
    BooleanFields,
    CharFields,
    Currency,
    DateFields,
    DatetimeFields,
    EnumFields,
    FloatFields,
    IntFields,
    JSONFields,
    Service,
    TimeDeltaFields,
    TimeFields,
    UUIDFields,
)
from tests.shared_models.testmodels_postgres import DecimalFields
from tests.utils.timezone_context import override_timezone

pytestmark = pytest.mark.database_independent

SQLITE_TYPES = SqliteTypes.build()
POSTGRESQL_TYPES = PostgresqlTypes.build()
#: (name, type registry, the driver's native Python types, parameter adapters)
CONNECTIONS = [
    ("sqlite", SQLITE_TYPES, AiosqliteClient.native_python_types, SqliteParameterAdapters()),
    ("rust_pg", POSTGRESQL_TYPES, PostgresqlClient.native_python_types, None),
    ("generic", TypeRegistry(), DatabaseClient.native_python_types, None),
]
#: (use_timezone, zone)
ZONES = [(False, "UTC"), (True, "UTC"), (True, "Europe/Berlin"), (True, "America/Sao_Paulo"), (True, "Asia/Kolkata")]
UTC = datetime.UTC
BERLIN = ZoneInfo("Europe/Berlin")
PLUS_0530 = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
PLUS_0300 = datetime.timezone(datetime.timedelta(hours=3))


class NoOffset(datetime.tzinfo):
    """A tzinfo reporting no offset - its values are naive."""

    def utcoffset(self, dt: Any) -> None:
        return None

    def dst(self, dt: Any) -> None:
        return None

    def tzname(self, dt: Any) -> str:
        return "none"


class DriverUUID(uuid.UUID):
    """A driver's own UUID subclass."""


class OtherService(IntEnum):
    python_programming = 1
    unknown = 99


class OtherCurrency(StrEnum):
    EUR = "EUR"
    XXX = "XXX"


class MultipleOfThree(Validator):
    def __call__(self, value: Any) -> None:
        if value % 3:
            self._raise("not a multiple of three")


def run(function: Any, *args: Any) -> tuple[tuple[Any, ...], list[str]]:
    """The outcome of a call - its value, or its error's class, message and cause class - and the
    warnings it gave."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            outcome: tuple[Any, ...] = ("value", function(*args))
        except Exception as error:
            outcome = ("error", type(error), str(error), type(error.__cause__))
    return outcome, [str(warning.message) for warning in caught]


def is_same(left: Any, right: Any) -> bool:
    """Equal, of the same type, with the same tzinfo - recursively through dicts and lists."""
    if type(left) is not type(right):
        return False
    if isinstance(left, float) and math.isnan(left):
        return math.isnan(right)
    if isinstance(left, dict):
        return list(left) == list(right) and all(is_same(left[key], right[key]) for key in left)
    if isinstance(left, list | tuple):
        return len(left) == len(right) and all(is_same(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, Decimal):
        return str(left) == str(right)
    if isinstance(left, datetime.datetime | datetime.time):
        return left == right and left.tzinfo == right.tzinfo and left.fold == right.fold
    return bool(left == right)


def assert_same_outcome(codec_run: Any, python_run: Any, label: str) -> None:
    (codec_outcome, codec_warnings), (python_outcome, python_warnings) = codec_run, python_run
    assert codec_warnings == python_warnings, f"{label}: warnings {codec_warnings} != {python_warnings}"
    if codec_outcome[0] == python_outcome[0] == "value":
        assert is_same(codec_outcome[1], python_outcome[1]), (
            f"{label}: codec gave {codec_outcome[1]!r}, the field gave {python_outcome[1]!r}"
        )
    else:
        assert codec_outcome == python_outcome, f"{label}: codec {codec_outcome!r}, the field {python_outcome!r}"


def get_layout(model: Any, types: Any, native_types: frozenset[type]) -> HydrationLayout:
    connection = SimpleNamespace(dialect=SimpleNamespace(types=types), native_python_types=native_types)
    return HydrationLayout.build(model._meta, connection)  # type: ignore[arg-type]


def assert_reads_match(
    model: Any,
    field_name: str,
    raw_values: list[Any],
    connections: list[tuple[str, Any, Any, SqliteParameterAdapters | None]] = CONNECTIONS,
) -> None:
    """Reads every raw value with the codec and with the field, on every connection and zone."""
    for connection_alias, types, native_types, _adapters in connections:
        layout = get_layout(model, types, native_types)
        column, field, bound_reader = layout.entry_by_field_name[field_name]
        reader = (lambda value: value) if bound_reader is None else types.get_python_reader(field)
        columns = ModelColumns.build(model, layout, [column], [0])
        for use_timezone, zone in ZONES:
            with override_timezone(use_timezone=use_timezone, timezone=zone):
                for raw in raw_values:
                    label = (
                        f"{model.__name__}.{field_name} read {raw!r} on {connection_alias}, {zone} "
                        f"use_timezone={use_timezone}"
                    )
                    assert_same_outcome(
                        run(
                            lambda value: getattr(
                                NativeModules.rows.ModelResultLayout((columns,), ()).read_row(
                                    (value,), None, DirtyFields.snapshot_dirty_fields
                                ),
                                field_name,
                            ),
                            raw,
                        ),
                        run(reader, raw),
                        label,
                    )


def bind(value: Any, types: Any, adapters: SqliteParameterAdapters | None, decode_through: Any = None) -> Any:
    """The bound value, optionally decoded for comparison of randomized ciphertexts."""
    bound = adapters.adapt(value) if adapters is not None else value
    return types.get_python_value(decode_through, bound) if decode_through is not None else bound


def assert_writes_match(
    field: Any,
    values: list[Any],
    instance: Any = None,
    connections: list[tuple[str, Any, Any, SqliteParameterAdapters | None]] = CONNECTIONS,
    *,
    compare_decoded: bool = False,
) -> None:
    """Writes every value with the codec and with the field, on every connection and zone."""
    for connection_alias, types, _native_types, adapters in connections:
        for use_timezone, zone in ZONES:
            with override_timezone(use_timezone=use_timezone, timezone=zone):
                writer = types.get_db_writer(field)
                for value in values:
                    label = (
                        f"{field.model_field_name} write {value!r} on {connection_alias}, {zone} "
                        f"use_timezone={use_timezone}"
                    )
                    assert_same_outcome(
                        run(
                            lambda value: bind(
                                writer(value, instance), types, adapters, field if compare_decoded else None
                            ),
                            value,
                        ),
                        run(
                            lambda value: bind(
                                types.get_db_value(field, value, instance),
                                types,
                                adapters,
                                field if compare_decoded else None,
                            ),
                            value,
                        ),
                        label,
                    )


def make_field(field: Any, name: str) -> Any:
    field.model_field_name = name
    return field


DATETIME_READ_VALUES = [
    None,
    datetime.datetime(2024, 1, 15, 10, 30, tzinfo=UTC),
    datetime.datetime(2024, 7, 15, 10, 30, 0, 123456, tzinfo=PLUS_0530),
    datetime.datetime(2024, 1, 15, 10, 30, tzinfo=BERLIN),
    datetime.datetime(2024, 1, 15, 10, 30, tzinfo=Timezone.parse("Europe/Berlin")),
    datetime.datetime(2024, 3, 31, 2, 30),
    datetime.datetime(2024, 10, 27, 2, 30),
    datetime.datetime(2024, 1, 15, 10, 30),
    datetime.datetime(1960, 6, 1, 12, 0, tzinfo=UTC),
    datetime.datetime(1960, 6, 1, 12, 0),
    datetime.datetime.min,
    datetime.datetime.max,
    datetime.datetime.min.replace(tzinfo=UTC),
    datetime.datetime.max.replace(tzinfo=UTC),
    datetime.datetime(1, 1, 1, 0, 30, tzinfo=UTC),
    datetime.datetime(9999, 12, 31, 23, 0, tzinfo=UTC),
    datetime.datetime(2024, 1, 15, 10, 30, tzinfo=NoOffset()),
    "2024-01-15 10:30:00+00:00",
    "2024-01-15T10:30:00.123456+05:30",
    "2024-01-15 10:30:00",
    "2024-03-31 02:30:00",
    "2024-01-15 10:30:00.5+00:00",
    "2024-01-15",
    "2024-01-15 10:30",
    "2024-13-01 00:00:00",
    "0001-01-01 00:00:00+00:00",
    "garbage",
    "",
    datetime.date(2024, 1, 15),
    1_700_000_000,
    3.5,
]
DATETIME_WRITE_VALUES = [
    None,
    datetime.datetime(2024, 1, 15, 10, 30, tzinfo=UTC),
    datetime.datetime(2024, 7, 15, 10, 30, 0, 123456, tzinfo=PLUS_0530),
    datetime.datetime(2024, 1, 15, 10, 30, tzinfo=BERLIN),
    datetime.datetime(2024, 1, 15, 10, 30, tzinfo=Timezone.parse("Europe/Berlin")),
    datetime.datetime(2024, 1, 15, 10, 30),
    datetime.datetime(2024, 3, 31, 2, 30),
    datetime.datetime(1, 1, 1, 0, 30, tzinfo=UTC),
    datetime.datetime(9999, 12, 31, 23, 0, tzinfo=UTC),
    datetime.datetime(9999, 12, 31, 23, 0, tzinfo=PLUS_0300),
    datetime.datetime(2024, 1, 15, 10, 30, tzinfo=NoOffset()),
    datetime.date(2024, 1, 15),
    "2024-01-15 10:30:00+00:00",
    "garbage",
    1_700_000_000,
    3.5,
]


def test_bound_datetime_writer_runs_validators():
    field = make_field(fields.DatetimeField(validators=[MinValueValidator(0)]), "moment")
    assert_writes_match(field, [datetime.datetime(2024, 1, 15, tzinfo=UTC)])


@pytest.mark.parametrize("field_name", ["datetime_auto", "datetime_add"])
def test_a_stamp_writes_like_the_field_and_the_write_leaves_the_instance_as_it_is(field_name):
    """The stamp a write gives an auto_now/auto_now_add field is written by the codec as by the field -
    and neither changes the instance: the stamp is the write's, set before the values are written."""
    field = DatetimeFields._meta.fields_map[field_name]
    for _connection_name, types, _native_types, adapters in CONNECTIONS:
        for use_timezone, zone in ZONES:
            with override_timezone(use_timezone=use_timezone, timezone=zone):
                writer = types.get_db_writer(field)
                instance = DatetimeFields(datetime=datetime.datetime(2024, 1, 1, tzinfo=UTC))
                RowWriteRules.get(DatetimeFields, WriteType.INSERT, None, "create()").stamp(
                    (instance,), InstanceValues()
                )
                stamp = getattr(instance, field_name)
                assert stamp is not None
                assert_same_outcome(
                    run(lambda value: bind(writer(value, instance), types, adapters), stamp),
                    run(lambda value: bind(types.get_db_value(field, value, instance), types, adapters), stamp),
                    f"{field_name} stamp, {zone} use_timezone={use_timezone}",
                )
                assert getattr(instance, field_name) is stamp


def test_auto_now_add_keeps_a_set_value():
    field = DatetimeFields._meta.fields_map["datetime_add"]
    moment = datetime.datetime(2020, 5, 5, 5, 5, tzinfo=UTC)
    instance = DatetimeFields(datetime=moment, datetime_add=moment)
    instance._saved_in_db = True
    assert_writes_match(field, [moment], instance)


DATE_READ_VALUES = [
    None,
    datetime.date(2024, 2, 29),
    datetime.datetime(2024, 2, 29, 23, 59, tzinfo=UTC),
    datetime.datetime(2024, 2, 29, 23, 59),
    "2024-02-29",
    "2024-02-30",
    "2024-2-29",
    "2024-02-29 10:00:00",
    "0001-01-01",
    "9999-12-31",
    "garbage",
    "",
    20240229,
]


TIME_VALUES = [
    None,
    datetime.time(10, 30),
    datetime.time(10, 30, 15, 250),
    datetime.time(10, 30, tzinfo=PLUS_0300),
    datetime.time(10, 30, tzinfo=UTC),
    datetime.time(10, 30, tzinfo=BERLIN),
    datetime.time(10, 30, tzinfo=NoOffset()),
    datetime.time(0, 0, fold=1),
    datetime.timedelta(hours=5),
    "10:30:00",
    "10:30:00.123456",
    "10:30:00+03:00",
    "10:30:00.123456-02:30",
    "10:30",
    "10:30:00.5",
    "25:00:00",
    "garbage",
    "",
    5,
]


TIMEDELTA_VALUES = [
    None,
    0,
    1,
    -1,
    86_400_000_000,
    -86_400_000_001,
    2**62,
    2**70,
    -(2**70),
    1.5,
    Decimal("2.5"),
    True,
    datetime.timedelta(days=2, hours=3, microseconds=7),
    datetime.timedelta(days=-3, seconds=5),
    datetime.timedelta.max,
    datetime.timedelta.min,
    "5",
]


SAMPLE_UUID = uuid.UUID("12345678-9abc-def0-1234-56789abcdef0")
UUID_VALUES = [
    None,
    SAMPLE_UUID,
    DriverUUID(int=SAMPLE_UUID.int),
    str(SAMPLE_UUID),
    str(SAMPLE_UUID).upper(),
    SAMPLE_UUID.hex,
    "{" + str(SAMPLE_UUID) + "}",
    "urn:uuid:" + str(SAMPLE_UUID),
    "12345678-9abc-def0-1234-56789abcdefg",
    "+2345678-9abc-def0-1234-56789abcdef0",
    "1234_5678-9abc-def0-1234-56789abcdef0",
    "",
    SAMPLE_UUID.bytes,
    SAMPLE_UUID.int,
]


def test_uuid_read_builds_a_plain_uuid():
    layout = RowLayout((RowColumn(0, SQLITE_TYPES.get_python_reader(UUIDFields._meta.fields_map["data"])),))
    value = layout.read((str(SAMPLE_UUID),))[0]
    assert type(value) is uuid.UUID
    assert value == SAMPLE_UUID
    assert value.is_safe is uuid.SafeUUID.unknown
    assert hash(value) == hash(SAMPLE_UUID)
    with pytest.raises(TypeError):
        value.int = 5


ENUM_VALUES = [
    None,
    1,
    2,
    99,
    True,
    "1",
    "HUF",
    "EUR",
    "XXX",
    "",
    Service.python_programming,
    Currency.EUR,
    OtherService.python_programming,
    OtherService.unknown,
    OtherCurrency.EUR,
    OtherCurrency.XXX,
    [1],
]


DECIMAL_VALUES = [
    None,
    Decimal("1.0050"),
    Decimal("1.00005"),
    Decimal("1.00015"),
    Decimal("-1.00005"),
    Decimal("-0.00001"),
    Decimal("-0"),
    Decimal("0E-10"),
    Decimal("1E+5"),
    Decimal("12345678901234.5678"),
    Decimal("123456789012345.6789"),
    Decimal("99999999999999.99995"),
    Decimal("1" * 40),
    Decimal("NaN"),
    Decimal("Infinity"),
    "1.005",
    "abc",
    "",
    5,
    -7,
    10**30,
    1.005,
    0.1,
    float("nan"),
    float("inf"),
    True,
    [1],
]


def test_decimal_bounds_are_checked_in_validator_order():
    field = make_field(
        fields.DecimalField(
            max_digits=6,
            decimal_places=2,
            validators=[MinValueValidator(Decimal("-10")), MinValueValidator(Decimal("0.5")), MaxValueValidator(50)],
        ),
        "amount",
    )
    assert_writes_match(field, [Decimal("-20"), Decimal("0.1"), Decimal("0.5"), Decimal("60"), Decimal("12345.67")])


def test_decimal_with_a_custom_message_is_validated_by_the_field():
    field = make_field(
        fields.DecimalField(max_digits=6, decimal_places=2, validators=[MinValueValidator(0, message="positive")]),
        "amount",
    )
    assert_writes_match(field, [Decimal("-1"), Decimal("1")])


def test_high_precision_decimal_writes_match():
    field = make_field(fields.DecimalField(max_digits=40, decimal_places=10), "amount")
    assert_writes_match(field, [Decimal("1" * 30 + ".123456789012"), Decimal("1" * 31), Decimal("0.00000000005")])


JSON_READ_VALUES = [
    None,
    '{"a": 1, "b": [1, 2.5, "x", null, true]}',
    "[1, 2, 3]",
    '"text"',
    "12345678901234567890123",
    '{"big": 123456789012345678901234567890}',
    "{not valid json",
    "",
    b'{"a": 1}',
    b"\xff\xfe",
    {"already": "decoded"},
    [1, 2],
    5,
]


def nested(depth: int) -> Any:
    value: Any = 1
    for _ in range(depth):
        value = [value]
    return value


JSON_WRITE_VALUES = [
    None,
    {"a": 1, "b": [1, 2.5, "x", None, True, False], "c": {"d": (1, 2)}},
    [1, 2, 3],
    "text",
    "",
    5,
    -(2**63),
    2**64 - 1,
    2**64,
    -(2**63) - 1,
    {"big": [2**70]},
    1.5,
    0.1 + 0.2,
    1e16,
    1e-7,
    -0.0,
    5e-324,
    1.7976931348623157e308,
    float("nan"),
    {"x": float("inf")},
    "a\x00b",
    {"a\x00": 1},
    '\b\t\n\f\r"\\/\x1b\x7f é😀',
    "\ud800",
    {1: "non-str key"},
    {"n": Service.python_programming},
    {"when": datetime.datetime(2024, 1, 1, tzinfo=UTC)},
    {"id": SAMPLE_UUID},
    nested(200),
    nested(300),
    True,
    b"bytes",
]


SCALAR_CASES = [
    (IntFields, "intnum", [None, 5, -(2**31), 2**31, 2**31 - 1, True, 5.0, 5.5, Decimal("7"), "42", "x", 2**80]),
    (IntFields, "intnum_null", [None, 5, True, "42"]),
    (CharFields, "char", [None, "text", "", "a\x00b", "\ud800", "x" * 300, 5, b"bytes"]),
    (FloatFields, "floatnum", [None, 1.5, float("nan"), float("inf"), 5, True, "2.5", Decimal("1.1")]),
    (BooleanFields, "boolean", [None, True, False, 1, 0, 2, "true", "", 1.0]),
    (BinaryFields, "binary", [None, b"bytes", b"", bytearray(b"x"), memoryview(b"x"), "text", 5]),
]


READ_CASES = [
    *SCALAR_CASES,
    (DatetimeFields, "datetime", DATETIME_READ_VALUES),
    (DatetimeFields, "datetime_null", DATETIME_READ_VALUES),
    (DateFields, "date", DATE_READ_VALUES),
    (TimeFields, "time", TIME_VALUES),
    (TimeDeltaFields, "timedelta", TIMEDELTA_VALUES),
    (UUIDFields, "data", UUID_VALUES),
    (EnumFields, "service", ENUM_VALUES),
    (EnumFields, "currency", ENUM_VALUES),
    (DecimalFields, "decimal", DECIMAL_VALUES),
    (DecimalFields, "decimal_nodec", DECIMAL_VALUES),
    (DecimalFields, "decimal_null", DECIMAL_VALUES),
    (JSONFields, "data", JSON_READ_VALUES),
    (JSONFields, "data_null", JSON_READ_VALUES),
]
WRITE_CASES = [
    *SCALAR_CASES,
    (DatetimeFields, "datetime", DATETIME_WRITE_VALUES),
    (DatetimeFields, "datetime_null", DATETIME_WRITE_VALUES),
    (DateFields, "date", DATE_READ_VALUES),
    (DateFields, "date_null", DATE_READ_VALUES),
    (TimeFields, "time", TIME_VALUES),
    (TimeFields, "time_null", TIME_VALUES),
    (TimeDeltaFields, "timedelta", TIMEDELTA_VALUES),
    (TimeDeltaFields, "timedelta_null", TIMEDELTA_VALUES),
    (UUIDFields, "data", UUID_VALUES),
    (UUIDFields, "data_null", UUID_VALUES),
    (EnumFields, "service", ENUM_VALUES),
    (EnumFields, "currency", ENUM_VALUES),
    (DecimalFields, "decimal", DECIMAL_VALUES),
    (DecimalFields, "decimal_nodec", DECIMAL_VALUES),
    (DecimalFields, "decimal_null", DECIMAL_VALUES),
    (JSONFields, "data", JSON_WRITE_VALUES),
    (JSONFields, "data_null", JSON_WRITE_VALUES),
]


@pytest.mark.parametrize(
    ("model", "field_name", "values"),
    [pytest.param(*case, id=f"{case[0].__name__}.{case[1]}") for case in READ_CASES],
)
def test_reads_match(model, field_name, values):
    assert_reads_match(model, field_name, values)


@pytest.mark.parametrize(
    ("model", "field_name", "values"),
    [pytest.param(*case, id=f"{case[0].__name__}.{case[1]}") for case in WRITE_CASES],
)
def test_writes_match(model, field_name, values):
    assert_writes_match(model._meta.fields_map[field_name], values)


def test_scalar_checks_run_in_validator_order():
    field = make_field(
        fields.CharField(max_length=5, validators=[MinLengthValidator(2), MinLengthValidator(4)]), "label"
    )
    assert_writes_match(field, ["a", "abc", "abcd", "abcdef", "a\x00bcd"])
    number = make_field(fields.IntField(validators=[MaxValueValidator(10), MaxValueValidator(100)]), "count")
    assert_writes_match(number, [5, 50, 500, -(2**40)])


def test_bound_writer_calls_custom_validator():
    number = make_field(fields.IntField(validators=[MultipleOfThree()]), "count")
    assert_writes_match(number, [9, 10, None, "9"])


def test_sensitive_fields_preserve_validation_errors():
    for field, values in (
        (make_field(fields.CharField(max_length=3, sensitive=True), "secret"), ["abc", "abcd", 5]),
        (make_field(fields.JSONField(sensitive=True), "secret_json"), [{"a": 1}, object()]),
        (
            make_field(fields.DecimalField(max_digits=4, decimal_places=1, sensitive=True), "secret_amount"),
            [Decimal("1.5"), "bad"],
        ),
    ):
        assert_writes_match(field, values)


EXPRESSION_FIELDS = [
    make_field(fields.IntField(), "count"),
    make_field(fields.FloatField(), "ratio"),
    make_field(fields.DecimalField(max_digits=10, decimal_places=3), "total"),
    make_field(fields.CharField(max_length=10), "label"),
    make_field(fields.BooleanField(), "exists"),
    make_field(fields.DatetimeField(), "latest"),
]
EXPRESSION_VALUES = [
    None,
    5,
    -3,
    True,
    5.0,
    5.5,
    float("nan"),
    Decimal("3.3"),
    Decimal("0.125"),
    Decimal("2.50000"),
    "7",
    "text",
    "2024-01-15 10:30:00+00:00",
    datetime.datetime(2024, 1, 15, 10, 30, tzinfo=UTC),
]


@pytest.mark.parametrize("field", EXPRESSION_FIELDS, ids=lambda field: field.model_field_name)
def test_expression_column_reads_match(field):
    for connection_alias, types, _native_types, _adapters in CONNECTIONS:
        reader = types.get_python_reader(field)
        layout = RowLayout((RowColumn("result", reader),))
        for use_timezone, zone in ZONES:
            with override_timezone(use_timezone=use_timezone, timezone=zone):
                for raw in EXPRESSION_VALUES:
                    assert_same_outcome(
                        run(lambda value: layout.read({"result": value})[0], raw),
                        run(types.get_python_value, field, raw),
                        f"{field.model_field_name} expression on {connection_alias}, {zone}",
                    )


def assert_container_reads_match(field: Any, raw_values: list[Any]) -> None:
    for use_timezone, zone in ZONES:
        with override_timezone(use_timezone=use_timezone, timezone=zone):
            reader = HydrationLayout.get_field_reader(field, POSTGRESQL_TYPES, PostgresqlClient.native_python_types)
            layout = RowLayout((RowColumn(0, reader),))
            for raw in raw_values:
                assert_same_outcome(
                    run(lambda value: layout.read((value,))[0], raw),
                    run(POSTGRESQL_TYPES.get_python_value, field, raw),
                    f"{field.model_field_name} container in {zone}",
                )


class DriverRange:
    """A driver's own range object, read by its attributes."""

    def __init__(self, lower: Any, upper: Any, lower_inc: bool, upper_inc: bool, isempty: bool = False) -> None:
        self.lower, self.upper = lower, upper
        self.lower_inc, self.upper_inc, self.isempty = lower_inc, upper_inc, isempty


def get_bound_shapes(lower: Any, upper: Any, middle: Any) -> list[tuple[Any, Any]]:
    return [(lower, upper), (None, upper), (lower, None), (None, None), (middle, middle), (upper, lower)]


def range_grid(lower: Any, upper: Any, middle: Any) -> list[Any]:
    grid: list[Any] = [None, Range(is_empty=True), DriverRange(None, None, False, False, True)]
    for lower_inc in (True, False):
        for upper_inc in (True, False):
            for bounds in get_bound_shapes(lower, upper, middle):
                grid.append(Range(*bounds, lower_inc=lower_inc, upper_inc=upper_inc))
                grid.append(DriverRange(*bounds, lower_inc, upper_inc))
    grid += [(lower, upper), f"[{lower},{upper})", "garbage", 5]
    return grid


RANGE_CASES = [
    (
        IntRangeField(),
        range_grid(1, 10, 5) + [Range(True, 3), Range(1.5, 3), Range("1", "3"), Range(-(2**31), 2**31 - 1)],
    ),
    (
        DecimalRangeField(),
        range_grid(Decimal("1.5"), Decimal("10.25"), Decimal("5")) + [Range(1, 2), Range(Decimal("NaN"), 1)],
    ),
    (
        DateRangeField(),
        range_grid(datetime.date(2024, 1, 1), datetime.date(2024, 3, 1), datetime.date(2024, 2, 1))
        + [
            Range(datetime.datetime(2024, 1, 1, 10), datetime.date(2024, 2, 1)),
            Range(datetime.date.max, None, lower_inc=False),
            Range(None, datetime.date.max, upper_inc=True),
            Range("2024-01-01", "2024-02-01"),
        ],
    ),
    (
        DateTimeRangeField(),
        range_grid(
            datetime.datetime(2024, 1, 1, tzinfo=UTC),
            datetime.datetime(2024, 3, 1, 12, tzinfo=PLUS_0530),
            datetime.datetime(2024, 2, 1, tzinfo=BERLIN),
        )
        + [
            Range(datetime.datetime(2024, 1, 1), datetime.datetime(2024, 2, 1)),
            Range(datetime.datetime(2024, 3, 31, 2, 30), None),
            Range(datetime.datetime.min.replace(tzinfo=UTC), datetime.datetime.max.replace(tzinfo=UTC)),
            Range(datetime.datetime.min, datetime.datetime.max),
            Range(datetime.datetime(1, 1, 1, 1, tzinfo=PLUS_0300), None),
            Range(datetime.datetime(2024, 1, 1, tzinfo=NoOffset()), None),
            Range("2024-01-01 10:00:00+00:00", None),
        ],
    ),
]


@pytest.mark.parametrize(("field", "raw_values"), RANGE_CASES, ids=lambda case: type(case).__name__)
def test_range_reads_match(field, raw_values):
    field.model_field_name = "span"
    assert_container_reads_match(field, raw_values)


def make_array_field(base_field: Any) -> Any:
    array_field = ArrayField(base_field=base_field)
    array_field.model_field_name = "items"
    return array_field


ARRAY_CASES = [
    (make_array_field(fields.IntField()), [None, [], [1, None, 3], [True, 5.0], [5.5], (1, 2), "{1,2}", [2**70]]),
    (make_array_field(fields.TextField()), [None, ["a", None, ""], ["a\x00"], [5]]),
    (
        make_array_field(fields.DecimalField(max_digits=10, decimal_places=2)),
        [[Decimal("1.005"), Decimal("2")], [1.005, "3.3", None], [Decimal("NaN")], [Decimal("1" * 30)]],
    ),
    (
        make_array_field(fields.DatetimeField()),
        [[datetime.datetime(2024, 1, 1, tzinfo=UTC), datetime.datetime(2024, 1, 1), None], ["2024-01-01 10:00:00"]],
    ),
    (make_array_field(ArrayField(base_field=fields.IntField())), [[[1, 2], [3, None]], [[1, 2], None], [[]], [["5"]]]),
]


@pytest.mark.parametrize(
    ("field", "raw_values"), ARRAY_CASES, ids=lambda case: getattr(case, "base_field", case).__class__.__name__
)
def test_array_reads_match(field, raw_values):
    assert_container_reads_match(field, raw_values)


def test_an_array_subclass_reading_otherwise_is_read_by_the_field():
    class UpperTextArray(ArrayField):
        def decode_value(self, value, types):
            return [element.upper() for element in super().decode_value(value, types)]

    field = UpperTextArray(base_field=fields.TextField())
    reader = HydrationLayout.get_field_reader(field, POSTGRESQL_TYPES, PostgresqlClient.native_python_types)
    assert RowLayout((RowColumn(0, reader),)).read((["hello", "World"],))[0] == ["HELLO", "WORLD"]


def test_a_range_subclass_reading_otherwise_is_read_by_the_field():
    class ShiftedIntRange(IntRangeField):
        def coerce_bound(self, value: Any) -> Any:
            return None if value is None else int(value) + 1

    field = ShiftedIntRange()
    reader = HydrationLayout.get_field_reader(field, POSTGRESQL_TYPES, PostgresqlClient.native_python_types)
    value = RowLayout((RowColumn(0, reader),)).read((Range(1, 4),))[0]
    assert (value.lower, value.upper) == (2, 5)
