"""A ClickHouse client opens its driver's connection on first use - once, however many statements
start together."""

import asyncio
import time
import uuid

import pytest

from hare import Connections
from hare.dialects.clickhouse.keeper.clickhouse_database_keeper_nodes import ClickhouseDatabaseKeeperNodes
from hare.dialects.clickhouse.keeper.clickhouse_migration_lock import ClickhouseMigrationLock
from hare.exceptions import ConfigurationError, DBConnectionError, OperationalError
from tests.dialects.clickhouse.constants import CLICKHOUSE_KEEPER_HOSTS
from tests.utils.waiting import WAIT_DEADLINE_SECONDS


@pytest.mark.asyncio
async def test_statements_starting_together_open_one_connection(clickhouse_db):
    client = Connections.current().create_independent("models", {})
    opened = []
    create_connection = client.create_connection

    async def counting_create_connection(with_db: bool) -> None:
        opened.append(with_db)
        await create_connection(with_db)

    client.create_connection = counting_create_connection
    try:
        results = await asyncio.gather(*[client.execute_dicts(f"SELECT {number} AS n") for number in range(8)])
        assert [rows[0]["n"] for rows in results] == list(range(8))
        assert opened == [True]
    finally:
        await client.close()


@pytest.mark.timing
@pytest.mark.asyncio
async def test_a_statement_running_past_the_timeout_is_stopped_by_the_server(clickhouse_db):
    """The native protocol's timeout bounds a wait for the server's next packet, which a running
    statement keeps sending - the server stops the statement itself (max_execution_time)."""
    client = Connections.current().create_independent("models", {"send_receive_timeout": 1})
    try:
        started = time.monotonic()
        with pytest.raises(OperationalError, match="Timeout exceeded|159"):
            await client.execute_dicts("SELECT sleep(2.5) AS slept")
        assert time.monotonic() - started < 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_migrate_runs_one_after_another_in_keeper_without_transactions(clickhouse_db):
    """Two migrate runs of one database applied the same migrations at once - the losers failed half
    way. With keeper_hosts (transactions or not) the run holds an ephemeral Keeper node the next one
    waits for."""
    clients = [
        Connections.current().create_independent("models", {"keeper_hosts": CLICKHOUSE_KEEPER_HOSTS}) for _ in range(2)
    ]
    try:
        await clients[0].execute_dicts("SELECT 1 AS one")
        assert not clients[0].features.supports_select_for_update
        order: list[str] = []
        first_holds, first_released = asyncio.Event(), asyncio.Event()

        async def migrate(index: int) -> None:
            async with clients[index].hold_migration_lock():
                order.append(f"start {index}")
                if index == 0:
                    first_holds.set()
                    await first_released.wait()
                order.append(f"end {index}")

        first = asyncio.create_task(migrate(0))
        await first_holds.wait()
        second = asyncio.create_task(migrate(1))
        # The second run gets every chance to start while the first holds the Keeper node.
        for _ in range(100):
            await asyncio.sleep(0)
        first_released.set()
        await asyncio.wait_for(asyncio.gather(first, second), WAIT_DEADLINE_SECONDS)
        assert order == ["start 0", "end 0", "start 1", "end 1"]
    finally:
        for client in clients:
            await client.close()


@pytest.mark.asyncio
async def test_a_database_named_with_a_slash_takes_a_separate_migration_lock(clickhouse_db):
    """The name went into the lock's path as it was: the lock of ``<a>/migrate`` made the node of ``<a>``'s
    lock a persistent one, and a ``migrate`` of ``<a>`` waited for its deletion forever."""
    addresses = ClickhouseMigrationLock.get_addresses(CLICKHOUSE_KEEPER_HOSTS)
    database = f"h11slash_{uuid.uuid4().hex}"
    nested_lock = ClickhouseMigrationLock(addresses, f"{database}/migrate", WAIT_DEADLINE_SECONDS)
    plain_lock = ClickhouseMigrationLock(addresses, database, WAIT_DEADLINE_SECONDS)
    try:
        await nested_lock.take()
        await asyncio.wait_for(plain_lock.take(), WAIT_DEADLINE_SECONDS)
    finally:
        await plain_lock.close()
        await nested_lock.close()
        for name in (database, f"{database}/migrate"):
            await ClickhouseDatabaseKeeperNodes(addresses, name, WAIT_DEADLINE_SECONDS).delete("")


