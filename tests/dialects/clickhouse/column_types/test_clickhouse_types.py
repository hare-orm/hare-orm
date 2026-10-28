"""ClickHouse's own types - unsigned and wide integers, Float32, FixedString, LowCardinality, enums
as Enum8/Enum16 and addresses: their columns, values written and read back on both drivers, their
range checks, lookups, inspectdb and drift."""

import datetime
import ipaddress
import uuid

import pytest
import pytest_asyncio

from hare import fields
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
from hare.dialects.clickhouse.fields import (
    ClickhouseDateField,
    ClickhouseDatetimeField,
    FixedStringField,
    LowCardinalityField,
)
from hare.dialects.clickhouse.introspection.clickhouse_type_parser import ClickhouseTypeParser
from hare.exceptions import ConfigurationError, FieldError, OperationalError, ValidationError
from hare.query.expressions import Case, F, Value, When
from hare.query.functions import Abs, Avg, Max, Min, Sum
from tests.dialects.clickhouse.models import (
    Country,
    Device,
    DeviceLevel,
    DeviceStatus,
    Measurement,
    MomentReading,
    Team,
)

ADDRESS_V6 = ipaddress.IPv6Address("2001:db8::1")
ADDRESS_V4 = ipaddress.IPv4Address("10.0.0.7")


def make_device(device_id: int, **values) -> Device:
    defaults = {
        "tiny": 255,
        "big": 2**64 - 1,
        "huge": -(2**200),
        "ratio": 0.1,
        "code": "ab",
        "country": "fr",
        "city": "Paris",
        "status": DeviceStatus.ONLINE,
        "level": DeviceLevel.HIGH,
        "address": ADDRESS_V6,
        "gateway": ADDRESS_V4,
        "labels": ["a", "b"],
    }
    return Device(id=device_id, **{**defaults, **values})


@pytest_asyncio.fixture
async def devices(clickhouse_db):
    await make_device(1).save()
    await Device.objects.bulk_create(
        [
            make_device(
                2,
                tiny=0,
                big=0,
                huge=None,
                ratio=2.5,
                code="z",
                country="de",
                city=None,
                level=None,
                status=DeviceStatus.OFFLINE,
                address=ADDRESS_V4,
                gateway=None,
                labels=[],
            ),
        ]
    )


@pytest.mark.database_independent
def test_column_types():
    from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT

    fields_map = Device._meta.fields_map

    def column_type(name):
        return fields_map[name].get_column_type(CLICKHOUSE_DIALECT)

    assert [column_type(name) for name in ("tiny", "big", "huge", "ratio", "code", "address", "gateway")] == [
        "UInt8",
        "UInt64",
        "Int256",
        "Float32",
        "FixedString(4)",
        "IPv6",
        "IPv4",
    ]
    assert column_type("country") == "LowCardinality(VARCHAR(2))"
    assert column_type("status").startswith("Enum16(")
    assert column_type("level") == "Enum16('LOW' = 1, 'HIGH' = 500)"
    assert column_type("labels") == "Array(LowCardinality(VARCHAR(10)))"


@pytest.mark.asyncio
async def test_values_round_trip(devices):
    first, second = await Device.objects.order_by("id")
    assert (first.tiny, first.big, first.huge, first.ratio, first.code) == (255, 2**64 - 1, -(2**200), 0.1, "ab")
    assert (first.country, first.city, first.status, first.level) == (
        "fr",
        "Paris",
        DeviceStatus.ONLINE,
        DeviceLevel.HIGH,
    )
    assert (first.address, first.gateway, first.labels) == (ADDRESS_V6, ADDRESS_V4, ["a", "b"])
    assert (second.huge, second.ratio, second.city, second.level, second.gateway) == (None, 2.5, None, None, None)
    # An IPv4 address in an IPv6 column reads back as IPv4.
    assert second.address == ADDRESS_V4
    assert await Device.objects.order_by("id").values_list("code", "status", "level") == [
        ("ab", DeviceStatus.ONLINE, DeviceLevel.HIGH),
        ("z", DeviceStatus.OFFLINE, None),
    ]


