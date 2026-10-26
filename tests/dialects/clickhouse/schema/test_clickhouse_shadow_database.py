"""The shadow database ``hare drift`` builds the models in on ClickHouse: a shadow created before a failure
on entering stayed on the server - no ``__aexit__()`` follows a failed ``__aenter__()``."""

import pytest

from hare.dialects.clickhouse.schema.shadow.clickhouse_shadow_database import ClickhouseShadowDatabase
from tests.dialects.clickhouse.models import Player


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

    assert shadow.replace_shadow_names(engine) == (
        "ReplicatedMergeTree('/clickhouse/tables/{replica}/sales/item_local', '{replica}')"
    )
    assert shadow.replace_shadow_names(other_engine) == "ReplicatedMergeTree('/clickhouse/archive/item', '{replica}')"
