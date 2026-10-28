"""Deleting a database with ``keeper_hosts`` deletes its nodes of ClickHouse Keeper - the series of its keys
and the node of its migration lock: a server keeps at most ``max_autoincrement_series`` series."""

import urllib.parse
import uuid

import pytest

from hare import Connections
from hare.contrib.test import hare_test_context
from hare.dialects.clickhouse.keeper.constants import KEEPER_MIGRATION_LOCKS_PATH
from tests.dialects.clickhouse.constants import CLICKHOUSE_KEEPER_HOSTS
from tests.dialects.clickhouse.generated_keys.models import Badge, Ticket
from tests.utils.database_under_test import DatabaseUnderTest

SERIES_NODES_SQL = (
    "SELECT name FROM system.zookeeper WHERE path = "
    "(SELECT value FROM system.server_settings WHERE name = 'series_keeper_path')"
)
MIGRATION_LOCK_NODES_SQL = f"SELECT name FROM system.zookeeper WHERE path = '{KEEPER_MIGRATION_LOCKS_PATH}'"


async def get_series_databases(connection) -> set[str]:
    """The databases of the server's series - a series is named ``<database>.<app_label>.<Model>``."""
    rows = await connection.execute_dicts(SERIES_NODES_SQL)
    return {urllib.parse.unquote(row["name"]).rsplit(".", 2)[0] for row in rows}


async def get_migration_lock_databases(connection) -> set[str]:
    return {row["name"] for row in await connection.execute_dicts(MIGRATION_LOCK_NODES_SQL)}


def get_keeper_url(database: str | None = None) -> str:
    """HARE_TEST_DB naming the server's Keeper - and another database, when one is given."""
    db_url = DatabaseUnderTest.get_url()
    if "keeper_hosts=" not in db_url:
        db_url = DatabaseUnderTest.get_url_with_options(f"keeper_hosts={CLICKHOUSE_KEEPER_HOSTS}")
    if database is None:
        return db_url
    server, _, database_and_query = db_url.rpartition("/")
    _, question_mark, query = database_and_query.partition("?")
    return f"{server}/{urllib.parse.quote(database, safe='')}{question_mark}{query}"


def open_context(db_url: str):
    return hare_test_context(
        modules=["tests.dialects.clickhouse.generated_keys.models"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    )


@pytest.mark.asyncio
async def test_deleting_a_database_deletes_its_key_series_and_its_migration_lock_node(
    clickhouse_generated_keys_module,
):
    observer = Connections.get("models")
    await Ticket.objects.create(subject="kept")
    async with open_context(get_keeper_url()):
        connection = Connections.get("models")
        database = connection.database
        await Ticket.objects.create(subject="first")
        await Badge.objects.create(label="first")
        async with connection.hold_migration_lock():
            pass
        assert database in await get_series_databases(connection)
        assert database in await get_migration_lock_databases(connection)

    assert database not in await get_series_databases(observer)
    assert database not in await get_migration_lock_databases(observer)
    # The database of the module, deleted at its end, keeps its own series meanwhile.
    assert observer.database in await get_series_databases(observer)


@pytest.mark.asyncio
async def test_deleting_a_database_keeps_the_series_of_a_database_named_after_it_with_a_dot(
    clickhouse_generated_keys_module,
):
    """Deleting database ``a`` deleted the series of database ``a.b`` too - its keys started again from the
    first one, a process that had moved the series handing out keys its table holds."""
    observer = Connections.get("models")
    database = f"h11keeper_{uuid.uuid4().hex}"
    async with open_context(get_keeper_url(f"{database}.dotted")):
        await Ticket.objects.create(subject="dotted")
        async with open_context(get_keeper_url(database)):
            await Ticket.objects.create(subject="plain")
        assert f"{database}.dotted" in await get_series_databases(observer)
        assert database not in await get_series_databases(observer)
    assert f"{database}.dotted" not in await get_series_databases(observer)


@pytest.mark.asyncio
async def test_deleting_a_database_dropped_already_deletes_its_key_series(clickhouse_generated_keys_module):
    observer = Connections.get("models")
    async with open_context(get_keeper_url()):
        connection = Connections.get("models")
        database = connection.database
        await Ticket.objects.create(subject="first")
        await observer.execute_script(f"DROP DATABASE {connection.dialect.literals.quote_identifier(database)}")
        assert database in await get_series_databases(observer)

    assert database not in await get_series_databases(observer)
