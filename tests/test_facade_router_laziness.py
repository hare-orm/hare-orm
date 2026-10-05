"""Regression coverage for bulk_create()/bulk_update()/all()/first()/last() resolving their
router lazily, at await time, instead of synchronously when the facade method is called - the
same contract filter()/exclude()/.update()/.delete() already honor. Before this fix, these five
methods called cls.get_connection()/router synchronously inside the facade call itself: a router
reconfigured between construction and await was never seen, and a router that raises surfaced
its exception at the synchronous call instead of at await.

Uses a dynamically-registered, throwaway model module (injected into sys.modules), following the
same pattern as test_queryset_db_freeze.py.
"""

from __future__ import annotations

import sys
import types

import pytest
import pytest_asyncio

from hare import fields
from hare.core.hare_context import HareContext
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.models import Model

MODULE_NAME = "tests._facade_router_laziness_models"


class InstrumentedRouter:
    """A single router combining every failure mode bulk_create()/bulk_update()/all()/first()/
    last() used to get wrong by resolving synchronously at call time instead of lazily at await:
    not seeing a config change (ALIAS) made between construction and await, not deferring an
    exception (RAISE) to await, and being consulted before await at all (READ_CALLS/WRITE_CALLS).
    """

    ALIAS = "shard_a"
    RAISE = False
    READ_CALLS = 0
    WRITE_CALLS = 0

    def db_for_read(self, model):
        InstrumentedRouter.READ_CALLS += 1
        if InstrumentedRouter.RAISE:
            raise RuntimeError("router exploded")
        return InstrumentedRouter.ALIAS

    def db_for_write(self, model):
        InstrumentedRouter.WRITE_CALLS += 1
        if InstrumentedRouter.RAISE:
            raise RuntimeError("router exploded")
        return InstrumentedRouter.ALIAS


async def _make_context(tmp_path, app_label: str):
    class Widget(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=50)

        class Meta:
            app = app_label

    module = types.ModuleType(MODULE_NAME)
    setattr(module, "Widget", Widget)  # noqa: B010
    sys.modules[MODULE_NAME] = module

    connections = {
        name: DbUrlConfigGenerator.expand(
            f"sqlite+aiosqlite:///{tmp_path / f'{name}.sqlite'}?synchronous=OFF", testing=True
        )
        for name in ("shard_a", "shard_b")
    }
    context = HareContext()
    await context.__aenter__()
    await context.init(
        config={
            "connections": connections,
            "apps": {app_label: {"models": [MODULE_NAME], "default_connection": "shard_a"}},
        },
        routers=[InstrumentedRouter],
        _create_db=True,
    )
    await context.generate_schemas()
    first_connection = context.connections.get("shard_a")
    await context.connections.get("shard_b").execute_script(first_connection.get_schema_sql(safe=True))
    return context, Widget


@pytest_asyncio.fixture
async def instrumented(tmp_path):
    InstrumentedRouter.ALIAS = "shard_a"
    InstrumentedRouter.RAISE = False
    InstrumentedRouter.READ_CALLS = 0
    InstrumentedRouter.WRITE_CALLS = 0
    context, widget = await _make_context(tmp_path, "facade_router_laziness")
    try:
        yield context, widget
    finally:
        InstrumentedRouter.ALIAS = "shard_a"
        InstrumentedRouter.RAISE = False
        await context.__aexit__(None, None, None)
        sys.modules.pop(MODULE_NAME, None)


# --- router config change between construction and await lands on the await-time choice ---


@pytest.mark.asyncio
async def test_bulk_create_uses_the_router_choice_active_at_await_not_at_build(instrumented):
    context, widget = instrumented
    query = widget.objects.bulk_create([widget(name="bc-1")])
    InstrumentedRouter.ALIAS = "shard_b"
    await query

    shard_a = context.connections.get("shard_a")
    shard_b = context.connections.get("shard_b")
    _, shard_a_rows = await shard_a.execute("SELECT name FROM widget")
    _, shard_b_rows = await shard_b.execute("SELECT name FROM widget")
    assert shard_a_rows == []
    assert [row["name"] for row in shard_b_rows] == ["bc-1"]


