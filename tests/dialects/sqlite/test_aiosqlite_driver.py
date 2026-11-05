"""SQLite's aiosqlite driver is picked by its own scheme - as a DB_URL scheme and as a connection
config's ``engine`` alike; the dialect's plain scheme picks no driver."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.sqlite.client import SqliteClient
from hare.dialects.sqlite.drivers.aiosqlite.aiosqlite_driver import AiosqliteDriver
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client_with_regexp_support import (
    AiosqliteClientWithRegexpSupport,
)
from hare.dialects.sqlite.functions.regex.sqlite_posix_regex import SqlitePosixRegex
from hare.exceptions import ConfigurationError

pytestmark = pytest.mark.sqlite_only


@pytest.mark.database_independent
def test_the_explicit_scheme_picks_the_aiosqlite_driver():
    assert DbUrlConfigGenerator.expand("sqlite+aiosqlite:///some/test.sqlite")["engine"] == "sqlite+aiosqlite"
    assert isinstance(DialectRegistry.get_driver_for_url_scheme("sqlite+aiosqlite"), AiosqliteDriver)
    assert isinstance(DialectRegistry.get_driver("sqlite+aiosqlite"), AiosqliteDriver)


@pytest.mark.database_independent
def test_the_plain_scheme_picks_no_driver():
    with pytest.raises(ConfigurationError, match="Unknown database URL scheme 'sqlite'"):
        DbUrlConfigGenerator.expand("sqlite:///some/test.sqlite")
    with pytest.raises(ConfigurationError, match="Unknown database engine 'sqlite'"):
        DialectRegistry.get_driver("sqlite")


@pytest.mark.database_independent
def test_an_unknown_engine_lists_the_schemes_too():
    with pytest.raises(ConfigurationError, match=r"sqlite\+aiosqlite"):
        DialectRegistry.get_driver("no_such_engine")


@pytest.mark.database_independent
@pytest.mark.parametrize("file_path", ["example.sqlite", b"example.sqlite", Path("example.sqlite")])
def test_the_client_normalizes_filesystem_paths(file_path):
    client = AiosqliteClient(file_path=file_path, connection_alias="default")
    assert client.filename == "example.sqlite"


@pytest.mark.database_independent
def test_the_aiosqlite_client_is_the_shared_sqlite_client_of_its_driver():
    driver = DialectRegistry.get_driver("sqlite+aiosqlite")
    assert driver.get_client_class({}) is AiosqliteClient
    assert all(issubclass(client_class, SqliteClient) for client_class in driver.get_client_classes())


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_read", [False, True])
async def test_a_client_of_the_explicit_engine_runs_statements(retry_read):
    credentials = DbUrlConfigGenerator.expand("sqlite+aiosqlite://:memory:")["credentials"]
    client = DialectRegistry.get_driver("sqlite+aiosqlite").get_client_class(credentials)(
        connection_alias="explicit", **credentials
    )
    await client.create_connection(with_db=True)
    try:
        result = await client.execute("SELECT 1 + 1 AS total")
        assert [row["total"] for row in result.rows] == [2]
        assert await client.execute_dicts('SELECT ? AS "a", ? AS "A"', [1, 2], retry_read=retry_read) == [
            {"a": 1, "A": 2}
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.database_independent
@pytest.mark.parametrize("setup_error", [ValueError, asyncio.CancelledError])
@pytest.mark.parametrize("release_fails", [False, True])
async def test_failed_setup_releases_connection_and_preserves_errors(monkeypatch, setup_error, release_fails):
    client = AiosqliteClient(file_path=":memory:", connection_alias="default")
    error = setup_error()
    release_error = RuntimeError("release failed") if release_fails else None
    connection = SimpleNamespace(close=AsyncMock(side_effect=release_error))
    opening = asyncio.get_running_loop().create_future()
    opening._thread = SimpleNamespace(daemon=False)
    opening.set_result(connection)
    monkeypatch.setattr("aiosqlite.connect", lambda *args, **kwargs: opening)
    monkeypatch.setattr(client, "_set_up_connection", AsyncMock(side_effect=error))

    if release_fails:
        with pytest.raises(BaseExceptionGroup) as raised:
            await client.create_connection(with_db=True)
        assert raised.value.exceptions == (error, release_error)
    else:
        with pytest.raises(setup_error) as raised:
            await client.create_connection(with_db=True)
        assert raised.value is error
    connection.close.assert_awaited_once_with()
    assert client._connection is None


@pytest.mark.asyncio
@pytest.mark.database_independent
async def test_regexp_installation_is_part_of_connection_setup(monkeypatch):
    client = AiosqliteClientWithRegexpSupport(file_path=":memory:", connection_alias="default")
    connection = object()
    client._connection = connection
    common_setup = AsyncMock()
    install = AsyncMock()
    monkeypatch.setattr(AiosqliteClient, "_post_connect", common_setup)
    monkeypatch.setattr(SqlitePosixRegex, "install", install)

    await client._post_connect()

    common_setup.assert_awaited_once_with()
    install.assert_awaited_once_with(connection, client.function_errors)


@pytest.mark.asyncio
async def test_a_client_closed_and_dropped_opens_no_connection_again(tmp_path):
    """A client kept after close_connections() opened a connection again that nothing closed - the
    other drivers refuse it."""
    from hare import Hare, HareConfig
    from hare.core.connections.connections import Connections
    from hare.exceptions import DBConnectionError

    database_url = "sqlite+aiosqlite://" + (tmp_path / "closed.sqlite3").as_posix()
    await Hare.init(HareConfig.from_db_url(database_url, {"models": ["tests.contrib.frameworks.models"]}))
    client = Connections.get("default")
    await client.execute_dicts("SELECT 1 AS n")
    await Hare.close_connections()
    with pytest.raises(DBConnectionError, match="was closed and dropped"):
        await client.execute_dicts("SELECT 1 AS n")
    assert client._connection is None
