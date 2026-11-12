"""Identity writers preserve field validation; converted values use the field's single writer."""

import datetime
import enum
import uuid
import warnings
from decimal import Decimal

import pytest

from hare import fields
from hare.contrib.test import hare_test_context
from hare.exceptions import ValidationError
from hare.fields.validators import (
    MaxLengthValidator,
    MaxValueValidator,
    MinLengthValidator,
    MinValueValidator,
    RegexValidator,
)
from hare.models import Model
from hare.native.native_modules import NativeModules

pytestmark = pytest.mark.sqlite_only


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


class OtherColor(enum.Enum):
    RED = "red"


class Placeholder(Model):
    id = fields.IntField(primary_key=True)


def refuse_odd(value):
    if value % 2:
        raise ValueError("odd")


AWARE = datetime.datetime(2026, 5, 1, 12, 30, tzinfo=datetime.UTC)
NAIVE = datetime.datetime(2026, 5, 1, 12, 30)


def make_fields_and_values():
    """Fields of every built-in conversion, each with the values its writer is compared on."""
    return [
        (fields.IntField(), [5, -3, 2**40, 2**70, True, 5.0, "5", Decimal(5), None, Level.LOW]),
        (fields.IntField(validators=[MinValueValidator(0), MaxValueValidator(10)]), [0, 10, -1, 11, 2**70, None]),
        (fields.IntField(null=True, validators=[refuse_odd]), [2, 3, None, "4"]),
        (fields.SmallIntField(), [5, 40000, -40000, None]),
        (fields.FloatField(), [1.5, float("inf"), 3, Decimal("1.5"), True, "1.5", None]),
        (
            fields.DecimalField(max_digits=6, decimal_places=2),
            [Decimal("1.234"), Decimal("12345.6"), Decimal("NaN"), Decimal("Infinity"), 1.25, 5, "3.10", True, None],
        ),
        (fields.CharField(max_length=5), ["abc", "abcdef", "", 5, None, b"ab"]),
        (
            fields.CharField(max_length=10, validators=[MinLengthValidator(2), RegexValidator(r"^[a-z]+", 0)]),
            ["abc", "a", "Abc", "abcdefghijk", None, 7],
        ),
        (fields.TextField(null=True), ["long" * 100, None, 3]),
        (fields.BooleanField(), [True, False, 1, 0, "true", None]),
        (fields.BinaryField(null=True), [b"abc", bytearray(b"ab"), memoryview(b"x"), "s", None]),
        (fields.UUIDField(), [uuid.UUID(int=7), str(uuid.UUID(int=9)), "nope", 5, None]),
        (
            fields.UUIDField(version=7, null=True),
            [uuid.UUID(int=5), uuid.UUID("0190163d-8694-739b-aea5-966c26f8ad91"), None],
        ),
        (fields.CharEnumField(Color), [Color.RED, Color.BLUE, "red", "green", OtherColor.RED, None]),
        (fields.IntEnumField(Level), [Level.LOW, Level.HIGH, 1, 3, "1", True, None]),
        (
            fields.JSONField(null=True),
            [
                {"a": 1},
                [1, 2],
                {"n": 2**70},
                {"f": float("nan")},
                {1: "x"},
                "text",
                5,
                {"d": datetime.date(2026, 1, 2)},
                {"u": uuid.UUID(int=5)},
                None,
            ],
        ),
        (fields.DateField(null=True), [datetime.date(2026, 1, 2), NAIVE, "2026-01-02", "bad", 5, None]),
        (
            fields.TimeField(null=True),
            [datetime.time(12, 30), datetime.time(12, 30, tzinfo=datetime.UTC), "12:30", datetime.timedelta(1), None],
        ),
        (
            fields.DatetimeField(null=True),
            [
                AWARE,
                NAIVE,
                datetime.datetime.min,
                datetime.datetime.max,
                datetime.datetime(1, 1, 2, tzinfo=datetime.UTC),
                datetime.date(2026, 1, 2),
                "2026-01-02T00:00:00",
                1700000000,
                None,
            ],
        ),
        (
            fields.TimeDeltaField(null=True),
            [datetime.timedelta(days=1, microseconds=5), datetime.timedelta(days=999999999), 5, None],
        ),
        (fields.SlugField(max_length=20, null=True), ["good-slug", "bad slug", "", None]),
        (fields.URLField(max_length=100, null=True), ["https://example.com/x", "nope", None]),
    ]