@pytest.mark.asyncio
async def test_lookups(devices):
    def ids(**filters):
        return Device.objects.filter(**filters).order_by("id").values_list("id", flat=True)

    assert await ids(big__gt=2**63) == [1]
    assert await ids(huge__lt=0) == [1]
    assert await ids(ratio=0.1) == [1]
    assert await ids(code="z") == [2]
    assert await ids(code__startswith="a") == [1]
    assert await ids(country__in=["de", "it"]) == [2]
    assert await ids(city__isnull=True) == [2]
    assert await ids(status=DeviceStatus.OFFLINE) == [2]
    assert await ids(level=DeviceLevel.HIGH) == [1]
    assert await ids(level__gte=DeviceLevel.LOW) == [1]
    assert await ids(address=ADDRESS_V4) == [2]
    assert await ids(address="2001:db8::1") == [1]
    assert await ids(gateway=ADDRESS_V4) == [1]
    assert await ids(labels__contains=["b"]) == [1]


@pytest.mark.asyncio
async def test_values_out_of_range_are_refused(clickhouse_db):
    for values in (
        {"tiny": 256},
        {"tiny": -1},
        {"big": 2**64},
        {"ratio": 1e39},
        {"code": "abcde"},
        {"code": "ab\x00"},
        {"gateway": ADDRESS_V6},
        {"address": "not an address"},
        {"status": "unknown"},
    ):
        with pytest.raises(ValidationError):
            await make_device(9, **values).save()


@pytest.mark.asyncio
async def test_an_update_to_a_number_of_no_enum_member_is_refused(clickhouse_db):
    """The mutation wrote it into the Enum8 column - clickhouse-connect read it back as None,
    clickhouse-driver failed with a KeyError."""
    await make_device(1, tiny=1).save()

    await Device.objects.filter(id=1).update(level=F("tiny"))
    assert (await Device.objects.get(id=1)).level is DeviceLevel.LOW

    await Device.objects.filter(id=1).update(tiny=7)
    with pytest.raises(OperationalError):
        await Device.objects.filter(id=1).update(level=F("tiny"))
    assert (await Device.objects.get(id=1)).level is DeviceLevel.LOW

    await Device.objects.filter(id=1).update(level=Case(When(tiny=7, then=Value(500)), default=F("level")))
    assert (await Device.objects.get(id=1)).level is DeviceLevel.HIGH


@pytest.mark.asyncio
async def test_inspectdb_reads_the_types_and_drift_finds_none(clickhouse_db):
    from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
    from hare.migrations.drift import detect_drift
    from hare.migrations.state.model_state import ModelState
    from hare.migrations.state.state import State
    from tests.utils.model_source_generator import ModelSourceGenerator

    connection = Device._meta.connection
    table = await DatabaseCatalog.inspect_table(connection, "device")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    for expected in (
        "tiny = UInt8Field()",
        "big = UInt64Field()",
        "huge = Int256Field(null=True)",
        "ratio = Float32Field()",
        "code = FixedStringField(length=4)",
        "country = LowCardinalityField(base_field=fields.TextField())",
        "city = LowCardinalityField(base_field=fields.TextField(), null=True)",
        "address = fields.IPAddressField()",
        "gateway = fields.IPv4AddressField(null=True)",
    ):
        assert expected in source, expected
    state = State()
    state.models[("models", "Device")] = ModelState.make_from_model("models", Device)
    drift = await detect_drift(connection, state, ["models"])
    assert drift.mismatched_columns == []
    assert [
        operation for operation in drift.operations if getattr(operation, "model_name", "").lower() == "device"
    ] == []


@pytest.mark.asyncio
async def test_integers_beyond_64_bits_keep_every_digit(clickhouse_db):
    # A number literal that wide is a Float64 to ClickHouse - it is read from its text instead.
    huge = -(2**200) - 12345678901234567
    big = 2**64 - 3
    await make_device(1, huge=huge, big=big).save()
    await Device.objects.bulk_create([make_device(2, huge=huge + 1, big=big + 1)])
    assert await Device.objects.order_by("id").values_list("huge", "big") == [(huge, big), (huge + 1, big + 1)]
    assert await Device.objects.filter(huge=huge).values_list("id", flat=True) == [1]
    assert await Device.objects.filter(huge__gt=huge).values_list("id", flat=True) == [2]
    await Device.objects.filter(id=1).update(huge=huge - 1)
    assert (await Device.objects.get(id=1)).huge == huge - 1


