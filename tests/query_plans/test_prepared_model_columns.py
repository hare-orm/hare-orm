"""Fixed projections reuse shared column metadata without retaining execution state."""

from unittest.mock import patch

import pytest

from hare.contrib.test import requires_features
from hare.core.caching.caches import Caches
from hare.query.plans.statement.statement_plans import StatementPlans
from tests.shared_models.testmodels import Tournament

pytestmark = pytest.mark.plan_verification_after_test


@requires_features(supports_positional_rows=True)
@pytest.mark.asyncio
async def test_fixed_projection_columns_are_shared_reused_and_invalidated(db):
    obj = await Tournament.objects.create(name="prepared columns")
    Caches.forget_model_caches([Tournament])
    assert (await Tournament.objects.get(pk=obj.pk)).name == obj.name
    assert (await Tournament.objects.get(name=obj.name)).pk == obj.pk
    layouts = [
        plan.result_reading.prepared_columns
        for plan in StatementPlans.plans.get_bucket(Tournament).values()
        if plan.result_reading is not None
    ]
    assert len(layouts) >= 2
    assert all(layout is layouts[0] for layout in layouts)

    with patch.object(
        StatementPlans.row_columns,
        "get_bucket",
        side_effect=AssertionError("a prepared projection must not look up its columns again"),
    ):
        assert (await Tournament.objects.get(pk=obj.pk)).name == obj.name

    Caches.forget_model_caches([Tournament])
    assert (await Tournament.objects.get(pk=obj.pk)).name == obj.name
    fresh_layouts = [
        plan.result_reading.prepared_columns
        for plan in StatementPlans.plans.get_bucket(Tournament).values()
        if plan.result_reading is not None
    ]
    assert fresh_layouts
    assert all(layout is not layouts[0] for layout in fresh_layouts)


@requires_features(supports_positional_rows=True)
@pytest.mark.asyncio
async def test_named_rows_keep_result_based_column_reading(db):
    obj = await Tournament.objects.create(name="named columns")
    connection = Tournament.get_connection()
    with patch.object(connection, "features", connection.features.replace(supports_positional_rows=False)):
        Caches.forget_model_caches([Tournament])
        assert (await Tournament.objects.get(pk=obj.pk)).name == obj.name
        assert (await Tournament.objects.get(name=obj.name)).pk == obj.pk
        plans = list(StatementPlans.plans.get_bucket(Tournament).values())
        assert plans
        assert all(plan.result_reading.prepared_columns is None for plan in plans)


@pytest.mark.asyncio
async def test_a_call_plan_keeps_one_reader_without_a_connection(db):
    """The rows of every query on a plan of calls are read through the one reader kept with the plan
    - made from the calls and the dialect, holding no connection; each query passes its own."""
    from hare.query.rows.model_rows.model_rows import ModelRows

    obj = await Tournament.objects.create(name="kept reader")
    Caches.forget_model_caches([Tournament])
    await Tournament.objects.get(pk=obj.pk)
    readers = []
    connections = []
    fetch = ModelRows.fetch

    async def recording_fetch(self, connection, *args, **kwargs):
        readers.append(self)
        connections.append(connection)
        return await fetch(self, connection, *args, **kwargs)

    with patch.object(ModelRows, "fetch", recording_fetch):
        assert (await Tournament.objects.get(pk=obj.pk)).name == obj.name
        assert (await Tournament.objects.get(pk=obj.pk)).name == obj.name
    assert len(readers) == 2
    assert readers[0] is readers[1]
    assert not hasattr(readers[0], "connection")
    assert connections[0] is Tournament.get_connection()


@requires_features(supports_positional_rows=True)
@pytest.mark.asyncio
async def test_prepared_columns_keep_one_native_layout_for_every_query(db):
    """The native layout making the instances is made once with the plan's columns, not per query."""
    from hare.native.native_modules import NativeModules
    from hare.query.rows.model_rows.model_rows import ModelRows

    obj = await Tournament.objects.create(name="kept layout")
    Caches.forget_model_caches([Tournament])
    await Tournament.objects.get(pk=obj.pk)
    layouts = []
    read_batch = ModelRows.read_batch

    def recording_read_batch(self, connection, result, layout, **kwargs):
        layouts.append(layout)
        return read_batch(self, connection, result, layout, **kwargs)

    with patch.object(ModelRows, "read_batch", recording_read_batch):
        assert (await Tournament.objects.get(pk=obj.pk)).name == obj.name
        assert (await Tournament.objects.get(pk=obj.pk)).name == obj.name
    assert isinstance(layouts[0], NativeModules.rows.ModelResultLayout)
    assert layouts[0] is layouts[1]
