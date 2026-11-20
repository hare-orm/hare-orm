"""Datetime awareness follows utcoffset rather than the presence of a tzinfo object."""

import datetime

import pytest

from hare import Model, fields
from hare.dialects.sqlite.parameters.sqlite_parameter_adapters import SqliteParameterAdapters
from hare.time import Timezone
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.utils.no_offset_timezone import NoOffsetTimezone
from tests.utils.timezone_context import override_timezone


@pytest.mark.database_independent
def test_sqlite_datetime_parameters_and_literals_keep_semantically_naive_wall_clocks():
    from hare.dialects.sqlite.constants import SQLITE_DIALECT

    plain = datetime.datetime(2024, 1, 1, 12, 34, 56, 123456)
    marked = plain.replace(tzinfo=NoOffsetTimezone())
    assert Timezone.is_naive(marked)
    assert marked == plain
    for value in (plain, marked):
        assert SqliteParameterAdapters.adapt_datetime(value) == "2024-01-01 12:34:56.123456"
        assert SQLITE_DIALECT.literals.get_adapted_moment_literal_sql(value) == "'2024-01-01 12:34:56.123456'"
    aware = plain.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
    assert SqliteParameterAdapters.adapt_datetime(aware) == "2024-01-01 10:34:56.123456+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_sqlite_naive_datetime_forms_share_storage_and_filters(contract_database, composite):
    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    if connection.dialect.name != "sqlite":
        pytest.skip("The storage adapter belongs to SQLite")

    class Entry(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        stamp = fields.DatetimeField()

    contract_database.register_live_models([Entry], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    plain = datetime.datetime(2024, 1, 1, 12, 34, 56, 123456)
    marked = plain.replace(tzinfo=NoOffsetTimezone())
    with override_timezone(use_timezone=False):
        await Entry.objects.create(number=1, stamp=marked)
        await Entry.objects.create(number=2, stamp=plain)
        for repeat in (0, 1):
            assert await Entry.objects.order_by("number").values_list("stamp", flat=True) == [plain, plain]
            for value in (plain, marked):
                assert await Entry.objects.filter(stamp=value).order_by("number").values_list("number", flat=True) == [
                    1,
                    2,
                ]
        changed = plain + datetime.timedelta(days=1)
        await Entry.objects.filter(number=1).update(stamp=changed.replace(tzinfo=NoOffsetTimezone()))
        assert await Entry.objects.filter(stamp=changed).values_list("number", flat=True) == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offset",
    [
        datetime.timedelta(microseconds=1),
        datetime.timedelta(seconds=1, microseconds=234567),
        -datetime.timedelta(seconds=1, microseconds=234567),
    ],
)
async def test_postgresql_raw_datetime_parameters_keep_fractional_offsets(contract_database, offset):
    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    if connection.dialect.name != "postgresql":
        pytest.skip("PostgreSQL parameter conversion")
    value = datetime.datetime(2024, 1, 2, 12, 34, 56, 654321, tzinfo=datetime.timezone(offset))
    rows = await connection.execute_dicts("SELECT $1::timestamptz AS moment", [value])
    assert rows[0]["moment"] == value.astimezone(datetime.UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "sql_type"),
    [
        (datetime.datetime(2024, 1, 2, 12), "timestamp"),
        (datetime.datetime(2024, 1, 2, 12, tzinfo=NoOffsetTimezone()), "timestamp"),
        (datetime.time(12), "time"),
        (datetime.time(12, tzinfo=NoOffsetTimezone()), "time"),
    ],
)
async def test_native_postgresql_parameters_keep_semantically_naive_wall_clocks(contract_database, value, sql_type):
    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    if connection.driver_name != "postgresql":
        pytest.skip("Native PostgreSQL parameter conversion")
    rows = await connection.execute_dicts(f"SELECT $1::{sql_type} AS clock", [value])
    assert rows[0]["clock"] == value.replace(tzinfo=None)
    assert rows[0]["clock"].tzinfo is None