@pytest.mark.asyncio
async def test_values_of_different_widths_share_their_column_s_type(clickhouse_db):
    """A bare literal takes the narrowest type holding it - an Int256, a UInt256 and an Int8 share no
    type in the rows of a bulk update or the branches of a CASE; they are read as their column's or
    result's type."""
    huge = 2**200
    await Device.objects.bulk_create([make_device(1), make_device(2)])
    await Device.objects.bulk_update(
        [make_device(1, huge=huge, big=2**64 - 1), make_device(2, huge=-1, big=1)], fields=["huge", "big"]
    )
    assert await Device.objects.order_by("id").values_list("huge", "big") == [(huge, 2**64 - 1), (-1, 1)]
    await Device.objects.update(huge=Case(When(id=1, then=Value(-(2**255))), default=Value(2**255 - 1)))
    assert await Device.objects.order_by("id").values_list("huge", flat=True) == [-(2**255), 2**255 - 1]
    picked = Case(When(id=1, then=Value(2**64 - 1)), default=Value(-1))
    assert await Device.objects.annotate(picked=picked).order_by("id").values_list("picked", flat=True) == [
        2**64 - 1,
        -1,
    ]


@pytest.mark.asyncio
async def test_a_case_of_a_uuid_or_a_map_literal_and_a_column(clickhouse_db):
    """A UUID literal is read as a UUID, a dict as the map the column holds - not as text, which
    shares no type with the column."""
    team = await Team.objects.create(name="a")
    await Team.objects.create(name="b")
    replacement = uuid.UUID(int=7)
    picked = Case(When(name="a", then=Value(replacement)), default=F("id"))
    picked_ids = await Team.objects.annotate(picked=picked).order_by("name").values_list("picked", flat=True)
    assert picked_ids[0] == replacement
    assert picked_ids[1] != team.id
    await Measurement.objects.create(id=1, counts=[], totals={1: 2}, marks=[], moments=[])
    await Measurement.objects.update(totals=Case(When(id=1, then=Value({3: 4})), default=F("totals")))
    assert await Measurement.objects.values_list("totals", flat=True) == [{3: 4}]


@pytest.mark.database_independent
def test_a_comma_inside_an_enum_value_does_not_split_the_type():
    specification = ClickhouseTypeParser.get_specification("Tuple(Enum8('a,b' = 1, 'c)' = 2), String)")
    assert [element.path for element in specification.kwargs["element_fields"]] == [
        "hare.fields.data.text.CharField",
        "hare.fields.data.text.TextField",
    ]


@pytest.mark.database_independent
def test_the_typed_literals_hare_writes_a_default_as_are_read_back_as_values():
    """A wide integer, a date and a moment came back as SqlDefault of their text - inspectdb of a database
    hare had migrated wrote other models than the ones migrated."""
    from hare.dialects.clickhouse.clickhouse_introspector import ClickhouseIntrospector

    for raw_sql, expected in (
        ("toInt128('18446744073709551616')", 18446744073709551616),
        ("toUInt256('5')", 5),
        ("CAST(toDate32('2020-01-01'), 'Date')", datetime.date(2020, 1, 1)),
        (
            "toDateTime64('2020-01-01 00:00:00.000001', 6, 'UTC')",
            datetime.datetime(2020, 1, 1, 0, 0, 0, 1, datetime.UTC),
        ),
    ):
        assert ClickhouseIntrospector.parse_db_default(raw_sql) == expected


@pytest.mark.database_independent
def test_a_json_column_is_a_json_field():
    specification = ClickhouseTypeParser.get_specification("Nullable(JSON)")
    assert specification.path == "hare.fields.data.json.JSONField"
    assert specification.kwargs == {"null": True}


@pytest.mark.asyncio
async def test_an_expression_of_a_64_bit_column_is_written(devices):
    await Device.objects.filter(id=1).update(big=F("big") - 1)
    await Device.objects.filter(id=2).update(big=F("big") + 2**63)
    assert await Device.objects.order_by("id").values_list("big", flat=True) == [2**64 - 2, 2**63]


@pytest.mark.asyncio
async def test_integer_arithmetic_of_a_null_column_reads_null(clickhouse_db):
    """An operand was widened to a non-Nullable integer type, which the server refuses a NULL in - the
    read failed instead of giving NULL."""
    await Country.objects.create(code="fr", name="France", population=None)
    await Country.objects.create(code="de", name="Germany", population=83)

    assert await Country.objects.order_by("code").annotate(next=F("population") + 1).values_list(
        "next", flat=True
    ) == [84, None]


