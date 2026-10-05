"""SQLite's aiosqlite driver is picked by its own scheme - as a DB_URL scheme and as a connection
config's ``engine`` alike; the dialect's plain scheme picks no driver."""

import pytest

from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.sqlite.client import SqliteClient
from hare.dialects.sqlite.drivers.aiosqlite.aiosqlite_driver import AiosqliteDriver
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
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
def test_the_aiosqlite_client_is_the_shared_sqlite_client_of_its_driver():
    driver = DialectRegistry.get_driver("sqlite+aiosqlite")
    assert driver.get_client_class({}) is AiosqliteClient
    assert all(issubclass(client_class, SqliteClient) for client_class in driver.get_client_classes())


@pytest.mark.asyncio
async def test_a_client_of_the_explicit_engine_runs_statements():
    credentials = DbUrlConfigGenerator.expand("sqlite+aiosqlite://:memory:")["credentials"]
    client = DialectRegistry.get_driver("sqlite+aiosqlite").get_client_class(credentials)(
        connection_alias="explicit", **credentials
    )
    await client.create_connection(with_db=True)
    try:
        result = await client.execute("SELECT 1 + 1 AS total")
        assert [row["total"] for row in result.rows] == [2]
    finally:
        await client.close()


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
