"""``command_timeout`` - the optional per-command timeout on both Postgres drivers, enforced on the
hare side (PostgresqlClient._run_with_command_timeout): asyncpg's native option never fires
against a server that stopped answering, and rust_pg has no such option."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.base.constants import COMMAND_TIMEOUT_METHOD_NAMES
from hare.dialects.postgresql.drivers.asyncpg.client import (
    AsyncpgClient,
    AsyncpgTransactionClient,
)
from hare.dialects.postgresql.drivers.rust_pg.client import RustPgClient, RustPgTransactionClient
from hare.exceptions import ConfigurationError

CLIENT_CLASSES = [AsyncpgClient, RustPgClient]


def make_client(client_class, **overrides):
    kwargs = {
        "connection_alias": "default",
        "user": "postgres",
        "password": "postgres",
        "database": "test",
        "host": "127.0.0.1",
        "port": 5432,
    }
    kwargs.update(overrides)
    return client_class(**kwargs)


async def hang_forever(*args, **kwargs):
    await asyncio.sleep(3600)


@pytest.mark.database_independent
@pytest.mark.parametrize("client_class", CLIENT_CLASSES)
def test_command_timeout_defaults_to_none(client_class):
    """Backward compatibility: nothing configured means no timeout at all, as before."""
    assert make_client(client_class).command_timeout is None


@pytest.mark.database_independent
@pytest.mark.parametrize("client_class", CLIENT_CLASSES)
def test_command_timeout_from_credentials_is_cast_to_float(client_class):
    client = make_client(client_class, command_timeout="2.5")
    assert client.command_timeout == 2.5
    assert isinstance(client.command_timeout, float)
    assert "command_timeout" not in client.extra


# The check is the clients' shared __init__: every value on the first client class, the first value
# on each other one.
INVALID_COMMAND_TIMEOUTS = [0, -1, "abc", float("nan"), float("inf"), 10**9, [1]]


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("client_class", "invalid_value"),
    [
        *[(CLIENT_CLASSES[0], invalid_value) for invalid_value in INVALID_COMMAND_TIMEOUTS],
        *[(client_class, INVALID_COMMAND_TIMEOUTS[0]) for client_class in CLIENT_CLASSES[1:]],
    ],
)
def test_command_timeout_rejects_invalid_values(client_class, invalid_value):
    with pytest.raises(ConfigurationError):
        make_client(client_class, command_timeout=invalid_value)


@pytest.mark.database_independent
@pytest.mark.parametrize("scheme", ["postgresql", "postgresql+asyncpg"])
def test_command_timeout_url_param_is_cast(scheme):
    result = DbUrlConfigGenerator.expand(f"{scheme}://postgres@127.0.0.1:5432/test?command_timeout=2.5")
    assert result["credentials"]["command_timeout"] == 2.5
    assert isinstance(result["credentials"]["command_timeout"], float)


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("client_class", CLIENT_CLASSES)
async def test_command_timeout_is_not_passed_to_the_driver(client_class):
    """Enforced on the hare side - handing it to the driver too would only race that layer."""
    client = make_client(client_class, command_timeout=2.5)
    driver_entry_point = (
        "hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client.asyncpg.create_pool"
        if client_class is AsyncpgClient
        else "hare.dialects.postgresql.drivers.rust_pg.client.rust_pg_client.pg.connect"
    )
    with (
        patch(driver_entry_point, new=AsyncMock()) as driver_connect,
        patch.object(client_class, "_fetch_server_version_number", new=AsyncMock(return_value=180000)),
    ):
        await client.create_connection(with_db=True)
    assert "command_timeout" not in driver_connect.await_args.kwargs
    assert "command_timeout" not in client._template


class FakeConnectionContext:
    """Stands in for asyncpg's ``acquire_connection()`` async context manager."""

    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *exception_info):
        return False


def make_client_with_fake_driver(client_class, driver_methods, **overrides):
    """A client whose driver-level calls are replaced by ``driver_methods`` (a name -> coroutine
    function mapping) - no server needed."""
    client = make_client(client_class, **overrides)
    fake_driver = SimpleNamespace(**driver_methods)
    if client_class is AsyncpgClient:
        client.acquire_connection = lambda: FakeConnectionContext(fake_driver)
    else:
        client._pool = fake_driver
    return client


