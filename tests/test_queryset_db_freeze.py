"""A queryset is a description of a query: the tenant its default scope filters by and the tenant
a sharding router routes by are both the one active when it is awaited, never the one active when
it was built. One queryset, built with no tenant active (as at import time), runs for whichever
tenant awaits it - the connection is still resolved lazily, so an await inside a transaction
opened after the query was built runs on that transaction's connection.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
import pytest_asyncio

from hare import fields
from hare.core.context import HareContext
from hare.dialects.base.db_url import DbUrlConfigGenerator
from hare.models import Model
from hare.models.tenancy import Tenancy
from hare.query.functions import Length
from hare.transactions.transactions import Transactions

MODULE_NAME = "tests._queryset_db_freeze_models"


class TenantShardRouter:
    """Shards by the active tenant: "A" -> shard_a, "B" -> shard_b, no opinion otherwise."""

    SHARD_BY_TENANT = {"A": "shard_a", "B": "shard_b"}

    def db_for_read(self, model):
        return self.SHARD_BY_TENANT.get(Tenancy.current.get())

    def db_for_write(self, model):
        return self.SHARD_BY_TENANT.get(Tenancy.current.get())


class ReadWriteSplitRouter:
    def db_for_read(self, model):
        return "read_replica"

    def db_for_write(self, model):
        return "write_primary"


async def _make_context(tmp_path, connection_names: list[str], router: type, app_label: str):
    class ShardWidget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=50)
        company_id = fields.CharField(max_length=10)

        class Meta:
            app = app_label
            tenant_field = "company_id"

    module = types.ModuleType(MODULE_NAME)
    setattr(module, "ShardWidget", ShardWidget)  # noqa: B010
    sys.modules[MODULE_NAME] = module

    connections = {
        name: DbUrlConfigGenerator.expand(f"sqlite:///{tmp_path / f'{name}.sqlite'}?synchronous=OFF", testing=True)
        for name in connection_names
    }
    context = HareContext()
    await context.__aenter__()
    await context.init(
        config={
            "connections": connections,
            "apps": {app_label: {"models": [MODULE_NAME], "default_connection": connection_names[0]}},
        },
        routers=[router],
        _create_db=True,
    )
    await context.generate_schemas()
    first_connection = context.connections.get(connection_names[0])
    for name in connection_names[1:]:
        await context.connections.get(name).execute_script(first_connection.get_schema_sql(safe=True))
    return context, ShardWidget


@pytest_asyncio.fixture
async def sharded(tmp_path):
    context, shard_widget = await _make_context(tmp_path, ["shard_a", "shard_b"], TenantShardRouter, "freeze_shards")
    try:
        with Tenancy.scope("A"):
            await shard_widget.objects.create(name="w-a")
            await shard_widget.objects.create(name="w-a-2")
        with Tenancy.scope("B"):
            await shard_widget.objects.create(name="w-b")
        yield context, shard_widget
    finally:
        await context.__aexit__(None, None, None)
        sys.modules.pop(MODULE_NAME, None)


@pytest_asyncio.fixture
async def read_write_split(tmp_path):
    context, shard_widget = await _make_context(
        tmp_path, ["write_primary", "read_replica"], ReadWriteSplitRouter, "freeze_split"
    )
    try:
        yield context, shard_widget
    finally:
        await context.__aexit__(None, None, None)
        sys.modules.pop(MODULE_NAME, None)


def _build(shard_widget: Any, build_type: str) -> Any:
    if build_type == "filter":
        return shard_widget.objects.filter(name__startswith="w-a").order_by("id")
    if build_type == "exclude":
        return shard_widget.objects.exclude(name="never").order_by("id")
    if build_type == "annotate":
        return shard_widget.objects.annotate(name_length=Length("name")).order_by("id")
    if build_type == "latest":
        return shard_widget.objects.latest("id")
    return shard_widget.objects.earliest("id")


EXPECTED_NAMES_BY_BUILD_TYPE = {
    "filter": ["w-a", "w-a-2"],
    "exclude": ["w-a", "w-a-2"],
    "annotate": ["w-a", "w-a-2"],
    "latest": "w-a-2",
    "earliest": "w-a",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("build_type", ["filter", "exclude", "annotate", "latest", "earliest"])
async def test_queryset_built_without_a_tenant_runs_for_the_tenant_active_when_awaited(sharded, build_type):
    _, shard_widget = sharded
    queryset = _build(shard_widget, build_type)
    with Tenancy.scope("A"):
        result = await queryset
    if isinstance(result, list):
        assert [widget.name for widget in result] == EXPECTED_NAMES_BY_BUILD_TYPE[build_type]
    else:
        assert result.name == EXPECTED_NAMES_BY_BUILD_TYPE[build_type]


@pytest.mark.asyncio
async def test_one_queryset_runs_for_each_tenant_that_awaits_it(sharded):
    _, shard_widget = sharded
    queryset = shard_widget.objects.all().order_by("id")
    with Tenancy.scope("A"):
        assert [widget.name for widget in await queryset.all()] == ["w-a", "w-a-2"]
    with Tenancy.scope("B"):
        assert [widget.name for widget in await queryset.all()] == ["w-b"]
    with Tenancy.scope("A"):
        assert [widget.name for widget in await queryset] == ["w-a", "w-a-2"]


@pytest.mark.asyncio
async def test_queryset_built_under_one_tenant_runs_for_the_tenant_active_when_awaited(sharded):
    _, shard_widget = sharded
    with Tenancy.scope("A"):
        queryset = shard_widget.objects.all().order_by("id")
    with Tenancy.scope("B"):
        assert [widget.name for widget in await queryset] == ["w-b"]


@pytest.mark.asyncio
async def test_first_and_last_run_for_the_tenant_active_when_awaited(sharded):
    _, shard_widget = sharded
    first_query = shard_widget.objects.first()
    last_query = shard_widget.objects.last()
    with Tenancy.scope("A"):
        first_result = await first_query
        last_result = await last_query
    assert first_result is not None
    assert first_result.name == "w-a"
    assert last_result is not None
    assert last_result.name == "w-a-2"


@pytest.mark.asyncio
async def test_bulk_create_routes_by_the_tenant_active_when_awaited(sharded):
    context, shard_widget = sharded
    with Tenancy.scope("A"):
        query = shard_widget.objects.bulk_create([shard_widget(name="bulk-create", company_id="B")])
    with Tenancy.scope("B"):
        await query
    shard_a = context.connections.get("shard_a")
    shard_b = context.connections.get("shard_b")
    _, shard_a_rows = await shard_a.execute("SELECT name FROM shardwidget WHERE name = 'bulk-create'")
    _, shard_b_rows = await shard_b.execute("SELECT name FROM shardwidget WHERE name = 'bulk-create'")
    assert shard_a_rows == []
    assert [row["name"] for row in shard_b_rows] == ["bulk-create"]


@pytest.mark.asyncio
async def test_bulk_update_runs_for_the_tenant_active_when_awaited(sharded):
    context, shard_widget = sharded
    with Tenancy.scope("A"):
        widget = await shard_widget.objects.create(name="bulk-update-original")
        widget.name = "bulk-update-updated"
        query = shard_widget.objects.bulk_update([widget], fields=["name"])
        await query
    shard_a = context.connections.get("shard_a")
    _, shard_a_rows = await shard_a.execute("SELECT name FROM shardwidget WHERE name = 'bulk-update-updated'")
    assert [row["name"] for row in shard_a_rows] == ["bulk-update-updated"]


@pytest.mark.asyncio
async def test_derived_queries_run_for_the_tenant_active_when_awaited(sharded):
    """count()/exists()/update()/delete() build their own query object from the filtered
    queryset - each runs for the tenant active when it is awaited, like the queryset itself."""
    context, shard_widget = sharded
    count_query = shard_widget.objects.filter(name__startswith="w-a").count()
    exists_query = shard_widget.objects.filter(name="w-a").exists()
    update_query = shard_widget.objects.filter(name="w-a").update(name="w-a-renamed")
    with Tenancy.scope("B"):
        assert await count_query == 0
        assert await exists_query is False
    with Tenancy.scope("A"):
        assert await count_query == 2
        assert await exists_query is True
        assert await update_query == 1
    shard_a = context.connections.get("shard_a")
    shard_b = context.connections.get("shard_b")
    _, shard_a_rows = await shard_a.execute("SELECT name FROM shardwidget ORDER BY id")
    _, shard_b_rows = await shard_b.execute("SELECT name FROM shardwidget ORDER BY id")
    assert [row["name"] for row in shard_a_rows] == ["w-a-renamed", "w-a-2"]
    assert [row["name"] for row in shard_b_rows] == ["w-b"]

    delete_query = shard_widget.objects.filter(name="w-a-2").delete()
    with Tenancy.scope("A"):
        assert await delete_query == 1
    _, shard_a_rows = await shard_a.execute("SELECT name FROM shardwidget ORDER BY id")
    assert [row["name"] for row in shard_a_rows] == ["w-a-renamed"]


@pytest.mark.asyncio
async def test_query_built_before_a_transaction_and_awaited_inside_it_joins_the_transaction(sharded):
    """The connection stays lazy: an update built BEFORE the transaction opened must still run
    on (and roll back with) that transaction's connection, not a pre-bound outer one."""
    context, shard_widget = sharded
    with Tenancy.scope("A"):
        update_query = shard_widget.objects.filter(name="w-a").update(name="w-a-renamed")
        with pytest.raises(KeyError):
            async with Transactions.atomic(using="shard_a"):
                await update_query
                raise KeyError("rollback")
        assert [
            widget.name for widget in await shard_widget.objects.filter(name__startswith="w-a").order_by("id")
        ] == [
            "w-a",
            "w-a-2",
        ]


