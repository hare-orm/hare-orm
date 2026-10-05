"""The clickhouse-driver driver - ClickHouse's native TCP protocol: its settings, the worker threads
keeping a connection each, the ping a busy connection skips and the moments its binary insert writes
as whole ticks. The tests of a live server run on its own DB_URL (``make test_clickhouse``)."""

import asyncio
import datetime
import os
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("clickhouse_driver")

import pytz  # noqa: E402
from clickhouse_driver.columns.datetimecolumn import DateTime64Column, DateTimeColumn  # noqa: E402
from clickhouse_driver.connection import Connection as LibraryConnection  # noqa: E402

from hare import Connections  # noqa: E402
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator  # noqa: E402
from hare.dialects.clickhouse.drivers.clickhouse_driver.client import clickhouse_driver_connections  # noqa: E402
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_client import (  # noqa: E402
    ClickhouseDriverClient,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_library_client import (  # noqa: E402
    ClickhouseDriverLibraryClient,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_moment_columns import (  # noqa: E402
    ClickhouseDriverMomentColumns,
)
from hare.exceptions import ConfigurationError, DBConnectionError, OperationalError  # noqa: E402
from tests.dialects.clickhouse.models import Player  # noqa: E402

BEFORE_1970 = datetime.datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=datetime.UTC)


@pytest.fixture
def driver_db(clickhouse_db):
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse+clickhouse-driver"):
        pytest.skip("runs on the clickhouse-driver driver")
    return clickhouse_db


def test_a_db_url_names_the_native_port_and_the_most_connections():
    config = DbUrlConfigGenerator.expand(
        "clickhouse+clickhouse-driver://user:secret@db.local/analytics?max_size=4&compress=false"
    )
    assert config["engine"] == "clickhouse+clickhouse-driver"
    assert config["credentials"] == {
        "host": "db.local",
        "port": 9000,
        "user": "user",
        "password": "secret",
        "database": "analytics",
        "max_size": 4,
        "compress": False,
    }
    client = ClickhouseDriverClient(connection_alias="clickhouse", **config["credentials"])
    assert (client.port, client.options["max_size"], client.options["compress"]) == (9000, 4, False)
    assert ClickhouseDriverClient(connection_alias="clickhouse", host="h").port == 9000
    assert ClickhouseDriverClient(connection_alias="clickhouse", host="h").options["max_size"] == 16


@pytest.mark.parametrize("max_size", [0, 1025, "many"])
def test_the_most_connections_is_checked(max_size):
    with pytest.raises(ConfigurationError, match="max_size"):
        ClickhouseDriverClient(connection_alias="clickhouse", host="h", max_size=max_size)


def test_a_setting_of_another_driver_is_refused():
    with pytest.raises(ConfigurationError, match="no_such_option"):
        DbUrlConfigGenerator.expand("clickhouse+clickhouse-driver://h/db?no_such_option=1")
    with pytest.raises(ConfigurationError, match="max_size"):
        DbUrlConfigGenerator.expand("clickhouse+clickhouse-connect://h/db?max_size=4")


@pytest.mark.asyncio
async def test_the_interactive_client_gets_the_connections_own_port():
    client = ClickhouseDriverClient(connection_alias="clickhouse", host="ch.internal", port=9440, secure=True)
    assert (await client.get_shell_command()).arguments == (
        "clickhouse-client",
        "--host",
        "ch.internal",
        "--user",
        "default",
        "--port",
        "9440",
        "--secure",
    )


@pytest.mark.parametrize(
    ("column_type", "moment_scale"),
    [
        ("DateTime64(6, 'UTC')", 6),
        ("DateTime64(3)", 3),
        ("Nullable(DateTime64(9, 'Asia/Tokyo'))", 9),
        ("DateTime", 0),
        ("DateTime('UTC')", 0),
        ("LowCardinality(Nullable(DateTime))", 0),
        ("Date", None),
        ("Date32", None),
        ("String", None),
        ("Array(DateTime64(6))", None),
    ],
)
def test_the_scale_of_moments_is_read_off_the_servers_column_type(column_type, moment_scale):
    assert ClickhouseDriverLibraryClient._get_moment_scale(column_type) == moment_scale


@pytest.mark.parametrize(
    ("moment_scale", "ticks"),
    [(6, -500_000), (3, -500), (0, -1), (9, -500_000_000)],
)
def test_a_moment_before_1970_is_written_as_its_whole_ticks(moment_scale, ticks):
    assert ClickhouseDriverLibraryClient._get_moment_ticks([BEFORE_1970, None, 7], moment_scale) == [ticks, None, 7]


def test_a_moment_of_another_zone_is_written_as_its_instant():
    tokyo = datetime.timezone(datetime.timedelta(hours=9))
    moment = datetime.datetime(2024, 1, 2, 12, 4, 5, 678901, tzinfo=tokyo)
    assert ClickhouseDriverLibraryClient._get_moment_ticks([moment], 6) == [1_704_164_645_678_901]
    assert ClickhouseDriverLibraryClient._get_moment_ticks([moment], 3) == [1_704_164_645_678]


@pytest.mark.asyncio
async def test_a_loaded_moment_before_1970_keeps_its_second(driver_db):
    await Player.objects.bulk_create([Player(id=1, name="early", joined=BEFORE_1970)])
    rows = await Player.get_connection().execute_dicts("SELECT toString(joined) AS joined FROM player")
    assert rows == [{"joined": "1969-12-31 23:59:59.500000"}]


@pytest.mark.asyncio
async def test_more_statements_than_connections_all_run(driver_db):
    client = Connections.current().create_independent("models", {"max_size": 2})
    try:
        results = await asyncio.gather(*[client.execute_dicts(f"SELECT {number} AS n") for number in range(12)])
        assert [rows[0]["n"] for rows in results] == list(range(12))
        status = client.get_pool_status()
        assert status is not None
        assert 1 <= status.size <= 2
        assert (status.in_use, status.waiting, status.max_size) == (0, 0, 2)
        assert status.acquire_count >= 12
        assert 1 <= status.connect_count <= 2
    finally:
        await client.close()
    assert client.get_pool_status() is None


@pytest.mark.asyncio
async def test_statements_waiting_for_a_connection_are_counted(driver_db):
    client = Connections.current().create_independent("models", {"max_size": 1})
    try:
        await client.execute_dicts("SELECT 1 AS n")
        statements = [asyncio.create_task(client.execute_dicts("SELECT sleep(0.3) AS n")) for _ in range(3)]
        await asyncio.sleep(0.1)
        status = client.get_pool_status()
        assert (status.size, status.in_use, status.idle, status.waiting) == (1, 1, 0, 2)
        await asyncio.gather(*statements)
        status = client.get_pool_status()
        assert (status.in_use, status.idle, status.waiting) == (0, 1, 0)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_failed_statement_leaves_the_connection_usable(driver_db):
    client = Connections.current().create_independent("models", {"max_size": 1})
    try:
        with pytest.raises(OperationalError, match="no_such_table"):
            await client.execute_dicts("SELECT * FROM no_such_table")
        assert await client.execute_dicts("SELECT 2 AS n") == [{"n": 2}]
        # The library closes the connection of a failed statement - the next one opened another.
        assert client.get_pool_status().connect_count == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_server_that_cannot_be_reached_is_a_connection_error(driver_db):
    client = Connections.current().create_independent("models", {"port": 1, "connect_timeout": 0.3})
    try:
        with pytest.raises(DBConnectionError):
            await client.execute_dicts("SELECT 1 AS n")
        assert client.get_pool_status() is None
        assert client.pool_statistics.connect_failures == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_busy_connection_skips_the_ping_and_an_idle_one_is_checked(driver_db, monkeypatch):
    pings = []
    library_ping = LibraryConnection.ping

    def counting_ping(connection):
        pings.append(connection)
        return library_ping(connection)

    monkeypatch.setattr(LibraryConnection, "ping", counting_ping)
    client = Connections.current().create_independent("models", {"max_size": 1})
    try:
        for _ in range(5):
            await client.execute_dicts("SELECT 1 AS n")
        assert pings == []
        (library_client,) = client._connection.library_clients
        library_client.last_statement_time = time.monotonic() - 3600
        await client.execute_dicts("SELECT 1 AS n")
        assert len(pings) == 1
        await client.execute_dicts("SELECT 1 AS n")
        assert len(pings) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_close_waits_for_the_running_statement(driver_db):
    client = Connections.current().create_independent("models", {"max_size": 1})
    statement = asyncio.create_task(client.execute_dicts("SELECT sleep(0.5) AS n"))
    await asyncio.sleep(0.1)
    await client.close()
    assert statement.done()
    assert await statement == [{"n": 0}]


@pytest.mark.asyncio
async def test_close_stops_waiting_for_a_statement_running_too_long(driver_db, monkeypatch):
    monkeypatch.setattr(clickhouse_driver_connections, "CLICKHOUSE_DRIVER_CLOSE_TIMEOUT_SECONDS", 0.2)
    client = Connections.current().create_independent("models", {"max_size": 1})
    await client.execute_dicts("SELECT 1 AS n")
    (library_client,) = client._connection.library_clients
    running = asyncio.create_task(client.execute_dicts("SELECT sleep(1.5) AS n"))
    waiting = asyncio.create_task(client.execute_dicts("SELECT 1 AS n"))
    await asyncio.sleep(0.1)
    start = time.perf_counter()
    await client.close()
    assert time.perf_counter() - start < 1
    assert library_client.connection.connected
    # The running statement ends on its own and its thread closes the connection; the one that
    # had not started fails.
    assert await running == [{"n": 0}]
    with pytest.raises(DBConnectionError, match="closed before the statement started"):
        await waiting
    assert not library_client.connection.connected


@pytest.mark.asyncio
async def test_a_cancelled_wait_drops_a_statement_that_has_not_started(driver_db):
    client = Connections.current().create_independent("models", {"max_size": 1})
    try:
        running = asyncio.create_task(client.execute_dicts("SELECT sleep(0.4) AS n"))
        waiting = asyncio.create_task(client.execute_dicts("SELECT 1 AS n"))
        await asyncio.sleep(0.1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert client.get_pool_status().waiting == 0
        assert await running == [{"n": 0}]
    finally:
        await client.close()


def test_a_column_of_anything_else_is_written_as_given():
    values = ["6f1e5a0e-0000-4000-8000-000000000001", BEFORE_1970]
    assert ClickhouseDriverLibraryClient._get_written_values(values, "String") is values
    # A moment held in a container is written by its ticks too.
    assert ClickhouseDriverLibraryClient._get_written_values([[BEFORE_1970], None], "Array(DateTime64(6))") == [
        [-500_000],
        None,
    ]
    assert ClickhouseDriverLibraryClient._get_written_values([BEFORE_1970], "Nullable(DateTime64(6, 'UTC'))") == [
        -500_000
    ]


def make_moment_column(column_class, **options):
    """A library column of moments, without a connection's context."""
    return column_class(nullable=False, context=SimpleNamespace(client_settings={}), **options)


@pytest.mark.parametrize(
    ("scale", "ticks", "moment"),
    [
        (6, -2_208_988_800_000_000, datetime.datetime(1900, 1, 1, tzinfo=datetime.UTC)),
        (6, 10_413_791_999_999_999, datetime.datetime(2299, 12, 31, 23, 59, 59, 999999, tzinfo=datetime.UTC)),
        (3, -500, datetime.datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=datetime.UTC)),
        (9, 1_500_000_999, datetime.datetime(1970, 1, 1, 0, 0, 1, 500000, tzinfo=datetime.UTC)),
    ],
)
def test_a_moment_is_read_from_its_ticks_exactly(scale, ticks, moment):
    ClickhouseDriverMomentColumns.install()
    column = make_moment_column(DateTime64Column, scale=scale, timezone=pytz.utc, offset_naive=False)
    assert column.after_read_items((ticks, 0), [0, 1]) == (moment, None)


def test_a_moment_of_another_zone_is_read_in_it():
    ClickhouseDriverMomentColumns.install()
    tokyo = pytz.timezone("Asia/Tokyo")
    column = make_moment_column(DateTime64Column, scale=6, timezone=tokyo, offset_naive=False)
    (moment,) = column.after_read_items((-1_000_000,))
    assert moment == datetime.datetime(1969, 12, 31, 23, 59, 59, tzinfo=datetime.UTC)
    assert moment.utcoffset() == datetime.timedelta(hours=9)
    naive_column = make_moment_column(DateTime64Column, scale=6, timezone=tokyo, offset_naive=True)
    assert naive_column.after_read_items((0,)) == (datetime.datetime(1970, 1, 1, 9),)


def test_a_whole_second_before_1970_is_read():
    ClickhouseDriverMomentColumns.install()
    column = make_moment_column(DateTimeColumn, timezone=pytz.utc, offset_naive=False)
    assert column.after_read_items((-86_400,)) == (datetime.datetime(1969, 12, 31, tzinfo=datetime.UTC),)