@pytest.mark.asyncio
async def test_an_integer_enum_is_computed_with_by_its_number(devices):
    """The column is an Enum16, which takes no arithmetic, average or math function - the server's
    own error came back where PostgreSQL computes with the member's number."""
    await Device.objects.filter(id=2).update(level=DeviceLevel.LOW)
    low, high = DeviceLevel.LOW.value, DeviceLevel.HIGH.value

    assert await Device.objects.aggregate(average=Avg("level"), total=Sum("level")) == {
        "average": (low + high) / 2,
        "total": low + high,
    }
    assert await Device.objects.order_by("id").annotate(next=F("level") + 1, twice=F("level") * 2).values_list(
        "next", "twice"
    ) == [(high + 1, high * 2), (low + 1, low * 2)]
    assert await Device.objects.order_by("id").annotate(size=Abs("level")).values_list("size", flat=True) == [
        high,
        low,
    ]
    assert await Device.objects.aggregate(highest=Max("level")) == {"highest": DeviceLevel.HIGH}


@pytest.mark.asyncio
async def test_a_container_without_null_given_none_is_refused(clickhouse_db):
    """A tuple has no empty value: an unset one was written as the zeros of its elements - (0, '') -
    by create(), and failed in the driver on bulk_create(); an unset array was written empty, where
    PostgreSQL refuses NULL in a column without it."""

    def make_device_without_labels() -> Device:
        return Device(id=9, tiny=1, big=1, ratio=0.5, code="ab", country="fr", status=DeviceStatus.ONLINE)

    for write in (
        lambda: make_device_without_labels().save(),
        lambda: Device.objects.bulk_create([make_device_without_labels()]),
        lambda: Device.objects.filter(id=9).update(labels=None),
    ):
        with pytest.raises(ValidationError, match="labels: a value is required, got None"):
            await write()
    assert not await Device.objects.filter(id=9).exists()


@pytest.mark.asyncio
async def test_a_filter_by_a_number_of_no_member_is_refused(devices):
    """The label of 1.5 was looked up with the enum's own ValueError - an int enum field takes an int."""
    with pytest.raises(ValidationError, match="expected an int, got float"):
        await Device.objects.filter(level=1.5).count()
    assert await Device.objects.filter(level=DeviceLevel.HIGH.value).count() == 1


@pytest.mark.asyncio
async def test_a_char_enum_is_compared_and_ordered_by_its_text(devices):
    """An Enum8 compares by the number of its label - a checksum of the label - so order_by(),
    __gt and Min/Max followed no order of the text."""
    await Device.objects.bulk_create([make_device(3, status=DeviceStatus.ONLINE)])
    statuses = sorted(member.value for member in DeviceStatus)

    assert [status.value for status in await Device.objects.order_by("status").values_list("status", flat=True)] == [
        statuses[0],
        statuses[-1],
        statuses[-1],
    ]
    assert await Device.objects.aggregate(low=Min("status"), high=Max("status")) == {
        "low": DeviceStatus(statuses[0]),
        "high": DeviceStatus(statuses[-1]),
    }
    assert await Device.objects.filter(status__gt=statuses[0]).count() == 2


@pytest.mark.asyncio
async def test_a_float32_too_close_to_zero_and_a_fixed_string_with_a_zero_byte_are_refused(clickhouse_db):
    """1e-50 was stored as 0 silently; a zero byte inside a FixedString value failed as a SQL literal
    naming no field."""
    with pytest.raises(ValidationError, match="ratio: 1e-50 is too close to zero for a float32"):
        await make_device(1, ratio=1e-50).save()
    with pytest.raises(ValidationError, match="code: 'a.x00b' holds a zero byte"):
        await make_device(2, code="a\x00b").save()
    with pytest.raises(ValidationError, match="code: 'a.x00' holds a zero byte"):
        await make_device(3, code="a\x00").save()
    await make_device(4, ratio=1e-30).save()
    assert await Device.objects.values_list("ratio", flat=True) == [1e-30]