def get_outcome(write, value):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            result = write(value, None)
        except Exception as error:
            return ("error", type(error), str(error), [str(item.message) for item in caught])
    return ("value", type(result), result, [str(item.message) for item in caught])


@pytest.mark.parametrize("use_timezone", [True, False])
@pytest.mark.asyncio
async def test_bound_writers_use_the_fields_conversion(use_timezone):
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:", use_timezone=use_timezone):
        for field, values in make_fields_and_values():
            writer = field.get_db_value_writer()
            if not isinstance(writer, NativeModules.rows.TypedColumnWriter):
                assert writer == field.to_db_value, type(field).__name__
                continue
            for value in values:
                assert get_outcome(writer, value) == get_outcome(field.to_db_value, value), (field, value)


@pytest.mark.parametrize("use_timezone", [True, False])
@pytest.mark.asyncio
async def test_lookup_writers_convert_what_the_fields_convert(use_timezone):
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:", use_timezone=use_timezone):
        for field, values in make_fields_and_values():
            writer = field.get_lookup_value_writer()
            for value in values:
                assert get_outcome(writer, value) == get_outcome(field.to_lookup_value, value), (field, value)


@pytest.mark.database_independent
def test_a_field_overriding_its_conversion_or_validation_writes_through_python():
    class OwnConversion(fields.IntField):
        def to_db_value(self, value, instance):
            return super().to_db_value(value, instance) * 2

    class OwnValidation(fields.IntField):
        def validate(self, value):
            super().validate(value)

    own_conversion = OwnConversion()
    own_validation = OwnValidation()
    sensitive = fields.CharField(max_length=5, sensitive=True)
    assert own_conversion.get_db_value_writer() == own_conversion.to_db_value
    assert own_conversion.get_db_value_writer()(4, None) == 8
    assert own_validation.get_db_value_writer() == own_validation.to_db_value
    assert sensitive.get_db_value_writer() == sensitive.to_db_value


@pytest.mark.database_independent
def test_a_validator_subclass_with_its_own_call_is_called_by_the_writer():
    calls = []

    class CountingMaxLength(MaxLengthValidator):
        def __call__(self, value):
            calls.append(value)
            super().__call__(value)

    field = fields.CharField(max_length=20, validators=[CountingMaxLength(3)])
    writer = field.get_db_value_writer()

    assert writer("abc", None) == "abc"
    assert calls == ["abc"]
    with pytest.raises(Exception, match="Length of 'abcd' 4 > 3"):
        writer("abcd", None)
    assert calls == ["abc", "abcd"]


@pytest.mark.database_independent
def test_a_dialect_converter_of_another_owner_writes_through_python():
    def doubled(field, value, instance):
        return field.to_db_value(value, instance) * 2

    field = fields.IntField()
    writer = field.get_db_value_writer(doubled)

    assert not isinstance(writer, NativeModules.rows.TypedColumnWriter)
    assert writer(4, None) == 8


@pytest.mark.database_independent
def test_bulk_serialization_writes_through_the_columns_writers():
    field = fields.IntField(validators=[MaxValueValidator(10)])

    class Row:
        def __init__(self, value):
            self.value = value

    rows = NativeModules.rows.serialize_instances([Row(1), Row(10)], [("value", field.get_db_value_writer())])
    assert rows == [[1], [10]]
    with pytest.raises(Exception, match="less or equal to 10"):
        NativeModules.rows.serialize_instances([Row(11)], [("value", field.get_db_value_writer())])


@pytest.mark.database_independent
@pytest.mark.parametrize("method_name", ["get_db_value_writer", "to_db_values", "to_lookup_values"])
@pytest.mark.parametrize("field_class, value", [(fields.IntField, 5), (fields.TextField, "text")])
def test_a_failed_validator_is_not_retried_and_stops_later_validators(method_name, field_class, value):
    calls = []

    def before(value):
        calls.append(("before", value))

    def refuse_once(value):
        calls.append(("refuse", value))
        if sum(name == "refuse" for name, _ in calls) == 1:
            raise ValueError("refused once")

    def after(value):
        calls.append(("after", value))

    field = field_class(validators=[before, refuse_once, after])
    with pytest.raises(ValidationError, match="refused once"):
        if method_name == "get_db_value_writer":
            field.get_db_value_writer()(value, None)
        else:
            getattr(field, method_name)([value], None)
    assert calls == [("before", value), ("refuse", value)]
