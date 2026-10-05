"""ClickHouse's own types - unsigned and wide integers, Float32, FixedString, LowCardinality, enums
as Enum8/Enum16 and addresses: their columns, values written and read back on both drivers, their
range checks, lookups, inspectdb and drift."""

import ipaddress

import pytest
import pytest_asyncio

from hare.dialects.clickhouse.introspection.clickhouse_type_parser import ClickhouseTypeParser
from hare.exceptions import ValidationError
from tests.dialects.clickhouse.models import Device, DeviceLevel, DeviceStatus

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
async def test_inspectdb_reads_the_types_and_drift_finds_none(clickhouse_db):
    from hare.inspectdb.generation.model_source_generator import ModelSourceGenerator
    from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
    from hare.migrations.drift import detect_drift
    from hare.migrations.state.model_state import ModelState
    from hare.migrations.state.state import State
    from hare.migrations.state.state_apps import StateApps

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
    state = State(models={}, apps=StateApps())
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


def test_a_comma_inside_an_enum_value_does_not_split_the_type():
    specification = ClickhouseTypeParser.get_specification("Tuple(Enum8('a,b' = 1, 'c)' = 2), String)")
    assert [element.path for element in specification.kwargs["element_fields"]] == [
        "hare.fields.data.text.CharField",
        "hare.fields.data.text.TextField",
    ]