@pytest.mark.asyncio
async def test_a_fixed_string_takes_a_str_alone(clickhouse_db):
    """A number or bytes was written and compared as its str() - b'x' as the text "b'x'"."""
    for value in (5, b"x"):
        with pytest.raises(ValidationError, match="code: expected a str"):
            await make_device(1, code=value).save()
        with pytest.raises(ValidationError, match="code: expected a str"):
            await Device.objects.filter(code=value).count()
    assert await Device.objects.count() == 0


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "base_field",
    [
        fields.IntField(),
        fields.DateField(),
        fields.DatetimeField(),
        fields.DecimalField(max_digits=5, decimal_places=2),
        fields.CharEnumField(DeviceStatus),
        fields.IntField(null=True),
    ],
)
def test_low_cardinality_of_a_type_clickhouse_refuses_is_refused(base_field):
    """The server refused the table with raw error 455 or 43."""
    with pytest.raises(ConfigurationError, match="ClickHouse keeps LowCardinality of text, FixedString and UUID"):
        LowCardinalityField(base_field).get_column_type(CLICKHOUSE_DIALECT)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("base_field", "column_type"),
    [
        (fields.CharField(max_length=2), "LowCardinality(VARCHAR(2))"),
        (fields.TextField(), "LowCardinality(TEXT)"),
        (FixedStringField(3), "LowCardinality(FixedString(3))"),
        (fields.UUIDField(), "LowCardinality(UUID)"),
    ],
)
def test_low_cardinality_of_text_and_uuids(base_field, column_type):
    assert LowCardinalityField(base_field).get_column_type(CLICKHOUSE_DIALECT) == column_type


@pytest.mark.asyncio
async def test_a_path_segment_named_like_a_lookup_is_the_lookup_not_a_key(clickhouse_db):
    """``totals__in`` is the ``in`` lookup, which a map refuses as an array does - not the value of a
    key named "in" compared with the list."""
    with pytest.raises(FieldError, match="has no lookup 'in'"):
        Measurement.objects.filter(totals__in=[{1: 2}]).sql()
    with pytest.raises(FieldError, match="has no lookup 'in'"):
        Measurement.objects.filter(counts__in=[[1]]).sql()


@pytest.mark.asyncio
async def test_a_moment_is_kept_in_the_columns_own_type_and_precision(clickhouse_db):
    """A Date, a DateTime or a DateTime64 of another precision than 6 had no field - only the
    Date32 and DateTime64(6, 'UTC') of DateField and DatetimeField."""
    moment = datetime.datetime(2020, 1, 2, 3, 4, 5, 678500, tzinfo=datetime.UTC)
    await MomentReading.objects.create(id=1, day=datetime.date(2020, 1, 2), second=moment, milli=moment, local=moment)
    reading = await MomentReading.objects.get(id=1)
    # Rounded to the column's precision, a tie away from zero - as PostgreSQL rounds a timestamptz(p).
    assert (reading.day, reading.second, reading.milli, reading.local) == (
        datetime.date(2020, 1, 2),
        moment.replace(second=6, microsecond=0),
        moment.replace(microsecond=679000),
        moment.replace(microsecond=679000),
    )
    assert await MomentReading.objects.filter(milli=moment.replace(microsecond=679000), second__year=2020).count() == 1
    # The moment written is compared rounded as it was written - a filter by it found no row, and
    # get_or_create() wrote it again.
    assert await MomentReading.objects.filter(second=moment, milli=moment, local__in=[moment]).count() == 1
    _reading, created = await MomentReading.objects.get_or_create(second=moment, milli=moment, defaults={"id": 2})
    assert not created
    columns = await MomentReading._meta.connection.execute_dicts(
        "SELECT name, type FROM system.columns WHERE table = 'momentreading' AND database = currentDatabase()"
    )
    assert {column["name"]: column["type"] for column in columns} == {
        "id": "Int64",
        "day": "Nullable(Date)",
        "second": "Nullable(DateTime('UTC'))",
        "milli": "Nullable(DateTime64(3, 'UTC'))",
        "local": "Nullable(DateTime64(3, 'Europe/Moscow'))",
        "count": "Nullable(Int32)",
        "lasts": "Nullable(Int64)",
    }