@pytest.mark.timing
@pytest.mark.asyncio
@pytest.mark.parametrize("client_class", CLIENT_CLASSES)
@pytest.mark.parametrize("method_name", ["execute", "execute_dicts"])
async def test_command_timeout_bounds_a_hanging_query(client_class, method_name):
    # asyncpg's fetch/fetchrow are the rust_pg pool's fetch_all/fetch_one - both spellings hang.
    client = make_client_with_fake_driver(
        client_class,
        {"fetch": hang_forever, "fetchrow": hang_forever, "fetch_all": hang_forever, "fetch_one": hang_forever},
        command_timeout=0.2,
    )
    arguments = ("SELECT 1", []) if method_name != "execute_dicts" else ("SELECT 1",)

    start = time.monotonic()
    # The outer wait_for only keeps a regression from hanging the suite forever.
    with pytest.raises(TimeoutError, match="0.2s"):
        await asyncio.wait_for(getattr(client, method_name)(*arguments), 5)
    assert time.monotonic() - start < 2.0


@pytest.mark.asyncio
@pytest.mark.parametrize("client_class", CLIENT_CLASSES)
async def test_command_timeout_bounds_a_hanging_write_and_script(client_class):
    client = make_client_with_fake_driver(
        client_class,
        {"execute": hang_forever, "execute_script": hang_forever},
        command_timeout=0.2,
    )

    with pytest.raises(TimeoutError, match="0.2s"):
        await asyncio.wait_for(client.execute("UPDATE t SET a = 1", []), 5)
    with pytest.raises(TimeoutError, match="0.2s"):
        await asyncio.wait_for(client.execute_script("SELECT 1"), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("client_class", CLIENT_CLASSES)
async def test_command_timeout_does_not_disturb_a_fast_query(client_class):
    async def fast_fetch(*args):
        # Suspends like a query waiting for the server.
        await asyncio.sleep(0)
        return ["row"]

    client = make_client_with_fake_driver(
        client_class, {"fetch": fast_fetch, "fetch_all": fast_fetch}, command_timeout=5
    )
    assert await client.execute("SELECT 1", []) == (1, ["row"])


@pytest.mark.asyncio
@pytest.mark.parametrize("client_class", CLIENT_CLASSES)
async def test_without_command_timeout_a_slow_query_is_never_cut_off(client_class):
    """Default None: a slow query just runs to completion, exactly as before."""

    async def slow_fetch(*args):
        for _ in range(100):
            await asyncio.sleep(0)
        return ["row"]

    client = make_client_with_fake_driver(client_class, {"fetch": slow_fetch, "fetch_all": slow_fetch})
    assert await client.execute("SELECT 1", []) == (1, ["row"])


@pytest.mark.asyncio
async def test_rust_pg_command_timeout_applies_inside_a_transaction():
    client = make_client(RustPgClient, command_timeout=0.2)
    client._pool = SimpleNamespace()
    transaction_wrapper = RustPgTransactionClient(client)
    transaction_wrapper._native_transaction = SimpleNamespace(fetch_all=hang_forever)

    with pytest.raises(TimeoutError, match="0.2s"):
        await asyncio.wait_for(transaction_wrapper.execute("SELECT 1", []), 5)


@pytest.mark.asyncio
async def test_asyncpg_command_timeout_applies_inside_a_transaction():
    client = make_client(AsyncpgClient, command_timeout=0.2)
    transaction_wrapper = AsyncpgTransactionClient(client)
    transaction_wrapper._connection = SimpleNamespace(fetch=hang_forever)

    with pytest.raises(TimeoutError, match="0.2s"):
        await asyncio.wait_for(transaction_wrapper.execute("SELECT 1", []), 5)


@pytest.mark.database_independent
def test_command_timeout_does_not_bound_transaction_control_methods():
    """Cancelling a commit/rollback/begin mid-flight would leave the transaction state unknown."""
    for method_name in ("begin", "commit", "rollback", "savepoint", "stream"):
        assert method_name not in COMMAND_TIMEOUT_METHOD_NAMES
    assert {"execute", "execute_described", "execute_many", "execute_script", "copy"} <= (COMMAND_TIMEOUT_METHOD_NAMES)


@pytest.mark.timing
@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_real_postgres_command_timeout_cancels_slow_query_and_connection_stays_usable(db_simple):
    """Both drivers against a real server: a query slower than command_timeout raises TimeoutError
    well before it would have finished, and the (single-connection) pool works afterwards."""
    shared = Connections.get("models")
    client = type(shared)(
        connection_alias="command_timeout_test",
        user=shared.user,
        password=shared.password,
        database=shared.database,
        host=shared.host,
        port=shared.port,
        min_size=1,
        max_size=1,
        command_timeout=0.3,
    )
    try:
        await client.create_connection(with_db=True)
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.execute("SELECT pg_sleep(5)"), 30)
        assert time.monotonic() - start < 3.0
        assert await client.ping() is True
        _, rows = await client.execute("SELECT 1 AS value")
        assert rows[0]["value"] == 1
    finally:
        await client.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_real_postgres_without_command_timeout_slow_query_completes(db_simple):
    shared = Connections.get("models")
    client = type(shared)(
        connection_alias="command_timeout_default_test",
        user=shared.user,
        password=shared.password,
        database=shared.database,
        host=shared.host,
        port=shared.port,
    )
    assert client.command_timeout is None
    try:
        await client.create_connection(with_db=True)
        _, rows = await client.execute("SELECT pg_sleep(0.5), 1 AS value")
        assert rows[0]["value"] == 1
    finally:
        await client.close()