@pytest.mark.asyncio
async def test_bulk_update_uses_the_router_choice_active_at_await_not_at_build(instrumented):
    context, widget = instrumented
    # The same pk is seeded on BOTH shards directly (bypassing the router) so whichever shard
    # bulk_update() actually targets at await time is independently observable.
    shard_a = context.connections.get("shard_a")
    shard_b = context.connections.get("shard_b")
    await shard_a.execute("INSERT INTO widget (id, name) VALUES (1, 'original')")
    await shard_b.execute("INSERT INTO widget (id, name) VALUES (1, 'original')")

    instance = widget(id=1, name="updated")
    query = widget.objects.bulk_update([instance], fields=["name"])
    InstrumentedRouter.ALIAS = "shard_b"
    await query

    _, shard_a_rows = await shard_a.execute("SELECT name FROM widget")
    _, shard_b_rows = await shard_b.execute("SELECT name FROM widget")
    assert [row["name"] for row in shard_a_rows] == ["original"]
    assert [row["name"] for row in shard_b_rows] == ["updated"]


@pytest.mark.asyncio
async def test_all_uses_the_router_choice_active_at_await_not_at_build(instrumented):
    context, widget = instrumented
    shard_a = context.connections.get("shard_a")
    shard_b = context.connections.get("shard_b")
    await shard_a.execute("INSERT INTO widget (id, name) VALUES (1, 'on-a')")
    await shard_b.execute("INSERT INTO widget (id, name) VALUES (2, 'on-b')")

    queryset = widget.objects.all()
    InstrumentedRouter.ALIAS = "shard_b"
    result = await queryset
    assert [row.name for row in result] == ["on-b"]


@pytest.mark.asyncio
async def test_first_uses_the_router_choice_active_at_await_not_at_build(instrumented):
    context, widget = instrumented
    shard_a = context.connections.get("shard_a")
    shard_b = context.connections.get("shard_b")
    await shard_a.execute("INSERT INTO widget (id, name) VALUES (1, 'on-a')")
    await shard_b.execute("INSERT INTO widget (id, name) VALUES (2, 'on-b')")

    query = widget.objects.first()
    InstrumentedRouter.ALIAS = "shard_b"
    result = await query
    assert result is not None
    assert result.name == "on-b"


@pytest.mark.asyncio
async def test_last_uses_the_router_choice_active_at_await_not_at_build(instrumented):
    context, widget = instrumented
    shard_a = context.connections.get("shard_a")
    shard_b = context.connections.get("shard_b")
    await shard_a.execute("INSERT INTO widget (id, name) VALUES (1, 'on-a')")
    await shard_b.execute("INSERT INTO widget (id, name) VALUES (2, 'on-b'), (3, 'on-b-2')")

    query = widget.objects.last()
    InstrumentedRouter.ALIAS = "shard_b"
    result = await query
    assert result is not None
    assert result.name == "on-b-2"


# --- the router is never consulted before await ---


@pytest.mark.asyncio
async def test_all_does_not_consult_the_router_before_await(instrumented):
    _, widget = instrumented
    query = widget.objects.all()
    assert InstrumentedRouter.READ_CALLS == 0
    await query
    assert InstrumentedRouter.READ_CALLS == 1


@pytest.mark.asyncio
async def test_first_does_not_consult_the_router_before_await(instrumented):
    _, widget = instrumented
    query = widget.objects.first()
    assert InstrumentedRouter.READ_CALLS == 0
    await query
    assert InstrumentedRouter.READ_CALLS == 1


@pytest.mark.asyncio
async def test_last_does_not_consult_the_router_before_await(instrumented):
    _, widget = instrumented
    query = widget.objects.last()
    assert InstrumentedRouter.READ_CALLS == 0
    await query
    assert InstrumentedRouter.READ_CALLS == 1