@pytest.mark.asyncio
async def test_a_tie_before_1970_rounds_away_from_the_epoch(clickhouse_db):
    """A moment before 1970 is a negative count of ticks - its tie rounded up, to the epoch, though a
    tie rounds away from zero; a DateTime column then took 1969 as 1970 instead of refusing it."""
    moment = datetime.datetime(1969, 12, 31, 23, 59, 59, 999500, tzinfo=datetime.UTC)
    await MomentReading.objects.create(id=1, milli=moment)

    assert (await MomentReading.objects.get(id=1)).milli == moment.replace(microsecond=999000)
    with pytest.raises(ValidationError, match="outside the moments a DateTime column holds"):
        await MomentReading.objects.create(id=2, second=moment.replace(microsecond=500000))


@pytest.mark.asyncio
async def test_a_moment_past_a_nanosecond_column_is_refused(clickhouse_db):
    """A DateTime64(9) counts nanoseconds in 64 bits - up to 2262-04-11; a later moment reached the server and
    failed there instead of being refused, in a write and in a filter alike."""
    field = ClickhouseDatetimeField(precision=9)
    field.model_field_name = "nano"
    last_moment = datetime.datetime(2262, 4, 11, 23, 47, 16, 854775, tzinfo=datetime.UTC)

    assert ClickhouseDatetimeField.to_db_rounded_value(field, last_moment, None).value == last_moment
    with pytest.raises(ValidationError, match=r"outside the moments a DateTime64\(9\) column holds"):
        ClickhouseDatetimeField.to_db_rounded_value(field, last_moment + datetime.timedelta(microseconds=1), None)


