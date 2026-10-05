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