@pytest.mark.asyncio
async def test_bulk_create_does_not_consult_the_router_before_await(instrumented):
    _, widget = instrumented
    query = widget.objects.bulk_create([widget(name="x")])
    assert InstrumentedRouter.WRITE_CALLS == 0
    await query
    assert InstrumentedRouter.WRITE_CALLS == 1


@pytest.mark.asyncio
async def test_bulk_update_does_not_consult_the_router_before_await(instrumented):
    context, widget = instrumented
    shard_a = context.connections.get("shard_a")
    await shard_a.execute("INSERT INTO widget (id, name) VALUES (1, 'original')")
    instance = widget(id=1, name="updated")
    query = widget.objects.bulk_update([instance], fields=["name"])
    assert InstrumentedRouter.WRITE_CALLS == 0
    await query
    assert InstrumentedRouter.WRITE_CALLS == 1


# --- a raising router surfaces its exception at await, not at the synchronous call ---


@pytest.mark.asyncio
async def test_bulk_create_router_exception_surfaces_at_await_not_at_build(instrumented):
    _, widget = instrumented
    query = widget.objects.bulk_create([widget(name="x")])
    InstrumentedRouter.RAISE = True
    with pytest.raises(RuntimeError, match="router exploded"):
        await query


@pytest.mark.asyncio
async def test_bulk_update_router_exception_surfaces_at_await_not_at_build(instrumented):
    context, widget = instrumented
    shard_a = context.connections.get("shard_a")
    await shard_a.execute("INSERT INTO widget (id, name) VALUES (1, 'original')")
    instance = widget(id=1, name="updated")
    query = widget.objects.bulk_update([instance], fields=["name"])
    InstrumentedRouter.RAISE = True
    with pytest.raises(RuntimeError, match="router exploded"):
        await query


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", ["all", "first", "last"])
async def test_all_first_last_router_exception_surfaces_at_await_not_at_build(instrumented, query_type):
    _, widget = instrumented
    query = getattr(widget.objects, query_type)()
    InstrumentedRouter.RAISE = True
    with pytest.raises(RuntimeError, match="router exploded"):
        await query


# --- an explicit using= still pins the connection, bypassing the router entirely ---


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", ["all", "first", "last"])
async def test_using_db_override_still_pins_the_connection(instrumented, query_type):
    context, widget = instrumented
    shard_b = context.connections.get("shard_b")
    await shard_b.execute("INSERT INTO widget (id, name) VALUES (1, 'on-b')")

    result = await getattr(widget.objects.using(shard_b), query_type)()
    if query_type == "all":
        assert [row.name for row in result] == ["on-b"]
    else:
        assert result is not None
        assert result.name == "on-b"


@pytest.mark.asyncio
async def test_using_db_override_still_pins_the_connection_for_bulk_create(instrumented):
    context, widget = instrumented
    shard_a = context.connections.get("shard_a")
    shard_b = context.connections.get("shard_b")
    await widget.objects.using(shard_b).bulk_create([widget(name="bc-1")])

    _, shard_a_rows = await shard_a.execute("SELECT name FROM widget")
    _, shard_b_rows = await shard_b.execute("SELECT name FROM widget")
    assert shard_a_rows == []
    assert [row["name"] for row in shard_b_rows] == ["bc-1"]


@pytest.mark.asyncio
async def test_using_db_override_still_pins_the_connection_for_bulk_update(instrumented):
    context, widget = instrumented
    shard_a = context.connections.get("shard_a")
    shard_b = context.connections.get("shard_b")
    await shard_a.execute("INSERT INTO widget (id, name) VALUES (1, 'original')")
    await shard_b.execute("INSERT INTO widget (id, name) VALUES (1, 'original')")

    instance = widget(id=1, name="updated")
    await widget.objects.using(shard_b).bulk_update([instance], fields=["name"])

    _, shard_a_rows = await shard_a.execute("SELECT name FROM widget")
    _, shard_b_rows = await shard_b.execute("SELECT name FROM widget")
    assert [row["name"] for row in shard_a_rows] == ["original"]
    assert [row["name"] for row in shard_b_rows] == ["updated"]