@pytest.mark.timing
@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_real_postgres_command_timeout_stops_the_query_on_the_server(db_simple):
    """The timed-out query is cancelled on the server too - through a pooler (PgBouncer) as well,
    which forwards a cancel only while the client connection it names is still open."""
    shared = Connections.get("models")
    client = type(shared)(
        connection_alias="command_timeout_server_test",
        user=shared.user,
        password=shared.password,
        database=shared.database,
        host=shared.host,
        port=shared.port,
        min_size=1,
        max_size=1,
        command_timeout=0.3,
    )
    marker = f"command_timeout_marker_{time.monotonic_ns()}"
    try:
        await client.create_connection(with_db=True)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.execute(f"SELECT pg_sleep(5) AS {marker}"), 30)
        deadline = time.monotonic() + 2
        while True:
            _, rows = await shared.execute(
                "SELECT count(*) AS running FROM pg_stat_activity "
                f"WHERE state = 'active' AND query LIKE '%{marker}%' AND pid <> pg_backend_pid()"
            )
            if rows[0]["running"] == 0 or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.1)
        assert rows[0]["running"] == 0
    finally:
        await client.close()


class FreezingProxy:
    """A TCP proxy in front of the test server that can stop passing bytes on - a server that stopped
    answering without closing its connections."""

    def __init__(self, host: str, port: int) -> None:
        self.target = (host, port)
        self.flowing = asyncio.Event()
        self.flowing.set()
        self.writers: list[asyncio.StreamWriter] = []
        self.server: asyncio.Server | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self.connect, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def connect(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        server_reader, server_writer = await asyncio.open_connection(*self.target)
        self.writers += [client_writer, server_writer]
        await asyncio.gather(self.pass_on(client_reader, server_writer), self.pass_on(server_reader, client_writer))

    async def pass_on(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                await self.flowing.wait()
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def stop(self) -> None:
        self.flowing.set()
        for writer in self.writers:
            writer.transport.abort()
        if self.server is not None:
            self.server.close()


@pytest.mark.timing
@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_real_postgres_command_timeout_ends_a_command_of_a_server_that_stopped_answering(db_simple):
    """asyncpg handed the connection back only once its cancel request was answered - over a network
    that stopped answering, never: the call hung until the network came back."""
    shared = Connections.get("models")
    proxy = FreezingProxy(shared.host, shared.port)
    client = type(shared)(
        connection_alias="command_timeout_frozen_test",
        user=shared.user,
        password=shared.password,
        database=shared.database,
        host="127.0.0.1",
        port=await proxy.start(),
        min_size=1,
        max_size=1,
        command_timeout=0.3,
    )
    try:
        await client.create_connection(with_db=True)
        await client.execute("SELECT 1")
        proxy.flowing.clear()
        with pytest.raises(TimeoutError, match="Command timed out"):
            await asyncio.wait_for(client.execute("SELECT 1"), 10)
    finally:
        await proxy.stop()
        await client.close()