@pytest.mark.asyncio
async def test_a_connection_opening_while_its_client_closes_is_closed_too(clickhouse_db):
    """A close while the connection was still opening found nothing to close: a statement waiting for
    it then read the client's connection as None (a raw AttributeError on clickhouse-connect), and the
    connection opened after the close stayed open. It is closed, and the statement runs on a
    connection opened anew - as after Connections.reconnect()."""
    client = Connections.current().create_independent("models", {})
    create_connection = client.create_connection
    opening = asyncio.Event()
    closed = asyncio.Event()
    opened_connections = []

    async def create_connection_after_the_close(with_db: bool) -> None:
        opening.set()
        await closed.wait()
        await create_connection(with_db)
        opened_connections.append(client._connection)

    client.create_connection = create_connection_after_the_close
    statement = asyncio.ensure_future(client.execute_dicts("SELECT 1 AS n"))
    await opening.wait()
    await client.close()
    closed.set()
    try:
        assert await statement == [{"n": 1}]
        assert len(opened_connections) == 2
        assert client._connection is opened_connections[1]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_close_while_the_connection_sets_itself_up_opens_it_anew(clickhouse_db):
    """A close between opening the connection and its first setup statement left that statement
    waiting on the lock its own task held - the client hung forever."""
    client = Connections.current().create_independent("models", {})
    post_connect = client._post_connect
    closes = []

    async def post_connect_closed_first() -> None:
        if not closes:
            closes.append(True)
            await client.close()
        await post_connect()

    client._post_connect = post_connect_closed_first
    try:
        assert await asyncio.wait_for(client.execute_dicts("SELECT 1 AS n"), 10) == [{"n": 1}]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_client_closed_and_dropped_opens_no_connection_again(clickhouse_db):
    """A client kept after close_connections() opened a connection again that nothing closed."""
    client = Connections.current().create_independent("models", {})
    await client.execute_dicts("SELECT 1 AS n")
    client.discarded = True
    await client.close()
    with pytest.raises(DBConnectionError, match="was closed and dropped"):
        await client.execute_dicts("SELECT 1 AS n")
    assert client._connection is None


@pytest.mark.requires("clickhouse-connect")
@pytest.mark.database_independent
def test_a_response_the_server_breaks_off_is_an_operational_error():
    """A statement ON CLUSTER failing on one of the servers breaks off the HTTP response - the HTTP
    library's own ClientPayloadError left execute_script() raw."""
    from aiohttp import ClientPayloadError

    from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_client import (
        ClickhouseConnectClient,
    )

    client = ClickhouseConnectClient(connection_alias="unconnected", database="unconnected")
    error = client.translate_driver_error(
        client,
        ClientPayloadError("Response payload is not completed"),
        "ALTER TABLE t ADD COLUMN c Int32",
        None,
        (),
        True,
    )
    assert isinstance(error, OperationalError)


@pytest.mark.asyncio
async def test_refused_credentials_and_a_missing_database_are_configuration_errors(clickhouse_db):
    """They were a DBConnectionError and a raw OperationalError - on PostgreSQL ConfigurationError, as
    no retry fixes them."""
    base = Connections.get("models")
    for overrides, message in (
        ({"password": "wrong"}, "Authentication failed connecting to ClickHouse"),
        # A URL without a password or a user gives None - clickhouse-driver failed on it with an AttributeError.
        ({"password": None}, "Authentication failed connecting to ClickHouse as user 'default'"),
        ({"user": None, "password": None}, "Authentication failed connecting to ClickHouse as user 'default'"),
        ({"database": "hare_missing_database"}, "Can't use database hare_missing_database"),
    ):
        settings = {"host": base.host, "port": base.port, "user": base.user, "password": base.password}
        settings["database"] = base.database
        settings.update(overrides)
        client = type(base)(connection_alias="refused", **settings)
        try:
            with pytest.raises(ConfigurationError, match=message):
                await client.execute_dicts("SELECT 1 AS n")
        finally:
            await client.close()


@pytest.mark.database_independent
def test_the_keeper_addresses_are_read():
    assert ClickhouseMigrationLock.get_addresses("keeper-1:9181, keeper-2") == (("keeper-1", 9181), ("keeper-2", 9181))
    for keeper_hosts in ("keeper-1:port", "keeper\x00-1:9181"):
        with pytest.raises(ConfigurationError):
            ClickhouseMigrationLock.get_addresses(keeper_hosts)


@pytest.mark.asyncio
async def test_a_keeper_taking_no_session_fails_the_opening_once(clickhouse_db):
    """The failed check closed the client - the opening took it for a close meanwhile and opened the
    connection again, failing the same way without end."""
    client = Connections.current().create_independent("models", {"keeper_hosts": "127.0.0.1:1", "connect_timeout": 1})
    try:
        with pytest.raises(ConfigurationError, match="which took no session"):
            await asyncio.wait_for(client.execute_dicts("SELECT 1 AS n"), WAIT_DEADLINE_SECONDS)
    finally:
        await client.close()