@pytest.mark.asyncio
async def test_using_db_after_filter_still_overrides_the_router_chosen_connection(sharded):
    context, shard_widget = sharded
    with Tenancy.scope("A"):
        queryset = shard_widget.objects.filter(name="w-a")
    shard_b = context.connections.get("shard_b")
    with Tenancy.scope("A"):
        # Tenant A's WHERE on shard_b, which holds no tenant-A rows - the override took effect
        # (the router's shard_a connection was replaced), it wasn't ignored.
        assert await queryset.using(shard_b) == []
        assert [widget.name for widget in await queryset] == ["w-a"]


@pytest.mark.asyncio
async def test_filter_update_and_delete_still_run_on_the_write_connection(read_write_split):
    """A chained .update()/.delete() must keep asking the router for a WRITE connection, not
    reuse a read one."""
    context, shard_widget = read_write_split
    write_db = context.connections.get("write_primary")
    read_db = context.connections.get("read_replica")
    with Tenancy.scope("A"):
        widget = await shard_widget.objects.create(name="original")

        await shard_widget.objects.filter(id=widget.id).update(name="updated")
        _, write_rows = await write_db.execute("SELECT name FROM shardwidget")
        _, read_rows = await read_db.execute("SELECT name FROM shardwidget")
        assert [row["name"] for row in write_rows] == ["updated"]
        assert read_rows == []

        await shard_widget.objects.filter(id=widget.id).delete()
        _, write_rows = await write_db.execute("SELECT name FROM shardwidget")
        assert write_rows == []


@pytest.mark.asyncio
async def test_explicit_using_db_survives_into_chained_update(read_write_split):
    """An explicitly pinned connection (here the read replica, deliberately) must NOT be
    re-resolved to the router's write connection by .update()."""
    context, shard_widget = read_write_split
    read_db = context.connections.get("read_replica")
    with Tenancy.scope("A"):
        widget = await shard_widget.objects.using(read_db).create(name="original")
        await shard_widget.objects.filter(id=widget.id).using(read_db).update(name="updated")
        await shard_widget.objects.using(read_db).all().update(name="updated-again")
    _, read_rows = await read_db.execute("SELECT name FROM shardwidget")
    assert [row["name"] for row in read_rows] == ["updated-again"]
