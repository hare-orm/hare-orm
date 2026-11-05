"""The shadow database ``hare drift`` builds the models in on ClickHouse: a shadow created before a failure
on entering stayed on the server - no ``__aexit__()`` follows a failed ``__aenter__()``."""

from types import SimpleNamespace

import pytest

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.clickhouse_engine import ClickhouseEngine
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
from hare.dialects.clickhouse.schema.shadow.clickhouse_shadow_database import ClickhouseShadowDatabase
from hare.exceptions import UnSupportedError
from hare.query.expressions import F
from tests.dialects.clickhouse.models import Player


@pytest.mark.database_independent
@pytest.mark.parametrize("database", ["sales", "archive", RawSQLTerm("currentDatabase()")])
def test_distributed_target_follows_the_shadow_database(database):
    shadow = ClickhouseShadowDatabase(SimpleNamespace(dialect=CLICKHOUSE_DIALECT), ["archive"])
    shadow.database = "sales"
    shadow.real_database_by_shadow_database = {shadow.prefix: "sales", f"{shadow.prefix}_archive": "archive"}
    engine = ClickhouseEngine("Distributed", "cluster", database, "events", F("id"))
    options = shadow.get_shadow_table_options(ClickhouseTableOptions(engine=engine))
    expected = (
        database
        if isinstance(database, RawSQLTerm)
        else shadow.prefix
        if database == "sales"
        else f"{shadow.prefix}_archive"
    )
    assert options.get_engine().arguments == ("cluster", expected, "events", F("id"))
    assert engine.arguments == ("cluster", database, "events", F("id"))


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "engine",
    [
        ClickhouseEngine("Distributed", "cluster", "external", "events"),
        ClickhouseEngine("Distributed", "cluster", RawSQLTerm("databaseFromSetting()"), "events"),
        ClickhouseEngine("Distributed", sql=RawSQLTerm("Distributed('cluster', 'sales', 'events')")),
    ],
)
def test_shadow_refuses_a_distributed_target_it_cannot_isolate(engine):
    shadow = ClickhouseShadowDatabase(SimpleNamespace(dialect=CLICKHOUSE_DIALECT), [])
    shadow.real_database_by_shadow_database = {shadow.prefix: "sales"}
    with pytest.raises(UnSupportedError, match="Distributed"):
        shadow.get_shadow_table_options(ClickhouseTableOptions(engine=engine))


@pytest.mark.asyncio
async def test_a_failure_on_entering_drops_the_shadows_created(clickhouse_db, monkeypatch):
    connection = Player._meta.connection
    create_shadow_database = ClickhouseShadowDatabase.create_shadow_database

    async def create_then_fail(self, shadow_database, database, engine_rows):
        if self.real_database_by_shadow_database:
            raise RuntimeError("the second shadow failed")
        await create_shadow_database(self, shadow_database, database, engine_rows)

    monkeypatch.setattr(ClickhouseShadowDatabase, "create_shadow_database", create_then_fail)
    shadow = ClickhouseShadowDatabase(connection, ["other_database_of_the_models"])

    with pytest.raises(RuntimeError, match="the second shadow failed"):
        async with shadow:
            pass

    assert (
        await connection.execute_dicts("SELECT name FROM system.databases WHERE startsWith(name, $1)", [shadow.prefix])
        == []
    )


@pytest.mark.asyncio
async def test_a_replica_path_naming_the_database_keeps_it_back_from_the_shadow(clickhouse_db):
    """The server fills a replica path's {database} with the shadow's name, which the shadow's Keeper path
    begins with - taking that prefix off took the database out of the path, and drift reported the table's
    options changed."""
    shadow = ClickhouseShadowDatabase(Player._meta.connection, [])
    shadow.database = "sales"
    shadow.real_database_by_shadow_database = {shadow.prefix: "sales", f"{shadow.prefix}_archive": "archive"}
    engine = (
        f"ReplicatedMergeTree('/{shadow.prefix}/clickhouse/tables/{{replica}}/{shadow.prefix}/item_local', "
        "'{replica}')"
    )
    other_engine = f"ReplicatedMergeTree('/{shadow.prefix}/clickhouse/{shadow.prefix}_archive/item', '{{replica}}')"

    assert shadow.get_catalog_identities().map_engine(engine) == (
        "ReplicatedMergeTree('/clickhouse/tables/{replica}/sales/item_local', '{replica}')"
    )
    assert (
        shadow.get_catalog_identities().map_engine(other_engine)
        == "ReplicatedMergeTree('/clickhouse/archive/item', '{replica}')"
    )