@pytest.mark.asyncio
async def test_a_list_of_moments_is_compared_with_a_datetime_column(clickhouse_db):
    """IN of a DateTime column failed on every value - ClickHouse compares no DateTime64 in its list."""
    moment = datetime.datetime(2020, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
    later = moment + datetime.timedelta(seconds=1)
    await MomentReading.objects.create(id=1, second=moment)
    await MomentReading.objects.create(id=2, second=later)
    assert await MomentReading.objects.filter(second__in=[moment]).values_list("id", flat=True) == [1]
    assert await MomentReading.objects.filter(second__in=[moment, later, None]).count() == 2
    assert await MomentReading.objects.filter(second__not_in=[moment]).values_list("id", flat=True) == [2]
    assert await MomentReading.objects.filter(second__global_in=[later]).values_list("id", flat=True) == [2]
    # A moment with a fraction of a second is rounded to the column's whole second, as it is written.
    assert await MomentReading.objects.filter(second__in=[moment.replace(microsecond=5)]).count() == 1
    rounded_down = moment.replace(microsecond=400000)
    assert await MomentReading.objects.filter(second=rounded_down).values_list("id", flat=True) == [1]


@pytest.mark.asyncio
async def test_a_moment_outside_the_columns_range_is_refused(clickhouse_db):
    with pytest.raises(ValidationError, match="Date column"):
        await MomentReading.objects.create(id=1, day=datetime.date(1969, 12, 31))
    with pytest.raises(ValidationError, match="DateTime column"):
        await MomentReading.objects.create(id=1, second=datetime.datetime(2107, 1, 1, tzinfo=datetime.UTC))
    assert await MomentReading.objects.count() == 0


@pytest.mark.database_independent
def test_a_moment_field_takes_a_precision_and_a_time_zone_clickhouse_has():
    assert ClickhouseDatetimeField(timezone=None).SQL_TYPE == "DateTime"
    assert ClickhouseDatetimeField(precision=9, timezone="Asia/Tokyo").SQL_TYPE == "DateTime64(9, 'Asia/Tokyo')"
    assert ClickhouseDateField().SQL_TYPE == "Date"
    for arguments in ({"precision": 10}, {"precision": True}, {"timezone": "Mars/Base"}):
        with pytest.raises(ConfigurationError):
            ClickhouseDatetimeField(**arguments)


@pytest.mark.parametrize(
    ("type_sql", "path", "kwargs"),
    [
        ("Date", "hare.dialects.clickhouse.fields.ClickhouseDateField", {}),
        ("Date32", "hare.fields.data.temporal.DateField", {}),
        ("DateTime", "hare.dialects.clickhouse.fields.ClickhouseDatetimeField", {"timezone": None}),
        ("DateTime('UTC')", "hare.dialects.clickhouse.fields.ClickhouseDatetimeField", {}),
        (
            "DateTime64(3, 'Europe/Moscow')",
            "hare.dialects.clickhouse.fields.ClickhouseDatetimeField",
            {"precision": 3, "timezone": "Europe/Moscow"},
        ),
        ("DateTime64(6, 'UTC')", "hare.fields.data.temporal.DatetimeField", {}),
    ],
)
def test_a_moment_column_maps_onto_the_field_writing_its_type(type_sql, path, kwargs):
    """inspectdb read a Date, a DateTime and a DateTime64 of another precision as the DateField and
    DatetimeField writing another type - drift reported them for good."""
    specification = ClickhouseTypeParser.get_specification(type_sql)
    assert (specification.path, specification.kwargs) == (path, kwargs)


@pytest.mark.asyncio
async def test_an_enum_field_updated_from_an_expression_keeps_its_member(clickhouse_db):
    """The value an update from F() writes was read back as the enum's label and checked as a plain
    number - update(level=F("level")) raised "Value must be a number, got a str"."""
    await Device.objects.bulk_create([make_device(1, level=DeviceLevel.HIGH)])
    await Device.objects.filter(id=1).update(level=F("level"), status=F("status"))
    assert await Device.objects.filter(id=1).values_list("level", "status") == [
        (DeviceLevel.HIGH, DeviceStatus.ONLINE)
    ]


@pytest.mark.asyncio
async def test_an_integer_enum_member_is_written_as_text_by_its_number(clickhouse_db):
    """An IntEnumField is an Enum column whose labels are the members' names - Cast() to text, Concat()
    and JSONObject() wrote the name of a member, 'HIGH', where its number is the field's value."""
    from hare.query.expressions import F, Value
    from hare.query.functions import Cast, Concat, JSONObject

    await make_device(1).save(force_create=True)

    async def get_value(expression):
        return (await Device.objects.annotate(value=expression).values_list("value", flat=True))[0]

    assert await get_value(Cast("level", fields.TextField())) == "500"
    assert await get_value(Concat(Value("level "), F("level"))) == "level 500"
    assert await get_value(JSONObject(level="level")) == {"level": 500}


@pytest.mark.asyncio
async def test_a_scaled_duration_shares_a_type_with_a_duration_beside_it(clickhouse_db):
    """A duration times a float, or divided, is a Float64 - beside a duration literal past 2 ** 32
    microseconds or a duration column ClickHouse found no common type."""
    from hare.query.functions import Coalesce, Greatest

    long_duration = datetime.timedelta(days=1, seconds=3, microseconds=7)
    await MomentReading.objects.bulk_create(
        [MomentReading(id=1, lasts=datetime.timedelta(seconds=10, microseconds=3)), MomentReading(id=2)]
    )
    for expression, expected in (
        (Coalesce(F("lasts") * 0.5, long_duration), [datetime.timedelta(seconds=5, microseconds=2), long_duration]),
        (Coalesce(F("lasts") / 2, long_duration), [datetime.timedelta(seconds=5, microseconds=2), long_duration]),
        # ClickHouse leaves a NULL argument of greatest() out.
        (Greatest(F("lasts") * 0.5, long_duration), [long_duration, long_duration]),
        (
            Case(When(id=1, then=F("lasts") * 1.5), default=long_duration),
            [
                datetime.timedelta(seconds=15, microseconds=4),
                long_duration,
            ],
        ),
    ):
        values = await MomentReading.objects.annotate(value=expression).order_by("id").values_list("value", flat=True)
        assert list(values) == expected
    await MomentReading.objects.update(lasts=Coalesce(F("lasts") * 0.5, long_duration))
    assert list(await MomentReading.objects.order_by("id").values_list("lasts", flat=True)) == [
        datetime.timedelta(seconds=5, microseconds=2),
        long_duration,
    ]


@pytest.mark.asyncio
async def test_a_cursor_after_an_enum_label_compares_the_label(clickhouse_db):
    """The boundary of a cursor past a CharEnumField was bound as the number the enum type writes it as,
    compared with the label the column is ordered by - ClickHouse found no common type."""
    for device_id, status in ((1, DeviceStatus.ONLINE), (2, DeviceStatus.OFFLINE), (3, DeviceStatus.ONLINE)):
        await make_device(device_id, status=status).save()
    query = Device.objects.order_by("status", "id")
    for _ in range(2):
        # The second time the boundary is bound again into the plan kept by the first.
        rows = await query.after_cursor(DeviceStatus.OFFLINE, 2).values_list("id", flat=True)
        assert rows == [1, 3]
